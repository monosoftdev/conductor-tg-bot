"""Configuration. Secrets come from the environment only, never from code.

This is *platform* configuration: the one shared Telegram bot, the two database
DSNs, and the master keys that seal tenant secrets. Anything a customer can
change — their Conductor key, their agent/model defaults, whether voice is on —
lives in the ``tenants`` table and arrives as
:class:`ctb.bot.middleware.tenancy.TenantSettings`. Nothing in the environment
identifies a tenant any more.

Boot fails loudly and immediately when a required variable is missing. All of
them are checked in *field* validators so a first deploy reports every problem
in one message; fixing them one crash at a time is its own kind of outage.
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from ctb.crypto import SecretBox, SecretError, parse_master_keys

__all__ = [
    "DEFAULT_CONDUCTOR_API_URL",
    "Settings",
    "SettingsError",
    "env_flag",
    "get_settings",
    "load_settings",
    "reset_settings",
    "set_settings",
]

DEFAULT_CONDUCTOR_API_URL = "https://api.conductor.build/v0"


class SettingsError(RuntimeError):
    """Configuration is missing or invalid. Raised at boot, never caught."""


_REQUIRED_HINT = {
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN — from @BotFather",
    "database_url": (
        "DATABASE_URL — PostgreSQL URL for the ctb_app role (row-level security "
        "applies)"
    ),
    "system_database_url": (
        "SYSTEM_DATABASE_URL — PostgreSQL URL for the ctb_worker role "
        "(BYPASSRLS; used by the supervisor and claim loops)"
    ),
    "master_keys": (
        "CTB_MASTER_KEYS — '<kid>:<base64 32 bytes>[,<older kid>:...]'; "
        "generate one with: python -m ctb.rewrap --new-key v1"
    ),
}

#: Marker a *field* validator raises for "set, but empty". pydantic only reports
#: ``missing`` for absent fields, and a model validator never runs while another
#: field is missing — so a blank check that lives anywhere else would surface on
#: the *next* deploy instead of this one. Field validators all run, so every
#: required var lands in one message.
_UNSET = "unset"


def _stripped(value: SecretStr) -> SecretStr:
    """Drop whitespace a paste into a hosting dashboard leaves behind.

    A trailing newline or space rides into the auth header verbatim and comes
    back as a 401 that looks exactly like a wrong key — observed on a real
    deploy. Cheaper to strip than to diagnose. Tenant API keys get the same
    treatment in ``/key``, which is where *those* are pasted.
    """
    raw = value.get_secret_value()
    trimmed = raw.strip()
    return value if trimmed == raw else SecretStr(trimmed)


class Settings(BaseSettings):
    """Everything the *platform* reads from the environment.

    Field names map to the upper-cased env var of the same name (see
    ``.env.example``), except ``master_keys``, which is ``CTB_MASTER_KEYS``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # -- secrets ---------------------------------------------------------------
    #: The one shared bot every tenant adds to their own group.
    telegram_bot_token: SecretStr
    #: Ordered; the first seals new writes, all of them can open. See ctb.crypto.
    master_keys: SecretStr = Field(alias="ctb_master_keys")

    # -- storage ---------------------------------------------------------------
    #: The ``ctb_app`` role. Row-level security applies; every query needs a
    #: tenant in scope.
    database_url: SecretStr
    #: The ``ctb_worker`` role, which holds BYPASSRLS. Cross-tenant workers only.
    system_database_url: SecretStr
    db_pool_max: int = Field(default=10, ge=1, le=100)
    system_db_pool_max: int = Field(default=6, ge=1, le=100)

    # -- Conductor -------------------------------------------------------------
    #: Only the default for a new tenant; each tenant stores its own.
    conductor_api_url: str = DEFAULT_CONDUCTOR_API_URL

    # -- platform operators ----------------------------------------------------
    #: Who may run platform-wide commands (``/platform``): suspend a tenant, read
    #: global health. Empty is legitimate — an unattended deployment has none.
    #: This is *not* an allowlist for using the bot; tenancy decides that.
    platform_admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # -- registration ----------------------------------------------------------
    #: Self-serve sign-up. Turn it off to run a closed instance; existing
    #: tenants keep working either way.
    registration_open: bool = True
    #: Ceiling on new tenants per rolling hour, so a script cannot enumerate the
    #: bot into a support incident.
    registration_rate_per_hour: int = Field(default=20, ge=1, le=10_000)

    # -- Telegram voice notes --------------------------------------------------
    #: Platform kill switch. A tenant can still turn voice off for itself, but
    #: it can never turn it on when this is false.
    voice_enabled: bool = False
    voice_stt_provider: Literal["elevenlabs"] = "elevenlabs"
    voice_stt_model: str = "scribe_v2"
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

    # -- defaults offered to a brand-new tenant --------------------------------
    default_agent: str = "claude"
    default_model: str = "opus-5-1m"
    default_effort: str = "high"
    default_branch: str = "main"

    # -- logging ---------------------------------------------------------------
    #: Transcript content is the user's source code. Keep this false outside of
    #: active debugging.
    log_transcript_content: bool = False
    log_level: str = "INFO"

    @field_validator("platform_admin_ids", mode="before")
    @classmethod
    def _parse_id_list(cls, value: Any) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, int):
            return [value]
        if isinstance(value, str):
            out: list[int] = []
            for part in (p.strip() for p in value.replace(";", ",").split(",")):
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

    @field_validator("platform_admin_ids", mode="after")
    @classmethod
    def _dedupe_preserving_order(cls, value: list[int]) -> list[int]:
        seen: set[int] = set()
        out: list[int] = []
        for uid in value:
            if uid not in seen:
                seen.add(uid)
                out.append(uid)
        return out

    @field_validator(
        "telegram_bot_token", "database_url", "system_database_url", mode="after"
    )
    @classmethod
    def _reject_blank_secret(cls, value: SecretStr) -> SecretStr:
        # ``TELEGRAM_BOT_TOKEN=`` is set as far as pydantic is concerned; the
        # failure would otherwise arrive later, from Telegram, as a 401.
        if not value.get_secret_value().strip():
            raise ValueError(_UNSET)
        return _stripped(value)

    @field_validator("master_keys", mode="after")
    @classmethod
    def _parse_master_keys(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if not raw:
            raise ValueError(_UNSET)
        try:
            parse_master_keys(raw)
        except SecretError as exc:
            raise ValueError(str(exc)) from exc
        return _stripped(value)

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

    @field_validator("default_branch", mode="after")
    @classmethod
    def _branch_or_main(cls, value: str) -> str:
        # ``DEFAULT_BRANCH=`` in a copied .env must not offer a nameless button.
        return value.strip() or "main"

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

    # -- derived ---------------------------------------------------------------

    def secret_box(self) -> SecretBox:
        """Build the envelope-encryption box. Validated at load time."""
        return SecretBox.from_env_value(self.master_keys.get_secret_value())

    def is_platform_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.platform_admin_ids

    def secret_values(self) -> tuple[str, ...]:
        """Platform secrets, for the log scrubber to redact on sight.

        Tenant API keys are deliberately absent: registering every tenant's key
        in a process-wide set would keep plaintext in memory for the life of the
        process and make scrubbing O(tenants) per log line. Those are redacted
        by pattern instead — see :mod:`ctb.logging`.
        """
        values = [
            self.telegram_bot_token.get_secret_value(),
            self.master_keys.get_secret_value(),
        ]
        for dsn in (self.database_url, self.system_database_url):
            password = urlsplit(dsn.get_secret_value()).password
            if password:
                values.append(password)
        return tuple(v for v in values if v)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Settings(api={self.conductor_api_url!r}, "
            f"registration_open={self.registration_open}, "
            f"admins={len(self.platform_admin_ids)})"
        )


def conductor_api_root(api_url: str) -> str:
    """The API root (no ``/v0``). ``GET /me`` lives here and only here."""
    parts = urlsplit(api_url)
    return f"{parts.scheme}://{parts.netloc}"


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
            key = _ALIAS_TO_FIELD.get(loc, loc)
            msg = err["msg"].removeprefix("Value error, ")
            if err["type"] == "missing" or (msg == _UNSET and key in _REQUIRED_HINT):
                lines.append(f"  missing: {_REQUIRED_HINT.get(key, key.upper())}")
            elif loc:
                lines.append(f"  {_FIELD_TO_ENV.get(key, key.upper())}: {msg}")
            else:
                lines.append(f"  {msg}")
        raise SettingsError(
            "Configuration is invalid. Set these in the environment "
            "(see .env.example):\n" + "\n".join(sorted(set(lines)))
        ) from exc


#: Field name <-> environment variable, for aliased fields. pydantic reports
#: whichever name the caller used; the operator only knows the env var.
_ALIAS_TO_FIELD = {"ctb_master_keys": "master_keys"}
_FIELD_TO_ENV = {"master_keys": "CTB_MASTER_KEYS"}


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
