"""Compact task cards; full reports are available via /subagents show <id>."""
from agent_tui import Container, Text

LABELS = {
    "queued": "等待启动", "running": "调查中", "completed": "报告已返回",
    "failed": "失败", "cancelled": "已停止", "timed_out": "已超时", "uncertain": "结果未知",
}


class SubagentComponent(Container):
    def __init__(self, task: dict):
        super().__init__()
        self.update(task)

    def update(self, task: dict) -> None:
        self.clear()
        task_id = task["taskId"]
        lines = [
            f"只读子代理 · {LABELS.get(task['status'], '结果未知')} · {task_id}",
            task["prompt"][:160],
            f"{task.get('turns', 0)}/{task.get('maxTurns', 12)} 轮 · {task.get('lastTool') or '尚无工具调用'}",
        ]
        if task.get("error"):
            lines.append(task["error"])
        if task.get("output"):
            lines.append(task["output"][:300])
        lines.append(f"查看报告：/subagents show {task_id}")
        if task["status"] in {"queued", "running"}:
            lines.append(f"停止：/subagents cancel {task_id}（或 Esc 停止当前工作）")
        self.add_child(Text("\n".join(lines), padding_x=1, padding_y=1))
