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

# Daily stats for summary
daily_stats = {
    "approved": 0,
    "denied": 0,
    "blacklisted": 0,
    "autokicked": 0,
    "joins": []
}

# =========================
# CONFIG
# =========================
config = {
    "log_channel": None,
    "category": None,
    "male_role": None,
    "female_role": None,
    "unverified_role": None,
    "staff_role": None
}

# =========================
# DATABASE INIT
# =========================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            user_id INTEGER PRIMARY KEY
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
            key TEXT PRIMARY KEY,
            value INTEGER
        )
        """)
        await db.commit()

# =========================
# DB HELPERS
# =========================
async def add_blacklist(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO blacklist (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def remove_blacklist(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM blacklist WHERE user_id=?", (user_id,))
        await db.commit()

async def is_blacklisted(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM blacklist WHERE user_id=?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None

async def save_config():
    async with aiosqlite.connect(DB_NAME) as db:
        for key, value in config.items():
            if value is not None:
                await db.execute(
                    "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                    (key, value)
                )
        await db.commit()

async def load_config():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT key, value FROM config") as cursor:
            rows = await cursor.fetchall()
            for key, value in rows:
                if key in config:
                    config[key] = value

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
    log_channel_id = config.get("log_channel")
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
# SETUP COMMAND
# =========================
@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    guild = ctx.guild

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

    config.update({
        "log_channel": log_channel.id,
        "category": category.id,
        "male_role": male.id,
        "female_role": female.id,
        "unverified_role": unverified.id,
        "staff_role": staff.id
    })

    await save_config()
    await ctx.send("✅ Setup complete")

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
    await ctx.send(f"✅ Requirement set for {gender}")

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
    await remove_blacklist(user_id)
    await ctx.send(f"✅ Unblacklisted {user_id}")

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
        staff_role = interaction.guild.get_role(config["staff_role"])
        return interaction.user.guild_permissions.administrator or staff_role in interaction.user.roles

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        member = interaction.guild.get_member(self.user_id)

        male = interaction.guild.get_role(config["male_role"])
        female = interaction.guild.get_role(config["female_role"])
        unverified = interaction.guild.get_role(config["unverified_role"])

        await member.remove_roles(unverified)

        role = male if self.gender == "male" else female
        await member.add_roles(role)

        try:
            await member.send("✅ You have been approved and verified.")
        except:
            pass

        daily_stats["approved"] += 1

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
                ("Channel", interaction.channel.mention, True)
            ]
        )

        await interaction.response.send_message("Approved")
        await interaction.channel.delete()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        member = interaction.guild.get_member(self.user_id)

        try:
            await member.send("❌ Your verification was denied.")
        except:
            pass

        await member.kick(reason="Denied")

        daily_stats["denied"] += 1

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
                ("Channel", interaction.channel.mention, True)
            ]
        )

        await interaction.response.send_message("Denied")
        await interaction.channel.delete()

    @discord.ui.button(label="Blacklist", style=discord.ButtonStyle.secondary)
    async def blacklist(self, interaction, button):
        if not self.is_staff(interaction):
            return await interaction.response.send_message("Staff only", ephemeral=True)

        member = interaction.guild.get_member(self.user_id)

        await add_blacklist(member.id)

        try:
            await member.send("🚫 You have been blacklisted from this server.")
        except:
            pass

        await member.kick(reason="Blacklisted")

        daily_stats["blacklisted"] += 1

        await log_action(
            interaction.guild,
            "⚫ Blacklisted",
            f"{interaction.user.mention} blacklisted {member.mention}.",
            color=0x000000,
            fields=[
                ("Staff", interaction.user.mention, True),
                ("User", member.mention, True),
                ("User ID", str(member.id), True),
                ("Channel", interaction.channel.mention, True)
            ]
        )

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

    unverified_role = guild.get_role(config["unverified_role"])
    if unverified_role in member.roles:
        try:
            await member.send("⏰ You did not complete verification in time and were removed from the server.")
        except:
            pass
        await member.kick(reason="Verification timeout")

        daily_stats["autokicked"] += 1

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
    if any(v is None for v in config.values()):
        male = discord.utils.get(guild.roles, name="Male")
        female = discord.utils.get(guild.roles, name="Female")
        unverified = discord.utils.get(guild.roles, name="Unverified")
        staff = discord.utils.get(guild.roles, name="Staff")
        category = discord.utils.get(guild.categories, name="Verification Tickets")
        log_channel = discord.utils.get(guild.text_channels, name="verification-logs")

        if all([male, female, unverified, staff, category, log_channel]):
            config.update({
                "log_channel": log_channel.id,
                "category": category.id,
                "male_role": male.id,
                "female_role": female.id,
                "unverified_role": unverified.id,
                "staff_role": staff.id
            })
            await save_config()

            await log_action(
                guild,
                "🛠️ Config Auto-Repaired",
                "Missing roles/channels were detected and automatically restored.",
                color=0xFEE75C
            )

@bot.event
async def on_member_join(member):
    await ensure_config(member.guild)

    if any(v is None for v in config.values()):
        print(f"Config not set up for guild {member.guild.name}, skipping member join.")
        return

    # Anti-rejoin cooldown
    if member.id in cooldowns and time.time() < cooldowns[member.id]:
        try:
            await member.send("⏳ You recently left and cannot rejoin yet. Please try again later.")
        except:
            pass
        await member.kick(reason="Rejoin cooldown")

        await log_action(
            member.guild,
            "🔁 Rejoin Cooldown Kick",
            f"{member.mention} was kicked for rejoining too quickly.",
            color=0xED4245,
            fields=[
                ("User", member.mention, True),
                ("User ID", str(member.id), True)
            ]
        )
        return

    if await is_blacklisted(member.id):
        await member.kick(reason="Blacklisted")

        await log_action(
            member.guild,
            "🚫 Blacklisted User Attempted To Join",
            f"{member.mention} attempted to join but is blacklisted.",
            color=0xED4245,
            fields=[
                ("User", member.mention, True),
                ("User ID", str(member.id), True)
            ]
        )
        return

    guild = member.guild

    unverified = guild.get_role(config["unverified_role"])
    await member.add_roles(unverified)

    category = guild.get_channel(config["category"])
    staff_role = guild.get_role(config["staff_role"])

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
        ),

        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True
        )
    }

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

    daily_stats["joins"].append(discord.utils.utcnow().hour)

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

    # Start auto-kick timer
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
# STAFF CLAIM / ALIAS / STAFF ACTIVITY
# =========================
@bot.event
async def on_message(message):
    if message.author.bot:
        return await bot.process_commands(message)

    guild = message.guild
    if not guild:
        return await bot.process_commands(message)

    channel = message.channel

    if channel.category and channel.category.id == config.get("category") and channel.topic:
        if channel.topic.startswith("ticket_for:"):
            try:
                user_id = int(channel.topic.split("ticket_for:")[1].split("|")[0])
            except:
                user_id = None

            staff_role = guild.get_role(config.get("staff_role"))
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

    await bot.process_commands(message)

# =========================
# STAFF INACTIVITY CHECK
# =========================
async def staff_inactivity_check():
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
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
# DAILY SUMMARY
# =========================
async def daily_summary():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = discord.utils.utcnow()
        if now.hour == 23 and now.minute == 59:
            for guild in bot.guilds:
                if daily_stats["joins"]:
                    peak_hour = max(set(daily_stats["joins"]), key=daily_stats["joins"].count)
                else:
                    peak_hour = "N/A"

                await log_action(
                    guild,
                    "📊 Daily Verification Summary",
                    "Here is the summary of today's verification activity:",
                    color=0x5865F2,
                    fields=[
                        ("Approved", str(daily_stats["approved"]), True),
                        ("Denied", str(daily_stats["denied"]), True),
                        ("Blacklisted", str(daily_stats["blacklisted"]), True),
                        ("Auto-Kicked", str(daily_stats["autokicked"]), True),
                        ("Peak Join Hour", str(peak_hour), True)
                    ]
                )

            daily_stats["approved"] = 0
            daily_stats["denied"] = 0
            daily_stats["blacklisted"] = 0
            daily_stats["autokicked"] = 0
            daily_stats["joins"] = []

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
        embed.add_field(name=".requirements <gender> <text>", value="Set verification requirements.", inline=False)
        embed.add_field(name=".unblacklist <user_id>", value="Remove a user from blacklist.", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Staff", style=discord.ButtonStyle.success)
    async def staff(self, interaction, button):
        embed = discord.Embed(
            title="🛠️ Staff Commands",
            description="Commands and controls for staff.",
            color=0x57F287
        )
        embed.add_field(name=".setup", value="Initial server setup.", inline=False)
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
            description="Verification & moderation bot with logging, tickets, and staff tools.",
            color=0xED4245
        )
        embed.add_field(name="Developer", value="Fuad", inline=False)
        embed.add_field(name="Features", value="Verification • Tickets • Logging • Staff Tools", inline=False)

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
    print(f"Config: {config}")

# =========================
bot.run(os.getenv("TOKEN"))
