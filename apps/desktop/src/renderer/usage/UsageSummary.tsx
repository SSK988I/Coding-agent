import type { SessionStats, UsageAnalysisRequest } from "../../shared/types";
import { known, moneyText, percentText, tokenText } from "./usageFormat";
import "./usageSummary.css";

/** Shared by the immediate loading shell and the fully loaded analysis drawer. */
export function UsageSummary({ stats, requests }: {
  stats?: SessionStats;
  requests?: UsageAnalysisRequest[];
}) {
  const context = stats?.metrics;
  const reported = { inputTokens: 0, outputTokens: 0, cost: 0 };
  for (const request of requests ?? []) {
    for (const field of ["inputTokens", "outputTokens", "cost"] as const) {
      if (known(request[field])) reported[field]++;
    }
  }
  const measured = (field: keyof typeof reported, value: number | null | undefined) =>
    requests && requests.length > 0 && reported[field] === 0 ? null : value;
  const detail = (field: keyof typeof reported, text: string) =>
    requests && reported[field] > 0 && reported[field] < requests.length ? `${text} · 部分请求未报告` : text;
  const input = measured("inputTokens", context?.input_tokens);
  const output = measured("outputTokens", stats?.tokens.output);
  const cost = measured("cost", stats?.cost);
  const used = context?.context_tokens;
  const capacity = context?.context_window;
  const percent = context?.context_percent;
  const pressure = known(percent) && percent >= 90 ? "critical" : known(percent) && percent >= 70 ? "warning" : "normal";
  const cards = [
    { label: "累计输入", value: tokenText(input, true), detail: detail("inputTokens", "含缓存读取与写入"), exact: tokenText(input) },
    { label: "累计输出", value: tokenText(output, true), detail: detail("outputTokens", "当前活动分支"), exact: tokenText(output) },
    { label: "缓存命中率", value: percentText(context?.cache_hit_rate), detail: "按输入 Token 加权", exact: percentText(context?.cache_hit_rate) },
    { label: "估算费用", value: moneyText(cost), detail: detail("cost", "USD · 按模型价格估算"), exact: moneyText(cost) },
  ];
  return <>
    <div className="usage-analysis-kpis">{cards.map((card) => <article key={card.label} className="usage-analysis-kpi">
      <span>{card.label}</span><strong title={card.exact}>{card.value}</strong><small>{card.detail}</small>
    </article>)}</div>
    <section className={`usage-analysis-context ${pressure}`} aria-label="当前上下文容量">
      <div><span>当前上下文{context?.context_estimated ? " · 估算" : ""}</span>
        <strong>{tokenText(used)} <span>/ {tokenText(capacity)} tokens</span><b>{percentText(percent)}</b></strong></div>
      <div className="usage-analysis-context-track" role="progressbar" aria-label="当前上下文使用率" aria-valuemin={0} aria-valuemax={100}
        aria-valuenow={known(percent) ? Math.min(100, percent) : undefined}
        aria-valuetext={known(percent) ? percentText(percent) : "未知"}>
        <span aria-hidden="true" style={{ width: `${known(percent) ? Math.min(100, percent) : 0}%` }} />
      </div>
      <p>上下文用量会随压缩变化，不等于累计输入。</p>
    </section>
  </>;
}
