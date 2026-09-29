"""Tests del ruteo de avisos del scheduler (alertas troll, papelones a
#general, aviso de partida) con canales de Discord falsos."""
from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import discord  # noqa: E402
from fixtures import record  # noqa: E402

from bogabot.core.models import MatchParticipant, MatchSummary  # noqa: E402
from bogabot.modules.lol.scheduler import LolScheduler  # noqa: E402
from bogabot.modules.lol.trolls import TrollService  # noqa: E402
from bogabot.storage.memory import InMemoryStorage  # noqa: E402
from bogabot.trolls.detector import TrollDetector  # noqa: E402
from bogabot.trolls.schema import TrollConfig  # noqa: E402

TROLL, RANKING, GENERAL, NOTIFY = 10, 20, 30, 40


class _Channel:
    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.sent: list[tuple[str | None, discord.Embed | None]] = []

    async def send(self, content: str | None = None, *, embed=None, allowed_mentions=None):
        if self.fail:
            raise discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "sin permisos")
        self.sent.append((content, embed))


def _summary(match_id: str) -> MatchSummary:
    parts = [
        MatchParticipant(discord_id=1 if pid == 1 else None, display_name=f"J{pid}", champion=f"C{pid}",
                         team_id=100 if pid <= 5 else 200, position="", win=pid > 5,
                         kills=1, deaths=1, assists=1, cs=100)
        for pid in range(1, 11)
    ]
    return MatchSummary(match_id=match_id, queue_id=420, game_duration_seconds=1800, participants=parts)


class _Fixture:
    def __init__(self, general_fails: bool = False) -> None:
        self.store = InMemoryStorage()
        settings = SimpleNamespace(
            timezone="America/Argentina/Buenos_Aires", troll_channel_id=TROLL, ranking_channel_id=RANKING,
            general_channel_id=GENERAL, match_notify_channel_id=NOTIFY,
        )
        self.summaries: list[str] = []

        async def build_match_summary(match_id):
            self.summaries.append(match_id)
            return _summary(match_id)

        bot = SimpleNamespace(
            settings=settings,
            trolls=TrollService(self.store, TrollDetector(TrollConfig.default()), settings),  # type: ignore[arg-type]
            ingest=SimpleNamespace(build_match_summary=build_match_summary),
        )
        self.channels = {TROLL: _Channel("trolls"), RANKING: _Channel("ranking"),
                         GENERAL: _Channel("general", fail=general_fails), NOTIFY: _Channel("partidas")}
        self.scheduler = LolScheduler(bot)  # type: ignore[arg-type]

        async def text_channel(channel_id, env_name):
            return self.channels.get(channel_id)

        self.scheduler._text_channel = text_channel  # type: ignore[method-assign]

    def ingest(self, *records):
        async def run():
            for r in records:
                await self.store.save_match(r)
            await self.scheduler._on_new_matches(list(records))
        asyncio.run(run())


_RECENT = datetime.now(timezone.utc) - timedelta(hours=1)
_FEEDER = dict(kills=1, deaths=12, assists=2, game_creation=_RECENT)  # alerta troll
_PAPELON = dict(kills=0, deaths=18, assists=1, items_sold=7, time_dead_seconds=800, deaths_before_10=4,
                deaths_to_lane_opponent=6, damage_to_champions=6000, game_creation=_RECENT)


class TestTrollAnnouncements(unittest.TestCase):
    def test_troll_alert_goes_to_troll_channel(self):
        f = _Fixture()
        f.ingest(record(discord_id=7, **_FEEDER))
        [(content, embed)] = f.channels[TROLL].sent
        self.assertIn("<@7>", content)
        self.assertEqual(f.channels[GENERAL].sent, [])

    def test_papelon_goes_to_general(self):
        f = _Fixture()
        f.ingest(record(discord_id=7, **_PAPELON))
        [(content, embed)] = f.channels[GENERAL].sent
        self.assertIn("<@7>", content)
        self.assertEqual(f.channels[TROLL].sent, [])

    def test_papelon_falls_back_to_troll_channel_if_general_fails(self):
        f = _Fixture(general_fails=True)
        with self.assertLogs("bogabot.modules.lol.scheduler", "ERROR"):
            f.ingest(record(discord_id=7, **_PAPELON))
        self.assertEqual(len(f.channels[TROLL].sent), 1)

    def test_old_and_clean_games_are_not_announced(self):
        f = _Fixture()
        old = dict(_FEEDER, game_creation=datetime.now(timezone.utc) - timedelta(days=3))
        f.ingest(record(match_id="LA2_OLD", discord_id=7, **old), record(match_id="LA2_OK", discord_id=8,
                                                                         game_creation=_RECENT))
        self.assertEqual(f.channels[TROLL].sent, [])
        self.assertEqual(f.channels[GENERAL].sent, [])

    def test_match_notification_has_troll_meter_and_is_not_repeated(self):
        f = _Fixture()
        f.ingest(record(match_id="LA2_5", discord_id=1, **_FEEDER))
        f.ingest(record(match_id="LA2_5", discord_id=2, game_creation=_RECENT))  # 2do jugador, más tarde
        [(_, embed)] = f.channels[NOTIFY].sent
        meter = next(field for field in embed.fields if "Troll-o-metro" in field.name)
        self.assertIn("<@1>", meter.value)
        self.assertEqual(f.summaries, ["LA2_5"])

    def test_notification_failure_does_not_block_troll_alerts(self):
        f = _Fixture()
        f.channels[NOTIFY].fail = True
        with self.assertLogs("bogabot.modules.lol.scheduler", "ERROR"):
            f.ingest(record(discord_id=7, **_FEEDER))
        self.assertEqual(len(f.channels[TROLL].sent), 1)

    def test_monday_recap_crowns_the_troll_of_the_week(self):
        f = _Fixture()
        last_week = datetime.now(timezone.utc) - timedelta(days=7)
        asyncio.run(f.store.save_match(record(discord_id=7, **dict(_FEEDER, game_creation=last_week))))
        asyncio.run(f.scheduler._post_troll_summaries(is_monday=True))
        contents = [c for c, _ in f.channels[TROLL].sent]
        self.assertTrue(any(c and "<@7>" in c and "Troll de la semana" in c for c in contents))


if __name__ == "__main__":
    unittest.main()
