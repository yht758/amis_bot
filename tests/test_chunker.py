"""
分片器测试 —— 覆盖代码块/URL 保护、硬切、段数上限与边界感知延迟。

运行：python tests/test_chunker.py
"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libs"))

from im_bridge.core.chunker import (  # noqa: E402
    ChunkStreamer,
    boundary_of,
    next_delay,
    protected_spans,
    split_chunks,
)


def squash(text: str) -> str:
    """去掉所有空白，用于校验分片拼接后与原文一致。"""
    return "".join(text.split())


class ProtectedSpanTest(unittest.TestCase):
    def test_code_fence_span(self) -> None:
        text = "看这个```print(1)\nprint(2)```好"
        spans = protected_spans(text)
        self.assertTrue(spans)
        # 代码块内部的某个位置必须在保护区内
        inner = text.index("print(1)")
        self.assertTrue(any(s < inner < e for s, e in spans))

    def test_unclosed_fence_protects_tail(self) -> None:
        """流式未闭合的代码块：从 ``` 到末尾（含末尾切点）全部保护。"""
        text = "开始```python\nprint('hi')"
        spans = protected_spans(text)
        self.assertTrue(any(s == text.index("```") and e >= len(text) for s, e in spans))

    def test_url_span(self) -> None:
        text = "看 https://example.com/a?b=1 这个"
        spans = protected_spans(text)
        inside = text.index("example")
        self.assertTrue(any(s < inside < e for s, e in spans))

    def test_inline_code_span(self) -> None:
        text = "执行 `print(1)` 就行"
        spans = protected_spans(text)
        inside = text.index("print")
        self.assertTrue(any(s < inside < e for s, e in spans))


class CodeBlockSafetyTest(unittest.TestCase):
    def test_code_block_not_split_by_punctuation(self) -> None:
        text = "看这个：```python\nprint('hi。there!')\n```\n就这样。"
        chunks = split_chunks(text, min_len=4, max_len=120)
        self.assertEqual(squash("".join(chunks)), squash(text))
        # 代码块内的句号/感叹号不应成为切点：完整代码块必须落在同一片里
        for chunk in chunks:
            self.assertEqual(
                chunk.count("```") % 2,
                0,
                f"代码块被切断了：{chunk!r}",
            )

    def test_code_block_streaming_character_by_character(self) -> None:
        """逐字符喂入也不能把代码块切开。"""
        text = "前面```js\nconst a = 1;。\n```后面。"
        streamer = ChunkStreamer(min_len=2, max_len=200)
        out: list[str] = []
        for char in text:
            out.extend(streamer.feed(char))
        out.extend(streamer.flush())
        self.assertEqual(squash("".join(out)), squash(text))
        for chunk in out:
            self.assertEqual(chunk.count("```") % 2, 0, f"代码块被切断：{chunk!r}")

    def test_url_not_split(self) -> None:
        text = "地址 https://example.com/path?a=1&b=2 。后面继续说话"
        chunks = split_chunks(text, min_len=4, max_len=200)
        joined = "".join(chunks)
        self.assertIn("https://example.com/path?a=1&b=2", joined)


class HardCutTest(unittest.TestCase):
    def test_long_text_without_punctuation(self) -> None:
        text = "啊" * 120
        chunks = split_chunks(text, min_len=4, max_len=30)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 30 for chunk in chunks))
        self.assertEqual(squash("".join(chunks)), squash(text))

    def test_short_text_waits(self) -> None:
        """内容不足 max_len 且无标点时不应切，避免碎句。"""
        streamer = ChunkStreamer(min_len=4, max_len=40)
        self.assertEqual(streamer.feed("还没说完"), [])
        self.assertEqual(streamer.pending, "还没说完")


class MaxSegmentsTest(unittest.TestCase):
    def test_segments_capped(self) -> None:
        text = "。".join(["这是第%d句话" % i for i in range(1, 21)]) + "。"
        chunks = split_chunks(text, min_len=2, max_len=40, max_segments=3)
        self.assertLessEqual(len(chunks), 4, f"段数未受限：{len(chunks)}")
        # 内容不能丢
        self.assertEqual(squash("".join(chunks)), squash(text))

    def test_zero_means_unlimited(self) -> None:
        text = "。".join(["这是第%d句话" % i for i in range(1, 11)]) + "。"
        chunks = split_chunks(text, min_len=2, max_len=40, max_segments=0)
        self.assertGreater(len(chunks), 4)


class BoundaryTest(unittest.TestCase):
    def test_boundary_kinds(self) -> None:
        self.assertEqual(boundary_of("你好。"), "sentence")
        self.assertEqual(boundary_of("你好！"), "sentence")
        self.assertEqual(boundary_of("你好，"), "clause")
        self.assertEqual(boundary_of("你好\n"), "newline")
        self.assertEqual(boundary_of("你好"), "hard")

    def test_delay_scales_with_boundary(self) -> None:
        rng = random.Random(42)
        sentence = [next_delay(1.0, 2.0, rng, "sentence") for _ in range(50)]
        clause = [next_delay(1.0, 2.0, rng, "clause") for _ in range(50)]
        hard = [next_delay(1.0, 2.0, rng, "hard") for _ in range(50)]
        newline = [next_delay(1.0, 2.0, rng, "newline") for _ in range(50)]

        self.assertTrue(all(1.0 <= value <= 2.0 for value in sentence))
        self.assertTrue(all(0.6 <= value <= 1.2 for value in clause))
        self.assertTrue(all(0.4 <= value <= 0.8 for value in hard))
        self.assertGreater(sum(newline) / 50, sum(sentence) / 50)
        self.assertGreater(sum(sentence) / 50, sum(clause) / 50)
        self.assertTrue(all(value >= 0 for value in sentence + clause + hard + newline))

    def test_zero_range_returns_zero(self) -> None:
        self.assertEqual(next_delay(0.0, 0.0), 0.0)


class BackwardCompatibilityTest(unittest.TestCase):
    def test_simple_sentence_split(self) -> None:
        chunks = split_chunks("你好呀。今天过得怎么样？我有点想你。")
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0], "你好呀。")

    def test_sent_count_property(self) -> None:
        streamer = ChunkStreamer(min_len=2, max_len=40)
        streamer.feed("第一句。第二句。")
        self.assertEqual(streamer.sent_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
