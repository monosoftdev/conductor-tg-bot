"""Self-serve sign-up: paste a key in a private chat, and go.

**"Team", not "workspace".** Conductor already calls a checkout-plus-agent a
*workspace*, and ``/new`` creates those by the dozen. Using the same word for
the thing that owns the API key and the people produced sentences like "this
workspace is at its limit of 50 workspaces". The container is a **team**; a
*workspace* is always the Conductor one.

The shortest path to working is **two messages, both private**:

1. ``/start`` creates a team implicitly, named after your Telegram account.
2. ``/key`` stores its Conductor API key. That is the whole sign-up — prompts,
   topics and results all live in the private chat.

Everything else is additive, and nothing else is required:

* ``/register <name>`` names the team instead of taking the default.
* ``/team`` explains what a group adds — several people, a shared topic list —
  and mints the single-use code; ``/setup <code>`` in that supergroup binds it.
  The code is issued privately and hashed at rest, because a shared bot can be
  added to any group by anyone: being added is not consent, and without the
  code somebody could bind the bot to a team that is not theirs.
* ``/voicekey`` stores a speech key.

``/start``, ``/register``, ``/help``, ``/privacy`` are the only commands a
non-member can reach, and only in a private chat —
:class:`~ctb.bot.middleware.tenancy.TenantMiddleware` lets exactly that set
through unresolved. Everything else about someone with no team is silence.

**The key never stays in Telegram.** Sent in a group it is refused *and*
deleted, with a rotate-it warning. Sent in a private chat it is validated,
sealed, stored, and the message that carried it is deleted. Telegram keeps
message history forever; a key pasted into a chat would sit there forever too.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from contextlib import suppress
from typing import Final

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User

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
from ctb.voice.provider import check_elevenlabs_key

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

#: What :func:`slugify` returns when a name carries nothing it can keep — a
#: name in a non-Latin script, or punctuation only. Never used as a slug: the
#: implicit path falls back to ``team-<telegram id>``, which is unique by
#: construction and still boring.
_FALLBACK_SLUG: Final = "workspace"

#: Codes are shown once and stored only as a digest.
_CODE_BYTES: Final = 9

#: The whole first run. One instruction, because there is only one thing to do:
#: the team is created implicitly by ``/start``, and a group is optional.
_WELCOME: Final = (
    "<b>Conductor from your phone</b>\n"
    "Your key, your workspaces, your spend.\n\n"
    "One step · <code>/key &lt;your Conductor API key&gt;</code>\n"
    "conductor.build → Settings → API keys. I check it, encrypt it, and "
    "delete your message."
)

_CLOSED: Final = (
    "Sign-up is closed on this instance. Ask whoever runs it for an invitation."
)

#: Asked for *in this chat*, and nowhere else — a key is the one thing that
#: must never be typed into a group.
_ASK_FOR_KEY: Final = (
    "<b>{slug}</b> · one step left.\n"
    "<code>/key &lt;your Conductor API key&gt;</code>\n"
    "conductor.build → Settings → API keys. I check it, encrypt it, and "
    "delete your message."
)

#: The optional half. Nothing above it mentions a group, and nothing here
#: pretends the bot is unusable without one.
_GROUP_STEPS: Final = (
    "<b>Add a group</b> · optional\n"
    "Several people on one team, and a topic list you all see.\n\n"
    "1 · Create a <b>private supergroup</b>, turn on <b>Topics</b>.\n"
    "2 · Add <b>this bot</b> as an administrator: manage topics, pin, delete, "
    "send.\n"
    "3 · Send there <code>/setup {code}</code>\n\n"
    "15 minutes, one use. <code>/team</code> mints another."
)


#: Anything this long, unbroken and key-shaped is a credential, whatever
#: command it arrived on. Deliberately loose: the cost of a false positive is
#: deleting one odd-looking message, and the cost of a false negative is a live
#: API key sitting in Telegram history forever.
_SECRET_RE: Final = re.compile(r"^[A-Za-z0-9_\-]{24,}$")


def looks_like_secret(text: str) -> bool:
    return bool(_SECRET_RE.match(text.strip()))


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
async def start(
    message: Message,
    settings: Settings,
    state: FSMContext,
    tenant: TenantContext | None = None,
) -> None:
    """The whole sign-up. One command, one instruction back.

    ``/start`` used to print a welcome that asked for ``/register``, which
    asked for a name, which asked for a key, which asked for a supergroup —
    three commands and an admin-rights dance before the first prompt. The team
    is a container, not a decision: it is created here, named after the
    Telegram account, and can be renamed later by whoever cares.

    Idempotent by membership, not by luck. A second ``/start`` finds the seat
    the first one made — whether or not the tenancy middleware resolved it —
    and resumes, so nobody ends up owning two teams by tapping twice.
    """
    await abandon_wizard(state)
    if message.chat.type != "private":
        # A group cannot be the entry point: sign-up ends in a key, and a key
        # in a group is a key to rotate.
        await tell(message, "Send <code>/start</code> to me in a private chat.")
        return
    if message.from_user is None:  # pragma: no cover - private chats have one
        return
    if tenant is not None:
        await _resume(message, tenant.row, is_owner=tenant.is_owner)
        return

    system = system_database()
    seats = await tenancy.memberships_for_user(system, message.from_user.id)
    if seats:
        await _resume_seat(message, seats)
        return

    created = await _create_team(
        message, settings, name=_default_name(message.from_user)
    )
    if created is None:
        return  # refused, and already said why
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
        "the team and everything in it.",
    )


@router.message(Command("register"))
async def register(
    message: Message,
    settings: Settings,
    state: FSMContext,
    tenant: TenantContext | None = None,
) -> None:
    """Name your team, for whoever wants to.

    Optional now: ``/start`` already made one. This exists so a team can carry
    a name somebody chose rather than a Telegram username, and so the old
    instructions in somebody's scrollback still work.
    """
    await abandon_wizard(state)
    if message.chat.type != "private":
        await tell(message, "Send <code>/register</code> to me in a private chat.")
        return
    if message.from_user is None:  # pragma: no cover - private chats have one
        return
    if tenant is not None:
        # Only *owning* one blocks you. Being a member of somebody else's
        # team is not a reason you cannot have your own.
        if not tenant.is_owner:
            await tell(
                message,
                f"You are a member of <b>{escape(tenant.slug)}</b>, which "
                "someone else owns. Send <code>/leave</code> first if you want "
                "your own team.",
            )
            return
        await _resume(message, tenant.row, is_owner=True)
        return

    seats = await tenancy.memberships_for_user(system_database(), message.from_user.id)
    if seats:
        # The middleware could not resolve which team this chat means, but the
        # seat exists — creating a second one would only deepen the ambiguity.
        await _resume_seat(message, seats)
        return

    # An empty name is not an error any more: the default is a good name, and
    # a usage line here would be one more round trip before the first prompt.
    name = command_text(message).strip() or _default_name(message.from_user)
    created = await _create_team(message, settings, name=name)
    if created is None:
        return
    await tell(message, _ASK_FOR_KEY.format(slug=escape(created.slug)))


@router.message(Command("team"))
async def team(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
) -> None:
    """The optional group flow, and the single-use code that starts it.

    Discoverable on its own so nothing earlier has to mention a group. Re-run
    it for a fresh code: the old one expires in 15 minutes, which is less time
    than creating a supergroup and granting four admin rights often takes.
    """
    await abandon_wizard(state)
    if message.chat.type != "private":
        await tell(message, "Send <code>/team</code> to me in a private chat.")
        return
    if not tenant.is_owner:
        await tell(message, "Owners only.")
        return
    if message.from_user is None:  # pragma: no cover - private chats have one
        return
    await tell(message, await _group_card(message, tenant))


async def _resume(message: Message, row: tenancy.TenantRow, *, is_owner: bool) -> None:
    """Answer someone who already has a team with the one thing left to do.

    Two states now, not three: no key yet, or ready. A group is no longer a
    step, so "you have not made one" is not an unfinished sign-up.
    """
    if not row.has_conductor_key:
        if not is_owner:
            await tell(
                message,
                f"<b>{escape(row.slug)}</b> has no Conductor key yet · its "
                "owner sends <code>/key</code>.",
            )
            return
        await tell(message, _ASK_FOR_KEY.format(slug=escape(row.slug)))
        return
    await tell(message, await _ready(row))


async def _resume_seat(message: Message, seats: list[tenancy.TenantMember]) -> None:
    """Resume from a membership row, when no tenant was resolved for us.

    ``/start`` and ``/register`` run unresolved, so this is the idempotency
    guard: the seat is the proof a team already exists, and two taps must not
    make two teams. Ownership wins ties the way the middleware picks a DM's
    team, and a genuine tie is answered rather than guessed.
    """
    system = system_database()
    owned = [seat for seat in seats if seat.role in ("owner", "admin")]
    candidates = owned or seats
    if len(candidates) != 1:
        await tell(
            message,
            "You are in several teams · <code>/use name</code> picks the one "
            "this chat means.",
        )
        return
    row = await tenancy.get(system, candidates[0].tenant_id)
    if row is None:  # pragma: no cover - FK makes this unreachable
        return
    await _resume(message, row, is_owner=candidates[0].role in ("owner", "admin"))


async def _ready(row: tenancy.TenantRow) -> str:
    """The finish line, and where to prompt. Never "now build a group"."""
    if await _has_group(row.id):
        return (
            f"<b>{escape(row.slug)}</b> is ready · send "
            "<code>/new &lt;prompt&gt;</code> in your group."
        )
    return (
        f"<b>{escape(row.slug)}</b> is ready · send "
        "<code>/new &lt;prompt&gt;</code> here.\n"
        "<code>/team</code> adds a group · optional."
    )


async def _has_group(tenant_id: uuid.UUID) -> bool:
    chats = await tenancy.list_chats(system_database(), tenant_id)
    return any(chat.kind == "group" for chat in chats)


def _default_name(user: User) -> str:
    """A team name derived from the Telegram account.

    The container needs *a* name, not a decision. ``/register`` still takes a
    better one, and it is only ever a label — the slug is what appears in logs.
    """
    if user.username:
        return user.username
    full = " ".join(part for part in (user.first_name, user.last_name) if part)
    return full.strip() or f"team-{user.id}"


async def _create_team(
    message: Message, settings: Settings, *, name: str
) -> tenancy.TenantRow | None:
    """Create a team, or say why not. ``None`` means the caller is done.

    This is the sign-up path whichever command reached it, so the instance's
    open/closed switch and its rate limit are enforced here rather than in one
    of the two callers.
    """
    if message.from_user is None:  # pragma: no cover - callers checked
        return None
    if not settings.registration_open:
        await tell(message, _CLOSED)
        return None

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
        return None

    slug = slugify(name)
    if slug == _FALLBACK_SLUG:
        # Nothing survived slugification — a name in another script, or one
        # that really is "workspace". Either way, say whose it is.
        slug = f"team-{message.from_user.id}"
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
        return None

    log.info("registration.created", tenant=created.slug)
    await _claim_dm(message, created)
    return created


async def _claim_dm(message: Message, row: tenancy.TenantRow) -> None:
    """Point this private chat at the team it just created.

    Without a row the DM resolves by *sole* membership, which stops resolving
    the moment somebody owns two teams — and ``/use``, the command that fixes
    that, needs a resolved tenant itself. Binding here means the second team
    never dead-ends the first.

    Best effort, and deliberately non-destructive: a DM already bound to
    another team stays where it is. ``/use`` is how one person says which of
    their teams this chat means; creating one must not decide it for them.
    """
    if message.from_user is None:  # pragma: no cover - callers checked
        return
    system = system_database()
    if await tenancy.chat_for(system, message.chat.id) is not None:
        return
    with suppress(Exception):
        await tenancy.bind_chat(
            system,
            message.chat.id,
            row.id,
            kind="dm",
            # Owner notices land here until a group is bound; `/setup` moves
            # the flag to the group, because that is where the team is then.
            is_primary=True,
            bound_by=message.from_user.id,
        )


async def _group_card(message: Message, tenant: TenantContext) -> str:
    """A fresh binding code and what to do with it.

    Minted at ``/team`` rather than at sign-up so the 15-minute clock starts
    when the owner is actually ready to make a group — and so nothing before
    this point has to mention one.
    """
    if message.from_user is None:  # pragma: no cover - private chats have one
        return ""
    code = await issue_code(
        system_database(),
        tenant_id=tenant.tenant_id,
        user_id=message.from_user.id,
        purpose="bind_chat",
    )
    log.info("registration.code_issued", tenant=tenant.slug)
    return _GROUP_STEPS.format(code=escape(code))


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
                "This group is not linked to a team yet. Send "
                "<code>/team</code> to me privately for a code, then "
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
                "<code>/team</code> to run <code>/setup</code> here, or send "
                "<code>/team</code> to me privately for your own code.",
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
        await tell(message, "Send <code>/start</code> first.")
        return
    async with tenant_scope(tenant.tenant_id):
        await chats_repo.ensure(resolve_db(db), message.chat.id, 0, kind="dm")
    await tell(message, "DM ready · <code>/new &lt;prompt&gt;</code> starts one.")


@router.message(Command("key", "voicekey"))
async def set_key(
    message: Message,
    tenant: TenantContext | None,
    settings: Settings,
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

    if tenant is None:
        # A key pasted before `/start`. The message is already deleted above,
        # which is the urgent half; the instruction is the cheap half.
        # Unreachable while ``UNRESOLVED_COMMANDS`` withholds ``/key`` from a
        # non-member — but a live credential must never depend on that.
        await tell(
            message,
            "Send <code>/start</code> first, then the key." + note,
            silent=not note,
        )
        return

    if not tenant.is_owner:
        await tell(
            message,
            "Only this team's owners can store its key." + note,
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

    # Validate before storing, either way: a typo should be an answer now, not a
    # mysterious auth failure an hour later. `/voicekey` used to skip this, so a
    # mistyped speech key was accepted with "Speech key stored" and only failed
    # at the first voice note — which reads as "voice is broken".
    if speech:
        checked = await check_elevenlabs_key(value)
        if checked is not None:
            await tell(
                message, f"ElevenLabs rejected that key · {escape(checked)}" + note
            )
            return
    else:
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
        # Storing a key *is* the request to use it. Two switches in series made
        # a dead end: "/voice on" said store a key, and storing a key said turn
        # voice on. The platform kill switch still wins, and `/voice off` still
        # pauses it without throwing the key away.
        enable = settings.voice_enabled
        await tenancy.set_elevenlabs_key(
            system,
            tenant.tenant_id,
            ciphertext=sealed,
            kid=box.active_kid,
            fingerprint=fingerprint,
        )
        if enable:
            await tenancy.update_defaults(system, tenant.tenant_id, voice_enabled=True)
        forget_cached(tenant.tenant_id)
        await tell(
            message,
            (
                "🎙 Speech key stored, checked, and <b>voice is on</b>.\n"
                "Send a voice note in a topic and it becomes a prompt. "
                "<code>/voice off</code> pauses it."
                if enable
                else "Speech key stored and checked. Voice is switched off for "
                "this whole instance, so nothing will use it yet."
            )
            + note,
        )
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
    stored = "Key stored and your message deleted." if not note else "Key stored."

    # This is the end of sign-up, so say so and name the next thing to *do*.
    # It used to hand out a binding code here, which made a supergroup read as
    # step three of three rather than an option nobody has to take.
    await tell(message, stored + "\n" + await _ready(tenant.row) + note)


async def _store_speech_key(
    message: Message,
    tenant: TenantContext,
    settings: Settings,
    value: str,
) -> None:
    """A speech key that arrived on ``/voice`` instead of ``/voicekey``.

    Deleting it is the urgent half — it is a live credential in a chat log —
    so that happens before anything that can refuse.
    """
    deleted = await _delete(message)
    note = _delete_note(deleted)
    if not tenant.is_owner:
        await tell(message, "Only this team's owners can store its key." + note)
        return
    checked = await check_elevenlabs_key(value)
    if checked is not None:
        await tell(message, f"ElevenLabs rejected that key · {escape(checked)}" + note)
        return
    box = secret_box()
    system = system_database()
    await tenancy.set_elevenlabs_key(
        system,
        tenant.tenant_id,
        ciphertext=box.seal(
            value, tenant_id=tenant.tenant_id, purpose=ELEVENLABS_KEY_PURPOSE
        ),
        kid=box.active_kid,
        fingerprint=box.fingerprint_of(value, tenant_id=tenant.tenant_id),
    )
    if settings.voice_enabled:
        await tenancy.update_defaults(system, tenant.tenant_id, voice_enabled=True)
    forget_cached(tenant.tenant_id)
    log.info("registration.speech_key_stored", tenant=tenant.slug, via="/voice")
    await tell(
        message,
        "🎙 That was a key, so I deleted your message and stored it. "
        "<b>Voice is on</b> — send a voice note in a topic." + note,
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

    argument = command_text(message).strip()
    want = argument.casefold()

    # `/voice sk_...` — the key typed on the neighbouring command. This used to
    # fall through to the status line, which left a live credential in the chat
    # and told the reader nothing was wrong. Treat it as what it obviously is.
    if want not in {"on", "off", ""} and looks_like_secret(argument):
        await _store_speech_key(message, tenant, settings, argument)
        return

    if want not in {"on", "off"}:
        state_now = "on" if tenant.settings.voice_enabled else "off"
        stored = "a key is stored" if tenant.row.elevenlabs_key_fp else "no key yet"
        await tell(
            message,
            f"Voice is <b>{state_now}</b> · {stored}\n"
            "<code>/voice off</code> to pause it · "
            "<code>/voicekey &lt;key&gt;</code> to replace the key",
        )
        return

    if want == "on" and not settings.voice_enabled:
        await tell(message, "Voice is switched off for this whole instance.")
        return
    if want == "on" and not tenant.row.elevenlabs_key_fp:
        await tell(
            message,
            "Send me your speech key and voice turns itself on: "
            "<code>/voicekey &lt;key&gt;</code>, here in this chat. "
            "There is no shared key — voice is billed to your own account.",
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
            "Usage: <code>/use team-name</code>\nYou are in: "
            + (", ".join(names) or "nothing yet"),
        )
        return

    target = await tenancy.get_by_slug(system, slug)
    if target is None or all(seat.tenant_id != target.id for seat in seats):
        await tell(message, "You are not in a team by that name.")
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
        await tell(message, "Only an owner can delete a team.")
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
