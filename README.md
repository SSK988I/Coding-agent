# Coding Agent

[简体中文](README.zh-CN.md) · **English**

Coding Agent is a programming agent for local development workflows. Its agent runtime is implemented in Python, with both a terminal interface and an Electron + React desktop MVP.

It can read and modify project files, search code, execute shell commands, and stream model responses and tool progress to the interface. Conversations are stored as JSONL and support restoration, branches, and context compaction.

> The project is in an early stage of development. The terminal application provides the broader feature set; the desktop runtime is connected and usable, while packaging and additional desktop features are still in progress.

## Features

- **Streaming agent loop**: processes model output, tool calls, tool results, and subsequent reasoning, with abort, steering, and follow-up support.
- **Built-in development and search tools**: `read`, `write`, `edit`, `grep`, `find`, `ls`, `bash`, plus optional `web_search`.
- **Restorable sessions**: stores messages and settings changes in append-only JSONL and tracks the active branch through an entry tree.
- **Context management**: includes token estimation, summary compaction, and compact-and-retry after context overflow.
- **File-backed long-term memory**: extracts stable preferences and confirmed project decisions, retrieves them by user and project, and persists YAML snapshots backed by append-only JSONL facts.
- **Multiple model providers**: currently includes catalogs for DeepSeek and Z.AI Coding Plan (China).
- **Project context**: discovers `AGENTS.md`, `CLAUDE.md`, skills, and prompt templates.
- **Terminal UI**: renders Markdown, streaming content, tool cards, model selection, and line-based differential updates.
- **Desktop MVP**: supports workspace selection, session history, streaming messages, tool approval, model switching, and a slash-command palette.
- **File and image input**: select, paste, or drop files into desktop chat. Text and code attachments include bounded excerpts; other documents remain available as file references. Vision-capable models also accept images.
- **Cross-client Plan Mode**: provides runtime-enforced observation tools, structured questions, immutable plan revisions, recovery states, and explicit execution confirmation shared by CLI and desktop.

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
- A right-hand workbench with independent side conversations, line diffs, interactive terminals, an embedded browser, and file previews; see the [workbench guide](docs/specs/workbench.md) for lifecycle and permission boundaries
- `/help`, `/new`, `/model`, `/compact`, `/clear`, `/session`, `/plan`, `/cancel-plan`, `/execute-plan`, and `/memory`

#### Files and images in chat

Select **添加文件**, paste files copied from your file manager, or drop them into the composer. Screenshots and copied browser images still paste as image previews. Files appear as removable cards and can be sent without text, together with images. Failed sends keep the attachments available for retry; switching sessions clears unsent attachments.

Each send accepts up to 8 attachments and 20 MiB combined; individual non-image files may be up to 10 MiB. File copies are kept in application storage for session recovery, outside the project directory. UTF-8 text and code include bounded excerpts; PDF, Word and other binary formats are references for suitable tools, not native document uploads to the model. See the [desktop attachment contract](docs/specs/desktop-attachments.md).

Images go directly to the selected model provider for understanding. The agent can read screenshots, describe scenes and identify subjects without a web search. It uses search when you request online verification or the task needs external evidence; attaching an image does not require a lookup. Uncertainty should reflect the available evidence, without routine "not searched" or "unverified" disclaimers. If a requested check cannot be completed, the agent should say so briefly.

Image-related searches use public visual keywords and cite returned sources. They are not reverse image search and do not establish the original image's source by themselves; the search tool does not upload the image. Read-only search remains available while drafting in Plan Mode, under the usual approval policy. The attachment area only shows previews and removal controls; search configuration stays in the existing global settings.

The current model must support image input. Images go to that model's provider using the existing backend credentials, not to a separate OCR service. They are also saved in the local session JSONL as Base64 data. Do not attach sensitive images unless you intend to share them with the provider.

Supported formats are PNG, JPEG, GIF, and WebP. Each send allows up to 4 images, 5 MiB per image and 12 MiB combined. The backend decodes the actual image and rejects corrupt files, mismatched formats, dimensions over 8192 pixels per side, or more than 16 million pixels. Animated images are limited to 64 frames and 16 million pixels across all frames. Split long screenshots into readable sections rather than shrinking their text.

History counts too: the active model context permits at most 24 images and 24 MiB of decoded image files. DeepSeek requests containing images must also fit within a 40 MiB complete JSON body. Compact the context or start a new session when a limit is reached. Switching to a text-only model does not silently discard historical images; requests are rejected until you choose a compatible model or a new conversation.

With DeepSeek V4.1 Flash, the agent can use `view_image` to inspect local files. In Default Mode, images outside the workspace or in sensitive directories require approval unless full access is enabled. Plan Mode only permits non-sensitive images inside the workspace, including when full access is enabled; image viewing does not authorize edits or shell commands. Other adapters do not expose this tool until they can preserve images in tool results.

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

In the desktop sidebar, hover a session to archive it. The archive button beside the sidebar heading opens archived sessions, where you can read or restore them. Archiving preserves history; restore a session to continue chatting. Automatic resume skips archived sessions. See [session archiving](docs/specs/session-archive.md).

