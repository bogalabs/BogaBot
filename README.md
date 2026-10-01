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
├── trolls/                 # detector de trolls (lógica pura)
│   ├── rules.py            # catálogo de reglas (feeder, FF al 15, AFK...)
│   ├── schema.py           # carga/valida config/trolls.yaml
│   └── detector.py         # corre las reglas -> cargos, puntos y nivel
├── carries/                # catálogo de carreadas (mismo motor que trolls/)
│   └── rules.py
└── modules/lol/            # feature LoL (un "cog" autocontenido)
    ├── cog.py              # /link /unlink /link-admin /ingest-now /ranking /trolls ...
    ├── ingest.py           # ingesta de partidas (+ timeline para el detector)
    ├── ranking.py          # agrega stats + arma embeds
    ├── trolls.py           # ranking (troll/carry) + mensajes; los textos van en un Flavor
    ├── carries.py          # textos de las carreadas (mismo servicio que trolls)
    └── scheduler.py        # job diario + chequeo de partidas nuevas + avisos
config/scoring.yaml         # fórmula del ranking, editable sin tocar código
config/trolls.yaml          # umbrales y puntos del detector de trolls
config/carries.yaml         # umbrales y puntos del detector de carreadas
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
copy .env.example .env.staging
# Editá .env.staging y completá los valores (ver guía abajo).

