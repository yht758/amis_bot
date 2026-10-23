"""
拟真聊天分片器。

把 LLM 输出按中文/英文标点切成若干短消息，模拟真人“一句一句打字”的节奏。
核心类 ``ChunkStreamer`` 支持增量输入：LLM 每吐出一个 delta，就可以立刻拿到
已经可以发送的完整片段，从而实现真正的流式分片。

与早期版本的差异（Issue #1）：
- **保护区**：代码块、行内代码与 URL 内部绝不切开，流式中未闭合的代码块也整段保护；
- **切点优先级**：句末标点 > 逗号/分号 > 换行 > 长度硬上限，硬切同样避开保护区；
- **段数上限**：``max_segments`` 达到后剩余内容合并为最后一片，避免刷屏；
- **边界感知延迟**：``next_delay(..., boundary=...)`` 让句末停顿明显长于逗号。
"""
from __future__ import annotations

import random
import re

# 句末标点：切成独立一条消息
SENTENCE_MARKS = "。！？!?…～~"
# 句中停顿：也允许成段，但优先级低于句末标点
CLAUSE_MARKS = "，,、；;：:"
# 连续标点、右引号、右括号应跟随前一段，不单独成段
TRAILING_MARKS = SENTENCE_MARKS + CLAUSE_MARKS + "”’\"'）)】」』]"

MIN_CHUNK_LEN = 4
MAX_CHUNK_LEN = 60

# 受保护片段：内部不可切分
_CODE_FENCE = re.compile(r"```")
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_URL = re.compile(r"https?://[^\s，。！？、；：）】》\"']+")

# 不同边界之后的延迟系数：换行最久（换段），逗号最短（喘口气）
BOUNDARY_FACTOR = {
    "sentence": 1.0,
    "newline": 1.15,
    "clause": 0.6,
    "hard": 0.4,
}


def protected_spans(text: str) -> list[tuple[int, int]]:
    """返回不可切分的字符区间 ``[start, end)``。

    包含三种情况：
    - 代码块 ```...```；流式中未闭合时，从开头的 ``` 一直到文本末尾都算保护；
    - 行内代码 `...`；
    - URL（http/https 开头，直到空白或中文标点）。
    """
    spans: list[tuple[int, int]] = []

    fences = [match.start() for match in _CODE_FENCE.finditer(text)]
    for index in range(0, len(fences) - 1, 2):
        spans.append((fences[index], fences[index + 1] + 3))
    if len(fences) % 2 == 1:  # 未闭合：尾部整段保护
        spans.append((fences[-1], len(text)))

    for pattern in (_INLINE_CODE, _URL):
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end()))

    # 片段一直延伸到 buffer 末尾时，说明它在流式输入中可能还没结束：
    # 把区间末尾再向外扩一格，使「切在末尾」也被判定为内部。
    return [
        (start, end + 1 if end == len(text) else end)
        for start, end in spans
    ]


def _inside(pos: int, spans: list[tuple[int, int]]) -> bool:
    """切点 ``pos``（在该下标前切开）是否落在某个保护区内部。"""
    return any(start < pos < end for start, end in spans)


def boundary_of(text: str) -> str:
    """根据分片结尾判断它是被什么边界切出来的。

    返回值：``sentence`` / ``clause`` / ``newline`` / ``hard``。
    """
    raw = text or ""
    if not raw:
        return "hard"
    if raw[-1] == "\n":
        return "newline"
    tail = raw.rstrip()
    if not tail:
        return "newline"
    if tail[-1] in SENTENCE_MARKS:
        return "sentence"
    if tail[-1] in CLAUSE_MARKS:
        return "clause"
    return "hard"


