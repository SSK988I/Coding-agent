import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent, WorkspacePayload } from "../shared/types";
import { App } from "./App";

const makeWorkspace = (overrides: Partial<WorkspacePayload> = {}): WorkspacePayload => ({
  path: "E:\\code\\Coding-agent",
  sessionId: "session-1",
  model: { id: "claude-sonnet", name: "Claude Sonnet", provider: "anthropic", input: ["text", "image"] },
  thinkingLevel: null,
  tools: ["read", "edit", "bash"],
  messages: [],
  collaborationMode: "default",
  planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
  memory: {
    enabled: true,
    autoExtractEnabled: true,
    userId: "local-user",
    projectId: "coding-agent",
    globalCount: 1,
    projectCount: 0,
    conflictCount: 0,
    pendingCount: 0,
    processingCount: 0,
    readyCount: 1,
    failedCount: 0,
  },
  webSearch: {
    enabled: true,
    backend: "zhipu-mcp",
    available: true,
    status: "idle",
    errorCode: null,
  },
  ...overrides,
});

describe("desktop frontend refactor capabilities", () => {
  const request = vi.fn();
  const setTheme = vi.fn();
  let listener: ((event: RuntimeEvent) => void) | undefined;
  let seq = 0;

  const mount = (viewportHeight = 900, itemHeight = 120) => render(
    <VirtuosoMockContext.Provider value={{ viewportHeight, itemHeight }}>
      <App />
    </VirtuosoMockContext.Provider>,
  );

  const emit = async (type: string, payload: Record<string, unknown>, runId: string | null = "run-1") => {
    await act(async () => {
      listener?.({
        v: 1,
        type: "event",
        seq: ++seq,
        timestamp: seq,
        sessionId: "session-1",
        runId,
        event: { type, payload },
      });
      await new Promise(requestAnimationFrame);
    });
  };

  beforeEach(() => {
    seq = 0;
    listener = undefined;
    window.localStorage.removeItem("coding-agent:desktop-theme-v2");
    document.documentElement.dataset.theme = "dark";
    request.mockReset();
    setTheme.mockReset();
    setTheme.mockResolvedValue(undefined);
    request.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "workspace.open") return makeWorkspace();
      if (method === "session.list" || method === "command.list") return [];
      if (method === "webSearch.setEnabled") {
        return { enabled: Boolean(params?.enabled), backend: "zhipu-mcp", available: true, status: params?.enabled ? "idle" : "disabled", errorCode: null };
      }
      if (method === "run.steer" || method === "run.followUp") return { queued: true };
      if (method === "memory.status") return makeWorkspace().memory;
      if (method === "memory.list" || method === "memory.conflicts") return [];
      return {};
    });
    window.agent = {
      request,
      onEvent: (cb) => { listener = cb; return () => undefined; },
      onStatus: () => () => undefined,
    };
    window.desktop = {
      chooseWorkspace: async () => null,
      getBootstrap: async () => ({ defaultWorkspace: "E:\\code\\Coding-agent", platform: "win32" }),
      setTheme,
    };
  });

  afterEach(cleanup);

  it("toggles between dark and light themes and syncs native shell theme", async () => {
    mount();
    const toggle = await screen.findByRole("button", { name: "切换到浅色主题" });
    expect(document.documentElement.dataset.theme).toBe("dark");

    fireEvent.click(toggle);
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(setTheme).toHaveBeenCalledWith("light");
    expect(screen.getByRole("button", { name: "切换到深色主题" })).toBeInTheDocument();
  });

  it("toggles web search via explicit sidebar control", async () => {
    mount();
    const searchToggle = await screen.findByRole("button", { name: /联网.*开/ });
    fireEvent.click(searchToggle);
    await waitFor(() => expect(request).toHaveBeenCalledWith("webSearch.setEnabled", { enabled: false }));
    expect(await screen.findByRole("button", { name: /联网.*关/ })).toBeInTheDocument();
  });

  it("queues steer and follow-up instructions while a run is active", async () => {
    mount();
    await screen.findByText("Claude Sonnet");
    await emit("run.started", {}, "run-42");

    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "先只检查测试文件" } });
    fireEvent.click(screen.getByRole("button", { name: "转向" }));
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(request).toHaveBeenCalledWith("run.steer", {
      text: "先只检查测试文件",
      sessionId: "session-1",
      runId: "run-42",
    }));

    fireEvent.click(screen.getByRole("button", { name: "排队" }));
    fireEvent.change(textarea, { target: { value: "完成后跑一次 build" } });
    fireEvent.click(screen.getByRole("button", { name: "加入队列" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("run.followUp", {
      text: "完成后跑一次 build",
      sessionId: "session-1",
      runId: "run-42",
    }));
  });

  it("opens the long-term memory drawer from the titlebar", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "管理长期记忆" }));
    expect(await screen.findByRole("dialog", { name: "长期记忆管理" })).toBeInTheDocument();
    await waitFor(() => expect(request).toHaveBeenCalledWith("memory.list", {}));
  });

  it("groups consecutive tools into a 2-level tree with collapsed Chinese L2 summaries and auto-opens approvals", async () => {
    mount();
    await screen.findByText("Claude Sonnet");
    await emit("tool_execution_start", {
      tool_call_id: "call-read-1",
      tool_name: "read",
      args: { path: "docs/AGENTS.md" },
    }, "run-1");
    await emit("tool_execution_end", {
      tool_call_id: "call-read-1",
      tool_name: "read",
      result: { content: [{ type: "text", text: "SECRET_FILE_BODY" }] },
      is_error: false,
    }, "run-1");
    await emit("tool_execution_start", {
      tool_call_id: "call-bash-1",
      tool_name: "bash",
      args: { command: "pnpm typecheck" },
    }, "run-1");
    await emit("tool_execution_end", {
      tool_call_id: "call-bash-1",
      tool_name: "bash",
      result: { content: [{ type: "text", text: "0 errors" }] },
      is_error: false,
    }, "run-1");

    const group = await screen.findByTestId("tool-group");
    expect(group).toHaveTextContent("检查工作区并运行命令");
    expect(group).not.toHaveTextContent("已完成");

    const readToggle = screen.getByRole("button", { name: "读取 docs/AGENTS.md" });
    expect(readToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("SECRET_FILE_BODY")).not.toBeInTheDocument();

    const bashToggle = screen.getByRole("button", { name: "pnpm typecheck" });
    expect(bashToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("0 errors")).not.toBeInTheDocument();

    fireEvent.click(readToggle);
    expect(readToggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("SECRET_FILE_BODY")).toBeInTheDocument();

    // Pending approval automatically expands the child tool inside the L1 group.
    await emit("tool_execution_start", {
      tool_call_id: "call-edit-1",
      tool_name: "edit",
      args: { path: "src/App.tsx", edits: [{ oldText: "a", newText: "b" }] },
    }, "run-1");
    await emit("approval.requested", {
      approvalId: "appr-1",
      toolCallId: "call-edit-1",
      toolName: "edit",
      args: { path: "src/App.tsx" },
    }, "run-1");

    expect(await screen.findByRole("button", { name: "允许一次" })).toBeInTheDocument();
    expect(screen.getByTestId("tool-group")).toHaveTextContent("检查工作区并应用修改");
  });

  it("locks and closes MemoryDrawer immediately during a delayed Project A to Project B switch", async () => {
    const projectA: WorkspacePayload = {
      ...makeWorkspace(),
      path: "E:\\code\\project-a",
      sessionId: "session-a",
      memory: { ...makeWorkspace().memory, projectId: "project-a", projectCount: 2 },
    };
    const projectB: WorkspacePayload = {
      ...makeWorkspace(),
      path: "E:\\code\\project-b",
      sessionId: "session-b",
      memory: { ...makeWorkspace().memory, projectId: "project-b", projectCount: 1 },
    };

    let currentWorkspace = projectA;
    let resolveProjectBOpen!: (value: WorkspacePayload) => void;
    window.desktop.chooseWorkspace = async () => projectB.path;
    request.mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "workspace.open") {
        if (params?.path === projectB.path) {
          return new Promise<WorkspacePayload>((resolve) => {
            resolveProjectBOpen = (payload) => {
              currentWorkspace = payload;
              resolve(payload);
            };
          });
        }
        currentWorkspace = projectA;
        return Promise.resolve(projectA);
      }
      if (method === "session.list" || method === "command.list") return Promise.resolve([]);
      if (method === "memory.status") return Promise.resolve(currentWorkspace.memory);
      if (method === "memory.list" || method === "memory.conflicts") return Promise.resolve([]);
      if (method === "memory.clear") return Promise.resolve({ count: 1, memory: currentWorkspace.memory });
      return Promise.resolve({});
    });

    mount();
    fireEvent.click(await screen.findByRole("button", { name: "管理长期记忆" }));
    expect(await screen.findByRole("dialog", { name: "长期记忆管理" })).toBeInTheDocument();

    // Start clear confirmation in Project A while drawer stays open.
    fireEvent.click(screen.getByRole("button", { name: /清空项目记忆/ }));
    expect(screen.getByRole("button", { name: "确认清空项目" })).toBeInTheDocument();

    // Initiate workspace switch to Project B while workspace.open(B) is still pending.
    fireEvent.click(screen.getByRole("button", { name: "打开项目" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("workspace.open", { path: projectB.path, resume: true }));

    // Drawer and confirmation must vanish immediately while B is opening, and reopening must be locked.
    expect(screen.queryByRole("dialog", { name: "长期记忆管理" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认清空项目" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "管理长期记忆" })).toBeDisabled();
    expect(request).not.toHaveBeenCalledWith("memory.clear", expect.anything());

    // Finish opening Project B and verify reopening the drawer starts cleanly with no pending confirmation.
    await act(async () => {
      resolveProjectBOpen(projectB);
      await Promise.resolve();
    });
    const memoryBtn = await screen.findByRole("button", { name: "管理长期记忆" });
    await waitFor(() => expect(memoryBtn).toBeEnabled());
    fireEvent.click(memoryBtn);
    expect(await screen.findByRole("dialog", { name: "长期记忆管理" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认清空项目" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /清空项目记忆/ })).toBeInTheDocument();
    expect(request).not.toHaveBeenCalledWith("memory.clear", expect.anything());
  });

  it("keeps toolbar / command filtering separate from chat draft, executes on Enter, and preserves draft on Escape", async () => {
    request.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return makeWorkspace();
      if (method === "session.list") return [];
      if (method === "command.list") {
        return [
          { name: "help", label: "帮助", description: "显示可用命令" },
          { name: "model", label: "模型", description: "切换当前模型" },
          { name: "memory", label: "长期记忆", description: "查看记忆" },
        ];
      }
      if (method === "model.list") {
        return [{ id: "claude-sonnet", name: "Claude Sonnet", provider: "anthropic", current: true }];
      }
      return {};
    });

    mount();
    await screen.findByText("Claude Sonnet");
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "保留这段未发送草稿" } });

    // 1. Click / button -> separate search input filters commands without touching the chat draft.
    fireEvent.click(screen.getByRole("button", { name: "打开命令菜单" }));
    const paletteSearch = await screen.findByRole("searchbox", { name: "筛选命令" });
    expect(paletteSearch).toHaveFocus();
    expect(textarea).toHaveValue("保留这段未发送草稿");

    fireEvent.change(paletteSearch, { target: { value: "m" } });
    expect(screen.getByRole("button", { name: /\/model/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /\/help/ })).not.toBeInTheDocument();
    expect(textarea).toHaveValue("保留这段未发送草稿");

    // 2. Pressing Enter executes the filtered command instead of sending "m" as chat text.
    fireEvent.keyDown(paletteSearch, { key: "Enter" });
    await waitFor(() => expect(request).toHaveBeenCalledWith("model.list"));
    expect(request).not.toHaveBeenCalledWith("run.start", expect.anything());
    expect(textarea).toHaveValue("保留这段未发送草稿");

    // 3. Reopen / palette, filter, and press Escape -> menu closes and draft remains intact.
    fireEvent.click(screen.getByRole("button", { name: "打开命令菜单" }));
    const reopenSearch = await screen.findByRole("searchbox", { name: "筛选命令" });
    fireEvent.change(reopenSearch, { target: { value: "h" } });
    fireEvent.keyDown(reopenSearch, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("searchbox", { name: "筛选命令" })).not.toBeInTheDocument());
    expect(request).not.toHaveBeenCalledWith("run.start", expect.anything());
    expect(textarea).toHaveValue("保留这段未发送草稿");
  });

  it("ignores Enter while IME composition is active for normal send, running steer/followUp and command palette", async () => {
    request.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return makeWorkspace();
      if (method === "session.list") return [];
      if (method === "command.list") {
        return [{ name: "model", label: "模型", description: "切换模型" }];
      }
      if (method === "run.start") return { runId: "run-1" };
      if (method === "run.steer" || method === "run.followUp") return { queued: true };
      return {};
    });

    mount();
    await screen.findByText("Claude Sonnet");
    const textarea = screen.getByRole("textbox");

    // Idle IME Enter must not call run.start.
    fireEvent.change(textarea, { target: { value: "中文输入法草稿" } });
    fireEvent.keyDown(textarea, { key: "Enter", isComposing: true });
    expect(request).not.toHaveBeenCalledWith("run.start", expect.anything());
    expect(textarea).toHaveValue("中文输入法草稿");

    // Command palette IME Enter must not execute a slash command.
    fireEvent.click(screen.getByRole("button", { name: "打开命令菜单" }));
    const search = await screen.findByRole("searchbox", { name: "筛选命令" });
    fireEvent.keyDown(search, { key: "Enter", isComposing: true });
    expect(request).not.toHaveBeenCalledWith("model.list");
    fireEvent.keyDown(search, { key: "Escape" });

    // Running IME Enter must not send run.steer or run.followUp.
    await emit("run.started", {}, "run-ime-1");
    fireEvent.click(screen.getByRole("button", { name: "转向" }));
    fireEvent.change(textarea, { target: { value: "正在选候选词" } });
    fireEvent.keyDown(textarea, { key: "Enter", isComposing: true });
    expect(request).not.toHaveBeenCalledWith("run.steer", expect.anything());
    expect(request).not.toHaveBeenCalledWith("run.followUp", expect.anything());
    expect(textarea).toHaveValue("正在选候选词");
  });

  it("prevents duplicate queued RPCs on rapid Enter or button clicks and keeps draft on error", async () => {
    let rejectFirst!: (reason: Error) => void;
    let resolveSecond!: (value: unknown) => void;
    let followUpCalls = 0;

    request.mockImplementation((method: string) => {
      if (method === "workspace.open") return Promise.resolve(makeWorkspace());
      if (method === "session.list" || method === "command.list") return Promise.resolve([]);
      if (method === "run.followUp") {
        followUpCalls += 1;
        if (followUpCalls === 1) {
          return new Promise((_resolve, reject) => { rejectFirst = reject; });
        }
        return new Promise((resolve) => { resolveSecond = resolve; });
      }
      return Promise.resolve({});
    });

    mount();
    await screen.findByText("Claude Sonnet");
    await emit("run.started", {}, "run-99");

    fireEvent.click(screen.getByRole("button", { name: "排队" }));
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "运行结束后跑单测" } });

    // Rapid double Enter before the first RPC settles must only send one request.
    fireEvent.keyDown(textarea, { key: "Enter" });
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(followUpCalls).toBe(1);
    expect(screen.getByRole("button", { name: "发送中…" })).toBeDisabled();

    // Rejection must release the guard, display the error, and keep the draft for retry.
    await act(async () => {
      rejectFirst(new Error("queue temporarily unavailable"));
      await Promise.resolve();
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("queue temporarily unavailable");
    expect(textarea).toHaveValue("运行结束后跑单测");

    // Rapid double button click must also only send one retry request.
    const queueBtn = screen.getByRole("button", { name: "加入队列" });
    expect(queueBtn).toBeEnabled();
    fireEvent.click(queueBtn);
    fireEvent.click(queueBtn);
    expect(followUpCalls).toBe(2);

    await act(async () => {
      resolveSecond({ queued: true });
      await Promise.resolve();
    });
    await waitFor(() => expect(textarea).toHaveValue(""));
  });

  it("hides steer/followUp delivery controls while submitting a plan", async () => {
    const draftingWorkspace: WorkspacePayload = {
      ...makeWorkspace(),
      collaborationMode: "plan",
      planState: { phase: "drafting", activePlanId: "plan-1", latestRevision: null, pendingQuestion: null },
    };
    let resolveRunStart!: (value: unknown) => void;
    request.mockImplementation((method: string) => {
      if (method === "workspace.open") return Promise.resolve(draftingWorkspace);
      if (method === "session.list") return Promise.resolve([]);
      if (method === "command.list") {
        return Promise.resolve([{ name: "plan", label: "计划模式", description: "Plan 操作" }]);
      }
      if (method === "run.start") {
        return new Promise((resolve) => { resolveRunStart = resolve; });
      }
      return Promise.resolve({});
    });

    mount();
    const textarea = await screen.findByRole("textbox");
    fireEvent.change(textarea, { target: { value: "/plan" } });
    fireEvent.click(await screen.findByRole("button", { name: /计划模式/ }));
    fireEvent.click(await screen.findByRole("button", { name: /整理并提交/ }));

    expect(await screen.findByTestId("plan-submitting")).toHaveTextContent("正在整理并提交计划");
    expect(screen.getByRole("button", { name: "■ 停止" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "转向" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "排队" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "发送转向" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "加入队列" })).not.toBeInTheDocument();
    expect(textarea).toBeDisabled();

    await act(async () => {
      resolveRunStart({ runId: "run-plan-1" });
      await Promise.resolve();
    });
    await emit("run.started", {}, "run-plan-1");
    expect(screen.queryByRole("button", { name: "转向" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "排队" })).not.toBeInTheDocument();
    expect(textarea).toBeDisabled();
    expect(request).not.toHaveBeenCalledWith("run.steer", expect.anything());
    expect(request).not.toHaveBeenCalledWith("run.followUp", expect.anything());
  });

  it("auto-expands a collapsed tool group on first failure and keeps failure visible when collapsed again", async () => {
    mount();
    await screen.findByText("Claude Sonnet");

    await emit("tool_execution_start", {
      tool_call_id: "call-bash-fail",
      tool_name: "bash",
      args: { command: "pnpm test" },
    }, "run-1");

    const groupToggle = await screen.findByRole("button", { name: /运行工作区命令/ });
    expect(groupToggle).toHaveAttribute("aria-expanded", "true");
    expect(groupToggle).toHaveTextContent("执行中");

    // Collapse the running group.
    fireEvent.click(groupToggle);
    expect(groupToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: /pnpm test/ })).not.toBeInTheDocument();

    // Tool fails while the group is collapsed -> auto-expands and marks failure.
    await emit("tool_execution_end", {
      tool_call_id: "call-bash-fail",
      tool_name: "bash",
      result: { content: [{ type: "text", text: "FAIL: assertion mismatch" }] },
      is_error: true,
    }, "run-1");

    await waitFor(() => expect(screen.getByRole("button", { name: /运行工作区命令/ })).toHaveAttribute("aria-expanded", "true"));
    expect(screen.getByTestId("tool-group")).toHaveTextContent("失败");
    expect(screen.getByText("FAIL: assertion mismatch")).toBeInTheDocument();

    // Manually collapsing afterwards keeps it collapsed while retaining the failure indicator in the header.
    fireEvent.click(screen.getByRole("button", { name: /运行工作区命令/ }));
    expect(screen.getByRole("button", { name: /运行工作区命令/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByTestId("tool-group")).toHaveTextContent("失败");
  });

  it("virtualizes 200 sequential tool calls at item level in Virtuoso while keeping approvals reachable", async () => {
    mount(400, 40);
    await screen.findByText("Claude Sonnet");

    await act(async () => {
      for (let index = 0; index < 200; index += 1) {
        const callId = `call-read-${index}`;
        listener?.({
          v: 1,
          type: "event",
          seq: ++seq,
          timestamp: seq,
          sessionId: "session-1",
          runId: "run-1",
          event: {
            type: "tool_execution_start",
            payload: { tool_call_id: callId, tool_name: "read", args: { path: `src/file-${index}.ts` } },
          },
        });
        listener?.({
          v: 1,
          type: "event",
          seq: ++seq,
          timestamp: seq,
          sessionId: "session-1",
          runId: "run-1",
          event: {
            type: "tool_execution_end",
            payload: {
              tool_call_id: callId,
              tool_name: "read",
              result: { content: [{ type: "text", text: `body-${index}` }] },
              is_error: false,
            },
          },
        });
      }
      await new Promise(requestAnimationFrame);
    });

    await waitFor(() => {
      const renderedReadCards = document.querySelectorAll('[data-tool-name="read"]').length;
      expect(renderedReadCards).toBeGreaterThan(0);
      expect(renderedReadCards).toBeLessThan(100);
    });

    // Collapse the group while at top of viewport, then emit a trailing approval tool.
    const header = screen.getByRole("button", { name: /查阅工作区文件/ });
    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "false");

    await emit("tool_execution_start", {
      tool_call_id: "call-edit-approval",
      tool_name: "edit",
      args: { path: "src/target.ts", edits: [{ oldText: "a", newText: "b" }] },
    }, "run-1");
    await emit("approval.requested", {
      approvalId: "appr-200",
      toolCallId: "call-edit-approval",
      toolName: "edit",
      args: { path: "src/target.ts" },
    }, "run-1");

    const updatedHeader = screen.getByTestId("tool-group");
    expect(updatedHeader).toHaveTextContent("等待审批");
    expect(screen.getByRole("button", { name: /检查工作区并应用修改/ })).toHaveAttribute("aria-expanded", "true");

    // Scroll the virtualized timeline to the trailing approval item and resolve it.
    const scroller = document.querySelector(".timeline") as HTMLElement;
    expect(scroller).not.toBeNull();
    const totalHeight = 203 * 40;
    Object.defineProperty(scroller, "offsetHeight", { configurable: true, value: 400 });
    Object.defineProperty(scroller, "scrollHeight", { configurable: true, value: totalHeight });
    Object.defineProperty(scroller, "scrollTop", { configurable: true, writable: true, value: totalHeight - 400 });
    fireEvent.scroll(scroller);

    const allowButton = await screen.findByRole("button", { name: "允许一次" });
    fireEvent.click(allowButton);
    await waitFor(() => expect(request).toHaveBeenCalledWith("approval.resolve", {
      approvalId: "appr-200",
      approved: true,
    }));
  });

  it("displays an error alert when clicking the sidebar model button fails to load models", async () => {
    request.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return makeWorkspace();
      if (method === "session.list" || method === "command.list") return [];
      if (method === "model.list") throw new Error("runtime disconnected");
      return {};
    });

    mount();
    const modelButton = await screen.findByTitle("点击切换模型");
    fireEvent.click(modelButton);

    expect(await screen.findByRole("alert")).toHaveTextContent("runtime disconnected");
    expect(screen.queryByRole("searchbox", { name: /筛选模型/ })).not.toBeInTheDocument();
  });

  it("shows a single replacing floating toast instead of polluting the chat timeline when toggling memory", async () => {
    let memState = { ...makeWorkspace().memory };
    request.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "workspace.open") return { ...makeWorkspace(), memory: memState };
      if (method === "session.list" || method === "command.list") return [];
      if (method === "memory.setEnabled") {
        memState = { ...memState, enabled: Boolean(params?.enabled) };
        return memState;
      }
      if (method === "memory.setAutoExtract") {
        memState = { ...memState, autoExtractEnabled: Boolean(params?.enabled) };
        return memState;
      }
      return {};
    });

    mount();
    fireEvent.click(await screen.findByRole("button", { name: "已开启" }));
    expect(await screen.findByTestId("floating-toast")).toHaveTextContent("长期记忆已关闭。");
    expect(screen.queryByRole("button", { name: "复制" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "已关闭" }));
    await waitFor(() => expect(screen.getByTestId("floating-toast")).toHaveTextContent("长期记忆已开启。"));

    fireEvent.click(screen.getByRole("button", { name: "自动提取开启" }));
    await waitFor(() => expect(screen.getByTestId("floating-toast")).toHaveTextContent("长期记忆自动提取已关闭。"));

    // Only one floating toast exists; previous toggle statuses are replaced rather than stacked in the timeline.
    expect(screen.getAllByTestId("floating-toast")).toHaveLength(1);
    expect(screen.queryByText("长期记忆已关闭。")).not.toBeInTheDocument();
    expect(screen.queryByText("长期记忆已开启。")).not.toBeInTheDocument();
    expect(document.querySelectorAll(".message.assistant")).toHaveLength(0);
  });

  it("shows a Working shimmer before first token and during inter-tool gaps, hiding when streaming, executing or awaiting approval", async () => {
    mount();
    await screen.findByText("Claude Sonnet");
    expect(screen.queryByTestId("working-indicator")).not.toBeInTheDocument();

    // 1. Run starts -> Working shimmer appears immediately before first assistant token.
    await emit("run.started", {}, "run-w1");
    expect(await screen.findByTestId("working-indicator")).toHaveTextContent("Working");

    // 2. Empty assistant message_start keeps a single Working shimmer without duplicating.
    await emit("message_start", { message: { role: "assistant", content: "" } }, "run-w1");
    expect(screen.getAllByTestId("working-indicator")).toHaveLength(1);

    // 3. First text delta arrives -> Working disappears, and message_end does NOT flash Working before run.completed.
    await emit("message_update", { kind: "text_delta", delta: "正在检查" }, "run-w1");
    expect(screen.queryByTestId("working-indicator")).not.toBeInTheDocument();
    await emit("message_end", { message: { role: "assistant", content: "正在检查", stop_reason: "end_turn" } }, "run-w1");
    expect(screen.queryByTestId("working-indicator")).not.toBeInTheDocument();

    // 4. Active tool hides Working; once tool completes while run continues, Working returns for the inter-step gap.
    await emit("tool_execution_start", {
      tool_call_id: "call-read-w1",
      tool_name: "read",
      args: { path: "README.md" },
    }, "run-w1");
    expect(screen.queryByTestId("working-indicator")).not.toBeInTheDocument();

    await emit("tool_execution_end", {
      tool_call_id: "call-read-w1",
      tool_name: "read",
      result: { content: [{ type: "text", text: "ok" }] },
      is_error: false,
    }, "run-w1");
    expect(await screen.findByTestId("working-indicator")).toHaveTextContent("Working");

    // 5. Pending approval hides Working.
    await emit("tool_execution_start", {
      tool_call_id: "call-edit-w1",
      tool_name: "edit",
      args: { path: "src/index.ts", edits: [{ oldText: "a", newText: "b" }] },
    }, "run-w1");
    await emit("approval.requested", {
      approvalId: "appr-w1",
      toolCallId: "call-edit-w1",
      toolName: "edit",
      args: { path: "src/index.ts" },
    }, "run-w1");
    expect(screen.queryByTestId("working-indicator")).not.toBeInTheDocument();

    // 6. Run completion hides Working cleanly.
    await emit("run.completed", {}, "run-w1");
    expect(screen.queryByTestId("working-indicator")).not.toBeInTheDocument();
  });

  it("clears empty Working placeholder when a run fails or is cancelled without message_end", async () => {
    mount();
    await screen.findByText("Claude Sonnet");

    await emit("run.started", {}, "run-fail-1");
    await emit("message_start", { message: { role: "assistant", content: "" } }, "run-fail-1");
    expect(screen.getByTestId("working-indicator")).toHaveTextContent("Working");

    await emit("run.failed", { message: "upstream timeout" }, "run-fail-1");
    expect(screen.queryByTestId("working-indicator")).not.toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("upstream timeout");

    // Also verify partial text survives cancellation without remaining stuck in streaming state.
    await emit("run.started", {}, "run-cancel-1");
    await emit("message_start", { message: { role: "assistant", content: "" } }, "run-cancel-1");
    await emit("message_update", { kind: "text_delta", delta: "半截回答" }, "run-cancel-1");
    await emit("run.cancelled", {}, "run-cancel-1");
    expect(screen.queryByTestId("working-indicator")).not.toBeInTheDocument();
    expect(screen.getByText("半截回答")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制回复" })).toBeInTheDocument();
  });

  it("locks memory writes after workspace.open fails with drifted backend scope until re-synced", async () => {
    const projectA: WorkspacePayload = {
      ...makeWorkspace(),
      path: "E:\\code\\project-a",
      sessionId: "session-a",
      memory: { ...makeWorkspace().memory, projectId: "project-a", projectCount: 1 },
    };
    const projectB: WorkspacePayload = {
      ...makeWorkspace(),
      path: "E:\\code\\project-b",
      sessionId: "session-b",
      memory: { ...makeWorkspace().memory, projectId: "project-b", projectCount: 1 },
    };

    let openBAttempts = 0;
    let backendScope = { workspacePath: projectA.path, sessionId: projectA.sessionId };
    window.desktop.chooseWorkspace = async () => projectB.path;
    request.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "workspace.open") {
        if (params?.path === projectB.path) {
          openBAttempts += 1;
          backendScope = { workspacePath: projectB.path, sessionId: projectB.sessionId };
          if (openBAttempts === 1) {
            throw new Error("memory snapshot failed");
          }
          return projectB;
        }
        return projectA;
      }
      if (method === "workbench.scope") return backendScope;
      if (method === "session.list" || method === "command.list") return [];
      if (method === "memory.status") return projectB.memory;
      if (method === "memory.list" || method === "memory.conflicts") return [];
      if (method === "memory.clear") return { count: 1, memory: projectB.memory };
      return {};
    });

    mount();
    await screen.findByText("Claude Sonnet");

    // Attempt to open Project B -> backend switches to B but snapshot throws.
    fireEvent.click(screen.getByRole("button", { name: "打开项目" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/已切换到 E:\\code\\project-b.*已锁定记忆写入/);

    // Memory drawer and toggles remain locked so Project A UI cannot clear Project B's memories.
    expect(screen.getByRole("button", { name: "管理长期记忆" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "已开启" })).toBeDisabled();
    expect(request).not.toHaveBeenCalledWith("memory.clear", expect.anything());

    // Re-opening Project B succeeds -> lock clears and subsequent clear carries Project B's scope.
    fireEvent.click(screen.getByRole("button", { name: "打开项目" }));
    const memoryBtn = await screen.findByRole("button", { name: "管理长期记忆" });
    await waitFor(() => expect(memoryBtn).toBeEnabled());
    fireEvent.click(memoryBtn);
    await screen.findByRole("dialog", { name: "长期记忆管理" });
    fireEvent.click(screen.getByRole("button", { name: /清空项目记忆/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认清空项目" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("memory.clear", {
      allScopes: false,
      confirmed: true,
      workspacePath: projectB.path,
      sessionId: projectB.sessionId,
    }));
  });

  it("keeps Shift+Tab/Tab navigation inert in toolbar command search and preserves /-prefixed chat drafts", async () => {
    request.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return makeWorkspace();
      if (method === "session.list") return [];
      if (method === "command.list") {
        return [
          { name: "clear", label: "清空会话", description: "重置当前对话" },
          { name: "help", label: "帮助", description: "显示可用命令" },
        ];
      }
      return {};
    });

    mount();
    await screen.findByText("Claude Sonnet");
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "/memory remember 以后用 pnpm --project" } });

    // Open toolbar command palette and filter for "clear", then press Shift+Tab and Tab -> must NOT execute session.clear!
    fireEvent.click(screen.getByRole("button", { name: "打开命令菜单" }));
    const search = await screen.findByRole("searchbox", { name: "筛选命令" });
    fireEvent.change(search, { target: { value: "clear" } });
    fireEvent.keyDown(search, { key: "Tab", shiftKey: true });
    fireEvent.keyDown(search, { key: "Tab" });
    expect(request).not.toHaveBeenCalledWith("session.clear");

    // Execute /help from toolbar palette -> must NOT erase the /-prefixed draft in composer!
    fireEvent.change(search, { target: { value: "help" } });
    fireEvent.keyDown(search, { key: "Enter" });
    expect(await screen.findByText("可用命令")).toBeInTheDocument();
    expect(textarea).toHaveValue("/memory remember 以后用 pnpm --project");
  });

  it("renders delivered steer/follow-up user messages live between assistant turns without duplicating the initial prompt", async () => {
    mount();
    await screen.findByText("Claude Sonnet");

    await emit("run.started", { text: "原提问" }, "run-live-1");
    await emit("message_start", { message: { role: "user", content: "原提问" } }, "run-live-1");
    await emit("message_end", { message: { role: "user", content: "原提问" } }, "run-live-1");
    await emit("message_start", { message: { role: "assistant", content: "" } }, "run-live-1");
    await emit("message_end", { message: { role: "assistant", content: "回复一", stop_reason: "end_turn" } }, "run-live-1");
    await emit("message_start", { message: { role: "user", content: "补充转向指令" } }, "run-live-1");
    await emit("message_end", { message: { role: "user", content: "补充转向指令" } }, "run-live-1");
    await emit("message_start", { message: { role: "assistant", content: "" } }, "run-live-1");
    await emit("message_end", { message: { role: "assistant", content: "回复二", stop_reason: "end_turn" } }, "run-live-1");
    await emit("run.completed", {}, "run-live-1");

    expect(screen.getAllByText("原提问")).toHaveLength(1);
    const messages = Array.from(document.querySelectorAll(".message .message-body")).map((node) => node.textContent?.trim());
    expect(messages).toEqual(["原提问", "回复一复制", "补充转向指令", "回复二复制"]);
  });

  it("does not execute /clear on Enter or Tab after focus returns from detached toolbar palette to chat composer", async () => {
    request.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return makeWorkspace();
      if (method === "session.list") return [];
      if (method === "command.list") {
        return [
          { name: "clear", label: "清空会话", description: "重置当前对话" },
          { name: "help", label: "帮助", description: "显示可用命令" },
        ];
      }
      if (method === "run.start") return { accepted: true, runId: "run-draft-1" };
      if (method === "session.clear") return makeWorkspace({ sessionId: "session-cleared" });
      return {};
    });

    mount();
    await screen.findByText("Claude Sonnet");
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "写好的普通草稿" } });

    // Open detached toolbar palette and filter for "clear".
    fireEvent.click(screen.getByRole("button", { name: "打开命令菜单" }));
    const paletteSearch = await screen.findByRole("searchbox", { name: "筛选命令" });
    expect(paletteSearch).toHaveFocus();
    fireEvent.change(paletteSearch, { target: { value: "clear" } });

    // Even before focus moves, textarea keyDown in detached palette mode must not trigger session.clear.
    fireEvent.keyDown(textarea, { key: "Tab" });
    expect(request).not.toHaveBeenCalledWith("session.clear");

    // Move focus genuinely back from palette searchbox to chat textarea -> detached palette closes.
    textarea.focus();
    fireEvent.focus(textarea);
    expect(textarea).toHaveFocus();
    await waitFor(() => expect(screen.queryByRole("searchbox", { name: "筛选命令" })).not.toBeInTheDocument());

    // Tab must not call session.clear; Enter must submit the draft via run.start instead of session.clear.
    fireEvent.keyDown(textarea, { key: "Tab" });
    expect(request).not.toHaveBeenCalledWith("session.clear");

    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(request).toHaveBeenCalledWith("run.start", {
      text: "写好的普通草稿",
    }));
    expect(request).not.toHaveBeenCalledWith("session.clear");
  });

  it("keeps slash-prefixed steer messages visible both live and after reopening the session", async () => {
    const persistedMessages = [
      { role: "user", content: "先看项目结构", source: "prompt", timestamp: 1 },
      { role: "assistant", content: "正在查看", stop_reason: "end_turn", timestamp: 2 },
      { role: "user", content: "/src/main.py 是要改的文件", source: "steer", timestamp: 3 },
      { role: "assistant", content: "已修改 /src/main.py", stop_reason: "end_turn", timestamp: 4 },
    ];
    request.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "workspace.open") return makeWorkspace();
      if (method === "command.list") return [];
      if (method === "session.list") {
        return [
          {
            id: "session-1",
            name: null,
            first_message: "先看项目结构",
            message_count: 4,
            modified: "2026-11-22T00:00:00Z",
            archived: false,
            current: true,
          },
          {
            id: "session-2",
            name: null,
            first_message: "另一个问题",
            message_count: 1,
            modified: "2026-11-21T00:00:00Z",
            archived: false,
            current: false,
          },
        ];
      }
      if (method === "run.steer") return { queued: true };
      if (method === "session.open") {
        if (params?.sessionId === "session-2") {
          return makeWorkspace({
            sessionId: "session-2",
            messages: [{ role: "user", content: "另一个问题", source: "prompt", timestamp: 1 }],
          });
        }
        return makeWorkspace({
          sessionId: "session-1",
          messages: persistedMessages,
        });
      }
      return {};
    });

    mount();
    await screen.findByText("Claude Sonnet");

    // Live run with a /-prefixed steer message.
    await emit("run.started", { text: "先看项目结构" }, "run-slash-1");
    await emit("message_end", { message: { role: "user", content: "先看项目结构", source: "prompt" } }, "run-slash-1");
    await emit("message_start", { message: { role: "assistant", content: "" } }, "run-slash-1");
    await emit("message_end", { message: { role: "assistant", content: "正在查看", stop_reason: "end_turn" } }, "run-slash-1");

    const textarea = screen.getByRole("textbox");
    fireEvent.click(screen.getByRole("button", { name: "转向" }));
    fireEvent.change(textarea, { target: { value: "/src/main.py 是要改的文件" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(request).toHaveBeenCalledWith("run.steer", {
      text: "/src/main.py 是要改的文件",
      sessionId: "session-1",
      runId: "run-slash-1",
    }));

    await emit("message_end", { message: { role: "user", content: "/src/main.py 是要改的文件", source: "steer" } }, "run-slash-1");
    await emit("message_start", { message: { role: "assistant", content: "" } }, "run-slash-1");
    await emit("message_end", { message: { role: "assistant", content: "已修改 /src/main.py", stop_reason: "end_turn" } }, "run-slash-1");
    await emit("run.completed", {}, "run-slash-1");

    expect(screen.getByText("/src/main.py 是要改的文件")).toBeInTheDocument();

    // Switch away to session-2 and reopen session-1 -> /-prefixed steer message must remain visible.
    fireEvent.click(await screen.findByRole("button", { name: /另一个问题/ }));
    await screen.findByText("另一个问题");
    fireEvent.click(await screen.findByRole("button", { name: /先看项目结构/ }));
    expect(await screen.findByText("/src/main.py 是要改的文件")).toBeInTheDocument();
    expect(screen.getByText("已修改 /src/main.py")).toBeInTheDocument();
  });
});
