import os, re, sqlite3
import discord
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime
import time
from collections import defaultdict

# ------------------ config ------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("COMMAND_PREFIX", "!")
DB_PATH = "fp.sqlite"
_unauth_attempts = defaultdict(lambda: [0, 0.0])
_MAX_ATTEMPTS = 3
_WINDOW_SECONDS = 60

import os, threading
from aiohttp import web

async def _health(_):
    return web.Response(text="OK")

def _run_health_server():
    app = web.Application()
    app.router.add_get("/", _health)
    port = int(os.getenv("PORT", 8000))
    web.run_app(app, port=port)

# start background HTTP server
threading.Thread(target=_run_health_server, daemon=True).start()

# ------------------ storage ------------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_fp(
            user_id INTEGER PRIMARY KEY,
            fp      INTEGER NOT NULL
        )
    """)
    return con

# ------------------ warning schema upgrade ------------------
def ensure_warning_schema():
    """Make sure the warnings table exists and has at least id/user_id; we'll migrate next."""
    con = db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_warnings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL
        )
    """)
    con.commit()
    con.close()

def migrate_user_warnings_table():
    """
    Migrate any legacy schema that had a NOT NULL 'warnings' column (and maybe no reason/date)
    to the new history table: (id, user_id, reason, date).
    """
    con = db()
    cur = con.execute("PRAGMA table_info(user_warnings)")
    cols = {row[1] for row in cur.fetchall()}

    if "warnings" not in cols:
        con.close()
        return

    today = datetime.utcnow().strftime("%Y-%m-%d")

    # Create a brand-new table with the correct schema
    con.execute("""
        CREATE TABLE IF NOT EXISTS _uw_new(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            reason   TEXT NOT NULL,
            date     TEXT NOT NULL
        )
    """)

    if {"reason", "date"}.issubset(cols):
        # Mixed schema already had reason/date—just copy rows, ignore 'warnings'
        con.execute("""
            INSERT INTO _uw_new (user_id, reason, date)
            SELECT user_id,
                   COALESCE(reason, 'Warning administered'),
                   COALESCE(date, ?)
            FROM user_warnings
        """, (today,))
    else:
        # Very old schema: only user_id + warnings count. Expand counts into history rows.
        cur2 = con.execute("SELECT user_id, warnings FROM user_warnings")
        for user_id, count in cur2.fetchall():
            count = int(count or 0)
            for _ in range(count):
                con.execute(
                    "INSERT INTO _uw_new (user_id, reason, date) VALUES (?, ?, ?)",
                    (user_id, "Migrated from legacy count", today)
                )

    # Replace old table
    con.execute("DROP TABLE user_warnings")
    con.execute("ALTER TABLE _uw_new RENAME TO user_warnings")
    con.commit()
    con.close()

# ------------------ FP functions ------------------
def get_fp(user_id: int) -> int:
    con = db()
    cur = con.execute("SELECT fp FROM user_fp WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else 0

def set_fp(user_id: int, value: int) -> None:
    con = db()
    con.execute("INSERT INTO user_fp(user_id, fp) VALUES(?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET fp=excluded.fp",
                (user_id, value))
    con.commit()
    con.close()

# ------------------ role helpers ------------------
fp_role_pattern = re.compile(r"^\d+\s*FP$", re.IGNORECASE)

def is_fp_role(role: discord.Role) -> bool:
    return fp_role_pattern.match(role.name or "") is not None

async def sync_member_fp_role(member: discord.Member, new_fp: int):
    roles_to_remove = [r for r in member.roles if is_fp_role(r)]
    if roles_to_remove:
        try:
            await member.remove_roles(*roles_to_remove, reason="FP update")
        except discord.Forbidden:
            pass

    role_name = f"{new_fp} FP"
    target_role = discord.utils.get(member.guild.roles, name=role_name)
    if target_role:
        try:
            await member.add_roles(target_role, reason=f"Set to {new_fp} FP")
        except discord.Forbidden:
            pass
        return True
    return False

# ------------------ bot setup ------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# Make sure warnings table is ready before using
ensure_warning_schema()
migrate_user_warnings_table()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")
    print("Ready to manage FP roles!")

def manage_roles_only():
    async def predicate(ctx):
        perms = ctx.author.guild_permissions
        has_perm = perms.manage_roles or perms.administrator

        # If they do have permission, reset their counter and allow
        if has_perm:
            _unauth_attempts[ctx.author.id] = [0, 0.0]
            return True

        # No permission: update attempt counter within time window
        now = time.time()
        count, last_ts = _unauth_attempts[ctx.author.id]

        # Reset if last attempt was outside the window
        if now - last_ts > _WINDOW_SECONDS:
            count = 0

        count += 1
        _unauth_attempts[ctx.author.id] = [count, now]

        if count >= _MAX_ATTEMPTS:
            # Escalated message on 3rd+ attempt within the window
            await ctx.reply("Hey guys, please stop spamming the commands if you aren’t powerful enough to use them. This is your final warning and i’m being serious guys thanks.", mention_author=False)
        else:
            # Your default message
            await ctx.reply("Hey guys, please stop trying to use commands that you can’t use thanks guys!", mention_author=False)

        return False
    return commands.check(predicate)

# ------------------ FP commands ------------------
@bot.command(name="fp", help="Adjust FP: !fp @user +5 or !fp @user -3")
@manage_roles_only()
async def fp(ctx, member: discord.Member, delta: int):
    current = get_fp(member.id)
    new = current + delta
    set_fp(member.id, new)
    existed = await sync_member_fp_role(member, new)
    note = "" if existed else f" *(role `{new} FP` not found)*"
    await ctx.reply(f"{member.mention} is now **{new} FP**.{note}", mention_author=False)

@bot.command(name="fpset", help="Set FP exactly: !fpset @user 109")
@manage_roles_only()
async def fpset(ctx, member: discord.Member, value: int):
    set_fp(member.id, value)
    existed = await sync_member_fp_role(member, value)
    note = "" if existed else f" *(role `{value} FP` not found)*"
    await ctx.reply(f"{member.mention} set to **{value} FP**.{note}", mention_author=False)

from typing import Optional

@bot.command(name="fpcheck", help="Show FP: !fpcheck [@user]")
async def fpcheck(ctx, member: Optional[discord.Member] = None):
    # Default to the caller if no user mentioned
    member = member or ctx.author
    await ctx.reply(f"{member.mention} has **{get_fp(member.id)} FP**.", mention_author=False)

@bot.command(name="fprolesync", help="Resync a member's FP -> role")
@manage_roles_only()
async def fprolesync(ctx, member: discord.Member):
    value = get_fp(member.id)
    existed = await sync_member_fp_role(member, value)
    note = "" if existed else f" *(role `{value} FP` not found)*"
    await ctx.reply(f"Synchronized {member.mention} to **{value} FP**.{note}", mention_author=False)

@bot.command(name="fpall", help="(Admin) Adjust FP for ALL members: !fpall +5 or !fpall -2")
@manage_roles_only()
async def fpall(ctx, delta: int):
    updated = 0
    skipped_bots = 0
    failed_syncs = 0

    # Iterate current guild members
    for member in ctx.guild.members:
        if member.bot:
            skipped_bots += 1
            continue
        try:
            current = get_fp(member.id)
            new = current + delta
            set_fp(member.id, new)

            # Try to sync role (if the matching role exists)
            existed = await sync_member_fp_role(member, new)
            if not existed:
                failed_syncs += 1
            updated += 1
        except Exception:
            # If anything weird happens with one member, just continue
            continue

    # Summary
    msg = f"Applied **{delta:+d} FP** to **{updated}** members."
    if failed_syncs:
        msg += f"\nNote: **{failed_syncs}** member had no matching FP role (e.g., `123 FP`)."

    await ctx.reply(msg, mention_author=False)

@bot.command(name="fptop", help="Show the top 5 users with the highest FP")
async def fptop(ctx):
    con = db()
    cur = con.execute("""
        SELECT user_id, fp
        FROM user_fp
        ORDER BY fp DESC, user_id ASC
        LIMIT 5
    """)
    rows = cur.fetchall()
    con.close()

    if not rows:
        await ctx.send("No FP data found.")
        return

    lines = []
    for i, (user_id, fp) in enumerate(rows, start=1):
        member = ctx.guild.get_member(user_id)
        name = member.display_name if member else f"User ID {user_id}"
        lines.append(f"**{i}.** {name} — **{fp} FP**")

    await ctx.send("__**Top FP Havers**__\n" + "\n".join(lines))

# ------------------ warning system ------------------
def add_warning(user_id: int, reason: str):
    con = db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_warnings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            date TEXT NOT NULL
        )
    """)
    con.execute("""
        INSERT INTO user_warnings (user_id, reason, date)
        VALUES (?, ?, ?)
    """, (user_id, reason, datetime.utcnow().strftime("%Y-%m-%d")))
    con.commit()
    con.close()

