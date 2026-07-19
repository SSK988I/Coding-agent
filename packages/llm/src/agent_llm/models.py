"""Provider / Models 三层分发。

分层结构:
  - ``Provider``:单个 provider 的运行时单元。持有 id/name/auth/models,
    并把 stream/stream_simple 分发到每个 model 的 ``.api`` 对应的 API 模块。
  - ``Models``:provider 的集合。解析 auth,把每个请求委派给拥有该 model
    的 provider。
  - ``ProviderStreams``:API 模块契约(stream + stream_simple),由
    ``src/api/*.py`` 实现。

错误契约:在每个边界,失败都会通过 ``lazy_stream`` 转换为 ``error`` 事件
(auth 失败、未知 provider、缺失 API 实现)。返回的 stream 的
``.result()`` 永远 resolve 到一个 AssistantMessage。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from agent_llm.auth.context import default_auth_context
from agent_llm.auth.credential_store import InMemoryCredentialStore
from agent_llm.auth.resolve import AuthResolutionOverrides, ModelsError, resolve_provider_auth
from agent_llm.auth.types import AuthContext, AuthResult, CredentialStore, ProviderAuth
from agent_llm.event_stream import AssistantMessageEventStream, lazy_stream
from agent_llm.types import (
    Api,
    AssistantMessage,
    Context,
    Model,
    ModelThinkingLevel,
    SimpleStreamOptions,
    StreamOptions,
    Usage,
)
from agent_llm.types import ProviderHeaders


# ─── Provider ─────────────────────────────────────────────────────────

@dataclass
class Provider:
    """单个 provider 的运行时单元。

    ``stream`` / ``stream_simple`` 会分发到该 model 的 ``.api`` 对应的
    API 模块。由 :func:`create_provider` 构造,通常不直接实例化。
    """

    id: str
    name: str
    base_url: str | None
    headers: ProviderHeaders | None
    auth: ProviderAuth
    get_models: Callable[[], "list[Model]"]
    refresh_models: "Callable[[], Any] | None"
    stream: Callable[..., AssistantMessageEventStream]
    stream_simple: Callable[..., AssistantMessageEventStream]


# ─── ProviderStreams 分发辅助 ──────────────────────────────────────────

def _no_api_error(provider_id: str, model: Model) -> AssistantMessageEventStream:
    """构造一个把"model.api 没有对应 API 实现"作为 error 事件抛出的 stream。"""

    async def _raise() -> Any:
        raise ModelsError(
            "stream", f'Provider {provider_id} 没有针对 "{model.api}" 的 API 实现'
        )

    return lazy_stream(model, _raise)


@dataclass
class _ProviderStreamsHolder:
    """持有 provider 对应的单个或映射型 API 模块。

    ``single`` 持有所有 model 共用的单一 API 模块;``by_api`` 按
    ``model.api`` 做映射分发。
    """

    single: Any = None  # ProviderStreams (object with stream/stream_simple)
    by_api: dict[str, Any] | None = None  # {api: ProviderStreams}

    def for_model(self, model: Model) -> Any | None:
        if self.single is not None:
            return self.single
        if self.by_api is not None:
            return self.by_api.get(model.api)
        return None


def create_provider(
    *,
    id: str,
    name: str | None = None,
    base_url: str | None = None,
    headers: ProviderHeaders | None = None,
    auth: ProviderAuth,
    models: list[Model],
    api: Any,  # ProviderStreams or dict[api, ProviderStreams]
    refresh_models: "Callable[[], Any] | None" = None,
) -> Provider:
    """从声明式选项构造一个 Provider。

    ``api`` 要么是单个 ProviderStreams(所有 model 都走它),要么是一个
    ``{api: ProviderStreams}`` 映射(按 ``model.api`` 分发)。找不到对应 api
    条目的 model 会触发 stream error。
    """
    # 区分 single 还是 byApi。
    holder: _ProviderStreamsHolder
    if hasattr(api, "stream") or hasattr(api, "stream_simple") or callable(getattr(api, "stream", None)):
        holder = _ProviderStreamsHolder(single=api)
    elif isinstance(api, dict):
        holder = _ProviderStreamsHolder(by_api=dict(api))
    else:
        # 当作带有模块级 stream/stream_simple 的类模块对象处理。
        holder = _ProviderStreamsHolder(single=api)

    # 可变的 model 列表 + 进行中 refresh 的去重。
    state: dict[str, Any] = {"models": list(models)}
    inflight: dict[str, asyncio.Task | None] = {"task": None}

    def _get_models() -> list[Model]:
        return list(state["models"])

    def _refresh_models() -> Any:
        if refresh_models is None:
            return asyncio.sleep(0)  # 静态 provider 的空操作
        if inflight["task"] is None or inflight["task"].done():
            async def _do_refresh() -> None:
                try:
                    fetched = await refresh_models()
                    state["models"] = list(fetched) if fetched else state["models"]
                finally:
                    inflight["task"] = None

            inflight["task"] = asyncio.ensure_future(_do_refresh())
        return inflight["task"]

    def _dispatch_stream(
        model: Model, context: Context, options: Any, use_simple: bool
    ) -> AssistantMessageEventStream:
        streams = holder.for_model(model)
        if streams is None:
            return _no_api_error(id, model)
        method = getattr(streams, "stream_simple" if use_simple else "stream", None)
        if method is None:
            # 模块级函数回退。
            fn = streams if not use_simple else None
            if fn is None:
                return _no_api_error(id, model)
            return fn(model, context, options)
        return method(model, context, options)

    def _stream(model: Model, context: Context, options: Any = None) -> AssistantMessageEventStream:
        return _dispatch_stream(model, context, options, use_simple=False)

    def _stream_simple(model: Model, context: Context, options: Any = None) -> AssistantMessageEventStream:
        return _dispatch_stream(model, context, options, use_simple=True)

    return Provider(
        id=id,
        name=name or id,
        base_url=base_url,
        headers=headers,
        auth=auth,
        get_models=_get_models,
        refresh_models=_refresh_models,
        stream=_stream,
        stream_simple=_stream_simple,
    )


def has_api(model: Model, api: Api) -> bool:
    """运行时收窄保护:model 是否使用 ``api``?"""
    return model.api == api


# ─── 费用计算 ──────────────────────────────────────────────────────────

def calculate_cost(model: Model, usage: Usage) -> Usage:
    """根据 model 定价计算 ``usage.cost``。

    Anthropic 对 1 小时 cache 写入按 2 倍基础 input 价格计费;此处照此处理。
    会就地修改并返回 ``usage``。
    """
    long_write = usage.cache_write_1h
    short_write = usage.cache_write - long_write
    c = model.cost
    usage.cost.input = (c.input / 1_000_000) * usage.input
    usage.cost.output = (c.output / 1_000_000) * usage.output
    usage.cost.cache_read = (c.cache_read / 1_000_000) * usage.cache_read
    usage.cost.cache_write = (c.cache_write * short_write + c.input * 2 * long_write) / 1_000_000
    usage.cost.total = (
        usage.cost.input + usage.cost.output + usage.cost.cache_read + usage.cost.cache_write
    )
    return usage


# ─── Thinking 级别 ─────────────────────────────────────────────────────

_EXTENDED_LEVELS: list[ModelThinkingLevel] = ["off", "minimal", "low", "medium", "high", "xhigh"]


def get_supported_thinking_levels(model: Model) -> list[ModelThinkingLevel]:
    """model 支持哪些 thinking 级别。"""
    if not model.reasoning:
        return ["off"]
    result: list[ModelThinkingLevel] = []
    tlm = model.thinking_level_map or {}
    for level in _EXTENDED_LEVELS:
        mapped = tlm.get(level)
        if mapped is None and level in tlm:
            continue  # 显式为 null -> 不支持
        if level == "xhigh" and mapped is None:
            continue  # xhigh 必须有显式映射
        result.append(level)
    return result


def clamp_thinking_level(model: Model, level: ModelThinkingLevel) -> ModelThinkingLevel:
    """把请求的级别收窄到 model 支持的级别。"""
    available = get_supported_thinking_levels(model)
    if level in available:
        return level
    idx = _EXTENDED_LEVELS.index(level) if level in _EXTENDED_LEVELS else -1
    if idx == -1:
        return available[0] if available else "off"
    # 先向上找,再向下找。
    for i in range(idx, len(_EXTENDED_LEVELS)):
        if _EXTENDED_LEVELS[i] in available:
            return _EXTENDED_LEVELS[i]
    for i in range(idx - 1, -1, -1):
        if _EXTENDED_LEVELS[i] in available:
            return _EXTENDED_LEVELS[i]
    return available[0] if available else "off"


def models_are_equal(a: Model | None, b: Model | None) -> bool:
    """按 (id, provider) 判等。"""
    if not a or not b:
        return False
    return a.id == b.id and a.provider == b.provider


# ─── Models 集合 ───────────────────────────────────────────────────────

class Models:
    """provider 的运行时集合,外加 auth 解析与 streaming。

    provider 持有 stream 行为;Models 负责解析 auth,并把每个请求委派给
    拥有该 model 的 provider。使用 :func:`create_models` 构造。
    """

    def __init__(
        self,
        *,
        credentials: CredentialStore | None = None,
        auth_context: AuthContext | None = None,
    ) -> None:
        self._providers: dict[str, Provider] = {}
        self._credentials = credentials or InMemoryCredentialStore()
        self._auth_context = auth_context or default_auth_context()

    # ── provider 管理 ────────────────────────────────────────────────────
    def set_provider(self, provider: Provider) -> None:
        self._providers[provider.id] = provider

    def delete_provider(self, id: str) -> None:
        self._providers.pop(id, None)

    def clear_providers(self) -> None:
        self._providers.clear()

    def get_providers(self) -> list[Provider]:
        return list(self._providers.values())

    def get_provider(self, id: str) -> Provider | None:
        return self._providers.get(id)

    # ── model 列举 ──────────────────────────────────────────────────────
    def get_models(self, provider: str | None = None) -> list[Model]:
        if provider is not None:
            entry = self._providers.get(provider)
            if not entry:
                return []
            try:
                return list(entry.get_models())
            except Exception:
                return []
        result: list[Model] = []
        for entry in self._providers.values():
            try:
                result.extend(entry.get_models())
            except Exception:
                continue
        return result

    def get_model(self, provider: str, id: str) -> Model | None:
        for m in self.get_models(provider):
            if m.id == id:
                return m
        return None

    async def refresh(self, provider: str | None = None) -> None:
        if provider is not None:
            entry = self._providers.get(provider)
            if entry and entry.refresh_models:
                try:
                    await entry.refresh_models()
                except ModelsError:
                    raise
                except Exception as e:  # noqa: BLE001
                    raise ModelsError(
                        "model_source", f"Model refresh failed for {provider}", cause=e
                    )
            return
        # 尽力刷新所有 provider。
        tasks = []
        for entry in self._providers.values():
            if entry.refresh_models:
                tasks.append(_safe_refresh(entry))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── auth ───────────────────────────────────────────────────────────
    async def get_auth(self, model: Model) -> AuthResult | None:
        provider = self._providers.get(model.provider)
        if not provider:
            return None
        return await resolve_provider_auth(provider, model, self._credentials, self._auth_context)

    # ── streaming ──────────────────────────────────────────────────────
    def stream(
        self, model: Model, context: Context, options: StreamOptions | None = None
    ) -> AssistantMessageEventStream:
        return lazy_stream(model, lambda: self._stream_inner(model, context, options))

    async def _stream_inner(
        self, model: Model, context: Context, options: StreamOptions | None
    ) -> Any:
        provider = self._require_provider(model)
        request_model, request_options = await self._apply_auth(model, options)
        return _provider_to_iterator(
            provider.stream(request_model, context, request_options)
        )

    async def complete(
        self, model: Model, context: Context, options: StreamOptions | None = None
    ) -> AssistantMessage:
        return await self.stream(model, context, options).result()

    def stream_simple(
        self, model: Model, context: Context, options: SimpleStreamOptions | None = None
    ) -> AssistantMessageEventStream:
        return lazy_stream(model, lambda: self._stream_simple_inner(model, context, options))

    async def _stream_simple_inner(
        self, model: Model, context: Context, options: SimpleStreamOptions | None
    ) -> Any:
        provider = self._require_provider(model)
        request_model, request_options = await self._apply_auth(model, options)
        return _provider_to_iterator(
            provider.stream_simple(request_model, context, request_options)
        )

    async def complete_simple(
        self, model: Model, context: Context, options: SimpleStreamOptions | None = None
    ) -> AssistantMessage:
        return await self.stream_simple(model, context, options).result()

    # ── auth 应用 ───────────────────────────────────────────────────────
    def _require_provider(self, model: Model) -> Provider:
        provider = self._providers.get(model.provider)
        if not provider:
            raise ModelsError("provider", f"未知的 provider:{model.provider}")
        return provider

    async def _apply_auth(
        self, model: Model, options: Any
    ) -> "tuple[Model, Any]":
        """解析 auth 并合并到 model/options 上。"""
        overrides = AuthResolutionOverrides(
            api_key=options.get("api_key") if options else None,
            env=options.get("env") if options else None,
        )
        resolution = await resolve_provider_auth(
            self._require_provider(model),
            model,
            self._credentials,
            self._auth_context,
            overrides,
        )
        if resolution is None:
            return model, options
        auth = resolution["auth"]

        # 把 auth 的 base_url 覆盖到 model 上。
        request_model = model
        if auth.get("base_url"):
            request_model = _clone_model_with_base_url(model, auth["base_url"])

        # 显式传入的请求选项按字段优先;headers/env 按 key 合并。
        request_options: dict = dict(options) if options else {}
        api_key = request_options.get("api_key") or auth.get("api_key")
        if api_key:
            request_options["api_key"] = api_key
        auth_headers = auth.get("headers")
        opt_headers = request_options.get("headers")
        if auth_headers or opt_headers:
            merged = {**(auth_headers or {}), **(opt_headers or {})}
            request_options["headers"] = merged
        resolution_env = resolution.get("env")
        opt_env = request_options.get("env")
        if resolution_env or opt_env:
            request_options["env"] = {**(resolution_env or {}), **(opt_env or {})}
        return request_model, request_options


# ─── 辅助函数 ───────────────────────────────────────────────────────────

async def _safe_refresh(entry: Provider) -> None:
    if entry.refresh_models:
        try:
            await entry.refresh_models()
        except Exception:
            pass


def _clone_model_with_base_url(model: Model, base_url: str) -> Model:
    """返回 model 的浅拷贝,只替换 base_url。"""
    import dataclasses
    return dataclasses.replace(model, base_url=base_url)


def _provider_to_iterator(stream: AssistantMessageEventStream) -> Any:
    """恒等透传:Provider 的 stream 本身就是一个 event 迭代器。

    lazy_stream 的 setup 返回一个 AsyncIterator[event];而
    AssistantMessageEventStream 本身就是 async-iterable 的,所以直接返回它。
    """
    return stream.__aiter__()


def create_models(
    *,
    credentials: CredentialStore | None = None,
    auth_context: AuthContext | None = None,
) -> Models:
    """构造一个 Models 集合。"""
    return Models(credentials=credentials, auth_context=auth_context)
