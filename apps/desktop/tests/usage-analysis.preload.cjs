// Offline fixture for the real drawer/chart smoke; no provider or Python sidecar.
const { contextBridge, ipcRenderer } = require("electron");
const listeners = new Set();
const clone = (value) => JSON.parse(JSON.stringify(value));
const calls = [];
const pending = [];
let sequence = 0;
let selectedId = "analysis-current";
let delayNext = false;
let failNext = false;
const project = "G:\\offline-usage-analysis";

const request = (index, model = "fixture-flash") => {
  const uncached = 50 + index * 10, read = index * 200, write = index % 3 === 0 ? 20 : 0;
  const input = uncached + read + write;
  return { id: `request-${index}`, index, timestamp: Date.UTC(2026, 9, 29, 8, index),
    provider: "fixture", model, stopReason: "stop", inputTokens: input,
    uncachedInputTokens: uncached, outputTokens: 100 + index * 20,
    cacheReadTokens: read, cacheWriteTokens: write, cacheHitRate: read / input * 100,
    cost: Math.round((input * 0.0000002 + (100 + index * 20) * 0.0000008) * 1000000) / 1000000 };
};
const records = [
  { id: selectedId, name: "分析当前会话用量", requests: Array.from({ length: 7 }, (_value, index) => request(index + 1)) },
  { id: "analysis-other", name: "另一个会话", requests: [{ ...request(1, "fixture-alt"),
    id: "other-request", inputTokens: 111, uncachedInputTokens: 111,
    outputTokens: 17, cacheReadTokens: 0, cacheWriteTokens: 0, cacheHitRate: 0, cost: 0.0007 }] },
  { id: "analysis-empty", name: "新建会话", requests: [] },
];
records[0].requests.push({ id: "request-unknown", index: 8, timestamp: null,
  provider: "fixture", model: "fixture-unknown", stopReason: "error", inputTokens: null,
  uncachedInputTokens: null, outputTokens: null, cacheReadTokens: null, cacheWriteTokens: null,
  cacheHitRate: null, cost: null });

const current = () => records.find((item) => item.id === selectedId);
const stats = (record) => {
  const sum = (field) => record.requests.reduce((total, row) => total + (row[field] ?? 0), 0);
  const input = sum("inputTokens"), output = sum("outputTokens"), cached = sum("cacheReadTokens");
  return {
    tokens: { input: sum("uncachedInputTokens"), output, cache_read: cached,
      cache_write: sum("cacheWriteTokens"), total: input + output },
    cost: sum("cost"),
    metrics: { cache_hit_rate: input ? cached / input * 100 : null, input_tokens: input,
      context_tokens: record.id === "analysis-empty" ? 400 : record.id === "analysis-other" ? 125000 : 82000,
      context_window: 100000,
      context_percent: record.id === "analysis-empty" ? 0.4 : record.id === "analysis-other" ? 125 : 82,
      context_estimated: record.id !== "analysis-current" },
  };
};
const workspace = () => clone({
  path: project, sessionId: selectedId, permissionMode: "ask",
  model: { id: current().requests[0]?.model ?? "fixture-flash", name: "Offline usage analysis", provider: "fixture", input: ["text"] },
  thinkingLevel: null, tools: [], collaborationMode: "default", archived: false,
  messages: [{ role: "user", content: "查看这次会话的输入、输出与缓存用量。", timestamp: 1 },
    { role: "assistant", content: [{ type: "text", text: "用量分析使用当前活动分支中的请求记录。" }], timestamp: 2 }],
  planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
  memory: { enabled: false, userId: null, projectId: null }, stats: stats(current()),
});
const analysis = () => clone({ sessionId: selectedId, leafId: `${selectedId}-${current().requests.length}`,
  stats: stats(current()), requests: current().requests });
const emit = (type, value, identity = selectedId) => {
  const event = { v: 1, type: "event", seq: ++sequence, timestamp: Date.now(), sessionId: identity,
    runId: null, event: { type, payload: clone(value) } };
  for (const listener of listeners) listener(event);
};

contextBridge.exposeInMainWorld("desktop", {
  getBootstrap: async () => ({ defaultWorkspace: project, platform: "offline-fixture" }),
  chooseWorkspace: async () => null,
  workbenchRequest: (method, params) => ipcRenderer.invoke("usage-analysis:native", method, params),
  onWorkbenchEvent: (listener) => {
    const handler = (_event, value) => listener(value);
    ipcRenderer.on("desktop:workbench-event", handler);
    return () => ipcRenderer.removeListener("desktop:workbench-event", handler);
  },
});
contextBridge.exposeInMainWorld("agent", {
  onEvent: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
  onStatus: () => () => undefined,
  request: async (method, params = {}) => {
    calls.push({ method, params: clone(params) });
    if (method === "workspace.open" || method === "session.snapshot") return workspace();
    if (method === "command.list") return [];
    if (method === "session.list") return clone(records.filter((record) => record.id !== "analysis-empty")
      .map((record) => ({ id: record.id, name: record.name, first_message: record.name, message_count: 2, modified: 1 })));
    if (method === "session.open" || method === "session.new") {
      selectedId = method === "session.new" ? "analysis-empty" : params.sessionId;
      if (!current()) throw Error("Unknown fixture session");
      return workspace();
    }
    if (method === "session.usage") {
      if (params.sessionId !== selectedId) throw Error("STALE_SESSION");
      if (failNext) { failNext = false; throw Error("离线夹具模拟读取失败"); }
      const value = analysis();
      if (delayNext) {
        delayNext = false;
        return new Promise((resolve) => pending.push({ sessionId: selectedId, value, resolve }));
      }
      return value;
    }
    throw Error(`Unexpected usage-analysis smoke RPC: ${method}`);
  },
});
contextBridge.exposeInMainWorld("usageAnalysisSmoke", {
  state: () => clone({ calls, selectedId, pendingUsage: pending.length, requestCount: current().requests.length }),
  scope: () => ({ workspacePath: project, sessionId: selectedId }),
  delayNextUsage: () => { delayNext = true; },
  failNextUsage: () => { failNext = true; },
  resolvePending: () => { for (const item of pending.splice(0)) item.resolve(clone(item.value)); },
  switchSession: (identity) => {
    if (!records.some((record) => record.id === identity)) throw Error("Unknown fixture session");
    selectedId = identity;
    emit("session.changed", workspace());
  },
  addRequest: () => {
    const rows = current().requests;
    rows.push({ ...request(rows.length + 1, "fixture-live"), id: `${selectedId}-live-${rows.length + 1}` });
    emit("session.statsChanged", { stats: stats(current()) });
  },
});
