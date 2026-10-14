import { useEffect, useRef, useState } from "react";
import type { FitAddon } from "@xterm/addon-fit";
import type { Terminal } from "@xterm/xterm";
import type { BrowserSnapshot, TerminalSnapshot, WorkbenchNativeEvent, WorkbenchScope } from "../../shared/workbench";
import { nativeRequest } from "./panels";
import "@xterm/xterm/css/xterm.css";

export function TerminalPanel({ scope, tabKey, active }: { scope: WorkbenchScope; tabKey: string; active: boolean }) {
  const viewport = useRef<HTMLDivElement>(null);
  const terminal = useRef<Terminal | null>(null);
  const fit = useRef<FitAddon | null>(null);
  const record = useRef<TerminalSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exitCode, setExitCode] = useState<number | null>(null);
  const [ready, setReady] = useState(false);
  const [restart, setRestart] = useState(0);
  const activeRef = useRef(active);
  activeRef.current = active;
  const writeQueue = useRef(Promise.resolve());
  useEffect(() => {
    let disposed = false;
    let unsubscribe: (() => void) | undefined;
    let resize: ResizeObserver | undefined;
    let currentTerminal: Terminal | undefined;
    const pending: WorkbenchNativeEvent[] = [];
    let latestOffset = 0;
    setReady(false); setExitCode(null); setError(null);
    const output = (event: WorkbenchNativeEvent) => {
      if (event.workspacePath !== scope.workspacePath || event.sessionId !== scope.sessionId || event.key !== tabKey || disposed) return;
      if (!record.current) { pending.push(event); return; }
      if (event.type === "terminal.output" && event.offset !== undefined && event.offset > latestOffset) {
        const delta = event.offset - latestOffset;
        latestOffset = event.offset;
        terminal.current?.write((event.data ?? "").slice(-delta));
      } else if (event.type === "terminal.exit") setExitCode(event.exitCode ?? 0);
    };
    const resizeTerminal = () => {
      if (!activeRef.current || !viewport.current?.clientWidth || !terminal.current || !record.current || disposed) return;
      fit.current?.fit();
      void nativeRequest(scope, "terminal.resize", { id: record.current.id, cols: terminal.current.cols, rows: terminal.current.rows }).catch((reason) => { if (!disposed) setError(String(reason)); });
    };
    void Promise.all([import("@xterm/xterm"), import("@xterm/addon-fit")]).then(async ([{ Terminal }, { FitAddon }]) => {
      if (disposed || !viewport.current) return;
      const xterm = new Terminal({ fontFamily: "Cascadia Code, Consolas, monospace", fontSize: 13, cursorBlink: true, scrollback: 5000, theme: { background: "#202124", foreground: "#e8eaed" }, allowProposedApi: false });
      currentTerminal = xterm;
      const addon = new FitAddon();
      xterm.loadAddon(addon); xterm.open(viewport.current);
      terminal.current = xterm; fit.current = addon;
      unsubscribe = window.desktop.onWorkbenchEvent?.(output);
      xterm.onData((data) => {
        const id = record.current?.id;
        if (!id) return;
        writeQueue.current = writeQueue.current.then(async () => { if (!disposed) await nativeRequest(scope, "terminal.write", { id, data }); }).catch((reason) => { if (!disposed) setError(String(reason)); });
      });
      const opened = await nativeRequest<TerminalSnapshot>(scope, "terminal.open", { key: tabKey, cols: 80, rows: 24 });
      if (disposed) { void nativeRequest(scope, "terminal.close", { id: opened.id }).catch(() => undefined); return; }
      record.current = opened; latestOffset = opened.offset;
      xterm.write(opened.backlog);
      if (opened.exited) setExitCode(opened.exitCode ?? 0);
      for (const event of pending) output(event);
      setReady(true);
      if (typeof ResizeObserver !== "undefined") { resize = new ResizeObserver(resizeTerminal); resize.observe(viewport.current); }
      resizeTerminal();
      if (activeRef.current) xterm.focus();
    }).catch((reason) => { if (!disposed) setError(String(reason)); });
    return () => {
      disposed = true; unsubscribe?.(); resize?.disconnect();
      const old = record.current; record.current = null;
      if (old) void nativeRequest(scope, "terminal.close", { id: old.id }).catch(() => undefined);
      currentTerminal?.dispose(); terminal.current = null; fit.current = null;
    };
  }, [scope, tabKey, restart]);
  useEffect(() => {
    if (!active || !ready) return;
    const frame = requestAnimationFrame(() => { fit.current?.fit(); terminal.current?.focus(); if (record.current && terminal.current) void nativeRequest(scope, "terminal.resize", { id: record.current.id, cols: terminal.current.cols, rows: terminal.current.rows }).catch((reason) => setError(String(reason))); });
    return () => cancelAnimationFrame(frame);
  }, [active, ready, scope]);
  return <section className="terminal-panel" aria-label="交互式终端"><div className="workbench-caption"><span className="terminal-workspace" title={scope.workspacePath}>{scope.workspacePath}</span><span className="workbench-spacer" /><button type="button" onClick={() => terminal.current?.clear()}>清屏</button>{exitCode !== null && <button type="button" onClick={() => setRestart((value) => value + 1)}>重新启动</button>}</div>{error && <p className="workbench-error" role="alert">{error}</p>}{exitCode !== null && <p className="workbench-notice">终端进程已退出 · exit {exitCode}</p>}{!ready && !error && <p className="workbench-caption" role="status">正在启动交互终端…</p>}<div className="terminal-viewport" ref={viewport} /></section>;
}

