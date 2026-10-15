import { useEffect, useMemo, useRef, useState } from "react";
import type { WorkspacePayload } from "../../shared/types";
import type { WorkbenchScope, WorkbenchTool } from "../../shared/workbench";
import { ChangesPanel, FilesPanel, SideChatPanel } from "./panels";
import { BrowserPanel, TerminalPanel } from "./nativePanels";
import "./workbench.css";

const TOOLS: { kind: WorkbenchTool; label: string; description: string; path: string }[] = [
  { kind: "chat", label: "侧边对话", description: "在独立会话中追问具体问题，不打断当前任务。", path: "M4 4h13v10H9l-4 4v-4H4z M19 8h3v14l-4-4h-6" },
  { kind: "changes", label: "变更", description: "查看当前目录的未暂存、已暂存、分支或会话变更。", path: "M6 3h9l5 5v13H6z M14 3v6h6 M9 14h7 M12.5 10.5v7" },
  { kind: "terminal", label: "终端", description: "打开当前工作区的交互式终端。", path: "M3 4h18v16H3z M7 9l3 3-3 3 M12 15h5" },
  { kind: "browser", label: "浏览器", description: "在工作栏中打开并操作网页。", path: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20 M2 12h20 M12 2c6 6 6 14 0 20 M12 2c-6 6-6 14 0 20" },
  { kind: "files", label: "文件", description: "浏览工作区文件，在文件树旁直接预览。", path: "M3 6h7l2 3h9v12H3z M3 6V3h7l2 3h7v3" },
];
interface ToolTab { id: string; kind: WorkbenchTool; title: string }
function ToolIcon({ kind }: { kind: WorkbenchTool }) { return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={TOOLS.find((tool) => tool.kind === kind)!.path} /></svg>; }

export function Workbench({ workspace, open, onClose, width, onWidth, sidebarOpen = true }: { workspace: WorkspacePayload; open: boolean; onClose: () => void; width: number; onWidth: (width: number) => void; sidebarOpen?: boolean }) {
  const scope = useMemo<WorkbenchScope>(() => ({ workspacePath: workspace.path, sessionId: workspace.sessionId }), [workspace.path, workspace.sessionId]);
  const [tabs, setTabs] = useState<ToolTab[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [launcher, setLauncher] = useState(true);
  const [maximized, setMaximized] = useState(false);
  const [requestedFile, setRequestedFile] = useState<{ path: string; serial: number } | null>(null);
  const history = useRef<string[]>([]);
  const terminalCounter = useRef(0);
  const tablist = useRef<HTMLDivElement>(null);
  const resizeCleanup = useRef<(() => void) | null>(null);
  const [viewportWidth, setViewportWidth] = useState(window.innerWidth);
  const maximumWidth = Math.max(360, viewportWidth - (sidebarOpen ? 268 : 0) - 280);
  const constrain = (value: number) => Math.max(360, Math.min(window.innerWidth - (sidebarOpen ? 268 : 0) - 280, value));
  const constrainedWidth = Math.max(360, Math.min(maximumWidth, width));
  useEffect(() => {
    const resized = () => setViewportWidth(window.innerWidth);
    window.addEventListener("resize", resized);
    return () => window.removeEventListener("resize", resized);
  }, []);
  useEffect(() => { if (width !== constrainedWidth) onWidth(constrainedWidth); }, [width, constrainedWidth, onWidth]);
  const activate = (id: string) => { history.current = [...history.current.filter((item) => item !== id), id]; setActive(id); setLauncher(false); };
  const openTool = (kind: WorkbenchTool) => {
    const old = tabs.find((tab) => tab.kind === kind);
    if (old && kind !== "terminal") { activate(old.id); return; }
    const label = TOOLS.find((tool) => tool.kind === kind)!.label;
    const id = kind === "terminal" ? `terminal:${crypto.randomUUID()}` : kind;
    const title = kind === "terminal" ? `${label} ${++terminalCounter.current}` : label;
    setTabs((current) => [...current, { id, kind, title }]); activate(id);
  };
  const closeTab = (id: string) => {
    const remaining = tabs.filter((tab) => tab.id !== id);
    setTabs(remaining); history.current = history.current.filter((item) => item !== id);
    if (active === id) { const next = history.current.at(-1) ?? remaining.at(-1)?.id ?? null; setActive(next); setLauncher(next === null); }
  };
  useEffect(() => { if (!open) setMaximized(false); }, [open]);
  useEffect(() => { tablist.current?.querySelector('.workbench-tab.active')?.scrollIntoView?.({ block: "nearest", inline: "nearest" }); }, [active, launcher]);
  useEffect(() => () => resizeCleanup.current?.(), []);
  const pointerResize = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    resizeCleanup.current?.();
    const start = event.clientX, original = constrainedWidth;
    const move = (next: PointerEvent) => onWidth(constrain(original + start - next.clientX));
    const done = () => { document.removeEventListener("pointermove", move); document.removeEventListener("pointerup", done); document.removeEventListener("pointercancel", done); document.body.style.cursor = ""; document.body.style.userSelect = ""; resizeCleanup.current = null; };
    resizeCleanup.current = done;
    document.body.style.cursor = "col-resize"; document.body.style.userSelect = "none";
    document.addEventListener("pointermove", move); document.addEventListener("pointerup", done, { once: true });
    document.addEventListener("pointercancel", done, { once: true });
  };
  return <aside className={`workbench ${maximized ? "maximized" : ""}`} hidden={!open} aria-label="工具栏" onKeyDown={(event) => { if (event.key === "Escape" && !(event.target instanceof HTMLTextAreaElement) && !((event.target as HTMLElement).closest(".xterm"))) { if (launcher && active) setLauncher(false); else onClose(); } }}>
    <div className="workbench-resize" role="separator" aria-label="调整工具栏宽度" aria-orientation="vertical" aria-valuemin={360} aria-valuemax={maximumWidth} aria-valuenow={constrainedWidth} tabIndex={0} onPointerDown={pointerResize} onKeyDown={(event) => { if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); onWidth(constrain(constrainedWidth + (event.key === "ArrowLeft" ? 40 : -40))); } }} />
    <header className="workbench-header">
      <div className="workbench-tabs" ref={tablist} role="tablist" aria-label="打开的工具">
        {launcher && <span className="workbench-launcher-title">＋ 打开工具</span>}
        {!launcher && tabs.map((tab) => <div key={tab.id} className={`workbench-tab ${active === tab.id ? "active" : ""}`}><button type="button" role="tab" aria-selected={active === tab.id} aria-controls={`workbench-panel-${tab.id}`} onClick={() => activate(tab.id)}><ToolIcon kind={tab.kind} /><span>{tab.title}</span></button><button type="button" className="workbench-tab-close" aria-label={`关闭${tab.title}`} onClick={() => closeTab(tab.id)}>×</button></div>)}
      </div>
      <button type="button" className="workbench-icon-button" title="打开其他工具" aria-label="打开其他工具" onClick={() => setLauncher(true)}>＋</button>
      <button type="button" className="workbench-icon-button" title={maximized ? "还原工具栏" : "展开工具栏"} aria-label={maximized ? "还原工具栏" : "展开工具栏"} onClick={() => setMaximized(!maximized)}>{maximized ? "⊡" : "⛶"}</button>
      <button type="button" className="workbench-icon-button" title="关闭工具栏" aria-label="关闭工具栏" onClick={onClose}>×</button>
    </header>
    {launcher && <section className="workbench-launcher" aria-label="打开工具"><div>{TOOLS.map((tool) => <button type="button" key={tool.kind} onClick={() => openTool(tool.kind)}><ToolIcon kind={tool.kind} /><span><strong>{tool.label}</strong><small>{tool.description}</small></span><span className="workbench-chevron">›</span></button>)}</div>{active && <button type="button" className="workbench-return" onClick={() => setLauncher(false)}>← 返回当前工具</button>}</section>}
    {tabs.map((tab) => {
      const visible = open && !launcher && active === tab.id;
      return <div className="workbench-tool-panel" role="tabpanel" id={`workbench-panel-${tab.id}`} aria-label={tab.title} key={tab.id} hidden={!visible}>
        {tab.kind === "changes" && <ChangesPanel scope={scope} active={visible} onOpenFile={(path) => { setRequestedFile((current) => ({ path, serial: (current?.serial ?? 0) + 1 })); openTool("files"); }} />}
        {tab.kind === "files" && <FilesPanel scope={scope} requestedFile={requestedFile} />}
        {tab.kind === "chat" && <SideChatPanel scope={scope} />}
        {tab.kind === "terminal" && <TerminalPanel scope={scope} tabKey={tab.id} active={visible} />}
        {tab.kind === "browser" && <BrowserPanel scope={scope} active={visible} />}
      </div>;
    })}
  </aside>;
}
