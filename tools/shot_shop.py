import os, sys, tempfile
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgshop")
os.environ["PIXEL_RPG_SEED"] = "5"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pygame
from pixel_rpg import game as G, menus
from pixel_rpg.items import roll_item

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_shop"
os.makedirs(OUT, exist_ok=True)
g = G.Game()
g.pending_class = "warden"
g.new_run("Savvydev", 1)
g.dialogue = None
g.state = G.OVERWORLD
g.world_time = 120.0
g.player.gain_xp(24000)
g.player.gold = 123456
for _ in range(14):
    g.player.add_item(roll_item(g.player.level + 4, rarity="epic"))
g.player.add_consumable("potion", 11)
g.ui.refresh_shop(g.player, g.world)


def shot(name, frames=3):
    for _ in range(frames):
        g.update(1 / 60)
        g.draw()
    pygame.image.save(g.screen, os.path.join(OUT, name + ".png"))
    print("  wrote", name)


g.go(G.SHOP)
g.ui.shop.index = 3
shot("01_shop")
g.state = G.OVERWORLD
g.go(G.INVENTORY)
g.ui.inv.index = 2
shot("02_bag")
g.state = G.OVERWORLD
g.world_time = 120.0
shot("03_world", 12)
pygame.quit()
