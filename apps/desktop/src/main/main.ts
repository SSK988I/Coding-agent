import { app, BrowserWindow, dialog, ipcMain, IpcMainInvokeEvent, Menu } from "electron";
import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (reason: Error) => void;
  timeout: NodeJS.Timeout;
}

let mainWindow: BrowserWindow | null = null;
let sidecar: ChildProcessWithoutNullStreams | null = null;
let quitting = false;
const pending = new Map<string, PendingRequest>();

const desktopRoot = path.resolve(__dirname, "..", "..");
const repositoryRoot = path.resolve(desktopRoot, "..", "..");

function broadcast(channel: string, payload: unknown): void {
  for (const window of BrowserWindow.getAllWindows()) {
    if (!window.isDestroyed()) window.webContents.send(channel, payload);
  }
}

function resolvePython(): string {
  if (process.env.CODING_AGENT_PYTHON) return process.env.CODING_AGENT_PYTHON;
  if (app.isPackaged) {
    const binary = process.platform === "win32" ? "coding-agent-desktop.exe" : "coding-agent-desktop";
    return path.join(process.resourcesPath, "agent-backend", binary);
  }
  const venvPython = process.platform === "win32"
    ? path.join(repositoryRoot, ".venv", "Scripts", "python.exe")
    : path.join(repositoryRoot, ".venv", "bin", "python");
  return fs.existsSync(venvPython) ? venvPython : (process.platform === "win32" ? "python" : "python3");
}

function startSidecar(): void {
  if (sidecar && !sidecar.killed) return;
  const pythonPath = resolvePython();
  const sourcePaths = ["llm", "core", "tui", "app"].map((name) =>
    path.join(repositoryRoot, "packages", name, "src"),
  );
  const pythonPathValue = [
    ...sourcePaths,
    process.env.PYTHONPATH ?? "",
  ].filter(Boolean).join(path.delimiter);
  const args = app.isPackaged ? [] : ["-m", "coding_agent.desktop"];

  sidecar = spawn(pythonPath, args, {
    cwd: repositoryRoot,
    env: {
      ...process.env,
      PYTHONPATH: pythonPathValue,
      PYTHONUNBUFFERED: "1",
      PYTHONUTF8: "1",
    },
    windowsHide: true,
    stdio: ["pipe", "pipe", "pipe"],
  });
  broadcast("agent:status", "starting");

  const lines = readline.createInterface({ input: sidecar.stdout });
  lines.on("line", (line) => {
    try {
      const message = JSON.parse(line) as Record<string, unknown>;
      if (message.type === "event") {
        broadcast("agent:event", message);
        return;
      }
      const id = typeof message.id === "string" ? message.id : null;
      if (!id) return;
      const request = pending.get(id);
      if (!request) return;
      clearTimeout(request.timeout);
      pending.delete(id);
      if (message.error && typeof message.error === "object") {
        const error = message.error as { code?: string; message?: string };
        request.reject(new Error(`${error.code ?? "RPC_ERROR"}: ${error.message ?? "Unknown error"}`));
      } else {
        request.resolve(message.result);
      }
    } catch (error) {
      console.error("Invalid sidecar message", error, line);
    }
  });

  sidecar.stderr.on("data", (chunk) => {
    const message = chunk.toString().trim();
    if (message) {
      console.error(`[agent-sidecar] ${message}`);
    }
  });

  sidecar.once("spawn", async () => {
    try {
      await requestSidecar("runtime.ping", {});
      broadcast("agent:status", "ready");
    } catch (error) {
      broadcast("agent:status", `error:${String(error)}`);
    }
  });

  sidecar.once("exit", (code, signal) => {
    sidecar = null;
    const reason = new Error(`Agent sidecar exited (${code ?? signal ?? "unknown"})`);
    for (const request of pending.values()) {
      clearTimeout(request.timeout);
      request.reject(reason);
    }
    pending.clear();
    if (!quitting) broadcast("agent:status", `stopped:${code ?? signal ?? "unknown"}`);
  });

  sidecar.once("error", (error) => {
    broadcast("agent:status", `error:${error.message}`);
  });
}

function requestSidecar(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
  startSidecar();
  if (!sidecar?.stdin.writable) return Promise.reject(new Error("Agent sidecar is not available"));
  const id = randomUUID();
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`RPC timeout: ${method}`));
    }, 120_000);
    pending.set(id, { resolve, reject, timeout });
    sidecar!.stdin.write(`${JSON.stringify({ v: 1, id, method, params })}\n`, "utf8");
  });
}

function assertTrustedSender(event: IpcMainInvokeEvent): void {
  if (!mainWindow || event.sender !== mainWindow.webContents) {
    throw new Error("Untrusted IPC sender");
  }
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 980,
    minHeight: 640,
    backgroundColor: "#fbfaf8",
    titleBarStyle: "hidden",
    titleBarOverlay: {
      color: "#fbfaf8",
      symbolColor: "#4e4b46",
      height: 54,
    },
    webPreferences: {
      preload: path.join(__dirname, "..", "preload", "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
  });

  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  mainWindow.webContents.on("will-navigate", (event, url) => {
    const devUrl = process.env.VITE_DEV_SERVER_URL;
    if (!devUrl || !url.startsWith(devUrl)) event.preventDefault();
  });

  if (process.env.VITE_DEV_SERVER_URL) {
    void mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL);
  } else {
    void mainWindow.loadFile(path.join(desktopRoot, "dist-renderer", "index.html"));
  }
  mainWindow.on("closed", () => { mainWindow = null; });
}

app.whenReady().then(() => {
  Menu.setApplicationMenu(null);
  ipcMain.handle("agent:request", (event, method: unknown, params: unknown) => {
    assertTrustedSender(event);
    if (typeof method !== "string" || !method) throw new Error("Invalid method");
    if (params !== undefined && (typeof params !== "object" || params === null || Array.isArray(params))) {
      throw new Error("Invalid params");
    }
    return requestSidecar(method, (params ?? {}) as Record<string, unknown>);
  });
  ipcMain.handle("desktop:choose-workspace", async (event) => {
    assertTrustedSender(event);
    const result = await dialog.showOpenDialog(mainWindow!, { properties: ["openDirectory"] });
    return result.canceled ? null : result.filePaths[0] ?? null;
  });
  ipcMain.handle("desktop:get-bootstrap", (event) => {
    assertTrustedSender(event);
    return { defaultWorkspace: repositoryRoot, platform: process.platform };
  });

  createWindow();
  startSidecar();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  quitting = true;
  if (sidecar && !sidecar.killed) {
    sidecar.stdin.end();
    setTimeout(() => sidecar?.kill(), 500).unref();
  }
});
