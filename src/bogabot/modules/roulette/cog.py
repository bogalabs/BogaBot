"""/ruleta: rol, campeón y build "no tan random" para los del canal de voz.

  - /ruleta [modo]: toma a los humanos de tu canal de voz (máx. 5) y le
    asigna a cada uno rol, campeón y build (ver `logic.py`).
  - 🔄 Re-tirar: cada uno re-tira su campeón y build (mismo rol) por
    ROULETTE_REROLL_COST, que sube con cada re-tirada. Si te castigaron, no.
  - 😈 Castigar: pagás ROULETTE_PUNISH_COST y a quien elijas le toca lo peor
    de la ruleta (campeón que no encaja en el rol + build maldita).
  - Apuesta "¿ganan con la ruleta?": Sí/No, los que juegan solo al Sí.
  - Todo eso cierra a los ROULETTE_BET_MINUTES o cuando el módulo de
    predicciones ve arrancar la partida (`add_game_listener`).
  - Bonus: si la ingesta ve que alguien jugó el campeón y la posición que
    le tocaron (en ROULETTE_HOURS), cobra ROULETTE_BONUS (doble si lo
    castigaron). La build no se puede chequear: match-v5 no la guardamos.
  - La apuesta se resuelve con la primera partida de los de la ruleta: si
    nadie respetó la ruleta, se devuelve.
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

from bogabot.core.models import MatchRecord
from bogabot.modules.points.cog import has_role, user_has_role
from bogabot.modules.predictions.betting import amount_modal, bet_done_text, bettor_error, charge_bet
from bogabot.modules.roulette.ddragon import DDragon, DDragonError
from bogabot.modules.roulette.logic import (MODES, ROLE_LABELS, ROLES, THEMES, Spin, assign_roles,
                                            comfort_champions, make_pick, win_market)
from bogabot.modules.roulette.store import RouletteStore

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

log = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()
_ACTIONS = {"re": ("🔄 Re-tirar el mío", discord.ButtonStyle.secondary),
            "pun": ("😈 Castigar a alguien", discord.ButtonStyle.danger),
            "yes": ("🏆 Ganan", discord.ButtonStyle.success),
            "no": ("💩 Pierden", discord.ButtonStyle.secondary)}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class RouletteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rl:(?P<id>[0-9a-f]+):(?P<act>re|pun|yes|no)"):
    def __init__(self, spin_id: str, action: str, disabled: bool = False) -> None:
        label, style = _ACTIONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"rl:{spin_id}:{action}",
                                           disabled=disabled))
        self.spin_id, self.action = spin_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str]) -> "RouletteButton":
        return cls(match["id"], match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("RouletteCog")  # type: ignore[union-attr]
        if cog is not None:
            await cog.on_click(interaction, self.spin_id, self.action)


class RouletteCog(commands.Cog):
    def __init__(self, bot: "BogaBot", store: RouletteStore) -> None:
        self.bot = bot
        self.store = store
        self.ddragon = DDragon(bot.settings.ddragon_lang)
        self._rng = random.SystemRandom()
        self._closed_shown: set[str] = set()  # tiradas a las que ya se les apagaron los botones

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RouletteButton)
        self.bot.ingest.add_listener(self._on_new_matches)
        predictions = self.bot.get_cog("PredictionsCog")
        if predictions is not None:
            predictions.add_game_listener(self._on_live_game)
        self.tick_job.start()

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(RouletteButton)
        self.bot.ingest.remove_listener(self._on_new_matches)
        predictions = self.bot.get_cog("PredictionsCog")
        if predictions is not None:
            predictions.remove_game_listener(self._on_live_game)
        self.tick_job.cancel()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _is_open(self, spin: Spin) -> bool:
        window = dt.timedelta(minutes=self.bot.settings.roulette_bet_minutes)
        return spin.is_open and _now() < spin.created_at + window

    def _reroll_cost(self, rerolls: int) -> int:
        return self.bot.settings.roulette_reroll_cost * (rerolls + 1)

    def _embed(self, spin: Spin, status: str | None = None, color: discord.Color | None = None) -> discord.Embed:
        s = self.bot.settings
        if status is None:
            closes = spin.created_at + dt.timedelta(minutes=s.roulette_bet_minutes)
            status = (f"🔄 Re-tirar: {s.roulette_reroll_cost} pts (sube cada vez) · 😈 Castigar: "
                      f"{s.roulette_punish_cost} pts · cierra {discord.utils.format_dt(closes, 'R')}.\n"
                      f"💰 Si jugás el campeón y la posición que te tocó cobrás {s.roulette_bonus} pts "
                      f"(el doble si te castigaron).")
        embed = discord.Embed(title=f"🎰 Ruleta · {MODES.get(spin.mode, spin.mode)}", description=status,
                              color=color or discord.Color.fuchsia())
        order = {r: i for i, r in enumerate(ROLES)}
        for uid, pick in sorted(spin.picks.items(), key=lambda kv: order.get(kv[1].role, 9)):
            marks = (" 💀 *maldita*" if pick.cursed and not pick.punished else "") + (" 😈 *castigado*" if pick.punished else "")
            items = ", ".join(pick.items)
            paid = " ✅ cumplió" if uid in spin.paid else ""
            embed.add_field(
                name=f"{ROLE_LABELS.get(pick.role, pick.role)} · {pick.champion_name}",
                value=(f"<@{uid}>{paid}\n{THEMES.get(pick.theme, pick.theme)}{marks}\n"
                       f"👟 {pick.boots or '—'}\n🧰 {items}")[:1024],
                inline=False)
        embed.add_field(name=spin.market.title, value=spin.market.pools_text(), inline=False)
        if self.ddragon.version:
            embed.set_footer(text=f"Data Dragon {self.ddragon.version}")
        return embed

    def _view(self, spin: Spin, disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        for action in _ACTIONS:
            view.add_item(RouletteButton(spin.id, action, disabled))
        return view

    async def _edit(self, spin: Spin, **kwargs) -> None:
        channel = self.bot.get_channel(spin.channel_id)
        if not isinstance(channel, discord.TextChannel) or not spin.message_id:
            return
        try:
            await channel.get_partial_message(spin.message_id).edit(allowed_mentions=_NO_MENTIONS, **kwargs)
        except discord.HTTPException as exc:
            log.warning("No pude editar la ruleta %s (%s).", spin.id, exc)

    async def _history(self, user_ids: list[int]) -> tuple[dict[int, list[str]], dict[int, set[str]]]:
        """(posiciones jugadas, campeones de siempre) de cada jugador."""
        positions: dict[int, list[str]] = {uid: [] for uid in user_ids}
        champions: dict[int, list[str]] = {uid: [] for uid in user_ids}
        if self.bot.storage.ready:
            for r in await self.bot.storage.get_all_matches():
                if r.discord_id in positions:
                    if r.position:
                        positions[r.discord_id].append(r.position)
                    champions[r.discord_id].append(r.champion)
        return positions, {uid: comfort_champions(c) for uid, c in champions.items()}

    # ------------------------------------------------------------------ #
    # /ruleta
    # ------------------------------------------------------------------ #
    @app_commands.command(name="ruleta", description="Rol, campeón y build random para los de tu canal de voz.")
    @app_commands.describe(modo="Normal, o te manda a las posiciones que menos jugás.")
    @app_commands.choices(modo=[app_commands.Choice(name=label, value=code) for code, label in MODES.items()])
    async def ruleta(self, interaction: discord.Interaction, modo: app_commands.Choice[str] | None = None) -> None:
        member = interaction.user
        voice = member.voice.channel if isinstance(member, discord.Member) and member.voice else None
        if voice is None:
            await interaction.response.send_message("Metete en un canal de voz con los que van a jugar. 🎙️",
                                                    ephemeral=True)
            return
        players = [m.id for m in voice.members if not m.bot]
        if len(players) > len(ROLES):
            await interaction.response.send_message(
                f"Hay {len(players)} en {voice.mention} y la ruleta es para 5 como mucho.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            await self.ddragon.ensure()
        except DDragonError as exc:
            log.warning("%s", exc)
            await interaction.followup.send("No pude bajar los campeones de Riot (Data Dragon). Probá en un rato.")
            return
        mode = modo.value if modo else "normal"
        self._rng.shuffle(players)
        positions, comfort = await self._history(players)
        roles = assign_roles(players, positions, mode, self._rng)
        picks = {}
        taken: set[str] = set()
        for uid, role in roles.items():
            pick = make_pick(role, self.ddragon.champions, self.ddragon.items, self.ddragon.boots, self._rng,
                             comfort=comfort.get(uid, set()), taken=taken,
                             cursed_chance=self.bot.settings.roulette_cursed_chance)
            taken.add(pick.champion_id)
            picks[uid] = pick
        spin = Spin(id=secrets.token_hex(4), created_at=_now(), mode=mode, picks=picks,
                    market=win_market(list(picks)))
        msg = await interaction.followup.send(embed=self._embed(spin), view=self._view(spin), wait=True,
                                              allowed_mentions=_NO_MENTIONS)
        spin.channel_id, spin.message_id = msg.channel.id, msg.id
        async with self.store.lock:
            self.store.spins[spin.id] = spin
            await self.store.save()
        log.info("Ruleta %s (%s) para %d jugadores.", spin.id, mode, len(picks))

    # ------------------------------------------------------------------ #
    # Botones
    # ------------------------------------------------------------------ #
    async def on_click(self, interaction: discord.Interaction, spin_id: str, action: str) -> None:
        spin = self.store.spins.get(spin_id)
        if spin is None:
            await interaction.response.send_message("Esta ruleta ya terminó.", ephemeral=True)
            return
        if not self._is_open(spin):
            await interaction.response.send_message("Ya arrancó la partida (o pasó el tiempo): está cerrada.",
                                                    ephemeral=True)
            return
        if action == "re":
            await self._reroll(interaction, spin)
        elif action == "pun":
            await self._punish_menu(interaction, spin)
        else:
            await self._bet_click(interaction, spin, action)

    async def _can_pay(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member) or not has_role(member, self.bot.settings.points_role_id):
            await interaction.response.send_message("Necesitás el rol de Boguero para usar puntos.", ephemeral=True)
            return False
        return True

    async def _reroll(self, interaction: discord.Interaction, spin: Spin) -> None:
        uid = interaction.user.id
        pick = spin.picks.get(uid)
        if pick is None:
            await interaction.response.send_message("No estás en esta ruleta.", ephemeral=True)
            return
        if pick.punished:
            await interaction.response.send_message("Te castigaron: bancátela. 😈", ephemeral=True)
            return
        if not await self._can_pay(interaction):
            return
        cost = self._reroll_cost(pick.rerolls)
        async with self.store.lock:
            if not await self.bot.points.spend(uid, cost, f"ruleta re-tirar {spin.id}"):
                await interaction.response.send_message(
                    f"Re-tirar sale **{cost}** y tenés {self.bot.points.balance(uid)}.", ephemeral=True)
                return
            taken = {p.champion_id for u, p in spin.picks.items() if u != uid} | {pick.champion_id}
            _, comfort = await self._history([uid])
            new = make_pick(pick.role, self.ddragon.champions, self.ddragon.items, self.ddragon.boots, self._rng,
                            comfort=comfort.get(uid, set()), taken=taken,
                            cursed_chance=self.bot.settings.roulette_cursed_chance)
            new.rerolls = pick.rerolls + 1
            spin.picks[uid] = new
            await self.store.save()
        await interaction.response.edit_message(embed=self._embed(spin), view=self._view(spin))
        await interaction.followup.send(f"🔄 Te salió **{new.champion_name}** (-{cost} pts). La próxima sale "
                                        f"{self._reroll_cost(new.rerolls)}.", ephemeral=True)

    async def _punish_menu(self, interaction: discord.Interaction, spin: Spin) -> None:
        if not await self._can_pay(interaction):
            return
        uid = interaction.user.id
        cost = self.bot.settings.roulette_punish_cost
        targets = [(t, p) for t, p in spin.picks.items() if t != uid and not p.punished]
        if not targets:
            await interaction.response.send_message("No queda nadie para castigar.", ephemeral=True)
            return
        if self.bot.points.balance(uid) < cost:
            await interaction.response.send_message(
                f"Castigar sale **{cost}** y tenés {self.bot.points.balance(uid)}.", ephemeral=True)
            return
        options = []
        for t, p in targets:
            name = self._name(interaction, t)
            options.append(discord.SelectOption(label=f"{name} ({p.champion_name})"[:100], value=str(t)))
        select = discord.ui.Select(placeholder="¿A quién castigás?", options=options)

        async def on_select(inter: discord.Interaction) -> None:
            await self._punish(inter, spin.id, int(select.values[0]))

        select.callback = on_select
        view = discord.ui.View(timeout=120)
        view.add_item(select)
        await interaction.response.send_message(f"😈 Castigar sale **{cost}** pts: le toca lo peor de la ruleta.",
                                                view=view, ephemeral=True)

    @staticmethod
    def _name(interaction: discord.Interaction, user_id: int) -> str:
        member = interaction.guild.get_member(user_id) if interaction.guild else None
        return member.display_name if member else f"Jugador {user_id}"

    async def _punish(self, interaction: discord.Interaction, spin_id: str, target: int) -> None:
        uid = interaction.user.id
        cost = self.bot.settings.roulette_punish_cost
        async with self.store.lock:
            spin = self.store.spins.get(spin_id)
            pick = spin.picks.get(target) if spin else None
            if spin is None or pick is None or not self._is_open(spin):
                error: str | None = "La ruleta ya cerró."
            elif pick.punished:
                error = "Ya lo castigaron."
            elif not await self.bot.points.spend(uid, cost, f"ruleta castigo {spin_id}"):
                error = f"Castigar sale **{cost}** y tenés {self.bot.points.balance(uid)}."
            else:
                error = None
                taken = {p.champion_id for u, p in spin.picks.items() if u != target}
                spin.picks[target] = make_pick(pick.role, self.ddragon.champions, self.ddragon.items,
                                               self.ddragon.boots, self._rng, taken=taken, worst=True)
                await self.store.save()
            if error:
                await interaction.response.edit_message(content=error, view=None)
                return
        new = spin.picks[target]
        await interaction.response.edit_message(
            content=f"😈 Listo: a <@{target}> le toca **{new.champion_name}** {THEMES.get(new.theme, '')} "
                    f"(-{cost} pts).", view=None)
        await self._edit(spin, embed=self._embed(spin))
        channel = self.bot.get_channel(spin.channel_id)
        if isinstance(channel, discord.TextChannel):
            await channel.send(f"😈 <@{uid}> castigó a <@{target}>: le toca **{new.champion_name}** en "
                               f"{ROLE_LABELS.get(new.role, new.role)} con {THEMES.get(new.theme, new.theme)}.",
                               allowed_mentions=discord.AllowedMentions(users=[discord.Object(target)]))

    async def _bet_click(self, interaction: discord.Interaction, spin: Spin, option: str) -> None:
        error = bettor_error(self.bot, interaction.user, self.bot.settings.prediction_min_bet)
        if error is None:
            error = spin.market.bet_error(interaction.user.id, option)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        async def on_amount(inter: discord.Interaction, raw: str) -> None:
            uid = inter.user.id
            async with self.store.lock:
                current = self.store.spins.get(spin.id)
                if current is None or not self._is_open(current):
                    bet_error: str | None = "La ruleta ya cerró."
                else:
                    bet_error = await charge_bet(self.bot, uid, current.market, option, raw, f"ruleta {spin.id}")
                if bet_error:
                    await inter.response.send_message(bet_error, ephemeral=True)
                    return
                await self.store.save()
            await inter.response.send_message(bet_done_text(self.bot, uid, current.market, option), ephemeral=True)
            await self._edit(current, embed=self._embed(current))

        await interaction.response.send_modal(amount_modal(self.bot, interaction.user.id,
                                                           spin.market.options[option], on_amount))

    # ------------------------------------------------------------------ #
    # Partida en vivo, bonus, resolución y vencimiento
    # ------------------------------------------------------------------ #
    async def _on_live_game(self, match_id: str, group: dict[int, int]) -> None:
        changed: list[Spin] = []
        async with self.store.lock:
            for spin in self.store.spins.values():
                if spin.is_open and set(spin.picks) & set(group) and _now() - spin.created_at < dt.timedelta(
                        hours=self.bot.settings.roulette_hours):
                    spin.match_id = match_id
                    changed.append(spin)
            if changed:
                await self.store.save()
        for spin in changed:
            await self._edit(spin, embed=self._embed(spin, "🔒 Arrancó la partida. ¡A cumplir con la ruleta!"),
                             view=self._view(spin, disabled=True))

    def _target_game(self, spin: Spin, records: list[MatchRecord]) -> list[MatchRecord]:
        """Los registros de la partida que define la apuesta."""
        mine = [r for r in records if r.discord_id in spin.picks and r.game_creation >= spin.created_at]
        if spin.match_id is not None:
            return [r for r in mine if r.match_id == spin.match_id]
        if not mine:
            return []
        first = min(mine, key=lambda r: r.game_creation).match_id
        return [r for r in mine if r.match_id == first]

    async def _on_new_matches(self, records: list[MatchRecord]) -> None:
        s = self.bot.settings
        for spin in list(self.store.spins.values()):
            if _now() - spin.created_at > dt.timedelta(hours=s.roulette_hours):
                continue
            bonus: dict[int, int] = {}
            result: str | None = None
            async with self.store.lock:
                if spin.id not in self.store.spins:
                    continue
                for r in records:
                    if r.game_creation < spin.created_at or r.discord_id in spin.paid:
                        continue
                    if spin.complied(r.discord_id, r.champion, r.position) \
                            and await user_has_role(self.bot, r.discord_id, s.points_role_id):
                        pick = spin.picks[r.discord_id]
                        bonus[r.discord_id] = s.roulette_bonus * (2 if pick.punished else 1)
                        spin.paid.append(r.discord_id)
                game = self._target_game(spin, records) if not spin.resolved else []
                if game:
                    complied = [r for r in game if spin.complied(r.discord_id, r.champion, r.position)]
                    if complied:
                        winner = "yes" if complied[0].win else "no"
                        paid = spin.market.payouts(winner)
                        result = f"🏁 Salió **{spin.market.options[winner]}**."
                    else:
                        paid = spin.market.refunds()
                        result = "🏁 Nadie respetó la ruleta 🙄: se devolvieron las apuestas."
                    for uid, amount in paid.items():
                        bonus[uid] = bonus.get(uid, 0) + amount
                    spin.resolved = True
                if not bonus and result is None:
                    continue
                await self.bot.points.add(bonus, f"ruleta {spin.id}")
                await self.store.save()
            status = result or "🎰 Hay gente que cumplió con la ruleta."
            await self._edit(spin, embed=self._embed(spin, status, discord.Color.green()), view=None)

    @tasks.loop(minutes=5)
    async def tick_job(self) -> None:
        """Vence las tiradas viejas (devuelve la apuesta si no se resolvió)."""
        expired: list[Spin] = []
        async with self.store.lock:
            for spin in list(self.store.spins.values()):
                if _now() - spin.created_at > dt.timedelta(hours=self.bot.settings.roulette_hours):
                    if not spin.resolved:
                        await self.bot.points.add(spin.market.refunds(), f"ruleta vencida {spin.id}")
                    del self.store.spins[spin.id]
                    expired.append(spin)
            if expired:
                await self.store.save()
        for spin in list(self.store.spins.values()):
            if spin.is_open and not self._is_open(spin) and spin.id not in self._closed_shown:
                self._closed_shown.add(spin.id)
                await self._edit(spin, view=self._view(spin, disabled=True))
        for spin in expired:
            if not spin.resolved and spin.market.bets:
                await self._edit(spin, embed=self._embed(spin, "⌛ No hubo partida: se devolvieron las apuestas.",
                                                         discord.Color.dark_grey()), view=None)
            elif spin.is_open:
                await self._edit(spin, view=self._view(spin, disabled=True))

    @tick_job.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()
