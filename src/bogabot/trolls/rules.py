"""Catálogo de reglas del detector de trolls.

Cada regla mira UN `MatchRecord` (las stats de un jugador en una partida) y,
si el jugador "cometió" ese papelón, devuelve los puntos troll y un detalle
legible. Los umbrales y puntos de acá son los DEFAULTS: el grupo los ajusta
en config/trolls.yaml sin tocar código (ver `trolls/schema.py`).

Para sumar una regla nueva: escribí la función `_check_*`, agregala a
`RULES` con sus params por defecto y (opcional) listala en trolls.yaml.

Perfiles de juego: los umbrales dependen del modo. En ARAM se muere mucho
más que en la Grieta, y en URF/One for All/etc. todavía más, así que un
param puede tener variante por perfil con sufijo: `min_deaths_aram` pisa a
`min_deaths` cuando la partida es ARAM. Las reglas de línea, visión y farm
solo aplican a la Grieta (rift).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from bogabot.core.models import MatchRecord
from bogabot.riot.mapper import SWIFTPLAY_QUEUE_ID

RIFT = "rift"  # Grieta del Invocador (normales, ranked, clash, swiftplay)
ARAM = "aram"
ARENA = "arena"
CHAOS = "chaos"  # URF, One for All, Nexus Blitz, Spellbook y modos rotativos

_PROFILE_BY_MODE = {"CLASSIC": RIFT, "SWIFTPLAY": RIFT, "ARAM": ARAM, "CHERRY": ARENA}
# Modos sin rivales humanos de verdad: no se juzga a nadie.
_IGNORED_MODES = {"TUTORIAL", "PRACTICETOOL", "STRAWBERRY"}

_ALL_PVP = frozenset({RIFT, ARAM, CHAOS})
_RIFT_ONLY = frozenset({RIFT})

_POSITION_NAMES = {
    "TOP": "top",
    "JUNGLE": "jungla",
    "MIDDLE": "mid",
    "BOTTOM": "ADC",
    "UTILITY": "support",
}


def game_profile(record: MatchRecord) -> str | None:
    """Perfil de juego de la partida, o None si no se juzga (tutorial, etc)."""
    mode = (record.game_mode or "").upper()
    if mode in _IGNORED_MODES:
        return None
    return _PROFILE_BY_MODE.get(mode, CHAOS)


class Params:
    """Umbrales efectivos de una regla para un perfil de juego. `p["x"]`
    devuelve `x_<perfil>` si existe, si no `x`."""

    def __init__(self, values: dict[str, float], profile: str) -> None:
        self._values = values
        self._profile = profile

    def __getitem__(self, name: str) -> float:
        return self._values.get(f"{name}_{self._profile}", self._values[name])

    def int(self, name: str) -> int:
        return int(self[name])


# (puntos, detalle) si la regla se cumple; None si no.
Hit = tuple[int, str] | None


@dataclass(frozen=True)
class RuleSpec:
    code: str
    emoji: str
    title: str
    description: str  # para /trolls-reglas
    profiles: frozenset[str]
    params: dict[str, float]  # defaults; siempre incluye "points"
    check: Callable[[MatchRecord, Params], Hit]
    # Reglas que esta reemplaza si se cumplen las dos (castigan lo mismo y
    # sumarlas inflaría los puntos), ej. feeder ya cubre el KDA trágico.
    supersedes: frozenset[str] = frozenset()


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _mmss(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _position(record: MatchRecord) -> str:
    return _POSITION_NAMES.get(record.position, record.position.lower())


# --- Reglas ------------------------------------------------------------------
def _check_feeder(r: MatchRecord, p: Params) -> Hit:
    min_deaths = p["min_deaths"]
    if r.deaths < min_deaths or r.kda >= p["max_kda"]:
        return None
    extra = min(p.int("max_extra"), int((r.deaths - min_deaths) // max(p["extra_every"], 1)))
    return p.int("points") + extra, f"murió {r.deaths} veces ({r.kills}/{r.deaths}/{r.assists})"


def _check_tragic_kda(r: MatchRecord, p: Params) -> Hit:
    if r.deaths < p["min_deaths"] or r.kda >= p["max_kda"]:
        return None
    return p.int("points"), f"KDA {r.kda:.2f}"


def _check_ghost(r: MatchRecord, p: Params) -> Hit:
    if r.kills + r.assists > 0 or r.minutes < p["min_minutes"]:
        return None
    if r.team_kills is not None and r.team_kills < p["min_team_kills"]:
        return None  # partida sin peleas: no es culpa suya
    return p.int("points"), f"0 kills y 0 asistencias en {int(r.minutes)} minutos"


def _check_pacifist(r: MatchRecord, p: Params) -> Hit:
    if r.kills > 0 or r.assists == 0 or r.position == "UTILITY" or r.minutes < p["min_minutes"]:
        return None
    where = f" jugando {_position(r)}" if r.position else ""
    return p.int("points"), f"ni una kill en {int(r.minutes)} minutos{where}"


def _check_first_blood(r: MatchRecord, p: Params) -> Hit:
    if not r.gave_first_blood:
        return None
    points = p.int("points")
    minute = r.first_death_minute
    if minute is not None and minute < p["early_minute"]:
        points += p.int("early_bonus")
    when = f" al minuto {minute}" if minute is not None else ""
    return points, f"la entregó{when}"


def _check_early_deaths(r: MatchRecord, p: Params) -> Hit:
    if r.deaths_before_10 is None or r.deaths_before_10 < p["min_deaths"]:
        return None
    return p.int("points"), f"{r.deaths_before_10} muertes antes del minuto 10"


def _check_lane_gap(r: MatchRecord, p: Params) -> Hit:
    if r.gold_diff_15 is None or not r.opponent_champion:
        return None
    threshold = p["min_deficit_support"] if r.position == "UTILITY" else p["min_deficit"]
    if -r.gold_diff_15 < threshold:
        return None
    deficit = f"{-r.gold_diff_15:,}".replace(",", ".")
    return p.int("points"), f"{deficit} de oro abajo vs {r.opponent_champion} al minuto 15"


def _check_lane_delivery(r: MatchRecord, p: Params) -> Hit:
    if r.deaths_to_lane_opponent is None or r.deaths_to_lane_opponent < p["min_deaths"]:
        return None
    rival = r.opponent_champion or "su rival de línea"
    return p.int("points"), f"{rival} lo mató {r.deaths_to_lane_opponent} veces"


def _check_low_damage(r: MatchRecord, p: Params) -> Hit:
    share = r.damage_share
    if share is None or r.position == "UTILITY" or r.minutes < p["min_minutes"]:
        return None
    if share >= p["max_share"]:
        return None
    return p.int("points"), f"{_pct(share)} del daño de su equipo"


def _check_low_kp(r: MatchRecord, p: Params) -> Hit:
    kp = r.kill_participation
    if kp is None or r.kills + r.assists == 0 or (r.team_kills or 0) < p["min_team_kills"]:
        return None
    if kp >= p["max_kp"]:
        return None
    return p.int("points"), f"participó en el {_pct(kp)} de las kills del equipo"


def _check_blind(r: MatchRecord, p: Params) -> Hit:
    if r.minutes < p["min_minutes"]:
        return None
    threshold = p["min_per_min_support"] if r.position == "UTILITY" else p["min_per_min"]
    if r.vision_score / r.minutes >= threshold:
        return None
    return p.int("points"), f"{r.vision_score} de visión en {int(r.minutes)} minutos"


def _check_no_control_wards(r: MatchRecord, p: Params) -> Hit:
    if r.control_wards_bought is None or r.control_wards_bought > 0 or r.minutes < p["min_minutes"]:
        return None
    return p.int("points"), f"0 control wards en {int(r.minutes)} minutos"


def _check_farm_allergy(r: MatchRecord, p: Params) -> Hit:
    if r.position not in ("TOP", "MIDDLE", "BOTTOM", "JUNGLE") or r.minutes < p["min_minutes"]:
        return None
    threshold = p["min_cs_per_min_jungle"] if r.position == "JUNGLE" else p["min_cs_per_min"]
    cs_per_min = r.cs / r.minutes
    if cs_per_min >= threshold:
        return None
    return p.int("points"), f"{cs_per_min:.1f} de farm por minuto jugando {_position(r)}"


def _check_tombstone(r: MatchRecord, p: Params) -> Hit:
    if r.time_dead_seconds is None or not r.game_duration_seconds:
        return None
    share = r.time_dead_seconds / r.game_duration_seconds
    if share < p["min_share"]:
        return None
    points = p.int("points") + (p.int("severe_bonus") if share >= p["severe_share"] else 0)
    return points, f"{_pct(share)} de la partida muerto ({_mmss(r.time_dead_seconds)})"


def _check_team_anchor(r: MatchRecord, p: Params) -> Hit:
    if r.team_deaths is None or r.deaths < p["min_deaths"]:
        return None
    rest = r.team_deaths - r.deaths
    if r.deaths <= rest:
        return None
    return p.int("points"), f"{r.deaths} muertes vs {rest} de los otros 4 juntos"


def _check_executed(r: MatchRecord, p: Params) -> Hit:
    if r.executed_deaths is None or r.executed_deaths < p["min_deaths"]:
        return None
    return p.int("points"), f"{r.executed_deaths} muertes sin que lo mate un campeón"


def _check_item_seller(r: MatchRecord, p: Params) -> Hit:
    if r.items_sold is None or r.items_sold < p["min_items"]:
        return None
    return p.int("points"), f"vendió {r.items_sold} items"


def _check_early_ff(r: MatchRecord, p: Params) -> Hit:
    if r.win or not r.game_ended_in_surrender or (r.game_mode or "").upper() != "CLASSIC":
        return None
    if r.queue_id == SWIFTPLAY_QUEUE_ID:
        return None
    if r.minutes >= p["max_minutes"]:
        return None
    return p.int("points"), f"se rindieron al minuto {int(r.minutes)}"


def _check_stomped(r: MatchRecord, p: Params) -> Hit:
    if r.win or r.team_kills is None or r.enemy_kills is None:
        return None
    if r.enemy_kills - r.team_kills < p["min_gap"]:
        return None
    return p.int("points"), f"perdieron {r.team_kills} a {r.enemy_kills} en kills"


def _check_pinger(r: MatchRecord, p: Params) -> Hit:
    if r.question_pings is None or r.question_pings < p["min_pings"]:
        return None
    return p.int("points"), f"{r.question_pings} pings de '?'"


def _check_arena_last(r: MatchRecord, p: Params) -> Hit:
    if r.placement is None or r.placement < p["min_placement"]:
        return None
    return p.int("points"), f"salió {r.placement}º"


RULES: dict[str, RuleSpec] = {
    spec.code: spec
    for spec in (
        RuleSpec(
            "feeder", "🍽️", "Feeder profesional",
            "Muchas muertes y un KDA menor a 1. Suma un punto extra cada 2 muertes de más.",
            _ALL_PVP,
            {"points": 3, "min_deaths": 10, "min_deaths_aram": 14, "min_deaths_chaos": 16,
             "max_kda": 1.0, "extra_every": 2, "max_extra": 3},
            _check_feeder,
            supersedes=frozenset({"tragic_kda"}),
        ),
        RuleSpec(
            "tragic_kda", "📉", "KDA de la vergüenza",
            "KDA menor a 0.5 con varias muertes encima (si ya es Feeder, no se suma).",
            _ALL_PVP,
            {"points": 2, "max_kda": 0.5, "min_deaths": 5, "min_deaths_aram": 8, "min_deaths_chaos": 8},
            _check_tragic_kda,
        ),
        RuleSpec(
            "ghost", "👻", "¿Estaba AFK?",
            "Ni una kill ni una asistencia en toda la partida.",
            _ALL_PVP,
            {"points": 5, "min_minutes": 12, "min_team_kills": 5},
            _check_ghost,
        ),
        RuleSpec(
            "pacifist", "🕊️", "Pacifista",
            "Cero kills en una partida larga (no aplica a supports).",
            _RIFT_ONLY,
            {"points": 1, "min_minutes": 20},
            _check_pacifist,
        ),
        RuleSpec(
            "first_blood", "🩸", "Regaló la primera sangre",
            "Fue la primera muerte de la partida (+1 si fue antes del minuto 3).",
            _RIFT_ONLY,
            {"points": 1, "early_minute": 3, "early_bonus": 1},
            _check_first_blood,
        ),
        RuleSpec(
            "early_deaths", "⏰", "Speedrun de muertes",
            "Varias muertes antes del minuto 10.",
            _RIFT_ONLY,
            {"points": 2, "min_deaths": 3},
            _check_early_deaths,
        ),
        RuleSpec(
            "lane_gap", "🚜", "Le pasaron el trapo en línea",
            "Muy abajo en oro contra su rival de línea al minuto 15.",
            _RIFT_ONLY,
            {"points": 2, "min_deficit": 2500, "min_deficit_support": 1500},
            _check_lane_gap,
        ),
        RuleSpec(
            "lane_delivery", "🎁", "Delivery a domicilio",
            "Su rival de línea lo mató una y otra vez.",
            _RIFT_ONLY,
            {"points": 2, "min_deaths": 4},
            _check_lane_delivery,
        ),
        RuleSpec(
            "low_damage", "🪶", "Daño de cotillón",
            "Muy poco daño a campeones comparado con su equipo (no aplica a supports).",
            _ALL_PVP,
            {"points": 2, "max_share": 0.10, "min_minutes": 15},
            _check_low_damage,
        ),
        RuleSpec(
            "low_kp", "🏝️", "Jugando otra partida",
            "Casi no participó de las kills de su equipo.",
            frozenset({RIFT, ARAM}),
            {"points": 2, "max_kp": 0.20, "max_kp_aram": 0.30, "min_team_kills": 10},
            _check_low_kp,
        ),
        RuleSpec(
            "blind", "🙈", "Ciego voluntario",
            "Visión casi nula para lo que duró la partida (a los supports se les exige más).",
            _RIFT_ONLY,
            {"points": 1, "min_per_min": 0.35, "min_per_min_support": 1.0, "min_minutes": 20},
            _check_blind,
        ),
        RuleSpec(
            "no_control_wards", "🧿", "Ni un control ward",
            "No compró ni un control ward en una partida larga.",
            _RIFT_ONLY,
            {"points": 1, "min_minutes": 25},
            _check_no_control_wards,
        ),
        RuleSpec(
            "farm_allergy", "🌾", "Alérgico al farm",
            "Farm por minuto muy bajo para su rol (no aplica a supports).",
            _RIFT_ONLY,
            {"points": 1, "min_cs_per_min": 4.0, "min_cs_per_min_jungle": 3.5, "min_minutes": 15},
            _check_farm_allergy,
        ),
        RuleSpec(
            "tombstone", "⚰️", "Veraneando en la fuente",
            "Pasó una buena parte de la partida muerto (+1 si fue más de un tercio).",
            _ALL_PVP,
            {"points": 2, "min_share": 0.25, "severe_share": 0.35, "severe_bonus": 1},
            _check_tombstone,
        ),
        RuleSpec(
            "team_anchor", "⚓", "Ancla del equipo",
            "Murió más que los otros 4 de su equipo juntos.",
            _ALL_PVP,
            {"points": 2, "min_deaths": 7},
            _check_team_anchor,
        ),
        RuleSpec(
            "executed", "🤖", "Ejecutado",
            "Murió varias veces sin que lo mate un campeón (torre, minions o monstruos).",
            _RIFT_ONLY,
            {"points": 1, "min_deaths": 2},
            _check_executed,
        ),
        RuleSpec(
            "item_seller", "💸", "Liquidación total",
            "Vendió una banda de items: señal clásica de inteo.",
            _ALL_PVP,
            {"points": 4, "min_items": 6},
            _check_item_seller,
        ),
        RuleSpec(
            "early_ff", "🏳️", "FF al 15",
            "Perdieron por rendición antes del minuto 20.",
            _RIFT_ONLY,
            {"points": 2, "max_minutes": 20},
            _check_early_ff,
        ),
        RuleSpec(
            "stomped", "🧹", "Barrida histórica",
            "Perdieron con una diferencia de kills enorme.",
            _ALL_PVP,
            {"points": 1, "min_gap": 20, "min_gap_aram": 25, "min_gap_chaos": 30},
            _check_stomped,
        ),
        RuleSpec(
            "pinger", "❓", "Tóxico del '?'",
            "Una catarata de pings de '?' a sus compañeros.",
            _ALL_PVP,
            {"points": 1, "min_pings": 15},
            _check_pinger,
        ),
        RuleSpec(
            "arena_last", "🥄", "Cuchara de madera",
            "Último puesto en Arena.",
            frozenset({ARENA}),
            {"points": 2, "min_placement": 8},
            _check_arena_last,
        ),
    )
}
