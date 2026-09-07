# ADR 0001: Centralize Collaboration Mode Policy

- Status: Accepted
- Date: 2026-08-22
- Updated: 2026-09-07

## Context

Coding Agent has a terminal client and an Electron desktop client over the same
session runtime. Plan Mode affects authorization, prompt composition, tool
registration, immutable user confirmation, branch recovery and cross-session
handoff. Prompt-only restrictions or frontend-specific state can diverge after
resume and cannot protect direct shell, custom-tool or RPC entry points.

Command-string allowlists are also not a sufficient read-only boundary. Shell
syntax has multiple composition forms, and apparently read-only Git commands
can invoke configured external diff or textconv programs.

## Decision

`coding_agent.core.AgentSession` is the sole owner of collaboration mode and
Plan State. Every operation appends a branch-local entry and a pure reducer
projects the authoritative state. Frontends render full snapshots and translate
explicit user actions into Core calls; they do not parse natural-language
approval or maintain their own transition graph.

Plan submission is an explicit control operation. `submit_plan` is available
only while drafting, must be the only tool call in its message, validates the
title/Markdown without semantic rewriting, persists an immutable digest-bound
revision and terminates the turn.

Tool availability uses one `plan_access` classification owned by the runtime.
Unclassified tools fail closed. Plan exploration uses Python workspace readers
and structured Git tools; arbitrary shell, tests, builds and mutation tools are
not registered and are blocked again at execution time. Git diff/show disable
external diff and textconv explicitly.

Execution authorization binds the latest `planId`, `revision` and `digest`.
Persisted `completed` means only a settled Agent turn. An orphaned `started`
entry becomes `uncertain`, never inferred success or an automatic retry.

Clean-session execution is a linked handoff: only the confirmed revision and
origin metadata cross into a child session, which remains ready until a second
explicit confirmation.

## Consequences

- CLI/TUI and desktop resume the same branch-local state and security policy.
- New tools and subagents cannot gain Plan access by being omitted from an
  allowlist maintained elsewhere.
- JSONL provides an auditable authorization trail and detects stale or corrupt
  state before execution.
- Plan submission no longer depends on Markdown heuristics or response timing.
- Git observation has concrete external-helper guarantees without claiming a
  general OS sandbox.
- Frontends contain more Plan-specific rendering but no safety-critical state
  transitions.
- Stopping or cancelling Plan work does not undo filesystem, Git, subprocess or
  external effects that already occurred.

## Alternatives rejected

- **Prompt-only Plan Mode:** cannot enforce tool or RPC behavior.
- **Frontend-local state:** diverges across clients and loses recovery truth.
- **Natural-language execution detection:** is ambiguous and unauditable.
- **General shell read allowlist:** cannot safely classify composition, flags,
  aliases and configuration-driven helpers.
- **Copying full planning context into an execution session:** preserves hidden
  instructions and defeats the isolation expected from a fresh handoff.
- **Treating a normal Agent stop as verified completion:** conflates runtime
  lifecycle with implementation evidence.
