"""Tests de la tienda (store de efectos/inmunidad), la inmunidad troll en el
scheduler y el sonido por evento."""
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
from test_scheduler import _FEEDER, TROLL, _Fixture  # noqa: E402

from bogabot.core.models import TrollLevel  # noqa: E402
from bogabot.modules.points.shop import parse_hex_color  # noqa: E402
from bogabot.modules.points.shop_store import ShopEffect, ShopStore  # noqa: E402
from bogabot.modules.sounds.cog import pick_event_sound  # noqa: E402


class TestShopStore(unittest.TestCase):
    def test_effects_persist_and_expire(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shop.json"
            store = ShopStore(path)
            now = datetime.now(timezone.utc)
            asyncio.run(store.add(ShopEffect("r1", "role", 1, 1, now - timedelta(minutes=1), {"role_id": 9})))
            asyncio.run(store.add(ShopEffect("n1", "nick", 1, 2, now + timedelta(hours=1), {"nick": "x"})))
            again = ShopStore(path)
            self.assertEqual([e.id for e in again.due(now)], ["r1"])
            self.assertEqual(again.find("nick", target_id=2).data, {"nick": "x"})
            self.assertIsNone(again.find("nick", target_id=1))

    def test_shields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ShopStore(Path(tmp) / "shop.json")
            self.assertFalse(asyncio.run(store.use_shield(7)))
            asyncio.run(store.add_shield(7))
            asyncio.run(store.add_shield(7))
            self.assertTrue(asyncio.run(store.use_shield(7)))
            self.assertEqual(store.shields(7), 1)

    def test_parse_hex_color(self):
        self.assertEqual(parse_hex_color("#ff66cc").value, 0xFF66CC)
        self.assertEqual(parse_hex_color("00ff00").value, 0x00FF00)
        self.assertIsNone(parse_hex_color("rojo"))
        self.assertIsNone(parse_hex_color("#fff"))


class TestShieldSilencesAlert(unittest.TestCase):
    def test_shield_skips_one_alert(self):
        f = _Fixture()
        shields = {7: 1}

        async def shield(verdict):
            uid = verdict.record.discord_id
            if shields.get(uid):
                shields[uid] -= 1
                return True
            return False

        f.scheduler.bot.trolls.add_alert_filter(shield)
        f.ingest(record(match_id="LA2_1", discord_id=7, **_FEEDER))
        self.assertEqual(f.channels[TROLL].sent, [])  # la gastó
        f.ingest(record(match_id="LA2_2", discord_id=7, **_FEEDER))
        self.assertEqual(len(f.channels[TROLL].sent), 1)  # ya no tenía


class TestEventSound(unittest.TestCase):
    def test_papelon_wins_over_victory(self):
        epic = record(win=True, kills=15, deaths=1, assists=5)
        self.assertEqual(pick_event_sound([epic], [TrollLevel.PAPELON], "buu", "yay"), "buu")
        self.assertEqual(pick_event_sound([epic], [TrollLevel.NONE], "buu", "yay"), "yay")

    def test_nothing_special_or_disabled(self):
        normal = record(win=True, kills=5, deaths=5, assists=5)
        self.assertIsNone(pick_event_sound([normal], [TrollLevel.NONE], "buu", "yay"))
        epic = record(win=True, kills=15, deaths=1, assists=5)
        self.assertIsNone(pick_event_sound([epic], [TrollLevel.PAPELON], "", ""))


if __name__ == "__main__":
    unittest.main()
