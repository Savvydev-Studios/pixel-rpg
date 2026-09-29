"""One-command rebuild of Pixel_RPG.exe - or, on Linux, of Pixel_RPG.

**The same recipe on both** (s96): `tools/Pixel_RPG.spec`, which is in the
repository now. On Windows it makes the exe with its icon; on Linux it
makes one executable file with Python, pygame and SDL inside it, which
`tools/build_deb.py` then packs for a Chromebook.

    python tools/build_exe.py

Regenerates the icon, then runs PyInstaller against the hand-written spec.
Everything is resolved relative to the project root, so this works from any
checkout location - including paths containing an apostrophe, which is what
the hand-written spec exists to survive.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _locked(path):
    """True if the file exists but cannot be opened for writing."""
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "ab"):
            return False
    except OSError:
        return True


def main():
    os.chdir(ROOT)
    icon = os.path.join("build_assets", "Pixel_RPG.ico")

    print("[1/4] building the icon ...")
    subprocess.run([sys.executable, os.path.join("tools", "make_icon.py"),
                    icon], check=True)

    # If the game is open, Windows holds the exe and PyInstaller cannot
    # replace it. Build to a side name instead of failing, so someone can keep
    # playing while a new build is made.
    target = "Pixel_RPG"
    if _locked(os.path.join(ROOT, "Pixel_RPG.exe")):
        target = "Pixel_RPG.new"
        print("NOTE: Pixel_RPG.exe is running, so this build goes to")
        print("      Pixel_RPG.new.exe. Close the game and rename it over the")
        print("      old one, or re-run this script once the game is closed.")

    print("[2/4] running PyInstaller ...")
    env = dict(os.environ)
    env["PIXEL_RPG_EXE_NAME"] = target
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm",
         "--distpath", ".", "--workpath", os.path.join("build", "work"),
         os.path.join("tools", "Pixel_RPG.spec")], env=env)
    if result.returncode != 0:
        print("build failed")
        return result.returncode

    exe = os.path.join(ROOT, target + (".exe" if os.name == "nt" else ""))
    if not os.path.isfile(exe):
        print("PyInstaller reported success but no exe was produced")
        return 1
    print("built %s (%.1f MB)" % (exe, os.path.getsize(exe) / 1048576))

    # Verify before declaring success. A build that imports fine on this
    # machine can still be missing a module inside the bundle, and that
    # failure only appears as a dialog on launch - which is exactly how a
    # broken build shipped once already.
    checks = [(3, "verify_exe.py")]
    # Reading real window titles is a Win32 call (PowerShell); on Linux the
    # self-test inside the build is the check.
    if os.name == "nt":
        checks.append((4, "verify_window.py"))
    for step, script in checks:
        print("")
        print("[%d/4] %s ..." % (step, script))
        check = subprocess.run([sys.executable,
                                os.path.join("tools", script), exe])
        if check.returncode != 0:
            print("")
            print("BUILD REJECTED: %s failed; the exe is not usable."
                  % script)
            return check.returncode
    print("")
    print("%s is built and verified." % os.path.basename(exe))
    if target != "Pixel_RPG":
        print("Close the game, then run update.bat to put it in place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
