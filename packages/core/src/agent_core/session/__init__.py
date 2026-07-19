"""Session persistence + compaction subpackage.

Public API:
  - SessionManager: create/open/continue session, append entries, build context
  - CompactionSettings / CompactionResult / SessionInfo: data structs
  - CompactionSummaryMessage + convert_messages_with_compaction: LLM boundary
  - serde: message <-> dict
  - compaction pure functions: estimate_tokens / find_cut_point / prepare_compaction / compact
"""
from agent_core.session.ids import create_session_id, encode_cwd, generate_entry_id
from agent_core.session.messages import (
    COMPACTION_SUMMARY_PREFIX,
    COMPACTION_SUMMARY_SUFFIX,
    CompactionSummaryMessage,
    convert_messages_with_compaction,
)
from agent_core.session.serde import dict_to_message, message_to_dict
from agent_core.session.session_manager import SessionManager
from agent_core.session.storage import default_agent_dir, session_dir_for_cwd
from agent_core.session.types import (
    CompactionDetails,
    CompactionEntry,
    CompactionPreparation,
    CompactionResult,
    CompactionSettings,
    ContextUsageEstimate,
    SessionContext,
    SessionEntry,
    SessionHeader,
    SessionInfo,
    SessionMessageEntry,
)
from agent_core.session.compaction import (
    FileOperations,
    estimate_context_tokens,
    estimate_tokens,
    extract_file_operations,
    find_cut_point,
    prepare_compaction,
    should_compact,
)
from agent_core.session.summarize import compact

__all__ = [
    "SessionManager",
    "SessionHeader",
    "SessionEntry",
    "SessionMessageEntry",
    "CompactionEntry",
    "CompactionDetails",
    "CompactionResult",
    "CompactionSettings",
    "CompactionPreparation",
    "ContextUsageEstimate",
    "CompactionSummaryMessage",
    "convert_messages_with_compaction",
    "SessionContext",
    "SessionInfo",
    "COMPACTION_SUMMARY_PREFIX",
    "COMPACTION_SUMMARY_SUFFIX",
    "create_session_id",
    "generate_entry_id",
    "encode_cwd",
    "message_to_dict",
    "dict_to_message",
    "default_agent_dir",
    "session_dir_for_cwd",
    # compaction pure functions
    "estimate_tokens",
    "estimate_context_tokens",
    "should_compact",
    "find_cut_point",
    "prepare_compaction",
    "extract_file_operations",
    "FileOperations",
    "compact",
]
