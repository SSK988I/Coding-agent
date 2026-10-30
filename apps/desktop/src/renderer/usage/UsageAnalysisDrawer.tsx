import { Component, lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Alert, Button, ConfigProvider, Drawer, Empty, Segmented, Skeleton, Table, Tabs, Tag } from "antd";
import zhCN from "antd/locale/zh_CN";
import type { TableColumnsType } from "antd";
import type { SessionStats, UsageAnalysisPayload, UsageAnalysisRequest } from "../../shared/types";
import { UsageSummary } from "./UsageSummary";
import { moneyText, percentText, timeText, tokenText } from "./usageFormat";
import "./usageAnalysis.css";

export interface UsageAnalysisDrawerProps {
  open: boolean;
  sessionId: string;
  workspacePath: string;
  stats?: SessionStats;
  onClose: () => void;
  onAfterClose?: () => void;
}

const stopLabels: Record<string, { label: string; color?: string }> = {
  stop: { label: "已结束" }, tool_use: { label: "工具调用" }, length: { label: "达到输出上限", color: "gold" },
  error: { label: "失败", color: "red" }, aborted: { label: "已中止", color: "orange" },
};

const columns: TableColumnsType<UsageAnalysisRequest> = [
  { title: "请求", dataIndex: "index", width: 62, render: (value: number) => <span className="usage-analysis-request-index">#{value}</span> },
  { title: "模型 / 时间", key: "model", width: 180, render: (_value, row) => <div className="usage-analysis-model">
    <strong title={row.model}>{row.model || "未记录模型"}</strong><span>{row.provider ? `${row.provider} · ` : ""}{timeText(row.timestamp)}</span>
  </div> },
  { title: "输入（含缓存）", dataIndex: "inputTokens", align: "right", width: 118, render: (value: unknown) => tokenText(value) },
  { title: "输出", dataIndex: "outputTokens", align: "right", width: 88, render: (value: unknown) => tokenText(value) },
  { title: "缓存命中", dataIndex: "cacheHitRate", align: "right", width: 95, render: percentText },
  { title: "估算费用", dataIndex: "cost", align: "right", width: 105, render: moneyText },
  { title: "状态", dataIndex: "stopReason", width: 110, render: (reason: string) => {
    const status = stopLabels[reason] ?? { label: reason || "未记录" };
    return <Tag color={status.color}>{status.label}</Tag>;
  } },
];

class ChartBoundary extends Component<{ children: ReactNode; onRetry: () => void }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed ? <Alert type="error" title="图表暂时无法加载"
      action={<Button size="small" onClick={this.props.onRetry}>重试图表</Button>} /> : this.props.children;
  }
}

function ChartSkeleton() {
  return <div className="usage-analysis-chart-skeleton" role="status" aria-label="正在加载图表">
    <span aria-hidden="true" /><span aria-hidden="true" /><span aria-hidden="true" />
  </div>;
}

