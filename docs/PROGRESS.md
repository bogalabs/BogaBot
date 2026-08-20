# Avances y faltantes — BogaBot

Documento **vivo**: actualizarlo a medida que avanza el proyecto. Marcar con
`[x]` lo hecho y mover items entre secciones. Poner la fecha en cada cambio.

_Última actualización: 2026-08-19_

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
- [x] Scheduler diario (`discord.ext.tasks`) con recap semanal los domingos — 2026-08-19
- [x] Composition root (`bot.py`) + bootstrap (`main.py`, `run.py`) — 2026-08-19
- [x] venv + `requirements.txt` instalados y verificados — 2026-08-19
- [x] Tests de scoring y dedup en verde (`tests/test_scoring.py`) — 2026-08-19
- [x] Docs: `README.md`, `CLAUDE.md`, `docs/agents/`, este archivo — 2026-08-19

## 🚧 En progreso
- [ ] _(nada activo)_

## ⛔ Faltante / próximos pasos

### Bloqueante para probar en vivo (lo hace José)
- [ ] Completar `.env` (token, IDs de canal, Riot API key).
- [ ] Crear canal privado de storage y canal de rankings; copiar sus IDs.
- [ ] Invitar el bot con scope `applications.commands` y permisos de lectura/
      escritura + leer historial en ambos canales.
- [ ] Primera corrida real: `/link` → ingesta → `/ranking` → posteo automático.

### Mejoras de producto/DX
- [ ] Comando `/ingest-now` (o `/refresh`) para forzar ingesta a mano y no
      esperar al job diario (útil para testear el flujo completo).
- [ ] Comando para ver/editar el estado del propio vínculo (`/whoami`).
- [ ] Paginado/estrategia si el canal de storage supera el `HYDRATE_LIMIT`.
- [ ] Manejo de Riot IDs que cambian de nombre (re-resolver puuid).

### Robustez
- [ ] Reintentos/backoff más finos ante errores transitorios de Riot.
- [ ] Tests del mapper (con un JSON de match-v5 de ejemplo).
- [ ] Aviso en el canal cuando la Riot API key expira (401).

## 🗺️ Backlog / futuro (fuera del MVP)
- [ ] Migrar storage de Discord a una DB real (SQLite → Postgres/Firebase).
      La interfaz Repository ya lo deja listo: solo cambia `bot.py`.
- [ ] Módulo de chat conversacional con IA (como cog aparte).
- [ ] Nuevas fuentes de datos / features no relacionadas a LoL.
- [ ] Estadísticas históricas y tendencias (más allá de día/semana).

## ⚠️ Verificado vs. no verificado
- **Verificado:** instalación de deps, import de los 14 módulos, validación de
  `scoring.yaml`, lógica de scoring y dedup (tests).
- **No verificado aún:** flujo real contra Discord y Riot (requiere tokens y
  correr `python run.py`).

## 🧭 Decisiones clave (resumen; detalle en `CLAUDE.md`)
- Región LAS (`la2` / `americas`); todas las colas; remakes excluidos.
- Semana desde el domingo (hora local, `TIMEZONE`).
- Job 1×/día; dedup por `(match_id, puuid)` sin cursor.
- Scoring configurable por `config/scoring.yaml`.
