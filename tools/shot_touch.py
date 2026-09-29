"""Screenshot the touch controls over the world, and on a phone-shaped window.

`tests/layout.py` checks text and panels; it has no opinion on whether a
thumbstick is sitting on top of the minimap. Look at these after moving a
control.

    python tools/shot_touch.py [output_dir]
"""
import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgtouch")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame                                                   # noqa: E402
from pixel_rpg import game as G                                 # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_touch"
os.makedirs(OUT, exist_ok=True)

g = G.Game()
g.new_run("Ysoldebane", "warden")
for _ in range(8):
    g.player.gain_xp(g.player.xp_to_next())
g.dialogue = None
g.state = G.OVERWORLD
g.touch.enabled = True


def shot(name, size, push=None):
    g.screen = pygame.display.set_mode(size)
    g.on_resize(*size) if hasattr(g, "on_resize") else None
    g.touch.layout(size)
    if push is not None:
        g.touch.stick_finger = 1
        g.touch.stick_at = (g.touch.stick_home[0] + push[0],
                            g.touch.stick_home[1] + push[1])
        g.touch._recompute()
    else:
        g.touch.stick_finger = None
        g.touch.held = set()
    for _ in range(3):
        g.update(1 / 60)
    g.draw()
    pygame.image.save(g.screen, os.path.join(OUT, name + ".png"))
    print("  wrote %s  %dx%d  held=%s"
          % (name, size[0], size[1], sorted(g.touch.held)))


shot("01_desktop_idle", (1280, 720))
shot("02_desktop_pushed", (1280, 720), push=(70, -50))
shot("03_phone_landscape", (844, 390), push=(60, 30))
shot("04_phone_portrait", (390, 844))
print("done ->", OUT)
