import { useCallback, useEffect, useMemo, useRef } from "react";
import { ArrowDown } from "lucide-react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import { Composer } from "./components/composer/Composer";
import { Sidebar } from "./components/layout/Sidebar";
import { Titlebar } from "./components/layout/Titlebar";
import { CommandPalette } from "./components/overlays/CommandPalette";
import { MemoryDrawer } from "./components/overlays/MemoryDrawer";
import { ModelPicker } from "./components/overlays/ModelPicker";
import { PlanControls } from "./components/plan/PlanControls";
import { TimelineCompactionCard } from "./components/timeline/TimelineCompaction";
import { TimelineMessageCard } from "./components/timeline/TimelineMessage";
import { TimelinePlanCard } from "./components/timeline/TimelinePlanCard";
import {
  TimelineToolGroupEntry,
  TimelineToolGroupFooter,
  TimelineToolGroupHeader,
} from "./components/timeline/TimelineToolGroup";
import { TurnChangesCard } from "./components/TurnChangesCard";
import { useDesktopSession, type RenderTimelineItem } from "./hooks/useDesktopSession";
import { useTheme } from "./hooks/useTheme";
import { UsageAnalysisLauncher } from "./usage/UsageAnalysisLauncher";
import { Workbench } from "./workbench/Workbench";

