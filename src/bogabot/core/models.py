"""Modelos del dominio.

Estos dataclasses son el "lenguaje" interno de la app. El resto del código
(scoring, comandos, ranking) trabaja SIEMPRE con estos objetos, nunca con el
JSON crudo de Riot ni con el formato de almacenamiento de Discord. Así, si
cambia la API de Riot o la capa de storage, estos modelos quedan estables.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _opt_int(value) -> int | None:
    return None if value is None else int(value)


def _opt_bool(value) -> bool | None:
    return None if value is None else bool(value)


@dataclass
class PlayerLink:
    """Asociación entre un usuario de Discord y una cuenta de Riot."""

    discord_id: int
    game_name: str
    tag_line: str
    puuid: str
    linked_at: datetime

    @property
    def riot_id(self) -> str:
        return f"{self.game_name}#{self.tag_line}"

    def to_dict(self) -> dict:
        return {
            "discord_id": self.discord_id,
            "game_name": self.game_name,
            "tag_line": self.tag_line,
            "puuid": self.puuid,
            "linked_at": _iso(self.linked_at),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlayerLink":
        return cls(
            discord_id=int(d["discord_id"]),
            game_name=d["game_name"],
            tag_line=d["tag_line"],
            puuid=d["puuid"],
            linked_at=_parse_iso(d["linked_at"]),
        )


@dataclass
class MatchRecord:
    """Stats de UN jugador en UNA partida. Es la unidad que se persiste.

    La clave de deduplicación es (match_id, discord_id): una misma partida
    genera un registro por cada jugador vinculado que la haya jugado. No se usa
    el puuid porque Riot lo encripta distinto según la app de la API key: al
    cambiar de key, el mismo jugador vuelve con otro puuid.
    """

    match_id: str
    puuid: str
    participant_id: int
    discord_id: int
    game_name: str  # denormalizado para poder mostrar nombre aunque se desvincule
    game_creation: datetime
    game_duration_seconds: int
    queue_id: int
    game_mode: str
    champion: str
    position: str  # TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY o "" (ej. ARAM)
    win: bool
    game_ended_in_surrender: bool
    kills: int
    deaths: int
    assists: int
    damage_to_champions: int
    vision_score: int
    opponent_champion: str = ""  # rival de línea (mismo position, equipo contrario) o "" si no aplica
    cs: int = 0  # farm: minions de línea + monstruos de jungla

    # --- Stats extendidas (para el detector de trolls, ver trolls/) --------
    # Todas opcionales: los registros guardados antes de que existieran no las
    # tienen (quedan en None) y las reglas que las necesitan se saltean.
    time_dead_seconds: int | None = None
    control_wards_bought: int | None = None
    question_pings: int | None = None  # pings de "?" (enemyMissingPings)
    placement: int | None = None  # puesto final en Arena (1..8); None en otros modos
    # Contexto del equipo (sale de los 10 jugadores de la partida).
    team_kills: int | None = None
    team_deaths: int | None = None
    enemy_kills: int | None = None
    team_damage: int | None = None
    damage_taken: int | None = None  # para distinguir al tanque que absorbe del que no pelea
    team_damage_taken: int | None = None
    # Del timeline de la partida (None si no se pudo pedir).
    first_death_minute: int | None = None
    deaths_before_10: int | None = None
    gave_first_blood: bool | None = None
    executed_deaths: int | None = None  # muertes sin campeón asesino (torre/minions/monstruos)
    items_sold: int | None = None
    deaths_to_lane_opponent: int | None = None
    gold_diff_15: int | None = None  # oro propio - oro del rival de línea al minuto 15
    # Situaciones del timeline (v2): posiciones, edificios y objetivos.
    base_absent: int | None = None  # estructuras de la base propia perdidas mientras estaba lejos y vivo
    base_absent_farming: str | None = None  # "jungla"/"línea" si farmeaba mientras caía la base
    throw_deaths: int | None = None  # veces que murió primero y enseguida perdieron Barón/Ancestral/nexo
    throw_objective: str | None = None  # qué perdieron la primera vez (para la anécdota)
    afk_minutes: int | None = None  # racha más larga de minutos quieto, vivo y sin ganar experiencia
    rich_deaths: int | None = None  # muertes con mucho oro sin gastar encima
    max_gold_on_death: int | None = None
    timeline_version: int | None = None  # versión del análisis de timeline aplicado (ver mapper)
    # Para las carreadas (ver bogabot/carries/).
    penta_kills: int | None = None
    quadra_kills: int | None = None
    largest_killing_spree: int | None = None
    first_blood_kill: bool | None = None
    solo_kills: int | None = None  # challenges.soloKills (puede faltar)
    objective_steals: int | None = None  # challenges.epicMonsterSteals (Barón/dragón/heraldo robado)
    max_gold_deficit: int | None = None  # peor desventaja de oro del equipo (timeline); 0 si nunca perdía

    @property
    def dedup_key(self) -> str:
        return f"{self.match_id}:{self.discord_id}"

    @property
    def kda(self) -> float:
        return (self.kills + self.assists) / max(self.deaths, 1)

    @property
    def minutes(self) -> float:
        return self.game_duration_seconds / 60

    @property
    def game_end(self) -> datetime:
        return self.game_creation + timedelta(seconds=self.game_duration_seconds)

    @property
    def kill_participation(self) -> float | None:
        if not self.team_kills:
            return None
        return (self.kills + self.assists) / self.team_kills

    @property
    def damage_share(self) -> float | None:
        if not self.team_damage:
            return None
        return self.damage_to_champions / self.team_damage

    @property
    def damage_taken_share(self) -> float | None:
        if self.damage_taken is None or not self.team_damage_taken:
            return None
        return self.damage_taken / self.team_damage_taken

    @property
    def has_extended_stats(self) -> bool:
        """False en registros guardados antes de las stats extendidas."""
        return self.team_kills is not None

    @property
    def has_timeline(self) -> bool:
        return self.deaths_before_10 is not None

    def to_dict(self) -> dict:
        data = {
            "match_id": self.match_id,
            "puuid": self.puuid,
            "participant_id": self.participant_id,
            "discord_id": self.discord_id,
            "game_name": self.game_name,
            "game_creation": _iso(self.game_creation),
            "game_duration_seconds": self.game_duration_seconds,
            "queue_id": self.queue_id,
            "game_mode": self.game_mode,
            "champion": self.champion,
            "position": self.position,
            "opponent_champion": self.opponent_champion,
            "win": self.win,
            "game_ended_in_surrender": self.game_ended_in_surrender,
            "kills": self.kills,
            "deaths": self.deaths,
            "assists": self.assists,
            "damage_to_champions": self.damage_to_champions,
            "vision_score": self.vision_score,
            "cs": self.cs,
        }
        # Las opcionales solo se escriben si tienen valor: cada registro es un
        # mensaje de Discord (máx. 2000 caracteres) y los legacy no las tienen.
        for name in _OPTIONAL_RECORD_FIELDS:
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "MatchRecord":
        return cls(
            match_id=d["match_id"],
            puuid=d["puuid"],
            participant_id=int(d.get("participant_id", 0)),
            discord_id=int(d["discord_id"]),
            game_name=d.get("game_name", ""),
            game_creation=_parse_iso(d["game_creation"]),
            game_duration_seconds=int(d["game_duration_seconds"]),
            queue_id=int(d["queue_id"]),
            game_mode=d.get("game_mode", ""),
            champion=d["champion"],
            position=d.get("position", ""),
            opponent_champion=d.get("opponent_champion", ""),
            win=bool(d["win"]),
            game_ended_in_surrender=bool(d.get("game_ended_in_surrender", False)),
            kills=int(d["kills"]),
            deaths=int(d["deaths"]),
            assists=int(d["assists"]),
            damage_to_champions=int(d["damage_to_champions"]),
            vision_score=int(d["vision_score"]),
            cs=int(d.get("cs", 0)),
            time_dead_seconds=_opt_int(d.get("time_dead_seconds")),
            control_wards_bought=_opt_int(d.get("control_wards_bought")),
            question_pings=_opt_int(d.get("question_pings")),
            placement=_opt_int(d.get("placement")),
            team_kills=_opt_int(d.get("team_kills")),
            team_deaths=_opt_int(d.get("team_deaths")),
            enemy_kills=_opt_int(d.get("enemy_kills")),
            team_damage=_opt_int(d.get("team_damage")),
            damage_taken=_opt_int(d.get("damage_taken")),
            team_damage_taken=_opt_int(d.get("team_damage_taken")),
            first_death_minute=_opt_int(d.get("first_death_minute")),
            deaths_before_10=_opt_int(d.get("deaths_before_10")),
            gave_first_blood=_opt_bool(d.get("gave_first_blood")),
            executed_deaths=_opt_int(d.get("executed_deaths")),
            items_sold=_opt_int(d.get("items_sold")),
            deaths_to_lane_opponent=_opt_int(d.get("deaths_to_lane_opponent")),
            gold_diff_15=_opt_int(d.get("gold_diff_15")),
            base_absent=_opt_int(d.get("base_absent")),
            base_absent_farming=d.get("base_absent_farming"),
            throw_deaths=_opt_int(d.get("throw_deaths")),
            throw_objective=d.get("throw_objective"),
            afk_minutes=_opt_int(d.get("afk_minutes")),
            rich_deaths=_opt_int(d.get("rich_deaths")),
            max_gold_on_death=_opt_int(d.get("max_gold_on_death")),
            timeline_version=_opt_int(d.get("timeline_version")),
            penta_kills=_opt_int(d.get("penta_kills")),
            quadra_kills=_opt_int(d.get("quadra_kills")),
            largest_killing_spree=_opt_int(d.get("largest_killing_spree")),
            first_blood_kill=_opt_bool(d.get("first_blood_kill")),
            solo_kills=_opt_int(d.get("solo_kills")),
            objective_steals=_opt_int(d.get("objective_steals")),
            max_gold_deficit=_opt_int(d.get("max_gold_deficit")),
        )


# Campos opcionales de MatchRecord (se serializan solo si no son None).
_OPTIONAL_RECORD_FIELDS = (
    "time_dead_seconds", "control_wards_bought", "question_pings", "placement",
    "team_kills", "team_deaths", "enemy_kills", "team_damage", "damage_taken", "team_damage_taken",
    "first_death_minute", "deaths_before_10", "gave_first_blood", "executed_deaths",
    "items_sold", "deaths_to_lane_opponent", "gold_diff_15",
    "base_absent", "base_absent_farming", "throw_deaths", "throw_objective",
    "afk_minutes", "rich_deaths", "max_gold_on_death", "timeline_version",
    "penta_kills", "quadra_kills", "largest_killing_spree", "first_blood_kill", "solo_kills",
    "objective_steals", "max_gold_deficit",
)


@dataclass
class PlayerStats:
    """Agregado de un jugador sobre una ventana de tiempo (día/semana)."""

    discord_id: int
    display_name: str
    games: int = 0
    wins: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    damage_to_champions: int = 0
    vision_score: int = 0
    cs: int = 0
    carry_games: int = 0
    troll_games: int = 0
    duration_seconds: int = 0
    champions: list[str] = field(default_factory=list)

    def add(self, m: MatchRecord) -> None:
        self.display_name = m.game_name or self.display_name
        self.games += 1
        self.wins += 1 if m.win else 0
        self.kills += m.kills
        self.deaths += m.deaths
        self.assists += m.assists
        self.damage_to_champions += m.damage_to_champions
        self.vision_score += m.vision_score
        self.cs += m.cs
        if m.win and m.kda >= 5.0:
            self.carry_games += 1
        if m.kda < 0.5:
            self.troll_games += 1
        self.duration_seconds += m.game_duration_seconds
        self.champions.append(m.champion)

    # --- Métricas derivadas (las usa el motor de scoring) ------------------
    @property
    def losses(self) -> int:
        return self.games - self.wins

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    @property
    def kda(self) -> float:
        return (self.kills + self.assists) / max(self.deaths, 1)

    @property
    def avg_kills(self) -> float:
        return self.kills / self.games if self.games else 0.0

    @property
    def avg_deaths(self) -> float:
        return self.deaths / self.games if self.games else 0.0

    @property
    def avg_assists(self) -> float:
        return self.assists / self.games if self.games else 0.0

    @property
    def damage_per_min(self) -> float:
        minutes = self.duration_seconds / 60
        return self.damage_to_champions / minutes if minutes else 0.0

    @property
    def avg_vision(self) -> float:
        return self.vision_score / self.games if self.games else 0.0

    @property
    def avg_cs(self) -> float:
        return self.cs / self.games if self.games else 0.0

    @property
    def cs_per_min(self) -> float:
        minutes = self.duration_seconds / 60
        return self.cs / minutes if minutes else 0.0

    @property
    def games_played(self) -> float:
        return float(self.games)

    @property
    def carry_rate(self) -> float:
        """Proporción de partidas carreadas (0.0 a 1.0). Mide calidad, no volumen."""
        return self.carry_games / self.games if self.games else 0.0

    @property
    def troll_rate(self) -> float:
        """Proporción de partidas troleadas (0.0 a 1.0). Mide calidad, no volumen."""
        return self.troll_games / self.games if self.games else 0.0


@dataclass
class MatchParticipant:
    """Un jugador dentro de una partida completa (los 10), para el aviso "en
    vivo" de partida terminada. No se persiste: se arma al vuelo con el JSON
    de match-v5 (ver `riot/mapper.py::map_match_summary`)."""

    discord_id: int | None  # None si no está vinculado al grupo
    display_name: str  # Riot ID, para mostrar cuando no hay discord_id
    champion: str
    team_id: int
    position: str  # TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY o "" (ej. ARAM)
    win: bool
    kills: int
    deaths: int
    assists: int
    cs: int


@dataclass
class MatchSummary:
    """Resumen de una partida completa (10 jugadores), para el aviso "en
    vivo". No se persiste (ver `MatchParticipant`)."""

    match_id: str
    queue_id: int
    game_duration_seconds: int
    participants: list[MatchParticipant]


