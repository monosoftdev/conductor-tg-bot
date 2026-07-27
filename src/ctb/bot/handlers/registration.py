"""Self-serve sign-up: become a workspace, bind a group, store a key.

These are the only commands a non-member can reach, and only in a private
chat — :class:`~ctb.bot.middleware.tenancy.TenantMiddleware` lets exactly this
set through unresolved. Everything else about someone with no workspace is
silence.

The flow is deliberately three explicit steps rather than one wizard:

1. ``/register <name>`` creates a *pending* workspace with you as its owner.
2. ``/setup <code>`` in your supergroup binds that chat. The code is issued in
   the private chat and hashed at rest, because a shared bot can be added to
   any group by anyone — being added is not consent, and without the code
   somebody could bind the bot to a workspace that is not theirs.
3. ``/key`` stores your Conductor API key and activates the workspace.

**The key never stays in Telegram.** Sent in a group it is refused *and*
deleted, with a rotate-it warning. Sent in a private chat it is validated,
sealed, stored, and the message that carried it is deleted. Telegram keeps
message history forever; a key pasted into a chat would sit there forever too.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Final

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ctb.bot.app import register_router
from ctb.bot.handlers.common import abandon_wizard, command_text, short_error, tell
from ctb.bot.handlers.topics import (
    TopicCreateError,
    discard_topic,
    forum_support,
    require_topic,
    resolve_db,
)
from ctb.bot.middleware.tenancy import TenantContext
from ctb.conductor.client import ConductorClient
from ctb.conductor.pool import CONDUCTOR_KEY_PURPOSE
from ctb.db.connection import Database, tenant_scope
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import tenancy
from ctb.delivery.render.html import escape
from ctb.logging import get_logger
from ctb.runtime import secret_box, system_database
from ctb.settings import Settings
from ctb.voice.pool import ELEVENLABS_KEY_PURPOSE

router = Router(name=__name__)
register_router(router, order=5)

log = get_logger(__name__)

#: How long a binding code is good for. Long enough to switch apps, short
#: enough that a screenshot in a scrollback is not a standing invitation.
CLAIM_TTL_MS: Final = 15 * 60 * 1000

#: Slugs appear in logs and in ``/members``; keep them boring and unambiguous.
_SLUG_RE: Final = re.compile(r"[^a-z0-9]+")
_SLUG_MAX: Final = 24

#: Codes are shown once and stored only as a digest.
_CODE_BYTES: Final = 9

_WELCOME: Final = (
    "<b>Conductor from your phone</b>\n"
    "Drive your Conductor cloud agents from Telegram.\n\n"
    "You bring your own Conductor API key; your workspaces, transcripts and "
    "spending stay yours.\n\n"
    "<code>/register your-team-name</code> to begin."
)

_CLOSED: Final = (
    "Sign-up is closed on this instance. Ask whoever runs it for an invitation."
)

_NEXT_STEPS: Final = (
    "1 · Create a <b>private supergroup</b> and turn on <b>Topics</b>.\n"
    "2 · Add this bot as an administrator: manage topics, pin, delete, send.\n"
    "3 · In that group send <code>/setup {code}</code>\n\n"
    "The code expires in 15 minutes and works once."
)


def slugify(name: str) -> str:
    """A short, lowercase handle. Never the raw name, which may be anything."""
    slug = _SLUG_RE.sub("-", name.strip().casefold()).strip("-")
    return slug[:_SLUG_MAX] or "workspace"


def hash_code(code: str) -> str:
    """Only the digest is stored, so a database read cannot bind anything."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


async def issue_code(
    db: Database, *, tenant_id: object, user_id: int, purpose: str
) -> str:
    """Mint a single-use code and store only its hash."""
    code = secrets.token_urlsafe(_CODE_BYTES)
    await tenancy.create_enrollment_token(
        db,
        token_hash=hash_code(code),
        tenant_id=tenant_id,  # type: ignore[arg-type]
        user_id=user_id,
        purpose=purpose,
        ttl_ms=CLAIM_TTL_MS,
    )
    return code


@router.message(Command("start"))
async def start(message: Message, state: FSMContext) -> None:
    await abandon_wizard(state)
    await tell(message, _WELCOME)


@router.message(Command("privacy"))
async def privacy(message: Message, state: FSMContext) -> None:
    """What this service holds on your behalf. Short, and true."""
    await abandon_wizard(state)
    await tell(
        message,
        "<b>What is stored</b>\n"
        "• Your Conductor API key, encrypted (AES-256-GCM).\n"
        "• Workspace, session and delivery bookkeeping.\n"
        "• Transcript text, capped and deleted after 30 days.\n\n"
        "<b>What leaves</b>\n"
        "• Requests to Conductor, with your key.\n"
        "• Voice notes to the speech vendor — only if you enable voice and "
        "store your own key for it.\n\n"
        "<code>/revoke</code> deletes your key. <code>/forget</code> deletes "
        "the workspace and everything in it.",
    )


