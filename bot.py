import discord
from discord import app_commands
from discord.ext import tasks
import sqlite3
import aiohttp
import asyncio
import os
import threading
from datetime import datetime
from dotenv import load_dotenv
from bs4 import BeautifulSoup
load_dotenv()

# === CONFIGURACIÓN ===
STEAM_API_KEY = os.getenv('TOKEN_STEAM')
DISCORD_TOKEN = os.getenv('TOKEN_DISCORD')

class SteamAchievementBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.session = None
        self.ciclos_revisión = 0
        self.inicio_time = datetime.now()

    async def setup_hook(self):
        conn = sqlite3.connect('achievements.db')
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS configuracion
                          (guild_id TEXT PRIMARY KEY, channel_id TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS usuarios
                          (discord_id TEXT PRIMARY KEY, steam_id_64 TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS logros_obtenidos
                          (steam_id_64 TEXT, appid TEXT, achievement_id TEXT,
                          PRIMARY KEY (steam_id_64, appid, achievement_id))''')
        conn.commit()
        conn.close()

        self.session = aiohttp.ClientSession()
        self.check_achievements_loop.start()
        threading.Thread(target=self.consola_input, daemon=True).start()

    async def on_ready(self):
        await self.tree.sync()
        print(f'Bot conectado como {self.user}')
        print("Escribe 'help' para ver la lista comandos\n")

    # === LÓGICA DE CONSOLA ===
    def consola_input(self):
        MI_STEAM_ID = "76561199351482162"
        HK_APP_ID = "367520"
        LOGRO_CHARMED = "CHARMED"
        LOGRO_ENCHANTED = "ENCHANTED"

        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                print("\n[INFO] No se detectó terminal interactiva. Consola de comandos deshabilitada.")
                break

            if cmd == "help":
                print("\nstats      - Ver uptime y usuarios")
                print("test_achie - Elimina un logro de la DB para que el bot lo detecte como nuevo")
                print("test_msg   - Envía un mensaje de prueba a Discord")
                print("help       - Mostrar este mensaje\n")

            elif cmd == "stats":
                delta = datetime.now() - self.inicio_time
                horas, resto = divmod(int(delta.total_seconds()), 3600)
                minutos, _ = divmod(resto, 60)
                dias, horas = divmod(horas, 24)

                conn = sqlite3.connect('achievements.db')
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM usuarios")
                total_usuarios = cursor.fetchone()[0]
                conn.close()

                print(f"\nTiempo encendido:    {dias}d {horas}h {minutos}m")
                print(f"Ciclos de revisión:  {self.ciclos_revisión}")
                print(f"Usuarios en DB:      {total_usuarios}\n")

            elif cmd == "test_achie":
                conn = sqlite3.connect('achievements.db')
                cursor = conn.cursor()
                cursor.execute("DELETE FROM logros_obtenidos WHERE steam_id_64=? AND appid=? AND achievement_id=?",
                              (MI_STEAM_ID, HK_APP_ID, LOGRO_CHARMED))
                conn.commit()
                conn.close()
                print(f"\n[DB] Logro '{LOGRO_CHARMED}' eliminado de la DB local.")
                print(f"El bot lo enviará automáticamente en el próximo escaneo.\n")

            elif cmd == "test_msg":
                asyncio.run_coroutine_threadsafe(
                    self.notificar_logro("0", MI_STEAM_ID, HK_APP_ID, "Hollow Knight", LOGRO_ENCHANTED),
                    self.loop
                )
                print(f"\n[MSG] Notificación manual enviada: '{LOGRO_ENCHANTED}'\n")

    async def check_steam_privacy(self, steam_id_64):
        url = f"http://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/?appid=440&key={STEAM_API_KEY}&steamid={steam_id_64}"
        try:
            async with self.session.get(url) as resp:
                data = await resp.json()
                stats = data.get('playerstats', {})
                if not stats.get('success', False):
                    if "Profile is not public" in stats.get('error', ""):
                        return False, "❌ El perfil o los juegos son **Privados**."
                    return False, "❌ Error al verificar perfil."
                return True, None
        except:
            return False, "❌ Error de conexión con Steam."

    # === NUEVO: Obtener porcentaje global por scraping como fallback ===
    async def get_global_percentage_scraping(self, appid, display_name):
        """
        Fallback: scrapea steamcommunity.com/stats/{appid}/achievements/
        cuando la API oficial devuelve vacío.
        Compara por display_name (nombre visible del logro).
        """
        try:
            url = f"https://steamcommunity.com/stats/{appid}/achievements/"
            headers = {"Accept-Language": "en-US,en;q=0.9"}
            async with self.session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    print(f"⚠️ Scraping: status {resp.status} para appid {appid}")
                    return None
                html = await resp.text()

            soup = BeautifulSoup(html, 'html.parser')
            rows = soup.select('.achieveRow')

            if not rows:
                print(f"⚠️ Scraping: no se encontraron logros en la página de appid {appid}")
                return None

            for row in rows:
                name_tag = row.select_one('.achieveTxt h3')
                percent_tag = row.select_one('.achievePercent')
                if name_tag and percent_tag:
                    page_name = name_tag.text.strip()
                    if page_name.lower() == display_name.lower():
                        percent_str = percent_tag.text.replace('%', '').strip()
                        percentage = round(float(percent_str), 1)
                        print(f"✅ Scraping encontró '{display_name}': {percentage}%")
                        return percentage

            print(f"❌ Scraping: '{display_name}' no encontrado en página de appid {appid}")
            return None

        except Exception as e:
            print(f"⚠️ Error en scraping: {e}")
            return None

    @tasks.loop(minutes=1)
    async def check_achievements_loop(self):
        self.ciclos_revisión += 1
        conn = sqlite3.connect('achievements.db')
        cursor = conn.cursor()
        cursor.execute("SELECT discord_id, steam_id_64 FROM usuarios")
        usuarios = cursor.fetchall()

        for discord_id, steam_id_64 in usuarios:
            url_recent = f"http://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v0001/?key={STEAM_API_KEY}&steamid={steam_id_64}&format=json"
            try:
                async with self.session.get(url_recent) as resp:
                    r_recent = await resp.json()

                games = r_recent.get('response', {}).get('games', [])
                for juego in games:
                    appid = str(juego['appid'])
                    game_name = juego['name']

                    url_ach = f"http://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/?appid={appid}&key={STEAM_API_KEY}&steamid={steam_id_64}"
                    async with self.session.get(url_ach) as resp:
                        r_ach = await resp.json()

                    if r_ach.get('playerstats', {}).get('success'):
                        logros = r_ach['playerstats'].get('achievements', [])
                        for l in logros:
                            if l.get('achieved') == 1:
                                ach_id = l['apiname']
                                cursor.execute("SELECT 1 FROM logros_obtenidos WHERE steam_id_64=? AND appid=? AND achievement_id=?",
                                             (steam_id_64, appid, ach_id))

                                if cursor.fetchone() is None:
                                    cursor.execute("INSERT INTO logros_obtenidos VALUES (?, ?, ?)", (steam_id_64, appid, ach_id))
                                    conn.commit()
                                    ahora = datetime.now().strftime("%H:%M:%S")
                                    print(f"[{ahora}] NUEVO LOGRO DETECTADO")
                                    print(f"   ├ Juego: {game_name} ({appid})")
                                    print(f"   ├ Logro: {ach_id}")
                                    print(f"   └ Usuario: {steam_id_64}\n")
                                    await self.notificar_logro(discord_id, steam_id_64, appid, game_name, ach_id)
            except Exception as e:
                print(f"❌ Error escaneando a {steam_id_64}: {e}")
        conn.close()

    async def notificar_logro(self, discord_id, steam_id_64, appid, game_name, ach_id):
        # Nombre de usuario Steam
        url_user = f"http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={STEAM_API_KEY}&steamids={steam_id_64}"
        steam_persona_name = "Usuario de Steam"
        async with self.session.get(url_user) as resp:
            data_user = await resp.json()
            players = data_user.get('response', {}).get('players', [])
            if players:
                steam_persona_name = players[0]['personaname']

        url_schema = f"http://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/?key={STEAM_API_KEY}&appid={appid}"
        display_name, description, icon_url = ach_id, "", ""
        global_percentage = None

        try:
            async with self.session.get(url_schema) as resp:
                schema = await resp.json()

            if 'game' in schema and 'availableGameStats' in schema['game']:
                available_ach = schema['game']['availableGameStats']['achievements']
                for a in available_ach:
                    if a['name'] == ach_id:
                        display_name = a.get('displayName', ach_id)
                        description = a.get('description', '')
                        icon_url = a.get('icon', '')
                        break

            # === PASO 1: Intentar API oficial ===
            url_global = f"https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v0002/?gameid={appid}&key={STEAM_API_KEY}"
            async with self.session.get(url_global) as resp:
                res_global = await resp.json()

            if 'achievementpercentages' in res_global:
                all_global_ach = res_global['achievementpercentages']['achievements']
                for g_ach in all_global_ach:
                    if g_ach['name'].lower() == ach_id.lower():
                        global_percentage = round(float(g_ach.get('percent', 0)), 1)
                        print(f"✅ API: porcentaje encontrado para '{ach_id}': {global_percentage}%")
                        break

            # === PASO 2: Fallback por scraping si la API no devolvió datos ===
            if global_percentage is None:
                print(f"⚠️ API sin datos para '{ach_id}' en appid {appid}. Intentando scraping...")
                global_percentage = await self.get_global_percentage_scraping(appid, display_name)

        except Exception as e:
            import traceback
            print(f"⚠️ Error en detalles: {type(e).__name__}: {e}")
            traceback.print_exc()
            # Último intento: scraping como fallback de error
            if global_percentage is None:
                global_percentage = await self.get_global_percentage_scraping(appid, display_name)

        # COLORES SEGÚN RAREZA
        if global_percentage is None:
            embed_color = discord.Color.light_grey()
            rareza_str = "Desconocida"
            porcentaje_display = ""
        elif global_percentage <= 2.0:
            embed_color = discord.Color.from_rgb(255, 215, 0)
            rareza_str = "👑 Legendario / Ultra Raro"
            porcentaje_display = f"({global_percentage}%)"
        elif global_percentage <= 10.0:
            embed_color = discord.Color.red()
            rareza_str = "🔴 Muy Raro"
            porcentaje_display = f"({global_percentage}%)"
        elif global_percentage <= 25.0:
            embed_color = discord.Color.purple()
            rareza_str = "🟣 Raro"
            porcentaje_display = f"({global_percentage}%)"
        elif global_percentage <= 50.0:
            embed_color = discord.Color.green()
            rareza_str = "🟢 Poco Común"
            porcentaje_display = f"({global_percentage}%)"
        else:
            embed_color = discord.Color.blue()
            rareza_str = "🔵 Común"
            porcentaje_display = f"({global_percentage}%)"

        conn = sqlite3.connect('achievements.db')
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id FROM configuracion")
        canales = cursor.fetchall()
        conn.close()

        for (channel_id,) in canales:
            channel = self.get_channel(int(channel_id))
            if channel:
                embed = discord.Embed(
                    title="🏆 ¡Logro Desbloqueado!",
                    description=f"**{steam_persona_name}** ha ganado un logro en **{game_name}**",
                    color=embed_color
                )
                embed.add_field(name="Logro", value=f"**{display_name}**", inline=True)

                # Mostrar rareza sin "(None%)" cuando no hay datos
                rareza_value = f"{rareza_str} {porcentaje_display}".strip()
                embed.add_field(name="Rareza Global", value=rareza_value, inline=True)

                if description:
                    embed.add_field(name="Descripción", value=f"*{description}*", inline=False)
                if icon_url:
                    embed.set_thumbnail(url=icon_url)

                try:
                    await channel.send(embed=embed)
                except:
                    pass

# --- COMANDOS ---
bot = SteamAchievementBot()

@bot.tree.command(name="configurar", description="Define el canal de anuncios")
@app_commands.checks.has_permissions(administrator=True)
async def configurar(interaction: discord.Interaction, canal: discord.TextChannel):
    conn = sqlite3.connect('achievements.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO configuracion VALUES (?, ?)", (str(interaction.guild_id), str(canal.id)))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ Canal de logros establecido en {canal.mention}")

@bot.tree.command(name="vincular", description="Vincula tu SteamID64")
async def vincular(interaction: discord.Interaction, steamid64: str):
    await interaction.response.defer(thinking=True)

    es_publico, msg_error = await bot.check_steam_privacy(steamid64)
    if not es_publico:
        await interaction.followup.send(msg_error)
        return

    url_resumen = f"http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={STEAM_API_KEY}&steamids={steamid64}"
    steam_name = "Usuario desconocido"
    async with bot.session.get(url_resumen) as resp:
        data = await resp.json()
        players = data.get('response', {}).get('players', [])
        if players:
            steam_name = players[0]['personaname']

    conn = sqlite3.connect('achievements.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO usuarios VALUES (?, ?)", (str(interaction.user.id), steamid64))

    url_games = f"http://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/?key={STEAM_API_KEY}&steamid={steamid64}&format=json&include_played_free_games=1"
    try:
        async with bot.session.get(url_games) as resp:
            r = await resp.json()
        games = r.get('response', {}).get('games', [])
        for g in games:
            appid = str(g['appid'])
            url_ach = f"http://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/?appid={appid}&key={STEAM_API_KEY}&steamid={steamid64}"
            async with bot.session.get(url_ach) as resp:
                res_ach = await resp.json()
            if res_ach.get('playerstats', {}).get('success'):
                for l in res_ach['playerstats'].get('achievements', []):
                    if l.get('achieved') == 1:
                        cursor.execute("INSERT OR IGNORE INTO logros_obtenidos VALUES (?, ?, ?)", (steamid64, appid, l['apiname']))
        conn.commit()
        await interaction.followup.send(f"🎮 <@{interaction.user.id}> ha vinculado el perfil de **{steam_name}**.")
    except Exception as e:
        await interaction.followup.send(f"⚠️ Vinculado con errores: {e}")
    finally:
        conn.close()

bot.run(DISCORD_TOKEN)
