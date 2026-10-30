import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SideChatPanel } from "./panels";

const scope = { workspacePath: "G:/project", sessionId: "parent" };
const snapshot = { conversationId: "child", title: "New question", status: "idle", messages: [], streaming: "", error: null, approval: null };
const request = vi.fn();
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};

beforeEach(() => {
  request.mockReset().mockImplementation(async (method) => {
    if (method === "workbench.chat.list") return { conversations: [{ conversationId: "child", title: "New question" }] };
    if (method === "workbench.chat.open") return snapshot;
    throw Error(`Unexpected method: ${method}`);
  });
  window.agent = { request, onEvent: () => () => undefined, onStatus: () => () => undefined };
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("side conversation navigation", () => {
  it("never creates another conversation when the placeholder is selected", async () => {
    render(<SideChatPanel scope={scope} />);
    fireEvent.click(screen.getByRole("button", { name: "开始侧边对话" }));
    const selector = screen.getByRole("combobox", { name: "选择侧边对话" });
    await waitFor(() => expect(selector).toHaveValue("child"));
    await waitFor(() => expect(selector).toBeEnabled());
    const before = request.mock.calls.filter(([method]) => method === "workbench.chat.open").length;
    fireEvent.change(selector, { target: { value: "" } });
    expect(request.mock.calls.filter(([method]) => method === "workbench.chat.open")).toHaveLength(before);
    expect(selector).toHaveValue("child");
  });

  it("does not let a late initial list remove a newly opened conversation", async () => {
    const old = deferred<{ conversations: [] }>();
    const fallback = request.getMockImplementation()!;
    let lists = 0;
    request.mockImplementation((method, params) => method === "workbench.chat.list" && lists++ === 0
      ? old.promise : fallback(method, params));
    render(<SideChatPanel scope={scope} />);
    fireEvent.click(screen.getByRole("button", { name: "开始侧边对话" }));
    const selector = screen.getByRole("combobox", { name: "选择侧边对话" });
    await waitFor(() => expect(selector).toHaveValue("child"));
    await act(async () => old.resolve({ conversations: [] }));
    expect(screen.getByRole("option", { name: "New question" })).toBeInTheDocument();
    expect(selector).toHaveValue("child");
  });

  it("keeps the active option visible when the list is still catching up", async () => {
    request.mockImplementation(async (method) => method === "workbench.chat.list" ? { conversations: [] } : snapshot);
    render(<SideChatPanel scope={scope} />);
    fireEvent.click(screen.getByRole("button", { name: "开始侧边对话" }));
    await screen.findByRole("textbox", { name: "侧边对话消息" });
    expect(screen.getByRole("combobox", { name: "选择侧边对话" })).toHaveValue("child");
    expect(screen.getByRole("option", { name: "New question" })).toBeInTheDocument();
  });

  it("ignores an old-scope list after switching the parent session", async () => {
    const old = deferred<{ conversations: { conversationId: string; title: string }[] }>();
    request.mockImplementation((method, params) => method === "workbench.chat.list" && params.sessionId === "parent"
      ? old.promise : Promise.resolve({ conversations: [{ conversationId: "new-child", title: "Next session" }] }));
    const view = render(<SideChatPanel scope={scope} />);
    view.rerender(<SideChatPanel scope={{ ...scope, sessionId: "next" }} />);
    await screen.findByRole("option", { name: "Next session" });
    await act(async () => old.resolve({ conversations: [{ conversationId: "old-child", title: "Old session" }] }));
    expect(screen.queryByRole("option", { name: "Old session" })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Next session" })).toBeInTheDocument();
  });

  it("does not create overlapping drafts from repeated clicks before the RPC finishes", async () => {
    const pending = deferred<typeof snapshot>();
    const fallback = request.getMockImplementation()!;
    request.mockImplementation((method, params) => method === "workbench.chat.open" ? pending.promise : fallback(method, params));
    render(<SideChatPanel scope={scope} />);
    const start = screen.getByRole("button", { name: "开始侧边对话" });
    act(() => { start.click(); start.click(); });
    expect(request.mock.calls.filter(([method]) => method === "workbench.chat.open")).toHaveLength(1);
    await act(async () => pending.resolve(snapshot));
    expect(screen.getByRole("combobox", { name: "选择侧边对话" })).toHaveValue("child");
  });

  it("ignores an open response belonging to the previous parent session", async () => {
    const pending = deferred<typeof snapshot>();
    const fallback = request.getMockImplementation()!;
    request.mockImplementation((method, params) => method === "workbench.chat.open" ? pending.promise : fallback(method, params));
    const view = render(<SideChatPanel scope={scope} />);
    fireEvent.click(screen.getByRole("button", { name: "开始侧边对话" }));
    view.rerender(<SideChatPanel scope={{ ...scope, sessionId: "next" }} />);
    await act(async () => pending.resolve(snapshot));
    expect(screen.queryByRole("textbox", { name: "侧边对话消息" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始侧边对话" })).toBeEnabled();
  });
});
