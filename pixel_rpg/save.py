"""Save slots: versioned, atomically written and validated on load.

Improvements over the original single `save.json`:

  * three named slots instead of one file
  * schema version, so old saves migrate instead of exploding
  * atomic write (temp file + os.replace) so a crash mid-save cannot leave a
    truncated file where the real save used to be
  * a rolling `.bak` of the previous good save
  * every field validated through `security` on the way in
  * an integrity checksum, and a rate limiter on repeated decode failures

On the checksum: it is *tamper-evident*, not tamper-proof. The algorithm and
the file both live on the player's own machine, so anyone determined can
recompute it. It exists to catch corruption and casual editing, and to make
"my save broke" diagnosable. Nothing security-critical rests on it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import classes as classes_mod
from . import security as sec
from . import store as store_mod
from . import story
from .appearance import Appearance
from .entities import (ATTRIBUTES, LOADOUT_SIZE, MAX_LEVEL,
                       POINTS_PER_LEVEL, Player)
from .modifiers import RunModifiers
from . import relics as relics_mod
from .items import CONSUMABLES, Item
from .quests import QuestLog
from .skills import Status

# 5 (2026-08-24): four attributes became nine. A v4 save cannot be read
# straight into the new roster - its points are sitting in bundles that no
# longer exist - so `deserialize_player` refunds them. See below.
SAVE_VERSION = 5
# Six, not three: a debug run, a serious run and an experiment should be
# able to coexist without anybody overwriting the one that mattered.
# **Twelve since 2026-09-06**, on his instruction: *"add more save
# slots"*. It was six, and six is the number every doc string and guide
# line in the game used to say out loud. The picker has scrolled since it
# was written - `draw_slots` computes its visible rows from the window and
# keeps the row height - so this is genuinely the one number to change.
SLOT_COUNT = 12
CHECKSUM_KEY = "checksum"

# Repeated failures to decode a slot are rate limited, mirroring the
# 5-attempts-per-15-minutes rule applied to anything retryable.
_decode_limiter = sec.RateLimiter(sec.AUTH_MAX_ATTEMPTS, sec.AUTH_WINDOW_SECONDS)
_write_cooldown = sec.Cooldown(1.0)


class SaveError(RuntimeError):
    """Raised when a save cannot be written or read safely."""


def _canonical(payload):
    """Stable JSON text used for checksumming."""
    clean = {k: v for k, v in payload.items() if k != CHECKSUM_KEY}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


def compute_checksum(payload):
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# --------------------------------------------------------------- serialize ---

def serialize_player(player):
    return {
        "name": player.name,
        "class_key": player.class_key,
        "appearance": player.appearance.to_dict(),
        "relics": list(player.relics),
        "ngplus": player.ngplus,
        "beaten": player.beaten,
        "fallen": bool(getattr(player, "fallen", False)),
        "chapter_seen": player.chapter_seen,
        "loadout": list(player.loadout),
        "modifiers": player.modifiers.to_list(),
        "level": player.level,
        "xp": player.xp,
        "hp": player.hp,
        "mp": player.mp,
        "gold": player.gold,
        "materials": player.materials,
        # Sticks to the run forever: a save that was ever edited from
        # the debug menu must never quietly become a clean record again.
        "debug_used": bool(player.debug_used),
        "bag_bought": int(getattr(player, "bag_bought", 0)),
        # Door coordinates, as flat pairs - the stacked rooms already
        # emptied (0af). Sorted so a save file does not churn between
        # writes for no reason.
        "hoards": sorted([int(gx), int(gy)]
                         for gx, gy in getattr(player, "looted_hoards", ())),
        "leanings": {str(k): int(v) for k, v in player.leanings.items()},
        # **What a wave gave this hero** (s83). Wave modes have no save,
        # so these only ever travel over a socket - but they travel
        # through the same door every other field does, and a mirrored
        # body whose buffs were dropped would show a friend the wrong
        # health bar.
        "granted": list(getattr(player, "granted", ())),
        "buffs": [[str(key), (-1 if until is None else int(until))]
                  for key, until in getattr(player, "buffs", ())],
        "points": player.points,
        # **Exact, not rounded** (s96). Two places could carry 3577.996
        # up to 3578.0 - a load a pixel lower than the save, which beside
        # a wall is a pixel inside it. JSON writes a float so it reads
        # back bit for bit; the file was only ever tidier for it.
        "x": float(player.x),
        "y": float(player.y),
        "attributes": dict(player.attributes),
        "equipment": {slot: (item.to_dict() if item else None)
                      for slot, item in player.equipment.items()},
        "inventory": [item.to_dict() for item in player.inventory],
        "consumables": {k: int(v) for k, v in player.consumables.items()},
        "tracker": {k: (round(v, 1) if isinstance(v, float) else int(v))
                    for k, v in player.stats_tracker.items()},
    }


def deserialize_player(data, version=SAVE_VERSION):
    """Rebuild a Player from untrusted JSON, field by field.

    `version` is what the file said it was, so an old save can be brought
    forward rather than refused. It defaults to current because most
    callers - tests, co-op payloads - are building from data this version
    produced a moment ago.
    """
    raw = sec.pick_known_keys(data, {
        "name", "level", "xp", "hp", "mp", "gold", "points", "x", "y",
        "attributes", "equipment", "inventory", "consumables", "tracker",
        "class_key", "relics", "ngplus", "beaten", "fallen", "chapter_seen",
        "loadout",
        "modifiers", "appearance",
        "materials", "debug_used", "leanings", "bag_bought", "hoards",
        "granted", "buffs"})

    class_key = sec.safe_choice(raw.get("class_key"),
                                tuple(classes_mod.CLASSES_BY_KEY), "warden")
    player = Player(sec.sanitize_name(raw.get("name"), "Wanderer"), class_key)
    player.appearance = Appearance.from_dict(raw.get("appearance"))
    player.relics = [r.key for r in relics_mod.resolve(raw.get("relics"))]
    player.ngplus = sec.safe_int(raw.get("ngplus"), 0, 99, 0)
    # Missing on any save written before 2026-08-24, and False is the
    # honest answer for those: nothing was recording it, so nothing can
    # claim they were finished.
    player.beaten = sec.safe_bool(raw.get("beaten"), False)
    player.fallen = sec.safe_bool(raw.get("fallen"), False)
    player.chapter_seen = sec.safe_int(raw.get("chapter_seen"), 0, 99, 0)
    # Validated against the real ability list, not just coerced: a save
    # naming a skill that does not exist would put a hole in the bar.
    from .skills import SKILLS_BY_KEY as _SKILLS
    from . import spoils as spoils_mod
    player.loadout = [key for key in
                      (sec.safe_str(k, 32)
                       for k in sec.safe_list(raw.get("loadout"), 16))
                      if key in _SKILLS][:LOADOUT_SIZE]
    player.modifiers = RunModifiers.from_list(raw.get("modifiers"))
    # **Both validated against the real tables, never coerced** - s20's
    # rule, and the same one the loadout above follows: a payload naming
    # a skill that does not exist would put a hole in the skill bar, and
    # one naming a buff that does not exist would be a stat bonus with
    # no card behind it.
    player.granted = [key for key in
                      (sec.safe_str(k, 32)
                       for k in sec.safe_list(raw.get("granted"), 48))
                      if key in _SKILLS]
    player.buffs = []
    for pair in sec.safe_list(raw.get("buffs"), 64):
        # Length before `safe_list`, which caps rather than rejects.
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        key = sec.safe_str(pair[0], 32)
        if key not in spoils_mod.BUFFS_BY_KEY:
            continue
        # **The table decides how long a buff lasts, not the wire.**
        # A payload could otherwise say a one-wave Fury is permanent,
        # and the length is not information the sender has - it is a
        # property of the buff, which both machines already have.
        buff = spoils_mod.BUFFS_BY_KEY[key]
        if buff.permanent:
            player.buffs.append((key, None))
            continue
        until = sec.safe_int(pair[1], 0, 100000, 0)
        player.buffs.append((key, until))
    player.level = sec.safe_int(raw.get("level"), 1, MAX_LEVEL, 1)
    player.xp = sec.safe_int(raw.get("xp"), 0, 10_000_000, 0)
    player.gold = sec.safe_int(raw.get("gold"), 0, 99_999_999, 0)
    player.materials = sec.safe_int(raw.get("materials"), 0, 999_999, 0)
    player.debug_used = sec.safe_bool(raw.get("debug_used"), False)
    player.bag_bought = sec.safe_int(raw.get("bag_bought"), 0, 20, 0)
    # **Coordinates, so they are refused rather than clamped** - s20's
    # rule. A clamped door is a different door, which here would mean a
    # hoard the player never took being marked as taken.
    player.looted_hoards = set()
    for pair in sec.safe_list(raw.get("hoards"), 64):
        # **Length before `safe_list`**, because `safe_list` caps rather
        # than rejects: [1, 2, 3] would arrive as the door (1, 2), which
        # is not a refused record but a different room marked as looted.
        # Same trap as `coop._safe_room` (s57) and the same fix.
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        spot = sec.safe_list(pair, 2)
        if len(spot) != 2:
            continue
        gx, gy = spot
        if all(isinstance(v, int) and not isinstance(v, bool)
               and 0 <= v < 4096 for v in (gx, gy)):
            player.looted_hoards.add((gx, gy))
    from .scenes import LEANINGS
    player.leanings = {}
    for key, value in sec.safe_dict(raw.get("leanings")).items():
        name = sec.safe_str(key, 24)
        if name in LEANINGS:
            player.leanings[name] = sec.safe_int(value, 0, 9999, 0)
    player.points = sec.safe_int(raw.get("points"), 0, 300, 0)
    player.x = sec.safe_float(raw.get("x"), 0, 100_000, 0.0)
    player.y = sec.safe_float(raw.get("y"), 0, 100_000, 0.0)

    attrs = sec.safe_dict(raw.get("attributes"))
    for key in ATTRIBUTES:
        player.attributes[key] = sec.safe_int(attrs.get(key), 0, 300, 0)

    if version < 5:
        # **Four attributes became nine.** A v4 save has points sitting in
        # bundles that no longer exist - its `might` bought crit damage
        # that now lives in `ferocity` - so carrying the numbers across
        # would silently delete half of what the player bought.
        #
        # So the points come back instead, and the hero is given exactly
        # what a fresh character of that level would have. It is the same
        # thing `refund_points` does for a paid respec, which is the point:
        # the migration is not a special case, it is the feature.
        for key in ATTRIBUTES:
            player.attributes[key] = 0
        player.points = POINTS_PER_LEVEL * (player.level - 1)
        player.hp = min(player.hp, player.max_hp())
        player.mp = min(player.mp, player.max_mp())

    equipment = sec.safe_dict(raw.get("equipment"))
    for slot in ("weapon", "armor", "accessory"):
        item = Item.from_dict(equipment.get(slot)) if equipment.get(slot) else None
        # An item filed under the wrong slot is dropped, not equipped.
        player.equipment[slot] = item if (item and item.slot == slot) else None

    player.inventory = []
    for entry in sec.safe_list(raw.get("inventory"), sec.MAX_INVENTORY):
        item = Item.from_dict(entry)
        if item:
            player.inventory.append(item)

    # **An older save keeps everything it was carrying.** Bags held
    # `MAX_INVENTORY` from the start until 2026-09-01, so a run made
    # before then can be holding more than a fresh bag does. Grandfather
    # it up rather than hand back a hero who cannot pick anything up and
    # has no idea why.
    #
    # **After the inventory is read, not before.** The first version sat
    # beside `bag_bought` at the top of this function, where the inventory
    # is still empty and the loop therefore never ran. A test caught it;
    # nothing else would have.
    while (len(player.inventory) > player.bag_size()
           and player.bag_size() < sec.MAX_INVENTORY):
        player.bag_bought += 1

    player.consumables = {}
    for key, value in sec.safe_dict(raw.get("consumables")).items():
        if key in CONSUMABLES:
            count = sec.safe_int(value, 0, 99, 0)
            if count:
                player.consumables[key] = count

    tracker = sec.safe_dict(raw.get("tracker"))
    for key in list(player.stats_tracker):
        if key == "playtime":
            player.stats_tracker[key] = sec.safe_float(
                tracker.get(key), 0, 10_000_000, 0.0)
        else:
            player.stats_tracker[key] = sec.safe_int(
                tracker.get(key), 0, 100_000_000, 0)

    # HP and MP are clamped last, once the derived maxima are knowable.
    player.hp = sec.safe_int(raw.get("hp"), 0, player.max_hp(), player.max_hp())
    player.mp = sec.safe_int(raw.get("mp"), 0, player.max_mp(), player.max_mp())
    if player.hp <= 0:
        player.hp = max(1, player.max_hp() // 2)
    return player


def serialize_world(world):
    return {
        "seed": world.seed if world.seed is not None else -1,
        "hollow_key": bool(getattr(world, "hollow_key", False)),
        "story_flags": sorted(getattr(world, "story_flags", set())),
        "portals": [{"key": p.boss_key, "unlocked": p.unlocked,
                     "defeated": p.defeated} for p in world.portals],
        "chests": [{"gx": c.gx, "gy": c.gy, "opened": c.opened}
                   for c in world.chests],
        # Field bosses: where, which variant lives there, and when it wakes
        # up. **Placement regenerates from the seed** like everything else
        # (hard rule 4); what is stored is only what has happened to them.
        "favours": [{"gx": gx, "gy": gy}
                    for (gx, gy), entry in
                    getattr(world, "favours", {}).items() if entry[1]],
        # Waysides, spent ones only (item 12). Same shape as favours and
        # for the same reason: placement comes back from the seed, and
        # what a save carries is only **what you did**.
        "waysides": [{"gx": gx, "gy": gy}
                     for (gx, gy), entry in
                     getattr(world, "waysides", {}).items() if entry[1]],
        "stepstones": [{"gx": gx, "gy": gy}
                     for (gx, gy), entry in
                     getattr(world, "stepstones", {}).items() if entry[1]],
        "towers": [{"gx": gx, "gy": gy, "climbed": bool(done)}
                   for (gx, gy), done in getattr(world, "towers", {}).items()
                   if done],
        "lairs": [{"gx": gx, "gy": gy, "variant": entry[1],
                   "rest": round(float(entry[2]), 1)}
                  for (gx, gy), entry in getattr(world, "lairs", {}).items()],
    }


def apply_world(world, data):
    """Restore portal and chest state onto a freshly generated world.

    The terrain itself is regenerated from the seed rather than stored, so a
    save file stays small and cannot inject arbitrary map data.
    """
    raw = sec.pick_known_keys(data, {"seed", "portals", "chests",
                                     "hollow_key", "story_flags", "lairs",
                                     "towers", "stepstones", "waygates",
                                     "favours", "waysides"})
    world.hollow_key = sec.safe_bool(raw.get("hollow_key"), False)
    world.story_flags = set()
    for flag in sec.safe_list(raw.get("story_flags"), 64):
        name = sec.safe_str(flag, 48)
        if name:
            world.story_flags.add(name)

    states = {}
    for entry in sec.safe_list(raw.get("portals"), 16):
        entry = sec.safe_dict(entry)
        key = sec.safe_str(entry.get("key"), 32)
        if key:
            states[key] = (sec.safe_bool(entry.get("unlocked")),
                           sec.safe_bool(entry.get("defeated")))
    for portal in world.portals:
        if portal.boss_key in states:
            portal.unlocked, portal.defeated = states[portal.boss_key]
    if world.portals and not any(p.unlocked for p in world.portals):
        world.portals[0].unlocked = True     # never soft-lock the run

    # Stepstones. Only the open ones are stored, and - as everywhere else
    # here - a stored coordinate looks one up rather than creating it.
    # `waygates` was the name for about an hour on 2026-08-29. Reading it
    # costs one expression and means a save written in that hour does not
    # quietly forget which stones the player had earned.
    for entry in sec.safe_list(raw.get("stepstones")
                               or raw.get("waygates"), 32):
        entry = sec.safe_dict(entry)
        spot = (sec.safe_int(entry.get("gx"), 0, 4096, -1),
                sec.safe_int(entry.get("gy"), 0, 4096, -1))
        if spot in world.stepstones:
            world.stepstones[spot][1] = True

    # Favours, settled ones only.
    for entry in sec.safe_list(raw.get("favours"), 32):
        entry = sec.safe_dict(entry)
        spot = (sec.safe_int(entry.get("gx"), 0, 4096, -1),
                sec.safe_int(entry.get("gy"), 0, 4096, -1))
        if spot in world.favours:
            world.favours[spot][1] = True

    # Waysides, spent ones only. **A coordinate off a save looks one up
    # and never creates one** (§20) - so a file claiming a wayside at a
    # tile the seed did not put one on is ignored rather than clamped
    # onto the nearest real one.
    for entry in sec.safe_list(raw.get("waysides"), 32):
        entry = sec.safe_dict(entry)
        spot = (sec.safe_int(entry.get("gx"), 0, 4096, -1),
                sec.safe_int(entry.get("gy"), 0, 4096, -1))
        if spot in world.waysides:
            world.waysides[spot][1] = True

    # Towers. Only the climbed ones are stored, and a stored coordinate
    # is a key for looking one up - a save cannot put a tower somewhere
    # the seed did not.
    for entry in sec.safe_list(raw.get("towers"), 32):
        entry = sec.safe_dict(entry)
        spot = (sec.safe_int(entry.get("gx"), 0, 4096, -1),
                sec.safe_int(entry.get("gy"), 0, 4096, -1))
        if spot in world.towers:
            world.towers[spot] = sec.safe_bool(entry.get("climbed"), False)

    # Field bosses. **The lair positions come from the seed, not the
    # file** - a stored coordinate is only a key for looking one up, so a
    # save that names a tile with no lair on it is ignored rather than
    # creating one. Same rule as the guest vault: the world is the world.
    for entry in sec.safe_list(raw.get("lairs"), 32):
        entry = sec.safe_dict(entry)
        spot = (sec.safe_int(entry.get("gx"), 0, 4096, -1),
                sec.safe_int(entry.get("gy"), 0, 4096, -1))
        held = world.lairs.get(spot)
        if not held:
            continue
        variant = sec.safe_str(entry.get("variant"), 16)
        if variant:
            held[1] = variant
        held[2] = sec.safe_float(entry.get("rest"), 0, 1e7, 0.0)

    opened = set()
    for entry in sec.safe_list(raw.get("chests"), 256):
        entry = sec.safe_dict(entry)
        opened.add((sec.safe_int(entry.get("gx"), 0, 9999, -1),
                    sec.safe_int(entry.get("gy"), 0, 9999, -1)))
    for chest in world.chests:
        if (chest.gx, chest.gy) in opened:
            chest.opened = True
            world.props[(chest.gx, chest.gy)] = "chest_open"
    return world


def build_payload(player, world, quests, settings, playtime=0.0, created=None):
    payload = {
        "version": SAVE_VERSION,
        "created": created or time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "playtime": round(float(playtime), 1),
        "player": serialize_player(player),
        "world": serialize_world(world),
        "quests": quests.to_dict(),
        "settings": settings.to_dict(),
    }
    payload[CHECKSUM_KEY] = compute_checksum(payload)
    return payload


class SaveManager:
    """Owns the save slots, wherever they are kept.

    Takes a `store.Store` or, as every existing caller does, a directory -
    which is wrapped in a `FileStore` and behaves exactly as it always did,
    down to the filenames. Nothing in here knows whether a slot is a file or
    a key in a browser; what it knows is that a slot has a number, a backup,
    a checksum and a version.
    """

    def __init__(self, directory):
        self.store = store_mod.open_store(directory)
        self.last_error = ""
        self.last_warning = ""

    @property
    def dir(self):
        """The directory, for callers that still want to show a path.

        None when saves are not in a filesystem at all, which is the honest
        answer rather than a plausible-looking lie.
        """
        return getattr(self.store, "dir", None)

    @property
    def where(self):
        """One line describing where saves go, for the player."""
        return self.store.description

    def ensure_dir(self):
        if self.store.prepare():
            return True
        self.last_error = "cannot open save storage (%s)" % self.store.description
        return False

    def slot_key(self, slot):
        """Slot numbers are ints, so the key cannot be steered by input."""
        return "slot%d.json" % sec.safe_int(slot, 1, SLOT_COUNT, 1)

    def backup_key(self, slot):
        return "slot%d.bak" % sec.safe_int(slot, 1, SLOT_COUNT, 1)

    def slot_path(self, slot):
        """The slot as a path. None unless saves are in a filesystem."""
        base = self.dir
        return None if base is None else base / self.slot_key(slot)

    def backup_path(self, slot):
        base = self.dir
        return None if base is None else base / self.backup_key(slot)

    # -- write -------------------------------------------------------------

    def write(self, slot, payload, force=False):
        """Atomically write one slot. Returns True on success."""
        if not force and not _write_cooldown.trigger():
            self.last_error = "save throttled, try again in a moment"
            return False
        if not self.ensure_dir():
            return False

        key = self.slot_key(slot)
        try:
            text = sec.encode_json_limited(payload, sec.MAX_SAVE_BYTES)
        except sec.ValidationError as exc:
            self.last_error = str(exc)
            return False

        # Roll the old save into the backup *first*. On disk that is a
        # rename and costs nothing; in a browser it is a copy, and it is the
        # entire safety net there, because `localStorage` has no rename to
        # fall back on if a write goes wrong halfway.
        if self.store.exists(key):
            try:
                self.store.rename(key, self.backup_key(slot))
            except Exception:                             # noqa: BLE001
                pass          # a missing backup must never block a save
        try:
            self.store.write_text(key, text, sec.MAX_SAVE_BYTES)
            return True
        except (sec.ValidationError, store_mod.StoreError) as exc:
            self.last_error = "save failed: %s" % exc
            return False

    # -- read --------------------------------------------------------------

    def _read_slot(self, key, slot):
        """Parse one stored slot with size, structure and version checks."""
        data = self.store.read_json(key)
        version = sec.safe_int(data.get("version"), 0, 999, 0)
        if version <= 0 or version > SAVE_VERSION:
            raise sec.ValidationError("unsupported save version %s" % version)
        expected = data.get(CHECKSUM_KEY)
        if isinstance(expected, str) and expected:
            if compute_checksum(data) != expected:
                self.last_warning = (
                    "slot %s checksum mismatch: the file was edited or is "
                    "corrupt. Loading it anyway, with every value validated."
                    % slot)
        else:
            self.last_warning = "slot %s has no checksum (older save)" % slot
        return data, version

    def read(self, slot):
        """Load one slot, falling back to its backup. Returns a dict or None.

        A decode failure consumes rate-limit budget: five bad attempts inside
        fifteen minutes and the slot stops being re-parsed until the window
        rolls over. That bounds how fast a malformed or crafted payload can be
        retried against the validator.
        """
        self.last_error = ""
        self.last_warning = ""
        key = "slot%s" % sec.safe_int(slot, 1, SLOT_COUNT, 1)

        if not _decode_limiter.check(key):
            wait = int(_decode_limiter.retry_after(key))
            self.last_error = ("too many failed load attempts; retry in %d:%02d"
                               % (wait // 60, wait % 60))
            return None

        for stored, label in ((self.slot_key(slot), "save"),
                              (self.backup_key(slot), "backup")):
            if not self.store.exists(stored):
                continue
            try:
                data, _version = self._read_slot(stored, slot)
            except (sec.ValidationError, store_mod.StoreError) as exc:
                _decode_limiter.hit(key)
                self.last_error = "%s rejected: %s" % (label, exc)
                continue
            if label == "backup":
                self.last_warning = ("primary save was unreadable; recovered "
                                     "from backup")
            return data
        if not self.last_error:
            self.last_error = "no save in this slot"
        return None

    def load_into(self, slot, world_factory, settings):
        """Full load. Returns (player, world, quests, meta) or None."""
        data = self.read(slot)
        if data is None:
            return None
        # Read off the payload rather than threaded through `read`,
        # which deliberately returns only the data - the version is
        # part of the document and `_read_slot` has already checked it.
        version = sec.safe_int(data.get("version"), 0, 999, SAVE_VERSION)
        player = deserialize_player(sec.safe_dict(data.get("player")),
                                    version)
        world_data = sec.safe_dict(data.get("world"))
        seed = sec.safe_int(world_data.get("seed"), -1, 2 ** 31 - 1, -1)
        world = world_factory(None if seed < 0 else seed)
        apply_world(world, world_data)
        quests = QuestLog().apply_dict(sec.safe_dict(data.get("quests")))
        settings.apply_dict(sec.safe_dict(data.get("settings")))
        meta = {
            "playtime": sec.safe_float(data.get("playtime"), 0, 1e7, 0.0),
            "created": sec.safe_str(data.get("created"), 32),
            "updated": sec.safe_str(data.get("updated"), 32),
            "warning": self.last_warning,
        }
        # A save from before the player ever moved would drop them at (0, 0),
        # which is inside the border wall.
        if player.x <= 0 and player.y <= 0:
            player.x = world.spawn[0] * world.tile
            player.y = world.spawn[1] * world.tile
        return player, world, quests, meta

    # -- listing -----------------------------------------------------------

    def describe(self, slot):
        """Header info for the slot picker, without loading the whole run."""
        stored = self.slot_key(slot)
        if not self.store.exists(stored):
            return {"slot": slot, "empty": True}
        try:
            data = self.store.read_json(stored)
        except (sec.ValidationError, store_mod.StoreError) as exc:
            return {"slot": slot, "empty": False, "corrupt": True,
                    "error": str(exc)[:60]}
        player = sec.safe_dict(data.get("player"))
        world = sec.safe_dict(data.get("world"))
        beaten = sum(1 for p in sec.safe_list(world.get("portals"), 16)
                     if sec.safe_bool(sec.safe_dict(p).get("defeated")))
        return {
            "slot": slot,
            "empty": False,
            "corrupt": False,
            "class_key": sec.safe_choice(player.get("class_key"),
                                         tuple(classes_mod.CLASSES_BY_KEY),
                                         "warden"),
            "ngplus": sec.safe_int(player.get("ngplus"), 0, 99, 0),
            # Whether this run has been finished. Per-save (s19 decision 5),
            # which `meta.endings` is not - that is global, and would call
            # every slot finished the moment one of them was. Absent on
            # older saves, where False is the only honest answer.
            "beaten": sec.safe_bool(player.get("beaten"), False),
            # **Whether this hero fell under Mortal** (s54). The run is
            # over and the slot is kept, so the picker has to say so -
            # a row you can select and not load is the "row you cannot
            # press" that s45 is about.
            "fallen": sec.safe_bool(player.get("fallen"), False),
            # **Whether the debug menu was opened in this run** (s16). The
            # picker shows it, because the one thing a marked run changes
            # is what it can never do - write to the codex, the records or
            # the endings - and finding that out after loading it is
            # finding it out too late.
            "marked": sec.safe_bool(player.get("debug_used"), False),
            "name": sec.sanitize_name(player.get("name"), "Wanderer"),
            "level": sec.safe_int(player.get("level"), 1, MAX_LEVEL, 1),
            "gold": sec.safe_int(player.get("gold"), 0, 99_999_999, 0),
            "bosses": beaten,
            "playtime": sec.safe_float(data.get("playtime"), 0, 1e7, 0.0),
            "updated": sec.safe_str(data.get("updated"), 32),
        }

    def describe_all(self):
        return [self.describe(i) for i in range(1, SLOT_COUNT + 1)]

    def delete(self, slot):
        removed = False
        for stored in (self.slot_key(slot), self.backup_key(slot)):
            try:
                removed = self.store.delete(stored) or removed
            except (store_mod.StoreError, OSError) as exc:
                self.last_error = "delete failed: %s" % exc
        _decode_limiter.reset("slot%s" % sec.safe_int(slot, 1, SLOT_COUNT, 1))
        return removed


# How many host-worlds we remember a hero for. A guest hero is not one of
# the save slots - it is keyed by somebody else's seed and there is no upper
# bound on how many rooms a player visits, so the store has to be capped or
# it grows without limit. Twelve is a number, not a measurement: enough that
# a regular group never loses a hero, small enough that the file set stays
# comprehensible. The eviction is least-recently-played.
GUEST_LIMIT = 12
GUEST_PREFIX = "guest"


class GuestVault:
    """Heroes that belong to *somebody else's* world, keyed by its seed.

    Savvydev, 2026-08-24: *"a guest hero does persist... but if the host
    switches worlds, then the guest hero resets unless the guest has a hero
    for that world too."* So a hero is not yours, it is yours-in-that-valley:
    the first visit is level one, and returning to the same seed finds the
    hero you left there.

    **Deliberately not one of the save slots.** A slot is a run you own and
    chose; these are keyed by other people's seeds, there may be many, and
    losing the oldest one to the cap must never cost somebody a save they
    picked. Same store, same checksum, same validators - a different shelf.

    **The world travels with the hero, minus the terrain.** Story flags and
    warden kills are private per player in co-op (s20) precisely so a second
    player is not robbed of a moment - which only holds across sessions if
    they persist. The terrain is still one integer (hard rule 4), so what is
    kept is what has *happened* to that valley for this guest, not the
    valley itself.
    """

    def __init__(self, directory):
        self.store = store_mod.open_store(directory)
        self.last_error = ""
        self.last_warning = ""

    # -- keys --------------------------------------------------------------

    def key_for(self, seed):
        """A seed becomes a key, never a caller's string.

        The seed arrives in a `welcome` off a socket, and everything off a
        socket is save data (s18) - so it is coerced to an int the same way a
        slot number is. A key that cannot be steered by input cannot be a
        traversal, whatever the peer sends.
        """
        return "%s%d.json" % (GUEST_PREFIX,
                              sec.safe_int(seed, 0, 2 ** 31 - 1, 0))

    def has(self, seed):
        return self.store.exists(self.key_for(seed))

    def seeds(self):
        """Every seed we hold a hero for. Unordered."""
        out = []
        for key in self.store.keys():
            name = str(key)
            if not name.startswith(GUEST_PREFIX) or not name.endswith(".json"):
                continue
            digits = name[len(GUEST_PREFIX):-len(".json")]
            if digits.isdigit():
                out.append(int(digits))
        return out

    # -- write -------------------------------------------------------------

    def remember(self, seed, player, world, quests):
        """Leave this hero in that valley. Returns True on success."""
        if not self.store.prepare():
            self.last_error = ("cannot open save storage (%s)"
                               % self.store.description)
            return False
        payload = {
            "version": SAVE_VERSION,
            "seed": sec.safe_int(seed, 0, 2 ** 31 - 1, 0),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "player": serialize_player(player),
            "world": serialize_world(world),
            "quests": quests.to_dict(),
        }
        payload[CHECKSUM_KEY] = compute_checksum(payload)
        try:
            text = sec.encode_json_limited(payload, sec.MAX_SAVE_BYTES)
        except sec.ValidationError as exc:
            self.last_error = str(exc)
            return False
        key = self.key_for(seed)
        # Make room *before* writing, and never count the key we are about to
        # overwrite - returning to a valley you already have a hero in must
        # not evict somebody else to make room for a hero already there.
        self._evict_for(key)
        try:
            self.store.write_text(key, text, sec.MAX_SAVE_BYTES)
            return True
        except (sec.ValidationError, store_mod.StoreError) as exc:
            self.last_error = "could not keep that hero: %s" % exc
            return False

    def _evict_for(self, key):
        """Drop least-recently-played heroes until `key` will fit."""
        held = [self.key_for(seed) for seed in self.seeds()]
        if key in held:
            return
        while len(held) >= GUEST_LIMIT:
            oldest = min(held, key=self._played_at)
            held.remove(oldest)
            try:
                self.store.delete(oldest)
            except (store_mod.StoreError, OSError):
                return        # cannot tidy: better a big vault than no save

    def _played_at(self, key):
        """The `updated` stamp, or empty.

        Empty sorts first, so a payload we cannot read is the first thing
        evicted rather than the last.
        """
        try:
            data = self.store.read_json(key)
        except (sec.ValidationError, store_mod.StoreError):
            return ""
        return sec.safe_str(data.get("updated"), 32)

    # -- read --------------------------------------------------------------

    def recall(self, seed, world_factory, tile=None):
        """The hero left in that valley, or None: `(player, world, quests)`.

        None is not an error - it is the ordinary answer the first time a
        guest steps into a world, and the caller makes a level-one hero.
        """
        self.last_error = ""
        self.last_warning = ""
        key = self.key_for(seed)
        if not self.store.exists(key):
            return None
        if not _decode_limiter.check(key):
            wait = int(_decode_limiter.retry_after(key))
            self.last_error = ("too many failed load attempts; retry in "
                               "%d:%02d" % (wait // 60, wait % 60))
            return None
        try:
            data = self.store.read_json(key)
        except (sec.ValidationError, store_mod.StoreError) as exc:
            _decode_limiter.hit(key)
            self.last_error = "that hero could not be read: %s" % exc
            return None
        version = sec.safe_int(data.get("version"), 0, 999, 0)
        if version <= 0 or version > SAVE_VERSION:
            _decode_limiter.hit(key)
            self.last_error = "unsupported save version %s" % version
            return None
        expected = data.get(CHECKSUM_KEY)
        if isinstance(expected, str) and expected:
            if compute_checksum(data) != expected:
                self.last_warning = ("that hero's file was edited or is "
                                     "corrupt. Loading it anyway, with every "
                                     "value validated.")
        player = deserialize_player(sec.safe_dict(data.get("player")), version)
        world_data = sec.safe_dict(data.get("world"))
        # **The seed is the host's, not the file's.** A stored seed only
        # records where the hero was left; the world being stepped into is
        # whichever one the host is running now. Trusting the file here would
        # let an edited one rebuild a different valley under everyone else.
        world = world_factory(sec.safe_int(seed, 0, 2 ** 31 - 1, 0))
        apply_world(world, world_data)
        quests = QuestLog().apply_dict(sec.safe_dict(data.get("quests")))
        if player.x <= 0 and player.y <= 0:
            step = tile or getattr(world, "tile", 32)
            player.x = world.spawn[0] * step
            player.y = world.spawn[1] * step
        return player, world, quests

    def forget(self, seed):
        try:
            return self.store.delete(self.key_for(seed))
        except (store_mod.StoreError, OSError) as exc:
            self.last_error = "delete failed: %s" % exc
            return False


def format_playtime(seconds):
    seconds = int(max(0, seconds))
    return "%d:%02d:%02d" % (seconds // 3600, (seconds // 60) % 60, seconds % 60)
