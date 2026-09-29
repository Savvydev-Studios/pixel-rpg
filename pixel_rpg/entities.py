"""Player, monsters and bosses.

The player gained nothing but gear in the original. Here there is a level
curve, four allocatable attributes, three equipment slots, MP, and derived
stats that combine base values, attributes, gear and active statuses.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from . import classes as classes_mod
from .appearance import Appearance
from . import relics as relics_mod
from . import security as sec
from .items import ELEMENT_STRONG_VS, Item
from .modifiers import RunModifiers
from .skills import SKILLS_BY_KEY, Status, crit_bonus, modifier
from . import spoils as spoils_mod
from .util import MAX_LEVEL, clamp, rng

# **One attribute, one stat.** Until 2026-08-24 there were four, and each
# bundled two or three stats together - a point in Might bought attack and
# crit damage whether you wanted crit damage or not. Savvydev asked for them
# split so a build can be pointed at one thing:
#
#     "maybe split up the attributes, give people more individual variety
#      to pick from ... they all remain as separate attributes to invest in"
#
# The four original names keep their headline stat, so Might is still
# attack and Vigor is still health. What was secondary is now its own row.
#
# Order is deliberate: each old attribute followed by what split off it,
# so the sheet reads as four familiar groups rather than nine new words.
ATTRIBUTES = ("might", "ferocity", "vigor", "guard",
              "mind", "focus", "warding", "agility", "precision")

# Points granted per level. **The one number that compensates for the
# split**: buying what a single old point bought now takes two or three,
# so holding this at 3 would have been a large stealth nerf. Measured
# rather than reasoned. Measured at nightmare, level 5, poor drops -
# the one setting with enough headroom to tell the values apart, since
# the usual level-8 probe saturates near 100% at every rate:
#
#     points/level   0    2    3    5    7   10
#     average win   39%  73%  80%  86%  89%  91%
#
# The old rate was 3, on four bundled attributes. Buying what one old
# point bought now takes two or three, so holding 3 would have been a
# quiet nerf; 7 put every class near the ceiling. 5 sits just above the
# old curve, which is the intent - the split should widen the choice,
# not change the difficulty. Section 38 has the whole table.
POINTS_PER_LEVEL = 5

# **How many abilities you carry into a fight.** Savvydev, decision 11 on
# 2026-08-24: "Four skills equipped". Item 13, and the Block Tales shape -
# a loadout rather than an inventory (§41).
LOADOUT_SIZE = 4

# **The ceiling, and the reason there is a second one.** Savvydev,
# 2026-08-29:
#
# > "the max level should be 100, and then place a limit on how high an
# > attribute can go, so like players cant just reach level 100 and then
# > have every single one of their points invested into attack so they end
# > up one shotting everything"
#
# 100 levels at 5 points is 500 points to spend. Poured into one row that
# is +1000 attack, which is not a build, it is the end of the game - so
# **no single attribute may pass `ATTRIBUTE_CAP`**. At 60 a maxed row
# costs 60 of your 500 and you need eight rows to spend the rest, which is
# the shape he asked for: specialise, but not into a single number.
#
# The cap is per attribute rather than a total, because a total is just a
# slower version of the same problem.
ATTRIBUTE_CAP = 60

# **Nine rows of sixty is every point there is to spend.** Savvydev,
# 2026-09-01: *"limit the max amount of skill points you can ever have to
# 540 since there are 9 attributes that can be upgraded 60 times"*.
#
# Derived rather than typed, so it cannot drift from the two numbers it is
# made of - and it is a cap on the *pool*, not on how many you earn: past
# it, levelling still levels and simply stops adding to a pile you could
# never spend. A number you can never use is a number that lies about what
# your hero can become.
def _total_points():
    return ATTRIBUTE_CAP * len(ATTRIBUTES)
ATTRIBUTE_LABEL = {
    "might": "Might", "ferocity": "Ferocity",
    "vigor": "Vigor", "guard": "Guard",
    "mind": "Mind", "focus": "Focus", "warding": "Warding",
    "agility": "Agility", "precision": "Precision",
}
ATTRIBUTE_HELP = {
    # **Say what the number does to what.** Savvydev, 2026-08-31: *"Precision
    # just says +1% crit, make it more clear, 1% crit what? crit rate?"* -
    # and the same question applies to Ferocity, which was "+1 crit
    # damage" and reads as a point of damage rather than a percentage.
    "might": "+2 ATK",
    "ferocity": "+1% crit damage",
    "vigor": "+8 max HP",
    "guard": "+1 DEF",
    "mind": "+2 MAG",
    "focus": "+4 max MP",
    "warding": "+1 RES",
    "agility": "+2 SPD",
    "precision": "+1% crit chance",
}


def xp_for_level(level):
    """Total XP needed to go from `level` to `level` + 1."""
    return int(46 + (level ** 1.72) * 13)


class Combatant:
    """Anything that can take a turn in battle."""

    def __init__(self, name="?", level=1):
        self.name = name
        self.level = level
        self.hp = 1
        self.mp = 0
        self.statuses = []
        self.defending = False
        self.revive_charge = False

    # -- derived stats (overridden by subclasses) --------------------------

    def max_hp(self):
        return 1

    def max_mp(self):
        return 0

    def stat(self, key):
        return 0

    # -- status handling ---------------------------------------------------

    def has_status(self, key):
        return any(s.key == key for s in self.statuses)

    def get_status(self, key):
        for s in self.statuses:
            if s.key == key:
                return s
        return None

    def add_status(self, key, turns=3, magnitude=0):
        existing = self.get_status(key)
        if existing:
            existing.turns = max(existing.turns, turns)
            existing.magnitude = max(existing.magnitude, magnitude)
            return False
        self.statuses.append(Status(key=key, turns=turns, magnitude=magnitude))
        return True

    def clear_statuses(self, only_bad=False):
        if only_bad:
            self.statuses = [s for s in self.statuses if s.info.good]
        else:
            self.statuses = []

    def tick_statuses(self):
        """Advance every status one turn. Returns a list of log strings."""
        from .skills import dot_damage, regen_amount
        events = []
        for status in list(self.statuses):
            if status.key in ("poison", "burn", "bleed"):
                hurt = dot_damage(status, self.max_hp())
                self.hp = max(0, self.hp - hurt)
                events.append(("damage", status.key, hurt))
            elif status.key == "regen":
                healed = min(regen_amount(self.max_hp()), self.max_hp() - self.hp)
                if healed:
                    self.hp += healed
                    events.append(("heal", status.key, healed))
            status.turns -= 1
            if status.turns <= 0:
                self.statuses.remove(status)
                events.append(("expire", status.key, 0))
        return events

    def absorb_with_shield(self, amount):
        """Shield soaks damage first and breaks when its pool is gone."""
        shield = self.get_status("shield")
        if not shield or amount <= 0:
            return amount, 0
        soaked = min(shield.magnitude, amount)
        shield.magnitude -= soaked
        if shield.magnitude <= 0:
            self.statuses.remove(shield)
        return amount - soaked, soaked

    @property
    def alive(self):
        return self.hp > 0


class Player(Combatant):
    """The hero. Owns progression, equipment and the bag."""

    BASE = {"atk": 8, "defense": 5, "mag": 6, "res": 4, "spd": 10,
            "crit": 5, "crit_dmg": 50, "lifesteal": 0}
    BASE_HP = 60
    BASE_MP = 24

    def __init__(self, name="Wanderer", class_key="warden"):
        super().__init__(name, level=1)
        self.class_key = (class_key if class_key in classes_mod.CLASSES_BY_KEY
                          else classes_mod.DEFAULT_CLASS)
        self.appearance = Appearance()
        self.relics = []              # relic keys, validated on load
        self.ngplus = 0
        self.modifiers = RunModifiers()
        # Set the moment a debug power is used, and never cleared. It is what
        # keeps a tinkered run out of the profile that survives across saves.
        self.debug_used = False
        # **Which stacked rooms this hero has already emptied** (0af, s58),
        # as a set of door coordinates. The first thing about an interior
        # that does not regenerate: s56 was proud of needing no save-format
        # change, and a hoard that came back every time you stepped outside
        # would be a farm rather than a find.
        self.looted_hoards = set()
        # Which ending this run's answers are leaning toward. Nothing here
        # picks an ending; the shape of the whole ledger does.
        self.leanings = {}
        # **What a wave handed you** (items 0bs/0bt, s83). Skills this
        # hero was *given* rather than unlocked, and buffs the run is
        # carrying as `(key, until_wave)` - `None` for the rest of the
        # run. Both are lists rather than sets because the same buff can
        # be taken twice and both should count.
        self.granted = []
        self.buffs = []
        # Per-battle signature state, reset by Battle on setup.
        self.kindling = 0             # Emberwright spell chain
        self.first_strike = True      # Cutpurse opening crit
        # Longshot: True while nothing has damaged you since your last turn.
        self.untouched = True
        self.xp = 0
        self.attributes = {k: 0 for k in ATTRIBUTES}
        # **Has this run reached an ending?** Nothing recorded that
        # before 2026-08-24: `meta.endings` is global across every save
        # in the profile, and `ngplus` counts New Game+ cycles - which
        # stopped being the same question the moment a finished run
        # survived instead of being deleted (s19 decision 5).
        #
        # Two things need it: keeping a finished run loadable, and the
        # veteran clause that lets a beaten hero cross into a beaten
        # world (s19 item 5c).
        self.beaten = False
        # **Fell under Mortal, and the run is over** (s54). Persisted
        # rather than acted on and forgotten, because the alternative
        # to remembering it is deleting the slot - and this project
        # does not write code whose job is erasing saves (s7).
        self.fallen = False
        # The highest chapter this run has been *shown* (s19 item 10). Kept
        # on the run rather than the profile: a chapter card is a moment in
        # one story, and a second character walking the same road has not
        # had it yet. Zero on an older save, which means the card for
        # whatever chapter they are in shows once and then never again -
        # the right answer for a save that predates chapters entirely.
        self.chapter_seen = 0
        # Which abilities are carried, by key. Empty means "the first four
        # you learned", so a hero is never unarmed and nobody has to open
        # a screen before their first fight.
        self.loadout = []
        self.points = 0
        # How many bag upgrades have been bought. See `bag_size`.
        self.bag_bought = 0
        self.equipment = {"weapon": None, "armor": None, "accessory": None}
        self.inventory = []
        self.consumables = {"potion": 3, "ether": 1, "antidote": 1}
        self.gold = 80
        self.materials = 0          # Ember Steel, for refining at the smith
        self.x = 0.0
        self.y = 0.0
        self.facing = "front"
        self.hp = self.max_hp()
        self.mp = self.max_mp()
        # Lifetime counters, surfaced on the character screen.
        self.stats_tracker = {"kills": 0, "bosses": 0, "damage": 0,
                              "steps": 0, "gold_earned": 0, "deaths": 0,
                              "chests": 0, "crits": 0, "playtime": 0.0,
                              # Counted since 2026-09-01, for the feats
                              # that are about the island rather than
                              # about killing things on it.
                              "favours": 0, "errands": 0, "towers": 0}

    # -- class and relics --------------------------------------------------

    @property
    def pclass(self):
        return classes_mod.get(self.class_key)

    def relic_objects(self):
        return relics_mod.resolve(self.relics)

    def add_relic(self, key):
        """Returns the Relic if it was new to this run, else None."""
        relic = relics_mod.get(key)
        if relic is None or key in self.relics:
            return None
        self.relics.append(key)
        return relic

    def has_relic_tag(self, tag):
        return relics_mod.has_tag(self.relic_objects(), tag)

    def signature(self):
        return self.pclass.signature

    # -- derived stats -----------------------------------------------------

    def gear_bonus(self, key):
        total = 0
        for item in self.equipment.values():
            if item:
                total += item.stats.get(key, 0)
        return total

    def max_hp(self):
        held = self.relic_objects()
        base = (self.BASE_HP + (self.level - 1) * 11
                + self.attributes["vigor"] * 8 + self.gear_bonus("hp")
                + relics_mod.stat_flat(held, "hp"))
        base *= self.pclass.hp
        base *= relics_mod.stat_multiplier(held, "hp")
        base *= spoils_mod.stat_multiplier(getattr(self, "buffs", ()), "hp")
        base *= self.modifiers.hp_mult()
        base *= 1.0 + 0.10 * self.ngplus
        return max(1, int(base))

    def max_mp(self):
        held = self.relic_objects()
        base = (self.BASE_MP + (self.level - 1) * 5
                + self.attributes["focus"] * 4 + self.gear_bonus("mp")
                + relics_mod.stat_flat(held, "mp"))
        base *= self.pclass.mp
        base *= relics_mod.stat_multiplier(held, "mp")
        base *= spoils_mod.stat_multiplier(getattr(self, "buffs", ()), "mp")
        return max(0, int(base))

    def stat(self, key):
        """Final stat: base + level growth + attributes + gear + statuses."""
        growth = {"atk": 1.6, "defense": 1.2, "mag": 1.4, "res": 1.0,
                  "spd": 0.8, "crit": 0.15, "crit_dmg": 1.2, "lifesteal": 0.0}
        value = self.BASE.get(key, 0) + growth.get(key, 0) * (self.level - 1)
        value += {
            "atk": self.attributes["might"] * 2,
            "crit_dmg": self.attributes["ferocity"] * 1,
            "defense": self.attributes["guard"] * 1,
            "mag": self.attributes["mind"] * 2,
            "res": self.attributes["warding"] * 1,
            "spd": self.attributes["agility"] * 2,
            "crit": self.attributes["precision"] * 1,
        }.get(key, 0)
        value += self.gear_bonus(key)

        held = self.relic_objects()
        value += relics_mod.stat_flat(held, key)
        # **A wave's boon, in the same two places a relic's is** (s83).
        # Next to relics rather than in a branch of its own, because it
        # is the same kind of thing - something the run picked up that is
        # not gear - and a special case here is a special case every
        # future reader has to hold.
        boons = getattr(self, "buffs", ())
        value += spoils_mod.stat_flat(boons, key)
        value *= getattr(self.pclass, key, 1.0)
        value *= relics_mod.stat_multiplier(held, key)
        value *= spoils_mod.stat_multiplier(boons, key)
        value *= modifier(self.statuses, key)
        if key == "defense":
            value *= self.modifiers.defense_mult()

        if key == "crit":
            value += crit_bonus(self.statuses)
            return int(clamp(value, 0, 90))
        if key == "lifesteal":
            return int(clamp(value, 0, ATTRIBUTE_CAP))
        return max(0, int(value))

    # **What carrying your own kind of weapon is worth** (2026-09-01, s47).
    # Savvydev: *"give like different weapon types special buffs that are
    # useful when used with the correct class"*.
    #
    # One number, on the stat the weapon is for. Twelve per cent is enough
    # to feel and small enough that a much better weapon of the wrong
    # family is still the right pick - which is the point: it is a thumb on
    # the scale, not a restriction. A class that cannot use a weapon is a
    # class with fewer choices, and nothing here says you may not swing it.
    AFFINITY_BONUS = 0.12

    # **The picker leans harder than the fight does, and that is not a
    # mismatch.** `Item.power()` is one number for every hero: it counts a
    # staff's magic and a bow's speed the same way whoever is holding it,
    # so at equal item level it will hand a Cutpurse a staff whose entire
    # value is a stat their attacks do not scale on. Twelve per cent
    # cannot correct for that; it was never meant to.
    #
    # So `best_for` weighs a favoured weapon by a third, which is enough to
    # win every close call and nowhere near enough to keep a weapon
    # twenty levels behind. **The damage bonus stays at 12%** - it is what
    # the weapon is worth once you are holding it, and that is a different
    # question from which one to pick up.
    #
    # The real fix is a hero-aware `power()`, and that is a change to how
    # every screen sorts gear rather than to one button (s47).
    EQUIP_AFFINITY_WEIGHT = 1.35

    def weapon_family(self):
        """The family of what is in your hand, or "" for bare fists."""
        from .items import family_of
        return family_of(self.equipment.get("weapon"))

    def has_affinity(self):
        """Whether this hero trained with what they are holding."""
        family = self.weapon_family()
        return bool(family and family in getattr(self.pclass, "favours", ()))

    def outgoing_multiplier(self):
        """Signature passives and modifiers that scale damage you deal."""
        value = self.modifiers.damage_mult()
        if self.has_affinity():
            value *= 1.0 + self.AFFINITY_BONUS
        sig = self.signature()
        if sig == "hollow":
            missing = 1.0 - self.hp / max(1, self.max_hp())
            value *= 1.0 + 0.60 * missing
        elif sig == "kindling":
            value *= 1.0 + 0.12 * clamp(self.kindling, 0, 5)
        elif sig == "steadyaim" and self.untouched:
            value *= 1.30
        return value

    def incoming_multiplier(self):
        """Signature passives that scale damage you take."""
        sig = self.signature()
        if sig == "bulwark" and self.hp <= self.max_hp() * 0.35:
            return 0.6
        # **Bulwark read backwards** (s52). The Warden gets tougher as it
        # loses; the Ironmonk gets brittler, which makes the same number
        # mean the opposite thing to play against.
        if sig == "momentum":
            return 0.82 if self.hp > self.max_hp() * 0.5 else 1.20
        return 1.0

    def heal_cap(self):
        """A Revenant cannot be mended past half. Everyone else, full."""
        if self.signature() == "hollow":
            return max(1, int(self.max_hp() * 0.5))
        return self.max_hp()

    def heal_multiplier(self, from_item=False):
        """How much good a heal from outside does you.

        A Bloodhowl mends by hitting things; potions and spells are half as
        useful, which is the trade for lifesteal on every swing. An
        Apothecary is the same number from the other end (s52) - **but only
        out of the bag**, because doubling `mend` and `sanctuary` too would
        make it a better Paladin rather than a different class.
        """
        sig = self.signature()
        if sig == "bloodthirst":
            return 0.5
        if sig == "decoction" and from_item:
            return 2.0
        return 1.0

    def loot_luck(self):
        return 0.55 if self.signature() == "fortune" else 0.0

    def gold_multiplier(self):
        value = self.modifiers.gold_mult()
        value *= relics_mod.stat_multiplier(self.relic_objects(), "gold")
        if self.signature() == "fortune":
            value *= 1.30
        elif self.signature() == "opportunist":
            value *= 1.15
        return value

    def xp_multiplier(self):
        value = self.modifiers.xp_mult()
        value *= relics_mod.stat_multiplier(self.relic_objects(), "xp")
        return value

    def element(self):
        weapon = self.equipment.get("weapon")
        return weapon.element if weapon else "physical"

    def known_skills(self):
        """Every ability this hero has, in the order they arrived.

        **The class ladder, and then what a wave gave you** (s83). A
        granted skill is one their class would never unlock, which is
        the point of the offer - and it goes on the end rather than in
        the ladder, because the ladder is what levelling still owes them.
        """
        keys = list(classes_mod.skills_for(self.class_key, self.level))
        for key in getattr(self, "granted", ()):
            if key not in keys:
                keys.append(key)
        return [SKILLS_BY_KEY[key] for key in keys if key in SKILLS_BY_KEY]

    def skills(self):
        """The abilities you can actually use in a fight - your loadout.

        **Four at a time** (roadmap item 13, Savvydev's decision 11). Block
        Tales calls them Cards: what you carry, not what you own (§41). A
        list of everything you have ever unlocked is not a decision, and by
        the late game it is not even a menu - it is a wall of options where
        two of them are correct.

        Until you have chosen, it is the first four you learned, so a new
        hero is never staring at an empty bar and nobody has to visit a
        screen before they can fight.
        """
        known = self.known_skills()
        if not self.loadout:
            return known[:LOADOUT_SIZE]
        chosen = [SKILLS_BY_KEY[key] for key in self.loadout
                  if key in SKILLS_BY_KEY
                  and key in {s.key for s in known}]
        return chosen[:LOADOUT_SIZE] or known[:LOADOUT_SIZE]

    def equip_skill(self, key):
        """Put an ability in the loadout, or take it out. Returns True if
        anything changed."""
        # **Iron Loadout refuses every edit** (s54). An empty loadout
        # already *means* the first four you learned, so refusing here is
        # the whole modifier: you keep those four for the run. It also
        # means the new-skill question never opens, because `Game` asks
        # this before it asks anything.
        if self.modifiers.has("iron_loadout"):
            return False
        known = {s.key for s in self.known_skills()}
        if key not in known:
            return False
        # **Start from what you are actually carrying, not from the raw
        # list.** An empty loadout *means* the first four you learned, so
        # editing it from empty threw those four away and left you holding
        # one ability - which is the kind of bug that looks like the
        # feature is broken rather than the edit.
        current = [s.key for s in self.skills()]
        if key in current:
            current.remove(key)
        elif len(current) >= LOADOUT_SIZE:
            return False
        else:
            current.append(key)
        self.loadout = current
        return True

    # -- progression -------------------------------------------------------

    def xp_to_next(self):
        return xp_for_level(self.level)

    def gain_xp(self, amount):
        """Returns (levels_gained, list of newly unlocked Skill objects)."""
        amount = max(0, int(amount))
        self.xp += amount
        gained = 0
        old_level = self.level
        # **The one literal that would have silently kept the old cap.**
        # Every other clamp moved to `MAX_LEVEL`; this loop condition is
        # the one that decides whether levelling happens at all, so a 60
        # left here would have made "max level 100" a lie that no clamp
        # test could catch - and a test that levels to 100 would hang.
        while self.xp >= self.xp_to_next() and self.level < MAX_LEVEL:
            self.xp -= self.xp_to_next()
            self.level += 1
            self.points = min(self.points + POINTS_PER_LEVEL,
                              self.spendable_points())
            gained += 1
        if gained and not self.modifiers.has("no_levelup_heal"):
            # Levelling always tops you up: a level-up should feel like
            # relief. **Unless you took Unmended** (s54), which is
            # Savvydev's: *"a modifier that removes the feature that
            # resets your mp and hp everytime you level up"*.
            self.hp = self.heal_cap()
            self.mp = self.max_mp()
        fresh = [SKILLS_BY_KEY[key] for key in
                 classes_mod.newly_unlocked(self.class_key, old_level,
                                            self.level)
                 if key in SKILLS_BY_KEY]
        return gained, fresh

    def respec_cost(self):
        """What taking every point back costs, in gold.

        Scaled by level rather than flat: a flat price is pocket change by
        the time you would want it and ruinous the first time you might.
        Zero when nothing is spent, so the option cannot charge for doing
        nothing.

        **Gold, not Ember Steel.** Steel is the smith's currency and
        refining is deliberately not a gold sink (section 9). Making them
        compete for one material would quietly tax rebuilding your hero
        against upgrading your sword.
        """
        if self.spent_points() <= 0:
            return 0
        return 40 + 30 * self.level

    def spent_points(self):
        """How many points are currently sitting in attributes."""
        return sum(self.attributes.values())

    def refund_points(self):
        """Take every spent point back. Returns how many came back.

        All of them at once, which is what Savvydev asked for - there is no
        un-spending one at a time, because that turns every level-up into
        a decision you can quietly walk back and stops it being a
        decision at all.

        **Health is clamped, not refilled.** A hero who dumped Vigor and
        respecs out of it must not end up with more current HP than the
        smaller maximum allows - and must not be healed for free either.
        The cost is the caller's business; this only moves the points.
        """
        moved = self.spent_points()
        for key in self.attributes:
            self.attributes[key] = 0
        self.points = min(self.points + moved, self.spendable_points())
        self.hp = min(self.hp, self.max_hp())
        self.mp = min(self.mp, self.max_mp())
        return moved

    def spendable_points(self):
        """The most points this hero could ever have waiting.

        Every point already spent is one this pool no longer has to hold,
        so the ceiling falls as you spend - which is what makes it a cap on
        *unspendable* points rather than on progress.
        """
        return max(0, _total_points() - self.spent_points())

    def spend_point(self, attribute):
        if self.points <= 0 or attribute not in ATTRIBUTES:
            return False
        # **The cap is enforced here, where points are spent.** It was only
        # applied on load, which caps a *save file* and not a player - you
        # could pour all 500 into one row and only find out on reload.
        if self.attributes.get(attribute, 0) >= ATTRIBUTE_CAP:
            return False
        before = self.max_hp()
        self.attributes[attribute] += 1
        self.points -= 1
        self.hp += max(0, self.max_hp() - before)   # new vigor is granted, not owed
        return True

    # -- inventory ---------------------------------------------------------

    # **A bag you can make bigger** (2026-09-01, s48). Savvydev: *"add a way
    # to upgrade your bag size to hold more relics and stuff"*.
    #
    # `sec.MAX_INVENTORY` stays what it always was - the ceiling the save
    # validator enforces - and this is where the player sits under it. A
    # bought upgrade moves the second number, never the first.
    BAG_START = 30
    BAG_STEP = 6

    def bag_size(self):
        """How much this hero can carry, ceiling included.

        Small Hands (s54) replaces the *start*, so a bought upgrade
        cannot climb out of it either - and `bag_upgrade_cost` refuses
        to sell one at all, so the two can never disagree.
        """
        start = self.modifiers.bag_cap(self.BAG_START)
        if self.modifiers.has("small_hands"):
            return start
        return int(clamp(getattr(self, "bag_bought", 0) * self.BAG_STEP
                         + start, start, sec.MAX_INVENTORY))

    def bag_upgrade_cost(self):
        """What the next one costs, or 0 when the bag is as big as it gets.

        Each is dearer than the last, on the same principle as refining:
        the first is a convenience and the fifth is a decision.
        """
        if self.modifiers.has("small_hands"):
            return 0
        if self.bag_size() >= sec.MAX_INVENTORY:
            return 0
        return int(240 * (1.6 ** getattr(self, "bag_bought", 0)))

    def buy_bag_upgrade(self):
        cost = self.bag_upgrade_cost()
        if cost <= 0 or self.gold < cost:
            return 0
        self.gold -= cost
        self.bag_bought = getattr(self, "bag_bought", 0) + 1
        return self.bag_size()

    def bag_full(self):
        return len(self.inventory) >= self.bag_size()

    def add_item(self, item):
        if self.bag_full():
            return False
        self.inventory.append(item)
        return True

    def remove_item(self, item):
        if item in self.inventory:
            self.inventory.remove(item)
            return True
        return False

    def equip(self, item):
        """Swap an item in. The displaced piece goes back to the bag.

        **Sworn Blade refuses a weapon once one is worn** (s54). You
        start a run holding your class's weapon, so in practice it is
        the one you finish with - and armour and trinkets are untouched,
        because the modifier is about the blade.
        """
        if item is None or item.slot not in self.equipment:
            return None
        if (item.slot == "weapon" and self.modifiers.has("sworn_blade")
                and self.equipment.get("weapon") is not None):
            return None
        self.remove_item(item)
        previous = self.equipment[item.slot]
        self.equipment[item.slot] = item
        if previous:
            self.inventory.append(previous)
        self.hp = min(self.hp, self.max_hp())
        self.mp = min(self.mp, self.max_mp())
        return previous

    def unequip(self, slot):
        item = self.equipment.get(slot)
        if not item or self.bag_full():
            return None
        self.equipment[slot] = None
        self.inventory.append(item)
        self.hp = min(self.hp, self.max_hp())
        self.mp = min(self.mp, self.max_mp())
        return item

    def best_for(self, slot):
        """Highest-scoring item for one slot, counting what is already worn.

        What you are wearing is listed first so that `max`, which keeps the
        first of equal elements, breaks ties in its favour. Otherwise a tie
        made "equip best" swap the same two items back and forth on every
        press.
        """
        current = self.equipment.get(slot)
        options = [current] if current else []
        options += [i for i in self.inventory if i.slot == slot]
        if not options:
            return None

        def score(item):
            """Power, plus a thumb on the scale for your own family.

            **This is the line the complaint was about** (2026-09-01):

            > "my class is Cutpurse (Assassin) and i used equip best and I
            > got a bow, but why would an assassin use a bow yk?"

            The weight is the same 12% the damage bonus is, so the picker
            and the fight agree: a favoured weapon wins ties and close
            calls, and a genuinely better one of the wrong family still
            wins. Ranking on power alone was not wrong, it was just
            answering a question nobody asked.
            """
            from .items import family_of
            value = item.power()
            family = family_of(item)
            if family and family in getattr(self.pclass, "favours", ()):
                value *= self.EQUIP_AFFINITY_WEIGHT
            return value

        return max(options, key=score)

    def equip_best(self):
        """Wear the strongest gear you own in every slot.

        Ranking uses the same `Item.power` score the shop and bag sort by, so
        the button agrees with the numbers the player can already see.
        """
        changed = []
        for slot in ("weapon", "armor", "accessory"):
            best = self.best_for(slot)
            if best is None or self.equipment.get(slot) is best:
                continue
            self.equip(best)
            # **Checked, not assumed** (s54). `equip` returns the piece it
            # displaced, which is legitimately None for an empty slot, so
            # the return value cannot say whether anything happened - and
            # Sworn Blade refuses weapons. Reporting a swap that did not
            # occur would put "Equipped X" in the log and leave X in the
            # bag.
            if self.equipment.get(slot) is best:
                changed.append(best)
        return changed

    def consumable_count(self, key):
        return int(self.consumables.get(key, 0))

    def add_consumable(self, key, count=1):
        self.consumables[key] = clamp(self.consumable_count(key) + count, 0, 99)

    def use_consumable(self, key):
        if self.consumable_count(key) <= 0:
            return False
        self.consumables[key] -= 1
        if self.consumables[key] <= 0:
            self.consumables.pop(key, None)
        return True

    def heal(self, amount):
        if self.has_relic_tag("mending"):
            amount = int(amount * 1.25)      # Ilan's Last Letter
        if self.has_relic_tag("potion_up"):
            amount = int(amount * 1.50)      # Bottomless Flask
        healed = max(0, min(int(amount), self.heal_cap() - self.hp))
        self.hp += healed
        return healed

    def restore_mp(self, amount):
        gained = min(int(amount), self.max_mp() - self.mp)
        self.mp += gained
        return gained

    def full_restore(self):
        self.hp = self.heal_cap()
        self.mp = self.max_mp()
        self.clear_statuses()


@dataclass(frozen=True)
class Archetype:
    key: str
    name: str
    sprite: str
    element: str
    hp: float            # per-level multipliers
    atk: float
    defense: float
    spd: float
    skills: tuple
    biomes: tuple
    xp: float = 1.0
    gold: float = 1.0


ARCHETYPES = [
    Archetype("slime", "Wild Slime", "slime", "physical",
              1.00, 0.90, 1.00, 0.80, ("slam",), ("meadow", "forest"), 0.9, 1.0),
    Archetype("bat", "Cave Bat", "bat", "shadow",
              0.80, 1.00, 0.70, 1.40, ("bite", "screech"),
              ("cave", "forest"), 1.0, 0.9),
    Archetype("rat", "Plague Rat", "rat", "shadow",
              0.85, 0.95, 0.80, 1.20, ("bite", "venom"),
              ("cave", "meadow"), 0.9, 1.1),
    Archetype("spider", "Dusk Spider", "spider", "shadow",
              0.95, 1.05, 0.90, 1.10, ("venom", "hex"),
              ("forest", "cave"), 1.1, 1.0),
    Archetype("wolf", "Ember Wolf", "wolf", "fire",
              1.10, 1.25, 0.95, 1.30, ("bite", "ember", "enrage"),
              ("wastes", "forest"), 1.2, 1.1),
    Archetype("imp", "Cinder Imp", "imp", "fire",
              1.00, 1.20, 0.90, 1.15, ("ember", "inferno"),
              ("wastes", "volcano"), 1.2, 1.2),
    Archetype("wisp", "Storm Wisp", "wisp", "thunder",
              0.90, 1.15, 1.10, 1.35, ("gale", "frost", "regenerate"),
              ("tundra", "meadow"), 1.15, 1.15),
    # **Each of these has a job**, which is the point of adding them: a
    # roster where everything hits you is a roster with one enemy in it.
    # Lurker soaks and stalls, chanter buffs itself and debuffs you,
    # gnawer is fast and fragile, husk punishes patience, and the drifter
    # is the one that heals - all of them read differently in the log
    # before you have looked at their stats.
    Archetype("lurker", "Bog Lurker", "slime", "physical",
              1.45, 0.85, 1.30, 0.55, ("harden", "slam"),
              ("forest", "meadow"), 1.1, 0.9),
    Archetype("chanter", "Hollow Chanter", "spider", "shadow",
              0.95, 0.90, 0.85, 1.00, ("hex", "screech", "harden"),
              ("cave", "wastes"), 1.15, 1.05),
    Archetype("gnawer", "Frost Gnawer", "rat", "frost",
              0.70, 1.35, 0.65, 1.75, ("bite", "frost"),
              ("tundra", "cave"), 1.05, 0.95),
    Archetype("husk", "Cinder Husk", "imp", "fire",
              1.30, 1.10, 1.15, 0.70, ("ember", "enrage", "harden"),
              ("volcano", "wastes"), 1.25, 1.15),
    Archetype("drifter", "Salt Drifter", "wisp", "physical",
              1.05, 0.80, 1.00, 0.90, ("regenerate", "gale"),
              ("wastes", "tundra"), 1.05, 1.2),
]

#: **The Long Night's own bestiary** (item 0bx). Savvydev, 2026-09-08:
#: *"the zombie gamemode should use actual zombies"*.
#:
#: They are a **biome**, not a mode flag, because that is what the
#: bestiary already keys on - so `make_enemy(level, "night")` is the
#: whole of what a night had to change, and nothing else in the game had
#: to learn that these exist.
#:
#: **Three, and each one is a different problem.** A crowd where
#: everything walks at you the same way is one enemy with several health
#: bars (§76's separate() lesson, in the roster rather than the physics):
#: the shambler is slow and hard to move, the crawler is quick and
#: fragile, and the risen hits hardest and is the one you deal with
#: first. They are all `physical` so a night is not an elements puzzle.
NIGHT_ARCHETYPES = [
    # **No two archetypes share a kit** - a test says so, and it caught
    # the first cut of these giving the shambler the slime's one move.
    # A roster where two things read the same in the log is a roster
    # with one of them in it.
    Archetype("shambler", "Shambler", "shambler", "physical",
              1.35, 0.95, 1.15, 0.55, ("slam", "quake"), ("night",),
              1.0, 0.9),
    Archetype("crawler", "Crawler", "crawler", "physical",
              0.70, 0.90, 0.70, 1.45, ("bite", "screech", "venom"),
              ("night",), 1.0, 1.0),
    Archetype("risen", "Risen", "risen", "physical",
              1.05, 1.30, 1.00, 0.85, ("slam", "enrage"), ("night",),
              1.15, 1.15),
]

ARCHETYPES.extend(NIGHT_ARCHETYPES)

ARCHETYPES_BY_KEY = {a.key: a for a in ARCHETYPES}

PREFIX_VARIANTS = [
    # (label, hp mult, atk mult, xp mult, gold mult, weight, tint)
    ("", 1.00, 1.00, 1.00, 1.00, 74.0, None),
    ("Feral", 1.15, 1.20, 1.30, 1.20, 14.0, (250, 120, 90)),
    ("Ancient", 1.45, 1.15, 1.60, 1.55, 8.0, (150, 190, 250)),
    ("Elite", 1.90, 1.45, 2.40, 2.40, 4.0, (250, 200, 90)),
]


class Enemy(Combatant):
    """A generated monster. Never persisted, so no from_dict is needed."""

    def __init__(self, archetype, level, variant="", mods=(1.0, 1.0, 1.0, 1.0, 0.6)):
        display = ("%s %s" % (variant, archetype.name)).strip()
        super().__init__(display, level)
        self.archetype = archetype
        self.variant = variant
        self.sprite = archetype.sprite
        self.element = archetype.element
        self.can_flee = True

        vmult = next((v for v in PREFIX_VARIANTS if v[0] == variant),
                     PREFIX_VARIANTS[0])
        hp_mod, atk_mod = mods[0], mods[1]

        self._max_hp = int((28 + level * 15) * archetype.hp * vmult[1] * hp_mod)
        self._atk = int((7 + level * 3.4) * archetype.atk * vmult[2] * atk_mod)
        self._def = int((3 + level * 1.9) * archetype.defense)
        self._mag = int((5 + level * 2.6) * archetype.atk)
        self._res = int((2 + level * 1.4) * archetype.defense)
        self._spd = int((8 + level * 1.1) * archetype.spd)
        self.tint = vmult[6]

        self.xp_reward = int((14 + level * 11) * archetype.xp * vmult[3] * mods[2])
        self.gold_reward = int((10 + level * 8) * archetype.gold * vmult[4] * mods[3])
        self.hp = self._max_hp
        self.mp = 0

    def max_hp(self):
        return self._max_hp

    def stat(self, key):
        raw = {"atk": self._atk, "defense": self._def, "mag": self._mag,
               "res": self._res, "spd": self._spd, "crit": 5,
               "crit_dmg": 50, "lifesteal": 0}.get(key, 0)
        return max(0, int(raw * modifier(self.statuses, key)))

    def choose_action(self):
        """Simple weighted AI: heal when hurt, otherwise mix skills in."""
        from .skills import ENEMY_SKILLS
        pool = [ENEMY_SKILLS[k] for k in self.archetype.skills
                if k in ENEMY_SKILLS]
        hurt = self.hp < self.max_hp() * 0.35
        for skill in pool:
            if hurt and skill.status in ("regen", "guard") and rng.chance(0.55):
                return skill
        if pool and rng.chance(0.55):
            return rng.choice([s for s in pool
                               if s.status not in ("regen",)] or pool)
        return None      # plain attack


@dataclass
class BossDef:
    key: str
    name: str
    title: str
    sprite: str
    level: int
    element: str
    hp: int
    atk: int
    defense: int
    mag: int
    res: int
    spd: int
    arena: tuple             # background colour of the arena
    biome: str
    skills: tuple
    phase2_skills: tuple
    taunt: str
    gold: int
    xp: int


BOSS_DEFS = [
    BossDef("sprout_king", "Sprout King", "Warden of the First Meadow",
            "sprout_king", 3, "physical", 190, 16, 8, 10, 6, 9,
            (36, 92, 56), "meadow", ("slam", "harden"),
            ("quake", "regenerate", "enrage"),
            "The meadow remembers every boot that crossed it.", 140, 120),
    BossDef("stone_golem", "Stone Golem", "Heart of the Quarry",
            "stone_golem", 7, "physical", 420, 30, 22, 14, 14, 6,
            (86, 86, 100), "cave", ("slam", "harden"),
            ("quake", "enrage"),
            "Stone does not tire. Stone does not yield.", 320, 300),
    BossDef("night_warden", "Night Warden", "Keeper of the Long Dark",
            "night_warden", 12, "shadow", 720, 46, 28, 44, 26, 18,
            (38, 34, 66), "forest", ("hex", "venom", "bite"),
            ("voidlash", "hex", "regenerate"),
            "Close your eyes. It changes nothing.", 620, 640),
    BossDef("inferno_beast", "Inferno Beast", "The Unquenched",
            "inferno_beast", 18, "fire", 1150, 66, 34, 62, 30, 22,
            (128, 48, 26), "volcano", ("ember", "slam", "enrage"),
            ("inferno", "quake", "enrage"),
            "Everything burns. You are simply next.", 1080, 1150),
    BossDef("sky_tyrant", "Sky Tyrant", "Storm That Walks",
            "sky_tyrant", 24, "thunder", 1680, 84, 42, 88, 40, 34,
            (48, 92, 140), "tundra", ("gale", "frost", "screech"),
            ("gale", "quake", "enrage", "harden"),
            "The sky answers to me. You answer to the sky.", 1800, 2000),
    BossDef("void_sovereign", "Void Sovereign", "The Last Door",
            "void_sovereign", 32, "shadow", 2600, 112, 56, 120, 54, 40,
            (52, 30, 78), "wastes", ("voidlash", "hex", "harden"),
            ("voidlash", "inferno", "enrage", "regenerate"),
            "Beyond me there is nothing. Come and see.", 3200, 3800),
]

BOSS_BY_KEY = {b.key: b for b in BOSS_DEFS}


# ------------------------------------------------------------- field bosses ---
#
# **Something out in the world worth walking to** (s42). Savvydev, after
# seeing the valley doubled: *"a huge thing about why the botw map is that
# there are super cool enemies of different types and mini bosses
# scattered throughout the map"*. Size is not the feature; the feature is
# that there is a reason to go somewhere.
#
# **A field boss is not a warden, and that separation is load-bearing.**
# There are six anchors, `THE SEVENTH` is an ending named after that
# count, and `story.ANCHOR_COUNT` guards it. So these live in their own
# list: no Memory, no codex page, no ending weight, no portal, and nothing
# in `story.py` knows they exist. They are a fight and a reward.
#
# They reuse `BossDef` and `Boss` outright - two phases, an arena colour, a
# taunt - because a mini boss that is a different *class* of thing to a
# boss would be two implementations of one idea.
#
# **Tiered by biome, not by level gate.** You can walk into any of them at
# any time, which is the whole appeal: the wastes one will kill a level 6
# hero and that is information, not a wall. It is the one place this game
# gets to borrow Breath of the Wild's "go and find out" honestly, since
# the wardens themselves are strictly ordered (s42).

FIELD_BOSSES = [
    BossDef("thicket_hulk", "Thicket Hulk", "Something in the Long Grass",
            "slime", 5, "physical", 260, 20, 14, 8, 8, 7,
            (34, 78, 48), "forest", ("slam", "harden"),
            ("quake", "enrage"),
            "It has been eating well and it is not finished.", 180, 150),
    BossDef("gravewake", "Gravewake", "What the Cave Kept",
            "bat", 10, "shadow", 470, 34, 20, 38, 22, 26,
            (46, 40, 66), "cave", ("bite", "hex", "screech"),
            ("venom", "enrage"),
            "The dark here has been dark a very long time.", 420, 380),
    BossDef("hoarfrost_stag", "Hoarfrost Stag", "The Cold That Watches",
            "wolf", 16, "frost", 720, 48, 34, 44, 30, 34,
            (150, 178, 206), "tundra", ("frost", "gale", "harden"),
            ("regenerate", "quake"),
            "It does not run. Nothing up here has needed to.", 700, 640),
    BossDef("cinder_maw", "Cinder Maw", "The Unfed",
            "imp", 22, "fire", 980, 62, 40, 58, 38, 30,
            (128, 46, 32), "volcano", ("inferno", "ember", "enrage"),
            ("quake", "harden"),
            "It burns because stopping would be worse.", 1020, 900),
    BossDef("salt_colossus", "Salt Colossus", "The Last Thing Standing",
            "stone_golem", 28, "physical", 1400, 78, 58, 40, 46, 22,
            (176, 158, 118), "wastes", ("slam", "quake", "harden"),
            ("enrage", "regenerate"),
            "Everything else out here has already given up.", 1500, 1300),
    # A second family for each biome that had one, so walking back into a
    # biome is not walking back into the same fight.
    BossDef("meadow_matriarch", "Brood Matriarch", "Mother of the Meadow",
            "spider", 7, "shadow", 300, 22, 12, 24, 12, 20,
            (58, 92, 62), "meadow", ("venom", "hex"),
            ("screech", "regenerate"),
            "She has been laying since before the village.", 220, 200),
    BossDef("rot_chorus", "Rot Chorus", "The Agreeable Dark",
            "rat", 13, "shadow", 560, 36, 24, 42, 26, 30,
            (54, 48, 44), "forest", ("venom", "hex", "screech"),
            ("enrage", "regenerate"),
            "It is several things and it has stopped arguing.", 520, 470),
    BossDef("glacier_wisp", "Glacier Choir", "Nine Voices, One Cold",
            "wisp", 19, "frost", 640, 44, 26, 66, 40, 40,
            (172, 200, 228), "tundra", ("frost", "gale", "hex"),
            ("regenerate", "screech"),
            "You are being sung at. It is not for you.", 820, 760),
    BossDef("ash_revenant", "Ash Revenant", "Still Walking",
            "night_warden", 25, "shadow", 1120, 68, 46, 62, 44, 34,
            (96, 62, 58), "volcano", ("hex", "inferno", "enrage"),
            ("quake", "regenerate"),
            "It burned a long time ago. Nobody told it.", 1180, 1040),
    # Two per biome, so every band of the map has more than one answer to
    # "what lives out here".
    BossDef("field_king", "The Fat King", "Ruler of Nine Acres",
            "slime", 4, "physical", 230, 18, 16, 6, 10, 5,
            (46, 96, 54), "meadow", ("slam", "harden"),
            ("quake", "enrage"),
            "It has never left this field and never had to.", 150, 130),
    BossDef("deep_chorus", "The Deep Chorus", "Everything the Cave Ate",
            "spider", 12, "shadow", 540, 38, 28, 40, 28, 24,
            (40, 36, 58), "cave", ("venom", "hex", "harden"),
            ("screech", "enrage"),
            "It is polite. That is the worst part of it.", 500, 450),
    BossDef("glass_wanderer", "Glass Wanderer", "It Was a Road Once",
            "wisp", 30, "thunder", 1240, 72, 44, 78, 52, 44,
            (198, 190, 160), "wastes", ("gale", "hex", "regenerate"),
            ("quake", "screech"),
            "It is going somewhere. It has been going there for centuries.",
            1600, 1400),
]

FIELD_BOSS_BY_KEY = {b.key: b for b in FIELD_BOSSES}

# **Nine families, not five** - Savvydev, 2026-08-29:
#
# > "there should be so many different mobs that all do something
# > different (except for mob variants, they should js do something
# > similar) and botw has way more than 5 field bosses, theres a LOT and
# > there are variants of them too"
#
# So the shape is **families x variants**, which is the shape the ordinary
# monsters already use (`PREFIX_VARIANTS`) and the shape Breath of the
# Wild uses for its own field bosses. A family is a thing that *fights
# differently*; a variant of it is the same fight, harder and stranger.
# That split is his and it is the right one - twenty bosses that each
# needed learning would be exhausting, and twenty that were all the same
# would be wallpaper.
#
# Two or more families per biome, so a biome is not one encounter you have
# seen already.

FIELD_VARIANTS = [
    # (prefix, hp x, atk x, gold x, xp x, weight)
    ("", 1.00, 1.00, 1.00, 1.00, 52.0),
    ("Starved", 0.85, 1.25, 1.10, 1.15, 20.0),
    ("Elder", 1.45, 1.15, 1.55, 1.60, 18.0),
    ("Hollow", 1.75, 1.35, 2.10, 2.30, 10.0),
]


def field_variant():
    """Roll which kind of a family this one is."""
    labels = [v[0] for v in FIELD_VARIANTS]
    weights = [v[5] for v in FIELD_VARIANTS]
    return rng.choices(labels, weights=weights, k=1)[0]


FIELD_VARIANT_BY_NAME = {v[0]: v for v in FIELD_VARIANTS}


# How far a field boss follows the player, 0 = never, 1 = exactly.
# Savvydev, 2026-08-29: *"stuff like bosses and mini-bosses shouldnt be as
# close to the players level compared to regular enemies"*.
#
# **Half.** A regular monster is a treadmill and should be - it is the
# texture of the place. A field boss is an event, and an event that
# rescaled perfectly would never be a wall you come back to, which is
# most of what makes one worth finding.
FIELD_BOSS_TRACKING = 0.5


def make_field_boss(key, variant="", player_level=None):
    """A field boss, scaled by its variant and loosely by the player.

    Returns a fresh `BossDef` rather than a `Boss`, because everything
    downstream - the arena, the two phases, the encounter builder - already
    takes a definition and should not learn a second way in.
    """
    base = FIELD_BOSS_BY_KEY.get(key)
    if base is None:
        return None
    if player_level is not None and player_level > base.level:
        drift = (player_level - base.level) * FIELD_BOSS_TRACKING
        base = replace(base, level=int(clamp(base.level + drift,
                                             1, MAX_LEVEL)))
    row = FIELD_VARIANT_BY_NAME.get(variant, FIELD_VARIANTS[0])
    _label, hp_x, atk_x, gold_x, xp_x, _w = row
    name = ("%s %s" % (variant, base.name)).strip()
    return replace(
        base, name=name,
        hp=int(base.hp * hp_x),
        atk=int(base.atk * atk_x),
        mag=int(base.mag * atk_x),
        gold=int(base.gold * gold_x),
        xp=int(base.xp * xp_x))

FIELD_BOSS_BY_BIOME = {}
for _b in FIELD_BOSSES:
    FIELD_BOSS_BY_BIOME.setdefault(_b.biome, _b)


# --------------------------------------------------------------- guardians ---
#
# **What stands between you and a waygate** (s42). Savvydev, 2026-08-29:
#
# > "when i meant shrine-ify, I meant like to be able to teleport to the
# > shrine, you have to beat the challenge inside, whether thats some
# > logic puzzle or js some fighting or fighting a mini-boss, etc etc"
#
# A fight, then - which is the option he listed and the one this engine
# already does well. `battle.py` imports no pygame on purpose (rule 2.3),
# so a timing puzzle would need a frame clock combat does not have; a
# guardian needs nothing new at all.
#
# **They are not wardens and not field bosses.** No Memory, no portal, no
# chapter, no lair, no respawn - one guardian per waygate, beaten once,
# and the waygate is yours from then on.

GUARDIANS = [
    BossDef("guard_hallow", "The Field Warden", "It has stood here longest",
            "stone_golem", 4, "physical", 250, 19, 15, 10, 10, 8,
            (60, 96, 62), "meadow", ("slam", "harden"), ("quake",),
            "It was here before the field was a field.", 160, 150),
    BossDef("guard_cut", "The Mason's Last", "Cut to fit, and left",
            "stone_golem", 9, "physical", 480, 33, 26, 14, 18, 10,
            (92, 84, 78), "cave", ("slam", "harden"), ("quake", "enrage"),
            "Somebody made this and did not sign it.", 400, 380),
    BossDef("guard_watch", "The Unlit", "It does not need the lamps",
            "night_warden", 14, "shadow", 640, 42, 28, 46, 30, 26,
            (52, 48, 74), "cave", ("hex", "screech"), ("venom", "enrage"),
            "The dark here is a room, and you are in it.", 620, 580),
    BossDef("guard_ninety", "The Milestone", "It counts you too",
            "imp", 19, "fire", 860, 55, 34, 52, 34, 24,
            (128, 74, 44), "volcano", ("ember", "inferno"),
            ("enrage", "quake"),
            "It has been keeping a total. You are in it now.", 880, 820),
    BossDef("guard_relay", "The Unsent", "Held, for collection",
            "wisp", 24, "thunder", 1020, 62, 36, 68, 42, 38,
            (76, 92, 128), "tundra", ("gale", "hex"),
            ("regenerate", "screech"),
            "It is still waiting for somebody who is not you.", 1080, 1000),
    BossDef("guard_ain", "The Sweeper's Charge", "Kept tidy, kept shut",
            "void_sovereign", 29, "shadow", 1300, 74, 48, 72, 50, 40,
            (68, 60, 88), "wastes", ("hex", "gale", "harden"),
            ("quake", "regenerate"),
            "Somebody has been keeping this clean for a very long time.",
            1400, 1300),
]

GUARDIAN_BY_KEY = {g.key: g for g in GUARDIANS}


def guardian_for(waystation_key):
    """The guardian standing at this waystation's gate, or None."""
    return GUARDIAN_BY_KEY.get("guard_%s" % waystation_key)


def field_boss_for(biome):
    """The field boss that belongs in this biome, or None."""
    return FIELD_BOSS_BY_BIOME.get(biome)




class Boss(Combatant):
    """A boss fight. Phase two triggers below 50% HP and changes the moveset."""

    def __init__(self, definition, mods=(1.0, 1.0, 1.0, 1.0, 0.6)):
        super().__init__(definition.name, definition.level)
        self.definition = definition
        self.sprite = definition.sprite
        self.element = definition.element
        self.can_flee = False
        self._max_hp = int(definition.hp * mods[0])
        self._atk = int(definition.atk * mods[1])
        self.hp = self._max_hp
        self.phase = 1
        self.enraged = False
        self.xp_reward = int(definition.xp * mods[2])
        self.gold_reward = int(definition.gold * mods[3])

    def max_hp(self):
        return self._max_hp

    def stat(self, key):
        d = self.definition
        raw = {"atk": self._atk, "defense": d.defense, "mag": d.mag,
               "res": d.res, "spd": d.spd, "crit": 8, "crit_dmg": 70,
               "lifesteal": 0}.get(key, 0)
        if self.phase == 2:
            raw = int(raw * 1.20)
        return max(0, int(raw * modifier(self.statuses, key)))

    def check_phase(self):
        """Returns True on the frame the boss flips to phase two."""
        if self.phase == 1 and self.hp <= self.max_hp() * 0.5:
            self.phase = 2
            self.clear_statuses(only_bad=True)
            return True
        return False

    def choose_action(self):
        from .skills import ENEMY_SKILLS
        keys = (self.definition.phase2_skills if self.phase == 2
                else self.definition.skills)
        pool = [ENEMY_SKILLS[k] for k in keys if k in ENEMY_SKILLS]
        if not pool:
            return None
        if self.hp < self.max_hp() * 0.3:
            healers = [s for s in pool if s.status == "regen"]
            if healers and rng.chance(0.4):
                return rng.choice(healers)
        # Bosses use abilities far more often than trash mobs.
        return rng.choice(pool) if rng.chance(0.78) else None


# How likely a roaming encounter is a pack rather than a lone monster, by the
# number of heroes in it. Solo play stays mostly one-on-one: the group fight is
# a change of pace there, not the norm. Co-op leans on it, because a party that
# outnumbers its enemy every time has no fight to lose.
PACK_ODDS = {
    #      1 foe  2     3     4     5     6
    1: (0.74, 0.21, 0.05, 0.00, 0.00, 0.00),
    2: (0.30, 0.44, 0.21, 0.05, 0.00, 0.00),
    3: (0.10, 0.32, 0.38, 0.16, 0.04, 0.00),
    4: (0.03, 0.14, 0.27, 0.29, 0.18, 0.09),
}
MAX_PACK = 6


# Extra monster levels, by how many heroes are in the fight. Section 23
# measured a full room winning every fight the game can generate; this is
# half of the answer (the other half is `battle.FOCUS_LEAN`).
#
# **Measured, not fitted, and steeper than linear on purpose.** A straight
# +1 per hero left a duo at 75% and a full room at 95%, because a party's
# advantage does not grow in a straight line either - four heroes focusing
# one monster remove an enemy action every single round, and that compounds.
# At these steps every party size sits in the mid-to-high seventies at its
# own worst case, which is a little easier than solo's 65% and considerably
# easier than that in the average fight, since `PACK_ODDS` weights the
# middle of the range rather than the top.
#
# Solo is 0 and so is exactly unaffected; the measured per-class table in
# section 6 depends on that.
PARTY_LEVEL_STEP = {1: 0, 2: 1, 3: 2, 4: 4}


def party_level(level, party=1):
    """The level a pack turns up at, given how many heroes it is meeting."""
    party = int(clamp(party, 1, max(PARTY_LEVEL_STEP)))
    return int(clamp(level + PARTY_LEVEL_STEP[party], 1, MAX_LEVEL))


def pack_softening(count, party=1):
    """How much weaker each monster in a pack is than a lone one would be.

    Keyed on monsters **per hero**, not on the raw count. A pack is softened
    because it out-acts you - two monsters is not twice the fight, it is
    closer to four times, because they both move every round while you move
    once. That reasoning is about the *ratio*, and keying it on the count
    instead handed a party of four a being-outnumbered discount while it was
    in fact outnumbering: six monsters split four ways is lighter pressure
    than two monsters on one hero, and they were being weakened for it.

    Solo is unchanged by design - one hero against N is a pressure of N, so
    every threshold lands exactly where the old count-based one did. The
    party rows are the ones that move.
    """
    pressure = count / max(1, int(party))
    if pressure <= 1.0:
        return 1.0, 1.0          # you have them outnumbered; no discount
    if pressure < 2.0:
        return 0.90, 0.94
    if pressure < 4.0:
        return 0.78, 0.86
    return 0.66, 0.78


def max_enemies(party=1):
    """The most monsters a party of `party` should ever face at once.

    Measured, not guessed: a level-14 solo Warden wins 30/30 against one
    monster, 27/30 against three and 5/30 against five (tools/balance.py).
    Three is therefore the honest solo ceiling - a fight you lose five times
    in six is not a difficulty spike, it is a wall. Each extra hero buys one
    more, because each extra hero is another action per round.
    """
    return int(clamp(2 + int(party), 3, MAX_PACK))


def boss_minions(party=1):
    """How many hangers-on a warden brings.

    Fewer than an ordinary pack of the same size: a boss already acts every
    round and hits like one, so its friends are there to split your attention
    rather than to double the damage. Solo can face a lone warden - that
    fight is tuned and should stay reachable - but usually gets one add.
    """
    ceiling = max(0, max_enemies(party) - 2)
    if ceiling <= 0:
        return 0
    return rng.rint(0 if party <= 1 else 1, ceiling)


def pack_size(party=1):
    """How many monsters turn up for a party of `party` heroes."""
    party = int(clamp(party, 1, max(PACK_ODDS)))
    odds = PACK_ODDS[party][:max_enemies(party)]
    roll = rng.rfloat()
    running = 0.0
    for count, weight in enumerate(odds, start=1):
        running += weight
        if roll <= running:
            return count
    return len(odds)


def make_encounter(level, biome="meadow", mods=(1.0, 1.0, 1.0, 1.0, 0.6),
                   party=1, count=None):
    """One roaming encounter: a list of monsters, never empty.

    Packs are made slightly weaker per head than a lone monster would be. Two
    full-strength monsters is not twice the fight, it is closer to four times,
    because they both act every round while you still only act once.

    `count` forces a pack size instead of rolling one. Only `tools/balance.py`
    passes it: measuring a cap means choosing the number rather than hoping
    the dice offer it. It lives here rather than in the harness so that the
    softening rules have exactly one home - a second copy of them in a
    measuring tool would eventually measure a game nobody is playing.
    """
    count = pack_size(party) if count is None \
        else int(clamp(count, 1, MAX_PACK))
    # Bigger parties meet tougher monsters rather than only more of them.
    # More would be the obvious lever and it does not work: the arena fans
    # them across the screen, and section 23 measured that a party of four
    # needs about eleven to face solo's odds - which is unreadable long
    # before it is balanced.
    level = party_level(level, party)
    if count == 1:
        return [make_enemy(level, biome, mods)]
    hp, atk, xp, gold, flee = mods
    hp_mult, atk_mult = pack_softening(count, party)
    softened = (hp * hp_mult, atk * atk_mult,
                xp * 0.72, gold * 0.72, flee)
    spread = max(1, level - 1)
    return [make_enemy(int(clamp(spread + rng.rint(0, 2), 1, MAX_LEVEL)),
                       biome, softened)
            for _ in range(count)]


def make_boss_encounter(definition, mods=(1.0, 1.0, 1.0, 1.0, 0.6),
                        party=1, biome="ruins"):
    """A warden, plus whatever it brought with it.

    The minions are ordinary monsters several levels below the boss and
    softened further, because their job is to make you choose a target rather
    than to out-damage the thing you came for. Killing them is not optional -
    the fight ends when the whole group is down - so a party that ignores
    them spends the whole battle being interrupted.
    """
    boss = Boss(definition, mods)
    count = boss_minions(party)
    if not count:
        return [boss]
    hp, atk, xp, gold, flee = mods
    weakened = (hp * 0.55, atk * 0.6, xp * 0.5, gold * 0.5, flee)
    level = max(1, int(getattr(boss, "level", 10)) - 3)
    return [boss] + [make_enemy(level, biome, weakened)
                     for _ in range(count)]


def make_enemy(level, biome="meadow", mods=(1.0, 1.0, 1.0, 1.0, 0.6)):
    """Spawn a level-appropriate monster suited to the biome.

    **"night" is a real biome with nothing else in it** (item 0bx), so
    The Long Night gets the dead and the island never does: no archetype
    lists both, and the fallback below only fires for a biome nobody
    has written monsters for.
    """
    level = int(clamp(level, 1, MAX_LEVEL))
    pool = [a for a in ARCHETYPES if biome in a.biomes] or ARCHETYPES
    archetype = rng.choice(pool)
    labels = [v[0] for v in PREFIX_VARIANTS]
    weights = [v[5] for v in PREFIX_VARIANTS]
    variant = rng.choices(labels, weights=weights, k=1)[0]
    return Enemy(archetype, level, variant, mods)


def element_multiplier(attacker_element, defender_element):
    """1.5x on an advantageous matchup, 0.75x when resisted."""
    if ELEMENT_STRONG_VS.get(attacker_element) == defender_element:
        return 1.5
    if ELEMENT_STRONG_VS.get(defender_element) == attacker_element:
        return 0.75
    return 1.0
