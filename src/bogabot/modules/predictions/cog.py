"""Predicciones con puntos (estilo Twitch) sobre partidas del grupo en vivo.

  1. poll_job (cada PREDICTION_POLL_MINUTES) le pregunta a Riot
     (spectator-v5) si algún vinculado está en partida. Si hay al menos 2
     vinculados en la misma (mismo criterio que la ingesta), abre una
     predicción en PREDICTION_CHANNEL_ID (default MATCH_NOTIFY_CHANNEL_ID).
  2. Se apuesta con botones (+ un modal para el monto) hasta
     PREDICTION_CLOSE_MINUTES de partida. Los puntos se descuentan al apostar.
  3. Cuando la ingesta guarda la partida (listener de IngestService), se
     resuelve y se reparte el pozo (ver `logic.Prediction.payouts`).
  4. Si nunca se resuelve (remake, que la ingesta descarta), a las
     _RESOLVE_TIMEOUT se cancela y se devuelve todo.

Junto con la predicción sale un segundo mensaje con las apuestas sobre la
partida (props.py: quién carrea, quién trollea, muertes, duración, primera
sangre), con un desplegable de mercados. Abren, cierran y se resuelven con
la predicción; si a un mercado le falta el dato, se devuelve lo apostado.

Los botones y el desplegable son `DynamicItem` con el game_id en el
custom_id: siguen andando después de un reinicio (las predicciones viven en
PREDICTIONS_FILE).

Cada partida del grupo detectada en vivo se avisa a los listeners de
`add_game_listener` (lo usan las recompensas para cerrar sus apuestas).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord.ext import commands, tasks

from bogabot.core.models import MatchRecord
from bogabot.core.models import TrollLevel
from bogabot.modules.points.cog import has_role
from bogabot.modules.predictions.betting import (OptionPicker, amount_modal, bet_done_text, bettor_error,
                                                 charge_bet, display_name)
from bogabot.modules.predictions.logic import BLUE, OPTIONS, Bet, Prediction
from bogabot.modules.predictions.market import Market
from bogabot.modules.predictions.props import GameFacts, build_props, resolve_prop
from bogabot.modules.predictions.store import PredictionStore
from bogabot.riot.client import RiotApiError, RiotAuthError
from bogabot.riot.mapper import QUEUE_NAMES, queue_name

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

log = logging.getLogger(__name__)

_RESOLVE_TIMEOUT = dt.timedelta(hours=3)
_IGNORED_MEMORY = 200  # game_ids descartados que se recuerdan (no re-evaluarlos)
_NO_MENTIONS = discord.AllowedMentions.none()
_STYLES = {"a": discord.ButtonStyle.success, "b": discord.ButtonStyle.danger}

# (match_id, discord_id -> team_id del grupo)
GameListener = Callable[[str, dict[int, int]], Awaitable[None]]


class BetButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pred:(?P<game>[0-9]+):(?P<opt>[ab])"):
    def __init__(self, game_id: int, option: str, label: str = "Apostar", disabled: bool = False) -> None:
        super().__init__(discord.ui.Button(label=label, style=_STYLES.get(option, discord.ButtonStyle.primary),
                                           custom_id=f"pred:{game_id}:{option}", disabled=disabled))
        self.game_id = game_id
        self.option = option

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str]) -> "BetButton":
        return cls(int(match["game"]), match["opt"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("PredictionsCog")  # type: ignore[union-attr]
        if cog is None:
            await interaction.response.send_message("Las predicciones están apagadas.", ephemeral=True)
            return
        await cog.on_bet_click(interaction, self.game_id, self.option)


class PropsSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"props:(?P<game>[0-9]+)"):
    def __init__(self, game_id: int, markets: list[Market] | None = None, disabled: bool = False) -> None:
        options = [discord.SelectOption(label=m.title[:100], value=m.key) for m in (markets or [])[:25]]
        super().__init__(discord.ui.Select(placeholder="🎯 Elegí en qué apostar", custom_id=f"props:{game_id}",
                                           options=options or [discord.SelectOption(label="-", value="-")],
                                           disabled=disabled))
        self.game_id = game_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select,
                             match: re.Match[str]) -> "PropsSelect":
        return cls(int(match["game"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("PredictionsCog")  # type: ignore[union-attr]
        if cog is None or not self.item.values:
            await interaction.response.send_message("Las predicciones están apagadas.", ephemeral=True)
            return
        await cog.on_prop_pick(interaction, self.game_id, self.item.values[0])


class BetModal(discord.ui.Modal):
    amount: discord.ui.TextInput = discord.ui.TextInput(label="¿Cuántos puntos?", max_length=6)

    def __init__(self, cog: "PredictionsCog", game_id: int, option: str, title: str,
                 min_bet: int, max_bet: int) -> None:
        super().__init__(title=title[:45])
        self.cog, self.game_id, self.option = cog, game_id, option
        self.amount.placeholder = f"Entre {min_bet} y {max_bet}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.place_bet(interaction, self.game_id, self.option, self.amount.value)


class PredictionsCog(commands.Cog):
    def __init__(self, bot: "BogaBot", store: PredictionStore) -> None:
        self.bot = bot
        self.store = store
        self._ignored: dict[int, None] = {}
        self._game_listeners: list[GameListener] = []

    def add_game_listener(self, callback: GameListener) -> None:
        self._game_listeners.append(callback)

    def remove_game_listener(self, callback: GameListener) -> None:
        if callback in self._game_listeners:
            self._game_listeners.remove(callback)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(BetButton, PropsSelect)
        self.bot.ingest.add_listener(self._on_new_matches)
        self.poll_job.change_interval(minutes=self.bot.settings.prediction_poll_minutes)
        self.poll_job.start()
        self.close_job.start()
        log.info("Predicciones: chequeo de partidas en vivo cada %d min, apuestas hasta el minuto %d.",
                 self.bot.settings.prediction_poll_minutes, self.bot.settings.prediction_close_minutes)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(BetButton, PropsSelect)
        self.bot.ingest.remove_listener(self._on_new_matches)
        self.poll_job.cancel()
        self.close_job.cancel()

    # ------------------------------------------------------------------ #
    # Mensaje
    # ------------------------------------------------------------------ #
    def _embed(self, p: Prediction, status: str, color: discord.Color | None = None) -> discord.Embed:
        if p.split:
            blue = ", ".join(f"<@{d}>" for d, t in p.players.items() if t == BLUE)
            red = ", ".join(f"<@{d}>" for d, t in p.players.items() if t != BLUE)
            who = f"🔵 {blue}\n🔴 {red}\n¡El grupo quedó dividido! ¿Quién gana?"
        else:
            who = f"{', '.join(f'<@{d}>' for d in p.players)} arrancaron partida. ¿Ganan o pierden?"
        embed = discord.Embed(title=f"🔮 Predicción · {queue_name(p.queue_id)}",
                              description=f"{who}\n\n{status}", color=color or discord.Color.purple())
        pools = p.pools()
        for o in OPTIONS:
            n = sum(1 for b in p.bets.values() if b.option == o)
            embed.add_field(name=p.option_labels[o], value=f"**{pools[o]}** pts · {n} apuesta(s)", inline=True)
        embed.set_footer(text="El pozo se reparte entre los que aciertan, según cuánto puso cada uno.")
        return embed

    def _view(self, p: Prediction, disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        for o in OPTIONS:
            view.add_item(BetButton(p.game_id, o, p.option_labels[o], disabled=disabled))
        return view

    def _open_status(self, p: Prediction) -> str:
        return f"⏳ Apuestas abiertas, cierran {discord.utils.format_dt(p.close_at, 'R')}."

    def _props_embed(self, p: Prediction, status: str, results: dict[str, str] | None = None,
                     color: discord.Color | None = None) -> discord.Embed:
        embed = discord.Embed(title="🎯 Apuestas sobre la partida", description=status,
                              color=color or discord.Color.purple())
        for m in list(p.markets.values())[:25]:
            value = m.pools_text()
            if results and m.key in results:
                value = f"{results[m.key]}\n{value}"
            embed.add_field(name=m.title, value=value[:1024], inline=False)
        embed.set_footer(text="Una apuesta por pregunta. Los que juegan no pueden apostar en su contra.")
        return embed

    def _props_view(self, p: Prediction, disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(PropsSelect(p.game_id, list(p.markets.values()), disabled=disabled))
        return view

    async def _edit(self, p: Prediction, message_id: int | None = None, **kwargs) -> None:
        message_id = message_id or p.message_id
        channel = self.bot.get_channel(p.channel_id)
        if not isinstance(channel, discord.TextChannel) or not message_id:
            return
        try:
            await channel.get_partial_message(message_id).edit(allowed_mentions=_NO_MENTIONS, **kwargs)
        except discord.HTTPException as exc:
            log.warning("No pude editar la predicción %s (%s).", p.game_id, exc)

    async def _edit_props(self, p: Prediction, **kwargs) -> None:
        if p.markets and p.props_message_id:
            await self._edit(p, message_id=p.props_message_id, **kwargs)

    async def _channel(self) -> discord.TextChannel | None:
        s = self.bot.settings
        channel_id = s.prediction_channel_id or s.match_notify_channel_id
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    # ------------------------------------------------------------------ #
    # Detección de partidas en vivo
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=3)  # el intervalo real se fija en cog_load
    async def poll_job(self) -> None:
        if not self.bot.storage.ready:
            return
        channel = await self._channel()
        if channel is None:
            return
        try:
            await self._poll(channel)
        except Exception:  # noqa: BLE001 - que un fallo no mate el loop
            log.exception("Error buscando partidas en vivo para predicciones.")

    @poll_job.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def _poll(self, channel: discord.TextChannel) -> None:
        links = await self.bot.storage.get_all_links()
        linked = {l.puuid: l.discord_id for l in links}
        names = {l.discord_id: l.game_name for l in links}
        known = {p.game_id for p in self.store.all()}
        covered: set[str] = set()  # puuids ya vistos en alguna partida de esta pasada
        close_seconds = self.bot.settings.prediction_close_minutes * 60
        for puuid in linked:
            if puuid in covered:
                continue
            try:
                game = await self.bot.riot.get_active_game(puuid)
            except RiotAuthError:
                return  # key vencida: ya lo avisa la ingesta
            except RiotApiError as exc:
                log.debug("Spectator falló para un vinculado (%s).", exc)
                continue
            if not game:
                continue
            participants = game.get("participants", [])
            covered.update(p.get("puuid") for p in participants if p.get("puuid"))
            game_id = int(game["gameId"])
            if game_id in known or game_id in self._ignored:
                continue
            group = {linked[p["puuid"]]: int(p["teamId"]) for p in participants if p.get("puuid") in linked}
            elapsed = max(0, int(game.get("gameLength") or 0))
            if len(group) >= 2 and game.get("gameType") != "CUSTOM_GAME":
                await self._notify_game(self._match_id(game), group)
            if len(group) < 2 or game.get("gameType") == "CUSTOM_GAME" or elapsed >= close_seconds:
                self._ignored[game_id] = None
                while len(self._ignored) > _IGNORED_MEMORY:
                    self._ignored.pop(next(iter(self._ignored)))
                continue
            await self._open(channel, game, group, close_seconds - elapsed, names)
            known.add(game_id)

    def _match_id(self, game: dict) -> str:
        return f"{game.get('platformId') or self.bot.settings.riot_platform.upper()}_{int(game['gameId'])}"

    async def _notify_game(self, match_id: str, group: dict[int, int]) -> None:
        for callback in list(self._game_listeners):
            try:
                await callback(match_id, group)
            except Exception:  # noqa: BLE001 - un listener roto no frena al resto
                log.exception("Error en un listener de partidas en vivo.")

    async def _open(self, channel: discord.TextChannel, game: dict, group: dict[int, int],
                    seconds_left: int, names: dict[int, str]) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        game_id = int(game["gameId"])
        p = Prediction.build(
            game_id=game_id,
            match_id=self._match_id(game),
            queue_id=int(game.get("gameQueueConfigId") or 0),
            players=group, opened_at=now, close_at=now + dt.timedelta(seconds=seconds_left),
        )
        try:
            msg = await channel.send(embed=self._embed(p, self._open_status(p)), view=self._view(p),
                                     allowed_mentions=_NO_MENTIONS)
        except discord.HTTPException as exc:
            log.warning("No pude abrir la predicción de %s (%s).", game_id, exc)
            return
        p.channel_id, p.message_id = channel.id, msg.id
        if self.bot.settings.props_enabled:
            labels = {uid: display_name(self.bot, uid, names.get(uid, "")) for uid in group}
            p.markets = build_props(group, labels, p.queue_id)
            try:
                props_msg = await channel.send(embed=self._props_embed(p, self._open_status(p)),
                                               view=self._props_view(p), allowed_mentions=_NO_MENTIONS)
                p.props_message_id = props_msg.id
            except discord.HTTPException as exc:
                log.warning("No pude abrir las apuestas sobre la partida de %s (%s).", game_id, exc)
                p.markets = {}
        await self.store.put(p)
        log.info("Predicción abierta para %s (%d vinculados, %d props).", p.match_id, len(group),
                 len(p.markets))
        sounds = self.bot.get_cog("SoundsCog")
        if sounds is not None:
            sounds.announce_game_start(list(group), QUEUE_NAMES.get(p.queue_id, "LoL"), channel.name)

    # ------------------------------------------------------------------ #
    # Apuestas
    # ------------------------------------------------------------------ #
    async def on_bet_click(self, interaction: discord.Interaction, game_id: int, option: str) -> None:
        s = self.bot.settings
        p = self.store.get(game_id)
        if p is None:
            await interaction.response.send_message("Esta predicción ya terminó.", ephemeral=True)
            return
        member = interaction.user
        if not isinstance(member, discord.Member) or not has_role(member, s.points_role_id):
            await interaction.response.send_message("Necesitás el rol de Boguero para apostar.", ephemeral=True)
            return
        error = p.bet_error(member.id, option, dt.datetime.now(dt.timezone.utc))
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        balance = self.bot.points.balance(member.id)
        if balance < s.prediction_min_bet:
            await interaction.response.send_message(
                f"Tenés **{balance}** puntos y la apuesta mínima es {s.prediction_min_bet}.", ephemeral=True)
            return
        max_bet = min(s.prediction_max_bet, balance)
        await interaction.response.send_modal(BetModal(
            self, game_id, option, f"Apostar a {p.option_labels[option]}", s.prediction_min_bet, max_bet))

    async def place_bet(self, interaction: discord.Interaction, game_id: int, option: str, raw: str) -> None:
        s = self.bot.settings
        uid = interaction.user.id
        try:
            amount = int(raw.strip())
        except ValueError:
            await interaction.response.send_message("Eso no es un número. 🤨", ephemeral=True)
            return
        async with self.store.lock:
            p = self.store.get(game_id)
            if p is None:
                await interaction.response.send_message("Esta predicción ya terminó.", ephemeral=True)
                return
            error = p.bet_error(uid, option, dt.datetime.now(dt.timezone.utc))
            if error is None and not s.prediction_min_bet <= amount <= s.prediction_max_bet:
                error = f"La apuesta tiene que ser entre {s.prediction_min_bet} y {s.prediction_max_bet}."
            if error is None and not await self.bot.points.spend(uid, amount, f"predicción {p.match_id}"):
                error = f"No te alcanza: tenés **{self.bot.points.balance(uid)}** puntos."
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            p.bets[uid] = Bet(option, amount)
            await self.store.put(p)
            await self.bot.missions.progress([uid], "bets")
        await interaction.response.send_message(
            f"🔮 Apostaste **{amount}** a **{p.option_labels[option]}**. Te quedan "
            f"{self.bot.points.balance(uid)} puntos.", ephemeral=True)
        await self._edit(p, embed=self._embed(p, self._open_status(p)))

    async def on_prop_pick(self, interaction: discord.Interaction, game_id: int, key: str) -> None:
        p = self.store.get(game_id)
        market = p.markets.get(key) if p else None
        if p is None or market is None:
            await interaction.response.send_message("Esta predicción ya terminó.", ephemeral=True)
            return
        error = bettor_error(self.bot, interaction.user, self.bot.settings.prediction_min_bet)
        if error is None and not p.is_open(dt.datetime.now(dt.timezone.utc)):
            error = "Las apuestas de esta partida ya cerraron."
        if error is None and interaction.user.id in market.bets:
            error = "Ya apostaste en esta."
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        async def on_option(inter: discord.Interaction, option: str) -> None:
            option_error = market.bet_error(inter.user.id, option)
            if option_error:
                await inter.response.send_message(option_error, ephemeral=True)
                return

            async def on_amount(modal_inter: discord.Interaction, raw: str) -> None:
                await self.place_prop_bet(modal_inter, game_id, key, option, raw)

            await inter.response.send_modal(amount_modal(self.bot, inter.user.id, market.options[option],
                                                         on_amount))

        await interaction.response.send_message(f"**{market.title}** ¿A qué le vas?",
                                                view=OptionPicker(market, on_option), ephemeral=True)

    async def place_prop_bet(self, interaction: discord.Interaction, game_id: int, key: str, option: str,
                             raw: str) -> None:
        uid = interaction.user.id
        async with self.store.lock:
            p = self.store.get(game_id)
            market = p.markets.get(key) if p else None
            if p is None or market is None:
                error: str | None = "Esta predicción ya terminó."
            elif not p.is_open(dt.datetime.now(dt.timezone.utc)):
                error = "Las apuestas de esta partida ya cerraron."
            else:
                error = await charge_bet(self.bot, uid, market, option, raw, f"prop {key} {p.match_id}")
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            await self.store.put(p)
        await interaction.response.send_message(bet_done_text(self.bot, uid, market, option), ephemeral=True)
        await self._edit_props(p, embed=self._props_embed(p, self._open_status(p)))

    # ------------------------------------------------------------------ #
    # Cierre, resolución y cancelación
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=1)
    async def close_job(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        for p in self.store.all():
            try:
                if now - p.opened_at > _RESOLVE_TIMEOUT:
                    await self._cancel(p)
                elif not p.closed and now >= p.close_at:
                    async with self.store.lock:
                        p.closed = True
                        await self.store.put(p)
                    closed = "🔒 Apuestas cerradas. Esperando el resultado..."
                    await self._edit(p, embed=self._embed(p, closed), view=self._view(p, disabled=True))
                    await self._edit_props(p, embed=self._props_embed(p, closed),
                                           view=self._props_view(p, disabled=True))
            except Exception:  # noqa: BLE001
                log.exception("Error cerrando la predicción %s.", p.game_id)

    @close_job.before_loop
    async def _before_close(self) -> None:
        await self.bot.wait_until_ready()

    async def _on_new_matches(self, records: list[MatchRecord]) -> None:
        by_match: dict[str, list[MatchRecord]] = {}
        for r in records:
            by_match.setdefault(r.match_id, []).append(r)
        for p in self.store.all():
            if p.match_id in by_match:
                await self._resolve(p, by_match[p.match_id])

    def _facts(self, p: Prediction, records: list[MatchRecord]) -> GameFacts:
        facts = GameFacts()
        first_blood: list[bool | None] = []
        for r in records:
            if r.discord_id not in p.players:
                continue
            facts.duration_minutes = r.minutes
            facts.deaths[r.discord_id] = r.deaths
            facts.trolled[r.discord_id] = self.bot.trolls.evaluate(r).level >= TrollLevel.TROLL
            carry = self.bot.carries.evaluate(r)
            if carry.level >= TrollLevel.TROLL:
                facts.carry_points[r.discord_id] = carry.points
            first_blood.append(r.first_blood_kill)
        if any(first_blood):
            facts.group_first_blood = True
        elif first_blood and None not in first_blood:
            facts.group_first_blood = False
        return facts

    def _resolve_markets(self, p: Prediction, facts: GameFacts) -> tuple[dict[int, int], dict[str, str]]:
        """Pagos (o devoluciones) de las props y el texto del resultado de cada una."""
        amounts: dict[int, int] = {}
        results: dict[str, str] = {}
        for m in p.markets.values():
            winner = resolve_prop(m, facts)
            if winner is None:
                paid = m.refunds()
                results[m.key] = "🚫 Sin datos: se devolvió lo apostado."
            else:
                paid = m.payouts(winner)
                cobran = ", ".join(f"<@{uid}> +{a}" for uid, a in paid.items())
                results[m.key] = f"✅ **{m.options[winner]}**" + (f" — {cobran}" if cobran else "")
            for uid, a in paid.items():
                amounts[uid] = amounts.get(uid, 0) + a
        return amounts, results

    async def _resolve(self, p: Prediction, records: list[MatchRecord]) -> None:
        results = {r.discord_id: r.win for r in records}
        async with self.store.lock:
            winner = p.winning_option(results)
            if winner is None or self.store.get(p.game_id) is None:
                return
            payouts = p.payouts(winner)
            prop_amounts, prop_results = self._resolve_markets(p, self._facts(p, records))
            total = dict(payouts)
            for uid, a in prop_amounts.items():
                total[uid] = total.get(uid, 0) + a
            await self.bot.points.add(total, f"predicción {p.match_id}")
            await self.store.remove(p.game_id)
        label = p.option_labels[winner]
        if not p.bets:
            status = f"🏁 Salió **{label}**. Nadie apostó. 🦗"
        elif not payouts:
            status = f"🏁 Salió **{label}**. Nadie acertó: el pozo se lo llevó la casa. 🏦"
        else:
            lines = [f"<@{uid}> +{amount}" for uid, amount in sorted(payouts.items(), key=lambda x: -x[1])]
            status = f"🏁 Salió **{label}**. Cobran:\n" + "\n".join(lines)
        await self._edit(p, embed=self._embed(p, status, discord.Color.green()), view=None)
        await self._edit_props(p, embed=self._props_embed(p, "🏁 Terminó la partida.", prop_results,
                                                          discord.Color.green()), view=None)
        log.info("Predicción %s resuelta: %s, %d ganadores.", p.match_id, winner, len(payouts))

    async def _cancel(self, p: Prediction) -> None:
        async with self.store.lock:
            if self.store.get(p.game_id) is None:
                return
            refunds = {uid: b.amount for uid, b in p.bets.items()}
            for m in p.markets.values():
                for uid, a in m.refunds().items():
                    refunds[uid] = refunds.get(uid, 0) + a
            await self.bot.points.add(refunds, f"predicción cancelada {p.match_id}")
            await self.store.remove(p.game_id)
        status = "🚫 Cancelada (remake o partida que no cuenta). Se devolvieron todas las apuestas."
        await self._edit(p, embed=self._embed(p, status, discord.Color.dark_grey()), view=None)
        await self._edit_props(p, embed=self._props_embed(p, status, color=discord.Color.dark_grey()), view=None)
        log.info("Predicción %s cancelada, apuestas devueltas.", p.match_id)
