import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent, WebSearchStatePayload, WorkspacePayload } from "../shared/types";
import { App } from "./App";

const searchState = (enabled = true): WebSearchStatePayload => ({
  enabled, backend: "zhipu-mcp", available: true,
  status: enabled ? "idle" : "disabled", errorCode: null,
});
const baseWorkspace = (): WorkspacePayload => ({
  path: "G:\\Coding-agent", sessionId: "session-1",
  model: { id: "deepseek-flash", name: "Flash", provider: "deepseek", input: ["text", "image"] },
  thinkingLevel: null, tools: [], messages: [], collaborationMode: "default",
  planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
  memory: { enabled: false, userId: null, projectId: null }, webSearch: searchState(),
});

describe("compact desktop image attachments", () => {
  const request = vi.fn();
  let current: WorkspacePayload;
  let listener: ((event: RuntimeEvent) => void) | undefined;
  let sequence: number;
  const mount = () => render(<VirtuosoMockContext.Provider value={{ viewportHeight: 900, itemHeight: 120 }}>
    <App />
  </VirtuosoMockContext.Provider>);
  const selectImage = async () => {
    await screen.findByRole("button", { name: "＋ 添加文件" });
    await waitFor(() => expect(screen.getByLabelText("选择文件")).toBeEnabled());
    const file = new File([new Uint8Array([137, 80, 78, 71])], "screen.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("选择文件"), { target: { files: [file] } });
    await screen.findByRole("img", { name: "screen.png" });
    await waitFor(() => expect(screen.getByRole("button", { name: "发送 ↑" })).toBeEnabled());
  };
  const expectCompact = () => {
    expect(screen.getByRole("img", { name: "screen.png" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "移除图片 screen.png" })).toBeInTheDocument();
    expect(screen.getByText(/支持文件与图片 · 最多 8 个/)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "联网核实角色/地点" })).not.toBeInTheDocument();
    expect(screen.queryByText(/发送后，图片将上传至当前模型供应商/)).not.toBeInTheDocument();
    expect(screen.queryByText(/未配置可用的联网搜索服务/)).not.toBeInTheDocument();
    expect(screen.queryByText(/这是全局联网设置/)).not.toBeInTheDocument();
    expect(screen.queryByText(/不把原图上传到搜索引擎/)).not.toBeInTheDocument();
    expect(screen.queryByText(/联网检索已启用|联网检索已禁用|Plan 或恢复状态暂停联网/)).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalledWith("webSearch.setEnabled", expect.anything());
  };
  const emit = async (type: string, payload: Record<string, unknown>, sessionId = current.sessionId) => {
    await act(async () => {
      listener?.({ v: 1, type: "event", seq: ++sequence, timestamp: sequence,
        sessionId, runId: null, event: { type, payload } });
      await new Promise(requestAnimationFrame);
    });
  };

  beforeEach(() => {
    current = baseWorkspace();
    sequence = 0;
    listener = undefined;
    request.mockReset();
    request.mockImplementation(async (method: string) => {
      if (method === "workspace.open" || method === "session.snapshot") return current;
      if (method === "session.list") return [{ id: "session-2", name: "Other session", message_count: 0 }];
      if (method === "session.open" || method === "session.new") return { ...current, sessionId: "session-2", messages: [] };
      if (method === "command.list") return [];
      return {};
    });
    window.agent = { request, onEvent: (cb) => { listener = cb; return () => undefined; }, onStatus: () => () => undefined };
    window.desktop = { chooseWorkspace: async () => null,
      getBootstrap: async () => ({ defaultWorkspace: "G:\\Coding-agent", platform: "win32" }) };
  });
  afterEach(cleanup);

  it.each([true, false])("keeps image controls without search or upload hints when search enabled=%s", async (enabled) => {
    current.webSearch = searchState(enabled);
    mount();
    await selectImage();
    expectCompact();
  });

  it.each(["unavailable", "unknown"])("does not show search service configuration hints for %s search", async (kind) => {
    current.webSearch = kind === "unknown" ? undefined : {
      ...searchState(), available: false, status: "unavailable", errorCode: "NOT_CONFIGURED",
    };
    mount();
    await selectImage();
    expectCompact();
  });

  it.each(["drafting", "uncertain", "recovery_error"] as const)("does not add search hints during %s", async (phase) => {
    current.collaborationMode = phase === "drafting" ? "plan" : "default";
    current.planState.phase = phase === "drafting" ? "drafting" : "idle";
    current.planState.activePlanId = "plan-1";
    mount();
    await selectImage();
    if (phase !== "drafting") await emit("plan.stateChanged", {
      sessionId: current.sessionId, state: { ...current.planState, mode: "default", phase },
    });
    expectCompact();
  });

  it("does not reintroduce search controls on runtime availability events", async () => {
    mount();
    await selectImage();
    await emit("webSearch.changed", { ...searchState(), available: false, status: "unavailable" });
    expectCompact();
    await emit("webSearch.changed", { ...searchState(false), backend: "native" });
    expectCompact();
  });

  it("clears pending attachments on session switch and ignores stale search events", async () => {
    mount();
    await selectImage();
    fireEvent.click(await screen.findByRole("button", { name: /Other session/ }));
    await waitFor(() => expect(screen.queryByRole("img", { name: "screen.png" })).not.toBeInTheDocument());
    await selectImage();
    await emit("webSearch.changed", { ...searchState(), backend: "stale-event" }, "session-1");
    expectCompact();
    expect(screen.queryByText(/stale-event/)).not.toBeInTheDocument();
  });

  it("still lets users remove a pending image", async () => {
    mount();
    await selectImage();
    fireEvent.click(screen.getByRole("button", { name: "移除图片 screen.png" }));
    await waitFor(() => expect(screen.queryByRole("img", { name: "screen.png" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "＋ 添加文件" })).toBeEnabled();
  });
});
