import { useEffect, useMemo, useRef } from "react";
import { Empty } from "antd";
import { init, use, type ComposeOption, type EChartsType } from "echarts/core";
import { BarChart, LineChart, PieChart, type BarSeriesOption, type LineSeriesOption, type PieSeriesOption } from "echarts/charts";
import {
  DataZoomComponent, GridComponent, LegendComponent, TooltipComponent,
  type DataZoomComponentOption, type GridComponentOption, type LegendComponentOption, type TooltipComponentOption,
} from "echarts/components";
import { SVGRenderer } from "echarts/renderers";
import type { SessionStats, UsageAnalysisRequest } from "../../shared/types";
import { known, moneyText, tokenText } from "./usageFormat";

use([BarChart, LineChart, PieChart, GridComponent, LegendComponent, TooltipComponent, DataZoomComponent, SVGRenderer]);

type ChartOption = ComposeOption<BarSeriesOption | LineSeriesOption | PieSeriesOption
  | GridComponentOption | LegendComponentOption | TooltipComponentOption | DataZoomComponentOption>;

const palette = { input: "#7c9080", output: "#7e91aa", cache: "#718d78", write: "#c1a16b", uncached: "#b6b3aa" };

/** Initialize after layout, including when a tab or drawer becomes visible. */
export function UsageChart({ option, label, active = true, className = "" }: {
  option: ChartOption; label: string; active?: boolean; className?: string;
}) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<EChartsType | null>(null);
  const latestOption = useRef(option);
  latestOption.current = option;

  useEffect(() => {
    const element = container.current;
    if (!active || !element) return;
    let disposed = false;
    const fit = () => {
      if (disposed) return;
      if (!element.clientWidth || !element.clientHeight) return;
      if (!chart.current) {
        chart.current = init(element, undefined, { renderer: "svg" });
        chart.current.setOption(latestOption.current, { notMerge: true });
      } else chart.current.resize();
    };
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fit);
    observer?.observe(element);
    window.addEventListener("resize", fit);
    const frame = requestAnimationFrame(fit);
    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      observer?.disconnect();
      window.removeEventListener("resize", fit);
      chart.current?.dispose();
      chart.current = null;
    };
  }, [active]);

  useEffect(() => {
    chart.current?.setOption(option, { notMerge: true });
  }, [option]);

  return <div ref={container} className={`usage-analysis-chart ${className}`} data-testid="usage-analysis-chart"
    role="img" aria-label={label} />;
}

export function UsageTrendChart({ requests, metric, active }: {
  requests: UsageAnalysisRequest[]; metric: "tokens" | "cost"; active: boolean;
}) {
  const hasData = requests.some((row) => metric === "cost" ? known(row.cost)
    : known(row.inputTokens) || known(row.outputTokens));
  const option = useMemo<ChartOption>(() => ({
    animation: false,
    textStyle: { fontFamily: 'Inter, "Segoe UI", "Microsoft YaHei", sans-serif', color: "#65625d" },
    grid: { left: 16, right: 20, top: 40, bottom: requests.length > 30 ? 58 : 24, containLabel: true },
    legend: { top: 0, left: 8, icon: "roundRect", itemWidth: 12, itemHeight: 8, textStyle: { color: "#65625d" } },
    tooltip: { trigger: "axis", axisPointer: { type: metric === "tokens" ? "shadow" : "line" },
      valueFormatter: (value) => metric === "cost" ? moneyText(value) : `${tokenText(value)} tokens` },
    xAxis: { type: "category", data: requests.map((row) => `#${row.index}`),
      axisLine: { lineStyle: { color: "#dedbd4" } }, axisTick: { show: false },
      axisLabel: { color: "#766f65", fontSize: 11 }, boundaryGap: metric === "tokens" },
    yAxis: { type: "value", min: 0, axisLabel: { color: "#766f65", fontSize: 11,
      formatter: (value: number) => metric === "cost"
        ? `$${value > 0 && value < .0001 ? value.toExponential(1) : value < .01 ? value.toFixed(4) : value.toFixed(2)}`
        : tokenText(value, true) },
      splitLine: { lineStyle: { color: "#efede8", type: "dashed" } } },
    ...(requests.length > 30 ? { dataZoom: [{ type: "inside", start: Math.max(0, 100 - 3000 / requests.length), end: 100 },
      { type: "slider", height: 16, bottom: 4, borderColor: "#e7e5e0", handleSize: "85%", showDetail: false }] } : {}),
    series: metric === "tokens" ? [
      { name: "输入（含缓存）", type: "bar", stack: "tokens", barMaxWidth: 34,
        itemStyle: { color: palette.input }, data: requests.map((row) => row.inputTokens) },
      { name: "输出", type: "bar", stack: "tokens", barMaxWidth: 34,
        itemStyle: { color: palette.output, borderRadius: [3, 3, 0, 0] }, data: requests.map((row) => row.outputTokens) },
    ] : [{ name: "单次估算费用", type: "line", showSymbol: requests.length < 50, symbolSize: 6,
      connectNulls: false, lineStyle: { width: 2 }, itemStyle: { color: palette.write },
      data: requests.map((row) => row.cost) }],
  }), [requests, metric]);

  if (!hasData) return <div className="usage-analysis-chart-empty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
    description={metric === "cost" ? "暂无已记录的费用用量" : "暂无已记录的 Token 用量"} /></div>;
  return <UsageChart option={option} active={active}
    label={metric === "cost" ? "每次模型请求的估算费用折线图" : "每次模型请求的输入与输出 Token 堆叠柱状图"} />;
}

export function CacheCompositionChart({ stats, active }: { stats: SessionStats; active: boolean }) {
  const complete = known(stats.metrics?.cache_hit_rate);
  const values = [stats.tokens.cache_read, stats.tokens.cache_write, stats.tokens.input];
  const available = complete && values.every(known) && values.reduce((sum, value) => sum + value, 0) > 0;
  const option = useMemo<ChartOption>(() => ({
    animation: false,
    tooltip: { trigger: "item", valueFormatter: (value) => `${tokenText(value)} tokens` },
    series: [{ name: "输入构成", type: "pie", radius: ["58%", "80%"], center: ["50%", "50%"],
      avoidLabelOverlap: true, label: { show: false }, labelLine: { show: false },
      itemStyle: { borderRadius: 3, borderColor: "#fff", borderWidth: 3 },
      data: [{ name: "缓存读取", value: values[0], itemStyle: { color: palette.cache } },
        { name: "缓存写入", value: values[1], itemStyle: { color: palette.write } },
        { name: "未缓存输入", value: values[2], itemStyle: { color: palette.uncached } }] }],
  }), [values[0], values[1], values[2]]);

  if (!available) return <div className="usage-analysis-cache-empty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
    description="暂无完整的缓存统计" /><span>未报告的用量保留为未知。</span></div>;
  return <div className="usage-analysis-composition">
    <UsageChart option={option} active={active} className="usage-analysis-donut" label="缓存读取、缓存写入与未缓存输入的构成环形图" />
    <ul className="usage-analysis-composition-legend">
      {[['缓存读取', palette.cache], ['缓存写入', palette.write], ['未缓存输入', palette.uncached]].map(([name, color], index) =>
        <li key={name}><span><i style={{ background: color }} />{name}</span><strong>{tokenText(values[index])}</strong></li>)}
    </ul>
  </div>;
}
