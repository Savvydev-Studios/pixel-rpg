"""Game modes: what shape a run has, chosen before it starts.

**Roadmap item 0ag** (2026-09-05, s59). Savvydev:

> "also add some extra gamemodes with co-op compatibility, like a wave
> gamemode that gives u stuff every wave and every 10 waves or something
> heals everyone and u get like skills and armor and weapons and stuff
> every wave, and like that kind of stuff yk? but like that gamemode
> wouldnt have save slots or anything, if u die u get a score and thats
> it, and u can add modifiers and set the difficulty for that one too, and
> when ur starting co-op u get to pick whether to do a regular run or do a
> different gamemode, yk? that kind of stuff."

**"extra gamemodes" is plural, and that is the requirement.** He is
describing a *seam* - somewhere other modes plug in - and giving waves as
the first one. A wave mode hardcoded into `Game` would satisfy the
sentence and miss the request, so the mode is data: `MODES` is a list,
everything that branches asks a `Mode` what it wants, and a second mode is
a new entry plus whatever screens it needs.

**No pygame in here**, for the same reason `battle.py` has none (hard rule
2.3): the ramp and the scoring are arithmetic, and arithmetic that can
only be tested by opening a window does not get tested.

**What a mode is allowed to change** is deliberately narrow - three
questions, because three is what the flow actually asks:

* `saves`  - does it use one of the save slots? Waves does not: *"that
             gamemode wouldnt have save slots or anything"*.
* `island` - does it put you in the world? Waves does not; it is the
             arena, back to back.
* `scores` - does it write a record when it ends?

Everything else - the classes, the difficulty, the modifiers, the battle
engine, the co-op transport - is shared, and that is the point. A mode
that needed its own combat would be a second game to keep in step.
"""
from __future__ import annotations

from dataclasses import dataclass

from .entities import MAX_PACK
from .util import MAX_LEVEL, clamp

#: How often the whole party is put back on its feet. His: *"every 10
#: waves or something heals everyone"*. Ten, and it is also where the
#: pack stops growing for a round, so the milestone reads as a breath
#: rather than only a number.
HEAL_EVERY = 10


@dataclass(frozen=True)
class Mode:
    """One way to play. `key` is what a save, a record and a room carry."""
    key: str
    name: str
    blurb: str
    #: Does it live in one of the six save slots?
    saves: bool = True
    #: Does it put the player on the island?
    island: bool = True
    #: Does finishing it write a record to the profile?
    scores: bool = True
    #: What this mode's `RunRecord.waves` counts, singular and plural.
    #: **Every scored mode records one number that means "how far"**, and
    #: it is a different noun in each: waves survived, nights stood,
    #: coins carried home. The record field is called `waves` because
    #: that is the mode that invented it; the *label* has to be the
    #: mode's own word or a hoard run reads as a Last Stand (§88).
    depth: tuple = ("wave", "waves")
    #: **Is it fought in real time?** (§87) Three places asked
    #: `self.mode == "zombies"` instead, and when a second free-roam mode
    #: arrived every one of them was wrong - including the one that
    #: builds the party, so a hoard in a room was announced with a roster
    #: of one and every guest correctly ignored it. A field, so a fifth
    #: mode says what it is rather than being remembered.
    roam: bool = False

    @property
    def is_waves(self):
        return self.key == "waves"


MODES = (
    Mode("story", "The long road",
         "The island, six wardens, and every ending. Saves to a slot.",
         saves=True, island=True, scores=True),
    Mode("waves", "Last Stand",
         "Wave after wave in the arena. No slot, no second chance - "
         "how far you got is the score.",
         saves=False, island=False, scores=True,
         depth=("wave", "waves")),
    # **The second mode to need the free-roam engine** (items 0ay and
    # 0ak). His words, 2026-09-06: *"we should add a zombies gamemode,
    # but that wouldnt be turn-based, it would be free-roam combat or
    # whatever its called, and ofc it would have co-op and no save"*.
    #
    # Everything about the *shape* is Last Stand's - no slot, no island,
    # a score at the end - and the only thing that differs is that the
    # fighting is real time. That is the seam §59 was built for working.
    Mode("zombies", "The Long Night",
         "They come at you and they do not take turns. Move, aim, dodge. "
         "No slot; how many nights you stood is the score.",
         saves=False, island=False, scores=True, roam=True,
         depth=("night", "nights")),
    # **The mode that is not about surviving** (item 0bv, §86). Savvydev,
    # 2026-09-07: *"i also want to add a lot more gamemodes but idek what
    # honestly"* - and the gap §83 named out loud was that both existing
    # modes are *survive a rising number of enemies*.
    #
    # So this one you cannot lose by dying. Falling costs you the load you
    # were carrying and four seconds; the clock is the only thing that
    # ends it. That turns the question from *can I stay alive* into **how
    # far out do I go before I turn back**, which is a decision the other
    # two modes never ask.
    Mode("hoard", "The Hoard",
         "Coins on the floor, monsters between you and them, and a clock. "
         "Falling costs you what you were carrying, not the run.",
         saves=False, island=False, scores=True, roam=True,
         depth=("coin", "coins")),
)

