"""
Study buddy bot - Phase 1 + 2
Telegram <-> Claude, with conversation history saved in SQLite.

Run:  python bot.py
"""

import logging
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ---------- Config (set these in .env) ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID", "").strip()  # only this chat gets replies
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "America/Chicago"))

DB_PATH = "bot.db"
HISTORY_LIMIT = 20  # how many past messages Claude sees (keep it even)

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# ---------- System prompt ----------
def build_system_prompt() -> str:
    now = datetime.now(TIMEZONE).strftime("%A, %B %d, %Y at %I:%M %p")
    return f"""You are a friendly accountability assistant for a high school student, chatting over Telegram.

Current date and time: {now}

Your job:
- Help him plan schoolwork (especially English and history) by breaking assignments into small steps, each with its own mini-deadline before the real due date.
- Keep him honest and moving: encouraging, but direct. If he's putting something off, say so kindly and suggest the smallest next step.
- Help him plan and get started. Don't write his assignments for him.

Style:
- Write like a text message: short, casual, usually 1-3 sentences.
- No markdown headers or long bullet lists."""


# ---------- Database ----------
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                role       TEXT    NOT NULL,  -- 'user' or 'assistant'
                content    TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            )"""
        )


def save_exchange(chat_id: int, user_text: str, reply: str) -> None:
    """Save a user message and Claude's reply together, so history always alternates."""
    now = datetime.now(TIMEZONE).isoformat()
    with sqlite3.connect(DB_PATH) as db:
        db.executemany(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [(chat_id, "user", user_text, now), (chat_id, "assistant", reply, now)],
        )


def load_history(chat_id: int) -> list[dict]:
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, HISTORY_LIMIT),
        ).fetchall()
    history = [{"role": role, "content": content} for role, content in reversed(rows)]
    # The Claude API needs the conversation to start with a user message.
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


def clear_history(chat_id: int) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))


# ---------- Telegram handlers ----------
def is_allowed(update: Update) -> bool:
    return bool(ALLOWED_CHAT_ID) and str(update.effective_chat.id) == ALLOWED_CHAT_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not ALLOWED_CHAT_ID:
        # First-time setup: tell the user their chat ID so they can lock the bot to it.
        await update.message.reply_text(
            f"Your chat ID is {chat_id}.\n"
            "Put it in your .env file as ALLOWED_CHAT_ID, then restart the bot."
        )
        return
    if not is_allowed(update):
        return  # silently ignore strangers
    await update.message.reply_text("Hey! I'm here. What are you working on?")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    clear_history(update.effective_chat.id)
    await update.message.reply_text("Memory wiped. Fresh start.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    messages = load_history(chat_id)
    messages.append({"role": "user", "content": user_text})

    try:
        response = await claude.messages.create(
            model=MODEL,
            max_tokens=500,
            system=build_system_prompt(),
            messages=messages,
        )
        reply = "".join(b.text for b in response.content if b.type == "text").strip()
    except Exception:
        logging.exception("Claude API call failed")
        await update.message.reply_text("Couldn't reach Claude just now. Try again in a sec.")
        return

    if not reply:
        reply = "(no reply - try rephrasing?)"

    save_exchange(chat_id, user_text, reply)
    await update.message.reply_text(reply)


# ---------- Main ----------
def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    # httpx logs full request URLs at INFO, which would print your bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logging.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
