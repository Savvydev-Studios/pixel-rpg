"""Build Pixel_RPG.ico from the game's own sprite art.

Windows shows a different icon size depending on the view - 16px in the
title bar and small list views, 32px on the desktop, 256px in the extra-large
tile view. Downscaling one big image makes the small sizes mushy, so each size
is drawn natively at a whole-pixel sprite scale, and all of them are packed
into a single multi-resolution .ico.
"""
import io
import os
import struct
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame

from pixel_rpg.assets import build_surface
from pixel_rpg import spritedata as sd

SIZES = (16, 24, 32, 48, 64, 128, 256)

BACKDROP_TOP = (46, 38, 78)
BACKDROP_BOTTOM = (18, 16, 30)
RIM = (250, 200, 96)
GLOW = (150, 90, 230)


def draw_icon(size):
    """One icon face: a lit backdrop, a warm rim, and the hero on top."""
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    radius = max(2, size // 6)

    # Vertical gradient body.
    body = pygame.Surface((size, size), pygame.SRCALPHA)
    for y in range(size):
        t = y / max(1, size - 1)
        color = tuple(int(BACKDROP_TOP[i] + (BACKDROP_BOTTOM[i]
                                             - BACKDROP_TOP[i]) * t)
                      for i in range(3))
        body.fill((*color, 255), (0, y, size, 1))
    mask = pygame.Surface((size, size), pygame.SRCALPHA)
    pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(),
                     border_radius=radius)
    body.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
    surf.blit(body, (0, 0))

    # Portal glow behind the hero, so the icon reads as "this one" at a glance.
    if size >= 32:
        glow = pygame.Surface((size, size), pygame.SRCALPHA)
        pygame.draw.circle(glow, (*GLOW, 90), (size // 2, int(size * 0.54)),
                           int(size * 0.30))
        pygame.draw.circle(glow, (*GLOW, 60), (size // 2, int(size * 0.54)),
                           int(size * 0.40))
        surf.blit(glow, (0, 0))

    # Hero, at a whole-number sprite scale so the pixels stay square.
    # Leave real padding: at 256 the hero should sit inside the frame with
    # the glow visible around it, not fill it corner to corner.
    margin = max(1, size // 5)
    scale = max(1, (size - margin * 2) // 18)
    hero = build_surface(sd.HERO["front"], scale=1, outline=True)
    hero = pygame.transform.scale(hero, (hero.get_width() * scale,
                                         hero.get_height() * scale))
    surf.blit(hero, ((size - hero.get_width()) // 2,
                     (size - hero.get_height()) // 2))

    # Warm rim last, so nothing paints over it.
    if size >= 24:
        pygame.draw.rect(surf, (*RIM, 235), surf.get_rect(),
                         max(1, size // 32), border_radius=radius)
    return surf


def png_bytes(surface, tmpdir):
    path = os.path.join(tmpdir, "icon_%d.png" % surface.get_width())
    pygame.image.save(surface, path)
    with open(path, "rb") as handle:
        return handle.read()


def build(out_path):
    __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
    pygame.display.set_mode((64, 64))
    tmpdir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(tmpdir, exist_ok=True)

    faces = []
    for size in SIZES:
        surface = draw_icon(size)
        faces.append((size, png_bytes(surface, tmpdir)))

    # ICONDIR, then one ICONDIRENTRY per face, then the PNG payloads.
    # PNG-compressed entries are supported by Windows Vista onward.
    header = struct.pack("<HHH", 0, 1, len(faces))
    offset = len(header) + 16 * len(faces)
    entries, payloads = b"", b""
    for size, data in faces:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                               len(data), offset)
        payloads += data
        offset += len(data)

    with open(out_path, "wb") as handle:
        handle.write(header + entries + payloads)

    for size in SIZES:
        stray = os.path.join(tmpdir, "icon_%d.png" % size)
        if size not in (256,) and os.path.exists(stray):
            os.remove(stray)
    pygame.quit()
    return out_path


def build_png(out_path, size=64):
    """One PNG face, which is what a browser tab wants.

    The .ico above is a Windows container; a web build needs a plain PNG and
    pygbag quietly falls back to its own generic pygame icon if it does not
    get one. Same artwork, one size, no packing.
    """
    __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
    pygame.display.set_mode((64, 64))
    folder = os.path.dirname(os.path.abspath(out_path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    pygame.image.save(draw_icon(size), out_path)
    pygame.quit()
    return out_path


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "build_assets/Pixel_RPG.ico"
    if target.lower().endswith(".png"):
        path = build_png(target)
        print("wrote %s (%d bytes)" % (path, os.path.getsize(path)))
        raise SystemExit(0)
    path = build(target)
    print("wrote %s (%d bytes, %d sizes)"
          % (path, os.path.getsize(path), len(SIZES)))
