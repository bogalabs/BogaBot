# CLAUDE.md — Guía del proyecto para agentes

Este archivo lo cargan automáticamente los agentes de IA (Claude Code) al
trabajar en el repo. Es la fuente de verdad de **cómo** se trabaja acá.
Guía específica de cada colaborador: ver `docs/agents/`.
Estado del proyecto (avances/faltantes): ver `docs/PROGRESS.md`.

---

## Qué es BogaBot

Bot de Discord **modular**, pensado para crecer con más funciones.
**Módulo 1 (MVP actual): ranking de League of Legends del grupo de amigos.**

Flujo: vincular cuenta de Riot → ingerir partidas desde la Riot API (1×/día) →
calcular un puntaje configurable por jugador → publicar rankings en Discord.

## Reglas innegociables

1. **Nunca hardcodear secretos ni rutas.** Todo sale de variables de entorno
   vía `settings.py` (que lee `.env`). El código no debe asumir que corre en
   una máquina puntual.
2. **`.env` nunca se commitea** (está en `.gitignore`). Cambios de config →
   actualizar también `.env.example` documentando la variable.
3. **El resto de la app no sabe de dónde salen los datos.** Todo pasa por las
   interfaces de `storage/base.py` (`LinkRepository`, `MatchRepository`).
   Nunca importar `DiscordChannelStorage` fuera de `bot.py`.
4. **Todo es async.** No usar librerías bloqueantes en el event loop
   (por eso Riot se consume con `aiohttp`, no con wrappers sync).
5. **Atribución de commits:** los co-autores son SOLO los colaboradores
   humanos reales del proyecto. **No agregar agentes de IA como autor,
   co-autor ni en comentarios/PRs.** No usar `Co-Authored-By` de herramientas.

## Arquitectura (capas reemplazables)

```
run.py                      # entrypoint: python run.py
src/bogabot/
├── main.py                 # bootstrap (logging + settings + run)
├── settings.py             # .env -> Settings (falla temprano si falta algo)
├── bot.py                  # composition root: arma e INYECTA todo
├── core/
│   ├── models.py           # dominio: PlayerLink, MatchRecord, PlayerStats, RankingRow
│   ├── timeutils.py        # cortes de día/semana según TIMEZONE
│   └── discord_log_handler.py  # logging.Handler -> cola -> bot.py la manda a LOG_CHANNEL_ID
├── storage/                # capa Repository
│   ├── base.py             # interfaces (ABC) — TODO depende de esto
│   ├── discord_channel.py  # impl "Discord como DB" (actual)
│   └── memory.py           # impl en memoria (tests)
├── riot/
│   ├── client.py           # aiohttp + rate limiter (20/s, 100/2min)
│   └── mapper.py           # ÚNICO lugar que conoce el JSON de match-v5
├── scoring/
│   ├── schema.py           # carga/valida config/scoring.yaml
│   └── engine.py           # puntaje compuesto (normaliza + pondera)
└── modules/lol/            # feature LoL como cog autocontenido
    ├── cog.py              # slash commands /link /unlink /link-admin /ingest-now /ranking /help /ayuda
    ├── ingest.py           # ingesta de partidas (devuelve list[MatchRecord] nuevos)
    ├── ranking.py          # agrega stats + arma embeds
    └── scheduler.py        # daily_job (1x/día) + notify_job (cada N min, avisos en vivo)
config/scoring.yaml         # fórmula del ranking, editable sin tocar código
tests/                      # tests de lógica pura (sin red ni tokens)
```

### Cómo fluye una dependencia
`bot.py` es el **único** lugar que instancia implementaciones concretas
(`DiscordChannelStorage`, `RiotClient`, `ScoringEngine`) y las inyecta en los
servicios (`IngestService`, `RankingService`) y cogs. Todo lo demás recibe
interfaces por constructor. Para swapear el storage a SQLite/Postgres:
escribir la clase nueva en `storage/` y cambiar **una línea** en `bot.py`.

## Convenciones de código

- **Comentarios y docstrings en español** (es el idioma del equipo).
- Nombres de código (variables, funciones, clases) en inglés.
- Type hints siempre. `from __future__ import annotations` arriba de cada módulo.
- Dataclasses para modelos del dominio; serialización con `to_dict`/`from_dict`.
- Un error de un jugador no debe frenar al resto en la ingesta (capturar y loguear).
- Logging con el logger del módulo (`log = logging.getLogger(__name__)`), no `print`.

## Cómo extender (patrones esperados)

- **Agregar una métrica de scoring:** sumarla en `scoring/schema.py`
  (`SUPPORTED_METRICS`) + una property en `core/models.py::PlayerStats`, y
  listarla en `config/scoring.yaml`. Cero cambios en el motor.
- **Agregar un slash command LoL:** método nuevo en `modules/lol/cog.py`.
- **Restringir un comando por rol/canal:** el ID de rol o canal sale de
  `settings.py` (nunca hardcodeado), y el chequeo se hace al inicio del
  handler con un helper tipo `_is_dev`/`_has_role` (ver `LolCog`). Ejemplo:
  `DEV_ROLE_ID` + `ADMIN_CHANNEL_ID` gatean `/link-admin`.
- **Agregar una feature nueva (no-LoL, ej. módulo de IA):** carpeta nueva en
  `modules/`, con su cog, y registrarla en `bot.py::setup_hook`. No tocar LoL.
- **Cambiar el backend de datos:** nueva clase en `storage/` que implemente
  `LinkRepository`/`MatchRepository`; swap en `bot.py`.

