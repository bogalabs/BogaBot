"""Tiradas de ruleta vivas (ROULETTE_FILE), para que un reinicio no se coma
las apuestas ni el bonus pendiente. Mismo esquema que `BetsStore`."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from bogabot.modules.roulette.logic import Spin


class RouletteStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self.lock = asyncio.Lock()
        self.spins: dict[str, Spin] = {}
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self.spins = {s["id"]: Spin.from_dict(s) for s in raw.get("spins", [])}

    async def save(self) -> None:
        data = json.dumps({"spins": [s.to_dict() for s in self.spins.values()]}, ensure_ascii=False, indent=1)
        await asyncio.to_thread(self._write, data)

    def _write(self, data: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self._path)
