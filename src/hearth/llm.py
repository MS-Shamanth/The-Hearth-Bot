"""A minimal OpenAI-compatible chat client (Gemini, OpenAI, OpenRouter, Ollama...)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from hearth.config import env

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
# The alias follows Google's current Flash model; gemini-2.5-flash is the pinned,
# stable fallback. Set HEARTH_LLM_MODEL=gemini-2.5-flash to pin it outright.
DEFAULT_MODEL = "gemini-flash-latest"
# gemini-2.5-flash is closed to new API keys (404 "no longer available to new users"),
# so it comes last; gemini-3.8-flash is what Google's error message points to.
DEFAULT_GEMINI_FALLBACKS = ("gemini-3.8-flash", "gemini-flash-lite-latest", "gemini-2.5-flash")
_BUSY = {429, 500, 502, 503, 504}


def model_missing(status: int, text: str) -> bool:
    """The provider doesn't have this model (retired alias, typo, no access)."""
    if status == 404:
        return True
    lowered = text.lower()
    return status == 400 and ("not found" in lowered or "is not supported" in lowered or "unknown model" in lowered)


@dataclass
class LLMSettings:
    base_url: str
    api_key: str
    model: str
    fallback_models: tuple[str, ...] = ()

    @staticmethod
    def model_candidates() -> tuple[str, ...]:
        """HEARTH_LLM_MODEL, then HEARTH_LLM_FALLBACK_MODELS (comma-separated).
        On Gemini the fallback defaults to gemini-2.5-flash."""
        base = env("HEARTH_LLM_BASE_URL", GEMINI_BASE_URL)
        primary = env("HEARTH_LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        raw = env("HEARTH_LLM_FALLBACK_MODELS")
        if raw:
            fallbacks = tuple(m.strip() for m in raw.split(",") if m.strip())
        else:
            fallbacks = DEFAULT_GEMINI_FALLBACKS if "generativelanguage.googleapis.com" in base else ()
        return (primary, *[m for m in fallbacks if m != primary])

    @classmethod
    def from_env(cls) -> LLMSettings:
        primary, *fallbacks = cls.model_candidates()
        return cls(
            base_url=env("HEARTH_LLM_BASE_URL", GEMINI_BASE_URL),
            api_key=env("HEARTH_LLM_API_KEY", required=True),
            model=primary,
            fallback_models=tuple(fallbacks),
        )


def image_part(data: bytes, mime: str) -> dict[str, Any]:
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


class ChatModel:
    def __init__(
        self, settings: LLMSettings, *, timeout: float = 25, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.models = [settings.model, *settings.fallback_models]
        self._http = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + "/",
            timeout=timeout,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            transport=transport,
        )

    @property
    def model(self) -> str:
        """The model answering now: the first candidate the provider has."""
        return self.models[0]

    async def close(self) -> None:
        await self._http.aclose()

    async def complete(
        self, messages: list[dict[str, Any]], *, json_mode: bool = False, temperature: float = 0.4
    ) -> str:
        response: httpx.Response | None = None
        failure = ""
        for attempt in range(2):  # one pass over the models, then one retry after a pause
            if attempt:
                await asyncio.sleep(2)
            for model in list(self.models):
                body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                try:
                    response = await self._http.post("chat/completions", json=body)
                except httpx.HTTPError as exc:
                    failure = f"{model}: {type(exc).__name__} {exc}".strip()
                    log.warning("LLM request to %s failed (%s); trying the next model", model, failure)
                    continue
                if model_missing(response.status_code, response.text) and len(self.models) > 1:
                    log.warning("model %s is not available (%s); dropping it", model, response.status_code)
                    self.models.remove(model)
                    continue
                if response.status_code in _BUSY:
                    failure = f"{model}: {response.status_code} (busy)"
                    log.warning("model %s is busy (%s); trying the next model", model, response.status_code)
                    continue
                if model != self.models[0]:  # stay on the model that answered
                    self.models.remove(model)
                    self.models.insert(0, model)
                break
            else:
                continue
            break
        else:
            raise LLMError(f"no model answered ({failure or 'no response'})")
        assert response is not None
        if response.status_code >= 400:
            raise LLMError(f"LLM ({self.model}) returned {response.status_code}: {response.text[:300]}")
        try:
            return response.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"unexpected LLM response: {response.text[:300]}") from exc

    async def complete_json(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        text = await self.complete(messages, json_mode=True, **kwargs)
        return parse_json_object(text)


def parse_json_object(text: str) -> dict[str, Any]:
    """Models sometimes wrap JSON in fences or prose; take the first object."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"no JSON object in model output: {text[:200]}") from None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"bad JSON in model output: {text[:200]}") from exc
    if not isinstance(value, dict):
        raise LLMError("model output is not a JSON object")
    return value
