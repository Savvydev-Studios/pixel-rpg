"""Fuzz the save loader.

Takes a valid save, mutates it thousands of ways, and asserts the loader never
raises an uncaught exception and never produces an out-of-range player. A save
file is the one attacker-controlled input this game has; it should be
impossible to crash the game by editing one.
"""
import json
import os
import random
import string
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame

from pixel_rpg import security as sec
from pixel_rpg.config import Settings
from pixel_rpg.entities import MAX_LEVEL, Player
from pixel_rpg.items import roll_item
from pixel_rpg.quests import QuestLog
from pixel_rpg import save as save_mod
from pixel_rpg.save import SaveManager, build_payload
from pixel_rpg.world import World

WEIRD = [
    None, True, False, 0, -1, 10 ** 15, -10 ** 15, 1e308, -1e308,
    float("inf"), float("-inf"), 3.14, "", " ", "x" * 300, "../../etc/passwd",
    "\x00\x01\x02", "NaN", "1e999", [], {}, [1, 2, 3], {"a": {"b": {"c": 1}}},
    {"__class__": "evil"}, "<script>alert(1)</script>", "'; DROP TABLE --",
    "%s%s%s%n", "﻿", "godmode",
]


def random_value(rng, depth=0):
    if depth > 2 or rng.random() < 0.6:
        return rng.choice(WEIRD)
    if rng.random() < 0.5:
        return [random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {"".join(rng.choices(string.ascii_letters, k=rng.randint(1, 8))):
            random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def mutate(rng, node, depth=0):
    """Randomly replace, delete or corrupt nodes anywhere in the tree."""
    if depth > 4:
        return node
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            roll = rng.random()
            if roll < 0.08:
                continue                       # delete the key
            if roll < 0.20:
                out[key] = random_value(rng)   # replace the value
            elif roll < 0.26:
                out["".join(rng.choices(string.ascii_letters, k=6))] = \
                    random_value(rng)          # inject an unknown key
                out[key] = mutate(rng, value, depth + 1)
            else:
                out[key] = mutate(rng, value, depth + 1)
        return out
    if isinstance(node, list):
        if rng.random() < 0.1:
            return random_value(rng)
        return [mutate(rng, v, depth + 1) for v in node[:20]]
    if rng.random() < 0.18:
        return random_value(rng)
    return node


def run(iterations=3000, seed=20260819):
    __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
    rng = random.Random(seed)
    tmp = tempfile.mkdtemp(prefix="rpgfuzz")
    mgr = SaveManager(tmp)
    settings = Settings.from_env()

    player = Player("Fuzz")
    player.gain_xp(5000)
    for slot in ("weapon", "armor", "accessory"):
        item = roll_item(10, slot)
        player.add_item(item)
        player.equip(item)
    for _ in range(8):
        player.add_item(roll_item(12))
    world = World(32, 4242)
    base = build_payload(player, world, QuestLog(), settings, playtime=10.0)

    crashes, loaded, rejected = [], 0, 0
    for i in range(iterations):
        # The rate limiter would (correctly) stop us after five bad loads,
        # so it is cleared each round; this targets the parser, not the limit.
        save_mod._decode_limiter.reset()

        payload = mutate(rng, json.loads(json.dumps(base)))
        path = mgr.slot_path(1)
        try:
            text = json.dumps(payload)
        except (TypeError, ValueError):
            continue
        if rng.random() < 0.12:                # sometimes truncate the file
            text = text[:rng.randint(0, max(1, len(text)))]
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            continue

        try:
            result = mgr.load_into(1, lambda s: World(32, s), settings)
        except Exception as exc:               # noqa: BLE001 - that is the point
            crashes.append((i, type(exc).__name__, str(exc)[:140]))
            continue
        if result is None:
            rejected += 1
            continue
        loaded += 1
        p = result[0]
        # **Against the constant.** The ceiling moved to 100 on
        # 2026-08-29 and this literal did not - the fuzzer was the only
        # gate that noticed, which is exactly what it is for.
        assert 1 <= p.level <= MAX_LEVEL, ("level", p.level, i)
        assert 0 < p.hp <= p.max_hp(), ("hp", p.hp, p.max_hp(), i)
        assert 0 <= p.mp <= p.max_mp(), ("mp", p.mp, i)
        assert 0 <= p.gold <= 99_999_999, ("gold", p.gold, i)
        assert len(p.inventory) <= sec.MAX_INVENTORY, ("bag", i)
        assert all(0 <= v <= 300 for v in p.attributes.values()), ("attrs", i)
        for item in p.inventory:
            assert item.slot in ("weapon", "armor", "accessory")
            assert all(k in ("atk", "defense", "hp", "mp", "mag", "res", "spd",
                             "crit", "crit_dmg", "lifesteal")
                       for k in item.stats), ("stat key", item.stats, i)

    pygame.quit()
    print("fuzz: %d iterations | %d loaded | %d rejected | %d crashes"
          % (iterations, loaded, rejected, len(crashes)))
    for entry in crashes[:10]:
        print("  crash #%d %s: %s" % entry)
    return 1 if crashes else 0


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    sys.exit(run(count))
