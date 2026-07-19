"""Mock chat model for testing and development.

Provides simulated responses with configurable delay and response patterns.
Useful for building and testing chat UIs without API keys or network.
"""

import asyncio
import random
from typing import AsyncIterator, Optional

from agent_tui.models.base import BaseChatModel, Message, StreamEvent


# 多样化的中文模拟回复（含 Markdown 格式）
MOCK_RESPONSES = [
    """这个观点很有意思！我来分享一些想法：

## 关键考量

1. **架构设计**：一个好的系统应该是模块化、可扩展的。
2. **性能优化**：可以考虑缓存策略和懒加载。
3. **测试覆盖**：完善的测试能有效防止回归问题。

需要我详细展开其中某一点吗？""",

    """关于这个问题，我的思路是这样的：

```python
def solve_problem(data: list[int]) -> int:
    \"\"\"处理数据并返回最优结果。\"\"\"
    if not data:
        return 0
    result = sum(data) / len(data)
    return max(0, int(result))
```

这是一个简单的实现，还有很多优化空间可以探讨。""",

    """好问题！这里有几种方案供参考：

- **方案 A**：使用哈希表实现 O(1) 的查找效率
- **方案 B**：使用字典树（Trie）支持前缀搜索
- **方案 C**：如果数据有序，可以采用二分查找

> "最好的优化，是你根本不需要的那个。"
> — 佚名

你觉得哪种方案最适合你的场景？""",

    """我理解你的意思了。让我来拆解一下：

| 维度 | 当前方案 | 建议方案 | 提升效果 |
|------|---------|---------|---------|
| 速度 | O(n²) | O(n log n) | 快 10 倍 |
| 内存 | 256MB | 128MB | 减少一半 |
| 代码量 | 200 行 | 150 行 | 更简洁 |

建议方案在各个维度上都有显著改进。""",

    """这个视角很有意思！以下是我的设计思路：

### 架构概览

1. **输入层**：解析并校验输入数据
2. **处理管道**：按顺序执行数据转换
3. **输出层**：格式化并流式返回结果

### 关键决策

- 使用 `async/await` 实现非阻塞 I/O
- 采用指数退避策略处理重试
- 添加结构化日志提升可观测性

这和你预想的方向一致吗？""",

    """观察得很到位！这里有一个代码示例展示这个模式：

```python
from typing import Protocol, TypeVar

T = TypeVar("T")

class Repository(Protocol[T]):
    async def get(self, id: str) -> T: ...
    async def save(self, entity: T) -> None: ...
    async def delete(self, id: str) -> None: ...

class InMemoryRepo(Repository):
    def __init__(self):
        self._store: dict[str, object] = {}

    async def get(self, id: str):
        return self._store.get(id)

    async def save(self, entity):
        self._store[entity.id] = entity
```

这种模式让代码既**易于测试**又**灵活可扩展**。""",

    """这个方案很扎实！总结一下：

1. [x] 清晰的关注点分离
2. [x] 合理使用依赖注入
3. [x] 完善的错误处理

一个建议：在合适的场景下使用 `asyncio.gather()` 并行执行，可以将总响应时间降低 30-40%。

继续加油！""",

    """让我更仔细地想想这个问题……

从宏观角度来看，有三个相互制衡的关注点：

1. **效率** — 解决方案的速度和资源消耗如何？
2. **可维护性** — 代码是否易于理解和修改？
3. **正确性** — 是否能正确处理所有边界情况？

最好的方案需要平衡这三点。我的建议是：

- 从简单正确的实现开始
- 先测量性能再优化
- 当模式浮现时及时重构

你觉得这个思路怎么样？""",

]


class MockChatModel(BaseChatModel):
    """A mock chat model that returns pre-written responses.

    Simulates LLM behavior with:
    - Rotating through diverse response patterns
    - Configurable delay to mimic network latency
    - Streaming output (character-by-character)

    Usage:
        model = MockChatModel(delay_range=(0.5, 1.5))
        response = await model.generate(messages)
    """

    def __init__(
        self,
        delay_range: tuple[float, float] = (0.3, 1.0),
        responses: Optional[list[str]] = None,
    ):
        """Initialize the mock model.

        Args:
            delay_range: (min, max) seconds of simulated delay.
            responses: Custom response list (defaults to MOCK_RESPONSES).
        """
        self._delay_range = delay_range
        self._responses = responses or MOCK_RESPONSES
        self._index = 0

    def _next_response(self) -> str:
        """Get the next response in rotation."""
        response = self._responses[self._index % len(self._responses)]
        self._index += 1
        return response

    async def _simulate_delay(self) -> None:
        """Simulate a random processing delay."""
        delay = random.uniform(*self._delay_range)
        await asyncio.sleep(delay)

    async def generate(self, messages: list[Message]) -> str:
        """Generate a complete response synchronously.

        Args:
            messages: Conversation history (unused by mock).

        Returns:
            A random markdown-formatted response.
        """
        await self._simulate_delay()

        # 检测特定关键词并给出相应回复
        last_message = messages[-1].content if messages else ""
        if any(w in last_message.lower() for w in ["你好", "hello", "hi", "嗨"]):
            return "你好！有什么可以帮你的吗？"

        if any(w in last_message.lower() for w in ["再见", "bye", "拜拜"]):
            return "再见！随时欢迎回来。:)"

        return self._next_response()

    async def generate_stream(
        self, messages: list[Message]
    ) -> AsyncIterator[StreamEvent]:
        """Generate a streaming response character-by-character.

        Args:
            messages: Conversation history.

        Yields:
            StreamEvent objects as text arrives.
        """
        # Simulate initial delay
        await asyncio.sleep(random.uniform(0.1, 0.3))

        response = self._next_response()

        # Stream character by character (with batched pauses for realism)
        chars = list(response)
        batch_size = random.randint(1, 5)

        for i in range(0, len(chars), batch_size):
            batch = "".join(chars[i : i + batch_size])
            yield StreamEvent(type="text_delta", content=batch)

            # Variable pause between batches
            if random.random() < 0.1:
                await asyncio.sleep(random.uniform(0.01, 0.05))

        # Final event
        yield StreamEvent(type="done", content=response)
