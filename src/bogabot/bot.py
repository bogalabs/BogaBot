"""Ensamblado de la aplicación (composition root).

Acá se construyen las implementaciones concretas y se inyectan en los
servicios y cogs. Es el ÚNICO lugar que sabe qué storage concreto se usa:
para migrar a SQLite/Postgres mañana, cambiás solo la línea de `self.storage`.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from bogabot.modules.lol.cog import LolCog
from bogabot.modules.lol.ingest import IngestService
from bogabot.modules.lol.ranking import RankingService
from bogabot.modules.lol.scheduler import LolScheduler
from bogabot.riot.client import RiotClient
from bogabot.scoring.engine import ScoringEngine
from bogabot.scoring.schema import load_scoring_config
from bogabot.settings import Settings
from bogabot.storage.discord_channel import DiscordChannelStorage

log = logging.getLogger(__name__)


class BogaBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        # No necesitamos intents privilegiados: los slash commands funcionan con
        # los intents por defecto y el storage solo lee mensajes del propio bot.
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.settings = settings

        # --- Servicios de infraestructura (elegí las implementaciones acá) ---
        self.riot = RiotClient(settings)
        self.scoring = ScoringEngine(load_scoring_config(settings.scoring_config_path))
        self.storage = DiscordChannelStorage(settings)

        # --- Servicios de dominio (dependen solo de interfaces) ---
        self.ingest = IngestService(self.riot, self.storage, self.storage, settings)
        self.ranking = RankingService(self.storage, self.scoring, settings)

    async def setup_hook(self) -> None:
        await self.riot.start()

        # Cada módulo/feature se registra como un cog. Sumar features futuras
        # (ej. un módulo de IA) es agregar cogs acá, sin tocar lo existente.
        await self.add_cog(LolCog(self))
        await self.add_cog(LolScheduler(self))

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
        await self.riot.close()
        await super().close()
