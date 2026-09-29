"""Reusable interface widgets: panels, bars, toasts, particles, item cards."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pygame

from .items import RARITY_ORDER, SHORT, STAT_KEYS
from .util import (clamp, ease_out_back, ease_out_cubic, fx_rng, lerp,
                   lerp_color, pulse, rng, shade, wrap_text)

INK = (238, 238, 246)
DIM = (168, 168, 186)
FAINT = (118, 118, 138)
PANEL_BG = (22, 20, 30)
PANEL_EDGE = (92, 86, 122)
ACCENT = (250, 200, 96)
GOOD = (118, 220, 138)
BAD = (236, 92, 96)


def draw_text(surface, assets, text, pos, font="body", color=INK,
              center=False, shadow=True, right=False):
    """Blit text with a one-pixel drop shadow so it stays legible on any tile."""
    img = assets.text(text, font, color)
    rect = img.get_rect()
    if center:
        rect.center = pos
    elif right:
        rect.midright = pos
    else:
        rect.topleft = pos
    if shadow:
        surface.blit(assets.text(text, font, (8, 6, 12)),
                     (rect.x + 2, rect.y + 2))
    surface.blit(img, rect)
    return rect


def legible(colour, floor=None):
    """`colour`, lifted toward white just far enough to read on a panel.

    **For text drawn in a colour that means something** (§95) - a hair or
    an outfit in the character creator is written in its own swatch, and
    *Black*, *Deep*, *Umber* and *Slate* were dark text on a dark panel.
    The floor is `FAINT`'s brightness, the dimmest text the game already
    uses, so a colour bright enough is returned untouched and a dark one
    keeps its hue and only gains light.
    """
    floor = _luma(FAINT) if floor is None else floor
    r, g, b = colour[:3]
    luma = _luma((r, g, b))
    if luma >= floor:
        return (r, g, b)
    t = (floor - luma) / max(1.0, 255.0 - luma)
    return (int(round(r + (255 - r) * t)), int(round(g + (255 - g) * t)),
            int(round(b + (255 - b) * t)))


def _luma(colour):
    r, g, b = colour[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


_DIM_SHEETS = {}


def tint_screen(surface, colour, alpha):
    """Lay one flat colour over the whole of `surface` at `alpha`.

    **A cached sheet with one alpha, not a fresh layer every frame**
    (§95). Every menu's backdrop, the chapter card and the night all did
    `pygame.Surface(size, SRCALPHA)`, filled it and blended it back, every
    frame - eight megabytes allocated and five milliseconds spent at
    1920x1080 to say "a bit darker". A surface-wide alpha blends the same
    colour by the same amount; the arithmetic rounds differently by at
    most one step in a channel, which nobody can see.
    """
    alpha = int(alpha)
    if alpha <= 0:
        return
    size = surface.get_size()
    key = (size, tuple(colour[:3]))
    sheet = _DIM_SHEETS.get(key)
    if sheet is None:
        if len(_DIM_SHEETS) > 12:
            _DIM_SHEETS.clear()           # a window dragged through sizes
        sheet = pygame.Surface(size)
        sheet.fill(colour[:3])
        _DIM_SHEETS[key] = sheet
    sheet.set_alpha(min(255, alpha))
    surface.blit(sheet, (0, 0))


def panel(surface, rect, fill=PANEL_BG, edge=PANEL_EDGE, alpha=236, radius=6):
    """Rounded translucent panel with a bright inner rule.

    A rect with no area draws nothing. That sounds obvious and it was not
    the behaviour: `pygame.Surface` refuses a non-positive size and raises,
    so a layout that worked itself out to a negative width took the whole
    game down rather than looking wrong. On a desktop that was unreachable -
    the window clamps to 800x600 - but a browser canvas is whatever the
    phone is. See HANDOFF section 28.
    """
    rect = pygame.Rect(rect)
    if rect.w <= 0 or rect.h <= 0:
        # Still the rect, because callers lay out against what comes back.
        return rect
    key = ("panel", rect.size, tuple(fill), tuple(edge), alpha, radius)
    layer = _LAYERS.get(key)
    if layer is None:
        layer = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(layer, (*fill, alpha), layer.get_rect(),
                         border_radius=radius)
        pygame.draw.rect(layer, (*edge, 255), layer.get_rect(), 2,
                         border_radius=radius)
        pygame.draw.rect(layer, (*shade(edge, 0.55), 140),
                         layer.get_rect().inflate(-6, -6), 1,
                         border_radius=radius)
        _LAYERS.put(key, layer)
    surface.blit(layer, rect.topleft)
    return rect


class _LayerCache:
    """Built layers kept by what they look like, within a memory budget.

    **A panel is the same picture every frame** (§95), and it was being
    rebuilt every frame: a fresh translucent surface the size of the
    panel, three rounded rectangles, then the blend - so a menu's frame,
    most of a 1920x1080 window, was allocated sixty times a second. Kept
    here instead, keyed on size and colours. The budget is bytes rather
    than entries because one frame is worth a thousand row highlights;
    past it everything goes, which is simpler than an LRU and only
    happens when a window is dragged through sizes.
    """

    BUDGET = 48 * 1024 * 1024

    def __init__(self):
        self.layers = {}
        self.used = 0

    def get(self, key):
        return self.layers.get(key)

    def put(self, key, layer):
        cost = layer.get_width() * layer.get_height() * 4
        if self.used + cost > self.BUDGET:
            self.layers.clear()
            self.used = 0
        self.layers[key] = layer
        self.used += cost


_LAYERS = _LayerCache()


def bar(surface, rect, ratio, fg, bg=(46, 42, 58), edge=(12, 10, 18),
        ghost=None, ghost_color=(250, 240, 160), label=None, assets=None,
        font="tiny"):
    """Stat bar. `ghost` draws a slower-draining trail behind the fill.

    A `label` is drawn centred in white over a dark shadow: numbers printed
    directly onto a bright fill in a dark ink are unreadable at a glance.
    """
    rect = pygame.Rect(rect)
    ratio = clamp(ratio, 0.0, 1.0)
    pygame.draw.rect(surface, bg, rect, border_radius=3)
    if ghost is not None and ghost > ratio:
        trail = pygame.Rect(rect.x, rect.y, int(rect.w * clamp(ghost, 0, 1)),
                            rect.h)
        pygame.draw.rect(surface, ghost_color, trail, border_radius=3)
    if ratio > 0:
        fill = pygame.Rect(rect.x, rect.y, max(2, int(rect.w * ratio)), rect.h)
        pygame.draw.rect(surface, fg, fill, border_radius=3)
        # Glossy top edge.
        pygame.draw.line(surface, shade(fg, 1.35), (fill.x + 1, fill.y + 1),
                         (fill.right - 2, fill.y + 1))
    pygame.draw.rect(surface, edge, rect, 2, border_radius=3)
    if label is not None and assets is not None:
        draw_text(surface, assets, label, rect.center, font, (250, 250, 255),
                  center=True, shadow=True)
    return rect


def row_height(assets, font, pitch, pad=8):
    """How tall a row may be inside `pitch` without crushing its own text.

    **`list_view` already promised this and could not keep it** (s53). Its
    docstring says it gives up and scrolls "because a row shorter than its
    own text is not a row" - but the caller decides the row's height, and
    every list in the game does it by subtracting a constant from the
    pitch. At a desktop size that constant is fine; squeezed onto a phone
    it takes the box below the height of the font drawing into it, and the
    label hangs out of the highlight. Savvydev saw it as *"a LOT of places
    where the text like overlaps the selection highlight"*, which is what
    one shared mistake looks like from the outside.

    So: shrink by `pad`, but never below the font, and never past the
    pitch - a row taller than its own slot would collide with its
    neighbour, which is the bug this one is not worth trading for.
    """
    return int(clamp(pitch - pad, min(pitch - 2, assets.font(font).get_height() + 2),
                     max(2, pitch - 2)))


def row_text(surface, assets, text, rect, font="small", color=INK,
             pad=10, right=False):
    """A label sitting inside a row, vertically centred in it.

    **This exists because "a LOT of places" was one mistake repeated**
    (2026-09-05, s53). Savvydev: *"theres a LOT of places where the text like
    overlaps the selection highlight and stuff, and it doesnt look too
    good"*.

    Every list in this game draws its label at `row.y + 3` and its row at
    `row_h - 4`, and `list_view` is allowed to squeeze a row down to 20
    pixels. A 16-pixel line at a 3-pixel offset in a 16-pixel box hangs
    three pixels out of the highlight behind it - on every row of every
    list, at the sizes where rows get squeezed. It overflows no panel and
    collides with no text, so nothing caught it for as long as the lists
    have existed; it simply looks wrong, which is exactly what he
    reported.

    Centring against the *font* rather than against a constant is the fix,
    because the constant was only ever right at one row height.
    """
    height = assets.font(font).get_height()
    y = rect.y + max(0, (rect.h - height) // 2)
    if right:
        return draw_text(surface, assets, text, (rect.right - pad, y + height // 2),
                         font, color, right=True)
    return draw_text(surface, assets, text, (rect.x + pad, y), font, color)


def highlight(surface, rect, color=ACCENT, fill_alpha=52, border=2,
              radius=4):
    """Translucent selection highlight.

    pygame.draw.rect ignores the alpha channel when the destination has no
    per-pixel alpha - which the display surface does not - so the fill has to
    go through an SRCALPHA layer or it comes out fully opaque and swallows the
    label underneath it.
    """
    rect = pygame.Rect(rect)
    if rect.w <= 0 or rect.h <= 0:
        return rect                 # nothing to highlight; see `panel`
    # Kept, like a panel (§95) - a list redraws every highlight each frame.
    key = ("highlight", rect.size, tuple(color[:3]), fill_alpha, border,
           radius)
    layer = _LAYERS.get(key)
    if layer is None:
        layer = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(layer, (*color[:3], fill_alpha), layer.get_rect(),
                         border_radius=radius)
        if border:
            pygame.draw.rect(layer, (*color[:3], 235), layer.get_rect(),
                             border, border_radius=radius)
        _LAYERS.put(key, layer)
    surface.blit(layer, rect.topleft)
    return rect


def hp_color(ratio):
    """Green to amber to red as health drains."""
    if ratio > 0.5:
        return lerp_color((250, 196, 64), (96, 210, 108), (ratio - 0.5) * 2)
    return lerp_color((222, 66, 72), (250, 196, 64), ratio * 2)


class Tween:
    """Value that eases toward a target. Used for every bar in the game."""

    def __init__(self, value=0.0, speed=3.4):
        self.value = float(value)
        self.target = float(value)
        self.speed = speed

    def set(self, target, snap=False):
        self.target = float(target)
        if snap:
            self.value = self.target

    def update(self, dt):
        if abs(self.target - self.value) < 0.001:
            self.value = self.target
        else:
            self.value += (self.target - self.value) * clamp(dt * self.speed,
                                                             0.0, 1.0)
        return self.value


@dataclass
class Toast:
    text: str
    color: tuple = INK
    life: float = 3.0
    age: float = 0.0
    icon: str = ""


class ToastQueue:
    """Stacked corner notifications for loot, level-ups and quest progress."""

    def __init__(self, limit=5):
        self.items = []
        self.limit = limit

    def push(self, text, color=INK, life=3.0, icon=""):
        self.items.append(Toast(str(text)[:80], color, life, 0.0, icon))
        if len(self.items) > self.limit:
            self.items.pop(0)

    def update(self, dt):
        for toast in list(self.items):
            toast.age += dt
            if toast.age >= toast.life:
                self.items.remove(toast)

    # Left margin a notice may never cross. It is the same idea as any
    # other panel on a narrow screen (s29) - a notice is not exempt from
    # fitting just because it is temporary.
    EDGE = 8

    def draw(self, surface, assets, anchor_x, anchor_y):
        y = anchor_y
        # **A notice is sized to its text, and the text is not bounded by
        # anything the sender controls.** A host name arrives off a socket
        # and rides inside "Welcome back to X's valley"; on a phone that
        # slid the whole panel off the left edge, and `layout.py` never saw
        # it because it does not drive the flows that raise these.
        # `push` caps at 80 *characters*, which s29 already records as the
        # wrong measure - the font's own width is the only honest one.
        room = max(0, anchor_x - self.EDGE)
        for toast in reversed(self.items):
            # Slide in fast, fade out over the last half second.
            slide = ease_out_back(clamp(toast.age / 0.28, 0, 1))
            fade = clamp((toast.life - toast.age) / 0.5, 0, 1)
            label = ellipsize(assets.font("small"), toast.text, room - 26)
            img = assets.text(label, "small", toast.color)
            width = min(img.get_width() + 26, room)
            rect = pygame.Rect(anchor_x - width * slide, y, width, 28)
            layer = pygame.Surface(rect.size, pygame.SRCALPHA)
            pygame.draw.rect(layer, (18, 16, 26, int(228 * fade)),
                             layer.get_rect(), border_radius=5)
            pygame.draw.rect(layer, (*toast.color, int(220 * fade)),
                             layer.get_rect(), 2, border_radius=5)
            layer.blit(img, (13, 6))
            layer.set_alpha(int(255 * fade))
            surface.blit(layer, rect.topleft)
            y += 32


@dataclass
class FloatingText:
    x: float
    y: float
    text: str
    color: tuple
    life: float = 1.0
    age: float = 0.0
    vy: float = -52.0
    size: str = "mid"
    scale_pop: bool = False


@dataclass
class Shot:
    """One thing in flight. Born somewhere, spent somewhere else."""
    x0: float
    y0: float
    x1: float
    y1: float
    color: tuple
    # **Called `look`, not `kind`.** `kind="..."` is how a clickable
    # hotspot is registered, and `TestMouseReach` scans every drawing
    # module for that word - a projectile shape called `kind` reads to it
    # as a button nobody can click.
    look: str = "bolt"
    life: float = 0.26
    age: float = 0.0
    arc: float = 0.0
    spin: float = 0.0
    #: How big it draws (item 0ba), off the skill's own power - so a
    #: Meteor and a Firebolt are not the same object in two colours.
    scale: float = 1.0


class ShotLayer:
    """Arrows, bolts and thrown things (item 0al, 2026-09-06).

    Savvydev: *"I also mean stuff like projectiles and stuff yk?"*

    **This is a different kind of thing from everything in `anim.py`**,
    which is why it is a layer rather than another curve. §22's motions
    move a body that is already on screen - a lunge, a dart, a recoil. A
    projectile has no body: it is created at one combatant, crosses the
    arena, and ceases to exist. Nothing in the game had ever drawn one.

    Kept to shapes rather than sprites, for the same reason the rest of
    the game is generated (hard rule 2.1): an arrow is two lines and a
    bolt is a circle with a tail, and both take their colour from the
    element that fired them, so a fire bolt and a frost bolt are the same
    eight lines of drawing.
    """

    def __init__(self, limit=24):
        self.items = []
        self.limit = limit

    def add(self, start, end, color, look="bolt", life=0.26, arc=0.0,
            scale=1.0):
        if len(self.items) >= self.limit:
            self.items.pop(0)
        shot = Shot(start[0], start[1], end[0], end[1],
                    tuple(color), look, max(0.05, life),
                    arc=arc, spin=rng.rfloat(0.0, 6.283))
        # **How big it draws** (item 0ba). A Meteor and a Firebolt are
        # the same element and should not be the same object; the scale
        # comes off the skill's own power, so a new spell is sized
        # without anybody choosing a number for it.
        shot.scale = max(0.6, min(2.2, float(scale)))
        self.items.append(shot)
        return shot

    def clear(self):
        self.items = []

    def update(self, dt):
        for shot in list(self.items):
            shot.age += dt
            if shot.age >= shot.life:
                self.items.remove(shot)

    def at(self, shot):
        """Where a shot is now, and which way it is pointing."""
        t = clamp(shot.age / shot.life, 0.0, 1.0)
        x = shot.x0 + (shot.x1 - shot.x0) * t
        y = shot.y0 + (shot.y1 - shot.y0) * t
        # **The arc is what stops it reading as a slide.** A straight line
        # between two bodies is a laser; a shallow hop is a thing thrown.
        y -= math.sin(math.pi * t) * shot.arc
        return x, y, t

    def draw(self, surface, assets, camera_offset=(0, 0)):
        ox, oy = camera_offset
        for shot in self.items:
            x, y, t = self.at(shot)
            x, y = x - ox, y - oy
            fade = clamp((shot.life - shot.age) / 0.10, 0, 1)
            angle = math.atan2(shot.y1 - shot.y0, shot.x1 - shot.x0)
            if shot.look == "arrow":
                # **Chunky, because everything else here is** (s69). A
                # 2px line read as a scratch on the background rather than
                # as an object - this game is 16x16 sprites at 32px tiles
                # and a hairline belongs to a different one. A dark shaft,
                # a bright head, and a notch of fletching behind it.
                back = (x - math.cos(angle) * 16, y - math.sin(angle) * 16)
                pygame.draw.line(surface, (26, 22, 30), back, (x, y), 5)
                pygame.draw.line(surface, shot.color, back, (x, y), 3)
                side = angle + 1.57
                for away in (-1, 1):
                    tip = (back[0] + math.cos(side) * 4 * away,
                           back[1] + math.sin(side) * 4 * away)
                    pygame.draw.line(surface, (236, 230, 216), back, tip, 2)
                pygame.draw.circle(surface, (26, 22, 30), (int(x), int(y)), 4)
                pygame.draw.circle(surface, (250, 246, 232),
                                   (int(x), int(y)), 3)
            elif shot.look == "thrown":
                # Tumbles: a small square turning as it goes.
                size = 10
                box = pygame.Surface((size, size), pygame.SRCALPHA)
                box.fill((*shot.color, int(235 * fade)))
                spun = pygame.transform.rotate(
                    box, (shot.spin + shot.age * 14.0) * 57.3)
                surface.blit(spun, spun.get_rect(center=(int(x), int(y))))
            else:
                self._spell(surface, shot, x, y, t, angle, fade)

    #: The shapes a spell can cross the arena as (item 0ba). Everything
    #: here is drawn from circles, lines and one rotated square, for the
    #: reason the rest of the game is generated: hard rule 2.1.
    SPELL_LOOKS = ("bolt", "orb", "shard", "spark", "mote", "beam", "wave")

    def _spell(self, surface, shot, x, y, t, angle, fade):
        """One spell in flight, drawn as the thing it actually is.

        **Every magical attack in the game used to be `bolt`** and
        differed only in colour, so a Firebolt, a Meteor and a Frost
        Lance were one drawing tinted three ways. The shape comes from
        `skills.skill_look` and the size from `skills.skill_scale`, both
        derived from what the skill already says it is.
        """
        size = getattr(shot, "scale", 1.0)
        core = shot.color
        white = (252, 250, 240)
        dark = (26, 22, 30)
        px, py = int(x), int(y)

        if shot.look == "orb":
            # **A sphere with a halo, and it wobbles.** Fire is the one
            # element that should look like it is still burning while it
            # travels rather than like a thrown stone.
            wob = 1.0 + 0.12 * math.sin(shot.age * 22.0)
            outer = int(9 * size * wob)
            halo = pygame.Surface((outer * 4, outer * 4), pygame.SRCALPHA)
            pygame.draw.circle(halo, (*core, int(70 * fade)),
                               (outer * 2, outer * 2), outer * 2)
            surface.blit(halo, (px - outer * 2, py - outer * 2))
            pygame.draw.circle(surface, dark, (px, py), outer + 2)
            pygame.draw.circle(surface, core, (px, py), outer)
            pygame.draw.circle(surface, white, (px, py), max(2, outer // 2))
            return

        if shot.look == "shard":
            # A crystal: a long diamond, turning slowly, with a cold
            # trail of smaller ones behind it.
            for back in (18, 11, 5):
                tx = x - math.cos(angle) * back * size
                ty = y - math.sin(angle) * back * size
                small = max(2, int(3 * size * (1 - back / 24.0)))
                pygame.draw.circle(surface, core, (int(tx), int(ty)), small)
            long_r, wide_r = 11 * size, 5 * size
            spin = shot.spin + shot.age * 5.0
            points = []
            for step in range(4):
                out = long_r if step % 2 == 0 else wide_r
                a = spin + step * 1.5708
                points.append((x + math.cos(a) * out, y + math.sin(a) * out))
            pygame.draw.polygon(surface, dark, points)
            pygame.draw.polygon(surface, core,
                                [(x + (p[0] - x) * 0.75, y + (p[1] - y) * 0.75)
                                 for p in points])
            return

        if shot.look == "spark":
            # **A jagged line, redrawn every frame.** Lightning that
            # keeps its shape is a stick; the flicker is the whole read.
            back = 30 * size
            steps = 5
            last = (x - math.cos(angle) * back, y - math.sin(angle) * back)
            side = angle + 1.5708
            for step in range(1, steps + 1):
                along = step / steps
                jitter = (fx_rng.rfloat(-1.0, 1.0) * 7 * size
                          * (1.0 if step < steps else 0.0))
                nxt = (last[0] + math.cos(angle) * back / steps
                       + math.cos(side) * jitter,
                       last[1] + math.sin(angle) * back / steps
                       + math.sin(side) * jitter)
                pygame.draw.line(surface, dark, last, nxt, int(6 * size))
                pygame.draw.line(surface, core, last, nxt, int(4 * size))
                last = nxt
            pygame.draw.circle(surface, white, (px, py), int(4 * size))
            return

        if shot.look == "mote":
            # Not one thing: a handful of small ones orbiting a centre,
            # which is what taking something looks like when it is on its
            # way back to you.
            for n in range(5):
                phase = shot.spin + shot.age * 8.0 + n * 1.2566
                out = (5 + 4 * math.sin(shot.age * 6.0 + n)) * size
                mx = int(x + math.cos(phase) * out)
                my = int(y + math.sin(phase) * out * 0.7)
                pygame.draw.circle(surface, dark, (mx, my), int(3 * size))
                pygame.draw.circle(surface, core, (mx, my), int(2 * size))
            return

        if shot.look == "beam":
            # **It is already there.** A beam does not travel; it appears
            # along the whole line and fades, so `t` drives the fade
            # rather than the position.
            width = max(2, int(9 * size * (1.0 - t)))
            if width <= 0:
                return
            # A layer the size of the line, not of the window (§95): it
            # is rebuilt every frame the beam is fading.
            pad = width * 2 + 8
            box = pygame.Rect(min(shot.x0, shot.x1), min(shot.y0, shot.y1),
                              abs(shot.x1 - shot.x0) + 1,
                              abs(shot.y1 - shot.y0) + 1).inflate(pad, pad)
            box.x, box.y = int(box.x), int(box.y)
            start = (shot.x0 - box.x, shot.y0 - box.y)
            end = (shot.x1 - box.x, shot.y1 - box.y)
            layer = pygame.Surface(box.size, pygame.SRCALPHA)
            pygame.draw.line(layer, (*core, int(210 * (1.0 - t))),
                             start, end, width + 4)
            pygame.draw.line(layer, (*white, int(230 * (1.0 - t))),
                             start, end, max(1, width // 2))
            surface.blit(layer, box.topleft)
            return

        if shot.look == "wave":
            # A crescent, opening as it goes: force with nothing in it.
            span = int(12 * size + 10 * t)
            box = pygame.Rect(px - span, py - span, span * 2, span * 2)
            pygame.draw.arc(surface, dark, box, angle - 1.0, angle + 1.0,
                            max(3, int(6 * size)))
            pygame.draw.arc(surface, core, box.inflate(-4, -4),
                            angle - 0.9, angle + 0.9, max(2, int(4 * size)))
            return

        # A bolt: a bright head with a short tail behind it.
        for back, wide in ((14, 2), (10, 3), (6, 4), (2, 5)):
            trail_x = x - math.cos(angle) * back * size
            trail_y = y - math.sin(angle) * back * size
            pygame.draw.circle(surface, shot.color,
                               (int(trail_x), int(trail_y)),
                               max(1, int(wide * size)))
        pygame.draw.circle(surface, dark, (px, py), int(6 * size))
        pygame.draw.circle(surface, core, (px, py), int(5 * size))
        pygame.draw.circle(surface, white, (px, py), max(2, int(3 * size)))


class FloatingTextLayer:
    """Damage numbers, heals and status callouts."""

    def __init__(self, limit=48):
        self.items = []
        self.limit = limit

    def add(self, x, y, text, color=INK, size="mid", pop=False, spread=14):
        if len(self.items) >= self.limit:
            self.items.pop(0)
        self.items.append(FloatingText(
            x + rng.rfloat(-spread, spread), y, str(text), color,
            life=1.05 if not pop else 1.35, size=size, scale_pop=pop))

    def update(self, dt):
        for item in list(self.items):
            item.age += dt
            item.y += item.vy * dt
            item.vy += 62 * dt          # gentle gravity so numbers arc
            if item.age >= item.life:
                self.items.remove(item)

    def draw(self, surface, assets, camera_offset=(0, 0)):
        ox, oy = camera_offset
        for item in self.items:
            fade = clamp((item.life - item.age) / 0.45, 0, 1)
            img = assets.text(item.text, item.size, item.color)
            if item.scale_pop:
                grow = 1.0 + 0.45 * (1.0 - ease_out_cubic(
                    clamp(item.age / 0.22, 0, 1)))
                img = pygame.transform.scale(
                    img, (int(img.get_width() * grow),
                          int(img.get_height() * grow)))
            shadow = assets.text(item.text, item.size, (8, 6, 12))
            if item.scale_pop:
                shadow = pygame.transform.scale(shadow, img.get_size())
            img = img.copy()
            img.set_alpha(int(255 * fade))
            shadow.set_alpha(int(190 * fade))
            rect = img.get_rect(center=(int(item.x - ox), int(item.y - oy)))
            surface.blit(shadow, (rect.x + 2, rect.y + 2))
            surface.blit(img, rect)


@dataclass
class Particle:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    age: float = 0.0
    color: tuple = (255, 255, 255)
    size: int = 3
    gravity: float = 220.0
    fade: bool = True


class ParticleSystem:
    def __init__(self, limit=420):
        self.items = []
        self.limit = limit

    def _add(self, particle):
        if len(self.items) < self.limit:
            self.items.append(particle)

    def burst(self, x, y, count=14, color=(250, 200, 96), speed=170,
              life=0.55, size=3, gravity=220.0):
        for _ in range(count):
            angle = rng.rfloat(0, math.tau)
            velocity = rng.rfloat(speed * 0.35, speed)
            self._add(Particle(x, y, math.cos(angle) * velocity,
                               math.sin(angle) * velocity,
                               life * rng.rfloat(0.6, 1.3), 0.0, color,
                               rng.rint(max(1, size - 1), size + 1), gravity))

    def spray(self, x, y, direction=(1, 0), count=10, color=(250, 120, 60),
              speed=220, life=0.4, spread=0.7):
        base = math.atan2(direction[1], direction[0])
        for _ in range(count):
            angle = base + rng.rfloat(-spread, spread)
            velocity = rng.rfloat(speed * 0.4, speed)
            self._add(Particle(x, y, math.cos(angle) * velocity,
                               math.sin(angle) * velocity,
                               life * rng.rfloat(0.6, 1.2), 0.0, color,
                               rng.rint(2, 4), 90.0))

    def ambient(self, x, y, color=(250, 240, 160)):
        """Slow drifting mote: fireflies in the meadow, embers in the wastes."""
        self._add(Particle(x, y, rng.rfloat(-14, 14), rng.rfloat(-26, -6),
                           rng.rfloat(1.4, 3.0), 0.0, color, 2, 0.0))

    def update(self, dt):
        for p in list(self.items):
            p.age += dt
            if p.age >= p.life:
                self.items.remove(p)
                continue
            p.x += p.vx * dt
            p.y += p.vy * dt
            p.vy += p.gravity * dt
            p.vx *= (1.0 - clamp(dt * 1.6, 0, 1))

    def draw(self, surface, camera_offset=(0, 0)):
        ox, oy = camera_offset
        for p in self.items:
            fade = clamp(1.0 - p.age / p.life, 0, 1) if p.fade else 1.0
            size = max(1, int(p.size * (0.4 + fade * 0.6)))
            layer = pygame.Surface((size, size), pygame.SRCALPHA)
            layer.fill((*p.color, int(240 * fade)))
            surface.blit(layer, (int(p.x - ox), int(p.y - oy)))

    def clear(self):
        self.items.clear()


class ScrollList:
    """Index plus visible window. Keeps selection on screen while scrolling."""

    def __init__(self, visible=10):
        self.index = 0
        self.offset = 0
        self.visible = visible

    def clamp_to(self, count):
        if count <= 0:
            self.index = self.offset = 0
            return
        self.index = int(clamp(self.index, 0, count - 1))
        self.offset = int(clamp(self.offset, 0,
                                max(0, count - self.visible)))
        if self.index < self.offset:
            self.offset = self.index
        elif self.index >= self.offset + self.visible:
            self.offset = self.index - self.visible + 1

    def move(self, step, count):
        if count <= 0:
            return
        self.index = (self.index + step) % count
        self.clamp_to(count)

    def window(self, items):
        self.clamp_to(len(items))
        return list(enumerate(items))[self.offset:self.offset + self.visible]


# Which stats matter most when there is not room for all of them.
STAT_PRIORITY = ("atk", "mag", "defense", "hp", "res", "spd", "mp",
                 "crit", "lifesteal", "crit_dmg")


# Below this, a frame cannot hold a list beside a card. Two columns need
# roughly 200 for the list, 24 of gutter, and 260 for the card - less than
# that and the card wraps every sentence one word per line, which is how
# `draw_story` managed 265 layout failures on a single screen.
TWO_COLUMN_WIDTH = 520


def is_narrow(rect):
    """Whether this frame is too tight for two columns.

    A screen that answers yes should show **one** of its columns, not both
    squeezed. Hiding a list behind a counter is a real design; a 60-pixel
    card is not a design at all.
    """
    return rect.w < TWO_COLUMN_WIDTH


def ellipsize(font, text, width):
    """`text`, shortened until it fits `width`, with an ellipsis if cut.

    Truncating by character count is the obvious way and it is wrong: a
    proportional font makes "Blessed Scale Armor" and "IIIIIIIIIIIIIIIIIII"
    very different widths, so the only honest measure is the font's own.
    Returns "" when even one character will not fit, because half a letter
    is worse than nothing.
    """
    text = str(text)
    if width <= 0:
        return ""
    if font.size(text)[0] <= width:
        return text
    cut = text
    while cut and font.size(cut + "\u2026")[0] > width:
        cut = cut[:-1]
    return (cut + "\u2026") if cut else ""


def list_view(top, bottom, count, index, natural=40, minimum=26,
              was=None, name=""):
    """How to fit `count` rows between `top` and `bottom`.

    Returns `(start, visible, pitch)` - the first row to draw, how many of
    them, and how far apart they sit.

    **It squeezes a little before it scrolls.** A list that scrolls when it
    did not have to is a list you have to hunt through, and losing four
    pixels of padding is cheaper than losing the ability to see the whole
    thing at once. Past `minimum` it gives up and scrolls instead, because a
    row shorter than its own text is not a row.

    The window follows the cursor rather than paging, so moving down one row
    moves the list by one row and never by a screenful - which is what makes
    a long list navigable by holding a key.

    Several screens grew their own version of this arithmetic and the ones
    with a fixed row count never got it at all: settings marched twelve rows
    at forty pixels out through the bottom of a panel that had already
    shrunk to fit. This is that arithmetic, once.
    """
    room = max(0, int(bottom - top))
    count = max(0, int(count))
    if count <= 0:
        return 0, 0, natural
    pitch = natural
    if natural * count > room:
        pitch = int(clamp(room // count, minimum, natural))
    visible = max(1, min(count, room // max(1, pitch)))
    if visible >= count:
        _WINDOWS.pop(name or _caller(), None)
        return 0, count, pitch
    index = int(clamp(index, 0, count - 1))
    key = name or _caller()
    if was is None:
        was = _WINDOWS.get(key)
    start = window_start(index, visible, count, was)
    _WINDOWS[key] = start
    return start, visible, pitch


#: How many rows are kept between the cursor and the edge of the window
#: before the list moves. One is enough to see where you are going and
#: small enough that a long list still scrolls when you want it to.
SCROLL_MARGIN = 1

#: Where every list's last window position is remembered, keyed by the
#: name the caller passes. **Module state, deliberately** - the
#: alternative is threading a holder through fourteen call sites and
#: three screens that build their lists inline, and the thing being
#: remembered is one integer per list that only matters between two
#: frames of the same screen.
_WINDOWS = {}


def _caller():
    """Which list is asking, without every caller having to say.

    **Fourteen call sites, three of which build their list inline**, and
    the thing being remembered is one integer per list that matters only
    between two frames of the same screen. The caller's own function and
    line identify it exactly and nothing has to be threaded through.
    """
    import sys
    frame = sys._getframe(2)
    return "%s:%d" % (frame.f_code.co_name, frame.f_lineno)


def window_start(index, visible, count, was=None):
    """Which row a list starts at, given where the cursor is.

    **The list stays still and the cursor moves inside it** (item 0bn).
    Savvydev: *"the scrollable menu feels really weird i just cant pinpoint
    why exactly it does"* - and this is why. The old line was

        start = clamp(index - visible // 2, 0, count - visible)

    which **re-centres on the cursor every frame**, so pressing Down
    moved the whole list up by one row rather than moving the highlight
    down by one. Everything you were reading slid, on every keypress, on
    every screen with more rows than fit. That is a thing you can feel
    and cannot easily name.

    Now it only moves when the cursor reaches within `SCROLL_MARGIN` of
    an edge, which is what every list in every other program does.
    """
    if visible >= count:
        return 0
    # **The margin has to fit inside the page** (item 0bw). With
    # `SCROLL_MARGIN` at 1 and only two rows visible, the top margin and
    # the bottom margin are the same row: every call moved the page one
    # way, and the next call moved it back. On the arena picker - two
    # rows of thumbnails - that meant the grid **swapped between two
    # pages every frame**, which is what *"the second page of the maps
    # screen is super buggy"* looks like from a chair.
    #
    # It also made `window_start` non-idempotent, so merely *asking* the
    # screen how wide it was changed what it showed.
    margin = max(0, min(SCROLL_MARGIN, (int(visible) - 1) // 2))
    if was is None:
        # Nothing remembered: put the cursor a row from the top if it is
        # near the start, and otherwise centre once. Centring *once* is
        # fine; it is centring on every frame that reads as sliding.
        start = index - margin
    else:
        start = int(was)
        if index < start + margin:
            start = index - margin
        elif index > start + visible - 1 - margin:
            start = index - visible + 1 + margin
    return int(clamp(start, 0, count - visible))


def scrollbar(surface, rect, start, visible, count, tone=(150, 150, 190)):
    """A track and a thumb down the right edge of a list (item 0bn).

    His words: *"add like one of those scroll bars to any scrollable
    menus like that"*.

    **The thumb's size is the fraction you can see** and its position is
    how far down you are - which is the whole job, and is the part a line
    of text saying "3 above, 5 below" cannot do at a glance. Nothing is
    drawn when everything fits: a scrollbar on a list that does not
    scroll is furniture.
    """
    if count <= 0 or visible >= count:
        return None
    track = pygame.Rect(rect.right + 6, rect.y, 5, max(24, rect.h))
    pygame.draw.rect(surface, (40, 38, 56), track, border_radius=3)
    share = max(0.08, min(1.0, visible / float(count)))
    thumb_h = max(16, int(track.h * share))
    span = max(1, count - visible)
    at = clamp(start / float(span), 0.0, 1.0)
    thumb = pygame.Rect(track.x, int(track.y + (track.h - thumb_h) * at),
                        track.w, thumb_h)
    pygame.draw.rect(surface, tone, thumb, border_radius=3)
    return track


def scroll_note(surface, assets, rect, start, visible, count):
    """A scrollbar down the side, and the counts under it.

    **The bar is the new half** (item 0bn). The words say how many rows
    are hidden; the bar says *where you are*, which is the question a
    count cannot answer at a glance and the one you ask while scrolling.
    Both, because they answer different questions and neither is
    expensive.
    """
    scrollbar(surface, rect, start, visible, count)
    hidden = count - (start + visible)
    if start <= 0 and hidden <= 0:
        return
    parts = []
    if start > 0:
        parts.append("%d above" % start)
    if hidden > 0:
        parts.append("%d below" % hidden)
    draw_text(surface, assets, "  ".join(parts),
              (rect.right - 8, rect.bottom + 2), "tiny", FAINT, right=True)


def fit_stats(item, font, max_width):
    """A stat summary that fits, cut at whole stats rather than mid-word.

    The first version sliced the string at 34 characters, which produced
    "CDM" and "CRI". This drops the least important stats instead and says
    how many were left out.
    """
    parts = []
    for key in STAT_PRIORITY:
        value = item.stats.get(key, 0)
        if not value:
            continue
        suffix = "%" if key in ("crit", "crit_dmg", "lifesteal") else ""
        parts.append("%s %+d%s" % (SHORT[key], value, suffix))
    if not parts:
        return "no bonuses"

    shown = []
    for index, part in enumerate(parts):
        remaining = len(parts) - index - 1
        tail = "  +%d" % remaining if remaining else ""
        candidate = "  ".join(shown + [part]) + tail
        if font.size(candidate)[0] > max_width:
            if not shown:
                return "+%d stats" % len(parts)
            return "  ".join(shown) + ("  +%d" % (len(parts) - len(shown)))
        shown.append(part)
    return "  ".join(shown)


def compare_arrow(delta):
    if delta > 0:
        return " +%d" % delta, GOOD
    if delta < 0:
        return " %d" % delta, BAD
    return "", DIM


def item_card(surface, assets, item, pos, equipped=None, width=308,
              price_value=None, price_label="Value", player=None):
    """Detailed item tooltip, optionally diffed against the equipped piece.

    The comparison is the whole point: seeing "+4 ATK / -2 DEF" beats reading
    two stat blocks and doing the subtraction yourself.
    """
    if item is None:
        return pygame.Rect(pos, (width, 0))

    lines = []
    for key in STAT_KEYS:
        value = item.stats.get(key, 0)
        base = equipped.stats.get(key, 0) if equipped else 0
        if not value and not base:
            continue
        suffix = "%" if key in ("crit", "crit_dmg", "lifesteal") else ""
        text = "%-5s %+d%s" % (SHORT[key], value, suffix)
        delta_text, delta_color = ("", DIM)
        if equipped is not None:
            delta_text, delta_color = compare_arrow(value - base)
        lines.append((text, delta_text, delta_color))

    # Measure before drawing: a long name wraps to two lines and the panel
    # has to grow with it, or the stats spill out the bottom.
    name_lines = wrap_text(item.name, assets.font("body"), width - 28)[:2]
    body_h = 28 + len(name_lines) * 22 + 20 + len(lines) * 20 + 14
    if item.element != "physical":
        body_h += 16
    if item.affixes:
        body_h += 18
    if player is not None:
        body_h += 16                       # the affinity line
    if price_value is not None:
        body_h += 22
    rect = pygame.Rect(pos[0], pos[1], width, body_h)
    # **Nudged back on screen rather than drawn off it** (2026-09-01). The
    # card is anchored to whatever row asked for it, and a row near the
    # bottom of a screen - the equipped strip, on either menu - put a tall
    # card through the floor and over the footer. Every caller wants this,
    # so it lives here rather than in each of them.
    rect.bottom = min(rect.bottom, surface.get_height() - 8)
    rect.right = min(rect.right, surface.get_width() - 8)
    rect.top = max(8, rect.top)
    rect.left = max(8, rect.left)
    panel(surface, rect, edge=item.color)

    x = rect.x + 14
    y = rect.y + 10
    for line in name_lines:
        draw_text(surface, assets, line, (x, y), "body", item.color)
        y += 22
    # **The family is part of what the thing is**, so it sits on the same
    # line as the rarity and the slot rather than below the numbers
    # (2026-09-01, s47).
    from .items import FAMILY_LABEL, family_of
    family = family_of(item)
    sub = "%s  %s  ilvl %d" % (item.rarity_info.label.upper(),
                               FAMILY_LABEL.get(family, item.slot.title()),
                               item.ilvl)
    draw_text(surface, assets, sub, (x, y), "tiny", DIM)
    y += 18
    # And whether this hero trained with it, which is the half that
    # answers "why would an assassin use a bow".
    if family and player is not None:
        favoured = family in getattr(player.pclass, "favours", ())
        draw_text(surface, assets,
                  ("Your weapon  +%d%% damage"
                   % int(player.AFFINITY_BONUS * 100)) if favoured
                  else "Not %s training" % player.pclass.name,
                  (x, y), "tiny", GOOD if favoured else FAINT)
        y += 16
    if item.element != "physical":
        from .items import ELEMENT_COLOR
        draw_text(surface, assets, item.element.title(), (x, y), "tiny",
                  ELEMENT_COLOR.get(item.element, DIM))
        y += 16

    for text, delta_text, delta_color in lines:
        draw_text(surface, assets, text, (x, y), "small", INK)
        if delta_text:
            draw_text(surface, assets, delta_text,
                      (x + 118, y), "small", delta_color)
        y += 20

    if item.affixes:
        draw_text(surface, assets, ", ".join(item.affixes[:3])[:40],
                  (x, y), "tiny", (176, 160, 210))
        y += 18
    if price_value is not None:
        draw_text(surface, assets, "%s: %d g" % (price_label, price_value),
                  (x, y), "small", ACCENT)
    return rect


def status_chips(surface, assets, statuses, pos, max_chips=6):
    """Row of small coloured tags for active buffs and ailments."""
    x, y = pos
    for status in statuses[:max_chips]:
        info = status.info
        label = "%s %d" % (info.name[:6], status.turns)
        img = assets.text(label, "tiny", (16, 14, 22))
        chip = pygame.Rect(x, y, img.get_width() + 12, 18)
        pygame.draw.rect(surface, info.color, chip, border_radius=4)
        pygame.draw.rect(surface, shade(info.color, 0.6), chip, 1,
                         border_radius=4)
        surface.blit(img, (chip.x + 6, chip.y + 3))
        x += chip.w + 5
    return x


class Fade:
    """Screen wipe used between scenes."""

    def __init__(self):
        self.alpha = 0.0
        self.direction = 0        # -1 fading in, +1 fading out
        self.on_done = None
        self.speed = 480.0

    def out(self, callback=None, speed=480.0):
        self.direction = 1
        self.on_done = callback
        self.speed = speed

    def inward(self):
        self.alpha = 255.0
        self.direction = -1
        self.on_done = None

    @property
    def busy(self):
        return self.direction != 0

    def update(self, dt):
        if self.direction == 0:
            return
        self.alpha += self.direction * self.speed * dt
        if self.direction > 0 and self.alpha >= 255:
            self.alpha = 255.0
            self.direction = 0
            callback, self.on_done = self.on_done, None
            if callback:
                callback()
            self.inward()
        elif self.direction < 0 and self.alpha <= 0:
            self.alpha = 0.0
            self.direction = 0

    def draw(self, surface):
        if self.alpha <= 0:
            return
        layer = pygame.Surface(surface.get_size())
        layer.fill((6, 5, 10))
        layer.set_alpha(int(clamp(self.alpha, 0, 255)))
        surface.blit(layer, (0, 0))


# Footer chips name a key; clicking one should do what pressing it does.
HINT_KEYS = {
    "e": pygame.K_e, "i": pygame.K_i, "c": pygame.K_c, "j": pygame.K_j,
    "b": pygame.K_b,
    "u": pygame.K_u, "x": pygame.K_x, "esc": pygame.K_ESCAPE,
    "enter": pygame.K_RETURN, "tab": pygame.K_TAB, "f5": pygame.K_F5,
    "space": pygame.K_SPACE, "shift": pygame.K_LSHIFT,
    "up/down": pygame.K_DOWN, "left/right": pygame.K_RIGHT,
    "1": pygame.K_1, "2": pygame.K_2, "3": pygame.K_3, "4": pygame.K_4,
    "5": pygame.K_5, "del": pygame.K_DELETE, "f11": pygame.K_F11,
}


def hint_key_code(label):
    """The key a footer chip stands for, or None.

    **The dict is the special cases, not the list** (2026-08-31). It was
    the whole list, so a footer could advertise a key that no click would
    ever reach - `G` for the guide and `R` for a respec both were, and any
    key the player rebinds to something not in the dict would be too. The
    fallback asks pygame for its own name table, which knows every key
    there is; the dict stays for the labels pygame cannot parse, like
    "Up/Down" and "Esc".
    """
    name = str(label).strip().lower()
    code = HINT_KEYS.get(name)
    if code is not None:
        return code
    try:
        return pygame.key.key_code(name)
    except (ValueError, TypeError):
        return None


def skill_card(surface, assets, skill, player, pos, width=300):
    """What a skill costs, what it does, and roughly what it hits for.

    **The damage number is honest about being an estimate.** The real one
    comes out of `battle.compute_damage`, which subtracts the defender's
    guard and rolls a spread - neither of which exists outside a fight. So
    this shows the part that is knowable here: the attacker's side of the
    formula, `power x the stat it scales on`, times the number of hits,
    and says "about".

    Called from the hero sheet on a right-click (2026-08-31). The MP figure
    stays on the row itself as well - *"still keep the 8 MP thing you have
    on the far right"* - because the card is a thing you ask for and the
    row is a thing you scan.
    """
    if skill is None:
        return pygame.Rect(pos, (width, 0))

    lines = []
    if skill.kind == "attack" or skill.kind == "drain":
        stat = player.stat(skill.scales) if player else 0
        hit = max(1, int(stat * skill.power))
        lines.append(("about %d damage%s"
                      % (hit, " x%d" % skill.hits if skill.hits > 1 else ""),
                      INK))
        lines.append(("%s, scales with %s"
                      % (skill.element.title(),
                         "magic" if skill.scales == "mag" else "attack"), DIM))
    elif skill.kind == "heal":
        stat = player.stat("mag") if player else 0
        lines.append(("about %d healing" % max(1, int(stat * skill.power)),
                      (118, 220, 138)))
    if skill.status:
        lines.append(("%s for %d turns (%d%%)"
                      % (skill.status.title(), skill.status_turns,
                         int(skill.status_chance * 100)), (226, 168, 96)))
    if skill.description:
        for line in wrap_text(skill.description, assets.font("tiny"),
                              width - 24):
            lines.append((line, FAINT))

    height = 54 + len(lines) * 16
    box = pygame.Rect(pos[0], pos[1], width, height)
    # Nudged back on screen rather than drawn off it: the row this hangs
    # off can be anywhere, including the bottom of a short window.
    box.right = min(box.right, surface.get_width() - 8)
    box.bottom = min(box.bottom, surface.get_height() - 8)
    box.left = max(8, box.left)
    box.top = max(8, box.top)
    panel(surface, box, edge=ACCENT, alpha=242)
    draw_text(surface, assets, skill.name, (box.x + 12, box.y + 10), "small",
              INK)
    draw_text(surface, assets, "%d MP" % skill.mp,
              (box.right - 12, box.y + 18), "tiny", ACCENT, right=True)
    y = box.y + 34
    for text, colour in lines:
        draw_text(surface, assets, text, (box.x + 12, y), "tiny", colour)
        y += 16
    return box


def key_hint(surface, assets, hints, pos, gap=18, game=None, hot=True,
             right_edge=None):
    """Renders "E Interact   I Bag   Esc Menu" style footers.

    When a `game` is passed, each chip and its label register as a clickable
    region bound to that key, so anything the footer advertises can be done
    with the mouse as well. The click replays the keypress rather than
    duplicating the action, so the two inputs cannot drift apart.

    **Stops rather than spilling.** A footer wider than its window used to
    run a half-drawn chip off the edge and register a hotspot nobody could
    reach. It now drops whole hints from the end, which is why the most
    important one goes first in every call: on a phone the tail is what
    goes. `right_edge` defaults to the window, so callers inside a panel
    can hand it a tighter one.
    """
    x, y = pos
    limit = surface.get_width() - 8 if right_edge is None else right_edge
    for key, label in hints:
        code = hint_key_code(key) if (game is not None and hot) else None
        kimg = assets.text(key, "tiny", (18, 16, 24))
        limg_width = assets.text(label, "tiny", DIM).get_width()
        span = pygame.Rect(x, y - 3, kimg.get_width() + 12 + 5 + limg_width,
                           25)
        if span.right > limit:
            break                   # no room; drop this hint and the rest
        hovered = code is not None and game.hover == ("key", code)

        box = pygame.Rect(x, y, kimg.get_width() + 12, 19)
        pygame.draw.rect(surface, ACCENT if hovered else (196, 196, 214), box,
                         border_radius=4)
        surface.blit(kimg, (box.x + 6, box.y + 3))
        x += box.w + 5
        surface.blit(assets.text(label, "tiny", INK if hovered else DIM),
                     (x, y + 3))
        x += limg_width + gap
        if code is not None:
            game.ui.hot.add(span, "key", code)
    return x


class Hotspots:
    """Clickable regions registered by the draw pass.

    Screens already compute a rect for every row they draw, so they register
    it here as they go. Mouse input then resolves a position to (kind, index)
    and drives the *same* cursor fields the keyboard uses - there is no second
    copy of the selection logic to drift out of sync.
    """

    def __init__(self):
        self.items = []

    def clear(self):
        self.items.clear()

    def add(self, rect, kind, index=0):
        self.items.append((pygame.Rect(rect), kind, index))
        return rect

    def at(self, pos):
        """Topmost registered region under `pos`, or (None, None)."""
        for rect, kind, index in reversed(self.items):
            if rect.collidepoint(pos):
                return kind, index
        return None, None
