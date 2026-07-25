# 更新日志

## 尚未发布

### 修复

- `write` 工具现在会自动创建缺失的父目录（递归），不再因父目录不存在而报错；并补充与 `edit` 共享的 per-realpath 互斥锁，避免并发写/编辑同一文件时互相覆盖。

### 新增

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
