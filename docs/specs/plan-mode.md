# Plan Mode Specification

## Status

Implemented as `plan_mode_v1` by the shared Python `AgentSession`, CLI/TUI, and Electron desktop client. The NDJSON desktop protocol remains v1; JSONL session storage is v4.

## Product contract

A session has one Collaboration Mode:

- `default`: analysis and project mutation are available subject to normal approvals.
- `plan`: repository exploration, structured clarification, validation, and plan drafting are available; implementation and unclassified side effects are blocked.

Only `/plan`, the TUI mode shortcut, CLI control flags, or desktop RPC can change Collaboration Mode. Model output and ordinary chat text cannot change it. A new session starts in Default Mode.

The Plan phases are `idle`, `drafting`, `awaiting_answer`, `ready`, `executing`, `completed`, `failed`, `aborted`, and `cancelled`.

```text
Default/idle --enter--> Plan/drafting
Plan/drafting --question--> Plan/awaiting_answer
Plan/awaiting_answer --answer--> Plan/drafting
Plan/drafting --valid spec--> Plan/ready
Plan/ready --supplemental user feedback--> Plan/drafting
Plan/* --cancel--> Default/cancelled
Plan/ready --exact confirmation--> Default/executing
Default/executing --> Default/completed|failed|aborted
```

While a run is active, mode changes, cancellation, and execution confirmation are rejected. Answering the pending structured question and aborting the run remain available. Leaving Plan Mode never implies execution.

## Plan episode and revision

Entering Plan Mode creates a Plan Episode with a unique `planId`. The preferred assistant response strictly matches one `<proposed_plan>` block with no surrounding text. The Markdown inside the block must contain exactly one H1 plus these H2 sections (English, Chinese, or a bilingual `English / 中文` heading):

- `Summary` / `摘要`
- `Implementation Changes` / `实现变更`
- `Public Interfaces` / `公开接口`
- `Test Plan` / `测试计划`
- `Assumptions` / `假设`

For model compatibility, the shared host may normalize a bare Markdown response only when it is recognizably decision-complete: it has one H1, Summary, Public Interfaces, Test Plan, Assumptions, and one or more implementation-change H2 sections between Summary and Public Interfaces. The host removes leading conversational prose and a trailing natural-language execution question, inserts the implementation umbrella heading when needed, then runs the same strict validator. Incomplete discussions are not normalized and cannot become executable revisions. On resume, the same normalization may recover the latest assistant response in the active Plan Episode, but never crosses a later user-feedback message to revive an obsolete plan.

Line endings are normalized to LF and the Markdown is hashed with SHA-256. Each valid response increments `revision`; a later revision supersedes the earlier one. Execution must match the current `planId`, `revision`, and `digest`, otherwise it fails with `STALE_PLAN_REVISION`.

Execution confirmation appends `plan_run: started`, switches to Default, and immediately prompts the agent with the exact confirmed plan. That single entry is both authorization and the mode transition, so recovery cannot observe a confirmed plan still in Plan Mode.

## Structured questions

`request_user_input` is an application control tool available in Plan Mode even when development tools are disabled. It accepts exactly one question:

- `header`: 1-12 characters.
- `question`: non-empty prompt.
- `options`: 2-3 mutually exclusive label/description pairs.
- `allowCustom`: the frontends expose a custom answer.

Interactive clients wait on the same run. A non-interactive client persists the question and exits successfully; `--answer-plan-question` resumes the same episode. Abort or UI closure cancels the waiting future but preserves the JSONL question.

## Tool policy

Tools declare `effect = read | write | shell | control | unknown`. Missing metadata is `unknown`.

| Effect | Plan Mode |
| --- | --- |
| `read` | Allowed |
| `control` | Allowed |
| `shell` | Allowed only when the shared conservative classifier recognizes it |
| `write` | Blocked |
| `unknown` | Blocked |

