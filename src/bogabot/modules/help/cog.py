"""Cog de ayuda: /help y /ayuda.

Arma la lista de comandos según los roles de quien pregunta y los módulos
que estén cargados. La respuesta es efímera (solo la ve quien la pidió).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bogabot.bot import BogaBot


class HelpCog(commands.Cog):
    def __init__(self, bot: "BogaBot") -> None:
        self.bot = bot

    @staticmethod
    def _has_role(member: discord.Member | None, role_id: int | None) -> bool:
        return member is not None and role_id is not None and any(r.id == role_id for r in member.roles)

    def _build_embed(self, member: discord.Member | None) -> discord.Embed:
        s = self.bot.settings
        is_dev = self._has_role(member, s.dev_role_id)
        is_player = self._has_role(member, s.player_role_id)
        has_points = self.bot.get_cog("PointsCog") is not None
        can_earn = self._has_role(member, s.points_role_id)

        embed = discord.Embed(title="📖 Comandos de BogaBot", color=discord.Color.blurple())
        if s.bot_channel_id is not None:
            embed.description = f"Usalos en <#{s.bot_channel_id}>. Los marcados con 🔒 te responden solo a vos."

        if is_dev or is_player:
            embed.add_field(
                name="🎮 Ranking LoL",
                value=(
                    "`/link <Nombre#TAG>` 🔒 — vinculá tu cuenta de Riot.\n"
                    "`/unlink` 🔒 — desvinculá tu cuenta.\n"
                    "`/ranking [Hoy|Semana]` — mostrá el ranking del grupo.\n"
                    "*(Solo cuentan las partidas jugadas con al menos otro vinculado del grupo.)*"
                ),
                inline=False,
            )

        if has_points:
            value = (
                "`/puntos [usuario]` 🔒 — cuántos puntos tenés y cómo se ganan.\n"
                "`/puntos-top` — ranking de puntos del server."
            )
            if not can_earn:
                value += f"\n*(Para juntar puntos necesitás el rol <@&{s.points_role_id}>.)*"
            embed.add_field(name="💰 Puntos", value=value, inline=False)

        sounds = [
            "`/sonido [nombre]` — el bot entra a tu canal de voz y tira un sonido.",
            "`/sonidos` 🔒 — lista de sonidos cargados.",
        ]
        if has_points and can_earn:
            sounds.append(f"`/canjear-sonido <nombre> <audio>` 🔒 — cambiá {s.sound_redeem_cost} puntos "
                          f"por un sonido tuyo por {s.sound_redeem_days} días.")
            sounds.append("`/sonido-del <nombre>` 🔒 — borrá un sonido que canjeaste.")
        embed.add_field(name="🔊 Sonidos", value="\n".join(sounds), inline=False)

        if is_dev:
            admin_where = f" (en <#{s.admin_channel_id}>)" if s.admin_channel_id else ""
            embed.add_field(
                name="🛠️ Administración (rol dev)",
                value=(
                    f"`/link-admin <usuario> <Nombre#TAG>`{admin_where} — vinculá la cuenta de otro.\n"
                    f"`/unlink-admin <usuario>`{admin_where} — desvinculá la cuenta de otro.\n"
                    f"`/ingest-now`{admin_where} — forzá una ingesta de partidas.\n"
                    "`/puntos-dar <usuario> <cantidad>` — sumá o restá puntos.\n"
                    "`/sonido-forzar [nombre] [canal]` — el bot entra a un canal de voz y tira ese sonido.\n"
                    "`/sonido-add <nombre> <audio>` — subí un sonido permanente.\n"
                    "`/sonido-del <nombre>` — borrá cualquier sonido.\n"
                    "*(Los devs pueden usar los comandos en cualquier canal.)*"
                ),
                inline=False,
            )

        embed.add_field(name="ℹ️ Ayuda", value="`/help` / `/ayuda` 🔒 — este mensaje.", inline=False)
        return embed

    async def _send_help(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        await interaction.response.send_message(embed=self._build_embed(member), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="help", description="Mostrá los comandos que podés usar.")
    async def help_command(self, interaction: discord.Interaction) -> None:
        await self._send_help(interaction)

    @app_commands.command(name="ayuda", description="Mostrá los comandos que podés usar.")
    async def ayuda_command(self, interaction: discord.Interaction) -> None:
        await self._send_help(interaction)
