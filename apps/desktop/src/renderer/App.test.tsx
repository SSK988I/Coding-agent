import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentMessage, RuntimeEvent, WorkspacePayload } from "../shared/types";
import { App } from "./App";

function renderApp() {
  return render(
    <VirtuosoMockContext.Provider value={{ viewportHeight: 900, itemHeight: 120 }}>
      <App />
    </VirtuosoMockContext.Provider>,
  );
}

const latestPlan = {
  planId: "plan-1",
  revision: 2,
  title: "Plan title",
  markdown: "# Plan title\n\n## Summary\nS\n\n## Implementation Changes\nI\n\n## Public Interfaces\nP\n\n## Test Plan\nT\n\n## Assumptions\nA",
  digest: "abcdef0123456789",
  sourceMessageId: "message-2",
};

function workspace(overrides: Partial<WorkspacePayload> = {}): WorkspacePayload {
  return {
    path: "G:\\Coding-agent",
    sessionId: "session-1",
    model: { id: "m", name: "Model", provider: "test" },
    thinkingLevel: null,
    tools: ["read", "request_user_input"],
    messages: [],
    collaborationMode: "plan",
    planState: {
      phase: "ready",
      activePlanId: "plan-1",
      latestRevision: latestPlan,
      pendingQuestion: null,
    },
    memory: { enabled: true, userId: "local-user", projectId: "sha256:test" },
    ...overrides,
  };
}

