import { useEffect, useRef, useState } from "react";
import type { PermissionMode } from "../../shared/types";

const OPTIONS: { mode: PermissionMode; label: string; description: string }[] = [
  { mode: "ask", label: "请求批准", description: "编辑外部文件和使用互联网时始终询问" },
  { mode: "auto", label: "帮我批准", description: "仅对检测到的风险操作请求批准" },
  { mode: "full_access", label: "完全访问权限", description: "可访问互联网和工作区外文件，无需逐次批准" },
];

function PermissionIcon({ mode }: { mode: PermissionMode }) {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
      {mode === "ask" ? <path d="M8 12V5a1.5 1.5 0 0 1 3 0v6-8a1.5 1.5 0 0 1 3 0v8-6a1.5 1.5 0 0 1 3 0v7-3a1.5 1.5 0 0 1 3 0v5c0 5-3 8-7 8h-1c-2 0-4-1-5-3l-3-5a1.5 1.5 0 0 1 2.5-1.5L8 12Z" />
        : mode === "auto" ? <><path d="M5 5h14v14H5z" strokeLinejoin="round" /><path d="m8 9 3 3-3 3m5 0h3" /></>
        : <><path d="m12 3 9 4v5c0 5-5 8-9 10-4-2-9-5-9-10V7l9-4Z" strokeLinejoin="round" /><path d="M12 7v6m0 3v1" /></>}
    </svg>
  );
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
      <button ref={buttonRef} type="button" className={`permission-trigger ${mode === "full_access" ? "full-access" : ""}`}
        aria-label={`访问权限：${selected.label}`} aria-haspopup="menu" aria-expanded={open}
        disabled={disabled || pending} onClick={() => setOpen((current) => !current)}>
        <PermissionIcon mode={mode} />{pending ? "正在保存…" : selected.label}<span aria-hidden="true">⌄</span>
      </button>
      {open && (
        <div className="permission-menu" role="menu" aria-label="访问权限" onKeyDown={(event) => {
          if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
          if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
            event.preventDefault();
            const buttons = Array.from(rootRef.current?.querySelectorAll<HTMLButtonElement>("[role=menuitemradio]") ?? []);
            const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
            const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
              : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
            buttons[next]?.focus();
          }
        }}>
          {OPTIONS.map((option) => (
            <button key={option.mode} type="button" role="menuitemradio" aria-checked={mode === option.mode}
              aria-label={option.label} aria-description={option.description}
              data-mode={option.mode} className={`${mode === option.mode ? "selected" : ""} ${option.mode === "full_access" ? "full-access" : ""}`}
              disabled={pending} onClick={() => { close(); if (mode !== option.mode) void onChange(option.mode); }}>
              <PermissionIcon mode={option.mode} />
              <span className="permission-copy"><strong>{option.label}</strong><small>{option.description}</small></span>
              <span className="permission-check" aria-hidden="true">{mode === option.mode ? "✓" : ""}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
