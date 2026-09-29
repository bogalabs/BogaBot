"""Servicio de trolls: juzga partidas con el `TrollDetector`, arma el ranking
troll de un período y los embeds (alerta, papelón histórico, ranking,
reglamento y análisis de una partida).

No sabe de canales: a dónde va cada aviso lo decide el scheduler (ver
`LolScheduler._announce_trolls`). Como el detector trabaja sobre los
`MatchRecord` guardados, el ranking se calcula al vuelo y refleja siempre
los umbrales vigentes de config/trolls.yaml.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import discord

from bogabot.core.models import MatchRecord, TrollLevel, TrollStanding, TrollVerdict
from bogabot.core.timeutils import now, start_of_day, start_of_month, start_of_week
from bogabot.riot.mapper import queue_name
from bogabot.settings import Settings
from bogabot.storage.base import MatchRepository
from bogabot.trolls.detector import TrollDetector
from bogabot.trolls.rules import RULES
from bogabot.trolls.schema import TrollConfig

# Períodos de /trolls: clave -> cómo se nombra en los títulos.
PERIODS: dict[str, str] = {
    "week": "de la semana",
    "prev_week": "de la semana pasada",
    "month": "del mes",
    "all": "histórico",
}

_PODIUM = {1: "👑", 2: "🥈", 3: "🥉"}
_FIELD_LIMIT = 1024  # máximo de caracteres de un field de embed

# --- Textos (se elige uno por partida, estable: la misma partida da siempre
# el mismo texto, ver `_rng`) ---------------------------------------------------
_TROLL_INTROS = (
    "🚨 Atención, tribunal: {mention} tiene cargos pendientes.",
    "🚨 {mention}, te estamos mirando.",
    "🤡 Se abrió una causa contra {mention}.",
    "📣 Llegó una denuncia contra {mention}. Riot no hace nada, nosotros sí.",
    "👀 {mention}, ¿querés explicar lo que acaba de pasar?",
)
_TROLL_TITLES = (
    "🚨 ¡ALERTA TROLL! 🚨",
    "🤡 Se escapó alguien del circo",
    "🚔 Operativo anti-troll en curso",
    "🧯 Incendio en la Grieta",
    "📉 Partida para el olvido",
)
_PAPELON_INTROS = (
    "💀 {mention} acaba de firmar un **PAPELÓN HISTÓRICO**. Que quede en actas.",
    "📢 Frenen todo: {mention} hizo un papelón de los que se cuentan en los asados.",
    "🪦 Un minuto de silencio por la dignidad de {mention}.",
    "🗞️ ÚLTIMO MOMENTO: {mention} protagonizó un papelón que ya es noticia nacional.",
    "🎖️ {mention} se ganó un lugar en el salón de la fama troll.",
)
_PAPELON_TITLES = (
    "💀 PAPELÓN HISTÓRICO 💀",
    "🪦 Acá yace la dignidad de {name}",
    "🗞️ Esto sale en todos los diarios",
    "🥇 Medalla de oro en papelones",
    "☢️ Zona de desastre",
)
_TAGLINES = (
    "y la comunidad exige explicaciones.",
    "y Riot ya está revisando el replay.",
    "y el equipo todavía lo está procesando.",
    "y nadie en la call entiende qué pasó.",
    "y el rival le mandó un 'gg ez' merecido.",
)
_ROASTS = (
    "Veredicto: culpable. Sin derecho a réplica.",
    "Se aceptan descargos en el canal de voz.",
    "El /mute all es gratis, lo sabés, ¿no?",
    "Esto no es personal. Bueno, un poco sí.",
    "La próxima probá contra bots, de a poco.",
    "Tu mouse pidió cambio de dueño.",
    "El reporte ya salió. Mentira. Ojalá.",
    "Ni el Wi-Fi de tu casa te puede defender de esto.",
)


def _rng(record: MatchRecord) -> random.Random:
    return random.Random(record.dedup_key)


def _names_list(names: list[str], limit: int = _FIELD_LIMIT) -> str:
    text = ", ".join(names)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TrollService:
    def __init__(self, matches: MatchRepository, detector: TrollDetector, settings: Settings) -> None:
        self._matches = matches
        self._detector = detector
        self._settings = settings

    @property
    def config(self) -> TrollConfig:
        return self._detector.config

    # --- Juicio ---------------------------------------------------------------
    def evaluate(self, record: MatchRecord) -> TrollVerdict:
        return self._detector.evaluate(record)

    def is_fresh(self, record: MatchRecord, ref: datetime | None = None) -> bool:
        """True si la partida terminó hace poco (vale la pena avisarla). Las
        viejas (bot caído un rato, alguien que se vinculó hoy) solo suman."""
        ref = ref or datetime.now(timezone.utc)
        return ref - record.game_end <= timedelta(hours=self.config.alert_max_age_hours)

    # --- Rankings -----------------------------------------------------------
    def _aggregate(self, records: list[MatchRecord]) -> list[TrollStanding]:
        by_player: dict[int, TrollStanding] = {}
        for r in sorted(records, key=lambda r: r.game_creation):
            v = self._detector.evaluate(r)
            s = by_player.get(r.discord_id)
            if s is None:
                s = TrollStanding(rank=0, discord_id=r.discord_id, display_name=r.game_name)
                by_player[r.discord_id] = s
            s.display_name = r.game_name or s.display_name
            s.games += 1
            s.points += v.points
            s.index_points += min(v.points, self.config.index_max_game_points)
            s.troll_games += 1 if v.level >= TrollLevel.TROLL else 0
            s.papelones += 1 if v.level >= TrollLevel.PAPELON else 0
            for f in v.flags:
                s.flag_counts[f.code] = s.flag_counts.get(f.code, 0) + 1
            if v.points > 0 and (s.worst is None or v.points > s.worst.points):
                s.worst = v
        # Índice = promedio suavizado (bayesiano): se suman `prior_games`
        # partidas "fantasma" que valen el promedio del grupo. Con pocas
        # partidas el índice queda cerca del grupo; con más, pesa lo propio.
        # Así no importa cuántas jugaste, pero una sola partida mala tampoco
        # te corona por azar.
        total_games = sum(s.games for s in by_player.values())
        group_mean = sum(s.index_points for s in by_player.values()) / total_games if total_games else 0.0
        prior = self.config.index_prior_games
        for s in by_player.values():
            s.index = (s.index_points + prior * group_mean) / (s.games + prior)
        rows = sorted(
            by_player.values(),
            # Por índice, no por total: la cantidad de partidas no debe pesar.
            # Los limpios (0 pts) siempre al fondo, aunque el suavizado les dé
            # un índice > 0. Desempatan papelones, trolleadas y promedio crudo.
            key=lambda s: (s.points == 0, -round(s.index, 6), -s.papelones, -s.troll_games,
                           -s.average, s.display_name.lower()),
        )
        for i, s in enumerate(rows, 1):
            s.rank = i
        return rows

    def _windows(self, period: str) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
        """(ventana del período, ventana anterior para la tendencia)."""
        tz = self._settings.timezone
        if period == "prev_week":
            until = start_of_week(tz)
            since = until - timedelta(days=7)
            return (since, until), (since - timedelta(days=7), since)
        if period == "month":
            since = start_of_month(tz)
            return (since, now(tz)), (start_of_month(tz, since - timedelta(days=1)), since)
        since = start_of_week(tz)
        return (since, now(tz)), (since - timedelta(days=7), since)

    async def standings(self, period: str) -> list[TrollStanding]:
        """Ranking troll de un período de `PERIODS`, con el índice del
        período anterior de cada jugador para mostrar la tendencia."""
        if period == "all":
            return self._aggregate(await self._matches.get_all_matches())
        (since, until), (prev_since, prev_until) = self._windows(period)
        rows = self._aggregate(await self._matches.get_matches(since, until))
        previous = {s.discord_id: s.index
                    for s in self._aggregate(await self._matches.get_matches(prev_since, prev_until))}
        for s in rows:
            s.previous_index = previous.get(s.discord_id)
        return rows

    def tier(self, index: float) -> str:
        """Categoría según el índice (relativa al umbral de alerta troll)."""
        troll = self.config.troll_level
        for limit, label in ((troll / 12, "😇 Santo"), (troll / 4, "🙂 Tranqui"),
                             (troll / 2, "😬 Sospechoso"), (troll, "🤡 Troll")):
            if index < limit:
                return label
        return "💀 Leyenda troll"

    @staticmethod
    def trend(s: TrollStanding) -> str:
        if s.previous_index is None:
            return "🆕"
        diff = s.index - s.previous_index
        if abs(diff) < 0.5:
            return "➡️"
        return f"{'📈' if diff > 0 else '📉'} {diff:+.1f}"

    async def had_trolls_today(self) -> bool:
        tz = self._settings.timezone
        rows = self._aggregate(await self._matches.get_matches(start_of_day(tz), now(tz)))
        return any(s.troll_games for s in rows)

    async def find_record(self, discord_id: int, match_id: str | None = None) -> MatchRecord | None:
        """La partida `match_id` del jugador (acepta "LA2_123" o solo "123"),
        o la última que jugó si no se pasa."""
        records = [r for r in await self._matches.get_all_matches() if r.discord_id == discord_id]
        if match_id:
            wanted = match_id.strip().upper()
            return next(
                (r for r in records if r.match_id.upper() == wanted or r.match_id.upper().endswith(f"_{wanted}")),
                None,
            )
        return max(records, key=lambda r: r.game_creation, default=None)

    # --- Piezas de texto ---------------------------------------------------
    def meter(self, points: int) -> str:
        """Barrita de 10 casilleros; se llena al llegar al nivel papelón."""
        filled = min(10, max(1, round(points / self.config.papelon_level * 10))) if points > 0 else 0
        return "🟥" * filled + "⬛" * (10 - filled)

    @staticmethod
    def _flags_text(verdict: TrollVerdict) -> str:
        if verdict.is_clean:
            return "Ninguno. Partida limpia. 😇"
        lines: list[str] = []
        for i, f in enumerate(verdict.flags):
            line = f"{f.emoji} **{f.title}** — {f.detail} `+{f.points}`"
            rest = len(verdict.flags) - i
            if len("\n".join([*lines, line])) > _FIELD_LIMIT - 30:
                lines.append(f"…y {rest} cargo{'s' if rest > 1 else ''} más.")
                break
            lines.append(line)
        return "\n".join(lines)

    def _meter_text(self, verdict: TrollVerdict) -> str:
        c = self.config
        lines = [f"{self.meter(verdict.points)} **{verdict.points} pts**"]
        if verdict.ranked_bonus:
            lines.append(f"×{c.ranked_multiplier:g} por ser ranked 🏆")
        if verdict.carried:
            lines.append(f"×{c.win_multiplier:g} porque igual ganaron: lo llevaron de mochila 🎒")
        return "\n".join(lines)

    @staticmethod
    def _score_line(r: MatchRecord) -> str:
        result = "Victoria" if r.win else "Derrota"
        if r.placement:
            result = f"Puesto {r.placement}"
        return (f"`{r.kills}/{r.deaths}/{r.assists}` · {result} · {queue_name(r.queue_id)} · "
                f"{int(r.minutes)} min")

    def meter_lines(self, verdicts: list[TrollVerdict]) -> str:
        """Una línea por jugador del grupo, para el aviso de partida terminada."""
        lines = []
        for v in sorted(verdicts, key=lambda v: -v.points):
            who = f"<@{v.record.discord_id}>"
            if v.is_clean:
                lines.append(f"{who} — 😇 limpio")
                continue
            emojis = "".join(f.emoji for f in v.flags[:6])
            tag = {TrollLevel.PAPELON: " 💀", TrollLevel.TROLL: " 🚨"}.get(v.level, "")
            lines.append(f"{who} — **{v.points} pts** {emojis}{tag}")
        return "\n".join(lines)[:_FIELD_LIMIT]

    # --- Embeds ------------------------------------------------------------
    def build_alert(self, verdict: TrollVerdict, standing: TrollStanding | None,
                    players: int) -> tuple[str, discord.Embed]:
        """Mensaje (texto con la mención + embed) de alerta troll o papelón."""
        r = verdict.record
        rng = _rng(r)
        mention = f"<@{r.discord_id}>"
        if verdict.level >= TrollLevel.PAPELON:
            content = rng.choice(_PAPELON_INTROS).format(mention=mention)
            title = rng.choice(_PAPELON_TITLES).format(name=r.game_name)
            color = discord.Color.dark_red()
        else:
            content = rng.choice(_TROLL_INTROS).format(mention=mention)
            title = rng.choice(_TROLL_TITLES)
            color = discord.Color.orange()

        vs = f" vs {r.opponent_champion}" if r.opponent_champion else ""
        embed = discord.Embed(
            title=title,
            description=f"**{r.game_name}** jugó **{r.champion}**{vs} {rng.choice(_TAGLINES)}\n{self._score_line(r)}",
            color=color,
            timestamp=r.game_end,
        )
        embed.add_field(name="📋 Cargos", value=self._flags_text(verdict), inline=False)
        embed.add_field(name="🤡 Troll-o-metro", value=self._meter_text(verdict), inline=False)
        if standing is not None:
            crown = " 👑" if standing.rank == 1 else ""
            embed.add_field(
                name="📆 En la semana",
                value=(f"Índice troll **{standing.index:.1f}** {self.tier(standing.index)} "
                       f"({standing.points} pts en {standing.games} partidas) · "
                       f"puesto **#{standing.rank}** de {players}{crown}\n"
                       f"🚨 {standing.troll_games} trolleadas · 💀 {standing.papelones} papelones"),
                inline=False,
            )
        embed.set_footer(text=rng.choice(_ROASTS))
        return content, embed

    def build_standings_embed(self, rows: list[TrollStanding], period: str) -> discord.Embed:
        embed = discord.Embed(title=f"🤡 Ranking troll {PERIODS.get(period, '')}".strip(),
                              color=discord.Color.orange())
        embed.set_footer(text=("Índice troll = puntos por partida, suavizado hacia el promedio del grupo: "
                               "no importa cuántas jugaste, pero 1 partida suelta no alcanza. "
                               "Tendencia vs. el período anterior. Mirá /trolls-reglas."))
        guilty = [s for s in rows if s.points > 0]
        clean = [s for s in rows if s.points == 0]
        if not rows:
            embed.description = "No hay partidas registradas en este período. Sospechoso. 🤔"
            return embed
        if not guilty:
            embed.description = "Nadie trolleó. Todos santos. 😇 (Por ahora.)"
        else:
            blocks: list[str] = []
            for s in guilty[:10]:
                medal = _PODIUM.get(s.rank, f"`#{s.rank}`")
                trend = f" · {self.trend(s)}" if period != "all" else ""
                lines = [f"{medal} **{s.display_name}** — índice **{s.index:.1f}** {self.tier(s.index)}{trend}",
                         f"{s.points} pts en {s.games} partida{'s' if s.games > 1 else ''} "
                         f"(promedio {s.average:.1f}, {s.troll_rate * 100:.0f}% trolleadas)"]
                counts = []
                if s.troll_games:
                    counts.append(f"🚨 {s.troll_games} trolleada{'s' if s.troll_games > 1 else ''}")
                if s.papelones:
                    counts.append(f"💀 {s.papelones} {'papelones' if s.papelones > 1 else 'papelón'}")
                if s.flag_counts:
                    code, times = max(s.flag_counts.items(), key=lambda kv: kv[1])
                    spec = RULES.get(code)
                    if spec is not None:
                        counts.append(f"especialidad {spec.emoji} {spec.title} (×{times})")
                if counts:
                    lines.append(" · ".join(counts))
                if s.worst is not None:
                    w = s.worst.record
                    lines.append(f"Peor: `{w.kills}/{w.deaths}/{w.assists}` con {w.champion} ({s.worst.points} pts)")
                blocks.append("\n".join(lines))
            embed.description = "\n\n".join(blocks)
        if clean:
            embed.add_field(name="😇 Limpios", value=_names_list([s.display_name for s in clean]), inline=False)
        return embed

    def build_weekly_recap(self, rows: list[TrollStanding]) -> tuple[str | None, discord.Embed]:
        """Recap del lunes: ranking troll de la semana que cerró + corona."""
        embed = self.build_standings_embed(rows, "prev_week")
        top = next((s for s in rows if s.points > 0), None)
        if top is None:
            return None, embed
        return (f"👑 <@{top.discord_id}> es el **Troll de la semana** con un índice de "
                f"{top.index:.1f} ({self.tier(top.index)}). "
                f"Aplausos. 👏"), embed

    def build_rules_embed(self) -> discord.Embed:
        c = self.config
        general = f"<#{self._settings.general_channel_id}>"
        header = (
            "Después de cada partida se juzga a cada jugador del grupo con estas reglas "
            "y se suman los puntos de los cargos:\n"
            f"• **{c.troll_level}+ pts** → 🚨 alerta troll\n"
            f"• **{c.papelon_level}+ pts** → 💀 papelón histórico en {general}\n"
            f"• En ranked ×{c.ranked_multiplier:g} · si igual ganaron ×{c.win_multiplier:g}\n"
            "El ranking troll (`/trolls`) ordena por **índice**: puntos por partida, así que "
            "jugar mucho no te hunde ni te salva. Para que 1 partida suelta no decida, cada uno "
            f"arranca con {c.index_prior_games:g} partidas con el promedio del grupo y una partida "
            f"cuenta como mucho {c.index_max_game_points:g} pts.\n\n"
        )
        lines = [
            f"{spec.emoji} **{spec.title}** (+{int(cfg.params['points'])}) — {spec.description}"
            for spec, cfg in self._detector.rules_overview()
        ]
        embed = discord.Embed(title="📜 Reglamento troll", color=discord.Color.orange())
        embed.description = (header + "\n".join(lines))[:4096]
        embed.set_footer(text="Los umbrales se ajustan en config/trolls.yaml.")
        return embed

    def build_analysis_embed(self, verdict: TrollVerdict) -> discord.Embed:
        """Detalle de por qué una partida tiene (o no) cargos."""
        r = verdict.record
        c = self.config
        vs = f" vs {r.opponent_champion}" if r.opponent_champion else ""
        color = {
            TrollLevel.PAPELON: discord.Color.dark_red(),
            TrollLevel.TROLL: discord.Color.orange(),
        }.get(verdict.level, discord.Color.green() if verdict.is_clean else discord.Color.gold())
        embed = discord.Embed(
            title=f"🔍 Análisis troll: {r.game_name}",
            description=f"**{r.champion}**{vs}\n{self._score_line(r)}",
            color=color,
            timestamp=r.game_end,
        )
        embed.add_field(name="📋 Cargos", value=self._flags_text(verdict), inline=False)
        level = {
            TrollLevel.PAPELON: "💀 Papelón histórico (va a #general)",
            TrollLevel.TROLL: "🚨 Alerta troll",
        }.get(verdict.level, f"Debajo del umbral de alerta ({c.troll_level} pts)")
        embed.add_field(name="🤡 Troll-o-metro", value=f"{self._meter_text(verdict)}\n{level}", inline=False)
        embed.add_field(name="📊 Números", value=self._stats_text(r), inline=False)
        if not r.has_timeline:
            embed.add_field(
                name="ℹ️ Faltan datos",
                value=("Esta partida no tiene el timeline (se guardó antes del detector nuevo o Riot no lo dio), "
                       "así que no se evaluaron las reglas de línea, primera sangre ni items vendidos. "
                       "Un dev puede completarla con `/trolls-recalcular`."),
                inline=False,
            )
        embed.set_footer(text=r.match_id)
        return embed

    @staticmethod
    def _stats_text(r: MatchRecord) -> str:
        parts = [f"KDA {r.kda:.2f}"]
        if r.kill_participation is not None:
            parts.append(f"KP {r.kill_participation * 100:.0f}%")
        if r.damage_share is not None:
            parts.append(f"daño {r.damage_share * 100:.0f}% del equipo")
        if r.minutes:
            parts.append(f"visión {r.vision_score / r.minutes:.2f}/min")
            parts.append(f"farm {r.cs / r.minutes:.1f}/min")
        if r.time_dead_seconds is not None and r.game_duration_seconds:
            parts.append(f"muerto {r.time_dead_seconds / r.game_duration_seconds * 100:.0f}% de la partida")
        if r.deaths_before_10 is not None:
            parts.append(f"{r.deaths_before_10} muertes antes del 10")
        if r.gold_diff_15 is not None:
            parts.append(f"oro vs rival al 15: {r.gold_diff_15:+,}".replace(",", "."))
        if r.items_sold is not None:
            parts.append(f"{r.items_sold} items vendidos")
        if r.question_pings is not None:
            parts.append(f"{r.question_pings} pings de '?'")
        return " · ".join(parts)[:_FIELD_LIMIT]
