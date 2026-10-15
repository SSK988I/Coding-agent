import { contextBridge, ipcRenderer } from "electron";
import type { DesktopBridge, DesktopShell, RuntimeEvent } from "../shared/types";

const agent: DesktopBridge = {
  request: (method, params = {}) => ipcRenderer.invoke("agent:request", method, params),
  onEvent: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, payload: RuntimeEvent) => listener(payload);
    ipcRenderer.on("agent:event", handler);
    return () => ipcRenderer.removeListener("agent:event", handler);
  },
  onStatus: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, payload: string) => listener(payload);
    ipcRenderer.on("agent:status", handler);
    return () => ipcRenderer.removeListener("agent:status", handler);
  },
};

const desktop: DesktopShell = {
  workbenchRequest: (method, params) => ipcRenderer.invoke("desktop:workbench-request", method, params),
  onWorkbenchEvent: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, payload: import("../shared/workbench").WorkbenchNativeEvent) => listener(payload);
    ipcRenderer.on("desktop:workbench-event", handler);
    return () => ipcRenderer.removeListener("desktop:workbench-event", handler);
  },
  chooseWorkspace: () => ipcRenderer.invoke("desktop:choose-workspace"),
  getBootstrap: () => ipcRenderer.invoke("desktop:get-bootstrap"),
};

contextBridge.exposeInMainWorld("agent", agent);
contextBridge.exposeInMainWorld("desktop", desktop);
