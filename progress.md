# Progress & Memoria del Agente

Este archivo sirve para mantener un registro sincronizado de los avances y de las reglas de negocio más importantes de nuestro bot de Discord para League of Legends, BogaBot.

## Historial Reciente
- **V1.0 - Setup inicial**: Inicialización del bot y comando básico de ranking diario/semanal.
- **Feature/troll-ranking**: Se añadieron detecciones de comportamiento en las partidas. Ahora el bot expone e interactúa con ciertos patrones de las partidas.
- **Merge & Fixes**: Se fusionaron los cambios de `main` (notificaciones de partidas terminadas) con la rama de trolleadas y papelones, y se sumó la nueva detección de Carreadas.
- **V2.0 - Sistema de Carreadas/Troleadas y filtro de colas**: Revisión completa del scoring. Los rankings diario/semanal ahora solo cuentan partidas Ranked (Flex + Solo/Duo). La fórmula del ranking prioriza carries y trolls como métricas dominantes. Se redefinió `is_troll_game` para que dependa solo del KDA (sin requerir FF ni derrota).

## Documentación de Nuevos Objetos / Eventos (Models)

En `src/bogabot/core/models.py`, el modelo `MatchRecord` tiene lógica central para identificar eventos especiales en las partidas de LoL de nuestros jugadores:

### 🚨 Troll Game (`is_troll_game`)
- **Regla**: KDA menor a 0.5, **sin importar si ganó o perdió** ni si hubo surrender. Si jugaste pésimo, es troll aunque tu equipo haya ganado.
- **Acción**: Alerta roja en el canal de ranking anunciando la trolleada (mostrando si ganó o perdió), junto a un leaderboard histórico de trolls (`troll_counts`).

### 📉 Papelón (`is_papelon`)
- **Regla**: El jugador pierde la partida, y la duración total de la partida es menor a 25 minutos (1500 segundos).
- **Acción**: Mensaje breve en el canal general notificando el papelón por haber perdido tan rápido.

### 🔥 Carreada (`is_carry_game`)
- **Regla**: El jugador **gana** la partida con un **KDA ≥ 5** (ej: 10/2/5 = KDA 7.5).
- **Acción**: Alerta 🔥 en el canal de ranking felicitando al jugador, junto a un leaderboard histórico de carries (`carry_counts`).

## Scoring y Rankings

### Fórmula del Ranking (`config/scoring.yaml`)
El puntaje se calcula con métricas normalizadas (zscore). **Todas las métricas son promedios o ratios por partida** — jugar muchas partidas mediocres no te sube el ranking. Pesos actuales:
- `carry_rate`: **5.0** — % de partidas carreadas (la más dominante)
- `troll_rate`: **4.0** — % de partidas troleadas (penaliza, higher_is_better: false)
- `kda`: 3.0
- `win_rate`: 2.0
- `avg_deaths`: 1.5 (penaliza)
- `damage_per_min`: 1.0
- `avg_vision`: 0.5

### Filtro de Colas
- **Rankings diario/semanal**: solo Ranked Flex (440) + Ranked Solo/Duo (420). Normals, ARAM y modos rotativos **no cuentan**.
- **Trolls y Pros (recap semanal del lunes)**: solo Ranked Flex (440).
- **Rankings históricos (trolls/carries)**: todas las partidas registradas.
- **Constantes**: `RANKED_QUEUE_IDS` en `riot/mapper.py`.

### Embeds
Los embeds de ranking ahora muestran 🔥carries y 🤡trolls por jugador. Los footers dicen "Solo cuentan partidas Ranked (Flex/Solo) jugadas con al menos otro vinculado del grupo."
