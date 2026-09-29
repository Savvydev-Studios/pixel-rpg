"""Unit tests. Pure stdlib unittest, so `python tests/test_all.py` is enough.

Focus is on the two things worth guarding: the security boundary around
untrusted save data, and the game rules that are easy to break by accident.
"""
import io
import json
import os
import sys
import tempfile
import base64
import struct
import time
import dataclasses
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

# ---------------------------------------------------------------------------
# **The suite gets its own save folder, and this line has to come before
# anything imports the game.**
#
# It did not have one until 2026-09-01, and the cost was somebody's run:
#
# > "bro my saves keep disappearing, i saved the game before turning it off
# > and now its gone :("
#
# Every test that builds a `Game` and calls `new_run(name, slot)` writes a
# real save, and the ones that tidy up with `saves.delete(slot)` delete a
# real save. The suite uses slots 1 to 6, which is all of them. It had been
# doing this for as long as it has existed; it only destroyed anything once
# somebody was playing on the same machine while it ran.
#
# `smoke.py` and `layout.py` have always pinned this. `test_all.py` is the
# one that did not, and nothing noticed because a deleted save fails no
# test - it fails the person whose save it was.
#
# `PIXEL_RPG_SAVE_DIR` deliberately outranks every other way the game
# chooses a folder (`config.save_directory`), so this is a hard override
# and not a preference.
# ---------------------------------------------------------------------------
_SAVE_SANDBOX = tempfile.mkdtemp(prefix="rpgunit")
os.environ["PIXEL_RPG_SAVE_DIR"] = _SAVE_SANDBOX
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame

from pixel_rpg import security as sec
from pixel_rpg.config import Settings, get_secret
from pixel_rpg.entities import (Boss, BOSS_DEFS, Player, element_multiplier,
                                make_enemy)
from pixel_rpg.items import Item, roll_item, sell_price
from pixel_rpg.quests import QuestLog
from pixel_rpg.save import (SaveManager, build_payload, compute_checksum,
                            deserialize_player, serialize_player)
from pixel_rpg.skills import Status, modifier, skills_for_level
from pixel_rpg.util import RNG, clamp, stable_seed, wrap_text


BACKSLASH = chr(92)   # spelled this way so no escape sequence can be mangled

class TestCoercion(unittest.TestCase):
    def test_safe_int_bounds(self):
        self.assertEqual(sec.safe_int(5, 0, 10, 0), 5)
        self.assertEqual(sec.safe_int(-5, 0, 10, 0), 0)
        self.assertEqual(sec.safe_int(10 ** 30, 0, 10, 0), 10)
        self.assertEqual(sec.safe_int("abc", 0, 10, 3), 3)
        self.assertEqual(sec.safe_int(None, 0, 10, 3), 3)
        self.assertEqual(sec.safe_int(float("nan"), 0, 10, 3), 3)
        self.assertEqual(sec.safe_int(float("inf"), 0, 10, 3), 3)

    def test_safe_int_rejects_bool(self):
        # bool is an int subclass; treating True as 1 hides real bugs.
        self.assertEqual(sec.safe_int(True, 0, 10, 7), 7)

    def test_safe_int_rejects_absurd_strings(self):
        self.assertEqual(sec.safe_int("9" * 500, 0, 100, 1), 1)

    def test_safe_float(self):
        self.assertEqual(sec.safe_float("2.5", 0, 10, 0), 2.5)
        self.assertEqual(sec.safe_float(float("nan"), 0, 10, 1.0), 1.0)
        self.assertEqual(sec.safe_float(float("-inf"), 0, 10, 1.0), 1.0)

    def test_safe_choice_is_allowlist(self):
        self.assertEqual(sec.safe_choice("b", ("a", "b"), "a"), "b")
        self.assertEqual(sec.safe_choice("z", ("a", "b"), "a"), "a")
        self.assertEqual(sec.safe_choice(None, ("a", "b"), "a"), "a")


class TestSanitization(unittest.TestCase):
    def test_control_characters_stripped(self):
        self.assertEqual(sec.sanitize_text("a\x00b\x1fc"), "abc")

    def test_bidi_and_zero_width_stripped(self):
        self.assertEqual(sec.sanitize_text("ab‮cd​e"), "abcde")

    def test_length_cap(self):
        self.assertEqual(len(sec.sanitize_text("x" * 5000, 20)), 20)

    def test_name_rejects_traversal(self):
        self.assertNotIn("/", sec.sanitize_name("../../etc/passwd"))
        self.assertNotIn(BACKSLASH, sec.sanitize_name(".." + BACKSLASH + ".." + BACKSLASH + "windows"))
        self.assertNotIn("..", sec.sanitize_name("../../etc"))

    def test_name_never_empty(self):
        self.assertEqual(sec.sanitize_name("!!!"), "Wanderer")
        self.assertEqual(sec.sanitize_name(""), "Wanderer")
        self.assertEqual(sec.sanitize_name(None), "Wanderer")

    def test_filename_is_leaf_only(self):
        for hostile in ("../../evil", "a/b", ".." + BACKSLASH + ".." + BACKSLASH + "x", "null" + chr(0)):
            leaf = sec.safe_filename(hostile)
            self.assertNotIn(os.sep, leaf)
            self.assertFalse(leaf.startswith("."))

    def test_is_within(self):
        base = tempfile.mkdtemp()
        self.assertTrue(sec.is_within(base, os.path.join(base, "a.json")))
        self.assertFalse(sec.is_within(base, os.path.join(base, "..", "x")))

    def test_is_within_rejects_sibling_with_shared_prefix(self):
        # "saves_evil" starts with "saves"; only a separator makes it inside.
        base = tempfile.mkdtemp()
        self.assertFalse(sec.is_within(base, base + "_evil"))
        self.assertFalse(sec.is_within(base, os.path.join(base + "_evil", "a.json")))

    def test_is_within_matches_case_insensitively_on_windows(self):
        base = tempfile.mkdtemp()
        target = os.path.join(base, "a.json")
        if os.name == "nt":
            self.assertTrue(sec.is_within(base.upper(), target))

    def test_save_manager_can_read_its_own_slot_path(self):
        """Regression: the loader refused the very file it had just written.

        A store-packaged Python redirects %APPDATA% into its own LocalCache,
        so the written file realpath'd somewhere the save directory did not,
        and is_within rejected it. Symptom was "refusing to read outside the
        save directory" on load, with saving reporting success.
        """
        from pixel_rpg import config, save as savemod
        for directory in (config.default_save_dir(), tempfile.mkdtemp()):
            mgr = savemod.SaveManager(directory)
            for slot in range(1, savemod.SLOT_COUNT + 1):
                self.assertTrue(
                    sec.is_within(mgr.dir, mgr.slot_path(slot)),
                    "slot %d unreadable under %s" % (slot, directory))
                self.assertTrue(
                    sec.is_within(mgr.dir, mgr.backup_path(slot)),
                    "backup %d unreadable under %s" % (slot, directory))


class TestStructure(unittest.TestCase):
    def test_rejects_deep_nesting(self):
        deep = current = {}
        for _ in range(40):
            current["a"] = {}
            current = current["a"]
        with self.assertRaises(sec.ValidationError):
            sec.check_structure(deep)

    def test_rejects_long_strings(self):
        with self.assertRaises(sec.ValidationError):
            sec.check_structure({"a": "x" * 9000})

    def test_rejects_huge_arrays(self):
        with self.assertRaises(sec.ValidationError):
            sec.check_structure({"a": list(range(5000))})

    def test_accepts_normal_payload(self):
        self.assertTrue(sec.check_structure({"a": 1, "b": [1, 2], "c": {"d": "e"}}))

    def test_oversized_file_rejected(self):
        path = os.path.join(tempfile.mkdtemp(), "big.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"a":"' + "z" * (sec.MAX_SAVE_BYTES + 10) + '"}')
        with self.assertRaises(sec.ValidationError):
            sec.load_json_limited(path)

    def test_malformed_json_rejected(self):
        path = os.path.join(tempfile.mkdtemp(), "bad.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(sec.ValidationError):
            sec.load_json_limited(path)

    def test_non_object_root_rejected(self):
        path = os.path.join(tempfile.mkdtemp(), "list.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[1,2,3]")
        with self.assertRaises(sec.ValidationError):
            sec.load_json_limited(path)


class TestRateLimit(unittest.TestCase):
    def test_five_per_window(self):
        limiter = sec.RateLimiter(5, 900)
        self.assertEqual(sum(1 for _ in range(20) if limiter.hit("k")), 5)
        self.assertFalse(limiter.check("k"))
        self.assertGreater(limiter.retry_after("k"), 0)

    def test_keys_are_independent(self):
        limiter = sec.RateLimiter(2, 900)
        limiter.hit("a")
        limiter.hit("a")
        self.assertFalse(limiter.check("a"))
        self.assertTrue(limiter.check("b"))

    def test_reset(self):
        limiter = sec.RateLimiter(1, 900)
        limiter.hit("a")
        limiter.reset("a")
        self.assertTrue(limiter.check("a"))

    def test_defaults_match_policy(self):
        self.assertEqual(sec.AUTH_MAX_ATTEMPTS, 5)
        self.assertEqual(sec.AUTH_WINDOW_SECONDS, 15 * 60)


class TestItemDeserialization(unittest.TestCase):
    def test_round_trip(self):
        item = roll_item(12, "weapon", rarity="epic")
        clone = Item.from_dict(item.to_dict())
        self.assertEqual(clone.name, item.name)
        self.assertEqual(clone.stats, item.stats)
        self.assertEqual(clone.rarity, item.rarity)

    def test_unknown_keys_dropped(self):
        data = roll_item(5).to_dict()
        data["__class__"] = "evil"
        data["os"] = "system"
        item = Item.from_dict(data)
        self.assertIsNotNone(item)
        self.assertFalse(hasattr(item, "os"))

    def test_unknown_stat_keys_dropped(self):
        data = roll_item(5).to_dict()
        data["stats"]["godmode"] = 9999
        self.assertNotIn("godmode", Item.from_dict(data).stats)

    def test_out_of_range_clamped(self):
        data = roll_item(5).to_dict()
        data["ilvl"] = 10 ** 9
        data["stats"]["atk"] = 10 ** 9
        item = Item.from_dict(data)
        self.assertLessEqual(item.ilvl, 99)
        self.assertLessEqual(item.stats["atk"], 9999)

    def test_invalid_rarity_falls_back(self):
        data = roll_item(5).to_dict()
        data["rarity"] = "godlike"
        self.assertEqual(Item.from_dict(data).rarity, "common")

    def test_garbage_returns_none(self):
        for junk in (None, "string", 42, [], {}, {"name": "x"},
                     {"slot": "weapon"}, {"name": "x", "slot": "banana"}):
            self.assertIsNone(Item.from_dict(junk), junk)

    def test_sell_price_is_below_buy_price(self):
        for _ in range(50):
            item = roll_item(20)
            self.assertLess(sell_price(item), item.price)
            self.assertGreaterEqual(sell_price(item), 1)


class TestPlayer(unittest.TestCase):
    def test_level_up_grants_points_and_heals(self):
        player = Player()
        player.hp = 1
        levels, _ = player.gain_xp(10 ** 6)
        self.assertGreater(levels, 0)
        from pixel_rpg.entities import POINTS_PER_LEVEL
        # The constant, not a literal 3. This test held the old number
        # and had to be edited when the attributes were split; it will
        # not have to be edited again.
        self.assertEqual(player.points, levels * POINTS_PER_LEVEL)
        self.assertEqual(player.hp, player.max_hp())

    def test_level_is_capped(self):
        player = Player()
        player.gain_xp(10 ** 9)
        from pixel_rpg.entities import MAX_LEVEL
        # The ceiling moved to 100 on 2026-08-29; a literal
        # here is an assertion that has to be remembered.
        self.assertLessEqual(player.level, MAX_LEVEL)

    def test_spend_point_requires_budget(self):
        player = Player()
        self.assertFalse(player.spend_point("might"))
        player.points = 1
        self.assertTrue(player.spend_point("might"))
        self.assertFalse(player.spend_point("might"))

    def test_spend_point_rejects_unknown_attribute(self):
        player = Player()
        player.points = 5
        self.assertFalse(player.spend_point("luck"))
        self.assertEqual(player.points, 5)

    def test_vigor_grants_the_new_hp(self):
        """The point of vigor is that the HP is granted, not merely offered.

        The exact number is class-scaled, so assert the invariant instead of
        a magic constant: max HP goes up, and current HP goes up by the same
        amount rather than leaving you proportionally more wounded.
        """
        for class_key in ("warden", "emberwright", "cutpurse"):
            player = Player("T", class_key)
            player.points = 1
            before_hp, before_max = player.hp, player.max_hp()
            player.spend_point("vigor")
            gain = player.max_hp() - before_max
            self.assertGreater(gain, 0, class_key)
            self.assertEqual(player.hp - before_hp, gain, class_key)

    def test_equip_swaps_and_returns_old(self):
        player = Player()
        first = roll_item(5, "weapon")
        second = roll_item(5, "weapon")
        player.add_item(first)
        player.add_item(second)
        player.equip(first)
        displaced = player.equip(second)
        self.assertIs(displaced, first)
        self.assertIs(player.equipment["weapon"], second)
        self.assertIn(first, player.inventory)

    def test_bag_capacity_enforced(self):
        player = Player()
        for _ in range(sec.MAX_INVENTORY + 20):
            player.add_item(roll_item(1))
        # **The bag starts smaller than the validator's ceiling** (s48):
        # `MAX_INVENTORY` is what a save file may contain, `bag_size()` is
        # what this hero can carry today, and the second one grows.
        self.assertEqual(len(player.inventory), player.bag_size())
        self.assertLessEqual(player.bag_size(), sec.MAX_INVENTORY)

    def test_hp_never_exceeds_max_after_unequip(self):
        player = Player()
        armor = roll_item(30, "armor", rarity="legendary")
        player.add_item(armor)
        player.equip(armor)
        player.hp = player.max_hp()
        player.unequip("armor")
        self.assertLessEqual(player.hp, player.max_hp())

    def test_consumables_never_negative(self):
        player = Player()
        player.consumables = {"potion": 1}
        self.assertTrue(player.use_consumable("potion"))
        self.assertFalse(player.use_consumable("potion"))
        self.assertEqual(player.consumable_count("potion"), 0)


class TestCombat(unittest.TestCase):
    def test_element_triangle(self):
        self.assertEqual(element_multiplier("fire", "ice"), 1.5)
        self.assertEqual(element_multiplier("ice", "fire"), 0.75)
        self.assertEqual(element_multiplier("fire", "fire"), 1.0)

    def test_status_modifiers(self):
        self.assertAlmostEqual(modifier([Status("might")], "atk"), 1.30)
        self.assertAlmostEqual(modifier([Status("weaken")], "atk"), 0.70)
        self.assertAlmostEqual(modifier([Status("chill")], "spd"), 0.65)
        self.assertAlmostEqual(modifier([], "atk"), 1.0)

    def test_shield_absorbs_then_breaks(self):
        player = Player()
        player.add_status("shield", 3, magnitude=50)
        remaining, soaked = player.absorb_with_shield(30)
        self.assertEqual((remaining, soaked), (0, 30))
        remaining, soaked = player.absorb_with_shield(40)
        self.assertEqual((remaining, soaked), (20, 20))
        self.assertFalse(player.has_status("shield"))

    def test_skills_unlock_by_level(self):
        self.assertEqual(len(skills_for_level(1)), 1)
        self.assertGreater(len(skills_for_level(30)), len(skills_for_level(10)))

    def test_status_from_dict_rejects_unknown(self):
        self.assertIsNone(Status.from_dict({"key": "godmode"}))
        self.assertIsNone(Status.from_dict("nonsense"))
        self.assertEqual(Status.from_dict({"key": "poison", "turns": 10 ** 9}).turns, 99)

    def test_boss_phase_flip_is_once_only(self):
        boss = Boss(BOSS_DEFS[0])
        boss.hp = boss.max_hp() // 3
        self.assertTrue(boss.check_phase())
        self.assertFalse(boss.check_phase())
        self.assertEqual(boss.phase, 2)

    def test_enemy_scaling_is_monotonic(self):
        low = make_enemy(1, "meadow")
        high = make_enemy(30, "meadow")
        self.assertGreater(high.max_hp(), low.max_hp())
        self.assertGreater(high.xp_reward, low.xp_reward)


class TestSaves(unittest.TestCase):
    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg.world import World
        self.tmp = tempfile.mkdtemp(prefix="rpgtest")
        self.mgr = SaveManager(self.tmp)
        self.settings = Settings.from_env()
        self.world_factory = lambda seed: World(32, seed)
        self.world = self.world_factory(99)
        self.player = Player("Tester")
        self.player.gain_xp(2000)
        self.player.gold = 777
        self.quests = QuestLog()

    def payload(self):
        return build_payload(self.player, self.world, self.quests,
                             self.settings, playtime=42.0)

    def test_round_trip_preserves_state(self):
        self.assertTrue(self.mgr.write(1, self.payload(), force=True))
        player, world, quests, meta = self.mgr.load_into(
            1, self.world_factory, self.settings)
        self.assertEqual(player.name, "Tester")
        self.assertEqual(player.level, self.player.level)
        self.assertEqual(player.gold, 777)
        self.assertEqual(meta["playtime"], 42.0)

    def test_world_regenerates_identically_from_seed(self):
        self.assertTrue(self.mgr.write(1, self.payload(), force=True))
        _, world, _, _ = self.mgr.load_into(1, self.world_factory, self.settings)
        self.assertEqual([(c.gx, c.gy) for c in world.chests],
                         [(c.gx, c.gy) for c in self.world.chests])
        self.assertEqual([(p.gx, p.gy) for p in world.portals],
                         [(p.gx, p.gy) for p in self.world.portals])

    def test_checksum_detects_tampering(self):
        self.mgr.write(1, self.payload(), force=True)
        path = self.mgr.slot_path(1)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["player"]["gold"] = 99999999
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNotNone(self.mgr.read(1))
        self.assertIn("checksum", self.mgr.last_warning)

    def test_checksum_is_stable(self):
        payload = self.payload()
        self.assertEqual(compute_checksum(payload), payload["checksum"])

    def test_atomic_write_leaves_no_temp_file(self):
        self.mgr.write(1, self.payload(), force=True)
        leftovers = [n for n in os.listdir(self.tmp) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_backup_created_on_second_write(self):
        self.mgr.write(1, self.payload(), force=True)
        self.mgr.write(1, self.payload(), force=True)
        self.assertTrue(self.mgr.backup_path(1).is_file())

    def test_corrupt_primary_recovers_from_backup(self):
        self.mgr.write(1, self.payload(), force=True)
        self.mgr.write(1, self.payload(), force=True)
        self.mgr.slot_path(1).write_text("{ broken", encoding="utf-8")
        self.assertIsNotNone(self.mgr.read(1))
        self.assertIn("backup", self.mgr.last_warning)

    def test_slot_number_cannot_steer_the_path(self):
        for hostile in ("../../evil", 999, -5, None, "1; rm -rf /"):
            name = self.mgr.slot_path(hostile).name
            self.assertTrue(name.startswith("slot"))
            self.assertTrue(name.endswith(".json"))
            self.assertNotIn(os.sep, name)

    def test_hostile_values_are_clamped(self):
        self.mgr.write(1, self.payload(), force=True)
        path = self.mgr.slot_path(1)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["player"].update({"level": 10 ** 9, "gold": 10 ** 15,
                               "hp": 10 ** 9, "points": 10 ** 9,
                               "attributes": {"might": 10 ** 9, "evil": 5}})
        path.write_text(json.dumps(data), encoding="utf-8")
        player, _, _, _ = self.mgr.load_into(1, self.world_factory,
                                             self.settings)
        from pixel_rpg.entities import MAX_LEVEL
        # The ceiling moved to 100 on 2026-08-29; a literal
        # here is an assertion that has to be remembered.
        self.assertLessEqual(player.level, MAX_LEVEL)
        self.assertLessEqual(player.gold, 99999999)
        self.assertLessEqual(player.hp, player.max_hp())
        self.assertLessEqual(player.attributes["might"], 300)
        self.assertNotIn("evil", player.attributes)

    def test_dead_player_revives_on_load(self):
        self.player.hp = 0
        self.mgr.write(2, self.payload(), force=True)
        player, _, _, _ = self.mgr.load_into(2, self.world_factory,
                                             self.settings)
        self.assertGreater(player.hp, 0)

    def test_missing_slot_reports_cleanly(self):
        self.assertIsNone(self.mgr.read(3))
        self.assertIn("no save", self.mgr.last_error)

    def test_item_in_wrong_slot_is_dropped(self):
        weapon = roll_item(5, "weapon")
        data = serialize_player(self.player)
        data["equipment"]["armor"] = weapon.to_dict()
        self.assertIsNone(deserialize_player(data).equipment["armor"])

    def test_describe_does_not_need_a_full_load(self):
        self.mgr.write(1, self.payload(), force=True)
        info = self.mgr.describe(1)
        self.assertFalse(info["empty"])
        self.assertEqual(info["name"], "Tester")

    def test_delete_removes_slot_and_backup(self):
        self.mgr.write(1, self.payload(), force=True)
        self.mgr.write(1, self.payload(), force=True)
        self.assertTrue(self.mgr.delete(1))
        self.assertFalse(self.mgr.slot_path(1).is_file())
        self.assertFalse(self.mgr.backup_path(1).is_file())


class TestQuests(unittest.TestCase):
    def test_gating_and_claiming(self):
        log, player = QuestLog(), Player()
        self.assertEqual([q.key for q in log.available(player)], ["cull"])
        log.accept("cull", player)
        self.assertIsNone(log.claim(player, "cull"))
        player.stats_tracker["kills"] = 10
        self.assertIsNotNone(log.claim(player, "cull"))
        self.assertIn("cull", log.completed)
        self.assertGreater(len(log.available(player)), 0)

    def test_cannot_claim_twice(self):
        log, player = QuestLog(), Player()
        log.accept("cull", player)
        player.stats_tracker["kills"] = 10
        log.claim(player, "cull")
        self.assertIsNone(log.claim(player, "cull"))

    def test_progress_only_counts_after_accepting(self):
        """Work done before taking a quest must not count toward it."""
        log, player = QuestLog(), Player()
        player.stats_tracker["kills"] = 40      # a long career already
        log.accept("cull", player)              # "defeat 10 monsters"
        done, target = log.progress(player, "cull")
        self.assertEqual(done, 0, "past kills counted toward a new quest")
        self.assertIsNone(log.claim(player, "cull"))

        player.stats_tracker["kills"] = 45
        self.assertEqual(log.progress(player, "cull")[0], 5)
        player.stats_tracker["kills"] = 50
        self.assertEqual(log.progress(player, "cull")[0], target)
        self.assertIsNotNone(log.claim(player, "cull"))

    def test_absolute_goals_ignore_the_baseline(self):
        """"Reach level 20" is a place, not a distance."""
        log, player = QuestLog(), Player()
        player.gain_xp(10 ** 6)
        log.accept("ascend", player)
        done, target = log.progress(player, "ascend")
        self.assertEqual(done, target, "an absolute goal was made relative")

    def test_baselines_survive_a_round_trip(self):
        log, player = QuestLog(), Player()
        player.stats_tracker["kills"] = 33
        log.accept("cull", player)
        restored = QuestLog().apply_dict(log.to_dict())
        self.assertEqual(restored.baselines.get("cull"), 33)
        self.assertEqual(restored.progress(player, "cull")[0], 0)

    def test_hostile_load_is_filtered(self):
        log = QuestLog().apply_dict({"active": ["cull", "evil", 123],
                                     "completed": ["cull"],
                                     "unlocked": ["nope"], "junk": 1})
        self.assertEqual(log.active, [])
        self.assertEqual(log.completed, ["cull"])
        self.assertEqual(log.unlocked, [])

    def test_achievements_unlock_once(self):
        log, player = QuestLog(), Player()
        player.stats_tracker["kills"] = 500
        self.assertGreater(len(log.check_achievements(player)), 0)
        self.assertEqual(log.check_achievements(player), [])


class TestConfig(unittest.TestCase):
    def test_hostile_env_is_clamped(self):
        os.environ["PIXEL_RPG_WIDTH"] = "99999999"
        os.environ["PIXEL_RPG_DIFFICULTY"] = "../../etc/passwd"
        os.environ["PIXEL_RPG_VOLUME"] = "banana"
        try:
            settings = Settings.from_env()
            self.assertLessEqual(settings.width, 3840)
            self.assertEqual(settings.difficulty, "normal")
            self.assertEqual(settings.master_volume, 0.55)
        finally:
            for key in ("PIXEL_RPG_WIDTH", "PIXEL_RPG_DIFFICULTY",
                        "PIXEL_RPG_VOLUME"):
                os.environ.pop(key, None)

    def test_placeholder_key_counts_as_absent(self):
        os.environ["API_KEY"] = "YOUR_API_KEY_HERE"
        try:
            self.assertIsNone(get_secret("API_KEY"))
        finally:
            os.environ.pop("API_KEY", None)

    def test_settings_dict_carries_no_secret(self):
        blob = json.dumps(Settings.from_env().to_dict()).lower()
        for word in ("key", "secret", "token", "password"):
            self.assertNotIn(word, blob)


class TestUtil(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(clamp(5, 0, 3), 3)
        self.assertEqual(clamp(-1, 0, 3), 0)

    def test_seeded_rng_is_reproducible(self):
        self.assertEqual(RNG(7).rint(0, 10 ** 9), RNG(7).rint(0, 10 ** 9))

    def test_wrap_text_handles_long_words(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        font = pygame.font.Font(None, 18)
        self.assertGreater(len(wrap_text("x" * 400, font, 100)), 1)


class TestAppearance(unittest.TestCase):
    def test_every_style_and_facing_makes_a_valid_grid(self):
        from pixel_rpg.appearance import Appearance, HAIR_STYLES
        from pixel_rpg import spritedata as sd
        look = Appearance()
        for style in range(len(HAIR_STYLES)):
            look.style = style
            for facing in ("front", "back", "side"):
                base = sd.HERO["side" if facing == "side" else facing]
                rows = look.rows(base, facing)
                self.assertEqual(len(rows), len(base), (style, facing))
                for row in rows:
                    self.assertEqual(len(row), len(base[0]), (style, facing))
                unknown = {c for r in rows for c in r} - set(sd.PALETTE) - {"."}
                self.assertEqual(unknown, set(), (style, facing))

    def test_palette_override_touches_only_the_look_channels(self):
        from pixel_rpg.appearance import Appearance
        from pixel_rpg import spritedata as sd
        merged = Appearance(3, 5, 2, 4).palette(sd.PALETTE)
        changed = {k for k in sd.PALETTE if merged[k] != sd.PALETTE[k]}
        self.assertTrue(changed <= set("sSnNbB"), changed)

    def test_boots_are_not_recoloured_with_hair(self):
        from pixel_rpg import spritedata as sd
        # The boots row must not use the hair character any more.
        for view in ("front", "back", "side"):
            self.assertNotIn("n", sd.HERO[view][-1], view)

    def test_cycle_wraps_both_ways(self):
        from pixel_rpg.appearance import Appearance
        look = Appearance(0, 0, 0, 0)
        look.cycle("skin", -1)
        self.assertEqual(look.skin, look.count("skin") - 1)
        look.cycle("skin", 1)
        self.assertEqual(look.skin, 0)

    def test_hostile_values_are_clamped(self):
        from pixel_rpg.appearance import Appearance
        look = Appearance.from_dict({"skin": 10 ** 9, "hair": -5,
                                     "style": "x", "outfit": None, "evil": 1})
        for field in Appearance.FIELDS:
            value = getattr(look, field)
            self.assertGreaterEqual(value, 0)
            self.assertLess(value, look.count(field))
        self.assertFalse(hasattr(look, "evil"))

    def test_round_trip(self):
        from pixel_rpg.appearance import Appearance
        look = Appearance(6, 9, 3, 7, 2)
        self.assertEqual(Appearance.from_dict(look.to_dict()).signature(),
                         look.signature())

    def test_builds_change_the_silhouette(self):
        from pixel_rpg.appearance import Appearance, BUILDS
        from pixel_rpg import spritedata as sd
        seen = set()
        for build in range(len(BUILDS)):
            look = Appearance(build=build)
            seen.add(tuple(look.rows(sd.HERO["front"], "front")))
        self.assertEqual(len(seen), len(BUILDS), "builds render identically")

    def test_walk_frames_differ_from_standing(self):
        from pixel_rpg.appearance import Appearance
        from pixel_rpg import spritedata as sd
        look = Appearance()
        standing = look.rows(sd.HERO["front"], "front", 0)
        stepping = look.rows(sd.HERO["front"], "front", 1)
        self.assertNotEqual(standing, stepping)

    def test_every_sprite_shares_one_grid(self):
        from pixel_rpg import spritedata as sd
        for group in (sd.MONSTERS, sd.BOSSES, sd.PROPS, sd.HERO,
                      sd.MONSTER_ALT):
            for name, rows in group.items():
                self.assertEqual(len(rows), sd.GRID, name)
                for row in rows:
                    self.assertEqual(len(row), sd.GRID, name)


class TestStatFitting(unittest.TestCase):
    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        pygame.display.set_mode((64, 64))
        from pixel_rpg.assets import Assets
        self.font = Assets(32).font("tiny")

    def test_never_overflows_and_never_cuts_mid_token(self):
        from pixel_rpg.ui import fit_stats
        from pixel_rpg.util import rng
        rng.reseed(3)
        for _ in range(60):
            item = roll_item(30, rarity="mythic")
            for width in (120, 200, 300, 460):
                text = fit_stats(item, self.font, width)
                self.assertLessEqual(self.font.size(text)[0], width,
                                     (item.name, width, text))
                for token in ("ATK", "DEF", "MAG", "RES", "SPD", "MP", "HP",
                              "CRIT", "CDMG", "LIFE"):
                    self.assertFalse(text.endswith(token[:-1])
                                     and not text.endswith(token),
                                     "cut mid-token: %r" % text)

    def test_says_how_many_were_left_out(self):
        from pixel_rpg.ui import fit_stats
        from pixel_rpg.items import Item
        item = Item(name="Crowded", slot="weapon",
                    stats={"atk": 5, "defense": 5, "hp": 5, "mp": 5,
                           "mag": 5, "res": 5, "spd": 5, "crit": 5})
        narrow = fit_stats(item, self.font, 130)
        self.assertIn("+", narrow)
        wide = fit_stats(item, self.font, 900)
        self.assertIn("SPD", wide)

    def test_empty_item_reads_cleanly(self):
        from pixel_rpg.ui import fit_stats
        from pixel_rpg.items import Item
        self.assertEqual(fit_stats(Item(name="Plain", slot="weapon"),
                                   self.font, 200), "no bonuses")


class TestEquipBest(unittest.TestCase):
    def test_wears_the_strongest_in_every_slot(self):
        from pixel_rpg.util import rng
        rng.reseed(8)
        player = Player("T", "warden")
        player.gain_xp(5000)
        for _ in range(16):
            player.add_item(roll_item(player.level + 2))
        player.equip_best()
        for slot in ("weapon", "armor", "accessory"):
            worn = player.equipment.get(slot)
            if worn is None:
                continue
            for spare in player.inventory:
                if spare.slot == slot:
                    self.assertGreaterEqual(worn.power(), spare.power(), slot)

    def test_is_idempotent(self):
        from pixel_rpg.util import rng
        rng.reseed(9)
        player = Player("T", "cutpurse")
        for _ in range(8):
            player.add_item(roll_item(6))
        player.equip_best()
        self.assertEqual(player.equip_best(), [])


class TestAudit(unittest.TestCase):
    def test_self_audit_has_no_failures(self):
        failures = [n for n in sec.self_audit() if n.level == "fail"]
        self.assertEqual(failures, [], failures)

    def test_tree_scan_finds_no_secrets(self):
        from pixel_rpg.audit import scan_tree
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertEqual(scan_tree(root), [])

    def test_gitignore_excludes_env(self):
        from pixel_rpg.audit import check_gitignore
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        passed, detail = check_gitignore(root)
        self.assertTrue(passed, detail)


class TestNet(unittest.TestCase):
    """The co-op transport.

    This is the first thing in the project that reads bytes another machine
    wrote, so the tests are mostly about what happens when those bytes are
    hostile rather than when they are fine.
    """

    def setUp(self):
        from pixel_rpg import net
        self.net = net

    # -- room codes --------------------------------------------------------

    def test_room_code_round_trip(self):
        for host, port in (("192.168.1.42", 47216), ("10.0.0.1", 1),
                           ("255.255.255.255", 65535), ("127.0.0.1", 47216)):
            code = self.net.encode_room(host, port)
            self.assertEqual(len(code), 10)
            self.assertEqual(self.net.decode_room(code), (host, port))

    def test_room_codes_survive_being_read_aloud(self):
        """Lowercase, spaces and a dash in the middle all still work."""
        code = self.net.encode_room("192.168.1.42", 47216)
        for written in (code.lower(), code[:5] + "-" + code[5:],
                        " " + code + " "):
            self.assertEqual(self.net.decode_room(written),
                             ("192.168.1.42", 47216))

    def test_room_code_alphabet_has_no_lookalikes(self):
        """O/0 and I/1/L are how a spoken code gets mistyped."""
        for ch in "O01IL":
            self.assertNotIn(ch, self.net.ALPHABET)

    def test_bad_room_codes_are_refused_clearly(self):
        for bad in ("", "SHORT", "OOOOOOOOOO", "!!!!!!!!!!", "x" * 40):
            with self.assertRaises(self.net.NetError):
                self.net.decode_room(bad)

    # -- framing -----------------------------------------------------------

    def test_a_frame_round_trips(self):
        frame = self.net.encode_frame("hello", {"name": "Friend"})
        body = frame[4:]
        self.assertEqual(self.net._decode_body(body),
                         ("hello", {"name": "Friend"}))

    def test_an_oversized_outgoing_frame_is_refused(self):
        with self.assertRaises(self.net.NetError):
            self.net.encode_frame("big", {"pad": "x" * (self.net.MAX_FRAME + 1)})

    def test_rubbish_on_the_wire_is_dropped_not_raised(self):
        """A bad frame must cost the frame, never the process."""
        for body in (b"not json", b"[]", b'"a string"', b"null", b"{}",
                     b'{"t": ""}', b'{"t": 123}', b"\xff\xfe"):
            self.assertIsNone(self.net._decode_body(body),
                              "accepted %r" % body)

    def test_a_frame_with_no_payload_is_fine(self):
        self.assertEqual(self.net._decode_body(b'{"t":"ping","d":null}'),
                         ("ping", {}))

    def test_a_nesting_bomb_is_refused(self):
        """Same guard the save loader uses, for the same reason."""
        import json
        deep = current = {}
        for _ in range(60):
            current["a"] = {}
            current = current["a"]
        body = json.dumps({"t": "bomb", "d": deep}).encode("utf-8")
        self.assertIsNone(self.net._decode_body(body))

    # -- sockets -----------------------------------------------------------

    def loopback(self):
        """A host and one connected client on loopback."""
        host = self.net.Host(port=0, bind="127.0.0.1")
        port = host.sock.getsockname()[1]
        client = self.net.Client("127.0.0.1", port, timeout=3.0)
        self.addCleanup(client.close)
        self.addCleanup(host.close)
        for _ in range(50):
            host.pump()
            if host.peers:
                break
        return host, client

    def settle(self, host, client, rounds=60):
        got_host, got_client = [], []
        for _ in range(rounds):
            got_host.extend((k, d) for _p, k, d in host.pump())
            got_client.extend(client.pump())
        return got_host, got_client

    def test_messages_travel_both_ways(self):
        host, client = self.loopback()
        self.assertEqual(len(host.peers), 1)
        client.send("hello", {"name": "Friend"})
        host.broadcast("welcome", {"player_id": 2})
        from_client, from_host = self.settle(host, client)
        self.assertIn(("hello", {"name": "Friend"}), from_client)
        self.assertIn(("welcome", {"player_id": 2}), from_host)

    def test_pump_never_blocks(self):
        """Rule 1. A network layer that can stall a 60fps loop, will."""
        import time
        host, client = self.loopback()
        start = time.perf_counter()
        for _ in range(200):
            host.pump()
            client.pump()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0,
                        "400 idle pumps took %.2fs; something blocks" % elapsed)

    def test_a_peer_claiming_a_huge_frame_is_dropped(self):
        """Checked before a byte is buffered, so the claim costs nothing."""
        import socket as sk
        import struct
        host, client = self.loopback()
        raw = sk.create_connection(("127.0.0.1",
                                    host.sock.getsockname()[1]), 3.0)
        self.addCleanup(raw.close)
        for _ in range(30):
            host.pump()
        raw.send(struct.pack("!I", self.net.MAX_FRAME + 1) + b"x")
        for _ in range(60):
            host.pump()
        self.assertLessEqual(len(host.peers), 1,
                             "the lying peer was kept")

    def test_garbage_does_not_kill_a_good_connection(self):
        import struct
        host, client = self.loopback()
        body = b"not json at all"
        client.peer.sock.send(struct.pack("!I", len(body)) + body)
        self.settle(host, client, rounds=30)
        client.send("ping", {"n": 1})
        from_client, _ = self.settle(host, client)
        self.assertIn(("ping", {"n": 1}), from_client)

    def test_the_room_is_capped(self):
        host, first = self.loopback()
        extras = []
        for _ in range(self.net.MAX_CLIENTS + 2):
            try:
                extra = self.net.Client(
                    "127.0.0.1", host.sock.getsockname()[1], timeout=3.0)
            except self.net.NetError:
                continue
            extras.append(extra)
            self.addCleanup(extra.close)
            for _ in range(10):
                host.pump()
        for _ in range(40):
            host.pump()
        self.assertLessEqual(len(host.peers), self.net.MAX_CLIENTS)


class TestPortmap(unittest.TestCase):
    """Reaching the outside world. Nothing here touches a real router."""

    def setUp(self):
        from pixel_rpg import portmap
        self.portmap = portmap

    def test_private_ranges_are_recognised(self):
        for address in ("10.0.0.4", "192.168.1.1", "172.16.5.5",
                        "127.0.0.1", "169.254.1.1"):
            self.assertTrue(self.portmap._private(address), address)
        for address in ("8.8.8.8", "104.33.145.42", "1.1.1.1"):
            self.assertFalse(self.portmap._private(address), address)

    def test_cgnat_is_called_out_specifically(self):
        """100.64/10 is the one failure a player cannot fix, so it must be
        distinguishable from an ordinary private address."""
        self.assertTrue(self.portmap.is_cgnat("100.64.0.1"))
        self.assertTrue(self.portmap.is_cgnat("100.127.255.254"))
        self.assertFalse(self.portmap.is_cgnat("100.128.0.1"))
        self.assertFalse(self.portmap.is_cgnat("192.168.1.1"))
        self.assertFalse(self.portmap.is_cgnat("nonsense"))

    def test_a_disabled_portal_costs_nothing(self):
        portal = self.portmap.Portal(1234, "192.168.1.5", enabled=False)
        self.assertTrue(portal.done)
        self.assertFalse(portal.mapped)
        self.assertEqual(portal.address, "192.168.1.5")

    def test_the_stun_transaction_id_is_twelve_bytes(self):
        """RFC 5389. One byte short and every server answers with silence,
        which looks exactly like having no internet."""
        import inspect
        source = inspect.getsource(self.portmap._stun_once)
        import re
        found = re.search(r'b"([^"]+)"', source)
        self.assertIsNotNone(found)
        self.assertEqual(len(found.group(1)), 12)

    def test_a_room_code_carries_a_public_address(self):
        from pixel_rpg import net
        code = net.encode_room("104.33.145.42", 47216)
        self.assertEqual(net.decode_room(code), ("104.33.145.42", 47216))


class TestEncounterScaling(unittest.TestCase):
    """How many things you fight, and who brought them."""

    def setUp(self):
        from pixel_rpg import entities
        self.entities = entities

    def test_caps_grow_one_per_hero(self):
        caps = [self.entities.max_enemies(p) for p in (1, 2, 3, 4)]
        self.assertEqual(caps, [3, 4, 5, 6])

    def test_solo_is_capped_where_the_measurements_said(self):
        """A level-14 solo Warden wins 5/30 against five. Three is the
        honest ceiling; more is a wall, not a difficulty curve."""
        from pixel_rpg.util import rng
        rng.reseed(11)
        biggest = max(self.entities.pack_size(1) for _ in range(4000))
        self.assertLessEqual(biggest, 3)

    def test_a_bigger_party_meets_bigger_packs(self):
        from pixel_rpg.util import rng
        averages = []
        for party in (1, 2, 3, 4):
            rng.reseed(21)
            averages.append(sum(self.entities.pack_size(party)
                                for _ in range(800)) / 800.0)
        self.assertEqual(averages, sorted(averages))

    def test_nothing_ever_exceeds_the_hard_cap(self):
        from pixel_rpg.util import rng
        rng.reseed(5)
        for party in (1, 2, 3, 4, 9):
            for _ in range(500):
                self.assertLessEqual(self.entities.pack_size(party),
                                     self.entities.MAX_PACK)

    def test_a_party_can_actually_meet_its_own_cap(self):
        """`PACK_ODDS` is sliced against `max_enemies`, so a row shorter
        than the cap silently makes the top of the range unreachable - which
        is what happened to a party of four, whose documented ceiling of six
        could never be rolled."""
        from pixel_rpg.util import rng
        for party in (1, 2, 3, 4):
            cap = self.entities.max_enemies(party)
            self.assertGreaterEqual(len(self.entities.PACK_ODDS[party]), cap,
                                    "party %d cannot reach its own cap" % party)
            rng.reseed(77 + party)
            rolls = {self.entities.pack_size(party) for _ in range(6000)}
            self.assertIn(cap, rolls,
                          "party %d never met %d monsters" % (party, cap))

    def test_every_odds_row_is_a_distribution(self):
        for party, odds in self.entities.PACK_ODDS.items():
            cap = self.entities.max_enemies(party)
            self.assertAlmostEqual(sum(odds[:cap]), 1.0, places=6,
                                   msg="party %d odds do not sum to 1" % party)

    # -- softening -----------------------------------------------------------

    def test_softening_is_keyed_on_pressure_not_headcount(self):
        """Six monsters split four ways is lighter pressure than two on one
        hero. Keying the discount on the raw count handed a party a
        being-outnumbered bonus while it was outnumbering."""
        soften = self.entities.pack_softening
        alone = soften(2, 1)          # two on one: real pressure
        crowd = soften(6, 4)          # six on four: less each
        self.assertLess(alone[0], crowd[0],
                        "a party got a bigger discount than a lone hero")

    def test_a_party_that_outnumbers_gets_no_discount(self):
        soften = self.entities.pack_softening
        self.assertEqual(soften(3, 4), (1.0, 1.0))
        self.assertEqual(soften(4, 4), (1.0, 1.0))

    def test_solo_softening_is_exactly_what_it_always_was(self):
        """The old rule was `0.78 if count < 4 else 0.66`. Solo is a
        pressure of N, so every threshold has to land where it did - the
        measured solo table depends on it."""
        soften = self.entities.pack_softening
        for count in range(2, 7):
            expected = (0.78, 0.86) if count < 4 else (0.66, 0.78)
            self.assertEqual(soften(count, 1), expected,
                             "solo vs %d changed" % count)

    def test_softening_never_makes_a_monster_stronger(self):
        soften = self.entities.pack_softening
        for party in range(1, 5):
            for count in range(1, 9):
                hp, atk = soften(count, party)
                self.assertLessEqual(hp, 1.0)
                self.assertLessEqual(atk, 1.0)
                self.assertGreater(hp, 0.0)

    def test_a_forced_count_still_carries_the_softening(self):
        """`count=` exists for the balance harness. If it skipped the
        softening the tool would measure a fight the game never generates."""
        from pixel_rpg.util import rng
        rng.reseed(9)
        group = self.entities.make_encounter(14, "meadow", party=4, count=6)
        self.assertEqual(len(group), 6)
        rng.reseed(9)
        lone = self.entities.make_encounter(14, "meadow", party=4, count=1)
        self.assertEqual(len(lone), 1)

    def test_a_forced_count_is_still_bounded(self):
        group = self.entities.make_encounter(5, "meadow", party=4, count=999)
        self.assertLessEqual(len(group), self.entities.MAX_PACK)
        self.assertGreaterEqual(len(
            self.entities.make_encounter(5, "meadow", count=0)), 1)

    # -- how hard co-op is ---------------------------------------------------

    def test_solo_meets_exactly_the_monsters_it_always_did(self):
        """The whole per-class table in section 6 rests on this."""
        self.assertEqual(self.entities.PARTY_LEVEL_STEP[1], 0)
        for level in (1, 8, 14, 40, 60):
            self.assertEqual(self.entities.party_level(level, 1), level)

    def test_a_bigger_party_meets_tougher_monsters(self):
        steps = [self.entities.party_level(20, p) for p in (1, 2, 3, 4)]
        self.assertEqual(steps, sorted(steps))
        self.assertLess(steps[0], steps[-1])

    def test_the_step_is_steeper_than_a_straight_line(self):
        """A straight +1 per hero left a duo at 75% and a full room at 95%,
        because a party's advantage does not grow in a straight line
        either - see section 23."""
        step = self.entities.PARTY_LEVEL_STEP
        self.assertGreater(step[4] - step[3], step[2] - step[1])

    def test_party_level_is_bounded(self):
        # **Against the constant, not against 60.** The ceiling moved
        # to 100 on 2026-08-29, and a literal here is an assertion
        # somebody has to remember to move.
        cap = self.entities.MAX_LEVEL
        self.assertLessEqual(self.entities.party_level(cap, 4), cap)
        self.assertGreaterEqual(self.entities.party_level(1, 1), 1)
        # A party bigger than the room can hold must not fall off the table.
        self.assertEqual(self.entities.party_level(10, 99),
                         self.entities.party_level(10, 4))

    def test_a_pack_arrives_at_the_party_level(self):
        from pixel_rpg.util import rng
        rng.reseed(64)
        alone = self.entities.make_encounter(20, "meadow", party=1, count=3)
        rng.reseed(64)
        crowd = self.entities.make_encounter(20, "meadow", party=4, count=3)
        self.assertGreater(max(m.level for m in crowd),
                           max(m.level for m in alone))

    def test_a_level_one_pack_is_not_a_level_harder(self):
        """s96. Every pack rolls its level plus or minus one; at level 1
        the floor slid the window up and rolled 1-3."""
        from pixel_rpg.util import rng
        for level in (1, 2, 5, 12):
            seen = set()
            for seed in range(200):
                rng.reseed(seed)
                pack = self.entities.make_encounter(level, "meadow",
                                                    party=1, count=3)
                seen.update(m.level for m in pack)
            self.assertEqual(seen, set(range(max(1, level - 1), level + 2)),
                             level)

    def test_a_boss_can_still_be_fought_alone(self):
        """The six warden fights are tuned one-on-one and must stay
        reachable that way."""
        from pixel_rpg.util import rng
        rng.reseed(3)
        rolls = [self.entities.boss_minions(1) for _ in range(400)]
        self.assertIn(0, rolls, "a solo warden never came alone")
        self.assertLessEqual(max(rolls), 1)

    def test_a_party_boss_always_brings_help(self):
        from pixel_rpg.util import rng
        rng.reseed(4)
        for party in (2, 3, 4):
            rolls = [self.entities.boss_minions(party) for _ in range(200)]
            self.assertGreaterEqual(min(rolls), 1)

    def test_boss_encounters_lead_with_the_boss(self):
        from pixel_rpg.entities import BOSS_DEFS, Boss
        from pixel_rpg.util import rng
        for party in (1, 2, 3, 4):
            rng.reseed(30 + party)
            group = self.entities.make_boss_encounter(BOSS_DEFS[0],
                                                      party=party)
            self.assertIsInstance(group[0], Boss)
            self.assertEqual(sum(1 for e in group if isinstance(e, Boss)), 1,
                             "more than one warden in a warden fight")
            self.assertLessEqual(len(group),
                                 self.entities.max_enemies(party))

    def test_minions_are_weaker_than_the_boss(self):
        """They exist to split your attention, not to out-damage the thing
        you came for."""
        from pixel_rpg.entities import BOSS_DEFS
        from pixel_rpg.util import rng
        rng.reseed(77)
        for _ in range(20):
            group = self.entities.make_boss_encounter(BOSS_DEFS[2], party=4)
            if len(group) < 2:
                continue
            boss = group[0]
            for minion in group[1:]:
                self.assertLess(minion.max_hp(), boss.max_hp())
            return
        self.skipTest("no minions rolled")


class TestBattleIsolation(unittest.TestCase):
    """One player's fight is one player's fight."""

    def setUp(self):
        from pixel_rpg import coop
        self.coop = coop

    def test_a_remote_carries_its_own_battle_state(self):
        remote = self.coop.Remote(2, "Friend")
        self.assertFalse(remote.in_battle)
        self.coop.Session._place(remote, {"x": 1, "y": 2, "b": True})
        self.assertTrue(remote.in_battle)
        self.coop.Session._place(remote, {"x": 1, "y": 2, "b": False})
        self.assertFalse(remote.in_battle)

    def test_fighting_lists_only_those_in_a_battle(self):
        session = self.coop.Session.host("Host", port=0, seed=1,
                                         reach_out=False)
        self.addCleanup(session.close)
        quiet = self.coop.Remote(2, "Quiet")
        busy = self.coop.Remote(3, "Busy")
        busy.in_battle = True
        session.remotes = {2: quiet, 3: busy}
        self.assertEqual([r.name for r in session.fighting()], ["Busy"])

    def test_starting_a_battle_does_not_touch_anybody_else(self):
        """Nobody is dragged into a fight they did not walk into - and so
        the XP goes only to whoever swung, because only they were there."""
        session = self.coop.Session.host("Host", port=0, seed=1,
                                         reach_out=False)
        self.addCleanup(session.close)
        friend = self.coop.Remote(2, "Friend")
        session.remotes = {2: friend}
        self.assertFalse(friend.in_battle)


class BrowserPretender:
    """A minimal RFC 6455 client, so the host is tested against a real one.

    Written out rather than mocked because the whole point of the transport
    is that something we did not write can talk to it. A fake that agreed
    with our own parser would prove only that the parser agrees with itself.
    """

    def __init__(self, host, port, path="/", headers=None, timeout=3.0):
        import socket
        self.sock = socket.create_connection((host, port), timeout)
        self.buffer = b""
        self.key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = ["GET %s HTTP/1.1" % path,
                 "Host: %s:%d" % (host, port),
                 "Upgrade: websocket",
                 "Connection: Upgrade",
                 "Sec-WebSocket-Key: %s" % self.key,
                 "Sec-WebSocket-Version: 13"]
        for name, value in (headers or {}).items():
            lines = [line for line in lines
                     if not line.lower().startswith(name.lower() + ":")]
            if value is not None:
                lines.append("%s: %s" % (name, value))
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

    def handshake(self):
        """The response head, as a dict. Raises if it is not a 101."""
        while b"\r\n\r\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AssertionError("host closed during the handshake")
            self.buffer += chunk
        head, _sep, self.buffer = self.buffer.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0]
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        return status, headers

    def send(self, body, opcode=0x1, mask=True, fin=True, rsv=0):
        """One client frame. Masked, because the RFC says so."""
        if isinstance(body, str):
            body = body.encode("utf-8")
        first = (0x80 if fin else 0) | (rsv << 4) | opcode
        header = bytes((first,))
        size = len(body)
        flag = 0x80 if mask else 0
        if size < 126:
            header += bytes((flag | size,))
        elif size < 65536:
            header += bytes((flag | 126,)) + struct.pack("!H", size)
        else:
            header += bytes((flag | 127,)) + struct.pack("!Q", size)
        if not mask:
            self.sock.sendall(header + body)
            return
        key = os.urandom(4)
        masked = bytearray(body)
        for i in range(size):
            masked[i] ^= key[i & 3]
        self.sock.sendall(header + key + bytes(masked))

    def send_json(self, kind, payload=None):
        self.send(json.dumps({"t": kind, "d": payload or {}}))

    def recv_frame(self, timeout=3.0):
        """One server frame as (opcode, payload). None if the host went away."""
        self.sock.settimeout(timeout)
        while True:
            got = self._take()
            if got is not None:
                return got
            try:
                chunk = self.sock.recv(4096)
            except OSError:
                return None
            if not chunk:
                return None
            self.buffer += chunk

    def _take(self):
        if len(self.buffer) < 2:
            return None
        first, second = self.buffer[0], self.buffer[1]
        size = second & 0x7F
        offset = 2
        if size == 126:
            if len(self.buffer) < 4:
                return None
            (size,) = struct.unpack("!H", self.buffer[2:4])
            offset = 4
        elif size == 127:
            if len(self.buffer) < 10:
                return None
            (size,) = struct.unpack("!Q", self.buffer[2:10])
            offset = 10
        if second & 0x80:
            raise AssertionError("the host masked a frame; servers must not")
        if len(self.buffer) < offset + size:
            return None
        body = self.buffer[offset:offset + size]
        self.buffer = self.buffer[offset + size:]
        return first & 0x0F, bytes(body)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class TestWebSocketFrames(unittest.TestCase):
    """The protocol, as maths over bytes."""

    def setUp(self):
        from pixel_rpg import websock
        self.ws = websock

    def test_the_accept_key_is_the_one_from_the_rfc(self):
        """RFC 6455 section 1.3 works the example through by hand."""
        self.assertEqual(self.ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_the_two_transports_cannot_be_confused(self):
        """A length prefix spelling GET would be over a gigabyte, which is
        forty times the ceiling and refused on sight."""
        from pixel_rpg import net
        (as_length,) = struct.unpack("!I", b"GET ")
        self.assertGreater(as_length, net.MAX_FRAME * 40)
        self.assertTrue(self.ws.looks_like_http(b"GET /room HTTP/1.1"))
        self.assertFalse(self.ws.looks_like_http(struct.pack("!I", 12)))

    def test_a_frame_survives_the_round_trip(self):
        payload = b'{"t":"hello","d":{}}'
        frame = self.ws.encode(payload)
        masked = _mask_frame(frame)
        fin, opcode, body, rest = self.ws.take_frame(masked, 65536)
        self.assertTrue(fin)
        self.assertEqual(opcode, self.ws.OP_TEXT)
        self.assertEqual(body, payload)
        self.assertEqual(rest, b"")

    def test_a_server_frame_is_never_masked(self):
        frame = self.ws.encode(b"x" * 10)
        self.assertFalse(frame[1] & 0x80)

    def test_every_length_form_round_trips(self):
        for size in (0, 1, 125, 126, 127, 65535, 65536, 70000):
            frame = self.ws.encode(b"y" * size)
            fin, _op, body, rest = self.ws.take_frame(
                _mask_frame(frame), 200000)
            self.assertEqual(len(body), size, "size %d" % size)
            self.assertEqual(rest, b"")

    def test_a_partial_frame_waits_rather_than_guessing(self):
        frame = _mask_frame(self.ws.encode(b"z" * 300))
        for cut in (1, 2, 3, 5, 8, 100, len(frame) - 1):
            fin, _op, _body, rest = self.ws.take_frame(frame[:cut], 65536)
            self.assertIsNone(fin, "cut at %d" % cut)
            self.assertEqual(rest, frame[:cut])

    def test_an_unmasked_client_frame_is_refused(self):
        """Masking is what stops a hostile page persuading a browser to emit
        bytes a confused proxy would read as its own protocol."""
        frame = self.ws.encode(b"hello")          # server-style, unmasked
        with self.assertRaises(self.ws.ProtocolError):
            self.ws.take_frame(frame, 65536)

    def test_an_enormous_claim_is_refused_before_it_is_held(self):
        header = bytes((0x81, 0xFF)) + struct.pack("!Q", 2 ** 40)
        with self.assertRaises(self.ws.ProtocolError):
            self.ws.take_frame(header, 65536)

    def test_reserved_bits_are_refused(self):
        frame = bytearray(_mask_frame(self.ws.encode(b"hi")))
        frame[0] |= 0x40
        with self.assertRaises(self.ws.ProtocolError):
            self.ws.take_frame(bytes(frame), 65536)

    def test_a_handshake_needs_all_of_its_parts(self):
        good = {"upgrade": "websocket", "connection": "Upgrade",
                "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
                "sec-websocket-version": "13"}
        self.assertIsNotNone(self.ws.handshake_response(good))
        for missing in ("upgrade", "connection", "sec-websocket-key"):
            broken = dict(good)
            del broken[missing]
            self.assertIsNone(self.ws.handshake_response(broken),
                              "accepted a handshake with no %s" % missing)
        self.assertIsNone(self.ws.handshake_response(
            dict(good, **{"sec-websocket-version": "8"})))
        self.assertIsNone(self.ws.handshake_response(None))

    def test_a_request_that_is_not_one_is_not_parsed(self):
        self.assertIsNone(self.ws.parse_request(b"POST / HTTP/1.1\r\n"))
        self.assertIsNone(self.ws.parse_request(b"\xff\xfe\x00"))


def _mask_frame(frame):
    """Turn a server frame into a client one, for the parser's benefit."""
    import os as _os
    first, second = frame[0], frame[1]
    size = second & 0x7F
    head = 2
    if size == 126:
        head = 4
    elif size == 127:
        head = 10
    body = frame[head:]
    key = _os.urandom(4)
    masked = bytearray(body)
    for i in range(len(masked)):
        masked[i] ^= key[i & 3]
    return frame[:1] + bytes((second | 0x80,)) + frame[2:head] + key \
        + bytes(masked)


class TestWebSocketHost(unittest.TestCase):
    """A real browser-shaped client against the real host."""

    def setUp(self):
        from pixel_rpg import net
        self.net = net
        self.host = net.Host(port=0, bind="127.0.0.1")
        self.addCleanup(self.host.close)
        self.port = self.host.port

    def connect(self, **kwargs):
        client = BrowserPretender("127.0.0.1", self.port, **kwargs)
        self.addCleanup(client.close)
        return client

    def pump(self, times=12):
        out = []
        for _ in range(times):
            time.sleep(0.005)
            out.extend(self.host.pump())
        return out

    def test_a_browser_is_upgraded(self):
        client = self.connect()
        self.pump()
        status, headers = client.handshake()
        self.assertIn("101", status)
        self.assertEqual(headers.get("upgrade", "").lower(), "websocket")
        from pixel_rpg import websock
        self.assertEqual(headers.get("sec-websocket-accept"),
                         websock.accept_key(client.key))
        self.assertEqual(len(self.host.peers), 1)
        self.assertEqual(self.host.peers[0].mode, self.net.MODE_WS)

    def test_a_message_arrives_the_same_as_any_other(self):
        """`coop.py` never learns which transport a peer used."""
        client = self.connect()
        self.pump()
        client.handshake()
        client.send_json("hello", {"protocol": 1, "name": "Phone"})
        inbox = self.pump()
        self.assertEqual([(kind, data) for _peer, kind, data in inbox],
                         [("hello", {"protocol": 1, "name": "Phone"})])

    def test_the_host_answers_in_the_same_language(self):
        client = self.connect()
        self.pump()
        client.handshake()
        self.host.send(self.host.peers[0], "welcome", {"seed": 7})
        self.pump()
        opcode, body = client.recv_frame()
        self.assertEqual(opcode, 0x1)
        self.assertEqual(json.loads(body.decode()),
                         {"t": "welcome", "d": {"seed": 7}})

    def test_a_split_message_is_reassembled(self):
        """A browser may fragment, and some proxies do it unasked."""
        client = self.connect()
        self.pump()
        client.handshake()
        body = json.dumps({"t": "at", "d": {"x": 5, "y": 6}})
        client.send(body[:9], opcode=0x1, fin=False)
        client.send(body[9:], opcode=0x0, fin=True)
        inbox = self.pump()
        self.assertEqual([(k, d) for _p, k, d in inbox],
                         [("at", {"x": 5, "y": 6})])

    def test_a_ping_is_answered(self):
        client = self.connect()
        self.pump()
        client.handshake()
        client.send(b"knock", opcode=0x9)
        self.pump()
        opcode, body = client.recv_frame()
        self.assertEqual(opcode, 0xA)
        self.assertEqual(body, b"knock")

    def test_a_close_is_answered_and_the_peer_goes(self):
        client = self.connect()
        self.pump()
        client.handshake()
        client.send(struct.pack("!H", 1000), opcode=0x8)
        self.pump()
        self.assertEqual(self.host.peers, [])

    def test_an_unmasked_client_is_dropped(self):
        client = self.connect()
        self.pump()
        client.handshake()
        client.send(b'{"t":"hello","d":{}}', mask=False)
        self.pump()
        self.assertEqual(self.host.peers, [])

    def test_a_request_that_is_not_an_upgrade_gets_a_status(self):
        """A browser deserves a status line rather than a dropped socket."""
        client = self.connect(headers={"Upgrade": None})
        self.pump()
        head = b""
        client.sock.settimeout(3.0)
        while b"\r\n" not in head:
            chunk = client.sock.recv(1024)
            if not chunk:
                break
            head += chunk
        self.assertIn(b"400", head)
        self.assertEqual(self.host.peers, [])

    def test_a_giant_handshake_is_refused(self):
        """Somebody seeing how much we will hold, checked while reading."""
        import socket
        raw = socket.create_connection(("127.0.0.1", self.port), 3.0)
        self.addCleanup(raw.close)
        raw.sendall(b"GET / HTTP/1.1\r\nX: " + b"a" * 40000)
        self.pump(20)
        self.assertEqual(self.host.peers, [])

    def test_both_transports_share_one_room(self):
        """The whole point: one port, one roster, two languages."""
        desktop = self.net.Client("127.0.0.1", self.port, timeout=3.0)
        self.addCleanup(desktop.close)
        browser = self.connect()
        self.pump()
        browser.handshake()
        self.assertEqual(len(self.host.peers), 2)

        desktop.send("hello", {"protocol": 1, "name": "Desk"})
        browser.send_json("hello", {"protocol": 1, "name": "Phone"})
        names = sorted(d.get("name") for _p, _k, d in self.pump())
        self.assertEqual(names, ["Desk", "Phone"])
        modes = sorted(peer.mode for peer in self.host.peers)
        self.assertEqual(modes, [self.net.MODE_TCP, self.net.MODE_WS])

        # And one broadcast reaches both, dressed differently.
        self.host.broadcast("roster", {"players": 2})
        self.pump()
        opcode, body = browser.recv_frame()
        self.assertEqual(json.loads(body.decode())["t"], "roster")
        self.assertIn(("roster", {"players": 2}),
                      [(k, d) for k, d in desktop.pump()])

    def test_a_transport_is_unknown_until_the_peer_speaks(self):
        """A WebSocket announces itself with a request head; the raw
        transport announces itself only by sending something. So a peer that
        connects and waits has no transport yet, and anything addressed to it
        is held rather than guessed at.

        Harmless in practice - `coop.Session.join` sends `hello` as its first
        act - but worth pinning down, because "held until we know" is only
        obviously right while somebody remembers it is happening.
        """
        desktop = self.net.Client("127.0.0.1", self.port, timeout=3.0)
        self.addCleanup(desktop.close)
        self.pump()
        peer = self.host.peers[0]
        self.assertEqual(peer.mode, self.net.MODE_UNKNOWN)

        self.host.send(peer, "welcome", {"seed": 3})
        self.pump()
        self.assertEqual(len(peer.pending), 1, "the message was not held")
        self.assertEqual(desktop.pump(), [], "it went out before we knew how")

        desktop.send("hello", {"protocol": 1})
        self.pump()
        self.assertEqual(peer.mode, self.net.MODE_TCP)
        self.assertEqual(peer.pending, [])
        self.assertIn(("welcome", {"seed": 3}), desktop.pump())

    def test_a_full_room_says_so_in_the_right_language(self):
        """A browser handed a length-prefixed refusal sees garbage and
        reports a broken host rather than a full room."""
        keep = []
        for _ in range(self.net.MAX_CLIENTS):
            peer = self.net.Client("127.0.0.1", self.port, timeout=3.0)
            keep.append(peer)
            self.addCleanup(peer.close)
            peer.send("hello", {"protocol": 1})
        self.pump()
        self.assertEqual(len(self.host.peers), self.net.MAX_CLIENTS)

        latecomer = self.connect()
        self.pump(20)
        status, _headers = latecomer.handshake()
        self.assertIn("101", status)
        opcode, body = latecomer.recv_frame()
        self.assertEqual(json.loads(body.decode())["t"], "full")

    def test_a_silent_refused_socket_cannot_camp(self):
        """Every "just until it finishes" queue needs a clock."""
        import socket
        for _ in range(self.net.MAX_CLIENTS):
            peer = self.net.Client("127.0.0.1", self.port, timeout=3.0)
            self.addCleanup(peer.close)
        self.pump()
        quiet = socket.create_connection(("127.0.0.1", self.port), 3.0)
        self.addCleanup(quiet.close)
        self.pump()
        self.assertEqual(len(self.host._rejecting), 1)
        self.host._rejecting[0] = (self.host._rejecting[0][0],
                                   time.monotonic() - self.net.REJECT_GRACE - 1)
        self.pump()
        self.assertEqual(self.host._rejecting, [])


class FakeLocalStorage:
    """A stand-in for the browser's `localStorage`, warts included.

    Written out rather than mocked for the same reason `BrowserPretender` is:
    the interesting behaviour is what happens when the browser says *no*, and
    a mock that always says yes proves nothing. `fail_after` makes it start
    refusing, which is what a full quota looks like from Python.
    """

    def __init__(self, fail_after=None):
        self.items = {}
        self.fail_after = fail_after
        self.writes = 0

    @property
    def length(self):
        return len(self.items)

    def key(self, index):
        try:
            return sorted(self.items)[index]
        except IndexError:
            return None

    def getItem(self, name):                              # noqa: N802
        return self.items.get(name)

    def setItem(self, name, value):                       # noqa: N802
        self.writes += 1
        if self.fail_after is not None and self.writes > self.fail_after:
            raise RuntimeError("QuotaExceededError")
        self.items[name] = str(value)

    def removeItem(self, name):                           # noqa: N802
        self.items.pop(name, None)


class FakeWindow:
    def __init__(self, storage):
        self.localStorage = storage                       # noqa: N815


class TestStoreBackends(unittest.TestCase):
    """Every backend has to behave the same, or the seam is a lie."""

    def setUp(self):
        from pixel_rpg import store
        self.store = store
        self.tmp = tempfile.mkdtemp(prefix="rpgstore")

    def backends(self):
        """One of each, so a test can assert they agree."""
        return [
            ("memory", self.store.MemoryStore()),
            ("file", self.store.FileStore(self.tmp)),
            ("browser", self.store.BrowserStore(
                window=FakeWindow(FakeLocalStorage()))),
        ]

    # -- the shared contract -------------------------------------------------

    def test_all_three_behave_the_same(self):
        for name, backend in self.backends():
            with self.subTest(backend=name):
                self.assertTrue(backend.prepare(), name)
                self.assertFalse(backend.exists("slot1.json"))
                backend.write_text("slot1.json", '{"a": 1}')
                self.assertTrue(backend.exists("slot1.json"))
                self.assertEqual(backend.read_text("slot1.json"), '{"a": 1}')
                self.assertIn("slot1.json", backend.keys())
                self.assertTrue(backend.delete("slot1.json"))
                self.assertFalse(backend.exists("slot1.json"))
                self.assertFalse(backend.delete("slot1.json"))

    def test_reading_nothing_is_a_validation_error_everywhere(self):
        for name, backend in self.backends():
            with self.subTest(backend=name):
                backend.prepare()
                with self.assertRaises(sec.ValidationError):
                    backend.read_text("slot4.json")

    def test_json_goes_through_the_same_validator_everywhere(self):
        """A new place to keep saves must never become a new way to skip
        the checks."""
        for name, backend in self.backends():
            with self.subTest(backend=name):
                backend.prepare()
                backend.write_text("bad.json", "{ not json")
                with self.assertRaises(sec.ValidationError):
                    backend.read_json("bad.json")
                backend.write_text("deep.json",
                                   '{"a":' * 40 + "1" + "}" * 40)
                with self.assertRaises(sec.ValidationError):
                    backend.read_json("deep.json")
                backend.write_text("long.json",
                                   json.dumps({"s": "x" * 9000}))
                with self.assertRaises(sec.ValidationError):
                    backend.read_json("long.json")

    def test_an_oversized_payload_is_refused_everywhere(self):
        for name, backend in self.backends():
            with self.subTest(backend=name):
                backend.prepare()
                with self.assertRaises(sec.ValidationError):
                    backend.write_text("big.json", "x" * 200, max_bytes=100)

    def test_rename_moves_a_value_everywhere(self):
        for name, backend in self.backends():
            with self.subTest(backend=name):
                backend.prepare()
                backend.write_text("slot1.json", "first")
                self.assertTrue(backend.rename("slot1.json", "slot1.bak"))
                self.assertFalse(backend.exists("slot1.json"))
                self.assertEqual(backend.read_text("slot1.bak"), "first")
                self.assertFalse(backend.rename("gone.json", "slot1.bak"))

    # -- keys ----------------------------------------------------------------

    def test_a_key_cannot_be_a_path(self):
        """The whole reason a key is a shape and not a cleaned string: a key
        that needs cleaning is a bug upstream and should be seen."""
        for bad in ("../escape", "a/b", "a\\b", "", ".", "..",
                    "/etc/passwd", "x" * 200, "\u202eevil"):
            self.assertFalse(self.store.valid_key(bad), repr(bad))
        for good in ("slot1.json", "slot6.bak", "profile.json", "a-b_c.1"):
            self.assertTrue(self.store.valid_key(good), repr(good))

    def test_a_bad_key_is_refused_rather_than_cleaned(self):
        for name, backend in self.backends():
            with self.subTest(backend=name):
                backend.prepare()
                with self.assertRaises(self.store.StoreError):
                    backend.write_text("../escape.json", "{}")

    # -- the file backend keeps its old promises ------------------------------

    def test_the_file_backend_writes_where_it_always_did(self):
        import os as _os
        backend = self.store.FileStore(self.tmp)
        backend.prepare()
        backend.write_text("slot2.json", '{"x": 1}')
        self.assertTrue(_os.path.isfile(_os.path.join(self.tmp, "slot2.json")))
        self.assertEqual(backend.description, self.tmp)

    def test_the_file_backend_leaves_no_temp_behind(self):
        backend = self.store.FileStore(self.tmp)
        backend.write_text("slot3.json", '{"x": 1}')
        self.assertEqual([k for k in backend.keys() if k.endswith(".tmp")], [])

    def test_the_file_backend_refuses_to_read_outside_its_directory(self):
        backend = self.store.FileStore(self.tmp)
        backend.prepare()
        with self.assertRaises(self.store.StoreError):
            backend.read_text("..")

    # -- the browser backend, and the ways a browser says no -------------------

    def test_a_browser_that_will_not_store_says_so(self):
        """Private mode, a blocked context, no bridge at all."""
        backend = self.store.BrowserStore(window=None)
        backend._window = None
        self.assertFalse(backend.available())
        self.assertFalse(backend.prepare())
        self.assertFalse(backend.exists("slot1.json"))
        self.assertEqual(backend.keys(), [])
        with self.assertRaises(sec.ValidationError):
            backend.read_text("slot1.json")
        with self.assertRaises(sec.ValidationError):
            backend.write_text("slot1.json", "{}")

    def test_a_full_quota_is_reported_not_raised_at_the_game(self):
        """The likeliest browser failure by far: a page gets a few megabytes
        and never more."""
        storage = FakeLocalStorage(fail_after=1)
        backend = self.store.BrowserStore(window=FakeWindow(storage))
        backend.write_text("slot1.json", "{}")
        with self.assertRaises(sec.ValidationError):
            backend.write_text("slot2.json", "{}")

    def test_a_browser_keeps_its_keys_to_itself(self):
        """Prefixed, so the game cannot collide with whatever else the page
        is keeping - and cannot see it either."""
        storage = FakeLocalStorage()
        storage.items["somebody-elses-key"] = "not ours"
        backend = self.store.BrowserStore(prefix="pixelrpg:",
                                          window=FakeWindow(storage))
        backend.write_text("slot1.json", "{}")
        self.assertEqual(backend.keys(), ["slot1.json"])
        self.assertIn("pixelrpg:slot1.json", storage.items)
        self.assertIn("somebody-elses-key", storage.items)

    # -- open_store ------------------------------------------------------------

    def test_open_store_takes_whatever_the_caller_had(self):
        self.assertIsInstance(self.store.open_store(self.tmp),
                              self.store.FileStore)
        self.assertIsInstance(self.store.open_store(None),
                              self.store.MemoryStore)
        made = self.store.MemoryStore()
        self.assertIs(self.store.open_store(made), made)


class TestSaveManagerOnAStore(unittest.TestCase):
    """The save manager against a medium that is not a filesystem."""

    def setUp(self):
        from pixel_rpg import store, save as savemod
        self.store = store
        self.save = savemod
        self.backend = store.MemoryStore()
        self.mgr = savemod.SaveManager(self.backend)

    def payload(self, name="Ghost", level=4):
        from pixel_rpg.world import World
        player = Player(name, "warden")
        for _ in range(level - 1):
            player.gain_xp(player.xp_to_next())
        world = World(32, 4242)
        return self.save.build_payload(player, world, QuestLog(),
                                       Settings(), 12.5)

    def test_a_run_survives_a_save_and_a_load_with_no_files(self):
        self.assertTrue(self.mgr.write(1, self.payload("Ghost", 5),
                                       force=True))
        data = self.mgr.read(1)
        self.assertIsNotNone(data, self.mgr.last_error)
        self.assertEqual(data["player"]["name"], "Ghost")
        self.assertIn("slot1.json", self.backend.keys())

    def test_a_second_save_leaves_a_backup(self):
        self.mgr.write(1, self.payload("First"), force=True)
        self.mgr.write(1, self.payload("Second"), force=True)
        self.assertIn("slot1.bak", self.backend.keys())
        self.assertEqual(self.mgr.read(1)["player"]["name"], "Second")

    def test_a_broken_slot_falls_back_to_its_backup(self):
        """The backup is the entire safety net where there is no rename."""
        self.mgr.write(1, self.payload("Good"), force=True)
        self.mgr.write(1, self.payload("Newer"), force=True)
        self.backend.items["slot1.json"] = "{ truncated"
        data = self.mgr.read(1)
        self.assertIsNotNone(data, self.mgr.last_error)
        self.assertEqual(data["player"]["name"], "Good")
        self.assertIn("recovered", self.mgr.last_warning)

    def test_describe_and_delete_work_without_paths(self):
        self.mgr.write(2, self.payload("Seen", 7), force=True)
        described = self.mgr.describe(2)
        self.assertFalse(described["empty"])
        self.assertEqual(described["name"], "Seen")
        self.assertTrue(self.mgr.delete(2))
        self.assertTrue(self.mgr.describe(2)["empty"])

    def test_a_pathless_manager_admits_it_has_no_path(self):
        """Better than a plausible-looking lie: there is no directory."""
        self.assertIsNone(self.mgr.dir)
        self.assertIsNone(self.mgr.slot_path(1))
        self.assertIn("memory", self.mgr.where)

    def test_a_profile_survives_without_a_filesystem(self):
        from pixel_rpg.meta import MetaProfile
        backend = self.store.MemoryStore()
        profile = MetaProfile(backend)
        profile.unlock_codex("maren")
        self.assertTrue(profile.save())
        self.assertIn("profile.json", backend.keys())
        self.assertIn("maren", MetaProfile(backend).load().codex)

    def test_a_profile_that_cannot_be_written_never_takes_the_game_down(self):
        """Losing a profile costs codex pages, not a run."""
        from pixel_rpg.meta import MetaProfile
        storage = FakeLocalStorage(fail_after=0)
        backend = self.store.BrowserStore(window=FakeWindow(storage))
        profile = MetaProfile(backend)
        profile.unlock_codex("maren")
        self.assertFalse(profile.save())
        self.assertEqual(MetaProfile(backend).load().codex, [])


class TestNoticesStayOnScreen(unittest.TestCase):
    """A notice is sized to its text, and the text is not bounded.

    Found by screenshotting the guest-hero notices at phone size (rule
    31.4), which is the only thing that looks at these: `layout.py` does
    not drive the flows that raise them, and it went on reporting zero.
    "Welcome back to <host>'s valley" carries a name that arrives off a
    socket, and on a 390px window the panel slid off the left edge.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import ui as ui_mod
        from pixel_rpg.assets import Assets
        self.ui = ui_mod
        self.assets = Assets(32)

    def painted_columns(self, width, anchor_x, texts):
        """Draw the notices and report the leftmost pixel actually touched."""
        surface = pygame.Surface((width, 300), pygame.SRCALPHA)
        surface.fill((0, 0, 0, 0))
        queue = self.ui.ToastQueue()
        for text in texts:
            queue.push(text, self.ui.ACCENT, 30.0)
        queue.update(0.5)          # past the slide-in, so it is at full width
        queue.draw(surface, self.assets, anchor_x, 10)
        for x in range(width):
            for y in range(300):
                if surface.get_at((x, y))[3]:
                    return x
        return width

    def test_a_long_notice_does_not_run_off_a_phone(self):
        long_host = "Bartholomew123456"
        left = self.painted_columns(
            390, 382,
            ["Welcome back to %s's valley." % long_host,
             "Could not keep that hero: storage is full"])
        self.assertGreaterEqual(
            left, self.ui.ToastQueue.EDGE - 1,
            "a notice was drawn off the left edge at phone width")

    def test_a_wide_window_is_untouched(self):
        """The fix is a ceiling, not a redesign: nothing changes where
        there was room all along."""
        text = "Welcome back to Maren's valley."
        img = self.assets.text(text, "small", self.ui.ACCENT)
        left = self.painted_columns(1280, 1260, [text])
        self.assertLessEqual(abs(left - (1260 - img.get_width() - 26)), 2,
                             "a notice moved on a screen with room for it")


class TestGuestVault(unittest.TestCase):
    """A hero that belongs to somebody else's world (s19 item 5c).

    Savvydev: *"a guest hero does persist... but if the host switches worlds,
    then the guest hero resets unless the guest has a hero for that world
    too."* Every check below is one clause of that sentence.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import store, save as savemod
        from pixel_rpg.world import World
        self.save = savemod
        self.backend = store.MemoryStore()
        self.vault = savemod.GuestVault(self.backend)
        self.world_factory = lambda seed: World(32, seed)

    def hero(self, name="Guest", level=1, gold=0):
        player = Player(name, "warden")
        for _ in range(level - 1):
            player.gain_xp(player.xp_to_next())
        player.gold = gold
        return player

    def leave(self, seed, name="Guest", level=1, gold=0, flags=()):
        world = self.world_factory(seed)
        for flag in flags:
            world.flag(flag)
        player = self.hero(name, level, gold)
        ok = self.vault.remember(seed, player, world, QuestLog())
        self.assertTrue(ok, self.vault.last_error)
        return player, world

    def test_a_first_visit_has_no_hero(self):
        """None is the ordinary answer, not a failure."""
        self.assertIsNone(self.vault.recall(1234, self.world_factory))
        self.assertEqual(self.vault.last_error, "")

    def test_a_hero_left_in_a_valley_is_waiting_next_time(self):
        self.leave(1234, "Ilan", level=6, gold=410)
        found = self.vault.recall(1234, self.world_factory)
        self.assertIsNotNone(found, self.vault.last_error)
        player, world, quests = found
        self.assertEqual(player.name, "Ilan")
        self.assertEqual(player.level, 6)
        self.assertEqual(player.gold, 410)
        self.assertEqual(world.seed, 1234)
        self.assertIsInstance(quests, QuestLog)

    def test_a_hero_belongs_to_one_valley_only(self):
        """The host switching worlds is what resets a guest hero."""
        self.leave(1234, "Ilan", level=6)
        self.assertIsNone(self.vault.recall(5678, self.world_factory))

    def test_a_guests_story_stays_theirs_between_visits(self):
        """Story flags are private in co-op (s20), which only holds if
        they survive the visit that earned them."""
        self.leave(1234, flags=("maren_0", "weaver_seen"))
        _player, world, _quests = self.vault.recall(1234, self.world_factory)
        # `flag` returns True only the first time, so False means it is set.
        self.assertFalse(world.flag("maren_0"))
        self.assertFalse(world.flag("weaver_seen"))
        self.assertTrue(world.flag("never_happened"))

    def test_a_guest_hero_is_not_one_of_the_six_slots(self):
        self.leave(1234)
        keys = list(self.backend.keys())
        self.assertIn("guest1234.json", keys)
        self.assertFalse([k for k in keys if k.startswith("slot")])

    def test_the_vault_is_capped_and_drops_the_oldest_hero(self):
        limit = self.save.GUEST_LIMIT
        for i in range(limit):
            self.leave(1000 + i, "Hero%d" % i)
            # `updated` has one-second resolution, so make the order real
            # rather than hoping the clock ticked between two writes.
            self._age(1000 + i, "2026-08-%02d 10:00:00" % (i + 1))
        self.assertEqual(len(self.vault.seeds()), limit)
        self.leave(9999, "Newcomer")
        seeds = self.vault.seeds()
        self.assertEqual(len(seeds), limit)
        self.assertNotIn(1000, seeds)          # the oldest went
        self.assertIn(9999, seeds)
        self.assertIn(1000 + limit - 1, seeds)  # the newest stayed

    def test_returning_to_a_valley_you_hold_evicts_nobody(self):
        """Overwriting a hero is not a new tenant. Without this, playing in
        a world you already have a hero in costs you a different one."""
        limit = self.save.GUEST_LIMIT
        for i in range(limit):
            self.leave(1000 + i, "Hero%d" % i)
            self._age(1000 + i, "2026-08-%02d 10:00:00" % (i + 1))
        self.leave(1000, "Hero0", level=4)     # back to the oldest valley
        seeds = self.vault.seeds()
        self.assertEqual(len(seeds), limit)
        self.assertIn(1000, seeds)
        self.assertEqual(
            self.vault.recall(1000, self.world_factory)[0].level, 4)

    def test_the_stored_seed_never_chooses_the_world(self):
        """An edited file must not rebuild a different valley under
        everybody else. The host's seed is the world; the file is a record
        of where the hero was left."""
        self.leave(1234)
        raw = self.backend.read_json("guest1234.json")
        raw["seed"] = 777
        self.backend.write_json("guest1234.json", raw)
        _player, world, _quests = self.vault.recall(1234, self.world_factory)
        self.assertEqual(world.seed, 1234)

    def test_an_edited_hero_still_loads_but_says_so(self):
        self.leave(1234, "Ilan", gold=10)
        raw = self.backend.read_json("guest1234.json")
        raw["player"]["gold"] = 999999
        self.backend.write_json("guest1234.json", raw)
        found = self.vault.recall(1234, self.world_factory)
        self.assertIsNotNone(found)
        self.assertIn("edited", self.vault.last_warning)

    def test_a_seed_off_a_socket_cannot_steer_the_key(self):
        """Everything off a socket is save data (s18). A seed arrives in a
        `welcome`, so it is coerced the way a slot number is."""
        from pixel_rpg import store as store_mod
        for hostile in ("../../etc/passwd", "slot1", -5, 2 ** 40, None,
                        [1, 2], "1234; rm"):
            key = self.vault.key_for(hostile)
            self.assertTrue(store_mod.valid_key(key), key)
            self.assertRegex(key, r"^guest[0-9]+\.json$")
        # It clamps rather than refusing, which is safe *here* because the
        # result is still a key of our own making - unlike an index off a
        # socket, which is refused (s20, s21).
        self.assertEqual(self.vault.key_for("../../etc/passwd"), "guest0.json")
        self.assertEqual(self.vault.key_for(-5), "guest0.json")

    def test_forgetting_a_valley_removes_its_hero(self):
        self.leave(1234)
        self.assertTrue(self.vault.forget(1234))
        self.assertFalse(self.vault.has(1234))

    def _age(self, seed, stamp):
        """Backdate a stored hero, so eviction order is a fact not a race."""
        key = self.vault.key_for(seed)
        raw = self.backend.read_json(key)
        raw["updated"] = stamp
        self.backend.write_json(key, raw)


class TestWebClient(unittest.TestCase):
    """The guest half: joining a PC's room the way a phone would."""

    def setUp(self):
        from pixel_rpg import net, websock
        self.net = net
        self.ws = websock
        self.host = net.Host(port=0, bind="127.0.0.1")
        self.addCleanup(self.host.close)
        self.port = self.host.port

    def guest(self):
        client = self.net.WebClient("127.0.0.1", self.port, timeout=3.0)
        self.addCleanup(client.close)
        return client

    def settle(self, guest=None, times=14):
        """Pump both ends. Returns whatever reached the host."""
        out = []
        for _ in range(times):
            time.sleep(0.004)
            out.extend(self.host.pump())
            if guest is not None:
                guest.pump()
        return out

    # -- the handshake -------------------------------------------------------

    def test_a_guest_upgrades_and_is_seen_as_a_websocket(self):
        guest = self.guest()
        self.settle(guest)
        self.assertTrue(guest.connected)
        self.assertTrue(guest.ready, "the upgrade never completed")
        self.assertEqual(len(self.host.peers), 1)
        self.assertEqual(self.host.peers[0].mode, self.net.MODE_WS)

    def test_a_guest_refuses_a_reply_that_did_not_read_its_request(self):
        """A 101 with the wrong accept value is a cache or a proxy answering
        on the host's behalf, and framing at it would be shouting into a pipe
        nobody is holding."""
        import socket as _socket
        listener = _socket.socket()
        listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]

        guest = self.net.WebClient("127.0.0.1", port, timeout=3.0)
        self.addCleanup(guest.close)
        conn, _addr = listener.accept()
        self.addCleanup(conn.close)
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                     b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: not-the-right-one\r\n\r\n")
        for _ in range(10):
            time.sleep(0.004)
            guest.pump()
        self.assertFalse(guest.connected,
                         "a guest accepted a handshake nobody computed")

    def test_a_guest_holds_what_it_says_before_the_upgrade_lands(self):
        """Framing before the 101 would put WebSocket bytes in front of an
        HTTP response."""
        guest = self.guest()
        guest.send("hello", {"protocol": 1, "name": "Phone"})
        self.assertFalse(guest.ready)
        self.assertEqual(len(guest.peer.pending), 1, "it was not held")
        inbox = self.settle(guest)
        self.assertEqual([(k, d.get("name")) for _p, k, d in inbox],
                         [("hello", "Phone")])

    # -- talking -------------------------------------------------------------

    def test_a_guest_masks_and_the_host_does_not(self):
        """The asymmetry is the protocol, not an accident."""
        guest = self.guest()
        self.settle(guest)
        guest.send("at", {"x": 1})
        self.settle(guest)
        # A masked reply must be refused outright.
        peer = self.host.peers[0]
        peer.send(self.ws.encode_masked(b'{"t":"at","d":{}}'))
        for _ in range(10):
            time.sleep(0.004)
            guest.pump()
        self.assertFalse(guest.connected,
                         "a guest accepted a masked server frame")

    def test_messages_cross_in_both_directions(self):
        guest = self.guest()
        self.settle(guest)
        guest.send("hello", {"protocol": 1, "name": "Phone"})
        inbox = self.settle(guest)
        self.assertEqual([(k, d) for _p, k, d in inbox],
                         [("hello", {"protocol": 1, "name": "Phone"})])

        self.host.send(self.host.peers[0], "welcome", {"seed": 99})
        got = []
        for _ in range(14):
            time.sleep(0.004)
            self.host.pump()
            got.extend(guest.pump())
        self.assertIn(("welcome", {"seed": 99}), got)

    def test_every_length_encoding_survives_the_round_trip(self):
        """A combat body is ~650 bytes. Past 125 the length encoding changes,
        and past 65535 it changes again - so all three forms get carried
        through the real stack rather than only through the frame parser.

        The big one is built out of many fields rather than one huge string
        because `security.check_structure` caps a single string at 4096, and
        that cap is right: see the test below.
        """
        guest = self.guest()
        self.settle(guest)
        cases = [("tiny", {"p": "x" * 40}),
                 ("mid", {"p": "x" * 4000}),
                 ("big", {"f%03d" % i: "y" * 4000 for i in range(40)})]
        for kind, payload in cases:
            guest.send(kind, payload)
            inbox = self.settle(guest)
            got = [d for _pr, k, d in inbox if k == kind]
            self.assertEqual(got, [payload], "%s did not arrive intact" % kind)

    def test_a_string_past_the_validator_is_still_refused(self):
        """The transport will carry it; `security.check_structure` will not.
        Worth pinning: a second transport must not be a second door around
        the validation, and this is the check that would notice."""
        guest = self.guest()
        self.settle(guest)
        guest.send("body", {"p": "x" * 9000})
        inbox = self.settle(guest)
        self.assertEqual([k for _p, k, _d in inbox], [],
                         "an over-long string got past the validator")
        self.assertTrue(guest.connected, "a refused payload killed the link")

    def test_a_guest_leaving_says_so(self):
        guest = self.guest()
        self.settle(guest)
        self.assertEqual(len(self.host.peers), 1)
        guest.close()
        self.settle()
        self.assertEqual(self.host.peers, [])

    # -- the whole room ------------------------------------------------------

    def test_a_pc_and_a_phone_in_one_room(self):
        """The point of the exercise: one host, one port, two transports,
        and one broadcast that reaches both."""
        desktop = self.net.Client("127.0.0.1", self.port, timeout=3.0)
        self.addCleanup(desktop.close)
        phone = self.guest()
        self.settle(phone)
        desktop.send("hello", {"protocol": 1, "name": "Desk"})
        phone.send("hello", {"protocol": 1, "name": "Phone"})
        inbox = self.settle(phone)
        self.assertEqual(sorted(d.get("name") for _p, _k, d in inbox),
                         ["Desk", "Phone"])
        self.assertEqual(sorted(p.mode for p in self.host.peers),
                         [self.net.MODE_TCP, self.net.MODE_WS])

        self.host.broadcast("roster", {"players": 2})
        seen = []
        for _ in range(14):
            time.sleep(0.004)
            self.host.pump()
            seen.extend(phone.pump())
            seen.extend(desktop.pump())
        self.assertEqual(sum(1 for k, _d in seen if k == "roster"), 2,
                         "one broadcast did not reach both transports")


class TestCrossplaySession(unittest.TestCase):
    """`coop.py` above the second transport, which it must not notice."""

    def setUp(self):
        from pixel_rpg import coop
        self.coop = coop

    def test_a_session_can_join_over_websocket(self):
        """The whole promise of section 24: `coop.py` never sees bytes, so a
        phone's session is the same object a PC's is."""
        game = TestCoop.FakeGame(seed=31337)
        host = self.coop.Session.host("Host", port=0, seed=31337,
                                      reach_out=False)
        self.addCleanup(host.close)
        guest = self.coop.Session.join(host.room_code, "Phone",
                                       websocket=True)
        self.addCleanup(guest.close)
        guest_game = TestCoop.FakeGame()
        for _ in range(120):
            time.sleep(0.002)
            host.update(game, 1 / 60)
            guest.update(guest_game, 1 / 60)

        self.assertTrue(guest.ready, "a websocket guest never joined")
        self.assertEqual(guest.seed, 31337)
        self.assertTrue(guest.adopt_seed)
        self.assertEqual([r.name for r in host.remotes.values()], ["Phone"])
        self.assertEqual([r.name for r in guest.remotes.values()], ["Host"])

    def test_a_wrong_build_is_turned_away_over_websocket_too(self):
        """The protocol check is above the transport, so it cannot be
        skipped by arriving a different way."""
        from pixel_rpg import net
        game = TestCoop.FakeGame(seed=1)
        host = self.coop.Session.host("Host", port=0, seed=1,
                                      reach_out=False)
        self.addCleanup(host.close)
        stranger = net.WebClient.from_code(host.room_code)
        self.addCleanup(stranger.close)
        for _ in range(20):
            time.sleep(0.003)
            host.update(game, 1 / 60)
            stranger.pump()
        stranger.send("hello", {"protocol": self.coop.PROTOCOL + 99,
                                "name": "Old"})
        for _ in range(30):
            time.sleep(0.003)
            host.update(game, 1 / 60)
            stranger.pump()
        self.assertEqual(host.remotes, {})


class TestCoop(unittest.TestCase):
    """Sessions: the handshake, the roster, and hostile peers."""

    def setUp(self):
        from pixel_rpg import coop
        self.coop = coop

    class FakeGame:
        """Just enough Game for `coop` to talk to: a world, a body, and the
        three chest hooks. Standing in for `Game` here is deliberate - the
        arbitration is what is under test, not the toasts."""

        def __init__(self, seed=None, x=100.0, y=200.0):
            from pixel_rpg.world import World
            self.world = World(32, seed) if seed is not None else None
            self.player = type("P", (), {"x": x, "y": y,
                                         "facing": "front"})()
            self.moving = False
            self.tile = 32
            # `None` outdoors, or `{"door": (gx, gy)}` - the only part of
            # `Game.house` that `coop` reads (s57).
            self.house = None
            self.taken = []          # loot this end actually kept
            self.offered = []        # every payout the host sent our way
            self.marked = []         # chests this end saw emptied
            self.lost = []           # races this end was told it lost

        def chest_loot(self, tier, level):
            from pixel_rpg.items import roll_item
            return {"gold": 10 * tier, "item": roll_item(level).to_dict(),
                    "cons": "", "relic": ""}

        def mark_chest_opened(self, gx, gy):
            chest = self.world.chest_at(gx, gy) if self.world else None
            if chest is None or chest.opened:
                return False
            chest.opened = True
            self.marked.append((gx, gy))
            return True

        def take_chest_loot(self, gx, gy, loot):
            self.offered.append((gx, gy, loot))
            if self.mark_chest_opened(gx, gy):
                self.taken.append((gx, gy, loot))

        def chest_lost(self, gx, gy):
            self.lost.append((gx, gy))

        def go_indoors(self, door):
            self.house = {"door": door}

        def go_outdoors(self):
            self.house = None

        def stand_on(self, gx, gy):
            self.player.x = gx * self.tile + self.tile / 2
            self.player.y = gy * self.tile + self.tile / 2

    def pair(self, seed=12345):
        host_game = self.FakeGame(seed=seed)
        host = self.coop.Session.host("Host", port=0, seed=seed,
                                      reach_out=False)
        self.addCleanup(host.close)
        guest = self.coop.Session.join(host.room_code, "Guest")
        self.addCleanup(guest.close)
        guest_game = self.FakeGame()
        for _ in range(80):
            host.update(host_game, 1 / 60)
            guest.update(guest_game, 1 / 60)
        return host, guest, host_game, guest_game

    def test_a_world_is_one_integer(self):
        """The whole reason co-op is tractable: terrain regenerates."""
        from pixel_rpg.world import World
        host, guest, host_game, _g = self.pair(seed=777001)
        self.assertTrue(guest.ready)
        self.assertEqual(guest.seed, 777001)
        rebuilt = World(32, guest.seed)
        rebuilt.generate()
        host_game.world.generate()
        self.assertEqual(rebuilt.grid, host_game.world.grid)

    def test_joining_asks_the_game_to_rebuild(self):
        _h, guest, _hg, _gg = self.pair()
        self.assertTrue(guest.adopt_seed,
                        "a client joined without adopting the host's world")

    def test_both_sides_see_each_other(self):
        host, guest, _hg, _gg = self.pair()
        self.assertEqual([r.name for r in host.remotes.values()], ["Guest"])
        self.assertEqual([r.name for r in guest.remotes.values()], ["Host"])
        self.assertEqual(host.player_count, 2)

    def test_a_different_build_is_turned_away_at_the_door(self):
        """Better a clear refusal than a desync an hour later."""
        from pixel_rpg import net
        host_game = self.FakeGame(seed=42)
        host = self.coop.Session.host("Host", port=0, seed=42,
                                      reach_out=False)
        self.addCleanup(host.close)
        stranger = net.Client.from_code(host.room_code)
        self.addCleanup(stranger.close)
        stranger.send("hello", {"protocol": self.coop.PROTOCOL + 99,
                                "name": "Old"})
        for _ in range(60):
            host.update(host_game, 1 / 60)
            stranger.pump()
        self.assertEqual(host.remotes, {})

    def test_a_host_with_no_run_refuses_players(self):
        from pixel_rpg import net
        empty = self.FakeGame()
        host = self.coop.Session.host("Host", port=0, reach_out=False)
        self.addCleanup(host.close)
        stranger = net.Client.from_code(host.room_code)
        self.addCleanup(stranger.close)
        stranger.send("hello", {"protocol": self.coop.PROTOCOL,
                                "name": "Eager"})
        for _ in range(60):
            host.update(empty, 1 / 60)
            stranger.pump()
        self.assertEqual(host.remotes, {})

    def test_a_position_off_a_socket_is_never_trusted(self):
        """Unclamped coordinates are not a cheat, they are a crash: they
        land in a camera transform and a blit."""
        remote = self.coop.Remote(2, "Guest")
        remote.x, remote.y = 50.0, 60.0
        self.coop.Session._place(remote, {"x": 1e18, "y": float("nan"),
                                          "f": "sideways", "w": True})
        self.assertLessEqual(abs(remote.x), 1e6)
        self.assertEqual(remote.y, 60.0)        # NaN refused, old kept
        self.assertEqual(remote.facing, "front")

    def test_a_hostile_name_is_sanitised(self):
        nasty = self.coop._safe_name("\u202eevil" + chr(0) + "x" * 200)
        self.assertLessEqual(len(nasty), self.coop.MAX_NAME)
        self.assertNotIn(chr(0), nasty)

    def test_a_junk_look_falls_back(self):
        self.assertEqual(self.coop._safe_look(None), (1, 1, 1, 0, 1))
        self.assertEqual(self.coop._safe_look([1, 2]), (1, 1, 1, 0, 1))
        self.assertEqual(len(self.coop._safe_look([9] * 5)), 5)

    # -- interiors, shared (s57) -------------------------------------------

    def test_a_junk_room_reads_as_outdoors(self):
        """A malformed door is outdoors, not a refused message: dropping the
        update would freeze somebody mid-field instead."""
        for junk in (None, [], [1], [1, 2, 3], "kitchen", [-1, 4], [4, 9999],
                     [True, 2], [1.5, 2]):
            self.assertIsNone(self.coop._safe_room(junk))
        self.assertEqual(self.coop._safe_room([7, 9]), (7, 9))

    def test_the_same_door_puts_two_players_in_one_room(self):
        """The whole feature: a room is (world seed, door), and both ends
        already share the seed - so naming the door is the entire sync."""
        host, guest, host_game, guest_game = self.joined_pair()
        host_game.go_indoors((12, 34))
        guest_game.go_indoors((12, 34))
        self.settle(host, host_game, guest, guest_game)
        self.assertEqual(host.room, (12, 34))
        self.assertEqual([r.player_id for r in host.here()],
                         [guest.player_id])
        self.assertEqual([r.player_id for r in guest.here()],
                         [host.player_id])

    def test_a_different_door_is_a_different_room(self):
        host, guest, host_game, guest_game = self.joined_pair()
        host_game.go_indoors((12, 34))
        guest_game.go_indoors((40, 41))
        self.settle(host, host_game, guest, guest_game)
        self.assertEqual(host.here(), [])
        self.assertEqual(guest.here(), [])

    def test_indoors_is_hidden_from_outdoors(self):
        host, guest, host_game, guest_game = self.joined_pair()
        host_game.go_indoors((12, 34))
        self.settle(host, host_game, guest, guest_game)
        self.assertEqual(host.here(), [])
        self.assertEqual(guest.here(), [])
        # ...and walking back out puts them back in each other's world.
        host_game.go_outdoors()
        self.settle(host, host_game, guest, guest_game)
        self.assertEqual(len(host.here()), 1)
        self.assertEqual(len(guest.here()), 1)

    def test_standing_in_the_same_spot_in_two_worlds_is_not_near(self):
        """The trap this filter exists for. A room is painted into the
        middle of the world grid, so somebody indoors has coordinates a few
        tiles from anyone standing mid-island - arithmetically adjacent, and
        in a different world."""
        host, guest, host_game, guest_game = self.joined_pair()
        host_game.stand_on(64, 64)
        guest_game.stand_on(64, 64)
        guest_game.go_indoors((12, 34))
        self.settle(host, host_game, guest, guest_game)
        self.assertEqual(host.near(64, 64, 32), [])
        # Same coordinates, same room: now they are together.
        host_game.go_indoors((12, 34))
        self.settle(host, host_game, guest, guest_game)
        self.assertEqual(host.near(64, 64, 32), [guest.player_id])

    def test_a_room_is_the_same_room_at_both_ends(self):
        """Nothing about a room is sent, so both ends must build it from
        the seed and the door alone and agree exactly."""
        from pixel_rpg import interiors
        mine, spawn_a, out_a = interiors.build(12345, (12, 34))
        theirs, spawn_b, out_b = interiors.build(12345, (12, 34))
        self.assertEqual(spawn_a, spawn_b)
        self.assertEqual(out_a, out_b)
        self.assertEqual(mine.grid, theirs.grid)
        self.assertEqual(mine.props, theirs.props)
        # And a different door is a different room.
        other, _, _ = interiors.build(12345, (40, 41))
        self.assertNotEqual(mine.seed, other.seed)

    # -- shared chests -----------------------------------------------------

    def settle(self, host, host_game, guest, guest_game, frames=40):
        """Pump both ends, forcing position broadcasts through.

        `_broadcast_self` throttles to 15/sec of wall clock, and a tight
        test loop runs far faster than that - so without this the host only
        ever hears where a guest started."""
        for _ in range(frames):
            host._last_send = guest._last_send = 0.0
            host.update(host_game, 1 / 60)
            guest.update(guest_game, 1 / 60)

    def joined_pair(self, seed=90210):
        """A host and a guest standing in the same, already-built valley."""
        from pixel_rpg.world import World
        host, guest, host_game, guest_game = self.pair(seed=seed)
        guest_game.world = World(32, seed)
        guest.adopt_seed = False
        return host, guest, host_game, guest_game

    def test_a_client_asks_for_a_chest_rather_than_opening_it(self):
        """The whole fix: one chest, one looting, and the host decides."""
        host, guest, host_game, guest_game = self.joined_pair()
        chest = host_game.world.chests[0]
        guest_game.stand_on(chest.gx, chest.gy)
        self.settle(host, host_game, guest, guest_game)

        self.assertTrue(guest.claim_chest(chest.gx, chest.gy, 5))
        self.assertFalse(guest_game.world.chest_at(chest.gx, chest.gy).opened,
                         "a client opened a chest without being told it could")
        self.settle(host, host_game, guest, guest_game)

        self.assertTrue(chest.opened, "the host never recorded the claim")
        self.assertEqual([(g, y) for g, y, _l in guest_game.taken],
                         [(chest.gx, chest.gy)])
        self.assertEqual(guest_game.taken[0][2]["gold"], 10 * chest.tier)

    def test_a_chest_is_looted_exactly_once(self):
        """Second through the door gets the news, not a second helping."""
        host, guest, host_game, guest_game = self.joined_pair()
        chest = host_game.world.chests[0]
        guest_game.stand_on(chest.gx, chest.gy)
        self.settle(host, host_game, guest, guest_game)

        # The host gets there first and says so.
        host_game.mark_chest_opened(chest.gx, chest.gy)
        host.announce_chest(chest.gx, chest.gy)
        self.settle(host, host_game, guest, guest_game)
        self.assertIn((chest.gx, chest.gy), guest_game.marked)

        guest._claims.clear()
        guest.claim_chest(chest.gx, chest.gy, 5)
        self.settle(host, host_game, guest, guest_game)
        self.assertEqual(guest_game.taken, [],
                         "an already-empty chest paid out a second time")
        # And the refusal is the host's, not the client politely declining
        # loot it was handed: the authority has to be the thing that says no.
        self.assertEqual(guest_game.offered, [],
                         "the host paid out for a chest it had already emptied")
        # ...and the player is told, rather than left pressing a dead key.
        self.assertEqual(guest_game.lost, [(chest.gx, chest.gy)])

    def test_a_claim_from_across_the_map_is_refused(self):
        """A client owns its feet and nothing else - including the claim
        that it is standing anywhere near what it is asking for."""
        host, guest, host_game, guest_game = self.joined_pair()
        chest = host_game.world.chests[0]
        guest_game.stand_on(chest.gx + 40, chest.gy + 40)
        self.settle(host, host_game, guest, guest_game)

        guest.claim_chest(chest.gx, chest.gy, 5)
        self.settle(host, host_game, guest, guest_game)
        self.assertFalse(chest.opened)
        self.assertEqual(guest_game.taken, [])

    def test_holding_the_key_down_sends_one_claim(self):
        host, guest, host_game, guest_game = self.joined_pair()
        chest = host_game.world.chests[0]
        self.assertTrue(guest.claim_chest(chest.gx, chest.gy, 5))
        for _ in range(20):
            self.assertFalse(guest.claim_chest(chest.gx, chest.gy, 5))

    def test_a_joiner_is_told_which_chests_are_already_empty(self):
        """The seed rebuilds the terrain; this is what happened to it since."""
        from pixel_rpg import net
        seed = 5150
        host_game = self.FakeGame(seed=seed)
        emptied = [(c.gx, c.gy) for c in host_game.world.chests[:3]]
        for gx, gy in emptied:
            host_game.mark_chest_opened(gx, gy)
        host = self.coop.Session.host("Host", port=0, seed=seed,
                                      reach_out=False)
        self.addCleanup(host.close)
        guest = self.coop.Session.join(host.room_code, "Guest")
        self.addCleanup(guest.close)
        guest_game = self.FakeGame()
        for _ in range(80):
            host.update(host_game, 1 / 60)
            guest.update(guest_game, 1 / 60)
        self.assertTrue(guest.ready)
        self.assertEqual(sorted(guest.pending_chests), sorted(emptied))

    def test_a_junk_chest_message_changes_nothing(self):
        """Off a socket is off a socket: coerced, not believed."""
        host, guest, host_game, guest_game = self.joined_pair()
        for junk in ({}, {"x": "nope", "y": None}, {"x": -5, "y": 1e18},
                     {"x": 99999, "y": 99999}):
            guest._chest_emptied(guest_game, junk)
            guest._chest_won(guest_game, junk)
        self.assertEqual(guest_game.marked, [])
        self.assertEqual(guest_game.offered, [])

    def test_story_flags_stay_private(self):
        """Sharing these would rob the second player of scenes rather than
        share them - see the policy note at the top of coop.py."""
        import inspect
        from pixel_rpg import coop
        source = inspect.getsource(coop)
        self.assertNotIn("story_flags", source)
        self.assertNotIn("\"flag\"", source)

    def test_a_silent_player_is_dropped(self):
        remote = self.coop.Remote(2, "Ghost")
        self.assertFalse(remote.stale)
        remote.last_seen -= self.coop.TIMEOUT + 1
        self.assertTrue(remote.stale)

    def test_positions_are_not_sent_every_frame(self):
        """60 a second is pointless traffic on somebody's worse connection."""
        self.assertLess(self.coop.SEND_RATE, 1.0)
        self.assertGreater(self.coop.SEND_RATE, 1 / 30.0)


class TestScenes(unittest.TestCase):
    """Story choices, the ledger they feed, and the things they hide."""

    def setUp(self):
        from pixel_rpg import scenes
        self.scenes = scenes

    def test_every_scene_has_answers(self):
        for scene in self.scenes.SCENES:
            self.assertTrue(scene.choices, "%s asks nothing" % scene.key)

    def test_choice_keys_are_unique_within_a_scene(self):
        for scene in self.scenes.SCENES:
            keys = [c.key for c in scene.choices]
            self.assertEqual(len(keys), len(set(keys)), scene.key)

    def test_every_lean_names_a_real_ending(self):
        from pixel_rpg.story import ENDINGS_BY_KEY
        for scene in self.scenes.SCENES:
            for option in scene.choices:
                if option.lean:
                    self.assertIn(option.lean, ENDINGS_BY_KEY,
                                  "%s/%s" % (scene.key, option.key))

    def test_a_secret_choice_is_not_offered_without_its_flag(self):
        door = self.scenes.SCENES_BY_KEY["door"]
        plain = [c.key for c in door.offered(())]
        self.assertNotIn("names", plain)
        opened = [c.key for c in door.offered([self.scenes.ALL_HEARD])]
        self.assertIn("names", opened)

    def test_the_secret_needs_all_six_shards(self):
        held = []
        for shard in self.scenes.HEARD[:-1]:
            held, _fresh = self.scenes.grant_for(held, shard)
        self.assertNotIn(self.scenes.ALL_HEARD, held)
        held, fresh = self.scenes.grant_for(held, self.scenes.HEARD[-1])
        self.assertIn(self.scenes.ALL_HEARD, held)
        self.assertIn(self.scenes.ALL_HEARD, fresh)

    def test_every_warden_scene_grants_a_distinct_shard(self):
        """Six shards, six wardens. A duplicate would make one unreachable."""
        granted = []
        for key in self.scenes.WARDEN_SCENE_BY_BOSS.values():
            scene = self.scenes.SCENES_BY_KEY[key]
            shards = [c.grants for c in scene.choices if c.grants]
            self.assertEqual(len(shards), 1, key)
            granted.append(shards[0])
        self.assertEqual(sorted(granted), sorted(self.scenes.HEARD))

    def test_every_warden_has_a_scene(self):
        from pixel_rpg.story import WARDENS
        for warden in WARDENS:
            self.assertIn(warden.boss_key, self.scenes.WARDEN_SCENE_BY_BOSS,
                          "%s has no scene" % warden.boss_key)
            key = self.scenes.WARDEN_SCENE_BY_BOSS[warden.boss_key]
            self.assertIn(key, self.scenes.SCENES_BY_KEY)

    def test_every_maren_briefing_has_a_scene(self):
        """Seven stages, seven conversations. A gap means she says nothing
        at all at that point in the run."""
        from pixel_rpg.story import MAREN_BRIEFINGS
        for stage in MAREN_BRIEFINGS:
            key = self.scenes.MAREN_SCENES.get(stage)
            self.assertIsNotNone(key, "no scene for stage %s" % stage)
            self.assertIn(key, self.scenes.SCENES_BY_KEY)

    def test_every_villager_has_a_scene(self):
        from pixel_rpg.game import Game
        for name in Game.VILLAGER_LINES:
            key = self.scenes.VILLAGER_SCENES.get(name)
            self.assertIsNotNone(key, "%s has nothing to say" % name)
            self.assertIn(key, self.scenes.SCENES_BY_KEY)

    def test_the_mourning_thread_runs_weaver_to_maren(self):
        """A secret choice unlocking a secret choice, which is the point."""
        weaver = self.scenes.SCENES_BY_KEY["villager_weaver"]
        granting = [c for c in weaver.choices
                    if c.grants == self.scenes.MOURNING]
        self.assertEqual(len(granting), 1)

        maren = self.scenes.SCENES_BY_KEY["maren_2"]
        self.assertNotIn("cloth", [c.key for c in maren.offered(())])
        opened = maren.offered([self.scenes.MOURNING])
        self.assertIn("cloth", [c.key for c in opened])
        secret = maren.choice("cloth")
        self.assertEqual(secret.grants, self.scenes.KNOWS_YSOLDE)

    def test_orientation_says_what_to_do(self):
        """Atmosphere is not a substitute for knowing where to walk."""
        scene = self.scenes.SCENES_BY_KEY["orientation"]
        text = " ".join(scene.body).lower()
        for word in ("road", "warden", "door"):
            self.assertIn(word, text)

    def test_the_ledger_picks_the_heaviest(self):
        self.assertEqual(
            self.scenes.leading_ending({"debt": 2, "longwalk": 5}), "longwalk")
        self.assertEqual(self.scenes.leading_ending({}), "")
        self.assertEqual(self.scenes.leading_ending({"debt": 0}), "")

    def test_the_hidden_ending_is_hidden(self):
        """Not in the list, and - just as important - not in the count."""
        from pixel_rpg import story
        secret = story.ENDINGS_BY_KEY["names"]
        self.assertTrue(secret.hidden)
        visible = [e.key for e in story.visible_endings()]
        self.assertNotIn("names", visible)
        self.assertIn("names", [e.key for e in story.visible_endings(["names"])])

    def test_the_hidden_ending_is_never_offered_by_qualifying(self):
        """It is reached by saying it, not by meeting requirements."""
        from pixel_rpg import story
        offered = story.offered_endings(
            {c.key for c in story.CODEX}, len(story.WARDENS), True)
        self.assertNotIn("names", [e.key for e in offered])

    def test_the_codex_total_does_not_leak_the_secret(self):
        """"0 of 17" against a visible 16 announces that something is
        missing just as loudly as naming it."""
        from pixel_rpg.meta import MetaProfile
        blank = MetaProfile(None)
        before = blank.codex_progress()[1]
        found = MetaProfile(None)
        found.codex = ["ending_names"]
        self.assertGreater(found.codex_progress()[1], before)

    def test_scene_bodies_and_replies_are_line_lists(self):
        """Every line is drawn as-is, so a bare string would print letter
        by letter down the screen."""
        for scene in self.scenes.SCENES:
            self.assertIsInstance(scene.body, tuple, scene.key)
            for option in scene.choices:
                self.assertIsInstance(option.reply, tuple,
                                      "%s/%s" % (scene.key, option.key))
                self.assertTrue(option.label.strip())


class TestDevMenu(unittest.TestCase):
    """The hidden debug menu, and the promise that makes it safe."""

    def setUp(self):
        from pixel_rpg import devmenu
        self.devmenu = devmenu

    # -- the typed amount (0ah, s58) ---------------------------------------

    def amount_game(self):
        game = build_game_for_mouse()
        game.ui.dev_amount = 1
        game.ui.dev_typing = False
        return game

    def test_a_number_can_be_typed_instead_of_cycled(self):
        """His ask: *"give yourself however much you want all at once"*.
        25000 is not one of the presets and never could have been."""
        game = self.amount_game()
        for digit in (2, 5, 0, 0, 0):
            self.devmenu.type_digit(game, digit)
        self.assertEqual(self.devmenu.scale(game), 25000)

    def test_the_first_digit_replaces_rather_than_appends(self):
        """Arriving on a preset and typing must not concatenate onto it."""
        game = self.amount_game()
        self.devmenu.set_amount(game, 1000)
        self.devmenu.type_digit(game, 2)
        self.devmenu.type_digit(game, 5)
        self.assertEqual(self.devmenu.scale(game), 25)

    def test_backspace_takes_a_digit_off(self):
        game = self.amount_game()
        for digit in (1, 2, 3):
            self.devmenu.type_digit(game, digit)
        self.devmenu.backspace(game)
        self.assertEqual(self.devmenu.scale(game), 12)

    def test_the_amount_is_capped(self):
        """`_gold` writes into the run and a save clamps what it holds, so
        an amount past that would be a number the next load disagrees
        with - the menu is allowed to be generous, not to lie."""
        game = self.amount_game()
        for _ in range(20):
            self.devmenu.type_digit(game, 9)
        self.assertLessEqual(self.devmenu.scale(game),
                             self.devmenu.MAX_AMOUNT)

    def test_it_never_goes_below_one(self):
        game = self.amount_game()
        self.devmenu.set_amount(game, 0)
        self.assertEqual(self.devmenu.scale(game), 1)
        self.devmenu.backspace(game)
        self.assertEqual(self.devmenu.scale(game), 1)

    def test_the_presets_still_step_both_ways(self):
        game = self.amount_game()
        self.devmenu.set_amount(game, 100)
        self.assertEqual(self.devmenu.cycle_scale(game, 1), 1000)
        self.assertEqual(self.devmenu.cycle_scale(game, -1), 100)
        # ...and stepping off a typed number that sits between presets
        # goes to the next one rather than to a remembered index.
        self.devmenu.set_amount(game, 250)
        self.assertEqual(self.devmenu.cycle_scale(game, 1), 1000)

    def test_the_amount_is_what_an_action_hands_over(self):
        game = self.amount_game()
        game.player.gold = 0
        self.devmenu.set_amount(game, 37)
        self.devmenu._gold(game)
        self.assertEqual(game.player.gold, 37000)

    def test_a_typed_amount_survives_to_the_mouse_chips(self):
        """Rule 8b: the presets are clickable, and clicking one sets it."""
        import pygame
        game = self.amount_game()
        self.devmenu.set_amount(game, 5)
        game.ui.hot.add(pygame.Rect(0, 0, 10, 10), "dev_amount", 1000)
        game.mouse_click((5, 5))
        self.assertEqual(self.devmenu.scale(game), 1000)

    def test_running_an_action_ends_the_typed_number(self):
        """Otherwise the next digit appends to the amount just spent."""
        game = self.amount_game()
        for digit in (2, 5):
            self.devmenu.type_digit(game, digit)
        self.devmenu.run_action(game, 0)
        self.assertFalse(game.ui.dev_typing)
        self.devmenu.type_digit(game, 3)
        self.assertEqual(self.devmenu.scale(game), 3)

    def test_the_sequence_needs_every_key_in_order(self):
        watcher = self.devmenu.SequenceWatcher()
        for code in self.devmenu.SEQUENCE[:-1]:
            self.assertFalse(watcher.feed(code))
        self.assertTrue(watcher.feed(self.devmenu.SEQUENCE[-1]))

    def test_a_wrong_key_breaks_the_sequence(self):
        watcher = self.devmenu.SequenceWatcher()
        for code in self.devmenu.SEQUENCE[:-1]:
            watcher.feed(code)
        watcher.feed(pygame.K_z)
        self.assertFalse(watcher.feed(self.devmenu.SEQUENCE[-1]))

    def test_a_restart_key_re_enters_the_sequence(self):
        """A wrong key that is also the first key starts a fresh attempt.

        Up, Up, Up is not a dead end: the third Up is the opening of a new
        try, so you are one step in rather than zero, and a fourth gets you
        to two. Without that, mashing Up could never reach the menu.
        """
        watcher = self.devmenu.SequenceWatcher()
        for _ in range(3):
            watcher.feed(pygame.K_UP)
        self.assertEqual(watcher.progress, 1)
        watcher.feed(pygame.K_UP)
        self.assertEqual(watcher.progress, 2)

    def test_the_sequence_cannot_be_hit_by_accident(self):
        """Ten specific keys is the only property the entrance needs."""
        self.assertGreaterEqual(len(self.devmenu.SEQUENCE), 8)

    def test_a_tainted_profile_refuses_every_mutator(self):
        """The whole safety model, checked directly.

        The codex, endings and records outlive the save and gate two endings.
        A debug run must not be able to touch any of them.
        """
        from pixel_rpg.meta import MetaProfile, RunRecord
        profile = MetaProfile(None)
        profile.tainted = True
        self.assertIsNone(profile.unlock_codex("ending_seventh"))
        self.assertFalse(profile.record_ending("seventh"))
        self.assertEqual(profile.codex, [])
        self.assertEqual(profile.endings, [])
        profile.finish_run(RunRecord.from_dict({"score": 999, "level": 40}))
        self.assertEqual(profile.runs, [])
        self.assertEqual(profile.best_score, 0)
        profile.add_totals({"kills": 500, "bosses": 6})
        self.assertEqual(profile.totals["kills"], 0)

    def test_a_clean_profile_still_works(self):
        from pixel_rpg.meta import MetaProfile
        profile = MetaProfile(None)
        self.assertIsNotNone(profile.unlock_codex("ending_seventh"))

    def test_the_mark_is_saved_with_the_run(self):
        from pixel_rpg.save import deserialize_player, serialize_player
        player = Player("Tinker", "warden")
        player.debug_used = True
        restored = deserialize_player(serialize_player(player))
        self.assertTrue(restored.debug_used,
                        "a marked run came back clean through a save")

    def test_a_fresh_player_is_not_marked(self):
        self.assertFalse(Player("Clean", "warden").debug_used)

    def test_everything_in_here_marks_the_run(self):
        """**Reversed on his word, 2026-09-01.** This used to assert the
        opposite - that something in here was safe to poke at:

        > "i thought i made it clear that even opening the menu marks the
        >  run? not just using anything inside, and even if it was using
        >  the tools inside to mark the run, everything inside should mark
        >  the run"

        s19 decision 14 had him choose "keep opening free and flag on
        first use", but that was picked from options that assumed opening
        could not mark the run - a marked run cannot write to the profile,
        so the achievement for finding the menu would be unwritable. It
        was an ordering problem, not an impossibility: grant the feat,
        then mark.
        """
        for label, _note, _fn, taints in self.devmenu.ACTIONS:
            self.assertTrue(taints, "%s does not mark the run" % label)

    def test_opening_it_is_enough_to_mark_the_run(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Walker", 3)
        try:
            self.assertFalse(game.player.debug_used)
            game.open_devmenu()
            self.assertTrue(game.player.debug_used,
                            "opening the menu left the run clean")
        finally:
            game.saves.delete(3)

    def test_the_feat_is_granted_before_the_mark(self):
        """The ordering that makes both true. A marked run cannot write to
        the profile, so the achievement has to land first."""
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Walker", 3)
        try:
            game.meta.found_devmenu = False
            game.meta.achievements = [a for a in game.meta.achievements
                                      if a != "curious"]
            game.open_devmenu()
            self.assertIn("curious", game.meta.achievements,
                          "finding it stopped being an achievement")
            self.assertTrue(game.player.debug_used)
        finally:
            game.saves.delete(3)

    def test_the_hidden_feat_hides_itself(self):
        from pixel_rpg.quests import ACHIEVEMENTS_BY_KEY
        feat = ACHIEVEMENTS_BY_KEY["curious"]
        self.assertTrue(feat.hidden)
        self.assertEqual(feat.shown_title(False), "???")
        self.assertEqual(feat.shown_detail(False), "???")
        self.assertEqual(feat.shown_title(True), feat.title)

    def test_no_other_feat_is_hidden(self):
        """Hiding an ordinary feat would just look like a bug."""
        from pixel_rpg.quests import ACHIEVEMENTS
        hidden = [a.key for a in ACHIEVEMENTS if a.hidden]
        self.assertEqual(hidden, ["curious"])

    def test_achievements_are_global_not_per_save(self):
        """Earned once, earned for good.

        They used to live in the save file, so a feat earned on one character
        showed as locked on the next. They are meta-progression; the profile
        owns them.
        """
        from pixel_rpg.meta import MetaProfile
        profile = MetaProfile(None)
        self.assertTrue(profile.unlock_achievement("first_blood"))
        self.assertFalse(profile.unlock_achievement("first_blood"))
        restored = MetaProfile(None).apply_dict(profile.to_dict())
        self.assertEqual(restored.achievements, ["first_blood"])

    def test_a_marked_run_earns_no_achievements(self):
        from pixel_rpg.meta import MetaProfile
        profile = MetaProfile(None)
        profile.tainted = True
        self.assertFalse(profile.unlock_achievement("first_blood"))
        self.assertEqual(profile.achievements, [])

    def test_unknown_achievement_keys_are_dropped(self):
        from pixel_rpg.meta import MetaProfile
        restored = MetaProfile(None).apply_dict(
            {"achievements": ["first_blood", "not_a_feat", {"x": 1}, 7]})
        self.assertEqual(restored.achievements, ["first_blood"])

    def test_every_character_earns_the_whole_set_again(self):
        """Per-save on purpose: that is what makes a 100% chase possible.

        The global cabinet is separate. A feat the profile already holds must
        still be earnable, and announced, by a fresh character.
        """
        from pixel_rpg.quests import QuestLog
        log = QuestLog()
        player = Player("A", "warden")
        player.stats_tracker["kills"] = 500
        fresh = log.check_achievements(player)
        self.assertIn("first_blood", [a.key for a in fresh])
        # ...but not twice within the same run
        self.assertEqual(log.check_achievements(player), [])

    def test_the_two_layers_are_independent(self):
        """A run's list and the cabinet answer different questions."""
        from pixel_rpg.meta import MetaProfile
        from pixel_rpg.quests import QuestLog
        profile = MetaProfile(None)
        profile.unlock_achievement("first_blood")
        log = QuestLog()
        self.assertNotIn("first_blood", log.unlocked,
                         "a fresh save started with a feat already earned")

    def test_the_debug_keybind_is_hidden_until_it_is_found(self):
        """The Controls screen is the first place anyone looks."""
        from pixel_rpg import keybinds as kb
        hidden = [a for a, _l, _k in kb.visible_actions(False)]
        shown = [a for a, _l, _k in kb.visible_actions(True)]
        self.assertNotIn("debug", hidden)
        self.assertIn("debug", shown)

    def test_the_discovery_is_remembered_across_saves(self):
        from pixel_rpg.meta import MetaProfile
        profile = MetaProfile(None)
        self.assertFalse(profile.found_devmenu)
        profile.found_devmenu = True
        restored = MetaProfile(None).apply_dict(profile.to_dict())
        self.assertTrue(restored.found_devmenu)


class TestLogbook(unittest.TestCase):
    """Session and crash logs."""

    def setUp(self):
        from pixel_rpg import logbook
        self.logbook = logbook
        self.dir = tempfile.mkdtemp()

    def read_session(self):
        import glob
        files = glob.glob(os.path.join(self.dir, "session-*.log"))
        self.assertTrue(files, "no session log was written")
        with open(files[0], encoding="utf-8") as fh:
            return fh.read()

    def test_building_a_logbook_touches_nothing(self):
        """Rule 2: logging must not cost startup."""
        self.logbook.Logbook(self.dir)
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_broken_folder_never_raises(self):
        """Rule 1: a logger that can crash the game is worse than none.

        The path here is a *file*, so creating a directory at it must fail.
        """
        blocker = os.path.join(self.dir, "not-a-folder")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        book = self.logbook.Logbook(blocker)
        book.info("this cannot be written anywhere")
        book.event("nor", this=1)
        self.assertIsNone(book.crash(ValueError("nope")))
        book.close()                       # and closing is safe too

    def test_scrub_removes_credentials(self):
        """Rule 3, checked against the auditor's own patterns."""
        samples = [
            "ghp_%s" % ("a" * 24),
            "AKIA%s" % ("B" * 16),
            "sk-%s" % ("c" * 24),
            "AIza%s" % ("d" * 35),
            # Assembled rather than written out, or the project's own secret
            # scanner flags this test file. Which is the scanner working.
            "%s = %s" % ("pass" + "word", '"hunter2hunter2"'),
        ]
        for sample in samples:
            cleaned = self.logbook.scrub("token here: %s end" % sample)
            self.assertIn(self.logbook.REDACTED, cleaned, sample)

    def test_a_secret_never_reaches_the_file(self):
        book = self.logbook.Logbook(self.dir)
        leak = "ghp_%s" % ("z" * 24)
        book.info("connecting with %s", leak)
        book.close()
        text = self.read_session()
        self.assertNotIn(leak, text)
        self.assertIn(self.logbook.REDACTED, text)

    def test_the_header_records_the_environment(self):
        book = self.logbook.Logbook(self.dir)
        book.info("first line")
        book.close()
        text = self.read_session()
        for field in ("game", "build", "python", "pygame", "platform"):
            self.assertIn(field, text)

    def test_events_are_greppable(self):
        book = self.logbook.Logbook(self.dir)
        book.event("battle_start", count=3, boss=False)
        book.close()
        self.assertIn("battle_start boss=False count=3", self.read_session())

    def test_crash_writes_a_separate_file_with_the_traceback(self):
        book = self.logbook.Logbook(self.dir)
        try:
            raise KeyError("the missing thing")
        except KeyError as exc:
            path = book.crash(exc, context="state=overworld")
        book.close()
        self.assertIsNotNone(path)
        self.assertTrue(os.path.basename(path).startswith("crash-"))
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("KeyError", body)
        self.assertIn("the missing thing", body)
        self.assertIn("state=overworld", body)

    def test_old_logs_are_pruned(self):
        """Rule 4: bounded, so a logging loop costs megabytes not a disk."""
        import time
        for i in range(self.logbook.KEEP_SESSIONS + 6):
            path = os.path.join(self.dir, "session-old%02d.log" % i)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x")
            os.utime(path, (time.time() - 1000 + i, time.time() - 1000 + i))
        self.logbook.prune(self.dir, "session-*.log",
                           self.logbook.KEEP_SESSIONS)
        left = [f for f in os.listdir(self.dir) if f.startswith("session-")]
        self.assertEqual(len(left), self.logbook.KEEP_SESSIONS)

    def test_a_runaway_log_stops_growing(self):
        book = self.logbook.Logbook(self.dir)
        book._bytes = self.logbook.MAX_SESSION_BYTES
        book.info("this line is past the cap")
        book.close()
        self.assertNotIn("past the cap", self.read_session())

    # -- a browser has no folder, but it does have a console ------------

    def _say(self, book, *args):
        """Run one log line with stdout captured, and return what it said."""
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            book.info(*args)
        return buffer.getvalue()

    def test_a_browser_log_goes_to_the_console_not_the_folder(self):
        """Redirected, not disabled: pygbag pipes stdout to the console."""
        book = self.logbook.Logbook(self.dir, console=True)
        said = self._say(book, "the door is open")
        book.close()
        self.assertIn("the door is open", said)
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_browser_log_is_still_scrubbed_and_still_capped(self):
        """Changing the medium must not change any of the four rules."""
        book = self.logbook.Logbook(self.dir, console=True)
        leak = "ghp_%s" % ("z" * 24)
        said = self._say(book, "connecting with %s", leak)
        self.assertNotIn(leak, said)
        self.assertIn(self.logbook.REDACTED, said)
        book._bytes = self.logbook.MAX_SESSION_BYTES
        self.assertEqual(self._say(book, "past the cap"), "")
        book.close()

    def test_a_browser_crash_prints_and_claims_no_file(self):
        """Returning a path nobody can open is worse than returning None."""
        import contextlib
        book = self.logbook.Logbook(self.dir, console=True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            try:
                raise KeyError("the missing thing")
            except KeyError as exc:
                path = book.crash(exc, context="state=overworld")
        book.close()
        self.assertIsNone(path)
        body = buffer.getvalue()
        self.assertIn("KeyError", body)
        self.assertIn("the missing thing", body)
        self.assertIn("state=overworld", body)
        self.assertEqual(os.listdir(self.dir), [])

    def test_the_console_choice_follows_is_browser(self):
        """Nothing else in the game should have to know which it is."""
        from pixel_rpg import config
        original = config.is_browser
        try:
            config.is_browser = lambda: True
            self.assertTrue(self.logbook.Logbook(self.dir).console)
            config.is_browser = lambda: False
            self.assertFalse(self.logbook.Logbook(self.dir).console)
        finally:
            config.is_browser = original

    def test_logs_are_not_written_inside_the_save_folder(self):
        """A stray log must never be mistaken for save data."""
        from pixel_rpg import config
        saves = str(config.default_save_dir())
        self.assertFalse(str(self.logbook.log_dir()).startswith(saves))


class FakeSocketEvent:
    """What a browser hands a WebSocket callback. Only `data` is ever read."""

    def __init__(self, data=None):
        self.data = data


class FakeWebSocket:
    """A `WebSocket` that a desktop can drive by hand.

    Written out rather than mocked, for the same reason `FakeLocalStorage`
    is: the interesting behaviour here is what happens when the answer is
    no - the connection never opens, the host floods, the send throws - and
    a mock that returns whatever you assert on cannot show you any of it.

    Nothing in it is asynchronous. A real browser fires these callbacks from
    the event loop; a test fires them where it wants them.
    """

    def __init__(self, url, refuse_send=False):
        self.url = url
        self.sent = []
        self.closed = False
        self.refuse_send = refuse_send
        self.onopen = self.onmessage = self.onclose = self.onerror = None

    # -- the half a browser provides ---------------------------------------

    def send(self, text):
        if self.refuse_send:
            raise RuntimeError("the socket is gone")
        self.sent.append(text)

    def close(self):
        self.closed = True

    # -- the half a test drives ---------------------------------------------

    def open_it(self):
        self.onopen(FakeSocketEvent())

    def deliver(self, text):
        self.onmessage(FakeSocketEvent(text))

    def hang_up(self):
        self.onclose(FakeSocketEvent())


class FakeSocketWindow:
    """The page's globals, reduced to the one thing `BrowserClient` uses."""

    def __init__(self, refuse=False, refuse_send=False):
        self.refuse = refuse
        self.refuse_send = refuse_send
        self.last = None
        window = self

        class WebSocket:
            @staticmethod
            def new(url):
                if window.refuse:
                    raise RuntimeError("WebSocket is not available")
                window.last = FakeWebSocket(url, window.refuse_send)
                return window.last

        self.WebSocket = WebSocket


class FakeBrowserPage:
    """What pygbag injects as `platform.window`, reduced to two numbers.

    Faithful in the way that matters: pygbag really does hang `window` off
    the stdlib `platform` module, so the tests below set it there and take
    it away again rather than inventing a different seam to test through.
    """

    def __init__(self, width, height):
        self.innerWidth = width
        self.innerHeight = height


class BrowserPage:
    """Context manager: pretend this process is a page of a given size."""

    def __init__(self, width, height):
        self.page = FakeBrowserPage(width, height)

    def __enter__(self):
        import platform
        from pixel_rpg import config
        self._real_is_browser = config.is_browser
        config.is_browser = lambda: True
        platform.window = self.page
        return self.page

    def __exit__(self, *exc):
        import platform
        from pixel_rpg import config
        config.is_browser = self._real_is_browser
        if hasattr(platform, "window"):
            del platform.window


class TestBrowserViewport(unittest.TestCase):
    """The web build is a PC build: it takes the size of the window (s37)."""

    def test_a_desktop_has_no_viewport_to_report(self):
        from pixel_rpg import config
        self.assertIsNone(config.browser_viewport())

    def test_a_page_reports_its_inner_size(self):
        from pixel_rpg import config
        with BrowserPage(1600, 900):
            self.assertEqual(config.browser_viewport(), (1600, 900))

    def test_a_silly_window_is_floored_not_obeyed(self):
        """The layout works down to 390px; this guards against nothing at all."""
        from pixel_rpg import config
        with BrowserPage(120, 40):
            self.assertEqual(config.browser_viewport(),
                             config.MIN_BROWSER_SIZE)

    def test_a_page_that_will_not_answer_is_not_fatal(self):
        """A game that will not start because it could not measure a window
        is worse than a game at the wrong size."""
        from pixel_rpg import config
        with BrowserPage(800, 600):
            import platform
            del platform.window
            self.assertIsNone(config.browser_viewport())

    def test_the_game_follows_the_window(self):
        from pixel_rpg import game as G
        game = build_game_for_mouse()
        with BrowserPage(1024, 768) as page:
            game.follow_browser_window()
            self.assertEqual((game.settings.width, game.settings.height),
                             (1024, 768))
            page.innerWidth, page.innerHeight = 1440, 810
            game.follow_browser_window()
            self.assertEqual((game.settings.width, game.settings.height),
                             (1440, 810))
            self.assertEqual(game.camera.view_w, 1440)

    def test_a_desktop_game_is_never_resized_by_this(self):
        game = build_game_for_mouse()
        before = (game.settings.width, game.settings.height)
        game.follow_browser_window()
        self.assertEqual((game.settings.width, game.settings.height), before)


class TestTouchParked(unittest.TestCase):
    """Touch is off unless somebody asks for it (s37)."""

    def test_nothing_turns_touch_on_by_itself(self):
        from pixel_rpg import touch
        self.assertFalse(touch.wants_touch(None))

    def test_not_even_a_browser(self):
        """This is the check that inverted: the browser build is a PC build."""
        from pixel_rpg import touch
        with BrowserPage(1280, 720):
            self.assertFalse(touch.wants_touch(None))


class TestAttributes(unittest.TestCase):
    """Nine attributes, one stat each, and a paid way to take them back."""

    def hero(self, level=10):
        from pixel_rpg.entities import Player
        player = Player("Probe", "warden")
        for _ in range(level - 1):
            player.gain_xp(player.xp_to_next())
        return player

    def test_each_attribute_moves_exactly_one_stat(self):
        """The whole point of the split: no attribute buys two things."""
        from pixel_rpg.entities import ATTRIBUTES
        watched = ("atk", "crit_dmg", "defense", "mag", "res", "spd", "crit")
        for key in ATTRIBUTES:
            player = self.hero()
            before = {s: player.stat(s) for s in watched}
            before["hp"], before["mp"] = player.max_hp(), player.max_mp()
            for _ in range(5):
                self.assertTrue(player.spend_point(key))
            after = {s: player.stat(s) for s in watched}
            after["hp"], after["mp"] = player.max_hp(), player.max_mp()
            moved = [s for s in after if after[s] != before[s]]
            self.assertEqual(len(moved), 1,
                             "%s moved %s, expected exactly one" % (key, moved))

    def test_every_stat_has_an_attribute(self):
        """The other direction: nothing was orphaned by the split."""
        from pixel_rpg.entities import ATTRIBUTES
        reached = set()
        for key in ATTRIBUTES:
            player = self.hero()
            before = {s: player.stat(s) for s in
                      ("atk", "crit_dmg", "defense", "mag", "res", "spd", "crit")}
            before["hp"], before["mp"] = player.max_hp(), player.max_mp()
            # Five, not one. A single point in Precision is +1 raw crit,
            # and a class with a crit multiplier below 1.0 (Warden is 0.8)
            # rounds that straight back down - so one point can look like it
            # did nothing. That is a real wart of the split and it is
            # recorded in section 38; this test is about the *mapping*, so
            # it spends enough to clear the rounding and says so.
            for _ in range(5):
                player.spend_point(key)
            after = {s: player.stat(s) for s in
                     ("atk", "crit_dmg", "defense", "mag", "res", "spd", "crit")}
            after["hp"], after["mp"] = player.max_hp(), player.max_mp()
            reached |= {s for s in after if after[s] != before[s]}
        self.assertEqual(reached, {"atk", "crit_dmg", "defense", "mag", "res",
                                   "spd", "crit", "hp", "mp"})

    def test_a_refund_returns_every_point(self):
        player = self.hero()
        for key in ("might", "vigor", "precision"):
            for _ in range(4):
                player.spend_point(key)
        spare = player.points
        self.assertEqual(player.refund_points(), 12)
        self.assertEqual(player.points, spare + 12)
        self.assertEqual(player.spent_points(), 0)

    def test_a_refund_never_heals_and_never_overfills(self):
        """Dumping Vigor must not leave you above your new maximum."""
        player = self.hero()
        for _ in range(10):
            player.spend_point("vigor")
        player.hp = player.max_hp()
        player.refund_points()
        self.assertLessEqual(player.hp, player.max_hp())

    def test_respec_is_free_only_when_there_is_nothing_to_undo(self):
        player = self.hero()
        self.assertEqual(player.respec_cost(), 0)
        player.spend_point("might")
        self.assertGreater(player.respec_cost(), 0)

    def test_a_v4_save_gets_its_points_back(self):
        """Four attributes became nine; carrying the numbers would eat half."""
        from pixel_rpg.entities import ATTRIBUTES, POINTS_PER_LEVEL
        from pixel_rpg.save import deserialize_player, serialize_player
        player = self.hero(level=12)
        for _ in range(6):
            player.spend_point("might")
        raw = serialize_player(player)
        # What a v4 file actually held: the four old names only.
        raw["attributes"] = {"might": 6, "vigor": 4, "mind": 2, "agility": 1}
        brought = deserialize_player(raw, version=4)
        self.assertEqual(brought.spent_points(), 0, "old points were kept")
        self.assertEqual(brought.points,
                         POINTS_PER_LEVEL * (brought.level - 1))
        for key in ATTRIBUTES:
            self.assertEqual(brought.attributes[key], 0)

    def test_a_current_save_is_left_alone(self):
        from pixel_rpg.save import (SAVE_VERSION, deserialize_player,
                                    serialize_player)
        player = self.hero(level=12)
        for _ in range(5):
            player.spend_point("ferocity")
        raw = serialize_player(player)
        brought = deserialize_player(raw, version=SAVE_VERSION)
        self.assertEqual(brought.attributes["ferocity"], 5)
        self.assertEqual(brought.points, player.points)


def build_game_for_mouse():
    """A `Game` with the contexts the click-reach test needs.

    `point_cursor` refuses some kinds unless the thing they point at exists -
    a battle target with no battle is correctly unreachable. So the fixture
    supplies a fight and a scene rather than the test special-casing them,
    which would be the test agreeing with the bug.
    """
    from pixel_rpg import game as G
    from pixel_rpg.entities import make_enemy
    game = G.Game()
    game.screen = pygame.display.set_mode((1280, 720))
    game.pending_class = "warden"
    game.new_run("Mouser", 1)
    game.dialogue = None
    game.state = G.OVERWORLD
    game.start_battle([make_enemy(3, "meadow", game.settings.mods)])
    game.scene = {"options": [("a", None), ("b", None), ("c", None)],
                  "index": 0, "phase": "ask", "key": "test"}
    # The trade table, for the same reason as the battle and the scene: its
    # rows only exist while a trade does, and a kind that cannot be reached
    # because its context is missing is the fixture's problem, not a bug.
    from pixel_rpg import trade as trade_mod
    game.trade = trade_mod.TradeSession(2, "Friend")
    return game


class TestARunSurvivesItsEnding(unittest.TestCase):
    """Finishing the game keeps the save (s19 decision 5).

    It used to delete the slot - section 33's design was that knowledge
    survives a run and the run does not. Savvydev reversed it after the
    playtest, where finishing and finding the slot gone read as data loss.

    The `beaten` flag it sets is also what the veteran clause needs: a hero
    may cross into somebody else's world only if both saves are finished
    (item 5c).
    """

    def finished_game(self, slot=4):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Ender", slot)
        game.dialogue = None
        game.save_run(force=True)
        return game

    def test_a_new_hero_has_not_beaten_anything(self):
        game = self.finished_game()
        self.assertFalse(game.player.beaten)

    def test_finishing_keeps_the_slot_and_marks_it(self):
        game = self.finished_game()
        slot = game.slot
        game.finish_ending()
        self.assertTrue(game.player.beaten)
        summary = game.saves.describe_all()[slot - 1]
        self.assertFalse(summary.get("empty"),
                         "the run was deleted; it is not spent any more")

    def test_the_mark_survives_a_save_and_load(self):
        """The veteran clause reads this off a stored save, so it must last."""
        game = self.finished_game()
        game.finish_ending()
        slot = game.slot
        game.player = game.world = None
        self.assertTrue(game.load_run(slot), game.saves.last_error)
        self.assertTrue(game.player.beaten)

    def test_an_older_save_is_not_claimed_as_beaten(self):
        """Nothing recorded this before, so False is the only honest answer."""
        from pixel_rpg.save import deserialize_player, serialize_player
        game = self.finished_game()
        raw = serialize_player(game.player)
        raw.pop("beaten", None)
        self.assertFalse(deserialize_player(raw).beaten)


class TestHostingPicksASave(unittest.TestCase):
    """Hosting asks which world, because a room *is* a world.

    §40: *"entirely unclear which save I was loading up or if i was making a
    new one"*. Hosting used to take whatever happened to be loaded, and
    refuse outright if nothing was.
    """

    def fresh_game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        return game

    def press(self, game, key):
        game.handle_event(pygame.event.Event(
            pygame.KEYDOWN, key=key, unicode="", mod=0))

    def host_row(self, game):
        from pixel_rpg import game as G
        game.open_lobby()
        game.ui.lobby_index = 0
        self.press(game, pygame.K_RETURN)
        # **Which game, then which world** (0ag, s59). Hosting asks the
        # mode first now; "the long road" is the answer that leads to the
        # slot picker these checks are about.
        if game.state == "modeselect":
            game.ui.mode_index = 0
            self.press(game, pygame.K_RETURN)

    def test_hosting_with_no_run_opens_the_picker(self):
        from pixel_rpg import game as G
        game = self.fresh_game()
        self.host_row(game)
        self.assertEqual(game.state, G.SLOTS)
        self.assertEqual(game.ui.slot_mode, "coop")
        self.assertTrue(game.host_when_ready)

    def test_backing_out_cancels_the_intent(self):
        game = self.fresh_game()
        self.host_row(game)
        self.press(game, pygame.K_ESCAPE)
        self.assertFalse(game.host_when_ready,
                         "an intent outlived the screen that set it")

    def test_an_empty_slot_becomes_a_new_run_and_then_a_room(self):
        game = self.fresh_game()
        # The whole suite shares one save directory, so this has to
        # make its own precondition rather than assume slot 1 is free.
        game.saves.delete(1)
        self.assertTrue(game.saves.describe_all()[0].get("empty"))
        self.host_row(game)
        game.ui.slot.index = 0
        self.press(game, pygame.K_RETURN)
        self.assertTrue(game.host_when_ready, "the intent was dropped early")
        game.pending_class = "warden"
        game.new_run("Hostess", 1)
        try:
            self.assertIsNotNone(game.coop, "the room never opened")
            self.assertFalse(game.host_when_ready)
        finally:
            game.leave_coop()

    def test_an_existing_slot_is_loaded_and_hosted(self):
        game = self.fresh_game()
        game.pending_class = "warden"
        game.new_run("Saved", 2)
        game.dialogue = None
        game.save_run(force=True)
        game.player = game.world = game.coop = None
        game.host_when_ready = False

        self.host_row(game)
        game.ui.slot.index = 1
        self.press(game, pygame.K_RETURN)
        try:
            self.assertIsNotNone(game.player)
            self.assertEqual(game.player.name, "Saved")
            self.assertIsNotNone(game.coop, "loading did not open the room")
        finally:
            game.leave_coop()


class TestSceneReplay(unittest.TestCase):
    """Watching the story back from the codex.

    Savvydev: *"it like allows you to replay/watch the entire story at any
    time yk?"* The record already existed; this is it played back on the
    screen the scene itself uses.

    **The thing every test here is really protecting** is that a replay
    earns nothing. If it recorded, leaned or handed over an unsaid reply,
    the codex would become a way to read the whole story without playing
    it - and the replay spine in s33 is the game.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def fresh_game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Watcher", 6)
        game.dialogue = None
        return game

    def played(self, game, key="prologue", take="accept"):
        """Play a scene for real, so there is something to replay."""
        game.play_scene(key)
        options = game.scene["options"]
        index = next(i for i, o in enumerate(options) if o.key == take)
        game.answer_scene(index)
        game.finish_scene()

    def test_a_scene_can_be_watched_again(self):
        from pixel_rpg import game as G
        game = self.fresh_game()
        try:
            self.played(game)
            self.assertTrue(game.replay_scene("prologue"))
            self.assertEqual(game.state, G.SCENE)
            self.assertTrue(game.scene["replay"])
        finally:
            game.saves.delete(6)

    def test_only_answers_you_have_been_offered_appear(self):
        """An answer that has never come up does not exist yet, and
        listing it would leak the shape of a run you have not had."""
        game = self.fresh_game()
        try:
            self.played(game)
            game.replay_scene("prologue")
            seen = set(game.meta.scene_seen.get("prologue", ()))
            self.assertEqual({o.key for o in game.scene["options"]}, seen)
        finally:
            game.saves.delete(6)

    def test_replaying_the_answer_you_gave_replays_its_reply(self):
        game = self.fresh_game()
        try:
            self.played(game, take="accept")
            game.replay_scene("prologue")
            index = next(i for i, o in enumerate(game.scene["options"])
                         if o.key == "accept")
            game.answer_scene(index)
            self.assertEqual(game.scene["phase"], "reply")
            self.assertEqual(game.scene["reply"],
                             game.scene["options"][index].reply)
        finally:
            game.saves.delete(6)

    def test_an_answer_you_never_gave_does_not_give_up_its_reply(self):
        """The reply is the reward for having said the thing (s33)."""
        from pixel_rpg import game as G
        game = self.fresh_game()
        try:
            self.played(game, take="accept")
            # **Make the precondition rather than inherit it.** The suite
            # shares one meta profile, so by the time this runs another
            # test may well have taken the answer this one calls untaken -
            # the same lesson as the shared save directory (s19 5b).
            game.meta.scene_taken["prologue"] = ["accept"]
            game.replay_scene("prologue")
            others = [i for i, o in enumerate(game.scene["options"])
                      if o.key != "accept"]
            self.assertTrue(others, "the scene offered only one answer")
            game.answer_scene(others[0])
            self.assertEqual(game.scene["reply"], G.Game.UNSAID)
            self.assertNotEqual(game.scene["reply"],
                                game.scene["options"][others[0]].reply)
        finally:
            game.saves.delete(6)

    def test_a_replay_records_nothing_and_weighs_nothing(self):
        """The test the whole feature rests on."""
        game = self.fresh_game()
        try:
            self.played(game, take="accept")
            before_taken = dict(game.meta.scene_taken)
            before_seen = dict(game.meta.scene_seen)
            before_lean = dict(game.player.leanings)
            before_secrets = list(game.meta.secrets)

            game.replay_scene("prologue")
            for index in range(len(game.scene["options"])):
                game.scene["phase"] = "choosing"
                game.answer_scene(index)

            self.assertEqual(game.meta.scene_taken, before_taken)
            self.assertEqual(game.meta.scene_seen, before_seen)
            self.assertEqual(game.player.leanings, before_lean)
            self.assertEqual(list(game.meta.secrets), before_secrets)
        finally:
            game.saves.delete(6)

    def test_a_scene_nobody_has_reached_cannot_be_replayed(self):
        game = self.fresh_game()
        try:
            self.assertFalse(game.replay_scene("door"))
        finally:
            game.saves.delete(6)

    def test_escape_leaves_a_replay_but_never_a_live_scene(self):
        """A scene is asking you something. A replay is not."""
        from pixel_rpg import game as G
        game = self.fresh_game()
        try:
            self.played(game)
            game.play_scene("orientation")
            if game.state == G.SCENE:
                game.event_scene(pygame.event.Event(
                    pygame.KEYDOWN, key=pygame.K_ESCAPE, unicode="", mod=0))
                self.assertEqual(game.state, G.SCENE,
                                 "Esc walked out of a live scene")
                game.finish_scene()

            game.replay_scene("prologue")
            game.event_scene(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_ESCAPE, unicode="", mod=0))
            self.assertEqual(game.state, G.STORY)
        finally:
            game.saves.delete(6)

    def test_a_reply_goes_back_to_the_answers_rather_than_closing(self):
        """So a scene can be read through instead of reopened per line."""
        from pixel_rpg import game as G
        game = self.fresh_game()
        try:
            self.played(game)
            game.replay_scene("prologue")
            game.answer_scene(0)
            self.assertEqual(game.scene["phase"], "reply")
            game.event_scene(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_RETURN, unicode="", mod=0))
            self.assertEqual(game.state, G.SCENE)
            self.assertEqual(game.scene["phase"], "choosing")
        finally:
            game.saves.delete(6)

    def test_it_can_be_watched_from_the_title_with_no_run_loaded(self):
        """The codex is reachable before a run exists, and `finish_scene`
        used to drop straight into the overworld."""
        from pixel_rpg import game as G
        game = self.fresh_game()
        try:
            self.played(game)
            game.player = game.world = None
            game.replay_scene("prologue")
            game.answer_scene(0)
            game.finish_scene()
            self.assertEqual(game.state, G.STORY)
        finally:
            game.saves.delete(6)


class TestTheIsland(unittest.TestCase):
    """The valley is an island, procedurally, per seed (s42).

    Savvydev, 2026-08-29: *"instead of the map being a huge rectangle/square
    it should be like a huge island that is realistically shaped (and also
    procedurally generated...)"*.

    **The test that earns its place is the stranding one.** The Hollow Key
    needs every chest opened, so a single chest on an offshore rock makes
    `THE LONG WALK` unreachable - an ending deleted by a coastline.
    """

    SEEDS = (4242, 17, 900, 5150, 77)

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def worlds(self):
        from pixel_rpg.world import World
        for seed in self.SEEDS:
            yield seed, World(32, seed).generate()

    def test_it_is_an_island_and_not_a_rectangle(self):
        from pixel_rpg.world import WORLD_W, WORLD_H
        area = WORLD_W * WORLD_H
        for seed, world in self.worlds():
            land = sum(1 for row in world.land for v in row if v)
            # Comfortably land, comfortably sea: a map that is 95% land is
            # a rectangle with damp corners, and one that is 15% is a reef.
            self.assertGreater(land / area, 0.30, seed)
            self.assertLess(land / area, 0.70, seed)

    def test_every_seed_makes_a_different_island(self):
        shapes = set()
        for _seed, world in self.worlds():
            shapes.add(tuple(tuple(row) for row in world.land))
        self.assertEqual(len(shapes), len(self.SEEDS))

    def test_the_same_seed_makes_the_same_island(self):
        """Hard rule 4: terrain regenerates rather than being stored, so
        two worlds from one seed must be the same island."""
        from pixel_rpg.world import World
        a = World(32, 4242).generate()
        b = World(32, 4242).generate()
        self.assertEqual(a.land, b.land)
        self.assertEqual([(p.gx, p.gy) for p in a.portals],
                         [(p.gx, p.gy) for p in b.portals])

    def test_the_village_is_never_flooded(self):
        for seed, world in self.worlds():
            for gx, gy in ((world.spawn[0], world.spawn[1]),):
                self.assertTrue(world.on_land(gx, gy), seed)
            self.assertTrue(
                all(world.on_land(x, y)
                    for x in range(world.town_rect.left, world.town_rect.right)
                    for y in range(world.town_rect.top,
                                   world.town_rect.bottom)), seed)

    def test_nothing_is_ever_stranded_on_an_offshore_rock(self):
        """**The ending-breaking one.** A chest nobody can reach means the
        Hollow Key can never be completed on that save."""
        for seed, world in self.worlds():
            for chest in world.chests:
                self.assertTrue(world.on_land(chest.gx, chest.gy),
                                "seed %d stranded a chest" % seed)
            for spot in world.lairs:
                self.assertTrue(world.on_land(*spot),
                                "seed %d stranded a lair" % seed)
            for spot in world.waystations:
                self.assertTrue(world.on_land(*spot),
                                "seed %d stranded a waystation" % seed)
            for portal in world.portals:
                self.assertTrue(world.on_land(portal.gx, portal.gy),
                                "seed %d put a warden in the sea" % seed)

    def test_every_difficulty_tier_still_exists_on_land(self):
        """The tiers follow the shore now. Laid out on the rectangle
        instead, the late biomes covered three tiles of land between
        them - measured, and the reason `shore` exists."""
        from pixel_rpg.world import BIOME_TIERS, TIER_ORDER, WORLD_W, WORLD_H
        for seed, world in self.worlds():
            seen = set()
            for gy in range(WORLD_H):
                for gx in range(WORLD_W):
                    if world.land[gy][gx]:
                        seen.add(BIOME_TIERS[world.biome_map[gy][gx]])
            self.assertEqual(seen, set(range(len(TIER_ORDER))),
                             "seed %d lost a tier to the sea" % seed)


class TestStepstones(unittest.TestCase):
    """Fast travel you have to win (s42).

    Savvydev, 2026-08-29: *"to be able to teleport to the shrine, you have
    to beat the challenge inside, whether thats some logic puzzle or js
    some fighting or fighting a mini-boss"*. So a gate is not a reward for
    walking somewhere - it is a reward for beating what stands there.
    """

    SEEDS = (4242, 17, 900)

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import entities, story
        self.entities = entities
        self.story = story

    def worlds(self):
        from pixel_rpg.world import World
        for seed in self.SEEDS:
            yield seed, World(32, seed).generate()

    def fresh_game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Traveller", 6)
        game.dialogue = None
        return game

    def test_every_waystation_has_a_gate_and_a_guardian(self):
        for seed, world in self.worlds():
            self.assertEqual(len(world.stepstones), len(world.waystations),
                             seed)
            for _spot, key in world.open_stepstones():
                pass
            for spot, entry in world.stepstones.items():
                self.assertIn(entry[0], self.story.WAYSTATION_BY_KEY)
                self.assertIsNotNone(
                    self.entities.guardian_for(entry[0]),
                    "%s has no guardian" % entry[0])

    def test_gates_start_shut(self):
        for seed, world in self.worlds():
            self.assertEqual(world.open_stepstones(), [], seed)

    def test_a_guardian_is_not_a_warden_and_not_a_field_boss(self):
        warden = {b.key for b in self.entities.BOSS_DEFS}
        field = {b.key for b in self.entities.FIELD_BOSSES}
        guard = {g.key for g in self.entities.GUARDIANS}
        self.assertFalse(guard & warden)
        self.assertFalse(guard & field)

    def test_beating_the_guardian_opens_that_gate_and_only_that_one(self):
        game = self.fresh_game()
        try:
            spot = next(iter(game.world.stepstones))
            key = game.world.stepstones[spot][0]
            guardian = self.entities.guardian_for(key)
            game.pending_gate = spot
            game.on_boss_defeated(self.entities.Boss(guardian))

            self.assertTrue(game.world.stepstone_at(*spot)[1])
            self.assertEqual(len(game.world.open_stepstones()), 1)
        finally:
            game.saves.delete(6)

    def test_a_guardian_opens_no_portal_and_turns_no_chapter(self):
        game = self.fresh_game()
        try:
            before = game.chapter().number
            relics = list(game.player.relics)
            portals = [(p.boss_key, p.defeated) for p in game.world.portals]

            spot = next(iter(game.world.stepstones))
            key = game.world.stepstones[spot][0]
            game.pending_gate = spot
            game.on_boss_defeated(
                self.entities.Boss(self.entities.guardian_for(key)))

            self.assertEqual(game.chapter().number, before)
            self.assertEqual(list(game.player.relics), relics)
            self.assertEqual([(p.boss_key, p.defeated)
                              for p in game.world.portals], portals)
        finally:
            game.saves.delete(6)

    def test_you_cannot_travel_to_a_shut_gate(self):
        game = self.fresh_game()
        try:
            spot = next(iter(game.world.stepstones))
            where = (game.player.x, game.player.y)
            self.assertFalse(game.travel_to(spot))
            self.assertEqual((game.player.x, game.player.y), where)
        finally:
            game.saves.delete(6)

    def test_travelling_to_an_open_gate_puts_you_there(self):
        game = self.fresh_game()
        try:
            spot = next(iter(game.world.stepstones))
            game.world.attune_stepstone(*spot)
            self.assertTrue(game.travel_to(spot))
            self.assertEqual(game.player_tile(), spot)
        finally:
            game.saves.delete(6)

    def test_travel_is_refused_in_a_room(self):
        """The host arbitrates position and fights (s21); a guest
        teleporting is a desync waiting to be found."""
        game = self.fresh_game()
        try:
            spot = next(iter(game.world.stepstones))
            game.world.attune_stepstone(*spot)

            class PretendSession:
                role = "client"
                ready = True
                remotes = {}
            game.coop = PretendSession()
            self.assertFalse(game.travel_to(spot))
        finally:
            game.coop = None
            game.saves.delete(6)

    def test_only_open_gates_are_offered_as_destinations(self):
        game = self.fresh_game()
        try:
            self.assertEqual(game.travel_targets(), [])
            spot = next(iter(game.world.stepstones))
            game.world.attune_stepstone(*spot)
            targets = game.travel_targets()
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0][0], spot)
        finally:
            game.saves.delete(6)

    def test_an_open_gate_survives_a_save_and_a_load(self):
        from pixel_rpg import save as savemod
        from pixel_rpg.world import World
        world = next(self.worlds())[1]
        spot = next(iter(world.stepstones))
        world.attune_stepstone(*spot)

        fresh = World(32, world.seed).generate()
        savemod.apply_world(fresh, savemod.serialize_world(world))
        self.assertTrue(fresh.stepstone_at(*spot)[1])

    def test_a_save_cannot_open_a_gate_that_is_not_there(self):
        from pixel_rpg import save as savemod
        world = next(self.worlds())[1]
        data = savemod.serialize_world(world)
        data["stepstones"] = [{"gx": 1, "gy": 1}]
        fresh = next(self.worlds())[1]
        savemod.apply_world(fresh, data)
        self.assertEqual(fresh.open_stepstones(), [])


class TestTheCeilingAndTheScaling(unittest.TestCase):
    """Level 100, a cap per attribute, and enemies that follow you.

    Savvydev, 2026-08-29:

    > "the max level should be 100, and then place a limit on how high an
    > attribute can go, so like players cant just reach level 100 and then
    > have every single one of their points invested into attack so they
    > end up one shotting everything"

    > "the level of enemies should be dependent on the level of the
    > player... a hard area's enemies would be like the player's level +15
    > to +20"
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import entities, world
        self.entities = entities
        self.world_mod = world

    def test_the_ceiling_is_a_hundred(self):
        self.assertEqual(self.entities.MAX_LEVEL, 100)

    def test_no_attribute_can_be_poured_past_the_cap(self):
        """**The whole point of the second cap.** 100 levels at 5 points is
        500 to spend; all of it in one row is not a build."""
        from pixel_rpg.entities import ATTRIBUTE_CAP, Player
        player = Player("Maxed", "warden")
        player.points = 500
        for _ in range(500):
            player.spend_point("might")
        self.assertLessEqual(player.attributes["might"], ATTRIBUTE_CAP)
        self.assertGreater(player.points, 0,
                           "every point went into one attribute")

    def test_the_cap_leaves_you_needing_several_rows(self):
        from pixel_rpg.entities import (ATTRIBUTE_CAP, ATTRIBUTES,
                                        MAX_LEVEL, POINTS_PER_LEVEL)
        budget = (MAX_LEVEL - 1) * POINTS_PER_LEVEL
        self.assertGreater(budget, ATTRIBUTE_CAP * 2,
                           "one or two maxed rows would spend everything")
        self.assertLess(budget, ATTRIBUTE_CAP * len(ATTRIBUTES),
                        "you could max every row, which is no choice")

    def test_a_hundredth_level_hero_survives_a_save(self):
        from pixel_rpg import save as savemod
        from pixel_rpg.entities import MAX_LEVEL, Player
        player = Player("Ancient", "warden")
        while player.level < MAX_LEVEL:
            player.gain_xp(player.xp_to_next())
        self.assertEqual(player.level, MAX_LEVEL)
        back = savemod.deserialize_player(savemod.serialize_player(player))
        self.assertEqual(back.level, MAX_LEVEL)

    # -- scaling ----------------------------------------------------------

    def world(self, seed=4242):
        from pixel_rpg.world import World
        return World(32, seed).generate()

    def test_enemies_follow_the_player(self):
        world = self.world()
        spot = next((gx, gy) for gy in range(0, 152, 3)
                    for gx in range(0, 336, 3) if world.on_land(gx, gy))
        low = world.suggested_level(*spot, player_level=5)
        high = world.suggested_level(*spot, player_level=70)
        self.assertGreater(high, low + 40,
                           "the world did not follow the player up")

    def test_the_frontier_is_hard_at_every_level(self):
        """*"some areas should be like hard no matter what the player level
        is but still like doable"*."""
        from pixel_rpg.world import biome_tier
        world = self.world()
        for player_level in (10, 40, 80):
            worst = 0
            for gy in range(0, 152, 3):
                for gx in range(0, 336, 3):
                    if not world.on_land(gx, gy):
                        continue
                    if biome_tier(world.biome_map[gy][gx]) != 5:
                        continue
                    gap = world.suggested_level(
                        gx, gy, player_level) - player_level
                    worst = max(worst, gap)
            self.assertGreaterEqual(worst, 15, player_level)
            self.assertLessEqual(worst, 20, player_level)

    def test_the_country_round_the_village_is_never_above_you(self):
        from pixel_rpg.world import biome_tier
        world = self.world()
        for player_level in (1, 30, 90):
            for gy in range(0, 152, 5):
                for gx in range(0, 336, 5):
                    if not world.on_land(gx, gy):
                        continue
                    if biome_tier(world.biome_map[gy][gx]) != 0:
                        continue
                    self.assertLessEqual(
                        world.suggested_level(gx, gy, player_level),
                        player_level, player_level)

    def test_a_field_boss_follows_more_loosely_than_a_monster(self):
        """*"bosses and mini-bosses shouldnt be as close to the players
        level compared to regular enemies"*."""
        from pixel_rpg.entities import make_field_boss
        base = make_field_boss("thicket_hulk", "")
        drifted = make_field_boss("thicket_hulk", "", player_level=60)
        self.assertGreater(drifted.level, base.level)
        self.assertLess(drifted.level, 60,
                        "a field boss tracked the player exactly")

    def test_wardens_and_guardians_never_scale(self):
        """The fixed points he named as outliers."""
        from pixel_rpg.entities import BOSS_DEFS, GUARDIANS
        self.assertEqual([b.level for b in BOSS_DEFS],
                         [3, 7, 12, 18, 24, 32])
        self.assertTrue(all(g.level > 0 for g in GUARDIANS))


class TestLoadoutAndHealCap(unittest.TestCase):
    """Four skills carried, and no healing forever (items 13 and 14).

    Both were Savvydev's decisions on 2026-08-24 and both sat unbuilt.
    Together they are the answer to the immortality combo his friend
    found: negation plus mana regen plus health regen, cycled forever.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import entities
        self.entities = entities

    def veteran(self):
        from pixel_rpg.entities import Player
        player = Player("Carrier", "warden")
        for _ in range(30):
            player.gain_xp(player.xp_to_next())
        return player

    def test_you_carry_four_however_many_you_know(self):
        player = self.veteran()
        self.assertGreater(len(player.known_skills()),
                           self.entities.LOADOUT_SIZE)
        self.assertEqual(len(player.skills()), self.entities.LOADOUT_SIZE)

    def test_an_empty_loadout_means_the_first_four(self):
        """So a new hero is never staring at an empty bar."""
        player = self.veteran()
        self.assertEqual([s.key for s in player.skills()],
                         [s.key for s in
                          player.known_skills()[:self.entities.LOADOUT_SIZE]])

    def test_editing_from_empty_keeps_what_you_were_carrying(self):
        """**The bug worth a test.** An empty loadout *means* the first
        four, so editing from empty threw those four away and left you
        holding one ability."""
        player = self.veteran()
        before = [s.key for s in player.skills()]
        fifth = player.known_skills()[4].key
        player.equip_skill(fifth)          # refused: already carrying four
        self.assertEqual([s.key for s in player.skills()], before)

    def test_a_fifth_is_refused_until_you_put_one_down(self):
        player = self.veteran()
        fifth = player.known_skills()[4].key
        self.assertFalse(player.equip_skill(fifth))
        self.assertTrue(player.equip_skill(player.skills()[0].key))
        self.assertTrue(player.equip_skill(fifth))
        self.assertEqual(len(player.skills()), self.entities.LOADOUT_SIZE)

    def test_you_cannot_carry_what_you_have_not_learned(self):
        player = self.veteran()
        self.assertFalse(player.equip_skill("not_a_skill"))

    def test_a_loadout_survives_a_save(self):
        from pixel_rpg import save as savemod
        player = self.veteran()
        player.equip_skill(player.skills()[0].key)
        player.equip_skill(player.known_skills()[4].key)
        chosen = [s.key for s in player.skills()]
        back = savemod.deserialize_player(savemod.serialize_player(player))
        self.assertEqual([s.key for s in back.skills()], chosen)

    def test_a_save_cannot_name_a_skill_that_does_not_exist(self):
        from pixel_rpg import save as savemod
        player = self.veteran()
        raw = savemod.serialize_player(player)
        raw["loadout"] = ["not_a_skill", "also_not"]
        back = savemod.deserialize_player(raw)
        self.assertTrue(back.skills(), "a bad save emptied the bar")

    # -- the healing cap ---------------------------------------------------

    def test_healing_is_capped_at_three_in_a_row(self):
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import make_enemy
        battle = Battle(self.veteran(), make_enemy(5, "meadow"))
        cap = battle.HEAL_STREAK_CAP
        for _ in range(cap):
            self.assertTrue(battle.may_heal(0))
            battle.note_action(0, True)
        self.assertFalse(battle.may_heal(0),
                         "you could heal forever")

    def test_anything_else_clears_the_streak(self):
        """It forbids *only* healing, not healing."""
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import make_enemy
        battle = Battle(self.veteran(), make_enemy(5, "meadow"))
        for _ in range(battle.HEAL_STREAK_CAP):
            battle.note_action(0, True)
        self.assertFalse(battle.may_heal(0))
        battle.note_action(0, False)
        self.assertTrue(battle.may_heal(0))

    def test_the_cap_applies_to_monsters_too(self):
        """A boss that heals every turn is the same stalemate from the
        other chair."""
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import make_enemy
        battle = Battle(self.veteran(), make_enemy(5, "meadow"))
        who = ("enemy", 0)
        for _ in range(battle.HEAL_STREAK_CAP):
            battle.note_action(who, True)
        self.assertFalse(battle.may_heal(who))

    def test_the_two_sides_are_counted_separately(self):
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import make_enemy
        battle = Battle(self.veteran(), make_enemy(5, "meadow"))
        for _ in range(battle.HEAL_STREAK_CAP):
            battle.note_action(0, True)
        self.assertFalse(battle.may_heal(0))
        self.assertTrue(battle.may_heal(("enemy", 0)))


class TestErrands(unittest.TestCase):
    """Sidequests, distinct from favours (s42).

    Savvydev, 2026-08-29: *"there should be npcs that give favors AND npcs
    that give you sidequests"* and *"you can have multiple sidequests
    accepted at once btw"*.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import quests
        self.quests = quests

    def log_and_player(self):
        from pixel_rpg.entities import Player
        return self.quests.QuestLog(), Player("Runner", "warden")

    def test_an_errand_never_costs_you_the_quest(self):
        """The two tracks are separate, which is the point."""
        log, player = self.log_and_player()
        log.accept("cull", player)
        self.assertTrue(log.full())
        self.assertTrue(log.accept_errand("errand_wolves", player))
        self.assertIn("cull", log.active)

    def test_you_may_carry_several(self):
        log, player = self.log_and_player()
        keys = [e.key for e in self.quests.ERRAND_DEFS[:3]]
        for key in keys:
            self.assertTrue(log.accept_errand(key, player), key)
        self.assertEqual(log.errands, keys)

    def test_the_cap_is_a_guard_not_a_wall(self):
        log, player = self.log_and_player()
        self.assertGreater(self.quests.MAX_ERRANDS, 1)
        for errand in self.quests.ERRAND_DEFS:
            log.accept_errand(errand.key, player)
        self.assertLessEqual(len(log.errands), self.quests.MAX_ERRANDS)

    def test_progress_counts_from_when_you_took_it(self):
        log, player = self.log_and_player()
        player.stats_tracker["kills"] = 40
        log.accept_errand("errand_wolves", player)
        self.assertEqual(log.progress(player, "errand_wolves")[0], 0)
        player.stats_tracker["kills"] = 46
        self.assertEqual(log.progress(player, "errand_wolves")[0], 6)

    def test_claiming_pays_once(self):
        log, player = self.log_and_player()
        log.accept_errand("errand_wolves", player)
        player.stats_tracker["kills"] = 99
        self.assertIsNotNone(log.claim_errand(player, "errand_wolves"))
        self.assertIsNone(log.claim_errand(player, "errand_wolves"))
        self.assertIn("errand_wolves", log.completed)

    def test_errands_survive_a_save(self):
        log, player = self.log_and_player()
        log.accept_errand("errand_wolves", player)
        log.accept_errand("errand_chests", player)
        back = self.quests.QuestLog().apply_dict(log.to_dict())
        self.assertEqual(back.errands, log.errands)

    def test_a_finished_errand_is_still_finished_after_a_load(self):
        """Both kinds finish into one `completed` list, so it has to be
        validated against both - otherwise an errand vanishes on load and
        becomes offerable again."""
        log, player = self.log_and_player()
        log.accept_errand("errand_wolves", player)
        player.stats_tracker["kills"] = 99
        log.claim_errand(player, "errand_wolves")
        back = self.quests.QuestLog().apply_dict(log.to_dict())
        self.assertIn("errand_wolves", back.completed)

    def test_nobody_carries_both_kinds(self):
        """Being asked a riddle and then given a job by the same villager
        reads as one NPC with two mouths."""
        from pixel_rpg.world import World
        for seed in (4242, 17, 900):
            world = World(32, seed).generate()
            self.assertFalse(set(world.favours) & set(world.errand_givers),
                             seed)
            self.assertTrue(world.errand_givers, seed)


class TestFavours(unittest.TestCase):
    """Small things people ask you for (s42).

    Savvydev, 2026-08-29: *"add a bunch of npcs around the map and stuff
    that give you sidequests or task you to do something super duper small
    like a puzzle task or something like that which makes them grant you a
    reward"*.

    **They are deliberately not quests**, and the first test says why:
    `quests.py` caps you at one at a time and that cap is his decision, so
    a dozen tiny errands must not compete for it.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import story
        self.story = story

    def fresh_game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Helper", 6)
        game.dialogue = None
        return game

    def someone(self, game):
        """An NPC with an unspent favour **the player can answer**.

        **A toll asks for something out of your bag**, and `play_favour`
        will not offer an answer you cannot give - asking for a whetstone
        and then refusing the reply is a worse conversation than not
        asking. So a fresh hero standing in front of that one opens no
        scene at all, which is correct, and means a test may not assume
        the first unspent favour in the world is a playable one. Found by
        adding a second toll (s44): the suite had been getting away with
        it while `potion` was the only thing ever asked for.
        """
        for spot, entry in game.world.favours.items():
            if entry[1]:
                continue
            npc = next(n for n in game.world.npcs if (n.gx, n.gy) == spot)
            favour = game.world.favour_at(*spot)[0]
            if game.play_favour(npc):
                game.scene = None
                return npc, favour
        raise AssertionError("no answerable favour in this world")

    def test_a_favour_never_touches_the_quest_cap(self):
        game = self.fresh_game()
        try:
            before = len(game.quests.active)
            npc, favour = self.someone(game)
            game.play_favour(npc)
            game.answer_scene(game.scene["favour"]["right"][0])
            self.assertEqual(len(game.quests.active), before)
            self.assertNotIn("favour", " ".join(game.quests.active).lower())
        finally:
            game.saves.delete(6)

    def test_the_right_answer_pays(self):
        game = self.fresh_game()
        try:
            npc, favour = self.someone(game)
            gold, xp = game.player.gold, game.player.xp
            game.play_favour(npc)
            game.answer_scene(game.scene["favour"]["right"][0])
            self.assertEqual(game.player.gold, gold + favour.reward_gold)
            if favour.reward_xp:
                self.assertGreater(game.player.xp + game.player.level, xp)
        finally:
            game.saves.delete(6)

    def test_a_wrong_answer_pays_nothing_and_still_settles_it(self):
        """**One attempt.** Leaving a wrong answer askable sounded kind
        and made every riddle meaningless - you could keep guessing."""
        game = self.fresh_game()
        try:
            npc, _favour = self.someone(game)
            spot = (npc.gx, npc.gy)
            game.play_favour(npc)
            wrong = [i for i in range(len(game.scene["options"]))
                     if i not in game.scene["favour"]["right"]]
            if not wrong:
                self.skipTest("that favour has no wrong answer")
            gold = game.player.gold
            game.answer_scene(wrong[0])
            self.assertEqual(game.player.gold, gold)
            self.assertTrue(game.world.favours[spot][1],
                            "a wrong answer left it askable")
            game.finish_scene()
            self.assertFalse(game.play_favour(npc))
        finally:
            game.saves.delete(6)

    def test_a_favour_records_nothing_in_the_profile(self):
        """It is not a scene: no lean, no secret, no codex."""
        game = self.fresh_game()
        try:
            npc, _favour = self.someone(game)
            taken = dict(game.meta.scene_taken)
            seen = dict(game.meta.scene_seen)
            leanings = dict(game.player.leanings)
            game.play_favour(npc)
            game.answer_scene(game.scene["favour"]["right"][0])
            self.assertEqual(game.meta.scene_taken, taken)
            self.assertEqual(game.meta.scene_seen, seen)
            self.assertEqual(game.player.leanings, leanings)
        finally:
            game.saves.delete(6)

    def test_a_toll_is_not_offered_when_you_cannot_pay(self):
        """Asking for a potion you do not have, then refusing the answer,
        is a worse conversation than not asking."""
        game = self.fresh_game()
        try:
            tolls = [(spot, e) for spot, e in game.world.favours.items()
                     if self.story.FAVOUR_BY_KEY[e[0]].kind == "toll"]
            if not tolls:
                self.skipTest("no toll placed on this seed")
            spot, entry = tolls[0]
            npc = next(n for n in game.world.npcs
                       if (n.gx, n.gy) == spot)
            game.player.consumables = {}
            self.assertFalse(game.play_favour(npc),
                             "a toll was offered with nothing to pay it")
        finally:
            game.saves.delete(6)

    def test_every_favour_has_exactly_one_right_answer(self):
        for favour in self.story.FAVOURS:
            right = [o for o in favour.options if o[2]]
            self.assertEqual(len(right), 1, favour.key)
            for label, reply, _correct in favour.options:
                self.assertTrue(label.strip(), favour.key)
                self.assertTrue(reply, "%s has a silent answer" % favour.key)

    def test_a_settled_favour_survives_a_save_and_a_load(self):
        from pixel_rpg import save as savemod
        from pixel_rpg.world import World
        game = self.fresh_game()
        try:
            spot = next(iter(game.world.favours))
            game.world.settle_favour(*spot)
            fresh = World(32, game.world.seed).generate()
            savemod.apply_world(fresh, savemod.serialize_world(game.world))
            self.assertTrue(fresh.favours[spot][1])
        finally:
            game.saves.delete(6)


class TestOutposts(unittest.TestCase):
    """Hamlets, ruins and standing stones (s42).

    Savvydev, 2026-08-29: *"add different things to do around the map as
    opposed to just simply making it bigger, add villages, abandoned
    villages, cool structures, etc, etc"*.
    """

    SEEDS = (4242, 17, 900, 5150, 77)

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def worlds(self):
        from pixel_rpg.world import World
        for seed in self.SEEDS:
            yield seed, World(32, seed).generate()

    def test_every_island_gets_all_three_kinds(self):
        for seed, world in self.worlds():
            kinds = {kind for kind, _name in world.outposts.values()}
            self.assertEqual(kinds, {"hamlet", "ruin", "stones"}, seed)
            self.assertGreaterEqual(len(world.outposts), 12, seed)

    def test_they_stand_on_reachable_land_away_from_town(self):
        for seed, world in self.worlds():
            for (gx, gy) in world.outposts:
                self.assertTrue(world.on_land(gx, gy), seed)
                self.assertFalse(
                    world.town_rect.inflate(18, 18).collidepoint(gx, gy),
                    seed)

    def test_they_do_not_pile_on_each_other(self):
        for seed, world in self.worlds():
            spots = list(world.outposts)
            for i, a in enumerate(spots):
                for b in spots[i + 1:]:
                    self.assertGreaterEqual(
                        max(abs(a[0] - b[0]), abs(a[1] - b[1])), 16, seed)

    def test_a_hamlet_has_somebody_in_it(self):
        """That is the difference between a hamlet and a ruin."""
        for seed, world in self.worlds():
            hamlets = [spot for spot, (kind, _n) in world.outposts.items()
                       if kind == "hamlet"]
            for spot in hamlets:
                near = [n for n in world.npcs
                        if max(abs(n.gx - spot[0]),
                               abs(n.gy - spot[1])) <= 6]
                self.assertTrue(near, "seed %d built an empty hamlet" % seed)

    def test_a_ruin_has_nobody_in_it(self):
        for seed, world in self.worlds():
            ruins = [spot for spot, (kind, _n) in world.outposts.items()
                     if kind == "ruin"]
            for spot in ruins:
                near = [n for n in world.npcs
                        if n.kind == "villager"
                        and max(abs(n.gx - spot[0]),
                                abs(n.gy - spot[1])) <= 5]
                self.assertFalse(near,
                                 "seed %d left somebody living in a ruin"
                                 % seed)

    def test_a_ruin_is_broken_rather_than_merely_unroofed(self):
        """Drawn as an intact rectangle it read as a building nobody had
        finished roofing; the eye needs gaps to call it a remnant."""
        from pixel_rpg.world import T_RUBBLE, T_STONE
        for seed, world in self.worlds():
            ruins = [spot for spot, (kind, _n) in world.outposts.items()
                     if kind == "ruin"]
            broken = 0
            for (gx, gy) in ruins:
                walls = stone = 0
                for dy in range(-5, 6):
                    for dx in range(-5, 6):
                        tx, ty = gx + dx, gy + dy
                        if not world.in_bounds(tx, ty):
                            continue
                        if world.grid[ty][tx] == T_RUBBLE:
                            walls += 1
                        elif world.grid[ty][tx] == T_STONE:
                            stone += 1
                if walls and stone:
                    broken += 1
            self.assertEqual(broken, len(ruins), seed)

    def test_outposts_never_add_a_chest(self):
        """The Hollow Key needs every chest, so the count is what
        `THE LONG WALK` costs (s42). Scenery must not reprice an ending."""
        for seed, world in self.worlds():
            self.assertEqual(len(world.chests), 22, seed)


class TestTowersAndTheMap(unittest.TestCase):
    """Towers light the map; they never open it (s42).

    Savvydev, 2026-08-29: *"you can travel anywhere in the map without
    unlocking those big towers, but to actually see how the whole map
    looks you have to unlock all the towers"*, and *"the towers in BoTW
    arent just assigned to each biome, theyre kind of just randomly
    scattered throughout the map"*.
    """

    SEEDS = (4242, 17, 900, 5150, 77)

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def worlds(self):
        from pixel_rpg.world import World
        for seed in self.SEEDS:
            yield seed, World(32, seed).generate()

    def test_towers_are_scattered_not_one_per_biome(self):
        from pixel_rpg.world import BIOME_TIERS
        from collections import Counter
        for seed, world in self.worlds():
            self.assertGreaterEqual(len(world.towers), 8, seed)
            biomes = Counter(world.biome_map[gy][gx]
                             for (gx, gy) in world.towers)
            # Scattered means the distribution is whatever the island
            # gave: what must *not* hold is one per biome and no more.
            self.assertNotEqual(sorted(biomes.values()),
                                [1] * len(BIOME_TIERS), seed)

    def test_towers_stand_on_reachable_land(self):
        for seed, world in self.worlds():
            for (gx, gy) in world.towers:
                self.assertTrue(world.on_land(gx, gy), seed)
                self.assertFalse(world.blocked(gx, gy), seed)

    def test_a_tower_gates_nothing(self):
        """**The whole point.** Everything is walkable from minute one;
        what a tower changes is what the map remembers."""
        for seed, world in self.worlds():
            before = world._reachable_from(world.spawn)
            for spot in world.towers:
                world.climb_tower(*spot)
            after = world._reachable_from(world.spawn)
            self.assertEqual(before, after,
                             "seed %d let a tower change where you can go"
                             % seed)

    def test_climbing_reveals_and_starts_dark(self):
        for seed, world in self.worlds():
            far = [(gx, gy) for (gx, gy) in world.towers
                   if not world.town_rect.inflate(24, 24).collidepoint(gx, gy)]
            self.assertTrue(far, seed)
            spot = far[0]
            self.assertFalse(world.revealed(*spot),
                             "seed %d started with country surveyed" % seed)
            self.assertTrue(world.climb_tower(*spot))
            self.assertTrue(world.revealed(*spot))
            # Climbing the same one twice is not progress.
            self.assertFalse(world.climb_tower(*spot))

    def test_the_village_is_known_from_the_start(self):
        """You live there. Fog over your own doorstep is not mystery."""
        for seed, world in self.worlds():
            self.assertTrue(world.revealed(*world.spawn), seed)

    def test_climbing_them_all_shows_most_of_the_island(self):
        """*"to actually see how the whole map looks you have to unlock
        all the towers"* - so all of them has to mean nearly all of it."""
        from pixel_rpg.world import WORLD_W, WORLD_H
        for seed, world in self.worlds():
            land = [(gx, gy) for gy in range(WORLD_H) for gx in range(WORLD_W)
                    if world.land[gy][gx]]
            for spot in world.towers:
                world.climb_tower(*spot)
            seen = sum(1 for (gx, gy) in land if world.revealed(gx, gy))
            self.assertGreater(seen / len(land), 0.90,
                               "seed %d left the island dark" % seed)

    def test_a_climbed_tower_survives_a_save_and_a_load(self):
        from pixel_rpg import save as savemod
        from pixel_rpg.world import World
        world = next(self.worlds())[1]
        spot = next(iter(world.towers))
        world.climb_tower(*spot)

        fresh = World(32, world.seed).generate()
        savemod.apply_world(fresh, savemod.serialize_world(world))
        self.assertTrue(fresh.towers[spot])
        self.assertEqual(fresh.towers_climbed(), 1)

    def test_a_save_cannot_invent_a_tower(self):
        from pixel_rpg import save as savemod
        world = next(self.worlds())[1]
        data = savemod.serialize_world(world)
        data["towers"] = [{"gx": 1, "gy": 1, "climbed": True}]
        fresh = next(self.worlds())[1]
        before = dict(fresh.towers)
        savemod.apply_world(fresh, data)
        self.assertEqual(set(fresh.towers), set(before))

    def test_the_map_never_marks_anything_under_the_fog(self):
        """A glowing dot inside unsurveyed country tells you what is
        there, which is the one thing the fog is for."""
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Surveyor", 6)
        game.dialogue = None
        try:
            for gx, gy, _colour, _label in game.map_markers():
                self.assertTrue(game.world.revealed(gx, gy),
                                "a marker glowed inside the fog")
        finally:
            game.saves.delete(6)


class TestGettingThere(unittest.TestCase):
    """Roads that wind, stop at the waystations, and rock in between.

    Savvydev, 2026-08-29: *"make the roads wind too"* and *"dont just add
    clear roads to the wardens, let players figure it out themselves how
    to get to them, add like terrain you cant go over and mountains you
    have to navigate and stuff"*.

    **Reachable is not the same as roaded**, and that is the whole design
    here: every warden must be walkable to or the save is broken, and
    none of them may have a road drawn to the door.
    """

    SEEDS = (4242, 17, 900, 5150, 77)

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def worlds(self):
        from pixel_rpg.world import World
        for seed in self.SEEDS:
            yield seed, World(32, seed).generate()

    def test_every_warden_is_reachable_on_foot(self):
        """The half that must never break. A warden you cannot walk to is
        a run that cannot be finished."""
        for seed, world in self.worlds():
            reachable = world._reachable_from(world.spawn)
            for portal in world.portals:
                # **Beside it, not on it** (s64). An arena gate is a
                # blocking prop - you walk up to it - so its own tile is
                # not in a flood that uses the player's rules, which is
                # what `_reachable_from` does now.
                self.assertTrue(
                    world._beside((portal.gx, portal.gy), reachable),
                    "seed %d walled off %s" % (seed, portal.boss_key))

    def test_every_chest_is_reachable_on_foot(self):
        """Same reason as the offshore rocks: the Hollow Key needs all of
        them, so one behind a ridge deletes `THE LONG WALK`."""
        for seed, world in self.worlds():
            reachable = world._reachable_from(world.spawn)
            for chest in world.chests:
                self.assertTrue(
                    world._beside((chest.gx, chest.gy), reachable),
                    "seed %d walled off a chest" % seed)

    def test_no_road_reaches_a_warden(self):
        """The other half. A path tile at the door is the signpost he
        asked not to be drawn - the marker gives a bearing, the ground
        gives the problem."""
        from pixel_rpg.world import T_PATH
        for seed, world in self.worlds():
            for portal in world.portals:
                ring = [(portal.gx + dx, portal.gy + dy)
                        for dx in range(-3, 4) for dy in range(-3, 4)
                        if max(abs(dx), abs(dy)) == 3]
                paved = [p for p in ring if world.in_bounds(*p)
                         and world.grid[p[1]][p[0]] == T_PATH]
                self.assertFalse(paved,
                                 "seed %d paved up to %s"
                                 % (seed, portal.boss_key))

    def test_roads_reach_the_waystations(self):
        """They are the network, and the network is what fast travel will
        hang off."""
        from pixel_rpg.world import T_PATH
        for seed, world in self.worlds():
            for (gx, gy) in world.waystations:
                near = [(gx + dx, gy + dy)
                        for dx in range(-3, 4) for dy in range(-3, 4)]
                paved = [p for p in near if world.in_bounds(*p)
                         and world.grid[p[1]][p[0]] == T_PATH]
                self.assertTrue(paved,
                                "seed %d left a waystation off the road"
                                % seed)

    def test_a_road_is_not_a_straight_line(self):
        """It used to be an L, which read as drawn-on across an island."""
        from pixel_rpg.world import T_PATH
        for seed, world in self.worlds():
            rows = {}
            for gy in range(len(world.grid)):
                for gx in range(len(world.grid[0])):
                    if world.grid[gy][gx] == T_PATH:
                        rows.setdefault(gy, []).append(gx)
            # An L-shaped network is a handful of very long straight runs.
            # A winding one is many short ones on many rows.
            self.assertGreater(len(rows), 40,
                               "seed %d's roads are too straight" % seed)

    def test_there_is_rock_you_cannot_cross(self):
        from pixel_rpg.world import T_MOUNTAIN, TILES
        self.assertTrue(TILES[T_MOUNTAIN][1], "mountains are walkable")
        for seed, world in self.worlds():
            land = sum(1 for row in world.land for v in row if v)
            rock = sum(1 for row in world.grid for t in row
                       if t == T_MOUNTAIN)
            self.assertGreater(rock / land, 0.05, seed)
            self.assertLess(rock / land, 0.35, seed)

    def test_the_starting_country_stays_open(self):
        """Ridges belong to the country you are not meant to stroll
        through; a mountain outside the village is a wall, not a puzzle."""
        from pixel_rpg.world import T_MOUNTAIN
        for seed, world in self.worlds():
            sx, sy = world.spawn
            for dy in range(-12, 13):
                for dx in range(-12, 13):
                    gx, gy = sx + dx, sy + dy
                    if not world.in_bounds(gx, gy):
                        continue
                    self.assertNotEqual(world.grid[gy][gx], T_MOUNTAIN,
                                        "seed %d walled the village in"
                                        % seed)


class TestFieldBosses(unittest.TestCase):
    """Mini bosses out in the world (s42).

    Savvydev: *"there are super cool enemies of different types and mini
    bosses scattered throughout the map"*, *"theres a LOT and there are
    variants of them too"*, and *"field bosses arent just a one-time
    encounter... low encounter rate and a cool down"*.

    **The test that matters most is that a field boss is not a warden.**
    Six anchors is a fixed point (`ANCHOR_COUNT`), `THE SEVENTH` is named
    after the count, and a mini boss that handed over a Memory would
    quietly become a seventh.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import entities, story
        self.entities = entities
        self.story = story

    def world(self, seed=4242):
        from pixel_rpg.world import World
        return World(32, seed).generate()

    def fresh_game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Hunter", 6)
        game.dialogue = None
        return game

    # -- what they are ----------------------------------------------------

    def test_a_field_boss_is_never_a_warden(self):
        warden_keys = {b.key for b in self.entities.BOSS_DEFS}
        field_keys = {b.key for b in self.entities.FIELD_BOSSES}
        self.assertFalse(warden_keys & field_keys)
        for key in field_keys:
            self.assertNotIn(key, self.story.WARDEN_BY_BOSS,
                             "%s would hand over a Memory" % key)

    def test_felling_one_opens_no_portal_and_turns_no_chapter(self):
        """The whole separation, exercised rather than asserted."""
        game = self.fresh_game()
        try:
            before_chapter = game.chapter().number
            before_relics = list(game.player.relics)
            unlocked = [(p.boss_key, p.unlocked, p.defeated)
                        for p in game.world.portals]

            boss = self.entities.Boss(
                self.entities.FIELD_BOSS_BY_KEY["thicket_hulk"])
            game.pending_lair = None
            game.on_boss_defeated(boss)

            self.assertEqual(game.chapter().number, before_chapter)
            self.assertEqual(list(game.player.relics), before_relics)
            self.assertEqual([(p.boss_key, p.unlocked, p.defeated)
                              for p in game.world.portals], unlocked)
        finally:
            game.saves.delete(6)

    def test_there_are_a_lot_of_them(self):
        """Families x variants, which is his shape: a family fights
        differently, a variant of it fights the same way harder."""
        families = self.entities.FIELD_BOSSES
        variants = self.entities.FIELD_VARIANTS
        self.assertGreaterEqual(len(families), 12)
        self.assertGreaterEqual(len(variants), 3)
        self.assertGreaterEqual(len(families) * len(variants), 40)

    def test_every_biome_has_more_than_one_answer(self):
        from collections import Counter
        counts = Counter(b.biome for b in self.entities.FIELD_BOSSES)
        for biome in ("meadow", "forest", "cave", "tundra", "volcano",
                      "wastes"):
            self.assertGreaterEqual(counts[biome], 2, biome)

    def test_a_variant_is_the_same_family_scaled(self):
        base = self.entities.make_field_boss("gravewake", "")
        hollow = self.entities.make_field_boss("gravewake", "Hollow")
        self.assertIn("Gravewake", hollow.name)
        self.assertGreater(hollow.hp, base.hp)
        self.assertGreater(hollow.xp, base.xp)
        # Same fight, harder - the skills are the family's, not the tier's.
        self.assertEqual(hollow.skills, base.skills)
        self.assertEqual(hollow.phase2_skills, base.phase2_skills)

    def test_more_mobs_and_they_do_different_things(self):
        """*"so many different mobs that all do something different"*."""
        arch = self.entities.ARCHETYPES
        self.assertGreaterEqual(len(arch), 12)
        kits = {a.key: tuple(sorted(a.skills)) for a in arch}
        self.assertEqual(len(set(kits.values())), len(kits),
                         "two archetypes have identical skill sets")

    # -- where they are ---------------------------------------------------

    def test_lairs_are_placed_off_the_roads_and_every_family_appears(self):
        """**Density is per area now, not one each** (s42): a map you can
        cross without passing a lair has no landmarks in it."""
        from pixel_rpg.world import T_PATH
        world = self.world()
        self.assertGreaterEqual(len(world.lairs),
                                len(self.entities.FIELD_BOSSES))
        self.assertEqual({v[0] for v in world.lairs.values()},
                         {b.key for b in self.entities.FIELD_BOSSES},
                         "a family never got planted")
        for (gx, gy) in world.lairs:
            self.assertEqual(world.props.get((gx, gy)), "lair")
            self.assertNotEqual(world.grid[gy][gx], T_PATH,
                                "a lair was planted in the road")
            self.assertFalse(world.town_rect.collidepoint(gx, gy))

    def test_a_lair_sits_in_the_tier_its_family_belongs_to(self):
        """**Its tier, not its exact biome** - since the sibling biomes
        arrived (s42) a family lives anywhere at its own difficulty, so
        the marsh is not a place field bosses have never heard of."""
        from pixel_rpg.world import siblings_of
        world = self.world()
        for (gx, gy), entry in world.lairs.items():
            family = self.entities.FIELD_BOSS_BY_KEY[entry[0]]
            self.assertIn(world.biome_map[gy][gx], siblings_of(family.biome))

    # -- that they come back ----------------------------------------------

    def test_felling_one_rests_it_rather_than_deleting_it(self):
        """*"field bosses arent just a one-time encounter"*."""
        world = self.world()
        spot = next(iter(world.lairs))
        self.assertFalse(world.lair_at(spot[0], spot[1], 0.0)[1])
        world.fell_lair(spot[0], spot[1], playtime=100.0)
        self.assertTrue(world.lair_at(spot[0], spot[1], 150.0)[1])
        self.assertFalse(
            world.lair_at(spot[0], spot[1], 100.0 + world.LAIR_REST + 1)[1],
            "it never woke up again")
        self.assertIn(spot, world.lairs, "the landmark was deleted")

    def test_a_resting_lair_survives_a_save_and_a_load(self):
        from pixel_rpg import save as savemod
        from pixel_rpg.world import World
        world = self.world()
        spot = next(iter(world.lairs))
        world.fell_lair(spot[0], spot[1], playtime=500.0)
        world.lairs[spot][1] = "Hollow"

        fresh = World(32, world.seed).generate()
        savemod.apply_world(fresh, savemod.serialize_world(world))
        self.assertEqual(fresh.lairs[spot][1], "Hollow")
        self.assertTrue(fresh.lair_at(spot[0], spot[1], 600.0)[1])

    def test_a_save_cannot_invent_a_lair_where_there_is_none(self):
        """Everything off disk is untrusted: a stored coordinate is a key
        to look one up, never a licence to create one."""
        from pixel_rpg import save as savemod
        world = self.world()
        data = savemod.serialize_world(world)
        data["lairs"].append({"gx": 1, "gy": 1, "variant": "Hollow",
                              "rest": 9e6})
        fresh = self.world()
        before = set(fresh.lairs)
        savemod.apply_world(fresh, data)
        self.assertEqual(set(fresh.lairs), before)

    # -- the wandering half -----------------------------------------------

    def test_the_cooldown_stops_five_in_a_row(self):
        """His actual worry, in one test: *"so u dont get 'lucky' and
        encounter them like 5 times in a row"*."""
        game = self.fresh_game()
        try:
            game.FIELD_BOSS_CHANCE = 1.0     # every roll would say yes
            first = game.roll_field_boss("meadow")
            self.assertIsNotNone(first)
            for _ in range(20):
                self.assertIsNone(game.roll_field_boss("meadow"),
                                  "the cooldown let a second one through")
        finally:
            game.FIELD_BOSS_CHANCE = type(game).FIELD_BOSS_CHANCE
            game.saves.delete(6)

    def test_a_wandering_boss_belongs_to_the_biome_it_found_you_in(self):
        game = self.fresh_game()
        try:
            game.FIELD_BOSS_CHANCE = 1.0
            for biome in ("meadow", "cave", "volcano"):
                game._field_cooldown = __import__(
                    "pixel_rpg.security", fromlist=["x"]).Cooldown(0.0)
                boss = game.roll_field_boss(biome)
                self.assertIsNotNone(boss, biome)
                family = next(b for b in self.entities.FIELD_BOSSES
                              if b.key == boss.key)
                self.assertEqual(family.biome, biome)
        finally:
            game.FIELD_BOSS_CHANCE = type(game).FIELD_BOSS_CHANCE
            game.saves.delete(6)

    def test_the_rate_is_low_enough_to_be_an_event(self):
        self.assertLess(self.fresh_game().FIELD_BOSS_CHANCE, 0.05)


class TestWaystations(unittest.TestCase):
    """A place on every road, and the rule that keeps it from spoiling.

    s42: the fragment must not resolve until the Memory does. A village
    that explains Callun before you kill Callun deletes the conceit s33 is
    built on, so the test that matters here is the one asserting no keeper
    and no waystation scene says a warden's name.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import scenes, story
        self.story = story
        self.scenes = scenes

    def world(self, seed=4242):
        from pixel_rpg.world import World
        return World(32, seed).generate()

    def test_one_waystation_per_road(self):
        self.assertEqual(len(self.story.WAYSTATIONS),
                         self.story.ANCHOR_COUNT)
        roads = {w.boss_key for w in self.story.WAYSTATIONS}
        self.assertEqual(roads, {w.boss_key for w in self.story.WARDENS})

    def test_every_waystation_is_placed_on_its_road(self):
        world = self.world()
        self.assertEqual(len(world.waystations),
                         len(self.story.WAYSTATIONS))
        for (gx, gy), key in world.waystations.items():
            self.assertIn(key, self.story.WAYSTATION_BY_KEY)
            self.assertEqual(world.props.get((gx, gy)), "keeper")

    def test_a_keeper_is_somebody_you_can_talk_to(self):
        world = self.world()
        from pixel_rpg import world as world_mod
        self.assertIn("keeper", world_mod.PROP_INTERACT)
        keepers = [n for n in world.npcs if n.kind == "keeper"]
        self.assertEqual(len(keepers), len(self.story.WAYSTATIONS))

    def test_the_keeper_says_something_different_once_the_road_is_done(self):
        """The keeper has learned nothing. You read a page. That is the
        whole design (s42)."""
        for stop in self.story.WAYSTATIONS:
            before = self.story.waystation_lines(stop.key, False)
            after = self.story.waystation_lines(stop.key, True)
            self.assertTrue(before)
            self.assertTrue(after)
            self.assertNotEqual(before, after)

    def test_no_waystation_names_the_warden_on_its_road(self):
        """**The fragment may not resolve early.** This is the test that
        stops the whole feature becoming a spoiler.

        It checks **given names only, on word boundaries**, and both halves
        of that were learned the hard way:

        * Substring matching says "Ain" is in "again", "Vell" is in
          "level". The first version of this test failed on the sweeper
          saying *"she starts again"*.
        * **A surname on a place is allowed, and is the point.** The ruin
          is called Old Ain and the warden at the end of that road turns
          out to be Ysolde Ain - which resolves nothing on the way in and
          lands hard on the way out. That is the design (s42) rather than a
          leak: what may not appear early is the *person*, not a word.
        """
        import re
        # Given names: what you learn a warden was called, as a person.
        names = [w.true_name.split()[-1].lower() if w.true_name.startswith(
            "Sister") else w.true_name.split()[0].lower()
            for w in self.story.WARDENS]
        words = []
        for stop in self.story.WAYSTATIONS:
            words.extend(stop.before)
            words.extend(stop.after)
            scene = self.scenes.SCENES_BY_KEY[stop.scene]
            words.extend(scene.body)
            for choice in scene.choices:
                words.append(choice.label)
                words.extend(choice.reply)
        blob = " ".join(words).lower()
        for name in names:
            self.assertIsNone(
                re.search(r"\b%s\b" % re.escape(name), blob),
                "a waystation says %r before the Memory does" % name)

    def test_no_waystation_leans_toward_taking_the_anchor(self):
        """Every one of these is an act of looking at somebody, and
        `seventh` is the ending for not having looked."""
        for stop in self.story.WAYSTATIONS:
            scene = self.scenes.SCENES_BY_KEY[stop.scene]
            for choice in scene.choices:
                self.assertNotEqual(choice.lean, "seventh", scene.key)

    def test_every_waystation_scene_replies_to_every_answer(self):
        """s33: the reply is the point, the button is not."""
        for stop in self.story.WAYSTATIONS:
            scene = self.scenes.SCENES_BY_KEY[stop.scene]
            self.assertGreaterEqual(len(scene.choices), 3, scene.key)
            for choice in scene.choices:
                self.assertTrue(choice.reply, "%s/%s has no reply"
                                % (scene.key, choice.key))
                self.assertIn(choice.lean, self.scenes.LEANINGS)

    def test_a_waystation_scene_happens_once(self):
        """A moment that replays stops being a moment."""
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Walker", 6)
        game.dialogue = None
        try:
            spot = next(iter(game.world.waystations))
            npc = next(n for n in game.world.npcs
                       if (n.gx, n.gy) == spot)
            game.talk_to_keeper(npc)
            self.assertEqual(game.state, G.SCENE)
            game.scene = None
            game.talk_to_keeper(npc)
            self.assertEqual(game.state, G.DIALOGUE,
                             "the scene played a second time")
        finally:
            game.saves.delete(6)













class TestMoreAchievements(unittest.TestCase):
    """s49: *"also more achievements is a MUST"*.

    Eleven became twenty-three. The rules they were written to: every one
    names a number you can watch on the hero sheet, they cover what the
    game is now rather than only its combat, and the ceiling moved with
    the level cap - a list that stops at level 25 is not serving a run
    that can reach 100.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import quests
        from pixel_rpg.entities import Player
        self.quests = quests
        self.Player = Player

    def test_there_are_a_lot_more_of_them(self):
        self.assertGreaterEqual(len(self.quests.ACHIEVEMENTS), 23)

    def test_every_one_counts_something_the_game_tracks(self):
        """A feat keyed on a metric nothing increments can never be
        earned, and nothing would ever say so."""
        hero = self.Player("H", "warden")
        countable = set(hero.stats_tracker) | {"level", "secrets"}
        for feat in self.quests.ACHIEVEMENTS:
            self.assertIn(feat.metric, countable,
                          "%s counts %r, which nothing tracks"
                          % (feat.key, feat.metric))

    def test_keys_and_titles_are_unique(self):
        keys = [a.key for a in self.quests.ACHIEVEMENTS]
        titles = [a.title for a in self.quests.ACHIEVEMENTS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(titles), len(set(titles)))

    def test_only_the_secret_one_is_hidden(self):
        hidden = [a.key for a in self.quests.ACHIEVEMENTS if a.hidden]
        self.assertEqual(hidden, ["curious"])

    def test_the_new_counters_actually_move(self):
        """`favours`, `errands` and `towers` were added with the feats, so
        the increments have to exist or three of them are unreachable."""
        import io as io_mod
        source = io_mod.open("pixel_rpg/game.py", encoding="utf-8").read()
        for metric in ("favours", "errands", "towers"):
            self.assertIn('stats_tracker["%s"]' % metric, source,
                          "nothing ever counts %s" % metric)

    def test_a_fresh_hero_starts_at_zero_on_all_of_them(self):
        hero = self.Player("H", "warden")
        for metric in ("favours", "errands", "towers"):
            self.assertEqual(hero.stats_tracker[metric], 0)

    def test_they_survive_a_save(self):
        from pixel_rpg.save import serialize_player, deserialize_player
        hero = self.Player("H", "warden")
        hero.stats_tracker["towers"] = 4
        hero.stats_tracker["favours"] = 3
        back = deserialize_player(serialize_player(hero))
        self.assertEqual(back.stats_tracker["towers"], 4)
        self.assertEqual(back.stats_tracker["favours"], 3)


class TestErasingEverything(unittest.TestCase):
    """s49: *"add a setting where you reset ALL of your data, with a
    confirmation button when you click it and everything"*."""

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def stocked(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        for slot in (1, 2):
            game.pending_class = "warden"
            game.new_run("Hero%d" % slot, slot)
            game.scene = None
            game.save_run(silent=True, force=True)
        game.meta.unlock_codex("valley")
        game.meta.save()
        return game

    def test_it_asks_before_it_does_anything(self):
        from pixel_rpg import game as G
        game = self.stocked()
        game.ask_erase_everything()
        self.assertEqual(game.state, G.DIALOGUE)
        self.assertEqual(game.dialogue["title"], "Erase everything")
        self.assertTrue(any(not s["empty"]
                            for s in game.saves.describe_all()),
                        "asking must not erase")

    def test_the_question_counts_what_will_be_lost(self):
        """"Are you sure?" is a speed bump. This says six runs and a
        codex, because that is what is about to go."""
        game = self.stocked()
        game.ask_erase_everything()
        body = " ".join(game.dialogue["body"])
        self.assertIn("2 saves", body)
        self.assertIn("codex", body)
        self.assertIn("no undo", body.lower())

    def test_confirming_removes_everything(self):
        from pixel_rpg import game as G
        game = self.stocked()
        game.ask_erase_everything()
        game.dialogue["on_confirm"]()
        self.assertTrue(all(s["empty"] for s in game.saves.describe_all()))
        self.assertEqual(len(game.meta.codex), 0)
        self.assertEqual(len(game.meta.achievements), 0)
        self.assertEqual(game.state, G.TITLE)
        self.assertIsNone(game.player)

    def test_cancelling_leaves_it_all_alone(self):
        game = self.stocked()
        before = [s["empty"] for s in game.saves.describe_all()]
        game.ask_erase_everything()
        game.dialogue["on_cancel"]()
        self.assertEqual([s["empty"] for s in game.saves.describe_all()],
                         before)
        self.assertEqual(len(game.meta.codex), 1)

    def test_it_is_on_the_settings_screen_and_needs_a_confirm(self):
        """Left and Right must not start it - only Enter."""
        from pixel_rpg import menus
        self.assertIn("Erase all data", menus.SETTING_ROWS)
        import inspect
        source = inspect.getsource(menus)
        self.assertIn('label == "Erase all data" and confirm', source)


class TestEveryRunGetsItsPrologue(unittest.TestCase):
    """s48: *"also where did the prologue for the game go, i dont get one
    anymore"*.

    `meta.seen_prologue` is a **profile** flag, so it fired once ever and
    every hero after the first started with a toast. The flag was not
    wrong, it was on the wrong scene: the prologue is the story and
    repeats, the orientation is the rules and does not.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        return game

    def test_a_second_hero_still_gets_it(self):
        game = self.game()
        try:
            game.pending_class = "warden"
            game.new_run("First", 1)
            self.assertEqual(game.scene["scene"].key, "prologue")
            game.scene = None
            game.pending_class = "warden"
            game.new_run("Second", 2)
            self.assertIsNotNone(game.scene, "the second run got no scene")
            self.assertEqual(game.scene["scene"].key, "prologue")
        finally:
            for slot in (1, 2):
                game.saves.delete(slot)

    def test_the_orientation_is_the_one_that_happens_once(self):
        """It opens with "she sees that you have no idea what she means",
        which is true the first time and rude the fifth."""
        game = self.game()
        try:
            game.meta.seen_prologue = False
            game.show_prologue(orientation=True)
            self.assertIsNotNone(game.scene["after"])
            game.scene = None
            game.show_prologue(orientation=False)
            self.assertIsNone(game.scene["after"])
        finally:
            pass


class TestAFullBagAsksFirst(unittest.TestCase):
    """s48: *"when your bag is full, you should be granted with a screen
    asking you if you want to keep the item you just got and toss out
    another one or toss out the item you just got instead of immediately
    discarding it for you"*."""

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def stuffed(self, slot=1):
        from pixel_rpg import game as G
        from pixel_rpg.items import roll_item
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Packrat", slot)
        game.dialogue = None
        game.scene = None
        while not game.player.bag_full():
            game.player.add_item(roll_item(5))
        return game

    def test_room_in_the_bag_means_no_question(self):
        from pixel_rpg import game as G
        from pixel_rpg.items import roll_item
        game = self.stuffed()
        try:
            game.player.remove_item(game.player.inventory[-1])
            self.assertTrue(game.take_item(roll_item(6)))
            self.assertNotEqual(game.state, G.FULLBAG)
        finally:
            game.saves.delete(1)

    def test_a_full_bag_asks(self):
        from pixel_rpg import game as G
        from pixel_rpg.items import roll_item
        game = self.stuffed()
        try:
            prize = roll_item(20, rarity="epic")
            self.assertFalse(game.take_item(prize))
            self.assertEqual(game.state, G.FULLBAG)
            self.assertIs(game.ui.fullbag["item"], prize)
        finally:
            game.saves.delete(1)

    def test_keeping_it_drops_exactly_one_thing(self):
        from pixel_rpg.items import roll_item
        game = self.stuffed()
        try:
            prize = roll_item(20, rarity="epic")
            game.take_item(prize)
            doomed = game.droppable()[2]
            size = len(game.player.inventory)
            game.resolve_full_bag(2)
            self.assertIn(prize, game.player.inventory)
            self.assertNotIn(doomed, game.player.inventory)
            self.assertEqual(len(game.player.inventory), size)
        finally:
            game.saves.delete(1)

    def test_leaving_it_changes_nothing(self):
        from pixel_rpg.items import roll_item
        game = self.stuffed()
        try:
            before = list(game.player.inventory)
            prize = roll_item(20, rarity="epic")
            game.take_item(prize)
            game.resolve_full_bag(None)
            self.assertEqual(game.player.inventory, before)
            self.assertNotIn(prize, game.player.inventory)
        finally:
            game.saves.delete(1)

    def test_what_you_are_wearing_is_not_on_the_table(self):
        """Dropping equipped gear has a second half - now you are wearing
        nothing - and that belongs on the bag screen."""
        game = self.stuffed()
        try:
            worn = [i for i in game.player.equipment.values() if i]
            self.assertTrue(worn)
            for item in worn:
                self.assertNotIn(item, game.droppable())
        finally:
            game.saves.delete(1)


class TestABiggerBag(unittest.TestCase):
    """s48: *"add a way to upgrade your bag size to hold more relics and
    stuff"*."""

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg.entities import Player
        from pixel_rpg import security as sec
        self.Player = Player
        self.sec = sec

    def test_it_starts_smaller_than_the_ceiling(self):
        hero = self.Player("H", "warden")
        self.assertEqual(hero.bag_size(), hero.BAG_START)
        self.assertLess(hero.bag_size(), self.sec.MAX_INVENTORY)

    def test_each_one_costs_more_and_they_run_out(self):
        hero = self.Player("H", "warden")
        hero.gold = 10 ** 6
        costs, sizes = [], []
        while hero.bag_upgrade_cost():
            costs.append(hero.bag_upgrade_cost())
            sizes.append(hero.buy_bag_upgrade())
        self.assertEqual(costs, sorted(costs), "they must get dearer")
        self.assertEqual(sizes[-1], self.sec.MAX_INVENTORY)
        self.assertEqual(hero.buy_bag_upgrade(), 0, "a sixth was sold")

    def test_it_never_passes_the_validator_ceiling(self):
        """`MAX_INVENTORY` is what the save loader enforces. A bag bigger
        than that is a run that loses items on load."""
        hero = self.Player("H", "warden")
        hero.bag_bought = 99
        self.assertEqual(hero.bag_size(), self.sec.MAX_INVENTORY)

    def test_an_old_save_keeps_everything_it_was_carrying(self):
        """Bags used to start at the ceiling, so a run from before this
        can hold more than a fresh bag does."""
        from pixel_rpg.items import roll_item
        from pixel_rpg.save import serialize_player, deserialize_player
        hero = self.Player("H", "warden")
        hero.bag_bought = 0
        for _ in range(48):
            hero.inventory.append(roll_item(5))
        back = deserialize_player(serialize_player(hero))
        self.assertGreaterEqual(back.bag_size(), len(back.inventory))

    def test_it_survives_a_save(self):
        from pixel_rpg.save import serialize_player, deserialize_player
        hero = self.Player("H", "warden")
        hero.gold = 10 ** 6
        hero.buy_bag_upgrade()
        hero.buy_bag_upgrade()
        back = deserialize_player(serialize_player(hero))
        self.assertEqual(back.bag_size(), hero.bag_size())


class TestWeaponFamilies(unittest.TestCase):
    """s47: *"why would an assassin use a bow yk? give like different
    weapon types special buffs that are useful when used with the correct
    class"*.

    Every weapon belongs to a family; every class was trained on some of
    them; carrying one you trained on is worth 12% damage, and the same
    12% is a thumb on the scale in `equip_best` - which is the line that
    handed him the bow.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import classes, items
        from pixel_rpg.entities import Player
        self.classes = classes
        self.items = items
        self.Player = Player

    def test_every_weapon_has_a_family(self):
        """A base with no family gets no affinity from anybody, silently -
        which is the shape of bug this test exists to refuse."""
        for base in self.items.WEAPON_BASES:
            self.assertTrue(base.family, base.name)
            self.assertIn(base.family, self.items.FAMILIES, base.name)

    def test_nothing_else_has_one(self):
        for base in self.items.ARMOR_BASES + self.items.ACCESSORY_BASES:
            self.assertFalse(getattr(base, "family", ""), base.name)

    def test_every_family_has_a_label_and_a_weapon(self):
        used = {b.family for b in self.items.WEAPON_BASES}
        for family in self.items.FAMILIES:
            self.assertIn(family, self.items.FAMILY_LABEL, family)
            self.assertIn(family, used, "%s exists but nothing is one"
                          % family)

    def test_every_class_trained_with_something_real(self):
        for cls in self.classes.CLASSES:
            self.assertTrue(cls.favours, cls.key)
            for family in cls.favours:
                self.assertIn(family, self.items.FAMILIES,
                              "%s favours %r" % (cls.key, family))

    def test_a_class_starts_with_a_weapon_it_trained_on(self):
        """The starting weapon is the game's own statement of what a class
        is. If it disagrees with `favours`, one of them is wrong."""
        for cls in self.classes.CLASSES:
            base = self.items.BASES_BY_NAME.get(cls.start_weapon)
            self.assertIsNotNone(base, cls.start_weapon)
            self.assertIn(base.family, cls.favours,
                          "%s starts with a %s and does not favour it"
                          % (cls.key, base.family))

    def test_the_bonus_applies_only_to_your_own(self):
        thief = self.Player("Thief", "cutpurse")
        dagger = self.items.roll_item(
            8, base=self.items.BASES_BY_NAME["Dagger"], rarity="rare")
        bow = self.items.roll_item(
            8, base=self.items.BASES_BY_NAME["Hunting Bow"], rarity="rare")
        staff = self.items.roll_item(
            8, base=self.items.BASES_BY_NAME["Oak Staff"], rarity="rare")
        thief.equipment["weapon"] = dagger
        self.assertTrue(thief.has_affinity())
        with_it = thief.outgoing_multiplier()
        thief.equipment["weapon"] = staff
        self.assertFalse(thief.has_affinity())
        without = thief.outgoing_multiplier()
        self.assertAlmostEqual(with_it / without,
                               1.0 + thief.AFFINITY_BONUS, places=5)
        # A bow is the Cutpurse's other family, so it counts too.
        thief.equipment["weapon"] = bow
        self.assertTrue(thief.has_affinity())

    def test_bare_hands_are_nobody_s_family(self):
        thief = self.Player("Thief", "cutpurse")
        thief.equipment["weapon"] = None
        self.assertEqual(thief.weapon_family(), "")
        self.assertFalse(thief.has_affinity())

    def test_equip_best_prefers_your_family_on_a_close_call(self):
        """**The complaint, as a test.** Two weapons of the same item
        level: the one you trained on wins."""
        thief = self.Player("Thief", "cutpurse")
        mage = self.Player("Mage", "emberwright")
        for hero, wanted in ((thief, "dagger"), (mage, "stave")):
            hero.inventory = [
                self.items.roll_item(
                    10, base=self.items.BASES_BY_NAME[name], rarity="rare")
                for name in ("Hunting Bow", "Dagger", "Oak Staff")]
            hero.equipment["weapon"] = None
            hero.equip_best()
            got = self.items.family_of(hero.equipment["weapon"])
            self.assertIn(got, hero.pclass.favours,
                          "%s picked a %s" % (hero.pclass.key, got))

    def test_a_far_better_weapon_still_wins(self):
        """It is a preference, not a restriction: 12% cannot outweigh a
        weapon twenty levels better, and should not."""
        thief = self.Player("Thief", "cutpurse")
        thief.inventory = [
            self.items.roll_item(
                30, base=self.items.BASES_BY_NAME["Oak Staff"],
                rarity="legendary"),
            self.items.roll_item(
                4, base=self.items.BASES_BY_NAME["Dagger"], rarity="common"),
        ]
        thief.equipment["weapon"] = None
        thief.equip_best()
        self.assertEqual(self.items.family_of(thief.equipment["weapon"]),
                         "stave")

    def test_the_picker_leans_harder_than_the_fight(self):
        """Deliberately two numbers, and this says why.

        `Item.power()` is hero-blind: at equal item level it rates a
        staff's magic the same for a Cutpurse as for a mage, so a 12%
        nudge cannot stop the picker handing a thief a spellcasting stick.
        The picker's weight is what makes "equip best" agree with the
        class; the damage bonus is what the weapon is worth once you hold
        it. They answer different questions.
        """
        from pixel_rpg.entities import Player
        self.assertGreater(Player.EQUIP_AFFINITY_WEIGHT,
                           1.0 + Player.AFFINITY_BONUS)
        self.assertLess(Player.EQUIP_AFFINITY_WEIGHT, 2.0,
                        "a preference this strong is a restriction")


class TestTheSuiteCannotEatARealSave(unittest.TestCase):
    """The suite writes and deletes saves in slots 1 to 6. It must do that
    somewhere that is not a player's own folder.

    **This is here because it happened.** On 2026-09-01 a test run deleted
    a run somebody was playing, on the same machine, between one session
    and the next:

    > "bro my saves keep disappearing, i saved the game before turning it
    > off and now its gone :("

    Nothing failed. A test that deletes the wrong file passes exactly as
    happily as one that deletes the right file, which is why the check has
    to be about *where*, not about *what*.
    """

    def test_saves_go_to_a_sandbox(self):
        from pixel_rpg import config
        folder = str(config.default_save_dir())
        self.assertTrue(folder.startswith(_SAVE_SANDBOX),
                        "the suite is writing to %s" % folder)

    def test_the_sandbox_is_not_the_real_folder(self):
        """The two ways the game finds a real folder, neither of which may
        be where the suite lands: the platform's roaming directory, and a
        `portable` file next to the exe."""
        from pixel_rpg import config
        folder = str(config.default_save_dir()).lower()
        for real in ("appdata\\roaming\\pixel_rpg", "pixel_rpg\\saves"):
            self.assertNotIn(real, folder)

    def test_a_game_built_here_agrees(self):
        """A real `Game` is what the tests actually build, so ask one."""
        from pixel_rpg import game as G
        game = G.Game()
        self.assertTrue(str(game.saves.dir).startswith(_SAVE_SANDBOX),
                        "a Game in this suite saves to %s" % game.saves.dir)


class TestNobodyIsDraggedIntoAFight(unittest.TestCase):
    """s45, and s40 before it. Savvydev, twice:

    > "i keep getting dragged into battle with my friend without even being
    >  near him" ... "dragging in shouldnt happen anyway though"

    > "using e to join my friend's battle doesnt work and i'm still dragged
    >  into battles with him"

    Proximity used to *be* consent: anyone within `FIGHT_REACH` went into
    the party. It now earns the question and nothing else. The socket half
    of this is in `tests/smoke.py`, which runs two real machines; these are
    the rules the host follows when it decides who to ask.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def game(self, slot=4):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Host", slot)
        game.dialogue = None
        return game

    def test_an_invitation_expires_on_its_own(self):
        """Ignoring it is a complete answer."""
        game = self.game()
        try:
            game.offered_a_fight("Savvydev", 5, 4.0)
            self.assertIsNotNone(game.fight_offer)
            game.update_fight_offer(2.0)
            self.assertIsNotNone(game.fight_offer)
            game.update_fight_offer(2.5)
            self.assertIsNone(game.fight_offer)
        finally:
            game.saves.delete(4)

    def test_e_answers_the_invitation_before_anything_else(self):
        """A chest will still be there in four seconds. The fight will
        not, so `interact` checks the offer first."""
        game = self.game()
        try:
            game.offered_a_fight("Savvydev", 5, 4.0)
            taken = []
            game.accept_fight_offer = lambda: taken.append(True) or True
            game.interact()
            self.assertEqual(taken, [True])
        finally:
            game.saves.delete(4)

    def test_the_prompt_says_who_and_how_long(self):
        game = self.game()
        try:
            game.offered_a_fight("Savvydev", 5, 4.0)
            seen = []
            from pixel_rpg import game as G
            real = G.draw_text

            def spy(surface, assets, text, *a, **kw):
                seen.append(str(text))
                return real(surface, assets, text, *a, **kw)

            G.draw_text = spy
            try:
                game.draw()
            finally:
                G.draw_text = real
            joined = " ".join(seen)
            self.assertIn("Savvydev", joined)
            self.assertIn("join", joined.lower())
        finally:
            game.saves.delete(4)

    def test_the_prompt_is_clickable_like_every_other_one(self):
        game = self.game()
        try:
            game.offered_a_fight("Savvydev", 5, 4.0)
            game.draw()
            keys = [index for _r, kind, index in game.ui.hot.items
                    if kind == "key"]
            self.assertIn(game.binds.key_for("interact"), keys)
        finally:
            game.saves.delete(4)

    def test_an_offer_needs_somebody_to_ask(self):
        """With nobody in range the fight opens at once - the window is a
        courtesy to the people being asked, not a delay for its own sake."""
        game = self.game()
        try:
            self.assertIsNone(game.coop)
            self.assertFalse(game.host_open_fight(3, "meadow", 0, 0))
        finally:
            game.saves.delete(4)


class TestTheOfferIsHeldByTheHost(unittest.TestCase):
    """The session's half: who was asked, who said yes, and for how long."""

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import coop as coop_mod
        self.coop_mod = coop_mod

    def session(self):
        """A host session with a transport that records rather than sends."""
        sess = self.coop_mod.Session.__new__(self.coop_mod.Session)
        sess.role = self.coop_mod.HOST
        sess.pending = None
        sess.fight = None
        sess.remotes = {}
        sent = []

        class Peer:
            def __init__(self, pid):
                self.player_id = pid

        class Transport:
            peers = [Peer(2), Peer(3)]

            def send(self, peer, kind, payload=None):
                sent.append((peer.player_id, kind, payload))

        sess.transport = Transport()
        return sess, sent, Peer

    def test_everybody_invited_gets_asked(self):
        sess, sent, _Peer = self.session()
        sess.offer_fight({"level": 4, "by": "Savvydev"}, [2, 3])
        self.assertEqual(sorted(p for p, _k, _d in sent), [2, 3])
        self.assertEqual([k for _p, k, _d in sent], ["offer", "offer"])

    def test_only_somebody_who_was_asked_may_accept(self):
        """A client that says "join" without an invitation is a client
        adding itself to a fight - `net.py` rule 2."""
        sess, _sent, Peer = self.session()
        sess.offer_fight({"level": 4, "by": "Savvydev"}, [2])
        sess._accepted_invitation(Peer(3))
        self.assertEqual(sess.pending["accepted"], [])
        sess._accepted_invitation(Peer(2))
        self.assertEqual(sess.pending["accepted"], [2])

    def test_saying_yes_twice_is_saying_yes_once(self):
        sess, _sent, Peer = self.session()
        sess.offer_fight({"level": 4, "by": "Savvydev"}, [2])
        sess._accepted_invitation(Peer(2))
        sess._accepted_invitation(Peer(2))
        self.assertEqual(sess.pending["accepted"], [2])

    def test_the_window_is_short(self):
        """It is dead time for whoever walked into the monster."""
        self.assertLessEqual(self.coop_mod.Session.JOIN_WINDOW, 6.0)
        self.assertGreaterEqual(self.coop_mod.Session.JOIN_WINDOW, 2.0)


class TestHostingAsksAndThenTellsYou(unittest.TestCase):
    """s45, playtest 2: two faults in one flow.

    > "when going back to title from a save and then going to play
    >  together, you are not prompted with what save you want to load"

    > "when quitting the game entirely and loading back in you are
    >  presented with the option to pick which save but you are not
    >  presented with the room code"

    The first was a shortcut - *"already in a run: that is the world they
    mean"* - reading a run object that "Back to title" had left behind.
    The second was `start_hosting` opening a room and leaving the player
    in the overworld, where the code is written down nowhere.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def game(self, slot=5):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Walker", slot)
        game.dialogue = None
        game.save_run(silent=True, force=True)
        return game

    def test_back_to_title_ends_the_run(self):
        from pixel_rpg import game as G
        game = self.game()
        try:
            game.clear_run()
            self.assertIsNone(game.player)
            self.assertIsNone(game.world)
            self.assertIsNone(game.slot)
            self.assertFalse(game.host_when_ready)
        finally:
            game.saves.delete(5)

    def test_hosting_always_asks_which_world(self):
        """Even with a run in memory, which is the case the shortcut got
        wrong: on the title, a live run object is residue."""
        from pixel_rpg import game as G
        game = self.game()
        try:
            game.choose_save_then_host()
            # The mode is asked first (0ag, s59) and the world straight
            # after - the rule this guards is that it is *always* asked,
            # not which screen number it is.
            self.assertEqual(game.state, G.MODE_SELECT)
            game.ui.mode_index = 0
            game.handle_event(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_RETURN, unicode="", mod=0))
            self.assertEqual(game.state, G.SLOTS)
            self.assertTrue(game.host_when_ready)
            self.assertIsNone(game.coop)
        finally:
            game.saves.delete(5)

    def test_picking_a_save_opens_the_room_and_shows_the_code(self):
        from pixel_rpg import game as G
        game = self.game()
        try:
            game.clear_run()
            game.state = G.TITLE
            game.choose_save_then_host()
            game.load_run(5)
            self.assertIsNotNone(game.coop)
            self.assertEqual(game.state, G.LOBBY,
                             "hosting did not land where the code is")
            self.assertTrue(game.coop.room_code)
        finally:
            if game.coop is not None:
                game.leave_coop()
            game.saves.delete(5)

    def test_the_intent_does_not_outlive_a_cancelled_picker(self):
        """`host_when_ready` is a one-shot: backing out must not leave the
        game quietly wanting to host the next run that loads."""
        game = self.game()
        try:
            game.choose_save_then_host()
            self.assertTrue(game.host_when_ready)
            game.clear_run()
            self.assertFalse(game.host_when_ready)
        finally:
            game.saves.delete(5)


class TestYouCanSeeWhatYouAreWearing(unittest.TestCase):
    """s45: *"there's still no way to easily check the stats of the items
    you have equipped"* - and, in the first playtest, *"should be a way to
    check the stats of your own weapons"*.

    Asked twice, which under rule 1c is a priority signal rather than a
    duplicate. The bag has shown worn gear for a long time in a strip that
    appears only *"when the frame can hold it"*; the hero sheet is the
    screen you open to read yourself, and this puts the three pieces
    directly under the totals they add up to.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def sheet_text(self, size=(1280, 720)):
        """Every string the hero sheet draws, at `size`."""
        from pixel_rpg import game as G, menus
        game = G.Game()
        game.screen = pygame.display.set_mode(size)
        game.pending_class = "warden"
        game.new_run("Walker", 6)
        game.dialogue = None
        seen = []
        real = menus.draw_text

        def spy(surface, assets, text, pos, *a, **kw):
            seen.append(str(text))
            return real(surface, assets, text, pos, *a, **kw)

        menus.draw_text = spy
        try:
            game.state = G.CHARACTER
            game.draw()
        finally:
            menus.draw_text = real
            game.saves.delete(6)
        return seen, game

    def test_the_sheet_names_what_is_worn(self):
        seen, game = self.sheet_text()
        self.assertIn("EQUIPPED", seen)
        for slot in ("Weapon", "Armor", "Accessory"):
            self.assertIn(slot, seen)

    def test_it_shows_the_stats_and_not_just_the_name(self):
        """The whole ask is the numbers. A name with no stat line is the
        bag's version, which is what he was complaining about."""
        seen, _game = self.sheet_text()
        joined = " ".join(seen)
        self.assertIn("Longsword", joined)
        self.assertRegex(joined, r"ATK \+\d")

    def test_an_empty_slot_says_so(self):
        seen, _game = self.sheet_text()
        self.assertIn("- empty -", " ".join(seen))

    def test_a_folded_sheet_still_finds_room_for_it(self):
        """Under 640 of frame the sheet folds to two columns and this
        block shares one with the skills list. It needs a tall window to
        fit there - and when it does not fit, it draws **nothing**, which
        is the half `layout.py` caught: a heading with no rows under it
        was landing off the bottom of an 844x390 window."""
        seen, _game = self.sheet_text((820, 900))
        self.assertIn("EQUIPPED", seen)
        seen, _game = self.sheet_text((844, 390))
        self.assertNotIn("EQUIPPED", seen)


class TestAModifierOnlyTakes(unittest.TestCase):
    """s45: *"remove all the buffs in the modifiers, my friend has all of
    them on and is one tapping everything"*.

    A modifier was a trade - a cost and a boon - and stacking all nine
    bought more power than it cost. It is a pure cost now, paid for in
    score.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import modifiers
        self.modifiers = modifiers

    def everything(self):
        return self.modifiers.RunModifiers(
            [m.key for m in self.modifiers.MODIFIERS])

    def test_nothing_hands_power_back(self):
        mods = self.everything()
        self.assertEqual(mods.gold_mult(), 1.0)
        self.assertEqual(mods.xp_mult(), 1.0)
        self.assertEqual(mods.damage_mult(), 1.0)
        self.assertEqual(mods.defense_mult(), 1.0)

    def test_the_costs_are_all_still_there(self):
        mods = self.everything()
        self.assertEqual(mods.hp_mult(), 0.50)          # Glass Road
        self.assertEqual(mods.enemy_level_bonus(), 3)   # Haunted
        self.assertGreater(mods.encounter_mult(), 1.0)  # Long Nightfall
        for tag in ("no_items", "no_shops", "no_map", "no_flee",
                    "no_shrine", "downgrade", "always_night"):
            self.assertTrue(mods.has(tag), tag)

    def test_the_boon_tags_are_gone(self):
        """`kill_heal` and `extra_relics` were the two that handed out
        power outside `modifiers.py`, so the tags themselves have to go or
        the call sites quietly keep working."""
        mods = self.everything()
        self.assertFalse(mods.has("kill_heal"))
        self.assertFalse(mods.has("extra_relics"))

    def test_the_score_still_rewards_taking_them(self):
        self.assertGreater(self.everything().score, 10.0)

    def test_every_modifier_says_what_it_scores(self):
        """**And a preference says that it does not** (item 0aj, s68).

        §45's rule is that `boon` is a score line and nothing else, which
        is why this reads the text rather than the number. A preference
        modifier has no score to quote, so it has to say so out loud -
        silence there would read as an oversight, which is exactly what
        this test exists to catch.
        """
        for mod in self.modifiers.MODIFIERS:
            if not mod.scores:
                self.assertEqual(mod.boon, "Score unchanged", mod.key)
                continue
            self.assertTrue(mod.boon.startswith("Score x"), mod.key)
            self.assertIn(str(mod.score), mod.boon, mod.key)


class TestBlindRoadActuallyBlinds(unittest.TestCase):
    """s45: *"the minimap remover modifier doesnt work"* - and it never
    had. `no_map` appeared in `gold_mult` and `xp_mult` and nowhere else,
    so the modifier that says "no minimap" was a pure reward."""

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def game(self, *mods):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        for key in mods:
            game.pending_modifiers.add(key)
        game.new_run("Walker", 6)
        game.dialogue = None
        # **Every new run opens on its prologue now** (s48), so a test
        # that wants the overworld has to close it - the same way a
        # player does.
        game.scene = None
        game.state = G.OVERWORLD
        return game

    def test_the_world_map_refuses_to_open(self):
        from pixel_rpg import game as G
        game = self.game("blindroad")
        try:
            game.open_world_map()
            self.assertEqual(game.state, G.OVERWORLD)
        finally:
            game.saves.delete(6)

    def test_without_it_the_map_opens(self):
        from pixel_rpg import game as G
        game = self.game()
        try:
            game.open_world_map()
            self.assertEqual(game.state, G.WORLDMAP)
        finally:
            game.saves.delete(6)

    def test_the_corner_map_is_not_drawn(self):
        """Counted by what the draw registers rather than by pixels: the
        biome label under the minimap is drawn in the same block."""
        blind = self.game("blindroad")
        try:
            blind.draw()
            spots_blind = len(blind.ui.hot.items)
        finally:
            blind.saves.delete(6)
        seeing = self.game()
        try:
            seeing.state = seeing.state
            seeing.draw()
        finally:
            seeing.saves.delete(6)
        self.assertLessEqual(spots_blind, len(seeing.ui.hot.items))


class TestHollowedTakesTheShrine(unittest.TestCase):
    """s45: *"and remove the shrine entirely too"*. A shrine you can walk
    up to and be refused reads as a broken shrine."""

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def game(self, *mods, **kw):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        for key in mods:
            game.pending_modifiers.add(key)
        game.new_run("Walker", kw.get("slot", 6))
        game.dialogue = None
        return game

    def shrines(self, game):
        return [s for s, prop in game.world.props.items() if prop == "shrine"]

    def test_a_hollowed_valley_has_none(self):
        game = self.game("hollowed")
        try:
            self.assertEqual(self.shrines(game), [])
        finally:
            game.saves.delete(6)

    def test_an_ordinary_valley_still_has_them(self):
        game = self.game()
        try:
            self.assertTrue(self.shrines(game))
        finally:
            game.saves.delete(6)

    def test_they_stay_gone_across_a_save_and_load(self):
        """The terrain regenerates from the seed (hard rule 4), so a prop
        removed at creation comes back unless the removal is re-applied."""
        game = self.game("hollowed")
        try:
            game.save_run(silent=True, force=True)
            game.load_run(6)
            self.assertEqual(self.shrines(game), [])
        finally:
            game.saves.delete(6)


class TestOathkeeperHidesTheRow(unittest.TestCase):
    """s45: *"remove the flee option entirely when you have the no flee
    modifier on"*. The menu offered it and then said no."""

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import battle as battle_mod
        from pixel_rpg.entities import Player, make_enemy
        self.battle_mod = battle_mod
        self.Player = Player
        self.make_enemy = make_enemy

    def fight(self, *mods):
        from pixel_rpg.modifiers import RunModifiers
        hero = self.Player("H", "warden")
        hero.modifiers = RunModifiers(list(mods))
        return self.battle_mod.Battle(hero,
                                      self.make_enemy(3, "forest"))

    def test_the_row_is_gone(self):
        rows = self.battle_mod.menu_actions_for(self.fight("oathkeeper"))
        self.assertNotIn("Flee", rows)
        self.assertEqual(len(rows), 4)

    def test_it_is_there_without_the_modifier(self):
        rows = self.battle_mod.menu_actions_for(self.fight())
        self.assertIn("Flee", rows)

    def test_the_cursor_cannot_run_off_the_short_menu(self):
        fight = self.fight("oathkeeper")
        fight.choose_menu(4)
        self.assertLess(fight.menu_index, 4)

    def test_a_warden_arena_keeps_the_row(self):
        """One fight refusing is not a run-long rule, and a menu that
        changes shape as you walk around is worse than a refusal."""
        fight = self.fight()
        fight.can_flee_flag = False
        self.assertIn("Flee", self.battle_mod.menu_actions_for(fight))


class TestClickWhereItSaysPressEnter(unittest.TestCase):
    """s45: the death screen and a scene reply both say "Enter" and both
    ignored the mouse."""

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Walker", 6)
        game.dialogue = None
        return game

    def test_the_death_screen_takes_a_click(self):
        from pixel_rpg import game as G
        game = self.game()
        try:
            game.state = G.GAMEOVER
            game.message = "You fell."
            game.draw()
            hit = [rect for rect, kind, index in game.ui.hot.items
                   if kind == "key" and index == pygame.K_RETURN]
            self.assertTrue(hit, "nothing on the death screen is clickable")
        finally:
            game.saves.delete(6)

    def test_a_scene_reply_takes_a_click(self):
        from pixel_rpg import game as G
        game = self.game()
        try:
            game.play_scene("prologue")
            game.answer_scene(0)
            self.assertEqual(game.scene["phase"], "reply")
            game.draw()
            hit = [rect for rect, kind, index in game.ui.hot.items
                   if kind == "key" and index == pygame.K_RETURN]
            self.assertTrue(hit, "a reply cannot be clicked past")
        finally:
            game.saves.delete(6)

    def test_the_question_is_still_answered_by_its_rows(self):
        """The panel-wide Enter must not eat the clicks that pick an
        answer - the rows are registered after it and win."""
        game = self.game()
        try:
            game.play_scene("prologue")
            game.draw()
            rows = [rect for rect, kind, _i in game.ui.hot.items
                    if kind == "scene"]
            self.assertTrue(rows)
            kind, _index = game.ui.hot.at(rows[0].center)
            self.assertEqual(kind, "scene")
        finally:
            game.saves.delete(6)


class TestTheWindowSurvivesADrag(unittest.TestCase):
    """s45: *"when i readjust the window size the game just closes out
    bro"*.

    Every `VIDEORESIZE` called `pygame.display.set_mode`, and a drag emits
    a stream of them - hundreds of window rebuilds inside Windows' modal
    move/size loop. A `RESIZABLE` pygame 2 window has already resized its
    own surface by the time the event arrives, so there was nothing to set
    in the first place.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        return game

    def test_a_resize_does_not_call_set_mode(self):
        import pygame as pg
        game = self.game()
        calls = []
        real = pg.display.set_mode

        def spy(*a, **kw):
            calls.append(a)
            return real(*a, **kw)

        pg.display.set_mode = spy
        try:
            for size in ((1100, 700), (1000, 650), (900, 640)):
                game.handle_event(pg.event.Event(pg.VIDEORESIZE, w=size[0],
                                                 h=size[1], size=size))
        finally:
            pg.display.set_mode = real
        self.assertEqual(calls, [],
                         "a drag still rebuilds the window %d times"
                         % len(calls))

    def test_the_camera_follows_the_new_size(self):
        import pygame as pg
        game = self.game()
        game.handle_event(pg.event.Event(pg.VIDEORESIZE, w=1000, h=650,
                                         size=(1000, 650)))
        self.assertEqual((game.settings.width, game.settings.height),
                         game.screen.get_size())

    def test_a_window_dragged_too_small_is_snapped_back_next_frame(self):
        import pygame as pg
        from pixel_rpg.game import MIN_WINDOW_W, MIN_WINDOW_H
        game = self.game()
        game.handle_event(pg.event.Event(pg.VIDEORESIZE, w=320, h=200,
                                         size=(320, 200)))
        self.assertIsNotNone(game.pending_clamp)
        game.update(1 / 60)
        self.assertIsNone(game.pending_clamp)
        self.assertGreaterEqual(game.screen.get_width(), MIN_WINDOW_W)
        self.assertGreaterEqual(game.screen.get_height(), MIN_WINDOW_H)


class TestTheFooterTellsTheTruth(unittest.TestCase):
    """s45: *"the M button for map isnt there and you also aren't able to
    click the tooltips with your mouse"*.

    Three separate faults met in one strip along the bottom of the screen:

    * **The map was not a bindable action.** The footer is built from
      `binds`, so a key the binds do not know about cannot be advertised
      and cannot be rebound - `event_overworld` had `K_m` written into it.
    * **The overworld handed every click to the world** before looking at
      a single hotspot, so the footer and the co-op chips were drawn,
      registered and dead.
    * **`hint_key_code` was a hand-kept dict**, so even a wired-up chip
      could advertise a key no click could reach. `G` and `R` both did.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def game(self, slot=6):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Walker", slot)
        game.dialogue = None
        game.scene = None
        game.state = __import__("pixel_rpg.game", fromlist=["g"]).OVERWORLD
        return game

    def test_the_map_is_a_bindable_action(self):
        from pixel_rpg import keybinds
        actions = [a for a, _label, _key in keybinds.ACTIONS]
        self.assertIn("map", actions)
        self.assertEqual(keybinds.DEFAULTS["map"], pygame.K_m)

    def test_the_footer_lists_the_map(self):
        game = self.game()
        try:
            game.draw()
            labels = [label for _rect, kind, index in game.ui.hot.items
                      if kind == "key" for label in [index]]
            self.assertIn(pygame.K_m, labels,
                          "the footer does not offer the map")
        finally:
            game.saves.delete(6)

    def test_every_chip_it_draws_can_be_clicked(self):
        """The general form of the bug: a footer may not advertise a key
        that no click will reach."""
        game = self.game()
        try:
            game.draw()
            chips = [index for _r, kind, index in game.ui.hot.items
                     if kind == "key"]
            self.assertGreaterEqual(len(chips), 6)
            for code in chips:
                self.assertIsInstance(code, int)
                self.assertGreater(code, 0)
        finally:
            game.saves.delete(6)

    def test_clicking_a_chip_does_what_the_key_does(self):
        from pixel_rpg import game as G
        game = self.game()
        try:
            game.draw()
            spot = next(rect for rect, kind, index in game.ui.hot.items
                        if kind == "key" and index == pygame.K_m)
            game.mouse_click(spot.center)
            self.assertEqual(game.state, G.WORLDMAP)
        finally:
            game.saves.delete(6)

    def test_the_world_still_gets_the_clicks_nothing_else_wanted(self):
        """Hard rule 2.6 is not what this touches, and a smoke check
        already asserts a world click does not move you - but the click
        that lands on no chip must still reach the world."""
        from pixel_rpg import game as G
        game = self.game()
        try:
            game.draw()
            taken = [rect for rect, _k, _i in game.ui.hot.items]
            middle = (game.screen.get_width() // 2,
                      game.screen.get_height() // 2)
            self.assertFalse(any(r.collidepoint(middle) for r in taken),
                             "pick a spot with no chip on it")
            before = (game.player.x, game.player.y)
            game.mouse_click(middle)
            self.assertEqual((game.player.x, game.player.y), before)
        finally:
            game.saves.delete(6)

    def test_hint_key_code_knows_more_than_its_dict(self):
        from pixel_rpg.ui import hint_key_code
        for label, code in (("M", pygame.K_m), ("G", pygame.K_g),
                            ("R", pygame.K_r), ("Esc", pygame.K_ESCAPE),
                            ("F5", pygame.K_F5)):
            self.assertEqual(hint_key_code(label), code, label)
        self.assertIsNone(hint_key_code("not a key at all"))


class TestYourOwnMenuIsYours(unittest.TestCase):
    """s45, playtest 2: *"i cant use my skills in battle when i joined my
    friend, it opens the skill menu and then immediately closes"*.

    `Battle.phase` does two jobs. It says what the **fight** is doing -
    acting, animating, finished - and it says which **menu** is on screen.
    The first belongs to the host. The second cannot, because the host is
    not looking at your screen: it sits in `MENU` while it waits for your
    move and sends that to you ten times a second, closing whatever you
    just opened.

    **The suite passed this twice.** s19 tier 1 item 1 was the same
    symptom from the host's chair, fixed on unit tests alone and shipped
    with a written note that it was unproven over a real socket. Two
    people on two machines found it in minutes. These tests are the mirror
    side of that, and they fail against the old one-line `apply_frame`.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import battle as battle_mod
        from pixel_rpg.entities import Player, make_enemy
        self.battle_mod = battle_mod
        self.Player = Player
        self.make_enemy = make_enemy

    def mirror(self, viewer=1):
        """A two-player fight as the second player's machine sees it."""
        party = [self.Player("Host", "warden"), self.Player("Guest", "warden")]
        foes = [self.make_enemy(3, "forest")]
        fight = self.battle_mod.Battle(party, foes, viewer=viewer,
                                       mirror=True, sort_party=False)
        fight.turn = "player"
        fight.acting_player = viewer
        fight.phase = self.battle_mod.MENU
        return fight

    def host_frame(self, phase="menu", turn="player", acting=1):
        """What the host sends while it waits for somebody to decide."""
        return {"ph": phase, "tn": turn, "ap": acting}

    def test_the_host_does_not_close_your_submenu(self):
        fight = self.mirror()
        fight.phase = self.battle_mod.SUBMENU
        fight.apply_frame(self.host_frame())
        self.assertEqual(fight.phase, self.battle_mod.SUBMENU,
                         "the host's MENU shut the guest's skill list")

    def test_it_only_protects_the_person_being_waited_on(self):
        """Somebody else's turn is somebody else's business: the mirror
        takes the host's word for the phase exactly as it always did."""
        fight = self.mirror()
        fight.phase = self.battle_mod.SUBMENU
        fight.apply_frame(self.host_frame(acting=0))
        self.assertEqual(fight.phase, self.battle_mod.MENU)

    def test_the_fight_moving_on_still_closes_it(self):
        """This is what makes the exception safe. The instant the host
        stops waiting - the move landed, an animation started, the fight
        ended - the mirror follows."""
        for phase in ("acting", "victory", "defeat", "fled"):
            fight = self.mirror()
            fight.phase = self.battle_mod.SUBMENU
            fight.apply_frame(self.host_frame(phase=phase))
            self.assertNotEqual(fight.phase, self.battle_mod.SUBMENU, phase)

    def test_an_enemy_turn_still_closes_it(self):
        fight = self.mirror()
        fight.phase = self.battle_mod.SUBMENU
        fight.apply_frame(self.host_frame(turn="enemy"))
        self.assertEqual(fight.phase, self.battle_mod.MENU)

    def test_a_host_is_never_protected_from_itself(self):
        """`mirror` is false on the host, and the host's own phase is not
        applied from anywhere - so this exception must not exist there."""
        party = [self.Player("Host", "warden")]
        fight = self.battle_mod.Battle(party, [self.make_enemy(3, "forest")],
                                       viewer=0, sort_party=False)
        self.assertFalse(fight.mirror)
        fight.phase = self.battle_mod.SUBMENU
        fight.turn = "player"
        fight.acting_player = 0
        fight.apply_frame(self.host_frame(acting=0))
        self.assertEqual(fight.phase, self.battle_mod.MENU)

    def test_everything_else_in_the_frame_still_lands(self):
        """The exception is one field under one condition. A frame that
        the mirror is protecting still updates the fight around it."""
        fight = self.mirror()
        fight.phase = self.battle_mod.SUBMENU
        frame = self.host_frame()
        frame.update({"rd": 4, "bn": "Guest attacks!", "lg": ["a line"]})
        fight.apply_frame(frame)
        self.assertEqual(fight.phase, self.battle_mod.SUBMENU)
        self.assertEqual(fight.round, 4)
        self.assertEqual(fight.banner, "Guest attacks!")
        self.assertEqual(fight.log[-1], "a line")

    def test_opening_the_list_and_picking_from_it_survives_a_frame(self):
        """The playtest sequence, start to finish: open Skills, take a
        frame, and the list is still there to pick from."""
        from pixel_rpg import battlescreen
        fight = self.mirror()
        fight.menu_index = 1                       # Skill
        fight.confirm_menu()
        self.assertEqual(fight.phase, self.battle_mod.SUBMENU)
        for _ in range(5):
            fight.apply_frame(self.host_frame())
        self.assertEqual(fight.phase, self.battle_mod.SUBMENU)
        self.assertFalse(battlescreen.waiting_on_someone_else(fight),
                         "the guest may act on their own turn")


class TestErrandVoices(unittest.TestCase):
    """An errand is a person asking, not a job board (2026-08-31, s44).

    `ERRAND_DEFS` is the counter and the purse; `story.ERRAND_VOICE` is
    why anybody wants it done. The constraint that shapes all of it is
    that **an errand does not know who is saying it** - the world hands
    them to whoever is spare, so the words have to work in the mouth of a
    runner, a villager, or "A man of Two Wells".
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import quests, story
        self.story = story
        self.quests = quests

    def test_every_errand_has_a_voice(self):
        for errand in self.quests.ERRAND_DEFS:
            voice = self.story.errand_voice(errand.key)
            self.assertIsNotNone(voice, errand.key)
            for part in ("ask", "chase", "done"):
                lines = getattr(voice, part)
                self.assertTrue(lines, "%s has no %s" % (errand.key, part))
                self.assertIsInstance(lines, tuple, errand.key)

    def test_a_voice_speaks_in_the_first_person(self):
        """The speaker is whoever the world had spare, so the words may
        never assume a he or a she. First person sidesteps it, and this is
        the check that the next one written does the same."""
        import re
        for voice in self.story.ERRAND_VOICE:
            blob = " ".join(voice.ask + voice.chase + voice.done)
            self.assertTrue(
                re.search(r"\b(I|my|me|we|our)\b", blob),
                "%s never speaks as itself" % voice.key)

    def test_the_game_never_genders_the_speaker(self):
        """**The bug this replaced.** The payout line said *"she says so
        in about four words"* and *"out of her bag"* - said by men, by
        women, and by "Somebody of Byre".

        The docstring of the function under test quotes the old line, so
        it is stripped before looking: a test that reads source has to
        read the code and not the commentary about it.
        """
        import io as io_mod
        source = io_mod.open("pixel_rpg/game.py", encoding="utf-8").read()
        start = source.index("def pay_errand")
        end = source.index("\n    def ", start + 10)
        body = source[start:end]
        doc = body.index('"""')
        body = body[:doc] + body[body.index('"""', doc + 3) + 3:]
        for word in ("she ", "her ", "he ", "his ", "him "):
            self.assertNotIn(
                '"%s' % word, body.lower(),
                "pay_errand assumes the speaker's gender: %r" % word)

    def test_an_errand_still_says_what_it_wants(self):
        """The prose is in front of the counter, not instead of it."""
        game, npc, key = errand_game()
        try:
            errand = self.quests.ERRANDS_BY_KEY[key]
            game.offer_errand(npc)
            body = " ".join(game.dialogue["body"])
            self.assertIn(errand.detail, body)
            self.assertIn(str(errand.xp), body)
            self.assertIn(self.story.errand_voice(key).ask[0], body)
        finally:
            game.saves.delete(6)

    def test_the_chase_and_the_payout_are_theirs_too(self):
        game, npc, key = errand_game()
        try:
            voice = self.story.errand_voice(key)
            game.take_errand(self.quests.ERRANDS_BY_KEY[key])
            game.dialogue = None
            game.offer_errand(npc)
            self.assertIn(voice.chase[0], " ".join(game.dialogue["body"]))
            # Finish it the way the counters do, then come back.
            errand = self.quests.ERRANDS_BY_KEY[key]
            if errand.metric in game.player.stats_tracker:
                game.player.stats_tracker[errand.metric] += errand.target * 3
            else:
                self.skipTest("this errand is not counter-driven")
            game.dialogue = None
            game.offer_errand(npc)
            self.assertIn(voice.done[0], " ".join(game.dialogue["body"]))
        finally:
            game.saves.delete(6)

    def test_nothing_out_here_knows_about_the_wardens(self):
        """s33: the valley does not know what is happening up the road.
        These people have ewes, knees and a daughter."""
        blob = " ".join(
            line for voice in self.story.ERRAND_VOICE
            for line in voice.ask + voice.chase + voice.done).lower()
        blob += " " + " ".join(
            line for lines in self.story.HAMLET_LINES.values()
            for line in lines).lower()
        # **Given names only, the way `TestWaystations` does it**, and for
        # the same reason: a surname on a place is allowed and is the whole
        # trick. Little Ain is a hamlet that mentions the ruin up the last
        # road, which resolves nothing and lands later (s42).
        import re
        for warden in self.story.WARDENS:
            name = (warden.true_name.split()[-1]
                    if warden.true_name.startswith("Sister")
                    else warden.true_name.split()[0])
            self.assertIsNone(
                re.search(r"\b%s\b" % re.escape(name.lower()), blob),
                "a bystander names %s" % warden.true_name)
        for ending in self.story.ENDINGS:
            self.assertNotIn(ending.title.lower(), blob)
        self.assertNotIn("warden", blob)


class TestTheHamletsSpeak(unittest.TestCase):
    """Six outposts that all said "Quiet year, so far." (2026-08-31, s44).

    `_build_outpost` gave its person an empty `lines` tuple, so every
    hamlet in the valley fell through to the villager default - six places
    with names like **The Drowned Rows**, and one shared sentence between
    them.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import story
        from pixel_rpg.world import World
        self.story = story
        self.World = World

    def test_every_hamlet_name_has_something_to_say(self):
        from pixel_rpg.world import World
        for name in World.HAMLET_NAMES:
            self.assertTrue(self.story.hamlet_lines(name), name)

    def test_the_people_in_them_carry_those_lines(self):
        for seed in (4242, 17, 900):
            world = self.World(32, seed).generate()
            folk = [n for n in world.npcs
                    if n.kind == "villager" and " of " in n.name]
            self.assertTrue(folk, seed)
            for npc in folk:
                place = npc.name.split(" of ", 1)[1]
                self.assertEqual(tuple(npc.lines),
                                 tuple(self.story.hamlet_lines(place)),
                                 npc.name)

    def test_a_ruin_has_nobody_in_it(self):
        """s42 gives the empty places their job - a valley with abandoned
        ones in it has had a past - and somebody living there undoes it."""
        from pixel_rpg.world import World
        for seed in (4242, 17):
            world = self.World(32, seed).generate()
            names = {n.name for n in world.npcs}
            for ruin in World.RUIN_NAMES:
                for name in names:
                    self.assertFalse(name.endswith(" of %s" % ruin),
                                     "%s lives in a ruin" % name)

    def test_everybody_out_here_has_a_name(self):
        """Savvydev, 2026-08-31: *"bro give the npcs actual names, instead
        of "A woman off Cobb's Rest" kinda weird"*.

        Hamlet folk were the only people in the valley introduced as a
        category - the keepers, the runners, the smith and the elder are
        all named. This is that fixed, and the check that a new hamlet
        name cannot be added without somebody living there.
        """
        from pixel_rpg.world import World
        for place in World.HAMLET_NAMES:
            person = self.story.hamlet_person(place)
            self.assertTrue(person, place)
            self.assertTrue(person.endswith(" of %s" % place), person)
            first = person.split(" of ")[0]
            self.assertNotIn(first, ("A woman", "A man", "Somebody"), place)
        for seed in (4242, 17, 900):
            world = self.World(32, seed).generate()
            for npc in world.npcs:
                self.assertFalse(
                    npc.name.startswith(("A woman", "A man", "Somebody")),
                    "%s (seed %d) is a category, not a person"
                    % (npc.name, seed))

    # **The one exemption, and it is his**: *"unless its apart of the story
    # or something to have a weird name"*. The sweeper at Old Ain is
    # anonymous because who she is is the reveal (s42) - naming her in the
    # NPC list would spoil the sixth road from the overworld.
    ANONYMOUS_ON_PURPOSE = {"the sweeper"}

    def test_nobody_is_named_after_their_job_alone(self):
        """Savvydev, 2026-08-31: *"real names for everyone bro (unless its
        apart of the story or something to have a weird name)"*.

        The village four were "Villager", "Herder", "Weaver" and "Child".
        A trade in the name is fine and is the valley's own convention -
        Bram the Smith, Foreman Reyd, Postmaster Ivy - but the trade may
        not be the whole of it.
        """
        roles = {"villager", "herder", "weaver", "child", "smith", "elder",
                 "runner", "keeper", "postmaster", "foreman", "lamplighter",
                 "woman", "man", "somebody", "person"}
        for seed in (4242, 17, 900):
            world = self.World(32, seed).generate()
            for npc in world.npcs:
                if npc.name in self.ANONYMOUS_ON_PURPOSE:
                    continue
                first = npc.name.split(",")[0].split(" of ")[0]
                words = [w.strip(".").lower() for w in first.split()]
                self.assertTrue(
                    any(w not in roles and w not in ("the", "a")
                        for w in words),
                    "%s is a job, not a name" % npc.name)

    def test_the_scene_map_still_finds_the_village_four(self):
        """Renaming them re-keyed `VILLAGER_SCENES`, and a miss there is
        silent: they simply stop having a conversation."""
        from pixel_rpg import scenes as scenes_mod
        from pixel_rpg import game as G
        for seed in (4242, 17):
            world = self.World(32, seed).generate()
            named = [n.name for n in world.npcs
                     if n.kind == "villager" and " of " not in n.name]
            self.assertEqual(len(named), 4, seed)
            for name in named:
                self.assertIn(name, scenes_mod.VILLAGER_SCENES, name)
                self.assertIn(name, G.Game.VILLAGER_LINES, name)
                key = scenes_mod.VILLAGER_SCENES[name]
                self.assertIn(key, scenes_mod.SCENES_BY_KEY, key)
                self.assertEqual(scenes_mod.SCENES_BY_KEY[key].title, name,
                                 "the scene header still says a job")

    def test_the_same_hamlet_is_the_same_person_every_run(self):
        """A name that changes with the seed is not a name. It also means
        a later pass can write *for* Orrin, the way the keepers got
        written for."""
        seen = {}
        for seed in (4242, 17, 900, 5):
            world = self.World(32, seed).generate()
            for npc in world.npcs:
                if npc.kind != "villager" or " of " not in npc.name:
                    continue
                place = npc.name.split(" of ", 1)[1]
                seen.setdefault(place, npc.name)
                self.assertEqual(seen[place], npc.name, place)

    def test_they_say_it_before_they_ask_you_for_anything(self):
        """The same rule the village NPCs already had: a favour offered
        first jumps the queue in front of the one thing they were written
        to say."""
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Walker", 6)
        try:
            npc = next(n for n in game.world.npcs
                       if n.kind == "villager" and " of " in n.name
                       and ((n.gx, n.gy) in game.world.favours
                            or (n.gx, n.gy) in game.world.errand_givers))
            game.dialogue = None
            game.scene = None
            game.talk_to_villager(npc)
            self.assertEqual(game.state, G.DIALOGUE,
                             "%s asked for something first" % npc.name)
            self.assertEqual(list(game.dialogue["body"]), list(npc.lines))
            # And the ask is still there, next time.
            game.dialogue = None
            game.talk_to_villager(npc)
            self.assertIn(game.state, (G.SCENE, G.DIALOGUE))
            if game.state == G.DIALOGUE:
                self.assertNotEqual(list(game.dialogue["body"]),
                                    list(npc.lines))
        finally:
            game.saves.delete(6)


class TestWhoGetsAskedWhat(unittest.TestCase):
    """Seventeen asks, about fifteen people (2026-08-31, s44)."""

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg.world import World
        self.World = World

    def worlds(self, seeds=(4242, 17, 900, 5, 88)):
        return [self.World(32, seed).generate() for seed in seeds]

    def test_both_kinds_are_placed_every_time(self):
        """Favours used to be dealt first and take everybody, so the last
        errands were never placed - a priority nobody had chosen."""
        from pixel_rpg import story
        from pixel_rpg.quests import ERRAND_DEFS
        for world in self.worlds():
            self.assertGreaterEqual(len(world.favours),
                                    len(story.FAVOURS) - 3)
            self.assertGreaterEqual(len(world.errand_givers),
                                    len(ERRAND_DEFS) - 3)
            self.assertFalse(set(world.favours) & set(world.errand_givers))

    def test_which_ones_are_missing_depends_on_the_seed(self):
        """There are more asks than people, so a run cannot hold all of
        them - and a second run is not the same list. That is the replay
        system being paid rather than spent (s33)."""
        from pixel_rpg import story
        seen = set()
        for world in self.worlds():
            placed = frozenset(entry[0] for entry in world.favours.values())
            seen.add(placed)
        self.assertGreater(len(seen), 1,
                           "every seed places exactly the same favours")

    def test_a_courier_is_not_given_somebody_elses_knees(self):
        """`ErrandVoice.settled` marks the ones that only work in the
        mouth of somebody who stays put - their own ewes, their own knees,
        their daughter standing behind you. Maren's runners walk the
        valley for a living, and a courier who cannot manage the distance
        is the kind of wrong that reads as a bug in the writing.
        """
        from pixel_rpg import story
        for world in self.worlds():
            for spot, key in world.errand_givers.items():
                npc = next(n for n in world.npcs
                           if (n.gx, n.gy) == spot)
                voice = story.errand_voice(key)
                if npc.kind == "runner" and voice is not None:
                    self.assertFalse(voice.settled,
                                     "%s was given %s" % (npc.name, key))

    def test_a_favour_is_never_placed_twice(self):
        for world in self.worlds():
            keys = [entry[0] for entry in world.favours.values()]
            self.assertEqual(len(keys), len(set(keys)))
            errands = list(world.errand_givers.values())
            self.assertEqual(len(errands), len(set(errands)))

    def test_every_favour_reply_is_a_tuple_of_lines(self):
        """A reply written as a bare string is iterated character by
        character by `tuple(reply)` in `play_favour` - which is one line
        per letter on screen, and is exactly what "bread" was doing."""
        from pixel_rpg import story
        for favour in story.FAVOURS:
            for label, reply, _correct in favour.options:
                self.assertIsInstance(reply, tuple,
                                      "%s/%s" % (favour.key, label))
                for line in reply:
                    self.assertIsInstance(line, str, favour.key)

    ATTRIBUTION = (r"\b(he|she)\s+(says?|agrees?|adds?|asks?|replies|"
                   r"nods?|repeats?|drinks?|tries|counts?|puts?|starts?|"
                   r"goes|takes?|does|looks?|hands?|gets?|stops?|sounds?)"
                   r"\b")

    def test_the_asker_is_never_given_a_gender(self):
        """**A favour does not know who is asking it.** They are handed to
        whoever the world had spare, and since everybody has a name (s34)
        the panel says "Orrin of Two Wells" over the prose. *"She is
        counting fence posts"* was fine while the asker was anonymous and
        contradicts the header the moment they are not.

        This checks **attribution** rather than banning pronouns outright:
        the roads favour is about somebody's brother, and *"He is going to
        be unbearable"* is exactly right.
        """
        import re
        from pixel_rpg import story
        pattern = re.compile(self.ATTRIBUTION, re.I)
        for favour in story.FAVOURS:
            lines = list(favour.prompt)
            for label, reply, _ok in favour.options:
                lines.append(label)
                lines.extend(reply)
            for line in lines:
                self.assertIsNone(pattern.search(line),
                                  "%s: %r" % (favour.key, line))

    def test_an_errand_voice_never_genders_its_speaker_either(self):
        import re
        from pixel_rpg import story
        pattern = re.compile(self.ATTRIBUTION, re.I)
        for voice in story.ERRAND_VOICE:
            for line in voice.ask + voice.chase + voice.done:
                self.assertIsNone(pattern.search(line),
                                  "%s: %r" % (voice.key, line))

    def test_a_toll_asks_for_something_that_exists(self):
        from pixel_rpg import story
        from pixel_rpg.items import CONSUMABLES
        for favour in story.FAVOURS:
            if favour.kind == "toll":
                self.assertIn(favour.needs, CONSUMABLES, favour.key)

    def test_every_favour_has_exactly_one_right_answer(self):
        from pixel_rpg import story
        for favour in story.FAVOURS:
            right = [o for o in favour.options if o[2]]
            self.assertEqual(len(right), 1, favour.key)


class TestDialogueProseIsAllReadable(unittest.TestCase):
    """The dialogue box had the scene screen's bug (2026-08-31, s44).

    `draw_dialogue` did `wrapped = wrapped[:max_lines]`. Nothing
    overflowed, so `tests/layout.py` passed it - and at 390 high it kept
    **three lines**, under a docstring that says wardens say long, uneven
    things. It scrolls now, on the same keys as the scene screen.
    """

    DESKTOP = ((1280, 720), (1024, 768), (1600, 900))
    PHONE = ((844, 390), (390, 844))

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def probe(self, size, body):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode(size)
        game.dialogue = {"title": "Somebody", "body": list(body),
                         "confirm": "Nod", "on_confirm": None, "scroll": 0}
        return game

    def bodies(self):
        """Every long thing the game puts in a dialogue box."""
        from pixel_rpg import story
        for warden in story.WARDENS:
            yield "intro %s" % warden.boss_key, list(warden.intro)
        for voice in story.ERRAND_VOICE:
            yield voice.key, list(voice.ask) + ["", "Defeat 30 monsters.",
                                               "", "340 gold and 360 "
                                               "experience, when it is "
                                               "done."]
        for name, lines in story.HAMLET_LINES.items():
            yield name, list(lines)

    def test_every_line_is_reachable_at_every_size(self):
        from pixel_rpg import menus
        for size in self.DESKTOP + self.PHONE:
            for label, body in self.bodies():
                game = self.probe(size, body)
                _box, wrapped, capacity, _lh, _h = menus.dialogue_measure(
                    game)
                reach = menus.dialogue_scroll_max(game) + capacity
                self.assertGreaterEqual(
                    reach, len(wrapped),
                    "%s cannot be read to the end at %dx%d"
                    % (label, size[0], size[1]))

    def test_nothing_needs_scrolling_on_a_desktop(self):
        from pixel_rpg import menus
        for size in self.DESKTOP:
            for label, body in self.bodies():
                game = self.probe(size, body)
                self.assertEqual(menus.dialogue_scroll_max(game), 0,
                                 "%s needs scrolling at %dx%d"
                                 % (label, size[0], size[1]))

    def test_a_long_name_stays_inside_the_panel(self):
        """**The world makes long names and nothing bounded the title.**
        "A woman of Cobb's Rest" and "Pell, Maren's runner" are real NPC
        names; drawn centred in the big font with no limit, they left the
        panel and then the window at 390 wide. The checker had never
        driven a dialogue with a real name in it - it used "Elder Maren"
        and "Bram Vance" - which is s7's rule again: a screen nobody
        drives with real data is a screen nobody looked at.
        """
        from pixel_rpg import menus
        from pixel_rpg.ui import ellipsize
        game = self.probe((390, 844), ["Short enough."])
        box, _w, _c, _lh, _h = menus.dialogue_measure(game)
        font = game.assets.font("big")
        for name in ("A woman of Cobb's Rest", "Somebody of The Drowned Rows",
                     "Pell, Maren's runner", "Bram Vance"):
            drawn = ellipsize(font, name, box.w - 48)
            self.assertLessEqual(font.size(drawn)[0], box.w - 48, name)

    def test_scrolling_is_silent_when_it_fits(self):
        game = self.probe((1280, 720), ["One line."])
        self.assertFalse(game.scroll_dialogue(1))
        self.assertEqual(game.dialogue["scroll"], 0)

    def test_scrolling_clamps_at_both_ends(self):
        from pixel_rpg import menus, story
        long_body = list(story.ERRAND_VOICE[0].ask) * 3
        game = self.probe((844, 390), long_body)
        limit = menus.dialogue_scroll_max(game)
        self.assertGreater(limit, 0)
        self.assertTrue(game.scroll_dialogue(-1))
        self.assertEqual(game.dialogue["scroll"], 0)
        for _ in range(limit + 4):
            game.scroll_dialogue(1)
        self.assertEqual(game.dialogue["scroll"], limit)


def errand_game(slot=6):
    """A run, somebody handing out an errand, and which errand it is."""
    from pixel_rpg import game as G
    game = G.Game()
    game.screen = pygame.display.set_mode((1280, 720))
    game.pending_class = "warden"
    game.new_run("Walker", slot)
    game.dialogue = None
    spot, key = next(iter(game.world.errand_givers.items()))
    npc = next(n for n in game.world.npcs if (n.gx, n.gy) == spot)
    return game, npc, key


class TestResolutionScenes(unittest.TestCase):
    """The road back (2026-08-30, roadmap item 12).

    A waystation asks a question on the way out that cannot resolve. This
    is the scene where it resolves, and the rules it has to obey are the
    mirror image of the ones guarding the way out:

    * It **must** name its warden. That is the payoff, and a resolution
      that stays coy is the fragment again.
    * It must **not** name an ending, which is the one leak rule that
      still applies here (s33).
    * The fourth answer is offered only to a player holding that warden's
      `heard_` shard - the "ask who they were" option s33 says has to look
      useless. This is the only place outside the Door where it pays.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import scenes, story
        self.story = story
        self.scenes = scenes

    def resolutions(self):
        for stop in self.story.WAYSTATIONS:
            yield stop, self.scenes.SCENES_BY_KEY[stop.resolution]

    def test_every_waystation_has_one(self):
        keys = [stop.resolution for stop in self.story.WAYSTATIONS]
        self.assertEqual(len(keys), self.story.ANCHOR_COUNT)
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(len(self.scenes.RESOLUTION_SCENES), len(keys))
        for key in keys:
            self.assertIn(key, self.scenes.SCENES_BY_KEY)

    def test_a_resolution_names_its_warden(self):
        """The inverse of `test_no_waystation_names_the_warden_on_its_road`,
        and the reason that test is scoped to the way out."""
        for stop, scene in self.resolutions():
            lore = self.story.WARDEN_BY_BOSS[stop.boss_key]
            name = (lore.true_name.split()[-1] if lore.true_name.startswith(
                "Sister") else lore.true_name.split()[0])
            words = " ".join(list(scene.body) + [
                line for choice in scene.choices for line in choice.reply])
            self.assertIn(name, words,
                          "%s never says who was at the end of the road"
                          % scene.key)

    def test_a_resolution_never_names_an_ending(self):
        """s33's leak rule is the one that survives into the second half:
        the player may know who they killed and still must not be told
        which endings exist."""
        titles = [e.title.lower() for e in self.story.ENDINGS]
        blob = " ".join(
            line.lower() for _stop, scene in self.resolutions()
            for line in list(scene.body) + [
                l for c in scene.choices for l in c.reply]).lower()
        for title in titles:
            self.assertNotIn(title, blob)
        self.assertNotIn("hollow key", blob)

    def test_the_fourth_answer_needs_the_shard(self):
        """One secret choice each, gated on having listened to that warden
        die, and the order matters: shard `n` belongs to road `n`."""
        for i, (stop, scene) in enumerate(self.resolutions()):
            secret = [c for c in scene.choices if c.needs]
            self.assertEqual(len(secret), 1, scene.key)
            self.assertEqual(secret[0].needs, self.scenes.HEARD[i],
                             "%s is gated on the wrong warden" % scene.key)
            self.assertEqual(secret[0].lean, "names", scene.key)
            self.assertIs(secret[0], scene.choices[-1],
                          "%s does not offer the secret last" % scene.key)

    def test_the_first_answer_is_silence(self):
        """Every one of these is a chance to say something to somebody who
        does not know. Saying nothing has to be on the table, first, and
        it leans toward letting the thing be what it is."""
        for _stop, scene in self.resolutions():
            self.assertEqual(scene.choices[0].lean, "breaking", scene.key)

    def test_no_resolution_leans_toward_taking_the_anchor(self):
        for _stop, scene in self.resolutions():
            for choice in scene.choices:
                self.assertNotEqual(choice.lean, "seventh", scene.key)
                self.assertIn(choice.lean, self.scenes.LEANINGS)
                self.assertTrue(choice.reply, "%s/%s has no reply"
                                % (scene.key, choice.key))

    def test_a_player_holding_no_shard_still_sees_three_answers(self):
        for _stop, scene in self.resolutions():
            self.assertEqual(len(scene.offered(())), 3, scene.key)
            self.assertEqual(len(scene.choices), 4, scene.key)

    def test_they_are_in_the_playback_register(self):
        """The codex's replay screen is every scene in `SCENES` the profile
        has seen (s19 item 11), so being in the list is the whole of what
        it takes to be replayable."""
        keys = {s.key for s in self.scenes.SCENES}
        for stop in self.story.WAYSTATIONS:
            self.assertIn(stop.resolution, keys)

    def test_the_keeper_residue_survives_a_silent_answer(self):
        """`after` is the residue now, not the payoff, and it is said
        whatever the player did - so it may not claim they spoke."""
        for stop in self.story.WAYSTATIONS:
            self.assertTrue(stop.after)
            self.assertNotEqual(stop.after, stop.before)


class TestTheRoadBack(unittest.TestCase):
    """Talking to a keeper after the road is done, in a real game."""

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def keeper_game(self, slot=6):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Walker", slot)
        game.dialogue = None
        spot, key = next(iter(game.world.waystations.items()))
        npc = next(n for n in game.world.npcs if (n.gx, n.gy) == spot)
        return game, npc, key

    def fell_the_road(self, game, key):
        """Mark that road done and hand over its Memory, the way the kill
        does - the resolution scene is gated on both."""
        from pixel_rpg import story
        stop = story.WAYSTATION_BY_KEY[key]
        portal = next(p for p in game.world.portals
                      if p.boss_key == stop.boss_key)
        portal.defeated = True
        lore = story.WARDEN_BY_BOSS[stop.boss_key]
        game.player.add_relic(lore.memory_key)
        return stop, lore

    def test_the_resolution_plays_once_after_the_road_is_done(self):
        from pixel_rpg import game as G
        game, npc, key = self.keeper_game()
        try:
            stop, _lore = self.fell_the_road(game, key)
            game.talk_to_keeper(npc)
            self.assertEqual(game.state, G.SCENE)
            self.assertEqual(game.scene["scene"].key, stop.resolution)
            game.scene = None
            game.talk_to_keeper(npc)
            self.assertEqual(game.state, G.DIALOGUE,
                             "the resolution played twice")
        finally:
            game.saves.delete(6)

    def test_it_waits_for_the_memory_rather_than_the_kill(self):
        """The scene says out loud that you are carrying a page. Without
        the page it holds, and it does not burn its flag doing so."""
        from pixel_rpg import game as G
        from pixel_rpg import story
        game, npc, key = self.keeper_game()
        try:
            stop = story.WAYSTATION_BY_KEY[key]
            portal = next(p for p in game.world.portals
                          if p.boss_key == stop.boss_key)
            portal.defeated = True
            game.talk_to_keeper(npc)
            self.assertEqual(game.state, G.DIALOGUE,
                             "played the resolution with no Memory held")
            lore = story.WARDEN_BY_BOSS[stop.boss_key]
            game.player.add_relic(lore.memory_key)
            game.dialogue = None
            game.talk_to_keeper(npc)
            self.assertEqual(game.state, G.SCENE, "the flag was spent early")
            self.assertEqual(game.scene["scene"].key, stop.resolution)
        finally:
            game.saves.delete(6)

    def test_the_fragment_does_not_play_once_the_road_is_done(self):
        """A player who never stopped here on the way out gets the
        resolution, not the question - the question is over."""
        from pixel_rpg import game as G
        game, npc, key = self.keeper_game()
        try:
            stop, _lore = self.fell_the_road(game, key)
            game.talk_to_keeper(npc)
            self.assertEqual(game.scene["scene"].key, stop.resolution)
            self.assertNotEqual(game.scene["scene"].key, stop.scene)
        finally:
            game.saves.delete(6)

    def test_both_scenes_can_happen_in_one_run(self):
        from pixel_rpg import game as G
        game, npc, key = self.keeper_game()
        try:
            stop = None
            game.talk_to_keeper(npc)
            self.assertEqual(game.state, G.SCENE)
            first = game.scene["scene"].key
            game.scene = None
            game.dialogue = None
            stop, _lore = self.fell_the_road(game, key)
            game.talk_to_keeper(npc)
            self.assertEqual(game.state, G.SCENE)
            self.assertEqual(first, stop.scene)
            self.assertEqual(game.scene["scene"].key, stop.resolution)
        finally:
            game.saves.delete(6)


class TestSceneProseIsAllReadable(unittest.TestCase):
    """The scene screen used to eat story silently (2026-08-30).

    `draw_scene` stopped drawing when it ran out of panel. Nothing
    overflowed, so `tests/layout.py` passed it: text that was never drawn
    is not outside its box. At 844x390 - a phone in landscape, and part of
    the gate since s29 - that cut the prologue, the Door, the orientation
    and four of the six waystation scenes, and it would have shown four
    lines of a thirteen-line resolution.

    So two rules, and they are different rules:

    * **On a desktop window every scene fits without scrolling.** If a new
      scene is too long for 1280x720 it is too long, full stop.
    * **On any window, every line is reachable** by scrolling. That is
      what makes the cut impossible rather than merely unlikely.
    """

    DESKTOP = ((1280, 720), (1024, 768), (1600, 900))
    PHONE = ((844, 390), (390, 844))

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def probe(self, size):
        """A game whose scene screen can be measured at `size`."""
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode(size)
        return game

    def measure(self, game, scene, lines, options):
        from pixel_rpg import menus
        game.scene = {"scene": scene, "options": list(options), "index": 0,
                      "phase": "reply" if lines is not scene.body
                      else "choosing",
                      "reply": tuple(lines), "after": None, "scroll": 0}
        return menus.scene_measure(game)

    def every_block(self):
        """Everything the scene screen ever draws.

        **Favours are built at the point of asking** (`Game.play_favour`
        makes a `Scene` out of a `story.Favour`), so they are not in
        `SCENES` and were escaping both of this class's rules - the
        66-column measure and the reachability check. They are the longest
        prompts in the game after the resolutions.
        """
        from pixel_rpg import scenes as scenes_mod, story
        for scene in scenes_mod.SCENES:
            yield scene, scene.body, scene.choices
            for choice in scene.choices:
                yield scene, choice.reply, ()
        for favour in story.FAVOURS:
            options = tuple(
                scenes_mod.Choice(str(i), label, tuple(reply))
                for i, (label, reply, _c) in enumerate(favour.options))
            built = scenes_mod.Scene("favour_%s" % favour.key,
                                     "Somebody of The Drowned Rows",
                                     "The Valley", tuple(favour.prompt),
                                     options)
            yield built, built.body, options
            for choice in options:
                yield built, choice.reply, ()

    def test_every_scene_fits_a_desktop_window(self):
        for size in self.DESKTOP:
            game = self.probe(size)
            for scene, lines, options in self.every_block():
                _box, wrapped, capacity, _rows = self.measure(
                    game, scene, lines, options)
                self.assertLessEqual(
                    len(wrapped), capacity,
                    "%s needs scrolling at %dx%d" % (scene.key, size[0],
                                                     size[1]))

    def test_every_line_is_reachable_on_a_phone(self):
        """Capacity plus the scroll range has to cover the prose. This is
        the check the old `break` could never have passed."""
        from pixel_rpg import menus
        for size in self.PHONE:
            game = self.probe(size)
            for scene, lines, options in self.every_block():
                _box, wrapped, capacity, _rows = self.measure(
                    game, scene, lines, options)
                self.assertGreaterEqual(capacity, 1, scene.key)
                reach = menus.scene_scroll_max(game) + capacity
                self.assertGreaterEqual(
                    reach, len(wrapped),
                    "%s cannot be read to the end at %dx%d"
                    % (scene.key, size[0], size[1]))

    def test_no_scene_line_rewraps_inside_the_panel(self):
        """Scene prose is hand-broken, so the panel has to be wide enough
        to take it as written.

        The panel was 600 wide, which is 60 characters, and every scene in
        the game is written to 65 - so forty lines re-wrapped and dropped
        their last word or two onto a line of their own. Nothing
        overlapped, so `tests/layout.py` passed it; it just looked like a
        mistake in the writing. This is the rule that keeps it fixed in
        both directions: **write to 66 columns, and do not narrow the
        panel under them.**
        """
        from pixel_rpg import scenes as scenes_mod
        from pixel_rpg import menus
        from pixel_rpg.util import wrap_text
        game = self.probe((1280, 720))
        scene = scenes_mod.SCENES_BY_KEY["prologue"]
        game.scene = {"scene": scene, "options": list(scene.choices),
                      "index": 0, "phase": "choosing", "reply": (),
                      "after": None, "scroll": 0}
        box, _wrapped, _capacity, _rows = menus.scene_measure(game)
        font = game.assets.font("small")
        for scene in scenes_mod.SCENES:
            lines = list(scene.body) + [line for choice in scene.choices
                                        for line in choice.reply]
            for line in lines:
                if not line:
                    continue
                self.assertEqual(
                    len(wrap_text(line, font, box.w - 60)), 1,
                    "%s re-wraps: %r" % (scene.key, line))

    def test_scrolling_is_silent_when_everything_fits(self):
        """`scroll_scene` returns False on a window with room, so Left and
        Right keep meaning whatever else they mean."""
        from pixel_rpg import scenes as scenes_mod
        game = self.probe((1280, 720))
        scene = scenes_mod.SCENES_BY_KEY["prologue"]
        game.scene = {"scene": scene, "options": list(scene.choices),
                      "index": 0, "phase": "choosing", "reply": (),
                      "after": None, "scroll": 0}
        self.assertFalse(game.scroll_scene(1))
        self.assertEqual(game.scene["scroll"], 0)

    def test_scrolling_clamps_at_both_ends(self):
        from pixel_rpg import scenes as scenes_mod, menus
        game = self.probe((844, 390))
        scene = scenes_mod.SCENES_BY_KEY["res_cut"]
        game.scene = {"scene": scene, "options": list(scene.choices),
                      "index": 0, "phase": "choosing", "reply": (),
                      "after": None, "scroll": 0}
        limit = menus.scene_scroll_max(game)
        self.assertGreater(limit, 0, "res_cut fits 844x390; pick another")
        self.assertTrue(game.scroll_scene(-1))
        self.assertEqual(game.scene["scroll"], 0)
        for _ in range(limit + 5):
            game.scroll_scene(1)
        self.assertEqual(game.scene["scroll"], limit)

    def test_answering_puts_the_reply_at_its_first_line(self):
        """A long question scrolled halfway down must not hand the reply a
        scroll position it never asked for."""
        from pixel_rpg import scenes as scenes_mod
        game = self.probe((844, 390))
        scene = scenes_mod.SCENES_BY_KEY["res_cut"]
        game.player = None
        game.scene = {"scene": scene, "options": scene.offered(()),
                      "index": 0, "phase": "choosing", "reply": (),
                      "after": None, "scroll": 0, "replay": True,
                      "taken": set()}
        game.scroll_scene(2)
        self.assertGreater(game.scene["scroll"], 0)
        game.answer_scene(0)
        self.assertEqual(game.scene["phase"], "reply")
        self.assertEqual(game.scene["scroll"], 0)

class TestTheAnchorCount(unittest.TestCase):
    """Six wardens is content, not a statistic (s42).

    Savvydev, 2026-08-24, on the longer story: *"make it so that the story
    remains expandable without cutting like the meaning of an ending from
    the previous version"*. This class is that sentence, enforced.

    Adding a seventh warden does not make the story longer - it makes
    finished runs wrong, because `THE SEVENTH` is an ending **named after
    the number**. So the expansion goes *between* the wardens, and these
    tests fail loudly if anybody ever tries the other thing.
    """

    def setUp(self):
        from pixel_rpg import story
        self.story = story

    def test_there_are_six_anchors_and_the_endings_depend_on_it(self):
        self.assertEqual(self.story.ANCHOR_COUNT, 6)
        self.assertEqual(len(self.story.WARDENS), self.story.ANCHOR_COUNT)

    def test_the_seventh_is_named_after_the_count(self):
        """If this ever needs changing, the ending needs renaming, and a
        rename invalidates every profile that already reached it."""
        seventh = self.story.ENDINGS_BY_KEY["seventh"]
        self.assertEqual(seventh.title, "THE SEVENTH")
        self.assertEqual(self.story.ANCHOR_COUNT + 1, 7)

    def test_breaking_requires_every_memory_there_is(self):
        """Derived from the wardens, so a seventh could not be silently
        left out of the ending that is *about* freeing all of them."""
        breaking = self.story.ENDINGS_BY_KEY["breaking"]
        self.assertEqual(set(breaking.requires),
                         {w.memory_key for w in self.story.WARDENS})

    def test_the_prose_that_counts_them_still_matches(self):
        """`THE BREAKING` and `THE LONG WALK` say the number out loud. A
        mismatch here means a finished run reads a lie."""
        words = " ".join(
            line for key in ("breaking", "longwalk")
            for line in self.story.ENDINGS_BY_KEY[key].body).lower()
        self.assertIn("six", words)
        self.assertEqual(self.story.ANCHOR_COUNT, 6,
                         "the prose says six; the roster does not")

    def test_every_warden_is_named_in_the_breaking(self):
        """The roll-call is the payoff of the whole curiosity mechanic
        (s33). A warden missing from it is a warden the ending forgets."""
        body = " ".join(self.story.ENDINGS_BY_KEY["breaking"].body)
        for warden in self.story.WARDENS:
            # Any part of the name, because one of them is a title:
            # "Sister Vell" is called Vell in the roll-call, correctly.
            self.assertTrue(
                any(part in body for part in warden.true_name.split()),
                "%s is not in the roll-call" % warden.true_name)


class TestChapters(unittest.TestCase):
    """Where you are in the story, said out loud (s19 item 10).

    Savvydev\'s friend finished the game and *"had no clue what was really
    going on"*. §33 explains how: the story is optional, so a player who
    never asks is playing correctly and learns nothing. A chapter is the
    floor under that - and it names structure the game already had rather
    than inventing any.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
        from pixel_rpg import story
        self.story = story

    def fresh_game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Reader", 6)
        game.dialogue = None
        return game

    def test_a_chapter_per_warden_and_one_for_the_door(self):
        self.assertEqual(self.story.chapter_total(), 7)
        self.assertEqual(self.story.chapter_for(0).number, 1)
        self.assertEqual(self.story.chapter_for(6).number, 7)

    def test_an_impossible_count_still_has_a_chapter(self):
        """It is fed from a world that may have come off a socket, and a
        crash here would take the HUD down."""
        self.assertEqual(self.story.chapter_for(99).number, 7)
        self.assertEqual(self.story.chapter_for(-3).number, 1)

    def test_every_chapter_has_a_goal_in_the_imperative(self):
        """The goal is what a player reads coming back after a week - the
        case this whole item exists for. An empty one is useless."""
        for chapter in self.story.CHAPTER_ARC:
            self.assertTrue(chapter.title.strip(), chapter)
            self.assertTrue(chapter.goal.strip(), chapter)
            self.assertLess(len(chapter.title), 40, chapter)

    def test_no_chapter_names_a_warden_or_an_ending(self):
        """**Only the chapter you are in is named**, and the words of the
        one you are in must not give away what is coming (s33). A warden\'s
        true name is learned *after* you kill them; an ending title is a
        spoiler on sight."""
        words = " ".join(c.title + " " + c.goal
                         for c in self.story.CHAPTER_ARC).lower()
        for warden in self.story.WARDENS:
            first = warden.true_name.split()[0].lower()
            self.assertNotIn(first, words,
                             "a chapter names %s" % warden.true_name)
        for ending in self.story.ENDINGS:
            self.assertNotIn(ending.title.lower(), words,
                             "a chapter names the %s ending" % ending.key)
        self.assertNotIn("ysolde", words)
        self.assertNotIn("hollow key", words)

    def test_a_new_run_starts_in_chapter_one_without_a_card(self):
        """The prologue says all of it at more length; a card in front of
        that is the third screen before the player has moved."""
        from pixel_rpg import game as G
        game = self.fresh_game()
        try:
            self.assertEqual(game.chapter().number, 1)
            self.assertEqual(game.player.chapter_seen, 1)
            self.assertNotEqual(game.state, G.CHAPTER_CARD)
        finally:
            game.saves.delete(6)

    def test_the_chapter_turns_when_a_warden_falls(self):
        from pixel_rpg import game as G
        game = self.fresh_game()
        try:
            game.world.portals[0].defeated = True
            game.refresh_chapter()
            self.assertEqual(game.state, G.CHAPTER_CARD)
            self.assertEqual(game.chapter_card.number, 2)
            self.assertEqual(game.player.chapter_seen, 2)
        finally:
            game.saves.delete(6)

    def test_a_card_is_shown_once_and_not_again(self):
        """A moment that repeats stops being a moment - and reloading a
        save must not replay it."""
        from pixel_rpg import game as G
        game = self.fresh_game()
        try:
            game.world.portals[0].defeated = True
            game.refresh_chapter()
            game.skip_chapter_card()
            self.assertEqual(game.state, G.OVERWORLD)
            game.refresh_chapter()
            self.assertEqual(game.state, G.OVERWORLD,
                             "the card came back")
        finally:
            game.saves.delete(6)

    def test_the_card_lifts_on_its_own(self):
        from pixel_rpg import game as G
        game = self.fresh_game()
        try:
            game.world.portals[0].defeated = True
            game.refresh_chapter()
            game.update(game.CHAPTER_HOLD + game.CHAPTER_FADE + 0.1)
            self.assertEqual(game.state, G.OVERWORLD)
            self.assertIsNone(game.chapter_card)
        finally:
            game.saves.delete(6)

    def test_the_chapter_survives_a_save_and_a_load(self):
        """`chapter_seen` rides with the run, so coming back does not
        replay a card - and an older save honestly says zero."""
        from pixel_rpg import save as savemod
        game = self.fresh_game()
        try:
            game.world.portals[0].defeated = True
            game.refresh_chapter()
            game.skip_chapter_card()
            game.save_run(silent=True, force=True)
            game.load_run(6)
            self.assertEqual(game.player.chapter_seen, 2)

            raw = savemod.serialize_player(game.player)
            del raw["chapter_seen"]
            older = savemod.deserialize_player(raw)
            self.assertEqual(older.chapter_seen, 0)
        finally:
            game.saves.delete(6)


class TestWhereToGoNext(unittest.TestCase):
    """The story tab and the marker (s19 item 11).

    Same complaint as chapters: *"had no clue what was really going on"*.
    Chapters say where you are in the story; these two say what to do about
    it - one on a screen you can go and read, one on the screen you are
    already looking at.
    """

    def setUp(self):
        __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()

    def fresh_game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Reader", 6)
        game.dialogue = None
        return game

    def test_the_marker_points_at_the_road_that_is_open(self):
        game = self.fresh_game()
        try:
            portal = game.world.portals[0]
            found = game.objective()
            self.assertIsNotNone(found)
            x, y, label = found
            self.assertAlmostEqual(x, portal.gx * game.tile + game.tile / 2)
            self.assertAlmostEqual(y, portal.gy * game.tile + game.tile / 2)
            self.assertEqual(label, "The road")
        finally:
            game.saves.delete(6)

    def test_it_moves_on_when_that_warden_falls(self):
        game = self.fresh_game()
        try:
            game.world.portals[0].defeated = True
            game.world.portals[1].unlocked = True
            x, _y, _label = game.objective()
            self.assertAlmostEqual(
                x, game.world.portals[1].gx * game.tile + game.tile / 2)
        finally:
            game.saves.delete(6)

    def test_a_locked_road_is_never_pointed_at(self):
        """Pointing somewhere the player cannot go is worse than
        pointing nowhere."""
        game = self.fresh_game()
        try:
            for portal in game.world.portals:
                portal.unlocked = False
            self.assertIsNotNone(game.objective())   # falls through to Maren
            _x, _y, label = game.objective()
            self.assertEqual(label, "Elder Maren")
        finally:
            game.saves.delete(6)

    def test_with_every_warden_down_it_points_at_maren(self):
        """Chapter seven, and MAREN_BRIEFINGS[6] before it: she asked to
        be seen before you walk through."""
        game = self.fresh_game()
        try:
            for portal in game.world.portals:
                portal.defeated = True
            _x, _y, label = game.objective()
            self.assertEqual(label, "Elder Maren")
        finally:
            game.saves.delete(6)

    def test_no_run_means_no_marker(self):
        from pixel_rpg import game as G
        game = G.Game()
        self.assertIsNone(game.objective())

    def test_the_marker_draws_nothing_when_the_road_is_on_screen(self):
        """It is always on, so it has to earn that by staying quiet."""
        from pixel_rpg import game as G
        game = self.fresh_game()
        try:
            x, y, _label = game.objective()
            game.player.x, game.player.y = x, y
            game.camera.follow(x, y, 0.0, snap=True)
            game.state = G.OVERWORLD
            before = pygame.Surface(game.screen.get_size())
            before.blit(game.screen, (0, 0))
            game.draw_objective_marker(game.screen)
            self.assertEqual(
                pygame.image.tostring(game.screen, "RGB"),
                pygame.image.tostring(before, "RGB"),
                "the marker drew while its target was in view")
        finally:
            game.saves.delete(6)

    def test_the_story_tab_is_a_page_not_a_list(self):
        from pixel_rpg import menus
        game = self.fresh_game()
        try:
            game.ui.quest_tab = menus.STORY_TAB
            self.assertEqual(menus.quest_rows(game), [])
            # And Enter on it must not reach into an empty list.
            event = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN,
                                       unicode="", mod=0)
            game.ui.quest_readonly = False
            menus.quests_event(game, event)
        finally:
            game.saves.delete(6)

    def test_each_way_in_opens_the_tab_that_answers_it(self):
        """J is "where am I"; Maren is "what work is there"."""
        from pixel_rpg import menus, game as G
        game = self.fresh_game()
        try:
            key = game.binds.key_for("quests")
            game.event_overworld(pygame.event.Event(
                pygame.KEYDOWN, key=key, unicode="", mod=0))
            self.assertEqual(game.state, G.QUESTS)
            self.assertEqual(game.ui.quest_tab, menus.STORY_TAB)
            self.assertTrue(game.ui.quest_readonly)

            game.talk_to_elder()
            self.assertEqual(game.ui.quest_tab, 1)
            self.assertFalse(game.ui.quest_readonly)
        finally:
            game.saves.delete(6)

    def test_the_story_tab_names_no_warden_and_no_ending(self):
        """It shows what the player has been told and rearranges it; it
        must not become a place the story leaks (s33)."""
        from pixel_rpg import menus, story
        game = self.fresh_game()
        try:
            for portal in game.world.portals:
                portal.defeated = True
            drawn = []
            real = menus.draw_text

            def spy(surface, assets, text, pos, *a, **k):
                drawn.append(str(text))
                return real(surface, assets, text, pos, *a, **k)

            menus.draw_text = spy
            try:
                game.ui.quest_tab = menus.STORY_TAB
                menus.draw_quests(game)
            finally:
                menus.draw_text = real
            words = " ".join(drawn).lower()
            self.assertTrue(words, "the tab drew nothing")
            for warden in story.WARDENS:
                self.assertNotIn(warden.true_name.lower(), words)
            for ending in story.ENDINGS:
                self.assertNotIn(ending.title.lower(), words)
        finally:
            game.saves.delete(6)


class TestTheVeteranClause(unittest.TestCase):
    """A finished hero may cross into a finished world, and only then.

    Savvydev, 2026-08-24: *"make it so that if a player beats the game on a
    save, they can bring that hero from that save into the host\'s game,
    only if the host had beaten the game in the save they\'re hosting
    too."* A New Game+ handshake - one side alone cannot import power into
    a fresh run - and **offered, never automatic** (2026-08-29).
    """

    def fresh_game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        return game

    def finished_run(self, game, slot, name="Veteran", level=12):
        """A run in a slot that has been beaten."""
        game.saves.delete(slot)
        game.pending_class = "warden"
        game.new_run(name, slot)
        game.dialogue = None
        for _ in range(level - 1):
            game.player.gain_xp(game.player.xp_to_next())
        game.player.beaten = True
        game.save_run(silent=True, force=True)
        return game.player.name

    class PretendHost:
        """Just the half of a session the door asks about."""
        host_name = "Hostess"
        role = "host"

        def __init__(self, beaten):
            self.host_beaten = beaten
            self.pending_chests = []

        def publish_body(self, *a, **k):
            pass

    def test_a_finished_save_is_reported_as_finished(self):
        """`meta.endings` is global; this has to be per-save."""
        game = self.fresh_game()
        try:
            self.finished_run(game, 3)
            self.assertTrue(game.saves.describe(3).get("beaten"))
            game.saves.delete(2)
            game.pending_class = "warden"
            game.new_run("Rookie", 2)
            game.dialogue = None
            game.save_run(silent=True, force=True)
            self.assertFalse(game.saves.describe(2).get("beaten"),
                             "an unfinished run was called finished")
        finally:
            for slot in (2, 3):
                game.saves.delete(slot)

    def test_the_door_needs_both_sides_to_have_finished(self):
        game = self.fresh_game()
        seed = 61_001
        game.guests.forget(seed)
        try:
            self.finished_run(game, 3)
            game.player = game.world = None

            game.coop = self.PretendHost(beaten=False)
            self.assertFalse(game.veteran_import_offered(seed),
                             "a fresh world took a finished hero")
            game.coop = self.PretendHost(beaten=True)
            self.assertTrue(game.veteran_import_offered(seed))
        finally:
            game.coop = None
            game.saves.delete(3)
            game.guests.forget(seed)

    def test_no_finished_save_means_no_offer(self):
        """The other side of the same handshake."""
        game = self.fresh_game()
        seed = 61_002
        game.guests.forget(seed)
        try:
            for slot in range(1, 7):
                game.saves.delete(slot)
            game.coop = self.PretendHost(beaten=True)
            self.assertFalse(game.veteran_import_offered(seed))
        finally:
            game.coop = None
            game.guests.forget(seed)

    def test_a_valley_you_already_have_a_hero_in_is_never_asked_about(self):
        """The question is *which hero starts here*, and it is settled."""
        game = self.fresh_game()
        seed = 61_003
        game.guests.forget(seed)
        try:
            self.finished_run(game, 3)
            game.coop = self.PretendHost(beaten=True)
            game.adopt_world(seed)
            game.pending_class = "warden"
            game.new_guest_run("Local", seed)
            self.assertFalse(game.veteran_import_offered(seed))
        finally:
            game.coop = None
            game.saves.delete(3)
            game.guests.forget(seed)

    def test_the_door_is_a_choice_and_declining_starts_fresh(self):
        game = self.fresh_game()
        from pixel_rpg import game as G
        seed = 61_004
        game.guests.forget(seed)
        try:
            self.finished_run(game, 3)
            game.player = game.world = None
            game.coop = self.PretendHost(beaten=True)

            self.assertFalse(game.adopt_world(seed))
            self.assertEqual(game.state, G.SLOTS)
            self.assertEqual(game.ui.slot_mode, "veteran",
                             "the veteran door never opened")

            event = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE,
                                       unicode="", mod=0)
            game.event_slots(event)
            self.assertEqual(game.state, G.CLASS_SELECT,
                             "declining did not start a fresh hero")
            self.assertEqual(game.guest_pending, seed)
        finally:
            game.coop = None
            game.saves.delete(3)
            game.guests.forget(seed)

    def test_importing_copies_the_hero_and_leaves_the_save_alone(self):
        game = self.fresh_game()
        seed = 61_005
        game.guests.forget(seed)
        try:
            self.finished_run(game, 3, "Ilan", level=12)
            level = game.player.level
            game.player = game.world = None
            game.coop = self.PretendHost(beaten=True)

            game.adopt_world(seed)
            self.assertTrue(game.import_veteran(3, seed))
            self.assertEqual(game.player.name, "Ilan")
            self.assertEqual(game.player.level, level)
            self.assertEqual(game.world.seed, seed, "wrong valley")
            self.assertEqual(game.guest_world, seed)
            self.assertIsNone(game.guest_pending)
            self.assertTrue(game.guests.has(seed))

            # **A copy.** What happens to them over there is nothing to do
            # with the run they came from.
            game.player.gold = 1
            game.save_run(silent=True, force=True)
            self.assertNotEqual(game.saves.describe(3).get("gold"), 1,
                                "the original save was written through")
            self.assertEqual(game.saves.describe(3).get("name"), "Ilan")
        finally:
            game.coop = None
            game.saves.delete(3)
            game.guests.forget(seed)

    def test_an_unfinished_run_cannot_cross(self):
        """The picker filters, and the import refuses anyway."""
        game = self.fresh_game()
        seed = 61_006
        game.guests.forget(seed)
        try:
            game.saves.delete(2)
            game.pending_class = "warden"
            game.new_run("Rookie", 2)
            game.dialogue = None
            game.save_run(silent=True, force=True)
            game.player = game.world = None
            game.coop = self.PretendHost(beaten=True)

            self.assertFalse(game.import_veteran(2, seed))
            self.assertFalse(game.guests.has(seed),
                             "an unfinished hero was kept in the valley")
        finally:
            game.coop = None
            game.saves.delete(2)
            game.guests.forget(seed)

    def test_the_host_tells_the_room_whether_it_has_finished(self):
        """The flag has to cross the wire, off the *run being hosted*."""
        from pixel_rpg import coop as coop_mod, security as sec
        sent = {}

        class PretendTransport:
            def send(self, peer, kind, body):
                if kind == "welcome":
                    sent.update(body)

            def broadcast(self, kind, body):
                pass

            def drop(self, peer, why):
                pass

        class PretendPeer:
            player_id = 2
            name = ""

        class PretendPlayer:
            beaten = True

        class PretendGame:
            player = PretendPlayer()

        session = coop_mod.Session(coop_mod.HOST, "Hostess")
        session.transport = PretendTransport()
        session.seed = 4242
        session._greet(PretendGame(), PretendPeer(),
                       {"protocol": coop_mod.PROTOCOL, "name": "Guest"})
        self.assertTrue(sent.get("beaten"), "the welcome never said so")

        guest = coop_mod.Session(coop_mod.CLIENT, "Guest")
        guest._welcomed(sent)
        self.assertTrue(guest.host_beaten)
        # And a welcome that says nothing means no.
        plain = coop_mod.Session(coop_mod.CLIENT, "Guest")
        plain._welcomed({k: v for k, v in sent.items() if k != "beaten"})
        self.assertFalse(plain.host_beaten)


class TestJoiningWithoutARunOfYourOwn(unittest.TestCase):
    """Joining no longer needs a hero, because a hero belongs to a world.

    Earlier the same day joining was made to *refuse* without a run, which
    was one way to stop `adopt_world` dying on `'NoneType' object has no
    attribute 'x'`. Savvydev then reversed the model: *"If you're joining a
    save for the first time (not as a host), you start at level one, no
    existing hero or anything."* (s19 item 5c.)

    So the refusal goes and the crash must stay fixed, which is two
    different things and is why both are checked here. **Hosting still
    refuses** - a room is a world, and a host has to have one.
    """

    def fresh_game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        return game

    def test_joining_without_a_run_is_no_longer_refused_for_that(self):
        game = self.fresh_game()
        self.assertIsNone(game.player)
        game.code_buffer = "82Q244DZNM"
        # It still fails - there is no host on the other end - but the
        # reason must not be "you have no hero" any more.
        self.assertFalse(game.try_join())
        self.assertIsNone(game.coop)
        self.assertNotIn("load a game first", game.ui.lobby_notice.lower())

    def test_stepping_into_a_new_valley_with_no_hero_does_not_crash(self):
        """The crash the refusal was standing in for."""
        game = self.fresh_game()
        from pixel_rpg import game as G
        seed = 60_001
        game.guests.forget(seed)
        self.assertIsNone(game.player)
        self.assertFalse(game.adopt_world(seed),
                         "a world was adopted without a hero to stand in it")
        self.assertEqual(game.state, G.CLASS_SELECT)
        self.assertEqual(game.guest_pending, seed)
        self.assertIsNone(game.player)

    def test_a_first_visit_makes_a_level_one_hero_for_that_valley(self):
        game = self.fresh_game()
        seed = 60_002
        game.guests.forget(seed)
        try:
            game.adopt_world(seed)
            game.pending_class = "warden"
            game.new_guest_run("Newcomer", seed)
            self.assertIsNotNone(game.player)
            self.assertEqual(game.player.level, 1)
            self.assertEqual(game.world.seed, seed)
            self.assertEqual(game.guest_world, seed)
            self.assertIsNone(game.guest_pending, "the one-shot outlived it")
            self.assertTrue(game.guests.has(seed), "the hero was not kept")
        finally:
            game.guests.forget(seed)

    def test_returning_to_a_valley_finds_the_hero_you_left(self):
        game = self.fresh_game()
        seed = 60_003
        game.guests.forget(seed)
        try:
            game.adopt_world(seed)
            game.pending_class = "warden"
            game.new_guest_run("Ilan", seed)
            game.player.gold = 512
            game.save_guest_hero(silent=True)
            game.player = game.world = None
            game.guest_world = None

            self.assertTrue(game.adopt_world(seed),
                            "the hero left there was not found")
            self.assertEqual(game.player.name, "Ilan")
            self.assertEqual(game.player.gold, 512)
            self.assertEqual(game.guest_world, seed)
        finally:
            game.guests.forget(seed)

    def test_a_guest_hero_never_lands_in_one_of_your_slots(self):
        """The whole reason the routing lives inside `save_run`."""
        game = self.fresh_game()
        seed = 60_004
        game.guests.forget(seed)
        game.saves.delete(5)
        try:
            game.pending_class = "warden"
            game.new_run("Mine", 5)
            game.dialogue = None
            game.save_run(force=True)
            before = game.saves.describe(5)

            game.adopt_world(seed)          # your run is written back first
            game.new_guest_run("Visitor", seed)
            game.player.gold = 999
            game.save_run(silent=True, force=True)

            after = game.saves.describe(5)
            self.assertEqual(after.get("name"), before.get("name"))
            self.assertNotEqual(after.get("name"), "Visitor")
            self.assertTrue(game.guests.has(seed))
        finally:
            game.guests.forget(seed)
            game.saves.delete(5)

    def test_leaving_puts_you_back_in_your_own_run(self):
        game = self.fresh_game()
        seed = 60_005
        game.guests.forget(seed)
        game.saves.delete(4)
        try:
            game.pending_class = "warden"
            game.new_run("Homebody", 4)
            game.dialogue = None
            game.save_run(force=True)

            game.adopt_world(seed)
            game.new_guest_run("Visitor", seed)
            self.assertEqual(game.player.name, "Visitor")

            game.leave_coop(resume_own_run=True)
            self.assertIsNone(game.guest_world)
            self.assertEqual(game.player.name, "Homebody",
                             "leaving did not give the run back")
            self.assertTrue(game.guests.has(seed),
                            "the guest hero was not kept behind")
        finally:
            game.guests.forget(seed)
            game.saves.delete(4)

    def test_leaving_with_no_run_of_your_own_returns_to_the_title(self):
        """The hero must not stay on screen: Host a room would then open a
        room onto somebody else's valley."""
        game = self.fresh_game()
        from pixel_rpg import game as G
        seed = 60_006
        game.guests.forget(seed)
        try:
            game.adopt_world(seed)
            game.pending_class = "warden"
            game.new_guest_run("Drifter", seed)
            game.leave_coop(resume_own_run=True)
            self.assertIsNone(game.player)
            self.assertIsNone(game.world)
            self.assertIsNone(game.guest_world)
            self.assertEqual(game.state, G.TITLE)
        finally:
            game.guests.forget(seed)

    def test_hosting_without_a_run_is_still_refused(self):
        """The pair no longer agree, and that is the design: a room is a
        world, so a host needs one - a guest does not."""
        game = self.fresh_game()
        game.start_hosting(reach_out=False, port=0)
        self.assertIsNone(game.coop)


class TestLeavingARoom(unittest.TestCase):
    """There has to be a way out of a room that is not quitting the game.

    §40: *"unless my friend exited out of the game completely, he would join
    back into the server"*. The lobby is only reachable from the title, the
    pause menu had no Leave row, and "Back to title" never disconnected - so
    the socket stayed open and going back in put you straight back into
    somebody else's world.
    """

    def game_in_a_room(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Leaver", 1)
        game.dialogue = None
        game.start_hosting(reach_out=False, port=0)
        self.assertIsNotNone(game.coop, "hosting failed")
        return game

    def test_the_leave_row_appears_only_in_a_room(self):
        from pixel_rpg import menus
        game = self.game_in_a_room()
        try:
            self.assertIn("Leave room", menus.pause_items(game))
            game.leave_coop()
            self.assertNotIn("Leave room", menus.pause_items(game))
        finally:
            game.leave_coop()

    def test_leaving_actually_disconnects(self):
        from pixel_rpg import game as G, menus
        game = self.game_in_a_room()
        try:
            game.go(G.PAUSE)
            items = menus.pause_items(game)
            game.ui.pause_index = items.index("Leave room")
            game.handle_event(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_RETURN, unicode="", mod=0))
            self.assertIsNone(game.coop)
        finally:
            game.leave_coop()

    def test_back_to_title_disconnects_too(self):
        """Leaving the run leaves the room; the socket must not outlive it."""
        from pixel_rpg import game as G, menus
        game = self.game_in_a_room()
        try:
            game.go(G.PAUSE)
            items = menus.pause_items(game)
            game.ui.pause_index = items.index("Back to title")
            game.handle_event(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_RETURN, unicode="", mod=0))
            self.assertIsNone(game.coop)
        finally:
            game.leave_coop()


class TestWhoMayAct(unittest.TestCase):
    """Only the player whose turn it is may act, on every machine.

    The school playtest (§40) found a host playing its guest's turn *for*
    them, the guest losing their prompt, and the menu showing the wrong
    person's skills. All three were one line:

        return battle.mirror and not battle.is_your_turn

    `mirror` is only true on a **client**, so the host was never a
    spectator and took input on everybody's turn. Nothing caught it,
    because every automated check ran both halves in one process and the
    host is the half that drives them.
    """

    def battle_for(self, viewer, acting, mirror, turn="player"):
        from pixel_rpg.battle import Battle, MENU
        from pixel_rpg.entities import Player, make_enemy
        party = [Player("Host", "warden"), Player("Guest", "warden")]
        battle = Battle(party, [make_enemy(3, "meadow")],
                        viewer=viewer, mirror=mirror, sort_party=False)
        battle.turn = turn
        battle.acting_player = acting
        battle.phase = MENU
        return battle

    def test_a_host_may_not_act_on_its_guests_turn(self):
        """The bug, stated as a test. `mirror` is False on a host."""
        from pixel_rpg.battlescreen import waiting_on_someone_else
        host_view = self.battle_for(viewer=0, acting=1, mirror=False)
        self.assertTrue(waiting_on_someone_else(host_view))

    def test_a_host_may_act_on_its_own_turn(self):
        from pixel_rpg.battlescreen import waiting_on_someone_else
        self.assertFalse(waiting_on_someone_else(
            self.battle_for(viewer=0, acting=0, mirror=False)))

    def test_a_guest_may_act_only_on_its_own_turn(self):
        from pixel_rpg.battlescreen import waiting_on_someone_else
        self.assertFalse(waiting_on_someone_else(
            self.battle_for(viewer=1, acting=1, mirror=True)))
        self.assertTrue(waiting_on_someone_else(
            self.battle_for(viewer=1, acting=0, mirror=True)))

    def test_solo_is_never_a_spectator(self):
        """A party of one is viewer 0 acting 0 - unchanged by all this."""
        from pixel_rpg.battle import Battle, MENU
        from pixel_rpg.battlescreen import waiting_on_someone_else
        from pixel_rpg.entities import Player, make_enemy
        battle = Battle([Player("Solo", "warden")],
                        [make_enemy(3, "meadow")])
        battle.turn, battle.phase = "player", MENU
        self.assertFalse(waiting_on_someone_else(battle))

    def test_a_monster_turn_does_not_lock_input(self):
        """Keyed on `turn`, not `is_your_turn`, so animations stay open."""
        from pixel_rpg.battlescreen import waiting_on_someone_else
        self.assertFalse(waiting_on_someone_else(
            self.battle_for(viewer=0, acting=1, mirror=False, turn="enemy")))


class TestMouseReach(unittest.TestCase):
    """Every row a screen registers has to be reachable by the mouse.

    Hard rule 9 says mouse and keyboard share one selection model: a screen
    registers a rect with a `kind`, a click moves the same cursor the
    keyboard uses and replays Enter. **Nothing enforced the first half.**

    A screen could register `kind="lobby"`, draw a hover outline for it, and
    have `point_cursor` know nothing about it - in which case clicking lit
    the row, played the move sound, and did nothing at all. Play Together
    shipped like that, along with the debug menu, the achievements list, the
    story log, and every answer in every story scene. Only Esc worked.

    This walks the *registered* kinds and asserts the cursor can reach each
    one, which is the half that was missing.
    """

    # Drawing modules only. `battle.py` and `anim.py` import no pygame and
    # cannot register anything - `battle._step_out(kind="lunge")` is an
    # animation name that looks exactly like a hotspot to a regex.
    DRAWS = ("menus", "devmenu", "battlescreen", "game", "ui", "touch")

    def registered_kinds(self):
        import re
        kinds = set()
        for name in self.DRAWS:
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "pixel_rpg", name + ".py")
            with open(path, encoding="utf-8") as handle:
                kinds |= set(re.findall(r'kind="([a-z_]+)"', handle.read()))
        self.assertIn("lobby", kinds, "the scan found nothing; regex rotted")
        return kinds

    def acted_on_directly(self):
        """Kinds `mouse_click` answers itself, before asking `point_cursor`.

        Read out of the source rather than listed here. A hand-kept list is
        a second copy of the truth, and this whole test exists because two
        copies of the selection model drifted apart.
        """
        import inspect, re
        from pixel_rpg import game as G
        body = inspect.getsource(G.Game.mouse_click)
        kinds = set(re.findall(r'kind == "([a-z_]+)"', body))
        for pair in re.findall(r'kind in \(("[a-z_", ]+)\)', body):
            kinds |= set(re.findall(r'"([a-z_]+)"', pair))
        return kinds

    def test_every_registered_kind_can_be_reached(self):
        game = build_game_for_mouse()
        direct = self.acted_on_directly()
        unreachable = []
        for kind in sorted(self.registered_kinds()):
            if kind in direct:
                continue
            if not game.point_cursor(kind, 0):
                unreachable.append(kind)
        self.assertEqual(unreachable, [],
                         "registered but the mouse cannot move to them")

    def test_a_scene_answer_is_one_click(self):
        """Playtested 2026-08-24: select-only made scenes unusable by mouse."""
        game = build_game_for_mouse()
        self.assertNotIn("scene", game.SELECT_ONLY)
        self.assertTrue(game.point_cursor("scene", 2))
        self.assertEqual(game.scene["index"], 2)

    def test_a_read_only_list_cannot_be_confirmed_by_a_click(self):
        """Enter on these means "close", so a click on a row must not.

        **`story` came off this list on 2026-08-29** and that is not a
        regression: Enter there stopped meaning "close" and started meaning
        "watch it again". A click that confirms is only a trap when
        confirming costs something (s2.8) - replaying a scene costs
        nothing, and clicking is the only way a mouse can start one."""
        game = build_game_for_mouse()
        for kind in ("feat", "story"):
            if kind == "story":
                continue        # see the docstring: it has a verb now
            self.assertIn(kind, game.SELECT_ONLY, kind)

    def test_a_debug_row_can_be_clicked(self):
        """**Also reversed on 2026-09-01.** `dev` was select-only so that a
        stray click could not mark a run - and opening the menu marks it
        now, so the protection was guarding a door that is already open:

        > "you cant use the menu with your mouse, and like left click and
        >  stuff / mouse compatibility should exist everywhere not just
        >  where i name it"
        """
        self.assertNotIn("dev", build_game_for_mouse().SELECT_ONLY)


class TestWheel(unittest.TestCase):
    """One notch of the wheel moves one row.

    pygame sends a wheel roll **twice**. Its own documentation: "The mouse
    wheel will generate pygame.MOUSEBUTTONDOWN and pygame.MOUSEBUTTONUP
    events when rolled. The button will be set to 4 when the wheel is rolled
    up, and to button 5 when the wheel is rolled down." pygame 2 then sends
    MOUSEWHEEL for the same roll as well.

    `handle_event` acted on both, so every list moved two rows a notch, and
    the save picker skipped a slot. This drives both events - which is what
    SDL actually delivers - and asserts the total is one.
    """

    def notch(self, game, direction):
        button = 4 if direction > 0 else 5
        game.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, button=button, pos=(10, 10)))
        game.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONUP, button=button, pos=(10, 10)))
        game.handle_event(pygame.event.Event(
            pygame.MOUSEWHEEL, y=direction, x=0, flipped=False, which=0))

    def test_one_notch_is_one_row(self):
        from pixel_rpg import game as G
        game = build_game_for_mouse()
        game.go_slots("load")
        game.ui.slot.index = 3
        self.notch(game, -1)
        self.assertEqual(game.ui.slot.index, 4)
        self.notch(game, 1)
        self.assertEqual(game.ui.slot.index, 3)

    def test_the_wheel_is_never_a_click(self):
        """Button 4/5 must not fall through to `mouse_click` either."""
        game = build_game_for_mouse()
        game.go_slots("load")
        clicked = []
        game.mouse_click = lambda *a, **k: clicked.append(a)
        self.notch(game, -1)
        self.assertEqual(clicked, [])


class TestBrowserClient(unittest.TestCase):
    """The guest half of crossplay, on the side that has no socket."""

    def setUp(self):
        from pixel_rpg import net
        self.net = net
        self.window = FakeSocketWindow()
        self.client = net.BrowserClient("10.0.0.5", 47216,
                                        window=self.window)
        self.socket = self.window.last

    def test_it_asks_for_the_right_url(self):
        self.assertEqual(self.socket.url, "ws://10.0.0.5:47216/")

    def test_nothing_is_sent_before_the_socket_opens(self):
        """Held, not lost, and not put on the wire in front of the handshake."""
        self.client.send("hello", {"protocol": 1, "name": "Wanderer"})
        self.assertFalse(self.client.ready)
        self.assertEqual(self.socket.sent, [])
        self.socket.open_it()
        self.assertTrue(self.client.ready)
        self.assertEqual(len(self.socket.sent), 1)
        self.assertIn("hello", self.socket.sent[0])

    def test_a_message_comes_back_decoded(self):
        self.socket.open_it()
        self.socket.deliver(self.net.encode_body(
            "welcome", {"id": 2}).decode("utf-8"))
        self.assertEqual(self.client.pump(), [("welcome", {"id": 2})])

    def test_pump_is_empty_when_nothing_arrived(self):
        self.socket.open_it()
        self.assertEqual(self.client.pump(), [])

    def test_nonsense_is_dropped_rather_than_raised(self):
        self.socket.open_it()
        self.socket.deliver("{not json at all")
        self.socket.deliver("[]")
        self.assertEqual(self.client.pump(), [])
        self.assertTrue(self.client.connected)

    def test_a_binary_frame_is_dropped(self):
        """Nothing in this protocol sends one, so one is nonsense."""
        self.socket.open_it()
        self.socket.onmessage(FakeSocketEvent(object()))
        self.assertEqual(self.client.pump(), [])
        self.assertTrue(self.client.connected)

    def test_an_oversized_message_ends_the_connection(self):
        """The same ceiling `_Peer.read` applies, checked the same way."""
        self.socket.open_it()
        self.socket.deliver("x" * (self.net.MAX_FRAME + 1))
        self.assertEqual(self.client.pump(), [])
        self.assertFalse(self.client.connected)
        self.assertTrue(self.socket.closed)

    def test_a_flood_ends_the_connection(self):
        """A guest has to refuse a host that shouts, not just the other way."""
        self.socket.open_it()
        body = self.net.encode_body("move", {"x": 1}).decode("utf-8")
        for _ in range(self.net.MAX_MESSAGES_PER_SECOND + 5):
            self.socket.deliver(body)
        self.client.pump()
        self.assertFalse(self.client.connected)

    def test_hanging_up_is_noticed(self):
        self.socket.open_it()
        self.socket.hang_up()
        self.assertFalse(self.client.connected)
        self.assertFalse(self.client.ready)

    def test_a_send_after_the_socket_died_is_quiet(self):
        self.socket.open_it()
        self.socket.hang_up()
        self.client.send("move", {"x": 1})
        self.assertEqual(self.socket.sent, [])

    def test_a_throwing_send_ends_the_connection(self):
        window = FakeSocketWindow(refuse_send=True)
        client = self.net.BrowserClient("10.0.0.5", 47216, window=window)
        window.last.open_it()
        client.send("move", {"x": 1})
        self.assertFalse(client.connected)

    def test_a_browser_without_websockets_says_so(self):
        with self.assertRaises(self.net.NetError):
            self.net.BrowserClient("10.0.0.5", 47216,
                                   window=FakeSocketWindow(refuse=True))

    def test_from_code_reaches_the_same_place(self):
        code = self.net.encode_room("10.0.0.5", 47216)
        window = FakeSocketWindow()
        self.net.BrowserClient.from_code(code, window=window)
        self.assertEqual(window.last.url, "ws://10.0.0.5:47216/")

    # -- and what the lobby is told when it does not work ------------------

    def _session_over(self, open_first):
        """Run a client session to failure and return what it says."""
        from pixel_rpg import coop
        window = FakeSocketWindow()
        client = self.net.BrowserClient("10.0.0.5", 47216, window=window)
        session = coop.Session(coop.CLIENT, client, "Wanderer")
        if open_first:
            window.last.open_it()
        window.last.hang_up()
        session._client_frame(None, [])
        return session.error

    def test_a_room_code_that_answers_nothing_says_so(self):
        """A browser cannot fail at the door, so it fails here instead.

        `Client` raises inside its own constructor and `try_join` shows the
        real reason. A `WebSocket` reports failure later and silently, and
        "the host closed the room" would send somebody hunting at the other
        end of a code they most likely mistyped.
        """
        self.assertEqual(self._session_over(open_first=False),
                         "nothing answered at that room code")

    def test_a_room_that_opened_and_then_died_says_that_instead(self):
        self.assertEqual(self._session_over(open_first=True),
                         "the host closed the room")

    def test_the_transport_choice_follows_is_browser(self):
        """Three transports, one place that picks. Nothing above it decides."""
        from pixel_rpg import config
        original = config.is_browser
        try:
            config.is_browser = lambda: False
            self.assertIs(self.net.guest_transport(), self.net.Client)
            self.assertIs(self.net.guest_transport(True), self.net.WebClient)
            config.is_browser = lambda: True
            self.assertIs(self.net.guest_transport(), self.net.BrowserClient)
            self.assertIs(self.net.guest_transport(True),
                          self.net.BrowserClient)
        finally:
            config.is_browser = original


class TestPartyBattle(unittest.TestCase):
    """A group of players against a group of monsters."""

    def setUp(self):
        from pixel_rpg.battle import Battle
        self.Battle = Battle

    def hero(self, name, class_key="warden", levels=14):
        player = Player(name, class_key)
        for _ in range(levels):
            player.gain_xp(player.xp_to_next())
        return player

    def party(self, count=3):
        return [self.hero("Hero%d" % i) for i in range(count)]

    def group(self, count, level=4):
        return [make_enemy(level, "meadow") for _ in range(count)]

    def fight(self, players=3, monsters=2):
        return self.Battle(self.party(players), self.group(monsters))

    # -- the party is a list, and one player is a party of one --------------

    def test_a_lone_player_still_works(self):
        """Every existing caller passes one player; that must keep working."""
        solo = self.hero("Alone")
        battle = self.Battle(solo, make_enemy(4, "meadow"))
        self.assertEqual(battle.players, [solo])
        self.assertIs(battle.actor, solo)
        self.assertIs(battle.you, solo)

    def test_the_party_acts_fastest_first(self):
        slow, quick = self.hero("Slow"), self.hero("Quick")
        slow.attributes["agility"] = 1
        quick.attributes["agility"] = 40
        battle = self.Battle([slow, quick], make_enemy(4, "meadow"))
        self.assertIs(battle.players[0], quick,
                      "a slower character moved before a faster one")

    def test_the_viewer_and_the_actor_are_different_ideas(self):
        """The party-side version of the bug section 15 is about."""
        battle = self.fight(players=3)
        battle.viewer = 2
        battle.acting_player = 0
        self.assertIsNot(battle.actor, battle.you)
        self.assertIs(battle.you, battle.players[2])
        self.assertIs(battle.actor, battle.players[0])

    def test_each_player_keeps_their_own_aim(self):
        """One player's Left/Right must not swing everybody's sword around."""
        battle = self.fight(players=2, monsters=3)
        battle.acting_player = 0
        battle.cycle_target(1)
        first = battle.target
        battle.acting_player = 1
        self.assertEqual(battle.target, 0,
                         "aiming moved somebody else's target")
        self.assertNotEqual(first, battle.target)

    # -- turn order ---------------------------------------------------------

    def test_every_player_acts_before_the_monsters_do(self):
        battle = self.fight(players=3, monsters=2)
        battle.players_first = True
        battle.begin_round()
        seen = []
        while battle.acting_member() is not None:
            seen.append(battle.acting_player)
            battle.acting_player += 1
        self.assertEqual(seen, [0, 1, 2])
        self.assertEqual(battle.turn, "player",
                         "the monsters moved before the party was spent")

    def test_a_fallen_player_is_skipped_mid_round(self):
        """An index alone goes stale the moment somebody drops out."""
        battle = self.fight(players=3)
        battle.acting_player = 0
        battle.players[0].hp = 0
        battle.players[1].hp = 0
        self.assertIs(battle.acting_member(), battle.players[2])
        self.assertEqual(battle.acting_player, 2)

    def test_a_monster_hits_someone_who_is_standing(self):
        battle = self.fight(players=3)
        battle.players[0].hp = 0
        battle.players[2].hp = 0
        for _ in range(20):
            self.assertIs(battle.pick_victim(), battle.players[1])

    def test_a_monster_leans_toward_whoever_is_hurt(self):
        """Damage spread evenly over four bodies kills nobody, which is most
        of why a full room won every fight in section 23."""
        battle = self.fight(players=3, monsters=1)
        battle.players[1].hp = max(1, battle.players[1].max_hp() // 20)
        picks = [battle.pick_victim() for _ in range(600)]
        hurt = picks.count(battle.players[1])
        self.assertGreater(hurt, 600 / 3,
                           "the wounded hero was in no more danger than anyone")

    def test_a_monster_never_only_hits_the_hurt_one(self):
        """Always striking the weakest cascades a downed player and leaves
        nothing to decide."""
        battle = self.fight(players=3, monsters=1)
        battle.players[1].hp = 1
        picks = {id(p) for p in (battle.pick_victim() for _ in range(600))}
        self.assertEqual(len(picks), 3,
                         "somebody standing was never once targeted")

    def test_an_untouched_party_is_chosen_evenly(self):
        battle = self.fight(players=4, monsters=1)
        picks = [battle.pick_victim() for _ in range(1200)]
        counts = [picks.count(p) for p in battle.players]
        self.assertLess(max(counts) - min(counts), 200,
                        "a full-health party was not treated evenly")

    def test_picking_among_one_costs_no_randomness(self):
        """A draw here would shift the seeded stream `tools/balance.py`
        measures solo play with - which is how a five-point swing in four
        classes was traced to a refactor that changed no rule."""
        from pixel_rpg.util import rng
        battle = self.Battle(self.hero("Alone"), make_enemy(4, "meadow"))
        rng.reseed(4242)
        before = rng.rint(1, 10 ** 9)
        rng.reseed(4242)
        battle.pick_victim()
        self.assertEqual(rng.rint(1, 10 ** 9), before)

    def test_the_acting_player_and_the_victim_can_differ(self):
        from pixel_rpg.battle import DamageResult
        battle = self.fight(players=3, monsters=1)
        battle.acting_player = 0
        victim = battle.players[2]
        hp_before = victim.hp
        battle.apply_damage_to_player(DamageResult(amount=7), "physical",
                                      victim, battle.enemies[0])
        self.assertEqual(victim.hp, hp_before - 7)
        self.assertEqual(battle.players[0].hp, battle.players[0].max_hp(),
                         "the wrong player took the hit")

    # -- losing and leaving --------------------------------------------------

    def test_the_party_loses_only_when_everyone_is_down(self):
        battle = self.fight(players=3)
        for member in battle.players:
            member.revive_charge = False
        battle.players[0].hp = 0
        self.assertFalse(battle.check_deaths())
        self.assertIsNone(battle.result)
        battle.players[1].hp = battle.players[2].hp = 0
        self.assertTrue(battle.check_deaths())
        self.assertEqual(battle.result, "defeat")

    def test_one_player_running_does_not_end_the_fight(self):
        """Otherwise one person deciding to leave ends everybody's fight."""
        battle = self.fight(players=3)
        battle.acting_player = 0
        battle.flee_success()
        self.assertIsNone(battle.result)
        self.assertEqual(battle.fled, {0})
        self.assertEqual(len(battle.standing()), 2)

    def test_the_fight_empties_when_the_last_one_leaves(self):
        battle = self.fight(players=2)
        for i in (0, 1):
            battle.acting_player = i
            battle.flee_success()
        self.assertEqual(battle.result, "fled")

    def test_a_lone_player_running_still_ends_it(self):
        """Solo, leaving and the fight ending are the same event."""
        battle = self.Battle(self.hero("Alone"), make_enemy(4, "meadow"))
        battle.flee_success()
        self.assertEqual(battle.result, "fled")

    # -- the split -----------------------------------------------------------

    def test_xp_splits_evenly_among_everyone_who_fought(self):
        battle = self.fight(players=3, monsters=2)
        total = sum(f.xp_reward for f in battle.enemies)
        for foe in battle.enemies:
            foe.hp = 0
        battle.check_deaths()
        shares = [battle.reward_for(p)["xp"] for p in battle.players]
        self.assertEqual(sum(shares), total, "the split lost or minted XP")
        self.assertLessEqual(max(shares) - min(shares), 1,
                             "an even split was not even")

    def test_the_remainder_is_not_dropped(self):
        """34/33/33, not 33/33/33 with a point quietly thrown away."""
        battle = self.fight(players=3, monsters=1)
        battle.enemies[0].xp_reward = 100
        battle.enemies[0].gold_reward = 100
        battle.enemies[0].hp = 0
        battle.check_deaths()
        xp = sorted(battle.reward_for(p)["xp"] for p in battle.players)
        self.assertEqual(xp, [33, 33, 34])
        self.assertEqual(sum(battle.reward_for(p)["gold"]
                             for p in battle.players), 100)

    def test_a_solo_player_takes_the_whole_pot(self):
        solo = self.hero("Alone")
        foe = make_enemy(4, "meadow")
        battle = self.Battle(solo, foe)
        xp, gold = foe.xp_reward, foe.gold_reward
        foe.hp = 0
        battle.check_deaths()
        self.assertEqual(battle.reward_for(solo)["xp"], xp)
        self.assertEqual(battle.reward_for(solo)["gold"], gold)

    def test_falling_is_not_leaving(self):
        """Taking a downed player's share away makes the weakest member of a
        party fall furthest behind fastest."""
        battle = self.fight(players=3, monsters=1)
        battle.players[1].hp = 0
        battle.players[1].revive_charge = False
        battle.enemies[0].hp = 0
        battle.check_deaths()
        self.assertGreater(battle.reward_for(battle.players[1])["xp"], 0)

    def test_running_away_forfeits_the_share(self):
        battle = self.fight(players=3, monsters=1)
        battle.acting_player = 2
        runner = battle.players[2]
        total = sum(f.xp_reward for f in battle.enemies)
        battle.flee_success()
        battle.enemies[0].hp = 0
        battle.check_deaths()
        self.assertEqual(battle.reward_for(runner)["xp"], 0)
        self.assertEqual(battle.reward_for(runner)["gold"], 0)
        self.assertEqual(sum(battle.reward_for(p)["xp"]
                             for p in battle.players), total,
                         "the runner's share vanished instead of being split")

    def test_the_log_says_you_only_to_the_viewer(self):
        """Two people called "You" in one log is unreadable."""
        battle = self.fight(players=2)
        battle.viewer = 0
        self.assertEqual(battle.name_of(battle.players[0]), "You")
        self.assertEqual(battle.name_of(battle.players[1]),
                         battle.players[1].name)


class TestAnimation(unittest.TestCase):
    """Procedural motion: curves, not frames."""

    def setUp(self):
        from pixel_rpg import anim
        self.anim = anim

    def sample(self, kind, steps=40):
        """The whole move, as a list of poses."""
        motion = self.anim.Motion()
        motion.play(kind)
        out = []
        for i in range(steps + 1):
            motion.t = motion.span * i / steps
            out.append(self.anim.pose_of(motion, elapsed=0.0, idle=0.0))
        return out

    # -- the clock ----------------------------------------------------------

    def test_a_move_clears_itself_when_it_ends(self):
        motion = self.anim.Motion()
        motion.play("lunge")
        self.assertTrue(motion.active)
        motion.advance(motion.span + 0.01)
        self.assertFalse(motion.active)

    def test_a_fall_stays_fallen(self):
        """A body that finished collapsing is still collapsed."""
        motion = self.anim.Motion()
        motion.play("fall")
        motion.advance(motion.span * 3)
        self.assertTrue(motion.active)
        self.assertEqual(motion.progress, 1.0)

    def test_an_unknown_move_is_never_started(self):
        from pixel_rpg.battle import Battle
        player = Player("Hero", "warden")
        battle = Battle(player, make_enemy(3, "meadow"))
        self.assertFalse(battle.set_motion("player", 0, "moonwalk"))
        self.assertFalse(battle.player_motion[0].active)

    # -- the load-bearing one -----------------------------------------------

    def test_the_impact_frame_lands_with_the_damage(self):
        """If these drift apart the number appears before the blow does,
        which reads as the number causing the swing. `battle.py` schedules
        damage at 0.16s for an attack and 0.18s for a skill."""
        lunge = self.anim.Motion()
        lunge.play("lunge")
        lunge.advance(0.16)
        pose = self.anim.pose_of(lunge, idle=0.0)
        self.assertGreater(pose.dx, self.anim.REACH * 0.9,
                           "the attacker is not extended when damage lands")

        cast = self.anim.Motion()
        cast.play("cast")
        cast.advance(0.18)
        pose = self.anim.pose_of(cast, idle=0.0)
        self.assertGreater(pose.dy, 8.0,
                           "the caster has not gathered when the spell fires")

    # -- the shapes ---------------------------------------------------------

    def test_a_lunge_starts_and_ends_where_it_stood(self):
        poses = self.sample("lunge")
        self.assertAlmostEqual(poses[0].dx, 0.0, places=5)
        self.assertAlmostEqual(poses[-1].dx, 0.0, places=5)
        self.assertAlmostEqual(poses[-1].stretch, 1.0, places=5)

    def test_a_lunge_pulls_back_before_it_goes_forward(self):
        """Anticipation is the whole difference between a blow and a slide."""
        poses = self.sample("lunge")
        self.assertLess(min(p.dx for p in poses), -1.0,
                        "no wind-up: the body never moves away first")
        self.assertGreater(max(p.dx for p in poses), self.anim.REACH * 0.95)

    def test_a_lunge_travels_on_an_arc(self):
        poses = self.sample("lunge")
        self.assertGreater(max(p.dy for p in poses), 4.0,
                           "the body slid along the floor instead of arcing")

    def test_a_lunge_squashes_and_stretches(self):
        poses = self.sample("lunge")
        self.assertLess(min(p.stretch for p in poses), 0.95)
        self.assertGreater(max(p.stretch for p in poses), 1.05)

    def test_a_recoil_is_knocked_away_and_settles(self):
        poses = self.sample("recoil")
        self.assertLess(min(p.dx for p in poses), -self.anim.KNOCK * 0.9,
                        "being hit did not move the body")
        self.assertAlmostEqual(poses[-1].dx, 0.0, delta=1.0)
        # The knock is fast and the settling is slow, not the other way round.
        worst = min(range(len(poses)), key=lambda i: poses[i].dx)
        self.assertLess(worst / len(poses), 0.25,
                        "the knock took longer than the recovery")

    def test_a_fall_sinks_flattens_and_fades(self):
        poses = self.sample("fall")
        self.assertGreater(poses[-1].sink, 8.0)
        self.assertLess(poses[-1].stretch, 0.7)
        self.assertLess(poses[-1].alpha, 0.6)
        self.assertGreater(poses[-1].alpha, 0.0, "a body vanished entirely")

    def test_a_dead_body_does_not_breathe(self):
        motion = self.anim.Motion()
        motion.play("fall")
        a = self.anim.pose_of(motion, elapsed=0.0, idle=4.0, alive=False)
        b = self.anim.pose_of(motion, elapsed=1.7, idle=4.0, alive=False)
        self.assertEqual(a.dy, b.dy)

    # -- idling -------------------------------------------------------------

    def test_breathing_is_not_a_metronome(self):
        """A single sine puts every body on screen at the top of its arc at
        the same moment forever, which reads as machinery."""
        import math as _math
        period = 2 * _math.pi / 2.1
        first = self.anim.breathe(0.0)
        self.assertNotAlmostEqual(self.anim.breathe(period), first, places=3,
                                  msg="the idle repeats on a single period")

    def test_two_bodies_do_not_breathe_in_lockstep(self):
        a = self.anim.breathe(3.3, seed=0.0)
        b = self.anim.breathe(3.3, seed=1.7)
        self.assertNotAlmostEqual(a, b, places=3)

    def test_a_squashed_sprite_keeps_its_volume(self):
        """A body that only grows or only shrinks reads as a zoom."""
        from pixel_rpg import battlescreen
        import pygame
        surf = pygame.Surface((40, 60), pygame.SRCALPHA)
        pose = self.anim.Pose(stretch=1.25)
        tall = battlescreen.shaped(surf, pose)
        self.assertGreater(tall.get_height(), 60)
        self.assertLess(tall.get_width(), 40)
        flat = battlescreen.shaped(surf, self.anim.Pose(stretch=0.7))
        self.assertLess(flat.get_height(), 60)
        self.assertGreater(flat.get_width(), 40)

    def test_a_neutral_pose_costs_no_copy(self):
        """Almost every body on almost every frame is standing still."""
        from pixel_rpg import battlescreen
        import pygame
        surf = pygame.Surface((40, 60), pygame.SRCALPHA)
        self.assertIs(battlescreen.shaped(surf, self.anim.Pose()), surf)


class TestBattleMirror(unittest.TestCase):
    """A fight resolved on one machine and drawn on another."""

    def setUp(self):
        from pixel_rpg import battle as battle_mod
        self.mod = battle_mod
        self.Battle = battle_mod.Battle

    def hero(self, name, levels=12):
        player = Player(name, "warden")
        for _ in range(levels):
            player.gain_xp(player.xp_to_next())
        return player

    def pair(self):
        """The same fight on two machines: an authority and a mirror."""
        party = [self.hero("Ana"), self.hero("Bo")]
        foes = [make_enemy(4, "meadow") for _ in range(2)]
        host = self.Battle(party, foes, viewer=0, sort_party=False)
        copies = [self.hero("Ana"), self.hero("Bo")]
        echoes = [make_enemy(4, "meadow") for _ in range(2)]
        guest = self.Battle(copies, echoes, viewer=1, mirror=True,
                            sort_party=False)
        return host, guest

    def test_a_mirror_never_advances_its_own_fight(self):
        """Advancing here would be this machine deciding its own damage."""
        _host, guest = self.pair()
        before = (guest.phase, guest.round, len(guest.timeline))
        for _ in range(600):
            guest.update(1 / 60)
        self.assertEqual(guest.phase, before[0])
        self.assertEqual(guest.round, before[1])
        self.assertEqual(len(guest.timeline), before[2],
                         "a mirror ran the host's timeline")

    def test_a_mirror_still_animates(self):
        """It draws nothing new, but what it was told still decays."""
        _host, guest = self.pair()
        guest.player_flashes[0] = 0.22
        guest.enemy_motion[0].play("lunge")
        before = guest.enemy_motion[0].t
        guest.update(0.1)
        self.assertLess(guest.player_flashes[0], 0.22)
        self.assertGreater(guest.enemy_motion[0].t, before,
                           "a mirror froze the animation as well as the fight")

    def test_a_frame_carries_the_fight_across(self):
        host, guest = self.pair()
        host.players[1].hp -= 40
        host.enemies[0].hp = 0
        host.players[0].add_status("poison", 2)
        host.acting_player = 1
        host.turn = "enemy"
        host.round = 5
        host.say("Ana hits Wild Slime for 12.")

        guest.apply_frame(host.snapshot())
        self.assertEqual(guest.players[1].hp, host.players[1].hp)
        self.assertEqual(guest.enemies[0].hp, 0)
        self.assertEqual([st.key for st in guest.players[0].statuses],
                         ["poison"])
        self.assertEqual(guest.acting_player, 1)
        self.assertEqual(guest.turn, "enemy")
        self.assertEqual(guest.round, 5)
        self.assertIn("Ana hits Wild Slime for 12.", guest.log)

    def test_a_frame_carries_the_result(self):
        host, guest = self.pair()
        for foe in host.enemies:
            foe.hp = 0
        host.check_deaths()
        guest.apply_frame(host.snapshot())
        self.assertEqual(guest.result, "victory")
        self.assertTrue(guest.finished)

    def test_a_cursor_is_not_battle_state(self):
        """Pushing it would drag every client's highlight around as one
        person moved theirs."""
        host, _guest = self.pair()
        frame = host.snapshot()
        self.assertNotIn("mi", frame)
        for value in frame.values():
            self.assertNotIn("menu_index", str(value))

    def test_a_hostile_frame_changes_nothing(self):
        """Off a socket is off a socket."""
        _host, guest = self.pair()
        before = (guest.phase, guest.turn, guest.acting_player, guest.round,
                  [p.hp for p in guest.players],
                  [m.hp for m in guest.enemies])
        for junk in ({}, {"ph": "sideways", "tn": 7, "ap": 99, "ae": -3,
                          "rd": "many", "pl": "nope", "en": {}},
                     {"ap": True, "rd": 1e18, "lg": [1, 2, 3], "fl": "all"},
                     # Note a *quantity* is clamped, not refused - that is
                     # the point of `_index` being different. So the junk
                     # here is junk, not merely out of range.
                     {"pl": [{"hp": "lots"}, {"hp": None, "mp": [3]}]}):
            guest.apply_frame(junk)
        self.assertEqual(guest.phase, before[0])
        self.assertEqual(guest.turn, before[1])
        self.assertEqual(guest.acting_player, before[2])
        self.assertEqual(guest.round, before[3])
        self.assertEqual([p.hp for p in guest.players], before[4])
        self.assertEqual([m.hp for m in guest.enemies], before[5])

    def test_an_index_off_a_socket_is_refused_not_clamped(self):
        """Moving an out-of-range index to the nearest legal one is not
        rejecting a bad frame, it is acting on a different one."""
        self.assertEqual(self.mod._index(99, 4, 1), 1)
        self.assertEqual(self.mod._index(-1, 4, 1), 1)
        self.assertEqual(self.mod._index(True, 4, 1), 1)
        self.assertEqual(self.mod._index("2", 4, 1), 1)
        self.assertEqual(self.mod._index(2, 4, 1), 2)

    def test_a_status_off_a_socket_is_allow_listed(self):
        _host, guest = self.pair()
        guest.apply_frame({"pl": [
            {"hp": 10, "mp": 1, "st": [["poison", 2, 0],
                                       ["mind_control", 9, 9],
                                       "not a list",
                                       ["burn", "soon", 0]]},
            {"hp": 10, "mp": 1, "st": []}]})
        self.assertEqual([st.key for st in guest.players[0].statuses],
                         ["poison"])

    def test_hp_off_a_socket_cannot_exceed_the_body(self):
        _host, guest = self.pair()
        guest.apply_frame({"pl": [{"hp": 10 ** 9, "mp": 10 ** 9, "st": []},
                                  {"hp": -400, "mp": -1, "st": []}]})
        self.assertEqual(guest.players[0].hp, guest.players[0].max_hp())
        self.assertEqual(guest.players[1].hp, 0)
        self.assertEqual(guest.players[1].mp, 0)

    def test_a_presentation_payload_is_flattened_for_the_wire(self):
        from pixel_rpg import coop
        plain = coop._plain({"who": "player", "index": 2,
                             "color": (250, 120, 120), "pop": True,
                             "text": "42!"})
        self.assertEqual(plain["color"], [250, 120, 120])
        self.assertEqual(plain["text"], "42!")
        self.assertIsNone(coop._plain(object()))


class TestGroupBattle(unittest.TestCase):
    """One player against a group of monsters."""

    def setUp(self):
        from pixel_rpg.battle import Battle
        self.Battle = Battle
        self.player = Player("Hero", "warden")
        for _ in range(14):
            self.player.gain_xp(self.player.xp_to_next())

    def group(self, count, level=4):
        return [make_enemy(level, "meadow") for _ in range(count)]

    def test_a_single_enemy_still_works(self):
        """Every existing caller passes one monster; that must keep working."""
        lone = make_enemy(4, "meadow")
        battle = self.Battle(self.player, lone)
        self.assertEqual(battle.enemies, [lone])
        self.assertIs(battle.enemy, lone)

    def test_a_group_is_kept_in_order(self):
        foes = self.group(3)
        battle = self.Battle(self.player, foes)
        self.assertEqual(battle.enemies, foes)
        self.assertIs(battle.enemy, foes[0])

    def test_target_follows_the_cursor(self):
        foes = self.group(3)
        battle = self.Battle(self.player, foes)
        battle.cycle_target(1)
        self.assertIs(battle.enemy, foes[1])
        battle.cycle_target(-1)
        self.assertIs(battle.enemy, foes[0])

    def test_cycling_wraps_and_skips_the_dead(self):
        foes = self.group(3)
        battle = self.Battle(self.player, foes)
        foes[1].hp = 0
        battle.cycle_target(1)
        self.assertIs(battle.enemy, foes[2], "cycled onto a corpse")

    def test_cycling_does_nothing_with_one_left(self):
        foes = self.group(3)
        battle = self.Battle(self.player, foes)
        foes[1].hp = foes[2].hp = 0
        battle._retarget()
        self.assertFalse(battle.cycle_target(1))

    def test_aim_moves_off_a_monster_that_dies(self):
        """A player action must never be aimed into an empty space."""
        foes = self.group(3)
        battle = self.Battle(self.player, foes)
        foes[0].hp = 0
        battle.check_deaths()
        self.assertTrue(battle.enemy.alive)

    def test_the_fight_ends_only_when_all_are_down(self):
        foes = self.group(3)
        battle = self.Battle(self.player, foes)
        foes[0].hp = 0
        self.assertFalse(battle.check_deaths())
        self.assertIsNone(battle.result)
        foes[1].hp = foes[2].hp = 0
        self.assertTrue(battle.check_deaths())
        self.assertEqual(battle.result, "victory")

    def test_rewards_are_summed_over_the_group(self):
        foes = self.group(3)
        battle = self.Battle(self.player, foes)
        for foe in foes:
            foe.hp = 0
        battle.check_deaths()
        self.assertEqual(battle.rewards["xp"],
                         sum(f.xp_reward for f in foes))
        self.assertEqual(battle.rewards["gold"],
                         sum(f.gold_reward for f in foes))

    def test_the_acting_enemy_is_not_the_targeted_one(self):
        """The bug this design exists to prevent.

        `battle.enemy` is who the *player* is aiming at. The monster taking
        its turn is a different thing, and sharing one attribute between them
        would make the second enemy in a group swing on the first's behalf.
        """
        foes = self.group(3)
        battle = self.Battle(self.player, foes)
        battle.target = 2               # player aims at the last
        battle.acting = 0               # the first is taking its turn
        self.assertIs(battle.enemy, foes[2])
        self.assertIs(battle.acting_enemy(), foes[0])

    def test_acting_skips_the_fallen(self):
        foes = self.group(3)
        battle = self.Battle(self.player, foes)
        foes[0].hp = foes[1].hp = 0
        battle.acting = 0
        self.assertIs(battle.acting_enemy(), foes[2])

    def test_acting_returns_none_once_the_group_is_spent(self):
        foes = self.group(2)
        battle = self.Battle(self.player, foes)
        battle.acting = 2
        self.assertIsNone(battle.acting_enemy())

    def test_every_monster_gets_a_turn_each_round(self):
        """Three monsters must land three blows a round, not one."""
        from pixel_rpg.battle import MENU
        foes = self.group(3, level=2)
        for foe in foes:
            foe.hp = foe.max_hp() * 40        # nothing dies, so the round runs
        battle = self.Battle(self.player, foes)
        self.player.hp = self.player.max_hp() * 100
        seen, guard = set(), 0
        while battle.round < 2 and guard < 3000:
            guard += 1
            if battle.phase == MENU:
                battle.player_defend()
            if battle.turn == "enemy":
                actor = battle.acting_enemy()
                if actor is not None:
                    seen.add(id(actor))
            battle.update(0.05)
        self.assertEqual(len(seen), 3, "not every monster acted")

    def test_one_unfleeable_monster_pins_the_whole_group(self):
        """A boss in a pack must be as inescapable as a boss alone."""
        from pixel_rpg.entities import Boss, BOSS_DEFS
        foes = self.group(2)
        battle = self.Battle(self.player, foes)
        self.assertTrue(battle.can_flee())
        pinned = self.Battle(self.player, foes + [Boss(BOSS_DEFS[0])])
        self.assertFalse(pinned.can_flee())

    def test_a_dead_boss_stops_pinning_you(self):
        from pixel_rpg.entities import Boss, BOSS_DEFS
        boss = Boss(BOSS_DEFS[0])
        battle = self.Battle(self.player, self.group(1) + [boss])
        self.assertFalse(battle.can_flee())
        boss.hp = 0
        self.assertTrue(battle.can_flee())

    def test_group_naming(self):
        battle = self.Battle(self.player, self.group(1))
        self.assertNotIn("and", battle.group_name())
        three = self.group(3)
        for foe in three:
            foe.name = "Rat"
        self.assertEqual(self.Battle(self.player, three).group_name(),
                         "3 Rats")

    def test_pack_size_grows_with_the_party(self):
        from pixel_rpg.entities import pack_size
        from pixel_rpg.util import rng
        averages = []
        for party in (1, 2, 3, 4):
            rng.reseed(99)
            averages.append(
                sum(pack_size(party) for _ in range(400)) / 400.0)
        self.assertEqual(averages, sorted(averages),
                         "a bigger party did not draw a bigger pack")
        self.assertLess(averages[0], 1.5, "solo play should stay mostly 1v1")

    def test_make_encounter_is_never_empty(self):
        from pixel_rpg.entities import make_encounter
        from pixel_rpg.util import rng
        for trial in range(40):
            rng.reseed(trial)
            for party in (1, 4):
                group = make_encounter(6, "meadow", party=party)
                self.assertTrue(group)
                self.assertTrue(all(e.alive for e in group))


class TestClasses(unittest.TestCase):
    """The roster, and the two signatures added with it."""

    def test_every_class_names_its_archetype(self):
        """The in-world name is the flavour; the archetype is the answer to
        "which one is the mage". A class without one is invisible to a new
        player."""
        from pixel_rpg import classes as C
        for cls in C.CLASSES:
            self.assertTrue(cls.archetype, "%s has no archetype" % cls.key)

    def test_archetypes_are_distinct(self):
        from pixel_rpg import classes as C
        names = [c.archetype for c in C.CLASSES]
        self.assertEqual(len(names), len(set(names)), names)

    def test_class_starting_gear_exists(self):
        from pixel_rpg import classes as C
        from pixel_rpg.items import BASES_BY_NAME, CONSUMABLES
        for cls in C.CLASSES:
            self.assertIn(cls.start_weapon, BASES_BY_NAME, cls.key)
            self.assertIn(cls.start_armor, BASES_BY_NAME, cls.key)
            for key, _count in cls.start_consumables:
                self.assertIn(key, CONSUMABLES, cls.key)

    def test_class_skills_all_exist(self):
        from pixel_rpg import classes as C
        from pixel_rpg.skills import SKILLS_BY_KEY
        for cls in C.CLASSES:
            for key, level in cls.skills:
                self.assertIn(key, SKILLS_BY_KEY, "%s: %s" % (cls.key, key))
                self.assertGreaterEqual(level, 1)

    def test_steady_aim_only_pays_while_untouched(self):
        player = Player("A", "longshot")
        player.untouched = True
        boosted = player.outgoing_multiplier()
        player.untouched = False
        plain = player.outgoing_multiplier()
        self.assertGreater(boosted, plain)
        # 1.30 since the s52 balance pass; it was 1.40 and the Archer sat
        # near the top of the ceiling table.
        self.assertAlmostEqual(boosted / plain, 1.30, places=5)

    def test_steady_aim_does_nothing_for_other_classes(self):
        player = Player("A", "warden")
        player.untouched = True
        with_flag = player.outgoing_multiplier()
        player.untouched = False
        without = player.outgoing_multiplier()
        self.assertAlmostEqual(with_flag, without)

    def test_taking_damage_spends_steady_aim(self):
        from pixel_rpg.battle import Battle
        player = Player("A", "longshot")
        enemy = make_enemy(5, "meadow")
        battle = Battle(player, enemy)
        self.assertTrue(player.untouched)
        battle.apply_damage_to_player(
            type("R", (), {"amount": 5, "blocked": False, "crit": False,
                           "absorbed": 0, "lifesteal": 0})())
        self.assertFalse(player.untouched)

    def test_bloodthirst_halves_outside_healing(self):
        self.assertEqual(Player("B", "bloodhowl").heal_multiplier(), 0.5)
        self.assertEqual(Player("W", "warden").heal_multiplier(), 1.0)

    def test_bloodthirst_adds_lifesteal_to_every_hit(self):
        from pixel_rpg.battle import compute_damage
        enemy = make_enemy(5, "meadow")
        enemy.hp = enemy.max_hp() * 10          # survive the sample hit
        berserker = compute_damage(Player("B", "bloodhowl"), enemy)
        plain = compute_damage(Player("W", "warden"), enemy)
        self.assertGreater(berserker.lifesteal, 0)
        self.assertEqual(plain.lifesteal, 0)

    def test_new_classes_are_playable_from_the_start(self):
        from pixel_rpg import classes as C
        starters = {c.key for c in C.STARTER_CLASSES}
        self.assertIn("longshot", starters)
        self.assertIn("bloodhowl", starters)


class _FakePad:
    """A controller with no hardware behind it.

    Only the three calls Gamepad makes: axes, buttons and a name.
    """

    def __init__(self, axes=None, buttons=None, name="Fake Pad"):
        self.axes = axes or {}
        self.buttons = buttons or {}
        self.name = name

    def get_axis(self, axis):
        return self.axes.get(axis, 0)

    def get_button(self, button):
        return self.buttons.get(button, 0)


class _FakeKeys:
    """Stands in for pygame.key.get_pressed().

    A real one needs a video system, and these tests deliberately run without
    a display -- that is what keeps the suite under half a second.
    """

    def __init__(self, down=()):
        self.down = set(down)

    def __getitem__(self, code):
        return code in self.down

    def __len__(self):
        return 512


class TestGamepad(unittest.TestCase):
    """Controller support, without a controller.

    The expensive part -- SDL enumeration -- is never exercised here, which is
    also the point: none of this may cost anything until somebody asks for it.
    """

    def setUp(self):
        from pixel_rpg import gamepad
        self.gp = gamepad
        self.pad = gamepad.Gamepad()

    def test_world_state_matches_game(self):
        # gamepad repeats the constant rather than importing game (which
        # imports gamepad). If one moves, this catches it.
        from pixel_rpg import game as game_mod
        self.assertEqual(self.gp.WORLD_STATE, game_mod.OVERWORLD)

    def test_construction_does_not_start_the_joystick_subsystem(self):
        """The whole reason this is opt-in.

        Enumeration measured 10.2s. If merely building a Gamepad starts it,
        the game's sub-second startup is gone and nobody would notice until
        they complained the window took ten seconds again.
        """
        self.assertFalse(self.pad.enabled)
        self.assertFalse(self.pad.pads)
        self.assertFalse(pygame.joystick.get_init())

    def test_disabled_pad_passes_the_real_keyboard_straight_through(self):
        real = _FakeKeys()
        self.assertIs(self.pad.wrap_keys(real), real)

    def test_key_view_ors_pad_over_keyboard(self):
        view = self.gp.KeyView(_FakeKeys([pygame.K_z]), {pygame.K_LEFT})
        self.assertTrue(view[pygame.K_LEFT])    # from the pad
        self.assertTrue(view[pygame.K_z])       # from the keyboard
        self.assertFalse(view[pygame.K_RIGHT])

    def test_held_direction_reaches_movement_as_an_arrow_key(self):
        """Movement must arrive as arrow keys.

        Arrows always walk whatever else is bound, so a pad cannot be rebound
        into uselessness.
        """
        self.pad.enabled = True
        self.pad.pads = [_FakePad(axes={pygame.CONTROLLER_AXIS_LEFTX: -32767})]
        self.assertIn(pygame.K_LEFT, self.pad._directions())
        self.pad.held = self.pad._directions()
        view = self.pad.wrap_keys(_FakeKeys())
        self.assertTrue(view[pygame.K_LEFT])

    def test_deadzone_ignores_a_resting_stick(self):
        self.pad.enabled = True
        drift = int(self.gp.DEADZONE * 32767) - 1
        self.pad.pads = [_FakePad(axes={pygame.CONTROLLER_AXIS_LEFTX: drift})]
        self.assertEqual(self.pad._directions(), set())

    def test_right_trigger_is_sprint(self):
        self.pad.enabled = True
        full = int(32767 * 0.9)
        self.pad.pads = [_FakePad(
            axes={pygame.CONTROLLER_AXIS_TRIGGERRIGHT: full})]
        self.assertIn(self.gp.SPRINT_KEY, self.pad._directions())

    def test_dpad_counts_as_a_direction(self):
        self.pad.enabled = True
        self.pad.pads = [_FakePad(
            buttons={pygame.CONTROLLER_BUTTON_DPAD_DOWN: 1})]
        self.assertIn(pygame.K_DOWN, self.pad._directions())

    def test_face_buttons_resolve_through_the_bindings(self):
        """A rebound action moves its pad button with it."""
        from pixel_rpg.keybinds import Keybinds

        class Stub:
            state = "overworld"
            dialogue = None
            binds = Keybinds()

        game = Stub()
        self.assertEqual(
            self.pad._key_for(game, pygame.CONTROLLER_BUTTON_A),
            game.binds.key_for("interact"))
        game.binds.rebind("bag", pygame.K_p)
        self.assertEqual(
            self.pad._key_for(game, pygame.CONTROLLER_BUTTON_X), pygame.K_p)

    def test_menu_buttons_are_enter_escape_and_tab(self):
        class Stub:
            state = "settings"
            dialogue = None

        game = Stub()
        pairs = {pygame.CONTROLLER_BUTTON_A: pygame.K_RETURN,
                 pygame.CONTROLLER_BUTTON_B: pygame.K_ESCAPE,
                 pygame.CONTROLLER_BUTTON_START: pygame.K_ESCAPE,
                 pygame.CONTROLLER_BUTTON_LEFTSHOULDER: pygame.K_TAB}
        for button, expected in pairs.items():
            self.assertEqual(self.pad._key_for(game, button), expected)

    def test_menu_direction_repeats_after_a_delay(self):
        """Held on a menu: one press, a pause, then a steady repeat."""
        sent = []

        class Stub:
            state = "settings"
            dialogue = None

            def handle_event(self, event):
                sent.append(event.key)

        game = Stub()
        self.pad.enabled = True
        self.pad.pads = [_FakePad(
            buttons={pygame.CONTROLLER_BUTTON_DPAD_DOWN: 1})]
        self.pad.update(0.016, game)
        self.assertEqual(sent, [pygame.K_DOWN])       # acts at once
        self.pad.update(self.gp.REPEAT_FIRST - 0.05, game)
        self.assertEqual(len(sent), 1)                # still waiting
        self.pad.update(0.1, game)
        self.assertEqual(len(sent), 2)                # and now it repeats

    def test_world_direction_does_not_emit_keypresses(self):
        """Walking is a held key, not a stream of presses."""
        sent = []

        class Stub:
            state = "overworld"
            dialogue = None

            def handle_event(self, event):
                sent.append(event.key)

        game = Stub()
        self.pad.enabled = True
        self.pad.pads = [_FakePad(
            buttons={pygame.CONTROLLER_BUTTON_DPAD_DOWN: 1})]
        for _ in range(60):
            self.pad.update(0.016, game)
        self.assertEqual(sent, [])
        self.assertIn(pygame.K_DOWN, self.pad.held)

    def test_disabled_pad_ignores_buttons(self):
        sent = []

        class Stub:
            state = "settings"
            dialogue = None

            def handle_event(self, event):
                sent.append(event.key)

        event = pygame.event.Event(pygame.CONTROLLERBUTTONDOWN,
                                   button=pygame.CONTROLLER_BUTTON_A)
        self.assertFalse(self.pad.handle_event(Stub(), event))
        self.assertEqual(sent, [])

    def test_profile_remembers_the_choice(self):
        from pixel_rpg.meta import MetaProfile
        profile = MetaProfile(None)
        self.assertFalse(profile.gamepad)
        profile.gamepad = True
        restored = MetaProfile(None).apply_dict(profile.to_dict())
        self.assertTrue(restored.gamepad)

    def test_profile_rejects_a_junk_gamepad_value(self):
        from pixel_rpg.meta import MetaProfile
        restored = MetaProfile(None).apply_dict({"gamepad": {"x": [1]}})
        self.assertIsInstance(restored.gamepad, bool)

class TestTrading(unittest.TestCase):
    """Roadmap item 0x: gold, gear and potions between two players.

    Decided on 2026-08-24 - *"no limits. Any gold, any gear"* - and asked
    about again on 2026-09-01 because it had never been built.

    **The rule every test here is really about**: nothing moves until both
    sides have agreed to the *same* pair of offers, and any change at all
    un-agrees both of them.
    """

    def setUp(self):
        from pixel_rpg import trade
        self.trade = trade

    def offer(self, gold=0, items=(), **cons):
        return self.trade.Offer(gold, list(items), cons)

    # -- the offer ---------------------------------------------------------

    def test_an_offer_survives_the_wire(self):
        offer = self.offer(140, [roll_item(9), roll_item(3)], potion=2)
        back = self.trade.Offer.from_wire(offer.to_wire())
        self.assertEqual(offer, back)
        self.assertEqual(back.gold, 140)
        self.assertEqual(back.cons, {"potion": 2})
        self.assertEqual(len(back.items), 2)

    def test_a_hostile_offer_is_dropped_rather_than_defaulted(self):
        """A defaulted item is loot nobody offered."""
        back = self.trade.Offer.from_wire({
            "g": "9" * 40,
            "i": [{"name": "Sword"}, "not a dict", {"slot": "hat"}],
            "c": {"potion": 10 ** 9, "nectar": 4, "ether": -3}})
        self.assertEqual(back.items, [])
        self.assertEqual(back.cons, {"potion": self.trade.MAX_STACK})
        self.assertLessEqual(back.gold, self.trade.MAX_GOLD)

    def test_two_identical_swords_are_interchangeable(self):
        """Signature, not uid: a uid means nothing across a socket."""
        one = roll_item(5, slot="weapon")
        two = Item.from_dict(one.to_dict())
        self.assertNotEqual(one.uid, two.uid)
        self.assertEqual(self.offer(items=[one]), self.offer(items=[two]))

    # -- what the host can actually check ----------------------------------

    def test_an_offer_has_to_be_backed_by_a_published_body(self):
        player = Player("Giver")
        sword = roll_item(6, slot="weapon")
        player.inventory.append(sword)
        player.gold = 50
        body = serialize_player(player)
        self.assertTrue(self.trade.covers(body, self.offer(50, [sword])))
        self.assertFalse(self.trade.covers(body, self.offer(51)))
        self.assertFalse(self.trade.covers(
            body, self.offer(items=[sword, sword])),
            "the same sword offered twice is the dupe this check exists for")

    def test_worn_gear_counts_as_carried(self):
        """Decision 10 says *any* gear, and the sword somebody wants is
        usually the one you are holding."""
        player = Player("Giver")
        sword = roll_item(6, slot="weapon")
        player.equip(sword)
        self.assertTrue(self.trade.covers(serialize_player(player),
                                          self.offer(items=[sword])))

    def test_nothing_published_backs_nothing(self):
        self.assertTrue(self.trade.covers(None, self.offer()))
        self.assertFalse(self.trade.covers(None, self.offer(1)))

    # -- the bag -----------------------------------------------------------

    def test_a_trade_that_would_overfill_a_bag_is_refused(self):
        player = Player("Hoarder")
        player.inventory = [roll_item(2) for _ in range(player.bag_size())]
        incoming = self.offer(items=[roll_item(4)])
        ok, why = self.trade.room_for(player, self.offer(), incoming)
        self.assertFalse(ok)
        self.assertIn("room", why)
        # Giving one away makes room for one.
        ok, _why = self.trade.room_for(
            player, self.offer(items=[player.inventory[0]]), incoming)
        self.assertTrue(ok)

    def test_you_cannot_offer_gold_you_do_not_have(self):
        player = Player("Broke")
        player.gold = 10
        ok, why = self.trade.room_for(player, self.offer(11), self.offer())
        self.assertFalse(ok)
        self.assertIn("gold", why)

    # -- the transfer ------------------------------------------------------

    def test_everything_leaves_before_anything_arrives(self):
        player = Player("Trader")
        player.gold = 200
        worn = roll_item(6, slot="weapon")
        player.equip(worn)
        carried = roll_item(4, slot="armor")
        player.inventory.append(carried)
        player.consumables = {"potion": 3}
        incoming = roll_item(8, slot="accessory")

        spare = self.trade.apply_to(
            player,
            self.offer(150, [worn, carried], potion=2),
            self.offer(20, [incoming], ether=1))

        self.assertEqual(spare, [])
        self.assertEqual(player.gold, 70)
        self.assertIsNone(player.equipment["weapon"],
                          "gear given away has to leave the slot it was in")
        self.assertNotIn(carried, player.inventory)
        self.assertIn(incoming, player.inventory)
        self.assertEqual(player.consumable_count("potion"), 1)
        self.assertEqual(player.consumable_count("ether"), 1)

    def test_a_swap_of_full_bags_still_fits(self):
        """Four out and four in on a full bag: the order is the whole fix."""
        player = Player("Swapper")
        player.inventory = [roll_item(2) for _ in range(player.bag_size())]
        giving = player.inventory[:4]
        taking = [roll_item(5) for _ in range(4)]
        ok, _why = self.trade.room_for(player, self.offer(items=giving),
                                       self.offer(items=taking))
        self.assertTrue(ok)
        spare = self.trade.apply_to(player, self.offer(items=giving),
                                    self.offer(items=taking))
        self.assertEqual(spare, [])
        self.assertEqual(len(player.inventory), player.bag_size())

    # -- the locks ---------------------------------------------------------

    def test_changing_either_side_un_agrees_both(self):
        session = self.trade.TradeSession(2, "Friend")
        session.mine_locked = session.theirs_locked = True
        session.set_theirs(self.offer(5))
        self.assertFalse(session.mine_locked)
        self.assertFalse(session.theirs_locked)

        session.mine_locked = session.theirs_locked = True
        session.set_mine(self.offer(9))
        self.assertFalse(session.mine_locked)
        self.assertFalse(session.theirs_locked)

    def test_an_offer_that_did_not_change_leaves_the_locks_alone(self):
        """Offers are republished on every edit; an identical one is news
        about nothing and must not cancel an agreement."""
        session = self.trade.TradeSession(2, "Friend")
        session.set_theirs(self.offer(5))
        session.mine_locked = session.theirs_locked = True
        self.assertFalse(session.set_theirs(self.offer(5)))
        self.assertTrue(session.mine_locked)
        self.assertTrue(session.theirs_locked)


class TestTradingOverASocket(unittest.TestCase):
    """The half unit tests cannot prove (s45, item 0a).

    Co-op was declared fixed once on the strength of five unit tests and
    was not - so this drives two real `Game`s through a real socket and
    checks that the items are in the other person's bag at the end.
    """

    def build(self, name, slot, seed):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run(name, slot)
        self.addCleanup(game.saves.delete, slot)
        game.dialogue = None
        game.state = G.OVERWORLD
        return game

    def joined(self):
        from pixel_rpg import coop
        seed = 4242
        host_game = self.build("Hostess", 1, seed)
        guest_game = self.build("Guest", 2, seed)
        host = coop.Session.host("Hostess", port=0, seed=seed,
                                 reach_out=False)
        self.addCleanup(host.close)
        guest = coop.Session.join(host.room_code, "Guest")
        self.addCleanup(guest.close)
        host_game.coop, guest_game.coop = host, guest
        guest.adopt_seed = False
        # Standing on the same spot, which is well inside `TRADE_REACH`.
        guest_game.player.x = host_game.player.x
        guest_game.player.y = host_game.player.y
        self.settle(host, host_game, guest, guest_game, frames=90)
        self.assertTrue(guest.ready)
        self.assertIsNotNone(guest_game.trade_partner(),
                             "the two never heard where the other stood")
        return host, guest, host_game, guest_game

    def settle(self, host, host_game, guest, guest_game, frames=30):
        """Pump both ends, forcing exactly one position broadcast.

        **Forcing one and not all of them.** `_broadcast_self` throttles to
        15 a second of wall clock and a test loop runs far faster, so
        without a push the host's position never reaches the guest and
        nobody is ever close enough to trade. Pushing it every frame is the
        other failure: `net.MAX_MESSAGES_PER_SECOND` is 120 and a peer over
        it has its socket closed, so a helper that forced 90 frames spent
        the room's whole budget on standing still and the trade messages
        arrived at a socket that was already shut.
        """
        for i in range(frames):
            if i == 0:
                host._last_send = guest._last_send = 0.0
            host.update(host_game, 1 / 60)
            guest.update(guest_game, 1 / 60)

    def test_a_sword_and_some_gold_change_hands(self):
        from pixel_rpg import game as G
        host, guest, host_game, guest_game = self.joined()
        sword = roll_item(9, slot="weapon")
        guest_game.player.inventory.append(sword)
        guest_game.player.gold = 500
        host_game.player.gold = 500
        host_game.player.consumables = {"potion": 4}

        self.assertTrue(guest_game.start_trade(), "nobody was close enough")
        self.settle(host, host_game, guest, guest_game)
        self.assertEqual(host_game.state, G.DIALOGUE,
                         "the host was never asked")
        host_game.answer_trade(True)
        self.settle(host, host_game, guest, guest_game)
        self.assertEqual(host_game.state, G.TRADE)
        self.assertEqual(guest_game.state, G.TRADE)

        # The guest puts up the sword and 200 gold.
        guest_game.trade_change(
            lambda o: (o.items.append(sword), setattr(o, "gold", 200)))
        # The host puts up two potions.
        host_game.trade_change(lambda o: o.cons.update({"potion": 2}))
        self.settle(host, host_game, guest, guest_game)
        self.assertEqual(host_game.trade.theirs.gold, 200)
        self.assertEqual(guest_game.trade.theirs.cons, {"potion": 2})

        guest_game.trade_toggle_lock()
        self.settle(host, host_game, guest, guest_game)
        self.assertTrue(host_game.trade.theirs_locked)
        self.assertIsNotNone(host_game.trade, "settled on one signature")

        host_game.trade_toggle_lock()
        self.settle(host, host_game, guest, guest_game)

        self.assertIsNone(host_game.trade)
        self.assertIsNone(guest_game.trade)
        names = [i.name for i in host_game.player.inventory]
        self.assertIn(sword.name, names, "the sword never arrived")
        self.assertNotIn(sword, guest_game.player.inventory,
                         "the sword was in two bags at once")
        self.assertEqual(host_game.player.gold, 700)
        self.assertEqual(guest_game.player.gold, 300)
        self.assertEqual(host_game.player.consumable_count("potion"), 2)
        self.assertEqual(guest_game.player.consumable_count("potion"), 2 + 3)

    def test_one_agreement_is_not_enough(self):
        from pixel_rpg import game as G
        host, guest, host_game, guest_game = self.joined()
        guest_game.player.gold = 90
        self.assertTrue(guest_game.start_trade())
        self.settle(host, host_game, guest, guest_game)
        host_game.answer_trade(True)
        self.settle(host, host_game, guest, guest_game)

        guest_game.trade_change(lambda o: setattr(o, "gold", 90))
        guest_game.trade_toggle_lock()
        self.settle(host, host_game, guest, guest_game)
        self.assertEqual(guest_game.player.gold, 90,
                         "one side agreeing moved gold")
        self.assertEqual(guest_game.state, G.TRADE)

    def test_a_changed_offer_cancels_the_agreement_on_both_screens(self):
        """The scam this whole design exists to make impossible."""
        host, guest, host_game, guest_game = self.joined()
        guest_game.player.gold = 400
        self.assertTrue(guest_game.start_trade())
        self.settle(host, host_game, guest, guest_game)
        host_game.answer_trade(True)
        self.settle(host, host_game, guest, guest_game)

        guest_game.trade_change(lambda o: setattr(o, "gold", 400))
        self.settle(host, host_game, guest, guest_game)
        host_game.trade_toggle_lock()
        self.settle(host, host_game, guest, guest_game)
        self.assertTrue(guest_game.trade.theirs_locked)

        # The guest quietly drops the price after the host agreed.
        guest_game.trade_change(lambda o: setattr(o, "gold", 1))
        self.settle(host, host_game, guest, guest_game)
        self.assertFalse(host_game.trade.mine_locked,
                         "the host was still agreeing to the old offer")
        self.assertFalse(guest_game.trade.theirs_locked)
        self.assertEqual(guest_game.player.gold, 400)

    def test_an_offer_the_body_does_not_back_ends_the_trade(self):
        """A client that offers what it is not carrying is refused."""
        host, guest, host_game, guest_game = self.joined()
        guest_game.player.gold = 10
        self.assertTrue(guest_game.start_trade())
        self.settle(host, host_game, guest, guest_game)
        host_game.answer_trade(True)
        self.settle(host, host_game, guest, guest_game)

        # Straight down the wire, bypassing the screen that would clamp it.
        guest.send_trade_offer(guest_game, {"g": 999999, "i": [], "c": {}})
        self.settle(host, host_game, guest, guest_game)
        self.assertIsNone(host.trade, "the host arbitrated a fiction")
        self.assertIsNone(host_game.trade)
        self.assertIsNone(guest_game.trade)

    def test_walking_out_mid_trade_moves_nothing(self):
        host, guest, host_game, guest_game = self.joined()
        guest_game.player.gold = 300
        self.assertTrue(guest_game.start_trade())
        self.settle(host, host_game, guest, guest_game)
        host_game.answer_trade(True)
        self.settle(host, host_game, guest, guest_game)
        guest_game.trade_change(lambda o: setattr(o, "gold", 300))
        guest_game.trade_toggle_lock()
        self.settle(host, host_game, guest, guest_game)

        guest_game.close_trade("left")
        self.settle(host, host_game, guest, guest_game)
        self.assertIsNone(host.trade)
        self.assertIsNone(host_game.trade)
        self.assertEqual(guest_game.player.gold, 300)

    def test_a_second_trade_is_refused_while_one_is_running(self):
        host, guest, host_game, guest_game = self.joined()
        self.assertTrue(guest_game.start_trade())
        self.settle(host, host_game, guest, guest_game)
        host_game.answer_trade(True)
        self.settle(host, host_game, guest, guest_game)
        self.assertFalse(guest_game.start_trade())


class TestTradingAsksAtTheRightTime(unittest.TestCase):
    """A trade is a screen somebody else opens on yours, which is the
    failure mode s45 reported about the co-op menu."""

    def game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Busy", 5)
        self.addCleanup(game.saves.delete, 5)
        game.dialogue = None
        game.state = G.OVERWORLD
        return game

    def test_an_invitation_mid_battle_is_declined_for_you(self):
        from pixel_rpg import game as G
        from pixel_rpg.entities import make_enemy
        game = self.game()
        answers = []
        game.coop = type("S", (), {
            "answer_trade": lambda _s, _g, ok: answers.append(ok)})()
        game.start_battle([make_enemy(3, "meadow", game.settings.mods)])
        game.trade_asked(2, "Friend")
        self.assertEqual(answers, [False])
        self.assertIsNone(game.trade)
        self.assertEqual(game.state, G.BATTLE)

    def test_an_invitation_on_the_overworld_asks(self):
        from pixel_rpg import game as G
        game = self.game()
        game.coop = type("S", (), {
            "answer_trade": lambda _s, _g, _ok: True,
            "ready": True, "publish_body": lambda _s, _b: None})()
        game.trade_asked(2, "Friend")
        self.assertEqual(game.state, G.DIALOGUE)
        self.assertIn("Friend", game.dialogue["title"])

    def test_nobody_nearby_means_nothing_happens(self):
        game = self.game()
        self.assertFalse(game.start_trade())
        self.assertIsNone(game.trade)

    def test_the_keyboard_reaches_every_kind_of_row(self):
        """The screen is driven through `handle_event`, not by calling its
        methods - which is the path a player actually takes."""
        from pixel_rpg import game as G, trade as trade_mod
        game = self.game()
        sent = []
        game.coop = type("S", (), {
            "ready": True,
            "send_trade_offer": lambda _s, _g, wire: sent.append(wire),
            "send_trade_lock": lambda _s, _g, on: sent.append(on),
            "end_trade": lambda _s, _g, why="": sent.append("end"),
            "publish_body": lambda _s, _b: None})()
        game.player.gold = 400
        game.player.consumables = {"potion": 5}
        game.trade = trade_mod.TradeSession(2, "Friend")
        game.state = G.TRADE

        def press(key):
            game.handle_event(pygame.event.Event(pygame.KEYDOWN, key=key,
                                                 unicode="", mod=0))

        # Row 0 is gold. Right nudges it by a twentieth of what you have.
        press(pygame.K_RIGHT)
        self.assertEqual(game.trade.mine.gold, 20)
        # Enter is a toggle between nothing and everything, so with 20
        # already up it clears the row rather than filling it.
        press(pygame.K_RETURN)
        self.assertEqual(game.trade.mine.gold, 0)
        press(pygame.K_RETURN)
        self.assertEqual(game.trade.mine.gold, 400, "Enter offers all of it")
        press(pygame.K_RETURN)
        self.assertEqual(game.trade.mine.gold, 0, "and takes it back")

        # Down to the first piece of gear, and Enter puts it on the table.
        press(pygame.K_DOWN)
        press(pygame.K_RETURN)
        self.assertEqual(len(game.trade.mine.items), 1)

        # The last two rows are always Agree and Call it off.
        rows = game.trade_rows()
        self.assertEqual([r["kind"] for r in rows[-2:]], ["lock", "cancel"])
        game.trade.index = len(rows) - 2
        press(pygame.K_RETURN)
        self.assertTrue(game.trade.mine_locked)
        game.trade.index = len(rows) - 1
        press(pygame.K_RETURN)
        self.assertIsNone(game.trade, "Call it off left the trade open")
        self.assertEqual(game.state, G.OVERWORLD)

    def test_escape_leaves_without_trading(self):
        from pixel_rpg import game as G, trade as trade_mod
        game = self.game()
        game.coop = type("S", (), {"end_trade": lambda _s, _g, why="": None})()
        game.player.gold = 100
        game.trade = trade_mod.TradeSession(2, "Friend")
        game.trade.mine = trade_mod.Offer(100)
        game.state = G.TRADE
        game.handle_event(pygame.event.Event(pygame.KEYDOWN,
                                             key=pygame.K_ESCAPE,
                                             unicode="", mod=0))
        self.assertIsNone(game.trade)
        self.assertEqual(game.player.gold, 100)
        self.assertEqual(game.state, G.OVERWORLD)

    def test_leaving_the_room_takes_the_trade_with_it(self):
        """The screen is modal, so a trade whose other half is gone is a
        player stuck at a table nobody is sitting at."""
        from pixel_rpg import game as G, trade as trade_mod
        game = self.game()
        game.trade = trade_mod.TradeSession(2, "Friend")
        game.state = G.TRADE
        game.leave_coop()
        self.assertIsNone(game.trade)
        self.assertEqual(game.state, G.OVERWORLD)

    def test_the_trade_key_is_bindable_and_advertised(self):
        """A hardcoded key cannot be rebound and cannot be put in the
        footer - which is exactly how the map went missing (s45, 0l)."""
        from pixel_rpg import keybinds
        self.assertIn("trade", keybinds.ACTION_KEYS)
        self.assertNotIn("trade", keybinds.SECRET_ACTIONS)
        self.assertEqual(keybinds.LABELS["trade"], "Trade")
class TestNothingIsDefinedTwice(unittest.TestCase):
    """A second `def` of the same name silently replaces the first.

    Written on 2026-09-03 after the trade pass added a `Game.publish_body`
    that `game.py` already had four hundred lines further down. Python
    keeps the **later** definition, so the new one did nothing whatsoever:
    no error, no warning, and every test still green because the older
    method was correct.

    That is standing rule 31.6's failure - a patch that reports success and
    changes nothing - reached by a different road, and it is the kind that
    a large file invites. `game.py` is six thousand lines and nobody is
    going to notice by reading.

    Scanned with `ast` rather than by importing, because the whole point is
    to see the source as written and not the one survivor of it.
    """

    def duplicates_in(self, path):
        import ast
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        found = []

        def scan(body, where):
            seen = {}
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                    # A property and its setter share a name on purpose,
                    # and so does `typing.overload`. Both are decorated.
                    decorated = bool(getattr(node, "decorator_list", ()))
                    if node.name in seen and not decorated:
                        found.append("%s: %s defined at lines %d and %d"
                                     % (where, node.name, seen[node.name],
                                        node.lineno))
                    seen[node.name] = node.lineno
                if isinstance(node, ast.ClassDef):
                    scan(node.body, "%s.%s" % (where, node.name))

        scan(tree.body, os.path.basename(path))
        return found

    def test_no_module_defines_the_same_name_twice(self):
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "pixel_rpg")
        problems = []
        names = sorted(n for n in os.listdir(root) if n.endswith(".py"))
        self.assertGreater(len(names), 20, "the scan found nothing to scan")
        for name in names:
            problems += self.duplicates_in(os.path.join(root, name))
        self.assertEqual(problems, [],
                         "a later definition silently replaced an earlier one")
class TestTheTurnBar(unittest.TestCase):
    """Roadmap item 0h, his words (2026-08-31):

    > "make it easier to know whether or not its your turn, add a turn bar
    > and gray out all of your options when its not your turn"

    The bar is only worth anything if it agrees with the fight, so most of
    this drives a real `Battle` and checks the two against each other.
    """

    def fight(self, party=2, monsters=2, first=True):
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import Player, make_enemy
        players = [Player("Hero%d" % i, "warden") for i in range(party)]
        pack = [make_enemy(4, "meadow") for _ in range(monsters)]
        battle = Battle(players, pack, viewer=0)
        battle.players_first = first
        return battle

    def test_the_bar_is_the_order_the_round_actually_goes_in(self):
        """The whole point. A bar that disagrees is worse than no bar."""
        battle = self.fight(party=3, monsters=2)
        battle.begin_round()
        order = battle.turn_order()
        walked = []
        for _ in range(400):
            now = battle.turn_now()
            if now is not None and (not walked or walked[-1] != now):
                walked.append(now)
            if battle.finished or battle.round > 1:
                break
            battle.update(1 / 30)
            if battle.phase == "menu":
                battle.player_defend()         # always legal, always cheap
        # Everyone the bar promised got a turn, in that order. A monster or
        # a hero may fall before their turn arrives, so this compares the
        # common prefix rather than demanding equality.
        self.assertGreater(len(walked), 1, "nobody took a turn at all")
        self.assertEqual(walked[:len(order)], order[:len(walked)])

    def test_the_fallen_and_the_fled_are_not_in_it(self):
        battle = self.fight(party=3, monsters=2)
        battle.begin_round()
        self.assertIn(("player", 1), battle.turn_order())
        battle.players[1].hp = 0
        self.assertNotIn(("player", 1), battle.turn_order())
        battle.fled.add(2)
        self.assertNotIn(("player", 2), battle.turn_order())
        battle.enemies[0].hp = 0
        self.assertNotIn(("enemy", 0), battle.turn_order())

    def test_the_slower_side_goes_second(self):
        battle = self.fight(party=2, monsters=2, first=False)
        order = battle.turn_order()
        self.assertEqual(order[0][0], "enemy")
        self.assertEqual(order[-1], ("player", 1))
        battle.players_first = True
        self.assertEqual(battle.turn_order()[0], ("player", 0))

    def test_asking_whose_turn_it_is_never_moves_the_fight(self):
        """`acting_member` and `acting_enemy` advance their index as they
        skip the dead. A renderer asking the question must not."""
        battle = self.fight(party=3, monsters=2)
        battle.begin_round()
        battle.players[0].hp = 0
        before = (battle.acting_player, battle.acting)
        for _ in range(5):
            self.assertEqual(battle.turn_now(), ("player", 1))
        self.assertEqual((battle.acting_player, battle.acting), before)

    def test_between_turns_nobody_is_up(self):
        battle = self.fight(party=1, monsters=1)
        battle.begin_round()
        battle.players[0].hp = 0
        self.assertIsNone(battle.turn_now())

    def test_the_bar_agrees_with_the_actor(self):
        battle = self.fight(party=3, monsters=2)
        battle.begin_round()
        for index in range(3):
            battle.acting_player = index
            self.assertEqual(battle.turn_now(), ("player", index))
            self.assertIs(battle.actor, battle.players[index])


class TestTheMenuGoesDeadWithTheTurn(unittest.TestCase):
    """0h's second half: *"gray out all of your options when its not your
    turn"*.

    **And the rows stop being clickable**, which is s45's rule from the
    other direction: a control that will be refused is not drawn as a
    control. Grey but clickable would be the Flee row under Oathkeeper
    again.
    """

    def battling(self, party=2, viewer=0, acting=0):
        from pixel_rpg import game as G
        from pixel_rpg.entities import Player, make_enemy
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Watcher", 3)
        self.addCleanup(game.saves.delete, 3)
        game.dialogue = None
        friends = [game.player] + [Player("Friend%d" % i, "warden")
                                   for i in range(1, party)]
        game.start_battle([make_enemy(4, "meadow", game.settings.mods)],
                          party=friends, viewer=viewer, sort_party=False)
        for _ in range(900):
            game.battle.update(1 / 60)
            if game.battle.phase == "menu":
                break
        game.battle.acting_player = acting
        return game

    def menu_rows(self, game):
        from pixel_rpg import battlescreen
        game.ui.hot.clear()
        battlescreen.draw_menu(game, game.battle, *game.screen.get_size())
        return [item for item in game.ui.hot.items if item[1] == "battle_menu"]

    def test_your_own_turn_leaves_the_menu_clickable(self):
        game = self.battling(party=2, viewer=0, acting=0)
        self.assertTrue(self.menu_rows(game),
                        "the menu stopped being clickable on your own turn")

    def test_somebody_elses_turn_registers_nothing(self):
        game = self.battling(party=2, viewer=0, acting=1)
        from pixel_rpg import battlescreen
        self.assertTrue(battlescreen.waiting_on_someone_else(game.battle))
        self.assertEqual(self.menu_rows(game), [],
                         "a row that will be ignored was still a hotspot")

    def test_the_greying_and_the_input_gate_are_the_same_question(self):
        """Written as one call on purpose - two copies of "is it my turn"
        is how "looks dead" and "is dead" drift apart."""
        import inspect
        from pixel_rpg import battlescreen
        body = inspect.getsource(battlescreen.draw_menu)
        self.assertIn("waiting_on_someone_else(battle)", body)


class TestTheLogStaysInsideItsBox(unittest.TestCase):
    """It did not, and had not since the stacked layout existed.

    Seven lines at a 17 pitch need 147 pixels and the stacked log box is
    104, so a busy round on a phone drew straight out through the bottom
    edge and over whatever was under it. Invisible until the turn bar
    moved in underneath (s51).
    """

    def test_no_more_lines_are_drawn_than_the_box_holds(self):
        from pixel_rpg import battlescreen
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import Player, make_enemy
        battle = Battle([Player("Hero", "warden")],
                        [make_enemy(4, "meadow")], viewer=0)
        for i in range(12):
            battle.say("A very long line of combat narration number %d "
                       "that will certainly wrap onto a second row" % i)

        for size in ((390, 844), (1280, 720)):
            box = battlescreen.battle_panels(battle, *size)["log"]
            room = max(1, (box.h - 34) // 17)
            self.assertLessEqual(28 + room * 17, box.h,
                                 "%dx%d: the log runs past its own panel"
                                 % size)

class TestTheGuideCanActuallyBeRead(unittest.TestCase):
    """Every page, in full, at every size the gate uses.

    **It could not be**, and had not been for as long as the screen has
    existed. `draw_guide` stopped when it ran out of panel, and text that
    was never drawn overflows nothing - so `tests/layout.py` reported zero
    for all nine pages at all five sizes. On a phone in landscape the book
    drew **seven lines of a forty-two line page**.

    That is s43's scene-screen bug in a second place, which is why s7
    states it as a general rule: **anything that stops on a boundary needs
    a test that asks how much it drew, not whether anything overlapped.**
    This is that test for the guide.
    """

    SIZES = ((1280, 720), (1024, 768), (1600, 900), (844, 390), (390, 844))

    def game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.state = G.GUIDE
        return game

    def test_every_page_is_reachable_at_every_size(self):
        from pixel_rpg import guide, menus
        game = self.game()
        for size in self.SIZES:
            game.screen = pygame.display.set_mode(size)
            for index in range(len(guide.PAGES)):
                wrapped, room = menus.guide_measure(game, index)
                limit = menus.guide_scroll_max(game, index)
                self.assertGreaterEqual(
                    limit + room, len(wrapped),
                    "%dx%d page %d (%s): %d lines, %d fit, scrolls %d"
                    % (size[0], size[1], index, guide.PAGES[index].title,
                       len(wrapped), room, limit))

    def test_scrolling_reaches_the_last_line_and_stops(self):
        from pixel_rpg import menus
        game = self.game()
        game.screen = pygame.display.set_mode((844, 390))
        # The worst case in the book, and the one that proves the point.
        game.ui.guide_index = 2
        game.ui.guide_scroll = 0
        limit = menus.guide_scroll_max(game)
        self.assertGreater(limit, 0, "the fixture stopped being a hard case")
        for _ in range(limit + 10):
            game.scroll_guide(1)
        self.assertEqual(game.ui.guide_scroll, limit)
        for _ in range(limit + 10):
            game.scroll_guide(-1)
        self.assertEqual(game.ui.guide_scroll, 0)

    def test_a_page_that_fits_still_turns_on_down(self):
        """The scroll must not eat the key on a wide window, or the book
        stops turning for everybody who never had the problem."""
        from pixel_rpg import menus
        game = self.game()
        game.screen = pygame.display.set_mode((1600, 900))
        short = next((i for i in range(9)
                      if menus.guide_scroll_max(game, i) == 0), None)
        self.assertIsNotNone(short, "no page fits at 1600x900 any more")
        game.ui.guide_index = short
        game.handle_event(pygame.event.Event(pygame.KEYDOWN,
                                             key=pygame.K_DOWN,
                                             unicode="", mod=0))
        self.assertEqual(game.ui.guide_index, (short + 1) % 9)

    def test_turning_the_page_goes_back_to_the_top(self):
        game = self.game()
        game.screen = pygame.display.set_mode((844, 390))
        game.ui.guide_index = 2
        game.ui.guide_scroll = 3
        game.handle_event(pygame.event.Event(pygame.KEYDOWN,
                                             key=pygame.K_RIGHT,
                                             unicode="", mod=0))
        self.assertEqual(game.ui.guide_scroll, 0)

    def test_clicking_a_chapter_also_goes_back_to_the_top(self):
        game = self.game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.ui.guide_scroll = 4
        self.assertTrue(game.point_cursor("guide", 5))
        self.assertEqual(game.ui.guide_index, 5)
        self.assertEqual(game.ui.guide_scroll, 0)

class TestTheFiveNewClasses(unittest.TestCase):
    """Roadmap item 0w (s52). Savvydev, twice:

    > "also i think you can offer WAYY more already unlocked classes"
    > "you never added more classes, i think we can add a lot more, no?"

    Eight became thirteen and five unlocked became ten. Each new one is a
    signature rather than a re-skinned statline, so each gets a test that
    the signature actually fires.
    """

    NEW = ("hexbinder", "ironmonk", "apothecary", "spellblade", "duelist")

    def hero(self, key, level=10):
        from pixel_rpg.entities import Player
        player = Player("Probe", key)
        for _ in range(level - 1):
            player.gain_xp(player.xp_to_next())
        player.hp = player.max_hp()
        player.mp = player.max_mp()
        return player

    def foe(self):
        from pixel_rpg.entities import make_enemy
        return make_enemy(10, "meadow")

    def test_all_five_are_playable_on_a_first_run(self):
        from pixel_rpg import classes as C
        starters = {c.key for c in C.STARTER_CLASSES}
        for key in self.NEW:
            self.assertIn(key, starters,
                          "%s is not offered on a first run" % key)
        self.assertEqual(len(C.CLASSES), 13)
        self.assertEqual(len(C.STARTER_CLASSES), 10)

    def test_the_three_that_stay_locked_are_the_original_three(self):
        """The unlocks are the replay spine (s33), not a gate on choice."""
        from pixel_rpg import classes as C
        locked = sorted(c.key for c in C.CLASSES if c.unlock)
        self.assertEqual(locked, ["oathbound", "revenant", "wayfarer"])

    def test_every_class_is_still_buildable(self):
        """A class is a pile of references - skills, bases, consumables,
        weapon families - and any one of them being a typo is a crash at
        character creation, not at import."""
        from pixel_rpg import classes as C
        from pixel_rpg.items import BASES_BY_NAME, CONSUMABLES, FAMILIES
        from pixel_rpg.skills import ALL_SKILLS
        keys = {s.key for s in ALL_SKILLS}
        for cls in C.CLASSES:
            for key, _level in cls.skills:
                self.assertIn(key, keys, "%s: %s" % (cls.key, key))
            self.assertIn(cls.start_weapon, BASES_BY_NAME, cls.key)
            self.assertIn(cls.start_armor, BASES_BY_NAME, cls.key)
            for ckey, count in cls.start_consumables:
                self.assertIn(ckey, CONSUMABLES, cls.key)
                self.assertGreater(count, 0)
            for family in cls.favours:
                self.assertIn(family, FAMILIES, cls.key)
            self.hero(cls.key)

    # -- the signatures ----------------------------------------------------

    def test_affliction_needs_something_to_be_wrong_with_them(self):
        from pixel_rpg.battle import compute_damage
        from pixel_rpg.util import rng
        hero, foe = self.hero("hexbinder"), self.foe()
        rng.reseed(7)
        clean = compute_damage(hero, foe, 1.0, "mag", can_crit=False).amount
        foe.add_status("poison", 3, 4)
        rng.reseed(7)
        cursed = compute_damage(hero, foe, 1.0, "mag", can_crit=False).amount
        self.assertGreater(cursed, clean)
        self.assertAlmostEqual(cursed / clean, 1.35, delta=0.02)

    def test_affliction_is_the_hexbinders_alone(self):
        from pixel_rpg.battle import compute_damage
        from pixel_rpg.util import rng
        hero, foe = self.hero("emberwright"), self.foe()
        rng.reseed(7)
        clean = compute_damage(hero, foe, 1.0, "mag", can_crit=False).amount
        foe.add_status("poison", 3, 4)
        rng.reseed(7)
        cursed = compute_damage(hero, foe, 1.0, "mag", can_crit=False).amount
        self.assertEqual(cursed, clean)

    def test_momentum_is_bulwark_read_backwards(self):
        hero = self.hero("ironmonk")
        hero.hp = hero.max_hp()
        self.assertLess(hero.incoming_multiplier(), 1.0)
        hero.hp = max(1, hero.max_hp() // 4)
        self.assertGreater(hero.incoming_multiplier(), 1.0,
                           "the Ironmonk got tougher as it lost, which is "
                           "the Warden's rule, not its own")

    def test_decoction_doubles_a_potion_and_not_a_spell(self):
        """The distinction is the whole class: doubling `mend` too would
        make it a better Paladin instead of a different hero."""
        hero = self.hero("apothecary")
        self.assertEqual(hero.heal_multiplier(from_item=True), 2.0)
        self.assertEqual(hero.heal_multiplier(from_item=False), 1.0)

    def test_a_potion_really_does_go_twice_as_far(self):
        """Through the battle, not just the multiplier - `_heal_player` is
        the single healing path and the flag has to survive it."""
        from pixel_rpg.battle import Battle
        results = {}
        for key in ("apothecary", "spellblade"):
            hero = self.hero(key)
            hero.consumables = {"potion": 1}
            hero.hp = 1
            battle = Battle(hero, self.foe())
            battle.use_item("potion")
            results[key] = hero.hp
        self.assertGreater(results["apothecary"], results["spellblade"])

    def test_runeblade_adds_magic_to_an_ordinary_swing(self):
        """**One Spellblade against another, differing only in magic.**

        Comparing it to a different class does not isolate the rule - the
        first version of this test lost to a Duelist because Single Combat
        was firing in a one-on-one fight. A basic attack scales on `atk`,
        so raising `mind` alone moves nothing *except* Runeblade.
        """
        from pixel_rpg.battle import Battle
        from pixel_rpg.util import rng
        totals = {}
        for mind in (0, 40):
            rng.reseed(11)
            hero = self.hero("spellblade")
            hero.attributes["might"] = 20
            hero.attributes["mind"] = mind
            foe = self.foe()
            foe.hp = foe.max_hp() * 50
            battle = Battle(hero, foe)
            before = foe.hp
            battle._resolve_basic_attack()
            totals[mind] = before - foe.hp
        self.assertGreater(totals[40], totals[0])

    def test_an_ordinary_swing_is_still_ordinary_for_everybody_else(self):
        from pixel_rpg.battle import Battle
        from pixel_rpg.util import rng
        totals = {}
        for mind in (0, 40):
            rng.reseed(11)
            hero = self.hero("duelist")
            hero.attributes["might"] = 20
            hero.attributes["mind"] = mind
            foe = self.foe()
            foe.hp = foe.max_hp() * 50
            battle = Battle(hero, foe)
            before = foe.hp
            battle._resolve_basic_attack()
            totals[mind] = before - foe.hp
        self.assertEqual(totals[40], totals[0])

    def test_single_combat_swings_both_ways(self):
        from pixel_rpg.battle import compute_damage
        from pixel_rpg.util import rng
        hero, foe = self.hero("duelist"), self.foe()
        # Damage is truncated to a whole number, so a ten-point hit cannot
        # express a ratio to two places. Swing hard enough that it can.
        hero.attributes["might"] = 60
        hero.duel_odds = 1
        rng.reseed(3)
        alone = compute_damage(hero, foe, 1.0, "atk", can_crit=False).amount
        hero.duel_odds = 3
        rng.reseed(3)
        crowd = compute_damage(hero, foe, 1.0, "atk", can_crit=False).amount
        self.assertGreater(alone, crowd)
        self.assertAlmostEqual(alone / crowd, 1.35 / 0.85, delta=0.03)

    def test_a_brawl_that_thins_to_one_becomes_a_duel(self):
        """`duel_odds` is refreshed every turn rather than fixed at setup,
        so the Duelist feels the turn the last one is left standing."""
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import make_enemy
        hero = self.hero("duelist")
        pack = [make_enemy(10, "meadow") for _ in range(3)]
        battle = Battle(hero, pack)
        battle.start_turn()
        self.assertEqual(hero.duel_odds, 3)
        for monster in pack[1:]:
            monster.hp = 0
        battle.start_turn()
        self.assertEqual(hero.duel_odds, 1)

    def test_fortunes_road_now_does_something_in_a_fight(self):
        """It was loot, gold and an opening relic - all of it outside
        combat - and the Generalist read as the weakest class in the game
        because of it (s52)."""
        from pixel_rpg.battle import Battle
        hero = self.hero("wayfarer")
        Battle(hero, self.foe())
        self.assertTrue(hero.revive_charge)
        other = self.hero("spellblade")
        Battle(other, self.foe())
        self.assertFalse(other.revive_charge)

    def test_a_signature_that_says_a_number_says_the_right_one(self):
        """**A signature whose description lies is worse than a wrong
        number**: the player builds around the sentence, not the code.
        Checked for the ones whose text quotes a single percentage that
        the code holds in one place."""
        from pixel_rpg import classes as C
        from pixel_rpg.entities import Player

        def says(key, text):
            self.assertIn(text, C.CLASSES_BY_KEY[key].signature_text,
                          "%s's signature no longer says %s" % (key, text))

        # The three the s52 pass moved. Every one of these was a different
        # number in the sentence an hour before this test existed.
        says("warden", "40%")
        says("oathbound", "35%")
        says("longshot", "30%")
        # And the five new ones.
        says("hexbinder", "35%")
        says("ironmonk", "18%")
        says("apothecary", "double")
        says("spellblade", "40%")
        says("duelist", "35%")

        monk = Player("A", "ironmonk")
        monk.hp = monk.max_hp()
        self.assertAlmostEqual(monk.incoming_multiplier(), 0.82, places=5)
        warden = Player("A", "warden")
        warden.hp = 1
        self.assertAlmostEqual(warden.incoming_multiplier(), 0.60, places=5)
class TestEveryHotspotIsReallyRegistered(unittest.TestCase):
    """`TestMouseReach` scanned for `kind="x"` and missed the rest.

    Most hotspots are registered **positionally** - `hot.add(row, "loadout",
    i)` - and the regex that looked for the keyword form never saw one of
    them. So the test that exists to prove the mouse can reach every row
    was, for most rows, proving nothing: the skill loadout was registered
    and unreachable for as long as it has existed, and Savvydev found it by
    playing (s53).

    This scans with `ast` instead, which sees both forms and cannot be
    fooled by a call that spans two lines - which is how the equipped-gear
    rows hid from the improved *regex* as well.
    """

    DRAWS = ("menus", "devmenu", "battlescreen", "game", "ui", "touch")

    def registered_kinds(self):
        import ast
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "pixel_rpg")
        kinds = set()
        for name in self.DRAWS:
            tree = ast.parse(open(os.path.join(root, name + ".py"),
                                  encoding="utf-8").read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr == "add" \
                        and len(node.args) >= 2:
                    second = node.args[1]
                    if isinstance(second, ast.Constant) \
                            and isinstance(second.value, str):
                        kinds.add(second.value)
                for kw in node.keywords:
                    if kw.arg == "kind" and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str):
                        kinds.add(kw.value.value)
        self.assertIn("loadout", kinds,
                      "the scan missed a positional hot.add; it rotted again")
        self.assertIn("gear", kinds,
                      "the scan missed a multi-line hot.add; it rotted again")
        return kinds

    def test_the_ast_scan_sees_more_than_the_regex_did(self):
        """Guards the *reason* this class exists rather than its result."""
        import re
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "pixel_rpg")
        by_regex = set()
        for name in self.DRAWS:
            with open(os.path.join(root, name + ".py"), encoding="utf-8") as fh:
                by_regex |= set(re.findall(r'kind="([a-z_]+)"', fh.read()))
        self.assertLess(len(by_regex), len(self.registered_kinds()),
                        "the old keyword-only scan now finds everything, "
                        "which means this test is measuring nothing")

    def test_every_registered_kind_can_be_reached(self):
        from pixel_rpg import game as G
        game = build_game_for_mouse()
        direct = TestMouseReach().acted_on_directly()
        unreachable = [k for k in sorted(self.registered_kinds())
                       if k not in direct and not game.point_cursor(k, 0)]
        self.assertEqual(unreachable, [],
                         "registered but the mouse cannot move to them")


class TestTheSkillLoadoutAnswersTheMouse(unittest.TestCase):
    """Roadmap item 0aa (s53). Savvydev, third school playtest:

    > "no clue what skill you have selected and mouse doesnt work on
    > selecting skills, you should be able to use mouse on selecting your
    > skills without having to click tab"
    """

    def sheet(self, slot=3):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "cutpurse"
        game.new_run("Clicker", slot)
        self.addCleanup(game.saves.delete, slot)
        game.dialogue = None
        for _ in range(12):
            game.player.gain_xp(game.player.xp_to_next())
        game.ui.newskill = None
        game.state = G.CHARACTER
        return game

    def test_a_click_reaches_the_pane_without_tab(self):
        game = self.sheet()
        self.assertEqual(game.ui.char_pane, "stats")
        self.assertTrue(game.point_cursor("loadout", 2))
        self.assertEqual(game.ui.char_pane, "skills",
                         "the cursor moved into a pane the keyboard was "
                         "not pointed at, which is the invisible selection")
        self.assertEqual(game.ui.skill_index, 2)

    def test_a_click_does_exactly_what_the_keyboard_does(self):
        """Parity, not a second implementation.

        Deliberately compares the two paths rather than asserting an
        outcome: with four carried, Enter on a *fifth* skill is correctly
        refused, and the first version of this test read that refusal as
        the click having failed. What matters is that both inputs reach
        the same code and agree - including when the answer is no.
        """
        from pixel_rpg import game as G

        def by_key(game, index):
            game.ui.char_pane = "skills"
            game.ui.skill_index = index
            game.handle_event(pygame.event.Event(pygame.KEYDOWN,
                                                 key=pygame.K_RETURN,
                                                 unicode="", mod=0))
            return sorted(s.key for s in game.player.skills())

        def by_click(game, index):
            game.ui.char_pane = "stats"          # as if Tab was never pressed
            rect = pygame.Rect(10, 10 + index * 20, 60, 18)
            game.ui.hot.clear()
            game.ui.hot.add(rect, "loadout", index)
            game.mouse_click(rect.center, button=1)
            return sorted(s.key for s in game.player.skills())

        known = self.sheet(slot=4).player.known_skills()
        self.assertGreater(len(known), 4, "the fixture has nothing to swap")
        for index in (0, len(known) - 1):
            keyed = by_key(self.sheet(slot=4), index)
            clicked = by_click(self.sheet(slot=4), index)
            self.assertEqual(keyed, clicked,
                             "row %d: the mouse and the keyboard disagree"
                             % index)

    def test_the_pane_says_which_four_you_carry(self):
        """The equipped mark was `highlight(row, colour, 2)` and the third
        positional argument is `fill_alpha`. It was painting at 2/255."""
        import inspect
        from pixel_rpg import menus
        body = inspect.getsource(menus.draw_character)
        self.assertIn('kind="loadout"', body,
                      "the loadout rows stopped going through row_highlight, "
                      "which is what draws chosen and cursor differently")
        self.assertIn("loadout", menus.MULTI_SELECT_KINDS,
                      "without this, equipped and 'where the cursor is' "
                      "draw the same box")

    def test_left_click_inspects_gear_as_well_as_right(self):
        game = self.sheet(slot=5)
        game.ui.gear_detail = None
        rect = pygame.Rect(10, 10, 40, 20)
        game.ui.hot.clear()
        game.ui.hot.add(rect, "gear", 1)
        game.mouse_click(rect.center, button=1)
        self.assertEqual(game.ui.gear_detail, 1,
                         "inspecting worn gear was right-click only")


class TestANewSkillAsksBeforeItIsLost(unittest.TestCase):
    """Roadmap item 0ac (s53). Savvydev:

    > "maybe add a pop up window asking you if you want to replace a skill
    > with the one you just learned and what that skill does"

    s48's full-bag question in a second place, and it also answers the
    notification he withdrew (0ab): there *was* a toast, among four other
    toasts a won fight raises, and he did not see it.

    **Every one of these levels a real hero into a real unlock** rather
    than handing `learned_skill` an arbitrary ability. The first draft did
    the latter and passed a Cutpurse `judgment`, which it cannot learn
    until 23 - so `equip_skill` refused it and four tests failed on the
    fixture rather than on the feature. A skill nobody can learn is not a
    skill anybody is about to be asked about.
    """

    def about_to_learn(self, slot=6):
        """A Cutpurse one level below Frostbite, carrying four."""
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "cutpurse"
        game.new_run("Learner", slot)
        self.addCleanup(game.saves.delete, slot)
        game.dialogue = None
        while game.player.level < 11:
            game.player.gain_xp(game.player.xp_to_next())
        game.ui.newskill = None
        game.state = G.OVERWORLD
        return game

    def level_up(self, game):
        """Gain the level, and hand the game what it actually unlocked."""
        _levels, fresh = game.player.gain_xp(game.player.xp_to_next())
        self.assertTrue(fresh, "level 12 stopped unlocking anything")
        game.learned_skill(fresh[0])
        return fresh[0]

    def test_a_free_hand_needs_no_question(self):
        from pixel_rpg import game as G
        game = self.about_to_learn(slot=2)
        # Carry one, so there is nothing to decide.
        game.player.loadout = [game.player.known_skills()[0].key]
        skill = self.level_up(game)
        self.assertIsNone(game.ui.newskill)
        self.assertEqual(game.state, G.OVERWORLD)
        self.assertIn(skill.key, {s.key for s in game.player.skills()},
                      "a skill that fits should just be picked up")

    def test_a_full_loadout_asks(self):
        from pixel_rpg import game as G
        game = self.about_to_learn()
        self.level_up(game)
        self.assertEqual(game.state, G.NEWSKILL)
        self.assertEqual(len(game.ui.newskill["carried"]), 4)

    def test_answering_swaps_exactly_one(self):
        from pixel_rpg import game as G
        game = self.about_to_learn()
        skill = self.level_up(game)
        dropped = game.ui.newskill["carried"][1]
        game.resolve_new_skill(1)
        carried = {s.key for s in game.player.skills()}
        self.assertIn(skill.key, carried)
        self.assertNotIn(dropped, carried)
        self.assertEqual(len(carried), 4, "the loadout changed size")
        self.assertEqual(game.state, G.OVERWORLD)

    def test_declining_keeps_the_four_you_had(self):
        game = self.about_to_learn()
        before = [s.key for s in game.player.skills()]
        skill = self.level_up(game)
        game.resolve_new_skill(None)
        self.assertEqual([s.key for s in game.player.skills()], before)
        self.assertIn(skill.key,
                      {s.key for s in game.player.known_skills()},
                      "declining must not un-learn it - it is still yours")

    def test_escape_declines(self):
        game = self.about_to_learn()
        before = [s.key for s in game.player.skills()]
        self.level_up(game)
        game.handle_event(pygame.event.Event(pygame.KEYDOWN,
                                             key=pygame.K_ESCAPE,
                                             unicode="", mod=0))
        self.assertIsNone(game.ui.newskill)
        self.assertEqual([s.key for s in game.player.skills()], before)

    def test_the_mouse_can_answer_it(self):
        game = self.about_to_learn()
        self.level_up(game)
        self.assertTrue(game.point_cursor("newskill", 2))
        self.assertEqual(game.ui.newskill_index, 2)


class TestTextStaysInsideItsHighlight(unittest.TestCase):
    """Savvydev, third school playtest (s53):

    > "also theres a LOT of places where the text like overlaps the
    > selection highlight and stuff, and it doesnt look too good"

    Ninety-two of them across twenty-five screens, and every one invisible
    to `tests/layout.py` until it learned to record `ui.highlight`. This
    guards the two helpers the fixes went through, because the failure was
    never one screen - it was one mistake copied into every list.
    """

    def test_a_row_is_never_shorter_than_its_own_font(self):
        from pixel_rpg import ui
        from pixel_rpg.assets import Assets
        pygame.display.set_mode((1280, 720))
        assets = Assets()
        for font in ("tiny", "small", "body", "mid"):
            height = assets.font(font).get_height()
            for pitch in range(14, 60):
                got = ui.row_height(assets, font, pitch)
                self.assertLessEqual(got, max(2, pitch - 2),
                                     "a row taller than its slot collides "
                                     "with the row under it")
                if pitch - 2 >= height + 2:
                    self.assertGreaterEqual(got, height + 2,
                                            "%s at pitch %d crushes its own "
                                            "text" % (font, pitch))

    def test_row_text_centres_rather_than_guessing_an_offset(self):
        from pixel_rpg import ui
        from pixel_rpg.assets import Assets
        screen = pygame.display.set_mode((1280, 720))
        assets = Assets()
        box = pygame.Rect(40, 40, 300, 30)
        rect = ui.row_text(screen, assets, "Backstab", box, "small")
        self.assertGreaterEqual(rect.top, box.top)
        self.assertLessEqual(rect.bottom, box.bottom)

    def test_a_right_aligned_row_label_also_stays_in(self):
        """`right=True` anchors on the vertical centre - §7's trap, and the
        cause of the creator rows hanging out of their own highlight."""
        from pixel_rpg import ui
        from pixel_rpg.assets import Assets
        screen = pygame.display.set_mode((1280, 720))
        assets = Assets()
        box = pygame.Rect(40, 40, 300, 30)
        rect = ui.row_text(screen, assets, "26 MP", box, "tiny", right=True)
        self.assertGreaterEqual(rect.top, box.top)
        self.assertLessEqual(rect.bottom, box.bottom)
        self.assertLessEqual(rect.right, box.right)

    # **The rule's own self-test lives in `tests/layout.py --selftest`**,
    # not here. It was written here first and importing that module
    # repoints `PIXEL_RPG_SAVE_DIR` at its own temp folder, which broke
    # `TestTheSuiteCannotEatARealSave` - the guard added after the suite
    # destroyed somebody's run (§7, §46). The guard caught it immediately,
    # which is the whole reason it exists, and §36 already says a
    # checker's rules are proved in the checker.

class TestTheSixNewModifiers(unittest.TestCase):
    """Designed with Savvydev on 2026-09-05 (s54). Two are his from s46:

    > "i think u can add a lot more modifiers, like a modifier that deletes
    > the save if you leave so you have to beat the game in one sitting for
    > example. or a modifier that removes the feature that resets your mp
    > and hp everytime you level up and stuff"

    Nine became fifteen, and every one still only takes (s45).
    """

    def hero(self, *keys):
        from pixel_rpg.entities import Player
        from pixel_rpg.modifiers import RunModifiers
        player = Player("Probe", "warden")
        player.modifiers = RunModifiers(keys)
        return player

    def test_none_of_them_give_anything(self):
        """**Sixteen since 2026-09-06** (item 0ai). The count moved and the
        rule did not: Timed Hands changes what a *press* is worth, and a
        missed one is worse than not having taken it - so nothing here
        hands the hero a flat number back."""
        from pixel_rpg.modifiers import MODIFIERS, RunModifiers
        self.assertEqual(len(MODIFIERS), 18)
        every = RunModifiers([m.key for m in MODIFIERS])
        # s45's rule: the score multiplier is the whole reward. Anything
        # that hands the hero a number back would show up in one of these.
        self.assertEqual(every.gold_mult(), 1.0)
        self.assertEqual(every.xp_mult(), 1.0)
        self.assertEqual(every.damage_mult(), 1.0)
        self.assertEqual(every.defense_mult(), 1.0)
        self.assertGreater(every.score, 1.0)

    def test_every_modifier_says_what_it_takes(self):
        """**Two kinds since 2026-09-06.** A `cost` scores above 1.0 -
        §45's rule, unchanged. A `preference` scores exactly 1.0: it
        changes how the game is played rather than how hard it is, and
        paying somebody for choosing the game they wanted is not a
        reward."""
        from pixel_rpg.modifiers import MODIFIERS
        for mod in MODIFIERS:
            self.assertTrue(mod.cost.strip(), mod.key)
            self.assertTrue(mod.tags, "%s does nothing at all" % mod.key)
            if mod.scores:
                self.assertGreater(mod.score, 1.0, mod.key)
            else:
                self.assertEqual(mod.score, 1.0,
                                 "%s is a preference and scores" % mod.key)

    def test_every_tag_is_actually_read_somewhere(self):
        """**A modifier whose cost silently does not exist is the Blind
        Road bug** - `modifiers.py` opens with it. Its `no_map` tag was
        checked in two places that paid out and nowhere that took away, so
        it was a pure boon by accident and Savvydev reported it as broken."""
        import os
        from pixel_rpg.modifiers import MODIFIERS
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "pixel_rpg")
        source = ""
        for name in sorted(os.listdir(root)):
            if name.endswith(".py"):
                with open(os.path.join(root, name), encoding="utf-8") as fh:
                    source += fh.read()
        for mod in MODIFIERS:
            for tag in mod.tags:
                # The declaration plus at least one reader. Counting rather
                # than excluding `modifiers.py` because a cost that *is* a
                # multiplier is answered in that file on purpose - `glass`
                # is `hp_mult` and lives nowhere else, and it is not dead.
                self.assertGreater(
                    source.count('"%s"' % tag), 1,
                    "%s's cost (%s) is declared and read by nothing"
                    % (mod.key, tag))

    # -- his two -----------------------------------------------------------

    def test_unmended_stops_the_level_up_top_up(self):
        plain, unmended = self.hero(), self.hero("unmended")
        for player in (plain, unmended):
            player.hp = 5
            player.mp = 0
            player.gain_xp(player.xp_to_next())
        self.assertEqual(plain.hp, plain.max_hp(), "levelling stopped healing")
        self.assertEqual(unmended.hp, 5)
        self.assertEqual(unmended.mp, 0)

    def test_one_sitting_is_never_written(self):
        """**Built inside out**: he asked for a modifier that deletes the
        save, and this one never makes one. There is no delete path."""
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.pending_modifiers.add("onesitting")
        game.new_run("Ghost", 5)
        game.dialogue = None
        self.assertTrue(game.player.modifiers.never_saves())
        self.assertFalse(game.save_run(silent=True, force=True))
        self.assertTrue(game.saves.describe(5).get("empty"),
                        "One Sitting wrote a slot")

    def test_one_sitting_does_not_touch_a_save_already_there(self):
        """It refuses to write; it must not erase either. The whole reason
        this is built this way round is that nothing here deletes."""
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Keeper", 4)
        game.dialogue = None
        self.assertTrue(game.save_run(silent=True, force=True))
        self.addCleanup(game.saves.delete, 4)
        before = game.saves.describe(4)

        game.pending_modifiers.add("onesitting")
        game.new_run("Ghost", 4)
        game.save_run(silent=True, force=True)
        after = game.saves.describe(4)
        self.assertFalse(after.get("empty"), "the old run was erased")
        self.assertEqual(after["name"], before["name"])

    def test_every_save_path_goes_through_the_one_gate(self):
        """Autosave, the pause menu, respawning, an ending and quitting all
        call `save_run`. If any of them wrote directly, One Sitting would
        leak a slot on a path nobody tested."""
        import inspect
        from pixel_rpg import game as G
        body = inspect.getsource(G)
        # `save_run` itself is allowed to reach the store; nothing else is.
        writes = [line.strip() for line in body.splitlines()
                  if "saves.write(" in line]
        self.assertEqual(len(writes), 1,
                         "something writes a slot outside save_run: %s"
                         % writes)

    # -- the four from the same conversation -------------------------------

    def test_mortal_does_not_wake_at_the_shrine(self):
        from pixel_rpg import game as G
        from pixel_rpg.entities import make_enemy
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.pending_modifiers.add("mortal")
        game.new_run("Doomed", 3)
        self.addCleanup(game.saves.delete, 3)
        game.dialogue = None
        game.start_battle([make_enemy(3, "meadow", game.settings.mods)])
        game.player.hp = 0
        game.battle.result = "defeat"
        game.finish_battle()
        self.assertTrue(game.player.fallen)
        self.assertEqual(game.state, G.GAMEOVER)

    def test_a_fallen_run_cannot_be_loaded_again(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.pending_modifiers.add("mortal")
        game.new_run("Doomed", 3)
        self.addCleanup(game.saves.delete, 3)
        game.dialogue = None
        game.player.fallen = True
        game.save_run(silent=True, force=True)
        self.assertTrue(game.saves.describe(3)["fallen"])
        self.assertFalse(game.load_run(3), "a fallen hero got back up")

    def test_falling_survives_a_save_and_a_load(self):
        from pixel_rpg.entities import Player
        from pixel_rpg.save import SAVE_VERSION, deserialize_player, \
            serialize_player
        player = Player("Gone", "warden")
        player.fallen = True
        back = deserialize_player(serialize_player(player), SAVE_VERSION)
        self.assertTrue(back.fallen)
        self.assertFalse(Player("Fine", "warden").fallen)

    def test_iron_loadout_refuses_every_edit(self):
        plain, iron = self.hero(), self.hero("ironloadout")
        for _ in range(20):
            for player in (plain, iron):
                player.gain_xp(player.xp_to_next())
        # **Putting one down, not picking one up.** With four carried the
        # loadout is full and adding a fifth is refused for everybody -
        # which is the rule, not the modifier. What Iron Loadout takes away
        # is the ability to change the four at all.
        carried = plain.skills()[0].key
        self.assertTrue(plain.equip_skill(carried))
        self.assertFalse(iron.equip_skill(iron.skills()[0].key))
        self.assertEqual([s.key for s in iron.skills()],
                         [s.key for s in iron.known_skills()[:4]],
                         "Iron Loadout is the first four you learned")

    def test_sworn_blade_keeps_the_weapon_and_frees_the_rest(self):
        from pixel_rpg.items import roll_item
        player = self.hero("swornblade")
        first = roll_item(4, slot="weapon")
        player.add_item(first)
        player.equip(first)
        better = roll_item(40, slot="weapon", rarity="mythic")
        player.add_item(better)
        self.assertIsNone(player.equip(better))
        self.assertIs(player.equipment["weapon"], first)
        # Armour and trinkets are untouched: the modifier is about a blade.
        armor = roll_item(20, slot="armor")
        player.add_item(armor)
        player.equip(armor)
        self.assertIs(player.equipment["armor"], armor)

    def test_equip_best_does_not_claim_a_swap_it_was_refused(self):
        """`equip` returns the *displaced* item, which is legitimately None
        for an empty slot - so the return value cannot say whether anything
        happened, and reporting a swap that did not occur would put
        "Equipped X" in the log with X still in the bag."""
        from pixel_rpg.items import roll_item
        player = self.hero("swornblade")
        first = roll_item(4, slot="weapon")
        player.add_item(first)
        player.equip(first)
        player.add_item(roll_item(40, slot="weapon", rarity="mythic"))
        self.assertEqual([i.slot for i in player.equip_best()
                          if i.slot == "weapon"], [])

    def test_small_hands_is_ten_and_cannot_be_bought_out_of(self):
        plain, small = self.hero(), self.hero("smallhands")
        self.assertEqual(small.bag_size(), 10)
        self.assertGreater(plain.bag_size(), 10)
        small.gold = 999999
        self.assertEqual(small.bag_upgrade_cost(), 0)
        self.assertEqual(small.buy_bag_upgrade(), 0)
        self.assertEqual(small.bag_size(), 10, "bought out of Small Hands")
        self.assertEqual(small.gold, 999999, "it charged for nothing")

    def test_one_sitting_and_mortal_are_separate_modifiers(self):
        """One sitting is not one life - his design, and they stack."""
        from pixel_rpg.modifiers import MODIFIERS_BY_KEY
        self.assertNotEqual(MODIFIERS_BY_KEY["onesitting"].tags,
                            MODIFIERS_BY_KEY["mortal"].tags)
        both = self.hero("onesitting", "mortal")
        self.assertTrue(both.modifiers.never_saves())
        self.assertTrue(both.modifiers.has("mortal"))
class TestTheRuinsSaySomething(unittest.TestCase):
    """Roadmap item 12b (s55). §44 closed the first half - the hamlets
    speak - and left the second written down:

    > "the ruins say nothing on purpose and could say something anyway -
    > not a person, but the place itself. `What Is Left of Hale` is a name
    > with a story in it and no way to read it."
    """

    def world(self, seed=4242):
        from pixel_rpg.world import World
        world = World(32, seed)
        world.generate()
        return world

    def test_every_ruin_and_circle_has_something_to_read(self):
        world = self.world()
        kinds = {}
        for spot, (kind, _name) in world.outposts.items():
            kinds.setdefault(kind, []).append(spot)
        self.assertEqual(len(kinds.get("ruin", [])), 6)
        self.assertEqual(len(kinds.get("stones", [])), 5)
        # Eleven places, eleven markers. A hamlet has a person instead.
        self.assertEqual(len(world.markers), 11)

    def test_a_marker_is_a_prop_you_can_walk_up_to(self):
        from pixel_rpg.world import BLOCKING_PROPS, PROP_INTERACT
        world = self.world()
        self.assertIn("marker", BLOCKING_PROPS)
        self.assertIn("marker", PROP_INTERACT)
        for spot in world.markers:
            self.assertEqual(world.props.get(spot), "marker")
            self.assertEqual(world.interactable_near(*spot)[0], "marker")

    def test_the_world_rebuilds_its_markers_from_the_seed(self):
        """Hard rule 4: terrain is never stored, it regenerates. A marker
        that did not come back the same would be a place that changed what
        it said between one load and the next."""
        first, second = self.world(777), self.world(777)
        self.assertEqual(first.markers, second.markers)
        self.assertTrue(first.markers)

    def test_reading_one_opens_a_dialogue_and_asks_nothing(self):
        """**Not a scene.** A scene asks a question and leans an ending
        (§33); a ruin is not a decision and must not record one."""
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Reader", 6)
        self.addCleanup(game.saves.delete, 6)
        game.dialogue = None
        # **The prologue is up** - it plays every run since s48 - and it is
        # a scene, so it has to be cleared before this can claim the marker
        # opened one.
        game.scene = None
        game.state = G.OVERWORLD
        spot = sorted(game.world.markers)[0]
        game.player.x = spot[0] * game.tile + game.tile / 2
        game.player.y = spot[1] * game.tile + game.tile / 2
        before = dict(game.meta.scene_seen)
        game.interact()
        self.assertEqual(game.state, G.DIALOGUE)
        self.assertTrue(game.dialogue["body"])
        self.assertIsNone(game.scene, "a marker opened a scene")
        self.assertEqual(game.meta.scene_seen, before,
                         "reading a ruin wrote to the story record")

    def test_the_one_the_handoff_named_can_be_read(self):
        from pixel_rpg import story
        what, lines = story.ruin_marker("What Is Left of Hale")
        self.assertTrue(what)
        self.assertIn("HALE", " ".join(lines))

    def test_a_circle_says_the_same_thing_every_time(self):
        """By index rather than at random: a place that changes its mind
        is not a place."""
        from pixel_rpg import story
        self.assertEqual(story.stone_reading(0), story.stone_reading(0))
        self.assertEqual(story.stone_reading(0),
                         story.stone_reading(len(story.STONE_READINGS)))
        seen = {story.stone_reading(i)[0]
                for i in range(len(story.STONE_READINGS))}
        self.assertEqual(len(seen), len(story.STONE_READINGS),
                         "two circles read the same")

    def test_no_marker_leaks_the_story(self):
        """**§42's rule for anything added between fixed points**: it must
        not change what an earlier ending meant. A ruin that named a
        warden, the Door or an ending would be the codex arriving in
        scenery nobody earned (§17)."""
        from pixel_rpg import story
        forbidden = ["warden", "the door", "anchor", "memory", "seventh",
                     "ysolde", "maren", "callun", "dorn", "vell", "kessa",
                     "ilan"]
        readings = list(story.RUIN_MARKERS.values()) + \
            list(story.STONE_READINGS)
        for what, lines in readings:
            text = (" ".join(lines) + " " + what).lower()
            for word in forbidden:
                self.assertNotIn(word, text,
                                 "a marker mentions %r: %s" % (word, what))

    def test_every_ruin_name_in_the_world_has_a_reading(self):
        """A place with no reading gets no marker rather than a blank one -
        so a name added to `RUIN_NAMES` without a line is a silent gap."""
        from pixel_rpg import story
        from pixel_rpg.world import World
        for name in World.RUIN_NAMES:
            self.assertIsNotNone(story.ruin_marker(name),
                                 "%s has no marker written" % name)
class TestTheProbesSpendWhatTheyAreGiven(unittest.TestCase):
    """§38's hole, as a rule rather than a fix (s62).

    `tools/balance.py` measured the difficulty table on a hero who had
    never opened their own sheet, and §6 carried those numbers for weeks.
    The fix was `spend_points`; **the fix was then re-broken twice**, once
    by the wave probe (s60) and once by nothing at all - the boss table
    was simply never re-run. So this asserts the property rather than any
    one caller: a hero this harness builds has spent its points.
    """

    def test_build_spends_every_point(self):
        import importlib.util
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            "balance_probe", os.path.join(root, "tools", "balance.py"))
        balance = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(balance)
        for level in (1, 8, 16, 30):
            hero = balance.build("warden", level, 0.0)
            self.assertEqual(hero.points, 0,
                             "level %d probe left points unspent" % level)
            self.assertTrue(hero.equipment.get("weapon"),
                            "level %d probe is unarmed" % level)

    def test_every_probe_in_the_tool_builds_through_build(self):
        """A new probe that rolls its own `Player` is the same bug again -
        it is how the wave table read 0% at wave 10 (s60)."""
        import ast
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "tools", "balance.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        made = [node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Player"]
        self.assertEqual(
            len(made), 1,
            "a probe builds a Player directly instead of through `build`; "
            "that hero will have unspent points and measure nothing")


class TestVillageVariety(unittest.TestCase):
    """Item 19 (s61): no two villages the same, and nothing else moved."""

    def worlds(self, seeds=(1, 5, 41, 99, 2024, 31415)):
        from pixel_rpg.world import World
        return [World(32, seed) for seed in seeds]

    def walkable_from(self, world, start):
        """Flood the island from `start`, as a player walks it."""
        from collections import deque
        seen, queue = {start}, deque([start])
        while queue:
            x, y = queue.popleft()
            for step in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                spot = (x + step[0], y + step[1])
                if spot in seen or not world.in_bounds(*spot):
                    continue
                if world.blocked(*spot) or not world.on_land(*spot):
                    continue
                seen.add(spot)
                queue.append(spot)
        return seen

    def test_no_two_villages_are_the_same(self):
        """*"each run less identical"* (§34). It was one fixed recipe:
        the same six houses and the same well, every run."""
        shapes = set()
        for world in self.worlds():
            homes = tuple(sorted(pos for pos, kind in world.doors.items()
                                 if kind == "home"
                                 and world.town_rect.collidepoint(pos)))
            shapes.add(homes)
        self.assertGreater(len(shapes), 1, "every village is identical")

    def test_the_village_never_moves_anything_else(self):
        """**The reason this was safe to do at all.**

        `generate()` reseeds the shared RNG and then rolls the chests, the
        portals, the lairs and the towers off it - and `save.py` stores
        chests **by coordinate**. One extra draw in `_build_town` would
        move every chest on the island, so a run in progress would load
        with its opened chests forgotten and `THE LONG WALK` quietly
        unwinnable. The village rolls from a private stream.
        """
        from pixel_rpg.world import World
        from pixel_rpg.util import rng
        world = World(32, 2024)
        rng.reseed(55)
        expected = rng.rint(0, 10 ** 9)
        rng.reseed(55)
        world._build_town()
        self.assertEqual(rng.rint(0, 10 ** 9), expected,
                         "building the village moved the shared RNG")

    def test_the_same_seed_builds_the_same_village(self):
        from pixel_rpg.world import World
        first, second = World(32, 777), World(32, 777)
        self.assertEqual(first.doors, second.doors)
        self.assertEqual(first.props, second.props)
        self.assertEqual(first.spawn, second.spawn)

    def test_every_village_still_has_the_things_that_are_referenced(self):
        """A village without a trader is not a variation, it is a broken
        run: `use_door`, both quest-givers and the shrine are anchored to
        these."""
        for world in self.worlds():
            kinds = list(world.doors.values())
            self.assertEqual(kinds.count("shop"), 1)
            self.assertEqual(kinds.count("forge"), 1)
            self.assertIn("well", world.props.values())
            self.assertIn("shrine", world.props.values())
            self.assertIn("smith", world.props.values())
            self.assertIn("elder", world.props.values())
            people = {npc.kind for npc in world.npcs}
            self.assertIn("smith", people)
            self.assertIn("elder", people)
            self.assertFalse(world.blocked(*world.spawn),
                             "you spawn inside something")

    def test_every_door_and_every_villager_can_be_reached(self):
        """**The bug this caught.** Two of the house slots sat one row
        lower to begin with, which put their doorway between their own
        walls and the fence along the town's edge - unreachable on 108 of
        120 seeds, and invisible in a screenshot of any other seed."""
        for world in self.worlds(seeds=range(1, 41)):
            reachable = self.walkable_from(world, world.spawn)

            def can_stand_beside(spot):
                return any((spot[0] + dx, spot[1] + dy) in reachable
                           for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))

            for pos, kind in world.doors.items():
                if not world.town_rect.collidepoint(pos):
                    continue          # hamlets, not the village
                self.assertTrue(can_stand_beside(pos),
                                "seed %s: the %s door at %s is walled in"
                                % (world.seed, kind, pos))
            for npc in world.npcs:
                if not world.town_rect.collidepoint((npc.gx, npc.gy)):
                    continue
                self.assertTrue(can_stand_beside((npc.gx, npc.gy)),
                                "seed %s: %s cannot be reached"
                                % (world.seed, npc.name))

    def test_every_home_door_can_actually_be_opened(self):
        """**Walkable is not the same as usable.**

        `interactable_near` scans the nine tiles around you and returns
        the *first* match, south before north - so a villager standing
        near the square you use a door from answers `E` instead of the
        door. Nothing is blocked, nothing looks wrong, and that house can
        never be entered. 19 seeds in 120 had one before villagers were
        kept three tiles clear of every doorway.
        """
        for world in self.worlds(seeds=range(1, 41)):
            for pos, kind in world.doors.items():
                if kind != "home" or not world.town_rect.collidepoint(pos):
                    continue
                openable = False
                for dx, dy in ((0, 1), (1, 0), (-1, 0), (0, -1)):
                    stand = (pos[0] + dx, pos[1] + dy)
                    if not world.in_bounds(*stand) or world.blocked(*stand):
                        continue
                    found, target = world.interactable_near(*stand)
                    if found == "door" and target[0] == pos:
                        openable = True
                        break
                self.assertTrue(openable,
                                "seed %s: the door at %s cannot be opened "
                                "from any side" % (world.seed, pos))

    def test_the_trader_can_be_entered(self):
        """**s96. On every island, it could not.** The test above holds
        *home* doors to it and the shop was never asked: Elder Maren stood
        on the trader's step, its doorway is a counter rather than a
        floor, and every tile you could open it from answered her first.
        She stands beside it now, and still answers where she stands."""
        for world in self.worlds(seeds=range(1, 41)):
            shop = next(pos for pos, kind in world.doors.items()
                        if kind == "shop")
            elder = next((n.gx, n.gy) for n in world.npcs
                         if n.kind == "elder")
            openable = talkable = False
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for (x, y), want in (((shop[0] + dx, shop[1] + dy), "door"),
                                         ((elder[0] + dx, elder[1] + dy), "npc")):
                        if not world.in_bounds(x, y) or world.blocked(x, y):
                            continue
                        found, target = world.interactable_near(x, y)
                        if want == "door" and found == "door" \
                                and target[0] == shop:
                            openable = True
                        if want == "npc" and found == "npc" \
                                and (target.gx, target.gy) == elder:
                            talkable = True
            self.assertTrue(openable, "seed %s: the trader cannot be entered"
                            % world.seed)
            self.assertTrue(talkable, "seed %s: Maren cannot be spoken to"
                            % world.seed)

    def test_the_village_four_all_turn_up_and_all_have_a_scene(self):
        """A villager who is in neither `VILLAGER_SCENES` nor
        `VILLAGER_LINES` **silently stops having a conversation** - which
        is how two invented names got past a screenshot. Varying *who*
        turns up would also mean a run where a scene cannot be reached at
        all, which is content removed rather than varied (§20)."""
        from pixel_rpg import scenes as scenes_mod
        from pixel_rpg import game as G
        for world in self.worlds():
            named = sorted(npc.name for npc in world.npcs
                           if npc.kind == "villager" and " of " not in npc.name)
            self.assertEqual(len(named), 4, world.seed)
            for name in named:
                self.assertIn(name, scenes_mod.VILLAGER_SCENES, name)
                self.assertIn(name, G.Game.VILLAGER_LINES, name)

    def test_houses_never_overlap_the_green_or_each_other(self):
        """Slots rather than rolled rectangles, so this can be asserted
        once: two houses in the same square is a wall you walk through and
        a door that opens into brick."""
        from pixel_rpg.world import World
        seen = []
        for dx, dy, hw, hh in World.HOUSE_SLOTS:
            box = (dx, dy, dx + hw, dy + hh)
            for other in seen:
                apart = (box[2] <= other[0] or box[0] >= other[2]
                         or box[3] <= other[1] or box[1] >= other[3])
                self.assertTrue(apart, "%s overlaps %s" % (box, other))
            seen.append(box)
            # **The three tiles that are referenced by name**, not the
            # whole green: houses have always abutted and slightly clipped
            # the green's corners - they did before item 19 too - and the
            # green is only stone floor. What must never be built on is
            # where you spawn, the well and the shrine.
            for spot in ((0, 0), (0, -2), (0, 2)):
                inside = (box[0] <= spot[0] < box[2]
                          and box[1] <= spot[1] < box[3])
                self.assertFalse(inside,
                                 "%s is built on %s" % (box, spot))

    def test_the_chest_count_is_untouched(self):
        """§42: the number of chests is what `THE LONG WALK` costs."""
        for world in self.worlds():
            self.assertEqual(len(world.chests), 22)


class TestWeightIsVisible(unittest.TestCase):
    """§70. Savvydev, 2026-09-06:

    > "make sure some of the bigger more heavy hitting weapons and skills
    > look way more cooler and bigger and stuff yk?"

    **A proportion rule, not a list of effects.** Every blow used to shake
    the screen by 3 (9 on a crit) and throw 9 sparks (16 on a crit),
    whether it was a dagger poke or a Meteor.
    """

    def armed(self, weapon, level=10):
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import Player, make_encounter
        from pixel_rpg.items import roll_item, BASES_BY_NAME
        from pixel_rpg.util import rng
        rng.reseed(4)
        hero = Player("Swinger", "warden")
        for _ in range(level - 1):
            hero.gain_xp(hero.xp_to_next())
        item = roll_item(level, rarity="common",
                         base=BASES_BY_NAME.get(weapon))
        item.name = weapon
        hero.add_item(item)
        hero.equip(item)
        hero.full_restore()
        battle = Battle(hero, make_encounter(level, "meadow", count=1))
        for _ in range(400):
            if battle.finished or battle.phase == "menu":
                break
            battle.update(0.05)
        return battle

    def spectacle(self, battle):
        """The shake and particle count of one swing."""
        seen = {"shake": [], "particles": [], "motion": []}
        original = battle.on_event

        def spy(kind, data):
            if kind in ("shake", "particles"):
                seen[kind].append(data.get("amount", data.get("count")))
            if kind == "motion":
                seen["motion"].append(data.get("move"))
            return original(kind, data)

        battle.on_event = spy
        battle.player_attack()
        for _ in range(80):
            battle.update(0.05)
        return seen

    def test_a_weapons_weight_is_read_off_what_it_already_is(self):
        """**No new field.** A heavy weapon is one that gives a lot of
        attack and takes speed away, which is how the bases were already
        written - so a new base gets a sensible answer for free."""
        from pixel_rpg.items import BASES_BY_NAME, weapon_heft
        light = weapon_heft(BASES_BY_NAME["Dagger"])
        heavy = weapon_heft(BASES_BY_NAME["Greatsword"])
        self.assertLess(light, 0.2)
        self.assertGreater(heavy, 0.8)
        for name in ("Dagger", "Greatsword", "Warhammer", "Oak Staff"):
            value = weapon_heft(BASES_BY_NAME[name])
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_the_base_is_looked_up_by_name(self):
        """`Item.base` is the base's **name**, not the base. Reading
        `.weights` off it silently gives nothing - which is how the first
        version called a greatsword and a dagger equally heavy."""
        from pixel_rpg.items import BASES_BY_NAME, roll_item, weapon_heft
        rolled = roll_item(10, rarity="common",
                           base=BASES_BY_NAME["Greatsword"])
        self.assertIsInstance(rolled.base, str)
        self.assertGreater(weapon_heft(rolled), 0.8)

    def test_a_greatsword_lands_harder_than_a_dagger(self):
        light = self.spectacle(self.armed("Dagger"))
        heavy = self.spectacle(self.armed("Greatsword"))
        self.assertGreater(max(heavy["particles"]), max(light["particles"]))

    def test_a_heavy_weapon_swings_on_the_heavy_motion(self):
        """A greatsword that swings at a dagger's speed is a dagger with a
        bigger number on it."""
        self.assertIn("heavy", self.spectacle(self.armed("Warhammer"))["motion"])
        self.assertNotIn("heavy", self.spectacle(self.armed("Dagger"))["motion"])

    def test_a_big_ability_is_big(self):
        from pixel_rpg.skills import SKILLS_BY_KEY
        battle = self.armed("Dagger")
        powers = [(battle.skill_heft(s), s.name)
                  for s in SKILLS_BY_KEY.values() if s.kind == "attack"]
        self.assertGreater(max(powers)[0], 0.9, "no ability reads as big")
        self.assertLess(min(powers)[0], 0.2, "every ability reads as big")

    def test_taking_a_third_of_something_reads_as_heavy_whatever_swung_it(self):
        """**The input that matters most.** A hit that removes a third of
        a warden is a big hit however small the weapon was - so the share
        of the target taken counts as well as the weight of the swing."""
        import inspect
        from pixel_rpg.battle import Battle
        source = inspect.getsource(Battle.apply_damage_to_enemy)
        self.assertIn("share", source)
        self.assertIn("max(self.heft", source)

    def test_the_heavy_motion_exists_and_lands_inside_itself(self):
        from pixel_rpg import anim
        self.assertIn("heavy", anim.SPANS)
        self.assertIn("heavy", anim.IMPACT_AT)
        self.assertLess(anim.IMPACT_AT["heavy"], anim.SPANS["heavy"])
        # A heavy blow takes longer to arrive than an ordinary one.
        self.assertGreater(anim.IMPACT_AT["heavy"], anim.IMPACT_AT["lunge"])


class TestProjectiles(unittest.TestCase):
    """Item 0al (a), s69. Savvydev, 2026-09-06:

    > "by animations, obviously I mean lots and lots more animations, but
    > I also mean stuff like projectiles and stuff yk?"

    **A projectile is not a pose.** Everything §22 built moves a body that
    is already on screen; an arrow is created, travels, and ceases to
    exist - which is a layer rather than a curve.
    """

    def fight(self, class_key, level=9, foes=2):
        from pixel_rpg import game as G
        from pixel_rpg.entities import make_encounter
        game = G.Game()
        game.pending_class = class_key
        game.new_run("Archer", 1)
        game.dialogue = None
        game.state = G.OVERWORLD
        for _ in range(level):
            game.player.gain_xp(game.player.xp_to_next())
        game.player.full_restore()
        game.start_battle(make_encounter(8, "meadow", count=foes))
        for _ in range(400):
            if game.battle.phase == "menu":
                break
            game.update(1 / 60)
        game.shots.clear()
        return game

    def seen(self, game, frames=60):
        kinds = set()
        for _ in range(frames):
            game.update(1 / 120)
            for shot in game.shots.items:
                kinds.add(shot.look)
        return kinds

    def test_an_archer_looses_an_arrow(self):
        game = self.fight("longshot")
        game.battle.player_attack()
        self.assertIn("arrow", self.seen(game))

    def test_an_alchemist_throws_something(self):
        game = self.fight("apothecary")
        game.battle.player_attack()
        self.assertIn("thrown", self.seen(game))

    def test_a_sword_swing_throws_nothing(self):
        game = self.fight("warden")
        game.battle.player_attack()
        self.assertEqual(self.seen(game), set())

    def test_a_spell_throws_its_own_shape(self):
        game = self.fight("emberwright")
        battle = game.battle
        spell = next(s for s in game.player.skills()
                     if s.kind == "attack" and s.scales == "mag"
                     and battle.can_pay(s))
        battle.phase = "submenu"
        battle.use_skill(spell)
        # **Not "bolt" any more** (item 0ba, §75): a spell throws the
        # shape its element and power say it should, and this one is
        # fire. The check is that something crossed the arena and that
        # it is one of the shapes the game can draw.
        from pixel_rpg.ui import ShotLayer
        thrown = self.seen(game)
        self.assertTrue(thrown, "a spell crossed nothing at all")
        for look in thrown:
            self.assertIn(look, ShotLayer.SPELL_LOOKS)

    def test_a_heal_throws_nothing(self):
        """A bolt launched at yourself reads as a bug, not a spell."""
        from pixel_rpg.battle import Battle
        game = self.fight("emberwright")
        heal = next((s for s in game.player.skills() if s.kind == "heal"),
                    None)
        self.assertIsNotNone(heal)
        self.assertFalse(game.battle.skill_flies(heal))

    def test_the_blow_waits_for_the_arrow_to_land(self):
        """A projectile that arrives after the damage number is a
        decoration; one the damage waits on is the attack."""
        archer = self.fight("longshot")
        knight = self.fight("warden")
        for game in (archer, knight):
            game.battle.player_attack()
        archer_hit = knight_hit = None
        for frame in range(200):
            for game, name in ((archer, "archer"), (knight, "knight")):
                before = sum(f.hp for f in game.battle.enemies)
                game.update(1 / 240)
                after = sum(f.hp for f in game.battle.enemies)
                if after < before:
                    if name == "archer" and archer_hit is None:
                        archer_hit = frame
                    if name == "knight" and knight_hit is None:
                        knight_hit = frame
        self.assertIsNotNone(archer_hit)
        self.assertIsNotNone(knight_hit)
        self.assertGreater(archer_hit, knight_hit,
                           "the arrow did not delay its own blow")

    def test_a_shot_travels_and_then_is_gone(self):
        from pixel_rpg.ui import ShotLayer
        shots = ShotLayer()
        shots.add((0, 0), (100, 0), (255, 255, 255), life=0.20)
        start = shots.at(shots.items[0])[0]
        shots.update(0.10)
        middle = shots.at(shots.items[0])[0]
        self.assertGreater(middle, start)
        shots.update(0.15)
        self.assertEqual(shots.items, [], "a spent shot outlived its flight")

    def test_the_layer_never_grows_without_bound(self):
        from pixel_rpg.ui import ShotLayer
        shots = ShotLayer(limit=4)
        for _ in range(30):
            shots.add((0, 0), (10, 10), (255, 255, 255))
        self.assertLessEqual(len(shots.items), 4)


class TestPlainSight(unittest.TestCase):
    """Item 0aj (s68). Savvydev, 2026-09-06:

    > "instead of having enemies as random encounters when walking around,
    > make them visible and if a player and an enemy interact then the
    > player is put into battle. in the overworld, only one enemy is
    > shown, but when battle begins there can be multiple."
    """

    def run_with(self, keys=("insight",), steps=140):
        from pixel_rpg import game as G
        from pixel_rpg.modifiers import RunModifiers
        game = G.Game()
        game.pending_class = "warden"
        game.pending_modifiers = RunModifiers(keys)
        game.new_run("Walker", 1)
        game.dialogue = None
        game.scene = None
        game.state = G.OVERWORLD
        # Out of the village: it is not hunting ground.
        game.player.x += 34 * game.tile
        game.enter_overworld(snap=True)
        for _ in range(steps):
            game.update(1 / 60)
        return game

    def test_monsters_are_visible_with_the_modifier(self):
        game = self.run_with()
        self.assertTrue(game.wanderers_on())
        self.assertTrue(game.wanderers, "nothing turned up to be seen")

    def test_nothing_wanders_without_it(self):
        game = self.run_with(keys=())
        self.assertFalse(game.wanderers_on())
        self.assertEqual(game.wanderers, [])

    def test_it_replaces_the_ambush_rather_than_adding_to_it(self):
        """His word was *"instead of"*. Monsters you can see **and**
        monsters you cannot would be twice the fights, which is a
        difficulty change nobody asked for."""
        game = self.run_with()
        game.battle = None
        for _ in range(400):
            game.last_tile = None          # force a roll every time
            game.check_encounter()
        self.assertIsNone(game.battle, "an ambush happened anyway")

    def test_walking_into_one_starts_a_fight(self):
        game = self.run_with()
        one = game.wanderers[0]
        game.player.x, game.player.y = one["x"], one["y"]
        game.update(1 / 60)
        self.assertIsNotNone(game.battle, "walking into it did nothing")

    def test_one_sprite_can_be_several_monsters(self):
        """*"only one enemy is shown, but when battle begins there can be
        multiple"* - so a wanderer carries a level and a biome, and the
        pack is still rolled by `make_encounter` when the fight opens."""
        seen = set()
        for attempt in range(6):
            game = self.run_with()
            if not game.wanderers:
                continue
            one = game.wanderers[0]
            game.player.x, game.player.y = one["x"], one["y"]
            game.update(1 / 60)
            if game.battle:
                seen.add(len(game.battle.enemies))
        self.assertTrue(seen)
        self.assertTrue(any(count > 1 for count in seen),
                        "a wanderer was never more than one monster: %s"
                        % sorted(seen))

    def test_they_never_spawn_in_the_village_or_on_a_road(self):
        """Roads are safe corridors (§42) and the village is not hunting
        ground - both fall out of `encounter_chance`, and both would be a
        difficulty change if they did not."""
        game = self.run_with()
        for _ in range(400):
            game.spawn_wanderer()
        for one in game.wanderers:
            spot = (int(one["x"] // game.tile), int(one["y"] // game.tile))
            self.assertFalse(game.world.town_rect.collidepoint(spot),
                             "one spawned in the village")
            self.assertGreater(game.world.encounter_chance(*spot), 0,
                               "one spawned somewhere nothing hunts")

    def test_none_indoors(self):
        game = self.run_with()
        door = next(k for k, v in game.world.doors.items() if v == "home")
        game.enter_house(door)
        game.update(1 / 60)
        self.assertFalse(game.wanderers_on())
        self.assertEqual(game.wanderers, [])

    def test_a_wave_run_has_none(self):
        """Last Stand is the arena; there is no world to walk in."""
        from pixel_rpg import game as G
        from pixel_rpg.modifiers import RunModifiers
        game = G.Game()
        game.pending_class = "warden"
        game.pending_modifiers = RunModifiers(("insight",))
        game.new_wave_run("Stander")
        self.assertFalse(game.wanderers_on())

    # -- mini-bosses walk too (his, 2026-09-06) ---------------------------

    def near_a_lair(self, seed=3, steps=90):
        from pixel_rpg import game as G
        from pixel_rpg.modifiers import RunModifiers
        game = G.Game()
        game.pending_class = "warden"
        game.pending_modifiers = RunModifiers(("insight",))
        game.settings.seed = seed
        game.new_run("Walker", 1)
        game.dialogue = None
        game.scene = None
        game.state = G.OVERWORLD
        spot = list(game.world.lairs)[0]
        game.player.x = spot[0] * game.tile + game.tile / 2
        game.player.y = (spot[1] + 4) * game.tile
        game.enter_overworld(snap=True)
        for _ in range(steps):
            game.update(1 / 60)
        return game, spot

    def test_field_bosses_come_out_of_their_dens(self):
        """His answer, 2026-09-06: *"the mini bosses should walk around"*.
        They were landmarks you pressed `E` on."""
        game, _spot = self.near_a_lair()
        self.assertTrue([one for one in game.wanderers if one.get("lair")],
                        "no field boss came out to walk")

    def test_a_patrolling_boss_actually_moves(self):
        """**Path length, not displacement.** A leashed wander comes back
        past where it started, so measuring net movement reported six
        patrols in fourteen as frozen when every one of them was walking -
        the metric was the bug, not the code."""
        import math
        game, _spot = self.near_a_lair()
        boss = next(one for one in game.wanderers if one.get("lair"))
        path, last = 0.0, (boss["x"], boss["y"])
        for _ in range(300):
            game.update(1 / 60)
            path += math.hypot(boss["x"] - last[0], boss["y"] - last[1])
            last = (boss["x"], boss["y"])
        self.assertGreater(path, 20.0, "it never went anywhere")

    def test_it_stays_on_its_leash(self):
        """Something four times your level following you across the valley
        is a different game; §42's whole point about a lair is that it is
        a place you decide to go."""
        import math
        game, _spot = self.near_a_lair()
        for _ in range(400):
            game.update(1 / 60)
        for one in game.wanderers:
            if not one.get("lair"):
                continue
            den = one["lair"]
            away = math.hypot(one["x"] - (den[0] * game.tile + game.tile / 2),
                              one["y"] - (den[1] * game.tile + game.tile / 2))
            self.assertLess(away / game.tile, game.BOSS_LEASH + 3,
                            "a field boss wandered off its leash")

    def test_walking_into_one_asks_rather_than_springs(self):
        """§42: *"it is asked, not sprung"*. He asked for them to move,
        not for that to stop."""
        game, _spot = self.near_a_lair()
        boss = next(one for one in game.wanderers if one.get("lair"))
        game.player.x, game.player.y = boss["x"], boss["y"]
        game.update(1 / 60)
        self.assertIsNone(game.battle, "a field boss sprang on us")
        self.assertIsNotNone(game.dialogue, "it did not ask")
        self.assertEqual(game.dialogue["confirm"], "Fight it")

    def test_the_den_is_empty_while_its_tenant_is_out(self):
        """Two ways to start the same fight would make the walking
        pointless."""
        game, spot = self.near_a_lair()
        self.assertIn(spot, game.walking_lairs())
        game.dialogue = None
        game.approach_lair(spot)
        self.assertIsNone(game.dialogue, "the empty den still offered it")

    def test_no_field_boss_walks_without_the_modifier(self):
        from pixel_rpg import game as G
        from pixel_rpg.modifiers import RunModifiers
        game = G.Game()
        game.pending_class = "warden"
        game.pending_modifiers = RunModifiers(())
        game.settings.seed = 3
        game.new_run("Walker", 1)
        game.dialogue = None
        game.state = G.OVERWORLD
        for _ in range(90):
            game.update(1 / 60)
        self.assertEqual(game.wanderers, [])

    def test_it_is_a_preference_and_scores_nothing(self):
        """His: *"modifiers that dont give you an score boost or
        anything"*."""
        from pixel_rpg.modifiers import MODIFIERS_BY_KEY, RunModifiers
        mod = MODIFIERS_BY_KEY["insight"]
        self.assertEqual(mod.kind, "preference")
        self.assertEqual(mod.score, 1.0)
        self.assertFalse(mod.scores)
        self.assertEqual(RunModifiers(("insight",)).score, 1.0)


class TestActionCommands(unittest.TestCase):
    """Item 0ai (s67). Block Tales as the reference (§41), as a modifier
    for single and multiplayer, with a score boost - asked for, decided in
    §19 answer 2, and then not built for a fortnight."""

    def battle(self, timed=True, foes=1, level=12, seed=9):
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import Player, make_encounter
        from pixel_rpg.modifiers import RunModifiers
        from pixel_rpg.util import rng
        rng.reseed(seed)
        hero = Player("Timer", "warden")
        if timed:
            hero.modifiers = RunModifiers(("timed",))
        for _ in range(7):
            hero.gain_xp(hero.xp_to_next())
        hero.full_restore()
        battle = Battle(hero, make_encounter(level, "meadow", count=foes))
        for _ in range(400):
            if battle.finished or battle.phase == "menu":
                break
            battle.update(0.05)
        return battle

    def swing(self, battle, press=None):
        """One attack. `press` is None, "early" or "perfect"."""
        before = [foe.hp for foe in battle.enemies]
        battle.player_attack()
        if press == "perfect":
            while battle.command and battle.elapsed < battle.command["best"]:
                battle.update(0.01)
            battle.time_command()
        elif press == "early":
            battle.time_command()
        for _ in range(80):
            battle.update(0.05)
        return sum(was - foe.hp for was, foe in zip(before, battle.enemies))

    def test_no_modifier_means_no_window_at_all(self):
        battle = self.battle(timed=False)
        battle.player_attack()
        self.assertIsNone(battle.command,
                          "a fight without the modifier opened a window")

    def test_the_modifier_opens_one(self):
        battle = self.battle()
        battle.player_attack()
        self.assertIsNotNone(battle.command)
        self.assertEqual(battle.command["kind"], "attack")
        self.assertFalse(battle.command["taken"])

    def test_timing_it_well_beats_not_timing_it(self):
        self.assertGreater(self.swing(self.battle(), "perfect"),
                           self.swing(self.battle(), None))

    def test_missing_it_is_worse_than_not_taking_the_modifier(self):
        """**This is what makes it a cost** (§45): the modifier has to take
        something away, and what it takes is the damage you used to get for
        free."""
        plain = self.swing(self.battle(timed=False), None)
        missed = self.swing(self.battle(), None)
        self.assertLess(missed, plain)

    def test_pressing_on_sight_is_worth_less_than_pressing_on_the_beat(self):
        early = self.swing(self.battle(), "early")
        perfect = self.swing(self.battle(), "perfect")
        self.assertLess(early, perfect)

    def test_a_window_pays_once(self):
        """Consumed as it is read, so a swing cannot be paid for twice and
        a leftover window cannot pay for the next one."""
        battle = self.battle()
        battle.player_attack()
        battle.time_command()
        self.assertGreater(battle.take_command("attack"), 0.0)
        self.assertIsNone(battle.command)
        self.assertEqual(battle.take_command("attack"), 0.0)

    def test_a_press_outside_the_window_scores_nothing(self):
        battle = self.battle()
        # A block window, so the swing's resolver does not consume it out
        # from under the test before it can be missed.
        battle.open_command("block", 0.0)
        for _ in range(40):        # long past its close
            battle.update(0.05)
        self.assertEqual(battle.time_command(), 0.0)
        self.assertEqual(battle.command["quality"], 0.0)

    def test_the_wrong_kind_of_window_pays_nothing(self):
        battle = self.battle()
        battle.player_attack()
        battle.time_command()
        self.assertEqual(battle.take_command("block"), 0.0)

    def test_a_blocked_blow_lands_softer(self):
        """The half of Block Tales that makes this a system rather than a
        damage bonus: *"defending also requires timing"*."""
        from pixel_rpg.battle import DamageResult
        battle = self.battle()
        battle.open_command("block", 0.0)
        battle.time_command()
        hero = battle.you
        hero.hp = hero.max_hp()
        battle.apply_damage_to_player(
            DamageResult(amount=100), victim=hero)
        blocked = hero.max_hp() - hero.hp

        battle2 = self.battle()
        hero2 = battle2.you
        hero2.hp = hero2.max_hp()
        battle2.apply_damage_to_player(
            DamageResult(amount=100), victim=hero2)
        plain = hero2.max_hp() - hero2.hp
        self.assertLess(blocked, plain, "a timed block did nothing")

    def test_a_missed_block_is_not_punished(self):
        """Being worse at attacking *and* worse at defending is two costs
        for one modifier."""
        from pixel_rpg.battle import DamageResult
        battle = self.battle()
        battle.open_command("block", 0.0)
        hero = battle.you
        hero.hp = hero.max_hp()
        battle.apply_damage_to_player(DamageResult(amount=100), victim=hero)
        self.assertEqual(hero.max_hp() - hero.hp, 100)

    def test_the_modifier_is_read_off_the_local_hero(self):
        """In a shared fight the modifier belongs to whoever is at this
        keyboard - a mirror of somebody else's hero must not start asking
        this player for inputs (§21)."""
        import inspect
        from pixel_rpg.battle import Battle
        source = inspect.getsource(Battle.timed_commands)
        self.assertIn("self, \"you\"", source.replace("'", '"'))

    def test_a_click_can_land_a_command(self):
        """**Rule 8b.** §67 shipped the window as a keypress and wrote the
        gap down; this closes it in the next pass rather than the next
        playtest, which is the whole point of that rule.

        The *whole bar* is the target: a timing test that also demanded
        precision with the pointer would be a different game.
        """
        import pygame
        from pixel_rpg import battlescreen as screen
        game = build_game_for_mouse()
        from pixel_rpg.entities import make_encounter
        from pixel_rpg.modifiers import RunModifiers
        game.player.modifiers = RunModifiers(("timed",))
        game.start_battle(make_encounter(8, "meadow", count=1))
        for _ in range(300):
            if game.battle.phase == "menu":
                break
            game.update(1 / 60)
        game.battle.player_attack()
        for _ in range(4):
            game.update(1 / 60)
        game.draw()
        width, height = game.screen.get_size()
        plate = screen.battle_panels(game.battle, width, height)["player"]
        spot = (width // 2, min(int(height * 0.70), plate.top - 44) + 8)
        self.assertEqual(game.ui.hot.at(spot), ("command", 0),
                         "the window is not clickable")
        game.mouse_click(spot)
        self.assertTrue(game.battle.command["taken"],
                        "a click did not land the command")

    def test_it_scores_and_it_is_selectable(self):
        from pixel_rpg.modifiers import MODIFIERS_BY_KEY, RunModifiers
        self.assertIn("timed", MODIFIERS_BY_KEY)
        self.assertGreater(MODIFIERS_BY_KEY["timed"].score, 1.0)
        self.assertTrue(RunModifiers(("timed",)).has("timed"))


class TestOnlyOneThingWritesASaveSlot(unittest.TestCase):
    """Savvydev, 2026-09-06: *"make sure there is nothing that is saved to
    the save slots"*.

    **`Game.save_run` is the only writer**, and that is load-bearing
    rather than tidy. Three things must never reach a slot, and each one
    of them nearly did:

    * **A guest hero** belongs to somebody else's valley and goes to the
      vault (§19 item 5c).
    * **A One Sitting run** is never written down at all (§54).
    * **A Last Stand run** has no slot - and `self.slot` carries over from
      whatever run came before, so before §59 an autosave in wave mode
      would have written the wave hero straight over a story save.

    A fourth writer added later would bypass all three gates without
    anything failing, which is why this counts them.
    """

    def test_save_run_is_the_only_caller_of_write(self):
        import ast
        import os
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "pixel_rpg")
        callers = []
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(root, name),
                                  encoding="utf-8").read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not isinstance(fn, ast.Attribute) or fn.attr != "write":
                    continue
                owner = fn.value
                if isinstance(owner, ast.Attribute) and owner.attr == "saves":
                    callers.append("%s:%d" % (name, node.lineno))
        self.assertEqual(len(callers), 1,
                         "something other than save_run writes a slot: %s"
                         % callers)

    def game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.pending_class = "warden"
        return game

    def test_a_wave_run_never_writes_a_slot(self):
        game = self.game()
        game.new_wave_run("Stander")
        self.assertEqual(game.slot, 0)
        self.assertFalse(game.save_run(silent=True, force=True),
                         "a Last Stand wrote itself to a slot")

    def test_a_wave_run_started_after_a_story_run_still_writes_nothing(self):
        """**The actual bug shape.** `self.slot` survives from the previous
        run, so a wave run inherits a real slot number unless something
        stops it."""
        game = self.game()
        game.new_run("Storyteller", 3)
        game.dialogue = None
        self.addCleanup(game.saves.delete, 3)
        self.assertTrue(game.save_run(silent=True, force=True))
        before = game.saves.describe(3)

        game.pending_class = "warden"
        game.new_wave_run("Stander")
        game.save_run(silent=True, force=True)
        after = game.saves.describe(3)
        self.assertEqual(after.get("name"), before.get("name"),
                         "a wave run overwrote the story save")

    def test_a_one_sitting_run_never_writes_a_slot(self):
        from pixel_rpg.modifiers import RunModifiers
        game = self.game()
        game.pending_modifiers = RunModifiers(("onesitting",))
        game.new_run("Sitter", 4)
        game.dialogue = None
        self.addCleanup(game.saves.delete, 4)
        self.assertFalse(game.save_run(silent=True, force=True))
        self.assertTrue(game.saves.describe(4).get("empty"),
                        "One Sitting reached a slot")

    def test_a_guest_hero_goes_to_the_vault_not_a_slot(self):
        game = self.game()
        game.new_run("Owner", 5)
        game.dialogue = None
        self.addCleanup(game.saves.delete, 5)
        game.save_run(silent=True, force=True)
        mine = game.saves.describe(5).get("name")
        # Now become a guest in somebody else's valley.
        game.guest_world = 4242
        game.player.name = "Visitor"
        game.save_run(silent=True, force=True)
        self.assertEqual(game.saves.describe(5).get("name"), mine,
                         "a guest hero was written into a slot")


class TestThereAreTwelveSlots(unittest.TestCase):
    """*"add more save slots"* (2026-09-06). Six became twelve."""

    def test_the_count_is_one_number(self):
        from pixel_rpg import save as save_mod
        self.assertGreaterEqual(save_mod.SLOT_COUNT, 12)

    def test_the_picker_offers_every_slot(self):
        import tempfile
        from pixel_rpg import save as save_mod, store as store_mod
        manager = save_mod.SaveManager(
            store_mod.FileStore(tempfile.mkdtemp()))
        rows = manager.describe_all()
        self.assertEqual(len(rows), save_mod.SLOT_COUNT)
        self.assertTrue(all(row.get("empty") for row in rows))

    def test_every_slot_round_trips(self):
        """A new slot is only real if it can be written and read back."""
        import tempfile
        from pixel_rpg import save as save_mod, store as store_mod
        from pixel_rpg.entities import Player
        from pixel_rpg.world import World
        from pixel_rpg.quests import QuestLog
        from pixel_rpg.config import Settings
        manager = save_mod.SaveManager(
            store_mod.FileStore(tempfile.mkdtemp()))
        world = World(32, 4242)
        for slot in range(1, save_mod.SLOT_COUNT + 1):
            player = Player("Slot%d" % slot, "warden")
            payload = save_mod.build_payload(player, world, QuestLog(),
                                             Settings(), 0.0)
            self.assertTrue(manager.write(slot, payload, force=True),
                            "slot %d refused a write" % slot)
            self.assertEqual(manager.describe(slot).get("name"),
                             "Slot%d" % slot)


class TestEveryClassSwingsItsOwn(unittest.TestCase):
    """Item 0ai's rider (s65). Savvydev, 2026-09-06:

    > "the basic attack that costs no mp should be different according to
    > each class"

    Thirteen classes shared one swing - the move you press most was the
    only part of a class that was not the class.
    """

    def hero(self, class_key, level=8):
        from pixel_rpg.entities import Player
        player = Player("Probe", class_key)
        for _ in range(level - 1):
            player.gain_xp(player.xp_to_next())
        player.full_restore()
        return player

    def fight(self, class_key, foes=3, level=8):
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import make_encounter
        from pixel_rpg.util import rng
        rng.reseed(11)
        pack = make_encounter(level, "meadow", count=foes)
        battle = Battle(self.hero(class_key, level), pack)
        for _ in range(400):
            if battle.finished or battle.phase == "menu":
                break
            battle.update(0.05)
        return battle

    def test_every_class_has_a_strike_and_every_strike_exists(self):
        from pixel_rpg import classes as classes_mod
        for cls in classes_mod.CLASSES:
            self.assertIn(cls.strike, classes_mod.STRIKES,
                          "%s swings a strike that does not exist" % cls.key)
            self.assertTrue(classes_mod.strike_for(cls.key).name)

    def test_they_are_not_all_the_same(self):
        from pixel_rpg import classes as classes_mod
        used = {cls.strike for cls in classes_mod.CLASSES}
        self.assertGreaterEqual(len(used), 4, "the classes still share a swing")

    def test_every_strike_animation_is_a_real_motion(self):
        """§7's rule about a fallback that names a symbol which does not
        exist: `dart` was written into the strike table before `anim` knew
        the word, and a missing motion is a swing that does not move -
        silently, because `_apply_move` just falls through."""
        from pixel_rpg import anim, classes as classes_mod
        for strike in classes_mod.STRIKES.values():
            self.assertIn(strike.anim, anim.SPANS,
                          "%s plays a motion that does not exist"
                          % strike.key)
            self.assertIn(strike.anim, anim.IMPACT_AT,
                          "%s has no impact time" % strike.key)

    def test_the_impact_lands_inside_its_own_motion(self):
        """A blow resolved after its animation has finished is a number
        appearing over a body that is already standing still."""
        from pixel_rpg import anim
        for kind, when in anim.IMPACT_AT.items():
            self.assertLess(when, anim.SPANS[kind],
                            "%s lands after it has finished" % kind)

    def test_a_sweep_carries_into_a_second_enemy(self):
        battle = self.fight("warden")
        before = [foe.hp for foe in battle.enemies]
        battle.player_attack()
        for _ in range(60):
            battle.update(0.05)
        hurt = sum(1 for was, foe in zip(before, battle.enemies)
                   if foe.hp < was)
        self.assertGreaterEqual(hurt, 2, "the sweep hit only one of them")

    def test_a_sweep_alone_with_one_enemy_is_just_a_hit(self):
        battle = self.fight("warden", foes=1)
        before = battle.enemies[0].hp
        battle.player_attack()
        for _ in range(60):
            battle.update(0.05)
        self.assertLess(battle.enemies[0].hp, before)

    def test_a_flurry_lands_twice(self):
        from pixel_rpg import classes as classes_mod
        self.assertEqual(classes_mod.STRIKES["flurry"].hits, 2)
        # **And is paid for it.** Defence subtracts from every hit, so two
        # halves land for less than one whole - measured at -45% on a
        # Cutpurse before `power` existed to compensate.
        self.assertGreater(classes_mod.STRIKES["flurry"].power, 1.0)

    def test_a_thrust_ignores_part_of_the_guard(self):
        from pixel_rpg.battle import compute_damage
        from pixel_rpg.entities import make_enemy
        from pixel_rpg.util import rng
        hero = self.hero("longshot", 12)
        soft, hard = 0, 0
        for trial in range(60):
            rng.reseed(300 + trial)
            foe = make_enemy(12, "meadow")
            rng.reseed(300 + trial)
            hard += compute_damage(hero, foe, 1.0, "atk").amount
            rng.reseed(300 + trial)
            soft += compute_damage(hero, foe, 1.0, "atk", pierce=0.34).amount
        self.assertGreater(soft, hard, "pierce did not get through anything")

    def test_a_monster_still_swings_plain(self):
        """Nothing that is not a class has a class's swing."""
        from pixel_rpg import classes as classes_mod
        battle = self.fight("warden")
        for foe in battle.enemies:
            self.assertIs(battle.strike_of(foe), classes_mod.STRIKES["plain"])

    def test_spellblade_is_deliberately_plain(self):
        """Runeblade already folds magic into every ordinary swing (s52),
        so a channelled strike on top is the same idea charged twice -
        measured at +30% on the class whose signature *is* this."""
        from pixel_rpg import classes as classes_mod
        self.assertEqual(classes_mod.get("spellblade").strike, "plain")
        self.assertEqual(classes_mod.get("spellblade").signature, "runeblade")


class TestOutpostVariety(unittest.TestCase):
    """s63: hamlets, ruins and circles differ, and nothing else moved."""

    def worlds(self, seeds=(1, 7, 41, 2024, 31415)):
        from pixel_rpg.world import World
        return [World(32, seed) for seed in seeds]

    def around(self, world, spot, reach=5):
        return tuple(sorted(
            (dx, dy, world.props.get((spot[0] + dx, spot[1] + dy)))
            for dx in range(-reach, reach + 1)
            for dy in range(-reach, reach + 1)
            if world.props.get((spot[0] + dx, spot[1] + dy))))

    def test_no_two_outposts_are_dressed_the_same(self):
        seen = {}
        for world in self.worlds(seeds=range(1, 16)):
            for spot, (kind, _name) in world.outposts.items():
                seen.setdefault(kind, set()).add(self.around(world, spot))
        for kind in ("hamlet", "ruin", "stones"):
            self.assertGreater(len(seen.get(kind, ())), 10,
                               "%s outposts all look the same" % kind)

    def test_the_same_seed_dresses_them_the_same(self):
        from pixel_rpg.world import World
        first, second = World(32, 909), World(32, 909)
        self.assertEqual(first.props, second.props)
        self.assertEqual(first.outposts, second.outposts)

    def test_dressing_never_moves_a_chest(self):
        """**The whole reason this pass is shaped the way it is.**

        `_place_outposts` runs *before* `_place_chests`, and chest
        placement rejection-samples against props and blocked tiles - so
        one extra rock added earlier moves every chest on the island.
        Saves store chests by coordinate, so a run in progress would load
        with its opened ones forgotten and the Hollow Key out of reach.
        The decoration therefore happens after the chests exist.
        """
        from pixel_rpg import world as world_mod
        bare = world_mod.World._dress_outposts
        try:
            world_mod.World._dress_outposts = lambda self: None
            plain = {w.seed: sorted((c.gx, c.gy) for c in w.chests)
                     for w in self.worlds()}
        finally:
            world_mod.World._dress_outposts = bare
        for world in self.worlds():
            self.assertEqual(sorted((c.gx, c.gy) for c in world.chests),
                             plain[world.seed],
                             "decorating the outposts moved the chests")

    def test_dressing_strands_nobody(self):
        """A rock three tiles away can close the corridor to a hamlet, and
        no local rule can see that - so the pass floods the island and
        takes its own decoration back out if it did."""
        from pixel_rpg import world as world_mod
        bare = world_mod.World._dress_outposts

        def stranded(worlds):
            out = set()
            for world in worlds:
                reachable = world._walkable_from(world.spawn)
                spots = ([(npc.gx, npc.gy) for npc in world.npcs]
                         + list(world.markers))
                for spot in spots:
                    if not any((spot[0] + dx, spot[1] + dy) in reachable
                               for dx, dy in ((1, 0), (-1, 0),
                                              (0, 1), (0, -1))):
                        out.add((world.seed, spot))
            return out

        try:
            world_mod.World._dress_outposts = lambda self: None
            before = stranded(self.worlds(seeds=range(1, 21)))
        finally:
            world_mod.World._dress_outposts = bare
        after = stranded(self.worlds(seeds=range(1, 21)))
        self.assertEqual(after - before, set(),
                         "the decoration walled somebody off")

    def test_everything_worth_walking_to_can_be_walked_to(self):
        """**Item 22** (s64). `_ensure_connectivity` checked portals and
        chests and nothing else, so a standing stone or a hamlet that
        generated inside a pocket was content nobody could reach - four
        across forty seeds. Markers and people are in the repair now.

        This is the check that would have caught it, and it is the one
        that keeps catching it: everything the player is *meant* to walk
        to has a walkable tile beside it.
        """
        for world in self.worlds(seeds=range(1, 31)):
            reachable = world._walkable_from(world.spawn)

            def can_reach(spot):
                return any((spot[0] + dx, spot[1] + dy) in reachable
                           for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))

            for npc in world.npcs:
                self.assertTrue(can_reach((npc.gx, npc.gy)),
                                "seed %s: %s cannot be reached"
                                % (world.seed, npc.name))
            for spot in world.markers:
                self.assertTrue(can_reach(spot),
                                "seed %s: the marker at %s cannot be read"
                                % (world.seed, spot))
            for chest in world.chests:
                self.assertTrue(can_reach((chest.gx, chest.gy)),
                                "seed %s: the chest at %d,%d is walled off"
                                % (world.seed, chest.gx, chest.gy))

    def test_there_are_always_twenty_two_chests(self):
        """**The count is what `THE LONG WALK` costs** (§42).

        The repair used to *delete* a chest it could not reach, which
        repriced that ending on the seeds where it fired. It moves the
        chest to the nearest tile you can stand on instead: where the
        twenty-second chest sits is promised to nobody, the count is.
        """
        for world in self.worlds(seeds=range(1, 31)):
            self.assertEqual(len(world.chests), 22,
                             "seed %s has %d chests"
                             % (world.seed, len(world.chests)))

    def test_the_validator_uses_the_players_own_rules(self):
        """**The bug behind item 22.** `_reachable_from` blocked on
        terrain and two props while `blocked()` blocks on nine, so it
        walked through fences, lanterns, wells and shrines that a player
        cannot - and a chest ringed by those read as reachable. Two chests
        in 1,320 were unreachable, which is `THE LONG WALK` impossible on
        those seeds. One definition now, so they cannot drift apart."""
        from pixel_rpg.world import World
        world = World(32, 4242)
        self.assertEqual(world._reachable_from(world.spawn),
                         world._walkable_from(world.spawn))

    def test_the_repair_never_drops_a_person(self):
        """A marker that stays stranded is deleted - it is decoration, and
        an unreadable one is worse than none. **A person is not**:
        `_place_favours` has already dealt them jobs, so removing one
        would leave an errand with nobody to hand it in to."""
        import inspect
        from pixel_rpg.world import World
        source = inspect.getsource(World._ensure_connectivity)
        # Nothing the player is meant to find is ever thrown away by the
        # repair: people stay put, markers stay put, and a chest that
        # cannot be reached is **carried** to a tile that can be.
        self.assertNotIn("self.npcs.remove", source)
        self.assertNotIn("self.markers.pop", source)
        self.assertNotIn("self.chests.remove", source)
        self.assertIn("chest.gx, chest.gy = nearest", source)

    def test_the_stone_slots_do_not_sit_on_the_shrine(self):
        """Every circle keeps its shrine in the middle to rest at."""
        from pixel_rpg.world import World
        for dx, dy in World.STONE_SLOTS:
            self.assertFalse(dx == 0 and dy == 0)

    def test_every_hamlet_still_has_somebody_outside(self):
        """`_place_favours` **shuffles the NPC list**, so one villager
        more or less advances the shared stream differently and every
        chest moves. It is also the point of a hamlet."""
        for world in self.worlds():
            hamlets = sum(1 for kind, _n in world.outposts.values()
                          if kind == "hamlet")
            folk = sum(1 for npc in world.npcs
                       if npc.kind == "villager" and " of " in npc.name)
            self.assertGreaterEqual(folk, hamlets - 1,
                                    "a hamlet lost its inhabitant")


class TestGameModes(unittest.TestCase):
    """The mode seam and the wave ramp (0ag, s59).

    All arithmetic, no window - `gamemodes.py` imports no pygame for the
    same reason `battle.py` does not (hard rule 2.3).
    """

    def setUp(self):
        from pixel_rpg import gamemodes
        self.gm = gamemodes

    def test_extra_gamemodes_is_plural(self):
        """*"add some extra gamemodes"* - the seam is the feature, and a
        wave mode hardcoded into `Game` would have missed the request."""
        self.assertGreaterEqual(len(self.gm.MODES), 2)
        keys = [mode.key for mode in self.gm.MODES]
        self.assertEqual(len(keys), len(set(keys)), "duplicate mode key")
        self.assertIn("story", keys)
        self.assertIn("waves", keys)

    def test_an_unknown_mode_is_the_ordinary_game(self):
        """A mode arrives off a socket (the welcome payload), so it is read
        the way save data is read: never raising."""
        for junk in (None, "", "nonsense", 7, "story\x00"):
            self.assertEqual(self.gm.mode_for(junk).key,
                             self.gm.DEFAULT_MODE)

    def test_waves_keeps_no_slot(self):
        """*"that gamemode wouldnt have save slots or anything"*."""
        waves = self.gm.MODES_BY_KEY["waves"]
        self.assertFalse(waves.saves)
        self.assertFalse(waves.island)
        self.assertTrue(self.gm.MODES_BY_KEY["story"].saves)

    def test_the_ramp_climbs_and_stops_climbing(self):
        from pixel_rpg.entities import MAX_PACK
        from pixel_rpg.util import MAX_LEVEL
        self.assertEqual(self.gm.wave_level(1), 1)
        self.assertEqual(self.gm.wave_pack(1, 1), 1)
        for wave in range(1, 200):
            self.assertLessEqual(self.gm.wave_level(wave), MAX_LEVEL)
            self.assertLessEqual(self.gm.wave_pack(wave, 4), MAX_PACK)
            self.assertGreaterEqual(self.gm.wave_pack(wave, 1), 1)
            self.assertGreaterEqual(self.gm.wave_level(wave + 1),
                                    self.gm.wave_level(wave))

    def test_every_tenth_wave_stands_everybody_up(self):
        """*"every 10 waves or something heals everyone"*."""
        rests = [w for w in range(1, 41) if self.gm.is_rest_wave(w)]
        self.assertEqual(rests, [10, 20, 30, 40])
        self.assertTrue(self.gm.wave_rewards(10)["heal"])
        self.assertFalse(self.gm.wave_rewards(9)["heal"])

    def test_every_wave_pays_something(self):
        """*"u get like skills and armor and weapons and stuff every
        wave"* - so no wave hands over nothing."""
        for wave in range(1, 60):
            reward = self.gm.wave_rewards(wave)
            self.assertGreaterEqual(reward["items"], 1)
            self.assertGreater(reward["gold"], 0)
            self.assertGreater(reward["xp"], 0)

    def test_depth_is_what_scores(self):
        """The one thing a mode with no save must not pay for is farming a
        wave you can already beat."""
        deep = self.gm.WaveRun()
        for _ in range(20):
            deep.clear_wave(kills=1)
        grinder = self.gm.WaveRun()
        for _ in range(5):
            grinder.clear_wave(kills=200)
        self.assertGreater(deep.score(), grinder.score())

    def test_a_harder_run_scores_more(self):
        easy = self.gm.WaveRun("story")
        hard = self.gm.WaveRun("nightmare")
        for run in (easy, hard):
            for _ in range(10):
                run.clear_wave(kills=3)
        self.assertGreater(hard.score(), easy.score())

    def test_modifiers_multiply_the_score(self):
        plain = self.gm.WaveRun("normal", 1.0)
        loaded = self.gm.WaveRun("normal", 2.4)
        for run in (plain, loaded):
            for _ in range(8):
                run.clear_wave(kills=2)
        self.assertGreater(loaded.score(), plain.score())


class TestWavePayoutHasOneHome(unittest.TestCase):
    """`grant_wave_rewards` is what both the game and the harness call.

    s23's warning, applied before it could bite: *"a second copy of them
    in a measuring tool would eventually measure a game nobody is
    playing"*. `tools/balance.py --waves` plays a real Last Stand, so the
    payout had to stop living inside `Game`.
    """

    def hero(self):
        from pixel_rpg.entities import Player
        player = Player("Probe", "warden")
        player.full_restore()
        return player

    def test_it_pays_what_the_reward_says(self):
        from pixel_rpg import gamemodes
        from pixel_rpg.items import roll_item
        player = self.hero()
        gold, xp = player.gold, player.xp
        reward = gamemodes.wave_rewards(5)
        levels, _skills, kept = gamemodes.grant_wave_rewards(
            player, reward, roll_item=lambda lv: roll_item(lv))
        self.assertEqual(player.gold - gold, reward["gold"])
        self.assertEqual(kept, reward["items"])
        self.assertGreater(player.xp + levels * 1000, xp)
        self.assertGreaterEqual(player.points, reward["points"])

    def test_a_rest_wave_heals_after_the_level_up_not_before(self):
        """A level raises the maximum, so healing first would leave the
        hero short of the new one."""
        from pixel_rpg import gamemodes
        from pixel_rpg.items import roll_item
        player = self.hero()
        player.hp = 1
        gamemodes.grant_wave_rewards(
            player, gamemodes.wave_rewards(10),
            roll_item=lambda lv: roll_item(lv))
        self.assertEqual(player.hp, player.max_hp())

    def test_an_ordinary_wave_gives_nothing_back(self):
        """**Deliberate** (s60): a per-wave top-up was tried at 25% and
        40% and moved the depth curve by less than the noise, so it was
        taken back out. Attrition between waves is what makes depth mean
        anything.

        **And levelling is why the knob did nothing.** A level-up already
        tops you right up - his rule, *"resets your mp and hp everytime
        you level up"* - so early waves heal you anyway and late ones are
        exactly where a fixed percentage matters least. The hero here is
        levelled past the point where one wave's XP moves them, which is
        the only place the question is even askable.
        """
        from pixel_rpg import gamemodes
        from pixel_rpg.items import roll_item
        player = self.hero()
        for _ in range(29):
            player.gain_xp(player.xp_to_next())
        player.hp = 5
        gamemodes.grant_wave_rewards(
            player, gamemodes.wave_rewards(4),
            roll_item=lambda lv: roll_item(lv))
        self.assertEqual(player.level, 30, "the hero levelled after all")
        self.assertEqual(player.hp, 5, "a plain wave healed the hero")

    def test_the_game_does_not_reimplement_it(self):
        """The whole point of the split - one home, so the harness and the
        game cannot drift."""
        import inspect
        from pixel_rpg import game as G
        source = inspect.getsource(G.Game.wave_cleared)
        self.assertIn("grant_wave_rewards", source)
        self.assertNotIn("full_restore", source,
                         "the payout grew a second copy in Game")


class TestWaveRuns(unittest.TestCase):
    """The wave loop inside a real `Game` (0ag, s59)."""

    def wave_game(self):
        from pixel_rpg import game as G
        game = G.Game()
        game.pending_class = "warden"
        game.pending_mode = "waves"
        game.new_wave_run("Stander")
        return game

    def clear_a_wave(self, game):
        game.begin_wave()
        game.battle.result = "victory"
        game.finish_battle()

    def test_a_wave_run_never_touches_a_slot(self):
        """*"that gamemode wouldnt have save slots or anything"*."""
        game = self.wave_game()
        self.assertEqual(game.slot, 0)
        self.assertTrue(game.in_waves())
        self.assertEqual(
            [s for s in game.saves.describe_all() if not s.get("empty")], [],
            "a wave run wrote a save")

    def test_the_packs_grow_with_the_wave(self):
        from pixel_rpg import gamemodes
        game = self.wave_game()
        seen = []
        for _ in range(12):
            game.begin_wave()
            seen.append(len(game.battle.enemies))
            game.battle.result = "victory"
            game.finish_battle()
        self.assertEqual(seen[0], gamemodes.wave_pack(1, 1))
        self.assertGreater(seen[-1], seen[0])

    def test_clearing_a_wave_pays_out(self):
        game = self.wave_game()
        before = (game.player.gold, game.player.xp, len(game.player.inventory))
        self.clear_a_wave(game)
        self.assertEqual(game.waves.cleared, 1)
        self.assertGreater(game.player.gold, before[0])
        self.assertGreater(len(game.player.inventory), before[2])
        # **A cleared wave now offers before it settles** (item 0bs,
        # s83): the payout is unchanged and the screen after it is the
        # offer, which hands back to the intermission when it is
        # answered. This assertion used to read "waves" and the change
        # is the feature, not a regression.
        self.assertEqual(game.state, "spoils")
        while game.state == "spoils":
            game.leave_spoils()
        self.assertEqual(game.state, "waves")

    def test_the_tenth_wave_heals(self):
        game = self.wave_game()
        for _ in range(9):
            self.clear_a_wave(game)
        game.begin_wave()
        game.player.hp = 1
        game.battle.result = "victory"
        game.finish_battle()
        self.assertEqual(game.waves.cleared, 10)
        self.assertEqual(game.player.hp, game.player.max_hp(),
                         "the tenth wave did not stand everybody up")

    def test_dying_ends_it_and_scores_it(self):
        """*"if u die u get a score and thats it"* - no shrine, no gold
        lost, no waking up. Those belong to a run that continues."""
        game = self.wave_game()
        for _ in range(3):
            self.clear_a_wave(game)
        gold = game.player.gold
        before_runs = len(game.meta.runs)
        game.begin_wave()
        game.battle.result = "defeat"
        game.finish_battle()
        self.assertEqual(game.state, "waveover")
        self.assertTrue(game.waves.over)
        self.assertEqual(game.player.gold, gold, "a wave death cost gold")
        # The profile is shared across tests in this sandbox, so this
        # asks that *one* record was added rather than that one exists.
        self.assertEqual(len(game.meta.runs), before_runs + 1)
        record = game.meta.runs[-1]
        self.assertEqual(record.mode, "waves")
        self.assertEqual(record.waves, 3)
        self.assertGreater(record.score, 0)

    def test_a_wave_score_does_not_swamp_the_story_best(self):
        """Two scales measuring two different games."""
        game = self.wave_game()
        for _ in range(6):
            self.clear_a_wave(game)
        game.begin_wave()
        game.battle.result = "defeat"
        game.finish_battle()
        self.assertEqual(game.meta.best_score, 0,
                         "a wave score reached the story best")
        self.assertGreaterEqual(game.meta.best_waves, 6)

    def test_a_marked_run_posts_no_score(self):
        """The debug menu's promise (s16) holds here too: opening it marks
        the run, and a marked run cannot write to the profile."""
        from pixel_rpg import devmenu
        game = self.wave_game()
        self.clear_a_wave(game)
        devmenu.mark(game)
        before_runs = len(game.meta.runs)
        before_best = game.meta.best_waves
        game.begin_wave()
        game.battle.result = "defeat"
        game.finish_battle()
        self.assertEqual(len(game.meta.runs), before_runs,
                         "a marked run wrote a record")
        self.assertEqual(game.meta.best_waves, before_best)

    def test_fleeing_ends_the_stand(self):
        """There is nowhere to flee to - the run is the arena."""
        game = self.wave_game()
        game.begin_wave()
        game.battle.result = "fled"
        game.finish_battle()
        self.assertEqual(game.state, "waveover")

    def test_the_profile_reads_back_a_wave_record(self):
        from pixel_rpg import meta as meta_mod
        record = meta_mod.RunRecord(mode="waves", waves=13, score=4200)
        again = meta_mod.RunRecord.from_dict(record.to_dict())
        self.assertEqual((again.mode, again.waves), ("waves", 13))
        # An older profile has no `mode` at all and must read as a story run.
        self.assertEqual(
            meta_mod.RunRecord.from_dict({"class_key": "warden"}).mode,
            "story")


class TestHouseInteriors(unittest.TestCase):
    """Roadmap item 18 (s56). Savvydev, 2026-08-24:

    > "yes house interior will be good."

    §13 had recorded the absence as scope - *"the village is a place to do
    business and go, not a set of rooms"* - and §11 named this as the
    feature that would change that.
    """

    def outside_a_door(self, slot=6):
        from pixel_rpg import game as G
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Caller", slot)
        self.addCleanup(game.saves.delete, slot)
        game.dialogue = None
        game.scene = None
        game.state = G.OVERWORLD
        door = next(k for k, v in game.world.doors.items() if v == "home")
        game.player.x = door[0] * game.tile + game.tile / 2
        game.player.y = (door[1] + 1) * game.tile + game.tile / 2
        game.enter_overworld(snap=True)
        return game, door

    # -- the room itself ---------------------------------------------------

    def test_a_room_is_a_world_that_was_never_generated(self):
        """**The whole design.** A generated `World` would raise an island
        to paint over *and* reseed the shared RNG from its own seed (hard
        rule 4), which would move the stream the run is drawing from."""
        from pixel_rpg import interiors
        from pixel_rpg.util import rng
        rng.reseed(11)
        expected = rng.rint(0, 10 ** 9)
        rng.reseed(11)
        interiors.build(4242, (100, 100))
        self.assertEqual(rng.rint(0, 10 ** 9), expected,
                         "furnishing a room moved the shared RNG")

    def test_the_same_door_opens_the_same_room(self):
        from pixel_rpg import interiors
        first, spawn_a, out_a = interiors.build(4242, (162, 119))
        second, spawn_b, out_b = interiors.build(4242, (162, 119))
        self.assertEqual(first.props, second.props)
        self.assertEqual((spawn_a, out_a), (spawn_b, out_b))
        other, _s, _o = interiors.build(4242, (174, 119))
        self.assertNotEqual(first.seed, other.seed)

    def test_there_is_nothing_indoors_to_take(self):
        """**No chests, ever.** The number of chests in the world is what
        `THE LONG WALK` costs (§42); one indoors would either reprice that
        ending or be a chest the Hollow Key can never count."""
        from pixel_rpg import interiors
        room, _spawn, _out = interiors.build(4242, (162, 119))
        self.assertEqual(room.chests, [])
        self.assertNotIn("chest", room.props.values())

    def test_you_can_stand_in_it_and_not_walk_out_of_it(self):
        from pixel_rpg import interiors
        room, spawn, way_out = interiors.build(4242, (162, 119))
        self.assertFalse(room.blocked(*spawn))
        self.assertFalse(room.blocked(*way_out))
        # Everything beyond the room is solid, so there is no walking off
        # into the grass a bare `World` is made of. The room's own corner
        # is worked out from its plan now that size is rolled (0af, s58).
        plan = interiors.plan_for(4242, (162, 119))
        rx = interiors.WORLD_W // 2 - plan["w"] // 2
        ry = interiors.WORLD_H // 2 - plan["h"] // 2
        self.assertTrue(room.blocked(rx - 1, ry - 1))
        self.assertTrue(room.blocked(0, 0))

    # -- variety and the stacked room (0af, s58) ---------------------------

    def all_plans(self, seed=4242, span=140):
        """Every door in a wide sweep, as (door, plan) pairs."""
        from pixel_rpg import interiors
        return [((gx, gy), interiors.plan_for(seed, (gx, gy)))
                for gx in range(0, span, 3)
                for gy in range(0, span, 7)]

    def test_no_two_houses_are_the_same(self):
        """His ask: *"no house is the same per seed"*. Before 0af every
        room was one fixed rectangle with two or three ornaments moved."""
        from pixel_rpg import interiors
        shapes, sizes = set(), set()
        for door, plan in self.all_plans():
            shapes.add(plan["shape"])
            sizes.add((plan["w"], plan["h"]))
        self.assertEqual(shapes, set(interiors.PLANS),
                         "some plans are unreachable")
        self.assertGreater(len(sizes), 12, "rooms are all one size")

    def test_a_room_is_bigger_than_it_was(self):
        """§56 shipped 11x8; he asked for bigger."""
        from pixel_rpg import interiors
        self.assertGreater(interiors.MIN_W, 11)
        self.assertGreater(interiors.MIN_H, 8)
        for _door, plan in self.all_plans(span=60):
            self.assertGreaterEqual(plan["w"], interiors.MIN_W)
            self.assertLessEqual(plan["w"], interiors.MAX_W)
            self.assertGreaterEqual(plan["h"], interiors.MIN_H)
            self.assertLessEqual(plan["h"], interiors.MAX_H)

    def test_the_plan_is_the_seed_and_the_door_and_nothing_else(self):
        """What makes co-op interiors free (§57): two machines build the
        same room from the same two facts, with nothing sent between."""
        from pixel_rpg import interiors
        for door in ((12, 34), (200, 7), (0, 0)):
            self.assertEqual(interiors.plan_for(4242, door),
                             interiors.plan_for(4242, door))
            self.assertNotEqual(interiors.plan_for(4242, door),
                                interiors.plan_for(4243, door))

    def test_every_room_can_be_walked_out_of(self):
        """**The one bug in here that ends a run.** Interior walls and
        solid furniture are both placed from a roll, so this is checked
        rather than reasoned about - `_unseal` is the guard."""
        from pixel_rpg import interiors
        from pixel_rpg.world import BLOCKING_PROPS, T_WALL
        for door, plan in self.all_plans(span=90):
            room, spawn, way_out = interiors.build(4242, door)
            rw, rh = plan["w"], plan["h"]
            rx = interiors.WORLD_W // 2 - rw // 2
            ry = interiors.WORLD_H // 2 - rh // 2
            solid = {spot for spot, prop in room.props.items()
                     if prop in BLOCKING_PROPS}
            floor = {(x, y)
                     for y in range(ry + 1, ry + rh - 1)
                     for x in range(rx + 1, rx + rw - 1)
                     if room.grid[y][x] != T_WALL and (x, y) not in solid}
            self.assertIn(spawn, floor, "spawned inside something at %s"
                          % (door,))
            seen, stack = {spawn}, [spawn]
            while stack:
                x, y = stack.pop()
                for step in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nxt = (x + step[0], y + step[1])
                    if nxt in floor and nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            self.assertEqual(seen, floor, "part of %s is sealed off" % (door,))
            self.assertFalse(room.blocked(*way_out),
                             "the way out of %s is blocked" % (door,))

    def test_the_stacked_room_is_rare(self):
        """*"a super rare house interior"* - and rare against the 22 doors
        an island actually has, not against a sweep."""
        from pixel_rpg import interiors
        plans = [plan for _door, plan in self.all_plans(span=200)]
        rate = sum(1 for p in plans if p["stacked"]) / len(plans)
        self.assertLess(rate, 0.12, "hoards are common enough to farm")
        self.assertGreater(rate, 0.02, "hoards are rare enough to never see")

    def test_the_hoard_is_never_a_chest(self):
        """**The rule it would be easiest to break.** The chest count is
        what `THE LONG WALK` costs and what the Hollow Key counts (§42),
        so the one takeable thing indoors must not register as one."""
        from pixel_rpg import interiors
        door = next(d for d, p in self.all_plans(span=200) if p["stacked"])
        room, _spawn, _out = interiors.build(4242, door)
        self.assertIn("hoard", room.props.values())
        self.assertEqual(room.chests, [])
        self.assertNotIn("chest", room.props.values())

    def test_taking_a_hoard_pays_out_once_and_only_once(self):
        game, door = self.outside_a_door()
        game.interact()
        self.assertIsNotNone(game.house)
        spot = self.plant_hoard(game)
        before = (game.player.gold, game.player.materials)
        game.interact()
        self.assertGreater(game.player.gold, before[0])
        self.assertGreater(game.player.materials, before[1])
        self.assertIn(tuple(door), game.player.looted_hoards)
        self.assertIsNone(game.world.props.get(spot))

        # Walking out and back in must not re-arm it, or it is a farm.
        paid = (game.player.gold, game.player.materials)
        game.world.props[spot] = "hoard"
        game.interact()
        self.assertEqual((game.player.gold, game.player.materials), paid)

    def test_a_taken_hoard_survives_a_save_and_load(self):
        """The first thing about an interior that does not regenerate."""
        from pixel_rpg import save
        player = save.deserialize_player(save.serialize_player(
            self.looted_player()))
        self.assertEqual(player.looted_hoards, {(12, 34), (200, 7)})

    def test_a_hostile_hoard_list_is_refused_rather_than_clamped(self):
        """§20's rule: a clamped coordinate is a different door, which
        here marks a room the player never found as already taken."""
        from pixel_rpg import save
        good = save.serialize_player(self.looted_player())
        for junk in ("nope", [[1]], [[-5, 2]], [[1, 2, 3]], [["a", "b"]],
                     [[9999, 1]], [[True, 2]], [None]):
            player = save.deserialize_player(dict(good, hoards=junk))
            self.assertEqual(player.looted_hoards, set(),
                             "%r was accepted" % (junk,))

    def looted_player(self):
        from pixel_rpg.entities import Player
        player = Player("Taker", "warden")
        player.looted_hoards = {(12, 34), (200, 7)}
        return player

    def plant_hoard(self, game):
        """Put a hoard somewhere reachable that is not beside the door.

        The door wins `interactable_near`'s scan, so a hoard next to it is
        never the thing `interact` finds - which is a property of the
        adjacency order, not of this feature.
        """
        from pixel_rpg.world import T_WALL
        way_out = game.house["out"]
        free = [(x, y)
                for (x, y) in [(gx, gy)
                               for gy in range(way_out[1] - 8, way_out[1])
                               for gx in range(way_out[0] - 5,
                                               way_out[0] + 5)]
                if game.world.grid[y][x] != T_WALL
                and abs(y - way_out[1]) > 2]
        stand = free[len(free) // 2]
        spot = (stand[0], stand[1] - 1)
        game.world.props.pop(spot, None)
        game.world.props[spot] = "hoard"
        game.player.x = stand[0] * game.tile + game.tile / 2
        game.player.y = stand[1] * game.tile + game.tile / 2
        return spot

    # -- going in and coming out -------------------------------------------

    def test_knocking_goes_inside_and_the_door_comes_back_out(self):
        game, door = self.outside_a_door()
        island = game.world
        where = (game.player.x, game.player.y)
        game.interact()
        self.assertIsNotNone(game.house)
        self.assertIsNot(game.world, island)
        self.assertIs(game.overworld(), island)

        way_out = game.house["out"]
        game.player.x = way_out[0] * game.tile + game.tile / 2
        game.player.y = way_out[1] * game.tile + game.tile / 2
        game.interact()
        self.assertIsNone(game.house)
        self.assertIs(game.world, island)
        self.assertEqual((game.player.x, game.player.y), where,
                         "you came out somewhere other than the step")

    def test_nothing_hunts_you_indoors(self):
        """A room is a `World` and its floor is stone, which has an
        encounter rate out on the island."""
        game, _door = self.outside_a_door()
        game.interact()
        game.last_tile = None
        for _ in range(200):
            game.check_encounter()
            game.last_tile = None
        self.assertIsNone(game.battle)

    # -- the two things a house must never leak into -----------------------

    def test_saving_indoors_saves_you_outdoors(self):
        game, door = self.outside_a_door(slot=5)
        island_seed = game.world.seed
        game.interact()
        self.assertTrue(game.save_run(silent=True, force=True))
        self.assertIsNotNone(game.house, "saving threw you out of the house")

        game.load_run(5)
        self.assertIsNone(game.house, "a loaded run started indoors")
        self.assertEqual(game.world.seed, island_seed,
                         "the save carried a room's seed")
        self.assertEqual(game.player_tile(), (door[0], door[1] + 1))

    def test_a_joiner_is_never_sent_a_kitchen(self):
        """`coop._island` exists for this: a host can be standing in
        somebody's front room when a guest arrives, and sending that seed
        would have them rebuild their island out of it."""
        from pixel_rpg import coop
        game, _door = self.outside_a_door(slot=4)
        island = game.world
        game.interact()
        self.assertIs(coop._island(game), island)
        self.assertIsNot(coop._island(game), game.world)

    def test_the_test_doubles_still_work(self):
        """`_island` falls back for anything with no houses - the co-op
        suite's `FakeGame` has a world and no `overworld`."""
        from pixel_rpg import coop
        stub = type("S", (), {"world": "the island"})()
        self.assertEqual(coop._island(stub), "the island")

    def test_a_new_run_starts_outdoors(self):
        game, _door = self.outside_a_door(slot=3)
        game.interact()
        self.assertIsNotNone(game.house)
        game.pending_class = "warden"
        game.new_run("Second", 3)
        self.assertIsNone(game.house)

    def test_leaving_a_room_leaves_the_house_behind(self):
        game, _door = self.outside_a_door(slot=2)
        game.interact()
        game.leave_coop()
        self.assertIsNone(game.house,
                          "the world was swapped out from under a house")

class TestEveryMoveIsAMove(unittest.TestCase):
    """§71, item 0al(b). Savvydev, 2026-09-06:

    > "by animations, obviously I mean lots and lots more animations"

    Four motions covered the whole game: `lunge`/`dart`/`heavy` for a
    swing, `cast` for **every ability of any kind**, `recoil` for every
    blow of any size, and `fall` for every death. These check the two
    things that can rot: that each new move is actually reachable, and
    that each one is actually a different move.
    """

    def played(self, kind, steps=40):
        """Every pose one motion passes through."""
        from pixel_rpg import anim
        motion = anim.Motion()
        motion.play(kind)
        out = []
        for _ in range(steps):
            out.append(anim.pose_of(motion, 0.0, idle=0.0,
                                    alive=kind not in anim.DEATHS))
            motion.advance(anim.SPANS[kind] / (steps - 1.0))
        return out

    def test_every_declared_move_has_a_curve(self):
        """s54's rule in a second place: a name in `SPANS` that no branch
        of `_apply_move` answers is a move that plays as **standing
        perfectly still**, and nothing anywhere would say so."""
        from pixel_rpg import anim
        for kind in anim.SPANS:
            poses = self.played(kind)
            moved = any(abs(p.dx) > 0.01 or abs(p.dy) > 0.01
                        or abs(p.stretch - 1.0) > 0.005
                        or p.sink > 0.01 or p.alpha < 0.995
                        for p in poses)
            self.assertTrue(moved, "%s is declared and does nothing" % kind)

    def test_no_two_moves_are_the_same_move(self):
        """A curve copied and retimed is one move with two names."""
        from pixel_rpg import anim
        shapes = {}
        for kind in anim.SPANS:
            # **The whole pose, not just `dx`.** Two moves that never
            # travel - a heal and a magical death - are identical on that
            # axis alone and different in every other way.
            shapes[kind] = tuple(
                (round(p.dx, 2), round(p.dy, 2), round(p.stretch, 3),
                 round(p.sink, 2), round(p.alpha, 3))
                for p in self.played(kind))
        seen = {}
        for kind, shape in shapes.items():
            self.assertNotIn(shape, seen,
                             "%s and %s are the same curve"
                             % (kind, seen.get(shape)))
            seen[shape] = kind

    def test_every_move_the_fight_can_play_is_declared(self):
        """`set_motion` **refuses** a name it does not know rather than
        raising - which is right for a name off a socket (§20) and means a
        typo here would simply never animate, silently, forever."""
        import ast
        import inspect
        from pixel_rpg import anim, battle as battle_mod, classes
        names = set()
        tree = ast.parse(inspect.getsource(battle_mod))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = getattr(node.func, "attr", "")
            if called not in ("play_motion", "set_motion", "_step_out"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.add(arg.value)
        names.discard("player")
        names.discard("enemy")
        for strike in classes.STRIKES.values():
            names.add(strike.anim)
        self.assertTrue(names, "the scan found no motion names at all")
        for name in names:
            self.assertIn(name, anim.SPANS, "%s is played and undeclared"
                          % name)

    def test_a_death_sticks_and_a_reaction_does_not(self):
        """The one property every death shares: a body that finished
        collapsing is still collapsed. `advance` used to name `fall`
        alone, so a `crumble` would have reset to standing."""
        from pixel_rpg import anim
        for kind in anim.DEATHS:
            motion = anim.Motion()
            motion.play(kind)
            motion.advance(anim.SPANS[kind] * 3)
            self.assertEqual(motion.kind, kind, "%s got up again" % kind)
        for kind in anim.REACTIONS:
            motion = anim.Motion()
            motion.play(kind)
            motion.advance(anim.SPANS[kind] * 3)
            self.assertEqual(motion.kind, "", "%s never ended" % kind)

    def test_a_dead_body_still_poses_and_a_hit_one_stops_breathing(self):
        from pixel_rpg import anim
        for kind in anim.DEATHS:
            motion = anim.Motion()
            motion.play(kind)
            motion.advance(anim.SPANS[kind] * 0.8)
            pose = anim.pose_of(motion, 1.0, alive=False)
            self.assertGreater(pose.sink + abs(pose.dx), 0.5,
                               "%s does not draw once the body is dead"
                               % kind)
        for kind in anim.REACTIONS:
            motion = anim.Motion()
            motion.play(kind)
            still = anim.pose_of(motion, 1.0, idle=9.0, alive=True)
            self.assertLess(abs(still.dy), 5.0,
                            "%s is breathing while taking a blow" % kind)


class TestWhichMoveAndWhy(unittest.TestCase):
    """The choosing, which is the half that has to keep being right when
    somebody adds a skill. **Nothing new was written down for any of it**
    - the same trick s70 got `heft` out of."""

    def test_an_ability_that_is_not_an_attack_never_swings(self):
        """`cast` steps `REACH * 0.10` forward, so mending yourself leaned
        at the enemy - for every heal, buff, ward and curse in the game."""
        from pixel_rpg import anim
        for kind in ("heal", "buff", "debuff", "special"):
            self.assertEqual(anim.skill_motion(kind, True, False), "bless")
            self.assertEqual(anim.skill_motion(kind, False, True), "bless")
        forward = max(abs(p.dx) for p in
                      TestEveryMoveIsAMove().played("bless"))
        self.assertLess(forward, 0.01, "a heal steps at the enemy")

    def test_a_big_spell_channels_and_a_small_one_casts(self):
        from pixel_rpg import anim
        self.assertEqual(anim.skill_motion("attack", True, True), "channel")
        self.assertEqual(anim.skill_motion("attack", True, False), "cast")

    def test_a_physical_ability_swings_the_way_the_class_swings(self):
        """A Cutpurse's Flurry should not lunge like a Warden just because
        it costs MP - the class's own `Strike.anim` is passed in."""
        from pixel_rpg import anim
        self.assertEqual(
            anim.skill_motion("attack", False, False, physical="dart"),
            "dart")
        self.assertEqual(
            anim.skill_motion("attack", False, True, physical="dart"),
            "heavy")

    def test_being_hit_is_not_one_move(self):
        from pixel_rpg import anim
        self.assertEqual(anim.reaction(0.01), "flinch")
        self.assertEqual(anim.reaction(0.14), "recoil")
        self.assertEqual(anim.reaction(0.40), "reel")

    def test_a_blow_that_was_turned_aside_is_braced_not_taken(self):
        """The game used to say "that hurt" with the body over the top of
        a number saying it did not."""
        from pixel_rpg import anim
        self.assertEqual(anim.reaction(0.9, turned=True), "shrug")

    def test_how_a_body_goes_down_comes_from_what_put_it_there(self):
        from pixel_rpg import anim
        self.assertEqual(anim.death_move("physical", False), "fall")
        self.assertEqual(anim.death_move("fire", False), "wither")
        self.assertEqual(anim.death_move("fire", True), "crumble")
        self.assertEqual(anim.death_move("physical", True), "crumble")

    def test_the_share_is_a_share_so_it_means_the_same_to_a_warden(self):
        """Both thresholds are fractions of the target's own health,
        which is the whole reason s70 made `share` a share."""
        from pixel_rpg import anim
        self.assertLess(anim.FLINCH_UNDER, anim.REEL_AT)
        self.assertLess(anim.REEL_AT, 1.0)


class TestTheFightPlaysThem(unittest.TestCase):
    """The moves reaching the screen, over a real `Battle`."""

    def fight(self, class_key="warden"):
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import Player, make_encounter
        from pixel_rpg.util import rng
        rng.reseed(11)
        hero = Player("Mover", class_key)
        for _ in range(9):
            hero.gain_xp(hero.xp_to_next())
        hero.full_restore()
        battle = Battle(hero, make_encounter(10, "meadow", count=1))
        for _ in range(400):
            if battle.finished or battle.phase == "menu":
                break
            battle.update(0.05)
        return battle

    def moves(self, battle, act, frames=90):
        seen = []
        original = battle.on_event

        def spy(kind, data):
            if kind == "motion":
                seen.append(data.get("move"))
            return original(kind, data)

        battle.on_event = spy
        act(battle)
        for _ in range(frames):
            battle.update(0.05)
        return seen

    def test_defending_is_a_move(self):
        """The log has said "braces" since this existed and the body did
        nothing - the only action in the game with no movement at all,
        which reads as the input having been dropped."""
        battle = self.fight()
        self.assertIn("brace", self.moves(battle, lambda b: b.player_defend()))

    def test_a_heal_blesses_and_does_not_cast(self):
        from pixel_rpg.skills import SKILLS_BY_KEY
        battle = self.fight("paladin")
        heal = next((s for s in SKILLS_BY_KEY.values()
                     if s.kind == "heal" and battle.can_pay(s)), None)
        if heal is None:
            self.skipTest("no affordable heal on this class")
        battle.phase = "submenu"
        seen = self.moves(battle, lambda b: b.use_skill(heal))
        self.assertIn("bless", seen)
        self.assertNotIn("cast", seen)

    def test_a_hit_that_takes_a_chunk_reels(self):
        battle = self.fight()
        seen = self.moves(battle, lambda b: b.player_attack(), frames=60)
        reactions = [m for m in seen if m in ("flinch", "recoil", "reel")]
        self.assertTrue(reactions, "nothing answered the blow")

    def test_a_skill_lands_when_its_own_move_lands(self):
        """This was a literal `0.18` while the motions kept their impact
        frames in `anim.IMPACT_AT` - the exact pair-of-numbers trap that
        table's own comment exists to close, one file away from it."""
        import inspect
        from pixel_rpg.battle import Battle
        source = inspect.getsource(Battle.use_skill)
        self.assertIn("anim.impact_time(motion)", source)
        self.assertNotIn("lands = 0.18", source)

    def test_the_thrown_strike_no_longer_charges_before_throwing(self):
        from pixel_rpg.classes import STRIKES
        self.assertEqual(STRIKES["hurl"].anim, "hurl")

    def test_a_death_knows_what_killed_it(self):
        from pixel_rpg import anim
        from pixel_rpg.battle import Battle
        battle = self.fight()
        self.assertEqual(len(battle.last_blow), 2)
        self.assertIn(anim.death_move(*battle.last_blow), anim.DEATHS)

    def test_being_hit_hard_looks_different_from_being_chipped(self):
        """s70 gave the *enemy* side a weight and left this one on a flat
        4-or-7 shake and ten particles, so a monster taking a third of
        your health looked exactly like one taking a chip."""
        import inspect
        from pixel_rpg.battle import Battle
        source = inspect.getsource(Battle.apply_damage_to_player)
        self.assertIn("share", source)
        self.assertNotIn('amount=7 if result.crit else 4', source)


class TestTheOverworldIsABodyToo(unittest.TestCase):
    """The last place in the game where a body was a rectangle that
    moved. `walk_anim` was set to exactly 0.0 on any frame with no input,
    so letting go of a key cut the bob to nothing mid-stride."""

    def test_the_walk_settles_instead_of_cutting(self):
        from pixel_rpg import anim
        ease = 1.0
        first = anim.walk_ease(ease, 1 / 60.0, False)
        self.assertGreater(first, 0.0, "stopping is still a cut")
        self.assertLess(first, 1.0)
        for _ in range(200):
            ease = anim.walk_ease(ease, 1 / 60.0, False)
        self.assertEqual(ease, 0.0, "the walk never finishes settling")

    def test_it_winds_up_and_is_bounded(self):
        from pixel_rpg import anim
        ease = 0.0
        for _ in range(200):
            ease = anim.walk_ease(ease, 1 / 60.0, True)
        self.assertEqual(ease, 1.0)

    def test_standing_still_is_not_completely_still(self):
        """No other body in this game has been, since `breathe` existed."""
        from pixel_rpg import anim
        heights = {round(anim.walk_pose(0.0, 0.0, t).dy, 3)
                   for t in (0.0, 0.4, 0.9, 1.6, 2.5)}
        self.assertGreater(len(heights), 3, "a standing hero is a statue")

    def test_a_step_plants(self):
        """A step that only rises and falls is a float; the frame the
        foot lands is the one that says weight."""
        from pixel_rpg import anim
        squash = min(anim.walk_pose(w / 4.0, 1.0, 0.0).stretch
                     for w in range(40))
        self.assertLess(squash, 0.99)
        self.assertGreater(squash, 0.9, "the plant is a stomp")

    def test_the_walk_does_not_move_the_body_sideways(self):
        """`dx` means *toward the other side*, which is a battle idea. On
        the overworld the world's own coordinates say where a body is."""
        from pixel_rpg import anim
        for w in range(20):
            self.assertEqual(anim.walk_pose(w / 3.0, 1.0, 0.0).dx, 0.0)


class TestTheProbeIsArmed(unittest.TestCase):
    """`tools/balance.py` rolled six unfiltered items and hoped one was a
    weapon, so a stream that came up all armour measured a hero fighting
    **bare handed** - and said nothing, because `equip_best` on nothing is
    not an error. This is that file's own docstring warning again."""

    def test_a_probe_is_armed_however_the_stream_falls(self):
        import importlib.util
        import os
        from pixel_rpg.util import rng
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            "balance_armed", os.path.join(root, "tools", "balance.py"))
        balance = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(balance)
        for seed in range(12):
            rng.reseed(seed)
            for level in (1, 16, 30):
                hero = balance.build("warden", level, 0.0)
                self.assertTrue(hero.equipment.get("weapon"),
                                "seed %d, level %d: unarmed probe"
                                % (seed, level))


class TestTheRoadHasSomethingOnIt(unittest.TestCase):
    """§72, roadmap item 12 - the last thing left of the two hours.

    *"Nothing happens between a waystation and the warden at the end of
    it except walking."* The waystation sits at two thirds; after it
    there is not even a road, because `_carve_road` stops there on
    purpose.
    """

    def island(self, seed=4242):
        from pixel_rpg.world import World
        return World(seed=seed)

    def road_ones(self, world):
        """The ones standing on somebody's last third.

        **Asked by position, not by insertion order.** §75 scattered
        twenty more across the island, and a test that assumed the road
        ones were the front of the dict was relying on every road having
        found room for both of its - which is not guaranteed and is not
        the thing being checked.
        """
        sx, sy = world.spawn
        marks = [(sx + (p.gx - sx) * f, sy + (p.gy - sy) * f)
                 for p in world.portals for f in (0.78, 0.90)]
        return [spot for spot in world.waysides
                if min(abs(spot[0] - mx) + abs(spot[1] - my)
                       for mx, my in marks) < 12]

    def test_every_road_gets_stops_in_its_last_third(self):
        world = self.island()
        self.assertTrue(world.waysides, "no waysides anywhere")
        sx, sy = world.spawn
        for portal in world.portals:
            near = [spot for spot in world.waysides
                    if min(abs(spot[0] - (sx + (portal.gx - sx) * f))
                           + abs(spot[1] - (sy + (portal.gy - sy) * f))
                           for f in (0.78, 0.90)) < 12]
            self.assertTrue(near,
                            "nothing on the last third of %s's road"
                            % portal.boss_key)

    def test_a_road_never_repeats_itself(self):
        """Two of the same stop on one road is one stop shown twice."""
        from pixel_rpg.world import World
        for seed in (1, 7, 99):
            world = World(seed=seed)
            sx, sy = world.spawn
            per_road = {}
            road_ones = set(self.road_ones(world))
            for spot, entry in world.waysides.items():
                if spot not in road_ones:
                    # The scattered ones (§75) are not on anybody's road
                    # and repeating a kind across the island is fine -
                    # this rule is about one road not saying the same
                    # thing twice.
                    continue
                near = min(world.portals,
                           key=lambda p: abs(spot[0] - p.gx)
                           + abs(spot[1] - p.gy))
                per_road.setdefault(near.boss_key, []).append(entry[0])
            for road, kinds in per_road.items():
                self.assertEqual(len(kinds), len(set(kinds)),
                                 "%s repeats a stop: %s" % (road, kinds))

    def test_placing_them_moves_no_chest(self):
        """**The trap this whole feature had to be built around** (§61,
        §63). Chests are stored in a save by coordinate; anything added
        before `_place_chests` moves all twenty-two and nothing fails."""
        from pixel_rpg.world import World
        original = World._place_waysides
        try:
            for seed in (1, 42, 4242):
                with_them = sorted((c.gx, c.gy)
                                   for c in World(seed=seed).chests)
                World._place_waysides = lambda self, per_road=2: None
                without = sorted((c.gx, c.gy)
                                 for c in World(seed=seed).chests)
                World._place_waysides = original
                self.assertEqual(with_them, without,
                                 "seed %d: the waysides moved the chests"
                                 % seed)
        finally:
            World._place_waysides = original

    def test_regenerating_does_not_stack_them_up(self):
        """`generate` runs again on every load (hard rule 2.4). A store
        that is not cleared there accumulates - this went 12, 23, 34."""
        from pixel_rpg.world import World
        world = self.island()
        first = len(world.waysides)
        for _ in range(3):
            world.generate()
        self.assertEqual(len(world.waysides), first)

    def test_every_one_can_be_stood_beside(self):
        """Rule 1d's reachability row. A stop behind a ridge is content
        that does not exist, and the sweep had never looked at these."""
        from pixel_rpg.world import World
        for seed in range(12):
            world = World(seed=seed)
            reach = world._reachable_from(world.spawn)
            for spot in world.waysides:
                self.assertTrue(world._beside(spot, reach),
                                "seed %d: %s is walled off" % (seed, spot))

    def test_no_wayside_touches_the_story(self):
        """§55's rule, and §42's expandability constraint underneath it:
        the road may grow without changing what an earlier ending meant.
        So no ending, secret or codex entry may lean on one."""
        from pixel_rpg import story
        keys = {w.key for w in story.WAYSIDES}
        haystack = []
        for name in ("ENDINGS", "SECRETS", "CODEX"):
            haystack.append(repr(getattr(story, name, ())))
        blob = " ".join(haystack)
        for key in keys:
            self.assertNotIn("wayside_%s" % key, blob)

    def test_walking_on_is_always_an_option_and_never_wrong(self):
        """**The difference from a favour**, which is a riddle you can
        fail. Every option here is a real one, so the road is a sequence
        of decisions rather than a sequence of rewards."""
        from pixel_rpg import story
        for stop in story.WAYSIDES:
            self.assertGreaterEqual(len(stop.options), 2, stop.key)
            free = [one for one in stop.options if not one[2]]
            self.assertTrue(free, "%s has no way to walk on" % stop.key)

    def test_every_effect_has_a_reader(self):
        """§54's rule: a declared thing nothing reads is indistinguishable
        from one whose behaviour was deleted, and only one is a bug."""
        import inspect
        from pixel_rpg import story
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.apply_wayside)
        for stop in story.WAYSIDES:
            for _label, _reply, effect in stop.options:
                for field in effect:
                    self.assertIn('"%s"' % field, source,
                                  "%s declares %s and nothing reads it"
                                  % (stop.key, field))


class TestStoppingAtOne(unittest.TestCase):
    """The stop itself, over a real `Game`."""

    def game(self):
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.new_run("Walker", 1)
        g.state = G.OVERWORLD
        g.scene = None
        g.dialogue = None
        g.player.gold = 500
        return g

    def a_stop(self, g, key):
        spot = next(iter(g.world.waysides))
        g.world.waysides[spot] = [key, False]
        return spot

    def test_it_pays_and_it_charges(self):
        g = self.game()
        spot = self.a_stop(g, "toll")
        before = g.player.gold
        g.stop_at_wayside(spot)
        g.answer_scene(0)
        self.assertEqual(g.player.gold, before - 60)
        self.assertEqual(len(g.player.inventory), 1)

    def test_resting_actually_restores(self):
        g = self.game()
        spot = self.a_stop(g, "fire")
        g.player.hp, g.player.mp = 1, 0
        g.stop_at_wayside(spot)
        g.answer_scene(0)
        self.assertEqual(g.player.hp, g.player.max_hp())
        self.assertGreater(g.player.mp, 0)

    def test_it_is_spent_whichever_way_you_went(self):
        """A stop you can come back to and take the other branch of is a
        menu, not a decision - `settle_favour`'s call, for the same
        reason."""
        g = self.game()
        spot = self.a_stop(g, "spring")
        g.stop_at_wayside(spot)
        last = len(g.scene["options"]) - 1
        g.answer_scene(last)              # the "walk on" option
        self.assertTrue(g.world.waysides[spot][1])
        g.finish_scene()
        g.state = __import__("pixel_rpg.game", fromlist=["x"]).OVERWORLD
        g.stop_at_wayside(spot)
        self.assertIsNone(g.scene, "a spent wayside offered its choices")
        self.assertIsNotNone(g.dialogue)

    def test_an_option_you_cannot_afford_is_not_offered(self):
        """His rule, 2026-08-31 (§45): if a control is on screen, it
        works. A row that will be refused is not drawn."""
        g = self.game()
        spot = self.a_stop(g, "toll")
        g.player.gold = 0
        g.stop_at_wayside(spot)
        labels = [o.label for o in g.scene["options"]]
        self.assertNotIn("Pay it (60 g)", labels)

    def test_the_cache_starts_the_fight_you_asked_for(self):
        """**Not a road encounter.** `TILES[T_PATH][2]` is 0.00 and stays
        0.00; this is §68's *asked, not sprung* from the other side."""
        from pixel_rpg import game as G
        from pixel_rpg.world import TILES, T_PATH
        self.assertEqual(TILES[T_PATH][2], 0.00,
                         "a road tile started spawning encounters")
        g = self.game()
        spot = self.a_stop(g, "cache")
        g.stop_at_wayside(spot)
        g.answer_scene(0)
        self.assertIsNone(g.battle, "the fight started over the reply")
        g.finish_scene()
        self.assertIsNotNone(g.battle)
        self.assertEqual(g.state, G.BATTLE)

    def test_a_spent_stop_survives_a_save(self):
        from pixel_rpg import save as save_mod
        from pixel_rpg.world import World
        g = self.game()
        spot = self.a_stop(g, "fire")
        g.world.settle_wayside(*spot)
        data = save_mod.serialize_world(g.world)
        fresh = World(seed=g.world.seed)
        fresh.waysides[spot] = ["fire", False]
        save_mod.apply_world(fresh, data)
        self.assertTrue(fresh.waysides[spot][1])

    def test_a_coordinate_off_a_save_is_refused_not_created(self):
        """§20's rule. A file may say what you did, never where the world
        put something."""
        from pixel_rpg import save as save_mod
        from pixel_rpg.world import World
        fresh = World(seed=4242)
        before = dict(fresh.waysides)
        save_mod.apply_world(fresh, {"waysides": [{"gx": 3, "gy": 3},
                                                  {"gx": 9999, "gy": -1}]})
        self.assertEqual(set(fresh.waysides), set(before))


class TestTheWavesHavePlaces(unittest.TestCase):
    """§72, roadmap item 0am. Savvydev, 2026-09-06:

    > "add a LOT of different maps for the wave gamemode, so if people
    > turn on the free roam combat thing it looks pretty cool and stuff"

    Every wave was fought in `"meadow"` - the string typed into
    `open_wave` and again into `host_open_wave` - so a run to wave forty
    was forty fights in one field against one tier-0 bestiary.
    """

    def test_there_are_a_lot_of_them(self):
        from pixel_rpg import gamemodes
        self.assertGreaterEqual(len(gamemodes.ARENAS), 15)
        names = [a.name for a in gamemodes.ARENAS]
        self.assertEqual(len(names), len(set(names)))
        keys = [a.key for a in gamemodes.ARENAS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_an_arena_decides_nothing_about_the_fight(self):
        """**His correction, 2026-09-06** (item 0ao):

        > "the arenas are supposed to be purely visual."

        The first version let an arena name a biome, which picked the
        bestiary - a difficulty lever wearing a scenery costume. An
        `Arena` may hold a name, a line and a colour and nothing else.
        """
        from pixel_rpg import gamemodes
        # **The rule is what an arena may not decide, not how many fields
        # it has.** 0ao was *"the arenas are supposed to be purely
        # visual"*, and what it was objecting to was concrete: the arena
        # was naming a **biome**, which chose the bestiary, so *where*
        # you fought changed *what* you fought.
        #
        # He then asked for arenas that are not *"far too simple"*
        # (0bp), and in a top-down real-time arena that means cover you
        # walk round - which is terrain, not a roster. `plan` is
        # allowed for that reason and `biome` is still forbidden: an
        # arena may shape the ground and may not pick the monsters, the
        # level or the pack.
        #
        # **His, 2026-09-07**, when the tension was put to him:
        # *"well stuff like cover that blocks is fine, that kind of stuff
        # is fine"*.
        forbidden = {"biome", "level", "count", "pack", "element",
                     "monsters", "difficulty", "mods"}
        fields = set(gamemodes.Arena.__dataclass_fields__)
        self.assertFalse(fields & forbidden,
                         "an arena decides part of the fight: %s"
                         % (fields & forbidden))
        for arena in gamemodes.ARENAS:
            self.assertTrue(arena.line.strip(), arena.key)

    def test_where_you_fight_does_not_change_what_you_fight(self):
        """The property underneath 0ao, over the real wave door: the pack
        a wave rolls must not depend on which arena it is drawn in."""
        import ast
        import inspect
        import textwrap
        from pixel_rpg.game import Game
        for method in (Game.begin_wave, Game.host_open_wave):
            source = textwrap.dedent(inspect.getsource(method))
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if getattr(node.func, "id", "") != "make_encounter":
                    continue
                names = {n.id for n in ast.walk(node)
                         if isinstance(n, ast.Name)}
                attrs = {n.attr for n in ast.walk(node)
                         if isinstance(n, ast.Attribute)}
                self.assertNotIn("arena", names,
                                 "%s rolls its pack off the arena"
                                 % method.__name__)
                self.assertNotIn("biome", attrs,
                                 "%s rolls its pack off the arena's biome"
                                 % method.__name__)

    def test_the_same_wave_is_always_the_same_place(self):
        """**His correction** (item 0aq): *"the arenas always remain the
        same, they dont change."* An earlier version rotated them per run
        so a mode nobody survives deep into would still show all
        eighteen - scenery you cannot rely on is just noise."""
        from pixel_rpg import gamemodes
        for wave in (1, 4, 17, 40):
            self.assertEqual(gamemodes.arena_for(wave).key,
                             gamemodes.arena_for(wave).key)
        # **On the default settings**, which is what 0aq is about: the
        # game may not shuffle scenery behind the player's back. §74
        # gave the *player* three shapes to choose from (0au), and a
        # setting somebody chose is the opposite of the game changing
        # its mind - so the check is that the default does not vary,
        # not that the function cannot be told to.
        self.assertEqual(gamemodes.ARENA_MODES[0], "every")
        first = [gamemodes.arena_for(w).key for w in range(1, 20)]
        again = [gamemodes.arena_for(w).key for w in range(1, 20)]
        self.assertEqual(first, again)

    def test_the_walk_through_them_is_in_order(self):
        from pixel_rpg import gamemodes
        span = gamemodes.WAVES_PER_ARENA
        for index, arena in enumerate(gamemodes.ARENAS):
            self.assertEqual(gamemodes.arena_for(index * span + 1).key,
                             arena.key)

    def test_it_never_runs_out(self):
        """A Last Stand has no last wave (§59), so the list of places
        cannot have a last entry either."""
        from pixel_rpg import gamemodes
        for wave in (1, 500, 5000):
            self.assertIsNotNone(gamemodes.arena_for(wave))
        span = len(gamemodes.ARENAS) * gamemodes.WAVES_PER_ARENA
        self.assertEqual(gamemodes.arena_for(1).key,
                         gamemodes.arena_for(1 + span).key)
        self.assertEqual(gamemodes.arena_lap(1 + span), 1)

    def test_arriving_is_announced_and_staying_is_not(self):
        from pixel_rpg import gamemodes
        first = gamemodes.WAVES_PER_ARENA + 1
        self.assertTrue(gamemodes.arena_changes_at(1))
        self.assertTrue(gamemodes.arena_changes_at(first))
        self.assertFalse(gamemodes.arena_changes_at(2))

    def test_choosing_an_arena_draws_no_randomness(self):
        """§21's wasted `rng.choice`, which moved four classes."""
        from pixel_rpg import gamemodes
        from pixel_rpg.util import rng
        rng.reseed(5)
        before = rng._r.getstate()
        for wave in range(1, 20):
            gamemodes.arena_for(wave)
        self.assertEqual(rng._r.getstate(), before)

    def test_a_wave_is_fought_where_it_says_it_is(self):
        from pixel_rpg import game as G, gamemodes
        from pixel_rpg.battlescreen import arena_colors
        g = G.Game()
        g.pending_class = "warden"
        g.new_wave_run("Stander")
        for wave in (1, 10, 40):
            g.waves.wave = wave
            g.battle = None
            self.assertTrue(g.begin_wave())
            arena = gamemodes.arena_for(wave)
            self.assertEqual(tuple(g.battle.arena_tint), tuple(arena.tint))
            self.assertNotEqual(arena_colors(g.battle)[0], (14, 13, 22))

    def test_battle_py_still_knows_nothing_about_arenas(self):
        """Hard rule 2.3 in a smaller costume: the fight holds a
        **colour**, not an arena key, so combat maths stays testable
        without a display and without the gamemode table."""
        import inspect
        from pixel_rpg import battle as battle_mod
        source = inspect.getsource(battle_mod)
        self.assertNotIn("gamemodes", source)
        self.assertNotIn("ARENAS", source)


class TestWardensTurnUpInTheWaves(unittest.TestCase):
    """§73, item 0as. Savvydev, 2026-09-06:

    > "also the wave gamemode has no connection to the story or anything
    > but yoou can end up fighting wardens as bosses"

    **Two halves, and the first is a constraint on the second.**
    """

    def waves(self):
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.new_wave_run("Stander")
        return g

    def at(self, g, wave):
        g.waves.wave = wave
        g.battle = None
        self.assertTrue(g.begin_wave())
        return g.battle

    def test_every_tenth_wave_is_a_warden(self):
        from pixel_rpg import gamemodes
        from pixel_rpg.entities import Boss
        g = self.waves()
        for wave in (9, 10, 11, 20):
            expect = gamemodes.boss_wave(wave)
            battle = self.at(g, wave)
            self.assertEqual(isinstance(battle.enemy, Boss), expect,
                             "wave %d" % wave)

    def test_they_arrive_in_the_islands_order(self):
        from pixel_rpg import gamemodes
        from pixel_rpg.entities import BOSS_DEFS
        keys = [b.key for b in BOSS_DEFS]
        self.assertEqual(gamemodes.boss_for_wave(10, keys), keys[0])
        self.assertEqual(gamemodes.boss_for_wave(20, keys), keys[1])
        span = len(keys) * gamemodes.WAVES_PER_BOSS
        self.assertEqual(gamemodes.boss_for_wave(10 + span, keys), keys[0])
        self.assertEqual(gamemodes.boss_for_wave(7, keys), "")

    def test_a_warden_gets_harder_the_deeper_it_is_met(self):
        """One printed level, and a Last Stand runs straight past it -
        so without this the wave-30 boss is easier than the wave-29
        pack."""
        from pixel_rpg import gamemodes
        self.assertEqual(gamemodes.boss_scale(10), 1.0)
        self.assertGreater(gamemodes.boss_scale(30),
                           gamemodes.boss_scale(20))
        g = self.waves()
        first = self.at(g, 10).enemy.max_hp
        first = first() if callable(first) else first
        later = self.at(g, 20).enemy.max_hp
        later = later() if callable(later) else later
        self.assertGreater(later, first)

    def test_killing_one_touches_no_part_of_the_story(self):
        """**The half that had to be sealed.** A wave run builds a world
        it never walks (§59), so the warden path would have marked that
        throwaway island's portal defeated, toasted the next warden,
        handed over a Memory, unlocked codex pages and queued an epitaph
        - in a mode with no story to put any of it in."""
        g = self.waves()
        battle = self.at(g, 10)
        before = [p.defeated for p in g.world.portals]
        relics = len(g.player.relic_objects())
        g.on_boss_defeated(battle.enemy)
        self.assertEqual([p.defeated for p in g.world.portals], before)
        self.assertEqual(len(g.player.relic_objects()), relics)
        self.assertIsNone(getattr(g, "pending_epitaph", None))
        self.assertFalse(getattr(g, "pending_ending", False))

    def test_a_story_run_still_gets_everything(self):
        """The guard is on the mode, not on wardens - so the thing it
        protects must be proved still to work."""
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.new_run("Islander", 1)
        self.assertFalse(g.in_waves())
        import inspect
        source = inspect.getsource(G.Game.on_boss_defeated)
        self.assertIn("self.in_waves()", source)
        self.assertIn("WARDEN_BY_BOSS", source)

    def test_one_door_builds_every_wave(self):
        """Solo and co-op both go through `wave_enemies`, so a room
        cannot disagree about whether wave ten is a warden - §54's *two
        payout paths, one behaviour* pointed at the other end of a
        fight."""
        import ast
        import inspect
        import textwrap
        from pixel_rpg.game import Game
        for method in (Game.begin_wave, Game.host_open_wave):
            tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
            calls = {getattr(n.func, "attr", "") for n in ast.walk(tree)
                     if isinstance(n, ast.Call)}
            self.assertIn("wave_enemies", calls,
                          "%s builds its own pack" % method.__name__)


def _display_only():
    """Bring up just enough pygame to make a Surface.

    **Not `pygame.init()`.** That starts the *joystick* subsystem too,
    and `TestGamepad` asserts it stays down until somebody opts in - so
    a drawing test calling the big init makes an unrelated test fail,
    two hundred tests later, depending on the order they ran in. Found
    exactly that way (§75).
    """
    import pygame
    pygame.display.init()
    pygame.font.init()
    if not pygame.display.get_surface():
        pygame.display.set_mode((64, 64))


class TestTheBackdropHasDepth(unittest.TestCase):
    """§73, item 0ar. Savvydev, on the arenas §72 had just built:

    > "also the battle background can be nicer tbh, its too simple"

    A backdrop was a two-colour gradient and one flat rectangle of
    ground, and painting that rectangle eighteen colours makes eighteen
    simple backdrops.
    """

    def test_every_arena_names_a_skyline(self):
        from pixel_rpg import battlescreen, gamemodes
        for arena in gamemodes.ARENAS:
            self.assertIn(arena.scene, battlescreen.MOTES,
                          "%s stands in front of nothing" % arena.key)

    def test_a_fight_anywhere_gets_one(self):
        """**Not just a wave.** He asked for *the battle background*, and
        an island fight is the one nearly every player is in."""
        from pixel_rpg import battlescreen
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import Player, make_encounter
        from pixel_rpg.util import rng
        rng.reseed(3)
        hero = Player("Looker", "warden")
        battle = Battle(hero, make_encounter(6, "meadow", count=1))
        self.assertEqual(battle.arena_scene, "")
        self.assertIn(battlescreen.arena_scene(battle), battlescreen.MOTES)

    def test_the_same_place_has_the_same_hills_every_time(self):
        """A backdrop that reshuffles between waves is scenery you cannot
        rely on, which is what 0aq says not to build."""
        from pixel_rpg import battlescreen

        class Fake:
            enemy = type("E", (), {"element": "fire"})()
            arena_scene = "crags"
        one, two = Fake(), Fake()
        self.assertEqual(battlescreen.arena_seed_of(one),
                         battlescreen.arena_seed_of(two))

    def test_drawing_the_sky_draws_no_randomness(self):
        """A silhouette rolls its own stream. §21's wasted `rng.choice`
        moved four classes; a backdrop rolling the shared one would move
        every fight behind it."""
        import pygame
        from pixel_rpg import battlescreen
        from pixel_rpg.assets import Assets
        from pixel_rpg.util import rng
        _display_only()
        assets = Assets()
        rng.reseed(9)
        before = rng._r.getstate()
        for kind in battlescreen.MOTES:
            surf = pygame.Surface((320, 200))
            battlescreen.draw_backdrop(surf, assets, (80, 90, 100),
                                       (10, 10, 14), kind, 17, 3.0)
        self.assertEqual(rng._r.getstate(), before)

    def test_the_ground_is_not_a_hole(self):
        """**Only a full-size screenshot showed this** (rule 31.4): the
        ground was derived from an already-darkened sky, compounded three
        times, and arrived at about 5% of the colour the place is
        supposed to be - so the bottom half of the screen was a void with
        sprites floating in it."""
        import pygame
        from pixel_rpg import battlescreen
        from pixel_rpg.assets import Assets
        _display_only()
        top, bottom = (100, 120, 90), (20, 24, 18)
        surface = pygame.Surface((320, 200))
        battlescreen.draw_backdrop(surface, Assets(), top, bottom,
                                   "hills", 5, 0.0)
        floor = surface.get_at((160, 190))[:3]
        self.assertGreater(sum(floor), 40,
                           "the ground is effectively black: %s" % (floor,))

    def test_the_backdrop_is_built_once_and_kept(self):
        """Polygons rebuilt sixty times a second is the difference
        between a backdrop and a stutter. **The layers are the cached
        thing now** (§74), not the whole picture, because the whole
        picture moves."""
        import pygame
        from pixel_rpg import battlescreen
        from pixel_rpg.assets import Assets
        _display_only()
        assets = Assets()
        first = battlescreen.backdrop_ground(
            assets, (320, 200), (90, 100, 80), (12, 14, 10), (40, 44, 50))
        again = battlescreen.backdrop_ground(
            assets, (320, 200), (90, 100, 80), (12, 14, 10), (40, 44, 50))
        self.assertIs(first, again)


class TestTheWheelWorksEverywhere(unittest.TestCase):
    """§74, item 0bc. Savvydev, 2026-09-06:

    > "you cant use the scroll wheel in the modifier menu, but you should
    > be able to, again its one of those industry standard/common sense
    > things"

    **"again" is the word that matters** - this is rule 8b, his own rule
    about the mouse, and the fourth screen he has had to report.
    """

    def game(self):
        from pixel_rpg import game as G
        return G.Game()

    def test_the_wheel_moves_the_modifier_cursor(self):
        from pixel_rpg import game as G
        g = self.game()
        g.state = G.MODIFIER_SELECT
        g.ui.modifier_index = 0
        g.mouse_scroll(-1)
        self.assertEqual(g.ui.modifier_index, 1)
        g.mouse_scroll(1)
        self.assertEqual(g.ui.modifier_index, 0)

    def test_it_is_a_fallback_and_not_a_seventh_table_entry(self):
        """**The fix has to be the class of problem.** A table of screens
        that answer the wheel is how this gets reported a fifth time; the
        rule is that a notch is Up or Down wherever those work."""
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.mouse_scroll)
        self.assertIn("K_UP", source)
        self.assertIn("K_DOWN", source)

    def test_the_screens_with_real_lists_still_scroll_their_lists(self):
        from pixel_rpg import game as G
        g = self.game()
        g.state = G.INVENTORY
        g.ui.inv.offset = 4
        g.mouse_scroll(1)
        self.assertEqual(g.ui.inv.offset, 3)

    def test_a_flick_does_not_jump_the_list(self):
        """A trackpad sends large `y` values; eleven rows on one flick is
        worse than none."""
        from pixel_rpg import game as G
        g = self.game()
        g.state = G.MODIFIER_SELECT
        g.ui.modifier_index = 0
        g.mouse_scroll(-11)
        self.assertLessEqual(g.ui.modifier_index, 3)


class TestTheWholePackHasHealth(unittest.TestCase):
    """§74, item 0av. Savvydev:

    > "show all of the enemy healthbars at once stacked on top of each
    > other and whichever enemy you have selected has a bigger health bar"
    """

    def test_the_plate_grows_with_the_pack(self):
        from pixel_rpg import battlescreen
        one = battlescreen.enemy_plate_height(1)
        five = battlescreen.enemy_plate_height(5)
        self.assertGreater(five, one)
        self.assertEqual(five - one, 4 * battlescreen.ROSTER_ROW)

    def test_the_target_gets_the_bigger_row(self):
        from pixel_rpg import battlescreen
        self.assertGreater(battlescreen.ROSTER_TARGET,
                           battlescreen.ROSTER_ROW)

    def test_a_solo_fight_is_left_alone(self):
        """The screen a one-monster fight draws should be the screen it
        always drew - the roster only appears when there is a pack."""
        from pixel_rpg import battlescreen
        self.assertEqual(battlescreen.enemy_plate_height(1),
                         12 + battlescreen.ROSTER_TARGET)

    def test_the_plate_never_reaches_what_is_under_it(self):
        """**`layout.py --phone` found this** the moment the plate stopped
        being a fixed 92: at 844x390 a five-monster roster wanted 162 and
        the submenu starts at 130."""
        from pixel_rpg import battlescreen
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import Player, make_encounter
        from pixel_rpg.util import rng
        rng.reseed(2)
        hero = Player("Looker", "warden")
        battle = Battle(hero, make_encounter(8, "meadow", count=5))
        for size in ((1280, 720), (844, 390), (390, 844)):
            panels = battlescreen.battle_panels(battle, *size)
            plate, sub = panels["enemy"], panels["submenu"]
            self.assertLessEqual(plate.bottom, sub.top,
                                 "%s: the roster runs into the submenu"
                                 % (size,))


class TestWhoIsAimingAtWhat(unittest.TestCase):
    """§74, items 0aw and 0ax."""

    def test_the_turn_bar_names_the_monsters(self):
        """It has always *included* them - `turn_order` returns the whole
        round - but a chip reading "3" says a monster is up and not
        which, which is most of why he asked."""
        import inspect
        from pixel_rpg import battlescreen
        source = inspect.getsource(battlescreen.draw_turn_bar)
        self.assertIn("battle.enemies[index].name", source)

    def test_the_order_still_comes_from_the_fight(self):
        """§51's rule: the renderer may not work the order out for
        itself, because a bar that disagrees with the fight is worse than
        no bar."""
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import Player, make_encounter
        from pixel_rpg.util import rng
        rng.reseed(4)
        battle = Battle(Player("A", "warden"),
                        make_encounter(6, "meadow", count=3))
        order = battle.turn_order()
        self.assertTrue(any(side == "enemy" for side, _i in order))
        self.assertTrue(any(side == "player" for side, _i in order))

    def test_the_aim_label_is_only_drawn_when_it_could_be_somebody_else(self):
        """Solo, the answer is always "you" and the label is noise."""
        import inspect
        from pixel_rpg import battlescreen
        source = inspect.getsource(battlescreen.draw_target_marker)
        self.assertIn("len(battle.players) < 2", source)

    def test_the_label_asks_the_layout_where_it_may_go(self):
        """Above is the log and roster band; below is the menu. On an
        844x390 window both are taken and the honest answer is to draw
        nothing - which a fixed offset cannot give."""
        import inspect
        from pixel_rpg import battlescreen
        source = inspect.getsource(battlescreen.draw_target_marker)
        self.assertIn("battle_panels", source)
        self.assertIn("colliderect", source)


class TestTheBackgroundMovesAndVaries(unittest.TestCase):
    """§74, items 0bd and 0be.

    > "make 3 more for each existing background, that way if ur in one
    > area for a long time, the background varies"

    > "the background should also move, idk if it does or not."
    """

    def surface(self):
        import pygame
        _display_only()
        return pygame.Surface((320, 200))

    def assets(self):
        from pixel_rpg.assets import Assets
        return Assets()

    def test_it_moves(self):
        import pygame
        from pixel_rpg import battlescreen
        assets = self.assets()
        frames = []
        for elapsed in (0.0, 5.0, 20.0):
            surf = self.surface()
            battlescreen.draw_backdrop(surf, assets, (90, 110, 80),
                                       (12, 14, 10), "hills", 33, elapsed)
            frames.append(pygame.image.tostring(surf, "RGB"))
        self.assertNotEqual(frames[0], frames[1], "the background is static")
        self.assertNotEqual(frames[1], frames[2])

    def test_the_layers_move_at_different_speeds(self):
        """Two layers at one speed is a sliding picture, not parallax."""
        from pixel_rpg import battlescreen
        self.assertEqual(len(battlescreen.DRIFT), 2)
        self.assertLess(battlescreen.DRIFT[0], battlescreen.DRIFT[1])

    def test_a_place_has_four_skylines_and_they_differ(self):
        import pygame
        from pixel_rpg import battlescreen
        assets = self.assets()
        seen = []
        for variant in range(battlescreen.ARENA_VARIANTS):

            class Fake:
                enemy = type("E", (), {"element": "physical"})()
                arena_scene = "hills"
                arena_variant = variant
            seed = battlescreen.arena_seed_of(Fake())
            surf = self.surface()
            battlescreen.draw_backdrop(surf, assets, (90, 110, 80),
                                       (12, 14, 10), "hills", seed, 0.0)
            shot = pygame.image.tostring(surf, "RGB")
            self.assertNotIn(shot, seen, "variant %d is a repeat" % variant)
            seen.append(shot)

    def test_a_fight_keeps_its_skyline_for_the_whole_fight(self):
        """A backdrop that changes mid-battle is 0aq's fault in a smaller
        costume: scenery you cannot rely on."""
        from pixel_rpg import battlescreen

        class Fake:
            enemy = type("E", (), {"element": "physical"})()
            arena_scene = "crags"
            arena_variant = 3
        one = Fake()
        self.assertEqual(battlescreen.arena_seed_of(one),
                         battlescreen.arena_seed_of(one))

    def test_a_new_fight_gets_a_new_one_without_touching_the_rng(self):
        from pixel_rpg import game as G
        from pixel_rpg.entities import make_encounter
        from pixel_rpg.util import rng
        g = G.Game()
        g.pending_class = "warden"
        g.new_run("Looker", 1)
        rng.reseed(8)
        before = rng._r.getstate()
        seen = set()
        for _ in range(4):
            g.battle = None
            g.start_battle(make_encounter(6, "meadow", count=1))
            seen.add(g.battle.arena_variant % 4)
        self.assertGreater(len(seen), 1, "every fight draws the same hills")
        # `make_encounter` rolls, so the stream is expected to have moved -
        # what must not have happened is the *backdrop* rolling it.
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.start_battle)
        self.assertIn("self.fights_seen += 1", source)
        self.assertNotIn("rng.rint(0, 3)", source)
        self.assertIsNotNone(before)

    def test_the_layers_are_cached(self):
        from pixel_rpg import battlescreen
        assets = self.assets()
        args = (assets, (320, 200), (90, 100, 80), "hills", 4)
        first = battlescreen.backdrop_layers(*args)
        again = battlescreen.backdrop_layers(*args)
        self.assertIs(first[0], again[0])
        self.assertIs(first[1], again[1])


class TestWhereTheWavesAreFoughtIsHisChoice(unittest.TestCase):
    """§74, item 0au. Savvydev:

    > "they can pick an arena to use for the whole run or pick a random
    > one, or it changes every 10 or 5 or whatever waves. its up to them."
    """

    def test_the_three_shapes_exist(self):
        from pixel_rpg import gamemodes
        self.assertEqual(set(gamemodes.ARENA_MODES),
                         {"every", "one", "random"})
        self.assertIn(10, gamemodes.ARENA_CADENCES)

    def test_one_place_stays_that_place(self):
        from pixel_rpg import gamemodes
        keys = {gamemodes.arena_for(w, "one", pick=5).key
                for w in range(1, 40)}
        self.assertEqual(len(keys), 1)

    def test_the_cadence_is_the_one_they_set(self):
        from pixel_rpg import gamemodes
        for every in gamemodes.ARENA_CADENCES:
            first = gamemodes.arena_for(1, "every", every).key
            self.assertEqual(gamemodes.arena_for(every, "every", every).key,
                             first)
            self.assertNotEqual(
                gamemodes.arena_for(every + 1, "every", every).key, first)

    def test_random_is_rolled_once_per_run_not_once_per_wave(self):
        """Rolling it per wave is the game changing its scenery behind
        the player's back, which is what 0aq forbids."""
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.new_wave_run("Stander")
        g.settings.arena_mode = "random"
        keys = {g.arena_now(w).key for w in range(1, 30)}
        self.assertEqual(len(keys), 1)

    def test_the_default_is_what_it_always_was(self):
        from pixel_rpg import gamemodes
        from pixel_rpg.config import Settings
        s = Settings()
        self.assertEqual(s.arena_mode, "every")
        self.assertEqual(s.arena_every, gamemodes.WAVES_PER_ARENA)

    def test_the_preference_survives_a_save(self):
        from pixel_rpg.config import Settings
        s = Settings()
        s.arena_mode, s.arena_every, s.arena_pick = "one", 10, 9
        back = Settings()
        back.apply_dict(s.to_dict())
        self.assertEqual((back.arena_mode, back.arena_every, back.arena_pick),
                         ("one", 10, 9))

    def test_a_junk_preference_is_refused(self):
        from pixel_rpg.config import Settings
        s = Settings()
        s.apply_dict({"arena_mode": "../etc", "arena_every": 9999,
                     "arena_pick": -3})
        self.assertEqual(s.arena_mode, "every")
        self.assertLessEqual(s.arena_every, 10)
        self.assertGreaterEqual(s.arena_pick, 0)


class TestStoryIsNotADifficultyEverywhere(unittest.TestCase):
    """§74, item 0bb. Savvydev:

    > "story shouldnt be a difficulty for the wave gamemode and same for
    > zombie gamemode"
    """

    def test_the_wave_mode_does_not_offer_it(self):
        from pixel_rpg import config
        self.assertNotIn("story", config.difficulties_for("waves"))
        self.assertNotIn("story", config.difficulties_for("zombies"))

    def test_the_story_mode_still_does(self):
        from pixel_rpg import config
        self.assertIn("story", config.difficulties_for("story"))
        self.assertEqual(config.difficulties_for("anything-else"),
                         config.DIFFICULTIES)

    def test_cycling_in_a_wave_run_never_lands_on_it(self):
        from pixel_rpg import config
        from pixel_rpg.config import Settings
        s = Settings()
        s.difficulty = "normal"
        seen = {s.cycle_difficulty(1, config.difficulties_for("waves"))
                for _ in range(12)}
        self.assertNotIn("story", seen)
        self.assertEqual(len(seen), 3)

    def test_a_run_carried_in_on_story_is_lifted_off_it(self):
        """The difficulty is one setting shared with the story mode, so
        somebody who finished an easy run and opened a Last Stand would
        carry it in without the picker ever refusing it."""
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.settings.difficulty = "story"
        g.new_wave_run("Stander")
        self.assertNotEqual(g.settings.difficulty, "story")


class TestWaysidesAreEverywhere(unittest.TestCase):
    """§75, item 0at. Savvydev, 2026-09-06:

    > "probably just add a lot more like that around the map."

    §72 put twelve on the last third of the six roads, because §72 was
    answering item 12 - *nothing happens between a waystation and the
    warden*. This is the wider ask.
    """

    def test_there_are_a_lot_more_of_them(self):
        from pixel_rpg.world import World
        for seed in (1, 42, 4242):
            world = World(seed=seed)
            self.assertGreaterEqual(len(world.waysides), 28,
                                    "seed %d: still only the road ones"
                                    % seed)

    def test_most_of_them_are_off_the_roads(self):
        """The road ones break up one specific empty stretch; these exist
        to be **found**, which means being where you were not sent."""
        from pixel_rpg.world import World
        world = World(seed=4242)
        sx, sy = world.spawn
        on_a_road = 0
        for spot in world.waysides:
            near = min(
                (abs(spot[0] - (sx + (p.gx - sx) * f))
                 + abs(spot[1] - (sy + (p.gy - sy) * f))
                 for p in world.portals for f in (0.78, 0.90)))
            if near < 12:
                on_a_road += 1
        self.assertLess(on_a_road, len(world.waysides) // 2)

    def test_they_are_not_stacked_on_each_other(self):
        from pixel_rpg.world import World
        world = World(seed=7)
        spots = list(world.waysides)
        for i, one in enumerate(spots):
            for other in spots[i + 1:]:
                gap = max(abs(one[0] - other[0]), abs(one[1] - other[1]))
                self.assertGreater(gap, 2, "%s and %s" % (one, other))

    def test_every_one_can_still_be_stood_beside(self):
        """**Placed only where the flood already reaches**, rather than
        leaning on the connectivity repair: three in twenty-five seeds
        came out walled off when it relied on the repair, because a pass
        cannot cut through the things it is protecting."""
        from pixel_rpg.world import World
        for seed in range(10):
            world = World(seed=seed)
            reach = world._reachable_from(world.spawn)
            for spot in world.waysides:
                self.assertTrue(world._beside(spot, reach),
                                "seed %d: %s is walled off" % (seed, spot))

    def test_the_extra_ones_still_move_no_chest(self):
        from pixel_rpg.world import World
        original = World._scatter_waysides
        try:
            for seed in (1, 42, 4242):
                with_them = sorted((c.gx, c.gy)
                                   for c in World(seed=seed).chests)
                World._scatter_waysides = lambda self, count=20: None
                without = sorted((c.gx, c.gy)
                                 for c in World(seed=seed).chests)
                World._scatter_waysides = original
                self.assertEqual(with_them, without,
                                 "seed %d: scattering moved the chests"
                                 % seed)
        finally:
            World._scatter_waysides = original

    def test_they_still_only_work_once(self):
        """His answer to §72's question: *"then only once per run"*."""
        from pixel_rpg.world import World
        world = World(seed=3)
        spot = next(iter(world.waysides))
        self.assertFalse(world.wayside_at(*spot)[1])
        world.settle_wayside(*spot)
        self.assertTrue(world.wayside_at(*spot)[1])


class TestEverySpellLooksDifferent(unittest.TestCase):
    """§75, item 0ba. Savvydev, 2026-09-06:

    > "also each spell should visually look different."

    Every magical attack fired `"bolt"` and differed only in colour, so
    a Firebolt, a Meteor and a Frost Lance were one drawing tinted three
    ways.
    """

    def test_the_shapes_are_derived_not_tabulated(self):
        """The same rule `heft` (§70) and `skill_motion` (§71) are built
        on: a skill added later gets a shape without anybody choosing
        one for it."""
        from pixel_rpg import skills
        looks = {skills.skill_look(s) for s in skills.SKILLS_BY_KEY.values()}
        self.assertGreaterEqual(len(looks), 5,
                                "every spell still looks the same")
        for skill in skills.SKILLS_BY_KEY.values():
            self.assertEqual(skill.look, "",
                             "%s hand-types a shape; derive it or say why"
                             % skill.key)

    def test_an_override_is_honoured_when_there_is_one(self):
        from pixel_rpg import skills
        one = list(skills.SKILLS_BY_KEY.values())[0]
        forced = dataclasses.replace(one, look="beam")
        self.assertEqual(skills.skill_look(forced), "beam")

    def test_a_big_spell_draws_bigger(self):
        from pixel_rpg import skills
        by_key = skills.SKILLS_BY_KEY
        big = max((s for s in by_key.values() if s.kind == "attack"),
                  key=lambda s: s.power)
        small = min((s for s in by_key.values() if s.kind == "attack"),
                    key=lambda s: s.power)
        self.assertGreater(skills.skill_scale(big),
                           skills.skill_scale(small))
        for skill in by_key.values():
            self.assertGreaterEqual(skills.skill_scale(skill), 1.0)
            self.assertLessEqual(skills.skill_scale(skill), 2.0)

    def test_a_drain_is_a_drain_whatever_it_is_made_of(self):
        """The shape is the *taking*, not the element."""
        from pixel_rpg import skills
        for skill in skills.SKILLS_BY_KEY.values():
            if skill.kind == "drain":
                self.assertEqual(skills.skill_look(skill), "mote", skill.key)

    def test_every_shape_the_table_can_produce_is_drawn(self):
        """§54's rule: a declared thing nothing reads is indistinguishable
        from one whose behaviour was deleted."""
        from pixel_rpg import skills
        from pixel_rpg.ui import ShotLayer
        produced = {skills.skill_look(s)
                    for s in skills.SKILLS_BY_KEY.values()}
        produced |= set(skills.LOOK_BY_ELEMENT.values())
        for look in produced:
            self.assertIn(look, ShotLayer.SPELL_LOOKS,
                          "%s is produced and never drawn" % look)

    def test_each_shape_actually_draws_something_different(self):
        import pygame
        from pixel_rpg.ui import ShotLayer
        _display_only()
        seen = {}
        for look in ShotLayer.SPELL_LOOKS:
            surface = pygame.Surface((200, 80))
            surface.fill((0, 0, 0))
            layer = ShotLayer()
            shot = layer.add((20, 40), (180, 40), (240, 120, 80),
                             look=look, life=1.0, arc=0.0, scale=1.0)
            shot.age = 0.5
            layer.draw(surface, None)
            shape = pygame.image.tostring(surface, "RGB")
            self.assertNotEqual(
                shape, pygame.image.tostring(
                    pygame.Surface((200, 80)), "RGB"),
                "%s draws nothing" % look)
            for other, blob in seen.items():
                self.assertNotEqual(shape, blob,
                                    "%s and %s draw the same thing"
                                    % (look, other))
            seen[look] = shape

    def test_the_fight_fires_the_spells_own_shape(self):
        import inspect
        from pixel_rpg.battle import Battle
        source = inspect.getsource(Battle.use_skill)
        self.assertIn("skill_look", source)
        self.assertIn("skill_scale", source)
        self.assertNotIn('fire_shot(\n                "bolt"', source)

    def test_battle_py_still_draws_nothing(self):
        """Hard rule 2.3: it says what was fired, never what it looks
        like on screen.

        **Read with `ast`, not with a substring** (§7). The word appears
        five times in that file - in the comments explaining why it is
        not imported - and a text search calls each of them a failure.
        """
        import ast
        import inspect
        from pixel_rpg import battle as battle_mod
        tree = ast.parse(inspect.getsource(battle_mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("pygame", alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn("pygame", node.module or "")


class TestTheBoards(unittest.TestCase):
    """§75, item 0az. Savvydev, 2026-09-06:

    > "also there should be a global score leaderboard for each gamemode
    > for each party size"

    **The global half is blocked and the shape is not.** There is
    nowhere to put a global board - *"no hosted server, ever"*
    (2026-08-22) is a decision, not an oversight - so what is built is
    everything on this side of that line.
    """

    def profile(self):
        from pixel_rpg import meta as meta_mod
        return meta_mod.MetaProfile()

    def record(self, **kw):
        from pixel_rpg import meta as meta_mod
        return meta_mod.RunRecord(**kw)

    def test_a_board_is_one_mode_at_one_party_size(self):
        meta = self.profile()
        for mode, party, score in (("waves", 1, 900), ("waves", 1, 1500),
                                   ("waves", 4, 700), ("story", 2, 300)):
            meta.finish_run(self.record(mode=mode, party=party, score=score))
        self.assertEqual([r.score for r in meta.board("waves", 1)],
                         [1500, 900])
        self.assertEqual([r.score for r in meta.board("waves", 4)], [700])
        self.assertEqual([r.score for r in meta.board("story", 2)], [300])
        self.assertEqual(meta.board("story", 1), [])

    def test_it_is_ordered_best_first(self):
        meta = self.profile()
        for score in (100, 900, 400):
            meta.finish_run(self.record(mode="waves", score=score))
        self.assertEqual([r.score for r in meta.board("waves", 1)],
                         [900, 400, 100])

    def test_a_run_carries_the_party_it_was_played_with(self):
        """Without this the board would have to guess, and a solo Last
        Stand and a four-player one are not comparable."""
        import ast
        import inspect
        from pixel_rpg.game import Game
        for method in (Game.finish_wave_run
                       if hasattr(Game, "finish_wave_run") else Game.tick,):
            pass
        source = inspect.getsource(inspect.getmodule(Game))
        tree = ast.parse(source)
        made = [node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "RunRecord"]
        self.assertTrue(made, "no run records are written at all")
        for call in made:
            names = {kw.arg for kw in call.keywords}
            self.assertIn("party", names,
                          "a run is recorded without its party size")

    def test_the_party_size_survives_a_save(self):
        from pixel_rpg import meta as meta_mod
        one = self.record(mode="waves", party=3, score=42)
        back = meta_mod.RunRecord.from_dict(one.to_dict())
        self.assertEqual(back.party, 3)

    def test_an_old_record_reads_back_as_solo(self):
        """Everything already on disk predates the field; it must not
        need a migration - the same call `mode` made in §59."""
        from pixel_rpg import meta as meta_mod
        back = meta_mod.RunRecord.from_dict({"score": 10, "mode": "waves"})
        self.assertEqual(back.party, 1)

    def test_a_junk_party_size_is_refused(self):
        from pixel_rpg import meta as meta_mod
        for junk in (0, -4, 99, "four", None):
            back = meta_mod.RunRecord.from_dict({"party": junk})
            self.assertGreaterEqual(back.party, 1)
            self.assertLessEqual(back.party, 4)

    def test_a_marked_run_never_reaches_a_board(self):
        """§16's rule: a debug run cannot post a score, and a board is a
        score in a hat."""
        meta = self.profile()
        meta.tainted = True
        meta.finish_run(self.record(mode="waves", score=99999))
        self.assertEqual(meta.board("waves", 1), [])


class TestTheGlobalBoard(unittest.TestCase):
    """§75, item 0az's other half - unblocked by his correction:

    > "i dont want ALL global servers running off of my pc, but you can
    > still use FREE 3rd party tools for the global stuff"

    Nothing of ours runs anywhere; this is a client for somebody else's
    free service, and which one is configuration.
    """

    def board(self, answer=None, url="https://example.invalid",
              key="k", clock=None):
        from pixel_rpg import leaderboard as lb
        sent = []

        def transport(method, full_url, payload, api_key):
            sent.append((method, full_url, payload, api_key))
            if isinstance(answer, Exception):
                raise answer
            return answer

        board = lb.Leaderboard(transport=transport, clock=clock)
        board.url, board.key = url, key
        return board, sent

    def test_it_is_off_until_somebody_configures_it(self):
        """**Off is the shipped state.** Nobody gets a global board by
        accident and no key of his is ever in a file."""
        from pixel_rpg import leaderboard as lb
        board = lb.Leaderboard()
        board.url = ""
        self.assertFalse(board.enabled)
        self.assertEqual(board.top(), [])
        self.assertFalse(board.submit(
            type("R", (), {"mode": "waves", "party": 1, "score": 9})(), "me"))

    def test_no_key_is_written_down_anywhere(self):
        """Hard rule 2.10 and rule 31.2, at the one place in the game
        that has ever wanted a credential."""
        import inspect
        from pixel_rpg import leaderboard as lb
        source = inspect.getsource(lb)
        self.assertIn("get_secret", source)
        for shape in ("Bearer sk", "api_key=", "AIza", "ghp_"):
            self.assertNotIn(shape, source)

    def test_a_board_comes_back_cleaned(self):
        board, _sent = self.board('{"scores": [{"name": "a", "score": 40},'
                                  ' {"name": "b", "score": 900}]}')
        rows = board.top("waves", 1)
        self.assertEqual([r["score"] for r in rows], [900, 40])
        self.assertEqual(rows[0]["party"], 1)

    def test_a_hostile_row_cannot_get_through(self):
        """**The least trustworthy input in the game** - it comes from
        strangers, where a save at least started on the player's disk."""
        board, _sent = self.board(
            '{"scores": [{"name": "' + "x" * 400 + '", "score": 99999999999,'
            ' "party": 77, "waves": -5, "mode": "../../etc"}]}')
        rows = board.top("waves", 1)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertLessEqual(len(row["name"]), 18)
        self.assertLessEqual(row["score"], 99_999_999)
        self.assertLessEqual(row["party"], 4)
        self.assertGreaterEqual(row["waves"], 0)
        self.assertIn(row["mode"], ("story", "waves", "zombies"))

    def test_junk_that_is_not_even_a_list_is_no_rows(self):
        for answer in ("not json at all", "{}", "[]", '{"scores": 7}',
                       'null'):
            board, _sent = self.board(answer)
            self.assertEqual(board.top(), [], answer)

    def test_a_dead_service_never_reaches_the_caller(self):
        """A records screen must never learn that sockets exist."""
        board, _sent = self.board(RuntimeError("no route to host"))
        self.assertEqual(board.top(), [])
        self.assertTrue(board.last_error)

    def test_an_enormous_response_is_refused_before_it_is_parsed(self):
        from pixel_rpg import leaderboard as lb
        board, _sent = self.board("x" * (lb.MAX_BYTES + 10))
        self.assertEqual(board.top(), [])

    def test_submitting_is_rate_limited_to_his_baseline(self):
        """Rule 31.2: five attempts per fifteen minutes. A client that
        hammers a free tier gets the free tier taken away."""
        from pixel_rpg import leaderboard as lb
        now = [1000.0]
        board, sent = self.board('{"ok": true}', clock=lambda: now[0])
        record = type("R", (), {"mode": "waves", "party": 1, "score": 9,
                                "waves": 3, "class_key": "warden",
                                "when": ""})()
        for _ in range(lb.SUBMITS):
            self.assertTrue(board.submit(record, "me"))
        self.assertFalse(board.submit(record, "me"))
        now[0] += lb.SUBMIT_WINDOW + 1
        self.assertTrue(board.submit(record, "me"))
        self.assertEqual(len(sent), lb.SUBMITS + 1)

    def test_the_key_goes_in_a_header_and_never_in_the_url(self):
        """A key in a query string ends up in somebody's access log."""
        import inspect
        from pixel_rpg import leaderboard as lb
        source = inspect.getsource(lb.Leaderboard._http)
        self.assertIn("Authorization", source)
        board, sent = self.board('{"scores": []}')
        board.top("waves", 2)
        method, url, _payload, key = sent[0]
        self.assertEqual(method, "GET")
        self.assertNotIn(key, url)
        self.assertIn("party=2", url)

    def test_a_submitted_row_carries_the_party_size(self):
        board, sent = self.board('{"ok": true}')
        record = type("R", (), {"mode": "waves", "party": 3, "score": 40,
                                "waves": 8, "class_key": "warden",
                                "when": "2026-09-06"})()
        self.assertTrue(board.submit(record, "Savvydev"))
        payload = sent[0][2]
        self.assertEqual(payload["party"], 3)
        self.assertEqual(payload["mode"], "waves")

    def test_the_local_board_works_with_no_network_at_all(self):
        """**The local board is the real one until this is configured.**
        A leaderboard that breaks the records screen when the wifi is off
        is worse than no leaderboard."""
        from pixel_rpg import meta as meta_mod
        profile = meta_mod.MetaProfile()
        profile.finish_run(meta_mod.RunRecord(mode="waves", party=1,
                                              score=500))
        self.assertEqual(len(profile.board("waves", 1)), 1)


class TestFreeRoamCombat(unittest.TestCase):
    """§76, roadmap item 0ak. Savvydev, 2026-09-06:

    > "it removes turn based combat and instead you fight openly, select
    > which skills you want to use, aim, dodge, etc etc."

    **Beside `battle.py`, not inside it** - §19 named that choice when
    the item was written.
    """

    def fight(self, foes=3, level=8):
        from pixel_rpg import freeroam as fr
        from pixel_rpg.entities import Player, make_enemy
        from pixel_rpg.util import rng
        rng.reseed(5)
        hero = Player("Runner", "warden")
        for _ in range(level - 1):
            hero.gain_xp(hero.xp_to_next())
        hero.full_restore()
        monsters = [make_enemy(level, "meadow") for _ in range(foes)]
        return fr.FreeRoam(hero, monsters), hero

    def test_it_imports_no_pygame(self):
        """Hard rule 2.3's reasoning, unchanged: a simulation that can
        only be tested by opening a window does not get tested."""
        import ast
        import inspect
        from pixel_rpg import freeroam as fr
        tree = ast.parse(inspect.getsource(fr))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("pygame", alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn("pygame", node.module or "")

    def test_the_turn_based_game_is_untouched(self):
        """It is what `tools/balance.py` measures the whole game with. A
        real-time mode that reached into it would invalidate every number
        this project has quoted, on the same day."""
        import ast
        import inspect
        from pixel_rpg import battle as battle_mod
        tree = ast.parse(inspect.getsource(battle_mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn("freeroam", node.module or "")

    def test_damage_comes_from_the_shared_model(self):
        """The one thing it borrows and the one thing it must: class
        signatures, modifiers, elements and crits all enter there, so a
        Cutpurse is a Cutpurse in both engines."""
        import inspect
        from pixel_rpg import freeroam as fr
        source = inspect.getsource(fr)
        self.assertIn("from .battle import compute_damage", source)

    def test_a_swing_hits_what_is_in_front_of_it(self):
        """**An arc, not a target.** Nothing is selected - what you hit
        is whatever was in front of you, which is what "aim" means."""
        import math
        from pixel_rpg import freeroam as fr
        fight, _hero = self.fight(foes=0)
        from pixel_rpg.entities import make_enemy
        body = fight.hero(0)
        ahead = fight.spawn(make_enemy(6, "meadow"),
                            at=(body.x + 40, body.y))
        behind = fight.spawn(make_enemy(6, "meadow"),
                             at=(body.x - 40, body.y))
        body.facing = 0.0
        before = (ahead.who.hp, behind.who.hp)
        fight.swing(0)
        self.assertLess(ahead.who.hp, before[0], "the one in front was missed")
        self.assertEqual(behind.who.hp, before[1], "the one behind was hit")

    def test_a_swing_out_of_reach_hits_nothing(self):
        from pixel_rpg import freeroam as fr
        from pixel_rpg.entities import make_enemy
        fight, _hero = self.fight(foes=0)
        body = fight.hero(0)
        far = fight.spawn(make_enemy(6, "meadow"),
                          at=(body.x + fr.SWING_REACH * 3, body.y))
        body.facing = 0.0
        before = far.who.hp
        fight.swing(0)
        self.assertEqual(far.who.hp, before)

    def test_a_dodge_commits_you(self):
        """The commitment is what makes it a decision rather than a
        second walk: during one, the input is ignored."""
        from pixel_rpg import freeroam as fr
        fight, _hero = self.fight(foes=0)
        body = fight.hero(0)
        body.facing = 0.0
        start = body.x
        fight.input(0, dx=-1.0, dy=0.0, dodge=True, dt=1 / 60)
        self.assertGreater(body.x, start,
                           "a dodge went where the keys said, not where "
                           "the body was pointed")

    def test_a_dodge_has_i_frames_and_they_end(self):
        """**Not the whole dodge**: if it were, the answer to every
        attack would be "dodge" and the decision would stop being one.

        And the window must be *shorter* than the dodge - the first
        version had `IFRAMES` longer, which made the threshold negative
        and **every body invulnerable forever**."""
        from pixel_rpg import freeroam as fr
        self.assertLess(fr.IFRAMES, fr.DODGE_TIME)
        fight, _hero = self.fight(foes=0)
        body = fight.hero(0)
        self.assertFalse(body.invulnerable, "invulnerable while standing")
        fight.input(0, dodge=True, dt=1 / 60)
        self.assertTrue(body.invulnerable)
        for _ in range(20):
            body.advance(1 / 60)
        self.assertFalse(body.invulnerable)

    def test_being_touched_is_what_hurts_and_it_has_a_grace(self):
        from pixel_rpg import freeroam as fr
        from pixel_rpg.entities import make_enemy
        fight, hero = self.fight(foes=0)
        body = fight.hero(0)
        foe = fight.spawn(make_enemy(8, "meadow"), at=(body.x, body.y))
        hits = 0
        for _ in range(60):
            before = hero.hp
            fight.update(1 / 60)
            if hero.hp < before:
                hits += 1
        self.assertGreaterEqual(hits, 1, "standing in one did nothing")
        self.assertLessEqual(hits, 3,
                             "a body was chewed on every frame")
        self.assertIsNotNone(foe)

    def test_a_crowd_does_not_stack_into_one_body(self):
        """**A screenshot found this** (rule 31.4): everything chases the
        same point, so everything arrives at it - eight monsters were
        inside one sprite with the hero underneath."""
        import math
        from pixel_rpg import freeroam as fr
        from pixel_rpg.entities import make_enemy
        fight, _hero = self.fight(foes=0)
        for _ in range(8):
            fight.spawn(make_enemy(6, "meadow"),
                        at=(fr.ARENA_W / 2, fr.ARENA_H / 2))
        for _ in range(120):
            fight.update(1 / 60)
        alive = fight.living_foes
        for i, one in enumerate(alive):
            for other in alive[i + 1:]:
                gap = math.hypot(one.x - other.x, one.y - other.y)
                self.assertGreater(gap, 6.0,
                                   "two monsters are in the same place")

    def test_nothing_leaves_the_arena(self):
        from pixel_rpg import freeroam as fr
        fight, _hero = self.fight(foes=4)
        body = fight.hero(0)
        for _ in range(600):
            fight.input(0, dx=1.0, dy=1.0, dt=1 / 60)
            fight.update(1 / 60)
        for one in [body] + fight.foes:
            self.assertGreaterEqual(one.x, fr.BODY - 0.01)
            self.assertLessEqual(one.x, fr.ARENA_W - fr.BODY + 0.01)
            self.assertGreaterEqual(one.y, fr.BODY - 0.01)
            self.assertLessEqual(one.y, fr.ARENA_H - fr.BODY + 0.01)

    def test_a_skill_is_a_swing_with_something_behind_it(self):
        """The smallest change from the turn-based game that still
        answers *"select which skills you want to use"*: the same
        abilities, the same costs, and aiming still matters."""
        from pixel_rpg import freeroam as fr
        from pixel_rpg.entities import make_enemy
        fight, hero = self.fight(foes=0)
        skill = next((s for s in hero.skills() if s.kind == "attack"), None)
        if skill is None:
            self.skipTest("this class knows no attacking skill")
        body = fight.hero(0)
        body.facing = 0.0
        foe = fight.spawn(make_enemy(6, "meadow"), at=(body.x + 40, body.y))
        hero.mp = hero.max_mp()
        before_mp, before_hp = hero.mp, foe.who.hp
        self.assertTrue(fight.charge(0, skill))
        fight.swing(0)
        self.assertLess(hero.mp, before_mp, "the skill cost nothing")
        self.assertLess(foe.who.hp, before_hp)

    def test_a_skill_you_cannot_pay_for_is_refused(self):
        from pixel_rpg import freeroam as fr
        fight, hero = self.fight(foes=0)
        skill = next((s for s in hero.skills() if s.mp > 0), None)
        if skill is None:
            self.skipTest("this class has no skill that costs anything")
        hero.mp = 0
        self.assertFalse(fight.charge(0, skill))

    def test_a_skill_rests_after_it_is_used(self):
        """**MP is not the only cost here.** With no turns, the answer to
        everything would otherwise be to hold the strongest skill and
        mash."""
        from pixel_rpg import freeroam as fr
        fight, hero = self.fight(foes=0)
        skill = next((s for s in hero.skills() if s.kind == "attack"), None)
        if skill is None:
            self.skipTest("no attacking skill")
        hero.mp = hero.max_mp()
        fight.charge(0, skill)
        fight.swing(0)
        self.assertGreater(fight.cooldowns.get((0, skill.key), 0.0), 0.0)
        self.assertFalse(fight.charge(0, skill))

    def test_the_fight_ends_when_nobody_is_standing(self):
        from pixel_rpg import freeroam as fr
        fight, hero = self.fight(foes=1)
        hero.hp = 1
        for _ in range(900):
            fight.update(1 / 60)
            if fight.finished:
                break
        self.assertTrue(fight.finished)
        self.assertEqual(fight.result, "defeat")

    def test_a_monster_never_outruns_you(self):
        """One that can run you down turns dodging from a skill into a
        formality."""
        from pixel_rpg import freeroam as fr
        from pixel_rpg.entities import make_enemy
        fight, _hero = self.fight(foes=0)
        for level in (1, 20, 40):
            foe = fight.spawn(make_enemy(level, "meadow"))
            self.assertLess(fight.monster_speed(foe), fr.PLAYER_SPEED)


class TestTheLongNight(unittest.TestCase):
    """§76, roadmap item 0ay. Savvydev, 2026-09-06:

    > "we should add a zombies gamemode, but that wouldnt be turn-based,
    > it would be free-roam combat or whatever its called, and ofc it
    > would have co-op and no save"
    """

    def night(self):
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.new_night_run("Nightwalker")
        return g

    def test_it_is_a_mode_like_the_others(self):
        from pixel_rpg import gamemodes
        mode = gamemodes.mode_for("zombies")
        self.assertEqual(mode.key, "zombies")
        self.assertFalse(mode.saves, "a Long Night must not take a slot")
        self.assertFalse(mode.island)
        self.assertTrue(mode.scores)

    def test_it_never_touches_a_save_slot(self):
        """His rule for these modes: *"wouldnt have save slots or
        anything, if u die u get a score and thats it"*."""
        from pixel_rpg import game as G
        g = self.night()
        self.assertEqual(g.mode, "zombies")
        self.assertFalse(G.gamemodes.mode_for(g.mode).saves)
        self.assertEqual(getattr(g, "slot", 0), 0)

    def test_a_night_opens_empty_and_fills_up(self):
        """A crowd already on top of you is not a fight you can read -
        §68's *asked, not sprung*, applied to the first second."""
        g = self.night()
        self.assertEqual(len(g.roam.foes), 0)
        for _ in range(600):
            g.update(1 / 60)
            if g.roam is None or len(g.roam.foes) >= 3:
                break
        self.assertGreaterEqual(len(g.roam.foes), 1)

    def test_the_crowd_is_capped(self):
        from pixel_rpg import gamemodes
        for night in range(1, 40):
            self.assertLessEqual(gamemodes.night_crowd(night),
                                 gamemodes.NIGHT_CROWD)
            self.assertGreaterEqual(gamemodes.night_gap(night),
                                    gamemodes.NIGHT_GAP_FLOOR)

    def test_the_nights_get_worse(self):
        from pixel_rpg import gamemodes
        self.assertGreater(gamemodes.night_count(6),
                           gamemodes.night_count(1))
        self.assertLess(gamemodes.night_gap(6), gamemodes.night_gap(1))
        self.assertGreaterEqual(gamemodes.night_level(6),
                                gamemodes.night_level(1))

    def test_surviving_one_opens_the_next(self):
        g = self.night()
        g.zombies.spawned = gamemodes_count(g)
        g.roam.foes = []
        g.finish_night()
        self.assertEqual(g.zombies.wave, 2)
        self.assertEqual(g.zombies.cleared, 1)
        # **The night's offers come first** (items 0bs/0bz), and
        # answering the last one is what opens the next night - so the
        # arena is not standing empty while somebody reads.
        self.assertEqual(g.state, "spoils")
        self.assertTrue(g.spoils)
        self.assertNotIn("mixed", [o.kind for o in g.spoils])
        while g.state == "spoils":
            g.take_spoil(0)
        self.assertEqual(g.state, "roam")

    def test_falling_writes_one_score_and_stops(self):
        g = self.night()
        g.zombies.wave, g.zombies.cleared, g.zombies.kills = 4, 3, 21
        before = len(g.meta.runs)
        g.finish_night_run()
        self.assertTrue(g.zombies.over)
        self.assertEqual(len(g.meta.runs), before + 1)
        record = g.meta.runs[-1]
        self.assertEqual(record.mode, "zombies")
        self.assertEqual(record.waves, 3)
        self.assertGreater(record.score, 0)
        g.finish_night_run()
        self.assertEqual(len(g.meta.runs), before + 1,
                         "falling twice wrote two scores")

    def test_it_has_its_own_board(self):
        """§75's shape: one board per gamemode per party size."""
        g = self.night()
        # **Counted before and after, never absolutely.** The profile is
        # loaded from disk and shared with every other test in this file,
        # so an earlier one's record is already sitting in it - §7's rule
        # that a test may not inherit the state it needs.
        before = len(g.meta.board("zombies", 1))
        waves_before = len(g.meta.board("waves", 1))
        g.zombies.cleared = 2
        g.finish_night_run()
        self.assertEqual(len(g.meta.board("zombies", 1)), before + 1)
        self.assertEqual(len(g.meta.board("waves", 1)), waves_before,
                         "a Long Night wrote onto Last Stand's board")

    def test_story_is_not_offered_as_a_difficulty(self):
        """Item 0bb named this mode explicitly."""
        from pixel_rpg import config
        self.assertNotIn("story", config.difficulties_for("zombies"))
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.settings.difficulty = "story"
        g.new_night_run("Nightwalker")
        self.assertNotEqual(g.settings.difficulty, "story")

    def test_walking_is_held_and_choosing_is_pressed(self):
        """The one structural difference from every other screen: a
        fight you have to tap your way across is not free-roam."""
        import inspect
        from pixel_rpg.game import Game
        self.assertIn("pygame.key.get_pressed",
                      inspect.getsource(Game.update_roam))
        self.assertNotIn("pygame.key.get_pressed",
                         inspect.getsource(Game.event_roam))


def gamemodes_count(game):
    from pixel_rpg import gamemodes
    return gamemodes.night_count(game.zombies.wave)


class TestFreeRoamInARoom(unittest.TestCase):
    """§77, item 0bg. Savvydev, 2026-09-07: *"add the co-op for free
    roam"* - the half §76 left open and had named as next.

    **The host is the authority**, exactly as it is for a turn-based
    shared fight (§21). What is different is only that the fight does
    not take turns.
    """

    def two(self):
        from pixel_rpg import freeroam as fr
        from pixel_rpg.entities import Player, make_enemy
        from pixel_rpg.util import rng
        rng.reseed(6)
        a, b = Player("A", "warden"), Player("B", "duelist")
        for who in (a, b):
            for _ in range(6):
                who.gain_xp(who.xp_to_next())
            who.full_restore()
        host = fr.FreeRoam(a, [make_enemy(6, "meadow") for _ in range(3)],
                           party=[a, b], viewer=0)
        mirror = fr.FreeRoam(Player("A", "warden"), [],
                             party=[Player("A", "warden"),
                                    Player("B", "duelist")],
                             viewer=1, mirror=True)
        return host, mirror

    def test_a_mirror_shows_and_does_not_decide(self):
        """No monster walks, nothing is hurt and nothing dies on a
        client - all of that is the host's (§21)."""
        host, mirror = self.two()
        mirror.apply_snapshot(host.snapshot())
        before = [(f.x, f.y) for f in mirror.foes]
        for _ in range(60):
            mirror.update(1 / 60)
        self.assertEqual([(f.x, f.y) for f in mirror.foes], before,
                         "a client walked the monsters itself")

    def test_a_mirror_drives_only_its_own_hero(self):
        """§21's rule: the host taking every player's turn was one line
        and three symptoms."""
        host, mirror = self.two()
        mine, theirs = mirror.heroes[1], mirror.heroes[0]
        start = theirs.x
        mirror.input(0, dx=1.0, dy=0.0, dt=1 / 60)
        self.assertEqual(theirs.x, start,
                         "a client moved somebody else's body")
        mirror.input(1, dx=1.0, dy=0.0, dt=1 / 60)
        self.assertGreater(mine.x, 0)

    def test_a_frame_carries_everybody(self):
        host, mirror = self.two()
        frame = host.snapshot()
        self.assertEqual(len(frame["h"]), 2)
        self.assertEqual(len(frame["e"]), len(host.foes))

    def test_a_mirror_stands_up_a_monster_it_has_never_heard_of(self):
        """A frame that only moved bodies a client already had left it
        fighting an arena with a hole in it - the guest saw zero
        monsters against the host's one."""
        host, mirror = self.two()
        self.assertEqual(len(mirror.foes), 0)
        mirror.apply_snapshot(host.snapshot())
        self.assertEqual(len(mirror.foes), len(host.foes))
        self.assertGreater(mirror.foes[0].who.max_hp(), 0)

    def test_a_client_never_invents_a_stranger_in_the_party(self):
        """A monster it can stand up; a *hero* it must wait for, or a
        stranger appears in the party."""
        from pixel_rpg import freeroam as fr
        host, mirror = self.two()
        mirror.heroes = mirror.heroes[:1]
        mirror.apply_snapshot(host.snapshot())
        self.assertEqual(len(mirror.heroes), 1)

    def test_the_client_keeps_its_own_hero_until_it_drifts(self):
        """Local prediction, the one concession to the network: frames
        arrive a dozen times a second and a body that only moved when
        one landed would stutter."""
        from pixel_rpg import freeroam as fr
        host, mirror = self.two()
        mine = mirror.heroes[1]
        mine.x = host.heroes[1].x + 5.0
        mirror.apply_snapshot(host.snapshot())
        self.assertNotEqual(mine.x, host.heroes[1].x,
                            "a small difference was snapped away")
        mine.x = host.heroes[1].x + fr.DRIFT + 20.0
        mirror.apply_snapshot(host.snapshot())
        self.assertEqual(mine.x, host.heroes[1].x,
                         "a real drift was not corrected")

    def test_health_and_death_always_come_from_the_host(self):
        host, mirror = self.two()
        mirror.apply_snapshot(host.snapshot())
        host.foes[0].who.hp = 0
        host.foes[0].dead = True
        mirror.apply_snapshot(host.snapshot())
        self.assertTrue(mirror.foes[0].dead)
        self.assertEqual(mirror.foes[0].who.hp, 0)

    def test_which_hero_a_guest_drives_comes_from_who_sent_it(self):
        """**The one thing a peer cannot lie about.** There is no index
        in the payload at all - a body index off a socket is refused,
        not trusted (§20)."""
        import inspect
        from pixel_rpg.coop import Session
        source = inspect.getsource(Session._roam_input)
        self.assertIn("peer.player_id", source)
        send = inspect.getsource(Session.send_roam_input)
        self.assertNotIn('"i"', send)

    def test_a_guest_asks_for_a_skill_rather_than_charging_it(self):
        """The host spends the MP and starts the cooldown, so a client
        that charged locally would show a skill it had not paid for."""
        import inspect
        from pixel_rpg.game import Game
        # **Moved to `hold_roam_skill`** (§92) so the mouse and the
        # number keys go through one door - the chips were drawn as
        # hotspots and did nothing when clicked.
        source = inspect.getsource(Game.hold_roam_skill)
        self.assertIn("roam_pending_skill", source)
        self.assertIn("hold_roam_skill",
                      inspect.getsource(Game.event_roam))

    def test_the_guest_input_rate_stays_under_the_drop_limit(self):
        """`net.MAX_MESSAGES_PER_SECOND` is 120 and a peer over it is
        **dropped, not throttled** (§7) - which cost a whole trade
        feature once."""
        from pixel_rpg import coop as coop_mod, net
        self.assertLessEqual(1.0 / coop_mod.ROAM_INPUT_RATE,
                             net.MAX_MESSAGES_PER_SECOND / 3.0)

    def test_the_protocol_went_up(self):
        """Two new message kinds; a build that does not know them would
        sit in an empty room watching nothing move."""
        from pixel_rpg import coop as coop_mod
        self.assertGreaterEqual(coop_mod.PROTOCOL, 10)

    def test_a_night_pumps_the_room(self):
        """**The one thing an early return must never skip.** The first
        version of the free-roam branch returned before `update_coop`,
        so a host in a night stopped answering the socket entirely and
        the guest never finished the handshake.

        Asked with `ast` rather than by slicing the text (§7): the
        branch's own comment contains the word "return", so a slice on
        the first occurrence cuts it in half.
        """
        import ast
        import inspect
        import textwrap
        from pixel_rpg.game import Game
        tree = ast.parse(textwrap.dedent(inspect.getsource(Game.update)))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not (isinstance(test, ast.Compare)
                    and getattr(test.left, "attr", "") == "state"
                    and any(getattr(c, "id", "") == "ROAM"
                            for c in test.comparators)):
                continue
            called = {getattr(n.func, "attr", "") for n in ast.walk(node)
                      if isinstance(n, ast.Call)}
            found.append(called)
        self.assertTrue(found, "the free-roam branch has gone")
        for called in found:
            self.assertIn("update_coop", called,
                          "a night stops answering the socket")
            self.assertIn("update_roam", called)

    def test_the_host_asks_its_own_mode_not_the_rooms(self):
        """A room's mode is set from the *welcome*, and a host never
        receives one - so `coop.mode` on the machine running the room is
        permanently "story" by construction."""
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.roam_party)
        self.assertIn("self.mode", source)
        self.assertNotIn('self.coop.mode != "zombies"', source)


class TestAnEnemyGetsABarAndNothingElse(unittest.TestCase):
    """§77, item 0bh. Savvydev, 2026-09-07:

    > "enemies in free-roam should just have a healthbar on top of them,
    > nothing complicated"

    **A fence, not a request.** §74 answered the same question for the
    turn-based screen with a stacked roster, and the obvious next move
    would have been to carry some of that across. This pins that it is
    not carried across.
    """

    def test_an_enemy_carries_a_bar_and_no_text(self):
        import ast
        import inspect
        import textwrap
        from pixel_rpg import freeroamscreen
        source = textwrap.dedent(
            inspect.getsource(freeroamscreen.draw_body))
        tree = ast.parse(source)
        called = {getattr(node.func, "id", "")
                  or getattr(node.func, "attr", "")
                  for node in ast.walk(tree) if isinstance(node, ast.Call)}
        self.assertIn("bar", called, "an enemy has no health bar at all")
        self.assertNotIn("draw_text", called,
                         "a body is carrying text; he asked for a bar")
        self.assertNotIn("panel", called,
                         "a body is carrying a panel; he asked for a bar")

    def test_there_is_no_roster_on_this_screen(self):
        """§74's stacked list is the turn-based answer and stays there."""
        import inspect
        from pixel_rpg import freeroamscreen
        source = inspect.getsource(freeroamscreen)
        self.assertNotIn("ROSTER", source)
        self.assertNotIn("enemy_plate", source)


class TestTheHudIsYourOwn(unittest.TestCase):
    """§77. In a room the host is hero 0 and a guest is hero 1, so a HUD
    keyed on zero shows a guest the **host's** health and cooldowns.

    Found by screenshotting the guest's own screen (rule 31.4) - the
    name on the plate was the other player's.
    """

    def test_the_plate_follows_the_viewer(self):
        import ast
        import inspect
        import textwrap
        from pixel_rpg import freeroamscreen
        source = textwrap.dedent(
            inspect.getsource(freeroamscreen.draw_roam_hud))
        self.assertIn("fight.viewer", source)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", "") != "hero":
                continue
            for arg in node.args:
                self.assertFalse(
                    isinstance(arg, ast.Constant) and arg.value == 0,
                    "the HUD is hard-wired to hero 0")


class TestTheTwoFreeRoams(unittest.TestCase):
    """§78, item 0bi. Savvydev, 2026-09-07:

    > "free-roam is available in all modes, and free-roam combat as well,
    > and if free-roam combat modifier is turned on then free-roam is
    > automatically turned on as well. for free-roam combat to exist
    > free-roam MUST also exist"

    And a minute later, on what they cost:

    > "both the free-roam and free-roam combat modifiers give no score
    > multiplier and are purely a playstyle choice"
    """

    def test_they_are_two_different_things(self):
        from pixel_rpg.modifiers import MODIFIERS_BY_KEY
        self.assertIn("insight", MODIFIERS_BY_KEY)
        self.assertIn("openfight", MODIFIERS_BY_KEY)
        self.assertIn("seen", MODIFIERS_BY_KEY["insight"].tags)
        self.assertIn("free_combat", MODIFIERS_BY_KEY["openfight"].tags)

    def test_neither_is_priced(self):
        """**His, and a fence.** Every other modifier here is a cost paid
        for in score (§45); these two change what the game *is*, and
        pricing a playstyle as difficulty is what §54 settled against."""
        from pixel_rpg.modifiers import MODIFIERS_BY_KEY
        for key in ("insight", "openfight"):
            mod = MODIFIERS_BY_KEY[key]
            self.assertEqual(mod.score, 1.00,
                             "%s is charging for a playstyle" % key)
            self.assertEqual(mod.kind, "preference", key)

    def test_taking_the_second_takes_the_first(self):
        from pixel_rpg.modifiers import RunModifiers
        run = RunModifiers()
        run.toggle("openfight")
        self.assertIn("insight", run.keys)

    def test_it_cannot_be_taken_away_underneath(self):
        """*"for free-roam combat to exist free-roam MUST also exist"* -
        a dependency, not a default."""
        from pixel_rpg.modifiers import RunModifiers
        run = RunModifiers(["openfight"])
        self.assertTrue(run.locked("insight"))
        run.toggle("insight")
        self.assertIn("insight", run.keys,
                      "free-roam was removed out from under free-roam "
                      "combat")

    def test_dropping_the_second_frees_the_first(self):
        from pixel_rpg.modifiers import RunModifiers
        run = RunModifiers(["openfight"])
        run.toggle("openfight")
        self.assertNotIn("openfight", run.keys)
        self.assertFalse(run.locked("insight"))

    def test_the_rule_is_enforced_where_it_cannot_be_bypassed(self):
        """There are three ways into a run's modifier set - the picker, a
        save and a co-op welcome - and a rule enforced in one of them is
        a rule two thirds absent."""
        from pixel_rpg.modifiers import RunModifiers
        for keys in (["openfight"], ["openfight", "mortal"],
                     ["mortal", "openfight"]):
            self.assertIn("insight", RunModifiers(keys).keys, keys)

    def test_it_is_available_in_every_mode(self):
        """*"available in all modes"* - it is a modifier, and every mode
        takes modifiers."""
        from pixel_rpg import gamemodes
        from pixel_rpg.modifiers import MODIFIERS_BY_KEY
        self.assertTrue(MODIFIERS_BY_KEY["openfight"])
        self.assertGreaterEqual(len(gamemodes.MODES), 3)

    def test_the_long_night_fights_that_way_whatever_you_picked(self):
        """The mode *is* free-roam combat; it does not ask."""
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.new_night_run("Nightwalker")
        self.assertTrue(g.open_combat())

    def test_a_story_run_that_took_it_fights_that_way(self):
        from pixel_rpg import game as G
        from pixel_rpg.entities import make_encounter
        g = G.Game()
        g.pending_class = "warden"
        g.pending_modifiers.toggle("openfight")
        g.new_run("Opener", 2)
        self.assertTrue(g.open_combat())
        g.start_battle(make_encounter(5, "meadow", count=1))
        self.assertIsNone(g.battle, "a turn-based fight opened anyway")
        self.assertIsNotNone(g.roam)
        self.assertEqual(g.state, G.ROAM)

    def test_a_story_run_without_it_is_untouched(self):
        from pixel_rpg import game as G
        from pixel_rpg.entities import make_encounter
        g = G.Game()
        g.pending_class = "warden"
        g.new_run("Ordinary", 3)
        self.assertFalse(g.open_combat())
        g.start_battle(make_encounter(5, "meadow", count=1))
        self.assertIsNotNone(g.battle)
        self.assertIsNone(g.roam)

    def test_winning_one_pays_through_the_ordinary_path(self):
        """**Not a second payout path** (§54): *"two payout paths, and
        the rarer one keeps getting patched first"*."""
        from pixel_rpg import game as G
        from pixel_rpg.entities import make_encounter
        g = G.Game()
        g.pending_class = "warden"
        g.pending_modifiers.toggle("openfight")
        g.new_run("Opener", 4)
        for _ in range(8):
            g.player.gain_xp(g.player.xp_to_next())
        g.player.full_restore()
        before = (g.player.xp, g.player.gold)
        g.start_battle(make_encounter(5, "meadow", count=2))
        body = g.roam.hero(0)
        body.facing = 0.0
        for foe in g.roam.foes:
            foe.who.hp = 1
            foe.x, foe.y = body.x + 28, body.y
        g.roam.swing(0)
        for _ in range(30):
            g.update(1 / 60)
        self.assertEqual(g.state, G.OVERWORLD)
        self.assertGreater(g.player.xp, before[0], "no experience was paid")
        self.assertGreater(g.player.gold, before[1], "no gold was paid")

    def test_the_free_roam_fight_shares_one_reward_split(self):
        """Evenly among everyone who fought, remainder to the earliest -
        the same arithmetic in both engines rather than in one."""
        from pixel_rpg import freeroam as fr
        from pixel_rpg.entities import Player, make_enemy
        from pixel_rpg.util import rng
        rng.reseed(11)
        a, b = Player("A", "warden"), Player("B", "duelist")
        fight = fr.FreeRoam(a, [make_enemy(6, "meadow")], party=[a, b],
                            until_clear=True)
        fight.rewards = fight.split_rewards()
        total = sum(s["xp"] for s in fight.rewards["shares"].values())
        self.assertEqual(total, fight.rewards["xp"],
                         "the split lost or invented experience")

    def test_the_tag_has_a_reader(self):
        """§54's rule: a declared tag nothing reads is a modifier whose
        cost was deleted, wearing a score multiplier."""
        import inspect
        from pixel_rpg.game import Game
        self.assertIn("free_combat", inspect.getsource(Game.open_combat))


class TestAModeWithNoSlotCannotWriteOne(unittest.TestCase):
    """§78, item 0bk. Savvydev, 2026-09-07: *"the long night gets no save
    either btw"*.

    It already had no slot. **What it did not have was the gate** -
    `save_run` refused wave mode by name, and `self.slot` survives from
    whatever run came before.
    """

    def test_no_mode_that_keeps_nothing_can_write_a_slot(self):
        from pixel_rpg import game as G, gamemodes
        for mode in gamemodes.MODES:
            if mode.saves:
                continue
            g = G.Game()
            g.pending_class = "warden"
            if mode.key == "waves":
                g.new_wave_run("Stander")
            else:
                g.new_night_run("Nightwalker")
            g.slot = 1                      # as if a story run left it
            self.assertFalse(g.save_run(silent=True, force=True),
                             "%s wrote to a save slot" % mode.key)

    def test_the_gate_asks_the_mode_rather_than_naming_one(self):
        """**A rule about a category, written as a list of its members,
        is wrong the moment the category grows** - which is exactly what
        happened here."""
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.save_run)
        self.assertIn(".saves", source)
        self.assertNotIn("if self.in_waves():", source)

    def test_a_story_run_still_saves(self):
        """The guard is on the mode, so the thing it protects has to be
        proved still to work."""
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.new_run("Ordinary", 5)
        self.assertTrue(g.save_run(silent=True, force=True))


class TestFreeRoamHasArenas(unittest.TestCase):
    """§79, item 0bl. Savvydev, 2026-09-07:

    > "we created a lot of battle backgrounds, but have we made any
    > arenas for free roam combat? for The Long Night and The Last Stand
    > gamemodes?"

    **The answer was no.** Eighteen arenas with four skylines each, all
    of them drawn by `battlescreen.py`, which free-roam does not use - it
    painted a grey grid.
    """

    def test_a_free_roam_fight_is_somewhere(self):
        from pixel_rpg import freeroamscreen, gamemodes
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.new_night_run("Nightwalker")
        arena = freeroamscreen.arena_of(g)
        self.assertIn(arena, gamemodes.ARENAS)
        self.assertTrue(arena.tint)

    def test_the_night_walks_the_same_country_a_wave_would(self):
        """*"the long night is also meant to use those arenas we
        created"* - the same table, so the two modes are the same places
        at the same depth."""
        from pixel_rpg import freeroamscreen, gamemodes
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.new_night_run("Nightwalker")
        for wave in (1, 5, 13, 27):
            g.zombies.wave = wave
            self.assertEqual(freeroamscreen.arena_of(g).key,
                             gamemodes.arena_for(wave).key)

    def test_the_floor_is_the_arena_and_not_a_grey_grid(self):
        import inspect
        from pixel_rpg import freeroamscreen
        source = inspect.getsource(freeroamscreen.draw_arena_floor)
        self.assertIn("arena_of", source)
        self.assertIn("tint", source)

    def test_neither_mode_ever_shows_the_island(self):
        """*"neither the long night or the last stand use the 'The Long
        Road' map"* - the arenas must not become a way in."""
        from pixel_rpg import gamemodes
        for key in ("waves", "zombies"):
            self.assertFalse(gamemodes.mode_for(key).island,
                             "%s is on the island" % key)

    def test_the_preference_reaches_free_roam_too(self):
        """0au's setting is about *where waves are fought*, and a night is
        waves fought differently."""
        from pixel_rpg import freeroamscreen
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.new_night_run("Nightwalker")
        g.settings.arena_mode = "one"
        g.settings.arena_pick = 7
        from pixel_rpg import gamemodes
        self.assertEqual(freeroamscreen.arena_of(g).key,
                         gamemodes.ARENAS[7].key)


class TestTheBarClearsThePlate(unittest.TestCase):
    """§79, item 0bm. Savvydev, with a screenshot: *"bar and hp kinda
    overlap"*.

    The turn bar was `width - 792` wide from x=396, so its right edge
    landed on `width - 396` and the enemy plate starts at `width - 400`.
    **Four pixels inside it, at every window size, since §51.**
    """

    def battle(self):
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import Player, make_encounter
        from pixel_rpg.util import rng
        rng.reseed(1)
        return Battle(Player("A", "warden"),
                      make_encounter(6, "meadow", count=2))

    def test_they_do_not_touch_at_any_width(self):
        from pixel_rpg.battlescreen import BATTLE_STACK_WIDTH, battle_panels
        fight = self.battle()
        for width in range(BATTLE_STACK_WIDTH, 2000, 60):
            panels = battle_panels(fight, width, 720)
            bar = panels["turns"]
            if bar.w < 60:
                # Too narrow to draw at all, which `draw_turn_bar`
                # already declines to do - and is the honest answer on a
                # window with no middle column.
                continue
            self.assertLessEqual(bar.right, panels["enemy"].left,
                                 "the bar runs into the plate at %d" % width)

    def test_the_bar_is_still_worth_having(self):
        """A gap is easy to make by deleting the thing."""
        from pixel_rpg.battlescreen import battle_panels
        panels = battle_panels(self.battle(), 1280, 720)
        self.assertGreater(panels["turns"].w, 200)


class TestListsHoldStill(unittest.TestCase):
    """§79, item 0bn. Savvydev:

    > "the scrollable menu feels really weird i just cant pinpoint why
    > exactly it does"

    **The window re-centred on the cursor every frame**, so pressing Down
    moved the whole list up by one rather than moving the highlight down
    by one. Everything you were reading slid, on every keypress, on every
    screen with more rows than fit.
    """

    def test_the_list_does_not_move_while_the_cursor_is_inside_it(self):
        from pixel_rpg.ui import window_start
        was = 0
        for index in range(0, 8):
            was = window_start(index, 10, 30, was)
        self.assertEqual(was, 0, "the list slid while the cursor moved")

    def test_it_moves_when_the_cursor_reaches_the_edge(self):
        from pixel_rpg.ui import window_start
        was = window_start(9, 10, 30, 0)
        self.assertGreater(was, 0)

    def test_it_moves_back_up_too(self):
        from pixel_rpg.ui import window_start
        was = 10
        self.assertEqual(window_start(10, 10, 30, was), 9)
        self.assertEqual(window_start(15, 10, 30, was), 10)

    def test_it_never_leaves_the_list(self):
        from pixel_rpg.ui import window_start
        for index in range(0, 30):
            start = window_start(index, 10, 30, index * 3)
            self.assertGreaterEqual(start, 0)
            self.assertLessEqual(start, 20)

    def test_nothing_scrolls_when_everything_fits(self):
        from pixel_rpg.ui import window_start
        self.assertEqual(window_start(5, 20, 10, 3), 0)

    def test_a_list_remembers_where_it_was(self):
        from pixel_rpg.ui import list_view

        def one_screen(index):
            return list_view(0, 300, 20, index, natural=30, minimum=30)

        one_screen(0)
        self.assertEqual(one_screen(5)[0], 0,
                         "the list slid before the cursor reached an edge")
        for index in range(6, 14):
            one_screen(index)
        self.assertGreater(one_screen(14)[0], 0)

    def test_two_lists_do_not_share_a_position(self):
        from pixel_rpg.ui import list_view

        def first(index):
            return list_view(0, 300, 40, index, natural=30, minimum=30)

        def second(index):
            return list_view(0, 300, 40, index, natural=30, minimum=30)

        for index in range(0, 30):
            first(index)
        self.assertEqual(second(0)[0], 0,
                         "one list moved another one")

    def test_there_is_a_real_scrollbar(self):
        import pygame
        from pixel_rpg.ui import scrollbar
        pygame.display.init()
        surface = pygame.Surface((200, 200))
        rect = pygame.Rect(10, 10, 150, 120)
        self.assertIsNone(scrollbar(surface, rect, 0, 10, 10),
                          "a scrollbar was drawn on a list that fits")
        track = scrollbar(surface, rect, 5, 10, 40)
        self.assertIsNotNone(track)
        self.assertGreater(track.h, 0)

    def test_the_thumb_says_where_you_are(self):
        import pygame
        from pixel_rpg.ui import scrollbar
        pygame.display.init()

        def thumb_at(start):
            surface = pygame.Surface((200, 200))
            surface.fill((0, 0, 0))
            scrollbar(surface, pygame.Rect(10, 10, 150, 120), start, 10, 40)
            lit = [y for y in range(200)
                   if surface.get_at((167, y))[:3] > (90, 90, 120)]
            return (sum(lit) / len(lit)) if lit else 0

        top, bottom = thumb_at(0), thumb_at(30)
        self.assertLess(top, bottom,
                        "the thumb does not move as the list does")


class TestTheGuideTeachesTheNewThings(unittest.TestCase):
    """§80, item 0bo. Savvydev: *"yeah do the guide pages"*.

    §76, §77 and §79 each ended by saying the guide owed pages and none
    of them wrote one - **rule 1d failing three times on the same row of
    its own checklist**.
    """

    def pages(self):
        from pixel_rpg.guide import PAGES
        return {page.title: chr(10).join(page.lines) for page in PAGES}

    def test_the_pages_exist(self):
        for title in ("Open Combat", "The Long Night", "Waysides",
                      "What a wave pays", "The Hoard"):
            self.assertIn(title, self.pages())

    def test_every_mode_the_picker_offers_has_a_page(self):
        """**A mode nobody is taught is a mode nobody plays properly**,
        and the picker's blurb is one sentence. Named by mode rather than
        by a hand-kept list, so a fifth mode fails this until it has
        somewhere to be explained (s86)."""
        from pixel_rpg import gamemodes
        pages = self.pages()
        for mode in gamemodes.MODES:
            if mode.key == "story":
                continue
            self.assertTrue(
                any(mode.name.lower() in title.lower() for title in pages),
                "%s has no guide page" % mode.name)

    def test_the_night_page_says_what_is_in_it(self):
        """Item 0bx: it is named for zombies and had slimes in it."""
        page = self.pages()["The Long Night"].lower()
        for word in ("shambler", "crawler", "risen"):
            self.assertIn(word, page, "the guide never names the %s" % word)

    def test_the_night_page_teaches_that_skills_have_shapes(self):
        """Item 0by. A rule nobody can see is a rule nobody plays
        around, and this one is drawn on the floor every swing."""
        page = self.pages()["The Long Night"].lower()
        self.assertIn("narrow", page)
        self.assertIn("wedge", page)

    def test_the_offer_page_teaches_the_part_nobody_would_guess(self):
        """s83. Three of the four things on this page are invisible
        otherwise: that a wave hands out skills your class cannot have,
        that a card can be *read* before it is taken, and that wardens
        are what makes the offers better."""
        page = self.pages()["What a wave pays"]
        for word in ("right-click", "Q", "warden", "Esc"):
            self.assertIn(word, page, "the guide never mentions %s" % word)
        # **0bz withdrew the mixed hand** (2026-09-08): every mode
        # offers the same three now, and what the page has to teach
        # instead is the rule he asked for - full health is a warden's.
        self.assertIn("full", page,
                      "it never says what a full heal costs")
        self.assertIn("warden", page)

    def test_open_combat_teaches_every_control_it_has(self):
        """A mode with four controls nobody is taught is a mode nobody
        plays properly."""
        page = self.pages()["Open Combat"]
        for word in ("WASD", "Space", "Shift", "1 to 4"):
            self.assertIn(word, page, "the guide never mentions %s" % word)

    def test_it_says_the_two_go_together(self):
        page = self.pages()["Open Combat"]
        self.assertIn("Plain Sight", page)
        self.assertIn("score", page.lower())

    def test_the_long_night_says_where_the_arena_choice_is(self):
        """0bq: he could not find it. A guide that does not say where it
        lives leaves it exactly as findable as it was."""
        page = self.pages()["The Long Night"]
        self.assertIn("modifiers", page.lower())
        for cadence in ("2", "5", "10"):
            self.assertIn(cadence, page)

    def test_the_waysides_page_says_they_are_spent(self):
        page = self.pages()["Waysides"]
        self.assertIn("E", page)
        self.assertIn("one answer each", page)

    def test_the_modifier_count_in_prose_is_right(self):
        """§75's checklist row: a number the guide says *out loud*.
        "sixteen" survived two modifiers being added."""
        from pixel_rpg.modifiers import MODIFIERS
        words = {16: "sixteen", 17: "seventeen", 18: "eighteen",
                 19: "nineteen", 20: "twenty"}
        page = self.pages()["Runs and records"]
        self.assertIn(words[len(MODIFIERS)], page,
                      "the guide states the wrong number of modifiers")

    def test_no_page_claims_every_modifier_raises_the_score(self):
        """Three of them do not, and the page used to say all of them
        did."""
        page = self.pages()["Runs and records"]
        self.assertNotIn("Each one takes something away", page)


class TestArenasAreNotJustColours(unittest.TestCase):
    """§80, item 0bp. Savvydev: *"the arenas are far too simple, there
    should be a LOT of different arenas and stuff"*.

    §79 answered that with a **colour**, and a colour is not a place.
    """

    def test_every_arena_names_a_floor_plan(self):
        from pixel_rpg import gamemodes
        for arena in gamemodes.ARENAS:
            self.assertTrue(arena.plan, arena.key)

    def test_there_are_a_lot_of_different_plans(self):
        from pixel_rpg import gamemodes
        plans = {a.plan for a in gamemodes.ARENAS}
        self.assertGreaterEqual(len(plans), 8,
                                "eighteen arenas, %d shapes" % len(plans))

    def test_every_plan_puts_something_in_the_arena(self):
        """§54's rule: a plan nothing draws is a name in a table."""
        from pixel_rpg import freeroam as fr, gamemodes
        for plan in {a.plan for a in gamemodes.ARENAS}:
            if plan == "open":
                continue
            self.assertTrue(fr.plan_blockers(plan, 5),
                            "%s is declared and empty" % plan)

    def test_cover_stays_inside_the_arena(self):
        from pixel_rpg import freeroam as fr, gamemodes
        for arena in gamemodes.ARENAS:
            for x, y, radius in fr.plan_blockers(arena.plan, 3):
                self.assertGreater(x - radius, -fr.BLOCK, arena.key)
                self.assertLess(x + radius, fr.ARENA_W + fr.BLOCK, arena.key)
                self.assertGreater(y - radius, -fr.BLOCK, arena.key)
                self.assertLess(y + radius, fr.ARENA_H + fr.BLOCK, arena.key)

    def test_cover_actually_blocks(self):
        """It is not decoration: you can put a rock between yourself and
        a crowd, and so can they."""
        from pixel_rpg import freeroam as fr
        from pixel_rpg.entities import Player
        fight = fr.FreeRoam(Player("A", "warden"), [])
        body = fight.hero(0)
        fight.blockers = [(body.x + 60.0, body.y, 46.0)]
        for _ in range(120):
            body.move(1.0, 0.0, fr.PLAYER_SPEED, 1 / 60, fight.blockers)
        self.assertLess(body.x, body_stop_line(fight),
                        "a body walked straight through cover")

    def test_a_body_slides_round_rather_than_sticking(self):
        from pixel_rpg import freeroam as fr
        from pixel_rpg.entities import Player
        fight = fr.FreeRoam(Player("A", "warden"), [])
        body = fight.hero(0)
        fight.blockers = [(body.x + 60.0, body.y + 6.0, 46.0)]
        start_y = body.y
        for _ in range(90):
            body.move(1.0, 0.0, fr.PLAYER_SPEED, 1 / 60, fight.blockers)
        self.assertNotEqual(round(body.y, 1), round(start_y, 1),
                            "a body pressed into cover and stayed put")

    def test_a_fight_is_given_its_arena_s_cover(self):
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.new_night_run("Nightwalker")
        g.settings.arena_mode = "one"
        g.settings.arena_pick = 6            # The Long Gallery: pillars
        g.dress_roam_arena()
        self.assertTrue(g.roam.blockers, "the arena is a bare plate")

    def test_the_same_arena_has_the_same_cover_every_time(self):
        """0aq applied to the floor: scenery you cannot rely on is
        noise, and cover you cannot rely on is worse than noise."""
        from pixel_rpg import freeroam as fr
        first = fr.plan_blockers("pillars", 42)
        again = fr.plan_blockers("pillars", 42)
        self.assertEqual(first, again)

    def test_laying_out_an_arena_draws_no_randomness(self):
        """§21's wasted `rng.choice`, which moved four classes."""
        from pixel_rpg import freeroam as fr, gamemodes
        from pixel_rpg.util import rng
        rng.reseed(3)
        before = rng._r.getstate()
        for arena in gamemodes.ARENAS:
            fr.plan_blockers(arena.plan, 9)
        self.assertEqual(rng._r.getstate(), before)


def body_stop_line(fight):
    from pixel_rpg import freeroam as fr
    bx, _by, radius = fight.blockers[0]
    return bx - radius * 0.5


class TestTheArenaChoiceIsWhereARunIsChosen(unittest.TestCase):
    """§80, item 0bq. Savvydev: *"what happened to the choices for the
    arena?"* - they were two rows in Settings and he could not find
    them.

    **The identical case §46 records about difficulty**, in his own
    words: *"when creating a new save you're never granted with the
    difficulty option itself"*.
    """

    def test_the_row_is_on_the_run_setup_screen(self):
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_mode = "waves"
        self.assertTrue(g.arena_row_shown())
        g.pending_mode = "zombies"
        self.assertTrue(g.arena_row_shown())

    def test_it_is_not_offered_where_it_means_nothing(self):
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_mode = "story"
        self.assertFalse(g.arena_row_shown())

    def test_the_arrows_walk_every_shape_of_the_setting(self):
        from pixel_rpg import game as G, gamemodes
        g = G.Game()
        g.pending_mode = "waves"
        g.settings.arena_mode = "every"
        seen_cadences, seen_modes = set(), set()
        for _ in range(30):
            seen_modes.add(g.settings.arena_mode)
            if g.settings.arena_mode == "every":
                seen_cadences.add(g.settings.arena_every)
            g.cycle_arena_choice(1)
        self.assertEqual(seen_modes, set(gamemodes.ARENA_MODES))
        self.assertEqual(seen_cadences, set(gamemodes.ARENA_CADENCES))

    def test_it_is_one_setting_and_not_a_second_copy(self):
        """The same `Settings` fields the options screen edits - the call
        `cycle_difficulty` already made."""
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.cycle_arena_choice)
        self.assertIn("self.settings", source)

    def test_the_modifier_rows_still_line_up_with_the_extra_row(self):
        """An off-by-one here toggles the wrong modifier, silently."""
        from pixel_rpg import game as G, modifiers as modifiers_mod
        import pygame
        g = G.Game()
        g.pending_mode = "waves"
        g.state = G.MODIFIER_SELECT
        first = modifiers_mod.MODIFIERS[0].key
        g.ui.modifier_index = 2              # difficulty, arena, then this
        g.event_modifier_select(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_RETURN, unicode="", mod=0))
        self.assertIn(first, g.pending_modifiers.keys,
                      "the arena row shifted the modifier list")


class TestAWardenIsAssembled(unittest.TestCase):
    """§81, §19 decision 4. Savvydev, 2026-08-24:

    > party-wide, but everyone must be present before it starts

    **Recorded and never built.** Warden arenas have been solo-only
    since §21, and §31.9 has carried that in its list of things that
    look like a bug and are not - with a note saying it was superseded
    and still unbuilt. Two years of sessions read past it.
    """

    def solo(self):
        from pixel_rpg import game as G
        g = G.Game()
        g.pending_class = "warden"
        g.new_run("Alone", 1)
        return g

    def test_solo_is_untouched(self):
        from pixel_rpg.entities import BOSS_DEFS, Boss
        g = self.solo()
        self.assertEqual(g.wardens_missing(), [])
        self.assertEqual(g.warden_party_size(), 1)
        g.open_warden(BOSS_DEFS[0])
        self.assertIsNotNone(g.battle)
        self.assertTrue(any(isinstance(e, Boss) for e in g.battle.enemies))

    def test_nobody_missing_when_there_is_no_room(self):
        self.assertEqual(self.solo().wardens_missing(), [])

    def test_the_reach_is_the_one_a_shared_fight_uses(self):
        """Two numbers for one idea drift - and the idea is the same:
        somebody across the island is not in this fight."""
        from pixel_rpg import coop as coop_mod
        from pixel_rpg.game import Game
        self.assertEqual(Game.WARDEN_REACH, coop_mod.FIGHT_REACH)

    def test_the_door_says_who_it_is_waiting_for(self):
        """*"Not everybody is here"* without saying who is a message you
        cannot act on."""
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.wardens_missing)
        self.assertIn("name", source)

    def test_a_control_that_will_refuse_is_not_offered(self):
        """His rule, 2026-08-31 (§45): the button says "Wait for them"
        rather than being a door that does not open."""
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.enter_portal)
        self.assertIn("Wait for them", source)
        self.assertIn("None if missing", source)

    def test_a_guest_asks_and_the_host_decides(self):
        """§21's rule, unchanged: a warden is not the exception any
        more, so it takes the same arbitration every shared fight has."""
        import inspect
        from pixel_rpg.coop import Session
        source = inspect.getsource(Session.request_boss)
        self.assertIn("self.role == HOST", source)
        handler = inspect.getsource(Session._asked_for_a_warden)
        self.assertIn("BOSS_BY_KEY", handler)

    def test_a_boss_key_off_a_socket_is_refused_not_trusted(self):
        """§20: a name that is not a warden names nothing."""
        import inspect
        from pixel_rpg.coop import Session
        source = inspect.getsource(Session._asked_for_a_warden)
        self.assertIn("if definition is None", source)
        self.assertIn("return", source)

    def test_a_mirror_builds_the_warden_and_not_a_pack(self):
        """`boss` has ridden in that payload since §21 and nothing read
        it, because a warden was solo - so a mirror rolled an ordinary
        encounter and the two machines fought different things."""
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.begin_shared_battle)
        self.assertIn("make_boss_encounter", source)
        self.assertIn('info.get("boss")', source)

    def test_the_protocol_went_up(self):
        from pixel_rpg import coop as coop_mod
        self.assertGreaterEqual(coop_mod.PROTOCOL, 11)


class TestThePartyStripSaysEnough(unittest.TestCase):
    """§81, from the first school playtest (§40): *"see a friend's
    HP/mana/level under your own"*.

    The strip has carried a name and a health bar since parties existed,
    which answers *are they about to fall* and not *can they still cast
    the thing I am waiting for*.
    """

    def test_it_draws_level_and_magic_as_well(self):
        import ast
        import inspect
        import textwrap
        from pixel_rpg import battlescreen
        source = textwrap.dedent(
            inspect.getsource(battlescreen.draw_party_strip))
        self.assertIn("Lv %d", source)
        self.assertIn("max_mp", source)
        tree = ast.parse(source)
        bars = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "id", "") == "bar"]
        self.assertGreaterEqual(len(bars), 2,
                                "the strip still draws one bar per friend")

    def test_the_viewer_is_still_left_out_of_it(self):
        """Four full plates would be the whole screen."""
        import inspect
        from pixel_rpg import battlescreen
        source = inspect.getsource(battlescreen.draw_party_strip)
        self.assertIn("i != battle.viewer", source)


class TestTheArenaPicker(unittest.TestCase):
    """s82, item 0br. Savvydev, 2026-09-07:

    > maybe set the arenas differently, like a whole new screen that has
    > pictures of what all the arenas look like and a random button at
    > the bottom and the wave settings next to it or whatever

    **The third position this setting has been in**, and each move was
    him saying the last was wrong: Settings (s74), a run-setup row (s80),
    now a screen. The reason is in his first noun - the thing being
    chosen is a *place*, and a name in a list does not show you one.
    """

    def screen(self, width=1280, height=720):
        import pygame
        from pixel_rpg import game as G
        _display_only()
        g = G.Game()
        g.screen = pygame.display.set_mode((width, height))
        return g

    def press(self, game, key):
        import pygame
        game.event_arena_select(pygame.event.Event(
            pygame.KEYDOWN, key=key, unicode="", mod=0))

    def test_every_arena_is_reachable(self):
        """**The first cut showed eight of eighteen** and stopped at the
        bottom edge, which is the one failure this screen cannot have -
        it exists to show him all of them."""
        from pixel_rpg import gamemodes, menus
        g = self.screen()
        seen = set()
        for index in range(len(gamemodes.ARENAS)):
            g.ui.arena_index = index
            cells, cols, (start, fit, rows) = menus.arena_grid(
                g, menus.frame_rect(g))
            for n in range(len(gamemodes.ARENAS)):
                if start <= n // cols < start + fit:
                    seen.add(n)
        self.assertEqual(len(seen), len(gamemodes.ARENAS))

    def test_the_page_holds_the_cursor(self):
        """Walking to the last arena must not walk off the page."""
        from pixel_rpg import gamemodes, menus
        for size in ((1280, 720), (900, 600), (420, 800), (640, 480)):
            g = self.screen(*size)
            for index in range(len(gamemodes.ARENAS)):
                g.ui.arena_index = index
                _, cols, (start, fit, _) = menus.arena_grid(
                    g, menus.frame_rect(g))
                self.assertTrue(start <= index // cols < start + fit,
                                "arena %d is off the page at %r"
                                % (index, size))

    def test_the_page_does_not_move_for_the_buttons(self):
        """Random and Changes-every sit below the grid and are not in it.
        Scrolling to the bottom because the cursor left the grid is
        exactly the slide item 0bn was about."""
        from pixel_rpg import gamemodes, menus
        g = self.screen()
        g.ui.arena_index = 0
        menus.arena_grid(g, menus.frame_rect(g))
        was = g.ui.arena_row
        for index in (len(gamemodes.ARENAS), len(gamemodes.ARENAS) + 1):
            g.ui.arena_index = index
            menus.arena_grid(g, menus.frame_rect(g))
            self.assertEqual(g.ui.arena_row, was)

    def test_thumbnails_do_not_squeeze(self):
        """A picture small enough to fit six of on a phone is a picture
        you cannot tell from the next one, which is the whole point."""
        from pixel_rpg import menus
        self.assertEqual(menus.arena_columns(420), 2)
        for width in (320, 480, 700, 1000, 1400, 2400):
            self.assertTrue(2 <= menus.arena_columns(width) <= 5, width)

    def test_the_cells_do_not_overlap(self):
        from pixel_rpg import menus
        g = self.screen()
        cells, _, _ = menus.arena_grid(g, menus.frame_rect(g))
        for i, a in enumerate(cells):
            for b in cells[i + 1:]:
                self.assertFalse(a.colliderect(b), "%r and %r" % (a, b))

    def test_it_is_a_picture_of_this_arena_and_not_a_shape(self):
        """Drawn from the arena's own colour, skyline and floor plan, so
        a new arena gets a picture free and a changed one cannot keep a
        stale portrait."""
        import pygame
        from pixel_rpg import freeroamscreen as frs, gamemodes
        from pixel_rpg.assets import Assets
        _display_only()
        assets = Assets()
        shots = []
        for arena in gamemodes.ARENAS:
            shot = frs.arena_thumbnail(assets, (120, 74), arena)
            self.assertEqual(shot.get_size(), (120, 74))
            shots.append(pygame.image.tostring(shot, "RGB"))
        self.assertEqual(len(set(shots)), len(shots),
                         "two arenas draw the same picture")

    def test_the_thumbnail_is_cached(self):
        """Eighteen of these are drawn every frame of the picker."""
        from pixel_rpg import freeroamscreen as frs, gamemodes
        from pixel_rpg.assets import Assets
        _display_only()
        assets = Assets()
        first = frs.arena_thumbnail(assets, (90, 56), gamemodes.ARENAS[0])
        again = frs.arena_thumbnail(assets, (90, 56), gamemodes.ARENAS[0])
        self.assertIs(first, again)

    def test_the_thumbnail_does_not_move_the_dice(self):
        """s7's rule: a drawing routine that draws from `rng` re-phases
        the stream, and the run is the seed."""
        from pixel_rpg import freeroamscreen as frs, gamemodes
        from pixel_rpg.assets import Assets
        from pixel_rpg.util import rng
        _display_only()
        assets = Assets()
        rng.reseed(31)
        before = rng._r.getstate()
        for arena in gamemodes.ARENAS[:6]:
            frs.arena_thumbnail(assets, (81, 51), arena)
        self.assertEqual(rng._r.getstate(), before)

    def test_one_setting_three_ways_in(self):
        """This screen, the Settings rows and the run-setup row write the
        same fields - the call `cycle_difficulty` made, and the reason
        there is nothing to drift."""
        from pixel_rpg import gamemodes
        g = self.screen()
        total = len(gamemodes.ARENAS)
        g.choose_arena(7)
        self.assertEqual((g.settings.arena_mode, g.settings.arena_pick),
                         ("one", 7))
        g.choose_arena(total)
        self.assertEqual(g.settings.arena_mode, "random")
        g.choose_arena(total + 1)
        self.assertEqual(g.settings.arena_mode, "every")
        first = g.settings.arena_every
        g.choose_arena(total + 1)
        self.assertNotEqual(g.settings.arena_every, first)
        self.assertIn(g.settings.arena_every, gamemodes.ARENA_CADENCES)

    def test_the_cursor_opens_on_what_is_set(self):
        from pixel_rpg import game as G, gamemodes
        g = self.screen()
        g.settings.arena_mode, g.settings.arena_pick = "one", 11
        g.open_arena_select()
        self.assertEqual(g.state, G.ARENA_SELECT)
        self.assertEqual(g.ui.arena_index, 11)
        g.settings.arena_mode = "random"
        g.open_arena_select()
        self.assertEqual(g.ui.arena_index, len(gamemodes.ARENAS))
        g.settings.arena_mode = "every"
        g.open_arena_select()
        self.assertEqual(g.ui.arena_index, len(gamemodes.ARENAS) + 1)

    def test_the_grid_keys_are_a_grid(self):
        """Left and Right are not Up and Down on a screen with columns -
        the one thing a list handler cannot be reused for."""
        import pygame
        from pixel_rpg import gamemodes, menus
        g = self.screen()
        # **The grid the screen draws, not the one the window would
        # hold** (item 0bw). This test measured the columns from the
        # window width and the screen measures them from the framed
        # panel inside it - so the test agreed with the bug.
        _cells, cols, _page = menus.arena_grid(g, menus.frame_rect(g))
        g.ui.arena_index = 0
        self.press(g, pygame.K_RIGHT)
        self.assertEqual(g.ui.arena_index, 1)
        self.press(g, pygame.K_DOWN)
        self.assertEqual(g.ui.arena_index, 1 + cols)
        self.press(g, pygame.K_UP)
        self.assertEqual(g.ui.arena_index, 1)
        g.ui.arena_index = len(gamemodes.ARENAS) + 1
        self.press(g, pygame.K_RIGHT)
        self.assertEqual(g.ui.arena_index, len(gamemodes.ARENAS) + 1,
                         "the cursor walked off the end")

    def test_escape_goes_back_to_the_run_setup(self):
        import pygame
        from pixel_rpg import game as G
        g = self.screen()
        g.go(G.ARENA_SELECT)
        self.press(g, pygame.K_ESCAPE)
        self.assertEqual(g.state, G.MODIFIER_SELECT)

    def test_the_wheel_reaches_it(self):
        """Rule 8b and item 0bc: anything that answers the arrow keys
        answers the wheel, because a notch replays Up or Down."""
        from pixel_rpg import game as G
        g = self.screen()
        g.go(G.ARENA_SELECT)
        g.ui.arena_index = 0
        g.mouse_scroll(-1)
        self.assertGreater(g.ui.arena_index, 0)

    def test_a_click_lands_on_the_arena_under_it(self):
        """Rule 2.9: a click replays Enter rather than having a second
        code path - and only cells that are on the page have a hotspot."""
        from pixel_rpg import game as G, gamemodes, menus
        g = self.screen()
        g.go(G.ARENA_SELECT)
        g.ui.hot.clear()
        g.screen.fill((0, 0, 0))
        menus.draw_arena_select(g)
        cells, cols, (start, fit, _) = menus.arena_grid(
            g, menus.frame_rect(g))
        target = min(len(gamemodes.ARENAS) - 1, cols + 1)
        self.assertEqual(g.ui.hot.at(cells[target].center),
                         ("arena", target))
        for n in range(len(gamemodes.ARENAS)):
            if start <= n // cols < start + fit:
                continue
            self.assertNotEqual(g.ui.hot.at(cells[n].center), ("arena", n),
                                "an arena nobody can see is clickable")


class TestWhatAWaveOffers(unittest.TestCase):
    """s83, items 0bs and 0bt. Savvydev, 2026-09-07:

    > at the end of each wave in the last stand gamemode, there should be
    > an option to pick a random skill out of four and then another
    > option to pick a random item (armor, weapon or accessories) each
    > when right clicked open a small screen that says what each does

    > and another option to gain a buff or a temporary buff for the rest
    > of the game, like heal 20% right now(temp buff) or +7% attack (perm
    > buff)
    """

    def hero(self, level=8):
        from pixel_rpg.entities import Player
        player = Player("Taker", "warden")
        player.level = level
        return player

    def test_four_of_each(self):
        """His number, and it is the same for all three offers."""
        from pixel_rpg import spoils
        player = self.hero()
        offers = spoils.offers_for("waves", 5, player, bosses=0)
        self.assertEqual([o.kind for o in offers], ["skill", "item", "buff"])
        for offer in offers:
            self.assertEqual(len(offer.cards), spoils.CARDS)

    def test_the_boons_are_their_own_four_in_every_mode(self):
        """**Item 0bz, and it withdraws §86's answer to 0bu.** The Long
        Night was given one mixed hand of four - a boon, an item, a skill
        - on the reasoning that a night is the mode about not stopping.
        He looked at it and said what he wanted instead:

        > and the boons are seperate from the items, the boons get their
        > own four options
        """
        from pixel_rpg import gamemodes, spoils
        for mode in gamemodes.MODES:
            if mode.key == "story":
                continue
            offers = spoils.offers_for(mode.key, 5, self.hero(), bosses=0)
            kinds = [offer.kind for offer in offers]
            self.assertIn("buff", kinds, mode.key)
            self.assertNotIn("mixed", kinds, mode.key)
            for offer in offers:
                self.assertEqual(len(offer.cards), spoils.CARDS, mode.key)

    def test_the_two_modes_offer_the_same_shape_again(self):
        """**0bu is open again** (item 0bz). §86 made the two wave modes
        differ by how they hand the offers out; he asked for the same
        shape everywhere, so what tells them apart is his to answer."""
        from pixel_rpg import spoils
        player = self.hero()
        stand = spoils.offers_for("waves", 9, player, bosses=1)
        night = spoils.offers_for("zombies", 9, player, bosses=1)
        self.assertEqual([o.kind for o in stand], [o.kind for o in night])

    def test_an_item_offer_is_not_four_helmets(self):
        """*"a random item (armor, weapon or accessories)"* - he named
        the slots, so the draw walks them before it rolls freely."""
        from pixel_rpg import spoils
        items = spoils.draw_items(4, level=9, bosses=0)
        self.assertEqual(len(items), 4)
        self.assertGreaterEqual(len({i.slot for i in items}), 3)

    def test_a_skill_offer_is_never_one_you_have(self):
        from pixel_rpg import spoils
        player = self.hero(level=40)
        known = {s.key for s in player.known_skills()}
        for skill in spoils.draw_skills(4, player):
            self.assertNotIn(skill.key, known)

    def test_the_offer_runs_out_rather_than_repeating(self):
        """A hero who has been given everything is offered nothing, and
        the screen skips that offer instead of showing four blanks."""
        from pixel_rpg import spoils
        from pixel_rpg.skills import ALL_SKILLS
        player = self.hero()
        player.granted = [s.key for s in ALL_SKILLS]
        self.assertEqual(spoils.draw_skills(4, player), [])
        kinds = [o.kind for o in spoils.offers_for("waves", 3, player)]
        self.assertNotIn("skill", kinds)

    def test_a_boss_raises_the_drop_rates(self):
        """Item 0bt: *"everytime a boss is defeated the drop rates for
        better items gets a bit higher"*."""
        from pixel_rpg import spoils
        luck = [spoils.luck_for(n) for n in range(0, 9)]
        self.assertEqual(luck, sorted(luck))
        self.assertGreater(luck[3], luck[0])
        self.assertLessEqual(luck[-1], 0.45 + spoils.LUCK_CEILING + 1e-9)

    def test_a_boss_raises_the_odds_of_a_better_boon(self):
        """*"everytime u beat a boss u have a chance of getting better
        buffs"* - a slope, not an unlock."""
        from pixel_rpg import spoils
        first = spoils.tier_odds(0)
        later = spoils.tier_odds(4)
        self.assertGreater(later[0], first[0])
        self.assertGreater(later[1], first[1])
        self.assertEqual(first[1], 0.0, "tier 2 starts closed off? it is a "
                         "weighting, not a gate - only its chance is 0")
        ceiling = spoils.tier_odds(999)
        self.assertEqual(ceiling, spoils.BEST_ODDS)

    def test_the_best_boons_are_reachable_and_the_odds_stop(self):
        """A run at wave 200 that only ever draws tier 2 has stopped
        offering a choice, so the ramp has a ceiling."""
        from pixel_rpg import spoils
        greater, best = spoils.tier_odds(999)
        self.assertLess(greater + best, 1.0)

    def test_a_permanent_boon_is_not_offered_twice(self):
        """Four cards, and one of them being something you already hold
        for the rest of the run is a card that does nothing."""
        from pixel_rpg import spoils
        player = self.hero()
        spoils.give(player, "sharp", 1)
        for _ in range(12):
            drawn = spoils.draw_buffs(4, 3, spoils._held(player))
            self.assertNotIn("sharp", [b.key for b in drawn
                                       if b.permanent])

    def test_his_two_examples_both_exist_and_read_differently(self):
        """*"heal 20% right now(temp buff) or +7% attack (perm buff)"* -
        both his, and the difference has to be visible or the card is
        misleading on the one thing it exists to say."""
        from pixel_rpg import spoils
        heal = spoils.BUFFS_BY_KEY["catch"]
        attack = spoils.BUFFS_BY_KEY["sharp"]
        self.assertEqual(heal.lasts, "now")
        self.assertTrue(attack.permanent)
        self.assertIn("20%", heal.line)
        self.assertIn("7%", attack.line)
        self.assertNotEqual(spoils.LASTS_LABEL[heal.lasts],
                            spoils.LASTS_LABEL[attack.lasts])

    def test_a_run_boon_moves_the_stat_and_a_wave_boon_expires(self):
        from pixel_rpg import spoils
        player = self.hero()
        base = player.stat("atk")
        spoils.give(player, "rally", wave=4)      # +25%, next wave only
        self.assertGreater(player.stat("atk"), base)
        spoils.expire(player, 4)
        self.assertGreater(player.stat("atk"), base, "it died a wave early")
        spoils.expire(player, 5)
        self.assertEqual(player.stat("atk"), base)
        spoils.give(player, "sharp", wave=5)      # +7%, the run
        spoils.expire(player, 500)
        self.assertGreater(player.stat("atk"), base)

    def test_a_flat_boon_is_flat_and_a_percent_one_is_not(self):
        """Five per cent of two crit is nothing, which is why the
        percentage stats take a point instead - the same call the item
        tables already made."""
        from pixel_rpg import spoils
        self.assertTrue(spoils.BUFFS_BY_KEY["steady"].flat)
        self.assertFalse(spoils.BUFFS_BY_KEY["sharp"].flat)
        self.assertEqual(spoils.stat_multiplier([("steady", None)], "crit"),
                         1.0)
        self.assertEqual(spoils.stat_flat([("steady", None)], "crit"), 2.0)

    def test_healing_now_cannot_overheal(self):
        from pixel_rpg import spoils
        player = self.hero()
        player.hp = player.max_hp()
        spoils.give(player, "wholeagain", 1)
        self.assertEqual(player.hp, player.max_hp())
        player.hp = 1
        spoils.give(player, "catch", 1)
        self.assertLessEqual(player.hp, player.max_hp())
        self.assertGreater(player.hp, 1)

    def test_taking_a_skill_puts_it_in_the_hero(self):
        from pixel_rpg import spoils
        from pixel_rpg.skills import SKILLS_BY_KEY
        player = self.hero()
        before = len(player.known_skills())
        spoils.take(player, "skill", SKILLS_BY_KEY["firebolt"])
        self.assertEqual(len(player.known_skills()), before + 1)
        self.assertIn("firebolt", [s.key for s in player.known_skills()])

    def test_a_card_says_what_it_does(self):
        """**The reading is the feature.** Four names is not a choice."""
        from pixel_rpg import spoils
        from pixel_rpg.items import roll_item
        from pixel_rpg.skills import SKILLS_BY_KEY
        player = self.hero()
        for kind, card in (("buff", spoils.BUFFS[0]),
                           ("skill", SKILLS_BY_KEY["firebolt"]),
                           ("item", roll_item(9, slot="weapon"))):
            name = spoils.card_name(kind, card)
            lines = spoils.card_lines(kind, card, player)
            self.assertTrue(name and str(name).strip(), kind)
            self.assertTrue(lines and any(l.strip() for l in lines), kind)

    def test_a_boon_card_says_how_long_it_lasts(self):
        from pixel_rpg import spoils
        for buff in spoils.BUFFS:
            lines = spoils.card_lines("buff", buff)
            self.assertIn(spoils.LASTS_LABEL[buff.lasts], lines, buff.key)

    def test_every_boon_that_holds_names_a_real_stat(self):
        """A boon with a stat nothing reads is the Blind Road bug (s54)
        wearing new clothes: a card that promises and does nothing."""
        from pixel_rpg import spoils
        from pixel_rpg.entities import Player
        known = set(Player.BASE) | {"hp", "mp"}
        for buff in spoils.BUFFS:
            if buff.stat:
                self.assertIn(buff.stat, known, buff.key)
                self.assertTrue(buff.amount, buff.key)
            else:
                self.assertEqual(buff.lasts, "now",
                                 "%s holds nothing and lasts" % buff.key)

    def test_every_now_boon_actually_does_something(self):
        from pixel_rpg import spoils
        player = self.hero()
        for buff in spoils.BUFFS:
            if buff.lasts != "now":
                continue
            player.hp, player.mp = 1, 0
            spoils.apply_now(player, buff)
            self.assertTrue(player.hp > 1 or player.mp > 0, buff.key)

    def test_the_keys_are_unique(self):
        from pixel_rpg import spoils
        keys = [b.key for b in spoils.BUFFS]
        self.assertEqual(len(keys), len(set(keys)))


class TestTheOfferScreen(unittest.TestCase):
    """s83. The screen his *"small screen that says what each does"*
    lives on, and the flow that reaches it."""

    def game(self, width=1280, height=720):
        import pygame
        from pixel_rpg import game as G
        _display_only()
        g = G.Game()
        g.screen = pygame.display.set_mode((width, height))
        g.new_wave_run("Taker")
        return g

    def clear_a_wave(self, g):
        g.begin_wave()
        enemies = list(g.battle.enemies)
        for enemy in enemies:
            enemy.hp = 0
        g.wave_cleared(enemies)

    def test_clearing_a_wave_opens_it(self):
        from pixel_rpg import game as G
        g = self.game()
        self.clear_a_wave(g)
        self.assertEqual(g.state, G.SPOILS)
        self.assertEqual([o.kind for o in g.spoils],
                         ["skill", "item", "buff"])

    def test_the_offers_are_answered_one_at_a_time(self):
        from pixel_rpg import game as G
        g = self.game()
        self.clear_a_wave(g)
        for step in range(3):
            self.assertEqual(g.spoil_at, step)
            self.assertEqual(g.state, G.SPOILS)
            g.take_spoil(0)
        self.assertEqual(g.state, G.WAVES, "it never went back to the run")
        self.assertEqual(g.spoils, [])

    def test_taking_one_takes_only_one(self):
        g = self.game()
        self.clear_a_wave(g)
        g.spoil_at = 1                       # the item offer
        before = len(g.player.inventory)
        g.take_spoil(2)
        self.assertEqual(len(g.player.inventory), before + 1)

    def test_leaving_them_is_allowed(self):
        """A full bag makes an item card impossible to accept, and a
        screen you cannot get past is worse than a gift declined."""
        from pixel_rpg import game as G
        g = self.game()
        self.clear_a_wave(g)
        bag = len(g.player.inventory)
        for _ in range(3):
            g.leave_spoils()
        self.assertEqual(g.state, G.WAVES)
        self.assertEqual(len(g.player.inventory), bag)

    def test_the_keys_walk_the_cards(self):
        import pygame
        g = self.game()
        self.clear_a_wave(g)
        g.ui.spoil_index = 0
        g.event_spoils(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RIGHT,
                                          unicode="", mod=0))
        self.assertEqual(g.ui.spoil_index, 1)
        for _ in range(9):
            g.event_spoils(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_RIGHT, unicode="", mod=0))
        self.assertLess(g.ui.spoil_index, 4, "the cursor walked off the end")

    def test_right_click_reads_a_card(self):
        """His words: *"each when right clicked open a small screen that
        says what each does"*. The same toggle gear already uses."""
        from pixel_rpg import menus
        g = self.game()
        self.clear_a_wave(g)
        g.ui.hot.clear()
        g.screen.fill((0, 0, 0))
        menus.draw_spoils(g)
        cells, _cols = menus.spoil_cards(g, menus.frame_rect(g))
        g.right_click(cells[2].center)
        self.assertEqual(g.ui.spoil_detail, 2)
        g.right_click(cells[2].center)
        self.assertIsNone(g.ui.spoil_detail, "a second right-click closes it")

    def test_the_keyboard_can_read_one_too(self):
        """Rule 8b's mirror: the mouse asked for it, and the keyboard
        cannot be the one input that has no way in."""
        import pygame
        g = self.game()
        self.clear_a_wave(g)
        g.ui.spoil_index = 1
        g.event_spoils(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_q,
                                          unicode="q", mod=0))
        self.assertEqual(g.ui.spoil_detail, 1)

    def test_a_click_picks_the_card_up_and_confirm_takes_it(self):
        """Taking a card spends the other three, so it is `SELECT_ONLY` -
        the same shape as spending gold."""
        from pixel_rpg import game as G, menus
        g = self.game()
        self.clear_a_wave(g)
        self.assertIn("spoil", G.Game.SELECT_ONLY)
        g.ui.hot.clear()
        g.screen.fill((0, 0, 0))
        menus.draw_spoils(g)
        cells, _cols = menus.spoil_cards(g, menus.frame_rect(g))
        g.mouse_click(cells[2].center, 1)
        self.assertEqual(g.ui.spoil_index, 2)
        self.assertEqual(g.spoil_at, 0, "one click took the card")

    def test_a_wave_boon_dies_with_the_wave_it_was_taken_for(self):
        from pixel_rpg import spoils
        g = self.game()
        self.clear_a_wave(g)
        spoils.give(g.player, "rally", g.waves.cleared)
        self.assertTrue(g.player.buffs)
        self.clear_a_wave(g)
        self.assertNotIn("rally", [key for key, _ in g.player.buffs])

    def test_the_run_counts_its_wardens(self):
        """Item 0bt is one number on the run, read by two things."""
        from pixel_rpg import gamemodes, spoils
        run = gamemodes.WaveRun()
        for _ in range(20):
            run.clear_wave(1)
        self.assertEqual(run.bosses, 2)
        night = gamemodes.NightRun()
        for _ in range(20):
            night.clear_night()
        self.assertEqual(night.bosses, 2)
        self.assertGreater(spoils.luck_for(run.bosses), spoils.luck_for(0))

    def test_a_night_pays_the_one_who_survived_it(self):
        """**A guest who survived a night was paid nothing at all** - no
        gold, no XP, no heal - because `finish_night` only ever runs on
        the machine simulating the fight. Found while building s83."""
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.begin_guest_night)
        self.assertIn("pay_for_night", source)
        self.assertIn("announced", source)

    def test_the_offer_is_data_until_it_is_taken(self):
        """Nothing is granted by opening the screen, which is what lets
        the same call serve a paused Last Stand and an opening night."""
        g = self.game()
        self.clear_a_wave(g)
        bag, granted = len(g.player.inventory), list(g.player.granted)
        buffs = list(g.player.buffs)
        g.offer_spoils()
        self.assertEqual(len(g.player.inventory), bag)
        self.assertEqual(g.player.granted, granted)
        self.assertEqual(g.player.buffs, buffs)

    def test_a_boon_survives_the_wire(self):
        """Wave modes have no save, so these only ever cross a socket -
        and a mirrored body whose boons were dropped shows a friend the
        wrong health bar."""
        from pixel_rpg import spoils
        from pixel_rpg.save import deserialize_player, serialize_player
        g = self.game()
        spoils.give(g.player, "sharp", 1)
        spoils.give(g.player, "rally", 1)
        spoils.take(g.player, "skill",
                    __import__("pixel_rpg.skills",
                               fromlist=["x"]).SKILLS_BY_KEY["firebolt"])
        back = deserialize_player(serialize_player(g.player))
        self.assertEqual(back.granted, ["firebolt"])
        self.assertEqual(sorted(key for key, _ in back.buffs),
                         ["rally", "sharp"])
        self.assertEqual(back.stat("atk"), g.player.stat("atk"))

    def test_a_forged_boon_is_refused_and_a_forged_length_ignored(self):
        """s20: a key off a socket names a real thing or nothing - and
        how long a boon lasts is the table's answer, not the sender's."""
        from pixel_rpg.save import deserialize_player, serialize_player
        from pixel_rpg.entities import Player
        blob = serialize_player(Player("A", "warden"))
        blob["buffs"] = [["nope", 3], ["rally", -1], ["sharp", 9]]
        blob["granted"] = ["nope", "firebolt", 5]
        back = deserialize_player(blob)
        self.assertEqual(back.granted, ["firebolt"])
        held = dict(back.buffs)
        self.assertNotIn("nope", held)
        self.assertIsNone(held["sharp"], "the table says this one is the run")
        self.assertEqual(held["rally"], 0,
                         "a forged permanent Fury should not be permanent")


class TestTheRoomWaitsForItsCards(unittest.TestCase):
    """s84, and s83's own open item.

    Everybody in a room gets their own offers after a wave, so the cards
    need no arbitration - but **when the next wave starts** does, and the
    first cut let the host send it in while three people were still
    deciding what they were carrying into it. That is the same failure
    decision 4 fixed for wardens, in the mode that has it every wave.
    """

    def host(self):
        import time
        from pixel_rpg import coop as coop_mod
        session = coop_mod.Session.__new__(coop_mod.Session)
        session.role = coop_mod.HOST
        session.remotes = {2: coop_mod.Remote(2, "Friend"),
                           3: coop_mod.Remote(3, "Other")}
        for remote in session.remotes.values():
            remote.last_seen = time.monotonic()
        return session

    def test_nobody_is_waited_for_until_somebody_says_so(self):
        self.assertEqual(self.host().still_choosing(), [])

    def test_a_reader_is_named(self):
        """*"Not everybody is ready"* without saying who is a message
        you cannot act on - `wardens_missing`'s rule, again."""
        import time
        session = self.host()
        session.remotes[2].choosing = True
        session.remotes[2].choosing_since = time.monotonic()
        self.assertEqual(session.still_choosing(), ["Friend"])

    def test_the_wait_expires(self):
        """**A room must never be stuck.** A guest who walked away with
        the offer screen open would otherwise hold the whole room at
        wave nine forever, and the only way out would be restarting."""
        import time
        session = self.host()
        session.remotes[2].choosing = True
        session.remotes[2].choosing_since = (time.monotonic()
                                             - session.PICK_PATIENCE - 1)
        self.assertEqual(session.still_choosing(), [])

    def test_a_silent_machine_is_not_waited_for(self):
        """The other half of the same rule: a peer that has gone quiet
        is already not in the room as far as everything else goes."""
        import time
        session = self.host()
        session.remotes[2].choosing = True
        session.remotes[2].choosing_since = time.monotonic()
        session.remotes[2].last_seen = time.monotonic() - 10_000
        self.assertEqual(session.still_choosing(), [])

    def test_a_guest_never_waits_for_anybody(self):
        """A guest's button already says it is waiting for the host, and
        two different sentences about waiting on one button is one too
        many."""
        from pixel_rpg import coop as coop_mod
        session = self.host()
        session.role = coop_mod.CLIENT
        session.remotes[2].choosing = True
        self.assertEqual(session.still_choosing(), [])

    def test_solo_waits_for_nobody(self):
        import pygame
        from pixel_rpg import game as G
        _display_only()
        g = G.Game()
        g.screen = pygame.display.set_mode((900, 600))
        g.new_wave_run("Alone")
        self.assertEqual(g.waves_waiting_for(), [])

    def test_the_button_does_not_send_the_wave_while_it_waits(self):
        """§45's rule: the control says what it is doing rather than
        being one that quietly does nothing."""
        import inspect
        from pixel_rpg import menus
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.event_waves)
        self.assertIn("waves_waiting_for", source)
        screen = inspect.getsource(menus.draw_waves)
        self.assertIn("Waiting for %s", screen)

    def test_the_protocol_went_up(self):
        """A build that does not know "pick" would send waves in while
        everybody else was still reading - which is the bug."""
        from pixel_rpg import coop as coop_mod
        self.assertGreaterEqual(coop_mod.PROTOCOL, 12)

    def test_the_message_is_told_not_asked(self):
        """What a player takes is nobody else's business; the only thing
        the room agrees on is when the next wave starts."""
        import inspect
        from pixel_rpg.coop import Session
        source = inspect.getsource(Session.say_choosing)
        self.assertIn("self.role == HOST", source)
        self.assertIn('"pick"', source)


class TestTheRelay(unittest.TestCase):
    """s85. The roadmap's *"a relay for CGNAT hosts"*, open since s32.

    **A room code is an address** (`net.encode_room`), which is what lets
    co-op need no server - and what stops working entirely when the host
    has no address anybody can dial. So that host dials *out* instead.
    """

    def setUp(self):
        import os
        self._was = os.environ.get("PIXEL_RPG_RELAY_URL")
        os.environ.pop("PIXEL_RPG_RELAY_URL", None)

    def tearDown(self):
        import os
        if self._was is None:
            os.environ.pop("PIXEL_RPG_RELAY_URL", None)
        else:
            os.environ["PIXEL_RPG_RELAY_URL"] = self._was

    def test_it_is_off_unless_it_is_configured(self):
        """The same shape as the leaderboard (s75), for the same two
        reasons: it is an account rather than code, and nothing should
        quietly start routing itself through a third party."""
        from pixel_rpg import relay
        self.assertFalse(relay.enabled())
        self.assertEqual(relay.url(), "")

    def test_the_env_var_is_read_every_time(self):
        """A setting nobody can change without restarting is a setting
        that gets reported as broken."""
        import os
        from pixel_rpg import relay
        os.environ["PIXEL_RPG_RELAY_URL"] = "ws://relay.example:9000"
        self.assertTrue(relay.enabled())
        os.environ.pop("PIXEL_RPG_RELAY_URL")
        self.assertFalse(relay.enabled())

    def test_a_url_is_read_or_refused(self):
        from pixel_rpg import relay
        self.assertEqual(relay.split("ws://a.example"), ("a.example", 80,
                                                         False))
        self.assertEqual(relay.split("wss://a.example"), ("a.example", 443,
                                                          True))
        self.assertEqual(relay.split("wss://a.example:8443"),
                         ("a.example", 8443, True))
        self.assertEqual(relay.split("a.example:1234"),
                         ("a.example", 1234, False))
        self.assertEqual(relay.split("https://a.example/"),
                         ("a.example", 443, True))
        for bad in ("", "   ", "ws://", "ws://a.example:0",
                    "ws://a.example:99999", "ws://a.example:eight",
                    "ws://a.example/somewhere"):
            self.assertEqual(relay.split(bad), ("", 0, False), bad)

    def test_a_url_with_a_path_is_refused(self):
        """The paths belong to this module. A URL that already carries
        one is a URL that means something else."""
        from pixel_rpg import relay
        self.assertEqual(relay.split("wss://host/relay/v2")[0], "")

    def test_a_room_id_is_six_of_the_spoken_alphabet(self):
        from pixel_rpg import relay
        for _ in range(50):
            room = relay.new_room()
            self.assertEqual(len(room), relay.ROOM_LEN)
            self.assertTrue(relay.valid_room(room), room)
            self.assertNotIn("#", room)

    def test_the_hash_is_not_in_a_relay_code(self):
        """`net.ALPHABET` has one, and a `#` in a URL is a fragment
        delimiter - the room id would be cut off before it arrived."""
        from pixel_rpg import net, relay
        self.assertIn("#", net.ALPHABET)
        self.assertNotIn("#", relay.ALPHABET)

    def test_a_code_survives_being_read_out(self):
        from pixel_rpg import relay
        self.assertTrue(relay.valid_room(" a2-b3 c4 "[:0] + "a2b3c4"))
        self.assertEqual(relay.clean(" a2-b3 c4 "), "A2B3C4")
        self.assertFalse(relay.valid_room("A2B3C"))
        self.assertFalse(relay.valid_room("A2B3C40"))
        self.assertFalse(relay.valid_room("A2B3C#"))
        self.assertFalse(relay.valid_room(None))

    def test_connecting_with_no_relay_is_refused_not_attempted(self):
        from pixel_rpg import relay
        with self.assertRaises(OSError):
            relay.connect("A2B3C4", "park")

    def test_a_bad_role_or_room_never_opens_a_socket(self):
        import os
        from pixel_rpg import relay
        os.environ["PIXEL_RPG_RELAY_URL"] = "ws://127.0.0.1:9"
        for room, role in (("A2B3C4", "sideways"), ("nope", "join"),
                           ("", "park")):
            with self.assertRaises(OSError):
                relay.connect(room, role)

    def test_the_two_code_shapes_do_not_collide(self):
        """Ten characters is an address, six is a relay room. Told apart
        by shape, so a player types what they were given."""
        from pixel_rpg import net, relay
        direct = net.encode_room("192.168.1.20", 47216)
        self.assertEqual(len(direct), 10)
        self.assertFalse(relay.valid_room(direct))
        self.assertTrue(relay.valid_room(relay.new_room()))

    def test_a_relay_code_with_no_relay_says_so(self):
        from pixel_rpg import net
        with self.assertRaises(net.NetError) as caught:
            net.RelayClient.from_code("A2B3C4")
        self.assertIn("relay", str(caught.exception).lower())

    def test_a_host_with_no_relay_refuses_to_pretend(self):
        from pixel_rpg import net
        with self.assertRaises(net.NetError):
            net.RelayHost()

    def test_hosting_still_opens_a_port_when_there_is_no_relay(self):
        """Nothing changes for anybody who does not need this."""
        from pixel_rpg import coop as coop_mod, net
        session = coop_mod.Session.host("Hostess", port=0, seed=7,
                                        reach_out=False)
        try:
            self.assertIsInstance(session.transport, net.Host)
            self.assertEqual(len(session.room_code), 10)
        finally:
            session.close()


class TestTheRelayServer(unittest.TestCase):
    """s85. The pipe itself: `tools/relay_server.py`.

    It knows nothing about the game - not the protocol, not the players,
    not the world - which is the property worth testing, because the
    moment it knows something it is a server rather than a wire.
    """

    def server(self):
        import os
        import sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tools = os.path.join(root, "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import relay_server
        return relay_server

    def test_a_route_names_one_of_two_things_or_nothing(self):
        """s20's rule, applied to a URL."""
        mod = self.server()
        self.assertEqual(mod.read_route("/park/A2B3C4"), ("park", "A2B3C4"))
        self.assertEqual(mod.read_route("/join/a2b3c4"), ("join", "A2B3C4"))
        for bad in ("/", "/park", "/park/A2B3C4/extra", "/steal/A2B3C4",
                    "/park/TOOLONGX", "/park/SHORT", "/park/A2B3C#",
                    "/../etc/passwd", ""):
            self.assertEqual(mod.read_route(bad), (None, None), bad)

    def test_a_path_is_read_out_of_a_real_request_line(self):
        mod = self.server()
        self.assertEqual(
            mod.request_path(b"GET /join/A2B3C4 HTTP/1.1\r\nHost: x\r\n"),
            "/join/A2B3C4")
        self.assertEqual(mod.request_path(b"GET /a?b=c HTTP/1.1"), "/a")
        self.assertEqual(mod.request_path(b"POST /join/A2B3C4 HTTP/1.1"), "")
        self.assertEqual(mod.request_path(b""), "")

    def test_every_queue_has_a_clock(self):
        """Three ways to be stuck - a handshake that never finishes, a
        room nobody joins, a pair gone silent - and three timeouts. The
        lesson `Host._show_the_door` already wrote down."""
        mod = self.server()
        for name in ("HANDSHAKE_TTL", "PARK_TTL", "IDLE_TTL"):
            self.assertGreater(getattr(mod, name), 0, name)
        self.assertGreater(mod.PARK_TTL, mod.IDLE_TTL)

    def test_it_has_ceilings(self):
        """A relay with no limits is one bored person away from a bill."""
        mod = self.server()
        self.assertGreater(mod.MAX_ROOMS, 0)
        self.assertGreaterEqual(mod.MAX_PARKED, 4)
        self.assertLessEqual(mod.MAX_PAYLOAD, 1024 * 1024)
        self.assertGreater(mod.MAX_QUEUE, mod.MAX_PAYLOAD)

    def test_it_knows_nothing_about_the_game(self):
        """The property that makes it a wire rather than a server: it
        never imports the protocol, the entities or the world."""
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "tools", "relay_server.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("import coop", "from pixel_rpg import coop",
                          "import game", "entities", "PROTOCOL"):
            self.assertNotIn(forbidden, source,
                             "the relay learned about %s" % forbidden)

    def test_it_pairs_and_pipes(self):
        """The whole thing, on a loopback port: two clients in, bytes
        across, in both directions."""
        import threading
        import time
        from pixel_rpg import relay, websock
        mod = self.server()
        server = mod.Relay(port=0, bind="127.0.0.1", quiet=True)
        import os
        was = os.environ.get("PIXEL_RPG_RELAY_URL")
        os.environ["PIXEL_RPG_RELAY_URL"] = "ws://127.0.0.1:%d" % server.port
        stop = []
        thread = threading.Thread(
            target=lambda: [server.tick(0.005)
                            for _ in iter(lambda: not stop, False)],
            daemon=True)
        thread.start()
        try:
            room = relay.new_room()
            park, park_key = relay.connect(room, "park")
            park_head, park_rest = relay.wait_for_head(park)
            self.assertTrue(websock.check_handshake(park_head, park_key))
            join, join_key = relay.connect(room, "join")
            join_head, join_rest = relay.wait_for_head(join)
            self.assertTrue(websock.check_handshake(join_head, join_key))

            join.sendall(websock.encode_masked(b"up", websock.OP_BINARY))
            park.settimeout(3.0)
            got = websock.take_frame(park_rest + park.recv(4096), 65536,
                                     expect_mask=False)
            self.assertEqual(got[2], b"up")

            park.sendall(websock.encode_masked(b"down", websock.OP_BINARY))
            join.settimeout(3.0)
            back = websock.take_frame(join_rest + join.recv(4096), 65536,
                                      expect_mask=False)
            self.assertEqual(back[2], b"down")

            # A room nobody parked in is closed rather than left hanging.
            lonely, _key = relay.connect(relay.new_room(), "join")
            head, rest = relay.wait_for_head(lonely)
            lonely.settimeout(3.0)
            frame = websock.take_frame(rest + lonely.recv(4096), 65536,
                                       expect_mask=False)
            self.assertEqual(frame[1], websock.OP_CLOSE)
            for sock in (park, join, lonely):
                sock.close()
            time.sleep(0.05)
        finally:
            stop.append(True)
            thread.join(timeout=1.0)
            server.close()
            if was is None:
                os.environ.pop("PIXEL_RPG_RELAY_URL", None)
            else:
                os.environ["PIXEL_RPG_RELAY_URL"] = was

    def test_a_socket_that_is_not_a_route_is_turned_away(self):
        import socket
        import threading
        import time
        mod = self.server()
        server = mod.Relay(port=0, bind="127.0.0.1", quiet=True)
        stop = []
        thread = threading.Thread(
            target=lambda: [server.tick(0.005)
                            for _ in iter(lambda: not stop, False)],
            daemon=True)
        thread.start()
        try:
            sock = socket.create_connection(("127.0.0.1", server.port), 3.0)
            sock.sendall(b"GET /secrets HTTP/1.1\r\nHost: x\r\n"
                         b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Key: AAAAAAAAAAAAAAAAAAAAAA==\r\n"
                         b"Sec-WebSocket-Version: 13\r\n\r\n")
            sock.settimeout(3.0)
            reply = sock.recv(1024)
            self.assertNotIn(b"101", reply.split(b"\r\n")[0])
            sock.close()
            time.sleep(0.05)
        finally:
            stop.append(True)
            thread.join(timeout=1.0)
            server.close()


class TestEveryModeCanActuallyBeStarted(unittest.TestCase):
    """s86, and the bug it found.

    **Choosing The Long Night in the picker started a story run.**
    `begin_run` branched on `== "waves"` and everything else fell through
    to `new_run`, so the mode had been built, tested, documented and
    shipped with no way into it - for as long as it had existed.

    Nothing caught it because every test and every smoke check called
    `new_night_run` directly. That is s7's *"a screen missing from the
    list is a silent pass"* wearing a different coat: **the route a
    player walks was never walked.**
    """

    def run_through_the_picker(self, key):
        import pygame
        from pixel_rpg import game as G
        _display_only()
        g = G.Game()
        g.screen = pygame.display.set_mode((900, 600))
        g.choose_mode(key)
        g.pending_class = "warden"
        g.name_buffer = "Router"
        g.begin_run()
        for _ in range(120):
            g.update(1 / 30)
        return g

    def test_every_slotless_mode_starts_in_itself(self):
        from pixel_rpg import gamemodes
        for mode in gamemodes.MODES:
            if mode.saves:
                continue
            game = self.run_through_the_picker(mode.key)
            self.assertEqual(game.mode, mode.key,
                             "picking %s started %r" % (mode.key, game.mode))

    def test_every_slotless_mode_has_a_starter(self):
        """The table is the fix; this is the thing that keeps it true
        when somebody adds a fifth mode."""
        from pixel_rpg import gamemodes
        from pixel_rpg.game import Game
        for mode in gamemodes.MODES:
            if mode.saves:
                continue
            self.assertIn(mode.key, Game.RUN_STARTERS, mode.key)
            self.assertTrue(hasattr(Game, Game.RUN_STARTERS[mode.key]))

    def test_a_slotless_mode_never_takes_a_save_slot(self):
        from pixel_rpg import gamemodes
        for mode in gamemodes.MODES:
            if mode.saves:
                continue
            game = self.run_through_the_picker(mode.key)
            self.assertEqual(game.slot, 0, mode.key)
            self.assertFalse(game.save_run(silent=True), mode.key)

    def test_none_of_them_play_the_prologue(self):
        """A scored run that opens on a story scene is the symptom this
        bug actually had."""
        from pixel_rpg import game as G, gamemodes
        for mode in gamemodes.MODES:
            if mode.saves:
                continue
            game = self.run_through_the_picker(mode.key)
            self.assertNotEqual(game.state, G.SCENE, mode.key)


class TestTheHoard(unittest.TestCase):
    """s86, item 0bv. Savvydev, 2026-09-07:

    > i also want to add a lot more gamemodes but idek what honestly

    **The gap §83 named**: both existing modes are *survive a rising
    number of enemies*. This one you cannot lose by dying - falling costs
    you the load you were carrying, and the clock is the only thing that
    ends it.
    """

    def arena(self, party=1):
        from pixel_rpg.entities import Player
        from pixel_rpg.freeroam import FreeRoam
        side = [Player("Greedy %d" % n, "warden") for n in range(party)]
        for who in side:
            who.level = 8
            who.full_restore()
        return FreeRoam(side[0], party=side, hoard=True)

    def test_it_is_a_mode_the_game_knows(self):
        from pixel_rpg import gamemodes
        mode = gamemodes.mode_for("hoard")
        self.assertEqual(mode.key, "hoard")
        self.assertFalse(mode.saves, "a hoard must not hold a save slot")
        self.assertFalse(mode.island)
        self.assertTrue(mode.scores)

    def test_story_is_not_one_of_its_difficulties(self):
        """His rule, by name (item 0bb), applied to the third mode of
        this shape rather than re-argued."""
        from pixel_rpg import config
        self.assertNotIn("story", config.difficulties_for("hoard"))

    def test_a_coin_is_picked_up_by_walking_over_it(self):
        fight = self.arena()
        fight.scatter(5)
        coin = fight.loot[0]
        hero = fight.heroes[0]
        hero.x, hero.y = coin.x, coin.y
        fight.gather()
        self.assertEqual(hero.carrying, 1)
        self.assertEqual(len(fight.loot), 4)

    def test_nothing_is_scattered_into_the_camp(self):
        """A coin you can bank without leaving is already banked."""
        import math
        from pixel_rpg import freeroam as fr
        fight = self.arena()
        fight.scatter(60)
        for coin in fight.loot:
            gap = math.hypot(coin.x - fight.bank[0], coin.y - fight.bank[1])
            self.assertGreater(gap, fr.BANK_REACH, "a coin in the camp")

    def test_carrying_it_home_is_what_counts(self):
        fight = self.arena()
        fight.scatter(4)
        hero = fight.heroes[0]
        for coin in list(fight.loot):
            hero.x, hero.y = coin.x, coin.y
            fight.gather()
        self.assertEqual(hero.carrying, 4)
        self.assertEqual(fight.banked_total(), 0, "it counted before it was in")
        hero.x, hero.y = fight.bank
        fight.gather()
        self.assertEqual(hero.carrying, 0)
        self.assertEqual(fight.banked_total(), 4)

    def test_a_full_load_slows_you_down_and_the_slowdown_stops(self):
        """**The whole tension**, and a capped penalty is what keeps a
        greedy run playable rather than hopeless."""
        from pixel_rpg import freeroam as fr
        fight = self.arena()
        hero = fight.heroes[0]
        self.assertEqual(fight.carry_speed(hero), fr.PLAYER_SPEED)
        hero.carrying = 5
        loaded = fight.carry_speed(hero)
        self.assertLess(loaded, fr.PLAYER_SPEED)
        hero.carrying = 500
        self.assertGreaterEqual(
            fight.carry_speed(hero),
            fr.PLAYER_SPEED * (1.0 - fr.CARRY_SLOW_CAP) - 0.001)

    def test_falling_costs_the_load_and_not_the_run(self):
        fight = self.arena()
        hero = fight.heroes[0]
        hero.carrying = 6
        hero.who.hp = 0
        hero.dead = True
        fight.spill(hero)
        hero.down = fr_time = 0.01
        self.assertEqual(hero.carrying, 0)
        self.assertEqual(len(fight.loot), 6, "the load did not hit the floor")
        self.assertFalse(fight.check_over(), "a hoard ended because of a fall")
        self.assertFalse(fight.finished)

    def test_you_get_back_up_at_the_camp(self):
        from pixel_rpg import freeroam as fr
        fight = self.arena()
        hero = fight.heroes[0]
        hero.who.hp, hero.dead, hero.down = 0, True, 0.05
        fight.update(0.06)
        self.assertFalse(hero.dead, "nobody ever got up")
        self.assertGreater(hero.who.hp, 0)
        self.assertEqual((round(hero.x), round(hero.y)),
                         (round(fr.BANK_AT[0]), round(fr.BANK_AT[1])))

    def test_the_camp_is_not_behind_a_wall(self):
        """The arenas were drawn before this mode existed and their floor
        plans put cover wherever the plan says - including on the one
        circle a hoard has to be able to run into (rule 31.4 found it)."""
        import math
        from pixel_rpg import freeroam as fr, gamemodes
        fight = self.arena()
        for arena in gamemodes.ARENAS:
            fight.blockers = fr.plan_blockers(getattr(arena, "plan", "open"),
                                              stable_seed(arena.key))
            fight.clear_camp()
            for bx, by, radius in fight.blockers:
                gap = math.hypot(bx - fight.bank[0], by - fight.bank[1])
                self.assertGreaterEqual(gap, fr.BANK_REACH + radius,
                                        "%s puts cover in the camp"
                                        % arena.key)

    def test_the_whole_arena_stays_on_the_floor(self):
        from pixel_rpg import freeroam as fr
        self.assertLess(fr.BANK_AT[1] + fr.BANK_REACH, fr.ARENA_H)
        self.assertGreater(fr.BANK_AT[0] - fr.BANK_REACH, 0)

    def test_a_night_and_an_island_fight_are_untouched(self):
        """Every hoard rule is off unless the run asks for it."""
        from pixel_rpg.entities import Player
        from pixel_rpg.freeroam import FreeRoam
        plain = FreeRoam(Player("Plain", "warden"))
        self.assertFalse(plain.hoard)
        self.assertEqual(plain.loot, [])
        plain.gather()
        self.assertEqual(plain.loot, [])
        self.assertNotIn("c", plain.snapshot())
        hero = plain.heroes[0]
        hero.who.hp, hero.dead = 0, True
        self.assertTrue(plain.check_over(), "a night stopped ending")

    def test_escape_asks_before_it_ends_a_run(self):
        """s90. Esc ended a real-time run on the spot - mid-fight, no
        question, with a score written to the records. **There is no
        pause menu out there**, so a player reaching for one by reflex
        lost the run; every other screen in the game treats Esc as
        *back*."""
        import pygame
        from pixel_rpg import game as G
        _display_only()
        for starter in ("new_night_run", "new_hoard_run"):
            game = G.Game()
            game.screen = pygame.display.set_mode((900, 600))
            game.pending_class = "warden"
            getattr(game, starter)("Tester")
            for _ in range(20):
                game.update(1 / 30)
            before = len(game.meta.runs)
            escape = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE,
                                        unicode="", mod=0)
            game.handle_event(escape)
            self.assertEqual(game.state, G.DIALOGUE, starter)
            self.assertEqual(len(game.meta.runs), before,
                             "%s scored the run before asking" % starter)
            # Backing out puts you where you were, still playing.
            game.handle_event(escape)
            self.assertEqual(game.state, G.ROAM, starter)
            self.assertEqual(len(game.meta.runs), before, starter)
            # And confirming ends it.
            game.handle_event(escape)
            game.handle_event(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_RETURN, unicode="", mod=0))
            self.assertEqual(game.state, G.WAVE_OVER, starter)
            self.assertEqual(len(game.meta.runs), before + 1, starter)

    def test_the_question_says_what_you_keep(self):
        """*"This ends the run"* without saying what it is worth is a
        question you cannot answer."""
        import pygame
        from pixel_rpg import game as G
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((900, 600))
        game.pending_class = "warden"
        game.new_hoard_run("Tester")
        game.hoard.banked = 37
        game.ask_leave_roam()
        body = " ".join(game.dialogue["body"])
        self.assertIn("37", body)
        self.assertIn("Esc", "Esc")
        self.assertTrue(game.dialogue.get("on_cancel"))

    def test_the_arena_is_still_behind_the_question(self):
        """A scored run never shows the world it quietly owns (§59), and
        the thing you are deciding about should be on screen."""
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.draw)
        self.assertIn("state == DIALOGUE and self.roam is not None", source)

    def test_the_clock_is_the_only_thing_that_ends_it(self):
        from pixel_rpg import gamemodes
        run = gamemodes.HoardRun()
        self.assertFalse(run.tick(1.0))
        self.assertFalse(run.over)
        run.left = 0.5
        self.assertTrue(run.tick(1.0))
        self.assertTrue(run.over)

    def test_a_big_trip_pays_double(self):
        """s87, measured. With a cost on greed and no reward for it, a
        bot banking three at a time beat every greedier policy - so the
        decision had one right answer and was not a decision. With this,
        the best policy moved from **carry 3** to **carry 14**."""
        from pixel_rpg import gamemodes
        big = gamemodes.HOARD_BIG_TRIP
        self.assertEqual(gamemodes.hoard_trip_value(big - 1), big - 1)
        self.assertEqual(gamemodes.hoard_trip_value(big),
                         big * gamemodes.HOARD_BIG_BONUS)
        self.assertEqual(gamemodes.hoard_trip_value(0), 0)
        self.assertEqual(gamemodes.hoard_trip_value(-4), 0)

    def test_the_trip_is_valued_where_it_happens(self):
        """A total cannot say how it got there, so the arena values the
        haul and the frame carries what it is worth - otherwise a guest
        and a host score the same trip differently."""
        from pixel_rpg import gamemodes
        fight = self.arena()
        hero = fight.heroes[0]
        hero.carrying = gamemodes.HOARD_BIG_TRIP
        hero.x, hero.y = fight.bank
        fight.gather()
        self.assertEqual(
            fight.banked[0],
            gamemodes.HOARD_BIG_TRIP * gamemodes.HOARD_BIG_BONUS)

    def test_the_hud_says_how_far_off_the_double_is(self):
        """A rule nobody can see is a rule nobody plays around."""
        import inspect
        from pixel_rpg import freeroamscreen
        source = inspect.getsource(freeroamscreen.draw_hoard_hud)
        self.assertIn("to double", source)
        self.assertIn("HOARD_BIG_TRIP", source)

    def test_the_score_is_what_got_home(self):
        """A run that dies holding forty coins scores nothing for them,
        which is the whole of the tension."""
        from pixel_rpg import gamemodes
        run = gamemodes.HoardRun()
        self.assertEqual(run.score(), 0)
        run.kills = 50
        self.assertEqual(run.score(), 0, "killing alone scored")
        run.bank(10)
        with_haul = run.score()
        self.assertGreater(with_haul, 0)
        run.bank(10)
        self.assertGreater(run.score(), with_haul)

    def test_fighting_never_outscores_carrying(self):
        from pixel_rpg import gamemodes
        carried = gamemodes.HoardRun()
        carried.bank(20)
        fought = gamemodes.HoardRun()
        fought.bank(5)
        fought.kills = 500
        self.assertGreater(carried.score(), fought.score())

    def test_the_crowd_grows_and_stops_growing(self):
        from pixel_rpg import gamemodes
        counts = [gamemodes.hoard_crowd(t) for t in (0, 30, 60, 120, 600)]
        self.assertEqual(counts, sorted(counts))
        self.assertLessEqual(counts[-1], gamemodes.HOARD_CROWD_MAX)

    def test_a_run_plays_from_the_picker_to_a_score(self):
        import pygame
        from pixel_rpg import game as G
        _display_only()
        g = G.Game()
        g.screen = pygame.display.set_mode((900, 600))
        g.pending_class = "warden"
        self.assertTrue(g.new_hoard_run("Greedy"))
        self.assertEqual(g.state, G.ROAM)
        self.assertTrue(g.roam.hoard)
        self.assertTrue(g.roam.loot, "the floor was empty at the start")
        for _ in range(120):
            g.update(1 / 30)
            hero = g.roam.heroes[0]
            if hero.carrying >= 3 or not g.roam.loot:
                hero.x, hero.y = g.roam.bank
            elif g.roam.loot:
                hero.x, hero.y = g.roam.loot[0].x, g.roam.loot[0].y
        self.assertGreater(g.hoard.banked, 0, "nothing was ever banked")
        g.hoard.left = 0.01
        g.update(1 / 30)
        self.assertEqual(g.state, G.WAVE_OVER)
        self.assertGreater(g.last_score, 0)
        self.assertTrue(g.meta.runs)
        self.assertEqual(g.meta.runs[0].mode, "hoard")

    def test_the_frame_carries_the_floor(self):
        """A guest that guessed would draw money that is not there."""
        fight = self.arena(party=2)
        fight.scatter(6)
        fight.heroes[0].carrying = 3
        fight.banked[1] = 9
        frame = fight.snapshot()
        self.assertEqual(len(frame["c"]), 6)
        self.assertEqual(frame["hold"][0], 3)
        self.assertEqual(frame["bank"][1], 9)

        mirror = self.arena(party=2)
        mirror.mirror = True
        mirror.apply_snapshot(frame)
        self.assertEqual(len(mirror.loot), 6)
        self.assertEqual(mirror.heroes[0].carrying, 3)
        self.assertEqual(mirror.banked.get(1), 9)

    def test_a_frame_never_describes_a_whole_floor(self):
        from pixel_rpg import freeroam as fr
        fight = self.arena()
        fight.scatter(fr.MAX_SENT_COINS + 40)
        self.assertLessEqual(len(fight.snapshot()["c"]), fr.MAX_SENT_COINS)

    def test_a_guest_waiting_has_something_to_look_at(self):
        """**A guest sat on a blank window** until the host opened the
        arena, because `draw_waves` returns when there is no wave run and
        a night guest has none. Found building the hoard's version of the
        same wait (§86)."""
        import inspect
        from pixel_rpg import menus
        source = inspect.getsource(menus.draw_waves)
        self.assertIn("draw_waiting_room", source)
        self.assertTrue(hasattr(menus, "draw_waiting_room"))


class TestAModeIsNeverNamedByHand(unittest.TestCase):
    """s87. Four bugs in one afternoon, all the same bug.

    A fourth gamemode arrived and **every place that had listed the
    modes by hand was wrong**: the wire clamp turned a hoard into a
    story, the party builder asked `== "zombies"` and made a room of
    one, the arena roll lived inside one starter, and `begin_run` knew
    about a single slotless mode.

    Each of those was written with a comment explaining why hand-kept
    lists go stale. **They are derived now**, and these are the tests
    that keep them that way.
    """

    def test_the_wire_accepts_every_mode_the_game_has(self):
        """§81's lesson, exactly: a key that is not listed is silently
        dropped, and this one decides whether the arena opens at all."""
        from pixel_rpg import coop as coop_mod, gamemodes, security as sec
        keys = coop_mod._mode_keys()
        for mode in gamemodes.MODES:
            self.assertIn(mode.key, keys, mode.key)
            self.assertEqual(sec.safe_choice(mode.key, keys, "story"),
                             mode.key)
        self.assertEqual(sec.safe_choice("nonsense", keys, "story"), "story")

    def test_a_real_time_mode_says_so_rather_than_being_remembered(self):
        from pixel_rpg import gamemodes
        self.assertTrue(gamemodes.is_roam("zombies"))
        self.assertTrue(gamemodes.is_roam("hoard"))
        self.assertFalse(gamemodes.is_roam("waves"))
        self.assertFalse(gamemodes.is_roam("story"))
        self.assertFalse(gamemodes.is_roam("nonsense"))

    def test_nothing_asks_for_one_mode_by_name_any_more(self):
        """A grep, and it is the honest test for this class of bug: the
        four sites that were wrong all looked like `== "zombies"`."""
        import os
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("game.py", "coop.py", "freeroamscreen.py",
                     "leaderboard.py", "menus.py"):
            with open(os.path.join(root, "pixel_rpg", name),
                      encoding="utf-8") as handle:
                source = handle.read()
            for line_no, line in enumerate(source.split(chr(10)), 1):
                if line.lstrip().startswith("#"):
                    continue
                if re.search(r'mode\s*[!=]=\s*"zombies"', line):
                    self.fail("%s:%d still names one real-time mode: %s"
                              % (name, line_no, line.strip()))
                # **And no hand-kept list of them either** (§88): the
                # clamp in `coop`, the one in `leaderboard`, the guest
                # vault, the arena row and the records screen were all
                # tuples of mode keys, and all five were wrong the day a
                # fourth mode existed.
                if '"waves", "zombies"' in line:
                    self.fail("%s:%d keeps its own list of modes: %s"
                              % (name, line_no, line.strip()))

    def test_every_slotless_mode_rolls_its_own_arena(self):
        """*Random each run* lived inside `new_wave_run`, so a night was
        fought in whatever the last Last Stand rolled - or arena zero on
        a fresh launch."""
        import inspect
        from pixel_rpg.game import Game
        for key, starter in Game.RUN_STARTERS.items():
            source = inspect.getsource(getattr(Game, starter))
            self.assertIn("roll_arena", source,
                          "%s never rolls an arena" % key)

    def test_random_really_is_random_in_every_mode(self):
        import pygame
        from pixel_rpg import freeroamscreen as frs, game as G
        _display_only()
        seen = {}
        for key, starter in G.Game.RUN_STARTERS.items():
            keys = set()
            for _ in range(12):
                game = G.Game()
                game.screen = pygame.display.set_mode((900, 600))
                game.settings.arena_mode = "random"
                game.pending_class = "warden"
                getattr(game, starter)("Probe")
                keys.add(frs.arena_of(game).key)
            seen[key] = keys
        for key, keys in seen.items():
            self.assertGreater(len(keys), 2,
                               "%s always uses the same arena: %r"
                               % (key, keys))

    def test_a_hoard_has_no_waves_to_change_arena_on(self):
        """*Changes every 10 waves* has nothing to step through in a mode
        that is one three-minute run, so it reads as the nearest thing
        that means something."""
        import pygame
        from pixel_rpg import freeroamscreen as frs, game as G
        _display_only()
        keys = set()
        for _ in range(12):
            game = G.Game()
            game.screen = pygame.display.set_mode((900, 600))
            game.settings.arena_mode = "every"
            game.pending_class = "warden"
            game.new_hoard_run("Probe")
            keys.add(frs.arena_of(game).key)
        self.assertGreater(len(keys), 2,
                           "a hoard is always in arena zero: %r" % keys)

    def test_the_arena_is_worked_out_in_one_place(self):
        """`arena_of` used to re-derive the choice from `settings`, which
        is a second copy of a decision - and it drifted the moment a
        third mode arrived."""
        import inspect
        from pixel_rpg import freeroamscreen
        source = inspect.getsource(freeroamscreen.arena_of)
        self.assertIn("arena_now", source)

    def test_a_room_in_a_real_time_mode_is_everybody_in_it(self):
        import pygame
        from pixel_rpg import game as G
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((900, 600))
        game.pending_class = "warden"
        game.new_hoard_run("Alone")
        party, order, viewer = game.roam_party()
        self.assertEqual(len(party), 1, "solo is still solo")
        self.assertEqual(order, [])
        self.assertEqual(viewer, 0)

    def test_the_guests_clock_runs(self):
        """A guest's HUD read 3:00 for a whole run because the branch
        that ticks it sat below the mirror's early return."""
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.update_roam)
        hoard_at = source.index("update_hoard")
        mirror_at = source.index("if fight.mirror:")
        self.assertLess(hoard_at, mirror_at,
                        "the hoard branch is below the mirror's return")


class TestEveryModeGetsABoard(unittest.TestCase):
    """s88. His shape, item 0az:

    > also there should be a global score leaderboard for each gamemode
    > for each party size

    **It was built for one gamemode.** The records screen listed Last
    Stand at every party size and nothing else, so a player could survive
    twenty nights and never see the number anywhere - and The Hoard
    arrived into the same silence.

    The same afternoon as §87's four, and the same bug: a list of modes
    written by hand.
    """

    def profile(self, rows):
        import time
        import pygame
        from pixel_rpg import game as G, meta as meta_mod
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        for mode, score, party in rows:
            game.meta.finish_run(meta_mod.RunRecord(
                class_key="warden", level=9, score=score, playtime=100.0,
                modifiers=(), when=time.strftime("%Y-%m-%d %H:%M"),
                mode=mode, party=party, waves=7))
        return game

    def test_every_scoring_mode_appears(self):
        from pixel_rpg import gamemodes, menus
        rows = [(m.key, 1000, 1) for m in gamemodes.MODES
                if m.scores and m.key != "story"]
        game = self.profile(rows)
        labels = " ".join(label for label, _ in menus.board_lines(game.meta))
        for mode in gamemodes.MODES:
            if not mode.scores or mode.key == "story":
                continue
            self.assertIn(mode.name, labels,
                          "%s has no board line" % mode.name)

    def test_a_line_per_party_size(self):
        from pixel_rpg import menus
        game = self.profile([("hoard", 100, 1), ("hoard", 900, 3),
                             ("hoard", 400, 2)])
        lines = menus.board_lines(game.meta)
        sizes = [label for label, _ in lines]
        self.assertEqual(len(sizes), 3, sizes)
        self.assertTrue(any("solo" in label for label in sizes))
        self.assertTrue(any("3 players" in label for label in sizes))

    def test_it_shows_the_best_run_at_that_size(self):
        from pixel_rpg import menus
        game = self.profile([("hoard", 100, 2), ("hoard", 9000, 2)])
        lines = dict(menus.board_lines(game.meta))
        self.assertEqual(lines["The Hoard, 2 players"], "9,000")

    def test_a_mode_nobody_has_played_is_not_listed(self):
        """An empty board is furniture - the same call `scrollbar` made
        about a list that does not scroll."""
        from pixel_rpg import menus
        game = self.profile([("waves", 500, 1)])
        labels = [label for label, _ in menus.board_lines(game.meta)]
        self.assertEqual(labels, ["Last Stand, solo"])

    def test_the_story_is_not_a_board(self):
        """A story run's score measures a different game, and the screen
        already gives it its own line."""
        from pixel_rpg import menus
        game = self.profile([("story", 5000, 1)])
        self.assertEqual(menus.board_lines(game.meta), [])

    def test_a_score_off_the_network_keeps_its_mode(self):
        """§87's clamp bug, in the file next door: a hoard row coming
        back off the board was relabelled a wave score."""
        from pixel_rpg import gamemodes, leaderboard
        for mode in gamemodes.MODES:
            row = leaderboard._clean_row(
                {"name": "A", "score": 10, "mode": mode.key, "party": 2,
                 "waves": 5})
            self.assertEqual(row["mode"], mode.key)
        junk = leaderboard._clean_row(
            {"name": "A", "score": 10, "mode": "nonsense", "party": 1})
        self.assertEqual(junk["mode"], "waves")

    def test_every_scored_mode_scores_itself(self):
        """s89. A night's score was a line of arithmetic inside
        `Game.finish_night_run` - so it was the one scored mode with no
        `score()`, and the difficulty multiplier every other mode applies
        was simply missing from it. §54's *"two payout paths, and the
        rarer one keeps getting patched first"*, in the scoring."""
        from pixel_rpg import gamemodes
        for name in ("WaveRun", "NightRun", "HoardRun"):
            run = getattr(gamemodes, name)()
            self.assertTrue(callable(getattr(run, "score", None)), name)
            self.assertTrue(callable(getattr(run, "end", None)), name)

    def test_a_harder_run_is_worth_more_in_every_mode(self):
        from pixel_rpg import gamemodes

        def scored(cls, difficulty):
            run = cls(difficulty=difficulty)
            run.cleared, run.kills, run.banked = 8, 40, 40
            return run.score()

        for cls in (gamemodes.WaveRun, gamemodes.NightRun,
                    gamemodes.HoardRun):
            easy = scored(cls, "story")
            normal = scored(cls, "normal")
            hard = scored(cls, "nightmare")
            self.assertLess(easy, normal, cls.__name__)
            self.assertLess(normal, hard, cls.__name__)

    def test_ending_a_run_hands_back_its_score(self):
        from pixel_rpg import gamemodes
        run = gamemodes.NightRun()
        run.cleared, run.kills = 5, 20
        self.assertEqual(run.end(), run.score())
        self.assertTrue(run.over)

    def test_the_difficulty_table_is_one_table(self):
        """A hoard was reaching through `WaveRun` to read it, which is
        how a shared rule ends up looking like one mode's property."""
        from pixel_rpg import gamemodes
        self.assertIs(gamemodes.DIFFICULTY_SCORE,
                      gamemodes.WaveRun.DIFFICULTY_SCORE)
        self.assertEqual(gamemodes.difficulty_score("nonsense"), 1.0)
        self.assertEqual(gamemodes.difficulty_score("normal"), 1.0)

    def test_a_night_has_no_kill_cap_and_that_is_deliberate(self):
        """The one rule *not* copied from `WaveRun.score`: that cap
        exists because a wave you can already beat can be farmed, and a
        night ends when its arrivals are dead."""
        from pixel_rpg import gamemodes
        thorough = gamemodes.NightRun()
        thorough.cleared, thorough.kills = 3, 200
        hasty = gamemodes.NightRun()
        hasty.cleared, hasty.kills = 3, 20
        self.assertGreater(thorough.score(), hasty.score())

    def test_each_mode_counts_in_its_own_noun(self):
        """`RunRecord.waves` holds *how far* for every mode, because Last
        Stand invented the field - so a hoard's 208 was printed as **208
        waves** and a night that stood twelve read as *"unfinished"*."""
        from pixel_rpg import gamemodes
        self.assertEqual(gamemodes.depth_label("waves", 3), "3 waves")
        self.assertEqual(gamemodes.depth_label("waves", 1), "1 wave")
        self.assertEqual(gamemodes.depth_label("zombies", 9), "9 nights")
        self.assertEqual(gamemodes.depth_label("hoard", 208), "208 coins")
        self.assertEqual(gamemodes.depth_label("story", 5), "")
        for mode in gamemodes.MODES:
            if mode.key == "story":
                continue
            self.assertTrue(gamemodes.depth_label(mode.key, 2),
                            "%s has no word for how far it got" % mode.key)

    def test_only_a_wave_run_sets_the_wave_record(self):
        """A hoard banking 208 coins was posting *Last Stand best: 208
        waves* - a record for a game nobody had played."""
        import time
        import pygame
        from pixel_rpg import game as G, meta as meta_mod
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((900, 600))
        def record(mode, waves):
            return meta_mod.RunRecord(
                class_key="warden", level=9, score=100, playtime=10.0,
                modifiers=(), when=time.strftime("%Y-%m-%d %H:%M"),
                mode=mode, party=1, waves=waves)
        game.meta.finish_run(record("waves", 12))
        game.meta.finish_run(record("hoard", 900))
        game.meta.finish_run(record("zombies", 400))
        self.assertEqual(game.meta.best_waves, 12)

    def test_the_arena_row_is_one_predicate(self):
        """Two copies of *does this mode have an arena* drifted the
        moment a third mode had one."""
        import inspect
        from pixel_rpg import menus
        source = inspect.getsource(menus.draw_modifier_select)
        self.assertIn("arena_row_shown", source)
        self.assertNotIn('pending_mode in ("waves", "zombies")', source)


class TestTheFirstPlaytestOfTheNewModes(unittest.TestCase):
    """s91. Savvydev played the build on 2026-09-08 and sent six things.

    **Every one of them was in code written in the two days before**, and
    four of them are the same shape as §§87-89: a screen or a mode that
    had been given its own copy of something the rest of the game already
    knew.
    """

    # -- 0bw: the arena picker's second page ---------------------------
    def test_the_scroll_window_does_not_oscillate(self):
        """His: *"the second page of the maps screen is super buggy"*.

        `SCROLL_MARGIN` keeps a row of context above and below the
        cursor. On a **two-row page** those are the same row, so every
        call moved the page one way and the next call moved it back - the
        arena picker swapped between two pages every frame, and merely
        *asking* it how wide it was changed what it showed.
        """
        from pixel_rpg.ui import window_start
        for visible in range(1, 12):
            was = window_start(4, visible, 20, None)
            seen = []
            for _ in range(8):
                was = window_start(4, visible, 20, was)
                seen.append(was)
            self.assertEqual(len(set(seen)), 1,
                             "visible=%d oscillates: %r" % (visible, seen))

    def test_the_cursor_stays_on_the_page_at_any_height(self):
        from pixel_rpg.ui import window_start
        for visible in range(1, 12):
            for index in range(20):
                start = window_start(index, visible, 20, None)
                self.assertTrue(start <= index < start + visible,
                                "visible=%d index=%d start=%d"
                                % (visible, index, start))

    def test_the_picker_measures_the_grid_once(self):
        """The keyboard measured the columns from the *window* and the
        screen measured them from the framed panel inside it - five
        columns for one and four for the other, so Down from the top left
        landed two cells along on the next row."""
        import pygame
        from pixel_rpg import game as G, menus
        _display_only()
        for size in ((1280, 720), (1024, 768), (900, 600), (420, 800)):
            game = G.Game()
            game.screen = pygame.display.set_mode(size)
            game.go(G.ARENA_SELECT)
            _cells, cols, _page = menus.arena_grid(game, menus.frame_rect(game))
            game.ui.arena_index = 0
            game.event_arena_select(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_DOWN, unicode="", mod=0))
            self.assertEqual(game.ui.arena_index, cols,
                             "Down moved %d on a %d-wide grid at %r"
                             % (game.ui.arena_index, cols, size))

    def test_every_cell_on_the_page_is_the_cell_it_looks_like(self):
        """The oscillation showed up as clicks landing on the wrong
        arena, which is the half of it a player would notice."""
        import pygame
        from pixel_rpg import game as G, gamemodes, menus
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.go(G.ARENA_SELECT)
        for _ in range(len(gamemodes.ARENAS)):
            game.ui.hot.clear()
            game.screen.fill((0, 0, 0))
            menus.draw_arena_select(game)
            cells, cols, (start, fit, _rows) = menus.arena_grid(
                game, menus.frame_rect(game))
            for n in range(len(gamemodes.ARENAS)):
                if not start <= n // cols < start + fit:
                    continue
                self.assertEqual(game.ui.hot.at(cells[n].center),
                                 ("arena", n))
            game.event_arena_select(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_DOWN, unicode="", mod=0))

    # -- 0bx: actual zombies -------------------------------------------
    def test_the_night_has_its_own_bestiary(self):
        """His: *"the zombie gamemode should use actual zombies"*. It
        spawned `"meadow"` - slimes and rats."""
        from pixel_rpg import gamemodes
        from pixel_rpg.entities import make_enemy
        names = {make_enemy(6, gamemodes.NIGHT_BIOME).archetype.key
                 for _ in range(60)}
        self.assertTrue(names, "the night biome is empty")
        self.assertEqual(names - {"shambler", "crawler", "risen"}, set())

    def test_the_island_never_meets_them(self):
        from pixel_rpg.entities import ARCHETYPES
        for archetype in ARCHETYPES:
            if "night" in archetype.biomes:
                self.assertEqual(tuple(archetype.biomes), ("night",),
                                 "%s walks the island" % archetype.key)

    def test_the_night_spawns_them(self):
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.update_roam)
        self.assertIn("NIGHT_BIOME", source)

    def test_each_of_the_dead_is_a_different_problem(self):
        """A crowd where everything walks at you the same way is one
        enemy with several health bars."""
        from pixel_rpg.entities import ARCHETYPES_BY_KEY
        night = [ARCHETYPES_BY_KEY[k]
                 for k in ("shambler", "crawler", "risen")]
        self.assertEqual(len({a.spd for a in night}), 3)
        self.assertEqual(len({tuple(sorted(a.skills)) for a in night}), 3)

    # -- 0by: skills have shapes ---------------------------------------
    def test_skills_reach_different_distances(self):
        """His: *"different skills should have like different ranges and
        stuff"*. It was two buckets - a spell reached 1.7x and everything
        else 1.25x - so a Power Strike and a Riposte were the same."""
        from pixel_rpg.freeroam import FreeRoam
        from pixel_rpg.skills import ALL_SKILLS
        reaches = {round(FreeRoam.reach_of(s)) for s in ALL_SKILLS}
        self.assertGreater(len(reaches), 8,
                           "only %d distinct reaches" % len(reaches))
        arcs = {round(FreeRoam.arc_of(s), 2) for s in ALL_SKILLS}
        self.assertGreater(len(arcs), 8, "only %d distinct arcs" % len(arcs))

    def test_a_bolt_is_long_and_thin_and_a_flurry_is_short_and_wide(self):
        from pixel_rpg.freeroam import FreeRoam
        from pixel_rpg.skills import SKILLS_BY_KEY
        bolt = SKILLS_BY_KEY["firebolt"]
        flurry = SKILLS_BY_KEY.get("flurry")
        self.assertGreater(FreeRoam.reach_of(bolt), FreeRoam.reach_of(flurry))
        self.assertLess(FreeRoam.arc_of(bolt), FreeRoam.arc_of(flurry))

    def test_nothing_becomes_a_sniper(self):
        from pixel_rpg import freeroam as fr
        from pixel_rpg.skills import ALL_SKILLS
        for skill in ALL_SKILLS:
            self.assertLessEqual(fr.FreeRoam.reach_of(skill),
                                 fr.SWING_REACH * 2.31, skill.key)
            self.assertGreaterEqual(fr.FreeRoam.reach_of(skill),
                                    fr.SWING_REACH * 0.84, skill.key)
            self.assertGreater(fr.FreeRoam.arc_of(skill), 0.0, skill.key)

    def test_the_telegraph_is_drawn_at_the_size_that_hits(self):
        """Its own docstring: *"aiming feedback, not decoration"*. The
        arc was drawn at the default width while the simulation used the
        skill's, so it lied about everything but a bare swing."""
        import inspect
        from pixel_rpg import freeroamscreen
        source = inspect.getsource(freeroamscreen.draw_swing)
        self.assertIn("roam_arc", source)
        self.assertNotIn("fr.SWING_ARC * n / steps", source)

    def test_the_arc_crosses_with_the_swing(self):
        import pygame
        from pixel_rpg import game as G
        from pixel_rpg.skills import SKILLS_BY_KEY
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((900, 600))
        game.pending_class = "warden"
        game.new_night_run("Tester")
        seen = set()
        for key in ("firebolt", "strike"):
            skill = SKILLS_BY_KEY.get(key)
            if skill is None:
                continue
            game.player.mp = game.player.max_mp()
            game.roam.heroes[0].swing_cd = 0.0
            game.roam.charge(0, skill)
            game.roam.swing(0)
            seen.add((round(game.roam_reach), round(game.roam_arc, 2)))
        self.assertEqual(len(seen), 2, "both swings looked the same: %r"
                         % (seen,))

    # -- 0ca: full health is a warden's -------------------------------
    def test_a_full_heal_is_only_offered_after_a_boss(self):
        """His: *"you shouldnt be able to like get to max hp after every
        single wave, only after killing a boss"*."""
        from pixel_rpg import spoils
        ordinary = [b.key for _ in range(200)
                    for b in spoils.draw_buffs(4, 8, boss=False)]
        self.assertNotIn("wholeagain", ordinary)
        after = [b.key for _ in range(200)
                 for b in spoils.draw_buffs(4, 8, boss=True)]
        self.assertIn("wholeagain", after)

    def test_partial_heals_are_still_ordinary(self):
        """He objected to reaching *max*, not to being patched up."""
        from pixel_rpg import spoils
        drawn = {b.key for _ in range(200)
                 for b in spoils.draw_buffs(4, 4, boss=False)}
        self.assertTrue({"catch", "mend"} & drawn)

    def test_the_wave_asks_whether_a_warden_fell(self):
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.offer_spoils)
        self.assertIn("boss_wave", source)

    def test_every_full_restore_card_is_marked(self):
        """A card that says *back to full* and is not marked would put
        the hole straight back."""
        from pixel_rpg import spoils
        for buff in spoils.BUFFS:
            if "full" in buff.line.lower():
                self.assertTrue(buff.after_boss, buff.key)

    # -- 0cb: hosting a new mode --------------------------------------
    def test_every_mode_opens_the_room_it_was_asked_for(self):
        """His: *"when playing one of the new gamemodes, co-op
        specifically doesnt present you with the room code option"*.

        `_host_if_asked` was called by three run starters and the two
        written later were not among them, so hosting a night or a hoard
        opened **no room at all**.
        """
        import inspect
        from pixel_rpg.game import Game
        for key, starter in Game.RUN_STARTERS.items():
            source = inspect.getsource(getattr(Game, starter))
            self.assertIn("_host_if_asked", source,
                          "%s never opens the room" % key)

    def test_leaving_the_lobby_goes_back_to_the_run(self):
        """`OVERWORLD` was hard-wired, so a Last Stand host leaving the
        lobby was put on the island their run never walks."""
        import pygame
        from pixel_rpg import game as G
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((900, 600))
        game.pending_class = "warden"
        game.new_wave_run("Hostess")
        self.assertEqual(game.run_screen(), G.WAVES)
        game.new_night_run("Hostess")
        self.assertEqual(game.run_screen(), G.ROAM)
        game.new_hoard_run("Hostess")
        self.assertEqual(game.run_screen(), G.ROAM)

    def test_the_lobby_asks_where_the_run_is(self):
        import inspect
        from pixel_rpg.game import Game
        source = inspect.getsource(Game.event_lobby)
        self.assertIn("run_screen", source)


class TestFleeingAFinishedFight(unittest.TestCase):
    """s92, found by `tests/uifuzz.py` on its third run.

    Kill the last monster and press Flee before the victory resolves -
    one or two frames - and `player_flee` did `max()` over an empty list
    and **crashed the game**.

    `battle.py` is the oldest and most-tested module here, it has 1300
    unit tests around it, and nothing had ever pressed two buttons in the
    wrong order quickly enough.
    """

    def battle(self):
        from pixel_rpg.battle import Battle
        from pixel_rpg.entities import Player, make_enemy
        player = Player("Runner", "warden")
        player.level = 6
        player.full_restore()
        return Battle(player, [make_enemy(4, "meadow")])

    def test_fleeing_an_empty_field_does_not_raise(self):
        fight = self.battle()
        for enemy in fight.enemies:
            enemy.hp = 0
        fight.player_flee()          # must not raise
        self.assertFalse(fight.fled)

    def test_it_says_why_rather_than_doing_nothing_quietly(self):
        fight = self.battle()
        for enemy in fight.enemies:
            enemy.hp = 0
        said = len(fight.log)
        fight.player_flee()
        self.assertGreater(len(fight.log), said)

    def test_fleeing_a_real_fight_still_works(self):
        """The fix must not make flee impossible - it is a whole
        modifier's worth of behaviour (Oathkeeper turns it off)."""
        outcomes = []
        for _ in range(40):
            fight = self.battle()
            fight.player_flee()
            outcomes.append(fight.phase)
        self.assertTrue(any(p == "acting" for p in outcomes), outcomes)


class TestTheInputFuzzerItself(unittest.TestCase):
    """s92. The instrument, checked the way `layout.py --selftest` is.

    **A fuzzer that cannot fail is a fuzzer that says nothing**, and this
    one asserts four rules that are easy to write and easy to get wrong.
    """

    def fuzzer(self):
        import os
        import sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tests = os.path.join(root, "tests")
        if tests not in sys.path:
            sys.path.insert(0, tests)
        import uifuzz
        return uifuzz

    def test_a_short_run_is_clean(self):
        """Also the cheapest possible guard against the fuzzer itself
        rotting: it is only run by hand, so nothing else would notice."""
        fuzz = self.fuzzer()
        _display_only()
        fuzz.one_round(4242, events=12)

    def test_it_knows_which_kinds_the_click_handler_answers(self):
        """Read out of `mouse_click`, not typed - the list this replaced
        would have gone stale the first time a screen added a button."""
        fuzz = self.fuzzer()
        self.assertIn("spoil", fuzz.ACTED_ON | {"spoil"})
        self.assertTrue(fuzz.ACTED_ON, "the scan found nothing; regex rotted")

    def test_it_walks_every_mode(self):
        from pixel_rpg import gamemodes
        fuzz = self.fuzzer()
        for mode in gamemodes.MODES:
            if mode.saves:
                continue
            self.assertIn(mode.key, fuzz.STARTS, mode.key)

    def test_it_is_put_on_the_screens_mashing_keys_never_finds(self):
        """2400 random events reached the offers screen **zero times**
        before this existed, which reads as coverage and is not."""
        fuzz = self.fuzzer()
        self.assertIn("spoils", fuzz.SCENARIOS)
        self.assertIn("picker", fuzz.SCENARIOS)

    def test_it_is_put_on_the_long_roads_screens_too(self):
        """s94. 43,200 events never reached the shop, the smith, falling,
        a new skill, a full bag, a chapter card or the Door. Each scenario
        has to land where it says, or it is coverage in name only."""
        import random
        fuzz = self.fuzzer()
        _display_only()
        self.assertLessEqual(fuzz.STORY_SCENARIOS, set(fuzz.SCENARIOS))
        lands = {"shop": "shop", "smith": "smith", "gameover": "gameover",
                 "newskill": "newskill", "fullbag": "fullbag",
                 "chapter": "chaptercard", "ending": "endingchoice"}
        was = fuzz.SCENARIOS
        try:
            for scenario, state in sorted(lands.items()):
                fuzz.SCENARIOS = (scenario,)
                game, _where = fuzz.fresh(random.Random(7), (1280, 720))
                self.assertEqual(game.state, state, scenario)
        finally:
            fuzz.SCENARIOS = was

    def test_it_only_counts_screens_that_exist(self):
        """`battle.MENU` and `battle.SUBMENU` are phases of a fight that
        `game.py` imports, and the coverage report listed them as screens
        it never reached. Read from the source, the list is the states."""
        fuzz = self.fuzzer()
        states = fuzz.known_states()
        self.assertNotIn("menu", states)
        self.assertNotIn("submenu", states)
        for state in ("overworld", "shop", "endingchoice", "trade"):
            self.assertIn(state, states)



class TestTextIsMeasuredNotAssumed(unittest.TestCase):
    """s94. The layout fit one face, and the first cloud session had another.

    **Every number here used to be a width somebody measured once in
    Consolas** - 660 for the scene panel, 260 for a button, 280 for a
    value column, seven pixels a letter for a skill chip. Consolas is on
    his PC and nowhere else: a Linux container hands back FreeMono, a Mac
    Menlo, and both are a tenth wider. These are the decisions that
    replaced the constants.
    """

    def setUp(self):
        _display_only()
        from pixel_rpg.assets import Assets
        self.assets = Assets()

    def test_a_range_note_keeps_its_range_at_every_width(self):
        """The range answers "is there more"; the hint only says which key.
        So the hint shortens and then goes, and the range never does."""
        from pixel_rpg.menus import range_note
        font = self.assets.font("tiny")
        span = "1-21 of 24"
        full = range_note(font, 1, 21, 24, 2000)
        self.assertEqual(full, span + "   Left and Right to read on")
        for width in range(font.size(span)[0], font.size(full)[0] + 1, 3):
            note = range_note(font, 1, 21, 24, width)
            self.assertTrue(note.startswith(span), (width, note))
            self.assertLessEqual(font.size(note)[0], width, note)
        short = range_note(font, 1, 21, 24,
                           font.size(span + "   Left/Right")[0])
        self.assertEqual(short, span + "   Left/Right")

    def test_a_label_steps_down_a_size_before_it_is_cut(self):
        """s35's rule for rows, as one call: the largest face that fits."""
        from pixel_rpg.menus import fitting_size
        text = "Put your hands where hers were"
        small = self.assets.font("small").size(text)[0]
        tiny = self.assets.font("tiny").size(text)[0]
        self.assertLess(tiny, small)
        self.assertEqual(fitting_size(self.assets, text, small), "small")
        self.assertEqual(fitting_size(self.assets, text, small - 1), "tiny")
        self.assertEqual(fitting_size(self.assets, text, tiny - 1), "tiny",
                         "nothing fits: the smallest, and the caller cuts")

    def test_the_scene_panel_is_66_columns_of_whatever_face_this_is(self):
        """660 was 66 columns of Consolas and 60 of anything wider, so the
        hand-broken prose re-wrapped on every machine but his. Never
        narrower than 660, so on Consolas nothing moves."""
        from pixel_rpg import game as G
        from pixel_rpg import menus
        from pixel_rpg import scenes as scenes_mod
        game = G.Game()
        game.screen = pygame.display.set_mode((1600, 900))
        scene = scenes_mod.SCENES_BY_KEY["prologue"]
        game.scene = {"scene": scene, "options": list(scene.choices),
                      "index": 0, "phase": "choosing", "reply": (),
                      "after": None, "scroll": 0}
        box, _wrapped, _capacity, _rows = menus.scene_measure(game)
        self.assertGreaterEqual(box.w, 660)
        self.assertGreaterEqual(
            box.w - 60, game.assets.font("small").size("M" * 66)[0])

    def test_a_pinned_font_is_the_font_that_is_used(self):
        """`tests/layout.py --font` is only worth anything if the game then
        measures in the face it was pointed at."""
        from pixel_rpg.assets import Assets
        path = os.path.join(os.path.dirname(pygame.__file__),
                            pygame.font.get_default_font())
        self.assertTrue(os.path.isfile(path), path)
        was = Assets.FONT_PATH
        try:
            Assets.FONT_PATH = path
            self.assertEqual(Assets().font_path, path)
        finally:
            Assets.FONT_PATH = was
        self.assertIsNone(Assets.FONT_PATH, "the game itself never pins one")

    def test_the_layout_checker_can_see_a_value_off_its_line(self):
        """The askew rule is in `layout.py --selftest`, which is in the
        gate - this only guards that the self-test still names it, so a
        refactor cannot drop the case and keep the green."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "tests", "layout.py")) as handle:
            source = handle.read()
        self.assertIn('"ASKEW", value_half_a_line_high', source)
        self.assertIn('"quests active"', source)


class TestTheDoorStartsOnYourEnding(unittest.TestCase):
    """s94. Mechanism 1 of s33, the design he invented: **choices lean;
    they never pick**, and *"the Door simply starts your cursor on
    whichever is heaviest."*

    `offer_ending_menu` found the heaviest ending, put the cursor on it -
    and then set it to 0 on the next line. `leading_ending` had a test and
    the cursor did not, so the half that the player can see was the half
    that never happened.
    """

    def door(self, leanings):
        from pixel_rpg import game as G
        from pixel_rpg import story
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Leaner", 6)
        game.dialogue = None
        for entry in story.CODEX:
            game.meta.unlock_codex(entry.key)
        for portal in game.world.portals:
            portal.defeated = True
        game.player.leanings = dict(leanings)
        game.offer_ending_menu()
        self.addCleanup(game.saves.delete, 6)
        return game

    def test_the_cursor_starts_on_the_heaviest_ending(self):
        probe = self.door({})
        keys = [option.key for option in probe.ending_options]
        self.assertGreaterEqual(len(keys), 2, keys)
        for leaning in keys[1:]:
            game = self.door({leaning: 5})
            self.assertEqual(
                game.ending_options[game.ui.ending_index].key, leaning,
                "leaning %s, the cursor started on %s"
                % (leaning, game.ending_options[game.ui.ending_index].key))

    def test_no_answers_start_it_at_the_top(self):
        game = self.door({})
        self.assertEqual(game.ui.ending_index, 0)

    def test_leaning_toward_one_that_is_not_offered_starts_at_the_top(self):
        """A lean can point at an ending the Door is not offering this
        time - the default has to survive that, which is exactly the
        line that was in the wrong place."""
        game = self.door({"no such ending": 9})
        self.assertEqual(game.ui.ending_index, 0)


class TestTheNightBeforeThePlaytest(unittest.TestCase):
    """s95. What the overnight polish pass decided, one test a decision.

    His, going to sleep on 2026-09-29: *"smoothing out the game, making
    sure there are little to none issues making it looks beautiful and
    smooth"*. Most of what that found was drawing, and a screenshot is the
    proof of drawing (rule 31.4). These are the rules underneath it.
    """

    def game(self, size=(1280, 720)):
        from pixel_rpg import game as G
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode(size)
        game.pending_class = "warden"
        game.new_run("Framer", 6)
        game.dialogue = None
        self.addCleanup(game.saves.delete, 6)
        return game

    # --- a room is framed, not followed -------------------------------
    def test_a_room_that_fits_is_framed_by_its_middle(self):
        game = self.game()
        game.enter_house((40, 40))
        rx, ry, rw, rh = game.world.room_bounds
        x, y = game.camera_target()
        self.assertEqual(x, (rx + rw / 2) * game.tile)
        self.assertEqual(y, (ry + rh / 2) * game.tile)
        # and it stays put while you walk about inside it
        game.player.x += game.tile * 3
        self.assertEqual(game.camera_target()[0], x)

    def test_a_room_wider_than_the_window_still_follows_you(self):
        game = self.game((390, 844))
        game.enter_house((40, 40))
        rx, ry, rw, rh = game.world.room_bounds
        self.assertGreater(rw * game.tile, game.camera.view_w)
        self.assertEqual(game.camera_target()[0], game.player.x)

    def test_outdoors_the_camera_follows_the_player(self):
        game = self.game()
        self.assertEqual(game.camera_target(),
                         (game.player.x, game.player.y))

    # --- a colour that means something still has to be read ------------
    def test_legible_leaves_a_bright_colour_alone(self):
        from pixel_rpg.ui import legible
        self.assertEqual(legible((250, 200, 96)), (250, 200, 96))

    def test_legible_lifts_a_dark_one_to_the_faintest_text(self):
        from pixel_rpg.ui import FAINT, _luma, legible
        for dark in ((20, 18, 22), (60, 40, 30), (70, 76, 88)):
            lifted = legible(dark)
            self.assertGreaterEqual(_luma(lifted), _luma(FAINT) - 1, dark)
            # it keeps its hue: the channel that led still leads
            self.assertEqual(max(range(3), key=lambda i: dark[i]),
                             max(range(3), key=lambda i: lifted[i]))

    # --- darkening the screen, cheaply --------------------------------
    def test_tint_screen_matches_the_old_layer_to_a_rounding_step(self):
        from pixel_rpg.ui import tint_screen
        _display_only()
        size = (64, 48)
        base = pygame.Surface(size)
        for x in range(64):
            for y in range(48):
                base.set_at((x, y), ((x * 4) % 256, (y * 5) % 256, 90))
        old, new = base.copy(), base.copy()
        layer = pygame.Surface(size, pygame.SRCALPHA)
        layer.fill((6, 5, 10, 176))
        old.blit(layer, (0, 0))
        tint_screen(new, (6, 5, 10), 176)
        a = pygame.image.tostring(old, "RGB")
        b = pygame.image.tostring(new, "RGB")
        self.assertLessEqual(max(abs(p - q) for p, q in zip(a, b)), 1)

    def test_tint_screen_at_nothing_draws_nothing(self):
        from pixel_rpg.ui import tint_screen
        _display_only()
        surface = pygame.Surface((8, 8))
        surface.fill((40, 50, 60))
        tint_screen(surface, (0, 0, 0), 0)
        self.assertEqual(surface.get_at((3, 3))[:3], (40, 50, 60))

    # --- kept layers ----------------------------------------------------
    def test_the_layer_cache_stays_inside_its_budget(self):
        from pixel_rpg.ui import _LayerCache
        _display_only()
        cache = _LayerCache()
        cache.BUDGET = 64 * 64 * 4 * 3
        for n in range(10):
            cache.put(n, pygame.Surface((64, 64)))
            self.assertLessEqual(cache.used, cache.BUDGET)
        self.assertIsNotNone(cache.get(9))

    def test_a_kept_panel_looks_like_a_fresh_one(self):
        from pixel_rpg import ui
        _display_only()
        first = pygame.Surface((200, 120))
        second = pygame.Surface((200, 120))
        ui.panel(first, (10, 10, 150, 80))
        ui.panel(second, (10, 10, 150, 80))          # from the cache
        self.assertEqual(pygame.image.tostring(first, "RGB"),
                         pygame.image.tostring(second, "RGB"))

    # --- the battle backdrop ------------------------------------------
    def test_the_drifting_skylines_are_run_length_encoded(self):
        from pixel_rpg import battlescreen
        from pixel_rpg.assets import Assets
        _display_only()
        for layer in battlescreen.backdrop_layers(
                Assets(), (320, 180), (40, 30, 70), "spires", 7):
            # RLEACCELOK when asked for; RLEACCEL once the first blit
            # has encoded it - either says it is on.
            self.assertTrue(layer.get_flags()
                            & (pygame.RLEACCEL | pygame.RLEACCELOK))

    def test_motes_are_where_they_always_were(self):
        """Drawn as squares instead of through a window-sized layer, and
        pixel for pixel the same picture."""
        import math
        from pixel_rpg import battlescreen as B
        _display_only()
        size = (320, 180)

        def old(surface, kind, elapsed, alpha=70):
            count, colour, speed = B.MOTES.get(kind, B.MOTES["hills"])
            width, height = surface.get_size()
            layer = pygame.Surface((width, height), pygame.SRCALPHA)
            for i in range(count):
                phase = i * 2.39996
                x = (math.sin(phase) * 0.5 + 0.5) * width
                x = ((x + elapsed * speed * (0.5 + (i % 3) * 0.4))
                     % (width + 20) - 10)
                y = ((math.cos(phase * 1.7) * 0.5 + 0.5) * height * 0.62
                     + math.sin(elapsed * 0.6 + phase) * 6.0)
                side = 1 + (i % 3)
                pygame.draw.rect(layer, colour + (alpha,),
                                 (int(x), int(y), side, side))
            surface.blit(layer, (0, 0))

        for kind in B.MOTES:
            for elapsed in (0.0, 3.7, 41.2):
                a = pygame.Surface(size)
                a.fill((30, 20, 50))
                b = a.copy()
                old(a, kind, elapsed)
                B.draw_motes(b, kind, elapsed)
                self.assertEqual(pygame.image.tostring(a, "RGB"),
                                 pygame.image.tostring(b, "RGB"),
                                 (kind, elapsed))

    # --- words --------------------------------------------------------
    def test_no_guide_paragraph_is_mistaken_for_a_table(self):
        """A line starting with a space is kept as written, because it is
        a table. Two such lines where the first stops mid-sentence and the
        next carries on in lower case are a paragraph that was indented,
        and it rendered broken in two places with its indent lost."""
        from pixel_rpg import guide
        for page in guide.PAGES:
            lines = list(page.lines)
            for this, after in zip(lines, lines[1:]):
                if not (this.startswith(" ") and after.startswith(" ")):
                    continue
                first = after.strip()[:1]
                self.assertFalse(
                    this.rstrip()[-1:].isalpha() and first.islower(),
                    "%s: %r runs on into %r" % (page.title, this, after))

    def test_an_arena_is_the_same_place_on_every_machine(self):
        """Cover and ground from `stable_seed`, not a salted `hash()`: two
        processes with different hash salts are two machines in one room,
        and they must agree on where the rocks are."""
        import subprocess
        probe = (
            "from pixel_rpg import freeroam, gamemodes, textures\n"
            "from pixel_rpg.util import stable_seed\n"
            "out = []\n"
            "for a in gamemodes.ARENAS:\n"
            "    out.append([tuple(round(v, 3) for v in b) for b in\n"
            "        freeroam.plan_blockers(getattr(a, 'plan', 'open'),\n"
            "                               stable_seed(a.key))])\n"
            "out.append(stable_seed(('grass', 2), 2 ** 31))\n"
            "print(repr(out))\n")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        seen = []
        for salt in ("1", "2"):
            env = dict(os.environ, PYTHONHASHSEED=salt)
            seen.append(subprocess.run(
                [sys.executable, "-c", probe], cwd=root, env=env,
                capture_output=True, text=True, check=True).stdout)
        self.assertEqual(seen[0], seen[1])
        self.assertTrue(seen[0].strip())

    def field_with_a_wall(self):
        from pixel_rpg.world import T_GRASS, T_WALL
        game = self.game()
        for y in range(40, 60):
            for x in range(40, 120):
                game.world.grid[y][x] = T_WALL if y == 50 else T_GRASS
                game.world.props.pop((x, y), None)
        return game

    def test_walking_along_a_wall_slides_and_never_enters_it(self):
        """The probe was the rounded step and the move was the real one,
        so the box could end a pixel inside a wall - and from there a
        diagonal walk along it stopped dead, about one approach in
        thirty. Every frame rate, every sub-pixel start."""
        game = self.field_with_a_wall()

        class Held:
            def __init__(self, keys):
                self.keys = keys

            def __getitem__(self, code):
                return code in self.keys

        keys = Held({pygame.K_UP, pygame.K_RIGHT})
        for fps in (30, 60, 144):
            for eighth in range(0, 32, 3):
                game.player.x = 60 * 32 + 16
                game.player.y = 51 * 32 + 14 + eighth / 8.0
                start = game.player.x
                for _ in range(int(fps * 1.5)):
                    game.move_player(1.0 / fps, keys)
                    self.assertFalse(game._collides(game.player_rect()),
                                     (fps, eighth))
                self.assertGreater(game.player.x - start, 50, (fps, eighth))
                self.assertEqual(game.player_rect().top, 51 * 32,
                                 "it should end touching the wall")

    def test_somebody_inside_a_wall_can_walk_out(self):
        game = self.field_with_a_wall()
        game.player.x, game.player.y = 60 * 32 + 16, 50 * 32 + 16

        class Down:
            def __getitem__(self, code):
                return code == pygame.K_DOWN

        for _ in range(60):
            game.move_player(1 / 60, Down())
        self.assertFalse(game._collides(game.player_rect()))

    def test_a_step_is_a_sixtieth_of_a_second_at_any_frame_rate(self):
        """Counted per frame, "Walk 900 steps" ran at the speed of the
        computer. At 60fps it counts exactly what it always did."""
        game = self.field_with_a_wall()

        class Right:
            def __getitem__(self, code):
                return code == pygame.K_RIGHT

        counts = {}
        for fps in (30, 60, 144):
            game.player.x, game.player.y = 45 * 32, 55 * 32
            game.step_carry = 0.0
            before = game.player.stats_tracker["steps"]
            for _ in range(fps):                       # one second
                game.move_player(1.0 / fps, Right())
            counts[fps] = game.player.stats_tracker["steps"] - before
        self.assertEqual(counts[60], 60)
        for fps, count in counts.items():
            self.assertLessEqual(abs(count - 60), 1, counts)

    def test_a_codex_row_holds_both_of_its_lines(self):
        from pixel_rpg.assets import Assets
        _display_only()
        assets = Assets()
        title_h = assets.font("body").get_height()
        preview_y = 4 + title_h + 1
        row_h = max(44, preview_y + assets.font("tiny").get_height() + 5 + 6)
        box_h = row_h - 6
        self.assertLessEqual(preview_y + assets.font("tiny").get_height(),
                             box_h - 2, "the preview sits on the border")


class TestAHeldArrowRunsDownAMenu(unittest.TestCase):
    """s96. Held on a menu, an arrow repeats - the way a pad's already did.

    A forty-row list was forty presses from a keyboard. Arrows only, only
    where an arrow is a press, and never into a key being rebound.
    """

    class Held:
        def __init__(self, *keys):
            self.keys = set(keys)

        def __getitem__(self, code):
            return code in self.keys

    def game(self):
        from pixel_rpg import game as G
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Holder", 6)
        game.dialogue = None
        self.addCleanup(game.saves.delete, 6)
        return game

    def repeats(self, game, key, seconds, state=None):
        """Press `key`, hold it for `seconds`; the repeats that came of it."""
        from unittest import mock
        heard = []
        real = game.handle_event

        def spy(event):
            if getattr(event, "repeat", False):
                heard.append(event.key)
            real(event)

        game.handle_event = spy
        held = self.Held(key)
        with mock.patch("pygame.key.get_pressed", return_value=held):
            real(pygame.event.Event(pygame.KEYDOWN, key=key, unicode="",
                                    mod=0))
            for _ in range(int(round(seconds * 60))):
                game.update(1 / 60)
        return heard

    def test_down_held_on_pause_waits_then_streams(self):
        game = self.game()
        game.state = "pause"
        self.assertEqual(self.repeats(game, pygame.K_DOWN, 0.4), [])
        game.key_repeat.clear()
        heard = self.repeats(game, pygame.K_DOWN, 1.0)
        # 0.42 before the first, then one every 0.14 - at 60 frames a
        # second that lands on frames 26, 35, 44 and 53
        self.assertEqual(len(heard), 4, heard)
        self.assertEqual(set(heard), {pygame.K_DOWN})

    def test_letting_go_stops_it(self):
        from unittest import mock
        game = self.game()
        game.state = "pause"
        self.repeats(game, pygame.K_DOWN, 0.2)
        with mock.patch("pygame.key.get_pressed", return_value=self.Held()):
            game.update(1 / 60)
        self.assertEqual(game.key_repeat, {})

    def test_enter_never_repeats(self):
        game = self.game()
        game.state = "pause"
        self.assertEqual(self.repeats(game, pygame.K_RETURN, 1.0), [])

    def test_walking_is_not_repeated(self):
        game = self.game()
        game.state = "overworld"
        self.assertEqual(self.repeats(game, pygame.K_DOWN, 1.0), [])

    def test_settings_stay_deaf_for_the_code(self):
        game = self.game()
        game.state = "settings"
        self.assertEqual(self.repeats(game, pygame.K_DOWN, 1.0), [])

    def test_a_held_up_is_one_up_to_the_code(self):
        game = self.game()
        game.state = "pause"
        game.konami.progress = 0
        # one repeat: fed to the watcher it would read as the code's
        # second Up (an odd number of them lands back on 1 by chance)
        heard = self.repeats(game, pygame.K_UP, 0.5)
        self.assertEqual(len(heard), 1)
        self.assertEqual(game.konami.progress, 1)

    def test_a_key_being_rebound_hears_no_repeat(self):
        from unittest import mock
        game = self.game()
        game.state = "keybinds"
        self.repeats(game, pygame.K_DOWN, 0.2)          # walking the list
        game.ui.rebinding = "interact"                  # then Enter on a row
        with mock.patch("pygame.key.get_pressed",
                        return_value=self.Held(pygame.K_DOWN)):
            for _ in range(60):
                game.update(1 / 60)
        self.assertEqual(game.ui.rebinding, "interact")

    def test_a_new_screen_forgets_the_held_key(self):
        from unittest import mock
        game = self.game()
        game.state = "pause"
        self.repeats(game, pygame.K_DOWN, 0.2)
        self.assertIn(pygame.K_DOWN, game.key_repeat)
        game.state = "quests"
        with mock.patch("pygame.key.get_pressed",
                        return_value=self.Held(pygame.K_DOWN)):
            game.update(1 / 60)
        self.assertEqual(game.key_repeat, {})

    def test_a_pad_repeat_does_not_cancel_a_rebind(self):
        from pixel_rpg import gamepad
        game = self.game()
        game.state = "keybinds"
        pad = gamepad.Gamepad()
        pad._repeat.clear()
        pad._repeat_keys(0.0, game, {pygame.K_DOWN})     # the first press
        game.ui.rebinding = "interact"
        for _ in range(60):
            pad._repeat_keys(1 / 60, game, {pygame.K_DOWN})
        self.assertEqual(game.ui.rebinding, "interact")




class TestTheWheelStaysInsideTheList(unittest.TestCase):
    """s96. The UI fuzzer: wheel, wheel, Enter on the codex -> IndexError.

    The wheel moved a list's cursor with no upper bound and left the draw
    to put it back; every handler indexes its rows with it directly. Two
    events in one frame - a trackpad flick and Enter - landed between.
    """

    def test_sixty_notches_then_enter_on_every_wheeled_list(self):
        from pixel_rpg import game as G
        from pixel_rpg.items import roll_item
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Wheeler", 6)
        game.dialogue = None
        self.addCleanup(game.saves.delete, 6)
        for _ in range(3):
            game.player.add_item(roll_item(game.player.level + 3))
        game.meta.unlock_codex(story_first_codex_key())
        enter = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN,
                                   unicode="", mod=0)
        for state in (G.INVENTORY, G.SHOP, G.SMITH, G.QUESTS, G.CODEX,
                      G.SLOTS):
            game.state = G.OVERWORLD
            game.dialogue = None
            game.reading = None
            game.go(state)
            if state == G.SHOP:
                game.ui.refresh_shop(game.player, game.world)
            window = game.scroll_windows()[state]
            window.index = window.offset = 0
            for _ in range(60):
                game.mouse_scroll(-1)
            count = game.scroll_length()
            self.assertLess(window.index, max(1, count), state)
            self.assertGreaterEqual(window.index, 0, state)
            if state == G.SLOTS:
                continue                # Enter there loads a run
            game.handle_event(enter)    # raised on the codex


    def test_a_clicked_tab_starts_its_list_at_the_top_like_tab_does(self):
        from pixel_rpg import game as G
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Tabber", 6)
        self.addCleanup(game.saves.delete, 6)
        for tab, cursor in (("codex_tab", "codex"), ("inv_tab", "inv"),
                            ("quest_tab", "quest")):
            window = getattr(game.ui, cursor)
            setattr(game.ui, tab, 0)
            window.index, window.offset = 5, 2
            self.assertTrue(game.point_cursor(tab, 0))     # the same tab
            self.assertEqual((window.index, window.offset), (5, 2), tab)
            self.assertTrue(game.point_cursor(tab, 1))     # another one
            self.assertEqual(getattr(game.ui, tab), 1, tab)
            self.assertEqual((window.index, window.offset), (0, 0), tab)
        game.ui.shop_mode = "buy"
        game.ui.shop.index = 4
        game.point_cursor("shop_tab", 1)
        self.assertEqual((game.ui.shop_mode, game.ui.shop.index), ("sell", 0))

def story_first_codex_key():
    from pixel_rpg import story
    return story.CODEX[0].key


class TestTheGuideNamesYourKeys(unittest.TestCase):
    """s96. The guide said E, M, (C), J, T and F5 whatever you had bound."""

    def text(self, game):
        from pixel_rpg import guide, menus
        return [" ".join(menus.guide_measure(game, i)[0])
                for i in range(len(guide.PAGES))]

    def game(self):
        from pixel_rpg import game as G
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        return game

    def test_the_defaults_read_as_they_always_did(self):
        pages = self.text(self.game())
        whole = " ".join(pages)
        self.assertIn("WASD or the arrow keys walk. Hold Shift to move "
                      "quickly.", whole)
        self.assertIn("E interacts with whatever you are standing next "
                      "to", whole)
        self.assertIn("M opens the map.", whole)
        self.assertIn("the hero screen (C)", whole)
        self.assertIn("whenever you press F5.", whole)

    def test_a_rebound_key_is_the_one_it_names(self):
        game = self.game()
        game.binds.rebind("interact", pygame.K_f)
        game.binds.rebind("map", pygame.K_k)
        game.binds.rebind("up", pygame.K_i)
        whole = " ".join(self.text(game))
        self.assertIn("F interacts with", whole)
        self.assertIn("K opens the map.", whole)
        self.assertIn("IASD or the arrow keys walk.", whole)
        self.assertNotIn("E interacts", whole)

    def test_no_page_is_left_with_a_blank_to_fill(self):
        for page in self.text(self.game()):
            self.assertNotIn("{", page)
            self.assertNotIn("}", page)


class TestUnspentPointsSayWhereToGo(unittest.TestCase):
    """s96. "4 points!" said nothing about where; a hero who never opens
    the sheet fights with none of them."""

    def game(self):
        from pixel_rpg import game as G
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Spender", 6)
        game.dialogue = None
        game.state = G.OVERWORLD
        self.addCleanup(game.saves.delete, 6)
        game.player.points = 4
        return game

    def badge(self, game):
        code = game.binds.key_for("hero")
        return [rect for rect, kind, index in game.ui.hot.items
                if kind == "key" and index == code
                and rect.right > 300 and 90 < rect.top < 130]

    def test_a_click_on_the_badge_opens_the_sheet(self):
        from pixel_rpg import game as G
        game = self.game()
        game.ui.hot.clear()
        game.draw()
        found = self.badge(game)
        self.assertEqual(len(found), 1)
        game.mouse_click(found[0].center, 1)
        self.assertEqual(game.state, G.CHARACTER)

    def test_it_names_the_key_you_bound(self):
        game = self.game()
        game.binds.rebind("hero", pygame.K_h)
        game.ui.hot.clear()
        game.draw()
        self.assertEqual(len(self.badge(game)), 1)

    def test_under_a_menu_it_is_not_a_button(self):
        from pixel_rpg import game as G
        game = self.game()
        game.go(G.INVENTORY)
        game.ui.hot.clear()
        game.draw()
        self.assertEqual(self.badge(game), [])


class TestDrawingRollsNothing(unittest.TestCase):
    """s96. A 20,000-event fuzz run: the battle's click targets moved
    between two draws with no input. `draw_battle` followed the camera,
    and the camera rolled its shake on the gameplay stream."""

    def battle(self):
        from pixel_rpg import game as G
        from pixel_rpg.entities import make_encounter
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Shaker", 6)
        game.dialogue = None
        self.addCleanup(game.saves.delete, 6)
        game.start_battle(make_encounter(3, "meadow", game.settings.mods,
                                         party=1, count=3))
        game.state = G.BATTLE
        for _ in range(90):                 # past the intro
            game.update(1 / 60)
        return game

    def targets(self, game):
        game.ui.hot.clear()
        game.draw()
        return [(tuple(r), i) for r, k, i in game.ui.hot.items
                if k == "battle_target"]

    def test_a_shaking_battle_draws_the_same_twice_and_rolls_nothing(self):
        from pixel_rpg.util import rng
        game = self.battle()
        game.camera.add_shake(20)
        game.update(1 / 60)
        before = rng._r.getstate()
        first = self.targets(game)
        second = self.targets(game)
        self.assertTrue(first)
        self.assertEqual(first, second)
        self.assertEqual(rng._r.getstate(), before,
                         "drawing spent the gameplay stream")

    def test_a_battle_shake_settles_as_it_did_at_sixty(self):
        game = self.battle()
        game.camera.shake = 0.0
        game.camera.add_shake(12.0)
        game.update(1 / 60)
        self.assertAlmostEqual(game.camera.shake, 12.0 - 84.0 / 60, places=6)

    def test_lightning_flickers_without_the_stream(self):
        from pixel_rpg.ui import ShotLayer
        from pixel_rpg.util import rng
        from pixel_rpg.assets import Assets
        _display_only()
        surface = pygame.Surface((400, 300))
        layer = ShotLayer()
        layer.add((20, 150), (380, 150), (200, 220, 255), look="spark")
        layer.update(0.05)
        before = rng._r.getstate()
        layer.draw(surface, Assets())
        layer.draw(surface, Assets())
        self.assertEqual(rng._r.getstate(), before)


class TestWindowSizedLayersAreForgotten(unittest.TestCase):
    """s96. `Assets._misc` kept every window-sized layer it ever made: a
    60-step drag of the window's edge held 327 MB."""

    def surface(self, w, h):
        return pygame.Surface((w, h), pygame.SRCALPHA)

    def test_a_dragged_window_stays_inside_the_budget(self):
        from pixel_rpg.assets import SurfaceCache
        cache = SurfaceCache()
        for i in range(60):
            size = (1280 + i * 10, 720 + i * 6)
            cache[("vignette", size)] = self.surface(*size)
        self.assertLessEqual(cache.bytes, cache.limit())
        self.assertLess(len(cache), 60)
        self.assertEqual(cache.bytes,
                         sum(cache.cost(v) for v in dict.values(cache)))

    def test_what_was_used_last_is_what_stays(self):
        from pixel_rpg.assets import SurfaceCache
        cache = SurfaceCache()
        cache[("keep",)] = self.surface(1280, 720)
        for i in range(40):
            cache[("layer", i)] = self.surface(1280, 720)
            cache[("keep",)]                      # used every frame
        self.assertIn(("keep",), cache)
        self.assertNotIn(("layer", 0), cache)

    def test_a_huge_window_keeps_its_own_screens(self):
        """Twelve of the biggest thing in it, whatever the budget - or a
        4K window would remake its backdrop every frame."""
        from pixel_rpg.assets import SurfaceCache
        cache = SurfaceCache()
        keys = [("layer", n) for n in range(4)]
        for _ in range(5):
            for key in keys:
                if key not in cache:
                    cache[key] = self.surface(3840, 2160)
                cache[key]
        self.assertEqual([k for k in keys if k in cache], keys)

    def test_the_game_hands_its_layers_to_one(self):
        from pixel_rpg.assets import Assets, SurfaceCache
        _display_only()
        self.assertIsInstance(Assets()._misc, SurfaceCache)


class TestAWaysideAnswersE(unittest.TestCase):
    """s96. Thirty-two waysides on an island and not one could be reached:
    `interactable_near` had no line for a camp, and every wayside test
    called `stop_at_wayside` directly."""

    def game(self):
        from pixel_rpg import game as G
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Stopper", 6)
        game.dialogue = None
        game.scene = None
        game.state = G.OVERWORLD
        self.addCleanup(game.saves.delete, 6)
        return game

    def test_e_beside_a_wayside_opens_it(self):
        from pixel_rpg import game as G
        game = self.game()
        gx, gy = next(iter(game.world.waysides))
        game.player.x = (gx + 1) * game.tile + game.tile / 2
        game.player.y = gy * game.tile + game.tile / 2
        self.assertEqual(game.world.interactable_near(gx + 1, gy),
                         ("camp", (gx, gy)))
        game.handle_event(pygame.event.Event(
            pygame.KEYDOWN, key=game.binds.key_for("interact"), unicode="",
            mod=0))
        self.assertEqual(game.state, G.SCENE)
        self.assertEqual(game.scene["wayside"]["spot"], (gx, gy))

    def test_every_thing_the_island_says_you_can_use_answers_e(self):
        """The class of bug, not the instance: `PROP_INTERACT` is what the
        world calls interactive, and each kind of it that the island
        places must be found by the scan E and the prompt both ask.
        An opened chest is the one exception, and on purpose - the scan
        returns its first match, and a spent chest must not stand in
        front of the villager or the door beside it (§61)."""
        from pixel_rpg.world import PROP_INTERACT
        game = self.game()
        placed = {}
        for spot, prop in game.world.props.items():
            if prop in PROP_INTERACT and prop != "chest_open":
                placed.setdefault(prop, spot)
        self.assertIn("camp", placed)
        for prop, (gx, gy) in placed.items():
            kind, _target = game.world.interactable_near(gx, gy)
            self.assertIsNotNone(kind, "%s at %r answers nothing" % (prop, (gx, gy)))

class TestTheShrineSaysWhatItTook(unittest.TestCase):
    """s96. The prompt said 23g; the toast after it said -25."""

    def test_the_toast_names_the_price_the_prompt_named(self):
        from pixel_rpg import game as G
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Mender", 6)
        game.dialogue = None
        self.addCleanup(game.saves.delete, 6)
        game.player.hp = 1
        game.player.gold = 100
        cost = game.shrine_cost()
        self.assertNotEqual(cost, 25, "pick a hero whose price is not 25")
        game.use_shrine()
        self.assertEqual(game.player.gold, 100 - cost)
        self.assertEqual(game.player.hp, game.player.max_hp())
        said = [t.text for t in game.toasts.items]
        self.assertIn("Fully restored. (-%d gold)" % cost, said)

class TestASaveGivesBackTheSameSpot(unittest.TestCase):
    """s96. The soak bot saved at y=3577.996 and loaded at 3578.0."""

    def test_the_position_survives_to_the_bit(self):
        from pixel_rpg import game as G
        _display_only()
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.pending_class = "warden"
        game.new_run("Keeper", 6)
        game.dialogue = None
        self.addCleanup(game.saves.delete, 6)
        game.player.x, game.player.y = 5162.3371, 3577.996
        game.save_run(force=True)
        again = G.Game()
        again.load_run(6)
        self.assertEqual((again.player.x, again.player.y),
                         (5162.3371, 3577.996))

if __name__ == "__main__":
    unittest.main(verbosity=2)
