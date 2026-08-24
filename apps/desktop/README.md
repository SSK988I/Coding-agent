# Coding Agent Desktop MVP

Electron + React frontend backed by the existing Python `AgentSession` runtime.

## Development

The sidecar uses the repository's `.venv` by default. Override it when needed:

```powershell
$env:CODING_AGENT_PYTHON = "C:\path\to\python.exe"
pnpm install
pnpm dev
```

The desktop includes workspace opening, persistent sessions, streaming messages,
tool execution cards, approval gates for `bash`/`write`/`edit`, and run abort.

Plan Mode is shared with the CLI through the Python `AgentSession`. The composer
mode selector, structured question card, immutable revision card, and explicit
execute/supplement/cancel choices restore from JSONL v4. Supplemental feedback
returns the episode to drafting and cannot authorize execution. The desktop does
not register the CLI `Shift+Tab` shortcut.

Validation:

```powershell
pnpm test
pnpm typecheck
pnpm build
```