```powershell
# Continue the latest session for the current project
uv run coding-agent --continue

# Open a session file or partial UUID
uv run coding-agent --session <path-or-session-id>

# Run without saving the session
uv run coding-agent --no-session
```

### Plan Mode

Enter Plan Mode interactively with `/plan`, `Shift+Tab`, or `Alt+M`. `Shift+Tab`/`Alt+M` leaves Plan Mode by cancelling; it never executes a plan. Thinking-level cycling is `Alt+T`, while `Ctrl+T` only toggles thinking-block visibility.

TUI tool approval uses `/permissions` to select a mode. `/permissions on` or
`Alt+P` enables full access and displays `bypass permissions on` in the footer;
`/permissions off` restores normal approval. `--dangerously-skip-permissions` or
`--permission-mode bypassPermissions` enables it for one launch. The setting does
not override Plan tool restrictions. See [access permissions](docs/specs/desktop-permissions.md).

For resumable non-interactive workflows:

```powershell
uv run coding-agent --agent-mode plan -p "Plan the requested change"
uv run coding-agent --session <session-id> --answer-plan-question <question-id> "answer"
uv run coding-agent --session <session-id> --execute-plan <revision>
uv run coding-agent --session <session-id> --handoff-plan [revision]
uv run coding-agent --session <session-id> --cancel-plan
```

Plan State is reduced from the active JSONL v4 branch and can be resumed by either the CLI or desktop client. In Default mode, `/plan` enters Plan directly and leaves the editor ready for the user's objective; while already in Plan, `/plan` opens the state-aware controls for drafting, pending questions, ready revisions, active execution, or recovery. Ready plans can be executed in place or handed to a clean child session for a second review; supplemental text returns the episode to drafting and never authorizes execution. `settled` means the Agent turn ended, not that its result was verified, and cancelling/stopping does not roll back effects already performed. See the [Plan Mode Specification](docs/specs/plan-mode.md) and [architecture decision](docs/adr/0001-centralize-collaboration-mode-policy.md).

### Provider and model selection

```powershell
uv run coding-agent --model deepseek-flash
uv run coding-agent --provider zhipu --model glm-5.2
uv run coding-agent --model zhipu/glm-5.2:high
```

DeepSeek defaults to V4.1 Flash (`deepseek-flash`), with text and image input. Only retired V4 Flash / Flash Vision ids resolve to Flash. V4 Pro (`deepseek-v4-pro`) remains a separate text-only model: explicit CLI requests, settings, and saved sessions keep Pro and are not silently switched to Flash. If a previous version already saved the selection as Flash, select Pro again manually. Cost estimates use each model's peak rates; off-peak rates are half as much. See the current [model catalog](https://api-docs.deepseek.com/api/list-models/), [change log](https://api-docs.deepseek.com/updates/) and [pricing](https://api-docs.deepseek.com/quick_start/pricing/).

### Tool restrictions

```powershell
# Enable only selected tools
uv run coding-agent --tools read,grep,find

# Disable shell execution
uv run coding-agent --exclude-tools bash

# Disable all tools
uv run coding-agent --no-tools
```

### Web search

Web search is enabled by default and automatically follows the selected model:

