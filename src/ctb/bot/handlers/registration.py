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
from contextlib import suppress
from typing import Final

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ctb.bot.app import register_router
from ctb.bot.handlers.common import abandon_wizard, command_text, short_error, tell
from ctb.bot.handlers.topics import (
    TopicCreateError,
    discard_topic,
    forum_support,
    require_topic,
    resolve_db,
)
from ctb.bot.keyboards import (
    Action,
    Cb,
    NonceError,
    NonceStore,
    confirm_keyboard,
    resolve,
)
from ctb.bot.middleware.tenancy import TenantContext, forget_cached
from ctb.conductor.client import ConductorClient
from ctb.conductor.pool import CONDUCTOR_KEY_PURPOSE
from ctb.db.connection import Database, now_ms, tenant_scope
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import tenancy
from ctb.delivery.render.html import escape
from ctb.logging import get_logger
from ctb.runtime import client_pool, provider_pool, secret_box, system_database
from ctb.settings import Settings
from ctb.voice.pool import ELEVENLABS_KEY_PURPOSE

router = Router(name=__name__)
register_router(router, order=5)

log = get_logger(__name__)

#: How long a binding code is good for. Long enough to switch apps, short
#: enough that a screenshot in a scrollback is not a standing invitation.
CLAIM_TTL_MS: Final = 15 * 60 * 1000

_HOUR_MS: Final = 60 * 60 * 1000

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
    """Create a workspace and return the code that binds a group to it.

    Re-running it is how you get another code. The first version refused
    outright once you had a workspace, which dead-ended anybody who took longer
    than the 15-minute TTL to create a supergroup and grant four admin rights —
    ``/setup`` needs a code, and this was the only command that minted one.
    """
    await abandon_wizard(state)
    if message.chat.type != "private":
        await tell(message, "Send <code>/register</code> to me in a private chat.")
        return
    if message.from_user is None:  # pragma: no cover - private chats have one
        return
    if tenant is not None:
        # Only *owning* one blocks you. Being a member of somebody else's
        # workspace is not a reason you cannot have your own.
        if not tenant.is_owner:
            await tell(
                message,
                f"You are a member of <b>{escape(tenant.slug)}</b>, which "
                "someone else owns. Send <code>/leave</code> first if you want "
                "your own workspace.",
            )
            return
        await _resume_registration(message, tenant)
        return
    if not settings.registration_open:
        await tell(message, _CLOSED)
        return

    name = command_text(message).strip()
    if not name:
        await tell(message, "Usage: <code>/register your-team-name</code>")
        return

    system = system_database()
    # Registration is open and unauthenticated: one INSERT per call, and
    # nothing prunes an abandoned `pending` tenant. A script with a pool of
    # Telegram accounts would otherwise fill the table overnight.
    recent = await tenancy.created_since(system, since_ms=now_ms() - _HOUR_MS)
    if recent >= settings.registration_rate_per_hour:
        log.warning(
            "registration.rate_limited", recent=recent, user_id=message.from_user.id
        )
        await tell(message, "Sign-ups are busy right now. Try again in an hour.")
        return

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


