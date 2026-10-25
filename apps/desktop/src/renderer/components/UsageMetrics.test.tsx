import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { SessionStats } from "../../shared/types";
import { UsageMetrics } from "./UsageMetrics";

export function usageStats(overrides: Partial<NonNullable<SessionStats["metrics"]>> = {}): SessionStats {
  return {
    tokens: { input: 200, output: 350, cache_read: 800, cache_write: 0, total: 1350 },
    cost: 0.0123,
    metrics: {
      cache_hit_rate: 80, input_tokens: 1000, context_tokens: 20000,
      context_window: 100000, context_percent: 20, context_estimated: false,
      ...overrides,
    },
  };
}

afterEach(cleanup);

describe("desktop session usage metrics", () => {
  it("shows unknown metrics before the backend reports usage", () => {
    render(<UsageMetrics />);
    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("—");
    expect(screen.getByTestId("context-usage")).toHaveTextContent("— / — · —");
    expect(screen.getByTestId("input-tokens")).toHaveTextContent("—");
    expect(screen.getByTestId("output-tokens")).toHaveTextContent("—");
    expect(screen.getByTestId("usage-cost")).toHaveTextContent("—");
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
  });

  it("preserves measured zero values instead of treating them as missing", () => {
    render(<UsageMetrics stats={{
      tokens: { input: 0, output: 0, cache_read: 0, cache_write: 0, total: 0 }, cost: 0,
      metrics: { cache_hit_rate: 0, input_tokens: 0, context_tokens: 0,
        context_window: 100000, context_percent: 0, context_estimated: false },
    }} />);
    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("0%");
    expect(screen.getByTestId("input-tokens")).toHaveTextContent("0");
    expect(screen.getByTestId("output-tokens")).toHaveTextContent("0");
    expect(screen.getByTestId("usage-cost")).toHaveTextContent("$0.0000");
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");
  });

  it("uses authoritative cache percentages rather than deriving them from token totals", () => {
    render(<UsageMetrics stats={usageStats({ cache_hit_rate: 62.25 })} />);
    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("62.3%");
    expect(screen.getByTestId("input-tokens")).toHaveTextContent("1K");
    expect(screen.getByTestId("output-tokens")).toHaveTextContent("350");
    expect(screen.getByTestId("usage-cost")).toHaveTextContent("$0.0123");
    expect(screen.getByText("缓存命中").parentElement).toHaveAttribute("title", expect.stringContaining("供应商未完整提供缓存统计"));
  });

  it("marks context estimates and exposes detailed accessible progress", () => {
    render(<UsageMetrics stats={usageStats({ context_estimated: true })} />);
    expect(screen.getByText("上下文≈")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "上下文容量使用率" }))
      .toHaveAttribute("aria-valuetext", "20,000 / 100,000 tokens，20%，估算值");
  });

  it.each([[69, "normal"], [70, "warning"], [89, "warning"], [90, "critical"]] as const)
    ("warns at context thresholds (%s percent)", (percent, pressure) => {
      render(<UsageMetrics stats={usageStats({ context_percent: percent })} />);
      expect(screen.getByTestId("context-usage").parentElement).toHaveClass(pressure);
    });

  it("shows overflow without hiding the true percentage and clamps only the progress indicator", () => {
    render(<UsageMetrics stats={usageStats({ context_percent: 125, context_tokens: 125000 })} />);
    expect(screen.getByTestId("context-usage")).toHaveTextContent("125K / 100K · 125%");
    const progress = screen.getByRole("progressbar");
    expect(progress).toHaveAttribute("aria-valuenow", "100");
    expect(progress).toHaveAttribute("aria-valuetext", "125,000 / 100,000 tokens，125%");
    expect(progress.firstElementChild).toHaveStyle({ width: "100%" });
  });

  it("does not invent missing metrics from an older backend", () => {
    const { metrics: _metrics, ...legacy } = usageStats();
    render(<UsageMetrics stats={legacy} />);
    expect(screen.getByTestId("cache-hit-rate")).toHaveTextContent("—");
    expect(screen.getByTestId("context-usage")).toHaveTextContent("— / — · —");
    expect(screen.getByTestId("output-tokens")).toHaveTextContent("350");
  });

  it("does not round very small nonzero estimated cost down to free", () => {
    render(<UsageMetrics stats={{ ...usageStats(), cost: 0.000001 }} />);
    expect(screen.getByTestId("usage-cost")).toHaveTextContent("<$0.0001");
  });
});
