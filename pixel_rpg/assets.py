"""Surface construction, fonts and cached procedural textures.

Everything the game draws is generated at runtime from the character grids in
`spritedata`, so there are no image files to ship or load.
"""
from __future__ import annotations

import math

import os

import pygame

from . import spritedata as sd
from . import textures
from .util import RNG, clamp, hsv_to_rgb, lerp_color, shade

TRANSPARENT = {".", " "}


def init_pygame():
    """Start only the subsystems this game uses.

    `pygame.init()` brings up every subsystem, including joystick and CD
    audio. On some Windows setups enumerating those costs about ten seconds
    each, which showed up as the window taking fifteen seconds to appear.
    Display and font are instant; the mixer is started separately by `Audio`,
    which already degrades gracefully when there is no device.
    """
    pygame.display.init()
    pygame.font.init()


def build_surface(rows, scale=1, palette=None, outline=False):
    """Turn a character grid into a per-pixel-alpha Surface."""
    palette = palette or sd.PALETTE
    h = len(rows)
    w = len(rows[0]) if h else 0
    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    px = pygame.PixelArray(surf)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch in TRANSPARENT:
                continue
            color = palette.get(ch)
            if color:
                px[x, y] = surf.map_rgb((*color, 255))
    del px
    if outline:
        surf = add_outline(surf)
    if scale > 1:
        surf = pygame.transform.scale(
            surf, (surf.get_width() * scale, surf.get_height() * scale))
    return surf


def add_outline(surf, color=(12, 10, 18)):
    """One-pixel dark border around opaque pixels: makes sprites pop on any tile."""
    w, h = surf.get_size()
    out = pygame.Surface((w + 2, h + 2), pygame.SRCALPHA)
    mask = pygame.mask.from_surface(surf)
    silhouette = mask.to_surface(setcolor=(*color, 255), unsetcolor=(0, 0, 0, 0))
    for dx, dy in ((0, 1), (2, 1), (1, 0), (1, 2), (0, 0), (2, 0), (0, 2), (2, 2)):
        out.blit(silhouette, (dx, dy))
    out.blit(surf, (1, 1))
    return out


