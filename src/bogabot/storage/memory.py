"""Implementación en memoria de los repositorios.

Útil para tests o para correr el bot sin depender del canal de Discord.
Demuestra que la interfaz Repository es realmente agnóstica del backend.
"""
from __future__ import annotations

from datetime import datetime

from bogabot.core.models import MatchRecord, PlayerLink
from bogabot.storage.base import LinkRepository, MatchRepository


class InMemoryStorage(LinkRepository, MatchRepository):
    def __init__(self) -> None:
        self._links: dict[int, PlayerLink] = {}
        self._matches: dict[str, MatchRecord] = {}  # dedup_key -> record

    # --- LinkRepository ---
    async def save_link(self, link: PlayerLink) -> None:
        self._links[link.discord_id] = link

    async def get_link(self, discord_id: int) -> PlayerLink | None:
        return self._links.get(discord_id)

    async def get_all_links(self) -> list[PlayerLink]:
        return list(self._links.values())

    async def delete_link(self, discord_id: int) -> bool:
        return self._links.pop(discord_id, None) is not None

    # --- MatchRepository ---
    async def save_match(self, record: MatchRecord) -> None:
        self._matches[record.dedup_key] = record

    async def match_exists(self, match_id: str, puuid: str) -> bool:
        return f"{match_id}:{puuid}" in self._matches

    async def get_matches(self, since: datetime, until: datetime) -> list[MatchRecord]:
        return [m for m in self._matches.values() if since <= m.game_creation < until]