# 4. Correr (BOGABOT_ENV default = staging si no lo seteás)
python run.py
```

Ver [Ambientes: staging vs. production](#ambientes-staging-vs-production) para
correr contra producción o entender cómo elige el bot qué archivo cargar.

## Guía rápida: correr tu propio bot en tu propio server

Este bot es de uso libre — cualquiera puede clonar el repo y levantarlo en su
propio server de Discord, con su propio bot y sus propios tokens. Pasos, en
orden:

### 1. Crear el bot en Discord
1. Andá a [Discord Developer Portal](https://discord.com/developers/applications)
   → **New Application**, ponele un nombre.
2. Pestaña **Bot** → **Reset Token** → copiá el token. Ese valor va en
   `DISCORD_TOKEN`. Guardalo ya, Discord no lo vuelve a mostrar.
3. En esa misma pestaña, dejá los **Privileged Gateway Intents** apagados —
   el bot no los necesita.

### 2. Invitar el bot a tu server
1. Pestaña **OAuth2 → URL Generator**.
2. Scopes: `bot` y `applications.commands`.
3. Bot Permissions: `Send Messages`, `Read Message History`, `Embed Links`,
   `View Channel` y `Manage Roles` (esta última solo si vas a usar
   `LOL_ROLE_ID`, para que el bot pueda asignarlo automáticamente).
4. Copiá la URL generada al final de la página, abrila y elegí tu server.

### 3. Activar el modo desarrollador (para copiar IDs)
En Discord: **Configuración de usuario → Avanzado → Modo de desarrollador**.
Con esto activado, click derecho sobre cualquier canal/rol/servidor te
muestra la opción "Copiar ID".

### 4. Crear los canales y roles
En tu server, creá (y después copiá el ID de cada uno con click derecho):

| Creá esto | Tipo | Va en |
|---|---|---|
| Un canal de texto **privado** (que solo vea el bot, nadie más escribe ahí) | canal | `STORAGE_CHANNEL_ID` |
| Un canal público para los rankings | canal | `RANKING_CHANNEL_ID` |
| El canal general del server (ahí van los papelones históricos) | canal | `GENERAL_CHANNEL_ID` |
| *(Opcional)* Canal de alertas troll (si no, van al de rankings) | canal | `TROLL_CHANNEL_ID` |
| *(Opcional)* Canal de avisos de partida terminada | canal | `MATCH_NOTIFY_CHANNEL_ID` |
| *(Opcional)* Canal de logs del bot | canal | `LOG_CHANNEL_ID` |
| *(Opcional)* Canal donde correr comandos de admin | canal | `ADMIN_CHANNEL_ID` |
| Un rol para quien administra el bot | rol | `DEV_ROLE_ID` |
| *(Opcional)* Un rol para jugadores/miembros del grupo | rol | `PLAYER_ROLE_ID` |
| *(Opcional)* Un rol que se asigna solo al vincularse | rol | `LOL_ROLE_ID` |

También necesitás el ID del server (click derecho sobre el ícono del server
→ Copiar ID) para `DISCORD_GUILD_ID`.

> 💡 El canal de `STORAGE_CHANNEL_ID` funciona como "base de datos" del bot
> (ver [Arquitectura](#arquitectura-por-capas-cada-una-reemplazable) arriba)
> — no debería verlo ni escribir en él nadie más que el bot.

### 5. Conseguir la API key de Riot
[developer.riotgames.com](https://developer.riotgames.com/) → **Register
Product → Personal API Key**. Es la que usa el bot en producción: gratis,
pensada para proyectos chicos como este y **no vence** (Riot la aprueba a
mano, puede tardar unos días). Va en `RIOT_API_KEY`, con
`RIOT_KEY_TTL_HOURS=0` para que el bot no avise vencimientos.

Para probar mientras tanto sirve la **Development API Key** del dashboard,
pero vence cada 24h: dejá `RIOT_KEY_TTL_HOURS=24` (default) y el bot avisa
antes de que venza; la nueva se carga con `/riot-key` sin reiniciar. Ajustá también `RIOT_PLATFORM`/`RIOT_REGION` según la región
de tu grupo (ver tabla de variables abajo).

### 6. Completar el `.env.staging` (o `.env.production`) y correr
Con todos los IDs y tokens, completá el archivo del ambiente que corresponda
(ver la tabla completa de variables más abajo, y la sección de
[ambientes](#ambientes-staging-vs-production)) y corré `python run.py`. Al
primer arranque el bot registra los slash commands en tu server (instantáneo
si pusiste `DISCORD_GUILD_ID`).

### Variables de entorno (.env.staging / .env.production)

| Variable | Descripción |
|---|---|
| `DISCORD_TOKEN` | Token del bot. |
| `DISCORD_GUILD_ID` | ID del server (opcional; registra los comandos al instante). |
| `STORAGE_CHANNEL_ID` | Canal privado que el bot usa como base de datos. |
| `RANKING_CHANNEL_ID` | Canal donde publica los rankings. |
| `GENERAL_CHANNEL_ID` | Canal general: ahí van los **papelones históricos** del detector de trolls. |
| `TROLL_CHANNEL_ID` | Canal de alertas troll y ranking troll (opcional; default `RANKING_CHANNEL_ID`). |
| `TROLLS_CONFIG_PATH` | Config del detector de trolls (default `config/trolls.yaml`). |
| `CARRY_CHANNEL_ID` | Canal de avisos de carreadas (opcional; default el canal de trolls). |
| `CARRIES_CONFIG_PATH` | Config del detector de carreadas (default `config/carries.yaml`). |
| `CARRIES_STATE_FILE` | Desde cuándo cuenta el ranking de carreadas (`/carries-reiniciar`; default `data/carries_state.json`). |
| `TROLLS_STATE_FILE` | Desde cuándo cuenta el ranking troll, lo escribe `/trolls-reiniciar` (default `data/trolls_state.json`). |
| `DEV_ROLE_ID` | Rol habilitado para comandos de administración (`/link-admin`). |
| `ADMIN_CHANNEL_ID` | Canal donde se pueden correr esos comandos (opcional). |
| `PLAYER_ROLE_ID` | Rol de jugador/miembro; solo afecta qué ve `/help` y `/ayuda`. |
| `MATCH_NOTIFY_CHANNEL_ID` | Canal donde se avisa cuando termina una partida (opcional). |
| `MATCH_POLL_INTERVAL_MINUTES` | Cada cuántos minutos se chequean partidas nuevas (para ese aviso y las alertas troll). |
| `RIOT_API_KEY` | API key de Riot. En producción, la **Personal API Key** (no vence); la dev key vence cada 24h. Se puede rotar en caliente con `/riot-key`. |
| `RIOT_KEY_FILE` | Dónde se guarda la key cargada con `/riot-key` (default `data/riot_key.json`). |
| `RIOT_KEY_TTL_HOURS` | Horas de vida de la key para el recordatorio (default `24`, para la dev key; con la Personal API Key poné `0` = no vence, sin recordatorio). |
| `RIOT_KEY_WARN_MINUTES` | Cuántos minutos antes de vencer se avisa (default `120`). |
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

## Ambientes: staging vs. production

El bot corre en dos ambientes posibles, elegidos por la variable de entorno
**`BOGABOT_ENV`** (la seteás en la terminal/shell antes de correr `python
run.py`, no dentro del `.env`):

| `BOGABOT_ENV` | Archivo que carga | Uso |
|---|---|---|
| _(sin setear)_ o `staging` | `.env.staging` | Default. Desarrollo y pruebas del equipo. |
| `production` | `.env.production` | El bot real, en el server real del grupo. |

El default es **staging** a propósito: si te olvidás de setear la variable,
nunca corrés contra producción por accidente. Ninguno de los dos archivos se
commitea (están en `.gitignore`); `.env.production` solo lo tiene quien
administra el bot en vivo.

Recomendado: **staging apunta a otro bot de Discord (otro token, otra
Application) invitado a otro server**, no al mismo. El storage del bot vive
en canales de Discord (`STORAGE_CHANNEL_ID`), así que si staging y
production comparten server/canales terminás mezclando datos de prueba con
datos reales del grupo.

Formas de correrlo:

```powershell
# Local, directo (default staging)
python run.py

