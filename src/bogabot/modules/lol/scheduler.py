"""Scheduler del módulo LoL (discord.ext.tasks).

Corre una vez al día a la hora local configurada (DAILY_POST_HOUR):
  1. Ingiere las partidas nuevas de todos los jugadores.
  2. Postea el ranking diario en el canal de rankings.
  3. Si es domingo (arranque de semana), postea además el recap semanal
     "Trolls y Pros" de la semana que acaba de cerrar.

El scheduler solo ORQUESTA: dispara los servicios de ingesta y ranking. Si
mañana querés otra estrategia (cada X horas, cron externo), se cambia acá sin
tocar la lógica de negocio.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from bogabot.core.timeutils import get_tz, is_week_start_day

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

log = logging.getLogger(__name__)


class LolScheduler(commands.Cog):
    def __init__(self, bot: "BogaBot") -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        tz = get_tz(self.bot.settings.timezone)
        run_at = dt.time(hour=self.bot.settings.daily_post_hour, minute=0, tzinfo=tz)
        self.daily_job.change_interval(time=run_at)
        self.daily_job.start()
        log.info("Scheduler diario configurado para las %02d:00 (%s).",
                 self.bot.settings.daily_post_hour, self.bot.settings.timezone)

    async def cog_unload(self) -> None:
        self.daily_job.cancel()

    @tasks.loop(hours=24)  # el horario real se fija en cog_load con change_interval
    async def daily_job(self) -> None:
        if not self.bot.storage.ready:
            log.warning("Storage no listo; salteo la corrida diaria.")
            return
        try:
            await self._run()
        except Exception:  # noqa: BLE001 - que un fallo no mate el loop
            log.exception("Error en la corrida diaria del scheduler.")

    async def _run(self) -> None:
        log.info("=== Corrida diaria: ingesta + posteo ===")
        new_count = await self.bot.ingest.ingest_all()
        log.info("Ingesta: %d partidas nuevas.", new_count)

        channel = self.bot.get_channel(self.bot.settings.ranking_channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(self.bot.settings.ranking_channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.error("RANKING_CHANNEL_ID no apunta a un canal de texto; no puedo postear.")
            return

        daily_rows = await self.bot.ranking.daily_rows()
        await channel.send(embed=self.bot.ranking.build_ranking_embed(daily_rows, "🏆 Ranking de hoy"))

        if is_week_start_day(self.bot.settings.timezone):
            prev_rows = await self.bot.ranking.previous_week_rows()
            await channel.send(embed=self.bot.ranking.build_trolls_and_pros_embed(prev_rows))

    @daily_job.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()
