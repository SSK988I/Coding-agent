"""tool 参数校验。

按 tool 的 JSON Schema ``parameters`` 校验一次 tool call 的参数,并做轻度
类型强转(字符串数字 -> int/float)。失败时抛出 ``Error``,message 中列出
每条出错的路径。

这里手写了一套 JSON Schema 检查,覆盖常见情形(type / required / enum /
minimum / maximum / items / properties),对 tool-call 参数来说已经够用。
深度校验并非必须 —— 无论结果如何,agent loop 都会把校验失败作为 error
tool result 上抛。
"""
from __future__ import annotations

import copy
from typing import Any

from agent_llm.types import Tool, ToolCall


def validate_tool_arguments(tool: Tool, tool_call: ToolCall) -> dict:
    """按 ``tool.parameters`` 校验 ``tool_call.arguments``。

    成功时返回强转之后的参数;失败时抛出 ``ValueError``,message 中列出
    每条出错路径。永不修改原始参数(使用深拷贝)。
    """
    args = copy.deepcopy(tool_call.arguments)
    schema = tool.parameters or {}
    errors: list[str] = []

    args = _coerce(args, schema)
    _validate(args, schema, "$", errors)

    if errors:
        received = json_dumps_pretty(tool_call.arguments)
        raise ValueError(
            f'tool "{tool_call.name}" 的参数校验失败:\n'
            + "\n".join(f"  - {e}" for e in errors)
            + f"\n\n收到的参数:\n{received}"
        )
    return args


def json_dumps_pretty(obj: Any) -> str:
    import json
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def _coerce(value: Any, schema: dict) -> Any:
    """按 schema 递归做类型强转并返回转换后的值。"""
    stype = schema.get("type")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                value[k] = _coerce(v, props[k])
        return value
    elif isinstance(value, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for index, item in enumerate(value):
                value[index] = _coerce(item, items_schema)
        return value
    elif stype == "number" and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    elif stype == "integer" and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    return value


def _validate(value: Any, schema: dict, path: str, errors: list[str]) -> None:
    """递归地按 ``schema`` 校验 ``value``,并把错误追加到 errors。"""
    stype = schema.get("type")
    if stype:
        if not _matches_type(value, stype):
            errors.append(f"{path}:期望 {stype},实际 {python_type_name(value)}")
            return

    if stype == "object" and isinstance(value, dict):
        required = schema.get("required", [])
        for req in required:
            if req not in value:
                errors.append(f"{path}.{req}:缺失必需属性")
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                _validate(v, props[k], f"{path}.{k}", errors)

    elif stype == "array" and isinstance(value, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for i, item in enumerate(value):
                _validate(item, items_schema, f"{path}[{i}]", errors)

    # enum
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        errors.append(f"{path}:{value!r} 不在 enum {enum} 中")

    # 数值边界
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}:{value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}:{value} > maximum {schema['maximum']}")

    # 字符串长度
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}:长度 {len(value)} < minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}:长度 {len(value)} > maxLength {schema['maxLength']}")


def _matches_type(value: Any, stype: str) -> bool:
    if stype == "string":
        return isinstance(value, str)
    if stype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if stype == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if stype == "boolean":
        return isinstance(value, bool)
    if stype == "object":
        return isinstance(value, dict)
    if stype == "array":
        return isinstance(value, list)
    if stype == "null":
        return value is None
    return True  # 未知类型:接受


def python_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__
