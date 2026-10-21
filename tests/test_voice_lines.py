"""Tests de las frases habladas de evento (quién trolleó/carreó)."""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import record  # noqa: E402

from bogabot.core.models import TrollFlag, TrollLevel, TrollVerdict  # noqa: E402
from bogabot.modules.sounds.voice_lines import (  # noqa: E402
    build_start_line,
    join_names,
    CARRY,
    TROLL,
    SpokenVerdict,
    build_line,
    pick_spoken_verdict,
    spoken_name,
)


def verdict(level: TrollLevel, points: int, detail: str = "murió 12 veces en 25 minutos",
            discord_id: int = 1) -> TrollVerdict:
    flags = [TrollFlag("x", "", "X", detail, points)] if level > TrollLevel.NONE else []
    return TrollVerdict(record(discord_id=discord_id, champion="Yasuo"), flags, points, points, level)


class TestPickSpokenVerdict(unittest.TestCase):
    def test_nothing_below_alert_level(self):
        self.assertIsNone(pick_spoken_verdict([verdict(TrollLevel.NONE, 3)], [verdict(TrollLevel.NONE, 3)]))

    def test_higher_level_wins(self):
        picked = pick_spoken_verdict([verdict(TrollLevel.TROLL, 10)], [verdict(TrollLevel.PAPELON, 20, discord_id=2)])
        self.assertEqual(picked.kind, CARRY)

    def test_troll_wins_a_tie_then_points(self):
        a, b = verdict(TrollLevel.TROLL, 8, discord_id=1), verdict(TrollLevel.TROLL, 12, discord_id=2)
        picked = pick_spoken_verdict([a, b], [verdict(TrollLevel.TROLL, 30, discord_id=3)])
        self.assertEqual((picked.kind, picked.verdict.record.discord_id), (TROLL, 2))


class TestBuildLine(unittest.TestCase):
    def test_line_mentions_name_champ_and_detail(self):
        text = build_line(SpokenVerdict(TROLL, verdict(TrollLevel.TROLL, 10)), "Pepo", random.Random(1))
        for part in ("Pepo", "Yasuo", "murió 12 veces en 25 minutos"):
            self.assertIn(part, text)

    def test_every_template_formats(self):
        for kind in (TROLL, CARRY):
            for level in (TrollLevel.TROLL, TrollLevel.PAPELON):
                for seed in range(10):
                    self.assertIn("Pepo", build_line(SpokenVerdict(kind, verdict(level, 10)), "Pepo",
                                                     random.Random(seed)))

    def test_spoken_name_strips_symbols_and_tag(self):
        self.assertEqual(spoken_name("🔥Pepo_Nullo🔥"), "Pepo Nullo")
        self.assertEqual(spoken_name("mcjhonson#LAS"), "mcjhonson")
        self.assertEqual(spoken_name("💀💀"), "alguien")


class TestStartLine(unittest.TestCase):
    def test_join_names(self):
        self.assertEqual(join_names(["A"]), "A")
        self.assertEqual(join_names(["A", "B"]), "A y B")
        self.assertEqual(join_names(["A", "B", "C"]), "A, B y C")

    def test_start_line(self):
        self.assertEqual(build_start_line(["Lauti", "Pepo"], "Ranked Flex", "commands"),
                         "Lauti y Pepo arrancaron una partida de Ranked Flex. Hagan sus apuestas en commands.")
        self.assertEqual(build_start_line(["A", "B"], "ARAM", None), "A y B arrancaron una partida de ARAM.")


if __name__ == "__main__":
    unittest.main()
