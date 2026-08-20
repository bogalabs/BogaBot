"""Scheduler del módulo LoL (discord.ext.tasks). Corre dos loops:

  daily_job (1×/día, a DAILY_POST_HOUR:DAILY_POST_MINUTE):
    1. Ingiere las partidas nuevas de todos los jugadores.
    2. Postea el ranking diario en el canal de rankings.
    3. Si es lunes (arranque de semana), postea además el recap semanal
       "Trolls y Pros" de la semana que acaba de cerrar.

  notify_job (cada MATCH_POLL_INTERVAL_MINUTES, si hay MATCH_NOTIFY_CHANNEL_ID):
    Ingiere seguido para detectar partidas recién terminadas y avisar "en
    vivo" en el canal de notificaciones, etiquetando a los jugadores del
    grupo que jugaron esa partida.

Ambos loops llaman al mismo IngestService.ingest_all(); el dedup por
(match_id, puuid) hace que correr los dos sea gratis (uno no duplica lo que
ya trajo el otro).

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

from bogabot.core.models import MatchRecord
from bogabot.core.timeutils import get_tz, is_week_start_day

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

log = logging.getLogger(__name__)


class LolScheduler(commands.Cog):
    def __init__(self, bot: "BogaBot") -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        tz = get_tz(self.bot.settings.timezone)
        run_at = dt.time(
            hour=self.bot.settings.daily_post_hour,
            minute=self.bot.settings.daily_post_minute,
            tzinfo=tz,
        )
        self.daily_job.change_interval(time=run_at)
        self.daily_job.start()
        log.info("Scheduler diario configurado para las %02d:%02d (%s).",
                 self.bot.settings.daily_post_hour, self.bot.settings.daily_post_minute,
                 self.bot.settings.timezone)

        if self.bot.settings.match_notify_channel_id is not None:
            self.notify_job.change_interval(minutes=self.bot.settings.match_poll_interval_minutes)
            self.notify_job.start()
            log.info("Chequeo de partidas nuevas cada %d minutos.",
                     self.bot.settings.match_poll_interval_minutes)

    async def cog_unload(self) -> None:
        self.daily_job.cancel()
        if self.notify_job.is_running():
            self.notify_job.cancel()

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
        new_records = await self.bot.ingest.ingest_all()
        log.info("Ingesta: %d partidas nuevas.", len(new_records))
        await self._notify_new_matches(new_records)

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

    @tasks.loop(minutes=5)  # el intervalo real se fija en cog_load con change_interval
    async def notify_job(self) -> None:
        if not self.bot.storage.ready:
            return
        try:
            new_records = await self.bot.ingest.ingest_all()
            await self._notify_new_matches(new_records)
        except Exception:  # noqa: BLE001 - que un fallo no mate el loop
            log.exception("Error en el chequeo periódico de partidas nuevas.")

    @notify_job.before_loop
    async def _before_notify(self) -> None:
        await self.bot.wait_until_ready()

    async def _notify_new_matches(self, records: list[MatchRecord]) -> None:
        """Postea un aviso por cada partida nueva, etiquetando a los
        jugadores del grupo que la jugaron (uno solo si jugaron separados,
        o el resultado de cada uno si terminaron en equipos contrarios)."""
        if not records:
            return
        channel_id = self.bot.settings.match_notify_channel_id
        if channel_id is None:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.error("MATCH_NOTIFY_CHANNEL_ID no apunta a un canal de texto; no puedo avisar.")
            return

        by_match: dict[str, list[MatchRecord]] = {}
        for record in records:
            by_match.setdefault(record.match_id, []).append(record)

        for match_records in by_match.values():
            await channel.send(self._match_notification_text(match_records))

    @staticmethod
    def _match_notification_text(records: list[MatchRecord]) -> str:
        lines = [
            f"<@{r.discord_id}> {'🏆 ganó' if r.win else '💀 perdió'} con "
            f"**{r.champion}** ({r.kills}/{r.deaths}/{r.assists})"
            for r in records
        ]
        return "🎮 **Partida terminada**\n" + "\n".join(lines)
