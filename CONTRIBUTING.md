# 参与开发

感谢你愿意改进 Coding Agent。Issue 可以使用中文或英文；代码、测试和公开 API
中的命名应保持清晰、一致。

## 提交 Issue

提交前请先搜索现有 Issue，确认问题尚未报告，并尽量在最新发布版本或最新的 `dev` 分支上复现。
一个 Issue 只讨论一个主题。

### Bug 报告

请使用 Bug 模板，并提供：

- 可复现的最小步骤；
- 预期行为和实际行为；
- Coding Agent 版本或提交 SHA、操作系统和 Python 版本；
- 相关组件，例如 CLI、TUI、Provider、工具、会话或设置；
- 已移除敏感信息的日志或截图。

不要在公开 Issue 中粘贴 API Key、访问令牌、完整凭据文件、私人会话内容或其他敏感数据。
安全漏洞请遵循 [SECURITY.md](SECURITY.md)，不要创建公开 Issue。

### 功能建议

请先说明要解决的用户问题，再描述建议方案和考虑过的替代方案。新增 Provider、改变持久化格式、
修改公开接口或引入大型依赖等影响较大的设计，应先通过 Issue 达成共识，再开始实现。

如果你准备处理某个 Issue，请先留言说明，避免重复工作。`good first issue` 和 `help wanted`
（如存在）表示维护者欢迎社区直接参与；其他较大的改动建议先等待维护者确认方向。

## 开发环境

项目使用 Python 3.13 和 [uv](https://docs.astral.sh/uv/)。Fork 并克隆仓库后，在仓库根目录执行：

```powershell
uv sync --locked
uv run coding-agent --help
```

请保持依赖单向流动：

```text
llm <- core
llm/core/tui <- app
```

- `llm`：Provider、模型和消息协议；
- `core`：Agent 循环、工具、会话和上下文压缩；
- `tui`：终端输入、渲染和界面组件；
- `app`：CLI、设置及各包的组合入口。

不要提交虚拟环境、缓存、生成的 `*.egg-info`、构建产物、凭据、会话文件或调试日志。

## 分支与提交

仓库采用 `main` + `dev` 双长期分支：

- `main`：稳定和发布分支，只接收从 `dev` 发起的发布 PR，以及经过确认的紧急修复；
- `dev`：日常集成分支，常规功能、修复、文档和测试 PR 均以此为目标。

常规贡献应先同步最新的 `dev`，再从 `dev` 创建短生命周期分支。建议使用以下命名：

- `fix/<简短描述>`：缺陷修复；
- `feat/<简短描述>`：功能开发；
- `docs/<简短描述>`：文档修改；
- `test/<简短描述>`：测试改进；
- `chore/<简短描述>`：维护工作。

紧急修复可以从 `main` 创建 `hotfix/<简短描述>`，合入 `main` 后必须把同一修复同步回 `dev`，
避免两个长期分支产生行为差异。

提交信息采用 Conventional Commits 风格：

```text
<类型>(<可选范围>): <简短说明>
```

常用类型为 `fix`、`feat`、`docs`、`test`、`refactor`、`perf`、`build`、`ci` 和 `chore`。
范围可使用 `llm`、`core`、`tui`、`app`、`cli`、`ci` 或 `docs`。

示例：

```text
fix(cli): emit UTF-8 help on Windows
test(tui): cover terminal resize polling
docs: document provider configuration
```

每个提交应保持可理解且与 PR 目标相关。不要把格式化整个仓库、无关重构或生成文件混入功能修复。

## 提交 Pull Request

1. 常规 PR 的 base 分支必须选择 `dev`，并在提交前同步最新的 `dev`。
2. `dev -> main` 的 PR 仅用于发布，由维护者创建；普通贡献不要直接以 `main` 为目标。
3. 保持 PR 单一目的且规模可审查；较大的改动拆成有明确依赖关系的 PR。
4. 使用清晰的标题，建议与提交信息采用相同格式。
5. 在 PR 正文中说明问题、解决方案、用户影响和测试计划。
6. 使用 `Closes #123`、`Fixes #123` 等关键字关联并关闭对应 Issue；没有 Issue 时说明原因。
7. 行为修复必须添加回归测试；TUI 或输出变化应提供截图、录屏或示例输出（适用时）。
8. 未完成或需要早期反馈时创建 Draft PR；准备好评审后再标记为 Ready for review。

提交 PR 前运行：

```powershell
uv sync --locked
uv run ruff check .
uv run pyright --project pyrightconfig.release.json
uv run pytest -q
uv build --all-packages
uv run python scripts/check_versions.py
uv run coding-agent --help
```

若某项检查不适用或无法在本机运行，请在 PR 的测试计划中明确说明。GitHub Actions 必须通过后才能合并。

## 评审与合并

- 维护者可能要求调整设计、补充测试、更新文档或缩小改动范围；
- 回应评审时请说明修改内容，未采纳的建议应解释原因；
- 不要在评审过程中加入无关改动；
- PR 的最终合并方式和合并时机由维护者决定；
- 不要直接向 `main` 或 `dev` 推送社区贡献，应通过 PR 完成评审和 CI 验证；
- 发布时由维护者审查并将 `dev` 合入 `main`，发布完成后继续以 `dev` 作为开发基线。
