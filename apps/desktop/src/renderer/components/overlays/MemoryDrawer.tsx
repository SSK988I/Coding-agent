import { useCallback, useEffect, useRef, useState } from "react";
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

interface PendingRecordForget {
  workspaceKey: string;
  id: string;
}

interface PendingConflictForget {
  workspaceKey: string;
  scope: string;
  key: string;
}

interface PendingClearState {
  workspaceKey: string;
  target: "project" | "all";
}

function formatMemoryValue(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function MemoryDrawer({
  open,
  workspaceKey,
  workspacePath,
  sessionId,
  memory,
  disabled,
  onClose,
  onMemoryChange,
}: {
  open: boolean;
  workspaceKey?: string;
  workspacePath?: string;
  sessionId?: string;
  memory: MemoryStatePayload;
  disabled: boolean;
  onClose: () => void;
  onMemoryChange: (next: MemoryStatePayload) => void;
}) {
  const activeWorkspaceKey = workspaceKey ?? JSON.stringify([memory.projectId ?? null, memory.userId ?? null]);
  const scopeParams = {
    ...(workspacePath ? { workspacePath } : {}),
    ...(sessionId ? { sessionId } : {}),
  };
  const [tab, setTab] = useState<MemoryTab>("all");
  const [records, setRecords] = useState<MemoryListItem[]>([]);
  const [conflicts, setConflicts] = useState<MemoryConflictItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rememberText, setRememberText] = useState("");
  const [rememberScope, setRememberScope] = useState<"project" | "global">("project");
  const [pendingForget, setPendingForget] = useState<PendingRecordForget | null>(null);
  const [pendingConflictForget, setPendingConflictForget] = useState<PendingConflictForget | null>(null);
  const [pendingClear, setPendingClear] = useState<PendingClearState | null>(null);
  const [busyAction, setBusyAction] = useState(false);

  const mountedRef = useRef(true);
  const openRef = useRef(open);
  const workspaceKeyRef = useRef(activeWorkspaceKey);
  const loadGenerationRef = useRef(0);

  openRef.current = open;
  workspaceKeyRef.current = activeWorkspaceKey;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      loadGenerationRef.current += 1;
    };
  }, []);

  const loadAll = useCallback(async () => {
    const generation = ++loadGenerationRef.current;
    const targetWorkspaceKey = workspaceKeyRef.current;
    setLoading(true);
    setError(null);
    try {
      const [status, list, conflictList] = await Promise.all([
        window.agent.request<MemoryStatePayload>("memory.status"),
        window.agent.request<MemoryListItem[]>("memory.list", {}),
        window.agent.request<MemoryConflictItem[]>("memory.conflicts", {}),
      ]);
      if (
        !mountedRef.current
        || !openRef.current
        || workspaceKeyRef.current !== targetWorkspaceKey
        || loadGenerationRef.current !== generation
      ) {
        return;
      }
      onMemoryChange(status);
      setRecords(Array.isArray(list) ? list : []);
      setConflicts(Array.isArray(conflictList) ? conflictList : []);
    } catch (reason) {
      if (
        !mountedRef.current
        || !openRef.current
        || workspaceKeyRef.current !== targetWorkspaceKey
        || loadGenerationRef.current !== generation
      ) {
        return;
      }
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (
        mountedRef.current
        && workspaceKeyRef.current === targetWorkspaceKey
        && loadGenerationRef.current === generation
      ) {
        setLoading(false);
      }
    }
  }, [onMemoryChange]);

  useEffect(() => {
    loadGenerationRef.current += 1;
    setTab("all");
    setRecords([]);
    setConflicts([]);
    setLoading(false);
    setError(null);
    setRememberText("");
    setPendingForget(null);
    setPendingConflictForget(null);
    setPendingClear(null);
    setBusyAction(false);
  }, [activeWorkspaceKey]);

  useEffect(() => {
    if (!open) {
      loadGenerationRef.current += 1;
      setLoading(false);
      setError(null);
      setPendingForget(null);
      setPendingConflictForget(null);
      setPendingClear(null);
      return;
    }
    void loadAll();
  }, [open, activeWorkspaceKey, loadAll]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const isCurrentWorkspace = (targetKey: string) => (
    mountedRef.current && workspaceKeyRef.current === targetKey
  );

  const toggleEnabled = async () => {
    if (disabled || busyAction) return;
    const targetWorkspaceKey = workspaceKeyRef.current;
    setBusyAction(true);
    setError(null);
    try {
      const next = await window.agent.request<MemoryStatePayload>("memory.setEnabled", {
        enabled: !memory.enabled,
        ...scopeParams,
      });
      if (!isCurrentWorkspace(targetWorkspaceKey)) return;
      onMemoryChange(next);
    } catch (reason) {
      if (isCurrentWorkspace(targetWorkspaceKey)) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (isCurrentWorkspace(targetWorkspaceKey)) setBusyAction(false);
    }
  };

  const toggleAutoExtract = async () => {
    if (disabled || busyAction || !memory.enabled) return;
    const targetWorkspaceKey = workspaceKeyRef.current;
    setBusyAction(true);
    setError(null);
    try {
      const next = await window.agent.request<MemoryStatePayload>("memory.setAutoExtract", {
        enabled: memory.autoExtractEnabled === false,
        ...scopeParams,
      });
      if (!isCurrentWorkspace(targetWorkspaceKey)) return;
      onMemoryChange(next);
    } catch (reason) {
      if (isCurrentWorkspace(targetWorkspaceKey)) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (isCurrentWorkspace(targetWorkspaceKey)) setBusyAction(false);
    }
  };

  const handleRemember = async (event: React.FormEvent) => {
    event.preventDefault();
    const content = rememberText.trim();
    if (!content || disabled || busyAction) return;
    const targetWorkspaceKey = workspaceKeyRef.current;
    setBusyAction(true);
    setError(null);
    try {
      const result = await window.agent.request<{ memory: MemoryStatePayload }>("memory.remember", {
        content,
        scope: rememberScope,
        ...scopeParams,
      });
      if (!isCurrentWorkspace(targetWorkspaceKey)) return;
      if (result.memory) onMemoryChange(result.memory);
      setRememberText("");
      await loadAll();
    } catch (reason) {
      if (isCurrentWorkspace(targetWorkspaceKey)) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (isCurrentWorkspace(targetWorkspaceKey)) setBusyAction(false);
    }
  };

  const handleForgetRecord = async (item: MemoryListItem) => {
    if (
      disabled
      || busyAction
      || !pendingForget
      || pendingForget.workspaceKey !== workspaceKeyRef.current
      || pendingForget.id !== item.record.id
    ) {
      return;
    }
    const targetWorkspaceKey = pendingForget.workspaceKey;
    setBusyAction(true);
    setError(null);
    try {
      const result = await window.agent.request<{ memory: MemoryStatePayload }>("memory.forget", {
        key: item.record.id,
        scope: item.scope === "global" || item.scope === "project" ? item.scope : undefined,
        confirmed: true,
        ...scopeParams,
      });
      if (!isCurrentWorkspace(targetWorkspaceKey)) return;
      if (result.memory) onMemoryChange(result.memory);
      setPendingForget(null);
      await loadAll();
    } catch (reason) {
      if (isCurrentWorkspace(targetWorkspaceKey)) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (isCurrentWorkspace(targetWorkspaceKey)) setBusyAction(false);
    }
  };

  const handleForgetConflict = async (item: MemoryConflictItem) => {
    if (
      disabled
      || busyAction
      || !pendingConflictForget
      || pendingConflictForget.workspaceKey !== workspaceKeyRef.current
      || pendingConflictForget.scope !== item.scope
      || pendingConflictForget.key !== item.conflict.key
    ) {
      return;
    }
    const targetWorkspaceKey = pendingConflictForget.workspaceKey;
    setBusyAction(true);
    setError(null);
    try {
      const result = await window.agent.request<{ memory: MemoryStatePayload }>("memory.forget", {
        key: item.conflict.key,
        scope: item.scope === "global" || item.scope === "project" ? item.scope : undefined,
        confirmed: true,
        ...scopeParams,
      });
      if (!isCurrentWorkspace(targetWorkspaceKey)) return;
      if (result.memory) onMemoryChange(result.memory);
      setPendingConflictForget(null);
      await loadAll();
    } catch (reason) {
      if (isCurrentWorkspace(targetWorkspaceKey)) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (isCurrentWorkspace(targetWorkspaceKey)) setBusyAction(false);
    }
  };

  const handleClear = async () => {
    if (disabled || busyAction || !pendingClear || pendingClear.workspaceKey !== workspaceKeyRef.current) {
      return;
    }
    const targetWorkspaceKey = pendingClear.workspaceKey;
    const allScopes = pendingClear.target === "all";
    setBusyAction(true);
    setError(null);
    try {
      const result = await window.agent.request<{ memory: MemoryStatePayload }>("memory.clear", {
        allScopes,
        confirmed: true,
        ...scopeParams,
      });
      if (!isCurrentWorkspace(targetWorkspaceKey)) return;
      if (result.memory) onMemoryChange(result.memory);
      setPendingClear(null);
      await loadAll();
    } catch (reason) {
      if (isCurrentWorkspace(targetWorkspaceKey)) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (isCurrentWorkspace(targetWorkspaceKey)) setBusyAction(false);
    }
  };

  const activePendingClear = pendingClear?.workspaceKey === activeWorkspaceKey ? pendingClear : null;
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
            conflicts.length ? conflicts.map((item) => {
              const isPendingConflict = pendingConflictForget?.workspaceKey === activeWorkspaceKey
                && pendingConflictForget.scope === item.scope
                && pendingConflictForget.key === item.conflict.key;
              return (
                <div className="memory-drawer__card memory-drawer__card--conflict" key={`${item.scope}:${item.conflict.key}`}>
                  <div className="memory-drawer__card-meta">
                    <code>{item.conflict.key}</code>
                    <span className="memory-drawer__card-meta-actions">
                      <span>{item.scope === "project" ? "项目" : "全局"} · 冲突未注入</span>
                      {!isPendingConflict && (
                        <button
                          type="button"
                          onClick={() => {
                            setPendingForget(null);
                            setPendingClear(null);
                            setPendingConflictForget({
                              workspaceKey: activeWorkspaceKey,
                              scope: item.scope,
                              key: item.conflict.key,
                            });
                          }}
                          disabled={disabled || busyAction}
                        >
                          遗忘整组冲突
                        </button>
                      )}
                    </span>
                  </div>
                  <div className="memory-drawer__candidates">
                    {item.conflict.candidates.map((candidate) => (
                      <div className="memory-drawer__candidate" key={candidate.id}>
                        <span>{formatMemoryValue(candidate.value)}</span>
                        {candidate.authority && <small>{candidate.authority}</small>}
                      </div>
                    ))}
                  </div>
                  {isPendingConflict && (
                    <div
                      className="memory-drawer__conflict-confirm"
                      role="region"
                      aria-label={`确认遗忘冲突组 ${item.conflict.key}`}
                    >
                      <p>
                        遗忘冲突键 <code>{item.conflict.key}</code> 将同时删除以下全部{" "}
                        {item.conflict.candidates.length} 个候选值并写入墓碑：
                      </p>
                      <ul>
                        {item.conflict.candidates.map((candidate) => (
                          <li key={candidate.id}>{formatMemoryValue(candidate.value)}</li>
                        ))}
                      </ul>
                      <div className="memory-drawer__confirm-row">
                        <button
                          type="button"
                          className="danger"
                          onClick={() => void handleForgetConflict(item)}
                          disabled={disabled || busyAction}
                        >
                          确认遗忘整组
                        </button>
                        <button type="button" onClick={() => setPendingConflictForget(null)}>取消</button>
                      </div>
                    </div>
                  )}
                </div>
              );
            }) : <p className="memory-drawer__empty">当前没有待解决的记忆冲突。</p>
          ) : (
            visibleRecords.length ? visibleRecords.map((item) => {
              const isPendingRecord = pendingForget?.workspaceKey === activeWorkspaceKey
                && pendingForget.id === item.record.id;
              return (
                <div className="memory-drawer__card" key={item.record.id}>
                  <div className="memory-drawer__card-meta">
                    <span>{item.scope === "project" ? "项目" : "全局"} · {item.record.kind}</span>
                    {isPendingRecord ? (
                      <span className="memory-drawer__confirm-row">
                        <button
                          type="button"
                          className="danger"
                          onClick={() => void handleForgetRecord(item)}
                          disabled={disabled || busyAction}
                        >
                          确认遗忘
                        </button>
                        <button type="button" onClick={() => setPendingForget(null)}>取消</button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        onClick={() => {
                          setPendingConflictForget(null);
                          setPendingClear(null);
                          setPendingForget({ workspaceKey: activeWorkspaceKey, id: item.record.id });
                        }}
                        disabled={disabled || busyAction}
                      >
                        遗忘
                      </button>
                    )}
                  </div>
                  <div className="memory-drawer__card-value">
                    {formatMemoryValue(item.record.value)}
                  </div>
                  <div className="memory-drawer__card-foot">
                    <code>{item.record.key}</code>
                    <span>{item.record.updated_at}</span>
                  </div>
                </div>
              );
            }) : !loading && <p className="memory-drawer__empty">暂无匹配的长期记忆。</p>
          )}
        </div>

        <footer className="memory-drawer__footer">
          <span>遗忘会写入墓碑，防止旧会话重放恢复</span>
          {activePendingClear ? (
            <div className="memory-drawer__confirm-row">
              <button
                type="button"
                className="danger"
                onClick={() => void handleClear()}
                disabled={disabled || busyAction}
              >
                确认清空{activePendingClear.target === "all" ? "全部" : "项目"}
              </button>
              <button type="button" onClick={() => setPendingClear(null)}>取消</button>
            </div>
          ) : (
            <button
              type="button"
              className="memory-drawer__clear-btn"
              onClick={() => {
                setPendingForget(null);
                setPendingConflictForget(null);
                setPendingClear({ workspaceKey: activeWorkspaceKey, target: "project" });
              }}
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
