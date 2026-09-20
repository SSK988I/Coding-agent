# Long-task runtime

## Tool and run outcomes

Core owns tool status. `tool_execution_start` means preparation (`pending`);
`tool_execution_running` follows argument validation and approval. A blocked
call never emits running. New result messages and end events carry `status`:

| Status | Meaning |
| --- | --- |
| `completed` | The tool returned normally, including exit code zero for shell commands |
| `failed` | Execution raised or a shell command returned nonzero |
| `cancelled` | The runtime observed cancellation |
| `timed_out` | The operation exceeded its time limit |
| `blocked` | Policy or approval rejected execution |
| `uncertain` | No trustworthy terminal result is available |

Legacy results without status use their existing `is_error` flag. An assistant
tool call with no matching result is displayed as unknown after restoration.
Late output cannot turn a terminal card back into running. Frontends also leave
unterminated tool cards unknown when the enclosing run stops.

Provider failures returned as assistant messages produce `run.failed`, not
`run.completed`. A completed run/report still does not prove implementation
correctness. Plan execution retains the separate `settled` projection.

## Directional compaction

`/compact` retains ordinary recent-context compaction. `/compact <direction>`
summarizes the whole active context around a user-supplied next-phase objective.
The direction accepts 1–2000 characters. The brief asks for decisions, constraints,
current code state, file references, evidence, failed attempts and next steps.

Only a nonempty, normally finished model response is accepted. Failed, aborted,
truncated and oversized (over 64 KiB per summary response) results are rejected.
The summary operation has a 90-second limit. Cancellation closes the built-in
stream producer, discards its result and keeps the existing context.

Success appends a durable compaction entry on the same session branch. Its
`details.contextPivotDirection` records the direction; `firstKeptEntryId` points
to the marker itself, so no older message remains in active model context.
Original entries remain available for inspection and branch switching. Plan
revision, digest, pending question, phase and execution confirmation are unchanged.
The summary is context, not permission to execute the next phase.

Manual compaction reserves the session against another prompt or session change.
A result is discarded if the source branch changed while the model was working.
Active subagents must finish or be stopped first; automatic compaction is deferred
while child tasks are active. A summary append failure does not replace the live
agent transcript.

Desktop calls `session.compact` with an optional `direction`, displays progress,
and uses `run.abort` to stop it. TUI uses the same Core method and Esc. No new
session is created by compaction.

## Read-only subagents

The parent can use `subagent_spawn`, `subagent_status`, `subagent_wait` and
`subagent_cancel`. Users can use these commands in either interactive client:

```text
/subagents
/subagents spawn <self-contained investigation brief>
/subagents review <self-contained review brief>
/subagents show <task ID>
/subagents cancel <task ID>
```

The ready Plan menu/sidebar has a read-only review shortcut. Unlike clean-session
handoff, review returns a report to the existing session and does not require or
grant execution confirmation. A large plan may exceed the task-brief limit; in
that case supply a narrower review brief with relevant file references.

Each child receives a fresh Agent, the supplied brief, and the parent's model,
credentials and reasoning settings. It does not receive parent chat history,
memory extraction, write tools, shell, Plan controls or delegation tools. Only
fresh instances of allowed built-in `read/grep/find/ls` and structured Git readers
are registered; custom tools cannot self-certify access. Parent tool exclusions
and approval hooks still apply. Search uses Python backends. Structured Git keeps
the existing no-external-diff/no-textconv protections.

This is a capability boundary, not an OS sandbox. Reads use the existing local
filesystem trust model, and other Git configuration remains within Project Trust.
The model provider still receives the brief and inspected content and may charge
for usage. Child reports should cite evidence and identify uncertainty.

Default bounds are three simultaneous children, 32 task records per branch,
12 model turns and 180 seconds per task. A task brief is limited to 12000 characters;
a report to 16000 characters with an explicit truncation notice. Status lists
sent to the parent model contain previews, while an explicit ID retrieves the
report. `subagent_wait` waits at most 30 seconds and never retries or restarts work.

Task lifecycle snapshots are durable `subagent_task` JSONL entries, not chat
messages. Child intermediate messages/tool output are not copied into the parent.
Only explicit result retrieval adds a report to the model's context. Clients
receive `subagent.stateChanged {sessionId, tasks}` snapshots, and workspace/session
snapshots include `subagents`. Desktop RPC exposes `subagent.spawn/list/status/wait/cancel`.

A task's launch entry must still belong to the active session branch before its
progress or terminal result can be appended or shown. Switching branches/sessions
stops detached live children; late results do not enter the new branch. Restored
`queued/running` records without a live owner project `uncertain`. There is no
automatic restart. `completed` means a report returned, not verified findings.

Stopping the parent or closing the session stops its children. Desktop can stop
individual tasks from their cards; TUI supports `/subagents cancel <id>` and Esc.
Session operations do not undo filesystem, Git or external side effects.

## Offline verification

Core tests use fake streams, temporary repositories and session files. Desktop
Vitest covers action routing, immediate feedback, authoritative recovery and stale
session events. `pnpm test:smoke` in `apps/desktop` builds the production renderer
and drives a hidden Electron window with a fixture-only bridge and blocked network.
It checks Plan submission feedback, directional compaction, task stop/report
display and wide/narrow windows, then prints a temporary screenshot directory.
Linux hosts need a display or `xvfb-run`; the smoke test makes no provider calls.
