"""Settings, paths and environment loading.

SECRETS POLICY
--------------
No credential is ever written into source. Every secret-shaped value is read
from the process environment via `os.environ.get(...)`, optionally populated
from a local `.env` file that is git-ignored. `.env.example` ships with
placeholders only.

The core game is fully offline and needs no key at all. `API_KEY` exists so
that the optional online extras (leaderboard submission, remote seed-of-the-day)
have a documented, safe way to obtain a credential if they are ever switched on.
When it is absent, those features stay disabled instead of failing loudly.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import security as sec

# python-dotenv is optional. If it is not installed we fall back to a tiny
# parser so a local .env still works, and the game still runs with neither.
try:  # pragma: no cover - trivial import shim
    from dotenv import load_dotenv as _dotenv_load
    HAS_DOTENV = True
except ImportError:  # pragma: no cover
    HAS_DOTENV = False
    _dotenv_load = None

APP_NAME = "Pixel_RPG"
ENV_PREFIX = "PIXEL_RPG_"

DIFFICULTIES = ("story", "normal", "hard", "nightmare")
#: What each mode offers. **`story` is the easiest difficulty and a mode
#: whose whole content is a score has no business offering the one named
#: after the thing it does not have** - item 0bb, his call. Anything not
#: listed here gets all of them, so a new mode is unrestricted until
#: somebody restricts it.
DIFFICULTIES_BY_MODE = {
    "waves": tuple(d for d in DIFFICULTIES if d != "story"),
    "zombies": tuple(d for d in DIFFICULTIES if d != "story"),
    # **His rule, by name** (item 0bb): *"story shouldnt be a difficulty
    # for the wave gamemode and same for zombie gamemode"*. A hoard is
    # the same kind of thing - a scored run with no save - so it takes
    # the same list rather than a new opinion.
    "hoard": tuple(d for d in DIFFICULTIES if d != "story"),
}


def difficulties_for(mode):
    """Which difficulties `mode` offers. Never raises."""
    return DIFFICULTIES_BY_MODE.get(str(mode or ""), DIFFICULTIES)
DIFFICULTY_MODS = {
    #            enemy hp   enemy atk   xp     gold   flee
    "story":     (0.75,     0.70,       1.15,  1.20,  0.90),
    "normal":    (1.00,     1.00,       1.00,  1.00,  0.60),
    "hard":      (1.30,     1.25,       1.25,  1.10,  0.45),
    "nightmare": (1.70,     1.55,       1.60,  1.25,  0.30),
}


def _fallback_dotenv(path):
    """Minimal KEY=VALUE reader used when python-dotenv is unavailable.

    Deliberately conservative: it never overwrites a variable that is already
    set in the real environment, and it ignores anything malformed rather than
    guessing.
    """
    try:
        if os.path.getsize(path) > 64 * 1024:
            return False
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read(64 * 1024).splitlines()
    except OSError:
        return False
    for line in lines[:500]:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.replace("_", "").isalnum():
            continue
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value[:4096])
    return True


def load_environment(project_root=None):
    """Populate os.environ from a local .env if one exists. Returns the path."""
    root = Path(project_root or Path(__file__).resolve().parent.parent)
    env_path = root / ".env"
    if not env_path.is_file():
        return None
    if HAS_DOTENV:
        _dotenv_load(dotenv_path=str(env_path), override=False)
    else:
        _fallback_dotenv(env_path)
    return env_path


def get_secret(name, default=None):
    """The only sanctioned way to read a credential.

    Values are never logged, never rendered on screen, and never written into
    a save file. Callers get the raw value or the default.
    """
    value = os.environ.get(name, default)
    if isinstance(value, str):
        value = value.strip()
        if not value or value.upper().startswith("YOUR_"):
            # An untouched placeholder from .env.example counts as "not set".
            return default
    return value


def has_online_features():
    """Online extras are opt-in and stay off unless a key is provided."""
    return get_secret("API_KEY") is not None


def _env(name, default=None):
    return os.environ.get(ENV_PREFIX + name, default)


SAVE_LOCATIONS = ("default", "portable")
SAVE_LOCATION_LABEL = {
    "default": "System folder",
    "portable": "Next to the game",
}


def project_root():
    """The folder holding the game, whether run from source or as an exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def location_file():
    """Where the save-location choice itself is remembered.

    It cannot live in a save file, since it decides where save files go, so
    it sits beside the game as one short line of text.
    """
    return project_root() / "save_location.txt"


