"""Free-roam combat: a fight you walk around in (roadmap item 0ak).

**His words**, 2026-09-06, and the second half is the specification:

> "it removes turn based combat and instead you fight openly, select
> which skills you want to use, aim, dodge, etc etc."

And the mode that asked for it a message later (item 0ay):

> "we should add a zombies gamemode, but that wouldnt be turn-based, it
> would be free-roam combat or whatever its called, and ofc it would have
> co-op and no save"

## Why this is a new file rather than a mode inside `battle.py`

§19 named the choice when the item was written: *"a real-time mode either
lives beside it or replaces it, and those are very different projects."*

**It lives beside it**, and that is not a preference:

- `battle.py` is turn-based **to its bones** - a phase machine, an
  `acting_player`, a `turn_order`, a timeline of `after` callbacks. There
  is no arrangement of those that is also a real-time simulation.
- It is what `tools/balance.py` measures the entire game with, at
  thousands of fights a run. A rewrite would invalidate every number this
  project has ever quoted, on the same day, with no way to tell a
  balance change from a bug.
- §59 built `gamemodes.py` **as a seam** for exactly this - his *"extra
  gamemodes"*, plural. A second combat engine behind a mode entry is the
  shape that seam exists for.

So the turn-based game is untouched, and nothing here can move a number
in it.

## What it shares, and what it must

**Damage comes from `battle.compute_damage`.** That is the one thing this
file borrows and the one thing it must: it is where class signatures, run
modifiers, elements, crits and pierce all enter (§2.3), so a Cutpurse who
is a Cutpurse in a turn-based fight is a Cutpurse here too. A second
damage model would be a second game wearing the same character sheet.

Stats, gear, skills, levels and modifiers are the ordinary ones.

## No pygame here either

Hard rule 2.3's reasoning applies unchanged, and more so: a simulation
that can only be tested by opening a window does not get tested. This
file is positions, velocities and cooldowns; `freeroamscreen.py` draws
them and collects the keys.
"""
from __future__ import annotations

import math

from .battle import compute_damage
from .util import clamp, rng

#: The arena, in world units. Deliberately not tiles: nothing here stands
#: on a grid, and borrowing the overworld's tile size would invite
#: somebody to think it does.
ARENA_W, ARENA_H = 960.0, 540.0

#: How fast a body moves, in units a second. The player is quicker than
#: everything except the thing that is meant to catch them.
PLAYER_SPEED = 210.0
#: A dodge is a short burst of speed with no control during it - the
#: commitment is what makes it a decision rather than a second walk.
DODGE_SPEED, DODGE_TIME, DODGE_COOLDOWN = 640.0, 0.16, 0.9
#: Being hit does nothing for this long afterwards, so a body cannot be
#: chewed to death in one frame by a crowd standing on it.
HURT_GRACE = 0.65
#: What a monster's touch is worth against a turn's swing. **Chosen, not
#: measured** - §19 decision 7 is his instruction to pick sane numbers
#: here rather than tune on a harness, and how lethal free-roam should
#: feel is a difficulty question, which rule 31.7 puts with him.
CONTACT_POWER = 0.5
#: A dodge that is *timed* is a dodge that ignores a blow entirely.
#: **Shorter than `DODGE_TIME`, and that is load-bearing**: the check is
#: `dodge > DODGE_TIME - IFRAMES`, so an i-frame window longer than the
#: dodge makes the threshold negative and *every body invulnerable
#: forever*. The first headless run raised 1,281 `evaded` events and
#: took two points of damage in ten seconds, which is what that looks
#: like from outside.
IFRAMES = 0.12

#: A swing reaches this far and covers this much of a circle, in radians.
SWING_REACH, SWING_ARC = 78.0, 1.5
SWING_COOLDOWN = 0.42
#: How long the swing is *drawing* for, which is also how long it can
#: connect. Short: a swing that lingers hits everything twice.
SWING_TIME = 0.18

#: A body's radius, for the only collision test in here.
BODY = 15.0

#: How far a client's own hero may drift from the host's idea of it
#: before it is snapped back (item 0bg). **Generous on purpose**: the
#: point of predicting locally is that walking feels immediate, and a
#: tight leash spends that to fix an error nobody can see.
DRIFT = 46.0


#: How big a piece of cover is, as a radius. Bodies are 15, so this is
#: about three of them wide - big enough to stand behind and small
#: enough to walk round without the arena becoming a corridor.
BLOCK = 46.0


