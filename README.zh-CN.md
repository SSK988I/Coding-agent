# Coding Agent

**简体中文** · [English](README.md)

Coding Agent 是一个面向本地开发工作的编程 Agent。项目以 Python 实现 Agent Runtime，同时提供终端交互界面和基于 Electron + React 的桌面端 MVP。

它能够读取和修改项目文件、检索代码、执行 Shell 命令，并通过流式事件持续更新模型回复与工具执行状态。会话以 JSONL 保存，支持恢复、分支和上下文压缩。

> 项目目前处于早期开发阶段。终端版本功能较完整；桌面端已经接通核心运行时，安装包与部分高级功能仍在完善中。

## 主要功能

- **流式 Agent Loop**：处理模型输出、工具调用、工具结果和后续推理，支持运行中终止、转向消息与后续消息。
- **内置开发与检索工具**：`read`、`write`、`edit`、`grep`、`find`、`ls`、`bash`，以及可选的 `web_search`。
- **可恢复会话**：使用 Append-only JSONL 保存消息和设置变更，通过 Entry Tree 记录活动分支。
- **上下文管理**：提供 Token 估算、摘要压缩和上下文溢出后的 Compact-and-Retry。
- **文件式长期记忆**：从成功任务中提取稳定偏好和已确认项目决策，按用户与项目检索，使用 YAML 快照与 Append-only JSONL 事实日志持久化。
- **多模型 Provider**：当前内置 DeepSeek 和智谱 Z.AI Coding Plan（中国区）模型目录。
- **项目上下文**：支持发现 `AGENTS.md`、`CLAUDE.md`、Skills 和提示词模板。
- **终端界面**：支持 Markdown、流式内容、工具卡片、模型选择和按行差分渲染。
- **桌面端 MVP**：支持项目选择、会话列表、流式消息、工具审批、模型切换和斜杠命令面板。
- **跨端 Plan Mode**：CLI 与桌面端共享 Runtime 强制的观察工具、结构化问题、不可变 revision、恢复状态和显式执行确认。

## 界面形态

### 终端模式

终端版本是当前的主要入口，适合在项目目录中直接启动。它包含完整的交互命令、项目上下文发现、会话树和导出能力。

### 桌面模式

桌面端采用 Electron + React，Python `AgentSession` 作为独立 Sidecar 运行。Renderer 不直接访问 Shell、文件系统或 API Key，相关操作统一通过 Electron IPC 和版本化 NDJSON RPC 交给 Python Runtime。

当前桌面端支持：

- 打开本地项目和切换历史会话
- 流式显示正文与思考内容
- “添加文件”、粘贴和拖入附件，保留图片预览；支持混合发送、失败恢复与历史文件卡片，详见[桌面附件](docs/specs/desktop-attachments.md)
- 展示工具调用状态和执行结果
- 对 `bash`、`write`、`edit` 请求执行确认
- 输入 `/` 打开命令面板
- 右上角“工具栏”：独立侧边对话、逐行变更、交互终端、内嵌浏览器和文件树预览；使用与权限边界见[桌面工作栏](docs/specs/workbench.md)
- `/help`、`/new`、`/model`、`/compact`、`/clear`、`/session`、`/plan`、`/cancel-plan`、`/execute-plan`、`/memory`

#### 聊天中的文件和图片

点击“添加文件”、粘贴文件管理器中复制的文件，或拖入输入框。截图和浏览器复制图片仍显示预览；附件可以单独或混合发送，失败时保留以供重试，切换会话会清空未发送附件。每轮最多 8 个附件、合计 20 MiB，普通文件每个最多 10 MiB。副本保存在项目之外的应用目录；UTF-8 文本和代码提供有界摘录，PDF、Word 等二进制文件仅作为文件引用，需由适当工具读取。详见[桌面附件契约](docs/specs/desktop-attachments.md)。

