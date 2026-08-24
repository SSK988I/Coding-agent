import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type {
  AgentMessage,
  ContentBlock,
  RuntimeEvent,
  PlanQuestionPayload,
  PlanRevisionPayload,
  SessionInfo,
  WorkspacePayload,
} from "../shared/types";

interface ViewMessage {
  id: string;
  order: number;
  role: "user" | "assistant";
  text: string;
  thinking: string;
  status?: string;
}

interface ToolView {
  id: string;
  order: number;
  name: string;
  args: unknown;
  result?: unknown;
  status: "running" | "approval" | "done" | "error";
  approval?: ApprovalView;
}

type CompactionStatus = "running" | "completed" | "skipped" | "failed" | "aborted";

interface CompactionView {
  id: string;
  order: number;
  status: CompactionStatus;
  reason: string;
  summary: string;
  tokensBefore?: number;
  detail?: string;
}

interface ApprovalView {
  approvalId: string;
  toolCallId: string;
  toolName: string;
  args: unknown;
}

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

const COMMAND_ICONS: Record<string, string> = {
  help: "?",
  new: "+",
  model: "◇",
  compact: "⌁",
  clear: "↺",
  session: "◷",
  memory: "◎",
};

function messageText(message: AgentMessage): { text: string; thinking: string } {
  if (typeof message.content === "string") return { text: message.content, thinking: "" };
  let text = "";
  let thinking = "";
  for (const block of message.content as ContentBlock[]) {
    if (block.type === "text") text += block.text ?? "";
    if (block.type === "thinking") thinking += block.thinking ?? "";
  }
  return { text, thinking };
}

function persistedMessages(messages: AgentMessage[]): ViewMessage[] {
  return messages.flatMap((message, index) => {
    if (message.role !== "user" && message.role !== "assistant") return [];
    const content = messageText(message);
    // Slash commands belong to the local UI, and tool-only/error assistant
    // records have no chat body. Older MVP sessions may contain both.
    if (message.role === "user" && content.text.trimStart().startsWith("/")) return [];
    if (message.role === "user" && content.text.trimStart().startsWith("<confirmed_plan_execution>")) {
      content.text = "执行已确认计划";
    }
    if (message.role === "assistant" && !content.text && !content.thinking) return [];
    return [{
      id: `persisted-${message.timestamp ?? index}-${index}`,
      order: index,
      role: message.role,
      text: content.text,
      thinking: content.thinking,
      status: message.stop_reason,
    } as ViewMessage];
  });
}

function persistedCompactions(messages: AgentMessage[]): CompactionView[] {
  return messages.flatMap((message, index) => {
    if (message.role !== "compactionSummary") return [];
    const tokensBefore = message.tokens_before ?? message.tokensBefore;
    return [{
      id: `persisted-compaction-${message.timestamp ?? index}-${index}`,
      order: index,
      status: "completed",
      reason: "persisted",
      summary: message.summary ?? "",
      tokensBefore: typeof tokensBefore === "number" ? tokensBefore : undefined,
    } as CompactionView];
  });
}

function toolOutput(value: unknown): string {
  if (value && typeof value === "object" && "content" in value) {
    const content = (value as { content?: unknown }).content;
    if (Array.isArray(content)) {
      const text = content
        .flatMap((item) => item && typeof item === "object" && "text" in item
          ? [String((item as { text?: unknown }).text ?? "")]
          : [])
        .filter(Boolean)
        .join("\n");
      if (text) return text;
    }
  }
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2) ?? "";
}

function shortPath(value: string): string {
  const normalized = value.replaceAll("\\", "/");
  const parts = normalized.split("/").filter(Boolean);
  return parts.slice(-2).join("/") || value;
}

