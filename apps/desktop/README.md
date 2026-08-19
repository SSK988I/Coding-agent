# Coding Agent Desktop MVP

Electron + React frontend backed by the existing Python `AgentSession` runtime.

## Development

The sidecar uses the repository's `.venv` by default. Override it when needed:

```powershell
$env:CODING_AGENT_PYTHON = "C:\path\to\python.exe"
pnpm install
pnpm dev
```

The MVP includes workspace opening, persistent sessions, streaming messages,
tool execution cards, approval gates for `bash`/`write`/`edit`, and run abort.
