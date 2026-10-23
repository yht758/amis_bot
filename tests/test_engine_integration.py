"""
引擎集成测试 —— 验证分片上限、单条长度上限与语音降级。

运行：python tests/test_engine_integration.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libs"))

from im_bridge.config import Config  # noqa: E402
from im_bridge.core.context import ContextManager  # noqa: E402
from im_bridge.core.engine import ChatEngine  # noqa: E402
from im_bridge.core.state import StateManager  # noqa: E402
from im_bridge.personas import load_persona  # noqa: E402


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.records: list[str] = []

    async def send_text(self, user_id: int, text: str) -> None:
        self.sent.append(text)

    async def send_record(self, user_id: int, file: str) -> None:
        self.records.append(file)


class FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def stream(self, messages):
        for char in self.reply:
            yield char

    async def complete(self, messages) -> str:
        return self.reply

    async def summarize(self, existing_summary: str, transcript: str) -> str:
        return "用户喜欢简短回复。"


def build_engine(reply: str, data_dir: Path, **overrides) -> tuple[ChatEngine, FakeTransport]:
    params = dict(
        api_key="sk-test",
        chunk_min_delay=0.0,
        chunk_max_delay=0.0,
        chunking_enabled=True,
        streaming_enabled=True,
    )
    params.update(overrides)
    config = Config(**params)
    transport = FakeTransport()
    engine = ChatEngine(
        config=config,
        llm=FakeLLM(reply),
        context_manager=ContextManager(
            max_history=20, summary_threshold=10, summary_keep_recent=4
        ),
        state_manager=StateManager(data_dir),
        transport=transport,
        persona=load_persona("aemeath"),
    )
    return engine, transport


class LengthLimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_oversize_piece_is_split(self) -> None:
        """单条上限 20 字：回复必须被拆成多条，且每条不超过 20 字。"""
        reply = "啊" * 100
        with tempfile.TemporaryDirectory() as tmp:
            engine, transport = build_engine(
                reply, Path(tmp), message_max_chars=20, chunk_max_segments=0
            )
            await engine.handle_message("10001", "测试", "说点什么")
        self.assertGreater(len(transport.sent), 1)
        self.assertTrue(
            all(len(piece) <= 20 for piece in transport.sent),
            f"存在超限分片：{[len(p) for p in transport.sent]}",
        )
        self.assertEqual("".join(transport.sent), reply)

    async def test_max_segments_caps_burst(self) -> None:
        """段数上限 2：总条数不应超过 3（2 段 + 合并的剩余）。"""
        reply = "。".join(["这是第%d句话" % i for i in range(1, 21)]) + "。"
        with tempfile.TemporaryDirectory() as tmp:
            engine, transport = build_engine(
                reply, Path(tmp), chunk_max_segments=2, message_max_chars=1000
            )
            await engine.handle_message("10002", "测试", "多说几句")
        self.assertLessEqual(len(transport.sent), 3, f"实际 {len(transport.sent)} 条")


class VoiceFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_voice_disabled_sends_nothing(self) -> None:
        """默认关闭语音：不应产生任何语音消息。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine, transport = build_engine("你好呀。", Path(tmp))
            await engine.handle_message("10003", "测试", "在吗")
        self.assertTrue(transport.sent)
        self.assertEqual(transport.records, [])

    async def test_voice_enabled_without_synthesizer(self) -> None:
        """开了语音但没配 TTS：只发文字，不能报错。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine, transport = build_engine(
                "你好呀。", Path(tmp), voice_reply_enabled=True
            )
            await engine.handle_message("10004", "测试", "在吗")
        self.assertTrue(transport.sent)
        self.assertEqual(transport.records, [])


class SegmentDelayTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_delay_and_all_content_delivered(self) -> None:
        """延迟为 0 时也要保证内容完整、顺序正确。"""
        reply = "第一句。第二句。第三句。"
        with tempfile.TemporaryDirectory() as tmp:
            engine, transport = build_engine(reply, Path(tmp))
            await engine.handle_message("10005", "测试", "说三句")
        self.assertEqual("".join(transport.sent), reply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