def get_all_warnings(user_id: int):
    con = db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_warnings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            date TEXT NOT NULL
        )
    """)
    cur = con.execute("""
        SELECT reason, date FROM user_warnings
        WHERE user_id=?
        ORDER BY id ASC
    """, (user_id,))
    rows = cur.fetchall()
    con.close()
    return rows

def clear_warnings(user_id: int) -> int:
    """Delete all warnings for a user. Returns number of rows deleted."""
    con = db()
    cur = con.execute("SELECT COUNT(*) FROM user_warnings WHERE user_id=?", (user_id,))
    (count,) = cur.fetchone()
    con.execute("DELETE FROM user_warnings WHERE user_id=?", (user_id,))
    con.commit()
    con.close()
    return count

def remove_last_warning(user_id: int) -> bool:
    """Delete the most recent warning (highest id) for a user. Returns True if one was deleted."""
    con = db()
    cur = con.execute("""
        SELECT id FROM user_warnings
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    if not row:
        con.close()
        return False
    (last_id,) = row
    con.execute("DELETE FROM user_warnings WHERE id=?", (last_id,))
    con.commit()
    con.close()
    return True

@bot.command(name="warn", help="Warn a user with optional reason: !warn @user [reason]")
@manage_roles_only()
async def warn(ctx, member: discord.Member, *, reason: str = None):
    if not reason:
        reason = "Warning administered"

    add_warning(member.id, reason)
    total_warnings = len(get_all_warnings(member.id))

    GENERAL_CHANNEL_ID = 1115086270780145728  # Replace with your general channel ID
    general_channel = bot.get_channel(GENERAL_CHANNEL_ID)

    if reason != "Warning administered":
        warn_message = (
            f"{member.mention} you have been warned for {reason}. "
            f"You are at {total_warnings} warnings."
        )
    else:
        warn_message = (
            f"{member.mention} you have been warned. "
            f"You are at {total_warnings} warnings."
        )

    if general_channel:
        await general_channel.send(warn_message)
    else:
        await ctx.send(f"Could not find the general channel. Warning recorded: {warn_message}")

