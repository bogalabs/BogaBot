"""Saldo de puntos por usuario, persistido en un JSON local (POINTS_FILE).

Es chico y lo escribe un solo proceso, así que un JSON alcanza: cada cambio
se escribe entero a un archivo temporal y se reemplaza atómicamente, fuera
del event loop (`asyncio.to_thread`). Un `asyncio.Lock` serializa las
operaciones para que un canje y una acreditación no se pisen.

Formato:
    {"users": {"<discord_id>": {"balance": 120, "voice_day": "2026-09-24", "voice_today": 12}}}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class PointsStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._users: dict[str, dict] = {}
        if self._path.exists():
            self._users = json.loads(self._path.read_text(encoding="utf-8")).get("users", {})
        log.info("Puntos cargados de %s (%d usuarios).", self._path, len(self._users))

    # ------------------------------------------------------------------ #
    # Lectura
    # ------------------------------------------------------------------ #
    def balance(self, user_id: int) -> int:
        return int(self._users.get(str(user_id), {}).get("balance", 0))

    def top(self, limit: int = 10) -> list[tuple[int, int]]:
        rows = [(int(uid), int(u.get("balance", 0))) for uid, u in self._users.items()]
        rows = [r for r in rows if r[1] > 0]
        return sorted(rows, key=lambda r: r[1], reverse=True)[:limit]

    # ------------------------------------------------------------------ #
    # Escritura
    # ------------------------------------------------------------------ #
    async def add(self, amounts: dict[int, int], reason: str) -> None:
        """Suma (o resta, si es negativo) puntos a varios usuarios de una.
        El saldo nunca queda por debajo de 0."""
        if not amounts:
            return
        async with self._lock:
            for uid, amount in amounts.items():
                u = self._user(uid)
                u["balance"] = max(0, u.get("balance", 0) + amount)
                log.info("Puntos %+d a %s (%s) -> %d.", amount, uid, reason, u["balance"])
            await self._save()

    async def add_voice(self, user_ids: list[int], amount: int, day: str, daily_cap: int) -> dict[int, int]:
        """Acredita puntos por tiempo en voz respetando un tope diario por
        usuario (`daily_cap` <= 0 = sin tope). Devuelve lo acreditado a cada uno."""
        awarded: dict[int, int] = {}
        async with self._lock:
            for uid in user_ids:
                u = self._user(uid)
                if u.get("voice_day") != day:
                    u["voice_day"], u["voice_today"] = day, 0
                give = amount
                if daily_cap > 0:
                    give = min(amount, daily_cap - u["voice_today"])
                if give <= 0:
                    continue
                u["voice_today"] += give
                u["balance"] = u.get("balance", 0) + give
                awarded[uid] = give
            if awarded:
                await self._save()
        return awarded

    async def spend(self, user_id: int, amount: int, reason: str) -> bool:
        """Descuenta `amount` si alcanza el saldo. Devuelve False si no alcanza."""
        async with self._lock:
            u = self._user(user_id)
            if u.get("balance", 0) < amount:
                return False
            u["balance"] -= amount
            log.info("Puntos -%d a %s (%s) -> %d.", amount, user_id, reason, u["balance"])
            await self._save()
            return True

    # ------------------------------------------------------------------ #
    # Internos
    # ------------------------------------------------------------------ #
    def _user(self, user_id: int) -> dict:
        return self._users.setdefault(str(user_id), {"balance": 0})

    async def _save(self) -> None:
        data = json.dumps({"users": self._users}, ensure_ascii=False, indent=1)
        await asyncio.to_thread(self._write, data)

    def _write(self, data: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self._path)
