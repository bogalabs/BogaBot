"""Modelos del dominio.

Estos dataclasses son el "lenguaje" interno de la app. El resto del código
(scoring, comandos, ranking) trabaja SIEMPRE con estos objetos, nunca con el
JSON crudo de Riot ni con el formato de almacenamiento de Discord. Así, si
cambia la API de Riot o la capa de storage, estos modelos quedan estables.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


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

    La clave de deduplicación es (match_id, puuid): una misma partida genera
    un registro por cada jugador vinculado que la haya jugado.
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

    def is_troll_game(self) -> bool:
        # KDA menor a 0.5, perdieron por surrender antes de los 20 min (1200 segs)
        kda = (self.kills + self.assists) / max(self.deaths, 1)
        is_early_ff = self.game_duration_seconds < 1200 and self.game_ended_in_surrender
        return kda < 0.5 and is_early_ff and not self.win

    def is_papelon(self) -> bool:
        # Pierden la partida antes de los 25 minutos (1500 segundos)
        return self.game_duration_seconds < 1500 and not self.win

    @property
    def dedup_key(self) -> str:
        return f"{self.match_id}:{self.puuid}"

    def to_dict(self) -> dict:
        return {
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
