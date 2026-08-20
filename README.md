# BogaBot

Bot de Discord modular para el grupo. **Módulo 1 (MVP): ranking de League of Legends.**

Vincula cuentas de Riot, ingiere partidas desde la Riot API (una vez al día y,
opcionalmente, cada pocos minutos para avisar en vivo), calcula un puntaje
configurable por jugador y publica rankings (diario y "Trolls y Pros" semanal)
en Discord.

## Arquitectura (por capas, cada una reemplazable)

```
run.py                      # entrypoint: python run.py
src/bogabot/
├── main.py                 # bootstrap (logging + settings + run)
├── settings.py             # lee .env -> objeto Settings (falla si falta algo)
├── bot.py                  # composition root: arma e inyecta todo
├── core/
│   ├── models.py           # dominio: PlayerLink, MatchRecord, PlayerStats, RankingRow
│   ├── timeutils.py        # cortes de día/semana según TIMEZONE
│   └── discord_log_handler.py  # logging.Handler que reenvía logs a LOG_CHANNEL_ID
├── storage/                # capa Repository (interfaz + implementaciones)
│   ├── base.py             # LinkRepository / MatchRepository (ABCs)
│   ├── discord_channel.py  # "Discord como DB" (implementación actual)
│   └── memory.py           # implementación en memoria (tests)
├── riot/
│   ├── client.py           # cliente aiohttp + rate limiter
│   └── mapper.py           # JSON de match-v5 -> MatchRecord
├── scoring/
│   ├── schema.py           # carga/valida config/scoring.yaml
│   └── engine.py           # calcula el puntaje compuesto
└── modules/lol/            # feature LoL (un "cog" autocontenido)
    ├── cog.py              # /link /unlink /link-admin /ingest-now /ranking /help /ayuda
    ├── ingest.py           # ingesta de partidas
    ├── ranking.py          # agrega stats + arma embeds
    └── scheduler.py        # job diario + chequeo de partidas nuevas (discord.ext.tasks)
config/scoring.yaml         # fórmula del ranking, editable sin tocar código
```

**Ideas clave**
- **Storage detrás de una interfaz.** Todo depende de `LinkRepository` /
  `MatchRepository`, no de Discord. Migrar a SQLite/Postgres/Firebase = escribir
  una clase nueva y cambiar una línea en `bot.py`.
- **Módulos como cogs.** Sumar features (incluido un futuro módulo de IA) es
  agregar una carpeta en `modules/` y registrarla en `bot.py`.
- **Scoring configurable.** La fórmula vive en `config/scoring.yaml` (pesos,
  métricas, normalización). El grupo la ajusta sin tocar código.

## Puesta en marcha (Windows / PowerShell)

```powershell
# 1. Entorno virtual
python -m venv venv
venv\Scripts\activate

# 2. Dependencias
pip install -r requirements.txt

# 3. Configuración
copy .env.example .env
# Editá .env y completá los valores (ver abajo).

# 4. Correr
python run.py
```

### Variables de entorno (.env)

| Variable | Descripción |
|---|---|
| `DISCORD_TOKEN` | Token del bot. |
| `DISCORD_GUILD_ID` | ID del server (opcional; registra los comandos al instante). |
| `STORAGE_CHANNEL_ID` | Canal privado que el bot usa como base de datos. |
| `RANKING_CHANNEL_ID` | Canal donde publica los rankings. |
| `DEV_ROLE_ID` | Rol habilitado para comandos de administración (`/link-admin`). |
| `ADMIN_CHANNEL_ID` | Canal donde se pueden correr esos comandos (opcional). |
| `PLAYER_ROLE_ID` | Rol de jugador/miembro; solo afecta qué ve `/help` y `/ayuda`. |
| `MATCH_NOTIFY_CHANNEL_ID` | Canal donde se avisa cuando termina una partida (opcional). |
| `MATCH_POLL_INTERVAL_MINUTES` | Cada cuántos minutos se chequean partidas nuevas para ese aviso. |
| `RIOT_API_KEY` | API key de Riot (la dev key vence cada 24h). |
| `RIOT_PLATFORM` | Plataforma (LAS = `la2`). |
| `RIOT_REGION` | Routing regional (LAS/LAN/NA → `americas`). |
| `TIMEZONE` | Zona horaria para los cortes de día/semana. |
| `DAILY_POST_HOUR` / `DAILY_POST_MINUTE` | Hora/minuto local del job diario (recomendado: cerca de medianoche). |
| `LOG_CHANNEL_ID` | Canal donde el bot manda sus propios logs (opcional). |
| `LOG_CHANNEL_LEVEL` | Nivel mínimo que se manda a ese canal (default `WARNING`). |

### Permisos del bot en Discord
- Debe poder **leer y escribir** en `STORAGE_CHANNEL_ID` y `RANKING_CHANNEL_ID`,
  y **leer el historial** del canal de storage.
- Al invitarlo, activá el scope `applications.commands` (para slash commands).
- No requiere intents privilegiados.

## Comandos
- `/link <Nombre#TAG>` — vincula tu cuenta de Riot (valida contra la API).
- `/unlink` — desvincula tu cuenta.
- `/link-admin <usuario> <Nombre#TAG>` — vincula la cuenta de Riot de otro
  usuario del server. Requiere el rol `DEV_ROLE_ID` y, si está configurado,
  correrse en el canal `ADMIN_CHANNEL_ID`.
- `/ranking [Hoy|Semana]` — muestra el ranking on-demand.
- `/help` y `/ayuda` — listan los comandos disponibles según el rol de quien
  los usa (rol `DEV_ROLE_ID` ve administración, rol `PLAYER_ROLE_ID` ve los
  comandos de ranking). Ambos hacen lo mismo, son solo dos nombres.
- `/ingest-now` — fuerza una ingesta de partidas manual (solo rol dev). Es
  idempotente: correrlo varias veces no duplica nada, el dedup por
  (match_id, puuid) saltea lo que ya está guardado.

## Avisos "en vivo" y logs

- **Partida terminada:** si `MATCH_NOTIFY_CHANNEL_ID` está seteado, cada
  `MATCH_POLL_INTERVAL_MINUTES` (5 por defecto) el bot chequea partidas
  nuevas y, por cada una que cuente (jugada con otro vinculado), postea un
  mensaje etiquetando a los jugadores del grupo que la jugaron — con el
  resultado de cada uno, por si terminaron en equipos contrarios.
- **Logs del bot:** si `LOG_CHANNEL_ID` está seteado, los logs de nivel
  `LOG_CHANNEL_LEVEL` (WARNING por defecto) o superior se reenvían también a
  ese canal, además de la consola. Pensado para enterarte de errores (Riot
  caído, key vencida, etc.) sin tener que mirar la terminal.

## Cómo se calcula el ranking
Cada partida se guarda como un registro por jugador, **pero solo si jugaste
esa partida acompañado de al menos otro jugador vinculado del grupo** (las
partidas jugadas sin ningún conocido se descartan en la ingesta). En cada
ventana (día o semana desde el lunes), se agregan las stats por jugador y el
motor de scoring aplica la fórmula de `config/scoring.yaml`. Ver comentarios
en ese archivo para ajustar métricas y pesos.

## Tests
```powershell
python -m pytest    # o:  python -m unittest
```
