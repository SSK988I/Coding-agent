# Coding Agent

[简体中文](README.zh-CN.md) · **English**

Coding Agent is a programming agent for local development workflows. Its agent runtime is implemented in Python, with both a terminal interface and an Electron + React desktop MVP.

It can read and modify project files, search code, execute shell commands, and stream model responses and tool progress to the interface. Conversations are stored as JSONL and support restoration, branches, and context compaction.

> The project is in an early stage of development. The terminal application provides the broader feature set; the desktop runtime is connected and usable, while packaging and additional desktop features are still in progress.

## Features

- **Streaming agent loop**: processes model output, tool calls, tool results, and subsequent reasoning, with abort, steering, and follow-up support.
- **Seven built-in development tools**: `read`, `write`, `edit`, `grep`, `find`, `ls`, and `bash`.
- **Restorable sessions**: stores messages and settings changes in append-only JSONL and tracks the active branch through an entry tree.
- **Context management**: includes token estimation, summary compaction, and compact-and-retry after context overflow.
- **Multiple model providers**: currently includes catalogs for DeepSeek and Z.AI Coding Plan (China).
- **Project context**: discovers `AGENTS.md`, `CLAUDE.md`, skills, and prompt templates.
- **Terminal UI**: renders Markdown, streaming content, tool cards, model selection, and line-based differential updates.
- **Desktop MVP**: supports workspace selection, session history, streaming messages, tool approval, model switching, and a slash-command palette.

## Interfaces

### Terminal

The terminal application is currently the primary entry point. It is designed to run directly inside a project directory and includes the complete interactive command set, project-context discovery, session branches, and exports.

### Desktop

The desktop application uses Electron + React with the Python `AgentSession` running as a separate sidecar. The renderer has no direct access to the shell, filesystem, or API keys. Native operations cross a restricted Electron IPC bridge and a versioned NDJSON RPC boundary before reaching the Python runtime.

The desktop MVP currently supports:

- Opening a local project and switching between saved sessions
- Streaming response text and thinking content
- Tool execution cards and result updates
- Approval prompts for `bash`, `write`, and `edit`
- A command palette that opens when `/` is entered
- `/help`, `/new`, `/model`, `/compact`, `/clear`, and `/session`

## Requirements

### Python runtime

