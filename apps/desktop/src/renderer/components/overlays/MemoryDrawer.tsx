import { useCallback, useEffect, useState } from "react";
import { RefreshCw, Trash2, X } from "lucide-react";
import type { MemoryStatePayload } from "../../../shared/types";

interface MemoryListItem {
  scope: string;
  record: {
    id: string;
    key: string;
    value: unknown;
    kind: string;
    status: string;
    authority: string;
    updated_at: string;
  };
}

interface MemoryConflictItem {
  scope: string;
  conflict: {
    key: string;
    candidates: Array<{ id: string; value: unknown; authority?: string; updated_at?: string }>;
  };
}

type MemoryTab = "all" | "global" | "project" | "conflicts";

export function MemoryDrawer({
  open,
  memory,
  disabled,
  onClose,
  onMemoryChange,
}: {
  open: boolean;
  memory: MemoryStatePayload;
  disabled: boolean;
  onClose: () => void;
  onMemoryChange: (next: MemoryStatePayload) => void;
}) {
  const [tab, setTab] = useState<MemoryTab>("all");
  const [records, setRecords] = useState<MemoryListItem[]>([]);
  const [conflicts, setConflicts] = useState<MemoryConflictItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rememberText, setRememberText] = useState("");
  const [rememberScope, setRememberScope] = useState<"project" | "global">("project");
  const [pendingForgetId, setPendingForgetId] = useState<string | null>(null);
  const [pendingClear, setPendingClear] = useState<"project" | "all" | null>(null);
  const [busyAction, setBusyAction] = useState(false);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [status, list, conflictList] = await Promise.all([
        window.agent.request<MemoryStatePayload>("memory.status"),
        window.agent.request<MemoryListItem[]>("memory.list", {}),
        window.agent.request<MemoryConflictItem[]>("memory.conflicts", {}),
      ]);
      onMemoryChange(status);
      setRecords(Array.isArray(list) ? list : []);
      setConflicts(Array.isArray(conflictList) ? conflictList : []);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [onMemoryChange]);

  useEffect(() => {
    if (!open) return;
    void loadAll();
  }, [open, loadAll]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const toggleEnabled = async () => {
    if (disabled || busyAction) return;
    setBusyAction(true);
    setError(null);
    try {
      const next = await window.agent.request<MemoryStatePayload>("memory.setEnabled", {
        enabled: !memory.enabled,
      });
      onMemoryChange(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction(false);
    }
  };

  const toggleAutoExtract = async () => {
    if (disabled || busyAction || !memory.enabled) return;
    setBusyAction(true);
    setError(null);
    try {
      const next = await window.agent.request<MemoryStatePayload>("memory.setAutoExtract", {
        enabled: memory.autoExtractEnabled === false,
      });
      onMemoryChange(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction(false);
    }
  };

  const handleRemember = async (event: React.FormEvent) => {
    event.preventDefault();
    const content = rememberText.trim();
    if (!content || disabled || busyAction) return;
    setBusyAction(true);
    setError(null);
    try {
      const result = await window.agent.request<{ memory: MemoryStatePayload }>("memory.remember", {
        content,
        scope: rememberScope,
      });
      if (result.memory) onMemoryChange(result.memory);
      setRememberText("");
      await loadAll();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction(false);
    }
  };

  const handleForget = async (keyOrId: string, scope?: string) => {
    if (disabled || busyAction) return;
    setBusyAction(true);
    setError(null);
    try {
      const result = await window.agent.request<{ memory: MemoryStatePayload }>("memory.forget", {
        key: keyOrId,
        scope: scope === "global" || scope === "project" ? scope : undefined,
        confirmed: true,
      });
      if (result.memory) onMemoryChange(result.memory);
      setPendingForgetId(null);
      await loadAll();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction(false);
    }
  };

  const handleClear = async (allScopes: boolean) => {
    if (disabled || busyAction) return;
    setBusyAction(true);
    setError(null);
    try {
      const result = await window.agent.request<{ memory: MemoryStatePayload }>("memory.clear", {
        allScopes,
        confirmed: true,
      });
      if (result.memory) onMemoryChange(result.memory);
      setPendingClear(null);
      await loadAll();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction(false);
    }
  };

  const visibleRecords = records.filter((item) => tab === "all" || item.scope === tab);
  const globalCount = records.filter((item) => item.scope === "global").length;
  const projectCount = records.filter((item) => item.scope === "project").length;

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <section
        className="memory-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="长期记忆管理"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="memory-drawer__header">
          <div>
            <h2>长期记忆</h2>
            <span>用户 {memory.userId ?? "local-user"} · 按全局与项目隔离</span>
          </div>
          <div className="memory-drawer__header-actions">
            <button
              type="button"
              className="titlebar-icon-btn"
              onClick={() => void loadAll()}
              disabled={loading}
              aria-label="刷新记忆"
              title="刷新记忆"
            >
              <RefreshCw size={14} strokeWidth={1.6} />
            </button>
            <button
              type="button"
              className="titlebar-icon-btn"
              onClick={onClose}
              aria-label="关闭长期记忆管理"
            >
              <X size={15} strokeWidth={1.6} />
            </button>
          </div>
        </header>

        <div className="memory-drawer__controls">
          <div className="memory-drawer__toggles">
            <button
              type="button"
              className="memory-drawer__toggle-card"
              onClick={() => void toggleEnabled()}
              disabled={disabled || busyAction}
            >
              <span>总开关</span>
              <strong>
                <span className={`status-dot ${memory.enabled ? "status-dot--ok" : ""}`} aria-hidden="true" />
                {memory.enabled ? "已开启" : "已关闭"}
              </strong>
            </button>
            <button
              type="button"
              className="memory-drawer__toggle-card"
              onClick={() => void toggleAutoExtract()}
              disabled={disabled || busyAction || !memory.enabled}
            >
              <span>自动提取</span>
              <strong>
                <span
                  className={`status-dot ${memory.enabled && memory.autoExtractEnabled !== false ? "status-dot--ok" : ""}`}
                  aria-hidden="true"
                />
                {memory.autoExtractEnabled === false ? "手动模式" : "任务后自动"}
              </strong>
            </button>
          </div>

          <div className="memory-drawer__queue">
            <span>后台提取队列</span>
            <strong>
              待 {memory.pendingCount ?? 0} · 处 {memory.processingCount ?? 0} · 完 {memory.readyCount ?? 0} · 败 {memory.failedCount ?? 0}
            </strong>
          </div>

          {memory.lastError && <p className="memory-drawer__error">{memory.lastError}</p>}
          {error && <p className="memory-drawer__error" role="alert">{error}</p>}

          <form className="memory-drawer__form" onSubmit={(event) => void handleRemember(event)}>
            <input
              type="search"
              value={rememberText}
              onChange={(event) => setRememberText(event.target.value)}
              placeholder="快速记住一条长期偏好或项目约定…"
              disabled={disabled || busyAction}
              aria-label="新增长期记忆"
            />
            <select
              value={rememberScope}
              onChange={(event) => setRememberScope(event.target.value as "project" | "global")}
              disabled={disabled || busyAction}
              aria-label="记忆作用域"
            >
              <option value="project">项目</option>
              <option value="global">全局</option>
            </select>
            <button
              type="submit"
              className="send-button"
              disabled={disabled || busyAction || !rememberText.trim()}
            >
              记住
            </button>
          </form>

          <div className="memory-drawer__tabs" role="tablist">
            {(
              [
                ["all", `全部 ${records.length}`],
                ["global", `全局 ${globalCount}`],
                ["project", `项目 ${projectCount}`],
                ["conflicts", `冲突 ${conflicts.length}`],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                className={tab === id ? "active" : ""}
                onClick={() => setTab(id)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        <div className="memory-drawer__list">
          {loading && !records.length && <p className="memory-drawer__empty">正在加载长期记忆…</p>}

          {tab === "conflicts" ? (
            conflicts.length ? conflicts.map((item) => (
              <div className="memory-drawer__card memory-drawer__card--conflict" key={`${item.scope}:${item.conflict.key}`}>
                <div className="memory-drawer__card-meta">
                  <code>{item.conflict.key}</code>
                  <span>{item.scope === "project" ? "项目" : "全局"} · 冲突未注入</span>
                </div>
                <div className="memory-drawer__candidates">
                  {item.conflict.candidates.map((candidate) => (
                    <div className="memory-drawer__candidate" key={candidate.id}>
                      <span>{typeof candidate.value === "string" ? candidate.value : JSON.stringify(candidate.value)}</span>
                      <button
                        type="button"
                        onClick={() => void handleForget(candidate.id, item.scope)}
                        disabled={disabled || busyAction}
                      >
                        遗忘
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )) : <p className="memory-drawer__empty">当前没有待解决的记忆冲突。</p>
          ) : (
            visibleRecords.length ? visibleRecords.map((item) => (
              <div className="memory-drawer__card" key={item.record.id}>
                <div className="memory-drawer__card-meta">
                  <span>{item.scope === "project" ? "项目" : "全局"} · {item.record.kind}</span>
                  {pendingForgetId === item.record.id ? (
                    <span className="memory-drawer__confirm-row">
                      <button
                        type="button"
                        className="danger"
                        onClick={() => void handleForget(item.record.id, item.scope)}
                        disabled={disabled || busyAction}
                      >
                        确认遗忘
                      </button>
                      <button type="button" onClick={() => setPendingForgetId(null)}>取消</button>
                    </span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setPendingForgetId(item.record.id)}
                      disabled={disabled || busyAction}
                    >
                      遗忘
                    </button>
                  )}
                </div>
                <div className="memory-drawer__card-value">
                  {typeof item.record.value === "string" ? item.record.value : JSON.stringify(item.record.value)}
                </div>
                <div className="memory-drawer__card-foot">
                  <code>{item.record.key}</code>
                  <span>{item.record.updated_at}</span>
                </div>
              </div>
            )) : !loading && <p className="memory-drawer__empty">暂无匹配的长期记忆。</p>
          )}
        </div>

        <footer className="memory-drawer__footer">
          <span>遗忘会写入墓碑，防止旧会话重放恢复</span>
          {pendingClear ? (
            <div className="memory-drawer__confirm-row">
              <button
                type="button"
                className="danger"
                onClick={() => void handleClear(pendingClear === "all")}
                disabled={disabled || busyAction}
              >
                确认清空{pendingClear === "all" ? "全部" : "项目"}
              </button>
              <button type="button" onClick={() => setPendingClear(null)}>取消</button>
            </div>
          ) : (
            <button
              type="button"
              className="memory-drawer__clear-btn"
              onClick={() => setPendingClear("project")}
              disabled={disabled || busyAction}
            >
              <Trash2 size={12} strokeWidth={1.6} aria-hidden="true" />
              清空项目记忆
            </button>
          )}
        </footer>
      </section>
    </div>
  );
}
