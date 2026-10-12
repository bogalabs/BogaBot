"""Predicciones abiertas (o cerradas sin resolver) persistidas en un JSON
(PREDICTIONS_FILE), para que un reinicio del bot no se coma las apuestas:
los puntos ya se descontaron al apostar. Mismo esquema que `PointsStore`."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from bogabot.modules.predictions.logic import Prediction


class PredictionStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self.lock = asyncio.Lock()  # lo usa el cog para serializar apuestas
        self._items: dict[int, Prediction] = {}
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._items = {int(p["game_id"]): Prediction.from_dict(p) for p in raw.get("predictions", [])}

    def get(self, game_id: int) -> Prediction | None:
        return self._items.get(game_id)

    def all(self) -> list[Prediction]:
        return list(self._items.values())

    async def put(self, prediction: Prediction) -> None:
        self._items[prediction.game_id] = prediction
        await self._save()

    async def remove(self, game_id: int) -> None:
        if self._items.pop(game_id, None) is not None:
            await self._save()

    async def _save(self) -> None:
        data = json.dumps({"predictions": [p.to_dict() for p in self._items.values()]},
                          ensure_ascii=False, indent=1)
        await asyncio.to_thread(self._write, data)

    def _write(self, data: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self._path)
