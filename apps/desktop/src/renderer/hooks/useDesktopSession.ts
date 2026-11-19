import { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  MemoryStatePayload,
  PermissionMode,
  PermissionModePayload,
  PlanRevisionPayload,
  PlanStatePayload,
  RuntimeEvent,
  SessionInfo,
  SessionSnapshotPayload,
  SessionStats,
  SubagentTaskPayload,
  WebSearchStatePayload,
  WorkspacePayload,
} from "../../shared/types";
import type { ChangeReviewRequest } from "../../shared/workbench";
import type { PendingAttachment } from "../attachments/imageAttachments";
import { useAttachments } from "../attachments/useImageAttachments";
import type { QueueDeliveryMode } from "../components/composer/Composer";
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
} from "../timeline/timelineState";
import {
  PLAN_SUBMISSION_PROMPT,
  type CommandOption,
  type ModelOption,
  type PendingPlanAction,
} from "../utils/format";
import {
  formatMemoryStatusNotice,
  parseMemoryRememberArgs,
  tokenizeCommandArguments,
} from "../utils/slashCommands";

export interface PlanTimelineItem {
  id: string;
  kind: "plan";
  order: number;
  plan: PlanRevisionPayload;
}

export interface ToolGroupTimelineItem {
  id: string;
  kind: "tool-group";
  order: number;
  revision: number;
  tools: TimelineToolItem[];
}

export type RenderTimelineItem =
  | Exclude<TimelineItem, TimelineToolItem>
  | ToolGroupTimelineItem
  | PlanTimelineItem;

interface PlanSubmission {
  sessionId: string;
  runId: string | null;
}

interface AttachmentRunDraft {
  sessionId: string;
  input: string;
  items: PendingAttachment[];
  runId: string | null;
  failed: boolean;
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

export function useDesktopSession() {
  const [workbenchOpen, setWorkbenchOpen] = useState(false);
  const [requestedChange, setRequestedChange] = useState<ChangeReviewRequest | null>(null);
  const [workbenchWidth, setWorkbenchWidth] = useState(480);
  const [usageAnalysisOpen, setUsageAnalysisOpen] = useState(false);
  const [memoryDrawerOpen, setMemoryDrawerOpen] = useState(false);
  const closeWorkbench = useCallback(() => setWorkbenchOpen(false), []);
  const closeMemoryDrawer = useCallback(() => setMemoryDrawerOpen(false), []);

  const [sidecarStatus, setSidecarStatus] = useState("starting");
  const [workspace, setWorkspace] = useState<WorkspacePayload | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [showArchivedSessions, setShowArchivedSessions] = useState(false);
  const archivedViewRef = useRef(false);
  const sessionListRequestRef = useRef(0);
  const workspacePathRef = useRef<string | null>(null);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionListError, setSessionListError] = useState<string | null>(null);
  const [pendingSessionArchive, setPendingSessionArchive] = useState<string | null>(null);
  const sessionArchiveRef = useRef<string | null>(null);
  const restoredSessionFocusRef = useRef<string | null>(null);
  const [timelineState, setTimelineState] = useState<TimelineState>(() => createTimelineState());
  const [commandOptions, setCommandOptions] = useState<CommandOption[]>([]);
  const [commandMenuOpen, setCommandMenuOpen] = useState(false);
  const [selectedCommandIndex, setSelectedCommandIndex] = useState(0);
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([]);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [modelQuery, setModelQuery] = useState("");
  const [input, setInput] = useState("");
  const [queueMode, setQueueMode] = useState<QueueDeliveryMode>("steer");
  const [running, setRunning] = useState(false);
  const [compacting, setCompacting] = useState(false);
  const [pendingSessionNavigation, setPendingSessionNavigation] = useState<string | null>(null);
  const sessionNavigationRef = useRef(false);
  const sessionNavigating = pendingSessionNavigation !== null;
  const [error, setError] = useState<string | null>(null);
  const supportsImages = workspace?.model.input?.includes("image") ?? false;
  const attachments = useAttachments(
    workspace ? JSON.stringify([workspace.path, workspace.sessionId]) : null,
    supportsImages,
    setError,
  );
  const submittingRef = useRef(false);
  const imageRunDraftRef = useRef<AttachmentRunDraft | null>(null);
  const activeRunIdRef = useRef<string | null>(null);
  const [pendingPermissionChange, setPendingPermissionChange] = useState(false);
  const [networkApproval, setNetworkApproval] = useState<{ approvalId: string; message: string } | null>(null);
  const [pendingNetworkDecision, setPendingNetworkDecision] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [customPlanAnswer, setCustomPlanAnswer] = useState("");
  const [supplementingPlanDigest, setSupplementingPlanDigest] = useState<string | null>(null);
  const [planCommandMenuOpen, setPlanCommandMenuOpen] = useState(false);
  const [pendingPlanAction, setPendingPlanAction] = useState<PendingPlanAction>(null);
  const [planSubmissionNotice, setPlanSubmissionNotice] = useState<string | null>(null);
  const planSubmissionRef = useRef<PlanSubmission | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const didBootstrap = useRef(false);
  const rafQueue = useRef<RuntimeEvent[]>([]);
  const rafId = useRef<number | null>(null);
  const localItemSequence = useRef(0);
  const activeCompactionId = useRef<string | null>(null);
  const compactingRef = useRef(false);
  const workspaceSessionIdRef = useRef<string | null>(null);
  const workspaceGenerationRef = useRef(0);

