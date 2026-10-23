"""
多用户会话上下文管理 —— 隔离 + Summary Buffer 摘要压缩 + 滑动窗口兜底。

每个用户拥有独立的 ``Conversation``：
- ``messages``：最近若干轮原始对话（窗口上限 ``max_history``）；
- ``summary``：更早历史的压缩摘要（由 LLM 生成）。

Issue #2 的改进：
- 摘要默认放在**后台任务**里执行，不再阻塞当前回复；
- 触发条件从「消息条数」扩展为「消息条数 **或** 估算 token」；
- 每个用户一把锁 + 单飞任务，避免多轮并发重复压缩；
- 摘要落盘到 ``data/summaries/<uid>.json``，重启后不丢；
- 压缩失败自动退化为窗口裁剪，且带冷却，不会每轮重试。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLMClient, LLMError

logger = logging.getLogger("im_bridge.context")

# 摘要失败后的冷却时间（秒），避免每轮都重试导致聊天卡顿
COMPRESS_COOLDOWN = 60.0


def estimate_tokens(messages: list[dict]) -> int:
    """粗略估算 token 数。

    中文按 1 字 ≈ 1 token、其他字符按 4 字符 ≈ 1 token 估算。
    不引入 tokenizer 依赖，误差通常在 30% 以内，足够用于触发阈值判断。
    """
    total = 0
    for message in messages:
        text = str(message.get("content", ""))
        cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        total += cjk + (len(text) - cjk) // 4 + 1
    return total


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
        async_summary: bool = False,
        token_threshold: int = 1800,
        persist_dir: Path | str | None = None,
    ) -> None:
        self.max_history = max(4, int(max_history))
        self.summary_enabled = bool(summary_enabled)
        self.summary_threshold = max(2, int(summary_threshold))
        self.summary_keep_recent = max(2, int(summary_keep_recent))
        self.async_summary = bool(async_summary)
        self.token_threshold = max(0, int(token_threshold))
        self.persist_dir = Path(persist_dir) if persist_dir else None

        self._conversations: dict[str, Conversation] = {}
        self._compress_block_until: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._load_persisted()

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
        """清空某个用户的全部记忆（原文 + 摘要 + 落盘文件）；返回是否原本存在。"""
        user_id = str(user_id)
        existed = user_id in self._conversations
        self._conversations.pop(user_id, None)
        self._compress_block_until.pop(user_id, None)

        task = self._tasks.pop(user_id, None)
        if task is not None and not task.done():
            task.cancel()

        path = self._summary_path(user_id)
        if path is not None and path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        return existed

    def active_count(self) -> int:
        return len(self._conversations)

    def total_messages(self) -> int:
        return sum(len(conv.messages) for conv in self._conversations.values())

    @property
    def pending_summaries(self) -> int:
        """还有多少个后台摘要任务在跑（供管理台/日志观察）。"""
        return sum(1 for task in self._tasks.values() if not task.done())

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
        """必要时压缩历史。

        返回 ``True`` 表示本轮**同步完成了压缩**；``False`` 表示未压缩
        （不需要、被冷却、或已转交后台任务）。
        """
        user_id = str(user_id)
        conversation = self.get(user_id)
        messages = conversation.messages

        # 1) 窗口兜底：即使不摘要，也不允许无限增长
        if len(messages) > self.max_history:
            overflow = len(messages) - self.max_history
            del messages[:overflow]
            conversation.summarized_messages += overflow

        if not self.summary_enabled:
            return False
        if not self._should_compress(conversation):
            return False
        if time.time() < self._compress_block_until.get(user_id, 0.0):
            return False

        if self.async_summary:
            # 后台压缩，不阻塞当前回复
            self._schedule(user_id, llm)
            return False

        return await self._compress_locked(user_id, llm)

    def _should_compress(self, conversation: Conversation) -> bool:
        if len(conversation.messages) > self.summary_threshold:
            return True
        if (
            self.token_threshold
            and estimate_tokens(conversation.messages) >= self.token_threshold
        ):
            return True
        return False

    def _schedule(self, user_id: str, llm: LLMClient) -> None:
        """投递后台压缩任务；同一用户已有任务在跑时直接跳过（去重）。"""
        existing = self._tasks.get(user_id)
        if existing is not None and not existing.done():
            return
        self._tasks[user_id] = asyncio.create_task(
            self._compress_safely(user_id, llm), name=f"summarize-{user_id}"
        )

    async def _compress_safely(self, user_id: str, llm: LLMClient) -> None:
        try:
            await self._compress_locked(user_id, llm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务失败不能污染主流程
            logger.warning("后台摘要任务失败（用户 %s）：%s", user_id, exc)
        finally:
            self._tasks.pop(user_id, None)

    async def _compress_locked(self, user_id: str, llm: LLMClient) -> bool:
        """加锁执行压缩，避免同一用户并发改写摘要。"""
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            return await self._compress_now(user_id, llm)

    async def _compress_now(self, user_id: str, llm: LLMClient) -> bool:
        conversation = self.get(user_id)
        messages = conversation.messages

        if not self.summary_enabled or not self._should_compress(conversation):
            return False

        # 保留最近 N 条，尽量从一条 user 消息开始，避免把一轮对话劈开
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
        self._persist(user_id)
        logger.info(
            "已压缩用户 %s 的历史：%d 条 -> 摘要（保留 %d 条原文）",
            user_id,
            len(older),
            len(recent),
        )
        return True

    async def close(self) -> None:
        """取消所有后台摘要任务（进程退出前调用）。"""
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # ---------- 摘要落盘 ----------
    def _summary_path(self, user_id: str) -> Path | None:
        if self.persist_dir is None:
            return None
        return self.persist_dir / f"{user_id}.json"

    def _persist(self, user_id: str) -> None:
        path = self._summary_path(user_id)
        if path is None:
            return
        conversation = self._conversations.get(user_id)
        if conversation is None or not conversation.summary:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "summary": conversation.summary,
                        "summarized_messages": conversation.summarized_messages,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "utf-8",
            )
        except OSError:
            pass

    def _load_persisted(self) -> None:
        if self.persist_dir is None or not self.persist_dir.is_dir():
            return
        for file in sorted(self.persist_dir.glob("*.json")):
            try:
                data = json.loads(file.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            summary = str(data.get("summary") or "").strip()
            if not summary:
                continue
            conversation = Conversation(user_id=file.stem, summary=summary)
            conversation.summarized_messages = int(data.get("summarized_messages", 0) or 0)
            self._conversations[file.stem] = conversation

    @staticmethod
    def _format_transcript(messages: list[dict]) -> str:
        lines: list[str] = []
        for message in messages:
            role = "用户" if message.get("role") == "user" else "角色"
            content = str(message.get("content", "")).strip()
            if content:
                lines.append(f"{role}：{content}")
        return "\n".join(lines)
