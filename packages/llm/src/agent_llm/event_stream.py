"""AssistantMessageEventStream:核心 stream 抽象。

关键不变量:``.result()`` 永不 reject。错误总是被编码为一个 ``error`` 事件,
其载荷是一个带 ``stop_reason`` ``"error"`` 或 ``"aborted"`` 以及
``error_message`` 的 AssistantMessage。正因如此,每个上游边界(auth 失败、
网络错误、model/运行时失败)都能通过 stream 统一上抛,无需层层 try/except。
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Awaitable, Callable, Generic, TypeVar

from agent_llm.types import (
    AssistantMessage,
    AssistantMessageEvent,
    Model,
    Usage,
    UsageCost,
)

T = TypeVar("T")
R = TypeVar("R")


class EventStream(Generic[T, R]):
    """基于 push 的异步流，同时支持异步迭代和等待最终结果。

    生产者调用 :meth:`push` 把事件入队;消费者用 ``async for`` 迭代。终止
    事件(让 ``is_complete`` 返回 True 的事件)会在投递给消费者**之前**通过
    ``extract_result`` 完成 :meth:`result`。

    设计上是单消费者的:async 迭代器会排空队列 / 等待新的 push。
    """

    def __init__(
        self,
        is_complete: Callable[[T], bool],
        extract_result: Callable[[T], R],
    ) -> None:
        self._is_complete = is_complete
        self._extract_result = extract_result

        self._queue: asyncio.Queue[T | None] = asyncio.Queue()
        self._done = False
        self._result_future: asyncio.Future[R] = asyncio.get_event_loop().create_future()

    # ── 生产者 API ──────────────────────────────────────────────────────

    def push(self, event: T) -> None:
        """把一个事件入队。

        如果这是终止事件,先把 stream 标记为 done 并完成 :meth:`result`,
        **然后**再把事件投递给消费者(这样并发的 ``await stream.result()``
        会在终止事件 push 进来的瞬间就解除阻塞)。``done`` 之后 push 是空操作。
        """
        if self._done:
            return
        if self._is_complete(event):
            self._done = True
            if not self._result_future.done():
                self._result_future.set_result(self._extract_result(event))
        # 即使是终止事件也要入队:消费者必须能观察到它。
        self._queue.put_nowait(event)

    def end(self, result: R | None = None) -> None:
        """在不 push 终止事件的情况下标记迭代完成。

        若传入了 ``result`` 且 future 尚未完成,则完成它(适用于终止事件
        从未 push、或需要覆盖的场景)。通过入队哨兵值来唤醒阻塞的消费者。
        """
        self._done = True
        if result is not None and not self._result_future.done():
            self._result_future.set_result(result)
        # 唤醒阻塞的消费者:None 哨兵表示迭代结束。
        self._queue.put_nowait(None)

    # ── 消费者 API ──────────────────────────────────────────────────────

    def __aiter__(self) -> AsyncIterator[T]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[T]:
        while True:
            if self._done and self._queue.empty():
                return
            event = await self._queue.get()
            if event is None:
                # 来自 end() 的哨兵:停止迭代。
                return
            yield event

    def result(self) -> "asyncio.Future[R]":
        """一个 future,会在终止事件到来时 resolve 到其提取结果。

        永不抛出:即便 error/abort 终止也会 resolve 到一个携带失败详情的
        ``AssistantMessage``。
        """
        return self._result_future


class AssistantMessageEventStream(EventStream[AssistantMessageEvent, AssistantMessage]):
    """终止事件为 ``done`` / ``error`` 的 event stream。

    ``.result()`` 会 resolve 到:
      - ``done`` 事件 → ``event["message"]``
      - ``error`` 事件 → ``event["error"]``(携带 stop_reason
        ``"error"`` / ``"aborted"`` 以及 ``error_message``)
    """

    def __init__(self) -> None:
        super().__init__(is_complete=_is_terminal_event, extract_result=_extract_terminal_message)


def _is_terminal_event(event: AssistantMessageEvent) -> bool:
    return event.get("type") in ("done", "error")


def _extract_terminal_message(event: AssistantMessageEvent) -> AssistantMessage:
    etype = event.get("type")
    if etype == "done":
        return event["message"]  # type: ignore[index]
    if etype == "error":
        return event["error"]  # type: ignore[index]
    raise AssertionError(f"未预期的终止事件类型:{etype}")


# ─── lazy_stream ───────────────────────────────────────────────────────

def _empty_usage() -> Usage:
    return Usage(cost=UsageCost())


def _error_message(model: Model, error: Any) -> AssistantMessage:
    """为 setup 失败构造一个零 usage 的 AssistantMessage。

    把失败编码为一个 ``stop_reason="error"`` 的 assistant message。
    """
    msg = error if isinstance(error, str) else (str(error) or error.__class__.__name__)
    return AssistantMessage(
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=_empty_usage(),
        stop_reason="error",
        error_message=msg,
    )


def lazy_stream(
    model: Model,
    setup: Callable[[], Awaitable[AsyncIterator[AssistantMessageEvent]]],
) -> AssistantMessageEventStream:
    """把一个产出 event 迭代器的 async setup 包装成 stream。

    同步返回外层 stream,在背后运行 ``setup()``。两种结果:
      - setup 成功:内部迭代器的每个事件都被 push 到外层 stream,然后
        调用 ``end()``。
      - setup 失败:错误被编码为 ``error`` 事件(绝不重新抛出),然后
        用错误 message 调用 ``end()``。
    """
    outer = AssistantMessageEventStream()

    async def _drive() -> None:
        try:
            inner = await setup()
            async for event in inner:
                outer.push(event)
            outer.end()
        except Exception as e:  # noqa: BLE001 — 编码为 error 事件,绝不抛出
            message = _error_message(model, e)
            outer.push({"type": "error", "reason": "error", "error": message})
            outer.end(message)

    # 在运行中的 loop 上 fire-and-forget 这个驱动协程。异常都在 _drive
    # 内部处理了,所以这里不会冒出未处理任务错误。
    asyncio.ensure_future(_drive())
    return outer
