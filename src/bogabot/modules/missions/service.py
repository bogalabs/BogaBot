"""Progreso de las misiones semanales, persistido en un JSON (MISSIONS_FILE).

`bot.missions` se crea siempre (en `bot.py`) para que cualquier módulo pueda
reportar progreso sin chequear nada: si las misiones están apagadas,
`progress()` no hace nada. Al completar una misión se paga MISSIONS_REWARD
y se avisa al callback de `set_on_complete` (el cog la anuncia).

Formato:
    {"week": "2026-10-19", "users": {"<discord_id>": {"progress": {"games": 2}, "done": ["voice"]}}}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from bogabot.core.timeutils import start_of_week
from bogabot.modules.missions.logic import Mission, pick_week
from bogabot.modules.points.store import PointsStore

log = logging.getLogger(__name__)

OnComplete = Callable[[int, Mission, int], Awaitable[None]]  # (discord_id, misión, premio)


class MissionsService:
    def __init__(self, path: str | Path, points: PointsStore, *, enabled: bool, timezone: str,
                 per_week: int, reward: int) -> None:
        self._path = Path(path)
        self._points = points
        self.enabled = enabled
        self._tz = timezone
        self.per_week = per_week
        self.reward = reward
        self._lock = asyncio.Lock()
        self._on_complete: OnComplete | None = None
        self._week = ""
        self._users: dict[str, dict] = {}
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._week, self._users = raw.get("week", ""), raw.get("users", {})

    def set_on_complete(self, callback: OnComplete | None) -> None:
        self._on_complete = callback

    def current_week(self) -> str:
        return start_of_week(self._tz).date().isoformat()

    def missions(self) -> list[Mission]:
        return pick_week(self.current_week(), self.per_week)

    def status(self, user_id: int) -> list[tuple[Mission, int, bool]]:
        """(misión, progreso, completada) de cada misión de esta semana."""
        u = self._users.get(str(user_id), {}) if self._week == self.current_week() else {}
        return [(m, int(u.get("progress", {}).get(m.code, 0)), m.code in u.get("done", []))
                for m in self.missions()]

    async def progress(self, user_ids: Iterable[int], code: str, amount: int = 1) -> None:
        """Suma `amount` a la misión `code` de cada usuario (si es de esta
        semana) y paga las que se completen."""
        if not self.enabled or amount <= 0:
            return
        mission = next((m for m in self.missions() if m.code == code), None)
        if mission is None:
            return
        completed: list[int] = []
        async with self._lock:
            week = self.current_week()
            if self._week != week:
                self._week, self._users = week, {}
            for uid in set(user_ids):
                u = self._users.setdefault(str(uid), {"progress": {}, "done": []})
                if code in u["done"]:
                    continue
                u["progress"][code] = u["progress"].get(code, 0) + amount
                if u["progress"][code] >= mission.goal:
                    u["done"].append(code)
                    completed.append(uid)
            await self._save()
        if not completed:
            return
        await self._points.add({uid: self.reward for uid in completed}, f"misión {code}")
        for uid in completed:
            log.info("Misión '%s' completada por %s (+%d).", code, uid, self.reward)
            if self._on_complete is not None:
                try:
                    await self._on_complete(uid, mission, self.reward)
                except Exception:  # noqa: BLE001 - el anuncio no puede romper a quien reporta
                    log.exception("No pude anunciar la misión completada.")

    async def _save(self) -> None:
        data = json.dumps({"week": self._week, "users": self._users}, ensure_ascii=False, indent=1)
        await asyncio.to_thread(self._write, data)

    def _write(self, data: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self._path)
