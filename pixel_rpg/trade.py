"""Player-to-player trading: the offer, the locks, and the transfer.

**Roadmap item 0x, decided 2026-08-24 and asked about again on
2026-09-01.** Savvydev settled the rules the first time - *"no limits. Any
gold, any gear"* - and then, weeks later: *"and what about trading? was
that ever implemented?"* It had not been. This module is the half that
knows what a trade *is*; `coop.py` moves the messages and `game.py` draws
the screen.

**A trade is a contract, not a transfer.** Nothing moves until both sides
have said yes to the same pair of offers, and **any change to either side
clears both locks** - so the offer you agreed to is always the offer you
were looking at. That rule is the whole safety design; it is why
`Offer.__eq__` exists and why `TradeSession.set_theirs` unlocks.

**Who can be trusted with what.** Each player owns their own bag - the
host has never tracked a client's inventory and cannot start now without
becoming a second save file. So the host arbitrates *agreement* (who is
trading, whether both said yes, when it is done) and each end applies its
own half. What the host can check, and does, is `covers()`: an offer has
to be backed by the last body its owner published, which is refreshed the
moment the screen opens. That catches a desynced client and it does not
pretend to catch a modified one - s16's precedent, and the honest version
of it: a game somebody owns cannot be secured against them, so the effort
goes into making the *accidental* failure impossible.

**What can be traded**: gold, the three gear slots, and consumables.
Decision 10 enumerated *"weapon, armor, accessory"*; potions ride the same
list because *"so a player can hand over what they do not need"* (s40) is
the reason he wanted it, and a friend out of Hi-Potions is the commonest
version of that. Nothing else moves - not relics, not materials, not
skills - because those are a *run's* progress rather than its luggage.
"""
from __future__ import annotations

from .items import CONSUMABLES, Item
from . import security as sec

# A cap on the wire, not a cap on the game. Decision 10 says "no limits",
# and this is the same ceiling the save validator already enforces on a
# bag - so no honest offer can reach it and a hostile one cannot use the
# list to allocate memory.
MAX_ITEMS = sec.MAX_INVENTORY
MAX_GOLD = 99_999_999
MAX_STACK = 99


def _signature(data):
    """A comparable identity for one item, as it appears on the wire.

    Deliberately *not* `uid`: a uid is local to the machine that rolled the
    item and means nothing on the other side of a socket. Two identical
    swords are interchangeable here, which is the right answer - a trade
    that refused to move the second of a matched pair would be a bug.
    """
    if not isinstance(data, dict):
        return None
    stats = tuple(sorted((str(k), int(v))
                         for k, v in sec.safe_dict(data.get("stats")).items()))
    return (str(data.get("name", "")), str(data.get("slot", "")),
            str(data.get("rarity", "")), int(data.get("ilvl", 1) or 1),
            str(data.get("element", "")), str(data.get("base", "")),
            stats, tuple(sorted(str(a) for a in
                                sec.safe_list(data.get("affixes"), 8))))


class Offer:
    """One side's half of a trade: gold, gear and consumables."""

    def __init__(self, gold=0, items=None, cons=None):
        self.gold = int(gold)
        self.items = list(items or [])          # list[Item]
        self.cons = {str(k): int(v)
                     for k, v in (cons or {}).items() if int(v) > 0}

    # -- shape -------------------------------------------------------------

    def empty(self):
        return not self.gold and not self.items and not self.cons

    def count(self):
        return len(self.items) + sum(self.cons.values()) \
            + (1 if self.gold else 0)

    def lines(self):
        """What this offer says, one row per thing, for the screen."""
        out = []
        if self.gold:
            out.append(("%d gold" % self.gold, (250, 216, 128)))
        for item in self.items:
            out.append((item.name, item.color))
        for key, count in sorted(self.cons.items()):
            spec = CONSUMABLES.get(key)
            if spec is None:
                continue
            out.append(("%s x%d" % (spec.name, count), spec.color))
        return out

    def __eq__(self, other):
        if not isinstance(other, Offer):
            return NotImplemented
        return (self.gold == other.gold and self.cons == other.cons
                and sorted(_signature(i.to_dict()) for i in self.items)
                == sorted(_signature(i.to_dict()) for i in other.items))

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    # -- the wire ----------------------------------------------------------

    def to_wire(self):
        return {"g": int(self.gold),
                "i": [item.to_dict() for item in self.items],
                "c": dict(self.cons)}

    @classmethod
    def from_wire(cls, data):
        """Rebuild an offer from a peer. Everything here is hostile input.

        An item that will not survive `Item.from_dict` is dropped rather
        than defaulted, because a defaulted item is loot somebody did not
        offer - and a consumable key that is not in the game is dropped for
        the same reason.
        """
        raw = sec.safe_dict(data)
        items = []
        for entry in sec.safe_list(raw.get("i"), MAX_ITEMS):
            item = Item.from_dict(entry) if isinstance(entry, dict) else None
            if item is not None:
                items.append(item)
        cons = {}
        for key, value in sec.safe_dict(raw.get("c")).items():
            if key in CONSUMABLES:
                count = sec.safe_int(value, 0, MAX_STACK, 0)
                if count:
                    cons[key] = count
        return cls(sec.safe_int(raw.get("g"), 0, MAX_GOLD, 0), items, cons)


