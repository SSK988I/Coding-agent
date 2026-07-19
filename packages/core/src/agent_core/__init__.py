"""Agent runtime with tool calling and session management.

The public API exposes the runtime loop, tools, events, and session types.
"""
from agent_core.agent import Agent, PendingMessageQueue, default_convert_to_llm
from agent_core.agent_loop import run_agent_loop, run_agent_loop_continue
from agent_core.compaction_orchestrator import CompactionOrchestrator
from agent_core.prompts import (
    build_system_prompt,
    escape_xml,
    format_skills_for_prompt,
)
from agent_core.types import (
    AgentContext,
    AgentEvent,
    AgentEventSink,
    AgentLoopConfig,
    AgentMessage,
    AgentState,
    AgentTool,
    AgentToolResult,
    AgentToolUpdateCallback,
    AfterToolCallContext,
    AfterToolCallResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
    QueueMode,
    StreamFn,
    ToolExecutionMode,
)
from agent_core.tools.bash import BashRawResult, BashTool
from agent_core.tools.edit import EditTool
from agent_core.tools.find import FindTool
from agent_core.tools.grep import GrepTool
from agent_core.tools.ls import LsTool
from agent_core.tools.read import ReadTool
from agent_core.tools.write import WriteTool
from agent_core.session import (
    CompactionSettings,
    CompactionResult,
    SessionInfo,
    SessionManager,
)

__all__ = [
    "Agent",
    "default_convert_to_llm",
    "run_agent_loop",
    "run_agent_loop_continue",
    "AgentTool",
    "AgentToolResult",
    "AgentToolUpdateCallback",
    "AgentEvent",
    "AgentEventSink",
    "AgentState",
    "AgentContext",
    "AgentLoopConfig",
    "AgentMessage",
    "StreamFn",
    "ToolExecutionMode",
    "QueueMode",
    "PendingMessageQueue",
    "BeforeToolCallContext",
    "BeforeToolCallResult",
    "AfterToolCallContext",
    "AfterToolCallResult",
    "ReadTool",
    "WriteTool",
    "BashTool",
    "BashRawResult",
    "EditTool",
    "GrepTool",
    "FindTool",
    "LsTool",
    # session persistence + compaction
    "SessionManager",
    "SessionInfo",
    "CompactionSettings",
    "CompactionResult",
    "CompactionOrchestrator",
    # system prompt
    "build_system_prompt",
    "escape_xml",
    "format_skills_for_prompt",
]

__version__ = "0.1.0"
