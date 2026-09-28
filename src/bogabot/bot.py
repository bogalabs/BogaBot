"""Ensamblado de la aplicación (composition root).

Acá se construyen las implementaciones concretas y se inyectan en los
servicios y cogs. Es el ÚNICO lugar que sabe qué storage concreto se usa:
para migrar a SQLite/Postgres mañana, cambiás solo la línea de `self.storage`.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from bogabot.core.command_tree import BogaCommandTree
from bogabot.core.discord_log_handler import DiscordLogHandler
from bogabot.modules.help.cog import HelpCog
from bogabot.modules.lol.cog import LolCog
from bogabot.modules.lol.ingest import IngestService
from bogabot.modules.lol.ranking import RankingService
from bogabot.modules.lol.riot_key import RiotKeyCog, RiotKeyStore
from bogabot.modules.lol.scheduler import LolScheduler
from bogabot.modules.points.cog import PointsCog
from bogabot.modules.points.store import PointsStore
from bogabot.modules.sounds.cog import SoundsCog
from bogabot.riot.client import RiotClient
from bogabot.scoring.engine import ScoringEngine
from bogabot.scoring.schema import load_scoring_config
from bogabot.settings import Settings
from bogabot.storage.discord_channel import DiscordChannelStorage

log = logging.getLogger(__name__)

# Tamaño máximo de cada bloque de log posteado (2000 es el límite de Discord
# por mensaje; dejamos margen para las comillas del bloque de código).
_LOG_CHUNK_SIZE = 1900


class BogaBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        # No necesitamos intents privilegiados: los slash commands funcionan con
        # los intents por defecto y el storage solo lee mensajes del propio bot.
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents, tree_cls=BogaCommandTree)

        self.settings = settings

        # --- Logging hacia Discord (opcional, ver LOG_USER_ID / LOG_CHANNEL_ID) ---
        self.log_handler = DiscordLogHandler(level=settings.log_channel_level)
        self._log_to_discord = settings.log_user_id is not None or settings.log_channel_id is not None
        if self._log_to_discord:
            logging.getLogger("bogabot").addHandler(self.log_handler)

        # --- Servicios de infraestructura (elegí las implementaciones acá) ---
        self.riot = RiotClient(settings)
        # La key vigente puede venir de /riot-key (guardada en RIOT_KEY_FILE)
        # en vez del .env; ver modules/lol/riot_key.py.
        self.riot_key_store = RiotKeyStore(settings.riot_key_file)
        self.riot_key_state = self.riot_key_store.resolve(settings.riot_api_key)
        self.riot.set_api_key(self.riot_key_state.api_key)
        self.scoring = ScoringEngine(load_scoring_config(settings.scoring_config_path))
        self.storage = DiscordChannelStorage(settings)
        self.points = PointsStore(settings.points_file)

        # --- Servicios de dominio (dependen solo de interfaces) ---
        self.ingest = IngestService(self.riot, self.storage, self.storage, settings)
        self.ranking = RankingService(self.storage, self.scoring, settings)

    async def setup_hook(self) -> None:
        await self.riot.start()

        # Cada módulo/feature se registra como un cog. Sumar features futuras
        # (ej. un módulo de IA) es agregar cogs acá, sin tocar lo existente.
        await self.add_cog(LolCog(self))
        await self.add_cog(LolScheduler(self))
        await self.add_cog(RiotKeyCog(self, self.riot_key_store, self.riot_key_state))
        await self.add_cog(SoundsCog(self))
        if self.settings.points_role_id is not None:
            await self.add_cog(PointsCog(self))
        await self.add_cog(HelpCog(self))

        if self._log_to_discord:
            self._flush_log_channel.start()

        # Sincronización de slash commands.
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands sincronizados en el guild %s.", self.settings.guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands sincronizados globalmente (pueden tardar ~1h).")

    async def on_ready(self) -> None:
        log.info("Conectado como %s (id=%s).", self.user, self.user.id if self.user else "?")
        if not self.storage.ready:
            await self.storage.connect(self)

    async def close(self) -> None:
        if self._flush_log_channel.is_running():
            self._flush_log_channel.cancel()
        await self.riot.close()
        await super().close()

    @tasks.loop(seconds=15)
    async def _flush_log_channel(self) -> None:
        """Vacía la cola de `self.log_handler` y la manda por DM a LOG_USER_ID
        (si está seteado) o, si no, al canal LOG_CHANNEL_ID."""
        pending = self.log_handler.drain()
        if not pending:
            return

        destination = await self._log_destination()
        if destination is None:
            return

        text = "\n".join(pending)
        for start in range(0, len(text), _LOG_CHUNK_SIZE):
            chunk = text[start : start + _LOG_CHUNK_SIZE]
            try:
                await destination.send(f"```{chunk}```")
            except discord.HTTPException:
                # No usamos log.exception acá: reentraría a este mismo handler.
                pass

    async def _log_destination(self) -> discord.abc.Messageable | None:
        """DM al usuario de LOG_USER_ID si está seteado; si no, el canal de logs.

        Sin logs acá ante errores: reentrarían a este mismo handler.
        """
        try:
            if self.settings.log_user_id is not None:
                user = self.get_user(self.settings.log_user_id)
                if user is None:
                    user = await self.fetch_user(self.settings.log_user_id)
                return user
            channel = self.get_channel(self.settings.log_channel_id)
            if channel is None:
                channel = await self.fetch_channel(self.settings.log_channel_id)
        except discord.HTTPException:
            return None
        return channel if isinstance(channel, discord.TextChannel) else None

    @_flush_log_channel.before_loop
    async def _before_flush_log_channel(self) -> None:
        await self.wait_until_ready()
