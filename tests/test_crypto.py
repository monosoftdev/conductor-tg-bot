"""Envelope encryption for tenant API keys.

The property that matters most is not "it round-trips" — it is that a sealed
blob is *useless anywhere else*. A row swap in the database must not move one
tenant's Conductor key onto another tenant's record, and an ElevenLabs blob must
not open as a Conductor one. That is what the AAD binding buys, and it is what
these tests pin.
"""

from __future__ import annotations

import base64
import uuid

import pytest

from ctb.crypto import (
    KEY_BYTES,
    MasterKey,
    SecretBox,
    SecretError,
    generate_master_key,
    parse_master_keys,
)

TENANT_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
SECRET = "cndk_live_not_a_real_key_0123456789"


def _keys(*ids: str) -> str:
    return ",".join(generate_master_key(kid) for kid in ids)


class TestParsing:
    def test_first_key_is_active(self) -> None:
        box = SecretBox.from_env_value(_keys("v3", "v2", "v1"))
        assert box.active_kid == "v3"
        assert set(box.kids) == {"v1", "v2", "v3"}

    def test_padding_is_optional(self) -> None:
        raw = base64.urlsafe_b64encode(b"K" * KEY_BYTES).decode().rstrip("=")
        assert parse_master_keys(f"v1:{raw}")[0].key == b"K" * KEY_BYTES

    def test_standard_base64_alphabet_is_accepted(self) -> None:
        raw = base64.b64encode(bytes(range(KEY_BYTES))).decode()
        assert parse_master_keys(f"v1:{raw}")[0].key == bytes(range(KEY_BYTES))

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "v1",
            "v1:",
            "v1:not-base64!!",
            "v1:" + base64.urlsafe_b64encode(b"short").decode(),
        ],
    )
    def test_malformed_configuration_is_rejected(self, raw: str) -> None:
        with pytest.raises(SecretError):
            parse_master_keys(raw)

    def test_duplicate_key_ids_are_rejected(self) -> None:
        with pytest.raises(SecretError, match="duplicate"):
            parse_master_keys(_keys("v1") + "," + _keys("v1"))

    def test_a_hostile_key_id_is_rejected(self) -> None:
        material = base64.urlsafe_b64encode(b"X" * KEY_BYTES).decode()
        with pytest.raises(SecretError):
            parse_master_keys(f"v1 drop table:{material}")

    def test_wrong_key_length_is_rejected(self) -> None:
        with pytest.raises(SecretError, match="bytes"):
            MasterKey(kid="v1", key=b"too short")

    @pytest.mark.parametrize(
        "mangled",
        [
            'v1:"{key}"',  # a secret manager that kept the quotes
            "v1:{key}\n",  # a value pasted with its trailing newline
            "v1:{key} ",  # or with a trailing space
        ],
    )
    def test_a_key_that_survived_a_paste_still_decodes(self, mangled: str) -> None:
        """Decoding is deliberately lenient, and must stay that way.

        This is a boot-critical environment variable that reaches the process
        through a secret manager, a deploy UI and a shell. Every one of those
        has a way of adding a quote or a newline, and a strict decoder would
        turn each into a refusal to start.

        Leniency costs nothing here, which is the part worth writing down: a
        discarded stray character can only ever yield the *correct* key or a
        length that fails :class:`MasterKey`'s own check. There is no corruption
        that decodes cleanly to different 32 bytes, so nothing silently seals
        under the wrong key.
        """
        raw = base64.b64encode(b"K" * KEY_BYTES).decode()
        assert parse_master_keys(mangled.format(key=raw))[0].key == b"K" * KEY_BYTES


class TestRoundTrip:
    def test_seal_then_open(self, secret_box: SecretBox) -> None:
        blob = secret_box.seal(SECRET, tenant_id=TENANT_A, purpose="conductor")
        assert secret_box.open(blob, tenant_id=TENANT_A, purpose="conductor") == SECRET

    def test_ciphertext_never_contains_the_plaintext(
        self, secret_box: SecretBox
    ) -> None:
        blob = secret_box.seal(SECRET, tenant_id=TENANT_A, purpose="conductor")
        assert SECRET.encode() not in blob

    def test_two_seals_of_one_secret_differ(self, secret_box: SecretBox) -> None:
        """Fresh data key and nonces every time: no ciphertext equality oracle."""
        first = secret_box.seal(SECRET, tenant_id=TENANT_A, purpose="conductor")
        second = secret_box.seal(SECRET, tenant_id=TENANT_A, purpose="conductor")
        assert first != second

    def test_every_nonce_is_fresh(self, secret_box: SecretBox) -> None:
        """Reusing a GCM nonce under one key is catastrophic, not untidy.

        The data key is random per seal, so ciphertexts would still differ and
        an equality check alone would not notice. Look at the nonces.
        """
        wrap: set[bytes] = set()
        body: set[bytes] = set()
        for _ in range(50):
            blob = secret_box.seal(SECRET, tenant_id=TENANT_A, purpose="c")
            kid_len = blob[4]
            start = 5 + kid_len
            wrap.add(blob[start : start + 12])
            body.add(blob[start + 60 : start + 72])
        assert len(wrap) == 50
        assert len(body) == 50

    def test_unicode_survives(self, secret_box: SecretBox) -> None:
        secret = "ключ-🔑-key"
        blob = secret_box.seal(secret, tenant_id=TENANT_A, purpose="conductor")
        assert secret_box.open(blob, tenant_id=TENANT_A, purpose="conductor") == secret

    def test_empty_secrets_are_refused(self, secret_box: SecretBox) -> None:
        with pytest.raises(SecretError):
            secret_box.seal("", tenant_id=TENANT_A, purpose="conductor")


