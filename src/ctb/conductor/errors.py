"""The exception hierarchy every caller of the Conductor client catches.

Design notes that matter to callers:

* ``AuthFatal`` (401/403) is **never** retried. Retrying a bad key risks a
  lockout, and the failure is not going to fix itself. The client stops all
  pollers, DMs the owner once, and stays alive for ``/health``.
* ``NotFound`` (404) on a session or workspace means the turn machine moves to
  ``DEAD`` — unbind the topic, stop the task, notify.
* ``Ambiguous`` is the *only* error class that says "the write may or may not
  have landed". For ``POST /sessions/{id}/messages`` the caller must retry with
  the **same** ``messageId`` — verified: re-POSTing an identical id dedupes. For
  ``POST /workspaces`` there is no idempotency key, so an ``Ambiguous`` there
  must be reconciled (nonce in the generated workspace name), never blind-retried.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "Ambiguous",
    "ApiError",
    "AuthFatal",
    "CircuitOpen",
    "ConductorError",
    "NotFound",
    "PairingError",
    "RateLimited",
    "ServerError",
    "api_error_for_status",
    "is_retryable_status",
]

#: Statuses that are worth a backoff retry on an idempotent request, absent an
#: explicit ``retryable`` flag in the error body.
RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


class ConductorError(Exception):
    """Base class for everything this package raises."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class PairingError(ConductorError):
    """An agent/model/effort combination the API would reject with a 400.

    Raised locally, before any HTTP call, by
    :func:`ctb.conductor.models.validate_pairing`.
    """

    def __init__(
        self,
        message: str,
        *,
        agent: str,
        model: str | None = None,
        effort: str | None = None,
        valid: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.agent = agent
        self.model = model
        self.effort = effort
        self.valid = valid


class CircuitOpen(ConductorError):
    """The circuit breaker is open; the request was not attempted."""

    def __init__(self, *, retry_after: float, opened_by: str | None = None) -> None:
        super().__init__(
            f"conductor circuit open, retry in {retry_after:.1f}s"
            + (f" (opened by {opened_by})" if opened_by else "")
        )
        self.retry_after = retry_after
        self.opened_by = opened_by


class Ambiguous(ConductorError):
    """A write whose outcome is unknown — timeout, reset, or 5xx on a POST.

    ``message_id`` is the caller-supplied idempotency key, when there was one.
    Retry with the identical key; do **not** mint a new one.
    """

    def __init__(
        self,
        message: str,
        *,
        method: str = "POST",
        path: str = "",
        message_id: str | None = None,
        idempotent: bool = False,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.path = path
        self.message_id = message_id
        #: True when the request carried a caller-supplied idempotency key and
        #: may therefore be replayed verbatim.
        self.idempotent = idempotent
        self.cause = cause


class ApiError(ConductorError):
    """A non-2xx HTTP response from the Conductor API.

    The API's error body is ``{code?, userMessage, debugMessage?, retryable?,
    source?}``. ``retryable`` is authoritative when present: ``true`` means retry
    regardless of status, ``false`` means never retry.
    """

    def __init__(
        self,
        status: int,
        body: Any = None,
        *,
        retryable: bool | None = None,
        method: str = "GET",
        path: str = "",
        request_id: str | None = None,
    ) -> None:
        parsed = body if isinstance(body, dict) else {}
        self.status = status
        self.body = body
        self.method = method
        self.path = path
        self.request_id = request_id
        self.code: str | None = _str_or_none(parsed.get("code"))
        self.user_message: str | None = _str_or_none(parsed.get("userMessage"))
        self.debug_message: str | None = _str_or_none(parsed.get("debugMessage"))
        self.source: str | None = _str_or_none(parsed.get("source"))
        flag = parsed.get("retryable")
        if retryable is None and isinstance(flag, bool):
            retryable = flag
        #: ``None`` means "the server did not say"; callers fall back to
        #: :data:`RETRYABLE_STATUSES`.
        self.declared_retryable: bool | None = retryable
        detail = self.user_message or self.debug_message or _truncate(body)
        super().__init__(
            f"{method} {path} -> {status}" + (f": {detail}" if detail else "")
        )

    @property
    def retryable(self) -> bool:
        """Whether a retry is worth attempting for an idempotent request."""
        if self.declared_retryable is not None:
            return self.declared_retryable
        return self.status in RETRYABLE_STATUSES


class RateLimited(ApiError):
    """429. Honour ``Retry-After`` and open the circuit."""

    def __init__(
        self,
        status: int = 429,
        body: Any = None,
        *,
        retry_after: float | None = None,
        method: str = "GET",
        path: str = "",
        request_id: str | None = None,
    ) -> None:
        super().__init__(
            status,
            body,
            retryable=True,
            method=method,
            path=path,
            request_id=request_id,
        )
        self.retry_after = retry_after


class AuthFatal(ApiError):
    """401/403. Never retried — a retry loop on a bad key risks a lockout."""

    @property
    def retryable(self) -> bool:
        return False


class NotFound(ApiError):
    """404. For a session or workspace this means ``DEAD``."""

    @property
    def retryable(self) -> bool:
        return False


class ServerError(ApiError):
    """5xx. Retryable on idempotent requests; ``Ambiguous`` on writes."""


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUSES


def api_error_for_status(
    status: int,
    body: Any = None,
    *,
    method: str = "GET",
    path: str = "",
    retry_after: float | None = None,
    request_id: str | None = None,
) -> ApiError:
    """Map an HTTP status onto the right :class:`ApiError` subclass."""
    if status == 429:
        return RateLimited(
            status,
            body,
            retry_after=retry_after,
            method=method,
            path=path,
            request_id=request_id,
        )
    if status in (401, 403):
        return AuthFatal(status, body, method=method, path=path, request_id=request_id)
    if status == 404:
        return NotFound(status, body, method=method, path=path, request_id=request_id)
    if status >= 500:
        return ServerError(
            status, body, method=method, path=path, request_id=request_id
        )
    return ApiError(status, body, method=method, path=path, request_id=request_id)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _truncate(body: Any, limit: int = 200) -> str:
    if body is None:
        return ""
    text = body if isinstance(body, str) else repr(body)
    return text if len(text) <= limit else text[: limit - 1] + "…"
