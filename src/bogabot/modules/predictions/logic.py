"""Lógica pura de las predicciones (sin Discord ni Riot, testeable).

Una predicción se abre cuando varios vinculados arrancan una partida juntos.
Tiene dos opciones ("a" y "b"), cada una atada a un equipo (100 azul /
200 rojo):
  - Todos del mismo lado: a = "Ganan" (su equipo), b = "Pierden" (el otro).
  - Grupo dividido: a = "Gana azul" (100), b = "Gana rojo" (200).

Reparto estilo Twitch (parimutuel): el pozo total se reparte entre los que
acertaron, proporcional a lo que puso cada uno. Si nadie acertó, el pozo se
pierde. Los que juegan la partida solo pueden apostar a que gana su equipo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

OPTIONS = ("a", "b")
BLUE, RED = 100, 200


def other_team(team_id: int) -> int:
    return RED if team_id == BLUE else BLUE


@dataclass
class Bet:
    option: str
    amount: int


@dataclass
class Prediction:
    game_id: int
    match_id: str  # como lo guarda match-v5, ej. "LA2_1627293075"
    queue_id: int
    channel_id: int
    message_id: int
    opened_at: datetime
    close_at: datetime
    players: dict[int, int]  # discord_id -> team_id
    option_labels: dict[str, str]
    option_teams: dict[str, int]
    bets: dict[int, Bet] = field(default_factory=dict)
    closed: bool = False

    # --- Armado ---------------------------------------------------------------
    @classmethod
    def build(cls, *, game_id: int, match_id: str, queue_id: int, players: dict[int, int],
              opened_at: datetime, close_at: datetime) -> "Prediction":
        teams = set(players.values())
        if len(teams) == 1:
            team = teams.pop()
            labels = {"a": "✅ Ganan", "b": "❌ Pierden"}
            option_teams = {"a": team, "b": other_team(team)}
        else:
            labels = {"a": "🔵 Gana azul", "b": "🔴 Gana rojo"}
            option_teams = {"a": BLUE, "b": RED}
        return cls(game_id=game_id, match_id=match_id, queue_id=queue_id, channel_id=0, message_id=0,
                   opened_at=opened_at, close_at=close_at, players=players, option_labels=labels,
                   option_teams=option_teams)

    @property
    def split(self) -> bool:
        return len(set(self.players.values())) > 1

    # --- Apuestas -------------------------------------------------------------
    def is_open(self, now: datetime) -> bool:
        return not self.closed and now < self.close_at

    def bet_error(self, user_id: int, option: str, now: datetime) -> str | None:
        """Por qué `user_id` no puede apostar a `option` (None si puede)."""
        if option not in OPTIONS:
            return "Opción inválida."
        if not self.is_open(now):
            return "Las apuestas de esta partida ya cerraron."
        if user_id in self.bets:
            return "Ya apostaste en esta partida."
        team = self.players.get(user_id)
        if team is not None and self.option_teams[option] != team:
            return "Estás jugando esta partida: solo podés apostar a que gana tu equipo. 😏"
        return None

    def pools(self) -> dict[str, int]:
        pools = {o: 0 for o in OPTIONS}
        for bet in self.bets.values():
            pools[bet.option] += bet.amount
        return pools

    # --- Resolución -----------------------------------------------------------
    def winning_option(self, results: dict[int, bool]) -> str | None:
        """Qué opción ganó según `results` (discord_id -> ganó, de los
        registros de la partida). None si no hay datos de ningún jugador."""
        for discord_id, won in results.items():
            team = self.players.get(discord_id)
            if team is None:
                continue
            winner_team = team if won else other_team(team)
            return next(o for o in OPTIONS if self.option_teams[o] == winner_team)
        return None

    def payouts(self, winner: str) -> dict[int, int]:
        """Cuánto cobra cada ganador (pozo total repartido en proporción a lo
        apostado). Sin ganadores, nadie cobra."""
        total = sum(b.amount for b in self.bets.values())
        winners = {uid: b.amount for uid, b in self.bets.items() if b.option == winner}
        win_total = sum(winners.values())
        if not win_total:
            return {}
        return {uid: amount * total // win_total for uid, amount in winners.items()}

    # --- Serialización ------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id, "match_id": self.match_id, "queue_id": self.queue_id,
            "channel_id": self.channel_id, "message_id": self.message_id,
            "opened_at": self.opened_at.isoformat(), "close_at": self.close_at.isoformat(),
            "players": {str(k): v for k, v in self.players.items()},
            "option_labels": self.option_labels, "option_teams": self.option_teams,
            "bets": {str(k): {"option": b.option, "amount": b.amount} for k, b in self.bets.items()},
            "closed": self.closed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Prediction":
        return cls(
            game_id=int(d["game_id"]), match_id=d["match_id"], queue_id=int(d["queue_id"]),
            channel_id=int(d["channel_id"]), message_id=int(d["message_id"]),
            opened_at=datetime.fromisoformat(d["opened_at"]), close_at=datetime.fromisoformat(d["close_at"]),
            players={int(k): int(v) for k, v in d["players"].items()},
            option_labels=dict(d["option_labels"]), option_teams={k: int(v) for k, v in d["option_teams"].items()},
            bets={int(k): Bet(b["option"], int(b["amount"])) for k, b in d.get("bets", {}).items()},
            closed=bool(d.get("closed", False)),
        )