# Local, explícito
$env:BOGABOT_ENV = "staging"
python run.py

# Con el script (levanta consola + log en logs\<ambiente>\)
.\scripts\run_bot.ps1                              # staging
.\scripts\run_bot.ps1 -Environment production       # pide confirmación
.\scripts\run_bot.ps1 -Environment production -Force  # sin confirmar (autorun)
```

**Importante (por ahora corremos todo local, no en VPS):** no levantar dos
instancias del bot al mismo tiempo apuntando al mismo ambiente/server — te
respondería el comando dos veces. Si dos personas quieren tocar código a la
vez, cada una con su propio server de prueba personal (su propio
`.env.staging`), y validar contra el staging "oficial" del equipo antes de
mergear a `main`.

## Producción: el server del grupo

Producción corre en un server Debian propio (`lautiserver`), no en la VPS
del workflow `.github/workflows/deploy.yml` (ese deploy automático a `main`
queda para cuando haya VPS). Así está armado hoy:

| Qué | Dónde |
|---|---|
| Código | `/home/lautiserver/BogaBot` (clon de este repo, rama que esté en producción) |
| Servicio | `bogabot.service` (systemd, usuario `lautiserver`, `BOGABOT_ENV=production`) |
| Config/secretos | `.env.production` (600, solo en el server) + `data/` (puntos, `riot_key.json`) |
| Logs | `journalctl -u bogabot -f` (y los `WARNING`+ en el canal de logs de Discord) |

Deploy manual (desde el server):

```bash
cd /home/lautiserver/BogaBot
git pull --ff-only origin <rama>
venv/bin/pip install -r requirements.txt   # solo si cambió requirements.txt
sudo systemctl restart bogabot
journalctl -u bogabot -n 50 --no-pager     # verificar que levantó
```

Cambiar la key de Riot no requiere deploy: `/riot-key <key>` desde Discord.
El acceso SSH y las herramientas de operación del server están en el proyecto
aparte `sshserver` (fuera de este repo).

## Comandos
- `/link <Nombre#TAG>` — vincula tu cuenta de Riot (valida contra la API).
- `/unlink` — desvincula tu cuenta.
- `/link-admin <usuario> <Nombre#TAG>` — vincula la cuenta de Riot de otro
  usuario del server. Requiere el rol `DEV_ROLE_ID` y, si está configurado,
  correrse en el canal `ADMIN_CHANNEL_ID`.
- `/ranking [Hoy|Semana]` — muestra el ranking on-demand.
- `/trolls [Semana|Semana pasada|Mes|Histórico]` — ranking troll por **índice**
  (puntos troll promedio por partida, así jugar mucho no pesa)
  (quién trolleó más, su "especialidad" y su peor partida).
- `/troll-analizar [usuario] [partida]` — muestra los cargos troll de una
  partida (por defecto la última guardada) y por qué suma o no. Sirve para
  calibrar los umbrales.
