"""DeepSeek Responses API streaming backend.

DeepSeek's ``/responses`` endpoint is OpenAI-SDK compatible but exposes two
capabilities that its Chat Completions endpoint does not: native server-side
``web_search`` and semantic streaming events.  This module translates those
events into the provider-neutral ``agent_llm`` stream contract while keeping
ordinary application function tools working.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from agent_llm.api.simple_options import adjust_max_tokens_for_thinking, build_base_options
from agent_llm.event_stream import AssistantMessageEventStream
from agent_llm.types import (
    AssistantMessage,
    Context,
    ImageContent,
    Model,
    SimpleStreamOptions,
    StopReason,
    StreamOptions,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    Usage,
    UsageCost,
)
from agent_llm.utils.json_parse import parse_streaming_json


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif isinstance(block, ImageContent):
            parts.append("(image)")
    return "\n".join(parts)


def _user_content(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for block in content or []:
        if isinstance(block, TextContent):
            parts.append({"type": "input_text", "text": block.text})
        elif isinstance(block, ImageContent):
            parts.append({
                "type": "input_image",
                "detail": "auto",
                "image_url": f"data:{block.mime_type};base64,{block.data}",
            })
    return parts


def _provider_response_items(message: AssistantMessage) -> list[dict[str, Any]]:
    data = message.provider_data
    if not isinstance(data, dict):
        return []
    items = data.get("response_items")
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def convert_input(context: Context) -> list[dict[str, Any]]:
    """Convert the full local transcript to stateless Responses input items."""
    items: list[dict[str, Any]] = []
    for message in context.messages:
        if message.role == "user":
            items.append({"role": "user", "content": _user_content(message.content)})
            continue

        if message.role == "assistant":
            # DeepSeek requires reasoning and native web-search items to be
            # replayed on later stateless requests. They are opaque to the app.
            provider_items = _provider_response_items(message)
            items.extend(provider_items)

            # Older persisted messages have no provider_data. Preserve a signed
            # reasoning item when possible; unsigned thinking is deliberately
            # omitted because the endpoint requires a provider-issued item id.
            if not any(item.get("type") == "reasoning" for item in provider_items):
                for block in message.content:
                    if isinstance(block, ThinkingContent) and block.thinking_signature:
                        items.append({
                            "type": "reasoning",
                            "id": block.thinking_signature,
                            "summary": [],
                            "content": [{"type": "reasoning_text", "text": block.thinking}],
                            "status": "completed",
                        })

            text = "\n".join(
                block.text for block in message.content
                if isinstance(block, TextContent) and block.text
            )
            if text:
                items.append({"role": "assistant", "content": text})

            for block in message.content:
                if isinstance(block, ToolCall):
                    items.append({
                        "type": "function_call",
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": json.dumps(block.arguments, ensure_ascii=False),
                    })
            continue

        if message.role == "toolResult":
            items.append({
                "type": "function_call_output",
                "call_id": message.tool_call_id,
                "output": _content_to_text(message.content) or "(no output)",
            })
    return items


def convert_tools(tools: list[Tool]) -> list[dict[str, Any]]:
    return [{
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
        "strict": False,
    } for tool in tools]


def build_params(
    model: Model,
    context: Context,
    options: StreamOptions | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build ``AsyncOpenAI.responses.create`` keyword arguments."""
    opts: dict[str, Any] = dict(options or {})
    params: dict[str, Any] = {
        "model": model.id,
        "input": convert_input(context),
        "stream": True,
    }
    if context.system_prompt:
        params["instructions"] = context.system_prompt

    max_tokens = opts.get("max_tokens")
    if max_tokens is None:
        max_tokens = model.max_tokens or None
    if max_tokens is not None:
        params["max_output_tokens"] = max_tokens

    tools = convert_tools(context.tools or [])
    if opts.get("web_search"):
        tools.append({"type": "web_search"})
    if tools:
        params["tools"] = tools

    # Responses defaults DeepSeek reasoning on. Sending ``none`` is required
    # for the UI's THINKING off state to actually mean off.
    level = opts.get("reasoning")
    effort = "max" if level == "xhigh" else (level or "none")
    params["reasoning"] = {"effort": effort}
    return params


