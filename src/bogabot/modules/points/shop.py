"""Tienda de canjes de texto: formas de gastar los puntos sin meter al bot en
un canal de voz. Todo es temporal y se revierte solo (expire_job):

  - /canjear-rol <nombre> <color>: rol con nombre y color propios por
    SHOP_ROLE_DAYS días.
  - /canjear-inmunidad: tu próxima alerta troll (o papelón) no se anuncia.
    Sigue sumando al ranking troll: solo se saltea el aviso. Se engancha como
    filtro de `TrollService` (LoL no sabe de puntos).
  - /canjear-apodo <usuario> <apodo>: le ponés un apodo a otro por
    SHOP_NICK_HOURS horas. El afectado lo puede vetar con /vetar-apodo (se le
    devuelve la mitad a quien pagó).
  - /canjear-mensaje <texto>: el bot publica tu mensaje en #general y lo fija
    por SHOP_PIN_HOURS horas. Sin menciones.

Si la acción falla (permisos, jerarquía de roles), se devuelven los puntos.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bogabot.core.models import TrollVerdict
from bogabot.modules.points.cog import has_role
from bogabot.modules.points.shop_store import ShopEffect, ShopStore

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

log = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()
_MAX_ROLE_NAME = 32
_MAX_NICK = 32
_MAX_PIN_TEXT = 200


def parse_hex_color(raw: str) -> discord.Colour | None:
    """'#ff00aa' o 'ff00aa' -> Colour; None si no es un hex de 6 dígitos."""
    raw = raw.strip().lstrip("#")
    if len(raw) != 6:
        return None
    try:
        return discord.Colour(int(raw, 16))
    except ValueError:
        return None


class ShopCog(commands.Cog):
    def __init__(self, bot: "BogaBot", store: ShopStore) -> None:
        self.bot = bot
        self.store = store

    async def cog_load(self) -> None:
        self.bot.trolls.add_alert_filter(self._shield_filter)
        self.expire_job.start()

    async def cog_unload(self) -> None:
        self.bot.trolls.remove_alert_filter(self._shield_filter)
        self.expire_job.cancel()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _shield_filter(self, verdict: TrollVerdict) -> bool:
        return await self.store.use_shield(verdict.record.discord_id)

    async def _charge(self, interaction: discord.Interaction, cost: int, reason: str) -> bool:
        """Chequea rol y saldo y cobra. Responde el error (followup) si no se
        puede. La interacción tiene que estar diferida."""
        member = interaction.user
        if not isinstance(member, discord.Member) or not has_role(member, self.bot.settings.points_role_id):
            await interaction.followup.send("Necesitás el rol de Boguero para canjear.")
            return False
        if not await self.bot.points.spend(member.id, cost, reason):
            await interaction.followup.send(
                f"Te faltan puntos: tenés **{self.bot.points.balance(member.id)}** y esto cuesta **{cost}**. "
                f"Mirá `/puntos`.")
            return False
        return True

    async def _refund(self, user_id: int, amount: int, reason: str) -> None:
        await self.bot.points.add({user_id: amount}, f"reintegro {reason}")

    @staticmethod
    def _effect_id(kind: str, user_id: int) -> str:
        return f"{kind}-{user_id}-{time.time_ns()}"

    def _guild(self) -> discord.Guild | None:
        gid = self.bot.settings.guild_id
        return self.bot.get_guild(gid) if gid else (self.bot.guilds[0] if self.bot.guilds else None)

    async def _post_public(self, interaction: discord.Interaction, text: str) -> None:
        if isinstance(interaction.channel, discord.abc.Messageable):
            try:
                await interaction.channel.send(text, allowed_mentions=_NO_MENTIONS)
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ #
    # Vencimientos
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=5)
    async def expire_job(self) -> None:
        guild = self._guild()
        if guild is None:
            return
        for effect in self.store.due(dt.datetime.now(dt.timezone.utc)):
            try:
                await self._revert(guild, effect)
            except discord.HTTPException as exc:
                log.warning("No pude revertir el canje %s (%s); lo doy por vencido igual.", effect.id, exc)
            await self.store.remove(effect.id)
            log.info("Canje %s vencido y revertido.", effect.id)

    @expire_job.before_loop
    async def _before_expire_job(self) -> None:
        await self.bot.wait_until_ready()

    async def _revert(self, guild: discord.Guild, effect: ShopEffect) -> None:
        if effect.kind == "role":
            role = guild.get_role(int(effect.data["role_id"]))
            if role is not None:
                await role.delete(reason="Canje de rol vencido")
        elif effect.kind == "nick":
            member = guild.get_member(effect.target_id)
            if member is None:
                try:
                    member = await guild.fetch_member(effect.target_id)
                except discord.NotFound:
                    return
            # Si se lo cambió por su cuenta, no se lo pisamos.
            if member.nick == effect.data.get("nick"):
                await member.edit(nick=effect.data.get("original"), reason="Canje de apodo vencido")
        elif effect.kind == "pin":
            channel = guild.get_channel(int(effect.data["channel_id"]))
            if isinstance(channel, discord.TextChannel):
                try:
                    msg = await channel.fetch_message(int(effect.data["message_id"]))
                    await msg.unpin(reason="Canje de mensaje fijado vencido")
                except discord.NotFound:
                    pass

    # ------------------------------------------------------------------ #
    # Slash commands
    # ------------------------------------------------------------------ #
    @app_commands.command(name="tienda", description="Mirá en qué podés gastar tus puntos.")
    async def tienda(self, interaction: discord.Interaction) -> None:
        s = self.bot.settings
        lines = [
            f"🔊 `/canjear-sonido` — **{s.sound_redeem_cost}** · tu sonido en el soundboard del server "
            f"por {s.sound_redeem_days} días.",
            f"🎨 `/canjear-rol` — **{s.shop_role_cost}** · rol con nombre y color propios por "
            f"{s.shop_role_days} días.",
            f"🛡️ `/canjear-inmunidad` — **{s.shop_shield_cost}** · tu próxima trolleada no se anuncia "
            f"(igual suma al ranking).",
            f"🏷️ `/canjear-apodo` — **{s.shop_nick_cost}** · ponele un apodo a alguien por "
            f"{s.shop_nick_hours} h (lo puede vetar).",
            f"📌 `/canjear-mensaje` — **{s.shop_pin_cost}** · tu mensaje fijado en general por "
            f"{s.shop_pin_hours} h.",
        ]
        if self.bot.get_cog("PredictionsCog") is not None:
            lines.append("🔮 **Predicciones** — cuando el grupo arranca una partida, apostá tus puntos "
                         "con los botones en el canal de partidas.")
        me = interaction.user.id
        extra = f"\n\nTenés **{self.bot.points.balance(me)}** puntos"
        if self.store.shields(me):
            extra += f" y **{self.store.shields(me)}** inmunidad(es) troll"
        embed = discord.Embed(title="🛒 Tienda de puntos", description="\n".join(lines) + extra + ".",
                              color=discord.Color.gold())
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=_NO_MENTIONS)

    @app_commands.command(name="canjear-rol", description="Canjeá puntos por un rol con nombre y color propios.")
    @app_commands.describe(nombre="Nombre del rol (máx 32).", color="Color en hex, ej. #ff66cc.")
    async def canjear_rol(self, interaction: discord.Interaction, nombre: str, color: str) -> None:
        await interaction.response.defer(ephemeral=True)
        s = self.bot.settings
        name = nombre.strip()[:_MAX_ROLE_NAME]
        colour = parse_hex_color(color)
        if not name or colour is None:
            await interaction.followup.send("Pasame un nombre y un color hex válido (ej. `#ff66cc`).")
            return
        guild, member = interaction.guild, interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.followup.send("Esto se usa dentro del server.")
            return
        if self.store.find("role", user_id=member.id) is not None:
            await interaction.followup.send("Ya tenés un rol canjeado activo. Esperá a que venza.")
            return
        if not await self._charge(interaction, s.shop_role_cost, "canje rol"):
            return
        role = None
        try:
            role = await guild.create_role(name=name, colour=colour, reason=f"Canje de puntos de {member}")
            # Lo más alto posible (debajo del bot) para que el color se vea.
            top = guild.me.top_role.position - 1
            if top > role.position:
                await role.edit(position=top)
            await member.add_roles(role, reason="Canje de puntos")
        except discord.HTTPException as exc:
            log.warning("Falló el canje de rol de %s (%s).", member, exc)
            if role is not None:
                try:
                    await role.delete(reason="Canje de rol fallido")
                except discord.HTTPException:
                    pass
            await self._refund(member.id, s.shop_role_cost, "canje rol")
            await interaction.followup.send("No pude crear el rol (¿permisos del bot?). Te devolví los puntos.")
            return
        expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=s.shop_role_days)
        await self.store.add(ShopEffect(self._effect_id("role", member.id), "role", member.id, member.id,
                                        expires, {"role_id": role.id}))
        await interaction.followup.send(f"Listo, tenés el rol {role.mention} hasta "
                                        f"{discord.utils.format_dt(expires, 'f')}.")
        await self._post_public(interaction, f"🎨 {member.mention} se compró el rol **{name}**.")

    @app_commands.command(name="canjear-inmunidad",
                          description="Canjeá puntos para que tu próxima trolleada no se anuncie.")
    async def canjear_inmunidad(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        s = self.bot.settings
        if not await self._charge(interaction, s.shop_shield_cost, "canje inmunidad"):
            return
        total = await self.store.add_shield(interaction.user.id)
        await interaction.followup.send(
            f"🛡️ Listo. Tenés **{total}** inmunidad(es): tu próxima alerta troll no sale "
            f"(igual suma al ranking troll, eh).")

    @app_commands.command(name="canjear-apodo", description="Canjeá puntos por ponerle un apodo a alguien.")
    @app_commands.describe(usuario="A quién.", apodo="El apodo (máx 32).")
    async def canjear_apodo(self, interaction: discord.Interaction, usuario: discord.Member, apodo: str) -> None:
        await interaction.response.defer(ephemeral=True)
        s = self.bot.settings
        nick = apodo.strip()[:_MAX_NICK]
        buyer = interaction.user
        if not nick:
            await interaction.followup.send("El apodo no puede estar vacío.")
            return
        if usuario.bot:
            await interaction.followup.send("A los bots no. 🤖")
            return
        if self.store.find("nick", target_id=usuario.id) is not None:
            await interaction.followup.send(f"{usuario.display_name} ya tiene un apodo canjeado activo.")
            return
        if not await self._charge(interaction, s.shop_nick_cost, "canje apodo"):
            return
        original = usuario.nick
        try:
            await usuario.edit(nick=nick, reason=f"Canje de apodo de {buyer}")
        except discord.HTTPException as exc:
            log.warning("No pude cambiarle el apodo a %s (%s).", usuario, exc)
            await self._refund(buyer.id, s.shop_nick_cost, "canje apodo")
            await interaction.followup.send(
                f"No le puedo cambiar el apodo a {usuario.display_name} (tiene un rol más alto que el bot o es "
                f"el dueño del server). Te devolví los puntos.")
            return
        expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=s.shop_nick_hours)
        await self.store.add(ShopEffect(self._effect_id("nick", buyer.id), "nick", buyer.id, usuario.id,
                                        expires, {"nick": nick, "original": original}))
        await interaction.followup.send(f"Listo, {usuario.display_name} ahora es **{nick}**.")
        if isinstance(interaction.channel, discord.abc.Messageable):
            await interaction.channel.send(
                f"🏷️ {buyer.mention} bautizó a {usuario.mention} como **{nick}** hasta "
                f"{discord.utils.format_dt(expires, 't')}. Si no te gusta, `/vetar-apodo`.",
                allowed_mentions=discord.AllowedMentions(users=[usuario]))

    @app_commands.command(name="vetar-apodo", description="Sacate el apodo que te pusieron con puntos.")
    async def vetar_apodo(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        effect = self.store.find("nick", target_id=interaction.user.id)
        if effect is None or interaction.guild is None:
            await interaction.followup.send("No tenés ningún apodo canjeado activo.")
            return
        try:
            await self._revert(interaction.guild, effect)
        except discord.HTTPException as exc:
            log.warning("No pude revertir el apodo de %s (%s).", interaction.user, exc)
            await interaction.followup.send("No pude sacarte el apodo, avisale a un dev.")
            return
        await self.store.remove(effect.id)
        half = self.bot.settings.shop_nick_cost // 2
        if half:
            await self._refund(effect.user_id, half, "apodo vetado")
        await interaction.followup.send("Listo, apodo vetado.")
        await self._post_public(
            interaction, f"🙅 {interaction.user.mention} vetó su apodo. A <@{effect.user_id}> se le devolvió la mitad.")

    @app_commands.command(name="canjear-mensaje", description="Canjeá puntos por un mensaje fijado en general.")
    @app_commands.describe(texto=f"Lo que querés que diga (máx {_MAX_PIN_TEXT} caracteres, sin menciones).")
    async def canjear_mensaje(self, interaction: discord.Interaction, texto: str) -> None:
        await interaction.response.defer(ephemeral=True)
        s = self.bot.settings
        text = texto.strip()
        if not text or len(text) > _MAX_PIN_TEXT:
            await interaction.followup.send(f"El mensaje tiene que tener entre 1 y {_MAX_PIN_TEXT} caracteres.")
            return
        channel = self.bot.get_channel(s.general_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("No encuentro el canal general, avisale a un dev.")
            return
        if not await self._charge(interaction, s.shop_pin_cost, "canje mensaje"):
            return
        member = interaction.user
        embed = discord.Embed(description=text, color=discord.Color.gold())
        embed.set_author(name=f"📌 Mensaje de {member.display_name}", icon_url=member.display_avatar.url)
        embed.set_footer(text="Canjeado con puntos · /tienda")
        try:
            msg = await channel.send(embed=embed, allowed_mentions=_NO_MENTIONS)
        except discord.HTTPException as exc:
            log.warning("No pude publicar el mensaje canjeado de %s (%s).", member, exc)
            await self._refund(member.id, s.shop_pin_cost, "canje mensaje")
            await interaction.followup.send("No pude publicarlo. Te devolví los puntos.")
            return
        try:
            await msg.pin(reason=f"Canje de puntos de {member}")
        except discord.HTTPException as exc:
            log.warning("Publiqué el mensaje de %s pero no lo pude fijar (%s).", member, exc)
            await interaction.followup.send(f"Lo publiqué en {channel.mention}, pero no lo pude fijar "
                                            f"(¿permisos del bot?).")
            return
        expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=s.shop_pin_hours)
        await self.store.add(ShopEffect(self._effect_id("pin", member.id), "pin", member.id, member.id, expires,
                                        {"channel_id": channel.id, "message_id": msg.id}))
        await interaction.followup.send(f"Listo, fijado en {channel.mention} hasta "
                                        f"{discord.utils.format_dt(expires, 'f')}.")
