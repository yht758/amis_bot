"""
核心层离线测试 —— 不依赖真实网络 / 真实 LLM。

运行：python tests/test_core.py
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libs"))

from im_bridge.config import Config  # noqa: E402
from im_bridge.core.chunker import ChunkStreamer, split_chunks  # noqa: E402
from im_bridge.core.context import ContextManager  # noqa: E402
from im_bridge.core.engine import ChatEngine, GENERIC_FALLBACK_TEXT, LLM_FALLBACK_TEXT  # noqa: E402
from im_bridge.core.llm import LLMError  # noqa: E402
from im_bridge.core.state import StateManager  # noqa: E402
from im_bridge.personas import Persona  # noqa: E402
import im_bridge.log_writer as _log_writer  # noqa: E402

# 测试日志写入独立目录，避免污染真实运行日志
_TEST_LOG_DIR = ROOT / "tests" / ".tmp_logs"
_TEST_LOG_DIR.mkdir(parents=True, exist_ok=True)
_log_writer.LOG_DIR = _TEST_LOG_DIR


class FakeLLM:
    """可编程的假 LLM。"""

    def __init__(
        self,
        chunks: list[str] | None = None,
        error: Exception | None = None,
        summarize_error: Exception | None = None,
    ):
        self.chunks = chunks or ["你好呀，", "今天过得怎么样？", "我最近在听歌。"]
        self.error = error
        self.summarize_error = summarize_error
        self.summary_calls = 0

    async def stream(self, messages):
        if self.error:
            raise self.error
        for chunk in self.chunks:
            yield chunk

    async def complete(self, messages, **kwargs):
        if self.error:
            raise self.error
        return "".join(self.chunks)

    async def summarize(self, existing_summary, transcript):
        if self.summarize_error:
            raise self.summarize_error
        self.summary_calls += 1
        return f"摘要({len(transcript)}字)"


class FakeTransport:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_text(self, user_id, text):
        self.sent.append((user_id, text))


def make_config(**overrides) -> Config:
    defaults = dict(
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        model="test-model",
        bot_qq=123456789,
        allowed_users=[],
        admin_users=["999"],
        max_history=40,
        summary_threshold=6,
        summary_keep_recent=2,
        chunk_min_delay=0,
        chunk_max_delay=0,
        streaming_enabled=True,
        chunking_enabled=True,
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_engine(config: Config, llm: FakeLLM) -> tuple[ChatEngine, FakeTransport, StateManager, ContextManager]:
    transport = FakeTransport()
    state = StateManager(ROOT / "tests" / ".tmp_state")
    contexts = ContextManager(
        max_history=config.max_history,
        summary_enabled=config.summary_enabled,
        summary_threshold=config.summary_threshold,
        summary_keep_recent=config.summary_keep_recent,
    )
    engine = ChatEngine(
        config=config,
        llm=llm,
        context_manager=contexts,
        state_manager=state,
        transport=transport,
        persona=Persona(name="test", system_prompt="TEST PROMPT"),
    )
    return engine, transport, state, contexts


class ChunkerTest(unittest.TestCase):
    def test_split_by_punctuation(self):
        chunks = split_chunks("你好呀，今天天气不错。出去走走吗？")
        self.assertEqual(chunks, ["你好呀，", "今天天气不错。", "出去走走吗？"])

    def test_split_long_text(self):
        text = "第一句话在这里呀，第二句话也在这里呢。第三句继续说一下！"
        chunks = split_chunks(text)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual("".join(chunks), text.replace(" ", ""))

    def test_streaming_incremental(self):
        streamer = ChunkStreamer()
        got = []
        for delta in ["你好", "呀，今天", "过得怎么样？", "我最近在听歌。"]:
            got.extend(streamer.feed(delta))
        got.extend(streamer.flush())
        self.assertTrue(got)
        self.assertEqual("".join(got), "你好呀，今天过得怎么样？我最近在听歌。")

    def test_oversized_sentence_is_hard_split(self):
        text = "啊" * 100 + "。"
        chunks = split_chunks(text)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 60 for chunk in chunks))
        self.assertEqual("".join(chunks), text)


class ContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_summary_trigger_and_keep_recent(self):
        contexts = ContextManager(
            max_history=40, summary_enabled=True, summary_threshold=4, summary_keep_recent=2
        )
        llm = FakeLLM()
        for index in range(6):
            contexts.append_user("u1", f"问题{index}")
            contexts.append_assistant("u1", f"回答{index}")
            await contexts.maybe_compress("u1", llm)

        conversation = contexts.get("u1")
        self.assertLessEqual(len(conversation.messages), 2 + 2)
        self.assertTrue(conversation.summary)
        self.assertGreater(llm.summary_calls, 0)

    async def test_build_messages_contains_summary(self):
        contexts = ContextManager(summary_threshold=2, summary_keep_recent=2)
        contexts.append_user("u1", "a")
        contexts.append_assistant("u1", "b")
        contexts.get("u1").summary = "用户喜欢猫"
        messages = contexts.build_messages("u1", "SYSTEM")
        self.assertEqual(messages[0]["content"], "SYSTEM")
        self.assertIn("用户喜欢猫", messages[1]["content"])

    async def test_reset_clears_summary_and_messages(self):
        contexts = ContextManager()
        contexts.append_user("u1", "hi")
        contexts.get("u1").summary = "旧摘要"
        self.assertTrue(contexts.reset("u1"))
        self.assertEqual(contexts.get("u1").summary, "")
        self.assertEqual(contexts.get("u1").messages, [])

    async def test_summary_unexpected_error_falls_back(self):
        contexts = ContextManager(summary_threshold=2, summary_keep_recent=2)
        llm = FakeLLM(summarize_error=RuntimeError("boom"))
        for _ in range(4):
            contexts.append_user("u1", "问题")
            contexts.append_assistant("u1", "回答")
        result = await contexts.maybe_compress("u1", llm)
        self.assertFalse(result)
        self.assertEqual(contexts.get("u1").summary, "")
        self.assertEqual(len(contexts.get("u1").messages), 8)


class EngineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = make_config()
        self.llm = FakeLLM()
        self.engine, self.transport, self.state, self.contexts = make_engine(self.config, self.llm)

    async def test_streaming_chunks_and_context(self):
        stop = await self.engine.handle_message("1001", "测试", "你好")
        self.assertFalse(stop)
        self.assertGreaterEqual(len(self.transport.sent), 1)
        self.assertEqual(self.transport.sent[0][0], 1001)
        reply = "".join(text for _, text in self.transport.sent)
        self.assertIn("今天过得怎么样", reply)
        self.assertEqual(self.contexts.get("1001").messages[-1]["content"], reply)

    async def test_streaming_without_chunking_still_sends_once(self):
        # 回归：STREAMING=true + CHUNKING=false 时必须整段发送一次
        engine, transport, _, contexts = make_engine(
            make_config(chunking_enabled=False), FakeLLM()
        )
        stop = await engine.handle_message("1001", "测试", "你好")
        self.assertFalse(stop)
        self.assertEqual(len(transport.sent), 1)
        self.assertIn("今天过得怎么样", transport.sent[0][1])
        self.assertEqual(len(contexts.get("1001").messages), 2)

    async def test_multi_user_isolation(self):
        await self.engine.handle_message("1001", "甲", "你好")
        await self.engine.handle_message("1002", "乙", "在吗")
        conv1 = self.contexts.get("1001")
        conv2 = self.contexts.get("1002")
        self.assertNotEqual(id(conv1), id(conv2))
        self.assertEqual(len(conv1.messages), 2)
        self.assertEqual(len(conv2.messages), 2)
        self.assertIn("你好", conv1.messages[0]["content"])
        self.assertIn("在吗", conv2.messages[0]["content"])

    async def test_meaningless_input_does_not_crash(self):
        for _ in range(5):
            await self.engine.handle_message("1001", "测试", "1")
        self.assertEqual(len(self.transport.sent), 15)
        # 每轮都应重新生成并回复；上下文受窗口/摘要约束，不会无限膨胀
        conversation = self.contexts.get("1001")
        self.assertGreaterEqual(len(conversation.messages), 2)
        self.assertLessEqual(len(conversation.messages), self.config.max_history + 2)

    async def test_reset_command_clears_only_target(self):
        await self.engine.handle_message("1001", "甲", "你好")
        await self.engine.handle_message("1002", "乙", "你好")
        await self.engine.handle_message("1001", "甲", "/reset")
        self.assertEqual(self.contexts.get("1001").messages, [])
        self.assertEqual(len(self.contexts.get("1002").messages), 2)

    async def test_llm_error_falls_back(self):
        llm = FakeLLM(error=LLMError("boom"))
        engine, transport, _, _ = make_engine(make_config(), llm)
        stop = await engine.handle_message("1001", "测试", "你好")
        self.assertFalse(stop)
        self.assertEqual(transport.sent[-1][1], LLM_FALLBACK_TEXT)

    async def test_unexpected_error_falls_back(self):
        class BrokenTransport(FakeTransport):
            async def send_text(self, user_id, text):
                raise RuntimeError("network down")

        llm = FakeLLM()
        transport = BrokenTransport()
        state = StateManager(ROOT / "tests" / ".tmp_state")
        contexts = ContextManager()
        engine = ChatEngine(make_config(), llm, contexts, state, transport, Persona("t", "P"))
        stop = await engine.handle_message("1001", "测试", "你好")
        self.assertFalse(stop)

    async def test_stop_command(self):
        stop = await self.engine.handle_message("999", "管理员", "/stop")
        self.assertTrue(stop)

    async def test_non_admin_cannot_stop(self):
        stop = await self.engine.handle_message("1001", "普通", "/stop")
        self.assertFalse(stop)
        self.assertEqual(self.transport.sent[-1][1], "你没有权限使用这个命令哦")


class ConfigTest(unittest.TestCase):
    def test_missing_api_key(self):
        errors = make_config(api_key="").validate()
        self.assertTrue(any("API Key" in error for error in errors))

    def test_placeholder_api_key(self):
        errors = make_config(api_key="sk-your-api-key-here").validate()
        self.assertTrue(any("占位符" in error for error in errors))

    def test_invalid_delays(self):
        errors = make_config(chunk_min_delay=3, chunk_max_delay=1).validate()
        self.assertTrue(any("CHUNK_MIN_DELAY" in error for error in errors))

    def test_threshold_must_be_below_max_history(self):
        errors = make_config(max_history=20, summary_threshold=24).validate()
        self.assertTrue(any("AMIS_MAX_HISTORY" in error for error in errors))

    def test_valid_defaults(self):
        self.assertEqual(make_config().validate(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
