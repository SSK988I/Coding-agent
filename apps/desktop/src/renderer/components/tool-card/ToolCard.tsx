import {
  memo,
  useMemo,
  useState,
  type ComponentType,
} from "react";

import "./ToolCard.css";

export type ToolCardStatus = "running" | "approval" | "done" | "error";
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
  running: "执行中",
  approval: "等待审批",
  done: "已完成",
  error: "失败",
};

const CATEGORY_ICONS: Record<ToolCategory, string> = {
  shell: ">_",
  read: "R",
  write: "±",
  search: "⌕",
  generic: "⌁",
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
const READ_ALIASES = new Set(["read", "read_file", "file_read"]);
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

  while (queue.length > 0) {
    const entry = queue.shift();
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

export function formatToolOutput(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;

  const content = textContent(value);
  if (content) return content;

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

function OutputBlock({
  value,
  limit,
  className = "",
  emptyText = "暂无输出",
}: {
  value: string;
  limit: ToolOutputLimit;
  className?: string;
  emptyText?: string;
}) {
  const output = useMemo(() => truncateToolOutput(value, limit), [value, limit]);
  if (!value) return <div className="tool-card-view__empty">{emptyText}</div>;
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
        <span>终端输出</span>
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
      <OutputBlock value={formatToolOutput(tool.result)} limit={outputLimit} emptyText="等待文件内容…" />
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
      <OutputBlock value={formatToolOutput(tool.result)} limit={outputLimit} emptyText="等待检索结果…" />
    </div>
  );
}

function GenericToolRenderer({ tool, outputLimit }: ToolRendererProps) {
  return (
    <div className="tool-card-view__body">
      <OutputBlock
        value={formatToolOutput(tool.result ?? tool.args)}
        limit={outputLimit}
        emptyText="暂无工具数据"
      />
    </div>
  );
}

export const BUILTIN_TOOL_RENDERERS: readonly ToolRendererDefinition[] = [
  {
    id: "shell",
    category: "shell",
    matches: (name) => classifyToolName(name) === "shell",
    component: ShellToolRenderer,
    priority: 100,
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

export const ToolCard = memo(function ToolCard({
  tool,
  cwd,
  outputLimit,
  registry = defaultToolRendererRegistry,
  onResolveApproval,
  approvalPending = false,
}: ToolCardProps) {
  const renderer = registry.resolve(tool.name);
  const Renderer = renderer.component;
  const resolvedOutputLimit = useMemo<ToolOutputLimit>(() => ({
    maxCharacters: outputLimit?.maxCharacters ?? DEFAULT_OUTPUT_LIMIT.maxCharacters,
    maxLines: outputLimit?.maxLines ?? DEFAULT_OUTPUT_LIMIT.maxLines,
  }), [outputLimit?.maxCharacters, outputLimit?.maxLines]);

  const resolveApproval = (approved: boolean) => {
    if (!tool.approval || !onResolveApproval) return;
    void onResolveApproval(tool.approval, approved);
  };

  return (
    <section
      className={`tool-card-view tool-card-view--${tool.status}`}
      data-tool-category={renderer.category}
      data-tool-name={tool.name}
      aria-busy={tool.status === "running"}
    >
      <header className="tool-card-view__header">
        <span className="tool-card-view__icon" aria-hidden="true">{CATEGORY_ICONS[renderer.category]}</span>
        <strong>{tool.name}</strong>
        <span className={`tool-card-view__status tool-card-view__status--${tool.status}`}>
          {STATUS_LABELS[tool.status]}
        </span>
      </header>

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
    </section>
  );
});
