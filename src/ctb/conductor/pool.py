"""One :class:`ConductorClient` per tenant, built on demand and cached.

The pool is where a sealed API key becomes a live client, and it is the only
place in the process that ever decrypts one. There is no plaintext cache: the
key goes straight into httpx's default headers and the local reference is
dropped. The *client* is the cache, and idle eviction destroys the last copy in
memory.

Three details carry weight:

**The cache key includes the key fingerprint.** Rotating a tenant's key changes
the fingerprint, so ``get()`` misses, the stale entry is evicted and closed, and
no caller has to remember to invalidate anything.

**One shared HTTP transport.** Per-tenant :class:`httpx.AsyncClient` objects own
the ``Authorization`` header and base URL; a single process-wide transport owns
the sockets. Without the :class:`_SharedTransport` wrapper, closing one tenant's
client would tear down every other tenant's keep-alive connections, because
``AsyncClient.aclose()`` closes the transport it was handed.

**Per-tenant limiters, one global gate.** The token bucket, circuit breaker and
auth-failure counter live on the client because they describe a *key*. The
semaphore that stops two hundred tenants times eight in-flight requests from
melting the process lives here.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import httpx

from ctb.conductor.client import ApiEventHook, ConductorClient
from ctb.crypto import SecretBox
from ctb.db.repo.tenancy import TenantRow
from ctb.logging import get_logger

__all__ = ["CONDUCTOR_KEY_PURPOSE", "ClientKey", "ClientPool", "MissingKeyError"]

#: AAD discriminator, so a Conductor blob cannot be opened as an ElevenLabs one.
CONDUCTOR_KEY_PURPOSE: Final = "conductor_api_key"

#: Sockets shared by every tenant. Comfortably above the per-tenant ceiling.
_MAX_CONNECTIONS: Final = 100
_MAX_KEEPALIVE: Final = 40

_log = get_logger(__name__)


class MissingKeyError(RuntimeError):
    """The tenant has not stored a Conductor API key yet."""


@dataclass(frozen=True, slots=True)
class ClientKey:
    """What makes two requests share a client. Rotation changes it."""

    tenant_id: uuid.UUID
    fingerprint: str
    api_url: str


@dataclass(slots=True)
class _Entry:
    client: ConductorClient
    last_used: float


class _SharedTransport(httpx.AsyncBaseTransport):
    """Delegates to the pool's transport and refuses to close it.

    ``httpx.AsyncClient.aclose()`` closes its transport. Per-tenant clients come
    and go; the sockets must not.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        """Intentionally a no-op; :meth:`ClientPool.aclose` owns the transport."""
        return None