export function BrowserPanel({ scope, active }: { scope: WorkbenchScope; active: boolean }) {
  const viewport = useRef<HTMLDivElement>(null);
  const [snapshot, setSnapshot] = useState<BrowserSnapshot | null>(null);
  const [address, setAddress] = useState("");
  const [error, setError] = useState<string | null>(null);
  const editing = useRef(false);
  const alive = useRef(true);
  const browserKey = useRef("");
  const activeRef = useRef(active);
  activeRef.current = active;
  const bounds = async () => {
    const rectangle = viewport.current?.getBoundingClientRect();
    if (!rectangle) return;
    await nativeRequest(scope, "browser.bounds", { key: browserKey.current, visible: activeRef.current && Boolean(snapshot && snapshot.url !== "about:blank"), bounds: { x: rectangle.x, y: rectangle.y, width: rectangle.width, height: rectangle.height } });
  };
  useEffect(() => {
    alive.current = true;
    const key = crypto.randomUUID();
    browserKey.current = key;
    const apply = (data: BrowserSnapshot) => { if (!alive.current || browserKey.current !== key || data.key !== key) return; setSnapshot(data); if (!editing.current) setAddress(data.url === "about:blank" ? "" : data.url); };
    const unsubscribe = window.desktop.onWorkbenchEvent?.((event) => { if (event.workspacePath === scope.workspacePath && event.sessionId === scope.sessionId && event.type === "browser.changed" && event.browser) apply(event.browser); });
    void nativeRequest<BrowserSnapshot>(scope, "browser.open", { key }).then(apply).catch((reason) => { if (alive.current && browserKey.current === key) setError(String(reason)); });
    return () => { alive.current = false; unsubscribe?.(); void nativeRequest(scope, "browser.close", { key }).catch(() => undefined); };
  }, [scope]);
  useEffect(() => {
    if (!snapshot) return;
    const adjust = () => { void bounds().catch((reason) => { if (alive.current) setError(String(reason)); }); };
    adjust();
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(adjust) : null;
    if (viewport.current) observer?.observe(viewport.current);
    window.addEventListener("resize", adjust);
    return () => { observer?.disconnect(); window.removeEventListener("resize", adjust); };
  }, [active, snapshot?.url, scope]);
  const command = async (method: string, extra: Record<string, unknown> = {}) => {
    setError(null);
    try { const data = await nativeRequest<BrowserSnapshot>(scope, method, { ...extra, key: browserKey.current }); if (alive.current && data.key === browserKey.current) setSnapshot(data); }
    catch (reason) { if (alive.current) setError(String(reason)); }
  };
  return <section className="browser-panel" aria-label="浏览器">
    <form className="workbench-toolbar browser-address" onSubmit={(event) => { event.preventDefault(); editing.current = false; void command("browser.navigate", { url: address }); }}>
      <button type="button" title="后退" aria-label="后退" disabled={!snapshot?.canGoBack} onClick={() => void command("browser.back")}>←</button><button type="button" title="前进" aria-label="前进" disabled={!snapshot?.canGoForward} onClick={() => void command("browser.forward")}>→</button><button type="button" title={snapshot?.loading ? "停止加载" : "刷新网页"} aria-label={snapshot?.loading ? "停止加载" : "刷新网页"} disabled={!snapshot} onClick={() => void command(snapshot?.loading ? "browser.stop" : "browser.reload")}>{snapshot?.loading ? "×" : "↻"}</button>
      <input aria-label="网页地址" placeholder="输入网址，例如 localhost:3000" value={address} onFocus={() => { editing.current = true; }} onBlur={() => { editing.current = false; }} onChange={(event) => setAddress(event.target.value)} /><button type="submit" disabled={!snapshot || !address.trim()}>前往</button><button type="button" title="在外部浏览器打开" aria-label="在外部浏览器打开" onClick={() => void command("browser.external")} disabled={!snapshot || snapshot.url === "about:blank"}>↗</button>
    </form>
    {(error || snapshot?.error) && <p className="workbench-error" role="alert">{error || snapshot?.error}</p>}
    <div className="browser-viewport" ref={viewport}>{(!snapshot || snapshot.url === "about:blank") && <div className="workbench-empty"><p>在工作栏中打开和操作网页。</p><p>输入开发服务器地址或 HTTP / HTTPS 网址。</p></div>}</div>
  </section>;
}
