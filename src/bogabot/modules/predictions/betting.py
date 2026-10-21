"""Piezas de Discord compartidas por todo lo que apuesta sobre un `Market`
(props de la partida, recompensas, apuestas semanales): el modal del monto,
el selector de opción y el cobro de la apuesta."""
from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

import discord

from bogabot.modules.points.cog import has_role
from bogabot.modules.predictions.market import Bet, Market

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

OnAmount = Callable[[discord.Interaction, str], Awaitable[None]]
OnOption = Callable[[discord.Interaction, str], Awaitable[None]]


class AmountModal(discord.ui.Modal):
    amount: discord.ui.TextInput = discord.ui.TextInput(label="¿Cuántos puntos?", max_length=6)

    def __init__(self, title: str, min_bet: int, max_bet: int, on_amount: OnAmount) -> None:
        super().__init__(title=title[:45])
        self.on_amount = on_amount
        self.amount.placeholder = f"Entre {min_bet} y {max_bet}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.on_amount(interaction, self.amount.value)


class OptionPicker(discord.ui.View):
    """Botones efímeros con las opciones de un mercado (para cuando el
    mensaje público solo tiene un desplegable de mercados)."""

    def __init__(self, market: Market, on_option: OnOption) -> None:
        super().__init__(timeout=180)
        for code, label in list(market.options.items())[:25]:
            button = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.primary)
            button.callback = self._make_callback(code, on_option)
            self.add_item(button)

    @staticmethod
    def _make_callback(code: str, on_option: OnOption):
        async def callback(interaction: discord.Interaction) -> None:
            await on_option(interaction, code)
        return callback


def display_name(bot: "BogaBot", user_id: int, fallback: str) -> str:
    """Apodo en el server si el miembro está en caché; si no, `fallback`."""
    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member is not None:
            return member.display_name
    return fallback or f"Jugador {user_id}"


def bettor_error(bot: "BogaBot", user: discord.abc.User, min_bet: int) -> str | None:
    """Por qué `user` no puede apostar nada (rol o saldo), o None."""
    if not isinstance(user, discord.Member) or not has_role(user, bot.settings.points_role_id):
        return "Necesitás el rol de Boguero para apostar."
    balance = bot.points.balance(user.id)
    if balance < min_bet:
        return f"Tenés **{balance}** puntos y la apuesta mínima es {min_bet}."
    return None


def amount_modal(bot: "BogaBot", user_id: int, title: str, on_amount: OnAmount) -> AmountModal:
    s = bot.settings
    max_bet = min(s.prediction_max_bet, bot.points.balance(user_id))
    return AmountModal(title, s.prediction_min_bet, max_bet, on_amount)


async def charge_bet(bot: "BogaBot", user_id: int, market: Market, option: str, raw: str,
                     reason: str) -> str | None:
    """Valida el monto, cobra y registra la apuesta en `market`. Devuelve el
    error a mostrar, o None si salió. Llamar con el lock del dueño del
    mercado tomado y guardar después."""
    s = bot.settings
    try:
        amount = int(raw.strip())
    except ValueError:
        return "Eso no es un número. 🤨"
    error = market.bet_error(user_id, option)
    if error is None and not s.prediction_min_bet <= amount <= s.prediction_max_bet:
        error = f"La apuesta tiene que ser entre {s.prediction_min_bet} y {s.prediction_max_bet}."
    if error is None and not await bot.points.spend(user_id, amount, reason):
        error = f"No te alcanza: tenés **{bot.points.balance(user_id)}** puntos."
    if error:
        return error
    market.bets[user_id] = Bet(option, amount)
    await bot.missions.progress([user_id], "bets")
    return None


def bet_done_text(bot: "BogaBot", user_id: int, market: Market, option: str) -> str:
    amount = market.bets[user_id].amount
    return (f"🎲 Apostaste **{amount}** a **{market.options[option]}** ({market.title}). "
            f"Te quedan {bot.points.balance(user_id)} puntos.")
