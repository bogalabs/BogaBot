"""Tests de IngestService con un RiotClient falso (sin red ni tokens)."""
from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import kill, match_json, timeline_json  # noqa: E402

from bogabot.core.models import MatchRecord, PlayerLink  # noqa: E402
from bogabot.core.timeutils import start_of_week, to_epoch_seconds  # noqa: E402
from bogabot.modules.lol.ingest import IngestService  # noqa: E402
from bogabot.riot.client import NotFoundError, RiotPuuidMismatchError, RiotUnavailableError  # noqa: E402
from bogabot.storage.memory import InMemoryStorage  # noqa: E402

_TZ = "America/Argentina/Buenos_Aires"


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


class _MatchRiot:
    """Riot falso con partidas reales (JSON de match-v5 sintético). Cuenta
    cuántas veces se pide cada cosa."""

    def __init__(self, matches: dict[str, dict], lists: dict[str, list[str]]) -> None:
        self.matches = matches
        self.lists = lists  # puuid -> match_ids
        self.match_calls: list[str] = []
        self.timeline_calls: list[str] = []
        self.start_times: list[int] = []
        self.timeline_down = False

    async def get_match_ids(self, puuid: str, start_time: int, count: int = 100) -> list[str]:
        self.start_times.append(start_time)
        return list(self.lists.get(puuid, []))

    async def get_match(self, match_id: str) -> dict:
        self.match_calls.append(match_id)
        await asyncio.sleep(0)  # cede el loop, como una request real
        return self.matches[match_id]

    async def get_match_timeline(self, match_id: str) -> dict:
        self.timeline_calls.append(match_id)
        await asyncio.sleep(0)
        if self.timeline_down:
            raise RiotUnavailableError(0, "sin respuesta")
        return timeline_json([kill(60_000, killer=6, victim=1)])


def _service(riot, store) -> IngestService:
    return IngestService(riot, store, store, SimpleNamespace(timezone=_TZ))  # type: ignore[arg-type]


async def _link_players(store: InMemoryStorage, *pids: int) -> None:
    for pid in pids:
        await store.save_link(_link(pid, f"Jugador{pid}", f"puuid-{pid}"))


