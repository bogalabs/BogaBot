"""Motor de puntaje: convierte una lista de PlayerStats en un ranking.

Algoritmo:
  1. Para cada métrica de la config, se lee su valor en cada jugador.
  2. Se normaliza la columna entre todos los jugadores (zscore/minmax/none),
     así ninguna métrica domina solo por tener números más grandes.
  3. score(jugador) = Σ  peso * valor_normalizado  (con signo invertido si
     higher_is_better == false, ej. muertes).
"""
from __future__ import annotations

import statistics

from bogabot.core.models import PlayerStats, RankingRow
from bogabot.scoring.schema import ScoringConfig


def _normalize(values: list[float], method: str) -> list[float]:
    if not values:
        return []
    if method == "none":
        return list(values)
    if method == "minmax":
        lo, hi = min(values), max(values)
        span = hi - lo
        if span == 0:
            return [0.0 for _ in values]
        return [(v - lo) / span for v in values]
    # zscore (default)
    if len(values) < 2:
        return [0.0 for _ in values]
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values)
    if stdev == 0:
        return [0.0 for _ in values]
    return [(v - mean) / stdev for v in values]


class ScoringEngine:
    def __init__(self, config: ScoringConfig) -> None:
        self._config = config

    @property
    def config(self) -> ScoringConfig:
        return self._config

    def rank(self, players: list[PlayerStats]) -> list[RankingRow]:
        if not players:
            return []

        scores = [0.0] * len(players)
        for metric in self._config.metrics:
            raw = [float(getattr(p, metric.attr)) for p in players]
            normalized = _normalize(raw, self._config.normalization)
            sign = 1.0 if metric.higher_is_better else -1.0
            for i, value in enumerate(normalized):
                scores[i] += metric.weight * sign * value

        ordered = sorted(
            zip(players, scores), key=lambda pair: pair[1], reverse=True
        )
        return [
            RankingRow(
                rank=i + 1,
                discord_id=player.discord_id,
                display_name=player.display_name,
                score=round(score, 2),
                stats=player,
            )
            for i, (player, score) in enumerate(ordered)
        ]
