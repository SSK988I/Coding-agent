# Coding Agent Domain Context

## Purpose

Coding Agent is one local programming agent with two presentation adapters: a terminal CLI/TUI and an Electron desktop app. Product state that affects safety, authorization, session recovery, or model context belongs in the shared Python application layer, not in either frontend.

## Ubiquitous language

- **Collaboration Mode**: session-level rule set governing what the agent may do. It is distinct from CLI output `--mode`, thinking level, and interactive/print application mode.
- **Default Mode**: normal collaboration in which development tools may execute under existing approval rules.
- **Plan Mode**: restricted collaboration for exploration, clarification, validation, and a decision-complete implementation plan.
- **Plan Episode**: one interval beginning with explicit Plan entry and identified by `planId`.
- **Plan Question**: the one structured clarification currently awaiting an answer.
- **Plan Revision**: immutable validated plan Markdown identified by `(planId, revision, digest)`.
- **Execution Confirmation**: explicit UI/CLI authorization for the exact latest Plan Revision. Natural language is not confirmation.
- **Session branch**: the active root-to-leaf JSONL entry path used to rebuild messages, settings, and Plan State.
- **Development tool**: read/write/shell tool exposed to the model.
- **Control tool**: host interaction such as `request_user_input`; it remains available when development tools are disabled.

## Ownership boundaries

```text
agent_core                 append-only data model, agent loop, tool metadata
coding_agent.core          AgentSession, Plan state machine, prompt and policy
coding_agent.cli/modes     CLI flags, resumable text/JSON, TUI interaction
coding_agent.desktop       versioned sidecar RPC adapter and approval UI boundary
apps/desktop/renderer      rendering and explicit user controls only
```

The model can propose text and request a structured question. It cannot change Collaboration Mode, bless a revision, or bypass tool policy. Frontends display shared state and submit explicit commands; they do not independently infer Plan transitions.

## Invariants

1. A new session is Default.
2. Only the latest valid revision is executable.
3. `plan_run: started` is the durable execution authorization and Default transition.
4. Plan policy is evaluated before a frontend approval hook.
5. Unknown custom tools are blocked in Plan Mode.
6. Active-branch reduction is the recovery source of truth.
7. Mode-only sessions do not become empty JSONL artifacts.
