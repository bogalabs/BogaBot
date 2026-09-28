"""Gestión de la RIOT_API_KEY en caliente: `/riot-key` + recordatorio de vencimiento.

Producción usa una Personal API Key, que no vence (RIOT_KEY_TTL_HOURS=0 apaga
el recordatorio). Esto sirve para rotarla, o para la dev key de Riot, que
vence cada 24h: en vez de editar el .env y reiniciar el bot, un dev la pega con `/riot-key <key>`: se valida contra Riot, se aplica
al `RiotClient` al instante y se guarda en RIOT_KEY_FILE para que sobreviva
un reinicio. Un loop avisa en ADMIN_CHANNEL_ID (etiquetando al rol dev)
cuando falta poco para que venza y cuando ya venció.

Precedencia al arrancar (ver `RiotKeyStore.resolve`): la key guardada por
`/riot-key` gana sobre la del .env, **salvo** que alguien haya cambiado
RIOT_API_KEY en el .env después; en ese caso manda el .env (es la acción más
reciente) y se descarta la guardada.

Formato de RIOT_KEY_FILE:
    {"api_key": "RGAPI-...", "env_key": "<RIOT_API_KEY del .env al guardar>",
     "updated_at": "2026-09-27T18:00:00+00:00"}
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bogabot.riot.client import RiotApiError, RiotUnavailableError

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

log = logging.getLogger(__name__)

# Cada cuánto se revisa si toca avisar del vencimiento.
_CHECK_INTERVAL_MINUTES = 10


@dataclass(frozen=True)
class RiotKeyState:
    api_key: str
    env_key: str
    updated_at: dt.datetime

    def to_dict(self) -> dict:
        return {"api_key": self.api_key, "env_key": self.env_key,
                "updated_at": self.updated_at.isoformat()}

    @classmethod
    def from_dict(cls, data: dict) -> "RiotKeyState":
        return cls(api_key=data["api_key"], env_key=data["env_key"],
                   updated_at=dt.datetime.fromisoformat(data["updated_at"]))


class RiotKeyStore:
    """Persiste la key vigente y desde cuándo, en un JSON local."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def resolve(self, env_key: str) -> RiotKeyState:
        """Devuelve la key a usar al arrancar. Si el .env cambió respecto de
        lo guardado (o no hay nada guardado), la del .env pasa a ser la
        vigente desde ahora. Reiniciar con la misma key conserva la fecha
        original, para que el recordatorio no se corra."""
        stored: RiotKeyState | None = None
        if self._path.exists():
            try:
                stored = RiotKeyState.from_dict(json.loads(self._path.read_text(encoding="utf-8")))
            except (ValueError, KeyError):
                log.warning("RIOT_KEY_FILE (%s) ilegible; uso la RIOT_API_KEY del .env.", self._path)
        if stored is not None and stored.env_key == env_key:
            return stored
        state = RiotKeyState(api_key=env_key, env_key=env_key,
                             updated_at=dt.datetime.now(dt.timezone.utc))
        self._write(json.dumps(state.to_dict(), indent=1))
        return state

    async def save(self, state: RiotKeyState) -> None:
        await asyncio.to_thread(self._write, json.dumps(state.to_dict(), indent=1))

    def _write(self, data: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self._path)


