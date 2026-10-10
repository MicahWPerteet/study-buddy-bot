# Study Buddy Bot

A Telegram bot that chats with you through Claude, remembers your conversation, and keeps a task list.
This covers Phases 1-3 of the plan: messaging, Claude replies, and assignments broken into steps. Scheduled check-ins come next.

## Setup

1. **Create the bot.** In Telegram, message `@BotFather`, send `/newbot`, pick a name and username, and copy the token it gives you.
2. **Get a Claude API key** from the Claude Console (console.anthropic.com) and add a few dollars of credit.
3. **Install dependencies** (Python 3.10+):
   ```
   python -m venv .venv
   source .venv/bin/activate      # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
4. **Configure.** Copy `.env.example` to `.env` and fill in `TELEGRAM_TOKEN` and `ANTHROPIC_API_KEY`.
5. **Lock it to you.** Run `python bot.py`, open your bot in Telegram, and send `/start`. It replies with your chat ID. Paste that into `.env` as `ALLOWED_CHAT_ID`, then stop the bot (Ctrl+C) and start it again. From now on it ignores everyone but you.
6. **Text it.**

## Commands

- `/start`: say hi (or get your chat ID during setup)
- `/tasks`: show your open assignments and their steps
- `/reset`: wipe the conversation history (your tasks are kept)

## Tasks

Tell the bot about an assignment ("history essay due Friday") and it suggests a few steps, each with its own mini-deadline. Say ok and it saves them. Tell it when you finish a step, when a due date moves, or when an assignment gets cancelled, and it updates the list. When the last step is done, the assignment is done too.

## Notes

- The bot uses polling, so it runs on your own computer with no server or public URL needed. It only answers while `bot.py` is running.
- For scheduled check-ins later, it needs to run all the time: an always-on computer, a Raspberry Pi, or a cheap cloud host.
- Conversation history and tasks live in `bot.db` (SQLite). Claude sees the last 20 messages plus your open tasks.
- Edit `build_system_prompt()` in `bot.py` to change the bot's personality.

## Next up

- **Phase 4:** scheduled check-ins using the job queue built into python-telegram-bot.
