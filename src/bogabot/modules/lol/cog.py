"""Cog con los slash commands del módulo LoL:
/link, /unlink, /link-admin, /ingest-now, /ranking, /help, /ayuda.

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

    def _has_role(self, member: discord.Member, role_id: int | None) -> bool:
        if role_id is None:
            return False
        return any(role.id == role_id for role in member.roles)

    def _is_dev(self, member: discord.Member) -> bool:
        return self._has_role(member, self.bot.settings.dev_role_id)

    def _is_player(self, member: discord.Member) -> bool:
        return self._has_role(member, self.bot.settings.player_role_id)

    async def _grant_lol_role(self, member: discord.Member) -> None:
        """Le asigna el rol de LoL configurado (ver LOL_ROLE_ID) al vincularse.
        Da acceso a los canales del módulo sin molestar al resto del server.
        No frena el flujo de /link si falla (falta de permisos, rol borrado, etc)."""
        role_id = self.bot.settings.lol_role_id
        if role_id is None:
            return
        if any(role.id == role_id for role in member.roles):
            return
        try:
            await member.add_roles(discord.Object(id=role_id), reason="Vinculación de cuenta de Riot")
        except discord.HTTPException:
            log.exception("No pude asignar el rol LOL_ROLE_ID (%s) a %s.", role_id, member.id)

    async def _check_admin(self, interaction: discord.Interaction) -> bool:
        """Valida canal + rol dev para comandos de administración.
        Si no pasa, responde el motivo y devuelve False."""
        admin_channel_id = self.bot.settings.admin_channel_id
        if admin_channel_id and interaction.channel_id != admin_channel_id:
            await interaction.followup.send(f"Este comando solo se puede usar en <#{admin_channel_id}>.")
            return False
        if not isinstance(interaction.user, discord.Member) or not self._is_dev(interaction.user):
            await interaction.followup.send("No tenés permiso para usar este comando.")
            return False
        return True

    @staticmethod
    def _parse_riot_id(riot_id: str) -> tuple[str, str] | None:
        """Separa "Nombre#TAG" en sus partes, o None si el formato es inválido."""
        if "#" not in riot_id:
            return None
        game_name, _, tag_line = riot_id.rpartition("#")
        game_name, tag_line = game_name.strip(), tag_line.strip()
        if not game_name or not tag_line:
            return None
        return game_name, tag_line

    @app_commands.command(name="link", description="Vinculá tu cuenta de Riot (formato Nombre#TAG).")
    @app_commands.describe(riot_id="Tu Riot ID completo, ej: Faker#KR1")
    async def link(self, interaction: discord.Interaction, riot_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self._storage_ready():
            await interaction.followup.send("El bot todavía se está inicializando, probá en unos segundos.")
            return

        parsed = self._parse_riot_id(riot_id)
        if parsed is None:
            await interaction.followup.send("Formato inválido. Usá `Nombre#TAG`, por ejemplo `Faker#KR1`.")
            return
        game_name, tag_line = parsed

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
        if isinstance(interaction.user, discord.Member):
            await self._grant_lol_role(interaction.user)
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

    @app_commands.command(
        name="link-admin",
        description="Vinculá la cuenta de Riot de otro usuario del server (solo rol dev).",
    )
    @app_commands.describe(
        usuario="Usuario de Discord a vincular (debe estar en el server)",
        riot_id="Riot ID completo del usuario, ej: Faker#KR1",
    )
    async def link_admin(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        riot_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not await self._check_admin(interaction):
            return

        if not self._storage_ready():
            await interaction.followup.send("El bot todavía se está inicializando, probá en unos segundos.")
            return

        parsed = self._parse_riot_id(riot_id)
        if parsed is None:
            await interaction.followup.send("Formato inválido. Usá `Nombre#TAG`, por ejemplo `Faker#KR1`.")
            return
        game_name, tag_line = parsed

        try:
            account = await self.bot.riot.get_account_by_riot_id(game_name, tag_line)
        except NotFoundError:
            await interaction.followup.send(f"No encontré la cuenta `{game_name}#{tag_line}`. Revisá el Riot ID.")
            return
        except RiotApiError:
            log.exception("Error de la Riot API en /link-admin.")
            await interaction.followup.send("Hubo un problema consultando a Riot. Probá de nuevo más tarde.")
            return

        link = PlayerLink(
            discord_id=usuario.id,
            game_name=account.get("gameName", game_name),
            tag_line=account.get("tagLine", tag_line),
            puuid=account["puuid"],
            linked_at=datetime.now(timezone.utc),
        )
        await self.bot.storage.save_link(link)
        await self._grant_lol_role(usuario)
        await interaction.followup.send(
            f"✅ Vinculé a {usuario.mention} con **{link.riot_id}**. "
            f"Sus partidas de esta semana van a empezar a contar para el ranking."
        )

    @app_commands.command(
        name="unlink-admin",
        description="Desvinculá la cuenta de Riot de otro usuario del server (solo rol dev).",
    )
    @app_commands.describe(usuario="Usuario de Discord a desvincular")
    async def unlink_admin(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not await self._check_admin(interaction):
            return

        if not self._storage_ready():
            await interaction.followup.send("El bot todavía se está inicializando, probá en unos segundos.")
            return

        deleted = await self.bot.storage.delete_link(usuario.id)
        if deleted:
            await interaction.followup.send(
                f"🗑️ Listo, desvinculé a {usuario.mention}. (Las partidas ya guardadas quedan en el historial.)"
            )
        else:
            await interaction.followup.send(f"{usuario.mention} no tenía ninguna cuenta vinculada.")

    @app_commands.command(
        name="ingest-now",
        description="Forzá una ingesta de partidas ahora mismo (solo rol dev).",
    )
    async def ingest_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        if not await self._check_admin(interaction):
            return

        if not self._storage_ready():
            await interaction.followup.send("El bot todavía se está inicializando, probá en unos segundos.")
            return

        new_records = await self.bot.ingest.ingest_all()
        await interaction.followup.send(
            f"✅ Ingesta manual completa: {len(new_records)} partidas-jugador nuevas guardadas. "
            f"Las que ya estaban se saltearon (dedup por partida+jugador)."
        )

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

    async def _send_help(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        is_dev = member is not None and self._is_dev(member)
        is_player = member is not None and self._is_player(member)

        embed = discord.Embed(title="📖 Comandos de BogaBot", color=discord.Color.blurple())

        if is_dev:
            embed.add_field(
                name="🛠️ Administración (rol dev)",
                value=(
                    "`/link-admin <usuario> <Nombre#TAG>` — vinculá la cuenta de Riot de otro usuario del server.\n"
                    "`/unlink-admin <usuario>` — desvinculá la cuenta de Riot de otro usuario del server.\n"
                    "`/ingest-now` — forzá una ingesta de partidas ahora mismo."
                ),
                inline=False,
            )

        if is_dev or is_player:
            embed.add_field(
                name="🎮 Ranking",
                value=(
                    "`/link <Nombre#TAG>` — vinculá tu cuenta de Riot.\n"
                    "`/unlink` — desvinculá tu cuenta.\n"
                    "`/ranking [Hoy|Semana]` — mostrá el ranking del grupo.\n"
                    "*(Solo cuentan las partidas jugadas con al menos otro vinculado del grupo.)*"
                ),
                inline=False,
            )

        if not is_dev and not is_player:
            embed.description = "Todavía no tenés un rol con comandos asignados. Hablá con un admin del server."
        else:
            embed.add_field(
                name="ℹ️ Ayuda",
                value="`/help` / `/ayuda` — mostrá este mensaje.",
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="help", description="Mostrá los comandos que podés usar.")
    async def help_command(self, interaction: discord.Interaction) -> None:
        await self._send_help(interaction)

    @app_commands.command(name="ayuda", description="Mostrá los comandos que podés usar.")
    async def ayuda_command(self, interaction: discord.Interaction) -> None:
        await self._send_help(interaction)
