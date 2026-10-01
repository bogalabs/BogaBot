"""Catálogo de reglas de carreadas: lo mismo que `trolls/rules.py`, pero
premiando en vez de castigar. Corre con el mismo motor (`TrollDetector`) y
se configura en config/carries.yaml.

Criterio: una carreada es **impacto relativo a tu equipo** en una partida
que importó, no un KDA lindo en una partida fácil. Por eso:
  - Las métricas se miden contra el equipo (% del daño, % de las kills,
    participación) o contra la duración (kills cada 10 min), no en crudo:
    en un stomp todos tienen KDA alto, pero uno solo hace el 40% del daño.
  - Las "jugadas menores" (KDA limpio, KP alta, ganar la línea, visión,
    farm, primera sangre) suman entre todas como mucho `weak_points_cap`:
    ganar una partida fácil no es carreada; hace falta una jugada fuerte.
  - Perder pesa la mitad (`loss_multiplier`): carrear y perder igual puede
    pasar, pero tiene que ser muy grosero para llegar al aviso.
  - Los supports y los tanques carrean distinto: tienen sus propias reglas
    (habilitador, muralla) en vez de pedirles daño o kills.

Los `detail` están escritos como frases para la anécdota ("hizo el 41% del
daño del equipo"), igual que en trolls.
"""
from __future__ import annotations

from bogabot.core.models import MatchRecord
from bogabot.trolls.rules import ARAM, ARENA, CHAOS, RIFT, Hit, Params, RuleSpec
from bogabot.trolls.schema import Catalog

_ALL_PVP = frozenset({RIFT, ARAM, CHAOS})
_RIFT_ONLY = frozenset({RIFT})
_RIFT_ARAM = frozenset({RIFT, ARAM})


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _thousands(value: int) -> str:
    return f"{value:,}".replace(",", ".")


# --- Jugadas fuertes ----------------------------------------------------------
def _check_damage_carry(r: MatchRecord, p: Params) -> Hit:
    share = r.damage_share
    if share is None or share < p["min_share"] or r.minutes < p["min_minutes"]:
        return None
    points = p.int("points") + (p.int("huge_bonus") if share >= p["huge_share"] else 0)
    return points, f"hizo el {_pct(share)} del daño del equipo"


def _check_slayer(r: MatchRecord, p: Params) -> Hit:
    # Según la duración (como el feeder) y relativo al equipo: en un stomp
    # cualquiera mete kills, pero no el 30% de las del equipo.
    threshold = max(p["min_kills"], p["kills_per_10"] * r.minutes / 10)
    if r.kills < threshold or not r.team_kills:
        return None
    share = r.kills / r.team_kills
    if share < p["min_kill_share"]:
        return None
    return p.int("points"), f"metió {r.kills} kills (el {_pct(share)} de las del equipo)"


def _check_pentakill(r: MatchRecord, p: Params) -> Hit:
    if not r.penta_kills:
        return None
    many = f"{r.penta_kills} pentakills" if r.penta_kills > 1 else "una pentakill"
    return p.int("points") + p.int("extra_each") * (r.penta_kills - 1), f"hizo {many}"


def _check_quadrakill(r: MatchRecord, p: Params) -> Hit:
    if not r.quadra_kills:
        return None
    many = f"{r.quadra_kills} quadrakills" if r.quadra_kills > 1 else "una quadrakill"
    return p.int("points"), f"hizo {many}"


def _check_legendary(r: MatchRecord, p: Params) -> Hit:
    if r.largest_killing_spree is None or r.largest_killing_spree < p["min_spree"]:
        return None
    return p.int("points"), f"llegó a una racha de {r.largest_killing_spree} kills sin morir"


