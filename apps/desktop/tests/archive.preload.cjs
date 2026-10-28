// Isolated archive fixture: no real sessions, providers, filesystem writes, or sidecar.
const { contextBridge } = require("electron");
const listeners = new Set();
const clone = (value) => JSON.parse(JSON.stringify(value));
const calls = [];
let sequence = 0;
let selectedId = "archive-demo-current";
const records = [
  { id: selectedId, name: "整理会话归档", archived: false },
  { id: "archive-demo-other", name: "优化缓存指标", archived: false },
  { id: "archive-demo-old", name: "旧版界面方案", archived: true },
].map((record) => ({
  ...record, first_message: record.name, message_count: 2, modified: 1,
  messages: [
    { role: "user", content: `请继续${record.name}，保留之前讨论的内容。`, timestamp: 1 },
    { role: "assistant", content: [{ type: "text", text: `${record.name}的讨论记录已保留。归档后仍然可以查看这段记录。` }],
      model: "offline", provider: "fixture", api: "openai-completions", stop_reason: "stop", timestamp: 2,
      usage: { input: 100, output: 40, cache_read: 0, cache_write: 0, total_tokens: 140 } },
  ],
}));
const payload = () => {
  const record = records.find((item) => item.id === selectedId);
  return clone({
    path: "G:\\offline-archive-fixture", sessionId: record.id, archived: record.archived, permissionMode: "ask",
    model: { id: "offline", name: "Offline archive fixture", provider: "fixture", input: ["text"] },
    thinkingLevel: null, tools: [], messages: record.messages, collaborationMode: "default",
    planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
    memory: { enabled: false, userId: null, projectId: null },
  });
};
const emit = (type, value) => {
  const event = { v: 1, type: "event", seq: ++sequence, timestamp: Date.now(), sessionId: selectedId,
    runId: null, event: { type, payload: clone(value) } };
  for (const listener of listeners) listener(event);
};
contextBridge.exposeInMainWorld("desktop", {
  getBootstrap: async () => ({ defaultWorkspace: "G:\\offline-archive-fixture", platform: "offline-fixture" }),
  chooseWorkspace: async () => null,
});
contextBridge.exposeInMainWorld("agent", {
  onEvent: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
  onStatus: () => () => undefined,
  request: async (method, params = {}) => {
    calls.push({ method, params: clone(params) });
    if (method === "workspace.open" || method === "session.snapshot") return payload();
    if (method === "session.list") return clone(records.filter((record) => record.archived === Boolean(params.archived))
      .map(({ messages: _messages, ...info }) => info));
    if (method === "command.list") return [];
    if (method === "session.open") {
      if (!records.some((record) => record.id === params.sessionId)) throw Error("Unknown fixture session");
      selectedId = params.sessionId;
      return payload();
    }
    if (method === "session.archive" || method === "session.unarchive") {
      const record = records.find((item) => item.id === params.sessionId);
      if (!record) throw Error("Unknown fixture session");
      record.archived = method === "session.archive";
      const result = { sessionId: record.id, archived: record.archived };
      emit(record.archived ? "session.archived" : "session.unarchived", result);
      return clone(result);
    }
    throw Error(`Unexpected archive smoke RPC: ${method}`);
  },
});
contextBridge.exposeInMainWorld("archiveSmoke", {
  state: () => clone({ records, calls, selectedId }),
});
