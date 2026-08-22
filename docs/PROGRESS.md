# Avances y faltantes — BogaBot

Documento **vivo**: actualizarlo a medida que avanza el proyecto. Marcar con
`[x]` lo hecho y mover items entre secciones. Poner la fecha en cada cambio.

_Última actualización: 2026-08-20 (3)_

---

## 🎯 Alcance actual
**Módulo 1 (MVP): ranking de LoL del grupo.** Sin funciones de IA todavía.

## ✅ Hecho

- [x] Estructura del proyecto por capas (`src/bogabot/`) — 2026-08-19
- [x] Config por entorno: `settings.py` + `.env.example` + `.gitignore` — 2026-08-19
- [x] Modelos del dominio (`core/models.py`) con serialización — 2026-08-19
- [x] Utilidades de tiempo con cortes día/semana por TIMEZONE — 2026-08-19
- [x] Capa Repository: interfaces (`storage/base.py`) — 2026-08-19
- [x] Storage "Discord como DB" con índice en memoria (`discord_channel.py`) — 2026-08-19
- [x] Storage en memoria para tests (`storage/memory.py`) — 2026-08-19
- [x] Cliente Riot async + rate limiter + manejo de 429/404 (`riot/client.py`) — 2026-08-19
- [x] Mapper de match-v5 → MatchRecord, con descarte de remakes — 2026-08-19
- [x] Motor de scoring configurable por YAML + validación — 2026-08-19
- [x] Servicio de ingesta con dedup por `(match_id, puuid)` — 2026-08-19
- [x] Servicio de ranking + embeds (diario y "Trolls y Pros") — 2026-08-19
- [x] Slash commands `/link`, `/unlink`, `/ranking` — 2026-08-19
- [x] Scheduler diario (`discord.ext.tasks`) con recap semanal los lunes — 2026-08-19
- [x] Composition root (`bot.py`) + bootstrap (`main.py`, `run.py`) — 2026-08-19
- [x] venv + `requirements.txt` instalados y verificados — 2026-08-19
- [x] Tests de scoring y dedup en verde (`tests/test_scoring.py`) — 2026-08-19
- [x] Docs: `README.md`, `CLAUDE.md`, `docs/agents/`, este archivo — 2026-08-19
- [x] `.env` completo con IDs reales; bot invitado y con permisos — 2026-08-19
- [x] Comando `/link-admin` (solo rol `DEV_ROLE_ID`, opcionalmente restringido
      a `ADMIN_CHANNEL_ID`) para que un dev vincule la cuenta de Riot de otro
      usuario del server buscándolo por Discord — 2026-08-19
- [x] Comandos `/help` y `/ayuda`: listan comandos disponibles según rol
      (`DEV_ROLE_ID` ve administración, `PLAYER_ROLE_ID` ve ranking) — 2026-08-19
- [x] Comando `/ingest-now` (solo rol dev) para forzar ingesta manual;
      idempotente por el dedup existente de `(match_id, puuid)` — 2026-08-20
- [x] Semana cambiada de domingo→domingo a **lunes→domingo**
      (`timeutils.py::start_of_week`); recap semanal ahora se postea los
      lunes — 2026-08-20
- [x] Job diario ahora soporta minuto configurable (`DAILY_POST_MINUTE`),
      seteado a **23:55** para que el "ranking de hoy" no salga vacío por
      correr antes de que el grupo termine de jugar — 2026-08-20
- [x] Primera prueba real: `/link` funcionó end-to-end (cuenta de Riot
      encontrada y vínculo guardado en `boga-storage-lol`) — 2026-08-20
- [x] Regla nueva: **una partida solo cuenta si jugaste con al menos otro
      vinculado del grupo** (se filtra en `ingest.py` contra
      `metadata.participants` de match-v5). Aplica tanto al ranking diario
      como al semanal "Trolls y Pros" porque ambos leen de lo ya guardado.
      Verificado con un test manual (partida en solitario descartada,
      partida compartida guardada para ambos jugadores) — 2026-08-20
- [x] `/help` y `/ayuda` actualizados con `/ingest-now` y la aclaración de
      la regla de "jugar acompañado" — 2026-08-20
- [x] `ingest_all()` ahora devuelve `list[MatchRecord]` (antes un `int`) para
      que quien la llame sepa QUÉ se guardó, no solo cuánto — 2026-08-20
- [x] Aviso "en vivo" de partida terminada: nuevo `notify_job` en
      `scheduler.py` (corre cada `MATCH_POLL_INTERVAL_MINUTES`, default 5),
      postea en `MATCH_NOTIFY_CHANNEL_ID` etiquetando a los jugadores del
      grupo que jugaron esa partida (agrupado por match_id, con el resultado
      individual de cada uno por si quedaron en equipos contrarios). El job
      diario usa el mismo helper como red de seguridad — 2026-08-20
- [x] Logging hacia Discord: `core/discord_log_handler.py` +
      `BogaBot._flush_log_channel`. Si hay `LOG_CHANNEL_ID`, los logs
      `WARNING`+ (`LOG_CHANNEL_LEVEL`) de la app se reenvían también a ese
      canal (`boga-bot-log`), no solo a la consola — 2026-08-20
- [x] `.env` con `MATCH_NOTIFY_CHANNEL_ID`, `MATCH_POLL_INTERVAL_MINUTES`,
      `LOG_CHANNEL_ID` y `LOG_CHANNEL_LEVEL` completados — 2026-08-20
- [x] Aviso de RIOT_API_KEY vencida: `RiotAuthError` (401/403) en
      `riot/client.py` corta la corrida de `ingest_all()` y loguea un
      `CRITICAL` (con cooldown de 3h) que llega a `boga-bot-log` — 2026-08-21
