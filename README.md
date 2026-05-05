# NovaTerm

A SecureCRT-inspired SSH / Telnet terminal client for Linux, built with
Python 3.12 + PyQt6.

See [CLAUDE.md](./CLAUDE.md) for the full architectural reference.

## Features (Phase 1 MVP)

- Session manager with folders, color tags and search
- Multi-tab + split-view + tab detach
- Encrypted credential vault (Fernet + bcrypt master password)
- Right-click terminal context menu (copy, paste, broadcast, find, clear)
- Command Manager dock with fuzzy search and `%HOST%`/`%USER%`/… variables
- Jump host / bastion chains (up to 3 hops, reusable Bastion Profiles)
- Cluster Send (broadcast keystrokes to selected sessions)
- Tab color coding & group presets
- In-terminal Find & Highlight (regex + match count)
- Two-pane SFTP file manager with concurrent transfer queue
- Quick Connect (`Ctrl+Q`) + Recent Connections
- Per-session file logging with variable substitution
- 6 built-in color schemes (Dark / Light / Solarized Dark / Dracula / Nord / Monokai)
- Settings dialog (General, Appearance, Terminal, Keyboard, Groups, SFTP, Security, Proxy, Advanced)

## Install (development)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python main.py
```

## Run tests

```bash
pytest
```

## Linux launcher

Copy `novaterm.desktop` into `~/.local/share/applications/` to register the
app with your desktop environment.
