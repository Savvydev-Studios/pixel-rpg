"""Drawing a free-roam fight, and collecting the keys for one.

The split `battle.py` / `battlescreen.py` has, for the same reason:
`freeroam.py` is positions and cooldowns and imports no pygame, and this
is every pixel of it (item 0ak, §76).

**Held keys, not pressed ones.** A turn-based fight reads `KEYDOWN` and
acts once; this reads the keyboard's *current* state every frame, because
walking is a thing you do continuously and a fight you have to tap your
way across is not free-roam. That is the one structural difference
between this screen and every other one in the game.
"""
from __future__ import annotations

import math

import pygame

from . import freeroam as fr
from .assets import flash_surface
from .assets import vertical_gradient
from .battlescreen import _shade, draw_motes
from .ui import (ACCENT, BAD, DIM, GOOD, INK, bar, draw_text, ellipsize,
                 hp_color, key_hint, panel)
from .util import clamp, pulse, stable_seed

#: The arena is a fixed size in world units and the window is not, so it
#: is drawn scaled to fit with a margin. One transform, worked out once a
#: frame, and every body goes through it - there is no second place that
#: converts a coordinate.
MARGIN = 24


def view(surface):
    """(scale, offset_x, offset_y) mapping arena units onto the window."""
    width, height = surface.get_size()
    room_w = max(120, width - MARGIN * 2)
    room_h = max(120, height - MARGIN * 2 - 96)
    scale = min(room_w / fr.ARENA_W, room_h / fr.ARENA_H)
    ox = (width - fr.ARENA_W * scale) / 2
    oy = MARGIN + 64
    return scale, ox, oy


def to_screen(surface, x, y):
    scale, ox, oy = view(surface)
    return int(ox + x * scale), int(oy + y * scale)


def draw_free_roam(game):
    """One frame of a free-roam fight."""
    fight = game.roam
    surface, assets = game.screen, game.assets
    if fight is None:
        return
    width, height = surface.get_size()
    scale, ox, oy = view(surface)
    floor = pygame.Rect(int(ox), int(oy), int(fr.ARENA_W * scale),
                        int(fr.ARENA_H * scale))
    draw_arena_floor(game, surface, floor, width, height)
    # **Under everything that moves** (item 0bv, §86): the camp is part
    # of the ground and a coin is something lying on it, so both go down
    # before a single body does. Drawn after the floor and before the
    # cast, which is the only order that reads right.
    if fight.hoard:
        draw_camp(game, surface, fight, scale)
        draw_loot(game, surface, fight, scale)

    for body in fight.foes:
        if body.alive:
            draw_body(game, body, "enemy")
    for body in fight.heroes:
        if body.alive:
            draw_body(game, body, "player")
        draw_swing(surface, game, body)

    draw_roam_hud(game, fight, width, height)


def arena_of(game):
    """Which of the eighteen places this fight is happening in.

    **Item 0bl.** He asked whether free-roam had any arenas and the
    answer was no: §§72-74 built eighteen with four skylines each, and
    every one of them is drawn by `battlescreen.py`, which this screen
    does not use. It painted a grey grid.

    A Long Night uses the same arena a Last Stand would at that wave, so
    the two modes walk the same country - which is what he meant by
    *"the long night is also meant to use those arenas we created"*.
    An island fight in real time (item 0bi) has no wave to ask about, so
    it takes the first one and lets its own tint come from the fight.
    """
    from . import gamemodes
    run = (getattr(game, "zombies", None) or getattr(game, "waves", None)
           or getattr(game, "hoard", None))
    wave = int(getattr(run, "wave", 1) or 1)
    # **Through `Game.arena_now`, which is the one that knows the rules**
    # - the run's random roll, and what *changes every N waves* means in
    # a mode with no waves. This function used to work them out again
    # from `settings`, which is a second copy of a decision and drifted
    # the moment a third mode arrived (§87).
    chooser = getattr(game, "arena_now", None)
    if callable(chooser):
        return chooser(wave)
    settings = getattr(game, "settings", None)
    pick = int(getattr(settings, "arena_pick", 0) or 0)
    if getattr(settings, "arena_mode", "every") == "random":
        pick = int(getattr(game, "wave_arena_roll", 0) or 0)
    return gamemodes.arena_for(wave,
                               getattr(settings, "arena_mode", "every"),
                               getattr(settings, "arena_every", None), pick)


