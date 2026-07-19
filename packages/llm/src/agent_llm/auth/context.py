"""默认的 AuthContext。

提供生产环境使用的、基于真实环境/文件系统的 AuthContext。
``default_auth_context()`` 读取 ``os.environ`` 和本地文件系统;测试时可以
注入假的实现。
"""
from __future__ import annotations

import os
from pathlib import Path

from agent_llm.auth.types import AuthContext


class _DefaultAuthContext:
    """基于 os.environ 和本地文件系统的 AuthContext。

    ``env`` 返回 trim 后非空的字符串,否则 None。``file_exists`` 会把开头的
    ``~`` 展开到家目录。
    """

    async def env(self, name: str) -> str | None:
        value = os.environ.get(name)
        if value is None:
            return None
        value = value.strip()
        return value or None

    async def file_exists(self, path: str) -> bool:
        expanded = _expand_home(path)
        return Path(expanded).exists()


def _expand_home(path: str) -> str:
    if path.startswith("~"):
        return str(Path(path).expanduser())
    return path


def default_auth_context() -> AuthContext:
    """构造生产用的 AuthContext。"""
    return _DefaultAuthContext()  # type: ignore[return-value]
