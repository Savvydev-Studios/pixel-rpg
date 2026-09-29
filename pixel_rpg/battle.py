"""Turn-based battle: initiative, skills, statuses and animation.

The original resolved a whole exchange inside one keypress handler. Here an
action schedules a short timeline of steps - wind-up, impact, damage, recoil -
so hits land with weight, and the battle state machine stays readable.

**A fight is a group against a group.** A lone monster is a group of one and
so is a solo player, so there is a single code path rather than a special
case: `self.enemies` and `self.players` are always lists.

**Three different "who" ideas live here and must not be merged.** Section 15
of the handoff records what happened when two of them shared one attribute -
the second monster in a group swung on the first's behalf - so each is named
for what it is:

  * `self.actor`   - the player whose **turn it is**. Every player action
    reads this: it is who is paying, swinging and being healed.
  * `self.you`     - the player **this machine is looking through**. Only the
    renderer wants this. In solo they are the same object, which is exactly
    why confusing them is easy and costly.
  * the **victim**, passed explicitly into `apply_damage_to_player`, which is
    whichever player a monster picked. Never stored on `self`.

and on the other side:

  * `self.enemy`   - who `self.actor` is aiming at.
  * the **acting enemy**, passed explicitly into `_resolve_enemy`.

The rule is the same one every time: if a thing can differ from another thing
during a single round, it does not get to share an attribute with it.

**Sides act in order, not interleaved.** Every player acts, then every living
monster acts. A true initiative order across both sides would be a different
game from the one `tools/balance.py` measured, and the encounter caps in
`entities.max_enemies()` were set from those numbers. Within a side the order
is fastest-first.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from . import anim
from .entities import Boss, element_multiplier
from .items import CONSUMABLES
from .skills import (ENEMY_SKILLS, HP_COST_SKILLS, SPECIAL_SKILLS,
                     STATUSES, Status)
from .util import clamp, rng

# Battle phases
INTRO = "intro"
MENU = "menu"
SUBMENU = "submenu"
ACTING = "acting"
VICTORY = "victory"
DEFEAT = "defeat"
FLED = "fled"

MENU_ACTIONS = ("Attack", "Skill", "Item", "Defend", "Flee")


def menu_actions_for(battle):
    """The rows this fight actually offers.

    **A row you cannot press should not be there.** Savvydev, 2026-08-31:
    *"remove the flee option entirely when you have the no flee modifier
    on"*. Oathkeeper's whole cost is that you cannot run, and the menu was
    still offering Flee and then saying no - which reads as a bug rather
    than as a rule, and costs a keypress every fight to find out.

    A warden arena refuses fleeing too and keeps the row on purpose: that
    is one fight saying no, not a run-long rule, and hiding it there would
    make the menu change shape as you walk around.
    """
    actor = battle.actor
    mods = getattr(actor, "modifiers", None)
    if mods is not None and mods.has("no_flee"):
        return tuple(a for a in MENU_ACTIONS if a != "Flee")
    return MENU_ACTIONS

# How much a monster prefers a hurt target. A hero at full health weighs 1
# and one on their last sliver weighs 1 + this, so at 2.0 the wounded are
# roughly three times as likely to be hit - a real lean, never a certainty.
# Turned up, downed players cascade and there is nothing left to decide;
# turned off, damage spreads evenly across a party and kills nobody, which
# is most of why a full room won every fight in section 23.
FOCUS_LEAN = 2.0
PHASES = (INTRO, MENU, SUBMENU, ACTING, VICTORY, DEFEAT, FLED)


def _body(combatant):
    """One combatant, as the few numbers a mirror has to draw."""
    return {
        "hp": int(combatant.hp),
        "mp": int(getattr(combatant, "mp", 0)),
        "st": [[st.key, int(st.turns), int(st.magnitude)]
               for st in combatant.statuses[:8]],
    }


def _one_of(value, allowed, fallback):
    return value if value in allowed else fallback


def _index(value, size, fallback):
    """A whole number in range, or the value already held.

    Clamping would be wrong here for the same reason it is wrong for a tile
    coordinate: silently moving an out-of-range index to the nearest legal
    one is not rejecting a bad frame, it is acting on a different one.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return fallback
    return value if 0 <= value < size else fallback


def _apply_bodies(combatants, rows):
    """Push the host's numbers onto this machine's copies of them."""
    if not isinstance(rows, list):
        return
    for combatant, row in zip(combatants, rows[:len(combatants)]):
        if not isinstance(row, dict):
            continue
        hp = row.get("hp")
        if isinstance(hp, int) and not isinstance(hp, bool):
            combatant.hp = int(clamp(hp, 0, combatant.max_hp()))
        mp = row.get("mp")
        if isinstance(mp, int) and not isinstance(mp, bool) \
                and hasattr(combatant, "mp"):
            combatant.mp = int(clamp(mp, 0, combatant.max_mp()))
        rolls = row.get("st")
        if not isinstance(rolls, list):
            continue
        combatant.statuses = []
        for entry in rolls[:8]:
            if not isinstance(entry, list) or len(entry) != 3:
                continue
            key, turns, magnitude = entry
            if key not in STATUSES:
                continue
            if not all(isinstance(v, int) and not isinstance(v, bool)
                       for v in (turns, magnitude)):
                continue
            combatant.statuses.append(
                Status(key, int(clamp(turns, 0, 99)),
                       int(clamp(magnitude, 0, 99999))))


@dataclass
class DamageResult:
    amount: int = 0
    crit: bool = False
    element_mult: float = 1.0
    absorbed: int = 0
    blocked: bool = False
    lifesteal: int = 0


def compute_damage(attacker, defender, power=1.0, scales="atk",
                   element="physical", defender_element="physical",
                   can_crit=True, force_crit=False, pierce=0.0):
    """Shared damage model for every attack in the game.

    Class signatures and run modifiers enter here as two multipliers - one on
    the attacker, one on the defender - so every source of damage in the game
    respects them without each call site having to remember.
    """
    offense = attacker.stat(scales)
    guard = defender.stat("res" if scales == "mag" else "defense")
    # **A class's own swing may ignore part of the guard** (item 0ai).
    # Here rather than at the call site, because this is the only function
    # that knows what a guard is worth - a `pierce` applied outside would
    # have to reimplement the subtraction below and would drift from it.
    guard *= max(0.0, 1.0 - float(pierce))

    # Defense subtracts, but never to zero: chip damage always gets through.
    base = offense * power
    raw = max(base * 0.18, base - guard * 0.62)
    raw *= rng.rfloat(0.90, 1.12)
    raw *= getattr(attacker, "outgoing_multiplier", lambda: 1.0)()
    raw *= getattr(defender, "incoming_multiplier", lambda: 1.0)()
    # **The two signatures that need to see the other side** (s52). Every
    # other one is a number a combatant knows about itself and lives in
    # `entities.py`; these two are about *who you are hitting* and about
    # *how many of them there are*, so this is the only place that can
    # answer them. Both read through `getattr` because a monster is an
    # attacker here too and has no signature at all.
    sig = getattr(attacker, "signature", lambda: "")()
    if sig == "affliction" and getattr(defender, "statuses", None):
        raw *= 1.35
    elif sig == "singlecombat":
        # `duel_odds` is set once per turn by `Battle.start_turn`. A
        # Duelist that walked into a brawl is worse off for it, on purpose
        # - see the note in handoff s31.9.
        raw *= 1.35 if getattr(attacker, "duel_odds", 1) <= 1 else 0.85

    result = DamageResult()
    result.element_mult = element_multiplier(element, defender_element)
    raw *= result.element_mult

    if force_crit or (can_crit and rng.rint(1, 100) <= attacker.stat("crit")):
        result.crit = True
        raw *= 1.0 + attacker.stat("crit_dmg") / 100.0

    if getattr(defender, "defending", False):
        raw *= 0.52
        result.blocked = True

    amount = max(1, int(raw))
    amount, absorbed = defender.absorb_with_shield(amount)
    result.absorbed = absorbed
    result.amount = amount

    steal = attacker.stat("lifesteal")
    if getattr(attacker, "signature", lambda: "")() == "bloodthirst":
        steal += 15
    if steal and amount:
        result.lifesteal = max(1, int(amount * steal / 100.0))
    return result


