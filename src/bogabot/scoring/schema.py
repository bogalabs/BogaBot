"""Carga y validación del archivo de configuración de scoring (YAML).

Convierte config/scoring.yaml en un objeto tipado `ScoringConfig`. Si el
archivo tiene una métrica desconocida o un peso inválido, falla temprano con
un mensaje claro en vez de dar un ranking silenciosamente mal calculado.
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml

# Métricas soportadas: nombre -> atributo/propiedad de PlayerStats a leer.
# Para habilitar una métrica nueva, agregala acá y en core/models.PlayerStats.
SUPPORTED_METRICS: dict[str, str] = {
    "win_rate": "win_rate",
    "kda": "kda",
    "avg_kills": "avg_kills",
    "avg_deaths": "avg_deaths",
    "avg_assists": "avg_assists",
    "damage_per_min": "damage_per_min",
    "avg_vision": "avg_vision",
    "games_played": "games_played",
}

VALID_NORMALIZATIONS = {"zscore", "minmax", "none"}


class ScoringConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class MetricSpec:
    name: str
    attr: str
    weight: float
    higher_is_better: bool


@dataclass(frozen=True)
class ScoringConfig:
    aggregation: str
    normalization: str
    metrics: list[MetricSpec]


def load_scoring_config(path: str) -> ScoringConfig:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ScoringConfigError(f"No encuentro el archivo de scoring en '{path}'.") from exc

    if not isinstance(raw, dict):
        raise ScoringConfigError("El archivo de scoring está vacío o mal formado.")

    normalization = str(raw.get("normalization", "zscore"))
    if normalization not in VALID_NORMALIZATIONS:
        raise ScoringConfigError(
            f"normalization '{normalization}' inválida. Usá una de: {sorted(VALID_NORMALIZATIONS)}."
        )

    raw_metrics = raw.get("metrics") or {}
    if not isinstance(raw_metrics, dict) or not raw_metrics:
        raise ScoringConfigError("Tenés que definir al menos una métrica en 'metrics'.")

    metrics: list[MetricSpec] = []
    for name, spec in raw_metrics.items():
        if name not in SUPPORTED_METRICS:
            raise ScoringConfigError(
                f"Métrica desconocida '{name}'. Soportadas: {sorted(SUPPORTED_METRICS)}."
            )
        if not isinstance(spec, dict) or "weight" not in spec:
            raise ScoringConfigError(f"La métrica '{name}' necesita al menos un 'weight'.")
        metrics.append(
            MetricSpec(
                name=name,
                attr=SUPPORTED_METRICS[name],
                weight=float(spec["weight"]),
                higher_is_better=bool(spec.get("higher_is_better", True)),
            )
        )

    return ScoringConfig(
        aggregation=str(raw.get("aggregation", "per_game_average")),
        normalization=normalization,
        metrics=metrics,
    )
