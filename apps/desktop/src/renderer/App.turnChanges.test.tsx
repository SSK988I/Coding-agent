import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent, WorkspacePayload } from "../shared/types";
import type { ChangesSnapshot, TurnChangesSummary } from "../shared/workbench";
import { App } from "./App";

vi.mock("./workbench/nativePanels", () => ({
  TerminalPanel: () => <div>Native terminal</div>,
  BrowserPanel: () => <div>Native browser</div>,
}));

const workspacePath = "G:\\turn-review-project";
const summary = (runId: string, afterMessageIndex: number, paths = ["src/a.ts"]): TurnChangesSummary => ({
  runId, entryId: `entry-${runId}`, afterMessageIndex, timestamp: afterMessageIndex + 100,
  files: paths.map((path) => ({ path, status: "M", additions: 1, deletions: 1, binary: false })),
  additions: paths.length, deletions: paths.length, truncated: false,
});
const firstTurn = summary("first-run", 1, ["src/a.ts", "src/shared.ts"]);
const secondTurn = summary("second-run", 3);
const workspace = (sessionId = "session-1"): WorkspacePayload => ({
  path: workspacePath, sessionId,
  model: { id: "deepseek-flash", name: "Flash", provider: "deepseek", input: ["text", "image"] },
  thinkingLevel: null, tools: [], collaborationMode: "default",
  planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
  memory: { enabled: false, userId: null, projectId: null },
  messages: sessionId === "session-1" ? [
    { role: "user", content: "First task", timestamp: 1 },
    { role: "assistant", content: "First response", timestamp: 2 },
    { role: "user", content: "Second task", timestamp: 3 },
    { role: "assistant", content: "Second response", timestamp: 4 },
  ] : [{ role: "assistant", content: "Other session response", timestamp: 5 }],
  turnChanges: sessionId === "session-1" ? [firstTurn, secondTurn] : [],
});

function review(change: TurnChangesSummary): ChangesSnapshot {
  return {
    workspacePath, sessionId: "session-1", source: "turn", runId: change.runId,
    branch: "dev", branches: ["dev"], baseRef: null, additions: change.additions,
    deletions: change.deletions, truncated: false,
    files: change.files.map((file) => ({ ...file,
      diff: `--- a/${file.path}\n+++ b/${file.path}\n@@ -1 +1 @@\n-old\n+${change.runId}:${file.path}\n`,
    })),
  };
}