The Plan gate runs before frontend approval. A Plan-blocked action returns `PLAN_POLICY_BLOCKED` and never displays an allow-once prompt. The same classifier protects model `bash` calls and TUI `!`/`!!` passthrough.

The shell allowlist recognizes workspace-contained file inspection, read-only Git operations, and explicit validation/build entry points such as pytest, Ruff, Pyright, `uv build`, and safe package scripts. Redirection, substitution, parent traversal, unknown executables, mutating Git/find flags, and output-file flags are rejected.

## JSONL v4

The following branch-aware entries extend the append-only session model:

- `collaboration_mode_change`
- `plan_question`
- `plan_question_answer`
- `plan_revision`
- `plan_run` with `started`, `completed`, `failed`, or `aborted`

Plan State is reduced from the active branch, so `/tree` restores the branch-specific mode, pending question, and latest revision. v1-v3 files open as Default. Before the first v4-only append, an existing header is atomically upgraded without changing entry content. A session containing only mode-switch bookkeeping is not materialized, preserving the empty-session lifecycle.

## CLI and TUI

`--mode text|json` remains the output format. Collaboration controls are:

```text
--agent-mode {default,plan}
--answer-plan-question QUESTION_ID "answer"
--execute-plan REVISION
--cancel-plan
```

The three control flags are mutually exclusive. Control/state errors return 2, an execution failure returns 1, and a pending question or ready plan returns 0. Text output prints a resumable command; JSON events carry the session ID and Plan identifiers.

TUI commands are `/plan`, `/cancel-plan`, and `/execute-plan`. `Shift+Tab` is the primary Default/Plan cycle and `Alt+M` is its terminal fallback. Leaving Plan cancels without execution. Thinking-level cycling moved to `Alt+T`; `Ctrl+T` still toggles thinking-block visibility.

When a revision becomes ready, both interactive clients present an explicit choice between **Execute plan** and **Supplement ideas**. Supplementing returns focus to the composer, keeps Plan Mode active, and changes the phase back to `drafting` when the feedback is sent. That feedback cannot authorize execution; it also makes the prior ready revision temporarily non-executable until a new valid revision is produced. Cancellation remains a separate action.

## Desktop

The desktop bridge exposes:

- `mode.enterPlan`
- `plan.answer(questionId, answer)`
- `plan.cancel(planId)`
- `plan.execute(planId, revision, digest)`

`WorkspacePayload` includes `collaborationMode` and `planState`. The Renderer provides a mode selector, PLAN badge, structured question/custom answer card, revision card, and continue/execute/cancel actions. The execution action submits only the currently rendered revision and digest. Plan control tools do not appear as ordinary tool cards.

Stable errors are `RUN_IN_PROGRESS`, `INVALID_MODE_TRANSITION`, `QUESTION_NOT_PENDING`, `PLAN_NOT_READY`, `STALE_PLAN_REVISION`, and `PLAN_POLICY_BLOCKED`.

## Compatibility and exclusions

This version does not implement accept-edits/auto/bypass modes, historical revision execution, an external plan editor, a task-step dashboard, or user-defined shortcut settings. Desktop deliberately does not register the TUI `Shift+Tab` shortcut.

## Acceptance tests

- Prompt/tool overlays switch without losing the configured base prompt.
- Plan policy runs before desktop approval and covers both `bash` and `!`.
- Plan envelope validation, digest, revision increment, and stale rejection are deterministic.
- Complete bare/bilingual plan output is normalized into a revision, while incomplete Markdown remains drafting.
- Confirmation, cancellation, failure, and abort reduce to the documented states.
- v3-to-v4 upgrade and branch switching preserve transcript and Plan State.
- A mode-only new session produces no JSONL file.
- Non-interactive questions return without hanging and print a resume command.
- TUI mappings are `Shift+Tab`/`Alt+M`, `Alt+T`, and `Ctrl+T` with no conflicts.
- Desktop restores question/revision cards and never submits a superseded digest.
