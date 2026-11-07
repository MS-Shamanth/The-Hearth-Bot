"""Voice notes: turn the parent's Telegram voice message into text for triage.

Parents who are anxious or unwell send voice notes rather than typing, so a
voice note goes through the same deterministic triage as text.

Two engines, tried in this order:
1. Any OpenAI-compatible ``/audio/transcriptions`` endpoint, when
   HEARTH_STT_BASE_URL is set (OpenAI ``whisper-1``, Groq
   ``whisper-large-v3-turbo``...). These accept Telegram's OGG/Opus as is.
2. Gemini's native ``generateContent`` with the audio inline, when the LLM is
   Gemini. Gemini's OpenAI-compatible endpoint only takes WAV and MP3, and
   Telegram sends OGG, so this path uses the native API with the same key.

If neither works, the companion relays the voice note to the family room for
a person to listen to. It is never silently dropped.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import httpx

from hearth.config import env
from hearth.llm import LLMSettings, model_missing, parse_json_object

log = logging.getLogger(__name__)

_GEMINI_HOST = "generativelanguage.googleapis.com"


class TranscriptionError(RuntimeError):
    pass


@dataclass
class Transcript:
    text: str          # what she said, as spoken
    english: str = ""  # an English rendering, when the engine gives one
    engine: str = ""

    @property
    def for_triage(self) -> str:
        return f"{self.text}\n{self.english}".strip()


def _prompt(language: str) -> str:
    return (
        "Transcribe this voice note from an elderly woman to her family's helper. "
        f"She speaks: {language}. Write exactly what she says, word for word: Kannada words in "
        "Kannada script, English words in English. Then give a faithful English translation. "
        'Answer with JSON only: {"transcript": "...", "english": "..."}. '
        'If there is no speech, use "[no speech]" for both.'
    )


class Transcriber:
    def __init__(
        self, *, language: str, timeout: float = 60, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.language = language
        self.stt_base = env("HEARTH_STT_BASE_URL").rstrip("/")
        self.stt_key = env("HEARTH_STT_API_KEY") or env("HEARTH_LLM_API_KEY")
        self.stt_model = env("HEARTH_STT_MODEL") or "whisper-1"
        llm_base = env("HEARTH_LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
        self.gemini_base = ""
        if _GEMINI_HOST in llm_base:
            self.gemini_base = llm_base.split("/openai", 1)[0].rstrip("/")
        self.gemini_key = env("HEARTH_LLM_API_KEY")
        # The same models as the companion: the pinned or alias model first, then the fallbacks.
        self.gemini_models = list(LLMSettings.model_candidates())
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def available(self) -> bool:
        return bool((self.stt_base and self.stt_key) or (self.gemini_base and self.gemini_key))

    async def close(self) -> None:
        await self._http.aclose()

    async def transcribe(self, data: bytes, mime: str, filename: str) -> Transcript:
        errors = []
        if self.stt_base and self.stt_key:
            try:
                return await self._openai_compatible(data, mime, filename)
            except TranscriptionError as exc:
                errors.append(str(exc))
        if self.gemini_base and self.gemini_key:
            try:
                return await self._gemini(data, mime)
            except TranscriptionError as exc:
                errors.append(str(exc))
        raise TranscriptionError("; ".join(errors) or "no speech-to-text engine is configured")

    async def _openai_compatible(self, data: bytes, mime: str, filename: str) -> Transcript:
        try:
            response = await self._http.post(
                f"{self.stt_base}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.stt_key}"},
                data={"model": self.stt_model, "response_format": "json",
                      "prompt": f"Kannada and English mixed speech. {self.language}"[:200]},
                files={"file": (filename or "voice.ogg", data, mime or "audio/ogg")},
            )
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"speech-to-text request failed: {exc}") from exc
        if response.status_code >= 400:
            raise TranscriptionError(f"speech-to-text returned {response.status_code}: {response.text[:200]}")
        text = str(response.json().get("text") or "").strip()
        if not text:
            raise TranscriptionError("empty transcript")
        return Transcript(text=text, engine=self.stt_model)

    async def _gemini(self, data: bytes, mime: str) -> Transcript:
        body = {
            "contents": [{"parts": [
                {"text": _prompt(self.language)},
                {"inline_data": {"mime_type": _gemini_mime(mime), "data": base64.b64encode(data).decode("ascii")}},
            ]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        response: httpx.Response | None = None
        model = ""
        for model in self.gemini_models:
            try:
                response = await self._http.post(
                    f"{self.gemini_base}/models/{model}:generateContent",
                    headers={"x-goog-api-key": self.gemini_key},
                    json=body,
                )
            except httpx.HTTPError as exc:
                raise TranscriptionError(f"Gemini request failed: {exc}") from exc
            if not model_missing(response.status_code, response.text) and response.status_code not in (
                    429, 500, 502, 503, 504):
                break
            log.warning("Gemini model %s can't take audio now (%s); trying the next one", model,
                        response.status_code)
        if response is None or response.status_code >= 400:
            status = response.status_code if response is not None else "no response"
            raise TranscriptionError(f"Gemini returned {status}: {response.text[:200] if response else ''}")
        try:
            parts = response.json()["candidates"][0]["content"]["parts"]
            out = parse_json_object("".join(p.get("text", "") for p in parts))
        except (KeyError, IndexError, ValueError) as exc:
            raise TranscriptionError(f"unexpected Gemini response: {response.text[:200]}") from exc
        text = str(out.get("transcript") or "").strip()
        if not text or text == "[no speech]":
            raise TranscriptionError("no speech in the voice note")
        return Transcript(text=text, english=str(out.get("english") or "").strip(), engine=model)


def _gemini_mime(mime: str) -> str:
    mime = (mime or "").split(";", 1)[0].strip().lower()
    return {"audio/opus": "audio/ogg", "audio/x-opus+ogg": "audio/ogg", "audio/oga": "audio/ogg"}.get(
        mime, mime or "audio/ogg"
    )


def is_audio(attachment: dict) -> bool:
    mime = str(attachment.get("mimetype") or "").lower()
    name = str(attachment.get("filename") or "").lower()
    return (
        mime.startswith("audio/")
        or attachment.get("msgtype") == "m.audio"
        or name.endswith((".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".aac"))
    )
