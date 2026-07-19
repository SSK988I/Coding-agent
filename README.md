# Coding Agent

[中文](#中文) | [English](#english)

## 中文

Coding Agent 是一个面向本地日常开发的 Python 终端编程助手。它提供流式对话、文件与 Shell 工具、会话恢复、上下文压缩、图片输入和可持久化设置。目前内置 DeepSeek 与智谱 Z.AI Coding Plan（中国区）支持。

### 环境要求

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Windows、Linux 或 macOS
- Windows 推荐安装 Git for Windows；程序会优先使用 Git Bash

### 在 Windows CMD 中启动

下载或克隆仓库后：

```bat
cd /d E:\code\Coding-agent
uv sync
set DEEPSEEK_API_KEY=你的_API_KEY
uv run coding-agent
```

使用智谱：

```bat
set ZAI_CODING_CN_API_KEY=你的_API_KEY
uv run coding-agent --provider zhipu
```

也可以启动后使用 `/login` 保存 API Key。凭据保存在 `~/.coding-agent/auth.json`，写入时采用原子替换并尽可能限制文件权限。

### 常用命令

```powershell
# 查看帮助和模型
uv run coding-agent --help
uv run coding-agent --list-models

# 直接提出问题
uv run coding-agent "解释这个项目的结构"

# 非交互输出 / JSONL 事件输出
uv run coding-agent -p "检查当前目录"
uv run coding-agent --mode json "检查当前目录"

# 恢复最近会话或指定会话
uv run coding-agent --continue
uv run coding-agent --session <文件路径或会话ID>

# 指定模型与思考级别
uv run coding-agent --model zhipu/glm-5.2:high
```

非交互模式使用稳定退出码：`0` 成功、`1` 模型或请求失败、`2` 参数/输入错误、`130` 用户中断。

### 文件与图片

在参数前添加 `@` 可附加文本文件或图片：

```powershell
uv run coding-agent --provider zhipu --model glm-5v-turbo `
  @screenshot.png "分析截图中的错误"
```

图片只允许发送给模型目录中声明支持 `image` 输入的模型；否则程序会在发起请求前报错。

### 交互命令

- `/help`：查看当前可用命令
- `/model`：切换已配置 Provider 的模型
- `/login`、`/logout`：管理 API Key
- `/new`：创建并切换到新的会话文件
- `/session`、`/tree`：查看会话信息与分支
- `/compact`：压缩上下文
- `/settings`：查看设置
- `/settings <键> <值>`：保存设置
- `/export`：导出 HTML 或 JSONL
- `/quit`：退出

### 设置和数据目录

默认目录为 `~/.coding-agent`，可通过 `CODING_AGENT_HOME` 修改。

```text
~/.coding-agent/
|-- auth.json       # API Key
|-- settings.json   # 默认模型、思考级别和重试设置
`-- sessions/       # JSONL 会话
```

可持久化设置包括：`default_provider`、`default_model`、`thinking_level`、`auto_retry`、`max_retries`、`retry_initial_delay`、`retry_max_delay`。当前内置主题只有 `dark`。

临时网络故障、429 和常见 5xx 会在尚未输出内容时自动指数退避重试；认证失败和无效请求不会重试。

### 项目结构

```text
packages/
|-- llm/     # Provider、模型、消息类型和流式协议
|-- core/    # Agent 循环、工具、会话和上下文压缩
|-- tui/     # 终端 UI 与渲染组件
`-- app/     # CLI、设置和交互模式
```

每个包使用标准 Python `src` 布局，`src/agent_llm` 等目录是实际可导入包，不应删除这一层。

### 开发验证

```powershell
uv sync
uv run ruff check .
uv run pyright --project pyrightconfig.release.json
uv run pytest -q
uv build --all-packages
```

### 安全边界

Coding Agent 不是沙箱。模型可以通过工具执行命令，并读取或修改当前用户有权限访问的文件。请只在可信项目中运行；处理陌生仓库时建议使用容器、虚拟机或受限账户。详细说明见 [SECURITY.md](SECURITY.md)。

## English

Coding Agent is a Python terminal coding assistant for everyday local development. It provides streaming chat, file and shell tools, resumable sessions, context compaction, image input, and persistent settings. The built-in providers are DeepSeek and Z.AI Coding Plan (China).

### Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Windows, Linux, or macOS
- Git for Windows is recommended; Coding Agent prefers Git Bash on Windows

### Start from Windows CMD

After downloading or cloning the repository:

```bat
cd /d E:\code\Coding-agent
uv sync
set DEEPSEEK_API_KEY=your_api_key
uv run coding-agent
```

For Z.AI:

```bat
set ZAI_CODING_CN_API_KEY=your_api_key
uv run coding-agent --provider zhipu
```

You can also run `/login` after startup. Credentials are stored in `~/.coding-agent/auth.json` using atomic writes and restrictive permissions where supported.

### Common commands

```powershell
uv run coding-agent --help
uv run coding-agent --list-models
uv run coding-agent "Explain this project"
uv run coding-agent -p "Inspect the current directory"
uv run coding-agent --mode json "Inspect the current directory"
uv run coding-agent --continue
uv run coding-agent --session <path-or-session-id>
uv run coding-agent --model zhipu/glm-5.2:high
```

Print mode uses stable exit codes: `0` success, `1` model/request failure, `2` invalid arguments or missing input, and `130` user interruption.

### Files and images

Prefix a path with `@` to attach a text file or image:

```powershell
uv run coding-agent --provider zhipu --model glm-5v-turbo `
  @screenshot.png "Analyze the error in this screenshot"
```

Images are accepted only by models whose catalog entry declares `image` input support.

### Interactive commands

- `/help`: show available commands
- `/model`: switch models across configured providers
- `/login`, `/logout`: manage API keys
- `/new`: create and attach a new session file
- `/session`, `/tree`: inspect the session and its branches
- `/compact`: compact context
- `/settings`: show persistent settings
- `/settings <key> <value>`: update a setting
- `/export`: export HTML or JSONL
- `/quit`: exit

### Configuration and data

Data is stored under `~/.coding-agent` by default. Set `CODING_AGENT_HOME` to override it.

```text
~/.coding-agent/
|-- auth.json
|-- settings.json
`-- sessions/
```

Persistent settings include the default provider/model, thinking level, and retry policy. The only bundled theme is currently `dark`. Transient network, 429, and common 5xx failures are retried with exponential backoff before any response content has been emitted.

### Repository layout

```text
packages/
|-- llm/     # Providers, models, message types, streaming protocol
|-- core/    # Agent loop, tools, sessions, context compaction
|-- tui/     # Terminal UI and rendering components
`-- app/     # CLI, settings, and interactive mode
```

Each distribution uses the standard Python `src` layout. Directories such as `src/agent_llm` are the importable packages and should remain.

### Development

```powershell
uv sync
uv run ruff check .
uv run pyright --project pyrightconfig.release.json
uv run pytest -q
uv build --all-packages
```

### Security boundary

Coding Agent is not a sandbox. The model can run commands and read or modify any file accessible to the current OS user. Use it only in trusted projects; use a container, VM, or restricted account for unfamiliar repositories. See [SECURITY.md](SECURITY.md).