describe("desktop conversation turn changes", () => {
  const request = vi.fn();
  const listeners = new Set<(event: RuntimeEvent) => void>();
  let current: WorkspacePayload;
  let sequence = 0;
  const mount = () => render(<VirtuosoMockContext.Provider value={{ viewportHeight: 2400, itemHeight: 100 }}>
    <App />
  </VirtuosoMockContext.Provider>);
  const emit = async (type: string, payload: Record<string, unknown>, runId = "live-run", sessionId = "session-1") => {
    await act(async () => {
      const event: RuntimeEvent = { v: 1, type: "event", seq: ++sequence, timestamp: sequence + 1000,
        sessionId, runId, event: { type, payload } };
      listeners.forEach((listener) => listener(event));
      await new Promise(requestAnimationFrame);
    });
  };
  const firstCard = () => screen.getByRole("region", { name: "本轮变更 2 个文件" });
  const secondCard = () => screen.getByRole("region", { name: "本轮变更 1 个文件" });

  beforeEach(() => {
    current = workspace();
    sequence = 0;
    listeners.clear();
    request.mockReset().mockImplementation(async (method: string, params: Record<string, unknown> = {}) => {
      if (method === "workspace.open" || method === "session.snapshot") return current;
      if (method === "session.list") return [{ id: "session-2", name: "Other session", message_count: 1 }];
      if (method === "session.open") { current = workspace(String(params.sessionId)); return current; }
      if (method === "command.list") return [];
      if (method === "workbench.changes") {
        const change = params.runId === firstTurn.runId ? firstTurn : secondTurn;
        return review(change);
      }
      return {};
    });
    window.agent = { request, onEvent: (listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
      onStatus: () => () => undefined };
    window.desktop = { chooseWorkspace: async () => null,
      getBootstrap: async () => ({ defaultWorkspace: workspacePath, platform: "win32" }) };
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("restores cards after their own responses and opens the exact earlier review after a newer review", async () => {
    mount();
    await screen.findByText("First response");
    const first = firstCard();
    const second = secondCard();
    expect(screen.getByText("First response").compareDocumentPosition(first) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(first.compareDocumentPosition(screen.getByText("Second task")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText("Second response").compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(within(second).getByRole("button", { name: "查看变更" }));
    await screen.findByText("second-run:src/a.ts");
    expect(request).toHaveBeenCalledWith("workbench.changes", { workspacePath, sessionId: "session-1", source: "turn", runId: "second-run" });
    expect(screen.getByRole("complementary", { name: "工具栏" })).toBeVisible();
    fireEvent.click(within(first).getByRole("button", { name: "查看变更" }));
    await screen.findByText("first-run:src/a.ts");
    expect(request).toHaveBeenLastCalledWith("workbench.changes", { workspacePath, sessionId: "session-1", source: "turn", runId: "first-run" });
    expect(screen.queryByText("second-run:src/a.ts")).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "变更来源" })).toHaveValue("review");
  });

  it("opens a clicked file from its own run and expands that file in the changes sidebar", async () => {
    mount();
    await screen.findByText("First response");
    fireEvent.click(within(firstCard()).getByRole("button", { name: "查看 src/shared.ts 的变更" }));
    await screen.findByText("first-run:src/shared.ts");
    expect(request).toHaveBeenCalledWith("workbench.changes", { workspacePath, sessionId: "session-1", source: "turn", runId: "first-run" });
    const selected = screen.getByText("src/shared.ts", { selector: ".changed-file-path" }).closest("details")!;
    expect(selected).toHaveAttribute("open");
    expect(selected.querySelector("summary")).toHaveFocus();
    expect(screen.getByText("src/a.ts", { selector: ".changed-file-path" }).closest("details")).not.toHaveAttribute("open");
  });

  it("adds one live card after the assistant response and ignores duplicate notifications", async () => {
    current = { ...workspace(), messages: [], turnChanges: [] };
    mount();
    await screen.findByPlaceholderText("描述你想完成的任务…");
    await emit("run.started", { text: "Live task" });
    await emit("message_start", { message: { role: "assistant", content: [] } });
    await emit("message_end", { message: { role: "assistant", content: [{ type: "text", text: "Live response" }], stop_reason: "stop" } });
    expect(screen.queryByRole("region", { name: /本轮变更/ })).not.toBeInTheDocument();
    const change = summary("live-run", 1);
    await emit("workbench.turnChanged", { change });
    await emit("workbench.turnChanged", { change });
    await emit("run.completed", {});
    const cards = screen.getAllByRole("region", { name: "本轮变更 1 个文件" });
    expect(cards).toHaveLength(1);
    expect(screen.getByText("Live response").compareDocumentPosition(cards[0]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("clears restored cards and review selection on session switch and ignores old-session changes", async () => {
    mount();
    await screen.findByText("First response");
    fireEvent.click(within(firstCard()).getByRole("button", { name: "查看变更" }));
    await screen.findByText("first-run:src/a.ts");
    fireEvent.click(screen.getByRole("button", { name: /Other session/ }));
    await screen.findByText("Other session response");
    expect(screen.queryByRole("region", { name: /本轮变更/ })).not.toBeInTheDocument();
    expect(screen.queryByText("first-run:src/a.ts")).not.toBeInTheDocument();
    await emit("workbench.turnChanged", { change: summary("stale-run", 1) }, "stale-run", "session-1");
    expect(screen.queryByRole("region", { name: /本轮变更/ })).not.toBeInTheDocument();
    expect(screen.queryByText("First response")).not.toBeInTheDocument();
    expect(screen.getByText("Other session response")).toBeInTheDocument();
  });
});
