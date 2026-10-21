"""Lógica pura de duelos, recompensas y apuestas semanales (sin Discord).

  - Duelo: dos usuarios ponen lo mismo. Gana el que tenga mejor KDA (o más
    puntos de carreada) en la próxima partida que jueguen juntos después de
    aceptar, o lo decide una moneda. Empate = se devuelve todo.
  - Recompensa: "pongo X a que fulano trollea (o carrea) la próxima". Es un
    `Market` Sí/No: el que la crea apuesta al Sí y los demás suman a
    cualquiera de los dos lados. Se resuelve con la próxima partida del
    grupo que juegue el apuntado.
  - Apuestas semanales: quién termina primero en el ranking troll y en el de
    carreadas de la semana. Cierran a los pocos días para que no se apueste
    sabiendo el resultado.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from bogabot.core.models import MatchRecord
from bogabot.modules.predictions.market import Market

DUEL_MODES = {"coin": "🪙 Moneda", "kda": "⚔️ Mejor KDA", "carry": "⭐ Más carreada"}
BOUNTY_KINDS = {"troll": "trollea", "carry": "carrea"}
WEEKLY_KINDS = {"troll": "🤡 ¿Quién es el troll de la semana?", "carry": "⭐ ¿Quién es el carry de la semana?"}


def _opt_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# ---------------------------------------------------------------------------
# Duelos
# ---------------------------------------------------------------------------
@dataclass
class Duel:
    id: str
    challenger: int
    opponent: int
    amount: int
    mode: str  # clave de DUEL_MODES
    created_at: datetime
    channel_id: int = 0
    message_id: int = 0
    accepted_at: datetime | None = None

    @property
    def pending(self) -> bool:
        return self.accepted_at is None

    def involves(self, user_id: int) -> bool:
        return user_id in (self.challenger, self.opponent)

    def find_game(self, records: list[MatchRecord]) -> tuple[MatchRecord, MatchRecord] | None:
        """La primera partida (después de aceptar) en la que jugaron los dos."""
        if self.accepted_at is None:
            return None
        by_match: dict[str, dict[int, MatchRecord]] = {}
        for r in records:
            if self.involves(r.discord_id) and r.game_creation >= self.accepted_at:
                by_match.setdefault(r.match_id, {})[r.discord_id] = r
        games = [g for g in by_match.values() if len(g) == 2]
        if not games:
            return None
        first = min(games, key=lambda g: g[self.challenger].game_creation)
        return first[self.challenger], first[self.opponent]

    def winner(self, score: Callable[[MatchRecord], tuple], challenger: MatchRecord,
               opponent: MatchRecord) -> int | None:
        """Quién gana según `score` (más alto gana). None = empate."""
        a, b = score(challenger), score(opponent)
        if a == b:
            return None
        return self.challenger if a > b else self.opponent

    def to_dict(self) -> dict:
        return {
            "id": self.id, "challenger": self.challenger, "opponent": self.opponent, "amount": self.amount,
            "mode": self.mode, "created_at": self.created_at.isoformat(), "channel_id": self.channel_id,
            "message_id": self.message_id,
            "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Duel":
        return cls(id=d["id"], challenger=int(d["challenger"]), opponent=int(d["opponent"]),
                   amount=int(d["amount"]), mode=d["mode"], created_at=datetime.fromisoformat(d["created_at"]),
                   channel_id=int(d.get("channel_id", 0)), message_id=int(d.get("message_id", 0)),
                   accepted_at=_opt_dt(d.get("accepted_at")))


# ---------------------------------------------------------------------------
# Recompensas
# ---------------------------------------------------------------------------
def bounty_market(target: int, target_name: str, kind: str) -> Market:
    if kind == "troll":
        title, yes, no, blocked = f"🤡 ¿{target_name} trollea la próxima?", "🤡 Sí", "😇 No", "yes"
    else:
        title, yes, no, blocked = f"⭐ ¿{target_name} carrea la próxima?", "⭐ Sí", "🥔 No", "no"
    # El apuntado no puede apostar a lo que puede forzar (trollear o no carrear a propósito).
    return Market(key=f"bounty:{kind}:{target}", title=title, options={"yes": yes, "no": no},
                  forbidden={target: [blocked]})


@dataclass
class Bounty:
    id: str
    target: int
    kind: str  # clave de BOUNTY_KINDS
    creator: int
    created_at: datetime
    market: Market
    channel_id: int = 0
    message_id: int = 0
    # Partida que la resuelve: se fija cuando se detecta al apuntado en vivo
    # (ahí cierran las apuestas). Sin detección, vale su próxima partida.
    match_id: str | None = None
    locked_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.match_id is None

    def accepts(self, record: MatchRecord) -> bool:
        if record.discord_id != self.target:
            return False
        if self.match_id is not None:
            return record.match_id == self.match_id
        return record.game_creation >= self.created_at

    def to_dict(self) -> dict:
        return {
            "id": self.id, "target": self.target, "kind": self.kind, "creator": self.creator,
            "created_at": self.created_at.isoformat(), "market": self.market.to_dict(),
            "channel_id": self.channel_id, "message_id": self.message_id, "match_id": self.match_id,
            "locked_at": self.locked_at.isoformat() if self.locked_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Bounty":
        return cls(id=d["id"], target=int(d["target"]), kind=d["kind"], creator=int(d["creator"]),
                   created_at=datetime.fromisoformat(d["created_at"]), market=Market.from_dict(d["market"]),
                   channel_id=int(d.get("channel_id", 0)), message_id=int(d.get("message_id", 0)),
                   match_id=d.get("match_id"), locked_at=_opt_dt(d.get("locked_at")))


# ---------------------------------------------------------------------------
# Apuestas semanales
# ---------------------------------------------------------------------------
def build_weekly(players: dict[int, str]) -> dict[str, Market]:
    """Un mercado por ranking, con una opción por jugador (máx. 25, lo que
    entra en un desplegable). Nadie apuesta a ser él el troll de la semana."""
    options = {str(uid): name for uid, name in list(players.items())[:25]}
    return {
        "troll": Market(key="troll", title=WEEKLY_KINDS["troll"], options=dict(options),
                        forbidden={int(uid): [uid] for uid in options}),
        "carry": Market(key="carry", title=WEEKLY_KINDS["carry"], options=dict(options)),
    }


def weekly_winner(rows: list[tuple[int, int]]) -> str | None:
    """Opción ganadora a partir del ranking final [(discord_id, puntos), ...]
    ya ordenado. None si nadie sumó puntos (se devuelve todo)."""
    if not rows or rows[0][1] <= 0:
        return None
    return str(rows[0][0])


@dataclass
class WeeklyBook:
    week: str  # lunes de la semana, ISO (ej. "2026-10-19")
    close_at: datetime
    markets: dict[str, Market]
    channel_id: int = 0
    message_id: int = 0
    closed: bool = False

    @property
    def week_start(self) -> date:
        return date.fromisoformat(self.week)

    def to_dict(self) -> dict:
        return {"week": self.week, "close_at": self.close_at.isoformat(),
                "markets": [m.to_dict() for m in self.markets.values()],
                "channel_id": self.channel_id, "message_id": self.message_id, "closed": self.closed}

    @classmethod
    def from_dict(cls, d: dict) -> "WeeklyBook":
        return cls(week=d["week"], close_at=datetime.fromisoformat(d["close_at"]),
                   markets={m["key"]: Market.from_dict(m) for m in d["markets"]},
                   channel_id=int(d.get("channel_id", 0)), message_id=int(d.get("message_id", 0)),
                   closed=bool(d.get("closed", False)))

