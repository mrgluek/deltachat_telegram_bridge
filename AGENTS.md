# AGENTS.md — Delta Chat ↔ Telegram Bridge

## Entry point & architecture

- **Entry point**: `bot.py` runs `python bot.py serve`. `bot.py` calls `handle_init()` for `init` subcommands, then `asyncio.run(main.main())`.
- **28 `.py` files**, ~5811 lines. Largest: `handlers/dc/commands.py` (934), `handlers/tg/commands.py` (907), `database.py` (646).
- **`handlers/dc/`** (3 files, 1479 lines) — DC commands, messaging, events. **`handlers/tg/`** (3 files, 1521 lines) — TG commands, messaging, events.
- **`dc_handlers.py` / `tg_handlers.py`** are thin re-export stubs (6 / 4 lines) — `from handlers.dc.commands import *`, etc. **Must be imported** for the `@app_ctx.dc_cli.on()` / `@app_ctx.tg_app.add_handler()` decorators inside submodules to fire.
- **`main.py`**: lazy-imports handlers inside `_import_handlers()` (line 37). At import time, the `handlers/dc/*` decorators register DC commands on `app_ctx.dc_cli`. TG handlers are registered explicitly in `_register_handlers()` (line 56).
- **No tests, no lint, no typecheck config.** CI is a changelog-only workflow — no test runs.
- **`database.py`**: single `Database` class + `@contextmanager` for `_conn()` (yields cursor). All 59 legacy module-level functions are preserved as thin wrappers. **Sync** (`threading.Lock`), no async. Do NOT call from async code without `run_in_executor`.
- **`app_context.py`**: module-level `app_ctx = AppContext()`. The `BotCli("tgbridge")` constructor runs at import time — on Python ≥3.14 it may crash due to argparse help-string incompatibility.
- **`shared.py`**: extracted to break circular imports. Contains `_inline_links`, `to_dc_markdown`, `get_tg_help_text`.

## Running & init

```bash
python bot.py serve                  # local (requires venv)
docker compose up -d                 # Docker (recommended)
docker compose run --rm bridge python bot.py init ...   # one-off init
```

All persistent state lives in `bridge.db` (SQLite). Init commands (`bot.py:INIT_DISPATCH`):
- `init dc <email> [password]` — prompted for display name & avatar
- `init tg [token]` — prompted if omitted
- `init admin_tg <id>` / `init admin_dc <email>` — locks management to owner
- `init api_id <id>` / `init api_hash <hash>` — Userbot MTProto credentials
- `init userbot` — interactive Telethon sign-in (creates `userbot_session.session`)
- `init transport <uri|addr> [password]` — backup mail relay

## Key behaviors

| Behavior | Detail |
|---|---|
| Rate limits | 30 msg/min per chat, 60 msg/min global DC (`constants.py`), 5 deletions/60s |
| Edit debounce | 60s debounce per (chat, msg_id) — suppresses link-preview edits |
| Double-bridge dedup | In-memory `_processed_tg_msgs` dict (120s TTL) |
| History relay | Last 3 posts on bridge creation & new member join (5min cooldown) |
| Group→supergroup | Auto-detected via `migrate_to_chat_id`, all DB refs updated |
| Transient errors | Filtered from admin notifications (`TRANSIENT_POLLING_ERRORS` in `constants.py`) |
| DC→TG deletion sync | Skipped for channel broadcasts; exempts bot-initiated deletes (edit replacements) |
| Userbot watchdog | Health check every 60s, auto-restarts on failure |

## Critical constraints

- **TG bot must have Group Privacy disabled** in BotFather to read group messages
- **For reaction bridging (TG→DC)**, bot must be group **administrator**
- **`main.py` uses lazy imports** inside `main()` (line 37) to avoid circular imports. Always keep handler imports lazy.
- **`app_context.py`**: `BotCli("tgbridge")` triggers at import — may crash on Python 3.14+.
- **`database.py` `_conn()` yields cursor, not connection.** All `c.execute()`, `c.fetchone()`, `c.fetchall()`, `c.lastrowid` work. Do NOT change to yielding `sqlite3.Connection`.
- Videos >20MB use lower-resolution fallback. Media >50MB rejected from Userbot downloads.
- `set_admin.py` is a standalone CLI for DC admin credential management — does NOT import `app_context`.
- Requires **Python 3.9+** (Docker uses `python:3.11-slim`).
