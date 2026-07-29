"""Tenant → GitHub client, opened from that tenant's own sealed token.

The same shape as :class:`ctb.voice.pool.ProviderPool`, for the same reason:
plaintext exists inside :meth:`get` and nowhere else, the cache key carries the
key's fingerprint so a rotated token misses it, and there is deliberately **no
shared fallback**. A borrowed token would read another customer's private
source — the precise cross-tenant read the rest of this codebase is built to
make impossible.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from ctb.crypto import SecretBox
from ctb.db.repo.tenancy import TenantRow
from ctb.github.client import GitHubClient
from ctb.logging import get_logger

__all__ = ["GITHUB_KEY_PURPOSE", "GitHubPool"]

#: AAD discriminator; a Conductor or speech blob must not open as a GitHub one.
GITHUB_KEY_PURPOSE = "github_api_token"

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Key:
    tenant_id: uuid.UUID
    fingerprint: str


type ClientFactory = Callable[[str], GitHubClient]


def _default_factory(token: str) -> GitHubClient:
    return GitHubClient(token=token)


class GitHubPool:
    """Tenant → client, or ``None`` when that team has stored no token."""

    def __init__(
        self,
        secrets: SecretBox,
        *,
        factory: ClientFactory = _default_factory,
    ) -> None:
        self._secrets = secrets
        self._factory = factory
        self._entries: dict[_Key, GitHubClient] = {}
        self._lock = asyncio.Lock()

    async def get(self, tenant: TenantRow) -> GitHubClient | None:
        if tenant.github_key_ct is None or not tenant.github_key_fp:
            return None
        key = _Key(tenant.id, tenant.github_key_fp)
        async with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                return existing
            await self._evict_locked(tenant.id)
            plaintext = self._secrets.open(
                tenant.github_key_ct,
                tenant_id=tenant.id,
                purpose=GITHUB_KEY_PURPOSE,
            )
            try:
                client = self._factory(plaintext)
            finally:
                del plaintext
            self._entries[key] = client
            return client

    async def forget(self, tenant_id: uuid.UUID) -> int:
        async with self._lock:
            return await self._evict_locked(tenant_id)

    async def aclose(self) -> None:
        async with self._lock:
            for client in tuple(self._entries.values()):
                await self._close(client)
            self._entries.clear()

    async def _evict_locked(self, tenant_id: uuid.UUID) -> int:
        doomed = [key for key in self._entries if key.tenant_id == tenant_id]
        for key in doomed:
            await self._close(self._entries.pop(key))
        return len(doomed)

    async def _close(self, client: GitHubClient) -> None:
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - closing never fails a caller
            _log.warning("github.close_failed", error=repr(exc))
