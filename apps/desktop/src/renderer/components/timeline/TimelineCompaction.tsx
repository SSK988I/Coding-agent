import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Minimize2 } from "lucide-react";
import type { TimelineCompactionItem } from "../../timeline/timelineState";
import { COMPACTION_LABELS } from "../../utils/format";

const Markdown = memo(ReactMarkdown);
const markdownPlugins = [remarkGfm];

export const TimelineCompactionCard = memo(function TimelineCompactionCard({
  compaction,
}: { compaction: TimelineCompactionItem }) {
  const railTone = compaction.status === "failed"
    ? "thread-rail--err"
    : compaction.status === "running" ? "thread-rail--warn" : "";

  return (
    <section
      className={`compaction-card ${compaction.status} thread-node`}
      data-testid="compaction-card"
    >
      <div className="compaction-card-header">
        <span className="thread-trigger__icon compaction-icon" aria-hidden="true">
          <Minimize2 size={14} strokeWidth={1.55} />
        </span>
        <strong>{COMPACTION_LABELS[compaction.status]}</strong>
        {typeof compaction.tokensBefore === "number" && (
          <span className="compaction-meta">压缩前约 {compaction.tokensBefore.toLocaleString()} tokens</span>
        )}
        {compaction.status === "running" && <span className="compaction-spinner" aria-hidden="true" />}
      </div>
      {(compaction.status === "running" || compaction.summary || compaction.detail) && (
        <div className={`thread-rail ${railTone}`.trim()}>
          <div className="thread-box thread-box--subtle compaction-card__body">
            {compaction.status === "running" && (
              <p>{compaction.reason === "pivot"
                ? "正在面向下一阶段整理当前上下文，历史记录仍会保留。"
                : "正在整理较早的对话并生成可恢复摘要，请稍候。"}</p>
            )}
            {compaction.summary && compaction.reason !== "persisted" && <p>{compaction.summary}</p>}
            {compaction.summary && compaction.reason === "persisted" && (
              <details>
                <summary>查看压缩摘要</summary>
                <div className="compaction-summary">
                  <Markdown remarkPlugins={markdownPlugins}>{compaction.summary}</Markdown>
                </div>
              </details>
            )}
            {compaction.detail && <p className="compaction-detail">{compaction.detail}</p>}
          </div>
        </div>
      )}
    </section>
  );
});
