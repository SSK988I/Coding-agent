# ADR 0001: Centralize Collaboration Mode Policy

- Status: Accepted
- Date: 2026-08-22

## Context

Coding Agent has a terminal interface and an Electron desktop interface over the same `AgentSession`. Plan Mode changes authorization, prompt construction, tool availability, session recovery, and the meaning of an execution confirmation. Implementing these rules independently in each frontend would make cross-client resume unsafe and allow one entry point (`bash`, `!`, desktop approval, or non-interactive CLI) to diverge.

## Decision

`coding_agent.core.AgentSession` is the sole owner of Collaboration Mode and Plan State. It composes the Plan prompt overlay, exposes the control API, validates immutable revisions, and wraps the single `before_tool_call` hook so the shared Plan policy runs before any frontend approval.

`agent_core` owns only generic tool-effect metadata and JSONL v4 entry types. The CLI/TUI and desktop runtime translate explicit user actions into the shared API and render emitted state. They do not parse natural-language authorization or maintain shadow state machines.

The shared application module also owns the conservative shell classifier. Both model `bash` and direct TUI shell passthrough use it; desktop approval consumes it after Plan policy.

## Consequences

- A Plan session can be created in one client and safely resumed in the other.
- Tool blocking is consistent and happens before an allow-once prompt.
- JSONL is the recoverable authorization audit trail.
- Frontends require Plan-specific rendering, but contain no safety-critical transition logic.
- Adding a new tool requires declaring its effect; omission intentionally yields the restrictive `unknown` behavior.

## Alternatives rejected

- **Prompt-only Plan Mode**: cannot enforce writes, shell passthrough, or cross-client confirmation.
- **Frontend-local state**: loses state on resume and creates divergent authorization paths.
- **Natural-language execution detection**: ambiguous and not an auditable explicit confirmation.
- **Reuse thinking level or CLI output mode**: conflates unrelated concepts and creates incompatible persistence semantics.
