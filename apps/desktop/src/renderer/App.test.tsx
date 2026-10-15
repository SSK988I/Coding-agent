import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentMessage, RuntimeEvent, SubagentTaskPayload, WorkspacePayload } from "../shared/types";
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

  it.each([
    ["open", false], ["open", true], ["new", false], ["new", true],
  ] as const)("responds to the first sidebar %s click (snapshot first: %s)", async (action, eventFirst) => {
    const initial = workspace({
      collaborationMode: "default",
      planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
      messages: [{ role: "user", content: "Original conversation", timestamp: 1 }],
    });
    const next = { ...initial, sessionId: "session-2", messages: action === "open"
      ? [{ role: "user", content: "Selected conversation", timestamp: 2 }]
      : [] };
    let resolveNavigation: ((value: WorkspacePayload) => void) | undefined;
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return initial;
      if (method === "session.list") return [
        { id: "session-1", name: "Original session", message_count: 1 },
        { id: "session-2", name: "Selected session", message_count: 1 },
      ];
      if (method === "command.list") return [];
      if (method === `session.${action}`) return new Promise<WorkspacePayload>((resolve) => { resolveNavigation = resolve; });
      return {};
    });
    renderApp();
    const target = action === "open"
      ? await screen.findByRole("button", { name: /Selected session/ })
      : await screen.findByTitle("新建会话");
    await screen.findByText("Original conversation");
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Old session draft" } });

    fireEvent.click(target);
    expect(screen.getByRole("status")).toHaveTextContent(action === "open" ? "正在切换会话" : "正在新建会话");
    expect(target).toBeDisabled();
    fireEvent.click(target);
    expect(requests.mock.calls.filter(([method]) => method === `session.${action}`)).toHaveLength(1);
    expect(screen.getByText("Original conversation")).toBeInTheDocument();
    if (eventFirst) {
      await act(async () => {
        eventListener?.({
          v: 1, type: "event", seq: 1, timestamp: 1, sessionId: next.sessionId, runId: null,
          event: { type: "session.changed", payload: next as unknown as Record<string, unknown> },
        });
        await new Promise(requestAnimationFrame);
      });
    }
    await act(async () => { resolveNavigation?.(next); });

    await waitFor(() => expect(screen.queryByText("Original conversation")).not.toBeInTheDocument());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByTitle("新建会话")).toBeEnabled();
    expect(screen.getByRole("textbox")).toHaveValue("");
    if (action === "open") expect(screen.getByText("Selected conversation")).toBeInTheDocument();
  });

  it.each([
    ["open", false], ["open", true], ["new", false], ["new", true],
  ] as const)("does not undo a single %s click with an old queued snapshot (snapshot first: %s)", async (action, eventFirst) => {
    const initial = workspace({ messages: [{ role: "user", content: "Original conversation", timestamp: 1 }] });
    const next = workspace({ sessionId: "session-2", messages: [{ role: "user", content: "Selected conversation", timestamp: 2 }] });
    let resolveNavigation: ((value: WorkspacePayload) => void) | undefined;
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return initial;
      if (method === "session.list") return [{ id: "session-2", name: "Selected session", message_count: 1 }];
      if (method === "command.list") return [];
      if (method === `session.${action}`) return new Promise<WorkspacePayload>((resolve) => { resolveNavigation = resolve; });
      return {};
    });
    renderApp();
    await screen.findByText("Original conversation");
    fireEvent.click(action === "open"
      ? await screen.findByRole("button", { name: /Selected session/ })
      : screen.getByTitle("新建会话"));
    if (eventFirst) {
      await act(async () => {
        eventListener?.({
          v: 1, type: "event", seq: 1, timestamp: 1, sessionId: next.sessionId, runId: null,
          event: { type: "session.changed", payload: next as unknown as Record<string, unknown> },
        });
        await new Promise(requestAnimationFrame);
      });
    }
    await act(async () => {
      eventListener?.({
        v: 1, type: "event", seq: 2, timestamp: 2, sessionId: initial.sessionId, runId: null,
        event: { type: "session.changed", payload: initial as unknown as Record<string, unknown> },
      });
      resolveNavigation?.(next);
      await new Promise(requestAnimationFrame);
    });

    expect(screen.getByText("Selected conversation")).toBeInTheDocument();
    expect(screen.queryByText("Original conversation")).not.toBeInTheDocument();
    expect(requests.mock.calls.filter(([method]) => method === `session.${action}`)).toHaveLength(1);
  });

  it.each([
    ["open", "sidecar unavailable"], ["new", "sidecar unavailable"],
    ["open", "REQUEST_CANCELLED: 请求被后台任务中断，请重试"],
    ["new", "REQUEST_CANCELLED: 请求被后台任务中断，请重试"],
  ])("shows a failed %s request (%s) and allows retry without losing the draft", async (action, reason) => {
    const initial = workspace({
      collaborationMode: "default",
      planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
      messages: [{ role: "user", content: "Original conversation", timestamp: 1 }],
    });
    let attempts = 0;
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return initial;
      if (method === "session.list") return [{ id: "session-2", name: "Selected session", message_count: 1 }];
      if (method === "command.list") return [];
      if (method === `session.${action}`) {
        attempts++;
        if (attempts === 1) throw new Error(reason);
        return { ...initial, sessionId: "session-2", messages: [] };
      }
      return {};
    });
    renderApp();
    await screen.findByText("Original conversation");
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Unsent draft" } });
    const target = action === "open"
      ? await screen.findByRole("button", { name: /Selected session/ })
      : screen.getByTitle("新建会话");
    fireEvent.click(target);

    expect(await screen.findByRole("alert")).toHaveTextContent(reason);
    expect(screen.getByText("Original conversation")).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("Unsent draft");
    expect(screen.getByRole("textbox")).toBeEnabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(target).toBeEnabled();
    expect(attempts).toBe(1);
    fireEvent.click(target);

    await waitFor(() => expect(screen.queryByText("Original conversation")).not.toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("");
    expect(attempts).toBe(2);
  });

  it("preserves new-session stream events while dropping an old queued snapshot", async () => {
    const initial = workspace({ messages: [{ role: "user", content: "Original conversation", timestamp: 1 }] });
    const next = workspace({ sessionId: "session-2", messages: [{ role: "user", content: "Selected conversation", timestamp: 2 }] });
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return initial;
      if (method === "session.list") return [{ id: "session-2", name: "Selected session", message_count: 1 }];
      if (method === "command.list") return [];
      if (method === "session.open") {
        eventListener?.({
          v: 1, type: "event", seq: 1, timestamp: 1, sessionId: initial.sessionId, runId: null,
          event: { type: "session.changed", payload: initial as unknown as Record<string, unknown> },
        });
        eventListener?.({
          v: 1, type: "event", seq: 2, timestamp: 2, sessionId: next.sessionId, runId: "next-run",
          event: { type: "message_start", payload: { message: { role: "assistant", content: "" } } },
        });
        eventListener?.({
          v: 1, type: "event", seq: 3, timestamp: 3, sessionId: next.sessionId, runId: "next-run",
          event: { type: "message_update", payload: { kind: "text_delta", delta: "Live selected response" } },
        });
        return next;
      }
      return {};
    });
    renderApp();
    fireEvent.click(await screen.findByRole("button", { name: /Selected session/ }));

    expect(await screen.findByText("Live selected response")).toBeInTheDocument();
    expect(screen.getByText("Selected conversation")).toBeInTheDocument();
    expect(screen.queryByText("Original conversation")).not.toBeInTheDocument();
  });

  it("shows all permission modes and applies only the saved server choice", async () => {
    let resolveMode: ((value: unknown) => void) | undefined;
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({ permissionMode: "auto" });
      if (method === "permission.setMode") return new Promise((resolve) => { resolveMode = resolve; });
      if (method === "session.list" || method === "command.list") return [];
      return {};
    });
    renderApp();
    fireEvent.click(await screen.findByRole("button", { name: "访问权限：帮我批准" }));
    expect(screen.getAllByRole("menuitemradio")).toHaveLength(3);
    expect(screen.getByRole("menuitemradio", { name: /帮我批准/ })).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByRole("menuitemradio", { name: /完全访问权限/ }));
    expect(requests).toHaveBeenCalledWith("permission.setMode", { mode: "full_access" });
    expect(screen.getByRole("button", { name: "访问权限：帮我批准" })).toBeDisabled();
    act(() => resolveMode?.({ sessionId: "session-1", permissionMode: "full_access" }));
    expect(await screen.findByRole("button", { name: "访问权限：完全访问权限" })).toBeEnabled();
  });

  it("restores authoritative permission state after a rejected change", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({ permissionMode: "auto" });
      if (method === "permission.setMode") throw new Error("权限保存失败");
      if (method === "session.snapshot") return workspace({ permissionMode: "ask" });
      if (method === "session.list" || method === "command.list") return [];
      return {};
    });
    renderApp();
    fireEvent.click(await screen.findByRole("button", { name: "访问权限：帮我批准" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: /完全访问权限/ }));
    expect(await screen.findByText("权限保存失败")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "访问权限：请求批准" })).toBeEnabled();
    expect(requests).toHaveBeenCalledWith("session.snapshot");
  });

  it("supports keyboard dismissal and clears the permission menu on session changes", async () => {
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "访问权限：请求批准" })).toBeEnabled());
    const trigger = screen.getByRole("button", { name: "访问权限：请求批准" });
    fireEvent.click(trigger);
    expect(screen.getByRole("menuitemradio", { name: /请求批准/ })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(screen.getByRole("menuitemradio", { name: /帮我批准/ })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    fireEvent.click(trigger);
    act(() => eventListener?.({
      v: 1, type: "event", seq: 1, timestamp: Date.now(), sessionId: "session-2", runId: null,
      event: { type: "session.changed", payload: workspace({ sessionId: "session-2", permissionMode: "full_access" }) as unknown as Record<string, unknown> },
    }));
    await screen.findByRole("button", { name: "访问权限：完全访问权限" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it.each([true, false])("resolves native network permission with approved=%s", async (approved) => {
    renderApp();
    await screen.findByRole("button", { name: "访问权限：请求批准" });
    act(() => eventListener?.({
      v: 1, type: "event", seq: 1, timestamp: Date.now(), sessionId: "session-1", runId: "run-1",
      event: { type: "permission.networkRequested", payload: { approvalId: "network-1", message: "允许本次任务联网？" } },
    }));
    await screen.findByRole("region", { name: "联网批准" });
    fireEvent.click(screen.getByRole("button", { name: approved ? "允许本次联网" : "继续离线" }));
    await waitFor(() => expect(requests).toHaveBeenCalledWith("approval.resolve", { approvalId: "network-1", approved }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "联网批准" })).not.toBeInTheDocument());
  });

  it("reviews the current plan read-only without executing it or clearing the composer", async () => {
    const task: SubagentTaskPayload = {
      taskId: "child-1", sessionId: "session-1", purpose: "review", prompt: "review plan",
      status: "running", turns: 1, maxTurns: 12, timeoutSeconds: 180,
      lastTool: "read", output: "", error: null,
    };
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace();
      if (method === "session.list" || method === "command.list") return [];
      if (method === "subagent.spawn") return task;
      if (method === "subagent.list") return [task];
      return {};
    });
    renderApp();
    fireEvent.click(await screen.findByRole("button", { name: "只读复核当前计划" }));
    await waitFor(() => expect(requests).toHaveBeenCalledWith("subagent.spawn", {
      task: expect.stringContaining(latestPlan.markdown), purpose: "review",
    }));
    expect(await screen.findByText("review plan", { selector: "summary span" })).toBeInTheDocument();
    expect(requests).not.toHaveBeenCalledWith("mode.enterPlan");
    expect(requests.mock.calls.some(([method]) => method === "plan.execute" || method === "run.start")).toBe(false);
    act(() => eventListener?.({
      v: 1, type: "event", seq: 3, timestamp: 1, sessionId: "session-1", runId: null,
      event: { type: "subagent.stateChanged", payload: { tasks: [{ ...task, status: "completed", output: "src/app.py:12 evidence" }] } },
    }));
    expect(await screen.findByText("src/app.py:12 evidence")).toBeInTheDocument();
    expect(screen.getByText("报告结束不代表结论已验证；请核对证据。")).toBeInTheDocument();
  });

  it("refreshes subagent state after stop is rejected and drops stale-session events", async () => {
    const task: SubagentTaskPayload = {
      taskId: "child-1", sessionId: "session-1", purpose: "investigate", prompt: "inspect file",
      status: "running", turns: 1, maxTurns: 12, timeoutSeconds: 180, lastTool: null, output: "", error: null,
    };
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({ subagents: [task] });
      if (method === "session.list" || method === "command.list") return [];
      if (method === "subagent.cancel") throw new Error("stop rejected");
      if (method === "subagent.list") return [task];
      return {};
    });
    renderApp();
    const stop = await screen.findByText("停止子代理");
    fireEvent.click(stop.closest("details")!.querySelector("summary")!);
    fireEvent.click(stop);
    expect(await screen.findByText("stop rejected")).toBeInTheDocument();
    await waitFor(() => expect(requests).toHaveBeenCalledWith("subagent.list"));
    expect(screen.getByRole("button", { name: "停止子代理" })).toBeEnabled();
    act(() => eventListener?.({
      v: 1, type: "event", seq: 5, timestamp: 1, sessionId: "session-2", runId: null,
      event: { type: "session.changed", payload: workspace({ sessionId: "session-2", subagents: [] }) as unknown as Record<string, unknown> },
    }));
    await waitFor(() => expect(screen.queryByText("inspect file", { selector: "summary span" })).not.toBeInTheDocument());
    act(() => eventListener?.({
      v: 1, type: "event", seq: 6, timestamp: 2, sessionId: "session-1", runId: null,
      event: { type: "subagent.stateChanged", payload: { tasks: [task] } },
    }));
    expect(screen.queryByText("inspect file", { selector: "summary span" })).not.toBeInTheDocument();
  });

  it("enters Plan directly from Default without showing drafting actions", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        collaborationMode: "default",
        planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
      });
      if (method === "command.list") return [
        { name: "plan", label: "计划模式", description: "进入 Plan Mode 或显示当前 Plan 操作" },
      ];
      if (method === "mode.enterPlan") return workspace({
        collaborationMode: "plan",
        planState: { phase: "drafting", activePlanId: "new-plan", latestRevision: null, pendingQuestion: null },
      });
      if (method === "session.list") return [];
      return {};
    });
    renderApp();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/plan" } });
    fireEvent.click(await screen.findByRole("button", { name: /计划模式/ }));

    await waitFor(() => expect(requests).toHaveBeenCalledWith("mode.enterPlan"));
    expect(screen.queryByText("整理并提交")).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText("描述你想完成的任务…")).toBeInTheDocument();
  });

  it("shows drafting actions when /plan is run while already in Plan", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        planState: { phase: "drafting", activePlanId: "plan-1", latestRevision: null, pendingQuestion: null },
      });
      if (method === "command.list") return [
        { name: "plan", label: "计划模式", description: "进入 Plan Mode 或显示当前 Plan 操作" },
      ];
      if (method === "session.list") return [];
      return {};
    });
    renderApp();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/plan" } });
    fireEvent.click(await screen.findByRole("button", { name: /计划模式/ }));

    expect(requests).not.toHaveBeenCalledWith("mode.enterPlan");
    expect(await screen.findByTestId("plan-mode-menu")).toHaveTextContent("继续规划");
    expect(screen.getByTestId("plan-mode-menu")).toHaveTextContent("整理并提交");
    expect(screen.getByTestId("plan-mode-menu")).toHaveTextContent("取消规划");
  });

  it("continues drafting from the Plan menu without entering a new episode", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        planState: { phase: "drafting", activePlanId: "plan-1", latestRevision: null, pendingQuestion: null },
      });
      if (method === "command.list") return [
        { name: "plan", label: "计划模式", description: "进入 Plan Mode 或显示当前 Plan 操作" },
      ];
      if (method === "session.list") return [];
      return {};
    });
    renderApp();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/plan" } });
    fireEvent.click(await screen.findByRole("button", { name: /计划模式/ }));
    fireEvent.click(await screen.findByRole("button", { name: /继续规划/ }));

    await waitFor(() => expect(screen.getByPlaceholderText("描述你想完成的任务…")).toHaveFocus());
    expect(screen.queryByTestId("plan-mode-menu")).not.toBeInTheDocument();
    expect(requests).not.toHaveBeenCalledWith("mode.enterPlan");
  });

  it("submits the drafting action through the normal run RPC", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        planState: { phase: "drafting", activePlanId: "plan-1", latestRevision: null, pendingQuestion: null },
      });
      if (method === "command.list") return [
        { name: "plan", label: "计划模式", description: "进入 Plan Mode 或显示当前 Plan 操作" },
      ];
      if (method === "session.list") return [];
      if (method === "run.start") return { accepted: true, runId: "run-1" };
      if (method === "session.snapshot") return workspace();
      return {};
    });
    renderApp();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/plan" } });
    fireEvent.click(await screen.findByRole("button", { name: /计划模式/ }));
    fireEvent.click(await screen.findByRole("button", { name: /整理并提交/ }));

    await waitFor(() => expect(requests).toHaveBeenCalledWith("run.start", {
      text: "请基于当前讨论整理一份决策完备的实施计划，并仅通过 submit_plan 控制工具提交；不要执行计划。",
    }));
    expect(screen.queryByTestId("plan-mode-menu")).not.toBeInTheDocument();
    expect(await screen.findByTestId("plan-submitting")).toHaveTextContent("正在整理并提交计划");

    eventListener?.({
      v: 1, type: "event", seq: 1, timestamp: Date.now(), sessionId: "session-1", runId: "run-1",
      event: { type: "run.started", payload: {} },
    });
    expect(screen.getByTestId("plan-submitting")).toHaveTextContent("正在整理并提交计划");

    eventListener?.({
      v: 1, type: "event", seq: 2, timestamp: Date.now(), sessionId: "session-1", runId: "run-1",
      event: { type: "run.completed", payload: {} },
    });
    await waitFor(() => expect(screen.queryByTestId("plan-submitting")).not.toBeInTheDocument());
  });

  it.each(["run.completed", "run.cancelled", "run.failed"])(
    "restores drafting actions with feedback after %s without a submitted plan",
    async (eventType) => {
      const draft = workspace({
        planState: { phase: "drafting", activePlanId: "plan-1", latestRevision: null, pendingQuestion: null },
      });
      requests.mockImplementation(async (method: string) => {
        if (method === "workspace.open" || method === "session.snapshot") return draft;
        if (method === "command.list") return [{ name: "plan", label: "计划模式" }];
        if (method === "session.list") return [];
        if (method === "run.start") return { accepted: true, runId: "run-1" };
        return {};
      });
      renderApp();
      fireEvent.change(await screen.findByPlaceholderText("描述你想完成的任务…"), { target: { value: "/plan" } });
      fireEvent.click(await screen.findByRole("button", { name: /计划模式/ }));
      fireEvent.click(await screen.findByRole("button", { name: /整理并提交/ }));
      expect(await screen.findByTestId("plan-submitting")).toBeInTheDocument();

      eventListener?.({
        v: 1, type: "event", seq: 1, timestamp: Date.now(), sessionId: "session-1", runId: "run-1",
        event: { type: eventType, payload: { message: "model unavailable" } },
      });

      await waitFor(() => expect(requests).toHaveBeenCalledWith("session.snapshot"));
      expect(await screen.findByTestId("plan-mode-menu")).toHaveTextContent("整理并提交");
      expect(screen.queryByTestId("plan-submitting")).not.toBeInTheDocument();
      if (eventType === "run.failed") {
        expect(screen.getByText("model unavailable")).toBeInTheDocument();
      } else {
        expect(screen.getByTestId("plan-submission-notice")).toHaveTextContent("尚未提交计划");
      }
      expect(requests).not.toHaveBeenCalledWith("plan.execute", expect.anything());
    },
  );

  it("shows progress before the submission RPC returns, and offers retry after rejection", async () => {
    let rejectStart: ((reason: Error) => void) | undefined;
    const start = new Promise((_, reject) => { rejectStart = reject; });
    const draft = workspace({
      planState: { phase: "drafting", activePlanId: "plan-1", latestRevision: null, pendingQuestion: null },
    });
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open" || method === "session.snapshot") return draft;
      if (method === "command.list") return [{ name: "plan", label: "计划模式" }];
      if (method === "session.list") return [];
      if (method === "run.start") return start;
      return {};
    });
    renderApp();
    fireEvent.change(await screen.findByPlaceholderText("描述你想完成的任务…"), { target: { value: "/plan" } });
    fireEvent.click(await screen.findByRole("button", { name: /计划模式/ }));
    fireEvent.click(await screen.findByRole("button", { name: /整理并提交/ }));

    expect(await screen.findByTestId("plan-submitting")).toHaveTextContent("正在整理并提交计划");
    expect(screen.getByRole("button", { name: /停止/ })).toBeEnabled();
    expect(requests.mock.calls.filter(([method]) => method === "run.start")).toHaveLength(1);
    rejectStart?.(new Error("start rejected"));
    expect(await screen.findByText("start rejected")).toBeInTheDocument();
    expect(screen.queryByTestId("plan-submitting")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /整理并提交/ })).toBeEnabled();
  });

  it.each(["ready", "awaiting_answer"] as const)("replaces submission progress with the authoritative %s controls", async (phase) => {
    const draft = workspace({
      planState: { phase: "drafting", activePlanId: "plan-1", latestRevision: null, pendingQuestion: null },
    });
    const finalState = workspace({
      planState: phase === "ready" ? workspace().planState : {
        phase, activePlanId: "plan-1", latestRevision: null,
        pendingQuestion: {
          questionId: "q", header: "范围", question: "选择范围？", allowCustom: true,
          options: [{ label: "核心", description: "只改核心" }, { label: "全部", description: "改双端" }],
        },
      },
    });
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return draft;
      if (method === "session.snapshot") return finalState;
      if (method === "command.list") return [{ name: "plan", label: "计划模式" }];
      if (method === "session.list") return [];
      if (method === "run.start") return { accepted: true, runId: "run-1" };
      return {};
    });
    renderApp();
    fireEvent.change(await screen.findByPlaceholderText("描述你想完成的任务…"), { target: { value: "/plan" } });
    fireEvent.click(await screen.findByRole("button", { name: /计划模式/ }));
    fireEvent.click(await screen.findByRole("button", { name: /整理并提交/ }));
    await screen.findByTestId("plan-submitting");
    eventListener?.({
      v: 1, type: "event", seq: 1, timestamp: Date.now(), sessionId: "session-1", runId: "run-1",
      event: { type: "plan.stateChanged", payload: { sessionId: "session-1", state: finalState.planState } },
    });
    // A pending question takes over even while the same run waits for an answer.
    expect(await screen.findByTestId(phase === "ready" ? "plan-decision" : "plan-question")).toBeInTheDocument();
    expect(screen.queryByTestId("plan-submitting")).not.toBeInTheDocument();
    eventListener?.({
      v: 1, type: "event", seq: 2, timestamp: Date.now(), sessionId: "session-1", runId: "run-1",
      event: { type: "run.completed", payload: {} },
    });
    await waitFor(() => expect(requests).toHaveBeenCalledWith("session.snapshot"));
    expect(screen.queryByTestId("plan-mode-menu")).not.toBeInTheDocument();
    expect(screen.queryByTestId("plan-submission-notice")).not.toBeInTheDocument();
    expect(requests).not.toHaveBeenCalledWith("plan.execute", expect.anything());
  });

  it("does not let a previous run completion clear submission progress", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        planState: { phase: "drafting", activePlanId: "plan-1", latestRevision: null, pendingQuestion: null },
      });
      if (method === "command.list") return [{ name: "plan", label: "计划模式" }];
      if (method === "session.list") return [];
      if (method === "run.start") return { accepted: true, runId: "run-1" };
      return {};
    });
    renderApp();
    fireEvent.change(await screen.findByPlaceholderText("描述你想完成的任务…"), { target: { value: "/plan" } });
    fireEvent.click(await screen.findByRole("button", { name: /计划模式/ }));
    fireEvent.click(await screen.findByRole("button", { name: /整理并提交/ }));
    await screen.findByTestId("plan-submitting");
    eventListener?.({
      v: 1, type: "event", seq: 1, timestamp: Date.now(), sessionId: "session-1", runId: "previous-run",
      event: { type: "run.failed", payload: { message: "stale failure" } },
    });
    // Flush the event's animation-frame batch rather than assert before it runs.
    await act(async () => {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    });
    expect(screen.getByTestId("plan-submitting")).toBeInTheDocument();
    expect(screen.queryByText("stale failure")).not.toBeInTheDocument();
  });

  it("discards a late submission rejection after switching sessions", async () => {
    let rejectStart: ((reason: Error) => void) | undefined;
    const start = new Promise((_, reject) => { rejectStart = reject; });
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        planState: { phase: "drafting", activePlanId: "plan-1", latestRevision: null, pendingQuestion: null },
      });
      if (method === "command.list") return [{ name: "plan", label: "计划模式" }];
      if (method === "session.list") return [];
      if (method === "run.start") return start;
      return {};
    });
    renderApp();
    fireEvent.change(await screen.findByPlaceholderText("描述你想完成的任务…"), { target: { value: "/plan" } });
    fireEvent.click(await screen.findByRole("button", { name: /计划模式/ }));
    fireEvent.click(await screen.findByRole("button", { name: /整理并提交/ }));
    await screen.findByTestId("plan-submitting");
    eventListener?.({
      v: 1, type: "event", seq: 1, timestamp: Date.now(), sessionId: "session-2", runId: null,
      event: { type: "session.changed", payload: workspace({ sessionId: "session-2" }) as unknown as Record<string, unknown> },
    });
    await screen.findByTestId("plan-decision");
    rejectStart?.(new Error("old submission rejected"));
    await waitFor(() => expect(screen.getByRole("button", { name: /执行方案/ })).toBeEnabled());
    expect(screen.queryByText("old submission rejected")).not.toBeInTheDocument();
    expect(screen.queryByTestId("plan-mode-menu")).not.toBeInTheDocument();
    expect(screen.queryByTestId("plan-submitting")).not.toBeInTheDocument();
    expect(requests).not.toHaveBeenCalledWith("session.snapshot");
  });

  it("cancels from the drafting Plan menu through plan.cancel", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        planState: { phase: "drafting", activePlanId: "plan-1", latestRevision: null, pendingQuestion: null },
      });
      if (method === "command.list") return [
        { name: "plan", label: "计划模式", description: "进入 Plan Mode 或显示当前 Plan 操作" },
      ];
      if (method === "session.list") return [];
      if (method === "plan.cancel") return workspace({
        collaborationMode: "default",
        planState: { phase: "cancelled", activePlanId: "plan-1", latestRevision: null, pendingQuestion: null },
      });
      return {};
    });
    renderApp();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/plan" } });
    fireEvent.click(await screen.findByRole("button", { name: /计划模式/ }));
    fireEvent.click(await screen.findByRole("button", { name: /取消规划/ }));

    await waitFor(() => expect(requests).toHaveBeenCalledWith("plan.cancel", { planId: "plan-1" }));
    expect(screen.queryByTestId("plan-mode-menu")).not.toBeInTheDocument();
  });

  it("shows execution details and a stop action for an executing plan", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        collaborationMode: "default",
        planState: {
          phase: "executing", activePlanId: "plan-1", latestRevision: latestPlan, pendingQuestion: null,
          latestRun: {
            planId: "plan-1", revision: 2, digest: latestPlan.digest, status: "started", runId: "run-1",
            error: null, assistantMessageId: null, stopReason: null, entryId: "entry-1", timestamp: "now",
          },
        },
      });
      if (method === "session.list" || method === "command.list") return [];
      return {};
    });
    renderApp();
    const details = await screen.findByTestId("plan-executing");
    expect(details).toHaveTextContent("plan-1");
    eventListener?.({
      v: 1, type: "event", seq: 1, timestamp: Date.now(), sessionId: "session-1", runId: "run-1",
      event: { type: "run.started", payload: {} },
    });
    const stop = await screen.findByRole("button", { name: /停止执行/ });
    await waitFor(() => expect(stop).not.toBeDisabled());
    fireEvent.click(stop);
    expect(requests).toHaveBeenCalledWith("run.abort");
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

  it("reopens ready actions when /plan is run after supplemental editing", async () => {
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace();
      if (method === "command.list") return [
        { name: "plan", label: "计划模式", description: "进入 Plan Mode 或显示当前 Plan 操作" },
      ];
      if (method === "session.list") return [];
      return {};
    });
    renderApp();
    await screen.findByTestId("plan-decision");
    fireEvent.click(screen.getByRole("button", { name: /继续修改/ }));
    const composer = screen.getByPlaceholderText("补充你的想法或修改要求…");
    fireEvent.change(composer, { target: { value: "/plan" } });
    fireEvent.click(await screen.findByRole("button", { name: /计划模式/ }));

    expect(await screen.findByTestId("plan-decision")).toHaveTextContent("执行方案");
    expect(screen.getByTestId("plan-decision")).toHaveTextContent("新会话复核");
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
    const childPlan = { ...latestPlan, title: "Fresh review", originSessionId: "session-1" };
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
          handoffSourceSessionId: "session-1",
          canHandoff: false,
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
    expect(screen.queryByRole("button", { name: /新会话复核/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /执行方案/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /继续修改/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: "取消规划" })).toBeEnabled();
    expect(requests.mock.calls.filter(([method]) => method === "plan.handoff")).toHaveLength(1);
    expect(requests).not.toHaveBeenCalledWith("plan.execute", expect.anything());
  });

  it.each(["origin", "episode"])("restores a %s review without offering another handoff", async (metadata) => {
    const reviewWorkspace = workspace({
      sessionId: "review-session",
      planState: {
        ...workspace().planState,
        latestRevision: { ...latestPlan, originSessionId: metadata === "origin" ? "session-1" : null },
        ...(metadata === "episode" ? { handoffSourceSessionId: "session-1", canHandoff: false } : {}),
      },
    });
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open" || method === "session.snapshot") return reviewWorkspace;
      if (method === "command.list") return [{ name: "plan", label: "计划模式" }];
      if (method === "session.list") return [];
      return {};
    });

    renderApp();
    expect(await screen.findByRole("button", { name: /执行方案/ })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /新会话复核/ })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveAttribute("placeholder", "请先选择执行或继续修改");
    fireEvent.click(screen.getByRole("button", { name: /继续修改/ }));
    expect(screen.getByRole("textbox")).toBeEnabled();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "/plan" } });
    fireEvent.click(await screen.findByRole("button", { name: /计划模式/ }));
    expect(await screen.findByTestId("plan-decision")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /新会话复核/ })).not.toBeInTheDocument();
    expect(requests).not.toHaveBeenCalledWith("plan.handoff", expect.anything());
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

  it("passes the next-phase direction and lets the user stop compaction", async () => {
    let finish: ((value: Record<string, unknown>) => void) | undefined;
    const pending = new Promise<Record<string, unknown>>((resolve) => { finish = resolve; });
    requests.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace({
        collaborationMode: "default",
        planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
      });
      if (method === "command.list") return [{ name: "compact", label: "压缩上下文", description: "定向整理" }];
      if (method === "session.list") return [];
      if (method === "session.compact") return pending;
      if (method === "run.abort") finish?.({ performed: false, error: "Compaction aborted" });
      return {};
    });
    renderApp();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/compact 实现选定方案" } });
    fireEvent.submit(composer.closest("form")!);
    await waitFor(() => expect(requests).toHaveBeenCalledWith("session.compact", { direction: "实现选定方案" }));
    expect(await screen.findByText("正在面向下一阶段整理当前上下文，历史记录仍会保留。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /停止压缩/ }));
    expect(await screen.findByText("上下文压缩已取消")).toBeInTheDocument();
    expect(requests).toHaveBeenCalledWith("run.abort");
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: /停止压缩/ })).toBeEnabled();

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