export function App() {
  const [sidecarStatus, setSidecarStatus] = useState("starting");
  const [workspace, setWorkspace] = useState<WorkspacePayload | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [messages, setMessages] = useState<ViewMessage[]>([]);
  const [tools, setTools] = useState<ToolView[]>([]);
  const [compactions, setCompactions] = useState<CompactionView[]>([]);
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
  const [customPlanAnswer, setCustomPlanAnswer] = useState("");
  const [supplementingPlanDigest, setSupplementingPlanDigest] = useState<string | null>(null);
  const timelineRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const didBootstrap = useRef(false);
  const rafQueue = useRef<RuntimeEvent[]>([]);
  const rafId = useRef<number | null>(null);
  const activeAssistantIds = useRef(new Map<string, string>());
  const assistantSequence = useRef(0);
  const timelineSequence = useRef(0);
  const activeCompactionId = useRef<string | null>(null);
  const compactingRef = useRef(false);

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

  const applyWorkspace = (payload: WorkspacePayload) => {
    timelineSequence.current = payload.messages.length;
    setWorkspace(payload);
    setMessages(persistedMessages(payload.messages));
    setCompactions(persistedCompactions(payload.messages));
    setTools([]);
    setCommandMenuOpen(false);
    setModelPickerOpen(false);
    setCustomPlanAnswer("");
    setSupplementingPlanDigest(null);
    activeAssistantIds.current.clear();
    activeCompactionId.current = null;
    compactingRef.current = false;
    setCompacting(false);
    setError(null);
    void refreshSessions();
    void refreshCommands();
  };

  const beginCompaction = (reason: string): string => {
    if (compactingRef.current && activeCompactionId.current) {
      return activeCompactionId.current;
    }
    const order = ++timelineSequence.current;
    const id = `compaction-${Date.now()}-${order}`;
    activeCompactionId.current = id;
    compactingRef.current = true;
    setCompacting(true);
    setCompactions((current) => [...current, {
      id,
      order,
      status: "running",
      reason,
      summary: "",
    }]);
    return id;
  };

  const finishCompaction = (
    status: Exclude<CompactionStatus, "running">,
    summary = "",
    detail?: string,
  ) => {
    const id = activeCompactionId.current;
    if (!id) return;
    compactingRef.current = false;
    setCompacting(false);
    setCompactions((current) => current.map((item) => item.id === id
      ? { ...item, status, summary: summary || item.summary, detail }
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
      // Compaction lifecycle events are sparse and user-visible. Process them
      // before the animation-frame stream queue so the RPC response cannot race
      // ahead of its start/end status in the renderer.
      if (event.event.type === "compaction_start" || event.event.type === "compaction_end") {
        handleRuntimeEvent(event);
        return;
      }
      rafQueue.current.push(event);
      if (rafId.current !== null) return;
      rafId.current = requestAnimationFrame(() => {
        const events = rafQueue.current.splice(0);
        rafId.current = null;
        for (const item of events) handleRuntimeEvent(item);
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

  useEffect(() => {
    const timeline = timelineRef.current;
    if (timeline) timeline.scrollTop = timeline.scrollHeight;
  }, [messages, tools, compactions, running, compacting]);

  const handleRuntimeEvent = (envelope: RuntimeEvent) => {
    const { type, payload } = envelope.event;
    const runKey = envelope.runId ?? `seq-${envelope.seq}`;
    if (type === "run.started") {
      setRunning(true);
      return;
    }
    if (type === "run.completed" || type === "run.cancelled" || type === "run.failed") {
      setRunning(false);
      activeAssistantIds.current.delete(runKey);
      if (type === "run.failed") setError(String(payload.message ?? "运行失败"));
      void refreshSessions();
      return;
    }
    if (type === "compaction_start") {
      beginCompaction(String(payload.reason ?? "automatic"));
      return;
    }
    if (type === "compaction_end") {
      const failed = typeof payload.error === "string" && payload.error.length > 0;
      const status: Exclude<CompactionStatus, "running"> = failed
        ? "failed"
        : payload.aborted ? "aborted" : "completed";
      finishCompaction(
        status,
        String(payload.summary_preview ?? ""),
        failed ? String(payload.error) : undefined,
      );
      void refreshSessions();
      return;
    }
    if (type === "message_start") {
      const raw = payload.message as AgentMessage | undefined;
      if (raw?.role !== "assistant") return;
      const messageId = `${runKey}-assistant-${++assistantSequence.current}`;
      const order = ++timelineSequence.current;
      activeAssistantIds.current.set(runKey, messageId);
      setMessages((current) => [
        ...current,
        {
          id: messageId,
          order,
          role: "assistant",
          text: "",
          thinking: "",
          status: "streaming",
        },
      ]);
      return;
    }
    if (type === "message_update") {
      const kind = String(payload.kind ?? "");
      const delta = typeof payload.delta === "string" ? payload.delta : "";
      if (!delta) return;
      const messageId = activeAssistantIds.current.get(runKey);
      if (!messageId) return;
      setMessages((current) => current.map((item) => item.id === messageId
        ? {
            ...item,
            text: kind === "text_delta" ? item.text + delta : item.text,
            thinking: kind === "thinking_delta" ? item.thinking + delta : item.thinking,
          }
        : item));
      return;
    }
    if (type === "message_end") {
      const raw = payload.message as AgentMessage | undefined;
      if (raw?.role !== "assistant") return;
      const content = messageText(raw);
      const messageId = activeAssistantIds.current.get(runKey);
      if (messageId) {
        setMessages((current) => content.text || content.thinking
          ? current.map((item) => item.id === messageId
              ? { ...item, ...content, status: raw.stop_reason }
              : item)
          : current.filter((item) => item.id !== messageId));
        activeAssistantIds.current.delete(runKey);
      }
      if (raw.error_message) setError(raw.error_message);
      return;
    }
    if (type === "tool_execution_start") {
      if (payload.tool_name === "request_user_input") return;
      const id = String(payload.tool_call_id);
      const order = ++timelineSequence.current;
      setTools((current) => {
        const existing = current.find((item) => item.id === id);
        if (existing) {
          return current.map((item) => item.id === id
            ? { ...item, name: String(payload.tool_name), args: payload.args, status: "running" }
            : item);
        }
        return [...current, {
          id,
          order,
          name: String(payload.tool_name),
          args: payload.args,
          status: "running",
        }];
      });
      return;
    }
    if (type === "tool_execution_end") {
      if (payload.tool_name === "request_user_input") return;
      const id = String(payload.tool_call_id);
      setTools((current) => current.map((item) => item.id === id
        ? {
            ...item,
            approval: undefined,
            result: payload.result,
            status: payload.is_error ? "error" : "done",
          }
        : item));
      return;
    }
    if (type === "approval.requested") {
      const approval = payload as unknown as ApprovalView;
      const order = ++timelineSequence.current;
      setTools((current) => {
        const existing = current.find((item) => item.id === approval.toolCallId);
        if (existing) {
          return current.map((item) => item.id === approval.toolCallId
            ? { ...item, approval, status: "approval" }
            : item);
        }
        return [...current, {
          id: approval.toolCallId,
          order,
          name: approval.toolName,
          args: approval.args,
          approval,
          status: "approval",
        }];
      });
      return;
    }
    if (type === "approval.expired") {
      const toolCallId = String(payload.toolCallId);
      setTools((current) => current.map((item) => item.id === toolCallId
        ? { ...item, approval: undefined, result: "工具审批已超时", status: "error" }
        : item));
      return;
    }
    if (type === "session.changed") {
      applyWorkspace(payload as unknown as WorkspacePayload);
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
    if (type === "collaboration_mode_changed") {
      const mode = payload.mode === "plan" ? "plan" : "default";
      setWorkspace((current) => current ? {
        ...current,
        collaborationMode: mode,
        planState: {
          ...current.planState,
          phase: String(payload.phase ?? (mode === "plan" ? "drafting" : "cancelled")) as WorkspacePayload["planState"]["phase"],
          activePlanId: String(payload.plan_id ?? current.planState.activePlanId ?? "") || null,
          pendingQuestion: mode === "default" ? null : current.planState.pendingQuestion,
        },
      } : current);
    }
    if (type === "plan_question_requested") {
      const question = payload.question as PlanQuestionPayload;
      setWorkspace((current) => current ? {
        ...current,
        collaborationMode: "plan",
        planState: { ...current.planState, phase: "awaiting_answer", pendingQuestion: question },
      } : current);
    }
    if (type === "plan_question_answered") {
      setWorkspace((current) => current ? {
        ...current,
        planState: { ...current.planState, phase: "drafting", pendingQuestion: null },
      } : current);
    }
    if (type === "plan_ready") {
      const plan = payload.plan as PlanRevisionPayload;
      setSupplementingPlanDigest(null);
      setWorkspace((current) => current ? {
        ...current,
        collaborationMode: "plan",
        planState: { ...current.planState, phase: "ready", pendingQuestion: null, latestRevision: plan },
      } : current);
    }
    if (type === "plan_execution_started") {
      setWorkspace((current) => current ? {
        ...current,
        collaborationMode: "default",
        planState: { ...current.planState, phase: "executing", pendingQuestion: null },
      } : current);
    }
    if (type === "plan_execution_completed" || type === "plan_execution_failed" || type === "plan_execution_aborted") {
      const phase = type.replace("plan_execution_", "") as "completed" | "failed" | "aborted";
      setWorkspace((current) => current ? {
        ...current,
        collaborationMode: "default",
        planState: { ...current.planState, phase },
      } : current);
    }
  };

  const chooseWorkspace = async () => {
    const selected = await window.desktop.chooseWorkspace();
    if (selected) await openWorkspace(selected, true);
  };

  const addNotice = (text: string) => {
    const order = ++timelineSequence.current;
    setMessages((current) => [...current, {
      id: `notice-${Date.now()}-${current.length}`,
      order,
      role: "assistant",
      text,
      thinking: "",
    }]);
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
            const status: Exclude<CompactionStatus, "running" | "completed"> = detail === "Compaction aborted"
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
        const parts = args.trim().split(/\s+/).filter(Boolean);
        const action = parts[0]?.toLocaleLowerCase() || "status";
        if (action === "on" || action === "off") {
          const memory = await window.agent.request<WorkspacePayload["memory"]>("memory.setEnabled", {
            enabled: action === "on",
          });
          setWorkspace((current) => current ? { ...current, memory } : current);
          addNotice(`长期记忆已${action === "on" ? "开启" : "关闭"}。`);
          return;
        }
        if (action === "status") {
          const memory = await window.agent.request<WorkspacePayload["memory"]>("memory.status");
          setWorkspace((current) => current ? { ...current, memory } : current);
          addNotice([
            "### 长期记忆状态", "",
            `- 状态：\`${memory.enabled ? "on" : "off"}\``,
            `- 用户：\`${memory.userId ?? "none"}\``,
            `- 项目：\`${memory.projectId ?? "none"}\``,
            `- 全局记忆：\`${memory.globalCount ?? 0}\``,
            `- 项目记忆：\`${memory.projectCount ?? 0}\``,
            `- 待解决冲突：\`${memory.conflictCount ?? 0}\``,
            `- 目录：\`${memory.root ?? ""}\``,
          ].join("\n"));
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
        addNotice("用法：`/memory status|list|conflicts|forget|clear|on|off`");
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
    const order = ++timelineSequence.current;
    setMessages((current) => [...current, {
      id: `user-${Date.now()}`,
      order,
      role: "user",
      text,
      thinking: "",
    }]);
    setRunning(true);
    try {
      await window.agent.request("run.start", { text });
    } catch (reason) {
      setRunning(false);
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const enterPlanMode = async () => {
    if (!workspace || running || compacting || workspace.collaborationMode === "plan") return;
    try {
      applyWorkspace(await window.agent.request<WorkspacePayload>("mode.enterPlan"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const cancelPlan = async () => {
    const planId = workspace?.planState.activePlanId;
    if (!planId || running || compacting) return;
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
      setWorkspace((current) => current ? {
        ...current,
        planState: { ...current.planState, phase: "drafting", pendingQuestion: null },
      } : current);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const executePlan = async () => {
    const plan = workspace?.planState.latestRevision;
    if (!plan || running || compacting || workspace?.planState.phase !== "ready") return;
    setError(null);
    setRunning(true);
    setWorkspace((current) => current ? {
      ...current,
      collaborationMode: "default",
      planState: { ...current.planState, phase: "executing", pendingQuestion: null },
    } : current);
    try {
      await window.agent.request("plan.execute", {
        planId: plan.planId,
        revision: plan.revision,
        digest: plan.digest,
      });
    } catch (reason) {
      setRunning(false);
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

  const resolveApproval = async (approval: ApprovalView, approved: boolean) => {
    setTools((current) => current.map((item) => item.id === approval.toolCallId
      ? {
          ...item,
          approval: undefined,
          result: approved ? item.result : "用户拒绝了本次工具调用",
          status: approved ? "running" : "error",
        }
      : item));
    try {
      await window.agent.request("approval.resolve", { approvalId: approval.approvalId, approved });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

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
  const awaitingPlanDecision = Boolean(
    readyPlan && supplementingPlanDigest !== readyPlan.digest,
  );

  const timelineItems = useMemo(() => [
    ...messages.map((value) => ({ kind: "message" as const, order: value.order, value })),
    ...tools.map((value) => ({ kind: "tool" as const, order: value.order, value })),
    ...compactions.map((value) => ({ kind: "compaction" as const, order: value.order, value })),
  ].sort((left, right) => left.order - right.order), [messages, tools, compactions]);

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
            </div>
          )}
        </aside>

        <main className="conversation">
          <button className="sidebar-toggle" onClick={() => setSidebarOpen((value) => !value)}>
            {sidebarOpen ? "‹" : "›"}
          </button>
          <div className="timeline" ref={timelineRef}>
            {!messages.length && !compactions.length && (
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
            )}

            {timelineItems.map((item) => {
              if (item.kind === "message") {
                const message = item.value;
                return (
                  <article key={`message-${message.id}`} className={`message ${message.role}`}>
                    <div className="message-avatar">{message.role === "user" ? "你" : "AI"}</div>
                    <div className="message-body">
                      {message.thinking && (
                        <details className="thinking-block">
                          <summary>思考过程</summary>
                          <pre>{message.thinking}</pre>
                        </details>
                      )}
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.text || (message.status === "streaming" ? "正在思考…" : "")}</ReactMarkdown>
                    </div>
                  </article>
                );
              }

              if (item.kind === "compaction") {
                const compaction = item.value;
                const labels: Record<CompactionStatus, string> = {
                  running: "正在压缩上下文…",
                  completed: "上下文压缩完成",
                  skipped: "未执行上下文压缩",
                  failed: "上下文压缩失败",
                  aborted: "上下文压缩已取消",
                };
                return (
                  <section
                    key={`compaction-${compaction.id}`}
                    className={`compaction-card ${compaction.status}`}
                    data-testid="compaction-card"
                  >
                    <div className="compaction-card-header">
                      <span className="compaction-icon">⌁</span>
                      <strong>{labels[compaction.status]}</strong>
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
              }

              const tool = item.value;
              const status = tool.status === "approval"
                ? "等待确认"
                : tool.status === "running"
                  ? "执行中"
                  : tool.status === "done" ? "已完成" : "失败";
              return (
                <section key={`tool-${tool.id}`} className={`tool-card ${tool.status}`}>
                  <div className="tool-card-header">
                    <span className="tool-icon">⌁</span>
                    <strong>{tool.name}</strong>
                    <span>{status}</span>
                  </div>
                  <pre>{toolOutput(tool.result ?? tool.args)}</pre>
                  {tool.approval && (
                    <div className="tool-approval">
                      <span>此操作可能修改文件或系统状态，是否允许执行？</span>
                      <div className="approval-actions">
                        <button className="danger-button" onClick={() => resolveApproval(tool.approval!, false)}>拒绝</button>
                        <button className="primary-button" onClick={() => resolveApproval(tool.approval!, true)}>允许一次</button>
                      </div>
                    </div>
                  )}
                </section>
              );
            })}
            {workspace?.planState.phase === "ready" && workspace.planState.latestRevision && (
              <section className="plan-card" data-testid="plan-card">
                <div className="plan-card-header">
                  <span className="plan-badge">PLAN</span>
                  <strong>{workspace.planState.latestRevision.title}</strong>
                  <span>revision {workspace.planState.latestRevision.revision} · {workspace.planState.latestRevision.digest.slice(0, 12)}</span>
                </div>
                <div className="plan-markdown">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{workspace.planState.latestRevision.markdown}</ReactMarkdown>
                </div>
              </section>
            )}
          </div>

          <div className="composer-wrap">
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
              </section>
            )}
            {readyPlan && awaitingPlanDecision && (
              <section className="plan-question plan-decision" data-testid="plan-decision">
                <div className="plan-question-header">
                  <span className="plan-badge">PLAN</span>
                  <strong>下一步</strong>
                </div>
                <p>计划已完成，下一步怎么做？</p>
                <div className="plan-options">
                  <button onClick={() => void executePlan()} disabled={running || compacting}>
                    <strong>执行方案</strong>
                    <span>确认 revision {readyPlan.revision} 并立即切回 Default 执行</span>
                  </button>
                  <button onClick={supplementPlan} disabled={running || compacting}>
                    <strong>补充想法</strong>
                    <span>保持 Plan Mode，在输入框中说明要补充或修改的内容</span>
                  </button>
                </div>
                <button className="plan-decision-cancel" onClick={() => void cancelPlan()} disabled={running || compacting}>取消规划</button>
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
                      ? "请先选择执行方案或补充想法"
                      : "描述你想完成的任务…"}
                disabled={!workspace || awaitingPlanDecision || compacting}
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
                  {workspace?.collaborationMode === "plan" && <span className="plan-badge">PLAN</span>}
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
