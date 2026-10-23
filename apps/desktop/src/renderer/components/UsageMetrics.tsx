import type { SessionStats } from "../../shared/types";

const percentage = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
const exactTokens = new Intl.NumberFormat("en-US");
const shortTokens = new Intl.NumberFormat("en-US", {
  notation: "compact", maximumFractionDigits: 1,
});

function known(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function tokens(value: number | null | undefined): string {
  return known(value) ? shortTokens.format(value) : "—";
}

function cost(value: number | undefined): string {
  if (!known(value)) return "—";
  if (value > 0 && value < 0.0001) return "<$0.0001";
  return `$${value.toFixed(4)}`;
}

/** Present server-derived metrics without inferring context size or cache policy. */
export function UsageMetrics({ stats }: { stats?: SessionStats }) {
  const metrics = stats?.metrics;
  const cacheHit = metrics?.cache_hit_rate;
  const contextPercent = metrics?.context_percent;
  const contextTokens = metrics?.context_tokens;
  const contextWindow = metrics?.context_window;
  const hasContextPercent = known(contextPercent);
  const contextValue = hasContextPercent ? `${percentage.format(contextPercent)}%` : "—";
  const contextDetail = `${known(contextTokens) ? exactTokens.format(contextTokens) : "—"} / ${known(contextWindow) ? exactTokens.format(contextWindow) : "—"} tokens`;
  const contextDescription = `${contextDetail}，${contextValue}${metrics?.context_estimated ? "，估算值" : ""}`;
  const pressure = hasContextPercent && contextPercent >= 90 ? "critical"
    : hasContextPercent && contextPercent >= 70 ? "warning" : "normal";

  return (
    <section className="usage-metrics" aria-label="会话用量指标">
      <span className="usage-metric" title="按当前分支累计输入计算：缓存读取 tokens / 全部输入 tokens（含缓存读取与写入）。— 表示尚无用量或供应商未完整提供缓存统计。">
        <span>缓存命中</span>
        <strong data-testid="cache-hit-rate">{known(cacheHit) ? `${percentage.format(cacheHit)}%` : "—"}</strong>
      </span>
      <span className={`usage-metric usage-context ${pressure}`} title={`上下文容量：${contextDescription}。不是会话累计 tokens；压缩后会下降。`}>
        <span>上下文{metrics?.context_estimated ? "≈" : ""}</span>
        <strong data-testid="context-usage">{tokens(contextTokens)} / {tokens(contextWindow)} · {contextValue}</strong>
        <span className="usage-context-track" role="progressbar" aria-label="上下文容量使用率"
          aria-valuemin={0} aria-valuemax={100}
          aria-valuenow={hasContextPercent ? Math.min(100, contextPercent) : undefined}
          aria-valuetext={contextDescription}>
          <span style={{ width: `${hasContextPercent ? Math.min(100, contextPercent) : 0}%` }} />
        </span>
      </span>
      <span className="usage-metric" title="当前活动分支累计输入 tokens，包含缓存读取与写入。">
        <span>输入</span><strong data-testid="input-tokens">{tokens(metrics?.input_tokens)}</strong>
      </span>
      <span className="usage-metric" title="当前活动分支累计输出 tokens。">
        <span>输出</span><strong data-testid="output-tokens">{tokens(stats?.tokens?.output)}</strong>
      </span>
      <span className="usage-metric" title="按模型价格累计估算的美元费用；不是供应商账单，不包含未上报的工具等费用。">
        <span>估算费用</span><strong data-testid="usage-cost">{cost(stats?.cost)}</strong>
      </span>
    </section>
  );
}
