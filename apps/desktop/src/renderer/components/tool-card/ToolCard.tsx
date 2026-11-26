import {
  memo,
  useMemo,
  useState,
  type ComponentType,
} from "react";
import {
  ChevronDown,
  ChevronRight,
  Code2,
  Cpu,
  FilePenLine,
  FileText,
  GitBranch,
  Globe,
  Image as ImageIcon,
  Monitor,
  Search,
} from "lucide-react";

import "./ToolCard.css";
import type { ToolDisplayStatus } from "../../../shared/toolStatus";
import type { ImageAttachmentPayload } from "../../../shared/types";
import { MessageImages } from "../../attachments/ImageAttachmentPicker";

export type ToolCardStatus = ToolDisplayStatus;
export type ToolCategory = "shell" | "read" | "write" | "search" | "generic";

export interface ToolApproval {
  approvalId: string;
  toolCallId: string;
  toolName: string;
  args: unknown;
}

/**
 * Structural view model used by ToolCard. App-level tool records can be passed
 * directly as long as they expose these fields; no dependency on App.tsx is
 * required.
 */
export interface ToolCardData {
  id: string;
  name: string;
  args: unknown;
  result?: unknown;
  status: ToolCardStatus;
  approval?: ToolApproval;
  cwd?: string;
}

export interface ToolOutputLimit {
  maxCharacters: number;
  maxLines: number;
}

export interface ToolRendererProps {
  tool: ToolCardData;
  cwd?: string;
  outputLimit: ToolOutputLimit;
}

export interface ToolRendererDefinition {
  id: string;
  category: ToolCategory;
  matches: (toolName: string) => boolean;
  component: ComponentType<ToolRendererProps>;
  priority?: number;
}

export interface ToolCardProps {
  tool: ToolCardData;
  cwd?: string;
  outputLimit?: Partial<ToolOutputLimit>;
  registry?: ToolRendererRegistry;
  onResolveApproval?: (approval: ToolApproval, approved: boolean) => void | Promise<void>;
  approvalPending?: boolean;
  chainedPrev?: boolean;
  chainedNext?: boolean;
  defaultCollapsed?: boolean;
  nested?: boolean;
}

export interface TruncatedToolOutput {
  text: string;
  truncated: boolean;
  omittedCharacters: number;
  omittedLines: number;
}

export interface DiffLine {
  kind: "meta" | "hunk" | "addition" | "deletion" | "context" | "note";
  text: string;
}

interface DiffPreview {
  text: string;
  source: "result" | "args" | "generated";
}

const DEFAULT_OUTPUT_LIMIT: ToolOutputLimit = {
  maxCharacters: 12_000,
  maxLines: 300,
};

const STATUS_LABELS: Record<ToolCardStatus, string> = {
  pending: "准备中",
  running: "执行中",
  approval: "等待审批",
  done: "已完成",
  error: "失败",
  cancelled: "已取消",
  timed_out: "已超时",
  blocked: "未获准执行",
  uncertain: "结果未知",
};