@router.message(Command("register"))
async def register(
    message: Message,
    settings: Settings,
    state: FSMContext,
    tenant: TenantContext | None = None,
) -> None:
    """Create a workspace and return the code that binds a group to it."""
    await abandon_wizard(state)
    if message.chat.type != "private":
        await tell(message, "Send <code>/register</code> to me in a private chat.")
        return
    if tenant is not None:
        await tell(
            message,
            f"You already have <b>{escape(tenant.slug)}</b>. "
            "Use <code>/members</code>, or <code>/setup</code> to bind a group.",
        )
        return
    if not settings.registration_open:
        await tell(message, _CLOSED)
        return

    name = command_text(message).strip()
    if not name:
        await tell(message, "Usage: <code>/register your-team-name</code>")
        return
    if message.from_user is None:  # pragma: no cover - private chats have one
        return

    system = system_database()
    slug = slugify(name)
    # Slugs are visible to their owner and appear in logs, so a collision must
    # not merge two customers. Suffix instead.
    if await tenancy.get_by_slug(system, slug) is not None:
        slug = f"{slug}-{secrets.token_hex(2)}"
    try:
        created = await tenancy.create(
            system,
            slug=slug,
            name=name[:120],
            owner_user_id=message.from_user.id,
            conductor_api_url=settings.conductor_api_url,
        )
    except Exception as exc:
        await tell(message, f"Could not register: {escape(short_error(exc))}")
        return

    code = await issue_code(
        system,
        tenant_id=created.id,
        user_id=message.from_user.id,
        purpose="bind_chat",
    )
    log.info("registration.created", tenant=created.slug)
    await tell(
        message,
        f"Workspace <b>{escape(created.slug)}</b> created.\n\n"
        + _NEXT_STEPS.format(code=escape(code)),
    )


@router.message(Command("setup"))
async def setup(
    message: Message,
    state: FSMContext,
    tenant: TenantContext | None = None,
    db: Database | None = None,
) -> None:
    """Bind this chat to a workspace, proving the bot can really run here.

    ``can_manage_topics`` has been observed ``true`` on a chat that then
    refused ``createForumTopic``. A check that does not perform the capability
    is a guess, so this creates a throwaway topic and deletes it. Two calls, no
    residue — and the answer is the truth.
    """
    await abandon_wizard(state)
    if message.chat.type == "private":
        await _setup_dm(message, tenant, db)
        return
    if message.chat.type != "supergroup" or message.bot is None:
        await tell(
            message,
            "Use a private supergroup with Topics enabled.",
            silent=False,
        )
        return

    system = system_database()
    code = command_text(message).strip()
    binding = await tenancy.chat_for(system, message.chat.id)

    if binding is None:
        if not code:
            await tell(
                message,
                "This group is not linked to a workspace yet. Send "
                "<code>/register</code> to me privately, then run "
                "<code>/setup &lt;code&gt;</code> here.",
            )
            return
        redeemed = await tenancy.consume_enrollment_token(
            system, token_hash=hash_code(code), purpose="bind_chat"
        )
        if redeemed is None:
            await tell(message, "That code is not valid, or has expired.")
            return
        tenant_id, _issued_to = redeemed
    elif tenant is None or not tenant.is_owner:
        # Bound already: only its owners may re-run the capability check.
        await tell(message, "Owners only.")
        return
    else:
        tenant_id = binding.tenant_id

    support = await forum_support(message.bot, message.chat.id)
    if support.degraded:
        detail = support.detail or "forum topics and topic permissions"
        await tell(message, f"Setup blocked · {escape(detail)}.", silent=False)
        return
    try:
        probe = await require_topic(message.bot, message.chat.id, "setup check")
    except TopicCreateError as exc:
        await tell(message, f"Setup blocked · {escape(exc.reason)}.", silent=False)
        return
    await discard_topic(message.bot, message.chat.id, probe)

    try:
        await tenancy.bind_chat(
            system,
            message.chat.id,
            tenant_id,
            kind="group",
            is_primary=binding is None,
            title=message.chat.title,
            bound_by=message.from_user.id if message.from_user else None,
            verified=True,
        )
    except ValueError as exc:
        await tell(message, escape(str(exc)), silent=False)
        return

    # The routing row belongs to the tenant, so it needs that scope.
    async with tenant_scope(tenant_id):
        await chats_repo.ensure(resolve_db(db), message.chat.id, 0, kind="general")

    row = await tenancy.get(system, tenant_id)
    needs_key = row is None or not row.has_conductor_key
    tail = (
        "\nSend <code>/key</code> to me privately to finish."
        if needs_key
        else "\nGeneral is search-only; <code>/new</code> creates topics."
    )
    await tell(message, "Ready ·" + tail)