class ClientPool:
    """Tenant id → live Conductor client, with idle eviction."""

    def __init__(
        self,
        secrets: SecretBox,
        *,
        on_event: ApiEventHook | None = None,
        max_clients: int = 200,
        idle_ttl_s: float = 900.0,
        global_inflight: int = 64,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._secrets = secrets
        self._on_event = on_event
        self._max_clients = max(1, max_clients)
        self._idle_ttl_s = idle_ttl_s
        self._clock = clock
        self._owns_transport = transport is None
        self._transport: httpx.AsyncBaseTransport = (
            transport
            or httpx.AsyncHTTPTransport(
                limits=httpx.Limits(
                    max_connections=_MAX_CONNECTIONS,
                    max_keepalive_connections=_MAX_KEEPALIVE,
                ),
                retries=0,
            )
        )
        self._gate = asyncio.Semaphore(max(1, global_inflight))
        self._entries: OrderedDict[ClientKey, _Entry] = OrderedDict()
        #: Tenants with a live poller. Never evicted while pinned.
        self._pinned: dict[uuid.UUID, int] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    # -- lookup ---------------------------------------------------------------

    async def get(self, tenant: TenantRow) -> ConductorClient:
        """The tenant's client, decrypting its key only if one must be built."""
        if tenant.conductor_key_ct is None or not tenant.conductor_key_fp:
            raise MissingKeyError(
                f"tenant {tenant.slug!r} has no Conductor API key stored"
            )
        key = ClientKey(tenant.id, tenant.conductor_key_fp, tenant.conductor_api_url)
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.last_used = self._clock()
                self._entries.move_to_end(key)
                return entry.client
            # A rotated key leaves a stale entry under the old fingerprint.
            await self._evict_locked(lambda k: k.tenant_id == tenant.id)
            client = self._build(tenant)
            self._entries[key] = _Entry(client, self._clock())
            await self._trim_locked()
            return client

    def peek(self, tenant_id: uuid.UUID) -> ConductorClient | None:
        """The cached client, if any. Never builds one, never decrypts."""
        for key, entry in self._entries.items():
            if key.tenant_id == tenant_id:
                return entry.client
        return None

    def _build(self, tenant: TenantRow) -> ConductorClient:
        plaintext = self._secrets.open(
            tenant.conductor_key_ct,
            tenant_id=tenant.id,
            purpose=CONDUCTOR_KEY_PURPOSE,
        )
        try:
            return ConductorClient(
                api_key=plaintext,
                api_url=tenant.conductor_api_url,
                tenant_id=tenant.id,
                transport=_SharedTransport(self._transport),
                on_event=self._on_event,
                global_semaphore=self._gate,
            )
        finally:
            # The header now holds the only copy we keep.
            del plaintext

    # -- lifetime -------------------------------------------------------------

    def pin(self, tenant_id: uuid.UUID) -> None:
        """Protect a tenant's client while it has a running poller."""
        self._pinned[tenant_id] = self._pinned.get(tenant_id, 0) + 1

    def unpin(self, tenant_id: uuid.UUID) -> None:
        remaining = self._pinned.get(tenant_id, 0) - 1
        if remaining > 0:
            self._pinned[tenant_id] = remaining
        else:
            self._pinned.pop(tenant_id, None)

    async def sweep(self) -> int:
        """Drop clients idle past the TTL. Returns how many were closed."""
        cutoff = self._clock() - self._idle_ttl_s
        async with self._lock:
            return await self._evict_locked(
                lambda key: (
                    key.tenant_id not in self._pinned
                    and self._entries[key].last_used < cutoff
                )
            )

    async def forget(self, tenant_id: uuid.UUID) -> int:
        """Close a tenant's client now — key revoked, tenant suspended."""
        async with self._lock:
            return await self._evict_locked(lambda key: key.tenant_id == tenant_id)

    async def aclose(self) -> None:
        async with self._lock:
            await self._evict_locked(lambda _key: True)
            if self._owns_transport and not self._closed:
                await self._transport.aclose()
            self._closed = True

    async def _evict_locked(self, predicate: Callable[[ClientKey], bool]) -> int:
        doomed = [key for key in tuple(self._entries) if predicate(key)]
        for key in doomed:
            entry = self._entries.pop(key)
            try:
                await entry.client.aclose()
            except Exception as exc:  # pragma: no cover - close is best effort
                _log.warning("client_pool.close_failed", error=repr(exc))
        return len(doomed)

    async def _trim_locked(self) -> None:
        """Bound the cache. Oldest unpinned entry goes first."""
        while len(self._entries) > self._max_clients:
            for key in self._entries:
                if key.tenant_id not in self._pinned:
                    await self._evict_locked(lambda k, target=key: k == target)
                    break
            else:  # pragma: no cover - every entry pinned is pathological
                _log.warning("client_pool.all_pinned", size=len(self._entries))
                return

    # -- introspection --------------------------------------------------------

    def clients(self) -> tuple[tuple[str, ConductorClient], ...]:
        """Live clients as ``(tenant id, client)``, for health aggregation."""
        return tuple(
            (str(key.tenant_id), entry.client) for key, entry in self._entries.items()
        )

    def health(self) -> dict[str, object]:
        return {
            "clients": len(self._entries),
            "pinned": len(self._pinned),
            "max_clients": self._max_clients,
            "idle_ttl_s": self._idle_ttl_s,
        }
