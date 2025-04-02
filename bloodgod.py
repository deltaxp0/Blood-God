import discord
from discord.ext import commands, tasks
import asyncio
import sqlite3
import random
from datetime import datetime, timedelta, timezone

# ---------------- Configuration ----------------

GUILD_ID             = 876189935382704210  
MUTE_ROLE_ID         = 965349651119243294   
CHANNEL_ID           = 876189935827288127    
APRIL_FOOLS_ROLE_ID  = 1356358749811642478    # April Fools 2025 role ID

# Role IDs that are "too powerful" to be sacrificed. >>>> ADD STAFF HERE!! <<<<
IGNORE_ROLE_IDS      = [876195007428718652, 1024089071674462239, 939348486338543636, 
                        1268581309794750506, 1270827058930647171, 1263514885636227153, 
                        876195144733429791, 954046716619948072 ]

# ---------------- Bot Setup ----------------

intents = discord.Intents.default()
intents.members = True
intents.presences = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

active_sacrifice = False
sacrifice_timer_task = None

# ---------------- Role Change Queue ----------------
# A global queue to process role modifications sequentially.
role_change_queue = asyncio.Queue()

async def remove_role(member, role, reason):
    await member.remove_roles(role, reason=reason)

async def add_role(member, role, reason):
    await member.add_roles(role, reason=reason)

async def role_change_worker():
    while True:
        # Get the next role change coroutine from the queue
        coro = await role_change_queue.get()
        try:
            await coro
        except Exception as e:
            print("Error processing role change:", e)
        # Add a small delay between operations to help avoid rate limits.
        await asyncio.sleep(0.5)
        role_change_queue.task_done()

# ---------------- Database Setup ----------------

DB_PATH = "mutes.db"

def init_db():
    """Create the mutes table if it does not exist, including an april flag."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mutes (
            user_id INTEGER PRIMARY KEY,
            guild_id INTEGER,
            unmute_time REAL,
            original_roles TEXT,
            april INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def update_db():
    """Alter the mutes table to add the 'april' column if it does not exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE mutes ADD COLUMN april INTEGER DEFAULT 0")
    except sqlite3.OperationalError as e:
        # Likely means the column already exists.
        print("Update DB:", e)
    conn.commit()
    conn.close()

def add_mute(user_id, guild_id, unmute_time, original_roles, april=0):
    """Store mute data in the database.
       original_roles should be a list of role IDs (as ints).
       'april': 1 if the caller should get the April Fools role back, 0 otherwise.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    roles_str = ",".join(map(str, original_roles))
    cursor.execute("""
        INSERT OR REPLACE INTO mutes VALUES (?, ?, ?, ?, ?)
    """, (user_id, guild_id, unmute_time, roles_str, april))
    conn.commit()
    conn.close()

def remove_mute(user_id):
    """Remove mute record from the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM mutes WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_all_mutes():
    """Return all active mutes from the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM mutes")
    rows = cursor.fetchall()
    conn.close()
    return rows

# ---------------- Mute/Unmute Logic ----------------

async def schedule_unmute(user_id, delay):
    """Wait for delay seconds, then unmute the member."""
    await asyncio.sleep(delay)
    await unmute_member(user_id)

async def unmute_member(user_id):
    """Restore a muted user's roles, remove the mute role, and if flagged, restore the April Fools role."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT guild_id, original_roles, april FROM mutes WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    if not result:
        return

    guild_id, original_roles_str, april_flag = result
    guild = bot.get_guild(guild_id)
    if guild is None:
        return

    member = guild.get_member(user_id)
    if member is None:
        return

    muted_role = guild.get_role(MUTE_ROLE_ID)
    if muted_role in member.roles:
        # Queue removal of the mute role.
        await role_change_queue.put(remove_role(member, muted_role, "Mute duration expired"))

    # Restore original roles
    role_ids = original_roles_str.split(",") if original_roles_str else []
    for role_id in role_ids:
        role = guild.get_role(int(role_id))
        if role:
            await role_change_queue.put(add_role(member, role, "Restoring role after mute"))
    # If the april flag is set, add the April Fools role
    if april_flag == 1:
        april_role = guild.get_role(APRIL_FOOLS_ROLE_ID)
        if april_role:
            await role_change_queue.put(add_role(member, april_role, "April Fools 2025 reward"))
    remove_mute(user_id)