def draw_arena_floor(game, surface, floor, width, height):
    """The ground of a free-roam arena, and the sky over it.

    **Looked at from above, so it is not `battlescreen`'s backdrop** -
    that one is a horizon with sky above it and bodies standing on a
    line. What carries across is the *place*: its colour, its skyline
    behind the arena, and its motes.
    """
    arena = arena_of(game)
    wash = tuple(arena.tint or (200, 200, 210))
    # The world outside the arena: the sky of that place, darkened, so
    # the plate reads as a lit floor in it rather than as a window.
    surface.blit(game.assets.gradient((width, height),
                                      _shade(wash, 0.30),
                                      _shade(wash, 0.08)), (0, 0))
    # The floor itself, lit, with the same receding bands the battle
    # backdrop uses so the two places look like one game.
    pygame.draw.rect(surface, _shade(wash, 0.46), floor)
    for n in range(1, 4):
        band = pygame.Rect(floor.x, floor.y + floor.h * n // 4,
                           floor.w, floor.h // 4 + 1)
        pygame.draw.rect(surface, _shade(wash, 0.46 - 0.05 * n), band)
    step = max(24, int(60 * (floor.w / max(1.0, fr.ARENA_W))))
    grid = _shade(wash, 0.56)
    for gx in range(floor.x, floor.right, step):
        pygame.draw.line(surface, grid, (gx, floor.y), (gx, floor.bottom))
    for gy in range(floor.y, floor.bottom, step):
        pygame.draw.line(surface, grid, (floor.x, gy), (floor.right, gy))
    pygame.draw.rect(surface, _shade(wash, 1.05), floor, 2)
    draw_motes(surface, getattr(arena, "scene", "hills"),
               getattr(getattr(game, "roam", None), "elapsed", 0.0),
               alpha=48)
    draw_cover(game, surface, floor, wash)
    return arena


def draw_cover(game, surface, floor, wash):
    """What stands in the arena (item 0bp).

    **Blocks, though the simulation uses circles.** A circle drawn on a
    grid of squares reads as a bubble; a block reads as a thing. The
    simulation only ever asks how far a centre is, so the two can
    disagree about the corners and nobody notices - and the block is
    drawn *inside* the radius so what you can walk through is never
    larger than what you can see.
    """
    fight = getattr(game, "roam", None)
    blockers = list(getattr(fight, "blockers", ()) or ())
    if not blockers:
        return
    scale = floor.w / max(1.0, fr.ARENA_W)
    top = _shade(wash, 0.72)
    side = _shade(wash, 0.40)
    edge = _shade(wash, 1.10)
    for bx, by, radius in blockers:
        wide = max(6, int(radius * 1.42 * scale))
        x = int(floor.x + bx * scale) - wide // 2
        y = int(floor.y + by * scale) - wide // 2
        # A face and a lip, so it reads as standing up off the floor
        # rather than as a patch painted on it.
        pygame.draw.rect(surface, side, (x, y + wide // 5, wide, wide))
        pygame.draw.rect(surface, top, (x, y, wide, wide - wide // 5))
        pygame.draw.rect(surface, edge, (x, y, wide, wide - wide // 5), 2)


def arena_thumbnail(assets, size, arena):
    """A small picture of one arena (item 0br).

    **The thing being chosen is a place**, and every version of this
    setting so far offered it as a *name in a list*. "The Dry Cistern"
    tells you nothing about whether you want to fight there.

    Drawn from the same three things the real arena is - its colour, its
    skyline and its floor plan - rather than from a saved image, so a
    new arena gets a picture for free and a changed one cannot end up
    with a stale portrait. **Cached**, because a grid of eighteen of
    these is drawn every frame of the picker.
    """
    from . import freeroam as fr
    from .battlescreen import backdrop_layers
    key = ("arenathumb", size, arena.key)
    if key in assets._misc:
        return assets._misc[key]
    width, height = size
    wash = tuple(arena.tint or (200, 200, 210))
    shot = pygame.Surface(size)
    shot.blit(vertical_gradient(size, _shade(wash, 0.34),
                                _shade(wash, 0.10)), (0, 0))
    # The skyline, borrowed from the battle backdrop so the two agree
    # about what this place looks like.
    for layer in backdrop_layers(assets, size, _shade(wash, 0.42),
                                 arena.scene, stable_seed(arena.scene)):
        shot.blit(layer, (0, 0))
    floor = pygame.Rect(int(width * 0.06), int(height * 0.42),
                        int(width * 0.88), int(height * 0.5))
    pygame.draw.rect(shot, _shade(wash, 0.46), floor)
    for n in range(1, 3):
        band = pygame.Rect(floor.x, floor.y + floor.h * n // 3,
                           floor.w, floor.h // 3 + 1)
        pygame.draw.rect(shot, _shade(wash, 0.46 - 0.05 * n), band)
    pygame.draw.rect(shot, _shade(wash, 1.0), floor, 1)
    # And what is standing in it, at the same relative places.
    scale = floor.w / max(1.0, fr.ARENA_W)
    top, side = _shade(wash, 0.74), _shade(wash, 0.42)
    for bx, by, radius in fr.plan_blockers(
            getattr(arena, "plan", "open"), stable_seed(arena.key)):
        wide = max(2, int(radius * 1.42 * scale))
        x = int(floor.x + bx * scale) - wide // 2
        y = int(floor.y + by * (floor.h / max(1.0, fr.ARENA_H))) - wide // 2
        pygame.draw.rect(shot, side, (x, y + max(1, wide // 5), wide, wide))
        pygame.draw.rect(shot, top, (x, y, wide, wide))
    assets._misc[key] = shot
    return shot


def draw_body(game, body, side):
    """One body, with its shadow, its facing and its health."""
    surface, assets = game.screen, game.assets
    x, y = to_screen(surface, body.x, body.y)
    scale, _ox, _oy = view(surface)
    if side == "player":
        sprite = assets.hero(facing_name(body.facing), 0,
                             appearance=getattr(body.who, "appearance", None))
    else:
        sprite = assets.creature(getattr(body.who, "sprite", "slime"),
                                 scale=max(2, int(3 * scale)))
    if body.hurt > fr.HURT_GRACE - 0.16:
        sprite = flash_surface(sprite, (255, 235, 235), 0.75)
    if body.invulnerable:
        sprite = sprite.copy()
        sprite.set_alpha(140)
    surface.blit(assets.shadow(sprite.get_width()),
                 (x - sprite.get_width() // 2, y + sprite.get_height() // 3))
    surface.blit(sprite, (x - sprite.get_width() // 2,
                          y - sprite.get_height() // 2))

    # **A bar over every body, always.** §74's roster answered "which one
    # is nearly dead" for a turn-based fight by listing them; here they
    # are all on screen already, so the answer belongs on the body.
    ceiling = getattr(body.who, "max_hp", 0)
    ceiling = ceiling() if callable(ceiling) else ceiling
    ratio = clamp(body.who.hp / max(1, int(ceiling or 1)), 0.0, 1.0)
    wide = 34 if side == "enemy" else 46
    bar(surface, (x - wide // 2, y - sprite.get_height() // 2 - 9, wide, 5),
        ratio, hp_color(ratio), assets=assets)


def facing_name(angle):
    """Which of the four hero sprites points this way."""
    turn = (math.degrees(angle) + 360.0) % 360.0
    if 45 <= turn < 135:
        return "front"
    if 135 <= turn < 225:
        return "left"
    if 225 <= turn < 315:
        return "back"
    return "right"


def draw_swing(surface, game, body):
    """The arc a swing covers, for as long as it is live.

    **It is the aiming feedback**, not decoration: what you hit is what
    was inside this, so it has to be drawn at the size the simulation
    actually used (`fr.reach_of`), never at a size that looks right.
    """
    if body.swing <= 0.0:
        return
    scale, _ox, _oy = view(surface)
    x, y = to_screen(surface, body.x, body.y)
    # **The reach the simulation used**, kept on the body when the swing
    # started - a spell reaches further than steel, and an arc drawn at
    # the wrong size is aiming feedback that lies.
    reach = int(getattr(game, "roam_reach", fr.SWING_REACH) * scale)
    # **And the width it used** (item 0by). Skills have their own arc
    # now - a bolt is a line and a cleave is a wedge - and a telegraph
    # drawn at the default width would be aiming feedback that lies,
    # which is the one thing this docstring says it must not be.
    arc = float(getattr(game, "roam_arc", 0.0)) or fr.SWING_ARC
    fade = clamp(body.swing / fr.SWING_TIME, 0.0, 1.0)
    layer = pygame.Surface((reach * 2 + 8, reach * 2 + 8), pygame.SRCALPHA)
    centre = (reach + 4, reach + 4)
    points = [centre]
    steps = 10
    for n in range(steps + 1):
        angle = body.facing - arc / 2 + arc * n / steps
        points.append((centre[0] + math.cos(angle) * reach,
                       centre[1] + math.sin(angle) * reach))
    pygame.draw.polygon(layer, (250, 230, 180, int(90 * fade)), points)
    surface.blit(layer, (x - centre[0], y - centre[1]))


def draw_camp(game, surface, fight, scale):
    """Where you carry it back to.

    **A ring rather than a box**, because the thing it is saying is
    *anywhere in here counts* - and a hard edge on a circle you run into
    at speed reads as a wall, which it is not.
    """
    from . import freeroam as fr
    surface_ = surface
    cx, cy = to_screen(surface_, fight.bank[0], fight.bank[1])
    radius = max(10, int(fr.BANK_REACH * scale))
    beat = pulse(fight.elapsed, 0.7)
    for step in range(3):
        tone = (int(120 + 70 * beat), int(96 + 60 * beat), int(52 + 30 * beat))
        pygame.draw.circle(surface_, tone, (cx, cy),
                           max(3, radius - step * 3), 1)
    held = fight.carried_total()
    if held:
        # Only says so when somebody is holding something, because the
        # rest of the time it is a place and not an instruction.
        # **Inside the ring.** Above it lands on whoever is standing at
        # the edge and below it lands off the floor; in the middle it
        # reads as something written on the ground, which is what it is.
        draw_text(surface_, game.assets, "bring it here", (cx, cy),
                  "tiny", DIM, center=True)


def draw_loot(game, surface, fight, scale):
    """The coins. Small, bright, and never a sprite.

    Hand-typed 16x16 grids are for things with a face (hard rule 3's
    spirit); a coin is a circle with a highlight, and drawing forty of
    those a frame is cheaper than blitting forty surfaces.
    """
    for coin in fight.loot:
        x, y = to_screen(surface, coin.x, coin.y)
        lift = int(2 * pulse(fight.elapsed + coin.seed * 0.37,
                             0.9))
        size = max(2, int(4 * scale))
        pygame.draw.circle(surface, (58, 44, 20), (x, y + 2), size)
        pygame.draw.circle(surface, (238, 196, 84), (x, y - lift), size)
        pygame.draw.circle(surface, (255, 238, 176),
                           (x - max(1, size // 3), y - lift - size // 3),
                           max(1, size // 3))


def draw_hoard_hud(game, fight, width, height, right):
    """The clock, what you are holding, and what is safe.

    **Three numbers and they are not the same kind of thing.** What is
    banked is yours; what you are carrying is not yet; the clock is what
    turns one into the other. So the carried figure is the loud one -
    it is the number the decision is about.
    """
    run = game.hoard
    surface, assets = game.screen, game.assets
    hero = fight.hero(fight.viewer)
    held = hero.carrying if hero is not None else 0
    safe = int(fight.banked.get(fight.viewer, 0))
    panel(surface, right, alpha=210)
    left = max(0.0, float(getattr(run, "left", 0.0)))
    urgent = left <= 20.0
    clock = draw_text(surface, assets,
                      "%d:%02d" % (int(left) // 60, int(left) % 60),
                      (right.x + 12, right.y + 8), "small",
                      BAD if urgent else ACCENT)
    # Each right-hand figure on its left-hand partner's centre (§7, §94).
    draw_text(surface, assets, "%d safe" % safe,
              (right.right - 12, clock.centery), "tiny", DIM, right=True)
    # **The rule has to be on the screen or nobody finds it** (§87). A
    # trip of ten or more pays double, and that is the decision this mode
    # is made of - so the right of the row counts down to it and goes
    # gold the moment it is reached.
    #
    # **The speed penalty is not written anywhere**, deliberately: it is
    # felt every step, the plate has room for one number, and the one
    # worth reading is the one you are deciding about.
    from . import gamemodes
    big = int(gamemodes.HOARD_BIG_TRIP)
    doubled = held >= big
    carried = draw_text(surface, assets,
                        ellipsize(assets.font("tiny"),
                                  "carrying %d" % held if held
                                  else "arms empty",
                                  right.w - 84),
                        (right.x + 12, right.y + 30), "tiny",
                        GOOD if held else DIM)
    if held:
        draw_text(surface, assets,
                  "DOUBLE" if doubled else "%d to double" % (big - held),
                  (right.right - 12, carried.centery), "tiny",
                  ACCENT if doubled else DIM, right=True)


def draw_roam_hud(game, fight, width, height):
    """Health, magic, the wave, and what is queued."""
    surface, assets = game.screen, game.assets
    # **The hero this machine is playing, not index 0** (item 0bg). In a
    # room the host is 0 and the guest is 1, so a HUD keyed on zero shows
    # a guest the *host's* health and the host's cooldowns - which a
    # screenshot of the guest's own screen made obvious and nothing else
    # would have.
    hero = fight.hero(fight.viewer)
    if hero is None:
        return
    who = hero.who
    # **The two plates share the width.** At 390 they were 342 and 210
    # starting at 156, so they sat on top of each other and the health
    # numbers were buried under the night counter - `layout.py --phone`
    # caught it the frame this screen was added to the list.
    night_w = min(180, max(96, (width - MARGIN * 3) // 3))
    box = pygame.Rect(MARGIN, MARGIN,
                      max(120, width - MARGIN * 3 - night_w), 54)
    panel(surface, box, alpha=210)
    draw_text(surface, assets, who.name[:16], (box.x + 12, box.y + 8),
              "small", INK)
    bar(surface, (box.x + 12, box.y + 26, box.w - 24, 9),
        clamp(who.hp / max(1, who.max_hp()), 0, 1),
        hp_color(who.hp / max(1, who.max_hp())),
        label="%d / %d" % (who.hp, who.max_hp()), assets=assets)
    bar(surface, (box.x + 12, box.y + 38, box.w - 24, 6),
        clamp(who.mp / max(1, who.max_mp()), 0, 1), (86, 152, 236),
        assets=assets)

    if fight.hoard and getattr(game, "hoard", None) is not None:
        # A hoard's right-hand plate says something else entirely, and
        # it is wider because a clock and two counts need the room.
        # Wider than a night's plate: a clock, a haul and a countdown
        # to the double is three things, and the last one is a sentence.
        wide = int(clamp(width - MARGIN * 3 - 200, 150, 196))
        draw_hoard_hud(game, fight, width, height,
                       pygame.Rect(width - MARGIN - wide, MARGIN, wide, 54))
        return
    run = getattr(game, "zombies", None)
    if run is not None:
        right = pygame.Rect(width - MARGIN - night_w, MARGIN,
                            night_w, 54)
        panel(surface, right, alpha=210)
        draw_text(surface, assets, "Night %d" % run.wave,
                  (right.x + 12, right.y + 8), "small", ACCENT)
        # **One line or two, depending on the room.** Side by side they
        # collide at 390 once the numbers get to two digits - and both
        # numbers grow all night, so the wide case is the temporary one.
        if right.w >= 150:
            felled = draw_text(surface, assets, "%d felled" % run.kills,
                               (right.x + 12, right.y + 30), "tiny", DIM)
            draw_text(surface, assets, "%d left" % len(fight.living_foes),
                      (right.right - 12, felled.centery), "tiny",
                      BAD if fight.living_foes else GOOD, right=True)
        else:
            draw_text(surface, assets,
                      "%d left" % len(fight.living_foes),
                      (right.x + 12, right.y + 30), "tiny",
                      BAD if fight.living_foes else GOOD)

    # **The loadout, and which one is held.** Four skills, four number
    # keys - the same four `Player.skills()` gives every other screen, so
    # a loadout chosen in the hero sheet is the loadout you fight with.
    skills = who.skills()
    y = height - MARGIN - 30
    x = MARGIN
    held = fight.charged.get(fight.viewer)
    # **As wide as there is room for, never wider.** Four fixed 158s is
    # 632 pixels and a phone in portrait has 390 - two chips ran off the
    # window. §29's rule: shorten to a *width*, never to a count.
    room = max(60, (width - MARGIN * 2 - 8 * max(1, len(skills) - 1))
               // max(1, len(skills)))
    chip_w = min(150, room)
    for n, skill in enumerate(skills):
        cooling = fight.cooldowns.get((fight.viewer, skill.key), 0.0)
        chip = pygame.Rect(x, y, chip_w, 26)
        tone = ACCENT if held is skill else (
            DIM if cooling > 0 or who.mp < skill.mp else INK)
        panel(surface, chip, alpha=170)
        # The number always fits; the name is what gives way - measured,
        # not counted at seven pixels a letter (§94). And centred: given
        # `chip.centery` as a top, the label hung on the chip's floor.
        tiny = assets.font("tiny")
        number = "%d " % (n + 1)
        draw_text(surface, assets,
                  number + ellipsize(tiny, skill.name,
                                     chip_w - 16 - tiny.size(number)[0]),
                  (chip.x + 8, chip.centery - tiny.get_height() // 2),
                  "tiny", tone)
        if cooling > 0:
            wide = int(chip.w * clamp(
                cooling / max(0.1, fr.FreeRoam.skill_cooldown(skill)), 0, 1))
            pygame.draw.rect(surface, (70, 66, 96),
                             (chip.x, chip.bottom - 3, wide, 3))
        game.ui.hot.add(chip, "roamskill", n)
        x += chip_w + 8

    key_hint(surface, assets,
             [("WASD", "Move"), ("Space", "Swing"), ("Shift", "Dodge"),
              ("1-4", "Hold a skill"), ("Esc", "Give up")],
             (MARGIN, height - MARGIN + 2), game=game)
