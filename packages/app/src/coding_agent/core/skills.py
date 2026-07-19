"""Skill prompt formatting.

Provides the ``Skill`` dataclass used throughout the coding-agent. The actual
prompt formatting (``format_skills_for_prompt`` / ``escape_xml``) lives in
``agent_core.prompts`` to avoid a circular dependency (agent_core must not import
from coding-agent); this module re-exports them for convenience.

Skill discovery and loading are handled by the application layer.

"""
from __future__ import annotations

from dataclasses import dataclass

# Re-exported from agent_core to avoid duplicating the prompt-rendering logic.
# ``build_system_prompt`` in agent_core reads skill entries duck-typed on
# name/description/file_path/disable_model_invocation, so a coding-agent Skill
# works directly without conversion.
from agent_core.prompts import escape_xml, format_skills_for_prompt  # noqa: F401


@dataclass
class Skill:
    """A discoverable skill (SKILL.md).

    Only ``name``, ``description`` and ``file_path`` are needed for prompt
    formatting. ``disable_model_invocation`` hides it from the prompt.
    """

    name: str
    description: str
    file_path: str
    base_dir: str = ""
    source_info: object | None = None
    disable_model_invocation: bool = False


__all__ = ["Skill", "escape_xml", "format_skills_for_prompt"]
