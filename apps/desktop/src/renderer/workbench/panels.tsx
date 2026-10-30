import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AgentMessage } from "../../shared/types";
import type { ChangeReviewRequest, ChangesSnapshot, ChangeSource, FileEntry, FileListing, FilePreview, SideConversationSnapshot, WorkbenchScope } from "../../shared/workbench";

export const scopeRequest = <T,>(scope: WorkbenchScope, method: string, params: Record<string, unknown> = {}) => window.agent.request<T>(method, { ...params, ...scope });
export const nativeRequest = <T,>(scope: WorkbenchScope, method: string, params: Record<string, unknown> = {}) => {
  if (!window.desktop.workbenchRequest) return Promise.reject(new Error("此工具需要桌面宿主"));
  return window.desktop.workbenchRequest<T>(method, { ...params, ...scope });
};

function PanelIcon({ name }: { name: "refresh" | "wrap" | "copy" | "plus" | "chat" }) {
  const paths = {
    refresh: "M20 7v5h-5 M19.4 12a7.5 7.5 0 1 1-1.8-5L20 9",
    wrap: "M4 6h16 M4 11h12a4 4 0 0 1 0 8h-3 M13 16l-3 3 3 3 M4 16h3",
    copy: "M8 8h12v13H8z M16 8V3H3v13h5",
    plus: "M12 5v14 M5 12h14",
    chat: "M4 4h16v12H9l-5 4z M8 8h8 M8 12h5",
  };
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>;
}

export function DiffView({ diff, wrap }: { diff: string; wrap: boolean }) {
  let oldLine = 0, newLine = 0;
  let inHunk = false;
  const rows = diff.split("\n").flatMap((line, index) => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(line);
    if (hunk) {
      inHunk = true;
      oldLine = Number(hunk[1]); newLine = Number(hunk[2]);
      return [<tr key={index} className="diff-hunk"><td colSpan={3}>{line}{oldLine > 1 ? " · 未改动行已折叠" : ""}</td></tr>];
    }
    if (line.startsWith("diff ")) inHunk = false;
    if ((!inHunk && /^(diff |index |--- |\+\+\+ |new file |deleted file |old mode |new mode )/.test(line)) || !line) return [];
    const kind = line.startsWith("+") ? "add" : line.startsWith("-") ? "remove" : line.startsWith(" ") ? "context" : "note";
    const old = kind === "remove" || kind === "context" ? oldLine++ : "";
    const next = kind === "add" || kind === "context" ? newLine++ : "";
    return [<tr key={index} className={`diff-${kind}`}><td className="diff-line-number">{old}</td><td className="diff-line-number">{next}</td><td><code>{kind === "note" ? line : line.slice(1) || " "}</code></td></tr>];
  });
  return <div className={`diff-code ${wrap ? "wrap" : ""}`}><table aria-label="逐行变更"><tbody>{rows}</tbody></table></div>;
}

