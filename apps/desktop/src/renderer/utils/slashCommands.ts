import type { WorkspacePayload } from "../../shared/types";

export function tokenizeCommandArguments(value: string): string[] {
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

export function formatMemoryStatusNotice(memory: WorkspacePayload["memory"]): string {
  return [
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
  ].join("\n");
}

export function parseMemoryRememberArgs(parts: string[]): { content: string; scope: "global" | "project" } | null {
  const values = parts.slice(1);
  const delimiter = values.indexOf("--");
  const optionSide = delimiter >= 0 ? values.slice(0, delimiter) : [...values];
  const literalSide = delimiter >= 0 ? values.slice(delimiter + 1) : [];
  const scopeFlags: Record<string, "global" | "project"> = {
    "--global": "global",
    "--project": "project",
  };
  let scope: "global" | "project" = "global";
  const trailing = optionSide.at(-1);
  if (trailing && trailing in scopeFlags) {
    scope = scopeFlags[trailing];
    optionSide.pop();
    const preceding = optionSide.at(-1);
    if (preceding && preceding in scopeFlags) return null;
  }
  const content = [...optionSide, ...literalSide].join(" ").trim();
  if (!content) return null;
  return { content, scope };
}
