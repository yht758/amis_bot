"""
主动会话调度器 —— 在合适时机由 Bot 先开口（Issue #4 的纯逻辑部分）。

职责边界：
- 只决定「该不该发」并生成内容、调用 ``MessageTransport`` 发送；
- 不感知 QQ / OneBot 协议细节，不修改 ``engine.py`` 的聊天主流程；
- 发送成功后把这句话写入 ``ContextManager``，后续对话自然衔接。

触发条件（全部满足才发）：
1. ``config.proactive_enabled`` 为 True；
2. 该用户已存在于 ``StateManager`` 且 ``last_interact_ts > 0``；
3. 闲置时长 ``now - last_interact_ts >= proactive_idle_hours``；
4. 当前北京时间不在 ``proactive_quiet_hours`` 静默区间内（支持跨零点）；
5. 当日（北京时间自然日）主动条数未达 ``proactive_max_per_day``；
6. 距该用户上次触达尝试已超过 ``COOLDOWN_SECONDS``（6 小时）。

失败策略：单个用户失败只跳过该用户，不重试、不影响其他人；
``stop()`` 之后不再发送。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable

from .llm import LLMError
from .state import CST

_MODULE_LOGGER = logging.getLogger("im_bridge.proactive")

# 同一用户两次主动触达的最小间隔（秒）。与每日上限叠加生效，避免连续打扰。
COOLDOWN_SECONDS = 6 * 3600

# 主动开场白的最大字数
MAX_OPENER_CHARS = 30

# 主动开场白的 system prompt 模板
OPENER_SYSTEM_PROMPT = (
    "你正在私聊窗口里主动给对方发一条消息，而不是回复对方。\n"
    "对方昵称：{nickname}\n"
    "下面可能带有你们之前的聊天记录，可以用来自然接上话题，"
    "但绝对不要复述、总结或引用这些记录。\n"
    "要求：\n"
    "1. 只输出一句中文口语，最多 {max_chars} 个字；\n"
    "2. 像真人随手发出的一条消息：具体、自然，可以问候近况、"
    "关心一句，或轻轻提起上次聊到的小事；\n"
    "3. 不要像群发消息，不要客套开场白，不要一次问一堆问题；\n"
    "4. 不要提到自己是 AI、机器人或助手，不要说“我注意到”“根据记录”这类话；\n"
    "5. 不要用引号包裹，不要解释，不要输出多余内容。"
)

# 附加在消息列表末尾的引导（不会写入上下文）
OPENER_NUDGE = "（现在由你先开口。请直接输出你要发出去的那一句话。）"

# 空字符串/格式非法时的兜底昵称
_FALLBACK_NICKNAME = "对方"


def parse_quiet_hours(text: str) -> tuple[int, int] | None:
    """解析 ``"23:00-08:00"`` 形式的静默时段。

    返回 ``(开始分钟数, 结束分钟数)``；留空、格式非法或首尾相同都返回 ``None``
    （表示不做静默限制）。
    """
    raw = (text or "").strip()
    if not raw or "-" not in raw:
        return None
    start_raw, end_raw = raw.split("-", 1)
    try:
        start_hour, start_minute = _parse_hhmm(start_raw)
        end_hour, end_minute = _parse_hhmm(end_raw)
    except ValueError:
        _MODULE_LOGGER.warning("静默时段格式非法，忽略限制：%r", text)
        return None

    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start == end:
        return None
    return start, end


def in_quiet_hours(minute_of_day: int, window: tuple[int, int] | None) -> bool:
    """判断一天中的第几分钟是否落在静默区间内（支持跨零点）。"""
    if window is None:
        return False
    start, end = window
    if start < end:
        return start <= minute_of_day < end
    # 跨零点，例如 23:00-08:00
    return minute_of_day >= start or minute_of_day < end


def _parse_hhmm(text: str) -> tuple[int, int]:
    parts = text.strip().split(":")
    if len(parts) != 2:
        raise ValueError(text)
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(text)
    return hour, minute


def clean_opener(text: str) -> str:
    """把模型输出清洗成一条可直接发送的短句。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    # 只取第一行，避免模型输出多行或多句解释
    cleaned = cleaned.splitlines()[0].strip()
    # 去掉成对/残留的引号
    cleaned = cleaned.strip("“”\"'「」『』 ")
    if not cleaned:
        return ""
    if len(cleaned) > MAX_OPENER_CHARS:
        cleaned = cleaned[:MAX_OPENER_CHARS].rstrip("，,、；;：:。.!！?？ 　")
    return cleaned.strip()


