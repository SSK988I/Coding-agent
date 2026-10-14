import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent, WorkspacePayload } from "../../shared/types";
import { Workbench } from "./Workbench";
import { ChangesPanel, DiffView, FilesPanel, SideChatPanel } from "./panels";

vi.mock("./nativePanels", () => ({
  TerminalPanel: ({ tabKey }: { tabKey: string }) => <div data-testid="terminal">{tabKey}</div>,
  BrowserPanel: () => <div data-testid="browser">Browser</div>,
}));

const scope = { workspacePath: "G:\\project", sessionId: "main" };
const workspace = { path: scope.workspacePath, sessionId: scope.sessionId } as WorkspacePayload;
const changes = { source: "unstaged", branch: "dev", branches: ["main", "dev"], baseRef: null, additions: 1, deletions: 1, truncated: false,
  files: [{ path: "sample.py", status: "M", additions: 1, deletions: 1, binary: false, diff: "--- a/sample.py\n+++ b/sample.py\n@@ -3 +3 @@\n-old\n+new\n" }] };
const side = { conversationId: "child", status: "idle", messages: [], streaming: "", error: null, approval: null };
const request = vi.fn();
const listeners = new Set<(event: RuntimeEvent) => void>();
const emit = (payload: Record<string, unknown>, sessionId = scope.sessionId) => act(() => {
  listeners.forEach((listener) => listener({ v: 1, type: "event", sessionId, runId: null, seq: 1, timestamp: 1, event: { type: "workbench.chat.changed", payload } }));
});

beforeEach(() => {
  listeners.clear();
  request.mockReset().mockImplementation(async (method: string) => {
    if (method === "workbench.changes") return changes;
    if (method === "workbench.files.list") return { path: "", entries: [{ name: "sample.py", path: "sample.py", kind: "file" }], truncated: false };
    if (method === "workbench.files.read") return { path: "sample.py", text: "print('preview')", image: null, size: 16, truncated: false, binary: false };
    if (method === "workbench.chat.list") return { conversations: [{ conversationId: "child", title: "Side question" }] };
    if (method === "workbench.chat.open") return side;
    return { accepted: true };
  });
  window.agent = { request, onEvent: (listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; }, onStatus: () => () => undefined };
  window.desktop = { chooseWorkspace: async () => null, getBootstrap: async () => ({ defaultWorkspace: scope.workspacePath, platform: "win32" }) };
});
afterEach(cleanup);

describe("workspace workbench", () => {
  const props = { workspace, open: true, onClose: vi.fn(), width: 480, onWidth: vi.fn() };
  it("is lazy while closed and lists all five tools when opened", () => {
    const view = render(<Workbench {...props} open={false} />);
    expect(request).not.toHaveBeenCalled();
    expect(screen.getByLabelText("工具栏")).not.toBeVisible();
    view.rerender(<Workbench {...props} />);
    for (const title of ["侧边对话", "变更", "终端", "浏览器", "文件"]) expect(screen.getByRole("button", { name: new RegExp(`^${title}`) })).toBeVisible();
  });

  it("reuses non-terminal tabs, supports multiple terminals and closes to the last used tool", async () => {
    render(<Workbench {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /^变更/ }));
    await screen.findByText("sample.py");
    fireEvent.click(screen.getByLabelText("打开其他工具"));
    fireEvent.click(screen.getByRole("button", { name: /^终端/ }));
    fireEvent.click(screen.getByLabelText("打开其他工具"));
    fireEvent.click(screen.getByRole("button", { name: /^终端/ }));
    expect(screen.getAllByTestId("terminal")).toHaveLength(2);
    fireEvent.click(screen.getByLabelText("关闭终端 2"));
    expect(screen.getByRole("tab", { name: "终端 1" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByLabelText("打开其他工具"));
    fireEvent.click(screen.getByRole("button", { name: /^变更/ }));
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    expect(screen.getByRole("tab", { name: "变更" })).toHaveAttribute("aria-selected", "true");
  });

  it("opens a changed file in the file tab with authoritative scope", async () => {
    render(<Workbench {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /^变更/ }));
    fireEvent.click(await screen.findByRole("button", { name: "在文件中打开 ↗" }));
    await screen.findByText("print('preview')");
    expect(request).toHaveBeenCalledWith("workbench.files.read", { ...scope, path: "sample.py" });
    expect(screen.getByRole("tab", { name: "文件" })).toHaveAttribute("aria-selected", "true");
  });

  it("keeps tools when hidden, but resets tabs on a session switch", () => {
    const view = render(<Workbench key="main" {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /^浏览器/ }));
    view.rerender(<Workbench key="main" {...props} open={false} />);
    view.rerender(<Workbench key="main" {...props} />);
    expect(screen.getByRole("tab", { name: "浏览器" })).toBeVisible();
    view.rerender(<Workbench key="next" {...props} workspace={{ ...workspace, sessionId: "next" }} />);
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^侧边对话/ })).toBeVisible();
  });
});

