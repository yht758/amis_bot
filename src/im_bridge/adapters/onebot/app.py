"""
OneBot 适配器运行时 —— 事件循环、连接重连、单用户串行化。

只负责“接消息 / 发消息 / 连接管理”，业务编排全部交给 ``ChatEngine``。
本层额外承担两件事：
- 把语音消息转写成文字（Issue #5），转写失败则忽略该条消息；
- 托管主动会话调度器的启停（Issue #4）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import websockets

from ...config import Config
from ...core.engine import ChatEngine
from ...core.voice import VoiceTranscriber
from ...log_writer import append_log
from .transport import OneBotTransport

logger = logging.getLogger("im_bridge.onebot.app")


class OneBotApp:
    """OneBot v11 私聊机器人运行时。"""

    def __init__(
        self,
        config: Config,
        engine: ChatEngine,
        transport: OneBotTransport,
        transcriber: VoiceTranscriber | None = None,
        proactive=None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.engine = engine
        self.transport = transport
        self.transcriber = transcriber
        self.proactive = proactive
        self.logger = logger_ or logger
        self.processing: set[str] = set()  # 同一用户串行处理，避免并发串台
        self.tasks: set[asyncio.Task] = set()
        self.stop_event = asyncio.Event()
        self._retry_delay = 1

    async def _cancel_pending_tasks(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---------- 语音 ----------
    @staticmethod
    def _find_record(message) -> str:
        """从消息段或 CQ 码里找出语音文件的标识。"""
        if isinstance(message, str):
            start = message.find("[CQ:record,")
            if start < 0:
                return ""
            end = message.find("]", start)
            if end < 0:
                return ""
            segment = message[start + len("[CQ:record,") : end]
            for part in segment.split(","):
                key, _, value = part.partition("=")
                if key.strip() == "file":
                    return value.strip()
            return ""

        if isinstance(message, list):
            for segment in message:
                if not isinstance(segment, dict) or segment.get("type") != "record":
                    continue
                data = segment.get("data") or {}
                file = data.get("file") or data.get("url") or ""
                if file:
                    return str(file)
        return ""

    def _map_local_path(self, remote: str) -> Path | None:
        """把容器内路径映射成宿主机可读路径（NapCat 在 Docker 里时必须配）。"""
        mapping = (self.config.voice_path_map or "").strip()
        if mapping:
            for pair in mapping.split(","):
                if "=" not in pair:
                    continue
                prefix, _, local_root = pair.partition("=")
                prefix, local_root = prefix.strip(), local_root.strip()
                if prefix and local_root and remote.startswith(prefix):
                    candidate = Path(local_root + remote[len(prefix) :])
                    return candidate if candidate.exists() else None
        candidate = Path(remote)
        return candidate if candidate.exists() else None

    async def _transcribe(self, file: str) -> str:
        if self.transcriber is None or not self.transcriber.enabled:
            return ""
        try:
            info = await self.transport.call_api(
                "get_record", {"file": file, "out_format": "mp3"}
            )
        except Exception as exc:  # noqa: BLE001 - 取语音失败只影响这一条消息
            self.logger.warning("获取语音文件失败：%s", exc)
            return ""

        remote_path = str((info or {}).get("file") or "").strip()
        if not remote_path:
            self.logger.warning("get_record 未返回文件路径")
            return ""

        local = self._map_local_path(remote_path)
        if local is None:
            self.logger.warning(
                "语音文件无法映射到本机：%s（请检查 AMIS_VOICE_PATH_MAP）", remote_path
            )
            return ""

        text = await self.transcriber.transcribe(local)
        if text:
            self.logger.info("语音已转写：%s", text[:40])
            append_log("questions.log", f"[语音转写] {text[:80]}")
        return text or ""

    # ---------- 消息处理 ----------
    async def _handle_private_msg(self, data: dict) -> None:
        user_id = str(data.get("user_id", ""))
        message = data.get("message")
        raw_message = data.get("raw_message", message if isinstance(message, str) else "")
        nickname = str((data.get("sender") or {}).get("nickname", ""))

        if not isinstance(raw_message, str):
            raw_message = str(raw_message)

        record = self._find_record(message if message is not None else raw_message)
        if record:
            if not self.config.voice_input_enabled:
                self.logger.info("收到语音消息，但未开启 AMIS_VOICE_INPUT_ENABLED，已忽略")
                return
            text = await self._transcribe(record)
            if not text:
                return
        else:
            text = raw_message.strip()

        if not user_id or not text:
            return
        if not self.config.respond_to_groups and data.get("message_type") == "group":
            return
        if user_id in self.processing:
            return

        self.processing.add(user_id)
        try:
            should_stop = await self.engine.handle_message(user_id, nickname, text)
            if should_stop:
                self.stop_event.set()
                await self.transport.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单条消息异常不影响连接
            self.logger.exception("处理消息异常：%s", exc)
        finally:
            self.processing.discard(user_id)

    async def _dispatch(self, data: dict) -> None:
        post_type = data.get("post_type", "")
        message_type = data.get("message_type", "")
        if post_type == "message" and message_type == "private":
            task = asyncio.create_task(self._handle_private_msg(data))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def run(self) -> None:
        """主循环：连接 / 收事件 / 断线指数退避重连，直到收到停止请求。"""
        ws_url = self.config.napcat_ws_url
        self.logger.info("正在连接 OneBot WebSocket：%s", ws_url)
        append_log("status.log", f"[运行] 正在连接 OneBot WebSocket：{ws_url}")

        if self.proactive is not None:
            await self.proactive.start()

        try:
            while not self.stop_event.is_set():
                try:
                    await self.transport.connect()
                    self._retry_delay = 1
                    append_log("status.log", "[运行] 已连接 OneBot WebSocket")

                    async for raw in self.transport.ws:
                        try:
                            data = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(data, dict):
                            # API 响应优先交给传输层匹配，其余才当事件分发
                            if self.transport.handle_response(data):
                                continue
                            await self._dispatch(data)
                        if self.stop_event.is_set():
                            break

                except (websockets.ConnectionClosed, OSError) as exc:
                    await self.transport.close()
                    await self._cancel_pending_tasks()
                    if self.stop_event.is_set():
                        break
                    self.logger.warning("连接断开：%s，%s 秒后重试", exc, self._retry_delay)
                    append_log(
                        "status.log",
                        f"[运行] 连接断开：{exc}，{self._retry_delay} 秒后重试",
                    )
                    await asyncio.sleep(self._retry_delay)
                    self._retry_delay = min(self._retry_delay * 2, 15)
                except asyncio.CancelledError:
                    await self.transport.close()
                    raise
                except Exception as exc:  # noqa: BLE001
                    await self.transport.close()
                    await self._cancel_pending_tasks()
                    if self.stop_event.is_set():
                        break
                    self.logger.error("运行出错：%s", exc)
                    append_log("status.log", f"[运行] 发生错误：{exc}")
                    await asyncio.sleep(5)
        finally:
            if self.proactive is not None:
                await self.proactive.stop()
            await self.engine.context.close()

        await self.transport.close()
        append_log("status.log", "[运行] 机器人已停止")
        self.logger.info("机器人已停止")
