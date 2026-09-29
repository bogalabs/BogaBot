"""Tests del PointsStore (saldo, tope diario de voz, canje). Sin red ni Discord."""
import asyncio
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from bogabot.modules.points.store import PointsStore  # noqa: E402


class PointsStoreTest(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp()) / "points.json"

    def test_add_and_spend(self):
        store = PointsStore(self.path)
        asyncio.run(store.add({1: 120, 2: 30}, "test"))
        self.assertTrue(asyncio.run(store.spend(1, 100, "canje")))
        self.assertFalse(asyncio.run(store.spend(2, 100, "canje")))
        self.assertEqual(store.balance(1), 20)
        self.assertEqual(store.balance(2), 30)
        self.assertEqual(store.top(), [(2, 30), (1, 20)])

    def test_balance_never_negative(self):
        store = PointsStore(self.path)
        asyncio.run(store.add({1: 10}, "test"))
        asyncio.run(store.add({1: -50}, "test"))
        self.assertEqual(store.balance(1), 0)

    def test_voice_daily_cap_resets_next_day(self):
        store = PointsStore(self.path)
        for _ in range(5):
            asyncio.run(store.add_voice([1], 1, "2026-09-24", daily_cap=3))
        self.assertEqual(store.balance(1), 3)
        awarded = asyncio.run(store.add_voice([1], 1, "2026-09-25", daily_cap=3))
        self.assertEqual(awarded, {1: 1})
        self.assertEqual(store.balance(1), 4)

    def test_persists_between_instances(self):
        asyncio.run(PointsStore(self.path).add({7: 42}, "test"))
        self.assertEqual(PointsStore(self.path).balance(7), 42)


if __name__ == "__main__":
    unittest.main()
