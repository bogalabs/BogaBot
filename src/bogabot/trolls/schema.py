"""Carga y validación de config/trolls.yaml (umbrales del detector de trolls).

Las reglas que no aparecen en el YAML usan los defaults de `trolls/rules.py`,
así una regla nueva queda activa sin tocar el archivo. Lo que sí aparece se
valida contra el catálogo: una regla o un parámetro con typo falla al
arrancar en vez de dejar al detector callado sin que nadie se entere.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from bogabot.trolls.rules import RULES, STORY_PRIORITY, RuleSpec


class TrollConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuleConfig:
    enabled: bool
    params: dict[str, float]  # defaults del catálogo + lo que pise el YAML


@dataclass(frozen=True)
class Catalog:
    """Un juego de reglas con sus defaults. El mismo motor sirve para trolls
    (castigos) y para carreadas (premios, ver `bogabot/carries/`)."""

    name: str  # para los mensajes de error ("trolls", "carreadas")
    rules: dict[str, RuleSpec]
    level_names: tuple[str, str] = ("troll", "papelon")  # claves de `levels` en el YAML
    level_defaults: tuple[int, int] = (8, 15)
    win_multiplier: float = 0.5  # trolls: si igual ganaron, pesa menos
    loss_multiplier: float = 1.0
    story_priority: dict[str, int] = field(default_factory=dict)  # orden de la anécdota


TROLL_CATALOG = Catalog("trolls", RULES, story_priority=STORY_PRIORITY)


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
    weak_points_cap: float = 4.0  # máximo que suman entre todos los cargos menores
    loss_multiplier: float = 1.0  # carreadas: en una derrota pesa menos
    # Índice: una partida que no llega al nivel de aviso (floja, sin trollear)
    # pesa esto; y los puntos de la tabla opuesta (carreadas para trolls,
    # trolleadas para carreadas) restan esta fracción. Así quien juega mucho
    # no acumula por partidas flojas, y las buenas partidas bajan el índice.
    index_minor_game_weight: float = 0.3
    index_redemption: float = 0.5
    catalog: Catalog = TROLL_CATALOG

    @classmethod
    def default(cls, catalog: Catalog = TROLL_CATALOG) -> "TrollConfig":
        return _build({}, catalog)


def load_troll_config(path: str, catalog: Catalog = TROLL_CATALOG) -> TrollConfig:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise TrollConfigError(f"No encuentro el archivo de {catalog.name} en '{path}'.") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TrollConfigError(f"El archivo de {catalog.name} está mal formado (se esperaba un mapa).")
    return _build(raw, catalog)


def _number(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrollConfigError(f"'{where}' debe ser un número, no '{value}'.")
    return float(value)


def _build(raw: dict, catalog: Catalog = TROLL_CATALOG) -> TrollConfig:
    low_name, high_name = catalog.level_names
    levels = raw.get("levels") or {}
    if not isinstance(levels, dict):
        raise TrollConfigError(f"'levels' debe ser un mapa con '{low_name}' y '{high_name}'.")
    unknown_levels = sorted(set(levels) - {low_name, high_name})
    if unknown_levels:
        raise TrollConfigError(f"Niveles desconocidos en 'levels': {unknown_levels}. Válidos: {low_name}, {high_name}.")
    troll_level = int(_number(levels.get(low_name, catalog.level_defaults[0]), f"levels.{low_name}"))
    papelon_level = int(_number(levels.get(high_name, catalog.level_defaults[1]), f"levels.{high_name}"))
    if troll_level < 1:
        raise TrollConfigError(f"levels.{low_name} debe ser >= 1.")
    if papelon_level <= troll_level:
        raise TrollConfigError(f"levels.{high_name} debe ser mayor que levels.{low_name}.")

    ranked_multiplier = _number(raw.get("ranked_multiplier", 1.25), "ranked_multiplier")
    win_multiplier = _number(raw.get("win_multiplier", catalog.win_multiplier), "win_multiplier")
    loss_multiplier = _number(raw.get("loss_multiplier", catalog.loss_multiplier), "loss_multiplier")
    max_age = _number(raw.get("alert_max_age_hours", 36), "alert_max_age_hours")
    weak_cap = _number(raw.get("weak_points_cap", 4), "weak_points_cap")
    if weak_cap < 0:
        raise TrollConfigError("weak_points_cap no puede ser negativo.")
    if ranked_multiplier < 0 or win_multiplier < 0 or loss_multiplier < 0 or max_age <= 0:
        raise TrollConfigError("Los multiplicadores no pueden ser negativos y alert_max_age_hours debe ser > 0.")

    index = raw.get("index") or {}
    if not isinstance(index, dict):
        raise TrollConfigError("'index' debe ser un mapa con 'prior_games' y 'max_game_points'.")
    unknown_index = sorted(set(index) - {"prior_games", "max_game_points", "minor_game_weight", "redemption"})
    if unknown_index:
        raise TrollConfigError(f"Parámetros desconocidos en 'index': {unknown_index}.")
    prior_games = _number(index.get("prior_games", 2), "index.prior_games")
    max_game_points = _number(index.get("max_game_points", 30), "index.max_game_points")
    if prior_games < 0 or max_game_points <= 0:
        raise TrollConfigError("index.prior_games no puede ser negativo e index.max_game_points debe ser > 0.")
    minor_weight = _number(index.get("minor_game_weight", 0.3), "index.minor_game_weight")
    redemption = _number(index.get("redemption", 0.5), "index.redemption")
    if not (0 <= minor_weight <= 1 and 0 <= redemption <= 1):
        raise TrollConfigError("index.minor_game_weight e index.redemption van entre 0 y 1.")

    raw_rules = raw.get("rules") or {}
    if not isinstance(raw_rules, dict):
        raise TrollConfigError("'rules' debe ser un mapa regla -> parámetros.")
    unknown = sorted(set(raw_rules) - set(catalog.rules))
    if unknown:
        raise TrollConfigError(
            f"Reglas desconocidas en la config de {catalog.name}: {unknown}. Existentes: {sorted(catalog.rules)}.")

    rules: dict[str, RuleConfig] = {}
    for code, spec in catalog.rules.items():
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
        weak_points_cap=weak_cap,
        loss_multiplier=loss_multiplier,
        catalog=catalog,
        index_minor_game_weight=minor_weight,
        index_redemption=redemption,
    )