图片直接交给当前模型供应商识别，使用现有后端凭据，并以 Base64 保存在本地会话 JSONL。当前模型必须声明支持图片输入。识别图片不强制联网；用户要求核实时才按画面中的公开线索提取关键词搜索，不把原图上传给搜索引擎，也不把关键词结果当作已证明的图片来源。证据不足时说明不确定，不固定追加“未联网”提示；未完成用户要求的查询时简要说明原因。全局搜索开关仍由现有设置控制。

支持 PNG、JPEG、GIF 和 WebP，每轮最多 4 张、每张 5 MiB、合计 12 MiB。后端验证实际格式和尺寸：每边最多 8192 像素、总像素最多 1600 万；动画最多 64 帧，所有帧合计最多 1600 万像素。历史模型上下文最多 24 张图片和 24 MiB 图片文件，DeepSeek 含图片请求的完整 JSON 还需不超过 40 MiB。超限时先压缩或新建会话；切到纯文本模型不会静默删除历史图片，而会要求使用兼容模型或新会话。

DeepSeek V4.1 Flash 可通过 `view_image` 查看本地图片。Default 下，工作区外或敏感目录中的图片需要审批，完全访问模式除外；Plan 只允许工作区内非敏感图片，完全访问也不放宽此边界。其他适配器在能够保留工具结果中的图片前不会暴露此工具；查看图片不授予编辑或 Shell 权限。

## 环境要求

### Python Runtime

- Python 3.13 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Windows、Linux 或 macOS
- Windows 推荐安装 Git for Windows；程序会优先使用 Git Bash

### 桌面端开发

- Node.js
- pnpm
- 已通过 `uv sync` 创建的项目虚拟环境

## 快速开始

### 1. 克隆并安装依赖

```powershell
git clone https://github.com/SSK988I/Coding-agent.git
cd Coding-agent
uv sync
```

### 2. 配置模型凭据

DeepSeek：

```powershell
$env:DEEPSEEK_API_KEY = "你的 API Key"
```

智谱 Z.AI Coding Plan：

```powershell
$env:ZAI_CODING_CN_API_KEY = "你的 API Key"
```

也可以进入终端界面后执行 `/login`。保存的凭据位于 `~/.coding-agent/auth.json`。

### 3. 启动终端界面

```powershell
uv run coding-agent
```

带初始任务启动：

```powershell
uv run coding-agent "阅读当前项目并说明核心模块"
```

### 4. 启动桌面端

```powershell
cd apps/desktop
pnpm install
pnpm dev
```

开发模式默认使用仓库根目录下的 `.venv` 启动 Python Sidecar。需要指定其他 Python 时：

```powershell
$env:CODING_AGENT_PYTHON = "C:\path\to\python.exe"
pnpm dev
```

## CLI 使用

### 查看帮助和模型

```powershell
uv run coding-agent --help
uv run coding-agent --list-models
uv run coding-agent --provider zhipu --list-models
```

### 非交互模式

只输出最终回复：

```powershell
uv run coding-agent -p "检查当前项目中可能存在的问题"
```

输出逐行 JSON 事件：

```powershell
uv run coding-agent --mode json "分析 packages/core"
```

退出码约定：

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 模型调用或请求失败 |
| `2` | 参数错误或缺少输入 |
| `130` | 用户中断 |

### 会话恢复

桌面侧栏悬停会话可点击归档图标；标题旁的归档入口可查看和恢复已归档会话。归档保留聊天记录，恢复后可继续对话；自动恢复最近会话会跳过归档记录。详见[会话归档](docs/specs/session-archive.md)。

```powershell
# 恢复当前项目最近的会话
uv run coding-agent --continue

# 使用会话文件或部分 UUID
uv run coding-agent --session <路径或会话ID>

# 不保存本次会话
uv run coding-agent --no-session
```

### Plan Mode

