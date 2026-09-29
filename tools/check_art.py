"""Validate every sprite grid: square, uniform width, known palette chars."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pixel_rpg import spritedata as sd

problems = []
sizes = {}
groups = [("MONSTERS", sd.MONSTERS), ("BOSSES", sd.BOSSES),
          ("PROPS", sd.PROPS), ("HERO", sd.HERO)]
if hasattr(sd, "MONSTER_ALT"):
    groups.append(("MONSTER_ALT", sd.MONSTER_ALT))
if hasattr(sd, "HERO_WALK"):
    groups.append(("HERO_WALK", {k: v for k, vs in sd.HERO_WALK.items()
                                 for k, v in [(k, vs[0])]}))

for label, group in groups:
    for name, rows in group.items():
        widths = {len(r) for r in rows}
        height = len(rows)
        if len(widths) != 1:
            problems.append("%s/%s: ragged widths %s" % (label, name,
                                                         sorted(widths)))
            continue
        width = widths.pop()
        if width != height:
            problems.append("%s/%s: %dx%d is not square" % (label, name,
                                                            width, height))
        sizes.setdefault((width, height), []).append("%s/%s" % (label, name))
        unknown = {c for r in rows for c in r} - set(sd.PALETTE) - {".", " "}
        if unknown:
            problems.append("%s/%s: unknown palette chars %s"
                            % (label, name, sorted(unknown)))

print("sprite sizes in use:")
for size, names in sorted(sizes.items()):
    print("  %dx%-3d  %2d sprites   %s" % (size[0], size[1], len(names),
                                           ", ".join(sorted(names)[:4])
                                           + ("..." if len(names) > 4 else "")))
if len(sizes) > 1:
    problems.append("MIXED SIZES: the art style is not consistent")
print()
if problems:
    print("PROBLEMS:")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("all sprite grids are square, uniform and in palette.")
