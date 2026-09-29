import os, sys, tempfile
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgcre")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pygame
from pixel_rpg import game as G, menus
from pixel_rpg.appearance import Appearance

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_creator"
os.makedirs(OUT, exist_ok=True)
g = G.Game()
g.pending_class = "cutpurse"
g.name_buffer = "Savvydev"
g.go(G.NAME_ENTRY)
for i, look in enumerate([Appearance(1, 1, 1, 0), Appearance(5, 8, 2, 4),
                          Appearance(6, 5, 3, 2)]):
    g.pending_appearance = look
    g.ui.creator_index = 1 + i
    for _ in range(3):
        g.update(1 / 60)
        g.draw()
    pygame.image.save(g.screen, os.path.join(OUT, "creator_%d.png" % i))
    print("  wrote creator_%d" % i)
pygame.quit()
