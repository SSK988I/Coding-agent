"""Built-in web search backends."""

from coding_agent.search.backends.deepseek import DeepSeekSearchBackend
from coding_agent.search.backends.zhipu import ZhipuMcpSearchBackend

__all__ = ["DeepSeekSearchBackend", "ZhipuMcpSearchBackend"]
