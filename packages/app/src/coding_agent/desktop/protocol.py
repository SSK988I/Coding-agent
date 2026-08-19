"""Small, versioned NDJSON protocol used by the desktop sidecar."""
from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import json
from pathlib import Path
import threading
from typing import Any, Callable

PROTOCOL_VERSION = 1


class RpcError(Exception):
    """An error safe to return to the desktop client."""

    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def to_jsonable(value: Any) -> Any:
    """Convert runtime dataclasses and typed objects to JSON-safe values."""
    if dataclasses.is_dataclass(value):
        return to_jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "__dict__"):
        return to_jsonable(vars(value))
    return str(value)


class JsonLineWriter:
    """Thread-safe JSONL writer; stdout is reserved for protocol messages."""

    def __init__(self, write_line: Callable[[str], None]) -> None:
        self._write_line = write_line
        self._lock = threading.Lock()

    def write(self, payload: dict[str, Any]) -> None:
        line = json.dumps(to_jsonable(payload), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._write_line(line + "\n")


def parse_request(line: str) -> dict[str, Any]:
    """Parse and validate one RPC request line."""
    try:
        request = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RpcError("INVALID_JSON", "请求不是合法 JSON", {"position": exc.pos}) from exc
    if not isinstance(request, dict):
        raise RpcError("INVALID_REQUEST", "请求必须是 JSON 对象")
    if request.get("v") != PROTOCOL_VERSION:
        raise RpcError("PROTOCOL_MISMATCH", f"仅支持协议版本 {PROTOCOL_VERSION}")
    if not isinstance(request.get("id"), str) or not request["id"]:
        raise RpcError("INVALID_REQUEST", "请求缺少字符串 id")
    if not isinstance(request.get("method"), str) or not request["method"]:
        raise RpcError("INVALID_REQUEST", "请求缺少字符串 method")
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise RpcError("INVALID_REQUEST", "params 必须是 JSON 对象")
    request["params"] = params
    return request
