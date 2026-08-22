"""Carga y valida la configuración desde variables de entorno (.env).

Toda la app depende de este objeto `Settings`. Nada de rutas ni valores
hardcodeados: si falta una variable requerida, el bot falla al arrancar con
un mensaje claro en vez de romperse a mitad de camino.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Se lanza cuando falta o es inválida una variable de entorno requerida."""


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(
            f"Falta la variable de entorno requerida '{name}'. "
            f"Revisá tu archivo .env (podés partir de .env.example)."
        )
    return value


def _require_int(name: str) -> int:
    raw = _require(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"La variable '{name}' debe ser un número entero, no '{raw}'.") from exc


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"La variable '{name}' debe ser un número entero, no '{raw}'.") from exc


@dataclass(frozen=True)
class Settings:
    # Discord
    discord_token: str
    guild_id: int | None
    storage_channel_id: int
    ranking_channel_id: int
    general_channel_id: int
    # Riot
    riot_api_key: str
    riot_platform: str
    riot_region: str
    # Scoring
    scoring_config_path: str
    # Tiempo / scheduler
    timezone: str
    daily_post_hour: int
    # Logging
    log_level: int

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()  # lee .env de la raíz del proyecto si existe

        level_name = os.getenv("LOG_LEVEL", "INFO").upper()
        log_level = getattr(logging, level_name, logging.INFO)

        hour = int(os.getenv("DAILY_POST_HOUR", "10"))
        if not 0 <= hour <= 23:
            raise ConfigError("DAILY_POST_HOUR debe estar entre 0 y 23.")

        return cls(
            discord_token=_require("DISCORD_TOKEN"),
            guild_id=_optional_int("DISCORD_GUILD_ID"),
            storage_channel_id=_require_int("STORAGE_CHANNEL_ID"),
            ranking_channel_id=_require_int("RANKING_CHANNEL_ID"),
            general_channel_id=_require_int("GENERAL_CHANNEL_ID"),
            riot_api_key=_require("RIOT_API_KEY"),
            riot_platform=os.getenv("RIOT_PLATFORM", "la2"),
            riot_region=os.getenv("RIOT_REGION", "americas"),
            scoring_config_path=os.getenv("SCORING_CONFIG_PATH", "config/scoring.yaml"),
            timezone=os.getenv("TIMEZONE", "America/Argentina/Buenos_Aires"),
            daily_post_hour=hour,
            log_level=log_level,
        )
