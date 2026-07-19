"""基于内存的 CredentialStore。

默认实现;App 自行注入持久化的 store。通过 asyncio 锁对每个 provider 串行化,
使 ``modify`` 成为真正的"读-改-写"。
"""
from __future__ import annotations

import asyncio
from typing import Dict

from agent_llm.auth.types import Credential, CredentialModifier


class InMemoryCredentialStore:
    """基于字典的 CredentialStore,带 per-provider 锁。

    ``modify`` 返回 ``next ?? current``(写入之后的凭证)。modifier 返回 None
    表示该条目保持不变。
    """

    def __init__(self) -> None:
        self._data: Dict[str, Credential] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock_for(self, provider_id: str) -> asyncio.Lock:
        return self._locks.setdefault(provider_id, asyncio.Lock())

    async def read(self, provider_id: str) -> Credential | None:
        return self._data.get(provider_id)

    async def modify(
        self,
        provider_id: str,
        fn: CredentialModifier,
    ) -> Credential | None:
        async with self._lock_for(provider_id):
            current = self._data.get(provider_id)
            next_cred = await fn(current)
            if next_cred is not None:
                self._data[provider_id] = next_cred
                return next_cred
            # modifier 返回 None:保持条目不变。
            return current

    async def delete(self, provider_id: str) -> None:
        async with self._lock_for(provider_id):
            self._data.pop(provider_id, None)
