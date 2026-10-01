"""Tests del detector de carreadas (mismo motor que trolls, otro catálogo)."""
from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import match_json, record  # noqa: E402

from bogabot.carries.rules import CARRY_CATALOG, CARRY_RULES  # noqa: E402
from bogabot.core.models import MatchRecord, TrollLevel  # noqa: E402
from bogabot.core.timeutils import start_of_week  # noqa: E402
from bogabot.modules.lol.carries import CARRY_FLAVOR  # noqa: E402
from bogabot.modules.lol.trolls import TrollService  # noqa: E402
from bogabot.riot.mapper import map_match  # noqa: E402
from bogabot.storage.memory import InMemoryStorage  # noqa: E402
from bogabot.trolls.detector import TrollDetector  # noqa: E402
from bogabot.trolls.schema import TrollConfig, TrollConfigError, load_troll_config  # noqa: E402

_REPO_YAML = Path(__file__).resolve().parents[1] / "config" / "carries.yaml"
_TZ = "America/Argentina/Buenos_Aires"
_WIN = dict(win=True)


def _detector() -> TrollDetector:
    return TrollDetector(TrollConfig.default(CARRY_CATALOG))


def _evaluate(**kw):
    return _detector().evaluate(record(**kw))


def _codes(**kw) -> set[str]:
    return {f.code for f in _evaluate(**kw).flags}


class TestCarryCriteria(unittest.TestCase):
    def test_a_normal_win_is_not_a_carry(self):
        self.assertEqual(_evaluate(kills=6, deaths=4, assists=8, **_WIN).points, 0)

    def test_easy_stomp_with_high_kda_is_not_a_carry(self):
        # El viejo criterio (ganar + KDA >= 5) lo contaba como carry; ahora no.
        v = _evaluate(kills=8, deaths=1, assists=9, team_kills=40, enemy_kills=10,
                      damage_to_champions=21000, **_WIN)
        self.assertEqual([f.code for f in v.flags], ["clean_kda"])
        self.assertEqual(v.level, TrollLevel.NONE)

    def test_real_carry_by_damage_and_kills(self):
        v = _evaluate(kills=13, deaths=2, assists=7, team_kills=30, damage_to_champions=38000, **_WIN)
        self.assertTrue({"damage_carry", "slayer"} <= {f.code for f in v.flags})
        self.assertEqual(v.level, TrollLevel.TROLL)  # nivel 1 = carreada

    def test_pentakill_alone_is_a_carry(self):
        v = _evaluate(kills=9, deaths=5, assists=6, penta_kills=1, quadra_kills=1, **_WIN)
        self.assertEqual([f.code for f in v.flags], ["pentakill"])  # reemplaza a la quadra
        self.assertEqual(v.level, TrollLevel.TROLL)

    def test_legendary_carry(self):
        v = _evaluate(kills=16, deaths=3, assists=8, team_kills=35, damage_to_champions=41000,
                      penta_kills=1, largest_killing_spree=9, **_WIN)
        self.assertEqual(v.level, TrollLevel.PAPELON)  # nivel 2 = legendaria

    def test_losing_carry_counts_half(self):
        lost = _evaluate(kills=11, deaths=3, assists=4, team_kills=20, damage_to_champions=43000)
        won = _evaluate(kills=11, deaths=3, assists=4, team_kills=20, damage_to_champions=43000, **_WIN)
        self.assertEqual(lost.points, round(won.points * 0.5 + 0.01))
        self.assertEqual(lost.level, TrollLevel.NONE)
        self.assertFalse(lost.carried)  # "lo llevaron de mochila" es solo de trolls

    def test_kills_need_to_be_a_big_share_of_the_team(self):
        # 12 kills en 30 min, pero el equipo hizo 60: no es "máquina de matar".
        self.assertNotIn("slayer", _codes(kills=12, deaths=3, assists=10, team_kills=60, **_WIN))
        self.assertIn("slayer", _codes(kills=12, deaths=3, assists=10, team_kills=25, **_WIN))
        # Y por duración: 10 kills en 30 min no alcanza (umbral 10,5); en 20 min sí.
        self.assertNotIn("slayer", _codes(kills=10, deaths=3, assists=5, team_kills=25, **_WIN))
        self.assertIn("slayer", _codes(kills=10, deaths=3, assists=5, team_kills=25,
                                       game_duration_seconds=20 * 60, **_WIN))

    def test_support_and_tank_can_carry_their_way(self):
        support = _evaluate(position="UTILITY", kills=1, deaths=2, assists=24, team_kills=30,
                            damage_to_champions=8000, vision_score=80, **_WIN)
        self.assertIn("enabler", {f.code for f in support.flags})
        self.assertNotIn("kp_carry", {f.code for f in support.flags})
        self.assertEqual(support.level, TrollLevel.TROLL)
        tank = _codes(position="TOP", kills=3, deaths=4, assists=20, team_kills=30,
                      damage_taken=60000, team_damage_taken=150000, **_WIN)
        self.assertIn("wall", tank)

    def test_immortal_needs_to_be_in_the_fights(self):
        self.assertIn("immortal", _codes(kills=10, deaths=0, assists=14, team_kills=30, **_WIN))
        # No murió, pero en un stomp y sin participar: no.
        self.assertNotIn("immortal", _codes(kills=5, deaths=0, assists=7, team_kills=40, **_WIN))

    def test_comeback_needs_him_to_matter(self):
        base = dict(kills=9, deaths=3, assists=10, team_kills=30, max_gold_deficit=6500, **_WIN)
        self.assertIn("comeback", _codes(damage_to_champions=35000, **base))
        self.assertNotIn("comeback", _codes(damage_to_champions=12000, kills=2, assists=4,
                                            **{k: v for k, v in base.items() if k not in ("kills", "assists")}))

    def test_minor_plays_are_capped(self):
        v = _evaluate(kills=6, deaths=1, assists=20, team_kills=30, gold_diff_15=3000, solo_kills=4,
                      first_blood_kill=True, vision_score=50, cs=280, **_WIN)
        self.assertGreater(v.capped_points, 0)
        self.assertEqual(v.level, TrollLevel.NONE)

    def test_every_rule_documented_and_yaml_matches_code(self):
        text = _REPO_YAML.read_text(encoding="utf-8")
        for code in CARRY_RULES:
            self.assertIn(f"\n  {code}:", text, f"la regla '{code}' no está en carries.yaml")
        self.assertEqual(load_troll_config(str(_REPO_YAML), CARRY_CATALOG), TrollConfig.default(CARRY_CATALOG))

    def test_carry_level_names_are_validated(self):
        path = Path(__file__).resolve().parent / "_tmp_carries.yaml"
        try:
            path.write_text("levels: {troll: 8}\n", encoding="utf-8")
            with self.assertRaises(TrollConfigError):
                load_troll_config(str(path), CARRY_CATALOG)
            path.write_text("levels: {carry: 6, legendaria: 12}\n", encoding="utf-8")
            cfg = load_troll_config(str(path), CARRY_CATALOG)
            self.assertEqual((cfg.troll_level, cfg.papelon_level), (6, 12))
        finally:
            path.unlink(missing_ok=True)


