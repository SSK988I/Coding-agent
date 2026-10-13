// Offline fixture bridge. Loaded only by electron.smoke.cjs, never by the app.
const { contextBridge } = require("electron");
const listeners = new Set();
let seq = 0;
let compactResult;
let sessionScenario = false;
let sessionDelay = false;
let finishNavigation;
const sessionRequests = { open: 0, new: 0 };
const savedSessions = [
  { id: "smoke-1", name: "会话甲", message_count: 1 },
  { id: "smoke-2", name: "会话乙", message_count: 1 },
];
const plan = {
  planId: "plan-smoke", revision: 1, title: "复核状态与上下文处理",
  markdown: "# 复核状态与上下文处理\n\n检查取消、失败和恢复行为，补充离线回归测试。",
  digest: "abcdef0123456789", sourceMessageId: "message-smoke",
};
const workspace = {
  permissionMode: "ask",
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
    if (method === "permission.setMode") {
      workspace.permissionMode = params.mode;
      const payload = { sessionId: workspace.sessionId, permissionMode: workspace.permissionMode };
      emit("permission.changed", payload);
      return copy(payload);
    }
    if (method === "session.list") return sessionScenario ? copy(savedSessions) : [];
    if (method === "session.open" || method === "session.new") {
      sessionRequests[method === "session.open" ? "open" : "new"]++;
      const next = {
        ...copy(workspace),
        sessionId: method === "session.open" ? params.sessionId : `smoke-new-${sessionRequests.new}`,
        messages: method === "session.open"
          ? [{ role: "user", content: params.sessionId === "smoke-1" ? "会话甲的消息" : "会话乙的消息", timestamp: 1 }]
          : [],
        collaborationMode: "default",
        planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
      };
      if (sessionDelay) return new Promise((resolve) => {
        finishNavigation = () => {
          // Deliberately queue the previous session's snapshot just before the
          // RPC reply, reproducing the response-vs-animation-frame race.
          emit("session.changed", workspace);
          Object.assign(workspace, next);
          resolve(copy(workspace));
          finishNavigation = undefined;
        };
      });
      Object.assign(workspace, next);
      emit("session.changed", workspace);
      return copy(workspace);
    }
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
    if (method === "plan.cancel") {
      workspace.collaborationMode = "default";
      workspace.planState = { ...workspace.planState, phase: "cancelled", pendingQuestion: null, canHandoff: false };
      return copy(workspace);
    }
    if (method === "plan.handoff") {
      if (!workspace.planState.canHandoff) throw Error("Repeated review handoff is forbidden");
      const sourceSessionId = workspace.sessionId;
      workspace.sessionId = "smoke-review";
      workspace.messages = [];
      workspace.planState = {
        phase: "ready", activePlanId: plan.planId,
        latestRevision: { ...plan, originSessionId: sourceSessionId },
        pendingQuestion: null, handoffSourceSessionId: sourceSessionId, canHandoff: false,
      };
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
  sessions: () => {
    sessionScenario = true;
    workspace.sessionId = "smoke-1";
    workspace.messages = [{ role: "user", content: "会话甲的消息", timestamp: 1 }];
    workspace.subagents = [];
    workspace.collaborationMode = "default";
    workspace.planState = { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null };
    emit("session.changed", workspace);
  },
  sessionRequests: () => copy(sessionRequests),
  delayNavigation: () => { sessionDelay = true; },
  finishNavigation: () => {
    if (!finishNavigation) throw Error("No pending session navigation");
    finishNavigation();
  },
  ready: () => {
    workspace.planState = { phase: "ready", activePlanId: plan.planId, latestRevision: plan, pendingQuestion: null, canHandoff: true };
    workspace.collaborationMode = "plan";
    emit("session.changed", workspace);
  },
  question: () => {
    workspace.collaborationMode = "plan";
    workspace.planState = {
      phase: "awaiting_answer", activePlanId: plan.planId, latestRevision: null, canHandoff: false,
      pendingQuestion: {
        questionId: "question-smoke", header: "整理力度", allowCustom: true,
        question: "整理计划希望做到哪一步？移动脚本可能改变相对路径，请先确认范围。",
        options: [
          { label: "保守：只分类归档，不删不改脚本（推荐）", description: "保留原始文件和脚本，按目录归档，便于回滚。" },
          { label: "标准：分类归档 + 删除确认清单", description: "在保守方案基础上，列出可删除内容，确认后再清理。" },
          { label: "激进：分类归档 + 每个项目独自包含", description: "调整相对路径并补充测试，需要修改代码。" },
        ],
      },
    };
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
