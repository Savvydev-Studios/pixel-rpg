"""What a cleared wave offers you, and what a boss does to those odds.

Items 0bs and 0bt. Savvydev, 2026-09-07:

> at the end of each wave in the last stand gamemode, there should be an
> option to pick a random skill out of four and then another option to
> pick a random item (armor, weapon or accessories) each when right
> clicked open a small screen that says what each does

> and another option to gain a buff or a temporary buff for the rest of
> the game, like heal 20% right now(temp buff) or +7% attack (perm buff)
> if that makes sense, the buffs that you get presented are also
> randomized and everytime u beat a boss u have a chance of getting
> better buffs

> and everytime a boss is defeated the drop rates for better items gets
> a bit higher

**Two of the three offers come out of systems that already exist** - a
skill is a `skills.Skill`, an item is whatever `items.roll_item` makes -
and the third does not. Nothing in the game held *"+7% attack for the
rest of this run"*: gear cannot, because a buff is not a thing you take
off, and a `Status` cannot, because statuses tick away inside one fight.
So a **buff** is the new object here and the rest of this module is a
draw.

**No pygame, and no `Game`.** Same rule as `battle.py` and `gamemodes.py`
- what this decides is arithmetic, so it can be tested without a window
and measured by `tools/balance.py` without a second copy of the payout
(§60's lesson, in the direction it was learned).

**The dice are the caller's.** Every draw takes the numbers it needs and
uses the shared `rng`, so a room agrees on nothing here by accident: the
offers a player sees are their own, exactly like the loot a wave pays.
"""
from __future__ import annotations

from dataclasses import dataclass

from .util import clamp, rng

#: How long a buff lasts, in the three lengths he described.
#:
#: *"heal 20% right now(temp buff) or +7% attack (perm buff)"* - the two
#: he named are the ends of this, and `wave` is the middle: something
#: that changes the next fight and then is gone. **The length is shown on
#: the card**, because a card that says "+25% attack" without saying for
#: how long is the one thing this screen must not do.
LASTS = ("now", "wave", "run")
LASTS_LABEL = {"now": "Right now", "wave": "Next wave only",
               "run": "Rest of the run"}

#: What a card can be.
KINDS = ("skill", "item", "buff")

#: How many cards an offer puts in front of you. His number: *"pick a
#: random skill out of four"*.
CARDS = 4


@dataclass(frozen=True)
class Buff:
    """One thing a run can be given that is not gear and not a skill.

    `stat` and `amount` are read by `Player.stat` through
    `stat_multiplier` and `stat_flat`; a buff with neither is one that
    *happens* rather than one that *holds*, and `apply_now` does it.
    """

    key: str
    name: str
    line: str
    lasts: str = "run"
    tier: int = 0
    stat: str = ""
    amount: float = 0.0
    flat: bool = False
    #: **Only in the pool after a warden falls** (item 0ca). Savvydev,
    #: 2026-09-08: *"you shouldnt be able to like get to max hp after
    #: every single wave, only after killing a boss"*. The wave reward
    #: already worked that way - every tenth wave is a warden and every
    #: tenth wave stands everybody up - and this was the hole in it: a
    #: card that put you back to full was in the hand every single wave.
    after_boss: bool = False

    @property
    def permanent(self):
        return self.lasts == "run"


