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
}

export interface AgentMessage {
  role: string;
  content: string | ContentBlock[];
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
