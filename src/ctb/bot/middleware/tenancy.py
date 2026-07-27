"""Resolve every update to a tenant, or drop it. This is the security boundary.

One shared bot serves every customer, so "who is this" is decided here and
nowhere else. Four rules:

* **Every update type is checked.** Registered as an *outer* middleware on
  ``dp.update``, the single funnel, so a new Bot API update kind is denied by
  default rather than falling through.
* **A chat belongs to exactly one tenant.** ``tenant_chats.chat_id`` is a
  primary key. A group nobody has bound is not "unknown yet", it is refused.
* **Membership is checked separately from the chat.** Being in the right group
  is not enough; the sender must also be a ``tenant_members`` row. A stranger
  added to a customer's supergroup gets silence.
* **Rejection is silence.** Replying "you are not authorised" tells a stranger
  the bot exists and the token is live. The exception is the registration
  entry point, which :class:`TenantMiddleware` lets through unresolved so
  ``/start`` can answer.

Resolution happens *before* :class:`~ctb.bot.middleware.routing.RoutingMiddleware`
and before aiogram's FSM middleware, because both read the database and the
database refuses a tenant-scoped query with no tenant in scope. Downstream of
this middleware there is always a scope; upstream there is never a query.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Final

from aiogram import Bot
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.dispatcher.middlewares.user_context import (
    EVENT_CONTEXT_KEY,
    EventContext,
    UserContextMiddleware,
)
from aiogram.types import Chat, TelegramObject, Update, User

from ctb.conductor.client import ConductorClient
from ctb.conductor.pool import ClientPool, MissingKeyError
from ctb.db.connection import Database, reset_tenant, set_tenant
from ctb.db.repo import tenancy
from ctb.db.repo.tenancy import TenantMember, TenantRow
from ctb.logging import get_logger
from ctb.settings import Settings

__all__ = [
    "MAX_STRANGER_NOTICES_PER_WINDOW",
    "STRANGER_NOTICE_INTERVAL_S",
    "TENANT_CACHE_TTL_S",
    "Principal",
    "StrangerNotifier",
    "TenantContext",
    "TenantMiddleware",
    "TenantSettings",
    "current_tenancy",
    "forget_cached",
    "set_tenancy",
    "UNRESOLVED_COMMANDS",
    "UNRESOLVED_GROUP_COMMANDS",
]

log = get_logger(__name__)

#: The installed middleware, so a handler that changes a key or a role can
#: expire the resolution cache immediately instead of waiting out its TTL.
_INSTALLED: TenantMiddleware | None = None


def set_tenancy(middleware: TenantMiddleware | None) -> None:
    global _INSTALLED
    _INSTALLED = middleware


def current_tenancy() -> TenantMiddleware | None:
    return _INSTALLED


def forget_cached(tenant_id: uuid.UUID) -> None:
    """Expire this workspace's resolution cache immediately.

    Resolution is cached for 30 seconds. Anything that changes what a
    resolution *means* — a stored key, a revoked key, a role, a membership —
    calls this, because for ``/revoke`` the alternative is a key you just
    deleted continuing to work for half a minute.
    """
    middleware = current_tenancy()
    if middleware is not None:
        middleware.invalidate(tenant_id)


#: One notice per unknown user per day.
STRANGER_NOTICE_INTERVAL_S: Final = 24 * 60 * 60.0
#: ...and never more than this many per window, however many strangers arrive.
MAX_STRANGER_NOTICES_PER_WINDOW: Final = 20
#: Bound on the "already told them" table. Eviction risks a duplicate notice,
#: never a missed rejection.
_MAX_TRACKED_STRANGERS: Final = 4096

#: How long a resolved tenant is reused without re-reading the row. Short
#: enough that a suspension takes effect promptly, long enough that a burst of
#: updates is three lookups rather than three hundred.
TENANT_CACHE_TTL_S: Final = 30.0

#: How often one chat is told its workspace is suspended. Long: the state does
#: not change on its own, and the notice is a courtesy, not an alert.
SUSPENDED_NOTICE_INTERVAL_S: Final = 6 * 60 * 60.0

#: Commands a *non-member* may run in a **private chat**, because they are how
#: someone becomes one.
#: ``/platform`` is here because an operator of a fresh instance owns no
#: workspace, and "suspend that customer, now" must not require registering a
#: decoy one first. The handler still gates on ``PLATFORM_ADMIN_IDS``.
UNRESOLVED_COMMANDS: Final = frozenset(
    {"/start", "/register", "/help", "/privacy", "/platform"}
)

#: The one command a non-member may run in a **group**. Binding a fresh
#: supergroup is impossible otherwise — it has no ``tenant_chats`` row yet, which
#: is exactly what ``/setup`` is for. It is safe because it does nothing without
#: a valid single-use code, which only its own workspace's owner ever saw.
UNRESOLVED_GROUP_COMMANDS: Final = frozenset({"/setup"})


@dataclass(frozen=True, slots=True)
class Principal:
    """Who sent the update, once tenancy has had its say."""

    user_id: int | None
    is_owner: bool = False
    #: ``"member"`` (a seated user) or ``"anonymous"`` (the registration path).
    source: str = "member"

    @property
    def is_allowed(self) -> bool:
        return self.user_id is not None


@dataclass(frozen=True, slots=True)
class TenantSettings:
    """Per-tenant knobs that used to be environment variables."""

    default_agent: str = "claude"
    default_model: str = "opus-5-1m"
    default_effort: str = "high"
    default_branch: str = "main"
    voice_enabled: bool = False
    voice_mode: str = "prompts"
    max_pollers: int = 20
    max_pending_deliveries: int = 5000
    max_workspaces: int = 50

    @classmethod
    def of(cls, tenant: TenantRow, platform: Settings) -> TenantSettings:
        """Tenant values, with the platform's voice kill switch applied.

        A tenant can turn voice off for itself; it can never turn it on when the
        platform has no speech vendor configured.
        """
        return cls(
            default_agent=tenant.default_agent,
            default_model=tenant.default_model,
            default_effort=tenant.default_effort,
            default_branch=tenant.default_branch,
            voice_enabled=tenant.voice_enabled and platform.voice_enabled,
            voice_mode=tenant.voice_mode,
            max_pollers=tenant.max_pollers,
            max_pending_deliveries=tenant.max_pending_deliveries,
            max_workspaces=tenant.max_workspaces,
        )


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Everything a handler needs about *whose* request this is.

    Injected into handlers as ``tenant`` by name. A handler that needs the
    Conductor API takes this and uses :attr:`client`; there is no other way to
    reach one, which is what makes a cross-organisation read impossible to write
    by accident.
    """

    tenant_id: uuid.UUID
    slug: str
    status: str
    role: str
    user_id: int
    owner_ids: tuple[int, ...]
    primary_chat_id: int | None
    settings: TenantSettings
    row: TenantRow
    _client: ConductorClient | None = None

    @property
    def client(self) -> ConductorClient:
        """The tenant's Conductor client."""
        if self._client is None:
            raise MissingKeyError(
                "this workspace has no Conductor API key yet — send /key in a "
                "private message to set one"
            )
        return self._client

    @property
    def has_client(self) -> bool:
        return self._client is not None

    @property
    def is_owner(self) -> bool:
        """Owners *and* admins pass the owner-only command gate."""
        return self.role in ("owner", "admin")

    @property
    def is_active(self) -> bool:
        return self.status == "active"


