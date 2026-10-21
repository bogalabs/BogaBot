"""Apuestas sobre lo que pasa en la partida ("props"), lógica pura.

Se abren junto con la predicción de ganar/perder y se resuelven con los
`MatchRecord` de la partida (ya evaluados por los detectores de trolls y
carreadas: ver `GameFacts`). Mercados:

  - carry:         ¿quién carrea? (un jugador del grupo o "nadie").
  - troll:<id>:    ¿fulano trollea? (llega al nivel de aviso troll).
  - deaths:<id>:   ¿fulano muere más de N veces?
  - duration:      ¿la partida dura más de N minutos?
  - fb:            ¿el grupo se lleva la primera sangre? (solo si están todos
                   del mismo lado).

Para que no sea plata fácil perdiendo a propósito, los que juegan tienen
opciones prohibidas (`Market.forbidden`): nadie apuesta a que él mismo
trollea o muere mucho, ni a "nadie carrea", ni a la duración (se puede
forzar con un FF). Si falta el dato para resolver un mercado (registro
viejo, sin timeline), ese mercado se cancela y se devuelve lo apostado.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from bogabot.modules.predictions.market import Market

ARAM_QUEUE_ID = 450
ARENA_QUEUE_ID = 1700
YES_NO = {"yes": "Sí", "no": "No"}

# Líneas de over/under: en ARAM se muere más y se termina antes.
DEATHS_LINE, DEATHS_LINE_ARAM = 7, 10
DURATION_MINUTES, DURATION_MINUTES_ARAM = 30, 20


@dataclass
class GameFacts:
    """Lo que pasó en la partida, por jugador del grupo (discord_id)."""

    duration_minutes: float | None = None
    deaths: dict[int, int] = field(default_factory=dict)
    trolled: dict[int, bool] = field(default_factory=dict)
    # Puntos de carreada de los que llegaron al nivel de aviso (los demás no están).
    carry_points: dict[int, int] = field(default_factory=dict)
    group_first_blood: bool | None = None


def _yes_no(key: str, title: str, yes: str, no: str, forbidden: dict[int, list[str]]) -> Market:
    return Market(key=key, title=title, options={"yes": yes, "no": no}, forbidden=forbidden)


def build_props(players: dict[int, int], names: dict[int, str], queue_id: int) -> dict[str, Market]:
    """Mercados para una partida. `players`: discord_id -> team_id del grupo."""
    def name(uid: int) -> str:
        return names.get(uid) or f"Jugador {uid}"

    markets: list[Market] = []
    carry_options = {str(uid): name(uid) for uid in players}
    carry_options["none"] = "Nadie"
    markets.append(Market(key="carry", title="⭐ ¿Quién carrea?", options=carry_options,
                          forbidden={uid: ["none"] for uid in players}))
    for uid in players:
        markets.append(_yes_no(f"troll:{uid}", f"🤡 ¿{name(uid)} trollea?", "🤡 Sí", "😇 No",
                               {uid: ["yes"]}))
    if queue_id != ARENA_QUEUE_ID:
        line = DEATHS_LINE_ARAM if queue_id == ARAM_QUEUE_ID else DEATHS_LINE
        for uid in players:
            markets.append(Market(key=f"deaths:{uid}", title=f"💀 ¿{name(uid)} muere más de {line}?",
                                  options={"over": f"Más de {line}", "under": f"{line} o menos"},
                                  forbidden={uid: ["over"]}))
        minutes = DURATION_MINUTES_ARAM if queue_id == ARAM_QUEUE_ID else DURATION_MINUTES
        markets.append(Market(key="duration", title=f"⏱️ ¿Dura más de {minutes} min?",
                              options={"over": f"Más de {minutes}", "under": f"{minutes} o menos"},
                              forbidden={uid: ["over", "under"] for uid in players}))
        if len(set(players.values())) == 1:
            markets.append(_yes_no("fb", "🩸 ¿El grupo se lleva la primera sangre?", "🩸 Sí", "🙈 No",
                                   {uid: ["no"] for uid in players}))
    return {m.key: m for m in markets}


def _line(market: Market) -> int:
    """La línea del over/under sale de la etiqueta ("Más de 7")."""
    return int(market.options["over"].rsplit(" ", 1)[-1])


def resolve_prop(market: Market, facts: GameFacts) -> str | None:
    """La opción ganadora de un mercado, o None si falta el dato (se devuelve)."""
    kind, _, target = market.key.partition(":")
    uid = int(target) if target else None
    if kind == "carry":
        if not facts.trolled:  # sin registros del grupo no se sabe
            return None
        if not facts.carry_points:
            return "none"
        best = max(facts.carry_points.items(), key=lambda kv: (kv[1], -facts.deaths.get(kv[0], 0)))[0]
        return str(best) if str(best) in market.options else "none"
    if kind == "troll":
        if uid not in facts.trolled:
            return None
        return "yes" if facts.trolled[uid] else "no"
    if kind == "deaths":
        if uid not in facts.deaths:
            return None
        return "over" if facts.deaths[uid] > _line(market) else "under"
    if kind == "duration":
        if facts.duration_minutes is None:
            return None
        return "over" if facts.duration_minutes > _line(market) else "under"
    if kind == "fb":
        if facts.group_first_blood is None:
            return None
        return "yes" if facts.group_first_blood else "no"
    return None
