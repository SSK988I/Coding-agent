import { ChevronDown } from "lucide-react";
import type { SessionInfo, WorkspacePayload } from "../../../shared/types";
import { SessionList } from "../SessionList";
import { SubagentPanel } from "../SubagentPanel";

export interface SidebarProps {
  workspace: WorkspacePayload | null;
  sessions: SessionInfo[];
  showArchivedSessions: boolean;
  sessionsLoading: boolean;
  sessionListError: string | null;
  pendingSessionNavigation: string | null;
  pendingSessionArchive: string | null;
  disabledSessions: boolean;
  disabledWorkspaceControls: boolean;
  running: boolean;
  compacting: boolean;
  sessionNavigating: boolean;
  onToggleArchivedSessions: () => void;
  onNewSession: () => void;
  onOpenSession: (id: string) => void;
  onArchiveSession: (id: string, archived: boolean) => void;
  onRetrySessions: () => void;
  onCancelSubagent: (taskId: string) => Promise<void>;
  onReviewPlanWithSubagent?: () => Promise<void>;
  onOpenModelPicker: () => void;
  onToggleMemory: () => void;
  onToggleAutoExtract: () => void;
  onToggleWebSearch: () => void;
  onOpenMemoryDrawer: () => void;
}

export function Sidebar({
  workspace,
  sessions,
  showArchivedSessions,
  sessionsLoading,
  sessionListError,
  pendingSessionNavigation,
  pendingSessionArchive,
  disabledSessions,
  disabledWorkspaceControls,
  running,
  compacting,
  sessionNavigating,
  onToggleArchivedSessions,
  onNewSession,
  onOpenSession,
  onArchiveSession,
  onRetrySessions,
  onCancelSubagent,
  onReviewPlanWithSubagent,
  onOpenModelPicker,
  onToggleMemory,
  onToggleAutoExtract,
  onToggleWebSearch,
  onOpenMemoryDrawer,
}: SidebarProps) {
  const totalMemories = (workspace?.memory.globalCount ?? 0) + (workspace?.memory.projectCount ?? 0);
  const supportsImages = workspace?.model.input?.includes("image") ?? false;

  return (
    <aside className="sidebar">
      <SessionList
        sessions={sessions}
        archived={showArchivedSessions}
        activeId={workspace?.sessionId}
        disabled={disabledSessions}
        loading={sessionsLoading}
        error={sessionListError}
        pendingNavigation={pendingSessionNavigation}
        pendingArchive={pendingSessionArchive}
        onToggleArchive={onToggleArchivedSessions}
        onNew={onNewSession}
        onOpen={onOpenSession}
        onArchive={onArchiveSession}
        onRetry={onRetrySessions}
      />

      {workspace && (
        <SubagentPanel
          key={workspace.sessionId}
          tasks={workspace.subagents ?? []}
          disabled={sessionNavigating || Boolean(workspace.archived) || Boolean(pendingSessionArchive)}
          onCancel={onCancelSubagent}
          onReview={onReviewPlanWithSubagent}
        />
      )}

      {workspace && (
        <div className="workspace-meta">
          <button
            type="button"
            className="workspace-model-button"
            onClick={onOpenModelPicker}
            disabled={disabledWorkspaceControls || Boolean(workspace.archived)}
            title="点击切换模型"
          >
            <span className="workspace-model-button__copy">
              <strong>{workspace.model.name || workspace.model.id}</strong>
              <small>
                {workspace.model.provider} · {workspace.tools.length} 个已启用
                {supportsImages ? " · 支持图片" : ""}
              </small>
            </span>
            <ChevronDown size={13} strokeWidth={1.6} aria-hidden="true" />
          </button>

          <div className={`workspace-quick-grid ${workspace.webSearch ? "" : "workspace-quick-grid--single"}`}>
            {workspace.webSearch && (
              <button
                type="button"
                className="workspace-quick-tile"
                onClick={onToggleWebSearch}
                disabled={running || compacting || sessionNavigating}
                aria-pressed={workspace.webSearch.enabled}
              >
                <span className="workspace-quick-tile__label">联网</span>
                <span className="workspace-quick-tile__value">
                  <span className={`status-dot ${workspace.webSearch.enabled ? "status-dot--ok" : ""}`} aria-hidden="true" />
                  {workspace.webSearch.enabled ? "开" : "关"}
                </span>
              </button>
            )}

            <button
              type="button"
              className="workspace-quick-tile"
              onClick={onOpenMemoryDrawer}
              disabled={disabledWorkspaceControls && !running}
              title="打开记忆详情与冲突管理"
            >
              <span className="workspace-quick-tile__label">记忆</span>
              <span className="workspace-quick-tile__value">{totalMemories} 条</span>
            </button>
          </div>

          <div className="workspace-meta__memory-grid">
            <button
              type="button"
              className={`memory-toggle ${workspace.memory.enabled ? "on" : "off"}`}
              onClick={onToggleMemory}
              disabled={disabledWorkspaceControls}
              aria-pressed={workspace.memory.enabled}
            >
              <span className={`status-dot ${workspace.memory.enabled ? "status-dot--ok" : ""}`.trim()} aria-hidden="true" />
              <span>{workspace.memory.enabled ? "已开启" : "已关闭"}</span>
            </button>
            <button
              type="button"
              className={`memory-toggle ${workspace.memory.enabled && workspace.memory.autoExtractEnabled !== false ? "on" : "off"}`}
              onClick={onToggleAutoExtract}
              disabled={disabledWorkspaceControls || !workspace.memory.enabled}
              aria-pressed={workspace.memory.enabled && workspace.memory.autoExtractEnabled !== false}
            >
              <span
                className={`status-dot ${workspace.memory.enabled && workspace.memory.autoExtractEnabled !== false ? "status-dot--ok" : ""}`.trim()}
                aria-hidden="true"
              />
              <span>{workspace.memory.autoExtractEnabled === false ? "自动提取关闭" : "自动提取开启"}</span>
            </button>
          </div>

          <div className="workspace-meta__queue">
            <strong>
              待处理 {workspace.memory.pendingCount ?? 0} · 处理中 {workspace.memory.processingCount ?? 0}
              {" · "}已完成 {workspace.memory.readyCount ?? 0} · 失败 {workspace.memory.failedCount ?? 0}
            </strong>
          </div>
        </div>
      )}
    </aside>
  );
}