  const setMemoryState = useCallback((memory: MemoryStatePayload) => {
    setWorkspace((current) => (current ? { ...current, memory: { ...current.memory, ...memory } } : current));
  }, []);

  const refreshSessions = async () => {
    const requestId = ++sessionListRequestRef.current;
    const path = workspacePathRef.current;
    setSessionsLoading(true);
    setSessionListError(null);
    try {
      const result = archivedViewRef.current
        ? await window.agent.request<SessionInfo[]>("session.list", { archived: true })
        : await window.agent.request<SessionInfo[]>("session.list");
      if (requestId === sessionListRequestRef.current && path === workspacePathRef.current) setSessions(result);
    } catch (reason) {
      if (requestId === sessionListRequestRef.current && path === workspacePathRef.current) {
        setSessionListError(`加载会话失败：${reason instanceof Error ? reason.message : String(reason)}`);
      }
    } finally {
      if (requestId === sessionListRequestRef.current) setSessionsLoading(false);
    }
  };

  const toggleArchivedSessions = () => {
    archivedViewRef.current = !archivedViewRef.current;
    setShowArchivedSessions(archivedViewRef.current);
    setSessions([]);
    void refreshSessions();
  };

  const setSessionArchived = async (sessionId: string, archived: boolean) => {
    if (sessionArchiveRef.current || sessionNavigationRef.current || running || compacting) return;
    const path = workspacePathRef.current;
    sessionArchiveRef.current = sessionId;
    setPendingSessionArchive(sessionId);
    setError(null);
    try {
      const result = await window.agent.request<{ sessionId: string; archived: boolean }>(
        archived ? "session.archive" : "session.unarchive",
        { sessionId },
      );
      if (path !== workspacePathRef.current) return;
      if (!result.archived) restoredSessionFocusRef.current = result.sessionId;
      if (result.archived && workspaceSessionIdRef.current === result.sessionId) {
        setCommandMenuOpen(false);
        setModelPickerOpen(false);
        setPlanCommandMenuOpen(false);
      }
      setWorkspace((current) => current?.sessionId === result.sessionId
        ? { ...current, archived: result.archived } : current);
      await refreshSessions();
    } catch (reason) {
      if (path !== workspacePathRef.current) return;
      setError(`${archived ? "归档" : "恢复"}会话失败：${reason instanceof Error ? reason.message : String(reason)}`);
      await refreshSessions();
    } finally {
      sessionArchiveRef.current = null;
      setPendingSessionArchive(null);
    }
  };

  useEffect(() => {
    if (pendingSessionArchive || !restoredSessionFocusRef.current) return;
    if (workspace?.sessionId === restoredSessionFocusRef.current && !workspace.archived) composerRef.current?.focus();
    restoredSessionFocusRef.current = null;
  }, [pendingSessionArchive, workspace?.sessionId, workspace?.archived]);

  const refreshCommands = async () => {
    try {
      setCommandOptions(await window.agent.request<CommandOption[]>("command.list"));
    } catch {
      setCommandOptions([]);
    }
  };

