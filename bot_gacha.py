# -*- coding: utf-8 -*-
import os
import time
import math
import random
import asyncio
import datetime as dt

import discord
from discord.ext import commands
from discord import app_commands

import psycopg2
import psycopg2.extras

# =========================
# ====== CONFIG BOT =======
# =========================
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.presences = False

BOT = commands.Bot(command_prefix="!", intents=INTENTS)

# ---------- Structure du serveur ----------
ACCOUNTS_CATEGORY_NAME = "comptes"         # salons privés par joueur
SIGNUP_CHANNEL_NAME    = "accueil"         # seul endroit où on peut taper /start
ARENA_LOG_CHANNEL_NAME = "arena-log"       # log public des combats (lecture seule)
PLAYER_ROLE_NAME       = "Joueur"          # rôle attribué après /start

# ---------- Gameplay ----------
MAX_ENERGY = 120                   # barre pleine
ENERGY_FULL_SECONDS = 3600         # ~1h pour recharger 120 → 2 énergie/minute
PULL_COST = 100                    # gemmes
MULTI_COST = 1000                  # gemmes (10 tirages)
SSR_RATE = 0.03
SR_RATE  = 0.15
R_RATE   = 0.82
PITY_SSR = 90                      # pitié SSR garantie à 90 tirages

STAGE_COST = 6                     # énergie par stage
CHAPTERS = 30
STAGES_PER_CHAPTER = 20            # 600 stages total

# ---------- PVP ----------
PVP_UNLOCK_MINUTES = 15
CHALLENGE_TTL_SEC  = 180
ELO_K = 32
ELO_START = 1200
CREATE_DEDICATED_FIGHT_CHANNELS = False  # sinon log public dans #arena-log

# ---------- Quêtes ----------
DAILY_REWARD_GEMS = 30
DAILY_TASKS = {
    "stages": {"goal": 3, "reward_gold": 500},
    "pulls": {"goal": 10, "reward_gold": 300},
    "pvp": {"goal": 1, "reward_gold": 400},
}
WEEKLY_REWARD_GEMS = 150
WEEKLY_TASKS = {
    "stages": {"goal": 25, "reward_gold": 2500},
    "pulls": {"goal": 50, "reward_gold": 1500},
    "pvp": {"goal": 5, "reward_gold": 2000},
}

# ---------- Pool de personnages ----------
TOP5_LR = {"Muzan", "Kokushibo", "Akaza", "Doma", "Yoriichi"}
POOL_R = ["Porteur de sabre", "Villageois", "Démon mineur", "Corbeau kasugai", "Apprenti"]
POOL_SR = ["Tanjiro", "Nezuko", "Zenitsu", "Inosuke", "Kanao", "Genya", "Aoi"]
POOL_SSR = ["Giyu", "Shinobu", "Rengoku", "Tengen", "Mitsuri", "Muichiro", "Obanai", "Sanemi",
            "Akaza", "Doma", "Kokushibo", "Muzan", "Yoriichi"]  # SSR obtenables (inclut Top 5)

# =========================
# ====== DATABASE (PG) ====
# =========================

DB_URL = os.getenv("DATABASE_URL")

