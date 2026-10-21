"""Tests de la lógica pura de apuestas: mercados, props de la partida,
duelos, recompensas, semanales, /daily y misiones. Sin Discord ni Riot."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import record  # noqa: E402

from bogabot.modules.bets.logic import (Bounty, Duel, WeeklyBook, bounty_market, build_weekly,  # noqa: E402
                                        weekly_winner)
from bogabot.modules.missions.logic import CATALOG, pick_week  # noqa: E402
from bogabot.modules.missions.service import MissionsService  # noqa: E402
from bogabot.modules.points.store import PointsStore  # noqa: E402
from bogabot.modules.predictions.logic import BLUE, RED, Prediction  # noqa: E402
from bogabot.modules.predictions.market import Bet, Market  # noqa: E402
from bogabot.modules.predictions.props import GameFacts, build_props, resolve_prop  # noqa: E402

NOW = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc)
NAMES = {1: "Uno", 2: "Dos"}


class TestMarket(unittest.TestCase):
    def test_forbidden_options(self):
        m = Market(key="k", title="t", options={"yes": "Sí", "no": "No"}, forbidden={1: ["yes"], 2: ["yes", "no"]})
        self.assertIsNone(m.bet_error(1, "no"))
        self.assertIn("no la podés", m.bet_error(1, "yes"))
        self.assertIn("no podés apostar", m.bet_error(2, "no"))
        self.assertIn("inválida", m.bet_error(3, "x"))
        m.bets[3] = Bet("yes", 10)
        self.assertIn("Ya apostaste", m.bet_error(3, "no"))

    def test_parimutuel_and_roundtrip(self):
        m = Market(key="k", title="t", options={"yes": "Sí", "no": "No"}, forbidden={1: ["yes"]})
        m.bets = {10: Bet("yes", 100), 11: Bet("no", 50), 12: Bet("no", 50)}
        self.assertEqual(m.payouts("no"), {11: 100, 12: 100})
        self.assertEqual(m.payouts("yes"), {10: 200})
        self.assertEqual(m.refunds(), {10: 100, 11: 50, 12: 50})
        self.assertEqual(Market.from_dict(m.to_dict()), m)


class TestProps(unittest.TestCase):
    def test_build_same_team(self):
        markets = build_props({1: BLUE, 2: BLUE}, NAMES, 420)
        self.assertEqual(set(markets), {"carry", "troll:1", "troll:2", "deaths:1", "deaths:2", "duration", "fb"})
        self.assertEqual(markets["troll:1"].forbidden, {1: ["yes"]})
        self.assertEqual(markets["deaths:2"].forbidden, {2: ["over"]})
        self.assertIn("none", markets["carry"].forbidden[1])
        self.assertIsNotNone(markets["duration"].bet_error(1, "over"))  # los que juegan no apuestan duración
        self.assertIsNone(markets["duration"].bet_error(99, "over"))

    def test_build_split_and_queues(self):
        self.assertNotIn("fb", build_props({1: BLUE, 2: RED}, NAMES, 420))
        arena = build_props({1: BLUE, 2: BLUE}, NAMES, 1700)
        self.assertEqual(set(arena), {"carry", "troll:1", "troll:2"})
        aram = build_props({1: BLUE, 2: BLUE}, NAMES, 450)
        self.assertIn("Más de 10", aram["deaths:1"].options["over"])

    def test_resolve(self):
        markets = build_props({1: BLUE, 2: BLUE}, NAMES, 420)
        facts = GameFacts(duration_minutes=31.5, deaths={1: 8, 2: 7}, trolled={1: True, 2: False},
                          carry_points={2: 12}, group_first_blood=False)
        self.assertEqual(resolve_prop(markets["carry"], facts), "2")
        self.assertEqual(resolve_prop(markets["troll:1"], facts), "yes")
        self.assertEqual(resolve_prop(markets["troll:2"], facts), "no")
        self.assertEqual(resolve_prop(markets["deaths:1"], facts), "over")
        self.assertEqual(resolve_prop(markets["deaths:2"], facts), "under")  # 7 no es "más de 7"
        self.assertEqual(resolve_prop(markets["duration"], facts), "over")
        self.assertEqual(resolve_prop(markets["fb"], facts), "no")

    def test_resolve_missing_data(self):
        markets = build_props({1: BLUE, 2: BLUE}, NAMES, 420)
        facts = GameFacts(duration_minutes=20, deaths={1: 2}, trolled={1: False})
        self.assertEqual(resolve_prop(markets["carry"], facts), "none")
        self.assertIsNone(resolve_prop(markets["troll:2"], facts))  # sin registro de 2: se devuelve
        self.assertIsNone(resolve_prop(markets["fb"], facts))
        self.assertIsNone(resolve_prop(markets["carry"], GameFacts()))

    def test_prediction_roundtrip_with_markets(self):
        p = Prediction.build(game_id=1, match_id="LA2_1", queue_id=420, players={1: BLUE, 2: BLUE},
                             opened_at=NOW, close_at=NOW + timedelta(minutes=8))
        p.markets = build_props(p.players, NAMES, 420)
        p.markets["carry"].bets[5] = Bet("1", 20)
        p.props_message_id = 7
        self.assertEqual(Prediction.from_dict(p.to_dict()), p)
        old = p.to_dict()
        del old["markets"], old["props_message_id"]  # predicciones guardadas antes de las props
        self.assertEqual(Prediction.from_dict(old).markets, {})


class TestDuel(unittest.TestCase):
    def _duel(self, mode: str = "kda") -> Duel:
        return Duel(id="ab", challenger=1, opponent=2, amount=50, mode=mode, created_at=NOW, accepted_at=NOW)

    def test_find_game_needs_both_after_accept(self):
        d = self._duel()
        old = [record(match_id="LA2_0", discord_id=1, game_creation=NOW - timedelta(hours=1)),
               record(match_id="LA2_0", discord_id=2, game_creation=NOW - timedelta(hours=1))]
        solo = [record(match_id="LA2_1", discord_id=1, game_creation=NOW + timedelta(minutes=5))]
        both = [record(match_id="LA2_2", discord_id=1, game_creation=NOW + timedelta(hours=1), kills=10),
                record(match_id="LA2_2", discord_id=2, game_creation=NOW + timedelta(hours=1), kills=1)]
        self.assertIsNone(d.find_game(old + solo))
        ch, op = d.find_game(old + solo + both)
        self.assertEqual((ch.match_id, op.discord_id), ("LA2_2", 2))
        self.assertEqual(d.winner(lambda r: (r.kda,), ch, op), 1)
        self.assertIsNone(d.winner(lambda r: (0,), ch, op))  # empate
        self.assertIsNone(Duel(id="x", challenger=1, opponent=2, amount=1, mode="kda",
                               created_at=NOW).find_game(both))  # sin aceptar

    def test_roundtrip(self):
        d = self._duel()
        self.assertEqual(Duel.from_dict(d.to_dict()), d)


class TestBounty(unittest.TestCase):
    def test_market_forbids_the_target(self):
        troll = bounty_market(1, "Uno", "troll")
        self.assertIsNotNone(troll.bet_error(1, "yes"))
        self.assertIsNone(troll.bet_error(1, "no"))
        carry = bounty_market(1, "Uno", "carry")
        self.assertIsNotNone(carry.bet_error(1, "no"))

    def test_accepts(self):
        b = Bounty(id="cd", target=1, kind="troll", creator=2, created_at=NOW, market=bounty_market(1, "Uno", "troll"))
        self.assertFalse(b.accepts(record(discord_id=1, game_creation=NOW - timedelta(minutes=10))))
        self.assertTrue(b.accepts(record(discord_id=1, game_creation=NOW + timedelta(minutes=10))))
        self.assertFalse(b.accepts(record(discord_id=2, game_creation=NOW + timedelta(minutes=10))))
        b.match_id = "LA2_9"
        self.assertFalse(b.is_open)
        self.assertFalse(b.accepts(record(match_id="LA2_8", discord_id=1, game_creation=NOW + timedelta(hours=1))))
        self.assertTrue(b.accepts(record(match_id="LA2_9", discord_id=1, game_creation=NOW - timedelta(hours=1))))
        b.market.bets[2] = Bet("yes", 30)
        self.assertEqual(Bounty.from_dict(b.to_dict()), b)


class TestWeekly(unittest.TestCase):
    def test_build_and_winner(self):
        markets = build_weekly({1: "Uno", 2: "Dos"})
        self.assertIsNotNone(markets["troll"].bet_error(1, "1"))  # no apostás a ser el troll
        self.assertIsNone(markets["troll"].bet_error(1, "2"))
        self.assertIsNone(markets["carry"].bet_error(1, "1"))
        self.assertEqual(weekly_winner([(2, 30), (1, 10)]), "2")
        self.assertIsNone(weekly_winner([(2, 0)]))
        self.assertIsNone(weekly_winner([]))

    def test_roundtrip(self):
        w = WeeklyBook(week="2026-10-19", close_at=NOW, markets=build_weekly({1: "Uno", 2: "Dos"}))
        w.markets["carry"].bets[1] = Bet("2", 15)
        self.assertEqual(WeeklyBook.from_dict(w.to_dict()), w)


class TestDaily(unittest.TestCase):
    def test_streak(self):
        store = PointsStore(Path(tempfile.mkdtemp()) / "points.json")
        claim = lambda day, prev: asyncio.run(store.claim_daily(1, day, prev, base=10, step=5, cap=20))  # noqa: E731
        self.assertEqual(claim("2026-10-20", "2026-10-19"), (10, 1))
        self.assertIsNone(claim("2026-10-20", "2026-10-19"))  # ya reclamó hoy
        self.assertEqual(claim("2026-10-21", "2026-10-20"), (15, 2))
        self.assertEqual(claim("2026-10-22", "2026-10-21"), (20, 3))
        self.assertEqual(claim("2026-10-23", "2026-10-22"), (20, 4))  # tope
        self.assertEqual(claim("2026-10-25", "2026-10-24"), (10, 1))  # se cortó la racha
        self.assertEqual(store.balance(1), 75)


class TestMissions(unittest.TestCase):
    def test_pick_is_deterministic(self):
        self.assertEqual(pick_week("2026-10-19", 4), pick_week("2026-10-19", 4))
        self.assertEqual(len(pick_week("2026-10-19", 99)), len(CATALOG))
        self.assertEqual(len({m.code for m in pick_week("2026-10-19", 4)}), 4)

    def test_progress_pays_once(self):
        tmp = Path(tempfile.mkdtemp())
        points = PointsStore(tmp / "points.json")
        service = MissionsService(tmp / "missions.json", points, enabled=True, timezone="UTC",
                                  per_week=len(CATALOG), reward=50)
        done: list[tuple[int, str]] = []

        async def on_complete(uid, mission, reward):
            done.append((uid, mission.code))

        service.set_on_complete(on_complete)

        async def run():
            await service.progress([1, 2], "games", 2)
            await service.progress([1], "games")
            await service.progress([1], "games")  # ya completada: no paga de nuevo
            await service.progress([1], "voice", 299)
        asyncio.run(run())
        self.assertEqual(done, [(1, "games")])
        self.assertEqual(points.balance(1), 50)
        status = {m.code: (v, d) for m, v, d in service.status(1)}
        self.assertEqual(status["games"], (3, True))
        self.assertEqual(status["voice"], (299, False))
        self.assertEqual(CATALOG["voice"].progress_text(299), "4/5")
        # Persiste
        again = MissionsService(tmp / "missions.json", points, enabled=True, timezone="UTC",
                                per_week=len(CATALOG), reward=50)
        self.assertEqual({m.code: v for m, v, _ in again.status(2)}["games"], 2)

    def test_disabled_does_nothing(self):
        tmp = Path(tempfile.mkdtemp())
        points = PointsStore(tmp / "points.json")
        service = MissionsService(tmp / "m.json", points, enabled=False, timezone="UTC", per_week=7, reward=50)
        asyncio.run(service.progress([1], "games", 10))
        self.assertEqual(points.balance(1), 0)


if __name__ == "__main__":
    unittest.main()
