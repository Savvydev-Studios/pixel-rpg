#!/usr/bin/env python3
"""One-command web build: the game as a page.

**This is a PC build.** Mobile was dropped on 2026-08-23 and the browser
build was kept and pointed at desktops instead - handoff section 37. It
is a live target: keyboard, mouse, and a window somebody drags about.

    python tools/build_web.py             # build into build/web
    python tools/build_web.py --serve     # ...then serve it on :8000
    python tools/build_web.py --autorun   # ...without the tap-to-start gate

Roadmap item 4 (handoff section 19). The transport, storage, frame loop,
touch controls and responsive layout all landed before this; pygbag is the
part that turns them into something a browser can open.

**This is the sibling of `build_exe.py`, and it is deliberately shaped like
it:** build, then refuse to report success until the artefact has been
checked. A web build fails the same way an exe build failed once - it
produces a file, the file looks right, and a module is missing inside it.
`_verify` opens the apk and looks.

pygbag is a build-time dependency and is not in `requirements.txt` for the
same reason PyInstaller is not: nobody playing the game needs it.

    python -m pip install pygbag
"""
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build", "web")
# pygbag names the apk after the *folder* it builds, whatever `--app_name`
# says (0.9.3): `pixel_rpg.apk` on one PC, `pixel-rpg.apk` in a checkout
# called pixel-rpg - which is every GitHub runner (s96). So it is found,
# not assumed; the page refers to the same name.
def _apk():
    found = sorted(name for name in os.listdir(BUILD)
                   if name.endswith(".apk")) if os.path.isdir(BUILD) else []
    return os.path.join(BUILD, found[0]) if len(found) == 1 else None

# Where the runtime lives, and the pieces of it the *page* asks its own
# origin for rather than the CDN.
CDN = "https://pygame-web.github.io"
MIRROR = ("/cdn/cp312/pygame_ce-2.5.7-cp312-cp312-wasm32_bi_emscripten.whl",)

# Everything under `pixel_rpg/` has to be inside the apk. A web build has no
# import fallback: a module that did not get packed is an ImportError on a
# stranger's phone, which is the web version of the missing-gamepad build.
PACKAGE = "pixel_rpg"


