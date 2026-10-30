// Offline bridge for the real Chromium image-input smoke only.
const { contextBridge } = require("electron");
const listeners = new Set();
let seq = 0;
let run = 0;
const calls = [];
const workspace = {
  path: "G:\\offline-vision", sessionId: "vision-1",
  model: { id: "vision", name: "Offline vision", provider: "offline-fixture", input: ["text", "image"] },
  thinkingLevel: null, tools: [], messages: [], collaborationMode: "default",
  planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
  memory: { enabled: false, userId: null, projectId: null },
  webSearch: { enabled: true, backend: "zhipu-mcp", available: true, status: "idle", errorCode: null },
};
const copy = (value) => JSON.parse(JSON.stringify(value));
const emit = (type, payload = {}) => {
  const event = { v: 1, type: "event", seq: ++seq, timestamp: Date.now(),
    sessionId: workspace.sessionId, runId: `vision-run-${run}`, event: { type, payload: copy(payload) } };
  for (const listener of listeners) listener(event);
};
contextBridge.exposeInMainWorld("desktop", {
  getBootstrap: async () => ({ defaultWorkspace: workspace.path, platform: "fixture" }),
  chooseWorkspace: async () => null,
});
contextBridge.exposeInMainWorld("agent", {
  onEvent: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
  onStatus: () => () => undefined,
  request: async (method, params = {}) => {
    if (method === "workspace.open" || method === "session.snapshot") return copy(workspace);
    if (method === "session.list" || method === "command.list") return [];
    if (method === "webSearch.setEnabled") {
      workspace.webSearch.enabled = params.enabled;
      workspace.webSearch.status = params.enabled ? "idle" : "disabled";
      emit("webSearch.changed", workspace.webSearch);
      return copy(workspace.webSearch);
    }
    if (method === "run.start") {
      calls.push(copy(params));
      run += 1;
      const attachments = (params.files ?? []).map((file, index) => ({ name: file.name,
        mime_type: file.mime_type, size: Buffer.from(file.data, "base64").length,
        path: `/fixture/attachments/${run}-${index}-${file.name}` }));
      workspace.messages.push({ role: "user", timestamp: Date.now(), content: [
        ...(params.text ? [{ type: "text", text: params.text }] : []),
        ...(params.images ?? []).map((image) => ({ type: "image", ...image })),
        ...attachments.map((metadata) => ({ type: "text",
          text: `<attached_file>\n${JSON.stringify(metadata)}\nfixture body\n</attached_file>` })),
      ], attachments });
      setTimeout(() => emit("run.started", { ...params, files: workspace.messages.at(-1).attachments }), 20);
      return { accepted: true, runId: `vision-run-${run}` };
    }
    throw Error(`Unexpected smoke RPC: ${method}`);
  },
});
contextBridge.exposeInMainWorld("visionSmoke", {
  calls: () => copy(calls),
  fail: () => emit("run.failed", { message: "离线模拟识图失败" }),
  history: () => emit("session.changed", workspace),
  textModel: () => {
    workspace.model.input = ["text"];
    emit("model.changed", workspace.model);
  },
  imageModel: () => {
    workspace.model.input = ["text", "image"];
    emit("model.changed", workspace.model);
  },
});