def _calculate_cost(model: Model, usage: Usage) -> UsageCost:
    cost = model.cost
    input_cost = cost.input / 1_000_000 * usage.input
    output_cost = cost.output / 1_000_000 * usage.output
    cache_read_cost = cost.cache_read / 1_000_000 * usage.cache_read
    cache_write_cost = cost.cache_write / 1_000_000 * usage.cache_write
    return UsageCost(
        input=input_cost,
        output=output_cost,
        cache_read=cache_read_cost,
        cache_write=cache_write_cost,
        total=input_cost + output_cost + cache_read_cost + cache_write_cost,
    )


def parse_usage(raw: Any, model: Model) -> Usage:
    if raw is None:
        return Usage()
    input_total = int(getattr(raw, "input_tokens", 0) or 0)
    input_details = getattr(raw, "input_tokens_details", None)
    cache_read = int(getattr(input_details, "cached_tokens", 0) or 0)
    output = int(getattr(raw, "output_tokens", 0) or 0)
    output_details = getattr(raw, "output_tokens_details", None)
    reasoning = int(getattr(output_details, "reasoning_tokens", 0) or 0)
    usage = Usage(
        input=max(0, input_total - cache_read),
        output=output,
        cache_read=cache_read,
        reasoning=reasoning,
        total_tokens=int(getattr(raw, "total_tokens", input_total + output) or 0),
    )
    usage.cost = _calculate_cost(model, usage)
    return usage


def _model_dump(item: Any) -> dict[str, Any] | None:
    if isinstance(item, dict):
        return dict(item)
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        value = dump(exclude_none=True)
        return value if isinstance(value, dict) else None
    return None


