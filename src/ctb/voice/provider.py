"""Speech-to-text provider boundary and ElevenLabs Scribe v2 adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx

from ctb.logging import get_logger

__all__ = [
    "ElevenLabsProvider",
    "SpeechProvider",
    "Transcription",
    "TranscriptionError",
]

log = get_logger(__name__)

ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"

# ``enable_logging=false`` is a *query* parameter, not a form field, and selects
# ElevenLabs' zero-retention mode: neither the audio nor the transcript is kept.
# It is enterprise-gated, so a rejection aimed at this parameter degrades the
# request instead of failing the job — the note still has to be transcribed.
_ZERO_RETENTION_PARAMS: Final[dict[str, str]] = {"enable_logging": "false"}


class TranscriptionError(RuntimeError):
    """A safe-to-display speech-provider failure."""


@dataclass(frozen=True, slots=True)
class Transcription:
    text: str
    language: str | None = None
    language_probability: float | None = None


class SpeechProvider(Protocol):
    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        mime_type: str,
        keyterms: list[str],
    ) -> Transcription: ...

    async def aclose(self) -> None: ...


class ElevenLabsProvider:
    """Synchronous file transcription; Telegram OGG/Opus is sent unchanged."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "scribe_v2",
        language: str = "auto",
        timeout: float = 45.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._language = language
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._zero_retention = True

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        mime_type: str,
        keyterms: list[str],
    ) -> Transcription:
        data: dict[str, Any] = {
            "model_id": self._model,
            "tag_audio_events": "false",
            "diarize": "false",
            "timestamps_granularity": "none",
            "file_format": "other",
            "no_verbatim": "false",
        }
        if self._language.strip().lower() != "auto":
            data["language_code"] = self._language.strip()
        cleaned = _valid_keyterms(keyterms)
        if cleaned:
            data["keyterms"] = cleaned
        response = await self._post(data, audio, filename=filename, mime_type=mime_type)
        if self._zero_retention and 400 <= response.status_code < 500:
            # Zero retention is enterprise-gated, so a refusal is expected on
            # most plans — and the refusal does not have to mention it. Matching
            # on vendor prose meant a 403 aimed at `enable_logging` read as "the
            # key lacks Speech-to-Text", which sent the owner to re-check a
            # permission that was already correct.
            #
            # So drop the optional parameter and try again rather than guess
            # what the body will say. One extra call, only on the failure path,
            # and only until the first refusal latches it off for the process.
            log.warning(
                "voice.zero_retention_unavailable",
                status=response.status_code,
                detail=_reason(response),
            )
            self._zero_retention = False
            response = await self._post(
                data, audio, filename=filename, mime_type=mime_type
            )
        if response.status_code == 429:
            raise TranscriptionError("Speech service is rate-limited. Retry shortly.")
        # 401 and 403 are different problems with different fixes, and saying
        # "key is invalid" for a 403 sends you to re-paste a key that was fine.
        # ElevenLabs scopes each key per product, so a real key with
        # Speech-to-Text unticked is the likelier of the two.
        if response.status_code == 401:
            raise TranscriptionError(
                f"ElevenLabs rejected the key · {_reason(response)}"
            )
        if response.status_code == 403:
            # Say what ElevenLabs said. Naming a cause we inferred sent the
            # owner to re-check a Speech-to-Text permission that was already on.
            raise TranscriptionError(f"ElevenLabs refused · {_reason(response)}")
        if response.status_code >= 400:
            raise TranscriptionError(
                f"Speech service rejected the note ({response.status_code})."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TranscriptionError(
                "Speech service returned an invalid response."
            ) from exc
        if not isinstance(payload, dict):
            raise TranscriptionError("Speech service returned an invalid response.")
        text = payload.get("text")
        if not isinstance(text, str):
            raise TranscriptionError("Speech service returned no transcript.")
        language = payload.get("language_code")
        probability = payload.get("language_probability")
        return Transcription(
            text=text,
            language=language if isinstance(language, str) else None,
            language_probability=(
                float(probability) if isinstance(probability, (int, float)) else None
            ),
        )

    async def _post(
        self,
        data: dict[str, Any],
        audio: bytes,
        *,
        filename: str,
        mime_type: str,
    ) -> httpx.Response:
        try:
            return await self._client.post(
                ELEVENLABS_STT_URL,
                headers={"xi-api-key": self._api_key},
                params=dict(_ZERO_RETENTION_PARAMS) if self._zero_retention else None,
                data=data,
                files={"file": (filename, audio, mime_type)},
            )
        except httpx.TimeoutException as exc:
            raise TranscriptionError("Transcription timed out.") from exc
        except httpx.HTTPError as exc:
            raise TranscriptionError("Speech service is unreachable.") from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _reason(response: httpx.Response) -> str:
    """ElevenLabs' own words for a refusal, in one short phone line.

    The body is one of several shapes (``detail.message``, ``detail.status``, a
    bare ``detail`` string, ``message``), so pull whichever is there and fall
    back to the status code rather than inventing a cause.
    """
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    candidates: list[Any] = []
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            candidates += [detail.get("message"), detail.get("status")]
        candidates += [detail, payload.get("message")]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:120]
    return f"HTTP {response.status_code}"


def _valid_keyterms(values: list[str]) -> list[str]:
    invalid = frozenset(r"<> {}[]\\".replace(" ", ""))
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = " ".join(value.split())
        folded = term.casefold()
        if (
            not term
            or len(term) >= 50
            or len(term.split()) > 5
            or any(char in invalid for char in term)
            or folded in seen
        ):
            continue
        seen.add(folded)
        out.append(term)
        if len(out) == 100:
            break
    return out
