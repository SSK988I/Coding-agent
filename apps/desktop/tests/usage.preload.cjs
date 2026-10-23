// Isolated, offline fixture; never connects to the real Python backend.
const { contextBridge } = require("electron");
const listeners = new Set();
let sequence = 0;
const clone = (value) => JSON.parse(JSON.stringify(value));
const stats = (cacheHit = 90, contextPercent = 82) => ({
  tokens: { input: 20000, output: 12000, cache_read: 180000, cache_write: 0, total: 212000 },
  cost: 0.0312,
  metrics: {
    cache_hit_rate: cacheHit, input_tokens: 200000, context_tokens: contextPercent * 1000,
    context_window: 100000, context_percent: contextPercent, context_estimated: true,
  },
});
const workspace = {
  path: "G:\\offline-usage-fixture", sessionId: "usage-1", permissionMode: "ask",
  model: { id: "offline", name: "Offline metrics fixture", provider: "fixture" },
  thinkingLevel: null, tools: [], messages: [], collaborationMode: "default",
  planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
  memory: { enabled: false, userId: null, projectId: null }, stats: stats(),
};
const emit = (type, payload, sessionId = workspace.sessionId) => {
  const event = {
    v: 1, type: "event", seq: ++sequence, timestamp: Date.now(),
    sessionId, runId: null, event: { type, payload: clone(payload) },
  };
  for (const listener of listeners) listener(event);
};
contextBridge.exposeInMainWorld("desktop", {
  getBootstrap: async () => ({ defaultWorkspace: workspace.path, platform: "offline-fixture" }),
  chooseWorkspace: async () => null,
});
contextBridge.exposeInMainWorld("agent", {
  onEvent: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
  onStatus: () => () => undefined,
  request: async (method) => {
    if (method === "workspace.open" || method === "session.snapshot") return clone(workspace);
    if (method === "session.list" || method === "command.list") return [];
    throw Error(`Unexpected usage smoke RPC: ${method}`);
  },
});
contextBridge.exposeInMainWorld("usageSmoke", {
  update: () => {
    workspace.stats = stats(94, 91);
    emit("session.statsChanged", { stats: workspace.stats });
  },
  switchSession: () => {
    workspace.sessionId = "usage-2";
    workspace.stats = stats(25, 15);
    emit("session.changed", workspace);
  },
  staleUpdate: () => emit("session.statsChanged", { stats: stats(99, 99) }, "usage-1"),
  overflow: () => {
    workspace.stats = stats(94, 125);
    emit("session.statsChanged", { stats: workspace.stats });
  },
});
