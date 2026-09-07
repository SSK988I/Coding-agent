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
  path: string;
  sessionId: string;
  model: { id: string; name: string; provider: string };
  thinkingLevel: string | null;
  tools: string[];
  messages: AgentMessage[];
  collaborationMode: "default" | "plan";
  planState: PlanStatePayload;
  memory: MemoryStatePayload;
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
  legacyCandidate?: boolean;
}

export interface SessionSnapshotPayload {
  sessionId: string;
  messages: AgentMessage[];
  stats: Record<string, unknown>;
  collaborationMode: "default" | "plan";
  planState: PlanStatePayload;
}

export interface AgentMessage {
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
  chooseWorkspace(): Promise<string | null>;
  getBootstrap(): Promise<BootstrapInfo>;
}
