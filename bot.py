from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    BotCommandScopeChatMember,
    ChatMember,
    ChatPermissions,
    Message,
    MessageEntity,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


BOT_TOKEN = os.environ.get("BOT_TOKEN", "8621256430:AAG53niJ-VJXyjod5OQauncJDbtYP_1Ilpk")
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "database.sqlite3"))
LEGACY_DATABASE_PATH = Path(os.environ.get("LEGACY_DATABASE_PATH", "database.json"))
LOGGER = logging.getLogger("new_member_guard")

DEFAULT_SETTINGS = {
    "message_limit": 500,
    "violation_limit": 3,
    "violation_window": 300,
    "mute_duration": 3600,
    "warning_duration": 15,
}

MEMBER_COMMANDS = [
    BotCommand("start", "Start the bot"),
    BotCommand("help", "Show commands available to you"),
    BotCommand("rules", "Show this group's sticker and GIF rules"),
    BotCommand("count", "Check your message count"),
]
ADMIN_COMMANDS = MEMBER_COMMANDS + [
    BotCommand("check", "Check a member's message count"),
    BotCommand("trust", "Trust a member"),
    BotCommand("untrust", "Remove a member's trusted status"),
    BotCommand("trusted", "Check whether a member is trusted"),
    BotCommand("violations", "Set violations before mute (2-6)"),
    BotCommand("window", "Set the violation window"),
    BotCommand("mutetime", "Set the mute duration"),
    BotCommand("warntime", "Set warning auto-delete time"),
    BotCommand("settings", "View this group's guard settings"),
]
OWNER_COMMANDS = ADMIN_COMMANDS + [
    BotCommand("limit", "Set the required message count (50-1000)"),
]

DURATION_PATTERN = re.compile(r"(\d+)([smhdw])", re.IGNORECASE)
MIN_MUTE_SECONDS = 5
MAX_MUTE_SECONDS = 7 * 24 * 60 * 60
MAX_WINDOW_SECONDS = MAX_MUTE_SECONDS
MAX_WARNING_SECONDS = 24 * 60 * 60


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_database()) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                settings TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS members (
                chat_id INTEGER NOT NULL REFERENCES groups(chat_id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT NOT NULL DEFAULT '',
                message_count INTEGER NOT NULL DEFAULT 0,
                join_time TEXT NOT NULL,
                violations TEXT NOT NULL DEFAULT '[]',
                is_trusted INTEGER NOT NULL DEFAULT 0,
                mute_until TEXT,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS pending_warnings (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delete_at REAL NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            """
        )
        connection.commit()
    migrate_legacy_database()


def migrate_legacy_database() -> None:
    if not LEGACY_DATABASE_PATH.exists() or LEGACY_DATABASE_PATH.resolve() == DATABASE_PATH.resolve():
        return
    with closing(connect_database()) as connection:
        if connection.execute("SELECT 1 FROM groups LIMIT 1").fetchone():
            return
        try:
            legacy_data = json.loads(LEGACY_DATABASE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Could not read legacy database {LEGACY_DATABASE_PATH}") from error

        for raw_chat_id, old_group in legacy_data.get("groups", {}).items():
            settings = dict(DEFAULT_SETTINGS)
            if old_group.get("restriction_mode", "message") == "message":
                settings["message_limit"] = _bounded_legacy_int(
                    old_group.get("restriction_value"), settings["message_limit"], 50, 1000
                )
            settings["violation_limit"] = _bounded_legacy_int(
                old_group.get("violation_limit"), settings["violation_limit"], 2, 6
            )
            settings["violation_window"] = _bounded_legacy_int(
                old_group.get("violation_window"), settings["violation_window"], 5, MAX_WINDOW_SECONDS
            )
            settings["mute_duration"] = _bounded_legacy_int(
                old_group.get("mute_duration"), settings["mute_duration"], MIN_MUTE_SECONDS, MAX_MUTE_SECONDS
            )
            chat_id = int(raw_chat_id)
            connection.execute(
                "INSERT OR IGNORE INTO groups(chat_id, settings) VALUES (?, ?)",
                (chat_id, json.dumps(settings)),
            )
            for raw_user_id, old_member in old_group.get("members", {}).items():
                violations = []
                for value in old_member.get("violations", []):
                    try:
                        violations.append(datetime.fromisoformat(value).timestamp())
                    except (TypeError, ValueError):
                        continue
                connection.execute(
                    """
                    INSERT OR REPLACE INTO members
                    (chat_id, user_id, message_count, join_time, violations, is_trusted, mute_until)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        int(raw_user_id),
                        _legacy_int(old_member.get("message_count"), 0),
                        old_member.get("join_time") or datetime.now(timezone.utc).isoformat(),
                        json.dumps(violations),
                        int(bool(old_member.get("is_unrestricted"))),
                        old_member.get("mute_until"),
                    ),
                )
            for raw_user_id in old_group.get("trusted", []):
                try:
                    user_id = int(raw_user_id)
                except (TypeError, ValueError):
                    continue
                _ensure_member(connection, chat_id, user_id)
                connection.execute(
                    "UPDATE members SET is_trusted = 1 WHERE chat_id = ? AND user_id = ?",
                    (chat_id, user_id),
                )
        connection.commit()


def _legacy_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bounded_legacy_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    result = _legacy_int(value, default)
    return result if minimum <= result <= maximum else default


def parse_duration(value: str) -> int | None:
    """Parse a duration such as 30s, 2h, 3d, or 1d12h."""
    value = value.strip().lower()
    if not value:
        return None
    parts = list(DURATION_PATTERN.finditer(value))
    if not parts or "".join(part.group(0) for part in parts) != value:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        seconds = sum(int(part.group(1)) * units[part.group(2)] for part in parts)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def format_duration(seconds: int) -> str:
    units = (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1))
    remaining = seconds
    values = []
    for name, size in units:
        amount, remaining = divmod(remaining, size)
        if amount:
            values.append(f"{amount}{name}")
    return " ".join(values) or "0s"