async def _setup_dm(
    message: Message, tenant: TenantContext | None, db: Database | None
) -> None:
    if tenant is None:
        await tell(message, "Send <code>/register</code> first.")
        return
    async with tenant_scope(tenant.tenant_id):
        await chats_repo.ensure(resolve_db(db), message.chat.id, 0, kind="dm")
    await tell(message, "DM mode ready · one session at a time.")


@router.message(Command("key", "voicekey"))
async def set_key(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
) -> None:
    """Store a sealed API key, and get it out of Telegram immediately."""
    await abandon_wizard(state)
    speech = (message.text or "").startswith("/voicekey")
    if message.chat.type != "private":
        # Refuse *and* clean up: the key is already in a group's history.
        await _delete(message)
        await tell(
            message,
            "Never send an API key to a group. I deleted that message — "
            "rotate the key at its provider, then send it to me privately.",
            silent=False,
        )
        return
    if not tenant.is_owner:
        await tell(message, "Owners only.")
        return

    value = command_text(message).strip()
    if not value:
        await tell(
            message,
            "Send <code>/key &lt;your Conductor API key&gt;</code>.\n"
            "I delete your message straight away and store the key encrypted.",
        )
        return

    system = system_database()
    box = secret_box()
    fingerprint = box.fingerprint(value)
    current = (
        tenant.row.elevenlabs_key_fp if speech else tenant.row.conductor_key_fp
    )
    await _delete(message)

    if current == fingerprint:
        await tell(message, "That is already the stored key. Nothing changed.")
        return

    if not speech:
        # Validate before storing: a typo should be an answer now, not a
        # mysterious auth failure an hour later.
        checked = await _check_conductor_key(value, tenant.row.conductor_api_url)
        if checked is not None:
            await tell(message, f"Conductor rejected that key · {escape(checked)}")
            return

    sealed = box.seal(
        value,
        tenant_id=tenant.tenant_id,
        purpose=ELEVENLABS_KEY_PURPOSE if speech else CONDUCTOR_KEY_PURPOSE,
    )
    if speech:
        await tenancy.set_elevenlabs_key(
            system,
            tenant.tenant_id,
            ciphertext=sealed,
            kid=box.active_kid,
            fingerprint=fingerprint,
        )
        await tell(message, "Speech key stored. Turn voice on with /voice.")
        return

    await tenancy.set_conductor_key(
        system,
        tenant.tenant_id,
        ciphertext=sealed,
        kid=box.active_kid,
        fingerprint=fingerprint,
        by_user_id=tenant.user_id,
    )
    if tenant.status == "pending":
        await tenancy.set_status(system, tenant.tenant_id, "active")
    log.info("registration.key_stored", tenant=tenant.slug)
    await tell(
        message,
        "Key stored and your message deleted. Your workspace is active — "
        "run <code>/new &lt;prompt&gt;</code> in your group.",
    )


@router.message(Command("revoke"))
async def revoke(
    message: Message, tenant: TenantContext, state: FSMContext
) -> None:
    """Forget the stored key. Polling stops; nothing else is touched."""
    await abandon_wizard(state)
    if not tenant.is_owner:
        await tell(message, "Owners only.")
        return
    system = system_database()
    await tenancy.set_conductor_key(
        system, tenant.tenant_id, ciphertext=None, kid=None, fingerprint=None
    )
    await tenancy.set_elevenlabs_key(
        system, tenant.tenant_id, ciphertext=None, kid=None, fingerprint=None
    )
    await tenancy.set_status(system, tenant.tenant_id, "pending")
    log.info("registration.key_revoked", tenant=tenant.slug)
    await tell(
        message,
        "Keys deleted and polling stopped. Send <code>/key</code> to start again.",
    )


async def _check_conductor_key(api_key: str, api_url: str) -> str | None:
    """``None`` when the key works; a short reason when it does not."""
    client = ConductorClient(api_key=api_key, api_url=api_url, max_attempts=1)
    try:
        await client.get_me()
    except Exception as exc:
        return short_error(exc)
    finally:
        await client.aclose()
    return None


async def _delete(message: Message) -> None:
    """Best effort. Deletion works in private chats and recent group messages."""
    if message.bot is None:
        return
    try:
        await message.bot.delete_message(message.chat.id, message.message_id)
    except Exception as exc:
        log.warning("registration.delete_failed", error=repr(exc))
