"""Tests de IngestService con un RiotClient falso (sin red ni tokens)."""
from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bogabot.core.models import PlayerLink  # noqa: E402
from bogabot.modules.lol.ingest import IngestService  # noqa: E402
from bogabot.riot.client import RiotPuuidMismatchError  # noqa: E402
from bogabot.storage.memory import InMemoryStorage  # noqa: E402


class _FakeRiot:
    """Simula una key de otra app: solo acepta los puuid "nuevos"."""

    def __init__(self, new_puuids: dict[str, str]) -> None:
        self._new_puuids = new_puuids  # riot_id -> puuid con la key actual
        self.match_ids_calls: list[str] = []

    async def get_match_ids(self, puuid: str, start_time: int, count: int = 100) -> list[str]:
        self.match_ids_calls.append(puuid)
        if puuid not in self._new_puuids.values():
            raise RiotPuuidMismatchError(400, "Bad Request - Exception decrypting " + puuid)
        return []

    async def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        return {"puuid": self._new_puuids[f"{game_name}#{tag_line}"],
                "gameName": game_name, "tagLine": tag_line}


def _link(discord_id: int, name: str, puuid: str) -> PlayerLink:
    return PlayerLink(discord_id=discord_id, game_name=name, tag_line="LAS", puuid=puuid,
                      linked_at=datetime.now(timezone.utc))


class TestPuuidRefresh(unittest.TestCase):
    def test_reresolves_puuids_from_other_app_key(self):
        async def scenario():
            store = InMemoryStorage()
            await store.save_link(_link(1, "A", "old-a"))
            await store.save_link(_link(2, "B", "old-b"))
            riot = _FakeRiot({"A#LAS": "new-a", "B#LAS": "new-b"})
            settings = SimpleNamespace(timezone="America/Argentina/Buenos_Aires")
            service = IngestService(riot, store, store, settings)  # type: ignore[arg-type]

            await service.ingest_all()

            puuids = {l.discord_id: l.puuid for l in await store.get_all_links()}
            self.assertEqual(puuids, {1: "new-a", 2: "new-b"})
            # Primera pasada corta en el primer puuid viejo; la segunda usa los nuevos.
            self.assertEqual(riot.match_ids_calls, ["old-a", "new-a", "new-b"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
