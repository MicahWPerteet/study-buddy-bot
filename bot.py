"""
Study buddy bot - Phases 1-4
Telegram <-> Claude, with conversation history and a task list saved in SQLite,
plus scheduled check-ins.

Run:  python bot.py
"""

import html
import logging
import os
import sqlite3
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction, ParseMode
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
MAX_TOKENS = 2000  # thinking counts toward this, so leave room beyond the short reply
MAX_TOOL_ROUNDS = 5  # most tool-use round trips per incoming message

# Scheduled check-ins: (kind, hour, minute, days). Days run 0-6 = Sunday-Saturday.
WEEKDAYS = (1, 2, 3, 4, 5)
EVERY_DAY = (0, 1, 2, 3, 4, 5, 6)
CHECKINS = [
    ("morning", 7, 0, WEEKDAYS),
    ("morning", 10, 0, (0, 6)),
    ("overdue", 16, 0, EVERY_DAY),
    ("evening", 20, 0, EVERY_DAY),
]

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# ---------- System prompt ----------
def build_system_prompt(chat_id: int) -> str:
    now = datetime.now(TIMEZONE).strftime("%A, %B %d, %Y at %I:%M %p")
    tasks = tasks_for_prompt(chat_id) or "None yet."
    checkins = checkins_for_prompt(chat_id) or "None."
    return f"""You are a friendly accountability assistant for a high school student, chatting over Telegram.

Current date and time: {now}

Your job:
- Help him plan schoolwork (especially English and history) by breaking assignments into small steps, each with its own mini-deadline before the real due date.
- Keep him honest and moving: encouraging, but direct. If he's putting something off, say so kindly and suggest the smallest next step.
- Help him plan and get started. Don't write his assignments for him.

Task list:
- When he mentions an assignment, propose 2-5 steps with mini-deadlines and ask if that works. Don't save anything yet. Put one line before the steps, one step per line as a keycap emoji, a few words, and the day, then a short question:
  Here's a plan for the WW1 essay (due Fri):

  1️⃣ Pick a thesis · Sun
  2️⃣ Gather 3 sources · Tue
  3️⃣ Outline · Wed

  Work for you?
- Once he agrees, save it with save_assignment, then confirm in a few words.
- When he says he finished a step, mark it with complete_step. Use update_assignment or update_step when plans change, and delete_assignment only if he asks or the assignment got cancelled.
- Tools take dates as YYYY-MM-DD. Work out days like "Friday" from the current date above.
- The [ids] below are for tools only. Never show them to him.

Current tasks:
{tasks}

Check-ins:
- You also message him on your own: a morning rundown, a 4 PM nudge about overdue steps, and an 8 PM check on today's steps. If he replies to one, handle it like any message (mark steps done, move deadlines).

Check-ins you sent in the last 24 hours:
{checkins}

Style:
- Write like a text message: short, casual, usually 1-3 sentences.
- Plain text only. Telegram shows markdown like ** or # as raw symbols, so never use it. A step proposal is the only list you write."""


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
        db.execute(
            """CREATE TABLE IF NOT EXISTS assignments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                title      TEXT    NOT NULL,
                subject    TEXT,
                due_date   TEXT    NOT NULL,  -- YYYY-MM-DD
                status     TEXT    NOT NULL,  -- 'open' or 'done'
                created_at TEXT    NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS steps (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                assignment_id INTEGER NOT NULL REFERENCES assignments(id),
                position      INTEGER NOT NULL,
                description   TEXT    NOT NULL,
                due_date      TEXT    NOT NULL,  -- YYYY-MM-DD
                done_at       TEXT              -- NULL until finished
            )"""
        )
        # Kept out of `messages` so stored history still alternates user/assistant.
        db.execute(
            """CREATE TABLE IF NOT EXISTS checkins (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                kind    TEXT    NOT NULL,  -- 'morning', 'overdue' or 'evening'
                content TEXT    NOT NULL,
                sent_at TEXT    NOT NULL
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
        db.execute("DELETE FROM checkins WHERE chat_id = ?", (chat_id,))


def save_checkin(chat_id: int, kind: str, text: str) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO checkins (chat_id, kind, content, sent_at) VALUES (?, ?, ?, ?)",
            (chat_id, kind, text, datetime.now(TIMEZONE).isoformat()),
        )


def checkins_for_prompt(chat_id: int) -> str:
    """Check-ins sent in the last 24 hours, oldest first, for Claude's system prompt."""
    since = (datetime.now(TIMEZONE) - timedelta(hours=24)).isoformat()
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute(
            "SELECT sent_at, content FROM checkins WHERE chat_id = ? AND sent_at > ? ORDER BY id",
            (chat_id, since),
        ).fetchall()
    return "\n".join(
        f"{datetime.fromisoformat(sent_at):%a %I:%M %p}: {content}" for sent_at, content in rows
    )


# ---------- Tasks ----------
# These raise ValueError on bad input; the tool loop sends the message back to Claude.
def check_date(value: str) -> str:
    """Normalize to YYYY-MM-DD so dates sort and compare correctly as text."""
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"Bad date {value!r}, use YYYY-MM-DD") from None


