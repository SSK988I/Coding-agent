# Coding Agent

**简体中文** · [English](README.md)

Coding Agent 是一个面向本地开发工作的编程 Agent。项目以 Python 实现 Agent Runtime，同时提供终端交互界面和基于 Electron + React 的桌面端 MVP。

它能够读取和修改项目文件、检索代码、执行 Shell 命令，并通过流式事件持续更新模型回复与工具执行状态。会话以 JSONL 保存，支持恢复、分支和上下文压缩。

> 项目目前处于早期开发阶段。终端版本功能较完整；桌面端已经接通核心运行时，安装包与部分高级功能仍在完善中。

## 主要功能

- **流式 Agent Loop**：处理模型输出、工具调用、工具结果和后续推理，支持运行中终止、转向消息与后续消息。
- **七个内置开发工具**：`read`、`write`、`edit`、`grep`、`find`、`ls` 和 `bash`。
- **可恢复会话**：使用 Append-only JSONL 保存消息和设置变更，通过 Entry Tree 记录活动分支。
- **上下文管理**：提供 Token 估算、摘要压缩和上下文溢出后的 Compact-and-Retry。
- **多模型 Provider**：当前内置 DeepSeek 和智谱 Z.AI Coding Plan（中国区）模型目录。
- **项目上下文**：支持发现 `AGENTS.md`、`CLAUDE.md`、Skills 和提示词模板。
- **终端界面**：支持 Markdown、流式内容、工具卡片、模型选择和按行差分渲染。
- **桌面端 MVP**：支持项目选择、会话列表、流式消息、工具审批、模型切换和斜杠命令面板。

## 界面形态

### 终端模式

终端版本是当前的主要入口，适合在项目目录中直接启动。它包含完整的交互命令、项目上下文发现、会话树和导出能力。

### 桌面模式

桌面端采用 Electron + React，Python `AgentSession` 作为独立 Sidecar 运行。Renderer 不直接访问 Shell、文件系统或 API Key，相关操作统一通过 Electron IPC 和版本化 NDJSON RPC 交给 Python Runtime。

当前桌面端支持：

- 打开本地项目和切换历史会话
- 流式显示正文与思考内容
- 展示工具调用状态和执行结果
- 对 `bash`、`write`、`edit` 请求执行确认
- 输入 `/` 打开命令面板
- `/help`、`/new`、`/model`、`/compact`、`/clear`、`/session`

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

```powershell
# 恢复当前项目最近的会话
uv run coding-agent --continue

# 使用会话文件或部分 UUID
uv run coding-agent --session <路径或会话ID>

# 不保存本次会话
uv run coding-agent --no-session
```

### 选择 Provider 和模型

```powershell
uv run coding-agent --model deepseek-v4-pro
uv run coding-agent --provider zhipu --model glm-5.2
uv run coding-agent --model zhipu/glm-5.2:high
```

### 限制工具

```powershell
# 只启用指定工具
uv run coding-agent --tools read,grep,find

# 禁用 Shell 工具
uv run coding-agent --exclude-tools bash

# 禁用全部工具
uv run coding-agent --no-tools
```

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

工具通过统一接口声明名称、描述、JSON Schema 参数和异步执行方法。Agent Runtime 会在执行前校验参数，并发出开始、更新和结束事件。嵌入式前端可以通过执行前后 Hook 加入审批、审计或结果处理逻辑。

## 终端交互命令

| 命令 | 说明 |
| --- | --- |
| `/help` | 查看命令和快捷键 |
| `/model` | 选择已配置 Provider 的模型 |
| `/login`、`/logout` | 管理 Provider 凭据 |
| `/new` | 创建新会话 |
| `/session` | 查看会话信息和统计数据 |
| `/tree` | 查看并切换会话分支 |
| `/compact` | 手动压缩上下文 |
| `/settings` | 查看或修改持久化设置 |
| `/export` | 导出 HTML 或 JSONL |
| `/copy` | 复制最近一条助手回复 |
| `/hotkeys` | 查看快捷键 |
| `/quit` | 退出程序 |

终端输入框支持斜杠命令补全。桌面端输入 `/` 会打开可筛选的命令面板。

## 会话与配置

默认数据目录为 `~/.coding-agent`，可以通过 `CODING_AGENT_HOME` 修改：

```text
~/.coding-agent/
├── auth.json        # Provider 凭据
├── settings.json    # 模型、思考级别和重试设置
└── sessions/        # 按项目保存的 JSONL 会话
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