@bot.command(name="warncheck", help="Check warnings: !warncheck [@user]")
async def warncheck(ctx, member: Optional[discord.Member] = None):
    # Default to the caller if no user mentioned
    member = member or ctx.author

    warnings = get_all_warnings(member.id)
    total = len(warnings)

    if total == 0:
        await ctx.send(f"{member.mention} has no warnings.")
        return

    warning_list = "\n".join([f"- [{date}] {reason}" for reason, date in warnings])
    message = f"{member.mention} has been warned {total} times:\n{warning_list}"
    await ctx.send(message)

@bot.command(name="warnclear", help="(Admin) Clear ALL warnings: !warnclear @user")
@manage_roles_only()
async def warnclear(ctx, member: discord.Member):
    removed = clear_warnings(member.id)
    if removed == 0:
        await ctx.reply(f"{member.mention} has no warnings to clear.", mention_author=False)
    else:
        await ctx.reply(f"Cleared **{removed}** warnings for {member.mention}.", mention_author=False)

@bot.command(name="warnsub", help="(Admin) Remove most recent warning: !warnsub @user")
@manage_roles_only()
async def warnsub(ctx, member: discord.Member):
    ok = remove_last_warning(member.id)
    if not ok:
        await ctx.reply(f"{member.mention} has no warnings to remove.", mention_author=False)
        return

    # show updated count for confirmation
    total = len(get_all_warnings(member.id))
    await ctx.reply(
        f"Removed most recent warning for {member.mention}. Now at **{total}** warnings.",
        mention_author=False
    )

