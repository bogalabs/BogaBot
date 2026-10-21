"""Cog de ayuda: /help y /ayuda [sección].

Sin sección muestra un índice con un desplegable para saltar a cada una;
con sección (`/ayuda puntos`, `/ayuda admin`...) va directo. Las secciones
dependen de los roles de quien pregunta y de los módulos cargados. Todo es
efímero (solo lo ve quien lo pidió).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import discord
from discord import app_commands
from discord.ext import commands

from bogabot.modules.points.shop import tienda_embed

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

_NO_MENTIONS = discord.AllowedMentions.none()


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    summary: str


SECTIONS = (
    Section("lol", "🎮 Ranking LoL", "vincular tu cuenta y el ranking del grupo"),
    Section("trolls", "🤡 Trolls y ⭐ carreadas", "quién la trolleó, quién carreó y por qué"),
    Section("puntos", "💰 Puntos", "cómo se ganan: voz, partidas, /daily y misiones"),
    Section("tienda", "🛒 Tienda", "en qué gastarlos y cómo se usa cada cosa"),
    Section("apuestas", "🎲 Apuestas y ruleta", "predicciones, duelos, recompensas, semanales y /ruleta"),
    Section("sonidos", "🔊 Sonidos", "sonidos del bot y el soundboard"),
    Section("admin", "🛠️ Administración", "comandos del rol dev"),
)
_BY_KEY = {s.key: s for s in SECTIONS}
_CHOICES = [app_commands.Choice(name=sec.title, value=sec.key) for sec in SECTIONS]


class SectionSelect(discord.ui.Select):
    def __init__(self, cog: "HelpCog", member: discord.Member | None, current: str | None) -> None:
        options = [discord.SelectOption(label=s.title, value=s.key, description=s.summary[:100],
                                        default=s.key == current)
                   for s in cog.visible_sections(member)]
        super().__init__(placeholder="📖 Elegí una sección", options=options)
        self.cog, self.member = cog, member

    async def callback(self, interaction: discord.Interaction) -> None:
        key = self.values[0]
        await interaction.response.edit_message(embed=self.cog.section_embed(key, self.member, interaction.user.id),
                                                view=self.cog.nav_view(self.member, key))


class HelpCog(commands.Cog):
    def __init__(self, bot: "BogaBot") -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # Visibilidad
    # ------------------------------------------------------------------ #
    @staticmethod
    def _has_role(member: discord.Member | None, role_id: int | None) -> bool:
        return member is not None and role_id is not None and any(r.id == role_id for r in member.roles)

    def _loaded(self, cog: str) -> bool:
        return self.bot.get_cog(cog) is not None

    def visible_sections(self, member: discord.Member | None) -> list[Section]:
        s = self.bot.settings
        is_dev = self._has_role(member, s.dev_role_id)
        is_player = is_dev or self._has_role(member, s.player_role_id)
        visible = {
            "lol": is_player, "trolls": is_player, "puntos": self._loaded("PointsCog"),
            "tienda": self._loaded("ShopCog"),
            "apuestas": any(self._loaded(c) for c in ("PredictionsCog", "BetsCog", "RouletteCog")),
            "sonidos": self._loaded("SoundsCog"), "admin": is_dev,
        }
        return [sec for sec in SECTIONS if visible[sec.key]]

    def nav_view(self, member: discord.Member | None, current: str | None) -> discord.ui.View:
        view = discord.ui.View(timeout=600)
        view.add_item(SectionSelect(self, member, current))
        return view

    # ------------------------------------------------------------------ #
    # Contenido
    # ------------------------------------------------------------------ #
    def _embed(self, key: str, lines: list[str]) -> discord.Embed:
        sec = _BY_KEY[key]
        embed = discord.Embed(title=sec.title, description="\n".join(lines)[:4096], color=discord.Color.blurple())
        embed.set_footer(text="🔒 = te responde solo a vos · /ayuda para ver todas las secciones")
        return embed

    def index_embed(self, member: discord.Member | None) -> discord.Embed:
        s = self.bot.settings
        lines = [f"**{sec.title}** — {sec.summary}\n`/ayuda {sec.key}`" for sec in self.visible_sections(member)]
        embed = discord.Embed(title="📖 Ayuda de BogaBot", color=discord.Color.blurple(),
                              description="Elegí una sección en el menú de abajo o escribila directo.\n\n"
                                          + "\n".join(lines))
        if s.bot_channel_id is not None:
            embed.add_field(name="Dónde", value=f"Los comandos se usan en <#{s.bot_channel_id}>.", inline=False)
        if self._loaded("PointsCog") and not self._has_role(member, s.points_role_id):
            embed.add_field(name="💰 Puntos", value=f"Para juntar y gastar puntos necesitás el rol "
                                                    f"<@&{s.points_role_id}>.", inline=False)
        return embed

    def section_embed(self, key: str, member: discord.Member | None, user_id: int) -> discord.Embed:
        if key not in {sec.key for sec in self.visible_sections(member)}:
            return discord.Embed(title="🔒 Sección no disponible", color=discord.Color.dark_grey(),
                                 description="No tenés acceso a esa sección (o ese módulo está apagado). "
                                             "Mirá `/ayuda`.")
        if key == "tienda":
            return tienda_embed(self.bot, user_id)
        builders: dict[str, Callable[[], list[str]]] = {
            "lol": self._lol, "trolls": self._trolls, "puntos": self._puntos, "apuestas": self._apuestas,
            "sonidos": self._sonidos, "admin": self._admin,
        }
        return self._embed(key, builders[key]())

    def _lol(self) -> list[str]:
        return [
            "`/link <Nombre#TAG>` 🔒 — vinculá tu cuenta de Riot. Sin esto no entrás al ranking ni a las apuestas "
            "por partida.",
            "`/unlink` 🔒 — desvinculá tu cuenta.",
            "`/ranking [Hoy|Semana]` — el ranking del grupo.",
            "",
            "*Solo cuentan las partidas jugadas con al menos otro vinculado. Las partidas terminadas se avisan "
            "solas en el canal de partidas.*",
        ]

    def _trolls(self) -> list[str]:
        return [
            "**🤡 Trolls**",
            "`/trolls [Semana|Semana pasada|Mes|Histórico]` — ranking troll.",
            "`/troll-analizar [usuario] [partida]` — los cargos de una partida y por qué.",
            "`/trolls-reglas` 🔒 — qué se detecta y cuánto suma cada cosa.",
            "",
            "**⭐ Carreadas**",
            "`/carries [período]` — ranking de carreadas.",
            "`/carry-analizar [usuario] [partida]` — las jugadas de carry de una partida.",
            "`/carries-reglas` 🔒 — qué cuenta como carreada.",
            "",
            "*El ranking es por **índice** (puntos por partida): jugar más no suma solo. Las trolleadas se avisan "
            "en el canal de trolls y solo los papelones van a general. Carreada = impacto en tu equipo, no un KDA "
            "lindo en una partida fácil.*",
        ]

    def _puntos(self) -> list[str]:
        s = self.bot.settings
        lines = [
            f"Juntan puntos los que tienen el rol <@&{s.points_role_id}>.",
            "",
            "**Cómo se ganan**",
            f"🎙️ **Voz:** {s.points_voice_amount} cada {s.points_voice_interval_minutes} min en un canal con "
            f"alguien más (no cuenta ensordecido ni AFK), hasta {s.points_voice_daily_cap} por día.",
            f"🎮 **Partidas:** {s.points_lol_game} por partida con el grupo, +{s.points_lol_win} si ganás.",
            f"📅 **`/daily`** 🔒 — {s.daily_base} por día, +{s.daily_step} por cada día seguido (hasta "
            f"{s.daily_max}). Si salteás un día, la racha vuelve a 1.",
        ]
        if self._loaded("MissionsCog"):
            lines.append(f"🗺️ **`/misiones`** 🔒 — {s.missions_per_week} misiones por semana, "
                         f"{s.missions_reward} pts cada una. Se renuevan los lunes.")
        if self._loaded("RouletteCog"):
            lines.append(f"🎰 **Ruleta:** {s.roulette_bonus} pts si jugás lo que te tocó.")
        lines.append("🎲 **Apostando:** `/ayuda apuestas`.")
        lines += [
            "",
            "**Ver**",
            "`/puntos [usuario]` 🔒 — tu saldo (o el de otro).",
            "`/puntos-top` — ranking de puntos.",
            "`/tienda` 🔒 — en qué gastarlos.",
        ]
        return lines

    def _apuestas(self) -> list[str]:
        s = self.bot.settings
        lines = [f"Apuestas de {s.prediction_min_bet} a {s.prediction_max_bet} pts. El pozo se reparte entre los "
                 "que aciertan, según cuánto puso cada uno. Nadie puede apostar en su propia contra.", ""]
        if self._loaded("PredictionsCog"):
            channel_id = s.prediction_channel_id or s.match_notify_channel_id
            where = f" en <#{channel_id}>" if channel_id else ""
            lines.append(f"**🔮 Predicciones** — cuando 2+ del grupo arrancan partida, sale un mensaje{where}: "
                         f"¿ganan o pierden? Se apuesta hasta el minuto {s.prediction_close_minutes}.")
            if s.props_enabled:
                lines.append("**🎯 Sobre la partida** — abajo de la predicción, un menú: quién carrea, si cada uno "
                             "trollea, muertes, duración y primera sangre.")
        if self._loaded("BetsCog"):
            lines += [
                f"**⚔️ `/desafio <usuario> <puntos> <modo>`** — duelo 1v1 (hasta {s.duel_max_bet} pts cada uno): "
                f"moneda, o mejor KDA / más carreada en la próxima partida juntos. El otro tiene "
                f"{s.duel_accept_minutes} min para aceptar.",
                "**💰 `/recompensa <usuario> <puntos> <tipo>`** — apostá a que alguien trollea (o carrea) la "
                "próxima; los demás ponen al Sí o al No.",
            ]
            if s.weekly_bets_enabled:
                lines.append(f"**📅 Semanales** — los lunes: ¿quién es el troll y el carry de la semana? Se "
                             f"apuesta {s.weekly_bet_close_days} días.")
        if self._loaded("RouletteCog"):
            lines += [
                "",
                "**🎰 `/ruleta [modo]`** — desde un canal de voz: elegís quiénes juegan y a cada uno le toca rol, "
                "campeón y build. Modo *fuera de tu zona*: te manda a lo que menos jugás.",
                f"🔄 Re-tirar el tuyo: {s.roulette_reroll_cost} pts (sube cada vez) · 😈 Castigar a otro: "
                f"{s.roulette_punish_cost} pts (le toca lo peor) · 🏆 Apostá si ganan.",
                f"💰 Si jugás el campeón y la posición que te tocaron: +{s.roulette_bonus} (el doble si te "
                "castigaron).",
            ]
        return lines

    def _sonidos(self) -> list[str]:
        s = self.bot.settings
        lines = [
            "`/sonido [nombre]` — el bot entra a tu canal de voz y tira un sonido.",
            "`/sonidos` 🔒 — lista de sonidos.",
        ]
        if self._loaded("PointsCog"):
            lines += [
                f"`/canjear-sonido <nombre> <archivo>` 🔒 — tu sonido en el soundboard por {s.sound_redeem_days} "
                f"días ({s.sound_redeem_cost} pts).",
                "`/sonido-del <nombre>` 🔒 — borrá un sonido que canjeaste.",
            ]
        lines += ["", "*Cuando termina una partida con trolleada o carreada, el bot lo dice en el canal de voz de "
                      "los que jugaron.*"]
        if s.sounds_game_start_voice and self._loaded("PredictionsCog"):
            lines.append("*Cuando arranca una partida con predicción, avisa en voz para que apuesten.*")
        return lines

    def _admin(self) -> list[str]:
        s = self.bot.settings
        where = f" (en <#{s.admin_channel_id}>)" if s.admin_channel_id else ""
        return [
            f"`/link-admin <usuario> <Nombre#TAG>`{where} — vinculá la cuenta de otro.",
            f"`/unlink-admin <usuario>`{where} — desvinculá la cuenta de otro.",
            f"`/ingest-now`{where} — forzá una ingesta de partidas.",
            f"`/trolls-recalcular [desde_cero]`{where} — recalculá trolls y carreadas.",
            f"`/trolls-reiniciar` / `/carries-reiniciar`{where} — el ranking arranca de cero.",
            f"`/riot-key [key]`{where} — cambiá o mirá la API key de Riot.",
            "`/puntos-dar <usuario> <cantidad>` — sumá o restá puntos.",
            "`/sonido-forzar [nombre] [canal]` — tirá un sonido en un canal de voz.",
            "`/sonido-add <nombre> <archivo>` — subí un sonido permanente.",
            "`/sonido-del <nombre>` — borrá cualquier sonido.",
            "",
            "*Los devs pueden usar los comandos en cualquier canal.*",
        ]

    # ------------------------------------------------------------------ #
    # Slash commands
    # ------------------------------------------------------------------ #
    async def _send_help(self, interaction: discord.Interaction, seccion: str | None) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        embed = self.section_embed(seccion, member, interaction.user.id) if seccion else self.index_embed(member)
        await interaction.response.send_message(embed=embed, view=self.nav_view(member, seccion), ephemeral=True,
                                                allowed_mentions=_NO_MENTIONS)

    @app_commands.command(name="help", description="Los comandos del bot, por sección.")
    @app_commands.describe(seccion="Ir directo a una sección (vacío = índice).")
    @app_commands.choices(seccion=_CHOICES)
    async def help_command(self, interaction: discord.Interaction,
                           seccion: app_commands.Choice[str] | None = None) -> None:
        await self._send_help(interaction, seccion.value if seccion else None)

    @app_commands.command(name="ayuda", description="Los comandos del bot, por sección.")
    @app_commands.describe(seccion="Ir directo a una sección (vacío = índice).")
    @app_commands.choices(seccion=_CHOICES)
    async def ayuda_command(self, interaction: discord.Interaction,
                            seccion: app_commands.Choice[str] | None = None) -> None:
        await self._send_help(interaction, seccion.value if seccion else None)
