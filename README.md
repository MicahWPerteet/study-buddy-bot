# Study Buddy Bot

A Telegram bot that chats with you through Claude, remembers your conversation, keeps a task list, and checks in on its own.
This covers all four phases of the plan: messaging, Claude replies, assignments broken into steps, and scheduled check-ins.

## Setup

1. **Create the bot.** In Telegram, message `@BotFather`, send `/newbot`, pick a name and username, and copy the token it gives you.
2. **Get a Claude API key** from the Claude Console (console.anthropic.com) and add a few dollars of credit.
3. **Install dependencies** (Python 3.10+):
   ```
   python -m venv .venv
   source .venv/bin/activate      # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
4. **Configure.** Create a file named `.env` next to `bot.py` (it's gitignored, so never commit it):
   ```
   TELEGRAM_TOKEN=        # from @BotFather
   ANTHROPIC_API_KEY=     # from the Claude Console
   ALLOWED_CHAT_ID=       # leave blank for now, step 5 fills it in
   # Optional:
   # CLAUDE_MODEL=claude-haiku-5-5
   # TIMEZONE=America/Chicago
   ```
5. **Lock it to you.** Run `python bot.py`, open your bot in Telegram, and send `/start`. It replies with your chat ID. Paste that into `.env` as `ALLOWED_CHAT_ID`, then stop the bot (Ctrl+C) and start it again. From now on it ignores everyone but you.
6. **Text it.**

## Commands

- `/start`: say hi (or get your chat ID during setup)
- `/tasks`: show your open assignments and their steps
- `/reset`: wipe the conversation history (your tasks are kept)

## Tasks

Tell the bot about an assignment ("history essay due Friday") and it suggests a few steps, each with its own mini-deadline. Say ok and it saves them. Tell it when you finish a step, when a due date moves, or when an assignment gets cancelled, and it updates the list. When the last step is done, the assignment is done too.

## Check-ins

The bot also messages you on its own:

- **Morning** (7 AM on weekdays, 10 AM on weekends): what's due today.
- **4 PM:** a nudge about steps that are past their mini-deadline, at most once a day per step.
- **8 PM:** asks how today's steps went, so you can tell it what's done or move what isn't.

A check-in with nothing to say isn't sent. Reply to one like any other message. Times use `TIMEZONE`, and you can change them in `CHECKINS` near the top of `bot.py`.

## Notes

- The bot uses polling, so it runs on your own computer with no server or public URL needed. It only answers while `bot.py` is running.
- Check-ins only go out while `bot.py` is running. For them to arrive every day, run it on something that's always on: a Raspberry Pi or a cheap cloud host.
- Conversation history and tasks live in `bot.db` (SQLite). Claude sees the last 20 messages, your open tasks, and the check-ins it sent in the last 24 hours.
- Edit `build_system_prompt()` in `bot.py` to change the bot's personality.
