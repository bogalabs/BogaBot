"""Interfaz de la capa de almacenamiento (patrón Repository).

El resto de la app depende SOLO de estas clases abstractas, nunca de una
implementación concreta. Hoy los datos viven en un canal de Discord
(`DiscordChannelStorage`); mañana podés escribir `SqliteStorage`,
`PostgresStorage` o `FirebaseStorage` implementando estas mismas firmas y
swapearlo en un solo lugar (bot.py) sin tocar scoring, comandos ni ranking.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from bogabot.core.models import MatchRecord, PlayerLink


class LinkRepository(ABC):
    """Vínculos usuario de Discord <-> cuenta de Riot."""

    @abstractmethod
    async def save_link(self, link: PlayerLink) -> None:
        """Crea o actualiza el vínculo de un usuario."""

    @abstractmethod
    async def get_link(self, discord_id: int) -> PlayerLink | None:
        ...

    @abstractmethod
    async def get_all_links(self) -> list[PlayerLink]:
        ...

    @abstractmethod
    async def delete_link(self, discord_id: int) -> bool:
        """Devuelve True si había un vínculo y se borró."""


class MatchRepository(ABC):
    """Registros de partidas (una fila por jugador por partida)."""

    @abstractmethod
    async def save_match(self, record: MatchRecord) -> None:
        ...

    @abstractmethod
    async def match_exists(self, match_id: str, puuid: str) -> bool:
        """Dedup: True si ese jugador+partida ya está guardado."""

    @abstractmethod
    async def get_matches(self, since: datetime, until: datetime) -> list[MatchRecord]:
        """Todas las partidas cuya creación cae en [since, until)."""

    @abstractmethod
    async def get_all_matches(self) -> list[MatchRecord]:
        """Todas las partidas almacenadas."""
