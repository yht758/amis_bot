"""
主动会话调度器测试 —— 全离线（假时钟 / 假 LLM / 假 Transport）。

运行：D:\\爱弥斯_bot\\.build\\venv\\Scripts\\python.exe tests/test_proactive.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
# 项目自带的离线依赖（httpx 等），与其他测试保持一致
sys.path.insert(0, str(ROOT / "libs"))

from im_bridge.config import Config  # noqa: E402
from im_bridge.core.context import ContextManager  # noqa: E402
from im_bridge.core.llm import LLMError  # noqa: E402
from im_bridge.core.proactive import (  # noqa: E402
    COOLDOWN_SECONDS,
    MAX_OPENER_CHARS,
    ProactiveScheduler,
    clean_opener,
    in_quiet_hours,
    parse_quiet_hours,
)
from im_bridge.core.state import CST, StateManager  # noqa: E402

# 测试用的固定「今天」：2026-05-10
DAY = 10


def beijing_ts(hour: int, minute: int = 0, day: int = DAY) -> float:
    """构造某个北京时间点的时间戳。"""
    return datetime(2026, 5, day, hour, minute, tzinfo=CST).timestamp()


class FakeClock:
    """可推进的假时钟。"""

    def __init__(self, ts: float) -> None:
        self.ts = ts

    def __call__(self) -> float:
        return self.ts

    def advance(self, seconds: float) -> None:
        self.ts += seconds


class FakeLLM:
    """假 LLM：按 system prompt 里的昵称决定成功还是抛异常。"""

    def __init__(self, text: str = "早呀，今天有好好吃饭吗？", fail_for: set[str] | None = None) -> None:
        self.text = text
        self.fail_for = set(fail_for or ())
        self.calls = 0

    async def complete(self, messages, **kwargs) -> str:
        self.calls += 1
        system = str(messages[0].get("content", "")) if messages else ""
        for nickname in self.fail_for:
            if nickname in system:
                raise LLMError("假 LLM 故障")
        return self.text


class FakeTransport:
    """假传输层：记录发出的消息。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.error = error

    async def send_text(self, user_id: int, text: str) -> None:
        if self.error:
            raise self.error
        self.sent.append((user_id, text))


class ProactiveSchedulerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="proactive_test_"))
        self.state = StateManager(self.tmpdir)
        self.context = ContextManager()
        self.transport = FakeTransport()
        self.llm = FakeLLM()
        self.clock = FakeClock(beijing_ts(12, 0))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---------- 工具 ----------
    def make_config(self, **overrides) -> Config:
        params = {
            "proactive_enabled": True,
            "proactive_idle_hours": 24.0,
            "proactive_max_per_day": 1,
            "proactive_quiet_hours": "23:00-08:00",
        }
        params.update(overrides)
        return Config(**params)

    def add_user(self, user_id: str, last_interact_ts: float, nickname: str = "") -> None:
        state = self.state.get(user_id)
        state.last_interact_ts = last_interact_ts
        state.nickname = nickname

    def make_scheduler(self, **overrides) -> ProactiveScheduler:
        return ProactiveScheduler(
            config=self.make_config(**overrides),
            llm=self.llm,
            context=self.context,
            state=self.state,
            transport=self.transport,
            clock=self.clock,
            loop_interval=3600.0,
        )

    # ---------- 开关 ----------
    async def test_disabled_does_not_send(self) -> None:
        self.add_user("10001", beijing_ts(12, 0) - 100 * 3600, "小满")
        scheduler = self.make_scheduler(proactive_enabled=False)

        result = await scheduler.tick()

        self.assertEqual(result, [])
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.llm.calls, 0)

    # ---------- 闲置阈值 ----------
    async def test_not_idle_enough_does_not_send(self) -> None:
        self.add_user("10001", beijing_ts(12, 0) - 5 * 3600, "小满")
        scheduler = self.make_scheduler(proactive_idle_hours=24.0)

        result = await scheduler.tick()

        self.assertEqual(result, [])
        self.assertEqual(self.transport.sent, [])

    async def test_user_without_interaction_skipped(self) -> None:
        self.add_user("10001", 0.0, "小满")
        scheduler = self.make_scheduler()

        self.assertEqual(await scheduler.tick(), [])

    # ---------- 静默时段（跨零点） ----------
    async def test_quiet_hours_cross_midnight_blocks_late_night(self) -> None:
        self.add_user("10001", beijing_ts(12, 0) - 100 * 3600, "小满")
        self.clock.ts = beijing_ts(23, 30)
        scheduler = self.make_scheduler(proactive_max_per_day=3)

        self.assertEqual(await scheduler.tick(), [])
        self.assertEqual(self.transport.sent, [])

    async def test_quiet_hours_cross_midnight_blocks_early_morning(self) -> None:
        self.add_user("10001", beijing_ts(12, 0) - 100 * 3600, "小满")
        self.clock.ts = beijing_ts(7, 30)
        scheduler = self.make_scheduler(proactive_max_per_day=3)

        self.assertEqual(await scheduler.tick(), [])
        self.assertEqual(self.transport.sent, [])

    async def test_quiet_hours_boundary_allows_daytime(self) -> None:
        """08:00 已出静默区间，同一用户应可发送（对照上面两个用例）。"""
        self.add_user("10001", beijing_ts(12, 0) - 100 * 3600, "小满")
        self.clock.ts = beijing_ts(8, 0)
        scheduler = self.make_scheduler()

        self.assertEqual(await scheduler.tick(), ["10001"])
        self.assertEqual(len(self.transport.sent), 1)

    async def test_empty_quiet_hours_means_no_restriction(self) -> None:
        self.add_user("10001", beijing_ts(12, 0) - 100 * 3600, "小满")
        self.clock.ts = beijing_ts(23, 30)
        scheduler = self.make_scheduler(proactive_quiet_hours="")

        self.assertEqual(await scheduler.tick(), ["10001"])

    # ---------- 每日上限（北京时间自然日） ----------
    async def test_daily_limit_blocks_second_send(self) -> None:
        self.add_user("10001", beijing_ts(9, 0) - 48 * 3600, "小满")
        self.clock.ts = beijing_ts(9, 0)
        scheduler = self.make_scheduler(proactive_max_per_day=1)

        self.assertEqual(await scheduler.tick(), ["10001"])

        # 跨过 6 小时冷却，但仍在北京时间同一天 -> 被每日上限拦住
        self.clock.advance(7 * 3600)
        self.assertEqual(await scheduler.tick(), [])
        self.assertEqual(len(self.transport.sent), 1)

    async def test_daily_limit_resets_next_beijing_day(self) -> None:
        self.add_user("10001", beijing_ts(9, 0) - 48 * 3600, "小满")
        self.clock.ts = beijing_ts(22, 0)
        scheduler = self.make_scheduler(proactive_max_per_day=1)
        self.assertEqual(await scheduler.tick(), ["10001"])

        # 次日 09:00（已出静默时段、已过冷却）-> 计数重置，可再发
        self.clock.ts = beijing_ts(9, 0, day=DAY + 1)
        self.assertEqual(await scheduler.tick(), ["10001"])
        self.assertEqual(len(self.transport.sent), 2)

    async def test_zero_max_per_day_disables_sending(self) -> None:
        self.add_user("10001", beijing_ts(12, 0) - 100 * 3600, "小满")
        scheduler = self.make_scheduler(proactive_max_per_day=0)

        self.assertEqual(await scheduler.tick(), [])

    # ---------- 额外冷却 ----------
    async def test_cooldown_blocks_rapid_repeat(self) -> None:
        self.add_user("10001", beijing_ts(9, 0) - 48 * 3600, "小满")
        self.clock.ts = beijing_ts(9, 0)
        scheduler = self.make_scheduler(proactive_max_per_day=5)

        self.assertEqual(await scheduler.tick(), ["10001"])
        self.clock.advance(COOLDOWN_SECONDS - 60)
        self.assertEqual(await scheduler.tick(), [])
        self.assertEqual(len(self.transport.sent), 1)

    # ---------- 正常触发 ----------
    async def test_normal_trigger_sends_and_writes_context(self) -> None:
        self.add_user("10001", beijing_ts(12, 0) - 30 * 3600, "小满")
        scheduler = self.make_scheduler()

        result = await scheduler.tick()

        self.assertEqual(result, ["10001"])
        self.assertEqual(len(self.transport.sent), 1)
        user_id, text = self.transport.sent[0]
        self.assertEqual(user_id, 10001)          # 传输层收到的是 int
        self.assertIsInstance(user_id, int)
        self.assertEqual(text, "早呀，今天有好好吃饭吗？")

        messages = self.context.get("10001").messages
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "assistant")
        self.assertEqual(messages[0]["content"], text)

    async def test_opener_is_trimmed_to_max_chars(self) -> None:
        self.add_user("10001", beijing_ts(12, 0) - 30 * 3600, "小满")
        self.llm.text = "“" + "很久没聊了，最近是不是又熬夜到很晚才睡呀，记得早点休息哦" * 2 + "”"
        scheduler = self.make_scheduler()

        self.assertEqual(await scheduler.tick(), ["10001"])
        _, text = self.transport.sent[0]
        self.assertLessEqual(len(text), MAX_OPENER_CHARS)
        self.assertTrue(text)
        self.assertFalse(text.startswith("“"))

    # ---------- 失败隔离 ----------
    async def test_llm_error_skips_user_without_affecting_others(self) -> None:
        self.add_user("10001", beijing_ts(12, 0) - 30 * 3600, "阿坏")
        self.add_user("10002", beijing_ts(12, 0) - 30 * 3600, "阿好")
        self.llm.fail_for = {"阿坏"}
        scheduler = self.make_scheduler(proactive_max_per_day=2)

        result = await scheduler.tick()

        self.assertEqual(result, ["10002"])
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.transport.sent[0][0], 10002)
        self.assertEqual(self.context.get("10001").messages, [])
        self.assertEqual(len(self.context.get("10002").messages), 1)

    async def test_transport_error_does_not_raise(self) -> None:
        self.add_user("10001", beijing_ts(12, 0) - 30 * 3600, "小满")
        self.transport.error = RuntimeError("网络断了")
        scheduler = self.make_scheduler()

        self.assertEqual(await scheduler.tick(), [])
        # 发送失败不写上下文
        self.assertEqual(self.context.get("10001").messages, [])

    # ---------- 生命周期 ----------
    async def test_start_is_idempotent_and_stop_reclaims_task(self) -> None:
        self.clock.ts = beijing_ts(3, 0)  # 静默时段内，后台 tick 不会发消息
        self.add_user("10001", beijing_ts(3, 0) - 100 * 3600, "小满")
        scheduler = self.make_scheduler()

        await scheduler.start()
        await scheduler.start()
        self.assertTrue(scheduler.running)

        await scheduler.stop()
        self.assertFalse(scheduler.running)
        await scheduler.stop()  # 重复 stop 无副作用

    async def test_no_send_after_stop(self) -> None:
        self.clock.ts = beijing_ts(3, 0)  # 静默时段内启动，避免后台任务抢先发送
        self.add_user("10001", beijing_ts(3, 0) - 100 * 3600, "小满")
        scheduler = self.make_scheduler()
        await scheduler.start()
        await scheduler.stop()

        self.clock.ts = beijing_ts(12, 0)  # 已离开静默时段
        self.assertEqual(await scheduler.tick(), [])
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.llm.calls, 0)

    # ---------- 纯函数 ----------
    def test_parse_and_match_quiet_hours(self) -> None:
        window = parse_quiet_hours("23:00-08:00")
        self.assertEqual(window, (23 * 60, 8 * 60))
        self.assertTrue(in_quiet_hours(23 * 60 + 30, window))
        self.assertTrue(in_quiet_hours(7 * 60 + 30, window))
        self.assertFalse(in_quiet_hours(8 * 60, window))
        self.assertFalse(in_quiet_hours(12 * 60, window))

        same_day = parse_quiet_hours("12:00-14:00")
        self.assertTrue(in_quiet_hours(13 * 60, same_day))
        self.assertFalse(in_quiet_hours(11 * 60 + 59, same_day))

        self.assertIsNone(parse_quiet_hours(""))
        self.assertIsNone(parse_quiet_hours("乱写"))
        self.assertIsNone(parse_quiet_hours("25:00-08:00"))

    def test_clean_opener(self) -> None:
        self.assertEqual(clean_opener("  你好呀  "), "你好呀")
        self.assertEqual(clean_opener("“今天过得怎么样？”"), "今天过得怎么样？")
        self.assertEqual(clean_opener(""), "")
        self.assertEqual(clean_opener("第一行\n第二行解释"), "第一行")


if __name__ == "__main__":
    unittest.main(verbosity=2)
