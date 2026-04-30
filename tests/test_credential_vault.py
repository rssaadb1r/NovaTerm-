"""Unit tests for ``core.credential_vault``."""
from __future__ import annotations

import pytest

from core.credential_vault import (
    CredentialVault,
    VaultAuthError,
    VaultExportBundle,
    VaultLocked,
)
from core.session_store import SessionStore


@pytest.fixture
def vault(store: SessionStore) -> CredentialVault:
    return CredentialVault(store)


def test_initialize_then_encrypt_decrypt(vault: CredentialVault) -> None:
    vault.initialize("hunter2")
    token = vault.encrypt("super-secret")
    assert isinstance(token, bytes)
    assert vault.decrypt(token) == "super-secret"


def test_encrypt_requires_unlock(vault: CredentialVault) -> None:
    with pytest.raises(VaultLocked):
        vault.encrypt("hi")


def test_unlock_with_wrong_password(vault: CredentialVault) -> None:
    vault.initialize("p1")
    vault.lock()
    with pytest.raises(VaultAuthError):
        vault.unlock("wrong")


def test_unlock_with_correct_password(vault: CredentialVault) -> None:
    vault.initialize("p1")
    token = vault.encrypt("data")
    vault.lock()
    vault.unlock("p1")
    assert vault.decrypt(token) == "data"


def test_change_password(vault: CredentialVault) -> None:
    vault.initialize("old")
    vault.change_password("old", "new")
    vault.lock()
    vault.unlock("new")
    # token encrypted with new key
    token = vault.encrypt("d")
    assert vault.decrypt(token) == "d"


def test_change_password_preserves_existing_ciphertexts(
    vault: CredentialVault, store: SessionStore
) -> None:
    """``change_password`` must transparently re-encrypt every stored token.

    Tokens encrypted with the old master password should still decrypt
    cleanly after a password rotation.
    """
    vault.initialize("old")
    cred_token = vault.encrypt("session-secret")
    passphrase_token = vault.encrypt("ssh-key-passphrase")
    bastion_token = vault.encrypt("bastion-password")

    sid = store.create_session(
        name="prod",
        hostname="example.com",
        port=22,
        protocol="ssh",
        username="root",
        auth_type="password",
        encrypted_credential=cred_token,
        encrypted_key_passphrase=passphrase_token,
    )
    bid = store.create_bastion(
        name="jump", hostname="bastion", port=22, username="ops",
        auth_type="password", encrypted_credential=bastion_token,
    )

    # Default Session credentials must also survive a password rotation.
    default_pw_token = vault.encrypt("default-pw")
    default_pp_token = vault.encrypt("default-passphrase")
    store.update_default_session(
        username="root",
        encrypted_password=default_pw_token,
        encrypted_key_passphrase=default_pp_token,
    )

    vault.change_password("old", "new")

    refreshed = store.get_session(sid)
    assert refreshed is not None
    assert vault.decrypt(refreshed.encrypted_credential) == "session-secret"
    assert (
        vault.decrypt(refreshed.encrypted_key_passphrase)
        == "ssh-key-passphrase"
    )
    refreshed_b = store.get_bastion(bid)
    assert refreshed_b is not None
    assert vault.decrypt(refreshed_b.encrypted_credential) == "bastion-password"

    refreshed_d = store.get_default_session()
    assert refreshed_d.encrypted_password is not None
    assert refreshed_d.encrypted_key_passphrase is not None
    assert vault.decrypt(refreshed_d.encrypted_password) == "default-pw"
    assert (
        vault.decrypt(refreshed_d.encrypted_key_passphrase)
        == "default-passphrase"
    )

    # And we can still unlock with the new password from a cold start.
    vault.lock()
    vault.unlock("new")
    assert vault.decrypt(refreshed.encrypted_credential) == "session-secret"
    assert vault.decrypt(refreshed_d.encrypted_password) == "default-pw"


def test_master_password_full_lifecycle(
    vault: CredentialVault, store: SessionStore
) -> None:
    """Cover the full master-password contract in one place:

    1. Wrong password is rejected.
    2. Correct password unlocks the vault.
    3. After rotation the *old* password no longer works.
    4. After rotation the *new* password unlocks the vault and stored
       credentials still decrypt cleanly.
    """
    vault.initialize("first")
    cred_token = vault.encrypt("payload")
    sid = store.create_session(
        name="prod",
        hostname="example.com",
        port=22,
        protocol="ssh",
        username="root",
        auth_type="password",
        encrypted_credential=cred_token,
    )
    vault.lock()

    # 1. wrong password is rejected
    with pytest.raises(VaultAuthError):
        vault.unlock("not-it")
    assert not vault.is_unlocked()

    # 2. correct password unlocks and persisted credentials decrypt
    vault.unlock("first")
    assert vault.decrypt(store.get_session(sid).encrypted_credential) == "payload"

    # rotate
    vault.change_password("first", "second")
    vault.lock()

    # 3. old password no longer works
    with pytest.raises(VaultAuthError):
        vault.unlock("first")
    assert not vault.is_unlocked()

    # 4. new password unlocks and pre-rotation credential still decrypts
    vault.unlock("second")
    assert vault.decrypt(store.get_session(sid).encrypted_credential) == "payload"


