"""Frases habladas (TTS) para cuando termina una partida con trolleada o
carreada: el bot entra al canal de voz de los que jugaron y dice algo sobre
quién fue. Lógica pura (sin Discord ni red) para poder testearla.

`pick_spoken_verdict` elige de qué partida/jugador hablar y `build_line`
arma el texto; la síntesis de voz está en `tts.py`. `build_start_line` es el
aviso de partida arrancada (para que vayan a apostar).
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

from bogabot.core.models import TrollLevel, TrollVerdict

TROLL = "troll"
CARRY = "carry"

# {name}, {champ}, {detail} (el cargo/jugada principal, ya en castellano).
_LINES: dict[tuple[str, TrollLevel], tuple[str, ...]] = {
    (TROLL, TrollLevel.TROLL): (
        "Atención Boga: {name} acaba de trollear con {champ}. {detail}. Qué vergüenza.",
        "Se viene el reporte. {name} con {champ}: {detail}.",
        "Señoras y señores, {name} jugó de {champ} y {detail}. Un aplauso para el equipo enemigo.",
        "Momento de silencio por la partida de {name}. {detail}, con {champ}.",
    ),
    (TROLL, TrollLevel.PAPELON): (
        "Papelón histórico. {name} con {champ}: {detail}. Esto queda en los anales de la Boga.",
        "Alerta máxima. {name} hizo un papelón con {champ}. {detail}. Desinstalá, por favor.",
        "Paren todo. {name} con {champ}: {detail}. Nunca vi algo igual.",
    ),
    (CARRY, TrollLevel.TROLL): (
        "Aplausos para {name}, que se carreó la partida con {champ}. {detail}.",
        "{name} se puso el equipo al hombro con {champ}: {detail}. Los demás, de mochila.",
        "Qué partidón de {name} con {champ}. {detail}.",
    ),
    (CARRY, TrollLevel.PAPELON): (
        "Carreada legendaria. {name} con {champ}: {detail}. Esto se enmarca.",
        "De pie, Boga. {name} jugó una partida para el museo con {champ}. {detail}.",
    ),
}


@dataclass(frozen=True)
class SpokenVerdict:
    kind: str  # TROLL o CARRY
    verdict: TrollVerdict


def pick_spoken_verdict(trolls: list[TrollVerdict], carries: list[TrollVerdict]) -> SpokenVerdict | None:
    """De qué hablar en una partida (veredictos de los vinculados que la
    jugaron, ya filtrados de inmunidades). Solo lo que llega a aviso
    (nivel TROLL o más); manda el de mayor nivel y, a igual nivel, el papelón
    le gana a la carreada (es lo más gracioso) y después los puntos."""
    candidates = [SpokenVerdict(TROLL, v) for v in trolls if v.level >= TrollLevel.TROLL]
    candidates += [SpokenVerdict(CARRY, v) for v in carries if v.level >= TrollLevel.TROLL]
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c.verdict.level, c.kind == TROLL, c.verdict.points))


def spoken_name(raw: str) -> str:
    """Nombre pronunciable: sin emojis ni símbolos raros, sin el #tag de Riot."""
    name = raw.split("#")[0]
    name = re.sub(r"[^\w\s'-]", " ", name)
    name = re.sub(r"[_\s]+", " ", name).strip()
    return name or "alguien"


def build_line(spoken: SpokenVerdict, name: str, rng: random.Random | None = None) -> str:
    """El texto a decir. `name` ya pronunciable (ver `spoken_name`)."""
    rng = rng or random.Random()
    v = spoken.verdict
    level = TrollLevel.PAPELON if v.level >= TrollLevel.PAPELON else TrollLevel.TROLL
    top = max(v.flags, key=lambda f: f.points, default=None)
    detail = top.detail if top is not None else f"terminó {v.record.kills}, {v.record.deaths}, {v.record.assists}"
    template = rng.choice(_LINES[(spoken.kind, level)])
    return template.format(name=name, champ=v.record.champion or "su campeón", detail=detail)


def join_names(names: list[str]) -> str:
    """"A", "A y B", "A, B y C"."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} y {names[-1]}"


def build_start_line(names: list[str], queue: str, bets_channel: str | None) -> str:
    """"Lauti, Nacho y Pepo arrancaron una partida de Ranked Flex. Hagan sus
    apuestas en commands." `names` y `bets_channel` ya pronunciables."""
    line = f"{join_names(names)} arrancaron una partida de {queue}."
    if bets_channel:
        line += f" Hagan sus apuestas en {bets_channel}."
    return line
