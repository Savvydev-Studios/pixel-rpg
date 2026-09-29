"""Session and crash logs.

The game ships as a windowed exe. When something goes wrong on somebody
else's machine there is no console to print to and no traceback to read: the
window simply closes, and every report is "it crashed". These files are the
only way to find out what actually happened.

Four rules hold this module together, in order of importance:

1. **Logging never breaks the game.** Every disk touch is wrapped. A full
   disk, a read-only folder, a file held open by a virus scanner - all of it
   costs you the log, never the run. A logger that can take the game down is
   worse than no logger, because it turns a diagnostic into a fault.
2. **Logging never costs startup.** The file is opened on the first line
   written, not on construction, so the sub-second launch is untouched.
3. **Logs never carry secrets.** Every line goes through `scrub()`, which
   uses the *same* patterns `pixel_rpg.audit` scans with - one list, so the
   scrubber and the auditor cannot drift apart and quietly disagree.
4. **Logs never grow without bound.** Each file is capped and old ones are
   deleted, so a run that logs in a loop costs a few megabytes, not a disk.

Crash logs are written separately from session logs because they are the ones
worth sending to somebody, and hunting for the interesting file inside a pile
of ordinary ones is exactly the friction that stops people reporting bugs.

**A browser gets the same logs on the console instead of on disk.** pygbag's
filesystem lives in memory and dies with the tab, so a file there would cost
memory and produce something nobody can send. Rule 1 is what decides it:
the point is that a crash leaves evidence, and in a tab the console is where
evidence goes. Nothing else about the module changes - same scrubbing, same
cap, same refusal to ever raise.
"""
from __future__ import annotations

import datetime
import platform
import sys
import traceback
from pathlib import Path

from . import config as cfg
from .audit import STRONG_PATTERNS, WEAK_PATTERNS

# A session log is generous; a crash log is small and self-contained.
MAX_SESSION_BYTES = 2 * 1024 * 1024
MAX_CRASH_BYTES = 256 * 1024
KEEP_SESSIONS = 10
KEEP_CRASHES = 20
REDACTED = "[redacted]"


def scrub(text):
    """Remove anything credential-shaped before it reaches the disk.

    Deliberately built from the audit module's patterns rather than its own
    copy: a secret the auditor would flag is a secret the log must not hold,
    and two lists would eventually disagree.
    """
    text = str(text)
    for _label, pattern in STRONG_PATTERNS:
        text = pattern.sub(REDACTED, text)
    for _label, pattern in WEAK_PATTERNS:
        text = pattern.sub(
            lambda m: "%s=%s" % (m.group(1), REDACTED), text)
    return text


def log_dir():
    """Where logs live: beside the saves, never inside them.

    Keeping them out of the save folder means a corrupt or oversized log can
    never be mistaken for save data, and clearing logs cannot touch a run.
    """
    from . import config
    try:
        return Path(config.default_save_dir()).parent / "logs"
    except Exception:                                  # noqa: BLE001
        return Path.cwd() / "logs"


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def environment_lines():
    """The handful of facts every bug report needs and nobody remembers."""
    from . import __version__
    frozen = getattr(sys, "frozen", False)
    try:
        import pygame
        pygame_version = "%s (SDL %s)" % (
            pygame.version.ver, ".".join(str(n) for n in pygame.version.SDL))
    except Exception:                                  # noqa: BLE001
        pygame_version = "unavailable"
    return [
        "game     : Pixel RPG %s" % __version__,
        "build    : %s" % ("packaged exe" if frozen else "from source"),
        "python   : %s" % sys.version.split()[0],
        "pygame   : %s" % pygame_version,
        "platform : %s %s" % (platform.system(), platform.release()),
        "saves    : %s" % log_dir().parent,
    ]


