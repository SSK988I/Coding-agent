import { FormEvent, KeyboardEvent, memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import remarkGfm from "remark-gfm";
import type {
  RuntimeEvent,
  PlanRevisionPayload,
  PlanStatePayload,
  SessionSnapshotPayload,
  SessionInfo,
  WorkspacePayload,
} from "../shared/types";
import { ToolCard } from "./components/tool-card";
import {
  createTimelineState,
  createTimelineStateFromMessages,
  reduceRuntimeEventBatch,
  selectTimelineItems,
  type TimelineApproval,
  type TimelineCompactionItem,
  type TimelineCompactionStatus,
  type TimelineItem,
  type TimelineMessageItem,
  type TimelineState,
  type TimelineToolItem,
} from "./timeline/timelineState";

interface ModelOption {
  id: string;
  name: string;
  provider: string;
  current: boolean;
}

interface CommandOption {
  name: string;
  label: string;
  description: string;
}

type PendingPlanAction = "execute" | "handoff" | null;

const COMMAND_ICONS: Record<string, string> = {
  help: "?",
  new: "+",
  model: "◇",
  compact: "⌁",
  clear: "↺",
  session: "◷",
  memory: "◎",
};

function shortPath(value: string): string {
  const normalized = value.replaceAll("\\", "/");
  const parts = normalized.split("/").filter(Boolean);
  return parts.slice(-2).join("/") || value;
}

interface PlanTimelineItem {
  id: string;
  kind: "plan";
  order: number;
  plan: PlanRevisionPayload;
}

type RenderTimelineItem = TimelineItem | PlanTimelineItem;

const COMPACTION_LABELS: Record<TimelineCompactionStatus, string> = {
  running: "正在压缩上下文…",
  completed: "上下文压缩完成",
  skipped: "未执行上下文压缩",
  failed: "上下文压缩失败",
  aborted: "上下文压缩已取消",
};

const TimelineMessageCard = memo(function TimelineMessageCard({
  message,
}: { message: TimelineMessageItem }) {
  return (
    <article className={`message ${message.role}`}>
      <div className="message-avatar">{message.role === "user" ? "你" : "AI"}</div>
      <div className="message-body">
        {message.thinking && (
          <details className="thinking-block">
            <summary>思考过程</summary>
            <pre>{message.thinking}</pre>
          </details>
        )}
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {message.text || (message.status === "streaming" ? "正在思考…" : "")}
        </ReactMarkdown>
      </div>
    </article>
  );
});

const TimelineCompactionCard = memo(function TimelineCompactionCard({
  compaction,
}: { compaction: TimelineCompactionItem }) {
  return (
    <section
      className={`compaction-card ${compaction.status}`}
      data-testid="compaction-card"
    >
      <div className="compaction-card-header">
        <span className="compaction-icon">⌁</span>
        <strong>{COMPACTION_LABELS[compaction.status]}</strong>
        {compaction.status === "running" && <span className="compaction-spinner" aria-hidden="true" />}
      </div>
      {compaction.status === "running" && <p>正在整理较早的对话并生成可恢复摘要，请稍候。</p>}
      {compaction.summary && compaction.reason !== "persisted" && <p>{compaction.summary}</p>}
      {compaction.summary && compaction.reason === "persisted" && (
        <details>
          <summary>查看压缩摘要</summary>
          <div className="compaction-summary">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{compaction.summary}</ReactMarkdown>
          </div>
        </details>
      )}
      {typeof compaction.tokensBefore === "number" && (
        <span className="compaction-meta">压缩前约 {compaction.tokensBefore.toLocaleString()} tokens</span>
      )}
      {compaction.detail && <p className="compaction-detail">{compaction.detail}</p>}
    </section>
  );
});

const TimelinePlanCard = memo(function TimelinePlanCard({ plan, badge }: { plan: PlanRevisionPayload; badge: string }) {
  return (
    <section className="plan-card" data-testid="plan-card">
      <div className="plan-card-header">
        <span className="plan-badge">{badge}</span>
        <strong>{plan.title}</strong>
        <span>revision {plan.revision} · {plan.digest.slice(0, 12)}</span>
      </div>
      <div className="plan-markdown">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{plan.markdown}</ReactMarkdown>
      </div>
    </section>
  );
});

function tokenizeCommandArguments(value: string): string[] {
  const tokens: string[] = [];
  let current = "";
  let quote: "'" | '"' | null = null;
  let tokenStarted = false;
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index];
    if (character === "\\" && quote !== "'") {
      if (index + 1 >= value.length) throw new Error("命令参数不能以转义符结尾");
      current += value[index + 1];
      tokenStarted = true;
      index += 1;
      continue;
    }
    if (quote !== null) {
      if (character === quote) quote = null;
      else current += character;
      tokenStarted = true;
      continue;
    }
    if (character === "'" || character === '"') {
      quote = character;
      tokenStarted = true;
      continue;
    }
    if (/\s/.test(character)) {
      if (tokenStarted) {
        tokens.push(current);
        current = "";
        tokenStarted = false;
      }
      continue;
    }
    current += character;
    tokenStarted = true;
  }
  if (quote !== null) throw new Error("记忆命令参数的引号未闭合");
  if (tokenStarted) tokens.push(current);
  return tokens;
}

function appendTimelineItem(
  state: TimelineState,
  createItem: (order: number) => TimelineItem,
): TimelineState {
  const order = state.lastOrder + 1;
  const item = createItem(order);
  const exists = Boolean(state.entities[item.id]);
  return {
    ...state,
    entities: { ...state.entities, [item.id]: item },
    orderedIds: exists ? state.orderedIds : [...state.orderedIds, item.id],
    lastOrder: Math.max(order, item.order),
    activeCompactionId: item.kind === "compaction" && item.status === "running"
      ? item.id
      : state.activeCompactionId,
  };
}

