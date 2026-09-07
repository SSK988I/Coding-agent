# Plan Mode Specification

## Status

Plan Mode is implemented by the shared Python `AgentSession` and rendered by
the CLI/TUI and Electron desktop clients. Session storage remains append-only
JSONL v4; new Plan revisions use the v1 digest format while v0 entries remain
readable.

## Runtime contract

A session has one collaboration mode:

- `default`: normal tools are available, subject to project trust and approval.
- `plan`: the runtime exposes only observation and Plan control tools. Project
  mutation, arbitrary shell execution, tests, builds and package scripts are
  unavailable.

Only host controls can enter, cancel, hand off or execute Plan Mode. Model text,
ordinary user messages and frontend-local state never authorize execution.
Leaving Plan Mode never implies execution.

Plan State is reduced from entries on the active branch. Switching `/tree`
therefore restores that branch's mode, pending question, latest revision and
run state rather than a session-global value.

The projected phases are:

```text
idle -> drafting -> awaiting_answer -> drafting -> ready
drafting|awaiting_answer|ready -> cancelled
ready -> executing -> settled|failed|aborted
orphaned executing -> uncertain
invalid persisted transitions or digests -> recovery_error
```

`settled` means only that the Agent execution turn ended normally. It is not
proof that the implementation is correct. Files, diffs, command exit codes and
user acceptance remain separate evidence.

## Plan submission and revisions

The assistant submits a plan only through
`submit_plan({title, markdown})`. The tool is available only while drafting,
must be the sole tool call in its assistant message and terminates that turn.
A normal text response, truncated response, aborted response or legacy
`<proposed_plan>` block cannot create a new executable revision.

The runtime validates a single-line title of at most 200 characters and
Markdown of at most 64 KiB. It normalizes CRLF to LF but does not add
sections, strip prose or otherwise rewrite the submitted plan.

Every accepted submission creates an immutable `PlanRevisionEntry`. New v1
digests are SHA-256 over compact, key-sorted JSON containing
`schemaVersion`, `planId`, `revision`, `title` and normalized `markdown`.
Execution and handoff must match the latest `planId + revision + digest` tuple;
stale confirmation fails closed.

Older digest entries remain readable and are never rewritten. A legacy session
with only assistant plan prose is shown as a legacy candidate: the user must
continue Plan Mode and the assistant must submit a new revision before it can
be executed.

## Questions and controls

`request_user_input` accepts one structured question with a short header, a
non-empty prompt, two or three mutually exclusive choices and an optional
custom response. The question is persisted before the UI waits. Closing a UI
or aborting the wait preserves the pending question for another client.

A bare `/plan` is state-aware:

- `idle`: start a new Plan Episode.
- `drafting`: continue, ask the assistant to submit, or cancel.
- `awaiting_answer`: restore the pending question or cancel.
- `ready`: supplement, execute here, hand off for clean-session review, or
  cancel.
- `executing`: inspect status or request stop.
- `uncertain` / `recovery_error`: inspect details, start a new Plan Episode, or
  cancel. The old run is never retried automatically.

`/cancel-plan` and `/execute-plan` remain compatibility aliases. Non-interactive
controls are:

```text
--agent-mode {default,plan}
--answer-plan-question QUESTION_ID "answer"
--execute-plan REVISION
--handoff-plan [REVISION]
--cancel-plan
```

The control flags are mutually exclusive. Omitting the revision after
`--handoff-plan` selects the latest ready revision.

## Tool policy and Git boundary

Every tool has one runtime-owned `plan_access` classification:
`observe`, `control` or `deny`; missing metadata defaults to `deny`. The same
classification controls tool registration and a pre-execution gate, so a
frontend, custom tool or subagent cannot widen Plan access independently.

Plan Mode exposes Python-backed workspace readers (`read`, `grep`, `find`,
`ls`), Plan controls and structured Git readers. It does not expose `bash`,
write/edit tools, test runners, linters, builders or package managers. Python
backends are used for search and discovery so repository-local executables and
`PATH` shims are not launched.

