"""Check that HANDOFF.md still describes the game that is actually here.

The handoff is the only thing carrying context between sessions, and the
standing rule (section 31) is to keep it current as the work happens. A rule
nothing enforces is a rule that decays, so this enforces the checkable half:
the counts in section 4, the test totals in sections 0 and 6, and whether
every module and tool on disk is in the map.

    python tools/check_handoff.py

It cannot check the part that matters most - whether the *reasoning* was
written down - so a clean run here is necessary and not sufficient.

Exit code 1 if anything drifted, so it can join the gate.
"""
import os
import re
import subprocess
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PROBLEMS = []


def check(label, claimed, actual):
    if claimed is None:
        PROBLEMS.append("%s: not stated in HANDOFF.md" % label)
    elif int(claimed) != int(actual):
        PROBLEMS.append("%s: handoff says %s, code says %s"
                        % (label, claimed, actual))
    else:
        print("  ok   %-26s %s" % (label, actual))


def first_int(text):
    m = re.search(r"\d+", text or "")
    return m.group(0) if m else None


def main():
    handoff = open(os.path.join(ROOT, "HANDOFF.md"), encoding="utf-8").read()

    # -- section 4, the content table --------------------------------------
    table = {}
    for row in re.findall(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$", handoff,
                          re.M):
        table[row[0].strip().lower()] = row[1].strip()

    from pixel_rpg import (audio, classes, guide, items, keybinds, modifiers,
                           relics, scenes, skills, spritedata, story,
                           textures, entities)

    print("HANDOFF.md section 4 - current content")
    check("classes", first_int(table.get("classes")), len(classes.CLASSES))
    check("skills", first_int(table.get("skills")), len(skills.SKILLS_BY_KEY))
    check("status effects", first_int(table.get("status effects")),
          len(skills.STATUSES))
    check("consumables", first_int(table.get("consumables")),
          len(items.CONSUMABLES))
    check("relics", first_int(table.get("relics")), len(relics.ALL_RELICS))
    check("run modifiers", first_int(table.get("run modifiers")),
          len(modifiers.MODIFIERS))
    check("bosses", first_int(table.get("bosses")), len(entities.BOSS_DEFS))
    check("endings", first_int(table.get("endings")), len(story.ENDINGS))
    check("ground materials", first_int(table.get("ground materials")),
          len(textures.MATERIALS))
    check("sound effects", first_int(table.get("sound effects")),
          len(audio.RECIPES))
    check("guide pages", first_int(table.get("guide pages")),
          len(guide.PAGES))
    # **The version, and it is a string rather than a count** (2026-09-05,
    # rule 31.1e). Savvydev: *"we've updated the game a copious amount of
    # times and yet we're still in version 3"*. It had not moved through
    # eight feature passes because nothing checked it and it was typed in
    # two places. Now the document holds it and this fails when the code
    # disagrees, exactly like every other row above.
    import pixel_rpg
    claimed = (table.get("version") or "").strip()
    if not claimed:
        PROBLEMS.append("version: not stated in HANDOFF.md")
    elif claimed != pixel_rpg.__version__:
        PROBLEMS.append("version: handoff says %s, code says %s"
                        % (claimed, pixel_rpg.__version__))
    else:
        print("  ok   %-26s %s" % ("version", claimed))
    # Stated as the *visible* count: the debug binding is secret until the
    # menu is found, and naming it in the table would give it away.
    check("bindable actions", first_int(table.get("bindable actions")),
          len(keybinds.visible_actions(False)))
    # Likewise the codex: the table says "16 (+1 ...)" and 16 is the visible
    # total, because announcing 17 announces the hidden ending.
    check("codex entries", first_int(table.get("codex entries")),
          len(story.visible_codex(set())))
    check("story scenes", first_int(table.get("story scenes")),
          len(scenes.SCENES))
    check("sprites", first_int(table.get("sprites")),
          len([k for k in dir(spritedata)
               if k.isupper() and k != "GRID"
               and isinstance(getattr(spritedata, k), (list, tuple))]))

    # -- the suite totals quoted in sections 0 and 6 ------------------------
    print("\nTest totals quoted in sections 0 and 6")
    for label, cmd, pattern in (
        ("unit tests", [sys.executable, "tests/test_all.py"],
         r"Ran (\d+) tests"),
        ("integration checks", [sys.executable, "tests/smoke.py"],
         r"All (\d+) smoke checks"),
    ):
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        found = re.search(pattern, out.stdout + out.stderr)
        if not found:
            PROBLEMS.append("%s: could not run the suite to find out" % label)
            continue
        claimed = re.search(r"(\d+)\s+%s" % label.replace(" ", r"\s+"),
                            handoff)
        check(label, claimed.group(1) if claimed else None, found.group(1))

    # -- every module and tool is in the map --------------------------------
    print("\nModule map")
    missing = []
    for name in sorted(os.listdir(os.path.join(ROOT, "pixel_rpg"))):
        if not name.endswith(".py") or name == "__init__.py":
            continue
        if re.search(r"^\s+%s\s" % re.escape(name), handoff, re.M) is None:
            missing.append("pixel_rpg/" + name)
    for name in sorted(os.listdir(os.path.join(ROOT, "tools"))):
        if not name.endswith(".py"):
            continue
        if name.split(".")[0] not in handoff:
            missing.append("tools/" + name)
    if missing:
        PROBLEMS.extend("not in the module map: %s" % m for m in missing)
    else:
        print("  ok   every module and tool is listed")

    print("\n" + "-" * 62)
    if PROBLEMS:
        print("HANDOFF.md has drifted:")
        for problem in PROBLEMS:
            print("  - %s" % problem)
        return 1
    print("HANDOFF.md matches the code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
