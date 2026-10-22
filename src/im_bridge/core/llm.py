"""
LLM 客户端 —— 对接任意 OpenAI 兼容 API（DeepSeek / OpenAI / 本地推理服务等）。

能力：
- ``complete``：一次性补全（内部带指数退避重试）；
- ``stream``：SSE 流式补全，逐 delta 产出文本；
- ``summarize``：把旧对话压缩成摘要（用于 Summary Buffer）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import httpx

logger = logging.getLogger("im_bridge.llm")

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
NON_RETRYABLE_CLIENT_STATUS = {400, 401, 403, 404, 422}

SUMMARY_SYSTEM_PROMPT = (
    "你是对话记忆压缩器。请把聊天记录压缩成一段不超过 80 字的中文摘要，"
    "保留：用户身份与关系、关键事实、偏好、约定和未完成的话题。"
    "不要编造，不要评价，不要使用 Markdown，只输出摘要正文。"
)


class LLMError(Exception):
    """LLM 请求失败（网络、协议、空响应等）。"""


class LLMClient:
    """OpenAI 兼容的异步 LLM 客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60,
        max_retries: int = 3,
        temperature: float = 0.9,
        max_tokens: int = 600,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.temperature = temperature
        self.max_tokens = max_tokens

    # ---------- 内部工具 ----------
    @property
    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: list[dict],
        *,
        stream: bool,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        return {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": stream,
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))

    # ---------- 非流式 ----------
    async def complete(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """发送对话并返回完整回复，失败自动重试。"""
        last_error: Exception | None = None
        payload = self._payload(
            messages, stream=False, temperature=temperature, max_tokens=max_tokens
        )

        for attempt in range(self.max_retries):
            try:
                async with self._client() as client:
                    response = await client.post(
                        self._endpoint, headers=self._headers, json=payload
                    )

                    if response.status_code in RETRYABLE_STATUS:
                        raise LLMError(f"HTTP {response.status_code}")
                    if response.status_code in NON_RETRYABLE_CLIENT_STATUS:
                        raise LLMError(
                            f"HTTP {response.status_code}: {response.text[:200]}"
                        )
                    response.raise_for_status()

                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise LLMError(f"响应不是合法 JSON：{exc}") from exc
                    if not isinstance(data, dict):
                        raise LLMError("响应 JSON 不是对象")
                    choices = data.get("choices") or []
                    if not choices:
                        raise LLMError("响应中没有 choices")
                    content = (choices[0].get("message") or {}).get("content") or ""
                    text = content.strip()
                    if not text:
                        raise LLMError("模型返回了空回复")
                    return text

            except LLMError as exc:
                last_error = exc
                if "HTTP 4" in str(exc) and not str(exc).startswith("HTTP 429"):
                    raise
            except httpx.HTTPStatusError as exc:
                raise LLMError(
                    f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                ) from exc
            except httpx.RequestError as exc:
                last_error = exc
            except (KeyError, ValueError, IndexError) as exc:
                last_error = exc

            if attempt < self.max_retries - 1:
                delay = 2**attempt
                logger.warning(
                    "LLM 请求失败（第 %d/%d 次），%ss 后重试：%s",
                    attempt + 1,
                    self.max_retries,
                    delay,
                    last_error,
                )
                await asyncio.sleep(delay)

        raise LLMError(
            f"LLM 请求在 {self.max_retries} 次尝试后仍失败：{last_error}"
        ) from last_error

    # ---------- 流式 ----------
    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """SSE 流式补全，逐个 delta 产出文本片段。

        注意：一旦已经产出过内容，后续异常不会自动重试（避免重复发送），
        而是抛出 ``LLMError``，由调用方决定如何处理已收到的部分内容。
        """
        last_error: Exception | None = None
        payload = self._payload(messages, stream=True)

        for attempt in range(self.max_retries):
            yielded = False
            try:
                async with self._client() as client:
                    async with client.stream(
                        "POST", self._endpoint, headers=self._headers, json=payload
                    ) as response:
                        if response.status_code in RETRYABLE_STATUS:
                            await response.aread()
                            raise LLMError(f"HTTP {response.status_code}")
                        if response.status_code >= 400:
                            body = (await response.aread()).decode("utf-8", "replace")[:200]
                            raise LLMError(f"HTTP {response.status_code}: {body}")

                        async for line in response.aiter_lines():
                            line = line.strip()
                            if not line or not line.startswith("data:"):
                                continue
                            data_text = line[5:].strip()
                            if data_text == "[DONE]":
                                return
                            try:
                                data = json.loads(data_text)
                            except ValueError:
                                continue
                            if not isinstance(data, dict):
                                continue
                            choices = data.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta") or {}
                            content = delta.get("content")
                            if content:
                                yielded = True
                                yield content

                if yielded:
                    return
                last_error = LLMError("流式响应为空")

            except LLMError as exc:
                if yielded:
                    raise
                last_error = exc
                if "HTTP 4" in str(exc) and not str(exc).startswith("HTTP 429"):
                    raise
            except httpx.RequestError as exc:
                if yielded:
                    raise LLMError(f"流式传输中断：{exc}") from exc
                last_error = exc
            except (KeyError, ValueError, IndexError) as exc:
                if yielded:
                    raise LLMError(f"流式响应解析失败：{exc}") from exc
                last_error = exc

            if attempt < self.max_retries - 1:
                delay = 2**attempt
                logger.warning(
                    "LLM 流式请求失败（第 %d/%d 次），%ss 后重试：%s",
                    attempt + 1,
                    self.max_retries,
                    delay,
                    last_error,
                )
                await asyncio.sleep(delay)

        raise LLMError(
            f"LLM 流式请求在 {self.max_retries} 次尝试后仍失败：{last_error}"
        ) from last_error

    # ---------- 摘要 ----------
    async def summarize(self, existing_summary: str, transcript: str) -> str:
        """把旧对话（可叠加已有摘要）压缩成一段新摘要。"""
        parts: list[str] = []
        if existing_summary.strip():
            parts.append(f"已有摘要：\n{existing_summary.strip()}")
        if transcript.strip():
            parts.append(f"新增对话：\n{transcript.strip()}")
        if not parts:
            return existing_summary.strip()

        messages = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "\n\n".join(parts)
                + "\n\n请输出合并后的摘要正文（不超过 80 字）。",
            },
        ]
        text = await self.complete(messages, temperature=0.2, max_tokens=220)
        return text.strip().strip("“”\"'").strip()
