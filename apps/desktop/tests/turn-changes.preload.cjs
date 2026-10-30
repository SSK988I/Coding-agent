// Offline turn-review fixture; no Python sidecar, provider, or project writes.
const { contextBridge } = require("electron");
const clone = (value) => JSON.parse(JSON.stringify(value));
const listeners = new Set();
const calls = [];
const project = "G:\\offline-turn-changes";
let selectedId = "turns-current";
let sequence = 0;
const file = (path, additions = 2, deletions = 1) => ({ path, status: "M", additions, deletions, binary: false });
const summary = (runId, afterMessageIndex, files) => ({ runId, entryId: `entry-${runId}`, afterMessageIndex,
  timestamp: 1700000000000 + afterMessageIndex, files,
  additions: files.reduce((total, item) => total + item.additions, 0),
  deletions: files.reduce((total, item) => total + item.deletions, 0), truncated: false });
const first = summary("first-run", 1, [file("src/first.ts"), file("src/shared.ts"), file("src/third.ts"), file("src/fourth.ts")]);
const second = summary("second-run", 3, [file("src/shared.ts", 5, 2)]);
const snapshots = new Map();
const freezeReview = (change) => {
  const snapshot = { workspacePath: project, sessionId: "turns-current", source: "turn", runId: change.runId,
    files: change.files.map((entry) => ({ ...entry,
      diff: `--- a/${entry.path}\n+++ b/${entry.path}\n@@ -1 +1 @@\n-before\n+${change.runId}:${entry.path}\n` })),
    additions: change.additions, deletions: change.deletions, branch: "dev", branches: ["dev"], baseRef: null, truncated: false };
  snapshots.set(change.runId, JSON.stringify(snapshot));
};
freezeReview(first);
freezeReview(second);
const records = [
  { id: selectedId, name: "逐轮变更审阅", messages: [
    { role: "user", content: "调整第一轮的四个文件。", timestamp: 1 },
    { role: "assistant", content: "第一轮已完成，保留该轮的变更供复核。", timestamp: 2 },
    { role: "user", content: "第二轮继续修改共享文件。", timestamp: 3 },
    { role: "assistant", content: "第二轮已完成，共享文件的新变更另行保存。", timestamp: 4 },
  ], turnChanges: [first, second] },
  { id: "turns-other", name: "另一个会话", messages: [{ role: "assistant", content: "这个会话没有代码变更。", timestamp: 5 }], turnChanges: [] },
];
const current = () => records.find((record) => record.id === selectedId);
const workspace = () => clone({ path: project, sessionId: selectedId, archived: false, permissionMode: "ask",
  model: { id: "fixture-text", name: "Offline turn review", provider: "fixture", input: ["text"] },
  thinkingLevel: null, tools: [], messages: current().messages, collaborationMode: "default", turnChanges: current().turnChanges,
  planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
  memory: { enabled: false, userId: null, projectId: null } });
const emit = (type, payload, runId, sessionId = selectedId) => {
  const event = { v: 1, type: "event", seq: ++sequence, timestamp: Date.now(), sessionId, runId,
    event: { type, payload: clone(payload) } };
  for (const listener of listeners) listener(event);
};

contextBridge.exposeInMainWorld("desktop", {
  chooseWorkspace: async () => null,
  getBootstrap: async () => ({ defaultWorkspace: project, platform: "offline-fixture" }),
});
contextBridge.exposeInMainWorld("agent", {
  onEvent: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
  onStatus: () => () => undefined,
  request: async (method, params = {}) => {
    calls.push({ method, params: clone(params) });
    if (method === "workspace.open" || method === "session.snapshot") return workspace();
    if (method === "session.list") return clone(records.map((record) => ({ id: record.id, name: record.name,
      first_message: record.name, message_count: record.messages.length, modified: 1 })));
    if (method === "command.list") return [];
    if (method === "session.open") {
      if (!records.some((record) => record.id === params.sessionId)) throw Error("Unknown fixture session");
      selectedId = params.sessionId;
      return workspace();
    }
    if (method === "workbench.changes") {
      if (params.sessionId !== selectedId || params.workspacePath !== project || params.source !== "turn") {
        throw Error("Review must use the current scope and exact turn");
      }
      const saved = snapshots.get(params.runId);
      if (!saved) throw Error("Unknown saved turn");
      return JSON.parse(saved);
    }
    throw Error(`Unexpected turn-changes smoke RPC: ${method}`);
  },
});
contextBridge.exposeInMainWorld("turnChangesSmoke", {
  state: () => clone({ calls, selectedId, messages: current().messages.length, cards: current().turnChanges.length }),
  liveTurn: (changed) => {
    const runId = changed ? "live-changed" : "live-unchanged";
    const text = changed ? "实时修改一个文件。" : "只解释代码，不修改文件。";
    const answer = changed ? "实时修改已结束。" : "本轮只有解释，没有文件变更。";
    const user = { role: "user", content: text, timestamp: Date.now() };
    const assistant = { role: "assistant", content: [{ type: "text", text: answer }], timestamp: Date.now(), stop_reason: "stop" };
    current().messages.push(user, assistant);
    emit("run.started", { text }, runId);
    emit("message_start", { message: { role: "assistant", content: [] } }, runId);
    emit("message_end", { message: assistant }, runId);
    if (changed) {
      const change = summary(runId, current().messages.length - 1, [file("src/live.ts", 3, 0)]);
      current().turnChanges.push(change);
      freezeReview(change);
      emit("workbench.turnChanged", { change }, runId);
      emit("workbench.turnChanged", { change }, runId); // Duplicate delivery stays idempotent.
    }
    emit("run.completed", {}, runId);
  },
  staleChange: () => emit("workbench.turnChanged", { change: first }, first.runId, "turns-current"),
});
