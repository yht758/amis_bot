"""
OneBot v11 传输层 —— 通过 WebSocket 与 NapCat / go-cqhttp 等实现通信。

这是“可选适配器”的一部分：核心层只认识 ``MessageTransport`` 协议，
换成其它平台时无需改动 ``src/im_bridge/core``。
"""
from __future__ import annotations

import json
import logging

import websockets

logger = logging.getLogger("im_bridge.onebot.transport")


class OneBotTransport:
    """封装 OneBot v11 的 WebSocket 连接与 ``send_private_msg`` 调用。"""

    def __init__(self, ws_url: str, logger_: logging.Logger | None = None) -> None:
        self.ws_url = ws_url
        self.logger = logger_ or logger
        self.ws = None
        self._echo_counter = 0

    @property
    def connected(self) -> bool:
        return self.ws is not None

    async def connect(self) -> None:
        """建立 WebSocket 连接（由调用方的重连循环负责调度）。"""
        try:
            # websockets >= 15 支持 proxy 参数；显式禁用代理，避免 localhost 被系统代理劫持
            self.ws = await websockets.connect(self.ws_url, max_size=2**23, proxy=None)
        except TypeError:
            # websockets < 15 没有 proxy 参数
            self.ws = await websockets.connect(self.ws_url, max_size=2**23)
        self.logger.info("已连接 OneBot WebSocket：%s", self.ws_url)

    async def send_text(self, user_id: int, text: str) -> None:
        """通过 OneBot v11 发送私聊消息。"""
        if self.ws is None:
            self.logger.warning("WebSocket 未连接，消息被丢弃")
            return

        self._echo_counter += 1
        payload = {
            "action": "send_private_msg",
            "params": {"user_id": int(user_id), "message": text},
            "echo": str(self._echo_counter),
        }
        try:
            await self.ws.send(json.dumps(payload, ensure_ascii=False))
            self.logger.info("已发送给 %s", user_id)
        except Exception as exc:  # noqa: BLE001 - 单条发送失败不应中断回复流程
            self.logger.error("发送失败：%s", exc)

    async def close(self) -> None:
        ws = self.ws
        self.ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
