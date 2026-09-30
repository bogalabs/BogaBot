"""Tests del detector de trolls (reglas, niveles, config) y del TrollService
(ranking troll y embeds). Sin red ni Discord: lógica pura."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from dataclasses import replace
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
        self.assertEqual(v.level, TrollLevel.NONE)  # feedear un poco sin nada más: no es trolleada
        self.assertEqual(v.flags[0].code, "feeder")  # ordenados por puntos
        heavy = _detector().evaluate(record(kills=1, deaths=14, assists=2, deaths_before_10=4))
        self.assertEqual(heavy.level, TrollLevel.TROLL)
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
                         "lane_delivery", "low_damage", "item_seller", "early_ff", "stomped"):
            self.assertIn(expected, codes)
        self.assertEqual(v.level, TrollLevel.PAPELON)
        self.assertGreaterEqual(v.points, TrollConfig.default().papelon_level)

    def test_first_blood_early_bonus(self):
        flag = lambda r: next(f for f in _detector().evaluate(r).flags if f.code == "first_blood")  # noqa: E731
        self.assertEqual(flag(record(gave_first_blood=True, first_death_minute=2)).points, 2)
        self.assertEqual(flag(record(gave_first_blood=True, first_death_minute=4)).points, 1)
        # Una primera sangre "normal" (gank al minuto 7) no es trolleada.
        self.assertNotIn("first_blood", _codes(record(gave_first_blood=True, first_death_minute=7)))

    def test_ghost_does_not_stack_with_low_kp(self):
        codes = _codes(record(kills=0, deaths=4, assists=0, damage_to_champions=3000))
        self.assertIn("ghost", codes)
        self.assertNotIn("low_kp", codes)
        self.assertNotIn("pacifist", codes)
        v = _detector().evaluate(record(kills=0, deaths=6, assists=0, damage_to_champions=2000))
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
        aram["game_duration_seconds"] = 20 * 60
        self.assertNotIn("feeder", _codes(record(kills=2, deaths=11, assists=8, **aram)))
        self.assertIn("feeder", _codes(record(kills=1, deaths=15, assists=6, **aram)))
        codes = _codes(record(gave_first_blood=True, deaths_before_10=5, gold_diff_15=-5000,
                              control_wards_bought=0, cs=10, **aram))
        self.assertFalse(codes & {"first_blood", "early_deaths", "lane_gap", "no_control_wards", "farm_allergy"})

    def test_early_ff_only_on_summoners_rift(self):
        ff = dict(game_ended_in_surrender=True, game_duration_seconds=16 * 60, kills=1, deaths=7, assists=1)
        self.assertIn("early_ff", _codes(record(**ff)))
        # Si jugó bien, el FF del equipo no le suma (es solo agravante).
        self.assertEqual(_codes(record(game_ended_in_surrender=True, game_duration_seconds=16 * 60)), set())
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

    def test_new_timeline_situations(self):
        base = _detector().evaluate(record(base_absent=2, base_absent_farming="jungla"))
        self.assertEqual([(f.code, f.detail) for f in base.flags],
                         [("base_absent", "nos tiraban la base y estaba farmeando la jungla")])
        self.assertNotIn("base_absent", _codes(record(base_absent=1)))  # 1 sola estructura no alcanza
        self.assertNotIn("base_absent", _codes(record(base_absent=3, win=True)))  # si ganaron, no
        throw = next(f for f in _detector().evaluate(record(throw_deaths=2, throw_objective="el Barón")).flags)
        self.assertEqual((throw.code, throw.points), ("throw", 4))
        self.assertIn("perdimos el Barón (2 veces)", throw.detail)
        afk = _detector().evaluate(record(afk_minutes=4))
        self.assertEqual([f.code for f in afk.flags], ["afk"])
        self.assertIn("4 minutos quieto", afk.flags[0].detail)
        self.assertNotIn("afk", _codes(record(afk_minutes=2)))
        self.assertIn("hoarder", _codes(record(rich_deaths=2, max_gold_on_death=4100)))

    def test_feeder_depends_on_game_length(self):
        self.assertIn("feeder", _codes(record(kills=1, deaths=9, assists=2, game_duration_seconds=20 * 60)))
        self.assertNotIn("feeder", _codes(record(kills=1, deaths=11, assists=2, game_duration_seconds=45 * 60)))

    def test_tank_is_not_low_damage(self):
        low = dict(damage_to_champions=7000, team_damage=100000, team_damage_taken=150000)
        self.assertNotIn("low_damage", _codes(record(damage_taken=50000, **low)))  # tanqueó el 33%
        self.assertIn("low_damage", _codes(record(damage_taken=15000, **low)))  # ni pegó ni tanqueó

    def test_lane_delivery_needs_most_deaths_to_the_rival(self):
        self.assertIn("lane_delivery", _codes(record(deaths=8, deaths_to_lane_opponent=5)))
        self.assertNotIn("lane_delivery", _codes(record(deaths=14, deaths_to_lane_opponent=4)))

    def test_control_wards_only_with_poor_vision(self):
        self.assertNotIn("no_control_wards", _codes(record(control_wards_bought=0, vision_score=30)))
        self.assertIn("no_control_wards", _codes(record(control_wards_bought=0, vision_score=18)))
        # Con visión nula ya cuenta "ciego"; no se suman los dos.
        codes = _codes(record(control_wards_bought=0, vision_score=5))
        self.assertIn("blind", codes)
        self.assertNotIn("no_control_wards", codes)

    def test_low_farm_or_no_kills_is_fine_when_dealing_damage(self):
        roamer = dict(kills=0, assists=10, cs=90, damage_to_champions=30000, team_damage=100000)
        codes = _codes(record(**roamer))
        self.assertNotIn("farm_allergy", codes)
        self.assertNotIn("pacifist", codes)

    def test_base_absent_farming_weighs_more(self):
        flag = lambda **kw: next(f for f in _detector().evaluate(record(**kw)).flags)  # noqa: E731
        self.assertEqual(flag(base_absent=2, base_absent_farming="jungla").points, 8)
        self.assertEqual(flag(base_absent=2).points, 5)
        # "Nos tiraban la base y estaba farmeando" alcanza sola para ser trolleada.
        self.assertEqual(_detector().evaluate(record(base_absent=2, base_absent_farming="jungla")).level,
                         TrollLevel.TROLL)
        self.assertEqual(_detector().evaluate(record(afk_minutes=3)).level, TrollLevel.TROLL)

    def test_a_bad_game_is_not_a_trolleada(self):
        # Muchos cargos menores juntos (línea perdida, muertes tempranas, poco
        # daño, poca KP, FF...) no llegan a trolleada: tienen tope.
        v = _detector().evaluate(record(
            queue_id=440, game_ended_in_surrender=True, game_duration_seconds=17 * 60, kills=1, deaths=7,
            assists=1, team_kills=8, enemy_kills=30, deaths_before_10=3, gold_diff_15=-2600,
            gave_first_blood=True, first_death_minute=4, damage_to_champions=5000))
        self.assertGreaterEqual(len(v.flags), 5)
        self.assertEqual(v.base_points, TrollConfig.default().weak_points_cap)
        self.assertGreater(v.capped_points, 0)
        self.assertEqual(v.level, TrollLevel.NONE)

    def test_papelon_needs_strong_signals(self):
        inting = _detector().evaluate(record(kills=0, deaths=18, assists=1, items_sold=6, deaths_before_10=4))
        self.assertEqual(inting.level, TrollLevel.PAPELON)
        afk_game = _detector().evaluate(record(kills=0, deaths=6, assists=0, afk_minutes=20,
                                               damage_to_champions=800))
        self.assertEqual(afk_game.level, TrollLevel.PAPELON)

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
        cfg = self._load("levels: {troll: 4, papelon: 9}\nrules:\n  feeder: {min_deaths: 8, deaths_per_10: 2}\n  pinger: {enabled: false}\n")
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
        with self.assertRaises(TrollConfigError):
            self._load("index: {prior_game: 2}\n")
        with self.assertRaises(TrollConfigError):
            self._load("index: {max_game_points: 0}\n")

    def test_index_config(self):
        cfg = self._load("index: {prior_games: 0, max_game_points: 15}\n")
        self.assertEqual((cfg.index_prior_games, cfg.index_max_game_points), (0, 15))
        repo = load_troll_config(str(_REPO_YAML))
        self.assertEqual((repo.index_prior_games, repo.index_max_game_points), (2, 30))


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
        self.assertEqual(troll.troll_games, 1)  # LA2_1 (1/12/2 sin más) ya no es trolleada
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
        self.assertAlmostEqual(by_name["Grinder"].average, by_name["Grinder"].points / 18)
        embed = self.service.build_standings_embed(rows, "week")
        self.assertIn("Índice", embed.footer.text)
        self.assertTrue(embed.description.startswith("👑 **Heavy**"))

    def _service_with(self, **index) -> TrollService:
        cfg = replace(TrollConfig.default(), **index)
        settings = SimpleNamespace(timezone=_TZ, general_channel_id=999)
        return TrollService(self.store, TrollDetector(cfg), settings)  # type: ignore[arg-type]

    def test_single_lucky_game_does_not_crown_you(self):
        # "Suerte": 1 partida con alerta justa. "Constante": 6 partidas, todas feas.
        self._save(match_id="LA2_S0", discord_id=1, game_name="Suerte", kills=1, deaths=12, assists=2)
        for i in range(6):
            self._save(match_id=f"LA2_C{i}", discord_id=2, game_name="Constante", kills=1, deaths=11, assists=2)
        for i in range(6):
            self._save(match_id=f"LA2_L{i}", discord_id=3, game_name="Limpio")
        rows = asyncio.run(self.service.standings("week"))
        by_name = {s.display_name: s for s in rows}
        self.assertGreater(by_name["Suerte"].average, by_name["Constante"].average)  # crudo: gana Suerte
        self.assertEqual(rows[0].display_name, "Constante")  # suavizado: gana el constante
        self.assertEqual(rows[-1].display_name, "Limpio")  # los limpios siempre al fondo
        # Con prior_games 0 vuelve a ser el promedio puro.
        pure = asyncio.run(self._service_with(index_prior_games=0).standings("week"))
        self.assertEqual(pure[0].display_name, "Suerte")

    def test_one_monster_game_is_capped(self):
        monster = dict(kills=0, deaths=25, assists=0, items_sold=9, deaths_before_10=6,
                       deaths_to_lane_opponent=10, gold_diff_15=-6000, time_dead_seconds=900,
                       gave_first_blood=True, first_death_minute=1, queue_id=440)
        self._save(match_id="LA2_X", discord_id=1, game_name="Monstruo", **monster)
        [row] = asyncio.run(self._service_with(index_max_game_points=10, index_prior_games=0).standings("week"))
        self.assertGreater(row.points, 10)
        self.assertEqual(row.index, 10)  # en el índice la partida vale como mucho el tope

    def test_trend_and_tiers(self):
        feeder = dict(kills=1, deaths=12, assists=2)
        self._save(match_id="LA2_T1", discord_id=1, game_name="Sube", **feeder)
        self._save(match_id="LA2_T0", discord_id=1, game_name="Sube",
                   game_creation=self.week_start - timedelta(days=2))
        self._save(match_id="LA2_N1", discord_id=2, game_name="Nuevo", **feeder)
        rows = {s.display_name: s for s in asyncio.run(self.service.standings("week"))}
        self.assertIsNotNone(rows["Sube"].previous_index)
        self.assertTrue(self.service.trend(rows["Sube"]).startswith("📈"))
        self.assertEqual(self.service.trend(rows["Nuevo"]), "🆕")
        self.assertEqual(self.service.tier(0), "😇 Santo")
        self.assertEqual(self.service.tier(2), "😬 Sospechoso")
        self.assertEqual(self.service.tier(6), "🤡 Troll")
        self.assertEqual(self.service.tier(8), "💀 Leyenda troll")

    def test_reset_ranking_starts_from_zero_and_persists(self):
        self._save(match_id="LA2_OLD", discord_id=1, game_name="Viejo", kills=1, deaths=15, assists=2,
                   game_creation=self.week_start + timedelta(minutes=10))
        self._save(match_id="LA2_NEW", discord_id=2, game_name="Nuevo", kills=1, deaths=12, assists=2,
                   game_creation=self.week_start + timedelta(hours=3))
        state = Path(tempfile.mkdtemp()) / "sub" / "trolls_state.json"
        settings = SimpleNamespace(timezone=_TZ, general_channel_id=999)
        service = TrollService(self.store, _detector(), settings, state_file=str(state))  # type: ignore[arg-type]
        asyncio.run(service.reset_ranking(self.week_start + timedelta(hours=1)))
        for period in ("week", "all"):
            names = [s.display_name for s in asyncio.run(service.standings(period))]
            self.assertEqual(names, ["Nuevo"])
        # Sobrevive un reinicio del bot (se relee del archivo).
        again = TrollService(self.store, _detector(), settings, state_file=str(state))  # type: ignore[arg-type]
        self.assertEqual(again.reset_at, service.reset_at)
        self.assertIn("Cuenta desde", again.build_standings_embed([], "week").footer.text)

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
        self.assertIsNone(content)  # el detalle no etiqueta: la mención va en #general
        self.assertEqual(embed.color.value, 0xE67E22)  # naranja: trolleada común
        self.assertIn("Pepe", embed.title)

        papelon = self.service.evaluate(_papelon_record(discord_id=7, game_name="Pepe"))
        _, embed = self.service.build_alert(papelon, None, 4)
        self.assertEqual(embed.color.value, discord_dark_red())
        self.assertLessEqual(embed.fields[0].value.count("\n"), 5)  # máx 5 cargos + "…y N más"
        self.assertIn("ranked", embed.footer.text)

    def test_general_line_is_short_and_anecdotal(self):
        base = self.service.evaluate(record(
            discord_id=7, champion="Lee Sin", kills=1, deaths=12, assists=2,
            base_absent=2, base_absent_farming="jungla"))
        line = self.service.build_general_line(base)
        self.assertIn("<@7>", line)
        self.assertIn("nos tiraban la base y estaba farmeando la jungla", line)  # la anécdota va primero
        self.assertIn("(Lee Sin 1/12/2)", line)
        self.assertNotIn("\n", line)
        self.assertLess(len(line), 250)
        self.assertEqual(self.service.build_general_line(base), line)  # estable
        historic = self.service.build_general_line(self.service.evaluate(_papelon_record(discord_id=7)))
        self.assertTrue(historic.startswith("💀"))

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
