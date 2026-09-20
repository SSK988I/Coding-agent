import type { SubagentTaskPayload } from "../../shared/types";
import { useRef, useState } from "react";

const LABELS: Record<SubagentTaskPayload["status"], string> = {
  queued: "等待启动", running: "调查中", completed: "报告已返回", failed: "失败",
  cancelled: "已停止", timed_out: "已超时", uncertain: "结果未知",
};

export function SubagentPanel({ tasks, onCancel, onReview }: {
  tasks: SubagentTaskPayload[];
  onCancel: (id: string) => Promise<void>;
  onReview?: () => Promise<void>;
}) {
  const [pending, setPending] = useState(false);
  const busy = useRef(false);
  const request = async (action: () => Promise<void>) => {
    if (busy.current) return;
    busy.current = true;
    setPending(true);
    try { await action(); }
    finally { busy.current = false; setPending(false); }
  };
  return (
    <section className="subagent-panel" aria-label="只读子代理">
      <h3>只读子代理</h3>
      {onReview && <button className="ghost-button" type="button" disabled={pending} onClick={() => void request(onReview)}>只读复核当前计划</button>}
      {pending && <p role="status">正在请求…</p>}
      {!tasks.length && <p>可用 /subagents spawn 任务说明 启动独立调查。</p>}
      {tasks.map((task) => (
        <details key={task.taskId} className="subagent-task">
          <summary>{task.purpose === "review" ? "审查" : "调查"} · {LABELS[task.status] ?? "结果未知"}
            <span>{task.prompt.slice(0, 60)}</span>
          </summary>
          <details className="subagent-brief"><summary>任务说明</summary><p>{task.prompt}</p></details>
          <small>{task.taskId} · {task.turns}/{task.maxTurns} 轮{task.lastTool ? ` · ${task.lastTool}` : ""}</small>
          {(task.status === "queued" || task.status === "running") && (
            <button className="ghost-button" type="button" disabled={pending} onClick={() => void request(() => onCancel(task.taskId))}>停止子代理</button>
          )}
          {task.error && <p role="status">{task.error}</p>}
          {task.output && <pre>{task.output}</pre>}
          {task.status === "completed" && <small>报告结束不代表结论已验证；请核对证据。</small>}
        </details>
      ))}
    </section>
  );
}
