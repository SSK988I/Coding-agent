"""Prompts and schema contract for memory extraction."""
from __future__ import annotations

import json

EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["operations"],
    "properties": {
        "operations": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "operation", "kind", "scope", "key", "value", "summary",
                    "confidence", "sourceKind", "evidenceEntryIds",
                ],
                "properties": {
                    "operation": {"enum": ["upsert", "retract", "noop"]},
                    "kind": {"enum": ["preference", "decision", "constraint", "convention"]},
                    "scope": {"enum": ["global", "project"]},
                    "key": {"type": "string", "maxLength": 120},
                    "value": {},
                    "summary": {"type": "string", "maxLength": 300},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "sourceKind": {
                        "enum": [
                            "explicit_correction", "explicit_user",
                            "accepted_plan", "inferred_user",
                        ]
                    },
                    "evidenceEntryIds": {
                        "type": "array", "minItems": 1, "maxItems": 12,
                        "items": {"type": "string"},
                    },
                },
            },
        }
    },
}

EXTRACTION_SYSTEM_PROMPT = f"""你是 Coding Agent 的长期记忆提取器。
只从提供的 evidence 中提取未来任务仍有价值的用户偏好、项目决策、约束和规范。

必须遵守：
1. 用户原文和明确接受的 Plan 才能作为证据；最终助手回复只能帮助理解，不能独立证明事实。
2. 不保存一次性任务要求、工具输出、代码内容、错误日志、模型建议或未接受的 Plan。
3. 不保存密码、API Key、Token、Cookie、私钥、支付信息或敏感个人属性。
4. 不确定时返回空 operations；宁可少记，不要猜测。
5. 全局偏好使用 global；仓库技术选择和约束使用 project。
6. 用户明确修改旧选择时使用 explicit_correction；普通直接陈述使用 explicit_user；
   已确认 Plan 中的选择使用 accepted_plan；只有非常明确的隐含偏好才使用 inferred_user。
7. key 使用稳定的英文点分层命名，例如 response.language、tooling.package_manager。
8. 只输出一个 JSON 对象，不要 Markdown，不要解释。

输出必须符合以下 JSON Schema：
{json.dumps(EXTRACTION_SCHEMA, ensure_ascii=False, separators=(',', ':'))}
"""

__all__ = ["EXTRACTION_SCHEMA", "EXTRACTION_SYSTEM_PROMPT"]