export function App() {
  const { theme, toggleTheme } = useTheme();
  const session = useDesktopSession();
  const virtuosoRef = useRef<VirtuosoHandle>(null);

  const {
    workspace,
    renderTimelineItems,
    lastTimelineSignature,
    isAtBottom,
    setIsAtBottom,
    resolveApproval,
    recoverEdit,
    toggleToolGroup,
    planBadgeLabel,
    viewTurnChanges,
    setInput,
    setSidebarOpen,
    setWorkbenchOpen,
    openMemoryDrawer,
    toggleMemoryDrawer,
  } = session;

  const toggleSidebar = useCallback(() => setSidebarOpen((value) => !value), [setSidebarOpen]);
  const toggleWorkbench = useCallback(() => setWorkbenchOpen((value) => !value), [setWorkbenchOpen]);

  const virtuosoComponents = useMemo(() => ({
    Header: () => <div className="timeline-spacer timeline-spacer--top" aria-hidden="true" />,
    Footer: () => <div className="timeline-spacer timeline-spacer--bottom" aria-hidden="true" />,
    EmptyPlaceholder: () => (
      <div className="timeline-row timeline-row--welcome">
        <section className="welcome-card">
          <span className="eyebrow">CODING AGENT</span>
          <h1>把 Agent 放进一个真正的工作区</h1>
          <p>支持流式响应、结构化工具轨迹、Plan 协作、长期记忆与内置工作台。</p>
          <div className="prompt-chips">
            {["概览这个项目的架构", "找出最值得优化的模块", "运行测试并分析失败原因"].map((value) => (
              <button type="button" key={value} onClick={() => setInput(value)}>{value}</button>
            ))}
          </div>
        </section>
      </div>
    ),
  }), [setInput]);

  const renderTimelineItem = useCallback((_index: number, item: RenderTimelineItem) => {
    if (item.kind === "working") {
      return (
        <div className="timeline-row">
          <div className="working-indicator" data-testid="working-indicator" aria-live="polite">
            <span className="working-shimmer">Working</span>
          </div>
        </div>
      );
    }
    if (item.kind === "plan") {
      return (
        <div className="timeline-row">
          <TimelinePlanCard plan={item.plan} badge={planBadgeLabel} />
        </div>
      );
    }
    if (item.kind === "message") {
      return (
        <div className="timeline-row">
          <TimelineMessageCard message={item} />
        </div>
      );
    }
    if (item.kind === "compaction") {
      return (
        <div className="timeline-row">
          <TimelineCompactionCard compaction={item} />
        </div>
      );
    }
    if (item.kind === "changes") {
      return (
        <div className="timeline-row">
          <TurnChangesCard change={item.change} onView={viewTurnChanges} />
        </div>
      );
    }
    if (item.kind === "tool-group-header") {
      return (
        <div className="timeline-row">
          <TimelineToolGroupHeader
            groupId={item.groupId}
            label={item.label}
            toolCount={item.toolCount}
            hasApproval={item.hasApproval}
            hasRunning={item.hasRunning}
            hasError={item.hasError}
            errorCount={item.errorCount}
            isOpen={item.isOpen}
            onToggle={toggleToolGroup}
          />
        </div>
      );
    }
    if (item.kind === "tool-group-footer") {
      return (
        <div className="timeline-row">
          <TimelineToolGroupFooter
            label={item.label}
            railToneClass={item.railToneClass}
          />
        </div>
      );
    }
    return (
      <div className="timeline-row">
        <TimelineToolGroupEntry
          tool={item.tool}
          railToneClass={item.railToneClass}
          isFirst={item.isFirst}
          cwd={workspace?.path}
          onResolveApproval={resolveApproval}
          onRecoverEdit={recoverEdit}
        />
      </div>
    );
  }, [resolveApproval, recoverEdit, toggleToolGroup, workspace?.path, planBadgeLabel, viewTurnChanges]);

  useEffect(() => {
    if (!isAtBottom || renderTimelineItems.length === 0) return;
    const frame = requestAnimationFrame(() => {
      virtuosoRef.current?.scrollToIndex({
        index: renderTimelineItems.length - 1,
        align: "end",
        behavior: "auto",
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [isAtBottom, lastTimelineSignature, renderTimelineItems.length, workspace?.sessionId]);

  const reviewPlanWithSubagent = workspace
    && !workspace.archived
    && workspace.planState.phase === "ready"
    && workspace.planState.latestRevision
    && !session.running
    && !session.compacting
    ? () => session.manageSubagents(
        "review",
        `只读复核下列计划：检查遗漏、风险和代码依据，不执行、不修改或提交计划。\n${workspace.planState.latestRevision?.markdown}`,
      )
    : undefined;

  const activeSessionName = useMemo(() => {
    if (!workspace) return null;
    const match = session.sessions.find((item) => item.id === workspace.sessionId);
    return match ? (match.name || match.first_message || null) : null;
  }, [session.sessions, workspace]);

  const memoryWorkspaceKey = workspace
    ? JSON.stringify([workspace.path, workspace.memory.projectId ?? null])
    : "";

  return (
    <div className="app-shell">
      <Titlebar
        workspace={workspace}
        activeSessionName={activeSessionName}
        sidecarStatus={session.sidecarStatus}
        statusLabel={session.statusLabel}
        sidebarOpen={session.sidebarOpen}
        workbenchOpen={session.workbenchOpen}
        memoryDrawerOpen={session.memoryDrawerOpen}
        planBadgeLabel={planBadgeLabel}
        planStatusActive={Boolean(session.planStatusMessage)}
        theme={theme}
        busyWorkspaceActions={session.running || session.compacting || session.sessionNavigating || session.workspaceSwitching}
        memoryLocked={session.workspaceSwitching || session.workspaceSyncUncertain}
        onToggleSidebar={toggleSidebar}
        onToggleWorkbench={toggleWorkbench}
        onToggleMemoryDrawer={toggleMemoryDrawer}
        onToggleTheme={toggleTheme}
        onChooseWorkspace={() => void session.chooseWorkspace()}
      />

      <div
        className={`workspace-grid ${session.sidebarOpen ? "" : "sidebar-closed"} ${session.workbenchOpen ? "with-workbench" : ""}`}
        style={{ "--workbench-width": `${session.workbenchWidth}px` } as React.CSSProperties}
      >
        <Sidebar
          workspace={workspace}
          sessions={session.sessions}
          showArchivedSessions={session.showArchivedSessions}
          sessionsLoading={session.sessionsLoading}
          sessionListError={session.sessionListError}
          pendingSessionNavigation={session.pendingSessionNavigation}
          pendingSessionArchive={session.pendingSessionArchive}
          disabledSessions={
            !workspace
            || session.running
            || session.compacting
            || session.sessionNavigating
            || session.workspaceSwitching
            || Boolean(session.pendingSessionArchive)
          }
          disabledWorkspaceControls={
            !workspace
            || session.running
            || session.compacting
            || session.sessionNavigating
            || session.workspaceSwitching
          }
          memoryLocked={session.workspaceSwitching || session.workspaceSyncUncertain}
          running={session.running}
          compacting={session.compacting}
          sessionNavigating={session.sessionNavigating}
          onToggleArchivedSessions={session.toggleArchivedSessions}
          onNewSession={() => void session.newSession()}
          onOpenSession={(id) => void session.openSession(id)}
          onArchiveSession={(id, archived) => void session.setSessionArchived(id, archived)}
          onRetrySessions={() => void session.refreshSessions()}
          onCancelSubagent={(taskId) => session.manageSubagents("cancel", taskId)}
          onReviewPlanWithSubagent={reviewPlanWithSubagent}
          onOpenModelPicker={() => void session.openModelPicker()}
          onToggleMemory={() => void session.executeSlashCommand(
            "memory",
            workspace?.memory.enabled ? "off" : "on",
          )}
          onToggleAutoExtract={() => void session.executeSlashCommand(
            "memory",
            workspace?.memory.autoExtractEnabled === false ? "auto on" : "auto off",
          )}
          onToggleWebSearch={() => void session.toggleWebSearch()}
          onOpenMemoryDrawer={openMemoryDrawer}
        />

        <main className="conversation" aria-busy={session.sessionNavigating} inert={session.sessionNavigating}>
          {session.toast && (
            <div
              key={session.toast.id}
              className="floating-toast"
              role="status"
              aria-live="polite"
              data-testid="floating-toast"
            >
              <span
                className={`status-dot ${session.toast.tone === "ok" ? "status-dot--ok" : ""}`.trim()}
                aria-hidden="true"
              />
              <span>{session.toast.text}</span>
            </div>
          )}

          <Virtuoso
            ref={virtuosoRef}
            className="timeline"
            aria-label="对话时间线"
            data={renderTimelineItems}
            initialItemCount={Math.min(renderTimelineItems.length, 20)}
            increaseViewportBy={{ top: 600, bottom: 800 }}
            atBottomThreshold={72}
            atBottomStateChange={setIsAtBottom}
            followOutput={(atBottom) => atBottom ? "auto" : false}
            computeItemKey={(_index, item) => item.id}
            components={virtuosoComponents}
            itemContent={renderTimelineItem}
          />

          <div className="composer-wrap">
            {!isAtBottom && renderTimelineItems.length > 0 && (
              <button
                type="button"
                className="jump-latest"
                aria-label="回到最新消息"
                onClick={() => virtuosoRef.current?.scrollToIndex({
                  index: renderTimelineItems.length - 1,
                  align: "end",
                  behavior: "smooth",
                })}
              >
                <ArrowDown size={13} strokeWidth={1.7} aria-hidden="true" />
                最新消息
              </button>
            )}

            <PlanControls
              workspace={workspace}
              readyPlan={session.readyPlan}
              canHandoffPlan={session.canHandoffPlan}
              reviewingPlan={session.reviewingPlan}
              awaitingPlanDecision={session.awaitingPlanDecision}
              planCommandMenuOpen={session.planCommandMenuOpen}
              planSubmissionNotice={session.planSubmissionNotice}
              pendingPlanAction={session.pendingPlanAction}
              planStatusPhase={session.planStatusPhase}
              planStatusMessage={session.planStatusMessage}
              planBadgeLabel={planBadgeLabel}
              customPlanAnswer={session.customPlanAnswer}
              conversationReadOnly={session.conversationReadOnly}
              running={session.running}
              compacting={session.compacting}
              onChangeCustomAnswer={session.setCustomPlanAnswer}
              onAnswerQuestion={(answer) => void session.answerPlanQuestion(answer)}
              onContinueDrafting={session.continuePlanDrafting}
              onRequestSubmission={() => void session.requestPlanSubmission()}
              onExecutePlan={() => void session.executePlan()}
              onHandoffPlan={() => void session.handoffPlan()}
              onSupplementPlan={session.supplementPlan}
              onCancelPlan={() => void session.cancelPlan()}
              onEnterPlanMode={() => void session.enterPlanMode()}
              onAbortExecution={() => void session.abortPlanExecution()}
            />

            {session.commandMenuOpen && (
              <CommandPalette
                commands={session.visibleCommands}
                selectedIndex={session.selectedCommandIndex}
                searchable={session.commandMenuMode === "palette"}
                query={session.commandPaletteQuery}
                onQueryChange={session.onCommandPaletteQueryChange}
                onSelectIndex={session.setSelectedCommandIndex}
                onExecute={(name) => void session.executeSlashCommand(name, "", {
                  consumeInput: session.commandMenuMode === "inline",
                })}
                onClose={session.closeCommandMenu}
              />
            )}

            {session.modelPickerOpen && (
              <ModelPicker
                models={session.visibleModels}
                query={session.modelQuery}
                onQueryChange={session.setModelQuery}
                onSelect={(model) => void session.selectModel(model)}
                onClose={() => session.setModelPickerOpen(false)}
              />
            )}

            <Composer
              workspace={workspace}
              composerRef={session.composerRef}
              input={session.input}
              running={session.running}
              canQueueWhileRunning={session.canQueueWhileRunning}
              queuing={session.queuing}
              compacting={session.compacting}
              sessionNavigating={session.sessionNavigating}
              composerBlocked={session.composerBlocked}
              conversationReadOnly={session.conversationReadOnly}
              commandMenuOpen={session.commandMenuOpen}
              supportsImages={session.supportsImages}
              attachments={session.attachments}
              readyPlan={session.readyPlan}
              canHandoffPlan={session.canHandoffPlan}
              awaitingPlanDecision={session.awaitingPlanDecision}
              planCommandMenuOpen={session.planCommandMenuOpen}
              pendingPlanAction={session.pendingPlanAction}
              planStatusMessage={session.planStatusMessage}
              planBadgeLabel={planBadgeLabel}
              supplementingPlanDigest={session.supplementingPlanDigest}
              pendingSessionArchive={session.pendingSessionArchive}
              pendingPermissionChange={session.pendingPermissionChange}
              networkApproval={session.networkApproval}
              pendingNetworkDecision={session.pendingNetworkDecision}
              error={session.error}
              queueMode={session.queueMode}
              onQueueModeChange={session.setQueueMode}
              onInputChange={session.onInputChange}
              onInputFocus={session.onInputFocus}
              onInputKeyDown={session.onInputKeyDown}
              onOpenCommands={session.toggleCommandMenu}
              onSubmit={session.submit}
              onSendQueued={() => void session.sendQueuedMessage()}
              onEnterPlanMode={() => void session.enterPlanMode()}
              onCancelPlan={() => void session.cancelPlan()}
              onChangePermission={session.changePermission}
              onResolveNetworkApproval={(approved) => void session.resolveNetworkApproval(approved)}
              onRestoreArchived={() => workspace ? void session.setSessionArchived(workspace.sessionId, false) : undefined}
              onAbortRun={() => void session.abortPlanExecution()}
              onAbortCompaction={session.abortCompaction}
              onDismissError={() => session.setError(null)}
            />

            <UsageAnalysisLauncher
              key={JSON.stringify([workspace?.path, workspace?.sessionId])}
              sessionId={workspace?.sessionId}
              workspacePath={workspace?.path}
              stats={workspace?.stats}
              onOpenChange={session.setUsageAnalysisOpen}
            />
          </div>
        </main>

        {workspace && (
          <Workbench
            key={JSON.stringify([workspace.path, workspace.sessionId])}
            workspace={workspace}
            open={session.workbenchOpen}
            onClose={session.closeWorkbench}
            width={session.workbenchWidth}
            onWidth={session.setWorkbenchWidth}
            sidebarOpen={session.sidebarOpen}
            suspended={session.usageAnalysisOpen || session.memoryDrawerOpen}
            requestedChange={session.requestedChange}
          />
        )}
      </div>

      {workspace && (
        <MemoryDrawer
          key={memoryWorkspaceKey}
          workspaceKey={memoryWorkspaceKey}
          workspacePath={workspace.path}
          sessionId={workspace.sessionId}
          open={session.memoryDrawerOpen}
          memory={workspace.memory}
          disabled={
            session.running
            || session.compacting
            || session.sessionNavigating
            || session.workspaceSwitching
            || session.workspaceSyncUncertain
          }
          onClose={session.closeMemoryDrawer}
          onMemoryChange={session.setMemoryState}
        />
      )}
    </div>
  );
}
