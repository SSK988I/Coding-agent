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
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

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

  it("allows reading archived side conversations without creating or sending one", async () => {
    render(<Workbench {...props} workspace={{ ...workspace, archived: true }} />);
    fireEvent.click(screen.getByRole("button", { name: /^侧边对话/ }));
    await screen.findByRole("option", { name: "Side question" });
    expect(screen.getByRole("button", { name: "新建侧边对话" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "开始侧边对话" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "新建侧边对话" }));
    fireEvent.click(screen.getByRole("button", { name: "开始侧边对话" }));
    expect(request).not.toHaveBeenCalledWith("workbench.chat.open", scope);

    fireEvent.change(screen.getByRole("combobox", { name: "选择侧边对话" }), { target: { value: "child" } });
    const input = await screen.findByLabelText("侧边对话消息");
    expect(request).toHaveBeenCalledWith("workbench.chat.open", { ...scope, conversationId: "child" });
    expect(input).toBeDisabled();
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "选择侧边对话" })).toBeEnabled());
  });

  it("updates an open side conversation to read-only on archive and preserves its draft on restore", async () => {
    const view = render(<Workbench {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /^侧边对话/ }));
    fireEvent.click(screen.getByRole("button", { name: "开始侧边对话" }));
    const input = await screen.findByLabelText("侧边对话消息");
    await waitFor(() => expect(input).toBeEnabled());
    fireEvent.change(input, { target: { value: "Preserved side draft" } });

    view.rerender(<Workbench {...props} workspace={{ ...workspace, archived: true }} />);
    expect(input).toBeDisabled();
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "新建侧边对话" })).toBeDisabled();
    fireEvent.submit(input.closest("form")!);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(request.mock.calls.some(([method]) => method === "workbench.chat.send")).toBe(false);
    expect(input).toHaveValue("Preserved side draft");

    view.rerender(<Workbench {...props} />);
    expect(input).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "发送 ↑" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("workbench.chat.send", {
      ...scope, conversationId: "child", text: "Preserved side draft",
    }));
  });

  it("limits pointer and keyboard resizing by sidebar and conversation widths", () => {
    vi.stubGlobal("innerWidth", 1440);
    vi.stubGlobal("PointerEvent", MouseEvent);
    const onWidth = vi.fn();
    render(<Workbench {...props} width={880} onWidth={onWidth} />);
    const separator = screen.getByRole("separator");
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(onWidth).toHaveBeenLastCalledWith(892);
    fireEvent.pointerDown(separator, { clientX: 1000 });
    fireEvent.pointerMove(document, { clientX: 0 });
    fireEvent.pointerUp(document);
    expect(onWidth).toHaveBeenLastCalledWith(892);
  });

  it("reclamps existing width when the sidebar opens or the window shrinks", () => {
    vi.stubGlobal("innerWidth", 1440);
    const onWidth = vi.fn();
    const view = render(<Workbench {...props} width={1100} onWidth={onWidth} sidebarOpen={false} />);
    expect(onWidth).not.toHaveBeenCalled();
    view.rerender(<Workbench {...props} width={1100} onWidth={onWidth} sidebarOpen />);
    expect(onWidth).toHaveBeenLastCalledWith(892);
    vi.stubGlobal("innerWidth", 1200);
    fireEvent.resize(window);
    expect(onWidth).toHaveBeenLastCalledWith(652);
  });
});

it("renders hunk line numbers, additions, removals and missing-newline notes", () => {
  const view = render(<DiffView diff={changes.files[0].diff + "\\ No newline at end of file\n"} wrap={false} />);
  expect(view.container.querySelector(".diff-add")).toHaveTextContent("3new");
  expect(view.container.querySelector(".diff-remove")).toHaveTextContent("3old");
  expect(screen.getByText(/未改动行已折叠/)).toBeVisible();
  expect(screen.getByText(/No newline/)).toBeVisible();
});