class StrangerNotifier:
    """Tells the people who care that an unknown user knocked, sparingly.

    Extracted from the old ``AuthMiddleware`` unchanged in spirit: once per
    stranger per day, with a hard cap per window so a scripted flood cannot turn
    someone's DM into the attacker's megaphone.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
        interval_s: float = STRANGER_NOTICE_INTERVAL_S,
        max_per_window: int = MAX_STRANGER_NOTICES_PER_WINDOW,
    ) -> None:
        self._enabled = enabled
        self._clock = clock
        self._interval = interval_s
        self._max = max_per_window
        self._notified: dict[int, float] = {}
        self._window_start: float | None = None
        self._window_count = 0
        self.sent = 0

    def _should_send(self, user_id: int) -> bool:
        now = self._clock()
        last = self._notified.get(user_id)
        if last is not None and now - last < self._interval:
            return False
        if self._window_start is None or now - self._window_start >= self._interval:
            self._window_start = now
            self._window_count = 0
        if self._window_count >= self._max:
            return False
        # Record *before* awaiting: two concurrent updates from the same
        # stranger must not both decide they are the first.
        if len(self._notified) >= _MAX_TRACKED_STRANGERS:
            oldest = min(self._notified, key=lambda uid: self._notified[uid])
            del self._notified[oldest]
        self._notified[user_id] = now
        self._window_count += 1
        return True

    async def notify(
        self, bot: Any, user: User, recipients: tuple[int, ...], detail: str
    ) -> None:
        if not self._enabled or not isinstance(bot, Bot) or not recipients:
            return
        if not self._should_send(user.id):
            return
        handle = f" @{user.username}" if user.username else ""
        text = (
            "🚫 Ignored an update from someone who is not in this workspace.\n"
            f"id <code>{user.id}</code>{handle} · {detail}\n"
            f"Add them with <code>/invite {user.id}</code>."
        )
        for chat_id in recipients:
            try:
                await bot.send_message(
                    chat_id=chat_id, text=text, disable_notification=True
                )
            except Exception as exc:
                # They may never have started the bot. Not this path's problem.
                log.warning("tenancy.stranger_notice_failed", error=repr(exc))
                continue
            self.sent += 1
            return


@dataclass(slots=True)
class _CacheEntry:
    tenant: TenantRow
    member: TenantMember
    primary_chat_id: int | None
    at: float


class TenantMiddleware(BaseMiddleware):
    """Resolve the tenant, publish the scope, and refuse everything else."""

    def __init__(
        self,
        *,
        system_db: Database,
        clients: ClientPool,
        settings: Settings,
        notifier: StrangerNotifier | None = None,
        cache_ttl_s: float = TENANT_CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = system_db
        self._clients = clients
        self._settings = settings
        self._notifier = notifier or StrangerNotifier()
        self._cache_ttl = cache_ttl_s
        self._clock = clock
        self._cache: dict[tuple[int, int], _CacheEntry] = {}
        #: One "this workspace is suspended" line per chat per interval. A
        #: suspended group can still be busy, and repeating the notice on every
        #: message would make the bot the noisiest thing in it.
        self._suspend_notices: dict[int, float] = {}
        #: Counters for ``/health``.
        self.rejected = 0
        self.resolved = 0
        self.unresolved_allowed = 0
        set_tenancy(self)

    # -- the boundary ---------------------------------------------------------

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = _user_of(event, data)
        chat = _chat_of(event, data)
        if user is None:
            return None  # service events carry no sender: nothing to authorise

        resolved = await self._resolve(chat, user)
        if resolved is None:
            return await self._unresolved(handler, event, data, user, chat)

        entry = resolved
        tenant = entry.tenant
        if tenant.status in ("suspended", "deleted"):
            self.rejected += 1
            log.warning("tenancy.suspended", tenant=tenant.slug, user_id=user.id)
            # Silence is the rule for *strangers*. This is a known customer in
            # a known state, and going completely mute — commands and buttons
            # alike — reads as an outage. One sentence, rate-limited by the
            # same notifier that bounds stranger notices.
            await self._say_suspended(data.get("bot"), user, chat)
            return None

        context = TenantContext(
            tenant_id=tenant.id,
            slug=tenant.slug,
            status=tenant.status,
            role=entry.member.role,
            user_id=user.id,
            owner_ids=await self._owner_ids(tenant.id),
            primary_chat_id=entry.primary_chat_id,
            settings=TenantSettings.of(tenant, self._settings),
            row=tenant,
            _client=await self._client_for(tenant),
        )

        self.resolved += 1
        token = set_tenant(tenant.id)
        try:
            data["tenant"] = context
            data["principal"] = Principal(
                user_id=user.id, is_owner=context.is_owner, source="member"
            )
            # Handlers written before tenancy read ``is_owner`` by name.
            data["is_owner"] = context.is_owner
            return await handler(event, data)
        finally:
            reset_tenant(token)

    # -- resolution -----------------------------------------------------------

    async def _resolve(self, chat: Chat | None, user: User) -> _CacheEntry | None:
        """``chat_id`` decides the tenant; membership decides the person.

        Some update types — inline queries, poll answers, payment callbacks —
        carry a sender but no chat at all. They resolve the same way a private
        message does: by the sender's own membership, and only when it is
        unambiguous.
        """
        cache_key = (0 if chat is None else chat.id, user.id)
        cached = self._cache.get(cache_key)
        if cached is not None and self._clock() - cached.at < self._cache_ttl:
            return cached

        try:
            binding = (
                None if chat is None else await tenancy.chat_for(self._db, chat.id)
            )
            if binding is None and (chat is None or chat.type == "private"):
                binding = await self._sole_membership_binding(user)
            if binding is None:
                return None
            member = await tenancy.member(self._db, binding.tenant_id, user.id)
            if member is None:
                return None
            tenant = await tenancy.get(self._db, binding.tenant_id)
            if tenant is None:  # pragma: no cover - FK makes this unreachable
                return None
            primary = await tenancy.primary_chat(self._db, tenant.id)
        except Exception as exc:
            # A broken database must not open the door.
            log.error("tenancy.lookup_failed", error=repr(exc))
            return None

        entry = _CacheEntry(
            tenant=tenant,
            member=member,
            primary_chat_id=None if primary is None else primary.chat_id,
            at=self._clock(),
        )
        self._remember(cache_key, entry)
        return entry

    async def _sole_membership_binding(self, user: User) -> tenancy.TenantChat | None:
        """Which workspace does this person's DM belong to?

        One membership is unambiguous. With several, prefer the ones they
        **own** — anyone can seat anyone with ``/invite``, and without this
        preference a stranger could seat a victim and jam their DMs, including
        the ``/key`` they need to finish their own sign-up. Ties among owned
        workspaces stay ambiguous and are refused, because a prompt must never
        silently land in the wrong organisation.
        """
        memberships = await tenancy.memberships_for_user(self._db, user.id)
        if not memberships:
            return None
        owned = [seat for seat in memberships if seat.role == "owner"]
        candidates = owned or memberships
        if len(candidates) != 1:
            return None
        chosen = candidates[0]
        return tenancy.TenantChat(
            chat_id=0, tenant_id=chosen.tenant_id, kind="dm", is_primary=False
        )

    def _remember(self, key: tuple[int, int], entry: _CacheEntry) -> None:
        if len(self._cache) >= _MAX_TRACKED_STRANGERS:
            oldest = min(self._cache, key=lambda k: self._cache[k].at)
            del self._cache[oldest]
        self._cache[key] = entry

    def invalidate(self, tenant_id: uuid.UUID | None = None) -> None:
        """Forget cached resolutions after a membership or status change."""
        if tenant_id is None:
            self._cache.clear()
            return
        for key in [k for k, v in self._cache.items() if v.tenant.id == tenant_id]:
            del self._cache[key]

    async def _owner_ids(self, tenant_id: uuid.UUID) -> tuple[int, ...]:
        try:
            return await tenancy.list_owner_ids(self._db, tenant_id)
        except Exception as exc:  # pragma: no cover - a read that just failed
            log.warning("tenancy.owner_lookup_failed", error=repr(exc))
            return ()

    async def _client_for(self, tenant: TenantRow) -> ConductorClient | None:
        """``None`` when no key is stored yet, so ``/key`` still works."""
        if not tenant.has_conductor_key:
            return None
        try:
            return await self._clients.get(tenant)
        except Exception as exc:
            log.error("tenancy.client_unavailable", tenant=tenant.slug, error=repr(exc))
            return None

    # -- the unresolved path --------------------------------------------------

    async def _unresolved(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
        user: User,
        chat: Chat | None,
    ) -> Any:
        """No tenant. Only the commands that *create* one get through."""
        if chat is not None and _is_entry_command(event, chat.type):
            self.unresolved_allowed += 1
            data["tenant"] = None
            data["principal"] = Principal(user_id=user.id, source="anonymous")
            data["is_owner"] = False
            return await handler(event, data)

        self.rejected += 1
        log.warning(
            "bot.update_rejected",
            user_id=user.id,
            username=user.username,
            chat_id=None if chat is None else chat.id,
            update_type=_update_type(event),
        )
        await self._notify_owners(data.get("bot"), user, chat, event)
        return None  # silence

    async def _say_suspended(self, bot: Any, user: User, chat: Chat | None) -> None:
        """Tell a suspended workspace's member why nothing is happening."""
        if bot is None or chat is None:
            return
        now = self._clock()
        last = self._suspend_notices.get(chat.id)
        if last is not None and now - last < SUSPENDED_NOTICE_INTERVAL_S:
            return
        self._suspend_notices[chat.id] = now
        with suppress(Exception):
            await bot.send_message(
                chat_id=chat.id,
                text=(
                    "This team is suspended, so I am not acting on "
                    "messages here. Contact whoever runs this instance."
                ),
                message_thread_id=None,
            )

    async def _notify_owners(
        self, bot: Any, user: User, chat: Chat | None, event: TelegramObject
    ) -> None:
        """Tell the *chat's* owners, if the chat belongs to someone."""
        if chat is None or chat.type == "private":
            return
        # A bot is not a person anyone can invite, so reporting one as an
        # unknown *user* is noise the owner can act on in no way. Most of these
        # are this bot's own doing: creating, renaming or closing a forum topic
        # posts a service message into that topic authored by the bot, which
        # Telegram delivers straight back — so an owner's own /new or /setup
        # made the bot report itself as a stranger. `GroupAnonymousBot` is the
        # same shape: an admin posting anonymously is attributed to a bot,
        # correctly refused, and not worth waking anyone for.
        #
        # Still dropped either way. Only the alert is suppressed, and the
        # rejection above is still logged.
        if user.is_bot:
            return
        try:
            binding = await tenancy.chat_for(self._db, chat.id)
            if binding is None:
                return
            recipients = await tenancy.list_owner_ids(self._db, binding.tenant_id)
        except Exception:  # pragma: no cover - already-degraded database
            return
        await self._notifier.notify(bot, user, recipients, _update_type(event))

    def health(self) -> dict[str, int]:
        return {
            "resolved": self.resolved,
            "rejected": self.rejected,
            "unresolved_allowed": self.unresolved_allowed,
            "notices_sent": self._notifier.sent,
            "cache": len(self._cache),
        }