MODES_BY_KEY = {mode.key: mode for mode in MODES}
DEFAULT_MODE = MODES[0].key


def mode_for(key):
    """Never raises. An unknown mode is the ordinary game."""
    return MODES_BY_KEY.get(str(key or ""), MODES_BY_KEY[DEFAULT_MODE])


def is_roam(key):
    """Whether `key` is a mode fought in real time (§87)."""
    return bool(mode_for(key).roam)


def depth_label(key, count):
    """How far a run of this mode got, in that mode's own noun (§88).

    `RunRecord.waves` holds it for every mode, because Last Stand
    invented the field - but a hoard's 208 is coins and a night's 9 is
    nights, and printing either as *"208 waves"* is a records screen
    lying about which game was played.
    """
    mode = mode_for(key)
    if mode.key == DEFAULT_MODE:
        return ""
    count = int(count)
    one, many = mode.depth
    return "%d %s" % (count, one if count == 1 else many)


# -- the wave ramp ------------------------------------------------------------
#
# **These numbers are his** (rule 31.7): how fast a wave mode ramps is
# difficulty, which is taste rather than measurement, so they are gathered
# here in one block to be moved rather than scattered through the code
# that reads them.
#
# The shape they are aiming at: wave 1 is a fight a level-1 hero wins,
# wave 10 is where the first heal lands and the hero is around level 8,
# and somewhere past wave 25 the packs are full-sized and above level.
# Nothing here is measured yet - `tools/balance.py` does not play waves -
# and saying so is better than implying it was.

#: Levels climbed per wave. 0.6 puts wave 10 at about level 6 and wave 30
#: at about 18, which the per-wave rewards are meant to keep pace with.
# ----------------------------------------------------------- the arenas ---
#
# **Item 0am** (2026-09-06). Savvydev:
#
# > "add a LOT of different maps for the wave gamemode, so if people turn
# > on the free roam combat thing it looks pretty cool and stuff"
#
# Every wave was fought in `"meadow"` - the string is typed into
# `Game.open_wave` and into `host_open_wave` - so a run to wave forty was
# forty fights in one field against one tier-0 bestiary, and the mode's
# whole ramp was a number going up.
#
# **An arena is purely visual** - his correction, 2026-09-06, item 0ao.
# It is a name, a line and a colour, and it decides **nothing** about
# the fight: not the monsters, not the level, not the pack size. Where
# you are must not change what you are up against, and the first
# version of this got that wrong by letting the arena pick a biome.
#
# Nothing here is new art (hard rule 1) and nothing here is a new
# system, which is what lets there be a lot of them.
#
# **They are ordered, and the order is the ramp.** You do not wander into
# The White Flats on wave two: the list climbs through the biome tiers the
# island climbs through, so arriving somewhere new says *it is getting
# worse* without a number being shown. After the last one it wraps, and
# says so.


@dataclass(frozen=True)
class Arena:
    """One place waves are fought in."""

    key: str
    name: str
    line: str           # one line when you arrive
    #: **What is actually in the arena** (item 0bp). His words:
    #: *"the arenas are far too simple, there should be a LOT of
    #: different arenas and stuff"* - and §79 had answered that with a
    #: *colour*, which is not a place. This names a floor plan, and
    #: `freeroam.plan_blockers` turns it into cover you walk round and
    #: fight behind.
    #:
    #: Still purely visual in the turn-based modes, which never read it -
    #: an arena decides nothing about a fight there (0ao).
    plan: str = "open"
    #: What stands between you and the sky (item 0ar). One of
    #: `battlescreen`'s silhouette kinds - "hills", "spires", "arches",
    #: "trees", "crags", "flat". **Still purely visual** (0ao): it
    #: changes the skyline and the motes and touches nothing about the
    #: fight.
    scene: str = "hills"
    #: **The colour of the place**, and it is not the biome's own `tint`.
    #: That field is an ambient wash meant to sit near white and multiply
    #: over a map, so running eighteen of them through the same 0.34
    #: produced eighteen arenas that a screenshot showed as about four
    #: (rule 31.4). These are chosen for the place instead - and the
    #: biome still decides the monsters, which is the half that should
    #: come from somewhere shared.
    tint: tuple = ()


