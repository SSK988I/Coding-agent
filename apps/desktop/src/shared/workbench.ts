import type { AgentMessage } from "./types";

export interface WorkbenchScope { workspacePath: string; sessionId: string }
export type WorkbenchTool = "chat" | "changes" | "terminal" | "browser" | "files";
export type ChangeSource = "unstaged" | "staged" | "branch" | "session" | "turn";
export interface ChangedFile { path: string; status: string; additions: number; deletions: number; binary: boolean; diff: string }
export interface ChangesSnapshot extends WorkbenchScope {
  source: ChangeSource; files: ChangedFile[]; additions: number; deletions: number;
  branch: string; branches: string[]; baseRef: string | null; truncated: boolean;
}
export interface FileEntry { name: string; path: string; kind: "directory" | "file"; size: number; symlink: boolean; blocked: boolean }
export interface FileListing extends WorkbenchScope { path: string; entries: FileEntry[]; truncated: boolean }
export interface FilePreview extends WorkbenchScope { path: string; size: number; mime: string; text: string | null; image: string | null; binary: boolean; truncated: boolean }
export interface SideConversationSnapshot {
  conversationId: string; messages: AgentMessage[]; streaming: string;
  status: "idle" | "running" | "settled" | "failed" | "cancelled" | "uncertain";
  error: string | null; approval: { approvalId: string; toolName: string; args: Record<string, unknown> } | null;
}
export interface TerminalSnapshot extends WorkbenchScope {
  id: string; key: string; backlog: string; offset: number; exited: boolean; exitCode: number | null;
}
export interface BrowserSnapshot extends WorkbenchScope {
  key: string;
  url: string; title: string; loading: boolean; canGoBack: boolean; canGoForward: boolean; error: string | null;
}
export interface WorkbenchNativeEvent extends WorkbenchScope {
  type: "terminal.output" | "terminal.exit" | "browser.changed";
  id?: string; key?: string; data?: string; offset?: number; exitCode?: number;
  browser?: BrowserSnapshot;
}
