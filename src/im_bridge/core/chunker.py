"""
拟真聊天分片器。

把 LLM 输出按中文/英文标点切成若干短消息，模拟真人“一句一句打字”的节奏。
核心类 ``ChunkStreamer`` 支持增量输入：LLM 每吐出一个 delta，就可以立刻拿到
已经可以发送的完整片段，从而实现真正的流式分片。
"""
from __future__ import annotations

import random

# 句末标点：切成独立一条消息
SENTENCE_MARKS = "。！？!?…～~"
# 句中停顿：也允许成段，但优先级低于句末标点（流式时按出现顺序切）
CLAUSE_MARKS = "，,、；;：:"
# 连续标点、右引号、右括号应跟随前一段，不单独成段
TRAILING_MARKS = SENTENCE_MARKS + CLAUSE_MARKS + "”’\"'）)】」』]"

MIN_CHUNK_LEN = 4
MAX_CHUNK_LEN = 60


class ChunkStreamer:
    """增量文本分片器。

    用法::

        streamer = ChunkStreamer()
        for delta in deltas:
            for chunk in streamer.feed(delta):
                await send(chunk)
        for chunk in streamer.flush():
            await send(chunk)
    """

    def __init__(self, min_len: int = MIN_CHUNK_LEN, max_len: int = MAX_CHUNK_LEN):
        self.min_len = max(2, int(min_len))
        self.max_len = max(self.min_len, int(max_len))
        self._buffer = ""

    @property
    def pending(self) -> str:
        return self._buffer

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
        return chunks

    def _find_cut(self) -> int:
        """返回当前 buffer 中第一个可切位置（切点下标）；0 表示继续等待。"""
        length = len(self._buffer)
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

            if end >= self.min_len:
                # 单个句子过长时先按最大长度硬切，避免一次发出超长消息
                return min(end, self.max_len)
            # 片段太短：跳过当前标点继续找更长的切点

        # 长时间没有标点：按最大长度硬切，避免无限等待
        if length >= self.max_len:
            return self.max_len
        return 0


def split_chunks(text: str, min_len: int = MIN_CHUNK_LEN, max_len: int = MAX_CHUNK_LEN) -> list[str]:
    """把一整段文本按标点切片（非流式场景使用）。"""
    streamer = ChunkStreamer(min_len=min_len, max_len=max_len)
    chunks = streamer.feed(text)
    chunks.extend(streamer.flush())
    return [chunk for chunk in chunks if chunk]


def next_delay(min_delay: float, max_delay: float, rng: random.Random | None = None) -> float:
    """返回两次发送之间的人味延迟（秒）。"""
    low = max(0.0, float(min_delay))
    high = max(low, float(max_delay))
    if high <= 0:
        return 0.0
    generator = rng or random
    return generator.uniform(low, high)
