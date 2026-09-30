"""Scheduler del módulo LoL (discord.ext.tasks). Corre dos loops:

  daily_job (1×/día, a DAILY_POST_HOUR:DAILY_POST_MINUTE):
    1. Ingiere las partidas nuevas de todos los jugadores.
    2. Postea el ranking diario en el canal de rankings.
    3. Si es lunes (arranque de semana), postea además el recap semanal
       "Trolls y Pros" de la semana que acaba de cerrar y la corona del
       "Troll de la semana".
    4. Si hubo trolleadas hoy, postea cómo va el ranking troll de la semana.

  notify_job (cada MATCH_POLL_INTERVAL_MINUTES):
    Ingiere seguido para detectar partidas recién terminadas.

  backfill_job (al arrancar, y se apaga solo cuando no queda nada):
    Recalcula en silencio las partidas guardadas con un análisis viejo del
    timeline (ver `TIMELINE_VERSION`): sin mensajes ni logs visibles y sin
    avisos; el único efecto es que la tabla troll queda al día.

Los avisos por partida NO dependen de qué loop ingirió: el scheduler se
registra como listener de `IngestService` (`_on_new_matches`), así que
cualquier ingesta (los dos loops o /ingest-now) dispara, una sola vez por
partida:
  - el aviso "en vivo" con el cuadro de los 10 jugadores y el troll-o-metro
    (si hay MATCH_NOTIFY_CHANNEL_ID), y
  - por cada trolleada, la anécdota + el detalle en el canal de trolls; si
    es papelón, la anécdota va además a GENERAL_CHANNEL_ID.

El dedup por (match_id, discord_id) de la ingesta y su lock hacen que correr
los dos loops sea gratis (uno no duplica lo que ya trajo el otro).

El scheduler solo ORQUESTA: dispara los servicios de ingesta, ranking y
trolls. Si mañana querés otra estrategia (cada X horas, cron externo), se
cambia acá sin tocar la lógica de negocio.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from bogabot.core.models import MatchParticipant, MatchRecord, MatchSummary, TrollLevel, TrollVerdict
from bogabot.core.timeutils import get_tz, is_week_start_day
from bogabot.riot.mapper import queue_name

# Orden de exposición de los carriles en el cuadro "línea vs línea".
_LANE_ORDER = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
# Cuántos match_id avisados se recuerdan para no repetir el aviso si un
# segundo jugador de la misma partida aparece en una corrida posterior.
_NOTIFIED_MEMORY = 500
# El recálculo silencioso se rinde tras estas vueltas seguidas sin avanzar.
_BACKFILL_MAX_IDLE_RUNS = 3
# Las alertas etiquetan al jugador, pero nunca a roles ni @everyone.
_USER_MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

log = logging.getLogger(__name__)


class LolScheduler(commands.Cog):
    def __init__(self, bot: "BogaBot") -> None:
        self.bot = bot
        self._notified: dict[str, None] = {}  # match_ids ya avisados (orden de inserción)
        self._backfill_idle_runs = 0  # vueltas seguidas del recálculo sin avanzar nada

    async def cog_load(self) -> None:
        self.bot.ingest.add_listener(self._on_new_matches)

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

        # Corre siempre (no solo con MATCH_NOTIFY_CHANNEL_ID): las alertas troll
        # tienen que salir al terminar la partida, no recién a la noche.
        self.notify_job.change_interval(minutes=self.bot.settings.match_poll_interval_minutes)
        self.notify_job.start()
        self.backfill_job.start()
        log.info("Chequeo de partidas nuevas cada %d minutos.",
                 self.bot.settings.match_poll_interval_minutes)

    async def cog_unload(self) -> None:
        self.bot.ingest.remove_listener(self._on_new_matches)
        self.daily_job.cancel()
        if self.notify_job.is_running():
            self.notify_job.cancel()
        if self.backfill_job.is_running():
            self.backfill_job.cancel()

    # --- Canales -----------------------------------------------------------
    async def _text_channel(self, channel_id: int | None, env_name: str) -> discord.TextChannel | None:
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                log.error("No puedo acceder al canal %s=%s (%s).", env_name, channel_id, exc)
                return None
        if not isinstance(channel, discord.TextChannel):
            log.error("%s no apunta a un canal de texto; no puedo postear.", env_name)
            return None
        return channel

    async def _troll_channel(self) -> discord.TextChannel | None:
        s = self.bot.settings
        if s.troll_channel_id is not None:
            return await self._text_channel(s.troll_channel_id, "TROLL_CHANNEL_ID")
        return await self._text_channel(s.ranking_channel_id, "RANKING_CHANNEL_ID")

    # --- Job diario --------------------------------------------------------
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
        # Los avisos de partida y las alertas troll salen solos por el listener.
        new_matches = await self.bot.ingest.ingest_all()
        log.info("Ingesta: %d partidas nuevas.", len(new_matches))

        channel = await self._text_channel(self.bot.settings.ranking_channel_id, "RANKING_CHANNEL_ID")
        if channel is None:
            return

        daily_rows = await self.bot.ranking.daily_rows()
        await channel.send(embed=self.bot.ranking.build_ranking_embed(daily_rows, "🏆 Ranking de hoy"))

        is_monday = is_week_start_day(self.bot.settings.timezone)
        if is_monday:
            prev_rows = await self.bot.ranking.previous_week_rows()
            await channel.send(embed=self.bot.ranking.build_trolls_and_pros_embed(prev_rows))

        try:
            await self._post_troll_summaries(is_monday)
        except Exception:  # noqa: BLE001 - el resto de la corrida ya salió
            log.exception("Error posteando los resúmenes troll.")

    async def _post_troll_summaries(self, is_monday: bool) -> None:
        """Ranking troll de la semana si hubo trolleadas hoy y, los lunes, la
        corona del "Troll de la semana" que cerró."""
        channel = await self._troll_channel()
        if channel is None:
            return
        trolls = self.bot.trolls
        if await trolls.had_trolls_today():
            rows = await trolls.standings("week")
            await channel.send(embed=trolls.build_standings_embed(rows, "week"))
        if is_monday:
            content, embed = trolls.build_weekly_recap(await trolls.standings("prev_week"))
            await channel.send(content, embed=embed, allowed_mentions=_USER_MENTIONS)

    @daily_job.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # --- Poll de partidas nuevas -------------------------------------------
    @tasks.loop(minutes=5)  # el intervalo real se fija en cog_load con change_interval
    async def notify_job(self) -> None:
        if not self.bot.storage.ready:
            return
        try:
            await self.bot.ingest.ingest_all()  # los avisos salen por el listener
        except Exception:  # noqa: BLE001 - que un fallo no mate el loop
            log.exception("Error en el chequeo periódico de partidas nuevas.")

    @notify_job.before_loop
    async def _before_notify(self) -> None:
        await self.bot.wait_until_ready()

    # --- Recálculo silencioso de partidas viejas --------------------------
    @tasks.loop(minutes=10)
    async def backfill_job(self) -> None:
        """Completa las partidas guardadas con datos o análisis viejos. Corre
        en silencio (logs solo a nivel DEBUG, nada a Discord) y se detiene
        cuando no queda nada pendiente. Reintenta cada 10 min lo que falle
        (ej. Riot caído); si varias vueltas seguidas no avanza (partidas que
        Riot ya no tiene), se rinde hasta el próximo reinicio."""
        if not self.bot.storage.ready:
            return  # todavía hidratando: probamos en la próxima vuelta
        try:
            if not await self.bot.ingest.pending_enrichment():
                self.backfill_job.cancel()
                return
            updated, _, failed = await self.bot.ingest.enrich_stored_matches(quiet=True)
            self._backfill_idle_runs = 0 if updated else self._backfill_idle_runs + 1
            if not failed or self._backfill_idle_runs >= _BACKFILL_MAX_IDLE_RUNS:
                self.backfill_job.cancel()
        except Exception:  # noqa: BLE001 - silencioso: se reintenta en la próxima vuelta
            log.debug("Falló el recálculo silencioso; reintento más tarde.", exc_info=True)

    @backfill_job.before_loop
    async def _before_backfill(self) -> None:
        await self.bot.wait_until_ready()

    # --- Avisos por partida (listener de IngestService) --------------------
    async def _on_new_matches(self, records: list[MatchRecord]) -> None:
        verdicts = {r.dedup_key: self.bot.trolls.evaluate(r) for r in records}
        # Cada aviso aislado: si falla el de partida, las alertas troll salen igual.
        try:
            await self._notify_new_matches(records, verdicts)
        except Exception:  # noqa: BLE001
            log.exception("Error avisando las partidas nuevas.")
        try:
            await self._announce_trolls(records, verdicts)
        except Exception:  # noqa: BLE001
            log.exception("Error mandando las alertas troll.")

    async def _announce_trolls(self, records: list[MatchRecord], verdicts: dict[str, TrollVerdict]) -> None:
        """Por cada jugador que pasó el umbral troll en una partida reciente:
        la línea anecdótica (con la mención) y el detalle en el canal de
        trolls. Si es papelón, la línea va además a #general (y en el canal
        de trolls queda solo el detalle, para no etiquetar dos veces)."""
        trolls = self.bot.trolls
        flagged = [
            verdicts[r.dedup_key]
            for r in sorted(records, key=lambda r: r.game_end)
            if verdicts[r.dedup_key].level >= TrollLevel.TROLL
        ]
        fresh = []
        for v in flagged:
            if trolls.is_fresh(v.record):
                fresh.append(v)
            else:
                log.info("Partida troll vieja (%s, %d pts); suma al ranking pero no la aviso.",
                         v.record.dedup_key, v.points)
        if not fresh:
            return

        week = await trolls.standings("week")
        by_player = {s.discord_id: s for s in week}
        troll_channel = await self._troll_channel()
        general_channel = None
        if any(v.level >= TrollLevel.PAPELON for v in fresh):
            general_channel = await self._text_channel(self.bot.settings.general_channel_id, "GENERAL_CHANNEL_ID")
        for v in fresh:
            line = trolls.build_general_line(v)
            _, embed = trolls.build_alert(v, by_player.get(v.record.discord_id), len(week))
            papelon = v.level >= TrollLevel.PAPELON
            # Solo el papelón sale en #general (una línea corta con la anécdota).
            in_general = papelon and await self._safe_send(general_channel, line)
            # El canal de trolls siempre lleva el detalle; la mención solo si no
            # salió ya en #general (para no etiquetar dos veces).
            await self._safe_send(troll_channel, None if in_general else line, embed=embed)
            log.info("Trolleada %s (%d pts) de %s avisada%s.", v.level.name, v.points,
                     v.record.dedup_key, " también en #general" if in_general else "")

    async def _safe_send(self, channel: discord.TextChannel | None, content: str | None,
                         embed: discord.Embed | None = None) -> bool:
        """Manda sin romper el resto de los avisos si Discord falla."""
        if channel is None:
            return False
        try:
            await channel.send(content, embed=embed, allowed_mentions=_USER_MENTIONS)
            return True
        except discord.HTTPException:
            log.exception("No pude mandar un aviso troll a #%s.", channel.name)
            return False

    async def _notify_new_matches(self, records: list[MatchRecord], verdicts: dict[str, TrollVerdict]) -> None:
        """Postea un aviso (cuadro con las 10 posiciones + troll-o-metro) por
        cada partida nueva, etiquetando a los jugadores del grupo."""
        if not records or self.bot.settings.match_notify_channel_id is None:
            return
        channel = await self._text_channel(self.bot.settings.match_notify_channel_id, "MATCH_NOTIFY_CHANNEL_ID")
        if channel is None:
            return

        by_match: dict[str, list[TrollVerdict]] = {}  # preserva orden de llegada
        for r in records:
            by_match.setdefault(r.match_id, []).append(verdicts[r.dedup_key])
        for match_id, match_verdicts in by_match.items():
            if match_id in self._notified:
                continue
            summary = await self.bot.ingest.build_match_summary(match_id)
            if summary is None:
                continue
            meter = self.bot.trolls.meter_lines(match_verdicts)
            await channel.send(embed=self._match_notification_embed(summary, meter))
            self._notified[match_id] = None
            while len(self._notified) > _NOTIFIED_MEMORY:
                self._notified.pop(next(iter(self._notified)))

    @staticmethod
    def _player_line(p: MatchParticipant) -> str:
        who = f"<@{p.discord_id}>" if p.discord_id is not None else p.display_name
        return f"{who} — **{p.champion}** ({p.kills}/{p.deaths}/{p.assists}) 🌾{p.cs}"

    @classmethod
    def _match_notification_embed(cls, summary: MatchSummary, troll_meter: str | None = None) -> discord.Embed:
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

        if troll_meter:
            embed.add_field(name="🤡 Troll-o-metro", value=troll_meter, inline=False)

        return embed