# ── event helpers ────────────────────────────────────────────────────────────


def _user_of(event: TelegramObject, data: dict[str, Any]) -> User | None:
    """The sender, from aiogram's event context with a direct fallback.

    :class:`~aiogram.dispatcher.middlewares.user_context.UserContextMiddleware`
    is registered ahead of us by ``Dispatcher.__init__`` and normally fills
    ``event_context``. The fallback keeps the check working if this middleware
    is ever used outside a Dispatcher (tests, a future webhook path).
    """
    context = data.get(EVENT_CONTEXT_KEY)
    if isinstance(context, EventContext):
        return context.user
    if isinstance(event, Update):
        return UserContextMiddleware.resolve_event_context(event).user
    return None


def _chat_of(event: TelegramObject, data: dict[str, Any]) -> Chat | None:
    context = data.get(EVENT_CONTEXT_KEY)
    if isinstance(context, EventContext):
        return context.chat
    if isinstance(event, Update):
        return UserContextMiddleware.resolve_event_context(event).chat
    return None


def _is_entry_command(event: TelegramObject, chat_type: str) -> bool:
    """Is this one of the few commands a non-member may run, here?"""
    message = event.message if isinstance(event, Update) else None
    text = (getattr(message, "text", None) or "").strip()
    if not text.startswith("/"):
        return False
    word = text.split(maxsplit=1)[0].split("@", 1)[0].casefold()
    allowed = (
        UNRESOLVED_COMMANDS if chat_type == "private" else UNRESOLVED_GROUP_COMMANDS
    )
    return word in allowed


def _update_type(event: TelegramObject) -> str:
    if isinstance(event, Update):
        try:
            return event.event_type
        except Exception:  # pragma: no cover - unknown future update kind
            return "unknown"
    return type(event).__name__
