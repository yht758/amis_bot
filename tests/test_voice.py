"""
语音能力离线测试 —— 假 HTTP，不访问真实网络。

运行：python tests/test_voice.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libs"))

import httpx  # noqa: E402

from im_bridge.core.voice import SpeechSynthesizer, VoiceTranscriber  # noqa: E402

FAKE_KEY = "sk-voice-secret"


def make_response(
    status_code: int = 200,
    *,
    json_data: dict | None = None,
    content: bytes = b"",
) -> httpx.Response:
    """构造带 request 的 httpx.Response，便于 raise_for_status 正常工作。"""
    request = httpx.Request("POST", "http://fake.local/v1/audio")
    if json_data is not None:
        return httpx.Response(status_code, json=json_data, request=request)
    return httpx.Response(status_code, content=content, request=request)


class FakeAsyncClient:
    """假的 httpx.AsyncClient，只实现 post 与异步上下文协议。"""

    def __init__(
        self,
        response: httpx.Response | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def patch_client(cls, fake: FakeAsyncClient):
    """把被测类的 _client() 换成返回假客户端的工厂。"""
    return patch.object(cls, "_client", lambda self: fake)


class VoiceTranscriberTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.audio = self.tmp_dir / "hello.mp3"
        self.audio.write_bytes(b"fake-audio-bytes")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_disabled_endpoint_returns_none(self) -> None:
        transcriber = VoiceTranscriber(endpoint="", api_key=FAKE_KEY)
        fake = FakeAsyncClient(response=make_response(json_data={"text": "不该被调用"}))
        with patch_client(VoiceTranscriber, fake):
            result = await transcriber.transcribe(self.audio)
        self.assertIsNone(result)
        self.assertEqual(fake.calls, [])
        self.assertFalse(transcriber.enabled)

    async def test_success_returns_text_and_sends_multipart(self) -> None:
        transcriber = VoiceTranscriber(
            endpoint="https://asr.local/v1", api_key=FAKE_KEY, model="whisper-1"
        )
        fake = FakeAsyncClient(response=make_response(json_data={"text": " 你好呀 "}))
        with patch_client(VoiceTranscriber, fake):
            result = await transcriber.transcribe(self.audio)

        self.assertEqual(result, "你好呀")
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["url"], "https://asr.local/v1/audio/transcriptions")
        self.assertEqual(call["data"], {"model": "whisper-1", "response_format": "json"})
        filename, payload, _mime = call["files"]["file"]
        self.assertEqual(filename, "hello.mp3")
        self.assertEqual(payload, b"fake-audio-bytes")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {FAKE_KEY}")

    async def test_full_endpoint_is_not_duplicated(self) -> None:
        transcriber = VoiceTranscriber(
            endpoint="https://asr.local/v1/audio/transcriptions", api_key=FAKE_KEY
        )
        fake = FakeAsyncClient(response=make_response(json_data={"text": "ok"}))
        with patch_client(VoiceTranscriber, fake):
            await transcriber.transcribe(self.audio)
        self.assertEqual(
            fake.calls[0]["url"], "https://asr.local/v1/audio/transcriptions"
        )

    async def test_http_error_returns_none_and_logs_without_key(self) -> None:
        transcriber = VoiceTranscriber(endpoint="https://asr.local/v1", api_key=FAKE_KEY)
        error = httpx.HTTPStatusError(
            "server error",
            request=httpx.Request("POST", "https://asr.local/v1/audio/transcriptions"),
            response=make_response(500),
        )
        fake = FakeAsyncClient(error=error)
        with patch_client(VoiceTranscriber, fake):
            with self.assertLogs("im_bridge.voice", level="WARNING") as logs:
                result = await transcriber.transcribe(self.audio)

        self.assertIsNone(result)
        joined = "\n".join(logs.output)
        self.assertIn("HTTP 500", joined)
        self.assertNotIn(FAKE_KEY, joined)

    async def test_network_error_returns_none(self) -> None:
        transcriber = VoiceTranscriber(endpoint="https://asr.local/v1", api_key=FAKE_KEY)
        fake = FakeAsyncClient(error=httpx.ConnectError("boom"))
        with patch_client(VoiceTranscriber, fake):
            result = await transcriber.transcribe(self.audio)
        self.assertIsNone(result)

    async def test_missing_audio_file_returns_none(self) -> None:
        transcriber = VoiceTranscriber(endpoint="https://asr.local/v1", api_key=FAKE_KEY)
        fake = FakeAsyncClient(response=make_response(json_data={"text": "不该被调用"}))
        with patch_client(VoiceTranscriber, fake):
            result = await transcriber.transcribe(self.tmp_dir / "missing.mp3")
        self.assertIsNone(result)
        self.assertEqual(fake.calls, [])

    async def test_invalid_json_returns_none(self) -> None:
        transcriber = VoiceTranscriber(endpoint="https://asr.local/v1", api_key=FAKE_KEY)
        fake = FakeAsyncClient(
            response=make_response(content=b"not-json", status_code=200)
        )
        with patch_client(VoiceTranscriber, fake):
            result = await transcriber.transcribe(self.audio)
        self.assertIsNone(result)


class SpeechSynthesizerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.out = self.tmp_dir / "reply.mp3"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_disabled_endpoint_returns_none(self) -> None:
        synth = SpeechSynthesizer(endpoint="", api_key=FAKE_KEY)
        fake = FakeAsyncClient(response=make_response(content=b"audio"))
        with patch_client(SpeechSynthesizer, fake):
            result = await synth.synthesize("你好", self.out)
        self.assertIsNone(result)
        self.assertEqual(fake.calls, [])
        self.assertFalse(self.out.exists())

    async def test_success_writes_file_and_returns_path(self) -> None:
        synth = SpeechSynthesizer(
            endpoint="https://tts.local/v1", api_key=FAKE_KEY, voice="nova"
        )
        fake = FakeAsyncClient(response=make_response(content=b"ID3-fake-audio"))
        with patch_client(SpeechSynthesizer, fake):
            result = await synth.synthesize("晚上好呀", self.out, fmt="wav")

        self.assertEqual(result, self.out)
        self.assertEqual(self.out.read_bytes(), b"ID3-fake-audio")
        call = fake.calls[0]
        self.assertEqual(call["url"], "https://tts.local/v1/audio/speech")
        self.assertEqual(
            call["json"],
            {
                "model": "tts-1",
                "input": "晚上好呀",
                "voice": "nova",
                "response_format": "wav",
            },
        )
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {FAKE_KEY}")

    async def test_http_error_returns_none_and_logs_without_key(self) -> None:
        synth = SpeechSynthesizer(endpoint="https://tts.local/v1", api_key=FAKE_KEY)
        error = httpx.HTTPStatusError(
            "bad request",
            request=httpx.Request("POST", "https://tts.local/v1/audio/speech"),
            response=make_response(401),
        )
        fake = FakeAsyncClient(error=error)
        with patch_client(SpeechSynthesizer, fake):
            with self.assertLogs("im_bridge.voice", level="WARNING") as logs:
                result = await synth.synthesize("你好", self.out)

        self.assertIsNone(result)
        self.assertFalse(self.out.exists())
        joined = "\n".join(logs.output)
        self.assertIn("HTTP 401", joined)
        self.assertNotIn(FAKE_KEY, joined)

    async def test_missing_output_dir_returns_none(self) -> None:
        synth = SpeechSynthesizer(endpoint="https://tts.local/v1", api_key=FAKE_KEY)
        fake = FakeAsyncClient(response=make_response(content=b"audio"))
        with patch_client(SpeechSynthesizer, fake):
            result = await synth.synthesize("你好", self.tmp_dir / "nope" / "reply.mp3")
        self.assertIsNone(result)
        self.assertEqual(fake.calls, [])

    async def test_empty_text_returns_none(self) -> None:
        synth = SpeechSynthesizer(endpoint="https://tts.local/v1", api_key=FAKE_KEY)
        fake = FakeAsyncClient(response=make_response(content=b"audio"))
        with patch_client(SpeechSynthesizer, fake):
            result = await synth.synthesize("   ", self.out)
        self.assertIsNone(result)
        self.assertEqual(fake.calls, [])

    async def test_empty_audio_response_returns_none(self) -> None:
        synth = SpeechSynthesizer(endpoint="https://tts.local/v1", api_key=FAKE_KEY)
        fake = FakeAsyncClient(response=make_response(content=b""))
        with patch_client(SpeechSynthesizer, fake):
            result = await synth.synthesize("你好", self.out)
        self.assertIsNone(result)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