def read_save_location():
    try:
        text = location_file().read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "default"
    return text if text in SAVE_LOCATIONS else "default"


def write_save_location(value):
    value = value if value in SAVE_LOCATIONS else "default"
    try:
        location_file().write_text(value, encoding="utf-8")
        return True
    except OSError:
        return False


def save_dir_overridden():
    """True when PIXEL_RPG_SAVE_DIR is forcing the location.

    The in-game setting cannot win against it, so the interface has to say so
    rather than appearing to do nothing.
    """
    return bool(_env("SAVE_DIR"))


def portable_save_dir():
    return project_root() / "saves"


def is_browser():
    """Whether this is running inside a browser under pygbag.

    `emscripten` is what CPython reports when compiled to WASM, and it is the
    check pygbag itself documents. A function rather than a constant so a
    test can stand in front of it - there is no other way to exercise the
    browser paths from a desktop.
    """
    return sys.platform == "emscripten"


# A browser window can be dragged to any size, including silly ones. The
# layout works down to 390px (section 29) so this is a floor against a
# window with no room for anything at all, not a target.
MIN_BROWSER_SIZE = (480, 360)


def browser_viewport():
    """The page's inner size, or None if this is not a page.

    Asked rather than assumed, because **the web build is a desktop build**
    (handoff section 37) and a desktop browser window is whatever size
    somebody dragged it to. pygbag builds a fixed framebuffer and its page
    template never resizes the canvas, so without this the game sits at its
    build size in the corner of a maximised window.

    Every failure is None: this runs on a bridge to JavaScript, and a game
    that will not start because it could not measure a window is worse than
    a game at the wrong size.
    """
    if not is_browser():
        return None
    try:
        import platform as browser_platform
        window = browser_platform.window
        width, height = int(window.innerWidth), int(window.innerHeight)
    except Exception:                                 # noqa: BLE001
        return None
    if width <= 0 or height <= 0:
        return None
    return (max(MIN_BROWSER_SIZE[0], width),
            max(MIN_BROWSER_SIZE[1], height))


def open_save_store(directory):
    """Where saves should live on this platform.

    The one place that decides. A browser has no filesystem worth the name -
    pygbag gives you an in-memory one that vanishes with the tab - so saves
    go to `localStorage` instead, through the seam in `store.py`.
    """
    from . import store as store_mod
    if is_browser():
        return store_mod.BrowserStore()
    return store_mod.FileStore(directory)


def default_save_dir():
    """Per-user save directory, outside the source tree so it is never staged.

    Honours PIXEL_RPG_SAVE_DIR, then the platform convention, then a local
    fallback. The chosen path is created lazily by the save manager.
    """
    override = _env("SAVE_DIR")
    if override:
        try:
            return Path(override).expanduser().resolve()
        except (OSError, ValueError):
            pass
    if read_save_location() == "portable":
        return portable_save_dir()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / APP_NAME / "saves"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_NAME / "saves"