def plan_blockers(plan="open", seed=0):
    """What stands in an arena, as (x, y, radius) circles (item 0bp).

    **Circles, not rectangles**, and that is the whole reason this is
    cheap: pushing a body out of a circle is one normalise and one
    clamp, with no corners to catch on and no axis to resolve first. The
    screen draws them as blocks; the simulation only ever asks how far
    the centre is.

    His words: *"the arenas are far too simple, there should be a LOT of
    different arenas and stuff"*. §79 had answered that with a colour,
    which is not a place - what an arena seen from above needs is
    **things in it**.

    Every plan is arithmetic on the arena's own size, so nothing here is
    a hand-placed level and a new plan is a few lines.
    """
    from .util import RNG
    here = RNG(int(seed) or 1)
    cx, cy = ARENA_W / 2, ARENA_H / 2
    out = []
    if plan == "pillars":
        for row in range(2):
            for col in range(4):
                out.append((ARENA_W * (0.18 + col * 0.215),
                            ARENA_H * (0.30 + row * 0.40), BLOCK * 0.62))
    elif plan == "ring":
        for n in range(8):
            angle = n * math.tau / 8
            out.append((cx + math.cos(angle) * ARENA_W * 0.30,
                        cy + math.sin(angle) * ARENA_H * 0.32,
                        BLOCK * 0.66))
    elif plan == "corners":
        for sx in (0.16, 0.84):
            for sy in (0.22, 0.78):
                out.append((ARENA_W * sx, ARENA_H * sy, BLOCK * 1.05))
    elif plan == "pit":
        # One big thing in the middle: everything is fought around it.
        out.append((cx, cy, BLOCK * 1.7))
    elif plan == "lanes":
        for n in range(3):
            for k in range(3):
                out.append((ARENA_W * (0.26 + n * 0.24),
                            ARENA_H * (0.16 + k * 0.34), BLOCK * 0.5))
    elif plan == "grove":
        for _ in range(11):
            out.append((here.rfloat(BLOCK, ARENA_W - BLOCK),
                        here.rfloat(BLOCK, ARENA_H - BLOCK),
                        here.rfloat(BLOCK * 0.32, BLOCK * 0.6)))
    elif plan == "scatter":
        for _ in range(6):
            out.append((here.rfloat(BLOCK, ARENA_W - BLOCK),
                        here.rfloat(BLOCK, ARENA_H - BLOCK),
                        here.rfloat(BLOCK * 0.45, BLOCK * 0.9)))
    elif plan == "stream":
        # A line of stepping stones across the middle, with a way past
        # each end - cover that also decides which half you are in.
        for n in range(5):
            out.append((ARENA_W * (0.16 + n * 0.17), cy, BLOCK * 0.7))
    elif plan == "shards":
        for _ in range(9):
            out.append((here.rfloat(BLOCK, ARENA_W - BLOCK),
                        here.rfloat(BLOCK, ARENA_H - BLOCK),
                        here.rfloat(BLOCK * 0.28, BLOCK * 0.5)))
    elif plan == "causeway":
        # Two long walls, leaving a road down the middle.
        for side in (0.22, 0.78):
            for n in range(4):
                out.append((ARENA_W * (0.2 + n * 0.2), ARENA_H * side,
                            BLOCK * 0.8))
    elif plan == "gate":
        out.append((cx - ARENA_W * 0.09, cy, BLOCK * 0.8))
        out.append((cx + ARENA_W * 0.09, cy, BLOCK * 0.8))
    return [(round(x, 1), round(y, 1), round(r, 1)) for x, y, r in out]


def _towards(x0, y0, x1, y1):
    """Unit vector from one point to another. (0, 0) when they coincide."""
    dx, dy = x1 - x0, y1 - y0
    gap = math.hypot(dx, dy)
    if gap < 1e-6:
        return 0.0, 0.0, 0.0
    return dx / gap, dy / gap, gap


#: **What a hoard adds to an arena** (item 0bv, §86). Every one of
#: these is off unless the run asks for it, so a night and an island
#: fight are byte for byte the fights they were.
#:
#: How close you have to be to sweep something up. Generous on purpose:
#: the decision in this mode is *how far out do I go*, and it must never
#: be *did I walk over the exact pixel*.
TAKE_REACH = 22.0
#: The camp, and how big it is. Big enough to run into without aiming,
#: and at the bottom of the arena rather than the middle - the trip out
#: and back is the mode.
#: Far enough inside the bottom edge that the whole ring is on the
#: floor - at 0.90 it hung over the border, which a screenshot showed
#: and no test could (rule 31.4).
BANK_AT = (ARENA_W * 0.5, ARENA_H * 0.855)
BANK_REACH = 66.0
#: What carrying costs you, per coin, and the most it can ever cost.
#: **This is the whole tension**: a full load is worth more and gets you
#: killed, and a capped penalty keeps a greedy run playable rather than
#: hopeless.
CARRY_SLOW = 0.018
CARRY_SLOW_CAP = 0.45
#: How long you lie there. **Falling is not the end of a hoard run** -
#: it costs you what you were carrying and four seconds of the clock,
#: which is what makes this a mode about greed rather than survival.
REVIVE_TIME = 4.0
#: How much health you get up with.
REVIVE_SHARE = 0.5
#: The most coins a frame will describe. A floor covered in hundreds is
#: a frame nobody can send twelve times a second, and a hoard that
#: reaches this number has stopped being about the trip anyway.
MAX_SENT_COINS = 90


class Coin:
    """One thing on the floor worth picking up.

    Not an `Item`: this is a number and a position, and the loot a hoard
    run pays out is rolled at the end from what was banked. Making these
    real items would mean a bag that fills up mid-run, which is a
    different and worse mode.
    """

    __slots__ = ("x", "y", "value", "taken", "seed")

    def __init__(self, x, y, value=1, seed=0):
        self.x, self.y = float(x), float(y)
        self.value = int(value)
        self.taken = False
        self.seed = int(seed)


