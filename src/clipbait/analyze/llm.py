"""Provider-agnostic LLM client.

Four providers, one JSON contract, no SDK bloat. Every call is bounded by a
timeout and exponential backoff; failure is non-fatal because the caller has a
heuristic fallback.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import httpx

from ..config import Settings, detect_capabilities, get_settings

log = logging.getLogger("clipbait.llm")


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMResult:
    data: dict
    provider: str
    model: str


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise LLMError(f"model returned non-JSON: {text[:160]}") from None


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        caps = detect_capabilities(self.settings)
        self.provider = caps.llm_provider
        self.model = self._model_for(self.provider)

    def _model_for(self, provider: str | None) -> str:
        match provider:
            case "openai":
                return self.settings.openai_model
            case "anthropic":
                return self.settings.anthropic_model
            case "gemini":
                return self.settings.gemini_model
            case "ollama":
                return self.settings.ollama_model
            case _:
                return "none"

    @property
    def available(self) -> bool:
        return self.provider is not None

    def complete_json(
        self, prompt: str, system: str | None = None, *, temperature: float = 0.3
    ) -> LLMResult:
        if not self.provider:
            raise LLMError("no LLM provider configured")
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                if self.provider == "openai":
                    data = self._openai(prompt, system, temperature)
                elif self.provider == "anthropic":
                    data = self._anthropic(prompt, system, temperature)
                elif self.provider == "gemini":
                    data = self._gemini(prompt, system, temperature)
                else:
                    data = self._ollama(prompt, system, temperature)
                return LLMResult(data=data, provider=self.provider, model=self.model)
            except (LLMError, httpx.HTTPError, json.JSONDecodeError) as exc:
                last_error = exc
                log.warning("LLM attempt %s failed: %s", attempt + 1, exc)
                time.sleep(min(8.0, 1.5 * (2**attempt)))
        raise LLMError(str(last_error) if last_error else "LLM request failed")

    # ---------------------------------------------------------------- providers

    def _openai(self, prompt: str, system: str | None, temperature: float) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            json={
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
            },
            timeout=180,
        )
        if response.status_code >= 400:
            raise LLMError(f"openai {response.status_code}: {response.text[:200]}")
        return _extract_json(response.json()["choices"][0]["message"]["content"])

    def _anthropic(self, prompt: str, system: str | None, temperature: float) -> dict:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.settings.anthropic_api_key or "",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": self.model,
                "max_tokens": 4096,
                "temperature": temperature,
                "system": system or "",
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=180,
        )
        if response.status_code >= 400:
            raise LLMError(f"anthropic {response.status_code}: {response.text[:200]}")
        blocks = response.json().get("content", [])
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
        return _extract_json(text)

    def _gemini(self, prompt: str, system: str | None, temperature: float) -> dict:
        payload: dict = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        response = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            params={"key": self.settings.gemini_api_key},
            json=payload,
            timeout=180,
        )
        if response.status_code >= 400:
            raise LLMError(f"gemini {response.status_code}: {response.text[:200]}")
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        return _extract_json(text)

    def _ollama(self, prompt: str, system: str | None, temperature: float) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = httpx.post(
            f"{self.settings.ollama_base_url.rstrip('/')}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "format": "json",
                "options": {"temperature": temperature},
            },
            timeout=600,
        )
        if response.status_code >= 400:
            raise LLMError(f"ollama {response.status_code}: {response.text[:200]}")
        return _extract_json(response.json()["message"]["content"])


def get_llm(settings: Settings | None = None) -> LLMClient:
    return LLMClient(settings)