export function ChangesPanel({ scope, active, onOpenFile, requestedChange = null }: { scope: WorkbenchScope; active: boolean; onOpenFile: (path: string) => void; requestedChange?: ChangeReviewRequest | null }) {
  const [source, setSource] = useState<ChangeSource>(requestedChange ? "turn" : "unstaged");
  const [reviewRunId, setReviewRunId] = useState<string | undefined>(requestedChange?.runId);
  const [baseRef, setBaseRef] = useState("");
  const [snapshot, setSnapshot] = useState<ChangesSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [wrap, setWrap] = useState(false);
  const generation = useRef(0);
  const inFlight = useRef<{ key: string; request: number } | null>(null);
  const fileNodes = useRef<Record<string, HTMLDetailsElement | null>>({});
  useEffect(() => {
    setReviewRunId(requestedChange?.runId);
    setSource(requestedChange ? "turn" : "unstaged");
    setExpanded(requestedChange?.path ? { [requestedChange.path]: true } : {});
  }, [requestedChange]);
  const refresh = useCallback(async () => {
    const key = JSON.stringify([source, baseRef, reviewRunId]);
    if (inFlight.current?.key === key && inFlight.current.request === generation.current) return;
    const request = ++generation.current;
    const pending = { key, request };
    inFlight.current = pending;
    setLoading(true);
    try {
      const data = await scopeRequest<ChangesSnapshot>(scope, "workbench.changes", {
        source, ...(baseRef ? { baseRef } : {}), ...(reviewRunId ? { runId: reviewRunId } : {}),
      });
      if (request !== generation.current) return;
      if ((data.sessionId && data.sessionId !== scope.sessionId) || (data.workspacePath && data.workspacePath !== scope.workspacePath)
        || (reviewRunId && (data.runId !== reviewRunId || data.source !== "turn"))) throw new Error("变更记录与所选轮次不匹配，请重新打开。");
      setSnapshot(data); setError(null);
    } catch (reason) { if (request === generation.current) setError(String(reason)); }
    finally { if (inFlight.current === pending) inFlight.current = null; if (request === generation.current) setLoading(false); }
  }, [scope, source, baseRef, reviewRunId]);
  useEffect(() => {
    if (!active) return;
    void refresh();
    if (reviewRunId) return () => { generation.current++; };
    const timer = window.setInterval(() => { void refresh(); }, 5000);
    return () => { generation.current++; clearInterval(timer); };
  }, [active, refresh, reviewRunId]);
  const current = snapshot?.source === source && (source !== "turn" || snapshot.runId === reviewRunId)
    && (!reviewRunId || reviewRunId === requestedChange?.runId) ? snapshot : null;
  useEffect(() => {
    if (!current || current.runId !== requestedChange?.runId || !requestedChange?.path) return;
    const node = fileNodes.current[requestedChange.path];
    node?.scrollIntoView?.({ block: "nearest" });
    node?.querySelector("summary")?.focus();
  }, [current, requestedChange]);
  return <section className="changes-panel" aria-label="变更" aria-busy={loading}>
    <div className="workbench-toolbar changes-toolbar">
      <select aria-label="变更来源" value={reviewRunId ? "review" : source} onChange={(event) => {
        if (event.target.value === "review") return;
        setReviewRunId(undefined); setSource(event.target.value as ChangeSource); setExpanded({});
      }}>
        {reviewRunId && <option value="review">所选轮次</option>}
        <option value="unstaged">未暂存</option><option value="staged">已暂存</option><option value="branch">分支</option><option value="session">本会话</option><option value="turn">上一轮</option>
      </select>
      <span className="changes-total"><span className="diff-add-count">+{current?.additions ?? 0}</span><span className="diff-remove-count">−{current?.deletions ?? 0}</span></span>
      <span className="workbench-spacer" />
      <div className="workbench-toolbar-actions">
        <button type="button" title="刷新变更" aria-label="刷新变更" onClick={() => void refresh()} disabled={loading}><PanelIcon name="refresh" /></button>
        <button type="button" title="自动换行" aria-label="自动换行" aria-pressed={wrap} onClick={() => setWrap(!wrap)}><PanelIcon name="wrap" /></button>
        <button type="button" title="复制全部 diff" aria-label="复制全部 diff" disabled={!current?.files.length} onClick={() => void navigator.clipboard.writeText(current!.files.map((file) => file.diff).join("\n")).catch((reason) => setError(String(reason)))}><PanelIcon name="copy" /></button>
      </div>
    </div>
    {source === "branch" && <div className="workbench-toolbar"><span>比较分支</span><select aria-label="比较分支" value={baseRef || snapshot?.baseRef || ""} onChange={(event) => setBaseRef(event.target.value)}><option value="">选择分支</option>{snapshot?.branches.map((branch) => <option key={branch} value={branch}>{branch}</option>)}</select></div>}
    {current && <div className="workbench-caption changes-caption">{current.branch && <span className="changes-branch" title={current.branch}>{current.branch}</span>}<span>{current.files.length} 个文件{current.runId ? " · 本轮保存的变更" : ["session", "turn"].includes(source) && " · 与任务开始时比较"}</span></div>}
    {error && <p className="workbench-error" role="alert">{error}</p>}
    {current?.truncated && <p className="workbench-notice">变更超过预览上限，当前列表可能不完整。</p>}
    <div className="changes-scroll">
      {current?.files.map((file, index) => <details className="changed-file" key={file.path}
        ref={(node) => { fileNodes.current[file.path] = node; }}
        open={expanded[file.path] ?? (requestedChange?.path && reviewRunId ? file.path === requestedChange.path : index === 0)}
        onToggle={(event) => { const open = event.currentTarget.open; setExpanded((before) => before[file.path] === open ? before : { ...before, [file.path]: open }); }}>
        <summary><span className={`file-status status-${file.status}`}>{file.status}</span><span className="changed-file-path" title={file.path}>{file.path}</span><span className="diff-add-count">+{file.additions}</span><span className="diff-remove-count">−{file.deletions}</span></summary>
        <div className="diff-file-actions"><button type="button" onClick={() => onOpenFile(file.path)} disabled={file.status === "D"}>在文件中打开 ↗</button><button type="button" onClick={() => void navigator.clipboard.writeText(file.path).catch((reason) => setError(String(reason)))}>复制路径</button></div>
        {file.binary ? <p className="workbench-notice">二进制文件发生变更</p> : (expanded[file.path] ?? index === 0) && <DiffView diff={file.diff} wrap={wrap} />}
        {file.truncated && <p className="workbench-notice">当前文件差异已截断，内容不完整。</p>}
      </details>)}
      {current && !current.files.length && !loading && <div className="workbench-empty">当前来源没有变更</div>}
      {!current && loading && <div className="workbench-empty" role="status">正在读取变更…</div>}
    </div>
  </section>;
}

