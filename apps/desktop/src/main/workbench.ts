import { BrowserWindow, shell, View, WebContentsView } from "electron";
import { randomUUID } from "node:crypto";
import path from "node:path";
import type { IPty } from "node-pty";
import type { BrowserSnapshot, TerminalSnapshot, WorkbenchNativeEvent, WorkbenchScope } from "../shared/workbench";

interface TerminalRecord extends TerminalSnapshot { process: IPty; closing?: Promise<void> }
type BackendRequest = (method: string, params: Record<string, unknown>) => Promise<unknown>;

export function browserUrl(input: unknown): string {
  if (typeof input !== "string" || input.length > 4096 || /[\x00-\x1f]/.test(input)) throw new Error("请输入有效的网址");
  const value = input.trim();
  if (value === "about:blank") return value;
  const local = /^(localhost|127\.\d+\.\d+\.\d+|\[::1\])(?::\d+)?(?:\/|$)/i.test(value);
  const normalized = /^https?:\/\//i.test(value) ? value : `${local ? "http" : "https"}://${value}`;
  const url = new URL(normalized);
  if (!["http:", "https:"].includes(url.protocol) || !url.hostname || url.username || url.password) throw new Error("浏览器仅支持 HTTP 和 HTTPS 地址");
  if (/^[a-z][a-z\d+.-]*:/i.test(value) && !/^https?:\/\//i.test(value) && !local) throw new Error("浏览器仅支持 HTTP 和 HTTPS 地址");
  return url.toString();
}

function dimension(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(2, Math.min(500, Math.floor(value))) : fallback;
}

export class NativeWorkbench {
  private scope: WorkbenchScope | null = null;
  private scopeEpoch = 0;
  private terminals = new Map<string, TerminalRecord>();
  private browser: WebContentsView | null = null;
  private browserAttached = false;
  private browserKey: string | null = null;
  private browserOpenEpoch = 0;
  private retiredBrowserKeys = new Set<string>();
  private browserError: string | null = null;
  private stopping = new Set<Promise<void>>();

  constructor(private window: BrowserWindow, private request: BackendRequest) {
    // Compose sibling web views under a neutral root. Nesting the browser in
    // BrowserWindow's main WebContentsView makes its render-widget visibility
    // depend on the parent page and can leave HTTP navigations blank on Windows.
    const main = window.contentView;
    if (main instanceof WebContentsView) {
      const root = new View();
      window.setContentView(root);
      root.addChildView(main);
      const resize = () => { if (!window.isDestroyed()) { const [width, height] = window.getContentSize(); main.setBounds({ x: 0, y: 0, width, height }); } };
      resize();
      window.on("resize", resize);
      window.once("closed", () => window.removeListener("resize", resize));
    }
  }

  retainScope(workspacePath: string, sessionId: string): void {
    if (this.scope?.workspacePath === workspacePath && this.scope.sessionId === sessionId) return;
    void this.dispose();
    this.scope = { workspacePath, sessionId };
  }

  private emit(event: WorkbenchNativeEvent): void {
    if (!this.window.isDestroyed()) this.window.webContents.send("desktop:workbench-event", event);
  }

  private async checkedScope(params: Record<string, unknown>): Promise<WorkbenchScope> {
    const epoch = this.scopeEpoch;
    const scope = await this.request("workbench.scope", {}) as WorkbenchScope;
    if (epoch !== this.scopeEpoch && (scope.sessionId !== this.scope?.sessionId || scope.workspacePath !== this.scope?.workspacePath)) throw new Error("STALE_WORKSPACE: 会话已经切换");
    this.retainScope(scope.workspacePath, scope.sessionId);
    if (params.workspacePath !== scope.workspacePath || params.sessionId !== scope.sessionId) throw new Error("STALE_WORKSPACE: 工作区或会话已经切换");
    return scope;
  }

  async dispatch(method: string, params: Record<string, unknown>): Promise<unknown> {
    const browserEpoch = method === "browser.open" ? ++this.browserOpenEpoch : 0;
    // Releases are idempotent and ownership-scoped. An old panel may unmount
    // after session.changed; it must not touch the new browser or restart Core.
    if (method === "browser.close") {
      if (typeof params.key === "string" && params.key.length <= 128) {
        this.retiredBrowserKeys.add(params.key);
        if (this.retiredBrowserKeys.size > 64) this.retiredBrowserKeys.delete(this.retiredBrowserKeys.values().next().value!);
      }
      if (this.scope && this.scope.sessionId === params.sessionId && this.scope.workspacePath === params.workspacePath && this.browserKey === params.key) this.closeBrowser();
      return { accepted: true };
    }
    if (method === "terminal.close") {
      const old = this.terminals.get(String(params.id));
      if (old && old.sessionId === params.sessionId && old.workspacePath === params.workspacePath) { await this.stopTerminal(old); this.terminals.delete(old.id); }
      return { accepted: true };
    }
    const scope = await this.checkedScope(params);
    if (method === "terminal.open") {
      if (typeof params.key !== "string" || params.key.length > 128) throw new Error("终端标识无效");
      const old = [...this.terminals.values()].find((record) => record.key === params.key);
      if (old) return this.terminalSnapshot(old);
      if (this.terminals.size >= 6) throw new Error("最多同时打开 6 个终端");
      const { spawn } = await import("node-pty");
      if (this.scope?.sessionId !== scope.sessionId || this.scope.workspacePath !== scope.workspacePath) throw new Error("STALE_WORKSPACE: 会话已经切换");
      // Two IPC requests can cross at the async module import.
      const existing = [...this.terminals.values()].find((record) => record.key === params.key);
      if (existing) return this.terminalSnapshot(existing);
      if (this.terminals.size >= 6) throw new Error("最多同时打开 6 个终端");
      const executable = process.platform === "win32"
        ? path.join(process.env.SystemRoot ?? "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        : process.env.SHELL || "/bin/bash";
      const args = process.platform === "win32" ? ["-NoLogo"] : ["-l"];
      const pty = spawn(executable, args, { cwd: scope.workspacePath, cols: dimension(params.cols, 80), rows: dimension(params.rows, 24), name: "xterm-256color", useConptyDll: process.platform === "win32", env: { ...process.env, TERM: "xterm-256color", COLORTERM: "truecolor" } });
      const record: TerminalRecord = { ...scope, process: pty, id: randomUUID(), key: params.key, backlog: "", offset: 0, exited: false, exitCode: null };
      this.terminals.set(record.id, record);
      pty.onData((data) => {
        record.backlog = (record.backlog + data).slice(-256 * 1024);
        record.offset += data.length;
        this.emit({ ...scope, type: "terminal.output", id: record.id, key: record.key, data, offset: record.offset });
      });
      pty.onExit(({ exitCode }) => {
        record.exited = true;
        record.exitCode = exitCode;
        this.emit({ ...scope, type: "terminal.exit", id: record.id, key: record.key, exitCode });
      });
      return this.terminalSnapshot(record);
    }
    if (method.startsWith("terminal.")) {
      const record = this.terminals.get(String(params.id));
      if (!record) throw new Error("终端已经关闭");
      if (record.closing && method !== "terminal.close") throw new Error("终端正在关闭");
      if (method === "terminal.write") {
        if (typeof params.data !== "string" || params.data.length > 16384 || record.exited) throw new Error("终端输入无效或进程已经退出");
        record.process.write(params.data);
      } else if (method === "terminal.resize") {
        if (!record.exited) record.process.resize(dimension(params.cols, 80), dimension(params.rows, 24));
      } else throw new Error("未知终端操作");
      return { accepted: true };
    }
    if (method === "files.reveal") {
      const result = await this.request("workbench.files.resolve", params) as { absolutePath: string };
      shell.showItemInFolder(result.absolutePath);
      return { accepted: true };
    }
    if (method === "browser.open") {
      if (typeof params.key !== "string" || !params.key || params.key.length > 128) throw new Error("浏览器标识无效");
      if (browserEpoch !== this.browserOpenEpoch || this.retiredBrowserKeys.has(params.key)) {
        return { ...scope, key: params.key, url: "about:blank", title: "浏览器", loading: false, canGoBack: false, canGoForward: false, error: null };
      }
      this.ensureBrowser(params.key);
      if (params.url !== undefined) await this.navigate(params.url);
      return this.browserSnapshot();
    }
    if (!this.browser || this.browser.webContents.isDestroyed()) throw new Error("请先打开浏览器");
    if (params.key !== this.browserKey) throw new Error("STALE_BROWSER: 浏览器面板已经关闭或替换");
    const contents = this.browser.webContents;
    if (method === "browser.navigate") await this.navigate(params.url);
    else if (method === "browser.back") { if (contents.navigationHistory.canGoBack()) contents.navigationHistory.goBack(); }
    else if (method === "browser.forward") { if (contents.navigationHistory.canGoForward()) contents.navigationHistory.goForward(); }
    else if (method === "browser.reload") contents.reload();
    else if (method === "browser.stop") contents.stop();
    else if (method === "browser.external") { const url = browserUrl(contents.getURL()); if (url !== "about:blank") await shell.openExternal(url); }
    else if (method === "browser.bounds") {
      const bounds = params.bounds as Record<string, unknown> | undefined;
      const area = this.window.getContentBounds();
      if (!bounds || ![bounds.x, bounds.y, bounds.width, bounds.height].every((value) => typeof value === "number" && Number.isFinite(value))) throw new Error("浏览器显示区域无效");
      const x = Math.max(0, Math.round(Number(bounds.x))), y = Math.max(0, Math.round(Number(bounds.y)));
      const visible = params.visible === true && Number(bounds.width) > 0 && Number(bounds.height) > 0;
      this.browser.setBounds({ x, y, width: visible ? Math.max(0, Math.min(area.width - x, Math.round(Number(bounds.width)))) : 0, height: visible ? Math.max(0, Math.min(area.height - y, Math.round(Number(bounds.height)))) : 0 });
      this.browser.setVisible(visible);
    } else throw new Error("未知浏览器操作");
    return this.browserSnapshot();
  }

  private terminalSnapshot({ process: _process, closing: _closing, ...snapshot }: TerminalRecord): TerminalSnapshot { return snapshot; }

  private stopTerminal(record: TerminalRecord): Promise<void> {
    if (record.exited) return Promise.resolve();
    record.closing ??= new Promise<void>((resolve) => {
      const exit = record.process.onExit(() => { clearTimeout(timeout); exit.dispose(); resolve(); });
      const timeout = setTimeout(() => { exit.dispose(); try { process.kill(record.process.pid); } catch { /* Owned process has already exited. */ } resolve(); }, 3000);
      try { record.process.kill(); } catch { clearTimeout(timeout); exit.dispose(); resolve(); }
    });
    return record.closing;
  }

  private ensureBrowser(key: string): void {
    if (this.browser && !this.browser.webContents.isDestroyed() && this.browserKey === key) return;
    this.closeBrowser();
    const browser = new WebContentsView({ webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, partition: "persist:coding-agent-workbench-browser" } });
    this.browser = browser;
    this.browserKey = key;
    // Retain native window ownership while inactive; never cover React with an
    // uninitialized view or detach a loading renderer from its owning window.
    browser.setBounds({ x: 0, y: 0, width: 0, height: 0 });
    this.window.contentView.addChildView(browser);
    this.browserAttached = true;
    browser.setVisible(false);
    const contents = browser.webContents;
    const scope = this.scope!;
    const changed = () => { if (this.browser === browser) this.emit({ ...scope, type: "browser.changed", browser: this.browserSnapshot() }); };
    contents.on("will-navigate", (event, url) => { try { browserUrl(url); } catch { event.preventDefault(); } });
    contents.on("will-redirect", (event, url) => { try { browserUrl(url); } catch { event.preventDefault(); } });
    contents.setWindowOpenHandler(({ url }) => { void this.navigate(url).catch(() => undefined); return { action: "deny" }; });
    contents.on("did-start-loading", changed);
    contents.on("did-stop-loading", changed);
    contents.on("did-navigate", changed);
    contents.on("did-navigate-in-page", changed);
    contents.on("did-finish-load", changed);
    contents.on("page-title-updated", changed);
    contents.on("did-fail-load", (_event, code, description, _url, mainFrame) => { if (mainFrame && code !== -3) { this.browserError = description; changed(); } });
    contents.session.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
    contents.session.setPermissionCheckHandler(() => false);
    // New webContents already has an empty page. Loading a second about:blank
    // before attaching the view races its first real navigation and surface.
  }

  private async navigate(input: unknown): Promise<void> {
    const url = browserUrl(input);
    this.browserError = null;
    await this.browser!.webContents.loadURL(url);
  }

  private browserSnapshot(): BrowserSnapshot {
    const candidate = this.browser?.webContents;
    const contents = candidate && !candidate.isDestroyed() ? candidate : undefined;
    return { ...this.scope!, key: this.browserKey ?? "", url: contents?.getURL() || "about:blank", title: contents?.getTitle() || "浏览器", loading: contents?.isLoading() ?? false, canGoBack: contents?.navigationHistory.canGoBack() ?? false, canGoForward: contents?.navigationHistory.canGoForward() ?? false, error: this.browserError };
  }

  private closeBrowser(): void {
    if (!this.browser) return;
    if (this.browserAttached && !this.window.isDestroyed()) this.window.contentView.removeChildView(this.browser);
    this.browserAttached = false;
    if (!this.browser.webContents.isDestroyed()) this.browser.webContents.close();
    this.browser = null;
    this.browserKey = null;
    this.browserError = null;
  }

  dispose(): Promise<void> {
    this.scopeEpoch++;
    for (const record of this.terminals.values()) {
      const stopped = this.stopTerminal(record);
      this.stopping.add(stopped);
      void stopped.finally(() => this.stopping.delete(stopped));
    }
    this.terminals.clear();
    this.closeBrowser();
    this.scope = null;
    return Promise.all([...this.stopping]).then(() => undefined);
  }
}
