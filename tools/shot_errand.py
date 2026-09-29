"""Screenshot the people on the road: errands, favours and hamlets (s44).

`tests/layout.py` says nothing overlaps. It cannot say whether an errand
reads like a person asking or like a job board, and it could not see the
dialogue box quietly cutting its own text (s43, s44) - so these get looked
at by eye, per standing rule 31.4.

    python tools/shot_errand.py [outdir]

Shoots an errand ask, its chase line, its payout, a hamlet's own line and
a favour, at 1280x720 - and the same errand at 844x390, at the top of the
text and scrolled to the end of it.
"""
import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgerrand")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame

from pixel_rpg import game as G, story
from pixel_rpg.quests import ERRANDS_BY_KEY

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_errand"
os.makedirs(OUT, exist_ok=True)

g = G.Game()
g.screen = pygame.display.set_mode((1280, 720))
g.pending_class = "warden"
g.new_run("Walker", 6)
g.dialogue = None


def save(name):
    g.draw()
    pygame.image.save(g.screen, os.path.join(OUT, name + ".png"))
    print("  wrote", name)


def at(size):
    g.screen = pygame.display.set_mode(size)


spot, key = next(iter(g.world.errand_givers.items()))
npc = next(n for n in g.world.npcs if (n.gx, n.gy) == spot)
errand = ERRANDS_BY_KEY[key]

g.offer_errand(npc)
save("01_ask")

g.take_errand(errand)
g.dialogue = None
g.offer_errand(npc)
save("02_chase")

g.player.stats_tracker[errand.metric] += errand.target * 3
g.dialogue = None
g.offer_errand(npc)
save("03_paid")

# A hamlet person, saying the thing they were given to say.
hamlet = next(n for n in g.world.npcs
              if n.kind == "villager" and " of " in n.name)
g.dialogue = None
g.scene = None
g.talk_to_villager(hamlet)
save("04_hamlet")

# A favour, on the scene screen, titled with somebody's name.
spot = next(s for s, entry in g.world.favours.items() if not entry[1])
asker = next(n for n in g.world.npcs if (n.gx, n.gy) == spot)
g.world.flag("villager_%s" % asker.name)     # past their own line
g.dialogue = None
g.play_favour(asker)
save("05_favour")

# The same errand on the window that made the box scroll.
at((844, 390))
g.scene = None
g.quests.errands = []
g.quests.completed = [k for k in g.quests.completed if k != key]
g.dialogue = None
g.offer_errand(npc)
save("06_ask_844_top")
g.dialogue["scroll"] = 99
save("07_ask_844_end")

print("done ->", OUT)
