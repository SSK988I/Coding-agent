import type { AgentMessage, ContentBlock, RuntimeEvent } from "../../shared/types";

export type TimelineItemKind = "message" | "tool" | "compaction";

interface TimelineItemBase {
  id: string;
  kind: TimelineItemKind;
  order: number;
  revision: number;
  sessionId: string | null;
  runId: string | null;
  createdAt: number;
  updatedAt: number;
  lastSeq?: number;
}

export interface TimelineMessageItem extends TimelineItemBase {
  kind: "message";
  role: "user" | "assistant";
  text: string;
  thinking: string;
  status?: string;
  /** The protocol-level id, when the runtime supplies one. */
  messageId?: string;
}

export interface TimelineApproval {
  approvalId: string;
  toolCallId: string;
  toolName: string;
  args: unknown;
}

export interface TimelineToolItem extends TimelineItemBase {
  kind: "tool";
  toolCallId: string;
  name: string;
  args: unknown;
  result?: unknown;
  status: "running" | "approval" | "done" | "error";
  approval?: TimelineApproval;
}

export type TimelineCompactionStatus =
  | "running"
  | "completed"
  | "skipped"
  | "failed"
  | "aborted";

export interface TimelineCompactionItem extends TimelineItemBase {
  kind: "compaction";
  status: TimelineCompactionStatus;
  reason: string;
  summary: string;
  tokensBefore?: number;
  detail?: string;
}

export type TimelineItem =
  | TimelineMessageItem
  | TimelineToolItem
  | TimelineCompactionItem;

/**
 * A normalized timeline store. Rendering code can subscribe to an individual
 * entity without rebuilding every item for each streamed token.
 *
 * Fields below `active*` are reducer bookkeeping and deliberately kept in the
 * state so event reduction remains deterministic and unit-testable.
 */
export interface TimelineState {
  entities: Readonly<Record<string, TimelineItem>>;
  orderedIds: readonly string[];
  activeMessageIdsByRun: Readonly<Record<string, string>>;
  messageIdsByRoute: Readonly<Record<string, string>>;
  activeCompactionId: string | null;
  lastOrder: number;
  assistantSequence: number;
  running: boolean;
  lastError: string | null;
}

interface MessageDeltaPatch {
  text: string;
  thinking: string;
  timestamp: number;
  seq: number;
}

interface ToolUpdatePatch {
  result: unknown;
  timestamp: number;
  seq: number;
}

export function createTimelineState(items: readonly TimelineItem[] = []): TimelineState {
  const entities: Record<string, TimelineItem> = {};
  const orderedIds: string[] = [];
  let lastOrder = 0;
  let assistantSequence = 0;

  for (const item of items) {
    if (!(item.id in entities)) orderedIds.push(item.id);
    entities[item.id] = item;
    lastOrder = Math.max(lastOrder, item.order);
    if (item.kind === "message" && item.role === "assistant") assistantSequence += 1;
  }

  return {
    entities,
    orderedIds,
    activeMessageIdsByRun: {},
    messageIdsByRoute: {},
    activeCompactionId: null,
    lastOrder,
    assistantSequence,
    running: false,
    lastError: null,
  };
}

export function selectTimelineItems(state: TimelineState): TimelineItem[] {
  return state.orderedIds.flatMap((id) => {
    const item = state.entities[id];
    return item ? [item] : [];
  });
}

export function readAgentMessageContent(message: AgentMessage): { text: string; thinking: string } {
  if (typeof message.content === "string") return { text: message.content, thinking: "" };
  let text = "";
  let thinking = "";
  for (const block of message.content as ContentBlock[]) {
    if (block.type === "text") text += block.text ?? "";
    if (block.type === "thinking") thinking += block.thinking ?? "";
  }
  return { text, thinking };
}

