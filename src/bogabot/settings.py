"""Carga y valida la configuración desde variables de entorno (.env.<ambiente>).

Toda la app depende de este objeto `Settings`. Nada de rutas ni valores
hardcodeados: si falta una variable requerida, el bot falla al arrancar con
un mensaje claro en vez de romperse a mitad de camino.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_VALID_ENVIRONMENTS = ("staging", "production")


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


def _flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"La variable '{name}' debe ser un número entero, no '{raw}'.") from exc


def _load_env_file() -> str:
    """Carga el archivo .env.<BOGABOT_ENV> correspondiente al ambiente.

    Default 'staging': si alguien se olvida de setear BOGABOT_ENV, el bot
    arranca contra el ambiente de prueba y no contra producción por accidente.
    Devuelve el nombre del ambiente cargado.
    """
    env_name = os.getenv("BOGABOT_ENV", "staging").strip().lower()
    if env_name not in _VALID_ENVIRONMENTS:
        raise ConfigError(
            f"BOGABOT_ENV='{env_name}' inválido. Usá uno de: "
            f"{', '.join(_VALID_ENVIRONMENTS)}."
        )

    env_path = _PROJECT_ROOT / f".env.{env_name}"
    if not env_path.exists():
        raise ConfigError(
            f"No encontré '{env_path.name}' en la raíz del proyecto. "
            f"Creá ese archivo a partir de '.env.example' (BOGABOT_ENV actual: "
            f"'{env_name}')."
        )

    load_dotenv(dotenv_path=env_path, override=True)
    return env_name


@dataclass(frozen=True)
class Settings:
    # Ambiente
    environment: str
    # Discord
    discord_token: str
    guild_id: int | None
    storage_channel_id: int
    ranking_channel_id: int
    general_channel_id: int
    dev_role_id: int | None
    admin_channel_id: int | None
    bot_channel_id: int | None
    player_role_id: int | None
    lol_role_id: int | None
    match_notify_channel_id: int | None
    match_poll_interval_minutes: int
    match_notify_max_age_minutes: int  # partidas más viejas se guardan pero no se avisan
    troll_channel_id: int | None  # alertas troll; si falta, van a RANKING_CHANNEL_ID
    carry_channel_id: int | None  # carreadas; si falta, van al canal de trolls
    # Riot
    riot_api_key: str
    riot_platform: str
    riot_region: str
    riot_key_file: str
    riot_key_ttl_hours: int
    riot_key_warn_minutes: int
    # Scoring
    scoring_config_path: str
    trolls_config_path: str
    trolls_state_file: str  # desde cuándo cuenta el ranking troll (/trolls-reiniciar)
    carries_config_path: str
    carries_state_file: str  # desde cuándo cuenta el ranking carry (/carries-reiniciar)
    # Tiempo / scheduler
    timezone: str
    daily_post_hour: int
    daily_post_minute: int
    # Logging
    log_level: int
    log_channel_id: int | None
    # Si está seteado, los logs van por DM a este usuario (tiene prioridad
    # sobre log_channel_id), para no molestar al resto en un canal.
    log_user_id: int | None
    log_channel_level: int
    # Sonidos (módulo de voz)
    sounds_enabled: bool
    sounds_dir: str
    sounds_chance: float
    sounds_cooldown_minutes: int
    sounds_check_interval_minutes: int
    sounds_active_from: int
    sounds_active_to: int
    # Sonidos por evento: nombre del sonido (de /sonidos) que suena en el canal
    # de voz de los que jugaron. "" = desactivado.
    sounds_event_papelon: str
    sounds_event_victory: str
    # Frase hablada (TTS) sobre quién trolleó/carreó, en el canal de voz de
    # los que jugaron. gTTS: idioma y dominio de Google (acento).
    sounds_event_voice: bool
    sounds_tts_lang: str
    sounds_tts_tld: str
    # Puntos (canjeables por sonidos temporales)
    points_role_id: int | None
    points_file: str
    points_voice_interval_minutes: int
    points_voice_amount: int
    points_voice_daily_cap: int
    points_lol_game: int
    points_lol_win: int
    sound_redeem_cost: int
    sound_redeem_days: int
    sound_redeem_max_seconds: int
    sound_redeem_max_active: int
    # Tienda de canjes de texto (módulo de puntos)
    shop_file: str
    shop_role_cost: int
    shop_role_days: int
    shop_shield_cost: int
    shop_nick_cost: int
    shop_nick_hours: int
    shop_pin_cost: int
    shop_pin_hours: int
    # Predicciones con puntos sobre partidas en vivo
    predictions_enabled: bool
    predictions_file: str
    prediction_channel_id: int | None  # si falta, van a MATCH_NOTIFY_CHANNEL_ID
    prediction_poll_minutes: int
    prediction_close_minutes: int
    prediction_min_bet: int
    prediction_max_bet: int
    props_enabled: bool  # apuestas sobre la partida (quién carrea, quién trollea, etc.)
    # /daily con racha
    daily_base: int
    daily_step: int
    daily_max: int
    # Misiones semanales
    missions_enabled: bool
    missions_file: str
    missions_per_week: int
    missions_reward: int
    missions_channel_id: int | None  # anuncios; si falta, BOT_CHANNEL_ID
    # Duelos, recompensas y apuestas semanales
    bets_file: str
    duel_max_bet: int
    duel_accept_minutes: int
    duel_match_hours: int
    bounty_days: int
    weekly_bets_enabled: bool
    weekly_bet_close_days: int
    # Ruleta (rol, campeón y build random)
    roulette_enabled: bool
    roulette_file: str
    roulette_reroll_cost: int
    roulette_punish_cost: int
    roulette_bonus: int
    roulette_bet_minutes: int
    roulette_hours: int
    roulette_cursed_chance: float
    ddragon_lang: str

    @classmethod
    def load(cls) -> "Settings":
        env_name = _load_env_file()

        level_name = os.getenv("LOG_LEVEL", "INFO").upper()
        log_level = getattr(logging, level_name, logging.INFO)

        hour = int(os.getenv("DAILY_POST_HOUR", "10"))
        if not 0 <= hour <= 23:
            raise ConfigError("DAILY_POST_HOUR debe estar entre 0 y 23.")

        minute = int(os.getenv("DAILY_POST_MINUTE", "0"))
        if not 0 <= minute <= 59:
            raise ConfigError("DAILY_POST_MINUTE debe estar entre 0 y 59.")

        poll_minutes = int(os.getenv("MATCH_POLL_INTERVAL_MINUTES", "5"))
        if poll_minutes < 1:
            raise ConfigError("MATCH_POLL_INTERVAL_MINUTES debe ser >= 1.")
        notify_max_age = int(os.getenv("MATCH_NOTIFY_MAX_AGE_MINUTES", "120"))
        if notify_max_age < 1:
            raise ConfigError("MATCH_NOTIFY_MAX_AGE_MINUTES debe ser >= 1.")

        log_channel_level_name = os.getenv("LOG_CHANNEL_LEVEL", "WARNING").upper()
        log_channel_level = getattr(logging, log_channel_level_name, logging.WARNING)

        # Default 0: el sonido random (entrar sin que nadie lo pida) molestaba.
        sounds_chance = float(os.getenv("SOUNDS_CHANCE", "0"))
        if not 0.0 <= sounds_chance <= 1.0:
            raise ConfigError("SOUNDS_CHANCE debe estar entre 0 y 1.")
        sounds_active_from = int(os.getenv("SOUNDS_ACTIVE_FROM", "0"))
        sounds_active_to = int(os.getenv("SOUNDS_ACTIVE_TO", "0"))
        if not (0 <= sounds_active_from <= 23 and 0 <= sounds_active_to <= 23):
            raise ConfigError("SOUNDS_ACTIVE_FROM / SOUNDS_ACTIVE_TO deben estar entre 0 y 23.")

        return cls(
            environment=env_name,
            discord_token=_require("DISCORD_TOKEN"),
            guild_id=_optional_int("DISCORD_GUILD_ID"),
            storage_channel_id=_require_int("STORAGE_CHANNEL_ID"),
            ranking_channel_id=_require_int("RANKING_CHANNEL_ID"),
            general_channel_id=_require_int("GENERAL_CHANNEL_ID"),
            dev_role_id=_optional_int("DEV_ROLE_ID"),
            admin_channel_id=_optional_int("ADMIN_CHANNEL_ID"),
            bot_channel_id=_optional_int("BOT_CHANNEL_ID"),
            player_role_id=_optional_int("PLAYER_ROLE_ID"),
            lol_role_id=_optional_int("LOL_ROLE_ID"),
            match_notify_channel_id=_optional_int("MATCH_NOTIFY_CHANNEL_ID"),
            match_poll_interval_minutes=poll_minutes,
            match_notify_max_age_minutes=notify_max_age,
            troll_channel_id=_optional_int("TROLL_CHANNEL_ID"),
            carry_channel_id=_optional_int("CARRY_CHANNEL_ID"),
            riot_api_key=_require("RIOT_API_KEY"),
            riot_platform=os.getenv("RIOT_PLATFORM", "la2"),
            riot_region=os.getenv("RIOT_REGION", "americas"),
            riot_key_file=os.getenv("RIOT_KEY_FILE", "data/riot_key.json"),
            riot_key_ttl_hours=max(0, int(os.getenv("RIOT_KEY_TTL_HOURS", "24"))),
            riot_key_warn_minutes=max(0, int(os.getenv("RIOT_KEY_WARN_MINUTES", "120"))),
            scoring_config_path=os.getenv("SCORING_CONFIG_PATH", "config/scoring.yaml"),
            trolls_config_path=os.getenv("TROLLS_CONFIG_PATH", "config/trolls.yaml"),
            trolls_state_file=os.getenv("TROLLS_STATE_FILE", "data/trolls_state.json"),
            carries_config_path=os.getenv("CARRIES_CONFIG_PATH", "config/carries.yaml"),
            carries_state_file=os.getenv("CARRIES_STATE_FILE", "data/carries_state.json"),
            timezone=os.getenv("TIMEZONE", "America/Argentina/Buenos_Aires"),
            daily_post_hour=hour,
            daily_post_minute=minute,
            log_level=log_level,
            log_channel_id=_optional_int("LOG_CHANNEL_ID"),
            log_user_id=_optional_int("LOG_USER_ID"),
            log_channel_level=log_channel_level,
            sounds_enabled=os.getenv("SOUNDS_ENABLED", "true").strip().lower() in ("1", "true", "yes"),
            sounds_dir=os.getenv("SOUNDS_DIR", "sounds"),
            sounds_chance=sounds_chance,
            sounds_cooldown_minutes=int(os.getenv("SOUNDS_COOLDOWN_MINUTES", "45")),
            sounds_check_interval_minutes=int(os.getenv("SOUNDS_CHECK_INTERVAL_MINUTES", "5")),
            sounds_active_from=sounds_active_from,
            sounds_active_to=sounds_active_to,
            points_role_id=_optional_int("POINTS_ROLE_ID"),
            points_file=os.getenv("POINTS_FILE", "data/points.json"),
            points_voice_interval_minutes=max(1, int(os.getenv("POINTS_VOICE_INTERVAL_MINUTES", "5"))),
            points_voice_amount=int(os.getenv("POINTS_VOICE_AMOUNT", "1")),
            points_voice_daily_cap=int(os.getenv("POINTS_VOICE_DAILY_CAP", "60")),
            points_lol_game=int(os.getenv("POINTS_LOL_GAME", "5")),
            points_lol_win=int(os.getenv("POINTS_LOL_WIN", "5")),
            sound_redeem_cost=int(os.getenv("SOUND_REDEEM_COST", "100")),
            sound_redeem_days=int(os.getenv("SOUND_REDEEM_DAYS", "7")),
            sound_redeem_max_seconds=int(os.getenv("SOUND_REDEEM_MAX_SECONDS", "5")),
            sound_redeem_max_active=int(os.getenv("SOUND_REDEEM_MAX_ACTIVE", "1")),
            sounds_event_papelon=os.getenv("SOUNDS_EVENT_PAPELON", "").strip(),
            sounds_event_victory=os.getenv("SOUNDS_EVENT_VICTORY", "").strip(),
            sounds_event_voice=os.getenv("SOUNDS_EVENT_VOICE", "true").strip().lower() in ("1", "true", "yes"),
            sounds_tts_lang=os.getenv("SOUNDS_TTS_LANG", "es").strip(),
            sounds_tts_tld=os.getenv("SOUNDS_TTS_TLD", "com.mx").strip(),
            shop_file=os.getenv("SHOP_FILE", "data/shop.json"),
            shop_role_cost=int(os.getenv("SHOP_ROLE_COST", "150")),
            shop_role_days=max(1, int(os.getenv("SHOP_ROLE_DAYS", "7"))),
            shop_shield_cost=int(os.getenv("SHOP_SHIELD_COST", "80")),
            shop_nick_cost=int(os.getenv("SHOP_NICK_COST", "60")),
            shop_nick_hours=max(1, int(os.getenv("SHOP_NICK_HOURS", "24"))),
            shop_pin_cost=int(os.getenv("SHOP_PIN_COST", "40")),
            shop_pin_hours=max(1, int(os.getenv("SHOP_PIN_HOURS", "24"))),
            predictions_enabled=os.getenv("PREDICTIONS_ENABLED", "true").strip().lower() in ("1", "true", "yes"),
            predictions_file=os.getenv("PREDICTIONS_FILE", "data/predictions.json"),
            prediction_channel_id=_optional_int("PREDICTION_CHANNEL_ID"),
            prediction_poll_minutes=max(1, int(os.getenv("PREDICTION_POLL_MINUTES", "3"))),
            prediction_close_minutes=max(1, int(os.getenv("PREDICTION_CLOSE_MINUTES", "8"))),
            prediction_min_bet=max(1, int(os.getenv("PREDICTION_MIN_BET", "5"))),
            prediction_max_bet=max(1, int(os.getenv("PREDICTION_MAX_BET", "200"))),
            props_enabled=_flag("PROPS_ENABLED"),
            daily_base=max(0, int(os.getenv("DAILY_BASE", "10"))),
            daily_step=max(0, int(os.getenv("DAILY_STEP", "5"))),
            daily_max=max(0, int(os.getenv("DAILY_MAX", "50"))),
            missions_enabled=_flag("MISSIONS_ENABLED"),
            missions_file=os.getenv("MISSIONS_FILE", "data/missions.json"),
            missions_per_week=max(1, int(os.getenv("MISSIONS_PER_WEEK", "4"))),
            missions_reward=max(0, int(os.getenv("MISSIONS_REWARD", "50"))),
            missions_channel_id=_optional_int("MISSIONS_CHANNEL_ID"),
            bets_file=os.getenv("BETS_FILE", "data/bets.json"),
            duel_max_bet=max(1, int(os.getenv("DUEL_MAX_BET", "500"))),
            duel_accept_minutes=max(1, int(os.getenv("DUEL_ACCEPT_MINUTES", "10"))),
            duel_match_hours=max(1, int(os.getenv("DUEL_MATCH_HOURS", "24"))),
            bounty_days=max(1, int(os.getenv("BOUNTY_DAYS", "3"))),
            weekly_bets_enabled=_flag("WEEKLY_BETS_ENABLED"),
            weekly_bet_close_days=min(6, max(1, int(os.getenv("WEEKLY_BET_CLOSE_DAYS", "2")))),
            roulette_enabled=_flag("ROULETTE_ENABLED"),
            roulette_file=os.getenv("ROULETTE_FILE", "data/roulette.json"),
            roulette_reroll_cost=max(0, int(os.getenv("ROULETTE_REROLL_COST", "15"))),
            roulette_punish_cost=max(0, int(os.getenv("ROULETTE_PUNISH_COST", "40"))),
            roulette_bonus=max(0, int(os.getenv("ROULETTE_BONUS", "30"))),
            roulette_bet_minutes=max(1, int(os.getenv("ROULETTE_BET_MINUTES", "20"))),
            roulette_hours=max(1, int(os.getenv("ROULETTE_HOURS", "3"))),
            roulette_cursed_chance=min(1.0, max(0.0, float(os.getenv("ROULETTE_CURSED_CHANCE", "0.1")))),
            ddragon_lang=os.getenv("DDRAGON_LANG", "es_AR"),
        )