  const applyWorkspace = (payload: WorkspacePayload, restoreTimeline = true) => {
    workspaceGenerationRef.current += 1;
    if (workspacePathRef.current !== payload.path) {
      archivedViewRef.current = false;
      setShowArchivedSessions(false);
      setSessions([]);
    }
    workspacePathRef.current = payload.path;
    if (restoreTimeline) {
      // RPC results are already authoritative. A snapshot queued for the old
      // session must not undo a successful switch in the next animation frame.
      // Preserve trailing events belonging to the selected session.
      rafQueue.current = rafQueue.current.filter((event) => (
        !event.sessionId || event.sessionId === payload.sessionId
      ) && (event.event.type !== "session.changed"
        || event.event.payload.sessionId === payload.sessionId));
    }
    if (workspaceSessionIdRef.current !== payload.sessionId) {
      setInput("");
      attachments.clear();
      imageRunDraftRef.current = null;
      activeRunIdRef.current = null;
    }
    workspaceSessionIdRef.current = payload.sessionId;
    setWorkspace(payload);
    if (restoreTimeline) {
      setRequestedChange(null);
      setTimelineState(createTimelineStateFromMessages(payload.messages, payload.sessionId, payload.turnChanges));
    }
    setCommandMenuOpen(false);
    setModelPickerOpen(false);
    setNetworkApproval(null);
    setCustomPlanAnswer("");
    setSupplementingPlanDigest(null);
    setPlanCommandMenuOpen(false);
    setPendingPlanAction(null);
    if (planSubmissionRef.current) setRunning(false);
    planSubmissionRef.current = null;
    setPlanSubmissionNotice(null);
    activeCompactionId.current = null;
    compactingRef.current = false;
    setCompacting(false);
    setIsAtBottom(true);
    setError(payload.turnChangesError ? `历史变更记录暂不可用：${payload.turnChangesError}` : null);
    void refreshSessions();
    void refreshCommands();
  };

  const applySessionSnapshot = (snapshot: SessionSnapshotPayload) => {
    if (workspaceSessionIdRef.current !== snapshot.sessionId) return;
    setWorkspace((current) => {
      if (!current) return current;
      return {
        ...current,
        archived: snapshot.archived ?? current.archived,
        collaborationMode: snapshot.collaborationMode,
        planState: snapshot.planState,
        messages: snapshot.messages,
        turnChanges: snapshot.turnChanges,
        permissionMode: snapshot.permissionMode ?? current.permissionMode,
        webSearch: snapshot.webSearch,
        stats: snapshot.stats,
      };
    });
    setRequestedChange(null);
    setTimelineState(createTimelineStateFromMessages(snapshot.messages, snapshot.sessionId, snapshot.turnChanges));
    if (snapshot.turnChangesError) setError(`历史变更记录暂不可用：${snapshot.turnChangesError}`);
  };

  const refreshSessionSnapshot = async () => {
    const snapshot = await window.agent.request<SessionSnapshotPayload>("session.snapshot");
    applySessionSnapshot(snapshot);
    return snapshot;
  };

  const changePermission = async (mode: PermissionMode) => {
    if (!workspace) return;
    const sessionId = workspace.sessionId;
    setPendingPermissionChange(true);
    try {
      const payload = await window.agent.request<PermissionModePayload>("permission.setMode", { mode });
      if (workspaceSessionIdRef.current === sessionId && payload.sessionId === sessionId) {
        setWorkspace((current) => current ? { ...current, permissionMode: payload.permissionMode } : current);
      }
    } catch (reason) {
      if (workspaceSessionIdRef.current === sessionId) {
        setError(reason instanceof Error ? reason.message : String(reason));
        await refreshSessionSnapshot().catch(() => undefined);
      }
    } finally {
      setPendingPermissionChange(false);
    }
  };

  const toggleWebSearch = async () => {
    if (!workspace?.webSearch || running || compacting || sessionNavigationRef.current) return;
    const sessionId = workspace.sessionId;
    const nextEnabled = !workspace.webSearch.enabled;
    setError(null);
    try {
      const state = await window.agent.request<WebSearchStatePayload>("webSearch.setEnabled", {
        enabled: nextEnabled,
      });
      if (workspaceSessionIdRef.current === sessionId) {
        setWorkspace((current) => current ? { ...current, webSearch: state } : current);
      }
    } catch (reason) {
      if (workspaceSessionIdRef.current === sessionId) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    }
  };