describe("desktop Plan Mode", () => {
  const requests = vi.fn();
  let eventListener: ((event: RuntimeEvent) => void) | undefined;

  beforeEach(() => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace();
      if (method === "session.list" || method === "command.list") return [];
      return {};
    });
    window.agent = {
      request: requests,
      onEvent: (listener) => { eventListener = listener; return () => undefined; },
      onStatus: () => () => undefined,
    };
    window.desktop = {
      chooseWorkspace: async () => null,
      getBootstrap: async () => ({ defaultWorkspace: "G:\\Coding-agent", platform: "win32" }),
    };
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("executes only the current revision and digest", async () => {
    renderApp();
    await screen.findByTestId("plan-card");
    const decision = await screen.findByTestId("plan-decision");
    expect(decision).toHaveTextContent("计划已提交，下一步怎么做？");
    fireEvent.click(screen.getByRole("button", { name: /执行方案/ }));
    await waitFor(() => expect(requests).toHaveBeenCalledWith("plan.execute", {
      planId: "plan-1", revision: 2, digest: "abcdef0123456789",
    }));
    await waitFor(() => expect(requests).toHaveBeenCalledWith("session.snapshot"));
  });

  it("lets the user supplement the ready plan from the composer", async () => {
    renderApp();
    await screen.findByTestId("plan-decision");
    fireEvent.click(screen.getByRole("button", { name: /继续修改/ }));
    const composer = screen.getByPlaceholderText("补充你的想法或修改要求…");
    await waitFor(() => expect(composer).toHaveFocus());
    expect(requests).not.toHaveBeenCalledWith("plan.execute", expect.anything());
  });

  it("uses a complete plan state snapshot from the live state event", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        planState: {
          phase: "drafting",
          activePlanId: "plan-1",
          latestRevision: null,
          pendingQuestion: null,
        },
      });
      if (method === "session.list" || method === "command.list") return [];
      return {};
    });
    renderApp();
    await screen.findByText("把 Agent 放进一个真正的工作区");

    eventListener?.({
      v: 1, type: "event", seq: 1, timestamp: Date.now(), sessionId: "session-1", runId: "run-1",
      event: {
        type: "plan.stateChanged",
        payload: {
          sessionId: "session-1",
          state: {
            mode: "plan",
            phase: "ready",
            activePlanId: "plan-1",
            latestRevision: latestPlan,
            pendingQuestion: null,
            latestRun: null,
            recoveryError: null,
            handoffTargetSessionId: null,
          },
        },
      },
    });

    expect(await screen.findByTestId("plan-decision")).toHaveTextContent("执行方案");
    expect(screen.getByTestId("plan-decision")).toHaveTextContent("继续修改");
  });

  it("ignores a delayed plan snapshot from another session", async () => {
    render(<App />);
    await screen.findByTestId("plan-decision");

    eventListener?.({
      v: 1, type: "event", seq: 2, timestamp: Date.now(), sessionId: "session-old", runId: null,
      event: {
        type: "plan.stateChanged",
        payload: {
          sessionId: "session-old",
          state: {
            mode: "default",
            phase: "uncertain",
            activePlanId: "plan-old",
            latestRevision: null,
            pendingQuestion: null,
          },
        },
      },
    });

    await waitFor(() => expect(screen.getByTestId("plan-decision")).toHaveTextContent("执行方案"));
    expect(screen.queryByText("执行状态不确定")).not.toBeInTheDocument();
  });

  it("keeps the authoritative ready state and rehydrates it when execute RPC fails", async () => {
    let rejectExecute: ((reason: Error) => void) | undefined;
    const executeResult = new Promise((_, reject) => {
      rejectExecute = reject;
    });
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace();
      if (method === "session.list" || method === "command.list") return [];
      if (method === "plan.execute") return executeResult;
      if (method === "session.snapshot") return {
        sessionId: "session-1",
        messages: [],
        stats: {},
        collaborationMode: "plan",
        planState: workspace().planState,
      };
      return {};
    });

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /执行方案/ }));

    expect(screen.getByTestId("plan-decision")).toHaveTextContent("正在确认…");
    expect(screen.getByTestId("plan-card")).toHaveTextContent("PLAN READY");
    rejectExecute?.(new Error("revision rejected"));
    await waitFor(() => expect(requests).toHaveBeenCalledWith("session.snapshot"));
    expect(await screen.findByText("revision rejected")).toBeInTheDocument();
    expect(screen.getByTestId("plan-decision")).toHaveTextContent("执行方案");
    expect(screen.getByTestId("plan-card")).toHaveTextContent("Plan title");
  });

  it("hands the exact ready revision to a fresh review session", async () => {
    const childPlan = { ...latestPlan, title: "Fresh review" };
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace();
      if (method === "session.list" || method === "command.list") return [];
      if (method === "plan.handoff") return workspace({
        sessionId: "session-2",
        planState: {
          mode: "plan",
          phase: "ready",
          activePlanId: "plan-1",
          latestRevision: childPlan,
          pendingQuestion: null,
        },
      });
      return {};
    });

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /新会话复核/ }));

    await waitFor(() => expect(requests).toHaveBeenCalledWith("plan.handoff", {
      planId: "plan-1", revision: 2, digest: "abcdef0123456789",
    }));
    expect(await screen.findByTestId("plan-card")).toHaveTextContent("Fresh review");
    expect(screen.getByTestId("plan-decision")).toHaveTextContent("新会话复核");
  });

  it.each([
    ["uncertain", "执行状态不确定"],
    ["recovery_error", "计划恢复失败"],
  ] as const)("shows a clear %s recovery state", async (phase, heading) => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        collaborationMode: phase === "recovery_error" ? "plan" : "default",
        planState: {
          mode: phase === "recovery_error" ? "plan" : "default",
          phase,
          activePlanId: "plan-1",
          latestRevision: latestPlan,
          pendingQuestion: null,
          recoveryError: phase === "recovery_error"
            ? { code: "PLAN_DIGEST_MISMATCH", message: "digest mismatch", entryId: "entry-1" }
            : null,
        },
      });
      if (method === "session.list" || method === "command.list") return [];
      return {};
    });

    render(<App />);

    expect(await screen.findByTestId("plan-status")).toHaveTextContent(heading);
    expect(await screen.findByTestId("plan-card")).toHaveTextContent("Plan title");
  });

  it("renders a structured question and submits the selected answer", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        planState: {
          phase: "awaiting_answer",
          activePlanId: "plan-1",
          latestRevision: null,
          pendingQuestion: {
            questionId: "question-1",
            header: "范围",
            question: "选择范围？",
            options: [{ label: "核心", description: "只改核心" }, { label: "全部", description: "改双端" }],
            allowCustom: true,
          },
        },
      });
      if (method === "session.list" || method === "command.list") return [];
      return {};
    });
    renderApp();
    await screen.findByTestId("plan-question");
    fireEvent.click(screen.getByRole("button", { name: /核心/ }));
    await waitFor(() => expect(requests).toHaveBeenCalledWith("plan.answer", {
      questionId: "question-1", answer: "核心",
    }));
  });

  it.each(["uncertain", "recovery_error"] as const)("can explicitly replan from %s", async (phase) => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        collaborationMode: phase === "uncertain" ? "default" : "plan",
        planState: { phase, activePlanId: null, latestRevision: null, pendingQuestion: null },
      });
      if (method === "mode.enterPlan") return workspace({
        planState: { phase: "drafting", activePlanId: "new-plan", latestRevision: null, pendingQuestion: null },
      });
      if (method === "session.list" || method === "command.list") return [];
      return {};
    });
    render(<App />);
    await screen.findByTestId("plan-status");
    expect(screen.getByRole("textbox")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重新规划" }));
    await waitFor(() => expect(requests).toHaveBeenCalledWith("mode.enterPlan"));
    await waitFor(() => expect(screen.queryByTestId("plan-status")).not.toBeInTheDocument());
    expect(requests).not.toHaveBeenCalledWith("plan.execute", expect.anything());
  });

  it("can cancel corrupt state without a recoverable plan ID", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        planState: { phase: "recovery_error", activePlanId: null, latestRevision: null, pendingQuestion: null },
      });
      if (method === "plan.cancel") return workspace({
        collaborationMode: "default",
        planState: { phase: "cancelled", activePlanId: null, latestRevision: null, pendingQuestion: null },
      });
      if (method === "session.list" || method === "command.list") return [];
      return {};
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "取消规划" }));
    await waitFor(() => expect(requests).toHaveBeenCalledWith("plan.cancel", { planId: null }));
    await waitFor(() => expect(screen.queryByTestId("plan-status")).not.toBeInTheDocument());
  });

  it("identifies legacy prose as a candidate that cannot execute", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        planState: { phase: "drafting", activePlanId: "old", latestRevision: null,
          pendingQuestion: null, legacyCandidate: true },
      });
      if (method === "session.list" || method === "command.list") return [];
      return {};
    });
    render(<App />);
    expect(await screen.findByTestId("plan-legacy")).toHaveTextContent("submit_plan");
    expect(screen.queryByTestId("plan-decision")).not.toBeInTheDocument();
  });

  it("ignores granular Plan state events even before a live full snapshot", async () => {
    render(<App />);
    await screen.findByTestId("plan-decision");
    await screen.findByTestId("plan-card");
    eventListener?.({
      v: 1, type: "event", seq: 1, timestamp: Date.now(), sessionId: "session-1", runId: "old-run",
      event: { type: "plan_execution_started", payload: {} },
    });
    expect(screen.getByTestId("plan-decision")).toHaveTextContent("执行方案");
    expect(screen.getByTestId("plan-card")).toHaveTextContent("PLAN READY");
  });

  it("disables Plan actions while a run is active", async () => {
    renderApp();
    await screen.findByTestId("plan-card");
    eventListener?.({
      v: 1, type: "event", seq: 1, timestamp: Date.now(), sessionId: "session-1", runId: "run-1",
      event: { type: "run.started", payload: {} },
    });
    await waitFor(() => expect(screen.getByRole("button", { name: /执行方案/ })).toBeDisabled());
    expect(screen.getByRole("button", { name: "取消规划" })).toBeDisabled();
  });

  it("keeps trailing stream events when a session snapshot arrives in the same frame", async () => {
    renderApp();
    await screen.findByTestId("plan-card");
    const nextWorkspace = workspace({
      sessionId: "session-2",
      messages: [{ role: "user", content: "new session transcript", timestamp: 20 }],
      collaborationMode: "default",
      planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
    });

    eventListener?.({
      v: 1, type: "event", seq: 10, timestamp: 10, sessionId: "session-2", runId: null,
      event: { type: "session.changed", payload: nextWorkspace as unknown as Record<string, unknown> },
    });
    eventListener?.({
      v: 1, type: "event", seq: 11, timestamp: 11, sessionId: "session-2", runId: "run-2",
      event: { type: "message_start", payload: { message: { role: "assistant", content: "" } } },
    });
    eventListener?.({
      v: 1, type: "event", seq: 12, timestamp: 12, sessionId: "session-2", runId: "run-2",
      event: { type: "message_update", payload: { kind: "text_delta", delta: "live response" } },
    });

    expect(await screen.findByText("new session transcript")).toBeInTheDocument();
    expect(await screen.findByText("live response")).toBeInTheDocument();
  });
});

