"""Tests del detector de trolls (reglas, niveles, config) y del TrollService
(ranking troll y embeds). Sin red ni Discord: lógica pura."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import record  # noqa: E402

from bogabot.core.models import MatchRecord, TrollLevel  # noqa: E402
from bogabot.core.timeutils import start_of_week  # noqa: E402
from bogabot.modules.lol.trolls import TrollService  # noqa: E402
from bogabot.storage.memory import InMemoryStorage  # noqa: E402
from bogabot.trolls.detector import TrollDetector  # noqa: E402
from bogabot.trolls.rules import RULES  # noqa: E402
from bogabot.trolls.schema import TrollConfig, TrollConfigError, load_troll_config  # noqa: E402

_REPO_YAML = Path(__file__).resolve().parents[1] / "config" / "trolls.yaml"
_TZ = "America/Argentina/Buenos_Aires"


def _detector() -> TrollDetector:
    return TrollDetector(TrollConfig.default())


def _codes(r: MatchRecord) -> set[str]:
    return {f.code for f in _detector().evaluate(r).flags}


def _papelon_record(**overrides) -> MatchRecord:
    data = dict(
        queue_id=440, kills=1, deaths=15, assists=2, damage_to_champions=5000, team_damage=90000,
        team_kills=12, team_deaths=30, enemy_kills=35, time_dead_seconds=700, gave_first_blood=True,
        first_death_minute=2, deaths_before_10=4, gold_diff_15=-3500, deaths_to_lane_opponent=6,
        items_sold=7, game_ended_in_surrender=True, game_duration_seconds=1150,
    )
    data.update(overrides)
    return record(**data)


class TestTrollRules(unittest.TestCase):
    def test_normal_game_is_clean(self):
        v = _detector().evaluate(record())
        self.assertTrue(v.is_clean)
        self.assertEqual((v.points, v.level), (0, TrollLevel.NONE))

    def test_feeder_game_triggers_alert(self):
        v = _detector().evaluate(record(kills=1, deaths=12, assists=2))
        # tragic_kda también se cumple, pero feeder la reemplaza (no se suman).
        self.assertEqual({f.code for f in v.flags}, {"feeder", "low_kp"})
        feeder = next(f for f in v.flags if f.code == "feeder")
        self.assertEqual(feeder.points, 4)  # 3 + 1 por las 2 muertes de más
        self.assertEqual(v.points, 6)
        self.assertEqual(v.level, TrollLevel.TROLL)
        self.assertEqual(v.flags[0].code, "feeder")  # ordenados por puntos
        self.assertIn("tragic_kda", _codes(record(kills=1, deaths=7, assists=1)))

    def test_ranked_multiplier_rounds_half_up(self):
        v = _detector().evaluate(record(queue_id=440, kills=1, deaths=7, assists=1, team_kills=8))
        self.assertEqual([f.code for f in v.flags], ["tragic_kda"])
        self.assertTrue(v.ranked_bonus)
        self.assertEqual(v.points, 3)  # 2 × 1.25 = 2.5 -> 3

    def test_win_halves_points(self):
        v = _detector().evaluate(record(win=True, kills=1, deaths=12, assists=2))
        self.assertTrue(v.carried)
        self.assertEqual(v.points, 3)
        self.assertEqual(v.level, TrollLevel.NONE)

    def test_historic_papelon(self):
        v = _detector().evaluate(_papelon_record())
        codes = {f.code for f in v.flags}
        for expected in ("feeder", "first_blood", "early_deaths", "lane_gap",
                         "lane_delivery", "low_damage", "tombstone", "item_seller", "early_ff", "stomped"):
            self.assertIn(expected, codes)
        self.assertEqual(v.level, TrollLevel.PAPELON)
        self.assertGreaterEqual(v.points, TrollConfig.default().papelon_level)

    def test_first_blood_early_bonus(self):
        flag = lambda r: next(f for f in _detector().evaluate(r).flags if f.code == "first_blood")  # noqa: E731
        self.assertEqual(flag(record(gave_first_blood=True, first_death_minute=2)).points, 2)
        self.assertEqual(flag(record(gave_first_blood=True, first_death_minute=6)).points, 1)

    def test_ghost_does_not_stack_with_low_kp(self):
        codes = _codes(record(kills=0, deaths=4, assists=0, damage_to_champions=3000))
        self.assertIn("ghost", codes)
        self.assertNotIn("low_kp", codes)
        self.assertNotIn("pacifist", codes)
        v = _detector().evaluate(record(kills=0, deaths=6, assists=0))
        self.assertGreaterEqual(v.level, TrollLevel.TROLL)

    def test_ghost_needs_a_real_game(self):
        self.assertNotIn("ghost", _codes(record(kills=0, assists=0, team_kills=2)))

    def test_supports_are_not_judged_like_carries(self):
        sup = dict(position="UTILITY", kills=0, assists=15, damage_to_champions=6000, cs=30)
        codes = _codes(record(**sup))
        self.assertNotIn("pacifist", codes)
        self.assertNotIn("low_damage", codes)
        self.assertNotIn("farm_allergy", codes)
        # Pero a un support se le exige visión: 25 en 30 min es poco.
        self.assertIn("blind", codes)
        self.assertNotIn("blind", _codes(record()))  # un mid con 25 de visión zafa

    def test_aram_uses_its_own_thresholds_and_skips_lane_rules(self):
        aram = dict(game_mode="ARAM", queue_id=450, position="", opponent_champion="")
        self.assertNotIn("feeder", _codes(record(kills=2, deaths=12, assists=8, **aram)))
        self.assertIn("feeder", _codes(record(kills=1, deaths=15, assists=6, **aram)))
        codes = _codes(record(gave_first_blood=True, deaths_before_10=5, gold_diff_15=-5000,
                              control_wards_bought=0, cs=10, **aram))
        self.assertFalse(codes & {"first_blood", "early_deaths", "lane_gap", "no_control_wards", "farm_allergy"})

    def test_early_ff_only_on_summoners_rift(self):
        ff = dict(game_ended_in_surrender=True, game_duration_seconds=16 * 60)
        self.assertIn("early_ff", _codes(record(**ff)))
        self.assertNotIn("early_ff", _codes(record(win=True, **ff)))
        self.assertNotIn("early_ff", _codes(record(game_mode="SWIFTPLAY", **ff)))
        self.assertNotIn("early_ff", _codes(record(queue_id=480, **ff)))

    def test_team_anchor(self):
        self.assertIn("team_anchor", _codes(record(deaths=9, team_deaths=15)))
        self.assertNotIn("team_anchor", _codes(record(deaths=9, team_deaths=20)))

    def test_arena_last_place(self):
        arena = dict(game_mode="CHERRY", queue_id=1700, position="")
        self.assertEqual(_codes(record(placement=8, **arena)), {"arena_last"})
        self.assertEqual(_codes(record(placement=3, **arena)), set())

    def test_practice_tool_is_not_judged(self):
        self.assertTrue(_detector().evaluate(record(game_mode="PRACTICETOOL", kills=0, deaths=30)).is_clean)

    def test_legacy_record_only_uses_what_it_has(self):
        legacy = MatchRecord.from_dict({
            "match_id": "LA2_9", "puuid": "p", "participant_id": 1, "discord_id": 1, "game_name": "X",
            "game_creation": "2026-08-22T01:00:00+00:00", "game_duration_seconds": 1150, "queue_id": 440,
            "game_mode": "CLASSIC", "champion": "Ahri", "position": "MIDDLE", "win": False,
            "game_ended_in_surrender": True, "kills": 1, "deaths": 11, "assists": 1,
            "damage_to_champions": 4000, "vision_score": 30, "cs": 150,
        })
        # Sin stats de equipo ni timeline: solo las reglas que se pueden evaluar.
        self.assertEqual(_codes(legacy), {"feeder", "early_ff"})

    def test_every_rule_has_points_and_profiles(self):
        for code, spec in RULES.items():
            self.assertEqual(code, spec.code)
            self.assertIn("points", spec.params)
            self.assertTrue(spec.profiles)


class TestTrollConfig(unittest.TestCase):
    def _load(self, text: str) -> TrollConfig:
        path = Path(tempfile.mkdtemp()) / "trolls.yaml"
        path.write_text(text, encoding="utf-8")
        return load_troll_config(str(path))

    def test_repo_yaml_loads_and_documents_every_rule(self):
        cfg = load_troll_config(str(_REPO_YAML))
        self.assertLess(cfg.troll_level, cfg.papelon_level)
        text = _REPO_YAML.read_text(encoding="utf-8")
        for code in RULES:
            self.assertIn(f"\n  {code}:", text, f"la regla '{code}' no está documentada en trolls.yaml")

    def test_overrides_and_disable(self):
        cfg = self._load("levels: {troll: 4, papelon: 9}\nrules:\n  feeder: {min_deaths: 8}\n  pinger: {enabled: false}\n")
        self.assertEqual((cfg.troll_level, cfg.papelon_level), (4, 9))
        self.assertEqual(cfg.rules["feeder"].params["min_deaths"], 8)
        self.assertEqual(cfg.rules["feeder"].params["points"], 3)  # el resto queda por defecto
        self.assertFalse(cfg.rules["pinger"].enabled)
        detector = TrollDetector(cfg)
        self.assertIn("feeder", {f.code for f in detector.evaluate(record(kills=0, deaths=8, assists=2)).flags})
        self.assertNotIn("pinger", {f.code for f in detector.evaluate(record(question_pings=50)).flags})
        self.assertNotIn("pinger", {spec.code for spec, _ in detector.rules_overview()})

    def test_empty_file_uses_defaults(self):
        self.assertEqual(self._load(""), TrollConfig.default())

    def test_typos_fail_early(self):
        with self.assertRaises(TrollConfigError):
            self._load("rules:\n  feder: {points: 2}\n")
        with self.assertRaises(TrollConfigError):
            self._load("rules:\n  feeder: {min_death: 2}\n")
        with self.assertRaises(TrollConfigError):
            self._load("rules:\n  feeder: {points: mucho}\n")
        with self.assertRaises(TrollConfigError):
            self._load("levels: {troll: 10, papelon: 5}\n")
        with self.assertRaises(TrollConfigError):
            load_troll_config("/no/existe/trolls.yaml")


class TestTrollService(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStorage()
        settings = SimpleNamespace(timezone=_TZ, general_channel_id=999)
        self.service = TrollService(self.store, _detector(), settings)  # type: ignore[arg-type]
        self.week_start = start_of_week(_TZ).astimezone(timezone.utc)

    def _save(self, **overrides) -> MatchRecord:
        overrides.setdefault("game_creation", self.week_start + timedelta(minutes=30))
        r = record(**overrides)
        asyncio.run(self.store.save_match(r))
        return r

    def test_week_standings(self):
        self._save(match_id="LA2_1", discord_id=1, game_name="Troll", kills=1, deaths=12, assists=2)
        self._save(match_id="LA2_2", discord_id=1, game_name="Troll")  # una partida limpia
        self._save(match_id="LA2_3", discord_id=1, game_name="Troll", queue_id=440, kills=1, deaths=15, assists=2,
                   gave_first_blood=True, first_death_minute=2, deaths_before_10=4, gold_diff_15=-3500,
                   deaths_to_lane_opponent=6, items_sold=7)
        self._save(match_id="LA2_1", discord_id=2, game_name="Santo")
        self._save(match_id="LA2_4", discord_id=3, game_name="Medio", kills=0, deaths=6, assists=4)
        # Partida de la semana pasada: no cuenta para "week".
        self._save(match_id="LA2_0", discord_id=2, game_name="Santo", kills=0, deaths=20, assists=0,
                   game_creation=self.week_start - timedelta(days=2))

        rows = asyncio.run(self.service.standings("week"))
        self.assertEqual([s.display_name for s in rows], ["Troll", "Medio", "Santo"])
        troll = rows[0]
        self.assertEqual((troll.rank, troll.games), (1, 3))
        self.assertEqual(troll.troll_games, 2)
        self.assertEqual(troll.papelones, 1)
        self.assertEqual(troll.worst.record.match_id, "LA2_3")
        self.assertEqual(rows[2].points, 0)

        prev = asyncio.run(self.service.standings("prev_week"))
        self.assertEqual([s.display_name for s in prev], ["Santo"])
        self.assertEqual(len(asyncio.run(self.service.standings("all"))), 3)

        embed = self.service.build_standings_embed(rows, "week")
        self.assertIn("Troll", embed.description)
        self.assertIn("Santo", embed.fields[0].value)  # sección de limpios

    def test_ranking_uses_per_game_index_not_total(self):
        # "Heavy" la trollea fuerte en 2 partidas; "Grinder" jugó 18 y trolleó 2.
        feeder = dict(kills=1, deaths=15, assists=2, deaths_before_10=4, gold_diff_15=-3000,
                      deaths_to_lane_opponent=5, items_sold=6)
        for i in range(2):
            self._save(match_id=f"LA2_H{i}", discord_id=1, game_name="Heavy", **feeder)
        for i in range(18):
            extra = feeder if i < 2 else {}
            self._save(match_id=f"LA2_G{i}", discord_id=2, game_name="Grinder", **extra)
        # Otro más con muchas partidas: más total que Heavy si se sumara, pero menos índice.
        for i in range(8):
            self._save(match_id=f"LA2_M{i}", discord_id=3, game_name="Mucho",
                       kills=1, deaths=12, assists=2)

        rows = asyncio.run(self.service.standings("week"))
        by_name = {s.display_name: s for s in rows}
        self.assertEqual(by_name["Heavy"].points, by_name["Grinder"].points)  # mismo total
        self.assertEqual(rows[0].display_name, "Heavy")
        self.assertGreater(by_name["Mucho"].points, by_name["Heavy"].points)  # más total...
        self.assertLess(by_name["Mucho"].index, by_name["Heavy"].index)  # ...pero menos índice
        self.assertEqual([s.display_name for s in rows], ["Heavy", "Mucho", "Grinder"])
        self.assertAlmostEqual(by_name["Grinder"].index, by_name["Grinder"].points / 18)
        embed = self.service.build_standings_embed(rows, "week")
        self.assertIn("índice", embed.description)

    def test_is_fresh(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(self.service.is_fresh(record(game_creation=now - timedelta(hours=2))))
        self.assertFalse(self.service.is_fresh(record(game_creation=now - timedelta(days=3))))

    def test_find_record(self):
        self._save(match_id="LA2_111", discord_id=1)
        self._save(match_id="LA2_222", discord_id=1, game_creation=self.week_start + timedelta(hours=5))
        find = lambda *a: asyncio.run(self.service.find_record(*a))  # noqa: E731
        self.assertEqual(find(1).match_id, "LA2_222")  # la última
        self.assertEqual(find(1, "111").match_id, "LA2_111")
        self.assertEqual(find(1, "la2_111").match_id, "LA2_111")
        self.assertIsNone(find(1, "333"))
        self.assertIsNone(find(2))

    def test_alert_embeds(self):
        troll = self.service.evaluate(record(kills=1, deaths=12, assists=2, discord_id=7, game_name="Pepe"))
        content, embed = self.service.build_alert(troll, None, 4)
        self.assertIn("<@7>", content)
        self.assertEqual(embed.color.value, 0xE67E22)  # naranja: alerta común
        # Determinístico: la misma partida da el mismo texto.
        self.assertEqual(self.service.build_alert(troll, None, 4)[0], content)

        papelon = self.service.evaluate(_papelon_record(discord_id=7, game_name="Pepe"))
        content, embed = self.service.build_alert(papelon, None, 4)
        self.assertIn("<@7>", content)
        self.assertEqual(embed.color.value, discord_dark_red())
        for field in embed.fields:
            self.assertLessEqual(len(field.value), 1024)
        self.assertIn("ranked", embed.fields[1].value)

    def test_analysis_and_rules_embeds(self):
        v = self.service.evaluate(record())
        embed = self.service.build_analysis_embed(v)
        self.assertIn("limpia", embed.fields[0].value)
        legacy = self.service.evaluate(record(deaths_before_10=None))
        self.assertIn("Faltan datos", [f.name.split(" ", 1)[1] for f in self.service.build_analysis_embed(legacy).fields])
        rules = self.service.build_rules_embed()
        self.assertLessEqual(len(rules.description), 4096)
        self.assertIn("<#999>", rules.description)

    def test_meter(self):
        self.assertEqual(self.service.meter(0), "⬛" * 10)
        self.assertEqual(self.service.meter(1).count("🟥"), 1)
        self.assertEqual(self.service.meter(18), "🟥" * 10)
        self.assertEqual(self.service.meter(40), "🟥" * 10)
        lines = self.service.meter_lines([
            self.service.evaluate(record(discord_id=1)),
            self.service.evaluate(record(discord_id=2, kills=1, deaths=12, assists=2)),
        ])
        self.assertTrue(lines.startswith("<@2>"))
        self.assertIn("limpio", lines)


def discord_dark_red() -> int:
    import discord
    return discord.Color.dark_red().value


if __name__ == "__main__":
    unittest.main()
