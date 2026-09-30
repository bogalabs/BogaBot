"""Motor del detector de trolls: corre las reglas del catálogo sobre un
`MatchRecord` y arma el veredicto (cargos, puntos y nivel).

Es lógica pura: no habla con Riot ni con Discord. Como todo sale del
`MatchRecord` guardado, cambiar un umbral en config/trolls.yaml recalcula
también el historial (el ranking troll se computa al vuelo).
"""
from __future__ import annotations

import logging

from bogabot.core.models import MatchRecord, TrollFlag, TrollLevel, TrollVerdict
from bogabot.riot.mapper import RANKED_QUEUE_IDS
from bogabot.trolls.rules import RULES, Params, RuleSpec, game_profile
from bogabot.trolls.schema import RuleConfig, TrollConfig

log = logging.getLogger(__name__)


class TrollDetector:
    def __init__(self, config: TrollConfig) -> None:
        self._config = config

    @property
    def config(self) -> TrollConfig:
        return self._config

    def rules_overview(self) -> list[tuple[RuleSpec, RuleConfig]]:
        """Reglas activas con sus parámetros efectivos (para /trolls-reglas)."""
        return [
            (spec, self._config.rules[code])
            for code, spec in RULES.items()
            if self._config.rules[code].enabled
        ]

    def level_for(self, points: int) -> TrollLevel:
        if points >= self._config.papelon_level:
            return TrollLevel.PAPELON
        if points >= self._config.troll_level:
            return TrollLevel.TROLL
        return TrollLevel.NONE

    def evaluate(self, record: MatchRecord) -> TrollVerdict:
        profile = game_profile(record)
        flags: list[TrollFlag] = []
        if profile is not None:
            for code, spec in RULES.items():
                rule = self._config.rules[code]
                if not rule.enabled or profile not in spec.profiles:
                    continue
                try:
                    hit = spec.check(record, Params(rule.params, profile))
                except (TypeError, ValueError, ZeroDivisionError):
                    # Un dato raro no debe tirar abajo el resto de las reglas.
                    log.warning("La regla troll '%s' falló con la partida %s.", code, record.match_id,
                                exc_info=True)
                    continue
                if hit is not None:
                    points, detail = hit
                    flags.append(TrollFlag(code, spec.emoji, spec.title, detail, points))
        superseded = {code for f in flags for code in RULES[f.code].supersedes}
        flags = [f for f in flags if f.code not in superseded]
        # Los cargos de equipo (FF, barrida) solo agravan un cargo propio.
        if all(RULES[f.code].aggravating for f in flags):
            flags = []
        flags.sort(key=lambda f: f.points, reverse=True)

        # Los cargos menores (mal rendimiento) suman con tope: para ser
        # trolleada hace falta una señal fuerte (feedear, AFK, vender items,
        # dejar caer la base, throw...), no una partida floja.
        strong = sum(f.points for f in flags if not RULES[f.code].weak)
        weak = sum(f.points for f in flags if RULES[f.code].weak)
        capped = max(0, weak - int(self._config.weak_points_cap))
        base = strong + weak - capped
        points = float(base)
        ranked = base > 0 and record.queue_id in RANKED_QUEUE_IDS
        if ranked:
            points *= self._config.ranked_multiplier
        carried = base > 0 and record.win
        if carried:
            points *= self._config.win_multiplier
        total = int(points + 0.5)  # redondeo "de escuela" (round() redondea al par)

        return TrollVerdict(
            record=record,
            flags=flags,
            base_points=base,
            points=total,
            level=self.level_for(total),
            ranked_bonus=ranked,
            carried=carried,
            capped_points=capped,
        )
