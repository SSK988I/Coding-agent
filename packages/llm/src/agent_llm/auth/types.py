"""auth 类型定义。

定义了分层的 auth 模型:
  - ``ModelAuth``:单次请求级的 auth(api key / headers / base url)。
  - ``Credential``:已存储的凭证(api_key 或 oauth),即 auth.json 的形状。
  - ``ProviderAuth``:一个 provider 的 auth 方式(至少要有 apiKey/oauth 之一)。
  - ``CredentialStore``:持久化接口(读/改/删)。
  - ``AuthContext``:注入到 resolve() 的环境/文件访问抽象。
  - ``AuthResult``:解析返回的结果(auth + 来源标签 + env)。
  - ``ModelsError``:解析/streaming 错误,带一个 code。

设计要点:``CredentialStore.modify`` 是唯一的写入路径 —— 每次修改都是一次
串行化的"读-改-写",以 provider id 为键。互斥粒度是 per provider id,只要
底层存储支持,还会跨进程互斥。App 注入持久化的 store;默认实现是
``InMemoryCredentialStore``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Protocol, TypedDict, Union, runtime_checkable

from agent_llm.types import ProviderEnv, ProviderHeaders, Model

# ─── 单次请求的 auth ──────────────────────────────────────────────────

class ModelAuth(TypedDict, total=False):
    """应用到单次请求的 auth。"""
    api_key: str
    headers: ProviderHeaders
    base_url: str


# ─── 已存储的凭证 ─────────────────────────────────────────────────────

class ApiKeyCredential(TypedDict, total=False):
    """api-key 凭证,即 auth.json 里的存储形状。"""
    type: Literal["api_key"]
    key: str | None
    env: ProviderEnv  # provider 作用域的 env/配置(例如 Cloudflare account id)


class OAuthCredential(TypedDict, total=False):
    """OAuth 凭证。"""
    type: Literal["oauth"]
    access: str
    refresh: str
    expires: float  # Unix 毫秒时间戳


#: 凭证类型的联合。靠 ``type`` 字段区分。
Credential = Union[ApiKeyCredential, OAuthCredential]


# ─── AuthContext ──────────────────────────────────────────────────────

@runtime_checkable
class AuthContext(Protocol):
    """注入到 auth resolve() 的环境/文件访问抽象。

    可注入以便测试和浏览器场景使用。``env`` 读取一个环境变量(未设置或空时
    返回 None);``file_exists`` 检查文件是否存在(支持开头的 ``~``)。
    """

    def env(self, name: str) -> Awaitable[str | None]: ...

    def file_exists(self, path: str) -> Awaitable[bool]: ...


# ─── AuthResult ───────────────────────────────────────────────────────

class AuthResult(TypedDict, total=False):
    """resolve_provider_auth 的返回值。"""
    auth: ModelAuth
    source: str  # 用于状态 UI 的来源标签,例如 "ANTHROPIC_API_KEY"、"OAuth"
    env: ProviderEnv


# ─── 交互类型 ─────────────────────────────────────────────────────────

AuthPromptType = Literal["text", "secret", "select", "manual_code"]


class AuthSelectOption(TypedDict, total=False):
    label: str
    value: str
    description: str


class AuthPrompt(TypedDict, total=False):
    """交互式登录时发出的一次提示。"""
    type: AuthPromptType
    message: str
    options: list[AuthSelectOption]  # 用于 "select"
    url: str  # 用于 "manual_code"


# 登录过程中会发出 auth 事件(oauth device code、进度等)。
# 这里只给出最小形状。
AuthEvent = TypedDict("AuthEvent", {"type": str, "total": int}, total=False)


@runtime_checkable
class AuthLoginCallbacks(Protocol):
    """provider 的 login() 与用户交互时用到的回调。

    ``prompt`` 收集输入;``notify`` 推送进度。
    """

    def prompt(self, prompt: AuthPrompt) -> Awaitable[str | None]: ...

    def notify(self, event: AuthEvent) -> None: ...


# ─── auth 方式 ────────────────────────────────────────────────────────

@runtime_checkable
class ApiKeyAuth(Protocol):
    """api-key auth 方式。

    覆盖已存储 key 和环境态(env vars、AWS profiles、ADC files)两种来源。
    仅环境态的 provider 可以省略 ``login``。provider 未配置时 ``resolve``
    返回 None。
    """

    name: str

    def login(self, callbacks: AuthLoginCallbacks) -> Awaitable[ApiKeyCredential]: ...

    def resolve(
        self,
        *,
        model: Model,
        ctx: AuthContext,
        credential: ApiKeyCredential | None,
    ) -> Awaitable[AuthResult | None]: ...


@runtime_checkable
class OAuthAuth(Protocol):
    """OAuth auth 方式。

    拆成 ``refresh``(网络调用,产出新凭证)和 ``to_auth``(无副作用地推导
    出请求 auth)两部分。这种拆分让 Models 可以自己持有带锁的 refresh 模式。
    """

    name: str

    def login(self, callbacks: AuthLoginCallbacks) -> Awaitable[OAuthCredential]: ...

    def refresh(self, credential: OAuthCredential) -> Awaitable[OAuthCredential]: ...

    def to_auth(self, credential: OAuthCredential) -> Awaitable[ModelAuth]: ...


@dataclass
class ProviderAuth:
    """一个 provider 的 auth 方式。

    至少要有 ``api_key`` / ``oauth`` 之一。即便是仅环境态/无 key 的 provider,
    也会提供 ``api_key``,其 resolve() 用来报告是否已配置。
    """
    api_key: ApiKeyAuth | None = None
    oauth: OAuthAuth | None = None


# ─── CredentialStore ─────────────────────────────────────────────────

#: 一个"读-改-写"函数:接收当前凭证(或 None),返回要写入的新凭证
#: (或 None 表示保持不变)。
CredentialModifier = Callable[[Credential | None], "Awaitable[Credential | None]"]


@runtime_checkable
class CredentialStore(Protocol):
    """凭证持久化接口。

    以 provider id 为键;每个 provider 对应一份凭证。``modify`` 是唯一的写入
    路径:每次修改都是一次串行化的"读-改-写"。modifier 返回 None 表示保持
    该条目不变。``read`` 在条目缺失时返回 None;各方法只在存储失败时 reject。
    """

    def read(self, provider_id: str) -> Awaitable[Credential | None]: ...

    def modify(
        self,
        provider_id: str,
        fn: CredentialModifier,
    ) -> Awaitable[Credential | None]: ...

    def delete(self, provider_id: str) -> Awaitable[None]: ...


# ─── ModelsError ─────────────────────────────────────────────────────

ModelsErrorCode = Literal[
    "model_source",
    "model_validation",
    "provider",
    "stream",
    "auth",
    "oauth",
]


class ModelsError(Exception):
    """model / auth 解析和 stream 分发抛出的错误。

    ``code`` 用来分类失败原因,方便上游处理(例如 ``"oauth"`` → 状态 UI 显示
    "需要重新登录")。
    """

    def __init__(self, code: ModelsErrorCode, message: str, *, cause: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.name = "ModelsError"
        self.__cause__ = cause