## Decisiones ya tomadas (no re-litigar sin motivo)

- **Región:** LAS → `RIOT_PLATFORM=la2`, `RIOT_REGION=americas` (routing de
  account-v1 y match-v5).
- **Colas contadas:** todas (incluye ARAM/rotativos) para el ranking diario/
  semanal. Se **excluyen remakes** (< 5 min o early surrender) en
  `riot/mapper.py`. **Excepción:** el recap "Trolls y Pros" (`previous_week_rows`
  en `ranking.py`) solo cuenta **Ranked Flex** (`RANKED_FLEX_QUEUE_ID = 440`
  en `riot/mapper.py`), para medir el juego serio del grupo.
- **Tipo de partida y rival de línea en los avisos:** `riot/mapper.py::queue_name`
  traduce `queue_id` a un nombre legible (Ranked Flex, ARAM, etc.) y
  `MatchRecord.opponent_champion` guarda al rival de línea (mismo
  `teamPosition`, equipo contrario; `""` si no aplica, ej. ARAM).
- **Aviso "en vivo" con cuadro completo (10 jugadores):** a diferencia del
  `MatchRecord` (que persiste solo a los jugadores vinculados, uno por fila),
  el aviso de partida terminada necesita ver a los 10. Para eso
  `IngestService.build_match_summary(match_id)` vuelve a pedirle la partida
  a Riot y `riot/mapper.py::map_match_summary` arma un `MatchSummary`
  (`core/models.py`, no se persiste) con un `MatchParticipant` por jugador,
  marcando `discord_id` cuando el puuid está vinculado. `scheduler.py::_match_notification_embed`
  arma el embed: resultado general (o "equipos contrarios" si el grupo quedó
  dividido), un field por equipo y un field "línea vs línea" con el
  matchup por posición. Se etiqueta (`<@id>`) a los vinculados; al resto se
  los muestra por su Riot ID.
- **Farm (CS):** `riot/mapper.py::_farm` suma `totalMinionsKilled` +
  `neutralMinionsKilled`. Se guarda en `MatchRecord.cs` (ranking/scoring) y
  en `MatchParticipant.cs` (aviso en vivo). `PlayerStats.avg_cs`/`cs_per_min`
  quedan disponibles y `cs_per_min` está en `SUPPORTED_METRICS`
  (`scoring/schema.py`), pero **no** está activada por defecto en
  `config/scoring.yaml` — es opt-in.
- **Solo cuentan partidas jugadas con otro vinculado:** en `ingest.py`, antes
  de mapear una partida se chequea `metadata.participants` del JSON de
  match-v5 contra el set de puuids vinculados; si hay menos de 2 vinculados
  en esa partida (o sea, jugaste sin nadie del grupo), se descarta.
- **Semana = lunes 00:00 hora local** (`TIMEZONE`, default Buenos Aires).
- **Job 1×/día** a `DAILY_POST_HOUR:DAILY_POST_MINUTE` (recomendado cerca de
  medianoche, ej. 23:55, para que el "ranking de hoy" no salga vacío si se
  juega de noche): ingesta → ranking diario; los lunes, recap semanal
  "Trolls y Pros" de la semana que cerró.
- **Dedup por `(match_id, puuid)`**, sin cursor: cada corrida pide "desde el
  lunes" y saltea lo ya guardado. Por esto `/ingest-now` (comando manual de
  ingesta, solo rol dev) es idempotente: correrlo varias veces no duplica nada.
- **Storage actual = Discord** (mensajes JSON en canal privado + índice en
  memoria hidratado al arrancar). Es O(n) mensajes; migrar a DB real cuando
  crezca (la interfaz lo hace trivial).
- **Comandos de administración** (`/link-admin`, `/ingest-now`) gatean por
  rol (`DEV_ROLE_ID`) y opcionalmente por canal (`ADMIN_CHANNEL_ID`), ambos
  configurables por `.env`. `/help` y `/ayuda` muestran comandos distintos
  según el rol de quien pregunta (`DEV_ROLE_ID` vs `PLAYER_ROLE_ID`).
- **Avisos de partida terminada:** `LolScheduler.notify_job` (solo si hay
  `MATCH_NOTIFY_CHANNEL_ID`) llama a `ingest_all()` cada
  `MATCH_POLL_INTERVAL_MINUTES` y postea un mensaje por partida nueva,
  agrupando por `match_id` y etiquetando a cada jugador del grupo que
  participó (con su resultado individual, por si quedaron en equipos
  contrarios). `daily_job` llama al mismo helper (`_notify_new_matches`)
  como red de seguridad. El dedup existente hace que correr `ingest_all()`
  desde los dos loops sea gratis.
- **Logging a Discord:** `DiscordLogHandler` (en `core/`) se engancha al
  logger `"bogabot"` (no a `discord.*`, para no capturar el ruido de la
  librería) cuando hay `LOG_CHANNEL_ID`. Solo encola texto formateado en
  `emit()` (es sync); `BogaBot._flush_log_channel` (un `tasks.loop`) vacía
  la cola cada 15s y la manda al canal. Nivel por defecto `WARNING`
  (`LOG_CHANNEL_LEVEL`) para no saturar el canal con el polling de
  `notify_job`.

## Correr y testear (Windows / PowerShell)

```powershell
venv\Scripts\activate
pip install -r requirements.txt      # primera vez
python run.py                        # requiere .env completo
python -m unittest discover -s tests -v
```

Setup completo y tabla de variables: ver `README.md`.