it("keeps header-like source lines inside hunks and advances context line numbers", () => {
  const diff = "--- a/counter.py\n+++ b/counter.py\n@@ -1,2 +1,2 @@\n--- counter;\n+++ counter;\n context\n";
  const view = render(<DiffView diff={diff} wrap={false} />);
  expect(view.container.querySelector(".diff-remove code")).toHaveTextContent("-- counter;");
  expect(view.container.querySelector(".diff-add code")).toHaveTextContent("++ counter;");
  const context = view.container.querySelector(".diff-context");
  expect(context?.querySelectorAll(".diff-line-number")[0]).toHaveTextContent("2");
  expect(context?.querySelectorAll(".diff-line-number")[1]).toHaveTextContent("2");
});

it("mounts only expanded diffs, preserves expansion on refresh and resets it for a new source", async () => {
  let addedLine = "second before refresh";
  request.mockImplementation(async (_method: string, params: { source: string }) => ({
    ...changes, source: params.source,
    files: [changes.files[0], {
      ...changes.files[0], path: "second.py",
      diff: `--- a/second.py\n+++ b/second.py\n@@ -1 +1 @@\n-old second\n+${addedLine}\n`,
    }],
  }));
  render(<ChangesPanel scope={scope} active onOpenFile={vi.fn()} />);
  const first = (await screen.findByText("sample.py")).closest("details")!;
  const second = screen.getByText("second.py").closest("details")!;

  expect(first).toHaveAttribute("open");
  expect(first.querySelector("table")).not.toBeNull();
  expect(second).not.toHaveAttribute("open");
  expect(second.querySelector("table")).toBeNull();

  fireEvent.click(second.querySelector("summary")!);
  await screen.findByText("second before refresh");
  expect(second).toHaveAttribute("open");
  fireEvent.click(second.querySelector("summary")!);
  await waitFor(() => expect(second.querySelector("table")).toBeNull());
  expect(second).not.toHaveAttribute("open");

  fireEvent.click(second.querySelector("summary")!);
  await screen.findByText("second before refresh");
  addedLine = "second after refresh";
  fireEvent.click(screen.getByRole("button", { name: "刷新变更" }));
  await screen.findByText("second after refresh");
  expect(second).toHaveAttribute("open");
  expect(screen.queryByText("second before refresh")).not.toBeInTheDocument();

  fireEvent.click(first.querySelector("summary")!);
  await waitFor(() => expect(first.querySelector("table")).toBeNull());
  fireEvent.change(screen.getByLabelText("变更来源"), { target: { value: "staged" } });
  await waitFor(() => {
    const nextFirst = screen.getByText("sample.py").closest("details")!;
    const nextSecond = screen.getByText("second.py").closest("details")!;
    expect(nextFirst).toHaveAttribute("open");
    expect(nextFirst.querySelector("table")).not.toBeNull();
    expect(nextSecond).not.toHaveAttribute("open");
    expect(nextSecond.querySelector("table")).toBeNull();
  });
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

it.each([
  { lines: 5000, trailingNewline: false, truncated: false },
  { lines: 5000, trailingNewline: true, truncated: true },
  { lines: 6500, trailingNewline: false, truncated: true },
])("bounds preview splitting at 5001 entries ($lines lines, trailing newline: $trailingNewline)", async ({ lines, trailingNewline, truncated }) => {
  const text = Array.from({ length: lines }, (_, index) => index === 4999
    ? "last visible line" : `preview line ${index + 1}`).join("\n") + (trailingNewline ? "\n" : "");
  request.mockImplementation(async (method: string) => method === "workbench.files.list"
    ? { entries: [], truncated: false }
    : { path: "large.py", text, size: text.length, truncated: false, binary: false, image: null });
  const split = vi.spyOn(String.prototype, "split");
  try {
    const view = render(<FilesPanel scope={scope} requestedFile={{ path: "large.py", serial: 1 }} />);
    await screen.findByText("last visible line");
    expect(view.container.querySelectorAll(".file-code-line")).toHaveLength(5000);
    expect(screen.queryByText("preview line 5001")).not.toBeInTheDocument();
    expect(Boolean(screen.queryByText("仅显示前 5000 行。"))).toBe(truncated);
    const previewSplits = split.mock.calls.filter((_, index) => split.mock.contexts[index] === text);
    expect(previewSplits.length).toBeGreaterThan(0);
    for (const args of previewSplits) expect(args).toEqual(["\n", 5001]);
  } finally {
    split.mockRestore();
  }
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
