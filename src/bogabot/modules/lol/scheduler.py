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

from bogabot.core.models import MatchParticipant, MatchRecord, MatchSummary
from bogabot.core.timeutils import get_tz, is_week_start_day
from bogabot.riot.mapper import queue_name

# Orden de exposición de los carriles en el cuadro "línea vs línea".
_LANE_ORDER = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")

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
        """Postea un aviso (cuadro con las 10 posiciones) por cada partida
        nueva, etiquetando a los jugadores del grupo que la jugaron."""
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

        match_ids = dict.fromkeys(r.match_id for r in records)  # preserva orden, sin duplicados
        for match_id in match_ids:
            summary = await self.bot.ingest.build_match_summary(match_id)
            if summary is None:
                continue
            await channel.send(embed=self._match_notification_embed(summary))

    @staticmethod
    def _player_line(p: MatchParticipant) -> str:
        who = f"<@{p.discord_id}>" if p.discord_id is not None else p.display_name
        return f"{who} — **{p.champion}** ({p.kills}/{p.deaths}/{p.assists}) 🌾{p.cs}"

    @classmethod
    def _match_notification_embed(cls, summary: MatchSummary) -> discord.Embed:
        linked = [p for p in summary.participants if p.discord_id is not None]
        if linked and all(p.team_id == linked[0].team_id for p in linked):
            title = "🏆 ¡Victoria!" if linked[0].win else "💀 Derrota"
            color = discord.Color.green() if linked[0].win else discord.Color.red()
        else:
            title = "🔀 Partida con integrantes en equipos contrarios"
            color = discord.Color.orange()

        minutes = summary.game_duration_seconds // 60
        embed = discord.Embed(
            title=f"🎮 {title}",
            description=f"_{queue_name(summary.queue_id)} · {minutes} min_",
            color=color,
        )

        team_ids = sorted({p.team_id for p in summary.participants})
        icons = {team_ids[0]: "🔵", **({team_ids[1]: "🔴"} if len(team_ids) > 1 else {})}
        for team_id in team_ids:
            team = [p for p in summary.participants if p.team_id == team_id]
            result = "Ganó" if team and team[0].win else "Perdió"
            name = f"{icons.get(team_id, '⬜')} Equipo {'azul' if team_id == team_ids[0] else 'rojo'} — {result}"
            value = "\n".join(cls._player_line(p) for p in team) or "—"
            embed.add_field(name=name, value=value, inline=True)

        by_position: dict[str, list[MatchParticipant]] = {}
        for p in summary.participants:
            if p.position:
                by_position.setdefault(p.position, []).append(p)
        lane_lines = [
            f"**{pos}:** {cls._player_line(pair[0])}  🆚  {cls._player_line(pair[1])}"
            for pos in _LANE_ORDER
            if len(pair := by_position.get(pos, [])) == 2
        ]
        if lane_lines:
            embed.add_field(name="⚔️ Línea vs línea", value="\n".join(lane_lines), inline=False)

        return embed
