import type { TimelineCompactionStatus } from "../timeline/timelineState";

export interface ModelOption {
  id: string;
  name: string;
  provider: string;
  current: boolean;
  input?: string[];
}

export interface CommandOption {
  name: string;
  label: string;
  description: string;
}

export type PendingPlanAction = "execute" | "handoff" | "submit" | null;

export const PLAN_SUBMISSION_PROMPT =
  "请基于当前讨论整理一份决策完备的实施计划，并仅通过 submit_plan 控制工具提交；不要执行计划。";

export const COMPACTION_LABELS: Record<TimelineCompactionStatus, string> = {
  running: "正在压缩上下文…",
  completed: "上下文压缩完成",
  skipped: "未执行上下文压缩",
  failed: "上下文压缩失败",
  aborted: "上下文压缩已取消",
};

export function shortPath(value: string): string {
  const normalized = value.replaceAll("\\", "/");
  const parts = normalized.split("/").filter(Boolean);
  return parts.slice(-2).join("/") || value;
}

export function workspaceName(value: string): string {
  const normalized = value.replaceAll("\\", "/");
  const parts = normalized.split("/").filter(Boolean);
  return parts.at(-1) || value;
}
