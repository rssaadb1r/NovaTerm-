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