ARENAS = (
    Arena("greens", "The Trampled Green",
          "Somebody's pasture. It was a pasture this morning.",
          plan="scatter",
          scene="hills",
          tint=(108, 196, 96)),
    Arena("fair", "The Fairground",
          "Bunting, still up. Nobody took it down and nobody will.",
          plan="lanes",
          scene="flat",
          tint=(232, 178, 92)),
    Arena("ford", "The Ford",
          "Ankle deep, and it gets deeper behind you.",
          plan="stream",
          scene="hills",
          tint=(96, 172, 168)),
    Arena("ring", "The Beast Ring",
          "A circle of packed earth with a fence somebody has mended "
          "many times.",
          plan="ring",
          scene="trees",
          tint=(74, 148, 84)),
    Arena("orchard", "The Drowned Orchard",
          "Rows of trees standing in water, all of them dead and all of "
          "them upright.",
          plan="grove",
          scene="trees",
          tint=(86, 138, 128)),
    Arena("quarry", "The Quarry Floor",
          "Cut stone on four sides. Whatever comes has to come down to "
          "you.",
          plan="corners",
          scene="crags",
          tint=(146, 142, 132)),
    Arena("gallery", "The Long Gallery",
          "A worked-out seam, propped and re-propped, going back further "
          "than the lamp reaches.",
          plan="pillars",
          scene="arches",
          tint=(108, 96, 132)),
    Arena("sink", "The Sinkhole",
          "Daylight in a circle overhead, a long way overhead.",
          plan="pit",
          scene="crags",
          tint=(92, 118, 176)),
    Arena("terrace", "The Frozen Terrace",
          "Flagstones under the ice, which means somebody built this.",
          plan="pillars",
          scene="arches",
          tint=(150, 208, 232)),
    Arena("pines", "The Black Pines",
          "The trees stop dead in a line and this is what is inside it.",
          plan="grove",
          scene="trees",
          tint=(72, 108, 118)),
    Arena("canyon", "Redcut Canyon",
          "Red walls, a red floor, and one way out at each end.",
          plan="lanes",
          scene="crags",
          tint=(206, 116, 76)),
    Arena("cistern", "The Dry Cistern",
          "Steps down all the way round to a floor that has not held "
          "water in a lifetime.",
          plan="ring",
          scene="arches",
          tint=(188, 152, 108)),
    Arena("caldera", "The Caldera Lip",
          "Warm through your boots. It gets warmer toward the middle.",
          plan="pit",
          scene="crags",
          tint=(226, 104, 56)),
    Arena("glass", "The Glassworks",
          "Black glass underfoot in sheets, and every one of them broken "
          "the same way.",
          plan="shards",
          scene="spires",
          tint=(150, 112, 208)),
    Arena("forge", "The Cold Forge",
          "Somebody's furnace, the size of a house, out for a hundred "
          "years.",
          plan="corners",
          scene="arches",
          tint=(176, 88, 72)),
    Arena("flats", "The White Flats",
          "Salt to the edge of sight in every direction. No cover, for "
          "anybody.",
          plan="open",
          scene="flat",
          tint=(226, 224, 208)),
    Arena("causeway", "The Bone Causeway",
          "A raised road across nothing, wide enough for four abreast.",
          plan="causeway",
          scene="flat",
          tint=(198, 186, 150)),
    Arena("gate", "The Hollow Gate",
          "Two uprights and a lintel, standing on their own, with "
          "nothing either side of them.",
          plan="gate",
          scene="arches",
          tint=(128, 120, 168)),
)

ARENA_BY_KEY = {a.key: a for a in ARENAS}

#: How many waves are fought in one place before the ground changes,
#: when the player has not said otherwise.
WAVES_PER_ARENA = 2