class Logbook:
    """One play session's log, plus a place to put a crash.

    Construction is free and touches nothing. The first `write` creates the
    folder and the file; if that fails the logbook goes quiet for the rest of
    the session rather than retrying on every line.
    """

    def __init__(self, directory=None, enabled=True, console=None):
        self.dir = Path(directory) if directory else None
        self.enabled = enabled
        # A browser is not a machine with a logs folder. pygbag gives the
        # page a filesystem that lives in memory and dies with the tab, so
        # writing there costs memory and produces a file nobody can open.
        # It does have a console, and `print` reaches it.
        #
        # So the log is not switched *off* in a browser, it is redirected -
        # because the reason this module exists is that a crash on somebody
        # else's machine leaves no evidence, and that is exactly as true in
        # a tab. `console` is a parameter rather than a straight call to
        # `is_browser` so a test can have either behaviour on a desktop.
        self.console = cfg.is_browser() if console is None else bool(console)
        self.path = None
        self._handle = None
        self._bytes = 0
        self._broken = False        # a failed open is not retried
        self._pending_header = True

    # -- plumbing ----------------------------------------------------------

    def _folder(self):
        return self.dir if self.dir is not None else log_dir()

    def _open(self):
        if self._handle is not None or self._broken or not self.enabled:
            return self._handle
        try:
            folder = self._folder()
            folder.mkdir(parents=True, exist_ok=True)
            self.path = folder / ("session-%s.log" % _stamp())
            self._handle = open(self.path, "a", encoding="utf-8",
                                errors="replace")
            prune(folder, "session-*.log", KEEP_SESSIONS)
        except Exception:                              # noqa: BLE001
            self._broken = True
            self._handle = None
        return self._handle

    def _emit(self, level, message):
        if not self.enabled or self._broken:
            return
        handle = None
        if not self.console:
            handle = self._open()
            if handle is None:
                return
        if self._pending_header:
            self._pending_header = False
            for line in environment_lines():
                self._emit("env", line)
        if self._bytes >= MAX_SESSION_BYTES:
            return                    # capped, quietly
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = "%s  %-5s %s\n" % (stamp, level, scrub(message))
        if handle is None:
            # The cap still applies: a game that logs in a loop must
            # not be able to fill a console buffer either.
            print(line, end="")
            self._bytes += len(line)
            return
        try:
            handle.write(line)
            handle.flush()            # a crash must not eat the last lines
            self._bytes += len(line)
        except Exception:                              # noqa: BLE001
            self._broken = True

    # -- the interface the game uses ---------------------------------------

    def info(self, message, *args):
        self._emit("info", message % args if args else message)

    def warn(self, message, *args):
        self._emit("warn", message % args if args else message)

    def error(self, message, *args):
        self._emit("error", message % args if args else message)

    def event(self, name, **fields):
        """A structured line: `name key=value key=value`.

        Grep-able without being JSON, which matters because the person
        reading this is usually reading it in Notepad.
        """
        parts = " ".join("%s=%s" % (k, v) for k, v in sorted(fields.items()))
        self._emit("info", ("%s %s" % (name, parts)).strip())

    def crash(self, exc=None, context=""):
        """Write a standalone crash report; return its path, or None.

        Separate from the session log so there is exactly one file to send.
        In a browser there is no file: the report goes to the console and
        this returns None, so nothing announces a path that does not exist.
        Never raises: this runs while the game is already failing.
        """
        try:
            body = ["Pixel RPG crash report", "=" * 44]
            body += environment_lines()
            if context:
                body += ["", "context:", scrub(context)]
            body += ["", "traceback:"]
            if exc is not None:
                body.append(scrub("".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__))))
            else:
                body.append(scrub(traceback.format_exc()))
            if self.path:
                body += ["", "session log: %s" % self.path]
            text = "\n".join(body)[:MAX_CRASH_BYTES]
            if self.console:
                # No folder to write to, and no need for one: the
                # console *is* where a browser crash gets read, and
                # returning None keeps the caller from announcing a
                # file that does not exist.
                print(text)
                return None
            folder = self._folder()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / ("crash-%s.log" % _stamp())
            with open(path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(text)
            prune(folder, "crash-*.log", KEEP_CRASHES)
            self.error("crash written to %s", path)
            return path
        except Exception:                              # noqa: BLE001
            return None

    def close(self):
        try:
            if self._handle is not None:
                self._handle.close()
        except Exception:                              # noqa: BLE001
            pass
        finally:
            self._handle = None


def prune(folder, pattern, keep):
    """Delete all but the newest `keep` files matching `pattern`."""
    try:
        files = sorted(Path(folder).glob(pattern),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in files[keep:]:
            try:
                stale.unlink()
            except OSError:
                continue
    except Exception:                                  # noqa: BLE001
        return
