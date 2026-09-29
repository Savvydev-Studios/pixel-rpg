"""Input validation, sanitization and rate limiting.

Threat model for an offline single-player game
----------------------------------------------
There is no server and no network listener, so there is no remote attacker.
The inputs that *are* genuinely untrusted are:

  1. Save files on disk - user-editable JSON, possibly hand-tampered, possibly
     copied from someone else, possibly truncated by a crash or a full disk.
     `Gear(**dict_from_json)` in the original version was an unbounded keyword
     splat straight from untrusted JSON: a crash-on-load at best.
  2. Environment variables - typo'd or hostile values must never take down the
     process or silently produce nonsense settings.
  3. Typed text - profile names, which are echoed to the screen and used to
     build filenames. Path traversal via a profile name is a real bug class.

Everything here is deliberately fail-closed: a malformed value is *rejected and
replaced with a safe default*, never partially trusted.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass

# --------------------------------------------------------------- constants ---

# Hard ceilings. Anything above these is treated as malformed, not clamped
# quietly, because a value this far out of range means the data is not ours.
MAX_SAVE_BYTES = 512 * 1024      # a legitimate save is ~4-20 KB
MAX_JSON_DEPTH = 12
MAX_JSON_NODES = 20000
MAX_NAME_LEN = 24
MAX_TEXT_LEN = 256
MAX_INVENTORY = 60
MAX_LIST_ITEMS = 512
MAX_INT = 1000000000
MIN_INT = -1000000000

# Rate limit defaults, mirroring the "5 attempts / 15 minutes" rule.
AUTH_MAX_ATTEMPTS = 5
AUTH_WINDOW_SECONDS = 15 * 60

_NAME_ALLOWED = re.compile(r"[^A-Za-z0-9 _-]")
_SAFE_SLUG = re.compile(r"[^a-z0-9_-]")


class ValidationError(ValueError):
    """Raised when untrusted data is malformed beyond safe recovery."""


# ----------------------------------------------------------- rate limiting ---

class RateLimiter:
    """Sliding-window rate limiter, keyed by an arbitrary string.

    Used for every action in this game that can be retried against a check, or
    that touches the disk:

      * save-slot decode failures - the closest analogue to a login attempt,
        being the one place where retrying a crafted payload against a
        validator could be used to fish for a parser weakness
      * save writes - prevents a stuck key from thrashing the disk
      * profile creation

    Deliberately monotonic-clock based, so that changing the system clock
    cannot be used to widen the window.
    """

    def __init__(self, max_attempts=AUTH_MAX_ATTEMPTS,
                 window_seconds=AUTH_WINDOW_SECONDS):
        self.max_attempts = max(1, int(max_attempts))
        self.window = max(1.0, float(window_seconds))
        self._hits = {}

    def _prune(self, key, now):
        q = self._hits.setdefault(key, deque())
        cutoff = now - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def check(self, key="default"):
        """True if an attempt is allowed right now. Does not consume budget."""
        return len(self._prune(key, time.monotonic())) < self.max_attempts

    def hit(self, key="default"):
        """Consume one attempt. Returns True if it was allowed."""
        now = time.monotonic()
        q = self._prune(key, now)
        if len(q) >= self.max_attempts:
            return False
        q.append(now)
        return True

    def remaining(self, key="default"):
        return max(0, self.max_attempts - len(self._prune(key, time.monotonic())))

    def retry_after(self, key="default"):
        """Seconds until the next attempt frees up. 0.0 if allowed now."""
        now = time.monotonic()
        q = self._prune(key, now)
        if len(q) < self.max_attempts:
            return 0.0
        return max(0.0, self.window - (now - q[0]))

    def reset(self, key=None):
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


class Cooldown:
    """Minimum interval between repeats of one action (autosave, SFX spam)."""

    def __init__(self, interval):
        self.interval = float(interval)
        self._last = -1e9

    def ready(self):
        return (time.monotonic() - self._last) >= self.interval

    def trigger(self):
        if not self.ready():
            return False
        self._last = time.monotonic()
        return True


# --------------------------------------------------------------- coercion ---

def safe_int(value, lo=MIN_INT, hi=MAX_INT, default=0):
    """Coerce to int inside [lo, hi]. Never raises, never returns a bool."""
    try:
        if isinstance(value, bool):
            return default
        if isinstance(value, str):
            value = value.strip()
            if not value or len(value) > 24:
                return default
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return default
        num = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return lo if num < lo else hi if num > hi else num


def safe_float(value, lo=-1e9, hi=1e9, default=0.0):
    try:
        if isinstance(value, bool):
            return default
        if isinstance(value, str) and len(value.strip()) > 32:
            return default
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if num != num or num in (float("inf"), float("-inf")):
        return default
    return lo if num < lo else hi if num > hi else num


def safe_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    return default


def safe_str(value, max_len=MAX_TEXT_LEN, default=""):
    """Length-capped, control-character-free text."""
    if not isinstance(value, str):
        return default
    return sanitize_text(value, max_len)


def safe_choice(value, allowed, default):
    """Reject anything outside an explicit allow-list (never a deny-list)."""
    return value if value in allowed else default


def safe_color(value, default=(200, 200, 200)):
    """Validate an (r, g, b) triple coming from a save file."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return default
    return tuple(safe_int(c, 0, 255, default[i]) for i, c in enumerate(value))