交互模式可通过 `/plan`、`Shift+Tab` 或 `Alt+M` 进入 Plan Mode。再次按 `Shift+Tab`/`Alt+M` 只会取消规划，不会执行计划。思考级别循环迁移到 `Alt+T`；`Ctrl+T` 仍只展开或折叠 thinking block。

### 访问权限

桌面输入框提供“请求批准 / 帮我批准 / 完全访问权限”菜单；TUI 使用 `/permissions` 选择相同策略。`/permissions on` 或 `Alt+P` 开启完全访问，底栏显示 `bypass permissions on`；`/permissions off` 恢复审批，`/permissions auto` 使用自动审批。双端设置分别保存。

启动参数 `--dangerously-skip-permissions` 或 `--permission-mode bypassPermissions` 只对本次启动生效。完全访问仅跳过工具审批，不绕过 Plan 工具限制或操作系统权限。详见[访问权限规范](docs/specs/desktop-permissions.md)。

### Plan 会话控制

非交互模式使用可恢复控制参数：

```powershell
uv run coding-agent --agent-mode plan -p "规划这项改动"
uv run coding-agent --session <会话ID> --answer-plan-question <问题ID> "回答"
uv run coding-agent --session <会话ID> --execute-plan <revision>
uv run coding-agent --session <会话ID> --handoff-plan [revision]
uv run coding-agent --session <会话ID> --cancel-plan
```

Plan State 从活动 JSONL v4 分支归约，可由 CLI 或桌面端交叉恢复。裸 `/plan` 会按 drafting、待回答问题、ready、执行中或恢复异常重新显示对应控件。ready 方案既可在当前会话执行，也可交接到干净子会话再次复核；补充内容会回到 drafting，且绝不会构成执行授权。`settled` 仅表示 Agent 回合结束，不代表结果已经验证；取消或停止也不会回滚已经发生的副作用。完整约束见 [Plan Mode 规范](docs/specs/plan-mode.md)及[架构决策](docs/adr/0001-centralize-collaboration-mode-policy.md)。

### 选择 Provider 和模型

```powershell
uv run coding-agent --model deepseek-flash
uv run coding-agent --provider zhipu --model glm-5.2
uv run coding-agent --model zhipu/glm-5.2:high
```

