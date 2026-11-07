import json

import httpx
import pytest

from hearth.llm import ChatModel, LLMSettings
from hearth.speech import Transcriber, is_audio

GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai/"


@pytest.fixture
def gemini_env(monkeypatch):
    monkeypatch.setenv("HEARTH_LLM_BASE_URL", GEMINI)
    monkeypatch.setenv("HEARTH_LLM_API_KEY", "test-key")
    monkeypatch.delenv("HEARTH_LLM_FALLBACK_MODELS", raising=False)
    monkeypatch.delenv("HEARTH_STT_BASE_URL", raising=False)
    monkeypatch.delenv("HEARTH_STT_MODEL", raising=False)


def test_alias_with_pinned_fallback(gemini_env, monkeypatch):
    monkeypatch.setenv("HEARTH_LLM_MODEL", "gemini-flash-latest")
    assert LLMSettings.model_candidates() == ("gemini-flash-latest", "gemini-3.8-flash", "gemini-flash-lite-latest", "gemini-2.5-flash")


def test_pinned_model_has_no_duplicate_fallback(gemini_env, monkeypatch):
    monkeypatch.setenv("HEARTH_LLM_MODEL", "gemini-2.5-flash")
    assert LLMSettings.model_candidates() == ("gemini-2.5-flash", "gemini-3.8-flash", "gemini-flash-lite-latest")


def test_explicit_fallback_list(gemini_env, monkeypatch):
    monkeypatch.setenv("HEARTH_LLM_MODEL", "gemini-flash-latest")
    monkeypatch.setenv("HEARTH_LLM_FALLBACK_MODELS", "gemini-2.5-flash, gemini-2.5-flash-lite")
    assert LLMSettings.model_candidates() == ("gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-flash-lite")


async def test_missing_model_falls_back(gemini_env, monkeypatch):
    monkeypatch.setenv("HEARTH_LLM_MODEL", "gemini-flash-latest")
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        asked.append(model)
        if model == "gemini-flash-latest":
            return httpx.Response(404, json={"error": {"message": "models/gemini-flash-latest is not found"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"reply": "Namaskara"}'}}]})

    model = ChatModel(LLMSettings.from_env(), transport=httpx.MockTransport(handler))
    assert (await model.complete_json([{"role": "user", "content": "hi"}]))["reply"] == "Namaskara"
    assert asked == ["gemini-flash-latest", "gemini-3.8-flash"]
    assert model.model == "gemini-3.8-flash"  # it stays on the model that answered
    await model.close()


async def test_gemini_transcribes_ogg_natively(gemini_env, monkeypatch):
    monkeypatch.setenv("HEARTH_LLM_MODEL", "gemini-2.5-flash")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        body = json.loads(request.content)
        seen["mime"] = body["contents"][0]["parts"][1]["inline_data"]["mime_type"]
        text = json.dumps({"transcript": "ಎದೆ ನೋವು ಆಗ್ತಿದೆ", "english": "I have chest pain"})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})

    speech = Transcriber(language="Kannada and English", transport=httpx.MockTransport(handler))
    assert speech.available
    transcript = await speech.transcribe(b"OggS fake", "audio/ogg; codecs=opus", "voice.oga")
    assert seen["url"].endswith("/v1beta/models/gemini-2.5-flash:generateContent")
    assert seen["mime"] == "audio/ogg"
    assert transcript.english == "I have chest pain"
    assert "chest pain" in transcript.for_triage
    await speech.close()


async def test_openai_compatible_transcription(gemini_env, monkeypatch):
    monkeypatch.setenv("HEARTH_STT_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("HEARTH_STT_MODEL", "whisper-large-v3-turbo")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openai/v1/audio/transcriptions"
        assert b"whisper-large-v3-turbo" in request.content
        return httpx.Response(200, json={"text": "ede novu aagtide"})

    speech = Transcriber(language="Kannada", transport=httpx.MockTransport(handler))
    transcript = await speech.transcribe(b"OggS", "audio/ogg", "voice.ogg")
    assert transcript.text == "ede novu aagtide" and transcript.engine == "whisper-large-v3-turbo"
    await speech.close()


def test_audio_detection():
    assert is_audio({"mimetype": "audio/ogg"})
    assert is_audio({"msgtype": "m.audio"})
    assert is_audio({"filename": "voice_note.oga", "mimetype": "application/octet-stream"})
    assert not is_audio({"mimetype": "image/jpeg", "filename": "rx.jpg"})
