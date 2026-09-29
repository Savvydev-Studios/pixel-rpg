"""Co-op sessions: the handshake, the roster, and who is standing where.

`net.py` moves dictionaries and knows nothing about the game. This module is
the part that knows what they mean, and it sits between the transport and
`Game` so that neither of those grows a copy of the other's job.

**A world is one integer.** `World.generate()` rebuilds terrain from its seed
(hard rule 4), so joining a game means being told a number, not being sent a
map. That is the single reason co-op is tractable here at all: the expensive
thing was already designed not to need sending.

**The host is the authority on the run; each player owns their own feet.**
A client cannot assert its health, its gold, or a kill - those come from the
host. It does own its position, because anything else means your character
stutters against your own latency, and this is co-op with friends rather than
a competitive game. Positions are still clamped on arrival: a client that
claims to stand at x=1e18 must not be able to break the other player's
renderer.

**Battles are shared, and the host resolves every one of them.** A fight is
one `Battle` object living on the host; everybody else runs a *mirror* of it
that owns no dice and advances no timeline - it draws what the host says is
true and sends back what its player pressed. Two things make that cheap: an
encounter regenerates from a seed exactly as terrain does (hard rule 4), so
the monsters are one integer rather than a payload; and a combat body is
`save.serialize_player`, which is 650 bytes and already hardened against
exactly this kind of input.

**Chests are shared; story flags and warden kills are not.** A chest is a
one-shot thing in the world, so two people looting the same one is simply a
bug, and the host arbitrates: a client asks, the host decides who was first
and rolls what was inside. Story flags are the opposite - every one of them
records "have *I* met the runner / heard this briefing / handed over the
letter", and sharing those would silently rob the second player of scenes
the first already watched. Warden kills stay private for the same reason
battles do: each player has to earn their own Memory, and being handed a
dead warden removes content rather than sharing it.

Everything that arrives from a peer is treated exactly like save data,
because it is exactly as untrusted.
"""
from __future__ import annotations

import time

from . import net
from . import relay
from . import portmap
from . import security as sec
# **`config` imports no pygame** (checked), so this does not break the rule
# at the top of this module that `coop.py` stays headless.
from .config import DIFFICULTIES

# Bumped whenever the message shapes change. Two builds that disagree should
# say so at the lobby rather than desynchronise quietly an hour later.
# The version peers compare in the handshake. **Not `net.PROTOCOL`**,
# which is a different number in a different module and is read by
# nothing - see the note there.
# 7 (s71): eleven new motion names ride in the `motion` event. A peer
# that does not know one **refuses it** rather than raising (`set_motion`),
# so a mismatch is a silent half of the fight not animating - which is
# exactly the kind of disagreement the handshake exists to catch at the
# door instead.
# 8 (s72): `arena` rides in the shared-fight payload, so a room fights
# each wave in the same place rather than each machine deriving it.
# 9 (s73): a wave's pack can be a warden now, so two machines have to
# agree about which waves are boss waves before they roll one.
# 10 (s77): free-roam co-op. Two new message kinds - `rs` carries the
# arena from the host, `ri` carries one guest's input back - and a build
# that does not know them would sit in an empty room watching nothing
# move, which is precisely the disagreement this number exists to catch
# at the door.
# 11 (s81): a guest can say it is at a warden's door, so a warden fight
# is party-wide when the room is assembled (decision 4). A build that
# does not know `boss` would leave one player fighting it alone while
# everybody else stood at the gate.
PROTOCOL = 12
MAX_NAME = 18
# A player who has said nothing for this long has gone.
TIMEOUT = 12.0
# Position updates are cheap but there is no reason to send 60 a second.
SEND_RATE = 1.0 / 15.0
# **How often a guest tells the host where it is pushing** (item 0bg).
# `net.MAX_MESSAGES_PER_SECOND` is 120 and a peer over it is **dropped,
# not throttled** (§7, which cost a whole trade feature once) - so this
# is the one number in free-roam co-op that has to be conservative.
# Twenty a second, with the discrete presses sent as they happen because
# there are only ever a few of those.
ROAM_INPUT_RATE = 1.0 / 20.0
# A claim that never comes back is retried after this long. Holding the key
# down must not put twenty claims for the same chest on the wire.
CLAIM_TIMEOUT = 2.5
# How far from a fight you have to be standing to be pulled into it, in
# tiles. Section 18 promised that walking into a monster would not drag the
# room into a fight nobody chose; being right next to it is what choosing
# looks like, and wandering off on your own still keeps you out.
FIGHT_REACH = 9.0
# Authoritative battle frames per second. The fight is turn-based and mostly
# waits on people, so this is about catching up a dropped frame rather than
# about smoothness.
FRAME_RATE = 1.0 / 12.0
# How far from somebody you have to be standing to offer them a trade, in
# tiles. **Tighter than everything else here on purpose**: a chest claim is
# checked against a position that arrives 15 times a second and a fight
# reaches across a clearing, but asking to trade is a thing you walk up to
# somebody and do.
TRADE_REACH = 2.5
# How far from a chest a claimer is allowed to be standing, in tiles.
# Interaction reaches one tile; positions arrive 15 times a second, so the
# slack is for latency rather than generosity.
CLAIM_REACH = 3.0

HOST = "host"
CLIENT = "client"

# The two halves of the trade protocol, listed rather than pattern-matched
# so that a message going the wrong way is refused instead of half-handled.
# Anything a participant says to the arbiter:
TRADE_TO_HOST = ("tinv", "tans", "toff", "tlock", "tend")
# Anything the arbiter says to a participant:
TRADE_TO_PLAYER = ("task", "topen", "toff", "tlock", "tdone", "tend")


class Remote:
    """Another player, as far as this machine is concerned."""

    def __init__(self, player_id, name="Wanderer"):
        self.player_id = int(player_id)
        self.name = name
        self.x = 0.0
        self.y = 0.0
        self.facing = "front"
        self.walking = False
        # Someone can be fighting without you being in it. Walking into a
        # monster must not drag the whole room into a battle they did not
        # choose, so this is shown rather than acted on.
        self.in_battle = False
        self.look = (1, 1, 1, 0, 1)
        # Their combat body, as `save.serialize_player` wrote it. Sent when
        # they join and refreshed whenever it changes, so that starting a
        # fight costs no round trip - by the time somebody swings, everybody
        # already knows what everybody is made of.
        self.body = None
        # **Which room they are standing in** (s57): `None` outdoors, or the
        # (gx, gy) of the door they walked through. A room is rebuilt from
        # the world seed and those two coordinates (`interiors.build`), so
        # this pair *is* the whole shared room - there is no room state to
        # send, which is why co-op interiors cost a field rather than a
        # system. Two players who name the same door are in the same room.
        self.room = None
        # **Still reading their cards** (item 0bs, s84). A cleared wave
        # puts three offers in front of every player, and the host's
        # "send in wave N" would otherwise start the next fight while
        # somebody is still deciding what they are taking into it.
        self.choosing = False
        self.choosing_since = 0.0
        self.last_seen = time.monotonic()

    @property
    def stale(self):
        return time.monotonic() - self.last_seen > TIMEOUT

    def still_choosing(self, patience):
        """Whether the room should still be waiting for this player.

        **A room must never be stuck**, so this stops being true on its
        own: a guest whose window is closed, whose machine went quiet, or
        who simply walked away stops holding everybody else up. The same
        shape as `JOIN_WINDOW` - a wait that expires is a wait, and one
        that does not is a hang.
        """
        if not self.choosing or self.stale:
            return False
        return time.monotonic() - self.choosing_since < patience


