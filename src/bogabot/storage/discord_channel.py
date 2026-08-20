"""Implementación de storage que usa un canal de Discord como "base de datos".

Cada registro es un mensaje del propio bot en el canal privado configurado
(STORAGE_CHANNEL_ID), con un prefijo que indica el tipo y un JSON con los datos:

    LINK  {"discord_id": 123, "game_name": "...", ...}
    MATCH {"match_id": "LA2_...", "puuid": "...", ...}

Para no leer el canal entero en cada consulta (sería O(n) mensajes y lento),
al arrancar se hidrata un índice en memoria leyendo el historial una sola vez.
Las escrituras van a Discord y actualizan el índice en caliente.

NOTA DE DISEÑO: esto es deliberadamente simple para el MVP. Como está detrás
de la interfaz Repository, migrar a SQLite/Postgres/Firebase después no toca
nada del resto de la app.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import discord

from bogabot.core.models import MatchRecord, PlayerLink
from bogabot.settings import Settings
from bogabot.storage.base import LinkRepository, MatchRepository

log = logging.getLogger(__name__)

LINK_PREFIX = "LINK "
MATCH_PREFIX = "MATCH "
# Cuántos mensajes leer al hidratar. Suficiente para un grupo de amigos.
HYDRATE_LIMIT = 5000


class DiscordChannelStorage(LinkRepository, MatchRepository):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._channel: discord.TextChannel | None = None
        self._ready = False
        # Índices en memoria (se hidratan en connect()):
        self._links: dict[int, tuple[PlayerLink, int]] = {}  # discord_id -> (link, message_id)
        self._matches: dict[str, MatchRecord] = {}  # dedup_key -> record

    @property
    def ready(self) -> bool:
        return self._ready

    async def connect(self, client: discord.Client) -> None:
        """Resuelve el canal e hidrata los índices desde el historial. Llamar
        una vez cuando el bot está listo (on_ready)."""
        channel = client.get_channel(self._settings.storage_channel_id)
        if channel is None:
            channel = await client.fetch_channel(self._settings.storage_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(
                f"STORAGE_CHANNEL_ID={self._settings.storage_channel_id} no es un canal de texto."
            )
        self._channel = channel
        await self._hydrate(client.user.id if client.user else None)
        self._ready = True
        log.info(
            "Storage hidratado: %d vínculos, %d partidas.",
            len(self._links),
            len(self._matches),
        )

    async def _hydrate(self, bot_user_id: int | None) -> None:
        assert self._channel is not None
        self._links.clear()
        self._matches.clear()
        async for msg in self._channel.history(limit=HYDRATE_LIMIT, oldest_first=True):
            if bot_user_id is not None and msg.author.id != bot_user_id:
                continue
            content = msg.content
            try:
                if content.startswith(LINK_PREFIX):
                    data = json.loads(content[len(LINK_PREFIX):])
                    link = PlayerLink.from_dict(data)
                    # El último mensaje LINK de un usuario gana (save reescribe/edita).
                    self._links[link.discord_id] = (link, msg.id)
                elif content.startswith(MATCH_PREFIX):
                    data = json.loads(content[len(MATCH_PREFIX):])
                    record = MatchRecord.from_dict(data)
                    self._matches[record.dedup_key] = record
            except (json.JSONDecodeError, KeyError, ValueError):
                log.warning("Mensaje de storage ilegible (id=%s), lo salteo.", msg.id)

    def _ensure_ready(self) -> discord.TextChannel:
        if not self._ready or self._channel is None:
            raise RuntimeError("El storage todavía no está inicializado.")
        return self._channel

    # --- LinkRepository ----------------------------------------------------
    async def save_link(self, link: PlayerLink) -> None:
        channel = self._ensure_ready()
        payload = LINK_PREFIX + json.dumps(link.to_dict(), ensure_ascii=False)
        existing = self._links.get(link.discord_id)
        if existing is not None:
            _, message_id = existing
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(content=payload)
                self._links[link.discord_id] = (link, message_id)
                return
            except discord.NotFound:
                pass  # el mensaje ya no existe: creamos uno nuevo abajo
        msg = await channel.send(payload)
        self._links[link.discord_id] = (link, msg.id)

    async def get_link(self, discord_id: int) -> PlayerLink | None:
        entry = self._links.get(discord_id)
        return entry[0] if entry else None

    async def get_all_links(self) -> list[PlayerLink]:
        return [link for link, _ in self._links.values()]

    async def delete_link(self, discord_id: int) -> bool:
        channel = self._ensure_ready()
        entry = self._links.pop(discord_id, None)
        if entry is None:
            return False
        _, message_id = entry
        try:
            msg = await channel.fetch_message(message_id)
            await msg.delete()
        except discord.NotFound:
            pass
        return True

    # --- MatchRepository ---------------------------------------------------
    async def save_match(self, record: MatchRecord) -> None:
        channel = self._ensure_ready()
        if record.dedup_key in self._matches:
            return
        payload = MATCH_PREFIX + json.dumps(record.to_dict(), ensure_ascii=False)
        await channel.send(payload)
        self._matches[record.dedup_key] = record

    async def match_exists(self, match_id: str, puuid: str) -> bool:
        return f"{match_id}:{puuid}" in self._matches

    async def get_matches(self, since: datetime, until: datetime) -> list[MatchRecord]:
        return [m for m in self._matches.values() if since <= m.game_creation < until]
