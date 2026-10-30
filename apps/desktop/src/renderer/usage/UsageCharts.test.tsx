import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionStats, UsageAnalysisRequest } from "../../shared/types";
import { CacheCompositionChart, UsageChart, UsageTrendChart } from "./UsageCharts";

const engine = vi.hoisted(() => ({
  init: vi.fn(), register: vi.fn(), setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn(),
}));
vi.mock("echarts/core", () => ({ init: engine.init, use: engine.register }));
vi.mock("echarts/charts", () => ({ BarChart: {}, LineChart: {}, PieChart: {} }));
vi.mock("echarts/components", () => ({ DataZoomComponent: {}, GridComponent: {}, LegendComponent: {}, TooltipComponent: {} }));
vi.mock("echarts/renderers", () => ({ SVGRenderer: {} }));

const row = (overrides: Partial<UsageAnalysisRequest> = {}): UsageAnalysisRequest => ({
  id: "request-1", index: 1, timestamp: null, provider: "deepseek", model: "flash", stopReason: "stop",
  inputTokens: 1000, uncachedInputTokens: 200, outputTokens: 250, cacheReadTokens: 800,
  cacheWriteTokens: 0, cacheHitRate: 80, cost: .0042, ...overrides,
});

describe("usage chart lifecycle and unknown data", () => {
  let resize: () => void;
  let disconnect: () => void;
  let width = 600;
  beforeEach(() => {
    width = 600;
    resize = () => undefined;
    disconnect = vi.fn();
    engine.init.mockReset().mockReturnValue({ setOption: engine.setOption, resize: engine.resize, dispose: engine.dispose });
    engine.setOption.mockClear(); engine.resize.mockClear(); engine.dispose.mockClear();
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => width);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(260);
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: () => void) { resize = callback; }
      observe() {}
      disconnect() { disconnect(); }
    });
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("initializes only with visible dimensions, resizes and releases observers/instances on close", () => {
    width = 0;
    const { rerender, unmount } = render(<UsageChart option={{}} label="chart" />);
    act(() => resize());
    expect(engine.init).not.toHaveBeenCalled();
    width = 600;
    act(() => resize());
    expect(engine.init).toHaveBeenCalledOnce();
    expect(engine.init).toHaveBeenCalledWith(screen.getByRole("img", { name: "chart" }), undefined, { renderer: "svg" });
    act(() => resize());
    expect(engine.resize).toHaveBeenCalledOnce();
    rerender(<UsageChart option={{}} label="chart" active={false} />);
    expect(engine.dispose).toHaveBeenCalledOnce();
    expect(disconnect).toHaveBeenCalledOnce();
    act(() => resize());
    expect(engine.init).toHaveBeenCalledOnce();
    rerender(<UsageChart option={{}} label="chart" active />);
    act(() => resize());
    expect(engine.init).toHaveBeenCalledTimes(2);
    unmount();
    expect(engine.dispose).toHaveBeenCalledTimes(2);
  });

  it("updates options without recreating the chart", () => {
    const { rerender } = render(<UsageChart option={{ animation: false }} label="chart" />);
    act(() => resize());
    rerender(<UsageChart option={{ animation: true }} label="chart" />);
    expect(engine.init).toHaveBeenCalledOnce();
    expect(engine.setOption).toHaveBeenLastCalledWith({ animation: true }, { notMerge: true });
  });

  it("preserves gaps for requests whose measurements are missing", () => {
    const requests = [row(), row({ id: "unknown", index: 2, inputTokens: null, outputTokens: null, cost: null })];
    const { rerender } = render(<UsageTrendChart requests={requests} metric="tokens" active />);
    act(() => resize());
    let option = engine.setOption.mock.calls.at(-1)![0];
    expect(option.series[0].data).toEqual([1000, null]);
    expect(option.series[1].data).toEqual([250, null]);
    rerender(<UsageTrendChart requests={requests} metric="cost" active />);
    option = engine.setOption.mock.calls.at(-1)![0];
    expect(option.series[0].data).toEqual([.0042, null]);
    expect(option.series[0].connectNulls).toBe(false);
    expect(option.yAxis.axisLabel.formatter(.000001)).toBe("$1.0e-6");
  });

  it("keeps an unknown-only trend empty but plots explicitly measured zero", () => {
    const { rerender } = render(<UsageTrendChart requests={[row({ inputTokens: null, outputTokens: null })]} metric="tokens" active />);
    expect(screen.getByText("暂无已记录的 Token 用量")).toBeInTheDocument();
    expect(screen.queryByTestId("usage-analysis-chart")).not.toBeInTheDocument();
    rerender(<UsageTrendChart requests={[row({ inputTokens: 0, outputTokens: 0 })]} metric="tokens" active />);
    act(() => resize());
    expect(engine.init).toHaveBeenCalledOnce();
    expect(engine.setOption.mock.calls.at(-1)![0].series[0].data).toEqual([0]);
  });

  it("does not draw a cache composition when reporting is incomplete", () => {
    const stats: SessionStats = { tokens: { input: 200, output: 250, cache_read: 800, cache_write: 0, total: 1250 }, cost: .0042,
      metrics: { input_tokens: 1000, cache_hit_rate: null, context_tokens: null, context_window: null,
        context_percent: null, context_estimated: true } };
    const { rerender } = render(<CacheCompositionChart stats={stats} active />);
    expect(screen.getByText("暂无完整的缓存统计")).toBeInTheDocument();
    expect(screen.queryByTestId("usage-analysis-chart")).not.toBeInTheDocument();
    rerender(<CacheCompositionChart stats={{ ...stats, metrics: { ...stats.metrics!, cache_hit_rate: 80 } }} active />);
    act(() => resize());
    expect(engine.setOption.mock.calls.at(-1)![0].series[0].data.map((entry: { value: number }) => entry.value))
      .toEqual([800, 0, 200]);
  });
});