def _island(game):
    """The world a room code is about - never a house interior (s56).

    A player can be standing in somebody's kitchen when a guest joins, and
    `game.world` is that kitchen while they are. Sending its seed would
    have the joiner rebuild their island out of a front parlour, and the
    chest list would be a room's. `Game.overworld()` is the island either
    way; the `getattr` is for the test doubles, which have no houses.
    """
    if hasattr(game, "overworld"):
        return game.overworld()
    return getattr(game, "world", None)


def _house_door(game):
    """The door of the room we are standing in, or `None` for outdoors.

    The counterpart to `_island`. That one exists so a house never leaks
    into the things that treat the world as *the run* (the seed, the chest
    list); this one exists so a house is the one thing that *is* shared -
    which room each player is in, so two people can be in it together.

    The `getattr` is for the test doubles, which have no houses.
    """
    house = getattr(game, "house", None)
    if not house:
        return None
    door = house.get("door") if isinstance(house, dict) else None
    if not door or len(door) != 2:
        return None
    return (int(door[0]), int(door[1]))


def _safe_name(value):
    return sec.sanitize_name(value, "Wanderer")[:MAX_NAME] or "Wanderer"


def _plain(value):
    """JSON-safe copy of a presentation payload.

    Colours arrive as tuples and come back as lists; everything else is
    already a number or a short string. Anything unexpected is dropped
    rather than carried, because an fx payload is decoration and not worth
    a failed frame.
    """
    if isinstance(value, dict):
        return {str(k)[:12]: _plain(v) for k, v in list(value.items())[:12]}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in list(value)[:8]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, str):
        return value[:64]
    return None


