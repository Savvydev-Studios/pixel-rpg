#!/usr/bin/env python3
"""Pack the Linux build as a .deb - the way a Chromebook installs an app.

    python tools/build_exe.py      # first: makes ./Pixel_RPG on Linux
    python tools/build_deb.py      # then:  makes pixel-rpg_<version>_<arch>.deb

**Why a .deb and not just the file** (s96). Savvydev: *"you should be able to
run the game without needing to install python and pygame and stuff"*. The
one-file build already carries Python, pygame and SDL inside it - but a bare
Linux executable still has to be started from a Terminal, and on a
Chromebook that is where most people stop. ChromeOS installs a .deb from
the Files app with a double-click (*Install with Linux*) and puts it in the
app launcher like anything else, so this is the no-Terminal way in.

It needs Linux turned on - a Chromebook with Linux locked by a school runs
no downloaded program at all, and for that one the browser build is the
only way (tools/build_web.py).

What goes in:

    /opt/pixel-rpg/Pixel_RPG                       the game, one file
    /usr/games/pixel-rpg                           a link to it
    /usr/share/applications/pixel-rpg.desktop     the launcher entry
    /usr/share/icons/hicolor/<n>x<n>/apps/pixel-rpg.png

Saves are not in any of it: they live in ~/.local/share/Pixel_RPG, so
installing a newer .deb over an older one keeps every save.
"""
import os
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ICON_SIZES = (32, 48, 64, 128, 256)
ARCHES = {"x86_64": "amd64", "amd64": "amd64",
          "aarch64": "arm64", "arm64": "arm64"}

DESKTOP = """[Desktop Entry]
Type=Application
Name=Pixel RPG
Comment=Six roads, six wardens, and the Door
Exec=/opt/pixel-rpg/Pixel_RPG
Icon=pixel-rpg
Terminal=false
Categories=Game;RolePlaying;
"""

CONTROL = """Package: pixel-rpg
Version: {version}
Architecture: {arch}
Maintainer: Savvydev-Studios
Section: games
Priority: optional
Installed-Size: {size_kb}
Description: Pixel RPG
 A pixel-art role-playing game. Python, pygame and SDL are built in:
 nothing else needs installing.
"""


def main():
    from pixel_rpg import __version__
    binary = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                             else os.path.join(ROOT, "Pixel_RPG"))
    if not os.path.isfile(binary):
        print("no Linux build at %s - run tools/build_exe.py first" % binary)
        return 2
    arch = ARCHES.get(platform.machine().lower())
    if arch is None:
        print("unknown machine %r" % platform.machine())
        return 2
    if shutil.which("dpkg-deb") is None:
        print("dpkg-deb is missing (it comes with Debian and Ubuntu)")
        return 2

    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import make_icon

    stage = tempfile.mkdtemp(prefix="pixelrpg-deb")
    # mkdtemp is 0700, and the package's root is recorded as it stands.
    os.chmod(stage, 0o755)
    try:
        game_dir = os.path.join(stage, "opt", "pixel-rpg")
        os.makedirs(game_dir)
        shutil.copy2(binary, os.path.join(game_dir, "Pixel_RPG"))
        os.chmod(os.path.join(game_dir, "Pixel_RPG"), 0o755)

        games = os.path.join(stage, "usr", "games")
        os.makedirs(games)
        os.symlink("/opt/pixel-rpg/Pixel_RPG",
                   os.path.join(games, "pixel-rpg"))

        apps = os.path.join(stage, "usr", "share", "applications")
        os.makedirs(apps)
        with open(os.path.join(apps, "pixel-rpg.desktop"), "w") as f:
            f.write(DESKTOP)

        for size in ICON_SIZES:
            folder = os.path.join(stage, "usr", "share", "icons", "hicolor",
                                  "%dx%d" % (size, size), "apps")
            os.makedirs(folder)
            make_icon.build_png(os.path.join(folder, "pixel-rpg.png"), size)

        size_kb = sum(os.path.getsize(os.path.join(d, n))
                      for d, _, names in os.walk(stage) for n in names
                      if not os.path.islink(os.path.join(d, n))) // 1024
        control = os.path.join(stage, "DEBIAN")
        os.makedirs(control)
        with open(os.path.join(control, "control"), "w") as f:
            f.write(CONTROL.format(version=__version__, arch=arch,
                                   size_kb=size_kb))
        for d, dirs, _ in os.walk(stage):
            for name in dirs:
                os.chmod(os.path.join(d, name), 0o755)

        out = os.path.join(ROOT, "pixel-rpg_%s_%s.deb" % (__version__, arch))
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", stage,
                        out], check=True, stdout=subprocess.DEVNULL)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    print("built %s (%.1f MB)" % (out, os.path.getsize(out) / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())