def _ensure_group(connection: sqlite3.Connection, chat_id: int) -> dict[str, Any]:
    row = connection.execute(
        "SELECT settings FROM groups WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    if row is None:
        settings = dict(DEFAULT_SETTINGS)
        connection.execute(
            "INSERT INTO groups(chat_id, settings) VALUES (?, ?)",
            (chat_id, json.dumps(settings)),
        )
        return settings
    settings = dict(DEFAULT_SETTINGS)
    settings.update(json.loads(row["settings"]))
    return settings


def get_group_settings(chat_id: int) -> dict[str, Any]:
    with closing(connect_database()) as connection:
        with connection:
            return _ensure_group(connection, chat_id)


def set_group_settings(chat_id: int, updates: dict[str, Any]) -> dict[str, Any]:
    with closing(connect_database()) as connection:
        with connection:
            settings = _ensure_group(connection, chat_id)
            settings.update(updates)
            connection.execute(
                "UPDATE groups SET settings = ? WHERE chat_id = ?",
                (json.dumps(settings), chat_id),
            )
            return settings


def _ensure_member(
    connection: sqlite3.Connection,
    chat_id: int,
    user_id: int,
    username: str | None = None,
    full_name: str = "",
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO members(chat_id, user_id, join_time)
        VALUES (?, ?, ?)
        """,
        (chat_id, user_id, datetime.now(timezone.utc).isoformat()),
    )
    connection.execute(
        """
        UPDATE members SET username = COALESCE(?, username),
            full_name = CASE WHEN ? != '' THEN ? ELSE full_name END
        WHERE chat_id = ? AND user_id = ?
        """,
        (username, full_name, full_name, chat_id, user_id),
    )


def cache_member_identity(
    chat_id: int, user_id: int, username: str | None, full_name: str
) -> None:
    with closing(connect_database()) as connection:
        with connection:
            _ensure_group(connection, chat_id)
            _ensure_member(connection, chat_id, user_id, username, full_name)


def record_join(chat_id: int, user_id: int, username: str | None, full_name: str) -> None:
    with closing(connect_database()) as connection:
        with connection:
            _ensure_group(connection, chat_id)
            _ensure_member(connection, chat_id, user_id, username, full_name)
            connection.execute(
                """
                UPDATE members SET message_count = 0, join_time = ?, violations = '[]',
                    mute_until = NULL
                WHERE chat_id = ? AND user_id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), chat_id, user_id),
            )


