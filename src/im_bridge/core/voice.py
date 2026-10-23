"""
语音能力 —— 语音转写（ASR）与语音合成（TTS）。

两个类都对接 OpenAI 兼容接口，默认关闭：``endpoint`` 为空即视为未启用，
直接返回 ``None``，调用方据此回退到纯文本流程。

- ``VoiceTranscriber.transcribe``：``/v1/audio/transcriptions``（multipart 上传）；
- ``SpeechSynthesizer.synthesize``：``/v1/audio/speech``（JSON 请求，响应为二进制音频）。

设计原则：语音链路属于可选增强，任何失败都不抛异常，只记录 ``warning``
并返回 ``None``，避免把正常对话带崩。日志中不输出 API Key。
"""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

import httpx

logger = logging.getLogger("im_bridge.voice")

TRANSCRIPTIONS_PATH = "/audio/transcriptions"
SPEECH_PATH = "/audio/speech"


def _resolve_url(endpoint: str, suffix: str) -> str:
    """把配置的 endpoint 规范成完整接口地址。

    配置项可能写成 base（``https://host/v1``），也可能直接写成完整地址
    （``https://host/v1/audio/transcriptions``），两种写法都兼容。
    """
    base = endpoint.strip().rstrip("/")
    if base.endswith(suffix):
        return base
    return f"{base}{suffix}"


def _guess_mime(path: Path) -> str:
    """猜测音频文件的 MIME 类型，未知时退回二进制流。"""
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


class VoiceTranscriber:
    """OpenAI 兼容的语音转写（ASR）客户端。"""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str = "whisper-1",
        timeout: float = 60,
    ) -> None:
        self.endpoint = (endpoint or "").strip()
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        """是否已配置接口地址；为空表示功能未启用。"""
        return bool(self.endpoint)

    @property
    def _url(self) -> str:
        return _resolve_url(self.endpoint, TRANSCRIPTIONS_PATH)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))

    async def transcribe(self, audio_path: Path) -> str | None:
        """把本地音频文件转写成文字。

        返回转写文本；未启用、文件不存在或请求失败时返回 ``None``，不抛异常。
        """
        if not self.enabled:
            logger.debug("未配置语音转写地址，跳过转写")
            return None

        path = Path(audio_path)
        if not path.is_file():
            logger.warning("语音转写失败：音频文件不存在 %s", path)
            return None

        try:
            async with self._client() as client:
                response = await client.post(
                    self._url,
                    headers=self._headers,
                    files={"file": (path.name, path.read_bytes(), _guess_mime(path))},
                    data={"model": self.model, "response_format": "json"},
                )
                response.raise_for_status()

                try:
                    data = response.json()
                except ValueError:
                    logger.warning("语音转写失败：响应不是合法 JSON")
                    return None
                if not isinstance(data, dict):
                    logger.warning("语音转写失败：响应 JSON 不是对象")
                    return None
                text = data.get("text")
                if not isinstance(text, str):
                    logger.warning("语音转写失败：响应缺少 text 字段")
                    return None
                return text.strip()

        except httpx.HTTPStatusError as exc:
            logger.warning("语音转写失败：HTTP %s", exc.response.status_code)
            return None
        except httpx.RequestError as exc:
            logger.warning("语音转写请求异常：%s", type(exc).__name__)
            return None
        except Exception as exc:  # noqa: BLE001 - 语音链路不允许抛异常
            logger.warning("语音转写失败：%s", type(exc).__name__)
            return None


class SpeechSynthesizer:
    """OpenAI 兼容的语音合成（TTS）客户端。"""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str = "tts-1",
        voice: str = "alloy",
        timeout: float = 60,
    ) -> None:
        self.endpoint = (endpoint or "").strip()
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        """是否已配置接口地址；为空表示功能未启用。"""
        return bool(self.endpoint)

    @property
    def _url(self) -> str:
        return _resolve_url(self.endpoint, SPEECH_PATH)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))

    async def synthesize(
        self, text: str, out_path: Path, fmt: str = "mp3"
    ) -> Path | None:
        """把文本合成为音频并写入 ``out_path``。

        返回写入后的路径；未启用、文本为空、输出目录不存在或请求失败时
        返回 ``None``，不抛异常。
        """
        if not self.enabled:
            logger.debug("未配置语音合成地址，跳过合成")
            return None
        if not text or not text.strip():
            logger.warning("语音合成失败：待合成文本为空")
            return None

        target = Path(out_path)
        if not target.parent.is_dir():
            logger.warning("语音合成失败：输出目录不存在 %s", target.parent)
            return None

        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": fmt,
        }

        try:
            async with self._client() as client:
                response = await client.post(
                    self._url, headers=self._headers, json=payload
                )
                response.raise_for_status()

                audio = response.content
                if not audio:
                    logger.warning("语音合成失败：响应音频为空")
                    return None
                target.write_bytes(audio)
                return target

        except httpx.HTTPStatusError as exc:
            logger.warning("语音合成失败：HTTP %s", exc.response.status_code)
            return None
        except httpx.RequestError as exc:
            logger.warning("语音合成请求异常：%s", type(exc).__name__)
            return None
        except Exception as exc:  # noqa: BLE001 - 语音链路不允许抛异常
            logger.warning("语音合成失败：%s", type(exc).__name__)
            return None
