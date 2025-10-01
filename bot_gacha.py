# -*- coding: utf-8 -*-
import os
import time
import psycopg2
import psycopg2.extras
import discord
from discord.ext import commands
from discord import app_commands

# =========================
# ===== CONFIG BOT ========
# =========================
INTENTS = discord.Intents.default()
INTENTS.message_content = True  # pas nécessaire pour les slash cmds, mais on le laisse
BOT = commands.Bot(command_prefix="!", intents=INTENTS)

DB_PATH = "gacha.db"

# =========================
# ====== DATABASE  (PG) ===
# =========================
import os
DB_URL = os.getenv("DATABASE_URL")  # on la met dans Railway > Variables

def db():
    # Connexion Postgres; RealDictCursor => rows comme des dicts (row["gems"])
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    con = db()
    cur = con.cursor()
    # NOTE: types Postgres; pas d'AUTOINCREMENT (on utilise TEXT comme avant pour user_id)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id TEXT PRIMARY KEY,
        pseudo  TEXT NOT NULL,
        created_at BIGINT NOT NULL,
        pvp_unlock_at BIGINT NOT NULL DEFAULT 0,
        gems INTEGER NOT NULL DEFAULT 100,
        gold INTEGER NOT NULL DEFAULT 1000,
        energy INTEGER NOT NULL DEFAULT 120,
        energy_ts BIGINT NOT NULL DEFAULT 0,
        pity INTEGER NOT NULL DEFAULT 0,
        chapter INTEGER NOT NULL DEFAULT 1,
        stage INTEGER NOT NULL DEFAULT 1,
        elo INTEGER NOT NULL DEFAULT 1200,
        last_daily BIGINT NOT NULL DEFAULT 0,
        daily_stages INTEGER NOT NULL DEFAULT 0,
        daily_pulls INTEGER NOT NULL DEFAULT 0,
        daily_pvp INTEGER NOT NULL DEFAULT 0,
        weekly_stages INTEGER NOT NULL DEFAULT 0,
        weekly_pulls INTEGER NOT NULL DEFAULT 0,
        weekly_pvp INTEGER NOT NULL DEFAULT 0,
        week_epoch BIGINT NOT NULL DEFAULT 0
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS inventory(
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        rarity TEXT NOT NULL,      -- R, SR, SSR, UR, LR
        stars INTEGER NOT NULL DEFAULT 0,
        dupes INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(user_id, name)
    );
    """)
    con.commit()
    con.close()

def user_get(uid: int):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=%s", (str(uid),))
    row = cur.fetchone()
    con.close()
    return row

def ensure_user(uid: int, pseudo: str):
    row = user_get(uid)
    if row:
        return row
    t = int(time.time())
    # si tu as une constante PVP_UNLOCK_MINUTES dans ton code, tu peux la réutiliser ici
    unlock = t + (15*60)  # 15 minutes par défaut
    con = db()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO users(user_id, pseudo, created_at, pvp_unlock_at, energy, energy_ts, week_epoch)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        RETURNING *;
    """, (str(uid), pseudo, t, unlock, 120, t, t))
    row = cur.fetchone()
    con.commit()
    con.close()
    return row


# =========================
# ===== SALON PRIVÉ =======
# =========================
ACCOUNTS_CATEGORY_NAME = "comptes"

async def ensure_accounts_category(guild: discord.Guild) -> discord.CategoryChannel:
    cat = discord.utils.get(guild.categories, name=ACCOUNTS_CATEGORY_NAME)
    if cat:
        return cat
    return await guild.create_category(ACCOUNTS_CATEGORY_NAME)

async def ensure_user_channel(inter: discord.Interaction) -> discord.TextChannel:
    guild = inter.guild
    assert guild, "Utilise cette commande dans un serveur."
    cat = await ensure_accounts_category(guild)
    name = f"compte-{inter.user.id}"
    chan = discord.utils.get(cat.text_channels, name=name)
    if chan:
        return chan
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        inter.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
    }
    chan = await guild.create_text_channel(name, category=cat, overwrites=overwrites,
                                           topic=f"Salon privé de {inter.user}.")
    await chan.send(f"👋 {inter.user.mention} voici **ton salon privé**. Utilise ici tes commandes.")
    return chan

def only_in_own_channel():
    async def predicate(inter: discord.Interaction) -> bool:
        if inter.guild is None:
            await inter.response.send_message("Utilise cette commande dans un serveur.", ephemeral=True)
            return False
        expected = f"compte-{inter.user.id}"
        if isinstance(inter.channel, discord.TextChannel) and inter.channel.name == expected:
            return True
        chan = await ensure_user_channel(inter)
        try:
            await inter.response.send_message(f"➡️ Va dans ton salon privé : {chan.mention}", ephemeral=True)
        except discord.InteractionResponded:
            pass
        return False
    return app_commands.check(predicate)

# =========================
# ====== SLASH CMDS =======
# =========================
@BOT.event
async def on_ready():
    init_db()
    try:
        synced = await BOT.tree.sync()
        print(f"✅ Slash commands synchronisées : {len(synced)}")
    except Exception as e:
        print("❌ Erreur de sync:", e)
    print(f"Connecté comme {BOT.user} (ID: {BOT.user.id})")

@BOT.tree.command(name="start", description="Créer ton compte (+ salon privé)")
@app_commands.describe(pseudo="Ton pseudo en jeu")
async def start(inter: discord.Interaction, pseudo: str):
    pseudo = (pseudo or "Joueur").strip()[:20]
    ensure_user(inter.user.id, pseudo)
    chan = await ensure_user_channel(inter)
    await inter.response.send_message(
        f"✅ Compte créé pour **{pseudo}** ! Utilise tes commandes ici : {chan.mention}",
        ephemeral=True
    )

@BOT.tree.command(name="profil", description="Afficher ton profil")
@only_in_own_channel()
async def profil(inter: discord.Interaction):
    row = get_user(inter.user.id)
    if not row:
        await inter.response.send_message("Crée d'abord ton compte avec **/start <pseudo>**.", ephemeral=True)
        return
    em = discord.Embed(title=f"Profil — {row['pseudo']}", color=0x5865F2)
    em.add_field(name="Gemmes", value=str(row["gems"]))
    await inter.response.send_message(embed=em)

# =========================
# ===== RUN ===============
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN or len(TOKEN) < 50:
    raise RuntimeError("DISCORD_TOKEN manquant. Mets-le avec $env:DISCORD_TOKEN=\"...\" ou via .env")
BOT.run(TOKEN)
