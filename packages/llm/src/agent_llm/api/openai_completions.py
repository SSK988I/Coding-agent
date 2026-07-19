"""OpenAI 兼容 chat completions 的 stream 后端。

驱动官方 ``openai`` Python SDK(由它负责 SSE 解析)。实现
``ProviderStreams`` 契约:模块级的 ``stream`` 和 ``stream_simple`` 可调用对象。

涵盖的能力:
  - convert_messages:UserMessage/AssistantMessage/ToolResultMessage ->
    OpenAI chat messages,包含 tool_calls 以及 role:"tool" 的结果。
  - convert_tools:Tool[] -> [{type:"function", function:{...}}]。
  - build_params:max_tokens / temperature / tools / tool_choice / reasoning。
  - streaming loop:text_delta、reasoning_content、tool_calls delta
    (按 index 累积 partial_args,每个 delta 都重新解析一次)。
  - toolcall block:ensure_tool_call_block / finish_block,发
    toolcall_start / delta / end 事件。
  - usage 解析、stop_reason 映射。
  - stream_simple:按 model.compat.thinking_format 把 reasoning 级别映射到
    thinking 参数。

错误契约:失败统一编码为 ``error`` 事件,绝不跨 stream 边界抛出
(lazy_stream 包裹 setup;主体自带 try/except)。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from agent_llm.event_stream import AssistantMessageEventStream
from agent_llm.api.simple_options import build_base_options, adjust_max_tokens_for_thinking
from agent_llm.types import (
    AssistantMessage,
    Context,
    ImageContent,
    Model,
    SimpleStreamOptions,
    StreamOptions,
    StopReason,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    Usage,
    UsageCost,
)
from agent_llm.utils.json_parse import parse_streaming_json


# ─── compat 解析 ──────────────────────────────────────────────────────

def _resolve_compat(model: Model) -> dict:
    """读取 model 的 OpenAICompletionsCompat,带合理的默认值。

    当设置了 ``model.compat`` 时直接用;否则按标准 OpenAI 兼容端点的默认
    自动探测结果给出取值。
    """
    c = model.compat or {}
    return {
        "supports_store": c.get("supports_store", False),
        "supports_developer_role": c.get("supports_developer_role", True),
        "supports_reasoning_effort": c.get("supports_reasoning_effort", True),
        "supports_usage_in_streaming": c.get("supports_usage_in_streaming", True),
        "max_tokens_field": c.get("max_tokens_field", "max_tokens"),
        "requires_tool_result_name": c.get("requires_tool_result_name", False),
        "requires_assistant_after_tool_result": c.get("requires_assistant_after_tool_result", False),
        "requires_thinking_as_text": c.get("requires_thinking_as_text", False),
        "requires_reasoning_content_on_assistant_messages": c.get(
            "requires_reasoning_content_on_assistant_messages", False
        ),
        "thinking_format": c.get("thinking_format", "openai"),
        "supports_strict_mode": c.get("supports_strict_mode", True),
        # Zhipu GLM:为真时,在 tools 之外额外发送 tool_stream:true。
        # 默认 False,不影响非 GLM 的 model。
        "zai_tool_stream": c.get("zai_tool_stream", False),
    }


# ─── message 转换 ──────────────────────────────────────────────────────

def _content_to_text(content: Any) -> str:
    """把一条 message 的 content block 拍平成一个文本字符串。"""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif isinstance(block, ImageContent):
            parts.append("(image)")
    return "\n".join(parts)


def convert_messages(context: Context, compat: dict) -> list[dict]:
    """从 Context 构造 OpenAI 的 ``messages`` 数组。

    system / user 处理直接;assistant message 会回放 tool_calls;
    toolResult message 会变成按 ``tool_call_id`` 索引的 ``role:"tool"`` 条目。
    """
    msgs: list[dict] = []

    # System prompt.
    if context.system_prompt:
        role = "developer" if compat["supports_developer_role"] else "system"
        msgs.append({"role": role, "content": context.system_prompt})

    for m in context.messages:
        if m.role == "user":
            content = m.content
            if isinstance(content, str):
                msgs.append({"role": "user", "content": content})
            else:
                # 文本/图片混合:OpenAI 要求 content 是一个 part 列表。
                parts = []
                for block in content:
                    if isinstance(block, TextContent):
                        parts.append({"type": "text", "text": block.text})
                    elif isinstance(block, ImageContent):
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{block.mime_type};base64,{block.data}"},
                        })
                msgs.append({"role": "user", "content": parts})

        elif m.role == "assistant":
            text = _content_to_text(m.content)
            tool_calls = [b for b in m.content if isinstance(b, ToolCall)]
            assistant_msg: dict = {"role": "assistant"}
            if text:
                assistant_msg["content"] = text
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in tool_calls
                ]
            # DeepSeek 要求回放的 assistant message 带上空的 reasoning_content。
            if compat["requires_reasoning_content_on_assistant_messages"]:
                assistant_msg["reasoning_content"] = ""
            # 跳过空的 assistant message(被中止的轮次)。
            if "content" in assistant_msg or "tool_calls" in assistant_msg:
                msgs.append(assistant_msg)

        elif m.role == "toolResult":
            text = _content_to_text(m.content)
            tool_msg: dict = {
                "role": "tool",
                "content": text or "(no output)",
                "tool_call_id": m.tool_call_id,
            }
            if compat["requires_tool_result_name"] and m.tool_name:
                tool_msg["name"] = m.tool_name
            msgs.append(tool_msg)

    return msgs


def _has_tool_history(context: Context) -> bool:
    """会话里是否已经存在 tool 调用/结果。"""
    for m in context.messages:
        if m.role == "toolResult":
            return True
        if m.role == "assistant" and any(isinstance(b, ToolCall) for b in m.content):
            return True
    return False


# ─── tool 转换 ──────────────────────────────────────────────────────────

def convert_tools(tools: list[Tool], compat: dict) -> list[dict]:
    """Tool[] -> OpenAI tools 数组。"""
    result = []
    for tool in tools:
        entry: dict = {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        if compat["supports_strict_mode"]:
            entry["function"]["strict"] = False
        result.append(entry)
    return result


# ─── usage / stop_reason ────────────────────────────────────────────────

def _calculate_cost(model: Model, usage: Usage) -> UsageCost:
    c = model.cost
    ci = c.input / 1_000_000 * usage.input
    co = c.output / 1_000_000 * usage.output
    cr = c.cache_read / 1_000_000 * usage.cache_read
    cw = c.cache_write / 1_000_000 * usage.cache_write
    return UsageCost(input=ci, output=co, cache_read=cr, cache_write=cw, total=ci + co + cr + cw)


def parse_chunk_usage(raw: Any, model: Model) -> Usage:
    """解析 OpenAI 的 usage chunk。

    兼容标准的 ``cached_tokens`` 和 DeepSeek 非标准的
    ``prompt_cache_hit_tokens``。
    """
    prompt_tokens = getattr(raw, "prompt_tokens", 0) or 0
    completion_tokens = getattr(raw, "completion_tokens", 0) or 0

    cache_read = 0
    ptd = getattr(raw, "prompt_tokens_details", None)
    if ptd is not None:
        cache_read = getattr(ptd, "cached_tokens", 0) or 0
    if not cache_read:
        cache_read = getattr(raw, "prompt_cache_hit_tokens", 0) or 0

    input_tokens = max(0, prompt_tokens - cache_read)

    reasoning_tokens = 0
    ctd = getattr(raw, "completion_tokens_details", None)
    if ctd is not None:
        reasoning_tokens = getattr(ctd, "reasoning_tokens", 0) or 0

    total = input_tokens + completion_tokens + cache_read
    usage = Usage(
        input=input_tokens,
        output=completion_tokens,
        cache_read=cache_read,
        cache_write=0,
        reasoning=reasoning_tokens,
        total_tokens=total,
    )
    usage.cost = _calculate_cost(model, usage)
    return usage


def map_stop_reason(reason: str | None) -> StopReason:
    """把 OpenAI 的 finish_reason 映射到本项目的 StopReason。"""
    if reason is None:
        return "stop"
    return {
        "stop": "stop",
        "end": "stop",
        "length": "length",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "content_filter": "error",
        "network_error": "error",
    }.get(reason, "error")


# ─── 参数构造 ──────────────────────────────────────────────────────────

def build_params(
    model: Model, context: Context, options: StreamOptions | None, compat: dict
) -> dict[str, Any]:
    """构造 OpenAI client 的 ``create`` 关键字参数。"""
    opts = options or {}
    params: dict[str, Any] = {
        "model": model.id,
        "messages": convert_messages(context, compat),
        "stream": True,
    }
    if compat["supports_usage_in_streaming"]:
        params["stream_options"] = {"include_usage": True}

    # max_tokens。调用方未设置时,默认用 model 自己的上限 —— 否则很多 provider
    # 会套一个小默认值(比如 4096),在 model 还没答完就截断。DeepSeek 用的是
    # 旧字段名。
    max_tokens = opts.get("max_tokens")
    if max_tokens is None and model.max_tokens:
        max_tokens = model.max_tokens
    if max_tokens is not None:
        field = compat["max_tokens_field"]
        params[field] = max_tokens

    # temperature(reasoning model 比如 deepseek-reasoner 会拒绝它)。
    temperature = opts.get("temperature")
    if temperature is not None and not model.reasoning:
        params["temperature"] = temperature

    # tools。
    if context.tools:
        params["tools"] = convert_tools(context.tools, compat)
        # Zhipu GLM(除 glm-4.5-air 外的所有 model)要求 tool_stream:true
        # 才会以流式吐出 tool-call 参数 delta,否则会缓冲整段。注意:和
        # `thinking` / `reasoning_effort` 一样,Python SDK 会把 `tool_stream`
        # 当作未知顶层 kwarg 在 create() 里拒绝,所以必须走 extra_body —— SDK
        # 会原样转发给 API。
        if compat.get("zai_tool_stream"):
            params.setdefault("extra_body", {})["tool_stream"] = True
    elif _has_tool_history(context):
        params["tools"] = []  # 历史中已有 tool 调用时,某些代理要求显式给空数组

    # Thinking / reasoning 注入。对支持 reasoning 的 model,开启 thinking 并
    # 映射 effort 级别。这段逻辑放在 build_params 里(而不只在 stream_simple),
    # 让 model 永远把 reasoning_content 和 content 分开 —— 否则 model 的内心
    # 独白会泄漏到正文里。DeepSeek 在开启时会自动 thinking。
    _inject_thinking_params(params, model, opts, compat)

    return params


def _inject_thinking_params(
    params: dict, model: Model, opts: dict, compat: dict
) -> None:
    """注入 provider 专属的 thinking / reasoning 参数。

    核心效果:对 DeepSeek,设置 ``thinking: {type: "enabled"}`` 会让 model
    把推理放进单独的 ``reasoning_content`` 字段(路由到 thinking block),
    而不是混进 ``content``(正文)里。
    """
    if not model.reasoning:
        return

    fmt = compat.get("thinking_format", "openai")
    reasoning = opts.get("reasoning")  # ThinkingLevel | None

    if fmt == "deepseek":
        # 注意：Python SDK 会拒绝 create() 中未知的顶层 kwargs，
        # 所以 thinking 和
        # reasoning_effort must go through extra_body, which the SDK forwards
        # verbatim to the API. DeepSeek then routes reasoning into a separate
        # reasoning_content 字段,而不是混进 content。
        extra = params.setdefault("extra_body", {})
        level_map = model.thinking_level_map or {}
        if reasoning:
            extra["thinking"] = {"type": "enabled"}
            if compat.get("supports_reasoning_effort"):
                mapped = level_map.get(reasoning)
                if mapped is not None:
                    extra["reasoning_effort"] = mapped
        elif level_map.get("off") is None:
            # DeepSeek 是默认就会 thinking 的 reasoning model。要真的关掉
            # thinking,必须显式发送 {type:"disabled"}。当 `off` 缺失时,
            # 视作"关闭它"。
            extra["thinking"] = {"type": "disabled"}
    elif fmt == "zai":
        # Zhipu GLM。和 DeepSeek 一样走 extra_body(Python SDK 拒绝未知顶层
        # kwargs)。关键区别:``clear_thinking: false`` 会在响应里保留思维链,
        # 这样我们才能把它渲染成 thinking block。只有 glm-5.2 支持
        # reasoning_effort 调节;其他 GLM model 虽支持 reasoning,但只能开/关。
        extra = params.setdefault("extra_body", {})
        if reasoning:
            extra["thinking"] = {"type": "enabled", "clear_thinking": False}
            if compat.get("supports_reasoning_effort"):
                mapped = (model.thinking_level_map or {}).get(reasoning)
                if mapped is not None:
                    extra["reasoning_effort"] = mapped
        else:
            extra["thinking"] = {"type": "disabled"}
    elif fmt == "openai":
        # OpenAI 风格的 reasoning_effort(o 系列 / 兼容端点)。
        if reasoning and compat.get("supports_reasoning_effort"):
            from agent_llm.api.simple_options import clamp_reasoning
            params["extra_body"] = {"reasoning_effort": clamp_reasoning(reasoning)}


# ─── streaming 状态 ────────────────────────────────────────────────────

class _StreamState:
    """单次 streaming 响应的累积器。

    内部维护 ``output`` AssistantMessage + ``blocks`` 数组 + 按 tool-call 的
    scratch 映射。block 按出现顺序追加,这样最终的 content 数组才能正确交错
    排列 text / thinking / toolcalls。
    """

    def __init__(self, model: Model) -> None:
        self.model = model
        self.output = AssistantMessage(
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=Usage(),
            stop_reason="stop",
        )
        self.blocks: list[Any] = []  # TextContent | ThinkingContent | ToolCall
        self._text_index: dict[int, TextContent] = {}
        self._thinking_index: dict[int, ThinkingContent] = {}
        self._tool_by_index: dict[int, dict] = {}  # scratch tool-call blocks
        self.stream: AssistantMessageEventStream = AssistantMessageEventStream()
        self._started = False

    def _content_index(self, block: Any) -> int:
        return self.blocks.index(block)

    def emit_start(self) -> None:
        if not self._started:
            self._started = True
            self.stream.push({"type": "start", "partial": self.output})

    def get_or_create_text(self, idx: int) -> TextContent:
        block = self._text_index.get(idx)
        if block is None:
            block = TextContent(text="")
            self._text_index[idx] = block
            self.blocks.append(block)
            # 把 block 同步镜像到 output.content,这样下游消费者(agent loop →
            # UI)看到的 partial AssistantMessage 在每个 delta 上都能反映已
            # 累积的文本。不做这步的话,output.content 会一直保持为 [],直到
            # finish_blocks() 才被填满 —— 每个 message_update 都带空 content
            # 数组,UI 直到最终的 done 事件才有内容可画,看起来就是"一次性
            # 输出完"。
            self.output.content.append(block)
            self.emit_start()
            self.stream.push({
                "type": "text_start", "content_index": self._content_index(block),
                "partial": self.output,
            })
        return block

    def get_or_create_thinking(self, idx: int) -> ThinkingContent:
        block = self._thinking_index.get(idx)
        if block is None:
            block = ThinkingContent(thinking="")
            self._thinking_index[idx] = block
            self.blocks.append(block)
            # 见 get_or_create_text:镜像到 output.content,让 partial 实时可用。
            self.output.content.append(block)
            self.emit_start()
            self.stream.push({
                "type": "thinking_start", "content_index": self._content_index(block),
                "partial": self.output,
            })
        return block

    def ensure_tool_call(self, idx: int | None, call_id: str | None) -> dict:
        """获取或创建 idx/call_id 对应的 scratch tool-call block。"""
        block = None
        if idx is not None:
            block = self._tool_by_index.get(idx)
        if block is None and call_id:
            for b in self._tool_by_index.values():
                if b.get("_id") == call_id:
                    block = b
                    break
        if block is None:
            block = {"type": "toolCall", "id": "", "name": "", "arguments": {},
                     "_partial_args": "", "_id": call_id or "", "_stream_index": idx}
            if idx is not None:
                self._tool_by_index[idx] = block
            self.blocks.append(block)
            self.emit_start()
            # 把一个占位 ToolCall 镜像到 output.content,让 partial 能反映一个
            # 进行中的 tool call(下面的每个 delta 都会重新解析它的 arguments)。
            # 见 get_or_create_text:为何 output.content 在 streaming 期间必须实时。
            tc = ToolCall(id="", name="", arguments={})
            block["_content_ref"] = tc
            self.output.content.append(tc)
            self.stream.push({
                "type": "toolcall_start", "content_index": len(self.blocks) - 1,
                "partial": self.output,
            })
        if idx is not None and block.get("_stream_index") is None:
            block["_stream_index"] = idx
            self._tool_by_index[idx] = block
        if call_id:
            block["_id"] = call_id
        return block

    def finish_blocks(self) -> None:
        """在 stream 结束后收尾所有 block。

        text / thinking block 早已实时镜像到 output.content(让 partial
        AssistantMessage 在每个 delta 上反映累积的文本);这里只需把 scratch
        字典形式的 tool-call block 换成最终的 ToolCall 形态,再对累积下来的
        arguments 做最后一次解析,并发 ``toolcall_end``。顺序通过按 self.blocks
        重建来保证。
        """
        final_content = []
        for block in self.blocks:
            if isinstance(block, (TextContent, ThinkingContent)):
                final_content.append(block)
            elif isinstance(block, dict) and block.get("type") == "toolCall":
                # 对累积的 arguments 做最后一次解析。
                block["arguments"] = parse_streaming_json(block.get("_partial_args", ""))
                tc = ToolCall(id=block["id"], name=block["name"], arguments=block["arguments"])
                final_content.append(tc)
                self.stream.push({
                    "type": "toolcall_end", "content_index": self.blocks.index(block),
                    "tool_call": tc, "partial": self.output,
                })
        self.output.content = final_content


# ─── 主 stream 函数 ────────────────────────────────────────────────────

def stream(
    model: Model,
    context: Context,
    options: StreamOptions | None = None,
) -> AssistantMessageEventStream:
    """从 OpenAI 兼容端点拉取 chat completion 事件。

    返回一个 AssistantMessageEventStream。按 stream 契约,失败被编码为
    ``error`` 事件 —— 返回的 stream 的 ``.result()`` 永远 resolve 到一个
    AssistantMessage(失败时其 ``stop_reason`` 为 ``"error"``)。
    """
    compat = _resolve_compat(model)
    state = _StreamState(model)

    async def _drive() -> AsyncIterator:
        opts = options or {}
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise RuntimeError("openai package is required: pip install openai") from e

        api_key = opts.get("api_key")
        if not api_key:
            raise RuntimeError(
                "No API key: pass options['api_key'] or set the provider's env var."
            )

        base_url = opts.get("base_url", model.base_url)
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        params = build_params(model, context, opts, compat)
        stop_reason: StopReason = "stop"
        usage: Usage | None = None

        # ── 诊断:记录 SDK chunk 到达时间戳 ─────────────────────────────────
        import os as _os
        _DBG_LOG = _os.environ.get("CODING_AGENT_STREAM_DEBUG")
        _dbg_n = 0
        def _dbg(tag, **kw):
            if not _DBG_LOG:
                return
            nonlocal _dbg_n
            _dbg_n += 1
            with open(_DBG_LOG, "a", encoding="utf-8") as f:
                import time as _t
                f.write(f"{_t.perf_counter():.6f} SDK#{_dbg_n} {tag} {kw}\n")
        # ────────────────────────────────────────────────────────────────────

        try:
            created = await client.chat.completions.create(**params)
            _dbg("stream_open")
            async for chunk in created:
                _dbg("chunk",
                     reason=getattr(getattr(getattr(chunk.choices[0], "delta", None), "reasoning_content", None), "__len__", lambda: 0)() if chunk.choices else 0,
                     content=len(getattr(getattr(chunk.choices[0], "delta", None), "content", None) or "") if chunk.choices else 0)
                if chunk.usage:
                    usage = parse_chunk_usage(chunk.usage, model)

                choices = chunk.choices
                if not choices:
                    continue
                choice = choices[0]

                if choice.finish_reason:
                    stop_reason = map_stop_reason(choice.finish_reason)

                delta = choice.delta
                if delta is None:
                    continue

                # 文本内容。
                content = getattr(delta, "content", None)
                if content:
                    block = state.get_or_create_text(0)
                    block.text += content
                    state.stream.push({
                        "type": "text_delta", "content_index": state._content_index(block),
                        "delta": content, "partial": state.output,
                    })

                # 推理内容(DeepSeek reasoner 的思维链)。
                reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if reasoning:
                    block = state.get_or_create_thinking(0)
                    block.thinking += reasoning
                    state.stream.push({
                        "type": "thinking_delta", "content_index": state._content_index(block),
                        "delta": reasoning, "partial": state.output,
                    })

                # Tool 调用(函数调用)。
                tool_calls = getattr(delta, "tool_calls", None)
                if tool_calls:
                    for tc in tool_calls:
                        idx = getattr(tc, "index", None)
                        call_id = getattr(tc, "id", None)
                        block = state.ensure_tool_call(idx, call_id)
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            if not block["id"] and getattr(fn, "name", None):
                                block["name"] = fn.name
                            if not block["id"] and call_id:
                                block["id"] = call_id
                            args_delta = getattr(fn, "arguments", None) or ""
                            if args_delta:
                                block["_partial_args"] += args_delta
                                block["arguments"] = parse_streaming_json(block["_partial_args"])
                                # 同步 output.content 中实时的 ToolCall 镜像,
                                # 让 partial 反映不断增长的 arguments。
                                ref = block.get("_content_ref")
                                if ref is not None:
                                    ref.name = block["name"]
                                    ref.id = block["id"]
                                    ref.arguments = block["arguments"]
                                state.stream.push({
                                    "type": "toolcall_delta",
                                    "content_index": state.blocks.index(block),
                                    "delta": args_delta, "partial": state.output,
                                })

            # 收尾:解析最终 args、push toolcall_end、构造 content 数组。
            state.finish_blocks()
            state.output.usage = usage or Usage()
            state.output.usage.cost = _calculate_cost(model, state.output.usage) if usage else UsageCost()
            state.output.stop_reason = stop_reason
            state.stream.push({
                "type": "done",
                "reason": stop_reason if stop_reason in ("stop", "length", "tool_use") else "stop",
                "message": state.output,
            })

        except Exception as e:  # noqa: BLE001 — 编码为 error 事件
            state.output.stop_reason = "error"
            state.output.error_message = str(e)
            state.stream.push({
                "type": "error", "reason": "error", "error": state.output,
            })

    # 把 async generator 接到 event stream 上。
    _wire_generator(state.stream, _drive())
    return state.stream


def _wire_generator(stream: AssistantMessageEventStream, coro) -> None:
    """驱动一个把事件 push 到 stream 的协程。

    协程(``_drive``)直接调用 ``stream.push(event)`` 并以一个终止事件结束;
    它不 yield。我们把它作为后台任务运行。如果它抛异常(理论上不该发生 ——
    _drive 自带 try/except),就把错误编码进去。
    """
    import asyncio

    async def _run() -> None:
        try:
            await coro
        except Exception as e:  # noqa: BLE001 — 防御性:_drive 自己也会 catch
            msg = AssistantMessage(stop_reason="error", error_message=str(e))
            stream.push({"type": "error", "reason": "error", "error": msg})
            stream.end(msg)

    asyncio.ensure_future(_run())


# ─── stream_simple ─────────────────────────────────────────────────────

def stream_simple(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    """把 SimpleStreamOptions(reasoning 级别)映射成 API 选项,再调用 stream()。

    对 DeepSeek(thinking_format 为 "deepseek"),reasoning 由 SDK 在
    build_params 里通过 ``thinking`` 参数控制;非 reasoning model 或
    reasoning="off" 时,直接走普通流。
    """
    opts = options or {}
    api_key = opts.get("api_key")
    base = build_base_options(model, context, opts, api_key)

    reasoning = opts.get("reasoning")
    if not reasoning or not model.reasoning:
        # 没请求 reasoning,或 model 不支持。
        return stream(model, context, base)

    # 请求了 reasoning:调整 max_tokens,给 thinking 预算留出空间。
    max_tokens, _budget = adjust_max_tokens_for_thinking(
        base.get("max_tokens"), model.max_tokens, reasoning, opts.get("thinking_budgets")
    )
    base["max_tokens"] = max_tokens
    # thinking / reasoning 参数本身的注入发生在 build_params 里(经由
    # _inject_thinking_params),所以这里只需把 reasoning 透传过去。
    base["reasoning"] = reasoning

    return stream(model, context, base)
