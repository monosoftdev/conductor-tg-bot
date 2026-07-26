"""Shared plumbing for the repository modules. Internal — not a public API.

Two things live here that every module needs: the ``UNSET`` sentinel that lets a
partial update distinguish "leave this column alone" from "set it to NULL", and
the small coercions that turn ``aiosqlite.Row`` values (typed ``Any``) into the
concrete types the row dataclasses declare.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final, Self

__all__ = [
    "UNSET",
    "Maybe",
    "Unset",
    "as_bool",
    "as_int",
    "as_opt_int",
    "as_opt_str",
    "as_str",
    "assign",
    "bit",
    "content_hash",
    "dumps",
    "loads",
    "update_sql",
]


class Unset:
    """Sentinel meaning "this column was not mentioned in the update".

    A singleton so ``value is UNSET`` and ``isinstance(value, Unset)`` agree.
    """

    __slots__ = ()
    _instance: Self | None = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


#: Pass this (or simply omit the argument) to leave a column untouched.
UNSET: Final[Unset] = Unset()

#: An argument that may be absent. ``None`` still means "write NULL".
type Maybe[T] = T | Unset


def assign(target: dict[str, Any], **columns: Any) -> dict[str, Any]:
    """Copy every column whose value is not :data:`UNSET` into ``target``."""
    for name, value in columns.items():
        if not isinstance(value, Unset):
            target[name] = value
    return target


def update_sql(table: str, columns: Mapping[str, Any], where: str) -> str:
    """``UPDATE <table> SET a = ?, b = ? WHERE <where>``.

    Table and column names always come from module-level literals in this
    package — never from user input — so the interpolation is safe. Values are
    always bound.
    """
    sets = ", ".join(f"{name} = ?" for name in columns)
    return f"UPDATE {table} SET {sets} WHERE {where}"


# -- coercions ---------------------------------------------------------------


def as_bool(value: Any) -> bool:
    return bool(value)


def bit(value: bool) -> int:
    """Booleans are stored as INTEGER 0/1."""
    return 1 if value else 0


def as_int(value: Any, default: int = 0) -> int:
    return default if value is None else int(value)


def as_opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def as_str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def as_opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


# -- JSON --------------------------------------------------------------------


def dumps(value: Any) -> str:
    """Compact JSON. ``default=str`` so an odd value degrades instead of raising."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def content_hash(payload: str) -> str:
    """Stable digest of a rendered delivery payload.

    Used by the boot-time re-send guard: an orphaned ``sending`` row whose hash
    already appears on a recently ``sent`` row in the same chat almost certainly
    landed, so it is skipped instead of duplicated. Truncated to 32 hex chars —
    collision-irrelevant at this scale and cheap to eyeball in a log line.
    """
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def loads(text: str | None) -> dict[str, Any]:
    """Parse a stored JSON object, tolerating NULL and corruption."""
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}
