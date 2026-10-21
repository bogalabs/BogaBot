"""Módulo "sonidos": el bot entra a un canal de voz, tira un audio corto y se va.

Formas de disparo:
  - Manual: /sonido [nombre]  (en el canal de voz de quien lo pide).
  - Forzado (solo devs): /sonido-forzar [nombre] [canal].
  - Por evento: listener de IngestService. Cuando termina (recién) una
    partida del grupo con trolleada o carreada (lo mismo que se avisa en
    #log-partidas), el bot entra al canal de voz donde están los que la
    jugaron y dice una frase sobre quién fue (TTS, SOUNDS_EVENT_VOICE; ver
    voice_lines.py). Antes de la frase puede sonar SOUNDS_EVENT_PAPELON /
    SOUNDS_EVENT_VICTORY si están configurados. Nada de azar: solo suena
    donde está la gente de esa partida.
  - Random: random_job corre cada SOUNDS_CHECK_INTERVAL_MINUTES; dentro del
    horario activo, si hay canales con gente y pasó el cooldown, tira un dado
    (SOUNDS_CHANCE) y, si sale, elige un canal ocupado y un audio al azar.
    Default SOUNDS_CHANCE=0 (apagado): molestaba que entrara sin que nadie
    lo pidiera.

Los audios viven en SOUNDS_DIR (fuera de git). Dos formas de cargarlos:
  - /sonido-add (solo devs): permanente.
  - /canjear-sonido (rol POINTS_ROLE_ID): cuesta SOUND_REDEEM_COST puntos, dura
    como mucho SOUND_REDEEM_MAX_SECONDS y vence a los SOUND_REDEEM_DAYS días.
    Además de guardarse acá, se sube al **soundboard del server**, así cada
    uno lo tira cuando quiere desde Discord (sin que el bot entre a nada).
    Dueño, vencimiento e id del soundboard quedan en SOUNDS_DIR/_meta.json;
    expire_job borra los vencidos (archivo + soundboard). Un sonido sin
    entrada en _meta.json es permanente.

El cog no toca storage ni Riot: solo settings + filesystem + voz (+ el
PointsStore del bot para cobrar el canje).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bogabot.core.models import MatchRecord, TrollLevel
from bogabot.core.timeutils import get_tz
from bogabot.modules.sounds.tts import synthesize
from bogabot.modules.sounds.voice_lines import (CARRY, build_line, build_start_line, pick_spoken_verdict,
                                                spoken_name)

if TYPE_CHECKING:
    from bogabot.bot import BogaBot

log = logging.getLogger(__name__)

_ALLOWED_SUFFIXES = (".mp3", ".ogg", ".wav")
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024
# Corte de seguridad por si alguien sube un audio largo: no queremos al bot
# 3 minutos colgado en un canal.
_MAX_PLAY_SECONDS = 20
_META_FILE = "_meta.json"
# Límites del soundboard de Discord: mp3/ogg, hasta 5.2 s y 512 KB.
_SOUNDBOARD_MAX_SECONDS = 5.2
_SOUNDBOARD_MAX_BYTES = 512 * 1024
_SOUNDBOARD_SUFFIXES = (".mp3", ".ogg")
# "Victoria épica" para el sonido por evento: algún ganador del grupo con
# muchas kills y buen KDA.
_EPIC_MIN_KILLS = 10
_EPIC_MIN_KDA = 7.0


def pick_event_sound(records: list[MatchRecord], levels: list[TrollLevel],
                     papelon_sound: str, victory_sound: str) -> str | None:
    """Qué sonido corresponde a una partida recién terminada (registros de
    los vinculados que la jugaron + su nivel troll), o None si ninguno.
    El papelón tiene prioridad: es lo más gracioso."""
    if papelon_sound and any(level >= TrollLevel.PAPELON for level in levels):
        return papelon_sound
    if victory_sound and any(r.win and r.kills >= _EPIC_MIN_KILLS and r.kda >= _EPIC_MIN_KDA
                             for r in records):
        return victory_sound
    return None


class SoundsCog(commands.Cog):
    def __init__(self, bot: "BogaBot") -> None:
        self.bot = bot
        self.sounds_dir = Path(bot.settings.sounds_dir)
        self.sounds_dir.mkdir(parents=True, exist_ok=True)
        # Una sola reproducción a la vez en todo el bot.
        self._play_lock = asyncio.Lock()
        self._last_random_play: dt.datetime | None = None
        # Sonidos de evento en curso: corren aparte para no frenar la ingesta
        # (y los avisos de texto) mientras el bot habla.
        self._event_tasks: set[asyncio.Task] = set()
        # Sonidos canjeados: nombre -> {"owner_id": int, "expires_at": iso}.
        meta_path = self.sounds_dir / _META_FILE
        self._meta: dict[str, dict] = (
            json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {})

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _sound_files(self) -> list[Path]:
        return sorted(p for p in self.sounds_dir.iterdir()
                      if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in _ALLOWED_SUFFIXES)

    def _find_sound(self, name: str) -> Path | None:
        name = name.lower().strip()
        for p in self._sound_files():
            if p.stem.lower() == name:
                return p
        return None

    def _has_role(self, member: discord.Member, role_id: int | None) -> bool:
        return role_id is not None and any(r.id == role_id for r in member.roles)

    def _is_dev(self, member: discord.Member) -> bool:
        return self._has_role(member, self.bot.settings.dev_role_id)

    async def _save_meta(self) -> None:
        path = self.sounds_dir / _META_FILE
        data = json.dumps(self._meta, indent=1)
        await asyncio.to_thread(path.write_text, data, "utf-8")

    def _expires_at(self, name: str) -> dt.datetime | None:
        entry = self._meta.get(name)
        return dt.datetime.fromisoformat(entry["expires_at"]) if entry else None

    def _active_redeems(self, owner_id: int) -> list[str]:
        return [n for n, e in self._meta.items() if e.get("owner_id") == owner_id]

    @staticmethod
    def _clean_name(raw: str) -> str:
        return "".join(c for c in raw.lower().strip() if c.isalnum() or c in "-_")

    @staticmethod
    async def _duration_seconds(path: Path) -> float | None:
        """Duración del audio según ffprobe, o None si no la pudo leer."""
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            return float(out.strip())
        except (asyncio.TimeoutError, ValueError):
            if proc.returncode is None:
                proc.kill()
            return None

    def _validate_upload(self, name: str, archivo: discord.Attachment) -> str | None:
        """Chequeos comunes a /sonido-add y /canjear-sonido. Devuelve el
        mensaje de error o None si está todo bien."""
        if not name:
            return "Nombre inválido: usá letras, números, `-` o `_`."
        if Path(archivo.filename).suffix.lower() not in _ALLOWED_SUFFIXES:
            return f"Formato no soportado. Usá {', '.join(_ALLOWED_SUFFIXES)}."
        if archivo.size > _MAX_UPLOAD_BYTES:
            return "Muy pesado: máximo 2 MB."
        if self._find_sound(name) is not None:
            return f"Ya existe `{name}`. Elegí otro nombre."
        return None

    def _occupied_voice_channels(self, guild: discord.Guild) -> list[discord.VoiceChannel]:
        """Canales de voz con al menos un humano (excluye AFK y bots)."""
        result = []
        for ch in guild.voice_channels:
            if guild.afk_channel and ch.id == guild.afk_channel.id:
                continue
            if any(not m.bot for m in ch.members):
                result.append(ch)
        return result

    def _in_active_hours(self, now: dt.datetime) -> bool:
        start, end = self.bot.settings.sounds_active_from, self.bot.settings.sounds_active_to
        if start == end:
            return True
        if start < end:
            return start <= now.hour < end
        # Rango que cruza medianoche, ej. 20 -> 3.
        return now.hour >= start or now.hour < end

    async def _play_in(self, channel: discord.VoiceChannel, *sounds: Path) -> None:
        """Entra, reproduce `sounds` en orden, se va. Serializado por `_play_lock`."""
        async with self._play_lock:
            guild = channel.guild
            if guild.voice_client is not None:
                # Quedó una conexión colgada de un intento anterior.
                await guild.voice_client.disconnect(force=True)

            vc = await channel.connect(timeout=15, self_deaf=True)
            try:
                for sound in sounds:
                    done = asyncio.Event()

                    def _after(err: Exception | None, sound: Path = sound, done: asyncio.Event = done) -> None:
                        if err:
                            log.warning("Error reproduciendo %s: %s", sound.name, err)
                        self.bot.loop.call_soon_threadsafe(done.set)

                    vc.play(discord.FFmpegPCMAudio(str(sound)), after=_after)
                    try:
                        await asyncio.wait_for(done.wait(), timeout=_MAX_PLAY_SECONDS)
                    except asyncio.TimeoutError:
                        log.warning("Sonido %s superó %ss, lo corto.", sound.name, _MAX_PLAY_SECONDS)
                        vc.stop()
            finally:
                await vc.disconnect(force=True)
            log.info("Sonido %s reproducido en #%s.", ", ".join(f"'{p.stem}'" for p in sounds), channel.name)

    # ------------------------------------------------------------------ #
    # Soundboard del server (canjes)
    # ------------------------------------------------------------------ #
    @staticmethod
    async def _soundboard_bytes(path: Path) -> bytes | None:
        """El audio listo para el soundboard: tal cual si ya es mp3/ogg y
        entra en 512 KB; si no, convertido a mp3 con ffmpeg. None si falla."""
        if path.suffix.lower() in _SOUNDBOARD_SUFFIXES and path.stat().st_size <= _SOUNDBOARD_MAX_BYTES:
            return await asyncio.to_thread(path.read_bytes)
        out = path.with_name(path.name + ".sb.mp3")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-v", "error", "-i", str(path), "-t", str(_SOUNDBOARD_MAX_SECONDS),
            "-ac", "2", "-b:a", "128k", str(out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
            if proc.returncode != 0 or not out.exists():
                return None
            data = await asyncio.to_thread(out.read_bytes)
            return data if len(data) <= _SOUNDBOARD_MAX_BYTES else None
        except asyncio.TimeoutError:
            proc.kill()
            return None
        finally:
            out.unlink(missing_ok=True)

    async def _delete_from_soundboard(self, name: str) -> None:
        """Saca del soundboard el sonido canjeado `name` (si se había subido)."""
        sound_id = self._meta.get(name, {}).get("soundboard_id")
        if not sound_id:
            return
        for guild in self._guilds():
            sound = guild.get_soundboard_sound(int(sound_id))
            if sound is None:
                continue
            try:
                await sound.delete(reason=f"Canje de sonido '{name}' vencido o borrado")
                log.info("Sonido '%s' sacado del soundboard.", name)
            except discord.HTTPException as exc:
                log.warning("No pude sacar '%s' del soundboard (%s).", name, exc)

    def _guilds(self) -> list[discord.Guild]:
        gid = self.bot.settings.guild_id
        return [g for g in self.bot.guilds if not gid or g.id == gid]

    # ------------------------------------------------------------------ #
    # Sonidos por evento (listener de IngestService)
    # ------------------------------------------------------------------ #
    def _voice_channel_of(self, user_ids: set[int]) -> discord.VoiceChannel | None:
        """El canal de voz donde hay más jugadores de `user_ids` (None si no
        hay ninguno conectado)."""
        best, best_count = None, 0
        for guild in self._guilds():
            for ch in self._occupied_voice_channels(guild):
                count = sum(1 for m in ch.members if m.id in user_ids)
                if count > best_count:
                    best, best_count = ch, count
        return best

    async def _on_new_matches(self, records: list[MatchRecord]) -> None:
        s = self.bot.settings
        if not (s.sounds_event_voice or s.sounds_event_papelon or s.sounds_event_victory):
            return
        now = dt.datetime.now(dt.timezone.utc)
        max_age = dt.timedelta(minutes=s.match_notify_max_age_minutes)
        by_match: dict[str, list[MatchRecord]] = {}
        for r in records:
            by_match.setdefault(r.match_id, []).append(r)
        for match_id, match_records in by_match.items():
            if now - match_records[0].game_end > max_age:
                continue  # partida vieja (ingesta forzada, bot caído): no suena
            channel = self._voice_channel_of({r.discord_id for r in match_records})
            if channel is None:
                continue  # nadie de la partida está en voz
            task = asyncio.create_task(self._play_event(channel, match_id, match_records))
            self._event_tasks.add(task)
            task.add_done_callback(self._event_done)

    def announce_game_start(self, user_ids: list[int], queue: str, bets_channel: str | None) -> None:
        """Arrancó una partida del grupo con predicción abierta: si alguno de
        los que juegan está en voz, el bot entra y avisa para que apuesten.
        Corre en una tarea aparte (no frena a quien llama)."""
        if not self.bot.settings.sounds_game_start_voice:
            return
        channel = self._voice_channel_of(set(user_ids))
        if channel is None:
            return
        task = asyncio.create_task(self._play_start(channel, user_ids, queue, bets_channel))
        self._event_tasks.add(task)
        task.add_done_callback(self._event_done)

    async def _play_start(self, channel: discord.VoiceChannel, user_ids: list[int], queue: str,
                          bets_channel: str | None) -> None:
        s = self.bot.settings
        names = []
        for uid in user_ids:
            member = channel.guild.get_member(uid)
            names.append(spoken_name(member.display_name if member else ""))
        text = build_start_line(names, queue, spoken_name(bets_channel) if bets_channel else None)
        voice_file = await synthesize(text, self.sounds_dir / f".tts-start-{channel.id}.mp3",
                                      s.sounds_tts_lang, s.sounds_tts_tld)
        if voice_file is None:
            return
        try:
            await self._play_in(channel, voice_file)
            log.info("Aviso de partida en #%s: %s", channel.name, text)
        finally:
            voice_file.unlink(missing_ok=True)

    def _event_done(self, task: asyncio.Task) -> None:
        self._event_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Falló el sonido de evento.", exc_info=task.exception())

    async def _play_event(self, channel: discord.VoiceChannel, match_id: str,
                          match_records: list[MatchRecord]) -> None:
        """Sonido (opcional) + frase hablada de una partida recién terminada."""
        s = self.bot.settings
        trolls = [v for v in (self.bot.trolls.evaluate(r) for r in match_records)
                  if not await self.bot.trolls.is_silenced(v)]
        carries = [self.bot.carries.evaluate(r) for r in match_records]
        spoken = pick_spoken_verdict(trolls, carries)

        to_play: list[Path] = []
        if spoken is not None:
            clip_name = s.sounds_event_victory if spoken.kind == CARRY else s.sounds_event_papelon
        else:  # sin trolleada ni carreada: igual puede ser una victoria épica
            clip_name = pick_event_sound(match_records, [v.level for v in trolls], "", s.sounds_event_victory)
        if clip_name:
            clip = self._find_sound(clip_name)
            if clip is None:
                log.warning("El sonido de evento '%s' no existe (ver /sonidos).", clip_name)
            else:
                to_play.append(clip)

        voice_file: Path | None = None
        if spoken is not None and s.sounds_event_voice:
            record = spoken.verdict.record
            member = channel.guild.get_member(record.discord_id)
            name = spoken_name(member.display_name if member else record.game_name)
            text = build_line(spoken, name)
            voice_file = await synthesize(text, self.sounds_dir / f".tts-{match_id}.mp3",
                                          s.sounds_tts_lang, s.sounds_tts_tld)
            if voice_file is not None:
                to_play.append(voice_file)
                log.info("Frase de evento (%s): %s", match_id, text)

        try:
            if to_play:
                await self._play_in(channel, *to_play)
        finally:
            if voice_file is not None:
                voice_file.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    # Loops: random y vencimiento de canjes
    # ------------------------------------------------------------------ #
    async def cog_load(self) -> None:
        self.expire_job.start()
        self.bot.ingest.add_listener(self._on_new_matches)
        if not self.bot.settings.sounds_enabled:
            log.info("Sonidos random desactivados (SOUNDS_ENABLED=false).")
            return
        self.random_job.change_interval(minutes=self.bot.settings.sounds_check_interval_minutes)
        self.random_job.start()
        log.info("Sonidos random: chequeo cada %s min, chance %.0f%%, cooldown %s min, horario %02d-%02d.",
                 self.bot.settings.sounds_check_interval_minutes,
                 self.bot.settings.sounds_chance * 100,
                 self.bot.settings.sounds_cooldown_minutes,
                 self.bot.settings.sounds_active_from,
                 self.bot.settings.sounds_active_to)

    async def cog_unload(self) -> None:
        self.expire_job.cancel()
        self.bot.ingest.remove_listener(self._on_new_matches)
        if self.random_job.is_running():
            self.random_job.cancel()

    @tasks.loop(minutes=5)
    async def random_job(self) -> None:
        now = dt.datetime.now(get_tz(self.bot.settings.timezone))
        if not self._in_active_hours(now):
            return
        cooldown = dt.timedelta(minutes=self.bot.settings.sounds_cooldown_minutes)
        if self._last_random_play and now - self._last_random_play < cooldown:
            return
        if random.random() >= self.bot.settings.sounds_chance:
            return

        sounds = self._sound_files()
        if not sounds:
            return
        for guild in self.bot.guilds:
            if self.bot.settings.guild_id and guild.id != self.bot.settings.guild_id:
                continue
            channels = self._occupied_voice_channels(guild)
            if not channels:
                continue
            channel, sound = random.choice(channels), random.choice(sounds)
            try:
                await self._play_in(channel, sound)
                self._last_random_play = now
            except Exception:
                log.exception("Falló el sonido random en #%s.", channel.name)

    @random_job.before_loop
    async def _before_random_job(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=30)
    async def expire_job(self) -> None:
        """Borra los sonidos canjeados que ya vencieron."""
        now = dt.datetime.now(dt.timezone.utc)
        expired = [n for n in self._meta if self._expires_at(n) <= now]
        for name in expired:
            sound = self._find_sound(name)
            if sound is not None:
                sound.unlink()
            await self._delete_from_soundboard(name)
            del self._meta[name]
            log.info("Sonido canjeado '%s' venció y se borró.", name)
        if expired:
            await self._save_meta()

    # ------------------------------------------------------------------ #
    # Slash commands
    # ------------------------------------------------------------------ #
    @app_commands.command(name="sonido", description="El bot entra a tu canal de voz, tira un sonido y se va.")
    @app_commands.describe(nombre="Nombre del sonido (ver /sonidos). Vacío = uno al azar.")
    async def sonido(self, interaction: discord.Interaction, nombre: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None \
                or not isinstance(member.voice.channel, discord.VoiceChannel):
            await interaction.followup.send("Tenés que estar en un canal de voz.")
            return

        if nombre:
            sound = self._find_sound(nombre)
            if sound is None:
                await interaction.followup.send(f"No existe el sonido `{nombre}`. Mirá `/sonidos`.")
                return
        else:
            sounds = self._sound_files()
            if not sounds:
                await interaction.followup.send("No hay sonidos cargados. Canjeá uno con `/canjear-sonido`.")
                return
            sound = random.choice(sounds)

        if self._play_lock.locked():
            await interaction.followup.send("Ya estoy tirando un sonido, esperá un toque.")
            return

        await interaction.followup.send(f"Voy con `{sound.stem}` 🔊")
        try:
            await self._play_in(member.voice.channel, sound)
        except Exception:
            log.exception("Falló /sonido en #%s.", member.voice.channel.name)

    @app_commands.command(name="sonidos", description="Lista los sonidos cargados.")
    async def sonidos(self, interaction: discord.Interaction) -> None:
        names = [p.stem for p in self._sound_files()]
        if not names:
            await interaction.response.send_message(
                "No hay sonidos todavía. Canjeá uno con `/canjear-sonido`.", ephemeral=True)
            return
        fixed = [n for n in names if n not in self._meta]
        redeemed = [n for n in names if n in self._meta]
        text = f"**Sonidos ({len(names)}):** " + ", ".join(f"`{n}`" for n in fixed)
        if redeemed:
            text += "\n**Canjeados:**\n" + "\n".join(
                f"`{n}` de <@{self._meta[n]['owner_id']}>, vence "
                f"{discord.utils.format_dt(self._expires_at(n), 'R')}" for n in redeemed)
        await interaction.response.send_message(
            text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="sonido-add",
                          description="Sube un sonido permanente (solo devs; mp3/ogg/wav, máx 2 MB).")
    @app_commands.describe(nombre="Nombre corto para el sonido (sin espacios).", archivo="El audio.")
    async def sonido_add(self, interaction: discord.Interaction, nombre: str, archivo: discord.Attachment) -> None:
        if not isinstance(interaction.user, discord.Member) or not self._is_dev(interaction.user):
            await interaction.response.send_message(
                "Solo devs. Para subir el tuyo usá `/canjear-sonido` (cuesta puntos, ver `/puntos`).",
                ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        name = self._clean_name(nombre)
        error = self._validate_upload(name, archivo)
        if error:
            await interaction.followup.send(error)
            return

        dest = self.sounds_dir / f"{name}{Path(archivo.filename).suffix.lower()}"
        await archivo.save(dest)
        log.info("Sonido '%s' agregado por %s.", name, interaction.user)
        await interaction.followup.send(f"Listo, `{name}` cargado. Probalo con `/sonido {name}`.")

    @app_commands.command(name="canjear-sonido",
                          description="Canjeá puntos por un sonido tuyo en el soundboard del server.")
    @app_commands.describe(nombre="Nombre corto para el sonido (sin espacios).",
                           archivo="El audio (mp3/ogg/wav, cortito).")
    async def canjear_sonido(self, interaction: discord.Interaction, nombre: str,
                             archivo: discord.Attachment) -> None:
        s = self.bot.settings
        member = interaction.user
        if not isinstance(member, discord.Member) or not self._has_role(member, s.points_role_id):
            await interaction.response.send_message("Necesitás el rol de Boguero para canjear.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        name = self._clean_name(nombre)
        error = self._validate_upload(name, archivo)
        if not error and len(name) < 2:
            error = "El nombre tiene que tener al menos 2 caracteres."
        if error:
            await interaction.followup.send(error)
            return
        if interaction.guild is None:
            await interaction.followup.send("Esto se usa dentro del server.")
            return
        active = self._active_redeems(member.id)
        if len(active) >= s.sound_redeem_max_active:
            await interaction.followup.send(
                f"Ya tenés {len(active)} sonido(s) activo(s) ({', '.join(f'`{n}`' for n in active)}). "
                f"El máximo es {s.sound_redeem_max_active}: esperá a que venza o borralo con `/sonido-del`.")
            return
        balance = self.bot.points.balance(member.id)
        if balance < s.sound_redeem_cost:
            await interaction.followup.send(
                f"Te faltan puntos: tenés **{balance}** y el canje cuesta **{s.sound_redeem_cost}**. Mirá `/puntos`.")
            return

        # Se baja a un archivo temporal (sufijo .part: no aparece en la lista)
        # y recién se publica si pasa el chequeo de duración y se pudo cobrar.
        suffix = Path(archivo.filename).suffix.lower()
        tmp = self.sounds_dir / f".{name}{suffix}.part"
        max_seconds = min(s.sound_redeem_max_seconds, _SOUNDBOARD_MAX_SECONDS)
        await archivo.save(tmp)
        try:
            seconds = await self._duration_seconds(tmp)
            if seconds is None:
                await interaction.followup.send("No pude leer ese audio. ¿Seguro que es un mp3/ogg/wav válido?")
                return
            if seconds > max_seconds + 0.2:
                await interaction.followup.send(
                    f"Dura {seconds:.1f} s y el máximo es {max_seconds:g} s (límite del soundboard). "
                    f"Recortalo y probá de nuevo.")
                return
            data = await self._soundboard_bytes(tmp)
            if data is None:
                await interaction.followup.send("No pude preparar ese audio para el soundboard. Probá con un mp3.")
                return
            if not await self.bot.points.spend(member.id, s.sound_redeem_cost, f"canje sonido {name}"):
                await interaction.followup.send("Te faltan puntos (¿canjeaste otra cosa recién?).")
                return
            try:
                board = await interaction.guild.create_soundboard_sound(
                    name=name, sound=data, reason=f"Canje de puntos de {member}")
            except discord.HTTPException as exc:
                await self.bot.points.add({member.id: s.sound_redeem_cost}, f"reintegro canje sonido {name}")
                log.warning("No pude subir '%s' al soundboard (%s).", name, exc)
                why = ("el bot no tiene permiso para crear expresiones" if isinstance(exc, discord.Forbidden)
                       else "¿está lleno el soundboard?")
                await interaction.followup.send(f"No pude subirlo al soundboard ({why}). Te devolví los puntos.")
                return
            tmp.replace(self.sounds_dir / f"{name}{suffix}")
        finally:
            tmp.unlink(missing_ok=True)

        expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=s.sound_redeem_days)
        self._meta[name] = {"owner_id": member.id, "expires_at": expires.isoformat(),
                            "soundboard_id": board.id}
        await self._save_meta()
        log.info("Sonido '%s' canjeado por %s (%.1f s), vence %s.", name, member, seconds, expires)
        await interaction.followup.send(
            f"Listo, `{name}` está en el soundboard del server hasta {discord.utils.format_dt(expires, 'f')}. "
            f"Te quedan **{self.bot.points.balance(member.id)}** puntos.")
        if isinstance(interaction.channel, discord.abc.Messageable):
            await interaction.channel.send(
                f"🔊 {member.mention} canjeó el sonido `{name}`: ya está en el soundboard por "
                f"{s.sound_redeem_days} días. ¡Tírenlo cuando quieran!",
                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="sonido-del", description="Borra un sonido (devs, o el dueño de uno canjeado).")
    @app_commands.describe(nombre="Nombre del sonido a borrar.")
    async def sonido_del(self, interaction: discord.Interaction, nombre: str) -> None:
        sound = self._find_sound(nombre)
        member = interaction.user
        is_owner = sound is not None and self._meta.get(sound.stem, {}).get("owner_id") == member.id
        if not isinstance(member, discord.Member) or not (self._is_dev(member) or is_owner):
            await interaction.response.send_message("No tenés permiso para borrar ese sonido.", ephemeral=True)
            return
        if sound is None:
            await interaction.response.send_message(f"No existe `{nombre}`.", ephemeral=True)
            return
        sound.unlink()
        await self._delete_from_soundboard(sound.stem)
        if self._meta.pop(sound.stem, None) is not None:
            await self._save_meta()
        log.info("Sonido '%s' borrado por %s.", sound.stem, interaction.user)
        await interaction.response.send_message(f"`{sound.stem}` borrado.", ephemeral=True)

    @app_commands.command(name="sonido-forzar",
                          description="Hace que el bot entre a un canal de voz y tire un sonido (solo devs).")
    @app_commands.describe(nombre="Sonido a tirar. Vacío = uno al azar.",
                           canal="Canal de voz. Vacío = el tuyo, o uno con gente al azar.")
    async def sonido_forzar(self, interaction: discord.Interaction, nombre: str | None = None,
                            canal: discord.VoiceChannel | None = None) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not self._is_dev(member):
            await interaction.response.send_message("No tenés permiso para usar este comando.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        if nombre:
            sound = self._find_sound(nombre)
            if sound is None:
                await interaction.followup.send(f"No existe el sonido `{nombre}`. Mirá `/sonidos`.")
                return
        else:
            sounds = self._sound_files()
            if not sounds:
                await interaction.followup.send("No hay sonidos cargados.")
                return
            sound = random.choice(sounds)

        if canal is None and member.voice is not None and isinstance(member.voice.channel, discord.VoiceChannel):
            canal = member.voice.channel
        if canal is None:
            occupied = self._occupied_voice_channels(member.guild)
            if not occupied:
                await interaction.followup.send("No hay nadie en ningún canal de voz. Elegí uno con `canal`.")
                return
            canal = random.choice(occupied)

        # A diferencia de /sonido, si hay otro sonido sonando espera su turno
        # (el lock de _play_in) en vez de rechazar.
        await interaction.followup.send(f"Voy con `{sound.stem}` a {canal.mention} 🔊")
        log.info("Sonido '%s' forzado en #%s por %s.", sound.stem, canal.name, member)
        try:
            await self._play_in(canal, sound)
        except Exception:
            log.exception("Falló /sonido-forzar en #%s.", canal.name)
            await interaction.followup.send(f"No pude tirar el sonido en {canal.mention}, mirá los logs.")

    @sonido_forzar.autocomplete("nombre")
    async def _sonido_forzar_nombre(self, interaction: discord.Interaction,
                                    current: str) -> list[app_commands.Choice[str]]:
        current = current.lower()
        return [app_commands.Choice(name=p.stem, value=p.stem)
                for p in self._sound_files() if current in p.stem.lower()][:25]