def _check_immortal(r: MatchRecord, p: Params) -> Hit:
    if r.deaths > 0 or r.minutes < p["min_minutes"]:
        return None
    takedowns = r.kills + r.assists
    if takedowns < max(p["min_takedowns"], p["takedowns_per_10"] * r.minutes / 10):
        return None
    kp = r.kill_participation
    if kp is not None and kp < p["min_kp"]:
        return None  # no murió, pero tampoco estuvo en las peleas
    points = p.int("points") + (p.int("high_kp_bonus") if kp is not None and kp >= p["high_kp"] else 0)
    return points, f"no murió nunca ({r.kills}/0/{r.assists})"


def _check_comeback(r: MatchRecord, p: Params) -> Hit:
    if not r.win or r.max_gold_deficit is None or r.max_gold_deficit < p["min_deficit"]:
        return None
    share, kp = r.damage_share, r.kill_participation
    if not ((share is not None and share >= p["min_share"]) or (kp is not None and kp >= p["min_kp"])):
        return None  # remontaron, pero no gracias a él
    return p.int("points"), f"remontaron estando {_thousands(r.max_gold_deficit)} de oro abajo y fue clave"


def _check_steal(r: MatchRecord, p: Params) -> Hit:
    if not r.objective_steals or r.objective_steals < p["min_steals"]:
        return None
    what = "un objetivo épico" if r.objective_steals == 1 else f"{r.objective_steals} objetivos épicos"
    return p.int("points"), f"robó {what} (Barón, dragón o heraldo)"


def _check_wall(r: MatchRecord, p: Params) -> Hit:
    taken, kp = r.damage_taken_share, r.kill_participation
    if not r.win or taken is None or kp is None:
        return None
    if taken < p["min_taken_share"] or kp < p["min_kp"]:
        return None
    return p.int("points"), f"tanqueó el {_pct(taken)} del daño y estuvo en el {_pct(kp)} de las kills"


def _check_enabler(r: MatchRecord, p: Params) -> Hit:
    kp = r.kill_participation
    if not r.win or r.position != "UTILITY" or kp is None or kp < p["min_kp"]:
        return None
    if r.assists < max(p["min_assists"], p["assists_per_10"] * r.minutes / 10):
        return None
    return p.int("points"), f"armó todo desde support: {r.assists} asistencias y el {_pct(kp)} de las kills"


def _check_arena_champion(r: MatchRecord, p: Params) -> Hit:
    if r.placement is None or r.placement > p["max_placement"]:
        return None
    return p.int("points"), "ganó la Arena"


# --- Jugadas menores (con tope) ----------------------------------------------
def _check_kp_carry(r: MatchRecord, p: Params) -> Hit:
    kp = r.kill_participation
    if kp is None or kp < p["min_kp"] or (r.team_kills or 0) < p["min_team_kills"]:
        return None
    return p.int("points"), f"estuvo en el {_pct(kp)} de las kills"


def _check_lane_kingdom(r: MatchRecord, p: Params) -> Hit:
    if r.gold_diff_15 is None or not r.opponent_champion:
        return None
    threshold = p["min_lead_support"] if r.position == "UTILITY" else p["min_lead"]
    if r.gold_diff_15 < threshold:
        return None
    return p.int("points"), f"le sacó {_thousands(r.gold_diff_15)} de oro a {r.opponent_champion} al minuto 15"


def _check_solo_killer(r: MatchRecord, p: Params) -> Hit:
    if r.solo_kills is None or r.solo_kills < p["min_solo_kills"]:
        return None
    return p.int("points"), f"ganó {r.solo_kills} duelos 1 contra 1"


def _check_vision_lord(r: MatchRecord, p: Params) -> Hit:
    if r.minutes < p["min_minutes"]:
        return None
    threshold = p["min_per_min_support"] if r.position == "UTILITY" else p["min_per_min"]
    per_min = r.vision_score / r.minutes
    if per_min < threshold:
        return None
    return p.int("points"), f"tuvo {r.vision_score} de visión ({per_min:.1f} por minuto)"


