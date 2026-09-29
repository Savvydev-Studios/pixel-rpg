"""House interiors: the room behind a door.

**Roadmap item 18** (2026-09-05, s56). Savvydev, 2026-08-24, when asked
whether the village should become somewhere you spend time rather than
somewhere you do business and leave:

> "yes house interior will be good."

Section 13 had recorded the absence as a deliberate scope decision -
*"there are no interiors to walk into"* - and section 11 called this the
most likely next feature if that decision were ever revisited. It has now
been revisited.

**A room is a `World`.** Not a new kind of map, not a screen: a bare
`World(tile, seed)` is already a valid, empty, walkable world - grass
everywhere, no chests, no portals, no monsters - so an interior is one of
those with a room painted into it. Every line of drawing, movement,
collision and interaction the overworld already has works on it unchanged,
and there is no second renderer to drift out of step with the first. The
alternative was an `Interior` class reimplementing about fifteen methods
of `World`'s surface, every one of them a chance to disagree.

**Nothing is in here to take.** No chests, ever: the number of chests in
the world is what `THE LONG WALK` costs (s42), and a chest indoors would
either reprice an ending or be a chest the Hollow Key cannot count. A room
is somewhere to be, not somewhere to loot.

**It regenerates rather than persisting.** The room is built from the
world seed and the door's own coordinates, so the same door opens the same
room every time without a byte of it being saved (hard rule 4, applied one
level down). Saving while indoors writes you standing outside the door -
see `Game.save_run` - which is why none of this needs a save-format
change.
"""
from __future__ import annotations

from .util import RNG
from .world import T_STONE, T_WALL, T_WOOD, WORLD_H, WORLD_W, World

# **How big a room is, in tiles, including its walls** - now a range.
#
# §56 fixed this at 11x8 with a reason worth keeping: *"a house on the
# overworld is four tiles across, and an interior four times that size
# would be a lie you can walk around in"*. Savvydev asked for bigger on
# 2026-09-05 (0af), so the floor moved up rather than the ceiling coming
# off - the smallest room now is half again the old one, and the largest
# is about four times it. The lie is his to price.
MIN_W, MAX_W = 15, 21
MIN_H, MAX_H = 11, 15

# **The plans.** §56 furnished one fixed rectangle and rolled two or three
# ornaments into it, which is why every house read as the same house.
# The *shape* is rolled now, and the furniture is rolled against the shape.
PLANS = ("open", "split", "ell", "alcove", "pillars")

# **One room in this many is stacked** (0af). Savvydev: *"add a super rare
# house interior that is absolutely STACKED bro"*. Twenty-two doors exist
# on the island, so at 1-in-18 most seeds have one and some have none -
# which is what makes finding one an event rather than a stop on a route.
HOARD_ONE_IN = 18


def _rng_for(world_seed, door):
    """The private stream a room is built from.

    **Private, never the shared one.** §56 records why and §21 paid for it:
    a wasted `rng.choice` on the shared stream moved four classes in the
    balance table. Furnishing a room happens whenever somebody opens a
    door, which is the worst possible time to disturb a run's stream.
    """
    return RNG(room_seed(world_seed, door))


def plan_for(world_seed, door):
    """What is behind this door, decided before anything is built.

    Separated from `build` so a test, and the codex line, can ask what a
    door is without constructing a `World` for it.
    """
    rng = _rng_for(world_seed, door)
    width = rng.rint(MIN_W, MAX_W)
    height = rng.rint(MIN_H, MAX_H)
    shape = rng.choice(PLANS)
    stacked = rng.rint(1, HOARD_ONE_IN) == 1
    return {"w": width, "h": height, "shape": shape, "stacked": stacked}


def room_seed(world_seed, door):
    """One number per door, so a house is itself every time you open it."""
    gx, gy = door
    return (int(world_seed or 0) * 7919 + gx * 131 + gy * 17) & 0x7FFFFFFF


