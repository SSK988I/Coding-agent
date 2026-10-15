# ADR 0002: Own long-task outcomes and delegated context in Core

- Status: Accepted
- Date: 2026-09-20

## Context

UI events previously could describe a call as running before approval and a
normally returned coroutine as successful even when its provider message failed.
Long investigations also need a way to reduce context or delegate a bounded
review without changing Plan confirmation.

## Decision

Core emits explicit preparation, running and terminal tool states. Clients
project those states and keep missing results unknown. Built-in stream consumers
own producer cancellation so stopping the UI also stops background generation.

A user-directed context pivot writes a compaction marker on the same branch.
The original log and Plan state remain intact. Failure, cancellation or a stale
branch cannot install the generated summary as current model context.

Read-only child Agents receive explicit briefs and a fresh restricted tool set.
They have independent transcripts and bounded execution. Their task snapshots
are persisted separately from conversation messages, and results reach the
parent only through explicit retrieval. Frontends share the task manager rather
than spawning their own processes or inferring lifecycle states.

## Consequences

Both clients can show consistent progress and restore unknown outcomes honestly.
Subagent review does not replace or authorize Plan execution; clean-session
handoff remains a separate operation. No write-capable delegation, recursive
orchestration, automatic retries, OS sandbox or file rollback is introduced.