def _check_farm_machine(r: MatchRecord, p: Params) -> Hit:
    if r.position not in ("TOP", "MIDDLE", "BOTTOM", "JUNGLE") or r.minutes < p["min_minutes"]:
        return None
    threshold = p["min_cs_per_min_jungle"] if r.position == "JUNGLE" else p["min_cs_per_min"]
    cs_per_min = r.cs / r.minutes
    if cs_per_min < threshold:
        return None
    return p.int("points"), f"farmeó {cs_per_min:.1f} por minuto"


def _check_first_blood_kill(r: MatchRecord, p: Params) -> Hit:
    if not r.first_blood_kill:
        return None
    return p.int("points"), "se llevó la primera sangre"


def _check_clean_kda(r: MatchRecord, p: Params) -> Hit:
    if r.deaths == 0 or r.kills + r.assists < p["min_takedowns"] or r.kda < p["min_kda"]:
        return None
    return p.int("points"), f"terminó con KDA {r.kda:.1f} ({r.kills}/{r.deaths}/{r.assists})"


CARRY_RULES: dict[str, RuleSpec] = {
    spec.code: spec
    for spec in (
        RuleSpec(
            "pentakill", "🖐️", "Pentakill",
            "Hizo una pentakill (+3 por cada otra).",
            _ALL_PVP,
            {"points": 8, "extra_each": 3},
            _check_pentakill,
            supersedes=frozenset({"quadrakill"}),
        ),
        RuleSpec(
            "damage_carry", "💥", "Le pegó a todos",
            "Una gran parte del daño a campeones de su equipo (+1 si fue enorme).",
            _ALL_PVP,
            {"points": 4, "min_share": 0.33, "min_share_aram": 0.28, "min_share_chaos": 0.28,
             "huge_share": 0.42, "huge_bonus": 1, "min_minutes": 15},
            _check_damage_carry,
        ),
        RuleSpec(
            "steal", "🐉", "Robo épico",
            "Robó un Barón, un dragón o un heraldo.",
            _RIFT_ONLY,
            {"points": 4, "min_steals": 1},
            _check_steal,
        ),
        RuleSpec(
            "comeback", "🔄", "Remontada",
            "Ganaron estando muy abajo en oro, y él fue clave (daño o participación).",
            _RIFT_ONLY,
            {"points": 4, "min_deficit": 5000, "min_share": 0.28, "min_kp": 0.65},
            _check_comeback,
        ),
        RuleSpec(
            "enabler", "💚", "Habilitador",
            "Support que estuvo en casi todas las kills con una banda de asistencias, y ganaron.",
            _RIFT_ONLY,
            {"points": 5, "min_kp": 0.75, "min_assists": 15, "assists_per_10": 6},
            _check_enabler,
            supersedes=frozenset({"kp_carry"}),
        ),
        RuleSpec(
            "arena_champion", "🏆", "Campeón de Arena",
            "Ganó la Arena.",
            frozenset({ARENA}),
            {"points": 4, "max_placement": 1},
            _check_arena_champion,
        ),
        RuleSpec(
            "slayer", "🗡️", "Máquina de matar",
            "Muchas kills para lo que duró la partida, y una buena parte de las de su equipo.",
            _ALL_PVP,
            {"points": 3, "min_kills": 8, "min_kills_aram": 12, "min_kills_chaos": 14,
             "kills_per_10": 3.5, "kills_per_10_aram": 6, "kills_per_10_chaos": 7, "min_kill_share": 0.30},
            _check_slayer,
        ),
        RuleSpec(
            "quadrakill", "4️⃣", "Quadrakill",
            "Hizo una quadrakill.",
            _ALL_PVP,
            {"points": 3},
            _check_quadrakill,
        ),
        RuleSpec(
            "legendary", "🔥", "Legendario",
            "Racha de 8 kills o más sin morir.",
            _ALL_PVP,
            {"points": 3, "min_spree": 8},
            _check_legendary,
        ),
        RuleSpec(
            "immortal", "🛡️", "Inmortal",
            "No murió nunca en una partida larga y estuvo en las peleas (+2 si estuvo en casi todas).",
            _ALL_PVP,
            {"points": 4, "min_minutes": 22, "min_takedowns": 8, "takedowns_per_10": 3, "min_kp": 0.5,
             "high_kp": 0.7, "high_kp_bonus": 2},
            _check_immortal,
            supersedes=frozenset({"clean_kda"}),
        ),
        RuleSpec(
            "wall", "🧱", "Muralla",
            "Tanqueó una banda del daño del equipo y estuvo en las peleas, y ganaron.",
            _RIFT_ARAM,
            {"points": 4, "min_taken_share": 0.33, "min_kp": 0.6},
            _check_wall,
        ),
        # --- Menores: entre todas suman como mucho weak_points_cap ---
        RuleSpec(
            "kp_carry", "🤝", "Estuvo en todas",
            "Participó en la gran mayoría de las kills de su equipo.",
            _ALL_PVP,
            {"points": 2, "min_kp": 0.75, "min_kp_aram": 0.85, "min_team_kills": 15},
            _check_kp_carry,
            weak=True,
        ),
        RuleSpec(
            "lane_kingdom", "👑", "Dueño de la línea",
            "Muy arriba en oro contra su rival de línea al minuto 15.",
            _RIFT_ONLY,
            {"points": 2, "min_lead": 2500, "min_lead_support": 1500},
            _check_lane_kingdom,
            weak=True,
        ),
        RuleSpec(
            "solo_killer", "⚔️", "Duelista",
            "Ganó varios duelos 1 contra 1.",
            _RIFT_ONLY,
            {"points": 2, "min_solo_kills": 3},
            _check_solo_killer,
            weak=True,
        ),
        RuleSpec(
            "clean_kda", "✨", "KDA limpio",
            "KDA muy alto con varias kills/asistencias (el viejo criterio de carry: solo suma poco).",
            _ALL_PVP,
            {"points": 2, "min_kda": 6, "min_takedowns": 10},
            _check_clean_kda,
            weak=True,
        ),
        RuleSpec(
            "first_blood_kill", "🩸", "Primera sangre",
            "Se llevó la primera sangre.",
            _RIFT_ONLY,
            {"points": 1},
            _check_first_blood_kill,
            weak=True,
        ),
        RuleSpec(
            "vision_lord", "👁️", "Ojo de halcón",
            "Visión muy por encima de lo normal (a los supports se les exige más).",
            _RIFT_ONLY,
            {"points": 1, "min_per_min": 1.4, "min_per_min_support": 2.2, "min_minutes": 20},
            _check_vision_lord,
            weak=True,
        ),
        RuleSpec(
            "farm_machine", "🌾", "Máquina de farmear",
            "Farm por minuto altísimo para su rol.",
            _RIFT_ONLY,
            {"points": 1, "min_cs_per_min": 8.5, "min_cs_per_min_jungle": 7.0, "min_minutes": 15},
            _check_farm_machine,
            weak=True,
        ),
    )
}

# Qué jugada se cuenta primero en la anécdota (a igualdad, la de más puntos).
CARRY_STORY_PRIORITY: dict[str, int] = {
    "pentakill": 100, "steal": 95, "comeback": 90, "immortal": 85, "legendary": 80,
    "quadrakill": 75, "damage_carry": 70, "slayer": 65, "enabler": 62, "wall": 60,
    "arena_champion": 60, "solo_killer": 40, "lane_kingdom": 35, "kp_carry": 30,
    "clean_kda": 25, "first_blood_kill": 20, "vision_lord": 10, "farm_machine": 10,
}

CARRY_CATALOG = Catalog(
    name="carreadas",
    rules=CARRY_RULES,
    level_names=("carry", "legendaria"),
    level_defaults=(8, 15),
    win_multiplier=1.0,
    loss_multiplier=0.5,  # carrear y perder pesa la mitad
    story_priority=CARRY_STORY_PRIORITY,
)
