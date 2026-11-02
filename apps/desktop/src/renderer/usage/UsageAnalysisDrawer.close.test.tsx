import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import UsageAnalysisDrawer from "./UsageAnalysisDrawer";

const drawer = vi.hoisted(() => ({ afterOpenChange: undefined as ((open: boolean) => void) | undefined }));
vi.mock("antd", async (importOriginal) => {
  const actual = await importOriginal<typeof import("antd")>();
  return { ...actual, Drawer: ({ open, afterOpenChange, children }: {
    open: boolean; afterOpenChange?: (open: boolean) => void; children: ReactNode;
  }) => {
    drawer.afterOpenChange = afterOpenChange;
    // Reproduce destroyOnHidden removing the actual content while its motion
    // callback is skipped. Other Ant Design components remain real.
    return open ? <section role="dialog">{children}</section> : null;
  } };
});
vi.mock("./UsageCharts", () => new Promise(() => {}));

const props = { open: true, sessionId: "session", workspacePath: "G:/project", onClose: vi.fn() };

beforeEach(() => {
  drawer.afterOpenChange = undefined;
  window.agent = { request: vi.fn().mockResolvedValue({ sessionId: "session", leafId: "leaf",
    stats: { tokens: { input: 0, output: 0, cache_read: 0, cache_write: 0, total: 0 }, cost: 0 },
    requests: [{ id: "request", index: 1, timestamp: null, model: "test", provider: "test", stopReason: "stop",
      inputTokens: 0, outputTokens: 0, uncachedInputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0,
      cacheHitRate: null, cost: 0 }],
  }), onEvent: () => () => undefined, onStatus: () => () => undefined };
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })));
  const nativeStyle = window.getComputedStyle.bind(window);
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) => nativeStyle(element));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("usage drawer close completion", () => {
  it("finishes each close once when content detaches even if charts are pending and the motion callback is missed", async () => {
    const closed = vi.fn();
    const view = render(<UsageAnalysisDrawer {...props} onAfterClose={closed} />);
    expect(await screen.findAllByRole("status", { name: "正在加载图表" })).toHaveLength(2);
    view.rerender(<UsageAnalysisDrawer {...props} open={false} onAfterClose={closed} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(closed).toHaveBeenCalledOnce();
    act(() => drawer.afterOpenChange?.(false));
    expect(closed).toHaveBeenCalledOnce();

    view.rerender(<UsageAnalysisDrawer {...props} onAfterClose={closed} />);
    act(() => drawer.afterOpenChange?.(false));
    expect(closed).toHaveBeenCalledOnce();
    view.rerender(<UsageAnalysisDrawer {...props} open={false} onAfterClose={closed} />);
    expect(closed).toHaveBeenCalledTimes(2);
  });

  it("uses the latest completion callback without detaching content on unrelated renders", async () => {
    const previous = vi.fn(), current = vi.fn();
    const view = render(<UsageAnalysisDrawer {...props} onAfterClose={previous} />);
    await screen.findByTestId("usage-analysis-drawer");
    view.rerender(<UsageAnalysisDrawer {...props} onAfterClose={current} />);
    expect(previous).not.toHaveBeenCalled();
    expect(current).not.toHaveBeenCalled();
    view.rerender(<UsageAnalysisDrawer {...props} open={false} onAfterClose={current} />);
    expect(previous).not.toHaveBeenCalled();
    expect(current).toHaveBeenCalledOnce();
    act(() => drawer.afterOpenChange?.(false));
    expect(current).toHaveBeenCalledOnce();
  });

  it("does not restore focus when an open drawer is unmounted by navigation", async () => {
    const focus = document.createElement("button");
    focus.textContent = "Next session input";
    document.body.append(focus);
    const closed = vi.fn(() => focus.blur());
    const view = render(<UsageAnalysisDrawer {...props} onAfterClose={closed} />);
    await screen.findByTestId("usage-analysis-drawer");
    focus.focus();
    view.unmount();
    expect(closed).not.toHaveBeenCalled();
    expect(focus).toHaveFocus();
    focus.remove();
  });

  it("does not report an initial closed mount as a completed user close", () => {
    const closed = vi.fn();
    render(<UsageAnalysisDrawer {...props} open={false} onAfterClose={closed} />);
    act(() => drawer.afterOpenChange?.(false));
    expect(closed).not.toHaveBeenCalled();
  });
});
