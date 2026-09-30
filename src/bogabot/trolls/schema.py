"""Carga y validación de config/trolls.yaml (umbrales del detector de trolls).

Las reglas que no aparecen en el YAML usan los defaults de `trolls/rules.py`,
así una regla nueva queda activa sin tocar el archivo. Lo que sí aparece se
valida contra el catálogo: una regla o un parámetro con typo falla al
arrancar en vez de dejar al detector callado sin que nadie se entere.
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml

from bogabot.trolls.rules import RULES


class TrollConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuleConfig:
    enabled: bool
    params: dict[str, float]  # defaults del catálogo + lo que pise el YAML


@dataclass(frozen=True)
class TrollConfig:
    troll_level: int  # desde estos puntos se avisa en el canal de trolls
    papelon_level: int  # desde estos puntos es papelón histórico (#general)
    ranked_multiplier: float
    win_multiplier: float
    alert_max_age_hours: float  # partidas más viejas suman al ranking pero no se avisan
    rules: dict[str, RuleConfig]
    # Índice troll (ranking): promedio suavizado de puntos por partida.
    index_prior_games: float = 2.0  # partidas "fantasma" con el promedio del grupo
    index_max_game_points: float = 30.0  # tope de puntos de UNA partida en el índice

    @classmethod
    def default(cls) -> "TrollConfig":
        return _build({})


def load_troll_config(path: str) -> TrollConfig:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise TrollConfigError(f"No encuentro el archivo de trolls en '{path}'.") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TrollConfigError("El archivo de trolls está mal formado (se esperaba un mapa).")
    return _build(raw)


def _number(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrollConfigError(f"'{where}' debe ser un número, no '{value}'.")
    return float(value)


def _build(raw: dict) -> TrollConfig:
    levels = raw.get("levels") or {}
    if not isinstance(levels, dict):
        raise TrollConfigError("'levels' debe ser un mapa con 'troll' y 'papelon'.")
    troll_level = int(_number(levels.get("troll", 6), "levels.troll"))
    papelon_level = int(_number(levels.get("papelon", 18), "levels.papelon"))
    if troll_level < 1:
        raise TrollConfigError("levels.troll debe ser >= 1.")
    if papelon_level <= troll_level:
        raise TrollConfigError("levels.papelon debe ser mayor que levels.troll.")

    ranked_multiplier = _number(raw.get("ranked_multiplier", 1.25), "ranked_multiplier")
    win_multiplier = _number(raw.get("win_multiplier", 0.5), "win_multiplier")
    max_age = _number(raw.get("alert_max_age_hours", 36), "alert_max_age_hours")
    if ranked_multiplier < 0 or win_multiplier < 0 or max_age <= 0:
        raise TrollConfigError("Los multiplicadores no pueden ser negativos y alert_max_age_hours debe ser > 0.")

    index = raw.get("index") or {}
    if not isinstance(index, dict):
        raise TrollConfigError("'index' debe ser un mapa con 'prior_games' y 'max_game_points'.")
    unknown_index = sorted(set(index) - {"prior_games", "max_game_points"})
    if unknown_index:
        raise TrollConfigError(f"Parámetros desconocidos en 'index': {unknown_index}.")
    prior_games = _number(index.get("prior_games", 2), "index.prior_games")
    max_game_points = _number(index.get("max_game_points", 30), "index.max_game_points")
    if prior_games < 0 or max_game_points <= 0:
        raise TrollConfigError("index.prior_games no puede ser negativo e index.max_game_points debe ser > 0.")

    raw_rules = raw.get("rules") or {}
    if not isinstance(raw_rules, dict):
        raise TrollConfigError("'rules' debe ser un mapa regla -> parámetros.")
    unknown = sorted(set(raw_rules) - set(RULES))
    if unknown:
        raise TrollConfigError(f"Reglas desconocidas en trolls.yaml: {unknown}. Existentes: {sorted(RULES)}.")

    rules: dict[str, RuleConfig] = {}
    for code, spec in RULES.items():
        overrides = raw_rules.get(code) or {}
        if not isinstance(overrides, dict):
            raise TrollConfigError(f"La regla '{code}' debe ser un mapa de parámetros.")
        params = dict(spec.params)
        enabled = True
        for key, value in overrides.items():
            if key == "enabled":
                enabled = bool(value)
            elif key in spec.params:
                params[key] = _number(value, f"rules.{code}.{key}")
            else:
                raise TrollConfigError(
                    f"Parámetro desconocido '{key}' en la regla '{code}'. "
                    f"Válidos: {sorted(['enabled', *spec.params])}."
                )
        rules[code] = RuleConfig(enabled=enabled, params=params)

    return TrollConfig(
        troll_level=troll_level,
        papelon_level=papelon_level,
        ranked_multiplier=ranked_multiplier,
        win_multiplier=win_multiplier,
        alert_max_age_hours=max_age,
        rules=rules,
        index_prior_games=prior_games,
        index_max_game_points=max_game_points,
    )
