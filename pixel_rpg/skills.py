"""Skills and status effects.

Combat in the original was Attack / Defend / Potion / Run. This module adds a
resource (MP), a dozen abilities gated by level, and eleven status effects that
tick between turns.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import security as sec
from .util import clamp


@dataclass(frozen=True)
class StatusDef:
    key: str
    name: str
    color: tuple
    good: bool
    max_stacks: int = 1
    description: str = ""


STATUSES = {
    s.key: s for s in (
        StatusDef("poison", "Poison", (128, 210, 96), False, 3,
                  "Loses 6% max HP each turn."),
        StatusDef("burn", "Burn", (250, 128, 52), False, 3,
                  "Loses 9% max HP each turn, halved duration."),
        StatusDef("bleed", "Bleed", (214, 62, 72), False, 3,
                  "Loses HP based on the attacker's power."),
        StatusDef("chill", "Chill", (130, 214, 240), False, 2,
                  "Speed cut by 35%."),
        StatusDef("stun", "Stun", (252, 226, 104), False, 1,
                  "Skips the next turn."),
        StatusDef("curse", "Curse", (168, 108, 226), False, 2,
                  "Magic and resistance cut by 30%."),
        StatusDef("weaken", "Weaken", (170, 140, 190), False, 2,
                  "Attack cut by 30%."),
        StatusDef("regen", "Regen", (118, 220, 138), True, 3,
                  "Restores 7% max HP each turn."),
        StatusDef("might", "Might", (250, 176, 72), True, 3,
                  "Attack raised by 30%."),
        StatusDef("guard", "Guard", (108, 164, 252), True, 3,
                  "Defense raised by 40%."),
        StatusDef("haste", "Haste", (252, 240, 180), True, 2,
                  "Speed raised by 40%."),
        StatusDef("shield", "Shield", (180, 220, 250), True, 1,
                  "Absorbs damage until it breaks."),
        StatusDef("focus", "Focus", (226, 140, 250), True, 2,
                  "Critical chance raised by 25 points."),
    )
}

DOT_STATUSES = ("poison", "burn", "bleed")


@dataclass
class Status:
    key: str
    turns: int = 3
    magnitude: int = 0        # used by shield (absorb pool) and bleed

    @property
    def info(self):
        return STATUSES.get(self.key, STATUSES["poison"])

    def to_dict(self):
        return {"key": self.key, "turns": self.turns,
                "magnitude": self.magnitude}

    @classmethod
    def from_dict(cls, data):
        raw = sec.pick_known_keys(data, {"key", "turns", "magnitude"})
        key = sec.safe_choice(raw.get("key"), tuple(STATUSES), "")
        if not key:
            return None
        return cls(key=key,
                   turns=sec.safe_int(raw.get("turns"), 0, 99, 1),
                   magnitude=sec.safe_int(raw.get("magnitude"), 0, 99999, 0))


@dataclass(frozen=True)
class Skill:
    key: str
    name: str
    mp: int
    kind: str                 # attack | heal | buff | debuff | drain | special
    power: float = 1.0        # multiplier on the relevant stat
    element: str = "physical"
    scales: str = "atk"       # atk | mag
    target: str = "enemy"     # enemy | self
    status: str = ""          # status applied on hit
    status_chance: float = 0.0
    status_turns: int = 3
    hits: int = 1
    unlock: int = 1
    sfx: str = "swing"
    description: str = ""
    #: What crosses the arena when this is cast, overriding the shape
    #: `skill_look` would work out (item 0ba). Empty means *derive it*,
    #: which is the answer for nearly every skill and the reason there is
    #: not a column of hand-typed shapes here.
    look: str = ""


PLAYER_SKILLS = [
    Skill("strike", "Power Strike", 4, "attack", 1.55, "physical", "atk",
          unlock=1, sfx="swing",
          description="A heavy overhead blow."),
    Skill("guardup", "Iron Guard", 5, "buff", 0.0, "physical", "atk", "self",
          status="guard", status_chance=1.0, status_turns=3, unlock=2,
          sfx="block", description="Raise defense by 40% for 3 turns."),
    Skill("firebolt", "Firebolt", 7, "attack", 1.60, "fire", "mag",
          status="burn", status_chance=0.45, status_turns=2, unlock=3,
          sfx="fire", description="Hurls flame that may burn."),
    Skill("mend", "Mend", 8, "heal", 1.80, "holy", "mag", "self",
          unlock=4, sfx="heal", description="Restores HP based on magic."),
    Skill("frostbite", "Frostbite", 9, "attack", 1.45, "ice", "mag",
          status="chill", status_chance=0.60, status_turns=2, unlock=6,
          sfx="ice", description="Icy shard that slows the target."),
    Skill("rally", "Rally", 8, "buff", 0.0, "physical", "atk", "self",
          status="might", status_chance=1.0, status_turns=3, unlock=8,
          sfx="unlock", description="Raise attack by 30% for 3 turns."),
    Skill("flurry", "Flurry", 12, "attack", 0.62, "physical", "atk",
          hits=3, unlock=10, sfx="swing",
          description="Three rapid cuts."),
    Skill("thunder", "Thunderclap", 14, "attack", 2.05, "thunder", "mag",
          status="stun", status_chance=0.30, status_turns=1, unlock=12,
          sfx="thunder", description="Lightning that can stun."),
    Skill("drain", "Life Drain", 13, "drain", 1.35, "shadow", "mag",
          unlock=14, sfx="shadow",
          description="Steals half the damage dealt as HP."),
    Skill("bulwark", "Aegis", 16, "buff", 2.5, "holy", "mag", "self",
          status="shield", status_chance=1.0, status_turns=4, unlock=16,
          sfx="holy", description="Absorbs a burst of incoming damage."),
    Skill("execute", "Executioner", 20, "attack", 2.60, "physical", "atk",
          status="bleed", status_chance=0.55, status_turns=3, unlock=18,
          sfx="crit", description="Massive strike that causes bleeding."),
    Skill("judgment", "Judgment", 26, "attack", 3.10, "holy", "mag",
          unlock=22, sfx="holy",
          description="Calls down a pillar of light."),
]

# ------------------------------------------------------ what a spell looks like ---
#
# **Item 0ba** (2026-09-06). Savvydev: *"each spell should visually look
# different."*
#
# Every magical attack in the game fired the same shape - `"bolt"` - and
# differed only in colour, so a Firebolt and a Meteor and a Frost Lance
# were one drawing tinted three ways.
#
# **Derived, not tabulated.** A skill already says what element it is,
# how hard it hits and what it does; a shape read off those is right for
# a skill nobody has written a shape for yet, which is the same rule
# `heft` (§70) and `skill_motion` (§71) are built on. `Skill.look`
# exists for the handful that want to disagree with the derivation.

#: The shape each element throws. **Physical magic gets `wave`**, which
#: is the shape for "force with no material" - and it is the fallback,
#: so an element added later is a crescent until somebody decides
#: otherwise.
LOOK_BY_ELEMENT = {
    "fire": "orb",
    "ice": "shard",
    "thunder": "spark",
    "shadow": "mote",
    "holy": "beam",
    "physical": "wave",
}

#: Above this `power`, a spell throws the heavy version of its shape.
#: The same number `Battle.SKILL_HUGE` is measured against, because a
#: spell that reads as big should also *look* big (§70's whole argument).
LOOK_HEAVY_AT = 2.2


def skill_look(skill):
    """The shape this ability throws. Never raises.

    A drain is a `mote` whatever it is made of - the shape is the
    *taking*, not the element - and everything else asks the element.
    """
    if getattr(skill, "look", ""):
        return str(skill.look)
    if getattr(skill, "kind", "") == "drain":
        return "mote"
    return LOOK_BY_ELEMENT.get(getattr(skill, "element", ""), "wave")


def skill_scale(skill):
    """How big it draws, 1.0 to about 2.0, from what it hits for."""
    power = float(getattr(skill, "power", 1.0) or 1.0)
    return 1.0 + min(1.0, max(0.0, (power - 1.0) / LOOK_HEAVY_AT))


SKILLS_BY_KEY = {s.key: s for s in PLAYER_SKILLS}

# Abilities used by monsters and bosses. Kept separate so a save file can never
# hand the player a boss-only move by editing a skill list.
ENEMY_SKILLS = {
    "bite": Skill("bite", "Bite", 0, "attack", 1.20, "physical", "atk",
                  status="bleed", status_chance=0.25, sfx="hit"),
    "venom": Skill("venom", "Venom Spit", 0, "attack", 0.95, "shadow", "atk",
                   status="poison", status_chance=0.55, sfx="shadow"),
    "screech": Skill("screech", "Screech", 0, "debuff", 0.0, "physical", "atk",
                     status="weaken", status_chance=0.75, sfx="error"),
    "ember": Skill("ember", "Ember", 0, "attack", 1.25, "fire", "mag",
                   status="burn", status_chance=0.40, sfx="fire"),
    "frost": Skill("frost", "Frost Touch", 0, "attack", 1.20, "ice", "mag",
                   status="chill", status_chance=0.50, sfx="ice"),
    "slam": Skill("slam", "Slam", 0, "attack", 1.65, "physical", "atk",
                  status="stun", status_chance=0.20, sfx="hit"),
    "quake": Skill("quake", "Quake", 0, "attack", 1.80, "physical", "atk",
                   sfx="hit"),
    "gale": Skill("gale", "Gale Slash", 0, "attack", 1.45, "thunder", "mag",
                  status="stun", status_chance=0.25, sfx="thunder"),
    "hex": Skill("hex", "Hex", 0, "debuff", 0.0, "shadow", "mag",
                 status="curse", status_chance=0.80, sfx="shadow"),
    "inferno": Skill("inferno", "Inferno", 0, "attack", 2.10, "fire", "mag",
                     status="burn", status_chance=0.70, sfx="fire"),
    "voidlash": Skill("voidlash", "Void Lash", 0, "attack", 2.30, "shadow",
                      "mag", status="curse", status_chance=0.45, sfx="shadow"),
    "regenerate": Skill("regenerate", "Regenerate", 0, "buff", 0.0,
                        "holy", "mag", "self", status="regen",
                        status_chance=1.0, sfx="heal"),
    "harden": Skill("harden", "Harden", 0, "buff", 0.0, "physical", "atk",
                    "self", status="guard", status_chance=1.0, sfx="block"),
    "enrage": Skill("enrage", "Enrage", 0, "buff", 0.0, "physical", "atk",
                    "self", status="might", status_chance=1.0, sfx="boss"),
}


def skills_for_level(level):
    return [s for s in PLAYER_SKILLS if s.unlock <= level]


def newly_unlocked(old_level, new_level):
    return [s for s in PLAYER_SKILLS if old_level < s.unlock <= new_level]


# ------------------------------------------------------------ status maths ---

def modifier(statuses, stat):
    """Multiplier applied to a stat given the active status list."""
    keys = {s.key for s in statuses}
    mult = 1.0
    if stat == "atk":
        if "might" in keys:
            mult *= 1.30
        if "weaken" in keys:
            mult *= 0.70
    elif stat == "defense":
        if "guard" in keys:
            mult *= 1.40
    elif stat == "spd":
        if "haste" in keys:
            mult *= 1.40
        if "chill" in keys:
            mult *= 0.65
    elif stat in ("mag", "res"):
        if "curse" in keys:
            mult *= 0.70
    return mult


def crit_bonus(statuses):
    return 25 if any(s.key == "focus" for s in statuses) else 0


def dot_damage(status, max_hp):
    """HP lost this turn from one damage-over-time effect."""
    if status.key == "poison":
        return max(1, int(max_hp * 0.06))
    if status.key == "burn":
        return max(1, int(max_hp * 0.09))
    if status.key == "bleed":
        return max(1, status.magnitude or int(max_hp * 0.05))
    return 0


def regen_amount(max_hp):
    return max(1, int(max_hp * 0.07))


# --------------------------------------------------------- class abilities ---
# Added with the class system. PLAYER_SKILLS above stays the shared pool that
# every class can draw from; these are the ones that give each class its shape.

CLASS_SKILLS = [
    # -- Warden ------------------------------------------------------------
    Skill("shieldbash", "Shield Bash", 6, "attack", 1.25, "physical", "atk",
          status="stun", status_chance=0.45, status_turns=1, sfx="block",
          description="A blow with the shield rim. Often stuns."),
    Skill("riposte", "Riposte", 9, "buff", 0.0, "physical", "atk", "self",
          status="focus", status_chance=1.0, status_turns=3, sfx="block",
          description="Read the next swing. Crit chance way up."),
    Skill("laststand", "Last Stand", 14, "heal", 0.0, "holy", "atk", "self",
          sfx="unlock",
          description="Heals more the closer you are to falling."),
    # -- Emberwright -------------------------------------------------------
    Skill("manawell", "Mana Well", 0, "buff", 0.0, "physical", "mag", "self",
          sfx="heal",
          description="Draw on the road itself. Restores MP, costs a turn."),
    Skill("meteor", "Meteor", 30, "attack", 3.40, "fire", "mag",
          status="burn", status_chance=0.80, status_turns=3, sfx="fire",
          description="Calls down a burning stone. Ruinous."),
    Skill("arcanesurge", "Arcane Surge", 16, "attack", 1.05, "thunder", "mag",
          hits=3, sfx="thunder",
          description="Three cracks of raw force."),
    # -- Cutpurse ----------------------------------------------------------
    Skill("backstab", "Backstab", 8, "attack", 1.90, "physical", "atk",
          sfx="crit",
          description="Find the gap. Always finds the gap."),
    Skill("poisonblade", "Poison Blade", 7, "attack", 1.10, "shadow", "atk",
          status="poison", status_chance=0.85, status_turns=4, sfx="shadow",
          description="Coats the edge. They will feel it later."),
    Skill("vanish", "Vanish", 11, "buff", 0.0, "shadow", "spd", "self",
          status="haste", status_chance=1.0, status_turns=3, sfx="back",
          description="Gone. Speed up, and escape is certain next turn."),
    # -- Oathbound ---------------------------------------------------------
    Skill("smite", "Smite", 10, "attack", 1.70, "holy", "mag", sfx="holy",
          description="Light with an opinion about you."),
    Skill("sanctuary", "Sanctuary", 12, "buff", 0.0, "holy", "mag", "self",
          status="regen", status_chance=1.0, status_turns=4, sfx="holy",
          description="A slow, steady mending."),
    # -- Revenant ----------------------------------------------------------
    Skill("bloodpact", "Blood Pact", 0, "attack", 2.80, "shadow", "mag",
          sfx="shadow",
          description="Costs health, not magic. Hits like it means it."),
    Skill("wither", "Wither", 11, "debuff", 0.0, "shadow", "mag",
          status="weaken", status_chance=0.90, status_turns=3, sfx="shadow",
          description="Takes the strength out of them."),
    Skill("hollowgrasp", "Hollow Grasp", 15, "drain", 1.80, "shadow", "mag",
          status="curse", status_chance=0.50, sfx="shadow",
          description="Drains deeply. Half the damage comes back as health."),
    # -- Wayfarer ----------------------------------------------------------
    Skill("luckystrike", "Lucky Strike", 9, "attack", 1.45, "physical", "atk",
          sfx="coin",
          description="Element decided on the swing. So is the outcome."),
    Skill("trailcraft", "Trailcraft", 8, "buff", 0.0, "physical", "atk",
          "self", status="might", status_chance=1.0, status_turns=4,
          sfx="unlock",
          description="Road knowledge, applied violently. Long-lasting."),
]

for _skill in CLASS_SKILLS:
    SKILLS_BY_KEY[_skill.key] = _skill

ALL_SKILLS = PLAYER_SKILLS + CLASS_SKILLS

# Skills whose cost or effect is not a plain MP spend, handled in battle.py.
HP_COST_SKILLS = {"bloodpact": 0.18}      # fraction of max HP
SPECIAL_SKILLS = {"manawell", "laststand", "vanish", "luckystrike"}


def skill(key):
    return SKILLS_BY_KEY.get(key)
