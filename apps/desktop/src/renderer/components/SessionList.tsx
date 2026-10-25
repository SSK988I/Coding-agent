import type { SessionInfo } from "../../shared/types";

export function ArchiveIcon({ restore = false }: { restore?: boolean }) {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M4 8h16v12H4zM3 4h18v4H3z" />
    {restore ? <path d="m9 14 3-3 3 3m-3-3v6" /> : <path d="M9 12h6" />}
  </svg>;
}

interface Props {
  sessions: SessionInfo[];
  archived: boolean;
  activeId?: string;
  disabled: boolean;
  loading: boolean;
  error: string | null;
  pendingNavigation: string | null;
  pendingArchive: string | null;
  onToggleArchive: () => void;
  onNew: () => void;
  onOpen: (id: string) => void;
  onArchive: (id: string, archived: boolean) => void;
  onRetry: () => void;
}

export function SessionList(props: Props) {
  return <>
    <div className="sidebar-heading">
      <span>{props.archived ? "已归档" : "会话"}</span>
      <div className="session-heading-actions">
        <button type="button" className="icon-button" onClick={props.onToggleArchive}
          title={props.archived ? "返回会话" : "查看已归档会话"}
          aria-label={props.archived ? "返回会话" : "查看已归档会话"}
          aria-pressed={props.archived} disabled={props.disabled}>
          {props.archived ? "‹" : <ArchiveIcon />}
        </button>
        <button type="button" className="icon-button" onClick={props.onNew} title="新建会话"
          aria-label="新建会话" disabled={props.disabled}>
          {props.pendingNavigation === "new" ? <span className="session-navigation-spinner" aria-hidden="true" /> : "＋"}
        </button>
      </div>
    </div>
    {props.pendingNavigation && <div className="session-navigation-status" role="status" aria-live="polite">
      <span className="session-navigation-spinner" aria-hidden="true" />
      {props.pendingNavigation === "new" ? "正在新建会话…" : "正在切换会话…"}
    </div>}
    <div className="session-list" aria-label={props.archived ? "已归档会话" : "会话列表"}
      aria-busy={props.loading}>
      {props.error && <div className="empty-sidebar" role="alert">{props.error}
        <button type="button" className="session-retry" onClick={props.onRetry}>重试</button>
      </div>}
      {props.sessions.map((session) => {
        const label = session.name || session.first_message || "新会话";
        return <div className="session-row" key={session.id} role="group" aria-label={label}>
          <button type="button" className={`session-item ${session.id === props.activeId ? "active" : ""}`}
            onClick={() => props.onOpen(session.id)} disabled={props.disabled}
            aria-current={session.id === props.activeId ? "true" : undefined}
            aria-busy={props.pendingNavigation === session.id}>
            <strong>{label}</strong>
            <span>{props.pendingNavigation === session.id ? "正在打开…" : `${session.message_count} 条消息`}</span>
          </button>
          <button type="button" className="session-archive-action"
            aria-label={props.archived ? "恢复会话" : "归档会话"}
            title={`${props.archived ? "恢复" : "归档"}会话：${label}`} disabled={props.disabled}
            onClick={() => props.onArchive(session.id, !props.archived)}>
            {props.pendingArchive === session.id ? <span className="session-navigation-spinner" aria-hidden="true" />
              : <ArchiveIcon restore={props.archived} />}
          </button>
        </div>;
      })}
      {!props.error && !props.sessions.length && <div className="empty-sidebar">
        {props.loading ? "正在加载会话…" : props.archived ? "暂无已归档会话。" : "发送第一条消息后，会话会保存到 JSONL。"}
      </div>}
    </div>
  </>;
}
