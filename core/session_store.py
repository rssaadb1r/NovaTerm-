"""SQLAlchemy ORM models and CRUD helpers for NovaTerm.

This module owns the SQLite schema and exposes a :class:`SessionStore`
facade for application code to use without touching the SQLAlchemy session
directly.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session as ORMSession,
    mapped_column,
    relationship,
    sessionmaker,
)

from . import paths


# ---------------------------------------------------------------------------
# ORM Base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Declarative base class for all NovaTerm ORM models."""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Folder(Base):
    """A folder used to group sessions in the sidebar tree."""

    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="SET NULL"), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    children: Mapped[list[Folder]] = relationship(
        "Folder",
        backref="parent",
        remote_side="Folder.id",
        cascade="all",
        single_parent=True,
    )


class BastionProfile(Base):
    """A reusable jump-host / bastion definition."""

    __tablename__ = "bastion_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=22, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(16), default="password", nullable=False)
    encrypted_credential: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    key_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class Session(Base):
    """A saved connection profile."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="SET NULL"), nullable=True
    )

    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=22, nullable=False)
    protocol: Mapped[str] = mapped_column(String(16), default="ssh", nullable=False)

    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_type: Mapped[str] = mapped_column(String(16), default="ask", nullable=False)
    encrypted_credential: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    key_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    encrypted_key_passphrase: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    jump_host_chain: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)

    color_tag: Mapped[str | None] = mapped_column(String(32), nullable=True)
    group_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)

    log_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    log_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    log_mode: Mapped[str] = mapped_column(String(16), default="append", nullable=False)

    color_scheme: Mapped[str | None] = mapped_column(String(64), nullable=True)
    font_family: Mapped[str | None] = mapped_column(String(128), nullable=True)
    font_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CommandGroup(Base):
    """A folder for commands in the Command Manager."""

    __tablename__ = "command_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Command(Base):
    """A reusable command snippet."""

    __tablename__ = "commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_groups.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    command_text: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    hotkey: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RecentConnection(Base):
    """Recent Quick-Connect entries (last 10 are kept)."""

    __tablename__ = "recent_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    connected_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class MasterAuth(Base):
    """Single-row table holding the master-password bcrypt hash + salt.

    See ``CLAUDE.md §6 — Security Model`` for details. This table is internal
    bootstrapping for :mod:`core.credential_vault` and not part of the public
    spec schema.
    """

    __tablename__ = "master_auth"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bcrypt_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kdf_salt: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# DTOs (used for import / export)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SessionExport:
    """Plain-data representation of a :class:`Session` used for JSON I/O."""

    name: str
    hostname: str
    port: int
    protocol: str
    username: str | None
    auth_type: str
    folder_path: list[str]
    jump_host_chain: list[str]
    color_tag: str | None
    group_tag: str | None
    notes: str | None
    color_scheme: str | None
    font_family: str | None
    font_size: int | None


# ---------------------------------------------------------------------------
# Store facade
# ---------------------------------------------------------------------------


class SessionStore:
    """High-level CRUD facade over the SQLite database.

    Always create exactly one :class:`SessionStore` per application; pass it
    around explicitly rather than reaching for a global.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        """Initialise the store.

        :param db_path: Optional override for the database location. ``None``
            uses :func:`core.paths.database_path`. Pass ``":memory:"`` for an
            in-memory test database.
        """
        if db_path is None:
            db_path = paths.database_path()

        url = (
            "sqlite:///:memory:"
            if str(db_path) == ":memory:"
            else f"sqlite:///{db_path}"
        )
        self._engine = create_engine(url, future=True)
        Base.metadata.create_all(self._engine)
        self._Session = sessionmaker(bind=self._engine, expire_on_commit=False, future=True)

    # -- session lifecycle -------------------------------------------------

    @contextmanager
    def session(self) -> Iterator[ORMSession]:
        """Yield a SQLAlchemy session inside a try/commit/rollback scope."""
        sess = self._Session()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    # -- folder CRUD -------------------------------------------------------

    def create_folder(self, name: str, parent_id: int | None = None) -> int:
        """Create a folder and return its primary key."""
        with self.session() as s:
            f = Folder(name=name, parent_id=parent_id)
            s.add(f)
            s.flush()
            return f.id

    def list_folders(self) -> list[Folder]:
        """Return every folder ordered by ``sort_order`` then name."""
        with self.session() as s:
            return list(s.scalars(select(Folder).order_by(Folder.sort_order, Folder.name)))

    # -- session CRUD ------------------------------------------------------

    def create_session(self, **fields: Any) -> int:
        """Create a session row from kwargs and return its primary key."""
        with self.session() as s:
            sess_row = Session(**fields)
            s.add(sess_row)
            s.flush()
            return sess_row.id

    def update_session(self, session_id: int, **fields: Any) -> None:
        """Update fields on an existing session row."""
        with self.session() as s:
            row = s.get(Session, session_id)
            if row is None:
                raise KeyError(f"Session id={session_id} not found")
            for k, v in fields.items():
                setattr(row, k, v)

    def delete_session(self, session_id: int) -> None:
        """Delete a session row by primary key (no-op if missing)."""
        with self.session() as s:
            row = s.get(Session, session_id)
            if row is not None:
                s.delete(row)

    def get_session(self, session_id: int) -> Session | None:
        """Return a session row or ``None`` if not found."""
        with self.session() as s:
            return s.get(Session, session_id)

    def list_sessions(self) -> list[Session]:
        """Return every session ordered by name."""
        with self.session() as s:
            return list(s.scalars(select(Session).order_by(Session.name)))

    def search_sessions(self, query: str) -> list[Session]:
        """Search sessions by name, hostname or group tag (case-insensitive).

        :param query: Free-form text typed into the sidebar filter bar.
        """
        q = f"%{query.lower()}%"
        with self.session() as s:
            stmt = select(Session).where(
                func.lower(Session.name).like(q)
                | func.lower(Session.hostname).like(q)
                | func.lower(Session.group_tag).like(q)
            )
            return list(s.scalars(stmt))

    def touch_last_connected(self, session_id: int) -> None:
        """Set ``last_connected_at`` on a session to *now*."""
        with self.session() as s:
            row = s.get(Session, session_id)
            if row is not None:
                row.last_connected_at = datetime.utcnow()

    # -- bastion profiles --------------------------------------------------

    def create_bastion(self, **fields: Any) -> int:
        """Create a :class:`BastionProfile` and return its id."""
        with self.session() as s:
            b = BastionProfile(**fields)
            s.add(b)
            s.flush()
            return b.id

    def list_bastions(self) -> list[BastionProfile]:
        """Return every bastion profile ordered by name."""
        with self.session() as s:
            return list(s.scalars(select(BastionProfile).order_by(BastionProfile.name)))

    def get_bastion(self, bastion_id: int) -> BastionProfile | None:
        """Return a bastion row or ``None``."""
        with self.session() as s:
            return s.get(BastionProfile, bastion_id)

    # -- recent connections ------------------------------------------------

    def add_recent_connection(
        self, hostname: str, port: int, protocol: str, username: str | None
    ) -> None:
        """Append a new recent-connection row, trimming to the last 10."""
        with self.session() as s:
            s.add(
                RecentConnection(
                    hostname=hostname, port=port, protocol=protocol, username=username
                )
            )
            s.flush()
            rows = list(
                s.scalars(select(RecentConnection).order_by(RecentConnection.connected_at.desc()))
            )
            for stale in rows[10:]:
                s.delete(stale)

    def list_recent_connections(self) -> list[RecentConnection]:
        """Return up to 10 recent connections, newest first."""
        with self.session() as s:
            return list(
                s.scalars(
                    select(RecentConnection)
                    .order_by(RecentConnection.connected_at.desc())
                    .limit(10)
                )
            )

    # -- master auth row ---------------------------------------------------

    def get_master_auth(self) -> MasterAuth | None:
        """Return the single master-auth row or ``None`` on first launch."""
        with self.session() as s:
            return s.scalars(select(MasterAuth).limit(1)).first()

    def set_master_auth(self, bcrypt_hash: bytes, kdf_salt: bytes) -> None:
        """Replace the master-auth row (used by the credential vault)."""
        with self.session() as s:
            existing = s.scalars(select(MasterAuth).limit(1)).first()
            if existing is not None:
                s.delete(existing)
                s.flush()
            s.add(MasterAuth(bcrypt_hash=bcrypt_hash, kdf_salt=kdf_salt))

    # -- import / export ---------------------------------------------------

    def export_sessions_json(self) -> str:
        """Return all sessions as a JSON document (no encrypted credentials)."""
        with self.session() as s:
            rows = list(s.scalars(select(Session)))
            data = []
            for r in rows:
                data.append(
                    {
                        "name": r.name,
                        "hostname": r.hostname,
                        "port": r.port,
                        "protocol": r.protocol,
                        "username": r.username,
                        "auth_type": r.auth_type,
                        "color_tag": r.color_tag,
                        "group_tag": r.group_tag,
                        "notes": r.notes,
                        "color_scheme": r.color_scheme,
                        "font_family": r.font_family,
                        "font_size": r.font_size,
                        "jump_host_chain": list(r.jump_host_chain or []),
                    }
                )
            return json.dumps(data, indent=2)

    def import_sessions_json(self, payload: str) -> int:
        """Import sessions from a JSON string. Returns number of rows added."""
        data = json.loads(payload)
        if not isinstance(data, list):
            raise ValueError("Expected a JSON list of session objects")
        added = 0
        with self.session() as s:
            for entry in data:
                s.add(
                    Session(
                        name=entry["name"],
                        hostname=entry["hostname"],
                        port=int(entry.get("port", 22)),
                        protocol=entry.get("protocol", "ssh"),
                        username=entry.get("username"),
                        auth_type=entry.get("auth_type", "ask"),
                        color_tag=entry.get("color_tag"),
                        group_tag=entry.get("group_tag"),
                        notes=entry.get("notes"),
                        color_scheme=entry.get("color_scheme"),
                        font_family=entry.get("font_family"),
                        font_size=entry.get("font_size"),
                        jump_host_chain=list(entry.get("jump_host_chain") or []),
                    )
                )
                added += 1
        return added
