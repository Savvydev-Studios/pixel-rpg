"""Run modifiers.

Chosen at the start of a run and locked in for its duration. **Each one
takes something away. Nothing here hands anything back**, and the score
multiplier is the entire reward.

Stacking is allowed and encouraged. The combination is the replay hook.

**It was a trade until 2026-08-31** - *"something is taken away and
something is handed back"* - and Savvydev killed the second half after the
school playtest (s45):

> "remove all the buffs in the modifiers, my friend has all of them on and
> is one tapping everything"

He is describing the arithmetic exactly. Every one of the nine was a cost
*plus* a boon, the boons stacked, and several of the costs cancelled each
other out - Ironheart takes your consumables and Hollowed heals you on
every kill, so taking both removes a cost you were paid for. A player who
selected all nine walked out with +60% damage, +30% more damage, +20%
defence, two extra relics, doubled chests and a heal on every kill, in
exchange for restrictions that had mostly stopped applying.

**And Blind Road was a pure boon by accident.** Its `no_map` tag appeared
in `gold_mult` and `xp_mult` and nowhere else: nothing in the game ever
hid the minimap. That is Savvydev's *"the minimap remover modifier doesnt
work"*, and it is the strongest argument for this change - a modifier
whose cost silently does not exist is indistinguishable from a modifier
whose cost was deleted on purpose, and only one of those is a bug.

**The costs are all that is left, and they are unchanged.** The score
multipliers are unchanged too, which is deliberately conservative: they
now under-price the modifiers rather than over-price them, and moving a
number that decides what a run *scores* is a difficulty decision
(rule 31.7). Flagged for him rather than guessed at.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import security as sec


@dataclass(frozen=True)
class Modifier:
    key: str
    name: str
    cost: str            # what it takes away
    boon: str            # what it gives back
    score: float         # score multiplier while active
    color: tuple = (206, 206, 216)
    tags: tuple = ()
    # **Two kinds since 2026-09-06** (items 0aj/0ak). §45 settled that a
    # modifier only *takes*, and the score multiplier is the whole reward
    # - which is still true of every `cost` here.
    #
    # Savvydev then asked for two that are neither. His words: *"the two new
    # ideas can be added in as modifiers that dont give you an score boost
    # or anything"*. They do not make a run harder or easier; they change
    # **how the game is played**, and scoring one would be paying somebody
    # for choosing the game they wanted.
    #
    # So: a `cost` scores above 1.0, a `preference` scores exactly 1.0,
    # and a test holds each to its own rule rather than to one rule that
    # no longer fits both.
    kind: str = "cost"

    @property
    def scores(self):
        return self.kind == "cost"


# `boon` is what the run *scores*, not what the hero gets. The field kept
# its name because every screen that draws a modifier reads it, and a
# rename would have been a bigger change than the one being made.
MODIFIERS = [
    Modifier("ironheart", "Ironheart",
             "No consumables at all. None.",
             "Score x1.45", 1.45, (226, 96, 106),
             ("no_items",)),
    Modifier("glass", "Glass Road",
             "Half maximum health.",
             "Score x1.60", 1.60, (130, 214, 240), ("glass",)),
    Modifier("haunted", "Haunted",
             "Every enemy is three levels above the area.",
             "Score x1.50", 1.50, (168, 108, 226),
             ("haunted",)),
    Modifier("miser", "Closed Shutters",
             "The trader and the smith will not serve you.",
             "Score x1.35", 1.35, (206, 168, 40),
             ("no_shops",)),
    Modifier("blindroad", "Blind Road",
             "No minimap, no world map, no interaction prompts.",
             "Score x1.25", 1.25, (92, 96, 110), ("no_map",)),
    Modifier("brittle", "Brittle Steel",
             "All gear drops one rarity lower.",
             "Score x1.40", 1.40, (156, 162, 178),
             ("downgrade",)),
    Modifier("nightfall", "Long Nightfall",
             "Permanent night. Encounters are far more frequent.",
             "Score x1.30", 1.30, (38, 34, 66),
             ("always_night", "more_encounters")),
    Modifier("oathkeeper", "Oathkeeper",
             "You cannot flee any battle, ever.",
             "Score x1.35", 1.35, (252, 240, 180),
             ("no_flee",)),
    Modifier("hollowed", "Hollowed",
             "The shrine no longer heals you.",
             "Score x1.30", 1.30, (118, 220, 138),
             ("no_shrine",)),
]

# **Six more, designed with Savvydev on 2026-09-05** (s54). Two are his from
# s46 - the one-sitting run and the level-up restore - and four came out
# of the same conversation. Every one takes something and hands nothing
# back (s45), and every one is a rule bolted onto a system that already
# exists rather than a new system.
MODIFIERS += [
    # **The big one, and it is built inside out.** He described "a modifier
    # that deletes the save if you leave"; this never writes one. Same
    # experience, and the difference is that there is no code path here
    # whose job is destroying somebody's data - which s7 records this
    # project getting wrong once already, through a test suite that
    # deleted real runs and failed nothing while doing it.
    #
    # It also closes the exploit the literal version has: if leaving
    # deletes, then killing the process is the safe way to keep a run, and
    # the whole tension goes with it. With nothing on disk, quitting,
    # crashing and the power going out are one event.
    Modifier("onesitting", "One Sitting",
             "The run is never saved. Close the game for any reason and "
             "it is over.",
             "Score x2.00", 2.00, (240, 96, 140),
             ("no_save",)),
    # **Item 0ai** (2026-09-06, s67). Block Tales is the reference (s41)
    # and Savvydev asked for it as a modifier for single *and* multiplayer,
    # with a score boost.
    #
    # **It is a cost, and §45's rule survives.** A missed command does
    # 0.80x - less than the same swing without the modifier - so taking
    # this is agreeing to be worse at the game until you learn the timing,
    # which is exactly the shape every other entry here has. The reward
    # for learning it is 1.40x, and the multiplier is the reward for
    # taking it at all.
    Modifier("timed", "Timed Hands",
             "Every swing and every blow has a timing window. Press on the "
             "beat or hit softer than you would have.",
             "Score x1.35", 1.35, (250, 190, 90),
             ("timed",)),
    # -- preferences: no score, no difficulty, a different game ----------
    #
    # **Item 0aj** (2026-09-06). His words:
    #
    # > "instead of having enemies as random encounters when walking
    # > around, make them visible and if a player and an enemy interact
    # > then the player is put into battle. in the overworld, only one
    # > enemy is shown, but when battle begins there can be multiple."
    Modifier("insight", "Plain Sight",
             "Monsters walk the world instead of ambushing you. Walk into "
             "one and the fight starts.",
             "Score unchanged", 1.00, (140, 205, 160),
             ("seen",), kind="preference"),
    # **Item 0bi** (2026-09-07). His words:
    #
    # > "free-roam is available in all modes, and free-roam combat as
    # > well, and if free-roam combat modifier is turned on then
    # > free-roam is automatically turned on as well. for free-roam
    # > combat to exist free-roam MUST also exist"
    #
    # **Two things with similar names**: *free-roam* is Plain Sight
    # above - monsters that walk the world - and *free-roam combat* is
    # this, the real-time engine §76 built. This one **requires** that
    # one, and `RunModifiers` enforces it rather than the picker asking
    # nicely (see `REQUIRES`).
    #
    # A preference rather than a cost, like Plain Sight: it changes what
    # the game *is* rather than how hard it is, and pricing "a different
    # game" as difficulty is what §54 settled against.
    Modifier("openfight", "Open Combat",
             "Fights happen where you stand, in real time. Move, aim and "
             "dodge instead of taking turns.",
             "Score unchanged", 1.00, (232, 168, 96),
             ("free_combat",), kind="preference"),
    Modifier("unmended", "Unmended",
             "Levelling up no longer restores your health and magic.",
             "Score x1.20", 1.20, (150, 200, 210),
             ("no_levelup_heal",)),
    # One sitting is not one life. These are two modifiers on purpose and
    # they stack into a full permadeath run.
    Modifier("mortal", "Mortal",
             "Falling ends the run. There is no waking at the shrine.",
             "Score x1.80", 1.80, (196, 60, 60),
             ("mortal",)),
    Modifier("ironloadout", "Iron Loadout",
             "You fight with the first four abilities you learn. Always.",
             "Score x1.25", 1.25, (150, 120, 230),
             ("iron_loadout",)),
    Modifier("swornblade", "Sworn Blade",
             "The weapon you start with is the weapon you finish with.",
             "Score x1.30", 1.30, (226, 176, 96),
             ("sworn_blade",)),
    Modifier("smallhands", "Small Hands",
             "A bag of ten, and it can never be made bigger.",
             "Score x1.30", 1.30, (160, 190, 140),
             ("small_hands",)),
]

MODIFIERS_BY_KEY = {m.key: m for m in MODIFIERS}


#: **What a modifier cannot exist without** (item 0bi). His words about
#: the only entry so far: *"for free-roam combat to exist free-roam MUST
#: also exist"* - a dependency, not a default.
#:
#: Kept as data next to the modifiers rather than as a branch in the
#: picker, because there are three ways into a run's modifier set - the
#: picker, a save, and a co-op handshake - and a rule enforced in one of
#: them is a rule two thirds absent.
REQUIRES = {"openfight": ("insight",)}


def required_by(key):
    """Which modifiers `key` cannot be taken without. Never raises."""
    return REQUIRES.get(sec.safe_str(key, 32), ())


def requires_which(key):
    """Which modifiers force `key` on, so a picker can say so."""
    return tuple(sorted(owner for owner, needed in REQUIRES.items()
                        if key in needed))


class RunModifiers:
    """The set active for one run, plus the queries the game asks of it."""

    def __init__(self, keys=()):
        self.keys = []
        for key in keys:
            self.add(key)

    def add(self, key):
        key = sec.safe_str(key, 32)
        if key in MODIFIERS_BY_KEY and key not in self.keys:
            self.keys.append(key)
            # **Its dependencies come with it, always** (item 0bi). Here
            # rather than in the picker: a set arrives from a save and
            # from a co-op welcome as well, and either could otherwise
            # carry free-roam combat without the free-roam it cannot
            # exist without.
            for needed in required_by(key):
                self.add(needed)
            return True
        return False

    def locked(self, key):
        """Whether something else in this run is forcing `key` on.

        A picker uses this to draw the row as on and refuse to turn it
        off - his rule that a control which will be refused is not
        offered (§45), applied to the opposite case: one that cannot be
        taken away should not look as though it can.
        """
        return any(owner in self.keys for owner in requires_which(key))

    def toggle(self, key):
        if key in self.keys:
            if self.locked(key):
                return True
            self.keys.remove(key)
            # And anything that required it goes too, rather than being
            # left in a state it says is impossible.
            for owner in requires_which(key):
                if owner in self.keys:
                    self.keys.remove(owner)
            return False
        return self.add(key)

    def active(self):
        return [MODIFIERS_BY_KEY[k] for k in self.keys]

    def has(self, tag):
        return any(tag in MODIFIERS_BY_KEY[k].tags for k in self.keys)

    @property
    def score(self):
        total = 1.0
        for key in self.keys:
            total *= MODIFIERS_BY_KEY[key].score
        return round(total, 3)

    # -- what the game asks for --------------------------------------------
    #
    # **These all return 1.0 now, and they are kept rather than deleted.**
    # Every one is a cost that happens to be expressed as a multiplier, and
    # the callers - `Player.gold_multiplier`, `attack_multiplier`,
    # `defense`, `max_hp` - are the right place for a modifier to be able
    # to reach. Deleting the seam would mean rewriting four call sites to
    # take the seam away, and then rewriting them again to put a cost back.
    #
    # A cost that *is* a multiplier still lives here: `hp_mult` is Glass
    # Road, and it is the only one left.

    def gold_mult(self):
        return 1.0

    def xp_mult(self):
        return 1.0

    def damage_mult(self):
        return 1.0

    def defense_mult(self):
        return 1.0

    def hp_mult(self):
        return 0.50 if self.has("glass") else 1.0

    def enemy_level_bonus(self):
        return 3 if self.has("haunted") else 0

    def encounter_mult(self):
        return 1.75 if self.has("more_encounters") else 1.0

    # -- the six from s54 --------------------------------------------------

    def never_saves(self):
        """Whether this run may be written to a slot at all.

        Asked by `Game.save_run`, which is the single place every save in
        the game goes through - autosave, the pause menu, respawning, an
        ending and the quit path all call it. **One gate rather than six
        call sites each remembering**, which is the same argument
        `save_run`'s own docstring makes about the guest vault.
        """
        return self.has("no_save")

    def bag_cap(self, default):
        return 10 if self.has("small_hands") else default

    def to_list(self):
        return list(self.keys)

    @classmethod
    def from_list(cls, data):
        return cls(sec.safe_list(data, 16))

    def summary(self):
        if not self.keys:
            return "No modifiers"
        return ", ".join(MODIFIERS_BY_KEY[k].name for k in self.keys)
