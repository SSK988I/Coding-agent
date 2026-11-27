import { memo, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Monitor } from "lucide-react";
import type { TimelineApproval, TimelineToolItem } from "../../timeline/timelineState";
import { ToolCard, classifyToolName, type ToolCardProps } from "../tool-card";

export function inferGroupLabel(tools: readonly TimelineToolItem[]): string {
  let hasRead = false;
  let hasWrite = false;
  let hasShell = false;
  let hasWeb = false;
  let hasOther = false;

  for (const tool of tools) {
    const normalized = tool.name.trim().toLowerCase();
    if (normalized === "web_search") {
      hasWeb = true;
      continue;
    }
    const category = classifyToolName(tool.name);
    if (category === "write") hasWrite = true;
    else if (category === "shell") hasShell = true;
    else if (category === "read" || category === "search") hasRead = true;
    else hasOther = true;
  }

  if (hasWrite && (hasRead || hasShell || hasWeb)) return "检查工作区并应用修改";
  if (hasWrite) return "编辑工作区文件";
  if (hasWeb && !hasRead && !hasShell && !hasOther) return "联网检索信息";
  if (hasShell && (hasRead || hasWeb)) return "检查工作区并运行命令";
  if (hasShell && !hasRead && !hasOther) return "运行工作区命令";
  if (hasRead && !hasOther) return "查阅工作区文件";
  return "执行工作区操作";
}

export function isToolErrorStatus(status: TimelineToolItem["status"]): boolean {
  return status === "error" || status === "blocked" || status === "timed_out";
}

export const TimelineToolGroupHeader = memo(function TimelineToolGroupHeader({
  groupId,
  label,
  toolCount,
  hasApproval,
  hasRunning,
  hasError,
  errorCount,
  isOpen,
  onToggle,
}: {
  groupId: string;
  label: string;
  toolCount: number;
  hasApproval: boolean;
  hasRunning: boolean;
  hasError: boolean;
  errorCount: number;
  isOpen: boolean;
  onToggle: (groupId: string) => void;
}) {
  return (
    <header
      className={`thread-group-row thread-group-row--header thread-node ${isOpen ? "thread-group-row--open" : ""}`.trim()}
      data-testid="tool-group"
    >
      <button
        type="button"
        className="thread-trigger thread-group__toggle"
        aria-expanded={isOpen}
        onClick={() => {
          if (!hasApproval) onToggle(groupId);
        }}
      >
        <span className="thread-trigger__icon" aria-hidden="true">
          <Monitor size={15} strokeWidth={1.55} />
        </span>
        <span className="thread-trigger__label">{label}</span>
        {!isOpen && toolCount > 1 && (
          <span className="thread-group__count">· {toolCount} 步</span>
        )}
        {hasRunning && (
          <span className="tool-card-view__inline-status">
            <span className="status-dot status-dot--pulse" aria-hidden="true" />
            执行中
          </span>
        )}
        {!hasRunning && hasApproval && (
          <span className="tool-card-view__inline-status tool-card-view__status--approval">
            <span className="status-dot status-dot--warn" aria-hidden="true" />
            等待审批
          </span>
        )}
        {hasError && (
          <span className="tool-card-view__inline-status tool-card-view__status--error">
            <span className="status-dot status-dot--err" aria-hidden="true" />
            {errorCount > 1 ? `${errorCount} 项失败` : "失败"}
          </span>
        )}
        <span className="thread-trigger__chevron" aria-hidden="true">
          {isOpen ? <ChevronDown size={14} strokeWidth={1.5} /> : <ChevronRight size={14} strokeWidth={1.5} />}
        </span>
      </button>
    </header>
  );
});

export const TimelineToolGroupEntry = memo(function TimelineToolGroupEntry({
  tool,
  railToneClass,
  isFirst,
  cwd,
  onResolveApproval,
  onRecoverEdit,
}: {
  tool: TimelineToolItem;
  railToneClass: string;
  isFirst: boolean;
  onRecoverEdit?: ToolCardProps["onRecoverEdit"];
  cwd?: string;
  onResolveApproval?: (approval: TimelineApproval, approved: boolean) => void | Promise<void>;
}) {
  return (
    <div
      className={`thread-group-row thread-group-row--item ${isFirst ? "thread-group-row--first" : ""}`.trim()}
      data-testid="tool-group-item"
    >
      <div className={`thread-rail-segment ${railToneClass}`.trim()}>
        <ToolCard
          tool={tool}
          cwd={cwd}
          onResolveApproval={onResolveApproval}
          onRecoverEdit={onRecoverEdit}
          defaultCollapsed
          nested
        />
      </div>
    </div>
  );
});