def _safe_tile(value):
    """One grid coordinate off a socket. Out of range is refused, not clamped.

    `sec.safe_int` clamps, which is right for a stat and wrong for a
    coordinate: clamping x=-5 up to 0 does not reject the message, it
    quietly asks the world about a different tile instead. Nothing bad
    follows from that today - an unknown tile has no chest on it - but the
    check reads as though it rejected something, and it did not.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return -1
    return value if 0 <= value < 4096 else -1


def _safe_room(value):
    """Which room a position claims to be in, off a socket.

    `None` is outdoors and is the common case. Anything else must be
    exactly two in-range tile coordinates; a malformed one is treated as
    outdoors rather than refused, because the alternative is dropping a
    position update and leaving somebody frozen mid-field.

    `_safe_tile` rather than `sec.safe_int` for the same reason s20 gives:
    a door coordinate is an index, and clamping an index invents a
    different door instead of rejecting a bad one - here that would put
    somebody in a room they never entered.
    """
    if value is None:
        return None
    # **Length is checked before `safe_list`, not after.** `safe_list` caps
    # rather than rejects, so a three-element door arrives as its first two
    # coordinates - which is s20's rule again: that does not reject a
    # malformed door, it quietly invents a different one and puts somebody
    # in a room they never opened.
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    values = sec.safe_list(value, 2)
    if len(values) != 2:
        return None
    gx, gy = _safe_tile(values[0]), _safe_tile(values[1])
    if gx < 0 or gy < 0:
        return None
    return (gx, gy)


def _safe_look(value):
    """Five small ints. Anything else becomes the default look."""
    values = sec.safe_list(value, 5)
    if len(values) != 5:
        return (1, 1, 1, 0, 1)
    return tuple(sec.safe_int(v, 0, 32, 0) for v in values)


def _mode_keys():
    """Every gamemode key, for validating one off a socket.

    Read from `gamemodes` rather than typed here, because the typed
    version was wrong within a day of a fourth mode existing.
    """
    from . import gamemodes
    return tuple(mode.key for mode in gamemodes.MODES)


class Session:
    """One end of a co-op game. Pumped once a frame, never blocks."""

    def __init__(self, role, transport, name="Wanderer"):
        self.role = role
        self.transport = transport
        self.name = _safe_name(name)
        self.player_id = 1 if role == HOST else 0
        self.seed = None
        self.remotes = {}          # player_id -> Remote
        # Which room *we* are in, refreshed every frame from the game (s57).
        # Kept here rather than read from the game at each use so that
        # `near` and the drawing filter agree with what was last broadcast.
        self.room = None
        self.status = ""           # last thing worth showing the player
        self.host_name = ""        # whose valley this is, once welcomed
        # **Whether the host has finished the save it is hosting.** Half of
        # the veteran handshake (s19 item 5c): a finished hero may only
        # cross into a finished world, so both sides have to say. It is a
        # claim by a peer, not a fact - it can only ever *unlock* bringing
        # your own hero into their game, never touch their save, so a lying
        # host cheats itself out of a fresh-start guest and nothing else.
        self.host_beaten = False
        # **Which game this room is playing** (0ag, s59). A room is a
        # world *and now also a mode*: joining a Last Stand room and being
        # handed the island would be the same failure s40 reported from
        # the other direction - the screen changing under you with nothing
        # saying why.
        self.mode = "story"
        #: The host's difficulty, once welcomed. Ours until then.
        self.difficulty = "normal"
        self.error = ""            # set when the session has failed
        self.ready = role == HOST  # a host is ready the moment it opens
        # Whether the transport ever actually opened. Only a browser can
        # answer no: the other two guests connect inside their own
        # constructor and raise if they cannot, so `try_join` shows the
        # real reason and this never runs. A `WebSocket` fails later and
        # silently, and without this the player is told the host closed a
        # room that never answered - which sends them looking for a
        # problem at the other end of a code they probably mistyped.
        self._reached_host = False
        # Set once, when a client learns the host's seed. The game clears it
        # after rebuilding its world; until then the client is standing in
        # the wrong valley.
        self.adopt_seed = False
        # Chests already emptied in the host's world. Held here until the
        # game has finished rebuilding from the seed, because there is
        # nothing to mark opened until the chests exist.
        self.pending_chests = []
        # Set for a host: the thread trying to make this room reachable from
        # outside the house. None until one is asked for.
        self.portal = None
        # Chests this end has asked the host about and not heard back on:
        # (gx, gy) -> the time we asked. Purely a "do not spam the wire"
        # record; the host's answer is what actually decides anything.
        self._claims = {}
        # The shared fight in progress, or None. On the host this is the
        # authority; everywhere else it is a mirror.
        self.fight = None
        # A fight offered to people nearby, waiting on their answers. Host
        # side only, and cleared the moment it opens or times out.
        self.pending = None
        # Host side only: the trade being arbitrated, or None. One at a
        # time in the room - two people swapping gear is a conversation,
        # and a second one running at the same time is a second set of
        # locks to get wrong for no feature anybody asked for.
        self.trade = None
        self.body = None              # our own combat body, as last published
        self._last_send = 0.0
        self._last_roam_input = 0.0
        #: The last movement a guest sent, so an unchanged direction is
        #: not re-sent at all. Standing still costs nothing on the wire.
        self._roam_was = (0.0, 0.0)
        self._last_frame = 0.0

    # -- opening and closing ----------------------------------------------

    @classmethod
    def host(cls, name="Wanderer", port=net.DEFAULT_PORT, seed=None,
             reach_out=True, through_relay=None):
        """Open a room, and try to make it reachable from the internet.

        The reachability work happens on a thread, so the lobby is usable
        immediately and simply shows a local code until the public one is
        ready. `reach_out=False` keeps it strictly on the LAN, which is what
        the tests use - no test should touch somebody's router.

        **`through_relay` is the CGNAT answer** (§85). A port map cannot
        help a host that has no address of its own, so that host dials
        *out* to a relay and parks connections there instead of
        listening. Defaults to *whether a relay is configured at all*,
        because nobody sets `PIXEL_RPG_RELAY_URL` for fun - and there is
        no portal to open when there is no port.
        """
        if through_relay is None:
            through_relay = relay.enabled()
        if through_relay:
            transport = net.RelayHost()
            session = cls(HOST, transport, name)
            session.seed = seed
            session.status = "Waiting for players"
            session.portal = None
            return session
        transport = net.Host(port=port)
        session = cls(HOST, transport, name)
        session.seed = seed
        session.status = "Waiting for players"
        session.portal = portmap.Portal(transport.port, net.local_address(),
                                        enabled=reach_out)
        return session

    @classmethod
    def join(cls, code, name="Wanderer", websocket=False):
        """Join a room. There are three doors now and this is still one line.

        `websocket=True` asks for the WebSocket guest rather than the raw
        one; inside a browser the question does not arise, because only
        `BrowserClient` can run there and `net.guest_transport` says so.
        Everything after this point moves dictionaries and could not tell
        you which was picked.

        **A browser join does not fail here.** A socket refuses at once
        and raises; a `WebSocket` reports failure later on the event loop,
        so a room code that goes nowhere shows up as `connected` turning
        False shortly after, and the lobby has to notice that rather than
        wait for an exception that is not coming.
        """
        # **A six-character code is a relay room** and a ten-character
        # one is an address (`net.encode_room`). Told apart by shape
        # rather than by a setting, so a player types what they were
        # given and the right door opens (§85).
        if relay.valid_room(code):
            transport = net.RelayClient
        else:
            transport = net.guest_transport(websocket)
        client = transport.from_code(code)
        session = cls(CLIENT, client, name)
        session.transport.send("hello", {"protocol": PROTOCOL,
                                         "name": session.name})
        session.status = "Joining"
        return session

    @property
    def room_code(self):
        """The code to read out. Local until the portal says otherwise."""
        if self.role != HOST:
            return ""
        if self.portal is not None and self.portal.done:
            return net.encode_room(self.portal.address, self.transport.port)
        return self.transport.room_code

    @property
    def reach(self):
        """(settled, one-line description) of how far this room carries."""
        if self.role != HOST or self.portal is None:
            return True, ""
        if not self.portal.done:
            return False, "Opening the room to the internet..."
        return True, self.portal.note

    @property
    def player_count(self):
        return 1 + len(self.remotes)

    def fighting(self):
        """Everyone currently in a battle of their own, in *our* world.

        The edge arrow this feeds points at a direction and prints a
        distance, and neither means anything across a wall into another
        world (s57). Nothing hunts you indoors, so in practice this only
        ever hides an outdoor fight from somebody standing in a kitchen -
        which is right: they are not in that valley to help.
        """
        return [r for r in self.here() if r.in_battle]

    # -- shared world state ------------------------------------------------

    def claim_chest(self, gx, gy, level):
        """Ask the host for a chest. True if the question went out.

        A client never opens a chest itself. It asks, and either loot or a
        bare "that one is already empty" comes back - which is the whole
        point: the host is the only thing that knows who got there first.
        """
        if self.role == HOST or not self.ready:
            return False
        key = (int(gx), int(gy))
        now = time.monotonic()
        waiting = self._claims.get(key)
        if waiting is not None and now - waiting < CLAIM_TIMEOUT:
            return False
        self._claims[key] = now
        try:
            self.transport.send("claim", {"x": int(gx), "y": int(gy),
                                          "lvl": int(level)})
        except Exception:                                 # noqa: BLE001
            self._claims.pop(key, None)
            return False
        return True

    # -- shared battles ----------------------------------------------------

    def publish_body(self, body):
        """Tell the room what we are made of.

        Sent on joining and after anything that changes the numbers - a
        level, a new sword, the end of a fight. Cheap enough to be careless
        with; heavy enough not to ride along with a position update.
        """
        if not isinstance(body, dict) or not self.ready:
            return
        self.body = body
        try:
            if self.role == HOST:
                self.transport.broadcast("body", {"id": self.player_id,
                                                  "p": body})
            else:
                self.transport.send("body", {"p": body})
        except Exception:                                 # noqa: BLE001
            pass

    def bodies(self):
        """player_id -> combat body, for everyone who has published one."""
        out = {}
        if self.body is not None:
            out[self.player_id] = self.body
        for remote in self.remotes.values():
            if remote.body is not None:
                out[remote.player_id] = remote.body
        return out

    def together(self, remote):
        """Are we and `remote` in the same place at all? (s57)

        **Distance is meaningless between two different worlds.** A room is
        painted into the middle of its own grid, so somebody indoors stands
        at overworld-looking coordinates near the centre of the map - close,
        in pure arithmetic, to anybody out on the island there. Every
        proximity test has to ask this before it measures anything.
        """
        return remote.room == self.room

    def here(self):
        """The remotes sharing our world - the ones to draw and to reach."""
        return [r for r in self.remotes.values() if self.together(r)]

    def near(self, x, y, tile):
        """Who is standing close enough to be part of a fight here."""
        tile = max(1, int(tile))
        close = []
        for remote in self.here():
            if abs(remote.x / tile - x) <= FIGHT_REACH \
                    and abs(remote.y / tile - y) <= FIGHT_REACH:
                close.append(remote.player_id)
        return close

    def request_fight(self, level, biome):
        """Client side: "I walked into something."

        The client does not roll the encounter, decide who is in it, or pick
        the seed - it reports that it bumped into a monster and the host
        answers with a fight. Anything else is a client asserting state.
        """
        if self.role == HOST or not self.ready or self.fight is not None:
            return False
        try:
            self.transport.send("spawn", {"lv": int(level),
                                          "bi": str(biome)[:24]})
        except Exception:                                 # noqa: BLE001
            return False
        return True

    def request_boss(self, boss_key):
        """Client side: "I am at a warden's door and we are all here."

        **The host still decides** (§21, decision 4). A guest reports
        which door it is standing at; whether the room is assembled, what
        the fight is sized for and what seed it rolls on are the host's,
        exactly as they are for every other shared fight.
        """
        if self.role == HOST or not self.ready or self.fight is not None:
            return False
        try:
            self.transport.send("boss", {"k": str(boss_key)[:32]})
        except Exception:                                 # noqa: BLE001
            return False
        return True

    #: How long the room waits for somebody reading their cards.
    #: Longer than `JOIN_WINDOW` because this is a real decision with
    #: twelve things to read, and still finite for the reason above.
    PICK_PATIENCE = 90.0

    def say_choosing(self, on):
        """Client side: "I am still picking" / "I am done" (item 0bs).

        **Told, not asked.** What a player takes is their own business
        and the host has no say in it; the only thing the room needs to
        agree on is *when the next wave starts*, and that is the host's
        as it always was.
        """
        if self.role == HOST or not self.ready:
            return False
        try:
            self.transport.send("pick", {"on": 1 if on else 0})
        except Exception:                                 # noqa: BLE001
            return False
        return True

    def still_choosing(self):
        """Host side: the names of everybody still at their offers.

        Empty solo, empty in a room where everybody has answered, and
        empty again once `PICK_PATIENCE` runs out - because a room that
        cannot start its next wave until an absent player presses a key
        is a room somebody has to restart.
        """
        if self.role != HOST:
            return []
        return [remote.name or "somebody"
                for remote in self.remotes.values()
                if remote.still_choosing(self.PICK_PATIENCE)]

    def send_act(self, kind, key="", target=0):
        """Client side: what this player just pressed."""
        if self.role == HOST or self.fight is None:
            return False
        try:
            self.transport.send("act", {"k": str(kind)[:12],
                                        "key": str(key)[:32],
                                        "t": int(target)})
        except Exception:                                 # noqa: BLE001
            return False
        return True

    # How long an invitation stands before the fight starts without you.
    # **Short on purpose**: it is dead time for whoever triggered the fight,
    # and the only thing it buys is not conscripting somebody. Long enough
    # to read a line and press a key, and no longer.
    JOIN_WINDOW = 4.0

    def offer_fight(self, info, invited):
        """Host side: ask everyone nearby whether they want in.

        **Nobody is added to a fight they did not agree to** (2026-09-01).
        Proximity used to be consent: anyone within `FIGHT_REACH` was put
        in the party, which is how Savvydev kept ending up in his friend's
        battles from across the map. Proximity now only earns you the
        question.

        Returns the pending offer, which the host holds until everybody
        answers or `JOIN_WINDOW` runs out.
        """
        if self.role != HOST:
            return None
        self.pending = {
            "info": dict(info),
            "invited": [int(p) for p in invited],
            "accepted": [],
            "left": float(self.JOIN_WINDOW),
        }
        for player_id in self.pending["invited"]:
            peer = self.peer_for(player_id)
            if peer is None:
                continue
            try:
                self.transport.send(peer, "offer", {
                    "by": str(info.get("by", ""))[:24],
                    "lv": int(info.get("level", 1)),
                    "for": float(self.JOIN_WINDOW)})
            except Exception:                             # noqa: BLE001
                pass
        return self.pending

    def peer_for(self, player_id):
        for peer in list(getattr(self.transport, "peers", []) or []):
            if getattr(peer, "player_id", None) == player_id:
                return peer
        return None

    def accept_offer(self):
        """Client side: yes, I am coming."""
        if self.role == HOST or not self.ready:
            return False
        try:
            self.transport.send("join", {})
        except Exception:                                 # noqa: BLE001
            return False
        return True

    def open_fight(self, info):
        """Host side: announce a fight and remember who is in it."""
        if self.role != HOST:
            return
        self.fight = dict(info)
        try:
            self.transport.broadcast("fight", info)
        except Exception:                                 # noqa: BLE001
            pass

    def push_frame(self, frame, force=False):
        """Host side: the authoritative state of the fight, rate limited."""
        if self.role != HOST or self.fight is None:
            return
        now = time.monotonic()
        if not force and now - self._last_frame < FRAME_RATE:
            return
        self._last_frame = now
        try:
            self.transport.broadcast("bs", frame)
        except Exception:                                 # noqa: BLE001
            pass

    def push_roam(self, frame, force=False):
        """Host side: where everything in the arena is (item 0bg).

        The same shape and the same rate as `push_frame`, and for the
        same reason - the host is the authority on a shared fight (§21),
        whether that fight takes turns or not.
        """
        if self.role != HOST or self.fight is None:
            return
        now = time.monotonic()
        if not force and now - self._last_frame < FRAME_RATE:
            return
        self._last_frame = now
        try:
            self.transport.broadcast("rs", frame)
        except Exception:                                 # noqa: BLE001
            pass

    def send_roam_input(self, dx, dy, swing=False, dodge=False, skill="",
                        force=False):
        """Client side: which way this player is pushing, and what they hit.

        **Movement is rate limited and the presses are not.** A direction
        held for a second is the same message sixty times; a swing is a
        thing that happened once and must not wait for the next tick.
        """
        if self.role == HOST or self.fight is None:
            return False
        now = time.monotonic()
        pressed = bool(swing or dodge or skill)
        moved = (round(dx, 2), round(dy, 2)) != self._roam_was
        if not force and not pressed and not moved \
                and now - self._last_roam_input < ROAM_INPUT_RATE:
            return False
        if not pressed and not moved and \
                now - self._last_roam_input < ROAM_INPUT_RATE:
            return False
        self._last_roam_input = now
        self._roam_was = (round(dx, 2), round(dy, 2))
        try:
            self.transport.send("ri", {"x": round(float(dx), 2),
                                       "y": round(float(dy), 2),
                                       "s": 1 if swing else 0,
                                       "d": 1 if dodge else 0,
                                       "k": str(skill)[:24]})
        except Exception:                                 # noqa: BLE001
            return False
        return True

    def push_fx(self, kind, data):
        """Host side: one presentation event, replayed on every screen."""
        if self.role != HOST or self.fight is None:
            return
        try:
            self.transport.broadcast("bfx", {"k": str(kind)[:16],
                                             "d": _plain(data)})
        except Exception:                                 # noqa: BLE001
            pass

    def close_fight(self, payload):
        """Host side: it is over, and here is what everybody earned."""
        if self.role != HOST:
            return
        self.fight = None
        try:
            self.transport.broadcast("be", payload)
        except Exception:                                 # noqa: BLE001
            pass

    def announce_chest(self, gx, gy):
        """Host side: tell the room a chest is now empty."""
        if self.role != HOST:
            return
        try:
            self.transport.broadcast("chest", {"x": int(gx), "y": int(gy)})
        except Exception:                                 # noqa: BLE001
            pass

    # -- trading -----------------------------------------------------------
    #
    # **The host arbitrates the agreement; each end applies its own half.**
    # A trade is the one shared thing here that touches two *bags*, and the
    # host has never held a client's bag (see the class docstring). So the
    # host owns the questions that have exactly one answer - who is trading
    # whom, whether both said yes, and when it is settled - and each player
    # moves their own items when told.
    #
    # Every message goes through the host even when the host is in the
    # trade, because a second path is a second set of bugs. `_deliver`
    # hands a message either to a peer or straight back to this machine,
    # and nothing above it has to know which.

    def _deliver(self, game, player_id, kind, data):
        """Send to a player, whoever they are - a peer, or us."""
        if player_id == self.player_id:
            self._client_trade(game, kind, data)
            return True
        peer = self.peer_for(player_id)
        if peer is None:
            return False
        try:
            self.transport.send(peer, kind, data)
        except Exception:                                 # noqa: BLE001
            return False
        return True

    def _name_of(self, player_id):
        if player_id == self.player_id:
            return self.name
        remote = self.remotes.get(player_id)
        return remote.name if remote else "Wanderer"

    def _to_host(self, game, kind, data):
        """Say something to the arbiter, which may be this machine."""
        if self.role == HOST:
            self._host_trade(game, self.player_id, kind, data)
            return True
        if not self.ready:
            return False
        try:
            self.transport.send(kind, data)
        except Exception:                                 # noqa: BLE001
            return False
        return True

    def invite_trade(self, game, player_id):
        """"Do you want to trade?", asked of one person."""
        if not self.ready or self.trade is not None:
            return False
        return self._to_host(game, "tinv", {"to": int(player_id)})

    def answer_trade(self, game, accept):
        return self._to_host(game, "tans", {"ok": bool(accept)})

    def send_trade_offer(self, game, wire):
        return self._to_host(game, "toff", {"o": wire})

    def send_trade_lock(self, game, locked):
        return self._to_host(game, "tlock", {"on": bool(locked)})

    def end_trade(self, game, reason=""):
        return self._to_host(game, "tend", {"why": str(reason)[:60]})

    # -- host side of a trade ----------------------------------------------

    def _trade_pair(self):
        return (self.trade or {}).get("pair", ())

    def _host_trade(self, game, from_id, kind, data):
        """One trade message, from anybody, decided here."""
        data = sec.safe_dict(data)
        if kind == "tinv":
            self._host_trade_invite(game, from_id, data)
        elif kind == "tans":
            self._host_trade_answer(game, from_id, data)
        elif kind == "toff":
            self._host_trade_offer(game, from_id, data)
        elif kind == "tlock":
            self._host_trade_lock(game, from_id, data)
        elif kind == "tend":
            self._host_trade_end(game, from_id, data)

    def _host_trade_invite(self, game, from_id, data):
        target = sec.safe_int(data.get("to"), 0, 99, 0)
        if self.trade is not None:
            self._deliver(game, from_id, "tend",
                          {"why": "somebody is already trading"})
            return
        known = {self.player_id} | set(self.remotes)
        if target not in known or target == from_id or from_id not in known:
            return
        self.trade = {"pair": (from_id, target), "asked": target,
                      "offers": {}, "locks": {}}
        if not self._deliver(game, target, "task",
                             {"by": int(from_id),
                              "name": self._name_of(from_id)}):
            # They went while the question was being asked. Without this
            # the room holds a trade nobody is in, and the *next* person to
            # press T is told somebody is already trading, forever.
            self.trade = None
            self._deliver(game, from_id, "tend", {"why": "they left"})

    def _host_trade_answer(self, game, from_id, data):
        trade = self.trade
        if not trade or trade.get("asked") != from_id:
            return
        other = [p for p in trade["pair"] if p != from_id]
        if not sec.safe_bool(data.get("ok"), False):
            self.trade = None
            for player_id in trade["pair"]:
                self._deliver(game, player_id, "tend", {"why": "declined"})
            return
        trade["asked"] = None
        for player_id in trade["pair"]:
            partner = other[0] if player_id == from_id else from_id
            self._deliver(game, player_id, "topen",
                          {"with": int(partner),
                           "name": self._name_of(partner)})

    def _host_trade_offer(self, game, from_id, data):
        """An offer changed. Check it is backed, then relay it.

        **The check is `trade.covers`**, against the last body its owner
        published - which `game` republishes as the screen opens, so during
        a trade it is current by construction. A body that does not back
        the offer ends the trade rather than quietly trimming it: a trimmed
        offer is an offer nobody agreed to.
        """
        from . import trade as trade_mod
        trade = self.trade
        if not trade or from_id not in trade["pair"] or trade.get("asked"):
            return
        offer = trade_mod.Offer.from_wire(data.get("o"))
        body = (self.body if from_id == self.player_id
                else getattr(self.remotes.get(from_id), "body", None))
        if not trade_mod.covers(body, offer):
            self._host_trade_end(game, from_id,
                                 {"why": "that offer is not backed by "
                                         "what they are carrying"})
            return
        trade["offers"][from_id] = offer.to_wire()
        # An offer that changed clears both locks; both ends do that for
        # themselves in `TradeSession`, and the host forgets them too so
        # that a `tlock` still in flight cannot settle the old pair.
        trade["locks"] = {}
        for player_id in trade["pair"]:
            if player_id != from_id:
                self._deliver(game, player_id, "toff", {"o": offer.to_wire()})

    def _host_trade_lock(self, game, from_id, data):
        trade = self.trade
        if not trade or from_id not in trade["pair"] or trade.get("asked"):
            return
        on = sec.safe_bool(data.get("on"), False)
        trade["locks"][from_id] = on
        for player_id in trade["pair"]:
            if player_id != from_id:
                self._deliver(game, player_id, "tlock", {"on": on})
        if all(trade["locks"].get(p) for p in trade["pair"]):
            self.trade = None
            for player_id in trade["pair"]:
                self._deliver(game, player_id, "tdone", {})
            self.status = "%s and %s traded" % (self._name_of(trade["pair"][0]),
                                                self._name_of(trade["pair"][1]))

    def _host_trade_end(self, game, from_id, data):
        trade = self.trade
        if not trade or from_id not in trade["pair"]:
            return
        self.trade = None
        why = sec.safe_str(data.get("why"), 60) or "the trade was called off"
        for player_id in trade["pair"]:
            self._deliver(game, player_id, "tend", {"why": why})

    def _trade_lost(self, game, player_id):
        """Somebody dropped. If they were mid-trade, nothing moves."""
        if self.role != HOST or not self.trade:
            return
        if player_id in self.trade["pair"]:
            self._host_trade_end(game, player_id, {"why": "they left"})

    # -- the end that owns a bag -------------------------------------------

    def _client_trade(self, game, kind, data):
        """A trade message addressed to this player, host or guest alike."""
        data = sec.safe_dict(data)
        if kind == "task":
            hook = getattr(game, "trade_asked", None)
            if callable(hook):
                hook(sec.safe_int(data.get("by"), 0, 99, 0),
                     _safe_name(data.get("name")))
        elif kind == "topen":
            hook = getattr(game, "trade_opened", None)
            if callable(hook):
                hook(sec.safe_int(data.get("with"), 0, 99, 0),
                     _safe_name(data.get("name")))
        elif kind == "toff":
            hook = getattr(game, "trade_partner_offer", None)
            if callable(hook):
                hook(sec.safe_dict(data.get("o")))
        elif kind == "tlock":
            hook = getattr(game, "trade_partner_lock", None)
            if callable(hook):
                hook(sec.safe_bool(data.get("on"), False))
        elif kind == "tdone":
            hook = getattr(game, "trade_settled", None)
            if callable(hook):
                hook()
        elif kind == "tend":
            hook = getattr(game, "trade_ended", None)
            if callable(hook):
                hook(sec.safe_str(data.get("why"), 60))

    def close(self):
        # Give the router its port back rather than leaving a hole open for
        # the six hours the lease would otherwise run.
        if self.portal is not None:
            try:
                self.portal.release()
            except Exception:                             # noqa: BLE001
                pass
            self.portal = None
        try:
            self.transport.close()
        except Exception:                                 # noqa: BLE001
            pass
        self.remotes = {}
        # The session outlives the room only as a corpse, but leaving a
        # room puts the house down too (`Game.leave_coop`), so a stale
        # door here would outlast the world it named.
        self.room = None

    def fail(self, message):
        self.error = message
        self.status = message
        self.close()

    # -- the frame ---------------------------------------------------------

    def update(self, game, dt):
        """One frame of co-op. Returns False once the session is over."""
        if self.error:
            return False
        try:
            messages = self.transport.pump()
        except Exception as exc:                          # noqa: BLE001
            self.fail("connection lost: %s" % exc)
            return False

        if self.role == HOST:
            self._host_frame(game, messages)
        else:
            self._client_frame(game, messages)

        self._expire(game)
        self._broadcast_self(game)
        return not self.error

    def _expire(self, game=None):
        for player_id, remote in list(self.remotes.items()):
            if remote.stale:
                del self.remotes[player_id]
                self.status = "%s left" % remote.name
                self._left_the_fight(player_id)
                self._trade_lost(game, player_id)

    def _broadcast_self(self, game):
        """Tell the others where we are, at a sane rate."""
        player = getattr(game, "player", None)
        if player is None:
            return
        # **Before the rate limit, not after.** `together` and the drawing
        # filter read `self.room` every frame, while this sends 15 times a
        # second - a room refreshed only on send frames would leave us
        # drawing the kitchen we had already walked out of.
        self.room = _house_door(game)
        now = time.monotonic()
        if now - self._last_send < SEND_RATE:
            return
        self._last_send = now
        payload = {"x": round(player.x, 1), "y": round(player.y, 1),
                   "f": player.facing,
                   "w": bool(getattr(game, "moving", False)),
                   "b": bool(getattr(game, "battle", None)),
                   # `None` outdoors. A door plus the world seed everybody
                   # already shares *is* the room (s57), so this is the
                   # whole of what an interior costs the protocol.
                   "r": list(self.room) if self.room else None}
        if self.role == HOST:
            payload["id"] = self.player_id
            self.transport.broadcast("at", payload)
        else:
            self.transport.send("at", payload)

    # -- host --------------------------------------------------------------

    def _host_frame(self, game, messages):
        for kind, peer in self.transport.events:
            if kind == "leave" and peer.player_id in self.remotes:
                remote = self.remotes.pop(peer.player_id)
                self.status = "%s left" % remote.name
                self.transport.broadcast("gone", {"id": peer.player_id})
                self._left_the_fight(peer.player_id)
                self._trade_lost(game, peer.player_id)
        self.transport.events = []

        for peer, kind, data in messages:
            if kind == "hello":
                self._greet(game, peer, data)
            elif kind == "at" and peer.player_id in self.remotes:
                self._place(self.remotes[peer.player_id], data)
                # Relay to everyone else; the sender already knows.
                self.transport.broadcast(
                    "at", dict(data, id=peer.player_id), skip=peer)
            elif kind == "claim" and peer.player_id in self.remotes:
                self._claimed(game, peer, data)
            elif kind == "body" and peer.player_id in self.remotes:
                self._took_body(peer.player_id, data, relay_from=peer)
            elif kind == "spawn" and peer.player_id in self.remotes:
                self._asked_for_a_fight(game, peer, data)
            elif kind == "act" and peer.player_id in self.remotes:
                self._acted(game, peer, data)
            elif kind == "ri" and peer.player_id in self.remotes:
                self._roam_input(game, peer, data)
            elif kind == "boss" and peer.player_id in self.remotes:
                self._asked_for_a_warden(game, peer, data)
            elif kind == "pick" and peer.player_id in self.remotes:
                remote = self.remotes[peer.player_id]
                remote.choosing = bool(sec.safe_int(data.get("on"), 0, 1, 0))
                remote.choosing_since = time.monotonic()
            elif kind == "join" and peer.player_id in self.remotes:
                self._accepted_invitation(peer)
            elif kind in TRADE_TO_HOST and peer.player_id in self.remotes:
                self._host_trade(game, peer.player_id, kind, data)

    def _greet(self, game, peer, data):
        """Accept a player, or turn them away with a reason."""
        if sec.safe_int(data.get("protocol"), 0, 999, 0) != PROTOCOL:
            self.transport.drop(peer, "different version of the game")
            return
        if self.seed is None:
            world = _island(game)
            self.seed = getattr(world, "seed", None) if world else None
        if self.seed is None:
            self.transport.drop(peer, "the host has no run in progress")
            return

        remote = Remote(peer.player_id, _safe_name(data.get("name")))
        remote.look = _safe_look(data.get("look"))
        self.remotes[peer.player_id] = remote
        peer.name = remote.name
        self.status = "%s joined" % remote.name

        self.transport.send(peer, "welcome", {
            "protocol": PROTOCOL,
            "id": peer.player_id,
            "seed": int(self.seed),
            "host": self.name,
            # The veteran handshake. `beaten` is per-save (s19 decision 5),
            # so it is read off the run being hosted rather than off the
            # profile - `meta.endings` is global and would say yes for a
            # world nobody has finished.
            "beaten": bool(getattr(getattr(game, "player", None),
                                   "beaten", False)),
            # The mode is part of the handshake rather than something a
            # guest infers, for the same reason the protocol version is:
            # two ends that disagree should say so at the door.
            "mode": str(getattr(game, "mode", "story"))[:16],
            # **And how hard the host set it.** A wave score is multiplied
            # by difficulty (s59), so a guest scoring against its own
            # local setting would be scoring a game nobody played - the
            # monsters were rolled on the host's.
            "diff": str(getattr(getattr(game, "settings", None),
                                "difficulty", "normal"))[:16],
            # The seed rebuilds the terrain; this is the part of the world
            # that has since happened to it. Twenty-two pairs of small ints,
            # so it rides along with the handshake rather than needing a
            # catch-up conversation of its own.
            "chests": self.opened_chests(game),
        })
        # And tell everybody who is now here.
        self.transport.broadcast("roster", {"players": self.roster()})

    def _claimed(self, game, peer, data):
        """Somebody asked for a chest. Decide, roll, and answer.

        Nothing here trusts the asker for anything except *which* chest.
        The tier comes from the host's own world, the level is clamped, and
        the roll happens here - a client that could name its own loot would
        be writing its own save with extra steps.
        """
        world = _island(game)
        if world is None:
            return
        gx, gy = _safe_tile(data.get("x")), _safe_tile(data.get("y"))
        chest = world.chest_at(gx, gy) if gx >= 0 and gy >= 0 else None
        if chest is None:
            return

        remote = self.remotes[peer.player_id]
        tile = max(1, int(getattr(game, "tile", 32)))
        if (abs(remote.x / tile - gx) > CLAIM_REACH
                or abs(remote.y / tile - gy) > CLAIM_REACH):
            # Standing nowhere near it. Say nothing rather than explain.
            return

        if chest.opened:
            # Not a refusal so much as news: their world is out of date.
            self.transport.send(peer, "chest", {"x": gx, "y": gy})
            return

        level = sec.safe_int(data.get("lvl"), 1, 99, 1)
        loot = None
        roll = getattr(game, "chest_loot", None)
        if callable(roll):
            try:
                loot = roll(chest.tier, level)
            except Exception:                             # noqa: BLE001
                loot = None
        mark = getattr(game, "mark_chest_opened", None)
        if callable(mark):
            mark(gx, gy)
        else:
            chest.opened = True
        self.transport.send(peer, "loot", dict(loot or {}, x=gx, y=gy))
        self.transport.broadcast("chest", {"x": gx, "y": gy}, skip=peer)
        self.status = "%s opened a chest" % remote.name

    def _took_body(self, player_id, data, relay_from=None):
        """Somebody published what they are made of.

        Not trusted here, only carried: it is rebuilt through
        `save.deserialize_player`, which validates every field of it, at the
        moment a fight actually needs it.
        """
        body = sec.safe_dict(data.get("p"))
        if not body:
            return
        remote = self.remotes.get(player_id)
        if remote is not None:
            remote.body = body
        if self.role == HOST:
            self.transport.broadcast("body", {"id": player_id, "p": body},
                                     skip=relay_from)

    def _asked_for_a_warden(self, game, peer, data):
        """A client is at a warden's door (decision 4).

        **The key is checked against the world, not trusted**: a boss
        name off a socket names a real warden or it names nothing, which
        is §20's rule that a coordinate is refused rather than clamped,
        applied to a key.
        """
        if self.fight is not None:
            return
        from .entities import BOSS_BY_KEY
        definition = BOSS_BY_KEY.get(sec.safe_str(data.get("k"), 32))
        if definition is None:
            return
        start = getattr(game, "open_warden", None)
        if callable(start):
            start(definition)

    def _asked_for_a_fight(self, game, peer, data):
        """A client bumped into a monster. The host decides what that means."""
        if self.fight is not None:
            return
        start = getattr(game, "host_open_fight", None)
        if not callable(start):
            return
        level = sec.safe_int(data.get("lv"), 1, 99, 1)
        biome = sec.safe_str(data.get("bi"), 24) or "meadow"
        remote = self.remotes[peer.player_id]
        tile = max(1, int(getattr(game, "tile", 32)))
        try:
            opened = start(level, biome, remote.x / tile, remote.y / tile,
                           trigger=peer.player_id)
        except Exception:                                 # noqa: BLE001
            opened = False
        if opened:
            return
        # The host was too far away to be in it, so there is nobody to share
        # it with. Hand it back rather than swallowing it - otherwise a
        # player exploring on their own walks through monsters that silently
        # never happen.
        try:
            self.transport.send(peer, "solo", {"lv": level, "bi": biome})
        except Exception:                                 # noqa: BLE001
            pass

    def _accepted_invitation(self, peer):
        """Host side: somebody said yes to the offer that is standing.

        **Only somebody who was asked.** The list of invitations was
        decided by the host from real positions; a client that says "join"
        without one is a client trying to add itself to a fight, which is
        the shape `net.py` rule 2 exists to refuse.
        """
        pending = self.pending
        if not pending or peer.player_id not in pending["invited"]:
            return
        if peer.player_id not in pending["accepted"]:
            pending["accepted"].append(peer.player_id)

    def _acted(self, game, peer, data):
        """A client pressed something. Only the host decides if it counts."""
        if self.fight is None:
            return
        apply_act = getattr(game, "host_apply_act", None)
        if not callable(apply_act):
            return
        try:
            apply_act(peer.player_id,
                      sec.safe_str(data.get("k"), 12),
                      sec.safe_str(data.get("key"), 32),
                      sec.safe_int(data.get("t"), 0, 15, 0))
        except Exception:                                 # noqa: BLE001
            pass

    def opened_chests(self, game):
        """Which chests are already empty, for a joiner catching up."""
        world = _island(game)
        if world is None:
            return []
        return [[c.gx, c.gy] for c in world.chests if c.opened]

    def _left_the_fight(self, player_id):
        """Somebody dropped. If they were mid-fight, they ran.

        Treated as fleeing rather than as a pause: a turn-based fight that
        waits on a player who is not coming back is a fight that never ends,
        and the flee path already knows how to take somebody out of one
        without ending it for the rest.
        """
        if self.role != HOST or self.fight is None:
            return
        drop = getattr(self, "_on_fighter_lost", None)
        if callable(drop):
            drop(player_id)

    def roster(self):
        out = [{"id": self.player_id, "name": self.name}]
        out += [{"id": r.player_id, "name": r.name}
                for r in self.remotes.values()]
        return out

    # -- client ------------------------------------------------------------

    def _client_frame(self, game, messages):
        # Asked of the transport rather than watched from here. Watching
        # `ready` once a frame misses a socket that opened and died in
        # between, and then blames the host for a room that answered.
        # Only `BrowserClient` defines it; the two socket transports are
        # connected by the time they exist, which is what the default says.
        if getattr(self.transport, "reached", True):
            self._reached_host = True
        if not self.transport.connected and not self.ready:
            self.fail("the host closed the room" if self._reached_host
                      else "nothing answered at that room code")
            return
        for kind, data in messages:
            if kind == "welcome":
                self._welcomed(data)
            elif kind == "bye":
                self.fail(sec.safe_str(data.get("reason"), 80)
                          or "the host closed the room")
            elif kind == "solo":
                # The host declined a shared fight because nobody else was
                # near. Fight it here, the way solo play always has.
                alone = getattr(game, "start_lone_encounter", None)
                if callable(alone):
                    alone(sec.safe_int(data.get("lv"), 1, 99, 1),
                          sec.safe_str(data.get("bi"), 24) or "meadow")
            elif kind == "full":
                self.fail("that room is full")
            elif kind == "roster":
                self._roster(data)
            elif kind == "at":
                player_id = sec.safe_int(data.get("id"), 0, 99, 0)
                if player_id and player_id != self.player_id:
                    remote = self.remotes.setdefault(player_id,
                                                     Remote(player_id))
                    self._place(remote, data)
            elif kind == "gone":
                self.remotes.pop(sec.safe_int(data.get("id"), 0, 99, 0), None)
            elif kind == "chest":
                self._chest_emptied(game, data)
            elif kind == "loot":
                self._chest_won(game, data)
            elif kind == "body":
                self._took_body(sec.safe_int(data.get("id"), 0, 99, 0), data)
            elif kind == "offer":
                self._invited(game, data)
            elif kind == "fight":
                self._fight_opened(game, data)
            elif kind == "rs":
                self._roam_frame(game, data)
            elif kind == "bs":
                self._fight_frame(game, data)
            elif kind == "bfx":
                self._fight_fx(game, data)
            elif kind == "be":
                self._fight_closed(game, data)
            elif kind in TRADE_TO_PLAYER:
                self._client_trade(game, kind, data)

    def _chest_emptied(self, game, data):
        """Someone else got there first, or we are catching up on arrival."""
        gx, gy = _safe_tile(data.get("x")), _safe_tile(data.get("y"))
        if gx < 0 or gy < 0:
            return
        # Was this the answer to a question we asked? If so the player is
        # standing there having just pressed the key, and silence reads as
        # a broken button rather than as "someone beat you to it".
        mine = self._claims.pop((gx, gy), None) is not None
        mark = getattr(game, "mark_chest_opened", None)
        if callable(mark):
            mark(gx, gy)
        if mine:
            lost = getattr(game, "chest_lost", None)
            if callable(lost):
                lost(gx, gy)

    def _chest_won(self, game, data):
        """The host says this chest was ours, and here is what was in it."""
        gx, gy = _safe_tile(data.get("x")), _safe_tile(data.get("y"))
        if gx < 0 or gy < 0:
            return
        self._claims.pop((gx, gy), None)
        take = getattr(game, "take_chest_loot", None)
        if callable(take):
            take(gx, gy, sec.safe_dict(data))

    def _invited(self, game, data):
        """The host is asking whether we want in on somebody's fight."""
        invite = getattr(game, "offered_a_fight", None)
        if callable(invite):
            invite(sec.safe_str(data.get("by"), 24),
                   sec.safe_int(data.get("lv"), 1, 99, 1),
                   sec.safe_float(data.get("for"), 0.5, 30.0, 4.0))

    def _fight_opened(self, game, data):
        """The host says a fight has started, and whether we are in it."""
        party = [sec.safe_int(v, 0, 99, -1)
                 for v in sec.safe_list(data.get("party"), 8)]
        party = [v for v in party if v >= 0]
        if self.player_id not in party:
            # Somebody else's fight. Their name tag already says so.
            return
        info = {
            "seed": sec.safe_int(data.get("seed"), 0, 2 ** 31 - 1, 0),
            "level": sec.safe_int(data.get("level"), 1, 99, 1),
            "biome": sec.safe_str(data.get("biome"), 24) or "meadow",
            "boss": sec.safe_str(data.get("boss"), 32),
            "party": party,
            # **A wave names its own pack size** (0ag, s59). Without this
            # the mirror rolls `pack_size(party)` and gets a different
            # fight from the host's - which is a desync rather than a
            # cosmetic difference, because every later frame is indexed
            # against the monster list.
            "wave": sec.safe_int(data.get("wave"), 0, 99_999, 0),
            "count": sec.safe_int(data.get("count"), 0, 16, 0),
            # **Which engine this fight is** (item 0bg). The dict is
            # rebuilt here rather than passed through, which is right -
            # nothing off a socket should reach the game unclamped - but
            # a key that is not listed is a key that is silently dropped,
            # and this one decides whether a night opens at all.
            # **Every mode the game has, read from the game** (§87). The
            # comment above was already right and the list under it was
            # already stale: a hoard announced by the host arrived as
            # `"story"`, so the guest stood in the lobby while everybody
            # else played. A hand-kept tuple beside a warning about
            # hand-kept tuples is the joke this file has now made twice.
            "mode": sec.safe_choice(sec.safe_str(data.get("mode"), 16),
                                    _mode_keys(), "story"),
            "night": sec.safe_int(data.get("night"), 0, 99_999, 0),
        }
        self.fight = info
        begin = getattr(game, "begin_shared_battle", None)
        if callable(begin):
            begin(info, self.bodies())

    def _roam_input(self, game, peer, data):
        """Host side: one guest's intent for one frame (item 0bg).

        **Every field is clamped, and the index is taken from the peer
        rather than from the message.** A body index off a socket is
        refused, not trusted (§20) - and here it is never even asked
        for: which hero this input drives is decided by *who sent it*,
        which is the one thing a peer cannot lie about.
        """
        drive = getattr(game, "roam_remote_input", None)
        if not callable(drive):
            return
        data = sec.safe_dict(data)
        drive(peer.player_id,
              sec.safe_float(data.get("x"), -1.0, 1.0, 0.0),
              sec.safe_float(data.get("y"), -1.0, 1.0, 0.0),
              bool(data.get("s")), bool(data.get("d")),
              sec.safe_str(data.get("k"), 24))

    def _roam_frame(self, game, data):
        """Client side: take the host's word for the arena."""
        if self.fight is None:
            return
        apply_frame = getattr(game, "roam_frame", None)
        if callable(apply_frame):
            apply_frame(sec.safe_dict(data))

    def _fight_frame(self, game, data):
        if self.fight is None:
            return
        apply_frame = getattr(game, "shared_battle_frame", None)
        if callable(apply_frame):
            apply_frame(sec.safe_dict(data))

    def _fight_fx(self, game, data):
        if self.fight is None:
            return
        replay = getattr(game, "shared_battle_fx", None)
        if callable(replay):
            replay(sec.safe_str(data.get("k"), 16), sec.safe_dict(data.get("d")))

    def _fight_closed(self, game, data):
        if self.fight is None:
            return
        self.fight = None
        finish = getattr(game, "finish_shared_battle", None)
        if callable(finish):
            finish(sec.safe_dict(data), self.player_id)

    def _welcomed(self, data):
        if sec.safe_int(data.get("protocol"), 0, 999, 0) != PROTOCOL:
            self.fail("different version of the game")
            return
        self.player_id = sec.safe_int(data.get("id"), 0, 99, 0)
        self.seed = sec.safe_int(data.get("seed"), 0, 2 ** 31 - 1, 0)
        self.adopt_seed = True
        self.ready = True
        pending = [
            (_safe_tile(pair[0]), _safe_tile(pair[1]))
            for pair in sec.safe_list(data.get("chests"), 128)
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        ]
        self.pending_chests = [p for p in pending if p[0] >= 0 and p[1] >= 0]
        # Kept, not just announced. The world you are about to stand in
        # is theirs (hard rule 4, s18) and nothing on screen said so -
        # which is why the playtest reported it as a bug (s40).
        self.host_name = _safe_name(data.get("host"))
        self.host_beaten = sec.safe_bool(data.get("beaten"), False)
        self.mode = sec.safe_str(data.get("mode"), 16) or "story"
        self.difficulty = sec.safe_choice(
            sec.safe_str(data.get("diff"), 16), DIFFICULTIES, "normal")
        self.status = "Joined %s" % self.host_name

    def _roster(self, data):
        for entry in sec.safe_list(data.get("players"), 8):
            if not isinstance(entry, dict):
                continue
            player_id = sec.safe_int(entry.get("id"), 0, 99, 0)
            if not player_id or player_id == self.player_id:
                continue
            remote = self.remotes.setdefault(player_id, Remote(player_id))
            remote.name = _safe_name(entry.get("name"))

    # -- shared ------------------------------------------------------------

    @staticmethod
    def _place(remote, data):
        """Move a remote player. Clamped, because this came off a socket.

        An unclamped coordinate is not a cheat so much as a crash: it lands
        in a camera transform and a blit, and neither enjoys 1e18.
        """
        remote.x = sec.safe_float(data.get("x"), -1e6, 1e6, remote.x)
        remote.y = sec.safe_float(data.get("y"), -1e6, 1e6, remote.y)
        facing = sec.safe_str(data.get("f"), 8)
        if facing in ("front", "back", "left", "right"):
            remote.facing = facing
        remote.walking = bool(data.get("w"))
        remote.in_battle = bool(data.get("b"))
        remote.room = _safe_room(data.get("r"))
        remote.last_seen = time.monotonic()
