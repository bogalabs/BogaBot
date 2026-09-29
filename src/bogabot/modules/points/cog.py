"""Módulo "puntos": los miembros con POINTS_ROLE_ID juntan puntos y los
canjean (por ahora, por subir un sonido temporal: ver `/canjear-sonido` en
el módulo de sonidos).

Cómo se ganan:
  - Voz: voice_job corre cada POINTS_VOICE_INTERVAL_MINUTES y da
    POINTS_VOICE_AMOUNT a cada miembro con el rol que esté en un canal de voz
    con al menos otro humano (no cuenta AFK ni estar ensordecido), con tope
    diario POINTS_VOICE_DAILY_CAP.
  - LoL: por cada partida nueva que ingiere el módulo LoL (listener de
    IngestService), POINTS_LOL_GAME por jugarla + POINTS_LOL_WIN si la ganó.
    El dedup de la ingesta garantiza que cada partida se paga una sola vez.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bogabot.core.models import MatchRecord
from bogabot.core.timeutils import get_tz

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

log = logging.getLogger(__name__)


def has_role(member: discord.Member, role_id: int | None) -> bool:
    return role_id is not None and any(r.id == role_id for r in member.roles)


class PointsCog(commands.Cog):
    def __init__(self, bot: "BogaBot") -> None:
        self.bot = bot
        self.points = bot.points

    async def cog_load(self) -> None:
        s = self.bot.settings
        self.bot.ingest.add_listener(self._on_new_matches)
        self.voice_job.change_interval(minutes=s.points_voice_interval_minutes)
        self.voice_job.start()
        log.info("Puntos: %d cada %d min en voz (tope %d/día), LoL %d por partida + %d por victoria.",
                 s.points_voice_amount, s.points_voice_interval_minutes, s.points_voice_daily_cap,
                 s.points_lol_game, s.points_lol_win)

    async def cog_unload(self) -> None:
        self.bot.ingest.remove_listener(self._on_new_matches)
        if self.voice_job.is_running():
            self.voice_job.cancel()

    def _guilds(self) -> list[discord.Guild]:
        gid = self.bot.settings.guild_id
        return [g for g in self.bot.guilds if not gid or g.id == gid]

    # ------------------------------------------------------------------ #
    # Fuentes de puntos
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=5)
    async def voice_job(self) -> None:
        s = self.bot.settings
        eligible: list[int] = []
        for guild in self._guilds():
            for ch in guild.voice_channels:
                if guild.afk_channel and ch.id == guild.afk_channel.id:
                    continue
                humans = [m for m in ch.members if not m.bot]
                if len(humans) < 2:
                    continue  # solo en el canal no cuenta
                for m in humans:
                    deaf = m.voice is not None and (m.voice.self_deaf or m.voice.deaf)
                    if not deaf and has_role(m, s.points_role_id):
                        eligible.append(m.id)
        if not eligible:
            return
        day = dt.datetime.now(get_tz(s.timezone)).date().isoformat()
        awarded = await self.points.add_voice(eligible, s.points_voice_amount, day, s.points_voice_daily_cap)
        if awarded:
            log.debug("Puntos por voz: %s", awarded)

    @voice_job.before_loop
    async def _before_voice_job(self) -> None:
        await self.bot.wait_until_ready()

    async def _on_new_matches(self, records: list[MatchRecord]) -> None:
        s = self.bot.settings
        amounts: dict[int, int] = {}
        for r in records:
            if not await self._discord_id_has_role(r.discord_id):
                continue
            amounts[r.discord_id] = amounts.get(r.discord_id, 0) + s.points_lol_game + (s.points_lol_win if r.win else 0)
        await self.points.add(amounts, "partidas LoL")

    async def _discord_id_has_role(self, user_id: int) -> bool:
        for guild in self._guilds():
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.HTTPException:
                    continue
            if has_role(member, self.bot.settings.points_role_id):
                return True
        return False

    # ------------------------------------------------------------------ #
    # Slash commands
    # ------------------------------------------------------------------ #
    @app_commands.command(name="puntos", description="Mirá cuántos puntos tenés (o los de otro).")
    @app_commands.describe(usuario="De quién ver los puntos. Vacío = vos.")
    async def puntos(self, interaction: discord.Interaction, usuario: discord.Member | None = None) -> None:
        s = self.bot.settings
        target = usuario or interaction.user
        bal = self.points.balance(target.id)
        who = "Tenés" if target.id == interaction.user.id else f"{target.display_name} tiene"
        text = (
            f"{who} **{bal}** puntos.\n\n"
            f"**Cómo se ganan** (con el rol <@&{s.points_role_id}>):\n"
            f"• {s.points_voice_amount} cada {s.points_voice_interval_minutes} min en voz con alguien más "
            f"(máx {s.points_voice_daily_cap}/día)\n"
            f"• {s.points_lol_game} por partida de LoL con el grupo, +{s.points_lol_win} si ganás\n\n"
            f"**Canje:** `/canjear-sonido` → {s.sound_redeem_cost} pts, sonido de hasta "
            f"{s.sound_redeem_max_seconds} s por {s.sound_redeem_days} días."
        )
        await interaction.response.send_message(text, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="puntos-top", description="Ranking de puntos del server.")
    async def puntos_top(self, interaction: discord.Interaction) -> None:
        rows = self.points.top(10)
        if not rows:
            await interaction.response.send_message("Nadie tiene puntos todavía.", ephemeral=True)
            return
        lines = [f"`{i}.` <@{uid}> — **{bal}**" for i, (uid, bal) in enumerate(rows, 1)]
        embed = discord.Embed(title="💰 Top puntos", description="\n".join(lines), color=discord.Color.gold())
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="puntos-dar", description="Suma o resta puntos a alguien (solo devs).")
    @app_commands.describe(usuario="A quién.", cantidad="Puntos a sumar (negativo para restar).")
    async def puntos_dar(self, interaction: discord.Interaction, usuario: discord.Member, cantidad: int) -> None:
        if not isinstance(interaction.user, discord.Member) \
                or not has_role(interaction.user, self.bot.settings.dev_role_id):
            await interaction.response.send_message("No tenés permiso para usar este comando.", ephemeral=True)
            return
        await self.points.add({usuario.id: cantidad}, f"manual por {interaction.user}")
        await interaction.response.send_message(
            f"Listo: {cantidad:+d} a {usuario.mention}. Saldo: **{self.points.balance(usuario.id)}**.",
            ephemeral=True)
