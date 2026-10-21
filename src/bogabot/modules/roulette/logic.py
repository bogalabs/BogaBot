"""Ruleta "no tan random" (lógica pura, sin Discord ni red).

Le asigna a cada jugador rol, campeón y build:
  - Rol: los 5 roles sin repetir. En modo "fuera de tu zona" pesan más las
    posiciones que menos jugó cada uno (sale de sus partidas guardadas).
  - Campeón: de Data Dragon, con más peso los que tienen sentido para el rol
    (según sus tags) y poca chance de algo maldito (Yuumi jungla). Sus 3
    campeones más jugados casi no salen.
  - Build: botas + 3 o 4 ítems de un "tema" acorde al campeón, con
    `cursed_chance` de que sea una build maldita (Tryndamere AP).
  - Castigo: el peor campeón posible para el rol + build maldita.

Todo recibe un `random.Random` para que los tests sean deterministas.
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from itertools import permutations

from bogabot.modules.predictions.market import Market

ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
ROLE_LABELS = {"TOP": "🗻 Top", "JUNGLE": "🌲 Jungla", "MIDDLE": "🔮 Mid", "BOTTOM": "🏹 ADC",
               "UTILITY": "🛟 Support"}
MODES = {"normal": "🎲 Normal", "fuera": "🙃 Fuera de tu zona"}

# Peso de cada tag de campeón por rol. El tag principal pesa entero y el
# secundario la mitad; si ninguno encaja queda CURSED_WEIGHT (poca chance).
ROLE_FIT: dict[str, dict[str, float]] = {
    "TOP": {"Fighter": 10, "Tank": 8},
    "JUNGLE": {"Fighter": 8, "Assassin": 8, "Tank": 6},
    "MIDDLE": {"Mage": 10, "Assassin": 9},
    "BOTTOM": {"Marksman": 12, "Mage": 3},
    "UTILITY": {"Support": 12, "Tank": 4},
}
CURSED_WEIGHT = 0.3
COMFORT_FACTOR = 0.15  # multiplicador para los campeones de siempre
COMFORT_TOP = 3  # cuántos de los más jugados cuentan como "de siempre"
COMFORT_MIN_GAMES = 3

THEMES = {
    "ap": "🔮 Full AP",
    "tank": "🛡️ Full tanque",
    "lethality": "🗡️ Letalidad",
    "crit": "🎯 Crítico",
    "onhit": "⚡ On-hit",
    "bruiser": "💪 Bruiser",
    "enchanter": "✨ Encantador",
}
# Tema natural según el tag principal del campeón (Marksman: crit u on-hit).
NATURAL_THEMES = {"Mage": ["ap"], "Assassin": ["lethality"], "Marksman": ["crit", "onhit"],
                  "Fighter": ["bruiser"], "Tank": ["tank"], "Support": ["enchanter"]}
# Build maldita: lo más opuesto posible a lo que el campeón quiere.
CURSED_THEMES = {"Mage": ["crit", "lethality"], "Assassin": ["tank", "enchanter"], "Marksman": ["ap", "tank"],
                 "Fighter": ["enchanter", "ap"], "Tank": ["crit", "lethality"], "Support": ["lethality", "crit"]}
# Botas por tema (ids de Data Dragon); si no hay, cualquiera.
THEME_BOOTS = {"ap": ["3020"], "crit": ["3006"], "onhit": ["3006"], "tank": ["3047", "3111"],
               "lethality": ["3158", "3047"], "bruiser": ["3047", "3111"], "enchanter": ["3158", "3009"]}


@dataclass(frozen=True)
class Champion:
    id: str  # como en Data Dragon y en match-v5 (ej. "MonkeyKing")
    name: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    tags: frozenset[str]


def item_themes(item: Item) -> set[str]:
    """A qué temas pertenece un ítem final, según sus tags de Data Dragon."""
    t = item.tags
    themes: set[str] = set()
    ap, ad, crit = "SpellDamage" in t, "Damage" in t, "CriticalStrike" in t
    if ap and not ad and "ManaRegen" not in t:
        themes.add("ap")
    if not ap and not ad and not crit and "ManaRegen" not in t and t & {"Health", "Armor", "SpellBlock"}:
        themes.add("tank")
    if "ArmorPenetration" in t and not crit and not ap:
        themes.add("lethality")
    if crit and not ap:
        themes.add("crit")
    if ("OnHit" in t or "AttackSpeed" in t) and not crit and not ap:
        themes.add("onhit")
    if "Health" in t and ad and not crit:
        themes.add("bruiser")
    if "ManaRegen" in t and (ap or "Health" in t):
        themes.add("enchanter")
    return themes


def role_weight(champion: Champion, role: str) -> float:
    fit = ROLE_FIT[role]
    weights = [fit.get(tag, 0) * (1 if i == 0 else 0.5) for i, tag in enumerate(champion.tags[:2])]
    return max(weights + [0]) or CURSED_WEIGHT


def comfort_champions(history: list[str]) -> set[str]:
    """Los campeones "de siempre" de un jugador (ids, en minúscula)."""
    counts = Counter(c.lower() for c in history)
    return {c for c, n in counts.most_common(COMFORT_TOP) if n >= COMFORT_MIN_GAMES}


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
def assign_roles(players: list[int], positions: dict[int, list[str]], mode: str,
                 rng: random.Random) -> dict[int, str]:
    """Un rol distinto por jugador (máx. 5). `positions`: posiciones jugadas
    por cada uno (de sus partidas). En modo "fuera" se sortea la combinación
    entera, con más peso donde cada uno jugó menos."""
    players = players[:len(ROLES)]
    combos = list(permutations(ROLES, len(players)))
    if mode != "fuera":
        return dict(zip(players, rng.choice(combos)))
    counts = {p: Counter(positions.get(p, [])) for p in players}

    def weight(combo: tuple[str, ...]) -> float:
        w = 1.0
        for p, role in zip(players, combo):
            w /= 1 + counts[p][role]
        return w

    return dict(zip(players, rng.choices(combos, weights=[weight(c) for c in combos])[0]))


# ---------------------------------------------------------------------------
# Campeones y builds
# ---------------------------------------------------------------------------
def pick_champion(role: str, champions: list[Champion], rng: random.Random, *, comfort: set[str] = frozenset(),
                  taken: set[str] = frozenset(), worst: bool = False) -> Champion:
    pool = [c for c in champions if c.id not in taken] or champions
    if worst:
        # Castigo: solo los que no encajan para nada en el rol.
        cursed = [c for c in pool if role_weight(c, role) == CURSED_WEIGHT]
        return rng.choice(cursed or pool)
    weights = [role_weight(c, role) * (COMFORT_FACTOR if c.id.lower() in comfort else 1) for c in pool]
    return rng.choices(pool, weights=weights)[0]


def pick_build(champion: Champion, items: list[Item], boots: list[Item], rng: random.Random, *,
               cursed: bool) -> tuple[str, Item | None, list[Item]]:
    """(tema, botas, ítems). Si el tema no alcanza para llenar, se completa
    con ítems de cualquier tema."""
    main_tag = champion.tags[0] if champion.tags else "Fighter"
    theme = rng.choice((CURSED_THEMES if cursed else NATURAL_THEMES).get(main_tag, list(THEMES)))
    count = rng.choice((3, 4))
    pool = [i for i in items if theme in item_themes(i)]
    picked: list[Item] = []
    seen_names: set[str] = set()
    for item in rng.sample(pool, len(pool)) + rng.sample(items, len(items)):
        if len(picked) == count:
            break
        if item.name not in seen_names:
            picked.append(item)
            seen_names.add(item.name)
    preferred = [b for b in boots if b.id in THEME_BOOTS.get(theme, [])]
    chosen_boots = rng.choice(preferred or boots) if boots else None
    return theme, chosen_boots, picked


@dataclass
class Pick:
    role: str
    champion_id: str
    champion_name: str
    theme: str
    boots: str
    items: list[str]
    cursed: bool = False
    punished: bool = False
    rerolls: int = 0

    def to_dict(self) -> dict:
        return {"role": self.role, "champion_id": self.champion_id, "champion_name": self.champion_name,
                "theme": self.theme, "boots": self.boots, "items": self.items, "cursed": self.cursed,
                "punished": self.punished, "rerolls": self.rerolls}

    @classmethod
    def from_dict(cls, d: dict) -> "Pick":
        return cls(role=d["role"], champion_id=d["champion_id"], champion_name=d["champion_name"],
                   theme=d["theme"], boots=d.get("boots", ""), items=list(d.get("items", [])),
                   cursed=bool(d.get("cursed")), punished=bool(d.get("punished")),
                   rerolls=int(d.get("rerolls", 0)))


def make_pick(role: str, champions: list[Champion], items: list[Item], boots: list[Item], rng: random.Random, *,
              comfort: set[str] = frozenset(), taken: set[str] = frozenset(), cursed_chance: float = 0.1,
              worst: bool = False) -> Pick:
    champion = pick_champion(role, champions, rng, comfort=comfort, taken=taken, worst=worst)
    cursed = worst or rng.random() < cursed_chance
    theme, chosen_boots, picked = pick_build(champion, items, boots, rng, cursed=cursed)
    return Pick(role=role, champion_id=champion.id, champion_name=champion.name, theme=theme,
                boots=chosen_boots.name if chosen_boots else "", items=[i.name for i in picked], cursed=cursed,
                punished=worst)


# ---------------------------------------------------------------------------
# Tirada guardada (para re-tirar, castigar, la apuesta y el bonus)
# ---------------------------------------------------------------------------
def win_market(players: list[int]) -> Market:
    """"¿Ganan con la ruleta?". Los que juegan solo pueden apostar al Sí."""
    return Market(key="roulette", title="🎰 ¿Ganan con la ruleta?", options={"yes": "🏆 Sí", "no": "💩 No"},
                  forbidden={uid: ["no"] for uid in players})


@dataclass
class Spin:
    id: str
    created_at: datetime
    mode: str
    picks: dict[int, Pick]
    market: Market
    channel_id: int = 0
    message_id: int = 0
    match_id: str | None = None  # partida en vivo detectada: cierra re-tiradas, castigos y apuestas
    paid: list[int] = field(default_factory=list)  # a quiénes ya se les pagó el bonus
    resolved: bool = False  # la apuesta ya se pagó (o devolvió)

    @property
    def is_open(self) -> bool:
        return self.match_id is None

    def complied(self, discord_id: int, champion: str, position: str) -> bool:
        """Jugó lo que le tocó: mismo campeón y misma posición."""
        pick = self.picks.get(discord_id)
        return pick is not None and pick.champion_id.lower() == champion.lower() and pick.role == position

    def to_dict(self) -> dict:
        return {"id": self.id, "created_at": self.created_at.isoformat(), "mode": self.mode,
                "picks": {str(k): p.to_dict() for k, p in self.picks.items()}, "market": self.market.to_dict(),
                "channel_id": self.channel_id, "message_id": self.message_id, "match_id": self.match_id,
                "paid": self.paid, "resolved": self.resolved}

    @classmethod
    def from_dict(cls, d: dict) -> "Spin":
        return cls(id=d["id"], created_at=datetime.fromisoformat(d["created_at"]), mode=d["mode"],
                   picks={int(k): Pick.from_dict(p) for k, p in d["picks"].items()},
                   market=Market.from_dict(d["market"]), channel_id=int(d.get("channel_id", 0)),
                   message_id=int(d.get("message_id", 0)), match_id=d.get("match_id"),
                   paid=[int(x) for x in d.get("paid", [])], resolved=bool(d.get("resolved", False)))