def build(world_seed, door, tile=32):
    """The room behind `door`, as a `World` nobody has generated.

    Returns `(room, spawn, exit_tile)` - the world, where the player
    stands on arriving, and the tile that takes them back out.

    **Everything rolled here comes from one seeded stream, in the same
    order every time.** That is what makes the same door open the same
    room on two machines with no room state sent between them (s57), so
    nothing in here may branch on anything but the seed and the door.
    """
    plan = plan_for(world_seed, door)
    room_w, room_h = plan["w"], plan["h"]
    # `plan_for`'s rolls are already spent, so furnishing continues from a
    # fresh stream of the same seed, skipping past them. Re-derived rather
    # than passed in, so `plan_for` stays answerable on its own.
    rng = _rng_for(world_seed, door)
    for _ in range(4):
        rng.rint(0, 1)

    # `generate=False` is load-bearing: see `World.__init__`. Calling the
    # generator here would raise a whole island to paint over and would
    # reseed the shared RNG while the player is mid-run.
    room = World(tile, room_seed(world_seed, door), generate=False)

    # **The middle of the grid, because the camera clamps to the world's
    # edges** - s56 hit this: in a corner the camera cannot centre and the
    # room draws top-left with two thirds of the window empty.
    room_x = WORLD_W // 2 - room_w // 2
    room_y = WORLD_H // 2 - room_h // 2
    # Where the room is, so the camera can frame it rather than the player
    # standing in its doorway (§95). Not saved - a room never is.
    room.room_bounds = (room_x, room_y, room_w, room_h)

    # **Everything that is not the room is wall.** A `World` starts as
    # grass edge to edge, so without this the house is a room standing in
    # a meadow - and the minimap drew it that way too.
    for row in room.grid:
        for x in range(len(row)):
            row[x] = T_WALL
    for y in range(room_y, room_y + room_h):
        for x in range(room_x, room_x + room_w):
            edge = (y in (room_y, room_y + room_h - 1)
                    or x in (room_x, room_x + room_w - 1))
            # Stone underfoot: the game has no dedicated floor tile and
            # `T_WOOD` is the doorway, so a boarded floor would make the
            # way out invisible against it.
            room.grid[y][x] = T_WALL if edge else T_STONE

    # The way out, in the middle of the south wall, so it is where the
    # door you came through would be if the house had a back.
    out_x = room_x + room_w // 2
    out_y = room_y + room_h - 1
    room.grid[out_y][out_x] = T_WOOD
    room.doors[(out_x, out_y)] = "out"
    spawn = (out_x, out_y - 1)

    _lay_out(room, plan, rng, room_x, room_y, room_w, room_h, spawn, out_x)
    _furnish(room, plan, rng, room_x, room_y, room_w, room_h, spawn,
             (out_x, out_y))
    return room, spawn, (out_x, out_y)


def _wall(room, x, y, keep_clear):
    """Raise one interior wall tile, unless it would seal the way out."""
    if (x, y) in keep_clear:
        return
    room.grid[y][x] = T_WALL