def safe_list(value, max_items=MAX_LIST_ITEMS):
    """Return a length-capped list, or [] for anything that is not a list."""
    if not isinstance(value, (list, tuple)):
        return []
    return list(value[:max_items])


def safe_dict(value):
    return value if isinstance(value, dict) else {}


# ----------------------------------------------------------- sanitization ---

def sanitize_text(text, max_len=MAX_TEXT_LEN):
    """Strip control/formatting characters and cap the length.

    Removes Unicode categories Cc (control), Cf (format - this is where
    bidi-override and zero-width-joiner spoofing lives) and Cs (surrogates),
    then normalizes to NFC so visually identical strings compare equal.
    """
    if not isinstance(text, str):
        return ""
    text = text[: max_len * 4]  # bound the work before doing per-char passes
    text = unicodedata.normalize("NFC", text)
    cleaned = "".join(
        ch for ch in text
        if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn")
    )
    cleaned = cleaned.replace("\t", " ").strip()
    return cleaned[:max_len]


def sanitize_name(name, fallback="Wanderer"):
    """Profile names: printable ASCII-ish, no path separators, non-empty.

    This value ends up inside a filename, so it is filtered down to a
    conservative allow-list rather than merely having "../" stripped.
    """
    cleaned = sanitize_text(name, MAX_NAME_LEN)
    cleaned = _NAME_ALLOWED.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_")
    return cleaned[:MAX_NAME_LEN] or fallback


def slugify(text, fallback="slot"):
    """Filename-safe slug, guaranteed free of separators and traversal."""
    slug = _SAFE_SLUG.sub("_", sanitize_text(text, MAX_NAME_LEN).lower())
    slug = slug.strip("_-")
    return slug[:MAX_NAME_LEN] or fallback


def safe_filename(name, extension=".json", fallback="slot"):
    """Build a leaf filename that cannot escape its directory."""
    leaf = slugify(name, fallback) + extension
    # Belt and braces: reject anything that still looks like a path.
    if os.sep in leaf or (os.altsep and os.altsep in leaf) or leaf.startswith("."):
        raise ValidationError("unsafe filename derived from input")
    return leaf