def _format_delta(delta: dt.timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


class RiotKeyCog(commands.Cog):
    def __init__(self, bot: "BogaBot", store: RiotKeyStore, state: RiotKeyState) -> None:
        self.bot = bot
        self._store = store
        self._state = state
        # Qué avisos ya salieron para la key actual ("warn", "expired").
        self._notified: set[str] = set()

    async def cog_load(self) -> None:
        if self.bot.settings.riot_key_ttl_hours > 0:
            self.expiry_job.start()
            log.info("Recordatorio de RIOT_API_KEY activo (vence a las %dh, aviso %d min antes).",
                     self.bot.settings.riot_key_ttl_hours, self.bot.settings.riot_key_warn_minutes)

    async def cog_unload(self) -> None:
        if self.expiry_job.is_running():
            self.expiry_job.cancel()

    def _expires_at(self) -> dt.datetime | None:
        ttl = self.bot.settings.riot_key_ttl_hours
        if ttl <= 0:
            return None
        return self._state.updated_at + dt.timedelta(hours=ttl)

    def _is_dev(self, member: discord.Member) -> bool:
        role_id = self.bot.settings.dev_role_id
        return role_id is not None and any(role.id == role_id for role in member.roles)

    async def _check_admin(self, interaction: discord.Interaction) -> bool:
        """Mismo gate que los comandos de admin de LoL: canal + rol dev."""
        admin_channel_id = self.bot.settings.admin_channel_id
        if admin_channel_id and interaction.channel_id != admin_channel_id:
            await interaction.followup.send(f"Este comando solo se puede usar en <#{admin_channel_id}>.")
            return False
        if not isinstance(interaction.user, discord.Member) or not self._is_dev(interaction.user):
            await interaction.followup.send("No tenés permiso para usar este comando.")
            return False
        return True

    def _status_text(self) -> str:
        expires_at = self._expires_at()
        loaded = discord.utils.format_dt(self._state.updated_at, "R")
        if expires_at is None:
            return f"🔑 Key cargada {loaded}. Configurada como sin vencimiento (`RIOT_KEY_TTL_HOURS=0`)."
        when = discord.utils.format_dt(expires_at, "R")
        if dt.datetime.now(dt.timezone.utc) >= expires_at:
            return f"⛔ La key cargada {loaded} ya venció ({when}). Cargá una nueva con `/riot-key <key>`."
        return f"🔑 Key cargada {loaded}; vence {when} ({discord.utils.format_dt(expires_at, 't')})."

    @app_commands.command(
        name="riot-key",
        description="Cargá una RIOT_API_KEY nueva sin reiniciar, o mirá cuándo vence (solo rol dev).",
    )
    @app_commands.describe(key="La key nueva (RGAPI-...). Vacío = ver el estado de la actual.")
    async def riot_key(self, interaction: discord.Interaction, key: str | None = None) -> None:
        # Ephemeral: ni la key ni la respuesta quedan visibles para el resto.
        await interaction.response.defer(ephemeral=True)
        if not await self._check_admin(interaction):
            return

        if key is None:
            await interaction.followup.send(self._status_text())
            return

        key = key.strip()
        try:
            valid = await self.bot.riot.validate_key(key)
        except RiotUnavailableError:
            await interaction.followup.send("Riot no responde ahora; no pude validar la key. Probá en un rato.")
            return
        except RiotApiError as exc:
            log.warning("Validando RIOT_API_KEY nueva: %s", exc)
            await interaction.followup.send(f"Riot respondió algo inesperado validando la key ({exc.status}).")
            return
        if not valid:
            await interaction.followup.send("❌ Riot rechazó esa key (401/403). Revisá que la copiaste entera.")
            return

        self._state = RiotKeyState(api_key=key, env_key=self.bot.settings.riot_api_key,
                                   updated_at=dt.datetime.now(dt.timezone.utc))
        await self._store.save(self._state)
        self.bot.riot.set_api_key(key)
        self.bot.ingest.reset_auth_alert()
        self._notified.clear()
        log.info("RIOT_API_KEY actualizada por %s vía /riot-key.", interaction.user)
        await interaction.followup.send(f"✅ Key nueva cargada y en uso.\n{self._status_text()}")

    @tasks.loop(minutes=_CHECK_INTERVAL_MINUTES)
    async def expiry_job(self) -> None:
        expires_at = self._expires_at()
        if expires_at is None:
            return
        now = dt.datetime.now(dt.timezone.utc)
        warn_at = expires_at - dt.timedelta(minutes=self.bot.settings.riot_key_warn_minutes)

        if now >= expires_at and "expired" not in self._notified:
            self._notified.update({"warn", "expired"})
            await self._notify(
                "⛔ La RIOT_API_KEY venció. Generá una nueva en https://developer.riotgames.com/ "
                "y cargala con `/riot-key <key>`."
            )
        elif warn_at <= now < expires_at and "warn" not in self._notified:
            self._notified.add("warn")
            await self._notify(
                f"⏰ La RIOT_API_KEY vence en ~{_format_delta(expires_at - now)} "
                f"({discord.utils.format_dt(expires_at, 't')}). Renovala en "
                "https://developer.riotgames.com/ y cargala con `/riot-key <key>`."
            )

    @expiry_job.before_loop
    async def _before_expiry_job(self) -> None:
        await self.bot.wait_until_ready()

    async def _notify(self, text: str) -> None:
        """Postea en ADMIN_CHANNEL_ID etiquetando al rol dev. Sin canal de
        admin (o si falla), cae al log -> canal de logs."""
        s = self.bot.settings
        if s.admin_channel_id is not None:
            try:
                channel = self.bot.get_channel(s.admin_channel_id) or await self.bot.fetch_channel(s.admin_channel_id)
                if isinstance(channel, discord.abc.Messageable):
                    mention = f"<@&{s.dev_role_id}> " if s.dev_role_id else ""
                    await channel.send(mention + text,
                                       allowed_mentions=discord.AllowedMentions(roles=True))
                    return
            except discord.HTTPException:
                log.exception("No pude avisar el vencimiento de la key en ADMIN_CHANNEL_ID.")
        log.warning(text)