def db():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    con = db(); cur = con.cursor()
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
        stars INTEGER NOT NULL DEFAULT 0,   -- pour SSR uniquement (0..5) vers UR ensuite
        dupes INTEGER NOT NULL DEFAULT 0,   -- doublons accumulés
        PRIMARY KEY(user_id, name)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pvp_challenges(
        challenger_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        created_at BIGINT NOT NULL,
        PRIMARY KEY(challenger_id, target_id)
    );
    """)
    con.commit(); con.close()

# =========================
# ====== HELPERS ==========
# =========================

def now() -> int:
    return int(time.time())

def weekly_epoch(ts: int) -> int:
    d = dt.datetime.utcfromtimestamp(ts)
    monday = d - dt.timedelta(days=d.weekday(), hours=d.hour, minutes=d.minute, seconds=d.second, microseconds=d.microsecond)
    return int(monday.timestamp())

def user_get(uid: int):
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=%s", (str(uid),))
    row = cur.fetchone(); con.close()
    return row

def update_user(uid: int, **kwargs):
    if not kwargs: return
    keys = ", ".join([f"{k}=%s" for k in kwargs])
    vals = list(kwargs.values()) + [str(uid)]
    con = db(); cur = con.cursor()
    cur.execute(f"UPDATE users SET {keys} WHERE user_id=%s", vals)
    con.commit(); con.close()

def ensure_user(uid: int, pseudo: str):
    row = user_get(uid)
    if row: return row
    t = now()
    unlock = t + PVP_UNLOCK_MINUTES*60
    con = db(); cur = con.cursor()
    cur.execute("""
        INSERT INTO users(user_id, pseudo, created_at, pvp_unlock_at, energy, energy_ts, week_epoch)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *;
    """, (str(uid), pseudo, t, unlock, MAX_ENERGY, t, weekly_epoch(t)))
    row = cur.fetchone(); con.commit(); con.close()
    return row

def regen_energy(row):
    """Retourne (energy, energy_ts) après régénération depuis energy_ts."""
    e = row["energy"]; ts = row["energy_ts"]
    if e >= MAX_ENERGY: return e, now()
    elapsed = max(0, now() - ts)
    regen = int(elapsed * (MAX_ENERGY / ENERGY_FULL_SECONDS))
    if regen <= 0: return e, ts
    e = min(MAX_ENERGY, e + regen)
    return e, now()

def energy_cost(uid: int, amount: int) -> bool:
    row = user_get(uid)
    if not row: return False
    e, ts = regen_energy(row)
    if e < amount:
        update_user(uid, energy=e, energy_ts=ts)
        return False
    update_user(uid, energy=e-amount, energy_ts=ts)
    return True

def add_inventory(uid: int, name: str, rarity: str):
    """Ajoute le perso si nouveau, sinon incrémente les doublons."""
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM inventory WHERE user_id=%s AND name=%s", (str(uid), name))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE inventory SET dupes=dupes+1 WHERE user_id=%s AND name=%s", (str(uid), name))
        con.commit(); con.close()
        return False, row["rarity"]
    else:
        cur.execute("INSERT INTO inventory(user_id,name,rarity,stars,dupes) VALUES (%s,%s,%s,%s,%s)",
                    (str(uid), name, rarity, 0, 0))
        con.commit(); con.close()
        return True, rarity

def get_inventory(uid: int):
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM inventory WHERE user_id=%s ORDER BY rarity DESC, stars DESC, name ASC", (str(uid),))
    rows = cur.fetchall(); con.close()
    return rows

def pity_increment(uid: int, inc: int):
    row = user_get(uid)
    p = row["pity"] + inc
    update_user(uid, pity=p)

def pity_reset(uid: int):
    update_user(uid, pity=0)

def roll_one(uid: int) -> dict:
    """Effectue 1 invocation et renvoie un dict {name, rarity, new, note}"""
    row = user_get(uid); pity = row["pity"]
    force_ssr = pity >= (PITY_SSR - 1)

    r = random.random()
    if force_ssr or r < SSR_RATE:
        pool = POOL_SSR; rarity = "SSR"
    elif r < SSR_RATE + SR_RATE:
        pool = POOL_SR; rarity = "SR"
    else:
        pool = POOL_R; rarity = "R"

    name = random.choice(pool)
    new, _rar = add_inventory(uid, name, rarity)

    if rarity == "SSR":
        pity_reset(uid)
    else:
        pity_increment(uid, 1)

    return {"name": name, "rarity": rarity, "new": new,
            "note": ("⭐ Nouvelle carte !" if new else "🔁 Doublon")}

def multi_roll(uid: int):
    results = []
    for _ in range(10):
        results.append(roll_one(uid))
    return results

def next_star_cost(current_stars: int) -> int:
    nxt = current_stars + 1
    return max(1, nxt) if nxt <= 5 else 0

# =========================
# ====== SERVER SETUP =====
# =========================

def slugify_channel(name: str) -> str:
    import re, unicodedata
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9\- ]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s or "joueur"

async def ensure_guild_setup(guild: discord.Guild) -> dict:
    result = {"category": None, "signup": None, "arena": None, "player_role": None}

    role = discord.utils.get(guild.roles, name=PLAYER_ROLE_NAME)
    if role is None:
        role = await guild.create_role(name=PLAYER_ROLE_NAME, reason="Rôle joueurs")
    result["player_role"] = role

    cat = discord.utils.get(guild.categories, name=ACCOUNTS_CATEGORY_NAME)
    if cat is None:
        cat = await guild.create_category(ACCOUNTS_CATEGORY_NAME, reason="Salons privés des comptes")
    result["category"] = cat

    signup = discord.utils.get(guild.text_channels, name=SIGNUP_CHANNEL_NAME)
    if signup is None:
        signup = await guild.create_text_channel(SIGNUP_CHANNEL_NAME, reason="Salon d'inscription")
    await signup.set_permissions(guild.default_role, view_channel=True, send_messages=True)
    await signup.set_permissions(role, send_messages=False)
    result["signup"] = signup

    arena = discord.utils.get(guild.text_channels, name=ARENA_LOG_CHANNEL_NAME)
    if arena is None:
        arena = await guild.create_text_channel(ARENA_LOG_CHANNEL_NAME, reason="Journal des combats")
    await arena.set_permissions(guild.default_role, view_channel=True, send_messages=False)
    result["arena"] = arena
    return result

async def create_private_account_channel(guild: discord.Guild, user: discord.Member) -> discord.TextChannel:
    setup = await ensure_guild_setup(guild)
    category = setup["category"]

    raw_name = user.display_name or user.name
    chan_name = slugify_channel(raw_name)

    for ch in category.text_channels:
        if ch.name == chan_name:
            await ch.set_permissions(guild.default_role, view_channel=False)
            await ch.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True)
            await ch.set_permissions(guild.me, view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)
            return ch

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user:               discord.PermissionOverwrite(view_channel=True,  send_messages=True, read_message_history=True),
        guild.me:           discord.PermissionOverwrite(view_channel=True,  send_messages=True, manage_channels=True, read_message_history=True),
    }
    ch = await guild.create_text_channel(chan_name, category=category, overwrites=overwrites,
                                         reason=f"Salon privé de {user.display_name}")
    return ch

# =========================
# ====== EMBEDS / RP ======
# =========================

def kagaya_rules_embed(guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(
        title="Bienvenue au Domaine Ubuyashiki",
        description=(
            "Je suis **Kagaya Ubuyashiki**.\n\n"
            f"• Va dans `#{SIGNUP_CHANNEL_NAME}` et tape **/start pseudo** pour créer ton compte.\n"
            "• Un **salon privé** à ton nom sera créé (toi seul peux y écrire).\n"
            "• Toutes tes commandes (tirages, histoire, quêtes, pvp) se font **dans ton salon**.\n"
            f"• Merci de ne pas écrire dans `#{ARENA_LOG_CHANNEL_NAME}` (journal public des combats).\n\n"
            "***Raretés & progression***\n"
            "R/SR/SSR au tirage (pitié SSR à 90). SSR → étoiles avec doublons, puis **UR**. "
            "Top 5 (Muzan, Kokushibo, Akaza, Doma, Yoriichi) → **LR** après UR + doublon UR.\n\n"
            "***Énergie***\n"
            "La barre se remplit en ~1h (joue sans prise de tête 😉).\n\n"
            "Que tes pas soient guidés par la bienveillance."
        ),
        color=0x93c5fd
    )
    e.set_author(name="Kagaya Ubuyashiki")
    return e

# =========================
# ====== COMMANDES  =======
# =========================

def only_in_own_channel():
    async def predicate(inter: discord.Interaction):
        setup = await ensure_guild_setup(inter.guild)
        category = setup["category"]
        return inter.channel.category_id == category.id if inter.channel.category_id else False
    return app_commands.check(predicate)

@BOT.tree.command(name="regles", description="Kagaya expose les règles du serveur (admin).")
@commands.has_permissions(manage_guild=True)
async def regles_cmd(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    setup = await ensure_guild_setup(inter.guild)
    embed = kagaya_rules_embed(inter.guild)
    msg = await setup["signup"].send(embed=embed)
    try:
        await msg.pin()
    except: pass
    await inter.followup.send("Règles publiées et épinglées dans #accueil.", ephemeral=True)

@BOT.tree.command(name="start", description="Créer ton compte (à utiliser dans #accueil).")
@app_commands.describe(pseudo="Ton pseudo en jeu")
async def start(inter: discord.Interaction, pseudo: str):
    await inter.response.defer(ephemeral=True)
    setup = await ensure_guild_setup(inter.guild)
    signup = setup["signup"]
    player_role = setup["player_role"]

    if inter.channel_id != signup.id:
        return await inter.followup.send(
            f"Va dans {signup.mention} pour créer ton compte, s’il te plaît 🙏", ephemeral=True
        )

    m: discord.Member = inter.user
    user_row = ensure_user(m.id, pseudo)
    private_ch = await create_private_account_channel(inter.guild, m)

    try:
        if player_role not in m.roles:
            await m.add_roles(player_role, reason="Compte créé")
    except: pass

    rp = (
        f"Bienvenue, **{pseudo}**.\n\n"
        "Je suis **Kagaya Ubuyashiki**. Ce salon est **le tien** : "
        "toi seul (avec moi) peux y écrire.\n\n"
        "• Tape **/profil** pour voir tes ressources.\n"
        f"• **/tirage** ({PULL_COST}💎) et **/multi** ({MULTI_COST}💎) pour invoquer.\n"
        f"• **/histoire** (−{STAGE_COST}⚡) pour progresser dans les chapitres.\n"
        "• **/quetes** pour les journalières et hebdos.\n"
        "• **/pvp defier** quand tu seras prêt à affronter d’autres pour l’ELO.\n\n"
        "Puissent tes choix être empreints de bienveillance."
    )
    await private_ch.send(rp)
    await inter.followup.send(
        f"Compte créé pour **{m.mention}** ! Utilise tes commandes ici : {private_ch.mention}",
        ephemeral=False
    )

@BOT.tree.command(name="profil", description="Voir ton profil (gemmes, or, énergie, progression).")
async def profil(inter: discord.Interaction):
    await inter.response.defer(ephemeral=False)
    row = ensure_user(inter.user.id, inter.user.display_name or inter.user.name)
    e, ts = regen_energy(row)
    if e != row["energy"]:
        update_user(inter.user.id, energy=e, energy_ts=ts)
    embed = discord.Embed(title=f"Profil de {row['pseudo']}", color=0x89cff0)
    embed.add_field(name="Gemmes 💎", value=str(row["gems"]), inline=True)
    embed.add_field(name="Or 🪙", value=str(row["gold"]), inline=True)
    embed.add_field(name="Énergie ⚡", value=f"{e}/{MAX_ENERGY}", inline=True)
    embed.add_field(name="Chapitre / Stage", value=f"{row['chapter']} / {row['stage']}", inline=True)
    embed.add_field(name="ELO 🏆", value=str(row["elo"]), inline=True)
    inv = get_inventory(inter.user.id)
    embed.add_field(name="Persos", value=f"{len(inv)} obtenus", inline=True)
    await inter.followup.send(embed=embed)

@BOT.tree.command(name="tirage", description=f"Invoquer 1 personnage ({PULL_COST} gemmes).")
@only_in_own_channel()
async def tirage(inter: discord.Interaction):
    await inter.response.defer(ephemeral=False)
    row = user_get(inter.user.id)
    if row["gems"] < PULL_COST:
        return await inter.followup.send("Pas assez de 💎.", ephemeral=True)
    update_user(inter.user.id, gems=row["gems"] - PULL_COST, daily_pulls=row["daily_pulls"]+1, weekly_pulls=row["weekly_pulls"]+1)
    res = roll_one(inter.user.id)
    msg = f"**{res['name']}** ({res['rarity']}) — {res['note']}"
    await inter.followup.send(msg)

@BOT.tree.command(name="multi", description=f"Invoquer 10 personnages ({MULTI_COST} gemmes).")
@only_in_own_channel()
async def multi(inter: discord.Interaction):
    await inter.response.defer(ephemeral=False)
    row = user_get(inter.user.id)
    if row["gems"] < MULTI_COST:
        return await inter.followup.send("Pas assez de 💎.", ephemeral=True)
    update_user(inter.user.id, gems=row["gems"] - MULTI_COST, daily_pulls=row["daily_pulls"]+10, weekly_pulls=row["weekly_pulls"]+10)
    results = multi_roll(inter.user.id)
    lines = [f"• **{r['name']}** ({r['rarity']}) — {r['note']}" for r in results]
    await inter.followup.send("\n".join(lines))

@BOT.tree.command(name="inventaire", description="Liste tes personnages.")
@only_in_own_channel()
async def inventaire(inter: discord.Interaction):
    await inter.response.defer(ephemeral=False)
    inv = get_inventory(inter.user.id)
    if not inv:
        return await inter.followup.send("Inventaire vide.")
    chunks = []
    for row in inv:
        extra = ""
        if row["rarity"] == "SSR":
            extra = f" — ⭐{row['stars']} (doublons: {row['dupes']})"
        elif row["rarity"] in ("UR", "LR"):
            extra = f" — (doublons: {row['dupes']})"
        chunks.append(f"• **{row['name']}** [{row['rarity']}] {extra}")
    text = "\n".join(chunks)
    for i in range(0, len(text), 1800):
        await inter.followup.send(text[i:i+1800])

@BOT.tree.command(name="promouvoir", description="Promouvoir R->SR (3 dupes), SR->SSR (5 dupes) ou SSR⭐/UR/LR via doublons.")
@app_commands.describe(nom="Nom exact du personnage")
@only_in_own_channel()
async def promouvoir(inter: discord.Interaction, nom: str):
    await inter.response.defer(ephemeral=False)
    con = db(); cur = con.cursor()
    cur.execute("SELECT name,rarity,stars,dupes FROM inventory WHERE user_id=%s AND name=%s", (str(inter.user.id), nom))
    row = cur.fetchone()
    if not row:
        con.close(); return await inter.followup.send("Perso introuvable.", ephemeral=True)
    name, rarity, stars, dup = row["name"], row["rarity"], row["stars"], row["dupes"]

    if rarity == "R":
        need = 3
        if dup < need:
            con.close(); return await inter.followup.send(f"Il faut {need} doublons pour **R->SR**.")
        cur.execute("UPDATE inventory SET rarity='SR', dupes=dupes-%s WHERE user_id=%s AND name=%s", (need, str(inter.user.id), name))
        con.commit(); con.close()
        return await inter.followup.send(f"⬆️ **{name}** est promu **SR** !")

    if rarity == "SR":
        need = 5
        if dup < need:
            con.close(); return await inter.followup.send(f"Il faut {need} doublons pour **SR->SSR**.")
        cur.execute("UPDATE inventory SET rarity='SSR', dupes=dupes-%s WHERE user_id=%s AND name=%s", (need, str(inter.user.id), name))
        con.commit(); con.close()
        return await inter.followup.send(f"⬆️ **{name}** est promu **SSR** !")

    if rarity == "SSR":
        if stars < 5:
            need = next_star_cost(stars)
            if dup < need:
                con.close(); return await inter.followup.send(f"Il faut {need} doublon(s) pour passer ⭐{stars+1}.")
            cur.execute("UPDATE inventory SET stars=stars+1, dupes=dupes-%s WHERE user_id=%s AND name=%s",
                        (need, str(inter.user.id), name))
            con.commit(); con.close()
            return await inter.followup.send(f"⭐ **{name}** passe à **{stars+1}** étoile(s) !")
        else:
            need = 6
            if dup < need:
                con.close(); return await inter.followup.send(f"Il faut {need} doublons pour **SSR⭐5 -> UR**.")
            cur.execute("UPDATE inventory SET rarity='UR', dupes=dupes-%s WHERE user_id=%s AND name=%s",
                        (need, str(inter.user.id), name))
            con.commit(); con.close()
            return await inter.followup.send(f"🌟 **{name}** devient **UR** !")

    if rarity == "UR":
        if name in TOP5_LR:
            need = 1
            if dup < need:
                con.close(); return await inter.followup.send(f"Il faut {need} doublon **UR** pour éveiller **{name}** en **LR**.")
            cur.execute("UPDATE inventory SET rarity='LR', dupes=dupes-%s WHERE user_id=%s AND name=%s",
                        (need, str(inter.user.id), name))
            con.commit(); con.close()
            return await inter.followup.send(f"💠 **{name}** éveillé **LR** !")
        else:
            con.close(); return await inter.followup.send("Seuls Muzan, Kokushibo, Akaza, Doma, Yoriichi peuvent passer **LR**.")

    con.close()
    await inter.followup.send("Cette promotion n'est pas applicable.")

@BOT.tree.command(name="histoire", description=f"Progresse dans l'histoire (−{STAGE_COST} énergie).")
@only_in_own_channel()
async def histoire(inter: discord.Interaction):
    await inter.response.defer(ephemeral=False)
    uid = inter.user.id
    if not energy_cost(uid, STAGE_COST):
        return await inter.followup.send(f"Pas assez d'énergie ⚡ (coût {STAGE_COST}).", ephemeral=True)

    row = user_get(uid)
    ch, st = row["chapter"], row["stage"]
    st += 1
    if st > STAGES_PER_CHAPTER:
        st = 1; ch = min(CHAPTERS, ch+1)
    update_user(uid, chapter=ch, stage=st, daily_stages=row["daily_stages"]+1, weekly_stages=row["weekly_stages"]+1)
    await inter.followup.send(f"Tu avances à **Chapitre {ch} — Stage {st}**. Courage !")

@BOT.tree.command(name="energie", description="Voir ta barre d'énergie et le temps de recharge.")
@only_in_own_channel()
async def energie(inter: discord.Interaction):
    await inter.response.defer(ephemeral=False)
    row = user_get(inter.user.id)
    e, ts = regen_energy(row)
    if e != row["energy"]:
        update_user(inter.user.id, energy=e, energy_ts=ts)
    missing = MAX_ENERGY - e
    secs = int(missing * (ENERGY_FULL_SECONDS / MAX_ENERGY))
    await inter.followup.send(f"Énergie : **{e}/{MAX_ENERGY}** ⚡ — pleine dans **{secs//60} min**.")

@BOT.tree.command(name="quetes", description="Voir tes quêtes journalières / hebdomadaires et réclamer.")
@only_in_own_channel()
async def quetes(inter: discord.Interaction):
    await inter.response.defer(ephemeral=False)
    row = user_get(inter.user.id)
    t = now()
    if t - row["last_daily"] >= 24*3600:
        update_user(inter.user.id, last_daily=t, daily_stages=0, daily_pulls=0, daily_pvp=0)
        row = user_get(inter.user.id)
    if weekly_epoch(t) != row["week_epoch"]:
        update_user(inter.user.id, week_epoch=weekly_epoch(t), weekly_stages=0, weekly_pulls=0, weekly_pvp=0)
        row = user_get(inter.user.id)

    daily_lines = []
    daily_done = True
    for k, info in DAILY_TASKS.items():
        cur = row[f"daily_{k}"]; goal = info["goal"]
        daily_lines.append(f"• {k}: {cur}/{goal} — +{info['reward_gold']}🪙")
        if cur < goal: daily_done = False

    weekly_lines = []
    weekly_done = True
    for k, info in WEEKLY_TASKS.items():
        cur = row[f"weekly_{k}"]; goal = info["goal"]
        weekly_lines.append(f"• {k}: {cur}/{goal} — +{info['reward_gold']}🪙")
        if cur < goal: weekly_done = False

    text = "**Journalières**\n" + "\n".join(daily_lines)
    if daily_done: text += f"\n➡️ tape **/quete_daily** pour réclamer **{DAILY_REWARD_GEMS}💎**"
    text += "\n\n**Hebdomadaires**\n" + "\n".join(weekly_lines)
    if weekly_done: text += f"\n➡️ tape **/quete_weekly** pour réclamer **{WEEKLY_REWARD_GEMS}💎**"
    await inter.followup.send(text)

@BOT.tree.command(name="quete_daily", description="Réclamer la récompense journalière si complète.")
@only_in_own_channel()
async def quete_daily(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    row = user_get(inter.user.id)
    if any(row[f"daily_{k}"] < info["goal"] for k, info in DAILY_TASKS.items()):
        return await inter.followup.send("Quêtes journalières pas encore complètes.")
    update_user(inter.user.id, gems=row["gems"]+DAILY_REWARD_GEMS)
    await inter.followup.send(f"+{DAILY_REWARD_GEMS}💎 reçus !")

@BOT.tree.command(name="quete_weekly", description="Réclamer la récompense hebdomadaire si complète.")
@only_in_own_channel()
async def quete_weekly(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    row = user_get(inter.user.id)
    if any(row[f"weekly_{k}"] < info["goal"] for k, info in WEEKLY_TASKS.items()):
        return await inter.followup.send("Quêtes hebdomadaires pas encore complètes.")
    update_user(inter.user.id, gems=row["gems"]+WEEKLY_REWARD_GEMS)
    await inter.followup.send(f"+{WEEKLY_REWARD_GEMS}💎 reçus !")

# =========================
# ====== PVP & ELO ========
# =========================

def elo_expected(a, b):
    return 1 / (1 + 10 ** ((b - a) / 400))

def elo_update(a, b, result_a):  # result_a: 1 win, 0 loss
    ea = elo_expected(a, b); eb = elo_expected(b, a)
    na = round(a + ELO_K * (result_a - ea))
    nb = round(b + ELO_K * ((1-result_a) - eb))
    return na, nb

@BOT.tree.command(name="pvp", description="Défier un joueur en duel (ELO).")
@app_commands.describe(action="defier/accept", cible="@joueur (si defier)")
@only_in_own_channel()
async def pvp(inter: discord.Interaction, action: str, cible: discord.Member=None):
    await inter.response.defer(ephemeral=False)
    setup = await ensure_guild_setup(inter.guild)
    arena = setup["arena"]
    uid = str(inter.user.id)

    if action.lower() == "defier":
        if not cible or cible.bot or cible.id == inter.user.id:
            return await inter.followup.send("Mentionne un adversaire valide.")
        con = db(); cur = con.cursor()
        cur.execute("INSERT INTO pvp_challenges(challenger_id,target_id,created_at) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (uid, str(cible.id), now()))
        con.commit(); con.close()
        await inter.followup.send(f"{cible.mention}, {inter.user.mention} te défie ! Tu as {CHALLENGE_TTL_SEC//60} min pour **/pvp accept**.")
        return

    if action.lower() == "accept":
        con = db(); cur = con.cursor()
        cur.execute("SELECT * FROM pvp_challenges WHERE target_id=%s ORDER BY created_at DESC", (uid,))
        row = cur.fetchone()
        if not row or now() - row["created_at"] > CHALLENGE_TTL_SEC:
            con.close(); return await inter.followup.send("Aucun défi valide trouvé.")
        challenger_id = row["challenger_id"]
        cur.execute("DELETE FROM pvp_challenges WHERE challenger_id=%s AND target_id=%s", (challenger_id, uid))
        con.commit(); con.close()

        a = user_get(int(challenger_id)); b = user_get(inter.user.id)
        ea = elo_expected(a["elo"], b["elo"])
        win_a = random.random() < ea
        na, nb = elo_update(a["elo"], b["elo"], 1 if win_a else 0)
        update_user(int(challenger_id), elo=na, weekly_pvp=a["weekly_pvp"]+1, daily_pvp=a["daily_pvp"]+1)
        update_user(inter.user.id, elo=nb, weekly_pvp=b["weekly_pvp"]+1, daily_pvp=b["daily_pvp"]+1)

        text = f"🗡️ **Duel** : <@{challenger_id}> vs {inter.user.mention}\n"
        text += f"Gagnant : {'<@'+challenger_id+'>' if win_a else inter.user.mention}\n"
        text += f"ELO: {a['elo']}→{na} | {b['elo']}→{nb}"
        await arena.send(text)
        return await inter.followup.send("Résultat publié dans #arena-log.")

    await inter.followup.send("Utilise `/pvp action:defier cible:@joueur` ou `/pvp action:accept`.")

@BOT.tree.command(name="classement", description="Top 10 ELO du serveur.")
async def classement(inter: discord.Interaction):
    await inter.response.defer(ephemeral=False)
    con = db(); cur = con.cursor()
    cur.execute("SELECT user_id, pseudo, elo FROM users ORDER BY elo DESC LIMIT 10")
    rows = cur.fetchall(); con.close()
    if not rows: return await inter.followup.send("Pas de joueurs.")
    lines = [f"{i+1}. **{r['pseudo']}** — {r['elo']} ELO" for i, r in enumerate(rows)]
    await inter.followup.send("\n".join(lines))

# =========================
# ====== ADMIN MOD  =======
# =========================

def is_admin():
    async def pred(inter: discord.Interaction):
        return inter.user.guild_permissions.manage_guild
    return app_commands.check(pred)

@BOT.tree.command(name="admin_gems", description="(Admin) Donner des gemmes à un joueur.")
@is_admin()
@app_commands.describe(joueur="Membre", montant="Nombre de gemmes à ajouter")
async def admin_gems(inter: discord.Interaction, joueur: discord.Member, montant: int):
    await inter.response.defer(ephemeral=True)
    row = ensure_user(joueur.id, joueur.display_name or joueur.name)
    update_user(joueur.id, gems=row["gems"]+montant)
    await inter.followup.send(f"{montant}💎 ajoutés à **{row['pseudo']}**.")

@BOT.tree.command(name="admin_perso", description="(Admin) Ajouter un personnage au joueur.")
@is_admin()
@app_commands.describe(joueur="Membre", nom="Nom du perso", rarete="R/SR/SSR/UR/LR")
async def admin_perso(inter: discord.Interaction, joueur: discord.Member, nom: str, rarete: str):
    await inter.response.defer(ephemeral=True)
    rarete = rarete.upper()
    if rarete not in ("R","SR","SSR","UR","LR"):
        return await inter.followup.send("Rareté invalide.")
    ensure_user(joueur.id, joueur.display_name or joueur.name)
    new, r = add_inventory(joueur.id, nom, rarete)
    await inter.followup.send(f"{'Nouveau' if new else 'Doublon'} **{nom}** [{rarete}] pour {joueur.mention}.")

# =========================
# ====== BOT LIFECYCLE ====
# =========================

@BOT.event
async def on_ready():
    init_db()
    try:
        await BOT.tree.sync()
    except Exception:
        pass
    for g in BOT.guilds:
        try:
            await ensure_guild_setup(g)
        except Exception:
            pass
    print(f"Connecté comme {BOT.user} (ID: {BOT.user.id})")

# =========================
# ====== MAIN =============
# =========================

if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN or not DB_URL:
        raise RuntimeError("DISCORD_TOKEN ou DATABASE_URL manquant. Ajoute-les dans Railway > Variables.")
    BOT.run(TOKEN)
