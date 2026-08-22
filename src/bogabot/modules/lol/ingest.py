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

import datetime as dt
import logging

from bogabot.core.models import MatchRecord, MatchSummary
from bogabot.core.timeutils import start_of_week, to_epoch_seconds
from bogabot.riot.client import RiotAuthError, RiotClient
from bogabot.riot.mapper import map_match, map_match_summary
from bogabot.settings import Settings
from bogabot.storage.base import LinkRepository, MatchRepository

log = logging.getLogger(__name__)

# Evita floodear el canal de logs: si la key sigue vencida, se re-avisa como
# mucho una vez cada tantas horas en vez de en cada corrida del scheduler.
_AUTH_ALERT_COOLDOWN = dt.timedelta(hours=3)


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
        self._last_auth_alert: dt.datetime | None = None

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
            except RiotAuthError:
                # La key es la misma para todos los jugadores: no tiene sentido
                # seguir pegándole a Riot con el resto del loop.
                self._alert_expired_key()
                break
            except Exception:  # noqa: BLE001 - un jugador no debe frenar al resto
                log.exception("Error trayendo IDs de partidas de %s.", link.riot_id)
                continue

            auth_failed = False
            for match_id in match_ids:
                if await self._matches.match_exists(match_id, link.puuid):
                    continue
                try:
                    data = await self._riot.get_match(match_id)
                except RiotAuthError:
                    self._alert_expired_key()
                    auth_failed = True
                    break
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

            if auth_failed:
                break

        log.info("Ingesta completa: %d partidas-jugador nuevas.", len(new_records))
        return new_records

    async def build_match_summary(self, match_id: str) -> MatchSummary | None:
        """Arma el resumen completo (10 jugadores) de una partida ya
        ingerida, para el aviso "en vivo" (ver `LolScheduler`). Devuelve None
        si falla la consulta a Riot; un jugador no debe frenar al resto."""
        links = await self._links.get_all_links()
        puuid_to_discord = {l.puuid: l.discord_id for l in links}
        try:
            data = await self._riot.get_match(match_id)
        except Exception:  # noqa: BLE001 - una partida no debe frenar al resto
            log.exception("Error trayendo la partida %s para el resumen del aviso.", match_id)
            return None
        return map_match_summary(data, puuid_to_discord)

    def _alert_expired_key(self) -> None:
        """Loguea (nivel CRITICAL, llega al canal de logs de Discord vía
        DiscordLogHandler) que la RIOT_API_KEY parece vencida. Con cooldown
        para no mandar un mensaje por cada corrida del scheduler mientras
        nadie la renueva."""
        now = dt.datetime.now(dt.timezone.utc)
        if self._last_auth_alert is not None and now - self._last_auth_alert < _AUTH_ALERT_COOLDOWN:
            log.debug("RIOT_API_KEY sigue vencida (alerta ya avisada, en cooldown).")
            return
        self._last_auth_alert = now
        log.critical(
            "RIOT_API_KEY vencida o inválida (Riot devolvió 401/403). "
            "Generá una nueva en https://developer.riotgames.com/, actualizá RIOT_API_KEY "
            "en el .env y reiniciá el bot. La dev key vence cada 24h."
        )
