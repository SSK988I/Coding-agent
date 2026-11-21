import { memo } from "react";
import {
  Brain,
  ChevronDown,
  Moon,
  PanelLeft,
  PanelRight,
  Sun,
} from "lucide-react";
import type { WorkspacePayload } from "../../../shared/types";
import type { DesktopTheme } from "../../hooks/useTheme";
import { shortPath, workspaceName } from "../../utils/format";

export interface TitlebarProps {
  workspace: WorkspacePayload | null;
  activeSessionName?: string | null;
  sidecarStatus: string;
  statusLabel: string;
  sidebarOpen: boolean;
  workbenchOpen: boolean;
  memoryDrawerOpen: boolean;
  planBadgeLabel: string;
  planStatusActive: boolean;
  theme: DesktopTheme;
  busyWorkspaceActions: boolean;
  workspaceSwitching?: boolean;
  onToggleSidebar: () => void;
  onToggleWorkbench: () => void;
  onToggleMemoryDrawer: () => void;
  onToggleTheme: () => void;
  onChooseWorkspace: () => void;
}

export const Titlebar = memo(function Titlebar({
  workspace,
  activeSessionName,
  sidecarStatus,
  statusLabel,
  sidebarOpen,
  workbenchOpen,
  memoryDrawerOpen,
  planBadgeLabel,
  planStatusActive,
  theme,
  busyWorkspaceActions,
  workspaceSwitching = false,
  onToggleSidebar,
  onToggleWorkbench,
  onToggleMemoryDrawer,
  onToggleTheme,
  onChooseWorkspace,
}: TitlebarProps) {
  const isOnline = sidecarStatus === "ready";
  const conflictCount = workspace?.memory.conflictCount ?? 0;
  const isPlan = workspace?.collaborationMode === "plan" || planStatusActive;

  return (
    <header className="titlebar">
      <button
        type="button"
        className="titlebar-icon-btn sidebar-toggle"
        onClick={onToggleSidebar}
        title={sidebarOpen ? "收起侧边栏" : "展开侧边栏"}
        aria-label={sidebarOpen ? "收起侧边栏" : "展开侧边栏"}
        aria-pressed={sidebarOpen}
      >
        <PanelLeft size={15} strokeWidth={1.55} />
      </button>

      <button
        type="button"
        className="titlebar-workspace-btn"
        onClick={onChooseWorkspace}
        disabled={busyWorkspaceActions}
        aria-label="打开项目"
        title={workspace ? `切换工作区：${workspace.path}` : "打开项目"}
      >
        <span className="brand-mark" aria-hidden="true">CA</span>
        <span className="brand-copy">
          <strong>{workspace ? workspaceName(workspace.path) : "Coding Agent"}</strong>
          <span>{workspace ? `/ ${shortPath(workspace.path)}` : "未打开工作区"}</span>
        </span>
        <ChevronDown size={12} strokeWidth={1.65} className="titlebar-workspace-chevron" aria-hidden="true" />
      </button>

      <div className="titlebar-center">
        {workspace && (
          <div className="titlebar-mode-pill">
            <span
              className={`status-dot ${
                workspace.planState.phase === "recovery_error"
                  ? "status-dot--err"
                  : isPlan
                    ? "status-dot--warn"
                    : ""
              }`.trim()}
              aria-hidden="true"
            />
            <span>
              {workspace.archived
                ? "已归档"
                : isPlan
                  ? planBadgeLabel
                  : "Default 协作模式"}
            </span>
          </div>
        )}
        {activeSessionName && (
          <span className="titlebar-session-name" title={activeSessionName}>
            {activeSessionName}
          </span>
        )}
      </div>

      <div className="titlebar-right">
        <div className="runtime-indicator" title={statusLabel}>
          <div className={`runtime-dot ${isOnline ? "online" : ""}`} />
          <span className="runtime-label">{statusLabel}</span>
        </div>

        <span className="titlebar-divider" aria-hidden="true" />

        <button
          type="button"
          className={`ghost-button titlebar-btn ${memoryDrawerOpen ? "active" : ""}`}
          aria-label="管理长期记忆"
          aria-expanded={memoryDrawerOpen}
          onClick={onToggleMemoryDrawer}
          disabled={!workspace || workspaceSwitching}
          title="打开长期记忆管理面板"
        >
          <Brain size={14} strokeWidth={1.55} aria-hidden="true" />
          <span>记忆</span>
          {conflictCount > 0 && <span className="status-dot status-dot--warn" aria-hidden="true" />}
        </button>

        <button
          type="button"
          className={`ghost-button titlebar-btn ${workbenchOpen ? "active" : ""}`}
          aria-label="打开工具栏"
          aria-expanded={workbenchOpen}
          onClick={onToggleWorkbench}
          disabled={!workspace}
        >
          <PanelRight size={14} strokeWidth={1.55} aria-hidden="true" />
          <span>工具栏</span>
        </button>

        <button
          type="button"
          className="titlebar-icon-btn"
          onClick={onToggleTheme}
          title={theme === "light" ? "切换到深色主题" : "切换到浅色主题"}
          aria-label={theme === "light" ? "切换到深色主题" : "切换到浅色主题"}
        >
          {theme === "light" ? <Moon size={15} strokeWidth={1.55} /> : <Sun size={15} strokeWidth={1.55} />}
        </button>
      </div>
    </header>
  );
});