/** Converts the current JSONL-backed transcript to the same item model as live events. */
export function timelineItemsFromPersistedMessages(
  messages: readonly AgentMessage[],
  sessionId: string | null = null,
): TimelineItem[] {
  return messages.flatMap<TimelineItem>((message, index): TimelineItem[] => {
    const timestamp = typeof message.timestamp === "number" ? message.timestamp : index;
    if (message.role === "compactionSummary") {
      const tokensBefore = message.tokens_before ?? message.tokensBefore;
      return [{
        id: `persisted-compaction-${timestamp}-${index}`,
        kind: "compaction",
        order: index,
        revision: 0,
        sessionId,
        runId: null,
        createdAt: timestamp,
        updatedAt: timestamp,
        status: "completed",
        reason: "persisted",
        summary: message.summary ?? "",
        tokensBefore: typeof tokensBefore === "number" ? tokensBefore : undefined,
      } satisfies TimelineCompactionItem];
    }
    if (message.role !== "user" && message.role !== "assistant") return [];

    const content = readAgentMessageContent(message);
    if (message.role === "user" && content.text.trimStart().startsWith("/")) return [];
    if (message.role === "user" && content.text.trimStart().startsWith("<confirmed_plan_execution>")) {
      content.text = "执行已确认计划";
    }
    if (message.role === "assistant" && !content.text && !content.thinking) return [];

    return [{
      id: `persisted-${timestamp}-${index}`,
      kind: "message",
      order: index,
      revision: 0,
      sessionId,
      runId: null,
      createdAt: timestamp,
      updatedAt: timestamp,
      role: message.role,
      text: content.text,
      thinking: content.thinking,
      status: message.stop_reason,
    } satisfies TimelineMessageItem];
  });
}

export function createTimelineStateFromMessages(
  messages: readonly AgentMessage[],
  sessionId: string | null = null,
): TimelineState {
  return createTimelineState(timelineItemsFromPersistedMessages(messages, sessionId));
}

export function reduceRuntimeEvent(state: TimelineState, event: RuntimeEvent): TimelineState {
  return reduceRuntimeEventBatch(state, [event]);
}

/**
 * Reduces all runtime events collected for one render frame.
 *
 * text_delta/thinking_delta events are accumulated by resolved message id and
 * written once at the end of the batch. Delta order is retained inside each
 * stream, while non-delta lifecycle events are still observed in input order.
 */
