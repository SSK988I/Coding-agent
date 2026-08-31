"""Prompts and strict schemas for the memory generation pipeline."""
from __future__ import annotations

import json

EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["memories"],
    "properties": {
        "memories": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "kind", "scope", "content", "value", "relationKey",
                    "confidence", "sourceKind", "evidenceEntryIds",
                ],
                "properties": {
                    "kind": {
                        "enum": [
                            "preference", "decision", "constraint", "convention",
                            "fact", "lesson",
                        ]
                    },
                    "scope": {"enum": ["global", "project"]},
                    "content": {"type": "string", "maxLength": 300},
                    "value": {},
                    "relationKey": {"type": ["string", "null"], "maxLength": 120},
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

CONSOLIDATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["candidateIndex", "action", "targetRecordId", "reason"],
                "properties": {
                    "candidateIndex": {"type": "integer", "minimum": 0},
                    "action": {
                        "enum": [
                            "add", "reinforce", "supersede", "conflict", "ignore",
                        ]
                    },
                    "targetRecordId": {"type": ["string", "null"]},
                    "reason": {"type": "string", "maxLength": 300},
                },
            },
        }
    },
}

EXTRACTION_SYSTEM_PROMPT = f"""你是 Coding Agent 的长期记忆提取器。
从 evidence 中自动提取对未来任务仍有用的原子记忆；用户不需要说“请记住”。

必须遵守：
1. 每条 memory 只表达一件事，并写成脱离当前对话也能理解的完整陈述。
2. 只把用户原文或明确接受的 Plan 当作证据；助手回复只能帮助理解，不能独立证明事实。
3. 可提取：稳定偏好、明确决策、长期约束、项目规范、普通身份/环境事实、可复用经验。
   例如“我是 ssk”可提取为 global fact，并使用 relationKey identity.display_name。
4. 不保存一次性任务要求、寒暄、工具输出、代码正文、错误日志、模型建议或未接受的 Plan。
5. 不保存密码、API Key、Token、Cookie、私钥、支付信息，以及健康、宗教、种族、性取向等敏感属性。
6. 用户明确纠正先前陈述时用 explicit_correction；普通直接陈述用 explicit_user；
   已确认 Plan 用 accepted_plan；只有证据非常充分时才用 inferred_user。
7. 只有存在“同一槽位只能有一个当前值”的事实才设置 relationKey，使用稳定英文点分命名；
   可并存的事实或经验将 relationKey 设为 null。不要为了凑字段虚构 relationKey。
8. 全局用户事实和跨项目偏好使用 global；仓库内决策、规范和经验使用 project。
9. 不确定或无长期价值时返回空 memories；宁可少记，不要猜测。
10. 只输出一个 JSON 对象，不要 Markdown，不要解释。

输出必须符合以下 JSON Schema：
{json.dumps(EXTRACTION_SCHEMA, ensure_ascii=False, separators=(',', ':'))}
"""

CONSOLIDATION_SYSTEM_PROMPT = f"""你是 Coding Agent 的长期记忆整合器。
你会收到本轮提取出的原子记忆和当前相关记忆。为每个 candidate 选择且只选择一个动作：
- add：不存在等价或互斥目标，新增记录；
- reinforce：与某条现有记录语义一致，强化该记录；
- supersede：用户以更高权威明确更新同一 relationKey 的旧值；
- conflict：同一 relationKey 的内容互斥，但证据不足以安全覆盖；
- ignore：重复、短期、无价值、不安全或无法可靠判断。

必须遵守：
1. reinforce/supersede/conflict 必须填写 existing 中真实存在的 targetRecordId。
2. add/ignore 的 targetRecordId 必须为 null。
3. 不得仅凭措辞相似合并两个可并存事实；relationKey 为 null 时，只有语义等价才 reinforce。
4. 模型动作不能绕过来源权威、墓碑和并发校验；应用层会再次验证。
5. 为每个 candidateIndex 恰好输出一个 decision，不能遗漏、重复或增加索引。
6. 自动整合无权删除记忆；删除只能由用户通过显式的记忆管理入口执行。
7. 只输出 JSON，不要 Markdown，不要解释。

输出必须符合以下 JSON Schema：
{json.dumps(CONSOLIDATION_SCHEMA, ensure_ascii=False, separators=(',', ':'))}
"""

__all__ = [
    "CONSOLIDATION_SCHEMA",
    "CONSOLIDATION_SYSTEM_PROMPT",
    "EXTRACTION_SCHEMA",
    "EXTRACTION_SYSTEM_PROMPT",
]