`git_status`, `git_log`, `git_diff` and `git_show` launch Git with an argument
array rather than through a shell. Pager, terminal prompts, optional locks and
fsmonitor are disabled. Diff/show additionally force
`--no-ext-diff --no-textconv --no-color`; log forces `--no-patch`. Revision and
path operands reject control characters, option-like values and excessive
length. These guarantees prevent external diff and textconv helpers from being
executed. Other hostile Git configuration remains part of the documented
Project Trust boundary, not an OS sandbox guarantee.

Policy rejection uses stable code `PLAN_POLICY_BLOCKED`, includes a reason and
suggests a structured alternative when one exists. It happens before any
frontend allow-once prompt.

## Persistence, execution and recovery

Plan commands append and flush an entry before a pure reducer projects the
next state. The reducer verifies identifiers, monotonic revisions, v0/v1
digests, question-answer pairing, run tuples and transition order. Invalid
history projects `recovery_error` and blocks execution.

Malformed JSONL, invalid UTF-8, duplicate entry IDs and missing parents are
retained as load diagnostics. Because a damaged line may conceal an execution
record, the runtime blocks ordinary prompts and shell passthrough until the
user explicitly cancels or starts a new planning episode. This also applies
when damage prevents the runtime from identifying an active plan. History is
preserved; the explicit recovery action is appended after the diagnostic.

Execution writes and flushes `plan_run: started` before enabling Default tools.
The terminal entry remains one of `completed`, `failed` or `aborted` for JSONL
compatibility; clients project `completed` as `settled`. If a later process
finds `started` without a live runtime owner, it projects `uncertain` and
requires explicit user action.

If the runtime cannot persist a terminal entry, it releases live ownership and
replays the log. A remaining `started` entry becomes `uncertain` immediately,
including in the current process. No retry is scheduled.

Confirmation may record accepted-plan memory. Neither submission alone nor a
`settled` result produces a completed-task memory claim.

Session controls are not transactions. Cancelling a plan, stopping a run,
switching branches, or deleting/closing a session does **not** roll back files,
Git changes, subprocess effects or external service actions already performed.

## Clean-session handoff

Handoff validates the exact latest revision, creates a child session whose
`parent_session` points to the source, and copies only the immutable revision
plus origin metadata. Planning conversation, questions and tool output are not
copied. The child opens in `ready`; execution requires another explicit click
or `--execute-plan` in that child.

The source records `reason=handoff` and the target ID only after the target is
durably created. A failed handoff leaves the source ready. A partially created
child is retained as an incomplete handoff for inspection; it is never executed
or deleted automatically.

## Client projection

Core publishes `plan.stateChanged` with `sessionId` and the complete
authoritative Plan State. The older granular lifecycle events remain available
for one compatibility cycle, but built-in clients render from the full state
snapshot. `session.snapshot` likewise includes `collaborationMode` and
`planState`. Clients may display a pending RPC spinner, but must not
optimistically advance mode or phase.

TUI startup, session creation, branch switching and handoff all rehydrate the
question/action component from Core state. The footer displays `plan`,
`plan ready`, `plan uncertain` or `plan recovery`. Desktop uses the same
snapshot and refetches it after a rejected control request.

## Acceptance criteria

- Only a successful, sole `submit_plan` call creates a ready revision.
- Replaying one active branch deterministically yields the same Plan State.
- Unknown tools, shell commands, tests/builds and mutating tools are denied in
  Plan Mode before frontend approval.
- Structured Git diff/show cannot launch configured external diff or textconv
  helpers.
- A stale tuple cannot execute or hand off; an orphaned run restores as
  `uncertain`.
- A child handoff contains no planning transcript and requires a second
  confirmation.
- TUI and desktop restore the same controls from the same snapshot and always
  expose a safe exit or recovery action.
