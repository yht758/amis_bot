"""
LLM 接口模块 —— 对接 OpenAI 兼容 API
"""
import asyncio
import logging
import httpx

from .character import SYSTEM_PROMPT

logger = logging.getLogger("llm_client")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    """LLM 请求失败"""


class LLMClient:
    """OpenAI 兼容的 LLM 客户端"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int = 60,
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    async def chat(self, history: list[dict]) -> str:
        """发送对话并获取回复，失败时自动重试"""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]

        for h in history:
            role = "assistant" if h["role"] == "assistant" else "user"
            messages.append({"role": role, "content": h["content"]})

        last_error = None

        for attempt in range(self.max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.model,
                            "messages": messages,
                            "temperature": 0.9,
                            "max_tokens": 600,
                        },
                    )

                    if resp.status_code in RETRYABLE_STATUS:
                        raise LLMError(f"HTTP {resp.status_code}")

                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"].strip()
                    if not content:
                        raise LLMError("empty response")
                    return content

            except LLMError as exc:
                last_error = exc
            except httpx.HTTPStatusError as exc:
                raise LLMError(
                    f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                ) from exc
            except httpx.RequestError as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc

            if attempt < self.max_retries - 1:
                delay = 2 ** attempt
                logger.warning(
                    "LLM request failed (attempt %s/%s), retry in %ss: %s",
                    attempt + 1,
                    self.max_retries,
                    delay,
                    last_error,
                )
                await asyncio.sleep(delay)

        raise LLMError(
            f"LLM request failed after {self.max_retries} attempts: {last_error}"
        ) from last_error
