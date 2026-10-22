"""
OneBot 适配器运行时 —— 事件循环、连接重连、单用户串行化。

只负责“接消息 / 发消息 / 连接管理”，业务编排全部交给 ``ChatEngine``。
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets

from ...config import Config
from ...core.engine import ChatEngine
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
        logger_: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.engine = engine
        self.transport = transport
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

    async def _handle_private_msg(self, data: dict) -> None:
        user_id = str(data.get("user_id", ""))
        raw_message = data.get("raw_message", data.get("message", ""))
        nickname = str((data.get("sender") or {}).get("nickname", ""))

        if not isinstance(raw_message, str):
            raw_message = str(raw_message)
        if not user_id or not raw_message.strip():
            return
        if not self.config.respond_to_groups and data.get("message_type") == "group":
            return
        if user_id in self.processing:
            return

        self.processing.add(user_id)
        try:
            should_stop = await self.engine.handle_message(user_id, nickname, raw_message)
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
                self._retry_delay = min(self._retry_delay * 2, 60)
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

        await self.transport.close()
        append_log("status.log", "[运行] 机器人已停止")
        self.logger.info("机器人已停止")
