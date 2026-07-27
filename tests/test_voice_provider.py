"""Provider contract tests without network access."""

from __future__ import annotations

import httpx
import pytest

from ctb.voice.provider import ElevenLabsProvider, TranscriptionError


async def test_elevenlabs_sends_ogg_with_safe_single_speaker_options() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        captured["body"] = (await request.aread()).decode("utf-8", errors="replace")
        return httpx.Response(
            200,
            json={
                "text": "Команда знайди sessionIndex",
                "language_code": "ukr",
                "language_probability": 0.99,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = ElevenLabsProvider("secret-stt-key", client=client)
    try:
        result = await provider.transcribe(
            b"OggS-test",
            filename="voice.ogg",
            mime_type="audio/ogg",
            keyterms=["sessionIndex", "SQLite"],
        )
    finally:
        await client.aclose()

    assert result.text == "Команда знайди sessionIndex"
    assert result.language == "ukr"
    assert captured["url"] == (
        "https://api.elevenlabs.io/v1/speech-to-text?enable_logging=false"
    )
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["xi-api-key"] == "secret-stt-key"
    body = str(captured["body"])
    assert 'name="model_id"' in body and "scribe_v2" in body
    assert 'name="diarize"' in body and "false" in body
    assert 'name="tag_audio_events"' in body and "false" in body
    assert 'filename="voice.ogg"' in body
    assert "sessionIndex" in body


async def test_a_non_enterprise_tier_degrades_instead_of_losing_the_note() -> None:
    """Zero retention is enterprise-gated; being refused it must not fail a job."""
    urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if "enable_logging" in str(request.url):
            return httpx.Response(
                403, json={"detail": "enable_logging=false requires an enterprise plan"}
            )
        return httpx.Response(200, json={"text": "ship it"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = ElevenLabsProvider("secret", client=client)
    try:
        first = await provider.transcribe(
            b"audio", filename="v.ogg", mime_type="audio/ogg", keyterms=[]
        )
        second = await provider.transcribe(
            b"audio", filename="v.ogg", mime_type="audio/ogg", keyterms=[]
        )
    finally:
        await client.aclose()

    assert first.text == "ship it" and second.text == "ship it"
    assert [("enable_logging" in url) for url in urls] == [True, False, False]


async def test_an_unrelated_4xx_is_still_an_error_and_is_not_retried() -> None:
    """Only a rejection naming the parameter degrades — nothing else."""
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"detail": "audio file is corrupt"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = ElevenLabsProvider("secret", client=client)
    try:
        with pytest.raises(TranscriptionError, match="rejected"):
            await provider.transcribe(
                b"audio", filename="v.ogg", mime_type="audio/ogg", keyterms=[]
            )
    finally:
        await client.aclose()

    assert calls == 1


@pytest.mark.parametrize(
    ("status", "message"),
    (
        # 401 and 403 are deliberately different sentences: re-paste the key
        # vs. tick Speech-to-Text on a key that was fine. See provider.py.
        (401, "rejected the key"),
        (403, "lacks Speech-to-Text"),
        (429, "rate-limited"),
        (500, "rejected"),
    ),
)
async def test_provider_errors_are_short_and_safe(status: int, message: str) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="vendor internals")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = ElevenLabsProvider("secret", client=client)
    try:
        with pytest.raises(TranscriptionError, match=message):
            await provider.transcribe(
                b"audio",
                filename="voice.ogg",
                mime_type="audio/ogg",
                keyterms=[],
            )
    finally:
        await client.aclose()