def find_assignment(db: sqlite3.Connection, chat_id: int, assignment_id: int) -> None:
    row = db.execute(
        "SELECT 1 FROM assignments WHERE id = ? AND chat_id = ?", (assignment_id, chat_id)
    ).fetchone()
    if row is None:
        raise ValueError(f"No assignment with id {assignment_id}")


def find_step(db: sqlite3.Connection, chat_id: int, step_id: int) -> int:
    """Return the step's assignment id, checking the step belongs to this chat."""
    row = db.execute(
        """SELECT s.assignment_id FROM steps s JOIN assignments a ON a.id = s.assignment_id
           WHERE s.id = ? AND a.chat_id = ?""",
        (step_id, chat_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"No step with id {step_id}")
    return row[0]


def refresh_status(db: sqlite3.Connection, assignment_id: int) -> str:
    """An assignment is done once all its steps are, and reopens if a step does."""
    open_steps = db.execute(
        "SELECT COUNT(*) FROM steps WHERE assignment_id = ? AND done_at IS NULL", (assignment_id,)
    ).fetchone()[0]
    status = "open" if open_steps else "done"
    db.execute("UPDATE assignments SET status = ? WHERE id = ?", (status, assignment_id))
    return status


def add_assignment(
    chat_id: int, title: str, subject: str | None, due_date: str, steps: list[dict]
) -> int:
    if not steps:
        raise ValueError("An assignment needs at least one step")
    due_date = check_date(due_date)
    step_rows = [(s["description"], check_date(s["due_date"])) for s in steps]
    now = datetime.now(TIMEZONE).isoformat()
    with sqlite3.connect(DB_PATH) as db:
        cursor = db.execute(
            """INSERT INTO assignments (chat_id, title, subject, due_date, status, created_at)
               VALUES (?, ?, ?, ?, 'open', ?)""",
            (chat_id, title, subject, due_date, now),
        )
        assignment_id = cursor.lastrowid
        db.executemany(
            "INSERT INTO steps (assignment_id, position, description, due_date) VALUES (?, ?, ?, ?)",
            [(assignment_id, i, desc, due) for i, (desc, due) in enumerate(step_rows, start=1)],
        )
    return assignment_id


def update_assignment(
    chat_id: int,
    assignment_id: int,
    title: str | None = None,
    subject: str | None = None,
    due_date: str | None = None,
    status: str | None = None,
) -> None:
    changes = {"title": title, "subject": subject, "due_date": due_date, "status": status}
    changes = {column: value for column, value in changes.items() if value is not None}
    if not changes:
        raise ValueError("Nothing to change")
    if "due_date" in changes:
        changes["due_date"] = check_date(changes["due_date"])
    set_clause = ", ".join(f"{column} = ?" for column in changes)
    with sqlite3.connect(DB_PATH) as db:
        find_assignment(db, chat_id, assignment_id)
        db.execute(
            f"UPDATE assignments SET {set_clause} WHERE id = ?", [*changes.values(), assignment_id]
        )
        if status == "done":  # finishing the assignment finishes its steps too
            db.execute(
                "UPDATE steps SET done_at = ? WHERE assignment_id = ? AND done_at IS NULL",
                (datetime.now(TIMEZONE).isoformat(), assignment_id),
            )


def update_step(
    chat_id: int,
    step_id: int,
    description: str | None = None,
    due_date: str | None = None,
    done: bool | None = None,
) -> str | None:
    """Returns the assignment's new status when `done` is given, else None."""
    changes = {"description": description, "due_date": due_date}
    changes = {column: value for column, value in changes.items() if value is not None}
    if "due_date" in changes:
        changes["due_date"] = check_date(changes["due_date"])
    if done is not None:
        changes["done_at"] = datetime.now(TIMEZONE).isoformat() if done else None
    if not changes:
        raise ValueError("Nothing to change")
    set_clause = ", ".join(f"{column} = ?" for column in changes)
    with sqlite3.connect(DB_PATH) as db:
        assignment_id = find_step(db, chat_id, step_id)
        db.execute(f"UPDATE steps SET {set_clause} WHERE id = ?", [*changes.values(), step_id])
        return refresh_status(db, assignment_id) if done is not None else None


def delete_assignment(chat_id: int, assignment_id: int) -> None:
    with sqlite3.connect(DB_PATH) as db:
        find_assignment(db, chat_id, assignment_id)
        db.execute("DELETE FROM steps WHERE assignment_id = ?", (assignment_id,))
        db.execute("DELETE FROM assignments WHERE id = ?", (assignment_id,))


def get_open_assignments(chat_id: int) -> list[dict]:
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        assignments = [
            dict(row)
            for row in db.execute(
                """SELECT id, title, subject, due_date FROM assignments
                   WHERE chat_id = ? AND status = 'open' ORDER BY due_date, id""",
                (chat_id,),
            )
        ]
        for assignment in assignments:
            assignment["steps"] = [
                dict(row)
                for row in db.execute(
                    """SELECT id, description, due_date, done_at FROM steps
                       WHERE assignment_id = ? ORDER BY position""",
                    (assignment["id"],),
                )
            ]
    return assignments


def today() -> str:
    return datetime.now(TIMEZONE).date().isoformat()


def is_overdue(step: dict) -> bool:
    return not step["done_at"] and step["due_date"] < today()


def tasks_for_prompt(chat_id: int) -> str:
    """Plain-text task list with ids and ISO dates, for Claude's system prompt."""

    def show_date(iso: str) -> str:
        return date.fromisoformat(iso).strftime("%a %Y-%m-%d")

    lines = []
    for assignment in get_open_assignments(chat_id):
        subject = f" ({assignment['subject']})" if assignment["subject"] else ""
        lines.append(
            f"[assignment {assignment['id']}] {assignment['title']}{subject}, "
            f"due {show_date(assignment['due_date'])}"
        )
        for step in assignment["steps"]:
            mark = "✓" if step["done_at"] else "-"
            overdue = ", overdue" if is_overdue(step) else ""
            lines.append(
                f"  {mark} [step {step['id']}] {step['description']} "
                f"(by {show_date(step['due_date'])}{overdue})"
            )
    return "\n".join(lines)


def tasks_for_telegram(chat_id: int) -> str:
    """Task list for /tasks, formatted with Telegram HTML (send with parse_mode=HTML)."""

    def show_date(iso: str) -> str:
        d = date.fromisoformat(iso)
        return f"{d:%a %b} {d.day}"

    blocks = []
    for assignment in get_open_assignments(chat_id):
        subject = f" · {html.escape(assignment['subject'])}" if assignment["subject"] else ""
        lines = [
            f"📚 <b>{html.escape(assignment['title'])}</b>{subject}",
            f"<i>due {show_date(assignment['due_date'])}</i>",
            "",
        ]
        for step in assignment["steps"]:
            description = html.escape(step["description"])
            when = show_date(step["due_date"])
            if step["done_at"]:
                lines.append(f"✅ <s>{description}</s> · {when}")
            elif is_overdue(step):
                lines.append(f"⚠️ {description} · {when} (overdue)")
            else:
                lines.append(f"⬜ {description} · {when}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ---------- Claude tools ----------
TOOLS = [
    {
        "name": "save_assignment",
        "description": "Save a new assignment and its steps. Only call this after the student has agreed to the steps you proposed.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short name, e.g. 'WW1 essay'"},
                "subject": {"type": "string", "description": "Class, e.g. 'History'"},
                "due_date": {"type": "string", "description": "Final due date, YYYY-MM-DD"},
                "steps": {
                    "type": "array",
                    "description": "Steps in the order he should do them",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "due_date": {"type": "string", "description": "Mini-deadline, YYYY-MM-DD"},
                        },
                        "required": ["description", "due_date"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["title", "due_date", "steps"],
            "additionalProperties": False,
        },
    },
    {
        "name": "complete_step",
        "description": "Mark a step as finished. Finishing the last step finishes the assignment.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"step_id": {"type": "integer"}},
            "required": ["step_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_assignment",
        "description": "Change an assignment: rename it, move its due date, or mark it done or open. Only pass the fields that change.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "assignment_id": {"type": "integer"},
                "title": {"type": "string"},
                "subject": {"type": "string"},
                "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                "status": {"type": "string", "enum": ["open", "done"]},
            },
            "required": ["assignment_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_step",
        "description": "Change a step: reword it, move its mini-deadline, or mark it done or not done. Only pass the fields that change.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "step_id": {"type": "integer"},
                "description": {"type": "string"},
                "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                "done": {"type": "boolean"},
            },
            "required": ["step_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_assignment",
        "description": "Delete an assignment and all its steps. Only when he asks, or the assignment was cancelled.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"assignment_id": {"type": "integer"}},
            "required": ["assignment_id"],
            "additionalProperties": False,
        },
    },
]


def run_tool(chat_id: int, name: str, args: dict) -> str:
    """Run one tool call and return a short result for Claude."""
    if name == "save_assignment":
        assignment_id = add_assignment(
            chat_id, args["title"], args.get("subject"), args["due_date"], args["steps"]
        )
        return f"Saved as assignment {assignment_id}."
    if name == "complete_step":
        status = update_step(chat_id, args["step_id"], done=True)
        return "Step done. That was the last one, so the assignment is done." if status == "done" else "Step done."
    if name == "update_assignment":
        update_assignment(
            chat_id,
            args["assignment_id"],
            args.get("title"),
            args.get("subject"),
            args.get("due_date"),
            args.get("status"),
        )
        return "Assignment updated."
    if name == "update_step":
        status = update_step(
            chat_id, args["step_id"], args.get("description"), args.get("due_date"), args.get("done")
        )
        return "Step updated, and all steps are done, so the assignment is done." if status == "done" else "Step updated."
    if name == "delete_assignment":
        delete_assignment(chat_id, args["assignment_id"])
        return "Assignment deleted."
    raise ValueError(f"Unknown tool {name}")


def tool_result(chat_id: int, block) -> dict:
    try:
        content, is_error = run_tool(chat_id, block.name, block.input), False
    except ValueError as e:
        content, is_error = f"Error: {e}", True
    except KeyError as e:
        content, is_error = f"Error: missing field {e}", True
    logging.info("Tool %s -> %s", block.name, content)
    return {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error}


# ---------- Check-ins ----------
CHECKIN_PROMPTS = {
    "morning": "It's the morning check-in. Everything below is due today. Tell him what's on and get him going.",
    "overdue": "It's the after-school nudge. These steps are past their mini-deadline. Be kind but direct, and suggest the smallest next step.",
    "evening": "It's the evening check-in. These steps were due today and aren't marked done. Ask how they went, so you can mark them done or move them.",
}
# Used when Claude can't be reached.
CHECKIN_HEADERS = {
    "morning": "Morning! On today's list:",
    "overdue": "Quick nudge, these are past their mini-deadline:",
    "evening": "How'd today go? Still open from today:",
}


def checkin_facts(chat_id: int, kind: str) -> str | None:
    """What a check-in is about, one item per line, or None if there's nothing to say.

    Morning and evening cover open steps due today, the 4 PM nudge covers overdue ones.
    The nudge job runs once a day, so each overdue step is nudged at most once a day.
    """
    now = today()
    lines = []
    for assignment in get_open_assignments(chat_id):
        for step in assignment["steps"]:
            if step["done_at"]:
                continue
            if kind == "overdue" and step["due_date"] < now:
                d = date.fromisoformat(step["due_date"])
                lines.append(f"{step['description']} ({assignment['title']}, was due {d:%a %b} {d.day})")
            elif kind != "overdue" and step["due_date"] == now:
                lines.append(f"{step['description']} ({assignment['title']})")
        if kind == "morning" and assignment["due_date"] == now:
            lines.append(f"{assignment['title']} itself is due today")
    return "\n".join(lines) or None


async def write_checkin(chat_id: int, kind: str, facts: str) -> str:
    """Have Claude write the check-in, falling back to a template."""
    # Stored history always ends on an assistant turn, so this user turn keeps it alternating.
    messages = load_history(chat_id)
    messages.append(
        {
            "role": "user",
            "content": f"[Scheduled check-in, not a message from the student] {CHECKIN_PROMPTS[kind]} "
            f"Write the message to send him, 1-3 sentences.\n\n{facts}",
        }
    )
    try:
        response = await claude.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=build_system_prompt(chat_id),
            output_config={"effort": "low"},
            messages=messages,
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
    except Exception:
        logging.exception("Claude API call failed for the %s check-in", kind)
        text = ""
    if not text:
        items = "\n".join(f"• {line}" for line in facts.splitlines())
        text = f"{CHECKIN_HEADERS[kind]}\n{items}"
    return text


async def send_checkin(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback. There's no Update here, so it sends to ALLOWED_CHAT_ID."""
    kind = context.job.data
    chat_id = int(ALLOWED_CHAT_ID)
    facts = checkin_facts(chat_id, kind)
    if facts is None:
        logging.info("Check-in %s skipped (nothing to say)", kind)
        return
    text = await write_checkin(chat_id, kind, facts)
    await context.bot.send_message(chat_id=chat_id, text=text)
    save_checkin(chat_id, kind, text)
    logging.info("Check-in %s sent", kind)


def schedule_checkins(app: Application) -> None:
    if not ALLOWED_CHAT_ID:
        logging.info("ALLOWED_CHAT_ID is unset, so no check-ins are scheduled.")
        return
    for kind, hour, minute, days in CHECKINS:
        app.job_queue.run_daily(
            send_checkin,
            time(hour, minute, tzinfo=TIMEZONE),
            days=days,
            data=kind,
            name=f"{kind} {hour:02}:{minute:02}",
        )


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
    await update.message.reply_text("Memory wiped. Fresh start. (Your tasks are still saved.)")


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    tasks = tasks_for_telegram(update.effective_chat.id)
    await update.message.reply_text(
        tasks or "Nothing on your list right now.", parse_mode=ParseMode.HTML
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text

    messages = load_history(chat_id)
    messages.append({"role": "user", "content": user_text})
    tools_ran = False

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            response = await claude.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=build_system_prompt(chat_id),
                tools=TOOLS,
                output_config={"effort": "low"},
                messages=messages,
            )
            if response.stop_reason != "tool_use":
                break
            # Send Claude's turn back unchanged (thinking blocks included),
            # with every tool result together in one user message.
            messages.append({"role": "assistant", "content": response.content})
            results = [tool_result(chat_id, b) for b in response.content if b.type == "tool_use"]
            messages.append({"role": "user", "content": results})
            tools_ran = True
        if response.stop_reason not in ("end_turn", "tool_use"):
            logging.warning("Claude stopped with %s", response.stop_reason)
        reply = "".join(b.text for b in response.content if b.type == "text").strip()
    except Exception:
        logging.exception("Claude API call failed")
        if tools_ran:
            await update.message.reply_text(
                "Your list is updated, but I couldn't finish replying. Check /tasks."
            )
        else:
            await update.message.reply_text("Couldn't reach Claude just now. Try again in a sec.")
        return

    if not reply:
        reply = "Done. Check /tasks to see your list." if tools_ran else "(no reply - try rephrasing?)"

    # Only the final text is stored; tool calls stay inside this request.
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
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    schedule_checkins(app)

    logging.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
