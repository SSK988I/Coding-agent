# 更新日志

## 尚未发布

### 修复

- 修复桌面端 `/compact` 执行期间界面无反馈、需要切换会话才能刷新的问题；现在会显示压缩中、完成、跳过、失败或取消状态，并在重新打开会话后恢复可展开的压缩摘要记录。
- `write` 工具现在会自动创建缺失的父目录（递归），不再因父目录不存在而报错；并补充与 `edit` 共享的 per-realpath 互斥锁，避免并发写/编辑同一文件时互相覆盖。
- 修复模型已经输出完整 Markdown 计划但桌面端没有执行/补充选择的问题：共享核心现在兼容双语章节标题，并对结构完整但缺少 `<proposed_plan>` 外壳或实现总标题的计划做受限规范化。

### 新增

- 新增 CLI 与桌面端共享的 Plan Mode：包含会话级状态机、结构化单问题交互、严格 `<proposed_plan>` 规范、不可变 revision/digest、显式执行确认和跨端恢复。
- 会话格式升级为 JSONL v4，记录 Collaboration Mode、Plan Question、Plan Revision 与 Plan Run；v1-v3 会话保持兼容，模式切换本身不会生成空会话文件。
- 工具新增 `effect` 元数据，Plan 权限策略先于桌面审批执行；`bash` 与 TUI `!`/`!!` 使用同一保守 Shell 分类器。
- CLI 新增 `--agent-mode`、`--answer-plan-question`、`--execute-plan`、`--cancel-plan`，TUI 新增 `/plan`、`/cancel-plan`、`/execute-plan`。
- 桌面端新增 Plan 模式选择、问题卡、计划 revision 卡与执行/取消 RPC，并引入 Vitest、Testing Library 和 jsdom。
- TUI `Shift+Tab` 改为 Default/Plan 模式切换，`Alt+M` 为备用键；思考级别循环迁移至 `Alt+T`，`Ctrl+T` 仍控制 thinking block 展开/隐藏。
- Plan Ready 现在会明确询问“执行方案”或“补充想法”；补充内容会回到 drafting，且不会被视为执行授权。

- 支持技能（Skills）发现与加载：自动扫描用户目录（`~/.coding-agent/skills/`）、项目目录（`.coding-agent/skills/`）以及 `--skill` 显式指定的路径，解析 `SKILL.md` frontmatter 并注入系统提示词的 `<available_skills>` 块。新增 `--no-skills` 禁用发现。
- 支持提示词模板（Prompt Templates）：在交互模式输入 `/name args` 可展开为模板内容，支持 `$1`/`$@`/`$ARGUMENTS`/`${N:-default}`/`${@:N:L}` 参数替换。模板从用户目录、项目目录及 `--prompt-template` 加载。新增 `--no-prompts` 禁用。
- 新增 `/skill:name` 命令显式调用技能：读取 `SKILL.md` 正文作为本轮指令发给模型。提示词模板与技能命令现已在 `/help` 与 `/` 自动补全中列出。
- 支持经过校验的持久化设置、原子写入和损坏文件恢复。
- 针对 Provider 临时故障提供应用层指数退避重试。
- 为支持图片输入的模型提供端到端图片附件能力。
- 增加 Windows 和 Linux CI，覆盖代码规范、类型检查、测试、构建和 CLI 冒烟测试。
- 增加安全边界与参与开发说明。

### 修复

- 恢复会话时会把消息历史和思考级别载入当前 Agent。
- `/new` 现在会创建并关联独立会话，不再继续写入旧文件。
- Print 和 JSON 模式在缺少输入、模型失败或中断时会返回非零退出码。
- 未知 Provider 会直接报错，不再静默回退到 DeepSeek。
- 支持解析文档中说明的 `provider/model:thinking` 简写。
- Bash 结果同时限制最大行数和最大字节数。
- 凭据采用原子写入；设置或凭据损坏时会生成可恢复备份。