class Battle:
    """One encounter. Owns its own log, timeline and animation state."""

    def __init__(self, player, enemy, mods=(1.0, 1.0, 1.0, 1.0, 0.6),
                 on_event=None, viewer=0, mirror=False, sort_party=True):
        # Accepts one of each or a group of each; callers that pass a single
        # player and a single monster keep working unchanged.
        self.players = (list(player) if isinstance(player, (list, tuple))
                        else [player])
        self.enemies = (list(enemy) if isinstance(enemy, (list, tuple))
                        else [enemy])
        # Fastest first, so a quick character moves before a slow one on the
        # same side. Solo this sorts a list of one.
        #
        # A mirror is handed the order the host already settled on and must
        # not re-derive it. Sorting is deterministic given identical stats,
        # but "deterministic given identical stats" is a promise about two
        # machines that a desync would collect on later; taking the order as
        # given removes the question.
        if sort_party:
            self.players.sort(key=lambda p: -p.stat("spd"))
        # A mirror shows a fight resolved somewhere else. It owns no dice:
        # its timeline never runs, and every number it displays arrived in a
        # frame from the host.
        self.mirror = bool(mirror)
        # Which of them this machine is looking through. The host is not
        # automatically player 0 - whoever runs this passes their own index.
        self.viewer = clamp(int(viewer), 0, len(self.players) - 1)

        self.acting_player = 0        # whose turn it is on the player side
        # Consecutive healing turns, per combatant (item 14). Keyed by the
        # player index, or ("enemy", index) for a monster - one dict, so
        # the rule is written once and applies to both sides.
        self._heal_streak = {}
        self.targets = [0] * len(self.players)   # each player's own aim
        self.acting = 0               # which monster is taking its turn
        self.defeated = set()         # monster ids already announced as dead
        self.fallen = set()           # player slots already announced as down
        self.fled = set()             # indices of players who ran
        self.mods = mods
        self.on_event = on_event or (lambda *a, **k: None)

        self.phase = INTRO
        self.log = []
        self.timeline = deque()
        self.wait = 0.0
        self.round = 1
        self.turn = "player"
        self.menu_index = 0
        self.sub_index = 0
        self.sub_kind = ""
        self.result = None            # "victory" | "defeat" | "fled"
        self.rewards = {}
        self.elapsed = 0.0

        # Animation state read by the renderer. The scalars describe a whole
        # side; the lists describe each combatant, so one taking a hit does
        # not jolt everybody standing next to them.
        self.escape_certain = set()   # player indices that cannot be caught
        self.enemy_flash = 0.0
        #: How heavy the blow being swung right now is, 0 to 1 (s70).
        #: Set when an action starts and read when it lands, because the
        #: screen has to know **before** the damage number what kind of
        #: hit it is drawing.
        self.heft = 0.0
        #: What the last blow to land was made of, and whether it was a
        #: heavy one - `(element, heavy)`, which is exactly what
        #: `anim.death_move` asks for (item 0al(b), s71). Kept rather than
        #: passed because the thing that needs it is `check_deaths`, which
        #: runs *after* the damage and has never been told what caused it.
        #: The default is an ordinary physical blow, which is what a body
        #: that dies to poison or a script should look like.
        self.last_blow = ("physical", False)
        #: The wash behind this fight, when the fight is somewhere in
        #: particular (item 0am, s72). `None` means the ordinary rule -
        #: the colour comes off what you are fighting. **A colour, not an
        #: arena key**, because `battle.py` imports no pygame and has no
        #: business knowing what a Last Stand is.
        self.arena_tint = None
        #: What stands on the horizon, when the fight is somewhere in
        #: particular. A **name**, not a shape - `battlescreen` owns
        #: every pixel of it, and this file still imports no pygame.
        self.arena_scene = ""
        #: Which of this place's skylines this fight draws (item 0bd).
        #: A number the screen interprets; `battle.py` has no idea what
        #: it means, which is how it stays free of pygame.
        self.arena_variant = 0
        #: The action command in flight, if any (item 0ai). A dict rather
        #: than a class because it crosses to `battlescreen` and to a co-op
        #: mirror, and both read it as data.
        self.command = None
        self.player_flashes = [0.0] * len(self.players)
        self.enemy_flashes = [0.0] * len(self.enemies)
        # What each body is doing. A move is a name and a clock; how it
        # *looks* lives in `anim`, so retiming a blow does not touch combat.
        # The seeds are spread so that bodies standing next to each other do
        # not breathe in lockstep and read as one object.
        self.player_motion = [anim.Motion(seed=i * 1.7)
                              for i in range(len(self.players))]
        self.enemy_motion = [anim.Motion(seed=i * 0.9 + 0.4)
                             for i in range(len(self.enemies))]
        self.banner = ""
        self.banner_timer = 0.0

        for combatant in self.players + self.enemies:
            combatant.defending = False
        for member in self.players:
            member.clear_statuses()
            # Reset per-battle signature state.
            member.kindling = 0
            member.first_strike = True
            member.untouched = True
            # How many monsters are still standing, for Single Combat
            # (s52). Refreshed every turn by `start_turn`; set here so a
            # Duelist that never gets a turn still has a number.
            member.duel_odds = len([m for m in self.enemies if m.alive])
            # **Fortune's Road finally does something in a fight** (s52).
            # It was better loot, more gold and an opening relic - all of
            # it outside combat - so the Generalist was the plainest
            # statline in the game with no clause to make up for it and
            # read as the weakest class by a wide margin. One free stand
            # per battle is luck, which is what the signature is about, and
            # it reuses the Phoenix Down path rather than inventing a
            # second way to survive a lethal blow.
            if member.signature() == "fortune":
                member.revive_charge = True

        # Measured between the quickest of each side, so a fast straggler in
        # a slow group still gets the jump on you.
        self.players_first = (
            max(p.stat("spd") for p in self.players)
            >= max(m.stat("spd") for m in self.enemies))

        self.say(self.intro_line())
        for monster in self.enemies:
            if isinstance(monster, Boss):
                self.say('"%s"' % monster.definition.taunt)
        self.set_banner(self.group_name(), 1.35)
        self.after(1.35, self.begin_round)

    # -- the party ---------------------------------------------------------

    @property
    def actor(self):
        """The player whose turn it is - who pays, swings and is healed."""
        if not self.players:
            return None
        return self.players[min(self.acting_player, len(self.players) - 1)]

    @property
    def you(self):
        """The player this machine is looking through. Renderer only."""
        return self.players[min(self.viewer, len(self.players) - 1)]

    @property
    def is_your_turn(self):
        """Whether the local player is the one the menu belongs to."""
        return (self.turn == "player"
                and self.acting_player == self.viewer
                and self.phase in (MENU, SUBMENU))

    def standing(self):
        """Players still in the fight: alive, and not already run off."""
        return [p for i, p in enumerate(self.players)
                if p.alive and i not in self.fled]

    # -- who is up, and who is next ----------------------------------------
    #
    # **Roadmap item 0h** (2026-09-03), his words: *"make it easier to know
    # whether or not its your turn, add a turn bar and gray out all of your
    # options when its not your turn"*. Both halves need the same answer -
    # the order this round will actually go in - and neither of them is
    # allowed to guess it from the renderer.
    #
    # It lives here rather than in `battlescreen.py` because this is where
    # the turn model is, and because a bar that disagrees with the fight is
    # worse than no bar: hard rule 2.3 keeps it testable headlessly, and a
    # test can assert the bar and the fight agree turn by turn.

    def turn_order(self):
        """Who acts this round, in order, as (side, index) pairs.

        **Side-based, because that is what this fight is**: one side moves
        in list order, then the other. `players_first` is settled when the
        battle opens and never changes, so the whole round is knowable the
        moment it begins - which is what makes a bar honest rather than a
        prediction.

        The fallen and the fled are left out. A bar listing somebody who
        cannot act is a bar you have to learn to read past.
        """
        party = [("player", i) for i, member in enumerate(self.players)
                 if member.alive and i not in self.fled]
        foes = [("enemy", i) for i, monster in enumerate(self.enemies)
                if monster.alive]
        return party + foes if self.players_first else foes + party

    def turn_now(self):
        """The (side, index) acting right now, or None between turns.

        Deliberately **not** `acting_member()` / `acting_enemy()`: those
        two advance their index as they skip the dead, and a renderer
        asking whose turn it is must never move the fight on.
        """
        if self.turn == "player":
            for i in range(self.acting_player, len(self.players)):
                if self.players[i].alive and i not in self.fled:
                    return ("player", i)
            return None
        for i in range(self.acting, len(self.enemies)):
            if self.enemies[i].alive:
                return ("enemy", i)
        return None

    # **Whether the local player may press anything is not decided here.**
    # `battlescreen.waiting_on_someone_else` is that answer and its
    # docstring records why it is phrased the way it is - one line that
    # caused three symptoms in the school playtest. The greyed-out menu
    # calls the same function the input gate does, so "looks dead" and "is
    # dead" cannot drift apart; a second, tidier copy of that rule here is
    # exactly how they would.

    def participants(self):
        """Everyone the win belongs to.

        Falling is not the same as leaving: a player who went down still
        fought for it, and taking their share away would make the weakest
        member fall furthest behind fastest. Running away is different -
        you are not paid for the fight you left.
        """
        return [p for i, p in enumerate(self.players) if i not in self.fled]

    def stun_immune_for(self, member):
        """The Cornerstone is one player's relic, not the party's."""
        return bool(member.has_relic_tag("stun_immune"))

    def player_index(self, member):
        for i, other in enumerate(self.players):
            if other is member:
                return i
        return 0

    # -- what the renderer asks for ----------------------------------------

    def pose(self, who, index, alive=True):
        """What one body looks like right now, breathing included."""
        motions = self.player_motion if who == "player" else self.enemy_motion
        if not 0 <= index < len(motions):
            return anim.Pose()
        return anim.pose_of(motions[index], self.elapsed,
                            idle=2.0 if who == "player" else 3.4,
                            alive=alive)

    def falling(self, who, index):
        """Whether a dead body is still on its way down."""
        motions = self.player_motion if who == "player" else self.enemy_motion
        if not 0 <= index < len(motions):
            return False
        return motions[index].kind in anim.DEATHS

    @property
    def player_offset(self):
        return self.pose("player", min(self.viewer, len(self.players) - 1)).dx

    @property
    def player_flash(self):
        return self.player_flashes[min(self.viewer, len(self.players) - 1)]

    # -- the group ---------------------------------------------------------

    @property
    def target(self):
        """Where `self.actor` is aiming. Each player keeps their own.

        A single shared aim would mean one player's Left/Right swinging
        everybody else's sword around, which is the party-side version of
        the bug section 15 of the handoff is about.
        """
        return self.targets[min(self.acting_player, len(self.targets) - 1)]

    @target.setter
    def target(self, value):
        self.targets[min(self.acting_player, len(self.targets) - 1)] = value

    @property
    def enemy(self):
        """The monster `self.actor` is aiming at.

        Never returns a corpse while anything is still standing: retargeting
        happens as soon as one falls, so player actions cannot be aimed into
        an empty space.
        """
        if not self.enemies:
            return None
        index = min(self.target, len(self.enemies) - 1)
        return self.enemies[index]

    def living(self):
        return [monster for monster in self.enemies if monster.alive]

    def group_name(self):
        """What to call the whole encounter on the banner."""
        names = [monster.name for monster in self.enemies]
        if len(names) == 1:
            return names[0]
        unique = set(names)
        if len(unique) == 1:
            return "%d %ss" % (len(names), names[0])
        return "%s and %d more" % (names[0], len(names) - 1)

    def intro_line(self):
        if len(self.enemies) == 1:
            return "%s appears!" % self.enemies[0].name
        return "%s appear!" % self.group_name()

    def can_flee(self):
        """You can only run if every one of them will let you.

        One boss in a group is enough to pin you, which is the same rule a
        lone boss has always had.
        """
        return all(monster.can_flee for monster in self.living())

    def cycle_target(self, step=1):
        """Move the player's aim to the next living enemy."""
        alive = [i for i, monster in enumerate(self.enemies) if monster.alive]
        if len(alive) < 2:
            return False
        here = alive.index(self.target) if self.target in alive else 0
        self.target = alive[(here + step) % len(alive)]
        return True

    def _retarget(self):
        """Point every aim at something alive, if its mark has fallen.

        All of them, not just the acting player's: a monster can die on
        somebody else's turn, and the next player to act should not have to
        discover that by swinging at a corpse.
        """
        alive = [i for i, m in enumerate(self.enemies) if m.alive]
        if not alive:
            return
        for i, aim in enumerate(self.targets):
            if aim >= len(self.enemies) or not self.enemies[aim].alive:
                self.targets[i] = alive[0]

    # -- plumbing ----------------------------------------------------------

    def say(self, text):
        self.log.append(str(text))
        if len(self.log) > 60:
            self.log = self.log[-40:]

    def set_banner(self, text, seconds=1.2):
        self.banner = text
        self.banner_timer = seconds

    # -- action commands ---------------------------------------------------
    #
    # **Item 0ai**, decided in §19 answer 2 and restated on 2026-09-06.
    # Savvydev took Block Tales as the reference (§41): every attack and every
    # defence has a hidden timing window, and hitting it is worth damage.
    #
    # The split §19 asked for, kept exactly: **`battle.py` says an attack
    # *has* a window and what it was worth; `battlescreen.py` collects the
    # keypress.** No pygame crosses this line (hard rule 2.3), and the
    # frame clock stays on the side that already has one - `self.elapsed`,
    # which `update` advances before anything else can return early.

    #: How wide a window is, in seconds. Wide enough to be a *timing*
    #: rather than a reflex test, narrow enough that mashing does not pass.
    COMMAND_WINDOW = 0.30
    #: What a perfectly timed attack multiplies damage by, and what a
    #: missed one does. **A miss is worse than not having the modifier**,
    #: which is what makes this a cost in §45's sense rather than a free
    #: upgrade with a score bonus attached.
    COMMAND_BEST = 1.40
    COMMAND_MISS = 0.80
    #: A timed block takes this share off the blow. A missed one takes
    #: nothing - it does not make the hit worse, because being punished
    #: for defending badly *and* attacking badly is two costs for one
    #: modifier.
    COMMAND_GUARD = 0.45

    def open_command(self, kind, lands_in):
        """Announce a window around a blow that lands in `lands_in`.

        Does nothing unless the hero whose fight this is took the modifier
        - so every call site can ask unconditionally and no branch has to
        remember. Returns True when a window actually opened, which is what
        the caller uses to decide whether to wait for it.
        """
        if not self.timed_commands():
            return False
        best = self.elapsed + max(0.0, lands_in)
        self.command = {
            "kind": kind,
            "best": best,
            "opens": best - self.COMMAND_WINDOW,
            "closes": best + self.COMMAND_WINDOW * 0.5,
            "taken": False,
            "quality": 0.0,
        }
        # `emit`'s own first parameter is called `kind`, so the payload
        # says `what` instead of shadowing it.
        self.emit("command", what=kind, window=self.COMMAND_WINDOW)
        return True

    def timed_commands(self):
        """Whether this fight is being played with action commands.

        Read off **the local hero**, not the party: in a shared fight the
        modifier belongs to whoever is sitting at this machine, and a
        mirror of somebody else's hero must not start asking this player
        for inputs (§21).
        """
        you = getattr(self, "you", None)
        mods = getattr(you, "modifiers", None)
        return bool(mods and mods.has("timed"))

    def time_command(self):
        """Presentation says the key went down. Returns the quality, 0-1.

        **Early is worth less than late is punished**, which is the shape
        every game with these has: the window opens well before the blow
        and closes just after it, so pressing on sight is a poor score and
        pressing on the beat is a good one.
        """
        command = self.command
        if not command or command["taken"]:
            return 0.0
        command["taken"] = True
        if not (command["opens"] <= self.elapsed <= command["closes"]):
            command["quality"] = 0.0
            self.emit("sfx", name="error")
            return 0.0
        gap = abs(self.elapsed - command["best"])
        quality = max(0.0, 1.0 - gap / self.COMMAND_WINDOW)
        command["quality"] = quality
        self.emit("sfx", name="crit" if quality > 0.75 else "hit")
        self.emit("float", who="player", index=self.acting_player,
                  text="Good!" if quality > 0.75 else "Late",
                  color=(252, 226, 104) if quality > 0.75 else (200, 200, 210))
        return quality

    def take_command(self, kind):
        """Consume the window for `kind`, and say what it was worth.

        Returns 0.0 when there was no window at all, which is every fight
        without the modifier - so the resolvers can multiply by whatever
        comes back without asking whether the feature is on.
        """
        command, self.command = self.command, None
        if not command or command["kind"] != kind:
            return 0.0
        return float(command["quality"])

    def attack_multiplier(self, quality):
        """What a timed swing is worth. 1.0 when the modifier is off."""
        if not self.timed_commands():
            return 1.0
        return self.COMMAND_MISS + (self.COMMAND_BEST
                                    - self.COMMAND_MISS) * quality

    def after(self, delay, fn):
        self.timeline.append((max(0.0, delay), fn))

    def emit(self, kind, **data):
        self.on_event(kind, data)

    @property
    def finished(self):
        return self.phase in (VICTORY, DEFEAT, FLED)

    # -- round flow --------------------------------------------------------

    def begin_round(self):
        if self.finished:
            return
        self.turn = "player" if self.players_first else "enemy"
        self.acting_player = 0
        self.acting = 0
        self.start_turn()

    def acting_enemy(self):
        """The monster whose turn it is, or None once the group is spent.

        Skips any that died earlier in the round, which is the whole reason
        this walks the list rather than trusting an index.
        """
        while self.acting < len(self.enemies):
            monster = self.enemies[self.acting]
            if monster.alive:
                return monster
            self.acting += 1
        return None

    def acting_member(self):
        """The player whose turn it is, or None once the party is spent.

        Skips anyone who fell or ran earlier in the round, for exactly the
        reason `acting_enemy` walks its list: an index alone goes stale the
        moment somebody drops out mid-round.
        """
        while self.acting_player < len(self.players):
            member = self.players[self.acting_player]
            if member.alive and self.acting_player not in self.fled:
                return member
            self.acting_player += 1
        return None

    def pick_victim(self):
        """Which player a monster swings at.

        **Leans toward whoever is already hurt, without ever committing to
        them.** Uniform was honest and dull: damage spread evenly over four
        bodies kills nobody, which section 23 measured as a large part of why
        a full party won every fight the game can generate. Always striking
        the weakest is the opposite failure - a downed player cascades and
        there is nothing to decide.

        In between, a wounded hero is in real danger and spending a turn
        mending them is worth it, but the monster can still go elsewhere.
        """
        standing = self.standing()
        if not standing:
            return None
        if len(standing) == 1:
            # No die is rolled to choose among one person. Besides being
            # pointless, a draw here would shift the seeded stream that
            # `tools/balance.py` measures solo play with - which is how this
            # was caught: four signature classes moved several points with
            # no rule change at all. **Solo never reaches the weighting
            # below, which is why the per-class table is untouched by it.**
            return standing[0]
        weights = [1.0 + FOCUS_LEAN * (1.0 - m.hp / max(1, m.max_hp()))
                   for m in standing]
        return rng.choices(standing, weights=weights, k=1)[0]

    def start_turn(self):
        if self.finished:
            return
        # Live, not fixed at setup: a brawl that thins to one *is* a duel
        # by the time it does, and a Duelist should feel that turn arrive.
        standing = len([m for m in self.enemies if m.alive])
        for member in self.players:
            member.duel_odds = standing
        if self.turn == "player":
            actor = self.acting_member()
            if actor is None:            # the whole party has acted
                self.end_player_phase()
                return
        else:
            actor = self.acting_enemy()
            if actor is None:            # the whole group has acted
                self.end_enemy_phase()
                return
        if (self.turn == "player" and self.stun_immune_for(actor)
                and actor.has_status("stun")):
            actor.statuses.remove(actor.get_status("stun"))
            self.say("The Cornerstone holds. %s shrugs it off."
                     % self.name_of(actor))
        if actor.has_status("stun"):
            self.say("%s is stunned and cannot move." % self.name_of(actor))
            self.emit("sfx", name="error")
            status = actor.get_status("stun")
            if status:
                actor.statuses.remove(status)
            self.after(0.7, self.end_turn)
            self.phase = ACTING
            return
        if self.turn == "player":
            self._retarget()
            self.phase = MENU
            self.menu_index = 0
        else:
            self.phase = ACTING
            self.after(0.45, self.enemy_action)

    def end_player_phase(self):
        """Every player has moved; the monsters get their round."""
        if self.finished:
            return
        self.turn = "enemy"
        self.acting = 0
        self.after(0.35, self.start_turn)

    def end_enemy_phase(self):
        """Every monster has moved; the round goes back to the party."""
        if self.finished:
            return
        self.turn = "player"
        self.acting_player = 0
        self.round += 1
        self.after(0.35, self.start_turn)

    def end_turn(self):
        """Tick the acting side's statuses, then hand over or start a round."""
        if self.finished:
            return
        if self.turn == "player":
            actor = self.acting_member()
            if actor is None:
                self.end_player_phase()
                return
        else:
            actor = self.acting_enemy()
            if actor is None:
                self.end_enemy_phase()
                return
        side = "player" if self.turn == "player" else "enemy"
        index = (self.player_index(actor) if side == "player"
                 else self.acting)
        for kind, key, amount in actor.tick_statuses():
            info = STATUSES.get(key)
            if kind == "damage":
                self.say("%s suffers %d from %s." %
                         (self.name_of(actor), amount, info.name))
                self.emit("float", who=side, index=index,
                          text="-%d" % amount, color=info.color)
            elif kind == "heal":
                self.say("%s regains %d HP." % (self.name_of(actor), amount))
                self.emit("float", who=side, index=index,
                          text="+%d" % amount, color=info.color)
        if self.check_deaths():
            return
        if self.turn == "player":
            # Steady Aim asks whether anything touched you *since* this
            # moment, so the flag is raised as that player's turn closes.
            actor.untouched = True
            # Hand on to the next member of the party; the monsters only get
            # their round once every one of us has had a go. Handing over is
            # done here rather than by letting `start_turn` discover the
            # party is spent, because that would cost a second 0.35s beat and
            # slow every solo round down for nothing.
            self.acting_player += 1
            if self.acting_member() is None:
                self.end_player_phase()
            else:
                self.after(0.35, self.start_turn)
        else:
            # Hand on to the next monster in the group; the round only turns
            # over once every one of them has had a go.
            self.acting += 1
            self.after(0.35, self.start_turn)

    def name_of(self, actor):
        """What to call somebody in the log.

        "You" is reserved for the player this machine is looking through -
        everybody else is named, including other players. A log that called
        two different people "You" would be unreadable in a room.
        """
        if actor is self.you:
            return "You"
        return actor.name

    def verb(self, actor, singular, plural):
        """"You hit" but "Kess hits", without a special case at every call."""
        return singular if actor is self.you else plural

    def check_deaths(self):
        for monster in self.enemies:
            if not monster.alive and id(monster) not in self.defeated:
                self.defeated.add(id(monster))
                self.play_motion("enemy", self.enemies.index(monster),
                                 anim.death_move(*self.last_blow))
                if len(self.enemies) > 1:
                    self.say("%s is defeated!" % monster.name)
        if not self.living():
            self.win()
            return True
        self._retarget()

        for index, member in enumerate(self.players):
            if member.alive or index in self.fled:
                continue
            if member.revive_charge:
                member.revive_charge = False
                member.hp = max(1, member.max_hp() // 2)
                self.say("Phoenix Down revives %s!" % self.name_of(member))
                self.emit("sfx", name="levelup")
                self.emit("float", who="player", index=index, text="REVIVED",
                          color=(250, 180, 90))
                self.fallen.discard(index)
                self.play_motion("player", index, "cast")
            elif len(self.players) > 1 and index not in self.fallen:
                # Once. `check_deaths` runs after every blow, and without
                # this the log fills with the same person falling over and
                # over while everybody else is still fighting.
                self.fallen.add(index)
                self.play_motion("player", index,
                                 anim.death_move(*self.last_blow))
                self.say("%s falls." % self.name_of(member))

        # The party loses when nobody is left standing, not when one falls.
        if not self.standing():
            self.lose()
            return True
        return False

    # -- player actions ----------------------------------------------------

    def choose_menu(self, index):
        self.menu_index = index % len(menu_actions_for(self))

    def confirm_menu(self):
        """Act on the highlighted top-level menu entry."""
        if self.phase != MENU:
            return
        rows = menu_actions_for(self)
        action = rows[min(self.menu_index, len(rows) - 1)]
        if action == "Attack":
            self.player_attack()
        elif action == "Skill":
            usable = self.actor.skills()
            if not usable:
                self.say("No skills learned yet.")
                self.emit("sfx", name="error")
                return
            self.phase = SUBMENU
            self.sub_kind = "skill"
            self.sub_index = 0
        elif action == "Item":
            if self.actor.modifiers.has("no_items"):
                self.say("Ironheart. You carry nothing and you use nothing.")
                self.emit("sfx", name="error")
                return
            if not self.usable_items():
                self.say("No usable items.")
                self.emit("sfx", name="error")
                return
            self.phase = SUBMENU
            self.sub_kind = "item"
            self.sub_index = 0
        elif action == "Defend":
            self.player_defend()
        elif action == "Flee":
            self.player_flee()

    def usable_items(self):
        """Ironheart is checked here, not at use time.

        Otherwise the Item menu opens and then refuses, which leaves the
        player in a submenu with nothing in it and no obvious way out.
        """
        if self.actor.modifiers.has("no_items"):
            return []
        return [key for key in self.actor.consumables
                if self.actor.consumable_count(key) > 0 and key in CONSUMABLES]

    def cancel_submenu(self):
        self.phase = MENU
        self.sub_kind = ""

    def set_motion(self, who, index, kind):
        """Start a move on one body, and tell nobody.

        This is the end a mirror uses. It is separate from `play_motion` for
        one concrete reason: a mirror is *told* about a move by an event, and
        if acting on that event emitted another one it would answer itself
        forever. Split, the direction of travel is obvious - `play_motion`
        announces, `set_motion` obeys.
        """
        motions = self.player_motion if who == "player" else self.enemy_motion
        if kind not in anim.SPANS:
            return False                # a move name off a socket
        if not 0 <= index < len(motions):
            return False
        motions[index].play(kind)
        return True

    def play_motion(self, who, index, kind):
        """Start a move on one body, and tell every screen watching.

        Announced rather than set quietly because a mirror has no timeline of
        its own (section 21): without this a client would see the numbers
        change and nobody swing.
        """
        if self.set_motion(who, index, kind):
            # `move`, not `kind`: `emit` already spends that word on the
            # event's own name, and the two collide.
            self.emit("motion", who=who, index=index, move=kind)

    def _step_out(self, kind="lunge"):
        """Lean the acting player into the blow. Only them: a party standing
        shoulder to shoulder must not surge forward as one."""
        self.play_motion("player", self.acting_player, kind)

    def player_attack(self):
        """The free swing, in whatever shape this class swings it.

        **The motion and the moment it lands come from the same place**
        (item 0al): `anim.impact_time` knows when a given move connects, so
        adding a third motion is a line of data rather than a second number
        to keep in step with this one by hand.
        """
        self.phase = ACTING
        strike = self.strike_of(self.actor)
        # **The weapon decides how this looks** (s70). A greatsword hauls
        # back and falls; a dagger flicks. Both are the same `Strike`, so
        # the weight comes from what is in the hand rather than from the
        # class - which is what makes picking up a warhammer feel like
        # picking up a warhammer.
        self.heft = self.weapon_heft(self.actor)
        self.emit("sfx", name="swing")
        motion = "heavy" if self.heft >= self.HEAVY_AT else strike.anim
        lands = anim.impact_time(motion)
        if strike.shot:
            # **Released at the top of the swing, and the blow waits for
            # it to land** (item 0al). A projectile that arrives after the
            # damage number is a decoration; one the damage waits on is
            # the attack.
            self.after(lands, lambda: self.fire_shot(strike.shot))
            lands += self.SHOT_FLIGHT
        # **The window is opened against the same number the blow lands
        # on**, so a third motion cannot drift the two apart (s65).
        self.open_command("attack", lands)
        self.after(0.0, lambda: self._step_out(motion))
        self.after(lands, self._resolve_basic_attack)

    #: Above this, a blow swings on the `heavy` motion instead of its
    #: usual one - and the screen knows about it.
    HEAVY_AT = 0.55
    #: The weakest and strongest attacking skills in the game, so
    #: `skill_heft` can place one between them rather than against a
    #: number somebody typed. `Flurry` is 0.62 and `Meteor` is 3.4.
    SKILL_LIGHT, SKILL_HUGE = 0.9, 3.0

    def weapon_heft(self, who):
        """How heavy the thing in this combatant's hand is, 0 to 1."""
        from .items import weapon_heft
        gear = getattr(who, "equipment", None) or {}
        weapon = gear.get("weapon")
        return weapon_heft(weapon) if weapon else 0.0

    def skill_heft(self, skill):
        """How big an ability is, 0 to 1, from what it actually does."""
        span = max(0.1, self.SKILL_HUGE - self.SKILL_LIGHT)
        return max(0.0, min(1.0, (skill.power - self.SKILL_LIGHT) / span))

    #: How long a thing takes to cross the arena. Short enough not to
    #: slow a fight down, long enough to be seen leaving and arriving -
    #: the impact timings either side of it are 0.16s, so this roughly
    #: doubles the length of a ranged attack and nothing else.
    SHOT_FLIGHT = 0.24

    def fire_shot(self, kind, element=None, target=None, scale=1.0):
        """Say that something is crossing the arena.

        **`battle.py` says what was fired and at whom; the screen works
        out where that is** - the same split as every other event here,
        and the reason this file still imports no pygame (hard rule 2.3).
        """
        # `emit`'s own first parameter is called `kind`, so the payload
        # says `what` - the same collision the action-command event hit
        # (s67), which is worth one comment rather than two surprises.
        self.emit("shot", what=str(kind), scale=float(scale),
                  element=str(element or self.actor.element()),
                  who="player", index=self.acting_player,
                  at="enemy",
                  at_index=self.target if target is None else int(target),
                  flight=self.SHOT_FLIGHT)

    def strike_of(self, who):
        """The `Strike` this combatant swings with (item 0ai, s65).

        Monsters have no class, so they get the plain one - which is what
        they have always swung. `getattr` rather than a type check for the
        same reason `compute_damage` uses one: an attacker here may be a
        monster, a boss, or a mirror of somebody else's hero.
        """
        from .classes import STRIKES, strike_for
        key = getattr(who, "class_key", "")
        return strike_for(key) if key else STRIKES["plain"]

    def _resolve_basic_attack(self):
        forced = (self.actor.signature() == "opportunist"
                  and self.actor.first_strike)
        self.actor.first_strike = False
        self.actor.kindling = 0          # a swing breaks the spell chain
        strike = self.strike_of(self.actor)
        # **Split into `hits` before anything else touches it.** Two hits
        # of half are not one hit of one: defence subtracts from each, so a
        # flurry loses more to armour, and each rolls its own crit, so it
        # gains more from a crit chance. That asymmetry *is* the trade, and
        # it falls out of the existing damage model rather than being
        # written down anywhere as a number.
        # **Consumed here, once.** `take_command` clears the window as it
        # reads it, so a swing cannot be paid for twice and a window left
        # over from a cancelled action cannot pay for the next one.
        timing = self.attack_multiplier(self.take_command("attack"))
        share = strike.power / max(1, strike.hits)
        result = compute_damage(self.actor, self.enemy, share, "atk",
                                self.actor.element(), self.enemy.element,
                                force_crit=forced, pierce=strike.pierce)
        for _ in range(max(1, strike.hits) - 1):
            again = compute_damage(self.actor, self.enemy, share, "atk",
                                   self.actor.element(), self.enemy.element,
                                   pierce=strike.pierce)
            result.amount += again.amount
            result.crit = result.crit or again.crit
        if strike.channel:
            # Steel with something behind it. Folded into the one number
            # for the same reason Runeblade is (s52): two figures off one
            # swing reads as a bug and lands on the wrong beat (s22).
            extra = compute_damage(self.actor, self.enemy, strike.channel,
                                   "mag", self.actor.element(),
                                   self.enemy.element, can_crit=False)
            result.amount += extra.amount
        # **The timing scales the damage, not the power.** Applied to
        # `power` it goes in *before* defence is subtracted, and against a
        # guard that already eats most of a swing a 1.40x power reads as
        # 2.6x damage - measured, on the first version of this. The
        # multiplier is meant to be the multiplier.
        if timing != 1.0:
            result.amount = max(1, int(round(result.amount * timing)))
        if forced:
            self.say("%s was already behind them." % self.name_of(self.actor))
        # **Runeblade** (s52): a Spellblade's ordinary swing carries magic
        # with it. Folded into the same hit rather than dealt as a second
        # one, because two damage numbers off one swing reads as a bug and
        # would land on the wrong beat of the animation (s22).
        if self.actor.signature() == "runeblade":
            extra = compute_damage(self.actor, self.enemy, 0.40, "mag",
                                   self.actor.element(), self.enemy.element,
                                   can_crit=False)
            result.amount += extra.amount
        self.apply_damage_to_enemy(
            result, "%s %s" % ("your" if self.actor is self.you
                               else self.actor.name + "'s", strike.name))
        if strike.cleave:
            self._cleave_onto(strike, result)
        self.after(0.55, self.end_turn)

    def _cleave_onto(self, strike, result):
        """Carry a sweep into one other enemy.

        **One other, not all of them.** A sweep that hits everything is a
        free `quake` on a class that paid nothing for it, and the pack
        sizes this game hands out (up to six, s23) would make it the only
        attack anybody pressed.
        """
        others = [foe for foe in self.living() if foe is not self.enemy]
        if not others:
            return
        target = others[0]
        splash = compute_damage(self.actor, target, strike.cleave, "atk",
                                self.actor.element(), target.element,
                                can_crit=False, pierce=strike.pierce)
        index = self.enemies.index(target)
        target.hp = max(0, target.hp - splash.amount)
        self.actor.stats_tracker["damage"] += splash.amount
        if 0 <= index < len(self.enemy_flashes):
            self.enemy_flashes[index] = 0.22
        self.emit("float", who="enemy", index=index,
                  text=str(splash.amount), color=(238, 226, 210))
        self.emit("particles", who="enemy", index=index,
                  element="physical", count=6)
        self.say("The sweep carries into %s for %d."
                 % (target.name, splash.amount))
        # **`check_deaths` rather than anything local.** It is what ends a
        # fight, pays it out and clears the arena; a sweep that killed the
        # last monster its own way would end nothing.
        self.check_deaths()

    def player_defend(self):
        self.phase = ACTING
        self.actor.defending = True
        self.say("%s %s for impact."
                 % (self.name_of(self.actor),
                    self.verb(self.actor, "brace", "braces")))
        self.emit("sfx", name="block")
        # **The log has said "braces" since this existed and the body did
        # nothing** (item 0al(b), s71). Defend was the only action in the
        # game with no movement at all, which read as the input having
        # been dropped.
        self.play_motion("player", self.acting_player, "brace")
        # Defending trades a turn for a little MP back, so it is never wasted.
        regained = self.actor.restore_mp(max(2, self.actor.max_mp() // 12))
        if regained:
            self.emit("float", who="player", index=self.acting_player,
                      text="+%d MP" % regained, color=(120, 160, 250))
        self.after(0.5, self.end_turn)

    def player_flee(self):
        """Run. One player leaving is not the party dissolving.

        Solo those are the same event, which is why this reads as it always
        did there. With company, the fight carries on without whoever left,
        and only empties out when the last of them goes.
        """
        if self.actor.modifiers.has("no_flee"):
            self.say("You swore you would not run. You do not run.")
            self.emit("sfx", name="error")
            return
        if not self.can_flee():
            self.say("There is no running from this.")
            self.emit("sfx", name="error")
            return
        if self.acting_player in self.escape_certain:
            self.say("%s never really there."
                     % self.verb(self.actor, "You were", self.actor.name
                                 + " was"))
            self.phase = ACTING
            self.after(0.35, self.flee_success)
            return
        standing = self.living()
        if not standing:
            # **There is nothing left to run from** (§92). Found by
            # `tests/uifuzz.py`: killing the last monster and pressing
            # Flee before the victory resolved crashed the game on
            # `max()` of an empty list. The window is one or two frames
            # wide and a mouse finds it, which is how a fuzzer found it
            # and five months of play did not.
            #
            # Nothing happens: the fight is already over, and letting it
            # finish is the honest answer to fleeing a won battle.
            self.say("There is nothing left to run from.")
            self.emit("sfx", name="error")
            return
        self.phase = ACTING
        # Measured against the quickest of them: outrunning four monsters
        # means outrunning the fastest one, not the average.
        quickest = max(monster.stat("spd") for monster in standing)
        chance = clamp(self.mods[4]
                       + (self.actor.stat("spd") - quickest) * 0.012
                       - 0.07 * (len(standing) - 1),
                       0.08, 0.92)
        if rng.chance(chance):
            self.say("%s %s away."
                     % (self.name_of(self.actor),
                        self.verb(self.actor, "slip", "slips")))
            self.emit("sfx", name="back")
            self.after(0.5, self.flee_success)
        else:
            self.say("%s could not get away!" % self.name_of(self.actor))
            self.emit("sfx", name="error")
            self.after(0.5, self.end_turn)

    def skill_cost_text(self, skill):
        """What the submenu should show as the price of this ability."""
        hp_fraction = HP_COST_SKILLS.get(skill.key)
        if hp_fraction:
            return "%d HP" % int(self.actor.max_hp() * hp_fraction)
        return "%d MP" % skill.mp

    def can_pay(self, skill):
        hp_fraction = HP_COST_SKILLS.get(skill.key)
        if hp_fraction:
            return self.actor.hp > int(self.actor.max_hp() * hp_fraction)
        return self.actor.mp >= skill.mp

    def _pay(self, skill):
        """Spend the cost. Echo Stone sometimes waives an MP price."""
        hp_fraction = HP_COST_SKILLS.get(skill.key)
        if hp_fraction:
            cost = int(self.actor.max_hp() * hp_fraction)
            self.actor.hp = max(1, self.actor.hp - cost)
            self.emit("float", who="player", text="-%d" % cost,
                      color=(214, 62, 72))
            return
        if (skill.mp and self.actor.has_relic_tag("free_cast")
                and rng.chance(0.18)):
            self.say("The Echo Stone pays for it.")
            return
        self.actor.mp -= skill.mp

    def _heal_player(self, amount, label="You", from_item=False):
        """Single healing path, so the Oathbound Vow can never be missed.

        `from_item` exists for the Apothecary (s52), whose signature is
        about what is in the bag rather than about healing in general -
        doubling `mend` and `sanctuary` as well would make it a better
        Paladin instead of a different class.
        """
        healed = self.actor.heal(
            max(1, int(amount * self.actor.heal_multiplier(from_item))))
        if healed:
            self.emit("float", who="player", text="+%d" % healed,
                      color=(118, 220, 138))
            if self.actor.signature() == "vow" and self.enemy.alive:
                lash = max(1, int(healed * 0.35))
                self.enemy.hp = max(0, self.enemy.hp - lash)
                self.enemy_flash = 0.22
                self.say("Your vow answers: %d holy damage." % lash)
                self.emit("float", who="enemy", text=str(lash),
                          color=(252, 240, 180))
                self.emit("particles", who="enemy", element="holy", count=8)
                self.check_deaths()
        return healed

    # **How many turns in a row anybody may heal.** Roadmap item 14, and
    # Savvydev's own fix for what his friend found: negation plus mana
    # regeneration plus health regeneration, cycled forever, is a hero who
    # cannot lose and a fight that cannot end.
    #
    # Three, and it applies to monsters too, because a regenerating boss
    # you cannot out-damage is the same stalemate wearing different
    # clothes. The counter resets the moment somebody does anything else,
    # so healing twice and hitting once is untouched - what it forbids is
    # *only* healing.
    HEAL_STREAK_CAP = 3

    def heal_streak(self, who):
        return self._heal_streak.get(who, 0)

    def note_action(self, who, healed):
        """Record what somebody just did, for the streak cap."""
        if healed:
            self._heal_streak[who] = self._heal_streak.get(who, 0) + 1
        else:
            self._heal_streak[who] = 0

    def may_heal(self, who):
        return self.heal_streak(who) < self.HEAL_STREAK_CAP

    def use_skill(self, skill):
        if self.phase != SUBMENU:
            return
        if not self.can_pay(skill):
            self.say("Not enough left to pay for %s." % skill.name)
            self.emit("sfx", name="error")
            return
        if skill.kind == "heal" and not self.may_heal(self.acting_player):
            # Refused rather than wasted: the cost is not taken and the
            # turn is not spent, because a rule the player cannot see
            # should never quietly eat their move.
            self.say("You cannot keep doing only that. Do something else.")
            self.emit("sfx", name="error")
            return
        self._pay(skill)
        self.phase = ACTING
        self.say("You use %s." % skill.name)
        self.emit("sfx", name=skill.sfx)
        # A big ability is big whatever you are holding. **Computed
        # before the motion is chosen, not after** - the old order worked
        # only because `after(0.0, ...)` defers the lambda past the
        # assignment two lines below it, which is a thing that happens to
        # be true rather than a thing anybody decided.
        self.heft = max(self.skill_heft(skill),
                        self.weapon_heft(self.actor) * 0.6)
        # **Which move, from what the ability already is** (item 0al(b),
        # s71). Every skill in the game played `cast` unless it was a
        # physical attack, so a mend, a curse, a ward and a Meteor were
        # one gesture - and `cast` steps forward, so healing yourself
        # leaned at the enemy.
        motion = anim.skill_motion(
            skill.kind, skill.scales == "mag",
            self.heft >= self.HEAVY_AT,
            physical=self.strike_of(self.actor).anim)
        self.after(0.0, lambda: self._step_out(motion))
        # **And it lands when that move lands.** This was a literal 0.18
        # while the motions kept their impact frames in `anim.IMPACT_AT` -
        # the exact pair-of-numbers trap that table's own comment exists
        # to close, sitting one file away from it.
        lands = anim.impact_time(motion)
        if self.skill_flies(skill):
            # **The shape and the size are the spell's own** (item
            # 0ba): every magical attack used to fire `"bolt"` and differ
            # only in colour. Both come from `skills`, derived from what
            # the ability already says it is.
            from .skills import skill_look, skill_scale
            look, size = skill_look(skill), skill_scale(skill)
            self.after(lands, lambda: self.fire_shot(
                look, element=skill.element or self.actor.element(),
                scale=size))
            lands += self.SHOT_FLIGHT
        self.after(lands, lambda: self._resolve_skill(skill))

    def skill_flies(self, skill):
        """Whether this ability throws something rather than reaching.

        **Magic that hits somebody else flies.** A heal, a buff and a
        self-cast do not - nothing crosses the arena for those, and a bolt
        launched at yourself reads as a bug rather than as a spell.
        """
        if skill.kind not in ("attack", "drain"):
            return False
        return skill.scales == "mag"

    def _resolve_skill(self, skill):
        player = self.actor
        if skill.kind != "heal":
            # **Any other action clears it.** The cap forbids *only*
            # healing, not healing - two heals and a swing is a fight, and
            # nothing here should punish it.
            self.note_action(self.acting_player, False)

        # Spells feed the Emberwright chain; everything else breaks it.
        if skill.scales == "mag" and skill.kind in ("attack", "drain"):
            player.kindling = min(5, player.kindling + 1)
            if player.signature() == "kindling" and player.kindling > 1:
                self.say("Kindling: +%d%% spell damage."
                         % int(12 * (player.kindling - 1)))
        elif skill.kind == "attack":
            player.kindling = 0

        if skill.key in SPECIAL_SKILLS:
            self._resolve_special(skill)
            return

        if skill.kind == "buff" or skill.target == "self":
            if skill.status == "shield":
                pool = int(player.stat("mag") * skill.power)
                player.add_status("shield", skill.status_turns, pool)
                self.say("A shield absorbs up to %d damage." % pool)
                self.emit("float", who="player", text="SHIELD",
                          color=(180, 220, 250))
            elif skill.kind == "heal":
                healed = self._heal_player(
                    int(player.stat("mag") * skill.power))
                self.say("You restore %d HP." % healed)
                self.note_action(self.acting_player, True)
            elif skill.status:
                player.add_status(skill.status, skill.status_turns)
                self.say("%s takes hold." % STATUSES[skill.status].name)
                self.emit("float", who="player",
                          text=STATUSES[skill.status].name.upper(),
                          color=STATUSES[skill.status].color)
            self.after(0.55, self.end_turn)
            return

        total = 0
        for hit in range(max(1, skill.hits)):
            if not self.enemy.alive:
                break
            result = compute_damage(player, self.enemy, skill.power,
                                    skill.scales, skill.element,
                                    self.enemy.element)
            total += result.amount
            self.apply_damage_to_enemy(result, skill.name,
                                       element=skill.element)
        if skill.kind == "drain" and total:
            healed = self._heal_player(total // 2)
            if healed:
                self.say("You drain %d HP." % healed)
        if skill.status and self.enemy.alive and rng.chance(skill.status_chance):
            if self.enemy.add_status(skill.status, skill.status_turns):
                self.say("%s is afflicted with %s!"
                         % (self.enemy.name, STATUSES[skill.status].name))
                self.emit("float", who="enemy",
                          text=STATUSES[skill.status].name.upper(),
                          color=STATUSES[skill.status].color)
        self.after(0.6, self.end_turn)

    def _resolve_special(self, skill):
        """Abilities whose effect is not a plain attack, heal or status."""
        player = self.actor

        if skill.key == "manawell":
            gained = player.restore_mp(max(8, player.max_mp() // 3))
            self.say("You draw on the road. %d MP." % gained)
            self.emit("float", who="player", text="+%d MP" % gained,
                      color=(120, 160, 250))

        elif skill.key == "laststand":
            missing = player.max_hp() - player.hp
            healed = self._heal_player(int(missing * 0.55) + 10)
            self.say("You refuse to fall. %d HP." % healed)

        elif skill.key == "vanish":
            player.add_status("haste", skill.status_turns)
            # One player's certainty, not the party's: Vanish hides the
            # person who cast it.
            self.escape_certain.add(self.acting_player)
            self.say("%s not where %s were. Escape is certain now."
                     % (self.verb(player, "You are", player.name + " is"),
                        self.verb(player, "you", "they")))
            self.emit("float", who="player", index=self.acting_player,
                      text="VANISH", color=(168, 108, 226))

        elif skill.key == "luckystrike":
            element = rng.choice(["fire", "ice", "thunder", "shadow", "holy"])
            result = compute_damage(player, self.enemy, skill.power, "atk",
                                    element, self.enemy.element)
            self.say("The road picks %s." % element)
            self.apply_damage_to_enemy(result, skill.name, element=element)

        self.after(0.6, self.end_turn)

    def use_item(self, key):
        if self.actor.modifiers.has("no_items"):
            self.say("Ironheart. You carry nothing and you use nothing.")
            self.emit("sfx", name="error")
            return
        item = CONSUMABLES.get(key)
        if not item or self.actor.consumable_count(key) <= 0:
            self.emit("sfx", name="error")
            return
        if item.effect == "escape" and not self.enemy.can_flee:
            self.say("The smoke does nothing here.")
            self.emit("sfx", name="error")
            return
        self.actor.use_consumable(key)
        self.phase = ACTING
        self.say("You use %s." % item.name)
        player = self.actor

        if item.effect == "heal":
            self.emit("sfx", name="heal")
            self._heal_player(item.power, from_item=True)
        elif item.effect == "mana":
            gained = player.restore_mp(item.power)
            self.emit("sfx", name="heal")
            self.emit("float", who="player", text="+%d MP" % gained,
                      color=(120, 160, 250))
        elif item.effect == "full":
            self.emit("sfx", name="levelup")
            self._heal_player(player.max_hp(), from_item=True)
            player.mp = player.max_mp()
            self.emit("float", who="player", text="FULL", color=(250, 216, 128))
        elif item.effect == "cure":
            player.clear_statuses(only_bad=True)
            self.emit("sfx", name="heal")
            self.emit("float", who="player", text="CURED", color=(140, 220, 140))
        elif item.effect == "revive":
            player.revive_charge = True
            self.emit("sfx", name="unlock")
            self.emit("float", who="player", text="BLESSED", color=(250, 180, 90))
        elif item.effect == "buff":
            player.add_status("might", 3)
            self.emit("sfx", name="unlock")
            self.emit("float", who="player", text="MIGHT", color=(250, 176, 72))
        elif item.effect == "guard":
            player.add_status("guard", 3)
            self.emit("sfx", name="block")
            self.emit("float", who="player", text="GUARD", color=(108, 164, 252))
        elif item.effect == "damage":
            self.emit("sfx", name="fire")
            self.apply_damage_to_enemy(DamageResult(amount=item.power),
                                       item.name, element="fire")
        elif item.effect == "escape":
            self.say("Smoke fills the air.")
            self.emit("sfx", name="back")
            self.after(0.45, self.flee_success)
            return
        self.after(0.55, self.end_turn)

    # -- enemy actions -----------------------------------------------------

    def enemy_action(self):
        if self.finished:
            return
        actor = self.acting_enemy()
        if actor is None:
            self.end_enemy_phase()
            return
        if isinstance(actor, Boss) and actor.check_phase():
            self.say("%s changes its stance!" % actor.name)
            self.set_banner("PHASE II", 1.0)
            self.emit("sfx", name="boss")
            self.emit("shake", amount=14)
            self.after(0.9, self._enemy_execute)
            return
        self._enemy_execute()

    def _enemy_execute(self):
        if self.finished:
            return
        actor = self.acting_enemy()
        if actor is None:
            self.end_enemy_phase()
            return
        index = self.acting
        skill = actor.choose_action()
        # **The cap applies to monsters too** (item 14). A boss that heals
        # every turn and cannot be out-damaged is the same unwinnable
        # stalemate as an immortal hero, seen from the other chair - so a
        # monster over its streak swings instead.
        who = ("enemy", index)
        if skill is not None and skill.kind == "heal":
            if not self.may_heal(who):
                skill = None
            else:
                self.note_action(who, True)
        else:
            self.note_action(who, False)
        self.emit("sfx", name=skill.sfx if skill else "swing")
        # **A block window on the way in**, which is the half of Block
        # Tales that makes the modifier a *system* rather than a damage
        # bonus (§41): *"defending also requires timing in order to block
        # damage"*. Opened whoever the monster picks, because you do not
        # know who it is aiming at until it lands - and a window you only
        # get when you are the target would tell you.
        self.open_command("block", 0.18)
        self.after(0.0, lambda: self._lunge(index))
        self.after(0.18, lambda: self._resolve_enemy(skill, actor, index))

    def _lunge(self, index):
        """Only the monster that is swinging moves toward you."""
        self.play_motion("enemy", index, "lunge")

    def _resolve_enemy(self, skill, enemy=None, index=None, victim=None):
        # `enemy` is the monster acting, which is not necessarily the one any
        # player is aiming at; `victim` is the player it picked, which is not
        # necessarily the one whose turn it was. Both are passed in and
        # neither is stored - see the note at the top of this file.
        enemy = enemy if enemy is not None else self.enemy
        player = victim if victim is not None else self.pick_victim()
        if player is None:
            self.after(0.35, self.end_turn)
            return
        victim_index = self.player_index(player)

        if skill and (skill.target == "self" or skill.kind == "buff"):
            enemy.add_status(skill.status, 3)
            self.say("%s uses %s." % (enemy.name, skill.name))
            self.emit("float", who="enemy", index=index,
                      text=skill.name.upper(),
                      color=STATUSES.get(skill.status, STATUSES["might"]).color)
            self.after(0.55, self.end_turn)
            return

        if skill and skill.kind == "debuff":
            self.say("%s uses %s on %s."
                     % (enemy.name, skill.name,
                        self.name_of(player)))
            if rng.chance(skill.status_chance) and \
                    player.add_status(skill.status, 3):
                self.emit("float", who="player", index=victim_index,
                          text=STATUSES[skill.status].name.upper(),
                          color=STATUSES[skill.status].color)
            self.after(0.55, self.end_turn)
            return

        power = skill.power if skill else 1.0
        scales = skill.scales if skill else "atk"
        element = skill.element if skill else enemy.element
        if skill:
            self.say("%s uses %s!" % (enemy.name, skill.name))
        result = compute_damage(enemy, player, power, scales, element,
                                player.element())
        self.apply_damage_to_player(result, element, player, enemy)

        if skill and skill.status and player.alive \
                and rng.chance(skill.status_chance):
            if player.add_status(skill.status, 3):
                self.say("%s %s afflicted with %s!"
                         % (self.name_of(player),
                            self.verb(player, "are", "is"),
                            STATUSES[skill.status].name))
                self.emit("float", who="player", index=victim_index,
                          text=STATUSES[skill.status].name.upper(),
                          color=STATUSES[skill.status].color)
        self.after(0.6, self.end_turn)

    # -- damage application -------------------------------------------------

    def apply_damage_to_enemy(self, result, source="attack", element="physical"):
        """`self.actor` hits what `self.actor` is aiming at."""
        attacker = self.actor
        enemy = self.enemy
        aim = self.target
        enemy.hp = max(0, enemy.hp - result.amount)
        self.enemy_flash = 0.22
        if 0 <= aim < len(self.enemy_flashes):
            self.enemy_flashes[aim] = 0.22
        attacker.stats_tracker["damage"] += result.amount

        text = str(result.amount)
        color = (250, 240, 210)
        # **How big this actually was**, which is not the same question as
        # "did it crit" (s70). Before this, a dagger poke and a Meteor
        # shook the screen by the same 3 and threw the same nine sparks -
        # the only thing the game could say loudly was *critical*.
        #
        # Two inputs, whichever is larger: how heavy the swing was, and
        # **what share of the target it took**. The second matters most -
        # a hit that removes a third of a warden is a big hit however
        # small the weapon was.
        # `max_hp` is a method on some combatants and a number on others,
        # so ask rather than assume - guessing wrong here raises inside
        # the draw path, which is the worst place in the game for it.
        ceiling = getattr(enemy, "max_hp", 0)
        ceiling = ceiling() if callable(ceiling) else ceiling
        share = result.amount / max(1, int(ceiling or 1))
        weight = max(self.heft, min(1.0, share * 2.6))
        # **What being hit looks like, from the same share** (item 0al(b),
        # s71). One `recoil` was played for a scratch, a blocked blow and
        # a third of a warden's health. It is chosen here rather than
        # above because `share` is what decides it, and `share` is not
        # known until the damage has been taken off.
        self.last_blow = (element, weight >= self.HEAVY_AT)
        if result.amount and enemy.alive:
            self.play_motion("enemy", aim, anim.reaction(
                share, turned=bool(result.blocked)
                or result.element_mult < 1.0))
        if result.crit:
            text += "!"
            color = (252, 208, 96)
            attacker.stats_tracker["crits"] += 1
            self.emit("sfx", name="crit")
            self.emit("shake", amount=int(9 + 12 * weight))
        else:
            self.emit("sfx", name="hit", throttle=0.05)
            self.emit("shake", amount=int(3 + 11 * weight))
        if result.element_mult > 1.0:
            text += " WEAK"
            color = (250, 140, 90)
        elif result.element_mult < 1.0:
            text += " resist"
            color = (170, 180, 200)

        # A heavy hit pops its number the way a crit does - the number is
        # the loudest thing on the screen and it should be loud for the
        # right reasons.
        self.emit("float", who="enemy", index=aim, text=text, color=color,
                  pop=result.crit or weight >= 0.55)
        self.emit("particles", who="enemy", index=aim, element=element,
                  count=int((16 if result.crit else 9) + 22 * weight))
        if result.absorbed:
            self.say("%d absorbed by a shield." % result.absorbed)
        self.say("%s hits %s for %d.%s"
                 % (source.capitalize(), enemy.name, result.amount,
                    " Critical!" if result.crit else ""))

        if result.lifesteal:
            healed = attacker.heal(result.lifesteal)
            if healed:
                self.emit("float", who="player",
                          index=self.player_index(attacker),
                          text="+%d" % healed, color=(214, 62, 72))
        self.check_deaths()

    def apply_damage_to_player(self, result, element="physical",
                               victim=None, source=None):
        """Hurt a named player.

        `victim` is passed in rather than read off `self` because the player
        a monster picked is not the player whose turn it was - the same
        distinction `_resolve_enemy` keeps on the other side.
        """
        player = victim if victim is not None else self.actor
        index = self.player_index(player)
        # **A timed block, if there was a window and this is our hero.**
        # Only `self.you` can have pressed anything - a mirror of somebody
        # else's hero has nobody at its keyboard (§21) - and the window is
        # consumed either way so it cannot pay for the next blow.
        guard = self.take_command("block")
        if guard and player is getattr(self, "you", None):
            softened = int(result.amount * self.COMMAND_GUARD * guard)
            if softened:
                result.amount = max(0, result.amount - softened)
                self.emit("float", who="player", index=index,
                          text="-%d blocked" % softened,
                          color=(150, 210, 255))
                self.emit("sfx", name="block")
                self.say("%s turned %d of it aside."
                         % (self.name_of(player), softened))
        if getattr(player, "dev_invulnerable", False):
            result.amount = 0
        player.hp = max(0, player.hp - result.amount)
        if 0 <= index < len(self.player_flashes):
            self.player_flashes[index] = 0.22
        # **The same three questions on this side of the arena** (item
        # 0al(b), s71). s70 gave the enemy side a weight and left this one
        # on a flat 4-or-7 shake and ten particles, so a monster taking a
        # third of your health looked exactly like one taking a chip -
        # and being hit hard is the moment the game most needs to say so.
        ceiling = getattr(player, "max_hp", 0)
        ceiling = ceiling() if callable(ceiling) else ceiling
        share = result.amount / max(1, int(ceiling or 1))
        weight = min(1.0, share * 2.6)
        turned = bool(result.blocked) or bool(guard)
        self.last_blow = (element, weight >= self.HEAVY_AT)
        if result.amount and player.alive:
            self.play_motion("player", index, anim.reaction(share,
                                                            turned=turned))
        player.defending = False
        if result.amount:
            player.untouched = False      # Steady Aim is spent

        color = (250, 120, 120)
        text = str(result.amount)
        if result.blocked:
            text += " blocked"
            color = (150, 200, 250)
        if result.crit:
            text += "!"
            color = (252, 120, 90)
        self.emit("float", who="player", index=index, text=text, color=color,
                  pop=result.crit or weight >= self.HEAVY_AT)
        self.emit("particles", who="player", index=index, element=element,
                  count=int((14 if result.crit else 8) + 20 * weight))
        self.emit("sfx", name="hurt", throttle=0.05)
        self.emit("shake", amount=int((7 if result.crit else 4) + 10 * weight))
        if result.absorbed:
            self.say("%s shield absorbs %d."
                     % (self.verb(player, "Your", player.name + "'s"),
                        result.absorbed))
        # Who swung is passed in: `self.enemy` is somebody's *aim*, and
        # naming it here would credit the wrong monster in a group.
        hitter = source
        if hitter is None:
            hitter = self.acting_enemy() or self.enemies[0]
        self.say("%s hits %s for %d."
                 % (hitter.name,
                    "you" if player is self.you else player.name,
                    result.amount))
        self.check_deaths()

    # -- outcomes -----------------------------------------------------------

    def win(self):
        if self.finished:
            return
        self.phase = VICTORY
        self.result = "victory"
        self.timeline.clear()
        for member in self.players:
            member.defending = False
            member.clear_statuses()
        self.set_banner("VICTORY", 2.0)
        self.emit("sfx", name="victory")
        # **Hollowed used to heal 6% on every kill.** It does not any
        # more (s45): the shrine still refuses to mend you, and that is
        # the whole of the modifier now. Savvydev, on a friend who had
        # taken all nine: *"is one tapping everything"*.
        self.rewards = self.split_rewards()
        if len(self.enemies) == 1:
            self.say("%s is defeated!" % self.enemies[0].name)
        else:
            self.say("The last of them falls.")

    def split_rewards(self):
        """What the fight paid, and to whom.

        **Evenly among everyone who fought** - Savvydev's rule. Falling is not
        leaving, so somebody who went down still takes a share; without that
        the weakest member of a party falls furthest behind fastest, which is
        the opposite of what playing together should do. Running away is the
        one way to forfeit: you are not paid for the fight you left.

        The remainder goes to the earliest participant rather than being
        dropped, so a three-way split of 100 XP pays 34/33/33 and the party
        total always matches what the monsters were worth.
        """
        total_xp = sum(monster.xp_reward for monster in self.enemies)
        total_gold = sum(monster.gold_reward for monster in self.enemies)
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
        """This player's cut, as {"xp", "gold"}. Zero if they ran."""
        shares = (self.rewards or {}).get("shares", {})
        return shares.get(self.player_index(member), {"xp": 0, "gold": 0})

    def lose(self):
        if self.finished:
            return
        self.phase = DEFEAT
        self.result = "defeat"
        self.timeline.clear()
        self.set_banner("DEFEATED", 2.0)
        self.emit("sfx", name="defeat")
        self.say("You fall..." if len(self.players) == 1
                 else "The last of you falls...")

    def flee_success(self):
        """The acting player is out of the fight.

        The battle only ends when the last of them leaves - otherwise one
        person deciding to run would end everybody else's fight, which is
        the party-side version of dragging the room into a battle nobody
        chose (section 18).
        """
        if self.finished:
            return
        leaver = self.actor
        self.fled.add(self.acting_player)
        leaver.clear_statuses()
        leaver.defending = False
        if self.standing():
            self.after(0.35, self.end_turn)
            return
        self.phase = FLED
        self.result = "fled"
        self.timeline.clear()

    # -- talking to another machine ------------------------------------------

    def snapshot(self):
        """Everything a mirror needs to draw this fight, and nothing else.

        Deliberately **not** in here: `menu_index` and `sub_index`. Those are
        one viewer's cursor, not the state of the battle, and pushing them
        would drag every client's highlight around as somebody else moved
        theirs. Everything that *is* here is a fact the host decided.
        """
        return {
            "ph": self.phase,
            "tn": self.turn,
            "ap": self.acting_player,
            "ae": self.acting,
            "rd": self.round,
            "bn": self.banner,
            "bt": round(self.banner_timer, 2),
            "fl": sorted(self.fled),
            "rs": self.result or "",
            "lg": self.log[-8:],
            "pl": [_body(p) for p in self.players],
            "en": [_body(m) for m in self.enemies],
        }

    def apply_frame(self, data):
        """Take the host's word for it.

        Everything here came off a socket, so it is read the way save data is
        read. A field that does not survive validation leaves the previous
        value standing rather than taking the mirror down mid-fight.

        **Except the menu you have open, which is yours** (2026-08-31,
        s45). `phase` does two jobs: it says what the *fight* is doing -
        acting, animating, over - and it also says which *menu* is on
        screen, `MENU` or `SUBMENU`. The first is the host's to decide and
        the second cannot be, because the host is not looking at your
        screen. It sits in `MENU` while it waits for your move and sends
        that ten times a second, so a guest who opened Skills had it shut
        again on the next frame:

        > "i cant use my skills in battle when i joined my friend, it opens
        > the skill menu and then immediately closes"

        So while the fight is waiting on **this viewer**, a frame that says
        `MENU` or `SUBMENU` does not move a mirror that is already in one
        of those two. Everything else still lands: the moment the host
        moves to `ACTING`, an animation, or a result, the mirror follows,
        which is what closes the menu when the move is actually taken.

        **This is the second time this bug has been fixed.** The first
        (s19 tier 1 item 1) was the same symptom from the host's chair and
        a different line. Both were found by two people on real machines
        and neither by the suite, which is the note s45 ends on.
        """
        phase = _one_of(data.get("ph"), PHASES, self.phase)
        turn = _one_of(data.get("tn"), ("player", "enemy"), self.turn)
        acting_player = _index(data.get("ap"), len(self.players),
                               self.acting_player)
        deciding = (self.mirror and turn == "player"
                    and acting_player == self.viewer
                    and phase in (MENU, SUBMENU)
                    and self.phase in (MENU, SUBMENU))
        self.phase = self.phase if deciding else phase
        self.turn = turn
        self.acting_player = acting_player
        self.acting = _index(data.get("ae"), len(self.enemies), self.acting)
        self.round = _index(data.get("rd"), 10000, self.round)
        banner = data.get("bn")
        if isinstance(banner, str):
            self.banner = banner[:40]
        if isinstance(data.get("bt"), (int, float)):
            self.banner_timer = clamp(float(data["bt"]), 0.0, 10.0)
        result = data.get("rs")
        self.result = result if result in ("victory", "defeat", "fled") else None
        if self.result:
            self.phase = {"victory": VICTORY, "defeat": DEFEAT,
                          "fled": FLED}[self.result]

        fled = data.get("fl")
        if isinstance(fled, list):
            self.fled = {i for i in (_index(v, len(self.players), -1)
                                     for v in fled[:8]) if i >= 0}
        log = data.get("lg")
        if isinstance(log, list):
            self.log = [str(line)[:160] for line in log[-8:]
                        if isinstance(line, str)]

        _apply_bodies(self.players, data.get("pl"))
        _apply_bodies(self.enemies, data.get("en"))

    # -- update -------------------------------------------------------------

    def update(self, dt):
        self.elapsed += dt
        self.banner_timer = max(0.0, self.banner_timer - dt)
        self.enemy_flash = max(0.0, self.enemy_flash - dt)
        for i in range(len(self.enemies)):
            self.enemy_flashes[i] = max(0.0, self.enemy_flashes[i] - dt)
            self.enemy_motion[i].advance(dt)
        for i in range(len(self.players)):
            self.player_flashes[i] = max(0.0, self.player_flashes[i] - dt)
            self.player_motion[i].advance(dt)

        if self.mirror:
            # Decay the flashes and lunges, and stop. Advancing here would be
            # this machine deciding the fight for itself.
            return
        if self.wait > 0:
            self.wait = max(0.0, self.wait - dt)
            return
        while self.timeline and self.wait <= 0:
            delay, fn = self.timeline.popleft()
            if delay > 0:
                self.wait = delay
                self.timeline.appendleft((0.0, fn))
                return
            fn()
