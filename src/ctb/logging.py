"""structlog → JSON on stdout, with a mandatory scrubbing processor.

Two rules this module exists to enforce:

1. **Secrets never reach a log line.** ``Authorization`` headers, anything that
   looks like ``Bearer <token>``, and the exact configured secret values are
   redacted anywhere in the event dict, including inside nested dicts and lists.
   The scrubber is installed unconditionally — there is no flag to turn it off.
2. **Transcript content is the user's source code.** It is only ever logged when
   ``LOG_TRANSCRIPT_CONTENT=true``, via :func:`transcript_preview`.

Bound context keys (PLAN §Logging & secrets)::

    request_id  chat_id  session_id  workspace_id  turn_state
    endpoint    status_code  duration_ms  attempt

One line per state transition is the debugging surface:
``turn.transition from=QUEUED to=WORKING evidence=delta``.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any, Final

import structlog
from structlog.typing import EventDict, FilteringBoundLogger, Processor, WrappedLogger

from ctb.settings import Settings

__all__ = [
    "REDACTED",
    "bind_log_context",
    "clear_log_context",
    "configure_logging",
    "get_logger",
    "register_secret",
    "scrub_secrets",
    "transcript_preview",
    "unbind_log_context",
]

REDACTED: Final = "[redacted]"

#: Keys whose *value* is always redacted regardless of content.
_SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "conductor_api_key",
        "password",
        "secret",
        "telegram_bot_token",
        "token",
        "x-api-key",
    }
)

_BEARER_RE: Final = re.compile(r"(?i)\bBearer\s+\S+")
# Telegram bot tokens appear inline in API URLs (".../bot<token>/sendMessage"),
# so no leading word boundary — there is a letter immediately before the digits.
_TG_TOKEN_RE: Final = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")

#: Secrets shorter than this are not worth substring-replacing — the false
#: positive rate would mangle ordinary log lines.
_MIN_SECRET_LEN: Final = 8

_secrets: set[str] = set()

#: Depth guard: transcript payloads nest deeply and a log call must never
#: become the expensive part of a poll tick.
_MAX_DEPTH: Final = 8


def register_secret(value: str | None) -> None:
    """Add a literal string the scrubber must redact wherever it appears."""
    if value and len(value) >= _MIN_SECRET_LEN:
        _secrets.add(value)


def _forget_secrets() -> None:
    """Test hook. Production never un-registers a secret."""
    _secrets.clear()


def _scrub_str(value: str) -> str:
    for secret in _secrets:
        if secret in value:
            value = value.replace(secret, REDACTED)
    value = _BEARER_RE.sub(f"Bearer {REDACTED}", value)
    return _TG_TOKEN_RE.sub(REDACTED, value)


def _scrub_value(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        return _scrub_str(value)
    if isinstance(value, Mapping):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS
                else _scrub_value(val, depth + 1)
            )
            for key, val in value.items()  # pyright: ignore[reportUnknownVariableType]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        scrubbed = [
            _scrub_value(item, depth + 1)
            for item in value  # pyright: ignore[reportUnknownVariableType]
        ]
        return type(value)(scrubbed) if isinstance(value, (list, tuple)) else scrubbed
    return value


def scrub_secrets(
    _logger: WrappedLogger | None = None,
    _method_name: str = "",
    event_dict: MutableMapping[str, Any] | None = None,
) -> EventDict:
    """structlog processor. Mandatory — installed by :func:`configure_logging`.

    Also usable directly as ``scrub_secrets(event_dict=...)`` in tests.
    """
    if event_dict is None:
        return {}
    scrubbed = _scrub_value(dict(event_dict), depth=0)
    assert isinstance(scrubbed, dict)
    return scrubbed


def _drop_transcript_content(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Belt and braces: never let raw transcript content through by accident.

    Renderers and repos should call :func:`transcript_preview`; this catches the
    ones that forget.
    """
    for key in (
        "content",
        "content_json",
        "transcript",
        "prompt",
        "prompt_text",
        "blocks",
        "rendered_html",
    ):
        if key in event_dict and not isinstance(event_dict[key], (int, float, bool)):
            value = event_dict[key]
            event_dict[key] = f"<{key} {_size_of(value)} chars, content logging off>"
    return event_dict


def _size_of(value: Any) -> int:
    try:
        return len(value if isinstance(value, str) else repr(value))
    except Exception:  # pragma: no cover - defensive
        return -1


def configure_logging(settings: Settings | None = None) -> None:
    """Configure structlog and the stdlib root logger to emit JSON on stdout.

    Idempotent: safe to call again (tests, reconfiguration after settings load).
    """
    level_name = (settings.log_level if settings else "INFO").upper()
    level = logging.getLevelNamesMapping().get(level_name, logging.INFO)

    if settings is not None:
        for secret in settings.secret_values():
            register_secret(secret)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings is None or not settings.log_transcript_content:
        shared.append(_drop_transcript_content)
    # The scrubber runs last, so it also sees anything the processors above added.
    shared.append(scrub_secrets)

    structlog.configure(
        processors=[*shared, structlog.processors.JSONRenderer(sort_keys=True)],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.WriteLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (aiogram, httpx, asyncio) through the same pipeline so
    # a third-party library cannot print an unscrubbed URL with a token in it.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    # httpx logs every request at INFO including the full URL; we log API calls
    # ourselves with structured fields.
    logging.getLogger("httpx").setLevel(max(level, logging.WARNING))


def get_logger(name: str | None = None, **initial: Any) -> FilteringBoundLogger:
    """A bound structlog logger. ``name`` is conventionally the module name."""
    logger: FilteringBoundLogger = (
        structlog.get_logger(name) if name else structlog.get_logger()
    )
    return logger.bind(**initial) if initial else logger


def bind_log_context(
    *,
    request_id: str | None = None,
    chat_id: int | None = None,
    thread_id: int | None = None,
    session_id: str | None = None,
    workspace_id: str | None = None,
    turn_state: str | None = None,
    endpoint: str | None = None,
    status_code: int | None = None,
    duration_ms: float | None = None,
    attempt: int | None = None,
    **extra: Any,
) -> None:
    """Bind the PLAN §Logging context keys for the current task/contextvar scope.

    ``None`` values are skipped, so this is safe to call with partial context.
    """
    values: dict[str, Any] = {
        "request_id": request_id,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "turn_state": turn_state,
        "endpoint": endpoint,
        "status_code": status_code,
        "duration_ms": duration_ms,
        "attempt": attempt,
        **extra,
    }
    structlog.contextvars.bind_contextvars(
        **{k: v for k, v in values.items() if v is not None}
    )


def unbind_log_context(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)


def clear_log_context() -> None:
    structlog.contextvars.clear_contextvars()


def transcript_preview(
    content: Any,
    settings: Settings | None = None,
    *,
    limit: int = 200,
) -> str:
    """Render transcript content for a log line — gated, truncated, scrubbed.

    Returns a size-only placeholder unless ``LOG_TRANSCRIPT_CONTENT`` is true.
    Transcript content is the user's source code; this is the *only* sanctioned
    way to put any of it in a log.
    """
    text = content if isinstance(content, str) else repr(content)
    if settings is None or not settings.log_transcript_content:
        return f"<{len(text)} chars, content logging off>"
    clipped = text if len(text) <= limit else text[: limit - 1] + "…"
    return _scrub_str(clipped)


def iter_secrets() -> Iterable[str]:  # pragma: no cover - introspection helper
    return tuple(_secrets)