- `/trolls-reglas` — qué detecta el bot y cuántos puntos suma cada cosa.
- `/carries`, `/carry-analizar`, `/carries-reglas`, `/carries-reiniciar` — lo mismo
  que los de trolls, para las carreadas (ver [Carreadas](#carreadas-)).
- `/trolls-reiniciar` — (solo rol dev, en `ADMIN_CHANNEL_ID`) el ranking troll
  arranca de cero desde ahora (las partidas viejas quedan guardadas).
- `/trolls-recalcular` — (solo rol dev, en `ADMIN_CHANNEL_ID`; igual corre solo al arrancar) vuelve a
  pedir a Riot las partidas guardadas antes del detector nuevo para
  completarles las stats y el timeline. Corre en segundo plano.
- `/help` y `/ayuda` — listan los comandos disponibles según el rol de quien
  los usa (rol `DEV_ROLE_ID` ve administración, rol `PLAYER_ROLE_ID` ve los
  comandos de ranking). Ambos hacen lo mismo, son solo dos nombres.
- `/ingest-now` — fuerza una ingesta de partidas manual (solo rol dev). Es
  idempotente: correrlo varias veces no duplica nada, el dedup por
  (match_id, discord_id) saltea lo que ya está guardado.
- `/riot-key [key]` — (solo rol dev, en `ADMIN_CHANNEL_ID`) valida y aplica
  una RIOT_API_KEY nueva **sin reiniciar** el bot, y la guarda en
  `RIOT_KEY_FILE` para que sobreviva reinicios. Sin `key`, muestra cuándo
  vence la actual. La respuesta es efímera: la key no queda visible. Si
  después alguien cambia `RIOT_API_KEY` en el `.env`, esa pasa a mandar.
  Además, el bot avisa en `ADMIN_CHANNEL_ID` (etiquetando al rol dev)
  `RIOT_KEY_WARN_MINUTES` antes de que venza y cuando vence.

## Avisos "en vivo" y logs

- **Partida terminada:** si `MATCH_NOTIFY_CHANNEL_ID` está seteado, cada
  `MATCH_POLL_INTERVAL_MINUTES` (5 por defecto) el bot chequea partidas
  nuevas y, por cada una que cuente (jugada con otro vinculado), postea un
  mensaje etiquetando a los jugadores del grupo que la jugaron — con el
  resultado de cada uno, por si terminaron en equipos contrarios.
  El aviso incluye un **troll-o-metro** con los puntos troll de cada uno.
  Los avisos salen de cualquier ingesta (el chequeo periódico, el job
  diario o `/ingest-now`), una sola vez por partida.
- **Logs del bot:** si `LOG_CHANNEL_ID` está seteado, los logs de nivel
  `LOG_CHANNEL_LEVEL` (WARNING por defecto) o superior se reenvían también a
  ese canal, además de la consola. Pensado para enterarte de errores (Riot
  caído, key vencida, etc.) sin tener que mirar la terminal.

## Detector de trolls 🤡

Después de cada partida, cada jugador del grupo se juzga con ~25 reglas
(`src/bogabot/trolls/rules.py`). Situaciones que detecta:

- **Del timeline (qué pasó y cuándo):** "nos tiraban la base y estaba
  farmeando" (inhibidores/torres del nexo cayendo mientras él, vivo, estaba
  lejos), *throw* (murió primero y enseguida perdieron Barón, Ancestral o
  el nexo), AFK (minutos quieto sin ganar experiencia), primera sangre
  regalada, muertes antes del minuto 10, línea perdida por oro al 15,
  "delivery" al rival de línea, ejecutado por torres/minions, items
  vendidos (inteo), morir con la plata encima.
- **De las stats de la partida:** feeder, KDA trágico, 0 kills y 0
  asistencias, pacifista, poco daño o participación, visión nula, cero
  control wards, farm bajo, mucho tiempo muerto, "ancla" del equipo, FF
  antes del 20, barrida en kills, spam de pings de "?", último en Arena.

Cada cargo suma puntos troll; en ranked se multiplican y si igual ganaron se
achican. Los umbrales cambian según el modo (ARAM y modos caóticos toleran
más muertes; las reglas de línea y de base son solo de la Grieta).

Criterios para no castigar juego normal:
- Las muertes se miden **según lo que duró la partida** (10 en 20 min no es
  lo mismo que en 45).
- La primera sangre solo cuenta si fue **temprano** (antes del minuto 5).
- "Delivery" exige que la mayoría de sus muertes sean contra su rival de línea.
- El **tanque** que absorbe daño no cuenta como "poco daño", y el que carrea
  con daño no cuenta como "pacifista" ni "poco farm".
- Sin control wards solo cuenta si además la visión fue floja.
- El FF y la barrida son **culpa del equipo**: solo agravan si el jugador ya
  tiene un cargo propio.
- "Nos tiraban la base" no cuenta si estaba haciendo *split push*.
- "Throw" es solo si lo agarraron **solo**, sin pelea alrededor.
- "Ahorrista" solo mira muertes antes del minuto 25.
- Reglas que miden lo mismo no se suman (feeder reemplaza a KDA trágico y a
  tiempo muerto; ciego reemplaza a sin control wards).

**Una partida floja no es una trolleada.** Los cargos "menores" (mal
rendimiento: poca KP, poco daño, línea perdida, visión, farm, primera
sangre, FF...) suman entre todos como mucho `weak_points_cap` (4). Para ser
trolleada hace falta una **señal fuerte**: feeder, AFK, 0 kills y 0
asistencias, ancla del equipo, vender items, dejar caer la base o throw.

| Puntos de la partida | Qué pasa |
|---|---|
| menos de `levels.troll` (8) | Nada aparte; suma al ranking troll y se ve en el troll-o-metro del aviso de partida. |
| `levels.troll`+ | 🤡 **Trolleada**: en `TROLL_CHANNEL_ID` (o el de rankings), una línea con la anécdota etiquetando al jugador + el detalle compacto. |
| `levels.papelon`+ (15) | 💀 **Papelón**: además, **una línea corta en `GENERAL_CHANNEL_ID`**: *"¡Trolleada histórica! @jugador nos tiraban la base y estaba farmeando la jungla. Encima, murió 12 veces. (Lee Sin 1/12/2)"*. Solo esto va a #general. |

Solo se avisan partidas recientes (`alert_max_age_hours`, 36 h). Todo se
ajusta en **`config/trolls.yaml`** sin tocar código; el ranking troll se
calcula al vuelo, así que un cambio ahí recalcula también el historial.

**Ranking troll (`/trolls`)**: ordena por **índice troll** (puntos por
partida), no por el total, así que jugar más no suma: el que la trollea
fuerte en 2 partidas queda arriba del que jugó 18 y trolleó 2. Para que 1
partida suelta no decida por azar, el índice se suaviza hacia el promedio
del grupo (`index.prior_games`) y una sola partida cuenta como mucho
`index.max_game_points`. Muestra una categoría (😇 Santo → 💀 Leyenda
troll) y la tendencia contra el período anterior. `/trolls-reiniciar`
(dev) lo pone en cero desde ese momento (se guarda en `TROLLS_STATE_FILE`).
El job diario postea cómo va la semana si hubo trolleadas y los lunes
corona al **Troll de la semana**.

Las reglas del timeline usan una consulta más a Riot por partida. Las
partidas guardadas antes (o analizadas con una versión vieja) **se
recalculan solas y en silencio** al arrancar el bot: sin mensajes, sin logs
visibles y sin avisos; lo único que cambia es la tabla troll.
`/trolls-recalcular` fuerza lo mismo a mano (responde solo a quien lo pide); con
`desde_cero: True` reanaliza **todas** las partidas guardadas y la tabla vuelve a
contar todo el historial (deshace `/trolls-reiniciar`).

## Carreadas ⭐

El mismo sistema que los trolls, pero premiando (`src/bogabot/carries/rules.py`,
configurable en **`config/carries.yaml`**). Criterio: una carreada es
**impacto relativo a tu equipo**, no un KDA lindo en una partida fácil (el
viejo "ganó + KDA ≥ 5" del ranking general contaba cualquier stomp).

- **Jugadas fuertes:** pentakill, % enorme del daño del equipo, muchas kills
  para lo que duró la partida *y* buena parte de las del equipo, racha
  legendaria, no morir nunca estando en las peleas, remontada siendo clave,
  robar Barón/dragón, muralla (tanque que absorbe y está en todas),
  habilitador (support en casi todas las kills), ganar la Arena.
- **Jugadas menores** (con tope de 4 pts entre todas): KDA limpio, KP alta,
  ganar la línea, duelos 1v1, primera sangre, visión, farm.
- Perder pesa la mitad; ranked ×1.25.

| Puntos | Qué pasa |
|---|---|
| `levels.carry`+ (8) | ⭐ **Carreada**: en `CARRY_CHANNEL_ID` (default: el canal de trolls) una línea con la anécdota + el detalle. |
| `levels.legendaria`+ (15) | 🌟 **Legendaria**: además, una línea en `GENERAL_CHANNEL_ID`. |

`/carries` ordena por índice (puntos por partida, suavizado, como `/trolls`),
`/carry-analizar` explica una partida, `/carries-reglas` muestra el
reglamento y `/carries-reiniciar` (dev) pone la tabla en cero. Los lunes se
corona al **Carry de la semana**. El aviso de partida suma un ⭐ Carry-o-metro.

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
