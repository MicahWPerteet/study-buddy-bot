# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-user Telegram bot that relays messages to Claude and acts as an accountability/study buddy for a high school student. Everything lives in `bot.py`. The README describes the roadmap: Phases 1-3 (messaging, Claude replies, a task list managed through Claude tool use) are done; Phase 4 adds scheduled check-ins via python-telegram-bot's job queue (the `[job-queue]` extra is already in `requirements.txt` for this).

## Commands

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in TELEGRAM_TOKEN and ANTHROPIC_API_KEY
python bot.py          # runs via long polling; no server or webhook
```

There are no tests, linter config, or build step. Requires Python 3.10+. The only way to check a change end to end is to run the bot and message it from the allowed Telegram account. Bot commands: `/start`, `/tasks` (lists open tasks, no API call), `/reset` (wipes chat history, keeps tasks).

- Run it from the repo root. `DB_PATH = "bot.db"` is relative, so starting it from another directory quietly creates a new, empty database.
- Only one process can poll a given token. If a second copy is running, you get a Telegram `Conflict` error.

## Architecture

- **Flow:** `handle_message` loads recent history from SQLite, appends the new user text, then runs a manual tool-use loop (up to `MAX_TOOL_ROUNDS`) around `claude.messages.create`. Inside the loop, each assistant turn goes back unchanged (thinking blocks included), with all tool results in one user message. Afterwards only the user text and the final text reply are persisted, together, by `save_exchange`; tool calls never reach the `messages` table. Persisting only after a successful reply keeps stored history strictly alternating user/assistant. Don't break that invariant, because `load_history` relies on it (it only trims leading assistant messages so the API sees a user turn first).
- **Model settings:** the default model (`claude-haiku-5-5`) thinks by default, and thinking counts toward `max_tokens`, so `MAX_TOKENS` is 2000 with `effort: low`. Reply length is controlled by the system prompt, not `max_tokens`. That model rejects `temperature` and assistant prefill.
- **Tasks:** `assignments` own ordered `steps`, and each step has its own mini-deadline. Dates are stored as `YYYY-MM-DD` text, so they compare as strings. Claude changes tasks through the tools in `TOOLS`, which `run_tool` dispatches to plain helper functions. Helpers raise `ValueError` for bad input, and `tool_result` turns that into an `is_error` result so Claude can retry. Every query is scoped to `chat_id` (steps through a join), so a wrong ID from Claude can't touch another chat's rows. An assignment's status follows its steps: it's done when the last step is done, and reopens when a step is reopened. There is no list tool. Open tasks with IDs are injected into the system prompt on each request by `tasks_for_prompt` (plain text, ISO dates).
- **History window:** `HISTORY_LIMIT = 20` rows, kept even so the window starts on a user message.
- **System prompt:** `build_system_prompt(chat_id)` is rebuilt on every API call, including each round of the tool loop, so it carries the current date/time in `TIMEZONE` and the up-to-date task list. Personality, style rules (short, text-message tone, don't write assignments for the student), and the task workflow live there. The workflow is to propose steps first, as a keycap-emoji list, and call `save_assignment` only after the student agrees.
- **Formatting:** Claude's replies are sent as plain text with no `parse_mode`, so the prompt forbids markdown. Telegram would show it as raw `**`/`#`, and enabling a parse mode for model output risks Telegram rejecting malformed markup. Only `/tasks` uses Telegram HTML (`tasks_for_telegram`, sent with `ParseMode.HTML`). It's built in code, so any user-supplied text in it must go through `html.escape`.
- **Access control:** `is_allowed` only replies to `ALLOWED_CHAT_ID`. When that is unset, `/start` replies with the caller's chat ID as a first-time setup step, and every other handler stays silent. Any new handler must check `is_allowed` first.
- **Storage:** `bot.db` (SQLite, gitignored) with `messages`, `assignments`, and `steps` tables, created by `init_db()` at startup with `CREATE TABLE IF NOT EXISTS`, so existing databases gain new tables but not new columns. Each helper opens its own short-lived connection. `messages.content` holds plain text and is passed straight to the API as message content.
- **Config:** loaded from `.env` via python-dotenv: `TELEGRAM_TOKEN`, `ANTHROPIC_API_KEY` (required), `ALLOWED_CHAT_ID`, `CLAUDE_MODEL` (default `claude-haiku-5-5`), `TIMEZONE` (default `America/Chicago`) (optional). The required vars are read at import time, so if one is missing the bot crashes on startup with a `KeyError`.
- **Logging:** the `httpx` logger is raised to WARNING on purpose, because at INFO it logs request URLs that contain the bot token. Keep it that way.
