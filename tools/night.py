"""What a Long Night actually asks of you (§93).

`tools/balance.py --waves` has measured Last Stand since §60 and nothing
has ever measured this mode. That mattered less when a night spawned the
island's bestiary; **§91 gave it three archetypes of its own** - a
shambler that is slow and tough, a crawler that is fast and thin, a risen
that hits hardest - written by hand and shipped without anybody playing a
night at them.

    python tools/night.py
    python tools/night.py --nights 12 --difficulty nightmare

**This prints arithmetic, not a bot's opinion**, and that is a deliberate
correction. The first version of this file auto-played and reported *zero
nights survived at every difficulty*, which reads as "the mode is
impossible" and was three different bots being bad at it:

  * one backed away from everything and never swung;
  * one stood in reach and traded, which loses to three things at once;
  * one kited so carefully it never got back in time to hit anything.

Each rewrite moved the number and none of them moved the *game*. That is
§23's warning - a harness eventually measures a game nobody is playing -
so what is measured here is what the mode demands, which is a fact:

  * how long a night's crowd takes to put down, in seconds of swinging;
  * how long that crowd takes to put **you** down, if it reaches you;
  * how much faster you are than it, which is the whole of your answer.

**The speed ratio is the mode.** You outrun them by two and a half times;
every night is that ratio against those two clocks, and when the first
clock outgrows the second, the night is asking for something a player
cannot give.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PIXEL_RPG_SAVE_DIR",
                      tempfile.mkdtemp(prefix="rpgnight"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame                                             # noqa: E402
pygame.display.init()
pygame.font.init()

from pixel_rpg import freeroam as fr, gamemodes           # noqa: E402
from pixel_rpg.battle import compute_damage               # noqa: E402
from pixel_rpg.config import DIFFICULTY_MODS              # noqa: E402
from pixel_rpg.entities import Player, make_enemy         # noqa: E402
from pixel_rpg.util import rng                            # noqa: E402

#: How many monsters each row is averaged over. The prefix variants
#: alone swing a single roll by half, so one sample says nothing.
SAMPLES = 120


def hero_at(level, cls="warden"):
    """A hero who has **spent their points**, which §62 found the old
    harness was not doing - and it was the difference between 39% and
    80% on the same fight."""
    player = Player("Measured", cls)
    for _ in range(max(0, level - 1)):
        player.gain_xp(player.xp_to_next())
    for _ in range(player.points * 2):
        for stat in ("might", "vigor"):
            if player.points and player.attributes[stat] < 60:
                player.attributes[stat] += 1
                player.points -= 1
    player.full_restore()
    return player


def speed_ratio(cls="warden"):
    """How much faster a hero is than what walks at them."""
    arena = fr.FreeRoam(hero_at(1, cls))
    fastest = 1.0
    for _ in range(SAMPLES):
        foe = make_enemy(6, gamemodes.NIGHT_BIOME)
        body = fr.Body(foe, 0.0, 0.0, "enemy")
        fastest = max(fastest, arena.monster_speed(body))
    return fr.PLAYER_SPEED / fastest, fastest


def night_row(night, difficulty="normal", cls="warden"):
    """One night, as two clocks."""
    mods = DIFFICULTY_MODS.get(difficulty, DIFFICULTY_MODS["normal"])
    # A hero who has been levelling through the nights before this one.
    player = hero_at(max(1, 1 + int(night * 0.7)), cls)
    level = gamemodes.night_level(night)
    arrivals = gamemodes.night_count(night)
    crowd = gamemodes.night_crowd(night)

    kill_time = drain = 0.0
    for _ in range(SAMPLES):
        foe = make_enemy(level, gamemodes.NIGHT_BIOME, mods)
        swing = max(1.0, compute_damage(player, foe, power=1.0).amount)
        kill_time += foe.max_hp() / swing * fr.SWING_COOLDOWN
        touch = max(1.0, compute_damage(foe, player,
                                        power=fr.CONTACT_POWER).amount)
        drain += player.max_hp() / (touch / fr.HURT_GRACE)
    return {
        "night": night, "level": level, "arrivals": arrivals,
        "crowd": crowd, "hero": player.level, "hp": player.max_hp(),
        # Seconds of landed swings to put the whole night down.
        "clear": kill_time / SAMPLES * arrivals,
        # Seconds you last with a full crowd standing on you.
        "drained": drain / SAMPLES / max(1, crowd),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--nights", type=int, default=20)
    parser.add_argument("--class", dest="cls", default="warden")
    parser.add_argument("--difficulty", default="normal")
    args = parser.parse_args(argv)

    rng.reseed(4242)
    ratio, fastest = speed_ratio(args.cls)
    print("The Long Night - %s, %s" % (args.cls, args.difficulty))
    print("you outrun the quickest of them %.1fx (%.0f against %.0f)"
          % (ratio, fr.PLAYER_SPEED, fastest))
    print()
    print("%6s %5s %7s %6s %9s %10s %10s"
          % ("night", "lvl", "arrive", "crowd", "your lvl",
             "to clear", "to drain"))
    for night in range(1, args.nights + 1):
        if night > 6 and night % 2:
            continue
        row = night_row(night, args.difficulty, args.cls)
        room = row["drained"] * ratio
        flag = "  <-- asks more than it gives" if row["clear"] > room else ""
        print("%6d %5d %7d %6d %9d %9.0fs %9.0fs%s"
              % (row["night"], row["level"], row["arrivals"], row["crowd"],
                 row["hero"], row["clear"], row["drained"], flag))
    print()
    print("to clear = seconds of landed swings to put the whole night down")
    print("to drain = seconds you last with a full crowd touching you")
    print()
    print("The second number times the speed ratio is the room a player")
    print("has to work in. It closing is the mode getting harder; the")
    print("first outgrowing it is the mode asking for something nobody")
    print("can give.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
