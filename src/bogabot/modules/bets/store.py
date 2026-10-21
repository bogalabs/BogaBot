"""Duelos, recompensas y apuestas semanales vivos, persistidos en un JSON
(BETS_FILE) para que un reinicio no se coma lo apostado: los puntos ya se
descontaron al apostar. Mismo esquema que `PredictionStore`."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from bogabot.modules.bets.logic import Bounty, Duel, WeeklyBook


class BetsStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self.lock = asyncio.Lock()  # lo usa el cog para serializar apuestas y pagos
        self.duels: dict[str, Duel] = {}
        self.bounties: dict[str, Bounty] = {}
        self.weekly: dict[str, WeeklyBook] = {}
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self.duels = {d["id"]: Duel.from_dict(d) for d in raw.get("duels", [])}
            self.bounties = {b["id"]: Bounty.from_dict(b) for b in raw.get("bounties", [])}
            self.weekly = {w["week"]: WeeklyBook.from_dict(w) for w in raw.get("weekly", [])}

    async def save(self) -> None:
        data = json.dumps({
            "duels": [d.to_dict() for d in self.duels.values()],
            "bounties": [b.to_dict() for b in self.bounties.values()],
            "weekly": [w.to_dict() for w in self.weekly.values()],
        }, ensure_ascii=False, indent=1)
        await asyncio.to_thread(self._write, data)

    def _write(self, data: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self._path)
