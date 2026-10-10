# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-user Telegram bot that relays messages to Claude and acts as an accountability/study buddy for a high school student. Everything lives in `bot.py`. The README describes the roadmap: Phases 1+2 (messaging + Claude replies) are done; Phase 3 adds a tasks table plus Claude tool use; Phase 4 adds scheduled check-ins via python-telegram-bot's job queue (the `[job-queue]` extra is already in `requirements.txt` for this).

## Commands

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in TELEGRAM_TOKEN and ANTHROPIC_API_KEY
python bot.py          # runs via long polling; no server or webhook
```

There are no tests, linter config, or build step. Requires Python 3.10+. The only way to check a change end to end is to run the bot and message it from the allowed Telegram account. Bot commands: `/start`, `/reset` (wipes history).

- Run it from the repo root. `DB_PATH = "bot.db"` is relative, so starting it from another directory quietly creates a new, empty database.
- Only one process can poll a given token. If a second copy is running, you get a Telegram `Conflict` error.

## Architecture

- **Flow:** `handle_message` loads recent history from SQLite, appends the new user text, calls `claude.messages.create` (async client, `max_tokens=500`), then persists the user message and reply together with `save_exchange`. Persisting only after a successful reply keeps stored history strictly alternating user/assistant. Don't break that invariant, because `load_history` relies on it (it only trims leading assistant messages so the API sees a user turn first).
- **History window:** `HISTORY_LIMIT = 20` rows, kept even so the window starts on a user message.
- **System prompt:** `build_system_prompt()` is rebuilt on every request so it carries the current date/time in `TIMEZONE`. Personality and style rules (short, text-message tone, no markdown, don't write assignments for the student) live there.
- **Access control:** `is_allowed` only replies to `ALLOWED_CHAT_ID`. When that is unset, `/start` replies with the caller's chat ID as a first-time setup step, and every other handler stays silent. Any new handler must check `is_allowed` first.
- **Storage:** `bot.db` (SQLite, gitignored) with a single `messages` table, created by `init_db()` at startup. Each helper opens its own short-lived connection. `content` holds plain text and is passed straight to the API as message content. For Phase 3 tool use, either save only the final text reply or change the schema to hold content blocks.
- **Config:** loaded from `.env` via python-dotenv: `TELEGRAM_TOKEN`, `ANTHROPIC_API_KEY` (required), `ALLOWED_CHAT_ID`, `CLAUDE_MODEL` (default `claude-haiku-5-5`), `TIMEZONE` (default `America/Chicago`) (optional). The required vars are read at import time, so if one is missing the bot crashes on startup with a `KeyError`.
- **Logging:** the `httpx` logger is raised to WARNING on purpose, because at INFO it logs request URLs that contain the bot token. Keep it that way.