DeepSeek 默认使用 V4.1 Flash（`deepseek-flash`），支持文本和图片输入。只有已下线的 V4 Flash / Flash Vision 名称会迁移到 Flash。V4 Pro（`deepseek-v4-pro`）仍是独立的纯文本模型；显式 CLI 请求、设置及保存会话中的 Pro 选择保持不变，不会静默切成 Flash。若旧版本已经把选择保存成 Flash，需手动重新选择 Pro。费用分别按各模型高峰价估算，闲时单价为一半。详见当前[模型目录](https://api-docs.deepseek.com/zh-cn/api/list-models/)、[更新日志](https://api-docs.deepseek.com/zh-cn/updates/)和[定价](https://api-docs.deepseek.com/quick_start/pricing/)。

### 限制工具

```powershell
# 只启用指定工具
uv run coding-agent --tools read,grep,find

# 禁用 Shell 工具
uv run coding-agent --exclude-tools bash

# 禁用全部工具
uv run coding-agent --no-tools
```

### 联网检索

联网检索默认开启，并按当前模型自动选择执行路径：

- DeepSeek V4 Pro：通过 Responses API 使用服务端原生 `web_search`，复用 `DEEPSEEK_API_KEY`。
- DeepSeek V4.1 Flash：通过函数工具 `web_search` 调用独立的 [DeepSeek Search API](https://api-docs.deepseek.com/guides/search/)，同样使用 `DEEPSEEK_API_KEY`，无需智谱 Coding Plan 凭据。不会将聊天模型切换为 Pro，也不会把图片或历史消息发送给搜索接口。Flash 的原生 Responses 搜索仍不生效，详见[兼容性表](https://api-docs.deepseek.com/guides/responses_api/#tools)。
- 其他模型：继续使用已保存的智谱 Z.AI Coding Plan 凭据连接 Web Search Prime Remote MCP。`web_search_backend: zhipu-mcp` 只描述非 DeepSeek 的回退后端，不会强制 DeepSeek 使用智谱；宿主显式注入的搜索后端保持不变。

欢迎卡片中的 `WEB on/off` 显示当前开关；两条 DeepSeek 路径均不建立 MCP 连接。桌面端根据当前模型检查 Flash 或 Pro 凭据，即使搜索已关闭也能显示配置状态，并随模型切换更新。

```powershell
# 仅为本次运行启用（可覆盖持久设置）
uv run coding-agent --web-search "搜索 Python 当前稳定版本，并附上官方来源"

# 持久启用；在交互界面中输入，后续会话生效
/settings web_search_enabled true

# 显式禁用本次运行
uv run coding-agent --no-web-search
```

搜索词会发送给当前搜索服务，并可能消耗 DeepSeek API 或 Coding Plan MCP 额度。独立 DeepSeek Search API 对成功且非空的请求计费，详见[计费规则](https://api-docs.deepseek.com/guides/search/#billing)。查询最多 128 个 Unicode 字符，包含附加的域名和日期条件；超限要求缩短，不静默截断。域名筛选使用 `site:`，时效筛选按 UTC 日历日生成 `after:`，不保证精确的滚动小时范围。Plan drafting 可在普通审批策略下使用只读公开搜索，不解锁写入、Shell、测试或构建；受限只读子代理仍不能联网。搜索不自动重试 HTTP 请求。不要在查询中放入 API Key、Cookie、私有代码、内部 URL 或个人数据。搜索摘要属于不可信外部数据，最终回答应保留来源 URL。

### 附加文件和图片

在路径前添加 `@`，可以把文本文件或图片附加到初始消息：

```powershell
uv run coding-agent @README.md "总结这个文件"

uv run coding-agent --provider zhipu --model glm-5v-turbo `
  @screenshot.png "分析截图中的问题"
```

图片只能发送给模型目录中声明支持 `image` 输入的模型。

## 内置工具

| 工具 | 用途 |
| --- | --- |
| `read` | 读取文本文件或图片 |
| `write` | 创建或覆盖文件 |
| `edit` | 按精确文本片段修改文件 |
| `grep` | 搜索文件内容 |
| `find` | 按名称或模式查找文件 |
| `ls` | 查看目录内容 |
| `bash` | 执行 Shell 命令 |
| `web_search` | 搜索公开网络；Pro 使用原生 Responses 搜索，Flash 使用独立 Search API，其他模型回退到配置的 Remote MCP；默认开启 |
| `view_image` | 兼容的 DeepSeek 适配器查看本地图片，遵循工作区和权限检查 |

工具通过统一接口声明名称、描述、JSON Schema 参数和异步执行方法。Agent Runtime 会在执行前校验参数，并发出开始、更新和结束事件。嵌入式前端可以通过执行前后 Hook 加入审批、审计或结果处理逻辑。

## 终端交互命令

| 命令 | 说明 |
| --- | --- |
| `/help` | 查看命令和快捷键 |
| `/model` | 选择已配置 Provider 的模型 |
| `/login`、`/logout` | 管理 Provider 凭据 |
| `/new` | 创建新会话 |
| `/plan`、`/cancel-plan`、`/execute-plan` | 打开状态化 Plan 控件、取消或显式执行 |
| `/session` | 查看会话信息和统计数据 |
| `/tree` | 查看并切换会话分支 |
| `/compact` | 手动压缩上下文 |
| `/memory` | 查看、开关、遗忘或清理长期记忆 |
| `/settings` | 查看或修改持久化设置 |
| `/export` | 导出 HTML 或 JSONL |
| `/copy` | 复制最近一条助手回复 |
| `/hotkeys` | 查看快捷键 |
| `/quit` | 退出程序 |

终端输入框支持斜杠命令补全。桌面端输入 `/` 会打开可筛选的命令面板。

### 长期记忆

长期记忆默认开启。每个成功的外层任务结束后，应用只使用本轮用户原文、结构化 Plan 回答或用户明确执行的精确 Plan revision 提取候选；工具输出、助手单方面结论、Plan 草稿和压缩摘要不能作为事实证据。新任务开始前，相关记录会以临时系统上下文注入，不写回会话 JSONL。

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

记忆按用户和项目隔离。项目级记录只在当前项目生效；同 key 的项目记录会临时覆盖全局记录。`forget` 和 `clear` 会写入墓碑，防止旧会话重放后恢复已删除内容。

## 会话与配置

桌面输入框下方和 TUI 底栏显示缓存命中率、当前上下文用量／容量、累计输入输出和费用估算；`/session` 可查看详情。非交互文本模式将用量摘要写入 stderr，不影响 stdout 或 JSON 输出。缺失数据使用 `—`，估算容量带“约”标识。计算口径见[用量指标](docs/specs/usage-metrics.md)。

桌面底栏点击“用量分析”可打开按需加载的分析抽屉，查看当前分支每次模型请求的 Token／费用趋势、输入缓存构成和分页请求明细。

默认数据目录为 `~/.coding-agent`，可以通过 `CODING_AGENT_HOME` 修改：

```text
~/.coding-agent/
├── auth.json        # Provider 凭据
├── settings.json    # 模型、思考级别、重试、记忆和联网检索设置
├── sessions/        # 按项目保存的 JSONL 会话
└── memory/          # 用户/项目隔离的 YAML 快照与 JSONL 事实日志
```

会话文件使用追加写入。除用户和助手消息外，还会记录模型切换、思考级别、压缩节点和分支指针等状态，因此可以在重启后恢复活动上下文。

当任务在工具执行完成前被终止时，下一次模型请求会在 LLM 边界补充缺失的错误 `toolResult`，避免残缺的 `tool_calls` 历史导致 Provider 拒绝请求。原始 JSONL 不会被覆盖。

## 架构

```text
Coding-agent/
├── apps/
│   └── desktop/                 # Electron + React 桌面端
│       └── src/
│           ├── main/            # Electron 主进程与 Sidecar 生命周期
│           ├── preload/         # 受限 IPC Bridge
│           ├── renderer/        # React 界面
│           └── shared/          # TypeScript 共享类型
└── packages/
    ├── llm/                     # Provider、模型、消息和 SSE 适配
    ├── core/                    # Agent Loop、工具、会话和压缩
    ├── tui/                     # 终端组件和渲染器
    └── app/                     # CLI、AgentSession 和应用层 Runtime
```

一次桌面端请求的主要数据流：

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

核心包不依赖具体界面。终端和桌面端共享模型适配、Agent Loop、工具系统、会话恢复与上下文压缩，只分别实现事件展示和用户交互。

## 开发与验证

### Python

```powershell
uv sync
uv run ruff check .
uv run pyright --project pyrightconfig.release.json
uv run pytest -q
uv build --all-packages
```

### 桌面端

```powershell
cd apps/desktop
pnpm install
pnpm typecheck
pnpm build
```

## 安全边界

Coding Agent 不是操作系统沙箱。模型调用工具后，可以在当前用户权限范围内读取文件、修改文件或执行命令。

- 仅在可信项目中启用项目上下文文件。
- 处理陌生仓库时，建议使用容器、虚拟机或受限账户。
- 不要把 API Key 写入仓库。
- 桌面端 Renderer 不持有 API Key，也不能直接调用 Shell 或文件系统。
- `bash`、`write`、`edit` 在桌面端默认需要用户确认。

更多说明参见 [SECURITY.md](SECURITY.md)。

## 参与开发

提交改动前，请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，并确保相关测试、静态检查和类型检查通过。
