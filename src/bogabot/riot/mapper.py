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
            team_id=int(team_id or 0),
            teammates={int(p.get("participantId", 0)) for p in team},
        )
    return record


def _sum(participants: list[dict], key: str) -> int:
    return sum(int(p.get(key, 0)) for p in participants)


def _opt_int(participant: dict, key: str) -> int | None:
    value = participant.get(key)
    return None if value is None else int(value)


# --- Timeline (match-v5 /timeline) -------------------------------------------
_MINUTE_MS = 60_000
# Versión del análisis de timeline. Subirla cuando se agregan datos nuevos:
# `/trolls-recalcular` completa los registros con una versión anterior.
TIMELINE_VERSION = 2
# Posición aproximada de cada nexo en la Grieta (coordenadas del mapa).
_NEXUS_POSITION = {100: (1550, 1660), 200: (13200, 13200)}
# Más lejos que esto del propio nexo = "no estaba defendiendo la base".
_BASE_ABSENT_DISTANCE = 7000
# Estructuras de la base (si caen y no estás, es "nos tiraban la base").
_BASE_TOWERS = {"NEXUS_TURRET", "BASE_TURRET"}
# Si morís primero y en este lapso pierden un objetivo grande, es un "throw".
_THROW_WINDOW_MS = 60_000
_THROW_OBJECTIVES = {"BARON_NASHOR": "el Barón", "ELDER_DRAGON": "el Dragón Ancestral"}
# Oro sin gastar encima al morir para considerarlo "ahorrista".
_RICH_DEATH_GOLD = 3000
# AFK: se movió menos que esto entre dos frames y no ganó experiencia.
_AFK_MAX_MOVE = 150
# Ventana en la que se considera que el jugador sigue muerto (aprox.).
_DEAD_WINDOW_MS = 45_000


def _apply_timeline(record: MatchRecord, timeline: dict, opponent_participant_id: int | None,
                    team_id: int = 0, teammates: set[int] | None = None) -> None:
    """Completa en `record` los datos que salen del timeline. Si el JSON viene
    raro, deja todos esos campos en None en vez de romper la ingesta."""
    try:
        stats = _timeline_stats(timeline, record.participant_id, opponent_participant_id,
                                team_id, teammates or {record.participant_id})
    except (AttributeError, TypeError, ValueError, KeyError):
        log.warning("Timeline ilegible para %s; sigo sin esos datos.", record.match_id)
        return
    for name, value in stats.items():
        setattr(record, name, value)


def _timeline_stats(timeline: dict, me: int, opponent: int | None,
                    team_id: int, teammates: set[int]) -> dict:
    frames = sorted(timeline["info"]["frames"], key=lambda f: int(f.get("timestamp", 0)))
    events = sorted(
        (e for f in frames for e in f.get("events", [])),
        key=lambda e: int(e.get("timestamp", 0)),
    )
    kills = [e for e in events if e.get("type") == "CHAMPION_KILL"]
    my_deaths = [e for e in kills if e.get("victimId") == me]
    death_times = [int(e.get("timestamp", 0)) for e in my_deaths]

    sold = sum(1 for e in events if e.get("type") == "ITEM_SOLD" and e.get("participantId") == me)
    # Un "deshacer" de una venta (beforeId 0 -> afterId item) la anula.
    undone = sum(
        1 for e in events
        if e.get("type") == "ITEM_UNDO" and e.get("participantId") == me
        and not e.get("beforeId") and e.get("afterId")
    )
    stats = {
        "first_death_minute": death_times[0] // _MINUTE_MS if death_times else None,
        "deaths_before_10": sum(1 for t in death_times if t < 10 * _MINUTE_MS),
        "gave_first_blood": bool(kills) and kills[0].get("victimId") == me,
        # killerId 0 = lo mató algo que no es un campeón (torre, minions, monstruos).
        "executed_deaths": sum(1 for e in my_deaths if int(e.get("killerId", 0)) <= 0),
        "items_sold": max(0, sold - undone),
        "timeline_version": TIMELINE_VERSION,
    }
    if opponent:
        stats["deaths_to_lane_opponent"] = sum(1 for e in my_deaths if e.get("killerId") == opponent)
        stats["gold_diff_15"] = _gold_diff_at(frames, me, opponent, minute=15)

    stats.update(_base_absence(frames, events, me, team_id, death_times))
    stats.update(_throws(events, kills, me, team_id, teammates))
    stats.update(_rich_deaths(frames, death_times, me))
    stats["afk_minutes"] = _afk_minutes(frames, me, death_times)
    return stats


def _pframe(frame: dict | None, pid: int) -> dict | None:
    return (frame or {}).get("participantFrames", {}).get(str(pid))


def _frame_near(frames: list[dict], t: int, max_gap: int = _MINUTE_MS) -> dict | None:
    if not frames:
        return None
    frame = min(frames, key=lambda f: abs(int(f.get("timestamp", 0)) - t))
    return frame if abs(int(frame.get("timestamp", 0)) - t) <= max_gap else None


def _frame_before(frames: list[dict], t: int) -> dict | None:
    before = [f for f in frames if int(f.get("timestamp", 0)) <= t]
    return before[-1] if before else None


