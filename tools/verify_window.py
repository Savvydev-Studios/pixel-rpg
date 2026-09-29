"""Launch the build for real and inspect the window it puts on screen.

The self-test runs headless, which proves the code works but not that the
packaged SDL video path does. This launches the executable normally and reads
the actual top-level window titles, so a PyInstaller error dialog is detected
as what it is rather than counted as "the game is running".
"""
import os
import subprocess
import sys
import time

EXE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "Pixel_RPG.exe")
NAME = os.path.basename(EXE)

PS = r'''
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class W {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
}
"@
$targets = @(%s)
$found = @()
$cb = [W+EnumProc]{
  param($h, $p)
  $pid2 = 0
  [W]::GetWindowThreadProcessId($h, [ref]$pid2) | Out-Null
  if ($targets -contains $pid2 -and [W]::IsWindowVisible($h)) {
    $sb = New-Object System.Text.StringBuilder 512
    [W]::GetWindowText($h, $sb, 512) | Out-Null
    $t = $sb.ToString()
    if ($t.Length -gt 0) { $script:found += $t }
  }
  return $true
}
[W]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
$found | ForEach-Object { Write-Output $_ }
'''

if not os.path.isfile(EXE):
    print("no such executable:", EXE)
    sys.exit(2)

env = dict(os.environ)
env.pop("SDL_VIDEODRIVER", None)
env.pop("SDL_AUDIODRIVER", None)

print("launching %s with a real window ..." % NAME)
proc = subprocess.Popen([EXE], env=env, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)

# Poll rather than sleeping a fixed amount: a cold start behind an
# on-access virus scanner can take far longer than a warm one, and a fixed
# wait turns that into a false failure.
DEADLINE = 45
titles = []
try:
    # A onefile build runs a bootloader that spawns a child; the window
    # belongs to the child, so every process with this image name counts.
    deadline = time.time() + DEADLINE
    while time.time() < deadline:
        time.sleep(2)
        listing = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process -Name '%s' -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty Id"
             % os.path.splitext(NAME)[0]],
            capture_output=True, timeout=30)
        pids = [p.strip() for p in
                listing.stdout.decode("utf-8", "replace").splitlines()
                if p.strip()]
        if not pids:
            break
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              PS % ",".join(pids)],
                             capture_output=True, timeout=45)
        titles = [t.strip() for t in
                  out.stdout.decode("utf-8", "replace").splitlines()
                  if t.strip()]
        if titles:
            break
    print("waited %ds for a window" % int(DEADLINE - (deadline - time.time())))
except Exception as exc:                          # noqa: BLE001
    print("could not enumerate windows:", exc)

print("window titles owned by the process:", titles or "(none)")
proc.terminate()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
subprocess.run(["taskkill", "/IM", NAME, "/F"], capture_output=True)

bad = [t for t in titles if "exception" in t.lower() or "error" in t.lower()
       or "failed" in t.lower()]
good = [t for t in titles if "pixel rpg" in t.lower()]

if bad:
    print("FAILED: the build opened an error dialog:", bad)
    sys.exit(1)
if not good:
    print("FAILED: no game window appeared.")
    sys.exit(1)
print("PASSED: a real 'Pixel RPG' window opened, with no error dialog.")
sys.exit(0)
