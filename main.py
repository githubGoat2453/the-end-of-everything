import discord
from discord.ext import commands
import os
import aiosqlite
import asyncio
import time

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=".", intents=intents)
bot.remove_command("help")

DB_NAME = "bot.db"

# In-memory rejoin cooldowns: {user_id: unix_timestamp_until_allowed}
cooldowns = {}

# Per-guild daily stats: {guild_id: {...}}
daily_stats = {}

# =========================
# CONFIG (PER GUILD)
# =========================
# config[guild_id] = {
#   "log_channel": int,
#   "category": int,
#   "male_role": int,
#   "female_role": int,
#   "unverified_role": int,
#   "staff_role": int
# }
config = {}

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
        # Drop old single-server tables if they exist (clean reset for multi-server)
        await db.execute("DROP TABLE IF EXISTS blacklist")
        await db.execute("DROP TABLE IF EXISTS config")

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

        # ===== Forensic / AI Judge Tables =====
        await db.execute("""
        CREATE TABLE IF NOT EXISTS forensic_scores (
            guild_id INTEGER,
            user_id INTEGER,
            suspicion INTEGER DEFAULT 0,
            tone TEXT DEFAULT '😐 Neutral',
            difficulty INTEGER DEFAULT 0,
            cooperation INTEGER DEFAULT 100,
            PRIMARY KEY (guild_id, user_id)
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS forensic_timeline (
            guild_id INTEGER,
            user_id INTEGER,
            event TEXT,
            timestamp REAL
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS forensic_patterns (
            guild_id INTEGER,
            user_id INTEGER,
            pattern TEXT
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS forensic_staff (
            guild_id INTEGER,
            staff_id INTEGER,
            tickets_claimed INTEGER DEFAULT 0,
            avg_response REAL DEFAULT 0,
            escalations INTEGER DEFAULT 0,
            notes INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, staff_id)
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS forensic_heatmap (
            guild_id INTEGER,
            hour INTEGER,
            joins INTEGER DEFAULT 0,
            tickets INTEGER DEFAULT 0,
            approvals INTEGER DEFAULT 0,
            denials INTEGER DEFAULT 0,
            blacklists INTEGER DEFAULT 0,
            staff_responses INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, hour)
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS ai_judge_overrides (
            guild_id INTEGER,
            user_id INTEGER,
            ai_verdict TEXT,
            staff_verdict TEXT,
            risk INTEGER
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

# =========================
# FORENSIC HELPERS (MODULES)
# =========================

async def add_timeline_event(guild_id: int, user_id: int, event: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO forensic_timeline (guild_id, user_id, event, timestamp) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, event, time.time())
        )
        await db.commit()

async def add_suspicion(guild_id: int, user_id: int, amount: int, reason: str, guild: discord.Guild | None = None):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT suspicion FROM forensic_scores WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
        current = row[0] if row else 0
        new_score = max(0, min(current + amount, 100))
        await db.execute(
            "INSERT OR REPLACE INTO forensic_scores (guild_id, user_id, suspicion) VALUES (?, ?, ?)",
            (guild_id, user_id, new_score)
        )
        await db.commit()

    if guild and abs(new_score - current) >= 25:
        await log_action(
            guild,
            "⚠️ Risk Spike Detected",
            f"User: <@{user_id}>\nSuspicion jumped from {current} → {new_score}\nReason: {reason}",
            color=0xFF0000,
            fields=[
                ("Old Suspicion", str(current), True),
                ("New Suspicion", str(new_score), True),
                ("Reason", reason, False)
            ]
        )

def analyze_tone(text: str):
    t = text.lower()
    if any(w in t for w in ["uh", "um", "idk", "i dont know", "..."]):
        return "😬 Nervous / Hesitant", 1
    if any(w in t for w in ["wtf", "bro", "tf", "mad", "angry", "annoyed"]):
        return "😡 Angry / Irritated", 2
    if t == t.strip().lower() and len(text.split()) > 4:
        return "🤖 Robotic / Scripted", 3
    if any(w in t for w in ["greetings", "dear", "sincerely", "regards"]):
        return "🧊 Overly Formal / Cold", 2
    if any(w in t for w in ["lol", "lmao", "haha", "😂"]):
        return "😁 Friendly / Casual", 1
    if "?" in text and len(text) < 20:
        return "🥴 Confused", 1
    if any(w in t for w in ["im sad", "im upset", "crying", "hurt"]):
        return "😭 Emotional Distress", 3
    return "😐 Neutral", 0

emoji_traits = {
    "😂": "Friendly",
    "😭": "Emotional",
    "😡": "Aggressive",
    "🤖": "Robotic",
    "💀": "Chaotic",
    "😶": "Nervous"
}

def calculate_confidence(text: str) -> int:
    score = 50
    if len(text) < 5:
        score -= 20
    if "?" in text:
        score -= 10
    if "..." in text:
        score -= 15
    if len(text) > 40:
        score += 10
    return max(0, min(score, 100))

def estimate_truthfulness(text: str) -> int:
    t = text.lower()
    score = 80
    if "i swear" in t:
        score -= 20
    if "trust me" in t:
        score -= 15
    if "honestly" in t:
        score -= 10
    if "edited" in t:
        score -= 25
    return max(0, min(score, 100))

async def log_tone(guild: discord.Guild, user_id: int, tone: str, severity: int, message: str):
    await log_action(
        guild,
        f"🎭 Tone Analysis — {tone}",
        f"User: <@{user_id}>\nTone Severity: **{severity}**\n\nMessage:\n{message}",
        color=0xCC33FF
    )

async def analyze_emoji_profile(guild: discord.Guild, user_id: int, text: str):
    used = [e for e in emoji_traits if e in text]
    if not used:
        return
    traits = ", ".join([emoji_traits[e] for e in used])
    await log_action(
        guild,
        "🎭 Emoji Personality Profile",
        f"User: <@{user_id}>\nTraits: **{traits}**",
        color=0xFF99FF
    )

async def log_emotional_curve(guild: discord.Guild, member: discord.Member, new_tone: str):
    history = getattr(member, "tone_history", [])
    history.append(new_tone)
    member.tone_history = history
    if len(history) >= 4:
        curve = " → ".join(history[-4:])
        await log_action(
            guild,
            "📉 Emotional Stability Curve",
            f"User: {member.mention}\nCurve: {curve}",
            color=0xFFAA33
        )

def calculate_momentum(member: discord.Member) -> int:
    fast = getattr(member, "fast_responses", 0)
    hes = getattr(member, "hesitations", 0)
    score = 50 + fast * 10 - hes * 10
    return max(0, min(score, 100))

def cooperation_score(edits: int, deletes: int, bypasses: int) -> int:
    score = 100 - edits * 10 - deletes * 15 - bypasses * 20
    return max(0, score)

def volatility(history: list[str]) -> int:
    if len(history) < 2:
        return 0
    changes = sum(1 for i in range(1, len(history)) if history[i] != history[i - 1])
    return min(100, changes * 20)

def predictability(history: list[str]) -> int:
    if not history:
        return 50
    return max(0, 100 - len(set(history)) * 20)

def classify_style(tone: str, patterns: list[str]) -> str:
    if "Robotic" in tone:
        return "🤖 Robotic"
    if "Nervous" in tone:
        return "😬 Nervous"
    if "Angry" in tone:
        return "😡 Aggressive"
    if patterns:
        return "🧩 Scripted"
    return "😐 Neutral"

def symmetry(user_len: int, staff_len: int) -> int:
    if staff_len == 0:
        return 50
    ratio = user_len / staff_len
    return int(100 - abs(1 - ratio) * 40)

def cognitive_load(hesitations: int, tone_history: list[str]) -> int:
    stress = sum(1 for t in tone_history if "Nervous" in t or "Distress" in t)
    return min(100, hesitations * 10 + stress * 15)

async def calculate_difficulty(guild_id: int, user_id: int) -> int:
    suspicion = 0
    tone = "Unknown"
    patterns = 0
    escalations = 0
    proof_requests = 0

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT suspicion, tone, difficulty, cooperation FROM forensic_scores WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                suspicion, tone, difficulty, coop = row
            else:
                suspicion, tone, difficulty, coop = 0, "Unknown", 0, 100

        async with db.execute(
            "SELECT COUNT(*) FROM forensic_patterns WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ) as cursor:
            patterns = (await cursor.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM forensic_timeline WHERE guild_id=? AND user_id=? AND event LIKE '🚨%'",
            (guild_id, user_id)
        ) as cursor:
            escalations = (await cursor.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM forensic_timeline WHERE guild_id=? AND user_id=? AND event LIKE '🔍%'",
            (guild_id, user_id)
        ) as cursor:
            proof_requests = (await cursor.fetchone())[0]

    difficulty_score = (
        suspicion * 0.5 +
        patterns * 5 +
        escalations * 15 +
        proof_requests * 10 +
        (100 - coop) * 0.2
    )

    if "🤖 Robotic" in tone:
        difficulty_score += 20
    if "😭 Emotional Distress" in tone:
        difficulty_score += 10
    if "😡 Angry" in tone:
        difficulty_score += 15

    return min(int(difficulty_score), 100)

def difficulty_label(score: int) -> str:
    if score < 20:
        return "🟢 EASY"
    if score < 40:
        return "🟡 MEDIUM"
    if score < 70:
        return "🔥 HARD"
    if score < 90:
        return "🚨 SUSPICIOUS"
    return "💀 IMPOSSIBLE"

async def build_final_verdict(guild: discord.Guild, user_id: int, verdict_label: str):
    suspicion = 0
    difficulty = 0
    tone = "Unknown"
    patterns = []
    timeline = []
    integrity_flags = []

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT suspicion, tone, difficulty, cooperation FROM forensic_scores WHERE guild_id=? AND user_id=?",
            (guild.id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                suspicion, tone, difficulty, coop = row
            else:
                suspicion, tone, difficulty, coop = 0, "Unknown", 0, 100

        async with db.execute(
            "SELECT pattern FROM forensic_patterns WHERE guild_id=? AND user_id=?",
            (guild.id, user_id)
        ) as cursor:
            patterns = [r[0] for r in await cursor.fetchall()]

        async with db.execute(
            "SELECT event, timestamp FROM forensic_timeline WHERE guild_id=? AND user_id=? ORDER BY timestamp ASC",
            (guild.id, user_id)
        ) as cursor:
            timeline = await cursor.fetchall()

        async with db.execute(
            "SELECT event FROM forensic_timeline WHERE guild_id=? AND user_id=? AND (event LIKE '🚨%' OR event LIKE '🧨%')",
            (guild.id, user_id)
        ) as cursor:
            integrity_flags = [r[0] for r in await cursor.fetchall()]

    timeline_text = ""
    for event, ts in timeline:
        t = time.strftime("%H:%M:%S", time.localtime(ts))
        timeline_text += f"{t} — {event}\n"

    patterns_text = "\n".join([f"• {p}" for p in patterns]) if patterns else "None"
    integrity_text = "\n".join([f"• {i}" for i in integrity_flags]) if integrity_flags else "None"

    return {
        "suspicion": suspicion,
        "difficulty": difficulty,
        "tone": tone,
        "patterns": patterns_text,
        "timeline": timeline_text,
        "integrity": integrity_text,
        "verdict": verdict_label
    }

async def log_final_verdict(guild: discord.Guild, user_id: int, verdict_label: str):
    data = await build_final_verdict(guild, user_id, verdict_label)
    await log_action(
        guild,
        f"📁 Final Forensic Verdict — {verdict_label}",
        (
            f"User: <@{user_id}>\n\n"
            f"🔥 Suspicion Score: {data['suspicion']}/100\n"
            f"🧬 Difficulty Score: {data['difficulty']}/100\n"
            f"🎭 Tone: {data['tone']}\n\n"
            f"🧩 Behavior Patterns:\n{data['patterns']}\n\n"
            f"🧱 Integrity Flags:\n{data['integrity']}\n\n"
            f"⏳ Timeline:\n{data['timeline']}\n\n"
            f"📌 Final Verdict: {data['verdict']}"
        ),
        color=0xFF00FF
    )

async def ai_judge_decide(guild_id: int, user_id: int):
    suspicion = 0
    difficulty = 0
    integrity_flags = 0
    cooperation = 100

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT suspicion, difficulty, cooperation FROM forensic_scores WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                suspicion, difficulty, cooperation = row

        async with db.execute(
            "SELECT COUNT(*) FROM forensic_timeline WHERE guild_id=? AND user_id=? AND (event LIKE '🚨%' OR event LIKE '🧨%')",
            (guild_id, user_id)
        ) as cursor:
            integrity_flags = (await cursor.fetchone())[0]

    risk = (
        suspicion * 0.4 +
        difficulty * 0.25 +
        integrity_flags * 10 +
        (100 - cooperation) * 0.2
    )
    risk = max(0, min(int(risk), 100))

    if risk < 20:
        verdict = "approve"
        label = "🟢 APPROVE"
    elif risk < 50:
        verdict = "review"
        label = "🟡 NEEDS REVIEW"
    elif risk < 75:
        verdict = "deny"
        label = "❌ DENY"
    else:
        verdict = "blacklist"
        label = "🧨 BLACKLIST"

    return risk, verdict, label

async def log_ai_override(guild_id: int, user_id: int, ai_verdict: str, staff_verdict: str, risk: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO ai_judge_overrides (guild_id, user_id, ai_verdict, staff_verdict, risk) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, ai_verdict, staff_verdict, risk)
        )
        await db.commit()

# =========================
# SETUP COMMAND (PER GUILD)
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

    # Lock all existing channels so @everyone sees nothing
    for channel in guild.channels:
        try:
            await channel.set_permissions(
                guild.default_role,
                view_channel=False
            )
        except:
            pass

    # Allow main channels for verified roles (male/female) AFTER verification
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
# REQUIREMENTS COMMAND (SHARED)
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
# UNBLACKLIST (PER GUILD)
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
# GENDER UI
# =========================
class GenderButtons(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id

    async def interaction_check(self, interaction):
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Male", style=discord.ButtonStyle.primary)
    async def male(self, interaction, button):
        await self.handle(interaction, "male")

    @discord.ui.button(label="Female", style=discord.ButtonStyle.danger)
    async def female(self, interaction, button):
        await self.handle(interaction, "female")

    async def handle(self, interaction, gender):
        req = await get_requirement(gender)

        embed = discord.Embed(
            title="Requirements",
            description=f"**{gender.capitalize()}**\n\n{req}",
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

        # Gender-specific NEXT STEPS embed
        if gender == "female":
            next_steps_embed = discord.Embed(
                title="Wait — we're not done yet!",
                description=(
                    "**Verification Requirement**\n"
                    "• Submit a short voice note confirming your identity\n\n"
                    "**Important Notice**\n"
                    "The buttons below are restricted and can only be used by authorized administrators."
                ),
                color=0x2b2d31
            )
        else:
            next_steps_embed = discord.Embed(
                title="Wait — we're not done yet!",
                description=(
                    "**Verification Requirement**\n"
                    "POF $1000.00 USD\n"
                    "•Invite 3 girls to the server\n\n"
                    "**Important Notice**\n"
                    "The buttons below are restricted and can only be used by authorized administrators."
                ),
                color=0x2b2d31
            )

        next_steps_embed.set_author(name="NEXT STEPS")

        await interaction.channel.send(embed=next_steps_embed)

        await interaction.channel.send(
            "🎫 Staff Controls:",
            view=TicketControls(self.user_id, gender)
        )

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

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        guild_cfg = get_guild_config(interaction.guild.id)
        stats = get_daily_stats(interaction.guild.id)

        member = interaction.guild.get_member(self.user_id)

        male = interaction.guild.get_role(guild_cfg["male_role"])
        female = interaction.guild.get_role(guild_cfg["female_role"])
        unverified = interaction.guild.get_role(guild_cfg["unverified_role"])

        if unverified in member.roles:
            await member.remove_roles(unverified)

        role = male if self.gender == "male" else female
        if role:
            await member.add_roles(role)

        try:
            await member.send("✅ You have been approved and verified.")
        except:
            pass

        stats["approved"] += 1

        # AI Judge shadow verdict
        risk, ai_verdict, ai_label = await ai_judge_decide(interaction.guild.id, member.id)
        await log_action(
            interaction.guild,
            "🟢 Approved",
            f"{interaction.user.mention} approved {member.mention}.",
            color=0x57F287,
            fields=[
                ("Staff", interaction.user.mention, True),
                ("User", member.mention, True),
                ("User ID", str(member.id), True),
                ("Gender", self.gender.capitalize(), True),
                ("Channel", interaction.channel.mention, True),
                ("AI Judge Verdict", ai_label, True),
                ("Risk Score", str(risk), True)
            ]
        )

        if ai_verdict != "approve":
            await log_ai_override(interaction.guild.id, member.id, ai_verdict, "approve", risk)

        await log_final_verdict(interaction.guild, member.id, "🟢 APPROVED")

        await interaction.response.send_message("Approved")
        await interaction.channel.delete()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        guild_cfg = get_guild_config(interaction.guild.id)
        stats = get_daily_stats(interaction.guild.id)

        member = interaction.guild.get_member(self.user_id)

        try:
            await member.send("❌ Your verification was denied.")
        except:
            pass

        await member.kick(reason="Denied")

        stats["denied"] += 1

        risk, ai_verdict, ai_label = await ai_judge_decide(interaction.guild.id, member.id)

        await log_action(
            interaction.guild,
            "🔴 Denied",
            f"{interaction.user.mention} denied {member.mention}.",
            color=0xED4245,
            fields=[
                ("Staff", interaction.user.mention, True),
                ("User", member.mention, True),
                ("User ID", str(member.id), True),
                ("Gender", self.gender.capitalize(), True),
                ("Channel", interaction.channel.mention, True),
                ("AI Judge Verdict", ai_label, True),
                ("Risk Score", str(risk), True)
            ]
        )

        if ai_verdict != "deny":
            await log_ai_override(interaction.guild.id, member.id, ai_verdict, "deny", risk)

        await log_final_verdict(interaction.guild, member.id, "❌ DENIED")

        await interaction.response.send_message("Denied")
        await interaction.channel.delete()

    @discord.ui.button(label="Blacklist", style=discord.ButtonStyle.secondary)
    async def blacklist(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        guild_cfg = get_guild_config(interaction.guild.id)
        stats = get_daily_stats(interaction.guild.id)

        member = interaction.guild.get_member(self.user_id)

        await add_blacklist(interaction.guild.id, member.id)

        try:
            await member.send("🚫 You have been blacklisted from this server.")
        except:
            pass

        await member.kick(reason="Blacklisted")

        stats["blacklisted"] += 1

        risk, ai_verdict, ai_label = await ai_judge_decide(interaction.guild.id, member.id)

        await log_action(
            interaction.guild,
            "⚫ Blacklisted",
            f"{interaction.user.mention} blacklisted {member.mention}.",
            color=0x000000,
            fields=[
                ("Staff", interaction.user.mention, True),
                ("User", member.mention, True),
                ("User ID", str(member.id), True),
                ("Channel", interaction.channel.mention, True),
                ("AI Judge Verdict", ai_label, True),
                ("Risk Score", str(risk), True)
            ]
        )

        if ai_verdict != "blacklist":
            await log_ai_override(interaction.guild.id, member.id, ai_verdict, "blacklist", risk)

        await log_final_verdict(interaction.guild, member.id, "🧨 BLACKLISTED")

        await interaction.response.send_message("Blacklisted")
        await interaction.channel.delete()

    @discord.ui.button(label="Add Note", style=discord.ButtonStyle.secondary)
    async def add_note(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        await interaction.response.send_message("✏️ Please type your note in this channel.", ephemeral=True)

        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel

        try:
            msg = await interaction.client.wait_for("message", check=check, timeout=300)
        except asyncio.TimeoutError:
            return

        member = interaction.guild.get_member(self.user_id)

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

    @discord.ui.button(label="Request Proof", style=discord.ButtonStyle.primary)
    async def request_proof(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        member = interaction.guild.get_member(self.user_id)
        try:
            await member.send(
                "📎 Please provide any required proof or screenshots by replying here or uploading them in your ticket channel."
            )
        except:
            pass

        await add_timeline_event(interaction.guild.id, member.id, "🔍 Proof requested by staff")

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

        await interaction.response.send_message("Requested proof from user.", ephemeral=True)

    @discord.ui.button(label="Escalate", style=discord.ButtonStyle.secondary)
    async def escalate(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        member = interaction.guild.get_member(self.user_id)

        await add_timeline_event(interaction.guild.id, member.id, "🚨 Ticket escalated by staff")

        await log_action(
            interaction.guild,
            "🚨 Ticket Escalated",
            f"{interaction.user.mention} escalated the ticket for {member.mention}.",
            color=0xED4245,
            fields=[
                ("Staff", interaction.user.mention, True),
                ("User", member.mention, True),
                ("User ID", str(member.id), True)
            ]
        )

        await interaction.response.send_message("Ticket escalated.", ephemeral=True)

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

        await add_timeline_event(guild.id, member.id, "🧨 Auto-kicked for verification timeout")

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

# =========================
# MEMBER JOIN / CONFIG
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

@bot.event
async def on_member_join(member):
    guild = member.guild
    guild_cfg = get_guild_config(guild.id)
    stats = get_daily_stats(guild.id)

    await ensure_config(guild)

    if any(v is None for v in guild_cfg.values()):
        print(f"Config not set up for guild {guild.name}, skipping member join.")
        return

    # Anti-rejoin cooldown
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

        member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True
        ),

        staff_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True
        ) if staff_role else None,

        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True
        )
    }
    # Remove None keys
    overwrites = {k: v for k, v in overwrites.items() if v is not None}

    channel = await guild.create_text_channel(
        f"verify-{member.name}",
        category=category,
        overwrites=overwrites,
        topic=f"ticket_for:{member.id}"
    )

    embed = discord.Embed(
        title="WELCOME TO THE SERVER",
        description=(
            "Welcome to the server. Before accessing the main sections, you must complete our screening verification.\n\n"
            "**Step 1:** Select your gender below.\n"
            "**Step 2:** Tell us how you were invited.\n"
            "**Step 3:** Wait for our higher-ups to review your screening.\n\n"
            "⚠️ Verification must be completed in 10 minutes or you will be kicked from the server."
        ),
        color=0x2b2d31
    )

    await channel.send(member.mention, embed=embed, view=GenderButtons(member.id))

    await channel.send(
        "📝 **Question:** whats your alias?\n"
        "Please answer in this channel."
    )

    stats["joins"].append(discord.utils.utcnow().hour)

    await add_timeline_event(guild.id, member.id, "🎫 Ticket created on join")

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
        await add_timeline_event(guild.id, member.id, "🚪 User left during verification")

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
# STAFF CLAIM / ALIAS / STAFF ACTIVITY + FORENSICS
# =========================
@bot.event
async def on_message(message):
    if message.author.bot:
        return await bot.process_commands(message)

    guild = message.guild
    if not guild:
        return await bot.process_commands(message)

    guild_cfg = get_guild_config(guild.id)
    channel = message.channel

    # Ticket channel logic
    if channel.category and guild_cfg.get("category") and channel.category.id == guild_cfg["category"] and channel.topic:
        if channel.topic.startswith("ticket_for:"):
            try:
                user_id = int(channel.topic.split("ticket_for:")[1].split("|")[0])
            except:
                user_id = None

            staff_role = guild.get_role(guild_cfg.get("staff_role")) if guild_cfg.get("staff_role") else None
            is_staff = (
                message.author.guild_permissions.administrator or
                (staff_role and staff_role in message.author.roles)
            )

            # STAFF CLAIM
            if is_staff and "claimed_by:" not in channel.topic:
                new_topic = channel.topic + f"|claimed_by:{message.author.id}"

                await channel.edit(
                    name=f"staff-{message.author.name}-verification",
                    topic=new_topic
                )

                await add_timeline_event(guild.id, user_id, f"🛡️ Ticket claimed by {message.author.id}")

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

            # STAFF TAKEOVER / SWITCH
            if is_staff and "claimed_by:" in channel.topic:
                try:
                    claimed_id = int(channel.topic.split("claimed_by:")[1].split("|")[0])
                except:
                    claimed_id = None

                if claimed_id and claimed_id != message.author.id:
                    await add_timeline_event(guild.id, user_id, f"⚠️ Staff takeover attempt by {message.author.id}")

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

            # USER ALIAS ANSWER
            if user_id and message.author.id == user_id and "alias_logged" not in channel.topic:
                await channel.edit(topic=channel.topic + "|alias_logged")

                await add_timeline_event(guild.id, user_id, "📝 Alias answered")

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

            # FORENSIC: tone, emoji, confidence, truth, emotional curve
            if user_id and message.author.id == user_id:
                tone, severity = analyze_tone(message.content)
                await log_tone(guild, user_id, tone, severity, message.content)
                await analyze_emoji_profile(guild, user_id, message.content)

                conf = calculate_confidence(message.content)
                truth = estimate_truthfulness(message.content)

                await log_action(
                    guild,
                    "💬 Confidence & Truthfulness",
                    f"User: <@{user_id}>\nConfidence: **{conf}%**\nTruthfulness Estimate: **{truth}%**",
                    color=0x33FFAA
                )

                member = guild.get_member(user_id)
                if member:
                    await log_emotional_curve(guild, member, tone)

                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute(
                        "INSERT OR REPLACE INTO forensic_scores (guild_id, user_id, tone) VALUES (?, ?, ?)",
                        (guild.id, user_id, tone)
                    )
                    await db.commit()

                await add_timeline_event(guild.id, user_id, f"💬 User message: {message.content[:100]}")

    await bot.process_commands(message)

# =========================
# STAFF INACTIVITY CHECK
# =========================
async def staff_inactivity_check():
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
            guild_cfg = get_guild_config(guild.id)
            for channel in guild.text_channels:
                if channel.topic and "claimed_by:" in channel.topic:
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
# DAILY SUMMARY (PER GUILD)
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
        embed.add_field(name="Approve Button", value="Approves a user.", inline=False)
        embed.add_field(name="Deny Button", value="Denies a user.", inline=False)
        embed.add_field(name="Blacklist Button", value="Blacklists a user.", inline=False)
        embed.add_field(name="Add Note Button", value="Adds a note to logs.", inline=False)
        embed.add_field(name="Request Proof Button", value="Requests proof from user.", inline=False)
        embed.add_field(name="Escalate Button", value="Marks ticket as escalated.", inline=False)

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
            description="Verification & moderation bot with logging, tickets, staff tools, and forensic intelligence.",
            color=0xED4245
        )
        embed.add_field(name="Developer", value="label", inline=False)
        embed.add_field(name="Features", value="Verification • Tickets • Logging • Staff Tools • Forensic AI Judge", inline=False)

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

    for guild in bot.guilds:
        await ensure_config(guild)
        await log_action(
            guild,
            "🟣 Bot Started",
            f"Bot is online and connected to **{guild.name}**.",
            color=0x9B59B6
        )

    bot.loop.create_task(staff_inactivity_check())
    bot.loop.create_task(daily_summary())

    print(f"Logged in as {bot.user}")
    print(f"Loaded config: {config}")

# =========================
bot.run(os.getenv("TOKEN"))