class TestCarryMapping(unittest.TestCase):
    def test_multikills_and_challenges_are_mapped(self):
        data = match_json(overrides={1: {"pentaKills": 1, "quadraKills": 2, "largestKillingSpree": 9,
                                         "firstBloodKill": True,
                                         "challenges": {"soloKills": 4, "epicMonsterSteals": 1}}})
        r = map_match(data, "puuid-1", 1)
        self.assertEqual((r.penta_kills, r.quadra_kills, r.largest_killing_spree), (1, 2, 9))
        self.assertTrue(r.first_blood_kill)
        self.assertEqual((r.solo_kills, r.objective_steals), (4, 1))
        self.assertEqual(MatchRecord.from_dict(r.to_dict()), r)

    def test_missing_challenges_stay_none(self):
        r = map_match(match_json(), "puuid-1", 1)
        self.assertIsNone(r.solo_kills)
        self.assertIsNone(r.objective_steals)


class TestCarryService(unittest.TestCase):
    def test_ranking_and_texts_use_carry_flavor(self):
        store = InMemoryStorage()
        settings = SimpleNamespace(timezone=_TZ, general_channel_id=999)
        service = TrollService(store, _detector(), settings, flavor=CARRY_FLAVOR)  # type: ignore[arg-type]
        week_start = start_of_week(_TZ).astimezone(timezone.utc)
        carry = dict(kills=13, deaths=2, assists=7, team_kills=30, damage_to_champions=38000, **_WIN)
        asyncio.run(store.save_match(record(match_id="LA2_1", discord_id=1, game_name="Carry",
                                            game_creation=week_start + timedelta(hours=1), **carry)))
        asyncio.run(store.save_match(record(match_id="LA2_1", discord_id=2, game_name="Mochila",
                                            game_creation=week_start + timedelta(hours=1), **_WIN)))
        rows = asyncio.run(service.standings("week"))
        self.assertEqual([s.display_name for s in rows], ["Carry", "Mochila"])
        embed = service.build_standings_embed(rows, "week")
        self.assertIn("Ranking carry", embed.title)
        self.assertEqual(embed.fields[0].name, "🎒 De mochila")
        line = service.build_general_line(service.evaluate(record(discord_id=1, **carry)))
        self.assertTrue(line.startswith("⭐"))
        self.assertIn("hizo el 38% del daño del equipo", line)
        rules = service.build_rules_embed()
        self.assertIn("Reglamento carry", rules.title)
        self.assertIn("si perdieron ×0.5", rules.description)


if __name__ == "__main__":
    unittest.main()
