"""Telegram voice-note transcription and strict intent routing."""

from __future__ import annotations

from ctb.voice.intent import VoiceCommand, VoiceIntent, VoiceIntentKind, parse_intent
from ctb.voice.provider import (
    ElevenLabsProvider,
    SpeechProvider,
    Transcription,
    TranscriptionError,
)

__all__ = [
    "ElevenLabsProvider",
    "SpeechProvider",
    "Transcription",
    "TranscriptionError",
    "VoiceCommand",
    "VoiceIntent",
    "VoiceIntentKind",
    "parse_intent",
]
