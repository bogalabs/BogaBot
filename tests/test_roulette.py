"""Tests de la ruleta (lógica pura y parseo de Data Dragon). Sin red."""
from __future__ import annotations

import random
import sys
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bogabot.modules.predictions.market import Bet  # noqa: E402
from bogabot.modules.roulette.ddragon import parse_champions, parse_items  # noqa: E402
from bogabot.modules.roulette.logic import (CURSED_WEIGHT, ROLES, Champion, Item, Spin, assign_roles,  # noqa: E402
                                            comfort_champions, item_themes, make_pick, pick_build, pick_champion,
                                            role_weight, win_market)

NOW = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc)


def _item(item_id: str, name: str, *tags: str) -> Item:
    return Item(id=item_id, name=name, tags=frozenset(tags))


CHAMPS = [
    Champion("Garen", "Garen", ("Fighter", "Tank")),
    Champion("Lux", "Lux", ("Mage", "Support")),
    Champion("Jinx", "Jinx", ("Marksman",)),
    Champion("Yuumi", "Yuumi", ("Support", "Mage")),
    Champion("Zed", "Zed", ("Assassin",)),
    Champion("Leona", "Leona", ("Tank", "Support")),
]
ITEMS = [
    _item("3089", "Rabadon", "SpellDamage"), _item("3135", "Vacío", "SpellDamage", "MagicPenetration"),
    _item("4645", "Lumbre", "SpellDamage", "MagicPenetration"), _item("3157", "Zhonya", "Armor", "SpellDamage"),
    _item("3031", "Filo", "CriticalStrike", "Damage"), _item("3036", "Dominik", "Damage", "CriticalStrike"),
    _item("3033", "Recordatorio", "Damage", "CriticalStrike"), _item("3046", "Bailarín", "CriticalStrike"),
    _item("3075", "Espinas", "Health", "Armor"), _item("3083", "Warmog", "Health"),
    _item("6695", "Colmillo", "Damage", "ArmorPenetration"),
    _item("3107", "Redención", "SpellDamage", "ManaRegen"),
]
BOOTS = [_item("3020", "Hechicero", "Boots"), _item("3006", "Berserker", "Boots")]


class TestThemes(unittest.TestCase):
    def test_item_themes(self):
        self.assertEqual(item_themes(_item("1", "x", "SpellDamage")), {"ap"})
        self.assertEqual(item_themes(_item("1", "x", "Health", "Armor")), {"tank"})
        self.assertEqual(item_themes(_item("1", "x", "Damage", "CriticalStrike")), {"crit"})
        self.assertIn("lethality", item_themes(_item("1", "x", "Damage", "ArmorPenetration")))
        self.assertIn("enchanter", item_themes(_item("1", "x", "SpellDamage", "ManaRegen")))
        self.assertNotIn("ap", item_themes(_item("1", "x", "SpellDamage", "ManaRegen")))

    def test_role_weight(self):
        garen, yuumi = CHAMPS[0], CHAMPS[3]
        self.assertEqual(role_weight(garen, "TOP"), 10)
        self.assertEqual(role_weight(garen, "UTILITY"), 2)  # Tank secundario: la mitad
        self.assertEqual(role_weight(yuumi, "JUNGLE"), CURSED_WEIGHT)


class TestRoles(unittest.TestCase):
    def test_unique_roles(self):
        roles = assign_roles([1, 2, 3, 4, 5], {}, "normal", random.Random(1))
        self.assertEqual(sorted(roles.values()), sorted(ROLES))
        self.assertEqual(len(assign_roles([1, 2], {}, "normal", random.Random(1))), 2)

    def test_out_of_zone_avoids_main_role(self):
        counts = Counter(assign_roles([1], {1: ["MIDDLE"] * 30}, "fuera", random.Random(i))[1] for i in range(300))
        normal = Counter(assign_roles([1], {1: ["MIDDLE"] * 30}, "normal", random.Random(i))[1] for i in range(300))
        self.assertLess(counts["MIDDLE"], 10)
        self.assertGreater(normal["MIDDLE"], 30)