export function FilesPanel({ scope, requestedFile }: { scope: WorkbenchScope; requestedFile: { path: string; serial: number } | null }) {
  const [folders, setFolders] = useState<Record<string, FileEntry[]>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({ "": true });
  const [selected, setSelected] = useState<string | null>(null);
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [filter, setFilter] = useState("");
  const [raw, setRaw] = useState(false);
  const [loading, setLoading] = useState(false);
  const generation = useRef(0);
  const folderGeneration = useRef(0);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; generation.current++; }; }, []);
  const loadFolder = useCallback(async (path: string) => {
    const version = folderGeneration.current;
    try {
      const data = await scopeRequest<FileListing>(scope, "workbench.files.list", { path, showHidden });
      if (alive.current && version === folderGeneration.current) { setFolders((current) => ({ ...current, [path]: data.entries })); if (data.truncated) setError("目录超过 500 个条目，当前只显示前 500 个。"); }
    } catch (reason) { if (alive.current && version === folderGeneration.current) setError(String(reason)); }
  }, [scope, showHidden]);
  useEffect(() => { folderGeneration.current++; setFolders({}); setExpanded({ "": true }); void loadFolder(""); }, [loadFolder]);
  const openFile = useCallback(async (path: string) => {
    const request = ++generation.current;
    setSelected(path); setLoading(true); setError(null); setRaw(false);
    try { const data = await scopeRequest<FilePreview>(scope, "workbench.files.read", { path }); if (alive.current && request === generation.current) setPreview(data); }
    catch (reason) { if (alive.current && request === generation.current) { setPreview(null); setError(String(reason)); } }
    finally { if (alive.current && request === generation.current) setLoading(false); }
  }, [scope]);
  useEffect(() => { if (requestedFile) void openFile(requestedFile.path); }, [requestedFile, openFile]);
  const renderFolder = (path: string, depth: number) => (folders[path] ?? []).filter((entry) => !filter || entry.kind === "directory" || entry.path.toLowerCase().includes(filter.toLowerCase())).map((entry) => <li key={entry.path}>
    <button type="button" role="treeitem" aria-selected={selected === entry.path} aria-expanded={entry.kind === "directory" ? Boolean(expanded[entry.path]) : undefined} disabled={entry.blocked} style={{ paddingLeft: `${10 + depth * 14}px` }} title={entry.blocked ? "符号链接指向工作区外" : entry.path} onClick={() => {
      if (entry.kind === "file") { void openFile(entry.path); return; }
      const next = !expanded[entry.path]; setExpanded((current) => ({ ...current, [entry.path]: next })); if (next && !folders[entry.path]) void loadFolder(entry.path);
    }}><span aria-hidden="true">{entry.kind === "directory" ? expanded[entry.path] ? "▾ ▰" : "▸ ▱" : "▫"}</span><span>{entry.name}{entry.symlink ? " ↗" : ""}</span></button>
    {entry.kind === "directory" && expanded[entry.path] && <ul role="group">{renderFolder(entry.path, depth + 1)}</ul>}
  </li>);
  const content = preview?.path === selected ? preview : null;
  const lines = content?.text?.split("\n", 5001) ?? [];
  return <section className="files-panel" aria-label="文件">
    <div className="workbench-toolbar"><input aria-label="筛选已展开文件" placeholder="筛选已展开文件…" value={filter} onChange={(event) => setFilter(event.target.value)} /><button type="button" title="刷新目录" aria-label="刷新目录" onClick={() => { folderGeneration.current++; setFolders({}); setExpanded({ "": true }); void loadFolder(""); }}>↻</button><label className="compact-check"><input type="checkbox" checked={showHidden} onChange={(event) => setShowHidden(event.target.checked)} />隐藏文件</label></div>
    <div className="file-browser">
      <nav className="file-tree" aria-label="工作区文件树"><ul role="tree">{renderFolder("", 0)}</ul>{folders[""]?.length === 0 && <p>目录为空</p>}</nav>
      <div className="file-preview">
        {selected && <div className="file-preview-heading"><span title={selected}>{selected}</span><button type="button" title="在系统文件管理器中显示" aria-label="在系统文件管理器中显示" onClick={() => void nativeRequest(scope, "files.reveal", { path: selected }).catch((reason) => setError(String(reason)))}>↗</button>{content?.text !== null && content && /\.md(?:own)?$/i.test(content.path) && <button type="button" onClick={() => setRaw(!raw)}>{raw ? "预览" : "源码"}</button>}</div>}
        {error && <p className="workbench-error" role="alert">{error}</p>}
        {loading && <div className="workbench-empty" role="status">正在读取文件…</div>}
        {content?.truncated && <p className="workbench-notice">文件超过 1 MiB，只预览开头内容。</p>}
        {!loading && content?.image && <img className="file-image-preview" src={content.image} alt={content.path} />}
        {!loading && content?.text !== null && content && (/\.md(?:own)?$/i.test(content.path) && !raw ? <div className="workbench-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{content.text}</ReactMarkdown></div> : <div className="file-code"><pre>{lines.slice(0, 5000).map((line, index) => <div className="file-code-line" key={index}><span>{index + 1}</span><code>{line || " "}</code></div>)}</pre>{lines.length > 5000 && <p className="workbench-notice">仅显示前 5000 行。</p>}</div>)}
        {!loading && content && content.text === null && !content.image && <div className="workbench-empty">此文件不支持文本预览 · {content.size.toLocaleString()} 字节</div>}
        {!selected && <div className="workbench-empty">从文件树选择文件，在这里预览。</div>}
      </div>
    </div>
  </section>;
}

function messageText(message: AgentMessage): string {
  return typeof message.content === "string" ? message.content : message.content.map((block) => block.text ?? "").join("");
}

export function SideChatPanel({ scope, readOnly = false }: { scope: WorkbenchScope; readOnly?: boolean }) {
  const [conversations, setConversations] = useState<{ conversationId: string; title: string }[]>([]);
  const [snapshot, setSnapshot] = useState<SideConversationSnapshot | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selected = useRef<string | null>(null);
  const navigation = useRef(0);
  const listGeneration = useRef(0);
  const alive = useRef(true);
  const busyRef = useRef(false);
  const owner = JSON.stringify([scope.workspacePath, scope.sessionId]);
  const ownerRef = useRef(owner);
  ownerRef.current = owner;
  const eventSeq = useRef(0);
  const bottom = useRef<HTMLDivElement>(null);
  const refreshList = useCallback(async () => {
    if (!alive.current || ownerRef.current !== owner) return;
    const generation = ++listGeneration.current;
    try {
      const data = await scopeRequest<{ conversations: { conversationId: string; title: string }[] }>(scope, "workbench.chat.list");
      if (alive.current && ownerRef.current === owner && generation === listGeneration.current) {
        const unique = new Map(data.conversations.map((item) => [item.conversationId, item]));
        setConversations([...unique.values()]);
      }
    } catch (reason) {
      if (alive.current && ownerRef.current === owner && generation === listGeneration.current) setError(String(reason));
    }
  }, [owner]);
  useEffect(() => {
    alive.current = true;
    navigation.current++;
    selected.current = null;
    eventSeq.current = 0;
    busyRef.current = false;
    setBusy(false); setSnapshot(null); setConversations([]); setDrafts({}); setError(null);
    void refreshList();
    const unsubscribe = window.agent.onEvent((event) => {
      if (!alive.current || ownerRef.current !== owner) return;
      if (event.sessionId !== scope.sessionId || event.event.type !== "workbench.chat.changed") return;
      const data = event.event.payload as unknown as SideConversationSnapshot;
      if (data.conversationId === selected.current) { eventSeq.current = event.seq; setSnapshot(data); }
      if (data.status !== "running") void refreshList();
    });
    return () => { alive.current = false; navigation.current++; listGeneration.current++; unsubscribe(); };
  }, [owner, refreshList]);
  useEffect(() => { bottom.current?.scrollIntoView?.({ block: "nearest" }); }, [snapshot?.streaming, snapshot?.messages.length]);
  const open = async (conversationId?: string) => {
    if (readOnly && !conversationId) return;
    if (busyRef.current) return;
    busyRef.current = true;
    const generation = ++navigation.current;
    const seq = eventSeq.current;
    const previous = selected.current;
    selected.current = conversationId ?? null;
    setBusy(true); setError(null);
    try {
      const data = await scopeRequest<SideConversationSnapshot>(scope, "workbench.chat.open", conversationId ? { conversationId } : {});
      if (!alive.current || ownerRef.current !== owner || generation !== navigation.current) return;
      selected.current = data.conversationId;
      if (eventSeq.current === seq) setSnapshot(data);
      void refreshList();
    } catch (reason) {
      if (alive.current && ownerRef.current === owner && generation === navigation.current) { selected.current = previous; setError(String(reason)); }
    } finally { if (alive.current && ownerRef.current === owner && generation === navigation.current) { busyRef.current = false; setBusy(false); } }
  };
  const request = async (method: string, extra: Record<string, unknown> = {}) => {
    if (!snapshot || busyRef.current || (readOnly && (method === "workbench.chat.send"
      || (method === "workbench.chat.approve" && extra.allowed === true)))) return;
    busyRef.current = true;
    const id = snapshot.conversationId, generation = navigation.current, seq = eventSeq.current;
    setBusy(true); setError(null);
    try {
      const data = await scopeRequest<SideConversationSnapshot>(scope, method, { conversationId: id, ...extra });
      if (!alive.current || ownerRef.current !== owner || generation !== navigation.current) return;
      if (eventSeq.current === seq && data.conversationId) setSnapshot(data);
      if (method === "workbench.chat.send") setDrafts((current) => ({ ...current, [id]: "" }));
    } catch (reason) { if (alive.current && ownerRef.current === owner && generation === navigation.current) setError(String(reason)); }
    finally { if (alive.current && ownerRef.current === owner && generation === navigation.current) { busyRef.current = false; setBusy(false); } }
  };
  const draft = drafts[snapshot?.conversationId ?? ""] ?? "";
  const options = snapshot && !conversations.some((item) => item.conversationId === snapshot.conversationId)
    ? [{ conversationId: snapshot.conversationId, title: snapshot.title || `当前对话 · ${snapshot.conversationId.slice(-8)}` }, ...conversations]
    : conversations;
  const emptyConversation = !snapshot || (!snapshot.messages.length && !snapshot.streaming && snapshot.status !== "running");
  return <section className="side-chat-panel" aria-label="侧边对话">
    <div className="workbench-toolbar side-chat-toolbar"><select aria-label="选择侧边对话" value={snapshot?.conversationId ?? ""} onChange={(event) => { if (event.target.value) void open(event.target.value); }} disabled={busy}><option value="" disabled>选择对话</option>{options.map((item) => <option key={item.conversationId} value={item.conversationId}>{item.title}</option>)}</select><button type="button" className="side-chat-new" title="新建侧边对话" aria-label="新建侧边对话" onClick={() => void open()} disabled={busy || readOnly}><PanelIcon name="plus" /></button></div>
    <p className="workbench-caption side-chat-caption" title="共享当前工作区与可用工具，消息不会自动注入主对话">独立上下文，不打断主任务</p>
    <div className="side-chat-messages">
      {emptyConversation && <div className="workbench-empty side-chat-empty"><span className="side-chat-empty-icon"><PanelIcon name="chat" /></span><h3>单独聊一个问题</h3><p>与主对话分别记录，共享当前工作区。</p>{snapshot ? <small>在下方输入问题，开始这段对话</small> : <button type="button" onClick={() => void open()} disabled={busy || readOnly}>开始侧边对话</button>}</div>}
      {snapshot?.messages.map((message, index) => message.role === "toolResult" ? <details className="side-chat-tool" key={index}><summary>{message.tool_name || "工具结果"}{message.is_error ? " · 失败" : ""}</summary><pre>{messageText(message)}</pre></details> : <article key={index} className={`side-chat-message ${message.role}`}><strong>{message.role === "user" ? "你" : "AI"}</strong><ReactMarkdown remarkPlugins={[remarkGfm]}>{messageText(message)}</ReactMarkdown></article>)}
      {snapshot?.streaming && <article className="side-chat-message assistant"><strong>AI</strong><ReactMarkdown remarkPlugins={[remarkGfm]}>{snapshot.streaming}</ReactMarkdown></article>}
      {snapshot?.status === "running" && !snapshot.streaming && <p className="workbench-caption" role="status">侧边对话正在处理…</p>}
      {snapshot?.error && <p className="workbench-error" role="alert">{snapshot.error}</p>}
      {error && <p className="workbench-error" role="alert">{error}</p>}
      <div ref={bottom} />
    </div>
    {snapshot?.approval && <section className="side-chat-approval" aria-label="侧边对话审批"><strong>批准 {snapshot.approval.toolName}？</strong><pre>{JSON.stringify(snapshot.approval.args, null, 2)}</pre><button type="button" disabled={busy} onClick={() => void request("workbench.chat.approve", { approvalId: snapshot.approval!.approvalId, allowed: false })}>拒绝</button><button type="button" disabled={busy || readOnly} onClick={() => void request("workbench.chat.approve", { approvalId: snapshot.approval!.approvalId, allowed: true })}>允许本次</button></section>}
    {snapshot && <form className="side-chat-composer" onSubmit={(event) => { event.preventDefault(); if (!readOnly && draft.trim() && snapshot.status !== "running") void request("workbench.chat.send", { text: draft }); }}>
      <textarea aria-label="侧边对话消息" placeholder="继续侧边对话…" value={draft} disabled={readOnly || busy || snapshot.status === "running"} onChange={(event) => setDrafts((current) => ({ ...current, [snapshot.conversationId]: event.target.value }))} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); if (!readOnly && draft.trim() && snapshot.status !== "running") void request("workbench.chat.send", { text: draft }); } }} rows={3} />
      <div><span>Enter 发送 · Shift+Enter 换行</span>{snapshot.status === "running" ? <button type="button" className="side-chat-stop" disabled={busy} onClick={() => void request("workbench.chat.stop")}>■ 停止侧边对话</button> : <button type="submit" className="side-chat-send" disabled={readOnly || busy || !draft.trim()}>发送 ↑</button>}</div>
    </form>}
  </section>;
}
