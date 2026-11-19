import {
  Activity,
  Bot,
  Brain,
  ClipboardList,
  Cpu,
  HelpCircle,
  Minimize2,
  Plus,
  RotateCcw,
  Terminal,
} from "lucide-react";
import type { CommandOption } from "../../utils/format";

function CommandIcon({ name }: { name: string }) {
  switch (name) {
    case "help": return <HelpCircle size={14} strokeWidth={1.55} />;
    case "new": return <Plus size={14} strokeWidth={1.6} />;
    case "model": return <Cpu size={14} strokeWidth={1.55} />;
    case "compact": return <Minimize2 size={14} strokeWidth={1.55} />;
    case "clear": return <RotateCcw size={14} strokeWidth={1.55} />;
    case "session": return <Activity size={14} strokeWidth={1.55} />;
    case "memory": return <Brain size={14} strokeWidth={1.55} />;
    case "subagents": return <Bot size={14} strokeWidth={1.55} />;
    case "plan":
    case "execute-plan":
    case "cancel-plan":
      return <ClipboardList size={14} strokeWidth={1.55} />;
    default:
      return <Terminal size={14} strokeWidth={1.55} />;
  }
}

export function CommandPalette({
  commands,
  selectedIndex,
  onSelectIndex,
  onExecute,
}: {
  commands: CommandOption[];
  selectedIndex: number;
  onSelectIndex: (index: number) => void;
  onExecute: (name: string) => void;
}) {
  return (
    <section className="command-palette" aria-label="斜杠命令">
      <div className="command-palette-title">
        <strong>命令</strong>
        <span>↑↓ 选择 · Enter 执行 · Esc 关闭</span>
      </div>
      <div className="command-list">
        {commands.map((command, index) => (
          <button
            type="button"
            key={command.name}
            className={index === selectedIndex ? "active" : ""}
            onMouseEnter={() => onSelectIndex(index)}
            onClick={() => onExecute(command.name)}
          >
            <span className="command-icon" aria-hidden="true">
              <CommandIcon name={command.name} />
            </span>
            <span className="command-name">
              <strong>{command.label}</strong>
              <small>/{command.name}</small>
            </span>
            <em>{command.description}</em>
          </button>
        ))}
        {!commands.length && <p>没有匹配的命令。</p>}
      </div>
    </section>
  );
}
