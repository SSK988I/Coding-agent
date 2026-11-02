import { useId, useState } from "react";
import type { TurnChangesSummary } from "../../shared/workbench";
import "./turnChangesCard.css";

/** Review the recorded net change for this turn, never the current worktree. */
export function TurnChangesCard({ change, onView }: {
  change: TurnChangesSummary;
  onView: (runId: string, path?: string) => void;
}) {
  const [expandedRun, setExpandedRun] = useState<string | null>(null);
  const listId = useId();
  const expanded = expandedRun === change.runId;
  if (!change.files.length) return null;
  const shown = expanded ? change.files : change.files.slice(0, 3);
  const hasTextChanges = change.files.some((file) => !file.binary);

  return <section className="turn-changes-card" aria-label={`本轮变更 ${change.files.length} 个文件`}>
    <header className="turn-changes-heading">
      <span className="turn-changes-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M6 3h8l4 4v14H6z M14 3v5h4 M9 13h6 M12 10v6" />
        </svg>
      </span>
      <div className="turn-changes-title">
        <h3>本轮变更 · {change.files.length} 个文件</h3>
        {hasTextChanges ? <span className="turn-changes-counts" aria-label={`新增 ${change.additions} 行，删除 ${change.deletions} 行`}>
          <span className="turn-changes-add">+{change.additions}</span>
          <span className="turn-changes-remove">−{change.deletions}</span>
        </span> : <span className="turn-changes-binary">二进制文件变更</span>}
      </div>
      <button type="button" className="turn-changes-view" onClick={() => onView(change.runId)}>
        查看变更<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M3 8h9 M8 4l4 4-4 4" /></svg>
      </button>
    </header>
    <ul className="turn-changes-files" id={listId}>{shown.map((file) => {
      const separator = Math.max(file.path.lastIndexOf("/"), file.path.lastIndexOf("\\"));
      const filename = file.path.slice(separator + 1);
      const directory = file.path.slice(0, separator + 1);
      return <li key={file.path}>
      <button type="button" className="turn-changes-file" aria-label={`查看 ${file.path} 的变更`}
        onClick={() => onView(change.runId, file.path)}>
        <span className="turn-changes-path" title={file.path}>
          <span className="turn-changes-filename">{filename}</span>
          {directory && <span className="turn-changes-directory">{directory}</span>}
        </span>
        {file.binary ? <span className="turn-changes-binary">二进制</span>
          : <span className="turn-changes-counts"><span className="turn-changes-add">+{file.additions}</span>
            <span className="turn-changes-remove">−{file.deletions}</span></span>}
      </button>
    </li>;
    })}</ul>
    {change.files.length > 3 && <button type="button" className="turn-changes-expand"
      aria-controls={listId} aria-expanded={expanded} onClick={() => setExpandedRun(expanded ? null : change.runId)}>
      {expanded ? "收起文件" : `再显示 ${change.files.length - 3} 个文件`}
      <svg className={expanded ? "expanded" : ""} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
        <path d="m4 6 4 4 4-4" />
      </svg>
    </button>}
    {change.truncated && <p className="turn-changes-notice">变更预览不完整，部分文件或内容未显示。</p>}
  </section>;
}
