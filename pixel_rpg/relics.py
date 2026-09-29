"""Relics, and the six Memories.

A Memory is what a warden leaves behind. Mechanically it is a permanent
passive; narratively it is a page of that person's life that you now carry
because you killed them for it. The two facts are meant to sit uncomfortably
together, so the strongest Memory in the game (Ysolde's) is also the one that
costs you something.

Ordinary relics drop from chests and New Game+ tiers and carry no such freight.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import security as sec


@dataclass(frozen=True)
class Relic:
    key: str
    name: str
    flavour: str
    mult: dict = field(default_factory=dict)    # stat -> multiplier
    flat: dict = field(default_factory=dict)    # stat -> flat bonus
    tags: tuple = ()                            # behaviour flags
    memory: bool = False                        # is this a warden Memory
    color: tuple = (206, 206, 216)


MEMORIES = [
    Relic("memory_callun", "Callun's Count",
          "Four hundred thousand and rising. He never lost his place.",
          mult={"xp": 1.18}, flat={"spd": 4},
          tags=("xp_up",), memory=True, color=(118, 220, 138)),
    Relic("memory_dorn", "Dorn's Cornerstone",
          "One stone a year, dressed true. The only part he chose.",
          mult={"defense": 1.14}, flat={"hp": 30},
          tags=("stun_immune",), memory=True, color=(156, 162, 178)),
    Relic("memory_vell", "Vell's Blindfold",
          "She arranged her own mercy. Nobody else was going to.",
          mult={"crit": 1.35, "res": 1.15}, flat={},
          tags=("blind_sight",), memory=True, color=(104, 84, 140)),
    Relic("memory_kessa", "Kessa's Ember",
          "Still warm in places. Eleven days of burning will do that.",
          mult={"mag": 1.16}, flat={"atk": 6},
          tags=("fire_up", "burn_immune"), memory=True, color=(250, 128, 52)),
    Relic("memory_ilan", "Ilan's Last Letter",
          "Only a name, written over and over, to keep the sound of it.",
          mult={"mp": 1.25}, flat={"res": 6},
          tags=("mending",), memory=True, color=(140, 96, 58)),
    Relic("memory_ysolde", "Ysolde's Grip",
          "Six hundred years of holding on. It does not let go easily.",
          mult={"atk": 1.20, "mag": 1.20, "hp": 0.90}, flat={},
          tags=("first_hold",), memory=True, color=(168, 108, 226)),
]

COMMON_RELICS = [
    Relic("roadstone", "Roadstone",
          "Warm underfoot. Points the way to money.",
          mult={"gold": 1.20}, tags=("gold_up",), color=(250, 200, 96)),
    Relic("frostward", "Frostward Charm",
          "The cold notices you slightly less.",
          mult={"res": 1.18}, flat={"hp": 20}, color=(130, 214, 240)),
    Relic("pathfinder", "Pathfinder's Boots",
          "Worn thin by somebody who kept going.",
          mult={"spd": 1.22}, tags=("flee_up",), color=(140, 96, 58)),
    Relic("glutton", "Bottomless Flask",
          "Whatever you pour in comes out as more.",
          tags=("potion_up",), color=(226, 96, 106)),
    Relic("haggler", "Haggler's Ledger",
          "Every column adds up in your favour. Suspicious.",
          tags=("sell_up",), color=(206, 168, 40)),
    Relic("echo", "Echo Stone",
          "Sometimes the road casts the spell for you.",
          tags=("free_cast",), color=(114, 224, 232)),
    Relic("keen", "Whetted Fang",
          "It has been sharpened far more times than is sensible.",
          mult={"crit": 1.25, "atk": 1.08}, color=(226, 74, 66)),
    Relic("bulwarkstone", "Sunken Bulwark",
          "Heavy. Reassuring. Slightly resentful.",
          mult={"defense": 1.20, "spd": 0.94}, flat={"hp": 40},
          color=(108, 164, 252)),
]

ALL_RELICS = MEMORIES + COMMON_RELICS
RELICS_BY_KEY = {r.key: r for r in ALL_RELICS}
MEMORY_KEYS = [r.key for r in MEMORIES]


def get(key):
    return RELICS_BY_KEY.get(key)


def resolve(keys):
    """Validated relic objects for a list of possibly-untrusted keys."""
    out = []
    for key in sec.safe_list(keys, 32):
        relic = RELICS_BY_KEY.get(sec.safe_str(key, 32))
        if relic and relic not in out:
            out.append(relic)
    return out


def stat_multiplier(relic_list, stat):
    total = 1.0
    for relic in relic_list:
        total *= relic.mult.get(stat, 1.0)
    return total


def stat_flat(relic_list, stat):
    return sum(relic.flat.get(stat, 0) for relic in relic_list)


def has_tag(relic_list, tag):
    return any(tag in relic.tags for relic in relic_list)


def memories_held(relic_list):
    return [r for r in relic_list if r.memory]
