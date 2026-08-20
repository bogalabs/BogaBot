"""Servicio de ingesta de partidas.

Para cada jugador vinculado, pide a Riot las partidas desde el inicio de la
semana en curso (lunes) y guarda las que todavía no estén. El dedup por
(match_id, puuid) hace que reprocesar sea gratis, así que no necesitamos
llevar un cursor: cada corrida pide "desde el lunes" y saltea lo ya guardado.

Regla del grupo: una partida solo cuenta para el ranking si jugaste
acompañado de al menos otro jugador vinculado (se descartan las que jugaste
sin ningún conocido del grupo). Se chequea contra `metadata.participants`
del JSON de match-v5, que trae los puuid de los 10 jugadores de la partida.
"""
from __future__ import annotations

import logging

from bogabot.core.models import MatchRecord
from bogabot.core.timeutils import start_of_week, to_epoch_seconds
from bogabot.riot.client import RiotClient
from bogabot.riot.mapper import map_match
from bogabot.settings import Settings
from bogabot.storage.base import LinkRepository, MatchRepository

log = logging.getLogger(__name__)


class IngestService:
    def __init__(
        self,
        riot: RiotClient,
        links: LinkRepository,
        matches: MatchRepository,
        settings: Settings,
    ) -> None:
        self._riot = riot
        self._links = links
        self._matches = matches
        self._settings = settings

    async def ingest_all(self) -> list[MatchRecord]:
        """Ingiere partidas nuevas de todos los jugadores. Devuelve los
        registros (partida+jugador) que se guardaron en esta corrida."""
        links = await self._links.get_all_links()
        if not links:
            log.info("No hay jugadores vinculados; nada para ingerir.")
            return []

        linked_puuids = {l.puuid for l in links}
        week_start = start_of_week(self._settings.timezone)
        start_epoch = to_epoch_seconds(week_start)
        new_records: list[MatchRecord] = []

        for link in links:
            try:
                match_ids = await self._riot.get_match_ids(link.puuid, start_epoch, count=100)
            except Exception:  # noqa: BLE001 - un jugador no debe frenar al resto
                log.exception("Error trayendo IDs de partidas de %s.", link.riot_id)
                continue

            for match_id in match_ids:
                if await self._matches.match_exists(match_id, link.puuid):
                    continue
                try:
                    data = await self._riot.get_match(match_id)
                except Exception:  # noqa: BLE001
                    log.exception("Error trayendo la partida %s.", match_id)
                    continue

                participants = set(data.get("metadata", {}).get("participants", []))
                if len(linked_puuids & participants) < 2:
                    continue  # jugó sin ningún otro vinculado: no cuenta

                record = map_match(data, link.puuid, link.discord_id)
                if record is None:
                    continue  # remake / jugador ausente
                await self._matches.save_match(record)
                new_records.append(record)

        log.info("Ingesta completa: %d partidas-jugador nuevas.", len(new_records))
        return new_records