function updateTimelineItem(
  state: TimelineState,
  id: string,
  update: (item: TimelineItem) => TimelineItem,
): TimelineState {
  const item = state.entities[id];
  if (!item) return state;
  const next = update(item);
  if (next === item) return state;
  return {
    ...state,
    entities: { ...state.entities, [id]: next },
    activeCompactionId: next.kind === "compaction" && next.status !== "running"
      && state.activeCompactionId === id
      ? null
      : state.activeCompactionId,
  };
}

export function App() {
  const [sidecarStatus, setSidecarStatus] = useState("starting");
  const [workspace, setWorkspace] = useState<WorkspacePayload | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [timelineState, setTimelineState] = useState<TimelineState>(() => createTimelineState());
  const [commandOptions, setCommandOptions] = useState<CommandOption[]>([]);
  const [commandMenuOpen, setCommandMenuOpen] = useState(false);
  const [selectedCommandIndex, setSelectedCommandIndex] = useState(0);
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([]);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [modelQuery, setModelQuery] = useState("");
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const [compacting, setCompacting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [customPlanAnswer, setCustomPlanAnswer] = useState("");
  const [supplementingPlanDigest, setSupplementingPlanDigest] = useState<string | null>(null);
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const [pendingPlanAction, setPendingPlanAction] = useState<PendingPlanAction>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const didBootstrap = useRef(false);
  const rafQueue = useRef<RuntimeEvent[]>([]);
  const rafId = useRef<number | null>(null);
  const localItemSequence = useRef(0);
  const activeCompactionId = useRef<string | null>(null);
  const compactingRef = useRef(false);
  const workspaceSessionIdRef = useRef<string | null>(null);

  const refreshSessions = async () => {
    try {
      setSessions(await window.agent.request<SessionInfo[]>("session.list"));
    } catch {
      setSessions([]);
    }
  };

  const refreshCommands = async () => {
    try {
      setCommandOptions(await window.agent.request<CommandOption[]>("command.list"));
    } catch {
      setCommandOptions([]);
    }
  };

  const applyWorkspace = (payload: WorkspacePayload, restoreTimeline = true) => {
    workspaceSessionIdRef.current = payload.sessionId;
    setWorkspace(payload);
    if (restoreTimeline) {
      setTimelineState(createTimelineStateFromMessages(payload.messages, payload.sessionId));
    }
    setCommandMenuOpen(false);
    setModelPickerOpen(false);
    setCustomPlanAnswer("");
    setSupplementingPlanDigest(null);
    setPendingPlanAction(null);
    activeCompactionId.current = null;
    compactingRef.current = false;
    setCompacting(false);
    setIsAtBottom(true);
    setError(null);
    void refreshSessions();
    void refreshCommands();
  };

  const applySessionSnapshot = (snapshot: SessionSnapshotPayload) => {
    if (workspaceSessionIdRef.current !== snapshot.sessionId) return;
    setWorkspace((current) => {
      if (!current) return current;
      return {
        ...current,
        collaborationMode: snapshot.collaborationMode,
        planState: snapshot.planState,
        messages: snapshot.messages,
      };
    });
    setTimelineState(createTimelineStateFromMessages(snapshot.messages, snapshot.sessionId));
  };

  const refreshSessionSnapshot = async () => {
    const snapshot = await window.agent.request<SessionSnapshotPayload>("session.snapshot");
    applySessionSnapshot(snapshot);
    return snapshot;
  };

  const beginCompaction = (reason: string): string => {
    if (compactingRef.current && activeCompactionId.current) {
      return activeCompactionId.current;
    }
    const timestamp = Date.now();
    const id = `compaction-${timestamp}-local-${++localItemSequence.current}`;
    activeCompactionId.current = id;
    compactingRef.current = true;
    setCompacting(true);
    setTimelineState((current) => appendTimelineItem(current, (order): TimelineCompactionItem => ({
      id,
      kind: "compaction",
      order,
      revision: 0,
      sessionId: workspace?.sessionId ?? null,
      runId: null,
      createdAt: timestamp,
      updatedAt: timestamp,
      status: "running",
      reason,
      summary: "",
    })));
    return id;
  };

  const finishCompaction = (
    status: Exclude<TimelineCompactionStatus, "running">,
    summary = "",
    detail?: string,
  ) => {
    const id = activeCompactionId.current;
    if (!id) return;
    compactingRef.current = false;
    setCompacting(false);
    setTimelineState((current) => updateTimelineItem(current, id, (item) => item.kind === "compaction"
      ? {
          ...item,
          status,
          summary: summary || item.summary,
          detail,
          revision: item.revision + 1,
          updatedAt: Date.now(),
        }
      : item));
  };

  const openWorkspace = async (path: string, resume = true) => {
    setError(null);
    try {
      const result = await window.agent.request<WorkspacePayload>("workspace.open", { path, resume });
      setSidecarStatus("ready");
      applyWorkspace(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  useEffect(() => {
    const unsubscribeStatus = window.agent.onStatus(setSidecarStatus);
    const unsubscribeEvents = window.agent.onEvent((event) => {
      rafQueue.current.push(event);
      if (rafId.current !== null) return;
      rafId.current = requestAnimationFrame(() => {
        const events = rafQueue.current.splice(0);
        rafId.current = null;
        // A session.changed snapshot is authoritative. Discard timeline events
        // before the last snapshot in this frame, then reduce only events that
        // belong to the newly selected session.
        let sessionChangeIndex = -1;
        for (let index = events.length - 1; index >= 0; index -= 1) {
          if (events[index].event.type === "session.changed") {
            sessionChangeIndex = index;
            break;
          }
        }
        if (sessionChangeIndex >= 0) {
          const changed = events[sessionChangeIndex];
          const snapshot = changed.event.payload as unknown as WorkspacePayload;
          setTimelineState(createTimelineStateFromMessages(snapshot.messages, snapshot.sessionId));
          const trailingEvents = events.slice(sessionChangeIndex + 1);
          if (trailingEvents.length) {
            setTimelineState((current) => reduceRuntimeEventBatch(current, trailingEvents));
          }
        } else {
          setTimelineState((current) => reduceRuntimeEventBatch(current, events));
        }
        for (const item of events) handleRuntimeSideEffects(item);
      });
    });
    if (!didBootstrap.current) {
      didBootstrap.current = true;
      window.desktop.getBootstrap()
        .then((bootstrap) => openWorkspace(bootstrap.defaultWorkspace, true))
        .catch((reason) => setError(String(reason)));
    }
    return () => {
      unsubscribeStatus();
      unsubscribeEvents();
      if (rafId.current !== null) cancelAnimationFrame(rafId.current);
    };
  }, []);

  const handleRuntimeSideEffects = (envelope: RuntimeEvent) => {
    const { type, payload } = envelope.event;
    if (type === "run.started") {
      setRunning(true);
      setPendingPlanAction(null);
      return;
    }
    if (type === "run.completed" || type === "run.cancelled" || type === "run.failed") {
      setRunning(false);
      setPendingPlanAction(null);
      if (type === "run.failed") setError(String(payload.message ?? "运行失败"));
      void refreshSessions();
      return;
    }
    if (type === "compaction_start") {
      compactingRef.current = true;
      setCompacting(true);
      return;
    }
    if (type === "compaction_end") {
      compactingRef.current = false;
      setCompacting(false);
      void refreshSessions();
      return;
    }
    if (type === "message_end") {
      const message = payload.message;
      if (message && typeof message === "object" && "error_message" in message) {
        const errorMessage = (message as { error_message?: unknown }).error_message;
        if (typeof errorMessage === "string" && errorMessage) setError(errorMessage);
      }
    }
    if (type === "session.changed") {
      applyWorkspace(payload as unknown as WorkspacePayload, false);
    }
    if (type === "model.changed") {
      const model = payload as unknown as WorkspacePayload["model"];
      setWorkspace((current) => current ? { ...current, model } : current);
    }
    if (type === "memory.changed") {
      setWorkspace((current) => current ? {
        ...current,
        memory: { ...current.memory, ...(payload as unknown as WorkspacePayload["memory"]) },
      } : current);
    }
    if (type === "plan_state_changed" || type === "plan.stateChanged") {
      const state = payload.state as PlanStatePayload | undefined;
      if (!state || typeof state.phase !== "string") return;
      const payloadSessionId = typeof payload.sessionId === "string"
        ? payload.sessionId
        : typeof payload.session_id === "string" ? payload.session_id : envelope.sessionId;
      const payloadMode = payload.collaboration_mode === "plan" ? "plan"
        : payload.collaboration_mode === "default" ? "default" : undefined;
      setWorkspace((current) => {
        if (!current || (payloadSessionId && payloadSessionId !== current.sessionId)) return current;
        return {
          ...current,
          collaborationMode: state.mode ?? payloadMode ?? current.collaborationMode,
          planState: state,
        };
      });
      if (state.phase !== "ready") setSupplementingPlanDigest(null);
      return;
    }
    // Granular Plan events remain on the wire for external older clients.
    // Built-in clients never reconstruct state from them.
  };

  const chooseWorkspace = async () => {
    const selected = await window.desktop.chooseWorkspace();
    if (selected) await openWorkspace(selected, true);
  };

  const addNotice = (text: string) => {
    const timestamp = Date.now();
    const id = `notice-${timestamp}-${++localItemSequence.current}`;
    setTimelineState((current) => appendTimelineItem(current, (order): TimelineMessageItem => ({
      id,
      kind: "message",
      order,
      revision: 0,
      sessionId: workspace?.sessionId ?? null,
      runId: null,
      createdAt: timestamp,
      updatedAt: timestamp,
      role: "assistant",
      text,
      thinking: "",
    })));
  };

  const openModelPicker = async (query = "") => {
    setModelQuery(query);
    setModelOptions(await window.agent.request<ModelOption[]>("model.list"));
    setModelPickerOpen(true);
  };

  const executeSlashCommand = async (name: string, args = "") => {
    setInput("");
    setCommandMenuOpen(false);
    setError(null);
    try {
      if (name === "model") {
        await openModelPicker(args);
        return;
      }
      if (name === "new") {
        applyWorkspace(await window.agent.request<WorkspacePayload>("session.new"));
        return;
      }
      if (name === "compact") {
        if (compactingRef.current) return;
        beginCompaction("manual");
        try {
          const result = await window.agent.request<Record<string, unknown>>("session.compact");
          if (result.performed) {
            finishCompaction("completed", String(result.summary_preview ?? ""));
          } else {
            const detail = String(result.error ?? "当前没有可压缩的上下文");
            const status: Exclude<TimelineCompactionStatus, "running" | "completed"> = detail === "Compaction aborted"
              ? "aborted"
              : detail === "Nothing to compact" || detail === "Already compacted"
                ? "skipped"
                : "failed";
            const localizedDetail = detail === "Nothing to compact"
              ? "当前没有足够的较早对话可供压缩"
              : detail === "Already compacted"
                ? "当前上下文已经压缩，无需重复执行"
                : detail === "Compaction aborted" ? "压缩已取消" : detail;
            finishCompaction(status, "", localizedDetail);
          }
        } catch (reason) {
          finishCompaction("failed", "", reason instanceof Error ? reason.message : String(reason));
          throw reason;
        }
        return;
      }
      if (name === "clear") {
        applyWorkspace(await window.agent.request<WorkspacePayload>("session.clear"));
        return;
      }
      if (name === "session") {
        const snapshot = await window.agent.request<Record<string, unknown>>("session.snapshot");
        addNotice(`### 当前会话\n\n\`\`\`json\n${JSON.stringify(snapshot.stats ?? {}, null, 2)}\n\`\`\``);
        return;
      }
      if (name === "help") {
        addNotice([
          "### 可用命令",
          "",
          ...commandOptions.map((command) => `- \`/${command.name}\` — ${command.description}`),
        ].join("\n"));
        return;
      }
      if (name === "plan") {
        await enterPlanMode();
        return;
      }
      if (name === "cancel-plan") {
        await cancelPlan();
        return;
      }
      if (name === "execute-plan") {
        await executePlan();
        return;
      }
      if (name === "memory") {
        const parts = tokenizeCommandArguments(args);
        const action = parts[0]?.toLocaleLowerCase() || "status";
        if (action === "on" || action === "off") {
          const memory = await window.agent.request<WorkspacePayload["memory"]>("memory.setEnabled", {
            enabled: action === "on",
          });
          setWorkspace((current) => current ? { ...current, memory } : current);
          addNotice(`长期记忆已${action === "on" ? "开启" : "关闭"}。`);
          return;
        }
        if (action === "auto") {
          const enabled = parts[1]?.toLocaleLowerCase();
          if (parts.length !== 2 || (enabled !== "on" && enabled !== "off")) {
            addNotice("用法：`/memory auto on|off`");
            return;
          }
          const memory = await window.agent.request<WorkspacePayload["memory"]>("memory.setAutoExtract", {
            enabled: enabled === "on",
          });
          setWorkspace((current) => current ? { ...current, memory } : current);
          addNotice(`长期记忆自动提取已${enabled === "on" ? "开启" : "关闭"}。`);
          return;
        }
        if (action === "status") {
          const memory = await window.agent.request<WorkspacePayload["memory"]>("memory.status");
          setWorkspace((current) => current ? { ...current, memory } : current);
          addNotice([
            "### 长期记忆状态", "",
            `- 状态：\`${memory.enabled ? "on" : "off"}\``,
            `- 自动提取：\`${memory.autoExtractEnabled === false ? "off" : "on"}\``,
            `- 用户：\`${memory.userId ?? "none"}\``,
            `- 项目：\`${memory.projectId ?? "none"}\``,
            `- 全局记忆：\`${memory.globalCount ?? 0}\``,
            `- 项目记忆：\`${memory.projectCount ?? 0}\``,
            `- 待解决冲突：\`${memory.conflictCount ?? 0}\``,
            `- 提取任务：待处理 \`${memory.pendingCount ?? 0}\` / 处理中 \`${memory.processingCount ?? 0}\` / 已完成 \`${memory.readyCount ?? 0}\` / 失败 \`${memory.failedCount ?? 0}\``,
            ...(memory.lastError ? [`- 最近错误：\`${memory.lastError}\``] : []),
            `- 目录：\`${memory.root ?? ""}\``,
          ].join("\n"));
          return;
        }
        if (action === "remember") {
          const values = parts.slice(1);
          const delimiter = values.indexOf("--");
          const optionSide = delimiter >= 0 ? values.slice(0, delimiter) : [...values];
          const literalSide = delimiter >= 0 ? values.slice(delimiter + 1) : [];
          const scopeFlags: Record<string, "global" | "project"> = {
            "--global": "global", "--project": "project",
          };
          let scope: "global" | "project" = "global";
          const trailing = optionSide.at(-1);
          if (trailing && trailing in scopeFlags) {
            scope = scopeFlags[trailing];
            optionSide.pop();
            const preceding = optionSide.at(-1);
            if (preceding && preceding in scopeFlags) {
              addNotice("用法：`/memory remember <内容> [--global|--project]`");
              return;
            }
          }
          const content = [...optionSide, ...literalSide].join(" ").trim();
          if (!content) {
            addNotice("用法：`/memory remember <内容> [--global|--project]`");
            return;
          }
          const result = await window.agent.request<{
            record: Record<string, unknown> | null;
            memory: WorkspacePayload["memory"];
          }>("memory.remember", { content, scope });
          setWorkspace((current) => current ? { ...current, memory: result.memory } : current);
          addNotice(result.record
            ? `已记住（${scope}）：\`${String(result.record.id)}\`。可用 \`/memory forget ${String(result.record.id)}\` 遗忘。`
            : "该内容未产生新的长期记忆。");
          return;
        }
        if (action === "list") {
          const scope = parts.includes("--global") ? "global" : parts.includes("--project") ? "project" : undefined;
          const records = await window.agent.request<Array<{ scope: string; record: Record<string, unknown> }>>(
            "memory.list", scope ? { scope } : {},
          );
          addNotice(records.length ? [
            "### 长期记忆", "",
            ...records.map((item) => [
              `- \`${String(item.record.key)}\`（\`${String(item.record.id)}\`）= `,
              `\`${JSON.stringify(item.record.value)}\``,
              `（${String(item.record.kind)}/${item.scope}，状态 ${String(item.record.status)}，`,
              `来源 ${String(item.record.authority)}，更新于 ${String(item.record.updated_at)}）`,
            ].join("")),
          ].join("\n") : "没有匹配的长期记忆。");
          return;
        }
        if (action === "conflicts") {
          const conflicts = await window.agent.request<Array<{ scope: string; conflict: Record<string, unknown> }>>(
            "memory.conflicts",
          );
          addNotice(conflicts.length ? [
            "### 记忆冲突", "",
            ...conflicts.map((item) => {
              const candidates = Array.isArray(item.conflict.candidates)
                ? item.conflict.candidates as Array<Record<string, unknown>>
                : [];
              const values = candidates.map((candidate) => [
                JSON.stringify(candidate.value), `（\`${String(candidate.id)}\`）`,
              ].join("")).join(" / ");
              return `- \`${String(item.conflict.key)}\`（${item.scope}）：${values}`;
            }),
          ].join("\n") : "当前没有待解决的记忆冲突。");
          return;
        }
        if (action === "forget") {
          const key = parts.find((item, index) => index > 0 && !item.startsWith("--"));
          if (!key || !parts.includes("--confirm")) {
            addNotice("用法：`/memory forget <key> [--global|--project] --confirm`");
            return;
          }
          const scope = parts.includes("--global") ? "global" : parts.includes("--project") ? "project" : undefined;
          const result = await window.agent.request<{
            removed: boolean;
            memory: WorkspacePayload["memory"];
          }>("memory.forget", {
            key, scope, confirmed: true,
          });
          setWorkspace((current) => current ? { ...current, memory: result.memory } : current);
          addNotice(result.removed ? "记忆已遗忘并写入墓碑。" : "未找到该记忆。");
          return;
        }
        if (action === "clear") {
          const allScopes = parts.includes("--all");
          if ((!allScopes && !parts.includes("--project")) || !parts.includes("--confirm")) {
            addNotice("用法：`/memory clear --project|--all --confirm`");
            return;
          }
          const result = await window.agent.request<{
            count: number;
            memory: WorkspacePayload["memory"];
          }>("memory.clear", {
            allScopes, confirmed: true,
          });
          setWorkspace((current) => current ? { ...current, memory: result.memory } : current);
          addNotice(`已遗忘 ${result.count} 条记忆，并保留墓碑记录。`);
          return;
        }
        addNotice("用法：`/memory status|list|conflicts|remember|forget|clear|auto|on|off`");
        return;
      }
      setError(`桌面端暂不支持命令：/${name}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    const text = input.trim();
    if (!text || !workspace || running || compacting) return;
    if (text.startsWith("/")) {
      const [rawName, ...rest] = text.slice(1).split(/\s+/);
      const command = commandOptions.find((item) => item.name === rawName.toLocaleLowerCase());
      if (command) await executeSlashCommand(command.name, rest.join(" "));
      else setError(`未知命令：/${rawName}`);
      return;
    }
    setInput("");
    setError(null);
    const timestamp = Date.now();
    const id = `user-${timestamp}-${++localItemSequence.current}`;
    setTimelineState((current) => appendTimelineItem(current, (order): TimelineMessageItem => ({
      id,
      kind: "message",
      order,
      revision: 0,
      sessionId: workspace.sessionId,
      runId: null,
      createdAt: timestamp,
      updatedAt: timestamp,
      role: "user",
      text,
      thinking: "",
    })));
    setRunning(true);
    try {
      await window.agent.request("run.start", { text });
    } catch (reason) {
      setRunning(false);
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const enterPlanMode = async () => {
    if (!workspace || running || compacting
      || (workspace.collaborationMode === "plan" && workspace.planState.phase !== "recovery_error")) return;
    try {
      applyWorkspace(await window.agent.request<WorkspacePayload>("mode.enterPlan"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const cancelPlan = async () => {
    const planId = workspace?.planState.activePlanId;
    if (!workspace || (!planId && workspace.planState.phase !== "recovery_error")
      || (running && workspace.planState.phase !== "awaiting_answer") || compacting) return;
    try {
      applyWorkspace(await window.agent.request<WorkspacePayload>("plan.cancel", { planId }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const answerPlanQuestion = async (answer: string) => {
    const question = workspace?.planState.pendingQuestion;
    if (!question || !answer.trim()) return;
    setError(null);
    try {
      await window.agent.request("plan.answer", {
        questionId: question.questionId,
        answer: answer.trim(),
      });
      setCustomPlanAnswer("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const executePlan = async () => {
    const plan = workspace?.planState.latestRevision;
    if (!plan || running || compacting || workspace?.planState.phase !== "ready") return;
    setError(null);
    setRunning(true);
    setPendingPlanAction("execute");
    try {
      await window.agent.request("plan.execute", {
        planId: plan.planId,
        revision: plan.revision,
        digest: plan.digest,
      });
      await refreshSessionSnapshot();
    } catch (reason) {
      setRunning(false);
      setPendingPlanAction(null);
      try {
        await refreshSessionSnapshot();
      } catch {
        // Keep the RPC error as the actionable message.  The last authoritative
        // snapshot remains visible when rehydration is itself unavailable.
      }
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const handoffPlan = async () => {
    const plan = workspace?.planState.latestRevision;
    if (!plan || running || compacting || workspace?.planState.phase !== "ready") return;
    setError(null);
    setRunning(true);
    setPendingPlanAction("handoff");
    try {
      const payload = await window.agent.request<WorkspacePayload>("plan.handoff", {
        planId: plan.planId,
        revision: plan.revision,
        digest: plan.digest,
      });
      setRunning(false);
      applyWorkspace(payload);
    } catch (reason) {
      setRunning(false);
      setPendingPlanAction(null);
      try {
        await refreshSessionSnapshot();
      } catch {
        // Preserve the last authoritative state if the runtime is unavailable.
      }
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const supplementPlan = () => {
    const plan = workspace?.planState.latestRevision;
    if (!plan || running || compacting || workspace?.planState.phase !== "ready") return;
    setSupplementingPlanDigest(plan.digest);
    requestAnimationFrame(() => composerRef.current?.focus());
  };

  const selectModel = async (model: ModelOption) => {
    try {
      const selected = await window.agent.request<WorkspacePayload["model"]>("model.select", {
        modelId: model.id,
        provider: model.provider,
      });
      setWorkspace((current) => current ? { ...current, model: selected } : current);
      setModelPickerOpen(false);
      setModelOptions([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const visibleModels = modelOptions.filter((model) => {
    const term = modelQuery.toLocaleLowerCase();
    return !term || `${model.name} ${model.id} ${model.provider}`.toLocaleLowerCase().includes(term);
  });

  const commandQuery = input.startsWith("/") ? input.slice(1).toLocaleLowerCase() : "";
  const visibleCommands = commandOptions.filter((command) =>
    !commandQuery
    || command.name.toLocaleLowerCase().includes(commandQuery)
    || command.label.toLocaleLowerCase().includes(commandQuery),
  );

  const onInputChange = (value: string) => {
    setInput(value);
    const isCommandSearch = Boolean(workspace)
      && !running
      && !compacting
      && value.startsWith("/")
      && !/\s/.test(value);
    if (isCommandSearch) setModelPickerOpen(false);
    setCommandMenuOpen(isCommandSearch);
    setSelectedCommandIndex(0);
  };

  const onInputKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (commandMenuOpen) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setSelectedCommandIndex((index) => visibleCommands.length
          ? (index + 1) % visibleCommands.length
          : 0);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setSelectedCommandIndex((index) => visibleCommands.length
          ? (index - 1 + visibleCommands.length) % visibleCommands.length
          : 0);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setCommandMenuOpen(false);
        return;
      }
      if ((event.key === "Enter" || event.key === "Tab") && visibleCommands.length) {
        event.preventDefault();
        const command = visibleCommands[Math.min(selectedCommandIndex, visibleCommands.length - 1)];
        void executeSlashCommand(command.name);
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  };

  const resolveApproval = useCallback(async (approval: TimelineApproval, approved: boolean) => {
    setTimelineState((current) => {
      const item = Object.values(current.entities).find((candidate): candidate is TimelineToolItem => (
        candidate.kind === "tool" && candidate.toolCallId === approval.toolCallId
      ));
      if (!item) return current;
      return updateTimelineItem(current, item.id, (candidate) => candidate.kind === "tool"
        ? {
            ...candidate,
            approval: undefined,
            result: approved ? candidate.result : "用户拒绝了本次工具调用",
            status: approved ? "running" : "error",
            revision: candidate.revision + 1,
            updatedAt: Date.now(),
          }
        : candidate);
    });
    try {
      await window.agent.request("approval.resolve", { approvalId: approval.approvalId, approved });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, []);

  const openSession = async (sessionId: string) => {
    if (running || compacting || sessionId === workspace?.sessionId) return;
    const result = await window.agent.request<WorkspacePayload>("session.open", { sessionId });
    applyWorkspace(result);
  };

  const newSession = async () => {
    if (running || compacting || !workspace) return;
    const result = await window.agent.request<WorkspacePayload>("session.new");
    applyWorkspace(result);
  };

  const statusLabel = useMemo(() => {
    if (sidecarStatus === "ready") return "运行时已连接";
    if (sidecarStatus === "starting") return "正在启动运行时";
    return sidecarStatus.startsWith("error:") ? "运行时异常" : "运行时已断开";
  }, [sidecarStatus]);
  const readyPlan = workspace?.planState.phase === "ready"
    ? workspace.planState.latestRevision
    : null;
  const visiblePlan = workspace?.planState.latestRevision ?? null;
  const awaitingPlanDecision = Boolean(
    readyPlan && supplementingPlanDigest !== readyPlan.digest,
  );
  const planStatusPhase = workspace?.planState.phase;
  const planStatusMessage = planStatusPhase === "uncertain"
    ? "上一次执行没有可靠的终态记录，无法判断是否完成。请先检查工作区和运行记录，再决定是否重新规划或执行。"
    : planStatusPhase === "recovery_error"
      ? "Plan 状态记录校验失败。为避免执行错误或被篡改的 revision，当前计划已被锁定。"
      : null;
  const planBadgeLabel = planStatusPhase === "ready" ? "PLAN READY"
    : planStatusPhase === "uncertain" ? "PLAN UNCERTAIN"
      : planStatusPhase === "recovery_error" ? "PLAN RECOVERY" : "PLAN";

  const timelineItems = useMemo(() => selectTimelineItems(timelineState), [timelineState]);
  const renderedPlan = ["ready", "uncertain", "recovery_error"].includes(planStatusPhase ?? "")
    ? visiblePlan : null;
  const renderTimelineItems = useMemo<RenderTimelineItem[]>(() => renderedPlan
    ? [
        ...timelineItems,
        {
          id: `plan:${renderedPlan.planId}:${renderedPlan.revision}:${renderedPlan.digest}`,
          kind: "plan",
          order: timelineState.lastOrder + 1,
          plan: renderedPlan,
        },
      ]
    : timelineItems, [renderedPlan, timelineItems, timelineState.lastOrder]);
  const virtuosoComponents = useMemo(() => ({
    Header: () => <div className="timeline-spacer timeline-spacer--top" aria-hidden="true" />,
    Footer: () => <div className="timeline-spacer timeline-spacer--bottom" aria-hidden="true" />,
    EmptyPlaceholder: () => (
      <div className="timeline-row timeline-row--welcome">
        <section className="welcome-card">
          <span className="eyebrow">DESKTOP MVP</span>
          <h1>把 Agent 放进一个真正的工作区</h1>
          <p>当前版本已经接通流式响应、工具调用、会话恢复和危险工具审批。</p>
          <div className="prompt-chips">
            {["概览这个项目的架构", "找出最值得优化的模块", "运行测试并分析失败原因"].map((value) => (
              <button key={value} onClick={() => setInput(value)}>{value}</button>
            ))}
          </div>
        </section>
      </div>
    ),
  }), []);
  const renderTimelineItem = useCallback((_index: number, item: RenderTimelineItem) => {
    if (item.kind === "plan") {
      return (
        <div className="timeline-row">
          <TimelinePlanCard plan={item.plan} badge={planBadgeLabel} />
        </div>
      );
    }
    if (item.kind === "message") {
      return (
        <div className="timeline-row">
          <TimelineMessageCard message={item} />
        </div>
      );
    }
    if (item.kind === "compaction") {
      return (
        <div className="timeline-row">
          <TimelineCompactionCard compaction={item} />
        </div>
      );
    }
    return (
      <div className="timeline-row">
        <ToolCard
          tool={item}
          cwd={workspace?.path}
          onResolveApproval={resolveApproval}
        />
      </div>
    );
  }, [resolveApproval, workspace?.path, planBadgeLabel]);
  const lastTimelineSignature = (() => {
    const last = renderTimelineItems.at(-1);
    if (!last) return "empty";
    return last.kind === "plan" ? `${last.id}:${last.plan.digest}` : `${last.id}:${last.revision}`;
  })();

  useEffect(() => {
    if (!isAtBottom || renderTimelineItems.length === 0) return;
    const frame = requestAnimationFrame(() => {
      virtuosoRef.current?.scrollToIndex({
        index: renderTimelineItems.length - 1,
        align: "end",
        behavior: "auto",
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [isAtBottom, lastTimelineSignature, renderTimelineItems.length, workspace?.sessionId]);

  return (
    <div className="app-shell">
      <header className="titlebar">
        <div className="brand-mark">CA</div>
        <div className="brand-copy">
          <strong>Coding Agent</strong>
          <span>{workspace ? shortPath(workspace.path) : "未打开工作区"}</span>
        </div>
        <div className="titlebar-spacer" />
        <div className={`runtime-dot ${sidecarStatus === "ready" ? "online" : ""}`} />
        <span className="runtime-label">{statusLabel}</span>
        <button className="ghost-button" onClick={chooseWorkspace} disabled={running || compacting}>打开项目</button>
      </header>

      <div className={`workspace-grid ${sidebarOpen ? "" : "sidebar-closed"}`}>
        <aside className="sidebar">
          <div className="sidebar-heading">
            <span>会话</span>
            <button className="icon-button" onClick={newSession} title="新建会话" disabled={running || compacting}>＋</button>
          </div>
          <div className="session-list">
            {sessions.map((session) => (
              <button
                key={session.id}
                className={`session-item ${session.id === workspace?.sessionId ? "active" : ""}`}
                onClick={() => openSession(session.id)}
                disabled={running || compacting}
              >
                <strong>{session.name || session.first_message || "新会话"}</strong>
                <span>{session.message_count} 条消息</span>
              </button>
            ))}
            {!sessions.length && <div className="empty-sidebar">发送第一条消息后，会话会保存到 JSONL。</div>}
          </div>
          {workspace && (
            <div className="workspace-meta">
              <span>模型</span>
              <strong>{workspace.model.name || workspace.model.id}</strong>
              <span>工具</span>
              <strong>{workspace.tools.length} 个已启用</strong>
              <span>长期记忆</span>
              <button
                className={`memory-toggle ${workspace.memory.enabled ? "on" : "off"}`}
                onClick={() => void executeSlashCommand("memory", workspace.memory.enabled ? "off" : "on")}
                disabled={running || compacting}
              >
                {workspace.memory.enabled ? "已开启" : "已关闭"}
              </button>
              <span>自动提取</span>
              <button
                className={`memory-toggle ${workspace.memory.autoExtractEnabled === false ? "off" : "on"}`}
                onClick={() => void executeSlashCommand(
                  "memory", workspace.memory.autoExtractEnabled === false ? "auto on" : "auto off",
                )}
                disabled={running || compacting || !workspace.memory.enabled}
              >
                {workspace.memory.autoExtractEnabled === false ? "自动提取关闭" : "自动提取开启"}
              </button>
              <span>提取队列</span>
              <strong>
                待处理 {workspace.memory.pendingCount ?? 0} · 处理中 {workspace.memory.processingCount ?? 0}
                {" · "}已完成 {workspace.memory.readyCount ?? 0} · 失败 {workspace.memory.failedCount ?? 0}
              </strong>
            </div>
          )}
        </aside>

        <main className="conversation">
          <button className="sidebar-toggle" onClick={() => setSidebarOpen((value) => !value)}>
            {sidebarOpen ? "‹" : "›"}
          </button>
          <Virtuoso
            ref={virtuosoRef}
            className="timeline"
            aria-label="对话时间线"
            data={renderTimelineItems}
            initialItemCount={Math.min(renderTimelineItems.length, 20)}
            increaseViewportBy={{ top: 600, bottom: 800 }}
            atBottomThreshold={72}
            atBottomStateChange={setIsAtBottom}
            followOutput={(atBottom) => atBottom ? "auto" : false}
            computeItemKey={(_index, item) => item.id}
            components={virtuosoComponents}
            itemContent={renderTimelineItem}
          />
          <div className="composer-wrap">
            {!isAtBottom && renderTimelineItems.length > 0 && (
              <button
                className="jump-latest"
                aria-label="回到最新消息"
                onClick={() => virtuosoRef.current?.scrollToIndex({
                  index: renderTimelineItems.length - 1,
                  align: "end",
                  behavior: "smooth",
                })}
              >
                ↓ 最新消息
              </button>
            )}
            {workspace?.planState.pendingQuestion && (
              <section className="plan-question" data-testid="plan-question">
                <div className="plan-question-header">
                  <span className="plan-badge">PLAN</span>
                  <strong>{workspace.planState.pendingQuestion.header}</strong>
                </div>
                <p>{workspace.planState.pendingQuestion.question}</p>
                <div className="plan-options">
                  {workspace.planState.pendingQuestion.options.map((option) => (
                    <button key={option.label} onClick={() => void answerPlanQuestion(option.label)}>
                      <strong>{option.label}</strong>
                      <span>{option.description}</span>
                    </button>
                  ))}
                </div>
                {workspace.planState.pendingQuestion.allowCustom && (
                  <div className="plan-custom-answer">
                    <input
                      value={customPlanAnswer}
                      onChange={(event) => setCustomPlanAnswer(event.target.value)}
                      placeholder="自定义回答"
                    />
                    <button onClick={() => void answerPlanQuestion(customPlanAnswer)} disabled={!customPlanAnswer.trim()}>提交</button>
                  </div>
                )}
                <button onClick={() => void cancelPlan()} disabled={compacting}>取消规划</button>
              </section>
            )}
            {readyPlan && awaitingPlanDecision && (
              <section className="plan-question plan-decision" data-testid="plan-decision">
                <div className="plan-question-header">
                  <span className="plan-badge">PLAN READY</span>
                  <strong>下一步</strong>
                </div>
                <p>计划已提交，下一步怎么做？</p>
                <div className="plan-options">
                  <button onClick={() => void executePlan()} disabled={running || compacting}>
                    <strong>{pendingPlanAction === "execute" ? "正在确认…" : "执行方案"}</strong>
                    <span>确认 revision {readyPlan.revision} 并立即切回 Default 执行</span>
                  </button>
                  <button onClick={() => void handoffPlan()} disabled={running || compacting}>
                    <strong>{pendingPlanAction === "handoff" ? "正在创建…" : "新会话复核"}</strong>
                    <span>只把这个 revision 交给新会话，打开后再次确认再执行</span>
                  </button>
                  <button onClick={supplementPlan} disabled={running || compacting}>
                    <strong>继续修改</strong>
                    <span>保持 Plan Mode，在输入框中说明要补充或修改的内容</span>
                  </button>
                </div>
                <button className="plan-decision-cancel" onClick={() => void cancelPlan()} disabled={running || compacting}>取消规划</button>
              </section>
            )}
            {workspace?.planState.legacyCandidate && (
              <section className="plan-status" data-testid="plan-legacy">
                <p>检测到旧版计划候选文本，尚未形成可执行 revision。请继续规划，并通过 submit_plan 重新提交后再确认执行。</p>
              </section>
            )}
            {planStatusMessage && (
              <section className={`plan-status ${planStatusPhase}`} data-testid="plan-status">
                <div className="plan-question-header">
                  <span className="plan-badge">{planBadgeLabel}</span>
                  <strong>{planStatusPhase === "uncertain" ? "执行状态不确定" : "计划恢复失败"}</strong>
                </div>
                <p>{planStatusMessage}</p>
                {workspace?.planState.recoveryError && (
                  <pre>{workspace.planState.recoveryError.code}: {workspace.planState.recoveryError.message}</pre>
                )}
                <details>
                  <summary>查看记录详情</summary>
                  <pre>{JSON.stringify(workspace?.planState.latestRun ?? workspace?.planState.recoveryError, null, 2)}</pre>
                </details>
                <div className="plan-options">
                  <button onClick={() => void enterPlanMode()} disabled={running || compacting}>重新规划</button>
                  <button onClick={() => void cancelPlan()} disabled={running || compacting}>取消规划</button>
                </div>
              </section>
            )}
            {commandMenuOpen && (
              <section className="command-palette" aria-label="斜杠命令">
                <div className="command-palette-title">
                  <strong>命令</strong>
                  <span>↑↓ 选择 · Enter 执行 · Esc 关闭</span>
                </div>
                <div className="command-list">
                  {visibleCommands.map((command, index) => (
                    <button
                      key={command.name}
                      className={index === selectedCommandIndex ? "active" : ""}
                      onMouseEnter={() => setSelectedCommandIndex(index)}
                      onClick={() => void executeSlashCommand(command.name)}
                    >
                      <span className="command-icon">{COMMAND_ICONS[command.name] ?? "/"}</span>
                      <span className="command-name"><strong>{command.label}</strong><small>/{command.name}</small></span>
                      <em>{command.description}</em>
                    </button>
                  ))}
                  {!visibleCommands.length && <p>没有匹配的命令。</p>}
                </div>
              </section>
            )}
            {modelPickerOpen && (
              <section className="model-picker">
                <div className="model-picker-header">
                  <div>
                    <strong>选择模型</strong>
                    <span>仅显示已配置凭据的 Provider</span>
                  </div>
                  <button onClick={() => setModelPickerOpen(false)} aria-label="关闭模型选择器">×</button>
                </div>
                <div className="model-list">
                  {visibleModels.map((model) => (
                    <button
                      key={`${model.provider}:${model.id}`}
                      className={model.current ? "active" : ""}
                      onClick={() => void selectModel(model)}
                    >
                      <span><strong>{model.name}</strong><small>{model.id}</small></span>
                      <em>{model.provider}{model.current ? " · 当前" : ""}</em>
                    </button>
                  ))}
                  {!visibleModels.length && <p>没有匹配的可用模型。</p>}
                </div>
              </section>
            )}
            {error && <div className="error-banner">{error}<button onClick={() => setError(null)}>×</button></div>}
            <form className={`composer ${workspace?.collaborationMode === "plan" ? "plan-mode" : ""}`} onSubmit={submit}>
              <textarea
                ref={composerRef}
                value={input}
                onChange={(event) => onInputChange(event.target.value)}
                onKeyDown={onInputKeyDown}
                placeholder={compacting
                  ? "正在压缩上下文，请稍候…"
                  : !workspace
                  ? "请先打开工作区"
                  : supplementingPlanDigest === readyPlan?.digest
                    ? "补充你的想法或修改要求…"
                    : awaitingPlanDecision
                      ? "请先选择执行、新会话复核或继续修改"
                      : "描述你想完成的任务…"}
                disabled={!workspace || awaitingPlanDecision || compacting || Boolean(planStatusMessage)}
                rows={3}
              />
              <div className="composer-footer">
                <div className="mode-switch" aria-label="协作模式">
                  <button
                    type="button"
                    className={workspace?.collaborationMode === "default" ? "active" : ""}
                    onClick={() => workspace?.collaborationMode === "plan" ? void cancelPlan() : undefined}
                    disabled={!workspace || running || compacting}
                  >Default</button>
                  <button
                    type="button"
                    className={workspace?.collaborationMode === "plan" ? "active" : ""}
                    onClick={() => void enterPlanMode()}
                    disabled={!workspace || running || compacting}
                  >Plan</button>
                  {(workspace?.collaborationMode === "plan" || planStatusMessage) && (
                    <span className="plan-badge">{planBadgeLabel}</span>
                  )}
                </div>
                <span>Enter 发送 · Shift+Enter 换行</span>
                {running ? (
                  <button type="button" className="stop-button" onClick={() => window.agent.request("run.abort")}>■ 停止</button>
                ) : compacting ? (
                  <button type="button" className="send-button" disabled>压缩中…</button>
                ) : (
                  <button type="submit" className="send-button" disabled={!input.trim() || !workspace}>发送 ↑</button>
                )}
              </div>
            </form>
          </div>
        </main>
      </div>
    </div>
  );
}