@dataclass
class Settings:
    """Runtime configuration.

    Every field is validated on construction, so a hostile or fat-fingered
    environment variable can degrade the experience but cannot crash the game
    or push a value somewhere it does not belong.
    """
    width: int = 1280
    height: int = 720
    fps: int = 60
    tile: int = 32
    fullscreen: bool = False
    master_volume: float = 0.55
    sfx_enabled: bool = True
    difficulty: str = "normal"
    seed: int | None = None
    show_fps: bool = False
    screen_shake: bool = True
    #: **The arena preference** (item 0au): how a Last Stand picks where
    #: each wave is fought. `"every"` walks the list on `arena_every`
    #: waves, `"one"` stays in `arena_pick`, `"random"` rolls one place
    #: per run. Settings rather than a wave-mode screen of its own,
    #: because it is a preference and this is where preferences live.
    arena_mode: str = "every"
    arena_every: int = 2
    arena_pick: int = 0
    damage_numbers: bool = True
    autosave: bool = True
    debug: bool = False
    save_dir: Path = field(default_factory=default_save_dir)

    @classmethod
    def from_env(cls):
        seed_raw = _env("SEED")
        seed = sec.safe_int(seed_raw, 0, 2 ** 31 - 1, -1) if seed_raw else -1
        return cls(
            width=sec.safe_int(_env("WIDTH"), 800, 3840, 1280),
            height=sec.safe_int(_env("HEIGHT"), 600, 2160, 720),
            fps=sec.safe_int(_env("FPS"), 30, 240, 60),
            tile=sec.safe_int(_env("TILE"), 16, 64, 32),
            fullscreen=sec.safe_bool(_env("FULLSCREEN"), False),
            master_volume=sec.safe_float(_env("VOLUME"), 0.0, 1.0, 0.55),
            sfx_enabled=sec.safe_bool(_env("SFX"), True),
            difficulty=sec.safe_choice(
                (_env("DIFFICULTY") or "normal").strip().lower(),
                DIFFICULTIES, "normal"),
            seed=None if seed < 0 else seed,
            show_fps=sec.safe_bool(_env("SHOW_FPS"), False),
            debug=sec.safe_bool(_env("DEBUG"), False),
            save_dir=default_save_dir(),
        )

    # -- derived -----------------------------------------------------------

    @property
    def mods(self):
        """(enemy_hp, enemy_atk, xp, gold, flee) multipliers for difficulty."""
        return DIFFICULTY_MODS.get(self.difficulty, DIFFICULTY_MODS["normal"])

    def cycle_difficulty(self, step=1, allowed=None):
        """Move one notch, through whichever difficulties are on offer.

        `allowed` lets a caller narrow the list - **item 0bb**, his
        words: *"story shouldnt be a difficulty for the wave gamemode
        and same for zombie gamemode"*. It is a filter rather than a
        second list, so a difficulty added later is offered everywhere
        by default and hidden only where somebody says so.
        """
        offer = tuple(d for d in DIFFICULTIES
                      if allowed is None or d in allowed) or DIFFICULTIES
        if self.difficulty not in offer:
            # Arrived here already set to something this mode does not
            # offer - carried in from a story run, or read off a config
            # file. Land on the first legal one rather than refusing.
            self.difficulty = offer[0]
            return self.difficulty
        idx = offer.index(self.difficulty)
        self.difficulty = offer[(idx + step) % len(offer)]
        return self.difficulty

    def to_dict(self):
        """Persisted user preferences. Note: no secret is ever included."""
        return {
            "master_volume": round(self.master_volume, 3),
            "sfx_enabled": self.sfx_enabled,
            "difficulty": self.difficulty,
            "show_fps": self.show_fps,
            "screen_shake": self.screen_shake,
            "arena_mode": self.arena_mode,
            "arena_every": self.arena_every,
            "arena_pick": self.arena_pick,
            "damage_numbers": self.damage_numbers,
            "autosave": self.autosave,
            "fullscreen": self.fullscreen,
        }

    def apply_dict(self, data):
        """Load preferences from disk, validating every field."""
        data = sec.safe_dict(data)
        self.master_volume = sec.safe_float(
            data.get("master_volume"), 0.0, 1.0, self.master_volume)
        self.sfx_enabled = sec.safe_bool(data.get("sfx_enabled"), self.sfx_enabled)
        self.difficulty = sec.safe_choice(
            data.get("difficulty"), DIFFICULTIES, self.difficulty)
        self.show_fps = sec.safe_bool(data.get("show_fps"), self.show_fps)
        self.screen_shake = sec.safe_bool(data.get("screen_shake"), self.screen_shake)
        from .gamemodes import ARENA_CADENCES, ARENA_MODES, ARENAS
        self.arena_mode = sec.safe_choice(
            data.get("arena_mode"), ARENA_MODES, self.arena_mode)
        self.arena_every = sec.safe_int(
            data.get("arena_every"), min(ARENA_CADENCES),
            max(ARENA_CADENCES), self.arena_every)
        self.arena_pick = sec.safe_int(
            data.get("arena_pick"), 0, len(ARENAS) - 1, self.arena_pick)
        self.damage_numbers = sec.safe_bool(
            data.get("damage_numbers"), self.damage_numbers)
        self.autosave = sec.safe_bool(data.get("autosave"), self.autosave)
        self.fullscreen = sec.safe_bool(data.get("fullscreen"), self.fullscreen)
        return self
