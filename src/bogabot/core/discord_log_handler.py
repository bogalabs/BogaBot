"""Handler de logging que reenvía registros a un canal de Discord.

`emit()` es sync (así es la API estándar de `logging`) y no queremos mandar
un mensaje a Discord ahí mismo: no hay garantía de que haya un event loop
corriendo, y mandar un mensaje por cada línea reventaría el rate limit.
Por eso el handler solo encola texto ya formateado; un loop en `bot.py`
(`BogaBot._flush_logs`) vacía la cola cada tantos segundos y la manda como
mensaje(s) al canal configurado en `LOG_CHANNEL_ID`.
"""
from __future__ import annotations

import logging


class DiscordLogHandler(logging.Handler):
    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level)
        self._pending: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._pending.append(self.format(record))
        except Exception:
            pass  # un error acá no debe romper el logging del resto de la app

    def drain(self) -> list[str]:
        """Devuelve los mensajes encolados y vacía la cola."""
        pending, self._pending = self._pending, []
        return pending