it("renders hunk line numbers, additions, removals and missing-newline notes", () => {
  const view = render(<DiffView diff={changes.files[0].diff + "\\ No newline at end of file\n"} wrap={false} />);
  expect(view.container.querySelector(".diff-add")).toHaveTextContent("3new");
  expect(view.container.querySelector(".diff-remove")).toHaveTextContent("3old");
  expect(screen.getByText(/未改动行已折叠/)).toBeVisible();
  expect(screen.getByText(/No newline/)).toBeVisible();
});

it("ignores an older diff response after changing the selected source", async () => {
  let resolveOld!: (value: unknown) => void;
  request.mockImplementation(async (_method: string, params: { source: string }) => params.source === "unstaged" ? new Promise((resolve) => { resolveOld = resolve; }) : { ...changes, source: "staged", files: [] });
  render(<ChangesPanel scope={scope} active onOpenFile={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("变更来源"), { target: { value: "staged" } });
  await screen.findByText("当前来源没有变更");
  await act(async () => { resolveOld(changes); });
  expect(screen.queryByText("sample.py")).not.toBeInTheDocument();
  expect(screen.getByText("当前来源没有变更")).toBeVisible();
});

it("does not replace a current file preview with an older read", async () => {
  let resolveOld!: (value: unknown) => void;
  request.mockImplementation(async (method: string, params: { path: string }) => method === "workbench.files.list" ? { entries: [] } : params.path === "old.py" ? new Promise((resolve) => { resolveOld = resolve; }) : { path: "new.py", text: "CURRENT", size: 7 });
  const view = render(<FilesPanel scope={scope} requestedFile={{ path: "old.py", serial: 1 }} />);
  view.rerender(<FilesPanel scope={scope} requestedFile={{ path: "new.py", serial: 2 }} />);
  await screen.findByText("CURRENT");
  await act(async () => { resolveOld({ path: "old.py", text: "STALE" }); });
  expect(screen.queryByText("STALE")).not.toBeInTheDocument();
});

it("sends, approves and stops only the side conversation; unrelated session events are ignored", async () => {
  render(<SideChatPanel scope={scope} />);
  fireEvent.click(screen.getByRole("button", { name: "开始侧边对话" }));
  const input = await screen.findByLabelText("侧边对话消息");
  await waitFor(() => expect(input).not.toBeDisabled());
  fireEvent.change(input, { target: { value: "Side question" } });
  fireEvent.click(screen.getByRole("button", { name: "发送 ↑" }));
  await waitFor(() => expect(request).toHaveBeenCalledWith("workbench.chat.send", { ...scope, conversationId: "child", text: "Side question" }));
  emit({ ...side, status: "running", approval: { approvalId: "approval", toolName: "bash", args: { command: "pnpm test" } } });
  await waitFor(() => expect(screen.getByText("允许本次")).not.toBeDisabled());
  fireEvent.click(screen.getByText("允许本次"));
  await waitFor(() => expect(request).toHaveBeenCalledWith("workbench.chat.approve", { ...scope, conversationId: "child", approvalId: "approval", allowed: true }));
  emit({ ...side, status: "settled" }, "another-main-session");
  const stop = screen.getByText("■ 停止侧边对话");
  await waitFor(() => expect(stop).not.toBeDisabled());
  fireEvent.click(stop);
  await waitFor(() => expect(request).toHaveBeenCalledWith("workbench.chat.stop", { ...scope, conversationId: "child" }));
  expect(request.mock.calls.some(([method]) => method === "run.start" || method === "run.abort")).toBe(false);
});