export function reduceRuntimeEventBatch(
  state: TimelineState,
  events: readonly RuntimeEvent[],
): TimelineState {
  if (events.length === 0) return state;

  const entities: Record<string, TimelineItem> = { ...state.entities };
  let orderedIds = [...state.orderedIds];
  const activeMessageIdsByRun: Record<string, string> = { ...state.activeMessageIdsByRun };
  const messageIdsByRoute: Record<string, string> = { ...state.messageIdsByRoute };
  let activeCompactionId = state.activeCompactionId;
  let lastOrder = state.lastOrder;
  let assistantSequence = state.assistantSequence;
  let running = state.running;
  let lastError = state.lastError;
  let changed = false;
  const pendingDeltas = new Map<string, MessageDeltaPatch>();
  const pendingToolUpdates = new Map<string, ToolUpdatePatch>();

  const nextOrder = (): number => {
    lastOrder += 1;
    return lastOrder;
  };

  const insert = (item: TimelineItem): void => {
    if (!(item.id in entities)) orderedIds.push(item.id);
    entities[item.id] = item;
    changed = true;
  };

  const remove = (id: string): void => {
    if (!(id in entities)) return;
    delete entities[id];
    orderedIds = orderedIds.filter((candidate) => candidate !== id);
    pendingDeltas.delete(id);
    pendingToolUpdates.delete(id);
    changed = true;
  };

  const applyPendingDelta = (id: string): void => {
    const patch = pendingDeltas.get(id);
    if (!patch) return;
    pendingDeltas.delete(id);
    const item = entities[id];
    if (!item || item.kind !== "message") return;
    entities[id] = {
      ...item,
      text: item.text + patch.text,
      thinking: item.thinking + patch.thinking,
      revision: item.revision + 1,
      updatedAt: patch.timestamp,
      lastSeq: patch.seq,
    };
    changed = true;
  };

  const applyPendingToolUpdate = (id: string): void => {
    const patch = pendingToolUpdates.get(id);
    if (!patch) return;
    pendingToolUpdates.delete(id);
    const item = entities[id];
    if (!item || item.kind !== "tool") return;
    entities[id] = {
      ...item,
      result: patch.result,
      status: "running",
      revision: item.revision + 1,
      updatedAt: patch.timestamp,
      lastSeq: patch.seq,
    };
    changed = true;
  };

  const clearMessageRoutes = (runKey: string, itemId?: string): void => {
    if (activeMessageIdsByRun[runKey] && (!itemId || activeMessageIdsByRun[runKey] === itemId)) {
      delete activeMessageIdsByRun[runKey];
      changed = true;
    }
    for (const [route, routedId] of Object.entries(messageIdsByRoute)) {
      const shouldDelete = itemId
        ? routedId === itemId
        : route.startsWith(`${encodeURIComponent(runKey)}|`);
      if (shouldDelete) {
        delete messageIdsByRoute[route];
        changed = true;
      }
    }
  };

  for (const envelope of events) {
    const { type, payload } = envelope.event;
    const runKey = runtimeRunKey(envelope);

    if (type === "run.started") {
      if (!running) {
        running = true;
        changed = true;
      }
      continue;
    }
    if (type === "run.completed" || type === "run.cancelled" || type === "run.failed") {
      if (running) {
        running = false;
        changed = true;
      }
      clearMessageRoutes(runKey);
      if (type === "run.failed") {
        const error = String(payload.message ?? "运行失败");
        if (lastError !== error) {
          lastError = error;
          changed = true;
        }
      }
      continue;
    }
    if (type === "message_start") {
      const raw = agentMessage(payload.message);
      if (raw?.role !== "assistant") continue;
      const protocolMessageId = protocolMessageIdFromPayload(payload);
      const existingId = protocolMessageId
        ? messageIdsByRoute[messageRoute(runKey, protocolMessageId)]
        : undefined;
      if (existingId) applyPendingDelta(existingId);

      assistantSequence += 1;
      const id = existingId ?? (protocolMessageId
        ? `message:${encodeURIComponent(runKey)}:${encodeURIComponent(protocolMessageId)}`
        : `${runKey}-assistant-${assistantSequence}`);
      const existing = entities[id];
      const order = existing?.order ?? nextOrder();
      const createdAt = existing?.createdAt ?? envelope.timestamp;
      insert({
        id,
        kind: "message",
        order,
        revision: existing ? existing.revision + 1 : 0,
        sessionId: envelope.sessionId,
        runId: envelope.runId,
        createdAt,
        updatedAt: envelope.timestamp,
        lastSeq: envelope.seq,
        role: "assistant",
        text: "",
        thinking: "",
        status: "streaming",
        messageId: protocolMessageId,
      });
      activeMessageIdsByRun[runKey] = id;
      if (protocolMessageId) messageIdsByRoute[messageRoute(runKey, protocolMessageId)] = id;
      continue;
    }
    if (type === "message_update") {
      const kind = String(payload.kind ?? "");
      if (kind !== "text_delta" && kind !== "thinking_delta") continue;
      const delta = typeof payload.delta === "string" ? payload.delta : "";
      if (!delta) continue;
      const id = resolveMessageId(
        activeMessageIdsByRun,
        messageIdsByRoute,
        runKey,
        payload,
      );
      if (!id) continue;
      const patch = pendingDeltas.get(id) ?? {
        text: "",
        thinking: "",
        timestamp: envelope.timestamp,
        seq: envelope.seq,
      };
      if (kind === "text_delta") patch.text += delta;
      else patch.thinking += delta;
      patch.timestamp = envelope.timestamp;
      patch.seq = envelope.seq;
      pendingDeltas.set(id, patch);
      continue;
    }
    if (type === "message_end") {
      const raw = agentMessage(payload.message);
      if (raw?.role !== "assistant") continue;
      const id = resolveMessageId(
        activeMessageIdsByRun,
        messageIdsByRoute,
        runKey,
        payload,
      );
      if (!id) continue;

      // message_end carries the authoritative full snapshot, so a pending
      // delta patch for the same item is superseded rather than written first.
      pendingDeltas.delete(id);
      const item = entities[id];
      if (!item || item.kind !== "message") continue;
      const content = readAgentMessageContent(raw);
      if (!content.text && !content.thinking) {
        remove(id);
      } else {
        entities[id] = {
          ...item,
          ...content,
          status: raw.stop_reason,
          revision: item.revision + 1,
          updatedAt: envelope.timestamp,
          lastSeq: envelope.seq,
        };
        changed = true;
      }
      clearMessageRoutes(runKey, id);
      if (raw.error_message) {
        lastError = raw.error_message;
        changed = true;
      }
      continue;
    }
    if (type === "tool_execution_start") {
      if (payload.tool_name === "request_user_input") continue;
      const toolCallId = nonEmptyString(payload.tool_call_id);
      if (!toolCallId) continue;
      const id = toolItemId(toolCallId);
      const existing = entities[id];
      const existingTool = existing?.kind === "tool" ? existing : undefined;
      insert({
        id,
        kind: "tool",
        order: existingTool?.order ?? nextOrder(),
        revision: existingTool ? existingTool.revision + 1 : 0,
        sessionId: envelope.sessionId,
        runId: envelope.runId,
        createdAt: existingTool?.createdAt ?? envelope.timestamp,
        updatedAt: envelope.timestamp,
        lastSeq: envelope.seq,
        toolCallId,
        name: String(payload.tool_name ?? ""),
        args: payload.args,
        result: existingTool?.result,
        status: "running",
        approval: existingTool?.approval,
      });
      continue;
    }
    if (type === "tool_execution_update") {
      if (payload.tool_name === "request_user_input") continue;
      const toolCallId = nonEmptyString(payload.tool_call_id);
      if (!toolCallId) continue;
      const id = toolItemId(toolCallId);
      const item = entities[id];
      if (!item || item.kind !== "tool") continue;
      pendingToolUpdates.set(id, {
        result: payload.partial_result,
        timestamp: envelope.timestamp,
        seq: envelope.seq,
      });
      continue;
    }
    if (type === "tool_execution_end") {
      if (payload.tool_name === "request_user_input") continue;
      const toolCallId = nonEmptyString(payload.tool_call_id);
      if (!toolCallId) continue;
      const id = toolItemId(toolCallId);
      // The final result is authoritative when an update and end event arrive
      // in the same animation frame.
      pendingToolUpdates.delete(id);
      const item = entities[id];
      if (!item || item.kind !== "tool") continue;
      entities[id] = {
        ...item,
        approval: undefined,
        result: payload.result,
        status: payload.is_error ? "error" : "done",
        revision: item.revision + 1,
        updatedAt: envelope.timestamp,
        lastSeq: envelope.seq,
      };
      changed = true;
      continue;
    }
    if (type === "approval.requested") {
      const approval = approvalFromPayload(payload);
      if (!approval) continue;
      const id = toolItemId(approval.toolCallId);
      const existing = entities[id];
      const existingTool = existing?.kind === "tool" ? existing : undefined;
      insert({
        id,
        kind: "tool",
        order: existingTool?.order ?? nextOrder(),
        revision: existingTool ? existingTool.revision + 1 : 0,
        sessionId: envelope.sessionId,
        runId: envelope.runId,
        createdAt: existingTool?.createdAt ?? envelope.timestamp,
        updatedAt: envelope.timestamp,
        lastSeq: envelope.seq,
        toolCallId: approval.toolCallId,
        name: approval.toolName,
        args: approval.args,
        result: existingTool?.result,
        status: "approval",
        approval,
      });
      continue;
    }
    if (type === "approval.expired") {
      const toolCallId = nonEmptyString(payload.toolCallId);
      if (!toolCallId) continue;
      const id = toolItemId(toolCallId);
      const item = entities[id];
      if (!item || item.kind !== "tool") continue;
      entities[id] = {
        ...item,
        approval: undefined,
        result: "工具审批已超时",
        status: "error",
        revision: item.revision + 1,
        updatedAt: envelope.timestamp,
        lastSeq: envelope.seq,
      };
      changed = true;
      continue;
    }
    if (type === "compaction_start") {
      if (activeCompactionId) continue;
      const order = nextOrder();
      const id = `compaction-${envelope.timestamp}-${order}`;
      activeCompactionId = id;
      insert({
        id,
        kind: "compaction",
        order,
        revision: 0,
        sessionId: envelope.sessionId,
        runId: envelope.runId,
        createdAt: envelope.timestamp,
        updatedAt: envelope.timestamp,
        lastSeq: envelope.seq,
        status: "running",
        reason: String(payload.reason ?? "automatic"),
        summary: "",
      });
      continue;
    }
    if (type === "compaction_end") {
      if (!activeCompactionId) continue;
      const item = entities[activeCompactionId];
      if (!item || item.kind !== "compaction") {
        activeCompactionId = null;
        changed = true;
        continue;
      }
      const error = typeof payload.error === "string" && payload.error.length > 0
        ? payload.error
        : undefined;
      const status: Exclude<TimelineCompactionStatus, "running" | "skipped"> = error
        ? "failed"
        : payload.aborted ? "aborted" : "completed";
      entities[item.id] = {
        ...item,
        status,
        reason: String(payload.reason ?? item.reason),
        summary: String(payload.summary_preview ?? item.summary),
        detail: error,
        revision: item.revision + 1,
        updatedAt: envelope.timestamp,
        lastSeq: envelope.seq,
      };
      activeCompactionId = null;
      changed = true;
    }
  }

  // A message receives one entity replacement for all deltas in this batch.
  for (const id of pendingDeltas.keys()) applyPendingDelta(id);
  // Partial tool results are snapshots; only the latest snapshot in a frame is
  // committed, avoiding a React update for every stdout/progress event.
  for (const id of pendingToolUpdates.keys()) applyPendingToolUpdate(id);

  if (!changed) return state;
  return {
    entities,
    orderedIds,
    activeMessageIdsByRun,
    messageIdsByRoute,
    activeCompactionId,
    lastOrder,
    assistantSequence,
    running,
    lastError,
  };
}