#: **Three tiers, and a boss is what moves you up them** (item 0bt).
#:
#: Tier 0 is what a first wave offers. Tier 2 exists to be the reason to
#: still be playing at wave 60, and it is deliberately not *twice* tier
#: 0 - a run that doubles its attack every ten waves stops being a run.
#:
#: **+7% attack is his**, and it sits at tier 1 because it is the example
#: he gave of a good one rather than of a starting one.
BUFFS = [
    # -- tier 0: the ordinary ones -------------------------------------
    Buff("edge", "Whetted Edge", "+5% attack for the rest of the run.",
         "run", 0, "atk", 0.05),
    Buff("plate", "Set Plate", "+5% defense for the rest of the run.",
         "run", 0, "defense", 0.05),
    Buff("focusrun", "Clear Head", "+5% magic for the rest of the run.",
         "run", 0, "mag", 0.05),
    Buff("quickstep", "Quick Step", "+4% speed for the rest of the run.",
         "run", 0, "spd", 0.04),
    Buff("steady", "Steady Hand", "+2 crit chance for the rest of the run.",
         "run", 0, "crit", 2.0, True),
    Buff("catch", "Catch Your Breath", "Heal 20% of your health now.",
         "now", 0),
    Buff("drink", "A Long Drink", "Restore 35% of your mana now.",
         "now", 0),
    Buff("rally", "Rally", "+25% attack for the next wave only.",
         "wave", 0, "atk", 0.25),
    Buff("dig", "Dig In", "+30% defense for the next wave only.",
         "wave", 0, "defense", 0.30),
    # -- tier 1: what a boss starts to buy you -------------------------
    Buff("sharp", "Sharpened", "+7% attack for the rest of the run.",
         "run", 1, "atk", 0.07),
    Buff("warded", "Warded", "+7% resistance for the rest of the run.",
         "run", 1, "res", 0.07),
    Buff("deep", "Deep Well", "+7% magic for the rest of the run.",
         "run", 1, "mag", 0.07),
    Buff("cruel", "Cruel Streak", "+8 crit damage for the rest of the run.",
         "run", 1, "crit_dmg", 8.0, True),
    Buff("mend", "Mend", "Heal 45% of your health now.", "now", 1),
    Buff("secondwind", "Second Wind",
         "Heal 25% and restore 25% of your mana now.", "now", 1),
    Buff("fury", "Fury", "+40% attack for the next wave only.",
         "wave", 1, "atk", 0.40),
    Buff("haste", "Haste", "+35% speed for the next wave only.",
         "wave", 1, "spd", 0.35),
    # -- tier 2: the ones worth fighting a warden for -------------------
    Buff("keen", "Keen Edge", "+12% attack for the rest of the run.",
         "run", 2, "atk", 0.12),
    Buff("bulwark", "Bulwark", "+12% defense for the rest of the run.",
         "run", 2, "defense", 0.12),
    Buff("wellspring", "Wellspring", "+12% magic for the rest of the run.",
         "run", 2, "mag", 0.12),
    Buff("hunter", "Hunter's Eye", "+5 crit chance for the rest of the run.",
         "run", 2, "crit", 5.0, True),
    Buff("bloodwise", "Bloodwise", "+4 lifesteal for the rest of the run.",
         "run", 2, "lifesteal", 4.0, True),
    Buff("wholeagain", "Whole Again",
         "Back to full health and mana now.", "now", 2, after_boss=True),
    Buff("ironhide", "Ironhide", "+10% health for the rest of the run.",
         "run", 2, "hp", 0.10),
]

BUFFS_BY_KEY = {b.key: b for b in BUFFS}
BUFFS_BY_TIER = {tier: [b for b in BUFFS if b.tier == tier]
                 for tier in (0, 1, 2)}

#: The most tier-1 and tier-2 chances a run can accumulate. **A ceiling,
#: because his sentence has no end in it** - *"everytime u beat a boss u
#: have a chance of getting better buffs"* - and a run at wave 200 that
#: only ever draws tier 2 has stopped offering a choice.
BEST_ODDS = (0.55, 0.30)

#: What one boss is worth, per tier. Ten waves is a boss (§72), so a run
#: reaches the ceiling somewhere past wave 60 rather than at wave 20.
PER_BOSS = (0.09, 0.05)

#: What a boss adds to the loot roll's `luck` (item 0bt). `roll_rarity`
#: takes luck as a straight addition to its bias, where item level
#: contributes `ilvl * 0.045` - so one boss is worth about three levels
#: of item quality, and four bosses about twelve.
LUCK_PER_BOSS = 0.14
LUCK_CEILING = 1.10