const SHELL_ALIASES = new Set([
  "bash",
  "shell",
  "terminal",
  "exec",
  "exec_command",
  "run_command",
  "shell_command",
]);
const READ_ALIASES = new Set(["read", "read_file", "file_read", "view_image"]);
const WRITE_ALIASES = new Set([
  "write",
  "edit",
  "patch",
  "apply_patch",
  "write_file",
  "edit_file",
  "file_write",
  "file_edit",
  "replace",
]);
const SEARCH_ALIASES = new Set([
  "grep",
  "rg",
  "search",
  "code_search",
  "find",
  "glob",
  "file_search",
  "search_files",
]);
const GIT_READ_ALIASES = new Set(["git_status", "git_log", "git_diff", "git_show"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function normalizeToolName(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

function normalizeKey(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function directValue(value: unknown, keys: readonly string[]): unknown {
  if (!isRecord(value)) return undefined;
  const normalizedKeys = new Set(keys.map(normalizeKey));
  for (const [key, item] of Object.entries(value)) {
    if (normalizedKeys.has(normalizeKey(key))) return item;
  }
  return undefined;
}

function findString(values: readonly unknown[], keys: readonly string[], maxDepth = 4): string | undefined {
  const normalizedKeys = new Set(keys.map(normalizeKey));
  const seen = new Set<unknown>();
  const queue = values.map((value) => ({ value, depth: 0 }));

  for (let index = 0; index < queue.length; index += 1) {
    const entry = queue[index];
    if (!entry || entry.value === null || typeof entry.value !== "object") continue;
    if (seen.has(entry.value)) continue;
    seen.add(entry.value);

    if (Array.isArray(entry.value)) {
      if (entry.depth < maxDepth) {
        for (const item of entry.value) queue.push({ value: item, depth: entry.depth + 1 });
      }
      continue;
    }

    for (const [key, item] of Object.entries(entry.value)) {
      if (normalizedKeys.has(normalizeKey(key)) && typeof item === "string" && item.length > 0) {
        return item;
      }
    }
    if (entry.depth < maxDepth) {
      for (const item of Object.values(entry.value)) {
        if (item !== null && typeof item === "object") {
          queue.push({ value: item, depth: entry.depth + 1 });
        }
      }
    }
  }
  return undefined;
}

function findPrimitive(values: readonly unknown[], keys: readonly string[]): string | undefined {
  for (const value of values) {
    const item = directValue(value, keys);
    if (typeof item === "string" || typeof item === "number" || typeof item === "boolean") {
      return String(item);
    }
  }
  return undefined;
}

function textContent(value: unknown): string {
  if (!isRecord(value) || !Array.isArray(value.content)) return "";
  return value.content
    .flatMap((item) => {
      if (typeof item === "string") return [item];
      if (isRecord(item) && typeof item.text === "string") return [item.text];
      return [];
    })
    .filter(Boolean)
    .join("\n");
}

function imageContentBlocks(value: unknown): Record<string, unknown>[] {
  if (!isRecord(value) || !Array.isArray(value.content)) return [];
  return value.content.filter((item): item is Record<string, unknown> => isRecord(item) && item.type === "image");
}

export function formatToolOutput(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;

  const content = textContent(value);
  if (content) return content;
  const imageCount = imageContentBlocks(value).length;
  if (imageCount > 0) return `已返回 ${imageCount} 张图片。`;

  if (isRecord(value)) {
    const streams = ["output", "stdout", "stderr"]
      .flatMap((key) => typeof value[key] === "string" ? [String(value[key])] : [])
      .filter(Boolean);
    if (streams.length > 0) return streams.join("\n");
  }

  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

/** Classifies both the project's built-in tools and common MCP/tool aliases. */
export function classifyToolName(toolName: string): ToolCategory {
  const normalized = normalizeToolName(toolName);
  if (SHELL_ALIASES.has(normalized)) return "shell";
  if (READ_ALIASES.has(normalized)) return "read";
  if (WRITE_ALIASES.has(normalized)) return "write";
  if (SEARCH_ALIASES.has(normalized)) return "search";

  const tokens = new Set(normalized.split("_").filter(Boolean));
  if (tokens.has("bash") || tokens.has("shell") || tokens.has("terminal")) return "shell";
  if (tokens.has("grep") || tokens.has("search") || tokens.has("glob") || tokens.has("find")) return "search";
  if (tokens.has("write") || tokens.has("edit") || tokens.has("patch") || tokens.has("replace")) return "write";
  if (tokens.has("read")) return "read";
  return "generic";
}

export function truncateToolOutput(
  value: string,
  limit: ToolOutputLimit = DEFAULT_OUTPUT_LIMIT,
): TruncatedToolOutput {
  const maxLines = Math.max(2, Math.floor(limit.maxLines));
  const maxCharacters = Math.max(80, Math.floor(limit.maxCharacters));
  let text = value;
  let omittedLines = 0;
  let omittedCharacters = 0;

  const lines = text.split(/\r?\n/);
  if (lines.length > maxLines) {
    const headCount = Math.ceil(maxLines * 0.65);
    const tailCount = maxLines - headCount;
    omittedLines = lines.length - maxLines;
    text = [
      ...lines.slice(0, headCount),
      `… [可视截断：省略 ${omittedLines} 行] …`,
      ...lines.slice(lines.length - tailCount),
    ].join("\n");
  }

  if (text.length > maxCharacters) {
    const markerReserve = 48;
    const retained = Math.max(32, maxCharacters - markerReserve);
    const headCount = Math.ceil(retained * 0.65);
    const tailCount = retained - headCount;
    omittedCharacters = text.length - headCount - tailCount;
    text = `${text.slice(0, headCount)}\n… [可视截断：省略 ${omittedCharacters} 个字符] …\n${text.slice(-tailCount)}`;
  }

  return {
    text,
    truncated: omittedLines > 0 || omittedCharacters > 0,
    omittedCharacters,
    omittedLines,
  };
}

function normalizeDiffPath(path: string): string {
  return path.replaceAll("\\", "/").replace(/^\.\//, "");
}

function splitDiffContent(value: string): string[] {
  const lines = value.replace(/\r\n?/g, "\n").split("\n");
  if (lines.at(-1) === "") lines.pop();
  return lines;
}

function generatedWriteDiff(path: string, content: string): string {
  const lines = splitDiffContent(content);
  const target = normalizeDiffPath(path || "untitled");
  const header = [`--- /dev/null`, `+++ b/${target}`, `@@ -0,0 +1,${lines.length} @@`];
  return [...header, ...lines.map((line) => `+${line}`)].join("\n");
}

function generatedEditDiff(path: string, editsValue: unknown): string | undefined {
  if (!Array.isArray(editsValue)) return undefined;
  const edits = editsValue.filter(isRecord);
  if (edits.length === 0) return undefined;

  const target = normalizeDiffPath(path || "unknown");
  const result = [`--- a/${target}`, `+++ b/${target}`];
  for (const edit of edits) {
    const oldText = directValue(edit, ["oldText", "old_text"]);
    const newText = directValue(edit, ["newText", "new_text"]);
    if (typeof oldText !== "string" || typeof newText !== "string") continue;
    const oldLines = splitDiffContent(oldText);
    const newLines = splitDiffContent(newText);
    result.push(`@@ -1,${oldLines.length} +1,${newLines.length} @@`);
    result.push(...oldLines.map((line) => `-${line}`));
    result.push(...newLines.map((line) => `+${line}`));
  }
  return result.length > 2 ? result.join("\n") : undefined;
}

function looksLikeUnifiedDiff(value: string): boolean {
  return /(^|\n)---\s+.+\n\+\+\+\s+.+(?:\n|$)/.test(value)
    || /(^|\n)diff --git\s+/.test(value);
}

function diffFromStringValue(value: unknown): string | undefined {
  if (typeof value === "string" && looksLikeUnifiedDiff(value)) return value;
  const content = textContent(value);
  return content && looksLikeUnifiedDiff(content) ? content : undefined;
}

function extractDiffPreview(tool: ToolCardData): DiffPreview | undefined {
  const resultDiff = findString([tool.result], ["patch", "diff", "unifiedDiff", "unified_diff"])
    ?? diffFromStringValue(tool.result);
  if (resultDiff) return { text: resultDiff, source: "result" };

  const argsDiff = findString([tool.args], ["patch", "diff", "unifiedDiff", "unified_diff"])
    ?? diffFromStringValue(tool.args);
  if (argsDiff) return { text: argsDiff, source: "args" };

  const path = findString([tool.args, tool.result], [
    "path",
    "file",
    "filePath",
    "file_path",
    "filename",
    "targetPath",
    "target_path",
  ]) ?? "unknown";
  const edits = directValue(tool.args, ["edits"]);
  const editDiff = generatedEditDiff(path, edits);
  if (editDiff) return { text: editDiff, source: "generated" };

  const content = directValue(tool.args, ["content", "data", "text"]);
  if (typeof content === "string" && classifyToolName(tool.name) === "write") {
    return { text: generatedWriteDiff(path, content), source: "generated" };
  }
  return undefined;
}

export function parseUnifiedDiff(value: string): DiffLine[] {
  return value.replace(/\r\n?/g, "\n").split("\n").map((text) => {
    if (text.startsWith("diff --git ") || text.startsWith("index ") || text.startsWith("--- ") || text.startsWith("+++ ")) {
      return { kind: "meta", text };
    }
    if (text.startsWith("@@")) return { kind: "hunk", text };
    if (text.startsWith("+")) return { kind: "addition", text };
    if (text.startsWith("-")) return { kind: "deletion", text };
    if (text.startsWith("\\")) return { kind: "note", text };
    return { kind: "context", text };
  });
}

function pathFromDiff(diff: string): string | undefined {
  const match = diff.match(/(?:^|\n)\+\+\+\s+(?:b\/)?([^\n\r]+)/);
  const value = match?.[1]?.trim();
  return value && value !== "/dev/null" ? value : undefined;
}

function toolPath(tool: ToolCardData, diff?: DiffPreview): string | undefined {
  return findString([tool.args, tool.result], [
    "path",
    "file",
    "filePath",
    "file_path",
    "filename",
    "targetPath",
    "target_path",
  ]) ?? (diff ? pathFromDiff(diff.text) : undefined);
}

/**
 * Lightweight args-only summary for the collapsed header row.
 * Never reads `tool.result` so memoized OutputBlock formatting runs once.
 */
function argsHeaderHint(tool: ToolCardData): string | undefined {
  if (!isRecord(tool.args)) return undefined;
  const cmd = directValue(tool.args, ["command", "cmd", "script"]);
  if (typeof cmd === "string" && cmd.trim()) return cmd.trim().split(/\r?\n/, 1)[0];
  const path = directValue(tool.args, ["path", "file", "filePath", "file_path", "targetPath", "target_path"]);
  if (typeof path === "string" && path.trim()) return path.trim();
  const query = directValue(tool.args, ["query", "pattern", "search", "glob"]);
  if (typeof query === "string" && query.trim()) return query.trim();
  return undefined;
}

function ToolCategoryIcon({ name, category }: { name: string; category: ToolCategory }) {
  const normalized = normalizeToolName(name);
  if (normalized === "view_image") return <ImageIcon size={15} strokeWidth={1.55} />;
  if (normalized === "web_search") return <Globe size={15} strokeWidth={1.55} />;
  if (GIT_READ_ALIASES.has(normalized)) return <GitBranch size={15} strokeWidth={1.55} />;
  if (category === "shell") return <Monitor size={15} strokeWidth={1.55} />;
  if (category === "read") return <FileText size={15} strokeWidth={1.55} />;
  if (category === "write") return <FilePenLine size={15} strokeWidth={1.55} />;
  if (category === "search") return <Search size={15} strokeWidth={1.55} />;
  return <Cpu size={15} strokeWidth={1.55} />;
}

function OutputBlock({
  value,
  limit,
  className = "",
  emptyText = "暂无输出",
}: {
  value: unknown;
  limit: ToolOutputLimit;
  className?: string;
  emptyText?: string;
}) {
  const output = useMemo(() => truncateToolOutput(formatToolOutput(value), limit), [value, limit]);
  if (!output.text) return <div className="tool-card-view__empty">{emptyText}</div>;
  return (
    <pre className={`tool-card-view__output ${className}`.trim()}>{output.text}</pre>
  );
}

function ShellToolRenderer({ tool, cwd, outputLimit }: ToolRendererProps) {
  const command = findString([tool.args], ["command", "cmd", "script"]) ?? formatToolOutput(tool.args);
  const workingDirectory = cwd
    ?? tool.cwd
    ?? findString([tool.args, tool.result], ["cwd", "workdir", "workingDirectory", "working_directory"])
    ?? "当前工作区";
  const rawOutput = formatToolOutput(tool.result);
  const preview = useMemo(() => truncateToolOutput(rawOutput, outputLimit), [rawOutput, outputLimit]);
  const [expanded, setExpanded] = useState(true);
  const [showFullOutput, setShowFullOutput] = useState(false);
  const visibleOutput = showFullOutput ? rawOutput : preview.text;

  return (
    <div className="tool-card-view__body tool-card-view__shell">
      <dl className="tool-card-view__metadata">
        <div>
          <dt>命令</dt>
          <dd><code>{command || "等待命令"}</code></dd>
        </div>
        <div>
          <dt>工作目录</dt>
          <dd title={workingDirectory}>{workingDirectory}</dd>
        </div>
      </dl>

      <div className="tool-card-view__output-header">
        <span>纯文本 · 终端输出</span>
        <div className="tool-card-view__output-actions">
          {expanded && preview.truncated && (
            <button type="button" onClick={() => setShowFullOutput((value) => !value)}>
              {showFullOutput ? "恢复截断" : "显示完整输出"}
            </button>
          )}
          <button
            type="button"
            aria-expanded={expanded}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? "收起输出" : "展开输出"}
          </button>
        </div>
      </div>
      {expanded && (
        rawOutput
          ? <pre className="tool-card-view__output tool-card-view__output--shell" data-testid="shell-output">{visibleOutput}</pre>
          : <div className="tool-card-view__empty">{tool.status === "running" ? "等待命令输出…" : "无命令输出"}</div>
      )}
    </div>
  );
}

function ReadToolRenderer({ tool, outputLimit }: ToolRendererProps) {
  const path = toolPath(tool);
  const offset = findPrimitive([tool.args], ["offset"]);
  const limit = findPrimitive([tool.args], ["limit"]);
  return (
    <div className="tool-card-view__body">
      <div className="tool-card-view__resource-row">
        <span>文件</span>
        <code title={path}>{path ?? "未知路径"}</code>
        {(offset || limit) && <small>{offset ? `从第 ${offset} 行` : ""}{limit ? ` · ${limit} 行` : ""}</small>}
      </div>
      <OutputBlock value={tool.result} limit={outputLimit} emptyText="等待文件内容…" />
    </div>
  );
}

function ImageToolRenderer({ tool, outputLimit }: ToolRendererProps) {
  const path = toolPath(tool);
  const blocks = imageContentBlocks(tool.result);
  const images: ImageAttachmentPayload[] = blocks.flatMap((block) => (
    typeof block.data === "string" && typeof block.mime_type === "string"
      ? [{ data: block.data, mime_type: block.mime_type }] : []
  ));
  // Image bytes belong only in the validated img source, never a JSON dump.
  const text = typeof tool.result === "string" ? tool.result : textContent(tool.result);
  return (
    <div className="tool-card-view__body">
      <div className="tool-card-view__resource-row">
        <span>图片</span>
        <code title={path}>{path ?? "未知路径"}</code>
      </div>
      {blocks.length > 0 && <p className="tool-card-view__caption">已返回 {blocks.length} 张图片。</p>}
      {images.length > 0 && <MessageImages images={images} />}
      {text && <OutputBlock value={text} limit={outputLimit} />}
      {!text && blocks.length === 0 && <div className="tool-card-view__empty">等待图片内容…</div>}
    </div>
  );
}

function DiffView({ preview }: { preview: DiffPreview }) {
  const lines = useMemo(() => parseUnifiedDiff(preview.text), [preview.text]);
  return (
    <div className="tool-card-view__diff" role="region" aria-label="文件差异">
      <div className="tool-card-view__diff-toolbar">
        <span>Unified diff</span>
        {preview.source === "generated" && <small>根据工具参数生成预览</small>}
      </div>
      <code>
        {lines.map((line, index) => (
          <span
            className={`tool-card-view__diff-line tool-card-view__diff-line--${line.kind}`}
            key={`${index}-${line.text}`}
          >
            {line.text || " "}
          </span>
        ))}
      </code>
    </div>
  );
}

function WriteToolRenderer({ tool, outputLimit }: ToolRendererProps) {
  const preview = useMemo(() => extractDiffPreview(tool), [tool]);
  const path = toolPath(tool, preview);
  const summary = formatToolOutput(tool.result);
  return (
    <div className="tool-card-view__body">
      <div className="tool-card-view__resource-row">
        <span>文件</span>
        <code title={path}>{path ?? "未知路径"}</code>
      </div>
      {summary && !looksLikeUnifiedDiff(summary) && (
        <OutputBlock value={summary} limit={outputLimit} className="tool-card-view__output--summary" />
      )}
      {preview
        ? <DiffView preview={preview} />
        : <div className="tool-card-view__empty">暂无可展示的差异</div>}
    </div>
  );
}

function SearchToolRenderer({ tool, outputLimit }: ToolRendererProps) {
  const pattern = findString([tool.args], ["pattern", "query", "search", "glob"]);
  const path = toolPath(tool) ?? findString([tool.args], ["root", "directory", "cwd"]);
  const glob = findString([tool.args], ["glob", "include", "filePattern", "file_pattern"]);
  return (
    <div className="tool-card-view__body">
      <dl className="tool-card-view__metadata tool-card-view__metadata--compact">
        <div><dt>查找</dt><dd><code>{pattern ?? "未提供模式"}</code></dd></div>
        <div><dt>范围</dt><dd>{path ?? "当前工作区"}{glob && glob !== pattern ? ` · ${glob}` : ""}</dd></div>
      </dl>
      <OutputBlock value={tool.result} limit={outputLimit} emptyText="等待检索结果…" />
    </div>
  );
}

interface WebSearchItem {
  title: string;
  url: string;
  snippet?: string;
  source?: string;
}

function extractWebSearchItems(value: unknown): WebSearchItem[] {
  if (!isRecord(value)) return [];
  const details = isRecord(value.details) ? value.details : value;
  const rawResults = details.results;
  if (!Array.isArray(rawResults)) return [];
  return rawResults.flatMap((entry): WebSearchItem[] => {
    if (!isRecord(entry)) return [];
    const title = typeof entry.title === "string" ? entry.title : "";
    const url = typeof entry.url === "string" ? entry.url : "";
    if (!title || !/^https?:\/\//i.test(url)) return [];
    return [{
      title,
      url,
      snippet: typeof entry.snippet === "string" ? entry.snippet : undefined,
      source: typeof entry.source === "string" ? entry.source : undefined,
    }];
  });
}

function WebSearchToolRenderer({ tool, outputLimit }: ToolRendererProps) {
  const query = findString([tool.args], ["query", "pattern", "search"]);
  const results = useMemo(() => extractWebSearchItems(tool.result), [tool.result]);
  return (
    <div className="tool-card-view__body">
      <div className="tool-card-view__resource-row">
        <span>联网检索</span>
        <code title={query}>{query ?? "未提供查询词"}</code>
        {results.length > 0 && <small>{results.length} 条来源</small>}
      </div>
      {results.length > 0 ? (
        <ul className="tool-card-view__sources">
          {results.map((item, index) => (
            <li key={`${item.url}-${index}`}>
              <div className="tool-card-view__source-title">{item.title}</div>
              <div className="tool-card-view__source-url">{item.url}</div>
              {item.snippet && <p className="tool-card-view__source-snippet">{item.snippet}</p>}
            </li>
          ))}
        </ul>
      ) : (
        <OutputBlock value={tool.result} limit={outputLimit} emptyText="正在联网检索…" />
      )}
    </div>
  );
}

function GitToolRenderer({ tool, outputLimit }: ToolRendererProps) {
  const diff = useMemo(() => diffFromStringValue(tool.result), [tool.result]);
  const target = toolPath(tool);
  return (
    <div className="tool-card-view__body">
      {target && (
        <div className="tool-card-view__resource-row">
          <span>Git</span>
          <code title={target}>{target}</code>
        </div>
      )}
      {diff
        ? <DiffView preview={{ text: diff, source: "result" }} />
        : <OutputBlock value={tool.result} limit={outputLimit} emptyText="等待 Git 输出…" />}
    </div>
  );
}

function GenericToolRenderer({ tool, outputLimit }: ToolRendererProps) {
  return (
    <div className="tool-card-view__body">
      <OutputBlock
        value={tool.result ?? tool.args}
        limit={outputLimit}
        emptyText="暂无工具数据"
      />
    </div>
  );
}

export const BUILTIN_TOOL_RENDERERS: readonly ToolRendererDefinition[] = [
  {
    id: "image",
    category: "read",
    matches: (name) => normalizeToolName(name) === "view_image",
    component: ImageToolRenderer,
    priority: 110,
  },
  {
    id: "web-search",
    category: "search",
    matches: (name) => normalizeToolName(name) === "web_search",
    component: WebSearchToolRenderer,
    priority: 105,
  },
  {
    id: "shell",
    category: "shell",
    matches: (name) => classifyToolName(name) === "shell",
    component: ShellToolRenderer,
    priority: 100,
  },
  {
    id: "git-read",
    category: "read",
    matches: (name) => GIT_READ_ALIASES.has(normalizeToolName(name)),
    component: GitToolRenderer,
    priority: 95,
  },
  {
    id: "read",
    category: "read",
    matches: (name) => classifyToolName(name) === "read",
    component: ReadToolRenderer,
    priority: 90,
  },
  {
    id: "write-edit",
    category: "write",
    matches: (name) => classifyToolName(name) === "write",
    component: WriteToolRenderer,
    priority: 80,
  },
  {
    id: "search-glob",
    category: "search",
    matches: (name) => classifyToolName(name) === "search",
    component: SearchToolRenderer,
    priority: 70,
  },
  {
    id: "generic",
    category: "generic",
    matches: () => true,
    component: GenericToolRenderer,
    priority: -1_000,
  },
] as const;

/**
 * Priority-based renderer registry. Product- or MCP-specific cards can be
 * registered ahead of the generic fallback without changing ToolCard.
 */
export class ToolRendererRegistry {
  private definitions: ToolRendererDefinition[];

  constructor(definitions: readonly ToolRendererDefinition[] = BUILTIN_TOOL_RENDERERS) {
    this.definitions = [...definitions];
    this.sort();
  }

  register(definition: ToolRendererDefinition): () => void {
    this.unregister(definition.id);
    this.definitions.push(definition);
    this.sort();
    return () => this.unregister(definition.id);
  }

  unregister(id: string): void {
    this.definitions = this.definitions.filter((definition) => definition.id !== id);
  }

  resolve(toolName: string): ToolRendererDefinition {
    return this.definitions.find((definition) => definition.matches(toolName))
      ?? BUILTIN_TOOL_RENDERERS[BUILTIN_TOOL_RENDERERS.length - 1];
  }

  list(): readonly ToolRendererDefinition[] {
    return [...this.definitions];
  }

  private sort(): void {
    this.definitions.sort((left, right) => (right.priority ?? 0) - (left.priority ?? 0));
  }
}

export const defaultToolRendererRegistry = new ToolRendererRegistry();

function statusTone(status: ToolCardStatus): "ok" | "warn" | "err" | "idle" {
  if (status === "done") return "ok";
  if (status === "approval" || status === "uncertain" || status === "pending") return "warn";
  if (status === "error" || status === "timed_out" || status === "blocked") return "err";
  return "idle";
}

export function isFileReadTool(toolName: string): boolean {
  const normalized = normalizeToolName(toolName);
  if (normalized === "view_image" || GIT_READ_ALIASES.has(normalized)) return false;
  return classifyToolName(toolName) === "read";
}

const GENERIC_FILENAMES = new Set([
  "index.ts",
  "index.tsx",
  "index.js",
  "index.jsx",
  "index.py",
  "__init__.py",
  "mod.rs",
  "main.ts",
  "main.py",
  "readme.md",
  "agents.md",
  "package.json",
]);

function compactDisplayPath(value: string): string {
  const normalized = value.replaceAll("\\", "/").replace(/\/+$/, "");
  const parts = normalized.split("/").filter(Boolean);
  if (parts.length <= 1) return value;
  const base = parts[parts.length - 1];
  if (GENERIC_FILENAMES.has(base.toLowerCase()) && parts.length >= 2) {
    return parts.slice(-2).join("/");
  }
  return base;
}

function nestedToolTitle(tool: ToolCardData, hint: string | undefined): { label: string; tooltip?: string } {
  const normalized = normalizeToolName(tool.name);
  const category = classifyToolName(tool.name);

  if (normalized === "view_image") {
    return { label: hint ? `查看图片 ${compactDisplayPath(hint)}` : "查看图片", tooltip: hint };
  }
  if (normalized === "web_search") {
    return { label: hint ? `联网检索 "${hint}"` : "联网检索", tooltip: hint };
  }
  if (GIT_READ_ALIASES.has(normalized)) {
    return { label: hint ? `${tool.name} ${compactDisplayPath(hint)}` : tool.name, tooltip: hint };
  }
  if (normalized === "ls" || normalized === "list_dir" || normalized === "list_files") {
    return { label: hint ? `浏览目录 ${compactDisplayPath(hint)}` : "浏览目录", tooltip: hint };
  }
  if (category === "read") {
    return { label: hint ? `读取 ${compactDisplayPath(hint)}` : "读取文件", tooltip: hint };
  }
  if (category === "write") {
    const verb = normalized.includes("write") ? "写入" : "编辑";
    return { label: hint ? `${verb} ${compactDisplayPath(hint)}` : `${verb}文件`, tooltip: hint };
  }
  if (category === "shell") {
    return { label: hint || "运行终端命令", tooltip: hint };
  }
  if (category === "search") {
    const verb = normalized === "glob" || normalized === "find" || normalized.includes("file") ? "查找" : "搜索";
    return { label: hint ? `${verb} ${verb === "搜索" ? `"${hint}"` : hint}` : `${verb}工作区`, tooltip: hint };
  }
  return { label: hint ? `${tool.name} ${hint}` : tool.name, tooltip: hint };
}

export const ToolCard = memo(function ToolCard({
  tool,
  cwd,
  outputLimit,
  registry = defaultToolRendererRegistry,
  onResolveApproval,
  approvalPending = false,
  chainedPrev = false,
  chainedNext = false,
  defaultCollapsed = false,
  nested = false,
}: ToolCardProps) {
  const renderer = registry.resolve(tool.name);
  const Renderer = renderer.component;
  const resolvedOutputLimit = useMemo<ToolOutputLimit>(() => ({
    maxCharacters: outputLimit?.maxCharacters ?? DEFAULT_OUTPUT_LIMIT.maxCharacters,
    maxLines: outputLimit?.maxLines ?? DEFAULT_OUTPUT_LIMIT.maxLines,
  }), [outputLimit?.maxCharacters, outputLimit?.maxLines]);

  const [userToggled, setUserToggled] = useState(false);
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const [showRawArgs, setShowRawArgs] = useState(false);
  const attentionError = tool.status === "error" || tool.status === "blocked" || tool.status === "timed_out";
  const isOpen = Boolean(tool.approval) || (userToggled ? !collapsed : (!defaultCollapsed || attentionError));
  const hint = useMemo(() => argsHeaderHint(tool), [tool.args]);
  const nestedSummary = useMemo(
    () => (nested ? nestedToolTitle(tool, hint) : null),
    [nested, tool.name, hint],
  );
  const tone = statusTone(tool.status);
  const hasArgs = tool.args !== undefined && tool.args !== null
    && !(isRecord(tool.args) && Object.keys(tool.args).length === 0);

  const rawArgsText = useMemo(() => {
    if (!showRawArgs) return "";
    try {
      return JSON.stringify(tool.args, null, 2) ?? String(tool.args);
    } catch {
      return String(tool.args);
    }
  }, [showRawArgs, tool.args]);

  const resolveApproval = (approved: boolean) => {
    if (!tool.approval || !onResolveApproval) return;
    void onResolveApproval(tool.approval, approved);
  };

  const railToneClass = tool.status === "approval"
    ? "thread-rail--warn"
    : attentionError
      ? "thread-rail--err"
      : "";

  const needsInlineAttention = nested && tool.status !== "done";
  const emphasizeTrigger = nested && (tool.status === "approval" || attentionError);

  return (
    <section
      className={[
        "tool-card-view",
        `tool-card-view--${tool.status}`,
        nested ? "tool-card-view--nested" : "",
        "thread-node",
        chainedPrev ? "thread-node--chained-prev" : "",
        chainedNext ? "thread-node--chained-next" : "",
      ].filter(Boolean).join(" ")}
      data-tool-category={renderer.category}
      data-tool-name={tool.name}
      aria-busy={tool.status === "running"}
    >
      <header className="tool-card-view__header">
        <button
          type="button"
          className={`thread-trigger tool-card-view__toggle ${emphasizeTrigger ? "tool-card-view__toggle--attention" : ""}`.trim()}
          aria-expanded={isOpen}
          title={nestedSummary?.tooltip}
          onClick={() => {
            setUserToggled(true);
            setCollapsed(isOpen);
          }}
        >
          <span className="thread-trigger__icon" aria-hidden="true">
            <ToolCategoryIcon name={tool.name} category={renderer.category} />
          </span>
          {nested && nestedSummary ? (
            <span className="thread-trigger__label">{nestedSummary.label}</span>
          ) : (
            <>
              <strong className="thread-trigger__title">{tool.name}</strong>
              {!isOpen && hint && <span className="thread-trigger__summary" title={hint}>{hint}</span>}
            </>
          )}
          {needsInlineAttention && (
            <span className={`tool-card-view__inline-status tool-card-view__status--${tool.status}`}>
              <span
                className={`status-dot ${tone === "idle" ? "" : `status-dot--${tone}`} ${tool.status === "running" || tool.status === "pending" ? "status-dot--pulse" : ""}`.trim()}
                aria-hidden="true"
              />
              {STATUS_LABELS[tool.status]}
            </span>
          )}
          <span className="thread-trigger__chevron" aria-hidden="true">
            {isOpen ? <ChevronDown size={14} strokeWidth={1.5} /> : <ChevronRight size={14} strokeWidth={1.5} />}
          </span>
        </button>

        {!nested && (
          <span className={`tool-card-view__status tool-card-view__status--${tool.status}`}>
            <span
              className={`status-dot ${tone === "idle" ? "" : `status-dot--${tone}`} ${tool.status === "running" ? "status-dot--pulse" : ""}`.trim()}
              aria-hidden="true"
            />
            {STATUS_LABELS[tool.status]}
          </span>
        )}
      </header>

      {isOpen && (
        <div className={`thread-rail ${railToneClass}`.trim()}>
          <div className="thread-box tool-card-view__box">
            <Renderer tool={tool} cwd={cwd} outputLimit={resolvedOutputLimit} />

            {tool.approval && (
              <footer className="tool-card-view__approval">
                <span>该操作需要你的确认</span>
                <div>
                  <button
                    type="button"
                    className="tool-card-view__approval-reject"
                    disabled={approvalPending}
                    onClick={() => resolveApproval(false)}
                  >
                    拒绝
                  </button>
                  <button
                    type="button"
                    className="tool-card-view__approval-accept"
                    disabled={approvalPending}
                    onClick={() => resolveApproval(true)}
                  >
                    允许一次
                  </button>
                </div>
              </footer>
            )}
          </div>

          {hasArgs && (
            <div className="tool-card-view__raw-wrap">
              <button
                type="button"
                className="thread-code-toggle"
                aria-expanded={showRawArgs}
                title={showRawArgs ? "隐藏原始参数" : "查看原始工具参数"}
                onClick={() => setShowRawArgs((value) => !value)}
              >
                <Code2 size={13} strokeWidth={1.6} aria-hidden="true" />
                {showRawArgs && <span>收起参数</span>}
              </button>
              {showRawArgs && (
                <pre className="tool-card-view__output tool-card-view__raw-args">{rawArgsText}</pre>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
});
