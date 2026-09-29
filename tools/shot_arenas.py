"""One frame from each Last Stand arena, in a grid (item 0am, s72).

Savvydev asked for *"a LOT of different maps for the wave gamemode... so it
looks pretty cool"*, and "a lot" and "cool" are both things a test cannot
answer. `tests/layout.py` will say the panels do not collide in any of
them; only this says whether eighteen places actually look like eighteen
places.

    python tools/shot_arenas.py [output_dir]

What to look for: **adjacent tiles in the grid should not be the same
colour.** The arenas are ordered so the ground climbs the biome tiers, so
neighbours are related on purpose - but two that read as identical are
two arenas that did not need to both exist.
"""
import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgarena")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame                                                   # noqa: E402
from pixel_rpg import game as G, gamemodes                      # noqa: E402
from pixel_rpg.ui import draw_text                              # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "shots_arenas"
os.makedirs(OUT, exist_ok=True)

COLS = 3
SHOT = (426, 240)          # each tile, scaled down from the real frame

g = G.Game()
g.pending_class = "warden"
g.new_wave_run("Stander")

tiles = []
for index, _listed in enumerate(gamemodes.ARENAS):
    # The first wave of that arena's block, so `arena_for` picks it rather
    # than being told - the tool exercises the mapping as well as the look.
    wave = index * gamemodes.WAVES_PER_ARENA + 1
    arena = gamemodes.arena_for(wave)
    g.waves.wave = wave
    g.battle = None
    g.floats.items.clear()
    g.particles.clear()
    if not g.begin_wave():
        print("  skipped %s: no wave" % arena.key)
        continue
    for _ in range(90):                       # run out the entry banner
        g.battle.update(1 / 60)
        if g.battle.phase == "menu":
            break
    g.draw()
    frame = pygame.transform.smoothscale(g.screen.copy(), SHOT)
    draw_text(frame, g.assets, "%d. %s" % (wave, arena.name), (10, 8))
    tiles.append(frame)
    print("  wave %-3d %s" % (wave, arena.name))

rows = (len(tiles) + COLS - 1) // COLS
sheet = pygame.Surface((SHOT[0] * COLS, SHOT[1] * rows))
for n, tile in enumerate(tiles):
    sheet.blit(tile, ((n % COLS) * SHOT[0], (n // COLS) * SHOT[1]))
pygame.image.save(sheet, os.path.join(OUT, "arenas.png"))
print("wrote arenas.png (%d places) ->" % len(tiles), OUT)
