"""Encrypted credential vault.

Implements the security model documented in ``CLAUDE.md §6``:

* The master password is hashed with ``bcrypt`` and stored in the database.
* The Fernet symmetric encryption key is **derived** from the master password
  via PBKDF2-HMAC-SHA256 (390 000 iterations) and is never written to disk.
* Per-session credentials and SSH key passphrases are encrypted with this
  derived key before being persisted.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .session_store import SessionStore

PBKDF2_ITERATIONS = 390_000
SALT_BYTES = 16


def _derive_key(master_password: str, salt: bytes) -> bytes:
    """Return a 32-byte Fernet key derived from ``master_password`` + ``salt``."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(master_password.encode("utf-8")))


@dataclass(slots=True)
class VaultExportBundle:
    """Container for an exported credential bundle (Fernet-encrypted)."""

    salt_b64: str
    payload_b64: str

    def to_json(self) -> str:
        """Serialise to a JSON string for transport / file save."""
        return json.dumps({"salt": self.salt_b64, "payload": self.payload_b64})

    @classmethod
    def from_json(cls, raw: str) -> VaultExportBundle:
        """Parse a previously exported JSON string."""
        data = json.loads(raw)
        return cls(salt_b64=data["salt"], payload_b64=data["payload"])


class VaultLocked(RuntimeError):
    """Raised when an encrypt/decrypt is attempted before unlocking the vault."""


class VaultAuthError(RuntimeError):
    """Raised when an invalid master password is supplied."""


