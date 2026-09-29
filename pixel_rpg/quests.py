"""Quests and achievements.

Quests are handed out by the elder in town and tracked against the same
counters the character sheet displays, so nothing has to be bookkept twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import security as sec


@dataclass(frozen=True)
class QuestDef:
    key: str
    title: str
    detail: str
    metric: str          # a key in Player.stats_tracker, or "level"
    target: int
    gold: int
    xp: int
    item_ilvl: int = 0
    requires: str = ""   # quest that must be complete first
    # Counter goals ("defeat 10 monsters") measure what you do after
    # accepting. Absolute goals ("reach level 20") measure where you are.
    absolute: bool = False


QUEST_DEFS = [
    QuestDef("cull", "Thin the Herd",
             "Defeat 10 monsters in the wild.", "kills", 10, 120, 90),
    QuestDef("scavenger", "Scavenger",
             "Open 5 treasure chests.", "chests", 5, 150, 110, 6, "cull"),
    QuestDef("warden1", "The First Warden",
             "Defeat any boss.", "bosses", 1, 260, 240, 8, "cull"),
    QuestDef("veteran", "Blooded",
             "Defeat 40 monsters.", "kills", 40, 380, 420, 12, "warden1"),
    QuestDef("striker", "Precision",
             "Land 25 critical hits.", "crits", 25, 420, 460, 14, "warden1"),
    QuestDef("wanderer", "Long Roads",
             "Travel 4000 steps.", "steps", 4000, 300, 380, 10, "cull"),
    QuestDef("warden3", "Three Crowns",
             "Defeat three bosses.", "bosses", 3, 900, 1200, 20, "warden1"),
    QuestDef("hoard", "Deep Pockets",
             "Earn 5000 gold across the run.", "gold_earned", 5000,
             800, 900, 18, "scavenger"),
    QuestDef("ascend", "Ascendant",
             "Reach level 20.", "level", 20, 1200, 0, 24, "warden3",
             absolute=True),
    QuestDef("warden6", "The Last Door",
             "Defeat all six wardens.", "bosses", 6, 4000, 6000, 32, "warden3"),
]

QUESTS_BY_KEY = {q.key: q for q in QUEST_DEFS}


# ---------------------------------------------------------------- errands ---
#
# **Sidequests, as distinct from favours** (s42). Savvydev, 2026-08-29:
# *"there should be npcs that give favors AND npcs that give you
# sidequests"*.
#
# A favour is answered where you stand. An errand is a thing you go away
# and do and come back from - which is a quest in every mechanical sense,
# so it reuses `QuestDef` outright rather than growing a parallel idea of
# what a task is.
#
# **They are a second track with their own small cap, and that is the
# whole design question.** `MAX_ACTIVE` is 1 because Maren hands out work
# in order and a stack of half-finished jobs is a chore list, not a quest
# log - that reasoning is Savvydev's and it still holds. But it is reasoning
# about *Maren's* work; a stranger on the road asking you to do something
# is a different relationship.
#
# **You may carry several**, confirmed by Savvydev on 2026-08-29: *"you can
# have multiple sidequests accepted at once btw"*. The cap of six is not a
# design statement, it is a guard against a list nobody reads - raise it
# freely.
ERRAND_DEFS = [
    QuestDef("errand_wolves", "Something in the Grass",
             "Defeat 6 monsters out in the wild.", "kills", 6, 90, 80),
    QuestDef("errand_chests", "What the Road Left",
             "Open 2 chests.", "chests", 2, 110, 70, 4),
    QuestDef("errand_walk", "As Far As the Marker",
             "Walk 900 steps.", "steps", 900, 80, 90),
    QuestDef("errand_crits", "A Clean Strike",
             "Land 8 critical hits.", "crits", 8, 130, 120, 5),
    QuestDef("errand_gold", "The Cost of Things",
             "Earn 600 gold on the road.", "gold_earned", 600, 0, 220, 6),
    QuestDef("errand_hunt", "The Thing on the Hill",
             "Defeat 15 monsters.", "kills", 15, 200, 210, 8),
    QuestDef("errand_deep", "Further Than That",
             "Defeat 30 monsters.", "kills", 30, 340, 360, 10),
    QuestDef("errand_purse", "A Purse of Her Own",
             "Earn 2000 gold on the road.", "gold_earned", 2000, 0, 520, 14),
]

ERRANDS_BY_KEY = {q.key: q for q in ERRAND_DEFS}
TASKS_BY_KEY = dict(QUESTS_BY_KEY)
TASKS_BY_KEY.update(ERRANDS_BY_KEY)

# See the note above: the cap exists so a player is never carrying a list
# they have stopped reading, not to limit how many they may hold.
MAX_ERRANDS = 6

# You carry one job at a time. Maren hands out work in order, and a stack of
# half-finished errands is not a quest log, it is a chore list.
MAX_ACTIVE = 1


@dataclass(frozen=True)
class AchievementDef:
    key: str
    title: str
    detail: str
    metric: str
    target: int
    # A hidden feat shows as "???" until it is earned. Naming it in the list
    # would be the same as telling somebody how to get it, which for this one
    # is the entire point of it.
    hidden: bool = False

    def shown_title(self, unlocked):
        return self.title if unlocked or not self.hidden else "???"

    def shown_detail(self, unlocked):
        return self.detail if unlocked or not self.hidden else "???"


ACHIEVEMENTS = [
    AchievementDef("first_blood", "First Blood", "Defeat one monster.",
                   "kills", 1),
    AchievementDef("centurion", "Centurion", "Defeat 100 monsters.",
                   "kills", 100),
    AchievementDef("boss_slayer", "Warden Slayer", "Defeat one boss.",
                   "bosses", 1),
    AchievementDef("completionist", "Crown of Wardens",
                   "Defeat all six bosses.", "bosses", 6),
    AchievementDef("treasure", "Tomb Raider", "Open 20 chests.", "chests", 20),
    AchievementDef("rich", "Coin Baron", "Earn 10000 gold.",
                   "gold_earned", 10000),
    AchievementDef("marathon", "Pathfinder", "Travel 10000 steps.",
                   "steps", 10000),
    AchievementDef("crit", "Surgeon", "Land 100 critical hits.", "crits", 100),
    AchievementDef("survivor", "Unbroken", "Reach level 25.", "level", 25),
    AchievementDef("heavy", "Heavy Hitter", "Deal 100000 total damage.",
                   "damage", 100000),
    AchievementDef("curious", "Curiosity",
                   "Find the door that was not meant for you.",
                   "secrets", 1, hidden=True),

    # **Twelve more** (2026-09-01), because *"more achievements is a
    # MUST"*. Three rules held while writing them:
    #
    # 1. **Every one names a number you can check on the hero sheet.**
    #    A feat you cannot tell you are near is a surprise, not a goal.
    # 2. **They cover what the game actually is now**, not just combat:
    #    the island got towers, favours and errands in s42 and s44, and
    #    nothing was counting any of them.
    # 3. **The high ones are for people who want them.** 100000 damage
    #    was the old ceiling; a run that reaches level 100 is not served
    #    by a list that stops at 25.
    AchievementDef("butcher", "Butcher", "Defeat 500 monsters.",
                   "kills", 500),
    AchievementDef("plunderer", "Plunderer", "Open 50 chests.",
                   "chests", 50),
    AchievementDef("magnate", "Magnate", "Earn 100000 gold.",
                   "gold_earned", 100000),
    AchievementDef("far_walker", "Far Walker", "Travel 50000 steps.",
                   "steps", 50000),
    AchievementDef("duellist", "Duellist", "Land 500 critical hits.",
                   "crits", 500),
    AchievementDef("veteran", "Veteran", "Reach level 50.", "level", 50),
    AchievementDef("centenary", "The Hundredth", "Reach level 100.",
                   "level", 100),
    AchievementDef("devastation", "Devastation",
                   "Deal 1000000 total damage.", "damage", 1000000),
    # **A feat for losing**, which every list of these should have: it is
    # the one that says the game expects you to fail and does not mind.
    AchievementDef("persistent", "Get Up", "Fall in battle 10 times.",
                   "deaths", 10),
    AchievementDef("neighbourly", "Neighbourly",
                   "Do 5 favours for people on the road.", "favours", 5),
    AchievementDef("errand_runner", "Errand Runner",
                   "Finish 5 errands.", "errands", 5),
    AchievementDef("surveyor", "Surveyor", "Climb 5 survey towers.",
                   "towers", 5),
]

ACHIEVEMENTS_BY_KEY = {a.key: a for a in ACHIEVEMENTS}


class QuestLog:
    """Tracks accepted, completed and claimable quests plus achievements."""

    def __init__(self):
        self.active = []          # quest keys
        self.completed = []       # quest keys already turned in
        self.unlocked = []        # achievement keys
        # Counter value when each quest was accepted, so work done before
        # taking it on does not count toward it.
        self.baselines = {}
        # Errands run alongside the quest rather than competing with it.
        self.errands = []

    # -- helpers -----------------------------------------------------------

    def _metric(self, player, name):
        if name == "level":
            return player.level
        return int(player.stats_tracker.get(name, 0))

    def available(self, player):
        """Quests that can be accepted right now."""
        out = []
        for quest in QUEST_DEFS:
            if quest.key in self.active or quest.key in self.completed:
                continue
            if quest.requires and quest.requires not in self.completed:
                continue
            out.append(quest)
        return out

    def full(self):
        return len(self.active) >= MAX_ACTIVE

    # -- errands -----------------------------------------------------------

    def errands_full(self):
        return len(self.errands) >= MAX_ERRANDS

    def accept_errand(self, key, player=None):
        """Take on an errand. Same bookkeeping, separate list."""
        errand = ERRANDS_BY_KEY.get(key)
        if not errand or key in self.errands or key in self.completed:
            return False
        if self.errands_full():
            return False
        self.errands.append(key)
        if player is not None and not errand.absolute:
            self.baselines[key] = self._metric(player, errand.metric)
        return True

    def claim_errand(self, player, key):
        """Hand one in. Returns (gold, xp, item_ilvl) or None."""
        errand = ERRANDS_BY_KEY.get(key)
        if not errand or key not in self.errands:
            return None
        done, target = self.progress(player, key)
        if done < target:
            return None
        self.errands.remove(key)
        self.completed.append(key)
        self.baselines.pop(key, None)
        return errand.gold, errand.xp, errand.item_ilvl

    def accept(self, key, player=None):
        """Take on a quest, snapshotting the counter it measures."""
        quest = QUESTS_BY_KEY.get(key)
        if not quest or key in self.active or key in self.completed:
            return False
        if self.full():
            return False
        self.active.append(key)
        if player is not None and not quest.absolute:
            self.baselines[key] = self._metric(player, quest.metric)
        return True

    def progress(self, player, key):
        """Work done since accepting, for counter goals."""
        quest = TASKS_BY_KEY.get(key)
        if not quest:
            return 0, 1
        value = self._metric(player, quest.metric)
        if not quest.absolute:
            value -= int(self.baselines.get(key, 0))
        return min(max(0, value), quest.target), quest.target

    def ready_to_claim(self, player):
        out = []
        for key in self.active:
            done, target = self.progress(player, key)
            if done >= target:
                out.append(QUESTS_BY_KEY[key])
        return out

    def claim(self, player, key):
        """Hand in a finished quest. Returns (gold, xp, item_ilvl) or None."""
        quest = QUESTS_BY_KEY.get(key)
        if not quest or key not in self.active:
            return None
        done, target = self.progress(player, key)
        if done < target:
            return None
        self.active.remove(key)
        self.completed.append(key)
        self.baselines.pop(key, None)
        return quest.gold, quest.xp, quest.item_ilvl

    def check_achievements(self, player):
        """Feats newly unlocked by this run's counters.

        Deliberately per-save: every character earns the whole set again from
        nothing, which is what makes a clean-run 100% chase a thing you can
        do. The *global* list is a separate cabinet on the title screen -
        see `MetaProfile.achievements` - and this feeds it without being it.
        """
        fresh = []
        for ach in ACHIEVEMENTS:
            if ach.key in self.unlocked:
                continue
            if self._metric(player, ach.metric) >= ach.target:
                self.unlocked.append(ach.key)
                fresh.append(ach)
        return fresh

    # -- persistence -------------------------------------------------------

    def to_dict(self):
        return {"active": list(self.active), "completed": list(self.completed),
                "errands": list(self.errands),
                "unlocked": list(self.unlocked),
                "baselines": {k: int(v)
                              for k, v in self.baselines.items()}}

    def apply_dict(self, data):
        raw = sec.pick_known_keys(data, {"active", "completed", "errands",
                                         "unlocked", "baselines"})

        def keys_in(field, table):
            # Entries must be coerced to strings before being used as keys: a
            # list or dict from a doctored save is unhashable and `x in table`
            # would raise rather than simply reject it.
            out = []
            for entry in sec.safe_list(raw.get(field), 32):
                name = sec.safe_str(entry, 32)
                if name and name in table and name not in out:
                    out.append(name)
            return out

        self.active = keys_in("active", QUESTS_BY_KEY)
        self.errands = keys_in("errands", ERRANDS_BY_KEY)
        # **Both kinds finish into the same list**, so it is validated
        # against both - an errand handed in before this line existed
        # would otherwise vanish on load and be offerable again.
        self.completed = keys_in("completed", TASKS_BY_KEY)
        self.unlocked = keys_in("unlocked", ACHIEVEMENTS_BY_KEY)

        self.baselines = {}
        for key, value in sec.safe_dict(raw.get("baselines")).items():
            name = sec.safe_str(key, 32)
            if name in QUESTS_BY_KEY:
                self.baselines[name] = sec.safe_int(value, 0,
                                                    100000000, 0)
        # A quest cannot be active and completed at once.
        self.active = [k for k in self.active if k not in self.completed]
        del self.active[MAX_ACTIVE:]
        del self.errands[MAX_ERRANDS:]
        return self