def tier_odds(bosses=0):
    """The chance a drawn buff is tier 1 or tier 2, as (greater, best).

    **This is the whole of "better buffs after a boss"** and it is a
    weighting rather than an unlock: tier 2 is reachable from wave one,
    just rarely, because a table that switches on is a table with a
    cliff in it and a player who beats a boss should feel a slope.
    """
    bosses = max(0, int(bosses))
    best = min(BEST_ODDS[1], PER_BOSS[1] * bosses)
    greater = min(BEST_ODDS[0], 0.16 + PER_BOSS[0] * bosses)
    return greater, best


def luck_for(bosses=0, base=0.45):
    """The `luck` a wave's loot is rolled with (item 0bt).

    > and everytime a boss is defeated the drop rates for better items
    > gets a bit higher

    **One number into the existing table**, not a second table. `luck`
    already shifts the whole rarity curve rather than only its tail
    (`items.roll_rarity`), which is exactly what "the drop rates get a
    bit higher" describes - and the alternative, a wave-mode rarity
    table, is the second copy §52's balance pass would have to be run
    against twice.
    """
    return float(base) + min(LUCK_CEILING, LUCK_PER_BOSS * max(0, int(bosses)))


def draw_buffs(count=CARDS, bosses=0, taken=(), boss=False):
    """`count` different buffs to choose between.

    Rolled one at a time so each card gets its own tier - a hand that is
    all tier 0 or all tier 2 reads as a rank rather than a choice.

    `boss` says whether the wave just cleared was a warden's. **The cards
    that put you back to full are only in the hand when it was** (item
    0ca): otherwise every wave ends with a way to top up, and the tenth
    wave's *"everybody back on their feet"* stops being a milestone.
    """
    greater, best = tier_odds(bosses)
    held = set(taken)
    picked, keys = [], set()
    for _ in range(int(count) * 8):
        if len(picked) >= int(count):
            break
        roll = rng.rfloat()
        tier = 2 if roll < best else (1 if roll < best + greater else 0)
        pool = [b for b in BUFFS_BY_TIER[tier]
                if b.key not in keys
                and (boss or not b.after_boss)
                and not (b.permanent and b.key in held)]
        if not pool:
            continue
        buff = rng.choice(pool)
        picked.append(buff)
        keys.add(buff.key)
    return picked


def skill_pool(player):
    """Skills this hero could be given: everything they do not have.

    **A wave hands out skills their class would never unlock**, which is
    the point of the offer - a Cutpurse who draws Firebolt has made a run
    that a Cutpurse cannot make in the story. Their own class ladder
    still runs underneath it, so nothing is taken away.
    """
    from .skills import ALL_SKILLS
    known = {s.key for s in player.known_skills()} if player else set()
    return [s for s in ALL_SKILLS if s.key not in known]


def draw_skills(count=CARDS, player=None):
    """`count` skills the hero does not know, or fewer near the end."""
    pool = skill_pool(player)
    if not pool:
        return []
    count = min(int(count), len(pool))
    return list(rng.sample(pool, count))


def draw_items(count=CARDS, level=1, bosses=0, roll=None):
    """`count` items, **one of each slot as far as the slots go**.

    His words name the slots: *"a random item (armor, weapon or
    accessories)"*. Four cards that all turn out to be helmets is a
    choice between three numbers, so the draw walks the slots first and
    only then rolls freely.
    """
    from .items import roll_item
    roll = roll or (lambda lvl, slot: roll_item(lvl, slot=slot,
                                                luck=luck_for(bosses)))
    slots = ["weapon", "armor", "accessory"]
    rng.shuffle(slots)
    made = []
    for index in range(int(count)):
        slot = slots[index] if index < len(slots) else None
        item = roll(level, slot)
        if item is not None:
            made.append(item)
    return made


@dataclass
class Offer:
    """One question: `kind`, and the cards that answer it."""

    kind: str
    cards: list
    title: str = ""
    line: str = ""

    def __bool__(self):
        return bool(self.cards)