class TestPicks(unittest.TestCase):
    def test_champion_fits_role_mostly(self):
        rng = random.Random(3)
        picks = Counter(pick_champion("BOTTOM", CHAMPS, rng).id for _ in range(500))
        self.assertEqual(picks.most_common(1)[0][0], "Jinx")

    def test_comfort_taken_and_worst(self):
        rng = random.Random(4)
        comfort = comfort_champions(["Jinx"] * 5 + ["Lux"] * 2)
        self.assertEqual(comfort, {"jinx"})  # Lux tiene pocas partidas
        picks = Counter(pick_champion("BOTTOM", CHAMPS, rng, comfort=comfort).id for _ in range(500))
        self.assertLess(picks["Jinx"], 450)
        self.assertNotEqual(pick_champion("BOTTOM", CHAMPS, rng, taken={"Jinx"}).id, "Jinx")
        for _ in range(30):
            self.assertEqual(role_weight(pick_champion("JUNGLE", CHAMPS, rng, worst=True), "JUNGLE"), CURSED_WEIGHT)

    def test_build(self):
        rng = random.Random(5)
        theme, boots, items = pick_build(CHAMPS[1], ITEMS, BOOTS, rng, cursed=False)
        self.assertEqual(theme, "ap")
        self.assertEqual(boots.name, "Hechicero")
        self.assertIn(len(items), (3, 4))
        self.assertEqual(len({i.name for i in items}), len(items))
        theme, _, _ = pick_build(CHAMPS[1], ITEMS, BOOTS, rng, cursed=True)
        self.assertIn(theme, ("crit", "lethality"))  # Lux maldita: AD

    def test_make_pick_punished(self):
        pick = make_pick("UTILITY", CHAMPS, ITEMS, BOOTS, random.Random(6), worst=True)
        self.assertTrue(pick.cursed and pick.punished)
        self.assertEqual(role_weight(next(c for c in CHAMPS if c.id == pick.champion_id), "UTILITY"), CURSED_WEIGHT)


class TestSpin(unittest.TestCase):
    def test_complied_and_roundtrip(self):
        pick = make_pick("BOTTOM", CHAMPS, ITEMS, BOOTS, random.Random(7))
        spin = Spin(id="ab", created_at=NOW, mode="normal", picks={1: pick}, market=win_market([1]))
        self.assertTrue(spin.complied(1, pick.champion_id.upper(), "BOTTOM"))  # sin importar mayúsculas
        self.assertFalse(spin.complied(1, pick.champion_id, "TOP"))
        self.assertFalse(spin.complied(2, pick.champion_id, "BOTTOM"))
        self.assertIsNotNone(spin.market.bet_error(1, "no"))  # el que juega no apuesta a perder
        spin.market.bets[3] = Bet("yes", 20)
        spin.paid, spin.resolved, spin.match_id = [1], True, "LA2_1"
        self.assertEqual(Spin.from_dict(spin.to_dict()), spin)


class TestDDragonParse(unittest.TestCase):
    def test_parse(self):
        champs = parse_champions({"data": {"Ahri": {"id": "Ahri", "name": "Ahri", "tags": ["Mage", "Assassin"]}}})
        self.assertEqual(champs, [Champion("Ahri", "Ahri", ("Mage", "Assassin"))])

        def raw(name, tags, depth=3, gold=3000, into=None, **extra):
            d = {"name": name, "tags": tags, "depth": depth, "gold": {"total": gold, "purchasable": True},
                 "maps": {"11": True}}
            if into:
                d["into"] = into
            d.update(extra)
            return d

        data = {"data": {
            "3089": raw("Rabadon ", ["SpellDamage"]),
            "323089": raw("Rabadon variante", ["SpellDamage"]),
            "1058": raw("Vara", ["SpellDamage"], depth=1, gold=1250, into=["3089"]),
            "3020": raw("Hechicero", ["Boots"], depth=2, gold=1100, into=["3175"]),
            "3175": raw("Hechicero 3", ["Boots"], depth=3, gold=1100),
            "2003": raw("Poción", ["Consumable"], depth=None, gold=50),
            "9999": raw("Solo ARAM", ["SpellDamage"], maps={"11": False, "12": True}),
            "3600": raw("De Kalista", ["Damage"], requiredChampion="Kalista"),
        }}
        items, boots = parse_items(data)
        self.assertEqual([i.name for i in items], ["Rabadon"])
        self.assertEqual([b.name for b in boots], ["Hechicero"])


if __name__ == "__main__":
    unittest.main()
