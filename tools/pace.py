"""What the walk to each warden actually hands you.

Not a balance harness - `tools/balance.py` is that, and it answers "can
this class win this fight". This answers the question in front of it:
**walk from the village to a warden the way a player would, and what level
are you when you get there?**

Two routes, because they are different games:

* the **shortest** walk, which follows the roads where they help - the
  player who is trying to get there; and
* the same crossing **off the road** (`--offroad`), one tile to the side
  of every step - the player who wanders.

Roads spawn nothing (`TILES[T_PATH][2]` is 0.00), so the gap between the
two is the whole of what travel contributes.

Read the output as a floor, not as an experience. It does not fight a
lair, take a quest, open a chest or double back, and a real player does
all four.
"""
import os
import sys
from collections import deque

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
import tempfile
os.environ.setdefault("PIXEL_RPG_SAVE_DIR", tempfile.mkdtemp(prefix="rpgpace"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pixel_rpg.world import World, enemy_kin, TILES   # noqa: E402
from pixel_rpg.entities import BOSS_DEFS, Player, make_enemy  # noqa: E402
from pixel_rpg.util import rng                          # noqa: E402

OFFROAD = "--offroad" in sys.argv
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
SEEDS = [int(s) for s in (ARGS or ["4242", "17", "900"])]


def walkable(world, spot):
    """The game's own rule, not `blocked`.

    `World.blocked` counts a portal, a well and a fence as solid, which is
    right for walking into them and wrong for asking whether a place can
    be got to - by that rule every warden came out unreachable, because a
    warden *is* a blocking prop. `_reachable_from` is the authority and
    this matches it.
    """
    if not world.in_bounds(*spot):
        return False
    if TILES[world.grid[spot[1]][spot[0]]][1]:
        return False
    return world.props.get(spot) not in ("tree", "rock")


def route(world, start, end):
    """The shortest walk between two tiles, or None if there is none."""
    start, end = tuple(start), tuple(end)
    seen = {start: None}
    queue = deque([start])
    while queue:
        here = queue.popleft()
        if here == end:
            break
        gx, gy = here
        for step in ((gx + 1, gy), (gx - 1, gy), (gx, gy + 1), (gx, gy - 1)):
            if step in seen or not world.in_bounds(*step):
                continue
            if not walkable(world, step):
                continue
            seen[step] = here
            queue.append(step)
    if end not in seen:
        return None
    path, node = [], end
    while node is not None:
        path.append(node)
        node = seen[node]
    return list(reversed(path))


def beside(world, path):
    """The same walk, one tile off it wherever that is walkable."""
    out = []
    for (gx, gy) in path:
        for step in ((gx, gy + 1), (gx, gy - 1), (gx + 1, gy), (gx - 1, gy)):
            if walkable(world, step):
                out.append(step)
                break
        else:
            out.append((gx, gy))
    return out


for seed in SEEDS:
    world = World(32, seed).generate()
    print("\nseed %d" % seed)
    print("  %-16s %6s %7s %8s %9s %6s %s"
          % ("road", "steps", "fights", "xp", "level on", "boss", "verdict"))
    player = Player("Pacer", "warden")
    for portal, boss in zip(world.portals, BOSS_DEFS):
        path = route(world, world.spawn, (portal.gx, portal.gy))
        if path is None:
            print("  %-16s unreachable" % boss.key)
            continue
        walk = beside(world, path) if OFFROAD else path
        fights = xp = 0
        for (gx, gy) in walk:
            chance = world.encounter_chance(gx, gy)
            if chance and rng.chance(chance):
                fights += 1
                xp += make_enemy(world.suggested_level(gx, gy,
                                                       player.level),
                                 enemy_kin(world.biome_map[gy][gx])).xp_reward
        player.gain_xp(xp)
        gap = player.level - boss.level
        verdict = ("ready" if gap >= 0 else
                   "close" if gap >= -3 else "far under")
        print("  %-16s %6d %7d %8d %9d %6d  %s"
              % (boss.key, len(path), fights, xp, player.level, boss.level,
                 verdict))
