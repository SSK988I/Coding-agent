import type { FormEvent, KeyboardEvent, RefObject } from "react";
import { Archive, CornerDownLeft, X } from "lucide-react";
import type { PermissionMode, PlanRevisionPayload, WorkspacePayload } from "../../../shared/types";
import { AttachmentPicker } from "../../attachments/ImageAttachmentPicker";
import { clipboardAttachmentFiles, type PendingAttachment } from "../../attachments/imageAttachments";
import { PermissionPicker } from "../PermissionPicker";

export type QueueDeliveryMode = "steer" | "followUp";

export interface ComposerProps {
  workspace: WorkspacePayload | null;
  composerRef: RefObject<HTMLTextAreaElement | null>;
  input: string;
  running: boolean;
  compacting: boolean;
  sessionNavigating: boolean;
  composerBlocked: boolean;
  conversationReadOnly: boolean;
  supportsImages: boolean;
  attachments: {
    items: readonly PendingAttachment[];
    images: readonly PendingAttachment[];
    reading: boolean;
    addFiles: (files: File[]) => Promise<void>;
    addPastedFiles: (files: File[]) => void;
    remove: (id: string) => void;
    cancelClipboardFallback: () => void;
  };
  readyPlan: PlanRevisionPayload | null;
  canHandoffPlan: boolean;
  awaitingPlanDecision: boolean;
  planCommandMenuOpen: boolean;
  planStatusMessage: string | null;
  planBadgeLabel: string;
  supplementingPlanDigest: string | null;
  pendingSessionArchive: string | null;
  pendingPermissionChange: boolean;
  networkApproval: { approvalId: string; message: string } | null;
  pendingNetworkDecision: boolean;
  error: string | null;
  queueMode: QueueDeliveryMode;
  onQueueModeChange: (mode: QueueDeliveryMode) => void;
  onInputChange: (value: string) => void;
  onInputKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onOpenCommands: () => void;
  onSubmit: (event?: FormEvent) => void;
  onSendQueued: () => void;
  onEnterPlanMode: () => void;
  onCancelPlan: () => void;
  onChangePermission: (mode: PermissionMode) => Promise<void>;
  onResolveNetworkApproval: (approved: boolean) => void;
  onRestoreArchived: () => void;
  onAbortRun: () => void;
  onAbortCompaction: () => void;
  onDismissError: () => void;
}

