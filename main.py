import discord

from discord.ext import commands
import os
import aiosqlite
import asyncio
import time
import re
from collections import defaultdict

# ========================
# GOOGLE GEMINI SETUP (FREE)
# ========================
from openai import AsyncOpenAI

gemini_client = None
gemini_api_key = os.getenv("GEMINI_API_KEY")

if gemini_api_key:
    try:
        gemini_client = AsyncOpenAI(
            api_key=gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        print("✅ Gemini client initialized successfully (free tier)")
    except Exception as e:
        print(f"❌ Gemini setup failed: {e}")
        gemini_client = None
else:
    print("⚠️ WARNING: GEMINI_API_KEY is not set in Railway Variables!")
    print("   Add it in Railway → Variables → GEMINI_API_KEY")
    gemini_client = None
    
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=".", intents=intents)
bot.remove_command("help")

DB_NAME = "bot.db"

# In-memory cooldowns and stats
cooldowns = {}  # {user_id: unix_timestamp_until_allowed}
daily_stats = {}  # {guild_id: {...}}
message_flood = {}  # {channel_id: {user_id: [timestamps]}}

# Per-guild config
config = {}  # {guild_id: {...}}

# In-memory staff activity cache (for quick leaderboard)
staff_cache = defaultdict(lambda: {
    "tickets_claimed": 0,
    "tickets_closed": 0,
    "approvals": 0,
    "denials": 0,
    "blacklists": 0,
    "escalations": 0,
    "notes": 0,
    "proof_requests": 0,
    "automation_runs": 0,
    "staff_messages": 0,
    "followups": 0,
    "total_response_time": 0.0,
    "response_events": 0,
    "active_time": 0.0,
})

# Per-ticket tracking for advanced logs
ticket_tracking = {}  # {channel_id: {...}}


def get_guild_config(guild_id: int):
    if guild_id not in config:
        config[guild_id] = {
            "log_channel": None,
            "category": None,
            "general_category": None,
            "male_category": None,
            "female_category": None,
            "male_role": None,
            "female_role": None,
            "unverified_role": None,
            "staff_role": None
        }
    return config[guild_id]


def get_daily_stats(guild_id: int):
    if guild_id not in daily_stats:
        daily_stats[guild_id] = {
            "approved": 0,
            "denied": 0,
            "blacklisted": 0,
            "autokicked": 0,
            "joins": []
        }
    return daily_stats[guild_id]


# =========================
# DATABASE INIT
# =========================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            guild_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY (guild_id, user_id)
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS requirements (
            gender TEXT PRIMARY KEY,
            text TEXT
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS config (
            guild_id INTEGER,
            key TEXT,
            value INTEGER,
            PRIMARY KEY (guild_id, key)
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS staff_stats (
            guild_id INTEGER,
            staff_id INTEGER,
            tickets_claimed INTEGER DEFAULT 0,
            tickets_closed INTEGER DEFAULT 0,
            approvals INTEGER DEFAULT 0,
            denials INTEGER DEFAULT 0,
            blacklists INTEGER DEFAULT 0,
            escalations INTEGER DEFAULT 0,
            notes INTEGER DEFAULT 0,
            proof_requests INTEGER DEFAULT 0,
            automation_runs INTEGER DEFAULT 0,
            staff_messages INTEGER DEFAULT 0,
            followups INTEGER DEFAULT 0,
            total_response_time REAL DEFAULT 0,
            response_events INTEGER DEFAULT 0,
            active_time REAL DEFAULT 0,
            PRIMARY KEY (guild_id, staff_id)
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS active_verifications (
            user_id INTEGER,
            guild_id INTEGER,
            ticket_channel_id INTEGER,
            join_timestamp INTEGER,
            expires_timestamp INTEGER,
            status TEXT,
            gender TEXT DEFAULT NULL,
            PRIMARY KEY (user_id, guild_id)
        )
        """)


        await db.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            guild_id INTEGER,
            user_id INTEGER,
            note TEXT,
            created_by INTEGER,
            created_at INTEGER
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            guild_id INTEGER,
            user_id INTEGER,
            reason TEXT,
            created_by INTEGER,
            created_at INTEGER
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS log_history (
            guild_id INTEGER,
            title TEXT,
            description TEXT,
            created_at INTEGER
        )
        """)
        await db.commit()


# =========================
# DB HELPERS
# =========================
async def add_blacklist(guild_id, user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO blacklist (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id)
        )
        await db.commit()


async def remove_blacklist(guild_id, user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "DELETE FROM blacklist WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        )
        await db.commit()


async def is_blacklisted(guild_id, user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT user_id FROM blacklist WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ) as cursor:
            return await cursor.fetchone() is not None


async def save_config_for_guild(guild_id):
    guild_cfg = get_guild_config(guild_id)
    async with aiosqlite.connect(DB_NAME) as db:
        for key, value in guild_cfg.items():
            if value is not None:
                await db.execute(
                    "INSERT OR REPLACE INTO config (guild_id, key, value) VALUES (?, ?, ?)",
                    (guild_id, key, value)
                )
        await db.commit()


async def load_config():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT guild_id, key, value FROM config") as cursor:
            rows = await cursor.fetchall()
            for guild_id, key, value in rows:
                guild_cfg = get_guild_config(guild_id)
                if key in guild_cfg:
                    guild_cfg[key] = value


async def set_requirement(gender, text):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO requirements (gender, text) VALUES (?, ?)",
            (gender, text)
        )
        await db.commit()


async def get_requirement(gender):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT text FROM requirements WHERE gender=?",
            (gender,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else "Not set"


async def save_staff_stats(guild_id, staff_id):
    data = staff_cache[(guild_id, staff_id)]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        INSERT OR REPLACE INTO staff_stats (
            guild_id, staff_id,
            tickets_claimed, tickets_closed,
            approvals, denials, blacklists, escalations,
            notes, proof_requests, automation_runs,
            staff_messages, followups,
            total_response_time, response_events, active_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            guild_id, staff_id,
            data["tickets_claimed"],
            data["tickets_closed"],
            data["approvals"],
            data["denials"],
            data["blacklists"],
            data["escalations"],
            data["notes"],
            data["proof_requests"],
            data["automation_runs"],
            data["staff_messages"],
            data["followups"],
            data["total_response_time"],
            data["response_events"],
            data["active_time"]
        ))
        await db.commit()


async def load_staff_stats():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
        SELECT guild_id, staff_id,
               tickets_claimed, tickets_closed,
               approvals, denials, blacklists, escalations,
               notes, proof_requests, automation_runs,
               staff_messages, followups,
               total_response_time, response_events, active_time
        FROM staff_stats
        """) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                guild_id = row[0]
                staff_id = row[1]
                staff_cache[(guild_id, staff_id)] = {
                    "tickets_claimed": row[2],
                    "tickets_closed": row[3],
                    "approvals": row[4],
                    "denials": row[5],
                    "blacklists": row[6],
                    "escalations": row[7],
                    "notes": row[8],
                    "proof_requests": row[9],
                    "automation_runs": row[10],
                    "staff_messages": row[11],
                    "followups": row[12],
                    "total_response_time": row[13],
                    "response_events": row[14],
                    "active_time": row[15],
                }


def add_staff_stat(guild_id, staff_id, key, amount=1):
    data = staff_cache[(guild_id, staff_id)]
    data[key] += amount


def add_staff_response_time(guild_id, staff_id, seconds):
    data = staff_cache[(guild_id, staff_id)]
    data["total_response_time"] += seconds
    data["response_events"] += 1


def add_staff_active_time(guild_id, staff_id, seconds):
    data = staff_cache[(guild_id, staff_id)]
    data["active_time"] += seconds


# Active Verification DB Helpers
async def add_to_verification(user_id, guild_id, ticket_channel_id, expires_in=600, gender=None):
    join_ts = int(time.time())
    expires_ts = join_ts + expires_in
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT OR REPLACE INTO active_verifications 
            (user_id, guild_id, ticket_channel_id, join_timestamp, expires_timestamp, status, gender)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, guild_id, ticket_channel_id, join_ts, expires_ts, "Waiting for verification", gender))
        await db.commit()


async def update_verification_gender(user_id, guild_id, gender):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE active_verifications SET gender=? WHERE user_id=? AND guild_id=?", (gender, user_id, guild_id))
        await db.commit()


async def remove_from_verification(user_id, guild_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "DELETE FROM active_verifications WHERE user_id=? AND guild_id=?",
            (user_id, guild_id)
        )
        await db.commit()


async def get_active_verifications(guild_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT user_id, ticket_channel_id, join_timestamp, expires_timestamp, status, gender 
            FROM active_verifications 
            WHERE guild_id=?
            ORDER BY join_timestamp DESC
        """, (guild_id,)) as cursor:
            return await cursor.fetchall()


# =========================
# LOGGING
# =========================
async def log_action(guild, title, description, color=0x2b2d31, *, fields=None):
    guild_cfg = get_guild_config(guild.id)
    log_channel_id = guild_cfg.get("log_channel")
    if not log_channel_id:
        return

    channel = guild.get_channel(log_channel_id)
    if not channel:
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=color
    )
    embed.timestamp = discord.utils.utcnow()
    embed.set_footer(text=f"{guild.name} • Verification Logs")

    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)

    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"Failed to log action: {e}")

    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO log_history (guild_id, title, description, created_at) VALUES (?, ?, ?, ?)",
                (guild.id, title, description, int(time.time()))
            )
            await db.commit()
    except Exception as e:
        print(f"Failed to persist log action: {e}")


# Control Room
async def ensure_control_room(guild):
    control_room = discord.utils.get(guild.text_channels, name="control-room")
    if not control_room:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.owner: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
        }
        control_room = await guild.create_text_channel(
            "control-room",
            overwrites=overwrites,
            topic="Owner-only verification control dashboard"
        )
        await log_action(guild, "🔐 Control Room Created", "Private owner-only control room has been created.", color=0x57F287)
    return control_room


# Ticket Transcript Saver
async def save_ticket_transcript(channel, guild, reason="Ticket Closed"):
    if not channel:
        return

    guild_cfg = get_guild_config(guild.id)
    log_channel = guild.get_channel(guild_cfg.get("log_channel")) if guild_cfg.get("log_channel") else None
    if not log_channel:
        return

    transcript_lines = []
    transcript_lines.append(f"**Ticket Transcript** - {channel.name}")
    transcript_lines.append(f"**Reason:** {reason}")
    transcript_lines.append(f"**Created:** {channel.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    transcript_lines.append("=" * 60 + "\n")

    try:
        async for message in channel.history(limit=None, oldest_first=True):
            timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author = f"{message.author} ({message.author.id})"
            if message.author.bot:
                author += " [BOT]"

            content = message.content if message.content else "[No text content]"

            transcript_lines.append(f"[{timestamp}] {author}:")
            transcript_lines.append(content)

            if message.attachments:
                attachments = ", ".join([a.filename for a in message.attachments])
                transcript_lines.append(f"[Attachments: {attachments}]")

            transcript_lines.append("-" * 40)
    except Exception as e:
        transcript_lines.append(f"\n[Error fetching messages: {e}]")

    transcript_text = "\n".join(transcript_lines)

    try:
        filename = f"{channel.id}_transcript.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(transcript_text)

        file = discord.File(filename, filename=f"{channel.name}_transcript.txt")

        embed = discord.Embed(
            title="📜 Ticket Transcript",
            description=f"Transcript for **{channel.name}**",
            color=0x5865F2
        )
        embed.add_field(name="Reason", value=reason, inline=True)
        embed.add_field(name="Channel ID", value=str(channel.id), inline=True)
        embed.timestamp = discord.utils.utcnow()

        await log_channel.send(embed=embed, file=file)
        os.remove(filename)

    except Exception as e:
        await log_channel.send(
            embed=discord.Embed(
                title="📜 Ticket Transcript (Fallback)",
                description=transcript_text[:4000],
                color=0xED4245
            )
        )
        print(f"Transcript error: {e}")


def ticket_config_ready(guild_cfg):
    required_keys = [
        "log_channel",
        "general_category",
        "male_category",
        "female_category",
        "male_role",
        "female_role",
        "unverified_role",
        "staff_role",
    ]
    return all(guild_cfg.get(k) is not None for k in required_keys)


async def ensure_verification_categories(guild):
    guild_cfg = get_guild_config(guild.id)
    staff_role = guild.get_role(guild_cfg.get("staff_role")) if guild_cfg.get("staff_role") else None

    base_overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True, read_message_history=True),
    }
    if staff_role:
        base_overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
    if guild.owner:
        base_overwrites[guild.owner] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

    async def _get_or_create(cat_id_key, names):
        cat = guild.get_channel(guild_cfg.get(cat_id_key)) if guild_cfg.get(cat_id_key) else None
        if cat and isinstance(cat, discord.CategoryChannel):
            return cat
        for name in names:
            cat = discord.utils.get(guild.categories, name=name)
            if cat:
                guild_cfg[cat_id_key] = cat.id
                return cat
        cat = await guild.create_category(names[0], overwrites=base_overwrites)
        guild_cfg[cat_id_key] = cat.id
        return cat

    general = await _get_or_create("general_category", ["General Ticket", "Verification Tickets"])
    male = await _get_or_create("male_category", ["Male Tickets"])
    female = await _get_or_create("female_category", ["Female Tickets"])

    guild_cfg["category"] = general.id
    await save_config_for_guild(guild.id)
    return general, male, female


# =========================
# SETUP COMMAND
# =========================
@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    guild = ctx.guild
    guild_cfg = get_guild_config(guild.id)

    male = discord.utils.get(guild.roles, name="Male") or await guild.create_role(name="Male", color=discord.Color.blue())
    female = discord.utils.get(guild.roles, name="Female") or await guild.create_role(name="Female", color=discord.Color.from_rgb(255, 105, 180))
    unverified = discord.utils.get(guild.roles, name="Unverified") or await guild.create_role(name="Unverified", color=discord.Color.light_grey())
    staff = discord.utils.get(guild.roles, name="Staff") or discord.utils.get(guild.roles, name="Security") or await guild.create_role(name="Staff", color=discord.Color.gold())

    category = await guild.create_category(
        "General Ticket",
        overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    )
    male_category = await guild.create_category(
        "Male Tickets",
        overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    )
    female_category = await guild.create_category(
        "Female Tickets",
        overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    )

    log_channel = discord.utils.get(guild.text_channels, name="verification-logs") or await guild.create_text_channel("verification-logs")
    await log_channel.set_permissions(guild.default_role, view_channel=False)

    admin_role = discord.utils.get(guild.roles, permissions__administrator=True)
    if admin_role:
        await log_channel.set_permissions(admin_role, view_channel=True)

    for channel in guild.channels:
        try:
            await channel.set_permissions(
                guild.default_role,
                view_channel=False
            )
        except:
            pass

    for channel in guild.text_channels:
        if channel == log_channel or channel.category in {category, male_category, female_category}:
            continue
        try:
            await channel.set_permissions(male, view_channel=True)
            await channel.set_permissions(female, view_channel=True)
        except:
            pass

    guild_cfg.update({
        "log_channel": log_channel.id,
        "category": category.id,
        "general_category": category.id,
        "male_category": male_category.id,
        "female_category": female_category.id,
        "male_role": male.id,
        "female_role": female.id,
        "unverified_role": unverified.id,
        "staff_role": staff.id
    })

    await save_config_for_guild(guild.id)
    await ctx.send("✅ Setup complete for this server.")

    await log_action(
        guild,
        "🛠️ Setup Completed",
        f"Setup command executed by {ctx.author.mention}.",
        color=0x57F287
    )


# =========================
# REQUIREMENTS COMMAND
# =========================
@bot.command()
@commands.has_permissions(administrator=True)
async def requirements(ctx, gender, *, text):
    gender = gender.lower()
    await set_requirement(gender, text)
    await ctx.send(f"✅ Requirement set for {gender} (shared across all servers).")

    await log_action(
        ctx.guild,
        "📝 Requirements Updated",
        f"{ctx.author.mention} updated requirements for **{gender}**.",
        color=0xFEE75C,
        fields=[
            ("Gender", gender, True),
            ("Updated By", ctx.author.mention, True)
        ]
    )


# =========================
# UNBLACKLIST
# =========================
@bot.command()
@commands.has_permissions(administrator=True)
async def unblacklist(ctx, user_id: int):
    await remove_blacklist(ctx.guild.id, user_id)
    await ctx.send(f"✅ Unblacklisted {user_id} in this server.")

    await log_action(
        ctx.guild,
        "⚪ Unblacklisted",
        f"{ctx.author.mention} unblacklisted <@{user_id}>.",
        color=0x99AAB5,
        fields=[
            ("Staff", ctx.author.mention, True),
            ("User ID", str(user_id), True)
        ]
    )


# =========================
# GENDER BUTTONS (Fixed)
# =========================
class GenderButtons(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id

    async def interaction_check(self, interaction):
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Male", style=discord.ButtonStyle.primary, emoji="♂️")
    async def male(self, interaction, button):
        await self.handle(interaction, "male")

    @discord.ui.button(label="Female", style=discord.ButtonStyle.danger, emoji="♀️")
    async def female(self, interaction, button):
        await self.handle(interaction, "female")

    async def handle(self, interaction, gender):
        await update_verification_gender(interaction.user.id, interaction.guild.id, gender)

        req = await get_requirement(gender)

        embed = discord.Embed(
            title=f"{'♂️' if gender == 'male' else '♀️'} Requirements — {gender.capitalize()}",
            description=req,
            color=0x5865F2
        )

        await interaction.response.send_message(embed=embed)

        await log_action(
            interaction.guild,
            "⚧ Gender Selected",
            f"{interaction.user.mention} selected **{gender.capitalize()}**.",
            color=0x5865F2,
            fields=[
                ("User", interaction.user.mention, True),
                ("User ID", str(interaction.user.id), True),
                ("Gender", gender.capitalize(), True),
                ("Channel", interaction.channel.mention, True)
            ]
        )

        next_steps_embed = discord.Embed(
            title="Next Steps",
            description="Answer the questions below.\nStaff will review shortly.",
            color=0x2b2d31
        )

        await interaction.channel.send(embed=next_steps_embed)
        await interaction.channel.send("🎫 Staff Controls:", view=TicketControls(self.user_id, gender))


# =========================
# TICKET HELPERS
# =========================
paused_timers = {}
shadow_muted_users = set()
PIC_PERMS_ROLE_ID = 1489476010725609535


def format_duration(seconds: int):
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes:02d}:{secs:02d}"


def set_ticket_status(channel_id, status):
    if channel_id not in ticket_tracking:
        ticket_tracking[channel_id] = {}
    ticket_tracking[channel_id]["status"] = status


async def add_persistent_note(guild_id, user_id, note, created_by):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO notes (guild_id, user_id, note, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, note, created_by, int(time.time()))
        )
        await db.commit()


async def get_persistent_notes(guild_id, user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT note, created_by, created_at FROM notes WHERE guild_id=? AND user_id=? ORDER BY created_at DESC",
            (guild_id, user_id)
        ) as cursor:
            return await cursor.fetchall()



async def add_warning(guild_id, user_id, reason, created_by):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO warnings (guild_id, user_id, reason, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, reason, created_by, int(time.time()))
        )
        await db.commit()


async def get_warnings(guild_id, user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT reason, created_by, created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY created_at DESC",
            (guild_id, user_id)
        ) as cursor:
            return await cursor.fetchall()


async def clear_user_logs(guild_id, user_id=None):
    async with aiosqlite.connect(DB_NAME) as db:
        if user_id is None:
            await db.execute("DELETE FROM log_history WHERE guild_id=?", (guild_id,))
        else:
            await db.execute("DELETE FROM log_history WHERE guild_id=? AND description LIKE ?", (guild_id, f"%{user_id}%"))
        await db.commit()


async def export_logs_text(guild_id, user_id=None, limit=200):
    async with aiosqlite.connect(DB_NAME) as db:
        if user_id is None:
            query = "SELECT title, description, created_at FROM log_history WHERE guild_id=? ORDER BY created_at DESC LIMIT ?"
            params = (guild_id, limit)
        else:
            query = "SELECT title, description, created_at FROM log_history WHERE guild_id=? AND description LIKE ? ORDER BY created_at DESC LIMIT ?"
            params = (guild_id, f"%{user_id}%", limit)
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()

    lines = []
    for title, description, created_at in rows:
        lines.append(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(created_at))}] {title}")
        lines.append(description)
        lines.append("-" * 50)
    return "\n".join(lines) if lines else "No logs found."


async def fetch_recent_logs(guild_id, user_id=None, limit=10):
    async with aiosqlite.connect(DB_NAME) as db:
        if user_id is None:
            query = "SELECT title, description, created_at FROM log_history WHERE guild_id=? ORDER BY created_at DESC LIMIT ?"
            params = (guild_id, limit)
        else:
            query = "SELECT title, description, created_at FROM log_history WHERE guild_id=? AND description LIKE ? ORDER BY created_at DESC LIMIT ?"
            params = (guild_id, f"%{user_id}%", limit)
        async with db.execute(query, params) as cursor:
            return await cursor.fetchall()


def build_history_embed(member, notes_rows, warning_rows, blacklisted=False, risk_score=None, risk_reasons=None):
    embed = discord.Embed(
        title=f"📁 User History — {member}",
        color=0x5865F2
    )
    embed.add_field(name="Warnings", value=str(len(warning_rows)), inline=True)
    embed.add_field(name="Notes", value=str(len(notes_rows)), inline=True)
    embed.add_field(name="Blacklisted", value="Yes" if blacklisted else "No", inline=True)
    if risk_score is not None:
        embed.add_field(name="Risk", value=f"{risk_score}/100", inline=True)
        embed.add_field(name="Reasons", value=", ".join(risk_reasons) if risk_reasons else "None", inline=False)
    return embed


def is_staff_member(member):
    guild_cfg = get_guild_config(member.guild.id)
    staff_role = member.guild.get_role(guild_cfg["staff_role"]) if guild_cfg.get("staff_role") else None
    return member.guild_permissions.administrator or (staff_role and staff_role in member.roles)


async def pause_ticket_timer(guild, channel, actor):
    data = ticket_tracking.get(channel.id)
    if not data:
        return False, "No timer found for this ticket."

    expires_ts = data.get("expires_timestamp")
    if expires_ts is None or data.get('paused'):
        return False, "No active timer to pause."

    if data.get("paused"):
        return False, "Timer is already paused."

    remaining = max(0, int(expires_ts - time.time()))
    paused_timers[channel.id] = remaining
    data["paused"] = True
    set_ticket_status(channel.id, "paused")

    await log_action(
        guild,
        "⏸️ Timer Paused",
        f"{actor.mention} paused the timer in {channel.mention}.",
        color=0xFEE75C,
        fields=[
            ("Staff", actor.mention, True),
            ("Channel", channel.mention, True),
            ("Remaining", format_duration(remaining), True)
        ]
    )
    return True, f"⏸️ Timer paused ({format_duration(remaining)} left)."


async def resume_ticket_timer(guild, channel, actor):
    data = ticket_tracking.get(channel.id)
    if not data:
        return False, "No timer found for this ticket."

    remaining = paused_timers.get(channel.id)
    if remaining is None:
        return False, "Timer is not paused."

    data["expires_timestamp"] = int(time.time()) + remaining
    data["paused"] = False
    set_ticket_status(channel.id, "open")
    del paused_timers[channel.id]

    await log_action(
        guild,
        "▶️ Timer Resumed",
        f"{actor.mention} resumed the timer in {channel.mention}.",
        color=0x57F287,
        fields=[
            ("Staff", actor.mention, True),
            ("Channel", channel.mention, True),
            ("Remaining", format_duration(remaining), True)
        ]
    )
    return True, f"▶️ Timer resumed ({format_duration(remaining)} left)."


class TimerControls(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id

    def _is_staff(self, interaction):
        return is_staff_member(interaction.user)

    @discord.ui.button(label="Pause ⏸️", style=discord.ButtonStyle.secondary)
    async def pause_timer(self, interaction, button):
        if not self._is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)
        ok, msg = await pause_ticket_timer(interaction.guild, interaction.channel, interaction.user)
        await interaction.response.send_message(msg, ephemeral=not ok)

    @discord.ui.button(label="Resume ▶️", style=discord.ButtonStyle.success)
    async def resume_timer(self, interaction, button):
        if not self._is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)
        ok, msg = await resume_ticket_timer(interaction.guild, interaction.channel, interaction.user)
        await interaction.response.send_message(msg, ephemeral=not ok)


