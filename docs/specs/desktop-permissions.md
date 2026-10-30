# Desktop access permissions

The picker beside Default/Plan selects the desktop approval policy. The Python
runtime owns the selected value; the renderer applies a successful RPC response
or a runtime snapshot. The default is `ask`. The choice is saved as
`desktop_permission_mode` in user settings and restored when opening a workspace
or session. Changing it requires an idle run, compaction and subagent manager.

| Value | Label | Approval behavior |
| --- | --- | --- |
| `ask` | 请求批准 | Ordinary workspace edits and recognized observations proceed. External edits, sensitive files, web search, scripts and unrecognized operations require approval. |
| `auto` | 帮我批准 | Ordinary workspace edits, public web search and recognized local validation commands proceed. External or sensitive edits, destructive/ambiguous shell operations and custom tools require approval. |
| `full_access` | 完全访问权限 | Desktop approval prompts are skipped. Existing tool registration, Plan restrictions and operating-system permissions still apply. |

Sensitive edit targets include `.git`, `.ssh`, `.aws` and `.env*`. File paths are
resolved against the workspace, including existing symlinks, before deciding
whether an edit is external. This approval layer is not an OS sandbox. Shell
shortcuts in automatic mode retain the existing Project Trust boundary for local
executables and project scripts.

Application `web_search` calls use the ordinary tool approval hook. Provider
native search is authorized before enabling its capability: strict mode asks
once per task, and a rejection or timeout keeps search disabled for that task.
Stopping while approval is pending prevents the provider stream from starting.
Model API communication itself is unaffected. Plan drafting permits read-only
web search through either path under this same approval policy; entering Plan
does not turn search on or bypass approval. Waiting-for-answer, ready and
recovery states do not start model tool rounds. Memory extraction and bounded
read-only subagents cannot enable native search through this approval hook.

`permission.setMode {mode}` saves the choice before updating live state and
returns `{sessionId, permissionMode}`. It emits `permission.changed` with the same
payload. Workspace and session snapshots include `permissionMode`. Native search
uses `permission.networkRequested` / `permission.networkResolved`; decisions go
through the existing `approval.resolve` RPC. A failed save leaves the live policy
unchanged and the renderer reloads its session snapshot.

## TUI bypass permissions

Both interactive clients use `coding_agent.core.permissions` for approval decisions.
The TUI defaults to `ask` and restores `tui_permission_mode` from user settings.
`/permissions` opens a keyboard menu; `on` enables `full_access`, `off` selects
`ask`, and `auto` selects automatic approval. `Alt+P` toggles between ask and full
access. Menu/command choices are saved; startup flags override only the current
launch. Desktop and TUI defaults are stored separately.

The footer displays `bypass permissions on` in full access, with a compact
`bypass on` label on narrow terminals. Shift+Tab continues to switch Default/Plan.
Normal mode presents an allow/deny dialog for operations requiring approval.
Parallel approval requests are serialized, and stopping the run clears pending
dialogs and restores the editor. Native search decisions are valid for one task.

CLI accepts `--permission-mode ask|auto|full_access`, with `default` and
`bypassPermissions` aliases, or `--dangerously-skip-permissions`. Without an
explicit permission flag, print/JSON mode retains its existing behavior. Explicit
ask mode without an interactive host blocks operations requiring approval and
does not enable Provider native search. Full access still respects tool filters,
the Plan gate and operating-system permissions.
