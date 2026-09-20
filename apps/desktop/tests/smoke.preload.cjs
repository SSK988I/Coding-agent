// Offline fixture bridge. Loaded only by electron.smoke.cjs, never by the app.
const { contextBridge } = require("electron");
const listeners = new Set();
let seq = 0;
let compactResult;
const plan = {
  planId: "plan-smoke", revision: 1, title: "复核状态与上下文处理",
  markdown: "# 复核状态与上下文处理\n\n检查取消、失败和恢复行为，补充离线回归测试。",
  digest: "abcdef0123456789", sourceMessageId: "message-smoke",
};
const workspace = {
  path: "G:\\offline-smoke", sessionId: "smoke-1", model: { id: "offline", name: "Offline test", provider: "fixture" },
  tools: ["read", "subagent_spawn"], thinkingLevel: null, messages: [], collaborationMode: "default",
  planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
  memory: { enabled: false, userId: null, projectId: null }, subagents: [],
};
const copy = (value) => JSON.parse(JSON.stringify(value));
function emit(type, payload = {}, runId = null) {
  const event = { v: 1, type: "event", seq: ++seq, timestamp: Date.now(), sessionId: workspace.sessionId, runId, event: { type, payload: copy(payload) } };
  for (const listener of listeners) listener(event);
}
contextBridge.exposeInMainWorld("desktop", {
  getBootstrap: async () => ({ defaultWorkspace: workspace.path, platform: "fixture" }),
  chooseWorkspace: async () => null,
});
contextBridge.exposeInMainWorld("agent", {
  onEvent: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
  onStatus: () => () => {},
  request: async (method, params = {}) => {
    if (method === "workspace.open" || method === "session.snapshot") return copy(workspace);
    if (method === "session.list") return [];
    if (method === "command.list") return [
      { name: "plan", label: "计划模式", description: "进入或显示 Plan 操作" },
      { name: "compact", label: "压缩上下文", description: "定向整理上下文" },
      { name: "subagents", label: "只读子代理", description: "独立调查" },
    ];
    if (method === "mode.enterPlan") {
      workspace.collaborationMode = "plan";
      workspace.planState = { phase: "drafting", activePlanId: plan.planId, latestRevision: null, pendingQuestion: null };
      return copy(workspace);
    }
    if (method === "run.start") {
      setTimeout(() => emit("run.started", {}, "run-smoke"), 20);
      return { accepted: true, runId: "run-smoke" };
    }
    if (method === "run.abort") {
      if (compactResult) {
        emit("compaction_end", { reason: "pivot", aborted: true });
        compactResult({ performed: false, error: "Compaction aborted" });
        compactResult = undefined;
      } else emit("run.cancelled", {}, "run-smoke");
      return { aborted: true };
    }
    if (method === "session.compact") {
      if (params.direction !== "实现已经选定的方案") throw Error("Direction was not forwarded");
      emit("compaction_start", { reason: "pivot" });
      return new Promise((resolve) => { compactResult = resolve; });
    }
    if (method === "subagent.spawn") {
      workspace.subagents = [{
        taskId: "child-smoke", sessionId: workspace.sessionId, purpose: params.purpose,
        prompt: params.task, status: "running", turns: 2, maxTurns: 12, timeoutSeconds: 180,
        lastTool: "git_diff", output: "", error: null,
      }];
      emit("subagent.stateChanged", { tasks: workspace.subagents });
      return copy(workspace.subagents[0]);
    }
    if (method === "subagent.list") return copy(workspace.subagents);
    if (method === "subagent.status") return copy(workspace.subagents[0]);
    if (method === "subagent.wait") return copy(workspace.subagents[0]);
    if (method === "subagent.cancel") {
      workspace.subagents[0].status = "cancelled";
      emit("subagent.stateChanged", { tasks: workspace.subagents });
      return copy(workspace.subagents[0]);
    }
    throw Error(`Unexpected smoke RPC: ${method}`);
  },
});
contextBridge.exposeInMainWorld("smoke", {
  ready: () => {
    workspace.planState = { phase: "ready", activePlanId: plan.planId, latestRevision: plan, pendingQuestion: null };
    workspace.collaborationMode = "plan";
    emit("session.changed", workspace);
  },
  drafting: () => {
    workspace.planState = { phase: "drafting", activePlanId: plan.planId, latestRevision: null, pendingQuestion: null };
    emit("session.changed", workspace);
  },
  report: () => {
    workspace.subagents[0].status = "completed";
    workspace.subagents[0].output = "packages/app/src/runtime.py:42\n发现：取消后应保留原上下文。\n未运行测试，结论仍需主代理核验。";
    emit("subagent.stateChanged", { tasks: workspace.subagents });
  },
});
