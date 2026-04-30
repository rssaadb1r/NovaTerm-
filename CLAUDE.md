# NovaTerm — Architecture Reference (CLAUDE.md)

NovaTerm is a Python/PyQt6 SSH/Telnet terminal application targeting Linux
Fedora 44, inspired by SecureCRT. This document is the canonical architectural
reference for the project; consult it before making changes.

---

## 1. Tech Stack

| Layer            | Choice                                                    |
| ---------------- | --------------------------------------------------------- |
| Language         | Python 3.12+                                              |
| GUI Framework    | PyQt6                                                     |
| Terminal Widget  | `qtermwidget` if importable, else a `pyte`-based fallback |
| SSH Backend      | Paramiko + `qasync` (asyncio ↔ Qt event-loop bridge)      |
| Telnet           | `telnetlib3` (asyncio-native)                             |
| SFTP             | Paramiko built-in SFTPClient                              |
| Crypto / Vault   | `cryptography` (Fernet) + `bcrypt`                        |
| ORM / Storage    | SQLAlchemy 2.x + SQLite                                   |
| Config           | TOML (stdlib `tomllib` for read, `tomli-w` for write)     |
| Dirs             | `platformdirs`                                            |
| Testing          | `pytest` + `pytest-qt` + `pytest-asyncio`                 |

### Design note: terminal widget
The original spec calls for `QTermWidget`. PyQt6 bindings for the C++
QTermWidget library are not reliably available via `pip` on Fedora 44, so
`ui/terminal_widget.py` first tries to import `qtermwidget` and falls back to
a self-contained `pyte`-based emulator embedded in a `QPlainTextEdit`. This
keeps the API surface stable regardless of which backend is active.

---

## 2. Directory Layout

```
NovaTerm-/
├── CLAUDE.md
├── main.py                    # entry-point; spins up qasync loop + MainWindow
├── pyproject.toml
├── requirements.txt
├── novaterm.desktop           # Linux launcher
├── ui/
│   ├── main_window.py         # QMainWindow + toolbar + menus + status bar
│   ├── session_manager.py     # collapsible left sidebar (tree + filter bar)
│   ├── tab_manager.py         # QTabWidget + color stripes + split-view
│   ├── terminal_widget.py     # terminal + inline find bar
│   ├── cluster_bar.py         # Cluster Send input bar + selection dialog
│   ├── command_manager.py     # bottom dock + fuzzy-search popup
│   ├── sftp_panel.py          # 2-pane SFTP (local | remote) + xfer queue
│   ├── quick_connect.py       # Ctrl+Q popup
│   ├── session_dialog.py      # New/Edit session (incl. jump-host chain)
│   └── settings_dialog.py     # global preferences (9 tabs)
├── core/
│   ├── __init__.py
│   ├── session_store.py       # SQLAlchemy models + CRUD (auth bootstrapping)
│   ├── credential_vault.py    # Fernet encryption + bcrypt master password
│   ├── ssh_client.py          # Paramiko + ProxyJump chaining + qasync
│   ├── telnet_client.py       # asyncio Telnet
│   ├── sftp_client.py         # SFTP ops + concurrent transfer queue
│   ├── command_store.py       # Command + CommandGroup CRUD
│   ├── logger.py              # per-session file logging
│   └── paths.py               # platformdirs wrapper
├── themes/
│   ├── dark.toml
│   ├── light.toml
│   ├── solarized_dark.toml
│   ├── dracula.toml
│   ├── nord.toml
│   └── monokai.toml
├── assets/icons/              # SVG toolbar/sidebar icons
└── tests/
    ├── test_credential_vault.py
    ├── test_session_store.py
    ├── test_command_store.py
    ├── test_ssh_client.py
    └── test_sftp_client.py
```

---

## 3. Module Dependency Graph

```
paths ─┬─► session_store ──┬─► command_store
       │                   │
       └─► credential_vault┴─► ssh_client ─► sftp_client
                                       │
                                       ├─► telnet_client
                                       └─► logger

ui/* depends on core/* (one-way). UI never imports from another UI module's
private internals — they communicate via Qt signals/slots.
```

Build order (must compile/test in this sequence):

1. `core/paths.py`
2. `core/session_store.py`
3. `core/credential_vault.py`
4. `core/command_store.py`, `core/logger.py`
5. `core/ssh_client.py`, `core/telnet_client.py`
6. `core/sftp_client.py`
7. `ui/terminal_widget.py`
8. `ui/session_manager.py`, `ui/tab_manager.py`, `ui/cluster_bar.py`,
   `ui/command_manager.py`, `ui/sftp_panel.py`,
   `ui/quick_connect.py`, `ui/session_dialog.py`, `ui/settings_dialog.py`
9. `ui/main_window.py`
10. `main.py`

---

## 4. SQLAlchemy Models (in `core/session_store.py`)