def test_change_password_rejects_wrong_current(vault: CredentialVault) -> None:
    """``change_password`` must reject an incorrect current password and
    leave the vault state untouched (caller can retry).
    """
    vault.initialize("real")
    token = vault.encrypt("data")

    with pytest.raises(VaultAuthError):
        vault.change_password("wrong", "new")

    # Still on the original password.
    vault.lock()
    with pytest.raises(VaultAuthError):
        vault.unlock("new")
    vault.unlock("real")
    assert vault.decrypt(token) == "data"


def test_default_session_password_is_encrypted_in_db(
    vault: CredentialVault, store: SessionStore
) -> None:
    """The Default Session password column must hold ciphertext, never plaintext."""
    vault.initialize("master")
    plaintext = "the-default-pw"
    store.update_default_session(
        encrypted_password=vault.encrypt(plaintext),
        encrypted_key_passphrase=vault.encrypt("the-default-passphrase"),
    )
    row = store.get_default_session()
    assert row.encrypted_password is not None
    assert plaintext.encode("utf-8") not in row.encrypted_password
    assert row.encrypted_password != plaintext.encode("utf-8")
    # And decrypting with the right key returns the original plaintext.
    assert vault.decrypt(row.encrypted_password) == plaintext


def test_default_session_decrypt_with_wrong_key_raises(
    vault: CredentialVault, store: SessionStore
) -> None:
    """Decrypting Default Session ciphertext with a wrong Fernet key fails."""
    from cryptography.fernet import Fernet

    vault.initialize("master")
    store.update_default_session(
        encrypted_password=vault.encrypt("secret"),
    )
    blob = store.get_default_session().encrypted_password

    wrong_vault = CredentialVault(store)
    wrong_vault._fernet = Fernet(Fernet.generate_key())
    with pytest.raises(VaultAuthError):
        wrong_vault.decrypt(blob)


def test_default_session_credentials_after_password_change(
    vault: CredentialVault, store: SessionStore
) -> None:
    """After a master-password rotation the Default Session credential must
    decrypt with the new key, and the *old* key must no longer work.
    """
    from cryptography.fernet import Fernet

    vault.initialize("first")
    pre_rotation_key = vault._fernet  # capture the old Fernet
    plaintext = "default-pw"
    store.update_default_session(encrypted_password=vault.encrypt(plaintext))

    vault.change_password("first", "second")

    refreshed = store.get_default_session()
    # New Fernet decrypts.
    assert vault.decrypt(refreshed.encrypted_password) == plaintext
    # Old Fernet does NOT decrypt the freshly-rotated ciphertext.
    assert pre_rotation_key is not None
    from cryptography.fernet import InvalidToken
    with pytest.raises(InvalidToken):
        pre_rotation_key.decrypt(refreshed.encrypted_password)


def test_migrate_default_session_plaintext_encrypts_in_place(
    vault: CredentialVault, store: SessionStore
) -> None:
    """The startup migration must encrypt any plaintext blob it finds.

    We bypass the encrypted-only public API by writing raw plaintext bytes
    straight to the column, then assert the migration:

    * detects the column is not a Fernet token,
    * encrypts the plaintext and writes the ciphertext back,
    * leaves a column that decrypts to the original plaintext.
    """
    vault.initialize("master")
    # Hand-write a raw plaintext blob (simulates an older build / a hand
    # edited DB that bypassed the dialog encryption path).
    store.update_default_session(encrypted_password=b"plain-text-secret")

    fixed = vault.migrate_default_session_plaintext()
    assert fixed == 1

    row = store.get_default_session()
    assert row.encrypted_password != b"plain-text-secret"
    assert vault.decrypt(row.encrypted_password) == "plain-text-secret"

    # Idempotent: a second pass must be a no-op.
    assert vault.migrate_default_session_plaintext() == 0


def test_export_import_bundle(vault: CredentialVault) -> None:
    vault.initialize("x")
    bundle = vault.export_bundle({"a": "1", "b": "2"}, passphrase="abc")
    raw = bundle.to_json()
    parsed = VaultExportBundle.from_json(raw)
    out = vault.import_bundle(parsed, passphrase="abc")
    assert out == {"a": "1", "b": "2"}


def test_import_bundle_wrong_passphrase(vault: CredentialVault) -> None:
    vault.initialize("x")
    bundle = vault.export_bundle({"a": "1"}, passphrase="abc")
    with pytest.raises(VaultAuthError):
        vault.import_bundle(bundle, passphrase="wrong")
