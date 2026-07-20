## 变更摘要

<!-- 说明改了什么，以及为什么需要这项改动。 -->

## 关联 Issue

<!-- 使用 Closes #123 / Fixes #123；若没有关联 Issue，请说明原因。 -->

## 目标分支

- [ ] 常规贡献：目标为 `dev`。
- [ ] 发布 PR：`dev -> main`（仅维护者）。
- [ ] 紧急修复：目标为 `main`，并已说明如何同步回 `dev`。

## 实现说明

<!-- 描述关键设计、取舍、兼容性影响和未采用的替代方案。小改动可简写。 -->

## 测试计划

<!-- 勾选已执行的检查；不适用或未执行的项目请在下方解释。 -->

- [ ] `uv run ruff check .`
- [ ] `uv run pyright --project pyrightconfig.release.json`
- [ ] `uv run pytest -q`
- [ ] `uv build --all-packages`
- [ ] `uv run python scripts/check_versions.py`
- [ ] `uv run coding-agent --help`

补充验证：

<!-- 操作系统、手动步骤、测试结果、截图或录屏。 -->

## 提交前检查

- [ ] 我已选择正确的目标分支，并将当前分支同步到最新基线。
- [ ] PR 只包含与目标相关的改动。
- [ ] 新增或变更的行为已有测试覆盖，或已说明无法添加测试的原因。
- [ ] 面向用户的行为、配置或接口变化已更新相关文档和 `CHANGELOG.md`（适用时）。
- [ ] 未提交凭据、会话、日志、缓存、构建产物或其他敏感信息。
- [ ] 我已阅读并遵循 `CONTRIBUTING.md`。
