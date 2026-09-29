# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the one-file game: Windows and Linux alike.

    python tools/build_exe.py           # the supported way in

**It lives in `tools/` now, where git can see it** (s96). It used to be
`build/Pixel_RPG.spec`, and `build/` is ignored - so the recipe for the
exe existed on exactly one PC, and no other machine, person or CI runner
could build the game at all.

Paths come from `SPECPATH`, never from the working directory, so a
checkout anywhere - including a folder with an apostrophe in its name,
which is what a hand-written spec first existed to survive - builds the
same thing.

**`hiddenimports` is not optional.** PyInstaller cannot see a module that
is imported lazily inside a function, and the game has two:
`pygame._sdl2.controller` (the gamepad) and `pygame._sdl2.video` (the
minimum window size). Leave either out and the build succeeds, the game
runs, and a feature quietly reports itself missing (handoff s7). Any new
`pygame._sdl2` import needs a line here *and* an import inside
`main.py --selftest`, which is what catches it inside the build.
"""
import os
import sys

ROOT = os.path.dirname(SPECPATH)            # tools/ -> the project
NAME = os.environ.get("PIXEL_RPG_EXE_NAME", "Pixel_RPG")
ICON = os.path.join(ROOT, "build_assets", "Pixel_RPG.ico")

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=[
        "pygame._sdl2",
        "pygame._sdl2.controller",
        "pygame._sdl2.video",
    ],
    hookspath=[],
    runtime_hooks=[],
    # The game never draws with Tk, and it is megabytes of dead weight.
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    debug=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # Windowed: no console window behind the game. A startup failure is a
    # dialog, which is why tools/verify_exe.py drives --selftest instead
    # of asking whether a process is alive (handoff s7).
    console=False,
    # The .ico is a Windows resource. Linux takes its icon from the
    # launcher entry instead (tools/build_deb.py).
    icon=ICON if os.name == "nt" else None,
)