# ---------------- Sacrifice Command ----------------

@bot.command()
async def sacrifice(ctx, target: discord.Member):
    global active_sacrifice, sacrifice_timer_task

    if not active_sacrifice:
        return  # Do nothing if no sacrifice is active

    guild = ctx.guild
    muted_role = guild.get_role(MUTE_ROLE_ID)
    if muted_role is None:
        return

    caller = ctx.author

    if caller.id == target.id:
        await ctx.send(f"{caller.mention} You cannot sacrifice yourself!")
        return

    def has_ignored_role(member: discord.Member):
        return any(role.id in IGNORE_ROLE_IDS for role in member.roles)

    if has_ignored_role(caller):
        await ctx.send(f"{caller.mention} YOU ARE TOO POWERFUL TO BE SACRIFICED!")
        return

    if muted_role in target.roles:
        await ctx.send(f"{target.mention} is already muted!")
        return

    if has_ignored_role(target) and not has_ignored_role(caller):
        await ctx.send("Don't even try.")
        await process_mute(caller, 10 * 60, guild, muted_role)
        active_sacrifice = False
        if sacrifice_timer_task:
            sacrifice_timer_task.cancel()
            sacrifice_timer_task = None
        return

    # Set normal mute durations (in seconds)
    caller_duration = 15 * 60    # 10 minutes
    target_duration = 15 * 60     # 5 minutes

    # For the caller, pass april_fools_flag=1 so that the April Fools role is restored upon unmute.
    await process_mute(caller, caller_duration, guild, muted_role, april_fools_flag=1)
    await process_mute(target, target_duration, guild, muted_role)

    await ctx.send(f"## A WORTHY SACRIFICE: {caller.mention} and {target.mention} have been muted, for 15 minutes EACH. ##")
    active_sacrifice = False
    if sacrifice_timer_task:
        sacrifice_timer_task.cancel()
        sacrifice_timer_task = None

# --------------------------------

async def process_mute(member, duration, guild, muted_role, april_fools_flag=0):
    """
    Remove non-ignored roles from member, store them, assign the mute role,
    and schedule unmute after the specified duration.
    If april_fools_flag is 1, mark this mute so that upon unmute the member gets the April Fools role.
    """
    original_roles = [role for role in member.roles 
                      if role.name != "@everyone" and role.id not in IGNORE_ROLE_IDS]
    original_role_ids = [role.id for role in original_roles]
    unmute_time = (datetime.now(timezone.utc) + timedelta(seconds=duration)).timestamp()
    add_mute(member.id, guild.id, unmute_time, original_role_ids, april=april_fools_flag)

    for role in original_roles:
        await role_change_queue.put(remove_role(member, role, "Sacrifice mute"))
    await role_change_queue.put(add_role(member, muted_role, "Sacrifice mute"))
    bot.loop.create_task(schedule_unmute(member.id, duration))

# ---------------- Sacrifice Announcement Task ----------------

async def wait_for_sacrifice_response(channel):
    """Wait 5 minutes for a sacrifice command; if none, announce 'IGNORANT FOOLS!'."""
    try:
        await asyncio.sleep(5 * 60)
        await channel.send("IGNORANT FOOLS!")
    except asyncio.CancelledError:
        pass

async def sacrifice_announcement_loop():
    global active_sacrifice, sacrifice_timer_task
    await bot.wait_until_ready()
    
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print("Specified guild not found.")
        return

    channel = discord.utils.get(guild.channels, id=CHANNEL_ID)
    if channel is None or not isinstance(channel, discord.TextChannel):
        print("Announcement channel not found or is not a text channel in the specified guild.")
        return

    while True:
        wait_time = random.choice([700 * 60, 500 * 60, 800 * 60])
        await asyncio.sleep(wait_time)
        await channel.send("## THE BLOOD GOD DEMANDS A SACRIFICE. ##")
        await channel.send("Use !sacrifice @[user].")
        active_sacrifice = True

        sacrifice_timer_task = asyncio.create_task(wait_for_sacrifice_response(channel))
        try:
            await sacrifice_timer_task
        except asyncio.CancelledError:
            pass

        active_sacrifice = False
        sacrifice_timer_task = None

