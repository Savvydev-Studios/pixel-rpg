"""Procedural animation: what a body is doing, as maths over a normalised beat.

Hard rule 1 says no binary assets, so there are no new frames to draw here and
there never will be - every sprite in the game is a hand-typed 16x16 grid, and
a full attack-and-death set for 36 of them is not work anybody should sign up
for. What is left is arithmetic on the sprites that already exist, and it
turns out that is where nearly all of the perceived improvement lives:
**anticipation, arcs, overshoot, recoil and easing**, not more pixels.

This module imports no pygame, for the same reason `battle.py` does not: a
curve is a number and should be testable without a display. It hands back a
`Pose` - a small bundle of offsets and scales - and the renderer decides what
to do with it.

**One convention, and everything depends on it:** `dx` is measured **toward
the other side**, not toward the right of the screen. Players face right and
monsters face left, so the caller multiplies by `+1` or `-1` and every move in
here reads the same for both. `dy` is **up**, which is the opposite of the
screen's y axis, again because "the hero rises" should read as a positive
number in the maths and the sign flip belongs in one place.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .util import clamp, ease_in_cubic, ease_out_cubic

# What each move is worth in seconds, and **the impact frame has to line up
# with the damage step in `battle.py`** - which schedules the numbers at
# 0.16s for an attack and 0.18s for a skill. Get this wrong and the damage
# appears before the blow lands, which is worse than no animation at all:
# the eye reads it as the number causing the swing.
#
#   lunge   impact opens at t=0.258 x 0.62s = 0.160s   matches `after(0.16)`
#   cast    peak    opens at t=0.260 x 0.70s = 0.182s   matches `after(0.18)`
#
# Change either number and change the other. The split within a move is the
# usual one for a blow that should feel heavy: a long wind-up, a very short
# strike, and a recovery several times longer than either.
SPANS = {
    "lunge": 0.62,     # 105ms wind-up, 55ms strike, 57ms impact, 403ms return
    "cast": 0.70,
    # **A dart is a lunge in a hurry** (item 0al, s65): less coil, less
    # reach, and back on its heel before a lunge has finished landing. It
    # is what a Cutpurse's flurry and a Duelist's thrust move like, so a
    # thirteen-class roster stops swinging one swing.
    "dart": 0.46,
    # **A heavy blow takes longer to arrive** (item 0al, s70). Savvydev:
    # *"make sure some of the bigger more heavy hitting weapons and skills
    # look way more cooler and bigger"*. A greatsword that swings at a
    # dagger's speed is a dagger with a bigger number on it.
    "heavy": 0.86,
    # **A spell is not one move** (item 0al(b), s71). `cast` was played by
    # every ability in the game - a heal, a curse, a buff and a Meteor -
    # so thirteen classes with sixty-odd skills between them had one
    # gesture. These three are chosen from what an ability already *is*
    # (`skill_motion`), so a new skill gets a sensible move for free.
    "channel": 0.98,   # a big spell: crouch, haul up, hold, release hard
    "bless": 0.72,     # a heal or a buff: rise, glow, settle. No forward step
    "hurl": 0.58,      # a throw: wind back and snap, without going anywhere
    # **Bracing is a move.** `player_defend` has always said "braces" in
    # the log and done nothing on screen.
    "brace": 0.50,
    # **Being hit is not one move either.** `recoil` was played for a
    # scratch, a blocked blow and a Meteor alike.
    "flinch": 0.26,    # a scratch
    "recoil": 0.45,    # an ordinary blow
    "reel": 0.68,      # a hit that took a chunk: knocked back, and staggers
    "shrug": 0.30,     # turned aside: a brace rather than a knock
    "fall": 0.85,
    # **And dying is not one move.** See `death_move`.
    "crumble": 0.95,   # thrown down by something heavy
    "wither": 0.90,    # unmade by magic: sinks where it stands, no knock
}

#: Moves that mean *this body is dead*. A death sticks at the end of its
#: clock rather than resetting, is the one thing that still poses when
#: `alive` is False, and is what `Battle.falling` asks about.
#: **Three places read this set and missing one puts a corpse back on its
#: feet**, which is why it is a set and not three string comparisons.
DEATHS = frozenset({"fall", "crumble", "wither"})

#: Moves that mean *this body was just hit*. They suppress the idle
#: breathing underneath, because a body absorbing a blow is not also
#: calmly filling its lungs.
REACTIONS = frozenset({"recoil", "flinch", "reel", "shrug"})

# **When the blow actually lands, in seconds from the start of the move.**
#
# The comment above spells out the trap this exists to close: *"change
# either number and change the other"*. Two motions had their impact frame
# written down in a comment here and their `after(...)` written down in
# `battle.py`, and a third would have made three pairs to keep in step by
# hand. `battle.py` asks this now.
IMPACT_AT = {
    "lunge": 0.160,    # 0.258 of 0.62
    "cast": 0.182,     # 0.260 of 0.70
    "dart": 0.160,     # 0.348 of 0.46 - the same beat, sooner in its own move
    "heavy": 0.290,    # 0.337 of 0.86 - a long wind-up is the whole point
    # **The spells land on their own release, not on a shared 0.18.**
    # `use_skill` had that number typed into it while the motions had
    # theirs written here - exactly the two-numbers-to-keep-in-step trap
    # the comment above this table exists to close (s71).
    "channel": 0.372,  # 0.380 of 0.98 - a big spell takes longer to arrive
    "bless": 0.200,    # 0.278 of 0.72
    "hurl": 0.185,     # 0.319 of 0.58 - the snap, not the wind-up
}


def impact_time(kind):
    """When to resolve a blow played as `kind`. Never raises."""
    return IMPACT_AT.get(kind, IMPACT_AT["lunge"])


# -- which move, from what already happened --------------------------------
#
# **Nothing new had to be written down for any of these** - the same
# principle s70 got `heft` out of. An ability already says what kind it is
# and what it scales on; a blow already knows what share of the target it
# took. Choosing off those means a skill added next year gets a move
# without anybody remembering to give it one, which is the only kind of
# rule that survives (rule 31.1's opening line).


def skill_motion(kind, magical, heavy, physical="lunge"):
    """Which move an ability plays.

    `kind` is the skill's own `kind` field, `magical` is whether it scales
    on magic, `heavy` is `heft >= Battle.HEAVY_AT`. The threshold stays in
    `battle.py` rather than being copied here: a second copy of a fact is
    a second chance to be wrong (rule 31.1e).
    """
    if kind not in ("attack", "drain"):
        # A heal, a buff, a curse, a shield. Nothing about any of them is a
        # swing, and `cast`'s forward step made every one look like an
        # attack that missed.
        return "bless"
    if magical:
        return "channel" if heavy else "cast"
    return "heavy" if heavy else physical


#: A blow that took this share of the target staggers it; below the second
#: number it is a scratch. Both are *shares*, so they mean the same thing
#: to a slime and to a warden - which is the whole reason the number is a
#: share and not an amount (s70).
REEL_AT, FLINCH_UNDER = 0.24, 0.07


def reaction(share, turned=False):
    """How a body answers a blow that took `share` of its health.

    `turned` covers a blocked or resisted hit: something arrived and was
    dealt with, which looks like bracing rather than being knocked about.
    """
    if turned:
        return "shrug"
    if share >= REEL_AT:
        return "reel"
    if share < FLINCH_UNDER:
        return "flinch"
    return "recoil"


def death_move(element="physical", heavy=False):
    """How a body goes down, from what put it there.

    Heavy first: something that hits that hard throws you, whatever it was
    made of. Otherwise magic *unmakes* rather than knocks over - it sinks
    where it stands - and everything else is the ordinary slump.
    """
    if heavy:
        return "crumble"
    if element and element != "physical":
        return "wither"
    return "fall"

# How far a body travels on each move, in pixels at the reference scale.
REACH = 54.0
KNOCK = 26.0


@dataclass
class Pose:
    """Where a body is and what shape it is, relative to standing still."""

    dx: float = 0.0        # toward the other side
    dy: float = 0.0        # up
    stretch: float = 1.0   # >1 tall and narrow, <1 squat and wide
    alpha: float = 1.0
    sink: float = 0.0      # extra downward slump, kept apart from `dy` so a
                           # dying body can fall while still bobbing


class Motion:
    """One combatant's current move, if any.

    Deliberately dumb: it holds a name and a clock. Everything about how a
    move *looks* is in `pose_of`, so a move can be retimed or reshaped without
    touching the thing that plays it.
    """

    __slots__ = ("kind", "t", "span", "seed")

    def __init__(self, seed=0):
        self.kind = ""
        self.t = 0.0
        self.span = 0.0
        # Keeps two bodies standing next to each other from breathing in
        # lockstep, which reads as one object rather than two.
        self.seed = float(seed)

    @property
    def active(self):
        return bool(self.kind)

    def play(self, kind, span=None):
        self.kind = kind
        self.span = float(span if span is not None else SPANS.get(kind, 0.5))
        self.t = 0.0

    def stop(self):
        self.kind = ""
        self.t = 0.0

    def advance(self, dt):
        """Tick the clock. A `fall` sticks at the end rather than resetting -
        a body that finished collapsing is still collapsed."""
        if not self.kind:
            return
        self.t += max(0.0, dt)
        if self.t >= self.span:
            if self.kind in DEATHS:
                self.t = self.span
            else:
                self.stop()

    @property
    def progress(self):
        if not self.kind or self.span <= 0:
            return 0.0
        return clamp(self.t / self.span, 0.0, 1.0)


def breathe(elapsed, seed=0.0, amount=1.0):
    """Idle rise and fall that is not a metronome.

    A single sine is the obvious thing and it reads as machinery: every body
    on screen hits the top of its arc at the same moment forever. Two waves
    at an irrational-ish ratio never quite line up, so the motion keeps
    drifting against itself and stops being countable.
    """
    phase = elapsed * 2.1 + seed
    wave = math.sin(phase) + 0.42 * math.sin(phase * 0.37 + 1.1)
    return wave * amount


#: How fast the overworld walk winds up and down, per second. Winding up
#: is nearly instant because a step should start on the frame the key goes
#: down; winding *down* is the part that matters - see `walk_pose`.
WALK_IN, WALK_OUT = 9.0, 5.5


def walk_ease(ease, dt, moving):
    """Advance the 0-to-1 blend between standing and walking.

    **The overworld had no blend at all** (item 0al(b), s71): `walk_anim`
    was set to exactly 0.0 on any frame with no input, so letting go of a
    key teleported the body from wherever its bob had reached to standing
    perfectly still. It is the only pop left in the game and it happens
    every few seconds of ordinary play.
    """
    rate = WALK_IN if moving else -WALK_OUT
    return clamp(ease + rate * max(0.0, dt), 0.0, 1.0)


def walk_pose(walk, ease=1.0, elapsed=0.0, seed=0.0, running=False):
    """What a body on the overworld is doing: a bob, a plant, and a breath.

    Three things, and the last two are new. The bob was already here; what
    it lacked was **a contact** - a step that only rises and falls is a
    float, and the frame the foot lands is the one that says weight. And
    standing still was *completely* still, which no other body in this game
    has been since `breathe` was written.

    `ease` blends the two, so the two never both apply at full strength and
    stopping is a settle rather than a cut.
    """
    pose = Pose()
    phase = walk * 2.0
    swing = math.sin(phase)
    lift = 1.35 if not running else 1.9
    pose.dy = swing * lift * ease
    # The plant: only the half of the cycle where the body is coming down
    # squashes, which is what makes one of the two contacts read as the
    # near foot and the other as the far one.
    plant = max(0.0, -swing) ** 1.5
    pose.stretch = 1.0 - 0.055 * plant * ease
    if ease < 1.0:
        pose.dy += breathe(elapsed, seed, 0.9) * (1.0 - ease)
    return pose


def pose_of(motion, elapsed=0.0, idle=2.0, alive=True):
    """The full pose for a body: its move, plus the breathing underneath it."""
    pose = Pose()
    if alive or motion.kind in DEATHS:
        _apply_move(pose, motion)
    if alive and motion.kind not in DEATHS and motion.kind not in REACTIONS:
        pose.dy += breathe(elapsed, motion.seed, idle)
    return pose


def _apply_move(pose, motion):
    kind = motion.kind
    if not kind:
        return
    t = motion.progress
    if kind == "lunge":
        _lunge(pose, t)
    elif kind == "heavy":
        _heavy(pose, t)
    elif kind == "dart":
        _dart(pose, t)
    elif kind == "cast":
        _cast(pose, t)
    elif kind == "channel":
        _channel(pose, t)
    elif kind == "bless":
        _bless(pose, t)
    elif kind == "hurl":
        _hurl(pose, t)
    elif kind == "brace":
        _brace(pose, t)
    elif kind == "recoil":
        _recoil(pose, t)
    elif kind == "flinch":
        _flinch(pose, t)
    elif kind == "reel":
        _reel(pose, t)
    elif kind == "shrug":
        _shrug(pose, t)
    elif kind == "fall":
        _fall(pose, t)
    elif kind == "crumble":
        _crumble(pose, t)
    elif kind == "wither":
        _wither(pose, t)


def _heavy(pose, t):
    """A long haul back, a slow fall, and a landing that keeps going.

    The three things that make a blow read as *weight* rather than as
    speed, and none of them is distance: **a wind-up you can see coming**,
    an arrival that accelerates all the way in, and a recovery long enough
    that the body has to gather itself. A lunge played at half speed just
    looks like a slow lunge; the proportions are what change.
    """
    if t < 0.34:                       # haul it back, and rise with it
        k = ease_out_cubic(t / 0.34)
        pose.dx = -REACH * 0.34 * k
        pose.dy = -REACH * 0.10 * k
        pose.stretch = 1.0 - 0.14 * k
    elif t < 0.337 + 0.10:             # the fall, accelerating hard
        k = ease_in_cubic((t - 0.34) / 0.097)
        pose.dx = -REACH * 0.34 + REACH * 1.52 * k
        pose.dy = -REACH * 0.10 + REACH * 0.10 * k
        pose.stretch = 0.86 + 0.40 * k
    elif t < 0.56:                     # impact: a deep squash
        k = (t - 0.437) / 0.123
        pose.dx = REACH * (1.18 - 0.10 * k)
        pose.stretch = 1.26 - 0.42 * k
        pose.dy = 2.5 * (1.0 - k)
    else:                              # gather it up again, slowly
        k = ease_out_cubic((t - 0.56) / 0.44)
        pose.dx = REACH * 1.08 * (1.0 - k)
        pose.stretch = 0.84 + 0.16 * k


def _dart(pose, t):
    """In, hit, out - the quick version of `_lunge`.

    Shorter coil, less reach, and the recovery is a snap rather than a
    drift. Written as its own curve rather than as `_lunge` with a
    multiplier, because the thing that makes it read as *quick* is the
    proportions changing, not the distance: a scaled-down lunge just looks
    like a small lunge.
    """
    if t < 0.10:                       # barely any wind-up
        k = ease_out_cubic(t / 0.10)
        pose.dx = -REACH * 0.08 * k
        pose.stretch = 1.0 - 0.05 * k
    elif t < 0.348:                    # out, fast and flat
        k = ease_in_cubic((t - 0.10) / 0.248)
        pose.dx = -REACH * 0.08 + REACH * 0.92 * k
        pose.stretch = 0.95 + 0.16 * k
        pose.dy = math.sin(math.pi * k) * REACH * 0.10
    elif t < 0.44:                     # the hit: a short, hard squash
        k = (t - 0.348) / 0.092
        pose.dx = REACH * (0.84 - 0.06 * k)
        pose.stretch = 1.11 - 0.20 * k
        pose.dy = -1.5 * (1.0 - k)
    else:                              # back on the heel
        k = ease_out_cubic((t - 0.44) / 0.56)
        pose.dx = REACH * 0.78 * (1.0 - k)
        pose.stretch = 0.91 + 0.09 * k


def _lunge(pose, t):
    """Wind up, strike, land, recover.

    The old version jumped straight to full reach and drifted back, which is
    a slide rather than a blow: no weight, and nothing to tell you a hit was
    coming. The three things that fix that are all here - a beat of pulling
    *back* first, a strike that accelerates instead of easing, and a snap of
    squash on the frame the blow lands.
    """
    if t < 0.17:                       # anticipation: settle back and coil
        k = ease_out_cubic(t / 0.17)
        pose.dx = -REACH * 0.17 * k
        pose.stretch = 1.0 - 0.09 * k
        pose.dy = -1.5 * k
    elif t < 0.258:                    # the strike, accelerating
        k = ease_in_cubic((t - 0.17) / 0.088)
        pose.dx = -REACH * 0.17 + REACH * 1.17 * k
        pose.stretch = 0.91 + 0.22 * k
        # An arc, not a rail: the body rises through the travel and comes
        # down onto the target.
        pose.dy = math.sin(math.pi * k) * REACH * 0.20
    elif t < 0.35:                     # impact: overshoot, then absorb it
        k = (t - 0.258) / 0.092
        pose.dx = REACH * (1.0 + 0.06 * (1.0 - k))
        pose.stretch = 1.13 - 0.29 * k
    else:                              # follow through, back to the line
        k = ease_out_cubic((t - 0.35) / 0.65)
        pose.dx = REACH * (1.0 - k)
        pose.stretch = 0.84 + 0.16 * k


def _cast(pose, t):
    """A spell has no impact frame, so it gets lift and hold instead."""
    if t < 0.26:                       # gather, arriving as the spell fires
        k = ease_out_cubic(t / 0.26)
        pose.dy = 9.0 * k
        pose.stretch = 1.0 + 0.10 * k
        pose.dx = REACH * 0.10 * k
    elif t < 0.52:                     # hold, with a breath in it
        k = (t - 0.26) / 0.26
        pose.dy = 9.0 + math.sin(k * math.pi) * 3.0
        pose.stretch = 1.10
        pose.dx = REACH * 0.10
    else:
        k = ease_out_cubic((t - 0.52) / 0.48)
        pose.dy = 9.0 * (1.0 - k)
        pose.stretch = 1.10 - 0.10 * k
        pose.dx = REACH * 0.10 * (1.0 - k)


def _channel(pose, t):
    """A big spell: crouch, haul it up, hold it, and let go hard.

    `cast` rises and holds; this **goes down first**. That is the whole
    difference and it is the same trick `_heavy` uses on the physical side -
    the thing that reads as power is the beat before it, not the size of
    the pose it reaches.
    """
    if t < 0.16:                       # gather down into the ground
        k = ease_out_cubic(t / 0.16)
        pose.dy = -5.0 * k
        pose.stretch = 1.0 - 0.13 * k
    elif t < 0.38:                     # haul it up, growing all the way
        k = ease_in_cubic((t - 0.16) / 0.22)
        pose.dy = -5.0 + 22.0 * k
        pose.stretch = 0.87 + 0.34 * k
    elif t < 0.56:                     # the release: everything drops at once
        k = (t - 0.38) / 0.18
        pose.dy = 17.0 * (1.0 - ease_in_cubic(k)) + 2.0
        pose.stretch = 1.21 - 0.36 * k
        pose.dx = REACH * 0.16 * k
    else:                              # spent, and slow to stand up again
        k = ease_out_cubic((t - 0.56) / 0.44)
        pose.dy = 2.0 * (1.0 - k)
        pose.stretch = 0.85 + 0.15 * k
        pose.dx = REACH * 0.16 * (1.0 - k)


def _bless(pose, t):
    """A heal or a buff: rise, hold, settle - and never step forward.

    `cast` puts `REACH * 0.10` of forward travel into every ability that
    played it, so mending yourself leaned at the enemy. Nothing here moves
    toward anybody: the gesture is upward, and it is the only move in this
    file with no `dx` at all.
    """
    if t < 0.30:
        k = ease_out_cubic(t / 0.30)
        pose.dy = 11.0 * k
        pose.stretch = 1.0 + 0.13 * k
    elif t < 0.66:                     # hold, with a slow swell in it
        k = (t - 0.30) / 0.36
        pose.dy = 11.0 + math.sin(k * math.pi) * 2.6
        pose.stretch = 1.13 + math.sin(k * math.pi) * 0.04
    else:
        k = ease_out_cubic((t - 0.66) / 0.34)
        pose.dy = 11.0 * (1.0 - k)
        pose.stretch = 1.13 - 0.13 * k


def _hurl(pose, t):
    """Wind back and snap - and end up where it started.

    A throw is the one attack where the body must **not** travel: whatever
    was thrown does the travelling (s69), and a thrower who also charges
    has thrown nothing. So the reach here is negative for most of the move
    and the forward part barely leaves the mark.
    """
    if t < 0.30:                       # take it back, and turn into it
        k = ease_out_cubic(t / 0.30)
        pose.dx = -REACH * 0.30 * k
        pose.dy = 3.0 * k
        pose.stretch = 1.0 + 0.10 * k
    elif t < 0.42:                     # the snap
        k = ease_in_cubic((t - 0.30) / 0.12)
        pose.dx = -REACH * 0.30 + REACH * 0.48 * k
        pose.dy = 3.0 * (1.0 - k)
        pose.stretch = 1.10 - 0.22 * k
    else:                              # follow through and recover
        k = ease_out_cubic((t - 0.42) / 0.58)
        pose.dx = REACH * 0.18 * (1.0 - k)
        pose.stretch = 0.88 + 0.12 * k


def _brace(pose, t):
    """Set your feet and take it. Squat, hold wide, stand up again."""
    if t < 0.18:
        k = ease_out_cubic(t / 0.18)
        pose.dy = -4.0 * k
        pose.dx = -KNOCK * 0.16 * k
        pose.stretch = 1.0 - 0.15 * k
    elif t < 0.70:
        pose.dy = -4.0
        pose.dx = -KNOCK * 0.16
        pose.stretch = 0.85
    else:
        k = ease_out_cubic((t - 0.70) / 0.30)
        pose.dy = -4.0 * (1.0 - k)
        pose.dx = -KNOCK * 0.16 * (1.0 - k)
        pose.stretch = 0.85 + 0.15 * k


def _flinch(pose, t):
    """A scratch. Barely moves, and is over before you have read it.

    Not `_recoil` scaled down: a scaled-down spring still rings, and a
    body that oscillates four times over a chip of damage reads as a bug.
    One knock, one return.
    """
    if t < 0.30:
        k = t / 0.30
        pose.dx = -KNOCK * 0.30 * k
        pose.stretch = 1.0 - 0.06 * k
    else:
        k = ease_out_cubic((t - 0.30) / 0.70)
        pose.dx = -KNOCK * 0.30 * (1.0 - k)
        pose.stretch = 0.94 + 0.06 * k


def _reel(pose, t):
    """Knocked properly, and it does not get its feet back at once.

    The stagger is the point and it is one line of maths: the settle is a
    damped spring like `_recoil`'s, but it is **caught halfway** by a
    second small shove, so the body recovers, loses it again, and only
    then straightens. That is what taking a quarter of your health looks
    like, and it is what `_recoil` could never say however hard it was
    pushed.
    """
    if t < 0.12:                       # thrown back, and lifted off the line
        k = ease_in_cubic(t / 0.12)
        pose.dx = -KNOCK * 1.7 * k
        pose.dy = 4.0 * k
        pose.stretch = 1.0 - 0.20 * k
    elif t < 0.34:                     # land badly
        k = (t - 0.12) / 0.22
        pose.dx = -KNOCK * (1.7 - 0.55 * k)
        pose.dy = 4.0 * (1.0 - k)
        pose.stretch = 0.80 + 0.16 * k
    elif t < 0.52:                     # the stagger: lose it again
        k = (t - 0.34) / 0.18
        pose.dx = -KNOCK * (1.15 + 0.30 * math.sin(k * math.pi))
        pose.stretch = 0.96
    else:                              # and finally straighten up
        k = (t - 0.52) / 0.48
        damp = math.exp(-3.6 * k) * math.cos(k * 7.0)
        pose.dx = -KNOCK * 1.15 * damp
        pose.stretch = 1.0 - 0.10 * damp


def _shrug(pose, t):
    """Something arrived and was dealt with.

    A blocked or resisted hit moves the body almost not at all - what it
    does instead is *widen*, which reads as taking the weight rather than
    as being missed. Playing `recoil` for these was the game saying "that
    hurt" over the top of a number saying it did not.
    """
    if t < 0.22:
        k = ease_out_cubic(t / 0.22)
        pose.dx = -KNOCK * 0.12 * k
        pose.stretch = 1.0 - 0.13 * k
        pose.dy = -2.0 * k
    else:
        k = ease_out_cubic((t - 0.22) / 0.78)
        pose.dx = -KNOCK * 0.12 * (1.0 - k)
        pose.stretch = 0.87 + 0.13 * k
        pose.dy = -2.0 * (1.0 - k)


def _crumble(pose, t):
    """Thrown down by something heavy: it travels before it collapses.

    `_fall` sinks where it stands, which is right for most deaths and
    wrong for the one a warhammer caused. Here the body is still moving
    backwards while it goes down, and it hits the ground rather than
    settling onto it - a short skid, then flat.

    **The travel is deliberately about a third of what looks right in
    isolation.** At `KNOCK * 2.1` it read as a body being launched across
    the arena, and on a screenshot the corpse ended up most of the way to
    the next monster's mark - which is the sort of thing only a
    screenshot says (rule 31.4). A death that travels *further than the
    blow that caused it* stops reading as weight and starts reading as a
    physics bug.
    """
    if t < 0.34:                       # airborne, and going over
        k = ease_out_cubic(t / 0.34)
        pose.dx = -KNOCK * 1.25 * k
        pose.dy = 9.0 * math.sin(math.pi * k)
        pose.stretch = 1.0 - 0.30 * k
    elif t < 0.46:                     # the landing
        k = (t - 0.34) / 0.12
        pose.dx = -KNOCK * 1.25
        pose.sink = 9.0 * k
        pose.stretch = 0.70 - 0.22 * k
        pose.alpha = 1.0 - 0.08 * k
    else:                              # a short skid, and then flat
        k = (t - 0.46) / 0.54
        e = ease_out_cubic(k)
        pose.dx = -KNOCK * (1.25 + 0.18 * e)
        pose.sink = 9.0 + 5.0 * e
        pose.stretch = 0.48 - 0.06 * e
        pose.alpha = 0.92 - 0.47 * k


def _wither(pose, t):
    """Unmade rather than knocked over.

    No knock at all, and the fade leads the sink - the opposite order from
    `_fall`: a body killed by magic should look like it is leaving before
    it looks like it is falling. It also **rises slightly first**, which is
    the one beat that stops this reading as a slow `_fall`.
    """
    if t < 0.24:                       # a moment of being held up by it
        k = ease_out_cubic(t / 0.24)
        pose.dy = 5.0 * k
        pose.stretch = 1.0 + 0.16 * k
        pose.alpha = 1.0 - 0.18 * k
    else:
        k = (t - 0.24) / 0.76
        e = ease_in_cubic(k)
        pose.dy = 5.0 * (1.0 - k)
        pose.sink = 11.0 * e
        pose.stretch = 1.16 - 0.62 * e
        pose.alpha = 0.82 - 0.70 * k


def _recoil(pose, t):
    """Knocked away, then a damped spring back.

    Deliberately not symmetric: the knock takes a tenth of the move and the
    settling takes the rest, because that is what being hit by something
    heavier than you looks like.
    """
    if t < 0.10:
        k = t / 0.10
        pose.dx = -KNOCK * k
        pose.stretch = 1.0 - 0.14 * k
    else:
        k = (t - 0.10) / 0.90
        damp = math.exp(-4.4 * k) * math.cos(k * 11.0)
        pose.dx = -KNOCK * damp
        pose.stretch = 1.0 - 0.14 * damp


def _fall(pose, t):
    """Slump and fade.

    No rotation. Turning a 16x16 grid by forty degrees produces a smear of
    half-lit pixels that reads as a rendering fault rather than a body, and
    the flatten-and-sink version says the same thing without fighting the art.
    """
    k = ease_in_cubic(t)
    pose.sink = 13.0 * k
    pose.stretch = 1.0 - 0.42 * k
    pose.dx = -KNOCK * 0.35 * ease_out_cubic(t)
    pose.alpha = 1.0 - 0.55 * k
