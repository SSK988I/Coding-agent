import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentMessage, RuntimeEvent, WorkspacePayload } from "../shared/types";
import { App } from "./App";

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
    render(<App />);
    await screen.findByTestId("plan-card");
    const decision = await screen.findByTestId("plan-decision");
    expect(decision).toHaveTextContent("计划已完成，下一步怎么做？");
    fireEvent.click(screen.getByRole("button", { name: /执行方案/ }));
    await waitFor(() => expect(requests).toHaveBeenCalledWith("plan.execute", {
      planId: "plan-1", revision: 2, digest: "abcdef0123456789",
    }));
  });

  it("lets the user supplement the ready plan from the composer", async () => {
    render(<App />);
    await screen.findByTestId("plan-decision");
    fireEvent.click(screen.getByRole("button", { name: /补充想法/ }));
    const composer = screen.getByPlaceholderText("补充你的想法或修改要求…");
    await waitFor(() => expect(composer).toHaveFocus());
    expect(requests).not.toHaveBeenCalledWith("plan.execute", expect.anything());
  });

  it("shows the decision selector when a live plan_ready event arrives", async () => {
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
    render(<App />);
    await screen.findByText("把 Agent 放进一个真正的工作区");

    eventListener?.({
      v: 1, type: "event", seq: 1, timestamp: Date.now(), sessionId: "session-1", runId: "run-1",
      event: { type: "plan_ready", payload: { plan: latestPlan } },
    });

    expect(await screen.findByTestId("plan-decision")).toHaveTextContent("执行方案");
    expect(screen.getByTestId("plan-decision")).toHaveTextContent("补充想法");
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
    render(<App />);
    await screen.findByTestId("plan-question");
    fireEvent.click(screen.getByRole("button", { name: /核心/ }));
    await waitFor(() => expect(requests).toHaveBeenCalledWith("plan.answer", {
      questionId: "question-1", answer: "核心",
    }));
  });

  it("disables Plan actions while a run is active", async () => {
    render(<App />);
    await screen.findByTestId("plan-card");
    eventListener?.({
      v: 1, type: "event", seq: 1, timestamp: Date.now(), sessionId: "session-1", runId: "run-1",
      event: { type: "run.started", payload: {} },
    });
    await waitFor(() => expect(screen.getByRole("button", { name: /执行方案/ })).toBeDisabled());
    expect(screen.getByRole("button", { name: "取消规划" })).toBeDisabled();
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

    render(<App />);
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

    render(<App />);

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
    render(<App />);
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
