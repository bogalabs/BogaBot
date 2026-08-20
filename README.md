# BogaBot

Bot de Discord modular para el grupo. **Módulo 1 (MVP): ranking de League of Legends.**

Vincula cuentas de Riot, ingiere partidas desde la Riot API una vez al día,
calcula un puntaje configurable por jugador y publica rankings (diario y
"Trolls y Pros" semanal) en Discord.

## Arquitectura (por capas, cada una reemplazable)

```
run.py                      # entrypoint: python run.py
src/bogabot/
├── main.py                 # bootstrap (logging + settings + run)
├── settings.py             # lee .env -> objeto Settings (falla si falta algo)
├── bot.py                  # composition root: arma e inyecta todo
├── core/
│   ├── models.py           # dominio: PlayerLink, MatchRecord, PlayerStats, RankingRow
│   └── timeutils.py        # cortes de día/semana según TIMEZONE
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
    ├── cog.py              # /link /unlink /ranking
    ├── ingest.py           # ingesta de partidas
    ├── ranking.py          # agrega stats + arma embeds
    └── scheduler.py        # job diario (discord.ext.tasks)
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
| `RIOT_API_KEY` | API key de Riot (la dev key vence cada 24h). |
| `RIOT_PLATFORM` | Plataforma (LAS = `la2`). |
| `RIOT_REGION` | Routing regional (LAS/LAN/NA → `americas`). |
| `TIMEZONE` | Zona horaria para los cortes de día/semana. |
| `DAILY_POST_HOUR` | Hora local del job diario. |

### Permisos del bot en Discord
- Debe poder **leer y escribir** en `STORAGE_CHANNEL_ID` y `RANKING_CHANNEL_ID`,
  y **leer el historial** del canal de storage.
- Al invitarlo, activá el scope `applications.commands` (para slash commands).
- No requiere intents privilegiados.

## Comandos
- `/link <Nombre#TAG>` — vincula tu cuenta de Riot (valida contra la API).
- `/unlink` — desvincula tu cuenta.
- `/ranking [Hoy|Semana]` — muestra el ranking on-demand.

## Cómo se calcula el ranking
Cada partida se guarda como un registro por jugador. En cada ventana (día o
semana desde el domingo), se agregan las stats por jugador y el motor de
scoring aplica la fórmula de `config/scoring.yaml`. Ver comentarios en ese
archivo para ajustar métricas y pesos.

## Tests
```powershell
python -m pytest    # o:  python -m unittest
```
