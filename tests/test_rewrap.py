"""Master-key rotation, against a real database.

This is the module `SECURITY.md` tells an operator to run after a master key
leaks, and it had no test at all — `needs_rewrap` on :class:`SecretBox` was the
only thing the word matched in this directory. What follows exercises the path
an operator actually walks: seal under ``v1``, load ``v2`` in front of it,
re-seal, drop ``v1``, and confirm every secret still opens.

Deliberately synchronous, like :func:`ctb.rewrap.rewrap` itself. Rotation runs
as a one-shot operator command with no event loop anywhere near it.
"""

from __future__ import annotations

import uuid
from typing import LiteralString, cast

import psycopg
import pytest

from ctb.crypto import SecretBox, SecretError
from ctb.rewrap import RewrapError, assert_bypasses_rls, main, rewrap
from tests.pg import (
    BOOTSTRAP_TENANT_ID,
    OTHER_TENANT_ID,
    admin_dsn,
    app_dsn,
    worker_dsn,
)

pytestmark = pytest.mark.db

#: One key each, so a box can be built that holds *only* the old one — which is
#: the state the operator is in before step 1 of the runbook.
V1 = "v1:T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT08="
V2 = "v2:VFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFQ="

#: ``(ciphertext, kid, fingerprint, purpose)`` for each sealed column on
#: ``tenants``. Named here so a fourth credential added later fails this file
#: rather than quietly going un-rotated in production.
COLUMNS = (
    ("conductor_key_ct", "conductor_key_kid", "conductor_key_fp", "conductor_api_key"),
    (
        "elevenlabs_key_ct",
        "elevenlabs_key_kid",
        "elevenlabs_key_fp",
        "elevenlabs_api_key",
    ),
    ("github_key_ct", "github_key_kid", "github_key_fp", "github_api_token"),
)

SECRETS = {
    "conductor_api_key": "conductor-key-for-the-tenant",
    "elevenlabs_api_key": "sk_elevenlabs_speech_key",
    "github_api_token": "github_pat_for_ci_visibility",
}


def old_box() -> SecretBox:
    """What the process held before rotation: ``v1`` alone, and active."""
    return SecretBox.from_env_value(V1)


def new_box() -> SecretBox:
    """After step 1: ``v2`` seals, ``v1`` still opens."""
    return SecretBox.from_env_value(f"{V2},{V1}")


#: Every sealed column, in one flat list. The order is the order `read_row`
#: returns.
SEALED = [name for group in COLUMNS for name in group[:3]]


def _literal(sql: str) -> LiteralString:
    """Assert that an interpolated statement was built only from constants.

    Column names here come from :data:`COLUMNS`, a module-level tuple — never
    from a test argument, and never from anything a fixture supplies. The cast
    says so out loud, the same way ``tests/pg.py`` does for its ``TRUNCATE``.
    """
    return cast(LiteralString, sql)


def seal_all(tenant_id: uuid.UUID, box: SecretBox) -> None:
    """Store all three credentials for one tenant, sealed under ``box``."""
    with psycopg.connect(admin_dsn(), autocommit=True) as conn:
        for ct, kid, fp, purpose in COLUMNS:
            plaintext = SECRETS[purpose]
            conn.execute(
                _literal(
                    f"UPDATE tenants SET {ct} = %s, {kid} = %s, {fp} = %s WHERE id = %s"
                ),
                (
                    box.seal(plaintext, tenant_id=tenant_id, purpose=purpose),
                    box.active_kid,
                    box.fingerprint_of(plaintext, tenant_id=tenant_id),
                    tenant_id,
                ),
            )


def read_row(tenant_id: uuid.UUID) -> dict[str, object]:
    with psycopg.connect(admin_dsn(), autocommit=True) as conn:
        row = conn.execute(
            _literal(f"SELECT {', '.join(SEALED)} FROM tenants WHERE id = %s"),
            (tenant_id,),
        ).fetchone()
    assert row is not None
    return dict(zip(SEALED, row, strict=True))


