import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionStats, UsageAnalysisPayload, UsageAnalysisRequest } from "../../shared/types";
import UsageAnalysisDrawer from "./UsageAnalysisDrawer";

const chartState = vi.hoisted(() => ({ gate: undefined as Promise<void> | undefined, fail: false }));
vi.mock("./UsageCharts", async () => {
  await chartState.gate;
  return {
    UsageTrendChart: ({ metric, requests }: { metric: string; requests: UsageAnalysisRequest[] }) => {
      if (chartState.fail) throw new Error("chart module failure");
      return <div data-testid="trend-chart" data-request-order={requests.map((request) => request.index).join(",")}>{metric}</div>;
    },
    CacheCompositionChart: () => <div data-testid="cache-chart" />,
  };
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};
const stats = (input = 1000): SessionStats => ({
  tokens: { input: input / 5, cache_read: input * .8, cache_write: 0, output: 250, total: input + 250 }, cost: .0042,
  metrics: { cache_hit_rate: 80, input_tokens: input, context_tokens: 5000, context_window: 10000,
    context_percent: 50, context_estimated: false },
});
const zeroStats = (): SessionStats => {
  const usage = stats(0);
  return { ...usage, tokens: { ...usage.tokens, output: 0, total: 0 }, cost: 0,
    metrics: { ...usage.metrics!, cache_hit_rate: null } };
};
const row = (overrides: Partial<UsageAnalysisRequest> = {}): UsageAnalysisRequest => ({
  id: "request-1", index: 1, timestamp: 1700000000000, provider: "deepseek", model: "deepseek-flash", stopReason: "stop",
  inputTokens: 1000, uncachedInputTokens: 200, outputTokens: 250, cacheReadTokens: 800, cacheWriteTokens: 0,
  cacheHitRate: 80, cost: .0042, ...overrides,
});
const report = (overrides: Partial<UsageAnalysisPayload> = {}): UsageAnalysisPayload => ({
  sessionId: "session-1", leafId: "leaf-1", stats: stats(), requests: [row()], ...overrides,
});
const props = { open: true, sessionId: "session-1", workspacePath: "G:/project", onClose: vi.fn() };

describe("session usage analysis drawer", () => {
  const request = vi.fn();
  beforeEach(() => {
    chartState.fail = false;
    request.mockReset().mockResolvedValue(report());
    window.agent = { request, onEvent: () => () => undefined, onStatus: () => () => undefined };
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(),
      addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })));
    const nativeStyle = window.getComputedStyle.bind(window);
    vi.spyOn(window, "getComputedStyle").mockImplementation((element) => nativeStyle(element));
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("keeps the summary and request details usable while the chart module is pending", async () => {
    let release!: () => void;
    chartState.gate = new Promise<void>((resolve) => { release = resolve; });
    render(<UsageAnalysisDrawer {...props} />);
    await screen.findByText("1 次模型请求");
    expect(screen.getByText("累计输入").closest("article")).toHaveTextContent("1K");
    expect(await screen.findAllByRole("status", { name: "正在加载图表" })).toHaveLength(2);
    expect(screen.queryByTestId("trend-chart")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "请求明细" }));
    expect(await screen.findByText("deepseek-flash")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭用量分析" }));
    expect(props.onClose).toHaveBeenCalled();
    await act(async () => release());
  });

  it("loads only when open and displays server stats with both chart and table views", async () => {
    const close = vi.fn();
    const { rerender } = render(<UsageAnalysisDrawer {...props} open={false} onClose={close} />);
    expect(request).not.toHaveBeenCalled();
    rerender(<UsageAnalysisDrawer {...props} onClose={close} />);
    await screen.findByText("1 次模型请求");
    expect(request).toHaveBeenCalledWith("session.usage", { sessionId: "session-1" });
    expect(screen.getByText("累计输入").closest("article")).toHaveTextContent("1K");
    expect(await screen.findByTestId("trend-chart")).toHaveTextContent("tokens");
    fireEvent.click(screen.getByText("估算费用", { selector: ".ant-segmented-item-label" }));
    expect(screen.getByTestId("trend-chart")).toHaveTextContent("cost");
    fireEvent.click(screen.getByRole("tab", { name: "请求明细" }));
    expect(await screen.findByText("deepseek-flash")).toBeInTheDocument();
    expect(screen.getByRole("table")).toHaveTextContent("1,000");
    fireEvent.click(screen.getByRole("button", { name: "关闭用量分析" }));
    expect(close).toHaveBeenCalledOnce();
  });

  it("shows the latest requests first across pages without reversing chart data or the server payload", async () => {
    const requests = Array.from({ length: 12 }, (_value, index) => row({ id: `request-${index + 1}`, index: index + 1 }));
    Object.freeze(requests);
    request.mockResolvedValue(report({ requests }));
    render(<UsageAnalysisDrawer {...props} />);
    await screen.findByText("12 次模型请求");
    const chronological = requests.map((entry) => entry.index).join(",");
    expect(await screen.findByTestId("trend-chart")).toHaveAttribute("data-request-order", chronological);
    fireEvent.click(screen.getByRole("tab", { name: "请求明细" }));
    const visibleIndices = () => Array.from(screen.getByRole("table").querySelectorAll(".usage-analysis-request-index"),
      (element) => element.textContent);
    expect(visibleIndices()).toEqual(["#12", "#11", "#10", "#9", "#8", "#7", "#6", "#5", "#4", "#3"]);
    fireEvent.click(screen.getByTitle("下一页"));
    expect(visibleIndices()).toEqual(["#2", "#1"]);
    fireEvent.click(screen.getByRole("tab", { name: "用量趋势" }));
    expect(await screen.findByTestId("trend-chart")).toHaveAttribute("data-request-order", chronological);
    fireEvent.click(screen.getByRole("tab", { name: "请求明细" }));
    expect(visibleIndices()[0]).toBe("#12");
    expect(requests.map((entry) => entry.index).join(",")).toBe(chronological);
  });

  it("shows a pending state, reports RPC errors and allows retry", async () => {
    const pending = deferred<UsageAnalysisPayload>();
    request.mockReturnValueOnce(pending.promise);
    render(<UsageAnalysisDrawer {...props} stats={stats()} />);
    expect(screen.getByRole("status", { name: "正在加载用量分析" })).toBeInTheDocument();
    expect(screen.getByText("累计输入").closest("article")).toHaveTextContent("1K");
    await act(async () => pending.reject(new Error("offline backend")));
    expect(await screen.findByRole("alert")).toHaveTextContent("offline backend");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByText("1 次模型请求");
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("keeps request details available when chart rendering fails and retries charts alone", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    chartState.fail = true;
    render(<UsageAnalysisDrawer {...props} />);
    await screen.findByText("图表暂时无法加载");
    expect(screen.getByText("累计输入").closest("article")).toHaveTextContent("1K");
    fireEvent.click(screen.getByRole("tab", { name: "请求明细" }));
    expect(await screen.findByText("deepseek-flash")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "用量趋势" }));
    await screen.findByText("图表暂时无法加载");
    chartState.fail = false;
    fireEvent.click(screen.getByRole("button", { name: "重试图表" }));
    expect(await screen.findByTestId("trend-chart")).toHaveTextContent("tokens");
    expect(request).toHaveBeenCalledOnce();
  });

  it("keeps unknown measurements distinct from measured zero in details", async () => {
    const usage = stats(0);
    usage.metrics = { ...usage.metrics!, cache_hit_rate: null, context_tokens: null,
      context_window: null, context_percent: null, context_estimated: true };
    request.mockResolvedValue(report({ stats: usage, requests: [row({
      model: "unknown-model", inputTokens: null, outputTokens: null, cacheReadTokens: null,
      cacheWriteTokens: null, cacheHitRate: null, cost: null, stopReason: "error",
    }), row({ id: "zero", index: 2, model: "zero-model", inputTokens: 0, outputTokens: 0, cacheHitRate: null, cost: 0 })] }));
    render(<UsageAnalysisDrawer {...props} />);
    await screen.findByText("2 次模型请求");
    expect(screen.getByText("缓存命中率").closest("article")).toHaveTextContent("—");
    expect(screen.getByRole("progressbar", { name: "当前上下文使用率" })).not.toHaveAttribute("aria-valuenow");
    fireEvent.click(screen.getByRole("tab", { name: "请求明细" }));
    const unknown = (await screen.findByText("unknown-model")).closest("tr")!;
    expect(within(unknown).getAllByText("—")).toHaveLength(4);
    expect(unknown).toHaveTextContent("失败");
    const zero = screen.getByText("zero-model").closest("tr")!;
    expect(within(zero).getAllByText("0")).toHaveLength(2);
    expect(zero).toHaveTextContent("$0.0000");
  });

  it("does not present unreported requests as zero usage or free requests in summary", async () => {
    request.mockResolvedValue(report({ stats: zeroStats(), requests: [row({
      inputTokens: null, outputTokens: null, cost: null, cacheHitRate: null,
    })] }));
    render(<UsageAnalysisDrawer {...props} />);
    await screen.findByText("1 次模型请求");
    for (const label of ["累计输入", "累计输出", "估算费用"]) {
      expect(screen.getByText(label, { selector: ".usage-analysis-kpi > span" }).closest("article")!.querySelector("strong")).toHaveTextContent("—");
    }
  });

  it("retains explicitly measured zero in summary", async () => {
    request.mockResolvedValue(report({ stats: zeroStats(), requests: [row({
      inputTokens: 0, outputTokens: 0, cost: 0, cacheHitRate: null,
    })] }));
    render(<UsageAnalysisDrawer {...props} />);
    await screen.findByText("1 次模型请求");
    for (const label of ["累计输入", "累计输出"]) {
      expect(screen.getByText(label).closest("article")!.querySelector("strong")).toHaveTextContent("0");
    }
    expect(screen.getByText("估算费用", { selector: ".usage-analysis-kpi > span" }).closest("article")!.querySelector("strong")).toHaveTextContent("$0.0000");
    expect(screen.queryByText(/部分请求未报告/)).not.toBeInTheDocument();
  });

  it("keeps authoritative aggregates and marks incomplete reporting separately for each metric", async () => {
    const usage = stats(1600);
    usage.tokens.output = 150;
    usage.cost = .007;
    usage.metrics!.cache_hit_rate = null;
    request.mockResolvedValue(report({ stats: usage, requests: [row({ inputTokens: 700, outputTokens: 25, cost: .001 }),
      row({ id: "unknown", index: 2, inputTokens: null, outputTokens: 125, cost: null, cacheHitRate: null })] }));
    render(<UsageAnalysisDrawer {...props} />);
    await screen.findByText("2 次模型请求");
    const input = screen.getByText("累计输入").closest("article")!;
    expect(input).toHaveTextContent("1.6K");
    expect(input).toHaveTextContent("含缓存读取与写入 · 部分请求未报告");
    const output = screen.getByText("累计输出").closest("article")!;
    expect(output).toHaveTextContent("150");
    expect(output).toHaveTextContent("当前活动分支");
    expect(output).not.toHaveTextContent("部分请求未报告");
    const cost = screen.getByText("估算费用", { selector: ".usage-analysis-kpi > span" }).closest("article")!;
    expect(cost).toHaveTextContent("$0.0070");
    expect(cost).toHaveTextContent("USD · 按模型价格估算 · 部分请求未报告");
  });

  it("displays estimated and overflowing context without disguising it as cumulative input", async () => {
    const usage = stats();
    usage.metrics = { ...usage.metrics!, context_tokens: 12500, context_percent: 125, context_estimated: true };
    request.mockResolvedValue(report({ stats: usage }));
    render(<UsageAnalysisDrawer {...props} />);
    await screen.findByText("1 次模型请求");
    expect(screen.getByText("当前上下文 · 估算")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "当前上下文使用率" }))
      .toHaveAttribute("aria-valuetext", "125%");
    expect(screen.getByRole("progressbar", { name: "当前上下文使用率" })).toHaveAttribute("aria-valuenow", "100");
    expect(screen.getByText("12,500")).toBeInTheDocument();
  });

  it("displays an empty session without inventing trend data", async () => {
    request.mockResolvedValue(report({ stats: zeroStats(), requests: [] }));
    render(<UsageAnalysisDrawer {...props} />);
    expect(await screen.findByText("还没有模型请求记录")).toBeInTheDocument();
    expect(screen.queryByTestId("trend-chart")).not.toBeInTheDocument();
    expect(screen.getByText("累计输入").closest("article")!.querySelector("strong")).toHaveTextContent("0");
    expect(screen.getByText("估算费用").closest("article")!.querySelector("strong")).toHaveTextContent("$0.0000");
  });

  it("discards late results after changing the workspace even when session IDs match", async () => {
    const first = deferred<UsageAnalysisPayload>();
    const next = deferred<UsageAnalysisPayload>();
    request.mockReturnValueOnce(first.promise).mockReturnValueOnce(next.promise);
    const { rerender } = render(<UsageAnalysisDrawer {...props} />);
    rerender(<UsageAnalysisDrawer {...props} workspacePath="G:/next-project" />);
    await act(async () => next.resolve(report({ stats: stats(1777) })));
    await screen.findByTitle("1,777");
    await act(async () => first.resolve(report({ stats: stats(9999) })));
    expect(screen.queryByTitle("9,999")).not.toBeInTheDocument();
    expect(screen.getByTitle("1,777")).toBeInTheDocument();
  });

  it("ignores a mismatching session response and supports refreshing while running", async () => {
    request.mockResolvedValueOnce(report({ sessionId: "old-session" }));
    const firstStats = stats();
    const { rerender } = render(<UsageAnalysisDrawer {...props} stats={firstStats} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("用量记录与当前会话不匹配");
    rerender(<UsageAnalysisDrawer {...props} stats={stats(2000)} />);
    await screen.findByText("1 次模型请求");
    fireEvent.click(screen.getByRole("button", { name: "刷新用量分析" }));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
    rerender(<UsageAnalysisDrawer {...props} open={false} stats={stats(4000)} />);
    await act(async () => undefined);
    expect(request).toHaveBeenCalledTimes(3);
  });

  it("does not apply a request that finishes after closing", async () => {
    const pending = deferred<UsageAnalysisPayload>();
    request.mockReturnValueOnce(pending.promise);
    const { rerender } = render(<UsageAnalysisDrawer {...props} />);
    rerender(<UsageAnalysisDrawer {...props} open={false} />);
    await act(async () => pending.resolve(report({ stats: stats(9999) })));
    rerender(<UsageAnalysisDrawer {...props} />);
    await screen.findByText("1 次模型请求");
    expect(screen.queryByTitle("9,999")).not.toBeInTheDocument();
  });
});
