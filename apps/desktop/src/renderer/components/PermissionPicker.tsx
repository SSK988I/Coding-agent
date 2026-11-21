import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Hand, ShieldAlert, Terminal } from "lucide-react";
import type { PermissionMode } from "../../shared/types";

const OPTIONS: { mode: PermissionMode; label: string; description: string }[] = [
  { mode: "ask", label: "请求批准", description: "编辑外部文件和使用互联网时始终询问" },
  { mode: "auto", label: "帮我批准", description: "仅对检测到的风险操作请求批准" },
  { mode: "full_access", label: "完全访问权限", description: "可访问互联网和工作区外文件，无需逐次批准" },
];

function PermissionIcon({ mode }: { mode: PermissionMode }) {
  if (mode === "ask") return <Hand size={14} strokeWidth={1.6} aria-hidden="true" />;
  if (mode === "auto") return <Terminal size={14} strokeWidth={1.6} aria-hidden="true" />;
  return <ShieldAlert size={14} strokeWidth={1.6} aria-hidden="true" />;
}

export function PermissionPicker({ mode, disabled, pending, onChange }: {
  mode: PermissionMode;
  disabled: boolean;
  pending: boolean;
  onChange: (mode: PermissionMode) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const selected = OPTIONS.find((option) => option.mode === mode) ?? OPTIONS[0];

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useEffect(() => {
    if (!open) return;
    rootRef.current?.querySelector<HTMLButtonElement>(`[data-mode="${mode}"]`)?.focus();
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open, mode]);

  const close = () => { setOpen(false); buttonRef.current?.focus(); };

  return (
    <div className="permission-picker" ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className={`permission-trigger ${mode === "full_access" ? "full-access" : ""}`}
        aria-label={`访问权限：${selected.label}`}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled || pending}
        onClick={() => setOpen((current) => !current)}
      >
        <PermissionIcon mode={mode} />
        <span>{pending ? "正在保存…" : selected.label}</span>
        <ChevronDown size={13} strokeWidth={1.6} aria-hidden="true" />
      </button>

      {open && (
        <div
          className="permission-menu"
          role="menu"
          aria-label="访问权限"
          onKeyDown={(event) => {
            if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
            if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
              event.preventDefault();
              const buttons = Array.from(rootRef.current?.querySelectorAll<HTMLButtonElement>("[role=menuitemradio]") ?? []);
              const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
              const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
                : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
              buttons[next]?.focus();
            }
          }}
        >
          {OPTIONS.map((option) => (
            <button
              key={option.mode}
              type="button"
              role="menuitemradio"
              aria-checked={mode === option.mode}
              aria-label={option.label}
              aria-description={option.description}
              data-mode={option.mode}
              className={`${mode === option.mode ? "selected" : ""} ${option.mode === "full_access" ? "full-access" : ""}`}
              disabled={pending}
              onClick={() => { close(); if (mode !== option.mode) void onChange(option.mode); }}
            >
              <PermissionIcon mode={option.mode} />
              <span className="permission-copy">
                <strong>{option.label}</strong>
                <small>{option.description}</small>
              </span>
              <span className="permission-check" aria-hidden="true">
                {mode === option.mode ? <Check size={14} strokeWidth={1.8} /> : ""}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