class TestBinding:
    """The reason this is AES-GCM with AAD rather than Fernet."""

    def test_another_tenants_row_cannot_open_it(self, secret_box: SecretBox) -> None:
        blob = secret_box.seal(SECRET, tenant_id=TENANT_A, purpose="conductor")
        with pytest.raises(SecretError, match="authentication"):
            secret_box.open(blob, tenant_id=TENANT_B, purpose="conductor")

    def test_another_purpose_cannot_open_it(self, secret_box: SecretBox) -> None:
        blob = secret_box.seal(SECRET, tenant_id=TENANT_A, purpose="conductor")
        with pytest.raises(SecretError, match="authentication"):
            secret_box.open(blob, tenant_id=TENANT_A, purpose="elevenlabs")

    def test_a_flipped_bit_is_detected(self, secret_box: SecretBox) -> None:
        blob = bytearray(secret_box.seal(SECRET, tenant_id=TENANT_A, purpose="c"))
        blob[-1] ^= 0x01
        with pytest.raises(SecretError, match="authentication"):
            secret_box.open(bytes(blob), tenant_id=TENANT_A, purpose="c")

    def test_a_foreign_master_key_cannot_open_it(self) -> None:
        theirs = SecretBox.from_env_value(_keys("v1"))
        ours = SecretBox.from_env_value(_keys("v1"))  # same kid, different bytes
        blob = theirs.seal(SECRET, tenant_id=TENANT_A, purpose="c")
        with pytest.raises(SecretError, match="authentication"):
            ours.open(blob, tenant_id=TENANT_A, purpose="c")

    @pytest.mark.parametrize(
        "blob", [None, b"", b"nope", b"ctb1", b"ctb1\x02ab", b"ctb1\x00"]
    )
    def test_garbage_is_rejected_cleanly(
        self, secret_box: SecretBox, blob: bytes | None
    ) -> None:
        with pytest.raises(SecretError):
            secret_box.open(blob, tenant_id=TENANT_A, purpose="c")


class TestRotation:
    def test_a_retired_key_still_opens_old_blobs(self) -> None:
        """Rotation is prepend-and-deploy: no dual-write window, no downtime."""
        v1, v2 = parse_master_keys(_keys("v1"))[0], parse_master_keys(_keys("v2"))[0]
        before = SecretBox([v1])
        blob = before.seal(SECRET, tenant_id=TENANT_A, purpose="c")

        after = SecretBox([v2, v1])
        assert after.active_kid == "v2"
        assert after.open(blob, tenant_id=TENANT_A, purpose="c") == SECRET

    def test_new_writes_use_the_active_key(self, secret_box: SecretBox) -> None:
        blob = secret_box.seal(SECRET, tenant_id=TENANT_A, purpose="c")
        assert blob.startswith(b"ctb1" + bytes([2]) + b"v2")

    def test_needs_rewrap_tracks_the_active_key(self, secret_box: SecretBox) -> None:
        assert secret_box.needs_rewrap("v1") is True
        assert secret_box.needs_rewrap(secret_box.active_kid) is False
        assert secret_box.needs_rewrap(None) is True

    def test_an_unloaded_key_id_says_so(self, secret_box: SecretBox) -> None:
        stranger = SecretBox.from_env_value(generate_master_key("v99"))
        blob = stranger.seal(SECRET, tenant_id=TENANT_A, purpose="c")
        with pytest.raises(SecretError, match="not loaded"):
            secret_box.open(blob, tenant_id=TENANT_A, purpose="c")


class TestOperationalSafety:
    def test_self_check_passes_for_every_loaded_key(
        self, secret_box: SecretBox
    ) -> None:
        secret_box.self_check()

    def test_self_check_actually_round_trips(
        self, secret_box: SecretBox, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is the boot canary. A no-op canary is worse than none.

        A misconfigured ``CTB_MASTER_KEYS`` must kill the process at boot, not
        surface as an unexplained failure on the first customer's first prompt.
        """
        monkeypatch.setattr(
            SecretBox, "open", lambda *_a, **_k: "something else entirely"
        )
        with pytest.raises(SecretError, match="round trip"):
            secret_box.self_check()

    def test_fingerprint_is_stable_and_not_the_secret(
        self, secret_box: SecretBox
    ) -> None:
        digest = SecretBox.fingerprint(SECRET)
        assert digest == SecretBox.fingerprint(SECRET)
        assert digest != SecretBox.fingerprint(SECRET + "x")
        assert SECRET not in digest
        assert len(digest) == 16

    def test_repr_never_shows_key_material(self, secret_box: SecretBox) -> None:
        assert "v2" in repr(secret_box)
        key = parse_master_keys(_keys("v1"))[0]
        assert base64.b64encode(key.key).decode() not in repr(key)
        assert repr(key) == "MasterKey(kid='v1')"
