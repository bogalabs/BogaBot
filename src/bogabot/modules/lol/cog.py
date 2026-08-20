"""Cog con los slash commands del módulo LoL: /link, /unlink, /ranking.

El cog es "delgado": valida input, llama a los servicios (riot, storage,
ranking) y responde. Toda la lógica de negocio vive en los servicios, no acá.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bogabot.core.models import PlayerLink
from bogabot.riot.client import NotFoundError, RiotApiError

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

log = logging.getLogger(__name__)


class LolCog(commands.Cog):
    def __init__(self, bot: "BogaBot") -> None:
        self.bot = bot

    def _storage_ready(self) -> bool:
        return self.bot.storage.ready

    @app_commands.command(name="link", description="Vinculá tu cuenta de Riot (formato Nombre#TAG).")
    @app_commands.describe(riot_id="Tu Riot ID completo, ej: Faker#KR1")
    async def link(self, interaction: discord.Interaction, riot_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self._storage_ready():
            await interaction.followup.send("El bot todavía se está inicializando, probá en unos segundos.")
            return

        if "#" not in riot_id:
            await interaction.followup.send("Formato inválido. Usá `Nombre#TAG`, por ejemplo `Faker#KR1`.")
            return
        game_name, _, tag_line = riot_id.rpartition("#")
        game_name, tag_line = game_name.strip(), tag_line.strip()
        if not game_name or not tag_line:
            await interaction.followup.send("Formato inválido. Usá `Nombre#TAG`, por ejemplo `Faker#KR1`.")
            return

        try:
            account = await self.bot.riot.get_account_by_riot_id(game_name, tag_line)
        except NotFoundError:
            await interaction.followup.send(f"No encontré la cuenta `{game_name}#{tag_line}`. Revisá el Riot ID.")
            return
        except RiotApiError:
            log.exception("Error de la Riot API en /link.")
            await interaction.followup.send("Hubo un problema consultando a Riot. Probá de nuevo más tarde.")
            return

        link = PlayerLink(
            discord_id=interaction.user.id,
            game_name=account.get("gameName", game_name),
            tag_line=account.get("tagLine", tag_line),
            puuid=account["puuid"],
            linked_at=datetime.now(timezone.utc),
        )
        await self.bot.storage.save_link(link)
        await interaction.followup.send(
            f"✅ Vinculé tu Discord con **{link.riot_id}**. "
            f"Tus partidas de esta semana van a empezar a contar para el ranking."
        )

    @app_commands.command(name="unlink", description="Desvinculá tu cuenta de Riot.")
    async def unlink(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self._storage_ready():
            await interaction.followup.send("El bot todavía se está inicializando, probá en unos segundos.")
            return
        deleted = await self.bot.storage.delete_link(interaction.user.id)
        if deleted:
            await interaction.followup.send("🗑️ Listo, desvinculé tu cuenta. (Las partidas ya guardadas quedan en el historial.)")
        else:
            await interaction.followup.send("No tenías ninguna cuenta vinculada.")

    @app_commands.command(name="ranking", description="Mostrá el ranking actual del grupo.")
    @app_commands.describe(periodo="Ventana de tiempo del ranking")
    @app_commands.choices(
        periodo=[
            app_commands.Choice(name="Hoy", value="daily"),
            app_commands.Choice(name="Semana", value="weekly"),
        ]
    )
    async def ranking(
        self,
        interaction: discord.Interaction,
        periodo: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        if not self._storage_ready():
            await interaction.followup.send("El bot todavía se está inicializando, probá en unos segundos.")
            return

        scope = periodo.value if periodo else "daily"
        if scope == "weekly":
            rows = await self.bot.ranking.weekly_rows()
            embed = self.bot.ranking.build_ranking_embed(rows, "🏆 Ranking de la semana")
        else:
            rows = await self.bot.ranking.daily_rows()
            embed = self.bot.ranking.build_ranking_embed(rows, "🏆 Ranking de hoy")
        await interaction.followup.send(embed=embed)