export const TimelineToolGroupFooter = memo(function TimelineToolGroupFooter({
  label,
  railToneClass,
}: {
  label: string;
  railToneClass: string;
}) {
  return (
    <div className="thread-group-row thread-group-row--footer" aria-hidden="true">
      <div className={`thread-rail-segment ${railToneClass}`.trim()}>
        <div className="thread-group__echo">
          <span className="thread-trigger__icon">
            <Monitor size={14} strokeWidth={1.5} />
          </span>
          <span>{label}</span>
        </div>
      </div>
    </div>
  );
});

export const TimelineToolGroup = memo(function TimelineToolGroup({
  tools,
  cwd,
  onResolveApproval,
}: {
  tools: readonly TimelineToolItem[];
  cwd?: string;
  onResolveApproval?: (approval: TimelineApproval, approved: boolean) => void | Promise<void>;
}) {
  const [expanded, setExpanded] = useState(true);
  const autoExpandedForErrorRef = useRef(false);
  const label = useMemo(() => inferGroupLabel(tools), [tools]);

  const hasApproval = tools.some((tool) => Boolean(tool.approval) || tool.status === "approval");
  const hasRunning = tools.some((tool) => tool.status === "running" || tool.status === "pending");
  const errorCount = tools.filter((tool) => isToolErrorStatus(tool.status)).length;
  const hasError = errorCount > 0;

  useEffect(() => {
    if (hasError && !autoExpandedForErrorRef.current) {
      autoExpandedForErrorRef.current = true;
      setExpanded(true);
    }
  }, [hasError]);

  const isOpen = expanded || hasApproval;

  const railToneClass = hasApproval
    ? "thread-rail--warn"
    : hasError
      ? "thread-rail--err"
      : "";

  return (
    <section className="thread-group thread-node" data-testid="tool-group">
      <header className="thread-group__header">
        <button
          type="button"
          className="thread-trigger thread-group__toggle"
          aria-expanded={isOpen}
          onClick={() => {
            if (!hasApproval) setExpanded((value) => !value);
          }}
        >
          <span className="thread-trigger__icon" aria-hidden="true">
            <Monitor size={15} strokeWidth={1.55} />
          </span>
          <span className="thread-trigger__label">{label}</span>
          {!isOpen && tools.length > 1 && (
            <span className="thread-group__count">· {tools.length} 步</span>
          )}
          {hasRunning && (
            <span className="tool-card-view__inline-status">
              <span className="status-dot status-dot--pulse" aria-hidden="true" />
              执行中
            </span>
          )}
          {!hasRunning && hasApproval && (
            <span className="tool-card-view__inline-status tool-card-view__status--approval">
              <span className="status-dot status-dot--warn" aria-hidden="true" />
              等待审批
            </span>
          )}
          {hasError && (
            <span className="tool-card-view__inline-status tool-card-view__status--error">
              <span className="status-dot status-dot--err" aria-hidden="true" />
              {errorCount > 1 ? `${errorCount} 项失败` : "失败"}
            </span>
          )}
          <span className="thread-trigger__chevron" aria-hidden="true">
            {isOpen ? <ChevronDown size={14} strokeWidth={1.5} /> : <ChevronRight size={14} strokeWidth={1.5} />}
          </span>
        </button>
      </header>

      {isOpen && (
        <div className={`thread-rail thread-rail--l1 ${railToneClass}`.trim()}>
          {tools.map((tool) => (
            <ToolCard
              key={tool.id}
              tool={tool}
              cwd={cwd}
              onResolveApproval={onResolveApproval}
              defaultCollapsed
              nested
            />
          ))}
          <div className="thread-group__echo" aria-hidden="true">
            <span className="thread-trigger__icon">
              <Monitor size={14} strokeWidth={1.5} />
            </span>
            <span>{label}</span>
          </div>
        </div>
      )}
    </section>
  );
});
