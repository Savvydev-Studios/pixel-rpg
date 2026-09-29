"""Frame strip of the overworld walk, and of stopping.

`tests/layout.py` turns its sprite rules **off** for the overworld (§36),
so nothing in the gate has ever looked at the hero out there. This is the
only thing that does.

    python tools/shot_walk.py [output_dir]

Two things to check, and the second is the one this was written for:

- **The walk plants.** One of the two contacts per cycle should squash the
  body a little; a step that only rises and falls is a float.
- **Letting go of the key settles rather than cuts.** `walk_anim` used to
  be set to exactly 0.0 on any frame with no input, so the hero teleported
  from wherever the bob had reached to standing perfectly still - which
  happens every few seconds of ordinary play (item 0al(b), s71).
"""
import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgwalk")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame                                                   # noqa: E402
from pixel_rpg import anim, game as G                           # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_walk"
os.makedirs(OUT, exist_ok=True)

g = G.Game()
g.new_run("Walker", "warden")
# A new run opens on the prologue scene, not on the island. `layout.py`
# does the same thing in three places.
g.state = G.OVERWORLD
g.dialogue = None
g.scene = None
# Centred on the hero, tight enough that a pixel of bob is visible.
CROP = pygame.Rect(596, 330, 88, 96)
#: A one-pixel plant on a 16x16 grid is invisible at 1:1 in a strip, and
#: the whole point of this tool is that somebody can *see* it. Nearest
#: neighbour, so what you are looking at is the pixels and not a blur.
ZOOM = 4


def frame():
    g.draw()
    return g.screen.subsurface(CROP).copy()


def strip(name, frames):
    sheet = pygame.Surface((CROP.w * ZOOM * len(frames), CROP.h * ZOOM))
    for n, one in enumerate(frames):
        big = pygame.transform.scale(
            one, (CROP.w * ZOOM, CROP.h * ZOOM))
        sheet.blit(big, (n * CROP.w * ZOOM, 0))
    pygame.image.save(sheet, os.path.join(OUT, name + ".png"))
    print("  wrote %s  (%d frames)" % (name, len(frames)))


def walk(steps, moving=True, dt=1 / 60.0):
    """Drive the walk clock directly rather than faking key state.

    The alternative is synthesising held keys through `pygame.key`, which
    is a lot of machinery to exercise two lines of arithmetic - and the
    two lines are the subject.
    """
    out = []
    for _ in range(steps):
        if moving:
            g.walk_anim += dt * 6.0
        g.walk_ease = anim.walk_ease(g.walk_ease, dt, moving)
        if not g.walk_ease:
            g.walk_anim = 0.0
        g.world_time += dt
        out.append(frame())
    return out


# 1. A full stride, every third frame: contact, pass, contact, pass - and
#    a plant on the way down.
frames = walk(24)
strip("01_stride", frames[::3])

# 2. Letting go. The bob decays over about a fifth of a second instead of
#    cutting to nothing, and the breath fades in underneath it.
frames = walk(18, moving=False)
strip("02_settle", frames[::2])

# 3. Standing still, over two seconds. It should not be a statue - every
#    other body in this game has breathed since `anim.breathe` existed.
frames = walk(120, moving=False)
strip("03_standing", frames[::17])

print("done ->", OUT)
