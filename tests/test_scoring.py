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


class TestCarryTrollDetection(unittest.TestCase):
    def _match(self, **overrides):
        defaults = dict(
            match_id="m1", puuid="p1", participant_id=1, discord_id=1, game_name="Test",
            game_creation=datetime.now(timezone.utc), game_duration_seconds=1800,
            queue_id=420, game_mode="CLASSIC", champion="Ahri", position="MIDDLE",
            win=True, game_ended_in_surrender=False, kills=10, deaths=2, assists=5,
            damage_to_champions=20000, vision_score=20,
        )
        defaults.update(overrides)
        return MatchRecord(**defaults)

    def test_carry_game_win_high_kda(self):
        # KDA = (10+5)/2 = 7.5 >= 5 y win=True → carry
        m = self._match(win=True, kills=10, deaths=2, assists=5)
        self.assertTrue(m.is_carry_game())

    def test_carry_game_requires_win(self):
        # KDA alto pero perdió → no es carry
        m = self._match(win=False, kills=10, deaths=2, assists=5)
        self.assertFalse(m.is_carry_game())

    def test_carry_game_low_kda_not_carry(self):
        # Ganó pero KDA = (3+2)/4 = 1.25 < 5 → no es carry
        m = self._match(win=True, kills=3, deaths=4, assists=2)
        self.assertFalse(m.is_carry_game())

    def test_carry_game_zero_deaths(self):
        # KDA = (5+3)/1 = 8 >= 5, win=True → carry (0 muertes usa max(0,1)=1)
        m = self._match(win=True, kills=5, deaths=0, assists=3)
        self.assertTrue(m.is_carry_game())

    def test_troll_game(self):
        # KDA = (0+0)/10 = 0 < 0.5 → troll (sin importar win/loss/FF)
        m = self._match(win=False, kills=0, deaths=10, assists=0)
        self.assertTrue(m.is_troll_game())

    def test_troll_game_even_if_won(self):
        # KDA pésimo pero ganó (lo carreo el equipo) → sigue siendo troll
        m = self._match(win=True, kills=0, deaths=10, assists=0)
        self.assertTrue(m.is_troll_game())

    def test_troll_game_long_game(self):
        # KDA malo en partida larga sin FF → sigue siendo troll
        m = self._match(win=False, kills=0, deaths=10, assists=0,
                        game_ended_in_surrender=False, game_duration_seconds=2400)
        self.assertTrue(m.is_troll_game())

    def test_not_troll_if_decent_kda(self):
        # KDA = (2+3)/6 = 0.83 >= 0.5 → no es troll
        m = self._match(win=False, kills=2, deaths=6, assists=3)
        self.assertFalse(m.is_troll_game())


class TestCarryTrollScoring(unittest.TestCase):
    def test_carrier_ranks_above_mediocre(self):
        """El que carreo partidas debe rankear arriba de alguien con muchas wins pero mediocres."""
        config = ScoringConfig(
            aggregation="per_game_average",
            normalization="zscore",
            metrics=[
                MetricSpec("carry_rate", "carry_rate", 5.0, True),
                MetricSpec("troll_rate", "troll_rate", 4.0, False),
                MetricSpec("kda", "kda", 3.0, True),
                MetricSpec("win_rate", "win_rate", 2.0, True),
            ],
        )
        engine = ScoringEngine(config)

        # Carrier: 3 partidas, 3 wins con KDA alto (carry)
        carrier = PlayerStats(discord_id=1, display_name="Carrier")
        for _ in range(3):
            carrier.add(MatchRecord(
                match_id="c", puuid="p", participant_id=1, discord_id=1, game_name="Carrier",
                game_creation=datetime.now(timezone.utc), game_duration_seconds=1800,
                queue_id=420, game_mode="CLASSIC", champion="Vayne", position="BOTTOM",
                win=True, game_ended_in_surrender=False, kills=15, deaths=2, assists=8,
                damage_to_champions=30000, vision_score=25,
            ))

        # Mediocre: 6 partidas, 4 wins pero KDA bajo (no carry)
        mediocre = PlayerStats(discord_id=2, display_name="Mediocre")
        for i in range(6):
            mediocre.add(MatchRecord(
                match_id="m", puuid="p", participant_id=1, discord_id=2, game_name="Mediocre",
                game_creation=datetime.now(timezone.utc), game_duration_seconds=1800,
                queue_id=420, game_mode="CLASSIC", champion="Garen", position="TOP",
                win=(i < 4), game_ended_in_surrender=False, kills=4, deaths=4, assists=3,
                damage_to_champions=15000, vision_score=15,
            ))

        rows = engine.rank([carrier, mediocre])
        self.assertEqual(rows[0].display_name, "Carrier")
        self.assertGreater(rows[0].score, rows[1].score)

    def test_player_stats_counts_carry_troll(self):
        """PlayerStats acumula correctamente carry_games y troll_games."""
        stats = PlayerStats(discord_id=1, display_name="Test")

        # Carry: win + KDA 7.5
        stats.add(MatchRecord(
            match_id="c1", puuid="p", participant_id=1, discord_id=1, game_name="Test",
            game_creation=datetime.now(timezone.utc), game_duration_seconds=1800,
            queue_id=420, game_mode="CLASSIC", champion="A", position="MID",
            win=True, game_ended_in_surrender=False, kills=10, deaths=2, assists=5,
            damage_to_champions=20000, vision_score=20,
        ))
        # Troll: loss + KDA 0 + FF < 20
        stats.add(MatchRecord(
            match_id="t1", puuid="p", participant_id=1, discord_id=1, game_name="Test",
            game_creation=datetime.now(timezone.utc), game_duration_seconds=900,
            queue_id=420, game_mode="CLASSIC", champion="B", position="MID",
            win=False, game_ended_in_surrender=True, kills=0, deaths=10, assists=0,
            damage_to_champions=5000, vision_score=5,
        ))
        # Normal: win pero KDA bajo (no carry)
        stats.add(MatchRecord(
            match_id="n1", puuid="p", participant_id=1, discord_id=1, game_name="Test",
            game_creation=datetime.now(timezone.utc), game_duration_seconds=1800,
            queue_id=420, game_mode="CLASSIC", champion="C", position="MID",
            win=True, game_ended_in_surrender=False, kills=3, deaths=3, assists=2,
            damage_to_champions=15000, vision_score=15,
        ))

        self.assertEqual(stats.carry_games, 1)
        self.assertEqual(stats.troll_games, 1)
        self.assertEqual(stats.games, 3)


if __name__ == "__main__":
    unittest.main()
