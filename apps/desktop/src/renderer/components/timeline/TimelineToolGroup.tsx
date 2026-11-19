import { memo, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Monitor } from "lucide-react";
import type { TimelineApproval, TimelineToolItem } from "../../timeline/timelineState";
import { ToolCard, classifyToolName } from "../tool-card";

function inferGroupLabel(tools: readonly TimelineToolItem[]): string {
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
  const label = useMemo(() => inferGroupLabel(tools), [tools]);

  const hasApproval = tools.some((tool) => Boolean(tool.approval) || tool.status === "approval");
  const hasRunning = tools.some((tool) => tool.status === "running" || tool.status === "pending");
  const hasError = tools.some((tool) => tool.status === "error" || tool.status === "blocked" || tool.status === "timed_out");
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
          onClick={() => setExpanded((value) => !value)}
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
