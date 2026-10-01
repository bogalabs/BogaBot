"""Servicio de ingesta de partidas.

Para cada jugador vinculado, pide a Riot las partidas desde el inicio de la
semana en curso (lunes) y guarda las que todavía no estén. El dedup por
(match_id, discord_id) hace que reprocesar sea gratis, así que no necesitamos
llevar un cursor: cada corrida pide "desde el lunes" y saltea lo ya guardado.

Regla del grupo: una partida solo cuenta para el ranking si jugaste
acompañado de al menos otro jugador vinculado (se descartan las que jugaste
sin ningún conocido del grupo). Se chequea contra `metadata.participants`
del JSON de match-v5, que trae los puuid de los 10 jugadores de la partida.

Para cada partida que cuenta se pide también su timeline (minuto a minuto),
que usa el detector de trolls (primera sangre, muertes antes del 10, oro vs.
rival al 15, items vendidos...). Partidas y timelines se cachean en memoria:
una partida con 3 vinculados se baja una sola vez, no 3.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import OrderedDict
from dataclasses import replace
from collections.abc import Awaitable, Callable

from bogabot.core.models import MatchRecord, MatchSummary
from bogabot.core.timeutils import start_of_week, to_epoch_seconds
from bogabot.riot.client import (
    NotFoundError,
    RiotApiError,
    RiotAuthError,
    RiotClient,
    RiotPuuidMismatchError,
    RiotUnavailableError,
)
from bogabot.riot.mapper import TIMELINE_VERSION, is_remake, map_match, map_match_summary
from bogabot.settings import Settings
from bogabot.storage.base import LinkRepository, MatchRepository

log = logging.getLogger(__name__)

# Evita floodear el canal de logs: si la key sigue vencida, se re-avisa como
# mucho una vez cada tantas horas en vez de en cada corrida del scheduler.
_AUTH_ALERT_COOLDOWN = dt.timedelta(hours=3)

# Cuánto antes del lunes se piden partidas (ver `_ingest_pass`).
_LOOKBACK_MARGIN = dt.timedelta(days=1)
# Cuántas partidas/timelines recientes se guardan en memoria (LRU).
_CACHE_SIZE = 64
# Si Riot no responde al pedir el timeline, la partida se deja para la
# próxima corrida (así no se guarda sin los datos del detector de trolls).
# Después de tantos intentos fallidos se guarda igual, sin timeline.
_TIMELINE_MAX_TRIES = 3


class _LRU(OrderedDict):
    def __init__(self, size: int) -> None:
        super().__init__()
        self._size = size

    def put(self, key: str, value: dict) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > self._size:
            self.popitem(last=False)


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
        # Otros módulos (ej. puntos) se enteran de las partidas nuevas sin que
        # LoL sepa de ellos: se registran acá y reciben los registros guardados.
        self._listeners: list[Callable[[list[MatchRecord]], Awaitable[None]]] = []
        # notify_job, daily_job e /ingest-now pueden coincidir: sin el lock, dos
        # corridas guardarían (y avisarían) la misma partida dos veces.
        self._lock = asyncio.Lock()
        self._match_cache = _LRU(_CACHE_SIZE)
        self._timeline_cache = _LRU(_CACHE_SIZE)
        self._timeline_tries: dict[str, int] = {}
        # Partidas descartadas (jugadas sin otro vinculado, o remakes) -> set de
        # puuids vinculados cuando se descartaron. Evita re-bajarlas en cada
        # poll; si cambian los vínculos se vuelven a evaluar.
        self._skipped: dict[str, frozenset[str]] = {}

    def add_listener(self, callback: Callable[[list[MatchRecord]], Awaitable[None]]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[list[MatchRecord]], Awaitable[None]]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    async def ingest_all(self) -> list[MatchRecord]:
        """Ingiere partidas nuevas de todos los jugadores. Devuelve los
        registros (partida+jugador) que se guardaron en esta corrida.

        Si Riot no puede desencriptar los puuid guardados (cambió la API key
        por una de otra app), se re-resuelven por Riot ID y se reintenta una
        vez en la misma corrida.

        Los listeners (avisos de partida, alertas troll, puntos) se llaman acá
        mismo, así cualquier camino que ingiera (poll, job diario,
        /ingest-now) dispara los avisos una sola vez por partida."""
        async with self._lock:
            new_records: list[MatchRecord] = []
            stale = await self._ingest_pass(new_records, heal=True)
            if stale and await self.refresh_puuids():
                await self._ingest_pass(new_records, heal=False)

            log.info("Ingesta completa: %d partidas-jugador nuevas.", len(new_records))
            if new_records:
                for callback in list(self._listeners):
                    try:
                        await callback(new_records)
                    except Exception:  # noqa: BLE001 - un listener no debe romper la ingesta
                        log.exception("Error en un listener de partidas nuevas.")
            return new_records

    async def _ingest_pass(self, new_records: list[MatchRecord], heal: bool) -> bool:
        """Una pasada por todos los vinculados; agrega lo guardado a
        `new_records`. Con `heal=True`, ante un puuid de otra app corta y
        devuelve True para que `ingest_all` los re-resuelva."""
        links = await self._links.get_all_links()
        if not links:
            log.info("No hay jugadores vinculados; nada para ingerir.")
            return False

        linked_puuids = frozenset(l.puuid for l in links)
        week_start = start_of_week(self._settings.timezone)
        # Con margen: una partida del domingo a la noche que termina pasada la
        # medianoche (o que el bot no vio por estar caído) no se pierde.
        start_epoch = to_epoch_seconds(week_start - _LOOKBACK_MARGIN)
        seen_ids: set[str] = set()

        for link in links:
            try:
                match_ids = await self._riot.get_match_ids(link.puuid, start_epoch, count=100)
            except RiotAuthError:
                # La key es la misma para todos los jugadores: no tiene sentido
                # seguir pegándole a Riot con el resto del loop.
                self._alert_expired_key()
                return False
            except RiotUnavailableError as exc:
                # Problema de red/Riot caído: afecta a todos por igual. Se corta
                # la corrida sin traceback; el próximo poll lo reintenta.
                log.warning("Riot no responde (%s); corto la ingesta y reintento en la próxima corrida.", exc)
                return False
            except RiotPuuidMismatchError:
                if heal:
                    return True
                log.warning("El puuid de %s sigue sin servir con la key actual; "
                            "revinculalo con /link-admin.", link.riot_id)
                continue
            except Exception:  # noqa: BLE001 - un jugador no debe frenar al resto
                log.exception("Error trayendo IDs de partidas de %s.", link.riot_id)
                continue

            seen_ids.update(match_ids)
            for match_id in match_ids:
                if await self._matches.match_exists(match_id, link.discord_id):
                    continue
                if self._skipped.get(match_id) == linked_puuids:
                    continue  # ya se descartó con estos mismos vínculos
                try:
                    data = await self._fetch_match(match_id)
                except RiotAuthError:
                    self._alert_expired_key()
                    return False
                except RiotUnavailableError as exc:
                    log.warning("Riot no responde trayendo la partida %s (%s); queda para la próxima corrida.", match_id, exc)
                    continue
                except Exception:  # noqa: BLE001
                    log.exception("Error trayendo la partida %s.", match_id)
                    continue

                participants = set(data.get("metadata", {}).get("participants", []))
                if len(linked_puuids & participants) < 2 or is_remake(data):
                    # Jugó sin ningún otro vinculado (no cuenta) o fue remake.
                    self._skipped[match_id] = linked_puuids
                    continue

                try:
                    timeline = await self._fetch_timeline(match_id)
                except RiotAuthError:
                    self._alert_expired_key()
                    return False
                except RiotUnavailableError as exc:
                    tries = self._timeline_tries.get(match_id, 0) + 1
                    self._timeline_tries[match_id] = tries
                    if tries < _TIMELINE_MAX_TRIES:
                        log.warning("Riot no responde trayendo el timeline de %s (%s); "
                                    "la partida queda para la próxima corrida.", match_id, exc)
                        continue
                    log.warning("El timeline de %s sigue sin responder; la guardo sin esos datos.", match_id)
                    timeline = None

                record = map_match(data, link.puuid, link.discord_id, timeline=timeline)
                if record is None:
                    continue  # jugador ausente (no debería pasar)
                await self._matches.save_match(record)
                new_records.append(record)
                self._timeline_tries.pop(match_id, None)

        # Solo se recuerda lo descartado que Riot sigue devolviendo (semana en curso).
        self._skipped = {mid: snap for mid, snap in self._skipped.items() if mid in seen_ids}
        return False

    async def _fetch_match(self, match_id: str) -> dict:
        data = self._match_cache.get(match_id)
        if data is None:
            data = await self._riot.get_match(match_id)
            self._match_cache.put(match_id, data)
        return data

    async def _fetch_timeline(self, match_id: str, quiet: bool = False) -> dict | None:
        """Timeline de la partida (cacheado). Devuelve None si Riot no lo tiene
        (404 u otro error del lado de Riot): la partida se guarda igual, sin
        esos datos. Deja pasar `RiotAuthError` y `RiotUnavailableError` para
        que quien llama decida (cortar la corrida o reintentar después)."""
        timeline = self._timeline_cache.get(match_id)
        if timeline is not None:
            return timeline
        try:
            timeline = await self._riot.get_match_timeline(match_id)
        except (RiotAuthError, RiotUnavailableError):
            raise
        except RiotApiError as exc:
            log.log(logging.DEBUG if quiet else logging.WARNING,
                    "No pude traer el timeline de %s (%s); sigo sin esos datos.", match_id, exc)
            return None
        self._timeline_cache.put(match_id, timeline)
        return timeline

    async def refresh_puuids(self) -> int:
        """Vuelve a pedir el puuid de cada vinculado por su Riot ID con la key
        actual y actualiza los que cambiaron. Hace falta al pasar a una key de
        otra app de Riot (ej. de la dev key a la Personal API Key), porque los
        puuid vienen encriptados por app. Devuelve cuántos se actualizaron."""
        updated = 0
        for link in await self._links.get_all_links():
            try:
                account = await self._riot.get_account_by_riot_id(link.game_name, link.tag_line)
            except NotFoundError:
                log.warning("No encontré %s en Riot (¿cambió de nombre?); revinculalo con /link-admin.",
                            link.riot_id)
                continue
            except RiotApiError as exc:
                log.warning("No pude re-resolver el puuid de %s (%s).", link.riot_id, exc)
                continue
            if account["puuid"] == link.puuid:
                continue
            await self._links.save_link(replace(
                link,
                puuid=account["puuid"],
                game_name=account.get("gameName", link.game_name),
                tag_line=account.get("tagLine", link.tag_line),
            ))
            updated += 1
        log.warning("La API key es de otra app de Riot: actualicé el puuid de %d vinculados.", updated)
        return updated

    async def build_match_summary(self, match_id: str) -> MatchSummary | None:
        """Arma el resumen completo (10 jugadores) de una partida ya
        ingerida, para el aviso "en vivo" (ver `LolScheduler`). Devuelve None
        si falla la consulta a Riot; un jugador no debe frenar al resto."""
        links = await self._links.get_all_links()
        puuid_to_discord = {l.puuid: l.discord_id for l in links}
        try:
            data = await self._fetch_match(match_id)  # casi siempre ya está en caché
        except RiotUnavailableError as exc:
            log.warning("Riot no responde armando el resumen de %s (%s).", match_id, exc)
            return None
        except Exception:  # noqa: BLE001 - una partida no debe frenar al resto
            log.exception("Error trayendo la partida %s para el resumen del aviso.", match_id)
            return None
        return map_match_summary(data, puuid_to_discord)

    async def pending_enrichment(self, everything: bool = False) -> list[MatchRecord]:
        """Partidas guardadas sin stats extendidas o con el timeline sin
        analizar (o analizado por una versión anterior, sin las situaciones
        nuevas). Con `everything=True`, todas (recálculo de 0)."""
        return sorted(
            (r for r in await self._matches.get_all_matches()
             if everything or not r.has_extended_stats or (r.timeline_version or 0) < TIMELINE_VERSION),
            key=lambda r: r.match_id,  # las de una misma partida seguidas: aprovechan la caché
        )

    async def enrich_stored_matches(self, quiet: bool = False,
                                    everything: bool = False) -> tuple[int, int, int]:
        """Completa las partidas ya guardadas a las que les faltan las stats
        extendidas o el timeline (las guardadas antes del detector de trolls
        nuevo): las vuelve a pedir a Riot y reescribe el registro. Lo usa
        `/trolls-recalcular` y el recálculo automático del scheduler; con
        `quiet=True` no deja rastro en los logs (solo a nivel DEBUG): el único
        efecto es que la tabla troll se actualiza. Nunca dispara avisos (no
        pasa por los listeners). Con `everything=True` reanaliza TODAS las
        partidas guardadas, aunque ya estén al día (recálculo de 0).
        Devuelve (actualizadas, sin cambios, fallidas)."""
        level = logging.DEBUG if quiet else logging.WARNING
        links = {l.discord_id: l for l in await self._links.get_all_links()}
        pending = await self.pending_enrichment(everything)
        updated = unchanged = failed = 0
        for old in pending:
            try:
                data = await self._fetch_match(old.match_id)
                try:
                    timeline = await self._fetch_timeline(old.match_id, quiet=quiet)
                except RiotUnavailableError:
                    timeline = None
            except RiotAuthError:
                self._alert_expired_key()
                failed += len(pending) - updated - unchanged - failed
                break
            except RiotApiError as exc:
                log.log(level, "No pude recalcular %s (%s).", old.dedup_key, exc)
                failed += 1
                continue
            link = links.get(old.discord_id)
            record = map_match(
                data,
                link.puuid if link else old.puuid,
                old.discord_id,
                timeline=timeline,
                participant_id=old.participant_id or None,
            )
            if record is None:
                failed += 1
                continue
            record.game_name = record.game_name or old.game_name
            if record.to_dict() == old.to_dict():
                unchanged += 1
                continue
            try:
                await self._matches.update_match(record)
            except Exception:  # noqa: BLE001 - un registro no debe frenar al resto
                log.log(level, "No pude reescribir %s en el storage.", old.dedup_key, exc_info=True)
                failed += 1
                continue
            updated += 1
        log.log(logging.DEBUG if quiet else logging.INFO,
                "Recálculo de partidas: %d actualizadas, %d sin cambios, %d fallidas.",
                updated, unchanged, failed)
        return updated, unchanged, failed

    def reset_auth_alert(self) -> None:
        """Se llama al cargar una key nueva: si esa también vence, el aviso
        sale enseguida en vez de esperar el cooldown de la anterior."""
        self._last_auth_alert = None

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
            "Generá una nueva en https://developer.riotgames.com/ y cargala con "
            "`/riot-key` (no hace falta reiniciar). Si es una dev key, vence cada 24h."
        )