def tint_surface(surf, color, strength=0.5):
    """Blend a sprite toward a colour without losing its shading."""
    tinted = surf.copy()
    overlay = pygame.Surface(surf.get_size(), pygame.SRCALPHA)
    overlay.fill((*color[:3], int(clamp(strength * 255, 0, 255))))
    tinted.blit(overlay, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    result = surf.copy()
    result.blit(tinted, (0, 0))
    return result


def flash_surface(surf, color=(255, 255, 255), strength=0.85):
    """Solid-colour silhouette used for hit flashes."""
    mask = pygame.mask.from_surface(surf)
    flashed = mask.to_surface(setcolor=(*color, int(255 * strength)),
                              unsetcolor=(0, 0, 0, 0))
    out = surf.copy()
    out.blit(flashed, (0, 0))
    return out


def hue_shift(rows, degrees):
    """Recolour a grid by rotating every palette entry through HSV."""
    import colorsys
    shifted = {}
    for ch, rgb in sd.PALETTE.items():
        h, s, v = colorsys.rgb_to_hsv(*[c / 255 for c in rgb])
        h = (h + degrees / 360.0) % 1.0
        shifted[ch] = hsv_to_rgb(h, s, v)
    return shifted


def make_shadow(width, height):
    surf = pygame.Surface((width, height), pygame.SRCALPHA)
    pygame.draw.ellipse(surf, (0, 0, 0, 90), surf.get_rect())
    return surf


def vertical_gradient(size, top, bottom):
    w, h = size
    surf = pygame.Surface((1, h))
    for y in range(h):
        surf.set_at((0, y), lerp_color(top, bottom, y / max(1, h - 1)))
    return pygame.transform.scale(surf, (w, h))


def radial_vignette(size, strength=140):
    """Soft dark corners. Built once, blitted every frame."""
    w, h = size
    small = pygame.Surface((64, 36), pygame.SRCALPHA)
    cx, cy = 32.0, 18.0
    maxd = math.hypot(cx, cy)
    for y in range(36):
        for x in range(64):
            d = math.hypot(x - cx, y - cy) / maxd
            alpha = int(clamp((d - 0.45) / 0.55 * strength, 0, 255))
            small.set_at((x, y), (0, 0, 0, alpha))
    return pygame.transform.smoothscale(small, (w, h))


# Tile textures now live in `textures`, which builds structured materials
# rather than scattering noise. Kept as aliases so callers do not care.
TILE_STYLE = {name: (spec[0], spec[1], 0.0, "material")
              for name, spec in textures.MATERIALS.items()}
ANIMATED_TILES = textures.ANIMATED
make_tile = textures.make_tile


class Assets:
    """Lazily-built, cached art. One instance lives on the Game object."""

    FONT_CANDIDATES = "consolas,dejavusansmono,couriernew,menlo,monospace"
    #: A font file to use instead of asking the system. **Only the checker
    #: sets it** (`tests/layout.py --font PATH`), so a session on a machine
    #: without Consolas can still measure against a named face (§94).
    FONT_PATH = None

    def __init__(self, tile_size=32):
        self.tile_size = tile_size
        # **Every layout measurement in the game goes through this font.**
        # `fit_stats`, `ellipsize`, the truncation in half a dozen menus and
        # the whole of `tests/layout.py` all call `font.size()`, so a machine
        # with a different one measures a different game. It is recorded
        # rather than left implicit, because "0 layout problems" is a claim
        # about a font as much as about the code - and the checker prints it.
        #
        # A bundled font would remove the variance and was tried: Silkscreen
        # is the obvious pixel face and it is **caps-only**, which turns
        # every warden's epitaph into shouting. Not worth it. If one is ever
        # bundled it needs real lowercase and roughly Consolas' metrics.
        path = self.FONT_PATH or pygame.font.match_font(self.FONT_CANDIDATES)
        self.font_path = path
        self.font_name = (os.path.basename(path) if path
                          else "pygame default")
        self.fonts = {
            "tiny": pygame.font.Font(path, 13),
            "small": pygame.font.Font(path, 16),
            "body": pygame.font.Font(path, 19),
            "mid": pygame.font.Font(path, 24),
            "big": pygame.font.Font(path, 34),
            "huge": pygame.font.Font(path, 52),
        }
        self.fonts["big"].set_bold(True)
        self.fonts["huge"].set_bold(True)

        self._sprites = {}
        self._tiles = {}
        self._text = {}
        self._misc = {}

    # -- fonts -------------------------------------------------------------

    def font(self, name="body"):
        return self.fonts.get(name, self.fonts["body"])

    def text(self, string, font="body", color=(238, 238, 246)):
        """Cached text surface, bounded so a chatty battle log cannot leak."""
        key = (string, font, color)
        surf = self._text.get(key)
        if surf is None:
            if len(self._text) > 900:
                self._text.clear()
            surf = self.font(font).render(str(string), True, color)
            self._text[key] = surf
        return surf

    # -- sprites -----------------------------------------------------------

    def _rows_for(self, name):
        """The art for a name, or something rather than nothing.

        **The fallback used to be `sd.SLIME`, which does not exist.** The
        grid is `SLIME16`, so an unknown sprite raised `AttributeError`
        from inside the draw loop and took down every screen that draws the
        overworld - the exact opposite of what a fallback is for. Found by
        placing a prop called `house`, which is not a prop either (s42).
        """
        for group in (sd.MONSTERS, sd.BOSSES, sd.PROPS, sd.HERO):
            if name in group:
                return group[name]
        return sd.SLIME16

    def sprite_scale(self):
        """Whole-number scale from the 16x16 art grid to the tile size."""
        return max(1, self.tile_size // sd.GRID)

    def sprite(self, name, scale=None, outline=True):
        scale = scale or self.sprite_scale()
        key = (name, scale, outline)
        surf = self._sprites.get(key)
        if surf is None:
            surf = build_surface(self._rows_for(name), scale=1, outline=outline)
            surf = pygame.transform.scale(
                surf, (surf.get_width() * scale, surf.get_height() * scale))
            self._sprites[key] = surf
        return surf

    def hero(self, facing="front", step=0, scale=None, appearance=None):
        """The hero, shaped by build, hair style and walk phase.

        `step` is the walk frame; the leg and boot rows are swapped rather
        than a whole second sprite being drawn.
        """
        view = "side" if facing in ("left", "right") else facing
        base = sd.HERO[view]
        if appearance is not None:
            rows = appearance.rows(base, view, step)
        else:
            rows = list(base)
            frames = sd.WALK_LEGS.get(view)
            if frames and step:
                legs, boots = frames[step % len(frames)]
                rows[-2], rows[-1] = legs, boots

        scale = scale or self.sprite_scale()
        look = appearance.signature() if appearance is not None else None
        key = ("hero", view, step, scale, look, facing)
        surf = self._sprites.get(key)
        if surf is None:
            palette = (appearance.palette(sd.PALETTE)
                       if appearance is not None else None)
            surf = build_surface(rows, scale=1, palette=palette, outline=True)
            surf = pygame.transform.scale(
                surf, (surf.get_width() * scale, surf.get_height() * scale))
            if facing == "left":
                surf = pygame.transform.flip(surf, True, False)
            self._sprites[key] = surf
        return surf

    def creature(self, name, scale=None, frame=0, outline=True):
        """A monster or boss, with its idle frame applied.

        Creatures with a hand-drawn second pose alternate between the two;
        the rest breathe procedurally, which costs one cached surface and
        reads as alive rather than pasted on.
        """
        scale = scale or self.sprite_scale()
        alt = sd.MONSTER_ALT.get(name)
        use_alt = bool(alt) and frame % 2 == 1
        squash = 0
        if not alt and frame % 2 == 1:
            squash = 1
        key = ("creature", name, scale, use_alt, squash, outline)
        surf = self._sprites.get(key)
        if surf is None:
            rows = alt if use_alt else self._rows_for(name)
            surf = build_surface(rows, scale=1, outline=outline)
            width = surf.get_width() * scale
            height = surf.get_height() * scale
            if squash:
                surf = pygame.transform.scale(surf, (width + scale,
                                                     height - scale))
            else:
                surf = pygame.transform.scale(surf, (width, height))
            self._sprites[key] = surf
        return surf

    # -- tiles -------------------------------------------------------------

    def tile(self, name, frame=0, variant=0):
        """Cached tile. `variant` breaks up visible repetition on the ground."""
        frame = frame % 4 if name in ANIMATED_TILES else 0
        variant = variant % textures.VARIANTS
        key = (name, frame, variant)
        surf = self._tiles.get(key)
        if surf is None:
            surf = textures.make_tile(name, self.tile_size, frame, variant)
            self._tiles[key] = surf
        return surf

    def edge_shadows(self):
        """Inner shadows blitted where walkable ground meets a solid tile."""
        if "edges" not in self._misc:
            self._misc["edges"] = textures.make_edge_shadow(self.tile_size)
        return self._misc["edges"]

    # -- misc --------------------------------------------------------------

    def shadow(self, width, height=None):
        height = height or max(4, width // 3)
        key = ("shadow", width, height)
        if key not in self._misc:
            self._misc[key] = make_shadow(width, height)
        return self._misc[key]

    def vignette(self, size):
        key = ("vignette", size)
        if key not in self._misc:
            self._misc[key] = radial_vignette(size)
        return self._misc[key]

    def gradient(self, size, top, bottom):
        key = ("grad", size, top, bottom)
        if key not in self._misc:
            self._misc[key] = vertical_gradient(size, top, bottom)
        return self._misc[key]
