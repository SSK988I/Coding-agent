"""auth 辅助函数。

提供标准的"读环境变量拿 api-key"的 auth 工厂,以及一个懒加载的 OAuth 包装。
这些是 provider 工厂声明 auth 时的基础积木,用法形如
``create_provider(auth=ProviderAuth(api_key=env_api_key_auth(...)))``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from agent_llm.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthContext,
    AuthLoginCallbacks,
    AuthResult,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
)
from agent_llm.types import Model


@dataclass
class _EnvApiKeyAuth:
    """``env_api_key_auth`` 的返回值;实现了 ``ApiKeyAuth``。

    解析优先级:已存储的凭证 key(来源标记 "stored credential")> 第一个有值
    的环境变量(来源标记为变量名)> None(未配置)。
    """

    name: str
    env_vars: list[str]

    async def login(self, callbacks: AuthLoginCallbacks) -> ApiKeyCredential:
        result = await callbacks.prompt({"type": "secret", "message": f"请输入{self.name}"})
        key = (result or "").strip()
        return {"type": "api_key", "key": key}

    async def resolve(
        self,
        *,
        model: Model,
        ctx: AuthContext,
        credential: ApiKeyCredential | None = None,
    ) -> AuthResult | None:
        # 已存储的凭证优先。
        if credential and credential.get("key"):
            return {"auth": {"api_key": credential["key"]}, "source": "stored credential"}
        # 其次按顺序找第一个有值的环境变量。
        for env_var in self.env_vars:
            value = await ctx.env(env_var)
            if value:
                return {"auth": {"api_key": value}, "source": env_var}
        return None


def env_api_key_auth(name: str, env_vars: list[str]) -> ApiKeyAuth:
    """构造一个从环境变量读取 API key 的 ApiKeyAuth。

    解析方式非标准的 provider(provider env、环境态文件、IAM 等)应自行实现
    自己的 ApiKeyAuth。
    """
    return _EnvApiKeyAuth(name=name, env_vars=list(env_vars))  # type: ignore[return-value]


@dataclass
class _LazyOAuth:
    """``lazy_oauth`` 的返回值;实现了 ``OAuthAuth``。

    包装一个动态导入的 OAuthAuth,这样 provider 定义里就能声明支持 OAuth,
    而不必把实现也打进去。在第一次调用 login/refresh/to_auth 时才加载。
    """

    name: str
    _load: Callable[[], "Awaitable[OAuthAuth]"]
    _impl: OAuthAuth | None = None

    async def _get(self) -> OAuthAuth:
        if self._impl is None:
            self._impl = await self._load()
        return self._impl

    async def login(self, callbacks: AuthLoginCallbacks) -> OAuthCredential:
        impl = await self._get()
        return await impl.login(callbacks)

    async def refresh(self, credential: OAuthCredential) -> OAuthCredential:
        impl = await self._get()
        return await impl.refresh(credential)

    async def to_auth(self, credential: OAuthCredential) -> ModelAuth:
        impl = await self._get()
        return await impl.to_auth(credential)


def lazy_oauth(*, name: str, load: Callable[[], "Awaitable[OAuthAuth]"]) -> OAuthAuth:
    """包装一个动态加载的 OAuthAuth。"""
    return _LazyOAuth(name=name, _load=load)  # type: ignore[return-value]