class ChunkStreamer:
    """增量文本分片器。

    用法::

        streamer = ChunkStreamer(max_segments=8)
        for delta in deltas:
            for chunk in streamer.feed(delta):
                await send(chunk)
        for chunk in streamer.flush():
            await send(chunk)
    """

    def __init__(
        self,
        min_len: int = MIN_CHUNK_LEN,
        max_len: int = MAX_CHUNK_LEN,
        max_segments: int = 0,
    ):
        self.min_len = max(2, int(min_len))
        self.max_len = max(self.min_len, int(max_len))
        self.max_segments = max(0, int(max_segments))
        self._buffer = ""
        self._sent = 0

    @property
    def pending(self) -> str:
        return self._buffer

    @property
    def sent_count(self) -> int:
        """已经切出的片数（不含尚未 flush 的 buffer）。"""
        return self._sent

    def feed(self, delta: str) -> list[str]:
        """喂入一段增量文本，返回可直接发送的完整分片列表。"""
        if not delta:
            return []
        self._buffer += delta
        return self._drain(final=False)

    def flush(self) -> list[str]:
        """结束流式输入，把剩余内容作为最后一片输出。"""
        return self._drain(final=True)

    def _drain(self, final: bool) -> list[str]:
        chunks: list[str] = []
        while self._buffer:
            # 达到段数上限：流式过程中先把剩余内容攒着，等 flush 时一次发出，
            # 否则每来一批 delta 都会单独成条，等于没限制
            if self.max_segments and self._sent >= self.max_segments:
                if not final:
                    break
                rest = self._buffer.strip()
                self._buffer = ""
                if rest:
                    chunks.append(rest)
                    self._sent += 1
                break

            if final:
                cut = len(self._buffer)
            else:
                cut = self._find_cut()
                if cut <= 0:
                    break

            chunk = self._buffer[:cut].strip()
            self._buffer = self._buffer[cut:]
            if chunk:
                chunks.append(chunk)
                self._sent += 1
        return chunks

    def _find_cut(self) -> int:
        """返回当前 buffer 中第一个可切位置（切点下标）；0 表示继续等待。"""
        length = len(self._buffer)
        if not length:
            return 0
        spans = protected_spans(self._buffer)

        for index, char in enumerate(self._buffer):
            if char == "\n":
                end = index + 1
            elif char in SENTENCE_MARKS or char in CLAUSE_MARKS:
                end = index + 1
                # 吞并连续标点与右引号/括号，例如 "？！"、"。”"
                while end < length and self._buffer[end] in TRAILING_MARKS:
                    end += 1
            else:
                continue

            if end < self.min_len:
                # 片段太短：跳过当前标点，继续找更长的切点
                continue
            if end >= self.max_len:
                # 单句过长：退化为硬切，但仍要避开保护区
                return self._hard_cut(spans, length)
            if _inside(end, spans):
                # 切点落在代码块/URL 内部，跳过
                continue
            return end

        # 全程没有可用标点：按最大长度硬切，避免无限等待
        return self._hard_cut(spans, length)

    def _hard_cut(self, spans: list[tuple[int, int]], length: int) -> int:
        """在 ``max_len`` 附近找一个不在保护区内的切点。"""
        if length < self.max_len:
            return 0
        cut = self.max_len
        while cut < length and _inside(cut, spans):
            cut += 1
        if cut < length:
            return cut
        # 保护区吞掉了整个窗口：只有文本确实过长时才强行切断
        return self.max_len if length >= self.max_len * 3 else 0


def split_chunks(
    text: str,
    min_len: int = MIN_CHUNK_LEN,
    max_len: int = MAX_CHUNK_LEN,
    max_segments: int = 0,
) -> list[str]:
    """把一整段文本按标点切片（非流式场景使用）。"""
    streamer = ChunkStreamer(min_len=min_len, max_len=max_len, max_segments=max_segments)
    chunks = streamer.feed(text)
    chunks.extend(streamer.flush())
    return [chunk for chunk in chunks if chunk]


def next_delay(
    min_delay: float,
    max_delay: float,
    rng: random.Random | None = None,
    boundary: str = "sentence",
) -> float:
    """返回两次发送之间的人味延迟（秒）。

    先在实际配置的 ``[min_delay, max_delay]`` 内随机，再按 ``boundary`` 加权：
    句末停顿最久，换行更长，逗号与硬切更短。结果不小于 0。
    """
    low = max(0.0, float(min_delay))
    high = max(low, float(max_delay))
    if high <= 0:
        return 0.0
    generator = rng or random
    base = generator.uniform(low, high)
    return max(0.0, base * BOUNDARY_FACTOR.get(boundary, 1.0))