class _StreamState:
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
        self.stream = AssistantMessageEventStream()
        self._started = False
        self._blocks: list[Any] = []
        self._texts: dict[tuple[int, int], TextContent] = {}
        self._thinking: dict[tuple[int, int], ThinkingContent] = {}
        self._tools: dict[str, ToolCall] = {}
        self._tool_args: dict[str, str] = {}
        self._ended_tools: set[str] = set()

    def _index(self, block: Any) -> int:
        return self._blocks.index(block)

    def _emit_start(self) -> None:
        if not self._started:
            self._started = True
            self.stream.push({"type": "start", "partial": self.output})

    def text(self, output_index: int, content_index: int, item_id: str = "") -> TextContent:
        key = (output_index, content_index)
        block = self._texts.get(key)
        if block is None:
            block = TextContent(text="", text_signature=item_id or None)
            self._texts[key] = block
            self._blocks.append(block)
            self.output.content.append(block)
            self._emit_start()
            self.stream.push({
                "type": "text_start",
                "content_index": self._index(block),
                "partial": self.output,
            })
        elif item_id and not block.text_signature:
            block.text_signature = item_id
        return block

    def thinking(
        self, output_index: int, content_index: int, item_id: str = "",
    ) -> ThinkingContent:
        key = (output_index, content_index)
        block = self._thinking.get(key)
        if block is None:
            block = ThinkingContent(thinking="", thinking_signature=item_id or None)
            self._thinking[key] = block
            self._blocks.append(block)
            self.output.content.append(block)
            self._emit_start()
            self.stream.push({
                "type": "thinking_start",
                "content_index": self._index(block),
                "partial": self.output,
            })
        elif item_id and not block.thinking_signature:
            block.thinking_signature = item_id
        return block

    def tool(self, key: str, *, call_id: str = "", name: str = "") -> ToolCall:
        block = self._tools.get(key)
        if block is None:
            block = ToolCall(id=call_id, name=name, arguments={})
            self._tools[key] = block
            self._tool_args[key] = ""
            self._blocks.append(block)
            self.output.content.append(block)
            self._emit_start()
            self.stream.push({
                "type": "toolcall_start",
                "content_index": self._index(block),
                "partial": self.output,
            })
        if call_id:
            block.id = call_id
        if name:
            block.name = name
        return block

    def tool_delta(self, key: str, delta: str) -> None:
        block = self.tool(key)
        self._tool_args[key] = self._tool_args.get(key, "") + delta
        block.arguments = parse_streaming_json(self._tool_args[key])
        self.stream.push({
            "type": "toolcall_delta",
            "content_index": self._index(block),
            "delta": delta,
            "partial": self.output,
        })

    def finish_tool(self, key: str, arguments: str | None = None) -> None:
        if key in self._ended_tools:
            return
        block = self.tool(key)
        if arguments is not None:
            self._tool_args[key] = arguments
        block.arguments = parse_streaming_json(self._tool_args.get(key, ""))
        self._ended_tools.add(key)
        self.stream.push({
            "type": "toolcall_end",
            "content_index": self._index(block),
            "tool_call": block,
            "partial": self.output,
        })

    def finish_all_tools(self) -> None:
        for key in list(self._tools):
            self.finish_tool(key)

    def hydrate_response(self, response: Any) -> None:
        """Fill metadata and recover content if an upstream omitted deltas."""
        self.output.response_id = getattr(response, "id", None)
        response_model = getattr(response, "model", None)
        if response_model:
            self.output.response_model = str(response_model)
        self.output.usage = parse_usage(getattr(response, "usage", None), self.model)

        provider_items: list[dict[str, Any]] = []
        for output_index, item in enumerate(getattr(response, "output", None) or []):
            item_type = getattr(item, "type", None)
            item_id = str(getattr(item, "id", "") or "")
            if item_type in {"reasoning", "web_search_call"}:
                dumped = _model_dump(item)
                if dumped is not None:
                    provider_items.append(dumped)

            if item_type == "reasoning":
                for content_index, part in enumerate(getattr(item, "content", None) or []):
                    text = str(getattr(part, "text", "") or "")
                    block = self.thinking(output_index, content_index, item_id)
                    if text and not block.thinking:
                        block.thinking = text
            elif item_type == "message":
                for content_index, part in enumerate(getattr(item, "content", None) or []):
                    if getattr(part, "type", None) != "output_text":
                        continue
                    text = str(getattr(part, "text", "") or "")
                    block = self.text(output_index, content_index, item_id)
                    if text and not block.text:
                        block.text = text
                    self._append_citations(block, getattr(part, "annotations", None) or [])
            elif item_type == "function_call":
                key = item_id or f"output:{output_index}"
                block = self.tool(
                    key,
                    call_id=str(getattr(item, "call_id", "") or item_id),
                    name=str(getattr(item, "name", "") or ""),
                )
                arguments = str(getattr(item, "arguments", "") or "")
                if arguments:
                    self._tool_args[key] = arguments
                    block.arguments = parse_streaming_json(arguments)

        if provider_items:
            self.output.provider_data = {"response_items": provider_items}

    def _append_citations(self, block: TextContent, annotations: list[Any]) -> None:
        citations: list[tuple[str, str]] = []
        seen: set[str] = set()
        for annotation in annotations:
            if getattr(annotation, "type", None) != "url_citation":
                continue
            url = str(getattr(annotation, "url", "") or "")
            if not url or url in seen or url in block.text:
                continue
            seen.add(url)
            title = str(getattr(annotation, "title", "") or url).replace("[", "\\[")
            citations.append((title, url))
        if not citations:
            return
        suffix = "\n\nSources:\n" + "\n".join(
            f"- [{title}]({url})" for title, url in citations
        )
        block.text += suffix
        self.stream.push({
            "type": "text_delta",
            "content_index": self._index(block),
            "delta": suffix,
            "partial": self.output,
        })


def _response_error(response: Any) -> str:
    error = getattr(response, "error", None)
    if error is not None:
        return str(getattr(error, "message", None) or error)
    details = getattr(response, "incomplete_details", None)
    reason = getattr(details, "reason", None)
    return str(reason or "DeepSeek Responses API request failed")


