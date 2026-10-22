"""
多用户会话上下文管理 —— 隔离 + Summary Buffer 摘要压缩 + 滑动窗口兜底。

每个用户拥有独立的 ``Conversation``：
- ``messages``：最近若干轮原始对话（窗口上限 ``max_history``）；
- ``summary``：更早历史的压缩摘要（由 LLM 生成，一句话/一小段）。

触发策略：消息条数超过 ``summary_threshold`` 时，把最早的部分压缩进
``summary``，只保留最近 ``summary_keep_recent`` 条原文。这样高频短文本聊天
不会无限增长 token，同时最近语境 100% 保真。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .llm import LLMClient, LLMError

logger = logging.getLogger("im_bridge.context")

# 摘要失败后的冷却时间（秒），避免每轮都重试导致聊天卡顿
COMPRESS_COOLDOWN = 60.0


@dataclass
class Conversation:
    """单个用户的对话上下文。"""

    user_id: str
    summary: str = ""
    messages: list[dict] = field(default_factory=list)
    summarized_messages: int = 0  # 已被压缩进 summary 的消息条数（统计用）

    def clear(self) -> None:
        self.summary = ""
        self.messages.clear()
        self.summarized_messages = 0


class ContextManager:
    """全局上下文管理器。"""

    def __init__(
        self,
        max_history: int = 40,
        summary_enabled: bool = True,
        summary_threshold: int = 24,
        summary_keep_recent: int = 8,
    ) -> None:
        self.max_history = max(4, int(max_history))
        self.summary_enabled = bool(summary_enabled)
        self.summary_threshold = max(2, int(summary_threshold))
        self.summary_keep_recent = max(2, int(summary_keep_recent))
        self._conversations: dict[str, Conversation] = {}
        self._compress_block_until: dict[str, float] = {}

    # ---------- 基础操作 ----------
    def get(self, user_id: str) -> Conversation:
        user_id = str(user_id)
        if user_id not in self._conversations:
            self._conversations[user_id] = Conversation(user_id=user_id)
        return self._conversations[user_id]

    def append_user(self, user_id: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.get(user_id).messages.append({"role": "user", "content": text})

    def append_assistant(self, user_id: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.get(user_id).messages.append({"role": "assistant", "content": text})

    def reset(self, user_id: str) -> bool:
        """清空某个用户的全部记忆（原文 + 摘要）；返回是否原本存在。"""
        existed = str(user_id) in self._conversations
        self._conversations.pop(str(user_id), None)
        self._compress_block_until.pop(str(user_id), None)
        return existed

    def active_count(self) -> int:
        return len(self._conversations)

    def total_messages(self) -> int:
        return sum(len(conv.messages) for conv in self._conversations.values())

    # ---------- 构建请求 ----------
    def build_messages(self, user_id: str, system_prompt: str) -> list[dict]:
        """组装发给 LLM 的 messages：人设 + 历史摘要 + 最近原文。"""
        conversation = self.get(user_id)
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        if conversation.summary:
            messages.append(
                {
                    "role": "system",
                    "content": "以下是这段关系更早之前的对话摘要，供你自然衔接，"
                    "不要向用户复述或暴露摘要本身：\n" + conversation.summary,
                }
            )
        messages.extend(conversation.messages)
        return messages

    # ---------- Summary Buffer ----------
    async def maybe_compress(self, user_id: str, llm: LLMClient) -> bool:
        """必要时压缩历史。返回是否真的执行了压缩。

        失败不抛异常：摘要失败时退化为滑动窗口裁剪，保证聊天可用。
        """
        conversation = self.get(user_id)
        messages = conversation.messages

        # 1) 窗口兜底：即使不摘要，也不允许无限增长
        if len(messages) > self.max_history:
            overflow = len(messages) - self.max_history
            del messages[:overflow]
            conversation.summarized_messages += overflow

        if not self.summary_enabled:
            return False
        if len(messages) <= self.summary_threshold:
            return False

        user_id = str(user_id)
        if time.time() < self._compress_block_until.get(user_id, 0.0):
            return False

        # 2) 保留最近 N 条，尽量从一条 user 消息开始，避免把一轮对话劈开
        cut = len(messages) - self.summary_keep_recent
        while cut > 1 and messages[cut].get("role") != "user":
            cut -= 1
        if cut <= 0:
            return False

        older = messages[:cut]
        recent = messages[cut:]
        if not older:
            return False

        transcript = self._format_transcript(older)
        try:
            summary = await llm.summarize(conversation.summary, transcript)
        except LLMError as exc:
            logger.warning("压缩历史失败（用户 %s），本轮退化为窗口裁剪：%s", user_id, exc)
            self._compress_block_until[user_id] = time.time() + COMPRESS_COOLDOWN
            return False
        except Exception as exc:  # noqa: BLE001 - 摘要异常绝不能影响聊天主流程
            logger.warning(
                "压缩历史出现未预期异常（用户 %s），本轮退化为窗口裁剪：%s", user_id, exc
            )
            self._compress_block_until[user_id] = time.time() + COMPRESS_COOLDOWN
            return False

        if not summary:
            self._compress_block_until[user_id] = time.time() + COMPRESS_COOLDOWN
            return False

        conversation.summary = summary
        conversation.messages = recent
        conversation.summarized_messages += len(older)
        logger.info(
            "已压缩用户 %s 的历史：%d 条 -> 摘要（保留 %d 条原文）",
            user_id,
            len(older),
            len(recent),
        )
        return True

    @staticmethod
    def _format_transcript(messages: list[dict]) -> str:
        lines: list[str] = []
        for message in messages:
            role = "用户" if message.get("role") == "user" else "角色"
            content = str(message.get("content", "")).strip()
            if content:
                lines.append(f"{role}：{content}")
        return "\n".join(lines)
