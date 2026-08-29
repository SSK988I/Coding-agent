import { describe, expect, it } from "vitest";
import type { AgentMessage, RuntimeEvent } from "../../shared/types";
import {
  createTimelineState,
  createTimelineStateFromMessages,
  reduceRuntimeEventBatch,
  selectTimelineItems,
  type TimelineCompactionItem,
  type TimelineMessageItem,
  type TimelineToolItem,
} from "./timelineState";

function runtimeEvent(
  seq: number,
  type: string,
  payload: Record<string, unknown> = {},
  runId: string | null = "run-1",
): RuntimeEvent {
  return {
    v: 1,
    type: "event",
    seq,
    timestamp: 1_000 + seq,
    sessionId: "session-1",
    runId,
    event: { type, payload },
  };
}

const assistant = (
  content: AgentMessage["content"],
  extra: Partial<AgentMessage> = {},
): AgentMessage => ({ role: "assistant", content, ...extra });

describe("timeline state", () => {
  it("hydrates messages and compactions into one normalized ordered store", () => {
    const state = createTimelineStateFromMessages([
      { role: "user", content: "hello", timestamp: 10 },
      { role: "user", content: "/model", timestamp: 11 },
      assistant([
        { type: "thinking", thinking: "reason" },
        { type: "text", text: "answer" },
      ], { timestamp: 12, stop_reason: "end_turn" }),
      {
        role: "compactionSummary",
        content: "",
        summary: "older context",
        tokens_before: 4_200,
        timestamp: 13,
      },
    ], "session-1");

    expect(Object.keys(state.entities)).toHaveLength(3);
    expect(selectTimelineItems(state).map((item) => item.kind)).toEqual([
      "message",
      "message",
      "compaction",
    ]);
    expect(selectTimelineItems(state)[1]).toMatchObject({
      role: "assistant",
      text: "answer",
      thinking: "reason",
      status: "end_turn",
    });
    expect(selectTimelineItems(state)[2]).toMatchObject({
      kind: "compaction",
      summary: "older context",
      tokensBefore: 4_200,
    });
  });

  it("coalesces every text and thinking delta for a message into one batch revision", () => {
    const started = reduceRuntimeEventBatch(createTimelineState(), [
      runtimeEvent(1, "message_start", { message: assistant("") }),
    ]);
    const messageId = started.orderedIds[0];
    const startItem = started.entities[messageId] as TimelineMessageItem;

    const reduced = reduceRuntimeEventBatch(started, [
      runtimeEvent(2, "message_update", { kind: "text_delta", delta: "hel" }),
      runtimeEvent(3, "message_update", { kind: "thinking_delta", delta: "why " }),
      runtimeEvent(4, "message_update", { kind: "text_delta", delta: "lo" }),
      runtimeEvent(5, "message_update", { kind: "thinking_delta", delta: "now" }),
    ]);
    const item = reduced.entities[messageId] as TimelineMessageItem;

    expect(item.text).toBe("hello");
    expect(item.thinking).toBe("why now");
    expect(item.revision).toBe(startItem.revision + 1);
    expect(item.lastSeq).toBe(5);
    expect(started.entities[messageId]).toBe(startItem);
  });

  it("keeps interleaved streams isolated by run id and preserves start order", () => {
    const state = reduceRuntimeEventBatch(createTimelineState(), [
      runtimeEvent(1, "message_start", { message: assistant("") }, "run-a"),
      runtimeEvent(2, "message_start", { message: assistant("") }, "run-b"),
      runtimeEvent(3, "message_update", { kind: "text_delta", delta: "A1" }, "run-a"),
      runtimeEvent(4, "message_update", { kind: "text_delta", delta: "B1" }, "run-b"),
      runtimeEvent(5, "message_update", { kind: "text_delta", delta: "A2" }, "run-a"),
    ]);
    const items = selectTimelineItems(state) as TimelineMessageItem[];

    expect(items.map((item) => item.runId)).toEqual(["run-a", "run-b"]);
    expect(items.map((item) => item.text)).toEqual(["A1A2", "B1"]);
    expect(items[0].order).toBeLessThan(items[1].order);
  });

  it("routes explicit message ids even when a newer message is active for the run", () => {
    const state = reduceRuntimeEventBatch(createTimelineState(), [
      runtimeEvent(1, "message_start", {
        messageId: "message-a",
        message: assistant(""),
      }),
      runtimeEvent(2, "message_start", {
        messageId: "message-b",
        message: assistant(""),
      }),
      runtimeEvent(3, "message_update", {
        messageId: "message-a",
        kind: "text_delta",
        delta: "first",
      }),
      runtimeEvent(4, "message_update", {
        kind: "text_delta",
        delta: "second",
      }),
    ]);
    const items = selectTimelineItems(state) as TimelineMessageItem[];

    expect(items.map((item) => [item.messageId, item.text])).toEqual([
      ["message-a", "first"],
      ["message-b", "second"],
    ]);
    expect(items.map((item) => item.revision)).toEqual([1, 1]);
  });

  it("stops routing an ended explicit message without disturbing another active message", () => {
    const state = reduceRuntimeEventBatch(createTimelineState(), [
      runtimeEvent(1, "message_start", {
        messageId: "message-a",
        message: assistant(""),
      }),
      runtimeEvent(2, "message_start", {
        messageId: "message-b",
        message: assistant(""),
      }),
      runtimeEvent(3, "message_end", {
        messageId: "message-a",
        message: assistant("A final"),
      }),
      runtimeEvent(4, "message_update", {
        messageId: "message-a",
        kind: "text_delta",
        delta: " ignored",
      }),
      runtimeEvent(5, "message_update", {
        messageId: "message-b",
        kind: "text_delta",
        delta: "B explicit",
      }),
      runtimeEvent(6, "message_update", {
        kind: "text_delta",
        delta: " active",
      }),
    ]);
    const items = selectTimelineItems(state) as TimelineMessageItem[];

    expect(items.map((item) => item.text)).toEqual(["A final", "B explicit active"]);
  });

  it("uses message_end as the authoritative snapshot and removes empty assistant records", () => {
    const completed = reduceRuntimeEventBatch(createTimelineState(), [
      runtimeEvent(1, "message_start", { message: assistant("") }),
      runtimeEvent(2, "message_update", { kind: "text_delta", delta: "partial" }),
      runtimeEvent(3, "message_end", {
        message: assistant("final", { stop_reason: "end_turn" }),
      }),
    ]);
    expect(selectTimelineItems(completed)).toMatchObject([
      { kind: "message", text: "final", status: "end_turn", revision: 1 },
    ]);

    const removed = reduceRuntimeEventBatch(createTimelineState(), [
      runtimeEvent(1, "message_start", { message: assistant("") }),
      runtimeEvent(2, "message_end", { message: assistant("") }),
    ]);
    expect(selectTimelineItems(removed)).toEqual([]);
  });

  it("updates tool and compaction lifecycle items without changing timeline order", () => {
    const state = reduceRuntimeEventBatch(createTimelineState(), [
      runtimeEvent(1, "message_start", { message: assistant("") }),
      runtimeEvent(2, "tool_execution_start", {
        tool_call_id: "call-1",
        tool_name: "read_file",
        args: { path: "README.md" },
      }),
      runtimeEvent(3, "compaction_start", { reason: "threshold" }),
      runtimeEvent(4, "tool_execution_end", {
        tool_call_id: "call-1",
        tool_name: "read_file",
        result: "contents",
        is_error: false,
      }),
      runtimeEvent(5, "compaction_end", {
        reason: "threshold",
        aborted: false,
        summary_preview: "summary",
      }),
    ]);
    const items = selectTimelineItems(state);
    const tool = items[1] as TimelineToolItem;
    const compaction = items[2] as TimelineCompactionItem;

    expect(items.map((item) => item.kind)).toEqual(["message", "tool", "compaction"]);
    expect(tool).toMatchObject({ status: "done", result: "contents", revision: 1 });
    expect(compaction).toMatchObject({
      status: "completed",
      reason: "threshold",
      summary: "summary",
      revision: 1,
    });
    expect(state.activeCompactionId).toBeNull();
  });

  it("coalesces partial tool snapshots per frame and lets the final result win", () => {
    const started = reduceRuntimeEventBatch(createTimelineState(), [
      runtimeEvent(1, "tool_execution_start", {
        tool_call_id: "shell-1",
        tool_name: "bash",
        args: { command: "pnpm test" },
      }),
    ]);
    const toolId = started.orderedIds[0];

    const updated = reduceRuntimeEventBatch(started, [
      runtimeEvent(2, "tool_execution_update", {
        tool_call_id: "shell-1",
        tool_name: "bash",
        partial_result: { content: [{ type: "text", text: "running" }] },
      }),
      runtimeEvent(3, "tool_execution_update", {
        tool_call_id: "shell-1",
        tool_name: "bash",
        partial_result: { content: [{ type: "text", text: "almost done" }] },
      }),
    ]);
    expect(updated.entities[toolId]).toMatchObject({
      status: "running",
      revision: 1,
      result: { content: [{ text: "almost done" }] },
      lastSeq: 3,
    });

    const completed = reduceRuntimeEventBatch(updated, [
      runtimeEvent(4, "tool_execution_update", {
        tool_call_id: "shell-1",
        tool_name: "bash",
        partial_result: { content: [{ type: "text", text: "stale partial" }] },
      }),
      runtimeEvent(5, "tool_execution_end", {
        tool_call_id: "shell-1",
        tool_name: "bash",
        result: { content: [{ type: "text", text: "complete" }] },
        is_error: false,
      }),
    ]);
    expect(completed.entities[toolId]).toMatchObject({
      status: "done",
      revision: 2,
      result: { content: [{ text: "complete" }] },
      lastSeq: 5,
    });
  });

  it("merges approval events into the matching tool and ignores request_user_input cards", () => {
    const state = reduceRuntimeEventBatch(createTimelineState(), [
      runtimeEvent(1, "tool_execution_start", {
        tool_call_id: "hidden",
        tool_name: "request_user_input",
        args: {},
      }),
      runtimeEvent(2, "tool_execution_start", {
        tool_call_id: "shell-1",
        tool_name: "bash",
        args: { command: "git status" },
      }),
      runtimeEvent(3, "approval.requested", {
        approvalId: "approval-1",
        toolCallId: "shell-1",
        toolName: "bash",
        args: { command: "git status" },
      }),
      runtimeEvent(4, "approval.expired", { toolCallId: "shell-1" }),
    ]);
    const items = selectTimelineItems(state) as TimelineToolItem[];

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      toolCallId: "shell-1",
      name: "bash",
      status: "error",
      result: "工具审批已超时",
      revision: 2,
    });
    expect(items[0].approval).toBeUndefined();
  });

  it("tracks run failure while leaving unrelated runtime events to other stores", () => {
    const initial = createTimelineState();
    const ignored = reduceRuntimeEventBatch(initial, [
      runtimeEvent(1, "model.changed", { id: "another-model" }),
    ]);
    expect(ignored).toBe(initial);

    const failed = reduceRuntimeEventBatch(initial, [
      runtimeEvent(2, "run.started"),
      runtimeEvent(3, "run.failed", { message: "network unavailable" }),
    ]);
    expect(failed.running).toBe(false);
    expect(failed.lastError).toBe("network unavailable");
  });

  it("clears active routing on run completion even without a run.started event", () => {
    const started = reduceRuntimeEventBatch(createTimelineState(), [
      runtimeEvent(1, "message_start", { message: assistant("") }),
    ]);
    const completed = reduceRuntimeEventBatch(started, [
      runtimeEvent(2, "run.completed"),
    ]);
    const afterLateDelta = reduceRuntimeEventBatch(completed, [
      runtimeEvent(3, "message_update", { kind: "text_delta", delta: "late" }),
    ]);

    expect(completed.activeMessageIdsByRun).toEqual({});
    expect(afterLateDelta).toBe(completed);
    expect((selectTimelineItems(completed)[0] as TimelineMessageItem).text).toBe("");
  });
});
