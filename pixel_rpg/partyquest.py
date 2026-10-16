"""One quest between two people (item 0cj).

His, 2026-08-24: *"one quest between two people."* A co-op party in a story
room works one of Maren's quests together, and the one-at-a-time cap does
not change. The details his sentence predates were settled with these
defaults (handoff s96, 0cj), each of them his to overrule:

* **The party's progress.** A kill by anybody in the room counts - the
  fight was shared anyway (s21). Progress that was each player's own would
  let two people on one quest finish it at different times, which is two
  quests with one name.
* **The whole reward each.** A quest is a job Maren gave the party;
  splitting it would make co-op the worse way to do her work. XP *from
  fights* still splits - that rule is about the fight. A hero who already
  finished the quest in their own log is not paid for it twice.
* **Only Maren's quest.** Errands are several at once and personal (s42),
  and sharing six of them is a different feature.

**Host-arbitrated, like a chest** (s20). The host's log *is* the party's
quest: a guest's accept or hand-in is a request, and the host decides. This
module is the arithmetic the host does, kept apart from the game and the
socket so it can be tested on its own. It reads a guest through the same
body (`save.serialize_player`) every other room feature already trusts,
and it treats it the same way: as untrusted numbers, clamped.
"""
from __future__ import annotations

from . import security as sec
from .quests import QUESTS_BY_KEY

# A counter no honest run gets near, so a body that claims more is
# clamped rather than believed.
_CEILING = 10_000_000


def metric_of(body, metric):
    """One counter out of a published body - `level`, or a tracker key."""
    body = body if isinstance(body, dict) else {}
    if metric == "level":
        return sec.safe_int(body.get("level"), 0, 999, 0)
    tracker = body.get("tracker")
    tracker = tracker if isinstance(tracker, dict) else {}
    return sec.safe_int(tracker.get(metric), 0, _CEILING, 0)


class PartyQuest:
    """The host's view of the quest a room is working on together.

    `start` is each guest's counter when they began contributing - when the
    quest was taken, or when they walked in on it - and `last` is the most
    recent value seen, so a guest who leaves keeps what they did. Nothing
    here is saved: a host that reloads starts the party's count again from
    its own progress, which loses a guest's share and never invents one.
    """

    def __init__(self):
        self.reset()

    def reset(self, key=""):
        self.key = key if key in QUESTS_BY_KEY else ""
        self.start = {}
        self.last = {}

    def follow(self, key, bodies):
        """Keep up with the quest the host is carrying. Returns True when
        it changed."""
        key = key if key in QUESTS_BY_KEY else ""
        if key == self.key:
            self.see(bodies)
            return False
        self.reset(key)
        self.see(bodies)
        return True

    def see(self, bodies):
        """Note every guest's counter: a newcomer starts from now."""
        quest = QUESTS_BY_KEY.get(self.key)
        if quest is None:
            return
        for player_id, body in (bodies or {}).items():
            value = metric_of(body, quest.metric)
            self.start.setdefault(player_id, value)
            self.last[player_id] = value

    def progress(self, own_value, own_baseline=0):
        """(done, target) for the party.

        `own_value` is the host's counter now, `own_baseline` what it was
        when the host took the quest on. A counter goal adds everybody's
        share; an absolute one (reach level 20) is whoever is furthest.
        """
        quest = QUESTS_BY_KEY.get(self.key)
        if quest is None:
            return 0, 1
        if quest.absolute:
            best = max([int(own_value)] + list(self.last.values()))
            return min(max(0, best), quest.target), quest.target
        done = max(0, int(own_value) - int(own_baseline))
        for player_id, start in self.start.items():
            done += max(0, self.last.get(player_id, start) - start)
        return min(done, quest.target), quest.target
