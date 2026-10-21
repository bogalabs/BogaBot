"""Catálogo de misiones semanales (lógica pura).

Cada semana (lunes 00:00 hora local) salen MISSIONS_PER_WEEK misiones del
catálogo, elegidas al azar pero con la semana como semilla: todos ven las
mismas y un reinicio no las cambia. El progreso lo reportan los módulos que
ven cada cosa (`MissionsService.progress(uids, code, cantidad)`).
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Mission:
    code: str
    emoji: str
    text: str  # con {n}: la meta en la unidad que se muestra
    goal: int  # meta en la unidad que se cuenta
    unit: int = 1  # cuántas unidades contadas son una mostrada (ej. 60 min = 1 h)

    def describe(self) -> str:
        return f"{self.emoji} {self.text.format(n=self.goal // self.unit)}"

    def progress_text(self, value: int) -> str:
        return f"{min(value, self.goal) // self.unit}/{self.goal // self.unit}"


CATALOG: dict[str, Mission] = {m.code: m for m in (
    Mission("games", "🎮", "Jugá {n} partidas con el grupo", 3),
    Mission("wins", "🏆", "Ganá {n} partidas con el grupo", 2),
    Mission("carry", "⭐", "Carreá {n} partida", 1),
    Mission("clean", "😇", "Jugá {n} partidas sin trollear", 3),
    Mission("voice", "🎙️", "Pasá {n} h en voz con el grupo", 5 * 60, unit=60),  # se cuenta en minutos
    Mission("bets", "🎲", "Apostá {n} veces (predicciones, duelos, recompensas)", 3),
    Mission("daily", "📅", "Reclamá el /daily {n} días", 5),
)}


def pick_week(week: str, count: int) -> list[Mission]:
    """Las misiones de la semana `week` (ISO del lunes). Deterministas."""
    codes = sorted(CATALOG)
    count = max(1, min(count, len(codes)))
    return [CATALOG[c] for c in random.Random(f"misiones-{week}").sample(codes, count)]
