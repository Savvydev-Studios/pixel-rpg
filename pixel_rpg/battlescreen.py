"""Battle rendering and input.

The Battle object owns the rules; this module owns how the fight looks and how
keys map onto it. Splitting them means the combat maths can be unit tested
without a display.
"""
from __future__ import annotations

import math

import pygame

from .battle import (ACTING, INTRO, MENU, MENU_ACTIONS, SUBMENU,
                     menu_actions_for)
from .entities import Boss
from .items import CONSUMABLES, ELEMENT_COLOR
from .ui import (ACCENT, BAD, DIM, FAINT, GOOD, INK, bar, draw_text,
                 highlight, hp_color, key_hint, panel, status_chips)
from .assets import vertical_gradient
from .util import clamp, ease_out_cubic, pulse, wrap_text


def arena_colors(battle):
    """The gradient behind a fight, in priority order.

    A boss brings its own; **a Last Stand arena brings its own** (item
    0am, s72); otherwise the colour comes from what you are fighting.
    The wave arena sits above the element rule rather than below it
    because in that mode the *place* is the thing that changes and the
    monsters are what the place contains - reading the colour off a
    slime would put the same green behind The Glassworks and the
    Caldera Lip.
    """
    if isinstance(battle.enemy, Boss):
        base = battle.enemy.definition.arena
        return base, tuple(int(c * 0.45) for c in base)
    wash = getattr(battle, "arena_tint", None)
    if wash:
        # **Not as dark as it wants to be.** At 0.34/0.06 the sky was
        # fine and everything derived from it came out near black, so
        # the bottom half of the screen was a void with sprites floating
        # in it - which only a full-size screenshot shows, because a
        # contact sheet is mostly sky (rule 31.4, twice in one feature).
        return (tuple(int(c * 0.42) for c in wash),
                tuple(int(c * 0.12) for c in wash))
    tint = ELEMENT_COLOR.get(battle.enemy.element, (120, 120, 140))
    top = tuple(int(c * 0.30) for c in tint)
    return top, (14, 13, 22)


# --------------------------------------------------------------- backdrop ---
#
# **Item 0ar** (2026-09-06). Savvydev, on the arenas §72 had just built:
#
# > "also the battle background can be nicer tbh, its too simple"
#
# He is right, and §72 answered a smaller question than the one he asked.
# A battle backdrop has been **a two-colour gradient and one flat
# rectangle of ground** since the battle screen existed; painting that
# rectangle eighteen different colours makes eighteen simple backdrops.
#
# What a place actually needs is **depth**: something between you and the
# sky, and more than one of it. So a backdrop is now four layers -
#
#   sky -> far silhouette -> near silhouette -> ground, with a lip
#
# - plus drifting motes in front of the lot. The silhouettes are drawn
# from the arena's own colour at three darknesses, which is what keeps
# eighteen of these looking like one game.
#
# **All of it is arithmetic on polygons** (hard rule 1: no binary
# assets), and **all of it is cached per size and colour** - a backdrop
# is rebuilt when the window changes, not sixty times a second.


def arena_scene(battle):
    """Which skyline a fight stands in front of.

    **Every battle gets one, not just a wave.** He asked for *the battle
    background* to be nicer, not the wave one - and an island fight is
    the fight nearly every player is in. A wave says which place it is;
    everywhere else it comes off what you are fighting, which is the same
    rule `arena_colors` has always used for the colour.
    """
    if isinstance(battle.enemy, Boss):
        return "spires"
    named = getattr(battle, "arena_scene", "")
    if named:
        return str(named)
    return {"fire": "crags", "ice": "arches", "thunder": "spires",
            "shadow": "trees", "holy": "arches"}.get(
                getattr(battle.enemy, "element", ""), "hills")


#: How many skylines each place has. **Item 0bd**, his words: *"make 3
#: more for each existing background, that way if ur in one area for a
#: long time, the background varies"*. Four seeds x eighteen places is
#: seventy-two, and it costs one number because a silhouette is
#: generated rather than drawn.
ARENA_VARIANTS = 4


def arena_seed_of(battle):
    """A stable number for this place's skyline.

    Off the *scene name*, so a place keeps its own family of hills
    rather than borrowing another's - 0aq's rule that scenery you cannot
    rely on is not scenery.

    **And then one of four inside that family** (item 0bd). The variant
    comes off `Battle.arena_variant`, which is set once when the fight
    opens and never moves, so a backdrop is fixed for the length of a
    battle and different between battles. That is exactly the case he
    described: *"if ur in one area for a long time"* - the island, where
    ten fights in a row happen in the same biome.
    """
    family = abs(hash(arena_scene(battle))) % 9973
    variant = int(getattr(battle, "arena_variant", 0)) % ARENA_VARIANTS
    return family + variant * 1009


def _shade(colour, amount):
    """One colour at a given darkness, kept out of gamut trouble."""
    return tuple(max(0, min(255, int(c * amount))) for c in colour)


def _ridge(surface, colour, base_y, seed, height, step, jag=0.0):
    """A skyline: one polygon, walked left to right on a fixed stream.

    Its own `RNG` rather than the shared one, seeded from the arena, so
    a backdrop cannot move the stream a fight rolls off - §21's wasted
    `rng.choice`, which moved four classes, is the reason that sentence
    is in every file that draws.

    **The last point is forced back to the first's height**, which is
    what lets the layer be blitted twice side by side and wrap: the seam
    is a continuation rather than a step (item 0be).
    """
    from .util import RNG
    width = surface.get_width()
    rng_here = RNG(seed)
    points = [(0, surface.get_height())]
    first = None
    x = 0
    while x <= width:
        y = base_y - rng_here.rfloat(0.0, height)
        if first is None:
            first = y
        if jag:
            # A peak rather than a hump: two points close together makes
            # the difference between a hill and a spire.
            points.append((x, base_y - rng_here.rfloat(0.0, height * jag)))
        points.append((x, int(y)))
        x += step
    points.append((width, int(first)))
    points.append((width, surface.get_height()))
    pygame.draw.polygon(surface, colour, points)


#: How each silhouette layer is shaped, per kind: the far layer then the
#: near one, as (height fraction, step divisor, jag). **Data rather than
#: a branch per kind**, because 0bd asks for four variants of eighteen
#: places and a chain of `elif`s is not something you want seventy-two
#: of.
SKYLINES = {
    "hills": ((0.18, 9, 0.0), (0.11, 13, 0.0)),
    "spires": ((0.30, 26, 1.9), (0.20, 40, 2.4)),
    "trees": ((0.22, 30, 1.6), (0.30, 55, 2.6)),
    "crags": ((0.34, 10, 1.2), (0.20, 16, 1.4)),
    "arches": ((0.16, 14, 0.0), (0.00, 0, 0.0)),
    "flat": ((0.00, 0, 0.0), (0.00, 0, 0.0)),
}


