from telegram import Update, ChatPermissions, ChatMember
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime, timedelta
import json
import os

# ==================== CONFIGURATION ====================
BOT_TOKEN = "8621256430:AAG7RhMxO-CoJzu_i83lQSygMYqZGdz0U7M"  # Your bot token
DB_FILE = "database.json"

# ==================== DATABASE FUNCTIONS ====================
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    return {"groups": {}}

def save_db(data):
    with open(DB_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def get_group_data(db, chat_id):
    chat_id = str(chat_id)
    if chat_id not in db["groups"]:
        db["groups"][chat_id] = {
            "restriction_mode": "message",  # "time" or "message"
            "restriction_value": 500,       # 500 messages or "3d"
            "violation_limit": 3,
            "violation_window": 300,        # 5 minutes in seconds
            "mute_duration": 3600,          # 1 hour in seconds
            "members": {},
            "trusted": []
        }
    return db["groups"][chat_id]

def get_member_data(group_data, user_id):
    user_id = str(user_id)
    if user_id not in group_data["members"]:
        group_data["members"][user_id] = {
            "join_time": datetime.now().isoformat(),
            "message_count": 0,
            "violations": [],
            "mute_until": None,
            "is_unrestricted": False
        }
    return group_data["members"][user_id]

# ==================== HELPER FUNCTIONS ====================
def parse_duration(duration_str):
    try:
        value = int(duration_str[:-1])
        unit = duration_str[-1].lower()
        if unit == 'm':
            return value * 60
        elif unit == 'h':
            return value * 3600
        elif unit == 'd':
            return value * 86400
        else:
            return None
    except:
        return None

def format_duration(seconds):
    if seconds >= 86400:
        return f"{seconds // 86400} days"
    elif seconds >= 3600:
        return f"{seconds // 3600} hours"
    elif seconds >= 60:
        return f"{seconds // 60} minutes"
    else:
        return f"{seconds} seconds"

async def is_admin(update: Update) -> bool:
    user = await update.effective_chat.get_member(update.effective_user.id)
    return user.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]

async def is_trusted(group_data, user_id):
    return str(user_id) in group_data["trusted"] or str(user_id) in [str(m) for m in group_data["trusted"]]

async def is_restricted(update: Update, group_data, member_data):
    if member_data["mute_until"]:
        mute_until = datetime.fromisoformat(member_data["mute_until"])
        if datetime.now() < mute_until:
            return True, "muted"
        else:
            member_data["mute_until"] = None

    if member_data["is_unrestricted"]:
        return False, None

    if group_data["restriction_mode"] == "time":
        join_time = datetime.fromisoformat(member_data["join_time"])
        restriction_seconds = parse_duration(str(group_data["restriction_value"]))
        if datetime.now() < join_time + timedelta(seconds=restriction_seconds):
            return True, "time"
        else:
            return False, None
    else:
        if member_data["message_count"] < group_data["restriction_value"]:
            return True, "message"
        else:
            return False, None

async def mute_user(app, chat_id, user_id, duration_seconds):
    permissions = ChatPermissions(
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
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
    )

    until_date = datetime.now() + timedelta(seconds=duration_seconds)
    await app.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=permissions,
        until_date=int(until_date.timestamp())
    )
    return until_date

async def delete_message(app, chat_id, message_id):
    try:
        await app.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass

# ==================== COMMAND HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to NewMemberGuardBot!\n\n"
        "I protect groups from sticker/GIF spam by new members.\n\n"
        "📋 Commands:\n"
        "/help - Show all commands\n"
        "/mycount - Check your message count\n"
        "/rules - View group rules\n\n"
        "Add me to your group and make me admin to start!"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    group_data = get_group_data(db, update.effective_chat.id)

    admin_commands = """
👮 Admin Commands:
/setrestriction <duration> - Set time restriction (e.g., 3d)
/setmsglimit <number> - Set message limit (e.g., 500)
/setviolationlimit <number> - Set violation threshold (e.g., 3)
/setviolationwindow <duration> - Set violation window (e.g., 5m)
/setmutetime <duration> - Set mute duration (e.g., 1h)
/checkcount @username - Check member's count
/trust @username - Trust a member
/untrust @username - Remove trust
/trusted - List trusted members
/settings - View bot settings
/reset @username - Reset member's count
/unmute @username - Unmute a user""" if await is_admin(update) else ""

    await update.message.reply_text(
        "📋 Available Commands:\n\n"
        "👤 Member Commands:\n"
        "/start - Welcome message\n"
        "/help - This help message\n"
        "/mycount - Check your message count\n"
        "/rules - View group rules\n" +
        admin_commands +
        f"\n\n⚙️ Current Settings:\n"
        f"• Mode: {group_data['restriction_mode']}\n"
        f"• Restriction: {group_data['restriction_value']}\n"
        f"• Violation limit: {group_data['violation_limit']}\n"
        f"• Violation window: {format_duration(group_data['violation_window'])}\n"
        f"• Mute duration: {format_duration(group_data['mute_duration'])}"
    )

