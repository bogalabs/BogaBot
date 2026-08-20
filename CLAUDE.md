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
│   └── timeutils.py        # cortes de día/semana según TIMEZONE
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
    ├── cog.py              # slash commands /link /unlink /ranking
    ├── ingest.py           # ingesta de partidas
    ├── ranking.py          # agrega stats + arma embeds
    └── scheduler.py        # job diario (discord.ext.tasks)
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
- **Agregar una feature nueva (no-LoL, ej. módulo de IA):** carpeta nueva en
  `modules/`, con su cog, y registrarla en `bot.py::setup_hook`. No tocar LoL.
- **Cambiar el backend de datos:** nueva clase en `storage/` que implemente
  `LinkRepository`/`MatchRepository`; swap en `bot.py`.

## Decisiones ya tomadas (no re-litigar sin motivo)

- **Región:** LAS → `RIOT_PLATFORM=la2`, `RIOT_REGION=americas` (routing de
  account-v1 y match-v5).
- **Colas contadas:** todas (incluye ARAM/rotativos). Se **excluyen remakes**
  (< 5 min o early surrender) en `riot/mapper.py`.
- **Semana = domingo 00:00 hora local** (`TIMEZONE`, default Buenos Aires).
- **Job 1×/día** a `DAILY_POST_HOUR`: ingesta → ranking diario; los domingos,
  recap semanal "Trolls y Pros" de la semana que cerró.
- **Dedup por `(match_id, puuid)`**, sin cursor: cada corrida pide "desde el
  domingo" y saltea lo ya guardado.
- **Storage actual = Discord** (mensajes JSON en canal privado + índice en
  memoria hidratado al arrancar). Es O(n) mensajes; migrar a DB real cuando
  crezca (la interfaz lo hace trivial).

## Correr y testear (Windows / PowerShell)

```powershell
venv\Scripts\activate
pip install -r requirements.txt      # primera vez
python run.py                        # requiere .env completo
python -m unittest discover -s tests -v
```

Setup completo y tabla de variables: ver `README.md`.