export function Composer({
  workspace,
  composerRef,
  input,
  running,
  compacting,
  sessionNavigating,
  composerBlocked,
  conversationReadOnly,
  supportsImages,
  attachments,
  readyPlan,
  canHandoffPlan,
  awaitingPlanDecision,
  planCommandMenuOpen,
  planStatusMessage,
  planBadgeLabel,
  supplementingPlanDigest,
  pendingSessionArchive,
  pendingPermissionChange,
  networkApproval,
  pendingNetworkDecision,
  error,
  queueMode,
  onQueueModeChange,
  onInputChange,
  onInputKeyDown,
  onOpenCommands,
  onSubmit,
  onSendQueued,
  onEnterPlanMode,
  onCancelPlan,
  onChangePermission,
  onResolveNetworkApproval,
  onRestoreArchived,
  onAbortRun,
  onAbortCompaction,
  onDismissError,
}: ComposerProps) {
  const canQueueWhileRunning = Boolean(
    workspace && running && !compacting && !composerBlocked && !workspace.archived,
  );

  const placeholder = compacting
    ? "正在压缩上下文，请稍候…"
    : !workspace
      ? "请先打开工作区"
      : supplementingPlanDigest === readyPlan?.digest
        ? "补充你的想法或修改要求…"
        : planCommandMenuOpen
          ? "请先选择 Plan 操作"
          : awaitingPlanDecision
            ? canHandoffPlan ? "请先选择执行、新会话复核或继续修改" : "请先选择执行或继续修改"
            : running
              ? (queueMode === "steer" ? "输入转向指令（立即引导当前运行）…" : "输入后续指令（当前运行结束后执行）…")
              : "描述你想完成的任务…";

  return (
    <>
      {workspace?.archived && (
        <section className="session-archive-banner" role="status">
          <Archive size={15} strokeWidth={1.55} aria-hidden="true" />
          <div>
            <strong>此会话已归档</strong>
            <span>恢复后可继续对话。</span>
          </div>
          <button
            type="button"
            className="ghost-button"
            disabled={Boolean(pendingSessionArchive)}
            onClick={onRestoreArchived}
          >
            {pendingSessionArchive ? "正在恢复…" : "恢复会话"}
          </button>
        </section>
      )}

      {error && (
        <div className="error-banner" role="alert">
          <span>{error}</span>
          <button type="button" onClick={onDismissError} aria-label="关闭错误提示">
            <X size={14} strokeWidth={1.6} />
          </button>
        </div>
      )}

      {networkApproval && (
        <section className="permission-network" role="region" aria-label="联网批准">
          <strong>{networkApproval.message}</strong>
          <p>授权仅在本次任务内有效。拒绝后会继续处理已有内容。</p>
          <div>
            <button
              type="button"
              disabled={pendingNetworkDecision}
              onClick={() => onResolveNetworkApproval(false)}
            >
              继续离线
            </button>
            <button
              type="button"
              className="permission-network__allow"
              disabled={pendingNetworkDecision}
              onClick={() => onResolveNetworkApproval(true)}
            >
              允许本次联网
            </button>
          </div>
        </section>
      )}

      <form
        className={`composer ${workspace?.collaborationMode === "plan" ? "plan-mode" : ""}`}
        onSubmit={onSubmit}
        onDragOver={(event) => {
          if (event.dataTransfer.types.includes("Files")) event.preventDefault();
        }}
        onDrop={(event) => {
          if (!event.dataTransfer.files.length) return;
          event.preventDefault();
          if (!composerBlocked && !running) void attachments.addFiles(Array.from(event.dataTransfer.files));
        }}
        onPaste={(event) => {
          const files = clipboardAttachmentFiles(event.clipboardData);
          if (!files.length) {
            // Ordinary text paste must retain the browser's default action
            // and must not prompt for an unrelated clipboard read.
            if (event.clipboardData.types.includes("text/plain")) attachments.cancelClipboardFallback();
            return;
          }
          event.preventDefault();
          if (!composerBlocked && !running) attachments.addPastedFiles(files);
        }}
      >
        {canQueueWhileRunning && (
          <div className="composer-queue-bar">
            <span className="composer-queue-bar__hint">
              <span className="status-dot status-dot--pulse" aria-hidden="true" />
              运行中 · 可发送转向或排队指令
            </span>
            <div className="composer-queue-bar__switch" role="group" aria-label="运行中消息投递方式">
              <button
                type="button"
                className={queueMode === "steer" ? "active" : ""}
                onClick={() => onQueueModeChange("steer")}
              >
                转向
              </button>
              <button
                type="button"
                className={queueMode === "followUp" ? "active" : ""}
                onClick={() => onQueueModeChange("followUp")}
              >
                排队
              </button>
            </div>
          </div>
        )}

        <textarea
          ref={composerRef}
          value={input}
          onChange={(event) => onInputChange(event.target.value)}
          onKeyDown={onInputKeyDown}
          placeholder={placeholder}
          disabled={composerBlocked}
          rows={2}
        />

        <AttachmentPicker
          items={attachments.items}
          disabled={composerBlocked || running}
          supported={supportsImages}
          reading={attachments.reading}
          onFiles={(files) => { void attachments.addFiles(files); }}
          onRemove={attachments.remove}
        />

        <div className="composer-footer">
          <div className="composer-controls">
            <div className="mode-switch" aria-label="协作模式">
              <button
                type="button"
                className={workspace?.collaborationMode === "default" ? "active" : ""}
                onClick={() => workspace?.collaborationMode === "plan" ? onCancelPlan() : undefined}
                disabled={!workspace || running || compacting || conversationReadOnly}
              >
                Default
              </button>
              <button
                type="button"
                className={workspace?.collaborationMode === "plan" ? "active" : ""}
                onClick={onEnterPlanMode}
                disabled={!workspace || running || compacting || conversationReadOnly}
              >
                Plan
              </button>
              {(workspace?.collaborationMode === "plan" || planStatusMessage) && (
                <span className="plan-badge">{planBadgeLabel}</span>
              )}
            </div>

            <PermissionPicker
              key={workspace?.sessionId}
              mode={workspace?.permissionMode ?? "ask"}
              disabled={
                !workspace
                || running
                || compacting
                || conversationReadOnly
                || sessionNavigating
                || (workspace.subagents ?? []).some((task) => task.status === "queued" || task.status === "running")
              }
              pending={pendingPermissionChange}
              onChange={onChangePermission}
            />

            <button
              type="button"
              className="composer-slash-btn"
              onClick={onOpenCommands}
              disabled={!workspace || running || compacting || composerBlocked}
              title="打开斜杠命令菜单 (/)"
              aria-label="打开命令菜单"
            >
              /
            </button>
          </div>

          <span className="composer-hint">Enter 发送 · Shift+Enter 换行</span>

          <div className="composer-actions">
            {canQueueWhileRunning && input.trim().length > 0 && (
              <button
                type="button"
                className="ghost-button composer-queue-send"
                onClick={onSendQueued}
              >
                <CornerDownLeft size={12} strokeWidth={1.7} aria-hidden="true" />
                {queueMode === "steer" ? "发送转向" : "加入队列"}
              </button>
            )}

            {running ? (
              <button type="button" className="stop-button" onClick={onAbortRun}>■ 停止</button>
            ) : compacting ? (
              <button type="button" className="stop-button" onClick={onAbortCompaction}>■ 停止压缩</button>
            ) : (
              <button
                type="submit"
                className="send-button"
                disabled={
                  composerBlocked
                  || attachments.reading
                  || (!input.trim() && !attachments.items.length)
                  || (attachments.images.length > 0 && !supportsImages)
                }
              >
                发送 ↑
              </button>
            )}
          </div>
        </div>
      </form>
    </>
  );
}
