import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent, SessionInfo, WorkspacePayload } from "../shared/types";
import { App } from "./App";

type Record = { id: string; name: string; archived: boolean };
const project = "G:\\Coding-agent";
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

describe("desktop session archive", () => {
  const request = vi.fn();
  let records: Map<string, Record>;
  let listener: ((event: RuntimeEvent) => void) | undefined;
  let sequence = 0;
  let selected = "session-1";

  const payload = (id = selected, path = project): WorkspacePayload => ({
    path, sessionId: id, archived: records.get(id)?.archived ?? false,
    model: { id: "offline", name: "Offline", provider: "offline" },
    thinkingLevel: null, tools: [], collaborationMode: "default",
    messages: [
      { role: "user", content: `Question in ${id}` },
      { role: "assistant", content: [{ type: "text", text: `Answer in ${id}` }] },
    ],
    planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
    memory: { enabled: false, userId: null, projectId: null },
  });
  const list = (archived: boolean): SessionInfo[] => [...records.values()]
    .filter((record) => record.archived === archived)
    .map((record) => ({ ...record, first_message: "", message_count: 2, modified: 1 }));
  const mount = () => render(<VirtuosoMockContext.Provider value={{ viewportHeight: 900, itemHeight: 120 }}>
    <App />
  </VirtuosoMockContext.Provider>);
  const rowAction = (name: string, archived = false) => within(screen.getByRole("group", { name }))
    .getByRole("button", { name: archived ? "恢复会话" : "归档会话" });
  const findRowAction = async (name: string, archived = false) => {
    await screen.findByRole("group", { name });
    return rowAction(name, archived);
  };
  const emit = async (type: string, body: object, sessionId = selected) => {
    await act(async () => {
      listener?.({ v: 1, type: "event", seq: ++sequence, timestamp: sequence,
        sessionId, runId: null, event: { type, payload: body as { [key: string]: unknown } } });
      await new Promise(requestAnimationFrame);
    });
  };

  beforeEach(() => {
    sequence = 0;
    selected = "session-1";
    listener = undefined;
    records = new Map([
      ["session-1", { id: "session-1", name: "Current conversation", archived: false }],
      ["session-2", { id: "session-2", name: "Other conversation", archived: false }],
      ["session-3", { id: "session-3", name: "Archived conversation", archived: true }],
    ]);
    request.mockReset();
    request.mockImplementation(async (method: string, params?: { sessionId?: string; archived?: boolean; path?: string }) => {
      if (method === "workspace.open") return payload(selected, params?.path ?? project);
      if (method === "session.open") {
        selected = params!.sessionId!;
        return payload();
      }
      if (method === "session.list") return list(params?.archived === true);
      if (method === "session.archive" || method === "session.unarchive") {
        const record = records.get(params!.sessionId!)!;
        record.archived = method === "session.archive";
        return { sessionId: record.id, archived: record.archived };
      }
      if (method === "session.snapshot") return payload();
      if (method === "command.list") return [];
      return {};
    });
    window.agent = { request, onEvent: (cb) => { listener = cb; return () => undefined; }, onStatus: () => () => undefined };
    window.desktop = { chooseWorkspace: async () => null,
      getBootstrap: async () => ({ defaultWorkspace: project, platform: "win32" }) };
  });
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  it("archives another conversation without opening it or replacing the current transcript", async () => {
    mount();
    fireEvent.click(await findRowAction("Other conversation"));
    await waitFor(() => expect(screen.queryByRole("group", { name: "Other conversation" })).not.toBeInTheDocument());
    expect(request).toHaveBeenCalledWith("session.archive", { sessionId: "session-2" });
    expect(request).not.toHaveBeenCalledWith("session.open", expect.anything());
    expect(screen.getByText("Answer in session-1")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("描述你想完成的任务…")).toBeEnabled();
    expect(screen.queryByText("此会话已归档")).not.toBeInTheDocument();
  });

  it("archives the current conversation while preserving its transcript and draft", async () => {
    mount();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "preserved draft" } });
    fireEvent.click(await findRowAction("Current conversation"));
    expect(await screen.findByText("此会话已归档")).toBeInTheDocument();
    expect(screen.getByText("Answer in session-1")).toBeInTheDocument();
    expect(composer).toBeDisabled();
    expect(composer).toHaveValue("preserved draft");
    expect(screen.getByRole("button", { name: "Plan" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Default" })).toBeDisabled();
    expect(request).not.toHaveBeenCalledWith("session.new");
    expect(request).not.toHaveBeenCalledWith("run.start", expect.anything());
  });

  it("restores the selected conversation and returns focus to its enabled composer", async () => {
    records.get(selected)!.archived = true;
    mount();
    expect(await screen.findByText("此会话已归档")).toBeInTheDocument();
    const composer = screen.getByPlaceholderText("描述你想完成的任务…");
    expect(composer).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "恢复会话" }));
    await waitFor(() => expect(composer).toBeEnabled());
    expect(request).toHaveBeenCalledWith("session.unarchive", { sessionId: "session-1" });
    expect(screen.queryByText("此会话已归档")).not.toBeInTheDocument();
    expect(screen.getByText("Answer in session-1")).toBeInTheDocument();
    await waitFor(() => expect(composer).toHaveFocus());
  });

  it("opens the archived list and reads a conversation without silently restoring it", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "查看已归档会话" }));
    const item = await screen.findByRole("button", { name: /^Archived conversation/ });
    expect(request).toHaveBeenCalledWith("session.list", { archived: true });
    fireEvent.click(item);
    expect(await screen.findByText("Answer in session-3")).toBeInTheDocument();
    expect(screen.getByText("此会话已归档")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("描述你想完成的任务…")).toBeDisabled();
    expect(request).toHaveBeenCalledWith("session.open", { sessionId: "session-3" });
    expect(request).not.toHaveBeenCalledWith("session.unarchive", expect.anything());
  });

  it("restores another archived conversation without changing the selected conversation", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "查看已归档会话" }));
    fireEvent.click(await findRowAction("Archived conversation", true));
    await waitFor(() => expect(screen.queryByRole("group", { name: "Archived conversation" })).not.toBeInTheDocument());
    expect(request).toHaveBeenCalledWith("session.unarchive", { sessionId: "session-3" });
    expect(screen.getByText("Answer in session-1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回会话" }));
    expect(await findRowAction("Archived conversation")).toBeInTheDocument();
  });

  it("reads persisted archive metadata from the backend after remounting", async () => {
    const first = mount();
    fireEvent.click(await findRowAction("Other conversation"));
    await waitFor(() => expect(records.get("session-2")!.archived).toBe(true));
    await waitFor(() => expect(screen.queryByRole("group", { name: "Other conversation" })).not.toBeInTheDocument());
    first.unmount();
    mount();
    await findRowAction("Current conversation");
    expect(screen.queryByRole("group", { name: "Other conversation" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看已归档会话" }));
    expect(await findRowAction("Other conversation", true)).toBeInTheDocument();
  });

  it("keeps the conversation and authoritative state when archive RPC fails", async () => {
    const fallback = request.getMockImplementation()!;
    request.mockImplementation((method: string, params?: object) => method === "session.archive"
      ? Promise.reject(new Error("disk write failed")) : fallback(method, params));
    mount();
    fireEvent.click(await findRowAction("Current conversation"));
    expect(await screen.findByRole("alert")).toHaveTextContent("归档会话失败：disk write failed");
    expect(rowAction("Current conversation")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("描述你想完成的任务…")).toBeEnabled();
    expect(screen.queryByText("此会话已归档")).not.toBeInTheDocument();
  });

  it("waits for the RPC before removing the row and suppresses duplicate archive clicks", async () => {
    const pending = deferred<{ sessionId: string; archived: boolean }>();
    const fallback = request.getMockImplementation()!;
    request.mockImplementation((method: string, params?: object) => method === "session.archive"
      ? pending.promise : fallback(method, params));
    mount();
    const archive = await findRowAction("Other conversation");
    fireEvent.click(archive);
    fireEvent.click(archive);
    expect(archive).toBeDisabled();
    expect(screen.getByRole("button", { name: /^Other conversation/ })).toBeInTheDocument();
    expect(request.mock.calls.filter(([method]) => method === "session.archive")).toHaveLength(1);
    await act(async () => {
      records.get("session-2")!.archived = true;
      pending.resolve({ sessionId: "session-2", archived: true });
    });
    await waitFor(() => expect(screen.queryByRole("group", { name: "Other conversation" })).not.toBeInTheDocument());
  });

  it("disables archive controls while a run is active", async () => {
    mount();
    const archive = await findRowAction("Current conversation");
    await emit("run.started", {});
    expect(archive).toBeDisabled();
    expect(screen.getByRole("button", { name: "查看已归档会话" })).toBeDisabled();
    fireEvent.click(archive);
    expect(request).not.toHaveBeenCalledWith("session.archive", expect.anything());
  });

  it("ignores an archived-list response after returning to the active filter", async () => {
    const pending = deferred<SessionInfo[]>();
    const fallback = request.getMockImplementation()!;
    request.mockImplementation((method: string, params?: { archived?: boolean }) => method === "session.list" && params?.archived
      ? pending.promise : fallback(method, params));
    mount();
    await findRowAction("Current conversation");
    fireEvent.click(screen.getByRole("button", { name: "查看已归档会话" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("session.list", { archived: true }));
    fireEvent.click(screen.getByRole("button", { name: "返回会话" }));
    await findRowAction("Current conversation");
    await act(async () => { pending.resolve(list(true)); });
    expect(rowAction("Current conversation")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Archived conversation" })).not.toBeInTheDocument();
  });

  it("resets the archived filter when a different project opens", async () => {
    window.desktop.chooseWorkspace = async () => "G:\\Other-project";
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "查看已归档会话" }));
    await findRowAction("Archived conversation", true);
    fireEvent.click(screen.getByRole("button", { name: "打开项目" }));
    expect(await screen.findByRole("button", { name: "查看已归档会话" })).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith("workspace.open", { path: "G:\\Other-project", resume: true });
    expect(await findRowAction("Current conversation")).toBeInTheDocument();
  });

  it("applies authoritative archive and restore events to the selected conversation", async () => {
    mount();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    records.get("session-1")!.archived = true;
    await emit("session.archived", { sessionId: "session-1", archived: true });
    expect(screen.getByText("此会话已归档")).toBeInTheDocument();
    expect(composer).toBeDisabled();
    records.get("session-1")!.archived = false;
    await emit("session.unarchived", { sessionId: "session-1", archived: false });
    expect(screen.queryByText("此会话已归档")).not.toBeInTheDocument();
    expect(composer).toBeEnabled();
  });
});