class TestRotation:
    def test_every_secret_still_opens_after_its_master_key_is_dropped(
        self, pg_reset: tuple[str, ...]
    ) -> None:
        """The whole runbook, end to end. This is the test that matters.

        Step 4 is the destructive one: once ``v1`` is gone from
        ``CTB_MASTER_KEYS`` nothing can recover a blob still wrapped by it, so
        the assertion is made with a box that holds ``v2`` and nothing else.
        """
        seal_all(BOOTSTRAP_TENANT_ID, old_box())

        assert rewrap(worker_dsn(), new_box()) == 3

        after_v1_is_dropped = SecretBox.from_env_value(V2)
        row = read_row(BOOTSTRAP_TENANT_ID)
        for ct, kid, _fp, purpose in COLUMNS:
            assert row[kid] == "v2"
            opened = after_v1_is_dropped.open(
                bytes(row[ct]),  # pyright: ignore[reportArgumentType]
                tenant_id=BOOTSTRAP_TENANT_ID,
                purpose=purpose,
            )
            assert opened == SECRETS[purpose]

    def test_the_fingerprint_is_recomputed_under_the_new_key(
        self, pg_reset: tuple[str, ...]
    ) -> None:
        """``fingerprint_of`` is keyed by a subkey of the *active* master key.

        Leaving the old value behind is not a leak — the pools only need it to
        be stable — but it silently breaks the "that is already the stored key"
        check in ``/key``, so an operator's rotation makes every tenant's next
        key submission look like a change.
        """
        seal_all(BOOTSTRAP_TENANT_ID, old_box())
        before = read_row(BOOTSTRAP_TENANT_ID)

        rewrap(worker_dsn(), new_box())

        after = read_row(BOOTSTRAP_TENANT_ID)
        box = new_box()
        for _ct, _kid, fp, purpose in COLUMNS:
            expected = box.fingerprint_of(
                SECRETS[purpose], tenant_id=BOOTSTRAP_TENANT_ID
            )
            assert after[fp] == expected
            assert after[fp] != before[fp], "the old fingerprint survived"

    def test_a_second_run_changes_nothing(self, pg_reset: tuple[str, ...]) -> None:
        """Rotation is safe to re-run: a half-finished one is resumable."""
        seal_all(BOOTSTRAP_TENANT_ID, old_box())
        assert rewrap(worker_dsn(), new_box()) == 3

        settled = read_row(BOOTSTRAP_TENANT_ID)
        assert rewrap(worker_dsn(), new_box()) == 0
        assert read_row(BOOTSTRAP_TENANT_ID) == settled

    def test_rows_already_on_the_active_key_are_left_alone(
        self, pg_reset: tuple[str, ...]
    ) -> None:
        """Nothing is re-encrypted for the sake of it — nonces would churn."""
        seal_all(BOOTSTRAP_TENANT_ID, new_box())
        before = read_row(BOOTSTRAP_TENANT_ID)

        assert rewrap(worker_dsn(), new_box()) == 0
        assert read_row(BOOTSTRAP_TENANT_ID) == before

    def test_every_tenant_is_rotated(self, pg_reset: tuple[str, ...]) -> None:
        """A worker command with no tenant in scope must reach all of them."""
        seal_all(BOOTSTRAP_TENANT_ID, old_box())
        seal_all(OTHER_TENANT_ID, old_box())

        assert rewrap(worker_dsn(), new_box()) == 6

        box = SecretBox.from_env_value(V2)
        for tenant_id in (BOOTSTRAP_TENANT_ID, OTHER_TENANT_ID):
            row = read_row(tenant_id)
            for ct, _kid, _fp, purpose in COLUMNS:
                assert (
                    box.open(
                        bytes(row[ct]),  # pyright: ignore[reportArgumentType]
                        tenant_id=tenant_id,
                        purpose=purpose,
                    )
                    == SECRETS[purpose]
                )

    def test_the_tenant_binding_survives_rotation(
        self, pg_reset: tuple[str, ...]
    ) -> None:
        """Re-sealing must not loosen the AAD. A row swap still fails."""
        seal_all(BOOTSTRAP_TENANT_ID, old_box())
        rewrap(worker_dsn(), new_box())

        row = read_row(BOOTSTRAP_TENANT_ID)
        with pytest.raises(SecretError):
            new_box().open(
                bytes(row["conductor_key_ct"]),  # pyright: ignore[reportArgumentType]
                tenant_id=OTHER_TENANT_ID,
                purpose="conductor_api_key",
            )

    def test_a_blob_no_key_can_open_stops_the_run(
        self, pg_reset: tuple[str, ...]
    ) -> None:
        """Better a loud failure than a partial rotation reported as complete.

        The transaction is never committed, so the run is re-runnable once the
        missing key is put back in ``CTB_MASTER_KEYS``.
        """
        seal_all(BOOTSTRAP_TENANT_ID, old_box())

        only_v2 = SecretBox.from_env_value(V2)
        with pytest.raises(SecretError):
            rewrap(worker_dsn(), only_v2)

        row = read_row(BOOTSTRAP_TENANT_ID)
        assert row["conductor_key_kid"] == "v1", "a failed run committed anyway"


class TestWrongRole:
    """The guard that keeps a breach runbook from succeeding at nothing."""

    def test_a_role_under_row_level_security_is_refused(
        self, pg_reset: tuple[str, ...]
    ) -> None:
        with psycopg.connect(app_dsn()) as conn, pytest.raises(RewrapError) as caught:
            assert_bypasses_rls(conn)
        assert "ctb_app" in str(caught.value)
        assert "SYSTEM_DATABASE_URL" in str(caught.value)

    def test_the_worker_role_is_accepted(self, pg_reset: tuple[str, ...]) -> None:
        with psycopg.connect(worker_dsn()) as conn:
            assert_bypasses_rls(conn)  # does not raise

    def test_the_app_dsn_never_reaches_a_single_row(
        self, pg_reset: tuple[str, ...]
    ) -> None:
        """The refusal happens before any tenant row is touched."""
        seal_all(BOOTSTRAP_TENANT_ID, old_box())
        with pytest.raises(RewrapError):
            rewrap(app_dsn(), new_box())
        assert read_row(BOOTSTRAP_TENANT_ID)["conductor_key_kid"] == "v1"


class TestCommandLine:
    def test_new_key_prints_a_usable_entry(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--new-key", "v3"]) == 0
        printed = capsys.readouterr().out.strip()

        box = SecretBox.from_env_value(printed)
        assert box.active_kid == "v3"
        blob = box.seal("x", tenant_id=BOOTSTRAP_TENANT_ID, purpose="p")
        assert box.open(blob, tenant_id=BOOTSTRAP_TENANT_ID, purpose="p") == "x"

    def test_two_generated_keys_differ(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["--new-key", "v3"])
        main(["--new-key", "v3"])
        first, second = capsys.readouterr().out.split()
        assert first != second

    def test_doing_nothing_is_an_error(self) -> None:
        with pytest.raises(SystemExit) as caught:
            main([])
        assert caught.value.code != 0