def stream(
    model: Model,
    context: Context,
    options: StreamOptions | None = None,
) -> AssistantMessageEventStream:
    state = _StreamState(model)

    async def _drive() -> None:
        opts: dict[str, Any] = dict(options or {})
        try:
            from openai import AsyncOpenAI

            api_key = opts.get("api_key")
            if not api_key:
                raise RuntimeError(
                    "No API key: pass options['api_key'] or set DEEPSEEK_API_KEY."
                )
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=opts.get("base_url", model.base_url),
            )
            created = await client.responses.create(**build_params(model, context, opts))
            terminal = False
            async for event in created:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_item.added":
                    item = event.item
                    item_type = getattr(item, "type", None)
                    item_id = str(getattr(item, "id", "") or "")
                    if item_type == "function_call":
                        key = item_id or f"output:{event.output_index}"
                        state.tool(
                            key,
                            call_id=str(getattr(item, "call_id", "") or item_id),
                            name=str(getattr(item, "name", "") or ""),
                        )
                elif event_type == "response.output_text.delta":
                    block = state.text(event.output_index, event.content_index, event.item_id)
                    block.text += event.delta
                    state.stream.push({
                        "type": "text_delta",
                        "content_index": state._index(block),
                        "delta": event.delta,
                        "partial": state.output,
                    })
                elif event_type == "response.reasoning_text.delta":
                    block = state.thinking(
                        event.output_index, event.content_index, event.item_id,
                    )
                    block.thinking += event.delta
                    state.stream.push({
                        "type": "thinking_delta",
                        "content_index": state._index(block),
                        "delta": event.delta,
                        "partial": state.output,
                    })
                elif event_type == "response.function_call_arguments.delta":
                    state.tool_delta(event.item_id, event.delta)
                elif event_type == "response.function_call_arguments.done":
                    block = state.tool(event.item_id, name=event.name)
                    if not block.id:
                        block.id = event.item_id
                    state.finish_tool(event.item_id, event.arguments)
                elif event_type == "response.completed":
                    state.hydrate_response(event.response)
                    state.finish_all_tools()
                    reason: StopReason = "tool_use" if state._tools else "stop"
                    state.output.stop_reason = reason
                    state.stream.push({
                        "type": "done",
                        "reason": reason,
                        "message": state.output,
                    })
                    terminal = True
                    break
                elif event_type == "response.incomplete":
                    state.hydrate_response(event.response)
                    state.finish_all_tools()
                    state.output.stop_reason = "length"
                    state.stream.push({
                        "type": "done",
                        "reason": "length",
                        "message": state.output,
                    })
                    terminal = True
                    break
                elif event_type == "response.failed":
                    state.hydrate_response(event.response)
                    raise RuntimeError(_response_error(event.response))
                elif event_type == "error":
                    raise RuntimeError(str(getattr(event, "message", None) or event))

            if not terminal:
                raise RuntimeError("DeepSeek Responses stream ended without a terminal event")
        except Exception as exc:  # noqa: BLE001 - errors are stream values by contract
            state.output.stop_reason = "error"
            state.output.error_message = str(exc) or exc.__class__.__name__
            state.stream.push({
                "type": "error",
                "reason": "error",
                "error": state.output,
            })

    _wire_coroutine(state.stream, _drive())
    return state.stream


def _wire_coroutine(stream: AssistantMessageEventStream, coroutine: Any) -> None:
    async def _run() -> None:
        try:
            await coroutine
        except Exception as exc:  # pragma: no cover - defensive boundary
            message = AssistantMessage(stop_reason="error", error_message=str(exc))
            stream.push({"type": "error", "reason": "error", "error": message})
            stream.end(message)

    asyncio.ensure_future(_run())


def stream_simple(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    opts = options or {}
    base: dict[str, Any] = dict(
        build_base_options(model, context, opts, opts.get("api_key"))
    )
    reasoning = opts.get("reasoning")
    if reasoning and model.reasoning:
        max_tokens, _ = adjust_max_tokens_for_thinking(
            base.get("max_tokens"),
            model.max_tokens,
            reasoning,
            opts.get("thinking_budgets"),
        )
        base["max_tokens"] = max_tokens
        base["reasoning"] = reasoning
    return stream(model, context, cast(StreamOptions, base))


__all__ = [
    "build_params",
    "convert_input",
    "convert_tools",
    "parse_usage",
    "stream",
    "stream_simple",
]
