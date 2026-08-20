import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type {
  AgentMessage,
  ContentBlock,
  RuntimeEvent,
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
  const [commandOptions, setCommandOptions] = useState<CommandOption[]>([]);
  const [commandMenuOpen, setCommandMenuOpen] = useState(false);
  const [selectedCommandIndex, setSelectedCommandIndex] = useState(0);
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([]);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [modelQuery, setModelQuery] = useState("");
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const timelineRef = useRef<HTMLDivElement>(null);
  const didBootstrap = useRef(false);
  const rafQueue = useRef<RuntimeEvent[]>([]);
  const rafId = useRef<number | null>(null);
  const activeAssistantIds = useRef(new Map<string, string>());
  const assistantSequence = useRef(0);
  const timelineSequence = useRef(0);

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
    setTools([]);
    setCommandMenuOpen(false);
    setModelPickerOpen(false);
    activeAssistantIds.current.clear();
    setError(null);
    void refreshSessions();
    void refreshCommands();
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
  }, [messages, tools, running]);

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
        const result = await window.agent.request<Record<string, unknown>>("session.compact");
        addNotice(result.performed
          ? `上下文已压缩。${String(result.summary_preview ?? "")}`
          : String(result.error ?? "当前无需压缩。"));
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
      setError(`桌面端暂不支持命令：/${name}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    const text = input.trim();
    if (!text || !workspace || running) return;
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
    if (running || sessionId === workspace?.sessionId) return;
    const result = await window.agent.request<WorkspacePayload>("session.open", { sessionId });
    applyWorkspace(result);
  };

  const newSession = async () => {
    if (running || !workspace) return;
    const result = await window.agent.request<WorkspacePayload>("session.new");
    applyWorkspace(result);
  };

  const statusLabel = useMemo(() => {
    if (sidecarStatus === "ready") return "运行时已连接";
    if (sidecarStatus === "starting") return "正在启动运行时";
    return sidecarStatus.startsWith("error:") ? "运行时异常" : "运行时已断开";
  }, [sidecarStatus]);

  const timelineItems = useMemo(() => [
    ...messages.map((value) => ({ kind: "message" as const, order: value.order, value })),
    ...tools.map((value) => ({ kind: "tool" as const, order: value.order, value })),
  ].sort((left, right) => left.order - right.order), [messages, tools]);

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
        <button className="ghost-button" onClick={chooseWorkspace}>打开项目</button>
      </header>

      <div className={`workspace-grid ${sidebarOpen ? "" : "sidebar-closed"}`}>
        <aside className="sidebar">
          <div className="sidebar-heading">
            <span>会话</span>
            <button className="icon-button" onClick={newSession} title="新建会话">＋</button>
          </div>
          <div className="session-list">
            {sessions.map((session) => (
              <button
                key={session.id}
                className={`session-item ${session.id === workspace?.sessionId ? "active" : ""}`}
                onClick={() => openSession(session.id)}
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
            </div>
          )}
        </aside>

        <main className="conversation">
          <button className="sidebar-toggle" onClick={() => setSidebarOpen((value) => !value)}>
            {sidebarOpen ? "‹" : "›"}
          </button>
          <div className="timeline" ref={timelineRef}>
            {!messages.length && (
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
          </div>

          <div className="composer-wrap">
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
            <form className="composer" onSubmit={submit}>
              <textarea
                value={input}
                onChange={(event) => onInputChange(event.target.value)}
                onKeyDown={onInputKeyDown}
                placeholder={workspace ? "描述你想完成的任务…" : "请先打开工作区"}
                disabled={!workspace}
                rows={3}
              />
              <div className="composer-footer">
                <span>Enter 发送 · Shift+Enter 换行</span>
                {running ? (
                  <button type="button" className="stop-button" onClick={() => window.agent.request("run.abort")}>■ 停止</button>
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