def record_text_message(chat_id: int, user_id: int, username: str | None, full_name: str) -> int:
    with closing(connect_database()) as connection:
        with connection:
            _ensure_group(connection, chat_id)
            _ensure_member(connection, chat_id, user_id, username, full_name)
            connection.execute(
                "UPDATE members SET message_count = message_count + 1 WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            row = connection.execute(
                "SELECT message_count FROM members WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            return int(row["message_count"])


def record_violation(
    chat_id: int,
    user_id: int,
    username: str | None,
    full_name: str,
) -> tuple[dict[str, Any], int] | None:
    now = time.time()
    with closing(connect_database()) as connection:
        with connection:
            settings = _ensure_group(connection, chat_id)
            _ensure_member(connection, chat_id, user_id, username, full_name)
            member = connection.execute(
                """
                SELECT message_count, violations, is_trusted
                FROM members WHERE chat_id = ? AND user_id = ?
                """,
                (chat_id, user_id),
            ).fetchone()
            if member["is_trusted"] or member["message_count"] >= settings["message_limit"]:
                return None
            violations = [
                stamp for stamp in json.loads(member["violations"])
                if now - float(stamp) <= settings["violation_window"]
            ]
            violations.append(now)
            connection.execute(
                "UPDATE members SET violations = ? WHERE chat_id = ? AND user_id = ?",
                (json.dumps(violations), chat_id, user_id),
            )
            return settings, len(violations)


def set_member_trusted(chat_id: int, user_id: int, trusted: bool) -> None:
    with closing(connect_database()) as connection:
        with connection:
            _ensure_group(connection, chat_id)
            _ensure_member(connection, chat_id, user_id)
            connection.execute(
                "UPDATE members SET is_trusted = ? WHERE chat_id = ? AND user_id = ?",
                (int(trusted), chat_id, user_id),
            )


def get_member_record(chat_id: int, user_id: int) -> dict[str, Any] | None:
    with closing(connect_database()) as connection:
        row = connection.execute(
            "SELECT * FROM members WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def find_member_by_username(chat_id: int, username: str) -> dict[str, Any] | None:
    with closing(connect_database()) as connection:
        row = connection.execute(
            "SELECT * FROM members WHERE chat_id = ? AND lower(username) = lower(?)",
            (chat_id, username.lstrip("@")),
        ).fetchone()
        return dict(row) if row else None


def set_member_mute(chat_id: int, user_id: int, until: datetime) -> None:
    with closing(connect_database()) as connection:
        with connection:
            connection.execute(
                "UPDATE members SET mute_until = ?, violations = '[]' WHERE chat_id = ? AND user_id = ?",
                (until.isoformat(), chat_id, user_id),
            )


def save_pending_warning(chat_id: int, message_id: int, delete_at: float) -> None:
    with closing(connect_database()) as connection:
        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO pending_warnings(chat_id, message_id, delete_at) VALUES (?, ?, ?)",
                (chat_id, message_id, delete_at),
            )


def clear_pending_warning(chat_id: int, message_id: int) -> None:
    with closing(connect_database()) as connection:
        with connection:
            connection.execute(
                "DELETE FROM pending_warnings WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )


def get_pending_warnings() -> list[dict[str, Any]]:
    with closing(connect_database()) as connection:
        rows = connection.execute(
            "SELECT chat_id, message_id, delete_at FROM pending_warnings"
        ).fetchall()
        return [dict(row) for row in rows]


def is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type in ("group", "supergroup")


async def is_admin(update: Update) -> bool:
    if not is_group_chat(update) or update.effective_user is None:
        return False
    member = await update.effective_chat.get_member(update.effective_user.id)
    return member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)


async def send_reply(
    update: Update, text: str, parse_mode: str | None = None
) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=parse_mode)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_reply(
        update,
        "Hi! I help keep new-member sticker and GIF spam out of groups. "
        "Add me as an admin with permission to delete messages and restrict members.",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group_chat(update):
        await send_reply(update, "Use /help in the group you want to check.")
        return
    if await is_admin(update):
        text = (
            "Member commands: /count, /rules\n"
            "Admin commands: /check, /trust, /untrust, /trusted, /violations, "
            "/window, /mutetime, /warntime, /settings"
        )
        if await is_owner(update.effective_chat, update.effective_user.id):
            text += "\nOwner command: /limit"
    else:
        text = "Commands: /count, /rules"
    await send_reply(update, text)


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group_chat(update):
        await send_reply(update, "Use /rules in the group you want to check.")
        return
    settings = get_group_settings(update.effective_chat.id)
    await send_reply(
        update,
        f"📜 Sticker & GIF rules\n\n"
        f"New members need {settings['message_limit']} text messages before sending stickers or GIFs.\n"
        f"Sticker/GIF violations are counted over {format_duration(settings['violation_window'])}. "
        f"After {settings['violation_limit']} violations, the member is muted for "
        f"{format_duration(settings['mute_duration'])}.",
    )


async def count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group_chat(update):
        await send_reply(update, "Use /count in the group you want to check.")
        return
    if update.effective_user is None:
        await send_reply(update, "I couldn't identify your Telegram account.")
        return
    member = get_member_record(update.effective_chat.id, update.effective_user.id)
    settings = get_group_settings(update.effective_chat.id)
    message_count = member["message_count"] if member else 0
    remaining = max(0, settings["message_limit"] - message_count)
    if remaining:
        text = f"📊 Your count: {message_count}/{settings['message_limit']} text messages ({remaining} to go)."
    else:
        text = f"✅ You have sent {message_count} text messages. Stickers and GIFs are unlocked."
    await send_reply(update, text)


async def is_owner(chat: Any, user_id: int) -> bool:
    member = await chat.get_member(user_id)
    return member.status == ChatMember.OWNER


async def resolve_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[int | None, str | None]:
    message = update.effective_message
    if message and message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        return user.id, user.full_name
    if message:
        for entity in message.entities or []:
            if entity.type == MessageEntity.TEXT_MENTION and entity.user:
                return entity.user.id, entity.user.full_name
    if not context.args:
        return None, None
    identifier = context.args[0]
    if identifier.isdecimal():
        return int(identifier), None
    if identifier.startswith("@"):
        member = find_member_by_username(update.effective_chat.id, identifier)
        if member:
            return int(member["user_id"]), member["full_name"]
    return None, None


async def check_target_membership(update: Update, user_id: int) -> bool:
    try:
        member = await update.effective_chat.get_member(user_id)
    except BadRequest:
        return False
    return member.status not in (ChatMember.LEFT, ChatMember.BANNED)


async def admin_only(update: Update) -> bool:
    if not is_group_chat(update):
        await send_reply(update, "This command only works in a group.")
        return False
    if not await is_admin(update):
        await send_reply(update, "This command is available to group admins only.")
        return False
    return True


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    user_id, name = await resolve_target(update, context)
    if user_id is None:
        await send_reply(update, "Reply to a member or use /check @username or /check user_id.")
        return
    if not await check_target_membership(update, user_id):
        await send_reply(update, "That user is not a current member of this group.")
        return
    member = get_member_record(update.effective_chat.id, user_id)
    settings = get_group_settings(update.effective_chat.id)
    message_count = member["message_count"] if member else 0
    remaining = max(0, settings["message_limit"] - message_count)
    label = escape(name or (member["full_name"] if member else "") or str(user_id))
    await send_reply(
        update,
        f"📊 <a href=\"tg://user?id={user_id}\">{label}</a>: "
        f"{message_count}/{settings['message_limit']} text messages; {remaining} to go.",
        parse_mode="HTML",
    )


async def set_trusted(update: Update, context: ContextTypes.DEFAULT_TYPE, trusted: bool) -> None:
    if not await admin_only(update):
        return
    user_id, name = await resolve_target(update, context)
    if user_id is None:
        verb = "trust" if trusted else "untrust"
        await send_reply(update, f"Reply to a member or use /{verb} @username or user_id.")
        return
    if not await check_target_membership(update, user_id):
        await send_reply(update, "That user is not a current member of this group.")
        return
    set_member_trusted(update.effective_chat.id, user_id, trusted)
    label = escape(name or str(user_id))
    status = "is now trusted" if trusted else "is no longer trusted"
    await send_reply(
        update, f"✅ <a href=\"tg://user?id={user_id}\">{label}</a> {status}.", parse_mode="HTML"
    )


async def trust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_trusted(update, context, True)


async def untrust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_trusted(update, context, False)


async def trusted_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    user_id, name = await resolve_target(update, context)
    if user_id is None:
        await send_reply(update, "Reply to a member or use /trusted @username or user_id.")
        return
    member = get_member_record(update.effective_chat.id, user_id)
    label = escape(name or (member["full_name"] if member else "") or str(user_id))
    state = "trusted ✅" if member and member["is_trusted"] else "not trusted"
    await send_reply(
        update, f"<a href=\"tg://user?id={user_id}\">{label}</a> is {state}.", parse_mode="HTML"
    )


async def set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group_chat(update):
        await send_reply(update, "This command only works in a group.")
        return
    if update.effective_user is None:
        await send_reply(update, "Only the group owner can change the message limit.")
        return
    if not await is_owner(update.effective_chat, update.effective_user.id):
        await send_reply(update, "Only this group's owner can change the message limit.")
        return
    if not context.args:
        await send_reply(update, "Usage: /limit 500 (choose a number from 50 to 1000).")
        return
    try:
        value = int(context.args[0])
    except ValueError:
        await send_reply(update, "Enter a whole number from 50 to 1000.")
        return
    if not 50 <= value <= 1000:
        await send_reply(update, "The message limit must be from 50 to 1000.")
        return
    set_group_settings(update.effective_chat.id, {"message_limit": value})
    await send_reply(update, f"✅ New members need {value} text messages to unlock stickers and GIFs.")


async def set_admin_setting(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    setting: str,
    label: str,
    minimum: int,
    maximum: int,
    duration: bool = False,
) -> None:
    if not await admin_only(update):
        return
    if not context.args:
        suffix = " (for example 30s, 5m, 3d)" if duration else f" ({minimum}-{maximum})"
        await send_reply(update, f"Usage: /{context.effective_message.text.split()[0][1:]} <value>{suffix}.")
        return
    if duration:
        value = parse_duration(context.args[0])
        if value is None or not minimum <= value <= maximum:
            await send_reply(
                update,
                f"{label} must be from {format_duration(minimum)} to {format_duration(maximum)}.",
            )
            return
    else:
        try:
            value = int(context.args[0])
        except ValueError:
            await send_reply(update, f"{label} must be a whole number from {minimum} to {maximum}.")
            return
        if not minimum <= value <= maximum:
            await send_reply(update, f"{label} must be from {minimum} to {maximum}.")
            return
    set_group_settings(update.effective_chat.id, {setting: value})
    shown_value = format_duration(value) if duration else str(value)
    await send_reply(update, f"✅ {label} set to {shown_value}.")


async def set_violations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_admin_setting(update, context, "violation_limit", "Violation limit", 2, 6)


async def set_window(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_admin_setting(
        update, context, "violation_window", "Violation window", 5, MAX_WINDOW_SECONDS, duration=True
    )


async def set_mute_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_admin_setting(
        update, context, "mute_duration", "Mute duration", MIN_MUTE_SECONDS, MAX_MUTE_SECONDS, duration=True
    )


async def set_warning_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_admin_setting(
        update,
        context,
        "warning_duration",
        "Warning auto-delete time",
        MIN_MUTE_SECONDS,
        MAX_WARNING_SECONDS,
        duration=True,
    )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    values = get_group_settings(update.effective_chat.id)
    text = (
        "⚙️ Sticker/GIF guard settings\n"
        f"Message limit: {values['message_limit']} (owner only)\n"
        f"Violations before mute: {values['violation_limit']}\n"
        f"Violation window: {format_duration(values['violation_window'])}\n"
        f"Mute duration: {format_duration(values['mute_duration'])}\n"
        f"Warning auto-delete: {format_duration(values['warning_duration'])}"
    )
    await send_reply(update, text)


def is_restricted_content(message: Message) -> bool:
    return bool(
        message.sticker
        or message.animation
        or (
            message.document
            and (message.document.mime_type or "").lower() == "image/gif"
        )
    )


async def delete_warning(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if job is None:
        return
    try:
        await context.application.bot.delete_message(
            chat_id=job.data["chat_id"], message_id=job.data["message_id"]
        )
    except BadRequest as error:
        LOGGER.debug("Could not delete expired warning: %s", error)
    else:
        clear_pending_warning(job.data["chat_id"], job.data["message_id"])
        return
    clear_pending_warning(job.data["chat_id"], job.data["message_id"])


async def send_warning(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    duration: int,
) -> None:
    user = update.effective_user
    if user is None or update.effective_message is None:
        return
    mention = f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'
    sent = await context.application.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"{mention}, {text}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    if context.job_queue is None:
        raise RuntimeError("The Telegram JobQueue is unavailable; install python-telegram-bot[job-queue].")
    delete_at = time.time() + duration
    save_pending_warning(sent.chat_id, sent.message_id, delete_at)
    context.job_queue.run_once(
        delete_warning,
        when=max(0.1, delete_at - time.time()),
        data={"chat_id": sent.chat_id, "message_id": sent.message_id},
        name=f"warning-{sent.chat_id}-{sent.message_id}",
    )


async def mute_member(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings_data: dict[str, Any],
    violation_count: int,
) -> None:
    until = datetime.now(timezone.utc).timestamp() + settings_data["mute_duration"]
    await context.application.bot.restrict_chat_member(
        chat_id=update.effective_chat.id,
        user_id=update.effective_user.id,
        permissions=ChatPermissions(
            can_send_messages=False,
            can_send_audios=False,
            can_send_documents=False,
            can_send_photos=False,
            can_send_videos=False,
            can_send_video_notes=False,
            can_send_voice_notes=False,
            can_send_polls=False,
            can_send_other_messages=False,
            can_add_web_page_previews=False,
        ),
        until_date=int(until),
    )
    until_datetime = datetime.fromtimestamp(until, timezone.utc)
    set_member_mute(update.effective_chat.id, update.effective_user.id, until_datetime)
    await send_warning(
        update,
        context,
        f"you've been muted for {format_duration(settings_data['mute_duration'])} "
        f"after {violation_count} sticker/GIF violations. Please follow the group rules.",
        settings_data["warning_duration"],
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if (
        message is None
        or user is None
        or user.is_bot
        or not is_group_chat(update)
        or update.message is None
    ):
        return
    if await is_admin(update):
        cache_member_identity(
            update.effective_chat.id, user.id, user.username, user.full_name
        )
        return
    if is_restricted_content(message):
        violation = record_violation(
            update.effective_chat.id, user.id, user.username, user.full_name
        )
        if violation is None:
            return
        settings_data, violation_count = violation
        # Surface permission failures instead of claiming the content was removed.
        await context.application.bot.delete_message(
            chat_id=update.effective_chat.id, message_id=message.message_id
        )
        if violation_count >= settings_data["violation_limit"]:
            await mute_member(update, context, settings_data, violation_count)
        else:
            remaining = settings_data["violation_limit"] - violation_count
            await send_warning(
                update,
                context,
                f"stickers and GIFs are locked until you send "
                f"{settings_data['message_limit']} text messages. "
                f"Violation {violation_count}/{settings_data['violation_limit']}; "
                f"{remaining} more violation(s) in {format_duration(settings_data['violation_window'])} "
                f"will mute you for {format_duration(settings_data['mute_duration'])}.",
                settings_data["warning_duration"],
            )
        return
    if message.text and not message.text.startswith("/"):
        record_text_message(
            update.effective_chat.id, user.id, user.username, user.full_name
        )


async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.new_chat_members or update.effective_chat is None:
        return
    for member in message.new_chat_members:
        if not member.is_bot:
            record_join(update.effective_chat.id, member.id, member.username, member.full_name)


async def refresh_command_scopes(application: Application, chat_id: int) -> None:
    administrators = await application.bot.get_chat_administrators(chat_id)
    owner = next((member.user for member in administrators if member.status == ChatMember.OWNER), None)
    if owner is None:
        LOGGER.error("No owner was returned while configuring command visibility for chat %s", chat_id)
        return

    settings_data = get_group_settings(chat_id)
    old_owner_id = settings_data.get("_command_owner_id")
    await application.bot.set_my_commands(
        MEMBER_COMMANDS, scope=BotCommandScopeChat(chat_id=chat_id)
    )
    await application.bot.set_my_commands(
        ADMIN_COMMANDS, scope=BotCommandScopeChatAdministrators(chat_id=chat_id)
    )
    if old_owner_id and old_owner_id != owner.id:
        old_owner_is_admin = any(
            member.user.id == old_owner_id
            and member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
            for member in administrators
        )
        await application.bot.set_my_commands(
            ADMIN_COMMANDS if old_owner_is_admin else MEMBER_COMMANDS,
            scope=BotCommandScopeChatMember(chat_id=chat_id, user_id=old_owner_id),
        )
    await application.bot.set_my_commands(
        OWNER_COMMANDS,
        scope=BotCommandScopeChatMember(chat_id=chat_id, user_id=owner.id),
    )
    if settings_data.get("_command_owner_id") != owner.id:
        set_group_settings(chat_id, {"_command_owner_id": owner.id})
    LOGGER.info("Configured member/admin/owner command menus for chat %s", chat_id)


async def handle_member_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        return
    if update.my_chat_member:
        status = update.my_chat_member.new_chat_member.status
        if status in (ChatMember.LEFT, ChatMember.BANNED):
            return
    try:
        await refresh_command_scopes(context.application, chat.id)
    except TelegramError:
        LOGGER.exception("Could not refresh command visibility for chat %s", chat.id)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(MEMBER_COMMANDS)
    with closing(connect_database()) as connection:
        rows = connection.execute("SELECT chat_id FROM groups").fetchall()
    for row in rows:
        chat_id = int(row["chat_id"])
        try:
            await refresh_command_scopes(application, chat_id)
        except TelegramError:
            LOGGER.exception("Could not refresh command visibility for chat %s", chat_id)
    if application.job_queue is None:
        raise RuntimeError("The Telegram JobQueue is unavailable; install python-telegram-bot[job-queue].")
    for warning in get_pending_warnings():
        application.job_queue.run_once(
            delete_warning,
            when=max(0.1, warning["delete_at"] - time.time()),
            data={"chat_id": warning["chat_id"], "message_id": warning["message_id"]},
            name=f"warning-{warning['chat_id']}-{warning['message_id']}",
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.error:
        LOGGER.error(
            "Unhandled error while processing update: %s",
            context.error,
            exc_info=(type(context.error), context.error, context.error.__traceback__),
        )
    else:
        LOGGER.error("An update failed without an exception object: %s", update)


def build_application(token: str) -> Application:
    application = Application.builder().token(token).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("rules", rules))
    application.add_handler(CommandHandler("count", count))

    application.add_handler(CommandHandler("check", check))
    application.add_handler(CommandHandler("trust", trust))
    application.add_handler(CommandHandler("untrust", untrust))
    application.add_handler(CommandHandler("trusted", trusted_status))
    application.add_handler(CommandHandler("limit", set_limit))
    application.add_handler(CommandHandler("violations", set_violations))
    application.add_handler(CommandHandler("window", set_window))
    application.add_handler(CommandHandler("mutetime", set_mute_time))
    application.add_handler(CommandHandler("warntime", set_warning_time))
    application.add_handler(CommandHandler("settings", settings))

    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member), group=-1
    )
    ordinary_messages = filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate.ALL
    application.add_handler(MessageHandler(ordinary_messages, handle_message))
    application.add_handler(
        ChatMemberHandler(handle_member_status, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    application.add_handler(
        ChatMemberHandler(handle_member_status, ChatMemberHandler.CHAT_MEMBER)
    )
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not BOT_TOKEN:
        LOGGER.critical("BOT_TOKEN is not set. Add the rotated token to the BOT_TOKEN environment variable.")
        sys.exit(1)
    init_database()
    build_application(BOT_TOKEN).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
