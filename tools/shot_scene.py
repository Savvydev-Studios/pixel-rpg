"""Screenshot the scene screen, including the resolution scenes (s42).

A clean `tests/layout.py` run is not proof a screen looks right (rule
31.4), and this screen has a second reason to be looked at: it **scrolls**
now, and the thing that made it necessary - prose being silently cut on a
short window - was invisible to the checker by construction.

    python tools/shot_scene.py [outdir]

Shoots each scene at a desktop size and at 844x390, the phone-in-landscape
size that found the bug, and the short one is shot twice: at the top of the
prose and scrolled to the end of it.
"""
import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgscene")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame

from pixel_rpg import game as G, menus, scenes as scenes_mod, story

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_scene"
os.makedirs(OUT, exist_ok=True)

g = G.Game()
g.new_run("Walker", "warden")
# Every shard held, so the fourth answer is on screen: it is the one thing
# about these scenes that a screenshot of a fresh profile cannot show.
g.meta.secrets = sorted(set(scenes_mod.HEARD) | {scenes_mod.ALL_HEARD})


def frame(key, name, size, phase="choosing", scroll=0, answer=None):
    g.screen = pygame.display.set_mode(size)
    scene = scenes_mod.SCENES_BY_KEY[key]
    options = scene.offered(g.meta.secrets)
    g.scene = {"scene": scene, "options": options, "index": 0,
               "phase": phase, "reply": (), "after": None, "scroll": scroll}
    if phase == "reply":
        g.scene["reply"] = scene.choice(answer).reply
    g.state = G.SCENE
    g.draw()
    pygame.image.save(g.screen, os.path.join(OUT, name + ".png"))
    print("  wrote", name)


for stop in story.WAYSTATIONS:
    frame(stop.resolution, "%s_1280" % stop.key, (1280, 720))

frame("res_relay", "relay_reply_1280", (1280, 720), "reply", answer="writing")
frame("res_cut", "cut_844_top", (844, 390))
frame("res_cut", "cut_844_end", (844, 390),
      scroll=99)   # clamped by the draw to the last page
frame("way_relay", "relay_fragment_1280", (1280, 720))

print("done ->", OUT)
