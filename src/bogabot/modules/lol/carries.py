"""Carreadas: el mismo sistema que los trolls (detector, índice, avisos,
ranking), pero premiando. Las reglas viven en `bogabot/carries/rules.py` y
se configuran en config/carries.yaml; acá solo están los textos.

El servicio es el mismo `TrollService` con `CARRY_FLAVOR` (ver bot.py).
"""
from __future__ import annotations

from bogabot.modules.lol.trolls import Flavor

CARRY_FLAVOR = Flavor(
    noun="carreada",
    historic_noun="carreada legendaria",
    emoji="⭐",
    historic_emoji="🌟",
    color=0xF1C40F,
    historic_color=0x9B59B6,
    headlines_small=("¡Linda carreada!", "¡Se puso la partida al hombro!", "¡Carreada!"),
    headlines_big=("¡Tremenda carreada!", "¡Carreada de manual!", "¡Los llevó a todos!"),
    headlines_historic=("¡Carreada legendaria!", "¡Partida para el museo!", "¡Esto se enmarca!"),
    ranking_title="⭐ Ranking carry",
    index_noun="carry",
    nobody="Nadie carreó. Todos de mochila. 🎒",
    zero_label="🎒 De mochila",
    tiers=("🎒 Mochila", "🙂 Cumplidor", "💪 Clutch", "⭐ Carry", "🌟 Leyenda carry"),
    week_title="Carry de la semana",
    clean_text="Nada para destacar.",
    clean_short="—",
    flags_label="Jugadas",
    weak_label="jugadas menores",
    weak_note="ganar una partida fácil no es carreada",
    meter_name="⭐ Carry-o-metro",
    meter_fill="🟩",
    rules_title="📜 Reglamento carry",
    config_file="config/carries.yaml",
    command="carries",
    channel_label="canal de carreadas",
    analysis_title="🔍 Análisis carry",
    chain=". Además, ",
)
