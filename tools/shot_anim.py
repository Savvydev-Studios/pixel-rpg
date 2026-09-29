"""Frame strips for the procedural animation, because a still proves nothing.

`tests/layout.py` checks text and panels; it has no opinion about whether a
body moves convincingly. This renders each move as a vertical strip of frames
so the next person can look at the timing rather than trust a docstring.

    python tools/shot_anim.py [output_dir]

The one thing worth checking every time: on the **impact** frame of a lunge
the attacker is at full extension *and* the target is flashing. If those two
drift apart, the damage number lands before the blow does - see the comment
over `anim.SPANS`.
"""
import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpganim")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame                                                   # noqa: E402
from pixel_rpg import game as G                                 # noqa: E402
from pixel_rpg.entities import make_enemy                       # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_anim"
os.makedirs(OUT, exist_ok=True)
CROP = pygame.Rect(230, 240, 780, 280)

g = G.Game()
g.new_run("Ysoldebane", "warden")
for _ in range(12):
    g.player.gain_xp(g.player.xp_to_next())


def fight(monsters=1, level=10):
    foes = [make_enemy(level, "forest", g.settings.mods)
            for _ in range(monsters)]
    for foe in foes:
        foe.hp = foe.max_hp()
    g.player.hp = g.player.max_hp()
    g.start_battle(foes)
    for _ in range(900):                     # run out the entry banner
        g.battle.update(1 / 60)
        if g.battle.phase == "menu":
            break
    g.floats.items.clear()
    g.particles.clear()
    return foes


def strip(name, frames, picks):
    chosen = [frames[i] for i in picks if i < len(frames)]
    sheet = pygame.Surface((CROP.w, CROP.h * len(chosen)))
    for n, frame in enumerate(chosen):
        sheet.blit(frame, (0, n * CROP.h))
    pygame.image.save(sheet, os.path.join(OUT, name + ".png"))
    print("  wrote %s  (%d frames)" % (name, len(chosen)))


def record(count):
    out = []
    for _ in range(count):
        g.battle.update(1 / 60)
        g.floats.update(1 / 60)
        g.particles.update(1 / 60)
        g.draw()
        out.append(g.screen.subsurface(CROP).copy())
    return out


# 1. A blow: wind-up, strike, impact, recovery - and the target's recoil.
fight()
g.battle.player_attack()
strip("01_lunge_and_recoil", record(40), (0, 4, 8, 12, 16, 22, 30))

# 2. A spell: lift and hold rather than travel.
foes = fight()
skills = g.player.skills()
if skills:
    g.battle.phase = "submenu"
    g.player.mp = g.player.max_mp()
    g.battle.use_skill(skills[0])
    strip("02_cast", record(46), (0, 5, 10, 15, 20, 28, 38))

# 3. A death: slump, flatten, fade - and the body stays put once it lands.
foes = fight()
foes[0].hp = 1
g.battle.player_attack()
strip("03_fall", record(70), (0, 10, 16, 22, 30, 42, 60))


# ---------------------------------------------------------------------------
# s71, item 0al(b). Four motions covered the whole game; there are fifteen.
# Everything below is a move that did not exist, and the point of looking at
# a strip rather than a docstring is that a curve can be arithmetically
# correct and still read as a slide.
# ---------------------------------------------------------------------------

# 4. A heal: it rises, and it must **not** step at the enemy. `cast` put
#    `REACH * 0.10` of forward travel into every ability that played it.
foes = fight()
g.player.hp = max(1, g.player.max_hp() // 3)
# A Warden knows no heal, so take one out of the table rather than
# reaching for a second class - the move is the subject, not the caster.
from pixel_rpg.skills import SKILLS_BY_KEY                       # noqa: E402
healers = [sk for sk in SKILLS_BY_KEY.values() if sk.kind == "heal"]
if healers:
    g.battle.phase = "submenu"
    g.player.mp = g.player.max_mp()
    g.battle.use_skill(sorted(healers, key=lambda sk: sk.mp)[0])
    strip("04_bless", record(48), (0, 6, 12, 18, 24, 32, 44))

# 5. A big spell: it crouches before it rises, which is the whole
#    difference between `channel` and `cast`.
foes = fight()
big = sorted((s for s in g.player.skills() if s.kind == "attack"),
             key=lambda s: -s.power)
if big:
    g.battle.phase = "submenu"
    g.player.mp = g.player.max_mp()
    g.battle.use_skill(big[0])
    strip("05_channel_or_swing", record(62), (0, 8, 16, 24, 32, 44, 58))

# 6. Defending, which until now was the one action in the game with no
#    movement at all - the log said "braces" and the body did nothing.
fight()
g.battle.player_defend()
strip("06_brace", record(40), (0, 4, 9, 14, 20, 28, 36))

# 7. Being hit hard. A blow that takes a quarter of you staggers: the
#    settle is caught halfway by a second shove, so the body recovers,
#    loses it again, and only then straightens.
foes = fight(level=3)
g.player.hp = g.player.max_hp()
g.battle.play_motion("player", 0, "reel")
strip("07_reel", record(46), (0, 3, 6, 10, 16, 24, 40))

# 8. The three deaths, side by side. `fall` sinks where it stands;
#    `crumble` is still travelling backwards as it goes down; `wither`
#    fades before it falls and never gets knocked at all.
for name, move in (("08a_fall", "fall"), ("08b_crumble", "crumble"),
                   ("08c_wither", "wither")):
    foes = fight()
    foes[0].hp = foes[0].max_hp()
    g.battle.play_motion("enemy", 0, move)
    strip(name, record(64), (0, 6, 12, 20, 28, 40, 58))

print("done ->", OUT)