@bot.command(name="warntop", help="Show the top 5 users with the most warnings")
async def warntop(ctx):
    con = db()
    cur = con.execute("""
        SELECT user_id, COUNT(*) AS total
        FROM user_warnings
        GROUP BY user_id
        ORDER BY total DESC, user_id ASC
        LIMIT 5
    """)
    rows = cur.fetchall()
    con.close()

    if not rows:
        await ctx.send("No warnings recorded.")
        return

    lines = []
    for i, (user_id, total) in enumerate(rows, start=1):
        member = ctx.guild.get_member(user_id)
        name = member.display_name if member else f"User ID {user_id}"
        lines.append(f"**{i}.** {name} — **{total} warnings**")

    await ctx.send("__**Top Warning Havers**__\n" + "\n".join(lines))

@bot.command(name="com", help="Show available commands (context-aware help)")
async def com(ctx):
    # Detect admin/mod (same rule as your manage_roles_only check)
    is_admin = ctx.author.guild_permissions.manage_roles or ctx.author.guild_permissions.administrator
    prefix = getattr(ctx, "prefix", PREFIX)

    # Public commands
    public_cmds = [
        (f"{prefix}fpcheck @user", "Show a member's FP"),
        (f"{prefix}fptop", "Show top FP havers"),
        (f"{prefix}warncheck @user", "Show member's warning history"),
        (f"{prefix}warntop", "Show top warning havers"),
        (f"{prefix}com", "Help command"),
    ]

    # Admin/mod-only commands
    admin_cmds = [
        (f"{prefix}fp @user +/-N", "Add or subtract FP from a member"),
        (f"{prefix}fpset @user N", "Set member FP to a value"),
        (f"{prefix}fprolesync @user", "Sync FP and role"),
        (f"{prefix}fpall +/-N", "Add or subtract FP for ALL members"),
        (f"{prefix}warn @user [reason]", "Issue a warning"),
        (f"{prefix}warnsub @user", "Remove the most recent warning from a member"),
        (f"{prefix}warnclear @user", "Clear ALL warnings for a member"),
    ]

    # Build the message based on permissions
    if is_admin:
        title = "__**Commands**__"
        lines = ["**Public:**"] + [f"- `{c}` — {d}" for c, d in public_cmds]
        lines += ["", "**Admin:**"] + [f"- `{c}` — {d}" for c, d in admin_cmds]
    else:
        title = "__**Commands**__"
        lines = [f"- `{c}` — {d}" for c, d in public_cmds]

    await ctx.send(f"{title}\n" + "\n".join(lines))

# ------------------ run bot ------------------

@bot.event
async def on_member_remove(member):
    # Remove their FP
    con = db()
    con.execute("DELETE FROM user_fp WHERE user_id = ?", (member.id,))
    # Remove their warnings
    con.execute("DELETE FROM user_warnings WHERE user_id = ?", (member.id,))
    con.commit()
    con.close()

    print(f"Removed FP and warnings for {member} ({member.id}) because they left or were kicked.")

bot.run(TOKEN)
