import { X } from "lucide-react";
import type { PlanRevisionPayload, WorkspacePayload } from "../../../shared/types";
import type { PendingPlanAction } from "../../utils/format";

export interface PlanControlsProps {
  workspace: WorkspacePayload | null;
  readyPlan: PlanRevisionPayload | null;
  canHandoffPlan: boolean;
  reviewingPlan: boolean;
  awaitingPlanDecision: boolean;
  planCommandMenuOpen: boolean;
  planSubmissionNotice: string | null;
  pendingPlanAction: PendingPlanAction;
  planStatusPhase: WorkspacePayload["planState"]["phase"] | undefined;
  planStatusMessage: string | null;
  planBadgeLabel: string;
  customPlanAnswer: string;
  conversationReadOnly: boolean;
  running: boolean;
  compacting: boolean;
  onChangeCustomAnswer: (value: string) => void;
  onAnswerQuestion: (answer: string) => void;
  onContinueDrafting: () => void;
  onRequestSubmission: () => void;
  onExecutePlan: () => void;
  onHandoffPlan: () => void;
  onSupplementPlan: () => void;
  onCancelPlan: () => void;
  onEnterPlanMode: () => void;
  onAbortExecution: () => void;
}

export function PlanControls({
  workspace,
  readyPlan,
  canHandoffPlan,
  reviewingPlan,
  awaitingPlanDecision,
  planCommandMenuOpen,
  planSubmissionNotice,
  pendingPlanAction,
  planStatusPhase,
  planStatusMessage,
  planBadgeLabel,
  customPlanAnswer,
  conversationReadOnly,
  running,
  compacting,
  onChangeCustomAnswer,
  onAnswerQuestion,
  onContinueDrafting,
  onRequestSubmission,
  onExecutePlan,
  onHandoffPlan,
  onSupplementPlan,
  onCancelPlan,
  onEnterPlanMode,
  onAbortExecution,
}: PlanControlsProps) {
  return (
    <fieldset className="session-plan-controls" disabled={conversationReadOnly}>
      {workspace?.planState.pendingQuestion && (
        <section className="plan-question" data-testid="plan-question">
          <div className="plan-question-header">
            <span className="plan-badge">PLAN</span>
            <strong>{workspace.planState.pendingQuestion.header}</strong>
          </div>
          <p>{workspace.planState.pendingQuestion.question}</p>
          <div className="plan-options">
            {workspace.planState.pendingQuestion.options.map((option) => (
              <button key={option.label} type="button" onClick={() => onAnswerQuestion(option.label)}>
                <strong>{option.label}</strong>
                <span>{option.description}</span>
              </button>
            ))}
          </div>
          {workspace.planState.pendingQuestion.allowCustom && (
            <div className="plan-custom-answer">
              <input
                value={customPlanAnswer}
                onChange={(event) => onChangeCustomAnswer(event.target.value)}
                placeholder="自定义回答"
              />
              <button
                type="button"
                onClick={() => onAnswerQuestion(customPlanAnswer)}
                disabled={!customPlanAnswer.trim()}
              >
                提交
              </button>
            </div>
          )}
          <div className="plan-question-footer">
            <button type="button" className="plan-cancel-button" onClick={onCancelPlan} disabled={compacting}>
              <X size={13} strokeWidth={1.7} aria-hidden="true" />取消规划
            </button>
          </div>
        </section>
      )}

      {planCommandMenuOpen
        && workspace?.collaborationMode === "plan"
        && workspace.planState.phase === "drafting" && (
        <section className="plan-question plan-mode-menu" data-testid="plan-mode-menu">
          <div className="plan-question-header">
            <span className="plan-badge">PLAN</span>
            <strong>当前正在规划</strong>
          </div>
          <p>请选择接下来要执行的 Plan 操作。</p>
          <div className="plan-options">
            <button type="button" onClick={onContinueDrafting} disabled={running || compacting}>
              <strong>继续规划</strong>
              <span>返回输入框，继续补充需求或约束</span>
            </button>
            <button type="button" onClick={onRequestSubmission} disabled={running || compacting}>
              <strong>{pendingPlanAction === "submit" ? "正在整理…" : "整理并提交"}</strong>
              <span>让 Agent 将当前讨论整理为可复核方案</span>
            </button>
            <button type="button" onClick={onCancelPlan} disabled={running || compacting}>
              <strong>取消规划</strong>
              <span>切回 Default，不执行任何方案</span>
            </button>
          </div>
        </section>
      )}

      {planSubmissionNotice && workspace?.planState.phase === "drafting" && (
        <section className="plan-status" data-testid="plan-submission-notice" role="status">
          <p>{planSubmissionNotice}</p>
        </section>
      )}

      {pendingPlanAction === "submit"
        && workspace?.planState.phase === "drafting" && (
        <section
          className="plan-status submitting"
          data-testid="plan-submitting"
          role="status"
          aria-live="polite"
        >
          <div className="plan-question-header">
            <span className="plan-badge">PLAN</span>
            <strong>正在整理并提交计划</strong>
            <span className="plan-spinner" aria-hidden="true" />
          </div>
          <p>正在整理当前讨论；提交成功后会显示计划和确认选项，不会自动执行。可点击下方“停止”中断。</p>
        </section>
      )}

      {workspace?.planState.phase === "executing" && (
        <section className="plan-status executing" data-testid="plan-executing">
          <div className="plan-question-header">
            <span className="plan-badge">PLAN EXECUTING</span>
            <strong>正在执行已确认方案</strong>
          </div>
          <p>运行仍在进行中。停止操作不会回滚已经产生的文件或外部副作用。</p>
          {workspace.planState.activePlanId && (
            <pre>planId: {workspace.planState.activePlanId}{workspace.planState.latestRun?.runId
              ? `\nrunId: ${workspace.planState.latestRun.runId}` : ""}</pre>
          )}
          <div className="plan-options">
            <button type="button" onClick={onAbortExecution} disabled={!running || compacting}>
              <strong>停止执行</strong>
              <span>请求中止当前执行回合</span>
            </button>
          </div>
        </section>
      )}

      {readyPlan && awaitingPlanDecision && (
        <section className="plan-question plan-decision" data-testid="plan-decision">
          <div className="plan-question-header">
            <span className="plan-badge">PLAN READY</span>
            <strong>下一步</strong>
          </div>
          <p>{reviewingPlan ? "当前已在复核会话中，可确认执行或继续修改方案。" : "计划已提交，下一步怎么做？"}</p>
          <div className="plan-options">
            <button type="button" className="plan-option-primary" onClick={onExecutePlan} disabled={running || compacting}>
              <strong>{pendingPlanAction === "execute" ? "正在确认…" : "执行方案"}</strong>
              <span>确认 revision {readyPlan.revision} 并立即切回 Default 执行</span>
            </button>
            {canHandoffPlan && (
              <button type="button" onClick={onHandoffPlan} disabled={running || compacting}>
                <strong>{pendingPlanAction === "handoff" ? "正在创建…" : "新会话复核"}</strong>
                <span>只把这个 revision 交给新会话，打开后再次确认再执行</span>
              </button>
            )}
            <button type="button" onClick={onSupplementPlan} disabled={running || compacting}>
              <strong>继续修改</strong>
              <span>保持 Plan Mode，在输入框中说明要补充或修改的内容</span>
            </button>
          </div>
          <div className="plan-question-footer">
            <button type="button" className="plan-cancel-button" onClick={onCancelPlan} disabled={running || compacting}>
              <X size={13} strokeWidth={1.7} aria-hidden="true" />取消规划
            </button>
          </div>
        </section>
      )}

      {workspace?.planState.legacyCandidate && (
        <section className="plan-status" data-testid="plan-legacy">
          <p>检测到旧版计划候选文本，尚未形成可执行 revision。请继续规划，并通过 submit_plan 重新提交后再确认执行。</p>
        </section>
      )}

      {planStatusMessage && (
        <section className={`plan-status ${planStatusPhase ?? ""}`} data-testid="plan-status">
          <div className="plan-question-header">
            <span className="plan-badge">{planBadgeLabel}</span>
            <strong>{planStatusPhase === "uncertain" ? "执行状态不确定" : "计划恢复失败"}</strong>
          </div>
          <p>{planStatusMessage}</p>
          {workspace?.planState.recoveryError && (
            <pre>{workspace.planState.recoveryError.code}: {workspace.planState.recoveryError.message}</pre>
          )}
          <details>
            <summary>查看记录详情</summary>
            <pre>{JSON.stringify(workspace?.planState.latestRun ?? workspace?.planState.recoveryError, null, 2)}</pre>
          </details>
          <div className="plan-options">
            <button type="button" onClick={onEnterPlanMode} disabled={running || compacting}>重新规划</button>
            <button type="button" onClick={onCancelPlan} disabled={running || compacting}>取消规划</button>
          </div>
        </section>
      )}
    </fieldset>
  );
}