class Body:
    """Anything with a position in the arena.

    Deliberately thin: it holds where something is and what it is doing,
    and the thing it *is* - a `Player` or an `Enemy` - hangs off `who`.
    That keeps every stat, every piece of gear and every modifier in the
    objects the rest of the game already understands.
    """

    __slots__ = ("who", "x", "y", "vx", "vy", "facing", "hurt", "dodge",
                 "dodge_cd", "swing", "swing_cd", "dead", "seed", "kind",
                 "carrying", "down")

    def __init__(self, who, x, y, kind="player", seed=0):
        self.who = who
        self.x, self.y = float(x), float(y)
        self.vx = self.vy = 0.0
        self.facing = 0.0            # radians; where a swing goes
        self.hurt = 0.0              # counts down; no damage while > 0
        self.dodge = 0.0             # counts down; moving fast, no control
        self.dodge_cd = 0.0
        self.swing = 0.0             # counts down; the blow is live
        self.swing_cd = 0.0
        self.dead = False
        self.kind = kind
        self.seed = seed
        #: What this body is holding, and how long it has been down.
        #: Both are only ever moved by a hoard run (§86); every other
        #: fight leaves them at zero and never reads them.
        self.carrying = 0
        self.down = 0.0

    @property
    def alive(self):
        return not self.dead and getattr(self.who, "alive", True)

    @property
    def invulnerable(self):
        """A dodge's first moments ignore a blow entirely.

        **Not the whole dodge.** If the whole thing were invulnerable the
        answer to every attack would be "dodge", and the decision would
        stop being one.
        """
        return self.dodge > 0.0 and self.dodge > DODGE_TIME - IFRAMES

    def advance(self, dt):
        for name in ("hurt", "dodge", "dodge_cd", "swing", "swing_cd"):
            setattr(self, name, max(0.0, getattr(self, name) - dt))

    def move(self, dx, dy, speed, dt, blockers=()):
        """Walk, and stay inside the arena and out of what is in it.

        A dodge overrides the input entirely: you go where you were
        pointed when you pressed it.
        """
        if self.dodge > 0.0:
            dx, dy = math.cos(self.facing), math.sin(self.facing)
            speed = DODGE_SPEED
        length = math.hypot(dx, dy)
        if length > 1e-6:
            dx, dy = dx / length, dy / length
            self.facing = math.atan2(dy, dx)
        self.x = clamp(self.x + dx * speed * dt, BODY, ARENA_W - BODY)
        self.y = clamp(self.y + dy * speed * dt, BODY, ARENA_H - BODY)
        # **Pushed out afterwards, rather than stopped before** (item
        # 0bp). Resolving a move against cover *before* taking it means
        # deciding what a diagonal into a corner does; pushing out after
        # means a body slides along the edge of a thing, which is what
        # walking round a rock feels like and is four lines.
        for bx, by, radius in blockers:
            gap = math.hypot(self.x - bx, self.y - by)
            keep = radius + BODY * 0.62
            if gap >= keep:
                continue
            if gap < 1e-6:
                self.x, self.y = bx + keep, by
                continue
            self.x = clamp(bx + (self.x - bx) / gap * keep,
                           BODY, ARENA_W - BODY)
            self.y = clamp(by + (self.y - by) / gap * keep,
                           BODY, ARENA_H - BODY)