function runtimeRunKey(event: RuntimeEvent): string {
  if (event.runId) return event.runId;
  if (event.sessionId) return `session:${event.sessionId}`;
  return "unscoped";
}

function protocolMessageIdFromPayload(payload: Record<string, unknown>): string | undefined {
  const direct = nonEmptyString(payload.messageId) ?? nonEmptyString(payload.message_id);
  if (direct) return direct;
  const message = payload.message;
  if (!message || typeof message !== "object" || Array.isArray(message)) return undefined;
  const record = message as Record<string, unknown>;
  return nonEmptyString(record.messageId)
    ?? nonEmptyString(record.message_id)
    ?? nonEmptyString(record.id);
}

function messageRoute(runKey: string, messageId: string): string {
  return `${encodeURIComponent(runKey)}|${encodeURIComponent(messageId)}`;
}

function resolveMessageId(
  activeMessageIdsByRun: Readonly<Record<string, string>>,
  messageIdsByRoute: Readonly<Record<string, string>>,
  runKey: string,
  payload: Record<string, unknown>,
): string | undefined {
  const protocolMessageId = protocolMessageIdFromPayload(payload);
  if (protocolMessageId) {
    return messageIdsByRoute[messageRoute(runKey, protocolMessageId)];
  }
  return activeMessageIdsByRun[runKey];
}

function toolItemId(toolCallId: string): string {
  return `tool:${encodeURIComponent(toolCallId)}`;
}

function agentMessage(value: unknown): AgentMessage | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const message = value as Partial<AgentMessage>;
  if (typeof message.role !== "string") return undefined;
  if (typeof message.content !== "string" && !Array.isArray(message.content)) return undefined;
  return message as AgentMessage;
}

function approvalFromPayload(payload: Record<string, unknown>): TimelineApproval | undefined {
  const approvalId = nonEmptyString(payload.approvalId);
  const toolCallId = nonEmptyString(payload.toolCallId);
  const toolName = nonEmptyString(payload.toolName);
  if (!approvalId || !toolCallId || !toolName) return undefined;
  return { approvalId, toolCallId, toolName, args: payload.args };
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}
