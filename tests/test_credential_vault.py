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