def _lay_out(room, plan, rng, rx, ry, rw, rh, spawn, out_x):
    """Put the interior walls in, according to the plan.

    **Nothing here may block the way out.** Every shape is measured
    against the door column and the tile you spawn on, and `_reachable` is
    the backstop - a room you cannot leave is the one bug in here that
    ends a run.
    """
    shape = plan["shape"]
    keep_clear = {spawn, (out_x, ry + rh - 1), (out_x, ry + rh - 2)}
    inner_x = range(rx + 1, rx + rw - 1)

    if shape == "split":
        # A partition across the room with one doorway in it, so the house
        # has a front and a back.
        wall_y = ry + rh // 2
        options = [x for x in inner_x if abs(x - out_x) > 1]
        gap = rng.choice(options) if options else out_x
        for x in inner_x:
            if x != gap:
                _wall(room, x, wall_y, keep_clear)
    elif shape == "ell":
        # A solid block in one of the two far corners.
        left = rng.chance(0.5)
        block_w = max(2, rw // 4)
        block_h = max(2, rh // 4)
        x0 = rx + 1 if left else rx + rw - 1 - block_w
        for y in range(ry + 1, ry + 1 + block_h):
            for x in range(x0, x0 + block_w):
                _wall(room, x, y, keep_clear)
    elif shape == "alcove":
        # A small back room off the far wall, open in the middle.
        width = max(3, rw // 3)
        x0 = rng.rint(rx + 1, rx + rw - 1 - width)
        wall_y = ry + max(2, rh // 3)
        for x in range(x0, x0 + width):
            if x != x0 + width // 2:
                _wall(room, x, wall_y, keep_clear)
    elif shape == "pillars":
        for y in range(ry + 2, ry + rh - 2, 3):
            for x in range(rx + 2, rx + rw - 2, 4):
                _wall(room, x, y, keep_clear)
    # "open" is the fifth plan and lays nothing down, on purpose: a plain
    # rectangle has to stay one of the possibilities, or the variety reads
    # as every house being strange.

    if not _reachable(room, spawn, rx, ry, rw, rh):
        # Not reachable with the shapes above, and not worth trusting: a
        # sealed room cannot be left. s7's rule about a fallback nobody
        # exercises is why a test drives every plan at every size as well.
        for y in range(ry + 1, ry + rh - 1):
            for x in range(rx + 1, rx + rw - 1):
                room.grid[y][x] = T_STONE


def _reachable(room, spawn, rx, ry, rw, rh):
    """Flood from the spawn; every floor tile has to be in it."""
    floor = {(x, y)
             for y in range(ry + 1, ry + rh - 1)
             for x in range(rx + 1, rx + rw - 1)
             if room.grid[y][x] != T_WALL}
    if spawn not in floor:
        return False
    seen, stack = {spawn}, [spawn]
    while stack:
        x, y = stack.pop()
        for step in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = (x + step[0], y + step[1])
            if nxt in floor and nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen == floor


# The hearth, the bed and the table are what make a room read as somewhere
# somebody lives, so they are always in it. The rest is what makes no two
# the same. `shelf` and `barrel` repeat to weight them above the
# ornaments, which were the whole of the old furniture list.
STAPLES = ("hearth", "bed", "table")
EXTRAS = ("shelf", "shelf", "barrel", "barrel", "rug", "lantern", "flower")


def _furnish(room, plan, rng, rx, ry, rw, rh, spawn, way_out):
    """Fill the room, rolled against its shape.

    **No chests, ever** (s42, s56): the number of chests in the world is
    what `THE LONG WALK` costs and what the Hollow Key counts, so one
    indoors would either reprice an ending or be a chest the key can never
    find. The stacked room below is loot and is deliberately not a chest
    for exactly that reason.
    """
    free = [(x, y)
            for y in range(ry + 1, ry + rh - 1)
            for x in range(rx + 1, rx + rw - 1)
            if room.grid[y][x] != T_WALL
            and (x, y) != spawn
            and not (x == way_out[0] and abs(y - way_out[1]) <= 1)]
    rng.shuffle(free)

    for staple in STAPLES:
        if free:
            room.props[free.pop()] = staple

    # A bigger room gets more in it, so a 21x15 hall does not read as the
    # small room's furniture adrift in a field.
    area = (rw - 2) * (rh - 2)
    for _ in range(rng.rint(area // 24, area // 12)):
        if not free:
            break
        room.props[free.pop()] = rng.choice(EXTRAS)

    if plan["stacked"]:
        _stack_it(room, rng, free, ry)
    _unseal(room, spawn, rx, ry, rw, rh)


def _unseal(room, spawn, rx, ry, rw, rh):
    """Take out any solid furniture that closed part of the room off.

    `shelf` and `barrel` are in `world.BLOCKING_PROPS`, so a pair of them
    dropped in the wrong two squares of a `pillars` plan can wall a corner
    off - or, in the worst case, the way out. **A room you cannot leave
    ends a run**, and the placement is random, so this is checked rather
    than reasoned about: drop solid props until everything is reachable
    again. Almost always a no-op; a test drives every plan at every size.
    """
    from .world import BLOCKING_PROPS
    for _ in range(64):
        blocked = {spot for spot, prop in room.props.items()
                   if prop in BLOCKING_PROPS}
        floor = {(x, y)
                 for y in range(ry + 1, ry + rh - 1)
                 for x in range(rx + 1, rx + rw - 1)
                 if room.grid[y][x] != T_WALL and (x, y) not in blocked}
        if spawn not in floor:
            room.props.pop(spawn, None)
            continue
        seen, stack = {spawn}, [spawn]
        while stack:
            x, y = stack.pop()
            for step in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (x + step[0], y + step[1])
                if nxt in floor and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        stranded = floor - seen
        if not stranded:
            return
        # Open the way to the nearest stranded tile by removing whichever
        # solid prop sits between it and the reachable part.
        for spot in sorted(stranded):
            for step in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                door = (spot[0] + step[0], spot[1] + step[1])
                if door in blocked:
                    room.props.pop(door, None)
                    break
            else:
                continue
            break
        else:
            # Nothing solid to remove - the walls did it, and `_lay_out`
            # already proved they do not.
            return


def _stack_it(room, rng, free, ry):
    """The rare one. Savvydev, 2026-09-05: *"absolutely STACKED bro"*.

    **The hoard is one prop, not a floor of them.** Everything else in any
    interior is scenery; this is the only thing in one the player can
    take. `Game.take_hoard` decides what it is worth, and it is recorded
    per save so a room cannot be walked out of and back into for another.

    It is also **per player in co-op, not arbitrated by the host** - the
    same call s20 makes for warden kills and story flags. Being handed
    somebody else's discovery removes the discovery rather than sharing
    it.
    """
    if not free:
        return
    # Against the far wall, where it is seen on the way in rather than
    # stood on. Anywhere free, in a room shaped oddly enough to have
    # nothing along the back.
    back = [spot for spot in free if spot[1] <= ry + 2]
    spot = rng.choice(back) if back else free[-1]
    room.props[spot] = "hoard"
    # Lanterns either side, so the one room worth finding looks like it
    # before you have read anything.
    for dx in (-1, 1):
        beside = (spot[0] + dx, spot[1])
        if beside in free and room.props.get(beside) is None:
            room.props[beside] = "lantern"


# **What the room says when you walk in.** One line, once, as a notice
# rather than a dialogue - the door already cost you a keypress and a
# second one to dismiss a box saying "it is a room" would be a toll.
#
# Chosen by the door, so a house says the same thing every time. Written in
# the hamlets' register (s44): dry, short, and about somebody who is not
# here, because they are outside where you met them.
ROOM_NOTES = (
    "Somebody lives here. The fire is banked, not out.",
    "One bowl on the table, washed and put back.",
    "The bed is made. Badly, but made.",
    "It smells of woodsmoke and wet wool.",
    "Boots by the door, still muddy.",
    "A chair pulled out and not pushed back in.",
    "Warm. Whoever banked that fire meant to come back.",
    "Tidy in the way a room is when one person lives in it.",
)


def room_note(door):
    gx, gy = door
    return ROOM_NOTES[(gx * 3 + gy * 5) % len(ROOM_NOTES)]
