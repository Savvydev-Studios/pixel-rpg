"""Procedural ground textures.

The first pass generated tiles by scattering random specks over a flat fill,
which reads as television static next to hand-placed sprite pixels. These
generators build structure instead: blades that lean, pebbles with a highlight
and a shadow, facets with lit and shaded edges, cracks that wander.

Every generator is deterministic in (name, variant, frame), so the world can
draw four variants of each tile and stop looking like graph paper without
storing anything.
"""
from __future__ import annotations

import math

import pygame

from .util import RNG, clamp, lerp_color, shade

VARIANTS = 4


def _base_fill(surf, size, base, rng, roughness=0.06):
    """Vertical shade gradient plus low-frequency mottling.

    Flat fills are what made the old tiles look like plastic; every texture
    starts from gently uneven ground instead.
    """
    top = shade(base, 1.0 + roughness)
    bottom = shade(base, 1.0 - roughness)
    for y in range(size):
        surf.fill(lerp_color(top, bottom, y / max(1, size - 1)),
                  (0, y, size, 1))
    for _ in range(3):
        cx, cy = rng.rint(0, size - 1), rng.rint(0, size - 1)
        radius = rng.rint(max(2, size // 4), max(3, size // 2))
        patch = pygame.Surface((radius * 2, radius * 2), pygame.SRCALPHA)
        tint = shade(base, rng.rfloat(0.90, 1.10))
        pygame.draw.circle(patch, (*tint, 46), (radius, radius), radius)
        surf.blit(patch, (cx - radius, cy - radius))


def _blades(surf, size, rng, colors, count, low, high, lean=0.0,
            tip_chance=0.30, tip_shade=1.18):
    """Short vertical strokes. Reads as grass.

    `tip_chance` matters more than it sounds: a bright tip on every blade
    turns a field into television static, so most blades get none.
    """
    for _ in range(count):
        x = rng.rint(0, size - 1)
        base_y = rng.rint(size // 3, size - 1)
        height = rng.rint(low, high)
        color = rng.choice(colors)
        drift = int(lean * height * rng.rfloat(-1.0, 1.0))
        tip_x = int(clamp(x + drift, 0, size - 1))
        tip_y = max(0, base_y - height)
        pygame.draw.line(surf, color, (x, base_y), (tip_x, tip_y))
        if rng.chance(tip_chance):
            surf.set_at((tip_x, tip_y), shade(color, tip_shade))


def _pebbles(surf, size, rng, base, count):
    """Two-tone specks with a highlight and a shadow, so they have volume."""
    for _ in range(count):
        x, y = rng.rint(1, size - 4), rng.rint(1, size - 4)
        w, h = rng.rint(2, 3), rng.rint(1, 2)
        body = shade(base, rng.rfloat(1.08, 1.30))
        pygame.draw.rect(surf, shade(base, 0.70), (x, y + 1, w, h))
        pygame.draw.rect(surf, body, (x, y, w, h))
        surf.set_at((x, y), shade(body, 1.28))


def _cracks(surf, size, rng, color, count, length):
    """Wandering dark lines: cracked earth, ice, stone facets."""
    for _ in range(count):
        x, y = rng.rint(0, size - 1), rng.rint(0, size - 1)
        for _ in range(length):
            surf.set_at((int(x) % size, int(y) % size), color)
            x += rng.rint(-1, 1)
            y += 1 if rng.chance(0.62) else rng.rint(-1, 0)


def _sparkle(surf, size, rng, color, count):
    for _ in range(count):
        x, y = rng.rint(1, size - 2), rng.rint(1, size - 2)
        surf.set_at((x, y), color)
        if rng.chance(0.4):
            surf.set_at((x + 1, y), shade(color, 0.85))


# ------------------------------------------------------------ generators ---

def _grass(surf, size, base, speck, rng):
    _base_fill(surf, size, base, rng, 0.07)
    shades = [shade(base, 0.84), base, shade(speck, 0.94)]
    _blades(surf, size, rng, shades, max(6, int(size * 0.55)), 2,
            max(3, size // 6), lean=0.22, tip_chance=0.16, tip_shade=1.10)
    if rng.chance(0.32):
        fx, fy = rng.rint(3, size - 4), rng.rint(3, size - 4)
        petal = rng.choice([(238, 226, 120), (236, 160, 180), (228, 238, 250)])
        surf.set_at((fx, fy), petal)
        surf.set_at((fx + 1, fy), shade(petal, 0.78))


def _tallgrass(surf, size, base, speck, rng):
    """Deeper, denser growth - read as *darker* than open grass, not busier.

    The first pass drew ~105 strokes on a 32px tile with a bright tip on every
    one, which came out as noise. This is a quarter of the blades, all in a
    narrow tonal band above a darkened ground.
    """
    _base_fill(surf, size, shade(base, 0.74), rng, 0.08)
    _blades(surf, size, rng, [shade(base, 0.72), shade(base, 0.88), base],
            max(8, int(size * 0.75)), max(5, size // 4), max(8, size // 2),
            lean=0.22, tip_chance=0.18, tip_shade=1.12)
    # A handful of taller stems catch the light, and only a handful.
    _blades(surf, size, rng, [shade(speck, 0.92)],
            max(2, size // 12), max(7, size // 3), max(10, int(size * 0.6)),
            lean=0.26, tip_chance=0.5, tip_shade=1.15)


def _dirt(surf, size, base, speck, rng):
    _base_fill(surf, size, base, rng, 0.07)
    for _ in range(2):                      # cart ruts give the road a line
        y = rng.rint(size // 4, size - size // 4)
        pygame.draw.line(surf, shade(base, 0.84), (0, y),
                         (size, y + rng.rint(-1, 1)))
    _pebbles(surf, size, rng, speck, max(4, size // 5))


def _stone(surf, size, base, speck, rng):
    _base_fill(surf, size, base, rng, 0.05)
    for _ in range(rng.rint(3, 5)):         # facets, lit above, shaded below
        cx, cy = rng.rint(2, size - 3), rng.rint(2, size - 3)
        radius = rng.rint(max(2, size // 6), max(3, size // 3))
        points = []
        for i in range(6):
            angle = i * math.tau / 6 + rng.rfloat(-0.3, 0.3)
            points.append((cx + math.cos(angle) * radius,
                           cy + math.sin(angle) * radius))
        pygame.draw.polygon(surf, shade(base, rng.rfloat(0.94, 1.12)), points)
        pygame.draw.lines(surf, shade(base, 0.72), True, points, 1)
    _cracks(surf, size, rng, shade(base, 0.60), 2, size)


def _brick(surf, size, base, speck, rng):
    _base_fill(surf, size, shade(base, 0.80), rng, 0.04)
    course = max(4, size // 4)
    mortar = shade(base, 0.58)
    for row, y in enumerate(range(0, size, course)):
        offset = (row % 2) * (size // 4)
        x = -offset
        while x < size:
            brick = pygame.Rect(x + 1, y + 1, size // 2 - 2, course - 2)
            tone = shade(base, rng.rfloat(0.90, 1.14))
            pygame.draw.rect(surf, tone, brick)
            pygame.draw.line(surf, shade(tone, 1.22), (brick.x, brick.y),
                             (brick.right - 1, brick.y))
            pygame.draw.line(surf, shade(tone, 0.78), (brick.x, brick.bottom - 1),
                             (brick.right - 1, brick.bottom - 1))
            x += size // 2
        pygame.draw.line(surf, mortar, (0, y), (size, y))


def _wood(surf, size, base, speck, rng):
    """Floorboards. The seams stay quiet: a town square should not read as
    a barcode when a hundred tiles of it are on screen at once."""
    _base_fill(surf, size, base, rng, 0.04)
    plank = max(7, size // 2)
    for x in range(0, size, plank):
        tone = shade(base, rng.rfloat(0.96, 1.05))
        pygame.draw.rect(surf, tone, (x, 0, plank - 1, size))
        pygame.draw.line(surf, shade(base, 0.84), (x + plank - 1, 0),
                         (x + plank - 1, size))
        for _ in range(rng.rint(1, 2)):     # soft grain
            gy = rng.rint(1, size - 2)
            pygame.draw.line(surf, shade(tone, 0.93), (x + 1, gy),
                             (x + plank - 3, gy + rng.rint(-1, 1)))
        if rng.chance(0.25):                # nail head
            surf.set_at((x + 2, rng.rint(2, size - 3)), shade(base, 0.66))


def _water(surf, size, base, speck, rng, frame=0):
    for y in range(size):
        wave = math.sin((y / size) * math.tau * 1.6 + frame * 0.8)
        surf.fill(lerp_color(base, speck, (wave + 1) * 0.20), (0, y, size, 1))
    for _ in range(rng.rint(2, 4)):         # caustic highlights
        y = rng.rint(1, size - 2)
        x = int((math.sin(y * 0.7 + frame) * 0.5 + 0.5) * (size - 6))
        length = rng.rint(3, max(4, size // 3))
        pygame.draw.line(surf, shade(speck, 1.25), (x, y), (x + length, y))
        pygame.draw.line(surf, shade(speck, 1.05), (x + 1, y + 1),
                         (x + length - 1, y + 1))


def _ice(surf, size, base, speck, rng, frame=0):
    _base_fill(surf, size, base, rng, 0.05)
    _cracks(surf, size, rng, shade(base, 0.74), 3, size)
    for _ in range(2):                      # sheen bands drift with the frame
        y = (rng.rint(0, size - 1) + frame * 2) % size
        pygame.draw.line(surf, shade(speck, 1.15), (0, y), (size, y))
    _sparkle(surf, size, rng, (250, 253, 255), 3)


def _lava(surf, size, base, speck, rng, frame=0):
    for y in range(size):
        wave = math.sin((y / size) * math.tau * 1.2 + frame * 0.9)
        surf.fill(lerp_color(shade(base, 0.72), base, (wave + 1) * 0.5),
                  (0, y, size, 1))
    _cracks(surf, size, rng, shade(base, 0.42), 3, size)
    for _ in range(rng.rint(3, 6)):         # molten glow
        x, y = rng.rint(1, size - 2), rng.rint(1, size - 2)
        glow = pygame.Surface((6, 6), pygame.SRCALPHA)
        pygame.draw.circle(glow, (*speck, 120), (3, 3), 3)
        pygame.draw.circle(glow, (255, 244, 210, 200), (3, 3), 1)
        surf.blit(glow, (x - 3, y - 3))


def _snow(surf, size, base, speck, rng):
    _base_fill(surf, size, base, rng, 0.04)
    for _ in range(rng.rint(2, 4)):         # soft drifts
        cx, cy = rng.rint(0, size), rng.rint(size // 2, size)
        rx, ry = rng.rint(size // 3, size), rng.rint(2, max(3, size // 5))
        drift = pygame.Surface((rx * 2, ry * 2), pygame.SRCALPHA)
        pygame.draw.ellipse(drift, (*speck, 130), drift.get_rect())
        surf.blit(drift, (cx - rx, cy - ry))
    _sparkle(surf, size, rng, (255, 255, 255), 4)


def _ash(surf, size, base, speck, rng):
    _base_fill(surf, size, base, rng, 0.08)
    _cracks(surf, size, rng, shade(base, 0.55), 4, size)
    _pebbles(surf, size, rng, speck, max(3, size // 7))
    for _ in range(rng.rint(0, 2)):         # dying embers
        x, y = rng.rint(1, size - 2), rng.rint(1, size - 2)
        surf.set_at((x, y), (232, 122, 48))


def _cave(surf, size, base, speck, rng):
    _base_fill(surf, size, base, rng, 0.10)
    for _ in range(rng.rint(2, 4)):         # dark pits
        cx, cy = rng.rint(2, size - 3), rng.rint(2, size - 3)
        radius = rng.rint(2, max(3, size // 5))
        pit = pygame.Surface((radius * 2, radius * 2), pygame.SRCALPHA)
        pygame.draw.circle(pit, (0, 0, 0, 92), (radius, radius), radius)
        surf.blit(pit, (cx - radius, cy - radius))
    _pebbles(surf, size, rng, speck, max(3, size // 6))


def _roof(surf, size, base, speck, rng):
    """Overlapping clay tiles, so a house has a lid instead of a flat top."""
    _base_fill(surf, size, base, rng, 0.05)
    course = max(4, size // 4)
    for row, y in enumerate(range(0, size, course)):
        offset = (row % 2) * (course // 2)
        for x in range(-offset, size, course):
            tile = pygame.Rect(x, y, course - 1, course - 1)
            tone = shade(base, rng.rfloat(0.92, 1.12))
            pygame.draw.rect(surf, tone, tile)
            pygame.draw.arc(surf, shade(tone, 1.28),
                            (tile.x, tile.y, tile.w, tile.h * 2),
                            0.3, math.pi - 0.3, 1)
        pygame.draw.line(surf, shade(base, 0.66), (0, y), (size, y))


def _thatch(surf, size, base, speck, rng):
    """Straw roofing: many fine, near-vertical strands."""
    _base_fill(surf, size, base, rng, 0.07)
    _blades(surf, size, rng, [shade(base, 0.82), base, shade(speck, 0.95)],
            max(10, int(size * 1.1)), max(4, size // 3), max(7, size - 2),
            lean=0.06, tip_chance=0.12, tip_shade=1.10)
    for y in range(0, size, max(5, size // 3)):
        pygame.draw.line(surf, shade(base, 0.72), (0, y), (size, y))


def _sand(surf, size, base, speck, rng):
    _base_fill(surf, size, base, rng, 0.06)
    for _ in range(rng.rint(3, 5)):         # ripples
        y = rng.rint(1, size - 2)
        pygame.draw.arc(surf, shade(base, 0.88),
                        (rng.rint(-size // 2, size // 2), y,
                         size, max(3, size // 3)), 0.2, math.pi - 0.2, 1)
    _pebbles(surf, size, rng, speck, max(2, size // 10))


# name -> (base colour, accent colour, generator, animated)
MATERIALS = {
    "grass":     ((52, 124, 64),   (86, 168, 88),   _grass,     False),
    "tallgrass": ((36, 100, 52),   (74, 152, 80),   _tallgrass, False),
    "path":      ((136, 108, 74),  (172, 144, 106), _dirt,      False),
    "stone":     ((96, 96, 108),   (132, 132, 146), _stone,     False),
    "wall":      ((60, 56, 70),    (88, 84, 102),   _brick,     False),
    "water":     ((36, 82, 156),   (96, 152, 220),  _water,     True),
    "sand":      ((202, 180, 124), (226, 208, 158), _sand,      False),
    "snow":      ((214, 224, 238), (248, 252, 255), _snow,      False),
    "ice":       ((148, 196, 228), (206, 236, 250), _ice,       True),
    "ash":       ((78, 70, 72),    (110, 100, 100), _ash,       False),
    "lava":      ((178, 58, 24),   (252, 162, 56),  _lava,      True),
    "cave":      ((64, 58, 72),    (94, 86, 104),   _cave,      False),
    "wood":      ((124, 88, 54),   (152, 112, 70),  _wood,      False),
    "shop":      ((168, 140, 56),  (212, 184, 88),  _brick,     False),
    "shrine":    ((100, 128, 148), (146, 180, 202), _stone,     False),
    "arena":     ((58, 50, 72),    (86, 76, 102),   _stone,     False),
    "roof":      ((146, 62, 50),   (196, 98, 78),   _roof,      False),
    "roof2":     ((78, 84, 116),   (118, 126, 166), _roof,      False),
    "thatch":    ((156, 130, 68),  (204, 176, 100), _thatch,    False),
    # Regional grounds. Each is a sibling of one of the originals at the
    # same difficulty tier, so the map varies sideways as well as outward.
    "heath":     ((132, 130, 74),  (176, 172, 104), _grass,     False),
    "marsh":     ((58, 84, 62),    (86, 122, 84),   _tallgrass, False),
    "mire":      ((62, 66, 52),    (92, 96, 74),    _dirt,      False),
    "clay":      ((150, 92, 62),   (192, 126, 84),  _stone,     False),
    "pine":      ((44, 74, 66),    (70, 108, 92),   _grass,     False),
    "obsidian":  ((40, 34, 46),    (78, 68, 92),    _stone,     False),
    "salt":      ((206, 202, 190), (238, 236, 228), _sand,      False),
    "mountain":  ((88, 82, 92),    (132, 126, 140), _stone,     False),
    "rubble":    ((150, 142, 128), (192, 184, 168), _brick,     False),
}

ANIMATED = {name for name, spec in MATERIALS.items() if spec[3]}


def make_tile(name, size, frame=0, variant=0):
    """One tile texture. Deterministic in (name, frame, variant)."""
    base, accent, generator, animated = MATERIALS.get(name,
                                                      MATERIALS["grass"])
    rng = RNG(abs(hash((name, variant))) % (2 ** 31))
    surf = pygame.Surface((size, size))
    surf.fill(base)
    if animated:
        generator(surf, size, base, accent, rng, frame)
    else:
        generator(surf, size, base, accent, rng)

    # A lit top edge and a shadowed bottom edge, kept subtle so the grid does
    # not turn into a lattice of visible seams.
    top = pygame.Surface((size, 1), pygame.SRCALPHA)
    top.fill((255, 255, 255, 22))
    surf.blit(top, (0, 0))
    bottom = pygame.Surface((size, 1), pygame.SRCALPHA)
    bottom.fill((0, 0, 0, 34))
    surf.blit(bottom, (0, size - 1))
    return surf


def make_edge_shadow(size):
    """Soft inner shadow, blitted where a walkable tile meets a solid one.

    Cheap ambient occlusion: it stops walls and water from looking like
    stickers laid on top of the ground.
    """
    edges = {}
    for side in ("top", "bottom", "left", "right"):
        surf = pygame.Surface((size, size), pygame.SRCALPHA)
        depth = max(3, size // 5)
        for i in range(depth):
            alpha = int(72 * (1 - i / depth))
            if side == "top":
                surf.fill((0, 0, 0, alpha), (0, i, size, 1))
            elif side == "bottom":
                surf.fill((0, 0, 0, alpha), (0, size - 1 - i, size, 1))
            elif side == "left":
                surf.fill((0, 0, 0, alpha), (i, 0, 1, size))
            else:
                surf.fill((0, 0, 0, alpha), (size - 1 - i, 0, 1, size))
        edges[side] = surf
    return edges
