"""Unit tests for ``core.session_store``."""
from __future__ import annotations

import pytest

from core.session_store import SessionStore


def test_create_and_fetch_session(store: SessionStore) -> None:
    sid = store.create_session(name="prod-1", hostname="10.0.0.1", port=22, protocol="ssh")
    sess = store.get_session(sid)
    assert sess is not None
    assert sess.name == "prod-1"
    assert sess.hostname == "10.0.0.1"
    assert sess.port == 22


def test_search_sessions(store: SessionStore) -> None:
    store.create_session(name="prod-web", hostname="web.prod.example", protocol="ssh", group_tag="Production")
    store.create_session(name="staging-db", hostname="db.staging.example", protocol="ssh", group_tag="Staging")
    hits = [s.name for s in store.search_sessions("staging")]
    assert hits == ["staging-db"]


def test_update_and_delete(store: SessionStore) -> None:
    sid = store.create_session(name="x", hostname="x", port=22)
    store.update_session(sid, hostname="y")
    assert store.get_session(sid).hostname == "y"
    store.delete_session(sid)
    assert store.get_session(sid) is None


def test_recent_connection_trim(store: SessionStore) -> None:
    for i in range(15):
        store.add_recent_connection(f"host-{i}", 22, "ssh", "u")
    rows = store.list_recent_connections()
    assert len(rows) == 10  # only last 10 retained


def test_export_import_roundtrip(store: SessionStore) -> None:
    store.create_session(name="a", hostname="ha", port=22, color_tag="red", group_tag="Production")
    store.create_session(name="b", hostname="hb", port=22, color_tag="green")
    payload = store.export_sessions_json()

    target = SessionStore(":memory:")
    n = target.import_sessions_json(payload)
    assert n == 2
    names = sorted(s.name for s in target.list_sessions())
    assert names == ["a", "b"]


def test_bastion_profile_crud(store: SessionStore) -> None:
    bid = store.create_bastion(name="edge", hostname="edge", port=22, username="u", auth_type="key")
    assert store.get_bastion(bid).hostname == "edge"
    assert [b.name for b in store.list_bastions()] == ["edge"]


def test_folder_self_ref_relationship(store: SessionStore) -> None:
    """``Folder.children`` must navigate to children, not parents."""
    from core.session_store import Folder

    parent_id = store.create_folder("parent")
    child_id = store.create_folder("child", parent_id=parent_id)
    with store.session() as s:
        p = s.get(Folder, parent_id)
        c = s.get(Folder, child_id)
        assert [f.id for f in p.children] == [child_id]
        assert c.parent is not None
        assert c.parent.id == parent_id


def test_master_auth_replace(store: SessionStore) -> None:
    store.set_master_auth(bcrypt_hash=b"$2b$abc", kdf_salt=b"saltA")
    store.set_master_auth(bcrypt_hash=b"$2b$xyz", kdf_salt=b"saltB")
    row = store.get_master_auth()
    assert row is not None
    assert row.kdf_salt == b"saltB"


def test_default_session_lazy_create_and_update(store: SessionStore) -> None:
    """Reading the default session before it exists creates the singleton."""
    row = store.get_default_session()
    assert row.id == 1
    assert row.username is None
    assert row.port == 22

    store.update_default_session(username="root", port=2222, protocol="ssh")
    row = store.get_default_session()
    assert row.username == "root"
    assert row.port == 2222


def test_folder_expand_state_persists(store: SessionStore) -> None:
    fid = store.create_folder("Production")
    # Default is expanded.
    assert next(f for f in store.list_folders() if f.id == fid).is_expanded is True
    store.set_folder_expanded(fid, False)
    assert next(f for f in store.list_folders() if f.id == fid).is_expanded is False


def test_folder_rename_and_cascade_delete(store: SessionStore) -> None:
    """``delete_folder`` cascades through sub-folders and sessions.

    Tree::

        Old name              (fid)
        ├── srv                       (session inside fid)
        └── child                     (sub-folder of fid)
            ├── srv2                  (session inside child)
            └── grand                 (sub-sub-folder of child)
                └── srv3              (session inside grand)

    Deleting ``fid`` removes every node above plus the
    sibling-untouched control row.
    """
    fid = store.create_folder("Old name")
    child_fid = store.create_folder("child", parent_id=fid)
    grand_fid = store.create_folder("grand", parent_id=child_fid)
    sid = store.create_session(
        name="srv", hostname="h", port=22, protocol="ssh", folder_id=fid
    )
    sid2 = store.create_session(
        name="srv2", hostname="h2", port=22, protocol="ssh", folder_id=child_fid
    )
    sid3 = store.create_session(
        name="srv3", hostname="h3", port=22, protocol="ssh", folder_id=grand_fid
    )
    # Rename round-trips before we destroy the tree.
    store.update_folder(fid, name="New name")
    assert next(f for f in store.list_folders() if f.id == fid).name == "New name"

    # Sibling control: unrelated folder + session must survive.
    other_fid = store.create_folder("Other")
    other_sid = store.create_session(
        name="other-srv",
        hostname="z",
        port=22,
        protocol="ssh",
        folder_id=other_fid,
    )

    store.delete_folder(fid)

    # Every doomed folder and session is gone.
    folder_ids = {f.id for f in store.list_folders()}
    assert fid not in folder_ids
    assert child_fid not in folder_ids
    assert grand_fid not in folder_ids
    assert store.get_session(sid) is None
    assert store.get_session(sid2) is None
    assert store.get_session(sid3) is None

    # Sibling subtree untouched.
    assert other_fid in folder_ids
    assert store.get_session(other_sid) is not None
