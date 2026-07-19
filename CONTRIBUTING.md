# 参与开发

项目使用 Python 3.13 和 uv。在仓库根目录执行：

```powershell
uv sync
uv run ruff check .
uv run pyright --project pyrightconfig.release.json
uv run pytest -q
uv build --all-packages
```

请保持依赖单向流动：`llm` 独立，`core` 依赖 `llm`，`tui` 依赖共享的消息和界面类型，`app` 负责组合各个包。修复行为问题时应添加回归测试，尤其关注会话、工具执行、CLI 退出码和持久化数据格式。

不要提交虚拟环境、缓存、生成的 `*.egg-info`、构建产物、凭据、会话文件或调试日志。
