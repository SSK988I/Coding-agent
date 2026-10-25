export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface RuntimeEvent {
  v: 1;
  type: "event";
  seq: number;
  timestamp: number;
  sessionId: string | null;
  runId: string | null;
  event: {
    type: string;
    payload: Record<string, unknown>;
  };
}

export interface RpcErrorPayload {
  code: string;
  message: string;
  details?: unknown;
}

export interface WorkspacePayload {
  permissionMode?: PermissionMode;
  path: string;
  sessionId: string;
  model: { id: string; name: string; provider: string };
  thinkingLevel: string | null;
  tools: string[];
  messages: AgentMessage[];
  collaborationMode: "default" | "plan";
  planState: PlanStatePayload;
  memory: MemoryStatePayload;
  subagents?: SubagentTaskPayload[];
  stats?: SessionStats;
}

export interface SessionStats {
  tokens: {
    input: number;
    output: number;
    cache_read: number;
    cache_write: number;
    total: number;
  };
  cost: number;
  // Older backends provide token totals without the derived metrics.
  metrics?: {
    cache_hit_rate: number | null;
    input_tokens: number;
    context_tokens: number | null;
    context_window: number | null;
    context_percent: number | null;
    context_estimated: boolean;
  };
}

export interface SubagentTaskPayload {
  taskId: string;
  sessionId: string;
  purpose: "investigate" | "review";
  prompt: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled" | "timed_out" | "uncertain";
  turns: number;
  lastTool: string | null;
  output: string;
  error: string | null;
  maxTurns: number;
  timeoutSeconds: number;
}

export interface MemoryStatePayload {
  enabled: boolean;
  autoExtractEnabled?: boolean;
  userId: string | null;
  projectId: string | null;
  globalCount?: number;
  projectCount?: number;
  conflictCount?: number;
  pendingCount?: number;
  processingCount?: number;
  readyCount?: number;
  failedCount?: number;
  lastError?: string | null;
  root?: string;
}

export type PlanPhase =
  | "idle" | "drafting" | "awaiting_answer" | "ready" | "executing"
  | "completed" | "settled" | "failed" | "aborted" | "cancelled"
  | "uncertain" | "recovery_error";

export interface PlanQuestionOptionPayload {
  label: string;
  description: string;
}

export interface PlanQuestionPayload {
  questionId: string;
  header: string;
  question: string;
  options: PlanQuestionOptionPayload[];
  allowCustom: boolean;
}

export interface PlanRevisionPayload {
  planId: string;
  revision: number;
  title: string;
  markdown: string;
  digest: string;
  sourceMessageId: string;
  schemaVersion?: number;
  submittedByToolCallId?: string | null;
  originSessionId?: string | null;
}

export interface PlanRunPayload {
  planId: string;
  revision: number;
  digest: string;
  status: "started" | "completed" | "failed" | "aborted";
  runId: string | null;
  error: string | null;
  assistantMessageId: string | null;
  stopReason: string | null;
  entryId: string;
  timestamp: string;
}

export interface PlanRecoveryErrorPayload {
  code: string;
  message: string;
  entryId: string;
}

export interface PlanStatePayload {
  mode?: "default" | "plan";
  phase: PlanPhase;
  activePlanId: string | null;
  latestRevision: PlanRevisionPayload | null;
  pendingQuestion: PlanQuestionPayload | null;
  latestRun?: PlanRunPayload | null;
  recoveryError?: PlanRecoveryErrorPayload | null;
  handoffTargetSessionId?: string | null;
  handoffSourceSessionId?: string | null;
  canHandoff?: boolean;
  legacyCandidate?: boolean;
}

export interface SessionSnapshotPayload {
  permissionMode?: PermissionMode;
  sessionId: string;
  messages: AgentMessage[];
  stats: SessionStats;
  collaborationMode: "default" | "plan";
  planState: PlanStatePayload;
  subagents?: SubagentTaskPayload[];
}

export type PermissionMode = "ask" | "auto" | "full_access";

export interface PermissionModePayload {
  sessionId: string;
  permissionMode: PermissionMode;
}

export interface AgentMessage {
  tool_call_id?: string;
  tool_name?: string;
  details?: unknown;
  is_error?: boolean;
  status?: string | null;
  role: string;
  content: string | ContentBlock[];
  summary?: string;
  tokens_before?: number;
  tokensBefore?: number;
  timestamp?: number;
  stop_reason?: string;
  error_message?: string | null;
}

export interface ContentBlock {
  type: string;
  text?: string;
  thinking?: string;
  name?: string;
  id?: string;
  arguments?: Record<string, unknown>;
}

export interface SessionInfo {
  id: string;
  name: string | null;
  first_message: string;
  message_count: number;
  modified: number;
}

export interface BootstrapInfo {
  defaultWorkspace: string;
  platform: string;
}

export interface DesktopBridge {
  request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
  onEvent(listener: (event: RuntimeEvent) => void): () => void;
  onStatus(listener: (status: string) => void): () => void;
}

export interface DesktopShell {
  workbenchRequest?<T = unknown>(method: string, params: Record<string, unknown>): Promise<T>;
  onWorkbenchEvent?(listener: (event: import("./workbench").WorkbenchNativeEvent) => void): () => void;
  chooseWorkspace(): Promise<string | null>;
  getBootstrap(): Promise<BootstrapInfo>;
}
