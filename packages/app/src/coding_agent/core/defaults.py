"""Default configuration values.

"""
from __future__ import annotations

from typing import Literal

#: All valid thinking levels, in display order.
VALID_THINKING_LEVELS: tuple["Literal['off', 'minimal', 'low', 'medium', 'high', 'xhigh']", ...] = (
    "off", "minimal", "low", "medium", "high", "xhigh",
)

#: Default reasoning/thinking level for models that support it.
DEFAULT_THINKING_LEVEL: "Literal['off', 'minimal', 'low', 'medium', 'high', 'xhigh']" = "medium"

#: Human-readable descriptions per level.
THINKING_LEVEL_DESCRIPTIONS: dict[str, str] = {
    "off": "不进行推理",
    "minimal": "极简推理（约 1k Token）",
    "low": "轻量推理（约 2k Token）",
    "medium": "中等推理（约 8k Token）",
    "high": "深度推理（约 16k Token）",
    "xhigh": "最大推理（约 32k Token）",
}
