import discord
from discord.ext import commands
import aiosqlite
import asyncio
import random
import pathlib
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

# ==================== CONFIG ====================
TOKEN = "123"

MATCH_CHANNEL_ID = 123

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

THEME = 0x173e1d
BOT_NAME = "JYNTRIX"

# ==================== DB ====================
BASE_DIR = pathlib.Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB = str(DB_DIR / "matchmaking.db")

# ==================== GLOBALS ====================
match_queue = []
queue_lock = asyncio.Lock()
queue_task = None

active_tournaments = {}
tourney_bracket_messages = {}
tourney_data = {}

player_losses = {}  # DOUBLE ELIM

# ==================== RANK ====================
def rank_info(elo):
    if elo < 1100: return "🥉 Bronze"
    if elo < 1300: return "🥈 Silver"
    if elo < 1500: return "🥇 Gold"
    if elo < 1800: return "💎 Diamond"
    return "👑 Master"

# ==================== DB ====================
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            elo INTEGER DEFAULT 1000,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0
        )""")

        await db.execute("""CREATE TABLE IF NOT EXISTS tournaments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER
        )""")

        await db.execute("""CREATE TABLE IF NOT EXISTS tournament_players (
            tournament_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY (tournament_id, user_id)
        )""")

        await db.execute("""CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id INTEGER,
            round INTEGER,
            player1 INTEGER,
            player2 INTEGER,
            winner INTEGER
        )""")

        await db.commit()

# ==================== USER ====================
async def get_user(uid):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT elo,wins,losses FROM users WHERE id=?", (uid,)) as c:
            row = await c.fetchone()
            if not row:
                await db.execute("INSERT INTO users(id) VALUES(?)", (uid,))
                await db.commit()
                return (1000,0,0)
            return row

async def update_user(uid, change, win=False):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))
        if win:
            await db.execute("UPDATE users SET elo=elo+?, wins=wins+1 WHERE id=?", (change, uid))
        else:
            await db.execute("UPDATE users SET elo=elo+?, losses=losses+1 WHERE id=?", (change, uid))
        await db.commit()

# ==================== MATCHMAKING ====================
async def matchmaking_loop():
    global queue_task

    while True:
        await asyncio.sleep(5)

        async with queue_lock:
            if len(match_queue) < 2:
                continue

            match_queue.sort(key=lambda x: x[1])
            (p1,_),(p2,_) = match_queue.pop(0), match_queue.pop(0)

        channel = bot.get_channel(MATCH_CHANNEL_ID)
        if not channel:
            continue

        u1 = await bot.fetch_user(p1)
        u2 = await bot.fetch_user(p2)

        await u1.send(f"⚔️ Match vs {u2.name}")
        await u2.send(f"⚔️ Match vs {u1.name}")

        embed = discord.Embed(
            title="⚔️ Match Found",
            description=f"{u1.mention} vs {u2.mention}",
            color=THEME
        )

        await channel.send(embed=embed)

# ==================== DOUBLE ELIM BRACKET IMAGE ====================
async def generate_bracket(players, round_num):
    img = Image.new("RGB", (1000, 600), (25,25,25))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("arial.ttf", 20)
    except:
        font = ImageFont.load_default()

    draw.text((350,10), f"ROUND {round_num}", fill="white", font=font)

    y = 80
    for i in range(0,len(players),2):
        p1 = str(players[i])
        p2 = str(players[i+1]) if i+1 < len(players) else "BYE"

        draw.text((100,y), f"{p1}", fill="white", font=font)
        draw.text((100,y+30), f"vs {p2}", fill="white", font=font)

        y += 80

    path = "bracket.png"
    img.save(path)
    return path

# ==================== BRACKET UPDATE ====================
async def update_bracket(channel_id):
    tid = active_tournaments.get(channel_id)
    if not tid:
        return

    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT round,player1,player2,winner FROM matches WHERE tournament_id=?",(tid,)) as c:
            rows = await c.fetchall()

    embed = discord.Embed(title="🏆 LIVE BRACKET", color=THEME)

    if not rows:
        embed.description = "Waiting..."
    else:
        text=""
        for r,p1,p2,w in rows:
            u1 = await bot.fetch_user(p1)
            u2 = await bot.fetch_user(p2)

            if w:
                uw = await bot.fetch_user(w)
                text += f"R{r}: {u1.name} vs {u2.name} → {uw.name}\n"
            else:
                text += f"R{r}: {u1.name} vs {u2.name}\n"

        embed.description = text

    ch = bot.get_channel(channel_id)
    msg_id = tourney_bracket_messages.get(channel_id)

    if msg_id:
        msg = await ch.fetch_message(msg_id)
        await msg.edit(embed=embed)

# ==================== ADVANCE ROUND ====================
async def advance_round(channel_id):
    tid = active_tournaments.get(channel_id)
    if not tid:
        return

    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT player1,player2,winner,round FROM matches WHERE tournament_id=?",(tid,)) as c:
            matches = await c.fetchall()

    rounds = [m[3] for m in matches if m[3]]
    if not rounds:
        return

    last = max(rounds)
    winners = [m[2] for m in matches if m[3]==last and m[2]]

    if len(winners) <= 1:
        return

    random.shuffle(winners)
    next_round = last + 1

    async with aiosqlite.connect(DB) as db:
        for i in range(0,len(winners)-1,2):
            await db.execute(
                "INSERT INTO matches VALUES (NULL,?,?,?,NULL,NULL)",
                (tid,next_round,winners[i],winners[i+1])
            )
        await db.commit()

    await update_bracket(channel_id)

# ==================== WIN BUTTON ====================
class MatchView(discord.ui.View):
    def __init__(self,p1,p2):
        super().__init__()
        self.p1=p1
        self.p2=p2

    @discord.ui.button(label="Win", style=discord.ButtonStyle.green)
    async def win(self, interaction, button):
        if interaction.user.id not in (self.p1,self.p2):
            return await interaction.response.send_message("❌",ephemeral=True)

        winner = interaction.user
        loser = self.p2 if winner.id==self.p1 else self.p1

        player_losses.setdefault(loser,0)
        player_losses[loser]+=1

        await update_user(winner.id,25,True)
        await update_user(loser,-20,False)

        async with aiosqlite.connect(DB) as db:
            await db.execute("UPDATE matches SET winner=? WHERE player1=? OR player2=?",
                             (winner.id,self.p1,self.p1))
            await db.commit()

        await interaction.response.send_message(f"🏆 {winner.name}")

        for cid in active_tournaments:
            await advance_round(cid)

# ==================== COMMANDS ====================
@bot.command()
async def queue(ctx):
    async with queue_lock:
        match_queue.append((ctx.author.id,1000))

    await ctx.author.send("Joined queue")

@bot.command()
async def stats(ctx,member=None):
    member=member or ctx.author
    elo,w,l=await get_user(member.id)

    e=discord.Embed(title=member.name,color=THEME)
    e.add_field(name="Rank",value=rank_info(elo))
    e.add_field(name="ELO",value=elo)
    e.add_field(name="W/L",value=f"{w}/{l}")
    await ctx.send(embed=e)

# ==================== TOURNAMENT ====================
@bot.command()
async def start_tourney(ctx):
    async with aiosqlite.connect(DB) as db:
        cur=await db.execute("INSERT INTO tournaments(channel_id) VALUES(?)",(ctx.channel.id,))
        tid=cur.lastrowid
        await db.commit()

    active_tournaments[ctx.channel.id]=tid
    tourney_data[ctx.channel.id]={"tid":tid}

    embed=discord.Embed(title="🏆 BRACKET",description="Waiting...",color=THEME)
    msg=await ctx.send(embed=embed)

    tourney_bracket_messages[ctx.channel.id]=msg.id

@bot.command()
async def join_tourney(ctx):
    tid=active_tournaments.get(ctx.channel.id)
    if not tid:
        return

    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO tournament_players VALUES(?,?)",(tid,ctx.author.id))
        await db.commit()

    await ctx.send("Joined")

@bot.command()
async def begin_tourney(ctx):
    tid=active_tournaments.get(ctx.channel.id)
    if not tid:
        return

    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT user_id FROM tournament_players WHERE tournament_id=?",(tid,)) as c:
            players=[r[0] for r in await c.fetchall()]

    random.shuffle(players)

    path=await generate_bracket(players,1)
    await ctx.send(file=discord.File(path))

    async with aiosqlite.connect(DB) as db:
        for i in range(0,len(players)-1,2):
            await db.execute("INSERT INTO matches VALUES(NULL,?,?,?,NULL,NULL)",
                             (tid,1,players[i],players[i+1]))
        await db.commit()

    await update_bracket(ctx.channel.id)
    await advance_round(ctx.channel.id)

# ==================== RUN ====================
@bot.event
async def on_ready():
    await init_db()
    print("ONLINE:",bot.user)

bot.run(TOKEN)