describe("desktop context compaction", () => {
  const requests = vi.fn();
  let eventListener: ((event: RuntimeEvent) => void) | undefined;

  beforeEach(() => {
    window.agent = {
      request: requests,
      onEvent: (listener) => { eventListener = listener; return () => undefined; },
      onStatus: () => () => undefined,
    };
    window.desktop = {
      chooseWorkspace: async () => null,
      getBootstrap: async () => ({ defaultWorkspace: "G:\\Coding-agent", platform: "win32" }),
    };
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("shows progress immediately and completion when /compact resolves", async () => {
    let resolveCompact: ((value: Record<string, unknown>) => void) | undefined;
    const compactResult = new Promise<Record<string, unknown>>((resolve) => {
      resolveCompact = resolve;
    });
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        collaborationMode: "default",
        planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
      });
      if (method === "session.list") return [];
      if (method === "command.list") return [
        { name: "compact", label: "压缩上下文", description: "立即压缩较早的会话内容" },
      ];
      if (method === "session.compact") return compactResult;
      return {};
    });

    renderApp();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/compact" } });
    fireEvent.click(await screen.findByRole("button", { name: /压缩上下文/ }));

    expect(await screen.findByText("正在压缩上下文…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "压缩中…" })).toBeDisabled();

    resolveCompact?.({ performed: true, summary_preview: "保留了关键实现决策" });

    expect(await screen.findByText("上下文压缩完成")).toBeInTheDocument();
    expect(screen.getByText("保留了关键实现决策")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "发送 ↑" })).toBeInTheDocument());
  });

  it("restores a persisted compaction summary after reopening a session", async () => {
    const compactedMessage = {
      role: "compactionSummary",
      content: "",
      summary: "此前已经完成桌面端压缩，并保留关键上下文。",
      tokens_before: 8120,
      timestamp: 1_724_470_000,
    } as AgentMessage;
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        collaborationMode: "default",
        messages: [compactedMessage],
        planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
      });
      if (method === "session.list" || method === "command.list") return [];
      return {};
    });

    renderApp();

    expect(await screen.findByText("上下文压缩完成")).toBeInTheDocument();
    expect(screen.getByText("此前已经完成桌面端压缩，并保留关键上下文。")).toBeInTheDocument();
    expect(screen.getByText("压缩前约 8,120 tokens")).toBeInTheDocument();
  });

  it("renders automatic compaction lifecycle events without waiting for another chat refresh", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        collaborationMode: "default",
        planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
      });
      if (method === "session.list" || method === "command.list") return [];
      return {};
    });
    renderApp();
    await screen.findByPlaceholderText("描述你想完成的任务…");

    eventListener?.({
      v: 1, type: "event", seq: 10, timestamp: Date.now(), sessionId: "session-1", runId: "run-1",
      event: { type: "compaction_start", payload: { reason: "threshold" } },
    });
    expect(await screen.findByText("正在压缩上下文…")).toBeInTheDocument();

    eventListener?.({
      v: 1, type: "event", seq: 11, timestamp: Date.now(), sessionId: "session-1", runId: "run-1",
      event: { type: "compaction_end", payload: { reason: "threshold", aborted: false, summary_preview: "自动压缩摘要" } },
    });
    expect(await screen.findByText("上下文压缩完成")).toBeInTheDocument();
    expect(screen.getByText("自动压缩摘要")).toBeInTheDocument();
  });
});

