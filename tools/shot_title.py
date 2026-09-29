import os, sys, tempfile
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgtitle")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pygame
from pixel_rpg import game as G, menus

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_title"
os.makedirs(OUT, exist_ok=True)
g = G.Game()
g.title_index = 1                       # "Continue" selected
g.hover = ("title", 3)                  # pointer over "Records"
for size in ((1280, 720), (1024, 768)):
    g.screen = pygame.display.set_mode(size)
    for _ in range(4):
        g.update(1 / 60)
        g.draw()
    pygame.image.save(g.screen, os.path.join(OUT, "title_%dx%d.png" % size))
    print("  wrote title_%dx%d" % size)
pygame.quit()
