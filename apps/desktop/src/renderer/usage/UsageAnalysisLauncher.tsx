import { Component, lazy, Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { SessionStats } from "../../shared/types";
import { UsageMetrics } from "../components/UsageMetrics";
import { UsageSummary } from "./UsageSummary";

function LoadingPanel({ failed = false, stats, onClose, onRetry }: {
  failed?: boolean; stats?: SessionStats; onClose: () => void; onRetry: () => void;
}) {
  return <div className="usage-loader-mask" onClick={onClose}>
    <section className="usage-loader-panel" role="dialog" aria-modal="true" aria-labelledby="usage-loader-title"
      onClick={(event) => event.stopPropagation()} onKeyDown={(event) => {
        if (event.key === "Escape") { event.stopPropagation(); onClose(); }
        if (event.key !== "Tab") return;
        const buttons = event.currentTarget.querySelectorAll<HTMLButtonElement>("button");
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }}>
      <header><h2 id="usage-loader-title">会话用量分析</h2>
        <button type="button" autoFocus aria-label="关闭用量分析" onClick={onClose}>×</button>
      </header>
      <div className="usage-loader-message" role={failed ? "alert" : "status"}>
        <p>{failed ? "暂时无法打开用量分析，请重试。" : "正在加载用量分析…"}</p>
        {failed && <button type="button" onClick={onRetry}>重新加载</button>}
      </div>
      <div className="usage-loader-content">
        <UsageSummary stats={stats} />
        {!failed && <div className="usage-analysis-skeleton" aria-hidden="true">
          <div className="usage-analysis-skeleton-tabs"><span /><span /></div>
          <div className="usage-analysis-chart-skeleton"><span /><span /><span /></div>
        </div>}
      </div>
    </section>
  </div>;
}

class DrawerLoadBoundary extends Component<{
  open: boolean; stats?: SessionStats; onClose: () => void; onRetry: () => void; children: ReactNode;
}, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed
      ? this.props.open && <LoadingPanel failed stats={this.props.stats} onClose={this.props.onClose} onRetry={this.props.onRetry} />
      : this.props.children;
  }
}

/** Key by workspace/session at the call site so navigation closes the old drawer. */
export function UsageAnalysisLauncher({ sessionId, workspacePath, stats, onOpenChange }: {
  sessionId?: string; workspacePath?: string; stats?: SessionStats; onOpenChange?: (open: boolean) => void;
}) {
  const [requested, setRequested] = useState(false);
  const [open, setOpen] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const triggerHost = useRef<HTMLDivElement>(null);
  const notifyOpen = useRef(onOpenChange);
  notifyOpen.current = onOpenChange;
  useEffect(() => () => notifyOpen.current?.(false), []);
  // Neither Ant Design nor ECharts is imported until the first deliberate open.
  const Drawer = useMemo(() => lazy(() => import("./UsageAnalysisDrawer")), [attempt]);
  const restoreFocus = () => {
    triggerHost.current?.querySelector<HTMLButtonElement>("[aria-label='查看用量分析']")?.focus();
  };
  const finishClose = () => { notifyOpen.current?.(false); restoreFocus(); };
  const closeLoading = () => { setOpen(false); finishClose(); };
  const retry = () => setAttempt((current) => current + 1);
  return <div className="session-usage" ref={triggerHost}>
    <UsageMetrics stats={stats} onOpenAnalysis={sessionId && workspacePath ? () => {
      setRequested(true);
      setOpen(true);
      notifyOpen.current?.(true);
    } : undefined} />
    {requested && sessionId && workspacePath && <DrawerLoadBoundary key={attempt} open={open} stats={stats}
      onClose={closeLoading} onRetry={retry}>
      <Suspense fallback={open ? <LoadingPanel stats={stats} onClose={closeLoading} onRetry={retry} /> : null}>
        <Drawer open={open} sessionId={sessionId} workspacePath={workspacePath} stats={stats}
          onClose={() => setOpen(false)} onAfterClose={finishClose} />
      </Suspense>
    </DrawerLoadBoundary>}
  </div>;
}
