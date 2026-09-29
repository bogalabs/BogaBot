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
# Colas rankeadas (el detector de trolls pondera más un papelón en ranked).
RANKED_QUEUE_IDS = frozenset({420, 440})
# Swiftplay: partidas cortas por diseño (la rendición temprana es normal).
SWIFTPLAY_QUEUE_ID = 480

# ID de la cola de Ranked Solo/Duo.
RANKED_SOLO_QUEUE_ID = 420

# Todas las colas ranked (para filtrar rankings diario/semanal).
RANKED_QUEUE_IDS: frozenset[int] = frozenset({RANKED_FLEX_QUEUE_ID, RANKED_SOLO_QUEUE_ID})

# Nombres legibles por queueId (ver Riot Data Dragon "queues.json"). Solo se
# listan las colas relevantes para el grupo; las demás caen al fallback.
QUEUE_NAMES: dict[int, str] = {
    400: "Normal Draft",
    420: "Ranked Solo/Duo",
    430: "Normal Blind",
    440: "Ranked Flex",
    450: "ARAM",
    480: "Swiftplay",
    490: "Normal (Quickplay)",
    700: "Clash",
    720: "ARAM (Clash)",
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


def is_remake(data: dict) -> bool:
    """True si la partida es un remake (muy corta o early surrender): no
    cuenta para nada."""
    info = data.get("info", {})
    if _game_duration_seconds(info) < MIN_DURATION_SECONDS:
        return True
    return any(p.get("gameEndedInEarlySurrender") for p in info.get("participants", []))


def map_match(
    data: dict,
    puuid: str,
    discord_id: int,
    timeline: dict | None = None,
    participant_id: int | None = None,
) -> MatchRecord | None:
    """Extrae las stats del jugador `puuid` en la partida. Devuelve None si es
    un remake o si el jugador no aparece (no debería pasar).

    Con `timeline` (respuesta de /matches/{id}/timeline) se completan además
    los datos minuto a minuto que usa el detector de trolls (primera sangre,
    muertes antes del 10, oro vs. rival al 15, items vendidos, etc).
    `participant_id` sirve de respaldo para ubicar al jugador si el puuid
    guardado es de otra API key (ver `RiotPuuidMismatchError`)."""
    info = data.get("info", {})
    metadata = data.get("metadata", {})
    match_id = metadata.get("matchId", "")

    duration = _game_duration_seconds(info)
    participants = info.get("participants", [])

    participant = next((p for p in participants if p.get("puuid") == puuid), None)
    if participant is None and participant_id:
        participant = next(
            (p for p in participants if int(p.get("participantId", 0)) == participant_id), None
        )
    if participant is None:
        log.warning("PUUID %s no encontrado en la partida %s.", puuid, match_id)
        return None

    if duration < MIN_DURATION_SECONDS or participant.get("gameEndedInEarlySurrender"):
        return None

    opponent = _find_opponent(participants, participant)
    opponent_champion = opponent.get("championName", "") if opponent else ""
    team_id = participant.get("teamId")
    team = [p for p in participants if p.get("teamId") == team_id]
    enemies = [p for p in participants if p.get("teamId") != team_id]
    placement = participant.get("placement")

    record = MatchRecord(
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
        opponent_champion=opponent_champion,
        cs=_farm(participant),
        # Si Riot no manda un campo, queda en None (la regla se saltea) en vez
        # de 0, que dispararía reglas como "ni un control ward" en falso.
        time_dead_seconds=_opt_int(participant, "totalTimeSpentDead"),
        control_wards_bought=_opt_int(participant, "visionWardsBoughtInGame"),
        question_pings=_opt_int(participant, "enemyMissingPings"),
        placement=int(placement) if placement else None,
        team_kills=_sum(team, "kills"),
        team_deaths=_sum(team, "deaths"),
        enemy_kills=_sum(enemies, "kills"),
        team_damage=_sum(team, "totalDamageDealtToChampions"),
    )
    if timeline is not None:
        _apply_timeline(
            record,
            timeline,
            opponent_participant_id=int(opponent.get("participantId", 0)) if opponent else None,
        )
    return record


def _sum(participants: list[dict], key: str) -> int:
    return sum(int(p.get(key, 0)) for p in participants)


def _opt_int(participant: dict, key: str) -> int | None:
    value = participant.get(key)
    return None if value is None else int(value)


# --- Timeline (match-v5 /timeline) -------------------------------------------
_MINUTE_MS = 60_000


def _apply_timeline(record: MatchRecord, timeline: dict, opponent_participant_id: int | None) -> None:
    """Completa en `record` los datos que salen del timeline. Si el JSON viene
    raro, deja todos esos campos en None en vez de romper la ingesta."""
    try:
        stats = _timeline_stats(timeline, record.participant_id, opponent_participant_id)
    except (AttributeError, TypeError, ValueError, KeyError):
        log.warning("Timeline ilegible para %s; sigo sin esos datos.", record.match_id)
        return
    for name, value in stats.items():
        setattr(record, name, value)


def _timeline_stats(timeline: dict, me: int, opponent: int | None) -> dict:
    frames = timeline["info"]["frames"]
    events = sorted(
        (e for f in frames for e in f.get("events", [])),
        key=lambda e: int(e.get("timestamp", 0)),
    )
    kills = [e for e in events if e.get("type") == "CHAMPION_KILL"]
    my_deaths = [e for e in kills if e.get("victimId") == me]

    sold = sum(1 for e in events if e.get("type") == "ITEM_SOLD" and e.get("participantId") == me)
    # Un "deshacer" de una venta (beforeId 0 -> afterId item) la anula.
    undone = sum(
        1 for e in events
        if e.get("type") == "ITEM_UNDO" and e.get("participantId") == me
        and not e.get("beforeId") and e.get("afterId")
    )
    stats = {
        "first_death_minute": int(my_deaths[0].get("timestamp", 0)) // _MINUTE_MS if my_deaths else None,
        "deaths_before_10": sum(1 for e in my_deaths if int(e.get("timestamp", 0)) < 10 * _MINUTE_MS),
        "gave_first_blood": bool(kills) and kills[0].get("victimId") == me,
        # killerId 0 = lo mató algo que no es un campeón (torre, minions, monstruos).
        "executed_deaths": sum(1 for e in my_deaths if int(e.get("killerId", 0)) <= 0),
        "items_sold": max(0, sold - undone),
    }
    if opponent:
        stats["deaths_to_lane_opponent"] = sum(1 for e in my_deaths if e.get("killerId") == opponent)
        stats["gold_diff_15"] = _gold_diff_at(frames, me, opponent, minute=15)
    return stats


def _gold_diff_at(frames: list[dict], me: int, other: int, minute: int) -> int | None:
    """Oro total propio menos el de `other` en el frame más cercano a
    `minute` (±30 s). None si la partida no llegó a ese minuto."""
    target = minute * _MINUTE_MS
    candidates = [f for f in frames if abs(int(f.get("timestamp", 0)) - target) <= 30_000]
    if not candidates:
        return None
    frame = min(candidates, key=lambda f: abs(int(f.get("timestamp", 0)) - target))
    pframes = frame.get("participantFrames", {})
    mine, theirs = pframes.get(str(me)), pframes.get(str(other))
    if not mine or not theirs:
        return None
    return int(mine.get("totalGold", 0)) - int(theirs.get("totalGold", 0))


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


def _find_opponent(participants: list[dict], me: dict) -> dict | None:
    """Busca al rival de línea: mismo teamPosition, equipo contrario. Devuelve
    None si no hay posición (ej. ARAM) o no se encuentra (no debería pasar)."""
    position = me.get("teamPosition")
    if not position:
        return None
    return next(
        (
            p for p in participants
            if p.get("teamPosition") == position and p.get("teamId") != me.get("teamId")
        ),
        None,
    )