describe("desktop long-term memory", () => {
  const requests = vi.fn();
  const memoryCommand = {
    name: "memory",
    label: "长期记忆",
    description: "查看和管理用户画像与项目决策",
  };

  beforeEach(() => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        collaborationMode: "default",
        planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
        memory: {
          enabled: true,
          autoExtractEnabled: true,
          userId: "local-user",
          projectId: "sha256:test",
          pendingCount: 5,
          processingCount: 1,
          readyCount: 8,
          failedCount: 2,
        },
      });
      if (method === "session.list") return [];
      if (method === "command.list") return [memoryCommand];
      if (method === "memory.status") return {
        enabled: true,
        autoExtractEnabled: true,
        userId: "local-user",
        projectId: "sha256:test",
        globalCount: 2,
        projectCount: 3,
        conflictCount: 1,
        pendingCount: 2,
        processingCount: 1,
        readyCount: 4,
        failedCount: 1,
        lastError: "extract failed",
        root: "C:\\memory",
      };
      if (method === "memory.setEnabled") return {
        enabled: false,
        autoExtractEnabled: true,
        userId: "local-user",
        projectId: "sha256:test",
      };
      if (method === "memory.setAutoExtract") return {
        enabled: true,
        autoExtractEnabled: false,
        userId: "local-user",
        projectId: "sha256:test",
      };
      if (method === "memory.remember") return {
        record: { id: "mem_manual_01" },
        memory: {
          enabled: true,
          autoExtractEnabled: true,
          userId: "local-user",
          projectId: "sha256:test",
          globalCount: 3,
          projectCount: 3,
        },
      };
      if (method === "memory.forget") return {
        removed: true,
        memory: {
          enabled: true,
          userId: "local-user",
          projectId: "sha256:test",
          globalCount: 1,
          projectCount: 3,
          conflictCount: 1,
        },
      };
      return {};
    });
    window.agent = {
      request: requests,
      onEvent: () => () => undefined,
      onStatus: () => () => undefined,
    };
    window.desktop = {
      chooseWorkspace: async () => null,
      getBootstrap: async () => ({ defaultWorkspace: "G:\\Coding-agent", platform: "win32" }),
    };
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("toggles memory from the workspace sidebar", async () => {
    renderApp();
    fireEvent.click(await screen.findByRole("button", { name: "已开启" }));

    await waitFor(() => expect(requests).toHaveBeenCalledWith("memory.setEnabled", { enabled: false }));
    expect(await screen.findByRole("button", { name: "已关闭" })).toBeInTheDocument();
    expect(screen.getByText("长期记忆已关闭。")).toBeInTheDocument();
  });

  it("renders the authoritative memory overview returned while opening a workspace", async () => {
    renderApp();

    expect(await screen.findByText(
      /待处理 5 · 处理中 1 · 已完成 8 · 失败 2/
    )).toBeInTheDocument();
  });

  it("shows status without sending the slash command to the model", async () => {
    renderApp();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/memory status" } });
    fireEvent.click(screen.getByRole("button", { name: "发送 ↑" }));

    await waitFor(() => expect(requests).toHaveBeenCalledWith("memory.status"));
    expect(await screen.findByText("长期记忆状态")).toBeInTheDocument();
    expect(screen.getByText(/待处理.*2.*处理中.*1.*已完成.*4.*失败.*1/)).toBeInTheDocument();
    expect(screen.getByText(/extract failed/)).toBeInTheDocument();
    expect(requests).not.toHaveBeenCalledWith("run.start", expect.anything());
  });

  it("toggles automatic extraction independently", async () => {
    renderApp();
    fireEvent.click(await screen.findByRole("button", { name: "自动提取开启" }));

    await waitFor(() => expect(requests).toHaveBeenCalledWith("memory.setAutoExtract", {
      enabled: false,
    }));
    expect(await screen.findByRole("button", { name: "自动提取关闭" })).toBeInTheDocument();
    expect(screen.getByText("长期记忆自动提取已关闭。")).toBeInTheDocument();
  });

  it("writes an explicit memory without sending it to the main model", async () => {
    renderApp();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/memory remember 偏好中文回答 --project" } });
    fireEvent.click(screen.getByRole("button", { name: "发送 ↑" }));

    await waitFor(() => expect(requests).toHaveBeenCalledWith("memory.remember", {
      content: "偏好中文回答", scope: "project",
    }));
    expect((await screen.findAllByText(/mem_manual_01/)).length).toBeGreaterThan(0);
    expect(requests).not.toHaveBeenCalledWith("run.start", expect.anything());
  });

  it("keeps flag-shaped text before a trailing remember scope", async () => {
    renderApp();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, {
      target: { value: "/memory remember 项目安装必须使用 --frozen-lockfile --project" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送 ↑" }));

    await waitFor(() => expect(requests).toHaveBeenCalledWith("memory.remember", {
      content: "项目安装必须使用 --frozen-lockfile", scope: "project",
    }));
  });

  it("treats remember arguments after the terminator as literal content", async () => {
    renderApp();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, {
      target: { value: "/memory remember \"保留字面参数\" -- --project" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送 ↑" }));

    await waitFor(() => expect(requests).toHaveBeenCalledWith("memory.remember", {
      content: "保留字面参数 --project", scope: "global",
    }));
  });

  it("requires explicit confirmation before forgetting a memory", async () => {
    renderApp();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/memory forget response.language" } });
    fireEvent.click(screen.getByRole("button", { name: "发送 ↑" }));

    expect(await screen.findByText(/memory forget.*--confirm/)).toBeInTheDocument();
    expect(requests).not.toHaveBeenCalledWith("memory.forget", expect.anything());

    fireEvent.change(composer, { target: { value: "/memory forget response.language --confirm" } });
    fireEvent.click(screen.getByRole("button", { name: "发送 ↑" }));
    await waitFor(() => expect(requests).toHaveBeenCalledWith("memory.forget", {
      key: "response.language", scope: undefined, confirmed: true,
    }));
    expect(await screen.findByText("记忆已遗忘并写入墓碑。")).toBeInTheDocument();
  });
});
