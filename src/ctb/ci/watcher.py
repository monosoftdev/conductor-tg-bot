"""The CI watch loop: poll GitHub, say the verdict once, then stop.

Shaped like :class:`ctb.voice.service.VoiceService` — claim on the worker pool,
then do everything else inside ``tenant_scope`` — and deliberately **optional**.
A team with no GitHub token, a repository the token cannot see, GitHub itself
being down: none of those may cost anybody a single delivered agent message, so
every failure here ends in a log line and a rescheduled row.

The two rules it obeys:

* **Say it once.** A watch records the commit and verdict it announced. A
  re-read of the same red run is silence; a *new* commit that goes red again is
  news, and says so.
* **Stop watching.** Terminal verdict, merged, closed, expired, or a token that
  cannot see the repository — each ends the watch rather than leaving a row
  spending somebody's rate limit forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Final

from ctb.bot.keyboards import NonceStore
from ctb.ci.notice import ci_keyboard, ci_text
from ctb.db.connection import Database, tenant_scope
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import ci as ci_repo
from ctb.db.repo import tenancy as tenancy_repo
from ctb.delivery.outbox import Outbox
from ctb.github.client import ChecksResult, CheckState, GitHubError
from ctb.github.pool import GitHubPool
from ctb.logging import get_logger

__all__ = ["CiWatcher"]

#: How often the loop looks for due watches. The per-row cadence is
#: ``ci.POLL_INTERVAL_MS``; this is only how promptly a due row is picked up.
TICK_INTERVAL_S: Final = 5.0
#: After a failed pass — a database blip, not a GitHub one.
ERROR_BACKOFF_S: Final = 30.0
#: Watches finished this long ago are deleted by the maintenance sweep.
PRUNE_AFTER_MS: Final = 7 * 24 * 60 * 60 * 1000

_log = get_logger(__name__)


class CiWatcher:
    """Polls every watched pull request and posts one verdict per commit."""

    def __init__(
        self,
        *,
        system_db: Database,
        outbox: Outbox,
        clients: GitHubPool,
        nonces: NonceStore | None = None,
        batch: int = 16,
        tick_interval: float = TICK_INTERVAL_S,
        sleep: Any = asyncio.sleep,
        clock: Any = time.time,
    ) -> None:
        self._db = system_db
        self._outbox = outbox
        self._clients = clients
        self._nonces = nonces
        self._batch = batch
        self._tick_interval = tick_interval
        self._sleep = sleep
        self._clock = clock
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._polls = 0
        self._notices = 0

    # -- lifecycle ------------------------------------------------------------

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a pass never kills the loop
                _log.error("ci.pass_failed", error=repr(exc)[:500])
                await self._sleep(ERROR_BACKOFF_S)
                continue
            await self._sleep(self._tick_interval)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="ctb-ci")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def health(self) -> dict[str, Any]:
        return {"polls": self._polls, "notices": self._notices}

    # -- one pass -------------------------------------------------------------

    async def tick(self) -> int:
        """Poll every due watch. Returns how many were polled."""
        due = await ci_repo.claim_due(self._db, limit=self._batch)
        for row in due:
            if self._stop.is_set():
                break
            # Inside the scope, including the failure paths: leaving it before
            # the write that records the failure would raise TenantScopeError
            # and strand the row — the mistake `voice.service` documents.
            async with tenant_scope(row.tenant_id):
                try:
                    await self._poll(row)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one row, not the loop
                    _log.warning(
                        "ci.poll_failed", watch=row.slug, error=repr(exc)[:200]
                    )
                    await ci_repo.fail(self._db, row, error=repr(exc))
            self._polls += 1
        return len(due)

    async def _poll(self, row: ci_repo.CiWatchRow) -> None:
        if self._now_ms() >= row.expires_at:
            await ci_repo.finish(
                self._db,
                row,
                head_sha=row.head_sha,
                status="expired",
                notified=False,
                state="gave_up",
            )
            return
        tenant = await tenancy_repo.get(self._db, row.tenant_id)
        if tenant is None or not tenant.is_active:
            await ci_repo.fail(self._db, row, error="tenant inactive", fatal=True)
            return
        client = await self._clients.get(tenant)
        if client is None:
            # No token stored, or it was revoked while this watch was live.
            # Not an error: the team simply has not opted in.
            await ci_repo.fail(self._db, row, error="no github token", fatal=True)
            return

        try:
            pull = await client.get_pull(row.owner, row.repo, row.pr_number)
        except GitHubError as exc:
            await self._record_github_error(row, exc)
            return
        if pull.merged or pull.is_closed:
            # Whatever CI says now, nobody is going to act on it.
            await ci_repo.finish(
                self._db,
                row,
                head_sha=pull.head_sha,
                status="merged" if pull.merged else "closed",
                notified=False,
            )
            return
        if not pull.head_sha:
            await ci_repo.reschedule(
                self._db, row, head_sha=None, status=str(CheckState.NONE)
            )
            return

        try:
            checks = await client.get_checks(row.owner, row.repo, pull.head_sha)
        except GitHubError as exc:
            await self._record_github_error(row, exc)
            return

        if not checks.state.is_terminal:
            await ci_repo.reschedule(
                self._db, row, head_sha=pull.head_sha, status=str(checks.state)
            )
            return
        if row.already_said(pull.head_sha, str(checks.state)):
            await ci_repo.finish(
                self._db,
                row,
                head_sha=pull.head_sha,
                status=str(checks.state),
                notified=False,
            )
            return
        told = await self._announce(row, checks, head_sha=pull.head_sha)
        if told:
            await ci_repo.finish(
                self._db,
                row,
                head_sha=pull.head_sha,
                status=str(checks.state),
                notified=True,
            )
        else:
            # Telegram refused it. The verdict is not going to change, so the
            # next tick tries the same message again rather than dropping it.
            await ci_repo.reschedule(
                self._db, row, head_sha=pull.head_sha, status=str(checks.state)
            )

    async def _record_github_error(
        self, row: ci_repo.CiWatchRow, exc: GitHubError
    ) -> None:
        fatal = exc.is_auth or exc.is_missing
        _log.warning(
            "ci.github_error",
            watch=row.slug,
            status_code=exc.status,
            fatal=fatal,
            error=str(exc)[:200],
        )
        await ci_repo.fail(self._db, row, error=str(exc), fatal=fatal)

    async def _announce(
        self, row: ci_repo.CiWatchRow, checks: ChecksResult, *, head_sha: str
    ) -> bool:
        """Post the verdict. ``False`` means Telegram did not take it."""
        chat = await chats_repo.get(self._db, row.chat_id, row.thread_id)
        if chat is not None and chat.notify == "off":
            return True  # muted on purpose; treat as said and stop watching
        failed = checks.state is CheckState.FAILURE
        markup = ci_keyboard(
            state=checks.state,
            session_id=row.session_id,
            owner=row.owner,
            repo=row.repo,
            pr_number=row.pr_number,
            chat_id=row.chat_id,
            thread_id=row.thread_id,
            store=self._nonces,
            failed_url=checks.failed_url,
        )
        sent = await self._outbox.send_text(
            ci_text(row.slug, checks),
            chat_id=row.chat_id,
            thread_id=row.thread_id,
            # A pass is a nice-to-know; a failure is the reason to pick the
            # phone up. Only one of them earns a buzz.
            silent=not failed,
            reply_markup=markup,
        )
        if sent:
            self._notices += 1
            _log.info(
                "ci.notified", watch=row.slug, state=str(checks.state), sha=head_sha[:7]
            )
        return bool(sent)

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)
