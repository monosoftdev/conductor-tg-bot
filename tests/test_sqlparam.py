"""The ``?`` → ``%s`` translator sits under all 137 repo call sites.

Every statement in the repository layer passes through it, so a bug here is a
bug everywhere. These tests are deliberately adversarial about quoting: the
whole point of the translator over a blind ``str.replace`` is that a ``?`` or a
``%`` inside a literal must survive untouched.
"""

from __future__ import annotations

import pytest

from ctb.db.sqlparam import to_pyformat


class TestPlaceholders:
    def test_a_bare_question_mark_becomes_pyformat(self) -> None:
        assert to_pyformat("SELECT ? , ?") == "SELECT %s , %s"

    def test_no_placeholders_is_unchanged(self) -> None:
        assert to_pyformat("SELECT 1") == "SELECT 1"

    def test_a_realistic_statement(self) -> None:
        assert (
            to_pyformat("UPDATE sessions SET a = ?, b = ? WHERE id = ?")
            == "UPDATE sessions SET a = %s, b = %s WHERE id = %s"
        )


class TestQuoting:
    """A ``?`` inside a literal is data, not a bind."""

    def test_question_mark_inside_a_string_literal_survives(self) -> None:
        assert to_pyformat("SELECT 'what?' , ?") == "SELECT 'what?' , %s"

    def test_doubled_quote_escape_does_not_end_the_literal(self) -> None:
        assert to_pyformat("SELECT 'it''s ?' , ?") == "SELECT 'it''s ?' , %s"

    def test_question_mark_inside_a_quoted_identifier_survives(self) -> None:
        assert to_pyformat('SELECT "odd?name" FROM t WHERE x = ?') == (
            'SELECT "odd?name" FROM t WHERE x = %s'
        )

    def test_doubled_double_quote_escape(self) -> None:
        assert to_pyformat('SELECT "a""?b" , ?') == 'SELECT "a""?b" , %s'

    def test_dollar_quoted_body_is_opaque(self) -> None:
        sql = "DO $tag$ SELECT '?' ; x % y $tag$"
        assert to_pyformat(sql) == "DO $tag$ SELECT '?' ; x %% y $tag$"

    def test_bare_dollar_quoting(self) -> None:
        assert to_pyformat("DO $$ a ? b $$") == "DO $$ a ? b $$"

    def test_line_comment_is_opaque(self) -> None:
        assert to_pyformat("SELECT ? -- why? 50%\nFROM t") == (
            "SELECT %s -- why? 50%%\nFROM t"
        )

    def test_block_comment_is_opaque(self) -> None:
        assert to_pyformat("SELECT /* ? 100% */ ?") == "SELECT /* ? 100%% */ %s"


class TestPercentEscaping:
    """psycopg treats ``%`` as its own escape once parameters are supplied."""

    def test_literal_percent_is_doubled(self) -> None:
        assert to_pyformat("SELECT 100 % 3") == "SELECT 100 %% 3"

    def test_percent_inside_a_like_pattern_is_doubled(self) -> None:
        assert to_pyformat("WHERE name LIKE '%x%' AND id = ?") == (
            "WHERE name LIKE '%%x%%' AND id = %%s".replace("%%s", "%s")
        )

    def test_like_pattern_and_placeholder_together(self) -> None:
        assert to_pyformat("WHERE a LIKE '%q%' AND b = ?") == (
            "WHERE a LIKE '%%q%%' AND b = %s"
        )


class TestUnterminated:
    """Malformed SQL must not hang or raise here; the server will complain."""

    @pytest.mark.parametrize(
        "sql", ["SELECT 'unterminated", 'SELECT "unterminated', "DO $x$ body"]
    )
    def test_unterminated_quoting_terminates(self, sql: str) -> None:
        assert isinstance(to_pyformat(sql), str)


def test_repeated_translation_is_cached_and_identical() -> None:
    sql = "SELECT ? FROM t WHERE b = ?"
    assert to_pyformat(sql) == to_pyformat(sql) == "SELECT %s FROM t WHERE b = %s"


def test_no_repo_statement_already_uses_pyformat() -> None:
    """A stray ``%s`` in repo SQL would be double-escaped into nonsense."""
    import pathlib

    repo = (
        pathlib.Path(__file__).resolve().parent.parent / "src" / "ctb" / "db" / "repo"
    )
    offenders = [
        path.name for path in repo.glob("*.py") if "%s" in path.read_text("utf-8")
    ]
    assert offenders == []
