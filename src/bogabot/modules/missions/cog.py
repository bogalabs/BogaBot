"""Misiones semanales: /misiones muestra las de la semana y el progreso.

El progreso lo reportan otros módulos vía `bot.missions` (voz y /daily en
puntos, apuestas en predicciones y duelos); este cog suma lo que sale de
las partidas (listener de IngestService) y anuncia las completadas en
MISSIONS_CHANNEL_ID (default BOT_CHANNEL_ID; sin canal, no se anuncia).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bogabot.core.models import MatchRecord, TrollLevel
from bogabot.modules.missions.logic import Mission
from bogabot.modules.points.cog import user_has_role

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

log = logging.getLogger(__name__)


class MissionsCog(commands.Cog):
    def __init__(self, bot: "BogaBot") -> None:
        self.bot = bot
        self.missions = bot.missions

    async def cog_load(self) -> None:
        self.bot.ingest.add_listener(self._on_new_matches)
        self.missions.set_on_complete(self._announce)

    async def cog_unload(self) -> None:
        self.bot.ingest.remove_listener(self._on_new_matches)
        self.missions.set_on_complete(None)

    async def _on_new_matches(self, records: list[MatchRecord]) -> None:
        role = self.bot.settings.points_role_id
        counts: dict[str, list[int]] = {"games": [], "wins": [], "carry": [], "clean": []}
        for r in records:
            if not await user_has_role(self.bot, r.discord_id, role):
                continue
            counts["games"].append(r.discord_id)
            if r.win:
                counts["wins"].append(r.discord_id)
            if self.bot.carries.evaluate(r).level >= TrollLevel.TROLL:
                counts["carry"].append(r.discord_id)
            if self.bot.trolls.evaluate(r).level == TrollLevel.NONE:
                counts["clean"].append(r.discord_id)
        for code, uids in counts.items():
            # Un mismo jugador puede tener varias partidas en una tanda.
            for uid in set(uids):
                await self.missions.progress([uid], code, uids.count(uid))

    async def _announce(self, user_id: int, mission: Mission, reward: int) -> None:
        s = self.bot.settings
        channel_id = s.missions_channel_id or s.bot_channel_id
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        await channel.send(f"✅ <@{user_id}> completó la misión **{mission.describe()}** (+{reward} puntos).",
                           allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="misiones", description="Las misiones de la semana y cómo vas.")
    async def misiones(self, interaction: discord.Interaction) -> None:
        lines = []
        for mission, value, done in self.missions.status(interaction.user.id):
            mark = "✅" if done else "⬜"
            lines.append(f"{mark} {mission.describe()} — `{mission.progress_text(value)}`")
        embed = discord.Embed(title="🗺️ Misiones de la semana", description="\n".join(lines),
                              color=discord.Color.blurple())
        embed.set_footer(text=f"{self.missions.reward} puntos por cada una. Se renuevan los lunes.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
