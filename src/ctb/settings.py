"""Configuration. Secrets come from the environment only, never from code.

Boot fails loudly and immediately if ``TELEGRAM_BOT_TOKEN``,
``CONDUCTOR_API_KEY`` or ``ALLOWED_TELEGRAM_USER_IDS`` are missing — a bot that
starts without an allowlist is a bot anyone can drive. All three are checked in
*field* validators so a first deploy reports every missing variable in one
message; fixing them one crash at a time is its own kind of outage.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from ctb.conductor.errors import PairingError
from ctb.conductor.models import validate_pairing

__all__ = [
    "Settings",
    "SettingsError",
    "env_flag",
    "get_settings",
    "load_settings",
    "reset_settings",
    "set_settings",
]


class SettingsError(RuntimeError):
    """Configuration is missing or invalid. Raised at boot, never caught."""


_REQUIRED_HINT = {
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN — from @BotFather",
    "conductor_api_key": (
        "CONDUCTOR_API_KEY — https://app.conductor.build/users/api-keys"
    ),
    "allowed_telegram_user_ids": (
        "ALLOWED_TELEGRAM_USER_IDS — comma-separated Telegram user ids, "
        "the first is the owner"
    ),
}

#: Marker a *field* validator raises for "set, but empty". pydantic only reports
#: ``missing`` for absent fields, and a model validator never runs while another
#: field is missing — so a blank check that lives anywhere else would surface on
#: the *next* deploy instead of this one. Field validators all run, so all three
#: required vars land in one message.
_UNSET = "unset"


class Settings(BaseSettings):
    """Everything the bot reads from the environment.

    Field names map to the upper-cased env var of the same name (see
    ``.env.example``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- secrets ---------------------------------------------------------------
    telegram_bot_token: SecretStr
    conductor_api_key: SecretStr

    # -- Conductor -------------------------------------------------------------
    conductor_api_url: str = "https://api.conductor.build/v0"

    # -- Telegram --------------------------------------------------------------
    #: Comma-separated in the environment. The first id is the owner. Required:
    #: a bot without an allowlist is a bot anyone can drive.
    allowed_telegram_user_ids: Annotated[list[int], NoDecode]
    #: The private supergroup the bot operates in. ``None`` until ``/setup`` runs.
    telegram_chat_id: int | None = None

    # -- Telegram voice notes -------------------------------------------------
    #: Feature-gated so typed control stays available without a speech vendor.
    voice_enabled: bool = False
    voice_stt_provider: Literal["elevenlabs"] = "elevenlabs"
    voice_stt_model: str = "scribe_v2"
    voice_mode: Literal["shadow", "prompts", "commands"] = "prompts"
    voice_max_duration_seconds: int = Field(default=180, ge=1, le=3600)
    voice_max_file_bytes: int = Field(
        default=20 * 1024 * 1024,
        ge=1,
        le=20 * 1024 * 1024,
    )
    voice_max_concurrent: int = Field(default=2, ge=1, le=8)
    voice_language: str = "auto"
    voice_wake_phrases: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["command", "команда", "slash"]
    )
    voice_completed_retention_days: int = Field(default=7, ge=1, le=90)
    elevenlabs_api_key: SecretStr | None = None

    # -- storage ---------------------------------------------------------------
    db_path: Path = Path("/data/ctb.db")

    # -- defaults for the zero-tap `/new <prompt>` path -------------------------
    default_agent: str = "claude"
    default_model: str = "opus-5-1m"
    default_effort: str = "high"

    # -- logging ---------------------------------------------------------------
    #: Transcript content is the user's source code. Keep this false outside of
    #: active debugging.
    log_transcript_content: bool = False
    log_level: str = "INFO"

    @field_validator("allowed_telegram_user_ids", mode="before")
    @classmethod
    def _parse_id_list(cls, value: Any) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, int):
            return [value]
        if isinstance(value, str):
            parts = [p.strip() for p in value.replace(";", ",").split(",")]
            out: list[int] = []
            for part in parts:
                if not part:
                    continue
                try:
                    out.append(int(part))
                except ValueError:
                    raise ValueError(
                        f"{part!r} is not a Telegram user id "
                        "(expected comma-separated integers)"
                    ) from None
            return out
        if isinstance(value, (list, tuple)):
            return [int(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
        raise ValueError(f"cannot read a user-id list from {value!r}")

    @field_validator("allowed_telegram_user_ids", mode="after")
    @classmethod
    def _dedupe_preserving_order(cls, value: list[int]) -> list[int]:
        seen: set[int] = set()
        out: list[int] = []
        for uid in value:
            if uid not in seen:
                seen.add(uid)
                out.append(uid)
        if not out:
            raise ValueError(_UNSET)
        return out

    @field_validator("telegram_chat_id", "elevenlabs_api_key", mode="before")
    @classmethod
    def _blank_optional_is_unset(cls, value: Any) -> Any:
        # ``cp .env.example .env`` leaves ``TELEGRAM_CHAT_ID=`` behind. Empty
        # means "not set"; without this the first boot dies on "unable to parse
        # string as an integer" for a variable nobody was asked to fill in.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("telegram_bot_token", "conductor_api_key", mode="after")
    @classmethod
    def _reject_blank_secret(cls, value: SecretStr) -> SecretStr:
        # ``TELEGRAM_BOT_TOKEN=`` is set as far as pydantic is concerned; the
        # failure would otherwise arrive later, from Telegram, as a 401.
        if not value.get_secret_value().strip():
            raise ValueError(_UNSET)
        return value

    @field_validator("voice_wake_phrases", mode="before")
    @classmethod
    def _parse_wake_phrases(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return ["command", "команда", "slash"]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        raise ValueError("expected comma-separated wake phrases")

    @field_validator("voice_wake_phrases", mode="after")
    @classmethod
    def _normalize_wake_phrases(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            phrase = " ".join(item.split()).casefold()
            if phrase and phrase not in seen:
                seen.add(phrase)
                out.append(phrase)
        if not out:
            raise ValueError("VOICE_WAKE_PHRASES must contain at least one phrase")
        return out

    @field_validator("log_level", mode="after")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if level not in allowed:
            raise ValueError(
                f"LOG_LEVEL must be one of {sorted(allowed)}, got {value!r}"
            )
        return level

    @field_validator("conductor_api_url", mode="after")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError(
                f"CONDUCTOR_API_URL must be an absolute URL, got {value!r}"
            )
        return url

    @model_validator(mode="after")
    def _check_pairing(self) -> Self:
        try:
            validate_pairing(
                self.default_agent, self.default_model, self.default_effort
            )
        except PairingError as exc:
            raise ValueError(
                f"DEFAULT_AGENT/DEFAULT_MODEL/DEFAULT_EFFORT are not a valid "
                f"combination: {exc}"
            ) from exc
        return self

    # -- derived ---------------------------------------------------------------

    @property
    def owner_id(self) -> int:
        """The first allow-listed id. Gets the DMs nobody else should see."""
        return self.allowed_telegram_user_ids[0]

    @property
    def conductor_api_root_url(self) -> str:
        """The API root (no ``/v0``). ``GET /me`` lives here and only here."""
        parts = urlsplit(self.conductor_api_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def me_url(self) -> str:
        return f"{self.conductor_api_root_url}/me"

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.allowed_telegram_user_ids

    def secret_values(self) -> tuple[str, ...]:
        """Every secret string, for the log scrubber to redact on sight."""
        values = (
            self.telegram_bot_token.get_secret_value(),
            self.conductor_api_key.get_secret_value(),
            (
                self.elevenlabs_api_key.get_secret_value()
                if self.elevenlabs_api_key is not None
                else ""
            ),
        )
        return tuple(v for v in values if v)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Settings(api={self.conductor_api_url!r}, db={self.db_path!s}, "
            f"owner={self.owner_id}, agent={self.default_agent}/"
            f"{self.default_model}/{self.default_effort})"
        )


def load_settings(**overrides: Any) -> Settings:
    """Build :class:`Settings`, converting pydantic noise into one clear message.

    Raises :class:`SettingsError` — the boot path lets it propagate.
    """
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        lines: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"])
            msg = err["msg"].removeprefix("Value error, ")
            if err["type"] == "missing" or (msg == _UNSET and loc in _REQUIRED_HINT):
                lines.append(f"  missing: {_REQUIRED_HINT.get(loc, loc.upper())}")
            elif loc:
                lines.append(f"  {loc.upper()}: {msg}")
            else:
                lines.append(f"  {msg}")
        raise SettingsError(
            "Configuration is invalid. Set these in the environment "
            "(see .env.example):\n" + "\n".join(sorted(set(lines)))
        ) from exc


_settings: Settings | None = None


def get_settings() -> Settings:
    """The process-wide settings, loaded once on first use."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Install a settings object explicitly (boot path and tests)."""
    global _settings
    _settings = settings


def reset_settings() -> None:
    """Drop the cached settings so the next :func:`get_settings` reloads."""
    global _settings
    _settings = None


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean env var that is not part of :class:`Settings`."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
