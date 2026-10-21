"""Estado de la tienda de canjes (SHOP_FILE): efectos temporales activos
(rol custom, apodo, mensaje fijado) y cargas de inmunidad troll.

Mismo criterio que `PointsStore`: JSON chico escrito por un solo proceso,
reemplazo atómico fuera del event loop y un `asyncio.Lock` para no pisarse.

Formato:
    {"effects": [{"id": "role-123-1727...", "kind": "role", "user_id": 123,
                  "target_id": 123, "expires_at": "2026-...", "data": {...}}],
     "shields": {"<discord_id>": 1}}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ShopEffect:
    """Un canje con vencimiento. `user_id` es quien pagó; `target_id`, a
    quién afecta (el mismo, salvo el apodo que se le pone a otro)."""

    id: str
    kind: str  # "role" | "nick" | "pin"
    user_id: int
    target_id: int
    expires_at: datetime
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expires_at"] = self.expires_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ShopEffect":
        return cls(id=d["id"], kind=d["kind"], user_id=int(d["user_id"]), target_id=int(d["target_id"]),
                   expires_at=datetime.fromisoformat(d["expires_at"]), data=dict(d.get("data", {})))


class ShopStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._effects: dict[str, ShopEffect] = {}
        self._shields: dict[str, int] = {}
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._effects = {e["id"]: ShopEffect.from_dict(e) for e in raw.get("effects", [])}
            self._shields = {k: int(v) for k, v in raw.get("shields", {}).items()}

    # --- Efectos temporales -------------------------------------------------
    def find(self, kind: str, *, user_id: int | None = None, target_id: int | None = None) -> ShopEffect | None:
        for e in self._effects.values():
            if e.kind == kind and (user_id is None or e.user_id == user_id) \
                    and (target_id is None or e.target_id == target_id):
                return e
        return None

    def all(self, kind: str) -> list[ShopEffect]:
        return [e for e in self._effects.values() if e.kind == kind]

    def due(self, now: datetime) -> list[ShopEffect]:
        return [e for e in self._effects.values() if e.expires_at <= now]

    async def add(self, effect: ShopEffect) -> None:
        async with self._lock:
            self._effects[effect.id] = effect
            await self._save()

    async def remove(self, effect_id: str) -> None:
        async with self._lock:
            if self._effects.pop(effect_id, None) is not None:
                await self._save()

    # --- Inmunidad troll ----------------------------------------------------
    def shields(self, user_id: int) -> int:
        return self._shields.get(str(user_id), 0)

    async def add_shield(self, user_id: int) -> int:
        async with self._lock:
            self._shields[str(user_id)] = self.shields(user_id) + 1
            await self._save()
            return self._shields[str(user_id)]

    async def use_shield(self, user_id: int) -> bool:
        """Gasta una carga de inmunidad si tiene. True si la gastó."""
        async with self._lock:
            left = self.shields(user_id)
            if left <= 0:
                return False
            if left == 1:
                del self._shields[str(user_id)]
            else:
                self._shields[str(user_id)] = left - 1
            await self._save()
            return True

    # --- Internos -----------------------------------------------------------
    async def _save(self) -> None:
        data = json.dumps({"effects": [e.to_dict() for e in self._effects.values()], "shields": self._shields},
                          ensure_ascii=False, indent=1)
        await asyncio.to_thread(self._write, data)

    def _write(self, data: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self._path)
