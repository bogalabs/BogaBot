"""Traduce el JSON crudo de match-v5 a nuestro modelo interno `MatchRecord`.

Esta es la ÚNICA parte del código que conoce el shape de la respuesta de Riot.
Si Riot cambia nombres de campos, se toca solo acá.
"""
from __future__ import annotations

import logging

from bogabot.core.models import MatchParticipant, MatchRecord, MatchSummary
from bogabot.core.timeutils import from_epoch_millis

log = logging.getLogger(__name__)

# Partidas más cortas que esto se consideran remakes y se descartan.
MIN_DURATION_SECONDS = 300

# ID de la cola de Ranked Flex 5v5 (la única que cuenta para "Trolls y Pros").
RANKED_FLEX_QUEUE_ID = 440

# Nombres legibles por queueId (ver Riot Data Dragon "queues.json"). Solo se
# listan las colas relevantes para el grupo; las demás caen al fallback.
QUEUE_NAMES: dict[int, str] = {
    400: "Normal Draft",
    420: "Ranked Solo/Duo",
    430: "Normal Blind",
    440: "Ranked Flex",
    450: "ARAM",
    490: "Normal (Quickplay)",
    700: "Clash",
    900: "URF",
    1020: "One for All",
    1300: "Nexus Blitz",
    1400: "Ultimate Spellbook",
    1700: "Arena",
    1900: "URF (Sin límites)",
}


def queue_name(queue_id: int) -> str:
    """Nombre legible de una cola de Riot, o un fallback genérico si no la
    tenemos mapeada (modos rotativos nuevos, etc)."""
    return QUEUE_NAMES.get(queue_id, f"Otra cola ({queue_id})")


def _game_duration_seconds(info: dict) -> int:
    """match-v5 la da en segundos, salvo partidas viejas que la dan en ms."""
    duration = int(info.get("gameDuration", 0))
    if duration and duration > 100_000:  # guarda por si viniera en milisegundos
        duration //= 1000
    return duration


def _farm(participant: dict) -> int:
    """CS total: minions de línea + monstruos de jungla."""
    return int(participant.get("totalMinionsKilled", 0)) + int(participant.get("neutralMinionsKilled", 0))


def map_match(data: dict, puuid: str, discord_id: int) -> MatchRecord | None:
    """Extrae las stats del jugador `puuid` en la partida. Devuelve None si es
    un remake o si el jugador no aparece (no debería pasar)."""
    info = data.get("info", {})
    metadata = data.get("metadata", {})
    match_id = metadata.get("matchId", "")

    duration = _game_duration_seconds(info)

    participant = next(
        (p for p in info.get("participants", []) if p.get("puuid") == puuid), None
    )
    if participant is None:
        log.warning("PUUID %s no encontrado en la partida %s.", puuid, match_id)
        return None

    if duration < MIN_DURATION_SECONDS or participant.get("gameEndedInEarlySurrender"):
        return None

    opponent_champion = _find_opponent_champion(info.get("participants", []), participant)

    return MatchRecord(
        match_id=match_id,
        puuid=puuid,
        discord_id=discord_id,
        game_name=participant.get("riotIdGameName") or participant.get("summonerName", ""),
        game_creation=from_epoch_millis(int(info.get("gameCreation", 0))),
        game_duration_seconds=duration,
        queue_id=int(info.get("queueId", 0)),
        game_mode=info.get("gameMode", ""),
        champion=participant.get("championName", ""),
        position=participant.get("teamPosition", ""),
        win=bool(participant.get("win", False)),
        kills=int(participant.get("kills", 0)),
        deaths=int(participant.get("deaths", 0)),
        assists=int(participant.get("assists", 0)),
        damage_to_champions=int(participant.get("totalDamageDealtToChampions", 0)),
        vision_score=int(participant.get("visionScore", 0)),
        opponent_champion=opponent_champion,
        cs=_farm(participant),
    )


def map_match_summary(data: dict, puuid_to_discord: dict[str, int]) -> MatchSummary:
    """Arma el resumen completo de la partida (los 10 jugadores), para el
    aviso "en vivo". A diferencia de `map_match`, no filtra por remake ni por
    vínculo: es solo para mostrar, no para el ranking."""
    info = data.get("info", {})
    metadata = data.get("metadata", {})
    participants = [
        MatchParticipant(
            discord_id=puuid_to_discord.get(p.get("puuid", "")),
            display_name=p.get("riotIdGameName") or p.get("summonerName", "?"),
            champion=p.get("championName", ""),
            team_id=int(p.get("teamId", 0)),
            position=p.get("teamPosition", ""),
            win=bool(p.get("win", False)),
            kills=int(p.get("kills", 0)),
            deaths=int(p.get("deaths", 0)),
            assists=int(p.get("assists", 0)),
            cs=_farm(p),
        )
        for p in info.get("participants", [])
    ]
    return MatchSummary(
        match_id=metadata.get("matchId", ""),
        queue_id=int(info.get("queueId", 0)),
        game_duration_seconds=_game_duration_seconds(info),
        participants=participants,
    )


def _find_opponent_champion(participants: list[dict], me: dict) -> str:
    """Busca al rival de línea: mismo teamPosition, equipo contrario. Devuelve
    "" si no hay posición (ej. ARAM) o no se encuentra (no debería pasar)."""
    position = me.get("teamPosition")
    if not position:
        return ""
    rival = next(
        (
            p for p in participants
            if p.get("teamPosition") == position and p.get("teamId") != me.get("teamId")
        ),
        None,
    )
    return rival.get("championName", "") if rival else ""
