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
  model: { id: "deepseek-flash", name: "Flash", provider: "deepseek", input: ["text", "image"] },
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
      if (method === "command.list") return [
        { name: "plan", label: "Plan", description: "Plan" },
        { name: "help", label: "Help", description: "Show commands" },
        { name: "session", label: "Session", description: "Show session usage" },
      ];
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

  it("refreshes /session usage without removing help or previous local session output", async () => {
    mount();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/help" } });
    fireEvent.submit(composer.closest("form")!);
    expect(await screen.findByRole("heading", { name: "可用命令" })).toBeInTheDocument();

    fireEvent.change(composer, { target: { value: "/session" } });
    fireEvent.submit(composer.closest("form")!);
    await screen.findByRole("heading", { name: "当前会话" });
    expect(screen.getByRole("heading", { name: "可用命令" })).toBeInTheDocument();
    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("35%");

    fireEvent.change(composer, { target: { value: "/session" } });
    fireEvent.submit(composer.closest("form")!);
    await waitFor(() => expect(screen.getAllByRole("heading", { name: "当前会话" })).toHaveLength(2));
    expect(screen.getByRole("heading", { name: "可用命令" })).toBeInTheDocument();
  });

  it.each(["resolve", "reject"])("ignores a delayed /session %s after switching sessions", async (outcome) => {
    let resolveSnapshot: ((value: WorkspacePayload) => void) | undefined;
    let rejectSnapshot: ((reason: Error) => void) | undefined;
    const fallback = request.getMockImplementation()!;
    request.mockImplementation((method: string) => method === "session.snapshot"
      ? new Promise<WorkspacePayload>((resolve, reject) => { resolveSnapshot = resolve; rejectSnapshot = reject; })
      : fallback(method));
    mount();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/session" } });
    fireEvent.submit(composer.closest("form")!);
    await waitFor(() => expect(request).toHaveBeenCalledWith("session.snapshot"));
    fireEvent.click(await screen.findByRole("button", { name: /Other session/ }));
    await waitFor(() => expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("75%"));

    await act(async () => {
      if (outcome === "resolve") resolveSnapshot?.(workspace("session-1", stats(99)));
      else rejectSnapshot?.(new Error("old session request failed"));
    });

    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("75%");
    expect(screen.queryByRole("heading", { name: "当前会话" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each(["resolve", "reject"])("ignores an old /session %s after navigating away and back", async (outcome) => {
    let resolveSnapshot: ((value: WorkspacePayload) => void) | undefined;
    let rejectSnapshot: ((reason: Error) => void) | undefined;
    const fallback = request.getMockImplementation()!;
    request.mockImplementation((method: string, params?: { sessionId?: string }) => {
      if (method === "session.snapshot") return new Promise<WorkspacePayload>((resolve, reject) => {
        resolveSnapshot = resolve;
        rejectSnapshot = reject;
      });
      if (method === "session.list") return [
        { id: "session-1", name: "Original session", message_count: 0 },
        { id: "session-2", name: "Other session", message_count: 0 },
      ];
      if (method === "session.open") return workspace(params?.sessionId, stats(75));
      return fallback(method);
    });
    mount();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/session" } });
    fireEvent.submit(composer.closest("form")!);
    await waitFor(() => expect(request).toHaveBeenCalledWith("session.snapshot"));
    fireEvent.click(await screen.findByRole("button", { name: /Other session/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Other session/ })).toHaveAttribute("aria-current", "true"));
    fireEvent.click(screen.getByRole("button", { name: /Original session/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Original session/ })).toHaveAttribute("aria-current", "true"));

    await act(async () => {
      if (outcome === "resolve") resolveSnapshot?.(workspace("session-1", stats(99)));
      else rejectSnapshot?.(new Error("obsolete session request failed"));
    });

    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("75%");
    expect(screen.queryByRole("heading", { name: "当前会话" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("ignores an old /session result after session events leave and return", async () => {
    let resolveSnapshot: ((value: WorkspacePayload) => void) | undefined;
    const fallback = request.getMockImplementation()!;
    request.mockImplementation((method: string) => method === "session.snapshot"
      ? new Promise<WorkspacePayload>((resolve) => { resolveSnapshot = resolve; }) : fallback(method));
    mount();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/session" } });
    fireEvent.submit(composer.closest("form")!);
    await waitFor(() => expect(request).toHaveBeenCalledWith("session.snapshot"));
    await emit("session.changed", workspace("session-2", stats(40)) as unknown as Record<string, unknown>, "session-2");
    await emit("session.changed", workspace("session-1", stats(75)) as unknown as Record<string, unknown>);

    await act(async () => { resolveSnapshot?.(workspace("session-1", stats(99))); });

    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("75%");
    expect(screen.queryByRole("heading", { name: "当前会话" })).not.toBeInTheDocument();
  });

  it("ignores /session snapshots belonging to a different session", async () => {
    const fallback = request.getMockImplementation()!;
    request.mockImplementation((method: string) => method === "session.snapshot"
      ? workspace("different-session", stats(99)) : fallback(method));
    mount();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    await act(async () => {
      fireEvent.change(composer, { target: { value: "/session" } });
      fireEvent.submit(composer.closest("form")!);
    });

    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("20%");
    expect(screen.queryByRole("heading", { name: "当前会话" })).not.toBeInTheDocument();
  });

  it("ignores /session results while session navigation is still pending", async () => {
    let resolveSnapshot: ((value: WorkspacePayload) => void) | undefined;
    let resolveNavigation: ((value: WorkspacePayload) => void) | undefined;
    const fallback = request.getMockImplementation()!;
    request.mockImplementation((method: string) => {
      if (method === "session.snapshot") return new Promise<WorkspacePayload>((resolve) => { resolveSnapshot = resolve; });
      if (method === "session.open") return new Promise<WorkspacePayload>((resolve) => { resolveNavigation = resolve; });
      return fallback(method);
    });
    mount();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/session" } });
    fireEvent.submit(composer.closest("form")!);
    await waitFor(() => expect(request).toHaveBeenCalledWith("session.snapshot"));
    fireEvent.click(await screen.findByRole("button", { name: /Other session/ }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("session.open", { sessionId: "session-2" }));

    await act(async () => { resolveSnapshot?.(workspace("session-1", stats(99))); });

    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("20%");
    expect(screen.queryByRole("heading", { name: "当前会话" })).not.toBeInTheDocument();
    await act(async () => { resolveNavigation?.(workspace("session-2", stats(75))); });
    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("75%");
  });

  it("still reports a current /session request failure without changing usage", async () => {
    const fallback = request.getMockImplementation()!;
    request.mockImplementation((method: string) => method === "session.snapshot"
      ? Promise.reject(new Error("current session request failed")) : fallback(method));
    mount();
    const composer = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(composer, { target: { value: "/session" } });
    fireEvent.submit(composer.closest("form")!);

    expect(await screen.findByRole("alert")).toHaveTextContent("current session request failed");
    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("20%");
    expect(screen.queryByRole("heading", { name: "当前会话" })).not.toBeInTheDocument();
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