class FreeRoam:
    """One free-roam fight. Arithmetic only; no pygame, no `Game`.

    **Events go out the same way `battle.py`'s do** - `on_event(kind,
    data)` - so the screen, the sound and a co-op mirror all attach the
    way they already know how, and none of them can reach in.
    """

    def __init__(self, player, monsters=(), on_event=None, mods=None,
                 party=None, viewer=0, mirror=False, until_clear=False,
                 hoard=False):
        self.on_event = on_event or (lambda kind, data: None)
        self.mods = mods
        self.elapsed = 0.0
        self.finished = False
        self.result = ""
        #: **Which hero this machine is driving** (item 0bg). Everything
        #: else in the room is somebody else's, and `input` refuses an
        #: index that is not this one on a mirror - the same rule §21
        #: settled for the turn-based fight, where the host taking every
        #: player's turn was one line and three symptoms.
        self.viewer = int(viewer)
        #: **A mirror shows; it does not decide.** No monster walks, no
        #: damage lands and nothing dies on a client - all of that is the
        #: host's, and this side draws what arrives (§21).
        self.mirror = bool(mirror)
        side = party if party is not None else [player]
        self.heroes = [
            Body(who, ARENA_W * (0.5 + 0.09 * (n - (len(side) - 1) / 2)),
                 ARENA_H * 0.75, "player", seed=n)
            for n, who in enumerate(side)]
        self.foes = []
        for n, monster in enumerate(monsters):
            self.spawn(monster, n)
        #: What each hero has queued: a skill key, spent on the next swing.
        self.charged = {}
        self.cooldowns = {}
        #: **Whether killing everything ends it** (item 0bi). An island
        #: fight is a finite pack and finishes when the pack does; a
        #: Long Night is a crowd that keeps arriving, and the mode says
        #: when a night is over. One flag, because the difference is one
        #: sentence and not two engines.
        self.until_clear = bool(until_clear)
        self.rewards = None
        #: What stands in this arena (item 0bp). Empty is a bare plate,
        #: which is what every free-roam fight was until now.
        self.blockers = []
        #: **A hoard run** (item 0bv, §86): coins on the floor, a camp to
        #: carry them to, and a fall that costs you your load instead of
        #: the run. One flag, because the difference between this and a
        #: night is a handful of rules rather than a second engine - the
        #: same call `until_clear` already made.
        self.hoard = bool(hoard)
        self.loot = []
        self.bank = BANK_AT
        #: What each hero has got home, by index. A dict rather than a
        #: number because a room banks separately and the score is the
        #: room's - see `HoardRun.score`.
        self.banked = {}

    # -- the cast ----------------------------------------------------------

    def spawn(self, monster, seed=0, at=None):
        """Put a monster in the arena, at the edge unless told otherwise.

        **At the edge**, because something appearing on top of you is
        not a fight, it is a tax - the same reasoning §68 used for the
        overworld's visible monsters.
        """
        if at is None:
            side = rng.rint(0, 3)
            if side == 0:
                at = (rng.rfloat(0, ARENA_W), BODY)
            elif side == 1:
                at = (rng.rfloat(0, ARENA_W), ARENA_H - BODY)
            elif side == 2:
                at = (BODY, rng.rfloat(0, ARENA_H))
            else:
                at = (ARENA_W - BODY, rng.rfloat(0, ARENA_H))
        body = Body(monster, at[0], at[1], "enemy", seed=seed)
        self.foes.append(body)
        self.emit("spawn", index=len(self.foes) - 1)
        return body

    def clear_camp(self):
        """Take any cover that is standing in the camp out of it.

        **The arenas were drawn before this mode existed** and their
        floor plans put blocks wherever the plan says, including on top
        of the one circle a hoard run has to be able to run into. A
        screenshot of the first hoard had a wall in the middle of it.
        """
        if not self.hoard or not self.blockers:
            return 0
        kept, removed = [], 0
        for blocker in self.blockers:
            bx, by, radius = blocker[0], blocker[1], blocker[2]
            if math.hypot(bx - self.bank[0],
                          by - self.bank[1]) < BANK_REACH + radius:
                removed += 1
                continue
            kept.append(blocker)
        self.blockers = kept
        return removed

    def scatter(self, count, value=1):
        """Put `count` coins about the arena, away from the camp.

        **Never in the camp.** A coin you can bank without leaving is a
        coin that is already banked, and this mode is the trip.
        """
        made = []
        for _ in range(max(0, int(count))):
            for _attempt in range(12):
                x = rng.rfloat(BODY * 2, ARENA_W - BODY * 2)
                y = rng.rfloat(BODY * 2, ARENA_H - BODY * 2)
                if math.hypot(x - self.bank[0],
                              y - self.bank[1]) > BANK_REACH * 2.2:
                    break
            coin = Coin(x, y, value, seed=len(self.loot))
            self.loot.append(coin)
            made.append(coin)
            self.emit("coin", index=len(self.loot) - 1)
        return made

    def spill(self, body, count=None):
        """Drop what somebody is carrying, where they are.

        **What a fall costs.** Scattered around the spot rather than
        stacked on it, so getting it back is a small job and not one
        step - and so a body lying on top of its own load does not hide
        it.
        """
        count = body.carrying if count is None else int(count)
        count = max(0, min(int(body.carrying), count))
        body.carrying -= count
        for n in range(count):
            angle = (n / max(1, count)) * math.tau + rng.rfloat(0.0, 0.6)
            reach = rng.rfloat(BODY, BODY * 3.4)
            self.loot.append(Coin(
                clamp(body.x + math.cos(angle) * reach,
                      BODY, ARENA_W - BODY),
                clamp(body.y + math.sin(angle) * reach,
                      BODY, ARENA_H - BODY),
                1, seed=len(self.loot)))
        if count:
            self.emit("spill", index=self.heroes.index(body), count=count)
        return count

    def gather(self):
        """Sweep up what heroes are standing on, and bank what they carry.

        Runs on the host only - like everything else that decides
        something - and the mirror is told by the frame.
        """
        if not self.hoard:
            return
        alive = [b for b in self.heroes if b.alive]
        for coin in self.loot:
            if coin.taken:
                continue
            for body in alive:
                if math.hypot(coin.x - body.x, coin.y - body.y) <= TAKE_REACH:
                    coin.taken = True
                    body.carrying += coin.value
                    self.emit("took", index=self.heroes.index(body),
                              value=coin.value)
                    break
        if any(coin.taken for coin in self.loot):
            self.loot = [coin for coin in self.loot if not coin.taken]
        for body in alive:
            if not body.carrying:
                continue
            if math.hypot(body.x - self.bank[0],
                          body.y - self.bank[1]) <= BANK_REACH:
                from . import gamemodes
                index = self.heroes.index(body)
                # **Valued here, where the trip is known.** A total
                # cannot say how it got there, and a big trip is worth
                # more per coin (§87) - so the frame carries what the
                # haul is *worth* and a guest never has to guess.
                worth = gamemodes.hoard_trip_value(body.carrying)
                self.banked[index] = self.banked.get(index, 0) + worth
                self.emit("banked", index=index, count=body.carrying,
                          worth=worth)
                body.carrying = 0

    def carried_total(self):
        return sum(b.carrying for b in self.heroes)

    def banked_total(self):
        return sum(self.banked.values())

    def carry_speed(self, body):
        """How fast somebody moves with their arms full."""
        if not self.hoard or not body.carrying:
            return PLAYER_SPEED
        slow = min(CARRY_SLOW_CAP, CARRY_SLOW * body.carrying)
        return PLAYER_SPEED * (1.0 - slow)

    def emit(self, kind, **data):
        self.on_event(kind, data)

    @property
    def living_foes(self):
        return [b for b in self.foes if b.alive]

    @property
    def standing(self):
        return [b for b in self.heroes if b.alive]

    def hero(self, index=0):
        return self.heroes[index] if 0 <= index < len(self.heroes) else None

    # -- what a player asks for --------------------------------------------

    def input(self, index, dx=0.0, dy=0.0, swing=False, dodge=False,
              skill=None, dt=1 / 60.0):
        """One hero's intent for one frame.

        **Everything a player can do arrives here**, which is what keeps
        a mirror honest: a machine that is not driving this hero simply
        never calls it.
        """
        body = self.hero(index)
        if body is None or not body.alive or self.finished:
            return
        if self.mirror and index != self.viewer:
            # **A mirror drives one body: its own.** Everybody else's
            # position arrives in a frame, and a client that also
            # simulated them would fight a different room from the host.
            return
        if dodge and body.dodge <= 0.0 and body.dodge_cd <= 0.0:
            body.dodge, body.dodge_cd = DODGE_TIME, DODGE_COOLDOWN
            self.emit("dodge", index=index)
        if skill is not None:
            self.charge(index, skill)
        body.move(dx, dy, self.carry_speed(body), dt, self.blockers)
        if swing:
            self.swing(index)

    def charge(self, index, skill):
        """Hold a skill for the next swing.

        **A skill is a swing with something behind it**, rather than a
        second button that damages on its own. That is the smallest
        change from the turn-based game that still answers *"select which
        skills you want to use"*: the same abilities, the same costs, and
        aiming still matters.
        """
        body = self.hero(index)
        if body is None or skill is None:
            return False
        if self.cooldowns.get((index, skill.key), 0.0) > 0.0:
            self.emit("refused", index=index, why="cooling")
            return False
        if body.who.mp < skill.mp:
            self.emit("refused", index=index, why="mp")
            return False
        self.charged[index] = skill
        self.emit("charged", index=index, skill=skill.key)
        return True

    def swing(self, index):
        """Attack in the direction this hero is facing."""
        body = self.hero(index)
        if body is None or body.swing_cd > 0.0 or body.dodge > 0.0:
            return False
        skill = self.charged.pop(index, None)
        if skill is not None and body.who.mp < skill.mp:
            skill = None
        body.swing, body.swing_cd = SWING_TIME, SWING_COOLDOWN
        if skill is not None:
            body.who.mp = max(0, body.who.mp - skill.mp)
            self.cooldowns[(index, skill.key)] = self.skill_cooldown(skill)
        self.emit("swing", index=index, facing=body.facing,
                  skill=(skill.key if skill else ""),
                  reach=self.reach_of(skill), arc=self.arc_of(skill))
        self.resolve_swing(index, skill)
        return True

    @staticmethod
    def skill_cooldown(skill):
        """How long a skill rests. **MP is not the only cost here.**

        In a turn-based fight the cost of a spell is a turn plus its MP;
        in real time there are no turns, so without this the answer to
        everything is to hold the strongest skill and mash. Bigger costs
        more, which is the same shape the MP number already has.
        """
        return clamp(1.2 + float(getattr(skill, "power", 1.0)), 1.2, 6.0)

    @staticmethod
    def reach_of(skill):
        """How far a swing carries (item 0by).

        Savvydev, 2026-09-08: *"different skills should have like different
        ranges and stuff"*. This used to be **two buckets** - a spell
        reached 1.7x and everything else 1.25x - so a Power Strike and a
        Riposte had the same range, and choosing between them was a
        choice about damage only.

        **Derived from the skill's own numbers**, the way `skill_look`
        derives a shape from element and power (§ item 0ba), so a new
        skill has a range the day it exists and there is no table to
        keep:

        - **A spell carries.** Something that scales on `mag`, or throws
          an element that is not `physical`, is a thing that leaves your
          hand.
        - **A heavy blow leans in.** Power above 1 buys a little reach,
          because a two-handed swing covers ground a jab does not.
        - **A flurry does not.** Several small hits are close work, and
          `hits` pulls the range back in.

        Everything is clamped: nothing gets to be a sniper, and nothing
        gets shorter than an arm.
        """
        if skill is None:
            return SWING_REACH
        power = float(getattr(skill, "power", 1.0) or 1.0)
        hits = max(1, int(getattr(skill, "hits", 1) or 1))
        far = 1.0
        if getattr(skill, "scales", "atk") == "mag":
            far += 0.70
        if getattr(skill, "element", "physical") != "physical":
            far += 0.25
        far += clamp((power - 1.0) * 0.30, -0.15, 0.45)
        far -= 0.08 * (hits - 1)
        return SWING_REACH * clamp(far, 0.85, 2.30)

    @staticmethod
    def arc_of(skill):
        """How wide a swing sweeps (item 0by).

        **The other half of a range, and it was one number for the whole
        game.** A bolt and a cleave reached differently and covered the
        same wedge, which is why the reach alone never read as *this
        skill is a different shape*.

        A spell narrows to a line; weight and extra hits widen. The two
        pull against each other on purpose: the long things are thin and
        the wide things are close, so there is no skill that is simply
        better in the arena than another one.
        """
        if skill is None:
            return SWING_ARC
        power = float(getattr(skill, "power", 1.0) or 1.0)
        hits = max(1, int(getattr(skill, "hits", 1) or 1))
        wide = 1.0
        if getattr(skill, "scales", "atk") == "mag":
            wide -= 0.42
        wide += clamp((power - 1.0) * 0.22, -0.10, 0.40)
        wide += 0.12 * (hits - 1)
        return SWING_ARC * clamp(wide, 0.45, 1.70)

    def resolve_swing(self, index, skill=None):
        """Everything inside the arc takes it.

        **An arc, not a target.** The whole difference between this and
        `battle.py` is that nothing is selected: what you hit is whatever
        was in front of you when you pressed it, which is what *"aim"*
        means in his sentence.
        """
        body = self.hero(index)
        if body is None:
            return []
        reach = self.reach_of(skill)
        arc = self.arc_of(skill)
        hit = []
        for foe in self.living_foes:
            dx, dy, gap = _towards(body.x, body.y, foe.x, foe.y)
            if gap > reach + BODY:
                continue
            if abs(_angle_gap(math.atan2(dy, dx), body.facing)) > arc / 2:
                continue
            hit.append(foe)
        for foe in hit:
            self.hurt_foe(body, foe, skill)
        if not hit:
            self.emit("miss", index=index)
        return hit

    # -- damage ------------------------------------------------------------

    def hurt_foe(self, body, foe, skill=None):
        """**Through `compute_damage`, always.** See the module docstring."""
        power = float(getattr(skill, "power", 1.0) or 1.0)
        result = compute_damage(
            body.who, foe.who, power=power,
            scales=(skill.scales if skill else "atk"),
            element=(skill.element if skill else body.who.element()),
            defender_element=getattr(foe.who, "element", "physical"))
        foe.who.hp = max(0, foe.who.hp - result.amount)
        ceiling = getattr(foe.who, "max_hp", 0)
        ceiling = ceiling() if callable(ceiling) else ceiling
        share = result.amount / max(1, int(ceiling or 1))
        self.emit("hit", who="enemy", index=self.foes.index(foe),
                  amount=result.amount, crit=result.crit, share=share,
                  element=(skill.element if skill else "physical"))
        if not foe.who.alive:
            foe.dead = True
            self.emit("felled", index=self.foes.index(foe))
        return result

    def hurt_hero(self, foe, body):
        """A monster reaches somebody. Grace and i-frames are checked here."""
        if body.hurt > 0.0 or body.invulnerable:
            if body.invulnerable:
                self.emit("evaded", index=self.heroes.index(body))
            return None
        # **A touch is not a sword blow.** `battle.py` prices a monster's
        # turn at `power=1.0` because it gets one per round; here a body
        # that has reached you connects every `HURT_GRACE`, which is
        # roughly twice as often. At full power four level-eight monsters
        # emptied a level-eight warden in **five seconds** of walking
        # into them - the first headless run did exactly that.
        result = compute_damage(foe.who, body.who, power=CONTACT_POWER,
                                element=getattr(foe.who, "element",
                                                "physical"))
        body.who.hp = max(0, body.who.hp - result.amount)
        body.hurt = HURT_GRACE
        self.emit("hit", who="player", index=self.heroes.index(body),
                  amount=result.amount, crit=result.crit,
                  share=result.amount / max(1, body.who.max_hp()),
                  element=getattr(foe.who, "element", "physical"))
        if not body.who.alive:
            body.dead = True
            if self.hoard:
                # **A fall costs your load, not the run** (§86). The
                # clock is the only thing that ends a hoard, which is
                # what makes the decision *how far out do I go* rather
                # than *can I stay alive*.
                body.down = REVIVE_TIME
                self.spill(body)
            self.emit("fell", index=self.heroes.index(body))
        return result

    # -- the clock ---------------------------------------------------------

    def monster_speed(self, foe):
        """How fast one monster comes at you.

        Off its own `spd`, so a fast archetype is fast here too - and
        capped **below** the player's walk, because a monster that can
        run you down turns dodging from a skill into a formality.
        """
        speed = 52.0 + 4.6 * float(getattr(foe.who, "stat", lambda k: 8)("spd"))
        return clamp(speed, 52.0, PLAYER_SPEED * 0.82)

    def update(self, dt):
        """One frame of the world."""
        if self.finished:
            return
        dt = max(0.0, min(0.05, float(dt)))
        self.elapsed += dt
        if self.mirror:
            # **Timers only.** A mirror runs the clocks so that a dodge
            # and a swing finish drawing, and does nothing else: no
            # monster moves, nothing is hurt and nothing dies here.
            for body in self.heroes + self.foes:
                body.advance(dt)
            return
        for key in list(self.cooldowns):
            self.cooldowns[key] = max(0.0, self.cooldowns[key] - dt)
        for body in self.heroes:
            body.advance(dt)
            if self.hoard and body.dead and body.down > 0.0:
                body.down = max(0.0, body.down - dt)
                if body.down <= 0.0:
                    self.stand_up(body)
        for foe in self.foes:
            foe.advance(dt)
            if not foe.alive:
                continue
            target = self.nearest_hero(foe)
            if target is None:
                continue
            dx, dy, gap = _towards(foe.x, foe.y, target.x, target.y)
            if gap > BODY * 1.9:
                # **A monster slides round cover rather than solving
                # it.** No pathfinding: it walks at you and the push-out
                # takes it round the edge of whatever is in the way,
                # which is enough for a thing that wants to reach you
                # and is not enough to make cover useless.
                foe.move(dx, dy, self.monster_speed(foe), dt, self.blockers)
            else:
                # **Touching is what hurts.** No monster swing, no
                # windup: a zombie that has reached you is doing damage,
                # and the grace timer is what stops that being instant
                # death in a crowd.
                foe.facing = math.atan2(dy, dx)
                self.hurt_hero(foe, target)
        self.separate(dt)
        self.gather()
        self.check_over()

    def stand_up(self, body):
        """Somebody gets back on their feet (§86, hoard runs only).

        Half health, at the camp, because a body that revives where it
        fell revives inside the crowd that put it there - and the walk
        back is the price, on a clock that is not stopping.
        """
        body.who.hp = max(1, int(body.who.max_hp() * REVIVE_SHARE))
        body.dead = False
        body.down = 0.0
        body.hurt = HURT_GRACE
        body.x, body.y = self.bank
        self.emit("stood", index=self.heroes.index(body))

    def separate(self, dt):
        """Push overlapping monsters apart.

        **Without this a crowd is one monster with several health bars.**
        Everything chases the same point, so everything arrives at the
        same point - a screenshot of night five showed eight of them
        stacked inside one sprite, with the hero somewhere underneath
        (§76, rule 31.4 again).

        Deliberately the cheapest thing that works: one pass, each pair
        nudged apart by half the overlap. It is not a physics solver and
        must not become one - what it has to buy is *"you can see how
        many there are and get between them"*, which is the whole
        difference between a crowd and a wall.
        """
        alive = self.living_foes
        for i, one in enumerate(alive):
            for other in alive[i + 1:]:
                dx, dy, gap = _towards(one.x, one.y, other.x, other.y)
                if gap <= 1e-6:
                    # **Exactly on top of each other**, which is the case
                    # that matters and the one the first version skipped:
                    # `_towards` has no direction to offer, so the guard
                    # `gap <= 0` meant two coincident bodies stayed
                    # coincident forever. A deterministic angle off their
                    # indices, so nothing here rolls the shared stream.
                    angle = (i * 2.39996 + len(alive)) % (2 * math.pi)
                    dx, dy, gap = math.cos(angle), math.sin(angle), 0.01
                overlap = BODY * 1.8 - gap
                if overlap <= 0.0:
                    continue
                push = min(overlap * 0.5, 90.0 * dt)
                one.x = clamp(one.x - dx * push, BODY, ARENA_W - BODY)
                one.y = clamp(one.y - dy * push, BODY, ARENA_H - BODY)
                other.x = clamp(other.x + dx * push, BODY, ARENA_W - BODY)
                other.y = clamp(other.y + dy * push, BODY, ARENA_H - BODY)

    # -- the wire ----------------------------------------------------------

    def snapshot(self):
        """The whole arena, small enough to send a dozen times a second.

        **Positions and health, nothing else** (item 0bg). Not the
        monsters' stats, not the loot, not who is holding which skill -
        a client draws this and asks the host for everything it cannot
        see, which is the same division §21 settled for a shared battle.
        """
        frame = {"t": round(self.elapsed, 2),
                 "h": [_pack(b) for b in self.heroes],
                 "e": [_pack(b) for b in self.foes],
                 "over": 1 if self.finished else 0}
        if self.hoard:
            # **What is on the floor, and what everybody is holding**
            # (§86). A guest that had to guess would draw coins that are
            # not there and miss the ones that are - and *what you are
            # carrying* is the only number in this mode that matters
            # while you are still out there.
            frame["c"] = [[int(coin.x), int(coin.y), coin.value]
                          for coin in self.loot[:MAX_SENT_COINS]]
            frame["hold"] = [int(b.carrying) for b in self.heroes]
            frame["bank"] = [int(self.banked.get(n, 0))
                             for n in range(len(self.heroes))]
        return frame

    def apply_snapshot(self, frame, keep_own=True):
        """Client side: take the host's word for where everything is.

        `keep_own` is **local prediction, and it is the one concession to
        the network in this file**: frames arrive a dozen times a second
        and a body that only moved when one landed would stutter visibly.
        So this machine keeps driving its own hero and accepts the host's
        position for it **only when the two have drifted far enough that
        the difference is real** - a wrong position you can feel is worse
        than a late one you cannot.
        """
        frame = frame or {}
        for name, bodies in (("h", self.heroes), ("e", self.foes)):
            rows = frame.get(name) or []
            for index, row in enumerate(rows):
                if index >= len(bodies):
                    if name != "e":
                        # A hero the client does not have is a roster it
                        # has not received yet, and inventing one would
                        # put a stranger in the party. Wait for it.
                        break
                    bodies.append(Body(
                        Ghost(str(row.get("n") or "slime"),
                              int(row.get("h") or 1),
                              int(row.get("m") or 1)),
                        float(row.get("x", 0.0)), float(row.get("y", 0.0)),
                        "enemy", seed=index))
                body = bodies[index]
                mine = (name == "h" and index == self.viewer and keep_own)
                x = float(row.get("x", body.x))
                y = float(row.get("y", body.y))
                if not mine or math.hypot(x - body.x, y - body.y) > DRIFT:
                    body.x, body.y = x, y
                    if not mine:
                        body.facing = float(row.get("f", body.facing))
                body.who.hp = max(0, int(row.get("h", body.who.hp)))
                body.dead = bool(row.get("d"))
                if not mine:
                    body.swing = float(row.get("s", 0.0))
                    body.dodge = float(row.get("g", 0.0))
        if self.hoard:
            # **A mirror draws the floor it is told about** (§86). Coins
            # are decided by the host like everything else, so a client
            # that kept its own list would draw money that is not there
            # and walk past money that is.
            rows = frame.get("c")
            if rows is not None:
                self.loot = [Coin(float(row[0]), float(row[1]),
                                  int(row[2]) if len(row) > 2 else 1, seed=n)
                             for n, row in enumerate(rows)
                             if isinstance(row, (list, tuple))
                             and len(row) >= 2]
            for name, take in (("hold", "carrying"), ("bank", None)):
                values = frame.get(name) or []
                for index, value in enumerate(values):
                    if index >= len(self.heroes):
                        break
                    if take:
                        self.heroes[index].carrying = max(0, int(value))
                    else:
                        self.banked[index] = max(0, int(value))
        if frame.get("over"):
            self.finished = True

    def nearest_hero(self, foe):
        best, best_gap = None, 1e9
        for body in self.standing:
            _dx, _dy, gap = _towards(foe.x, foe.y, body.x, body.y)
            if gap < best_gap:
                best, best_gap = body, gap
        return best

    def check_over(self):
        if self.hoard:
            # **Nobody loses a hoard by falling** - they lose what they
            # were carrying. The mode's clock finishes it, from outside,
            # which is why there is nothing here.
            return False
        if not self.standing:
            self.finished, self.result = True, "defeat"
            self.emit("over", result="defeat")
            return True
        if self.until_clear and self.foes and not self.living_foes:
            self.finished, self.result = True, "victory"
            self.rewards = self.split_rewards()
            self.emit("over", result="victory")
            return True
        return False

    # -- the outcome, in the shape the rest of the game already reads ------
    #
    # **One payout path, two engines** (item 0bi). §54 records the cost of
    # the alternative: *"two payout paths, and the rarer one keeps getting
    # patched first"* - Mortal shipped handling defeat in one of them, and
    # §53's new-skill question went into the other. `Game.finish_battle`
    # does levelling, loot, quests and achievements, and none of that is
    # different because the fight was real time.
    #
    # So this presents the same four things a finished `Battle` does, and
    # the payout code does not learn that a second engine exists.

    @property
    def enemies(self):
        """The monsters, as the objects the rest of the game knows."""
        return [body.who for body in self.foes]

    @property
    def players(self):
        return [body.who for body in self.heroes]

    @property
    def round(self):
        """There are no rounds. One, for the log line that asks."""
        return 1

    def player_index(self, member):
        for index, body in enumerate(self.heroes):
            if body.who is member:
                return index
        return 0

    def participants(self):
        """Everybody who was in it. Nobody can flee a free-roam fight."""
        return [body.who for body in self.heroes]

    def split_rewards(self):
        """Evenly among everyone who fought - his rule, unchanged.

        Deliberately the same arithmetic as `Battle.split_rewards`,
        including the remainder going to the earliest participant, so a
        party of three splitting 100 XP gets 34/33/33 in both engines
        rather than in one.
        """
        total_xp = sum(int(getattr(m, "xp_reward", 0)) for m in self.enemies)
        total_gold = sum(int(getattr(m, "gold_reward", 0))
                         for m in self.enemies)
        earners = self.participants()
        shares = {}
        if not earners:
            return {"xp": total_xp, "gold": total_gold, "shares": shares,
                    "earners": []}
        count = len(earners)
        xp_each, xp_left = divmod(total_xp, count)
        gold_each, gold_left = divmod(total_gold, count)
        for i, member in enumerate(earners):
            shares[self.player_index(member)] = {
                "xp": xp_each + (1 if i < xp_left else 0),
                "gold": gold_each + (1 if i < gold_left else 0)}
        return {"xp": total_xp, "gold": total_gold, "shares": shares,
                "earners": [self.player_index(m) for m in earners]}

    def reward_for(self, member):
        """This player's cut, as {"xp", "gold"}."""
        shares = (self.rewards or {}).get("shares", {})
        return shares.get(self.player_index(member), {"xp": 0, "gold": 0})