def mirror_runtime():
    """Pull the same-origin runtime pieces into the build, once.

    **This is what makes `build/web` uploadable.** The page asks its own
    origin for parts of the interpreter - pygame itself, most of all - and
    `serve_web.py` fetches those on demand. A static host does no such thing:
    it serves the folder and nothing else, so anything missing is a 404 and a
    grey canvas with nothing in the browser console to explain it.

    **The list is empirical and cannot be derived.** pygbag resolves the
    wheel at *runtime*, against a package index on the CDN, so nothing at
    build time knows its name. It was read out of `serve_web.py`'s log, which
    prints every path it had to go and fetch. If a future pygbag or pygame-ce
    changes the name, the symptom is a grey page on a host and a `PULLED`
    line locally - update `MIRROR` from that line.

    Failures are warnings, not errors: a build that cannot reach the CDN is
    still fine to serve locally, where the lazy fetch covers it.
    """
    for path in MIRROR:
        target = os.path.join(BUILD, *path.strip("/").split("/"))
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            with urllib.request.urlopen(CDN + path, timeout=60) as response:
                body = response.read()
        except Exception as exc:                          # noqa: BLE001
            print("  ! could not mirror %s (%s)" % (path, exc))
            print("    the build still works locally; it is not uploadable")
            continue
        if not body:
            print("  ! %s came back empty" % path)
            continue
        with open(target, "wb") as handle:
            handle.write(body)
        print("  mirrored %s (%d KB)" % (path, len(body) // 1024))


def uploadable():
    """True when every mirrored piece is present and non-empty."""
    return all(
        os.path.isfile(os.path.join(BUILD, *path.strip("/").split("/")))
        and os.path.getsize(os.path.join(BUILD, *path.strip("/").split("/")))
        for path in MIRROR)


def _have_pygbag():
    try:
        import pygbag                                  # noqa: F401
        return True
    except ImportError:
        return False


def _expected_modules():
    folder = os.path.join(ROOT, PACKAGE)
    return sorted(name for name in os.listdir(folder) if name.endswith(".py"))


def _verify():
    """Open the apk and check what is actually in it. Returns True if usable."""
    apk = _apk()
    if apk is None:
        print("pygbag reported success but did not produce exactly one apk")
        return False
    with zipfile.ZipFile(apk) as archive:
        names = set(archive.namelist())

    problems = []
    if "assets/main.py" not in names:
        problems.append("main.py is missing - the page has no entry point")

    for module in _expected_modules():
        want = "assets/%s/%s" % (PACKAGE, module)
        if want not in names:
            problems.append("%s is missing from the apk" % want)

    # The other half, and it is an allow-list on purpose. A deny-list of
    # things not to ship was the first version and it was not good enough:
    # pygbag's own filter drops dot-*files* but not dot-*folders* other
    # than the handful it hardcodes, because it tests paths that already
    # start with a slash. A stray `.claude/launch.json` shipped in a build
    # made during this pass and nothing said a word.
    #
    # Nothing there was sensitive. The next thing to land in a folder like
    # that might be, and a web build is published by definition - so this
    # names what belongs and refuses the rest, rather than trying to guess
    # what does not.
    for name in sorted(names):
        if name in ("assets/main.py", "assets/pygbag.ini"):
            continue
        if name.startswith("assets/%s/" % PACKAGE) and name.endswith(".py"):
            continue
        problems.append(
            "%s is in the apk and nothing put it there on purpose" % name)

    for problem in problems:
        print("  ! %s" % problem)
    if problems:
        return False

    print("  apk holds %d files, %d of them %s modules"
          % (len(names), len(_expected_modules()), PACKAGE))
    return True


def main(argv):
    os.chdir(ROOT)              # pygbag reads pygbag.ini from the cwd
    if not _have_pygbag():
        print("pygbag is not installed. This is a build-time dependency:")
        print("    python -m pip install pygbag")
        return 1

    autorun = "--autorun" in argv
    serve = "--serve" in argv

    print("[1/3] drawing the favicon ...")
    icon = os.path.join("build", "favicon.png")
    result = subprocess.run([sys.executable,
                             os.path.join("tools", "make_icon.py"), icon])
    if result.returncode != 0:
        print("could not draw the icon")
        return result.returncode

    # pygbag writes into this folder but does not create it, so a first
    # build on a clean machine dies with a FileNotFoundError about its own
    # output. Cheap to prevent, confusing to hit.
    os.makedirs(BUILD, exist_ok=True)
    # A package left by an earlier build under another name (see `_apk`)
    # would make two, and which one the page loads would be a guess.
    for name in os.listdir(BUILD):
        if name.endswith(".apk"):
            os.remove(os.path.join(BUILD, name))

    print("[2/3] running pygbag ...")
    command = [
        sys.executable, "-m", "pygbag", "--build",
        "--app_name", "pixel_rpg",
        "--title", "Pixel RPG",
        "--icon", icon,
        # The window the page opens at. `Game` is responsive either way
        # (section 29), so this is a starting size and not a constraint.
        "--width", "1280", "--height", "720",
        # A browser will not start audio before a user gesture, so pygbag
        # holds the whole app behind one. That is the right default for a
        # player - it is how the sound gets to work at all - and the wrong
        # one for an automated check, which has no finger.
        "--ume_block", "0" if autorun else "1",
        ".",
    ]
    result = subprocess.run(command)
    if result.returncode != 0:
        print("pygbag failed")
        return result.returncode

    print("")
    print("[3/3] checking what actually got packed ...")
    if not _verify():
        print("")
        print("BUILD REJECTED: the apk is not usable.")
        return 1

    print("")
    print("mirroring the runtime so the folder can be uploaded ...")
    mirror_runtime()

    size = sum(os.path.getsize(os.path.join(root, name))
               for root, _dirs, names in os.walk(BUILD) for name in names)
    print("")
    print("built %s (%.1f MB)" % (BUILD, size / 1048576.0))
    if uploadable():
        print("self-contained: upload this folder to any static host")
    else:
        print("NOT self-contained - fine locally, will grey out on a host")
    if autorun:
        print("NOTE: built with --autorun, so there is no tap-to-start and")
        print("      the browser will refuse it audio. Checks only.")
    if serve:
        print("")
        return subprocess.call([sys.executable,
                                os.path.join("tools", "serve_web.py")])
    print("serve it with: python tools/serve_web.py")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
