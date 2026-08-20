"""Utilidades de tiempo. Los cortes de "día" y "semana" dependen de la zona
horaria configurada (TIMEZONE), no de la del servidor donde corra el bot.

Convención del grupo: la semana empieza el DOMINGO 00:00 hora local.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def get_tz(tz_name: str) -> ZoneInfo:
    return ZoneInfo(tz_name)


def now(tz_name: str) -> datetime:
    return datetime.now(get_tz(tz_name))


def start_of_day(tz_name: str, ref: datetime | None = None) -> datetime:
    tz = get_tz(tz_name)
    ref = ref.astimezone(tz) if ref else datetime.now(tz)
    return ref.replace(hour=0, minute=0, second=0, microsecond=0)


def start_of_week(tz_name: str, ref: datetime | None = None) -> datetime:
    """Domingo 00:00 de la semana en curso, en hora local."""
    tz = get_tz(tz_name)
    ref = ref.astimezone(tz) if ref else datetime.now(tz)
    midnight = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    # weekday(): lunes=0 ... domingo=6  ->  días desde el domingo pasado:
    days_since_sunday = (ref.weekday() + 1) % 7
    return midnight - timedelta(days=days_since_sunday)


def is_week_start_day(tz_name: str, ref: datetime | None = None) -> bool:
    """True si hoy (hora local) es domingo, el día en que arranca la semana."""
    tz = get_tz(tz_name)
    ref = ref.astimezone(tz) if ref else datetime.now(tz)
    return ref.weekday() == 6  # domingo


def to_epoch_seconds(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())


def from_epoch_millis(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
