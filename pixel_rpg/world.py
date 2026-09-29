"""World generation, tiles, props and the camera.

The original map was one screen of 40x22 tiles. This one is 112x76 tiles of
biome-partitioned terrain with a town, roads, treasure, NPCs and six boss
arenas, viewed through a scrolling camera.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import pygame

from .entities import BOSS_DEFS, FIELD_BOSSES
from .util import MAX_LEVEL, RNG, clamp, fx_rng, rng

# **The valley, doubled in each direction** (s42, roadmap item 12).
# Savvydev: *"for the map, make it huge bro, huge."* Four times the area,
# which is what makes room for six waystations on six roads without the
# roads becoming a corridor of signposts.
#
# **Nothing is stored, so this costs no save space** - terrain regenerates
# from the seed (hard rule 4) and a bigger world is still one integer.
WORLD_W, WORLD_H = 336, 228

# What the numbers below were tuned against. Every absolute distance in
# this file - portal radii, runner posts, how far a chest has to be from
# the doorstep - was picked on a 112-wide map, so they are expressed as a
# fraction of it rather than re-guessed. Growing the map again means
# changing the two numbers above and nothing else.
_TUNED_W = 112.0
MAP_SCALE = WORLD_W / _TUNED_W


def _scaled(distance):
    """A distance tuned on the old map, in tiles on this one."""
    return int(round(distance * MAP_SCALE))

# id -> (texture name, blocks movement, encounter chance per new tile)
TILES = {
    0:  ("grass", False, 0.00),
    1:  ("tallgrass", False, 0.15),
    2:  ("path", False, 0.00),
    3:  ("water", True, 0.00),
    4:  ("wall", True, 0.00),
    5:  ("sand", False, 0.04),
    6:  ("snow", False, 0.11),
    7:  ("ice", False, 0.07),
    8:  ("ash", False, 0.13),
    9:  ("lava", True, 0.00),
    19: ("heath", False, 0.13),
    20: ("marsh", False, 0.17),
    21: ("mire", False, 0.10),
    22: ("clay", False, 0.09),
    23: ("pine", False, 0.14),
    24: ("obsidian", False, 0.12),
    25: ("salt", False, 0.08),
    26: ("mountain", True, 0.00),
    27: ("rubble", True, 0.00),
    10: ("cave", False, 0.14),
    11: ("stone", False, 0.00),
    12: ("wood", False, 0.00),
    13: ("shop", True, 0.00),
    14: ("shrine", True, 0.00),
    15: ("arena", False, 0.00),
    16: ("roof", True, 0.00),
    17: ("roof2", True, 0.00),
    18: ("thatch", True, 0.00),
}

T_GRASS, T_TALL, T_PATH, T_WATER, T_WALL = 0, 1, 2, 3, 4
T_SAND, T_SNOW, T_ICE, T_ASH, T_LAVA = 5, 6, 7, 8, 9
T_CAVE, T_STONE, T_WOOD, T_SHOP, T_SHRINE, T_ARENA = 10, 11, 12, 13, 14, 15
T_ROOF, T_ROOF2, T_THATCH = 16, 17, 18
# The regional grounds (s42). Difficulty still rises with distance; these
# are what makes two places at the *same* distance feel different, which
# is the half concentric rings could never do.
T_HEATH, T_MARSH, T_MIRE, T_CLAY = 19, 20, 21, 22
T_PINE, T_OBSIDIAN, T_SALT = 23, 24, 25
# **Rock you cannot cross.** Savvydev, 2026-08-29: *"add like terrain you
# cant go over and mountains you have to navigate and stuff"*. It is the
# other half of taking the roads away - a direction to walk in is only
# interesting if the ground argues with you.
T_MOUNTAIN = 26
# **Pale broken stone, for ruins.** They were drawn in `T_WALL`, which is
# near-black and vanished into the ash and the mire - a ruin the player
# cannot pick out of the background is not a landmark, it is texture.
T_RUBBLE = 27
ROOFS = (T_ROOF, T_ROOF2, T_THATCH)


@dataclass(frozen=True)
class Biome:
    key: str
    label: str
    ground: int
    brush: int          # the tile that spawns encounters
    hazard: int         # blocking terrain (water, lava)
    props: tuple
    tint: tuple         # ambient colour wash
    level_bias: int
    # **Whose monsters live here.** A sibling biome is a different place at
    # the same difficulty, not a different bestiary - the pines borrow the
    # wood's animals, the flats borrow the reach's. Without this an unknown
    # biome falls through to *every* archetype, and a marsh spawns imps.
    kin: str = ""


BIOMES = {
    b.key: b for b in (
        Biome("meadow", "Verdant Meadow", T_GRASS, T_TALL, T_WATER,
              ("tree", "flower", "rock"), (255, 255, 250), 0),
        Biome("forest", "Whispering Wood", T_GRASS, T_TALL, T_WATER,
              ("tree", "tree", "rock", "flower"), (215, 240, 220), 2),
        Biome("cave", "Sunken Hollow", T_CAVE, T_CAVE, T_WATER,
              ("rock", "rock"), (190, 190, 215), 5),
        Biome("tundra", "Frostreach", T_SNOW, T_SNOW, T_ICE,
              ("rock", "tree"), (225, 240, 255), 9),
        Biome("volcano", "Cinder Wastes", T_ASH, T_ASH, T_LAVA,
              ("rock", "rock"), (255, 215, 200), 14),
        Biome("wastes", "The Hollow Reach", T_SAND, T_SAND, T_WATER,
              ("rock",), (245, 235, 215), 20),
        # --- the siblings ------------------------------------------------
        # One per tier, sharing its difficulty and nothing else. A player
        # crossing the map at a fixed distance from town should pass
        # through different country, not the same ring twice.
        Biome("heath", "The Long Heath", T_HEATH, T_TALL, T_WATER,
              ("rock", "flower"), (252, 250, 236), 0, kin="meadow"),
        Biome("marsh", "Sodden Mile", T_MARSH, T_MARSH, T_WATER,
              ("tree", "rock"), (206, 232, 214), 2, kin="forest"),
        Biome("mire", "The Black Mire", T_MIRE, T_MARSH, T_WATER,
              ("tree", "tree", "rock"), (196, 206, 198), 5, kin="cave"),
        Biome("clay", "Redcut Canyon", T_CLAY, T_SAND, T_WATER,
              ("rock", "rock"), (250, 218, 198), 9, kin="wastes"),
        Biome("pine", "The Black Pines", T_PINE, T_SNOW, T_ICE,
              ("tree", "tree", "rock"), (214, 236, 232), 9, kin="forest"),
        Biome("obsidian", "Glasswake", T_OBSIDIAN, T_ASH, T_LAVA,
              ("rock",), (226, 210, 240), 14, kin="volcano"),
        Biome("salt", "The White Flats", T_SALT, T_SAND, T_WATER,
              ("rock",), (250, 248, 240), 20, kin="wastes"),
    )
}

# **Which biomes sit at the same distance, and therefore the same
# difficulty.** Enemies, field bosses and the level curve all key off
# this: a sibling is a different *place*, never a different tier.
BIOME_TIERS = {
    "meadow": 0, "heath": 0,
    "forest": 1, "marsh": 1,
    "cave": 2, "mire": 2,
    "tundra": 3, "clay": 3, "pine": 3,
    "volcano": 4, "obsidian": 4,
    "wastes": 5, "salt": 5,
}
TIER_ORDER = [[k for k, t in BIOME_TIERS.items() if t == tier]
              for tier in range(max(BIOME_TIERS.values()) + 1)]


def biome_tier(biome):
    return BIOME_TIERS.get(biome, 0)


def enemy_kin(biome):
    """The biome whose bestiary this one borrows. Itself, usually."""
    entry = BIOMES.get(biome)
    return (entry.kin or biome) if entry else biome


def siblings_of(biome):
    """Every biome at the same difficulty as this one, itself included."""
    return tuple(TIER_ORDER[biome_tier(biome)])

BLOCKING_PROPS = {"tree", "rock", "shrine", "portal", "well", "fence",
                  "lantern", "sign", "marker", "camp",
                  # Interior furniture (0af, s58). A bookshelf you can
                  # walk through reads worse than one you cannot, and
                  # `interiors._unseal` is what stops a solid thing
                  # landing where it would close a room off.
                  "shelf", "barrel"}
PROP_INTERACT = {"chest", "chest_open", "elder", "smith", "shrine", "portal",
                 "runner", "keeper", "lair", "tower", "stepstone", "marker",
                 # A wayside stop on the last third of a road (item 12).
                 "camp",
                 # The stacked room's pile (0af). **Not a chest**, and
                 # deliberately: the chest count prices an ending (s42).
                 "hoard"}


@dataclass
class Chest:
    gx: int
    gy: int
    tier: int = 1
    opened: bool = False

    @property
    def sprite(self):
        return "chest_open" if self.opened else "chest"


@dataclass
class NPC:
    gx: int
    gy: int
    kind: str            # elder | smith
    name: str
    lines: tuple = ()


@dataclass
class Portal:
    gx: int
    gy: int
    boss_key: str
    unlocked: bool = False
    defeated: bool = False


class Camera:
    """Smoothly trails the player and never shows past the world edge."""

    def __init__(self, view_w, view_h, tile):
        self.x = 0.0
        self.y = 0.0
        self.view_w = view_w
        self.view_h = view_h
        self.tile = tile
        self.shake = 0.0
        self.shake_x = 0.0
        self.shake_y = 0.0

    def resize(self, view_w, view_h):
        self.view_w, self.view_h = view_w, view_h

    def add_shake(self, amount):
        self.shake = min(24.0, self.shake + amount)

    def follow(self, target_x, target_y, dt, snap=False, settle=42.0):
        """Trail the target; `settle` is how fast a shake dies, per second."""
        world_px_w = WORLD_W * self.tile
        world_px_h = WORLD_H * self.tile
        want_x = target_x - self.view_w / 2
        want_y = target_y - self.view_h / 2
        want_x = clamp(want_x, 0, max(0, world_px_w - self.view_w))
        want_y = clamp(want_y, 0, max(0, world_px_h - self.view_h))
        if snap:
            self.x, self.y = want_x, want_y
        else:
            # Frame-rate independent exponential smoothing.
            factor = 1.0 - pow(0.0001, dt)
            self.x += (want_x - self.x) * factor
            self.y += (want_y - self.y) * factor

        if self.shake > 0.05:
            self.shake = max(0.0, self.shake - dt * settle)
            self.shake_x = fx_rng.rfloat(-self.shake, self.shake)
            self.shake_y = fx_rng.rfloat(-self.shake, self.shake)
        else:
            self.shake_x = self.shake_y = 0.0

    @property
    def offset(self):
        return (int(self.x + self.shake_x), int(self.y + self.shake_y))

    def to_screen(self, world_x, world_y):
        ox, oy = self.offset
        return int(world_x - ox), int(world_y - oy)

    def visible_tiles(self):
        ox, oy = self.offset
        x0 = max(0, ox // self.tile - 1)
        y0 = max(0, oy // self.tile - 1)
        x1 = min(WORLD_W, (ox + self.view_w) // self.tile + 2)
        y1 = min(WORLD_H, (oy + self.view_h) // self.tile + 2)
        return range(int(x0), int(x1)), range(int(y0), int(y1))


class World:
    """Terrain, props and points of interest for one run."""

    def __init__(self, tile=32, seed=None, generate=True):
        self.tile = tile
        self.seed = seed
        self.grid = [[T_GRASS] * WORLD_W for _ in range(WORLD_H)]
        self.biome_map = [["meadow"] * WORLD_W for _ in range(WORLD_H)]
        self.props = {}                 # (gx, gy) -> prop name
        self.chests = []
        self.npcs = []
        # (gx, gy) -> waystation key, so the overworld can tell a keeper
        # from any other person standing in a field.
        self.waystations = {}
        # The landmass mask and the shore field, filled by
        # `_shape_island`. Regenerated from the seed with everything else,
        # never stored.
        self.land = [[True] * WORLD_W for _ in range(WORLD_H)]
        self.shore = [[0.0] * WORLD_W for _ in range(WORLD_H)]
        self.mainland = set()
        # How far a tower can see *here*, as a multiplier. Filled in
        # `generate`; see `revealed`.
        self.survey = [[0.5] * WORLD_W for _ in range(WORLD_H)]
        # (gx, gy) -> [field boss key, felled?]. Out in the world rather
        # than on a road, and remembered per save like a chest is.
        self.lairs = {}
        # (gx, gy) -> climbed?  Survey towers (s42): they reveal the map
        # and gate nothing.
        self.towers = {}
        # (gx, gy) -> [waystation key, attuned?]. The fast-travel network,
        # and each one has to be earned - see `entities.GUARDIANS`.
        self.stepstones = {}
        # (gx, gy) -> (kind, name). Hamlets, ruins and standing stones -
        # the places that are neither the village nor a waystation.
        self.outposts = {}
        # (gx, gy) -> (what stands there, the lines it reads). The
        # readable thing at a ruin or a circle (s55, item 12b). A hamlet
        # has a person to ask; these have only what is left standing.
        self.markers = {}
        # (gx, gy) -> [wayside key, taken?]. Somewhere on the last third
        # of a road worth stopping at (item 12). Shaped like `favours`
        # rather than like `markers` because a wayside is **spent**: it
        # has a decision in it and the decision is made once.
        self.waysides = {}
        # (gx, gy) -> [favour key, done?]. Small things people ask you for
        # (s42). Not quests: they never touch the one-at-a-time cap.
        self.favours = {}
        # (gx, gy) -> errand key. **A different set of people** to the ones
        # who ask favours (s42): a favour is answered where you stand, an
        # errand is a thing you go away and do.
        self.errand_givers = {}
        self.portals = []
        self.spawn = (WORLD_W // 2, WORLD_H // 2)
        self.town_rect = pygame.Rect(0, 0, 0, 0)
        self.doors = {}             # (gx, gy) -> "shop" | "forge" | "home"
        # Story state that belongs to this world rather than to the player.
        self.hollow_key = False
        self.story_flags = set()
        # **`generate=False` builds an empty one** (s56). A house
        # interior is a `World` with a room painted into it, and it must
        # not raise an island to throw away - not for the cost, but
        # because `generate` **reseeds the shared RNG** from its own
        # seed (hard rule 4). Building a room mid-run would move the
        # stream the overworld is drawing from, which is s21's bug in a
        # place nobody would think to look.
        if generate:
            self.generate()

    # -- queries -----------------------------------------------------------

    def in_bounds(self, gx, gy):
        return 0 <= gx < WORLD_W and 0 <= gy < WORLD_H

    def tile_at(self, gx, gy):
        if not self.in_bounds(gx, gy):
            return T_WALL
        return self.grid[gy][gx]

    def blocked(self, gx, gy):
        if not self.in_bounds(gx, gy):
            return True
        if TILES[self.grid[gy][gx]][1]:
            return True
        return self.props.get((gx, gy)) in BLOCKING_PROPS

    def on_land(self, gx, gy):
        """Whether this tile is walkable island - **the main one**.

        Offshore rocks are land and are deliberately not this: see
        `_find_mainland` for the ending it protects.
        """
        if not self.in_bounds(gx, gy):
            return False
        return (gx, gy) in self.mainland

    def encounter_chance(self, gx, gy):
        if not self.in_bounds(gx, gy) or self.props.get((gx, gy)):
            return 0.0
        if self.town_rect.collidepoint(gx, gy):
            return 0.0
        return TILES[self.grid[gy][gx]][2]

    def biome_at(self, gx, gy):
        if not self.in_bounds(gx, gy):
            return BIOMES["meadow"]
        return BIOMES[self.biome_map[gy][gx]]

    def texture_at(self, gx, gy):
        return TILES[self.grid[gy][gx]][0]

    # -- generation --------------------------------------------------------

    def _noise(self, scale=8):
        """Cheap value noise: a coarse random grid, bilinearly interpolated.

        Good enough for organic biome borders and terrain blobs, and it costs
        nothing compared with pulling in a noise library.
        """
        cw, ch = WORLD_W // scale + 2, WORLD_H // scale + 2
        coarse = [[rng.rfloat(0.0, 1.0) for _ in range(cw)] for _ in range(ch)]
        out = [[0.0] * WORLD_W for _ in range(WORLD_H)]
        for y in range(WORLD_H):
            fy = y / scale
            y0 = int(fy)
            ty = fy - y0
            for x in range(WORLD_W):
                fx = x / scale
                x0 = int(fx)
                tx = fx - x0
                top = coarse[y0][x0] * (1 - tx) + coarse[y0][x0 + 1] * tx
                bot = coarse[y0 + 1][x0] * (1 - tx) + coarse[y0 + 1][x0 + 1] * tx
                out[y][x] = top * (1 - ty) + bot * ty
        return out

    def _assign_biomes(self):
        """Difficulty rises with distance; *country* varies sideways.

        **Two fields, not one** (s42). Distance picks the tier, which is
        what every enemy, field boss and level number keys off and must
        not change. A second, much coarser noise field picks which biome
        of that tier you are actually standing in.

        That is the difference between a map with six rings and a map with
        thirteen places: crossing the frontier at one bearing takes you
        through the pines, at another through the canyon, and the fights
        are the same difficulty either way. Concentric rings could never
        do that, and it is what Savvydev was pointing at with *"so many
        different biomes"*.
        """
        wobble = self._noise(11)
        # Coarse on purpose: regions should be big enough to be somewhere,
        # rather than a rash of one-tile countries.
        region = self._noise(18)
        tiers = len(TIER_ORDER)
        for gy in range(WORLD_H):
            for gx in range(WORLD_W):
                # Distance to the *coast*, not to the corner of the map
                # (see `_shape_island`), so the last biome hugs the shore
                # the whole way round however the island came out.
                d = self.shore[gy][gx]
                d += (wobble[gy][gx] - 0.5) * 0.22
                d = max(0.0, d)   # wobble can push d negative near town
                # Power curve widens the early rings: the meadow and
                # wood should be roomy, the late biomes a thin frontier.
                # **Cubed, and that exponent is doing real work.** `d` is
                # now distance to the *shore*, which reaches 1.0 at the
                # coast, and the area of a ring grows with its radius - so
                # a gentle curve gives the outermost tiers most of the
                # island. Measured: at 1.7 the salt flats covered 14% of
                # the land and the meadow 2%, which is the wrong way round
                # for a valley you are supposed to start in.
                idx = int(clamp((d ** 3.0) * tiers, 0, tiers - 1))
                band = TIER_ORDER[idx]
                pick = int(region[gy][gx] * len(band)) % len(band)
                self.biome_map[gy][gx] = band[pick]

    # How far out the coast can reach, as a fraction of the half-width.
    # Below `COAST_INNER` is always land whatever the noise says, so the
    # village and the early roads can never be flooded; past
    # `COAST_OUTER` is always sea.
    COAST_INNER = 0.52
    COAST_OUTER = 0.98

    def _shape_island(self):
        """Carve the landmass into an island rather than a rectangle.

        Savvydev, 2026-08-29: *"instead of the map being a huge
        rectangle/square it should be like a huge island that is
        realistically shaped"*.

        **Radial falloff plus noise**, which is the standard way and the
        right one here: distance from the middle sets how likely a tile is
        to be sea, and a coarse noise field pushes the coast in and out so
        it comes out with bays, headlands and the odd offshore rock
        instead of a circle.

        Two guards, both learned rather than guessed:

        * **Inside `COAST_INNER` is land, unconditionally.** The village
          sits at the centre and the first roads leave it; a bay carved
          through either is not a coastline, it is a broken save.
        * **The border stays walled.** The sea is deep water and blocking
          anyway, but a wall means an out-of-bounds read can never be a
          walkable tile.
        """
        cx, cy = WORLD_W / 2.0, WORLD_H / 2.0
        coast = self._noise(22)
        self.land = [[True] * WORLD_W for _ in range(WORLD_H)]
        # **How far out to sea this tile is, as a fraction of the way from
        # the village to the coast on its own bearing.** 0 at the middle,
        # 1 at the water's edge, whatever shape the island came out.
        #
        # The biome tiers key off this rather than off distance from the
        # centre, and that is not a refinement - measured on the first
        # island, the late biomes had *vanished*: wastes covered three
        # tiles of land and salt none at all, because the rings were laid
        # out on the rectangle and the rectangle's corners are now sea.
        self.shore = [[0.0] * WORLD_W for _ in range(WORLD_H)]
        for gy in range(WORLD_H):
            for gx in range(WORLD_W):
                # Normalised so the shape does not stretch with the aspect
                # ratio - an island on a wide map should still be an
                # island, not an ellipse.
                dx = (gx - cx) / cx
                dy = (gy - cy) / cy
                d = (dx * dx + dy * dy) ** 0.5
                edge = self.COAST_INNER + (self.COAST_OUTER
                                           - self.COAST_INNER) * coast[gy][gx]
                self.shore[gy][gx] = min(1.0, d / edge) if edge > 0 else 1.0
                if d > edge:
                    self.land[gy][gx] = False
        self._find_mainland()

    def _find_mainland(self):
        """Which land is actually the island, and which is an islet.

        **An offshore rock is scenery and must never hold anything.** The
        Hollow Key needs *every* chest opened, so one chest on an island
        nobody can swim to makes `THE LONG WALK` unreachable for that save
        - an ending quietly deleted by a coastline. Lairs and waystations
        would be merely infuriating; the chest case is a broken run.

        So the islets stay on the map, because they look right, and
        `on_land` stops meaning "not sea" and starts meaning "reachable
        from the village on foot".
        """
        seen = set()
        start = (WORLD_W // 2, WORLD_H // 2)
        if not self.land[start[1]][start[0]]:
            self.mainland = seen
            return
        stack = [start]
        seen.add(start)
        while stack:
            gx, gy = stack.pop()
            for nx, ny in ((gx + 1, gy), (gx - 1, gy),
                           (gx, gy + 1), (gx, gy - 1)):
                if (nx, ny) in seen:
                    continue
                if not (0 <= nx < WORLD_W and 0 <= ny < WORLD_H):
                    continue
                if not self.land[ny][nx]:
                    continue
                seen.add((nx, ny))
                stack.append((nx, ny))
        self.mainland = seen

    def _fill_terrain(self):
        brush_noise = self._noise(6)
        hazard_noise = self._noise(9)
        for gy in range(WORLD_H):
            for gx in range(WORLD_W):
                if not self.land[gy][gx]:
                    self.grid[gy][gx] = T_WATER
                    continue
                biome = BIOMES[self.biome_map[gy][gx]]
                tile = biome.ground
                if hazard_noise[gy][gx] > 0.80:
                    tile = biome.hazard
                elif brush_noise[gy][gx] > 0.58:
                    tile = biome.brush
                self.grid[gy][gx] = tile

        # Solid border so the player can never walk off the map.
        for gx in range(WORLD_W):
            self.grid[0][gx] = self.grid[WORLD_H - 1][gx] = T_WALL
        for gy in range(WORLD_H):
            self.grid[gy][0] = self.grid[gy][WORLD_W - 1] = T_WALL

    def _house(self, hx, hy, hw, hh, roof, door_floor=None, door="home"):
        """A house: a roofed body with a doorway and windows in the front.

        Drawn roof-first so the top two rows read as a lid rather than more
        wall. A flat rectangle of brick is a compound, not a home.
        """
        for gy in range(hy, hy + hh):
            for gx in range(hx, hx + hw):
                if not self.in_bounds(gx, gy):
                    continue
                self.grid[gy][gx] = roof if gy < hy + 2 else T_WALL
                self.props.pop((gx, gy), None)

        front = hy + hh - 1
        door_x = hx + hw // 2
        if self.in_bounds(door_x, front):
            self.grid[front][door_x] = door_floor or T_WOOD
            self.doors[(door_x, front)] = door
        # **A window goes on a wall, and the wall is the test** (2026-09-01).
        # This used to ask whether the window was within half the house's
        # width of the door, which is not the same question: on a four-wide
        # house `door_x + 2` is one tile *past* the end of it, and
        # `abs(2) < 4 // 2 + 1` is true - so a lit window sat on the grass
        # beside the house. Savvydev sent screenshots of exactly that.
        #
        # Asking the grid what is actually there cannot drift the way an
        # arithmetic guard can: the front row is wall everywhere except the
        # doorway, which was carved above.
        for wx in (door_x - 2, door_x + 2):
            if not self.in_bounds(wx, front):
                continue
            if not (hx <= wx < hx + hw):
                continue
            if self.grid[front][wx] != T_WALL:
                continue
            self.props[(wx, front)] = "window"
        return (door_x, front)

    # **Where a house can stand**, as offsets from the centre of the
    # green: (dx, dy, w, h). Fixed slots rather than rolled rectangles,
    # because two houses that overlap is a wall you can walk through and
    # a door that opens into brick - and a slot list can be checked once
    # and then trusted. The first four are the deep ones the shop and the
    # forge want; the last two sit below the green and are three tall so
    # their doorway opens onto clear ground rather than into the fence
    # line along the town's edge. **That row matters**: one lower and the
    # door was hemmed in by its own walls and a fence, which the sweep in
    # `TestVillageVariety` caught on 108 of 120 seeds.
    HOUSE_SLOTS = (
        (-8, -9, 5, 4),
        (4, -9, 5, 4),
        (-8, 2, 5, 4),
        (4, 2, 5, 4),
        (-12, -3, 4, 4),
        (9, -3, 4, 4),
        (-4, 6, 4, 3),
        (1, 6, 4, 3),
    )

    #: How many houses a village has, beyond the shop and the forge.
    HOMES_MIN, HOMES_MAX = 3, 6

    def town_rng(self):
        """A private stream for the village (item 19).

        **It must not touch the shared one**, and that is not tidiness.
        `generate()` reseeds the shared RNG from the world seed and then
        rolls the chests, the portals, the lairs and the towers off it -
        and a save stores its chests **by coordinate** (`save.py`). So a
        single extra draw in here would move every chest on the island,
        and a run in progress would load with its opened chests forgotten
        and `THE LONG WALK` quietly unwinnable.

        Same rule as `interiors._rng_for` and for the same reason (s56,
        and s21's four-classes bug).
        """
        return RNG((int(self.seed or 0) * 31337 + 17) & 0x7FFFFFFF)

    def _build_town(self):
        """A village: houses with roofs and doors, a green, a well, lanes.

        The first attempt paved the whole area in planks and dropped four
        brick rectangles on it, which read as a walled compound.

        **No two villages are the same since item 19.** Which slots hold
        houses, how they are roofed, where the lanes run, what is scattered
        on the green and how many people live there are all rolled from
        the world seed. What does *not* vary is everything the rest of the
        game holds a reference to: the shop, the forge, the well, the
        shrine, the smith, the elder, and where you spawn.
        """
        rng_town = self.town_rng()
        cx, cy = WORLD_W // 2, WORLD_H // 2
        self.town_rect = pygame.Rect(cx - 13, cy - 10, 27, 21)

        # Ground: grass, with dirt lanes and a stone green at the centre.
        for gy in range(self.town_rect.top, self.town_rect.bottom):
            for gx in range(self.town_rect.left, self.town_rect.right):
                if self.in_bounds(gx, gy):
                    self.grid[gy][gx] = T_GRASS
                    self.props.pop((gx, gy), None)
        # **The lanes vary, the crossroads does not.** The middle pair is
        # always there - it is what makes the green a crossing rather than
        # a field - and the outer pair is rolled, so some villages are a
        # crossroads and some are a grid.
        lanes_across = [cy]
        lanes_down = [cx]
        if rng_town.chance(0.75):
            lanes_across += [cy - 6, cy + 6]
        if rng_town.chance(0.75):
            lanes_down += [cx - 9, cx + 9]
        for gx in range(self.town_rect.left, self.town_rect.right):
            for gy in lanes_across:
                if self.in_bounds(gx, gy):
                    self.grid[gy][gx] = T_PATH
        for gy in range(self.town_rect.top, self.town_rect.bottom):
            for gx in lanes_down:
                if self.in_bounds(gx, gy):
                    self.grid[gy][gx] = T_PATH
        for gy in range(cy - 4, cy + 5):
            for gx in range(cx - 4, cx + 5):
                if self.in_bounds(gx, gy):
                    self.grid[gy][gx] = T_STONE

        # **The shop and the forge take two of the four deep slots**, and
        # the homes take a rolled number of what is left. They are first
        # because `use_door` and the two quest-giving NPCs are anchored to
        # them - a village without a trader is not a variation, it is a
        # broken run.
        self.doors = {}
        deep = list(self.HOUSE_SLOTS[:4])
        rng_town.shuffle(deep)
        roofs = (T_ROOF, T_ROOF2, T_THATCH)
        shop_slot, forge_slot = deep[0], deep[1]
        shop_door = self._house(cx + shop_slot[0], cy + shop_slot[1],
                                shop_slot[2], shop_slot[3],
                                rng_town.choice(roofs), T_SHOP, "shop")
        forge_door = self._house(cx + forge_slot[0], cy + forge_slot[1],
                                 forge_slot[2], forge_slot[3],
                                 rng_town.choice(roofs), None, "forge")
        spare = deep[2:] + list(self.HOUSE_SLOTS[4:])
        rng_town.shuffle(spare)
        homes = rng_town.rint(self.HOMES_MIN, self.HOMES_MAX)
        for dx, dy, hw, hh in spare[:homes]:
            self._house(cx + dx, cy + dy, hw, hh, rng_town.choice(roofs))
        self.props[(shop_door[0] - 2, shop_door[1])] = "sign"

        # The green: a well in the middle, and lanterns on some of the
        # corners rather than all four.
        self.props[(cx, cy - 2)] = "well"
        corners = [(cx - 4, cy - 4), (cx + 4, cy - 4),
                   (cx - 4, cy + 4), (cx + 4, cy + 4)]
        rng_town.shuffle(corners)
        for lx, ly in corners[:rng_town.rint(2, 4)]:
            if self.in_bounds(lx, ly) and (lx, ly) not in self.props:
                self.props[(lx, ly)] = "lantern"

        # A fence line along the outer edge, with gaps for the lanes.
        # **A doorway is a gap too** - the two low slots put a front door
        # near this row, and a fence is solid, so without this a house
        # could be fenced shut.
        for gx in range(self.town_rect.left, self.town_rect.right, 2):
            for gy in (self.town_rect.top, self.town_rect.bottom - 1):
                if gx in lanes_down:
                    continue
                if (gx, gy) in self.doors:
                    continue
                if self.in_bounds(gx, gy) and (gx, gy) not in self.props:
                    self.props[(gx, gy)] = "fence"

        # The people who live here.
        smith_at = (forge_door[0], forge_door[1] + 1)
        elder_at = (shop_door[0], shop_door[1] + 1)
        self.npcs = [
            NPC(smith_at[0], smith_at[1], "smith", "Bram the Smith",
                ("Bring me coin and steel and I will make it meaner.",
                 "Salvage what you do not wear. That is where steel comes from.")),
            NPC(elder_at[0], elder_at[1], "elder", "Elder Maren",
                ("Six wardens hold the roads. Fell one and the next stirs.",
                 "The shrine restores what the road takes. Use it.")),
        ]
        self.props[smith_at] = "smith"
        self.props[elder_at] = "elder"

        # **Named, not labelled** (2026-08-31, Savvydev). The four of them
        # were "Villager", "Herder", "Weaver" and "Child"; the trade stays
        # in the name where there is one, the way Bram the Smith's does.
        # How many of them turn out, and where they stand, is rolled.
        # **Every clear tile in the town, not a short list of spots.**
        # A fixed list was written against fixed houses; now that the
        # houses move, a spot that was clear of every door in the old
        # recipe can be standing in a doorway in the new one.
        villager_spots = [
            (vx, vy)
            for vy in range(self.town_rect.top + 1, self.town_rect.bottom - 1)
            for vx in range(self.town_rect.left + 1, self.town_rect.right - 1)
            if self.grid[vy][vx] in (T_GRASS, T_PATH, T_STONE)
        ]
        rng_town.shuffle(villager_spots)
        # **All four, always, and only these four** (item 19). Each one is
        # a key into `scenes.VILLAGER_SCENES` and `Game.VILLAGER_LINES`,
        # and a villager who is in neither **silently stops having a
        # conversation** - which is how a fifth invented name got through
        # a screenshot and was caught by a test. Varying *who turns up*
        # would also mean a run where a scene cannot be reached at all,
        # which is content removed rather than varied (§20's rule about
        # story). So the people are fixed and where they stand is not.
        names = ["Tobin", "Grell the Herder", "Sena the Weaver", "Pip"]
        taken = 0
        for vx, vy in villager_spots:
            if taken >= len(names):
                break
            if not self.in_bounds(vx, vy) or (vx, vy) in self.props:
                continue
            if self.blocked(vx, vy) or self.grid[vy][vx] in (T_WALL,):
                continue
            # **Three tiles clear of every door**, and the number is
            # derived rather than chosen. `interactable_near` scans the
            # nine tiles around you and **returns the first match**, with
            # south ahead of north - so a villager anywhere in the
            # neighbourhood of the square you stand on to open a door
            # answers `E` instead of the door, and that house can never be
            # opened. Nothing is blocked and nothing looks wrong; the door
            # simply stops working.
            #
            # A standing tile is one from the door, and a shadowing
            # villager is one from the standing tile, so anything closer
            # than three can shadow. Item 19 moved the houses, so spots
            # that were clear of every door by construction no longer are:
            # 19 seeds in 120 had a house nobody could get into.
            if any(max(abs(vx - dx), abs(vy - dy)) < 3
                   for dx, dy in self.doors):
                continue
            # **And two clear of the spawn, the well and the shrine.**
            # Standing on them is obviously wrong; standing *next* to the
            # spawn means every return to the village opens with a prompt
            # to talk to whoever is loitering on the doorstep.
            if any(max(abs(vx - sx), abs(vy - sy)) < 2
                   for sx, sy in (self.spawn, (cx, cy - 2), (cx, cy + 2))):
                continue
            self.props[(vx, vy)] = "villager"
            self.npcs.append(NPC(vx, vy, "villager", names[taken],
                                 ("Quiet year, so far.",)))
            taken += 1

        # A garden or two, purely so the green is not the same picture
        # twice. Flowers do not block, so nothing can be walled in by one.
        for _ in range(rng_town.rint(2, 6)):
            fx = rng_town.rint(self.town_rect.left + 1,
                               self.town_rect.right - 2)
            fy = rng_town.rint(self.town_rect.top + 1,
                               self.town_rect.bottom - 2)
            if (fx, fy) in self.props or (fx, fy) in self.doors:
                continue
            if self.grid[fy][fx] != T_GRASS:
                continue
            self.props[(fx, fy)] = "flower"

        self.grid[cy + 2][cx] = T_SHRINE
        self.props[(cx, cy + 2)] = "shrine"
        self.spawn = (cx, cy)

    def _place_portals(self):
        """Arena gates, spaced around the valley and clear of the village.

        The first version started at radius 13, which reached inside the
        village once it grew, so a warden's arena was carved through people's
        houses.
        """
        import math
        cx, cy = WORLD_W // 2, WORLD_H // 2
        keep_clear = self.town_rect.inflate(10, 10)

        # **The roads reach the frontier now** (s42). Scaling the old
        # radii kept every portal at the fraction of the map it always
        # sat at - which was fine on a valley half this size and put all
        # six inside the meadow ring on this one, leaving the tundra, the
        # volcano and the wastes as scenery nobody has a reason to enter.
        # Screenshotting the whole valley is what showed it; the numbers
        # all looked right (rule 31.4).
        #
        # So the ring is a **band across the usable radius** instead: the
        # first road is close to home and the last is out at the rim,
        # which is also what the biome curve was always describing -
        # difficulty rising with distance from town.
        span = min(WORLD_W, WORLD_H / 0.72) / 2.0
        near, far = 0.30, 0.88
        for i, boss in enumerate(BOSS_DEFS):
            angle = (i / len(BOSS_DEFS)) * math.tau + 0.35
            placed = None
            reach = near + (far - near) * (i / max(1, len(BOSS_DEFS) - 1))
            outer = int(span * reach)
            # Room to fall back inward when the spot is water or town, but
            # never so far that a late road lands in the early biomes.
            inner = int(span * max(near, reach - 0.10))
            for radius in range(outer, inner - 1, -1):
                gx = int(clamp(cx + math.cos(angle) * radius, 4, WORLD_W - 5))
                gy = int(clamp(cy + math.sin(angle) * radius * 0.72,
                               4, WORLD_H - 5))
                arena = pygame.Rect(gx - 2, gy - 2, 5, 5)
                if keep_clear.colliderect(arena):
                    continue
                # **On land, and with land around it.** An arena carves
                # walkable floor over whatever is underneath, so a portal
                # in the sea does not look wrong - it looks like an island
                # nobody can reach, and the road to it runs over water.
                if not self._arena_fits(gx, gy):
                    continue
                placed = (gx, gy)
                break
            if placed is None:
                # Nothing on that bearing: sweep inward along it until the
                # island offers somewhere. The band is a preference, and
                # a warden that exists beats a warden in the right ring.
                for radius in range(inner, 4, -1):
                    gx = int(clamp(cx + math.cos(angle) * radius,
                                   4, WORLD_W - 5))
                    gy = int(clamp(cy + math.sin(angle) * radius * 0.72,
                                   4, WORLD_H - 5))
                    if keep_clear.colliderect(pygame.Rect(gx - 2, gy - 2,
                                                          5, 5)):
                        continue
                    if self._arena_fits(gx, gy):
                        placed = (gx, gy)
                        break
            if placed is None:          # pushed to the rim as a last resort
                gx = int(clamp(cx + math.cos(angle) * (WORLD_W // 2 - 6),
                               4, WORLD_W - 5))
                gy = int(clamp(cy + math.sin(angle) * (WORLD_H // 2 - 6),
                               4, WORLD_H - 5))
                placed = (gx, gy)
            gx, gy = placed

            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    tx, ty = gx + dx, gy + dy
                    if self.in_bounds(tx, ty):
                        self.grid[ty][tx] = T_ARENA
                        self.props.pop((tx, ty), None)
            self.props[(gx, gy)] = "portal"
            self.portals.append(Portal(gx, gy, boss.key, unlocked=(i == 0)))


    def _arena_fits(self, gx, gy):
        """Whether a 5x5 arena here would sit on the island."""
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                if not self.on_land(gx + dx, gy + dy):
                    return False
        return True

    def _carve_road(self, start, end, paved=True, protect=()):
        """A way from one place to another, wandering rather than square.

        **It used to be an L**, which was fine on a rectangle and read as
        drawn-on across an island. It walks toward the target one tile at
        a time now, choosing the axis it is furthest off and wandering
        sideways when it is nearly lined up, so a road bends around the
        country instead of ignoring it.

        `paved=False` carves **a pass, not a road**: it turns blocking
        rock into the local ground and leaves no path tile behind. That is
        what connectivity repairs use, because a repair that paves is a
        signpost, and Savvydev asked for the way to a warden to be something
        you work out rather than something you are shown.
        """
        (x0, y0), (x1, y1) = start, end
        town = self.town_rect
        gx, gy = int(x0), int(y0)
        for _ in range(WORLD_W * 4):
            if (gx, gy) == (int(x1), int(y1)):
                break
            if not town.collidepoint(gx, gy) and self.in_bounds(gx, gy) \
                    and self.grid[gy][gx] != T_ARENA:
                if (gx, gy) in protect:
                    # A chest, a marker, a door or somebody standing
                    # there. **A repair that removes the thing it is
                    # repairing access to** is worse than the fault.
                    pass
                elif paved:
                    self.grid[gy][gx] = T_PATH
                    self.props.pop((gx, gy), None)
                else:
                    # A pass, not a road. It clears the rock **and the
                    # solid props** in the way: without the second half a
                    # pass could be carved straight through a fence line
                    # and leave it standing, which is what left chests
                    # unreachable after a "successful" repair (s64).
                    if TILES[self.grid[gy][gx]][1]:
                        self.grid[gy][gx] = \
                            BIOMES[self.biome_map[gy][gx]].ground
                    if self.props.get((gx, gy)) in BLOCKING_PROPS:
                        self.props.pop((gx, gy), None)
            dx, dy = int(x1) - gx, int(y1) - gy
            # Take the axis we are furthest off, and wander on the other
            # one once the two are close - which is what makes it bend.
            if abs(dx) > abs(dy):
                step_x, step_y = (1 if dx > 0 else -1), 0
                if abs(dy) and rng.chance(0.28):
                    step_x, step_y = 0, (1 if dy > 0 else -1)
            elif dy:
                step_x, step_y = 0, (1 if dy > 0 else -1)
                if abs(dx) and rng.chance(0.28):
                    step_x, step_y = (1 if dx > 0 else -1), 0
            else:
                step_x, step_y = (1 if dx > 0 else -1), 0
            gx, gy = gx + step_x, gy + step_y
            gx = int(clamp(gx, 1, WORLD_W - 2))
            gy = int(clamp(gy, 1, WORLD_H - 2))

    # How much of the island is mountain, and how far out it starts.
    # Ridges belong to the country you are not meant to stroll through,
    # so the starting tiers stay open.
    RIDGE_FROM = 0.34
    RIDGE_THRESHOLD = 0.62

    def _raise_mountains(self):
        """Ridges of impassable rock, out past the early country.

        **Ridges, not blobs.** A field of scattered boulders is noise you
        walk around without noticing; a ridge is a thing you have to find
        the end of, which is the navigation Savvydev asked for. So the noise
        is stretched along one axis per band, and thresholded high enough
        that what survives is long and thin.

        Nothing is walled off permanently - `_ensure_connectivity` carves
        a pass through anything it cannot reach, and the pass is unpaved
        so it reads as a gap in the rock rather than a road.
        """
        ridge = self._noise(9)
        cross = self._noise(26)
        for gy in range(2, WORLD_H - 2):
            for gx in range(2, WORLD_W - 2):
                if not self.land[gy][gx]:
                    continue
                if self.shore[gy][gx] < self.RIDGE_FROM:
                    continue          # the valley you start in stays open
                if TILES[self.grid[gy][gx]][1]:
                    continue          # already water or lava
                # Stretched: sample the two fields at different rates so
                # the high ground comes out in lines rather than patches.
                value = ridge[gy][gx] * 0.65 + cross[gy][gx] * 0.35
                if value > self.RIDGE_THRESHOLD:
                    self.grid[gy][gx] = T_MOUNTAIN
                    self.props.pop((gx, gy), None)

    def _scatter_props(self):
        for gy in range(2, WORLD_H - 2):
            for gx in range(2, WORLD_W - 2):
                if self.grid[gy][gx] in (T_PATH, T_ARENA, T_WOOD, T_STONE):
                    continue
                if TILES[self.grid[gy][gx]][1] or (gx, gy) in self.props:
                    continue
                biome = BIOMES[self.biome_map[gy][gx]]
                if not biome.props or not rng.chance(0.055):
                    continue
                prop = rng.choice(biome.props)
                # Never let a decoration seal off a tile next to a road.
                if prop in BLOCKING_PROPS and self._touches_path(gx, gy):
                    continue
                self.props[(gx, gy)] = prop

    def _touches_path(self, gx, gy):
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if self.tile_at(gx + dx, gy + dy) in (T_PATH, T_ARENA):
                return True
        return False

    def _place_waystations(self):
        """One inhabited place partway along each road (s42, item 12).

        **On the road, not near it.** A waystation the player can walk past
        is a waystation most players never see, and the whole point of it
        is to be the thing that happens between the village and the warden.
        So it is planted at a fraction of the way along the same line
        `_carve_road` will later carve, which guarantees the road runs
        through it.

        Two thirds of the way out, not half: near the portal the road is
        already tense, and a place with people in it lands harder there
        than it does in the middle of a field.
        """
        from . import story
        for portal in self.portals:
            stop = story.WAYSTATION_BY_BOSS.get(portal.boss_key)
            if stop is None:
                continue
            sx, sy = self.spawn
            gx = int(round(sx + (portal.gx - sx) * 0.66))
            gy = int(round(sy + (portal.gy - sy) * 0.66))
            spot = self._clear_spot_near(gx, gy)
            if spot is None:
                continue
            gx, gy = spot
            self.props[(gx, gy)] = "keeper"
            self.npcs.append(NPC(gx, gy, "keeper", stop.keeper,
                                 tuple(stop.before)))
            self.waystations[(gx, gy)] = stop.key
            # **The gate stands beside the person**, not instead of them.
            # The waystation is somewhere people live and the story
            # fragment lives with the keeper (s42); the stepstone is an old
            # thing in the same place that happens to still work.
            gate = self._clear_spot_near(gx + 2, gy, reach=3)
            if gate and gate not in self.waystations:
                self.props[gate] = "stepstone"
                self.stepstones[gate] = [stop.key, False]
            # A fence line and a lamp, so it reads as somewhere rather
            # than as one person standing in a field. **Existing props
            # only** - hard rule 1 means no new art, and a waystation is a
            # place people live in, not a new kind of object.
            #
            # (`house` was tried here first and there is no such prop: the
            # village draws its buildings as terrain, not sprites. It
            # crashed every screen that draws the overworld.)
            for dx, dy, prop in ((-2, -1, "fence"), (2, -1, "fence"),
                                 (-2, 2, "fence"), (2, 2, "fence"),
                                 (0, -3, "lantern")):
                tx, ty = gx + dx, gy + dy
                if (self.in_bounds(tx, ty) and (tx, ty) not in self.props
                        and not self.town_rect.collidepoint(tx, ty)):
                    self.props[(tx, ty)] = prop

    def _clear_spot_near(self, gx, gy, reach=4):
        """The nearest tile that is walkable, empty and out of town."""
        for radius in range(reach + 1):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    tx, ty = gx + dx, gy + dy
                    if not self.in_bounds(tx, ty):
                        continue
                    if not self.on_land(tx, ty):
                        continue
                    if self.blocked(tx, ty) or (tx, ty) in self.props:
                        continue
                    if self.town_rect.collidepoint(tx, ty):
                        continue
                    return (tx, ty)
        return None

    def _place_runners(self, count=5):
        """Maren's couriers, posted along the roads.

        Quests can only be taken from a person, so there has to be a person
        within reach - otherwise every errand means crossing the whole valley
        back to the village.
        """
        import math
        cx, cy = self.spawn
        names = ["Pell", "Odarry", "Wick", "Sannet", "Corvi"]
        for i in range(count):
            angle = (i / count) * math.tau + 0.9
            for radius in (_scaled(11), _scaled(14), _scaled(17),
                           _scaled(20), _scaled(8)):
                gx = int(clamp(cx + math.cos(angle) * radius, 3, WORLD_W - 4))
                gy = int(clamp(cy + math.sin(angle) * radius * 0.7,
                               3, WORLD_H - 4))
                if self.blocked(gx, gy) or (gx, gy) in self.props:
                    continue
                if self.town_rect.collidepoint(gx, gy):
                    continue
                self.props[(gx, gy)] = "runner"
                self.npcs.append(NPC(gx, gy, "runner",
                                     "%s, Maren's runner" % names[i],
                                     ("She sends word out. I carry it back.",)))
                break

    def _place_lairs(self):
        """One field boss in each biome it belongs to (s42).

        **Off the roads, on purpose.** A mini boss you meet by walking the
        main route is an obstacle; one you meet by going to look at
        something is a discovery, and the second is the thing Savvydev asked
        for. So the search starts well away from the village and rejects
        anything sitting on a path.

        **Nothing gates them.** You can walk into the wastes at level six
        and be killed by the Salt Colossus, and that is information rather
        than a wall - it is the one place this game can honestly borrow
        "go and find out", because the wardens themselves are strictly
        ordered.
        """
        cx, cy = self.spawn
        # **Density is per area, not per family.** One lair each was
        # already thin on the 224-wide valley and would be scenery on this
        # one: the whole point of a landmark is that you keep meeting them,
        # and a map you can cross without passing one has none.
        per_family = max(1, int(round(2 * (WORLD_W * WORLD_H) / 76608.0)))
        for boss in FIELD_BOSSES:
            home = siblings_of(boss.biome)
            for _ in range(per_family):
                self._plant_lair(boss, home, cx, cy)

    def _plant_lair(self, boss, home, cx, cy):
        """One lair for this family, somewhere in its own tier."""
        spot = None
        for _ in range(400):
            gx = rng.rint(3, WORLD_W - 4)
            gy = rng.rint(3, WORLD_H - 4)
            if not self.on_land(gx, gy):
                continue
            if self.biome_map[gy][gx] not in home:
                continue
            if self.blocked(gx, gy) or (gx, gy) in self.props:
                continue
            if self.town_rect.collidepoint(gx, gy):
                continue
            if self.grid[gy][gx] == T_PATH:
                continue          # not standing in the road
            if abs(gx - cx) + abs(gy - cy) < _scaled(14):
                continue          # not on the doorstep either
            spot = (gx, gy)
            break
        if spot is None:
            return                # that tier had no room this seed
        from .entities import field_variant
        self.props[spot] = "lair"
        # [family, variant, resting-until-playtime]
        self.lairs[spot] = [boss.key, field_variant(), 0.0]

    # How long a felled field boss stays felled, in seconds of play.
    # **Long enough to be a real absence, short enough to come back** -
    # Savvydev asked for these to be repeatable rather than cleared, so the
    # landmark has to still mean something after you have won there.
    LAIR_REST = 900.0

    def lair_at(self, gx, gy, playtime=0.0):
        """`(BossDef, resting)` for a lair on this tile, or None."""
        from .entities import FIELD_BOSS_BY_KEY
        entry = self.lairs.get((gx, gy))
        if not entry:
            return None
        boss = FIELD_BOSS_BY_KEY.get(entry[0])
        if boss is None:
            return None
        return (boss, float(playtime) < float(entry[2]))

    def lair_variant(self, spot):
        entry = self.lairs.get(tuple(spot))
        return entry[1] if entry else ""

    def fell_lair(self, gx, gy, playtime=0.0):
        """Put this one to rest. It wakes up again later."""
        entry = self.lairs.get((gx, gy))
        if entry:
            entry[2] = float(playtime) + self.LAIR_REST

    # How far a climbed tower shows, in tiles, and how many there are.
    # **Scattered, not one per biome** - Savvydev, 2026-08-29: *"the towers
    # in BoTW arent just assigned to each biome, theyre kind of just
    # randomly scattered throughout the map"*. So they go where the island
    # will take them, spaced off each other and nothing else.
    # Sight and spacing were tuned against coverage, not guessed: at 46
    # and nine towers, climbing every one of them still left 13% of the
    # island dark on some seeds, and \"climb them all to see the map\"
    # has to actually mean that.
    TOWER_SIGHT = 66
    TOWER_SPACING = 34

    def _place_towers(self, count=11):
        """Survey towers, scattered across the island.

        **They reveal the map and gate nothing.** You can walk anywhere on
        the island from the first minute; what you cannot do is *see* where
        you have not been. That is the Breath of the Wild arrangement and
        Savvydev asked for it by name: *"you can travel anywhere in the map
        without unlocking those big towers, but to actually see how the
        whole map looks you have to unlock all the towers"*.

        Spacing is the only rule beyond standing on solid ground - two
        towers close together reveal one circle twice and waste a
        landmark.
        """
        placed = []
        for _ in range(count * 120):
            if len(placed) >= count:
                break
            gx = rng.rint(4, WORLD_W - 5)
            gy = rng.rint(4, WORLD_H - 5)
            if not self.on_land(gx, gy):
                continue
            if self.blocked(gx, gy) or (gx, gy) in self.props:
                continue
            if self.town_rect.collidepoint(gx, gy):
                continue
            if any(max(abs(gx - px), abs(gy - py)) < self.TOWER_SPACING
                   for px, py in placed):
                continue
            placed.append((gx, gy))
            self.props[(gx, gy)] = "tower"
            self.towers[(gx, gy)] = False

    def climb_tower(self, gx, gy):
        """Mark a tower climbed. True if this one was new."""
        if self.towers.get((gx, gy)) is False:
            self.towers[(gx, gy)] = True
            return True
        return False

    def revealed(self, gx, gy):
        """Whether the map has been surveyed here.

        The village and its surroundings are known from the start - you
        live there - and everything else waits for a tower.
        """
        if self.town_rect.inflate(24, 24).collidepoint(gx, gy):
            return True
        # **Ragged, not round.** Savvydev, 2026-08-29: *"the towers just
        # unlock huge spheres of the map, if you look at BoTW the parts of
        # the map unlocked are way less perfect and stuff"*. He is right,
        # and a perfect disc is worse than wrong - it reads as a mask laid
        # over the map rather than as country somebody could see.
        #
        # So the sight radius is modulated per tile by a coarse noise
        # field: the edge bulges down valleys and stops short against the
        # far side of a ridge, and the border between surveyed and
        # unsurveyed comes out irregular the whole way round. Same field
        # every time, because it comes from the seed.
        for (tx, ty), climbed in self.towers.items():
            if not climbed:
                continue
            dx, dy = gx - tx, gy - ty
            reach = self.TOWER_SIGHT * (0.62 + 0.68 * self.survey[gy][gx])
            if dx * dx + dy * dy <= reach * reach:
                return True
        return False

    def towers_climbed(self):
        return sum(1 for done in self.towers.values() if done)

    def stepstone_at(self, gx, gy):
        """`(waystation key, attuned)` for a gate here, or None."""
        entry = self.stepstones.get((gx, gy))
        return (entry[0], bool(entry[1])) if entry else None

    def attune_stepstone(self, gx, gy):
        """Open this gate. True if it was shut."""
        entry = self.stepstones.get((gx, gy))
        if entry and not entry[1]:
            entry[1] = True
            return True
        return False

    def open_stepstones(self):
        """Every gate you may travel to: `[(spot, waystation key)]`."""
        return [(spot, entry[0]) for spot, entry in self.stepstones.items()
                if entry[1]]

    # Names for the places that are still lived in, and for the ones that
    # are not. Drawn from the same well as the rest of the valley: plain,
    # a bit worn, nobody trying to sound grand.
    HAMLET_NAMES = ("Two Wells", "Underhill", "Cobb's Rest", "Fen End",
                    "Stonecross", "Mareswood", "Little Ain", "Byre",
                    "Thorncot", "Rushmere")
    RUIN_NAMES = ("Ashcot", "Old Byre", "The Drowned Rows", "Nettlefold",
                  "What Is Left of Hale", "Cinderhome", "Grey Acre",
                  "The Empty Fold")

    def _place_outposts(self, hamlets=6, ruins=6, stones=5):
        """Places that are not the village and not a waystation (s42).

        Savvydev, 2026-08-29: *"to make the bigger valley more interesting
        add different things to do around the map as opposed to just
        simply making it bigger, add villages, abandoned villages, cool
        structures, etc, etc"*.

        Three kinds, all built from tiles and props that already exist -
        hard rule 1 is not in the way, because a settlement is an
        arrangement rather than an asset:

        * **A hamlet** is lived in: two or three houses and somebody
          outside who will talk to you.
        * **A ruin** is the same shape with the roofs gone. Nobody is
          there. It is the one that does the most work, because a valley
          with abandoned places in it has had a past.
        * **Standing stones** are neither - a thing somebody put up, and
          no explanation offered.

        **None of them hold a chest.** The Hollow Key needs *every* chest
        and the count is what `THE LONG WALK` costs (§42), so scattering
        a few more around ruins would quietly reprice an ending.
        """
        self.outposts = {}
        self.markers = {}
        circle = 0
        for kind, count, names in (("hamlet", hamlets, self.HAMLET_NAMES),
                                   ("ruin", ruins, self.RUIN_NAMES),
                                   ("stones", stones, ())):
            for i in range(count):
                spot = self._outpost_spot()
                if spot is None:
                    continue
                name = names[i % len(names)] if names else ""
                self._build_outpost(kind, spot, name, circle)
                if kind == "stones":
                    circle += 1

    def _place_favours(self):
        """Give some of the people on the island something to ask.

        **Hamlet folk first, then runners**, because somebody standing
        outside their own house is who you expect to want something.
        Nobody ever carries both kinds - being asked a riddle and then
        handed a job by one person reads as one NPC with two mouths.

        **Dealt one each way, and there are more asks than people**
        (2026-08-30). Seventeen between them, about fifteen people to
        carry them. Taking every favour first, which is what this did,
        meant the favours were always all placed and the last errands
        never were - a silent priority nobody chose. Alternating puts the
        shortfall on both kinds, and **both pools are shuffled**, so which
        two go missing is a property of the seed. That pays the replay
        system (s33) rather than costing it: a second run is not the same
        people wanting the same things in the same places.
        """
        from . import story
        from .quests import ERRAND_DEFS
        takers = [n for n in self.npcs if n.kind == "villager"]
        takers += [n for n in self.npcs if n.kind == "runner"]
        rng.shuffle(takers)

        favours = list(story.FAVOURS)
        errands = list(ERRAND_DEFS)
        rng.shuffle(favours)
        rng.shuffle(errands)

        def take_errand(npc):
            """The next errand this person can plausibly be the one asking.

            **A runner does not have ewes, knees or a daughter behind
            them** (`story.ErrandVoice.settled`) - they walk the valley
            for a living. Preference, not a rule: if the settled ones are
            all that is left, a runner gets one rather than the errand
            going unplaced.
            """
            for i, errand in enumerate(errands):
                voice = story.errand_voice(errand.key)
                if npc.kind != "runner" or not (voice and voice.settled):
                    return errands.pop(i)
            return errands.pop() if errands else None

        favour_next = True
        for npc in takers:
            errand = None
            if not favour_next or not favours:
                errand = take_errand(npc)
            if errand is not None:
                self.errand_givers[(npc.gx, npc.gy)] = errand.key
            elif favours:
                self.favours[(npc.gx, npc.gy)] = [favours.pop().key, False]
            else:
                break
            favour_next = not favour_next

    def errand_at(self, gx, gy):
        """The errand this person hands out, or None."""
        from .quests import ERRANDS_BY_KEY
        return ERRANDS_BY_KEY.get(self.errand_givers.get((gx, gy), ""))

    def favour_at(self, gx, gy):
        """`(Favour, done)` for whoever is standing here, or None."""
        from . import story
        entry = self.favours.get((gx, gy))
        if not entry:
            return None
        favour = story.FAVOUR_BY_KEY.get(entry[0])
        return (favour, bool(entry[1])) if favour else None

    def settle_favour(self, gx, gy):
        entry = self.favours.get((gx, gy))
        if entry:
            entry[1] = True

    def _outpost_spot(self, clearance=5):
        """Somewhere with room, off the road and away from everything."""
        for _ in range(300):
            gx = rng.rint(6, WORLD_W - 7)
            gy = rng.rint(6, WORLD_H - 7)
            if not self.on_land(gx, gy):
                continue
            if self.town_rect.inflate(18, 18).collidepoint(gx, gy):
                continue
            if any(max(abs(gx - ox), abs(gy - oy)) < 16
                   for (ox, oy) in list(self.outposts)
                   + list(self.waystations) + list(self.towers)):
                continue
            # **Ground, not emptiness.** The first version demanded an
            # 11x11 patch with no props in it, which on an island covered
            # in trees and rocks is almost nowhere - five seeds produced
            # one hamlet between them. Somebody founding a village clears
            # the trees; what they cannot clear is water, lava and rock.
            ok = True
            for dy in range(-clearance, clearance + 1):
                for dx in range(-clearance, clearance + 1):
                    tx, ty = gx + dx, gy + dy
                    if not self.on_land(tx, ty):
                        ok = False
                        break
                    if TILES[self.grid[ty][tx]][1]:
                        ok = False          # sea, lava or mountain
                        break
                if not ok:
                    break
            if ok:
                return (gx, gy)
        return None

    # **Where a hamlet's houses can stand**, as offsets from its centre,
    # each 4 wide and 3 tall. Five slots, of which two to four are used -
    # the same "slots not rectangles" rule as the village (s61), and for
    # the same reason: overlap can be asserted once instead of reasoned
    # about per roll. None of them covers the centre or the tile the
    # person outside stands on.
    HAMLET_SLOTS = ((-5, -3), (0, -3), (-5, 1), (1, 1), (-2, -6))

    # Where a standing stone can go, as offsets. Four to seven of these,
    # so a circle is a circle rather than *the* circle.
    STONE_SLOTS = ((-2, -2), (2, -2), (-3, 1), (3, 1), (0, 3),
                   (-1, -3), (1, -3), (-3, -1), (3, -1))

    def outpost_rng(self, spot):
        """A private stream for one hamlet, ruin or circle (s63).

        **`_place_outposts` runs before `_place_chests`**, so a single
        extra draw from the shared stream here would move all twenty-two
        chests - and saves store chests by coordinate, so a run in
        progress would load with its opened ones forgotten and the Hollow
        Key unreachable. Exactly the trap s61 records for the village, one
        function along, and the same answer: roll from the seed and the
        place's own coordinates and touch nothing else.
        """
        gx, gy = spot
        return RNG((int(self.seed or 0) * 6151 + gx * 251 + gy * 41)
                   & 0x7FFFFFFF)

    def _build_outpost(self, kind, spot, name, circle=0):
        gx, gy = spot
        here = self.outpost_rng(spot)
        self.outposts[spot] = (kind, name)
        if kind == "stones":
            # A ring of standing stones. No door, no story, no reward -
            # somebody put them here and that is all anybody knows.
            for dx, dy in ((-2, -2), (2, -2), (-3, 1), (3, 1), (0, 3)):
                tx, ty = gx + dx, gy + dy
                if self.in_bounds(tx, ty) and (tx, ty) not in self.props:
                    self.props[(tx, ty)] = "rock"
            if (gx, gy) not in self.props:
                self.props[(gx, gy)] = "shrine"
            # **Something to read, beside the thing to rest at** (s55).
            # The circle keeps its shrine; the marker is what tells you
            # anything about the place, and the two are separate because
            # resting and reading are separate reasons to walk over.
            from . import story as story_mod
            self._place_marker(gx, gy, story_mod.stone_reading(circle),
                               "Standing stones")
            return

        roofed = kind == "hamlet"
        # **The footprint here is frozen.** Everything in this function
        # runs before `_place_chests`, which rejection-samples against
        # `self.props` and against blocked tiles - so one extra rock, one
        # extra house, one flower, and the chests land somewhere else.
        # Saves store chests by coordinate, so that silently resets the
        # opened ones on every run in progress. Variety goes in
        # `_dress_outposts` instead, which runs after the chests exist.
        #
        # The one exception is the roof *material*: all three roofs block
        # identically, so which one a house wears changes nothing that
        # anything downstream measures.
        for i, (dx, dy) in enumerate(((-4, -2), (1, -3), (-1, 2))):
            hx, hy = gx + dx, gy + dy
            if not self.in_bounds(hx + 4, hy + 3):
                continue
            roof = (T_ROOF, T_ROOF2, T_THATCH)[i % 3]
            if roofed:
                self._house(hx, hy, 4, 3,
                            here.choice((T_ROOF, T_ROOF2, T_THATCH)))
            else:
                # **A ruin is a house with the roof off and the walls
                # broken**, and the second half is what makes it read.
                # Drawn as an intact rectangle it looked like a building
                # somebody had simply not finished roofing - the eye needs
                # gaps to call something a remnant. So roughly a third of
                # the wall comes out, and what is left is corners and
                # stubs standing in a stone floor.
                for wy in range(hy, hy + 3):
                    for wx in range(hx, hx + 4):
                        if not self.in_bounds(wx, wy):
                            continue
                        edge = (wy in (hy, hy + 2) or wx in (hx, hx + 3))
                        corner = (wy in (hy, hy + 2)
                                  and wx in (hx, hx + 3))
                        # Corners stay: a wall that has fallen evenly all
                        # round reads as a pattern rather than as damage.
                        fallen = edge and not corner and rng.chance(0.45)
                        self.grid[wy][wx] = (T_RUBBLE if edge and not fallen
                                             else T_STONE)
                        self.props.pop((wx, wy), None)
                # Something grew back through the floor.
                for _ in range(2):
                    rx = rng.rint(hx, hx + 3)
                    ry = rng.rint(hy, hy + 2)
                    if self.in_bounds(rx, ry) \
                            and self.grid[ry][rx] == T_STONE \
                            and (rx, ry) not in self.props:
                        self.props[(rx, ry)] = rng.choice(("rock", "flower"))
        if not roofed:
            # **The ruin gets what is left of it to read** (s55, item
            # 12b). A name only, and only for the ruins that have one
            # written - a place with no reading gets no marker rather
            # than a marker with nothing on it.
            from . import story as story_mod
            reading = story_mod.ruin_marker(name)
            if reading:
                self._place_marker(gx, gy, reading, name)
        if roofed:
            # **The person is placed first, and the scatter goes round
            # them.** The other way costs a hamlet its inhabitant when a
            # lantern happens to take the last clear tile - and
            # `_place_favours` **shuffles the NPC list**, so one villager
            # more or less advances the *shared* stream by a different
            # amount and every chest on the island moves. That is not a
            # theory: it happened on seed 7 and the baseline caught it.
            person = self._clear_spot_near(gx, gy + 1, reach=3)
            if person:
                # **They get the lines for the place they live** (2026-08-30).
                # This was `()`, so every hamlet in the valley fell through
                # to the villager default and all six said "Quiet year, so
                # far." The name is the only thing that varies about these
                # people, so it is what their line is about.
                from . import story as story_mod
                # **A name, not a category** (2026-08-31, Savvydev). This
                # used to pick from ("A woman", "A man", "Somebody"), which
                # made the only anonymous people in the valley the ones you
                # meet standing outside their own front door.
                self.npcs.append(NPC(person[0], person[1], "villager",
                                     story_mod.hamlet_person(name)
                                     or "Somebody of %s" % name,
                                     story_mod.hamlet_lines(name)))
                self.props[person] = "villager"

    def _dress_outposts(self):
        """Make no two hamlets, ruins or circles look the same (s63).

        **Everything here runs after `_place_chests` and before
        `_ensure_connectivity`**, and both halves of that matter:

        * *After the chests*, because chest placement rejection-samples
          against props and blocked tiles. A single lantern added earlier
          moves chests, and a save stores them by coordinate - so every
          run in progress would load with its opened chests forgotten.
          That is not a theory; the first version of this pass did it, on
          seed 7, and a saved baseline caught it.
        * *Before the connectivity repair*, because this adds solid things
          and the repair is what guarantees the island is still walkable.

        Each place rolls from its own private stream, so the shared one -
        which everything above has already drawn from - is untouched.
        """
        taken = {(chest.gx, chest.gy) for chest in self.chests}
        taken |= {(npc.gx, npc.gy) for npc in self.npcs}
        taken |= set(self.doors) | set(self.markers)
        added = []
        for spot, (kind, _name) in list(self.outposts.items()):
            here = self.outpost_rng(spot)
            gx, gy = spot
            if kind == "stones":
                # A circle that is not always the same circle.
                extra = list(self.STONE_SLOTS)
                here.shuffle(extra)
                for dx, dy in extra[:here.rint(1, 4)]:
                    self._dress_one(gx + dx, gy + dy, "rock", taken, added)
                continue
            # A hamlet gets lanterns and a garden; a ruin gets what has
            # grown back through it. Both are scatter rather than
            # structure - a fourth house would have to be built before the
            # chests, and it cannot be.
            look = (("lantern", "flower", "flower") if kind == "hamlet"
                    else ("rock", "flower", "flower"))
            for _ in range(here.rint(2, 5)):
                dx, dy = here.rint(-5, 5), here.rint(-4, 4)
                if max(abs(dx), abs(dy)) <= 1:
                    continue          # keep the middle of the place clear
                self._dress_one(gx + dx, gy + dy, here.choice(look),
                                taken, added)
        self._unseal_dressing(added, taken)

    def _dress_one(self, gx, gy, prop, taken, added=None):
        """One decorative prop, if that tile will have it.

        **A solid one never goes next to somebody.** `taken` keeps
        decoration off the tiles people, chests, markers and doors are
        *on*; this keeps the blocking kinds off the tiles beside them,
        because a rock dropped in the last gap around a person in a tight
        spot walls them in. The connectivity repair afterwards reopens the
        island, not the one square somebody is standing in - it measures
        the map, and one unreachable villager is not a disconnected map.
        One hamlet in 1,346 people lost that way before this guard.
        """
        if not self.in_bounds(gx, gy) or (gx, gy) in taken:
            return False
        if (gx, gy) in self.props or self.grid[gy][gx] != T_GRASS:
            return False
        if prop in BLOCKING_PROPS and any(
                (gx + dx, gy + dy) in taken
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
            return False
        self.props[(gx, gy)] = prop
        if added is not None and prop in BLOCKING_PROPS:
            added.append((gx, gy))
        return True

    def _unseal_dressing(self, added, taken):
        """Take back any decoration that walled somebody off.

        **The guard in `_dress_one` is necessary and not sufficient.** It
        keeps a rock off the tiles beside a person, and the case that got
        through was a rock three tiles away closing the corridor that
        reached them - which no local rule can see. `_ensure_connectivity`
        does not catch it either: it repairs *the island*, and one villager
        in a pocket is not a disconnected island.

        So this measures the thing it actually cares about. One flood from
        the spawn; anybody unreachable gets the decoration near them taken
        back out. It found one hamlet in 1,346 people across forty seeds,
        which is exactly the rate at which nobody would ever notice until
        it was somebody's run.
        """
        if not added:
            return
        for _ in range(4):
            reachable = self._walkable_from(self.spawn)
            stranded = [spot for spot in taken
                        if not any((spot[0] + dx, spot[1] + dy) in reachable
                                   for dx, dy in ((1, 0), (-1, 0),
                                                  (0, 1), (0, -1)))]
            if not stranded:
                return
            freed = False
            for spot in stranded:
                for prop_at in list(added):
                    if max(abs(prop_at[0] - spot[0]),
                           abs(prop_at[1] - spot[1])) <= 4:
                        self.props.pop(prop_at, None)
                        added.remove(prop_at)
                        freed = True
            if not freed:
                # Nothing of ours is near them: they were unreachable
                # before this pass ran, and that is not this pass's to
                # fix. Four such markers exist across forty seeds.
                return

    def _walkable_from(self, start):
        """Flood the island on foot. Used by the dressing check."""
        seen, queue = {start}, deque([start])
        while queue:
            x, y = queue.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                spot = (x + dx, y + dy)
                if spot in seen or not self.in_bounds(*spot):
                    continue
                if self.blocked(*spot) or not self.on_land(*spot):
                    continue
                seen.add(spot)
                queue.append(spot)
        return seen

    def _place_marker(self, gx, gy, reading, title):
        """Stand a readable marker near (gx, gy), if there is room.

        Near rather than on, because the centre of a circle already has a
        shrine and the centre of a ruin is usually rubble. `reading` is the
        `(what stands there, lines)` pair from `story.py`; it is stored
        against the tile so the game can read it back without knowing
        which outpost it belongs to.
        """
        spot = self._clear_spot_near(gx, gy + 2, reach=4)
        if not spot:
            return False
        self.props[spot] = "marker"
        # **The place name is the title; the stone is the first line.**
        # A marker's description is a whole sentence and a dialogue
        # title ellipsizes (s44), so putting "A boundary stone, chiselled
        # and then re-chiselled" up there cut it at "and th...". The name
        # says where you are, which is what a title is for.
        what, lines = reading
        self.markers[spot] = (title, (what, "") + tuple(lines))
        return True

    def marker_at(self, gx, gy):
        return self.markers.get((int(gx), int(gy)))

    def _place_waysides(self, per_road=2):
        """Something worth stopping at on the last third of each road.

        **This is what was left of item 12.** The waystation sits at two
        thirds of the way out (`_place_waystations`), and after it the
        road is a corridor with a warden on the end: *nothing happens
        between a waystation and the warden except walking*. §42's
        Breath of the Wild note calls the answer **density of purpose** -
        something worth stopping for turns up often enough - and these
        are it.

        **Placed after `_place_chests`, and that is not a detail.**
        Placement rejection-samples against whatever is already on the
        map, saves store chests by coordinate, and a run loaded from a
        save regenerates its terrain from the seed - so anything added
        *before* the chests moves all twenty-two of them and quietly
        breaks every existing save (§61, §63). The chest baseline was
        captured before this function was written and diffed after.

        **And there is no road out here**, which is the thing to
        understand before moving these. `_carve_road` runs
        village-to-waystation and stops, because Savvydev asked for the way
        to a warden to be worked out rather than shown - so the last
        third is not a corridor, it is trackless country with a bearing
        across it.

        That makes a wayside a **landmark** rather than a roadside stop,
        and landmarks are the better half of §42's Breath of the Wild
        note: *you see a thing and go to it*. One on the line you are
        already walking is something to aim at in country that otherwise
        has nothing in it - which is worth more than the same object
        would be sitting on a paved road.
        """
        from . import story as story_mod
        for portal in self.portals:
            pool = [w.key for w in story_mod.WAYSIDES]
            rng.shuffle(pool)
            sx, sy = self.spawn
            for n in range(per_road):
                # 0.78 and 0.90: after the waystation at 0.66, and short
                # of the portal itself - the last stretch before a warden
                # should be the empty one, not the busy one.
                where = 0.78 + 0.12 * n
                gx = int(round(sx + (portal.gx - sx) * where))
                gy = int(round(sy + (portal.gy - sy) * where))
                spot = self._clear_spot_near(gx, gy, reach=5)
                if spot is None or not pool:
                    continue
                if spot in self.waysides:
                    continue
                self.props[spot] = "camp"
                self.waysides[spot] = [pool.pop(), False]
        self._scatter_waysides()

    #: How far apart two scattered waysides have to stand. Close ones
    #: read as one camp with two fires, and the point of them is that
    #: they turn up **while you are walking somewhere else**.
    WAYSIDE_SPACING = 22

    def _scatter_waysides(self, count=20):
        """The rest of them, anywhere a player might walk (item 0at).

        Savvydev, 2026-09-06: *"probably just add a lot more like that
        around the map."* Twelve, all on the last third of six roads, is
        not "around the map" - it is on the roads, which is where §72 put
        them because §72 was answering item 12 (*nothing happens between
        a waystation and the warden*). This is the wider ask.

        **Placed off the roads on purpose.** The road ones exist to break
        up a specific empty stretch; these exist to be **found**, which
        means they want to be where you were not sent. The spacing rule
        is the same one the towers have and for the same reason: two
        close together are one landmark drawn twice.

        Runs inside `_place_waysides`, which runs after `_place_chests` -
        so this cannot move a chest either (§61, §63). The baseline was
        captured before this function existed and diffed after.
        """
        from . import story as story_mod
        keys = [w.key for w in story_mod.WAYSIDES]
        placed = [spot for spot in self.waysides]
        # **Only where you could already stand next to it.** The
        # connectivity repair does carve to a stranded wayside - they
        # are in its sweep (§72) - but a repair is a last resort, and
        # three in twenty-five seeds still came out walled off because a
        # pass cannot cut through the things it is protecting. A camp
        # that has to be dug out is a camp that should have been put
        # somewhere else: one flood, and every candidate is tested
        # against it.
        reach = self._reachable_from(self.spawn)
        for _ in range(count * 60):
            if len(self.waysides) >= len(placed) + count:
                break
            gx = rng.rint(4, WORLD_W - 5)
            gy = rng.rint(4, WORLD_H - 5)
            if not self.on_land(gx, gy):
                continue
            if self.blocked(gx, gy) or (gx, gy) in self.props:
                continue
            if self.town_rect.inflate(16, 16).collidepoint(gx, gy):
                continue
            if any(max(abs(gx - px), abs(gy - py)) < self.WAYSIDE_SPACING
                   for px, py in self.waysides):
                continue
            if not self._beside((gx, gy), reach):
                continue
            # **Open ground only, and this is a correctness rule rather
            # than a taste one.** A camp is a *blocking* prop, and a
            # blocking prop dropped into a one-tile mountain pass closes
            # it - `TestGettingThere` found a scattered camp walling off
            # the Night Warden on seed 77. The connectivity repair could
            # not undo it either, because a wayside is in the set that
            # repair protects (§72), so the camp it needed to remove was
            # the one thing it was forbidden to touch.
            #
            # Requiring room on nearly every side means a camp can never
            # *be* the pass. It is also where a camp belongs: somebody
            # stopping for the night picks a clearing, not a crevice.
            if self._open_around(gx, gy) < 7:
                continue
            self.props[(gx, gy)] = "camp"
            self.waysides[(gx, gy)] = [rng.choice(keys), False]

    def _open_around(self, gx, gy):
        """How many of the eight neighbours can be walked on."""
        room = 0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if not dx and not dy:
                    continue
                tx, ty = gx + dx, gy + dy
                if not self.in_bounds(tx, ty) or not self.on_land(tx, ty):
                    continue
                if self.blocked(tx, ty) or (tx, ty) in self.props:
                    continue
                room += 1
        return room

    def wayside_at(self, gx, gy):
        """The wayside standing here, and whether it has been used."""
        from . import story as story_mod
        entry = self.waysides.get((int(gx), int(gy)))
        if not entry:
            return None
        stop = story_mod.WAYSIDE_BY_KEY.get(entry[0])
        return (stop, bool(entry[1])) if stop else None

    def settle_wayside(self, gx, gy):
        """Spend it. A wayside is a decision, and it is made once."""
        entry = self.waysides.get((int(gx), int(gy)))
        if entry:
            entry[1] = True
            return True
        return False

    def _place_chests(self, count=22):
        """Twenty-two, on a map four times the size (s42).

        **The count is held rather than scaled, because it is an ending.**
        `THE LONG WALK` is reached with the Hollow Key, which comes from
        opening *every* chest - so the number of chests is what that ending
        costs. Quadrupling them would quadruple the price of an ending that
        players have already paid, which is the one thing Savvydev's
        expandability constraint forbids (s42).

        A bigger valley therefore means the same chests, further apart:
        more walking to the same ending, and the loot economy untouched.
        """
        placed = 0
        attempts = 0
        cx, cy = self.spawn
        while placed < count and attempts < count * 60:
            attempts += 1
            gx = rng.rint(3, WORLD_W - 4)
            gy = rng.rint(3, WORLD_H - 4)
            if not self.on_land(gx, gy):
                continue          # never strand the Hollow Key on an islet
            if self.blocked(gx, gy) or (gx, gy) in self.props:
                continue
            if self.town_rect.collidepoint(gx, gy):
                continue
            # Reward exploration: no free chests on the doorstep.
            if abs(gx - cx) + abs(gy - cy) < _scaled(9):
                continue
            tier = 1 + int(clamp((abs(gx - cx) + abs(gy - cy)) / 22, 0, 4))
            self.chests.append(Chest(gx, gy, tier))
            self.props[(gx, gy)] = "chest"
            placed += 1

    def _reachable_from(self, start):
        """Flood fill over walkable tiles. Used to validate the map.

        **It disagreed with the player, and that made an ending
        unwinnable** (item 22, s64). This blocked on terrain and on two
        props - `tree` and `rock` - while `blocked()` blocks on nine, so
        the validator walked through fences, lanterns, wells, signs,
        shrines and markers that a player cannot. A chest ringed by any of
        those read as reachable, the repair left it alone, and **the
        Hollow Key needs every chest**: two chests in 1,320 across sixty
        seeds were unreachable, which is `THE LONG WALK` quietly
        impossible on those seeds.

        It asks `blocked()` now, which is the same question the player's
        own movement asks. One definition, so they cannot drift again.
        """
        return self._walkable_from(start)

    @staticmethod
    def _beside(spot, reachable):
        """Can the player stand next to this, or on it?

        **The distinction that made item 22 subtle.** A marker, a portal,
        a shrine and a well are all in `BLOCKING_PROPS` - they are things
        you walk *up to*, not onto - so their own tile is never in a flood
        that uses the player's rules. Asking "is its tile reachable"
        therefore answers no for every one of them, and the first version
        of this check duly deleted all eleven markers on the island.
        """
        if spot in reachable:
            return True
        return any((spot[0] + dx, spot[1] + dy) in reachable
                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))

    def _ensure_connectivity(self):
        """Carve a way to anything the player could not otherwise get to.

        Generation is random, so this is the check that stops a run from
        starting on a map where a boss or a chest is walled off forever.

        **It used to check portals and chests only** (item 22, s64), so a
        standing stone or a hamlet inside a pocket was content nobody
        could reach. And it used a flood that disagreed with the player
        about what blocks, which let a chest ringed by a fence read as
        reachable - two chests in 1,320 across sixty seeds, each of them
        `THE LONG WALK` quietly impossible on that seed.
        """
        reachable = self._reachable_from(self.spawn)
        stranded = []
        for portal in self.portals:
            if not self._beside((portal.gx, portal.gy), reachable):
                stranded.append((portal.gx, portal.gy))
        for chest in list(self.chests):
            if not self._beside((chest.gx, chest.gy), reachable):
                stranded.append((chest.gx, chest.gy))
        # And the things you read and talk to, which were never in here.
        # **Appended after the portals and chests, never mixed in**, so the
        # carves those two already trigger happen in the same order off
        # the same stream and nothing they touch moves.
        for spot in list(self.markers):
            if not self._beside(spot, reachable):
                stranded.append(spot)
        for npc in self.npcs:
            if not self._beside((npc.gx, npc.gy), reachable):
                stranded.append((npc.gx, npc.gy))
        # And the waysides (item 12), appended last for the reason above:
        # they are placed after the chests, so anything that reordered
        # the carves in front of them would move the chests they were
        # deliberately placed behind.
        for spot in list(self.waysides):
            if not self._beside(spot, reachable):
                stranded.append(spot)

        # Everything the repair exists to reach, so a pass cannot bulldoze
        # one of them on its way to another.
        protect = {(chest.gx, chest.gy) for chest in self.chests}
        protect |= {(npc.gx, npc.gy) for npc in self.npcs}
        protect |= set(self.markers) | set(self.doors) | set(self.waysides)
        protect |= {(portal.gx, portal.gy) for portal in self.portals}
        for target in stranded:
            # Unpaved: a repair that paves is a signpost, and this is
            # exactly the road Savvydev asked not to be drawn.
            self._carve_road(self.spawn, target, paved=False, protect=protect)
        if stranded:
            reachable = self._reachable_from(self.spawn)
            # **A chest is freed, not deleted.** The count is what
            # `THE LONG WALK` costs (§42), so dropping one reprices an
            # ending - which the old code did as its last resort.
            for chest in list(self.chests):
                spot = (chest.gx, chest.gy)
                if self._beside(spot, reachable):
                    continue
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    beside = (spot[0] + dx, spot[1] + dy)
                    if beside in protect:
                        continue
                    if self.props.get(beside) in BLOCKING_PROPS:
                        self.props.pop(beside, None)
            reachable = self._reachable_from(self.spawn)
            for chest in list(self.chests):
                spot = (chest.gx, chest.gy)
                if self._beside(spot, reachable):
                    continue
                # **Moved, not deleted.** Genuinely on an island of its
                # own, and both other answers are wrong: leaving it is a
                # chest the Hollow Key waits on forever, and removing it
                # reprices the ending. Where the twenty-second chest sits
                # is promised to nobody; the count is.
                nearest = min(
                    (place for place in reachable
                     if place not in self.props and place not in self.doors),
                    key=lambda place: (abs(place[0] - spot[0])
                                       + abs(place[1] - spot[1])),
                    default=None)
                if nearest is None:
                    continue
                self.props.pop(spot, None)
                chest.gx, chest.gy = nearest
                self.props[nearest] = "chest"
        return len(stranded)

    def generate(self):
        # Terrain is rebuilt from the seed on load rather than stored, so the
        # generator must start from the same RNG state every time or a loaded
        # save would find its chests and portals somewhere else entirely.
        if self.seed is not None:
            rng.reseed(self.seed)
        self.props.clear()
        self.chests.clear()
        self.portals.clear()
        self.waystations.clear()
        self.lairs.clear()
        self.towers.clear()
        self.stepstones.clear()
        self.outposts.clear()
        self.markers.clear()
        # **`generate` runs again on every load** (hard rule 2.4), so a
        # store that is not cleared here accumulates: twelve waysides
        # became twenty-three on the second call and thirty-four on the
        # third, all of them at stale coordinates.
        self.waysides.clear()
        self.favours.clear()
        self.errand_givers.clear()
        self._shape_island()
        # Coarse: the survey edge should wander over tens of tiles, not
        # fray into static.
        self.survey = self._noise(20)
        self._assign_biomes()
        self._fill_terrain()
        self._raise_mountains()
        self._scatter_props()
        self._build_town()
        self._place_portals()
        self._place_waystations()
        # **The road reaches the waystation and stops there** (s42).
        # Savvydev: *"dont just add clear roads to the wardens, let players
        # figure it out themselves how to get to them"*. So the network
        # is village-to-village; past the last inhabited place you are on
        # your own, with the objective marker giving a bearing and the
        # ridges giving you something to solve.
        for spot in self.waystations:
            self._carve_road(self.spawn, spot)
        self._place_runners()
        self._place_lairs()
        self._place_towers()
        self._place_outposts()
        self._place_favours()
        self._place_chests()
        # **After the chests, always** - see the docstring. Adding
        # anything before them moves all twenty-two and breaks saves.
        self._place_waysides()
        # **After the chests, before the repair** - see `_dress_outposts`.
        self._dress_outposts()
        self.repairs = self._ensure_connectivity()
        if self.seed is not None:
            # Hand gameplay its own deterministic stream, distinct from the
            # one terrain consumed, so loot is reproducible but not a mirror
            # of the map generation sequence.
            rng.reseed((self.seed * 7919 + 13) % (2 ** 31))
        return self

    # -- points of interest -------------------------------------------------

    def portal_at(self, gx, gy):
        for portal in self.portals:
            if portal.gx == gx and portal.gy == gy:
                return portal
        return None

    def chest_at(self, gx, gy):
        for chest in self.chests:
            if chest.gx == gx and chest.gy == gy:
                return chest
        return None

    def npc_at(self, gx, gy):
        for npc in self.npcs:
            if npc.gx == gx and npc.gy == gy:
                return npc
        return None

    def interactable_near(self, gx, gy):
        """What the player would interact with from where they stand."""
        for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1),
                       (1, 1), (-1, 1), (1, -1), (-1, -1)):
            tx, ty = gx + dx, gy + dy
            prop = self.props.get((tx, ty))
            if prop == "portal":
                return ("portal", self.portal_at(tx, ty))
            if prop == "chest":
                return ("chest", self.chest_at(tx, ty))
            if prop in ("elder", "smith", "runner", "villager", "keeper"):
                return ("npc", self.npc_at(tx, ty))
            if prop == "lair":
                return ("lair", (tx, ty))
            if prop == "tower":
                return ("tower", (tx, ty))
            if prop == "stepstone":
                return ("stepstone", (tx, ty))
            if prop == "shrine":
                return ("shrine", (tx, ty))
            if prop == "marker":
                return ("marker", (tx, ty))
            if prop == "hoard":
                return ("hoard", (tx, ty))
            # **A wayside was never found here** (s96). `interact` and the
            # prompt both had a `camp` branch and `PROP_INTERACT` listed
            # it, but this scan - the one thing both ask - had no line for
            # it: 32 stops on an island, no prompt at any of them, and E
            # said "Nothing to interact with here". Its tests all called
            # `stop_at_wayside` directly, which is how it went unseen.
            if prop == "camp" and (tx, ty) in self.waysides:
                return ("camp", (tx, ty))
            if (tx, ty) in self.doors:
                return ("door", ((tx, ty), self.doors[(tx, ty)]))
            if self.tile_at(tx, ty) == T_SHOP:
                return ("shop", (tx, ty))
        return (None, None)

    # **How far above you each tier fights.** Savvydev, 2026-08-29:
    #
    # > "the level of enemies should be dependent on the level of the
    # > player... and some areas should be like hard no matter what the
    # > player level is but still like doable, so like a hard area's
    # > enemies would be like the player's level +15 to +20"
    #
    # So a tier stopped being an absolute level and became **a distance
    # from wherever you are**. The country round the village is a little
    # under you at every level; the frontier is +18 whether you arrive at
    # ten or at ninety, which is what makes it stay frightening instead of
    # becoming scenery you out-grow.
    #
    # The absolute floor is what stops a level-one hero finding level -2
    # slimes; the ceiling is `MAX_LEVEL`.
    TIER_OFFSET = (-3, 0, 3, 7, 12, 18)

    def suggested_level(self, gx, gy, player_level=1):
        # **No import in here.** This runs on every encounter check and on
        # every tile a measurement tool walks; a function-level import made
        # a 17,000-call test take minutes.
        tier = biome_tier(self.biome_at(gx, gy).key)
        offset = self.TIER_OFFSET[min(tier, len(self.TIER_OFFSET) - 1)]
        return int(clamp(player_level + offset, 1, MAX_LEVEL))

    def wardens_defeated(self):
        return sum(1 for p in self.portals if p.defeated)

    def all_chests_opened(self):
        return bool(self.chests) and all(c.opened for c in self.chests)

    def remove_shrines(self):
        """Take every shrine out of the valley.

        **Hollowed used to leave them standing and refuse** (2026-08-31).
        A stone you can walk up to, press E on, and be told no is the same
        wrong shape as a Flee button that will not flee - it reads as a
        broken shrine rather than as the modifier you chose.

        **Called after every `generate()`, never stored.** The terrain
        regenerates from the seed on load (hard rule 4), so this has to be
        re-applied rather than saved; `Game.strip_world_for_modifiers` is
        the one caller and it runs on both paths.
        """
        for spot in [s for s, prop in self.props.items() if prop == "shrine"]:
            self.props.pop(spot, None)
            gx, gy = spot
            if self.grid[gy][gx] != T_SHRINE:
                continue
            # **Put back the floor that was there, not grass** (2026-09-01).
            # Savvydev: *"the shrine got removed due to the modifier (good) but
            # now theres a random grass tile in the village"*. The village
            # floor is boards and the shrine sits in the middle of them, so
            # a grass square reads as a hole rather than as an absence.
            #
            # The neighbours know what this tile should be: whatever most of
            # them are. Ties go to the first, which on a board floor is a
            # board and out in the open is grass.
            around = []
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0),
                           (1, 1), (-1, -1), (1, -1), (-1, 1)):
                if self.in_bounds(gx + dx, gy + dy):
                    tile = self.grid[gy + dy][gx + dx]
                    if tile not in (T_SHRINE, T_WALL, T_WATER):
                        around.append(tile)
            self.grid[gy][gx] = (max(set(around), key=around.count)
                                 if around else T_GRASS)

    def flag(self, name):
        """Set a one-shot story flag. True the first time only."""
        if name in self.story_flags:
            return False
        self.story_flags.add(name)
        return True
