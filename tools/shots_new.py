"""Render the screens added in this pass, for a visual once-over."""
import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgnew")
os.environ["PIXEL_RPG_SEED"] = "2024"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame

from pixel_rpg import game as G, menus, story
from pixel_rpg.items import roll_item

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_new"
os.makedirs(OUT, exist_ok=True)


def shot(g, name, frames=2):
    for _ in range(frames):
        g.update(1 / 60)
        g.draw()
    pygame.image.save(g.screen, os.path.join(OUT, name + ".png"))
    print("  wrote", name)


g = G.Game()
g.ui.slot_mode = "new"
g.go(G.CLASS_SELECT)
g.ui.class_index = 1
shot(g, "01_class_select", 4)
g.ui.class_index = 4                       # a locked class
shot(g, "02_class_locked", 3)

g.pending_class = "cutpurse"
g.go(G.MODIFIER_SELECT)
g.pending_modifiers.add("glass")
g.pending_modifiers.add("haunted")
g.ui.modifier_index = 2
shot(g, "03_modifiers", 3)

g.pending_class = "emberwright"
g.new_run("Savvydev", 1)
g.dialogue = None
g.state = G.OVERWORLD
g.world_time = 120.0
g.player.gain_xp(26000)
for _ in range(10):
    g.player.add_item(roll_item(g.player.level + 2))
for w in story.WARDENS[:3]:
    g.player.add_relic(w.memory_key)
    g.meta.unlock_codex(w.memory_key)
for k in ("valley", "anchor", "bram", "maren"):
    g.meta.unlock_codex(k)
shot(g, "04_overworld_day", 20)

g.go(G.CODEX)
shot(g, "05_codex", 3)
g.reading = story.CODEX_BY_KEY["memory_callun"]
shot(g, "06_codex_page", 3)
g.reading = None

g.state = G.OVERWORLD
g.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_c, unicode="",
                                  mod=0))
shot(g, "07_hero_sheet", 3)

g.state = G.OVERWORLD
g.dialogue = {"title": story.WARDENS[1].true_name,
              "body": list(story.WARDENS[1].intro) + [
                  "", "Recommended level 7.  You are level 14."],
              "confirm": "Draw your weapon", "on_confirm": None}
g.state = G.DIALOGUE
shot(g, "08_warden_dialogue", 3)
g.dialogue = None

g.state = G.OVERWORLD
for p in g.world.portals:
    p.defeated = True
for entry in story.CODEX:
    g.meta.unlock_codex(entry.key)
g.open_ending_choice()
shot(g, "09_ending_choice", 3)
g.ending = story.ENDINGS_BY_KEY["debt"]
g.last_score = 184200
g.state = G.ENDING_TEXT
shot(g, "10_ending_text", 3)
g.ending = None

g.state = G.RECORDS
shot(g, "11_records", 3)

pygame.quit()
print("done")
