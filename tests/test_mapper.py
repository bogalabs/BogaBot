"""Tests del mapper de match-v5 (partida + timeline) con JSON sintético."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import kill, match_json, timeline_json  # noqa: E402

from bogabot.core.models import MatchRecord  # noqa: E402
from bogabot.riot.mapper import is_remake, map_match  # noqa: E402
from bogabot.storage.discord_channel import MATCH_PREFIX  # noqa: E402


def _troll_match() -> dict:
    """El azul pierde; el mid azul (participante 3) va 1/11/2."""
    overrides = {pid: {"win": pid > 5} for pid in range(1, 11)}
    overrides[3].update(kills=1, deaths=11, assists=2, totalDamageDealtToChampions=5000,
                        totalTimeSpentDead=600, visionWardsBoughtInGame=0, enemyMissingPings=22)
    return match_json(overrides=overrides)


def _troll_timeline() -> dict:
    events = [
        kill(90_000, killer=8, victim=3),  # primera sangre, a manos del rival de línea
        kill(300_000, killer=8, victim=3),
        kill(420_000, killer=0, victim=3),  # ejecutado (torre/minions)
        kill(500_000, killer=7, victim=3),
        kill(700_000, killer=8, victim=3),
        kill(800_000, killer=3, victim=8),
        {"type": "ITEM_SOLD", "timestamp": 1000, "participantId": 4, "itemId": 1055},
    ]
    events += [{"type": "ITEM_SOLD", "timestamp": 900_000 + i, "participantId": 3, "itemId": 3000 + i}
               for i in range(7)]
    events.append({"type": "ITEM_UNDO", "timestamp": 900_100, "participantId": 3, "beforeId": 0, "afterId": 3006})
    return timeline_json(events, gold_at_15={3: 4000, 8: 7000})


class TestMapMatch(unittest.TestCase):
    def test_extended_stats_and_team_context(self):
        r = map_match(_troll_match(), "puuid-3", discord_id=33)
        self.assertIsNotNone(r)
        self.assertEqual((r.kills, r.deaths, r.assists), (1, 11, 2))
        self.assertEqual(r.participant_id, 3)
        self.assertEqual(r.opponent_champion, "Champ8")
        self.assertFalse(r.win)
        self.assertEqual(r.team_kills, 21)  # 4 × 5 + 1
        self.assertEqual(r.team_deaths, 31)  # 4 × 5 + 11
        self.assertEqual(r.enemy_kills, 25)
        self.assertEqual(r.team_damage, 85000)
        self.assertEqual(r.time_dead_seconds, 600)
        self.assertEqual(r.control_wards_bought, 0)
        self.assertEqual(r.question_pings, 22)
        self.assertIsNone(r.placement)
        # Sin timeline: esos campos quedan en None.
        self.assertIsNone(r.deaths_before_10)
        self.assertFalse(r.has_timeline)
        self.assertTrue(r.has_extended_stats)

    def test_timeline_stats(self):
        r = map_match(_troll_match(), "puuid-3", discord_id=33, timeline=_troll_timeline())
        self.assertTrue(r.has_timeline)
        self.assertTrue(r.gave_first_blood)
        self.assertEqual(r.first_death_minute, 1)
        self.assertEqual(r.deaths_before_10, 4)
        self.assertEqual(r.executed_deaths, 1)
        self.assertEqual(r.deaths_to_lane_opponent, 3)
        self.assertEqual(r.items_sold, 6)  # 7 ventas - 1 deshecha; la del participante 4 no cuenta
        self.assertEqual(r.gold_diff_15, -3000)

    def test_other_player_did_not_give_first_blood(self):
        r = map_match(_troll_match(), "puuid-8", discord_id=88, timeline=_troll_timeline())
        self.assertFalse(r.gave_first_blood)
        self.assertEqual(r.deaths_before_10, 0)
        self.assertEqual(r.gold_diff_15, 3000)
        self.assertEqual(r.first_death_minute, 13)

    def test_short_game_has_no_gold_diff_at_15(self):
        timeline = _troll_timeline()
        timeline["info"]["frames"] = timeline["info"]["frames"][:12]
        r = map_match(_troll_match(), "puuid-3", discord_id=33, timeline=timeline)
        self.assertIsNone(r.gold_diff_15)

    def test_broken_timeline_does_not_break_mapping(self):
        r = map_match(_troll_match(), "puuid-3", discord_id=33, timeline={"info": {"frames": "???"}})
        self.assertIsNotNone(r)
        self.assertIsNone(r.deaths_before_10)

    def test_remakes_are_discarded(self):
        self.assertIsNone(map_match(match_json(duration=200), "puuid-1", 1))
        self.assertTrue(is_remake(match_json(duration=200)))
        early = match_json(overrides={pid: {"gameEndedInEarlySurrender": True} for pid in range(1, 11)})
        self.assertIsNone(map_match(early, "puuid-1", 1))
        self.assertTrue(is_remake(early))
        self.assertFalse(is_remake(match_json()))

    def test_participant_id_fallback_for_stale_puuid(self):
        r = map_match(_troll_match(), "puuid-de-otra-key", discord_id=33, participant_id=3)
        self.assertIsNotNone(r)
        self.assertEqual(r.kills, 1)
        self.assertIsNone(map_match(_troll_match(), "puuid-de-otra-key", discord_id=33))

    def test_missing_optional_fields_stay_none(self):
        data = match_json()
        for key in ("totalTimeSpentDead", "visionWardsBoughtInGame", "enemyMissingPings"):
            del data["info"]["participants"][0][key]
        r = map_match(data, "puuid-1", 1)
        self.assertIsNone(r.time_dead_seconds)
        self.assertIsNone(r.control_wards_bought)  # no dispara "ni un control ward" en falso
        self.assertIsNone(r.question_pings)

    def test_arena_placement(self):
        data = match_json(game_mode="CHERRY", queue_id=1700, overrides={1: {"placement": 8}})
        self.assertEqual(map_match(data, "puuid-1", 1).placement, 8)


class TestRecordSerialization(unittest.TestCase):
    def test_roundtrip_with_timeline(self):
        r = map_match(_troll_match(), "puuid-3", discord_id=33, timeline=_troll_timeline())
        self.assertEqual(MatchRecord.from_dict(json.loads(json.dumps(r.to_dict()))), r)

    def test_fits_in_a_discord_message(self):
        # Peor caso razonable: nombres largos y todos los campos con valor.
        overrides = {3: {"riotIdGameName": "N" * 16, "championName": "Fiddlesticks", "placement": 8}}
        r = map_match(match_json(overrides=overrides), "puuid-3", 33, timeline=_troll_timeline())
        r.puuid = "x" * 78
        r.match_id = "LA2_" + "9" * 12
        payload = MATCH_PREFIX + json.dumps(r.to_dict(), ensure_ascii=False)
        self.assertLess(len(payload), 2000)

    def test_legacy_record_without_new_fields(self):
        legacy = {
            "match_id": "LA2_9", "puuid": "p", "participant_id": 1, "discord_id": 1, "game_name": "X",
            "game_creation": "2026-08-22T01:00:00+00:00", "game_duration_seconds": 1500, "queue_id": 440,
            "game_mode": "CLASSIC", "champion": "Ahri", "position": "MIDDLE", "win": False,
            "game_ended_in_surrender": True, "kills": 0, "deaths": 9, "assists": 1,
            "damage_to_champions": 4000, "vision_score": 5, "cs": 90,
        }
        r = MatchRecord.from_dict(legacy)
        self.assertIsNone(r.team_kills)
        self.assertFalse(r.has_extended_stats)
        self.assertIsNone(r.kill_participation)
        # Al volver a serializar no aparecen claves nuevas vacías.
        self.assertEqual(set(r.to_dict()), set(legacy) | {"opponent_champion"})


if __name__ == "__main__":
    unittest.main()
