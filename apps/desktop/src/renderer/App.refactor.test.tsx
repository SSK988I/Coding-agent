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

  const mount = () => render(
    <VirtuosoMockContext.Provider value={{ viewportHeight: 900, itemHeight: 120 }}>
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
});