# ---------------- Debug Feature ----------------

@bot.command()
@commands.has_role("Loser 😂")
async def debugdrop(ctx):
    """
    Debug command: instantly triggers a sacrifice announcement.
    This command forces the sacrifice event immediately in the announcement channel.
    !!!!!!!!!!!REMOVE IT WHEN ADDING TO THE SERVER!!!!!!!!!!!!
    """
    global active_sacrifice, sacrifice_timer_task

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        await ctx.send("Specified guild not found.")
        return

    channel = discord.utils.get(guild.channels, id=CHANNEL_ID)
    if channel is None or not isinstance(channel, discord.TextChannel):
        await ctx.send("Announcement channel not found or is not a text channel.")
        return

    if sacrifice_timer_task:
        sacrifice_timer_task.cancel()
        sacrifice_timer_task = None

    await channel.send("THE BLOOD GOD DEMANDS A SACRIFICE. (DEBUG DROP)")
    active_sacrifice = True
    sacrifice_timer_task = asyncio.create_task(wait_for_sacrifice_response(channel))
    await ctx.send("Debug sacrifice event triggered.")
@bot.command()
@commands.has_role("Loser 😂")
async def say(ctx, *, message: str):
    # Retrieve the guild using the hard-locked guild ID.
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        await ctx.send("Specified guild not found.")
        return

    # Retrieve the target channel from that guild.
    target_channel = discord.utils.get(guild.channels, id=CHANNEL_ID)
    if target_channel is None or not isinstance(target_channel, discord.TextChannel):
        await ctx.send("Target channel not found or is not a text channel.")
        return

    # Send the message to the target channel.
    await target_channel.send(message)


@bot.command()
@commands.has_role("Loser 😂")
async def soul(ctx, target: discord.Member):
    # Retrieve the guild and main channel using your configuration.
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        await ctx.send("Specified guild not found.")
        return

    main_channel = discord.utils.get(guild.channels, id=CHANNEL_ID)
    if main_channel is None or not isinstance(main_channel, discord.TextChannel):
        await ctx.send("Main channel not found or is not a text channel.")
        return

    muted_role = guild.get_role(MUTE_ROLE_ID)
    if muted_role is None:
        await main_channel.send("Mute role is not configured.")
        return

    # Prevent self-targeting.
    if ctx.author.id == target.id:
        await main_channel.send(f"{ctx.author.mention} You cannot target yourself!")
        return

    # Prevent muting a target who is already muted.
    if muted_role in target.roles:
        await main_channel.send(f"{target.mention} is already muted!")
        return

    # Set target's mute duration (for example, 10 minutes)
    target_duration = 10 * 60

    # Announce in the main channel.
    await main_channel.send(f"YOUR SOUL BELONGS TO ME {target.mention}")

    # Mute the target only.
    await process_mute(target, target_duration, guild, muted_role)

    await main_channel.send(f"## A WORTHY SACRIFICE: {target.mention} has been muted for 10 minutes. ##")

# ---------------- Bot Events ----------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    init_db()
    update_db()  # Update the database schema if needed.
    # Start the role change worker:
    bot.loop.create_task(role_change_worker())
    bot.loop.create_task(sacrifice_announcement_loop())
    current_time = datetime.now(timezone.utc).timestamp()
    for user_id, guild_id, unmute_time, roles_str, _ in get_all_mutes():
        delay = unmute_time - current_time
        if delay < 0:
            delay = 0
        bot.loop.create_task(schedule_unmute(user_id, delay))

bot.run('MTM1NjQ4NTA4MTQ5NzIxMDkzMQ.GoEG17.cELigQwTgRgxi6NMGmGACHlj7sSEh7OmvQ3-CM')