- DeepSeek V4 Pro uses the native server-side `web_search` tool through the Responses API and reuses `DEEPSEEK_API_KEY`; no separate search key is required.
- DeepSeek V4.1 Flash uses the standalone [DeepSeek Search API](https://api-docs.deepseek.com/guides/search/) through the function tool `web_search`, also with `DEEPSEEK_API_KEY`. It does not switch the chat model to Pro or send images/history to the search endpoint. A DeepSeek key is sufficient for both image understanding and keyword-based web lookup; no Zhipu Coding Plan credential is required. Flash still ignores the native Responses search tool, as described in the [Responses compatibility table](https://api-docs.deepseek.com/guides/responses_api/#tools).
- Other model providers keep the Zhipu Z.AI Coding Plan Web Search Prime Remote MCP backend. The existing `web_search_backend: zhipu-mcp` setting describes this non-DeepSeek fallback; it does not force DeepSeek sessions to use Zhipu. Hosts that inject their own search backend keep that backend.

The welcome card reports the effective state as `WEB on/off`. Neither DeepSeek path opens an MCP connection. Desktop search status checks DeepSeek credentials for Flash and Pro, including while search is disabled, and follows model switches.

```powershell
# Enable for this run only (overrides the persisted setting)
uv run coding-agent --web-search "Find the current stable Python release and cite official sources"

# Persist the setting for future sessions (enter this in the interactive UI)
/settings web_search_enabled true

# Explicitly disable for this run
uv run coding-agent --no-web-search
```

Queries are sent to the active search service and may consume DeepSeek API or Coding Plan MCP quota. The standalone DeepSeek Search API is billed per successful non-empty request; check the [current billing rules](https://api-docs.deepseek.com/guides/search/#billing). Its query limit is 128 Unicode characters, including appended domain/date operators; longer queries are rejected with a request to shorten them, not silently truncated. Domain filters use `site:` clauses; freshness uses UTC calendar-day `after:` boundaries, not an exact rolling-hour guarantee. Plan drafting permits public web search through either DeepSeek path or the configured backend, under the same approval policy as Default Mode. It does not unlock writes, shell commands, tests or builds, and bounded read-only subagents still have no network access. Search has no automatic HTTP retries. Never include API keys, cookies, private code, internal URLs, or personal data. Search snippets are untrusted external data, and final answers should retain the returned source URLs.

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
| `subagent_spawn` | Start a bounded read-only investigation or review with an independent context |
| `subagent_status`, `subagent_wait`, `subagent_cancel` | Inspect, wait for, or stop a task on the active session branch |
| `web_search` | Search the public web through DeepSeek Pro native Responses, the Flash Search API, or the configured Remote MCP fallback; enabled by default |
| `view_image` | Inspect local images with compatible DeepSeek adapters, subject to workspace and permission checks |

Tools expose their name, description, JSON Schema parameters, and asynchronous execution method through a common interface. The agent runtime validates arguments and approval before emitting the running event. Terminal results distinguish failure, cancellation, timeout, denial and unknown outcomes; a nonzero shell exit is a failure. Embedding frontends can use before/after hooks for approval, auditing, or result transformation.

## Terminal commands

| Command | Description |
| --- | --- |
| `/help` | Show commands and keybindings |
| `/model` | Select a model from configured providers |
| `/login`, `/logout` | Manage provider credentials |
| `/new` | Start a new session |
| `/plan`, `/cancel-plan`, `/execute-plan` | Open state-aware Plan controls, cancel, or explicitly execute |
| `/session` | Show session information and statistics |
| `/tree` | Inspect and switch session branches |
| `/compact` | Compact context manually |
| `/memory` | Inspect, toggle, forget, or clear long-term memory |
| `/settings` | View or update persistent settings |
| `/export` | Export HTML or JSONL |
| `/copy` | Copy the latest assistant response |
| `/hotkeys` | Show keyboard shortcuts |
| `/quit` | Exit the application |

The terminal editor provides slash-command completion. Entering `/` in the desktop composer opens a filterable command palette.

### Long-term memory

Long-term memory is enabled by default. After a successful outer task, extraction uses only user-authored evidence, structured Plan answers, or the exact Plan revision the user explicitly executed. Tool output, assistant-only conclusions, Plan drafts, and compaction summaries are excluded. Relevant records are injected transiently before the next task and are never appended to the session JSONL.

```text
/memory status
/memory list [--global|--project]
/memory conflicts
/memory forget <id-or-key> [--global|--project] --confirm
/memory clear --project --confirm
/memory clear --all --confirm
/memory on
/memory off
```

Memories are isolated by user and project. Forget and clear operations append tombstones so replaying old sessions cannot restore deleted values.

## Sessions and configuration

Both desktop and terminal clients show cache hit rate, current context usage/capacity, cumulative input/output tokens and estimated cost. Text print mode writes a usage summary to stderr; JSON output stays machine-readable. Missing cache data shows `—`, and estimated context values are marked. See [usage metrics](docs/specs/usage-metrics.md) for calculation and recovery behavior.

In the desktop footer, **用量分析** opens an on-demand analysis drawer with per-request token and cost charts, input/cache composition, and paginated request details for the active branch.

The default data directory is `~/.coding-agent`. Override it with `CODING_AGENT_HOME`:

```text
~/.coding-agent/
├── auth.json        # Provider credentials
├── settings.json    # Model, thinking, retry, memory, and web-search settings
├── sessions/        # Per-project JSONL sessions
└── memory/          # User/project YAML snapshots and JSONL fact logs
```

Session files are append-only. In addition to user and assistant messages, they record model changes, thinking levels, compaction entries, and branch pointers so the active context can be restored after a restart.

If a task is interrupted before a tool finishes, the next LLM request inserts an error `toolResult` for the missing result at the model boundary. This prevents providers from rejecting an incomplete `tool_calls` history without overwriting the original JSONL file.

## Architecture

### Long tasks and independent review

Both interactive clients support these commands:

```text
/compact
/compact implement the selected design and focus on verification
/subagents spawn inspect the cancellation path and cite file/line evidence
/subagents review check the plan for missing failure cases
/subagents show TASK_ID
/subagents cancel TASK_ID
```

Plain `/compact` keeps the existing recent-context behavior. A direction turns
it into a next-phase brief: the active model context is replaced, while the
same session's original JSONL history and Plan state are retained. It does not
start the next phase. Esc in the TUI or **停止压缩** on desktop cancels it.

Read-only subagents receive the supplied brief, not the parent conversation.
They can read/search files and use structured Git readers, but cannot write,
run shell commands, execute tests/builds, change Plan state or delegate again.
Use `/subagents` to inspect tasks. The ready Plan controls also offer a
read-only review without a session handoff or execution confirmation.

Task reports are advisory. A restored task without a live owner is shown as
unknown and is never restarted automatically. Limits, persistence and RPC
contracts are documented in [Long-task runtime](docs/specs/long-task-runtime.md).

## Package layout

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
pnpm test
pnpm typecheck
pnpm build
pnpm test:vision
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
