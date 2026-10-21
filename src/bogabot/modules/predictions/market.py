"""Mercado de apuestas genérico (lógica pura, sin Discord).

Un `Market` es una pregunta con varias opciones donde cada usuario apuesta
una sola vez a una opción. Lo usan las apuestas sobre la partida (props),
las recompensas y las apuestas semanales; cuándo abre, cierra y se resuelve
lo decide quien lo contiene.

Reparto estilo Twitch (parimutuel): el pozo total se reparte entre los que
acertaron, proporcional a lo que puso cada uno. Si nadie acertó, el pozo se
pierde.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Bet:
    option: str
    amount: int


def parimutuel(bets: dict[int, Bet], winner: str) -> dict[int, int]:
    """Cuánto cobra cada ganador: el pozo total repartido en proporción a lo
    apostado. Sin ganadores, nadie cobra."""
    total = sum(b.amount for b in bets.values())
    winners = {uid: b.amount for uid, b in bets.items() if b.option == winner}
    win_total = sum(winners.values())
    if not win_total:
        return {}
    return {uid: amount * total // win_total for uid, amount in winners.items()}


@dataclass
class Market:
    key: str
    title: str
    options: dict[str, str]  # código de opción -> etiqueta
    # Opciones que un usuario no puede elegir (ej. apostar a que él mismo
    # trollea). Si tiene prohibidas todas, no puede apostar en este mercado.
    forbidden: dict[int, list[str]] = field(default_factory=dict)
    bets: dict[int, Bet] = field(default_factory=dict)

    def bet_error(self, user_id: int, option: str) -> str | None:
        """Por qué `user_id` no puede apostar a `option` (None si puede). No
        chequea si el mercado está abierto: eso lo sabe quien lo contiene."""
        if option not in self.options:
            return "Opción inválida."
        if user_id in self.bets:
            return "Ya apostaste en esta."
        blocked = self.forbidden.get(user_id, [])
        if set(self.options) <= set(blocked):
            return "Estás metido en esta: no podés apostar acá. 😏"
        if option in blocked:
            return "Esa no la podés elegir: sería plata fácil. 😏"
        return None

    def pools(self) -> dict[str, int]:
        pools = {o: 0 for o in self.options}
        for bet in self.bets.values():
            pools[bet.option] = pools.get(bet.option, 0) + bet.amount
        return pools

    def payouts(self, winner: str) -> dict[int, int]:
        return parimutuel(self.bets, winner)

    def refunds(self) -> dict[int, int]:
        return {uid: b.amount for uid, b in self.bets.items()}

    def pools_text(self) -> str:
        """Una línea con lo apostado a cada opción, para los embeds."""
        pools = self.pools()
        return " · ".join(f"{label} **{pools[o]}**" for o, label in self.options.items())

    # --- Serialización ------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "key": self.key, "title": self.title, "options": self.options,
            "forbidden": {str(k): v for k, v in self.forbidden.items()},
            "bets": {str(k): {"option": b.option, "amount": b.amount} for k, b in self.bets.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Market":
        return cls(
            key=d["key"], title=d["title"], options=dict(d["options"]),
            forbidden={int(k): list(v) for k, v in d.get("forbidden", {}).items()},
            bets={int(k): Bet(b["option"], int(b["amount"])) for k, b in d.get("bets", {}).items()},
        )
