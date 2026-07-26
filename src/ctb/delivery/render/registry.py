"""The adapter registry: one transcript message in, blocks out, never raises.

This is the module the poller calls. Its contract is narrow and absolute:

    **A renderer bug must never stall delivery.**

So every ``matches`` and every ``render`` is wrapped. An adapter that raises is
logged, counted in ``unknown_content_types``, and the message degrades to
:class:`~ctb.delivery.render.adapters.unknown.UnknownAdapter` — which is
itself wrapped, and whose worst case is an empty block list. There is no input
for which :meth:`Registry.render` propagates an exception, and no input for
which it loops: the last adapter matches everything.

The registry also applies the default visibility policy from PLAN §Adapters
(:func:`~ctb.delivery.render.types.is_visible`) as a final filter, so a
verbosity mistake inside one adapter cannot leak reasoning or tool spam into a
``quiet`` chat. Activity lines bypass the filter — they go to the status card,
never to the chat, and a working turn with no activity looks frozen.

Nothing here decides *whether* to deliver. That is the cursor's job. This
module only decides what a message looks like.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ctb.conductor.models import TranscriptMessage
from ctb.delivery.render.adapters import (
    Adapter,
    ProseRenderer,
    UnknownAdapter,
    UnknownRecord,
    default_adapters,
    shape_signature,
)
from ctb.delivery.render.html import markdown_to_html
from ctb.delivery.render.types import (
    ActivityLine,
    Block,
    CodeBlock,
    DocumentBlock,
    RenderContext,
    TextBlock,
    Verbosity,
    is_visible,
)
from ctb.logging import get_logger

__all__ = [
    "RenderResult",
    "Registry",
    "UnknownRecord",
    "default_registry",
    "render_message",
    "reset_default_registry",
    "set_default_registry",
]

_log: Final = get_logger(__name__)

#: The concrete block types the outbox knows how to send. An adapter returning
#: anything else is a bug, and the bug is dropped rather than propagated.
_BLOCK_TYPES: Final = (TextBlock, CodeBlock, DocumentBlock, ActivityLine)


@dataclass(frozen=True, slots=True)
class RenderResult:
    """Everything the outbox and ``/health`` need from one rendered message."""

    #: Chat blocks and activity lines, in emission order.
    blocks: tuple[Block, ...] = ()
    #: Which adapter produced them, for logs and tests.
    adapter: str = ""
    #: Rows to upsert into ``unknown_content_types``. Content-free by design.
    unknown: tuple[UnknownRecord, ...] = ()
    #: True when an adapter raised and the message fell back to unknown.
    degraded: bool = False
    #: ``repr`` of the failure behind :attr:`degraded`. Never contains content.
    error: str = ""

    @property
    def chat(self) -> tuple[Block, ...]:
        """The blocks that go to the chat."""
        return tuple(b for b in self.blocks if not isinstance(b, ActivityLine))

    @property
    def activity(self) -> tuple[str, ...]:
        """The one-liners that go to the status card."""
        return tuple(b.text for b in self.blocks if isinstance(b, ActivityLine))

    @property
    def is_empty(self) -> bool:
        return not self.blocks


class Registry:
    """An ordered adapter chain with the failure policy baked in."""

    def __init__(
        self,
        adapters: Sequence[Adapter] | None = None,
        *,
        prose: ProseRenderer = markdown_to_html,
    ) -> None:
        """``adapters`` defaults to the production chain.

        A custom chain is accepted for tests and for wiring experiments. Two
        things are enforced regardless of what is passed in: there *is* an
        :class:`UnknownAdapter` (a registry that can fail to match is a
        registry that can drop a reply), and it is **last** (it matches
        everything, so anything behind it would be dead code).
        """
        supplied: list[Adapter] = list(
            adapters if adapters is not None else default_adapters(prose=prose)
        )
        fallback = next(
            (a for a in supplied if isinstance(a, UnknownAdapter)),
            None,
        ) or UnknownAdapter(prose)
        chain = [a for a in supplied if not isinstance(a, UnknownAdapter)]
        chain.append(fallback)
        self._adapters: tuple[Adapter, ...] = tuple(chain)
        self._fallback: UnknownAdapter = fallback

    @property
    def adapters(self) -> tuple[Adapter, ...]:
        return self._adapters

    @property
    def fallback(self) -> UnknownAdapter:
        return self._fallback

    # -- rendering ------------------------------------------------------------

    def render(
        self,
        message: TranscriptMessage,
        context: RenderContext | Verbosity | None = None,
    ) -> RenderResult:
        """Render one message. Guaranteed not to raise, for any input."""
        ctx = _as_context(context)

        for adapter in self._adapters:
            if adapter is self._fallback:
                break
            try:
                claimed = adapter.matches(message.type, message.content)
            except Exception as exc:  # noqa: BLE001 - a bug here must not stall
                self._log_failure(adapter, message, exc, "matches")
                continue
            if not claimed:
                continue
            try:
                produced = adapter.render(message, ctx)
            except Exception as exc:  # noqa: BLE001 - degrade, never propagate
                self._log_failure(adapter, message, exc, "render")
                return self._degraded(message, ctx, adapter, exc)
            return RenderResult(
                blocks=self._finalize(produced, ctx, adapter, message),
                adapter=adapter.name,
            )

        return self._fallback_result(message, ctx)

    def select(self, message: TranscriptMessage) -> Adapter:
        """The adapter that would claim ``message``. Never raises."""
        for adapter in self._adapters:
            if adapter is self._fallback:
                break
            try:
                if adapter.matches(message.type, message.content):
                    return adapter
            except Exception as exc:  # noqa: BLE001 - a bad probe is not fatal
                self._log_failure(adapter, message, exc, "matches")
        return self._fallback

    # -- fallback paths -------------------------------------------------------

    def _fallback_result(
        self,
        message: TranscriptMessage,
        ctx: RenderContext,
        *,
        reason: str = "",
        degraded: bool = False,
        error: str = "",
    ) -> RenderResult:
        try:
            produced = self._fallback.render(message, ctx)
        except Exception as exc:  # noqa: BLE001 - the safety net has a net
            self._log_failure(self._fallback, message, exc, "render")
            produced = []
            degraded = True
            error = error or repr(exc)
            reason = reason or "unknown_adapter_failed"
        try:
            record = self._fallback.record(message, reason=reason)
        except Exception as exc:  # noqa: BLE001 - never lose the render for it
            self._log_failure(self._fallback, message, exc, "record")
            record = UnknownRecord(
                type=message.type or "<empty>",
                shape_signature="unavailable",
                session_id=message.session_id,
                message_id=message.id,
                reason=reason,
            )
        return RenderResult(
            blocks=self._finalize(produced, ctx, self._fallback, message),
            adapter=self._fallback.name,
            unknown=(record,),
            degraded=degraded,
            error=error,
        )

    def _degraded(
        self,
        message: TranscriptMessage,
        ctx: RenderContext,
        adapter: Adapter,
        exc: Exception,
    ) -> RenderResult:
        return self._fallback_result(
            message,
            ctx,
            reason=f"{adapter.name} raised",
            degraded=True,
            error=repr(exc),
        )

    # -- shared tail ----------------------------------------------------------

    def _finalize(
        self,
        produced: object,
        ctx: RenderContext,
        adapter: Adapter,
        message: TranscriptMessage,
    ) -> tuple[Block, ...]:
        """Drop junk, then apply the default visibility policy.

        ``produced`` is typed ``object`` on purpose: it came from an adapter,
        and an adapter is exactly the thing this module refuses to trust.
        """
        if not isinstance(produced, Sequence) or isinstance(produced, str | bytes):
            if produced is not None:
                self._log_bad_output(adapter, message)
            return ()
        blocks: list[Block] = []
        dropped = False
        for item in produced:
            if not isinstance(item, _BLOCK_TYPES):
                dropped = True
                continue
            if isinstance(item, ActivityLine) or is_visible(item.kind, ctx.verbosity):
                blocks.append(item)
        if dropped:
            self._log_bad_output(adapter, message)
        return tuple(blocks)

    def _log_failure(
        self,
        adapter: Adapter,
        message: TranscriptMessage,
        exc: Exception,
        phase: str,
    ) -> None:
        _log.warning(
            "render.adapter_failed",
            adapter=adapter.name,
            phase=phase,
            error=repr(exc),
            message_type=message.type,
            # Shape only — the content itself is the user's source code.
            shape=_safe_signature(message),
            session_id=message.session_id,
            transcript_message_id=message.id,
        )

    def _log_bad_output(self, adapter: Adapter, message: TranscriptMessage) -> None:
        _log.warning(
            "render.adapter_bad_output",
            adapter=adapter.name,
            message_type=message.type,
            session_id=message.session_id,
            transcript_message_id=message.id,
        )


def _safe_signature(message: TranscriptMessage) -> str:
    try:
        return shape_signature(message.content)
    except Exception:  # noqa: BLE001 - logging must not raise either
        return "unavailable"


def _as_context(context: RenderContext | Verbosity | None) -> RenderContext:
    """Accept a full context, a bare verbosity, or nothing."""
    if context is None:
        return RenderContext()
    if isinstance(context, Verbosity):
        return RenderContext(verbosity=context)
    return context


# -- process-wide default -----------------------------------------------------

_default: Registry | None = None


def default_registry() -> Registry:
    """The shared production registry, built on first use."""
    global _default
    if _default is None:
        _default = Registry()
    return _default


def set_default_registry(registry: Registry) -> None:
    """Install a registry process-wide (wiring a richer prose renderer, tests)."""
    global _default
    _default = registry


def reset_default_registry() -> None:
    global _default
    _default = None


def render_message(
    message: TranscriptMessage,
    context: RenderContext | Verbosity | None = None,
    *,
    registry: Registry | None = None,
) -> RenderResult:
    """Render one transcript message with the default registry. Never raises."""
    return (registry or default_registry()).render(message, context)
