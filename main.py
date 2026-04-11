import discord

from discord.ext import commands
import os
import aiosqlite
import asyncio
import time
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


# =========================
# SETUP COMMAND
# =========================
@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    guild = ctx.guild
    guild_cfg = get_guild_config(guild.id)

    male = await guild.create_role(name="Male", color=discord.Color.blue())
    female = await guild.create_role(name="Female", color=discord.Color.from_rgb(255, 105, 180))
    unverified = await guild.create_role(name="Unverified", color=discord.Color.light_grey())
    staff = await guild.create_role(name="Staff", color=discord.Color.gold())

    category = await guild.create_category(
        "Verification Tickets",
        overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    )

    log_channel = await guild.create_text_channel("verification-logs")
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
        if channel == log_channel or channel.category == category:
            continue
        try:
            await channel.set_permissions(male, view_channel=True)
            await channel.set_permissions(female, view_channel=True)
        except:
            pass

    guild_cfg.update({
        "log_channel": log_channel.id,
        "category": category.id,
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
    if expires_ts is None:
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
    end_time = time.time() + duration
    warned = False

    embed = discord.Embed(
        title="⏳ Verification Timer",
        description="Time remaining: **10:00**",
        color=0xED4245
    )

    msg = await channel.send(embed=embed)

    while True:
        try:
            remaining = int(end_time - time.time())

            if remaining <= 0:
                embed.description = "⛔ Time is up!"
                await msg.edit(embed=embed)
                break

            minutes = remaining // 60
            seconds = remaining % 60

            embed.description = f"Time remaining: **{minutes:02d}:{seconds:02d}**"

            if remaining <= 60 and not warned:
                warned = True
                await channel.send(f"⚠️ {member.mention} you have **1 minute left** to complete verification!")

            await msg.edit(embed=embed)
            await asyncio.sleep(1)

        except discord.NotFound:
            break
        except Exception:
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
    if any(v is None for v in guild_cfg.values()):
        male = discord.utils.get(guild.roles, name="Male")
        female = discord.utils.get(guild.roles, name="Female")
        unverified = discord.utils.get(guild.roles, name="Unverified")
        staff = discord.utils.get(guild.roles, name="Staff")
        category = discord.utils.get(guild.categories, name="Verification Tickets")
        log_channel = discord.utils.get(guild.text_channels, name="verification-logs")
        if all([male, female, unverified, staff, category, log_channel]):
            guild_cfg.update({
                "log_channel": log_channel.id,
                "category": category.id,
                "male_role": male.id,
                "female_role": female.id,
                "unverified_role": unverified.id,
                "staff_role": staff.id
            })
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

    if any(v is None for v in guild_cfg.values()):
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

    category = guild.get_channel(guild_cfg["category"])
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
            title=f"Control Room — Verification Console ({self.current_page + 1}/{total_pages})",
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
                await asyncio.sleep(20)
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
        await interaction.response.edit_message(embed=self.get_embed(), view=self)
        await interaction.followup.send(msg, ephemeral=True)

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

    @discord.ui.button(label="🔗 Open Ticket", style=discord.ButtonStyle.primary, row=3)
    async def open_ticket_btn(self, interaction: discord.Interaction, button):
        entry = self.current_entry()
        if not entry:
            return await interaction.response.send_message("No active verification selected.", ephemeral=True)
        _, ticket_id, _, _, _, _ = entry
        channel = self.guild.get_channel(ticket_id)
        if not channel:
            return await interaction.response.send_message("Ticket channel no longer exists.", ephemeral=True)
        await interaction.response.send_message(f"Open this ticket: {channel.mention}", ephemeral=True)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=3)
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


@bot.command()
async def adminpanel(ctx):
    if ctx.author != ctx.guild.owner:
        return await ctx.send("❌ Only the server owner can use this command.", ephemeral=True)

    control_room = await ensure_control_room(ctx.guild)
    rows = await get_active_verifications(ctx.guild.id)

    if not rows:
        await control_room.send(embed=discord.Embed(title="Control Room", description="✅ No active verifications.", color=0x57F287))
        return await ctx.send(f"{ctx.author.mention} **Control Room updated.** No active verifications right now.", ephemeral=True)

    view = VerificationPanel(ctx.guild, rows)
    await view.start(control_room)

    await ctx.send(f"{ctx.author.mention} **Control Room opened.** Check `#control-room` for the categorized live moderation console.", ephemeral=True)

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
# RUN
# =========================
bot.run(os.getenv("TOKEN"))