- Python 3.13 or newer
- [uv](https://docs.astral.sh/uv/)
- Windows, Linux, or macOS
- Git for Windows is recommended; the application prefers Git Bash on Windows

### Desktop development

- Node.js
- pnpm
- A project virtual environment created by `uv sync`

## Quick start

### 1. Clone and install dependencies

```powershell
git clone https://github.com/SSK988I/Coding-agent.git
cd Coding-agent
uv sync
```

### 2. Configure provider credentials

DeepSeek:

```powershell
$env:DEEPSEEK_API_KEY = "your API key"
```

Z.AI Coding Plan:

```powershell
$env:ZAI_CODING_CN_API_KEY = "your API key"
```

You can also run `/login` from the terminal interface. Stored credentials are written to `~/.coding-agent/auth.json`.

### 3. Start the terminal interface

```powershell
uv run coding-agent
```

Start with an initial task:

```powershell
uv run coding-agent "Read this project and explain its main modules"
```

### 4. Start the desktop application

```powershell
cd apps/desktop
pnpm install
pnpm dev
```

Development mode uses the repository's `.venv` for the Python sidecar by default. To select a different Python executable:

```powershell
$env:CODING_AGENT_PYTHON = "C:\path\to\python.exe"
pnpm dev
```

## CLI usage

### Help and model catalog

```powershell
uv run coding-agent --help
uv run coding-agent --list-models
uv run coding-agent --provider zhipu --list-models
```

### Non-interactive mode

Print only the final response:

```powershell
uv run coding-agent -p "Inspect this project for potential issues"
```

Stream newline-delimited JSON events:

```powershell
uv run coding-agent --mode json "Analyze packages/core"
```

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Model or request failure |
| `2` | Invalid arguments or missing input |
| `130` | User interruption |

### Session restoration

```powershell
# Continue the latest session for the current project
uv run coding-agent --continue

# Open a session file or partial UUID
uv run coding-agent --session <path-or-session-id>

# Run without saving the session
uv run coding-agent --no-session
```

### Provider and model selection

```powershell
uv run coding-agent --model deepseek-v4-pro
uv run coding-agent --provider zhipu --model glm-5.2
uv run coding-agent --model zhipu/glm-5.2:high
```

### Tool restrictions

```powershell
# Enable only selected tools
uv run coding-agent --tools read,grep,find

# Disable shell execution
uv run coding-agent --exclude-tools bash

# Disable all tools
uv run coding-agent --no-tools
```

### File and image attachments

Prefix a path with `@` to attach a text file or image to the initial message:

```powershell
uv run coding-agent @README.md "Summarize this file"

uv run coding-agent --provider zhipu --model glm-5v-turbo `
  @screenshot.png "Analyze the problem in this screenshot"
```

Images can only be sent to models whose catalog entries declare `image` input support.

## Built-in tools

| Tool | Purpose |
| --- | --- |
| `read` | Read text files or images |
| `write` | Create or overwrite files |
| `edit` | Modify files by matching exact text fragments |
| `grep` | Search file contents |
| `find` | Find files by name or pattern |
| `ls` | List directory contents |
| `bash` | Execute shell commands |

Tools expose their name, description, JSON Schema parameters, and asynchronous execution method through a common interface. The agent runtime validates arguments before execution and emits start, update, and end events. Embedding frontends can use before/after hooks for approval, auditing, or result transformation.

## Terminal commands

| Command | Description |
| --- | --- |
| `/help` | Show commands and keybindings |
| `/model` | Select a model from configured providers |
| `/login`, `/logout` | Manage provider credentials |
| `/new` | Start a new session |
| `/session` | Show session information and statistics |
| `/tree` | Inspect and switch session branches |
| `/compact` | Compact context manually |
| `/settings` | View or update persistent settings |
| `/export` | Export HTML or JSONL |
| `/copy` | Copy the latest assistant response |
| `/hotkeys` | Show keyboard shortcuts |
| `/quit` | Exit the application |

The terminal editor provides slash-command completion. Entering `/` in the desktop composer opens a filterable command palette.

## Sessions and configuration

The default data directory is `~/.coding-agent`. Override it with `CODING_AGENT_HOME`:

```text
~/.coding-agent/
├── auth.json        # Provider credentials
├── settings.json    # Model, thinking level, and retry settings
└── sessions/        # Per-project JSONL sessions
```

Session files are append-only. In addition to user and assistant messages, they record model changes, thinking levels, compaction entries, and branch pointers so the active context can be restored after a restart.

If a task is interrupted before a tool finishes, the next LLM request inserts an error `toolResult` for the missing result at the model boundary. This prevents providers from rejecting an incomplete `tool_calls` history without overwriting the original JSONL file.

## Architecture

```text
Coding-agent/
├── apps/
│   └── desktop/                 # Electron + React desktop app
│       └── src/
│           ├── main/            # Electron main process and sidecar lifecycle
│           ├── preload/         # Restricted IPC bridge
│           ├── renderer/        # React interface
│           └── shared/          # Shared TypeScript types
└── packages/
    ├── llm/                     # Providers, models, messages, and SSE adapters
    ├── core/                    # Agent loop, tools, sessions, and compaction
    ├── tui/                     # Terminal components and renderer
    └── app/                     # CLI, AgentSession, and application runtime
```

The main desktop request path is:

```text
React Renderer
    ↓ typed IPC
Electron Main / Preload
    ↓ versioned NDJSON RPC
Python DesktopRuntime
    ↓
AgentSession → Agent Loop → LLM Provider
    ↓                         ↓
Tool Runtime              SSE Events
    └──────────── events ─────┘
```

The core packages do not depend on a specific UI. The terminal and desktop applications share model adapters, the agent loop, tool execution, session restoration, and context compaction while implementing their own event presentation and interaction layers.

## Development and validation

### Python

```powershell
uv sync
uv run ruff check .
uv run pyright --project pyrightconfig.release.json
uv run pytest -q
uv build --all-packages
```

### Desktop

```powershell
cd apps/desktop
pnpm install
pnpm typecheck
pnpm build
```

## Security boundary

Coding Agent is not an operating-system sandbox. Once the model invokes a tool, it can read files, modify files, or execute commands with the permissions of the current user.

- Enable project context files only in trusted repositories.
- Use a container, virtual machine, or restricted account for unfamiliar projects.
- Never commit API keys to the repository.
- The desktop renderer does not hold API keys and cannot access the shell or filesystem directly.
- `bash`, `write`, and `edit` require user approval in the desktop application by default.

See [SECURITY.md](SECURITY.md) for more information.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting changes, and make sure the relevant tests, static checks, and type checks pass.
