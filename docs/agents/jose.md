# Agente de José (joseGiraudo) — addendum

> Este archivo complementa a `CLAUDE.md` (raíz), que tiene la arquitectura,
> convenciones y reglas del proyecto. Leé ese primero.

## Quién es
- **Rol:** dueño del proyecto / arquitecto. Toma las decisiones de producto y
  de diseño, define los criterios del ranking y administra el server de Discord.
- **Identidad git:** `joseGiraudo` (josegiraudo7@gmail.com).

## Entorno
- **SO:** Windows 11.
- **Terminal:** PowerShell / CMD. Usar sintaxis de PowerShell y activación de
  venv con `venv\Scripts\activate`.

## Cómo le gusta trabajar
- Comunicación en **español**.
- Antes de escribir código para algo grande: **proponer estructura y explicar
  decisiones de diseño**, y hacer las preguntas necesarias para destrabar.
- Priorizar que el **MVP funcione end-to-end** antes de pulir detalles.
- Ser honesto sobre qué quedó **verificado** vs. qué falta probar en vivo.

## Foco / responsabilidades actuales
- Config de Discord (crear canales, IDs, permisos e invitación del bot).
- Riot API key y su renovación (la dev key vence cada 24 h).
- Definir/ajustar la fórmula del ranking en `config/scoring.yaml`.
- Decisiones sobre features futuras (módulo de IA, nuevas fuentes de datos).

## Recordatorios para el agente
- **No** agregar agentes de IA como co-autor en commits/PRs/comentarios.
- **No** hacer commits ni push salvo pedido explícito.
