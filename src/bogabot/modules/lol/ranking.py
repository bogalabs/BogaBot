"""Servicio de ranking: agrega partidas -> PlayerStats -> puntaje -> embed.

No sabe de dónde salen las partidas (habla con MatchRepository) ni cómo se
calcula el puntaje (habla con ScoringEngine). Solo orquesta y arma los embeds.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

import discord

from bogabot.core.models import PlayerStats, RankingRow
from bogabot.core.timeutils import now, start_of_day, start_of_week
from bogabot.scoring.engine import ScoringEngine
from bogabot.settings import Settings
from bogabot.storage.base import MatchRepository

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


class RankingService:
    def __init__(
        self,
        matches: MatchRepository,
        scoring: ScoringEngine,
        settings: Settings,
    ) -> None:
        self._matches = matches
        self._scoring = scoring
        self._settings = settings

    async def _rows_for_window(self, since: datetime, until: datetime) -> list[RankingRow]:
        records = await self._matches.get_matches(since, until)
        by_player: dict[int, PlayerStats] = {}
        for m in records:
            stats = by_player.get(m.discord_id)
            if stats is None:
                stats = PlayerStats(discord_id=m.discord_id, display_name=m.game_name)
                by_player[m.discord_id] = stats
            stats.add(m)
        return self._scoring.rank(list(by_player.values()))

    async def daily_rows(self) -> list[RankingRow]:
        tz = self._settings.timezone
        return await self._rows_for_window(start_of_day(tz), now(tz))

    async def weekly_rows(self) -> list[RankingRow]:
        tz = self._settings.timezone
        return await self._rows_for_window(start_of_week(tz), now(tz))

    async def previous_week_rows(self) -> list[RankingRow]:
        """Ranking de la semana que acaba de cerrar (para el recap del lunes)."""
        tz = self._settings.timezone
        this_week_start = start_of_week(tz)
        prev_week_start = this_week_start - timedelta(days=7)
        return await self._rows_for_window(prev_week_start, this_week_start)

    # --- Embeds ------------------------------------------------------------
    def build_ranking_embed(self, rows: list[RankingRow], title: str) -> discord.Embed:
        embed = discord.Embed(title=title, color=discord.Color.gold())
        embed.set_footer(text="Solo cuentan las partidas jugadas con al menos otro vinculado del grupo.")
        if not rows:
            embed.description = "Todavía no hay partidas registradas en esta ventana. 🦗"
            return embed

        for row in rows:
            s = row.stats
            fav_champ = Counter(s.champions).most_common(1)[0][0] if s.champions else "?"
            medal = MEDALS.get(row.rank, f"#{row.rank}")
            value = (
                f"**Puntaje:** {row.score}\n"
                f"{s.wins}V / {s.losses}D  ·  KDA {s.kda:.2f} "
                f"({s.avg_kills:.1f}/{s.avg_deaths:.1f}/{s.avg_assists:.1f})\n"
                f"Daño/min {s.damage_per_min:.0f}  ·  Visión {s.avg_vision:.0f}  "
                f"·  {s.games} partidas  ·  🏆 {fav_champ}"
            )
            embed.add_field(name=f"{medal} {row.display_name}", value=value, inline=False)
        return embed

    def build_trolls_and_pros_embed(self, rows: list[RankingRow]) -> discord.Embed:
        embed = discord.Embed(
            title="📊 Tabla de Trolls y Pros de la semana",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Solo cuentan las partidas jugadas con al menos otro vinculado del grupo.")
        if not rows:
            embed.description = "No hubo partidas esta semana. Se salvaron de la vergüenza. 😌"
            return embed

        top = rows[: min(3, len(rows))]
        pros = "\n".join(
            f"{MEDALS.get(r.rank, f'#{r.rank}')} **{r.display_name}** — {r.score} pts "
            f"({r.stats.wins}V/{r.stats.losses}D, KDA {r.stats.kda:.2f})"
            for r in top
        )
        embed.add_field(name="😎 PROS", value=pros, inline=False)

        # Trolls: los últimos del ranking (si hay suficientes jugadores distintos).
        trolls_pool = [r for r in rows if r not in top]
        if trolls_pool:
            bottom = list(reversed(trolls_pool[-min(3, len(trolls_pool)):]))
            trolls = "\n".join(
                f"💩 **{r.display_name}** — {r.score} pts "
                f"({r.stats.wins}V/{r.stats.losses}D, KDA {r.stats.kda:.2f})"
                for r in bottom
            )
            embed.add_field(name="🤡 TROLLS", value=trolls, inline=False)
        return embed
