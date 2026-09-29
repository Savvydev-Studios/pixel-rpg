import os, sys, tempfile
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgui")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pygame
from pixel_rpg import game as G, menus
from pixel_rpg.modifiers import MODIFIERS

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_ui"
os.makedirs(OUT, exist_ok=True)
g = G.Game()


def shot(name, frames=3):
    for _ in range(frames):
        g.update(1 / 60)
        g.draw()
    pygame.image.save(g.screen, os.path.join(OUT, name + ".png"))
    print("  wrote", name)


# Modifiers: two chosen, cursor on a third, pointer on a fourth.
g.ui.slot_mode = "new"
g.go(G.MODIFIER_SELECT)
g.pending_modifiers.add("glass")
g.pending_modifiers.add("haunted")
g.ui.modifier_index = 5
g.hover = ("modifier", 7)
shot("01_modifiers")

# Settings, with the cursor on a row that has a value.
g.go(G.SETTINGS_SCREEN)
g.ui.settings_index = menus.SETTING_ROWS.index("Save folder")
g.hover = ("setting", menus.SETTING_ROWS.index("Difficulty"))
shot("02_settings")

# Class select: a class picked, with its Confirm button.
g.go(G.CLASS_SELECT)
g.ui.class_index = 2
g.hover = ("class", 0)
shot("03_class")
pygame.quit()
