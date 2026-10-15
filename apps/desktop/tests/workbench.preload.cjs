const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("desktop", {
  getBootstrap: () => ipcRenderer.invoke("wb:bootstrap"),
  chooseWorkspace: async () => null,
  workbenchRequest: (method, params) => ipcRenderer.invoke("wb:native", method, params),
  onWorkbenchEvent: (listener) => { const handler = (_event, event) => listener(event); ipcRenderer.on("desktop:workbench-event", handler); return () => ipcRenderer.removeListener("desktop:workbench-event", handler); },
});
contextBridge.exposeInMainWorld("agent", {
  request: (method, params = {}) => ipcRenderer.invoke("wb:agent", method, params),
  onEvent: (listener) => { const handler = (_event, event) => listener(event); ipcRenderer.on("agent:event", handler); return () => ipcRenderer.removeListener("agent:event", handler); },
  onStatus: (listener) => { listener("ready"); return () => {}; },
});