def _dead_at(death_times: list[int], t: int) -> bool:
    return any(0 <= t - d <= _DEAD_WINDOW_MS for d in death_times)


def _base_losses(events: list[dict], team_id: int) -> list[int]:
    """Momentos en que el equipo perdió una estructura de su base
    (torres del nexo/de inhibidor, inhibidores y el nexo mismo)."""
    times = []
    for e in events:
        if e.get("type") == "BUILDING_KILL" and e.get("teamId") == team_id and (
            e.get("buildingType") == "INHIBITOR_BUILDING" or e.get("towerType") in _BASE_TOWERS
        ):
            times.append(int(e.get("timestamp", 0)))
        elif e.get("type") == "GAME_END" and e.get("winningTeam") not in (None, team_id):
            times.append(int(e.get("timestamp", 0)))
    return times


def _base_absence(frames: list[dict], events: list[dict], me: int, team_id: int,
                  death_times: list[int]) -> dict:
    """Cuántas estructuras de la base cayeron mientras el jugador estaba vivo
    y lejos, y si en ese momento estaba farmeando (jungla o línea)."""
    nexus = _NEXUS_POSITION.get(team_id)
    if nexus is None:
        return {}
    absent, farming = 0, None
    for t in _base_losses(events, team_id):
        if _dead_at(death_times, t):
            continue
        pos = (_pframe(_frame_near(frames, t), me) or {}).get("position")
        if not pos:
            continue
        distance = ((pos.get("x", 0) - nexus[0]) ** 2 + (pos.get("y", 0) - nexus[1]) ** 2) ** 0.5
        if distance <= _BASE_ABSENT_DISTANCE:
            continue
        absent += 1
        if farming is None:
            farming = _farming_between(frames, me, t - 90_000, t + 30_000)
    return {"base_absent": absent, "base_absent_farming": farming}


def _farming_between(frames: list[dict], me: int, start: int, end: int) -> str | None:
    before, after = _pframe(_frame_before(frames, start), me), _pframe(_frame_near(frames, end), me)
    if not before or not after:
        return None
    if int(after.get("jungleMinionsKilled", 0)) - int(before.get("jungleMinionsKilled", 0)) >= 2:
        return "jungla"
    if int(after.get("minionsKilled", 0)) - int(before.get("minionsKilled", 0)) >= 3:
        return "línea"
    return None


def _throws(events: list[dict], kills: list[dict], me: int, team_id: int,
            teammates: set[int]) -> dict:
    """Veces que el jugador fue el PRIMERO de su equipo en morir justo antes
    de que perdieran un objetivo grande (Barón, Ancestral o el nexo)."""
    objectives: list[tuple[int, str]] = []
    for e in events:
        if e.get("type") == "ELITE_MONSTER_KILL" and e.get("killerTeamId") not in (None, team_id):
            name = _THROW_OBJECTIVES.get(e.get("monsterSubType")) or _THROW_OBJECTIVES.get(e.get("monsterType"))
            if name:
                objectives.append((int(e.get("timestamp", 0)), name))
        elif e.get("type") == "GAME_END" and e.get("winningTeam") not in (None, team_id):
            objectives.append((int(e.get("timestamp", 0)), "el nexo"))
    count, first = 0, None
    for t, name in objectives:
        team_deaths = [e for e in kills if e.get("victimId") in teammates
                       and 0 <= t - int(e.get("timestamp", 0)) <= _THROW_WINDOW_MS]
        if team_deaths and team_deaths[0].get("victimId") == me:
            count += 1
            first = first or name
    return {"throw_deaths": count, "throw_objective": first}


def _rich_deaths(frames: list[dict], death_times: list[int], me: int) -> dict:
    golds = [int((_pframe(_frame_before(frames, t), me) or {}).get("currentGold", 0)) for t in death_times]
    rich = [g for g in golds if g >= _RICH_DEATH_GOLD]
    return {"rich_deaths": len(rich), "max_gold_on_death": max(golds, default=0)}


def _afk_minutes(frames: list[dict], me: int, death_times: list[int]) -> int:
    """Racha más larga de minutos en que el jugador, vivo, no se movió ni
    ganó experiencia. Se saltean los 2 primeros minutos (compras iniciales)."""
    best = streak = 0
    for prev, cur in zip(frames[2:], frames[3:]):
        a, b = _pframe(prev, me), _pframe(cur, me)
        t0, t1 = int(prev.get("timestamp", 0)), int(cur.get("timestamp", 0))
        if not a or not b or not a.get("position") or not b.get("position"):
            streak = 0
            continue
        dead = any(t0 - _MINUTE_MS <= d <= t1 for d in death_times)
        dx = a["position"].get("x", 0) - b["position"].get("x", 0)
        dy = a["position"].get("y", 0) - b["position"].get("y", 0)
        idle = (dx * dx + dy * dy) ** 0.5 < _AFK_MAX_MOVE and int(b.get("xp", 0)) == int(a.get("xp", 0))
        streak = streak + 1 if idle and not dead else 0
        best = max(best, streak)
    return best


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
