"""Items, affixes and loot generation.

The original game had a fixed list of twelve gear pieces. This version rolls
items from base types x rarity x affixes, so an Epic Greatsword of the Titan is
a thing that can exist without anyone having typed it out.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from . import security as sec
from .util import MAX_LEVEL, clamp, rng

# Stat keys an item may carry. Anything outside this set is dropped on load.
STAT_KEYS = ("atk", "defense", "hp", "mp", "mag", "res", "spd", "crit",
             "crit_dmg", "lifesteal")

ELEMENTS = ("physical", "fire", "ice", "thunder", "shadow", "holy")

ELEMENT_COLOR = {
    "physical": (206, 206, 216),
    "fire": (250, 128, 52),
    "ice": (130, 214, 240),
    "thunder": (250, 220, 90),
    "shadow": (168, 108, 226),
    "holy": (252, 240, 180),
}

# Rock-paper-scissors-ish: attacking with the key element against the listed
# element deals bonus damage.
ELEMENT_STRONG_VS = {
    "fire": "ice", "ice": "thunder", "thunder": "shadow",
    "shadow": "holy", "holy": "shadow",
}


@dataclass(frozen=True)
class Rarity:
    key: str
    label: str
    color: tuple
    mult: float          # stat multiplier
    affixes: int         # how many affixes roll
    weight: float        # relative drop weight
    price_mult: float


RARITIES = {
    r.key: r for r in (
        Rarity("common",    "Common",    (206, 206, 214), 1.00, 0, 46.0, 1.0),
        Rarity("uncommon",  "Uncommon",  (118, 220, 138), 1.22, 1, 27.0, 1.6),
        Rarity("rare",      "Rare",      (108, 164, 252), 1.50, 2, 15.0, 2.8),
        Rarity("epic",      "Epic",      (192, 118, 250), 1.90, 3,  8.0, 5.2),
        Rarity("legendary", "Legendary", (252, 176, 72),  2.45, 4,  3.4, 9.0),
        Rarity("mythic",    "Mythic",    (252, 104, 132), 3.20, 5,  0.6, 16.0),
    )
}
RARITY_ORDER = ["common", "uncommon", "rare", "epic", "legendary", "mythic"]


# **Weapons come in families** (2026-09-01, s47). A family is what the
# thing *is* - a blade, a bow, a stave - as opposed to what it rolls, and
# it is the hook a class hangs an affinity on:
#
# > "why would an assassin use a bow yk? give like different weapon types
# > special buffs that are useful when used with the correct class"
#
# Armour and accessories have none. They are not what anybody trains with.
FAMILIES = ("dagger", "blade", "axe", "hammer", "polearm", "bow", "stave")

FAMILY_LABEL = {
    "dagger": "Dagger", "blade": "Blade", "axe": "Axe", "hammer": "Hammer",
    "polearm": "Polearm", "bow": "Bow", "stave": "Stave",
}


@dataclass(frozen=True)
class BaseItem:
    name: str
    slot: str            # weapon | armor | accessory
    tier: int            # 1..6, gates which bases appear at which item level
    weights: dict        # stat -> per-item-level contribution
    element: str = "physical"
    flavour: str = ""
    family: str = ""     # weapons only; see FAMILIES


WEAPON_BASES = [
    BaseItem("Dagger",     "weapon", 1, {"atk": 0.8, "spd": 0.9, "crit": 1.1}),
    BaseItem("Shortsword", "weapon", 1, {"atk": 1.0, "spd": 0.4}),
    BaseItem("Handaxe",    "weapon", 2, {"atk": 1.2, "crit": 0.5}),
    BaseItem("Longsword",  "weapon", 2, {"atk": 1.3, "defense": 0.2}),
    BaseItem("Hunting Bow", "weapon", 2, {"atk": 1.1, "spd": 0.8, "crit": 0.9}),
    BaseItem("Rapier",     "weapon", 3, {"atk": 1.1, "spd": 1.2, "crit": 1.4}),
    BaseItem("Warhammer",  "weapon", 3, {"atk": 1.7, "spd": -0.5}),
    BaseItem("Greatsword", "weapon", 4, {"atk": 1.9, "spd": -0.4, "hp": 0.6}),
    BaseItem("Scythe",     "weapon", 4, {"atk": 1.6, "crit": 0.9,
                                         "lifesteal": 0.4}, "shadow"),
    BaseItem("Oak Staff",  "weapon", 2, {"atk": 0.4, "mag": 1.6, "mp": 1.2}),
    BaseItem("Runewand",   "weapon", 3, {"atk": 0.3, "mag": 2.0, "mp": 1.6}),
    BaseItem("Emberbrand", "weapon", 5, {"atk": 1.8, "mag": 0.8}, "fire"),
    BaseItem("Frostfang",  "weapon", 5, {"atk": 1.7, "spd": 0.6}, "ice"),
    BaseItem("Stormpike",  "weapon", 6, {"atk": 2.0, "crit": 0.8}, "thunder"),
    BaseItem("Dawnblade",  "weapon", 6, {"atk": 2.1, "hp": 0.8}, "holy"),
]

# Assigned here rather than in each constructor so the mapping is one
# readable table - and `TestWeaponFamilies` fails if a weapon is missing
# from it, which is what stops a new base being silently family-less.
_WEAPON_FAMILY = {
    "Dagger": "dagger", "Shortsword": "blade", "Handaxe": "axe",
    "Longsword": "blade", "Hunting Bow": "bow", "Rapier": "blade",
    "Warhammer": "hammer", "Greatsword": "blade", "Scythe": "polearm",
    "Oak Staff": "stave", "Runewand": "stave", "Emberbrand": "blade",
    "Frostfang": "blade", "Stormpike": "polearm", "Dawnblade": "blade",
}
def weapon_heft(item):
    """How heavy a weapon swings, 0 (a dagger) to 1 (a greatsword).

    **Read off what the weapon already is** rather than a new field: a
    heavy weapon is one that gives a lot of attack and takes speed away,
    which is exactly how the bases are written. A Dagger is 0.8 attack and
    +0.9 speed; a Greatsword is 1.9 and -0.4. Nothing had to be added to
    the table, and a new base gets a sensible answer for free.
    """
    # **`Item.base` is the base's *name*, not the base.** Reading
    # `.weights` off it silently gives nothing, and "nothing" here is a
    # perfectly plausible 0.15 - which is how the first version reported
    # a greatsword and a dagger as equally heavy.
    base = getattr(item, "base", None) or item
    if isinstance(base, str):
        base = BASES_BY_NAME.get(base) or item
    weights = getattr(base, "weights", None) or {}
    swing = float(weights.get("atk", 1.0))
    quick = float(weights.get("spd", 0.0))
    return max(0.0, min(1.0, (swing - 0.8) / 1.3 - quick * 0.35))


WEAPON_BASES = [
    BaseItem(b.name, b.slot, b.tier, b.weights, b.element, b.flavour,
             _WEAPON_FAMILY.get(b.name, ""))
    for b in WEAPON_BASES
]


ARMOR_BASES = [
    BaseItem("Cloth Robe",   "armor", 1, {"defense": 0.6, "mp": 1.2, "mag": 0.5}),
    BaseItem("Leather Vest", "armor", 1, {"defense": 1.0, "spd": 0.4, "hp": 1.0}),
    BaseItem("Chain Mail",   "armor", 2, {"defense": 1.5, "hp": 1.4}),
    BaseItem("Scale Armor",  "armor", 3, {"defense": 1.8, "hp": 1.8, "res": 0.6}),
    BaseItem("Plate Armor",  "armor", 4, {"defense": 2.4, "hp": 2.2, "spd": -0.4}),
    BaseItem("Mage Weave",   "armor", 4, {"defense": 1.2, "mp": 2.4, "mag": 1.4}),
    BaseItem("Dragonhide",   "armor", 5, {"defense": 2.6, "hp": 2.6, "res": 1.2},
             "fire"),
    BaseItem("Aegis Plate",  "armor", 6, {"defense": 3.2, "hp": 3.0, "res": 1.6}),
]

ACCESSORY_BASES = [
    BaseItem("Copper Ring", "accessory", 1, {"hp": 0.8, "mp": 0.6}),
    BaseItem("Silver Band", "accessory", 2, {"crit": 1.0, "spd": 0.5}),
    BaseItem("Jade Charm",  "accessory", 2, {"res": 1.2, "mp": 1.0}),
    BaseItem("Wolf Fang",   "accessory", 3, {"atk": 0.7, "crit": 1.2}),
    BaseItem("Sun Amulet",  "accessory", 4, {"hp": 1.6, "res": 1.4}, "holy"),
    BaseItem("Void Sigil",  "accessory", 5, {"mag": 1.8, "lifesteal": 0.6},
             "shadow"),
    BaseItem("Titan Seal",  "accessory", 6, {"atk": 1.2, "defense": 1.2,
                                             "hp": 2.0}),
]

ALL_BASES = WEAPON_BASES + ARMOR_BASES + ACCESSORY_BASES
BASES_BY_NAME = {b.name: b for b in ALL_BASES}


def family_of(item):
    """The weapon family of an item, or "" for anything that has none.

    **`Item.base` is the base's *name*, not the base**, which is the sort
    of thing that reads fine and silently returns nothing: the first
    version of the affinity asked `item.base.family` and got `""` for
    every weapon in the game, so the bonus never applied and no test
    noticed because "" is a perfectly good answer for armour.
    """
    if item is None:
        return ""
    base = BASES_BY_NAME.get(getattr(item, "base", "") or "")
    return getattr(base, "family", "") or ""


@dataclass(frozen=True)
class Affix:
    name: str
    kind: str            # prefix | suffix
    stat: str
    lo: float            # per-item-level contribution range
    hi: float
    element: str = ""


PREFIXES = [
    Affix("Sharp",     "prefix", "atk", 0.25, 0.60),
    Affix("Brutal",    "prefix", "atk", 0.45, 0.90),
    Affix("Sturdy",    "prefix", "defense", 0.30, 0.70),
    Affix("Bulwark",   "prefix", "defense", 0.55, 1.00),
    Affix("Vital",     "prefix", "hp", 0.80, 1.80),
    Affix("Arcane",    "prefix", "mag", 0.35, 0.80),
    Affix("Swift",     "prefix", "spd", 0.30, 0.70),
    Affix("Keen",      "prefix", "crit", 0.50, 1.20),
    Affix("Warded",    "prefix", "res", 0.35, 0.85),
    Affix("Vampiric",  "prefix", "lifesteal", 0.20, 0.50),
    Affix("Blazing",   "prefix", "atk", 0.30, 0.70, "fire"),
    Affix("Frozen",    "prefix", "atk", 0.30, 0.70, "ice"),
    Affix("Storming",  "prefix", "atk", 0.30, 0.70, "thunder"),
    Affix("Shadowed",  "prefix", "atk", 0.30, 0.70, "shadow"),
    Affix("Blessed",   "prefix", "atk", 0.30, 0.70, "holy"),
]

SUFFIXES = [
    Affix("of the Bear",   "suffix", "hp", 1.00, 2.20),
    Affix("of the Fox",    "suffix", "spd", 0.35, 0.80),
    Affix("of the Owl",    "suffix", "mp", 0.80, 1.80),
    Affix("of the Bull",   "suffix", "atk", 0.30, 0.70),
    Affix("of the Titan",  "suffix", "defense", 0.45, 0.95),
    Affix("of Warding",    "suffix", "res", 0.40, 0.90),
    Affix("of Ruin",       "suffix", "crit_dmg", 1.20, 3.00),
    Affix("of the Serpent", "suffix", "lifesteal", 0.25, 0.55),
    Affix("of Insight",    "suffix", "mag", 0.40, 0.90),
    Affix("of Fortune",    "suffix", "crit", 0.40, 1.00),
]

AFFIX_BY_NAME = {a.name: a for a in PREFIXES + SUFFIXES}

_uid_counter = itertools.count(1)


@dataclass
class Item:
    """A concrete inventory item.

    `stats` only ever holds keys from STAT_KEYS, enforced in `sanitized`, so a
    doctored save cannot inject an arbitrary attribute into combat maths.
    """
    name: str
    slot: str
    rarity: str = "common"
    ilvl: int = 1
    element: str = "physical"
    base: str = ""
    stats: dict = field(default_factory=dict)
    affixes: list = field(default_factory=list)
    price: int = 10
    uid: int = field(default_factory=lambda: next(_uid_counter))

    # -- presentation ------------------------------------------------------

    @property
    def rarity_info(self):
        return RARITIES.get(self.rarity, RARITIES["common"])

    @property
    def color(self):
        return self.rarity_info.color

    def stat_line(self):
        parts = []
        for key in STAT_KEYS:
            value = self.stats.get(key, 0)
            if not value:
                continue
            if key in ("crit", "lifesteal", "crit_dmg"):
                parts.append("%s +%d%%" % (SHORT[key], value))
            else:
                parts.append("%s +%d" % (SHORT[key], value))
        return "  ".join(parts) or "no bonuses"

    def power(self):
        """Rough single number for shop sorting and upgrade hints."""
        weights = {"atk": 3.0, "defense": 2.6, "hp": 0.8, "mp": 0.5, "mag": 2.6,
                   "res": 2.0, "spd": 1.8, "crit": 1.4, "crit_dmg": 0.5,
                   "lifesteal": 2.0}
        return sum(self.stats.get(k, 0) * w for k, w in weights.items())

    # -- persistence -------------------------------------------------------

    ALLOWED_KEYS = {"name", "slot", "rarity", "ilvl", "element", "base",
                    "stats", "affixes", "price"}

    def to_dict(self):
        return {"name": self.name, "slot": self.slot, "rarity": self.rarity,
                "ilvl": self.ilvl, "element": self.element, "base": self.base,
                "stats": dict(self.stats), "affixes": list(self.affixes),
                "price": self.price}

    @classmethod
    def from_dict(cls, data):
        """Build from untrusted JSON.

        Replaces the original `Gear(**d)`: unknown keys are dropped, every
        value is coerced into range, and stat keys are checked against an
        allow-list. Returns None if the record is unusable.
        """
        raw = sec.pick_known_keys(data, cls.ALLOWED_KEYS)
        if not raw:
            return None
        name = sec.safe_str(raw.get("name"), 48)
        slot = sec.safe_choice(raw.get("slot"),
                               ("weapon", "armor", "accessory"), "")
        if not name or not slot:
            return None
        stats = {}
        for key, value in sec.safe_dict(raw.get("stats")).items():
            if key in STAT_KEYS:
                stats[key] = sec.safe_int(value, -999, 9999, 0)
        affixes = [sec.safe_str(a, 32) for a in sec.safe_list(raw.get("affixes"), 8)]
        return cls(
            name=name,
            slot=slot,
            rarity=sec.safe_choice(raw.get("rarity"), tuple(RARITIES), "common"),
            ilvl=sec.safe_int(raw.get("ilvl"), 1, 99, 1),
            element=sec.safe_choice(raw.get("element"), ELEMENTS, "physical"),
            base=sec.safe_str(raw.get("base"), 32),
            stats=stats,
            affixes=[a for a in affixes if a in AFFIX_BY_NAME],
            price=sec.safe_int(raw.get("price"), 0, 999999, 10),
        )


SHORT = {"atk": "ATK", "defense": "DEF", "hp": "HP", "mp": "MP", "mag": "MAG",
         "res": "RES", "spd": "SPD", "crit": "CRIT", "crit_dmg": "CDMG",
         "lifesteal": "LIFE"}


# ------------------------------------------------------------- generation ---

def roll_rarity(ilvl=1, luck=0.0):
    """Weighted rarity pick, biased upward by item level and luck.

    Weights are recomputed per roll rather than baked into a table so that a
    high-luck character genuinely shifts the whole curve, not just the tail.
    """
    bias = clamp(ilvl * 0.045 + luck, 0.0, 3.0)
    weights = []
    for idx, key in enumerate(RARITY_ORDER):
        weight = RARITIES[key].weight
        # Each step up the ladder gains more from bias than the step below.
        weights.append(max(0.01, weight * (1.0 + bias * idx * 0.55)
                           / (1.0 + bias * 0.35)))
    return rng.choices(RARITY_ORDER, weights=weights, k=1)[0]


def eligible_bases(slot=None, ilvl=1):
    max_tier = clamp(1 + ilvl // 4, 1, 6)
    pool = [b for b in ALL_BASES
            if (slot is None or b.slot == slot) and b.tier <= max_tier]
    return pool or [b for b in ALL_BASES if slot is None or b.slot == slot]


def roll_item(ilvl=1, slot=None, rarity=None, luck=0.0, base=None,
              downgrade=0):
    """Generate one item. This is the single entry point for all loot.

    `downgrade` is Brittle Steel (s54): *"all gear drops one rarity
    lower"*. **The modifier existed for weeks and did nothing** - its
    `downgrade` tag was declared in `modifiers.py` and read by no code
    anywhere, so a run paid a 1.40 score multiplier for a cost that was
    never applied. That is the Blind Road bug the top of `modifiers.py`
    is about, still live in the file that documents it, and it was found
    by a test asserting every tag has a reader rather than by anybody
    playing.

    Applied after the roll rather than by biasing it, so luck and level
    still decide the roll and the modifier takes a step off whatever came
    out - which is what "drops one rarity lower" says.
    """
    ilvl = int(clamp(ilvl, 1, MAX_LEVEL))
    base = base or rng.choice(eligible_bases(slot, ilvl))
    rarity = rarity if rarity in RARITIES else roll_rarity(ilvl, luck)
    if downgrade:
        index = RARITY_ORDER.index(rarity) if rarity in RARITY_ORDER else 0
        rarity = RARITY_ORDER[max(0, index - int(downgrade))]
    info = RARITIES[rarity]

    stats = {}
    for stat, weight in base.weights.items():
        raw = weight * ilvl * info.mult * rng.rfloat(0.85, 1.15)
        if stat in ("crit", "lifesteal", "crit_dmg"):
            raw *= 0.55       # percentages need a gentler curve
        value = int(round(raw))
        if value or weight < 0:
            stats[stat] = value

    prefix_name = suffix_name = ""
    chosen = []
    slots_left = info.affixes
    element = base.element

    if slots_left and rng.chance(0.72):
        affix = rng.choice(PREFIXES)
        chosen.append(affix)
        prefix_name = affix.name
        slots_left -= 1
        if affix.element:
            element = affix.element
    if slots_left and rng.chance(0.72):
        affix = rng.choice(SUFFIXES)
        chosen.append(affix)
        suffix_name = affix.name
        slots_left -= 1
    # Remaining budget rolls as hidden bonus stats (no extra name segments).
    for _ in range(slots_left):
        chosen.append(rng.choice(PREFIXES + SUFFIXES))

    for affix in chosen:
        gain = int(round(rng.rfloat(affix.lo, affix.hi) * ilvl
                         * (0.55 if affix.stat in ("crit", "lifesteal",
                                                   "crit_dmg") else 1.0)))
        stats[affix.stat] = stats.get(affix.stat, 0) + max(1, gain)

    name = " ".join(p for p in (prefix_name, base.name, suffix_name) if p)
    price = int(max(5, (ilvl * 9 + sum(abs(v) for v in stats.values()) * 2)
                    * info.price_mult))

    return Item(name=name, slot=base.slot, rarity=rarity, ilvl=ilvl,
                element=element, base=base.name, stats=stats,
                affixes=[a.name for a in chosen], price=price)


def sell_price(item):
    """Vendors pay a fraction, better for rarer goods."""
    fraction = 0.42 + RARITY_ORDER.index(item.rarity) * 0.035
    return max(1, int(item.price * fraction))


SALVAGE_NAME = "Ember Steel"


def salvage_yield(item):
    """How much Ember Steel breaking this item down gives back.

    Rarer and higher-level gear yields more, so the endless stream of loot
    you would otherwise sell for coppers becomes the fuel for improving the
    piece you actually want to keep.
    """
    tier = RARITY_ORDER.index(item.rarity)
    return max(1, int(1 + tier * 2 + item.ilvl // 6))


def upgrade_cost(item, level):
    """Gold cost to push an item one refinement step further."""
    base = item.price * 0.30 + item.ilvl * 10
    return int(base * (1.0 + level * 0.75)) + 20


def upgrade_materials(item, level):
    """Ember Steel needed for the next refinement step."""
    tier = RARITY_ORDER.index(item.rarity)
    return int(3 + tier + level * (2 + tier * 0.5))


def apply_upgrade(item):
    """+1 refinement: bumps every existing stat and renames the item."""
    for key in list(item.stats):
        if item.stats[key] > 0:
            item.stats[key] = int(item.stats[key] * 1.12) + 1
    plus = 1
    if item.name.endswith(tuple("0123456789")) and " +" in item.name:
        head, _, tail = item.name.rpartition(" +")
        plus = sec.safe_int(tail, 1, 20, 1) + 1
        item.name = head
    item.name = "%s +%d" % (item.name, plus)
    item.price = int(item.price * 1.25) + 20
    return item


# ------------------------------------------------------------ consumables ---

@dataclass(frozen=True)
class Consumable:
    key: str
    name: str
    price: int
    effect: str          # heal | mana | cure | revive | buff | damage | escape
    power: int = 0
    description: str = ""
    color: tuple = (220, 220, 230)
    battle_only: bool = False


CONSUMABLES = {
    c.key: c for c in (
        Consumable("potion", "Potion", 30, "heal", 45,
                   "Restores 45 HP.", (226, 96, 106)),
        Consumable("hipotion", "Hi-Potion", 90, "heal", 140,
                   "Restores 140 HP.", (246, 128, 138)),
        Consumable("elixir", "Elixir", 420, "full", 0,
                   "Fully restores HP and MP.", (250, 216, 128)),
        Consumable("ether", "Ether", 70, "mana", 40,
                   "Restores 40 MP.", (120, 160, 250)),
        Consumable("antidote", "Antidote", 25, "cure", 0,
                   "Clears poison, burn and bleed.", (140, 220, 140)),
        Consumable("phoenix", "Phoenix Down", 260, "revive", 0,
                   "Survive one fatal blow this battle.", (250, 180, 90)),
        Consumable("bomb", "Fire Bomb", 60, "damage", 70,
                   "Hurls 70 fire damage at the foe.", (250, 128, 52), True),
        Consumable("smoke", "Smoke Bomb", 45, "escape", 0,
                   "Guarantees escape from a normal fight.", (170, 170, 180),
                   True),
        Consumable("whetstone", "Whetstone", 80, "buff", 0,
                   "+30% attack for three turns.", (206, 206, 216), True),
        Consumable("wardstone", "Wardstone", 80, "guard", 0,
                   "+40% defense for three turns.", (130, 200, 230), True),
    )
}

CONSUMABLE_ORDER = ["potion", "hipotion", "ether", "elixir", "antidote",
                    "phoenix", "bomb", "smoke", "whetstone", "wardstone"]


def shop_stock(progress, ilvl, count=8, downgrade=0):
    """Rotating stock. Progress (bosses beaten) gates the good shelves.

    Returns (consumable_keys, items).
    """
    keys = ["potion", "antidote"]
    if progress >= 1:
        keys += ["ether", "smoke"]
    if progress >= 2:
        keys += ["hipotion", "bomb"]
    if progress >= 3:
        keys += ["whetstone", "wardstone"]
    if progress >= 4:
        keys += ["phoenix"]
    if progress >= 5:
        keys += ["elixir"]

    items = []
    for _ in range(count):
        # Shops skew toward the middle of the rarity curve: good, not free.
        rarity = roll_rarity(ilvl, luck=0.15 + progress * 0.08)
        if rarity in ("legendary", "mythic") and not rng.chance(0.30):
            rarity = "epic"
        item = roll_item(max(1, ilvl + rng.rint(-1, 2)), rarity=rarity,
                         downgrade=downgrade)
        item.price = int(item.price * 1.35)   # retail markup
        items.append(item)
    items.sort(key=lambda i: RARITY_ORDER.index(i.rarity))
    return keys, items


def starter_kit():
    """Deterministic opening gear so run one is never a coin flip."""
    weapon = roll_item(1, rarity="common", base=BASES_BY_NAME["Shortsword"])
    weapon.name = "Worn Shortsword"
    armor = roll_item(1, rarity="common", base=BASES_BY_NAME["Leather Vest"])
    armor.name = "Traveller Vest"
    return weapon, armor
