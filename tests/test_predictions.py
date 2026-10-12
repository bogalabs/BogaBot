"""Tests de la lógica pura de predicciones (sin Discord ni Riot)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bogabot.modules.predictions.logic import BLUE, RED, Bet, Prediction  # noqa: E402

NOW = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)


def _prediction(players: dict[int, int]) -> Prediction:
    return Prediction.build(game_id=1, match_id="LA2_1", queue_id=440, players=players,
                            opened_at=NOW, close_at=NOW + timedelta(minutes=8))


class TestPrediction(unittest.TestCase):
    def test_same_team_is_win_or_lose(self):
        p = _prediction({1: RED, 2: RED})
        self.assertFalse(p.split)
        self.assertEqual(p.option_teams, {"a": RED, "b": BLUE})
        self.assertEqual(p.winning_option({1: True}), "a")
        self.assertEqual(p.winning_option({2: False}), "b")

    def test_split_group_is_blue_vs_red(self):
        p = _prediction({1: BLUE, 2: RED})
        self.assertTrue(p.split)
        self.assertEqual(p.winning_option({2: True}), "b")  # ganó el rojo
        self.assertEqual(p.winning_option({1: True}), "a")

    def test_winning_option_without_known_players(self):
        self.assertIsNone(_prediction({1: BLUE, 2: BLUE}).winning_option({99: True}))

    def test_bet_rules(self):
        p = _prediction({1: BLUE, 2: BLUE})
        self.assertIsNone(p.bet_error(50, "b", NOW))
        self.assertIn("tu equipo", p.bet_error(1, "b", NOW))  # jugador apostando en contra
        self.assertIsNone(p.bet_error(1, "a", NOW))
        p.bets[50] = Bet("a", 10)
        self.assertIn("Ya apostaste", p.bet_error(50, "a", NOW))
        self.assertIn("cerraron", p.bet_error(60, "a", NOW + timedelta(minutes=9)))
        p.closed = True
        self.assertIn("cerraron", p.bet_error(60, "a", NOW))

    def test_payouts_split_pool_proportionally(self):
        p = _prediction({1: BLUE, 2: BLUE})
        p.bets = {10: Bet("a", 100), 11: Bet("a", 50), 12: Bet("b", 150)}
        self.assertEqual(p.payouts("a"), {10: 200, 11: 100})
        self.assertEqual(p.payouts("b"), {12: 300})

    def test_payouts_nobody_right_or_everyone_right(self):
        p = _prediction({1: BLUE, 2: BLUE})
        p.bets = {10: Bet("a", 30)}
        self.assertEqual(p.payouts("b"), {})
        self.assertEqual(p.payouts("a"), {10: 30})  # todos del mismo lado: recupera lo suyo

    def test_roundtrip(self):
        p = _prediction({1: BLUE, 2: RED})
        p.bets = {10: Bet("b", 25)}
        p.channel_id, p.message_id, p.closed = 5, 6, True
        again = Prediction.from_dict(p.to_dict())
        self.assertEqual(again, p)


if __name__ == "__main__":
    unittest.main()