| Model              | Key fields                                                              |
| ------------------ | ----------------------------------------------------------------------- |
| `Session`          | name, folder_id, hostname, port, protocol, username, auth_type,         |
|                    | encrypted_credential, key_path, encrypted_key_passphrase,               |
|                    | jump_host_chain (JSON list of BastionProfile.id), color_tag, group_tag, |
|                    | notes, log_enabled, log_path, log_mode, color_scheme, font_family,      |
|                    | font_size, created_at, last_connected_at                                |
| `Folder`           | name, parent_id (self-FK, nullable), sort_order, is_expanded            |
| `DefaultSession`   | singleton (id=1) global profile: username, encrypted_password,          |
|                    | key_path, encrypted_key_passphrase, port, protocol, color_scheme,       |
|                    | font_family, font_size, scrollback_lines, updated_at                    |
| `BastionProfile`   | name, hostname, port, username, auth_type, encrypted_credential,        |
|                    | key_path                                                                |
| `Command`          | group_id, name, command_text, description, tags (JSON), hotkey         |
| `CommandGroup`     | name, sort_order                                                        |
| `RecentConnection` | hostname, port, protocol, username, connected_at                        |
| `MasterAuth`       | bcrypt password hash + Fernet key salt (bootstrap row)                  |

`MasterAuth` is internal scaffolding — not in the original spec — used by the
credential vault to bootstrap the encryption key from the master password.

---

## 5. Concurrency Model

* **Qt main thread** owns all widgets.
* **`qasync` event-loop** runs alongside the Qt loop. Network I/O lives in
  asyncio tasks scheduled on this loop.
* Paramiko itself is blocking; we wrap blocking SSH transport calls in
  `loop.run_in_executor()` so they don't pin the GUI.
* SFTP transfers run on a `ThreadPoolExecutor` (default 3 workers, configurable
  in Settings → SFTP).
* All cross-thread communication goes through Qt signals.

---

## 6. Security Model

* Master password chosen on first launch.
* `bcrypt` hash of master password + a random salt are stored in the
  `master_auth` table.
* The Fernet key is derived from the master password via PBKDF2-HMAC-SHA256
  (390 000 iterations) using the stored salt.
* Per-session credentials and SSH key passphrases are Fernet-encrypted with
  this derived key before being persisted.
* Credentials are never logged. Plaintext is only held in memory while a
  connection is being established and is zeroed afterwards.
* Credential export is also Fernet-encrypted (separate passphrase prompt).

---

## 7. Color Tag Palette (Feature 8)

```
red  #e74c3c   orange #e67e22   yellow #f1c40f   green #27ae60
teal #1abc9c   blue   #3498db   purple #9b59b6   pink   #e91e63
gray #95a5a6
```

Group presets (rename/recolor in Settings → Groups):
`Production → red, Staging → yellow, Dev → green, Local → teal`.

---

## 8. Variable Substitution (Feature 5)

Tokens recognised in command text and log filenames:

| Token       | Replaced with                                       |
| ----------- | --------------------------------------------------- |
| `%HOST%`    | active session hostname                             |
| `%USER%`    | active session username                             |
| `%SESSION%` | active session name (sanitised for filenames)       |
| `%DATE%`    | `YYYY-MM-DD` (local time)                           |
| `%TIME%`    | `HH-MM-SS` (local time, dashes for filename safety) |

---

## 9. Key Bindings

| Action                | Shortcut                          |
| --------------------- | --------------------------------- |
| New tab / Quick conn. | `Ctrl+T`                          |
| Quick Connect dialog  | `Ctrl+Q`                          |
| Close tab             | `Ctrl+W`                          |
| Cycle tabs            | `Ctrl+Tab` / `Ctrl+Shift+Tab`     |
| Jump to tab N         | `Ctrl+1`…`Ctrl+9`                 |
| Find in scrollback    | `Ctrl+F`                          |
| Fuzzy command popup   | `Ctrl+Shift+Space`                |
| SFTP panel toggle     | `Ctrl+Shift+F`                    |
| Settings              | `Ctrl+,`                          |

---

## 10. Design Decisions Not in the Original Prompt

These are documented per the spec's instruction to record any deviations.

1. **Fallback terminal widget.** As described in §1, we use `qtermwidget` if
   importable and otherwise use a self-contained pyte/QPlainTextEdit emulator.
2. **Telnet via `telnetlib3`.** Python 3.12 removed `telnetlib`; `telnetlib3`
   is a maintained asyncio-native replacement.
3. **`MasterAuth` model.** Added as an internal table to bootstrap the Fernet
   key from the master password (not in the original schema).
4. **Variable `%TIME%`.** Uses dashes (`HH-MM-SS`) instead of colons so it can
   be embedded in filenames safely.
5. **SFTP transfer backend.** Paramiko is blocking, so transfers run on a
   `ThreadPoolExecutor` rather than asyncio tasks.
6. **`platformdirs`** is used for config/data/log paths — no hardcoded paths.
7. **Qt private-API avoidance.** Splits, detached tabs, and find-bar overlays
   are implemented purely with public PyQt6 API.
8. **Default Session (SecureCRT-style global profile).** Stored as a singleton
   row in the new `default_session` table rather than scattered across the
   settings TOML, so the password / key passphrase can be encrypted through
   the same `CredentialVault` as per-session credentials. Saved sessions
   inherit any field they leave blank from this row at connect time, and the
   toolbar Quick Host Bar uses these values to authenticate without prompting.
9. **Folder expand-state persistence.** `Folder.is_expanded` lives on the row
   itself rather than in a separate UI-state table — it's a single bool and
   the sidebar is the only consumer.
