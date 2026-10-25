import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import ReactMarkdown from "react-markdown";
import { VirtuosoMockContext } from "react-virtuoso";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RuntimeEvent, WorkspacePayload } from "../shared/types";
import { App } from "./App";
import { FilesPanel } from "./workbench/panels";

vi.mock("react-markdown", async () => {
  const actual = await vi.importActual<typeof import("react-markdown")>("react-markdown");
  return { ...actual, default: vi.fn(actual.default) };
});

vi.mock("./workbench/panels", async () => {
  const actual = await vi.importActual<typeof import("./workbench/panels")>("./workbench/panels");
  return { ...actual, FilesPanel: vi.fn(actual.FilesPanel) };
});

const workspace: WorkspacePayload = {
  path: "G:\\Coding-agent",
  sessionId: "session-1",
  model: { id: "m", name: "Model", provider: "test" },
  thinkingLevel: null,
  tools: [],
  messages: [],
  collaborationMode: "default",
  planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
  memory: { enabled: false, userId: "local-user", projectId: "test" },
};

async function setupApp() {
  let eventListener: ((event: RuntimeEvent) => void) | undefined;
  const request = vi.fn();
  request.mockImplementation(async (method: string) => method === "workspace.open" ? workspace : []);
  window.agent = {
    request,
    onEvent: (listener) => { eventListener = listener; return () => undefined; },
    onStatus: () => () => undefined,
  };
  window.desktop = {
    chooseWorkspace: async () => null,
    getBootstrap: async () => ({ defaultWorkspace: workspace.path, platform: "win32" }),
  };
  render(
    <VirtuosoMockContext.Provider value={{ viewportHeight: 900, itemHeight: 120 }}>
      <App />
    </VirtuosoMockContext.Provider>,
  );
  await screen.findByText("Model");
  let seq = 0;
  const emit = async (
    events: { type: string; payload: Record<string, unknown> }[],
    sessionId = workspace.sessionId,
  ) => {
    await act(async () => {
      for (const event of events) {
        seq += 1;
        eventListener?.({ v: 1, type: "event", seq, timestamp: seq, sessionId, runId: "run-1", event });
      }
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    });
  };
  return { request, emit };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("desktop Markdown rendering", () => {
  it("reuses parsed Markdown for thinking and status changes but renders new text", async () => {
    const { emit } = await setupApp();
    const markdown = vi.mocked(ReactMarkdown);
    await emit([
      { type: "message_start", payload: { message: { role: "assistant", content: "" } } },
      { type: "message_update", payload: { kind: "text_delta", delta: "Stable **body**" } },
    ]);
    expect(screen.getByText("body", { selector: "strong" })).toBeInTheDocument();
    const rendersAfterText = markdown.mock.calls.length;

    await emit([{ type: "message_update", payload: { kind: "thinking_delta", delta: "More reasoning" } }]);
    expect(screen.getByText("More reasoning")).toBeInTheDocument();
    expect(markdown).toHaveBeenCalledTimes(rendersAfterText);

    await emit([{ type: "message_update", payload: { kind: "text_delta", delta: " and **update**" } }]);
    expect(screen.getByText("update", { selector: "strong" })).toBeInTheDocument();
    expect(markdown).toHaveBeenCalledTimes(rendersAfterText + 1);

    await emit([{ type: "message_end", payload: {
      message: { role: "assistant", stop_reason: "end_turn", content: [
        { type: "thinking", thinking: "More reasoning" },
        { type: "text", text: "Stable **body** and **update**" },
      ] },
    } }]);
    expect(markdown).toHaveBeenCalledTimes(rendersAfterText + 1);
    expect(screen.getByText("body", { selector: "strong" })).toBeInTheDocument();
  });
});

it("skips workbench panel renders for main-chat updates but responds to its own state and props", async () => {
  const { request, emit } = await setupApp();
  request.mockImplementation(async (method: string) => method === "workbench.files.list"
    ? { entries: [], truncated: false } : []);
  fireEvent.click(screen.getByRole("button", { name: "打开工具栏" }));
  fireEvent.click(screen.getByRole("button", { name: /^文件/ }));
  await screen.findByText("目录为空");
  const filesPanel = vi.mocked(FilesPanel);
  const initialRenders = filesPanel.mock.calls.length;
  expect(initialRenders).toBeGreaterThan(0);

  fireEvent.change(screen.getByPlaceholderText("描述你想完成的任务…"), { target: { value: "A main-chat draft" } });
  await emit([
    { type: "message_start", payload: { message: { role: "assistant", content: "" } } },
    { type: "message_update", payload: { kind: "text_delta", delta: "A streamed main-chat response" } },
  ]);
  expect(screen.getByText("A streamed main-chat response")).toBeInTheDocument();
  expect(filesPanel).toHaveBeenCalledTimes(initialRenders);

  const separator = screen.getByRole("separator");
  const width = Number(separator.getAttribute("aria-valuenow"));
  fireEvent.keyDown(separator, { key: "ArrowRight" });
  expect(separator).toHaveAttribute("aria-valuenow", String(Math.max(360, width - 40)));
  expect(filesPanel.mock.calls.length).toBeGreaterThan(initialRenders);
  const afterResize = filesPanel.mock.calls.length;
  fireEvent.click(screen.getByRole("button", { name: "打开其他工具" }));
  fireEvent.click(screen.getByRole("button", { name: /^文件/ }));
  expect(screen.getByRole("tab", { name: "文件" })).toHaveAttribute("aria-selected", "true");
  expect(filesPanel.mock.calls.length).toBeGreaterThan(afterResize);

  fireEvent.click(screen.getByRole("button", { name: "关闭工具栏" }));
  expect(screen.getByLabelText("工具栏")).not.toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "打开工具栏" }));
  expect(screen.getByRole("tab", { name: "文件" })).toBeVisible();
  await emit([{ type: "session.changed", payload: {
    ...workspace, sessionId: "session-2",
  } }], "session-2");
  await waitFor(() => expect(screen.queryByRole("tab", { name: "文件" })).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: /^文件/ })).toBeVisible();
});