class TestIngestPass(unittest.TestCase):
    def test_shared_match_is_fetched_once_and_has_timeline(self):
        async def scenario():
            store = InMemoryStorage()
            await _link_players(store, 1, 2)
            riot = _MatchRiot({"LA2_1": match_json("LA2_1")},
                              {"puuid-1": ["LA2_1"], "puuid-2": ["LA2_1"]})
            new = await _service(riot, store).ingest_all()

            self.assertEqual(sorted(r.discord_id for r in new), [1, 2])
            self.assertEqual(riot.match_calls, ["LA2_1"])  # caché: una sola vez para los 2
            self.assertEqual(riot.timeline_calls, ["LA2_1"])
            saved = {r.discord_id: r for r in await store.get_all_matches()}
            self.assertTrue(saved[1].has_timeline and saved[1].has_extended_stats)
            self.assertTrue(saved[1].gave_first_blood)
            # Se piden partidas desde un día antes del lunes (partidas que cruzan la medianoche).
            week_start = to_epoch_seconds(start_of_week(_TZ))
            self.assertEqual(set(riot.start_times), {week_start - 86400})

        asyncio.run(scenario())

    def test_solo_games_and_remakes_are_not_refetched(self):
        async def scenario():
            store = InMemoryStorage()
            await _link_players(store, 1, 2)
            solo = match_json("LA2_SOLO")
            solo["metadata"]["participants"][1] = "desconocido"  # el 2 no estaba
            remake = match_json("LA2_REMAKE", duration=200)
            riot = _MatchRiot({"LA2_SOLO": solo, "LA2_REMAKE": remake},
                              {"puuid-1": ["LA2_SOLO", "LA2_REMAKE"], "puuid-2": ["LA2_REMAKE"]})
            service = _service(riot, store)
            self.assertEqual(await service.ingest_all(), [])
            self.assertEqual(await service.ingest_all(), [])
            self.assertEqual(sorted(riot.match_calls), ["LA2_REMAKE", "LA2_SOLO"])  # nada repetido
            self.assertEqual(riot.timeline_calls, [])

            # Si cambian los vínculos, lo descartado se vuelve a evaluar: el 3
            # (recién vinculado) estaba en esa partida, así que ahora cuenta.
            await _link_players(store, 3)
            new = await service.ingest_all()
            self.assertEqual([r.dedup_key for r in new], ["LA2_SOLO:1"])

        asyncio.run(scenario())

    def test_timeline_down_postpones_then_saves_without_it(self):
        async def scenario():
            store = InMemoryStorage()
            await _link_players(store, 1, 2)
            riot = _MatchRiot({"LA2_1": match_json("LA2_1")}, {"puuid-1": ["LA2_1"]})
            riot.timeline_down = True
            service = _service(riot, store)
            self.assertEqual(await service.ingest_all(), [])  # 1er intento: queda para después
            self.assertEqual(await service.ingest_all(), [])  # 2do
            new = await service.ingest_all()  # 3ro: se guarda igual, sin timeline
            self.assertEqual(len(new), 1)
            self.assertFalse(new[0].has_timeline)
            self.assertTrue(new[0].has_extended_stats)

        asyncio.run(scenario())

    def test_concurrent_runs_do_not_duplicate(self):
        async def scenario():
            store = InMemoryStorage()
            await _link_players(store, 1, 2)
            riot = _MatchRiot({"LA2_1": match_json("LA2_1")},
                              {"puuid-1": ["LA2_1"], "puuid-2": ["LA2_1"]})
            service = _service(riot, store)
            seen: list[MatchRecord] = []

            async def listener(records):
                seen.extend(records)

            service.add_listener(listener)
            await asyncio.gather(service.ingest_all(), service.ingest_all())
            self.assertEqual(sorted(r.dedup_key for r in seen), ["LA2_1:1", "LA2_1:2"])

        asyncio.run(scenario())

    def test_enrich_legacy_records(self):
        async def scenario():
            store = InMemoryStorage()
            await _link_players(store, 1, 2)
            data = match_json("LA2_OLD")
            legacy = MatchRecord.from_dict({
                "match_id": "LA2_OLD", "puuid": "puuid-de-otra-key", "participant_id": 1, "discord_id": 1,
                "game_name": "Jugador1", "game_creation": "2026-08-22T01:00:00+00:00",
                "game_duration_seconds": 1800, "queue_id": 420, "game_mode": "CLASSIC", "champion": "Champ1",
                "position": "TOP", "win": True, "game_ended_in_surrender": False, "kills": 5, "deaths": 5,
                "assists": 5, "damage_to_champions": 20000, "vision_score": 25, "cs": 190,
            })
            await store.save_match(legacy)
            riot = _MatchRiot({"LA2_OLD": data}, {})
            service = _service(riot, store)

            self.assertEqual(len(await service.pending_enrichment()), 1)
            self.assertEqual(await service.enrich_stored_matches(), (1, 0, 0))
            [enriched] = await store.get_all_matches()
            self.assertTrue(enriched.has_extended_stats and enriched.has_timeline)
            self.assertEqual(enriched.dedup_key, legacy.dedup_key)
            self.assertEqual(await service.pending_enrichment(), [])

        asyncio.run(scenario())


    def test_quiet_enrichment_leaves_no_visible_logs(self):
        async def scenario():
            store = InMemoryStorage()
            await _link_players(store, 1, 2)
            for mid in ("LA2_OK", "LA2_GONE"):
                await store.save_match(MatchRecord.from_dict({
                    "match_id": mid, "puuid": "puuid-1", "participant_id": 1, "discord_id": 1,
                    "game_name": "Jugador1", "game_creation": "2026-08-22T01:00:00+00:00",
                    "game_duration_seconds": 1800, "queue_id": 420, "game_mode": "CLASSIC",
                    "champion": "Champ1", "position": "TOP", "win": True, "game_ended_in_surrender": False,
                    "kills": 5, "deaths": 5, "assists": 5, "damage_to_champions": 20000,
                    "vision_score": 25, "cs": 190,
                }))

            class _Riot(_MatchRiot):
                async def get_match(self, match_id: str) -> dict:
                    if match_id == "LA2_GONE":
                        raise NotFoundError(404, "no existe más")
                    return await super().get_match(match_id)

            riot = _Riot({"LA2_OK": match_json("LA2_OK")}, {})
            service = _service(riot, store)
            with self.assertNoLogs("bogabot", "INFO"):
                result = await service.enrich_stored_matches(quiet=True)
            self.assertEqual(result, (1, 0, 1))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