class ProactiveScheduler:
    """按固定间隔扫描用户状态，在合适时机主动发一条短消息。"""

    def __init__(
        self,
        config: Any,
        llm: Any,
        context: Any,
        state: Any,
        transport: Any,
        clock: Callable[[], float] | None = None,
        logger: logging.Logger | None = None,
        loop_interval: float = 300.0,
    ) -> None:
        self._config = config
        self._llm = llm
        self._context = context
        self._state = state
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(CST).timestamp())
        self._logger = logger or _MODULE_LOGGER
        self.loop_interval = max(0.01, float(loop_interval))

        self._task: asyncio.Task | None = None
        self._closed = False

        # 记账：用户 -> 上次触达尝试时间戳（成功或失败都会占用冷却）
        self._last_attempt: dict[str, float] = {}
        # 记账：用户 -> (北京日期 YYYY-MM-DD, 当日已发条数)
        self._daily: dict[str, tuple[str, int]] = {}

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        """启动后台循环；重复调用无副作用。"""
        if self._task is not None and not self._task.done():
            return
        self._closed = False
        self._task = asyncio.create_task(self._run(), name="proactive-scheduler")
        self._logger.info("主动会话调度器已启动（间隔 %.0fs）", self.loop_interval)

    async def stop(self) -> None:
        """取消后台循环并等待回收；重复调用无副作用。"""
        self._closed = True
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - 关闭流程不应抛出
            self._logger.warning("主动会话调度器退出时出现异常：%s", exc)
        self._logger.info("主动会话调度器已停止")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        """后台循环：一轮扫描 + 固定间隔休眠。"""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 循环不能因单轮异常退出
                self._logger.warning("主动会话扫描出现异常：%s", exc)
            await asyncio.sleep(self.loop_interval)

    # ---------- 单轮扫描 ----------
    async def tick(self) -> list[str]:
        """执行一轮扫描，返回这一轮成功发出的 ``user_id`` 列表。"""
        if self._closed:
            return []
        if not bool(getattr(self._config, "proactive_enabled", False)):
            return []

        now = float(self._clock())
        quiet = str(getattr(self._config, "proactive_quiet_hours", "") or "")
        window = parse_quiet_hours(quiet)
        if in_quiet_hours(self._minute_of_day(now), window):
            self._logger.debug("当前处于静默时段 %s，跳过主动会话", window)
            return []

        max_per_day = int(getattr(self._config, "proactive_max_per_day", 1) or 0)
        if max_per_day <= 0:
            return []
        idle_hours = float(getattr(self._config, "proactive_idle_hours", 24.0) or 0.0)
        idle_seconds = max(0.0, idle_hours * 3600)

        sent: list[str] = []
        for user_id in self._existing_user_ids():
            if self._closed:
                break
            state = self._state.get(user_id)
            if state is None:
                continue
            last_interact = float(getattr(state, "last_interact_ts", 0.0) or 0.0)
            if last_interact <= 0:
                continue
            if now - last_interact < idle_seconds:
                continue

            last_attempt = self._last_attempt.get(user_id)
            if last_attempt is not None and now - last_attempt < COOLDOWN_SECONDS:
                continue
            if self._daily_count(user_id, now) >= max_per_day:
                continue

            if await self._reach_out(user_id, state, now):
                sent.append(user_id)
        return sent

    # ---------- 单用户触达 ----------
    async def _reach_out(self, user_id: str, state: Any, now: float) -> bool:
        """给单个用户发一条主动消息；任何失败都返回 False 且不抛异常。"""
        try:
            platform_id = int(user_id)
        except (TypeError, ValueError):
            self._logger.warning("用户 ID 不是数字，跳过主动会话：%r", user_id)
            return False

        # 先占冷却：失败也不在每个 tick 重复重试
        self._last_attempt[user_id] = now

        try:
            text = await self._generate_opener(user_id, state)
        except asyncio.CancelledError:
            raise
        except LLMError as exc:
            self._logger.warning("生成主动开场白失败（用户 %s），本轮跳过：%s", user_id, exc)
            return False
        except Exception as exc:  # noqa: BLE001 - 单用户异常不影响其他用户
            self._logger.warning("生成主动开场白出现异常（用户 %s），本轮跳过：%s", user_id, exc)
            return False

        if not text:
            self._logger.warning("主动开场白为空（用户 %s），本轮跳过", user_id)
            return False
        if self._closed:
            return False

        try:
            await self._transport.send_text(platform_id, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 发送失败同样只跳过该用户
            self._logger.warning("主动消息发送失败（用户 %s）：%s", user_id, exc)
            return False

        self._bump_daily(user_id, now)
        self._context.append_assistant(user_id, text)
        self._logger.info("已向用户 %s 主动发送：%s", user_id, text)
        return True

    async def _generate_opener(self, user_id: str, state: Any) -> str:
        nickname = str(getattr(state, "nickname", "") or "").strip() or _FALLBACK_NICKNAME
        system_prompt = OPENER_SYSTEM_PROMPT.format(
            nickname=nickname, max_chars=MAX_OPENER_CHARS
        )
        messages = self._context.build_messages(user_id, system_prompt)
        messages.append({"role": "user", "content": OPENER_NUDGE})
        raw = await self._llm.complete(messages)
        return clean_opener(raw)

    # ---------- 内部工具 ----------
    def _existing_user_ids(self) -> list[str]:
        """列出 ``StateManager`` 中已存在的用户。

        ``StateManager`` 目前没有公开的遍历接口，这里读取其内部映射；
        只读、不调用 ``get()``，因此不会凭空创建用户。
        """
        states = getattr(self._state, "_states", None)
        if not isinstance(states, dict):
            return []
        return list(states.keys())

    @staticmethod
    def _minute_of_day(now: float) -> int:
        beijing = datetime.fromtimestamp(now, CST)
        return beijing.hour * 60 + beijing.minute

    @staticmethod
    def _day_key(now: float) -> str:
        return datetime.fromtimestamp(now, CST).strftime("%Y-%m-%d")

    def _daily_count(self, user_id: str, now: float) -> int:
        entry = self._daily.get(user_id)
        if not entry or entry[0] != self._day_key(now):
            return 0
        return entry[1]

    def _bump_daily(self, user_id: str, now: float) -> None:
        day = self._day_key(now)
        count = self._daily_count(user_id, now)
        self._daily[user_id] = (day, count + 1)
