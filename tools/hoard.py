"""What a hoard run is worth, auto-played (§87).

The mode's design claim is that *how far out do I go before I turn back*
is a real decision. That is only true if the best answer is somewhere in
the middle - if carrying twenty always wins, the slowdown is decoration,
and if carrying three always wins, so is the bonus.

**This is the instrument that says which.** A bot walks to the nearest
coin until it is carrying `threshold`, then walks home, swinging at
whatever is in the way; the table is what several thresholds bank.

    python tools/hoard.py

**It found the mode's missing half.** The first run of this printed a
flat win for *carry 3* at every level - greed cost speed and paid
nothing - which is what `hoard_trip_value` exists to fix. After it, the
best policy is *carry 14* and both ends of the table score less.

Like `tools/balance.py`, this **plays the real game**: `Game.update`,
the real arena, the real monsters. A harness that reimplemented the
walk would eventually measure a game nobody is playing (§23).
"""
import math
import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpghoardplay")
sys.path.insert(0, os.path.abspath("."))

import pygame                                             # noqa: E402
pygame.display.init()
pygame.font.init()

from pixel_rpg import game as G, gamemodes                 # noqa: E402
from pixel_rpg.util import rng                             # noqa: E402


def play(threshold, seed, level=8, difficulty="normal"):
    """One run. The bot walks to the nearest coin until it is carrying
    `threshold`, then walks home. It swings at anything in the way."""
    rng.reseed(seed)
    game = G.Game()
    game.screen = pygame.display.set_mode((640, 360))
    game.settings.difficulty = difficulty
    game.pending_class = "warden"
    game.new_hoard_run("Bot")
    for _ in range(level - 1):
        game.player.gain_xp(game.player.xp_to_next())
    game.player.full_restore()

    dt = 1 / 30.0
    frames = int(gamemodes.HOARD_TIME / dt) + 60
    for _ in range(frames):
        if game.state != G.ROAM or game.roam is None:
            break
        fight = game.roam
        hero = fight.hero(0)
        if hero is None or hero.dead:
            game.update(dt)
            continue
        if hero.carrying >= threshold or not fight.loot:
            goal = fight.bank
        else:
            goal = min(((c.x, c.y) for c in fight.loot),
                       key=lambda p: math.hypot(p[0] - hero.x, p[1] - hero.y))
        dx, dy = goal[0] - hero.x, goal[1] - hero.y
        gap = math.hypot(dx, dy) or 1.0
        # Swing when something is close enough to be in the way.
        near = any(math.hypot(f.x - hero.x, f.y - hero.y) < 46.0
                   for f in fight.living_foes)
        fight.input(0, dx / gap, dy / gap, swing=near, dt=dt)
        game.update(dt)
    run = game.hoard
    return {"banked": run.banked, "score": run.score(),
            "falls": run.falls, "kills": run.kills, "lost": run.lost}


def main():
    print("The Hoard, %d seconds, level 8 warden, normal"
          % int(gamemodes.HOARD_TIME))
    print("%-8s %8s %8s %7s %7s" % ("carry", "banked", "score", "falls",
                                    "kills"))
    best = None
    for threshold in (1, 3, 5, 7, 10, 14, 20, 30):
        runs = [play(threshold, 1000 + n) for n in range(8)]
        banked = sum(r["banked"] for r in runs) / len(runs)
        score = sum(r["score"] for r in runs) / len(runs)
        falls = sum(r["falls"] for r in runs) / len(runs)
        kills = sum(r["kills"] for r in runs) / len(runs)
        print("%-8d %8.1f %8.0f %7.1f %7.1f"
              % (threshold, banked, score, falls, kills))
        if best is None or score > best[1]:
            best = (threshold, score)
    print()
    print("best threshold: carry %d (%.0f)" % best)


if __name__ == "__main__":
    main()
