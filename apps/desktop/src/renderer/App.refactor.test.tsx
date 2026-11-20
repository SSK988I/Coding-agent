import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent, WorkspacePayload } from "../shared/types";
import { App } from "./App";

const makeWorkspace = (): WorkspacePayload => ({
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

  it("resets MemoryDrawer clear confirmation when switching from Project A to Project B", async () => {
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
    window.desktop.chooseWorkspace = async () => projectB.path;
    request.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "workspace.open") {
        currentWorkspace = params?.path === projectB.path ? projectB : projectA;
        return currentWorkspace;
      }
      if (method === "session.list" || method === "command.list") return [];
      if (method === "memory.status") return currentWorkspace.memory;
      if (method === "memory.list" || method === "memory.conflicts") return [];
      if (method === "memory.clear") return { count: 1, memory: currentWorkspace.memory };
      return {};
    });

    mount();
    fireEvent.click(await screen.findByRole("button", { name: "管理长期记忆" }));
    expect(await screen.findByRole("dialog", { name: "长期记忆管理" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /清空项目记忆/ }));
    expect(screen.getByRole("button", { name: "确认清空项目" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "关闭长期记忆管理" }));
    expect(screen.queryByRole("dialog", { name: "长期记忆管理" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "打开项目" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("workspace.open", { path: projectB.path, resume: true }));

    fireEvent.click(screen.getByRole("button", { name: "管理长期记忆" }));
    expect(await screen.findByRole("dialog", { name: "长期记忆管理" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认清空项目" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /清空项目记忆/ })).toBeInTheDocument();
    expect(request).not.toHaveBeenCalledWith("memory.clear", expect.anything());
  });

  it("preserves unsent draft when opening command palette via / button and dismissing with Escape", async () => {
    request.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return makeWorkspace();
      if (method === "session.list") return [];
      if (method === "command.list") {
        return [
          { name: "help", label: "帮助", description: "显示可用命令" },
          { name: "model", label: "模型", description: "切换当前模型" },
        ];
      }
      return {};
    });

    mount();
    await screen.findByText("Claude Sonnet");
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "保留这段未发送草稿" } });

    fireEvent.click(screen.getByRole("button", { name: "打开命令菜单" }));
    expect(await screen.findByRole("button", { name: /\/help/ })).toBeInTheDocument();
    expect(textarea).toHaveValue("保留这段未发送草稿");

    fireEvent.keyDown(textarea, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("button", { name: /\/help/ })).not.toBeInTheDocument());
    expect(textarea).toHaveValue("保留这段未发送草稿");
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

    // Collapsing the group and receiving an approval re-opens the group cleanly.
    const header = screen.queryByRole("button", { name: /查阅工作区文件/ });
    if (header) {
      fireEvent.click(header);
      expect(header).toHaveAttribute("aria-expanded", "false");
    }
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

    const groupHeader = screen.queryByTestId("tool-group");
    if (groupHeader) {
      expect(groupHeader).toHaveTextContent("等待审批");
    }
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

    // 3. First text delta arrives -> Working disappears.
    await emit("message_update", { kind: "text_delta", delta: "正在检查" }, "run-w1");
    expect(screen.queryByTestId("working-indicator")).not.toBeInTheDocument();
    await emit("message_end", { message: { role: "assistant", content: "正在检查" } }, "run-w1");

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
});
