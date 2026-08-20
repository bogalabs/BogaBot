# Agente de devllo — addendum

> Este archivo complementa a `CLAUDE.md` (raíz), que tiene la arquitectura,
> convenciones y reglas del proyecto. Leé ese primero.

## Quién es
- **Rol:** colaborador/desarrollador del proyecto.
- **Identidad git:** _(completar: usuario y email de git de devllo)_.

## Entorno
- **SO / terminal:** _(completar: Windows/macOS/Linux; PowerShell/bash)_.
  Ajustar los comandos de la doc a ese entorno si hace falta.

## Cómo le gusta trabajar
- Comunicación en **español**.
- _(completar preferencias: nivel de detalle, si prefiere que se le pregunte
  antes de cambios grandes, estilo de PRs, etc.)_

## Foco / responsabilidades actuales
- _(completar: qué parte del proyecto va a llevar devllo — ej. cliente de Riot,
  motor de scoring, storage, comandos, tests…)_

## Recordatorios para el agente
- **No** agregar agentes de IA como co-autor en commits/PRs/comentarios.
- **No** hacer commits ni push salvo pedido explícito.
- Respetar la separación por capas: no importar implementaciones concretas de
  storage fuera de `bot.py`; todo contra las interfaces de `storage/base.py`.