#: **How the arena is chosen, and it is the player's call** - item 0au,
#: his words: *"they can pick an arena to use for the whole run or pick a
#: random one, or it changes every 10 or 5 or whatever waves. its up to
#: them."*
#:
#: `"every"` walks the list on a cadence they set; `"one"` stays in a
#: place they picked; `"random"` picks one place per run and stays there.
#: **None of these is the game changing its mind** - 0aq's rule is about
#: the game shuffling scenery behind your back, and a setting somebody
#: chose is the opposite of that.
ARENA_MODES = ("every", "one", "random")
#: The cadences offered when the arena changes. Ten is his own example.
ARENA_CADENCES = (2, 5, 10)


def arena_step(wave, every=None):
    """Which step of the climb this wave is on. 0 is the first."""
    span = int(every or WAVES_PER_ARENA) or WAVES_PER_ARENA
    return max(0, int(wave) - 1) // span


def arena_for(wave, mode="every", every=None, pick=0):
    """Where wave `wave` is fought. Never raises, and always answers.

    **On the default setting the same wave is always the same place.**
    Savvydev, 2026-09-06: *"the arenas always remain the same, they dont
    change."* An earlier version rotated them per run behind the
    player's back; this varies only where the player asked it to.

    Wraps at the end rather than stopping: a Last Stand has no last wave
    (§59), so the list of places cannot have a last entry either.
    """
    if str(mode) in ("one", "random"):
        return ARENAS[int(pick) % len(ARENAS)]
    return ARENAS[arena_step(wave, every) % len(ARENAS)]


def arena_lap(wave, every=None):
    """How many times the arenas have been round. 0 on the first pass."""
    return arena_step(wave, every) // len(ARENAS)


def arena_changes_at(wave, mode="every", every=None):
    """Whether this wave is the first one in a new place."""
    if str(mode) in ("one", "random"):
        return int(wave) == 1
    span = int(every or WAVES_PER_ARENA) or WAVES_PER_ARENA
    return (max(0, int(wave) - 1)) % span == 0


# ------------------------------------------------------- the boss waves ---
#
# **Item 0as** (2026-09-06). Savvydev:
#
# > "also the wave gamemode has no connection to the story or anything
# > but yoou can end up fighting wardens as bosses"
#
# **Two halves, and the first is a constraint on the second.** Wave mode
# touches no story - and *within* that, a warden can be the thing that
# walks in.
#
# So a warden here is **a statblock, not a story beat**: no arena
# briefing, no taunt scene, no Memory, no chapter, nothing recorded
# against `meta`, and killing one does not count toward any ending. The
# thing that makes this safe is that `Game.finish_battle` only records a
# warden kill on the *story* path, and a wave run has no story path at
# all - it has no slot to write to (§59).

#: Every this-many waves, the thing that walks in is a warden.
#: **Ten**, because his own description of the mode is *"every 10 waves
#: or something heals everyone"* (§59) - the mode already thinks in tens,
#: and a boss on the same beat as the heal is a rhythm rather than two
#: competing ones.
WAVES_PER_BOSS = 10


def boss_wave(wave):
    """Whether this wave is a warden. Never raises."""
    wave = int(wave)
    return wave > 0 and wave % WAVES_PER_BOSS == 0


