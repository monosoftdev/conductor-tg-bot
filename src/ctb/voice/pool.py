"""One speech provider per tenant, built from that tenant's sealed key.

The same shape as :class:`ctb.conductor.pool.ClientPool` and for the same
reason: the key is decrypted once, handed to the provider, and never cached in
plaintext anywhere else. Rotating a tenant's key changes its fingerprint, which
misses the cache and retires the old provider.

Voice is the one place where data genuinely leaves the perimeter, so a tenant
with no ElevenLabs key of its own simply has no provider — there is deliberately
no platform-wide fallback key that would silently bill or expose one customer
through another's account.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from ctb.crypto import SecretBox
from ctb.db.repo.tenancy import TenantRow
from ctb.logging import get_logger
from ctb.voice.provider import ElevenLabsProvider, SpeechProvider

__all__ = ["ELEVENLABS_KEY_PURPOSE", "ProviderPool"]

#: AAD discriminator; a Conductor blob must not open as a speech one.
ELEVENLABS_KEY_PURPOSE = "elevenlabs_api_key"

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Key:
    tenant_id: uuid.UUID
    fingerprint: str
    model: str
    language: str


type ProviderFactory = Callable[[str, str, str], SpeechProvider]


def _default_factory(api_key: str, model: str, language: str) -> SpeechProvider:
    return ElevenLabsProvider(api_key, model=model, language=language)


class ProviderPool:
    """Tenant id → speech provider, or ``None`` when they have not set a key."""

    def __init__(
        self,
        secrets: SecretBox,
        *,
        model: str,
        language: str,
        factory: ProviderFactory = _default_factory,
    ) -> None:
        self._secrets = secrets
        self._model = model
        self._language = language
        self._factory = factory
        self._entries: dict[_Key, SpeechProvider] = {}
        self._lock = asyncio.Lock()

    async def get(self, tenant: TenantRow) -> SpeechProvider | None:
        if tenant.elevenlabs_key_ct is None or not tenant.elevenlabs_key_fp:
            return None
        key = _Key(
            tenant.id, tenant.elevenlabs_key_fp, self._model, self._language
        )
        async with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                return existing
            await self._evict_locked(tenant.id)
            plaintext = self._secrets.open(
                tenant.elevenlabs_key_ct,
                tenant_id=tenant.id,
                purpose=ELEVENLABS_KEY_PURPOSE,
            )
            try:
                provider = self._factory(plaintext, self._model, self._language)
            finally:
                del plaintext
            self._entries[key] = provider
            return provider

    async def forget(self, tenant_id: uuid.UUID) -> int:
        async with self._lock:
            return await self._evict_locked(tenant_id)

    async def aclose(self) -> None:
        async with self._lock:
            for provider in tuple(self._entries.values()):
                await self._close(provider)
            self._entries.clear()

    async def _evict_locked(self, tenant_id: uuid.UUID) -> int:
        doomed = [key for key in self._entries if key.tenant_id == tenant_id]
        for key in doomed:
            await self._close(self._entries.pop(key))
        return len(doomed)

    @staticmethod
    async def _close(provider: SpeechProvider) -> None:
        try:
            await provider.aclose()
        except Exception as exc:  # pragma: no cover - close is best effort
            _log.warning("voice_pool.close_failed", error=repr(exc))

    def health(self) -> dict[str, int]:
        return {"providers": len(self._entries)}