def is_within(base_dir, candidate):
    """True only if `candidate` resolves inside `base_dir`.

    Each side is compared twice: once through realpath, once through plain
    abspath. The second pass exists because a store-packaged Python silently
    redirects %APPDATA% writes into its own LocalCache. The save file then
    resolves through that redirection while the save directory does not, so
    two paths in genuinely the same tree stopped matching and the loader
    refused its own saves. abspath still collapses "..", so a traversal
    attempt is rejected by both passes.

    Comparison is normcased, which on Windows also makes it case-insensitive;
    the filesystem is, so a drive letter in the other case is the same place.
    """
    try:
        pairs = ((os.path.realpath(str(base_dir)),
                  os.path.realpath(str(candidate))),
                 (os.path.abspath(str(base_dir)),
                  os.path.abspath(str(candidate))))
    except (OSError, ValueError):
        return False
    for base, target in pairs:
        base = os.path.normcase(base)
        target = os.path.normcase(target)
        if target == base or target.startswith(base + os.sep):
            return True
    return False


# ------------------------------------------------------ structural checks ---

def check_structure(data, max_depth=MAX_JSON_DEPTH, max_nodes=MAX_JSON_NODES):
    """Reject deeply nested or absurdly large decoded JSON.

    Guards against a billion-laughs-shaped save file: JSON that decodes fine
    but blows the stack or the heap once we start walking it.
    """
    nodes = 0
    stack = [(data, 0)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if depth > max_depth:
            raise ValidationError("structure nested too deeply")
        if nodes > max_nodes:
            raise ValidationError("structure has too many nodes")
        if isinstance(node, dict):
            if len(node) > MAX_LIST_ITEMS:
                raise ValidationError("object has too many keys")
            for key, val in node.items():
                if not isinstance(key, str) or len(key) > 64:
                    raise ValidationError("invalid object key")
                stack.append((val, depth + 1))
        elif isinstance(node, list):
            if len(node) > MAX_LIST_ITEMS:
                raise ValidationError("array too long")
            for val in node:
                stack.append((val, depth + 1))
        elif isinstance(node, str):
            if len(node) > 4096:
                raise ValidationError("string field too long")
        elif node is not None and not isinstance(node, (int, float, bool)):
            raise ValidationError("unsupported JSON value type")
    return True


def read_text_limited(path, max_bytes=MAX_SAVE_BYTES):
    """The text of a file, with a size ceiling checked *before* reading.

    Order matters: stat first, then read at most max_bytes + 1, so a file that
    grows between the two calls still cannot blow up memory.

    Split out from `load_json_limited` when saves stopped necessarily being
    files (`store.py`). This half is about a **path**; the half below is about
    a **payload**, and only the first one has nothing to bite on in a browser.
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ValidationError("cannot stat save file: %s" % exc) from exc
    if size > max_bytes:
        raise ValidationError("file is %d bytes, limit is %d" % (size, max_bytes))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read(max_bytes + 1)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError("cannot read save file: %s" % exc) from exc
    if len(raw.encode("utf-8", "ignore")) > max_bytes:
        raise ValidationError("file exceeded size limit while reading")
    return raw


def parse_json_limited(raw, max_bytes=MAX_SAVE_BYTES):
    """Parse and structure-check JSON text, whatever medium it came from.

    Every backend in `store.py` funnels through here, so a new place to keep
    saves can never become a new way to skip the checks.
    """
    if len(raw.encode("utf-8", "ignore")) > max_bytes:
        raise ValidationError("payload exceeded size limit")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError("malformed JSON: %s" % exc) from exc
    if not isinstance(data, dict):
        raise ValidationError("top-level JSON value must be an object")
    check_structure(data)
    return data


def encode_json_limited(payload, max_bytes=MAX_SAVE_BYTES, indent=1):
    """Serialize with a ceiling, before anything touches a medium."""
    try:
        text = json.dumps(payload, indent=indent, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValidationError("cannot encode payload: %s" % exc) from exc
    if len(text.encode("utf-8")) > max_bytes:
        raise ValidationError("payload exceeds %d bytes" % max_bytes)
    return text


def load_json_limited(path, max_bytes=MAX_SAVE_BYTES):
    """Read and parse JSON from a path. Kept for callers that have a path."""
    return parse_json_limited(read_text_limited(path, max_bytes), max_bytes)


def pick_known_keys(data, allowed_keys):
    """Drop unknown keys instead of splatting untrusted input into a class.

    This is the fix for the original `Gear(**d)` pattern: unknown keys are
    discarded rather than becoming unexpected keyword arguments.
    """
    src = safe_dict(data)
    return {k: v for k, v in src.items() if k in allowed_keys}


@dataclass
class AuditNote:
    level: str      # "info" | "warn" | "fail"
    area: str
    detail: str


def self_audit():
    """Runtime self-check, surfaced by `python -m pixel_rpg.audit`.

    Verifies the invariants this module is meant to guarantee, so a regression
    shows up as a failed check rather than a silent hole.
    """
    notes = []

    def check(cond, area, ok_msg, fail_msg):
        notes.append(AuditNote("info" if cond else "fail", area,
                               ok_msg if cond else fail_msg))

    check(sanitize_name("../../etc/passwd") == "etcpasswd",
          "path-traversal", "profile names cannot contain separators",
          "traversal sequence survived sanitize_name")
    check("/" not in safe_filename("../../evil")
          and "\\" not in safe_filename("../../evil"),
          "path-traversal", "derived filenames are leaf-only",
          "safe_filename produced a path")
    check(safe_int("9" * 100, 0, 100, 7) == 7,
          "oversized-input", "absurd numeric strings are rejected",
          "oversized numeric string accepted")
    check(safe_int(10 ** 20, 0, 100, 5) == 100,
          "oversized-input", "huge ints clamp to range",
          "huge int not clamped")
    check(safe_int(float("nan"), 0, 100, 3) == 3,
          "malformed-input", "NaN rejected", "NaN accepted")
    check(len(sanitize_text("A" * 10000, 32)) == 32,
          "oversized-input", "text is length-capped", "text cap not enforced")
    check(sanitize_text("bad‮chars​ here") == "badchars here",
          "malformed-input", "control and bidi characters stripped",
          "control characters survived sanitization")
    check(pick_known_keys({"name": "x", "__class__": "y"}, {"name"}) == {"name": "x"},
          "deserialization", "unknown keys dropped before construction",
          "unknown keys reached the constructor")

    deep = {"a": None}
    node = deep
    for _ in range(40):
        node["a"] = {"a": None}
        node = node["a"]
    try:
        check_structure(deep)
        notes.append(AuditNote("fail", "resource-exhaustion",
                               "deeply nested JSON was accepted"))
    except ValidationError:
        notes.append(AuditNote("info", "resource-exhaustion",
                               "deeply nested JSON rejected"))

    limiter = RateLimiter(5, 900)
    allowed = sum(1 for _ in range(9) if limiter.hit("probe"))
    check(allowed == 5, "rate-limit",
          "limiter caps attempts at 5 per 15 min window",
          "limiter allowed %d attempts, expected 5" % allowed)

    if os.environ.get("API_KEY"):
        notes.append(AuditNote("info", "secrets",
                               "API_KEY read from environment, never from source"))
    else:
        notes.append(AuditNote("info", "secrets",
                               "no API_KEY set; optional online features stay off"))
    return notes


def atomic_write_json(path, payload, max_bytes=MAX_SAVE_BYTES, indent=1):
    """Write JSON without ever leaving a half-written file in place.

    Serialize first, size-check, write to a temp file in the same directory,
    fsync, then rename over the target. A crash at any point leaves either the
    old file or the new one, never a truncated mixture.
    """
    try:
        text = json.dumps(payload, indent=indent, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValidationError("cannot encode payload: %s" % exc) from exc
    if len(text.encode("utf-8")) > max_bytes:
        raise ValidationError("payload exceeds %d bytes" % max_bytes)

    target = str(path)
    temp = target + ".tmp"
    try:
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        return True
    except OSError as exc:
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except OSError:
            pass
        raise ValidationError("cannot write %s: %s" % (target, exc)) from exc
