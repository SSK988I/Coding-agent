"""provider auth 解析。

``resolve_provider_auth`` 是唯一的入口,把一个 provider 的 auth 方式、已存储
凭证、环境变量、单次请求的 overrides 综合起来,产出 ``AuthResult``(未配置时
返回 None)。

解析优先级:
  1. overrides.env(若提供,叠加到 auth context 之上)
  2. 显式 override api key + provider.api_key -> resolve_api_key
  3. 已存储的凭证 -> resolve(oauth 走带双重检查锁的 refresh;api_key 走合并
     env 后的 resolve)
  4. 环境态路径:provider.api_key.resolve(..., None)(env 变量/文件都在
     provider 自己的 resolve 里处理)

关键不变量:已存储的凭证独占该 provider;只有没有任何存储时才会去看环境态。
refresh 失败、或凭证类型没有对应的 handler 时,绝不会静默回退到环境态。

这里抛出的 ``ModelsError`` 的 code 有:``"auth"``(存储/解析失败)和
``"oauth"``(refresh/推导失败)。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from agent_llm.auth.types import (
    ApiKeyCredential,
    AuthContext,
    AuthResult,
    Credential,
    CredentialStore,
    ModelsError,
    OAuthCredential,
    ProviderAuth,
)
from agent_llm.types import Model, ProviderEnv


# ─── overrides ────────────────────────────────────────────────────────

@dataclass
class AuthResolutionOverrides:
    """单次请求的 auth override。"""
    api_key: str | None = None
    env: ProviderEnv | None = None


# ─── context 叠加 ─────────────────────────────────────────────────────

class _OverlayAuthContext:
    """包装一个基础 AuthContext,让 provider 作用域的 env 覆盖进程 env。

    当调用方传入 ``overrides.env`` 时,这些值会优先于基础 context 的 env()
    返回(按 key 匹配)。
    """

    def __init__(self, base: AuthContext, overlay: ProviderEnv) -> None:
        self._base = base
        self._overlay = overlay

    async def env(self, name: str) -> str | None:
        if name in self._overlay:
            value = self._overlay[name]
            return value if value else None
        return await self._base.env(name)

    async def file_exists(self, path: str) -> bool:
        return await self._base.file_exists(path)


# ─── 主入口 ───────────────────────────────────────────────────────────

async def resolve_provider_auth(
    provider: Any,  # 含 ``id: str`` 和 ``auth: ProviderAuth``。
    model: Model,
    credentials: CredentialStore,
    auth_context: AuthContext,
    overrides: AuthResolutionOverrides | None = None,
) -> AuthResult | None:
    """解析 ``provider`` 上 ``model`` 的请求 auth。

    provider 未配置时返回 None。存储/refresh 失败时抛出 ``ModelsError``
    (code 为 ``"auth"`` / ``"oauth"``)。
    """
    overrides = overrides or AuthResolutionOverrides()
    provider_auth: ProviderAuth = provider.auth

    # 把 overrides.env 叠加到 context 上,让 provider 的 resolve() 能看到合并后的 env。
    ctx: AuthContext = auth_context
    if overrides.env:
        ctx = _OverlayAuthContext(auth_context, overrides.env)  # type: ignore[assignment]

    # 2. 显式 override api key 路径。
    if overrides.api_key is not None and provider_auth.api_key is not None:
        synthetic: ApiKeyCredential = {
            "type": "api_key",
            "key": overrides.api_key,
            "env": overrides.env or {},
        }
        return await _resolve_api_key(provider_auth.api_key, model, ctx, synthetic)

    # 3. 已存储的凭证路径。
    credential = await _read_credential(credentials, provider.id)
    if credential is not None:
        cred_type = credential.get("type")
        if cred_type == "oauth" and provider_auth.oauth is not None:
            return await _resolve_stored_oauth(
                provider_auth.oauth, provider.id, credential, credentials, model  # type: ignore[arg-type]
            )
        if cred_type == "api_key" and provider_auth.api_key is not None:
            # 把 overrides.env 合并到已存储凭证的 env 上。
            if overrides.env:
                merged_env = {**(credential.get("env") or {}), **overrides.env}
                credential = {**credential, "env": merged_env}  # type: ignore[dict-item]
            return await _resolve_api_key(
                provider_auth.api_key, model, ctx, credential  # type: ignore[arg-type]
            )
        # 凭证类型没有对应的 handler -> 视作未配置(而不是回退到环境态 None)。
        return None

    # 4. 环境态路径:没有存储,用空凭证去问 provider 的 resolve。
    if provider_auth.api_key is not None:
        return await _resolve_api_key(provider_auth.api_key, model, ctx, None)
    return None


# ─── 辅助函数 ─────────────────────────────────────────────────────────

async def _read_credential(
    credentials: CredentialStore, provider_id: str
) -> Credential | None:
    """带错误包装的读取。"""
    try:
        return await credentials.read(provider_id)
    except Exception as e:  # noqa: BLE001
        raise ModelsError("auth", f"读取 {provider_id} 的凭证失败", cause=e)


async def _resolve_api_key(
    api_key_auth: Any,  # ApiKeyAuth
    model: Model,
    ctx: AuthContext,
    credential: ApiKeyCredential | None,
) -> AuthResult | None:
    """委派给 provider 的 ApiKeyAuth.resolve。"""
    try:
        return await api_key_auth.resolve(model=model, ctx=ctx, credential=credential)
    except Exception as e:  # noqa: BLE001
        raise ModelsError("auth", f"{model.provider} 的 API key 解析失败", cause=e)


async def _resolve_stored_oauth(
    oauth_auth: Any,  # OAuthAuth
    provider_id: str,
    credential: OAuthCredential,
    credentials: CredentialStore,
    model: Model,
) -> AuthResult:
    """解析已存储的 OAuth 凭证,过期则 refresh。

    在 ``credentials.modify`` 下做双重检查锁:进入锁后再次确认过期和登出
    状态,避免多个并发进程都去 refresh。refresh 失败 -> ``ModelsError``
    code 为 ``"oauth"``;存储失败 -> code 为 ``"auth"``。
    """
    expires = credential.get("expires", 0)
    # 乐观检查。
    if time.time() * 1000 >= expires:
        try:
            async def _refresh(current: Credential | None) -> Credential | None:
                if current is None or current.get("type") != "oauth":
                    # 期间被登出了。
                    return current
                current_expires = current.get("expires", 0)  # type: ignore[union-attr]
                if time.time() * 1000 < current_expires:
                    # 另一个进程已经 refresh 过了,沿用它的结果。
                    return current
                return await oauth_auth.refresh(current)  # type: ignore[arg-type]

            refreshed = await credentials.modify(provider_id, _refresh)
            if refreshed is None or refreshed.get("type") != "oauth":
                raise ModelsError("oauth", f"{provider_id} 在 refresh 期间被登出")
            credential = refreshed  # type: ignore[assignment]
        except ModelsError:
            raise
        except Exception as e:  # noqa: BLE001
            if isinstance(e, ModelsError):
                raise
            raise ModelsError("oauth", f"{provider_id} 的 token refresh 失败", cause=e)

    # 推导请求 auth。
    try:
        auth = await oauth_auth.to_auth(credential)
        return {"auth": auth, "source": "OAuth"}
    except Exception as e:  # noqa: BLE001
        raise ModelsError("oauth", f"{provider_id} 的 OAuth auth 推导失败", cause=e)
