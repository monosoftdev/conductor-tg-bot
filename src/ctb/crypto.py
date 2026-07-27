"""Envelope encryption for the secrets this service holds on someone's behalf.

A tenant's Conductor API key can read every transcript in their organisation and
spend their money. It is the first secret this project has ever persisted, so
the scheme is deliberately conservative:

**AES-256-GCM, not Fernet.** Fernet has no additional authenticated data. AAD is
the entire point here: every ciphertext is bound to ``(kid, tenant_id,
purpose)``, so copying tenant A's sealed blob onto tenant B's row does not
decrypt — it fails authentication. With a Fernet blob that row swap would
succeed silently.

**Envelope, not direct.** A fresh 256-bit data key encrypts the secret; the
master key only ever wraps that data key. Re-wrapping under a new master key
therefore re-encrypts 48 bytes rather than the payload, which is what makes
:mod:`ctb.rewrap` cheap.

**Several master keys, one active.** ``CTB_MASTER_KEYS`` is an ordered list;
the first entry seals new writes and every entry can open. Rotation is: prepend
a key, deploy, re-wrap, drop the old key on the next deploy. No dual-write
window and no atomic swap.

Blob layout, all big-endian::

    b"ctb1" | len(kid) | kid | wrap_nonce(12) | wrapped_dek(48) | nonce(12) | body

Nothing here logs, and :class:`SecretBox` has no ``__repr__`` exposing key
material.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "KEY_BYTES",
    "MasterKey",
    "SecretBox",
    "SecretError",
    "generate_master_key",
    "parse_master_keys",
]

_MAGIC: Final = b"ctb1"
_NONCE_BYTES: Final = 12
#: 32-byte data key plus GCM's 16-byte tag.
_WRAPPED_BYTES: Final = 48
KEY_BYTES: Final = 32

#: Key ids appear in the database and in logs, so keep them boring.
_KID_RE: Final = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


class SecretError(RuntimeError):
    """A sealed blob is malformed, or no loaded master key can open it."""


@dataclass(frozen=True, slots=True)
class MasterKey:
    """One 256-bit key encryption key and the id it is referenced by."""

    kid: str
    key: bytes

    def __post_init__(self) -> None:
        if not _KID_RE.match(self.kid):
            raise SecretError(
                f"master key id {self.kid!r} is not [A-Za-z0-9_.-]{{1,32}}"
            )
        if len(self.key) != KEY_BYTES:
            raise SecretError(
                f"master key {self.kid!r} is {len(self.key)} bytes, need {KEY_BYTES}"
            )

    def __repr__(self) -> str:
        return f"MasterKey(kid={self.kid!r})"


def parse_master_keys(raw: str) -> tuple[MasterKey, ...]:
    """Parse ``"v2:<base64url-32B>,v1:<base64url-32B>"``.

    The first entry is the active key. Padding is optional; both the URL-safe
    and standard base64 alphabets are accepted, because a key pasted through
    three tools tends to arrive in whichever one they preferred.
    """
    keys: list[MasterKey] = []
    seen: set[str] = set()
    for chunk in raw.split(","):
        entry = chunk.strip()
        if not entry:
            continue
        kid, _, material = entry.partition(":")
        kid = kid.strip()
        if not material:
            raise SecretError("each master key must be '<kid>:<base64 32 bytes>'")
        if kid in seen:
            raise SecretError(f"duplicate master key id {kid!r}")
        seen.add(kid)
        keys.append(MasterKey(kid=kid, key=_decode_key(material.strip(), kid)))
    if not keys:
        raise SecretError("no master keys configured")
    return tuple(keys)


def _decode_key(material: str, kid: str) -> bytes:
    padded = material + "=" * (-len(material) % 4)
    for decode in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decode(padded)
        except (binascii.Error, ValueError):
            continue
    raise SecretError(f"master key {kid!r} is not valid base64")


class SecretBox:
    """Seals and opens tenant secrets. One instance per process."""

    __slots__ = ("_active", "_by_kid")

    def __init__(self, keys: Sequence[MasterKey]) -> None:
        if not keys:
            raise SecretError("SecretBox needs at least one master key")
        self._active = keys[0]
        self._by_kid: dict[str, MasterKey] = {key.kid: key for key in keys}

    @classmethod
    def from_env_value(cls, raw: str) -> SecretBox:
        return cls(parse_master_keys(raw))

    @property
    def active_kid(self) -> str:
        return self._active.kid

    @property
    def kids(self) -> tuple[str, ...]:
        return tuple(self._by_kid)

    def seal(self, plaintext: str, *, tenant_id: uuid.UUID, purpose: str) -> bytes:
        """Encrypt under the active master key."""
        if not plaintext:
            raise SecretError("refusing to seal an empty secret")
        dek = os.urandom(KEY_BYTES)
        aad = _aad(self._active.kid, tenant_id, purpose)
        wrap_nonce = os.urandom(_NONCE_BYTES)
        body_nonce = os.urandom(_NONCE_BYTES)
        wrapped = AESGCM(self._active.key).encrypt(wrap_nonce, dek, aad)
        body = AESGCM(dek).encrypt(body_nonce, plaintext.encode("utf-8"), aad)
        kid = self._active.kid.encode("ascii")
        return b"".join(
            (_MAGIC, bytes([len(kid)]), kid, wrap_nonce, wrapped, body_nonce, body)
        )

    def open(self, blob: bytes | None, *, tenant_id: uuid.UUID, purpose: str) -> str:
        """Decrypt, verifying the blob really belongs to this tenant and use."""
        kid, wrap_nonce, wrapped, body_nonce, body = _split(blob)
        master = self._by_kid.get(kid)
        if master is None:
            raise SecretError(
                f"master key {kid!r} is not loaded; add it to CTB_MASTER_KEYS"
            )
        aad = _aad(kid, tenant_id, purpose)
        try:
            dek = AESGCM(master.key).decrypt(wrap_nonce, wrapped, aad)
            return AESGCM(dek).decrypt(body_nonce, body, aad).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise SecretError("sealed secret failed authentication") from exc

    def needs_rewrap(self, kid: str | None) -> bool:
        return kid != self._active.kid

    def self_check(self) -> None:
        """Seal and open a canary. Called at boot: fail now, not on first use."""
        probe = uuid.UUID(int=0)
        secret = "ctb-master-key-self-check"
        for kid in self._by_kid:
            box = SecretBox([self._by_kid[kid]])
            blob = box.seal(secret, tenant_id=probe, purpose="self-check")
            if box.open(blob, tenant_id=probe, purpose="self-check") != secret:
                raise SecretError(f"master key {kid!r} failed its round trip")

    @staticmethod
    def fingerprint(plaintext: str) -> str:
        """A stable, non-reversible handle for "is this the same key?".

        Stored alongside the ciphertext so key rotation can invalidate a cached
        client, and ``/key`` can answer "you already have this one", without
        anything ever decrypting.
        """
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()[:16]

    def __repr__(self) -> str:
        return f"SecretBox(active={self._active.kid!r}, loaded={len(self._by_kid)})"


def _aad(kid: str, tenant_id: uuid.UUID, purpose: str) -> bytes:
    """Bind the ciphertext to its key, its tenant, and what it is for."""
    return b"|".join((_MAGIC, kid.encode("ascii"), tenant_id.bytes, purpose.encode()))


def _split(blob: bytes | None) -> tuple[str, bytes, bytes, bytes, bytes]:
    if not blob:
        raise SecretError("no sealed secret to open")
    if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + 1:
        raise SecretError("not a ctb sealed secret")
    kid_len = blob[len(_MAGIC)]
    start = len(_MAGIC) + 1
    end = start + kid_len
    minimum = end + _NONCE_BYTES + _WRAPPED_BYTES + _NONCE_BYTES
    if kid_len == 0 or len(blob) <= minimum:
        raise SecretError("sealed secret is truncated")
    try:
        kid = blob[start:end].decode("ascii")
    except UnicodeDecodeError as exc:
        raise SecretError("sealed secret has a non-ASCII key id") from exc
    wrap_nonce = blob[end : end + _NONCE_BYTES]
    wrapped = blob[end + _NONCE_BYTES : end + _NONCE_BYTES + _WRAPPED_BYTES]
    rest = blob[end + _NONCE_BYTES + _WRAPPED_BYTES :]
    return kid, wrap_nonce, wrapped, rest[:_NONCE_BYTES], rest[_NONCE_BYTES:]


def generate_master_key(kid: str) -> str:
    """``<kid>:<base64url>`` for a fresh key, ready to paste into the env."""
    material = os.urandom(KEY_BYTES)
    MasterKey(kid=kid, key=material)  # validates the id before we print it
    return f"{kid}:{base64.urlsafe_b64encode(material).decode('ascii')}"
