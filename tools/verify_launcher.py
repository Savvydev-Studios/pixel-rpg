"""Prove run.bat is location-independent.

Invokes the batch file by absolute path while the working directory is
somewhere else entirely. If run.bat resolved anything relative to the caller's
cwd instead of its own folder (%~dp0), this fails.
"""
import os
import subprocess
import sys
import time

folder = os.path.abspath(sys.argv[1])
bat = os.path.join(folder, "run.bat")
if not os.path.isfile(bat):
    print("no run.bat at", bat)
    sys.exit(2)

env = dict(os.environ)
env["SDL_VIDEODRIVER"] = "dummy"
env["SDL_AUDIODRIVER"] = "dummy"
env["PIXEL_RPG_SAVE_DIR"] = os.path.join(folder, "saves")

elsewhere = os.path.expanduser("~")
print("running: %s" % bat)
print("but from cwd: %s" % elsewhere)

# The batch uses `start`, which detaches the game. Piping its output would
# keep this process waiting on a handle the detached child still holds, so
# the streams go to DEVNULL and success is judged by the process listing.
result = subprocess.run(["cmd.exe", "/c", bat], cwd=elsewhere, env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=60)
print("batch exit code:", result.returncode)

time.sleep(7)
listing = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Pixel_RPG.exe",
                          "/NH"], capture_output=True)
running = b"Pixel_RPG.exe" in listing.stdout
print("Pixel_RPG.exe running:", running)
if running:
    subprocess.run(["taskkill", "/IM", "Pixel_RPG.exe", "/F"],
                   capture_output=True)
    print("stopped it again")
sys.exit(0 if (result.returncode == 0 and running) else 1)