def _layer_surface(size, colour, kind, seed, near):
    """One silhouette on its own transparent surface, ready to wrap."""
    width, height = size
    surface = pygame.Surface(size, pygame.SRCALPHA)
    horizon = int(height * 0.66)
    shape = SKYLINES.get(kind, SKYLINES["hills"])[1 if near else 0]
    tall, divisor, jag = shape
    if kind == "arches" and near:
        # Uprights and a lintel, repeated: a colonnade rather than a hill.
        span = max(60, width // 7)
        for x in range(-span // 2, width + span, span):
            pygame.draw.rect(surface, colour,
                             (x, horizon - int(height * 0.22),
                              max(4, span // 9), int(height * 0.22)))
            pygame.draw.rect(surface, colour,
                             (x, horizon - int(height * 0.22),
                              span - span // 4, max(3, height // 90)))
        return surface
    if not tall:
        return surface
    _ridge(surface, colour, horizon, seed, height * tall,
           max(9, width // max(1, divisor)), jag=jag)
    return surface


def backdrop_layers(assets, size, top, kind, seed):
    """The two drifting silhouettes for one place, cached."""
    out = []
    for near in (False, True):
        key = ("layer", size, top, kind, seed, near)
        if key not in assets._misc:
            shade = 0.34 if near else 0.56
            assets._misc[key] = _layer_surface(
                size, _shade(top, shade), kind, seed + (7 if near else 0),
                near)
        out.append(assets._misc[key])
    return out


def backdrop_ground(assets, size, top, bottom, ground_c):
    """Sky and ground: the half of a backdrop that does not move."""
    key = ("ground", size, top, bottom, ground_c)
    if key in assets._misc:
        return assets._misc[key]
    width, height = size
    surface = pygame.Surface(size)
    surface.blit(vertical_gradient(size, top, bottom), (0, 0))
    horizon = int(height * 0.66)
    floor = pygame.Rect(0, horizon, width, height - horizon)
    pygame.draw.rect(surface, ground_c, floor)
    for n in range(1, 4):
        band = pygame.Rect(0, horizon + (height - horizon) * n // 4,
                           width, (height - horizon) // 4 + 1)
        pygame.draw.rect(surface, _shade(ground_c, 1.0 - 0.11 * n), band)
    assets._misc[key] = surface
    return surface


#: How fast each silhouette layer slides, in pixels a second. **The far
#: one is slower**, which is the whole of parallax and the reason the two
#: are separate surfaces at all (item 0be).
DRIFT = (5.0, 13.0)


def draw_backdrop(surface, assets, top, bottom, kind, seed, elapsed):
    """Sky, two drifting silhouettes, ground - and the air in front.

    **Item 0be** (2026-09-06): *"the background should also move, idk if
    it does or not."* It did not - §73 built four layers and cached them
    into one static surface, so the only thing moving was the motes.

    Each silhouette is its own transparent layer now, blitted **twice**
    at an offset that wraps, and the far one moves at a third of the
    near one's speed. `_ridge` forces its last point back to its first
    height so the two copies meet without a step.
    """
    size = surface.get_size()
    width = size[0]
    surface.blit(backdrop_ground(assets, size, top, bottom,
                                 _shade(top, 1.30)), (0, 0))
    layers = backdrop_layers(assets, size, top, kind, seed)
    for layer, speed in zip(layers, DRIFT):
        shift = int(elapsed * speed) % max(1, width)
        surface.blit(layer, (-shift, 0))
        surface.blit(layer, (width - shift, 0))
    horizon = int(size[1] * 0.66)
    # A lit edge along the horizon, drawn **after** the silhouettes so a
    # hill does not sit on top of the line it is standing on.
    pygame.draw.line(surface, _shade(top, 1.9), (0, horizon),
                     (width, horizon), 2)


#: What drifts in front of a place, per silhouette kind. Deliberately
#: small numbers: motes are the thing that says *this air is not still*,
#: and past about thirty they stop being air and start being weather.
MOTES = {"spires": (26, (250, 190, 120), 14.0),
         "arches": (18, (220, 220, 240), 7.0),
         "trees": (20, (200, 240, 200), 9.0),
         "crags": (22, (240, 220, 190), 11.0),
         "hills": (16, (240, 245, 220), 8.0),
         "flat": (12, (235, 235, 250), 6.0)}


def draw_motes(surface, kind, elapsed, alpha=70):
    """Slow drift in front of the backdrop, on a closed-form path.

    **No particle list and no state**: every mote's position is a
    function of the clock, so this costs nothing to start, nothing to
    stop, and cannot leak between fights the way a list would.
    """
    count, colour, speed = MOTES.get(kind, MOTES["hills"])
    width, height = surface.get_size()
    layer = pygame.Surface((width, height), pygame.SRCALPHA)
    for i in range(count):
        phase = i * 2.39996                      # golden angle: no clumps
        x = (math.sin(phase) * 0.5 + 0.5) * width
        x = (x + elapsed * speed * (0.5 + (i % 3) * 0.4)) % (width + 20) - 10
        y = ((math.cos(phase * 1.7) * 0.5 + 0.5) * height * 0.62
             + math.sin(elapsed * 0.6 + phase) * 6.0)
        size = 1 + (i % 3)
        pygame.draw.rect(layer, colour + (alpha,), (int(x), int(y), size, size))
    surface.blit(layer, (0, 0))


def draw_target_marker(surface, assets, spot, battle):
    """Show which one an attack would land on.

    Only drawn when there is a choice to make. A pulse rather than a static
    bracket, so it reads as "aimed here" and not as part of the sprite.
    """
    pulse = 0.55 + 0.45 * math.sin(battle.elapsed * 5.0)
    color = (250, 200, 96)
    bracket = 9
    box = spot.inflate(14, 14)
    layer = pygame.Surface((box.w, box.h), pygame.SRCALPHA)
    alpha = int(140 + 90 * pulse)
    for cx, cy, dx, dy in ((0, 0, 1, 1), (box.w - 1, 0, -1, 1),
                           (0, box.h - 1, 1, -1), (box.w - 1, box.h - 1, -1, -1)):
        pygame.draw.line(layer, (*color, alpha), (cx, cy),
                         (cx + dx * bracket, cy), 2)
        pygame.draw.line(layer, (*color, alpha), (cx, cy),
                         (cx, cy + dy * bracket), 2)
    surface.blit(layer, box.topleft)

    # **Whose aim this is** (item 0ax, s74). Savvydev: *"show which player
    # has which enemy selected"*. A fight is sequential, so at any moment
    # exactly one person has a target - the one whose turn it is - and
    # the useful thing is not *that* something is aimed at but **who is
    # aiming**. Solo that is always you and the label would be noise, so
    # it only appears when there is somebody else it could be.
    if len(battle.players) < 2:
        return
    index = battle.acting_player
    if not 0 <= index < len(battle.players):
        return
    who = battle.players[index]
    mine = index == battle.viewer
    # **It asks the layout where it is allowed to go**, rather than
    # picking a side and hoping. Above is the band the log, the turn bar
    # and the enemy roster live in; below is the menu. On an 844x390
    # window both are occupied and there is genuinely nowhere - so it
    # draws nothing, which is the right answer and the one a fixed
    # offset cannot give. `layout.py --phone` found each of these in
    # turn; the third time is when guessing stops being reasonable.
    label = "you" if mine else who.name[:12]
    panels = battle_panels(battle, *surface.get_size())
    blockers = [panels[key] for key in ("enemy", "menu", "log", "turns")
                if key in panels and isinstance(panels[key], pygame.Rect)]
    for y in (box.bottom + 10, box.y - 8):
        line = pygame.Rect(box.centerx - 46, y - 7, 92, 14)
        if any(line.colliderect(other) for other in blockers):
            continue
        draw_text(surface, assets, label, (box.centerx, y), "tiny",
                  ACCENT if mine else (250, 200, 96), center=True)
        return


def enemy_positions(battle, width, height):
    """Where each monster stands, and how big it draws.

    A lone enemy keeps exactly the spot it always had, so single fights look
    untouched. A group fans out around that same centre and staggers in depth,
    because a straight line hides the back rank behind the front one.
    """
    count = len(battle.enemies)
    if count == 1:
        scale = 5 if isinstance(battle.enemies[0], Boss) else 4
        return [(width * 0.72, height * 0.40, scale)]

    scale = 4 if count <= 3 else 3
    span = min(width * 0.42, 110 + count * 44)
    left = width * 0.72 - span / 2
    step = span / (count - 1)
    places = []
    for i in range(count):
        depth = 16 if i % 2 else -12
        places.append((left + step * i, height * 0.40 + depth, scale))
    return places


def shaped(sprite, pose):
    """Apply squash, stretch and fade to a sprite without redrawing it.

    Volume is roughly kept: a body that stretches tall gets narrower, and one
    that squashes flat gets wider. That is what sells the weight of a blow -
    a sprite that only grows or only shrinks reads as a zoom.

    Scaling is skipped entirely when the pose is neutral, which is almost
    every frame for almost every body, so the common case costs one float
    comparison rather than a surface copy.
    """
    if abs(pose.stretch - 1.0) > 0.01:
        w, h = sprite.get_size()
        tall = clamp(pose.stretch, 0.35, 1.6)
        wide = 1.0 / tall
        sprite = pygame.transform.scale(
            sprite, (max(1, int(w * wide)), max(1, int(h * tall))))
    if pose.alpha < 0.995:
        sprite = sprite.copy()
        sprite.set_alpha(int(clamp(pose.alpha, 0.0, 1.0) * 255))
    return sprite


# Below this the battle screen cannot hold a panel on the left and another
# on the right; it stacks them instead. The design width is 1280 and every
# panel in here was a fixed 376 or 446 across, which at 390 put three of
# them at a negative x.
BATTLE_STACK_WIDTH = 700


def battle_panels(battle, width, height):
    """Where every panel on the battle screen goes.

    One place, because there are five of them and they have to agree. Wide,
    they sit in two columns exactly where they always have; narrow, they
    stack down the screen, which is the only arrangement that fits a phone
    held upright.
    """
    rows = max(0, len(battle.players) - 1)
    strip_h = rows * 26 + 16 if rows else 0
    # **The enemy plate is as tall as the pack** (item 0av, s74). It used
    # to be a fixed 92 showing one monster - whichever was targeted - so
    # in a pack of five you could not see what was nearly dead without
    # walking the cursor over every one of them. It shows all of them
    # now, so its height has to follow the count.
    plate_h = enemy_plate_height(len(battle.enemies))
    # **The turn bar** (item 0h, 2026-09-03). Wide, it takes the empty
    # column between the log and the enemy plate, which is the only piece
    # of this screen nothing has ever used. Stacked, it goes under the log,
    # because a phone has no middle column at all.
    if width >= BATTLE_STACK_WIDTH:
        # The strip grows downward from the party size and the log has to
        # give way to it: four players on a 390-tall window put the strip
        # inside the log box, and the log is the thing that can be shorter.
        strip_top = height - 218 - strip_h
        # **The roster stops above whatever is under it.** The submenu is
        # bottom-anchored and capped (see below), so on a 390-tall window
        # it starts at 130 - and a five-monster roster wants 162, which
        # put four rows straight through it. `layout.py --phone` found
        # this the moment the plate stopped being a fixed 92.
        sub_top = max(130, height - 330)
        capped = max(74, min(plate_h, sub_top - 32))
        return {
            "log": pygame.Rect(24, 24, 360,
                               max(46, min(150, strip_top - 32))),
            "enemy": pygame.Rect(width - 400, 24, 376, capped),
            # **The empty column between the log and the enemy plate.** It
            # is the only piece of this screen nothing has ever used, and
            # the turn bar wants to be near the top and near the middle -
            # you look at it to answer "am I up?", not to read it.
            # **It stops short of the enemy plate** (item 0bm). His
            # screenshot: *"bar and hp kinda overlap"*. The bar was
            # `width - 792` wide from x=396, so its right edge landed on
            # `width - 396` and the plate starts at `width - 400` - four
            # pixels *inside* it, at every window size, since the turn
            # bar existed.
            #
            # `layout.py` never had an opinion: a panel overlapping a
            # panel is not one of its rules (they are about text), which
            # is the same blind spot §51 found when the enemy plate
            # covered a party's top row.
            # **`max(0, ...)`, not `max(60, ...)`.** A 60-pixel floor is
            # a floor on the *width*, and at 700 across it put the bar's
            # left edge past the plate's - which is the same overlap
            # again, arriving from the other side. `draw_turn_bar`
            # already declines to draw below 60, so nothing is lost by
            # letting this reach zero.
            "turns": pygame.Rect(396, 24, max(0, width - 808), 32),
            "strip": pygame.Rect(24, strip_top, 376, strip_h),
            "player": pygame.Rect(24, height - 210, 376, 108),
            "menu": pygame.Rect(width - 400, height - 210, 376, 186),
            # Bottom-anchored and capped, so on a short window it stops
            # below the enemy plate instead of covering it. At the design
            # height this is the rect it always was.
            "submenu": pygame.Rect(
                width - 470, sub_top, 446,
                min(306, height - sub_top)),
            "stacked": False,
        }
    pad = 14
    inner = width - pad * 2
    menu_h = min(186, max(120, height // 3))
    # Stacked there is no middle column, so the bar takes a strip of its
    # own under the log and everything below it moves down by exactly that
    # much. A phone in portrait has the vertical room; the one thing that
    # must not happen is the bar landing on the enemy's health.
    bar_h = 26
    return {
        "log": pygame.Rect(pad, pad, inner, 104),
        "turns": pygame.Rect(pad, pad + 110, inner, bar_h),
        "enemy": pygame.Rect(pad, pad + 112 + bar_h + 4, inner,
                             min(plate_h, 82 + 3 * ROSTER_ROW)),
        "strip": pygame.Rect(pad, height - menu_h - 118 - strip_h, inner,
                             strip_h),
        "player": pygame.Rect(pad, height - menu_h - 112, inner, 100),
        "menu": pygame.Rect(pad, height - menu_h - 6, inner, menu_h),
        # The band between the enemy plate and the player plate - the only
        # part of a stacked screen nothing else is using.
        # **Below the plate, whatever height the plate turned out to
        # be.** It was written as a literal 202 - the old fixed plate
        # plus its gap - so a taller plate would have been drawn on by
        # the submenu rather than pushing it down.
        "submenu": pygame.Rect(
            pad, pad + 116 + bar_h + min(plate_h, 82 + 3 * ROSTER_ROW) + 4,
            inner,
            max(120, height - menu_h - 122 - pad - 202 - bar_h - 4)),
        "stacked": True,
    }


def party_positions(battle, width, height):
    """Where each player stands, and how big they draw.

    A lone hero keeps exactly the spot it always had, so solo fights look
    untouched - the same promise `enemy_positions` makes for a lone monster.

    A party of three or four forms **two columns**, not one. A single column
    was the obvious thing and it was wrong: four sprites at 48px in the band
    left between the log box and the party strip gave each of them about
    forty pixels, so they overlapped each other and their own name tags. The
    layout checker passed it - it only looks at text and panels - and the
    screenshot did not.
    """
    count = len(battle.players)
    if count == 1:
        return [(width * 0.28, height * 0.52, 5)]

    scale = 4 if count <= 2 else 3
    # The left column of the screen is already spoken for: the log box ends
    # at y=174, and the party strip and plate own the bottom. A party stands
    # in what is left between them.
    places = battle_panels(battle, width, height)
    top = places["log"].bottom + 24 + scale * 8
    if places["stacked"]:
        # **Below the enemy plate, not just below the log.** Stacked, that
        # plate is a full-width band rather than a corner panel, so "below
        # the log" put the top row of a party *behind* it - only the tops
        # of their heads showed, above its edge. It had been doing that
        # since the stacked layout existed and no checker sees it: the
        # panel is drawn after the sprites, so nothing is covering text and
        # nothing overflows anything.
        top = max(top, places["enemy"].bottom + 20 + scale * 8)
    else:
        top = max(top, 206 + scale * 8)
    bottom = min(places["strip"].top, places["player"].top) - 20 - scale * 8
    if bottom - top < 60:
        # Four heroes and five monsters on a 390-tall window leaves about
        # eight pixels between the strip and the plate. Nothing fits, so
        # **only the viewer is drawn** and the strip carries the rest -
        # which it already does, with names and health. Overlapping the
        # panels instead would hide the numbers the fight is decided on.
        y = (places["log"].bottom + places["player"].top) / 2.0
        return [(width * 0.28, y, scale) if i == battle.viewer else None
                for i in range(count)]

    cols = 1 if count <= 2 else 2
    rows = -(-count // cols)                   # ceiling division
    gap = (bottom - top) / max(1, rows - 1)
    places = []
    for i in range(count):
        col, row = i % cols, i // cols
        places.append((width * 0.28 + (col - (cols - 1) / 2.0) * 72,
                       top + gap * row, scale))
    return places


def party_strip_rect(battle, width, height):
    """Where the other party members' health sits.

    Anchored to the bottom of the screen just above the plate rather than to
    a fixed offset, so that it is the same distance from it whether there is
    one other person in the fight or three.
    """
    return battle_panels(battle, width, height)["strip"]


def combatant_anchor(battle, who, index, size):
    """Where a damage number or a puff of dust belongs.

    Floats used to be pinned to two fixed points on the screen, which was
    fine when a fight had exactly two sides of one. With groups on both
    sides an unplaced number is a number over the wrong body.
    """
    width, height = size
    places = (party_positions(battle, width, height) if who == "player"
              else enemy_positions(battle, width, height))
    if not places:
        return width * 0.5, height * 0.5
    i = 0 if index is None else int(index)
    i = max(0, min(i, len(places) - 1))
    spot = places[i] or next((p for p in places if p), None)
    if spot is None:
        return width * 0.5, height * 0.5
    return spot[0], spot[1]


def draw_command(game, battle, width, height):
    """The timing window, as a bar that closes onto a mark.

    **A shrinking bar rather than a countdown number.** The thing being
    asked for is a *beat*, and a number cannot be read on a beat - the eye
    has to be able to take it in without leaving the fight. The mark is
    where perfect is; the bar is where you are.

    Drawn near the bottom, above the log and clear of the menu, because
    during `ACTING` the menu is gone and that strip is empty.
    """
    command = battle.command
    if not command:
        return
    span = command["closes"] - command["opens"]
    if span <= 0:
        return
    at = (battle.elapsed - command["opens"]) / span
    if at < -0.35 or at > 1.35:
        return
    at = clamp(at, 0.0, 1.0)

    bar_w = min(320, width - 80)
    # **Above the player plate, measured rather than guessed.** A flat 70%
    # of the height put it straight through the plate at 390x844, where
    # the plate is tall and the arena is short - `layout.py --phone` found
    # it burying two lines of the hero's own HP and MP. `battle_panels` is
    # the one place that knows where that plate ends up at any size, so
    # this asks it and sits a clear 44 above, or at 70%, whichever is
    # higher up the screen.
    plate = battle_panels(battle, width, height)["player"]
    box_y = min(int(height * 0.70), plate.top - 44)
    box = pygame.Rect(width // 2 - bar_w // 2, box_y, bar_w, 16)
    panel(game.screen, box.inflate(14, 14), alpha=205,
          edge=(250, 190, 90) if command["kind"] == "attack"
          else (150, 210, 255))
    # Where perfect is, as a share of the whole window.
    best = (command["best"] - command["opens"]) / span
    pygame.draw.rect(game.screen, (58, 54, 70), box, border_radius=4)
    good = pygame.Rect(0, 0, max(8, int(bar_w * 0.22)), box.h)
    good.center = (box.x + int(bar_w * best), box.centery)
    pygame.draw.rect(game.screen, (92, 132, 96), good, border_radius=3)
    marker = pygame.Rect(box.x + int(bar_w * at) - 2, box.y - 3, 5, box.h + 6)
    pygame.draw.rect(game.screen, (252, 232, 150), marker, border_radius=2)

    # **Clickable, because rule 8b is a rule** (2026-09-06). §67 shipped
    # this window as a keypress and wrote the gap down; that is the third
    # time this project has known about a mouse hole in advance and the
    # first time it was closed in the next pass instead of the next
    # playtest. The whole bar is the target - a timing test that also
    # demanded precision with the pointer would be a different game.
    if not command["taken"]:
        game.ui.hot.add(box.inflate(30, 30), "command", 0)

    if command["taken"]:
        label = "Good!" if command["quality"] > 0.75 else (
            "Late" if command["quality"] > 0 else "Missed")
    else:
        label = ("Strike!" if command["kind"] == "attack" else "Block!")
    draw_text(game.screen, game.assets, label,
              (box.centerx, box.y - 22), "small",
              (252, 226, 104) if not command["taken"] else DIM, center=True)


def draw_battle(game):
    surface = game.screen
    assets = game.assets
    battle = game.battle
    # The camera follows the body this machine is looking through; the menus
    # belong to whoever is acting. Solo they are the same person.
    player = battle.you
    enemy = battle.enemy
    width, height = surface.get_size()

    top, bottom = arena_colors(battle)
    # **Four layers and some drifting air** (item 0ar), instead of a
    # gradient and one flat rectangle. Cached per size and colour, so the
    # cost is one blit however complicated the place is.
    kind = arena_scene(battle)
    draw_backdrop(surface, assets, top, bottom, kind,
                  arena_seed_of(battle), battle.elapsed)
    draw_motes(surface, kind, battle.elapsed)

    shake_x = shake_y = 0
    if game.settings.screen_shake:
        shake_x, shake_y = game.camera.shake_x, game.camera.shake_y
    game.camera.follow(player.x, player.y, 1 / 60)

    # -- enemies -----------------------------------------------------------
    from .assets import flash_surface, tint_surface
    places = enemy_positions(battle, width, height)
    many = len(battle.enemies) > 1
    for i, monster in enumerate(battle.enemies):
        # A dead monster is still drawn while it is on its way down. Cutting
        # it on the frame its health hit zero was the old behaviour and it
        # read as the sprite being deleted rather than the thing dying.
        if not monster.alive and not battle.falling("enemy", i):
            continue
        base_x, base_y, scale = places[i]
        pose = battle.pose("enemy", i, alive=monster.alive)
        # Idle animation: two poses where they exist, a breath where they do
        # not. Seeded per monster so a group does not move in lockstep.
        idle = int(battle.elapsed * 2.4 + i) % 2
        sprite = assets.creature(monster.sprite, scale=scale, frame=idle)
        if getattr(monster, "tint", None):
            sprite = tint_surface(sprite, monster.tint, 0.45)
        flash = battle.enemy_flashes[i] if i < len(battle.enemy_flashes) else 0
        if flash > 0:
            sprite = flash_surface(sprite, (255, 240, 240),
                                   clamp(flash / 0.22, 0, 1) * 0.9)
        # Measure the foot line *before* squashing. A flattened body has a
        # smaller half-height, so sizing the shadow off the shaped sprite
        # walks it up the screen as something dies - the one place it must
        # not move at all.
        stood = sprite.get_size()
        sprite = shaped(sprite, pose)

        # `dx` is measured toward the other side and `dy` is up, so the
        # monsters flip the first and both sides flip the second.
        ex = int(base_x - pose.dx + shake_x)
        ey = int(base_y - pose.dy + pose.sink + shake_y)
        surface.blit(assets.shadow(stood[0]),
                     (ex - stood[0] // 2, int(base_y) + stood[1] // 2 + 6))
        surface.blit(sprite, (ex - sprite.get_width() // 2,
                              ey - sprite.get_height() // 2))

        spot = pygame.Rect(ex - sprite.get_width() // 2,
                           ey - sprite.get_height() // 2,
                           sprite.get_width(), sprite.get_height())
        if many and monster.alive:
            # Clicking a monster aims at it; it never attacks on its own.
            game.ui.hot.add(spot, "battle_target", i)
            if i == battle.target and battle.phase in (MENU, SUBMENU):
                draw_target_marker(surface, assets, spot, battle)

    # -- the party ---------------------------------------------------------
    # Each hero breathes between turns and plants a foot while acting.
    from .assets import flash_surface as _flash
    spots = party_positions(battle, width, height)
    crowd = len(battle.players) > 1
    for i, member in enumerate(battle.players):
        if i in battle.fled or spots[i] is None:
            continue                # no room for this body; see the strip
        base_x, base_y, scale = spots[i]
        pose = battle.pose("player", i, alive=member.alive)
        acting = (battle.turn == "player" and i == battle.acting_player)
        hero_step = 0 if (acting and battle.phase in ("menu", "submenu")) \
            else int(battle.elapsed * 7.0 + i) % 4
        hero = assets.hero("side", hero_step, scale=scale,
                           appearance=member.appearance)
        flash = battle.player_flashes[i] if i < len(battle.player_flashes) else 0
        if flash > 0:
            hero = _flash(hero, (255, 220, 220),
                          clamp(flash / 0.22, 0, 1) * 0.9)
        if not member.alive:
            hero = tint_surface(hero, (40, 40, 60), 0.65)
        stood = hero.get_size()
        hero = shaped(hero, pose)
        px = int(base_x + pose.dx + shake_x)
        py = int(base_y - pose.dy + pose.sink + shake_y)
        surface.blit(assets.shadow(stood[0]),
                     (px - stood[0] // 2, int(base_y) + stood[1] // 2 + 4))
        surface.blit(hero, (px - hero.get_width() // 2,
                            py - hero.get_height() // 2))
        if crowd and acting:
            # Only whoever is up. The strip below already names everybody,
            # and four tags over four sprites is four collisions waiting to
            # happen at the first small window.
            draw_text(surface, assets, member.name[:12],
                      (px, py - hero.get_height() // 2 - 14), "tiny",
                      ACCENT, center=True)

    game.particles.draw(surface)
    # **Under the numbers, over the bodies** (item 0al): an arrow passes
    # in front of whoever loosed it and behind the damage it causes.
    game.shots.draw(surface, assets)
    game.floats.draw(surface, assets)

    draw_enemy_plate(game, battle, width, height)
    draw_turn_bar(game, battle, width, height)
    draw_party_strip(game, battle, width, height)
    draw_player_plate(game, battle, width, height)
    draw_log(game, battle, width, height)

    if battle.phase == MENU:
        draw_menu(game, battle, width, height)
    elif battle.phase == SUBMENU:
        draw_submenu(game, battle, width, height)
    else:
        # **Only while acting** (item 0ai). The window lives in the phase
        # the menu is gone for, which is what makes room for it: during
        # `MENU` the strip it sits in is the menu.
        draw_command(game, battle, width, height)

    if battle.banner_timer > 0:
        draw_banner(game, battle, width, height)
    # **Under the enemy plate, wherever that is.** This was a hard 132,
    # which is right in the wide layout - the plate ends at 116 - and
    # wrong in the stacked one, where the plate runs down to 208 and a
    # notice landed on the enemy's name, level and HP for three seconds
    # in the middle of a fight. Found by the sprite checker (s36).
    #
    # Derived rather than given a second constant: wide still comes out
    # at exactly 132, so the layout nobody complained about cannot move.
    # Held rather than hidden - see `Game.notices_would_intrude`.
    if not game.notices_would_intrude():
        panels = battle_panels(battle, width, height)
        game.toasts.draw(surface, assets, width - 24,
                         panels["enemy"].bottom + 16)


#: One row per enemy that is not the one you are aiming at, and the
#: taller row for the one you are.
ROSTER_ROW, ROSTER_TARGET = 22, 62


def enemy_plate_height(count):
    """How tall the roster is for a pack of `count`."""
    others = max(0, int(count) - 1)
    return 12 + ROSTER_TARGET + others * ROSTER_ROW


def draw_enemy_plate(game, battle, width, height):
    """Every enemy's health at once, and the target's larger.

    **Item 0av** (2026-09-06). Savvydev:

    > "show all of the enemy healthbars at once stacked on top of each
    > other and whichever enemy you have selected has a bigger health bar"

    Before this the plate showed **one** monster - whichever was targeted
    - and everything else in the pack was a numbered chip with no health
    on it at all. In a pack of five, "which one is nearly dead" was a
    question you answered by walking the cursor across all of them and
    reading the plate five times.

    **The target keeps the plate it always had** - name, level, a full
    bar with numbers, status chips - and everybody else gets a compact
    row underneath. That way the screen a solo fight draws is the screen
    it always drew, and the information only appears when there is a pack
    to need it.
    """
    surface, assets = game.screen, game.assets
    plate = battle_panels(battle, width, height)["enemy"]
    living = [(i, foe) for i, foe in enumerate(battle.enemies) if foe.alive]
    if not living:
        living = [(battle.target, battle.enemy)]
    aim = battle.target
    boss_here = any(isinstance(foe, Boss) for _i, foe in living)
    panel(surface, plate, alpha=222,
          edge=(226, 96, 96) if boss_here else (140, 140, 170))

    # The targeted one first, then the rest in the order they stand in.
    ordered = ([one for one in living if one[0] == aim]
               + [one for one in living if one[0] != aim])
    y = plate.y + 8
    for slot, (index, foe) in enumerate(ordered):
        target = index == aim and slot == 0
        if y + (ROSTER_TARGET if target else ROSTER_ROW) > plate.bottom:
            # **What did not fit, said as a count.** A list that silently
            # stops is the bug §51 found in the guide and §43 in the
            # scene screen; a list that says "+2 more" is a list.
            left = len(ordered) - slot
            if left > 0:
                draw_text(surface, assets, "+%d more" % left,
                          (plate.right - 16, y + 6), "tiny", DIM, right=True)
            break
        ratio = foe.hp / max(1, foe.max_hp())
        if target:
            named = draw_text(surface, assets, foe.name[:26],
                              (plate.x + 16, y + 4), "body", INK)
            # On the name's centre: `right=True` anchors there (§7, §94).
            draw_text(surface, assets, "Lv %d" % foe.level,
                      (plate.right - 16, named.centery), "small", DIM,
                      right=True)
            # **The tween is the target's**, because it is the one the
            # damage numbers are landing on; the others read their health
            # straight, which is honest and costs nothing.
            bar(surface, (plate.x + 16, y + 34, plate.w - 32, 14),
                game.enemy_hp_tween.value, hp_color(ratio),
                label="%d / %d" % (foe.hp, foe.max_hp()), assets=assets)
            if foe.statuses:
                status_chips(surface, assets, foe.statuses,
                             (plate.x + 16, y + 54))
            if isinstance(foe, Boss) and foe.phase == 2:
                draw_text(surface, assets, "PHASE II",
                          (plate.right - 16, y + 56), "tiny",
                          (250, 140, 90), right=True)
            y += ROSTER_TARGET
        else:
            # A number, a short name and a thin bar. The number is the
            # one the target cursor uses, so the row and the chip under
            # the monster say the same thing.
            draw_text(surface, assets, "%d" % (index + 1),
                      (plate.x + 16, y + 8), "tiny", DIM)
            draw_text(surface, assets, foe.name[:14],
                      (plate.x + 32, y + 8), "tiny",
                      (206, 206, 216) if ratio > 0.0 else DIM)
            bar(surface, (plate.right - 132, y + 4, 116, 8),
                ratio, hp_color(ratio), assets=assets)
            y += ROSTER_ROW


def draw_turn_bar(game, battle, width, height):
    """Who acts this round, in order, and where in it you are.

    **Roadmap item 0h**, his words (2026-08-31):

    > "make it easier to know whether or not its your turn, add a turn bar
    > and gray out all of your options when its not your turn"

    He described the feature rather than the complaint, so this is the
    feature. The order comes from `Battle.turn_order()` - the renderer is
    not allowed to work it out for itself, because a bar that disagrees
    with the fight is worse than no bar at all.

    **You are marked, not just the current chip.** The question it exists
    to answer is *"am I up?"*, and a bar that only says whose turn it is
    makes you find yourself in it first.
    """
    if battle.phase == INTRO or battle.finished:
        return
    surface, assets = game.screen, game.assets
    box = battle_panels(battle, width, height)["turns"]
    order = battle.turn_order()
    if not order or box.w < 60:
        return
    now = battle.turn_now()

    panel(surface, box, alpha=196)

    # **Whose turn it is, in words, when there is room for words.** The
    # chips answer "am I up" at a glance; the name answers "who am I
    # waiting for", which is the question during somebody else's turn.
    # It gets the right-hand end of the bar and the chips get the rest.
    note = ""
    note_w = 0
    if now is not None:
        who = (battle.players[now[1]].name[:14] if now[0] == "player"
               else battle.enemies[now[1]].name[:14])
        note = "you" if now == ("player", battle.viewer) else who
        note_w = assets.font("tiny").size(note)[0] + 16
        if note_w > box.w // 2:
            note, note_w = "", 0

    # As many as fit, and never fewer than one. A round can hold nine
    # combatants and a narrow column cannot show nine, so the window
    # follows whoever is up - the same rule every list in this game uses.
    pitch = 26
    chips_w = box.w - 16 - note_w
    room = max(1, chips_w // pitch)
    start = 0
    if len(order) > room and now in order:
        start = int(clamp(order.index(now) - room // 2, 0,
                          len(order) - room))
    shown = order[start:start + room]
    if note:
        draw_text(surface, assets, note, (box.right - 8, box.centery),
                  "tiny", ACCENT if now == ("player", battle.viewer) else DIM,
                  right=True)
    x = box.x + 8 + max(0, (chips_w - len(shown) * pitch) // 2)
    for slot, (side, index) in enumerate(shown):
        chip = pygame.Rect(x + slot * pitch, box.y + 4, pitch - 4, box.h - 8)
        mine = side == "player" and index == battle.viewer
        acting = (side, index) == now
        if side == "player":
            tone = ACCENT if mine else (150, 200, 250)
            label = battle.players[index].name[:1].upper() or "?"
        else:
            # **Named, not numbered** (item 0aw, s74). The bar has always
            # included the enemies - `turn_order` returns the whole round
            # - but a chip reading "3" tells you a monster is up and not
            # *which*, which is most of why he asked for a turn order
            # that includes them.
            tone = (232, 120, 120)
            name = battle.enemies[index].name
            label = (name[:1].upper() or "?") if name else "?"
        if acting:
            glow = pulse(pygame.time.get_ticks() / 1000.0, 1.2)
            highlight(surface, chip, tone, fill_alpha=int(70 + glow * 50))
        elif mine:
            # Your own chip is outlined wherever it sits, so finding
            # yourself in the bar is not a step.
            highlight(surface, chip, tone, fill_alpha=0, border=2)
        draw_text(surface, assets, label, chip.center, "small",
                  INK if acting else (tone if mine else DIM), center=True)
    if start > 0:
        draw_text(surface, assets, "<", (box.x + 4, box.centery), "tiny",
                  FAINT, center=True)
    if start + len(shown) < len(order):
        draw_text(surface, assets, ">",
                  (box.x + 11 + chips_w, box.centery), "tiny",
                  FAINT, center=True)


def draw_party_strip(game, battle, width, height):
    """The rest of the party: name, a health bar, and whose turn it is.

    Only the viewer gets the full plate with MP and status chips - four full
    plates would be the whole screen. Everybody else gets the one number that
    decides whether you spend your turn helping them.
    """
    if len(battle.players) < 2:
        return
    surface, assets = game.screen, game.assets
    rows = [(i, m) for i, m in enumerate(battle.players) if i != battle.viewer]
    strip = party_strip_rect(battle, width, height)
    panel(surface, strip, alpha=210)
    for slot, (i, member) in enumerate(rows):
        y = strip.y + 8 + slot * 26
        acting = battle.turn == "player" and i == battle.acting_player
        name = member.name[:14]
        if i in battle.fled:
            name += " (gone)"
        elif not member.alive:
            name += " (down)"
        draw_text(surface, assets, name, (strip.x + 12, y), "small",
                  ACCENT if acting else (INK if member.alive else FAINT))
        # **Their level, and their magic** (§40, his: *"see a friend's
        # HP/mana/level under your own"*). The strip has carried a name
        # and a health bar since parties existed, which answers *"are
        # they about to fall"* and not *"can they still cast the thing I
        # am waiting for"* - and in a room the second question is why you
        # hold a turn.
        draw_text(surface, assets, "Lv %d" % member.level,
                  (strip.x + 12 + assets.font("small").size(name)[0] + 10,
                   y + 2), "tiny", DIM)
        ratio = member.hp / max(1, member.max_hp())
        bar(surface, (strip.right - 156, y + 1, 144, 11), ratio,
            hp_color(ratio),
            label="%d / %d" % (member.hp, member.max_hp()), assets=assets)
        # Thinner than the health bar on purpose: it is the second
        # question, and a row that gives both the same weight makes you
        # read it twice to find the first.
        magic = member.mp / max(1, member.max_mp())
        bar(surface, (strip.right - 156, y + 14, 144, 5), magic,
            (86, 152, 236), assets=assets)


def draw_player_plate(game, battle, width, height):
    surface, assets, player = game.screen, game.assets, battle.you
    plate = battle_panels(battle, width, height)["player"]
    panel(surface, plate, alpha=228)
    named = draw_text(surface, assets, player.name[:20],
                      (plate.x + 16, plate.y + 10), "body", INK)
    draw_text(surface, assets, "Lv %d" % player.level,
              (plate.right - 16, named.centery), "small", ACCENT, right=True)
    ratio = player.hp / max(1, player.max_hp())
    bar(surface, (plate.x + 16, plate.y + 40, plate.w - 32, 14),
        game.hp_tween.value, hp_color(ratio),
        label="%d / %d" % (player.hp, player.max_hp()), assets=assets)
    bar(surface, (plate.x + 16, plate.y + 60, plate.w - 32, 10),
        game.mp_tween.value, (86, 132, 226),
        label="MP %d / %d" % (player.mp, player.max_mp()), assets=assets)
    if player.statuses:
        status_chips(surface, assets, player.statuses, (plate.x + 16, plate.y + 78))
    if player.revive_charge:
        draw_text(surface, assets, "Phoenix ready",
                  (plate.right - 16, plate.y + 82), "tiny", (250, 180, 90),
                  right=True)


def draw_log(game, battle, width, height):
    surface, assets = game.screen, game.assets
    box = battle_panels(battle, width, height)["log"]
    panel(surface, box, alpha=200)
    draw_text(surface, assets, "Round %d" % battle.round,
              (box.x + 14, box.y + 8), "tiny", FAINT)
    lines = []
    for entry in battle.log[-6:]:
        lines.extend(wrap_text(entry, assets.font("tiny"), box.w - 28)[:2])
    # **As many as the box holds, not a fixed seven.** The box is 150 tall
    # wide and 104 stacked, and seven lines at a 17 pitch need 147 - so a
    # busy round on a phone drew the log straight out through its own
    # bottom edge and over whatever was under it. It had been doing that
    # since the stacked layout existed; it only became visible when the
    # turn bar (s51) moved in underneath. Newest lines win, because the
    # bottom of a log is the part you are reading.
    room = max(1, (box.h - 34) // 17)
    for i, line in enumerate(lines[-room:]):
        draw_text(surface, assets, line, (box.x + 14, box.y + 28 + i * 17),
                  "tiny", DIM)


def draw_menu(game, battle, width, height):
    """Your five options - and nothing lit when they are not yours.

    **Item 0h's second half**: *"gray out all of your options when its not
    your turn"*. It is drawn during somebody else's turn because the panel
    holding still is better than a panel that blinks in and out four times
    a round - but it looked exactly like your own, so people pressed it.
    Greyed now, titled with whose turn it is, and **the rows stop being
    hotspots**: a control that will be ignored is not a control (s45's
    rule, from Flee under Oathkeeper).
    """
    surface, assets = game.screen, game.assets
    box = battle_panels(battle, width, height)["menu"]
    # The same call the input gate makes, so "looks dead" and "is dead"
    # cannot drift apart.
    theirs = waiting_on_someone_else(battle)
    panel(surface, box, edge=(90, 90, 104) if theirs else ACCENT, alpha=234)
    for i, label in enumerate(menu_actions_for(battle)):
        row = pygame.Rect(box.x + 14, box.y + 14 + i * 33, box.w - 28, 30)
        enabled = not theirs
        if label == "Flee" and not battle.can_flee():
            enabled = False
        if label == "Skill" and not battle.actor.skills():
            enabled = False
        if label == "Item" and not battle.usable_items():
            enabled = False
        if not theirs:
            game.ui.hot.add(row, "battle_menu", i)
        selected = i == battle.menu_index and not theirs
        if selected:
            glow = pulse(pygame.time.get_ticks() / 1000.0, 1.2)
            highlight(surface, row, (250, int(180 + glow * 60), 96),
                      fill_alpha=54)
        elif game.hover == ("battle_menu", i) and not theirs:
            highlight(surface, row, ACCENT, fill_alpha=38, border=2)
        color = (ACCENT if selected else INK) if enabled else FAINT
        named = draw_text(surface, assets, "%d. %s" % (i + 1, label),
                          (row.x + 14, row.y + 6), "body", color)
        if label == "Item":
            count = sum(battle.actor.consumables.values())
            draw_text(surface, assets, str(count),
                      (row.right - 14, named.centery), "small",
                      DIM if enabled else FAINT, right=True)
    # **Who you are waiting on is said in the turn bar, not here.** Five
    # rows at a 33 pitch leave ten pixels inside this panel and the note
    # landed on the Flee row - and the bar is the thing that exists to
    # answer "whose turn is it" in the first place.


def draw_submenu(game, battle, width, height):
    surface, assets, player = game.screen, game.assets, battle.actor
    box = battle_panels(battle, width, height)["submenu"]
    panel(surface, box, edge=(150, 120, 230), alpha=240)
    rows = submenu_rows(game, battle)
    title = "Skills" if battle.sub_kind == "skill" else "Items"
    draw_text(surface, assets, title, (box.x + 16, box.y + 10), "body", INK)

    # As many as the box holds. Six was right for a 306-pixel panel and put
    # the detail line on top of the last row once the panel got shorter.
    visible = max(1, (box.h - 108) // 32)
    start = max(0, min(battle.sub_index - visible + 1,
                       max(0, len(rows) - visible)))
    for offset, (label, detail, cost, usable, color) in enumerate(
            rows[start:start + visible]):
        i = start + offset
        row = pygame.Rect(box.x + 12, box.y + 40 + offset * 32, box.w - 24, 29)
        game.ui.hot.add(row, "battle_sub", i)
        if i == battle.sub_index:
            highlight(surface, row, color, fill_alpha=54)
        elif game.hover == ("battle_sub", i):
            highlight(surface, row, color, fill_alpha=38, border=2)
        named = draw_text(surface, assets, label[:26], (row.x + 12, row.y + 6),
                          "small", color if usable else FAINT)
        # The cost on the name's line. At `row.y + 6` it rode half a line
        # high and the top row's poked out of its highlight (§7, §94).
        draw_text(surface, assets, cost, (row.right - 12, named.centery),
                  "small", DIM if usable else BAD, right=True)

    if rows:
        detail = rows[min(battle.sub_index, len(rows) - 1)][1]
        for i, line in enumerate(wrap_text(detail, assets.font("tiny"),
                                           box.w - 32)[:2]):
            draw_text(surface, assets, line,
                      (box.x + 16, box.bottom - 62 + i * 16), "tiny", DIM)
    key_hint(surface, assets, [("Enter", "Use"), ("Esc", "Back")],
             (box.x + 16, box.bottom - 26), game=game)


def confirm_top(game, battle):
    """The top-level menu: three of these act, two open a list.

    Opening a list is pure local cursor work and happens on a mirror exactly
    as it does anywhere else. The three that act go through `commit`.
    """
    rows = menu_actions_for(battle)
    action = rows[min(battle.menu_index, len(rows) - 1)]
    if action in ("Attack", "Defend", "Flee"):
        if action == "Flee" and not battle.can_flee():
            battle.say("There is no running from this.")
            game.sfx("error")
            return
        commit(game, battle, action.lower())
    else:
        battle.confirm_menu()


def submenu_rows(game, battle):
    """(label, detail, cost text, usable, colour) for the skill or item list."""
    player = battle.actor
    if battle.sub_kind == "skill":
        rows = []
        for skill in player.skills():
            color = ELEMENT_COLOR.get(skill.element, INK)
            rows.append((skill.name, skill.description,
                         battle.skill_cost_text(skill),
                         battle.can_pay(skill), color))
        return rows
    rows = []
    for key in battle.usable_items():
        item = CONSUMABLES[key]
        usable = not (item.effect == "escape" and not battle.can_flee())
        rows.append((item.name, item.description,
                     "x%d" % player.consumable_count(key), usable, item.color))
    return rows


def draw_banner(game, battle, width, height):
    surface, assets = game.screen, game.assets
    progress = ease_out_cubic(clamp(1.0 - battle.banner_timer / 1.2, 0, 1))
    alpha = int(255 * clamp(battle.banner_timer / 0.6, 0, 1))
    band = pygame.Surface((width, 92), pygame.SRCALPHA)
    band.fill((10, 8, 16, int(alpha * 0.82)))
    pygame.draw.line(band, (250, 200, 96, alpha), (0, 0), (width, 0), 2)
    pygame.draw.line(band, (250, 200, 96, alpha), (0, 91), (width, 91), 2)
    surface.blit(band, (0, height // 2 - 46))
    img = assets.text(battle.banner, "huge", (250, 240, 250))
    img = img.copy()
    img.set_alpha(alpha)
    rect = img.get_rect(center=(int(width * (0.5 + (1 - progress) * 0.08)),
                                height // 2))
    surface.blit(img, rect)


# ------------------------------------------------------------------ input ---

def commit(game, battle, kind, key=""):
    """Act, or ask the host to.

    On a mirror this machine owns its cursor and nothing else. The move goes
    up as an intent and comes back as a frame; acting locally would be a
    client deciding its own damage, which is the one thing `net.py` rule 2
    exists to prevent. The phase is pushed to ACTING straight away so that
    holding the key down does not put four attacks on the wire before the
    first answer arrives - the host's next frame overwrites it either way.
    """
    if battle.mirror:
        if game.coop is not None:
            game.coop.send_act(kind, key, battle.target)
        battle.phase = ACTING
        return
    if kind == "attack":
        battle.player_attack()
    elif kind == "defend":
        battle.player_defend()
    elif kind == "flee":
        battle.player_flee()
    elif kind == "skill":
        for skill in battle.actor.skills():
            if skill.key == key:
                battle.use_skill(skill)
                return
    elif kind == "item":
        battle.use_item(key)


def waiting_on_someone_else(battle):
    """Whether the local player is a spectator right now.

    **Asks whose turn it is, not whether this is a mirror.** It used to
    read `battle.mirror and not battle.is_your_turn`, and `mirror` is
    only true on a *client* - so the **host took input on every player's
    turn, including its guest's.**

    One line, three symptoms in the school playtest (§40): the host
    played its guest's turn *for* them, the guest's prompt vanished
    because their turn had already been spent, and the menu showed the
    wrong person's skills - because the menu draws from `battle.actor`
    (§21a) and on the host `actor` was whoever was up.

    Deliberately keyed on `turn == "player"` rather than on
    `is_your_turn`, which also requires a MENU or SUBMENU phase. Outside
    a player's turn - monsters acting, animations running - input stays
    as open as it has always been, so solo behaviour does not move.
    """
    return battle.turn == "player" and battle.acting_player != battle.viewer


def handle_battle_event(game, event):
    battle = game.battle
    if battle is None:
        return

    # **An action command is answered before anything else** (item 0ai).
    # It is live during `ACTING`, which is a phase that otherwise ignores
    # input entirely, and it is the one moment where a keypress is worth
    # something without a menu open. `battle.py` decides what the press
    # was worth; this only says that it happened.
    if battle.command and not battle.command["taken"]:
        if event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE):
            battle.time_command()
            return

    # A mirror only takes input on its own turn. Everything else on screen
    # is somebody else's decision being played back.
    if waiting_on_someone_else(battle) and not battle.finished:
        return

    if battle.phase == MENU:
        if event.key in (pygame.K_w, pygame.K_UP):
            battle.choose_menu(battle.menu_index - 1)
            game.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            battle.choose_menu(battle.menu_index + 1)
            game.sfx("move")
        elif event.key in (pygame.K_a, pygame.K_LEFT):
            if battle.cycle_target(-1):
                game.sfx("move")
        elif event.key in (pygame.K_d, pygame.K_RIGHT):
            if battle.cycle_target(1):
                game.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE):
            confirm_top(game, battle)
        elif pygame.K_1 <= event.key <= pygame.K_5:
            battle.choose_menu(event.key - pygame.K_1)
            confirm_top(game, battle)
        elif event.key == pygame.K_ESCAPE:
            game.toast("No pausing mid-fight.", DIM, 1.4)
        return

    if battle.phase == SUBMENU:
        rows = submenu_rows(game, battle)
        if event.key in (pygame.K_w, pygame.K_UP):
            battle.sub_index = (battle.sub_index - 1) % max(1, len(rows))
            game.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            battle.sub_index = (battle.sub_index + 1) % max(1, len(rows))
            game.sfx("move")
        elif event.key == pygame.K_ESCAPE:
            battle.cancel_submenu()
            game.sfx("back")
        elif event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE):
            if not rows:
                battle.cancel_submenu()
                return
            index = min(battle.sub_index, len(rows) - 1)
            if battle.sub_kind == "skill":
                commit(game, battle, "skill",
                       battle.actor.skills()[index].key)
            else:
                commit(game, battle, "item", battle.usable_items()[index])
        return

    # During an animation the only accepted key is a nudge to hurry it along.
    if battle.finished and event.key in (pygame.K_RETURN, pygame.K_e,
                                         pygame.K_SPACE, pygame.K_ESCAPE):
        battle.banner_timer = 0.0
