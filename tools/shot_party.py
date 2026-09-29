"""Screenshot a shared battle. A clean layout run is not proof it looks right."""
import os, sys, tempfile
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgparty")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pygame
from pixel_rpg import game as G, battlescreen
from pixel_rpg.entities import Player, make_enemy

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_party"
os.makedirs(OUT, exist_ok=True)
g = G.Game()
g.new_run("Ysoldebane", "warden")
for _ in range(12):
    g.player.gain_xp(g.player.xp_to_next())


def party_of(n):
    members = [g.player]
    for name, key in list(zip(("Kessa", "Dorn", "Vell"),
                              ("emberwright", "bloodhowl", "oathbound")))[:n - 1]:
        friend = Player(name, key)
        for _ in range(12):
            friend.gain_xp(friend.xp_to_next())
        members.append(friend)
    return members


def shot(name, players, monsters, acting=0, hurt=True):
    party = party_of(players)
    pack = [make_enemy(12, "forest", g.settings.mods) for _ in range(monsters)]
    for foe in pack:
        foe.hp = int(foe.max_hp() * 0.8)
    for member in party:
        member.hp = int(member.max_hp() * 0.9)
    if hurt and len(party) > 1:
        party[1].hp = max(1, party[1].max_hp() // 4)
        if len(party) > 3:
            party[3].hp = 0
    g.start_battle(pack, party=party, viewer=0, sort_party=False)
    for _ in range(900):
        g.battle.update(1 / 60)
        if g.battle.phase == "menu":
            break
    g.battle.acting_player = acting
    g.battle.say("Kessa's attack hits Ancient Wolf for 42.")
    g.draw()
    pygame.image.save(g.screen, os.path.join(OUT, name + ".png"))
    print("  wrote", name)
    g.battle = None


shot("01_solo_one", 1, 1)
shot("02_two_v_three", 2, 3, acting=1)
shot("03_four_v_five", 4, 5, acting=2)
print("done ->", OUT)
