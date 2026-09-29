"""The meta profile: what survives across every save file.

Codex pages, endings reached, unlocked classes, New Game+ tier and run history
live here, in one `profile.json` beside the save slots. This is the spine of
the replay loop - a new run starts empty of gear but never empty of knowledge,
and two of the four endings are gated on knowing things you can only have
learned in an earlier run.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import classes as classes_mod
from . import gamemodes
from . import security as sec
from . import story

PROFILE_VERSION = 1
MAX_HISTORY = 40


@dataclass
class RunRecord:
    """One completed run, for the records screen."""
    class_key: str = "warden"
    ending: str = ""
    level: int = 1
    score: int = 0
    ngplus: int = 0
    playtime: float = 0.0
    modifiers: tuple = ()
    when: str = ""
    # **Which mode wrote this** (0ag, s59). Defaulted rather than
    # required, so every record already on disk reads back as a story run
    # without a migration - `pick_known_keys` drops what it does not know
    # and fills what is missing.
    mode: str = "story"
    #: Waves survived. Zero for anything that is not a wave run.
    waves: int = 0
    #: **How many people were in the party** (item 0az). His shape:
    #: *"a global score leaderboard for each gamemode for each party
    #: size"* - so a board is keyed on the pair, and a solo Last Stand
    #: and a four-player one are different boards rather than one board
    #: somebody has to mentally filter. Defaulted, so every record
    #: already on disk reads back as a solo run without a migration.
    party: int = 1

    def to_dict(self):
        return {"class_key": self.class_key, "ending": self.ending,
                "level": self.level, "score": self.score,
                "ngplus": self.ngplus, "playtime": round(self.playtime, 1),
                "modifiers": list(self.modifiers), "when": self.when,
                "mode": self.mode, "waves": int(self.waves),
                "party": int(self.party)}

    @classmethod
    def from_dict(cls, data):
        raw = sec.pick_known_keys(data, {"class_key", "ending", "level",
                                         "score", "ngplus", "playtime",
                                         "modifiers", "when",
                                         "mode", "waves", "party"})
        return cls(
            class_key=sec.safe_choice(raw.get("class_key"),
                                      tuple(classes_mod.CLASSES_BY_KEY),
                                      "warden"),
            ending=sec.safe_str(raw.get("ending"), 24),
            level=sec.safe_int(raw.get("level"), 1, 99, 1),
            score=sec.safe_int(raw.get("score"), 0, 99_999_999, 0),
            party=sec.safe_int(raw.get("party"), 1, 4, 1),
            ngplus=sec.safe_int(raw.get("ngplus"), 0, 99, 0),
            playtime=sec.safe_float(raw.get("playtime"), 0, 1e7, 0.0),
            modifiers=tuple(sec.safe_str(m, 32)
                            for m in sec.safe_list(raw.get("modifiers"), 16)),
            when=sec.safe_str(raw.get("when"), 32),
            mode=sec.safe_choice(raw.get("mode"),
                                 tuple(gamemodes.MODES_BY_KEY),
                                 gamemodes.DEFAULT_MODE),
            waves=sec.safe_int(raw.get("waves"), 0, 99_999, 0))


def _safe_scene_map(raw):
    """Validate a save-borne {scene: [choices]} map down to known scenes.

    Everything here came off disk, so nothing is trusted: unknown scene keys
    and unknown choice keys are dropped rather than carried around.
    """
    from . import scenes as scenes_mod
    out = {}
    for key, values in sec.safe_dict(raw).items():
        scene = scenes_mod.SCENES_BY_KEY.get(sec.safe_str(key, 48))
        if scene is None:
            continue
        valid = {c.key for c in scene.choices}
        picked = []
        for value in sec.safe_list(values, 32):
            name = sec.safe_str(value, 32)
            if name in valid and name not in picked:
                picked.append(name)
        if picked:
            out[scene.key] = picked
    return out


class MetaProfile:
    """Persistent, cross-save player knowledge."""

    def __init__(self, directory=None):
        # A directory, a `store.Store`, or None for a profile that is never
        # written. `None` stays meaningful: dozens of tests build a throwaway
        # profile with it and must not start touching a disk.
        from . import store as store_mod
        self.store = store_mod.open_store(directory) if directory else None
        self.dir = getattr(self.store, "dir", None) if self.store else None
        self.codex = []            # unlocked codex keys, in discovery order
        self.endings = []          # ending keys reached
        self.ngplus = 0            # highest New Game+ tier reached
        self.runs = []             # RunRecord list, newest last
        self.totals = {"runs": 0, "wins": 0, "kills": 0, "bosses": 0,
                       "deaths": 0, "gold": 0, "playtime": 0.0}
        self.best_score = 0
        # Wave mode keeps its own bests (0ag, s59) - see `finish_run`.
        self.best_waves = 0
        self.best_wave_score = 0
        self.seen_prologue = False
        self.keybinds = {}          # action -> key code, empty means defaults
        # Controller support is off until asked for, because finding a pad
        # costs about ten seconds. Kept here, beside the keybinds, so the
        # choice is shared by every save rather than living in one of them.
        self.gamepad = False
        self.touch = False
        # Whether the debug menu has ever been found. Recording the
        # *discovery* is safe: it is finding it that earns the feat,
        # and finding it changes nothing about the run.
        self.found_devmenu = False
        # Feats earned, kept here rather than in the save. They are
        # meta-progression: earning one on a second character should not
        # mean the first character never did it.
        self.achievements = []
        # The story record, across every save. `scene_seen` is what a scene
        # has ever offered you; `scene_taken` is what you actually picked.
        # Both are needed: the codex greys out the roads not taken, and it
        # can only do that if it knows they were on the table.
        self.scene_seen = {}       # scene key -> [choice keys offered]
        self.scene_taken = {}      # scene key -> [choice keys chosen]
        self.secrets = []          # flags that unlock hidden choices
        # Set while the current run has used a debug power. Deliberately not
        # persisted: it describes the run in progress, not the profile.
        #
        # Every mutator below refuses while it is set, and that is the whole
        # safety model for the debug menu. Enforcing it here rather than at
        # each call site means a new caller cannot forget: the codex, the
        # endings and the records are the progression that survives across
        # saves, and spoiling those from a debug menu would be irreversible.
        self.tainted = False

    # -- knowledge ---------------------------------------------------------

    def unlock_codex(self, key):
        """Returns the CodexEntry if this is the first time, else None."""
        if self.tainted:
            return None
        key = sec.safe_str(key, 48)
        if key not in story.CODEX_BY_KEY or key in self.codex:
            return None
        self.codex.append(key)
        return story.CODEX_BY_KEY[key]

    def knows(self, key):
        return key in self.codex

    def codex_progress(self):
        """Pages known, out of pages the player may know exist.

        The total moves, because a hidden ending adds its page only once it
        has happened. A fixed total would announce that something is missing.
        """
        visible = story.visible_codex(self.codex)
        return len(self.codex), len(visible)

    def record_ending(self, key):
        if self.tainted:
            return False
        key = sec.safe_str(key, 24)
        fresh = key in story.ENDINGS_BY_KEY and key not in self.endings
        if fresh:
            self.endings.append(key)
        self.unlock_codex("ending_%s" % key)
        return fresh

    def unlock_achievement(self, key):
        """Record a feat. Refused by a marked run, like every mutator here."""
        if self.tainted:
            return False
        key = sec.safe_str(key, 32)
        if not key or key in self.achievements:
            return False
        self.achievements.append(key)
        return True

    def record_scene(self, scene_key, offered, taken):
        """Remember a scene: what it offered, and what was picked.

        Refused by a marked run like every mutator here, which is what stops
        the debug menu being a way to farm the story.
        """
        if self.tainted:
            return False
        scene_key = sec.safe_str(scene_key, 48)
        if not scene_key:
            return False
        seen = list(self.scene_seen.get(scene_key, []))
        for key in offered:
            key = sec.safe_str(key, 32)
            if key and key not in seen:
                seen.append(key)
        self.scene_seen[scene_key] = seen

        taken_key = sec.safe_str(taken, 32)
        if taken_key:
            picked = list(self.scene_taken.get(scene_key, []))
            if taken_key not in picked:
                picked.append(taken_key)
            self.scene_taken[scene_key] = picked
        return True

    def learn_secret(self, flag):
        """Hold a hidden-choice flag. Returns the flags newly held."""
        if self.tainted or not flag:
            return []
        from . import scenes as scenes_mod
        held, fresh = scenes_mod.grant_for(self.secrets,
                                           sec.safe_str(flag, 32))
        self.secrets = held
        return fresh

    def saw_scene(self, scene_key):
        return scene_key in self.scene_seen

    def scenes_discovered(self):
        return len(self.scene_seen)

    def unlocked_classes(self):
        return [c for c in classes_mod.CLASSES
                if classes_mod.is_unlocked(c.key, self)]

    def locked_classes(self):
        return [c for c in classes_mod.CLASSES
                if not classes_mod.is_unlocked(c.key, self)]

    # -- runs --------------------------------------------------------------

    def finish_run(self, record):
        if self.tainted:
            return
        self.runs.append(record)
        if len(self.runs) > MAX_HISTORY:
            self.runs = self.runs[-MAX_HISTORY:]
        self.totals["runs"] += 1
        if record.ending:
            self.totals["wins"] += 1
        # **A wave score does not touch `best_score`.** They are two
        # scales measuring two different games (a long story run and a
        # deep wave run are not comparable), and one swamping the other on
        # the records screen would make the older number meaningless.
        if record.mode == gamemodes.DEFAULT_MODE:
            self.best_score = max(self.best_score, record.score)
        else:
            # **Only a wave run's depth is a wave count** (§88). Every
            # scored mode fills the same field, so a hoard banking 208
            # coins was posting "Last Stand best: 208 waves" - a record
            # for a game nobody had played.
            if record.mode == "waves":
                self.best_waves = max(self.best_waves, int(record.waves))
            self.best_wave_score = max(self.best_wave_score, record.score)
        self.ngplus = max(self.ngplus, record.ngplus)

    #: How many rows a board shows. Ten is the shape every leaderboard
    #: has ever had and there is no reason to be original about it.
    BOARD_ROWS = 10

    def board(self, mode="story", party=1, limit=None):
        """The best runs for one gamemode at one party size (item 0az).

        **Local for now, and the shape is the point.** He asked for a
        *global* board; there is nowhere to put one, because *"no hosted
        server, ever"* (2026-08-22) is a decision rather than an
        oversight and the relay that would answer it is still open (§19).

        What is built is everything on this side of that line: runs
        carry their party size, the boards are keyed the way he
        described, and syncing one upward later is a transport problem
        rather than a data one. **A local board is also the honest thing
        to show in the meantime** - it is the same list, with fewer
        people in it.
        """
        rows = [r for r in self.runs
                if r.mode == str(mode) and int(r.party) == int(party)]
        rows.sort(key=lambda r: (-int(r.score), -int(r.waves)))
        return rows[:int(limit or self.BOARD_ROWS)]

    def board_sizes(self, mode="story"):
        """Which party sizes have anything on the board for this mode."""
        return sorted({int(r.party) for r in self.runs
                       if r.mode == str(mode)}) or [1]

    def add_totals(self, tracker):
        if self.tainted:
            return
        for key in ("kills", "bosses", "deaths"):
            self.totals[key] = self.totals.get(key, 0) + int(
                tracker.get(key, 0))
        self.totals["gold"] = self.totals.get("gold", 0) + int(
            tracker.get("gold_earned", 0))
        self.totals["playtime"] = self.totals.get("playtime", 0.0) + float(
            tracker.get("playtime", 0.0))

    # -- scoring -----------------------------------------------------------

    @staticmethod
    def score_run(player, wardens_defeated, modifier_score, ngplus, ending):
        """One number for the records screen. Rewards depth, not grinding."""
        base = (player.level * 120
                + wardens_defeated * 900
                + int(player.stats_tracker.get("kills", 0)) * 6
                + int(player.stats_tracker.get("chests", 0)) * 40
                + int(player.stats_tracker.get("gold_earned", 0)) // 25)
        base -= int(player.stats_tracker.get("deaths", 0)) * 250
        if ending:
            base += 2500
        base = int(base * float(modifier_score) * (1.0 + 0.35 * ngplus))
        return max(0, base)

    # -- persistence -------------------------------------------------------

    #: The one key a profile lives under, wherever "under" is.
    KEY = "profile.json"

    def path(self):
        """The profile as a path. None unless it is kept in a filesystem."""
        return (self.dir / self.KEY) if self.dir else None

    def to_dict(self):
        return {
            "version": PROFILE_VERSION,
            "codex": list(self.codex),
            "endings": list(self.endings),
            "ngplus": self.ngplus,
            "best_score": self.best_score,
            "best_waves": self.best_waves,
            "best_wave_score": self.best_wave_score,
            "seen_prologue": self.seen_prologue,
            "totals": {k: (round(v, 1) if isinstance(v, float) else int(v))
                       for k, v in self.totals.items()},
            "runs": [r.to_dict() for r in self.runs],
            "keybinds": dict(self.keybinds),
            "gamepad": bool(self.gamepad),
            "touch": bool(self.touch),
            "found_devmenu": bool(self.found_devmenu),
            "achievements": list(self.achievements),
            "scene_seen": {k: list(v) for k, v in self.scene_seen.items()},
            "scene_taken": {k: list(v) for k, v in self.scene_taken.items()},
            "secrets": list(self.secrets),
        }

    def apply_dict(self, data):
        raw = sec.pick_known_keys(data, {"version", "codex", "endings",
                                         "ngplus", "best_score", "totals",
                                         "best_waves", "best_wave_score",
                                         "runs", "seen_prologue", "keybinds",
                                         "gamepad", "touch",
                                         "found_devmenu",
                                         "achievements", "scene_seen",
                                         "scene_taken", "secrets"})
        self.codex = []
        for key in sec.safe_list(raw.get("codex"), 128):
            name = sec.safe_str(key, 48)
            if name in story.CODEX_BY_KEY and name not in self.codex:
                self.codex.append(name)
        self.endings = []
        for key in sec.safe_list(raw.get("endings"), 16):
            name = sec.safe_str(key, 24)
            if name in story.ENDINGS_BY_KEY and name not in self.endings:
                self.endings.append(name)
        self.ngplus = sec.safe_int(raw.get("ngplus"), 0, 99, 0)
        self.best_score = sec.safe_int(raw.get("best_score"), 0, 99_999_999, 0)
        self.best_waves = sec.safe_int(raw.get("best_waves"), 0, 99_999, 0)
        self.best_wave_score = sec.safe_int(
            raw.get("best_wave_score"), 0, 99_999_999, 0)
        self.seen_prologue = sec.safe_bool(raw.get("seen_prologue"), False)
        self.gamepad = sec.safe_bool(raw.get("gamepad"), False)
        self.touch = sec.safe_bool(raw.get("touch"), False)
        self.found_devmenu = sec.safe_bool(raw.get("found_devmenu"),
                                          False)
        self.scene_seen = _safe_scene_map(raw.get("scene_seen"))
        self.scene_taken = _safe_scene_map(raw.get("scene_taken"))
        self.secrets = []
        for flag in sec.safe_list(raw.get("secrets"), 64):
            name = sec.safe_str(flag, 32)
            if name and name not in self.secrets:
                self.secrets.append(name)
        self.secrets.sort()

        from .quests import ACHIEVEMENTS_BY_KEY
        self.achievements = []
        for key in sec.safe_list(raw.get("achievements"), 64):
            name = sec.safe_str(key, 32)
            if name in ACHIEVEMENTS_BY_KEY and name not in self.achievements:
                self.achievements.append(name)

        totals = sec.safe_dict(raw.get("totals"))
        for key in list(self.totals):
            if key == "playtime":
                self.totals[key] = sec.safe_float(totals.get(key), 0, 1e8, 0.0)
            else:
                self.totals[key] = sec.safe_int(totals.get(key), 0,
                                                100_000_000, 0)
        from .keybinds import Keybinds
        self.keybinds = Keybinds().apply_dict(raw.get("keybinds")).to_dict()
        self.runs = [RunRecord.from_dict(entry)
                     for entry in sec.safe_list(raw.get("runs"), MAX_HISTORY)
                     if isinstance(entry, dict)]
        return self

    def save(self):
        if self.store is None:
            return False
        try:
            self.store.prepare()
            self.store.write_json(self.KEY, self.to_dict())
            return True
        except Exception:                                 # noqa: BLE001
            # A profile is convenience, never progress-critical: losing one
            # costs codex pages, not a run. Never let it take the game down -
            # which now includes a browser refusing storage outright.
            return False

    def load(self):
        if self.store is None or not self.store.exists(self.KEY):
            return self
        try:
            data = self.store.read_json(self.KEY)
        except Exception:                                 # noqa: BLE001
            # Corrupt profile: start a fresh one rather than refusing to boot.
            return self
        version = sec.safe_int(data.get("version"), 0, 999, 0)
        if 0 < version <= PROFILE_VERSION:
            self.apply_dict(data)
        return self


def load_profile(directory):
    return MetaProfile(directory).load()