- [x] Script de autorun local `scripts/run_bot.ps1` (consola visible + log
      en `logs/`) — 2026-08-21
- [x] Ranking de trolleadas y papelones (`feature/troll-ranking`, PR #2):
      nuevos campos `participant_id` y `game_ended_in_surrender` en
      `MatchRecord` + `MatchRecord.is_troll_game()` (KDA < 0.5, FF antes de
      los 20 min) e `is_papelon()` (derrota antes de los 25 min). Cada
      corrida del scheduler detecta partidas trolleadas/papelón entre las
      últimas 48hs y avisa en `RANKING_CHANNEL_ID` (trolls) y
      `GENERAL_CHANNEL_ID` (papelones, nuevo canal en `.env`); hay un
      ranking histórico de trolls (`all_time_troll_counts` +
      `build_troll_ranking_embed`) y el aviso de troll consulta el timeline
      de la partida (`RiotClient.get_match_timeline`, nuevo) para mostrar en
      qué minuto murió por primera vez — 2026-08-22

## 🚧 En progreso
- [ ] _(nada activo)_

## ⛔ Faltante / próximos pasos

### Bloqueante para probar en vivo (lo hace José)
- [x] Completar `.env` (token, IDs de canal, Riot API key) — 2026-08-19
- [x] Crear canal privado de storage y canal de rankings; copiar sus IDs — 2026-08-19
- [x] Invitar el bot con scope `applications.commands` y permisos de lectura/
      escritura + leer historial en ambos canales — 2026-08-19
- [x] Crear canal de administración (`ADMIN_CHANNEL_ID`) y definir el rol
      `DEV_ROLE_ID` para `/link-admin` — 2026-08-19
- [x] `PLAYER_ROLE_ID` (rol de jugador/miembro) seteado para que `/help` y
      `/ayuda` muestren el menú correcto — 2026-08-19
- [ ] Probar `/ingest-now` → `/ranking` con partidas reales del grupo.
- [ ] Ver el posteo automático del job diario (23:55) y el recap semanal
      del próximo lunes.
- [ ] Probar `/link-admin` en el canal de administración con el rol dev.
- [ ] Dar permiso de **ver el canal admin** al rol boguero (`PLAYER_ROLE_ID`)
      en Discord (Editar canal → Permisos). Esto es config de Discord, no de
      código: `/link-admin` e `/ingest-now` siguen exigiendo `DEV_ROLE_ID`
      aunque más gente pueda ver el canal.
- [ ] Confirmar que el bot tiene permiso de **enviar mensajes** en
      `boga-bot-log` (1539827725525717014) y en el canal de avisos de
      partida (1539828323532677231).
- [ ] Probar el aviso "en vivo": jugar una partida con otro vinculado y
      esperar hasta 5 min (`MATCH_POLL_INTERVAL_MINUTES`) a que aparezca el
      mensaje en el canal de avisos.
- [ ] Provocar un error a propósito (ej. Riot API key vencida) y confirmar
      que aparece en `boga-bot-log`. Código listo desde 2026-08-21
      (`RiotAuthError` en `riot/client.py` + alerta `CRITICAL` en
      `ingest.py`), falta probarlo contra una key realmente vencida.
- [ ] Registrar la Tarea Programada de Windows para el autorun (comando en
      `scripts/run_bot.ps1`; falta correr `Register-ScheduledTask`).

### Mejoras de producto/DX
- [ ] Comando para ver/editar el estado del propio vínculo (`/whoami`).
- [ ] Paginado/estrategia si el canal de storage supera el `HYDRATE_LIMIT`.
- [ ] Manejo de Riot IDs que cambian de nombre (re-resolver puuid).

### Robustez
- [ ] Reintentos/backoff más finos ante errores transitorios de Riot.
- [ ] Tests del mapper (con un JSON de match-v5 de ejemplo).

## 🗺️ Backlog / futuro (fuera del MVP)
- [ ] Migrar storage de Discord a una DB real (SQLite → Postgres/Firebase).
      La interfaz Repository ya lo deja listo: solo cambia `bot.py`.
- [ ] Módulo de chat conversacional con IA (como cog aparte).
- [ ] Nuevas fuentes de datos / features no relacionadas a LoL.
- [ ] Estadísticas históricas y tendencias (más allá de día/semana).

## ⚠️ Verificado vs. no verificado
- **Verificado:** instalación de deps, import de los módulos, validación de
  `scoring.yaml`, lógica de scoring y dedup (tests), `/link` end-to-end
  contra Discord + Riot reales.
- **No verificado aún:** ingesta de partidas reales (`/ingest-now`) — en
  particular el filtro de "jugar acompañado" contra la Riot API real (solo
  se probó con datos simulados), `/ranking` con datos reales, `/link-admin`,
  posteo automático del job diario (23:55), el recap semanal del lunes, el
  aviso en vivo de partida terminada (`notify_job` / `MATCH_NOTIFY_CHANNEL_ID`,
  probado solo con un Riot client simulado) y el logging hacia
  `LOG_CHANNEL_ID`.

## 🧭 Decisiones clave (resumen; detalle en `CLAUDE.md`)
- Región LAS (`la2` / `americas`); todas las colas; remakes excluidos.
- Semana desde el **lunes** (hora local, `TIMEZONE`).
- Job 1×/día a `DAILY_POST_HOUR:DAILY_POST_MINUTE` (23:55 recomendado);
  dedup por `(match_id, puuid)` sin cursor.
- Comandos de administración (`/link-admin`, `/ingest-now`) gateados por
  `DEV_ROLE_ID` + `ADMIN_CHANNEL_ID`.
- Scoring configurable por `config/scoring.yaml`.