@dataclass
class RankingRow:
    """Una fila del ranking ya calculado."""

    rank: int
    discord_id: int
    display_name: str
    score: float
    stats: PlayerStats


class TrollLevel(IntEnum):
    """Gravedad de una partida según los puntos troll (umbrales en
    config/trolls.yaml). Define a dónde va el aviso."""

    NONE = 0  # nada que avisar (igual suma puntos al ranking troll)
    TROLL = 1  # aviso en el canal de trolls
    PAPELON = 2  # papelón histórico: va a #general


@dataclass(frozen=True)
class TrollFlag:
    """Un "cargo" detectado en una partida (ej. feeder, FF al 15)."""

    code: str
    emoji: str
    title: str
    detail: str
    points: int


@dataclass
class TrollVerdict:
    """Resultado del detector de trolls para UN jugador en UNA partida."""

    record: MatchRecord
    flags: list[TrollFlag]
    base_points: int
    points: int  # ya con multiplicadores (ranked / victoria)
    level: TrollLevel
    ranked_bonus: bool = False
    carried: bool = False  # ganó igual: el equipo lo llevó de mochila
    capped_points: int = 0  # puntos de cargos menores que no sumaron (tope)

    @property
    def is_clean(self) -> bool:
        return not self.flags


@dataclass
class TrollStanding:
    """Una fila del ranking troll de un período."""

    rank: int
    discord_id: int
    display_name: str
    points: int = 0
    games: int = 0
    troll_games: int = 0  # partidas con nivel TROLL o más
    papelones: int = 0  # partidas con nivel PAPELON
    flag_counts: dict[str, int] = field(default_factory=dict)  # code -> veces
    worst: TrollVerdict | None = None  # la partida con más puntos del período
    # Índice troll: puntos por partida suavizados hacia el promedio del grupo
    # (ver `TrollService._aggregate`). Es lo que ordena el ranking: jugar
    # mucho no suma por sí solo, el que la trollea fuerte en 2 partidas queda
    # arriba del que jugó 18 y trolleó 2.
    index: float = 0.0
    index_points: float = 0.0  # suma de puntos con el tope por partida aplicado
    previous_index: float | None = None  # índice del período anterior (tendencia)

    @property
    def average(self) -> float:
        """Puntos troll promedio por partida, sin suavizar."""
        return self.points / self.games if self.games else 0.0

    @property
    def troll_rate(self) -> float:
        """Fracción de partidas que llegaron a alerta troll."""
        return self.troll_games / self.games if self.games else 0.0
