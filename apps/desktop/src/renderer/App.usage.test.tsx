import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent, SessionStats, WorkspacePayload } from "../shared/types";
import { App } from "./App";

const stats = (percent: number): SessionStats => ({
  tokens: { input: 200, output: 350, cache_read: 800, cache_write: 0, total: 1350 },
  cost: 0.0123,
  metrics: { cache_hit_rate: percent, input_tokens: 1000, context_tokens: 20000,
    context_window: 100000, context_percent: percent, context_estimated: true },
});
const workspace = (sessionId = "session-1", usage?: SessionStats): WorkspacePayload => ({
  path: "G:\\Coding-agent", sessionId,
  model: { id: "deepseek-flash", name: "Flash", provider: "deepseek" },
  thinkingLevel: null, tools: [], messages: [], collaborationMode: "default",
  planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
  memory: { enabled: false, userId: null, projectId: null }, stats: usage,
});

describe("desktop authoritative session usage", () => {
  const request = vi.fn();
  let listener: ((event: RuntimeEvent) => void) | undefined;
  let sequence = 0;
  const emit = async (type: string, payload: Record<string, unknown>, sessionId = "session-1") => {
    await act(async () => {
      listener?.({ v: 1, type: "event", seq: ++sequence, timestamp: sequence,
        sessionId, runId: "run-1", event: { type, payload } });
      await new Promise(requestAnimationFrame);
    });
  };
  const mount = () => render(<VirtuosoMockContext.Provider value={{ viewportHeight: 900, itemHeight: 120 }}>
    <App />
  </VirtuosoMockContext.Provider>);

  beforeEach(() => {
    sequence = 0;
    listener = undefined;
    request.mockReset();
    request.mockImplementation(async (method: string) => {
      if (method === "workspace.open") return workspace("session-1", stats(20));
      if (method === "session.open") return workspace("session-2", stats(75));
      if (method === "session.new") return workspace("session-3");
      if (method === "session.snapshot") return workspace("session-1", stats(35));
      if (method === "session.list") return [{ id: "session-2", name: "Other session", message_count: 0 }];
      if (method === "command.list") return [{ name: "plan", label: "Plan", description: "Plan" }];
      return {};
    });
    window.agent = { request, onEvent: (cb) => { listener = cb; return () => undefined; }, onStatus: () => () => undefined };
    window.desktop = { chooseWorkspace: async () => null,
      getBootstrap: async () => ({ defaultWorkspace: "G:\\Coding-agent", platform: "win32" }) };
  });
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  it("hydrates workspace metrics and updates them during a run", async () => {
    mount();
    await waitFor(() => expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("20%"));
    await emit("session.statsChanged", { stats: stats(70) });
    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("70%");
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "70");
  });

  it.each(["run.completed", "run.cancelled", "run.failed"])("applies final authoritative metrics on %s", async (type) => {
    mount();
    await screen.findByPlaceholderText("描述你想完成的任务…");
    await emit(type, { stats: stats(90) });
    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("90%");
  });

  it("rehydrates metrics from the authoritative session snapshot", async () => {
    mount();
    await screen.findByPlaceholderText("描述你想完成的任务…");
    await emit("run.started", {});
    fireEvent.click(screen.getByRole("button", { name: "■ 停止" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("session.snapshot"));
    await waitFor(() => expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("35%"));
  });

  it("restores selected session metrics and ignores delayed stats from the previous session", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Other session/ }));
    await waitFor(() => expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("75%"));
    await emit("session.statsChanged", { stats: stats(99) });
    await emit("run.completed", { stats: stats(98) });
    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("75%");
    await emit("session.statsChanged", { stats: stats(50) }, "session-2");
    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("50%");
  });

  it("clears old metrics when a new or legacy session has no stats", async () => {
    mount();
    await waitFor(() => expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("20%"));
    fireEvent.click(screen.getByTitle("新建会话"));
    await waitFor(() => expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("—"));
    expect(screen.getByTestId("usage-cost")).toHaveTextContent("—");
  });

  it("uses session change snapshots then only their trailing stats", async () => {
    mount();
    await screen.findByPlaceholderText("描述你想完成的任务…");
    await emit("session.changed", workspace("session-2", stats(40)) as unknown as Record<string, unknown>, "session-2");
    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("40%");
    await emit("session.statsChanged", { stats: stats(65) }, "session-2");
    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("65%");
  });
});
