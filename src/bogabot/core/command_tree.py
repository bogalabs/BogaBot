"""CommandTree con un chequeo global de canal para los slash commands.

Si BOT_CHANNEL_ID está seteado, los miembros comunes solo pueden usar
comandos en ese canal (o en ADMIN_CHANNEL_ID). Los devs (DEV_ROLE_ID) pueden
usarlos en cualquier lado. Los comandos de administración siguen validando
su propio canal en cada cog.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from bogabot.bot import BogaBot


class BogaCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot: BogaBot = self.client  # type: ignore[assignment]
        settings = bot.settings
        if settings.bot_channel_id is None:
            return True
        if interaction.channel_id in (settings.bot_channel_id, settings.admin_channel_id):
            return True

        member = interaction.user
        if isinstance(member, discord.Member) and settings.dev_role_id is not None \
                and any(role.id == settings.dev_role_id for role in member.roles):
            return True

        await interaction.response.send_message(
            f"Los comandos del bot se usan en <#{settings.bot_channel_id}> 🙏", ephemeral=True)
        return False