async def mycount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    group_data = get_group_data(db, update.effective_chat.id)
    member_data = get_member_data(group_data, update.effective_user.id)

    if group_data["restriction_mode"] == "message":
        limit = group_data["restriction_value"]
        count = member_data["message_count"]
        remaining = max(0, limit - count)

        if count >= limit:
            await update.message.reply_text(
                "✅ Congratulations!\n\n"
                f"You have sent {count}/{limit} messages.\n"
                f"You can now send stickers and GIFs! 🎉"
            )
        else:
            await update.message.reply_text(
                f"📊 Your Message Count:\n\n"
                f"Sent: {count}/{limit} messages\n"
                f"Remaining: {remaining} messages\n\n"
                f"⚠️ You cannot send stickers/GIFs yet."
            )
    else:
        join_time = datetime.fromisoformat(member_data["join_time"])
        restriction_value = str(group_data["restriction_value"])
        restriction_seconds = parse_duration(restriction_value)
        unlock_time = join_time + timedelta(seconds=restriction_seconds)

        if datetime.now() >= unlock_time:
            await update.message.reply_text(
                "✅ Your restriction period has ended!\n"
                "You can now send stickers and GIFs! 🎉"
            )
        else:
            remaining = unlock_time - datetime.now()
            await update.message.reply_text(
                f"⏳ Time-Based Restriction:\n\n"
                f"Join time: {join_time.strftime('%Y-%m-%d %H:%M')}\n"
                f"Unlocks in: {format_duration(int(remaining.total_seconds()))}\n\n"
                f"⚠️ You cannot send stickers/GIFs yet."
            )

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show group rules – simple and readable"""
    db = load_db()
    group_data = get_group_data(db, update.effective_chat.id)

    mode_text = (
        f"Wait {group_data['restriction_value']} after joining."
        if group_data['restriction_mode'] == 'time'
        else f"Send {group_data['restriction_value']} text messages."
    )

    await update.message.reply_text(
        "📜 Group Rules\n\n"
        "1️⃣ New members cannot send stickers, GIFs, or media until they unlock.\n"
        "2️⃣ To unlock, you must: " + mode_text + "\n"
        "3️⃣ Sending too many restricted messages quickly will get you muted.\n"
        "4️⃣ Be respectful and follow Telegram's Terms of Service."
    )

# ==================== ADMIN COMMANDS ====================
async def setrestriction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("❌ You are not an admin.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /setrestriction <duration> (e.g., 3d)")
        return

    duration = context.args[0].lower()
    seconds = parse_duration(duration)

    if seconds and seconds >= 86400:
        db = load_db()
        group_data = get_group_data(db, update.effective_chat.id)
        group_data["restriction_mode"] = "time"
        group_data["restriction_value"] = duration
        save_db(db)

        await update.message.reply_text(f"✅ Time-based restriction set to {duration}")
    else:
        await update.message.reply_text("❌ Duration must be at least 1 day (e.g., 1d, 2d, 7d)")

async def setmsglimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("❌ You are not an admin.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /setmsglimit <number> (100-1000)")
        return

    try:
        limit = int(context.args[0])
        if 100 <= limit <= 1000:
            db = load_db()
            group_data = get_group_data(db, update.effective_chat.id)
            group_data["restriction_mode"] = "message"
            group_data["restriction_value"] = limit
            save_db(db)

            await update.message.reply_text(f"✅ Message limit set to {limit} messages")
        else:
            await update.message.reply_text("❌ Limit must be between 100 and 1000")
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid number")

async def setviolationlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("❌ You are not an admin.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /setviolationlimit <number> (2-10)")
        return

    try:
        limit = int(context.args[0])
        if 2 <= limit <= 10:
            db = load_db()
            group_data = get_group_data(db, update.effective_chat.id)
            group_data["violation_limit"] = limit
            save_db(db)

            await update.message.reply_text(f"✅ Violation limit set to {limit}")
        else:
            await update.message.reply_text("❌ Limit must be between 2 and 10")
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid number")

async def setviolationwindow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("❌ You are not an admin.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /setviolationwindow <duration> (e.g., 5m, 10m)")
        return

    duration = context.args[0].lower()
    seconds = parse_duration(duration)

    if seconds and 60 <= seconds <= 3600:
        db = load_db()
        group_data = get_group_data(db, update.effective_chat.id)
        group_data["violation_window"] = seconds
        save_db(db)

        await update.message.reply_text(f"✅ Violation window set to {format_duration(seconds)}")
    else:
        await update.message.reply_text("❌ Window must be between 1 minute and 1 hour")

async def setmutetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("❌ You are not an admin.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /setmutetime <duration> (e.g., 5m, 1h, 3d)")
        return

    duration = context.args[0].lower()
    seconds = parse_duration(duration)

    if seconds and 300 <= seconds <= 259200:
        db = load_db()
        group_data = get_group_data(db, update.effective_chat.id)
        group_data["mute_duration"] = seconds
        save_db(db)

        await update.message.reply_text(f"✅ Mute duration set to {format_duration(seconds)}")
    else:
        await update.message.reply_text("❌ Duration must be between 5 minutes and 3 days")

async def checkcount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("❌ You are not an admin.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /checkcount @username")
        return

    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "mention":
                await update.message.reply_text(
                    "⚠️ Username resolution requires database lookup. Use user ID instead: /checkcount 123456789"
                )
                return

    try:
        user_id = int(context.args[0].replace('@', ''))
        db = load_db()
        group_data = get_group_data(db, update.effective_chat.id)
        member_data = get_member_data(group_data, user_id)

        count = member_data["message_count"]
        limit = group_data["restriction_value"]

        await update.message.reply_text(
            f"📊 User {user_id}:\n\n"
            f"Messages: {count}/{limit}\n"
            f"Remaining: {max(0, limit - count)}"
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID")

async def trust(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("❌ You are not an admin.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /trust @username or /trust user_id")
        return

    user_identifier = context.args[0].replace('@', '')

    db = load_db()
    group_data = get_group_data(db, update.effective_chat.id)

    if user_identifier not in group_data["trusted"]:
        group_data["trusted"].append(user_identifier)
        save_db(db)

        try:
            user_id = int(user_identifier)
            member_data = get_member_data(group_data, user_id)
            member_data["is_unrestricted"] = True
            save_db(db)
        except:
            pass

        await update.message.reply_text(f"✅ User {user_identifier} is now trusted")
    else:
        await update.message.reply_text(f"⚠️ User {user_identifier} is already trusted")

async def untrust(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("❌ You are not an admin.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /untrust @username or /untrust user_id")
        return

    user_identifier = context.args[0].replace('@', '')

    db = load_db()
    group_data = get_group_data(db, update.effective_chat.id)

    if user_identifier in group_data["trusted"]:
        group_data["trusted"].remove(user_identifier)
        save_db(db)

        try:
            user_id = int(user_identifier)
            member_data = get_member_data(group_data, user_id)
            member_data["is_unrestricted"] = False
            save_db(db)
        except:
            pass

        await update.message.reply_text(f"✅ User {user_identifier} is no longer trusted")
    else:
        await update.message.reply_text(f"⚠️ User {user_identifier} is not trusted")

async def trusted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("❌ You are not an admin.")
        return

    db = load_db()
    group_data = get_group_data(db, update.effective_chat.id)

    if group_data["trusted"]:
        await update.message.reply_text(
            f"👥 Trusted Members ({len(group_data['trusted'])}):\n\n" +
            "\n".join([f"• {uid}" for uid in group_data["trusted"]])
        )
    else:
        await update.message.reply_text("No trusted members")

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("❌ You are not an admin.")
        return

    db = load_db()
    group_data = get_group_data(db, update.effective_chat.id)

    await update.message.reply_text(
        f"⚙️ Bot Settings:\n\n"
        f"📌 Restriction Mode: {group_data['restriction_mode']}\n"
        f"📌 Restriction Value: {group_data['restriction_value']}\n"
        f"📌 Violation Limit: {group_data['violation_limit']}\n"
        f"📌 Violation Window: {format_duration(group_data['violation_window'])}\n"
        f"📌 Mute Duration: {format_duration(group_data['mute_duration'])}\n"
        f"📌 Trusted Members: {len(group_data['trusted'])}\n"
        f"📌 Total Members Tracked: {len(group_data['members'])}"
    )

async def reset_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("❌ You are not an admin.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /reset @username or /reset user_id")
        return

    user_identifier = context.args[0].replace('@', '')

    try:
        user_id = int(user_identifier)
        db = load_db()
        group_data = get_group_data(db, update.effective_chat.id)
        member_data = get_member_data(group_data, user_id)
        member_data["message_count"] = 0
        save_db(db)

        await update.message.reply_text(f"✅ Reset message count for user {user_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("❌ You are not an admin.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /unmute user_id")
        return

    try:
        user_id = int(context.args[0])

        permissions = ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_change_info=False,
            can_invite_users=True,
            can_pin_messages=False,
        )

        await context.application.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=user_id,
            permissions=permissions
        )

        db = load_db()
        group_data = get_group_data(db, update.effective_chat.id)
        member_data = get_member_data(group_data, user_id)
        member_data["mute_until"] = None
        save_db(db)

        await update.message.reply_text(f"✅ Unmuted user {user_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

# ==================== MESSAGE HANDLER ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if await is_admin(update):
        return

    db = load_db()
    group_data = get_group_data(db, chat_id)
    member_data = get_member_data(group_data, user_id)

    if await is_trusted(group_data, user_id):
        return

    is_restricted_now, restriction_type = await is_restricted(update, group_data, member_data)

    is_restricted_content = (
        update.message.sticker or
        update.message.animation or
        update.message.photo or
        update.message.video or
        update.message.audio or
        update.message.document or
        update.message.voice
    )

    if is_restricted_content and is_restricted_now:
        await delete_message(context.application, chat_id, update.message.message_id)

        now = datetime.now()
        member_data["violations"].append(now.isoformat())

        window_start = now - timedelta(seconds=group_data["violation_window"])
        member_data["violations"] = [
            v for v in member_data["violations"]
            if datetime.fromisoformat(v) > window_start
        ]

        if len(member_data["violations"]) >= group_data["violation_limit"]:
            mute_until = await mute_user(
                context.application,
                chat_id,
                user_id,
                group_data["mute_duration"]
            )
            member_data["mute_until"] = mute_until.isoformat()
            member_data["violations"] = []
            save_db(db)

            await update.message.reply_text(
                "🚫 You have been temporarily muted.\n\n"
                f"Duration: {format_duration(group_data['mute_duration'])}\n"
                f"Reason: You sent {len(member_data['violations']) + 1} restricted messages (stickers/GIFs/media) "
                f"in {format_duration(group_data['violation_window'])}.\n"
                f"Rule: Maximum allowed is {group_data['violation_limit'] - 1} such messages in that time.\n\n"
                "Please wait until the mute expires."
            )
        else:
            save_db(db)

            if group_data["restriction_mode"] == "message":
                limit = group_data["restriction_value"]
                count = member_data["message_count"]
                remaining = max(0, limit - count)

                await update.message.reply_text(
                    "⚠️ Stickers, GIFs and media are not allowed until you unlock.\n\n"
                    f"📊 You have sent {count}/{limit} messages.\n"
                    f"📍 {remaining} messages remaining.\n\n"
                    f"⚠️ Violation {len(member_data['violations'])}/{group_data['violation_limit']} "
                    f"(in {format_duration(group_data['violation_window'])})"
                )
            else:
                await update.message.reply_text(
                    "⚠️ Stickers, GIFs and media are not allowed until you unlock.\n\n"
                    f"⏳ You must wait {group_data['restriction_value']} after joining.\n\n"
                    f"⚠️ Violation {len(member_data['violations'])}/{group_data['violation_limit']} "
                    f"(in {format_duration(group_data['violation_window'])})"
                )
        return

    if update.message.text and is_restricted_now and restriction_type == "message":
        member_data["message_count"] += 1
        save_db(db)

        if member_data["message_count"] == group_data["restriction_value"]:
            await update.message.reply_text(
                "🎉 Congratulations!\n\n"
                f"You have sent {group_data['restriction_value']} messages.\n"
                "You can now send stickers and GIFs! ✅"
            )

    if update.message.text and not is_restricted_now:
        member_data["message_count"] += 1
        save_db(db)

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return

    for new_member in update.message.new_chat_members:
        if new_member.is_bot:
            continue

        db = load_db()
        group_data = get_group_data(db, update.effective_chat.id)
        member_data = get_member_data(group_data, new_member.id)

        member_data["join_time"] = datetime.now().isoformat()
        member_data["message_count"] = 0
        member_data["violations"] = []
        save_db(db)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Error: {context.error}")

# ==================== MAIN FUNCTION ====================
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mycount", mycount))
    application.add_handler(CommandHandler("rules", rules))

    application.add_handler(CommandHandler("setrestriction", setrestriction))
    application.add_handler(CommandHandler("setmsglimit", setmsglimit))
    application.add_handler(CommandHandler("setviolationlimit", setviolationlimit))
    application.add_handler(CommandHandler("setviolationwindow", setviolationwindow))
    application.add_handler(CommandHandler("setmutetime", setmutetime))
    application.add_handler(CommandHandler("checkcount", checkcount))
    application.add_handler(CommandHandler("trust", trust))
    application.add_handler(CommandHandler("untrust", untrust))
    application.add_handler(CommandHandler("trusted", trusted))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CommandHandler("reset", reset_count))
    application.add_handler(CommandHandler("unmute", unmute))

    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    application.add_error_handler(error_handler)

    print("🤖 Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
