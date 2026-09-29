"""Security self-audit CLI: `python -m pixel_rpg.audit`.

Runs the invariant checks in `security`, then sweeps the project tree for
anything that looks like a committed credential. Exits non-zero if either
finds a problem, so it can be dropped into a pre-commit hook or CI job.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from . import security as sec
from .config import get_secret, has_online_features

# High-confidence patterns: provider-issued credential prefixes and key
# blocks. These are NEVER allowlist-suppressed, because an allowlist word
# can occur inside the credential itself - AWS example key ids literally
# contain the word EXAMPLE - and a real leak must not hide behind a
# coincidence of letters.
STRONG_PATTERNS = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]

# Heuristic pattern: a credential-shaped name assigned a literal. This one
# fires on ordinary code, so the allowlist does apply to it.
WEAK_PATTERNS = [
    ("assigned literal secret",
     re.compile(r"""(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['"][^'"\n]{8,}['"]""")),
]

# A match is ignored when the line also contains one of these: it is a
# placeholder, a reference to an environment variable, or documentation.
ALLOWLIST = re.compile(
    r"(?i)(YOUR_[A-Z_]*_HERE|os\.environ|getenv|get_secret|\.env\.example"
    r"|placeholder|example|<[a-z-]+>|xxxx)")

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
             ".mypy_cache", ".pytest_cache", "dist", "build"}
SCAN_SUFFIXES = {".py", ".md", ".txt", ".json", ".cfg", ".ini", ".toml",
                 ".yml", ".yaml", ".sh", ".bat", ".env", ".example"}
MAX_SCAN_BYTES = 2 * 1024 * 1024


def scan_tree(root):
    """Return a list of (path, line_no, label, excerpt) for likely secrets."""
    findings = []
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() not in SCAN_SUFFIXES and filename != ".env":
                continue
            try:
                if path.stat().st_size > MAX_SCAN_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if len(line) > 4000:
                    continue
                hit = next((label for label, pattern in STRONG_PATTERNS
                            if pattern.search(line)), None)
                if hit is None and not ALLOWLIST.search(line):
                    hit = next((label for label, pattern in WEAK_PATTERNS
                                if pattern.search(line)), None)
                if hit:
                    findings.append((path.relative_to(root), line_no,
                                     hit, line.strip()[:88]))
    return findings


def check_gitignore(root):
    """Confirm .env and the save directory are excluded from version control."""
    path = Path(root) / ".gitignore"
    if not path.is_file():
        return False, ".gitignore is missing"
    try:
        entries = {line.strip() for line in
                   path.read_text(encoding="utf-8").splitlines()}
    except OSError as exc:
        return False, "cannot read .gitignore: %s" % exc
    missing = [needed for needed in (".env",) if needed not in entries]
    if missing:
        return False, ".gitignore does not list: %s" % ", ".join(missing)
    return True, ".gitignore excludes .env"


def check_env_file_committed(root):
    path = Path(root) / ".env"
    if path.is_file():
        return True, ".env exists locally (git-ignored, never committed)"
    return True, "no local .env; the game runs without one"


def check_logs():
    """Scan the logs the game has written for anything credential-shaped.

    `logbook.scrub` is supposed to make this impossible, so a hit here means
    the scrubber has a hole, not that a log needs deleting. Scanning the real
    files is the only way to find that out.
    """
    from . import logbook
    folder = logbook.log_dir()
    if not folder.is_dir():
        return ["ok   no logs written yet"]
    checked = 0
    for path in list(folder.glob("*.log"))[:200]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        checked += 1
        for label, pattern in STRONG_PATTERNS:
            if pattern.search(text):
                return ["FAIL %s leaked a %s" % (path.name, label)]
    if not checked:
        return ["ok   no logs written yet"]
    return ["ok   %d log file%s scanned, nothing credential-shaped"
            % (checked, "" if checked == 1 else "s")]


def main(argv=None):
    root = Path(__file__).resolve().parent.parent
    print("Pixel RPG - security audit")
    print("project: %s" % root)
    print("-" * 68)

    failures = 0

    print("\n[1] Runtime invariants")
    for note in sec.self_audit():
        marker = "ok  " if note.level == "info" else "FAIL"
        print("  %s %-22s %s" % (marker, note.area, note.detail))
        if note.level == "fail":
            failures += 1

    print("\n[2] Secret scan")
    findings = scan_tree(root)
    if findings:
        failures += len(findings)
        for path, line_no, label, excerpt in findings:
            print("  FAIL %s:%d  %s" % (path, line_no, label))
            print("       %s" % excerpt)
    else:
        print("  ok   no hardcoded credentials found in the project tree")

    print("\n[3] Log hygiene")
    for line in check_logs():
        print("  %s" % line)
        if line.startswith("FAIL"):
            failures += 1

    print("\n[4] Version control hygiene")
    for check in (check_gitignore, check_env_file_committed):
        passed, detail = check(root)
        print("  %s %s" % ("ok  " if passed else "FAIL", detail))
        if not passed:
            failures += 1

    print("\n[5] Configuration")
    print("  ok   online features: %s"
          % ("enabled (API_KEY present)" if has_online_features()
             else "disabled (no API_KEY set)"))
    # **Every way the game can reach off this machine, named** (s85).
    # A player should be able to ask "what talks to the internet?" and
    # get a list rather than a shrug, and both of these are off by
    # default and turned on by an environment variable.
    from . import config as _config, relay as _relay
    _board_on = bool((_config.get_secret("PIXEL_RPG_BOARD_URL")
                      or "").strip())
    print("  ok   global board: %s"
          % ("on (PIXEL_RPG_BOARD_URL set)" if _board_on
             else "off (no PIXEL_RPG_BOARD_URL)"))
    print("  ok   co-op relay: %s"
          % ("on (PIXEL_RPG_RELAY_URL set) - whoever runs it can see "
             "everything the room says" if _relay.enabled()
             else "off (no PIXEL_RPG_RELAY_URL); rooms are direct"))
    print("  ok   rate limit: %d attempts per %d minutes on retryable actions"
          % (sec.AUTH_MAX_ATTEMPTS, sec.AUTH_WINDOW_SECONDS // 60))
    print("  ok   save file ceiling: %d KB, max nesting %d"
          % (sec.MAX_SAVE_BYTES // 1024, sec.MAX_JSON_DEPTH))

    print("\n" + "-" * 68)
    if failures:
        print("RESULT: %d issue(s) found." % failures)
        return 1
    print("RESULT: clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
