"""Adversarial tests for the exact wake-phrase command grammar."""

from __future__ import annotations

from ctb.voice.intent import VoiceCommand, VoiceIntentKind, parse_intent

WAKE = ["command", "команда", "slash"]


def test_ordinary_command_words_never_execute() -> None:
    for text in (
        "Please stop after the tests",
        "The command should create a workspace",
        "I am done when Ruff is clean",
        "Не забудь остановить после тестов",
    ):
        assert parse_intent(text, WAKE).kind is VoiceIntentKind.PROMPT


def test_wake_phrase_and_exact_alias_create_a_command() -> None:
    cases = (
        ("Command, stop", VoiceCommand.STOP, ""),
        ("КОМАНДА: зупини", VoiceCommand.STOP, ""),
        ("команда найти sessionIndex", VoiceCommand.FIND, "sessionIndex"),
        ("slash done", VoiceCommand.DONE, ""),
        ("command new api: fix Pyright", VoiceCommand.NEW, "api: fix Pyright"),
    )
    for text, command, argument in cases:
        intent = parse_intent(text, WAKE)
        assert intent.kind is VoiceIntentKind.COMMAND
        assert intent.command is command
        assert intent.argument == argument


def test_unicode_is_normalized_but_identifiers_are_not_translated() -> None:
    intent = parse_intent(
        "Ｃｏｍｍａｎｄ find API_Клієнт/sessionIndex",
        WAKE,
    )
    assert intent.command is VoiceCommand.FIND
    assert intent.argument == "API_Клієнт/sessionIndex"


def test_unknown_or_fuzzy_command_is_ambiguous() -> None:
    for text in ("command stopp", "команда зупин", "slash", "command please stop"):
        assert parse_intent(text, WAKE).kind is VoiceIntentKind.AMBIGUOUS


def test_done_is_confirmation_only() -> None:
    intent = parse_intent("command done", WAKE)
    assert intent.command is VoiceCommand.DONE
    assert intent.requires_confirmation


def test_intent_json_round_trip() -> None:
    original = parse_intent("команда знайди SQLite", WAKE)
    assert type(original).from_json(original.to_json()) == original