#: How much of a body crosses the wire. **Short keys on purpose**: a
#: frame goes out a dozen times a second to every peer, and `net.py`
#: drops a peer that talks too much (§7) - so the thing to be careful
#: about is size, not elegance.
def _pack(body):
    ceiling = getattr(body.who, "max_hp", 0)
    ceiling = ceiling() if callable(ceiling) else ceiling
    return {"x": round(body.x, 1), "y": round(body.y, 1),
            "f": round(body.facing, 2), "h": int(body.who.hp),
            "d": 1 if body.dead else 0,
            "s": round(body.swing, 2), "g": round(body.dodge, 2),
            # **Enough to stand a stranger up.** A mirror is told about a
            # monster it has never heard of by the same frame that moves
            # the ones it has - so a dropped spawn message cannot leave a
            # client fighting an arena with a hole in it.
            "n": str(getattr(body.who, "sprite", "slime"))[:24],
            "m": int(ceiling or 1)}


class Ghost:
    """A monster a client has been told about but never rolled.

    **Deliberately not an `Enemy`.** A client has no business knowing a
    monster's stats - it never computes damage, and inventing an `Enemy`
    from a sprite name would be a second, wrong bestiary. This holds the
    two things the screen asks for and nothing else.
    """

    __slots__ = ("sprite", "hp", "_max", "element", "name", "level")

    def __init__(self, sprite="slime", hp=1, ceiling=1):
        self.sprite = sprite
        self.hp = int(hp)
        self._max = max(1, int(ceiling))
        self.element = "physical"
        self.name = "?"
        self.level = 1

    def max_hp(self):
        return self._max

    @property
    def alive(self):
        return self.hp > 0


def _angle_gap(a, b):
    """The shortest signed angle from `b` to `a`, in radians."""
    gap = (a - b + math.pi) % (2 * math.pi) - math.pi
    return gap
