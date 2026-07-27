"""Translate this project's SQL dialect (``?`` placeholders) to psycopg's.

Every statement in :mod:`ctb.db.repo` is written with ``?`` placeholders — the
form the repository layer was born with, and the form
:func:`ctb.db.repo._util.update_sql` *generates* at runtime from a column
mapping. psycopg speaks ``%s`` and treats ``%`` as its own escape character, so
one translation pass sits at the :class:`ctb.db.connection.Database` boundary
rather than in 275 hand-edited call sites.

Two rules, applied outside string literals, quoted identifiers, dollar-quoted
bodies and comments:

* ``?``  → ``%s``  — a bind placeholder.
* ``%``  → ``%%``  — a literal percent, which psycopg un-escapes on the way out.

Both rules stop at a quote. ``LIKE '%x%'`` keeps its wildcards, and a ``?``
inside a literal stays a question mark.

.. warning::
   PostgreSQL's ``jsonb`` ``?`` (key-exists) operator cannot be written in this
   dialect: it is indistinguishable from a placeholder. Use ``jsonb_exists(a,
   b)`` instead. Nothing in this project uses either today.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["to_pyformat"]

#: ``$$`` or ``$tag$``. Postgres dollar quoting, which nests nothing.
_DOLLAR_OPEN: Final = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")

#: Bounded so a pathological caller cannot grow it without limit. Every query
#: in this project is either a module-level literal or one of the finitely many
#: shapes ``update_sql`` produces, so the cache converges almost immediately.
_CACHE_MAX: Final = 4096
_cache: Final[dict[str, str]] = {}


def _escaped(text: str) -> str:
    """A span psycopg must see verbatim: only ``%`` needs protecting."""
    return text.replace("%", "%%")


def to_pyformat(sql: str) -> str:
    """Rewrite ``?`` placeholders to ``%s`` and escape literal ``%``.

    Idempotent only for SQL with no ``%`` and no ``?``; the result is meant to
    be handed straight to psycopg, never translated twice.
    """
    cached = _cache.get(sql)
    if cached is not None:
        return cached

    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        char = sql[i]
        if char == "'":
            # A single-quoted literal, in which '' is an escaped quote.
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(_escaped(sql[i : min(j + 1, n)]))
            i = j + 1
        elif char == '"':
            # A quoted identifier. "" escapes a quote here too.
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            out.append(_escaped(sql[i : min(j + 1, n)]))
            i = j + 1
        elif char == "$" and (match := _DOLLAR_OPEN.match(sql, i)):
            tag = match.group(0)
            end = sql.find(tag, match.end())
            j = n if end < 0 else end + len(tag)
            out.append(_escaped(sql[i:j]))
            i = j
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            j = n if end < 0 else end
            out.append(_escaped(sql[i:j]))
            i = j
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            j = n if end < 0 else end + 2
            out.append(_escaped(sql[i:j]))
            i = j
        elif char == "?":
            out.append("%s")
            i += 1
        elif char == "%":
            out.append("%%")
            i += 1
        else:
            out.append(char)
            i += 1

    result = "".join(out)
    if len(_cache) < _CACHE_MAX:
        _cache[sql] = result
    return result