async def _resume_registration(message: Message, tenant: TenantContext) -> None:
    """Pick up where an interrupted sign-up left off, with a fresh code.

    The three states worth distinguishing: no group yet (issue another code),
    a group but no key, and finished. Each ends with the one thing to do next,
    because "you already have a workspace" is not an instruction.
    """
    if message.from_user is None:  # pragma: no cover - private chats have one
        return
    system = system_database()
    bound = await tenancy.primary_chat(system, tenant.tenant_id)
    if bound is None:
        code = await issue_code(
            system,
            tenant_id=tenant.tenant_id,
            user_id=message.from_user.id,
            purpose="bind_chat",
        )
        log.info("registration.code_reissued", tenant=tenant.slug)
        await tell(
            message,
            f"<b>{escape(tenant.slug)}</b> has no group yet. Here is a new "
            "code — the previous one no longer works.\n\n"
            + _NEXT_STEPS.format(code=escape(code)),
        )
        return
    if not tenant.row.has_conductor_key:
        await tell(
            message,
            f"<b>{escape(tenant.slug)}</b> is bound to your group and needs a "
            "key. Send <code>/key &lt;your Conductor API key&gt;</code> here.",
        )
        return
    await tell(
        message,
        f"<b>{escape(tenant.slug)}</b> is set up and active. "
        "Run <code>/new &lt;prompt&gt;</code> in your group, or "
        "<code>/members</code> to see who is in.",
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
        if message.from_user is None:  # pragma: no cover - groups have a sender
            return
        # Prove the group works *before* spending the code. A single-use code
        # burned on a failed permissions check would leave the workspace
        # unbindable until `/register` mints another.
        if not await _capable(message):
            return
        redeemed = await tenancy.consume_enrollment_token(
            system, token_hash=hash_code(code), purpose="bind_chat"
        )
        if redeemed is None:
            await tell(message, "That code is not valid, or has expired.")
            return
        tenant_id, issued_to = redeemed
        if issued_to != message.from_user.id:
            # The code is a bearer token otherwise: anyone who saw it — a
            # screenshot, a forward, a paste into the wrong chat — could bind a
            # group *they* control to somebody else's workspace, making it that
            # workspace's primary chat and redirecting its owner notices there.
            log.warning(
                "registration.code_wrong_user",
                tenant=str(tenant_id),
                by=message.from_user.id,
            )
            await tell(
                message,
                "That code was issued to someone else. Ask the person who ran "
                "<code>/register</code> to run <code>/setup</code> here, or "
                "send <code>/register</code> to me privately for your own code.",
            )
            return
    elif tenant is None or not tenant.is_owner:
        # Bound already: only its owners may re-run the capability check.
        await tell(message, "Owners only.")
        return
    else:
        tenant_id = binding.tenant_id
        if not await _capable(message):
            return

    try:
        await tenancy.bind_chat(
            system,
            message.chat.id,
            tenant_id,
            kind="group",
            # Re-running /setup must not demote the group. `is_primary` is
            # what `primary_chat()` returns, and auth-fatal notices go there;
            # clearing it on a permissions re-check would silently stop the
            # group ever hearing that its key was rejected.
            is_primary=True if binding is None else binding.is_primary,
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


async def _capable(message: Message) -> bool:
    """Can the bot really run here? Perform the capability, do not ask about it.

    ``can_manage_topics`` has been observed ``true`` on a chat that then refused
    ``createForumTopic``, so ``/setup`` used to answer "Ready" while every
    ``/new`` failed. Create a throwaway topic and delete it: two calls, no
    residue, and the answer is the truth.
    """
    if message.bot is None:  # pragma: no cover - guarded by the caller
        return False
    support = await forum_support(message.bot, message.chat.id)
    if support.degraded:
        detail = support.detail or "forum topics and topic permissions"
        await tell(message, f"Setup blocked · {escape(detail)}.", silent=False)
        return False
    try:
        probe = await require_topic(message.bot, message.chat.id, "setup check")
    except TopicCreateError as exc:
        await tell(message, f"Setup blocked · {escape(exc.reason)}.", silent=False)
        return False
    await discard_topic(message.bot, message.chat.id, probe)
    return True


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
        deleted = await _delete(message)
        await tell(
            message,
            "Never send an API key to a group. "
            + ("I deleted that message — r" if deleted else "R")
            + "otate the key at its provider, then send it to me privately.",
            silent=False,
        )
        return

    # Delete FIRST, before any check that can reject. Every early return below
    # this point would otherwise leave a live API key sitting in the sender's
    # chat history forever — and the most likely wrong-user path, a member who
    # is not an owner, is exactly the one who was told by /help to run this.
    value = command_text(message).strip()
    if value:
        deleted = await _delete(message)
        note = _delete_note(deleted)
    else:
        note = ""

    if not tenant.is_owner:
        await tell(
            message,
            "Only this workspace's owners can store its key." + note,
            silent=not note,
        )
        return

    if not value:
        await tell(
            message,
            "Send <code>/key &lt;your Conductor API key&gt;</code>.\n"
            "I delete your message straight away and store the key encrypted.",
        )
        return

    system = system_database()
    box = secret_box()
    fingerprint = box.fingerprint_of(value, tenant_id=tenant.tenant_id)
    current = tenant.row.elevenlabs_key_fp if speech else tenant.row.conductor_key_fp

    if current == fingerprint:
        await tell(message, "That is already the stored key. Nothing changed." + note)
        return

    if not speech:
        # Validate before storing: a typo should be an answer now, not a
        # mysterious auth failure an hour later.
        checked = await _check_conductor_key(value, tenant.row.conductor_api_url)
        if checked is not None:
            await tell(
                message, f"Conductor rejected that key · {escape(checked)}" + note
            )
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
        await tell(message, "Speech key stored. Turn voice on with /voice." + note)
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
    forget_cached(tenant.tenant_id)
    await tell(
        message,
        ("Key stored and your message deleted. " if not note else "Key stored. ")
        + "Your workspace is active — run <code>/new &lt;prompt&gt;</code> "
        "in your group." + note,
    )


@router.message(Command("voice"))
async def voice(
    message: Message,
    tenant: TenantContext,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Turn this workspace's voice input on or off.

    ``tenants.voice_enabled`` gated the whole feature and nothing set it, so a
    workspace that had stored a speech key still could not use one. Two
    switches in series, deliberately: the operator's ``VOICE_ENABLED`` is a
    kill switch a tenant cannot override, because voice is the only feature
    whose data leaves the perimeter.
    """
    await abandon_wizard(state)
    if not tenant.is_owner:
        await tell(message, "Owners only.")
        return

    want = command_text(message).strip().casefold()
    if want not in {"on", "off"}:
        state_now = "on" if tenant.settings.voice_enabled else "off"
        await tell(
            message,
            f"Voice is <b>{state_now}</b> for this workspace.\n"
            "<code>/voice on</code> · <code>/voice off</code>",
        )
        return

    if want == "on" and not settings.voice_enabled:
        await tell(message, "Voice is switched off for this whole instance.")
        return
    if want == "on" and not tenant.row.elevenlabs_key_fp:
        await tell(
            message,
            "Store a speech key first: send <code>/voicekey &lt;key&gt;</code> "
            "to me privately. There is no shared key — voice is billed to your "
            "own account, and only yours.",
        )
        return

    await tenancy.update_defaults(
        system_database(), tenant.tenant_id, voice_enabled=want == "on"
    )
    forget_cached(tenant.tenant_id)
    log.info("registration.voice_toggled", tenant=tenant.slug, enabled=want == "on")
    await tell(
        message,
        f"Voice is now <b>{want}</b>."
        + (
            "\nSend a voice note in a topic and it becomes a prompt."
            if want == "on"
            else ""
        ),
    )


@router.message(Command("revoke"))
async def revoke(message: Message, tenant: TenantContext, state: FSMContext) -> None:
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
    forget_cached(tenant.tenant_id)
    await deauthorize(tenant.tenant_id)
    await tell(
        message,
        "Keys deleted and polling stopped. Send <code>/key</code> to start again.",
    )


async def deauthorize(tenant_id: object) -> None:
    """Drop every in-memory copy of a workspace's decrypted keys.

    The database row is only half of a revocation. A built ``ConductorClient``
    holds the plaintext key in an httpx ``Authorization`` header and an
    ``ElevenLabsProvider`` holds the speech key in a field; both outlive the row
    unless something says so. ``ClientPool`` would eventually sweep on its idle
    TTL — up to fifteen minutes — and the provider pool has no sweep at all, so
    without this a key the owner just deleted stays live in the process.

    Best effort on purpose: the row is already gone, which is what stops the
    polling. Failing to tidy memory must not turn a successful revocation into
    an error message.
    """
    with suppress(Exception):
        await client_pool().forget(tenant_id)  # type: ignore[arg-type]
    providers = provider_pool()
    if providers is not None:
        with suppress(Exception):
            await providers.forget(tenant_id)  # type: ignore[arg-type]


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


async def _delete(message: Message) -> bool:
    """Best effort, and it says which. Callers must not claim more than this.

    Deletion works in private chats and on recent group messages, but the bot
    can be missing *Delete messages*, or the message can be too old. Telling
    someone "I deleted that" when the key is still visible above the reply is
    worse than admitting the failure, because it stops them from deleting it
    themselves.
    """
    if message.bot is None:
        return False
    try:
        await message.bot.delete_message(message.chat.id, message.message_id)
    except Exception as exc:
        log.warning("registration.delete_failed", error=repr(exc))
        return False
    return True


#: Appended whenever a key-bearing message survived. Deliberately blunt: the
#: only safe move once a key has sat in Telegram history is to rotate it.
_UNDELETED: Final = (
    "\n\n⚠️ I could <b>not</b> delete your message — delete it yourself and "
    "rotate that key at its provider."
)


def _delete_note(deleted: bool) -> str:
    return "" if deleted else _UNDELETED


@router.message(Command("use"))
async def use(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
) -> None:
    """Point *this private chat* at one of your workspaces.

    A DM with no explicit binding resolves to the workspace you own, and
    refuses when that is ambiguous — a prompt must never silently land in the
    wrong organisation. This is how you say which one you meant.
    """
    await abandon_wizard(state)
    if message.chat.type != "private":
        await tell(message, "That only makes sense in a private chat.")
        return
    if message.from_user is None:  # pragma: no cover - private chats have one
        return

    system = system_database()
    slug = command_text(message).strip().casefold()
    seats = await tenancy.memberships_for_user(system, message.from_user.id)
    if not slug:
        names: list[str] = []
        for seat in seats:
            row = await tenancy.get(system, seat.tenant_id)
            if row is not None:
                mark = " ·" if row.id == tenant.tenant_id else ""
                names.append(f"<code>{escape(row.slug)}</code>{mark}")
        await tell(
            message,
            "Usage: <code>/use workspace-name</code>\nYou are in: "
            + (", ".join(names) or "nothing yet"),
        )
        return

    target = await tenancy.get_by_slug(system, slug)
    if target is None or all(seat.tenant_id != target.id for seat in seats):
        await tell(message, "You are not in a workspace by that name.")
        return
    if target.id == tenant.tenant_id:
        await tell(message, f"Already using <b>{escape(target.slug)}</b>.")
        return

    # Re-home rather than bind: `bind_chat` refuses a chat that belongs to
    # another tenant, and this DM may already point at the one we are switching
    # away from. Without this, /use worked exactly once and then raised.
    await tenancy.rebind_chat(
        system,
        message.chat.id,
        target.id,
        kind="dm",
        title=None,
        bound_by=message.from_user.id,
    )
    # Invalidate the tenant we are leaving, not the one we are joining — the
    # stale cache entry is keyed on this chat and holds the *old* tenant, so
    # sweeping by the new one matches nothing and the next prompt would land in
    # the wrong Conductor organisation for the rest of the TTL.
    forget_cached(tenant.tenant_id)
    forget_cached(target.id)
    await tell(message, f"This chat now uses <b>{escape(target.slug)}</b>.")


@router.message(Command("forget"))
async def forget(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
    nonces: NonceStore,
) -> None:
    """Delete this workspace and everything in it. Two taps, owners only.

    ``/privacy`` promises this, so it exists. Every tenant-scoped table cascades
    from the ``tenants`` row, so one delete really is everything: routing,
    sessions, transcripts, deliveries, wizard state, voice jobs and the sealed
    keys.
    """
    await abandon_wizard(state)
    if message.chat.type != "private":
        await tell(message, "Send <code>/forget</code> to me in a private chat.")
        return
    if tenant.role != "owner":
        await tell(message, "Only an owner can delete a workspace.")
        return
    await tell(
        message,
        f"This deletes <b>{escape(tenant.slug)}</b> and every transcript, "
        "session and key in it. It cannot be undone.",
        reply_markup=confirm_keyboard(
            Action.FORGET,
            str(tenant.tenant_id),
            tenant.slug,
            verb="Delete",
            store=nonces,
            user_id=tenant.user_id,
            chat_id=message.chat.id,
        ),
    )


@router.callback_query(Cb.filter(F.action == Action.FORGET.value))
async def confirm_forget(
    query: CallbackQuery,
    tenant: TenantContext,
    nonces: NonceStore,
) -> None:
    """The second tap. Only fires for a payload naming *this* workspace."""
    try:
        ticket = resolve(query, expect=Action.FORGET, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    if ticket.target != str(tenant.tenant_id) or tenant.role != "owner":
        await query.answer("Not yours to delete.", show_alert=True)
        return
    await tenancy.delete_tenant(system_database(), tenant.tenant_id)
    forget_cached(tenant.tenant_id)
    with suppress(Exception):
        await deauthorize(tenant.tenant_id)
    log.info("registration.deleted", tenant=tenant.slug)
    await query.answer("Deleted.")