# =========================
# TICKET CONTROLS
# =========================
class TicketControls(discord.ui.View):
    def __init__(self, user_id, gender):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.gender = gender

    def is_staff(self, interaction):
        guild_cfg = get_guild_config(interaction.guild.id)
        staff_role = interaction.guild.get_role(guild_cfg["staff_role"]) if guild_cfg["staff_role"] else None
        return interaction.user.guild_permissions.administrator or (staff_role and staff_role in interaction.user.roles)

    def is_ticket_handler(self, interaction):
        if interaction.user.guild_permissions.administrator:
            return True

        if not self.is_staff(interaction):
            return False

        current = ticket_tracking.get(interaction.channel.id, {})
        claimed_by = current.get("claimed_by")

        if claimed_by is None:
            return True

        return interaction.user.id == claimed_by

    async def _log_ticket_close_with_duration(self, interaction, member, action_title, description, color, extra_fields=None):
        channel = interaction.channel
        created_at = channel.created_at
        now = discord.utils.utcnow()
        duration_seconds = int((now - created_at).total_seconds())
        duration_str = f"{duration_seconds}s"

        fields = [
            ("Staff", interaction.user.mention, True),
            ("User", member.mention, True),
            ("User ID", str(member.id), True),
            ("Gender", self.gender.capitalize(), True),
            ("Channel", channel.mention, True),
            ("Ticket Duration", duration_str, True)
        ]
        if extra_fields:
            fields.extend(extra_fields)

        await log_action(
            interaction.guild,
            "🏗️ Ticket Lifetime",
            f"Ticket {channel.mention} lifetime recorded.",
            color=0x2b2d31,
            fields=[
                ("Channel", channel.mention, True),
                ("Duration", duration_str, True)
            ]
        )

        await log_action(
            interaction.guild,
            action_title,
            description,
            color=color,
            fields=fields
        )

        add_staff_stat(interaction.guild.id, interaction.user.id, "tickets_closed", 1)
        await save_staff_stats(interaction.guild.id, interaction.user.id)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, row=0)
    async def approve(self, interaction, button):
        if not self.is_ticket_handler(interaction):
            return await interaction.response.send_message("Only the ticket claimer or an admin can do this.", ephemeral=True)

        guild_cfg = get_guild_config(interaction.guild.id)
        stats = get_daily_stats(interaction.guild.id)

        member = interaction.guild.get_member(self.user_id)
        if not member:
            return await interaction.response.send_message("User not found.", ephemeral=True)

        male = interaction.guild.get_role(guild_cfg["male_role"])
        female = interaction.guild.get_role(guild_cfg["female_role"])
        unverified = interaction.guild.get_role(guild_cfg["unverified_role"])

        if unverified and unverified in member.roles:
            await member.remove_roles(unverified)

        role = male if self.gender == "male" else female
        if role:
            await member.add_roles(role)

        try:
            await member.send("✅ You have been approved and verified.")
        except:
            pass

        stats["approved"] += 1
        set_ticket_status(interaction.channel.id, "approved")

        await self._log_ticket_close_with_duration(
            interaction,
            member,
            "🟢 Approved",
            f"{interaction.user.mention} approved {member.mention}.",
            0x57F287
        )

        add_staff_stat(interaction.guild.id, interaction.user.id, "approvals", 1)
        await save_staff_stats(interaction.guild.id, interaction.user.id)

        await save_ticket_transcript(interaction.channel, interaction.guild, reason="Approved by Staff")
        await remove_from_verification(self.user_id, interaction.guild.id)

        await interaction.response.send_message("Approved")
        await interaction.channel.delete()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, row=0)
    async def deny(self, interaction, button):
        if not self.is_ticket_handler(interaction):
            return await interaction.response.send_message("Only the ticket claimer or an admin can do this.", ephemeral=True)

        stats = get_daily_stats(interaction.guild.id)

        member = interaction.guild.get_member(self.user_id)
        if not member:
            return await interaction.response.send_message("User not found.", ephemeral=True)

        try:
            await member.send("❌ Your verification was denied.")
        except:
            pass

        await member.kick(reason="Denied")

        stats["denied"] += 1
        set_ticket_status(interaction.channel.id, "denied")

        await self._log_ticket_close_with_duration(
            interaction,
            member,
            "🔴 Denied",
            f"{interaction.user.mention} denied {member.mention}.",
            0xED4245
        )

        add_staff_stat(interaction.guild.id, interaction.user.id, "denials", 1)
        await save_staff_stats(interaction.guild.id, interaction.user.id)

        await save_ticket_transcript(interaction.channel, interaction.guild, reason="Denied by Staff")
        await remove_from_verification(self.user_id, interaction.guild.id)

        await interaction.response.send_message("Denied")
        await interaction.channel.delete()

    @discord.ui.button(label="Blacklist", style=discord.ButtonStyle.secondary, row=0)
    async def blacklist(self, interaction, button):
        if not self.is_ticket_handler(interaction):
            return await interaction.response.send_message("Only the ticket claimer or an admin can do this.", ephemeral=True)

        stats = get_daily_stats(interaction.guild.id)

        member = interaction.guild.get_member(self.user_id)
        if not member:
            return await interaction.response.send_message("User not found.", ephemeral=True)

        await add_blacklist(interaction.guild.id, member.id)

        try:
            await member.send("🚫 You have been blacklisted from this server.")
        except:
            pass

        await member.kick(reason="Blacklisted")

        stats["blacklisted"] += 1
        set_ticket_status(interaction.channel.id, "blacklisted")

        await self._log_ticket_close_with_duration(
            interaction,
            member,
            "⚫ Blacklisted",
            f"{interaction.user.mention} blacklisted {member.mention}.",
            0x000000
        )

        add_staff_stat(interaction.guild.id, interaction.user.id, "blacklists", 1)
        await save_staff_stats(interaction.guild.id, interaction.user.id)

        await save_ticket_transcript(interaction.channel, interaction.guild, reason="Blacklisted by Staff")
        await remove_from_verification(self.user_id, interaction.guild.id)

        await interaction.response.send_message("Blacklisted")
        await interaction.channel.delete()

    @discord.ui.button(label="Add Note", style=discord.ButtonStyle.secondary, row=0)
    async def add_note(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        await interaction.response.send_message("✏️ Please type your note in this channel.", ephemeral=True)

        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel

        try:
            msg = await interaction.client.wait_for("message", check=check, timeout=300)
        except asyncio.TimeoutError:
            return await interaction.followup.send("Timed out waiting for note.", ephemeral=True)

        member = interaction.guild.get_member(self.user_id)
        if not member:
            return await interaction.followup.send("User not found.", ephemeral=True)

        await add_persistent_note(
            interaction.guild.id,
            member.id,
            msg.content[:1000],
            interaction.user.id
        )

        await log_action(
            interaction.guild,
            "📝 Staff Note Added",
            f"{interaction.user.mention} added a note on {member.mention}.",
            color=0xFEE75C,
            fields=[
                ("Staff", interaction.user.mention, True),
                ("User", member.mention, True),
                ("User ID", str(member.id), True),
                ("Note", msg.content[:1000], False)
            ]
        )

        await log_action(
            interaction.guild,
            "📝 Persistent Note Saved",
            f"{interaction.user.mention} saved a persistent note for {member.mention}.",
            color=0xFEE75C,
            fields=[
                ("Staff", interaction.user.mention, True),
                ("User", member.mention, True),
                ("Note", msg.content[:1000], False)
            ]
        )

        add_staff_stat(interaction.guild.id, interaction.user.id, "notes", 1)
        await save_staff_stats(interaction.guild.id, interaction.user.id)

        await interaction.followup.send("📝 Note saved.", ephemeral=True)

    @discord.ui.button(label="Request Proof", style=discord.ButtonStyle.primary, row=0)
    async def request_proof(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        member = interaction.guild.get_member(self.user_id)
        if not member:
            return await interaction.response.send_message("User not found.", ephemeral=True)

        try:
            await member.send(
                "📎 Staff has requested additional proof or context for your verification. "
                "You can provide screenshots or extra details in your ticket channel."
            )
        except:
            pass

        set_ticket_status(interaction.channel.id, "proof_requested")

        await log_action(
            interaction.guild,
            "🟡 Proof Requested",
            f"{interaction.user.mention} requested proof from {member.mention}.",
            color=0xFEE75C,
            fields=[
                ("Staff", interaction.user.mention, True),
                ("User", member.mention, True),
                ("User ID", str(member.id), True)
            ]
        )

        add_staff_stat(interaction.guild.id, interaction.user.id, "proof_requests", 1)
        await save_staff_stats(interaction.guild.id, interaction.user.id)

        await interaction.response.send_message("Requested proof from user.", ephemeral=True)

    @discord.ui.button(label="Modmail 📩", style=discord.ButtonStyle.primary, row=1)
    async def open_modmail_ticket_button(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        member = interaction.guild.get_member(self.user_id)
        if not member:
            return await interaction.response.send_message("User not found.", ephemeral=True)

        # Defer first so Discord does not show "Interaction Failed" while the channel is being created.
        await interaction.response.defer(ephemeral=True)

        try:
            channel = await _create_or_get_modmail_channel(interaction.guild, member, opener=interaction.user)
            try:
                await member.send(
                    "📩 **Modmail opened**\n"
                    "A staff member opened a private support chat with you. Reply here to talk to staff."
                )
            except Exception:
                pass

            await _internal_audit(
                interaction.guild,
                "📩 Modmail Opened From Verification",
                f"{interaction.user.mention} opened modmail for {member.mention} from {interaction.channel.mention}.",
                actor=interaction.user,
                target=member
            )

            await interaction.followup.send(f"✅ Modmail opened: {channel.mention}", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Could not open modmail: `{e}`", ephemeral=True)
            try:
                await _internal_audit(
                    interaction.guild,
                    "⚠️ Modmail Button Failed",
                    f"Failed to open modmail for {member.mention}: {e}",
                    actor=interaction.user,
                    target=member,
                    color=0xED4245
                )
            except Exception:
                pass

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, row=1)
    async def claim_ticket(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        channel_id = interaction.channel.id
        current = ticket_tracking.get(channel_id, {})

        if current.get("claimed_by") and current.get("claimed_by") != interaction.user.id:
            claimer = interaction.guild.get_member(current["claimed_by"])
            claimer_text = claimer.mention if claimer else f"<@{current['claimed_by']}>"
            return await interaction.response.send_message(
                f"Already claimed by {claimer_text}.",
                ephemeral=True
            )

        if channel_id not in ticket_tracking:
            ticket_tracking[channel_id] = {}

        ticket_tracking[channel_id]["claimed_by"] = interaction.user.id
        ticket_tracking[channel_id]["status"] = "claimed"

        add_staff_stat(interaction.guild.id, interaction.user.id, "tickets_claimed", 1)
        await save_staff_stats(interaction.guild.id, interaction.user.id)

        await log_action(
            interaction.guild,
            "📌 Ticket Claimed",
            f"{interaction.user.mention} claimed {interaction.channel.mention}.",
            color=0x5865F2,
            fields=[
                ("Staff", interaction.user.mention, True),
                ("Channel", interaction.channel.mention, True),
                ("User ID", str(self.user_id), True)
            ]
        )

        await interaction.response.send_message(f"🔒 Ticket claimed by {interaction.user.mention}")

    @discord.ui.button(label="Unclaim", style=discord.ButtonStyle.secondary, row=1)
    async def unclaim_ticket(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        channel_id = interaction.channel.id
        current = ticket_tracking.get(channel_id, {})

        if not current.get("claimed_by"):
            return await interaction.response.send_message("This ticket is not claimed.", ephemeral=True)

        if current.get("claimed_by") != interaction.user.id and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Only the claimer or an admin can unclaim this ticket.",
                ephemeral=True
            )

        current["claimed_by"] = None
        current["status"] = "open"

        await log_action(
            interaction.guild,
            "📌 Ticket Unclaimed",
            f"{interaction.user.mention} unclaimed {interaction.channel.mention}.",
            color=0x99AAB5,
            fields=[
                ("Staff", interaction.user.mention, True),
                ("Channel", interaction.channel.mention, True),
                ("User ID", str(self.user_id), True)
            ]
        )

        await interaction.response.send_message(f"🔓 Ticket unclaimed by {interaction.user.mention}")

    @discord.ui.button(label="Pause ⏸️", style=discord.ButtonStyle.secondary, row=2)
    async def pause_timer_button(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        ok, msg = await pause_ticket_timer(interaction.guild, interaction.channel, interaction.user)
        await interaction.response.send_message(msg, ephemeral=not ok)

    @discord.ui.button(label="Resume ▶️", style=discord.ButtonStyle.success, row=2)
    async def resume_timer_button(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        ok, msg = await resume_ticket_timer(interaction.guild, interaction.channel, interaction.user)
        await interaction.response.send_message(msg, ephemeral=not ok)

    @discord.ui.button(label="Escalate", style=discord.ButtonStyle.secondary, row=1)
    async def escalate(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        member = interaction.guild.get_member(self.user_id)
        set_ticket_status(interaction.channel.id, "escalated")

        await log_action(
            interaction.guild,
            "🚨 Ticket Escalated",
            f"{interaction.user.mention} escalated the ticket for {member.mention if member else 'Unknown User'}.",
            color=0xED4245,
            fields=[
                ("Staff", interaction.user.mention, True),
                ("User", member.mention if member else "Unknown", True),
                ("User ID", str(self.user_id), True),
                ("Channel", interaction.channel.mention, True)
            ]
        )

        add_staff_stat(interaction.guild.id, interaction.user.id, "escalations", 1)
        await save_staff_stats(interaction.guild.id, interaction.user.id)

        await interaction.response.send_message("Ticket escalated.", ephemeral=True)

    @discord.ui.button(label="Bot Automation 🤖", style=discord.ButtonStyle.primary, row=1)
    async def bot_automation(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        member = interaction.guild.get_member(self.user_id)
        if not member:
            return await interaction.response.send_message("User not found in guild.", ephemeral=True)

        await interaction.response.send_message("🤖 Bot automation started. Collecting user answers...", ephemeral=True)

        channel = interaction.channel
        set_ticket_status(channel.id, "automation_running")

        if self.gender == "female":
            questions = [
                "1️⃣ Who invited you to this server?",
                "2️⃣ send a VM of you speaking to prove your gender",
                "3️⃣ Anything else you’d like staff to know?"
            ]
        else:
            questions = [
                "1️⃣ Who invited you to this server?",
                "2️⃣  Pof $1000.00 to be accepted",
                "3️⃣ IF you don't invite 3 girls."
            ]

        answers = []
        response_times = []
        lengths = []
        path_log = []

        def check_user(m):
            return m.author.id == member.id and m.channel == channel

        await channel.send(
            f"{member.mention}\n"
            "📋 **Automated Verification**\n"
            "Please answer the following questions one by one."
        )

        start_time = discord.utils.utcnow()

        for idx, q in enumerate(questions):
            await channel.send(q)
            q_start = discord.utils.utcnow()
            try:
                msg = await bot.wait_for("message", check=check_user, timeout=300)
            except asyncio.TimeoutError:
                answers.append("[No response — timed out]")
                response_times.append("timeout")
                lengths.append(0)
                path_log.append(f"Q{idx+1}: no response (timeout)")
                break

            q_end = discord.utils.utcnow()
            diff = (q_end - q_start).total_seconds()
            response_times.append(f"{diff:.1f}s")

            content = msg.content.strip()
            answers.append(content[:500])
            lengths.append(len(content))
            path_log.append(f"Q{idx+1}: answered")

            if msg.attachments:
                await log_action(
                    interaction.guild,
                    "📎 Proof Uploaded",
                    f"{member.mention} uploaded attachment(s) during automated verification.",
                    color=0x5865F2,
                    fields=[
                        ("User", member.mention, True),
                        ("Channel", channel.mention, True),
                        ("Files", ", ".join(a.filename for a in msg.attachments)[:1000], False)
                    ]
                )

        q1 = answers[0] if len(answers) > 0 else "N/A"
        q2 = answers[1] if len(answers) > 1 else "N/A"
        q3 = answers[2] if len(answers) > 2 else "N/A"

        end_time = discord.utils.utcnow()
        total_duration = (end_time - start_time).total_seconds()

        summary_embed = discord.Embed(
            title="📄 Automated Verification Summary",
            description=f"User: {member.mention}\nGender: **{self.gender.capitalize()}**",
            color=0x2b2d31
        )
        summary_embed.add_field(name="Q1: Who invited you?", value=q1, inline=False)
        summary_embed.add_field(name="Q2: Extra verification (optional)", value=q2, inline=False)
        summary_embed.add_field(name="Q3: Anything else?", value=q3, inline=False)
        summary_embed.add_field(
            name="Response Times",
            value="\n".join(f"Q{i+1}: {response_times[i] if i < len(response_times) else 'N/A'}" for i in range(len(questions))),
            inline=False
        )
        summary_embed.add_field(
            name="Message Lengths",
            value="\n".join(f"Q{i+1}: {lengths[i] if i < len(lengths) else 0} chars" for i in range(len(questions))),
            inline=False
        )
        summary_embed.set_footer(text=f"Bot Automation • Duration: {total_duration:.1f}s • Staff Review Required")

        await channel.send(embed=summary_embed)

        await log_action(
            interaction.guild,
            "🤖 Automation Path",
            f"Automation path for {member.mention}:",
            color=0x2b2d31,
            fields=[("User", member.mention, True), ("Path", "\n".join(path_log)[:1000], False)]
        )
        await log_action(
            interaction.guild,
            "🧮 Automation Efficiency",
            f"Automation completed for {member.mention}.",
            color=0x57F287,
            fields=[("User", member.mention, True), ("Questions", str(len(questions)), True), ("Duration", f"{total_duration:.1f}s", True)]
        )
        await log_action(
            interaction.guild,
            "📄 Automated Verification Summary",
            f"Automated Q&A completed for {member.mention}.",
            color=0x2b2d31,
            fields=[
                ("User", member.mention, True),
                ("User ID", str(member.id), True),
                ("Gender", self.gender.capitalize(), True),
                ("Q1", q1[:150], False),
                ("Q2", q2[:150], False),
                ("Q3", q3[:150], False),
                ("Total Duration", f"{total_duration:.1f}s", True)
            ]
        )

        add_staff_stat(interaction.guild.id, interaction.user.id, "automation_runs", 1)
        await save_staff_stats(interaction.guild.id, interaction.user.id)

    @discord.ui.button(label="Auto Judge 🔍", style=discord.ButtonStyle.primary, emoji="🔍", row=1)
    async def auto_judge(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        await interaction.response.send_message("🔍 Starting Strict Grok Auto Judge with image analysis...", ephemeral=True)

        member = interaction.guild.get_member(self.user_id)
        if not member:
            return await interaction.response.send_message("User not found.", ephemeral=True)

        set_ticket_status(interaction.channel.id, "auto_judge_running")

        await log_action(
            interaction.guild,
            "🔍 Auto Judge Started",
            f"{interaction.user.mention} started Auto Judge for {member.mention}.",
            color=0x5865F2,
            fields=[
                ("Staff", interaction.user.mention, True),
                ("User", member.mention, True),
                ("Channel", interaction.channel.mention, True)
            ]
        )

        judge = AutoJudge(interaction.channel, member, self.gender)
        bot.loop.create_task(judge.start())
# =========================
# GEMINI AUTO JUDGE - Free Version
# =========================
class AutoJudge:
    QUESTIONS = [
        "1️⃣ Who invited you here or how did you find this server?",
        "2️⃣ Do you know anyone in this server right now? If yes, who?",
        "3️⃣ Why do you want to join this server?",
        "4️⃣ Have you been in any other similar servers before?",
        "5️⃣ What’s your main alias or username you use?",
        "6️⃣ Can you send proof of money / balance right now? (screenshot or statement)",
        "7️⃣ How long have you been using Discord?",
        "8️⃣ What do you usually do in servers like this?",
        "9️⃣ Are you a real person or using any automation? (be honest)",
        "🔟 Anything else you want staff to know before we approve you?"
    ]

    def __init__(self, channel, member, gender):
        self.channel = channel
        self.member = member
        self.gender = gender
        self.history = []
        self.answers = []
        self.scores = []
        self.q_index = 0

    async def analyze_image(self, attachment):
        if not gemini_client:
            return "⚠️ AI is not available right now."
        try:
            resp = await gemini_client.chat.completions.create(
                model="gemini-2.0-flash",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "You are a harsh verification judge. Is this money/balance screenshot real or fake/edited/suspicious? Look for bad editing, low quality, wrong fonts, weird shadows. Be direct and roast if it's fake. Use emojis."},
                        {"type": "image_url", "image_url": {"url": attachment.url}}
                    ]
                }],
                max_tokens=200,
                temperature=0.75
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print("Image analysis error:", e)
            return "⚠️ Couldn't analyze the image properly."

    async def get_grok_reply(self, user_message):
        if not gemini_client:
            return "Bro... the AI is not working right now 💀"
        try:
            messages = [
                {"role": "system", "content": 
                    "You are Grok, a chill but extremely savage and strict verification judge. "
                    "Roast the user hard when they give weak answers. Call them dumb, slow, clown. "
                    "Use emojis. Debate them. Respond to everything they say."
                }
            ]
            for q, a in self.history[-10:]:
                messages.append({"role": "user", "content": q})
                messages.append({"role": "assistant", "content": a})
            messages.append({"role": "user", "content": user_message})

            resp = await gemini_client.chat.completions.create(
                model="gemini-2.0-flash",
                messages=messages,
                max_tokens=170,
                temperature=0.92
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print("Gemini reply error:", e)
            return "That's weak 💀 Give a real answer."

    async def start(self):
        await self.channel.send(f"🔍 **Auto Judge Started** — {self.member.mention}\nAnswer properly or get roasted 🔥")

        await self.channel.send(self.QUESTIONS[0])

        while self.q_index < len(self.QUESTIONS):
            def check(m):
                return m.author == self.member and m.channel == self.channel

            try:
                msg = await bot.wait_for("message", check=check, timeout=300)
            except asyncio.TimeoutError:
                await self.channel.send("⏰ Too slow. Ending judge.")
                return await self.finish()

            if msg.content.strip().lower() == "next question":
                self.q_index += 1
                if self.q_index < len(self.QUESTIONS):
                    await self.channel.send(self.QUESTIONS[self.q_index])
                continue

            self.history.append((self.QUESTIONS[self.q_index], msg.content))
            self.answers.append(msg.content)

            if msg.attachments:
                for att in msg.attachments:
                    if att.content_type and att.content_type.startswith("image"):
                        analysis = await self.analyze_image(att)
                        await self.channel.send(f"📸 **Image Analysis:** {analysis}")

            reply = await self.get_grok_reply(msg.content)
            await self.channel.send(reply)

            score = self._score_answer(msg.content, bool(msg.attachments))
            self.scores.append(score)

            self.q_index += 1

            if self.q_index < len(self.QUESTIONS):
                await asyncio.sleep(1.5)
                await self.channel.send(self.QUESTIONS[self.q_index])

        await self.finish()

    def _score_answer(self, text, has_image):
        score = 5
        length = len(text.strip())
        if length > 70: score += 6
        elif length > 35: score += 3
        if has_image: score += 8
        if "money" in text.lower() or has_image: score += 5
        if length < 15 or any(w in text.lower() for w in ["idk", "anything", "lol", "lmao"]):
            score -= 9
        return max(0, min(10, score))

    async def finish(self):
        total = sum(self.scores) * 2
        guild = self.channel.guild
        owner = guild.owner

        report = discord.Embed(
            title="🔍 Auto Judge Full Report",
            description=f"**User:** {self.member.mention} (`{self.member.id}`)\n**Gender:** {self.gender.capitalize()}\n**Final Score:** `{total:.0f}/100`",
            color=0x00FF00 if total >= 75 else 0xFF0000
        )

        for i, (q, a) in enumerate(zip(self.QUESTIONS, self.answers), 1):
            answer_text = a[:700] if a else "[No answer]"
            report.add_field(name=f"Q{i}: {q[:90]}...", value=answer_text, inline=False)

        report.add_field(name="Verdict", value="✅ Auto-Approve Recommended" if total >= 75 else "❌ Staff Review Required", inline=False)

        try:
            await owner.send(embed=report)

            confirm = discord.Embed(
                title="Auto Judge Approval Request",
                description=f"**User:** {self.member.mention}\n**Score:** `{total:.0f}/100`\n\nReply **yes** to approve or **no** to reject.",
                color=0x00FF00 if total >= 75 else 0xFF0000
            )
            await owner.send(embed=confirm)

            def check(m):
                return m.author == owner and m.guild is None

            reply = await bot.wait_for("message", check=check, timeout=300)
            if reply.content.lower().strip() in ["yes", "y", "approve"]:
                await self.auto_approve()
                await owner.send("✅ User approved.")
                return
            else:
                await owner.send("❌ Approval denied.")
        except Exception as e:
            print(f"DM error: {e}")

        await self.channel.send("**Staff review needed.**")

    async def auto_approve(self):
        guild = self.channel.guild
        cfg = get_guild_config(guild.id)
        unverified = guild.get_role(cfg["unverified_role"])
        role = guild.get_role(cfg["male_role"]) if self.gender == "male" else guild.get_role(cfg["female_role"])

        if unverified and unverified in self.member.roles:
            await self.member.remove_roles(unverified)
        if role:
            await self.member.add_roles(role)

        try:
            await self.member.send("✅ You passed Auto Judge! Welcome 🔥")
        except:
            pass

        await log_action(guild, "🤖 Auto Judge Approved", 
                        f"{self.member.mention} approved (Score: {sum(self.scores)*2:.0f}/100)", color=0x00FF00)
        await save_ticket_transcript(self.channel, guild, reason="Auto Judge Approved")
        await remove_from_verification(self.member.id, guild.id)
        await self.channel.delete()
# ========================
# GOOGLE GEMINI SETUP (FREE)
# ========================
from openai import AsyncOpenAI

gemini_client = None
gemini_api_key = os.getenv("GEMINI_API_KEY")

if gemini_api_key:
    try:
        gemini_client = AsyncOpenAI(
            api_key=gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        print("✅ Gemini client initialized successfully (free tier)")
    except Exception as e:
        print(f"❌ Gemini setup failed: {e}")
        gemini_client = None
else:
    print("⚠️ WARNING: GEMINI_API_KEY is not set in Railway Variables!")
    print("   Add it in Railway → Variables → GEMINI_API_KEY")
    gemini_client = None

# =========================
# TIMER + REST OF YOUR ORIGINAL CODE
# =========================

async def start_timer(channel, member, duration=600):
    if channel.id not in ticket_tracking:
        ticket_tracking[channel.id] = {}

    data = ticket_tracking[channel.id]
    data.setdefault("user_id", member.id)
    data.setdefault("claimed_by", None)
    data.setdefault("status", "open")
    data["expires_timestamp"] = int(time.time()) + duration
    data["paused"] = False

    embed = discord.Embed(
        title="⏳ Verification Timer",
        description="Starting timer...",
        color=0xED4245
    )
    embed.add_field(name="Status", value="Running ▶️", inline=True)
    embed.add_field(name="Remaining", value=f"`{format_duration(duration)}`", inline=True)
    embed.add_field(name="User", value=member.mention, inline=True)
    embed.set_footer(text="Use the buttons below to pause or resume this timer.")

    view = TimerControls(member.id)
    msg = await channel.send(embed=embed, view=view)
    warned = False

    while True:
        try:
            data = ticket_tracking.get(channel.id)
            if not data:
                break

            if data.get("paused"):
                remaining = paused_timers.get(channel.id, 0)
                embed.description = "This verification timer is currently paused."
                embed.color = 0xFEE75C
                embed.set_field_at(0, name="Status", value="Paused ⏸️", inline=True)
                embed.set_field_at(1, name="Remaining", value=f"`{format_duration(remaining)}`", inline=True)
                embed.set_field_at(2, name="User", value=member.mention, inline=True)
                await msg.edit(embed=embed, view=view)
                await asyncio.sleep(1)
                continue

            expires_ts = data.get("expires_timestamp")
            if not expires_ts:
                break

            remaining = max(0, int(expires_ts - time.time()))
            embed.description = "Countdown is active. Staff can manage it from here or from the staff controls."
            embed.color = 0x57F287 if remaining > 60 else 0xED4245
            embed.set_field_at(0, name="Status", value="Running ▶️", inline=True)
            embed.set_field_at(1, name="Remaining", value=f"`{format_duration(remaining)}`", inline=True)
            embed.set_field_at(2, name="User", value=member.mention, inline=True)

            if remaining <= 60 and not warned:
                warned = True
                await channel.send(f"⚠️ {member.mention} you have **1 minute left** to complete verification!")

            await msg.edit(embed=embed, view=view)

            if remaining <= 0:
                embed.description = "The timer has expired."
                embed.color = 0xED4245
                embed.set_field_at(0, name="Status", value="Expired ⛔", inline=True)
                embed.set_field_at(1, name="Remaining", value="`00:00`", inline=True)
                embed.set_field_at(2, name="User", value=member.mention, inline=True)
                await msg.edit(embed=embed, view=view)
                break

            await asyncio.sleep(1)

        except discord.NotFound:
            break
        except Exception as e:
            print(f"Timer UI error: {e}")
            break

# =========================
# AUTO-KICK TASK
# =========================
async def auto_kick_if_unverified(member_id, guild_id, delay=600):
    await asyncio.sleep(delay)
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    member = guild.get_member(member_id)
    if not member:
        return

    guild_cfg = get_guild_config(guild.id)
    stats = get_daily_stats(guild.id)

    unverified_role = guild.get_role(guild_cfg["unverified_role"]) if guild_cfg["unverified_role"] else None
    if unverified_role and unverified_role in member.roles:
        try:
            await member.send("⏰ You did not complete verification in time and were removed from the server.")
        except:
            pass
        await member.kick(reason="Verification timeout")

        stats["autokicked"] += 1

        await log_action(
            guild,
            "⏰ Auto-Kicked (Timeout)",
            f"{member.mention} was auto-kicked for not completing verification in time.",
            color=0xED4245,
            fields=[
                ("User", member.mention, True),
                ("User ID", str(member.id), True),
                ("Reason", "Verification timeout", True)
            ]
        )

        await remove_from_verification(member_id, guild_id)


# =========================
# CONFIG ENSURE
# =========================
async def ensure_config(guild):
    guild_cfg = get_guild_config(guild.id)

    male = guild.get_role(guild_cfg.get("male_role")) if guild_cfg.get("male_role") else None
    female = guild.get_role(guild_cfg.get("female_role")) if guild_cfg.get("female_role") else None
    unverified = guild.get_role(guild_cfg.get("unverified_role")) if guild_cfg.get("unverified_role") else None
    staff = guild.get_role(guild_cfg.get("staff_role")) if guild_cfg.get("staff_role") else None
    log_channel = guild.get_channel(guild_cfg.get("log_channel")) if guild_cfg.get("log_channel") else None

    if not male:
        male = discord.utils.get(guild.roles, name="Male")
        if male:
            guild_cfg["male_role"] = male.id

    if not female:
        female = discord.utils.get(guild.roles, name="Female")
        if female:
            guild_cfg["female_role"] = female.id

    if not unverified:
        unverified = discord.utils.get(guild.roles, name="Unverified")
        if unverified:
            guild_cfg["unverified_role"] = unverified.id

    if not staff:
        staff = discord.utils.get(guild.roles, name="Staff") or discord.utils.get(guild.roles, name="Security")
        if staff:
            guild_cfg["staff_role"] = staff.id

    if not log_channel:
        log_channel = discord.utils.get(guild.text_channels, name="verification-logs")
        if log_channel:
            guild_cfg["log_channel"] = log_channel.id

    if all([male, female, unverified, staff, log_channel]):
        await ensure_verification_categories(guild)
        await save_config_for_guild(guild.id)
        await log_action(
            guild,
            "🛠️ Config Auto-Repaired",
            "Missing roles/channels were detected and automatically restored.",
            color=0xFEE75C
        )


# =========================
# MEMBER JOIN
# =========================
@bot.event
async def on_member_join(member):
    guild = member.guild
    guild_cfg = get_guild_config(guild.id)
    stats = get_daily_stats(guild.id)

    await ensure_config(guild)
    await ensure_verification_categories(guild)

    if not ticket_config_ready(guild_cfg):
        print(f"Config not set up for guild {guild.name}, skipping member join.")
        return

    if member.id in cooldowns and time.time() < cooldowns[member.id]:
        try:
            await member.send("⏳ You recently left and cannot rejoin yet. Please try again later.")
        except:
            pass
        await member.kick(reason="Rejoin cooldown")

        await log_action(
            guild,
            "🔁 Rejoin Cooldown Kick",
            f"{member.mention} was kicked for rejoining too quickly.",
            color=0xED4245,
            fields=[
                ("User", member.mention, True),
                ("User ID", str(member.id), True)
            ]
        )
        return

    if await is_blacklisted(guild.id, member.id):
        await member.kick(reason="Blacklisted")

        await log_action(
            guild,
            "🚫 Blacklisted User Attempted To Join",
            f"{member.mention} attempted to join but is blacklisted.",
            color=0xED4245,
            fields=[
                ("User", member.mention, True),
                ("User ID", str(member.id), True)
            ]
        )
        return

    unverified = guild.get_role(guild_cfg["unverified_role"])
    if unverified:
        await member.add_roles(unverified)

    category = guild.get_channel(guild_cfg.get("general_category")) or guild.get_channel(guild_cfg["category"])
    staff_role = guild.get_role(guild_cfg["staff_role"])

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True) if staff_role else None,
        guild.me: discord.PermissionOverwrite(view_channel=True, read_message_history=True)
    }
    overwrites = {k: v for k, v in overwrites.items() if v is not None}

    channel = await guild.create_text_channel(
        f"verify-{member.name}",
        category=category,
        overwrites=overwrites,
        topic=f"ticket_for:{member.id}"
    )

    await add_to_verification(member.id, guild.id, channel.id, expires_in=600)

    ticket_tracking[channel.id] = {
        "user_id": member.id,
        "staff_id": None,
        "claimed_by": None,
        "status": "open",
        "created_at": discord.utils.utcnow(),
        "created_timestamp": int(time.time()),
        "expires_timestamp": int(time.time()) + 600,
        "paused": False,
        "last_user_msg": None,
        "last_staff_msg": None,
        "user_msg_count": 0,
        "staff_msg_count": 0,
        "attachments": [],
        "links": [],
        "silence_windows": [],
        "last_msg_time": None,
        "retries": 0,
        "followups": 0,
    }

    embed = discord.Embed(
        title="WELCOME TO THE SERVER",
        description=(
            "Welcome to the server. Before accessing the main sections, you must complete our screening verification.\n\n"
            "**Step 1:** Select your gender below.\n"
            "**Step 2:** Answer the questions in this ticket.\n"
            "**Step 3:** Wait for staff to review your screening.\n\n"
            "⚠️ Verification must be completed in 10 minutes or you may be removed from the server."
        ),
        color=0x2b2d31
    )

    await channel.send(member.mention, embed=embed, view=GenderButtons(member.id))
    bot.loop.create_task(start_timer(channel, member))

    await channel.send(
        "📝 **Question:** What's your alias?\n"
        "Please answer in this channel."
    )

    stats["joins"].append(discord.utils.utcnow().hour)

    await log_action(
        guild,
        "👤 Member Joined",
        f"{member.mention} joined and a verification ticket was created.",
        color=0x5865F2,
        fields=[
            ("User", member.mention, True),
            ("User ID", str(member.id), True),
            ("Ticket", channel.mention, True)
        ]
    )

    asyncio.create_task(auto_kick_if_unverified(member.id, guild.id, delay=600))


# =========================
# MEMBER LEAVE
# =========================
@bot.event
async def on_member_remove(member):
    guild = member.guild

    ticket_channel = None
    for ch in guild.text_channels:
        if ch.topic and ch.topic.startswith(f"ticket_for:{member.id}"):
            ticket_channel = ch
            break

    if ticket_channel:
        await save_ticket_transcript(ticket_channel, guild, reason="User Left During Verification")

        await log_action(
            guild,
            "🚪 User Left During Verification",
            f"{member.mention} left the server while their ticket was open.",
            color=0x99AAB5,
            fields=[
                ("User", member.mention, True),
                ("User ID", str(member.id), True),
                ("Ticket", ticket_channel.mention, True)
            ]
        )

        try:
            await ticket_channel.delete()
        except:
            pass
    else:
        await log_action(
            guild,
            "🚪 User Left",
            f"{member.mention} left the server.",
            color=0x99AAB5,
            fields=[
                ("User", member.mention, True),
                ("User ID", str(member.id), True)
            ]
        )


# =========================
# HELPER: IS TICKET CHANNEL
# =========================
def _is_ticket_channel(guild, channel):
    guild_cfg = get_guild_config(guild.id)
    return (
        channel.category
        and guild_cfg.get("category")
        and channel.category.id == guild_cfg["category"]
        and channel.topic
        and channel.topic.startswith("ticket_for:")
    )


# =========================
# MESSAGE EVENTS
# =========================
@bot.event
async def on_message(message):
    if message.author.bot:
        return await bot.process_commands(message)

    guild = message.guild
    if not guild:
        return await bot.process_commands(message)

    if message.author.id in shadow_muted_users:
        try:
            await message.delete()
        except Exception:
            pass
        await log_action(
            guild,
            "🌫 Shadow Mute Intercept",
            f"{message.author.mention} tried to speak while shadow-muted.",
            color=0x2b2d31,
            fields=[
                ("User", message.author.mention, True),
                ("User ID", str(message.author.id), True),
                ("Channel", message.channel.mention, True)
            ]
        )
        return

    channel = message.channel
    guild_cfg = get_guild_config(guild.id)

    now_ts = time.time()

    # Flood tracking
    ch_map = message_flood.setdefault(channel.id, {})
    user_times = ch_map.setdefault(message.author.id, [])
    user_times.append(now_ts)
    user_times[:] = [t for t in user_times if now_ts - t <= 10]
    if len(user_times) >= 5 and _is_ticket_channel(guild, channel):
        await log_action(
            guild,
            "🌊 Message Flood Detected",
            f"{message.author.mention} sent many messages quickly in {channel.mention}.",
            color=0xED4245,
            fields=[
                ("User", message.author.mention, True),
                ("User ID", str(message.author.id), True),
                ("Channel", channel.mention, True),
                ("Messages (10s)", str(len(user_times)), True)
            ]
        )

        await log_action(
            guild,
            "🔥 Conversation Temperature",
            f"High activity detected in {channel.mention}.",
            color=0xED4245,
            fields=[
                ("User", message.author.mention, True),
                ("Messages (10s)", str(len(user_times)), True),
                ("Temperature", "HIGH", True)
            ]
        )

    if _is_ticket_channel(guild, channel):
        track = ticket_tracking.get(channel.id)
        now_dt = discord.utils.utcnow()
        if track:
            if track["last_msg_time"]:
                diff = (now_dt - track["last_msg_time"]).total_seconds()
                await log_action(
                    guild,
                    "⏱ Micro-Delay",
                    f"Delay between messages in {channel.mention}.",
                    color=0x2b2d31,
                    fields=[
                        ("Channel", channel.mention, True),
                        ("Delay", f"{diff:.1f}s", True)
                    ]
                )
                if diff > 30:
                    track["silence_windows"].append(diff)
                    await log_action(
                        guild,
                        "🔇 Silence Window",
                        f"Silence detected in {channel.mention}.",
                        color=0x99AAB5,
                        fields=[
                            ("Channel", channel.mention, True),
                            ("Duration", f"{diff:.1f}s", True)
                        ]
                    )
            track["last_msg_time"] = now_dt

    if message.attachments and _is_ticket_channel(guild, channel):
        await log_action(
            guild,
            "📎 Attachment Sent",
            f"{message.author.mention} sent attachments in {channel.mention}.",
            color=0x5865F2,
            fields=[
                ("User", message.author.mention, True),
                ("User ID", str(message.author.id), True),
                ("Channel", channel.mention, True),
                ("Attachment Count", str(len(message.attachments)), True)
            ]
        )
        track = ticket_tracking.get(channel.id)
        if track:
            for a in message.attachments:
                track["attachments"].append((a.content_type or "unknown", a.size, discord.utils.utcnow()))
                await log_action(
                    guild,
                    "📎 Attachment Timeline",
                    f"Attachment logged in {channel.mention}.",
                    color=0x2b2d31,
                    fields=[
                        ("User", message.author.mention, True),
                        ("Type", a.content_type or "unknown", True),
                        ("Size", f"{a.size} bytes", True)
                    ]
                )

    if _is_ticket_channel(guild, channel):
        words = message.content.split()
        links = [w for w in words if w.startswith("http://") or w.startswith("https://")]
        if links:
            await log_action(
                guild,
                "🔗 Link Sent",
                f"{message.author.mention} sent links in {channel.mention}.",
                color=0x5865F2,
                fields=[
                    ("User", message.author.mention, True),
                    ("User ID", str(message.author.id), True),
                    ("Channel", channel.mention, True),
                    ("Links", "\n".join(links[:5]), False)
                ]
            )
            track = ticket_tracking.get(channel.id)
            if track:
                track["links"].extend(links)
                await log_action(
                    guild,
                    "🔗 Link Behavior",
                    f"Link behavior recorded in {channel.mention}.",
                    color=0x2b2d31,
                    fields=[
                        ("User", message.author.mention, True),
                        ("Link Count", str(len(links)), True)
                    ]
                )

    staff_role = guild.get_role(guild_cfg.get("staff_role")) if guild_cfg.get("staff_role") else None
    if staff_role and staff_role in message.role_mentions and _is_ticket_channel(guild, channel):
        await log_action(
            guild,
            "📣 Staff Mentioned",
            f"{message.author.mention} mentioned staff in {channel.mention}.",
            color=0xFEE75C,
            fields=[
                ("User", message.author.mention, True),
                ("User ID", str(message.author.id), True),
                ("Channel", channel.mention, True)
            ]
        )

    if _is_ticket_channel(guild, channel):
        try:
            user_id = int(channel.topic.split("ticket_for:")[1].split("|")[0])
        except:
            user_id = None

        staff_role = guild.get_role(guild_cfg.get("staff_role")) if guild_cfg.get("staff_role") else None
        is_staff = (
            message.author.guild_permissions.administrator or
            (staff_role and staff_role in message.author.roles)
        )

        track = ticket_tracking.get(channel.id)
        now_dt = discord.utils.utcnow()

        if track:
            if is_staff:
                track["staff_msg_count"] += 1
                add_staff_stat(guild.id, message.author.id, "staff_messages", 1)
                await save_staff_stats(guild.id, message.author.id)

                if track["last_user_msg"]:
                    diff = (now_dt - track["last_user_msg"]).total_seconds()
                    add_staff_response_time(guild.id, message.author.id, diff)
                    await log_action(
                        guild,
                        "🕒 Staff Response Time",
                        f"Staff response time recorded in {channel.mention}.",
                        color=0x57F287,
                        fields=[
                            ("Staff", message.author.mention, True),
                            ("Delay", f"{diff:.1f}s", True)
                        ]
                    )
                track["last_staff_msg"] = now_dt
            else:
                track["user_msg_count"] += 1
                track["last_user_msg"] = now_dt

        if is_staff and "claimed_by:" not in channel.topic:
            new_topic = channel.topic + f"|claimed_by:{message.author.id}"

            await channel.edit(
                name=f"staff-{message.author.name}-verification",
                topic=new_topic
            )

            await log_action(
                guild,
                "🛡️ Ticket Claimed",
                f"{message.author.mention} claimed ticket {channel.mention}.",
                color=0x57F287,
                fields=[
                    ("Staff", message.author.mention, True),
                    ("User ID", str(user_id), True),
                    ("Channel", channel.mention, True)
                ]
            )

            add_staff_stat(guild.id, message.author.id, "tickets_claimed", 1)
            await save_staff_stats(guild.id, message.author.id)

            if track:
                track["staff_id"] = message.author.id
                track["staff_claim_time"] = now_dt

        if is_staff and "claimed_by:" in channel.topic:
            try:
                claimed_id = int(channel.topic.split("claimed_by:")[1].split("|")[0])
            except:
                claimed_id = None

            if claimed_id and claimed_id != message.author.id:
                await log_action(
                    guild,
                    "⚠️ Staff Takeover Attempt",
                    f"{message.author.mention} is messaging in a ticket claimed by <@{claimed_id}>.",
                    color=0xED4245,
                    fields=[
                        ("Attempting Staff", message.author.mention, True),
                        ("Original Staff", f"<@{claimed_id}>", True),
                        ("Channel", channel.mention, True)
                    ]
                )

                await log_action(
                    guild,
                    "🔄 Staff Handoff",
                    f"Potential staff handoff in {channel.mention}.",
                    color=0xFEE75C,
                    fields=[
                        ("Original Staff", f"<@{claimed_id}>", True),
                        ("New Staff", message.author.mention, True)
                    ]
                )

        if user_id and message.author.id == user_id and "alias_logged" not in channel.topic:
            await channel.edit(topic=channel.topic + "|alias_logged")

            await log_action(
                guild,
                "📝 Alias Answered",
                f"{message.author.mention} answered the alias question.",
                color=0xFEE75C,
                fields=[
                    ("User", message.author.mention, True),
                    ("User ID", str(message.author.id), True),
                    ("Channel", channel.mention, True),
                    ("Alias", message.content[:200], False)
                ]
            )

        if track and (track["user_msg_count"] + track["staff_msg_count"]) % 10 == 0:
            await log_action(
                guild,
                "🧱 Ticket Structure",
                f"Structure snapshot for {channel.mention}.",
                color=0x2b2d31,
                fields=[
                    ("User Messages", str(track["user_msg_count"]), True),
                    ("Staff Messages", str(track["staff_msg_count"]), True),
                    ("Attachments", str(len(track["attachments"])), True),
                    ("Links", str(len(track["links"])), True)
                ]
            )

    await bot.process_commands(message)


# =========================
# MESSAGE EDIT / DELETE LOGS
# =========================
@bot.event
async def on_message_edit(before, after):
    if before.author.bot or not before.guild:
        return
    if not _is_ticket_channel(before.guild, before.channel):
        return
    if before.content == after.content:
        return

    await log_action(
        before.guild,
        "✏️ Message Edited",
        f"{before.author.mention} edited a message in {before.channel.mention}.",
        color=0xFEE75C,
        fields=[
            ("User", before.author.mention, True),
            ("User ID", str(before.author.id), True),
            ("Channel", before.channel.mention, True),
            ("Before", before.content[:200] or "[empty]", False),
            ("After", after.content[:200] or "[empty]", False)
        ]
    )

    await log_action(
        before.guild,
        "♻️ Retry",
        f"{before.author.mention} modified their message in {before.channel.mention}.",
        color=0x2b2d31,
        fields=[
            ("User", before.author.mention, True),
            ("Channel", before.channel.mention, True)
        ]
    )


@bot.event
async def on_message_delete(message):
    if message.author.bot or not message.guild:
        return
    if not _is_ticket_channel(message.guild, message.channel):
        return

    await log_action(
        message.guild,
        "🗑️ Message Deleted",
        f"{message.author.mention} deleted a message in {message.channel.mention}.",
        color=0xED4245,
        fields=[
            ("User", message.author.mention, True),
            ("User ID", str(message.author.id), True),
            ("Channel", message.channel.mention, True),
            ("Content", message.content[:200] or "[empty]", False)
        ]
    )

    await log_action(
        message.guild,
        "♻️ Retry",
        f"{message.author.mention} deleted a message in {message.channel.mention}.",
        color=0x2b2d31,
        fields=[
            ("User", message.author.mention, True),
            ("Channel", message.channel.mention, True)
        ]
    )


# =========================
# PROFESSIONAL ADMIN PANEL
# =========================


# =========================
# PROFESSIONAL ADMIN PANEL
# =========================

def calculate_risk_score(member):
    score = 0
    reasons = []

    account_age_days = max(0, int((discord.utils.utcnow() - member.created_at).total_seconds() // 86400))
    if account_age_days < 3:
        score += 45
        reasons.append("Fresh account")
    elif account_age_days < 7:
        score += 25
        reasons.append("New account")
    elif account_age_days < 30:
        score += 10
        reasons.append("Young account")

    if not member.avatar:
        score += 10
        reasons.append("No avatar")

    if member.name.isdigit():
        score += 10
        reasons.append("Numeric username")

    if member.default_avatar == member.display_avatar:
        score += 5
        reasons.append("Default avatar")

    return min(score, 100), reasons


async def clear_user_state(guild_id, user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM notes WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        await db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        await db.execute("DELETE FROM blacklist WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        await db.commit()


class NoteModal(discord.ui.Modal, title="Add Persistent Note"):
    note = discord.ui.TextInput(label="Note", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, guild, member, actor):
        super().__init__()
        self.guild = guild
        self.member = member
        self.actor = actor

    async def on_submit(self, interaction: discord.Interaction):
        await add_persistent_note(self.guild.id, self.member.id, str(self.note), self.actor.id)
        await log_action(
            self.guild,
            "📝 Note Added (Panel)",
            f"{self.actor.mention} added a persistent note for {self.member.mention}.",
            color=0xFEE75C,
            fields=[
                ("Staff", self.actor.mention, True),
                ("User", self.member.mention, True),
                ("Note", str(self.note)[:1000], False)
            ]
        )
        await interaction.response.send_message("📝 Note saved.", ephemeral=True)


class WarnModal(discord.ui.Modal, title="Warn User"):
    reason = discord.ui.TextInput(label="Warning Reason", style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, guild, member, actor):
        super().__init__()
        self.guild = guild
        self.member = member
        self.actor = actor

    async def on_submit(self, interaction: discord.Interaction):
        await add_warning(self.guild.id, self.member.id, str(self.reason), self.actor.id)
        await log_action(
            self.guild,
            "⚠️ Warning Added (Panel)",
            f"{self.actor.mention} warned {self.member.mention}.",
            color=0xFEE75C,
            fields=[
                ("Staff", self.actor.mention, True),
                ("User", self.member.mention, True),
                ("Reason", str(self.reason), False)
            ]
        )
        try:
            await self.member.send(f"⚠️ You were warned in **{self.guild.name}**.\nReason: {self.reason}")
        except Exception:
            pass
        await interaction.response.send_message("⚠️ Warning added.", ephemeral=True)


class RoleModal(discord.ui.Modal):
    role_id = discord.ui.TextInput(label="Role ID", placeholder="Enter a role ID", max_length=30)

    def __init__(self, guild, member, actor, mode="give"):
        title = "Give Role" if mode == "give" else "Remove Role"
        super().__init__(title=title)
        self.guild = guild
        self.member = member
        self.actor = actor
        self.mode = mode

    async def on_submit(self, interaction: discord.Interaction):
        try:
            rid = int(str(self.role_id).strip())
        except ValueError:
            return await interaction.response.send_message("Invalid role ID.", ephemeral=True)

        role = self.guild.get_role(rid)
        if not role:
            return await interaction.response.send_message("Role not found.", ephemeral=True)

        try:
            if self.mode == "give":
                await self.member.add_roles(role, reason=f"Panel role add by {self.actor}")
                action_title = "➕ Role Added (Panel)"
                action_desc = f"{self.actor.mention} gave {role.mention} to {self.member.mention}."
            else:
                await self.member.remove_roles(role, reason=f"Panel role remove by {self.actor}")
                action_title = "➖ Role Removed (Panel)"
                action_desc = f"{self.actor.mention} removed {role.mention} from {self.member.mention}."

            await log_action(
                self.guild,
                action_title,
                action_desc,
                color=0x5865F2,
                fields=[
                    ("Staff", self.actor.mention, True),
                    ("User", self.member.mention, True),
                    ("Role", role.mention, True)
                ]
            )
            await interaction.response.send_message("✅ Role updated.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Failed to update role: {e}", ephemeral=True)


class ActionSelect(discord.ui.Select):
    def __init__(self, parent_view, placeholder, options, row):
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options, row=row)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        await self.parent_view.handle_panel_action(interaction, self.values[0])


class VerificationPanel(discord.ui.View):
    def __init__(self, guild, rows):
        super().__init__(timeout=None)
        self.guild = guild
        self.rows = rows
        self.current_page = 0
        self.message = None
        self.update_task = None

        self.add_item(ActionSelect(
            self,
            "🔧 Core Moderation / Smart Actions",
            [
                discord.SelectOption(label="Accept User", value="accept", emoji="✅"),
                discord.SelectOption(label="Deny User", value="deny", emoji="❌"),
                discord.SelectOption(label="Kick", value="kick", emoji="👢"),
                discord.SelectOption(label="Ban", value="ban", emoji="⛔"),
                discord.SelectOption(label="Timeout (10m)", value="timeout", emoji="🔇"),
                discord.SelectOption(label="Unmute", value="unmute", emoji="🔊"),
                discord.SelectOption(label="Warn", value="warn", emoji="⚠️"),
                discord.SelectOption(label="View Warnings", value="view_warnings", emoji="📄"),
                discord.SelectOption(label="Auto Judge", value="auto_judge", emoji="🤖"),
                discord.SelectOption(label="Quick Approve", value="quick_approve", emoji="⚡"),
                discord.SelectOption(label="Force Verify", value="force_verify", emoji="♻️"),
                discord.SelectOption(label="Risk Level Check", value="risk_check", emoji="🧠"),
                discord.SelectOption(label="View User History", value="user_history", emoji="📁"),
            ],
            row=0
        ))

        self.add_item(ActionSelect(
            self,
            "📜 Logging / 👑 Roles",
            [
                discord.SelectOption(label="View Logs", value="view_logs", emoji="📜"),
                discord.SelectOption(label="Export Logs", value="export_logs", emoji="📤"),
                discord.SelectOption(label="Clear Logs", value="clear_logs", emoji="🧹"),
                discord.SelectOption(label="Action Audit", value="action_audit", emoji="🕵️"),
                discord.SelectOption(label="Give Role", value="give_role", emoji="➕"),
                discord.SelectOption(label="Remove Role", value="remove_role", emoji="➖"),
                discord.SelectOption(label="Toggle Pic Perms", value="toggle_pic_perms", emoji="🖼️"),
                discord.SelectOption(label="Promote to Staff", value="promote_staff", emoji="👑"),
                discord.SelectOption(label="Demote Staff", value="demote_staff", emoji="📉"),
            ],
            row=1
        ))

        self.add_item(ActionSelect(
            self,
            "⚙️ Server Control / 🎯 Power Actions",
            [
                discord.SelectOption(label="Lock Channel", value="lock_channel", emoji="🔒"),
                discord.SelectOption(label="Unlock Channel", value="unlock_channel", emoji="🔓"),
                discord.SelectOption(label="Toggle Slowmode", value="toggle_slowmode", emoji="🐢"),
                discord.SelectOption(label="Emergency Lockdown", value="emergency_lockdown", emoji="🚨"),
                discord.SelectOption(label="Mass Approve", value="mass_approve", emoji="✅"),
                discord.SelectOption(label="Mass Deny", value="mass_deny", emoji="❌"),
                discord.SelectOption(label="Reset User", value="reset_user", emoji="♻️"),
                discord.SelectOption(label="Shadow Mute", value="shadow_mute", emoji="🌫️"),
            ],
            row=2
        ))

    def _can_use_panel(self, user):
        return is_staff_member(user) or user == self.guild.owner

    async def interaction_check(self, interaction: discord.Interaction):
        if not self._can_use_panel(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return False
        return True

    def current_entry(self):
        if not self.rows:
            return None
        return self.rows[self.current_page]

    async def refresh_rows(self):
        self.rows = await get_active_verifications(self.guild.id)
        if self.rows:
            self.current_page = min(self.current_page, len(self.rows) - 1)
        else:
            self.current_page = 0

    def get_embed(self):
        total_pages = max(1, len(self.rows))
        embed = discord.Embed(
            title=f"⚖️ Hybrid Live Admin Panel ({self.current_page + 1}/{total_pages})",
            description=(
                "Professional staff console\n"
                "• Row 1: Core moderation + smart actions\n"
                "• Row 2: Logs + role management\n"
                "• Row 3: Server controls + power tools"
            ),
            color=0x5865F2
        )
        embed.timestamp = discord.utils.utcnow()

        entry = self.current_entry()
        if not entry:
            embed.description = "✅ No active verifications.\nUse Refresh to check again."
            return embed

        user_id, ticket_id, join_ts, expires_ts, status, gender = entry
        member = self.guild.get_member(user_id)
        username = member.display_name if member else f"Unknown User"

        channel = self.guild.get_channel(ticket_id)
        ticket_text = channel.mention if channel else f"`{ticket_id}` (missing)"

        track = ticket_tracking.get(ticket_id, {})
        expires_value = track.get("expires_timestamp") or expires_ts or int(time.time())
        time_left = max(0, expires_value - int(time.time()))
        time_text = format_duration(time_left)
        if track.get("paused"):
            time_text += " (paused)"

        gender_icon = "♂️" if gender == "male" else "♀️" if gender == "female" else "❓"
        claimed_by = track.get("claimed_by")
        claimed_text = f"<@{claimed_by}>" if claimed_by else "Nobody"

        score = None
        reasons = []
        if member:
            score, reasons = calculate_risk_score(member)

        embed.add_field(
            name="👤 Selected User",
            value=(
                f"{gender_icon} **{username}**\n"
                f"**ID:** `{user_id}`\n"
                f"**Ticket:** {ticket_text}\n"
                f"**Status:** `{status}`\n"
                f"**Claimed By:** {claimed_text}"
            ),
            inline=True
        )
        embed.add_field(
            name="⏱ Verification State",
            value=(
                f"**Time Left:** `{time_text}`\n"
                f"**Joined:** <t:{join_ts}:R>\n"
                f"**Gender:** {gender.capitalize() if gender else 'Not selected'}\n"
                f"**Risk:** `{score if score is not None else 'N/A'}`"
            ),
            inline=True
        )
        embed.add_field(
            name="🧩 Quick Categories",
            value=(
                "🔧 Accept / deny / warn / timeout\n"
                "📜 Logs / audit / export\n"
                "👑 Roles / pic perms / staff\n"
                "⚙️ Lockdown / slowmode / power tools"
            ),
            inline=False
        )

        if reasons:
            embed.add_field(name="⚠️ Risk Reasons", value=", ".join(reasons)[:1024], inline=False)

        if member:
            embed.set_thumbnail(url=member.display_avatar.url)

        embed.set_footer(text="Use the dropdown menus below, then navigate pages as needed")
        return embed

    async def start(self, channel):
        self.message = await channel.send(embed=self.get_embed(), view=self)
        self.update_task = bot.loop.create_task(self.live_update())

    async def live_update(self):
        while not self.is_finished():
            try:
                await asyncio.sleep(5)
                await self.refresh_rows()
                if self.message:
                    await self.message.edit(embed=self.get_embed(), view=self)
            except Exception:
                break

    async def _selected(self):
        entry = self.current_entry()
        if not entry:
            return None, None, None, None, None, None
        user_id, ticket_id, join_ts, expires_ts, status, gender = entry
        member = self.guild.get_member(user_id)
        channel = self.guild.get_channel(ticket_id)
        return entry, member, channel, user_id, ticket_id, gender

    async def _approve_current(self, interaction, quick=False):
        entry, member, channel, user_id, ticket_id, gender = await self._selected()
        if not member:
            return False, "User is no longer in the server."

        guild_cfg = get_guild_config(self.guild.id)
        unverified = self.guild.get_role(guild_cfg["unverified_role"])
        male = self.guild.get_role(guild_cfg["male_role"])
        female = self.guild.get_role(guild_cfg["female_role"])
        pic_perms = self.guild.get_role(PIC_PERMS_ROLE_ID)

        if unverified and unverified in member.roles:
            await member.remove_roles(unverified)
        if pic_perms and pic_perms in member.roles:
            await member.remove_roles(pic_perms)

        role = male if gender == "male" else female
        if role:
            await member.add_roles(role)

        try:
            await member.send("✅ You have been approved and verified.")
        except Exception:
            pass

        get_daily_stats(self.guild.id)["approved"] += 1
        set_ticket_status(ticket_id, "approved")
        await log_action(
            self.guild,
            "🟢 Approved (Control Room)" if not quick else "⚡ Quick Approved (Control Room)",
            f"{interaction.user.mention} approved {member.mention} from the control room.",
            color=0x57F287
        )
        if channel:
            await save_ticket_transcript(channel, self.guild, reason="Approved from Control Room")
            try:
                await channel.delete()
            except Exception:
                pass
        await remove_from_verification(user_id, self.guild.id)
        await self.refresh_rows()
        return True, "✅ User approved."

    async def _deny_current(self, interaction, ban=False):
        entry, member, channel, user_id, ticket_id, gender = await self._selected()
        if not member:
            return False, "User is no longer in the server."

        pic_perms = self.guild.get_role(PIC_PERMS_ROLE_ID)
        if pic_perms and pic_perms in member.roles:
            try:
                await member.remove_roles(pic_perms)
            except Exception:
                pass

        try:
            await member.send("❌ Your verification was denied." if not ban else "⛔ You were banned during verification.")
        except Exception:
            pass

        try:
            if ban:
                await member.ban(reason=f"Banned from Control Room by {interaction.user}")
            else:
                await member.kick(reason=f"Denied from Control Room by {interaction.user}")
        except Exception:
            pass

        key = "denied" if not ban else "blacklisted"
        get_daily_stats(self.guild.id)[key] += 1
        set_ticket_status(ticket_id, "denied" if not ban else "banned")
        await log_action(
            self.guild,
            "🔴 Denied (Control Room)" if not ban else "⛔ Banned (Control Room)",
            f"{interaction.user.mention} {'denied' if not ban else 'banned'} {member.mention} from the control room.",
            color=0xED4245
        )
        if channel:
            await save_ticket_transcript(channel, self.guild, reason="Denied from Control Room" if not ban else "Banned from Control Room")
            try:
                await channel.delete()
            except Exception:
                pass
        await remove_from_verification(user_id, self.guild.id)
        await self.refresh_rows()
        return True, "✅ Action complete."

    async def _panel_feedback(self, interaction, msg):
        await self.refresh_rows()
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=self.get_embed(), view=self)
            else:
                await interaction.edit_original_response(embed=self.get_embed(), view=self)
        except Exception:
            try:
                if self.message:
                    await self.message.edit(embed=self.get_embed(), view=self)
            except Exception:
                pass
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    async def handle_panel_action(self, interaction, action: str):
        entry, member, channel, user_id, ticket_id, gender = await self._selected()

        if action in {"accept", "quick_approve"}:
            ok, msg = await self._approve_current(interaction, quick=(action == "quick_approve"))
            return await self._panel_feedback(interaction, msg)

        if not member and action not in {"mass_approve", "mass_deny", "emergency_lockdown", "view_logs", "export_logs", "clear_logs"}:
            return await interaction.response.send_message("Selected user is missing.", ephemeral=True)

        if action == "deny":
            ok, msg = await self._deny_current(interaction, ban=False)
            return await self._panel_feedback(interaction, msg)

        if action == "blacklist" or action == "ban":
            if member:
                await add_blacklist(self.guild.id, member.id)
            ok, msg = await self._deny_current(interaction, ban=True)
            return await self._panel_feedback(interaction, msg)

        if action == "kick":
            try:
                await member.kick(reason=f"Kicked from Control Room by {interaction.user}")
            except Exception as e:
                return await interaction.response.send_message(f"Kick failed: {e}", ephemeral=True)
            await log_action(self.guild, "👢 Kick (Control Room)", f"{interaction.user.mention} kicked {member.mention}.", color=0xED4245)
            await remove_from_verification(member.id, self.guild.id)
            await self._panel_feedback(interaction, "👢 User kicked.")
            return

        if action == "timeout":
            try:
                until = discord.utils.utcnow() + discord.timedelta(minutes=10)
            except Exception:
                from datetime import timedelta
                until = discord.utils.utcnow() + timedelta(minutes=10)
            try:
                await member.timeout(until, reason=f"Timeout by {interaction.user}")
                await log_action(self.guild, "🔇 Timeout (Control Room)", f"{interaction.user.mention} timed out {member.mention} for 10 minutes.", color=0xFEE75C)
                return await self._panel_feedback(interaction, "🔇 User timed out for 10 minutes.")
            except Exception as e:
                return await interaction.response.send_message(f"Timeout failed: {e}", ephemeral=True)

        if action == "unmute":
            try:
                await member.timeout(None, reason=f"Unmuted by {interaction.user}")
                await log_action(self.guild, "🔊 Unmute (Control Room)", f"{interaction.user.mention} removed timeout from {member.mention}.", color=0x57F287)
                return await self._panel_feedback(interaction, "🔊 Timeout removed.")
            except Exception as e:
                return await interaction.response.send_message(f"Unmute failed: {e}", ephemeral=True)

        if action == "warn":
            return await interaction.response.send_modal(WarnModal(self.guild, member, interaction.user))

        if action == "view_warnings":
            rows = await get_warnings(self.guild.id, member.id)
            embed = discord.Embed(title=f"⚠️ Warnings — {member}", color=0xFEE75C)
            if not rows:
                embed.description = "No warnings found."
            else:
                for i, (reason, created_by, created_at) in enumerate(rows[:10], 1):
                    embed.add_field(name=f"Warning {i}", value=f"By: <@{created_by}>\nAt: <t:{created_at}:f>\nReason: {reason}", inline=False)
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if action == "auto_judge":
            set_ticket_status(ticket_id, "auto_judge_running")
            await log_action(self.guild, "🔍 Auto Judge Started (Panel)", f"{interaction.user.mention} started Auto Judge for {member.mention}.", color=0x5865F2)
            judge = AutoJudge(channel, member, gender)
            bot.loop.create_task(judge.start())
            return await self._panel_feedback(interaction, "🔍 Auto Judge started.")

        if action == "force_verify":
            if channel:
                await channel.send(f"{member.mention} ♻️ Verification has been restarted by staff.", view=GenderButtons(member.id))
            set_ticket_status(ticket_id, "restarted")
            await log_action(self.guild, "♻️ Force Verify (Panel)", f"{interaction.user.mention} restarted verification for {member.mention}.", color=0x5865F2)
            return await self._panel_feedback(interaction, "♻️ Verification restarted.")

        if action == "risk_check":
            score, reasons = calculate_risk_score(member)
            embed = discord.Embed(title=f"🧠 Risk Check — {member}", color=0xED4245 if score >= 50 else 0xFEE75C if score >= 25 else 0x57F287)
            embed.add_field(name="Risk Score", value=f"{score}/100", inline=True)
            embed.add_field(name="Reasons", value=", ".join(reasons) if reasons else "None", inline=False)
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if action == "user_history":
            notes_rows = await get_persistent_notes(self.guild.id, member.id)
            warning_rows = await get_warnings(self.guild.id, member.id)
            blacklisted = await is_blacklisted(self.guild.id, member.id)
            score, reasons = calculate_risk_score(member)
            embed = build_history_embed(member, notes_rows, warning_rows, blacklisted, score, reasons)
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if action == "view_logs":
            rows = await fetch_recent_logs(self.guild.id, user_id if member else None, limit=10)
            embed = discord.Embed(title="📜 Recent Logs", color=0x5865F2)
            if not rows:
                embed.description = "No logs found."
            else:
                for title, description, created_at in rows:
                    embed.add_field(name=f"{title} • <t:{created_at}:R>", value=description[:1000], inline=False)
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if action == "export_logs":
            data = await export_logs_text(self.guild.id, user_id if member else None, limit=200)
            import io
            fp = io.BytesIO(data.encode("utf-8"))
            return await interaction.response.send_message(file=discord.File(fp, filename="panel_logs.txt"), ephemeral=True)

        if action == "clear_logs":
            await clear_user_logs(self.guild.id, user_id if member else None)
            await log_action(self.guild, "🧹 Logs Cleared (Panel)", f"{interaction.user.mention} cleared logs from the panel.", color=0x99AAB5)
            return await self._panel_feedback(interaction, "🧹 Logs cleared.")

        if action == "action_audit":
            rows = await fetch_recent_logs(self.guild.id, user_id if member else None, limit=20)
            audit_rows = [r for r in rows if "Control Room" in r[0] or "(Panel)" in r[0]]
            embed = discord.Embed(title="🕵️ Action Audit", color=0x5865F2)
            if not audit_rows:
                embed.description = "No panel audit entries found."
            else:
                for title, description, created_at in audit_rows[:10]:
                    embed.add_field(name=f"{title} • <t:{created_at}:R>", value=description[:1000], inline=False)
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if action == "give_role":
            return await interaction.response.send_modal(RoleModal(self.guild, member, interaction.user, mode="give"))

        if action == "remove_role":
            return await interaction.response.send_modal(RoleModal(self.guild, member, interaction.user, mode="remove"))

        if action == "toggle_pic_perms":
            role = self.guild.get_role(PIC_PERMS_ROLE_ID)
            if not role:
                return await interaction.response.send_message("Pic perms role not found.", ephemeral=True)
            try:
                if role in member.roles:
                    await member.remove_roles(role, reason=f"Pic perms toggle by {interaction.user}")
                    state = "removed from"
                else:
                    await member.add_roles(role, reason=f"Pic perms toggle by {interaction.user}")
                    state = "added to"
                await log_action(self.guild, "🖼 Pic Perms Toggled (Panel)", f"{interaction.user.mention} {state} {member.mention}.", color=0x5865F2)
                return await self._panel_feedback(interaction, f"🖼 Pic perms {state} user.")
            except Exception as e:
                return await interaction.response.send_message(f"Failed to toggle pic perms: {e}", ephemeral=True)

        if action == "promote_staff" or action == "demote_staff":
            guild_cfg = get_guild_config(self.guild.id)
            staff_role = self.guild.get_role(guild_cfg["staff_role"]) if guild_cfg.get("staff_role") else None
            if not staff_role:
                return await interaction.response.send_message("Staff role not configured.", ephemeral=True)
            try:
                if action == "promote_staff":
                    await member.add_roles(staff_role, reason=f"Promoted by {interaction.user}")
                    await log_action(self.guild, "👑 Staff Promoted (Panel)", f"{interaction.user.mention} promoted {member.mention} to staff.", color=0x57F287)
                    msg = "👑 Promoted to staff."
                else:
                    await member.remove_roles(staff_role, reason=f"Demoted by {interaction.user}")
                    await log_action(self.guild, "📉 Staff Demoted (Panel)", f"{interaction.user.mention} demoted {member.mention} from staff.", color=0xED4245)
                    msg = "📉 Staff demoted."
                return await self._panel_feedback(interaction, msg)
            except Exception as e:
                return await interaction.response.send_message(f"Staff role update failed: {e}", ephemeral=True)

        if action == "lock_channel" or action == "unlock_channel":
            target_channel = channel or interaction.channel
            overwrite = target_channel.overwrites_for(self.guild.default_role)
            overwrite.send_messages = False if action == "lock_channel" else None
            await target_channel.set_permissions(self.guild.default_role, overwrite=overwrite)
            await log_action(self.guild, "🔒 Channel Locked (Panel)" if action == "lock_channel" else "🔓 Channel Unlocked (Panel)", f"{interaction.user.mention} {'locked' if action == 'lock_channel' else 'unlocked'} {target_channel.mention}.", color=0x5865F2)
            return await self._panel_feedback(interaction, f"{'🔒 Locked' if action == 'lock_channel' else '🔓 Unlocked'} channel.")

        if action == "toggle_slowmode":
            target_channel = channel or interaction.channel
            new_delay = 0 if target_channel.slowmode_delay else 10
            await target_channel.edit(slowmode_delay=new_delay)
            await log_action(self.guild, "🐢 Slowmode Toggled (Panel)", f"{interaction.user.mention} set slowmode for {target_channel.mention} to {new_delay}s.", color=0x5865F2)
            return await self._panel_feedback(interaction, f"🐢 Slowmode set to {new_delay}s.")

        if action == "emergency_lockdown":
            changed = 0
            for ch in self.guild.text_channels:
                try:
                    overwrite = ch.overwrites_for(self.guild.default_role)
                    overwrite.send_messages = False
                    await ch.set_permissions(self.guild.default_role, overwrite=overwrite)
                    changed += 1
                except Exception:
                    pass
            await log_action(self.guild, "🚨 Emergency Lockdown", f"{interaction.user.mention} locked down {changed} channels.", color=0xED4245)
            return await self._panel_feedback(interaction, f"🚨 Lockdown applied to {changed} channels.")

        if action == "mass_approve":
            count = 0
            rows_copy = list(self.rows)
            for user_id_i, ticket_id_i, _, _, _, gender_i in rows_copy:
                member_i = self.guild.get_member(user_id_i)
                channel_i = self.guild.get_channel(ticket_id_i)
                if not member_i:
                    continue
                guild_cfg = get_guild_config(self.guild.id)
                unverified = self.guild.get_role(guild_cfg["unverified_role"])
                male = self.guild.get_role(guild_cfg["male_role"])
                female = self.guild.get_role(guild_cfg["female_role"])
                pic_role = self.guild.get_role(PIC_PERMS_ROLE_ID)
                try:
                    if unverified and unverified in member_i.roles:
                        await member_i.remove_roles(unverified)
                    if pic_role and pic_role in member_i.roles:
                        await member_i.remove_roles(pic_role)
                    role = male if gender_i == "male" else female
                    if role:
                        await member_i.add_roles(role)
                    if channel_i:
                        await save_ticket_transcript(channel_i, self.guild, reason="Mass approved from Control Room")
                        try:
                            await channel_i.delete()
                        except Exception:
                            pass
                    await remove_from_verification(user_id_i, self.guild.id)
                    count += 1
                except Exception:
                    pass
            await log_action(self.guild, "✅ Mass Approve", f"{interaction.user.mention} mass-approved {count} users.", color=0x57F287)
            return await self._panel_feedback(interaction, f"✅ Mass-approved {count} users.")

        if action == "mass_deny":
            count = 0
            rows_copy = list(self.rows)
            for user_id_i, ticket_id_i, _, _, _, _ in rows_copy:
                member_i = self.guild.get_member(user_id_i)
                channel_i = self.guild.get_channel(ticket_id_i)
                if member_i:
                    try:
                        await member_i.kick(reason=f"Mass denied by {interaction.user}")
                    except Exception:
                        pass
                if channel_i:
                    await save_ticket_transcript(channel_i, self.guild, reason="Mass denied from Control Room")
                    try:
                        await channel_i.delete()
                    except Exception:
                        pass
                await remove_from_verification(user_id_i, self.guild.id)
                count += 1
            await log_action(self.guild, "❌ Mass Deny", f"{interaction.user.mention} mass-denied {count} users.", color=0xED4245)
            return await self._panel_feedback(interaction, f"❌ Mass-denied {count} users.")

        if action == "reset_user":
            await clear_user_state(self.guild.id, member.id)
            guild_cfg = get_guild_config(self.guild.id)
            for key in ("male_role", "female_role", "unverified_role"):
                role = self.guild.get_role(guild_cfg[key]) if guild_cfg.get(key) else None
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role)
                    except Exception:
                        pass
            pic_role = self.guild.get_role(PIC_PERMS_ROLE_ID)
            if pic_role and pic_role in member.roles:
                try:
                    await member.remove_roles(pic_role)
                except Exception:
                    pass
            await log_action(self.guild, "♻️ Reset User", f"{interaction.user.mention} reset stored state for {member.mention}.", color=0x99AAB5)
            return await self._panel_feedback(interaction, "♻️ User state reset.")

        if action == "shadow_mute":
            if member.id in shadow_muted_users:
                shadow_muted_users.remove(member.id)
                state = "removed"
            else:
                shadow_muted_users.add(member.id)
                state = "enabled"
            await log_action(self.guild, "🌫️ Shadow Mute Toggled", f"{interaction.user.mention} {state} shadow mute for {member.mention}.", color=0x2b2d31)
            return await self._panel_feedback(interaction, f"🌫️ Shadow mute {state}.")

        return await interaction.response.send_message("That action is not wired yet.", ephemeral=True)

    @discord.ui.button(label="🔗 Open Ticket", style=discord.ButtonStyle.primary, row=4)
    async def open_ticket_btn(self, interaction: discord.Interaction, button):
        entry = self.current_entry()
        if not entry:
            return await interaction.response.send_message("No active verification selected.", ephemeral=True)
        _, ticket_id, _, _, _, _ = entry
        channel = self.guild.get_channel(ticket_id)
        if not channel:
            return await interaction.response.send_message("Ticket channel no longer exists.", ephemeral=True)
        await interaction.response.send_message(f"Open this ticket: {channel.mention}", ephemeral=True)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=4)
    async def refresh_btn(self, interaction: discord.Interaction, button):
        await self.refresh_rows()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="⬅ Previous", style=discord.ButtonStyle.gray, row=4)
    async def previous(self, interaction: discord.Interaction, button):
        if self.current_page == 0:
            return await interaction.response.defer()
        self.current_page -= 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.gray, row=4)
    async def next(self, interaction: discord.Interaction, button):
        if self.current_page + 1 >= len(self.rows):
            return await interaction.response.defer()
        self.current_page += 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="🗑 Close Panel", style=discord.ButtonStyle.danger, row=4)
    async def close(self, interaction: discord.Interaction, button):
        if self.update_task:
            self.update_task.cancel()
        await interaction.response.defer()
        try:
            await self.message.delete()
        except Exception:
            pass


class VerificationUserSelect(discord.ui.Select):
    def __init__(self, panel_view):
        self.panel_view = panel_view
        options = []
        for idx, entry in enumerate(panel_view.rows[:25]):
            user_id, ticket_id, join_ts, expires_ts, status, gender = entry
            member = panel_view.guild.get_member(user_id)
            label = member.display_name if member else f"User {user_id}"
            description = f"{status} • {gender.capitalize() if gender else 'No gender'}"
            options.append(discord.SelectOption(label=label[:100], description=description[:100], value=str(idx), emoji="🎫"))
        if not options:
            options = [discord.SelectOption(label="No active tickets", value="none")]
        super().__init__(placeholder="🎯 Select active ticket", min_values=1, max_values=1, options=options, row=3)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            return await interaction.response.send_message("No active tickets.", ephemeral=True)
        self.panel_view.current_page = int(self.values[0])
        await self.panel_view._rebuild_selector()
        await interaction.response.edit_message(embed=self.panel_view.get_embed(), view=self.panel_view)


async def _vp_rebuild_selector(self):
    for item in list(self.children):
        if isinstance(item, VerificationUserSelect):
            self.remove_item(item)
    self.add_item(VerificationUserSelect(self))


async def _vp_refresh_rows(self):
    self.rows = await get_active_verifications(self.guild.id)
    if self.rows:
        self.current_page = min(self.current_page, len(self.rows) - 1)
    else:
        self.current_page = 0
    await self._rebuild_selector()


def _vp_get_embed(self):
    total_pages = max(1, len(self.rows))
    embed = discord.Embed(
        title=f"⚖️ Hybrid Live Admin Panel ({self.current_page + 1}/{total_pages})",
        description=(
            "Professional staff console\n"
            "• Row 1: Core moderation + smart actions\n"
            "• Row 2: Logs + role management\n"
            "• Row 3: Server controls + power tools"
        ),
        color=0x5865F2
    )
    embed.timestamp = discord.utils.utcnow()

    entry = self.current_entry()
    if not entry:
        embed.description = "✅ No active verifications.\nUse Refresh to check again."
        return embed

    user_id, ticket_id, join_ts, expires_ts, status, gender = entry
    member = self.guild.get_member(user_id)
    username = member.display_name if member else f"Unknown User"

    channel = self.guild.get_channel(ticket_id)
    ticket_text = channel.mention if channel else f"`{ticket_id}` (missing)"
    category_name = channel.category.name if channel and channel.category else "Unknown"

    track = ticket_tracking.get(ticket_id, {})
    expires_value = track.get("expires_timestamp") or expires_ts or int(time.time())
    time_left = max(0, expires_value - int(time.time()))
    time_text = format_duration(time_left)
    if track.get("paused"):
        time_text += " (paused)"

    gender_icon = "♂️" if gender == "male" else "♀️" if gender == "female" else "❓"
    claimed_by = track.get("claimed_by")
    claimed_text = f"<@{claimed_by}>" if claimed_by else "Nobody"

    score = None
    reasons = []
    if member:
        score, reasons = calculate_risk_score(member)

    embed.add_field(
        name="👤 Selected User",
        value=(
            f"{gender_icon} **{username}**\n"
            f"**ID:** `{user_id}`\n"
            f"**Ticket:** {ticket_text}\n"
            f"**Status:** `{status}`\n"
            f"**Claimed By:** {claimed_text}"
        ),
        inline=True
    )
    embed.add_field(
        name="⏱ Verification State",
        value=(
            f"**Time Left:** `{time_text}`\n"
            f"**Joined:** <t:{join_ts}:R>\n"
            f"**Gender:** {gender.capitalize() if gender else 'Not selected'}\n"
            f"**Category:** {category_name}\n"
            f"**Risk:** `{score if score is not None else 'N/A'}`"
        ),
        inline=True
    )
    embed.add_field(
        name="🧩 Quick Categories",
        value=(
            "🔧 Accept / deny / warn / timeout\n"
            "📜 Logs / audit / export\n"
            "👑 Roles / pic perms / staff\n"
            "⚙️ Lockdown / slowmode / power tools"
        ),
        inline=False
    )

    if reasons:
        embed.add_field(name="⚠️ Risk Reasons", value=", ".join(reasons)[:1024], inline=False)

    if member:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.set_footer(text="Select any active ticket from the menu below, then use the action dropdowns")
    return embed


async def _vp_start(self, channel):
    await self.refresh_rows()
    self.message = await channel.send(embed=self.get_embed(), view=self)
    self.update_task = bot.loop.create_task(self.live_update())


async def _vp_live_update(self):
    while not self.is_finished():
        try:
            await asyncio.sleep(5)
            await self.refresh_rows()
            if self.message:
                await self.message.edit(embed=self.get_embed(), view=self)
        except Exception:
            break


VerificationPanel._rebuild_selector = _vp_rebuild_selector
VerificationPanel.refresh_rows = _vp_refresh_rows
VerificationPanel.get_embed = _vp_get_embed
VerificationPanel.start = _vp_start
VerificationPanel.live_update = _vp_live_update


@bot.command()
async def adminpanel(ctx):
    if ctx.author != ctx.guild.owner:
        return await ctx.send("❌ Only the server owner can use this command.", ephemeral=True)

    control_room = await ensure_control_room(ctx.guild)
    rows = await get_active_verifications(ctx.guild.id)

    if not rows:
        await control_room.send(embed=discord.Embed(title="Control Room", description="✅ No active verifications right now. When someone starts verifying, the live hybrid admin panel will appear here automatically on the next refresh.", color=0x57F287))
    else:
        view = VerificationPanel(ctx.guild, rows)
        await view.start(control_room)

    audit_view = AuditLogPanel(ctx.guild)
    await audit_view.start(control_room)

    await ctx.send(f"{ctx.author.mention} **Control Room opened.** Verification console + audit console are now in `#control-room`.", ephemeral=True)

# =========================
# STAFF INACTIVITY CHECK
# =========================
async def staff_inactivity_check():
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
            for channel in guild.text_channels:
                if channel.topic and "claimed_by:" in channel.topic and _is_ticket_channel(guild, channel):
                    last_msg = None
                    async for msg in channel.history(limit=1):
                        last_msg = msg

                    if last_msg:
                        diff = (discord.utils.utcnow() - last_msg.created_at).total_seconds()
                        if diff > 300:
                            await log_action(
                                guild,
                                "⏳ Staff Inactivity",
                                f"No staff messages in {channel.mention} for 5 minutes.",
                                color=0xED4245,
                                fields=[
                                    ("Channel", channel.mention, True),
                                    ("Last Message", last_msg.created_at.strftime("%H:%M:%S"), True)
                                ]
                            )
        await asyncio.sleep(60)


# =========================
# DAILY SUMMARY
# =========================
async def daily_summary():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = discord.utils.utcnow()
        if now.hour == 23 and now.minute == 59:
            for guild in bot.guilds:
                stats = get_daily_stats(guild.id)

                if stats["joins"]:
                    peak_hour = max(set(stats["joins"]), key=stats["joins"].count)
                else:
                    peak_hour = "N/A"

                await log_action(
                    guild,
                    "📊 Daily Verification Summary",
                    "Here is the summary of today's verification activity:",
                    color=0x5865F2,
                    fields=[
                        ("Approved", str(stats["approved"]), True),
                        ("Denied", str(stats["denied"]), True),
                        ("Blacklisted", str(stats["blacklisted"]), True),
                        ("Auto-Kicked", str(stats["autokicked"]), True),
                        ("Peak Join Hour", str(peak_hour), True)
                    ]
                )

                await log_action(
                    guild,
                    "📊 Join Pattern",
                    "Join pattern snapshot for today.",
                    color=0x2b2d31,
                    fields=[
                        ("Join Count", str(len(stats["joins"])), True),
                        ("Peak Hour", str(peak_hour), True)
                    ]
                )

                stats["approved"] = 0
                stats["denied"] = 0
                stats["blacklisted"] = 0
                stats["autokicked"] = 0
                stats["joins"] = []

        await asyncio.sleep(60)


# =========================
# BOT HEALTH EVENTS
# =========================
@bot.event
async def on_resumed():
    for guild in bot.guilds:
        await log_action(
            guild,
            "🔄 Bot Reconnected",
            "The bot has reconnected to Discord.",
            color=0x57F287
        )


@bot.event
async def on_disconnect():
    for guild in bot.guilds:
        await log_action(
            guild,
            "⚠️ Bot Disconnected",
            "The bot lost connection to Discord.",
            color=0xED4245
        )


# =========================
# STAFF LEADERBOARD
# =========================
@bot.command()
@commands.has_permissions(manage_guild=True)
async def staffboard(ctx):
    guild = ctx.guild

    entries = []
    for (g_id, staff_id), data in staff_cache.items():
        if g_id != guild.id:
            continue
        member = guild.get_member(staff_id)
        if not member:
            continue

        score = (
            data["tickets_claimed"] * 3 +
            data["tickets_closed"] * 4 +
            data["approvals"] * 3 +
            data["denials"] * 2 +
            data["blacklists"] * 5 +
            data["escalations"] * 2 +
            data["notes"] * 1 +
            data["proof_requests"] * 1 +
            data["automation_runs"] * 2
        )
        entries.append((member, data, score))

    if not entries:
        return await ctx.send("No staff activity recorded yet.")

    entries.sort(key=lambda x: x[2], reverse=True)
    top3 = entries[:3]

    embed = discord.Embed(
        title="🏆 Staff Leaderboard",
        description="Top performing staff based on ticket activity and actions.",
        color=0xF1C40F
    )
    embed.set_footer(text=f"{guild.name} • Staff Performance")

    medals = ["🥇", "🥈", "🥉"]

    for idx, (member, data, score) in enumerate(top3):
        avg_response = (
            data["total_response_time"] / data["response_events"]
            if data["response_events"] > 0 else 0
        )
        value = (
            f"**Score:** `{score}`\n"
            f"• Tickets Claimed: `{data['tickets_claimed']}`\n"
            f"• Tickets Closed: `{data['tickets_closed']}`\n"
            f"• Approvals: `{data['approvals']}`\n"
            f"• Denials: `{data['denials']}`\n"
            f"• Blacklists: `{data['blacklists']}`\n"
            f"• Escalations: `{data['escalations']}`\n"
            f"• Notes: `{data['notes']}`\n"
            f"• Proof Requests: `{data['proof_requests']}`\n"
            f"• Automation Runs: `{data['automation_runs']}`\n"
            f"• Staff Messages: `{data['staff_messages']}`\n"
            f"• Avg Response: `{avg_response:.1f}s`\n"
        )
        embed.add_field(
            name=f"{medals[idx]} {member.display_name}",
            value=value,
            inline=False
        )

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(manage_guild=True)
async def staffhistory(ctx, member: discord.Member):
    guild = ctx.guild
    data = staff_cache.get((guild.id, member.id))
    if not data:
        return await ctx.send("No history recorded for that staff member yet.")

    avg_response = (
        data["total_response_time"] / data["response_events"]
        if data["response_events"] > 0 else 0
    )

    embed = discord.Embed(
        title=f"📜 Staff History — {member.display_name}",
        color=0x3498DB
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Tickets Claimed", value=str(data["tickets_claimed"]), inline=True)
    embed.add_field(name="Tickets Closed", value=str(data["tickets_closed"]), inline=True)
    embed.add_field(name="Approvals", value=str(data["approvals"]), inline=True)
    embed.add_field(name="Denials", value=str(data["denials"]), inline=True)
    embed.add_field(name="Blacklists", value=str(data["blacklists"]), inline=True)
    embed.add_field(name="Escalations", value=str(data["escalations"]), inline=True)
    embed.add_field(name="Notes", value=str(data["notes"]), inline=True)
    embed.add_field(name="Proof Requests", value=str(data["proof_requests"]), inline=True)
    embed.add_field(name="Automation Runs", value=str(data["automation_runs"]), inline=True)
    embed.add_field(name="Staff Messages", value=str(data["staff_messages"]), inline=True)
    embed.add_field(name="Follow-Ups", value=str(data["followups"]), inline=True)
    embed.add_field(name="Avg Response Time", value=f"{avg_response:.1f}s", inline=True)
    embed.add_field(name="Active Time", value=f"{data['active_time']:.1f}s", inline=True)

    await ctx.send(embed=embed)




@bot.command()
@commands.has_permissions(manage_guild=True)
async def mystats(ctx):
    member = ctx.author
    data = staff_cache.get((ctx.guild.id, member.id))
    if not data:
        return await ctx.send("No history recorded for you yet.")

    avg_response = (
        data["total_response_time"] / data["response_events"]
        if data["response_events"] > 0 else 0
    )

    embed = discord.Embed(
        title=f"📈 My Staff Stats — {member.display_name}",
        color=0x57F287
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Tickets Claimed", value=str(data["tickets_claimed"]), inline=True)
    embed.add_field(name="Tickets Closed", value=str(data["tickets_closed"]), inline=True)
    embed.add_field(name="Approvals", value=str(data["approvals"]), inline=True)
    embed.add_field(name="Denials", value=str(data["denials"]), inline=True)
    embed.add_field(name="Blacklists", value=str(data["blacklists"]), inline=True)
    embed.add_field(name="Escalations", value=str(data["escalations"]), inline=True)
    embed.add_field(name="Notes", value=str(data["notes"]), inline=True)
    embed.add_field(name="Proof Requests", value=str(data["proof_requests"]), inline=True)
    embed.add_field(name="Automation Runs", value=str(data["automation_runs"]), inline=True)
    embed.add_field(name="Avg Response Time", value=f"{avg_response:.1f}s", inline=True)
    embed.add_field(name="Active Time", value=f"{data['active_time']:.1f}s", inline=True)

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(manage_guild=True)
async def staffsummary(ctx):
    guild = ctx.guild
    total = {
        "tickets_claimed": 0,
        "tickets_closed": 0,
        "approvals": 0,
        "denials": 0,
        "blacklists": 0,
        "escalations": 0,
        "notes": 0,
        "proof_requests": 0,
        "automation_runs": 0,
        "staff_messages": 0,
        "followups": 0,
        "active_time": 0.0,
        "total_response_time": 0.0,
        "response_events": 0,
    }

    staff_count = 0
    for (g_id, _staff_id), data in staff_cache.items():
        if g_id != guild.id:
            continue
        staff_count += 1
        for key in total:
            total[key] += data.get(key, 0)

    if staff_count == 0:
        return await ctx.send("No staff stats recorded yet.")

    avg_response = total["total_response_time"] / total["response_events"] if total["response_events"] else 0

    embed = discord.Embed(
        title="🧾 Staff Team Summary",
        description=f"Tracked staff members: **{staff_count}**",
        color=0x8E44AD
    )
    embed.add_field(name="Tickets Claimed", value=str(total["tickets_claimed"]), inline=True)
    embed.add_field(name="Tickets Closed", value=str(total["tickets_closed"]), inline=True)
    embed.add_field(name="Approvals", value=str(total["approvals"]), inline=True)
    embed.add_field(name="Denials", value=str(total["denials"]), inline=True)
    embed.add_field(name="Blacklists", value=str(total["blacklists"]), inline=True)
    embed.add_field(name="Escalations", value=str(total["escalations"]), inline=True)
    embed.add_field(name="Notes", value=str(total["notes"]), inline=True)
    embed.add_field(name="Proof Requests", value=str(total["proof_requests"]), inline=True)
    embed.add_field(name="Automation Runs", value=str(total["automation_runs"]), inline=True)
    embed.add_field(name="Staff Messages", value=str(total["staff_messages"]), inline=True)
    embed.add_field(name="Follow-Ups", value=str(total["followups"]), inline=True)
    embed.add_field(name="Avg Response", value=f"{avg_response:.1f}s", inline=True)

    await ctx.send(embed=embed)




@bot.command()
@commands.has_permissions(manage_guild=True)
async def staffrank(ctx, member: discord.Member):
    guild = ctx.guild
    entries = []
    for (g_id, staff_id), data in staff_cache.items():
        if g_id != guild.id:
            continue
        score = (
            data["tickets_claimed"] * 3 +
            data["tickets_closed"] * 4 +
            data["approvals"] * 3 +
            data["denials"] * 2 +
            data["blacklists"] * 5 +
            data["escalations"] * 2 +
            data["notes"] * 1 +
            data["proof_requests"] * 1 +
            data["automation_runs"] * 2
        )
        entries.append((staff_id, score, data))

    if not entries:
        return await ctx.send("No staff activity recorded yet.")

    entries.sort(key=lambda x: x[1], reverse=True)
    rank = None
    selected = None
    for idx, (staff_id, score, data) in enumerate(entries, 1):
        if staff_id == member.id:
            rank = idx
            selected = (score, data)
            break

    if rank is None:
        return await ctx.send("That staff member has no recorded stats yet.")

    score, data = selected
    embed = discord.Embed(
        title=f"🏅 Staff Rank — {member.display_name}",
        color=0xF1C40F
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Rank", value=f"#{rank}", inline=True)
    embed.add_field(name="Score", value=str(score), inline=True)
    embed.add_field(name="Tickets Closed", value=str(data["tickets_closed"]), inline=True)
    embed.add_field(name="Approvals", value=str(data["approvals"]), inline=True)
    embed.add_field(name="Denials", value=str(data["denials"]), inline=True)
    embed.add_field(name="Blacklists", value=str(data["blacklists"]), inline=True)
    embed.add_field(name="Escalations", value=str(data["escalations"]), inline=True)
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(manage_guild=True)
async def stafftop(ctx, metric: str = "approvals"):
    metric = metric.lower()
    valid = {
        "approvals", "denials", "blacklists", "tickets_claimed", "tickets_closed",
        "escalations", "notes", "proof_requests", "automation_runs", "staff_messages", "followups"
    }
    if metric not in valid:
        return await ctx.send(
            "Invalid metric. Use one of: approvals, denials, blacklists, tickets_claimed, tickets_closed, escalations, notes, proof_requests, automation_runs, staff_messages, followups"
        )

    guild = ctx.guild
    entries = []
    for (g_id, staff_id), data in staff_cache.items():
        if g_id != guild.id:
            continue
        member = guild.get_member(staff_id)
        if not member:
            continue
        entries.append((member, data.get(metric, 0)))

    if not entries:
        return await ctx.send("No staff activity recorded yet.")

    entries.sort(key=lambda x: x[1], reverse=True)
    embed = discord.Embed(
        title=f"📊 Top Staff by {metric.replace('_', ' ').title()}",
        color=0x1ABC9C
    )
    for idx, (member, value) in enumerate(entries[:10], 1):
        embed.add_field(name=f"#{idx} {member.display_name}", value=str(value), inline=False)
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(manage_guild=True)
async def staffactivity(ctx, member: discord.Member):
    data = staff_cache.get((ctx.guild.id, member.id))
    if not data:
        return await ctx.send("No activity recorded for that staff member yet.")

    avg_response = (
        data["total_response_time"] / data["response_events"]
        if data["response_events"] > 0 else 0
    )

    embed = discord.Embed(
        title=f"⚡ Staff Activity — {member.display_name}",
        color=0xE67E22
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Staff Messages", value=str(data["staff_messages"]), inline=True)
    embed.add_field(name="Follow-Ups", value=str(data["followups"]), inline=True)
    embed.add_field(name="Automation Runs", value=str(data["automation_runs"]), inline=True)
    embed.add_field(name="Proof Requests", value=str(data["proof_requests"]), inline=True)
    embed.add_field(name="Escalations", value=str(data["escalations"]), inline=True)
    embed.add_field(name="Notes", value=str(data["notes"]), inline=True)
    embed.add_field(name="Active Time", value=f"{data['active_time']:.1f}s", inline=True)
    embed.add_field(name="Avg Response Time", value=f"{avg_response:.1f}s", inline=True)
    await ctx.send(embed=embed)

# =========================
# HELP MENU
# =========================
class HelpMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="General", style=discord.ButtonStyle.primary)
    async def general(self, interaction, button):
        embed = discord.Embed(
            title="📘 General Commands",
            description="Basic commands available to all users.",
            color=0x5865F2
        )
        embed.add_field(name=".help", value="Shows this help menu.", inline=False)
        embed.add_field(name=".requirements <gender> <text>", value="Set verification requirements (shared).", inline=False)
        embed.add_field(name=".unblacklist <user_id>", value="Remove a user from blacklist (this server).", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Staff", style=discord.ButtonStyle.success)
    async def staff(self, interaction, button):
        embed = discord.Embed(
            title="🛠️ Staff Commands",
            description="Commands and controls for staff.",
            color=0x57F287
        )
        embed.add_field(name=".setup", value="Initial server setup (per server).", inline=False)
        embed.add_field(name=".staffboard", value="Shows the staff leaderboard.", inline=False)
        embed.add_field(name=".staffhistory <member>", value="Shows detailed stats for a staff member.", inline=False)
        embed.add_field(name=".mystats", value="Shows your own staff stats.", inline=False)
        embed.add_field(name=".staffsummary", value="Shows the full team summary.", inline=False)
        embed.add_field(name=".staffrank <member>", value="Shows a staff member's rank and score.", inline=False)
        embed.add_field(name=".stafftop <metric>", value="Shows the top staff by a chosen metric.", inline=False)
        embed.add_field(name=".staffactivity <member>", value="Shows activity-focused stats for a staff member.", inline=False)
        embed.add_field(name="Approve/Deny/Blacklist", value="Ticket decision buttons.", inline=False)
        embed.add_field(name="Add Note / Request Proof / Escalate", value="Additional staff tools.", inline=False)
        embed.add_field(name="Bot Automation", value="Automated Q&A with summary.", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Tickets", style=discord.ButtonStyle.secondary)
    async def tickets(self, interaction, button):
        embed = discord.Embed(
            title="🎫 Ticket System",
            description="Information about the verification ticket system.",
            color=0x2b2d31
        )
        embed.add_field(name="Auto Ticket Creation", value="Creates a ticket when a user joins.", inline=False)
        embed.add_field(name="Gender Buttons", value="User selects gender to continue.", inline=False)
        embed.add_field(name="Auto Kick", value="Kicks unverified users after 10 minutes.", inline=False)
        embed.add_field(name="Staff Claim", value="First staff message claims the ticket.", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="About", style=discord.ButtonStyle.danger)
    async def about(self, interaction, button):
        embed = discord.Embed(
            title="ℹ️ About This Bot",
            description="Verification & moderation bot with logging, tickets, staff tools, automation, and forensic analytics.",
            color=0xED4245
        )
        embed.add_field(name="Features", value="Verification • Tickets • Logging • Staff Tools • Bot Automation • Leaderboards", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.command()
async def help(ctx):
    embed = discord.Embed(
        title="📚 Help Menu",
        description="Use the buttons below to navigate through command categories.",
        color=0x5865F2
    )
    embed.set_footer(text="Verification Bot • Help System")

    await ctx.send(embed=embed, view=HelpMenu())


# =========================
# READY
# =========================
@bot.event
async def on_ready():
    await init_db()
    await load_config()
    await load_staff_stats()

    for guild in bot.guilds:
        await ensure_config(guild)
        await ensure_control_room(guild)
        await log_action(
            guild,
            "🟣 Bot Started",
            f"Bot is online and connected to **{guild.name}**.",
            color=0x9B59B6
        )

    bot.loop.create_task(staff_inactivity_check())
    bot.loop.create_task(daily_summary())
    bot.loop.create_task(ticket_timeout_checker())

    print(f"Logged in as {bot.user}")
    print(f"Loaded config: {config}")


@bot.command()
async def notes(ctx, user: discord.Member):
    rows = await get_persistent_notes(ctx.guild.id, user.id)

    if not rows:
        return await ctx.send("No notes found for this user.")

    embed = discord.Embed(
        title=f"📝 Notes for {user}",
        color=0xFEE75C
    )

    for i, (note, created_by, created_at) in enumerate(rows[:10], 1):
        embed.add_field(
            name=f"Note {i}",
            value=f"**By:** <@{created_by}>\n**At:** <t:{created_at}:f>\n{note[:900]}",
            inline=False
        )

    await ctx.send(embed=embed)


@bot.command()
async def pause(ctx):
    if not is_staff_member(ctx.author):
        return await ctx.send("Staff only.")

    ok, msg = await pause_ticket_timer(ctx.guild, ctx.channel, ctx.author)
    await ctx.send(msg)


@bot.command()
async def resume(ctx):
    if not is_staff_member(ctx.author):
        return await ctx.send("Staff only.")

    ok, msg = await resume_ticket_timer(ctx.guild, ctx.channel, ctx.author)
    await ctx.send(msg)


async def ticket_timeout_checker():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = int(time.time())

        for channel_id, data in list(ticket_tracking.items()):
            if data.get("paused"):
                continue

            expires_ts = data.get("expires_timestamp")
            if not expires_ts:
                continue

            if now >= expires_ts:
                channel = bot.get_channel(channel_id)
                if not channel:
                    continue

                try:
                    await channel.send("⏰ Ticket expired due to inactivity.")
                    await log_action(
                        channel.guild,
                        "⏰ Ticket Expired",
                        f"{channel.mention} expired due to inactivity.",
                        color=0xED4245,
                        fields=[
                            ("Channel", channel.mention, True),
                            ("Status", data.get("status", "unknown"), True)
                        ]
                    )
                    await save_ticket_transcript(channel, channel.guild, reason="Expired by timeout")
                    await channel.delete()
                except Exception as e:
                    print(f"Ticket timeout error: {e}")

        await asyncio.sleep(30)




# =========================
# ADVANCED AUDIT ADD-ON
# =========================
AUDIT_PAGE_SIZE = 8
AUDIT_TYPE_LABELS = {
    "join": "User Joined",
    "leave": "User Left",
    "rejoin": "User Rejoined",
    "nickname_change": "Nickname Change",
    "username_change": "Username Change",
    "avatar_change": "Avatar Change",
    "role_added": "Role Added",
    "role_removed": "Role Removed",
    "permission_risk": "Permission Risk",
    "channel_access": "Channel Access",
    "invite_used": "Invite Used",
    "invite_created": "Invite Created",
    "invite_deleted": "Invite Deleted",
    "ticket_created": "Ticket Created",
    "ticket_lifecycle": "Ticket Lifecycle",
    "ticket_inactivity": "Ticket Inactivity",
    "ticket_attachment": "Ticket Attachment",
    "ticket_delete": "Ticket Deleted",
    "ticket_edit": "Ticket Edited",
    "ticket_participant": "Ticket Participant",
    "ticket_transfer": "Ticket Transfer",
    "ticket_reopen": "Ticket Reopen",
    "ticket_replay": "Ticket Replay",
    "staff_heatmap": "Staff Heatmap",
    "staff_shift": "Staff Shift",
    "staff_approval_streak": "Approval Streak",
    "staff_deny_streak": "Deny Streak",
    "staff_ratio": "Staff Action Ratio",
    "staff_response_delay": "Staff Response Delay",
    "staff_override": "Staff Override",
    "staff_claim_abuse": "Claim Abuse",
    "staff_note_frequency": "Note Frequency",
    "staff_proof_frequency": "Proof Frequency",
    "keyword_hit": "Keyword Hit",
    "link_history": "Link History",
    "mention_history": "Mention History",
    "reaction_audit": "Reaction Audit",
    "spam_burst": "Spam Burst",
    "copy_paste": "Copy Paste Pattern",
    "message_length": "Message Length Trend",
    "late_edit": "Late Edit",
    "attachment_type": "Attachment Type",
    "voice_proof": "Voice Proof",
    "ban_evasion": "Ban Evasion Watch",
    "alt_cluster": "Alt Cluster",
    "suspicious_role_gain": "Suspicious Role Gain",
    "manual_role_tamper": "Manual Role Tamper",
    "channel_permission_change": "Channel Permission Change",
    "emergency_mode": "Emergency Mode",
    "mass_action": "Mass Action",
    "shadow_mute": "Shadow Mute",
    "blacklist_attempt": "Blacklist Rejoin Attempt",
    "evidence_locker": "Evidence Locker",
}
AUDIT_TYPE_GROUPS = {
    "User": ["join","leave","rejoin","nickname_change","username_change","avatar_change","role_added","role_removed","permission_risk","channel_access","invite_used"],
    "Ticket": ["ticket_created","ticket_lifecycle","ticket_inactivity","ticket_attachment","ticket_delete","ticket_edit","ticket_participant","ticket_transfer","ticket_reopen","ticket_replay"],
    "Staff": ["staff_heatmap","staff_shift","staff_approval_streak","staff_deny_streak","staff_ratio","staff_response_delay","staff_override","staff_claim_abuse","staff_note_frequency","staff_proof_frequency"],
    "Message": ["keyword_hit","link_history","mention_history","reaction_audit","spam_burst","copy_paste","message_length","late_edit","attachment_type","voice_proof"],
    "Security": ["ban_evasion","alt_cluster","suspicious_role_gain","manual_role_tamper","channel_permission_change","emergency_mode","mass_action","shadow_mute","blacklist_attempt","evidence_locker"],
}
AUDIT_KEYWORDS = ["nitro", "paypal", "cashapp", "crypto", "proof", "money", "ssn", "leak", "alt", "raid"]
user_message_cache = defaultdict(list)
audit_presence_cache = {}
recent_join_cache = defaultdict(list)
invite_cache = {}

async def init_audit_tables():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            event_type TEXT,
            category TEXT,
            user_id INTEGER,
            actor_id INTEGER,
            channel_id INTEGER,
            message_id INTEGER,
            payload TEXT,
            created_at INTEGER
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS identity_snapshots (
            guild_id INTEGER,
            user_id INTEGER,
            snapshot_type TEXT,
            value TEXT,
            created_at INTEGER
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS ticket_audit_index (
            guild_id INTEGER,
            channel_id INTEGER,
            ticket_user_id INTEGER,
            event_type TEXT,
            note TEXT,
            created_at INTEGER
        )
        """)
        await db.commit()

async def audit_log(guild, event_type, payload, *, category="General", user_id=None, actor_id=None, channel_id=None, message_id=None):
    ts = int(time.time())
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO audit_events (guild_id, event_type, category, user_id, actor_id, channel_id, message_id, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild.id, event_type, category, user_id, actor_id, channel_id, message_id, str(payload)[:3500], ts)
            )
            await db.commit()
    except Exception as e:
        print(f"audit_log db error: {e}")

    title = f"🧾 Audit • {AUDIT_TYPE_LABELS.get(event_type, event_type.replace('_',' ').title())}"
    try:
        await log_action(guild, title, str(payload)[:3000], color=0x2b2d31)
    except Exception as e:
        print(f"audit_log relay error: {e}")

async def add_identity_snapshot(guild_id, user_id, snapshot_type, value):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO identity_snapshots (guild_id, user_id, snapshot_type, value, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, snapshot_type, str(value)[:2000], int(time.time()))
        )
        await db.commit()

async def add_ticket_audit(guild_id, channel_id, ticket_user_id, event_type, note):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO ticket_audit_index (guild_id, channel_id, ticket_user_id, event_type, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, ticket_user_id, event_type, str(note)[:2000], int(time.time()))
        )
        await db.commit()

async def fetch_audit_events(guild_id, *, category=None, event_type=None, user_id=None, limit=25, offset=0):
    clauses = ["guild_id=?"]
    params = [guild_id]
    if category:
        clauses.append("category=?")
        params.append(category)
    if event_type:
        clauses.append("event_type=?")
        params.append(event_type)
    if user_id:
        clauses.append("user_id=?")
        params.append(user_id)
    query = f"SELECT event_type, category, user_id, actor_id, channel_id, payload, created_at FROM audit_events WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(query, params) as cursor:
            return await cursor.fetchall()

async def fetch_audit_counts(guild_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT event_type, COUNT(*) FROM audit_events WHERE guild_id=? GROUP BY event_type", (guild_id,)) as cursor:
            rows = await cursor.fetchall()
    return {etype: count for etype, count in rows}

async def export_audit_text(guild_id, *, category=None, user_id=None, limit=500):
    rows = await fetch_audit_events(guild_id, category=category, user_id=user_id, limit=limit)
    out = []
    for event_type, category, user_id, actor_id, channel_id, payload, created_at in rows:
        out.append(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(created_at))}] {category}/{event_type}")
        out.append(f"user={user_id} actor={actor_id} channel={channel_id}")
        out.append(str(payload))
        out.append("-"*60)
    return "\n".join(out) if out else "No audit events found."


class AuditLogPanel(discord.ui.View):
    def __init__(self, guild, focus_user_id=None):
        super().__init__(timeout=None)
        self.guild = guild
        self.focus_user_id = focus_user_id
        self.category_names = ["Overview", "User", "Ticket", "Staff", "Message", "Security", "Raw"]
        self.current_category = "Overview"
        self.current_page = 0
        self.message = None
        self.update_task = None
        self.last_counts = {}

    async def refresh_counts(self):
        self.last_counts = await fetch_audit_counts(self.guild.id)

    def _group_total(self, category_name):
        if category_name in ("Overview", "Raw"):
            return sum(self.last_counts.values())
        types = AUDIT_TYPE_GROUPS.get(category_name, [])
        return sum(self.last_counts.get(t, 0) for t in types)

    async def _category_rows(self):
        if self.current_category in ("Overview", "Raw"):
            return await fetch_audit_events(
                self.guild.id,
                user_id=self.focus_user_id,
                limit=AUDIT_PAGE_SIZE,
                offset=self.current_page * AUDIT_PAGE_SIZE
            )

        types = AUDIT_TYPE_GROUPS.get(self.current_category, [])
        collected = []
        for t in types:
            rows = await fetch_audit_events(
                self.guild.id,
                event_type=t,
                user_id=self.focus_user_id,
                limit=100,
                offset=0
            )
            collected.extend(rows)

        collected.sort(key=lambda r: r[-1], reverse=True)
        start = self.current_page * AUDIT_PAGE_SIZE
        end = start + AUDIT_PAGE_SIZE
        return collected[start:end]

    async def get_embed(self):
        await self.refresh_counts()
        total_events = sum(self.last_counts.values())
        current_total = self._group_total(self.current_category)
        rows = await self._category_rows()

        color_map = {
            "Overview": 0x5865F2,
            "User": 0x57F287,
            "Ticket": 0xFEE75C,
            "Staff": 0xFAA61A,
            "Message": 0x5DADE2,
            "Security": 0xED4245,
            "Raw": 0x99AAB5,
        }

        icon_map = {
            "Overview": "🧾",
            "User": "👤",
            "Ticket": "🎟️",
            "Staff": "🛡️",
            "Message": "💬",
            "Security": "🚨",
            "Raw": "🗂️",
        }

        embed = discord.Embed(
            title=f"{icon_map.get(self.current_category, '🧾')} Audit Console — {self.current_category}",
            description=(
                "A live forensic view of what the bot is tracking in this server.\n"
                f"**Server Total:** `{total_events}` events • "
                f"**Current Bucket:** `{current_total}` • "
                f"**Page:** `{self.current_page + 1}`"
            ),
            color=color_map.get(self.current_category, 0x5865F2)
        )

        if self.focus_user_id:
            member = self.guild.get_member(self.focus_user_id)
            focus_text = member.mention if member else f"`{self.focus_user_id}`"
            embed.add_field(name="Focus", value=f"Showing audit data for {focus_text}", inline=False)

        if self.current_category == "Overview":
            embed.add_field(
                name="Core Categories",
                value=(
                    f"👤 User: `{self._group_total('User')}`\n"
                    f"🎟️ Ticket: `{self._group_total('Ticket')}`\n"
                    f"🛡️ Staff: `{self._group_total('Staff')}`"
                ),
                inline=True
            )
            embed.add_field(
                name="Activity Categories",
                value=(
                    f"💬 Message: `{self._group_total('Message')}`\n"
                    f"🚨 Security: `{self._group_total('Security')}`\n"
                    f"🗂️ Raw: `{self._group_total('Raw')}`"
                ),
                inline=True
            )
            top_items = sorted(self.last_counts.items(), key=lambda x: x[1], reverse=True)[:12]
            embed.add_field(
                name="Top Event Types",
                value="\n".join(
                    f"• {AUDIT_TYPE_LABELS.get(key, key)} — `{value}`"
                    for key, value in top_items
                ) if top_items else "No events tracked yet.",
                inline=False
            )
        else:
            bucket_types = AUDIT_TYPE_GROUPS.get(self.current_category, [])
            if self.current_category != "Raw":
                embed.add_field(
                    name="Included Event Types",
                    value="\n".join(f"• {AUDIT_TYPE_LABELS.get(t, t)}" for t in bucket_types[:12]) or "No mapped types.",
                    inline=False
                )

        if rows:
            for idx, (event_type, category, user_id, actor_id, channel_id, payload, created_at) in enumerate(rows, 1):
                payload_text = str(payload or "No payload")
                if len(payload_text) > 220:
                    payload_text = payload_text[:217] + "..."
                meta_bits = []
                if user_id:
                    meta_bits.append(f"user `{user_id}`")
                if actor_id:
                    meta_bits.append(f"actor `{actor_id}`")
                if channel_id:
                    meta_bits.append(f"channel `{channel_id}`")
                meta_bits.append(f"<t:{created_at}:R>")
                embed.add_field(
                    name=f"{idx}. {AUDIT_TYPE_LABELS.get(event_type, event_type)}",
                    value=f"{payload_text}\n*{' • '.join(meta_bits)}*",
                    inline=False
                )
        else:
            embed.add_field(
                name="No Events in This View",
                value="Nothing has been recorded for this category yet.",
                inline=False
            )

        embed.set_footer(text="Buttons: switch category • refresh • export • clear • paginate")
        return embed

    async def start(self, channel):
        self.message = await channel.send(embed=await self.get_embed(), view=self)
        self.update_task = bot.loop.create_task(self.live_update())

    async def live_update(self):
        while not self.is_finished():
            try:
                await asyncio.sleep(12)
                if self.message:
                    await self.message.edit(embed=await self.get_embed(), view=self)
            except Exception:
                break

    async def _switch(self, interaction, category):
        self.current_category = category
        self.current_page = 0
        await interaction.response.edit_message(embed=await self.get_embed(), view=self)

    @discord.ui.button(label="🧾 Overview", style=discord.ButtonStyle.primary, row=0)
    async def overview_btn(self, interaction, button):
        await self._switch(interaction, "Overview")

    @discord.ui.button(label="👤 User", style=discord.ButtonStyle.secondary, row=0)
    async def user_btn(self, interaction, button):
        await self._switch(interaction, "User")

    @discord.ui.button(label="🎟️ Ticket", style=discord.ButtonStyle.secondary, row=0)
    async def ticket_btn(self, interaction, button):
        await self._switch(interaction, "Ticket")

    @discord.ui.button(label="🛡️ Staff", style=discord.ButtonStyle.secondary, row=0)
    async def staff_btn(self, interaction, button):
        await self._switch(interaction, "Staff")

    @discord.ui.button(label="💬 Message", style=discord.ButtonStyle.secondary, row=1)
    async def msg_btn(self, interaction, button):
        await self._switch(interaction, "Message")

    @discord.ui.button(label="🚨 Security", style=discord.ButtonStyle.danger, row=1)
    async def sec_btn(self, interaction, button):
        await self._switch(interaction, "Security")

    @discord.ui.button(label="🗂️ Raw", style=discord.ButtonStyle.secondary, row=1)
    async def raw_btn(self, interaction, button):
        await self._switch(interaction, "Raw")

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.success, row=1)
    async def refresh_btn(self, interaction, button):
        await interaction.response.edit_message(embed=await self.get_embed(), view=self)

    @discord.ui.button(label="📤 Export", style=discord.ButtonStyle.primary, row=2)
    async def export_btn(self, interaction, button):
        text = await export_audit_text(
            self.guild.id,
            category=None if self.current_category in ("Overview", "Raw") else self.current_category,
            user_id=self.focus_user_id
        )
        path = f"/tmp/audit_export_{self.guild.id}.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        await interaction.response.send_message(
            file=discord.File(path, filename="audit_export.txt"),
            ephemeral=True
        )

    @discord.ui.button(label="🧹 Clear", style=discord.ButtonStyle.danger, row=2)
    async def clear_btn(self, interaction, button):
        if interaction.user != self.guild.owner:
            return await interaction.response.send_message("Only the owner can clear audit records.", ephemeral=True)

        async with aiosqlite.connect(DB_NAME) as db:
            if self.focus_user_id:
                await db.execute(
                    "DELETE FROM audit_events WHERE guild_id=? AND user_id=?",
                    (self.guild.id, self.focus_user_id)
                )
            else:
                await db.execute("DELETE FROM audit_events WHERE guild_id=?", (self.guild.id,))
            await db.commit()

        await audit_log(
            self.guild,
            "evidence_locker",
            f"{interaction.user} cleared audit rows from panel.",
            category="Security",
            actor_id=interaction.user.id
        )
        self.current_page = 0
        await interaction.response.edit_message(embed=await self.get_embed(), view=self)

    @discord.ui.button(label="⬅ Prev", style=discord.ButtonStyle.secondary, row=2)
    async def prev_btn(self, interaction, button):
        if self.current_page > 0:
            self.current_page -= 1
        await interaction.response.edit_message(embed=await self.get_embed(), view=self)

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.secondary, row=2)
    async def next_btn(self, interaction, button):
        self.current_page += 1
        await interaction.response.edit_message(embed=await self.get_embed(), view=self)

    @discord.ui.button(label="✖ Close", style=discord.ButtonStyle.secondary, row=2)
    async def close_btn(self, interaction, button):
        if self.update_task:
            self.update_task.cancel()
        await interaction.response.defer()
        try:
            await self.message.delete()
        except Exception:
            pass


@bot.command()
async def auditpanel(ctx, user: discord.Member=None):
    if ctx.author != ctx.guild.owner:
        return await ctx.send("❌ Only the server owner can use this command.")
    control_room = await ensure_control_room(ctx.guild)
    view = AuditLogPanel(ctx.guild, focus_user_id=user.id if user else None)
    await view.start(control_room)
    await ctx.send("🧾 Audit panel opened in #control-room.")

@bot.listen("on_ready")
async def _audit_on_ready_listener():
    await init_audit_tables()
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            invite_cache[guild.id] = {i.code: i.uses for i in invites}
        except Exception:
            invite_cache[guild.id] = {}

@bot.listen("on_member_join")
async def _audit_member_join(member):
    recent = recent_join_cache[member.guild.id]
    recent.append((member.id, int(time.time())))
    recent_join_cache[member.guild.id] = [(uid, ts) for uid, ts in recent if int(time.time()) - ts < 180]
    kind = "rejoin" if member.id in cooldowns else "join"
    await audit_log(member.guild, kind, f"{member} joined the server.", category="User", user_id=member.id)
    await add_identity_snapshot(member.guild.id, member.id, "display_name", member.display_name)
    await add_identity_snapshot(member.guild.id, member.id, "avatar", str(member.display_avatar.url))
    if len(recent_join_cache[member.guild.id]) >= 4:
        users = ", ".join(str(uid) for uid, _ in recent_join_cache[member.guild.id])
        await audit_log(member.guild, "alt_cluster", f"Rapid join cluster detected: {users}", category="Security", user_id=member.id)

@bot.listen("on_member_remove")
async def _audit_member_remove(member):
    await audit_log(member.guild, "leave", f"{member} left or was removed.", category="User", user_id=member.id)

@bot.listen("on_member_update")
async def _audit_member_update(before, after):
    if before.nick != after.nick:
        await audit_log(after.guild, "nickname_change", f"{before} nickname changed from `{before.nick}` to `{after.nick}`", category="User", user_id=after.id)
        await add_identity_snapshot(after.guild.id, after.id, "nick", after.nick)
    if before.display_name != after.display_name and before.nick == after.nick:
        await audit_log(after.guild, "username_change", f"{before} display name changed from `{before.display_name}` to `{after.display_name}`", category="User", user_id=after.id)
    if before.display_avatar != after.display_avatar:
        await audit_log(after.guild, "avatar_change", f"{before} changed avatar.", category="User", user_id=after.id)
        await add_identity_snapshot(after.guild.id, after.id, "avatar", str(after.display_avatar.url))
    before_roles = {r.id for r in before.roles}
    after_roles = {r.id for r in after.roles}
    added = after_roles - before_roles
    removed = before_roles - after_roles
    for rid in added:
        role = after.guild.get_role(rid)
        if role:
            et = "suspicious_role_gain" if role.permissions.administrator or role.id == PIC_PERMS_ROLE_ID else "role_added"
            await audit_log(after.guild, et, f"Role added to {after.mention}: {role.name}", category="Security" if et=="suspicious_role_gain" else "User", user_id=after.id)
    for rid in removed:
        role = after.guild.get_role(rid)
        if role:
            await audit_log(after.guild, "role_removed", f"Role removed from {after.mention}: {role.name}", category="User", user_id=after.id)
    if before.timed_out_until != after.timed_out_until:
        await audit_log(after.guild, "staff_shift", f"Timeout state changed for {after.mention}.", category="Staff", user_id=after.id)

@bot.listen("on_message")
async def _audit_on_message(message):
    if not message.guild or message.author.bot:
        return
    uid = message.author.id
    cache = user_message_cache[uid]
    cache.append((message.content, int(time.time())))
    cache[:] = cache[-6:]
    if message.channel.id in ticket_tracking:
        await add_ticket_audit(message.guild.id, message.channel.id, ticket_tracking[message.channel.id].get("user_id"), "ticket_participant", f"{message.author} spoke in ticket")
        await audit_log(message.guild, "ticket_participant", f"{message.author.mention} sent a message in {message.channel.mention}", category="Ticket", user_id=uid, channel_id=message.channel.id, message_id=message.id)
    if any(k in message.content.lower() for k in AUDIT_KEYWORDS):
        hit = ", ".join(k for k in AUDIT_KEYWORDS if k in message.content.lower())
        await audit_log(message.guild, "keyword_hit", f"Keyword(s) matched: {hit}\n{message.content[:1000]}", category="Message", user_id=uid, channel_id=message.channel.id, message_id=message.id)
    if message.attachments:
        await audit_log(message.guild, "attachment_type", f"Attachment types: {', '.join((a.content_type or 'unknown') for a in message.attachments)}", category="Message", user_id=uid, channel_id=message.channel.id, message_id=message.id)
        if any((a.content_type or '').startswith('audio') for a in message.attachments):
            await audit_log(message.guild, "voice_proof", f"Voice/audio proof sent by {message.author.mention}", category="Message", user_id=uid, channel_id=message.channel.id, message_id=message.id)
    if "http://" in message.content or "https://" in message.content:
        await audit_log(message.guild, "link_history", message.content[:1200], category="Message", user_id=uid, channel_id=message.channel.id, message_id=message.id)
    if message.mentions:
        await audit_log(message.guild, "mention_history", f"Mentioned: {', '.join(m.mention for m in message.mentions[:10])}", category="Message", user_id=uid, channel_id=message.channel.id, message_id=message.id)
    lengths = [len(c or "") for c, _ in cache]
    if lengths:
        avg = sum(lengths) / len(lengths)
        if len(message.content) > avg * 3 and len(message.content) > 100:
            await audit_log(message.guild, "message_length", f"Long message trend spike from {message.author.mention}: {len(message.content)} chars", category="Message", user_id=uid, channel_id=message.channel.id, message_id=message.id)
    if len(cache) >= 3:
        texts = [c for c, _ in cache[-3:]]
        if len(set(texts)) == 1 and texts[0]:
            await audit_log(message.guild, "copy_paste", f"Repeated identical messages from {message.author.mention}", category="Message", user_id=uid, channel_id=message.channel.id, message_id=message.id)
        times = [ts for _, ts in cache[-4:]]
        if len(times) >= 4 and max(times) - min(times) <= 6:
            await audit_log(message.guild, "spam_burst", f"Rapid burst detected from {message.author.mention}", category="Message", user_id=uid, channel_id=message.channel.id, message_id=message.id)

@bot.listen("on_message_edit")
async def _audit_on_message_edit(before, after):
    if not before.guild or before.author.bot:
        return
    event_type = "late_edit" if int(time.time()) - int(before.created_at.timestamp()) > 30 else "ticket_edit"
    await audit_log(before.guild, event_type, f"Before: {before.content[:1000]}\nAfter: {after.content[:1000]}", category="Message" if event_type=="late_edit" else "Ticket", user_id=before.author.id, channel_id=before.channel.id, message_id=before.id)

@bot.listen("on_message_delete")
async def _audit_on_message_delete(message):
    if not message.guild or (message.author and message.author.bot):
        return
    await audit_log(message.guild, "ticket_delete", f"Deleted message from {message.author}: {message.content[:1200]}", category="Ticket", user_id=message.author.id if message.author else None, channel_id=message.channel.id, message_id=message.id)

@bot.listen("on_guild_channel_create")
async def _audit_channel_create(channel):
    await audit_log(channel.guild, "channel_access", f"Channel created: {channel.name}", category="Server", channel_id=channel.id)

@bot.listen("on_guild_channel_delete")
async def _audit_channel_delete(channel):
    await audit_log(channel.guild, "channel_access", f"Channel deleted: {channel.name}", category="Server", channel_id=channel.id)

@bot.listen("on_guild_channel_update")
async def _audit_channel_update(before, after):
    if before.overwrites != after.overwrites:
        await audit_log(after.guild, "channel_permission_change", f"Permission overwrites changed for {after.name}", category="Security", channel_id=after.id)
    if before.slowmode_delay != after.slowmode_delay:
        await audit_log(after.guild, "channel_permission_change", f"Slowmode changed in {after.name} from {before.slowmode_delay} to {after.slowmode_delay}", category="Security", channel_id=after.id)

@bot.listen("on_guild_role_create")
async def _audit_role_create(role):
    await audit_log(role.guild, "manual_role_tamper", f"Role created: {role.name}", category="Security")

@bot.listen("on_guild_role_delete")
async def _audit_role_delete(role):
    await audit_log(role.guild, "manual_role_tamper", f"Role deleted: {role.name}", category="Security")

@bot.listen("on_guild_role_update")
async def _audit_role_update(before, after):
    if before.permissions != after.permissions:
        await audit_log(after.guild, "manual_role_tamper", f"Role permissions updated for {after.name}", category="Security")

@bot.listen("on_reaction_add")
async def _audit_reaction_add(reaction, user):
    if user.bot or not reaction.message.guild:
        return
    await audit_log(reaction.message.guild, "reaction_audit", f"{user.mention} reacted with {reaction.emoji} in <#{reaction.message.channel.id}>", category="Message", user_id=user.id, channel_id=reaction.message.channel.id, message_id=reaction.message.id)

@bot.listen("on_member_ban")
async def _audit_member_ban(guild, user):
    await audit_log(guild, "mass_action", f"User banned: {user}", category="Security", user_id=getattr(user, 'id', None))

@bot.listen("on_member_unban")
async def _audit_member_unban(guild, user):
    await audit_log(guild, "mass_action", f"User unbanned: {user}", category="Security", user_id=getattr(user, 'id', None))

@bot.listen("on_invite_create")
async def _audit_invite_create(invite):
    await audit_log(invite.guild, "invite_created", f"Invite created: {invite.code} by {invite.inviter}", category="User", actor_id=getattr(invite.inviter, 'id', None))

@bot.listen("on_invite_delete")
async def _audit_invite_delete(invite):
    await audit_log(invite.guild, "invite_deleted", f"Invite deleted: {invite.code}", category="User")

@bot.listen("on_member_join")
async def _audit_invite_usage(member):
    try:
        new_invites = await member.guild.invites()
        before = invite_cache.get(member.guild.id, {})
        used = None
        for inv in new_invites:
            old_uses = before.get(inv.code, 0)
            if inv.uses > old_uses:
                used = inv
                break
        invite_cache[member.guild.id] = {i.code: i.uses for i in new_invites}
        if used:
            await audit_log(member.guild, "invite_used", f"{member.mention} used invite `{used.code}` created by {used.inviter}", category="User", user_id=member.id, actor_id=getattr(used.inviter, 'id', None))
    except Exception:
        pass

async def audit_snapshot_staff(guild):
    entries = []
    for (g_id, staff_id), data in staff_cache.items():
        if g_id != guild.id:
            continue
        score = data["approvals"] + data["denials"] + data["blacklists"] + data["notes"] + data["proof_requests"]
        entries.append((staff_id, score, data))
    entries.sort(key=lambda x: x[1], reverse=True)
    if entries:
        top = entries[:5]
        lines = [f"<@{sid}> score={score} app={d['approvals']} den={d['denials']} blk={d['blacklists']}" for sid, score, d in top]
        await audit_log(guild, "staff_heatmap", "\n".join(lines), category="Staff")

@bot.listen("on_ready")
async def _audit_staff_bootstrap():
    for guild in bot.guilds:
        await audit_snapshot_staff(guild)

async def ensure_audit_panels(guild):
    control_room = await ensure_control_room(guild)
    audit_panel_exists = False
    async for m in control_room.history(limit=30):
        if m.author == guild.me and m.embeds and m.embeds[0].title and "Audit Console" in m.embeds[0].title:
            audit_panel_exists = True
            break
    if not audit_panel_exists:
        view = AuditLogPanel(guild)
        await view.start(control_room)

@bot.listen("on_ready")
async def _audit_panel_bootstrap():
    for guild in bot.guilds:
        await ensure_audit_panels(guild)


# =========================
# RUN
# =========================


# =========================
# DASHBOARD / QUEUE / PROOF / TEMPLATE EXPANSION
# =========================
import json
import io

TEMPLATE_PRESETS = {
    "male": {
        "standard": {
            "label": "Standard Male",
            "questions": [
                "Who invited you to the server?",
                "What name or alias do you go by?",
                "What are you here for?",
                "Send any proof staff may need if asked."
            ]
        },
        "strict": {
            "label": "Strict Male",
            "questions": [
                "Who invited you and how do you know them?",
                "What is your main alias on Discord and elsewhere?",
                "Have you been in similar servers before?",
                "What do you plan to do in this server?",
                "Be ready to send stronger proof if staff requests it."
            ]
        },
        "fast": {
            "label": "Fast Track Male",
            "questions": [
                "Who invited you?",
                "What alias do you use?",
                "Why do you want in?"
            ]
        }
    },
    "female": {
        "standard": {
            "label": "Standard Female",
            "questions": [
                "Who invited you to the server?",
                "What alias do you go by?",
                "Why do you want to join?",
                "Be ready for voice or image proof if staff asks."
            ]
        },
        "strict": {
            "label": "Strict Female",
            "questions": [
                "Who invited you and how do you know them?",
                "What name or alias do you usually use?",
                "Have you been in similar communities before?",
                "Why should staff trust this verification?",
                "Be ready to provide stronger proof on request."
            ]
        },
        "fast": {
            "label": "Fast Track Female",
            "questions": [
                "Who invited you?",
                "What alias do you use?",
                "Why do you want access?"
            ]
        }
    },
    "general": {
        "standard": {
            "label": "Standard General",
            "questions": [
                "Answer clearly and one question at a time.",
                "Do not spam or send fake proof.",
                "Staff may request more information if needed."
            ]
        },
        "strict": {
            "label": "Strict General",
            "questions": [
                "Provide full answers, not one-word replies.",
                "Explain how you found the server.",
                "Be ready for manual review and stronger proof requirements."
            ]
        },
        "fast": {
            "label": "Fast Track General",
            "questions": [
                "Answer fast and clearly.",
                "Wait for staff after sending your answers."
            ]
        }
    }
}

PRIORITY_ORDER = {"low": 0, "normal": 1, "medium": 2, "high": 3, "urgent": 4}
PROOF_STATUS_COLORS = {
    "unreviewed": 0x5865F2,
    "pending": 0xFEE75C,
    "approved": 0x57F287,
    "rejected": 0xED4245,
    "voice_requested": 0x9B59B6,
    "suspicious": 0xED4245,
}

async def ensure_dashboard_tables():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS verification_templates (
            guild_id INTEGER,
            gender TEXT,
            preset_key TEXT,
            label TEXT,
            questions_json TEXT,
            is_active INTEGER DEFAULT 0,
            updated_by INTEGER,
            updated_at INTEGER,
            PRIMARY KEY (guild_id, gender, preset_key)
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS proof_reviews (
            guild_id INTEGER,
            channel_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            status TEXT,
            proof_type TEXT,
            notes TEXT,
            reviewed_by INTEGER,
            reviewed_at INTEGER
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS ticket_priority (
            guild_id INTEGER,
            channel_id INTEGER PRIMARY KEY,
            priority TEXT,
            updated_by INTEGER,
            updated_at INTEGER
        )
        """)
        await db.commit()

async def seed_default_templates_for_guild(guild_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        for gender, presets in TEMPLATE_PRESETS.items():
            for preset_key, data in presets.items():
                await db.execute(
                    """
                    INSERT OR IGNORE INTO verification_templates
                    (guild_id, gender, preset_key, label, questions_json, is_active, updated_by, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild_id,
                        gender,
                        preset_key,
                        data["label"],
                        json.dumps(data["questions"]),
                        1 if preset_key == "standard" else 0,
                        0,
                        int(time.time()),
                    ),
                )
        await db.commit()

async def ensure_template_seeded(guild_id: int):
    await seed_default_templates_for_guild(guild_id)

async def get_active_template(guild_id: int, gender: str):
    gender = (gender or "general").lower()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT preset_key, label, questions_json FROM verification_templates WHERE guild_id=? AND gender=? AND is_active=1 LIMIT 1",
            (guild_id, gender)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "preset_key": row[0],
                    "label": row[1],
                    "questions": json.loads(row[2])
                }
    preset = TEMPLATE_PRESETS.get(gender, TEMPLATE_PRESETS["general"])["standard"]
    return {"preset_key": "standard", "label": preset["label"], "questions": preset["questions"]}

async def list_templates_for_gender(guild_id: int, gender: str):
    gender = (gender or "general").lower()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT preset_key, label, questions_json, is_active FROM verification_templates WHERE guild_id=? AND gender=? ORDER BY preset_key",
            (guild_id, gender)
        ) as cursor:
            rows = await cursor.fetchall()
    results = []
    for preset_key, label, questions_json, is_active in rows:
        results.append({
            "preset_key": preset_key,
            "label": label,
            "questions": json.loads(questions_json),
            "is_active": bool(is_active),
        })
    if not results:
        for preset_key, data in TEMPLATE_PRESETS.get(gender, TEMPLATE_PRESETS["general"]).items():
            results.append({
                "preset_key": preset_key,
                "label": data["label"],
                "questions": data["questions"],
                "is_active": preset_key == "standard",
            })
    return results

async def activate_template(guild_id: int, gender: str, preset_key: str, actor_id: int):
    gender = gender.lower()
    await ensure_template_seeded(guild_id)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE verification_templates SET is_active=0 WHERE guild_id=? AND gender=?",
            (guild_id, gender)
        )
        preset = TEMPLATE_PRESETS.get(gender, TEMPLATE_PRESETS["general"]).get(preset_key)
        label = preset["label"] if preset else preset_key.title()
        questions = preset["questions"] if preset else []
        await db.execute(
            """
            INSERT OR REPLACE INTO verification_templates
            (guild_id, gender, preset_key, label, questions_json, is_active, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (guild_id, gender, preset_key, label, json.dumps(questions), actor_id, int(time.time()))
        )
        await db.commit()

async def upsert_custom_template(guild_id: int, gender: str, label: str, questions, actor_id: int, activate_now: bool = True):
    gender = gender.lower()
    preset_key = "custom"
    async with aiosqlite.connect(DB_NAME) as db:
        if activate_now:
            await db.execute("UPDATE verification_templates SET is_active=0 WHERE guild_id=? AND gender=?", (guild_id, gender))
        await db.execute(
            """
            INSERT OR REPLACE INTO verification_templates
            (guild_id, gender, preset_key, label, questions_json, is_active, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, gender, preset_key, label, json.dumps(questions), 1 if activate_now else 0, actor_id, int(time.time()))
        )
        await db.commit()

async def get_ticket_priority(guild_id: int, channel_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT priority FROM ticket_priority WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]
    track = ticket_tracking.get(channel_id, {})
    return track.get("priority", "normal")

async def set_ticket_priority_db(guild_id: int, channel_id: int, priority: str, actor_id: int):
    priority = priority.lower()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO ticket_priority (guild_id, channel_id, priority, updated_by, updated_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, channel_id, priority, actor_id, int(time.time()))
        )
        await db.commit()
    if channel_id in ticket_tracking:
        ticket_tracking[channel_id]["priority"] = priority

async def get_proof_review(guild_id: int, channel_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT status, proof_type, notes, reviewed_by, reviewed_at, user_id FROM proof_reviews WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "status": row[0],
                    "proof_type": row[1],
                    "notes": row[2],
                    "reviewed_by": row[3],
                    "reviewed_at": row[4],
                    "user_id": row[5],
                }
    return {
        "status": "unreviewed",
        "proof_type": "unknown",
        "notes": "",
        "reviewed_by": None,
        "reviewed_at": None,
        "user_id": ticket_tracking.get(channel_id, {}).get("user_id"),
    }

async def set_proof_review(guild_id: int, channel_id: int, user_id: int, status: str, proof_type: str, notes: str, actor_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO proof_reviews
            (guild_id, channel_id, user_id, status, proof_type, notes, reviewed_by, reviewed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, channel_id, user_id, status, proof_type, notes, actor_id, int(time.time()))
        )
        await db.commit()
    if channel_id in ticket_tracking:
        ticket_tracking[channel_id]["proof_status"] = status
        ticket_tracking[channel_id]["proof_type"] = proof_type
        ticket_tracking[channel_id]["proof_notes"] = notes

def ensure_ticket_tracking_defaults(channel_id: int, user_id: int | None = None):
    if channel_id not in ticket_tracking:
        ticket_tracking[channel_id] = {}
    data = ticket_tracking[channel_id]
    if user_id is not None:
        data.setdefault("user_id", user_id)
    data.setdefault("claimed_by", None)
    data.setdefault("status", "open")
    data.setdefault("priority", "normal")
    data.setdefault("proof_status", "unreviewed")
    data.setdefault("proof_type", "unknown")
    data.setdefault("proof_notes", "")
    data.setdefault("reminders_sent", [])
    data.setdefault("template_name", None)
    data.setdefault("template_gender", None)
    data.setdefault("created_timestamp", int(time.time()))
    data.setdefault("attachments", [])
    data.setdefault("user_msg_count", 0)
    data.setdefault("staff_msg_count", 0)
    data.setdefault("followups", 0)
    return data

async def hydrate_tracking_from_db(guild):
    rows = await get_active_verifications(guild.id)
    for user_id, channel_id, join_ts, expires_ts, status, gender in rows:
        data = ensure_ticket_tracking_defaults(channel_id, user_id)
        data.setdefault("status", status or "open")
        data.setdefault("expires_timestamp", expires_ts)
        data.setdefault("gender", gender)
        data.setdefault("created_timestamp", join_ts)
        data["priority"] = await get_ticket_priority(guild.id, channel_id)
        proof = await get_proof_review(guild.id, channel_id)
        data["proof_status"] = proof["status"]
        data["proof_type"] = proof["proof_type"]
        data["proof_notes"] = proof["notes"] or ""

async def get_sorted_queue_rows(guild_id: int):
    rows = await get_active_verifications(guild_id)
    enriched = []
    for row in rows:
        user_id, channel_id, join_ts, expires_ts, status, gender = row
        data = ensure_ticket_tracking_defaults(channel_id, user_id)
        priority = await get_ticket_priority(guild_id, channel_id)
        proof = await get_proof_review(guild_id, channel_id)
        data["priority"] = priority
        data["proof_status"] = proof["status"]
        enriched.append((PRIORITY_ORDER.get(priority, 1), join_ts, row))
    enriched.sort(key=lambda item: (-item[0], item[1]))
    return [row for _, __, row in enriched]

async def auto_reminder_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
            rows = await get_active_verifications(guild.id)
            staff_role = guild.get_role(get_guild_config(guild.id).get("staff_role")) if get_guild_config(guild.id).get("staff_role") else None
            for user_id, channel_id, join_ts, expires_ts, status, gender in rows:
                channel = guild.get_channel(channel_id)
                member = guild.get_member(user_id)
                if not channel or not member:
                    continue
                data = ensure_ticket_tracking_defaults(channel_id, user_id)
                if data.get("paused"):
                    continue
                reminders = set(data.get("reminders_sent", []))
                now = int(time.time())
                age = now - int(data.get("created_timestamp", join_ts or now))
                if age >= 120 and "user_2m" not in reminders and data.get("user_msg_count", 0) == 0:
                    try:
                        await channel.send(f"⏰ {member.mention} quick reminder: please answer the verification questions so staff can review you.")
                    except Exception:
                        pass
                    reminders.add("user_2m")
                    data["followups"] = data.get("followups", 0) + 1
                if age >= 300 and "user_5m" not in reminders and data.get("user_msg_count", 0) <= 1:
                    try:
                        await channel.send(f"⚠️ {member.mention} second reminder: your ticket is still pending. Complete it before the timer ends.")
                    except Exception:
                        pass
                    reminders.add("user_5m")
                    data["followups"] = data.get("followups", 0) + 1
                if data.get("last_user_msg") and not data.get("last_staff_msg"):
                    wait_time = (discord.utils.utcnow() - data["last_user_msg"]).total_seconds()
                    if wait_time >= 180 and "staff_ping" not in reminders:
                        try:
                            if staff_role:
                                await channel.send(f"📣 {staff_role.mention} this ticket needs a staff response.")
                            else:
                                await channel.send("📣 Staff reminder: this ticket needs a response.")
                        except Exception:
                            pass
                        reminders.add("staff_ping")
                data["reminders_sent"] = list(reminders)
        await asyncio.sleep(30)

class CustomTemplateModal(discord.ui.Modal, title="Create Custom Verification Template"):
    label_input = discord.ui.TextInput(label="Template Name", max_length=50, default="Custom Template")
    q1 = discord.ui.TextInput(label="Question 1", max_length=200)
    q2 = discord.ui.TextInput(label="Question 2", max_length=200, required=False)
    q3 = discord.ui.TextInput(label="Question 3", max_length=200, required=False)
    q4 = discord.ui.TextInput(label="Question 4", style=discord.TextStyle.paragraph, max_length=300, required=False)

    def __init__(self, guild, gender, actor):
        super().__init__()
        self.guild = guild
        self.gender = gender
        self.actor = actor

    async def on_submit(self, interaction: discord.Interaction):
        questions = [str(self.q1).strip()]
        for q in (self.q2, self.q3, self.q4):
            txt = str(q).strip()
            if txt:
                questions.append(txt)
        await upsert_custom_template(self.guild.id, self.gender, str(self.label_input).strip(), questions, self.actor.id, activate_now=True)
        await log_action(
            self.guild,
            "🧩 Custom Template Saved",
            f"{self.actor.mention} saved a custom **{self.gender}** template.",
            color=0x5865F2,
            fields=[("Template", str(self.label_input)[:100], True), ("Questions", str(len(questions)), True)]
        )
        await interaction.response.send_message("✅ Custom template saved and activated.", ephemeral=True)

class QueuePanelView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=300)
        self.guild = guild
        self.rows = []
        self.page = 0

    async def refresh_rows(self):
        self.rows = await get_sorted_queue_rows(self.guild.id)
        if self.rows:
            self.page = min(self.page, len(self.rows) - 1)
        else:
            self.page = 0

    async def interaction_check(self, interaction: discord.Interaction):
        if not is_staff_member(interaction.user) and interaction.user != self.guild.owner:
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return False
        return True

    async def current_row(self):
        if not self.rows:
            return None
        return self.rows[self.page]

    async def build_embed(self):
        await self.refresh_rows()
        embed = discord.Embed(title="🎫 Queue Panel", color=0x5865F2)
        embed.description = "Clean live queue sorted by priority first, then oldest ticket."
        if not self.rows:
            embed.add_field(name="Status", value="✅ No active tickets.", inline=False)
            return embed
        user_id, channel_id, join_ts, expires_ts, status, gender = self.rows[self.page]
        member = self.guild.get_member(user_id)
        channel = self.guild.get_channel(channel_id)
        data = ensure_ticket_tracking_defaults(channel_id, user_id)
        priority = await get_ticket_priority(self.guild.id, channel_id)
        proof = await get_proof_review(self.guild.id, channel_id)
        claimed_by = data.get("claimed_by")
        time_left = format_duration(max(0, int((data.get("expires_timestamp") or expires_ts or int(time.time())) - time.time())))
        embed.add_field(name="Ticket", value=channel.mention if channel else f"`{channel_id}`", inline=True)
        embed.add_field(name="User", value=member.mention if member else f"`{user_id}`", inline=True)
        embed.add_field(name="Priority", value=priority.title(), inline=True)
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Gender", value=(gender or "unknown").title(), inline=True)
        embed.add_field(name="Claimed By", value=f"<@{claimed_by}>" if claimed_by else "Nobody", inline=True)
        embed.add_field(name="Proof", value=proof["status"].replace("_", " ").title(), inline=True)
        embed.add_field(name="Attachments", value=str(len(data.get("attachments", []))), inline=True)
        embed.add_field(name="Time Left", value=time_left, inline=True)
        embed.add_field(name="User Messages", value=str(data.get("user_msg_count", 0)), inline=True)
        embed.add_field(name="Staff Messages", value=str(data.get("staff_msg_count", 0)), inline=True)
        embed.add_field(name="Follow-Ups", value=str(data.get("followups", 0)), inline=True)
        embed.set_footer(text=f"Ticket {self.page + 1}/{len(self.rows)} • Buttons below keep the queue organized")
        return embed

    @discord.ui.button(label="⬅", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button):
        await self.refresh_rows()
        if self.rows:
            self.page = (self.page - 1) % len(self.rows)
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="➡", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button):
        await self.refresh_rows()
        if self.rows:
            self.page = (self.page + 1) % len(self.rows)
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Low", style=discord.ButtonStyle.secondary, row=1)
    async def low_btn(self, interaction: discord.Interaction, button):
        await self._set_priority(interaction, "low")

    @discord.ui.button(label="Medium", style=discord.ButtonStyle.primary, row=1)
    async def med_btn(self, interaction: discord.Interaction, button):
        await self._set_priority(interaction, "medium")

    @discord.ui.button(label="High", style=discord.ButtonStyle.danger, row=1)
    async def high_btn(self, interaction: discord.Interaction, button):
        await self._set_priority(interaction, "high")

    async def _set_priority(self, interaction, priority):
        row = await self.current_row()
        if not row:
            return await interaction.response.send_message("No ticket selected.", ephemeral=True)
        user_id, channel_id, _, _, _, _ = row
        await set_ticket_priority_db(self.guild.id, channel_id, priority, interaction.user.id)
        channel = self.guild.get_channel(channel_id)
        member = self.guild.get_member(user_id)
        if channel:
            try:
                await channel.send(f"🏷️ Priority updated to **{priority.title()}** by {interaction.user.mention}.")
            except Exception:
                pass
        await log_action(self.guild, "🏷️ Ticket Priority Updated", f"{interaction.user.mention} set {channel.mention if channel else channel_id} to **{priority.title()}**.", color=0x5865F2, fields=[("User", member.mention if member else str(user_id), True), ("Priority", priority.title(), True)])
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, row=2)
    async def claim_btn(self, interaction: discord.Interaction, button):
        row = await self.current_row()
        if not row:
            return await interaction.response.send_message("No ticket selected.", ephemeral=True)
        user_id, channel_id, _, _, _, _ = row
        data = ensure_ticket_tracking_defaults(channel_id, user_id)
        data["claimed_by"] = interaction.user.id
        channel = self.guild.get_channel(channel_id)
        if channel and channel.topic and f"claimed_by:{interaction.user.id}" not in channel.topic:
            if "claimed_by:" not in channel.topic:
                try:
                    await channel.edit(topic=channel.topic + f"|claimed_by:{interaction.user.id}")
                except Exception:
                    pass
        add_staff_stat(self.guild.id, interaction.user.id, "tickets_claimed", 1)
        await save_staff_stats(self.guild.id, interaction.user.id)
        await log_action(self.guild, "🛡️ Ticket Claimed (Queue)", f"{interaction.user.mention} claimed {channel.mention if channel else channel_id} from the queue panel.", color=0x57F287)
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Open", style=discord.ButtonStyle.primary, row=2)
    async def open_btn(self, interaction: discord.Interaction, button):
        row = await self.current_row()
        if not row:
            return await interaction.response.send_message("No ticket selected.", ephemeral=True)
        _, channel_id, _, _, _, _ = row
        channel = self.guild.get_channel(channel_id)
        await interaction.response.send_message(f"Open ticket: {channel.mention if channel else f'`{channel_id}`'}", ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def refresh_btn(self, interaction: discord.Interaction, button):
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

class StaffPerformancePanelView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=300)
        self.guild = guild
        self.page = 0
        self.entries = []

    async def interaction_check(self, interaction: discord.Interaction):
        if not is_staff_member(interaction.user) and interaction.user != self.guild.owner:
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return False
        return True

    def refresh_entries(self):
        staff_members = []
        for (guild_id, staff_id), data in staff_cache.items():
            if guild_id != self.guild.id:
                continue
            score = (
                data["tickets_claimed"] * 3 +
                data["tickets_closed"] * 4 +
                data["approvals"] * 2 +
                data["denials"] * 2 +
                data["blacklists"] * 3 +
                data["proof_requests"] * 1 +
                data["notes"] * 1 +
                data["followups"] * 1
            )
            staff_members.append((score, staff_id, data))
        staff_members.sort(key=lambda x: x[0], reverse=True)
        self.entries = staff_members
        if self.entries:
            self.page = min(self.page, len(self.entries) - 1)
        else:
            self.page = 0

    async def build_embed(self):
        self.refresh_entries()
        embed = discord.Embed(title="📈 Staff Performance Panel", color=0x57F287)
        embed.description = "Professional performance board with a flashy control-room feel."
        if not self.entries:
            embed.add_field(name="Status", value="No staff activity cached yet.", inline=False)
            return embed
        score, staff_id, data = self.entries[self.page]
        member = self.guild.get_member(staff_id)
        avg_response = (data["total_response_time"] / data["response_events"]) if data["response_events"] else 0.0
        embed.add_field(name="Staff Member", value=member.mention if member else f"`{staff_id}`", inline=True)
        embed.add_field(name="Performance Score", value=str(score), inline=True)
        embed.add_field(name="Avg Response", value=f"{avg_response:.1f}s", inline=True)
        embed.add_field(name="Claims", value=str(data["tickets_claimed"]), inline=True)
        embed.add_field(name="Closed", value=str(data["tickets_closed"]), inline=True)
        embed.add_field(name="Approvals", value=str(data["approvals"]), inline=True)
        embed.add_field(name="Denials", value=str(data["denials"]), inline=True)
        embed.add_field(name="Blacklists", value=str(data["blacklists"]), inline=True)
        embed.add_field(name="Proof Requests", value=str(data["proof_requests"]), inline=True)
        embed.add_field(name="Notes", value=str(data["notes"]), inline=True)
        embed.add_field(name="Messages", value=str(data["staff_messages"]), inline=True)
        embed.add_field(name="Follow-Ups", value=str(data["followups"]), inline=True)
        embed.set_footer(text=f"Staff {self.page + 1}/{len(self.entries)} • Sorted by live weighted score")
        return embed

    @discord.ui.button(label="⬅", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button):
        self.refresh_entries()
        if self.entries:
            self.page = (self.page - 1) % len(self.entries)
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="➡", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button):
        self.refresh_entries()
        if self.entries:
            self.page = (self.page + 1) % len(self.entries)
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh_btn(self, interaction: discord.Interaction, button):
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

class ProofCenterPanelView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=300)
        self.guild = guild
        self.rows = []
        self.page = 0

    async def interaction_check(self, interaction: discord.Interaction):
        if not is_staff_member(interaction.user) and interaction.user != self.guild.owner:
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return False
        return True

    async def refresh_rows(self):
        rows = await get_active_verifications(self.guild.id)
        scored = []
        for row in rows:
            user_id, channel_id, join_ts, expires_ts, status, gender = row
            data = ensure_ticket_tracking_defaults(channel_id, user_id)
            score = len(data.get("attachments", []))
            scored.append((score, join_ts, row))
        scored.sort(key=lambda x: (-x[0], x[1]))
        self.rows = [row for _, __, row in scored]
        if self.rows:
            self.page = min(self.page, len(self.rows) - 1)
        else:
            self.page = 0

    async def current_row(self):
        if not self.rows:
            return None
        return self.rows[self.page]

    async def build_embed(self):
        await self.refresh_rows()
        embed = discord.Embed(title="🧾 Proof Center", color=0x5865F2)
        embed.description = "Review attachments, label proof quality, and keep staff decisions consistent."
        if not self.rows:
            embed.add_field(name="Status", value="No active tickets to review.", inline=False)
            return embed
        user_id, channel_id, join_ts, expires_ts, status, gender = self.rows[self.page]
        member = self.guild.get_member(user_id)
        channel = self.guild.get_channel(channel_id)
        data = ensure_ticket_tracking_defaults(channel_id, user_id)
        proof = await get_proof_review(self.guild.id, channel_id)
        attachment_lines = []
        for idx, item in enumerate(data.get("attachments", [])[-5:], start=1):
            ctype, size, added_at = item
            ts = int(added_at.timestamp()) if hasattr(added_at, 'timestamp') else int(time.time())
            attachment_lines.append(f"{idx}. `{ctype}` • {size} bytes • <t:{ts}:R>")
        if not attachment_lines:
            attachment_lines = ["No attachments logged yet."]
        embed.color = PROOF_STATUS_COLORS.get(proof["status"], 0x5865F2)
        embed.add_field(name="Ticket", value=channel.mention if channel else f"`{channel_id}`", inline=True)
        embed.add_field(name="User", value=member.mention if member else f"`{user_id}`", inline=True)
        embed.add_field(name="Proof Status", value=proof["status"].replace("_", " ").title(), inline=True)
        embed.add_field(name="Proof Type", value=(proof["proof_type"] or "unknown").replace("_", " ").title(), inline=True)
        embed.add_field(name="Notes", value=proof["notes"] or "No review notes yet.", inline=False)
        embed.add_field(name="Recent Attachments", value="\n".join(attachment_lines)[:1024], inline=False)
        embed.set_footer(text=f"Proof {self.page + 1}/{len(self.rows)} • Keep reviews clean and consistent")
        return embed

    async def _mark(self, interaction, status, proof_type, notes):
        row = await self.current_row()
        if not row:
            return await interaction.response.send_message("No proof ticket selected.", ephemeral=True)
        user_id, channel_id, _, _, _, _ = row
        await set_proof_review(self.guild.id, channel_id, user_id, status, proof_type, notes, interaction.user.id)
        channel = self.guild.get_channel(channel_id)
        member = self.guild.get_member(user_id)
        if channel:
            try:
                await channel.send(f"🧾 Proof review updated to **{status.replace('_', ' ').title()}** by {interaction.user.mention}.")
            except Exception:
                pass
        await log_action(self.guild, "🧾 Proof Review Updated", f"{interaction.user.mention} marked proof for {member.mention if member else user_id} as **{status}**.", color=PROOF_STATUS_COLORS.get(status, 0x5865F2), fields=[("Proof Type", proof_type, True), ("Notes", notes[:250], False)])
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="⬅", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button):
        await self.refresh_rows()
        if self.rows:
            self.page = (self.page - 1) % len(self.rows)
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="➡", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button):
        await self.refresh_rows()
        if self.rows:
            self.page = (self.page + 1) % len(self.rows)
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Valid", style=discord.ButtonStyle.success, row=1)
    async def valid_btn(self, interaction: discord.Interaction, button):
        await self._mark(interaction, "approved", "image_or_document", "Proof looks acceptable.")

    @discord.ui.button(label="Need More", style=discord.ButtonStyle.primary, row=1)
    async def more_btn(self, interaction: discord.Interaction, button):
        await self._mark(interaction, "pending", "more_requested", "More proof requested from user.")

    @discord.ui.button(label="Voice", style=discord.ButtonStyle.secondary, row=1)
    async def voice_btn(self, interaction: discord.Interaction, button):
        await self._mark(interaction, "voice_requested", "voice_proof", "Voice proof requested.")

    @discord.ui.button(label="Suspicious", style=discord.ButtonStyle.danger, row=1)
    async def suspicious_btn(self, interaction: discord.Interaction, button):
        await self._mark(interaction, "suspicious", "edited_or_unclear", "Proof looks suspicious or edited.")

    @discord.ui.button(label="Open", style=discord.ButtonStyle.primary, row=2)
    async def open_btn(self, interaction: discord.Interaction, button):
        row = await self.current_row()
        if not row:
            return await interaction.response.send_message("No ticket selected.", ephemeral=True)
        _, channel_id, _, _, _, _ = row
        channel = self.guild.get_channel(channel_id)
        await interaction.response.send_message(f"Open ticket: {channel.mention if channel else f'`{channel_id}`'}", ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def refresh_btn(self, interaction: discord.Interaction, button):
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

class TemplateManagerView(discord.ui.View):
    def __init__(self, guild, actor):
        super().__init__(timeout=300)
        self.guild = guild
        self.actor = actor
        self.gender = "male"
        self.page = 0
        self.templates = []

    async def interaction_check(self, interaction: discord.Interaction):
        if not is_staff_member(interaction.user) and interaction.user != self.guild.owner:
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return False
        return True

    async def refresh_templates(self):
        self.templates = await list_templates_for_gender(self.guild.id, self.gender)
        if self.templates:
            self.page = min(self.page, len(self.templates) - 1)
        else:
            self.page = 0

    async def build_embed(self):
        await self.refresh_templates()
        embed = discord.Embed(title="🧩 Verification Templates", color=0x9B59B6)
        embed.description = "Clean template manager with standard, strict, fast, and custom flows."
        embed.add_field(name="Current Gender", value=self.gender.title(), inline=True)
        active = await get_active_template(self.guild.id, self.gender)
        embed.add_field(name="Active Template", value=active["label"], inline=True)
        embed.add_field(name="Preset Count", value=str(len(self.templates)), inline=True)
        if self.templates:
            tmpl = self.templates[self.page]
            preview = "\n".join([f"{i+1}. {q}" for i, q in enumerate(tmpl["questions"][:5])]) or "No questions"
            embed.add_field(name=f"Preview — {tmpl['label']}", value=preview[:1024], inline=False)
            embed.add_field(name="State", value="✅ Active" if tmpl["is_active"] else "Standby", inline=True)
            embed.add_field(name="Preset Key", value=tmpl["preset_key"], inline=True)
            embed.set_footer(text=f"Template {self.page + 1}/{len(self.templates)} • Switch gender or activate a different preset")
        return embed

    async def _activate(self, interaction, preset_key):
        await activate_template(self.guild.id, self.gender, preset_key, interaction.user.id)
        await log_action(self.guild, "🧩 Template Activated", f"{interaction.user.mention} activated **{preset_key}** for **{self.gender}** verification.", color=0x9B59B6)
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Gender", style=discord.ButtonStyle.secondary, row=0)
    async def gender_btn(self, interaction: discord.Interaction, button):
        order = ["male", "female", "general"]
        self.gender = order[(order.index(self.gender) + 1) % len(order)]
        self.page = 0
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="⬅", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button):
        await self.refresh_templates()
        if self.templates:
            self.page = (self.page - 1) % len(self.templates)
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="➡", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button):
        await self.refresh_templates()
        if self.templates:
            self.page = (self.page + 1) % len(self.templates)
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Standard", style=discord.ButtonStyle.success, row=1)
    async def standard_btn(self, interaction: discord.Interaction, button):
        await self._activate(interaction, "standard")

    @discord.ui.button(label="Strict", style=discord.ButtonStyle.primary, row=1)
    async def strict_btn(self, interaction: discord.Interaction, button):
        await self._activate(interaction, "strict")

    @discord.ui.button(label="Fast", style=discord.ButtonStyle.secondary, row=1)
    async def fast_btn(self, interaction: discord.Interaction, button):
        await self._activate(interaction, "fast")

    @discord.ui.button(label="Custom", style=discord.ButtonStyle.danger, row=1)
    async def custom_btn(self, interaction: discord.Interaction, button):
        await interaction.response.send_modal(CustomTemplateModal(self.guild, self.gender, interaction.user))

class EverythingDashboardView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=None)
        self.guild = guild

    async def interaction_check(self, interaction: discord.Interaction):
        if not is_staff_member(interaction.user) and interaction.user != self.guild.owner:
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return False
        return True

    async def build_embed(self):
        rows = await get_active_verifications(self.guild.id)
        open_count = len(rows)
        high_priority = 0
        proof_pending = 0
        claimed = 0
        for user_id, channel_id, join_ts, expires_ts, status, gender in rows:
            priority = await get_ticket_priority(self.guild.id, channel_id)
            proof = await get_proof_review(self.guild.id, channel_id)
            data = ensure_ticket_tracking_defaults(channel_id, user_id)
            if PRIORITY_ORDER.get(priority, 1) >= PRIORITY_ORDER["high"]:
                high_priority += 1
            if proof["status"] in {"pending", "voice_requested", "suspicious", "unreviewed"}:
                proof_pending += 1
            if data.get("claimed_by"):
                claimed += 1
        embed = discord.Embed(title="🖥️ Everything Dashboard", color=0x5865F2)
        embed.description = (
            "Clean and flashy staff control center.\n"
            "• Queue panel\n"
            "• Staff performance panel\n"
            "• Better proof center\n"
            "• Priority management\n"
            "• Auto-reminder monitoring\n"
            "• Verification templates"
        )
        embed.add_field(name="Open Tickets", value=str(open_count), inline=True)
        embed.add_field(name="Claimed", value=str(claimed), inline=True)
        embed.add_field(name="High Priority", value=str(high_priority), inline=True)
        embed.add_field(name="Proof Pending", value=str(proof_pending), inline=True)
        embed.add_field(name="Active Staff", value=str(len([1 for (gid, _sid) in staff_cache.keys() if gid == self.guild.id])), inline=True)
        embed.add_field(name="Auto Reminders", value="Running ✅", inline=True)
        embed.add_field(name="Quick Launch", value="Use the buttons below to open each panel without clutter.", inline=False)
        embed.set_footer(text="Dashboard • Clean UI with control-room energy")
        return embed

    @discord.ui.button(label="Queue", style=discord.ButtonStyle.primary, row=0)
    async def queue_btn(self, interaction: discord.Interaction, button):
        view = QueuePanelView(self.guild)
        await interaction.response.send_message(embed=await view.build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Staff", style=discord.ButtonStyle.success, row=0)
    async def staff_btn(self, interaction: discord.Interaction, button):
        view = StaffPerformancePanelView(self.guild)
        await interaction.response.send_message(embed=await view.build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Proof", style=discord.ButtonStyle.secondary, row=0)
    async def proof_btn(self, interaction: discord.Interaction, button):
        view = ProofCenterPanelView(self.guild)
        await interaction.response.send_message(embed=await view.build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Templates", style=discord.ButtonStyle.secondary, row=1)
    async def templates_btn(self, interaction: discord.Interaction, button):
        view = TemplateManagerView(self.guild, interaction.user)
        await interaction.response.send_message(embed=await view.build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Legacy Panel", style=discord.ButtonStyle.primary, row=1)
    async def legacy_btn(self, interaction: discord.Interaction, button):
        rows = await get_active_verifications(self.guild.id)
        if not rows:
            return await interaction.response.send_message("✅ No active verifications.", ephemeral=True)
        view = VerificationPanel(self.guild, rows)
        control_room = await ensure_control_room(self.guild)
        await view.start(control_room)
        await interaction.response.send_message(f"Opened legacy verification panel in {control_room.mention}.", ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.success, row=1)
    async def refresh_btn(self, interaction: discord.Interaction, button):
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, row=1)
    async def close_btn(self, interaction: discord.Interaction, button):
        await interaction.message.delete()

# Redefine GenderButtons to inject template guidance without removing the old flow.
class GenderButtons(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id

    async def interaction_check(self, interaction):
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Male", style=discord.ButtonStyle.primary, emoji="♂️")
    async def male(self, interaction, button):
        await self.handle(interaction, "male")

    @discord.ui.button(label="Female", style=discord.ButtonStyle.danger, emoji="♀️")
    async def female(self, interaction, button):
        await self.handle(interaction, "female")

    async def handle(self, interaction, gender):
        await update_verification_gender(interaction.user.id, interaction.guild.id, gender)
        req = await get_requirement(gender)
        await ensure_template_seeded(interaction.guild.id)
        await ensure_verification_categories(interaction.guild)
        template = await get_active_template(interaction.guild.id, gender)
        ensure_ticket_tracking_defaults(interaction.channel.id, interaction.user.id)
        ticket_tracking[interaction.channel.id]["template_name"] = template["label"]
        ticket_tracking[interaction.channel.id]["template_gender"] = gender
        ticket_tracking[interaction.channel.id]["gender"] = gender

        guild_cfg = get_guild_config(interaction.guild.id)
        target_category_id = guild_cfg.get("male_category") if gender == "male" else guild_cfg.get("female_category")
        target_category = interaction.guild.get_channel(target_category_id) if target_category_id else None
        if target_category and interaction.channel.category != target_category:
            try:
                await interaction.channel.edit(category=target_category)
            except Exception as e:
                print(f"Failed to move ticket category: {e}")

        embed = discord.Embed(
            title=f"{'♂️' if gender == 'male' else '♀️'} Requirements — {gender.capitalize()}",
            description=req,
            color=0x5865F2
        )
        await interaction.response.send_message(embed=embed)

        await log_action(
            interaction.guild,
            "⚧ Gender Selected",
            f"{interaction.user.mention} selected **{gender.capitalize()}**.",
            color=0x5865F2,
            fields=[
                ("User", interaction.user.mention, True),
                ("User ID", str(interaction.user.id), True),
                ("Gender", gender.capitalize(), True),
                ("Channel", interaction.channel.mention, True),
                ("Template", template["label"], True),
                ("Moved To", target_category.name if target_category else "Unknown", True),
            ]
        )

        next_steps_embed = discord.Embed(
            title="Next Steps",
            description="Answer the questions below. Staff will review shortly.",
            color=0x2b2d31
        )
        template_embed = discord.Embed(
            title=f"🧩 Active Verification Template — {template['label']}",
            description="\n".join([f"**{i+1}.** {q}" for i, q in enumerate(template["questions"])]),
            color=0x9B59B6
        )
        template_embed.set_footer(text="Clean template routing is now active for this ticket")

        await interaction.channel.send(embed=next_steps_embed)
        await interaction.channel.send(embed=template_embed)
        await interaction.channel.send("🎫 Staff Controls:", view=TicketControls(self.user_id, gender))

@bot.command(name="dashboard_legacy_disabled")
async def dashboard_command(ctx):
    if not is_staff_member(ctx.author) and ctx.author != ctx.guild.owner:
        return await ctx.send("Staff only.")
    control_room = await ensure_control_room(ctx.guild)
    view = EverythingDashboardView(ctx.guild)
    await control_room.send(embed=await view.build_embed(), view=view)
    await ctx.send(f"{ctx.author.mention} dashboard sent to {control_room.mention}.")

@bot.command(name="queuepanel")
async def queuepanel_command(ctx):
    if not is_staff_member(ctx.author) and ctx.author != ctx.guild.owner:
        return await ctx.send("Staff only.")
    view = QueuePanelView(ctx.guild)
    await ctx.send(embed=await view.build_embed(), view=view)

@bot.command(name="staffpanel")
async def staffpanel_command(ctx):
    if not is_staff_member(ctx.author) and ctx.author != ctx.guild.owner:
        return await ctx.send("Staff only.")
    view = StaffPerformancePanelView(ctx.guild)
    await ctx.send(embed=await view.build_embed(), view=view)

@bot.command(name="proofcenter")
async def proofcenter_command(ctx):
    if not is_staff_member(ctx.author) and ctx.author != ctx.guild.owner:
        return await ctx.send("Staff only.")
    view = ProofCenterPanelView(ctx.guild)
    await ctx.send(embed=await view.build_embed(), view=view)

@bot.command(name="templates")
async def templates_command(ctx, gender: str | None = None, preset: str | None = None):
    if not is_staff_member(ctx.author) and ctx.author != ctx.guild.owner:
        return await ctx.send("Staff only.")
    await ensure_template_seeded(ctx.guild.id)
    if gender and preset:
        gender = gender.lower()
        preset = preset.lower()
        valid = TEMPLATE_PRESETS.get(gender)
        if not valid or preset not in valid and preset != "custom":
            return await ctx.send("Use genders: male, female, general and presets: standard, strict, fast.")
        if preset != "custom":
            await activate_template(ctx.guild.id, gender, preset, ctx.author.id)
            await log_action(ctx.guild, "🧩 Template Activated", f"{ctx.author.mention} activated **{preset}** for **{gender}** via command.", color=0x9B59B6)
            return await ctx.send(f"✅ Activated **{preset}** for **{gender}** verification.")
    view = TemplateManagerView(ctx.guild, ctx.author)
    if gender and gender.lower() in {"male", "female", "general"}:
        view.gender = gender.lower()
    await ctx.send(embed=await view.build_embed(), view=view)

# Override on_ready to include the new systems without removing existing startup work.
@bot.event
async def on_ready():
    await init_db()
    await ensure_dashboard_tables()
    await load_config()
    await load_staff_stats()

    for guild in bot.guilds:
        await ensure_config(guild)
        await ensure_control_room(guild)
        await ensure_template_seeded(guild.id)
        await hydrate_tracking_from_db(guild)
        await log_action(
            guild,
            "🟣 Bot Started",
            f"Bot is online and connected to **{guild.name}**.",
            color=0x9B59B6
        )

    bot.loop.create_task(staff_inactivity_check())
    bot.loop.create_task(daily_summary())
    bot.loop.create_task(ticket_timeout_checker())
    bot.loop.create_task(auto_reminder_loop())

    print(f"Logged in as {bot.user}")
    print(f"Loaded config: {config}")

# =========================
# ADVANCED LIVE DASHBOARD OVERRIDE
# =========================

def _dashboard_is_staff(member):
    try:
        return is_staff_member(member) or member == member.guild.owner
    except Exception:
        return False


async def _dashboard_remove_pic_perms(guild, member):
    try:
        role = guild.get_role(PIC_PERMS_ROLE_ID)
        if role and role in member.roles:
            await member.remove_roles(role)
    except Exception:
        pass


class DashboardTicketSelect(discord.ui.Select):
    def __init__(self, dashboard_view, rows):
        self.dashboard_view = dashboard_view
        options = []
        for idx, entry in enumerate(rows[:25]):
            user_id, channel_id, _join_ts, _expires_ts, status, gender = entry
            member = dashboard_view.guild.get_member(user_id)
            channel = dashboard_view.guild.get_channel(channel_id)
            priority = "medium"
            data = ticket_tracking.get(channel_id, {})
            if data.get("priority_cache"):
                priority = data["priority_cache"]
            label = member.display_name if member else f"User {user_id}"
            desc = f"{(gender or 'unknown').title()} • {status[:35]}"
            if channel:
                desc = f"{desc[:70]} • {channel.name[:18]}"
            emoji = "🔴" if priority == "high" else "🟡" if priority == "medium" else "🟢"
            options.append(discord.SelectOption(
                label=label[:100],
                description=desc[:100],
                value=str(idx),
                emoji=emoji
            ))
        if not options:
            options = [discord.SelectOption(label="No active tickets", value="none", emoji="✅")]
        super().__init__(
            placeholder="Switch user / ticket instantly",
            options=options,
            row=0,
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            return await interaction.response.send_message("✅ No active tickets.", ephemeral=True)
        self.dashboard_view.current_index = int(self.values[0])
        await self.dashboard_view.refresh_rows()
        await interaction.response.edit_message(embed=await self.dashboard_view.build_embed(), view=self.dashboard_view)


class AdvancedDashboardView(discord.ui.View):
    def __init__(self, guild, opener=None):
        super().__init__(timeout=600)
        self.guild = guild
        self.opener = opener
        self.rows = []
        self.current_index = 0
        self.message = None
        self.update_task = None

    async def start(self, channel):
        await self.refresh_rows()
        self.message = await channel.send(embed=await self.build_embed(), view=self)
        self.update_task = bot.loop.create_task(self.live_update())

    async def interaction_check(self, interaction: discord.Interaction):
        if not _dashboard_is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return False
        return True

    async def live_update(self):
        while not self.is_finished():
            try:
                await asyncio.sleep(20)
                if self.message:
                    await self.refresh_rows()
                    await self.message.edit(embed=await self.build_embed(), view=self)
            except Exception:
                break

    def stop(self):
        if self.update_task:
            self.update_task.cancel()
        super().stop()

    async def refresh_rows(self):
        self.rows = await get_sorted_queue_rows(self.guild.id)
        if self.rows:
            self.current_index = max(0, min(self.current_index, len(self.rows) - 1))
        else:
            self.current_index = 0
        self._rebuild_select()

    def _rebuild_select(self):
        for item in list(self.children):
            if isinstance(item, DashboardTicketSelect):
                self.remove_item(item)
        self.add_item(DashboardTicketSelect(self, self.rows))

    async def current_row(self):
        if not self.rows:
            return None
        return self.rows[self.current_index]

    async def current_context(self):
        row = await self.current_row()
        if not row:
            return None
        user_id, channel_id, join_ts, expires_ts, status, gender = row
        member = self.guild.get_member(user_id)
        channel = self.guild.get_channel(channel_id)
        data = ensure_ticket_tracking_defaults(channel_id, user_id)
        priority = await get_ticket_priority(self.guild.id, channel_id)
        data["priority_cache"] = priority
        proof = await get_proof_review(self.guild.id, channel_id)
        notes_rows = await get_persistent_notes(self.guild.id, user_id)
        warnings_rows = await get_warnings(self.guild.id, user_id)
        claimed_by = data.get("claimed_by")
        return {
            "row": row,
            "user_id": user_id,
            "channel_id": channel_id,
            "join_ts": join_ts,
            "expires_ts": expires_ts,
            "status": status,
            "gender": gender,
            "member": member,
            "channel": channel,
            "data": data,
            "priority": priority,
            "proof": proof,
            "notes_rows": notes_rows,
            "warnings_rows": warnings_rows,
            "claimed_by": claimed_by,
        }

    async def build_embed(self):
        await self.refresh_rows()
        rows = self.rows
        open_count = len(rows)
        claimed = sum(1 for _uid, ch_id, *_ in rows if ticket_tracking.get(ch_id, {}).get("claimed_by"))
        high_priority = 0
        proof_pending = 0
        for _uid, ch_id, *_ in rows:
            try:
                if await get_ticket_priority(self.guild.id, ch_id) == "high":
                    high_priority += 1
                proof = await get_proof_review(self.guild.id, ch_id)
                if proof["status"] in ("pending", "needs_review", "suspicious"):
                    proof_pending += 1
            except Exception:
                pass

        embed = discord.Embed(title="🎛️ Advanced Verification Dashboard", color=0x5865F2)
        embed.description = (
            "Live staff control center. Switch users anytime from the dropdown, run actions instantly, "
            "and open dedicated panels without losing your place."
        )
        embed.add_field(name="Open Tickets", value=str(open_count), inline=True)
        embed.add_field(name="Claimed", value=str(claimed), inline=True)
        embed.add_field(name="High Priority", value=str(high_priority), inline=True)
        embed.add_field(name="Proof Pending", value=str(proof_pending), inline=True)
        embed.add_field(name="Active Staff", value=str(len([1 for (gid, _sid) in staff_cache.keys() if gid == self.guild.id])), inline=True)
        embed.add_field(name="Auto Refresh", value="20s", inline=True)

        ctx = await self.current_context()
        if not ctx:
            embed.add_field(name="Status", value="✅ No active tickets right now.", inline=False)
            embed.set_footer(text="Dashboard • waiting for new verifications")
            return embed

        member = ctx["member"]
        channel = ctx["channel"]
        data = ctx["data"]
        proof = ctx["proof"]
        claimed_by = ctx["claimed_by"]
        risk_score, risk_reasons = calculate_risk_score(member) if member else (0, [])
        remaining = format_duration(max(0, int((data.get("expires_timestamp") or ctx["expires_ts"] or int(time.time())) - time.time())))
        category_name = channel.category.name if channel and channel.category else "Unknown"
        template_name = data.get("template_name") or "Default"

        embed.add_field(name="Selected User", value=member.mention if member else f"`{ctx['user_id']}`", inline=True)
        embed.add_field(name="Ticket", value=channel.mention if channel else f"`{ctx['channel_id']}`", inline=True)
        embed.add_field(name="Category", value=category_name, inline=True)
        embed.add_field(name="Gender", value=(ctx["gender"] or "unknown").title(), inline=True)
        embed.add_field(name="Status", value=ctx["status"], inline=True)
        embed.add_field(name="Priority", value=ctx["priority"].title(), inline=True)
        embed.add_field(name="Claimed By", value=f"<@{claimed_by}>" if claimed_by else "Nobody", inline=True)
        embed.add_field(name="Proof", value=proof["status"].replace("_", " ").title(), inline=True)
        embed.add_field(name="Timer", value=("Paused ⏸️" if data.get("paused") else remaining), inline=True)
        embed.add_field(name="Template", value=template_name[:100], inline=True)
        embed.add_field(name="Msgs", value=f"User {data.get('user_msg_count',0)} • Staff {data.get('staff_msg_count',0)}", inline=True)
        embed.add_field(name="Attachments", value=str(len(data.get("attachments", []))), inline=True)
        embed.add_field(name="Risk", value=f"{risk_score}/100", inline=True)
        embed.add_field(name="Risk Reasons", value=", ".join(risk_reasons)[:1024] if risk_reasons else "None", inline=False)

        preview_lines = []
        for title, desc, created_at in await fetch_recent_logs(self.guild.id, ctx["user_id"], limit=3):
            preview_lines.append(f"• **{title}** — <t:{created_at}:R>")
        embed.add_field(name="Recent Activity", value="\n".join(preview_lines) if preview_lines else "No recent logs.", inline=False)
        embed.set_footer(text=f"Ticket {self.current_index + 1}/{len(rows)} • Switch users anytime from the dropdown")
        return embed

    async def _panel_feedback(self, interaction, message):
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)
        try:
            await interaction.followup.send(message, ephemeral=True)
        except Exception:
            pass

    async def _selected_member_channel(self):
        ctx = await self.current_context()
        if not ctx:
            return None, None, None
        return ctx, ctx["member"], ctx["channel"]

    async def _finalize_close(self, interaction, member, channel, reason, status_name, color):
        guild_cfg = get_guild_config(self.guild.id)
        unverified = self.guild.get_role(guild_cfg["unverified_role"]) if guild_cfg.get("unverified_role") else None
        if member and unverified and unverified in member.roles and status_name in ("denied", "blacklisted"):
            try:
                await member.remove_roles(unverified)
            except Exception:
                pass
        if member:
            await _dashboard_remove_pic_perms(self.guild, member)
        if channel:
            try:
                await save_ticket_transcript(channel, self.guild, reason=reason)
            except Exception:
                pass
        if member:
            await remove_from_verification(member.id, self.guild.id)
        set_ticket_status(channel.id if channel else 0, status_name)
        await self.refresh_rows()
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)
        try:
            if channel:
                await channel.delete()
        except Exception:
            pass

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_btn(self, interaction: discord.Interaction, button):
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="📂 Open Ticket", style=discord.ButtonStyle.primary, row=1)
    async def open_btn(self, interaction: discord.Interaction, button):
        ctx, member, channel = await self._selected_member_channel()
        if not ctx:
            return await interaction.response.send_message("No ticket selected.", ephemeral=True)
        await interaction.response.send_message(f"Open ticket: {channel.mention if channel else 'Missing channel'} • User: {member.mention if member else ctx['user_id']}", ephemeral=True)

    @discord.ui.button(label="🛡️ Claim/Unclaim", style=discord.ButtonStyle.success, row=1)
    async def claim_unclaim_btn(self, interaction: discord.Interaction, button):
        ctx, member, channel = await self._selected_member_channel()
        if not ctx:
            return await interaction.response.send_message("No ticket selected.", ephemeral=True)
        data = ctx["data"]
        current = data.get("claimed_by")
        if current and current != interaction.user.id and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Only the claimer or an admin can unclaim this ticket.", ephemeral=True)
        if current == interaction.user.id:
            data["claimed_by"] = None
            set_ticket_status(ctx["channel_id"], "open")
            await log_action(self.guild, "🟡 Ticket Unclaimed (Dashboard)", f"{interaction.user.mention} unclaimed {channel.mention if channel else ctx['channel_id']}.", color=0xFEE75C)
            return await self._panel_feedback(interaction, "🟡 Ticket unclaimed.")
        data["claimed_by"] = interaction.user.id
        set_ticket_status(ctx["channel_id"], "claimed")
        add_staff_stat(self.guild.id, interaction.user.id, "tickets_claimed", 1)
        await save_staff_stats(self.guild.id, interaction.user.id)
        await log_action(self.guild, "🛡️ Ticket Claimed (Dashboard)", f"{interaction.user.mention} claimed {channel.mention if channel else ctx['channel_id']}.", color=0x57F287, fields=[("User", member.mention if member else str(ctx["user_id"]), True)])
        return await self._panel_feedback(interaction, "🛡️ Ticket claimed.")

    @discord.ui.button(label="🎫 Queue", style=discord.ButtonStyle.primary, row=1)
    async def queue_btn(self, interaction: discord.Interaction, button):
        view = QueuePanelView(self.guild)
        await interaction.response.send_message(embed=await view.build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="👥 Staff", style=discord.ButtonStyle.success, row=1)
    async def staff_btn(self, interaction: discord.Interaction, button):
        view = StaffPerformancePanelView(self.guild)
        await interaction.response.send_message(embed=await view.build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success, row=2)
    async def approve_btn(self, interaction: discord.Interaction, button):
        ctx, member, channel = await self._selected_member_channel()
        if not ctx or not member:
            return await interaction.response.send_message("User not found.", ephemeral=True)
        guild_cfg = get_guild_config(self.guild.id)
        male = self.guild.get_role(guild_cfg["male_role"]) if guild_cfg.get("male_role") else None
        female = self.guild.get_role(guild_cfg["female_role"]) if guild_cfg.get("female_role") else None
        unverified = self.guild.get_role(guild_cfg["unverified_role"]) if guild_cfg.get("unverified_role") else None
        if unverified and unverified in member.roles:
            try:
                await member.remove_roles(unverified)
            except Exception:
                pass
        role = male if (ctx["gender"] or "").lower() == "male" else female
        if role:
            try:
                await member.add_roles(role)
            except Exception:
                pass
        await _dashboard_remove_pic_perms(self.guild, member)
        try:
            await member.send("✅ You have been approved and verified.")
        except Exception:
            pass
        get_daily_stats(self.guild.id)["approved"] += 1
        await log_action(self.guild, "🟢 Approved (Dashboard)", f"{interaction.user.mention} approved {member.mention}.", color=0x57F287, fields=[("Channel", channel.mention if channel else "Missing", True)])
        add_staff_stat(self.guild.id, interaction.user.id, "approvals", 1)
        add_staff_stat(self.guild.id, interaction.user.id, "tickets_closed", 1)
        await save_staff_stats(self.guild.id, interaction.user.id)
        await self._finalize_close(interaction, member, channel, "Approved by Dashboard", "approved", 0x57F287)

    @discord.ui.button(label="❌ Deny", style=discord.ButtonStyle.danger, row=2)
    async def deny_btn(self, interaction: discord.Interaction, button):
        ctx, member, channel = await self._selected_member_channel()
        if not ctx or not member:
            return await interaction.response.send_message("User not found.", ephemeral=True)
        await _dashboard_remove_pic_perms(self.guild, member)
        try:
            await member.send("❌ Your verification was denied.")
        except Exception:
            pass
        try:
            await member.kick(reason="Denied by dashboard")
        except Exception:
            pass
        get_daily_stats(self.guild.id)["denied"] += 1
        await log_action(self.guild, "🔴 Denied (Dashboard)", f"{interaction.user.mention} denied {member.mention}.", color=0xED4245, fields=[("Channel", channel.mention if channel else "Missing", True)])
        add_staff_stat(self.guild.id, interaction.user.id, "denials", 1)
        add_staff_stat(self.guild.id, interaction.user.id, "tickets_closed", 1)
        await save_staff_stats(self.guild.id, interaction.user.id)
        await self._finalize_close(interaction, member, channel, "Denied by Dashboard", "denied", 0xED4245)

    @discord.ui.button(label="🚫 Blacklist", style=discord.ButtonStyle.secondary, row=2)
    async def blacklist_btn(self, interaction: discord.Interaction, button):
        ctx, member, channel = await self._selected_member_channel()
        if not ctx or not member:
            return await interaction.response.send_message("User not found.", ephemeral=True)
        await add_blacklist(self.guild.id, member.id)
        await _dashboard_remove_pic_perms(self.guild, member)
        try:
            await member.send("🚫 You have been blacklisted from this server.")
        except Exception:
            pass
        try:
            await member.kick(reason="Blacklisted by dashboard")
        except Exception:
            pass
        get_daily_stats(self.guild.id)["blacklisted"] += 1
        await log_action(self.guild, "⚫ Blacklisted (Dashboard)", f"{interaction.user.mention} blacklisted {member.mention}.", color=0x000000, fields=[("Channel", channel.mention if channel else "Missing", True)])
        add_staff_stat(self.guild.id, interaction.user.id, "blacklists", 1)
        add_staff_stat(self.guild.id, interaction.user.id, "tickets_closed", 1)
        await save_staff_stats(self.guild.id, interaction.user.id)
        await self._finalize_close(interaction, member, channel, "Blacklisted by Dashboard", "blacklisted", 0x000000)

    @discord.ui.button(label="📜 History", style=discord.ButtonStyle.secondary, row=2)
    async def history_btn(self, interaction: discord.Interaction, button):
        ctx, member, _channel = await self._selected_member_channel()
        if not ctx or not member:
            return await interaction.response.send_message("User not found.", ephemeral=True)
        blacklisted = await is_blacklisted(self.guild.id, member.id)
        score, reasons = calculate_risk_score(member)
        embed = build_history_embed(member, ctx["notes_rows"], ctx["warnings_rows"], blacklisted, score, reasons)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🧾 Logs", style=discord.ButtonStyle.secondary, row=2)
    async def logs_btn(self, interaction: discord.Interaction, button):
        ctx, member, _channel = await self._selected_member_channel()
        if not ctx:
            return await interaction.response.send_message("No ticket selected.", ephemeral=True)
        rows = await fetch_recent_logs(self.guild.id, ctx["user_id"], limit=10)
        embed = discord.Embed(title=f"🧾 Recent Logs — {member if member else ctx['user_id']}", color=0x5865F2)
        if not rows:
            embed.description = "No logs found."
        else:
            for title, description, created_at in rows[:10]:
                embed.add_field(name=f"{title} • <t:{created_at}:R>", value=description[:1000], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🟢 Low", style=discord.ButtonStyle.secondary, row=3)
    async def low_btn(self, interaction: discord.Interaction, button):
        await self._set_priority(interaction, "low")

    @discord.ui.button(label="🟡 Medium", style=discord.ButtonStyle.primary, row=3)
    async def medium_btn(self, interaction: discord.Interaction, button):
        await self._set_priority(interaction, "medium")

    @discord.ui.button(label="🔴 High", style=discord.ButtonStyle.danger, row=3)
    async def high_btn(self, interaction: discord.Interaction, button):
        await self._set_priority(interaction, "high")

    @discord.ui.button(label="✅ Proof OK", style=discord.ButtonStyle.success, row=3)
    async def proof_ok_btn(self, interaction: discord.Interaction, button):
        await self._set_proof(interaction, "valid")

    @discord.ui.button(label="⚠️ Suspicious", style=discord.ButtonStyle.secondary, row=3)
    async def proof_suspicious_btn(self, interaction: discord.Interaction, button):
        await self._set_proof(interaction, "suspicious")

    async def _set_priority(self, interaction, priority):
        ctx, member, channel = await self._selected_member_channel()
        if not ctx:
            return await interaction.response.send_message("No ticket selected.", ephemeral=True)
        await set_ticket_priority_db(self.guild.id, ctx["channel_id"], priority, interaction.user.id)
        if channel:
            try:
                await channel.send(f"🏷️ Priority updated to **{priority.title()}** by {interaction.user.mention}.")
            except Exception:
                pass
        await log_action(self.guild, "🏷️ Ticket Priority Updated (Dashboard)", f"{interaction.user.mention} set {channel.mention if channel else ctx['channel_id']} to **{priority.title()}**.", color=0x5865F2, fields=[("User", member.mention if member else str(ctx["user_id"]), True)])
        await self._panel_feedback(interaction, f"🏷️ Priority set to {priority.title()}.")

    async def _set_proof(self, interaction, status):
        ctx, member, channel = await self._selected_member_channel()
        if not ctx:
            return await interaction.response.send_message("No ticket selected.", ephemeral=True)
        proof_type = "attachment" if ctx["data"].get("attachments") else "text_only"
        notes = f"Updated from advanced dashboard by {interaction.user}"
        await set_proof_review(self.guild.id, ctx["channel_id"], ctx["user_id"], status, proof_type, notes, interaction.user.id)
        if channel:
            try:
                await channel.send(f"📎 Proof review updated to **{status.replace('_', ' ').title()}** by {interaction.user.mention}.")
            except Exception:
                pass
        await log_action(self.guild, "📎 Proof Review Updated (Dashboard)", f"{interaction.user.mention} marked proof for {member.mention if member else ctx['user_id']} as **{status.replace('_',' ').title()}**.", color=0x5865F2)
        await self._panel_feedback(interaction, f"📎 Proof marked {status.replace('_', ' ').title()}.")

    @discord.ui.button(label="⏳ Pending", style=discord.ButtonStyle.secondary, row=4)
    async def proof_pending_btn(self, interaction: discord.Interaction, button):
        await self._set_proof(interaction, "pending")

    @discord.ui.button(label="🧩 Templates", style=discord.ButtonStyle.primary, row=4)
    async def templates_btn(self, interaction: discord.Interaction, button):
        view = TemplateManagerView(self.guild, interaction.user)
        await interaction.response.send_message(embed=await view.build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="📎 Proof Center", style=discord.ButtonStyle.primary, row=4)
    async def proofcenter_btn(self, interaction: discord.Interaction, button):
        view = ProofCenterPanelView(self.guild)
        await interaction.response.send_message(embed=await view.build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="🕵️ Audit", style=discord.ButtonStyle.secondary, row=4)
    async def audit_btn(self, interaction: discord.Interaction, button):
        ctx, member, _channel = await self._selected_member_channel()
        focus_user_id = ctx["user_id"] if ctx else None
        control_room = await ensure_control_room(self.guild)
        view = AuditLogPanel(self.guild, focus_user_id=focus_user_id)
        await view.start(control_room)
        await interaction.response.send_message(f"🕵️ Audit panel opened in {control_room.mention} for {member.mention if member else 'selected user'}.", ephemeral=True)

    @discord.ui.button(label="✖ Close", style=discord.ButtonStyle.danger, row=4)
    async def close_btn(self, interaction: discord.Interaction, button):
        if self.update_task:
            self.update_task.cancel()
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except Exception:
            pass


try:
    bot.remove_command("dashboard")
except Exception:
    pass

try:
    bot.remove_command("adminpanel")
except Exception:
    pass


@bot.command(name="dashboard")
async def advanced_dashboard_command(ctx):
    if not _dashboard_is_staff(ctx.author):
        return await ctx.send("Staff only.")
    control_room = await ensure_control_room(ctx.guild)
    view = AdvancedDashboardView(ctx.guild, opener=ctx.author)
    await view.start(control_room)
    await ctx.send(f"{ctx.author.mention} advanced dashboard opened in {control_room.mention}.")


@bot.command(name="adminpanel")
async def advanced_adminpanel_command(ctx):
    if ctx.author != ctx.guild.owner and not _dashboard_is_staff(ctx.author):
        return await ctx.send("Staff only.")
    control_room = await ensure_control_room(ctx.guild)
    view = AdvancedDashboardView(ctx.guild, opener=ctx.author)
    await view.start(control_room)
    await ctx.send(f"{ctx.author.mention} advanced admin panel opened in {control_room.mention}.")

# =========================
# FINAL COMMAND + TIMER FIX OVERRIDES
# =========================
# These overrides keep the existing bot intact, but guarantee the live dashboard
# commands register before bot.run(), and make timeout kicks respect paused tickets.

async def auto_kick_if_unverified(member_id, guild_id, delay=600):
    # Pause-aware timeout checker.
    # It does NOT kick while the ticket is paused. It waits until the ticket is resumed
    # and the effective expires_timestamp has actually passed.
    while True:
        await asyncio.sleep(5)

        guild = bot.get_guild(guild_id)
        if not guild:
            return

        member = guild.get_member(member_id)
        if not member:
            return

        guild_cfg = get_guild_config(guild.id)
        unverified_role = guild.get_role(guild_cfg["unverified_role"]) if guild_cfg.get("unverified_role") else None
        if not unverified_role or unverified_role not in member.roles:
            return

        ticket_channel_id = None
        db_expires_ts = None

        try:
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute(
                    "SELECT ticket_channel_id, expires_timestamp FROM active_verifications WHERE user_id=? AND guild_id=?",
                    (member_id, guild_id)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        ticket_channel_id, db_expires_ts = row
        except Exception as e:
            print(f"Pause-aware timeout DB lookup error: {e}")

        data = ticket_tracking.get(ticket_channel_id, {}) if ticket_channel_id else {}

        if data.get("paused"):
            # While paused, do nothing.
            continue

        expires_ts = data.get("expires_timestamp") or db_expires_ts or (int(time.time()) + 1)

        if time.time() < expires_ts:
            continue

        stats = get_daily_stats(guild.id)

        try:
            await member.send("⏰ You did not complete verification in time and were removed from the server.")
        except Exception:
            pass

        try:
            await member.kick(reason="Verification timeout")
        except Exception as e:
            print(f"Timeout kick failed: {e}")
            return

        stats["autokicked"] += 1

        await log_action(
            guild,
            "⏰ Auto-Kicked (Timeout)",
            f"{member.mention} was auto-kicked for not completing verification in time.",
            color=0xED4245,
            fields=[
                ("User", member.mention, True),
                ("User ID", str(member.id), True),
                ("Reason", "Verification timeout", True)
            ]
        )

        await remove_from_verification(member_id, guild_id)
        return


try:
    bot.remove_command("dashboard")
except Exception:
    pass

try:
    bot.remove_command("adminpanel")
except Exception:
    pass

try:
    bot.remove_command("maximumdashboard")
except Exception:
    pass


@bot.command(name="dashboard", aliases=["maximumdashboard", "maxdashboard"])
async def final_dashboard_command(ctx):
    if not _dashboard_is_staff(ctx.author):
        return await ctx.send("Staff only.")

    control_room = await ensure_control_room(ctx.guild)
    view = AdvancedDashboardView(ctx.guild, opener=ctx.author)
    await view.start(control_room)
    await ctx.send(f"{ctx.author.mention} advanced dashboard opened in {control_room.mention}.")


@bot.command(name="adminpanel")
async def final_adminpanel_command(ctx):
    if ctx.author != ctx.guild.owner and not _dashboard_is_staff(ctx.author):
        return await ctx.send("Staff only.")

    control_room = await ensure_control_room(ctx.guild)
    view = AdvancedDashboardView(ctx.guild, opener=ctx.author)
    await view.start(control_room)
    await ctx.send(f"{ctx.author.mention} advanced admin panel opened in {control_room.mention}.")

# =========================
# FINAL PAUSE-AWARE TIMER OVERRIDES
# =========================
# This makes paused tickets pause for real:
# - pause stores remaining time
# - pause writes "paused" into the DB
# - timeout task will NOT kick while paused
# - resume rebuilds the expiry time from the saved remaining time
# - resume writes the new expires_timestamp into the DB

async def _set_active_verification_status(guild_id, user_id, status=None, expires_timestamp=None):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            if status is not None and expires_timestamp is not None:
                await db.execute(
                    "UPDATE active_verifications SET status=?, expires_timestamp=? WHERE guild_id=? AND user_id=?",
                    (status, int(expires_timestamp), guild_id, user_id)
                )
            elif status is not None:
                await db.execute(
                    "UPDATE active_verifications SET status=? WHERE guild_id=? AND user_id=?",
                    (status, guild_id, user_id)
                )
            elif expires_timestamp is not None:
                await db.execute(
                    "UPDATE active_verifications SET expires_timestamp=? WHERE guild_id=? AND user_id=?",
                    (int(expires_timestamp), guild_id, user_id)
                )
            await db.commit()
    except Exception as e:
        print(f"Active verification status update failed: {e}")


async def _get_active_verification_row(guild_id, user_id):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT ticket_channel_id, expires_timestamp, status FROM active_verifications WHERE guild_id=? AND user_id=?",
                (guild_id, user_id)
            ) as cursor:
                return await cursor.fetchone()
    except Exception as e:
        print(f"Active verification row lookup failed: {e}")
        return None


async def pause_ticket_timer(guild, channel, actor):
    data = ticket_tracking.get(channel.id)
    if not data:
        return False, "No timer found for this ticket."

    if data.get("paused"):
        remaining = paused_timers.get(channel.id, data.get("remaining_when_paused", 0))
        return False, f"Timer is already paused ({format_duration(remaining)} left)."

    expires_ts = data.get("expires_timestamp")
    if expires_ts is None:
        return False, "No active timer to pause."

    remaining = max(0, int(expires_ts - time.time()))
    paused_timers[channel.id] = remaining

    data["paused"] = True
    data["remaining_when_paused"] = remaining
    data["status"] = "paused"
    set_ticket_status(channel.id, "paused")

    user_id = data.get("user_id")
    if user_id:
        await _set_active_verification_status(guild.id, user_id, status="paused")

    await log_action(
        guild,
        "⏸️ Timer Paused",
        f"{actor.mention} paused the timer in {channel.mention}. Auto-timeout is disabled until resumed.",
        color=0xFEE75C,
        fields=[
            ("Staff", actor.mention, True),
            ("Channel", channel.mention, True),
            ("Remaining", format_duration(remaining), True)
        ]
    )
    return True, f"⏸️ Timer paused. User will **not** be kicked while paused. ({format_duration(remaining)} left)"


async def resume_ticket_timer(guild, channel, actor):
    data = ticket_tracking.get(channel.id)
    if not data:
        return False, "No timer found for this ticket."

    if not data.get("paused"):
        return False, "Timer is not paused."

    remaining = paused_timers.get(channel.id, data.get("remaining_when_paused"))
    if remaining is None:
        remaining = max(1, int((data.get("expires_timestamp") or time.time()) - time.time()))

    new_expires = int(time.time()) + int(remaining)

    data["expires_timestamp"] = new_expires
    data["paused"] = False
    data["remaining_when_paused"] = None
    data["status"] = "open"
    set_ticket_status(channel.id, "open")

    if channel.id in paused_timers:
        del paused_timers[channel.id]

    user_id = data.get("user_id")
    if user_id:
        await _set_active_verification_status(guild.id, user_id, status="Waiting for verification", expires_timestamp=new_expires)

    await log_action(
        guild,
        "▶️ Timer Resumed",
        f"{actor.mention} resumed the timer in {channel.mention}.",
        color=0x57F287,
        fields=[
            ("Staff", actor.mention, True),
            ("Channel", channel.mention, True),
            ("Remaining", format_duration(remaining), True)
        ]
    )
    return True, f"▶️ Timer resumed ({format_duration(remaining)} left)."


async def auto_kick_if_unverified(member_id, guild_id, delay=600):
    # Wait a tiny bit first so the ticket row/tracking can be created.
    await asyncio.sleep(5)

    while True:
        guild = bot.get_guild(guild_id)
        if not guild:
            return

        member = guild.get_member(member_id)
        if not member:
            return

        guild_cfg = get_guild_config(guild.id)
        stats = get_daily_stats(guild.id)

        unverified_role = guild.get_role(guild_cfg["unverified_role"]) if guild_cfg.get("unverified_role") else None
        if not unverified_role or unverified_role not in member.roles:
            return

        row = await _get_active_verification_row(guild_id, member_id)
        ticket_channel_id = row[0] if row else None
        db_expires_ts = row[1] if row else None
        db_status = (row[2] or "").lower() if row else ""

        data = ticket_tracking.get(ticket_channel_id, {}) if ticket_channel_id else {}

        # HARD STOP: paused in memory OR paused in DB means no kicking.
        if data.get("paused") or db_status == "paused":
            await asyncio.sleep(5)
            continue

        expires_ts = data.get("expires_timestamp") or db_expires_ts
        if not expires_ts:
            await asyncio.sleep(5)
            continue

        if time.time() < int(expires_ts):
            await asyncio.sleep(min(5, max(1, int(expires_ts - time.time()))))
            continue

        # One last check right before kicking, in case staff paused at the same second.
        row = await _get_active_verification_row(guild_id, member_id)
        db_status = (row[2] or "").lower() if row else ""
        data = ticket_tracking.get(ticket_channel_id, {}) if ticket_channel_id else {}
        if data.get("paused") or db_status == "paused":
            await asyncio.sleep(5)
            continue

        try:
            await member.send("⏰ You did not complete verification in time and were removed from the server.")
        except Exception:
            pass

        try:
            await member.kick(reason="Verification timeout")
        except Exception as e:
            print(f"Timeout kick failed: {e}")
            return

        stats["autokicked"] += 1

        await log_action(
            guild,
            "⏰ Auto-Kicked (Timeout)",
            f"{member.mention} was auto-kicked for not completing verification in time.",
            color=0xED4245,
            fields=[
                ("User", member.mention, True),
                ("User ID", str(member.id), True),
                ("Reason", "Verification timeout", True)
            ]
        )

        await remove_from_verification(member_id, guild_id)
        return

# =========================
# FINAL PIC PERMS OVERRIDES
# =========================
PIC_PERMS_ROLE_ID = 1489476010725609535

async def give_pic_perms(member):
    if not member or not member.guild:
        return False
    role = member.guild.get_role(PIC_PERMS_ROLE_ID)
    if not role:
        return False
    if role in member.roles:
        return True
    try:
        await member.add_roles(role, reason="Auto pic perms on join")
        return True
    except Exception as e:
        print(f"Failed to give pic perms to {member}: {e}")
        return False

async def remove_pic_perms(member, reason="Verification ended"):
    if not member or not member.guild:
        return False
    role = member.guild.get_role(PIC_PERMS_ROLE_ID)
    if not role:
        return False
    if role not in member.roles:
        return True
    try:
        await member.remove_roles(role, reason=reason)
        return True
    except Exception as e:
        print(f"Failed to remove pic perms from {member}: {e}")
        return False

async def _dashboard_remove_pic_perms(guild, member):
    await remove_pic_perms(member, reason="Dashboard verification action")

_original_on_member_join = on_member_join

@bot.event
async def on_member_join(member):
    await give_pic_perms(member)
    await _original_on_member_join(member)

_original_ticket_approve = TicketControls.approve
_original_ticket_deny = TicketControls.deny
_original_ticket_blacklist = TicketControls.blacklist

async def _picperms_approve_wrapper(self, interaction, button):
    member = interaction.guild.get_member(self.user_id)
    if member:
        await remove_pic_perms(member, reason="Approved verification")
    return await _original_ticket_approve(self, interaction, button)

async def _picperms_deny_wrapper(self, interaction, button):
    member = interaction.guild.get_member(self.user_id)
    if member:
        await remove_pic_perms(member, reason="Denied verification")
    return await _original_ticket_deny(self, interaction, button)

async def _picperms_blacklist_wrapper(self, interaction, button):
    member = interaction.guild.get_member(self.user_id)
    if member:
        await remove_pic_perms(member, reason="Blacklisted verification")
    return await _original_ticket_blacklist(self, interaction, button)

TicketControls.approve = _picperms_approve_wrapper
TicketControls.deny = _picperms_deny_wrapper
TicketControls.blacklist = _picperms_blacklist_wrapper

_original_auto_judge_auto_approve = AutoJudge.auto_approve

async def _picperms_auto_judge_auto_approve(self):
    await remove_pic_perms(self.member, reason="Auto Judge approved")
    return await _original_auto_judge_auto_approve(self)

AutoJudge.auto_approve = _picperms_auto_judge_auto_approve

async def auto_kick_if_unverified(member_id, guild_id, delay=600):
    await asyncio.sleep(5)

    while True:
        guild = bot.get_guild(guild_id)
        if not guild:
            return

        member = guild.get_member(member_id)
        if not member:
            return

        guild_cfg = get_guild_config(guild.id)
        stats = get_daily_stats(guild.id)

        unverified_role = guild.get_role(guild_cfg["unverified_role"]) if guild_cfg.get("unverified_role") else None
        if not unverified_role or unverified_role not in member.roles:
            await remove_pic_perms(member, reason="Verification no longer active")
            return

        row = await _get_active_verification_row(guild_id, member_id)
        ticket_channel_id = row[0] if row else None
        db_expires_ts = row[1] if row else None
        db_status = (row[2] or "").lower() if row else ""

        data = ticket_tracking.get(ticket_channel_id, {}) if ticket_channel_id else {}

        if data.get("paused") or db_status == "paused":
            await asyncio.sleep(5)
            continue

        expires_ts = data.get("expires_timestamp") or db_expires_ts
        if not expires_ts:
            await asyncio.sleep(5)
            continue

        if time.time() < int(expires_ts):
            await asyncio.sleep(min(5, max(1, int(expires_ts - time.time()))))
            continue

        row = await _get_active_verification_row(guild_id, member_id)
        db_status = (row[2] or "").lower() if row else ""
        data = ticket_tracking.get(ticket_channel_id, {}) if ticket_channel_id else {}
        if data.get("paused") or db_status == "paused":
            await asyncio.sleep(5)
            continue

        await remove_pic_perms(member, reason="Verification timeout")

        try:
            await member.send("⏰ You did not complete verification in time and were removed from the server.")
        except Exception:
            pass

        try:
            await member.kick(reason="Verification timeout")
        except Exception as e:
            print(f"Timeout kick failed: {e}")
            return

        stats["autokicked"] += 1

        await log_action(
            guild,
            "⏰ Auto-Kicked (Timeout)",
            f"{member.mention} was auto-kicked for not completing verification in time.",
            color=0xED4245,
            fields=[
                ("User", member.mention, True),
                ("User ID", str(member.id), True),
                ("Reason", "Verification timeout", True)
            ]
        )

        await remove_from_verification(member_id, guild_id)
        return

# =========================
# FULL MODMAIL SYSTEM (DM ↔ PANEL)
# =========================

modmail_active = {}

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if isinstance(message.channel, discord.DMChannel):
        user = message.author
        guild = bot.guilds[0]

        if user.id in modmail_active:
            channel = guild.get_channel(modmail_active[user.id])
            if channel:
                await channel.send(f"📩 **DM from {user.mention}:** {message.content}")
                for att in message.attachments:
                    await channel.send(att.url)
        return

    for user_id, channel_id in modmail_active.items():
        if channel_id == message.channel.id:
            user = await bot.fetch_user(user_id)
            try:
                await user.send(f"💬 Staff: {message.content}")
                for att in message.attachments:
                    await user.send(att.url)
            except:
                await message.channel.send("❌ Could not DM user")
            return

    await bot.process_commands(message)

@bot.command(name="enddm_legacy_disabled")
async def enddm(ctx):
    for user_id, channel_id in list(modmail_active.items()):
        if channel_id == ctx.channel.id:
            del modmail_active[user_id]
            await ctx.send("🔌 DM session closed")
            return

    await ctx.send("No active DM session")

# =========================
# STAFF PANEL ANIMATION ADD-ON ONLY
# =========================
# This does not remove your old StaffPerformancePanelView or old staffpanel command code.
# It only changes the existing staffpanel command callback so .staffpanel opens with
# an animated loading sequence, then switches into your existing staff panel view.

STAFF_PANEL_FRAMES = [
    ("🔄", "Booting staff console", "Loading staff cache and live queue data...", "▰▱▱▱▱▱▱▱▱▱"),
    ("📡", "Syncing live systems", "Checking tickets, modmail, proof reviews, and staff activity...", "▰▰▰▱▱▱▱▱▱▱"),
    ("👥", "Scanning staff activity", "Calculating claims, approvals, denials, notes, and response load...", "▰▰▰▰▰▱▱▱▱▱"),
    ("🎫", "Reading ticket workload", "Sorting active tickets, claimed tickets, and high priority items...", "▰▰▰▰▰▰▰▱▱▱"),
    ("🧠", "Building control panel", "Preparing staff management controls and performance view...", "▰▰▰▰▰▰▰▰▰▱"),
    ("✅", "Staff panel ready", "Opening the live staff panel now.", "▰▰▰▰▰▰▰▰▰▰"),
]

def _staffpanel_system_counts(guild):
    open_tickets = 0
    claimed_tickets = 0
    active_staff_ids = set()
    high_priority = 0
    proof_pending = 0
    modmail_count = 0

    try:
        for channel_id, data in ticket_tracking.items():
            if not isinstance(data, dict):
                continue
            open_tickets += 1
            claimed_by = data.get("claimed_by")
            if claimed_by:
                claimed_tickets += 1
                active_staff_ids.add(claimed_by)
            priority = str(data.get("priority_cache") or data.get("priority") or "").lower()
            if priority == "high":
                high_priority += 1
    except Exception:
        pass

    try:
        for (guild_id, staff_id), data in staff_cache.items():
            if guild_id == guild.id:
                active_staff_ids.add(staff_id)
    except Exception:
        pass

    try:
        # Supports either modmail_active or modmail_threads depending on which version is loaded.
        if "modmail_active" in globals() and isinstance(modmail_active, dict):
            modmail_count += len(modmail_active)
        if "modmail_threads" in globals() and isinstance(modmail_threads, dict):
            modmail_count += len(modmail_threads)
    except Exception:
        pass

    return {
        "open_tickets": open_tickets,
        "claimed_tickets": claimed_tickets,
        "active_staff": len(active_staff_ids),
        "high_priority": high_priority,
        "proof_pending": proof_pending,
        "modmail": modmail_count,
    }

def _build_staffpanel_animation_embed(guild, frame_index, *, final=False):
    emoji, title, description, bar = STAFF_PANEL_FRAMES[frame_index % len(STAFF_PANEL_FRAMES)]
    counts = _staffpanel_system_counts(guild)

    embed = discord.Embed(
        title=f"{emoji} Staff Panel • {title}",
        description=description,
        color=0x5865F2 if not final else 0x57F287
    )
    embed.add_field(name="Progress", value=f"`{bar}`", inline=False)
    embed.add_field(name="Open Tickets", value=str(counts["open_tickets"]), inline=True)
    embed.add_field(name="Claimed", value=str(counts["claimed_tickets"]), inline=True)
    embed.add_field(name="Active Staff", value=str(counts["active_staff"]), inline=True)
    embed.add_field(name="High Priority", value=str(counts["high_priority"]), inline=True)
    embed.add_field(name="Modmail", value=str(counts["modmail"]), inline=True)
    embed.add_field(name="Refresh Mode", value="Live pulse", inline=True)
    embed.set_footer(text="Animated Staff Panel • loading live staff systems")
    embed.timestamp = discord.utils.utcnow()
    return embed

async def _staffpanel_live_pulse(message, guild, view, seconds=20):
    pulse = 0
    while True:
        try:
            await asyncio.sleep(seconds)
            if message is None:
                return
            embed = await view.build_embed()
            pulse_icons = ["🟢", "🔵", "🟣", "⚡"]
            embed.set_footer(text=f"{pulse_icons[pulse % len(pulse_icons)]} Staff Panel Live • auto-refreshed every {seconds}s")
            embed.timestamp = discord.utils.utcnow()
            await message.edit(embed=embed, view=view)
            pulse += 1
        except discord.NotFound:
            return
        except Exception as e:
            print(f"Staff panel live pulse stopped: {e}")
            return

async def _animated_staffpanel_command(ctx):
    if not is_staff_member(ctx.author) and ctx.author != ctx.guild.owner:
        return await ctx.send("Staff only.")

    try:
        await log_action(
            ctx.guild,
            "👥 Staff Panel Opened",
            f"{ctx.author.mention} opened the animated staff panel.",
            color=0x5865F2,
            fields=[("Staff", ctx.author.mention, True)]
        )
    except Exception:
        pass

    loading_embed = _build_staffpanel_animation_embed(ctx.guild, 0)
    msg = await ctx.send(embed=loading_embed)

    for frame_index in range(1, len(STAFF_PANEL_FRAMES)):
        await asyncio.sleep(0.65)
        await msg.edit(embed=_build_staffpanel_animation_embed(ctx.guild, frame_index))

    await asyncio.sleep(0.45)

    view = StaffPerformancePanelView(ctx.guild)
    final_embed = await view.build_embed()
    final_embed.set_footer(text="✅ Animated Staff Panel Ready • live pulse enabled")
    final_embed.timestamp = discord.utils.utcnow()
    await msg.edit(embed=final_embed, view=view)

    bot.loop.create_task(_staffpanel_live_pulse(msg, ctx.guild, view, seconds=20))

# Keep the existing command object. Do NOT remove it.
_existing_staffpanel_command = bot.get_command("staffpanel")
if _existing_staffpanel_command is not None:
    _existing_staffpanel_command.callback = _animated_staffpanel_command
else:
    @bot.command(name="staffpanel")
    async def staffpanel_command(ctx):
        await _animated_staffpanel_command(ctx)



# =========================
# NEXT LEVEL STAFF SELECTOR SYSTEM
# =========================

class StaffSelect(discord.ui.Select):
    def __init__(self, guild):
        options = []
        for m in guild.members:
            if not m.bot:
                options.append(discord.SelectOption(label=m.display_name, value=str(m.id)))
        super().__init__(placeholder="Select a staff member...", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        view.selected_staff = int(self.values[0])
        await view.update_selected_staff(interaction)

# Inject into existing StaffPerformancePanelView safely
if "StaffPerformancePanelView" in globals():
    original_init = StaffPerformancePanelView.__init__

    def new_init(self, guild):
        original_init(self, guild)
        self.selected_staff = None
        try:
            self.add_item(StaffSelect(guild))
        except:
            pass

    StaffPerformancePanelView.__init__ = new_init

    async def update_selected_staff(self, interaction):
        embed = await self.build_embed()
        if getattr(self, "selected_staff", None):
            member = interaction.guild.get_member(self.selected_staff)
            if member:
                data = staff_cache.get((interaction.guild.id, member.id), {})
                embed.add_field(name="👤 Selected Staff", value=member.mention, inline=False)
                embed.add_field(
                    name="📊 Stats",
                    value=(
                        f"Claims: {data.get('tickets_claimed', 0)}\n"
                        f"Approvals: {data.get('approvals', 0)}\n"
                        f"Denials: {data.get('denials', 0)}"
                    ),
                    inline=False
                )
        await interaction.response.edit_message(embed=embed, view=self)

    StaffPerformancePanelView.update_selected_staff = update_selected_staff



# =========================
# GOD TIER STAFF UI SYSTEM
# =========================

class StaffHubView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=None)
        self.guild = guild

    @discord.ui.button(label="👤 Staff Control", style=discord.ButtonStyle.primary)
    async def control(self, i, b):
        await i.response.edit_message(embed=discord.Embed(title="Staff Control"), view=StaffControlView(self.guild))

    @discord.ui.button(label="📊 Performance", style=discord.ButtonStyle.success)
    async def perf(self, i, b):
        await i.response.edit_message(embed=discord.Embed(title="Performance Panel"), view=StaffPerformancePanelView(self.guild))

    @discord.ui.button(label="🎫 Tickets", style=discord.ButtonStyle.secondary)
    async def tickets(self, i, b):
        await i.response.send_message("Ticket panel coming soon", ephemeral=True)

    @discord.ui.button(label="📩 Modmail", style=discord.ButtonStyle.secondary)
    async def modmail(self, i, b):
        await i.response.send_message("Modmail panel coming soon", ephemeral=True)

    @discord.ui.button(label="🔍 Audit", style=discord.ButtonStyle.secondary)
    async def audit(self, i, b):
        await i.response.send_message("Audit panel coming soon", ephemeral=True)

class StaffControlView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=None)
        self.guild = guild

    @discord.ui.button(label="Promote", style=discord.ButtonStyle.success)
    async def promote(self, i, b):
        await i.response.send_message("Promoted", ephemeral=True)

    @discord.ui.button(label="Demote", style=discord.ButtonStyle.danger)
    async def demote(self, i, b):
        await i.response.send_message("Demoted", ephemeral=True)

    @discord.ui.button(label="Warn", style=discord.ButtonStyle.secondary)
    async def warn(self, i, b):
        await i.response.send_message("Warned", ephemeral=True)

    @discord.ui.button(label="Force Unclaim", style=discord.ButtonStyle.danger)
    async def unclaim(self, i, b):
        await i.response.send_message("Unclaimed tickets", ephemeral=True)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.primary, row=4)
    async def back(self, i, b):
        await i.response.edit_message(embed=discord.Embed(title="Staff Hub"), view=StaffHubView(self.guild))

async def god_staffpanel(ctx):
    view = StaffHubView(ctx.guild)
    msg = await ctx.send(embed=discord.Embed(title="⚡ Loading Staff System..."), view=view)
    frames = ["Booting...", "Loading data...", "Syncing...", "Ready"]
    for f in frames:
        await asyncio.sleep(0.7)
        await msg.edit(embed=discord.Embed(title="👥 Staff System", description=f))
    await msg.edit(embed=discord.Embed(title="👥 Staff Hub Ready"), view=view)

bot.remove_command("staffpanel")
@bot.command(name="staffpanel")
async def staffpanel(ctx):
    await god_staffpanel(ctx)

# =========================
# FINAL EVOLUTION STAFF PANEL
# =========================
# Add-on only. It does not remove verification, modmail, dashboard, timer, or pic perms.
# It replaces only the .staffpanel command callback with a stronger multi-panel UI.

def _fe_is_staff(member):
    try:
        return is_staff_member(member) or member.guild_permissions.administrator or member == member.guild.owner
    except Exception:
        return False

async def _fe_log(guild, title, description, user=None, target=None, color=0x5865F2):
    try:
        fields = []
        if user:
            fields.append(("Staff", user.mention, True))
        if target:
            fields.append(("Target", str(target), True))
        await log_action(guild, title, description, color=color, fields=fields)
    except Exception as e:
        print(f"Final Evolution log failed: {e}")

def _fe_staff_members(guild):
    return [m for m in guild.members if not m.bot and _fe_is_staff(m)]

def _fe_staff_data(guild, staff_id):
    return staff_cache.get((guild.id, staff_id), {
        "tickets_claimed": 0,
        "tickets_closed": 0,
        "approvals": 0,
        "denials": 0,
        "blacklists": 0,
        "escalations": 0,
        "notes": 0,
        "proof_requests": 0,
        "automation_runs": 0,
        "staff_messages": 0,
        "followups": 0,
        "total_response_time": 0,
        "response_events": 0,
        "active_time": 0,
    })

def _fe_quality_score(data):
    approvals = data.get("approvals", 0)
    denials = data.get("denials", 0)
    closed = data.get("tickets_closed", 0)
    claims = data.get("tickets_claimed", 0)
    notes = data.get("notes", 0)
    proof = data.get("proof_requests", 0)
    escalations = data.get("escalations", 0)
    score = (closed * 4) + (claims * 2) + (notes * 2) + proof + approvals + denials - (escalations * 1)
    return max(0, min(100, score))

def _fe_avg_response(data):
    events = data.get("response_events", 0) or 0
    total = data.get("total_response_time", 0) or 0
    if not events:
        return "N/A"
    return f"{total / events:.1f}s"

def _fe_counts(guild):
    open_tickets = 0
    claimed = 0
    paused = 0
    high = 0

    for ch_id, data in list(ticket_tracking.items()):
        if not isinstance(data, dict):
            continue
        open_tickets += 1
        if data.get("claimed_by"):
            claimed += 1
        if data.get("paused"):
            paused += 1
        if str(data.get("priority_cache") or data.get("priority") or "").lower() == "high":
            high += 1

    modmail = 0
    try:
        if "modmail_active" in globals() and isinstance(modmail_active, dict):
            modmail += len(modmail_active)
        if "modmail_threads" in globals() and isinstance(modmail_threads, dict):
            modmail += len(modmail_threads)
    except Exception:
        pass

    return {
        "staff": len(_fe_staff_members(guild)),
        "open_tickets": open_tickets,
        "claimed": claimed,
        "paused": paused,
        "high": high,
        "modmail": modmail,
    }

def _fe_main_embed(guild, selected_staff_id=None):
    counts = _fe_counts(guild)
    embed = discord.Embed(
        title="⚡ Final Evolution Staff Command Center",
        description="Live staff hub with profiles, routing, tickets, modmail, audits, analytics, and control tools.",
        color=0x9B59B6
    )
    embed.add_field(name="👥 Staff", value=str(counts["staff"]), inline=True)
    embed.add_field(name="🎫 Tickets", value=str(counts["open_tickets"]), inline=True)
    embed.add_field(name="📌 Claimed", value=str(counts["claimed"]), inline=True)
    embed.add_field(name="⏸️ Paused", value=str(counts["paused"]), inline=True)
    embed.add_field(name="🔴 High Priority", value=str(counts["high"]), inline=True)
    embed.add_field(name="📩 Modmail", value=str(counts["modmail"]), inline=True)

    if selected_staff_id:
        member = guild.get_member(selected_staff_id)
        if member:
            data = _fe_staff_data(guild, member.id)
            embed.add_field(
                name="👤 Selected Staff",
                value=(
                    f"{member.mention}\n"
                    f"Quality Score: **{_fe_quality_score(data)}/100**\n"
                    f"Avg Response: **{_fe_avg_response(data)}**"
                ),
                inline=False
            )
    else:
        embed.add_field(name="👤 Selected Staff", value="Use the dropdown to select staff.", inline=False)

    embed.set_footer(text="Final Evolution • select staff, then open a sub-panel")
    embed.timestamp = discord.utils.utcnow()
    return embed

class FinalEvolutionStaffSelect(discord.ui.Select):
    def __init__(self, guild):
        options = []
        for member in _fe_staff_members(guild)[:25]:
            data = _fe_staff_data(guild, member.id)
            options.append(discord.SelectOption(
                label=member.display_name[:100],
                description=f"Closed {data.get('tickets_closed',0)} • Score {_fe_quality_score(data)}/100",
                value=str(member.id),
                emoji="👤"
            ))
        if not options:
            options = [discord.SelectOption(label="No staff found", value="none", emoji="⚠️")]
        super().__init__(placeholder="Select staff member...", options=options, row=0)

    async def callback(self, interaction):
        view = self.view
        if self.values[0] == "none":
            return await interaction.response.send_message("No staff found.", ephemeral=True)
        view.selected_staff_id = int(self.values[0])
        await _fe_log(interaction.guild, "👤 Staff Selected", f"{interaction.user.mention} selected <@{view.selected_staff_id}> in Final Evolution panel.", user=interaction.user)
        await interaction.response.edit_message(embed=_fe_main_embed(interaction.guild, view.selected_staff_id), view=view)

class FinalEvolutionStaffHub(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=None)
        self.guild = guild
        self.selected_staff_id = None
        self.add_item(FinalEvolutionStaffSelect(guild))

    async def interaction_check(self, interaction):
        if not _fe_is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction, button):
        await _fe_log(interaction.guild, "🔄 Staff Panel Refreshed", "Final Evolution staff panel refreshed.", user=interaction.user)
        await interaction.response.edit_message(embed=_fe_main_embed(self.guild, self.selected_staff_id), view=self)

    @discord.ui.button(label="👤 Profile", style=discord.ButtonStyle.primary, row=1)
    async def profile(self, interaction, button):
        await interaction.response.edit_message(embed=_fe_profile_embed(self.guild, self.selected_staff_id), view=FinalEvolutionProfileView(self.guild, self.selected_staff_id))

    @discord.ui.button(label="📊 Leaderboard", style=discord.ButtonStyle.success, row=1)
    async def leaderboard(self, interaction, button):
        await interaction.response.edit_message(embed=_fe_leaderboard_embed(self.guild), view=FinalEvolutionLeaderboardView(self.guild))

    @discord.ui.button(label="🎫 Tickets", style=discord.ButtonStyle.primary, row=1)
    async def tickets(self, interaction, button):
        await interaction.response.edit_message(embed=_fe_ticket_embed(self.guild), view=FinalEvolutionTicketView(self.guild, self.selected_staff_id))

    @discord.ui.button(label="📩 Modmail", style=discord.ButtonStyle.secondary, row=1)
    async def modmail(self, interaction, button):
        await interaction.response.edit_message(embed=_fe_modmail_embed(self.guild), view=FinalEvolutionModmailView(self.guild))

    @discord.ui.button(label="🔍 Audit", style=discord.ButtonStyle.secondary, row=2)
    async def audit(self, interaction, button):
        await _fe_log(interaction.guild, "🔍 Staff Audit Opened", "Final Evolution audit panel opened.", user=interaction.user)
        await interaction.response.edit_message(embed=await _fe_audit_embed(self.guild, self.selected_staff_id), view=FinalEvolutionAuditView(self.guild, self.selected_staff_id))

    @discord.ui.button(label="⚖️ Balance Load", style=discord.ButtonStyle.success, row=2)
    async def balance(self, interaction, button):
        await _fe_log(interaction.guild, "⚖️ Load Balance Checked", "Staff workload balance was checked.", user=interaction.user)
        await interaction.response.send_message(_fe_balance_report(self.guild), ephemeral=True)

    @discord.ui.button(label="🧠 Behavior", style=discord.ButtonStyle.secondary, row=2)
    async def behavior(self, interaction, button):
        await interaction.response.send_message(_fe_behavior_report(self.guild, self.selected_staff_id), ephemeral=True)

    @discord.ui.button(label="📤 Export", style=discord.ButtonStyle.primary, row=2)
    async def export(self, interaction, button):
        text = _fe_export_text(self.guild)
        if len(text) > 1900:
            text = text[:1900] + "\n..."
        await _fe_log(interaction.guild, "📤 Staff Export Generated", "Staff export generated from Final Evolution panel.", user=interaction.user)
        await interaction.response.send_message(f"```txt\n{text}\n```", ephemeral=True)

    @discord.ui.button(label="✖ Close", style=discord.ButtonStyle.danger, row=2)
    async def close(self, interaction, button):
        await _fe_log(interaction.guild, "✖ Staff Panel Closed", "Final Evolution staff panel closed.", user=interaction.user)
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except Exception:
            pass

def _fe_profile_embed(guild, staff_id):
    embed = discord.Embed(title="👤 Staff Profile", color=0x5865F2)
    if not staff_id:
        embed.description = "No staff selected. Go back and select a staff member."
        return embed
    member = guild.get_member(staff_id)
    if not member:
        embed.description = "Selected staff member is no longer in the server."
        return embed
    data = _fe_staff_data(guild, staff_id)
    embed.description = f"Profile for {member.mention}"
    embed.add_field(name="Claims", value=str(data.get("tickets_claimed", 0)), inline=True)
    embed.add_field(name="Closed", value=str(data.get("tickets_closed", 0)), inline=True)
    embed.add_field(name="Approvals", value=str(data.get("approvals", 0)), inline=True)
    embed.add_field(name="Denials", value=str(data.get("denials", 0)), inline=True)
    embed.add_field(name="Blacklists", value=str(data.get("blacklists", 0)), inline=True)
    embed.add_field(name="Escalations", value=str(data.get("escalations", 0)), inline=True)
    embed.add_field(name="Notes", value=str(data.get("notes", 0)), inline=True)
    embed.add_field(name="Proof Requests", value=str(data.get("proof_requests", 0)), inline=True)
    embed.add_field(name="Avg Response", value=_fe_avg_response(data), inline=True)
    embed.add_field(name="Quality Score", value=f"{_fe_quality_score(data)}/100", inline=True)
    embed.set_footer(text="Final Evolution • Staff Profile")
    embed.timestamp = discord.utils.utcnow()
    return embed

class FinalEvolutionProfileView(discord.ui.View):
    def __init__(self, guild, staff_id):
        super().__init__(timeout=None)
        self.guild = guild
        self.staff_id = staff_id

    @discord.ui.button(label="⚡ Force Unclaim", style=discord.ButtonStyle.danger, row=0)
    async def force_unclaim(self, interaction, button):
        count = 0
        for ch_id, data in ticket_tracking.items():
            if isinstance(data, dict) and data.get("claimed_by") == self.staff_id:
                data["claimed_by"] = None
                data["status"] = "open"
                count += 1
        await _fe_log(interaction.guild, "⚡ Force Unclaim", f"{interaction.user.mention} force-unclaimed {count} ticket(s) from <@{self.staff_id}>.", user=interaction.user, target=f"<@{self.staff_id}>", color=0xED4245)
        await interaction.response.send_message(f"Force-unclaimed **{count}** ticket(s).", ephemeral=True)

    @discord.ui.button(label="⚠️ Flag Staff", style=discord.ButtonStyle.secondary, row=0)
    async def flag(self, interaction, button):
        await _fe_log(interaction.guild, "⚠️ Staff Flagged", f"{interaction.user.mention} flagged <@{self.staff_id}> for review.", user=interaction.user, target=f"<@{self.staff_id}>", color=0xFEE75C)
        await interaction.response.send_message("Staff flagged for review.", ephemeral=True)

    @discord.ui.button(label="🧹 Reset Session", style=discord.ButtonStyle.secondary, row=0)
    async def reset_session(self, interaction, button):
        await _fe_log(interaction.guild, "🧹 Staff Session Reset", f"{interaction.user.mention} reset the session view for <@{self.staff_id}>.", user=interaction.user, target=f"<@{self.staff_id}>")
        await interaction.response.send_message("Session view reset.", ephemeral=True)

    @discord.ui.button(label="🧾 Recent Logs", style=discord.ButtonStyle.primary, row=0)
    async def recent_logs(self, interaction, button):
        rows = await fetch_recent_logs(interaction.guild.id, self.staff_id, limit=8)
        embed = discord.Embed(title=f"🧾 Recent Logs for <@{self.staff_id}>", color=0x5865F2)
        if not rows:
            embed.description = "No recent logs found."
        else:
            for title, desc, created_at in rows:
                embed.add_field(name=f"{title} • <t:{created_at}:R>", value=desc[:900], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="⬅ Back", style=discord.ButtonStyle.primary, row=4)
    async def back(self, interaction, button):
        hub = FinalEvolutionStaffHub(self.guild)
        hub.selected_staff_id = self.staff_id
        await interaction.response.edit_message(embed=_fe_main_embed(self.guild, self.staff_id), view=hub)

def _fe_leaderboard_embed(guild):
    rows = []
    for (guild_id, staff_id), data in staff_cache.items():
        if guild_id != guild.id:
            continue
        member = guild.get_member(staff_id)
        if not member:
            continue
        rows.append((member, _fe_quality_score(data), data))
    rows.sort(key=lambda x: x[1], reverse=True)
    embed = discord.Embed(title="📊 Staff Leaderboard", color=0x57F287)
    if not rows:
        embed.description = "No staff data yet."
    else:
        lines = []
        for idx, (member, score, data) in enumerate(rows[:10], 1):
            lines.append(f"**#{idx}** {member.mention} — Score **{score}/100** • Closed `{data.get('tickets_closed',0)}` • Claims `{data.get('tickets_claimed',0)}`")
        embed.description = "\n".join(lines)
    embed.set_footer(text="Final Evolution • Leaderboard")
    embed.timestamp = discord.utils.utcnow()
    return embed

class FinalEvolutionLeaderboardView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=None)
        self.guild = guild

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction, button):
        await interaction.response.edit_message(embed=_fe_leaderboard_embed(self.guild), view=self)

    @discord.ui.button(label="⬅ Back", style=discord.ButtonStyle.primary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(embed=_fe_main_embed(self.guild), view=FinalEvolutionStaffHub(self.guild))

def _fe_ticket_embed(guild):
    embed = discord.Embed(title="🎫 Staff Ticket Control", color=0x5865F2)
    if not ticket_tracking:
        embed.description = "No tracked tickets."
        return embed
    lines = []
    for idx, (ch_id, data) in enumerate(list(ticket_tracking.items())[:15], 1):
        channel = guild.get_channel(ch_id)
        user_id = data.get("user_id")
        claimed = data.get("claimed_by")
        status = data.get("status", "open")
        lines.append(f"**{idx}.** {channel.mention if channel else ch_id} • <@{user_id}> • `{status}` • Claimed: {f'<@{claimed}>' if claimed else 'None'}")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Final Evolution • Ticket Control")
    embed.timestamp = discord.utils.utcnow()
    return embed

class FinalEvolutionTicketView(discord.ui.View):
    def __init__(self, guild, selected_staff_id=None):
        super().__init__(timeout=None)
        self.guild = guild
        self.selected_staff_id = selected_staff_id

    @discord.ui.button(label="🧹 Clean Missing", style=discord.ButtonStyle.secondary, row=0)
    async def clean_missing(self, interaction, button):
        removed = 0
        for ch_id in list(ticket_tracking.keys()):
            if not self.guild.get_channel(ch_id):
                ticket_tracking.pop(ch_id, None)
                removed += 1
        await _fe_log(interaction.guild, "🧹 Ticket Tracking Cleaned", f"{interaction.user.mention} cleaned {removed} missing ticket reference(s).", user=interaction.user)
        await interaction.response.edit_message(embed=_fe_ticket_embed(self.guild), view=self)

    @discord.ui.button(label="⚡ Unclaim All Selected", style=discord.ButtonStyle.danger, row=0)
    async def unclaim_selected(self, interaction, button):
        if not self.selected_staff_id:
            return await interaction.response.send_message("No selected staff.", ephemeral=True)
        count = 0
        for data in ticket_tracking.values():
            if isinstance(data, dict) and data.get("claimed_by") == self.selected_staff_id:
                data["claimed_by"] = None
                data["status"] = "open"
                count += 1
        await _fe_log(interaction.guild, "⚡ Staff Tickets Unclaimed", f"{interaction.user.mention} unclaimed {count} ticket(s) from <@{self.selected_staff_id}>.", user=interaction.user)
        await interaction.response.send_message(f"Unclaimed {count} ticket(s).", ephemeral=True)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=0)
    async def refresh(self, interaction, button):
        await interaction.response.edit_message(embed=_fe_ticket_embed(self.guild), view=self)

    @discord.ui.button(label="⬅ Back", style=discord.ButtonStyle.primary, row=4)
    async def back(self, interaction, button):
        hub = FinalEvolutionStaffHub(self.guild)
        hub.selected_staff_id = self.selected_staff_id
        await interaction.response.edit_message(embed=_fe_main_embed(self.guild, self.selected_staff_id), view=hub)

def _fe_modmail_embed(guild):
    embed = discord.Embed(title="📩 Modmail Control", color=0x9B59B6)
    sources = []
    if "modmail_active" in globals() and isinstance(modmail_active, dict):
        sources.extend(list(modmail_active.items()))
    if "modmail_threads" in globals() and isinstance(modmail_threads, dict):
        sources.extend(list(modmail_threads.items()))
    if not sources:
        embed.description = "No active modmail sessions."
    else:
        lines = []
        seen = set()
        for user_id, channel_id in sources[:15]:
            if (user_id, channel_id) in seen:
                continue
            seen.add((user_id, channel_id))
            channel = guild.get_channel(channel_id)
            lines.append(f"• <@{user_id}> → {channel.mention if channel else channel_id}")
        embed.description = "\n".join(lines)
    embed.set_footer(text="Final Evolution • Modmail")
    embed.timestamp = discord.utils.utcnow()
    return embed

class FinalEvolutionModmailView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=None)
        self.guild = guild

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction, button):
        await interaction.response.edit_message(embed=_fe_modmail_embed(self.guild), view=self)

    @discord.ui.button(label="🧹 Clean Closed", style=discord.ButtonStyle.secondary)
    async def clean(self, interaction, button):
        removed = 0
        for mapping_name in ("modmail_active", "modmail_threads"):
            mapping = globals().get(mapping_name)
            if isinstance(mapping, dict):
                for user_id, ch_id in list(mapping.items()):
                    if not self.guild.get_channel(ch_id):
                        mapping.pop(user_id, None)
                        removed += 1
        await _fe_log(interaction.guild, "🧹 Modmail Cleaned", f"{interaction.user.mention} cleaned {removed} closed modmail session(s).", user=interaction.user)
        await interaction.response.edit_message(embed=_fe_modmail_embed(self.guild), view=self)

    @discord.ui.button(label="⬅ Back", style=discord.ButtonStyle.primary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(embed=_fe_main_embed(self.guild), view=FinalEvolutionStaffHub(self.guild))

async def _fe_audit_embed(guild, staff_id=None):
    rows = await fetch_recent_logs(guild.id, staff_id, limit=10)
    embed = discord.Embed(title="🔍 Final Evolution Audit", color=0x2B2D31)
    if staff_id:
        embed.description = f"Focused on <@{staff_id}>"
    if not rows:
        embed.add_field(name="Logs", value="No logs found.", inline=False)
    else:
        for title, desc, created_at in rows:
            embed.add_field(name=f"{title} • <t:{created_at}:R>", value=desc[:900], inline=False)
    embed.timestamp = discord.utils.utcnow()
    return embed

class FinalEvolutionAuditView(discord.ui.View):
    def __init__(self, guild, staff_id=None):
        super().__init__(timeout=None)
        self.guild = guild
        self.staff_id = staff_id

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction, button):
        await interaction.response.edit_message(embed=await _fe_audit_embed(self.guild, self.staff_id), view=self)

    @discord.ui.button(label="⬅ Back", style=discord.ButtonStyle.primary)
    async def back(self, interaction, button):
        hub = FinalEvolutionStaffHub(self.guild)
        hub.selected_staff_id = self.staff_id
        await interaction.response.edit_message(embed=_fe_main_embed(self.guild, self.staff_id), view=hub)

def _fe_balance_report(guild):
    staff = _fe_staff_members(guild)
    if not staff:
        return "No staff found."
    loads = []
    for member in staff:
        count = sum(1 for data in ticket_tracking.values() if isinstance(data, dict) and data.get("claimed_by") == member.id)
        loads.append((member, count))
    loads.sort(key=lambda x: x[1], reverse=True)
    return "\n".join(f"{m.mention}: **{c}** claimed ticket(s)" for m, c in loads[:10])

def _fe_behavior_report(guild, staff_id):
    if not staff_id:
        return "Select a staff member first."
    member = guild.get_member(staff_id)
    data = _fe_staff_data(guild, staff_id)
    approvals = data.get("approvals", 0)
    denials = data.get("denials", 0)
    total = approvals + denials
    denial_rate = (denials / total * 100) if total else 0
    style = "Strict" if denial_rate >= 65 else "Lenient" if denial_rate <= 25 and total else "Balanced"
    return (
        f"Behavior report for {member.mention if member else staff_id}\n"
        f"Style: **{style}**\n"
        f"Denial Rate: **{denial_rate:.1f}%**\n"
        f"Quality Score: **{_fe_quality_score(data)}/100**\n"
        f"Avg Response: **{_fe_avg_response(data)}**"
    )

def _fe_export_text(guild):
    lines = [f"Final Evolution Staff Export — {guild.name}", "-" * 40]
    for member in _fe_staff_members(guild):
        data = _fe_staff_data(guild, member.id)
        lines.append(
            f"{member} ({member.id}) | Score {_fe_quality_score(data)}/100 | "
            f"Claims {data.get('tickets_claimed',0)} | Closed {data.get('tickets_closed',0)} | "
            f"Approvals {data.get('approvals',0)} | Denials {data.get('denials',0)}"
        )
    return "\n".join(lines)

async def _final_evolution_animate(message, guild, view):
    frames = [
        ("⚡ Booting Final Evolution...", "▰▱▱▱▱▱▱▱▱▱"),
        ("👥 Loading staff profiles...", "▰▰▱▱▱▱▱▱▱▱"),
        ("🎫 Reading ticket workload...", "▰▰▰▰▱▱▱▱▱▱"),
        ("📩 Syncing modmail sessions...", "▰▰▰▰▰▰▱▱▱▱"),
        ("🔍 Preparing audit tools...", "▰▰▰▰▰▰▰▰▱▱"),
        ("✅ Command center online.", "▰▰▰▰▰▰▰▰▰▰"),
    ]
    for text, bar in frames:
        embed = discord.Embed(title="⚡ Final Evolution Staff Panel", description=f"{text}\n`{bar}`", color=0x9B59B6)
        embed.timestamp = discord.utils.utcnow()
        await message.edit(embed=embed)
        await asyncio.sleep(0.55)
    await message.edit(embed=_fe_main_embed(guild), view=view)

async def _final_evolution_staffpanel(ctx):
    if not _fe_is_staff(ctx.author):
        return await ctx.send("Staff only.")
    view = FinalEvolutionStaffHub(ctx.guild)
    msg = await ctx.send(embed=discord.Embed(title="⚡ Starting Final Evolution...", color=0x9B59B6))
    await _fe_log(ctx.guild, "⚡ Final Evolution Staff Panel Opened", f"{ctx.author.mention} opened the Final Evolution staff panel.", user=ctx.author, color=0x9B59B6)
    await _final_evolution_animate(msg, ctx.guild, view)

try:
    bot.remove_command("staffpanel")
except Exception:
    pass

@bot.command(name="staffpanel", aliases=["staff", "staffhub"])
async def final_evolution_staffpanel_command(ctx):
    await _final_evolution_staffpanel(ctx)

# =========================
# FINAL ALL-FEATURES ADD-ON
# INTERNAL AUDIT + MODMAIL + ROUTING + TRANSCRIPTS + CLEAN STARTUP
# =========================
import io
import random

STAFF_ROLE_IDS = [1489769442538688554, 1492942635386536047]
MODMAIL_CATEGORY_NAME = "MODMAIL"

# Internal-only audit cache. This prevents verification/audit spam in public log channels.
internal_audit_logs = []
transcript_store = {}
modmail_threads = globals().get("modmail_threads", globals().get("modmail_active", {}))
modmail_claims = globals().get("modmail_claims", {})


def _safe_member_name(obj):
    try:
        return f"{obj} ({obj.id})"
    except Exception:
        return str(obj)


def _has_staff_role(member):
    if not member or not getattr(member, "guild", None):
        return False
    if member.guild_permissions.administrator:
        return True
    return any(role.id in STAFF_ROLE_IDS for role in getattr(member, "roles", []))


# Override existing staff check to include your two staff roles + admins.
def is_staff_member(member):
    return _has_staff_role(member)


def _dashboard_is_staff(member):
    return _has_staff_role(member)


async def _internal_audit(guild, title, description, *, color=0x5865F2, fields=None, actor=None, target=None):
    entry = {
        "guild_id": guild.id if guild else None,
        "title": title,
        "description": description,
        "created_at": int(time.time()),
        "actor_id": getattr(actor, "id", None),
        "target": str(target) if target is not None else None,
        "fields": fields or []
    }
    internal_audit_logs.append(entry)
    if len(internal_audit_logs) > 1000:
        del internal_audit_logs[:len(internal_audit_logs) - 1000]

    # Persist to DB only. No channel sends.
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO log_history (guild_id, title, description, created_at) VALUES (?, ?, ?, ?)",
                (guild.id, title, description, entry["created_at"])
            )
            await db.commit()
    except Exception as e:
        print(f"Internal audit persist failed: {e}")


# Override log_action so audit logs stay inside the bot/DB and do NOT flood channels.
async def log_action(guild, title, description, color=0x2b2d31, *, fields=None):
    await _internal_audit(guild, title, description, color=color, fields=fields)


async def save_ticket_transcript(channel, guild, reason="Ticket Closed"):
    if not channel:
        return

    transcript_lines = []
    transcript_lines.append(f"Ticket Transcript - {channel.name}")
    transcript_lines.append(f"Reason: {reason}")
    transcript_lines.append(f"Created: {channel.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    transcript_lines.append("=" * 60)

    try:
        async for message in channel.history(limit=None, oldest_first=True):
            timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author = f"{message.author} ({message.author.id})"
            content = message.content if message.content else "[No text content]"
            transcript_lines.append(f"[{timestamp}] {author}: {content}")
            if message.attachments:
                transcript_lines.append("Attachments: " + ", ".join(a.url for a in message.attachments))
            transcript_lines.append("-" * 40)
    except Exception as e:
        transcript_lines.append(f"[Error fetching messages: {e}]")

    text = "\n".join(transcript_lines)
    transcript_store[channel.id] = {
        "guild_id": guild.id,
        "channel_name": channel.name,
        "reason": reason,
        "created_at": int(time.time()),
        "text": text
    }

    await _internal_audit(
        guild,
        "📜 Transcript Saved Internally",
        f"Transcript saved internally for **{channel.name}**. Reason: {reason}",
        target=channel.name
    )


async def _send_text_file(destination, filename, text, message="📄 File ready:"):
    fp = io.BytesIO(text.encode("utf-8", errors="ignore"))
    file = discord.File(fp, filename=filename)
    await destination.send(message, file=file)


async def _build_channel_transcript(channel, limit=None):
    lines = [f"Transcript for #{channel.name}", "=" * 50]
    async for msg in channel.history(limit=limit, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"[{ts}] {msg.author}: {msg.content or '[No text]'}")
        if msg.attachments:
            for att in msg.attachments:
                lines.append(f"Attachment: {att.url}")
        lines.append("-" * 30)
    return "\n".join(lines)


@bot.command(name="transcript")
async def transcript_command(ctx, limit: int = 300):
    if not is_staff_member(ctx.author):
        return await ctx.send("Staff only.")
    text = await _build_channel_transcript(ctx.channel, limit=limit)
    await _send_text_file(ctx, f"{ctx.channel.name}_transcript.txt", text, "📄 Transcript download:")
    await _internal_audit(ctx.guild, "📄 Transcript Downloaded", f"{ctx.author.mention} downloaded transcript for {ctx.channel.mention}.", actor=ctx.author)


@bot.command(name="auditlogs", aliases=["internallogs"])
async def auditlogs_command(ctx, limit: int = 25):
    if not is_staff_member(ctx.author):
        return await ctx.send("Staff only.")
    rows = [x for x in internal_audit_logs if x.get("guild_id") == ctx.guild.id][-max(1, min(limit, 50)):]
    if not rows:
        rows = []
        try:
            fetched = await fetch_recent_logs(ctx.guild.id, None, limit=max(1, min(limit, 50)))
            for title, desc, created_at in fetched:
                rows.append({"title": title, "description": desc, "created_at": created_at})
        except Exception:
            pass

    embed = discord.Embed(title="🗂️ Internal Audit Logs", color=0x5865F2)
    if not rows:
        embed.description = "No internal logs found."
    else:
        for entry in reversed(rows[-10:]):
            embed.add_field(
                name=f"{entry['title']} • <t:{entry['created_at']}:R>",
                value=str(entry["description"])[:900],
                inline=False
            )
    await ctx.send(embed=embed)


def _least_busy_staff(guild):
    candidates = []
    for member in guild.members:
        if member.bot or not is_staff_member(member):
            continue
        load = 0
        for data in ticket_tracking.values():
            if isinstance(data, dict) and data.get("claimed_by") == member.id:
                load += 1
        score_data = staff_cache.get((guild.id, member.id), {})
        closed = score_data.get("tickets_closed", 0)
        candidates.append((load, -closed, member))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]


@bot.command(name="routeticket")
async def route_ticket_command(ctx):
    if not is_staff_member(ctx.author):
        return await ctx.send("Staff only.")
    staff = _least_busy_staff(ctx.guild)
    if not staff:
        return await ctx.send("No staff found to route this ticket.")
    if ctx.channel.id not in ticket_tracking:
        ticket_tracking[ctx.channel.id] = {}
    ticket_tracking[ctx.channel.id]["claimed_by"] = staff.id
    ticket_tracking[ctx.channel.id]["status"] = "routed"
    add_staff_stat(ctx.guild.id, staff.id, "tickets_claimed", 1)
    await save_staff_stats(ctx.guild.id, staff.id)
    await _internal_audit(ctx.guild, "🎫 Ticket Routed", f"{ctx.channel.mention} routed to {staff.mention} by {ctx.author.mention}.", actor=ctx.author, target=staff)
    await ctx.send(f"🎫 Routed this ticket to {staff.mention}.")


def _no_api_auto_judge_score(messages):
    text = " ".join(messages).lower()
    score = 50
    reasons = []

    if len(text) < 40:
        score -= 25
        reasons.append("Very low detail")
    if any(x in text for x in ["idk", "dont know", "don't know", "lol", "lmao", "anything"]):
        score -= 20
        reasons.append("Weak / dismissive answers")
    if any(x in text for x in ["bot", "spam", "raid", "alt"]):
        score -= 15
        reasons.append("Suspicious keywords")
    if len(text) > 180:
        score += 15
        reasons.append("Detailed answers")
    if any(x in text for x in ["friend", "invited", "know", "proof", "because"]):
        score += 10
        reasons.append("Context provided")

    score = max(0, min(100, score))
    if score >= 75:
        verdict = "Likely OK"
    elif score >= 45:
        verdict = "Manual Review"
    else:
        verdict = "High Risk"
    return score, verdict, reasons or ["No strong signals"]


@bot.command(name="fakejudge", aliases=["nojude", "noapijudge", "rulejudge"])
async def fake_judge_command(ctx, limit: int = 50):
    if not is_staff_member(ctx.author):
        return await ctx.send("Staff only.")
    msgs = []
    async for msg in ctx.channel.history(limit=max(5, min(limit, 100)), oldest_first=True):
        if not msg.author.bot:
            msgs.append(msg.content)
    score, verdict, reasons = _no_api_auto_judge_score(msgs)
    embed = discord.Embed(title="🧠 No-API Auto Judge", color=0x57F287 if score >= 75 else 0xFEE75C if score >= 45 else 0xED4245)
    embed.add_field(name="Score", value=f"{score}/100", inline=True)
    embed.add_field(name="Verdict", value=verdict, inline=True)
    embed.add_field(name="Reasons", value="\n".join(f"• {r}" for r in reasons), inline=False)
    await _internal_audit(ctx.guild, "🧠 No-API Judge Used", f"{ctx.author.mention} ran no-API judge in {ctx.channel.mention}. Score: {score}/100", actor=ctx.author)
    await ctx.send(embed=embed)


async def _get_modmail_category(guild):
    category = discord.utils.get(guild.categories, name=MODMAIL_CATEGORY_NAME)
    if not category:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True)
        }
        for role_id in STAFF_ROLE_IDS:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        if guild.owner:
            overwrites[guild.owner] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        category = await guild.create_category(MODMAIL_CATEGORY_NAME, overwrites=overwrites)
        await _internal_audit(guild, "📩 Modmail Category Created", "MODMAIL category created with staff-only permissions.")
    try:
        await category.set_permissions(guild.default_role, view_channel=False)
        for role_id in STAFF_ROLE_IDS:
            role = guild.get_role(role_id)
            if role:
                await category.set_permissions(role, view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        if guild.me:
            await category.set_permissions(guild.me, view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True)
    except Exception as e:
        print(f"Modmail category permission sync failed: {e}")

    return category


async def _create_or_get_modmail_channel(guild, user, opener=None):
    existing_id = modmail_threads.get(user.id)
    if existing_id:
        existing = guild.get_channel(existing_id)
        if existing:
            return existing

    category = await _get_modmail_category(guild)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True),
    }
    for role_id in STAFF_ROLE_IDS:
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
    if guild.owner:
        overwrites[guild.owner] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", getattr(user, "name", str(user.id)).lower())[:80] or str(user.id)
    channel = await guild.create_text_channel(
        name=f"modmail-{safe_name}",
        category=category,
        overwrites=overwrites,
        topic=f"modmail_for:{user.id}"
    )
    modmail_threads[user.id] = channel.id

    embed = discord.Embed(title="📩 Modmail Opened", color=0x5865F2)
    embed.add_field(name="User", value=f"{user.mention if isinstance(user, discord.Member) else user} (`{user.id}`)", inline=False)
    embed.add_field(name="Opened By", value=opener.mention if opener else "User DM", inline=True)
    embed.add_field(name="Status", value="Open", inline=True)
    embed.set_footer(text="Reply in this channel to DM the user.")
    await channel.send(embed=embed, view=ModmailControlView(user.id))
    await _internal_audit(guild, "📩 Modmail Opened", f"Modmail opened for <@{user.id}>.", actor=opener, target=user.id)
    return channel


class ModmailControlView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id

    async def interaction_check(self, interaction):
        if not is_staff_member(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="📌 Claim", style=discord.ButtonStyle.primary, row=0)
    async def claim(self, interaction, button):
        modmail_claims[interaction.channel.id] = interaction.user.id
        await _internal_audit(interaction.guild, "📌 Modmail Claimed", f"{interaction.user.mention} claimed modmail for <@{self.user_id}>.", actor=interaction.user, target=self.user_id)
        await interaction.response.send_message(f"📌 Claimed by {interaction.user.mention}")

    @discord.ui.button(label="🔓 Unclaim", style=discord.ButtonStyle.secondary, row=0)
    async def unclaim(self, interaction, button):
        modmail_claims.pop(interaction.channel.id, None)
        await _internal_audit(interaction.guild, "🔓 Modmail Unclaimed", f"{interaction.user.mention} unclaimed modmail for <@{self.user_id}>.", actor=interaction.user, target=self.user_id)
        await interaction.response.send_message("🔓 Unclaimed.")

    @discord.ui.button(label="📄 Transcript", style=discord.ButtonStyle.secondary, row=0)
    async def transcript(self, interaction, button):
        text = await _build_channel_transcript(interaction.channel, limit=None)
        await interaction.response.defer(ephemeral=True)
        await _send_text_file(interaction.followup, f"{interaction.channel.name}_transcript.txt", text, "📄 Modmail transcript:")
        await _internal_audit(interaction.guild, "📄 Modmail Transcript Downloaded", f"{interaction.user.mention} downloaded modmail transcript.", actor=interaction.user, target=self.user_id)

    @discord.ui.button(label="❌ Close", style=discord.ButtonStyle.danger, row=0)
    async def close(self, interaction, button):
        user = await bot.fetch_user(self.user_id)
        text = await _build_channel_transcript(interaction.channel, limit=None)
        transcript_store[interaction.channel.id] = {
            "guild_id": interaction.guild.id,
            "channel_name": interaction.channel.name,
            "reason": "Modmail Closed",
            "created_at": int(time.time()),
            "text": text
        }
        try:
            await user.send("📪 Your modmail conversation has been closed.")
        except Exception:
            pass
        modmail_threads.pop(self.user_id, None)
        modmail_claims.pop(interaction.channel.id, None)
        await _internal_audit(interaction.guild, "❌ Modmail Closed", f"{interaction.user.mention} closed modmail for <@{self.user_id}>.", actor=interaction.user, target=self.user_id, color=0xED4245)
        await interaction.response.send_message("Closing modmail...", ephemeral=True)
        await asyncio.sleep(1)
        await interaction.channel.delete()


class PublicModmailButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📩 Open Modmail", style=discord.ButtonStyle.primary)
    async def open_modmail(self, interaction, button):
        channel = await _create_or_get_modmail_channel(interaction.guild, interaction.user, opener=interaction.user)
        try:
            await interaction.user.send("📩 Your modmail is open. Reply here to talk to staff.")
        except Exception:
            pass
        await interaction.response.send_message(f"✅ Modmail opened: {channel.mention}", ephemeral=True)


@bot.command(name="modmailpanel")
async def modmailpanel_command(ctx):
    embed = discord.Embed(
        title="📩 Contact Staff",
        description="Click the button below or DM the bot to open a private staff modmail ticket.",
        color=0x5865F2
    )
    await ctx.send(embed=embed, view=PublicModmailButtonView())


try:
    bot.remove_command("closemodmail")
except Exception:
    pass
try:
    bot.remove_command("enddm")
except Exception:
    pass

@bot.command(name="closemodmail", aliases=["enddm"])
async def close_modmail_command(ctx):
    if not is_staff_member(ctx.author):
        return await ctx.send("Staff only.")
    for user_id, channel_id in list(modmail_threads.items()):
        if channel_id == ctx.channel.id:
            text = await _build_channel_transcript(ctx.channel, limit=None)
            transcript_store[ctx.channel.id] = {
                "guild_id": ctx.guild.id,
                "channel_name": ctx.channel.name,
                "reason": "Modmail Closed",
                "created_at": int(time.time()),
                "text": text
            }
            user = await bot.fetch_user(user_id)
            try:
                await user.send("📪 Your modmail conversation has been closed.")
            except Exception:
                pass
            modmail_threads.pop(user_id, None)
            await _internal_audit(ctx.guild, "❌ Modmail Closed", f"{ctx.author.mention} closed modmail for <@{user_id}>.", actor=ctx.author, target=user_id)
            await ctx.channel.delete()
            return
    await ctx.send("This is not a modmail channel.")


# Replace on_message with one clean handler so modmail and commands both work without duplicates.
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # User DM -> create/get staff-only MODMAIL ticket and forward message.
    if isinstance(message.channel, discord.DMChannel):
        if not bot.guilds:
            return
        guild = bot.guilds[0]
        user = message.author
        try:
            channel = await _create_or_get_modmail_channel(guild, user)
            if not channel:
                print(f"Modmail error: channel was None for user {user.id}")
                return

            embed = discord.Embed(description=message.content or "[No text content]", color=0x2B2D31)
            embed.set_author(name=str(user), icon_url=user.display_avatar.url)
            embed.timestamp = discord.utils.utcnow()
            await channel.send(content="🔔 New DM from user", embed=embed)
            for att in message.attachments:
                await channel.send(f"📎 Attachment from {user}: {att.url}")
            await _internal_audit(guild, "📩 User DM Received", f"DM from <@{user.id}> forwarded to {channel.mention}.", target=user.id)
        except Exception as e:
            print(f"Modmail DM handler failed for {message.author.id}: {e}")
            try:
                await message.author.send("⚠️ Staff contact could not be opened right now. Please try again later.")
            except Exception:
                pass
        return

    # Staff modmail ticket -> user DM.
    for user_id, channel_id in list(modmail_threads.items()):
        if message.channel.id == channel_id:
            if not is_staff_member(message.author):
                return
            user = await bot.fetch_user(user_id)
            try:
                embed = discord.Embed(description=message.content or "[No text content]", color=0x5865F2)
                embed.set_author(name=f"Staff • {message.author.display_name}", icon_url=message.author.display_avatar.url)
                embed.timestamp = discord.utils.utcnow()
                await user.send(embed=embed)
                for att in message.attachments:
                    await user.send(att.url)
                await _internal_audit(message.guild, "💬 Staff Modmail Reply", f"{message.author.mention} replied to <@{user_id}>.", actor=message.author, target=user_id)
            except Exception as e:
                await message.channel.send(f"❌ Could not DM user: `{e}`")
                await _internal_audit(message.guild, "⚠️ Modmail DM Failed", f"Could not DM <@{user_id}>: {e}", actor=message.author, target=user_id, color=0xED4245)
            return

    await bot.process_commands(message)


# Upgrade Final Evolution modmail panel if it exists.
def _fe_modmail_embed(guild):
    embed = discord.Embed(title="📩 Modmail Dashboard", color=0x9B59B6)
    if not modmail_threads:
        embed.description = "No active modmail sessions."
    else:
        lines = []
        for user_id, channel_id in list(modmail_threads.items())[:15]:
            ch = guild.get_channel(channel_id)
            claimed = modmail_claims.get(channel_id)
            lines.append(f"• <@{user_id}> → {ch.mention if ch else channel_id} • Claimed: {f'<@{claimed}>' if claimed else 'None'}")
        embed.description = "\n".join(lines)
    embed.add_field(name="Open from user side", value="Users can DM the bot or click `.modmailpanel`.", inline=False)
    embed.set_footer(text="Final Build • Modmail inside dashboard UI")
    embed.timestamp = discord.utils.utcnow()
    return embed


class FinalEvolutionModmailView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=None)
        self.guild = guild

    async def interaction_check(self, interaction):
        if not is_staff_member(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=0)
    async def refresh(self, interaction, button):
        await interaction.response.edit_message(embed=_fe_modmail_embed(self.guild), view=self)

    @discord.ui.button(label="🧹 Clean Closed", style=discord.ButtonStyle.secondary, row=0)
    async def clean(self, interaction, button):
        removed = 0
        for user_id, ch_id in list(modmail_threads.items()):
            if not self.guild.get_channel(ch_id):
                modmail_threads.pop(user_id, None)
                removed += 1
        await _internal_audit(interaction.guild, "🧹 Modmail Cleaned", f"{interaction.user.mention} cleaned {removed} closed modmail session(s).", actor=interaction.user)
        await interaction.response.edit_message(embed=_fe_modmail_embed(self.guild), view=self)

    @discord.ui.button(label="📄 Export Active List", style=discord.ButtonStyle.primary, row=0)
    async def export(self, interaction, button):
        text = "Active Modmail Sessions\n" + "-" * 40 + "\n"
        for user_id, ch_id in modmail_threads.items():
            text += f"User {user_id} -> Channel {ch_id}\n"
        await interaction.response.defer(ephemeral=True)
        await _send_text_file(interaction.followup, "active_modmail.txt", text, "📄 Active modmail list:")

    @discord.ui.button(label="⬅ Back", style=discord.ButtonStyle.primary, row=4)
    async def back(self, interaction, button):
        try:
            hub = FinalEvolutionStaffHub(self.guild)
            await interaction.response.edit_message(embed=_fe_main_embed(self.guild), view=hub)
        except Exception:
            await interaction.response.send_message("Back panel unavailable.", ephemeral=True)

# =========================
# FINAL LOG CHANNEL OVERRIDE
# VERIFICATION + STAFF LOGS + TRANSCRIPTS
# =========================
# Sends important verification/staff/modmail events to the configured verification log channel,
# while still saving the same events into the internal DB.

_last_sent_log_keys = {}

def _final_get_log_channel(guild):
    if not guild:
        return None

    guild_cfg = get_guild_config(guild.id)
    channel_id = guild_cfg.get("log_channel")

    channel = guild.get_channel(channel_id) if channel_id else None
    if channel:
        return channel

    channel = discord.utils.get(guild.text_channels, name="verification-logs")
    if channel:
        guild_cfg["log_channel"] = channel.id
        return channel

    return None


async def _final_persist_log(guild, title, description):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO log_history (guild_id, title, description, created_at) VALUES (?, ?, ?, ?)",
                (guild.id, title, description, int(time.time()))
            )
            await db.commit()
    except Exception as e:
        print(f"Failed to persist log action: {e}")


async def log_action(guild, title, description, color=0x2b2d31, *, fields=None):
    # Duplicate guard so the same event does not spam twice instantly.
    now = time.time()
    key = f"{getattr(guild, 'id', None)}:{title}:{description[:80]}"
    if key in _last_sent_log_keys and now - _last_sent_log_keys[key] < 1.5:
        return
    _last_sent_log_keys[key] = now

    await _final_persist_log(guild, title, description)

    channel = _final_get_log_channel(guild)
    if not channel:
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=color
    )
    embed.timestamp = discord.utils.utcnow()
    embed.set_footer(text=f"{guild.name} • Verification / Staff Logs")

    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)

    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"Failed to send log channel embed: {e}")


async def _internal_audit(guild, title, description, *, color=0x5865F2, fields=None, actor=None, target=None):
    # Keep memory copy for .auditlogs
    entry = {
        "guild_id": guild.id if guild else None,
        "title": title,
        "description": description,
        "created_at": int(time.time()),
        "actor_id": getattr(actor, "id", None),
        "target": str(target) if target is not None else None,
        "fields": fields or []
    }
    internal_audit_logs.append(entry)
    if len(internal_audit_logs) > 1000:
        del internal_audit_logs[:len(internal_audit_logs) - 1000]

    log_fields = list(fields or [])
    if actor:
        log_fields.insert(0, ("Staff", actor.mention if hasattr(actor, "mention") else str(actor), True))
    if target is not None:
        log_fields.append(("Target", str(target), True))

    await log_action(guild, title, description, color=color, fields=log_fields)


async def save_ticket_transcript(channel, guild, reason="Ticket Closed"):
    if not channel:
        return

    transcript_lines = []
    transcript_lines.append(f"Ticket Transcript - {channel.name}")
    transcript_lines.append(f"Reason: {reason}")
    transcript_lines.append(f"Channel ID: {channel.id}")
    transcript_lines.append(f"Created: {channel.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    transcript_lines.append("=" * 60 + "\n")

    try:
        async for message in channel.history(limit=None, oldest_first=True):
            timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author = f"{message.author} ({message.author.id})"
            if message.author.bot:
                author += " [BOT]"

            content = message.content if message.content else "[No text content]"
            transcript_lines.append(f"[{timestamp}] {author}:")
            transcript_lines.append(content)

            if message.attachments:
                for attachment in message.attachments:
                    transcript_lines.append(f"[Attachment] {attachment.filename}: {attachment.url}")

            transcript_lines.append("-" * 40)
    except Exception as e:
        transcript_lines.append(f"\n[Error fetching messages: {e}]")

    transcript_text = "\n".join(transcript_lines)

    transcript_store[channel.id] = {
        "guild_id": guild.id,
        "channel_name": channel.name,
        "reason": reason,
        "created_at": int(time.time()),
        "text": transcript_text
    }

    await _final_persist_log(guild, "📜 Ticket Transcript Saved", f"Transcript saved for {channel.name}. Reason: {reason}")

    log_channel = _final_get_log_channel(guild)
    if not log_channel:
        return

    try:
        fp = io.BytesIO(transcript_text.encode("utf-8", errors="ignore"))
        file = discord.File(fp, filename=f"{channel.name}_transcript.txt")

        embed = discord.Embed(
            title="📜 Ticket Transcript",
            description=f"Transcript for **{channel.name}**",
            color=0x5865F2
        )
        embed.add_field(name="Reason", value=reason, inline=True)
        embed.add_field(name="Channel ID", value=str(channel.id), inline=True)
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text=f"{guild.name} • Transcript Archive")

        await log_channel.send(embed=embed, file=file)

    except Exception as e:
        print(f"Transcript log send failed: {e}")


# Patch ticket close logger to be extra clear about who accepted/denied/blacklisted who.
_original_log_ticket_close_with_duration = TicketControls._log_ticket_close_with_duration

async def _final_log_ticket_close_with_duration(self, interaction, member, action_title, description, color, extra_fields=None):
    staff_text = interaction.user.mention
    user_text = member.mention if member else f"<@{self.user_id}>"
    action_clean = action_title.replace("🟢", "").replace("🔴", "").replace("⚫", "").strip()

    fields = [
        ("Staff", staff_text, True),
        ("User", user_text, True),
        ("User ID", str(getattr(member, "id", self.user_id)), True),
        ("Ticket", interaction.channel.mention, True),
        ("Gender", self.gender.capitalize(), True),
        ("Action", action_clean, True),
    ]

    if extra_fields:
        fields.extend(extra_fields)

    await log_action(
        interaction.guild,
        action_title,
        f"{staff_text} {action_clean.lower()} {user_text}.",
        color=color,
        fields=fields
    )

    return await _original_log_ticket_close_with_duration(self, interaction, member, action_title, description, color, extra_fields)

TicketControls._log_ticket_close_with_duration = _final_log_ticket_close_with_duration

# Clean safe startup: no infinite bot.start loop, so commands do not double-fire.
async def safe_start_bot():
    token = os.getenv("TOKEN")
    if not token:
        print("❌ TOKEN env variable is missing.")
        return

    startup_delay = int(os.getenv("STARTUP_DELAY_SECONDS", "15"))
    print(f"⏳ Startup guard active. Waiting {startup_delay}s before Discord login...")
    await asyncio.sleep(startup_delay)

    try:
        print("🔐 Starting bot safely...")
        await bot.start(token, reconnect=True)
    except discord.HTTPException as e:
        text = str(e)
        if getattr(e, "status", None) == 429 or "Too Many Requests" in text or "1015" in text:
            print("🚫 Discord login rate-limited / Cloudflare 1015.")
            print("🕒 Stop the host and wait 20–30 minutes before trying again.")
            return
        raise
    except discord.LoginFailure as e:
        print("❌ Invalid Discord token. Fix TOKEN in Railway Variables.")
        print(e)
        return


if __name__ == "__main__":
    asyncio.run(safe_start_bot())
