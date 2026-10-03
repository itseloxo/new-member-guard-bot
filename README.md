# New Member Guard Bot

A Telegram group bot that stops new members from sending stickers and GIFs until
they have sent the message count chosen by the group owner. Photos, videos, and
other media are not restricted.

## Setup

1. Create a bot with BotFather and add it to your group as an administrator with
   permission to delete messages and restrict members.
2. Set the environment variable `BOT_TOKEN` to the bot's token. Never put the
   token in source code or commit it.
3. Install dependencies with `python -m pip install -r requirements.txt`.
4. Run `python bot.py`.

The bot stores group settings and member counts in `database.sqlite3`. Set
`DATABASE_PATH` to another path if needed, and use persistent storage for that
file when deploying. Existing `database.json` settings are imported on first
startup. If the token was previously committed, revoke it with BotFather before
deploying this version.

## Commands

Everyone can use `/count` and `/rules`. The bot only shows moderation commands
in the Telegram command menu to group administrators, and shows `/limit` only
to the group owner. Commands are also checked for permissions when run.

| Command | Who can use it | Purpose |
| --- | --- | --- |
| `/count` | Everyone | Check your text-message count |
| `/rules` | Everyone | View the group's sticker/GIF rules |
| `/check` | Admins | Check a member's count; reply to them or provide a username or user ID |
| `/trust`, `/untrust` | Admins | Add or remove a trusted member |
| `/trusted` | Admins | Check whether one member is trusted |
| `/violations 2-6` | Admins | Choose violations before a mute |
| `/window 30s` | Admins | Choose the violation window (5 seconds to 7 days) |
| `/mutetime 1h` | Admins | Choose mute duration (5 seconds to 7 days) |
| `/warntime 30s` | Admins | Choose how long warning messages stay (5 seconds to 1 day) |
| `/settings` | Admins | View the active group settings |
| `/limit 500` | Group owner | Set the required text-message count (50–1000) |

Durations accept seconds, minutes, hours, days, or weeks, including combinations
such as `1d12h`. A username can be resolved after the bot has observed that
member in the group; replying to the member or using their numeric user ID also
works.

Warnings are automatically deleted after the configured time. The bot counts
ordinary text messages, deletes restricted stickers/GIFs, and mutes a member
when they reach the configured violation threshold within the configured
window.
