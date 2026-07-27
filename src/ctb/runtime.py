"""The handful of objects that are genuinely one-per-process.

Everything tenant-shaped is resolved per update and reaches handlers through
:class:`~ctb.bot.middleware.tenancy.TenantContext`. What lives here is the
opposite: the things a tenant does not get its own copy of.

``system_database``
    The ``ctb_worker`` pool. It bypasses row-level security, so it is for the
    tables that *decide* scope (``tenants``, ``tenant_members``,
    ``tenant_chats``) and for the cross-tenant workers — supervisor reconcile,
    the delivery and voice claim loops, prune. A handler that reaches for this
    to read tenant data is a bug; handlers take ``db``, which is scoped.

``client_pool``
    Tenant id → Conductor client.

``provider_pool``
    Tenant id → speech provider. Registered so ``/revoke`` and ``/forget`` can
    evict a decrypted key rather than leaving it live in a header.

``secret_box``
    Envelope encryption for stored API keys.

These are explicit setters rather than lazy singletons: the boot path installs
them in a known order, and a test installs fakes. Reading one before boot
raises with the name of what is missing instead of quietly building a second.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime only
    from ctb.conductor.pool import ClientPool
    from ctb.crypto import SecretBox
    from ctb.db.connection import Database
    from ctb.voice.pool import ProviderPool

__all__ = [
    "client_pool",
    "provider_pool",
    "reset_runtime",
    "secret_box",
    "set_client_pool",
    "set_provider_pool",
    "set_secret_box",
    "set_system_database",
    "system_database",
]

_system_db: Database | None = None
_client_pool: ClientPool | None = None
_secret_box: SecretBox | None = None
_provider_pool: ProviderPool | None = None


def set_system_database(db: Database | None) -> None:
    global _system_db
    _system_db = db


def system_database() -> Database:
    """The BYPASSRLS worker pool. Never use it to serve a handler's read."""
    if _system_db is None:
        raise RuntimeError(
            "no system Database installed; call set_system_database() at boot"
        )
    return _system_db


def set_client_pool(pool: ClientPool | None) -> None:
    global _client_pool
    _client_pool = pool


def client_pool() -> ClientPool:
    if _client_pool is None:
        raise RuntimeError("no ClientPool installed; call set_client_pool() at boot")
    return _client_pool


def set_provider_pool(pool: ProviderPool | None) -> None:
    global _provider_pool
    _provider_pool = pool


def provider_pool() -> ProviderPool | None:
    """The speech-provider pool, or ``None`` when voice is off.

    Optional by design — voice is the one feature that can be disabled outright
    — so callers evicting a key must tolerate its absence rather than raise.
    """
    return _provider_pool


def set_secret_box(box: SecretBox | None) -> None:
    global _secret_box
    _secret_box = box


def secret_box() -> SecretBox:
    if _secret_box is None:
        raise RuntimeError("no SecretBox installed; call set_secret_box() at boot")
    return _secret_box


def reset_runtime() -> None:
    """Drop every installed handle. Test teardown."""
    set_system_database(None)
    set_client_pool(None)
    set_provider_pool(None)
    set_secret_box(None)
