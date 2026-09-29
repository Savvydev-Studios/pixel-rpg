"""Verify a packaged build actually works.

The naive check - launch it and see whether a process is still alive - is
worthless for a --windowed PyInstaller build: a startup failure puts up a
modal error dialog and the process stays alive indefinitely, which looks
exactly like a healthy game.

So this drives the executable's own `--selftest`, which boots the game,
renders the title, starts a run, fights a battle, saves and loads, then exits
with a status code. A build that cannot import something fails to reach the
end of that and is caught. A build that puts up a modal dialog never exits at
all, and the timeout catches that.
"""
import os
import subprocess
import sys
import tempfile

EXE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "Pixel_RPG.exe")
TIMEOUT = 90

if not os.path.isfile(EXE):
    print("no such executable: %s" % EXE)
    sys.exit(2)

report_dir = tempfile.mkdtemp(prefix="rpgverify")
report = os.path.join(report_dir, "selftest.txt")

env = dict(os.environ)
env["SDL_VIDEODRIVER"] = "dummy"
env["SDL_AUDIODRIVER"] = "dummy"
env["PIXEL_RPG_SAVE_DIR"] = os.path.join(report_dir, "saves")

print("running self-test in %s ..." % os.path.basename(EXE))
try:
    result = subprocess.run([EXE, "--selftest", report], env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=TIMEOUT)
    code = result.returncode
except subprocess.TimeoutExpired:
    print("FAILED: the executable never exited within %ds." % TIMEOUT)
    print("        That usually means a startup error dialog is waiting for")
    print("        someone to click Close - i.e. a missing module.")
    subprocess.run(["taskkill", "/IM", os.path.basename(EXE), "/F"],
                   capture_output=True)
    sys.exit(1)

lines = []
if os.path.isfile(report):
    with open(report, "r", encoding="utf-8", errors="replace") as handle:
        lines = handle.read().strip().splitlines()

for line in lines:
    print("   ", line)

if code != 0 or not lines or lines[-1] != "OK":
    print("FAILED: exit code %d" % code)
    if not lines:
        print("        the build produced no report at all, so it died before")
        print("        it could run any of the game code")
    sys.exit(1)

print("PASSED: the packaged build boots, plays, saves and loads.")
sys.exit(0)
