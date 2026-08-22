"""Cliente async de la Riot API (aiohttp) con rate limiting.

Por qué aiohttp directo y no un wrapper (ej. Cassiopeia):
  - Todo el bot es async (discord.py); un cliente sync bloquearía el event loop.
  - El volumen de un grupo de amigos es bajísimo: no necesitamos la maquinaria
    (cache, ORM) de un wrapper pesado.
  - No nos atamos a las decisiones de un tercero.

Rate limits de una dev key: ~20 req/s y ~100 req/2min. El `RateLimiter`
respeta ambas ventanas; además reintentamos ante un 429 usando Retry-After.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote

import aiohttp

from bogabot.settings import Settings

log = logging.getLogger(__name__)


class RiotApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Riot API {status}: {message}")
        self.status = status


class NotFoundError(RiotApiError):
    """404: recurso inexistente (ej. Riot ID que no existe)."""


class RateLimiter:
    """Limitador por ventanas deslizantes. Serializa la espera con un lock,
    lo cual es más que suficiente para el volumen esperado."""

    def __init__(self, limits: tuple[tuple[int, float], ...] = ((20, 1.0), (100, 120.0))) -> None:
        self._limits = limits
        self._window = max(w for _, w in limits)
        self._calls: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < self._window]
                wait = 0.0
                for count, window in self._limits:
                    recent = [t for t in self._calls if now - t < window]
                    if len(recent) >= count:
                        wait = max(wait, window - (now - recent[0]))
                if wait <= 0:
                    self._calls.append(now)
                    return
                await asyncio.sleep(wait + 0.01)


class RiotClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._region = settings.riot_region  # americas (routing regional)
        self._session: aiohttp.ClientSession | None = None
        self._limiter = RateLimiter()

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"X-Riot-Token": self._settings.riot_api_key}
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _base(self) -> str:
        return f"https://{self._region}.api.riotgames.com"

    async def _get(self, url: str, params: dict | None = None, _retries: int = 3):
        if self._session is None:
            raise RuntimeError("RiotClient no inicializado: llamá a start() primero.")
        await self._limiter.acquire()
        async with self._session.get(url, params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 404:
                raise NotFoundError(404, await resp.text())
            if resp.status == 429 and _retries > 0:
                retry_after = float(resp.headers.get("Retry-After", "1"))
                log.warning("Rate limited por Riot; espero %.1fs y reintento.", retry_after)
                await asyncio.sleep(retry_after)
                return await self._get(url, params, _retries - 1)
            raise RiotApiError(resp.status, await resp.text())

    # --- account-v1 --------------------------------------------------------
    async def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        """Devuelve {'puuid', 'gameName', 'tagLine'} o lanza NotFoundError."""
        url = (
            f"{self._base()}/riot/account/v1/accounts/by-riot-id/"
            f"{quote(game_name)}/{quote(tag_line)}"
        )
        return await self._get(url)

    # --- match-v5 ----------------------------------------------------------
    async def get_match_ids(self, puuid: str, start_time: int, count: int = 100) -> list[str]:
        """IDs de partidas del jugador desde `start_time` (epoch segundos)."""
        url = f"{self._base()}/lol/match/v5/matches/by-puuid/{puuid}/ids"
        return await self._get(url, params={"startTime": start_time, "count": count})

    async def get_match(self, match_id: str) -> dict:
        url = f"{self._base()}/lol/match/v5/matches/{match_id}"
        return await self._get(url)

    async def get_match_timeline(self, match_id: str) -> dict:
        url = f"{self._base()}/lol/match/v5/matches/{match_id}/timeline"
        return await self._get(url)
