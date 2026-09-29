"""Render representative frames to PNG so the visuals can be eyeballed."""
import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgshots")
os.environ["PIXEL_RPG_SEED"] = "2024"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame

from pixel_rpg import game as G
from pixel_rpg.entities import BOSS_DEFS, Boss, make_enemy

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots"
os.makedirs(OUT, exist_ok=True)


def shot(g, name, frames=2):
    for _ in range(frames):
        g.update(1 / 60)
        g.draw()
    pygame.image.save(g.screen, os.path.join(OUT, name + ".png"))
    print("  wrote %s.png" % name)


g = G.Game()
shot(g, "01_title", 8)

g.ui.slot_mode = "new"
g.go(G.SLOTS)
shot(g, "02_slots")

g.new_run("Savvydev", 1)
g.dialogue = None
g.state = G.OVERWORLD
g.world_time = 120.0          # high noon, so the art is judged unlit
g.player.gain_xp(2600)
g.player.gold = 1450
for _ in range(12):
    from pixel_rpg.items import roll_item
    g.player.add_item(roll_item(g.player.level + 2))
g.player.add_consumable("potion", 4)
g.player.add_consumable("hipotion", 2)
g.toast("Found Blazing Greatsword", (192, 118, 250), 9)
g.toast("Level 12 - 3 points to spend", (118, 220, 138), 9)
shot(g, "03_overworld", 30)

g.state = G.OVERWORLD
g.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_i, unicode="",
                                  mod=0))
shot(g, "04_bag", 3)

g.state = G.OVERWORLD
g.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_c, unicode="",
                                  mod=0))
shot(g, "05_hero", 3)

g.state = G.OVERWORLD
g.ui.refresh_shop(g.player, g.world)
g.go(G.SHOP)
shot(g, "06_shop", 3)

g.state = G.OVERWORLD
g.go(G.QUESTS)
shot(g, "07_quests", 3)

g.state = G.OVERWORLD
g.player.full_restore()
g.start_battle(make_enemy(9, "forest", g.settings.mods))
for _ in range(120):
    g.update(1 / 60)
    g.draw()
    if g.battle and g.battle.phase == "menu":
        break
shot(g, "08_battle", 2)

if g.battle:
    g.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_1,
                                      unicode="", mod=0))
    for _ in range(12):
        g.update(1 / 60)
        g.draw()
    shot(g, "09_battle_hit", 1)

g.state = G.OVERWORLD
g.battle = None
g.player.gain_xp(60000)
g.player.full_restore()
g.start_battle(Boss(BOSS_DEFS[3], g.settings.mods))
for _ in range(30):
    g.update(1 / 60)
    g.draw()
shot(g, "10_boss", 1)
for _ in range(90):
    g.update(1 / 60)
    g.draw()
    if g.battle and g.battle.phase == "menu":
        break
g.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_2, unicode="",
                                  mod=0))
shot(g, "11_boss_skills", 3)

pygame.quit()
print("done")
