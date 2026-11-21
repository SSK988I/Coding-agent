import { useEffect, useRef, type KeyboardEvent } from "react";
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
  searchable = false,
  query = "",
  onQueryChange,
  onSelectIndex,
  onExecute,
  onClose,
}: {
  commands: CommandOption[];
  selectedIndex: number;
  searchable?: boolean;
  query?: string;
  onQueryChange?: (value: string) => void;
  onSelectIndex: (index: number) => void;
  onExecute: (name: string) => void;
  onClose?: () => void;
}) {
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!searchable) return;
    searchRef.current?.focus();
  }, [searchable]);

  const handleSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      onSelectIndex(commands.length ? (selectedIndex + 1) % commands.length : 0);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      onSelectIndex(commands.length ? (selectedIndex - 1 + commands.length) % commands.length : 0);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      onClose?.();
      return;
    }
    if ((event.key === "Enter" || event.key === "Tab") && commands.length) {
      event.preventDefault();
      const command = commands[Math.min(selectedIndex, commands.length - 1)];
      onExecute(command.name);
    }
  };

  return (
    <section className="command-palette" aria-label="斜杠命令">
      <div className="command-palette-title">
        <strong>命令</strong>
        <span>↑↓ 选择 · Enter 执行 · Esc 关闭</span>
      </div>
      {searchable && (
        <div className="command-palette-search">
          <input
            ref={searchRef}
            type="search"
            value={query}
            onChange={(event) => onQueryChange?.(event.target.value)}
            onKeyDown={handleSearchKeyDown}
            placeholder="输入命令名筛选…"
            aria-label="筛选命令"
          />
        </div>
      )}
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
