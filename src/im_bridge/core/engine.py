"""
聊天引擎 —— 平台无关的核心编排。

负责把「上下文管理 + LLM 调用 + 拟真分片发送 + 指令系统」串起来：

    用户消息 -> 准入判断 -> 指令？ -> 上下文压缩 -> 构建 messages
             -> LLM 流式生成 -> 按标点分片 + 随机间隔发送 -> 记录回复

平台接入层只需实现 ``MessageTransport.send_text`` 并调用 ``handle_message``。
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime

from ..config import Config
from ..log_writer import append_log
from ..personas import Persona
from ..ports import MessageTransport
from .chunker import ChunkStreamer, next_delay, split_chunks
from .commands import CommandResult, CommandRouter
from .context import ContextManager
from .llm import LLMClient, LLMError
from .state import StateManager

logger = logging.getLogger("im_bridge.engine")

# 用户可见的兜底文案（不暴露异常原文）
LLM_FALLBACK_TEXT = "我刚刚没想好，稍等再试一次哦~"
GENERIC_FALLBACK_TEXT = "刚刚有点卡，稍后再试~"


class ChatEngine:
    """把消息变成回复的编排器。"""

    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        context_manager: ContextManager,
        state_manager: StateManager,
        transport: MessageTransport,
        persona: Persona,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.context = context_manager
        self.state = state_manager
        self.transport = transport
        self.persona = persona
        self.logger = logger_ or logger
        self.started_at = datetime.now()
        self.commands = CommandRouter(config, state_manager, context_manager, self.started_at)
        self._rng = random.Random()

    # ---------- 入口 ----------
    async def handle_message(self, user_id: str, nickname: str, text: str) -> bool:
        """处理一条私聊消息；返回 True 表示请求停止。"""
        user_id = str(user_id)
        text = (text or "").strip()
        if not text:
            return False
        if not self.config.is_allowed(user_id):
            self.logger.info("忽略非白名单用户 %s", user_id)
            return False

        try:
            uid = int(user_id)
        except ValueError:
            self.logger.warning("非法 user_id：%r", user_id)
            return False

        append_log("questions.log", f"[{nickname}({user_id})] {text}")
        self.state.record_interaction(user_id, nickname)

        # 1) 指令：不分片，一次发完
        if self.commands.is_command(text):
            result: CommandResult = await self.commands.handle(user_id, text)
            if result.reply:
                append_log("answers.log", f"[{nickname}({user_id})] {result.reply}")
                self.logger.info("[回复->%s]: %s", nickname or user_id, result.reply[:80])
                await self._safe_send(uid, result.reply)
            return result.stop

        # 2) 普通聊天：先压缩旧历史，再追加当前用户消息
        try:
            await self.context.maybe_compress(user_id, self.llm)
            self.context.append_user(user_id, text)
            messages = self.context.build_messages(user_id, self.persona.system_prompt)

            self.logger.info("[%s(%s)]: %s", nickname or "用户", user_id, text)
            reply = await self._reply(uid, messages)

            self.context.append_assistant(user_id, reply)
            append_log("answers.log", f"[{nickname}({user_id})] {reply}")
            self.logger.info("[回复->%s]: %s", nickname or user_id, reply[:80])
            return False

        except LLMError as exc:
            self.logger.error("LLM 调用失败：%s", exc)
            append_log("status.log", f"[运行] LLM 请求失败：{exc}")
            await self._safe_send(uid, LLM_FALLBACK_TEXT)
            return False
        except Exception as exc:  # noqa: BLE001 - 单条消息失败不能拖垮整个进程
            self.logger.exception("处理消息出错：%s", exc)
            append_log("status.log", f"[运行] 处理消息出错：{exc}")
            await self._safe_send(uid, GENERIC_FALLBACK_TEXT)
            return False

    async def _safe_send(self, user_id: int, text: str) -> None:
        """发送兜底消息；发送通道本身异常时只记录日志，避免二次崩溃。"""
        try:
            await self.transport.send_text(user_id, text)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("发送消息失败：%s", exc)

    # ---------- 回复生成 ----------
    async def _reply(self, user_id: int, messages: list[dict]) -> str:
        """生成并发送回复，返回完整回复文本（用于写入上下文）。"""
        sent = 0

        if self.config.streaming_enabled:
            full: list[str] = []
            streamer = ChunkStreamer()
            stream_failed = False
            try:
                async for delta in self.llm.stream(messages):
                    full.append(delta)
                    if self.config.chunking_enabled:
                        for chunk in streamer.feed(delta):
                            sent += await self._send_piece(user_id, chunk, sent)
            except LLMError as exc:
                stream_failed = True
                self.logger.warning("流式生成中断：%s", exc)

            reply = "".join(full).strip()
            if reply:
                if self.config.chunking_enabled:
                    for chunk in streamer.flush():
                        sent += await self._send_piece(user_id, chunk, sent)
                else:
                    # 关闭分片时：流式仅用于降低首字延迟，内容仍整段一次发送
                    await self.transport.send_text(user_id, reply)
                return reply

            # 没有任何内容：可能是端点不支持 SSE，回退为一次性补全
            self.logger.warning("流式未产出内容，回退一次性补全（stream_failed=%s）", stream_failed)

        reply = await self.llm.complete(messages)

        if self.config.chunking_enabled:
            for chunk in split_chunks(reply):
                sent += await self._send_piece(user_id, chunk, sent)
        else:
            await self.transport.send_text(user_id, reply)
            sent += 1
        return reply

    async def _send_piece(self, user_id: int, text: str, sent_count: int) -> int:
        """发送一个分片；非首片前加入 1~2 秒随机延迟，模拟打字节奏。"""
        text = (text or "").strip()
        if not text:
            return 0
        if sent_count > 0:
            delay = next_delay(
                self.config.chunk_min_delay, self.config.chunk_max_delay, self._rng
            )
            if delay > 0:
                await asyncio.sleep(delay)
        await self.transport.send_text(user_id, text)
        return 1
