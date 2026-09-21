"""Run the desktop runtime as a versioned NDJSON process over stdio."""
from __future__ import annotations

import asyncio
import sys
from typing import Any

from coding_agent.desktop.protocol import (
    PROTOCOL_VERSION,
    JsonLineWriter,
    RpcError,
    parse_request,
)
from coding_agent.desktop.runtime import DesktopRuntime


# These handlers address live work or wait on a captured task. Ordinary state
# mutations remain FIFO: opening a workspace/session must not race another one.
_OUT_OF_BAND_METHODS = frozenset({
    "runtime.ping", "run.abort", "approval.resolve", "run.steer", "run.followUp",
    "subagent.wait", "subagent.cancel",
})


async def serve() -> int:
    try:
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8", line_buffering=True, write_through=True)
    except (AttributeError, ValueError):
        pass

    def write_line(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    writer = JsonLineWriter(write_line)
    runtime = DesktopRuntime(writer.write)
    serial = asyncio.Lock()
    pending: set[asyncio.Task[None]] = set()
    shutdown_request: dict[str, Any] | None = None

    def write_error(request_id: str | None, exc: RpcError) -> None:
        writer.write({
            "v": PROTOCOL_VERSION,
            "id": request_id,
            "error": {"code": exc.code, "message": exc.message, "details": exc.details},
        })

    async def respond(request: dict[str, Any]) -> None:
        request_id = request["id"]
        try:
            result = await runtime.dispatch(request["method"], request["params"])
            writer.write({"v": PROTOCOL_VERSION, "id": request_id, "result": result})
        except RpcError as exc:
            write_error(request_id, exc)
        except Exception as exc:  # noqa: BLE001 - process boundary
            print(f"desktop sidecar error: {exc}", file=sys.stderr)
            writer.write({
                "v": PROTOCOL_VERSION,
                "id": request_id,
                "error": {"code": "INTERNAL_ERROR", "message": str(exc)},
            })

    async def dispatch(request: dict[str, Any]) -> None:
        if request["method"] in _OUT_OF_BAND_METHODS:
            await respond(request)
        else:
            async with serial:
                await respond(request)

    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            try:
                request = parse_request(line)
            except RpcError as exc:
                write_error(None, exc)
                continue
            if request["method"] == "runtime.dispose":
                shutdown_request = request
                break
            task = asyncio.create_task(dispatch(request))
            pending.add(task)
            task.add_done_callback(pending.discard)
    finally:
        # Stop request handlers before detaching the session. A disconnected
        # client must not leave a queued mutation or a compaction writing to it.
        active = list(pending)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        if shutdown_request is not None:
            await respond(shutdown_request)
        else:
            await runtime.dispose()
    return 0


def main() -> int:
    return asyncio.run(serve())


if __name__ == "__main__":
    raise SystemExit(main())
