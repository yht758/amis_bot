"""
聊天引擎 —— 平台无关的核心编排。

负责把「上下文管理 + LLM 调用 + 拟真分片发送 + 指令系统」串起来：

    用户消息 -> 准入判断 -> 指令？ -> 上下文压缩 -> 构建 messages
             -> LLM 流式生成 -> 按标点分片 + 随机间隔发送 -> 记录回复

平台接入层只需实现 ``MessageTransport.send_text`` 并调用 ``handle_message``。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import random
import tempfile
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..log_writer import append_log
from ..personas import Persona
from ..ports import MessageTransport
from .chunker import ChunkStreamer, boundary_of, next_delay, split_chunks
from .commands import CommandResult, CommandRouter
from .context import ContextManager
from .llm import LLMClient, LLMError
from .state import StateManager
from .voice import SpeechSynthesizer

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
        synthesizer: SpeechSynthesizer | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.context = context_manager
        self.state = state_manager
        self.transport = transport
        self.persona = persona
        self.synthesizer = synthesizer
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
            await self._send_voice(uid, reply)
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

    async def _send_voice(self, user_id: int, text: str) -> None:
        """可选：把文本回复合成语音一并发出（失败绝不影响文字回复）。"""
        if not self.config.voice_reply_enabled:
            return
        if self.synthesizer is None or not self.synthesizer.enabled:
            return
        if not hasattr(self.transport, "send_record"):
            self.logger.warning("当前传输层不支持发送语音，跳过语音回复")
            return

        text = (text or "").strip()
        if not text:
            return

        try:
            with tempfile.TemporaryDirectory(prefix="im-bridge-voice-") as tmp:
                out = Path(tmp) / "reply.mp3"
                path = await self.synthesizer.synthesize(text, out)
                if path is None:
                    return
                data = path.read_bytes()
                if len(data) > 5 * 1024 * 1024:
                    self.logger.warning("合成语音超过 5MB，放弃发送")
                    return
                # NapCat 跑在容器里读不到宿主机临时路径，用 base64 内联传输
                encoded = "base64://" + base64.b64encode(data).decode("ascii")
                await self.transport.send_record(user_id, encoded)
                self.logger.info("[语音->%s] 已发送 %.1f KB", user_id, len(data) / 1024)
        except Exception as exc:  # noqa: BLE001 - 语音是锦上添花，失败不影响文字
            self.logger.warning("发送语音失败：%s", exc)

    # ---------- 回复生成 ----------
    async def _reply(self, user_id: int, messages: list[dict]) -> str:
        """生成并发送回复，返回完整回复文本（用于写入上下文）。"""
        sent = 0

        if self.config.streaming_enabled:
            full: list[str] = []
            streamer = ChunkStreamer(max_segments=self.config.chunk_max_segments)
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
                    # 关闭分片：流式只用于降低首字延迟，内容按平台上限拆条发送
                    sent += await self._send_piece(user_id, reply, sent)
                return reply

            # 没有任何内容：可能是端点不支持 SSE，回退为一次性补全
            self.logger.warning("流式未产出内容，回退一次性补全（stream_failed=%s）", stream_failed)

        reply = await self.llm.complete(messages)

        if self.config.chunking_enabled:
            for chunk in split_chunks(
                reply, max_segments=self.config.chunk_max_segments
            ):
                sent += await self._send_piece(user_id, chunk, sent)
        else:
            sent += await self._send_piece(user_id, reply, sent)
        return reply

    async def _send_piece(self, user_id: int, text: str, sent_count: int) -> int:
        """发送一个分片（必要时按平台长度上限再拆），返回实际发出的条数。"""
        text = (text or "").strip()
        if not text:
            return 0

        count = 0
        for piece in self._limit_pieces(text):
            piece = piece.strip()
            if not piece:
                continue
            if sent_count + count > 0:
                # 句末停顿最久、逗号最短，硬切几乎不停 —— 更像真人喘气
                delay = next_delay(
                    self.config.chunk_min_delay,
                    self.config.chunk_max_delay,
                    self._rng,
                    boundary=boundary_of(piece),
                )
                if delay > 0:
                    await asyncio.sleep(delay)
            await self.transport.send_text(user_id, piece)
            count += 1
        return count

    def _limit_pieces(self, text: str) -> list[str]:
        """按 AMIS_MESSAGE_MAX_CHARS 拆条；已在上限内则原样返回。"""
        limit = int(self.config.message_max_chars or 0)
        if limit <= 0 or len(text) <= limit:
            return [text]
        pieces = split_chunks(text, min_len=1, max_len=limit, max_segments=0)
        result: list[str] = []
        for piece in pieces:
            if len(piece) <= limit:
                result.append(piece)
            else:
                # 保护区超长等极端情况：按硬长度兜底，保证一定能发出去
                result.extend(
                    piece[index : index + limit] for index in range(0, len(piece), limit)
                )
        return result or [text]
