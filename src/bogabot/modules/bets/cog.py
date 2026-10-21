"""Duelos 1v1, recompensas y apuestas semanales (ver `logic.py`).

  - /desafio <usuario> <puntos> <modo>: el retado acepta o rechaza con
    botones (DUEL_ACCEPT_MINUTES para aceptar). Moneda se resuelve al
    aceptar; KDA y carreada, con la próxima partida que jueguen juntos
    (listener de IngestService). Sin partida en DUEL_MATCH_HOURS, se devuelve.
  - /recompensa <usuario> <puntos> <tipo>: abre (o suma a) un Sí/No sobre si
    el apuntado trollea o carrea la próxima. Las apuestas cierran cuando el
    módulo de predicciones lo ve arrancar una partida con el grupo
    (`PredictionsCog.add_game_listener`) y se resuelve con esa partida. Sin
    partida en BOUNTY_DAYS, se devuelve todo.
  - Apuestas semanales: weekly_job publica los lunes en el canal de
    predicciones quién va a ser el troll y el carry de la semana. Cierran a
    los WEEKLY_BET_CLOSE_DAYS días y se resuelven el lunes siguiente con los
    rankings de esa semana.

Los botones y desplegables son `DynamicItem`: sobreviven reinicios (todo
vive en BETS_FILE).
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import re
import secrets
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bogabot.core.models import MatchRecord, TrollLevel
from bogabot.core.timeutils import get_tz, start_of_week
from bogabot.modules.bets.logic import (BOUNTY_KINDS, DUEL_MODES, Bounty, Duel, WeeklyBook, bounty_market,
                                        build_weekly, weekly_winner)
from bogabot.modules.bets.store import BetsStore
from bogabot.modules.points.cog import has_role
from bogabot.modules.predictions.betting import (amount_modal, bet_done_text, bettor_error, charge_bet,
                                                 display_name)
from bogabot.modules.predictions.market import Market

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

log = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()
_USER_MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)
_LOCK_TIMEOUT = dt.timedelta(hours=3)  # partida fijada que nunca se guardó (remake): se libera
_WEEKLY_GRACE = dt.timedelta(hours=2)  # margen el lunes para ingerir lo del domingo a la noche


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class DuelButton(discord.ui.DynamicItem[discord.ui.Button], template=r"duel:(?P<id>[0-9a-f]+):(?P<act>acc|rej)"):
    def __init__(self, duel_id: str, action: str, disabled: bool = False) -> None:
        label, style = ("Aceptar", discord.ButtonStyle.success) if action == "acc" \
            else ("Rechazar", discord.ButtonStyle.secondary)
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"duel:{duel_id}:{action}",
                                           disabled=disabled))
        self.duel_id, self.action = duel_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str]) -> "DuelButton":
        return cls(match["id"], match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("BetsCog")  # type: ignore[union-attr]
        if cog is not None:
            await cog.on_duel_click(interaction, self.duel_id, self.action == "acc")


class BountyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"bnt:(?P<id>[0-9a-f]+):(?P<opt>yes|no)"):
    def __init__(self, bounty_id: str, option: str, label: str = "Apostar", disabled: bool = False) -> None:
        style = discord.ButtonStyle.danger if option == "yes" else discord.ButtonStyle.success
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"bnt:{bounty_id}:{option}",
                                           disabled=disabled))
        self.bounty_id, self.option = bounty_id, option

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str]) -> "BountyButton":
        return cls(match["id"], match["opt"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("BetsCog")  # type: ignore[union-attr]
        if cog is not None:
            await cog.on_bounty_click(interaction, self.bounty_id, self.option)


class WeeklySelect(discord.ui.DynamicItem[discord.ui.Select], template=r"wk:(?P<week>[0-9-]+):(?P<kind>troll|carry)"):
    def __init__(self, week: str, kind: str, market: Market | None = None, disabled: bool = False) -> None:
        options = [discord.SelectOption(label=label[:100], value=code)
                   for code, label in (market.options.items() if market else [])]
        placeholder = market.title[:150] if market else "Elegí"
        super().__init__(discord.ui.Select(placeholder=placeholder, custom_id=f"wk:{week}:{kind}",
                                           options=options or [discord.SelectOption(label="-", value="-")],
                                           disabled=disabled))
        self.week, self.kind = week, kind

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select,
                             match: re.Match[str]) -> "WeeklySelect":
        return cls(match["week"], match["kind"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("BetsCog")  # type: ignore[union-attr]
        if cog is not None and self.item.values:
            await cog.on_weekly_pick(interaction, self.week, self.kind, self.item.values[0])


class BetsCog(commands.Cog):
    def __init__(self, bot: "BogaBot", store: BetsStore) -> None:
        self.bot = bot
        self.store = store

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(DuelButton, BountyButton, WeeklySelect)
        self.bot.ingest.add_listener(self._on_new_matches)
        predictions = self.bot.get_cog("PredictionsCog")
        if predictions is not None:
            predictions.add_game_listener(self._on_live_game)
        self.tick_job.start()

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(DuelButton, BountyButton, WeeklySelect)
        self.bot.ingest.remove_listener(self._on_new_matches)
        predictions = self.bot.get_cog("PredictionsCog")
        if predictions is not None:
            predictions.remove_game_listener(self._on_live_game)
        self.tick_job.cancel()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _can_play(self, member: discord.abc.User) -> bool:
        return isinstance(member, discord.Member) and has_role(member, self.bot.settings.points_role_id)

    async def _edit(self, channel_id: int, message_id: int, **kwargs) -> None:
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel) or not message_id:
            return
        try:
            await channel.get_partial_message(message_id).edit(allowed_mentions=_NO_MENTIONS, **kwargs)
        except discord.HTTPException as exc:
            log.warning("No pude editar el mensaje de apuestas %s (%s).", message_id, exc)

    async def _weekly_channel(self) -> discord.TextChannel | None:
        s = self.bot.settings
        channel_id = s.prediction_channel_id or s.match_notify_channel_id
        channel = self.bot.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    # ------------------------------------------------------------------ #
    # Duelos
    # ------------------------------------------------------------------ #
    def _duel_embed(self, d: Duel, status: str, color: discord.Color | None = None) -> discord.Embed:
        desc = (f"<@{d.challenger}> desafía a <@{d.opponent}> por **{d.amount}** puntos cada uno.\n"
                f"Modo: **{DUEL_MODES[d.mode]}**")
        if d.mode != "coin":
            desc += " en la próxima partida que jueguen juntos."
        return discord.Embed(title="⚔️ Duelo", description=f"{desc}\n\n{status}",
                             color=color or discord.Color.orange())

    def _duel_view(self, d: Duel, disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(DuelButton(d.id, "acc", disabled))
        view.add_item(DuelButton(d.id, "rej", disabled))
        return view

    @app_commands.command(name="desafio", description="Desafiá a alguien a un duelo de puntos.")
    @app_commands.describe(usuario="A quién desafiás.", puntos="Cuánto pone cada uno.",
                           modo="Moneda, o quién juega mejor la próxima partida juntos.")
    @app_commands.choices(modo=[app_commands.Choice(name=label, value=code) for code, label in DUEL_MODES.items()])
    async def desafio(self, interaction: discord.Interaction, usuario: discord.Member, puntos: int,
                      modo: app_commands.Choice[str]) -> None:
        s = self.bot.settings
        me = interaction.user
        error = None
        if not self._can_play(me) or not self._can_play(usuario):
            error = "Los dos necesitan el rol de Boguero."
        elif usuario.id == me.id or usuario.bot:
            error = "Desafiate a alguien de verdad. 🙃"
        elif not s.prediction_min_bet <= puntos <= s.duel_max_bet:
            error = f"El duelo tiene que ser entre {s.prediction_min_bet} y {s.duel_max_bet} puntos."
        elif any(d.involves(me.id) and d.involves(usuario.id) for d in self.store.duels.values()):
            error = "Ya tienen un duelo abierto entre ustedes."
        elif modo.value != "coin" and (await self.bot.storage.get_link(me.id) is None
                                       or await self.bot.storage.get_link(usuario.id) is None):
            error = "Para un duelo por partida los dos tienen que tener la cuenta vinculada (`/link`)."
        if error is None and self.bot.points.balance(usuario.id) < puntos:
            error = f"{usuario.display_name} no tiene {puntos} puntos."
        if error is None and not await self.bot.points.spend(me.id, puntos, "duelo"):
            error = f"No te alcanza: tenés **{self.bot.points.balance(me.id)}** puntos."
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        d = Duel(id=secrets.token_hex(4), challenger=me.id, opponent=usuario.id, amount=puntos, mode=modo.value,
                 created_at=_now())
        until = d.created_at + dt.timedelta(minutes=s.duel_accept_minutes)
        await interaction.response.send_message(
            content=usuario.mention,
            embed=self._duel_embed(d, f"⏳ {usuario.mention}, tenés hasta {discord.utils.format_dt(until, 'R')} "
                                      "para aceptar."),
            view=self._duel_view(d), allowed_mentions=_USER_MENTIONS)
        msg = await interaction.original_response()
        d.channel_id, d.message_id = msg.channel.id, msg.id
        async with self.store.lock:
            self.store.duels[d.id] = d
            await self.store.save()
        await self.bot.missions.progress([me.id], "bets")

    async def on_duel_click(self, interaction: discord.Interaction, duel_id: str, accept: bool) -> None:
        uid = interaction.user.id
        async with self.store.lock:
            d = self.store.duels.get(duel_id)
            if d is None or not d.pending:
                await interaction.response.send_message("Este duelo ya no está abierto.", ephemeral=True)
                return
            if uid != d.opponent and not (uid == d.challenger and not accept):
                await interaction.response.send_message("Este duelo no es con vos.", ephemeral=True)
                return
            if not accept:
                await self.bot.points.add({d.challenger: d.amount}, "duelo rechazado")
                del self.store.duels[d.id]
                await self.store.save()
                who = "lo canceló" if uid == d.challenger else "lo rechazó"
                await interaction.response.edit_message(
                    content=None, embed=self._duel_embed(d, f"🐔 <@{uid}> {who}. Se devolvieron los puntos.",
                                                         discord.Color.dark_grey()), view=None)
                return
            if not await self.bot.points.spend(uid, d.amount, "duelo"):
                await interaction.response.send_message(
                    f"No te alcanza: tenés **{self.bot.points.balance(uid)}** puntos.", ephemeral=True)
                return
            d.accepted_at = _now()
            if d.mode == "coin":
                winner = random.choice((d.challenger, d.opponent))
                await self.bot.points.add({winner: d.amount * 2}, "duelo moneda")
                del self.store.duels[d.id]
                status = f"🪙 Salió... ¡<@{winner}>! Se lleva **{d.amount * 2}** puntos."
                color = discord.Color.gold()
            else:
                status = "✅ Aceptado. Se define en la próxima partida que jueguen juntos."
                color = None
            await self.store.save()
        await self.bot.missions.progress([uid], "bets")
        await interaction.response.edit_message(content=None, embed=self._duel_embed(d, status, color), view=None)

    def _duel_score(self, d: Duel):
        if d.mode == "carry":
            return lambda r: (self.bot.carries.evaluate(r).points, round(r.kda, 2))
        return lambda r: (round(r.kda, 2),)

    async def _resolve_duels(self, records: list[MatchRecord]) -> None:
        for d in list(self.store.duels.values()):
            game = d.find_game(records)
            if game is None:
                continue
            async with self.store.lock:
                if d.id not in self.store.duels:
                    continue
                ch, op = game
                winner = d.winner(self._duel_score(d), ch, op)
                if winner is None:
                    await self.bot.points.add({d.challenger: d.amount, d.opponent: d.amount}, "duelo empatado")
                else:
                    await self.bot.points.add({winner: d.amount * 2}, "duelo ganado")
                del self.store.duels[d.id]
                await self.store.save()
            if d.mode == "carry":
                detail = (f"Carreada: <@{d.challenger}> {self.bot.carries.evaluate(ch).points} pts · "
                          f"<@{d.opponent}> {self.bot.carries.evaluate(op).points} pts")
            else:
                detail = (f"KDA: <@{d.challenger}> {ch.kills}/{ch.deaths}/{ch.assists} ({ch.kda:.2f}) · "
                          f"<@{d.opponent}> {op.kills}/{op.deaths}/{op.assists} ({op.kda:.2f})")
            result = ("🤝 Empate. Se devolvió todo." if winner is None
                      else f"🏆 Ganó <@{winner}> y se lleva **{d.amount * 2}** puntos.")
            await self._edit(d.channel_id, d.message_id,
                             embed=self._duel_embed(d, f"{detail}\n{result}", discord.Color.gold()))

    # ------------------------------------------------------------------ #
    # Recompensas
    # ------------------------------------------------------------------ #
    def _bounty_embed(self, b: Bounty, status: str, color: discord.Color | None = None) -> discord.Embed:
        embed = discord.Embed(title=f"💰 Recompensa · {b.market.title}",
                              description=f"<@{b.creator}> puso la primera. {status}",
                              color=color or discord.Color.dark_red())
        pools = b.market.pools()
        for code, label in b.market.options.items():
            n = sum(1 for bet in b.market.bets.values() if bet.option == code)
            embed.add_field(name=label, value=f"**{pools[code]}** pts · {n} apuesta(s)", inline=True)
        embed.set_footer(text="El pozo se reparte entre los que aciertan. El apuntado no puede apostar en su contra.")
        return embed

    def _bounty_view(self, b: Bounty, disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        for code, label in b.market.options.items():
            view.add_item(BountyButton(b.id, code, label, disabled))
        return view

    def _bounty_open_status(self, b: Bounty) -> str:
        until = b.created_at + dt.timedelta(days=self.bot.settings.bounty_days)
        return (f"⏳ Se puede apostar hasta que <@{b.target}> arranque partida con el grupo "
                f"(vence {discord.utils.format_dt(until, 'R')}).")

    @app_commands.command(name="recompensa", description="Poné puntos a que alguien trollea (o carrea) la próxima.")
    @app_commands.describe(usuario="A quién apuntás.", puntos="Cuánto ponés al Sí.", tipo="Qué tiene que pasar.")
    @app_commands.choices(tipo=[app_commands.Choice(name=f"Que {verb}", value=code)
                                for code, verb in BOUNTY_KINDS.items()])
    async def recompensa(self, interaction: discord.Interaction, usuario: discord.Member, puntos: int,
                         tipo: app_commands.Choice[str]) -> None:
        s = self.bot.settings
        me = interaction.user
        error = bettor_error(self.bot, me, s.prediction_min_bet)
        link = await self.bot.storage.get_link(usuario.id)
        if error is None and (usuario.id == me.id or usuario.bot):
            error = "Apuntale a otro. 🙃"
        elif error is None and link is None:
            error = f"{usuario.display_name} no tiene la cuenta vinculada."
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        existing = next((b for b in self.store.bounties.values()
                         if b.target == usuario.id and b.kind == tipo.value and b.is_open), None)
        if existing is not None:
            # Ya hay una: se suma al Sí de esa en vez de abrir otra.
            async with self.store.lock:
                error = await charge_bet(self.bot, me.id, existing.market, "yes", str(puntos),
                                         f"recompensa {existing.id}")
                if error is None:
                    await self.store.save()
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            await interaction.response.send_message(
                f"Ya había una recompensa por {usuario.mention}: sumaste **{puntos}** al Sí. "
                f"https://discord.com/channels/{interaction.guild_id}/{existing.channel_id}/{existing.message_id}",
                ephemeral=True)
            await self._edit(existing.channel_id, existing.message_id,
                             embed=self._bounty_embed(existing, self._bounty_open_status(existing)))
            return
        market = bounty_market(usuario.id, usuario.display_name, tipo.value)
        b = Bounty(id=secrets.token_hex(4), target=usuario.id, kind=tipo.value, creator=me.id, created_at=_now(),
                   market=market)
        async with self.store.lock:
            error = await charge_bet(self.bot, me.id, market, "yes", str(puntos), f"recompensa {b.id}")
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.send_message(content=usuario.mention,
                                                embed=self._bounty_embed(b, self._bounty_open_status(b)),
                                                view=self._bounty_view(b), allowed_mentions=_USER_MENTIONS)
        msg = await interaction.original_response()
        b.channel_id, b.message_id = msg.channel.id, msg.id
        async with self.store.lock:
            self.store.bounties[b.id] = b
            await self.store.save()

    async def on_bounty_click(self, interaction: discord.Interaction, bounty_id: str, option: str) -> None:
        b = self.store.bounties.get(bounty_id)
        if b is None:
            await interaction.response.send_message("Esta recompensa ya terminó.", ephemeral=True)
            return
        error = bettor_error(self.bot, interaction.user, self.bot.settings.prediction_min_bet)
        if error is None and not b.is_open:
            error = "Ya arrancó la partida: las apuestas cerraron."
        if error is None:
            error = b.market.bet_error(interaction.user.id, option)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        async def on_amount(inter: discord.Interaction, raw: str) -> None:
            await self._place_bounty_bet(inter, bounty_id, option, raw)

        await interaction.response.send_modal(amount_modal(self.bot, interaction.user.id,
                                                           b.market.options[option], on_amount))

    async def _place_bounty_bet(self, interaction: discord.Interaction, bounty_id: str, option: str,
                                raw: str) -> None:
        uid = interaction.user.id
        async with self.store.lock:
            b = self.store.bounties.get(bounty_id)
            if b is None or not b.is_open:
                error: str | None = "Ya arrancó la partida: las apuestas cerraron."
            else:
                error = await charge_bet(self.bot, uid, b.market, option, raw, f"recompensa {b.id}")
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            await self.store.save()
        await interaction.response.send_message(bet_done_text(self.bot, uid, b.market, option), ephemeral=True)
        await self._edit(b.channel_id, b.message_id, embed=self._bounty_embed(b, self._bounty_open_status(b)))

    async def _on_live_game(self, match_id: str, group: dict[int, int]) -> None:
        """Arrancó una partida del grupo: las recompensas sobre alguien que
        la juega cierran y quedan atadas a esa partida."""
        changed: list[Bounty] = []
        async with self.store.lock:
            for b in self.store.bounties.values():
                if b.is_open and b.target in group:
                    b.match_id, b.locked_at = match_id, _now()
                    changed.append(b)
            if changed:
                await self.store.save()
        for b in changed:
            await self._edit(b.channel_id, b.message_id,
                             embed=self._bounty_embed(b, f"🔒 <@{b.target}> arrancó partida. Apuestas cerradas."),
                             view=self._bounty_view(b, disabled=True))

    def _bounty_hit(self, b: Bounty, record: MatchRecord) -> bool:
        service = self.bot.trolls if b.kind == "troll" else self.bot.carries
        return service.evaluate(record).level >= TrollLevel.TROLL

    async def _resolve_bounties(self, records: list[MatchRecord]) -> None:
        for b in list(self.store.bounties.values()):
            record = next((r for r in sorted(records, key=lambda r: r.game_creation) if b.accepts(r)), None)
            if record is None:
                continue
            async with self.store.lock:
                if b.id not in self.store.bounties:
                    continue
                winner = "yes" if self._bounty_hit(b, record) else "no"
                payouts = b.market.payouts(winner)
                await self.bot.points.add(payouts, f"recompensa {b.id}")
                del self.store.bounties[b.id]
                await self.store.save()
            cobran = ", ".join(f"<@{uid}> +{a}" for uid, a in sorted(payouts.items(), key=lambda x: -x[1]))
            status = (f"🏁 {record.champion} {record.kills}/{record.deaths}/{record.assists}: salió "
                      f"**{b.market.options[winner]}**. " + (f"Cobran: {cobran}" if cobran else "Nadie acertó. 🏦"))
            await self._edit(b.channel_id, b.message_id, embed=self._bounty_embed(b, status, discord.Color.green()),
                             view=None)

    # ------------------------------------------------------------------ #
    # Apuestas semanales
    # ------------------------------------------------------------------ #
    def _weekly_embed(self, w: WeeklyBook, status: str, results: dict[str, str] | None = None,
                      color: discord.Color | None = None) -> discord.Embed:
        embed = discord.Embed(title="📅 Apuestas de la semana", description=status,
                              color=color or discord.Color.teal())
        for m in w.markets.values():
            pools = m.pools()
            top = sorted(((pools[c], label) for c, label in m.options.items() if pools[c]), reverse=True)[:8]
            value = "\n".join(f"{label}: **{amount}**" for amount, label in top) or "Nadie apostó todavía."
            if results and m.key in results:
                value = f"{results[m.key]}\n{value}"
            embed.add_field(name=m.title, value=value[:1024], inline=False)
        embed.set_footer(text="Una apuesta por pregunta. Nadie puede apostar a ser él el troll de la semana.")
        return embed

    def _weekly_view(self, w: WeeklyBook, disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        for kind, m in w.markets.items():
            view.add_item(WeeklySelect(w.week, kind, m, disabled))
        return view

    async def _open_weekly(self, week_start: dt.datetime) -> None:
        s = self.bot.settings
        channel = await self._weekly_channel()
        if channel is None:
            return
        links = await self.bot.storage.get_all_links()
        players = {l.discord_id: display_name(self.bot, l.discord_id, l.game_name) for l in links}
        if len(players) < 2:
            return
        close_at = (week_start + dt.timedelta(days=s.weekly_bet_close_days)).astimezone(dt.timezone.utc)
        w = WeeklyBook(week=week_start.date().isoformat(), close_at=close_at, markets=build_weekly(players))
        status = (f"¿Quién termina primero en `/trolls` y en `/carries` esta semana? "
                  f"Se apuesta hasta {discord.utils.format_dt(close_at, 'F')} y se paga el lunes.")
        try:
            msg = await channel.send(embed=self._weekly_embed(w, status), view=self._weekly_view(w),
                                     allowed_mentions=_NO_MENTIONS)
        except discord.HTTPException as exc:
            log.warning("No pude publicar las apuestas semanales (%s).", exc)
            return
        w.channel_id, w.message_id = channel.id, msg.id
        async with self.store.lock:
            self.store.weekly[w.week] = w
            await self.store.save()
        log.info("Apuestas semanales abiertas para la semana del %s.", w.week)

    async def on_weekly_pick(self, interaction: discord.Interaction, week: str, kind: str, option: str) -> None:
        w = self.store.weekly.get(week)
        market = w.markets.get(kind) if w else None
        if w is None or market is None:
            await interaction.response.send_message("Estas apuestas ya terminaron.", ephemeral=True)
            return
        error = bettor_error(self.bot, interaction.user, self.bot.settings.prediction_min_bet)
        if error is None and (w.closed or _now() >= w.close_at):
            error = "Las apuestas de esta semana ya cerraron."
        if error is None:
            error = market.bet_error(interaction.user.id, option)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        async def on_amount(inter: discord.Interaction, raw: str) -> None:
            await self._place_weekly_bet(inter, week, kind, option, raw)

        await interaction.response.send_modal(amount_modal(self.bot, interaction.user.id,
                                                           f"{market.options[option]}", on_amount))

    async def _place_weekly_bet(self, interaction: discord.Interaction, week: str, kind: str, option: str,
                                raw: str) -> None:
        uid = interaction.user.id
        async with self.store.lock:
            w = self.store.weekly.get(week)
            if w is None or w.closed or _now() >= w.close_at:
                error: str | None = "Las apuestas de esta semana ya cerraron."
            else:
                error = await charge_bet(self.bot, uid, w.markets[kind], option, raw, f"semanal {week} {kind}")
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            await self.store.save()
        await interaction.response.send_message(bet_done_text(self.bot, uid, w.markets[kind], option),
                                                ephemeral=True)
        status = f"Se apuesta hasta {discord.utils.format_dt(w.close_at, 'F')} y se paga el lunes."
        await self._edit(w.channel_id, w.message_id, embed=self._weekly_embed(w, status))

    async def _resolve_weekly(self, w: WeeklyBook, week_start: dt.datetime) -> None:
        until = week_start + dt.timedelta(days=7)
        rankings = {
            "troll": await self.bot.trolls.standings_between(week_start, until),
            "carry": await self.bot.carries.standings_between(week_start, until),
        }
        amounts: dict[int, int] = {}
        results: dict[str, str] = {}
        async with self.store.lock:
            if w.week not in self.store.weekly:
                return
            for kind, m in w.markets.items():
                winner = weekly_winner([(s.discord_id, s.points) for s in rankings[kind]])
                if winner is None or winner not in m.options:
                    paid = m.refunds()
                    results[kind] = "🚫 Nadie de la lista terminó primero: se devolvió todo."
                else:
                    paid = m.payouts(winner)
                    cobran = ", ".join(f"<@{uid}> +{a}" for uid, a in paid.items())
                    results[kind] = f"✅ **{m.options[winner]}**" + (f" — cobran {cobran}" if cobran else "")
                for uid, a in paid.items():
                    amounts[uid] = amounts.get(uid, 0) + a
            await self.bot.points.add(amounts, f"semanal {w.week}")
            del self.store.weekly[w.week]
            await self.store.save()
        await self._edit(w.channel_id, w.message_id, view=None,
                         embed=self._weekly_embed(w, "🏁 Terminó la semana.", results, discord.Color.green()))
        log.info("Apuestas semanales del %s resueltas.", w.week)

    # ------------------------------------------------------------------ #
    # Resolución y vencimientos
    # ------------------------------------------------------------------ #
    async def _on_new_matches(self, records: list[MatchRecord]) -> None:
        await self._resolve_duels(records)
        await self._resolve_bounties(records)

    @tasks.loop(minutes=1)
    async def tick_job(self) -> None:
        try:
            await self._expire()
            if self.bot.settings.weekly_bets_enabled and self.bot.storage.ready:
                await self._weekly_tick()
        except Exception:  # noqa: BLE001 - que un fallo no mate el loop
            log.exception("Error en el loop de duelos/recompensas/semanales.")

    @tick_job.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _expire(self) -> None:
        s = self.bot.settings
        now = _now()
        edits: list[tuple[int, int, discord.Embed, discord.ui.View | None]] = []
        async with self.store.lock:
            changed = False
            for d in list(self.store.duels.values()):
                if d.pending and now - d.created_at > dt.timedelta(minutes=s.duel_accept_minutes):
                    await self.bot.points.add({d.challenger: d.amount}, "duelo sin aceptar")
                    edits.append((d.channel_id, d.message_id, self._duel_embed(
                        d, "⌛ No lo aceptó a tiempo. Se devolvieron los puntos.", discord.Color.dark_grey()), None))
                elif not d.pending and now - d.accepted_at > dt.timedelta(hours=s.duel_match_hours):
                    await self.bot.points.add({d.challenger: d.amount, d.opponent: d.amount}, "duelo sin partida")
                    edits.append((d.channel_id, d.message_id, self._duel_embed(
                        d, f"⌛ No jugaron juntos en {s.duel_match_hours} h. Se devolvió todo.",
                        discord.Color.dark_grey()), None))
                else:
                    continue
                del self.store.duels[d.id]
                changed = True
            for b in list(self.store.bounties.values()):
                if b.locked_at is not None and now - b.locked_at > _LOCK_TIMEOUT:
                    # La partida fijada nunca se guardó (remake o no contó): vuelve a abrir.
                    b.match_id, b.locked_at = None, None
                    changed = True
                    edits.append((b.channel_id, b.message_id, self._bounty_embed(b, self._bounty_open_status(b)),
                                  self._bounty_view(b)))
                elif b.is_open and now - b.created_at > dt.timedelta(days=s.bounty_days):
                    await self.bot.points.add(b.market.refunds(), f"recompensa vencida {b.id}")
                    del self.store.bounties[b.id]
                    changed = True
                    edits.append((b.channel_id, b.message_id, self._bounty_embed(
                        b, "⌛ No jugó con el grupo a tiempo. Se devolvió todo.", discord.Color.dark_grey()), None))
            if changed:
                await self.store.save()
        for channel_id, message_id, embed, view in edits:
            await self._edit(channel_id, message_id, embed=embed, view=view)

    async def _weekly_tick(self) -> None:
        s = self.bot.settings
        tz = get_tz(s.timezone)
        current = start_of_week(s.timezone)
        now = _now()
        for w in list(self.store.weekly.values()):
            week_start = dt.datetime.combine(w.week_start, dt.time(0), tz)
            if now >= week_start + dt.timedelta(days=7) + _WEEKLY_GRACE:
                await self._resolve_weekly(w, week_start)
            elif not w.closed and now >= w.close_at:
                async with self.store.lock:
                    w.closed = True
                    await self.store.save()
                await self._edit(w.channel_id, w.message_id, view=self._weekly_view(w, disabled=True),
                                 embed=self._weekly_embed(w, "🔒 Apuestas cerradas. Se paga el lunes."))
        week = current.date().isoformat()
        close_at = current + dt.timedelta(days=s.weekly_bet_close_days)
        if week not in self.store.weekly and now < close_at:
            await self._open_weekly(current)

