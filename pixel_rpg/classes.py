"""Character classes.

Each class changes three things: the stat curve, which skills unlock and when,
and a signature passive that alters a rule of combat rather than a number.
The passive is the point - a Cutpurse and a Warden should want different turns,
not the same turn with different figures on it.

Every class also carries an `archetype`: the ordinary RPG word for what it is.
The in-world names are the ones worth keeping, but "Emberwright" does not tell
a new player they are picking the mage, and a roster of six invented words asks
somebody to learn the game before they can choose how to play it. The archetype
is shown beside the name and is never used for anything mechanical.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlayerClass:
    key: str
    name: str
    title: str
    blurb: tuple
    color: tuple
    archetype: str = ""      # the plain RPG word: Mage, Assassin, Knight...
    hp: float = 1.0          # multipliers on the shared base curve
    mp: float = 1.0
    atk: float = 1.0
    defense: float = 1.0
    mag: float = 1.0
    res: float = 1.0
    spd: float = 1.0
    crit: float = 1.0
    skills: tuple = ()       # (skill_key, unlock_level) in order
    signature: str = ""
    signature_name: str = ""
    signature_text: str = ""
    # **What this class trained with** (2026-09-01, s47). Carrying one of
    # these is worth a real bonus, and `equip_best` will not hand a
    # Cutpurse a bow while a dagger is in the bag. Empty means no
    # preference, which no class has and a new one might.
    favours: tuple = ()
    start_weapon: str = "Shortsword"
    start_armor: str = "Leather Vest"
    start_consumables: tuple = (("potion", 3),)
    unlock: str = ""         # empty means available from the start
    unlock_hint: str = ""
    # **The swing that costs nothing** (2026-09-06, item 0ai). Savvydev:
    # *"the basic attack that costs no mp should be different according to
    # each class"*. A key into `STRIKES`; every class has one.
    strike: str = "plain"


@dataclass(frozen=True)
class Strike:
    """A class's ordinary attack: what it is called and what it does.

    **Thirteen classes shared one swing**, which is what he noticed: the
    free attack is the move you press most, and it was the only part of a
    class that was not the class. Savvydev, 2026-09-06: *"the basic attack
    that costs no mp should be different according to each class"*.

    Kept to **one clause each**, on purpose. A basic attack is the floor
    every other number in the game is measured against - `tools/balance.py`
    leans on it heavily - so these are chosen to say what a class *is*
    rather than to move what it wins, and the balance table is diffed
    either side of them (rule 31.5).

    `anim` names the movement `battlescreen` plays, so a Longshot's swing
    stops looking like a Warden's (item 0al).
    """
    key: str
    name: str            # what the log calls it
    note: str            # one line, for the hero sheet
    anim: str = "lunge"  # how it moves; see `battle._step_out`
    #: Fraction of the hit that splashes onto one other enemy.
    cleave: float = 0.0
    #: How many hits it lands. Two smaller ones lose more to armour and
    #: gain more from a crit chance, which is the whole trade.
    hits: int = 1
    #: Total power before it is split between `hits`. **A flurry needs
    #: more than 1.0 to break even**: defence subtracts from every hit, so
    #: two halves land for less than one whole against anything armoured -
    #: measured at **-45%** on a Cutpurse before this existed.
    power: float = 1.0
    #: Share of the target's defence ignored.
    pierce: float = 0.0
    #: Share of the attacker's magic added to a physical swing.
    channel: float = 0.0
    #: What crosses the arena, if anything: "arrow", "bolt", "thrown" -
    #: or empty for a swing that never leaves the hand (item 0al).
    shot: str = ""


STRIKES = {
    "plain": Strike("plain", "attack", "An honest swing.", "lunge"),
    "cleave": Strike("cleave", "sweep",
                     "Carries into a second enemy for a quarter.",
                     "lunge", cleave=0.25),
    "flurry": Strike("flurry", "flurry",
                     "Two quick hits instead of one heavy one.",
                     "dart", hits=2, power=1.26),
    "pierce": Strike("pierce", "thrust",
                     "Finds the gap: ignores a third of their guard.",
                     "dart", pierce=0.34),
    # **A bow is not a thrust** (item 0al). `pierce` describes what the
    # damage does; a Longshot doing it from thirty feet is a different
    # picture, and until projectiles existed there was no way to say so.
    "loose": Strike("loose", "loosed arrow",
                    "An arrow, and it finds the gap: a third of their "
                    "guard ignored.",
                    "dart", pierce=0.34, shot="arrow"),
    "channel": Strike("channel", "channelled strike",
                      "Ordinary steel with something else behind it.",
                      "cast", channel=0.30),
    # **A throw is not a cast** (item 0al(b), s71). This strike has been
    # named `hurl` since it existed and playing `cast` - which steps
    # *forward*, so an Alchemist charged the enemy and then threw
    # something at them from arm's length. `hurl` winds back and snaps,
    # and ends where it started: the flask does the travelling (s69).
    "hurl": Strike("hurl", "hurled flask",
                   "Something breakable, thrown, with magic behind it.",
                   "hurl", channel=0.30, shot="thrown"),
    # **Spellblade is deliberately not `channel`.** Runeblade (s52)
    # already folds 40% of its magic into every ordinary swing, so giving
    # it a channelled strike on top would be the same idea charged twice -
    # measured at +30% on a class whose signature *is* this. Its swing is
    # its signature; the animation is what makes it look like one.

}


def strike_for(class_key):
    """The `Strike` a class swings with. Never raises."""
    return STRIKES.get(get(class_key).strike, STRIKES["plain"])


CLASSES = [
    PlayerClass(
        "warden", "Warden", "the one who stands",
        ("Heavy, slow, and very hard to remove from a doorway.",
         "Gets stronger the worse things get."),
        (108, 164, 252),
        archetype="Knight",
        hp=1.18, mp=0.75, atk=1.05, defense=1.14, mag=0.70, res=1.08,
        spd=0.85, crit=0.80,
        skills=(("strike", 1), ("guardup", 2), ("shieldbash", 4),
                ("riposte", 7), ("mend", 9), ("laststand", 12),
                ("rally", 15), ("execute", 18), ("bulwark", 22)),
        signature="bulwark",
        signature_name="Bulwark",
        signature_text="Below 35% health, incoming damage is cut by 40%.",
        start_weapon="Longsword", start_armor="Chain Mail",
        start_consumables=(("potion", 3), ("wardstone", 1)),
        favours=('blade', 'hammer'),
        strike="cleave"
    ),
    PlayerClass(
        "emberwright", "Emberwright", "the one who burns it down",
        ("Enormous magic, paper defences, a habit of escalating.",
         "Every spell in a row makes the next one worse for them."),
        (250, 128, 52),
        archetype="Mage",
        hp=0.80, mp=1.60, atk=0.65, defense=0.75, mag=1.55, res=1.15,
        spd=1.00, crit=0.95,
        skills=(("firebolt", 1), ("manawell", 3), ("frostbite", 5),
                ("mend", 7), ("thunder", 9), ("arcanesurge", 12),
                ("bulwark", 16), ("judgment", 20), ("meteor", 24)),
        signature="kindling",
        signature_name="Kindling",
        signature_text="Each spell cast in a row adds 12% spell damage, up "
                       "to 60%. A physical attack resets it.",
        start_weapon="Oak Staff", start_armor="Cloth Robe",
        start_consumables=(("potion", 2), ("ether", 3)),
        favours=('stave',),
        strike="channel"
    ),
    PlayerClass(
        "cutpurse", "Cutpurse", "the one who is already gone",
        ("Fast, precise, and morally flexible about other people's gold.",
         "Opens every fight with a guaranteed critical hit."),
        (118, 220, 138),
        archetype="Assassin",
        hp=1.10, mp=0.95, atk=1.10, defense=1.00, mag=0.85, res=0.95,
        spd=1.55, crit=1.70,
        skills=(("backstab", 1), ("poisonblade", 3), ("strike", 5),
                ("drain", 7), ("vanish", 9), ("frostbite", 12),
                ("flurry", 15), ("execute", 18), ("judgment", 23)),
        signature="opportunist",
        signature_name="Opportunist",
        signature_text="Your first strike of every battle always crits, and "
                       "you take 15% more gold from every kill.",
        start_weapon="Dagger", start_armor="Leather Vest",
        start_consumables=(("potion", 2), ("smoke", 2)),
        favours=('dagger', 'bow'),
        strike="flurry"
    ),
]

CLASSES += [
    PlayerClass(
        "oathbound", "Oathbound", "the one who keeps promises",
        ("Holy damage, real healing, and no wasted turns.",
         "Mending yourself hurts whatever you are looking at."),
        (252, 240, 180),
        archetype="Paladin",
        hp=1.00, mp=1.20, atk=1.00, defense=0.98, mag=1.08, res=1.06,
        spd=0.95, crit=0.90,
        skills=(("smite", 1), ("mend", 3), ("guardup", 5),
                ("sanctuary", 8), ("rally", 11), ("bulwark", 14),
                ("thunder", 17), ("execute", 20), ("judgment", 24)),
        signature="vow",
        signature_name="Vow",
        signature_text="Whenever you heal yourself, the enemy takes 35% of "
                       "the amount as holy damage.",
        start_weapon="Shortsword", start_armor="Scale Armor",
        start_consumables=(("potion", 3), ("antidote", 2)),
        unlock="ending_debt",
        unlock_hint="Reach THE DEBT ending.",
        favours=('blade', 'hammer'),
        strike="cleave"
    ),
    PlayerClass(
        "revenant", "Revenant", "the one who came back wrong",
        ("Cannot be healed past half. Does not especially need to be.",
         "The emptier you are, the harder you hit."),
        (168, 108, 226),
        archetype="Dark Knight",
        hp=1.16, mp=1.10, atk=1.15, defense=1.02, mag=1.30, res=1.00,
        spd=1.05, crit=1.10,
        skills=(("bloodpact", 1), ("wither", 4), ("drain", 6),
                ("frostbite", 9), ("hollowgrasp", 12), ("rally", 15),
                ("execute", 18), ("meteor", 22), ("judgment", 26)),
        signature="hollow",
        signature_name="Hollow",
        signature_text="Healing cannot lift you above 50% health, but your "
                       "damage rises by up to 60% as your health falls.",
        start_weapon="Scythe", start_armor="Cloth Robe",
        start_consumables=(("potion", 2), ("ether", 2)),
        unlock="ending_seventh",
        unlock_hint="Reach any ending.",
        favours=('polearm', 'dagger'),
        strike="pierce"
    ),
    PlayerClass(
        "wayfarer", "Wayfarer", "the one who knows the road",
        ("Even everywhere, exceptional nowhere, and absurdly lucky.",
         "Finds better things than anyone has a right to."),
        (250, 200, 96),
        archetype="Generalist",
        hp=1.18, mp=1.05, atk=1.12, defense=1.10, mag=1.10, res=1.08,
        spd=1.15, crit=1.15,
        skills=(("luckystrike", 1), ("trailcraft", 3), ("mend", 5),
                ("firebolt", 7), ("flurry", 10), ("guardup", 13),
                ("drain", 16), ("thunder", 19), ("judgment", 22)),
        signature="fortune",
        signature_name="Fortune's Road",
        signature_text="Much better loot rarity, 30% more gold, an "
                       "opening Memory, and once a battle a blow "
                       "that would fell you leaves you standing.",
        start_weapon="Rapier", start_armor="Leather Vest",
        start_consumables=(("potion", 3), ("ether", 1), ("smoke", 1)),
        unlock="codex_12",
        unlock_hint="Unlock 12 codex entries.",
        favours=('blade', 'bow'),
        strike="plain"
    ),
]

# The two plainest archetypes the roster was missing. Both are starters: a new
# player looking for "the archer" or "the berserker" should find one without
# having to finish a run first.
CLASSES += [
    PlayerClass(
        "longshot", "Longshot", "the one who never closes the distance",
        ("Patient, accurate, and happiest with a fight between you.",
         "Hits hardest on a turn the enemy failed to touch you."),
        (140, 208, 176),
        archetype="Archer",
        hp=0.95, mp=1.00, atk=1.15, defense=0.85, mag=0.85, res=0.95,
        spd=1.30, crit=1.35,
        skills=(("trailcraft", 1), ("strike", 3), ("poisonblade", 5),
                ("flurry", 8), ("frostbite", 11), ("vanish", 14),
                ("thunder", 17), ("execute", 21), ("judgment", 25)),
        signature="steadyaim",
        signature_name="Steady Aim",
        signature_text="If nothing damaged you since your last turn, your "
                       "next hit deals 30% more.",
        start_weapon="Hunting Bow", start_armor="Leather Vest",
        start_consumables=(("potion", 2), ("antidote", 1), ("smoke", 1)),
        favours=('bow', 'dagger'),
        strike="loose"
    ),
    PlayerClass(
        "bloodhowl", "Bloodhowl", "the one who does not stop",
        ("Enormous damage, no patience, and no interest in your potions.",
         "Heals by hitting things, not by drinking."),
        (226, 96, 84),
        archetype="Berserker",
        hp=1.20, mp=0.70, atk=1.45, defense=0.75, mag=0.65, res=0.80,
        spd=1.05, crit=1.20,
        skills=(("strike", 1), ("bloodpact", 3), ("flurry", 6),
                ("riposte", 9), ("drain", 12), ("rally", 15),
                ("execute", 18), ("wither", 22), ("meteor", 26)),
        signature="bloodthirst",
        signature_name="Bloodthirst",
        signature_text="Every hit returns 15% of the damage as health, but "
                       "potions and spells mend you for half.",
        start_weapon="Handaxe", start_armor="Leather Vest",
        start_consumables=(("potion", 2), ("wardstone", 1)),
        favours=('axe', 'hammer'),
        strike="cleave"
    ),
]

# **Five more, all of them starters** (2026-09-04, s52, roadmap item 0w).
# Savvydev, twice - 2026-08-24 and again 2026-09-01:
#
#   "also i think you can offer WAYY more already unlocked classes"
#   "you never added more classes, i think we can add a lot more, no?"
#
# Eight became thirteen and five unlocked became ten. **The three that stay
# locked are the three that were always locked** - Oathbound, Revenant and
# Wayfarer are earned with endings and codex pages, and that is the replay
# spine (s33), not a gate on choice. Everything new is available on the
# first run, because "more classes" that you have to finish the game to see
# is not what he asked for.
#
# Every one recombines skills that already exist and adds one signature
# apiece; none of them needs a sprite, because a hero's look comes from
# `appearance.py` and never from the class.
CLASSES += [
    PlayerClass(
        "hexbinder", "Hexbinder", "the one who leaves a mark",
        ("Weak swings, foul magic, and infinite patience.",
         "Everything hurts more once something is already wrong with you."),
        (176, 200, 96),
        archetype="Warlock",
        hp=0.85, mp=1.40, atk=0.75, defense=0.80, mag=1.40, res=1.10,
        spd=1.00, crit=0.90,
        skills=(("poisonblade", 1), ("wither", 3), ("drain", 5),
                ("firebolt", 7), ("frostbite", 10), ("hollowgrasp", 13),
                ("bloodpact", 16), ("meteor", 20), ("judgment", 25)),
        signature="affliction",
        signature_name="Affliction",
        signature_text="You deal 35% more damage to anything already "
                       "suffering a status effect.",
        start_weapon="Runewand", start_armor="Cloth Robe",
        start_consumables=(("potion", 2), ("ether", 2), ("antidote", 1)),
        favours=('stave', 'dagger'),
        strike="channel"
    ),
    PlayerClass(
        "ironmonk", "Ironmonk", "the one who does not get hit",
        ("Fast, disciplined, and much worse off once you are hurt.",
         "Bulwark in reverse: strongest while nothing has landed."),
        (232, 176, 120),
        archetype="Monk",
        hp=1.00, mp=0.85, atk=1.10, defense=0.92, mag=0.75, res=0.92,
        spd=1.30, crit=1.12,
        skills=(("flurry", 1), ("strike", 3), ("guardup", 5),
                ("riposte", 8), ("rally", 11), ("execute", 14),
                ("drain", 17), ("thunder", 20), ("judgment", 24)),
        signature="momentum",
        signature_name="Momentum",
        signature_text="Above half health you take 18% less damage. Below "
                       "it, you take 20% more.",
        start_weapon="Oak Staff", start_armor="Leather Vest",
        start_consumables=(("potion", 3), ("wardstone", 1)),
        favours=('stave', 'polearm'),
        strike="flurry"
    ),
    PlayerClass(
        "apothecary", "Apothecary", "the one who packed for this",
        ("Middling at everything and never out of anything.",
         "A potion in their hand is worth two in anybody else's."),
        (120, 200, 210),
        archetype="Alchemist",
        hp=0.95, mp=1.15, atk=0.88, defense=0.92, mag=1.12, res=1.05,
        spd=1.05, crit=0.95,
        skills=(("firebolt", 1), ("mend", 3), ("poisonblade", 5),
                ("frostbite", 8), ("sanctuary", 11), ("thunder", 14),
                ("wither", 17), ("drain", 20), ("meteor", 24)),
        signature="decoction",
        signature_name="Decoction",
        signature_text="Potions and elixirs mend you for double, and you "
                       "set out carrying far more of them.",
        start_weapon="Runewand", start_armor="Leather Vest",
        start_consumables=(("potion", 4), ("ether", 3), ("antidote", 2)),
        favours=('stave', 'dagger'),
        strike="hurl"
    ),
    PlayerClass(
        "spellblade", "Spellblade", "the one who does both",
        ("A sword that is also a spell, and a hero who is neither best.",
         "Every ordinary swing carries magic with it."),
        (150, 160, 250),
        archetype="Battlemage",
        hp=1.05, mp=1.20, atk=1.10, defense=1.00, mag=1.20, res=1.05,
        spd=1.05, crit=1.00,
        skills=(("strike", 1), ("firebolt", 3), ("guardup", 6),
                ("frostbite", 9), ("thunder", 12), ("arcanesurge", 15),
                ("execute", 18), ("meteor", 22), ("judgment", 26)),
        signature="runeblade",
        signature_name="Runeblade",
        signature_text="Your weapon attacks also deal magic damage worth "
                       "40% of your magic.",
        start_weapon="Longsword", start_armor="Scale Armor",
        start_consumables=(("potion", 3), ("ether", 2)),
        favours=('blade', 'stave'),
        strike="plain"
    ),
    PlayerClass(
        "duelist", "Duelist", "the one who wants it one to one",
        ("Devastating across a single blade. Surrounded is another story.",
         "Built to win a duel and to lose a brawl."),
        (238, 208, 232),
        archetype="Fencer",
        hp=0.90, mp=0.95, atk=1.10, defense=0.85, mag=0.80, res=0.86,
        spd=1.32, crit=1.20,
        skills=(("riposte", 1), ("strike", 3), ("flurry", 5),
                ("backstab", 8), ("vanish", 11), ("execute", 14),
                ("luckystrike", 17), ("thunder", 20), ("judgment", 24)),
        signature="singlecombat",
        signature_name="Single Combat",
        signature_text="Against one enemy you deal 35% more damage. "
                       "Against a group, 15% less.",
        start_weapon="Rapier", start_armor="Leather Vest",
        start_consumables=(("potion", 2), ("smoke", 2)),
        favours=('blade', 'dagger'),
        strike="pierce"
    ),
]

CLASSES_BY_KEY = {c.key: c for c in CLASSES}
STARTER_CLASSES = [c for c in CLASSES if not c.unlock]
DEFAULT_CLASS = "warden"


def get(key):
    return CLASSES_BY_KEY.get(key, CLASSES_BY_KEY[DEFAULT_CLASS])


def skills_for(class_key, level):
    """Ordered skill keys this class has unlocked by `level`."""
    return [key for key, need in get(class_key).skills if need <= level]


def next_skill(class_key, level):
    """(skill_key, level) of the next unlock, or None if the class is done."""
    for key, need in get(class_key).skills:
        if need > level:
            return key, need
    return None


def newly_unlocked(class_key, old_level, new_level):
    return [key for key, need in get(class_key).skills
            if old_level < need <= new_level]


def is_unlocked(class_key, meta):
    """`meta` is a MetaProfile. Starter classes are always available."""
    cls = get(class_key)
    if not cls.unlock:
        return True
    if cls.unlock.startswith("codex_"):
        return len(meta.codex) >= int(cls.unlock.split("_")[1])
    if cls.unlock == "ending_seventh":
        return bool(meta.endings)          # any ending at all counts
    if cls.unlock.startswith("ending_"):
        return cls.unlock.split("_", 1)[1] in meta.endings
    return False
