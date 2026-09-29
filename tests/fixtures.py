"""Fábricas de datos de prueba compartidas por los tests (sin red ni tokens):
JSON de match-v5 / timeline con la forma que devuelve Riot, y MatchRecord."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bogabot.core.models import MatchRecord  # noqa: E402

POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


def participant(pid: int, **overrides) -> dict:
    """Un participante de match-v5 "promedio". pid 1-5 = azul, 6-10 = rojo."""
    team_id = 100 if pid <= 5 else 200
    data = {
        "participantId": pid,
        "puuid": f"puuid-{pid}",
        "riotIdGameName": f"Jugador{pid}",
        "summonerName": "",
        "championName": f"Champ{pid}",
        "teamId": team_id,
        "teamPosition": POSITIONS[(pid - 1) % 5],
        "win": team_id == 100,
        "gameEndedInSurrender": False,
        "gameEndedInEarlySurrender": False,
        "kills": 5,
        "deaths": 5,
        "assists": 5,
        "totalDamageDealtToChampions": 20000,
        "visionScore": 25,
        "totalMinionsKilled": 180,
        "neutralMinionsKilled": 10,
        "totalTimeSpentDead": 120,
        "visionWardsBoughtInGame": 2,
        "enemyMissingPings": 1,
    }
    data.update(overrides)
    return data


def match_json(match_id: str = "LA2_1", duration: int = 1800, queue_id: int = 420,
               game_mode: str = "CLASSIC", creation: datetime | None = None,
               overrides: dict[int, dict] | None = None) -> dict:
    overrides = overrides or {}
    creation = creation or datetime.now(timezone.utc) - timedelta(hours=1)
    parts = [participant(pid, **overrides.get(pid, {})) for pid in range(1, 11)]
    return {
        "metadata": {"matchId": match_id, "participants": [p["puuid"] for p in parts]},
        "info": {
            "gameCreation": int(creation.timestamp() * 1000),
            "gameDuration": duration,
            "queueId": queue_id,
            "gameMode": game_mode,
            "participants": parts,
        },
    }


def timeline_json(events: list[dict], gold_at_15: dict[int, int] | None = None) -> dict:
    """Timeline con un frame por minuto (0..20). `events` van todos al frame
    0 (el mapper los ordena por timestamp igual)."""
    frames = []
    for minute in range(21):
        pframes = {str(pid): {"participantId": pid, "totalGold": 500 + minute * 400} for pid in range(1, 11)}
        if minute == 15 and gold_at_15:
            for pid, gold in gold_at_15.items():
                pframes[str(pid)]["totalGold"] = gold
        frames.append({
            "timestamp": minute * 60_000 + 37,
            "participantFrames": pframes,
            "events": events if minute == 0 else [],
        })
    return {"metadata": {"matchId": "LA2_1"}, "info": {"frameInterval": 60_000, "frames": frames}}


def kill(ts_ms: int, killer: int, victim: int) -> dict:
    return {"type": "CHAMPION_KILL", "timestamp": ts_ms, "killerId": killer, "victimId": victim}


def record(**overrides) -> MatchRecord:
    """Un MatchRecord de una partida normal y sin nada troll."""
    data = dict(
        match_id="LA2_1", puuid="p1", participant_id=1, discord_id=1, game_name="Tester",
        game_creation=datetime.now(timezone.utc) - timedelta(hours=1), game_duration_seconds=1800,
        queue_id=400, game_mode="CLASSIC", champion="Ahri", position="MIDDLE",
        win=False, game_ended_in_surrender=False, kills=5, deaths=5, assists=5,
        damage_to_champions=20000, vision_score=25, opponent_champion="Zed", cs=200,
        time_dead_seconds=150, control_wards_bought=2, question_pings=0,
        team_kills=25, team_deaths=25, enemy_kills=25, team_damage=100000,
        first_death_minute=8, deaths_before_10=1, gave_first_blood=False, executed_deaths=0,
        items_sold=1, deaths_to_lane_opponent=1, gold_diff_15=0,
    )
    data.update(overrides)
    return MatchRecord(**data)
