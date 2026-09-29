"""Screenshot the trade table (s50, roadmap item 0x).

`tests/layout.py` says nothing overlaps at six sizes. It cannot say
whether a player can tell at a glance what they are handing over, which
half of the table is theirs, or whether "AGREED" is legible - so this gets
looked at by eye, per standing rule 31.4.

    python tools/shot_trade.py [outdir]

Shoots an empty table, a loaded one, one side agreed, both agreed, and the
refusal a full bag produces - then the same screen on a phone both ways
up, where the two offer panels stop sitting side by side.
"""
import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgtrade")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame

from pixel_rpg import game as G, trade
from pixel_rpg.items import roll_item

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_trade"
os.makedirs(OUT, exist_ok=True)

g = G.Game()
g.screen = pygame.display.set_mode((1280, 720))
g.pending_class = "cutpurse"
g.new_run("Savvydev", 6)
g.dialogue = None
g.state = G.TRADE


def save(name):
    g.draw()
    pygame.image.save(g.screen, os.path.join(OUT, name + ".png"))
    print("  wrote", name)


def at(size):
    g.screen = pygame.display.set_mode(size)


g.player.gold = 4820
g.player.consumables = {"potion": 9, "ether": 4, "elixir": 2}
for level, rarity in ((28, "epic"), (24, "rare"), (31, "legendary"),
                      (19, "uncommon"), (36, "mythic")):
    g.player.inventory.append(roll_item(level, rarity=rarity))
g.player.equip_best()

g.trade = trade.TradeSession(2, "Wanderingsmith")
save("01_empty")

g.trade.mine = trade.Offer(1200, g.player.inventory[:2], {"potion": 4})
g.trade.theirs = trade.Offer(75, [roll_item(40, rarity="mythic")],
                             {"elixir": 1, "phoenix": 2})
g.trade.index = 3
save("02_loaded")

g.trade.theirs_locked = True
save("03_they_agreed")

g.trade.mine_locked = True
g.trade.index = len(g.trade_rows()) - 2
save("04_both_agreed")

g.trade.mine_locked = g.trade.theirs_locked = False
g.trade.note = "your bag has no room for that"
save("05_refused")

# The two sizes the responsive pass cares about (s29). Portrait is where
# the offer panels became a band and the AGREED mark merged into the
# title - see `_offer_panel`.
at((844, 390))
save("06_phone_landscape")
at((390, 844))
save("07_phone_portrait")

print("done ->", OUT)