export default function UsageAnalysisDrawer({ open, sessionId, workspacePath, stats, onClose, onAfterClose }: UsageAnalysisDrawerProps) {
  const [analysis, setAnalysis] = useState<UsageAnalysisPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [tab, setTab] = useState("trend");
  const [metric, setMetric] = useState<"tokens" | "cost">("tokens");
  const [chartAttempt, setChartAttempt] = useState(0);
  const charts = useMemo(() => ({
    Trend: lazy(() => import("./UsageCharts").then((module) => ({ default: module.UsageTrendChart }))),
    Cache: lazy(() => import("./UsageCharts").then((module) => ({ default: module.CacheCompositionChart }))),
  }), [chartAttempt]);
  const latestOpen = useRef(open);
  const latestAfterClose = useRef(onAfterClose);
  const closeCompleted = useRef(!open);
  latestOpen.current = open;
  latestAfterClose.current = onAfterClose;
  if (open) closeCompleted.current = false;
  const notifyClosed = useCallback(() => {
    if (latestOpen.current || closeCompleted.current) return;
    closeCompleted.current = true;
    latestAfterClose.current?.();
  }, []);
  const contentRef = useCallback((node: HTMLDivElement | null) => {
    // destroyOnHidden detaches this content after closing. Ant Design can miss
    // its motion callback when the drawer closes with a suspended chart, so
    // use the actual detach as a second, idempotent completion signal.
    if (node === null) notifyClosed();
  }, [notifyClosed]);
  const generation = useRef(0);
  const route = `${workspacePath}\u0000${sessionId}`;
  const dataRoute = useRef<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const request = ++generation.current;
    if (dataRoute.current !== route) { setAnalysis(null); setTab("trend"); setMetric("tokens"); }
    setLoading(true);
    setError(null);
    void window.agent.request<UsageAnalysisPayload>("session.usage", { sessionId }).then((payload) => {
      if (generation.current !== request) return;
      if (payload.sessionId !== sessionId || !Array.isArray(payload.requests) || !payload.stats) {
        throw new Error("用量记录与当前会话不匹配，请刷新重试。");
      }
      dataRoute.current = route;
      setAnalysis(payload);
    }).catch((reason: unknown) => {
      if (generation.current === request) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => { if (generation.current === request) setLoading(false); });
    return () => { generation.current++; };
  }, [open, route, sessionId, stats, refresh]);

  const retry = useCallback(() => setRefresh((value) => value + 1), []);
  const current = dataRoute.current === route ? analysis : null;
  const requestDetails = useMemo(() => current ? [...current.requests].reverse() : [], [current]);
  const trendsActive = open && tab === "trend";

  return <ConfigProvider locale={zhCN} button={{ autoInsertSpace: false }} theme={{ token: {
    colorPrimary: "#71826f", colorText: "#242321", colorTextSecondary: "#77736b", colorBorder: "#dedbd4",
    colorBgContainer: "#ffffff", colorBgElevated: "#fbfaf8", borderRadius: 8,
    fontFamily: 'Inter, "Segoe UI", "Microsoft YaHei", sans-serif', fontSize: 13,
  }, components: { Table: { headerBg: "#f4f3f0", rowHoverBg: "#faf9f6" }, Tabs: { titleFontSize: 13 } } }}>
    <Drawer open={open} onClose={onClose} title="会话用量分析" placement="right" size="min(920px, 100vw)"
      rootClassName="usage-analysis-drawer" destroyOnHidden
      afterOpenChange={(visible) => { if (!visible) notifyClosed(); }}
      closable={{ placement: "end", "aria-label": "关闭用量分析" }}
      styles={{ body: { padding: 24 }, header: { padding: "20px 24px", borderBottomColor: "#e7e5e0" }, footer: { padding: "13px 24px" } }}
      extra={<Button size="small" onClick={retry} loading={loading} aria-label="刷新用量分析">刷新</Button>}
      footer={<span className="usage-analysis-footnote">仅统计当前活动分支 · 费用为估算值，不是供应商账单 · 未报告的数据以 — 表示</span>}>
      <div ref={contentRef} data-testid="usage-analysis-drawer" className="usage-analysis-content" aria-busy={loading}>
        <div className="usage-analysis-intro">
          <p>看清每一次模型请求的用量与开销。</p>
          <span>{current ? `${current.requests.length} 次模型请求` : "当前活动分支"}{loading && current ? " · 正在更新…" : ""}</span>
        </div>
        {error && <Alert type="error" showIcon title="暂时无法读取用量分析" description={error}
          action={<Button size="small" onClick={retry}>重试</Button>} className="usage-analysis-error" />}
        <UsageSummary stats={current?.stats ?? stats} requests={current?.requests} />
        {!current && loading && <div role="status" aria-label="正在加载用量分析"><Skeleton active paragraph={{ rows: 7 }} /></div>}
        {current && <>
          {current.requests.length === 0 ? <div className="usage-analysis-empty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有模型请求记录" /><p>发送消息后，可在这里查看用量与费用。</p></div>
            : <Tabs activeKey={tab} onChange={setTab} destroyOnHidden className="usage-analysis-tabs" items={[
              { key: "trend", label: "用量趋势", children: <ChartBoundary key={chartAttempt}
                onRetry={() => setChartAttempt((value) => value + 1)}><div className="usage-analysis-overview">
                <section className="usage-analysis-panel">
                  <div className="usage-analysis-panel-heading"><div><h3>每次请求</h3><p>一次用户任务可能包含多次模型请求。</p></div>
                    <Segmented size="small" value={metric} onChange={(value) => setMetric(value as "tokens" | "cost")}
                      options={[{ label: "Token 用量", value: "tokens" }, { label: "估算费用", value: "cost" }]} />
                  </div>
                  <Suspense fallback={<ChartSkeleton />}>
                    <charts.Trend requests={current.requests} metric={metric} active={trendsActive} />
                  </Suspense>
                </section>
                <section className="usage-analysis-panel usage-analysis-cache-panel">
                  <div className="usage-analysis-panel-heading"><div><h3>输入构成</h3><p>缓存同样占用上下文容量。</p></div></div>
                  <Suspense fallback={<ChartSkeleton />}>
                    <charts.Cache stats={current.stats} active={trendsActive} />
                  </Suspense>
                </section>
              </div></ChartBoundary> },
              { key: "requests", label: "请求明细", children: <div className="usage-analysis-detail">
                <Table<UsageAnalysisRequest> dataSource={requestDetails} columns={columns} rowKey="id" size="small"
                  scroll={{ x: 760 }} pagination={{ pageSize: 10, showSizeChanger: false, hideOnSinglePage: true,
                    showTotal: (total) => `共 ${total} 次模型请求` }} />
                <p className="usage-analysis-table-note">输入已包含缓存读取和写入；失败或中止的请求也保留已报告的用量。</p>
              </div> },
            ]} />}
        </>}
      </div>
    </Drawer>
  </ConfigProvider>;
}
