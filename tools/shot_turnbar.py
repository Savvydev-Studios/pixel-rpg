"""Screenshot the turn bar and the greyed-out menu (s51, roadmap item 0h).

`tests/layout.py` says nothing overlaps. It cannot say whether you can
tell at a glance that the fight is waiting on somebody else, which is the
whole of what he asked for:

> "make it easier to know whether or not its your turn, add a turn bar
> and gray out all of your options when its not your turn"

    python tools/shot_turnbar.py [outdir]

Shoots your own turn, a friend's turn (the greyed menu), a full round of
four against five, and a round where the party goes second - then the
stacked layout, where the bar has a strip of its own under the log.
"""
import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgturn")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame

from pixel_rpg import game as G
from pixel_rpg.entities import Player, make_enemy

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_turnbar"
os.makedirs(OUT, exist_ok=True)

g = G.Game()
g.screen = pygame.display.set_mode((1280, 720))
g.new_run("Ysoldebane", "warden")
for _ in range(12):
    g.player.gain_xp(g.player.xp_to_next())


def party_of(n):
    members = [g.player]
    for name, key in list(zip(("Kessa", "Dorn", "Vell"),
                              ("emberwright", "bloodhowl",
                               "oathbound")))[:n - 1]:
        friend = Player(name, key)
        for _ in range(12):
            friend.gain_xp(friend.xp_to_next())
        members.append(friend)
    return members


def shot(name, players, monsters, acting=0, first=True, size=(1280, 720)):
    g.screen = pygame.display.set_mode(size)
    party = party_of(players)
    pack = [make_enemy(12, "forest", g.settings.mods) for _ in range(monsters)]
    for foe in pack:
        foe.hp = int(foe.max_hp() * 0.8)
    for member in party:
        member.hp = int(member.max_hp() * 0.9)
    g.start_battle(pack, party=party, viewer=0, sort_party=False)
    for _ in range(900):
        g.battle.update(1 / 60)
        if g.battle.phase == "menu":
            break
    g.battle.players_first = first
    g.battle.acting_player = acting
    g.battle.say("Kessa's attack hits Ancient Wolf for 42.")
    g.draw()
    pygame.image.save(g.screen, os.path.join(OUT, name + ".png"))
    print("  wrote", name)
    g.battle = None


shot("01_your_turn", 4, 5, acting=0)
shot("02_someone_elses_turn", 4, 5, acting=2)
shot("03_solo", 1, 3)
shot("04_monsters_go_first", 4, 5, acting=1, first=False)
shot("05_stacked", 4, 5, acting=2, size=(390, 844))
shot("06_narrow_wide", 4, 5, acting=2, size=(1024, 768))

print("done ->", OUT)