def offers_for(mode, wave, player, bosses=0, roll=None, level=None,
               boss=False):
    """Everything a cleared wave puts in front of one player.

    **This is where the two modes differ** (item 0bu). Savvydev:

    > but i feel like there should be more to distinguish between the two
    > gamemodes other than a "start next wave" option but im not too sure
    > what / maybe skills and loot and buffs would have a different way
    > of being distributed?

    So they do, and along the line he guessed at:

    - **The Last Stand stops, and hands you all three.** It is a
      turn-based mode that is already standing still between waves, and
      it is the mode about *building* something over an hour.
    - **The Long Night hands you one card of four, drawn across all
      three kinds and weighted toward what helps immediately.** A night
      is the mode about *not stopping*, and a night that halts three
      times an hour to walk through three menus is a worse mode wearing
      the same name.

    **Withdrawn 2026-09-08** (item 0bz). He looked at the mixed hand and
    said what he wanted instead:

    > and the boons are seperate from the items, the boons get their own
    > four options

    So every mode offers the same three, and **0bu is open again** - the
    two wave modes are once more distinguished by what they are rather
    than by what they hand out, which is his to answer. The reasoning
    below is kept because it was the argument, not because it won.
    """
    wave = max(1, int(wave))
    level = int(level if level is not None else wave)
    out = []
    skills = draw_skills(CARDS, player)
    if skills:
        out.append(Offer("skill", skills, "Learn one",
                         "Whatever you take, you keep."))
    items = draw_items(CARDS, level, bosses, roll)
    if items:
        out.append(Offer("item", items, "Take one",
                         "The rest are left where they fell."))
    buffs = draw_buffs(CARDS, bosses, _held(player), boss=bool(boss))
    if buffs:
        out.append(Offer("buff", buffs, "Choose a boon",
                         "Some last the run. Some are gone by morning."))
    return out


def _night_hand(wave, player, bosses, roll, level):
    """**No longer used** (item 0bz, withdrawn 2026-09-08).

    The Long Night's single hand: one card, four choices, mixed.

    Weighted toward buffs that land now, because the thing a night is
    short of is time - and against skills, because a skill you have to
    stop and slot is the wrong prize in a mode with no pause.
    """
    hand = []
    buffs = [b for b in draw_buffs(6, bosses, _held(player))
             if b.lasts in ("now", "wave")] or draw_buffs(2, bosses)
    hand.extend(("buff", b) for b in buffs[:2])
    for item in draw_items(1, level, bosses, roll):
        hand.append(("item", item))
    skills = draw_skills(1, player)
    if skills:
        hand.append(("skill", skills[0]))
    while len(hand) < CARDS:
        extra = draw_buffs(1, bosses, _held(player))
        if not extra:
            break
        hand.append(("buff", extra[0]))
    return hand[:CARDS]


def _held(player):
    return {key for key, _ in getattr(player, "buffs", ())}


# ------------------------------------------------------------- holding ---
# A run buff lives on the player as `(key, until_wave)`, where `None`
# means the rest of the run. **A list rather than a dict** because the
# same buff can be taken twice and both should count - a run that stacks
# three Whetted Edges has made a real choice about what it is.


def give(player, buff, wave=0):
    """Hand `buff` to `player`. Returns a line saying what happened.

    `now` buffs are *done* here rather than held; there is nothing to
    hold. Everything else is written down with the wave it dies on.
    """
    if isinstance(buff, str):
        buff = BUFFS_BY_KEY.get(buff)
    if buff is None:
        return ""
    if not hasattr(player, "buffs"):
        player.buffs = []
    if buff.lasts == "now":
        return apply_now(player, buff)
    until = None if buff.lasts == "run" else int(wave) + 1
    player.buffs.append((buff.key, until))
    return buff.line


def apply_now(player, buff):
    """The buffs that are an event rather than a state."""
    healed = mana = 0
    if buff.key in ("catch", "mend", "secondwind", "wholeagain"):
        share = {"catch": 0.20, "mend": 0.45,
                 "secondwind": 0.25, "wholeagain": 1.0}[buff.key]
        want = int(player.max_hp() * share)
        healed = min(want, player.max_hp() - player.hp)
        player.hp += healed
    if buff.key in ("drink", "secondwind", "wholeagain"):
        share = {"drink": 0.35, "secondwind": 0.25, "wholeagain": 1.0}[buff.key]
        want = int(player.max_mp() * share)
        mana = min(want, player.max_mp() - player.mp)
        player.mp += mana
    if healed and mana:
        return "+%d health, +%d mana" % (healed, mana)
    if healed:
        return "+%d health" % healed
    if mana:
        return "+%d mana" % mana
    return "Already whole"


