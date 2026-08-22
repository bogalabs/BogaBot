"""Tests del motor de scoring y del storage en memoria. Corré con:

    python -m pytest        (o)     python -m unittest

No requieren red, token ni Discord: prueban la lógica pura.
"""
import asyncio
import pathlib
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from bogabot.core.models import MatchRecord, PlayerStats  # noqa: E402
from bogabot.scoring.engine import ScoringEngine  # noqa: E402
from bogabot.scoring.schema import MetricSpec, ScoringConfig  # noqa: E402
from bogabot.storage.memory import InMemoryStorage  # noqa: E402


def _stats(discord_id, name, games, wins, kills, deaths, assists):
    s = PlayerStats(discord_id=discord_id, display_name=name)
    for _ in range(games):
        s.add(
            MatchRecord(
                match_id=f"m{discord_id}", puuid="p", participant_id=1, discord_id=discord_id, game_name=name,
                game_creation=datetime.now(timezone.utc), game_duration_seconds=1800,
                queue_id=420, game_mode="CLASSIC", champion="Ahri", position="MIDDLE",
                win=(wins > 0), game_ended_in_surrender=False, kills=kills, deaths=deaths, assists=assists,
                damage_to_champions=20000, vision_score=20,
            )
        )
        wins -= 1
    return s


class TestScoringEngine(unittest.TestCase):
    def _engine(self):
        config = ScoringConfig(
            aggregation="per_game_average",
            normalization="zscore",
            metrics=[
                MetricSpec("win_rate", "win_rate", 5.0, True),
                MetricSpec("kda", "kda", 3.0, True),
                MetricSpec("avg_deaths", "avg_deaths", 2.0, False),
            ],
        )
        return ScoringEngine(config)

    def test_better_player_ranks_first(self):
        pro = _stats(1, "Pro", games=4, wins=4, kills=10, deaths=2, assists=8)
        troll = _stats(2, "Troll", games=4, wins=0, kills=1, deaths=12, assists=1)
        rows = self._engine().rank([pro, troll])
        self.assertEqual(rows[0].display_name, "Pro")
        self.assertEqual(rows[0].rank, 1)
        self.assertGreater(rows[0].score, rows[1].score)

    def test_empty_input(self):
        self.assertEqual(self._engine().rank([]), [])

    def test_lower_is_better_penalizes_deaths(self):
        # Mismos K/A y winrate; el que muere más debe quedar por debajo.
        a = _stats(1, "A", games=2, wins=1, kills=5, deaths=2, assists=5)
        b = _stats(2, "B", games=2, wins=1, kills=5, deaths=9, assists=5)
        rows = self._engine().rank([a, b])
        self.assertEqual(rows[0].display_name, "A")


class TestInMemoryStorage(unittest.TestCase):
    def test_match_dedup(self):
        async def scenario():
            store = InMemoryStorage()
            rec = MatchRecord(
                match_id="LA2_1", puuid="p1", participant_id=1, discord_id=1, game_name="X",
                game_creation=datetime.now(timezone.utc), game_duration_seconds=1500,
                queue_id=420, game_mode="CLASSIC", champion="Ahri", position="MIDDLE",
                win=True, game_ended_in_surrender=False, kills=1, deaths=1, assists=1,
                damage_to_champions=1, vision_score=1,
            )
            await store.save_match(rec)
            await store.save_match(rec)  # duplicado
            self.assertTrue(await store.match_exists("LA2_1", "p1"))
            self.assertFalse(await store.match_exists("LA2_1", "otro"))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
