"""
平台端口（Ports）定义。

核心层只依赖这些抽象，不感知 QQ / OneBot / NapCat 等具体平台。
接入新平台时实现 ``MessageTransport`` 并在装配入口注入即可。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MessageTransport(Protocol):
    """发送文本消息的能力。"""

    async def send_text(self, user_id: int, text: str) -> None:
        """向指定用户发送一段文本。实现方负责平台协议细节。"""
        ...


@runtime_checkable
class MessageTransportLifecycle(Protocol):
    """可选的生命周期能力；适配器可实现以便优雅关闭。"""

    async def close(self) -> None:
        ...