class CredentialVault:
    """Master-password protected secret store.

    Typical lifecycle::

        store = SessionStore()
        vault = CredentialVault(store)
        if not vault.is_initialized():
            vault.initialize("hunter2")
        else:
            vault.unlock("hunter2")

        token = vault.encrypt("s3cret")
        plain = vault.decrypt(token)
    """

    def __init__(self, store: SessionStore) -> None:
        """Bind to a :class:`SessionStore` for persistence."""
        self._store = store
        self._fernet: Fernet | None = None

    # -- state ---------------------------------------------------------------

    def is_initialized(self) -> bool:
        """Return ``True`` once a master password has been set."""
        return self._store.get_master_auth() is not None

    def is_unlocked(self) -> bool:
        """Return ``True`` when the in-memory Fernet key is available."""
        return self._fernet is not None

    # -- bootstrap -----------------------------------------------------------

    def initialize(self, master_password: str) -> None:
        """Create the master-auth row on first launch and unlock the vault."""
        if self.is_initialized():
            raise RuntimeError("Vault is already initialized")
        salt = os.urandom(SALT_BYTES)
        bcrypt_hash = bcrypt.hashpw(master_password.encode("utf-8"), bcrypt.gensalt())
        self._store.set_master_auth(bcrypt_hash=bcrypt_hash, kdf_salt=salt)
        self._fernet = Fernet(_derive_key(master_password, salt))

    def unlock(self, master_password: str) -> None:
        """Verify ``master_password`` and load the in-memory Fernet key."""
        row = self._store.get_master_auth()
        if row is None:
            raise RuntimeError("Vault has not been initialized")
        if not bcrypt.checkpw(master_password.encode("utf-8"), row.bcrypt_hash):
            raise VaultAuthError("Invalid master password")
        self._fernet = Fernet(_derive_key(master_password, row.kdf_salt))

    def lock(self) -> None:
        """Drop the in-memory Fernet key. Subsequent encrypt/decrypt fail."""
        self._fernet = None

    def change_password(self, old_password: str, new_password: str) -> None:
        """Re-key the vault with a new master password.

        This atomically re-encrypts every Fernet ciphertext currently stored
        in the database (per-session credentials, SSH key passphrases, and
        bastion-profile credentials) with the freshly-derived key, then
        swaps in the new master-auth row. If any decrypt fails (e.g. a
        corrupted token) the in-memory key is left untouched and the
        database is not modified, so the caller can retry safely.
        """
        from sqlalchemy import select as _select

        from .session_store import (
            BastionProfile,
            DefaultSession,
            MasterAuth,
            Session as SessionRow,
        )

        self.unlock(old_password)
        old_fernet = self._fernet
        assert old_fernet is not None  # unlock() set it

        salt = os.urandom(SALT_BYTES)
        new_fernet = Fernet(_derive_key(new_password, salt))

        # Re-encrypt every Fernet ciphertext and rotate the master-auth row
        # inside a single transaction so a crash mid-way leaves the DB
        # consistent (rollback restores the old hash + ciphertexts together
        # with the rows we touched).
        with self._store.session() as s:
            sessions = list(s.scalars(_select(SessionRow)))
            bastions = list(s.scalars(_select(BastionProfile)))

            # Decrypt everything *first* so a failure surfaces before any
            # writes; SQLAlchemy will roll back the open transaction.
            decoded: list[tuple[object, str, bytes]] = []
            for row in sessions:
                if row.encrypted_credential:
                    decoded.append(
                        (row, "encrypted_credential",
                         old_fernet.decrypt(row.encrypted_credential))
                    )
                if row.encrypted_key_passphrase:
                    decoded.append(
                        (row, "encrypted_key_passphrase",
                         old_fernet.decrypt(row.encrypted_key_passphrase))
                    )
            for row in bastions:
                if row.encrypted_credential:
                    decoded.append(
                        (row, "encrypted_credential",
                         old_fernet.decrypt(row.encrypted_credential))
                    )

            # The Default Session row holds two Fernet ciphertexts that go
            # through the same vault (see ui/default_session_dialog.py and the
            # Quick Host Bar in ui/main_window.py). Without re-encrypting them
            # alongside the per-session and bastion ciphertexts, a master-
            # password rotation would leave the Default Session unable to
            # decrypt and silently fall back to ``password = None``.
            default_row = s.get(DefaultSession, 1)
            if default_row is not None:
                if default_row.encrypted_password:
                    decoded.append(
                        (default_row, "encrypted_password",
                         old_fernet.decrypt(default_row.encrypted_password))
                    )
                if default_row.encrypted_key_passphrase:
                    decoded.append(
                        (default_row, "encrypted_key_passphrase",
                         old_fernet.decrypt(default_row.encrypted_key_passphrase))
                    )

            for row, attr, plaintext in decoded:
                setattr(row, attr, new_fernet.encrypt(plaintext))

            existing = s.scalars(_select(MasterAuth).limit(1)).first()
            if existing is not None:
                s.delete(existing)
                s.flush()
            bcrypt_hash = bcrypt.hashpw(
                new_password.encode("utf-8"), bcrypt.gensalt()
            )
            s.add(MasterAuth(bcrypt_hash=bcrypt_hash, kdf_salt=salt))

        self._fernet = new_fernet

    # -- crypto --------------------------------------------------------------

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt ``plaintext`` and return the raw Fernet token."""
        if self._fernet is None:
            raise VaultLocked("Vault must be unlocked before encrypting")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        """Decrypt a Fernet token and return UTF-8 text."""
        if self._fernet is None:
            raise VaultLocked("Vault must be unlocked before decrypting")
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            raise VaultAuthError("Could not decrypt: invalid token / wrong key") from exc

    # -- export / import -----------------------------------------------------

    def export_bundle(self, payload: dict[str, str], passphrase: str) -> VaultExportBundle:
        """Encrypt a dict of secrets with a one-off passphrase.

        :param payload: ``{"name": "secret"}`` mapping to encrypt.
        :param passphrase: Standalone passphrase, *not* the master password.
        """
        salt = os.urandom(SALT_BYTES)
        f = Fernet(_derive_key(passphrase, salt))
        token = f.encrypt(json.dumps(payload).encode("utf-8"))
        return VaultExportBundle(
            salt_b64=base64.urlsafe_b64encode(salt).decode("ascii"),
            payload_b64=base64.urlsafe_b64encode(token).decode("ascii"),
        )

    def import_bundle(self, bundle: VaultExportBundle, passphrase: str) -> dict[str, str]:
        """Decrypt a previously exported bundle."""
        salt = base64.urlsafe_b64decode(bundle.salt_b64)
        token = base64.urlsafe_b64decode(bundle.payload_b64)
        f = Fernet(_derive_key(passphrase, salt))
        try:
            data = f.decrypt(token)
        except InvalidToken as exc:
            raise VaultAuthError("Could not decrypt bundle: wrong passphrase") from exc
        result = json.loads(data.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("Bundle payload is not a JSON object")
        return result