  const resolveNetworkApproval = async (approved: boolean) => {
    if (!networkApproval) return;
    const approvalId = networkApproval.approvalId;
    setPendingNetworkDecision(true);
    try {
      await window.agent.request("approval.resolve", { approvalId, approved });
      setNetworkApproval((current) => current?.approvalId === approvalId ? null : current);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPendingNetworkDecision(false);
    }
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
      if (event.event.type.startsWith("workbench.") && !["workbench.turnChanged", "workbench.turnFailed"].includes(event.event.type)) return;
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
          setRequestedChange(null);
          setTimelineState(createTimelineStateFromMessages(snapshot.messages, snapshot.sessionId, snapshot.turnChanges));
          const trailingEvents = events.slice(sessionChangeIndex + 1).filter((item) => (
            !item.sessionId || item.sessionId === snapshot.sessionId
          ));
          if (trailingEvents.length) {
            setTimelineState((current) => reduceRuntimeEventBatch(current, trailingEvents));
          }
        } else {
          const currentEvents = events.filter((item) => (
            !item.sessionId || item.sessionId === workspaceSessionIdRef.current
          ));
          setTimelineState((current) => reduceRuntimeEventBatch(current, currentEvents));
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
    if (type === "session.archived" || type === "session.unarchived") {
      setWorkspace((current) => current && current.sessionId === payload.sessionId
        ? { ...current, archived: payload.archived === true } : current);
      void refreshSessions();
      return;
    }
    if (type !== "session.changed" && envelope.sessionId
      && envelope.sessionId !== workspaceSessionIdRef.current) return;
    if (type === "workbench.turnFailed") {
      setError(`本轮变更记录暂不可用：${String(payload.message ?? "请稍后重试")}`);
      return;
    }
    const activeSubmission = planSubmissionRef.current;
    if (type.startsWith("run.") && activeSubmission?.runId
      && envelope.runId !== activeSubmission.runId) return;
    if ((type === "session.statsChanged" || type === "run.completed"
      || type === "run.cancelled" || type === "run.failed")
      && envelope.sessionId === workspaceSessionIdRef.current
      && payload.stats && typeof payload.stats === "object") {
      setWorkspace((current) => current && current.sessionId === envelope.sessionId
        ? { ...current, stats: payload.stats as SessionStats } : current);
    }
    if (type === "run.started") {
      if (envelope.runId) activeRunIdRef.current = envelope.runId;
      const imageDraft = imageRunDraftRef.current;
      if (imageDraft && !imageDraft.runId) imageDraft.runId = envelope.runId;
      const submission = planSubmissionRef.current;
      if (submission && !submission.runId) submission.runId = envelope.runId;
      setRunning(true);
      // Plan submission needs a persistent progress affordance after its menu
      // closes. Other actions move immediately to their phase-specific UI.
      setPendingPlanAction((current) => current === "submit" ? current : null);
      return;
    }
    if (type === "run.completed" || type === "run.cancelled" || type === "run.failed") {
      activeRunIdRef.current = null;
      setRunning(false);
      const imageDraft = imageRunDraftRef.current;
      if (imageDraft && (!imageDraft.runId || imageDraft.runId === envelope.runId)) {
        if (type === "run.failed") {
          imageDraft.failed = true;
          attachments.restore(imageDraft.items);
          setInput((current) => current || imageDraft.input);
        }
        imageRunDraftRef.current = null;
      }
      const submission = planSubmissionRef.current;
      if (submission && (!submission.runId || submission.runId === envelope.runId)) {
        void finishPlanSubmission(submission, type);
      } else {
        setPendingPlanAction(null);
      }
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
    if (type === "subagent.stateChanged" && Array.isArray(payload.tasks)) {
      setWorkspace((current) => current ? { ...current, subagents: payload.tasks as SubagentTaskPayload[] } : current);
    }
    if (type === "model.changed") {
      const model = payload as unknown as WorkspacePayload["model"];
      setWorkspace((current) => current ? { ...current, model } : current);
    }
    if (type === "webSearch.changed") {
      const state = payload as unknown as WebSearchStatePayload;
      setWorkspace((current) => current ? { ...current, webSearch: state } : current);
    }
    if (type === "permission.changed") {
      const state = payload as unknown as PermissionModePayload;
      if (state.sessionId === workspaceSessionIdRef.current) {
        setWorkspace((current) => current ? { ...current, permissionMode: state.permissionMode } : current);
      }
    }
    if (type === "permission.networkRequested" && typeof payload.approvalId === "string") {
      setNetworkApproval({ approvalId: payload.approvalId, message: String(payload.message) });
    }
    if (type === "permission.networkResolved") {
      setNetworkApproval((current) => current?.approvalId === payload.approvalId ? null : current);
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
      if (payloadSessionId && payloadSessionId !== workspaceSessionIdRef.current) return;
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
      // A state event is authoritative rehydration, not a request to keep a
      // transient command menu open. Drafting can reopen its actions only
      // through an explicit second /plan command.
      setPlanCommandMenuOpen(false);
      if (state.phase !== "drafting") setPlanSubmissionNotice(null);
      return;
    }
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

  const manageSubagents = async (action: string, value = "") => {
    const owner = workspaceSessionIdRef.current;
    try {
      if (action === "spawn" || action === "review") {
        await window.agent.request("subagent.spawn", {
          task: value,
          purpose: action === "review" ? "review" : "investigate",
        });
      } else if (action === "cancel") {
        await window.agent.request("subagent.cancel", { taskId: value });
      } else if (action === "wait") {
        await window.agent.request("subagent.wait", { taskId: value });
      } else if (action === "status") {
        const task = await window.agent.request<SubagentTaskPayload>("subagent.status", { taskId: value });
        addNotice(`${task.prompt}\n\n${task.output || task.error || task.status}`);
      } else if (action && action !== "list" && action !== "show") {
        addNotice("用法：/subagents list | spawn <任务> | review <任务> | status <id> | wait <id> | show <id> | cancel <id>");
        return;
      }
      const tasks = await window.agent.request<SubagentTaskPayload[]>("subagent.list");
      if (workspaceSessionIdRef.current !== owner) return;
      setWorkspace((current) => current ? { ...current, subagents: tasks } : current);
      setSidebarOpen(true);
      if (action === "show") {
        const task = tasks.find((item) => item.taskId === value);
        addNotice(task ? `${task.prompt}\n\n${task.output || task.error || task.status}` : "当前分支没有这个子代理任务。");
      }
    } catch (reason) {
      if (workspaceSessionIdRef.current !== owner) return;
      setError(reason instanceof Error ? reason.message : String(reason));
      try {
        const tasks = await window.agent.request<SubagentTaskPayload[]>("subagent.list");
        if (workspaceSessionIdRef.current === owner) setWorkspace((current) => current ? { ...current, subagents: tasks } : current);
      } catch {
        // Preserve the last authoritative snapshot if refresh also fails.
      }
    }
  };

  const executeSlashCommand = async (name: string, args = "") => {
    if (sessionNavigationRef.current) return;
    setInput("");
    setCommandMenuOpen(false);
    setError(null);
    try {
      if (name === "model") {
        await openModelPicker(args);
        return;
      }
      if (name === "new") {
        await newSession();
        return;
      }
      if (name === "subagents") {
        const match = args.trim().match(/^(\S+)(?:\s+([\s\S]*))?$/);
        await manageSubagents(match?.[1] ?? "list", match?.[2] ?? "");
        return;
      }
      if (name === "compact") {
        if (compactingRef.current) return;
        beginCompaction(args.trim() ? "pivot" : "manual");
        try {
          const result = args.trim()
            ? await window.agent.request<Record<string, unknown>>("session.compact", { direction: args.trim() })
            : await window.agent.request<Record<string, unknown>>("session.compact");
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
        const owner = workspaceSessionIdRef.current;
        const generation = workspaceGenerationRef.current;
        if (!owner) return;
        let snapshot: SessionSnapshotPayload;
        try {
          snapshot = await window.agent.request<SessionSnapshotPayload>("session.snapshot");
        } catch (reason) {
          if (sessionNavigationRef.current || workspaceSessionIdRef.current !== owner
            || workspaceGenerationRef.current !== generation) return;
          throw reason;
        }
        if (sessionNavigationRef.current || workspaceSessionIdRef.current !== owner
          || workspaceGenerationRef.current !== generation || snapshot.sessionId !== owner) return;
        // A read-only usage query must not replace renderer-local timeline items.
        setWorkspace((current) => current?.sessionId === owner ? { ...current, stats: snapshot.stats } : current);
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
        await handlePlanCommand();
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
          addNotice(formatMemoryStatusNotice(memory));
          return;
        }
        if (action === "remember") {
          const parsed = parseMemoryRememberArgs(parts);
          if (!parsed) {
            addNotice("用法：`/memory remember <内容> [--global|--project]`");
            return;
          }
          const result = await window.agent.request<{
            record: Record<string, unknown> | null;
            memory: WorkspacePayload["memory"];
          }>("memory.remember", parsed);
          setWorkspace((current) => current ? { ...current, memory: result.memory } : current);
          addNotice(result.record
            ? `已记住（${parsed.scope}）：\`${String(result.record.id)}\`。可用 \`/memory forget ${String(result.record.id)}\` 遗忘。`
            : "该内容未产生新的长期记忆。");
          return;
        }
        if (action === "list") {
          const scope = parts.includes("--global") ? "global" : parts.includes("--project") ? "project" : undefined;
          const records = await window.agent.request<Array<{ scope: string; record: Record<string, unknown> }>>(
            "memory.list",
            scope ? { scope } : {},
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
            key,
            scope,
            confirmed: true,
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
            allScopes,
            confirmed: true,
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

  const sendQueuedMessage = async () => {
    const text = input.trim();
    if (
      !text
      || !workspace
      || !running
      || compacting
      || composerBlocked
      || submittingRef.current
      || planSubmissionRef.current
      || sessionNavigationRef.current
      || sessionArchiveRef.current
      || workspace.archived
    ) return;
    const sessionId = workspace.sessionId;
    const originalInput = input;
    const method = queueMode === "steer" ? "run.steer" : "run.followUp";
    setError(null);
    try {
      await window.agent.request(method, {
        text,
        sessionId,
        ...(activeRunIdRef.current ? { runId: activeRunIdRef.current } : {}),
      });
      if (workspaceSessionIdRef.current === sessionId) {
        setInput((current) => current === originalInput ? "" : current);
      }
    } catch (reason) {
      if (workspaceSessionIdRef.current === sessionId) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    }
  };

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    const text = input.trim();
    if ((!text && !attachments.items.length) || !workspace || running || compacting
      || composerBlocked || attachments.reading || submittingRef.current || planSubmissionRef.current
      || sessionNavigationRef.current || sessionArchiveRef.current || workspace.archived) return;
    if (attachments.images.length && !supportsImages) {
      setError("当前模型不支持图片输入，请先切换到支持视觉的模型，或移除附件。");
      return;
    }
    if (text.startsWith("/")) {
      if (attachments.items.length) {
        setError("附件不能随斜杠命令发送，请移除附件或改为普通消息。");
        return;
      }
      const [rawName, ...rest] = text.slice(1).split(/\s+/);
      const command = commandOptions.find((item) => item.name === rawName.toLocaleLowerCase());
      if (command) await executeSlashCommand(command.name, rest.join(" "));
      else setError(`未知命令：/${rawName}`);
      return;
    }
    setError(null);
    const sessionId = workspace.sessionId;
    const originalInput = input;
    const imageDraft: AttachmentRunDraft | null = attachments.items.length ? {
      sessionId, input: originalInput, items: [...attachments.items], runId: null, failed: false,
    } : null;
    imageRunDraftRef.current = imageDraft;
    const images = attachments.images.map(({ data, mime_type }) => ({ data, mime_type }));
    const files = attachments.files.map(({ name, data, mime_type }) => ({ name, data, mime_type }));
    submittingRef.current = true;
    setRunning(true);
    try {
      const result = await window.agent.request<{ runId: string }>("run.start", {
        text, ...(images.length ? { images } : {}), ...(files.length ? { files } : {}),
      });
      if (result.runId) activeRunIdRef.current = result.runId;
      if (imageDraft) imageDraft.runId = result.runId;
      if (workspaceSessionIdRef.current === sessionId && !imageDraft?.failed) {
        setInput((current) => current === originalInput ? "" : current);
        attachments.clear();
      }
    } catch (reason) {
      if (workspaceSessionIdRef.current === sessionId) {
        setRunning(false);
        setError(reason instanceof Error ? reason.message : String(reason));
      }
      if (imageRunDraftRef.current === imageDraft) imageRunDraftRef.current = null;
    } finally {
      submittingRef.current = false;
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

  const handlePlanCommand = async () => {
    if (!workspace || running || compacting) return;
    const phase = workspace.planState.phase;
    const mode = workspace.planState.mode ?? workspace.collaborationMode;

    if (phase === "executing") {
      setPlanCommandMenuOpen(true);
      return;
    }
    if (phase === "uncertain" || phase === "recovery_error") {
      setPlanCommandMenuOpen(false);
      return;
    }

    if (mode !== "plan") {
      setPlanCommandMenuOpen(false);
      await enterPlanMode();
      requestAnimationFrame(() => composerRef.current?.focus());
      return;
    }

    if (phase === "ready") {
      setSupplementingPlanDigest(null);
    }
    setPlanCommandMenuOpen(phase === "drafting");
  };

  const cancelPlan = async () => {
    const planId = workspace?.planState.activePlanId;
    if (!workspace || (!planId && workspace.planState.phase !== "recovery_error")
      || (running && workspace.planState.phase !== "awaiting_answer") || compacting) return;
    try {
      applyWorkspace(await window.agent.request<WorkspacePayload>("plan.cancel", { planId }));
    } catch (reason) {
      try {
        await refreshSessionSnapshot();
      } catch {
        // Preserve the local error when the runtime cannot provide a fresh snapshot.
      }
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
      try {
        await refreshSessionSnapshot();
      } catch {
        // Preserve the local error when the runtime cannot provide a fresh snapshot.
      }
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
        // Keep the RPC error as the actionable message.
      }
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const readyPlan = workspace?.planState.phase === "ready"
    ? workspace.planState.latestRevision
    : null;
  const canHandoffPlan = Boolean(readyPlan && (
    workspace?.planState.canHandoff
    ?? !(workspace?.planState.handoffSourceSessionId || readyPlan.originSessionId)
  ));

  const handoffPlan = async () => {
    const plan = workspace?.planState.latestRevision;
    if (!plan || running || compacting || !canHandoffPlan) return;
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

  const continuePlanDrafting = () => {
    setPlanCommandMenuOpen(false);
    setPlanSubmissionNotice(null);
    requestAnimationFrame(() => composerRef.current?.focus());
  };

  const abortPlanExecution = async () => {
    if (compacting) return;
    let abortError: unknown = null;
    try {
      await window.agent.request("run.abort");
    } catch (reason) {
      abortError = reason;
    }
    try {
      await refreshSessionSnapshot();
    } catch {
      // Keep the last authoritative state when rehydration is unavailable.
    }
    if (abortError !== null) {
      const reason = abortError;
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const abortCompaction = () => {
    void window.agent.request("run.abort").catch((reason) => setError(String(reason)));
  };

  const finishPlanSubmission = async (submission: PlanSubmission, outcome: string) => {
    try {
      const snapshot = await window.agent.request<SessionSnapshotPayload>("session.snapshot");
      if (planSubmissionRef.current !== submission
        || snapshot.sessionId !== submission.sessionId) return;
      applySessionSnapshot(snapshot);
      const stillDrafting = snapshot.planState.phase === "drafting"
        && (snapshot.planState.mode ?? snapshot.collaborationMode) === "plan";
      setPlanCommandMenuOpen(stillDrafting);
      if (stillDrafting && outcome !== "run.failed") {
        setPlanSubmissionNotice(outcome === "run.cancelled"
          ? "已停止整理，尚未提交计划。可以继续规划，或再次整理并提交。"
          : "本轮已结束，但尚未提交计划。可以继续规划，或再次整理并提交。");
      }
    } catch (reason) {
      if (planSubmissionRef.current === submission) {
        const detail = `无法刷新计划状态：${reason instanceof Error ? reason.message : String(reason)}`;
        setError((current) => current ? `${current}；${detail}` : detail);
      }
    } finally {
      if (planSubmissionRef.current === submission) {
        planSubmissionRef.current = null;
        setPendingPlanAction(null);
      }
    }
  };

  const requestPlanSubmission = async () => {
    if (!workspace || running || compacting || planSubmissionRef.current
      || workspace.planState.phase !== "drafting") return;
    const submission: PlanSubmission = { sessionId: workspace.sessionId, runId: null };
    planSubmissionRef.current = submission;
    setPlanCommandMenuOpen(false);
    setPlanSubmissionNotice(null);
    setInput("");
    setError(null);
    setRunning(true);
    setPendingPlanAction("submit");
    try {
      const result = await window.agent.request<{ runId: string }>("run.start", {
        text: PLAN_SUBMISSION_PROMPT,
      });
      if (planSubmissionRef.current === submission) submission.runId = result.runId;
    } catch (reason) {
      if (planSubmissionRef.current !== submission) return;
      setRunning(false);
      setError(reason instanceof Error ? reason.message : String(reason));
      await finishPlanSubmission(submission, "run.failed");
    }
  };

  const selectModel = async (model: ModelOption) => {
    const sessionId = workspaceSessionIdRef.current;
    if (!sessionId) return;
    try {
      const selected = await window.agent.request<WorkspacePayload["model"]>("model.select", {
        modelId: model.id,
        provider: model.provider,
      });
      if (workspaceSessionIdRef.current !== sessionId) return;
      setWorkspace((current) => current ? { ...current, model: selected } : current);
      setModelPickerOpen(false);
      setModelOptions([]);
    } catch (reason) {
      if (workspaceSessionIdRef.current === sessionId) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
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
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "v"
      && !event.repeat && !composerBlocked && !running && !compacting) {
      attachments.prepareClipboardPaste();
    }
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
      if (running) {
        void sendQueuedMessage();
      } else {
        void submit();
      }
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

  const navigateSession = async (sessionId?: string) => {
    if (running || compacting || !workspace || sessionNavigationRef.current || sessionArchiveRef.current
      || (sessionId && sessionId === workspaceSessionIdRef.current)) return;
    sessionNavigationRef.current = true;
    workspaceGenerationRef.current += 1;
    setPendingSessionNavigation(sessionId ?? "new");
    setError(null);
    try {
      const result = sessionId
        ? await window.agent.request<WorkspacePayload>("session.open", { sessionId })
        : await window.agent.request<WorkspacePayload>("session.new");
      if (!sessionId) {
        archivedViewRef.current = false;
        setShowArchivedSessions(false);
      }
      applyWorkspace(result);
      requestAnimationFrame(() => composerRef.current?.focus());
    } catch (reason) {
      const detail = reason instanceof Error ? reason.message : String(reason);
      setError(`${sessionId ? "切换" : "新建"}会话失败：${detail}`);
    } finally {
      sessionNavigationRef.current = false;
      setPendingSessionNavigation(null);
    }
  };
  const openSession = (sessionId: string) => navigateSession(sessionId);
  const newSession = () => navigateSession();

  const statusLabel = useMemo(() => {
    if (sidecarStatus === "ready") return "运行时已连接";
    if (sidecarStatus === "starting") return "正在启动运行时";
    return sidecarStatus.startsWith("error:") ? "运行时异常" : "运行时已断开";
  }, [sidecarStatus]);

  const reviewingPlan = Boolean(
    workspace?.planState.handoffSourceSessionId || readyPlan?.originSessionId,
  );
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
  const conversationReadOnly = Boolean(workspace?.archived) || Boolean(pendingSessionArchive);
  const composerBlocked = !workspace || awaitingPlanDecision || planCommandMenuOpen
    || compacting || sessionNavigating || Boolean(planStatusMessage) || conversationReadOnly;

  const timelineItems = useMemo(() => selectTimelineItems(timelineState), [timelineState]);
  const renderedPlan = ["ready", "uncertain", "recovery_error"].includes(planStatusPhase ?? "")
    ? visiblePlan : null;
  const renderTimelineItems = useMemo<RenderTimelineItem[]>(() => {
    const grouped: RenderTimelineItem[] = [];
    for (const item of timelineItems) {
      if (item.kind === "tool") {
        const previous = grouped[grouped.length - 1];
        if (previous && previous.kind === "tool-group") {
          previous.tools.push(item);
          previous.revision += item.revision + 1;
        } else {
          grouped.push({
            id: `tool-group:${item.id}`,
            kind: "tool-group",
            order: item.order,
            revision: item.revision + 1,
            tools: [item],
          });
        }
        continue;
      }
      grouped.push(item);
    }
    if (renderedPlan) {
      grouped.push({
        id: `plan:${renderedPlan.planId}:${renderedPlan.revision}:${renderedPlan.digest}`,
        kind: "plan",
        order: timelineState.lastOrder + 1,
        plan: renderedPlan,
      });
    }
    return grouped;
  }, [renderedPlan, timelineItems, timelineState.lastOrder]);

  const viewTurnChanges = useCallback((runId: string, path?: string) => {
    setRequestedChange((current) => ({ runId, ...(path ? { path } : {}), serial: (current?.serial ?? 0) + 1 }));
    setWorkbenchOpen(true);
  }, []);

  const lastTimelineSignature = (() => {
    const last = renderTimelineItems.at(-1);
    if (!last) return "empty";
    return last.kind === "plan" ? `${last.id}:${last.plan.digest}` : `${last.id}:${last.revision}`;
  })();

  return {
    // Workspace & layout UI state
    workbenchOpen,
    setWorkbenchOpen,
    closeWorkbench,
    requestedChange,
    workbenchWidth,
    setWorkbenchWidth,
    usageAnalysisOpen,
    setUsageAnalysisOpen,
    memoryDrawerOpen,
    setMemoryDrawerOpen,
    closeMemoryDrawer,
    sidebarOpen,
    setSidebarOpen,
    isAtBottom,
    setIsAtBottom,

    // Sidecar & workspace data
    sidecarStatus,
    statusLabel,
    workspace,
    sessions,
    showArchivedSessions,
    sessionsLoading,
    sessionListError,
    pendingSessionNavigation,
    pendingSessionArchive,
    sessionNavigating,
    running,
    compacting,
    error,
    setError,

    // Timeline
    renderTimelineItems,
    lastTimelineSignature,
    resolveApproval,
    viewTurnChanges,

    // Composer & attachments
    composerRef,
    input,
    setInput,
    queueMode,
    setQueueMode,
    supportsImages,
    attachments,
    composerBlocked,
    conversationReadOnly,
    pendingPermissionChange,
    networkApproval,
    pendingNetworkDecision,
    onInputChange,
    onInputKeyDown,
    submit,
    sendQueuedMessage,
    changePermission,
    resolveNetworkApproval,
    toggleWebSearch,
    setMemoryState,

    // Command palette & Model picker
    commandMenuOpen,
    selectedCommandIndex,
    setSelectedCommandIndex,
    visibleCommands,
    executeSlashCommand,
    modelPickerOpen,
    setModelPickerOpen,
    modelQuery,
    setModelQuery,
    visibleModels,
    openModelPicker,
    selectModel,

    // Plan mode
    readyPlan,
    canHandoffPlan,
    reviewingPlan,
    awaitingPlanDecision,
    planStatusPhase,
    planStatusMessage,
    planBadgeLabel,
    planCommandMenuOpen,
    planSubmissionNotice,
    pendingPlanAction,
    customPlanAnswer,
    setCustomPlanAnswer,
    supplementingPlanDigest,
    enterPlanMode,
    cancelPlan,
    answerPlanQuestion,
    executePlan,
    handoffPlan,
    supplementPlan,
    continuePlanDrafting,
    requestPlanSubmission,
    abortPlanExecution,
    abortCompaction,

    // Sessions & subagents
    chooseWorkspace,
    refreshSessions,
    toggleArchivedSessions,
    setSessionArchived,
    openSession,
    newSession,
    manageSubagents,
  };
}
