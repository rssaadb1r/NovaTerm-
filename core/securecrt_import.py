"""Import sessions and Button-Bar commands from a SecureCRT *Config* tree.

SecureCRT (VanDyke) stores its configuration as a tree of ``.ini`` files
on disk. Each saved session lives at::

    <Config>/Sessions/<optional sub-folders>/<Session Name>.ini

with key-value lines in a custom format::

    S:"Hostname"=server.example.com
    D:"[SSH2] Port"=00000016
    S:"Username"=admin
    S:"Protocol Name"=SSH2

Button-bar commands live in a separate file (``Button Bar.ini`` or
``ButtonBar.ini``) inside ``<Config>``, also using the
``S:"…"=…`` / ``D:"…"=…`` syntax.

This module is *only* responsible for parsing those files and turning
them into typed :class:`ImportedSession` / :class:`ImportedCommand`
records. The UI layer (``ui/securecrt_import_dialog.py``) drives the
actual SQLite writes and de-duplication so the import dialog can show a
summary before committing.

Per the spec, **passwords are not imported** — SecureCRT encrypts them
with a non-portable scheme. Sessions land with ``auth_type="ask"`` so
NovaTerm prompts on first connect.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# SecureCRT INI lines look like:
#   S:"Hostname"=server.example.com
#   D:"[SSH2] Port"=00000016
#   B:"Use SSH2"=00000001
#
# Type prefix is ``S`` (string), ``D`` (decimal int — *hex-encoded as
# 8-digit ASCII*), or ``B`` (boolean). For our purposes only string and
# decimal matter; booleans we simply ignore.
_KV_SEP = '"='


def _parse_ini(path: Path) -> dict[str, str | int]:
    """Return a flat ``key -> value`` mapping for a SecureCRT INI file.

    Values are coerced based on the type prefix: ``D:`` is parsed as a
    base-16 integer (SecureCRT pads ports as ``00000016`` for decimal
    22 — yes, the digits are hex). Everything else is left as a string.
    Unparseable / unknown-type lines are skipped silently so a single
    odd row in one session doesn't abort the whole import.
    """
    out: dict[str, str | int] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < 4 or line[1] != ':' or line[2] != '"':
            continue
        kind = line[0]
        sep = line.find(_KV_SEP, 3)
        if sep < 0:
            continue
        key = line[3:sep]
        value = line[sep + len(_KV_SEP):]
        if kind == "S":
            out[key] = value
        elif kind == "D":
            try:
                # SecureCRT writes decimal numbers as 8-digit hex.
                out[key] = int(value, 16)
            except ValueError:
                continue
        # 'B' (boolean) and unknown kinds: ignored.
    return out


@dataclass(slots=True)
class ImportedSession:
    """Normalised session record ready to insert into NovaTerm's DB."""

    name: str
    hostname: str
    port: int
    protocol: str  # "ssh" / "telnet"
    username: str | None
    folder_path: tuple[str, ...] = ()


@dataclass(slots=True)
class ImportedCommand:
    """Normalised button-bar command record ready to insert."""

    name: str
    command_text: str


@dataclass(slots=True)
class ImportResult:
    """Aggregated parse result across the whole *Config* directory."""

    sessions: list[ImportedSession] = field(default_factory=list)
    commands: list[ImportedCommand] = field(default_factory=list)
    skipped_files: list[Path] = field(default_factory=list)


def _normalise_protocol(name: str | None) -> str:
    """Map SecureCRT's *Protocol Name* string to NovaTerm's enum.

    SecureCRT uses ``SSH2``, ``SSH1``, ``Telnet``, ``Serial``, ``RAW``
    and a few others. We only support ``ssh`` / ``telnet`` so anything
    SSH-flavoured collapses to ``ssh`` and Telnet to ``telnet``;
    everything else (Serial, RAW, …) defaults to ``ssh`` so the imported
    row is at least valid and the user can edit it afterwards.
    """
    if not name:
        return "ssh"
    n = name.strip().lower()
    if "telnet" in n:
        return "telnet"
    return "ssh"


def _default_port(protocol: str) -> int:
    """Standard well-known port for ``protocol`` when SecureCRT didn't set one."""
    return 23 if protocol == "telnet" else 22