def expire(player, wave):
    """Drop the buffs whose wave has passed. Called when one is cleared."""
    held = getattr(player, "buffs", None)
    if not held:
        return 0
    before = len(held)
    player.buffs = [(key, until) for key, until in held
                    if until is None or int(wave) < int(until)]
    return before - len(player.buffs)


def held_buffs(player):
    """The `Buff` objects a player is carrying, longest-lasting first."""
    out = []
    for key, until in getattr(player, "buffs", ()):
        buff = BUFFS_BY_KEY.get(key)
        if buff is not None:
            out.append((buff, until))
    out.sort(key=lambda pair: (pair[1] is not None, pair[0].name))
    return out


def stat_multiplier(buffs, key):
    """What the held buffs multiply `key` by. 1.0 when none of them do.

    Read by `Player.stat` next to `relics.stat_multiplier`, which is the
    same shape and the reason this one is not a special case.
    """
    total = 1.0
    for held_key, _until in buffs or ():
        buff = BUFFS_BY_KEY.get(held_key)
        if buff is not None and not buff.flat and buff.stat == key:
            total += buff.amount
    return total


def stat_flat(buffs, key):
    """What the held buffs add to `key` outright - crit and lifesteal.

    **Percentage stats take a flat point, never a multiplier.** Five per
    cent of two crit is nothing, and the item tables already treat these
    as points (`items.roll_item` gives them a gentler curve for exactly
    this reason).
    """
    total = 0.0
    for held_key, _until in buffs or ():
        buff = BUFFS_BY_KEY.get(held_key)
        if buff is not None and buff.flat and buff.stat == key:
            total += buff.amount
    return total


# --------------------------------------------------------------- cards ---
# What a card *says*, in one place, because three screens ask: the row
# of cards, the detail panel a right-click opens (his *"a small screen
# that says what each does"*), and the toast that says what you took.


def card_name(kind, card):
    if kind == "buff":
        return card.name
    if kind == "skill":
        return card.name
    return getattr(card, "name", str(card))


def card_lines(kind, card, player=None):
    """The detail panel's body: what this actually does.

    **This is the half of his sentence that makes the choice a choice.**
    Four names is not a decision; four names you can read is.
    """
    if kind == "buff":
        return [card.line, LASTS_LABEL.get(card.lasts, "")]
    if kind == "skill":
        cost = "%d mana" % card.mp if card.mp else "No mana"
        lines = [card.description or "", cost]
        if card.element and card.element != "physical":
            lines.append("%s damage" % card.element.title())
        if card.status:
            lines.append("Can inflict %s" % card.status)
        return [line for line in lines if line]
    lines = []
    rarity = getattr(card, "rarity", "")
    slot = getattr(card, "slot", "")
    if rarity or slot:
        lines.append(" ".join(x for x in (str(rarity).title(),
                                          str(slot).title()) if x))
    for stat, value in sorted(getattr(card, "stats", {}).items()):
        lines.append("%+d %s" % (value, stat))
    if player is not None:
        try:
            worn = player.equipment.get(slot)
            if worn is not None:
                lines.append("You are wearing %s" % worn.name)
        except Exception:
            pass
    return lines


def take(player, kind, card, wave=0):
    """Give the player the card they picked. Returns a line for the toast.

    The three kinds land in three different places and none of them is
    new: a skill joins the ones the class unlocked, an item goes in the
    bag the same way a wave's loot does, and a buff is held here.
    """
    if kind == "buff":
        return give(player, card, wave)
    if kind == "skill":
        if not hasattr(player, "granted"):
            player.granted = []
        if card.key not in player.granted:
            player.granted.append(card.key)
        return "%s learned" % card.name
    if player.add_item(card):
        return "%s taken" % getattr(card, "name", "It")
    return "Your bag is full"


def clamp_tier(value):
    """Kept for callers that want a tier from a number."""
    return int(clamp(int(value), 0, 2))