def holdings(body):
    """What a published combat body says its owner is carrying.

    Returns (gold, {signature: count}, {consumable: count}). Equipped gear
    counts: decision 10 says any gear, and a sword you are holding is the
    one somebody actually wants.
    """
    raw = sec.safe_dict(body)
    gold = sec.safe_int(raw.get("gold"), 0, MAX_GOLD, 0)
    counts = {}
    for entry in sec.safe_list(raw.get("inventory"), MAX_ITEMS):
        key = _signature(entry)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    for entry in sec.safe_dict(raw.get("equipment")).values():
        key = _signature(entry)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    cons = {k: sec.safe_int(v, 0, MAX_STACK, 0)
            for k, v in sec.safe_dict(raw.get("consumables")).items()}
    return gold, counts, cons


def covers(body, offer):
    """Host side: is this offer backed by what its owner last published?

    Not a cheat check so much as a desync check - see the module docstring
    on which of those is possible. A body arrives on joining and is
    republished when the trade screen opens, so during a trade it is
    current by construction; if it is not, refusing is the safe answer
    because the alternative is an item arriving that never left.
    """
    if not isinstance(body, dict):
        # Nothing published yet. Only an empty offer is provably honest.
        return offer.empty()
    gold, counts, cons = holdings(body)
    if offer.gold > gold:
        return False
    for key, count in offer.cons.items():
        if count > cons.get(key, 0):
            return False
    wanted = {}
    for item in offer.items:
        key = _signature(item.to_dict())
        wanted[key] = wanted.get(key, 0) + 1
    for key, count in wanted.items():
        if count > counts.get(key, 0):
            return False
    return True


def room_for(player, mine, theirs):
    """Will the bag hold what is coming back? (ok, one-line reason).

    Counted rather than tried, because the answer has to be known *before*
    either side locks. Gear given away leaves the bag or a worn slot; gear
    received always arrives in the bag.
    """
    if player is None:
        return False, "no hero"
    if mine.gold > player.gold:
        return False, "you do not have that much gold"
    for key, count in mine.cons.items():
        if count > player.consumable_count(key):
            return False, "you no longer have those"
    freed = sum(1 for item in mine.items if item in player.inventory)
    after = len(player.inventory) - freed + len(theirs.items)
    if after > player.bag_size():
        return False, "your bag has no room for that"
    return True, ""


def apply_to(player, mine, theirs):
    """Move it. Everything leaves before anything arrives.

    That order matters: a trade that hands over four pieces for four
    should not fail on a bag that is momentarily eight over, and
    `room_for` has already been asked whether the *result* fits.

    Returns the items that would not fit after all, which should be
    nothing - the caller offers them to the full-bag question rather than
    dropping them, because s48's rule is that the game never throws away
    something you would have kept.
    """
    player.gold = max(0, player.gold - mine.gold)
    for key, count in mine.cons.items():
        player.consumables[key] = max(0, player.consumable_count(key) - count)
        if not player.consumables[key]:
            player.consumables.pop(key, None)
    for item in mine.items:
        if not player.remove_item(item):
            # It was worn rather than carried. The slot empties; nothing is
            # put back, because what would go back is the thing being given.
            for slot, worn in player.equipment.items():
                if worn is item:
                    player.equipment[slot] = None
                    break
    player.gold = min(MAX_GOLD, player.gold + theirs.gold)
    for key, count in theirs.cons.items():
        player.add_consumable(key, count)
    spare = []
    for item in theirs.items:
        if not player.add_item(item):
            spare.append(item)
    player.hp = min(player.hp, player.max_hp())
    player.mp = min(player.mp, player.max_mp())
    return spare


# States a local trade can be in. `INVITED` is the only one that draws a
# question rather than the screen.
INVITING = "inviting"     # we asked; waiting for them
INVITED = "invited"       # they asked; we have not answered
OPEN = "open"             # both in, offers live
DONE = "done"             # applied, showing what changed


class TradeSession:
    """One trade, as this machine sees it.

    Both ends keep one of these and they are deliberately *not* mirrors:
    `mine` is authoritative here and arrives as `theirs` over there.
    """

    def __init__(self, partner_id, partner_name, state=OPEN):
        self.partner_id = int(partner_id)
        self.partner_name = str(partner_name)[:18] or "Wanderer"
        self.state = state
        self.mine = Offer()
        self.theirs = Offer()
        self.mine_locked = False
        self.theirs_locked = False
        self.note = ""
        # Cursor state for the screen, kept here so closing the trade takes
        # it with it.
        self.index = 0
        self.received = None      # what we got, once it is DONE

    @property
    def both_locked(self):
        return self.mine_locked and self.theirs_locked

    def set_mine(self, offer):
        """Change our side. **Both locks drop.**"""
        if offer == self.mine:
            return False
        self.mine = offer
        self.mine_locked = False
        self.theirs_locked = False
        return True

    def set_theirs(self, offer):
        """Their side changed. Both locks drop here too.

        The second half is the one that matters: an offer that changed
        after you agreed to it is not the offer you agreed to, and a lock
        that survived would let somebody swap a legendary for a stick
        between your press and the host's answer.
        """
        changed = offer != self.theirs
        self.theirs = offer
        if changed:
            self.mine_locked = False
            self.theirs_locked = False
        return changed
