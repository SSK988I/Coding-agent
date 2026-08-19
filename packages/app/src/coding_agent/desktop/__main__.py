"""Run the desktop runtime as a versioned NDJSON process over stdio."""
from __future__ import annotations

import asyncio
import sys

from coding_agent.desktop.protocol import (
    PROTOCOL_VERSION,
    JsonLineWriter,
    RpcError,
    parse_request,
)
from coding_agent.desktop.runtime import DesktopRuntime


async def serve() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True, write_through=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True, write_through=True)
    except (AttributeError, ValueError):
        pass

    writer = JsonLineWriter(lambda text: (sys.stdout.write(text), sys.stdout.flush()))
    runtime = DesktopRuntime(writer.write)

    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            break
        request_id: str | None = None
        try:
            request = parse_request(line)
            request_id = request["id"]
            result = await runtime.dispatch(request["method"], request["params"])
            writer.write({"v": PROTOCOL_VERSION, "id": request_id, "result": result})
        except RpcError as exc:
            writer.write({
                "v": PROTOCOL_VERSION,
                "id": request_id,
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
            })
        except Exception as exc:  # noqa: BLE001 - process boundary
            print(f"desktop sidecar error: {exc}", file=sys.stderr)
            writer.write({
                "v": PROTOCOL_VERSION,
                "id": request_id,
                "error": {"code": "INTERNAL_ERROR", "message": str(exc)},
            })

    await runtime.dispose()
    return 0


def main() -> int:
    return asyncio.run(serve())


if __name__ == "__main__":
    raise SystemExit(main())
