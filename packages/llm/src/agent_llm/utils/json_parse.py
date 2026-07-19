"""用于 streaming tool-call 参数的部分 JSON 解析。

容忍接收了一半的 JSON:先尝试严格解析,然后尝试补全末尾未闭合的
``{`` / ``[``,并削掉末尾悬空的逗号/冒号,最后回退到 ``{}``。

这样 tool-call 的 ``arguments`` 可以在 stream 中途就被实时解析,让 UI 能
在 model 还没吐完之前就渲染出部分参数。
"""
from __future__ import annotations

import json


def parse_streaming_json(text: str | None) -> dict:
    """尽力解析一个可能不完整的 JSON 对象字符串。

    返回一个 dict。空串/纯空白输入返回 ``{}``。非对象 JSON(例如一个裸
    数字)只有在能干净解析时才会被包装成 ``{"value": <解析值>}`` 返回;
    否则返回 ``{}``。
    """
    if not text:
        return {}
    s = text.strip()
    if not s:
        return {}

    # 1. 严格解析 —— 快乐路径。
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        pass

    # 2. 容错修复:闭合末尾未闭合的结构,并削掉悬空的逗号/冒号。
    #    即"先修复"策略。
    repaired = _close_trailing(s)
    if repaired != s:
        try:
            parsed = json.loads(repaired)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass

    return {}


def _close_trailing(s: str) -> str:
    """为未闭合的 ``{`` / ``[`` 追加对应的闭合符,并削掉悬空的末尾 token。

    跟踪字符串状态,避免字符串内部的引号干扰深度计数。会去掉末尾的逗号或
    冒号 —— 否则它们会让修复后的 JSON 不合法。
    """
    out: list[str] = []
    stack: list[str] = []  # 存放 '{' 或 '['
    in_string = False
    escape = False

    for ch in s:
        out.append(ch)
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("{")
        elif ch == "[":
            stack.append("[")
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()

    repaired = "".join(out)

    # 削掉末尾悬空的逗号或冒号(model 在 key 或 value 之后暂停时常见)。
    stripped = repaired.rstrip()
    while stripped and stripped[-1] in ",:":
        stripped = stripped[:-1].rstrip()

    # 按开括号的逆序追加对应的闭合符。
    for opener in reversed(stack):
        stripped += "}" if opener == "{" else "]"

    return stripped
