"""Traduce el JSON crudo de match-v5 a nuestro modelo interno `MatchRecord`.

Esta es la ÚNICA parte del código que conoce el shape de la respuesta de Riot.
Si Riot cambia nombres de campos, se toca solo acá.
"""
from __future__ import annotations

import logging

from bogabot.core.models import MatchRecord
from bogabot.core.timeutils import from_epoch_millis

log = logging.getLogger(__name__)

# Partidas más cortas que esto se consideran remakes y se descartan.
MIN_DURATION_SECONDS = 300


def map_match(data: dict, puuid: str, discord_id: int) -> MatchRecord | None:
    """Extrae las stats del jugador `puuid` en la partida. Devuelve None si es
    un remake o si el jugador no aparece (no debería pasar)."""
    info = data.get("info", {})
    metadata = data.get("metadata", {})
    match_id = metadata.get("matchId", "")

    # Duración: match-v5 la da en segundos. Descartamos remakes / early surrender.
    duration = int(info.get("gameDuration", 0))
    if duration and duration > 100_000:  # guarda por si viniera en milisegundos
        duration //= 1000

    participant = next(
        (p for p in info.get("participants", []) if p.get("puuid") == puuid), None
    )
    if participant is None:
        log.warning("PUUID %s no encontrado en la partida %s.", puuid, match_id)
        return None

    if duration < MIN_DURATION_SECONDS or participant.get("gameEndedInEarlySurrender"):
        return None

    return MatchRecord(
        match_id=match_id,
        puuid=puuid,
        participant_id=int(participant.get("participantId", 0)),
        discord_id=discord_id,
        game_name=participant.get("riotIdGameName") or participant.get("summonerName", ""),
        game_creation=from_epoch_millis(int(info.get("gameCreation", 0))),
        game_duration_seconds=duration,
        queue_id=int(info.get("queueId", 0)),
        game_mode=info.get("gameMode", ""),
        champion=participant.get("championName", ""),
        position=participant.get("teamPosition", ""),
        win=bool(participant.get("win", False)),
        game_ended_in_surrender=bool(participant.get("gameEndedInSurrender", False)),
        kills=int(participant.get("kills", 0)),
        deaths=int(participant.get("deaths", 0)),
        assists=int(participant.get("assists", 0)),
        damage_to_champions=int(participant.get("totalDamageDealtToChampions", 0)),
        vision_score=int(participant.get("visionScore", 0)),
    )