def boss_for_wave(wave, keys):
    """Which warden turns up, given the ones the game has.

    **In the island's order and then round again**, so the first one you
    meet in a Last Stand is the first one you would meet in a story run.
    `keys` is passed in rather than imported so this file keeps knowing
    nothing about `entities` - the same reason the rest of it is
    arithmetic.
    """
    keys = list(keys)
    if not keys or not boss_wave(wave):
        return ""
    return keys[(int(wave) // WAVES_PER_BOSS - 1) % len(keys)]


def boss_scale(wave):
    """How much tougher a warden is than its printed self, this deep.

    A warden has one level and a Last Stand runs past it, so without
    this the wave-30 boss is easier than the wave-29 pack. Multiplies
    health and damage rather than moving its level, because level is
    what its skills and its Memory are written against on the island -
    and this is the same warden, met somewhere with no story in it.
    """
    laps = max(0, int(wave) // WAVES_PER_BOSS - 1)
    return 1.0 + 0.35 * laps


# ---------------------------------------------------------- the long night ---
#
# **Item 0ay.** The ramp is deliberately *not* Last Stand's: a wave there
# is a pack you fight to a finish, and a night here is a crowd that keeps
# arriving while you are still dealing with the last of it. So the
# numbers that matter are how many are on the field at once and how fast
# the next one turns up, not how many the wave contains.

#: How many are already waiting when a night opens.
NIGHT_OPENING = 3
#: And how many more it adds per night after the first.
NIGHT_GROWTH = 1.6
#: Never more than this on the field at once. **A cap, not a budget** -
#: past about a dozen the arena stops being a place you can move in and
#: the fight becomes a queue.
NIGHT_CROWD = 14
#: Seconds between arrivals, and how much that tightens each night.
NIGHT_GAP, NIGHT_GAP_STEP, NIGHT_GAP_FLOOR = 2.4, 0.12, 0.55


def night_count(night):
    """How many turn up over the course of one night."""
    night = max(1, int(night))
    return int(NIGHT_OPENING + NIGHT_GROWTH * (night - 1) * 1.8)


def night_crowd(night):
    """The most that may be on the field at once."""
    return int(min(NIGHT_CROWD, 4 + max(0, int(night) - 1)))


def night_gap(night):
    """Seconds between arrivals on this night."""
    gap = NIGHT_GAP - NIGHT_GAP_STEP * (max(1, int(night)) - 1)
    return max(NIGHT_GAP_FLOOR, gap)


def night_level(night):
    """How strong the things that arrive are.

    The same shape as `wave_level` and deliberately a little slower: a
    night lasts longer than a wave, so the same climb per night would
    outrun the player faster than it does in Last Stand.
    """
    return int(clamp(1 + (max(1, int(night)) - 1) * 0.5, 1, MAX_LEVEL))


class NightRun:
    """The state of one Long Night run. Arithmetic only - no pygame.

    Shaped like `WaveRun` on purpose: the two modes are scored the same
    way and by the same code, and the only difference between them is
    what happens between the scores.
    """

    def __init__(self, difficulty="normal", modifier_score=1.0):
        self.wave = 1                # "night", but the records call it wave
        self.cleared = 0
        self.kills = 0
        self.playtime = 0.0
        self.over = False
        self.spawned = 0             # how many have arrived this night
        self.since = 0.0             # seconds since the last arrival
        # **How many wardens this run has put down** (item 0bt). Savvydev:
        # *"everytime a boss is defeated the drop rates for better items
        # gets a bit higher"* - so it is a count on the run rather than a
        # flag, and `spoils.luck_for` and `spoils.tier_odds` are the two
        # things that read it. One number, two readers, no second table.
        self.bosses = 0
        self.difficulty = str(difficulty)
        self.modifier_score = float(modifier_score)

    @property
    def due(self):
        """Whether this night still has arrivals to come."""
        return self.spawned < night_count(self.wave)

    def ready(self, on_field):
        """Whether one more may arrive right now."""
        return (self.due and on_field < night_crowd(self.wave)
                and self.since >= night_gap(self.wave))

    def arrived(self):
        self.spawned += 1
        self.since = 0.0

    def clear_night(self):
        """The night is survived. Returns what it paid."""
        reward = wave_rewards(self.wave)
        self.cleared = self.wave
        if boss_wave(self.wave):
            self.bosses += 1
        self.wave += 1
        self.spawned = 0
        self.since = 0.0
        return reward

    def end(self):
        self.over = True
        return self.score()

    def score(self):
        """One number, the same shape the other two scored modes use.

        **A night's score lived in `Game.finish_night_run` as a line of
        arithmetic** and never went through a difficulty multiplier - so
        a nightmare night was harder for exactly the same score, while a
        Last Stand and a hoard were both worth 1.8x for it (§89). That is
        §54's *"two payout paths, and the rarer one keeps getting patched
        first"* in the scoring rather than the rewards.

        **There is deliberately no kill cap here**, which is the one
        thing that is *not* copied from `WaveRun.score`. That cap exists
        because a wave you can already beat can be farmed; a night ends
        when its arrivals are dead, so there is nothing to farm and a cap
        would only punish clearing the place out.
        """
        base = self.cleared * 260 + self.kills * 18
        base *= difficulty_score(self.difficulty)
        base *= max(1.0, self.modifier_score)
        return max(0, int(base))


# -- the hoard ---------------------------------------------------------------
#
# **His numbers to move** (rule 31.7), gathered here for the same reason
# the wave ramp is: how long a run is and how fast it gets crowded is
# taste rather than measurement.

#: How long a run lasts. Three minutes is one bus ride's worth and short
#: enough that a bad run costs nothing - which is what lets somebody be
#: greedy twice.
HOARD_TIME = 180.0
#: What is on the floor when it starts, and how often more arrives.
HOARD_START = 14
HOARD_DROP = 6.5
HOARD_DROP_FLOOR = 2.5
#: How many coins arrive at a time, and the most that can be lying about.
HOARD_BATCH = 3
HOARD_MAX_LOOSE = 60
#: **A big trip pays double** (§87). Measured: with a cost on greed and
#: no reward for it, a bot banking three at a time beat every greedier
#: policy by a mile, so *how far out do I go* had one right answer and
#: was not a decision. This is the upside the premise needs - a full
#: load is worth more *per coin*, so the tenth coin is the one worth
#: risking - and it is a step rather than a curve because a player has
#: to be able to count it while being chased.
HOARD_BIG_TRIP = 10
HOARD_BIG_BONUS = 2
#: The monsters. **Fewer than a night and they never stop coming**: they
#: are the terrain here, not the point, and a hoard that turns into a
#: fight has become The Long Night with a timer.
HOARD_CROWD_START = 3
HOARD_CROWD_EVERY = 26.0
HOARD_CROWD_MAX = 12
HOARD_SPAWN_GAP = 3.4
HOARD_LEVEL_EVERY = 30.0


def hoard_trip_value(count):
    """What one trip home is worth. **Ten or more pays double.**

    Measured (§87): with a cost on greed and no reward for it, banking
    three at a time beat every greedier policy, so *how far out do I go*
    had one right answer and was not a decision. With this, the best
    policy a bot found moved from **carry 3** to **carry 14**, and both
    the timid and the reckless ends score less.

    A step rather than a curve because a player has to be able to count
    it while being chased.
    """
    count = max(0, int(count))
    if count >= HOARD_BIG_TRIP:
        return count * HOARD_BIG_BONUS
    return count


def hoard_level(seconds):
    """How hard the monsters are, this far into a run."""
    return max(1, 1 + int(max(0.0, seconds) // HOARD_LEVEL_EVERY) * 2)


def hoard_crowd(seconds):
    """How many may be on the floor at once, this far in."""
    grown = HOARD_CROWD_START + int(max(0.0, seconds) // HOARD_CROWD_EVERY)
    return int(clamp(grown, HOARD_CROWD_START, HOARD_CROWD_MAX))


def hoard_drop_gap(seconds):
    """How long between coin drops. Faster as the run goes on, so the
    floor keeps refilling while it gets harder to cross."""
    eased = HOARD_DROP - (max(0.0, seconds) / HOARD_TIME) * 3.0
    return max(HOARD_DROP_FLOOR, eased)


class HoardRun:
    """The state of one hoard run. Arithmetic only - no pygame.

    Shaped like `WaveRun` and `NightRun` on purpose (§59's seam): a
    clock, a number that goes up, and a score. **What is different is
    only what ends it** - and that is the mode.
    """

    def __init__(self, difficulty="normal", modifier_score=1.0):
        self.left = HOARD_TIME
        self.playtime = 0.0
        self.since_drop = 0.0
        self.since_spawn = 0.0
        self.banked = 0
        self.best_trip = 0
        self.big_trips = 0
        self.lost = 0                # coins dropped and never picked back up
        self.falls = 0
        self.kills = 0
        self.over = False
        #: Kept so the records and the boards can read every run the same
        #: way. A hoard has no waves; what it has is one clock.
        self.wave = 1
        self.cleared = 0
        self.bosses = 0
        self.difficulty = str(difficulty)
        self.modifier_score = float(modifier_score)

    @property
    def elapsed(self):
        return max(0.0, HOARD_TIME - self.left)

    def tick(self, dt):
        """Advance the clock. Returns True on the frame it runs out."""
        if self.over:
            return False
        dt = max(0.0, float(dt))
        self.playtime += dt
        self.since_drop += dt
        self.since_spawn += dt
        self.left = max(0.0, self.left - dt)
        if self.left <= 0.0:
            self.over = True
            return True
        return False

    def due_coins(self, loose):
        """Whether more should be scattered now."""
        if self.over or loose >= HOARD_MAX_LOOSE:
            return 0
        if self.since_drop < hoard_drop_gap(self.elapsed):
            return 0
        self.since_drop = 0.0
        return HOARD_BATCH

    def due_monster(self, on_field):
        """Whether one more should arrive now."""
        if self.over or on_field >= hoard_crowd(self.elapsed):
            return False
        if self.since_spawn < HOARD_SPAWN_GAP:
            return False
        self.since_spawn = 0.0
        return True

    def bank(self, worth, count=None):
        """One trip home, already valued by `hoard_trip_value`.

        **Valued where the trip happens** (`freeroam.gather`), not here:
        a guest is told what its haul is worth in the frame, and a room
        where two machines score the same trip differently is a room
        with two scores.
        """
        worth = max(0, int(worth))
        count = worth if count is None else max(0, int(count))
        self.banked += worth
        if count >= HOARD_BIG_TRIP:
            self.big_trips += 1
        self.best_trip = max(self.best_trip, count)
        return self.banked

    def end(self):
        self.over = True
        return self.score()

    def score(self):
        """One number: **what you got home**.

        Not what you picked up - a run that dies holding forty coins
        scores nothing for them, which is the whole of the tension. Kills
        are worth a little because clearing a path is a real strategy,
        and capped against the haul so that fighting instead of carrying
        is never the better score.
        """
        base = self.banked * 60 + min(self.kills * 10, self.banked * 20)
        base *= difficulty_score(self.difficulty)
        base *= max(1.0, self.modifier_score)
        return max(0, int(base))


#: What The Long Night spawns from (item 0bx). Named here rather than
#: written into `game.py` so the mode owns its own bestiary the way it
#: owns its own ramp.
NIGHT_BIOME = "night"

LEVEL_PER_WAVE = 0.6
#: A wave gets one more monster every this many waves, up to `MAX_PACK`.
WAVES_PER_EXTRA = 4
# **There is deliberately no per-wave heal** (s60), and it was tried.
# Health carrying between waves looked like the reason nothing reached
# wave 10 - but that was the harness, not the mode, and once the probe
# spent its attribute points the same measurement said a 25% or even a 40%
# top-up moves the curve by less than the noise. So it was taken back out:
# attrition between waves is what makes depth mean anything, and a knob
# that changes nothing is a knob somebody will tune later believing it
# does. `HEAL_EVERY` is the one recovery this mode has.


def wave_level(wave):
    """The level the monsters in a wave are rolled at."""
    wave = max(1, int(wave))
    return int(clamp(1 + (wave - 1) * LEVEL_PER_WAVE, 1, MAX_LEVEL))


def wave_pack(wave, party=1):
    """How many monsters a wave brings.

    Grows with the wave and with the party, and stops at `MAX_PACK` - the
    arena fans them across the screen and s23 already found that more than
    that is unreadable long before it is balanced.
    """
    wave = max(1, int(wave))
    party = max(1, int(party))
    base = 1 + (wave - 1) // WAVES_PER_EXTRA
    return int(clamp(base + (party - 1), 1, MAX_PACK))


def is_rest_wave(wave):
    """Every tenth wave puts everybody back on their feet."""
    return int(wave) > 0 and int(wave) % HEAL_EVERY == 0


def wave_rewards(wave):
    """What clearing `wave` hands out, as a description rather than an act.

    Returned as data so it can be tested, shown on the intermission screen
    *before* it is taken, and - the reason that matters - handed to every
    player in a co-op wave from the one machine that decided it.

    His list: *"u get like skills and armor and weapons and stuff every
    wave"*. So every wave pays gear; the rest are milestones on top.
    """
    wave = max(1, int(wave))
    level = wave_level(wave)
    reward = {
        "wave": wave,
        # Every wave, because "every wave" is what he said.
        "items": 1,
        "level": level,
        "gold": 40 + wave * 18,
        "xp": 30 + wave * 22,
        "consumables": 0,
        "points": 0,
        "heal": False,
        "skill": False,
    }
    if wave % 3 == 0:
        reward["consumables"] = 2
    if wave % 5 == 0:
        reward["points"] = 2
        reward["items"] = 2
    if is_rest_wave(wave):
        # The milestone: everybody back on their feet, a better haul, and
        # something new to press.
        reward["heal"] = True
        reward["skill"] = True
        reward["items"] = 3
        reward["consumables"] = 3
    return reward


def grant_wave_rewards(player, reward, roll_item, roll_consumable=None):
    """Hand `reward` to `player`. **The mechanical half only.**

    Split out of `Game.wave_cleared` so that `tools/balance.py` can play a
    real wave run headlessly (s60). The alternative was the harness
    reimplementing the payout, which s23's docstring warns about in the
    other direction: *"a second copy of them in a measuring tool would
    eventually measure a game nobody is playing"*.

    So `Game` keeps the toasts, the sound and the co-op publish, and this
    keeps everything that changes the hero. `roll_item` is passed in
    rather than imported because the game rolls loot through its own
    modifier-aware path.

    Returns `(levels_gained, new_skills, items_kept)`.
    """
    player.gold += reward["gold"]
    player.stats_tracker["gold_earned"] = (
        player.stats_tracker.get("gold_earned", 0) + reward["gold"])
    kept = 0
    for _ in range(reward["items"]):
        if not player.add_item(roll_item(reward["level"] + 2)):
            break
        kept += 1
    if roll_consumable is not None:
        for _ in range(reward["consumables"]):
            player.add_consumable(roll_consumable(), 1)
    if reward["points"]:
        player.points = min(player.points + reward["points"],
                            player.spendable_points())
    levels, new_skills = player.gain_xp(reward["xp"])
    if reward["heal"]:
        # *"every 10 waves or something heals everyone"*. After the level
        # up, not before: a level raises the maximum, and healing first
        # would leave the hero short of the new one.
        player.full_restore()
    return levels, new_skills, kept


#: What each difficulty multiplies a score by, in every scored mode.
#: The same ordering as `config.DIFFICULTIES`, and the same idea as a
#: modifier's score: **a harder run is worth more, and nothing else
#: about it changes.**
DIFFICULTY_SCORE = {"story": 0.5, "normal": 1.0,
                    "hard": 1.35, "nightmare": 1.8}


def difficulty_score(key):
    """The multiplier for one difficulty. Never raises."""
    return DIFFICULTY_SCORE.get(str(key or ""), 1.0)


class WaveRun:
    """The state of one wave run. Arithmetic only - no pygame, no `Game`.

    Deliberately small: the wave number, what has happened, and enough to
    score it. Everything that *acts* on this lives in `Game`, so that a
    second mode with a different loop does not have to inherit a
    half-relevant object.
    """

    def __init__(self, difficulty="normal", modifier_score=1.0):
        self.wave = 1
        self.cleared = 0
        self.kills = 0
        self.playtime = 0.0
        self.over = False
        # **How many wardens this run has put down** (item 0bt). Savvydev:
        # *"everytime a boss is defeated the drop rates for better items
        # gets a bit higher"* - so it is a count on the run rather than a
        # flag, and `spoils.luck_for` and `spoils.tier_odds` are the two
        # things that read it. One number, two readers, no second table.
        self.bosses = 0
        self.difficulty = str(difficulty)
        self.modifier_score = float(modifier_score)

    def clear_wave(self, kills=0):
        """A wave was won. Returns the rewards for the wave just cleared."""
        reward = wave_rewards(self.wave)
        self.cleared = self.wave
        self.kills += max(0, int(kills))
        if boss_wave(self.wave):
            self.bosses += 1
        self.wave += 1
        return reward

    def end(self):
        self.over = True
        return self.score()

    #: Kept as a name on the class because that is where it was found
    #: for two months. **The table itself is module level** (§89) - a
    #: hoard was already reaching through `WaveRun` to read it, which is
    #: how a shared rule ends up looking like one mode's property.
    DIFFICULTY_SCORE = DIFFICULTY_SCORE

    def score(self):
        """One number. *"if u die u get a score and thats it"*.

        **Waves survived is the headline and everything else is a
        rounding.** Anything that made kills or gold competitive with
        depth would reward farming a wave you can already beat, which is
        the one thing a mode with no save must not pay for.
        """
        # **The kill bonus is capped against depth**, which is the whole
        # of what makes the sentence above true. Written first as a flat
        # `kills * 8`, it let five farmed waves outscore twenty survived
        # ones - a test caught it, and the docstring above was already
        # claiming otherwise. A cap of 40 a wave keeps kills as a
        # tie-breaker between two runs that got equally deep and stops
        # them being a strategy.
        base = self.cleared * 250 + min(self.kills * 8, self.cleared * 40)
        base *= difficulty_score(self.difficulty)
        base *= max(1.0, self.modifier_score)
        return max(0, int(base))