def _parse_session_file(
    path: Path,
    folder_path: tuple[str, ...],
) -> ImportedSession | None:
    """Parse one ``<name>.ini`` into an :class:`ImportedSession`."""
    data = _parse_ini(path)
    if not data:
        return None
    hostname_raw = data.get("Hostname")
    hostname = (
        hostname_raw.strip() if isinstance(hostname_raw, str) else ""
    )
    if not hostname:
        return None  # nothing to connect to → skip
    protocol = _normalise_protocol(
        data.get("Protocol Name") if isinstance(data.get("Protocol Name"), str) else None
    )
    # SecureCRT keys ports per protocol: "[SSH2] Port", "[SSH1] Port",
    # "[Telnet] Port" etc. Try the protocol-specific key first, then a
    # generic "Port" fallback, then the well-known port.
    port: int | None = None
    if protocol == "telnet":
        candidates = ("[Telnet] Port", "Port")
    else:
        candidates = ("[SSH2] Port", "[SSH1] Port", "Port")
    for key in candidates:
        val = data.get(key)
        if isinstance(val, int) and val > 0:
            port = val
            break
    if port is None:
        port = _default_port(protocol)
    username_raw = data.get("Username")
    username = (
        username_raw.strip() if isinstance(username_raw, str) and username_raw.strip() else None
    )
    return ImportedSession(
        name=path.stem,
        hostname=hostname,
        port=port,
        protocol=protocol,
        username=username,
        folder_path=folder_path,
    )


def _walk_sessions(sessions_dir: Path) -> list[ImportedSession]:
    """Recursively parse every ``.ini`` under ``<Config>/Sessions/``."""
    out: list[ImportedSession] = []
    if not sessions_dir.is_dir():
        return out
    for ini in sessions_dir.rglob("*.ini"):
        # SecureCRT writes ``__FolderData__.ini`` markers per folder —
        # they have no Hostname so they'd be skipped anyway, but skip
        # them up-front to keep the result list tidy.
        if ini.name.startswith("__"):
            continue
        relative = ini.parent.relative_to(sessions_dir)
        folder_path = tuple(p for p in relative.parts if p) if relative.parts else ()
        session = _parse_session_file(ini, folder_path)
        if session is not None:
            out.append(session)
    return out


def _parse_button_bar(button_bar_path: Path) -> list[ImportedCommand]:
    """Extract ``(name, command_text)`` pairs from a Button-Bar INI file.

    SecureCRT writes button entries as numbered ``Button N - Action``
    plus a ``Button N - Caption`` (display text) and ``Button N - Arg``
    (the literal command sent on click). The exact key spelling has
    varied across versions, so we detect entries in a tolerant way:
    any key matching ``Button <n> - Caption`` is treated as a button
    name; the corresponding command text is read from the matching
    ``Button <n> - Arg`` (or ``... Argument``) entry, falling back to
    ``Caption`` if no Arg is present.
    """
    data = _parse_ini(button_bar_path)
    if not data:
        return []
    captions: dict[int, str] = {}
    args: dict[int, str] = {}
    for key, value in data.items():
        if not key.lower().startswith("button "):
            continue
        # "Button 03 - Caption" -> idx=3, suffix='Caption'
        try:
            head, suffix = key.split("-", 1)
        except ValueError:
            continue
        try:
            idx = int(head.strip().split()[1])
        except (IndexError, ValueError):
            continue
        suffix_lower = suffix.strip().lower()
        if not isinstance(value, str) or not value:
            continue
        if suffix_lower == "caption":
            captions[idx] = value
        elif suffix_lower in ("arg", "argument", "command"):
            args[idx] = value
    out: list[ImportedCommand] = []
    for idx in sorted(captions):
        name = captions[idx].strip()
        cmd = args.get(idx, name).strip()
        if not name or not cmd:
            continue
        out.append(ImportedCommand(name=name, command_text=cmd))
    return out


def _find_button_bar(config_dir: Path) -> Path | None:
    """Locate the button-bar INI inside ``config_dir``.

    SecureCRT has historically used both ``Button Bar.ini`` and
    ``ButtonBar.ini``; some custom layouts also nest it under
    ``Buttons/Default.ini``. We try the common spellings in order.
    """
    candidates = [
        config_dir / "Button Bar.ini",
        config_dir / "ButtonBar.ini",
        config_dir / "Buttons" / "Default.ini",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def parse_config(config_dir: Path) -> ImportResult:
    """Parse a SecureCRT *Config* tree and return an :class:`ImportResult`.

    ``config_dir`` should point at the directory containing
    ``Sessions/`` (e.g. on Windows
    ``C:\\Users\\<u>\\AppData\\Roaming\\VanDyke\\Config``; on Linux
    ``~/.vandyke/SecureCRT/Config``). Missing sub-paths are tolerated —
    we simply return an empty list for whichever side is absent.
    """
    config_dir = Path(config_dir)
    sessions = _walk_sessions(config_dir / "Sessions")
    button_bar = _find_button_bar(config_dir)
    commands = _parse_button_bar(button_bar) if button_bar is not None else []
    return ImportResult(sessions=sessions, commands=commands)
