"""Campeones e ítems de Data Dragon (CDN público de Riot, sin API key).

Se baja la última versión y se cachea en memoria `_TTL`; si la descarga
falla y hay algo cacheado, se sigue usando lo viejo. `parse_*` son puras
(testeables con el JSON guardado).
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from bogabot.modules.roulette.logic import Champion, Item, item_themes

log = logging.getLogger(__name__)

_BASE = "https://ddragon.leagueoflegends.com"
_TTL = 12 * 3600
_SUMMONERS_RIFT = "11"
_MIN_ITEM_GOLD = 2000


class DDragonError(Exception):
    pass


def parse_champions(data: dict) -> list[Champion]:
    return sorted((Champion(id=c["id"], name=c["name"], tags=tuple(c.get("tags", [])))
                   for c in data["data"].values()), key=lambda c: c.id)


def _buyable_on_rift(item_id: str, item: dict) -> bool:
    return (bool(item.get("maps", {}).get(_SUMMONERS_RIFT)) and item.get("gold", {}).get("purchasable", False)
            and item.get("inStore", True) and not item.get("requiredChampion") and not item.get("requiredAlly")
            and "Consumable" not in item.get("tags", [])
            # Los ids largos (32xxxx, 66xxxx...) son variantes de otros modos o de la misión de support.
            and len(item_id) <= 4)


def parse_items(data: dict) -> tuple[list[Item], list[Item]]:
    """(ítems finales con algún tema, botas de nivel 2) de la Grieta."""
    finals: list[Item] = []
    boots: list[Item] = []
    for item_id, raw in data["data"].items():
        if not _buyable_on_rift(item_id, raw):
            continue
        item = Item(id=item_id, name=raw["name"].strip(), tags=frozenset(raw.get("tags", [])))
        if "Boots" in item.tags:
            if raw.get("depth") == 2:
                boots.append(item)
        elif not raw.get("into") and raw.get("depth") and raw["gold"]["total"] >= _MIN_ITEM_GOLD \
                and item_themes(item):
            finals.append(item)
    return sorted(finals, key=lambda i: i.id), sorted(boots, key=lambda i: i.id)


class DDragon:
    def __init__(self, lang: str) -> None:
        self._lang = lang
        self._lock = asyncio.Lock()
        self._loaded_at = 0.0
        self.version = ""
        self.champions: list[Champion] = []
        self.items: list[Item] = []
        self.boots: list[Item] = []

    async def ensure(self) -> None:
        """Deja cargados campeones e ítems (re-descarga si venció el caché)."""
        async with self._lock:
            if self.champions and time.monotonic() - self._loaded_at < _TTL:
                return
            try:
                await self._download()
            except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError, DDragonError) as exc:
                if not self.champions:
                    raise DDragonError(f"No pude bajar los datos de Data Dragon ({exc}).") from exc
                log.warning("Data Dragon falló, sigo con la versión %s (%s).", self.version, exc)

    async def _download(self) -> None:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async def get(url: str):
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise DDragonError(f"HTTP {resp.status} en {url}")
                    return await resp.json(content_type=None)

            version = (await get(f"{_BASE}/api/versions.json"))[0]
            champions = await get(f"{_BASE}/cdn/{version}/data/{self._lang}/champion.json")
            items = await get(f"{_BASE}/cdn/{version}/data/{self._lang}/item.json")
        self.champions = parse_champions(champions)
        self.items, self.boots = parse_items(items)
        if not self.champions or not self.items:
            raise DDragonError("Data Dragon vino vacío.")
        self.version, self._loaded_at = version, time.monotonic()
        log.info("Data Dragon %s: %d campeones, %d ítems, %d botas.", version, len(self.champions),
                 len(self.items), len(self.boots))
