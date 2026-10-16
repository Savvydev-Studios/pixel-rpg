"""Headless integration smoke test.

Drives a real Game instance through every screen and several battles,
rendering each frame to a dummy display. This catches the whole class of bugs
that only appear once the modules are wired together.
"""
import os
import sys
import tempfile
import time
import traceback

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpgsmoke")
os.environ["PIXEL_RPG_SEED"] = "1234"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The relay server is a tool rather than part of the game, and one
# smoke check runs a real one (s85).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))

import pygame

from pixel_rpg import game as G
from pixel_rpg.battlescreen import confirm_top
from pixel_rpg.entities import BOSS_DEFS, Boss, make_enemy

FAILURES = []
# Counted as checks run, not written down: a hand-kept total drifts, and a
# check quietly dropped from the list at the bottom of this file would
# still have reported a clean run against the old number.
TOTAL = 0


def key(code, unicode_char="", mod=0):
    return pygame.event.Event(pygame.KEYDOWN, key=code,
                              unicode=unicode_char, mod=mod)


def step(g, frames=1, dt=1 / 60):
    for _ in range(frames):
        g.update(dt)
        g.draw()


class FakePad:
    """A controller with nothing behind it, for driving Gamepad headlessly."""

    def __init__(self, axes=None, buttons=None, name="Smoke Pad"):
        self.axes = axes or {}
        self.buttons = buttons or {}
        self.name = name

    def get_axis(self, axis):
        return self.axes.get(axis, 0)

    def get_button(self, button):
        return self.buttons.get(button, 0)


def clear_story(g, limit=8):
    """Answer whatever narrative screen is up, dialogue or scene.

    A scene takes two presses - one to answer, one to read what is said back
    - so this loops until the story lets go rather than assuming a count.
    """
    for _ in range(limit):
        if g.state in (G.DIALOGUE, G.SCENE):
            g.handle_event(key(pygame.K_RETURN))
            step(g, 2)
        else:
            break


class BridgeSocket:
    """A JS `WebSocket` stand-in that is a real WebSocket on a real socket.

    `BrowserClient` (section 32) talks to `window.WebSocket`, which does not
    exist off a page - so it has only ever been driven by a written-out fake
    in the unit tests, and section 37 records the honest consequence:
    **co-op from a browser had never met a real host.**

    This closes most of that. It presents the four methods and four callbacks
    a browser's `WebSocket` offers, and implements them over `net.WebClient` -
    so everything underneath is real: a real socket, a real RFC 6455
    handshake, and a real `Host` at the other end that has no idea it is
    being tested.

    **The one compromise, named because it is easy to miss:** the host's bytes
    arrive already decoded, because `_Peer.read` decodes as it unframes. They
    are re-encoded with `encode_body` before reaching `onmessage`. It is the
    same body either way - `encode_body` is deterministic - but it is a round
    trip rather than a passthrough, so this proves the conversation rather
    than the exact bytes. The exact bytes are section 25's job and it does
    them against the same host.

    What is still untested after this is `net._bind`, the one line that hands
    a Python callback to JavaScript. Section 32 says so and it is still true.
    """

    def __init__(self, url):
        from pixel_rpg import net
        address = url.split("://", 1)[-1].split("/", 1)[0]
        host, port = address.rsplit(":", 1)
        self._client = net.WebClient(host, int(port))
        self._opened = False
        self.closed = False
        self.onopen = self.onmessage = self.onclose = self.onerror = None

    # -- the half a browser provides ---------------------------------------

    def send(self, text):
        self._client.peer.send_message(text.encode("utf-8"))

    def close(self):
        self.closed = True
        self._client.close()

    # -- the half a page's event loop would do for us ----------------------

    def pump(self):
        """Advance the real socket and fire the callbacks a browser would.

        A page does this on its own event loop. Here the test does it, which
        is the same thing a frame later.
        """
        from pixel_rpg import net
        if self.closed:
            return
        messages = self._client.pump()
        if not self._opened and self._client.ready:
            self._opened = True
            if self.onopen:
                self.onopen(None)
        for kind, data in messages:
            if self.onmessage:
                body = net.encode_body(kind, data).decode("utf-8")
                self.onmessage(type("Event", (), {"data": body})())
        if not self._client.connected and self.onclose:
            self.closed = True
            self.onclose(None)


class BridgeWindow:
    """`window`, with the one attribute `BrowserClient` reaches for."""

    def __init__(self):
        self.last = None
        window = self

        class WebSocket:
            @staticmethod
            def new(url):
                window.last = BridgeSocket(url)
                return window.last

        self.WebSocket = WebSocket


def a_browser_guest_joins_a_real_room():
    """The check section 37 said did not exist.

    A real `Host` on a real loopback socket, and a real `coop.Session`
    driving a real `BrowserClient` into it. The host is not told which kind
    of guest it has - it sniffs `GET ` on the first four bytes and upgrades
    (section 24), which is exactly what a browser sends.
    """
    from pixel_rpg import coop as coop_mod, net

    host = G.Game()
    host.new_run("Hostess", "warden")
    host.start_hosting(reach_out=False, port=0)
    assert host.coop is not None, "hosting failed"

    # A real guest `Game` too, for the same reason `_room` uses one:
    # `Session.update` is handed the game and reaches into it - the
    # world seed, the player it broadcasts - so a stand-in would be
    # testing a different thing.
    guest_game = G.Game()
    guest_game.new_run("WebGuest", "warden")

    address, port = net.decode_room(host.coop.room_code)
    window = BridgeWindow()
    guest = net.BrowserClient(address, port, window=window)

    # Sends made before the socket opened must be held, not lost - that
    # is the whole reason `BrowserClient` keeps a pending list, and the
    # handshake is the first thing said.
    assert not guest.ready, "the socket cannot already be open"
    session = coop_mod.Session(coop_mod.CLIENT, guest, "WebGuest")
    # `coop.PROTOCOL`, not `net.PROTOCOL`. Two constants of the same name
    # in adjacent modules with different values (3 and 1); the host
    # answers the wrong one with "different version of the game", which
    # reads exactly like a real version mismatch.
    session.transport.send("hello", {"protocol": coop_mod.PROTOCOL,
                                     "name": "WebGuest"})
    guest_game.coop = session

    for _ in range(400):
        time.sleep(0.004)
        window.last.pump()
        host.update(1 / 30)
        guest_game.update(1 / 30)
        if session.ready:
            break

    assert guest.reached, "the socket never opened"
    assert session.ready, "the browser guest never got a welcome: %r" % (
        session.error or session.status)
    assert session.player_id, "no player id was assigned"
    assert host.coop.remotes, "the host never saw the guest arrive"
    name = list(host.coop.remotes.values())[0].name
    assert name == "WebGuest", "the host heard the wrong name: %r" % name

    guest_game.leave_coop()
    host.leave_coop()


def a_guest_is_told_whose_valley_it_is():
    """Stepping into somebody else's world is the design (s18).

    Being told nothing about it is why the playtest read it as a save being
    replaced: *"it looks like it started his world save instead of mine"* -
    an unfamiliar map with a hero standing in it.

    The hero is no longer the one you came with (s19 item 5c) - it is the
    one that belongs to this valley - but saying whose valley it is matters
    at least as much now, because the hero is unfamiliar too.
    """
    host, guest = _room()
    try:
        assert guest.coop.host_name, "the guest never learned the host's name"
        assert guest.coop.host_name == host.player.name, (
            "wrong host name: %r" % guest.coop.host_name)
        assert guest.world.seed == host.world.seed, "seeds diverged"
        assert guest.player.name != host.player.name, (
            "the guest should be playing its own character")
        assert guest.guest_world == host.world.seed, (
            "the guest hero was not tied to the valley it is standing in")
        said = " ".join(t.text for t in guest.toasts.items)
        assert host.player.name in said, (
            "nothing on screen said whose valley it is: %r" % said)
    finally:
        for game in (host, guest):
            game.leave_coop()


def a_far_away_guest_does_not_drag_the_host_in():
    """Nobody is pulled into a fight they are nowhere near.

    The school playtest (§40): *"i keep getting dragged into battle with my
    friend without even being near him"*, and the other direction did not
    happen. `host_open_fight` added the host to every fight unconditionally
    while everyone else got a proximity test, so a guest anywhere on the map
    pulled the host in, and a host who met a monster only pulled in a guest
    who was genuinely close.
    """
    host, guest = _room()
    try:
        # Put them far apart, and let both ends hear about it.
        guest.player.x = host.player.x + host.tile * 60
        guest.player.y = host.player.y + host.tile * 60
        # **A teleport is not a step, and must not roll an encounter.**
        # `check_encounter` fires on a change of tile, so moving the guest
        # sixty tiles looks like one enormous stride: it rolls, asks the
        # host for a shared fight, and the host - still holding the last
        # position it was *told*, which was adjacent - correctly opens
        # one. A real fight, on the information available, and nothing to
        # do with what this check is about. Telling the guest it has
        # already been on this tile suppresses exactly that one roll.
        #
        # It only started happening on the bigger valley (s42), where
        # sixty tiles away is somewhere rather than off the edge.
        guest.last_tile = guest.player_tile()
        _pump(host, guest, 40)

        opened = host.host_open_fight(
            4, "meadow",
            guest.player.x / host.tile, guest.player.y / host.tile,
            trigger=2)
        assert opened is False, "the host joined a fight 60 tiles away"
        assert host.battle is None, "the host was dragged into it anyway"

        # And the other way: a fight where they *are* together is offered,
        # and shares once it is accepted. **Offered, not forced** - that is
        # the 2026-09-01 half of the same complaint (s45), and this check
        # is the one that proves proximity still *means* something: it is
        # the difference between being asked and not being asked.
        _stand_together(host, guest)
        _fight_together(host, guest)
        assert host.battle is not None and len(host.battle.players) == 2, (
            "standing together and saying yes should have made a party of "
            "two, got %s" % (host.battle and len(host.battle.players)))
    finally:
        for game in (host, guest):
            game.battle = None
            game.leave_coop()


def a_declined_fight_is_handed_back():
    """A guest exploring alone must still meet monsters.

    The host declining is not the same as the encounter never happening -
    without this a player who walks off on their own wanders through an
    empty world.
    """
    host, guest = _room()
    try:
        guest.player.x = host.player.x + host.tile * 60
        guest.player.y = host.player.y + host.tile * 60
        # **A teleport is not a step, and must not roll an encounter.**
        # `check_encounter` fires on a change of tile, so moving the guest
        # sixty tiles looks like one enormous stride: it rolls, asks the
        # host for a shared fight, and the host - still holding the last
        # position it was *told*, which was adjacent - correctly opens
        # one. A real fight, on the information available, and nothing to
        # do with what this check is about. Telling the guest it has
        # already been on this tile suppresses exactly that one roll.
        #
        # It only started happening on the bigger valley (s42), where
        # sixty tiles away is somewhere rather than off the edge.
        guest.last_tile = guest.player_tile()
        _pump(host, guest, 40)
        assert guest.battle is None
        guest.coop.request_fight(4, "meadow")
        _pump(host, guest, 60)
        assert guest.battle is not None, "the guest never got its fight"
        assert len(guest.battle.players) == 1, "it should be theirs alone"
        assert host.battle is None, "the host was pulled in after all"
    finally:
        for game in (host, guest):
            game.battle = None
            game.leave_coop()


def two_players_share_one_front_room():
    """Co-op interiors (s57), end to end on a real socket.

    §20 recorded the limitation this removes: *"two players who walk
    through the same door are in two private copies of the same room and
    cannot see each other - which is a real limitation, not a decision"*.

    The fix is not a system. A room is built from the world seed and the
    door's coordinates, and both ends already share the seed - so naming
    the door is the whole of the sync, and the only thing that had to
    change was that proximity stops meaning anything between two worlds.
    """
    host, guest = _room()
    try:
        door = next(pos for pos, kind in host.world.doors.items()
                    if kind not in ("shop", "forge", "out"))
        _stand_together(host, guest)
        _pump(host, guest, 30)
        assert host.coop.here(), "they could not see each other outdoors"

        # One of them goes in. **A room is somewhere the other one is not**
        # until they follow, even though the room is painted into the middle
        # of the grid and the coordinates now look adjacent.
        host.enter_house(door)
        _pump(host, guest, 30)
        assert host.house is not None, "the host never got inside"
        assert not host.coop.here(), (
            "the guest was visible from inside a house they never entered")
        assert not guest.coop.here(), (
            "somebody indoors was still standing in the guest's field")

        # And now the guest follows them in through the same door.
        guest.enter_house(door)
        _pump(host, guest, 30)
        assert guest.world.seed == host.world.seed, (
            "the same door built two different rooms")
        assert host.coop.here() and guest.coop.here(), (
            "they walked through the same door and could not see each other")

        # Back out onto the step, and back into a shared valley.
        host.leave_house()
        guest.leave_house()
        _pump(host, guest, 30)
        assert host.house is None and guest.house is None
        assert host.coop.here() and guest.coop.here(), (
            "coming back outside lost them each other")
    finally:
        for game in (host, guest):
            game.house = None
            game.leave_coop()


def a_long_night_plays_to_a_score():
    """The Long Night, end to end, on a real `Game` (items 0ak, 0ay, s76).

    **The one thing a unit test cannot prove**: that a free-roam fight
    survives the *frame*. The simulation is exercised directly elsewhere;
    this drives `Game.update` and `Game.draw` for real, which is the path
    where a missing camera method, a wrong helper name or a draw that
    raises actually lives - and three of those four were what the first
    run of this found.
    """
    game = G.Game()
    game.pending_class = "warden"
    assert game.new_night_run("Nightwalker"), "the mode would not start"
    assert game.state == G.ROAM, "a Long Night did not open its own screen"
    assert game.slot == 0, "a Long Night is holding a save slot"
    assert game.roam is not None and not game.roam.foes, (
        "a night opened with a crowd already on top of the player")

    seen_a_monster = False
    for _ in range(3600):
        game.update(1 / 60)
        game.draw()
        seen_a_monster = seen_a_monster or bool(game.roam
                                                and game.roam.foes)
        if game.state != G.ROAM:
            break
    assert seen_a_monster, "nothing ever turned up"
    assert game.state == G.WAVE_OVER, (
        "standing still all night did not end it: %s" % game.state)
    assert game.zombies.over, "the run never closed"
    record = game.meta.runs[-1]
    assert record.mode == "zombies", (
        "the score went onto the wrong board: %r" % record.mode)
    return True


def a_warden_is_fought_by_the_whole_room():
    """Decision 4, at last (s81): *"party-wide, but everyone must be
    present before it starts"*.

    Warden arenas have been solo-only since §21, and §31.9 has carried
    that as a thing that looks like a bug and is not - because his
    answer was recorded on 2026-08-24 and never built.

    **Two halves, and the second is the one he actually specified:** the
    room fights it together, and it does not start until the room is
    together.
    """
    from pixel_rpg.entities import BOSS_DEFS
    host = G.Game()
    host.pending_class = "warden"
    host.new_run("Hostess", 1)
    host.start_hosting(reach_out=False, port=0)
    assert host.coop is not None, "hosting failed"

    guest = G.Game()
    guest.pending_class = "duelist"
    guest.code_buffer = host.coop.room_code
    assert guest.try_join(), "joining failed"
    _pump(host, guest, 120)
    try:
        if guest.state == G.CLASS_SELECT:
            guest.name_buffer = "Guest"
            guest.begin_run()
            _pump(host, guest, 90)
        # Somewhere else on the island: the room is not assembled.
        guest.player.x = host.player.x + host.tile * 40
        guest.player.y = host.player.y
        _pump(host, guest, 60)
        assert host.wardens_missing(), (
            "a warden would have opened with somebody across the map")

        # Now standing together.
        _stand_together(host, guest)
        assert not host.wardens_missing(), (
            "the room is together and the host still says it is not: %s"
            % host.wardens_missing())
        assert host.warden_party_size() == 2, (
            "a warden sized for %d in a room of two"
            % host.warden_party_size())

        host.open_warden(BOSS_DEFS[0])
        _pump(host, guest, 90)
        assert host.battle is not None, "the host is not fighting it"
        assert guest.battle is not None, "the guest never joined the warden"
        assert len(host.battle.players) == 2, (
            "the host is fighting it alone: %d" % len(host.battle.players))
        theirs = [e.name for e in host.battle.enemies]
        ours = [e.name for e in guest.battle.enemies]
        assert theirs == ours, (
            "the two ends are fighting different things: %s vs %s"
            % (theirs, ours))

        # **And it counts, for each of them** (s96). The shared payout
        # never called `on_boss_defeated`: a warden a room felled was
        # nobody's - no portal marked, no Memory, the next road sealed -
        # and a room cannot fight one alone, so co-op stopped at the first
        # warden. Played to the end here because only the end shows it.
        for monster in host.battle.enemies:
            monster.hp = 1
        _drive_shared(host, guest)
        for _ in range(12):
            for game in (host, guest):
                if game.state in (G.CHAPTER_CARD, G.SCENE, G.DIALOGUE):
                    game.handle_event(key(pygame.K_RETURN))
            _pump(host, guest, 6)
        for name, game in (("host", host), ("guest", guest)):
            assert game.world.portals[0].defeated, (
                "%s: the warden the room felled was nobody's" % name)
            assert game.world.portals[1].unlocked, (
                "%s: the next road stayed sealed" % name)
            assert len(game.player.relic_objects()) == 1, (
                "%s never got the Memory" % name)
            assert game.player.chapter_seen == 2, (
                "%s: the chapter never turned" % name)
    finally:
        for game in (host, guest):
            game.battle = None
            game.leave_coop()
    return True


def a_host_that_leaves_ends_the_room():
    """s96: a guest only noticed a dead socket before the welcome, so a
    host who left mid-game left the guest in a room with nobody in it -
    and a guest mid-fight in a fight that could never end.

    Over a real socket because that is where "the socket closed" is a
    fact rather than a stub's say-so: the guest is fighting beside the
    host with a run of their own saved, the host goes, and the guest
    must come home to that run - out of the fight, in story mode, able
    to save.
    """
    from pixel_rpg.entities import BOSS_DEFS
    host = G.Game()
    host.pending_class = "warden"
    host.new_run("Hostess", 1)
    host.start_hosting(reach_out=False, port=0)
    assert host.coop is not None, "hosting failed"

    guest = G.Game()
    guest.pending_class = "duelist"
    guest.new_run("Homebody", 3)
    guest.dialogue = None
    guest.scene = None
    assert guest.save_run(force=True), "the guest's own run did not save"
    guest.code_buffer = host.coop.room_code
    assert guest.try_join(), "joining failed"
    _pump(host, guest, 120)
    try:
        if guest.state == G.CLASS_SELECT:
            guest.name_buffer = "Guest"
            guest.begin_run()
            _pump(host, guest, 90)
        assert guest.guest_world is not None, "the guest never arrived"
        _stand_together(host, guest)
        host.open_warden(BOSS_DEFS[0])
        _pump(host, guest, 90)
        assert guest.battle is not None and guest.battle.mirror, (
            "the guest is not in the host's fight")

        host.battle = None
        host.leave_coop()
        started = time.monotonic()
        while guest.coop is not None and time.monotonic() - started < 5:
            guest.update(1 / 30)
            time.sleep(0.01)
        assert guest.coop is None, "the guest never noticed the host left"
        assert guest.battle is None, "the guest was left in the host's fight"
        assert guest.guest_world is None, "the guest is still a guest"
        assert guest.player is not None and guest.player.name == "Homebody", (
            "the guest did not come home: %r"
            % (guest.player and guest.player.name))
        assert guest.mode == "story" and guest.state == G.OVERWORLD, (
            guest.mode, guest.state)
        assert guest.save_run(silent=True, force=True), (
            "home, and it could not be saved")
    finally:
        for game in (host, guest):
            game.battle = None
            game.leave_coop()
        guest.saves.delete(3)
    return True


def a_long_night_runs_in_a_room():
    """Free-roam co-op, end to end on a real socket (item 0bg, s77).

    **The three things only a socket can prove**, and every one of them
    was a real bug in a shared turn-based fight first (§21, §40, §50):

    1. the guest ends up in the arena at all;
    2. **both machines see the same monsters** - a client that spawned
       its own would fight a different room;
    3. a guest's keys reach the host, so the person playing is driving
       their own body rather than watching it stand still.
    """
    host = G.Game()
    host.pending_class = "warden"
    host.new_night_run("Hostess")
    host.start_hosting(reach_out=False, port=0)
    assert host.coop is not None, "hosting a long night failed"

    guest = G.Game()
    guest.pending_class = "duelist"
    guest.code_buffer = host.coop.room_code
    assert guest.try_join(), "joining failed"
    _pump(host, guest, 120)
    try:
        assert guest.coop.mode == "zombies", (
            "the room never said which game it was: %r" % guest.coop.mode)
        # **A guest makes a hero for the room**, exactly as a Last Stand
        # guest does and for the same reason: the vault is per *valley*
        # and would outlive a mode that keeps nothing.
        assert guest.state == G.CLASS_SELECT, (
            "a night guest should be making a hero, not %r" % guest.state)
        guest.name_buffer = "Guest"
        guest.begin_run()
        _pump(host, guest, 90)
        assert guest.mode == "zombies", "the guest is not in the mode"
        assert guest.slot == 0, "a night guest was given a save slot"
        assert host.coop.player_count == 2, "the host never saw the guest"

        # The host opens the night the room is already in.
        host.begin_night()
        _pump(host, guest, 90)
        assert guest.roam is not None, "the guest never entered the arena"
        assert guest.roam.mirror, "a guest is simulating the fight itself"
        assert len(guest.roam.heroes) == len(host.roam.heroes) == 2, (
            "the room is not two people: %d / %d"
            % (len(guest.roam.heroes), len(host.roam.heroes)))

        # Let some monsters turn up, then check both ends agree.
        _pump(host, guest, 400)
        assert host.roam.foes, "nothing ever arrived on the host"
        assert len(guest.roam.foes) == len(host.roam.foes), (
            "the two machines see different monsters: %d / %d"
            % (len(guest.roam.foes), len(host.roam.foes)))

        # The guest pushes right; the host has to move that hero.
        mine = guest.roam.viewer
        before = host.roam.heroes[mine].x
        for _ in range(40):
            time.sleep(0.004)
            guest.coop.send_roam_input(1.0, 0.0, force=True)
            host.update(1 / 30)
            guest.update(1 / 30)
        assert host.roam.heroes[mine].x > before + 4, (
            "the guest's keys never reached the host: %.1f -> %.1f"
            % (before, host.roam.heroes[mine].x))

        # **Surviving the night, and being paid for it** (s83, s86).
        # `finish_night` only ever runs on the machine simulating the
        # fight, so before s83 a guest could play an hour of nights and
        # come out at level one - and the fix for that had never been
        # driven over a socket, which is the state the bug was in.
        from pixel_rpg import gamemodes
        owed = (guest.player.gold, guest.player.xp, guest.zombies.cleared)
        host.zombies.spawned = gamemodes.night_count(host.zombies.wave)
        host.roam.foes = []
        _pump(host, guest, 20)
        assert host.state == G.SPOILS, (
            "the host survived a night and was offered nothing: %r"
            % host.state)
        # **Every offer, not one** (item 0bz): a night hands out the same
        # three a Last Stand does now, and the next night opens when the
        # last of them is answered.
        guard = 0
        while host.state == G.SPOILS and guard < 8:
            host.take_spoil(0)
            guard += 1
        _pump(host, guest, 120)

        assert guest.zombies.cleared >= 1, (
            "the guest survived a night and it was never counted")
        assert (guest.player.gold, guest.player.xp) != owed[:2], (
            "the guest survived a night and was paid nothing at all")
        assert guest.state in (G.SPOILS, G.ROAM), (
            "the guest ended up somewhere else entirely: %r" % guest.state)
        guard = 0
        while guest.state == G.SPOILS and guard < 8:
            assert guest.spoils, "an offer screen with nothing on it"
            guest.take_spoil(0)
            guard += 1
        _pump(host, guest, 60)
        assert host.zombies.wave == guest.zombies.wave, (
            "the two ends are on different nights: %d / %d"
            % (host.zombies.wave, guest.zombies.wave))
    finally:
        for game in (host, guest):
            game.roam = None
            game.leave_coop()
    return True


def a_hoard_runs_in_a_room():
    """The Hoard in co-op, end to end on a real socket (item 0bv, §87).

    **The three things only a socket can prove**, and each of them is a
    bug that has actually happened in this codebase before:

    1. the guest ends up in the arena at all (§77, the night's version);
    2. **both machines see the same floor** - coins are the host's, and
       a guest that kept its own list would draw money that is not there
       and walk past money that is;
    3. what a guest banks is counted, because the score is the room's.
    """
    host = G.Game()
    host.pending_class = "warden"
    host.new_hoard_run("Hostess")
    host.start_hosting(reach_out=False, port=0)
    assert host.coop is not None, "hosting a hoard failed"

    guest = G.Game()
    guest.pending_class = "duelist"
    guest.code_buffer = host.coop.room_code
    assert guest.try_join(), "joining failed"
    _pump(host, guest, 120)
    try:
        assert guest.coop.mode == "hoard", (
            "the room never said which game it was: %r" % guest.coop.mode)
        assert guest.state == G.CLASS_SELECT, (
            "a hoard guest should be making a hero, not %r" % guest.state)
        guest.name_buffer = "Guest"
        guest.begin_run()
        _pump(host, guest, 90)
        assert guest.mode == "hoard", "the guest is not in the mode"
        assert guest.slot == 0, "a hoard guest was given a save slot"
        assert host.coop.player_count == 2, "the host never saw the guest"

        # The host re-opens the arena now that there are two of them, and
        # the announcement is what puts the guest in it.
        host.begin_hoard()
        _pump(host, guest, 120)
        assert guest.roam is not None, "the guest never entered the arena"
        assert guest.roam.hoard, "the guest is in the wrong kind of fight"
        assert guest.roam.mirror, "a guest is simulating the floor itself"
        assert len(guest.roam.heroes) == len(host.roam.heroes) == 2, (
            "the room is not two people: %d / %d"
            % (len(guest.roam.heroes), len(host.roam.heroes)))

        # **The same floor.** The host scatters; the guest is told.
        _pump(host, guest, 60)
        assert host.roam.loot, "the host's floor is empty"
        # Stop the host adding to it first: a frame is in flight for a
        # tick or two, so comparing while coins are still arriving would
        # be testing the latency rather than the message.
        host.hoard.since_drop = -10_000.0
        _pump(host, guest, 40)
        assert len(guest.roam.loot) == len(host.roam.loot), (
            "the two machines see different floors: %d / %d"
            % (len(guest.roam.loot), len(host.roam.loot)))

        # The guest's clock runs, so its HUD is not frozen at 3:00.
        from pixel_rpg import gamemodes
        assert guest.hoard is not None, "the guest has no run to read"
        assert guest.hoard.left < gamemodes.HOARD_TIME, (
            "the guest's clock never started")

        # **What the guest carries and banks is the host's arithmetic.**
        # Everything that decides happens there; this side is told.
        mine = guest.roam.viewer
        theirs = host.roam.heroes[mine]
        for coin in list(host.roam.loot)[:3]:
            theirs.x, theirs.y = coin.x, coin.y
            host.roam.gather()
        assert theirs.carrying, "the host never gave the guest its coins"
        _pump(host, guest, 30)
        assert guest.roam.heroes[mine].carrying == theirs.carrying, (
            "the guest does not know what it is carrying: %d vs %d"
            % (guest.roam.heroes[mine].carrying, theirs.carrying))

        theirs.x, theirs.y = host.roam.bank
        host.roam.gather()
        _pump(host, guest, 30)
        assert host.roam.banked.get(mine), "banking did nothing"
        assert guest.roam.banked.get(mine) == host.roam.banked.get(mine), (
            "the guest's haul did not cross: %r vs %r"
            % (guest.roam.banked.get(mine), host.roam.banked.get(mine)))
        assert host.hoard.banked >= host.roam.banked.get(mine), (
            "the room's pile does not include the guest's coins")
    finally:
        for game in (host, guest):
            game.roam = None
            game.leave_coop()
    return True


def a_last_stand_runs_in_a_room():
    """Wave mode in co-op (0ag, s59), end to end on a real socket.

    The one thing worth proving over a socket rather than in a unit test:
    **both machines fight the same monsters.** A wave names its own pack
    size, and the first version did not send it - so the guest's mirror
    rolled `pack_size(party)` and got a different fight from the host's,
    which every later frame is indexed against.
    """
    host = G.Game()
    host.pending_class = "warden"
    host.new_wave_run("Hostess")
    host.start_hosting(reach_out=False, port=0)
    assert host.coop is not None, "hosting a last stand failed"

    guest = G.Game()
    guest.pending_class = "duelist"
    guest.code_buffer = host.coop.room_code
    assert guest.try_join(), "joining failed"
    _pump(host, guest, 90)
    try:
        assert guest.coop.mode == "waves", (
            "the room never said which game it was: %r" % guest.coop.mode)
        # A wave guest makes a hero for the room rather than being handed
        # one out of the vault - that vault is per *valley* and would
        # outlive a mode that keeps nothing.
        assert guest.state == G.CLASS_SELECT, (
            "a wave guest should be making a hero, not %r" % guest.state)
        guest.name_buffer = "Guest"
        guest.begin_run()
        _pump(host, guest, 90)
        assert guest.in_waves(), "the guest never entered wave mode"
        assert guest.slot == 0, "a wave guest was given a save slot"

        _pump(host, guest, 40)
        assert host.coop.player_count == 2, "the host never saw the guest"

        assert host.begin_wave(), "the host could not open wave one"
        _pump(host, guest, 90)
        assert host.battle is not None, "the host is not fighting"
        assert guest.battle is not None, "the guest never joined the wave"
        theirs = [e.name for e in host.battle.enemies]
        ours = [e.name for e in guest.battle.enemies]
        assert theirs == ours, (
            "the two ends are fighting different monsters: %s vs %s"
            % (theirs, ours))
        assert len(host.battle.players) == 2, (
            "a wave is everybody in the room, got %d"
            % len(host.battle.players))

        # **The host waits for the room to finish choosing** (s84). The
        # guest opens its offers, tells the room, and the host's button
        # stops being "send in wave 2" until it hears otherwise. Over a
        # real socket because that is the only place the message either
        # arrives or does not.
        assert host.waves_waiting_for() == [], (
            "the host is waiting for somebody before anybody chose: %r"
            % host.waves_waiting_for())
        guest.coop.say_choosing(True)
        _pump(host, guest, 30)
        assert host.waves_waiting_for(), (
            "the guest is reading its cards and the host never heard")
        guest.coop.say_choosing(False)
        _pump(host, guest, 30)
        assert host.waves_waiting_for() == [], (
            "the guest finished and the host is still waiting: %r"
            % host.waves_waiting_for())
    finally:
        for game in (host, guest):
            game.battle = None
            game.leave_coop()


def a_room_works_through_a_relay():
    """§85, and the roadmap's *"a relay for CGNAT hosts"*.

    **A real relay, on a real socket, in this process.** The relay is
    `tools/relay_server.py` bound to a loopback port; the host dials out
    to it and parks connections, the guest dials in with the same code,
    and the relay ties them together. Every byte takes the path it would
    take through a relay on the other side of the world - the only thing
    loopback removes is the distance.

    Proving it any other way would mean a mock of the thing being
    tested, which is the argument `WebClient`'s docstring already makes
    about the browser transport.
    """
    import threading
    import relay_server

    server = relay_server.Relay(port=0, bind="127.0.0.1", quiet=True)
    was = os.environ.get("PIXEL_RPG_RELAY_URL", "")
    os.environ["PIXEL_RPG_RELAY_URL"] = "ws://127.0.0.1:%d" % server.port
    stop = []
    def spin():
        while not stop:
            server.tick(0.005)
    thread = threading.Thread(target=spin, daemon=True)
    thread.start()

    host = guest = None
    try:
        host = G.Game()
        host.pending_class = "warden"
        host.new_run("Hostess", 1)
        host.start_hosting(reach_out=False)
        assert host.coop is not None, "hosting through the relay failed"
        assert type(host.coop.transport).__name__ == "RelayHost", (
            "a relay is configured and the host still opened a port: %s"
            % type(host.coop.transport).__name__)
        code = host.coop.room_code
        assert len(code) == 6, "a relay room code is six characters: %r" % code
        assert host.coop.portal is None, (
            "there is no port to map, so there must be no portal")

        guest = G.Game()
        guest.pending_class = "duelist"
        guest.code_buffer = code
        assert guest.try_join(), "joining through the relay failed"
        assert type(guest.coop.transport).__name__ == "RelayClient", (
            "a six-character code opened the wrong door")
        _pump(host, guest, 150)

        assert host.coop.player_count == 2, (
            "the host never saw the guest: %d" % host.coop.player_count)
        assert guest.coop.ready, "the guest never finished the handshake"
        assert guest.coop.seed == host.world.seed, (
            "the world did not cross the relay: %r vs %r"
            % (guest.coop.seed, host.world.seed))

        # And it keeps working: a hero, a roster, and positions moving.
        guest.name_buffer = "Guest"
        guest.begin_run()
        _pump(host, guest, 90)
        assert guest.player is not None, "the guest never made a hero"
        _stand_together(host, guest)
        remotes = list(host.coop.remotes.values())
        assert remotes, "the host has no roster"
        assert remotes[0].x or remotes[0].y, (
            "positions never crossed the relay")

        # **A code nobody is hosting is refused rather than hung.** The
        # relay closes the socket; the guest has to notice.
        stranger = G.Game()
        stranger.code_buffer = "AAAAAA"
        stranger.try_join()
        if stranger.coop is not None:
            for _ in range(60):
                time.sleep(0.004)
                stranger.update(1 / 30)
            # **Either answer is the right one**: the game may have
            # dropped the session already, or it may still be holding one
            # that never became ready. What must not happen is a stranger
            # standing in somebody's room.
            assert stranger.coop is None or not stranger.coop.ready, (
                "a room nobody is hosting let somebody in")
            stranger.leave_coop()
    finally:
        stop.append(True)
        thread.join(timeout=1.0)
        for game in (host, guest):
            if game is not None:
                game.leave_coop()
        server.close()
        if was:
            os.environ["PIXEL_RPG_RELAY_URL"] = was
        else:
            os.environ.pop("PIXEL_RPG_RELAY_URL", None)


def a_hoard_plays_to_a_score(g):
    """Item 0bv, §86: the mode that is not about surviving.

    **Played through the picker**, which is the half that was broken for
    The Long Night: `begin_run` branched on one mode key and everything
    else quietly became a story run, so a mode could be built, tested and
    shipped with no way into it. This walks the route a player walks.
    """
    from pixel_rpg import gamemodes

    game = G.Game()
    game.screen = pygame.display.set_mode((1280, 720))
    game.choose_mode("hoard")
    game.pending_class = "warden"
    game.name_buffer = "Greedy"
    game.begin_run()
    for _ in range(90):
        game.update(1 / 30)

    assert game.mode == "hoard", (
        "picking The Hoard started %r instead" % game.mode)
    assert game.state == G.ROAM, "the arena never opened: %r" % game.state
    assert game.roam is not None and game.roam.hoard
    assert game.roam.loot, "the floor was empty when it opened"
    assert game.slot == 0, "a hoard was given a save slot"
    assert not game.save_run(silent=True), "a hoard wrote a save"

    # **Nobody can be walled out of the camp.** The arenas were drawn
    # before this mode existed and their cover goes where the plan says.
    import math
    from pixel_rpg import freeroam as fr
    for bx, by, radius in game.roam.blockers:
        gap = math.hypot(bx - game.roam.bank[0], by - game.roam.bank[1])
        assert gap >= fr.BANK_REACH + radius, "there is cover in the camp"

    # Now play it: out for coins, back to the fire, for a while.
    hero = game.roam.heroes[0]
    trips = 0
    for step in range(900):
        game.update(1 / 30)
        if game.state != G.ROAM:
            break
        if hero.dead:
            continue
        if hero.carrying >= 4 or not game.roam.loot:
            if hero.carrying:
                trips += 1
            hero.x, hero.y = game.roam.bank
        else:
            coin = game.roam.loot[0]
            hero.x, hero.y = coin.x, coin.y
    assert trips, "never got a load home"
    assert game.hoard.banked > 0, "nothing was ever banked"
    assert game.hoard.left < gamemodes.HOARD_TIME, "the clock never moved"

    # **Falling costs the load, not the run.**
    hero.carrying = 5
    before = len(game.roam.loot)
    hero.who.hp = 0
    hero.dead = True
    game.roam.spill(hero)
    assert hero.carrying == 0
    assert len(game.roam.loot) == before + 5, "the load did not hit the floor"
    hero.down = 0.01
    game.update(1 / 30)
    game.update(1 / 30)
    assert not hero.dead, "nobody got back up"
    assert game.state == G.ROAM, (
        "a fall ended a hoard run: %r" % game.state)

    # And the clock is what ends it.
    banked = game.hoard.banked
    game.hoard.left = 0.01
    game.update(1 / 30)
    assert game.state == G.WAVE_OVER, (
        "the clock ran out and the run went on: %r" % game.state)
    assert game.last_score > 0, "a run that banked %d scored nothing" % banked
    # **Any hoard record, not the first one.** The profile is shared
    # across every check in this file, so `runs[0]` is whatever ran
    # first - which is a different game entirely.
    assert any(record.mode == "hoard" for record in game.meta.runs), (
        "the run left no record: %r"
        % [record.mode for record in game.meta.runs][:6])


def every_mode_starts_in_itself(g):
    """§86, and the bug that made this check exist.

    **Choosing The Long Night started a story run** - `begin_run` read
    one mode key and every other fell through to `new_run`. It had been
    that way since the mode was added, because every test called
    `new_night_run` directly and nothing walked the picker.
    """
    from pixel_rpg import gamemodes

    for mode in gamemodes.MODES:
        if mode.saves:
            continue
        game = G.Game()
        game.screen = pygame.display.set_mode((1280, 720))
        game.choose_mode(mode.key)
        game.pending_class = "warden"
        game.name_buffer = "Router"
        game.begin_run()
        for _ in range(120):
            game.update(1 / 30)
        assert game.mode == mode.key, (
            "picking %s started %r" % (mode.key, game.mode))
        assert game.state != G.SCENE, (
            "%s opened on a story scene" % mode.key)
        assert game.slot == 0, "%s took a save slot" % mode.key


def check(label, fn):
    global TOTAL
    TOTAL += 1
    try:
        fn()
        print("  ok   %s" % label)
    except Exception as exc:
        FAILURES.append((label, exc))
        print("  FAIL %s -> %s: %s" % (label, type(exc).__name__, exc))
        traceback.print_exc()


class FakeKeys:
    def __init__(self, pressed):
        self.pressed = pressed

    def __getitem__(self, code):
        return self.pressed.get(code, False)


def new_game(g):
    g.state = G.TITLE
    g.title_index = 0
    g.handle_event(key(pygame.K_RETURN))            # New Game
    step(g, 2)
    # **Which game, first** (0ag, s59). New Game asks the mode before it
    # asks the slot, because a mode reachable only from the co-op flow is
    # one most people would never find.
    assert g.state == G.MODE_SELECT, g.state
    g.ui.mode_index = 0                             # the long road
    g.handle_event(key(pygame.K_RETURN))
    step(g, 2)
    assert g.state == G.SLOTS, g.state
    g.handle_event(key(pygame.K_RETURN))            # slot 1
    step(g, 2)
    assert g.state == G.CLASS_SELECT, g.state

    # Browse to a locked class, be refused, then take a starter class.
    from pixel_rpg import classes as classes_mod
    locked = next(i for i, c in enumerate(classes_mod.CLASSES)
                  if not classes_mod.is_unlocked(c.key, g.meta))
    g.ui.class_index = locked
    g.handle_event(key(pygame.K_RETURN))
    step(g, 2)
    assert g.state == G.CLASS_SELECT, "a locked class was accepted"
    g.ui.class_index = 1                            # Emberwright
    g.handle_event(key(pygame.K_RETURN))
    step(g, 2)
    assert g.state == G.MODIFIER_SELECT, g.state

    # **Row 0 is the difficulty now** (2026-09-01), so the modifiers start
    # at 1 and Begin is one further down than it was:
    #
    # > "when creating a new save you're never granted with the difficulty
    # > option itself, whether the save is on hard, normal or easy mode"
    from pixel_rpg.modifiers import MODIFIERS
    g.ui.modifier_index = 0
    difficulty_before = g.settings.difficulty
    g.handle_event(key(pygame.K_RETURN))
    step(g, 1)
    assert g.settings.difficulty != difficulty_before, \
        "the difficulty row did nothing"
    g.settings.difficulty = difficulty_before

    # Toggle two modifiers on, one back off, then begin.
    for index in (1, 2, 2):
        g.ui.modifier_index = index
        g.handle_event(key(pygame.K_RETURN))
        step(g, 1)
    assert g.pending_modifiers.keys == [MODIFIERS[0].key], \
        g.pending_modifiers.keys
    g.ui.modifier_index = len(MODIFIERS) + 1        # the Begin row
    g.handle_event(key(pygame.K_RETURN))
    step(g, 2)
    assert g.state == G.NAME_ENTRY, g.state

    # Character creation: type a name, then change every look field.
    for ch in "Tester":
        g.handle_event(key(ord(ch.lower()), ch))
    from pixel_rpg import menus as menus_mod
    before = g.pending_appearance.signature()
    for label in ("Skin", "Hair", "Style", "Outfit"):
        g.ui.creator_index = menus_mod.CREATOR_ROWS.index(label)
        g.handle_event(key(pygame.K_RIGHT))
        step(g, 1)
    assert g.pending_appearance.signature() != before, "look never changed"
    g.ui.creator_index = menus_mod.CREATOR_ROWS.index("Surprise me")
    g.handle_event(key(pygame.K_RETURN))
    step(g, 1)
    chosen_look = g.pending_appearance.signature()
    g.ui.creator_index = menus_mod.CREATOR_ROWS.index("Begin")
    g.handle_event(key(pygame.K_RETURN))
    step(g, 60)                                     # let the fade finish
    clear_story(g)
    assert g.state == G.OVERWORLD, g.state
    assert g.player.name == "Tester", g.player.name
    assert g.player.class_key == "emberwright", g.player.class_key
    assert g.player.appearance.signature() == chosen_look, "look was lost"
    assert g.player.modifiers.keys == [MODIFIERS[0].key]


def walk(g):
    start = (g.player.x, g.player.y)
    real = pygame.key.get_pressed
    pygame.key.get_pressed = lambda: FakeKeys({pygame.K_d: True})
    try:
        step(g, 90)
    finally:
        pygame.key.get_pressed = real
    assert (g.player.x, g.player.y) != start, "player did not move"


def screen(g, state, opener):
    g.state = G.OVERWORLD
    g.handle_event(key(opener))
    step(g, 3)
    assert g.state == state, (state, g.state)
    for code in (pygame.K_DOWN, pygame.K_UP, pygame.K_TAB, pygame.K_RETURN,
                 pygame.K_DOWN, pygame.K_RETURN, pygame.K_u, pygame.K_x):
        g.handle_event(key(code))
        step(g, 1)
    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 1)
    assert g.state == G.OVERWORLD, g.state


def shop(g):
    g.state = G.OVERWORLD
    g.ui.refresh_shop(g.player, g.world)
    g.go(G.SHOP)
    g.player.gold = 99999
    step(g, 2)
    for code in (pygame.K_DOWN, pygame.K_RETURN, pygame.K_DOWN, pygame.K_RETURN,
                 pygame.K_TAB, pygame.K_DOWN, pygame.K_RETURN):
        g.handle_event(key(code))
        step(g, 1)
    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 1)


def smith(g):
    """Salvage spare gear into Ember Steel, then spend it on a refinement."""
    from pixel_rpg.items import roll_item
    from pixel_rpg import menus as menus_mod

    g.state = G.OVERWORLD
    if g.player.equipment["weapon"] is None:
        spare = roll_item(3, "weapon")
        g.player.add_item(spare)
        g.player.equip(spare)
    for _ in range(4):
        g.player.add_item(roll_item(8))
    g.player.gold = 99999
    g.player.materials = 0
    g.go(G.SMITH)
    step(g, 2)

    # Refining with no steel must be refused.
    before = g.player.equipment["weapon"].name
    g.ui.smith_mode = "refine"
    g.ui.smith.index = 0
    g.handle_event(key(pygame.K_RETURN))
    step(g, 2)
    assert g.player.equipment["weapon"].name == before, \
        "refined without any Ember Steel"

    # Salvage a spare piece for steel.
    g.handle_event(key(pygame.K_TAB))
    step(g, 2)
    assert g.ui.smith_mode == "salvage", g.ui.smith_mode
    rows = menus_mod.smith_rows(g)
    assert rows, "nothing available to salvage"
    bag_before = len(g.player.inventory)
    g.handle_event(key(pygame.K_RETURN))
    step(g, 2)
    assert g.player.materials > 0, "salvaging produced no steel"
    assert len(g.player.inventory) == bag_before - 1, "the item was not consumed"

    # Give it enough steel to actually refine, then do it.
    g.player.materials += 200
    g.handle_event(key(pygame.K_TAB))
    step(g, 2)
    g.ui.smith.index = 0
    steel_before = g.player.materials
    g.handle_event(key(pygame.K_RETURN))
    step(g, 2)
    assert g.player.equipment["weapon"].name != before, "no refinement happened"
    assert g.player.materials < steel_before, "refining consumed no steel"

    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 1)


def pause_settings(g):
    g.state = G.OVERWORLD
    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 2)
    assert g.state == G.PAUSE, g.state
    from pixel_rpg import menus
    settings_row = menus.PAUSE_ITEMS.index("Settings")
    for _ in range(settings_row):
        g.handle_event(key(pygame.K_DOWN))
        step(g, 1)
    g.handle_event(key(pygame.K_RETURN))
    step(g, 1)
    assert g.state == G.SETTINGS_SCREEN, g.state
    from pixel_rpg import menus
    for _ in range(len(menus.SETTING_ROWS)):
        g.handle_event(key(pygame.K_RIGHT))
        g.handle_event(key(pygame.K_LEFT))
        g.handle_event(key(pygame.K_DOWN))
        step(g, 1)
    g.state = G.OVERWORLD


def drive_battle(g, picker, limit=8000):
    guard = 0
    while g.state == G.BATTLE and guard < limit:
        guard += 1
        step(g, 1)
        b = g.battle
        if b is None:
            break
        if b.phase == "menu":
            picker(g, b)
        elif b.phase == "submenu":
            g.handle_event(key(pygame.K_DOWN))
            g.handle_event(key(pygame.K_RETURN))
        elif b.finished:
            g.handle_event(key(pygame.K_RETURN))
    assert g.state != G.BATTLE, "battle never resolved"


def fight(g):
    g.state = G.OVERWORLD
    g.player.full_restore()
    g.start_battle(make_enemy(3, "meadow", g.settings.mods))
    drive_battle(g, lambda g_, b: g_.handle_event(key(pygame.K_1)))


def skills_items(g):
    g.state = G.OVERWORLD
    # This scenario is about the two submenus, so drop any run modifier that
    # would legitimately disable one of them.
    from pixel_rpg.modifiers import RunModifiers
    g.player.modifiers = RunModifiers()
    g.player.gain_xp(9000)
    g.player.add_consumable("potion", 5)
    g.player.add_consumable("bomb", 3)
    g.player.full_restore()
    g.start_battle(make_enemy(6, "forest", g.settings.mods))
    used = {"skill": False, "item": False}

    def pick(g_, b):
        if not used["skill"]:
            used["skill"] = True
            g_.handle_event(key(pygame.K_2))
        elif not used["item"]:
            used["item"] = True
            g_.handle_event(key(pygame.K_3))
        else:
            g_.handle_event(key(pygame.K_1))
    drive_battle(g, pick)
    assert used["skill"] and used["item"], "submenus not exercised"


def boss(g):
    g.state = G.OVERWORLD
    g.player.gain_xp(200000)
    g.player.full_restore()
    g.start_battle(Boss(BOSS_DEFS[0], g.settings.mods))
    drive_battle(g, lambda g_, b: g_.handle_event(key(pygame.K_1)))
    assert g.world.portals[0].defeated or g.state == G.GAMEOVER


def death(g):
    g.state = G.OVERWORLD
    g.player.hp = 1
    g.start_battle(Boss(BOSS_DEFS[5], g.settings.mods))
    drive_battle(g, lambda g_, b: g_.handle_event(key(pygame.K_1)))
    if g.state == G.GAMEOVER:
        g.handle_event(key(pygame.K_RETURN))
        step(g, 60)
    assert g.state == G.OVERWORLD, g.state


def chest_shrine(g):
    g.state = G.OVERWORLD
    chest = next((c for c in g.world.chests if not c.opened), None)
    assert chest is not None, "no unopened chest in the world"
    g.open_chest(chest)
    g.player.hp = 1
    g.player.gold = 500
    g.use_shrine()
    assert g.player.hp == g.player.max_hp()
    step(g, 3)


def dialogue(g):
    g.state = G.OVERWORLD
    g.world.portals[1].unlocked = True
    g.enter_portal(g.world.portals[1])
    step(g, 2)
    assert g.state == G.DIALOGUE, g.state
    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 2)
    assert g.state == G.OVERWORLD


def save_load(g):
    g.state = G.OVERWORLD
    g.player.appearance.skin = 6
    g.player.appearance.hair = 8
    g.player.appearance.style = 4
    look = g.player.appearance.signature()
    assert g.save_run(force=True), g.saves.last_error
    level, gold = g.player.level, g.player.gold
    assert g.load_run(g.slot), g.saves.last_error
    assert g.player.level == level, (g.player.level, level)
    assert g.player.gold == gold, (g.player.gold, gold)
    assert g.player.appearance.signature() == look, "appearance did not persist"
    step(g, 3)


def codex_screen(g):
    from pixel_rpg import story
    g.state = G.OVERWORLD
    for entry in story.CODEX[:6]:
        g.meta.unlock_codex(entry.key)
    g.go(G.CODEX)
    step(g, 2)
    for code in (pygame.K_TAB, pygame.K_DOWN, pygame.K_DOWN, pygame.K_TAB):
        g.handle_event(key(code))
        step(g, 1)
    g.ui.codex_tab = 0
    g.ui.codex.index = 0
    g.handle_event(key(pygame.K_RETURN))            # open a page
    step(g, 2)
    assert g.reading is not None, "codex page did not open"
    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 1)
    assert g.reading is None
    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 1)


def memory_drop(g):
    """Killing a warden must hand over its Memory and its codex page."""
    from pixel_rpg import story
    g.state = G.OVERWORLD
    lore = story.WARDENS[0]
    g.player.relics = [r for r in g.player.relics if r != lore.memory_key]
    portal = next(p for p in g.world.portals if p.boss_key == lore.boss_key)
    portal.defeated = False
    g.player.gain_xp(400000)
    g.player.full_restore()
    g.start_battle(Boss(BOSS_DEFS[0], g.settings.mods))
    drive_battle(g, lambda g_, b: g_.handle_event(key(pygame.K_1)))
    clear_story(g)
    assert lore.memory_key in g.player.relics, "Memory was not granted"
    assert g.meta.knows(lore.memory_key), "codex page was not unlocked"


def ending_flow(g):
    """All six down, full codex, then walk through an ending to the records."""
    from pixel_rpg import story
    g.state = G.OVERWORLD
    for entry in story.CODEX:
        g.meta.unlock_codex(entry.key)
    for portal in g.world.portals:
        portal.defeated = True
    g.open_ending_choice()
    step(g, 2)
    # The Door asks something before it offers anything.
    assert g.state == G.SCENE, g.state
    assert g.scene["scene"].key == "door", g.scene["scene"].key
    clear_story(g)
    step(g, 2)
    assert g.state == G.ENDING_CHOICE, g.state
    assert len(g.ending_options) >= 2, [e.key for e in g.ending_options]
    g.handle_event(key(pygame.K_DOWN))
    g.handle_event(key(pygame.K_RETURN))
    step(g, 3)
    assert g.state == G.ENDING_TEXT, g.state
    assert g.ending is not None
    assert g.ending.key in g.meta.endings, g.meta.endings
    assert g.last_score > 0, g.last_score
    g.handle_event(key(pygame.K_RETURN))
    step(g, 3)
    assert g.state == G.RECORDS, g.state
    g.handle_event(key(pygame.K_RETURN))
    step(g, 2)
    assert g.state == G.TITLE, g.state


def ngplus(g):
    """New Game+ keeps the class and modifiers and raises the tier."""
    g.pending_class = "cutpurse"
    g.pending_modifiers = __import__(
        "pixel_rpg.modifiers", fromlist=["m"]).RunModifiers(["glass"])
    g.pending_ngplus = 0
    g.new_run("NgTester", 2)
    step(g, 4)
    clear_story(g)
    base_hp = g.player.max_hp()
    g.start_ngplus()
    step(g, 60)
    clear_story(g)
    assert g.player.ngplus == 1, g.player.ngplus
    assert g.player.class_key == "cutpurse", g.player.class_key
    assert g.player.modifiers.has("glass"), g.player.modifiers.keys
    assert g.player.max_hp() > base_hp, (g.player.max_hp(), base_hp)


def class_signatures(g):
    """Each signature must actually change a number in combat."""
    from pixel_rpg.entities import Player
    warden = Player("W", "warden")
    warden.hp = warden.max_hp() // 5
    # 0.6 since the s52 balance pass; it was 0.5 and the Warden essentially
    # never lost a fight. `classes.py` says 40% and so does this.
    assert warden.incoming_multiplier() == 0.6

    # **Momentum is the same hook read backwards**, so it goes in beside
    # the Warden: an Ironmonk on its last quarter must take *more*, and
    # getting the sign wrong would be invisible everywhere else (s52).
    monk = Player("M", "ironmonk")
    monk.hp = monk.max_hp()
    assert monk.incoming_multiplier() < 1.0
    monk.hp = max(1, monk.max_hp() // 5)
    assert monk.incoming_multiplier() > 1.0

    apoth = Player("A", "apothecary")
    assert apoth.heal_multiplier(from_item=True) == 2.0
    assert apoth.heal_multiplier(from_item=False) == 1.0

    ember = Player("E", "emberwright")
    ember.kindling = 5
    assert ember.outgoing_multiplier() > 1.5

    rev = Player("R", "revenant")
    assert rev.heal_cap() < rev.max_hp()
    rev.hp = 1
    assert rev.outgoing_multiplier() > 1.5

    way = Player("Y", "wayfarer")
    assert way.loot_luck() > 0 and way.gold_multiplier() > 1.2

    # Oathbound: healing must splash onto the enemy.
    oath = Player("O", "oathbound")
    oath.gain_xp(4000)
    oath.hp = oath.max_hp() // 3
    enemy = make_enemy(5, "meadow", g.settings.mods)
    battle = __import__("pixel_rpg.battle", fromlist=["b"]).Battle(oath, enemy)
    before = enemy.hp
    battle._heal_player(60)
    assert enemy.hp < before, "Vow did not splash onto the enemy"


def mouse_input(g):
    """Hover, click and wheel must drive the same cursors the keys do."""
    def click(pos, button=1):
        g.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos,
                                          button=button))

    def motion(pos):
        g.handle_event(pygame.event.Event(pygame.MOUSEMOTION, pos=pos,
                                          rel=(0, 0), buttons=(0, 0, 0)))

    # Title: click the third row and land on the Codex.
    g.state = G.TITLE
    step(g, 2)
    rows = [(r, k, i) for r, k, i in g.ui.hot.items if k == "title"]
    assert len(rows) >= 4, "title rows were not registered"
    # Look the row up by label; the menu order changes as options are added.
    labels = [label for label, _fn in g.title_options()]
    wanted = next(i for i, label in enumerate(labels)
                  if label.startswith("Codex"))
    codex_row = next(r for r, k, i in rows if i == wanted)
    click(codex_row.center)
    step(g, 2)
    assert g.state == G.CODEX, g.state
    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 2)

    # Bag: hover moves the selection, wheel scrolls, click equips.
    # The preceding scenario starts a fresh run, so stock the bag first.
    from pixel_rpg.items import roll_item
    while len(g.player.inventory) < 6:
        g.player.add_item(roll_item(max(1, g.player.level)))
    g.state = G.OVERWORLD
    g.handle_event(key(pygame.K_i))
    step(g, 3)
    assert g.state == G.INVENTORY
    rows = [(r, i) for r, k, i in g.ui.hot.items if k == "inv"]
    assert rows, "inventory rows were not registered"
    target_rect, target_index = rows[min(2, len(rows) - 1)]
    g.ui.inv.index = 0
    motion(target_rect.center)
    step(g, 1)
    # Hover must NOT move the selection; only a click commits.
    assert g.ui.inv.index == 0, "hovering moved the selection"
    click(target_rect.center)
    step(g, 2)
    g.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-1, x=0))
    step(g, 1)

    tabs = [(r, i) for r, k, i in g.ui.hot.items if k == "inv_tab"]
    click(tabs[4][0].center)                 # the Items tab
    step(g, 2)
    assert g.ui.inv_tab == 4, g.ui.inv_tab
    g.ui.inv_tab = 0
    step(g, 2)

    # Clicking a bag row selects it and nothing else; Confirm commits.
    rows = [(r, i) for r, k, i in g.ui.hot.items if k == "inv"]
    if rows:
        weapons = [i for i in g.player.inventory if i.slot == "weapon"]
        if weapons:
            g.ui.inv_tab = 1                 # the Weapon tab
            step(g, 2)
            rows = [(r, i) for r, k, i in g.ui.hot.items if k == "inv"]
            before = g.player.equipment["weapon"]
            click(rows[0][0].center)
            step(g, 2)
            assert g.player.equipment["weapon"] is before, \
                "clicking a row equipped without confirmation"
            confirm = [r for r, k, i in g.ui.hot.items if k == "confirm"]
            assert confirm, "no confirm button was offered"
            click(confirm[0].center)
            step(g, 2)
            assert g.player.equipment["weapon"] is not before, \
                "Confirm did not equip"
            g.ui.inv_tab = 0
            step(g, 1)
    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 2)

    # Battle: clicking Attack must actually take the turn.
    g.player.full_restore()
    g.start_battle(make_enemy(4, "meadow", g.settings.mods))
    for _ in range(200):
        step(g, 1)
        if g.battle and g.battle.phase == "menu":
            break
    assert g.battle and g.battle.phase == "menu", "battle never offered a menu"
    rows = [(r, i) for r, k, i in g.ui.hot.items if k == "battle_menu"]
    assert rows, "battle menu rows were not registered"
    enemy_hp = g.battle.enemy.hp
    click(rows[0][0].center)                 # Attack
    for _ in range(120):
        step(g, 1)
        if not g.battle or g.battle.enemy.hp < enemy_hp:
            break
    assert g.battle is None or g.battle.enemy.hp < enemy_hp, "click did nothing"
    drive_battle(g, lambda g_, b: g_.handle_event(key(pygame.K_1)))

    # Movement is keyboard-only: a click on distant ground must NOT walk.
    g.state = G.OVERWORLD
    step(g, 2)
    start = (g.player.x, g.player.y)
    width, height = g.screen.get_size()
    click((width // 2 + 200, height // 2))
    step(g, 40)
    assert (g.player.x, g.player.y) == start, \
        "clicking the world moved the player; movement should be keys only"


def creator_does_not_start_early(g):
    """Regression: editing the look must never launch the run.

    The first build treated Enter on any creator row as "Begin", so clicking
    Skin or Hair dropped you straight into the game without letting you
    finish the character.
    """
    from pixel_rpg import menus as menus_mod
    from pixel_rpg.appearance import Appearance

    def click(pos):
        g.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos,
                                          button=1))

    g.state = G.TITLE
    g.title_index = 0
    g.handle_event(key(pygame.K_RETURN))          # New Game
    step(g, 2)
    g.ui.mode_index = 0                           # the long road (s59)
    g.handle_event(key(pygame.K_RETURN))
    step(g, 2)
    # Make the precondition rather than inherit it. A finished run
    # keeps its slot now (s19 decision 5), so an earlier check can
    # leave one occupied - and picking an occupied slot asks to
    # overwrite instead of going on to the creator.
    g.ui.slot.index = 0
    g.saves.delete(1)
    g.handle_event(key(pygame.K_RETURN))          # slot
    step(g, 2)
    g.handle_event(key(pygame.K_RETURN))          # class
    step(g, 2)
    # +1: the difficulty row sits above the modifiers now (2026-09-01).
    g.ui.modifier_index = len(__import__(
        "pixel_rpg.modifiers", fromlist=["m"]).MODIFIERS) + 1
    g.handle_event(key(pygame.K_RETURN))          # begin modifiers
    step(g, 3)
    assert g.state == G.NAME_ENTRY, g.state

    # Keyboard: Enter on every look row must stay on the creator.
    for label, field in menus_mod.CREATOR_FIELDS.items():
        g.ui.creator_index = menus_mod.CREATOR_ROWS.index(label)
        before = getattr(g.pending_appearance, field)
        g.handle_event(key(pygame.K_RETURN))
        step(g, 2)
        assert g.state == G.NAME_ENTRY, (
            "Enter on %s started the run" % label)
        assert getattr(g.pending_appearance, field) != before, (
            "Enter on %s did not cycle it" % label)

    # Mouse: clicking a look row cycles it and stays put.
    step(g, 2)
    rows = [(r, i) for r, k, i in g.ui.hot.items if k == "creator"]
    assert rows, "creator rows were not registered for the mouse"
    look_index = menus_mod.CREATOR_ROWS.index("Hair")
    row_rect = next(r for r, i in rows if i == look_index)
    before = g.pending_appearance.hair
    click(row_rect.center)
    step(g, 2)
    assert g.state == G.NAME_ENTRY, "clicking Hair started the run"
    assert g.pending_appearance.hair != before, "clicking Hair did nothing"

    # Mouse: the arrows cycle without starting either.
    arrows = [(r, k, i) for r, k, i in g.ui.hot.items
              if k in ("creator_prev", "creator_next")]
    assert arrows, "creator arrows were not registered"
    rect, kind, idx = arrows[0]
    click(rect.center)
    step(g, 2)
    assert g.state == G.NAME_ENTRY, "an arrow started the run"

    # Only Begin starts it.
    g.ui.creator_index = menus_mod.CREATOR_ROWS.index("Begin")
    chosen = g.pending_appearance.signature()
    g.handle_event(key(pygame.K_RETURN))
    step(g, 60)
    clear_story(g)
    assert g.state == G.OVERWORLD, g.state
    assert g.player.appearance.signature() == chosen, "look was lost"


def art_is_one_grid(g):
    """Every sprite must share the 16x16 grid - no mixed art styles."""
    from pixel_rpg import spritedata as sd
    sizes = set()
    for group in (sd.MONSTERS, sd.BOSSES, sd.PROPS, sd.HERO, sd.MONSTER_ALT):
        for name, rows in group.items():
            sizes.add((len(rows), len(rows[0])))
            assert len(rows) == sd.GRID, (name, len(rows))
            for row in rows:
                assert len(row) == sd.GRID, (name, len(row))
    assert sizes == {(sd.GRID, sd.GRID)}, sizes


def shop_needs_confirmation(g):
    """Buying must take a deliberate second press, not one stray click."""
    def click(pos):
        g.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos,
                                          button=1))

    g.state = G.OVERWORLD
    g.player.gold = 99999
    g.ui.refresh_shop(g.player, g.world)
    g.go(G.SHOP)
    step(g, 3)

    rows = [(r, i) for r, k, i in g.ui.hot.items if k == "shop"]
    assert rows, "shop rows were not registered"
    gold_before = g.player.gold
    click(rows[min(2, len(rows) - 1)][0].center)
    step(g, 2)
    assert g.player.gold == gold_before, "clicking a row bought something"

    confirm = [r for r, k, i in g.ui.hot.items if k == "confirm"]
    assert confirm, "the shop offered no confirm button"
    click(confirm[0].center)
    step(g, 2)
    assert g.player.gold < gold_before, "Confirm did not buy anything"

    # And the footer chips are clickable too.
    chips = [(r, i) for r, k, i in g.ui.hot.items if k == "key"]
    assert chips, "footer chips were not clickable"
    esc = next((r for r, code in chips if code == pygame.K_ESCAPE), None)
    assert esc is not None, "no Esc chip to click"
    click(esc.center)
    step(g, 2)
    assert g.state == G.OVERWORLD, "clicking the Esc chip did not leave"


def hover_highlights_without_selecting(g):
    """Hovering must light a row up; only a click may move the selection."""
    from pixel_rpg import menus as menus_mod

    def motion(pos):
        g.handle_event(pygame.event.Event(pygame.MOUSEMOTION, pos=pos,
                                          rel=(0, 0), buttons=(0, 0, 0)))

    # Battle menu: hovering row 2 must register as hovered, not selected.
    g.state = G.OVERWORLD
    g.player.full_restore()
    g.start_battle(make_enemy(4, "meadow", g.settings.mods))
    for _ in range(200):
        step(g, 1)
        if g.battle and g.battle.phase == "menu":
            break
    assert g.battle and g.battle.phase == "menu"
    rows = [(r, i) for r, k, i in g.ui.hot.items if k == "battle_menu"]
    assert rows, "battle menu rows were not registered"
    target, index = rows[2]
    before = g.battle.menu_index
    motion(target.center)
    step(g, 2)
    assert g.hover == ("battle_menu", index), g.hover
    assert g.battle.menu_index == before, "hovering moved the battle cursor"
    drive_battle(g, lambda g_, b: g_.handle_event(key(pygame.K_1)))

    # Footer chips report hover too.
    g.state = G.OVERWORLD
    g.handle_event(key(pygame.K_i))
    step(g, 3)
    chips = [(r, i) for r, k, i in g.ui.hot.items if k == "key"]
    assert chips, "no footer chips registered"
    rect, code = chips[0]
    motion(rect.center)
    step(g, 1)
    assert g.hover == ("key", code), g.hover
    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 2)


def rebinding_controls(g):
    """Rebinding must take effect, persist, and refuse to steal menu keys."""
    from pixel_rpg import keybinds as kb

    g.state = G.OVERWORLD
    g.go(G.KEYBINDS)
    step(g, 2)
    bag_row = next(i for i, (a, _l, _k) in enumerate(kb.ACTIONS)
                   if a == "bag")
    g.ui.keybind_index = bag_row

    # Reserved keys are refused.
    g.handle_event(key(pygame.K_RETURN))
    step(g, 1)
    g.handle_event(key(pygame.K_TAB))
    step(g, 1)
    assert g.binds.key_for("bag") == pygame.K_i, "a reserved key was accepted"

    # A free key binds, and the new key opens the bag.
    g.handle_event(key(pygame.K_RETURN))
    step(g, 1)
    g.handle_event(key(pygame.K_p))
    step(g, 1)
    assert g.binds.key_for("bag") == pygame.K_p, g.binds.key_for("bag")
    assert g.meta.keybinds.get("bag") == pygame.K_p, "binding was not saved"

    g.go(G.OVERWORLD)
    g.handle_event(key(pygame.K_p))
    step(g, 2)
    assert g.state == G.INVENTORY, "the rebound key did not open the bag"
    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 1)

    # The old key no longer does anything.
    g.state = G.OVERWORLD
    g.handle_event(key(pygame.K_i))
    step(g, 2)
    assert g.state == G.OVERWORLD, "the old binding still works"

    # Reset puts everything back.
    g.go(G.KEYBINDS)
    step(g, 1)
    g.handle_event(key(pygame.K_r))
    step(g, 1)
    assert g.binds.key_for("bag") == pygame.K_i, "reset did not restore"
    g.go(G.OVERWORLD)
    step(g, 1)


def guide_has_no_spoilers(g):
    """The guide teaches mechanics and gives away no story."""
    from pixel_rpg import guide, story
    text = " ".join(line for page in guide.PAGES for line in page.lines)
    banned = []
    for warden in story.WARDENS:
        banned.append(warden.true_name.split()[0])
    for ending in story.ENDINGS:
        banned.append(ending.title)
    banned += ["Ysolde", "anchor", "Hollow Key"]
    leaked = [word for word in banned if word and word in text]
    assert not leaked, "the guide leaks: %s" % leaked
    assert len(guide.PAGES) >= 5


def save_folder_setting(g):
    """Switching the save folder must repoint the game and be remembered."""
    from pixel_rpg import config as cfg
    from pixel_rpg import menus as menus_mod

    original = cfg.read_save_location()
    # The harness pins PIXEL_RPG_SAVE_DIR, which deliberately outranks the
    # in-game setting; lift it so the setting itself can be tested.
    forced = os.environ.pop("PIXEL_RPG_SAVE_DIR", None)
    try:
        g.state = G.OVERWORLD
        g.go(G.SETTINGS_SCREEN)
        step(g, 2)
        row = menus_mod.SETTING_ROWS.index("Save folder")
        g.ui.settings_index = row
        before = str(g.saves.dir)
        # `D`, not Right. The settings screen went deaf to the arrows
        # so the konami sequence could live there without changing
        # anything on the way in (s19 decision 6).
        g.handle_event(key(pygame.K_d))
        step(g, 2)
        assert cfg.read_save_location() != original, "the choice did not change"
        assert str(g.saves.dir) != before, "the save manager was not repointed"
        assert cfg.location_file().is_file(), "the choice was not written down"

        # And it survives a fresh read.
        assert cfg.default_save_dir() == g.saves.dir
        # With the override back in place the setting must refuse politely.
        os.environ["PIXEL_RPG_SAVE_DIR"] = forced or str(g.saves.dir)
        assert cfg.save_dir_overridden()
        pinned = cfg.read_save_location()
        g.handle_event(key(pygame.K_RIGHT))
        step(g, 2)
        assert cfg.read_save_location() == pinned, \
            "the setting changed while the environment was forcing the path"
    finally:
        if forced is not None:
            os.environ["PIXEL_RPG_SAVE_DIR"] = forced
        cfg.write_save_location(original)
        g.settings.save_dir = cfg.default_save_dir()
        g.rebind_save_dir()
        g.state = G.OVERWORLD


def village_is_a_village(g):
    """The spawn must look like somewhere people live, and stay clear of arenas."""
    from pixel_rpg.world import ROOFS, T_ARENA, World

    for seed in (3, 11, 29):
        world = World(32, seed)
        town = world.town_rect
        roofs = sum(1 for gy in range(town.top, town.bottom)
                    for gx in range(town.left, town.right)
                    if world.in_bounds(gx, gy)
                    and world.grid[gy][gx] in ROOFS)
        arenas = sum(1 for gy in range(town.top, town.bottom)
                     for gx in range(town.left, town.right)
                     if world.in_bounds(gx, gy)
                     and world.grid[gy][gx] == T_ARENA)
        props = [world.props.get((gx, gy))
                 for gy in range(town.top, town.bottom)
                 for gx in range(town.left, town.right)]
        assert roofs >= 12, "seed %d has no roofed buildings" % seed
        assert arenas == 0, "seed %d put an arena inside the village" % seed
        assert "well" in props, "no well"
        assert props.count("window") >= 4, "houses have no windows"
        assert props.count("villager") >= 3, "nobody lives here"
        assert not world.blocked(*world.spawn), "spawn is inside something"
        reach = world._reachable_from(world.spawn)
        # Beside the gate, not on it (s64): an arena gate blocks, so its
        # own tile is never in a flood that uses the player's rules.
        assert all(world._beside((p.gx, p.gy), reach)
                   for p in world.portals), seed


def npc_and_door_routing(g):
    """Each kind of person and door must lead somewhere sensible."""
    from pixel_rpg.world import NPC

    g.state = G.OVERWORLD
    villager = next(n for n in g.world.npcs if n.kind == "villager")
    g.player.x = villager.gx * g.tile + g.tile / 2
    g.player.y = (villager.gy + 1) * g.tile + g.tile / 2
    g.interact()
    step(g, 2)
    # A villager has up to two conversations with answers in them - their
    # own written scene, and then a favour if they have one to ask (s42) -
    # and after those they fall back to a line of flavour. **The count is
    # not the point; the fallback is.** This used to assert "second visit"
    # and broke the moment people had a second thing to say.
    assert g.state in (G.DIALOGUE, G.SCENE), "a villager did not talk"
    for _ in range(4):
        if g.state != G.SCENE:
            break
        assert g.scene["options"], "a villager scene offered nothing"
        clear_story(g)
        g.interact()
        step(g, 2)
    assert g.state == G.DIALOGUE, "a villager never fell back to flavour"
    assert "quest" not in (g.dialogue or {}).get("title", "").lower()
    assert g.dialogue["title"] == villager.name, g.dialogue["title"]
    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 2)

    # A runner leads to the quest board; a villager must not.
    runner = next(n for n in g.world.npcs if n.kind == "runner")
    g.state = G.OVERWORLD
    g.player.x = runner.gx * g.tile + g.tile / 2
    g.player.y = (runner.gy + 1) * g.tile + g.tile / 2
    g.interact()
    step(g, 2)
    clear_story(g)
    assert g.state == G.QUESTS, g.state
    assert g.ui.quest_readonly is False, "a runner opened a read-only log"
    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 2)

    # Doors: the trader, the forge, and somebody's front door.
    #
    # **A home door goes inside now** (s56, item 18). It used to open a
    # dialogue through the letterbox, and this asserted that. Coming back
    # out is part of the check rather than a tidy-up: `enter_house` swaps
    # `game.world` for the room, so a check that walked in and stopped
    # would leave every check *after* it looking at somebody's kitchen -
    # which is exactly what happened, and two chest checks two hundred
    # lines later failed with `StopIteration` on an empty world.
    island = g.world
    for pos, kind in list(g.world.doors.items()):
        g.state = G.OVERWORLD
        g.dialogue = None
        g.use_door((pos, kind))
        step(g, 2)
        if kind == "shop":
            assert g.state == G.SHOP, (kind, g.state)
        elif kind == "forge":
            assert g.state in (G.SMITH, G.DIALOGUE), (kind, g.state)
        else:
            assert g.house is not None, "a front door did not open"
            assert g.world is not island, "inside is the same world"
            assert g.overworld() is island, "the island went missing"
            g.leave_house()
            step(g, 2)
            assert g.world is island, "the way out led somewhere else"
        g.handle_event(key(pygame.K_ESCAPE))
        step(g, 1)
    g.state = G.OVERWORLD


def one_quest_at_a_time(g):
    """Only one job may be carried, and the screen has to say so."""
    from pixel_rpg.quests import MAX_ACTIVE
    g.quests.active = []
    g.quests.completed = []
    g.quests.baselines = {}
    assert g.quests.accept("cull", g.player)
    assert not g.quests.accept("warden1", g.player), "took a second job"
    assert len(g.quests.active) <= MAX_ACTIVE
    assert g.quests.full()

    g.state = G.OVERWORLD
    g.ui.quest_readonly = False
    g.go(G.QUESTS)
    g.ui.quest_tab = 0
    step(g, 2)
    rows = [(r, i) for r, k, i in g.ui.hot.items if k == "quest"]
    if rows:
        g.ui.quest.index = rows[0][1]
        g.handle_event(key(pygame.K_RETURN))
        step(g, 2)
        assert g.ui.quest_notice, "no explanation was shown"
    g.handle_event(key(pygame.K_ESCAPE))
    step(g, 1)


def overwriting_a_slot_asks_first(g):
    """Starting a new game on an occupied slot must confirm."""
    assert g.save_run(force=True), g.saves.last_error
    g.state = G.TITLE
    g.title_index = 0
    g.handle_event(key(pygame.K_RETURN))          # New Game
    step(g, 2)
    g.ui.mode_index = 0                           # the long road (s59)
    g.handle_event(key(pygame.K_RETURN))
    step(g, 2)
    assert g.state == G.SLOTS
    g.ui.slot.index = g.slot - 1
    g.handle_event(key(pygame.K_RETURN))
    step(g, 2)
    assert g.state == G.DIALOGUE, "it did not ask before overwriting"
    assert "Overwrite" in g.dialogue["title"], g.dialogue["title"]
    g.handle_event(key(pygame.K_ESCAPE))          # back out
    step(g, 2)
    assert g.state == G.SLOTS, g.state
    assert g.saves.describe(g.slot)["empty"] is False, "the save was erased"
    g.go(G.TITLE)
    step(g, 1)


def story_choices_are_recorded(g):
    """Answering a scene must weigh the ending and fill in the codex."""
    from pixel_rpg import scenes as scenes_mod

    g.meta.tainted = False
    g.meta.scene_seen = {}
    g.meta.scene_taken = {}
    g.meta.secrets = []
    g.player.leanings = {}
    g.state = G.OVERWORLD
    g.scene = None

    assert g.play_scene("prologue"), "the prologue would not open"
    assert g.state == G.SCENE, g.state
    offered = [o.key for o in g.scene["options"]]
    g.scene["index"] = offered.index("who")
    g.handle_event(key(pygame.K_RETURN))
    assert g.scene["phase"] == "reply", "the answer was not said back"
    g.handle_event(key(pygame.K_RETURN))
    assert g.state == G.OVERWORLD, g.state

    assert g.meta.scene_taken.get("prologue") == ["who"], g.meta.scene_taken
    assert set(g.meta.scene_seen["prologue"]) == set(offered), \
        "the roads not taken were not remembered"
    assert g.player.leanings.get("longwalk"), "the answer weighed nothing"

    # the record renders, and shows the road not taken
    g.go(G.STORY)
    step(g, 2)
    assert any(k == "story" for _r, k, _i in g.ui.hot.items), \
        "the story record has no rows"
    g.state = G.OVERWORLD
    step(g, 2)

    # six listens open something that is otherwise not in the game
    door = scenes_mod.SCENES_BY_KEY["door"]
    assert "names" not in [c.key for c in door.offered(g.meta.secrets)], \
        "the secret was on the table before it was earned"
    for scene_key in scenes_mod.WARDEN_SCENE_BY_BOSS.values():
        g.play_scene(scene_key)
        opts = [o.key for o in g.scene["options"]]
        g.scene["index"] = opts.index("listen")
        g.handle_event(key(pygame.K_RETURN))
        g.handle_event(key(pygame.K_RETURN))
    assert scenes_mod.ALL_HEARD in g.meta.secrets, "the shards did not add up"
    assert "names" in [c.key for c in door.offered(g.meta.secrets)], \
        "listening to all six opened nothing"

    # and a marked run records none of it
    g.meta.tainted = True
    before = dict(g.meta.scene_taken)
    g.play_scene("prologue")
    g.handle_event(key(pygame.K_RETURN))
    g.handle_event(key(pygame.K_RETURN))
    assert g.meta.scene_taken == before, "a marked run wrote to the record"
    g.meta.tainted = False
    g.state = G.OVERWORLD
    step(g, 2)


def achievements_have_two_layers(g):
    """Per-save for the chase, global for the cabinet."""
    from pixel_rpg.quests import ACHIEVEMENTS

    g.meta.tainted = False
    g.quests.unlocked = []
    g.meta.achievements = []
    g.player.stats_tracker["kills"] = 500
    g.after_battle_checks()
    assert "first_blood" in g.quests.unlocked, "the run earned nothing"
    assert "first_blood" in g.meta.achievements, "the cabinet stayed empty"

    # a second character earns the whole set again from nothing
    g.quests.unlocked = []
    fresh = g.quests.check_achievements(g.player)
    assert "first_blood" in [a.key for a in fresh], \
        "a new save could not re-earn a feat"

    # but a marked run never reaches the cabinet
    g.meta.achievements = []
    g.meta.tainted = True
    g.quests.unlocked = []
    g.after_battle_checks()
    assert g.quests.unlocked, "a marked run earned nothing at all"
    assert not g.meta.achievements, "a marked run filled the cabinet"
    g.meta.tainted = False

    # and the screen renders
    g.meta.achievements = ["first_blood"]
    g.go(G.ACHIEVEMENTS_SCREEN)
    step(g, 2)
    assert any(k == "feat" for _r, k, _i in g.ui.hot.items), \
        "the cabinet has no clickable rows"
    labels = [label for label, _fn in g.title_options()]
    assert any("Achievements" in label for label in labels), \
        "no way in from the title"
    g.go(G.OVERWORLD)
    step(g, 2)


def the_debug_menu_is_found(g):
    """Hidden entrance, and a marked run that cannot touch the profile."""
    from pixel_rpg import devmenu

    g.state = G.OVERWORLD
    g.player.debug_used = False
    g.meta.tainted = False

    # a near miss opens nothing
    for code in devmenu.SEQUENCE[:-1]:
        g.handle_event(key(code))
    g.handle_event(key(pygame.K_z))
    assert g.state == G.OVERWORLD, "a wrong key still opened the debug menu"

    # the sequence asks first; backing out must leave no trace
    for code in devmenu.SEQUENCE:
        g.handle_event(key(code))
    assert g.state == G.DIALOGUE, "the sequence did not ask for confirmation"
    g.handle_event(key(pygame.K_ESCAPE))
    assert g.state != G.DEVMENU, "declining still opened the debug menu"
    assert g.dialogue is None, "the prompt was left on screen"
    assert not g.player.debug_used, "declining marked the run"

    g.state = G.OVERWORLD
    for code in devmenu.SEQUENCE:
        g.handle_event(key(code))
    g.handle_event(key(pygame.K_RETURN))
    assert g.state == G.DEVMENU, "confirming did not open the debug menu"
    assert "curious" in g.quests.unlocked, "no feat for finding it"
    assert g.meta.found_devmenu, "the discovery was not remembered"
    # **Opening it marks the run** (2026-09-01, his instruction). The feat
    # above is granted first and on purpose: a marked run cannot write to
    # the profile, so the order is what lets both of these be true.
    assert g.player.debug_used, "opening the menu left the run clean"
    step(g, 2)

    # a power marks the run, and the profile then refuses everything
    codex_before = list(g.meta.codex)
    endings_before = list(g.meta.endings)
    gold_before = g.player.gold
    index = [i for i, (label, _n, _f, _t) in enumerate(devmenu.ACTIONS)
             if "gold" in label][0]
    devmenu.run_action(g, index)
    assert g.player.gold > gold_before, "the power did nothing"
    assert g.player.debug_used and g.meta.tainted, "the run was not marked"

    g.meta.unlock_codex("ending_seventh")
    g.meta.record_ending("seventh")
    assert g.meta.codex == codex_before, "a marked run wrote to the codex"
    assert g.meta.endings == endings_before, "a marked run recorded an ending"

    g.handle_event(key(pygame.K_ESCAPE))
    assert g.state != G.DEVMENU, "Esc did not close the debug menu"

    # once found, a bound key gets you back in without the sequence
    assert any(a == "debug" for a, _l, _k in
               __import__("pixel_rpg.keybinds", fromlist=["k"]).ACTIONS), \
        "no debug keybind exists"
    g.state = G.OVERWORLD
    g.handle_event(key(g.binds.key_for("debug")))
    assert g.state == G.DEVMENU, "the bound key did not open the debug menu"
    g.handle_event(key(pygame.K_ESCAPE))

    # and a fresh run starts clean again
    g.player.debug_used = False
    g.meta.tainted = False
    g.dialogue = None
    g.state = G.OVERWORLD
    step(g, 2)


def the_session_is_logged(g):
    """The log must record what actually happened, not just that it started."""
    g.log.event("smoke_marker", check="ok")
    assert g.log.path is not None, "nothing was ever written"
    with open(g.log.path, encoding="utf-8") as fh:
        text = fh.read()
    for expected in ("Pixel RPG", "run_start", "battle_start", "smoke_marker"):
        assert expected in text, "log is missing %r" % expected


def a_crash_leaves_a_report(g):
    """A windowed exe cannot print a traceback; it has to leave a file."""
    import os

    try:
        raise RuntimeError("deliberate smoke crash")
    except RuntimeError as exc:
        path = g.log.crash(exc, context="state=%s" % g.state)
    assert path is not None, "no crash report was written"
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    assert "deliberate smoke crash" in body, "no traceback in the report"
    assert "platform" in body, "no environment in the report"
    os.remove(path)


def a_chest_pays_out_once(g):
    """One chest, one looting - and junk loot costs the reward, not the run.

    `take_chest_loot` is fed straight off a socket in co-op, so it is read
    the way save data is read. The check that matters is that a second
    helping is impossible and a malformed payload is survivable.
    """
    chest = next(c for c in g.world.chests if not c.opened)
    before_gold = g.player.gold
    before_chests = g.player.stats_tracker["chests"]

    loot = g.chest_loot(chest.tier, g.player.level)
    assert loot["gold"] > 0, "a chest rolled nothing"
    assert loot["item"], "a chest rolled no item"

    g.take_chest_loot(chest.gx, chest.gy, loot)
    assert chest.opened, "the chest never opened"
    assert g.world.props[(chest.gx, chest.gy)] == "chest_open"
    assert g.player.gold == before_gold + loot["gold"], "gold not paid"
    assert g.player.stats_tracker["chests"] == before_chests + 1

    # Second helping: refused, and nothing moves.
    gold_now, count_now = g.player.gold, g.player.stats_tracker["chests"]
    g.take_chest_loot(chest.gx, chest.gy, loot)
    assert g.player.gold == gold_now, "a chest paid out twice"
    assert g.player.stats_tracker["chests"] == count_now

    # And a payload that came off a hostile socket.
    other = next(c for c in g.world.chests if not c.opened)
    gold_now = g.player.gold
    g.take_chest_loot(other.gx, other.gy,
                      {"gold": "all of it", "item": {"name": "‮hax"},
                       "cons": "../../etc", "relic": ["not", "a", "key"]})
    assert other.opened, "a bad payload left the chest hanging"
    assert g.player.gold == gold_now, "a string paid out as gold"

    # A chest nobody has ever heard of is simply not there.
    g.take_chest_loot(4095, 4095, {"gold": 999999})
    assert g.player.gold == gold_now, "loot was paid for a chest off the map"


def a_client_never_opens_a_chest_itself(g):
    """In a room, `interact` asks the host instead of reaching in."""
    from pixel_rpg import coop as coop_mod

    chest = next(c for c in g.world.chests if not c.opened)
    asked = []

    class Pretend:
        role = coop_mod.CLIENT
        ready = True
        remotes = {}
        status = ""
        error = ""

        def claim_chest(self, gx, gy, level):
            asked.append((gx, gy, level))
            return True

    real, g.coop = g.coop, Pretend()
    try:
        before = g.player.gold
        g.open_chest(chest)
        assert asked == [(chest.gx, chest.gy, g.player.level)], \
            "a client opened a chest without asking the host"
        assert not chest.opened, "a client opened a chest on its own say-so"
        assert g.player.gold == before, "a client paid itself"
    finally:
        g.coop = real


def _room(host_name="Hostess", guest_name="Guest"):
    """Two real Games on a real socket, in the same valley.

    Nothing here is mocked below the transport: two `Game` objects, two
    `Session` objects, and loopback TCP between them. A shared battle is the
    one feature where a mock would prove nothing - the whole question is
    whether two machines agree.
    """
    host = G.Game()
    guest = G.Game()
    host.new_run(host_name, "warden")
    for _ in range(10):
        host.player.gain_xp(host.player.xp_to_next())
    host.start_hosting(reach_out=False, port=0)
    assert host.coop is not None, "hosting failed"
    guest.code_buffer = host.coop.room_code
    assert guest.try_join(), "joining failed"
    _pump(host, guest, 120)
    assert guest.coop is not None and guest.coop.ready, "guest never joined"

    # **The guest arrives with nothing** (s19 item 5c): a hero belongs to a
    # valley, this is its first visit, so character creation is on screen.
    # Finishing it is what the player does and the harness does it for them
    # - a guest with no hero is not a state any later check is about.
    if guest.player is None:
        assert guest.guest_pending == host.world.seed, (
            "the guest was sent to make a hero for the wrong valley")
        guest.pending_class = "warden"
        guest.new_guest_run(guest_name, guest.guest_pending)
    for _ in range(10):
        guest.player.gain_xp(guest.player.xp_to_next())
    _pump(host, guest, 20)
    return host, guest


def _fight_together(host, guest, level=1, biome="meadow", frames=40):
    """Open a fight and have the guest accept the invitation.

    **Standing close is no longer consent** (s45, 2026-09-01). Savvydev,
    twice: *"dragging in shouldnt happen anyway though"*. So a shared
    fight now takes three steps rather than one - the host offers, the
    guest presses E, and the host opens it - and this harness does what a
    player does rather than reaching past the new rule.
    """
    gx, gy = host.player_tile()
    offered = host.host_open_fight(level, biome, gx, gy)
    assert offered, "the host would not start anything"
    # The offer has to reach the guest before it can be answered.
    _pump(host, guest, 6)
    assert guest.fight_offer is not None, "the guest was never invited"
    assert guest.accept_fight_offer(), "the guest could not say yes"
    _pump(host, guest, frames)
    return offered


def _pump(host, guest, frames=60, dt=1 / 30):
    """Run both ends for a while, in something close to real time.

    The sleep is not politeness. Peers are dropped for talking faster than
    120 messages a second, and that limit is measured against the wall clock
    - so a test loop that runs a whole fight in fifty milliseconds looks
    exactly like a flood and gets itself disconnected. Sleeping keeps the
    ratio between game time and real time somewhere near honest.
    """
    for _ in range(frames):
        time.sleep(0.004)
        for game in (host, guest):
            # The real loop, not a hand-rolled one: `Game.update` is where a
            # finished fight is noticed, and a test that skipped it would
            # have missed a mirror ending its own battle.
            game.update(dt)


def _stand_together(host, guest):
    """Put the guest right next to the host, and let both ends hear it."""
    guest.player.x, guest.player.y = host.player.x + 8, host.player.y
    _pump(host, guest, 40)


def _until_someones_turn(host, guest, frames=200):
    """Run the fight up to the first decision it waits on.

    The banner alone is 1.35 seconds, and a check that pokes at a battle
    still playing its intro proves nothing - which is how the authority
    check below passed against a build with the authority removed.
    """
    for _ in range(frames):
        _pump(host, guest, 4)
        if host.battle is not None and host.battle.phase == G.BATTLE_MENU:
            return
    raise AssertionError("the fight never reached a decision")


def finger(kind, x, y, finger_id=1):
    """A finger event in the normalised coordinates SDL actually sends."""
    return pygame.event.Event(kind, x=x, y=y, dx=0.0, dy=0.0,
                              finger_id=finger_id, touch_id=1, pressure=1.0)


def _normalise(g, px, py):
    w, h = g.screen.get_size()
    return px / float(w), py / float(h)


def touch_walks_but_a_tap_never_does(g):
    """Hard rule 6, on glass.

    A tap on the world does nothing, exactly as a mouse click on the world
    does nothing. The stick moves you, and it moves you by publishing arrow
    keys - which is the allowance section 14 already spells out for a pad.
    """
    from pixel_rpg import touch as touch_mod

    g.go(G.OVERWORLD)
    g.dialogue = None
    g.touch.enabled = True
    try:
        g.touch.layout(g.screen.get_size())
        start = (g.player.x, g.player.y)

        # A tap in the middle of the world: nothing moves.
        g.handle_event(finger(pygame.FINGERDOWN, *_normalise(g, 640, 300)))
        g.handle_event(finger(pygame.FINGERUP, *_normalise(g, 640, 300)))
        step(g, 4)
        assert (g.player.x, g.player.y) == start, \
            "a tap on the world moved the player"

        # The stick, pushed right: arrow keys, and the player walks.
        home = g.touch.stick_home
        g.handle_event(finger(pygame.FINGERDOWN, *_normalise(g, *home)))
        g.handle_event(finger(pygame.FINGERMOTION,
                              *_normalise(g, home[0] + 80, home[1])))
        assert pygame.K_RIGHT in g.touch.held, g.touch.held
        keys = g.touch.wrap_keys(FakeKeys({}))
        assert keys[pygame.K_RIGHT], "the stick did not publish an arrow key"
        step(g, 6)
        assert g.player.x > start[0], "the stick did not walk the player"

        # Let go and it stops asking.
        g.handle_event(finger(pygame.FINGERUP,
                              *_normalise(g, home[0] + 80, home[1])))
        assert g.touch.held == set(), g.touch.held
    finally:
        g.touch.enabled = False
        g.touch.stick_finger = None
        g.touch.held = set()


def a_stick_at_the_edge_sprints(g):
    g.go(G.OVERWORLD)
    g.touch.enabled = True
    try:
        g.touch.layout(g.screen.get_size())
        home = g.touch.stick_home
        g.handle_event(finger(pygame.FINGERDOWN, *_normalise(g, *home)))
        from pixel_rpg.touch import STICK_RADIUS, SPRINT_KEY
        g.handle_event(finger(pygame.FINGERMOTION,
                              *_normalise(g, home[0] + STICK_RADIUS + 20,
                                          home[1])))
        assert SPRINT_KEY in g.touch.held, "a stick at the edge did not run"
        g.handle_event(finger(pygame.FINGERMOTION,
                              *_normalise(g, home[0] + 40, home[1])))
        assert SPRINT_KEY not in g.touch.held, "it never stopped running"
    finally:
        g.touch.enabled = False
        g.touch.stick_finger = None
        g.touch.held = set()


def a_resting_thumb_is_not_a_direction(g):
    """There is no spring on glass to centre the stick."""
    g.go(G.OVERWORLD)
    g.touch.enabled = True
    try:
        g.touch.layout(g.screen.get_size())
        home = g.touch.stick_home
        g.handle_event(finger(pygame.FINGERDOWN, *_normalise(g, *home)))
        g.handle_event(finger(pygame.FINGERMOTION,
                              *_normalise(g, home[0] + 6, home[1] + 5)))
        assert g.touch.held == set(), g.touch.held
    finally:
        g.touch.enabled = False
        g.touch.stick_finger = None
        g.touch.held = set()


def a_touch_button_presses_the_bound_key(g):
    """The buttons are the pad's face buttons, so rebinding moves both."""
    from pixel_rpg import touch as touch_mod

    g.go(G.OVERWORLD)
    g.touch.enabled = True
    try:
        g.touch.layout(g.screen.get_size())
        bag = next(b for b in touch_mod.BUTTONS if b.action == "bag")
        g.handle_event(finger(pygame.FINGERDOWN,
                              *_normalise(g, bag.cx, bag.cy)))
        assert g.state == G.INVENTORY, \
            "the Bag button did not open the bag (state=%s)" % g.state
        g.handle_event(finger(pygame.FINGERUP,
                              *_normalise(g, bag.cx, bag.cy)))
        g.go(G.OVERWORLD)
    finally:
        g.touch.enabled = False
        g.touch.button_finger = {}


def a_tap_on_a_menu_is_a_click(g):
    """Every screen was already clickable, so this had better be all it takes."""
    g.go(G.TITLE)
    g.touch.enabled = True
    try:
        step(g, 2)
        rows = [(r, kind, index) for r, kind, index in g.ui.hot.items
                if kind == "title"]
        assert rows, "the title has no clickable rows"
        rect, _kind, index = rows[0]
        g.handle_event(finger(pygame.FINGERDOWN,
                              *_normalise(g, rect.centerx, rect.centery)))
        g.handle_event(finger(pygame.FINGERUP,
                              *_normalise(g, rect.centerx, rect.centery)))
        assert g.state != G.TITLE or g.ui.title_index == index, \
            "a tap on a title row did nothing at all"
    finally:
        g.touch.enabled = False
        g.go(G.TITLE)


def the_controls_stay_out_of_the_way_of_menus(g):
    """A stick over a menu is a stick in the way."""
    g.go(G.OVERWORLD)
    g.touch.enabled = True
    try:
        assert g.touch.active(g), "the controls are missing in the world"
        g.go(G.INVENTORY)
        assert not g.touch.active(g), "the stick followed us onto a menu"
        g.go(G.OVERWORLD)
        g.dialogue = {"title": "x", "body": "y", "confirm": "ok"}
        assert not g.touch.active(g), "the stick sat on top of a dialogue"
        g.dialogue = None
    finally:
        g.touch.enabled = False
        g.go(G.OVERWORLD)


def sdl_touch_echoes_are_swallowed_once(g):
    """SDL synthesises a mouse event per touch. Handling both means every
    tap happens twice."""
    echo = pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=(10, 10), button=1,
                              touch=True)
    real = pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=(10, 10), button=1,
                              touch=False)
    g.touch.enabled = True
    try:
        assert g.touch.swallow_mouse(echo), "the echo was let through"
        assert not g.touch.swallow_mouse(real), "a real click was swallowed"
    finally:
        g.touch.enabled = False
    assert not g.touch.swallow_mouse(echo), \
        "a touchscreen laptop stopped working as a mouse"


def both_drivers_run_the_same_frame(g):
    """The desktop loop and the browser loop must differ only in the yield.

    `tick` is the whole frame and both drivers call it; this proves the async
    one actually advances a real game rather than merely existing.
    """
    import asyncio

    g.go(G.OVERWORLD) if g.player else g.go(G.TITLE)
    before = g.frame_count if hasattr(g, "frame_count") else None
    assert before is None or isinstance(before, int)

    # Synchronous: one tick is one frame.
    playtime_before = g.playtime
    for _ in range(3):
        assert g.tick() is True, "tick reported the game had stopped"

    # Asynchronous: the same, driven by an event loop.
    async def a_few_frames():
        frames = 0
        while frames < 5:
            g.tick()
            await asyncio.sleep(0)
            frames += 1
        return frames

    assert asyncio.run(a_few_frames()) == 5
    assert g.playtime >= playtime_before, "time went backwards"
    assert g.running, "driving the loop stopped the game"


def the_async_driver_stops_when_the_game_does(_g):
    """`run_async` has to notice `quit_game`, or a browser tab hangs."""
    import asyncio

    game = G.Game()
    game.go(G.TITLE)

    async def drive():
        async def stopper():
            for _ in range(4):
                await asyncio.sleep(0)
            game.quit_game()
        task = asyncio.ensure_future(stopper())
        await game.run_async()
        await task

    asyncio.run(drive())
    assert not game.running, "the async driver outlived quit_game"


def a_browser_keeps_its_saves_in_the_browser(_g):
    """`config.open_save_store` is the one place that decides, so this is
    the one place that has to be checked."""
    from pixel_rpg import config as cfg, store as store_mod

    real = cfg.is_browser
    try:
        cfg.is_browser = lambda: False
        desktop = cfg.open_save_store(tempfile.mkdtemp(prefix="rpgdesk"))
        assert isinstance(desktop, store_mod.FileStore), desktop

        cfg.is_browser = lambda: True
        browser = cfg.open_save_store("/ignored")
        assert isinstance(browser, store_mod.BrowserStore), browser
        # No bridge on a desktop, so it must degrade rather than explode.
        assert browser.available() is False
        assert browser.prepare() is False
        assert browser.keys() == []
    finally:
        cfg.is_browser = real
    assert cfg.is_browser() is False, "we are not, in fact, in a browser"


def a_run_survives_a_save_and_load_in_browser_storage(_g):
    """End to end on the medium a phone would actually use."""
    from pixel_rpg import config as cfg, store as store_mod
    from pixel_rpg.save import SaveManager
    from pixel_rpg.meta import load_profile

    storage = FakeStorage()
    store = store_mod.BrowserStore(window=FakeWindow(storage))
    saves = SaveManager(store)

    game = G.Game()
    game.saves = saves
    game.meta = load_profile(store)
    game.pending_class = "warden"
    game.new_run("Phone", 1)
    game.dialogue = None
    game.state = G.OVERWORLD
    game.player.gold = 1234

    assert game.save_run(force=True), game.saves.last_error
    assert any(k.startswith("slot") for k in store.keys()), store.keys()
    assert not any("/" in k or "\\" in k for k in storage.items)

    fresh = SaveManager(store)
    described = [row for row in fresh.describe_all() if not row["empty"]]
    assert described, "nothing was found in browser storage"
    assert described[0]["name"] == "Phone", described[0]
    assert described[0]["gold"] == 1234, described[0]


class FakeStorage:
    """The browser's localStorage, near enough for a smoke check."""

    def __init__(self):
        self.items = {}

    @property
    def length(self):
        return len(self.items)

    def key(self, index):
        try:
            return sorted(self.items)[index]
        except IndexError:
            return None

    def getItem(self, name):                              # noqa: N802
        return self.items.get(name)

    def setItem(self, name, value):                       # noqa: N802
        self.items[name] = str(value)

    def removeItem(self, name):                           # noqa: N802
        self.items.pop(name, None)


class FakeWindow:
    def __init__(self, storage):
        self.localStorage = storage                       # noqa: N815


def a_shared_battle_runs_to_a_payout(_g):
    host, guest = _room()
    try:
        _stand_together(host, guest)
        assert guest.coop.body is not None, "the guest never said what it is"
        assert host.coop.bodies().get(guest.coop.player_id), \
            "the host never heard what the guest is made of"

        # A weak encounter on purpose: this checks that two machines agree
        # on a fight, not that anybody can win a hard one.
        _fight_together(host, guest)

        assert host.battle is not None and guest.battle is not None, \
            "the fight did not reach both machines"
        assert not host.battle.mirror and guest.battle.mirror, \
            "the wrong end is holding the dice"
        assert len(host.battle.players) == 2, "the guest was not pulled in"
        assert [m.name for m in host.battle.enemies] == \
               [m.name for m in guest.battle.enemies], \
            "the two machines rolled different monsters"
        assert host.battle.viewer != guest.battle.viewer, \
            "both ends think they are the same player"

        xp_before = (host.player.xp, guest.player.xp)
        _drive_shared(host, guest)

        assert host.battle is None and guest.battle is None, \
            "the fight never ended on both machines"
        assert host.coop.fight is None and guest.coop.fight is None
        gained = (host.player.xp - xp_before[0], guest.player.xp - xp_before[1])
        assert min(gained) > 0, "somebody who fought was paid nothing: %r" % (gained,)
        assert abs(gained[0] - gained[1]) <= 1, \
            "an even split was not even: %r" % (gained,)
    finally:
        for game in (host, guest):
            game.leave_coop()


def _drive_shared(host, guest, limit=400):
    """Play the fight out from both ends until somebody wins."""
    for _ in range(limit):
        for game in (host, guest):
            battle = game.battle
            if battle is None or not battle.is_your_turn:
                continue
            if battle.phase == G.BATTLE_SUBMENU:
                battle.cancel_submenu()
                continue
            battle.choose_menu(0)                      # Attack
            confirm_top(game, battle)
        _pump(host, guest, 6)
        if host.battle is None and guest.battle is None:
            return
    raise AssertionError("a shared fight never resolved")


def a_client_can_start_a_fight_it_does_not_roll(_g):
    """A client reports bumping into a monster; the host decides the rest."""
    host, guest = _room()
    try:
        _stand_together(host, guest)
        assert guest.coop.request_fight(3, "meadow"), "the ask never went out"
        # **The host is asked too, and answers by walking in.** A guest's
        # monster is the guest's fight; the host joins it or does not
        # (s45). The host has no prompt of its own - it is the machine
        # holding the offer - so it accepts by letting the window run out
        # with itself as the trigger's neighbour, which is what
        # `update_pending_fight` does after four seconds.
        _pump(host, guest, 8)
        if host.coop.pending is not None:
            host.coop.pending["accepted"] = list(
                host.coop.pending["invited"])
        _pump(host, guest, 40)
        assert host.battle is not None, "the host never opened the fight"
        assert guest.battle is not None and guest.battle.mirror, \
            "the asker did not get a mirror"
        assert len(host.battle.players) == 2, "the host was left out of it"
        assert ([m.name for m in host.battle.enemies]
                == [m.name for m in guest.battle.enemies])
        # The client asked; the host still rolled.
        assert host.coop.fight["seed"] > 0
    finally:
        for game in (host, guest):
            game.leave_coop()


def a_client_cannot_move_for_somebody_else(_g):
    """The host's two checks are the whole of the authority."""
    host, guest = _room()
    try:
        _stand_together(host, guest)
        _fight_together(host, guest, frames=4)
        _until_someones_turn(host, guest)

        battle = host.battle
        assert battle.phase == G.BATTLE_MENU, "not waiting on anybody yet"
        assert battle.turn == "player"
        # Whoever is *not* acting tries to swing anyway.
        idle = [i for i in range(2) if i != battle.acting_player][0]
        idle_id = host.coop.fight["party"][idle]
        phase_before = battle.phase
        hp_before = [m.hp for m in battle.enemies]
        host.host_apply_act(idle_id, "attack", "", 0)
        assert battle.phase == phase_before, "a spectator took a turn"
        assert [m.hp for m in battle.enemies] == hp_before, \
            "a player who was not acting dealt damage"

        # And a player who is not in the fight at all.
        host.host_apply_act(99, "attack", "", 0)
        assert [m.hp for m in battle.enemies] == hp_before, \
            "a stranger dealt damage"
    finally:
        for game in (host, guest):
            game.leave_coop()


def a_dropped_fighter_does_not_hang_the_fight(_g):
    """A turn-based fight that waits on somebody who left never ends."""
    host, guest = _room()
    try:
        _stand_together(host, guest)
        _fight_together(host, guest, frames=4)
        _until_someones_turn(host, guest)
        battle = host.battle
        assert len(battle.players) == 2

        gone = host.coop.fight["party"][1]
        host.fighter_lost(gone)
        assert 1 in battle.fled, "the leaver is still expected to act"
        assert battle.result is None, "one player leaving ended the fight"
        assert len(battle.standing()) == 1
    finally:
        for game in (host, guest):
            game.leave_coop()


def a_host_whose_own_connection_fails_finishes_its_fight(_g):
    """0cl. A guest dropping out of the host's fight counts as fleeing
    (the check above calls that by hand). The *host's* own connection
    failing cleared every guest at once and told the fight nothing, so
    the host's battle waited on turns that could never come. Here the
    host's socket fails mid-fight the way a dead connection does - its
    next pump raises - and the game finds out by itself."""
    host, guest = _room()
    try:
        _stand_together(host, guest)
        _fight_together(host, guest, frames=4)
        _until_someones_turn(host, guest)
        battle = host.battle
        assert len(battle.players) == 2
        gone = host.coop.fight["party"].index(guest.coop.player_id)

        def dead():
            raise OSError("network is unreachable")
        host.coop.transport.pump = dead
        for _ in range(3):
            host.update(1 / 30)
        assert host.coop is None, "the host never noticed its room was gone"
        assert host.battle is battle, "the host's own fight was thrown away"
        assert gone in battle.fled, "the host's fight still counts the guest in"
        for _ in range(240):
            host.update(1 / 30)
            if host.battle is not battle or battle.finished:
                break
            assert not (battle.phase == G.BATTLE_MENU
                        and battle.turn == "player"
                        and battle.acting_player == gone), (
                "the host's fight is waiting on a guest who is not there")
    finally:
        for game in (host, guest):
            game.leave_coop()


def one_quest_is_shared_by_a_room(_g):
    """0cj, his: *"one quest between two people."* In a story room the
    host's Maren quest is the party's. A guest takes it on by asking from
    their own board, kills by either of them count, and when it is handed
    in - by the guest, here - every machine pays its own hero the whole
    reward. Driven through the board, over a real socket."""
    from pixel_rpg import menus as menus_mod
    from pixel_rpg.quests import QUESTS_BY_KEY

    def enter(game):
        game.handle_event(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_RETURN, unicode="", mod=0))

    def board(game, tab, key):
        game.go("quests")
        game.ui.quest_readonly = False
        game.ui.quest_tab = tab
        rows = menus_mod.quest_rows(game)
        keys = [entry.key for _kind, entry in rows]
        assert key in keys, "%s is not on the board: %r" % (key, keys)
        game.ui.quest.index = keys.index(key)

    host, guest = _room()
    try:
        _pump(host, guest, 10)
        assert host.party_room() and guest.party_room(), "not a party room"
        assert "cull" in (guest.party_view or {}).get("o", ()), (
            "the guest never heard what Maren's board offers")

        # The hero they made after joining, not the "Newcomer" they said
        # hello as (a first visit has no hero yet).
        assert host.coop.remotes[guest.coop.player_id].name == "Guest", (
            "the host still calls the guest %r"
            % host.coop.remotes[guest.coop.player_id].name)

        board(guest, 1, "cull")
        enter(guest)                      # an ask, not an accept
        _pump(host, guest, 10)
        assert host.quests.active == ["cull"], (
            "the host never took the guest's pick on")
        assert (guest.party_view or {}).get("k") == "cull"
        assert "cull" not in guest.quests.active, (
            "the guest carries the party's quest as a second one")

        guest.player.stats_tracker["kills"] += 6
        guest.publish_body()
        host.player.stats_tracker["kills"] += 4
        _pump(host, guest, 10)
        assert host.quest_progress("cull") == (10, 10), (
            "the host does not count the guest's kills: %r"
            % (host.quest_progress("cull"),))
        assert guest.quest_progress("cull") == (10, 10)

        before = (host.player.gold, guest.player.gold)
        board(guest, 2, "cull")
        enter(guest)                      # handed to the host
        _pump(host, guest, 10)
        for game in (host, guest):
            for _ in range(6):            # a level's questions, answered
                if game.state not in (G.NEWSKILL, G.FULLBAG):
                    break
                game.handle_event(pygame.event.Event(
                    pygame.KEYDOWN, key=pygame.K_ESCAPE, unicode="", mod=0))
        reward = QUESTS_BY_KEY["cull"].gold
        assert host.player.gold - before[0] == reward, "the host was not paid"
        assert guest.player.gold - before[1] == reward, (
            "the guest was not paid the whole reward")
        assert "cull" in host.quests.completed
        assert "cull" in guest.quests.completed, (
            "the guest's own log never heard the job was done")
        assert not host.quests.active
        assert not (guest.party_view or {}).get("k")
    finally:
        for game in (host, guest):
            game.leave_coop()


def a_group_fight_resolves(g):
    """A pack must actually die, pay out, and hand the world back.

    The failure this guards against is a fight that never ends because the
    win condition still looks at one monster.
    """
    from pixel_rpg.entities import make_encounter

    # The smoke hero is level 1; three monsters at full strength would simply
    # kill it, and this check is about the group machinery, not the balance.
    # They are put on one hit of health so the fight is short and decided.
    g.player.hp = g.player.max_hp()
    g.player.mp = g.player.max_mp()
    before_gold = g.player.gold
    # **On a stream of its own** (s96). One hit of health is not a decided
    # fight when the pack can stun and regenerate: when two checks were
    # added before this one, the shared stream rolled a Storm Wisp that
    # stunned the hero, healed 2 a turn and outlasted a bleed, and the
    # "victory" was the hero waking at the shrine. Forty fresh fights won
    # forty - the dice were rare, not wrong - so the check stops depending
    # on which checks ran first, rather than on luck.
    from pixel_rpg.util import rng
    with rng.forked(1):
        foes = [make_enemy(1, "meadow", g.settings.mods) for _ in range(3)]
        for foe in foes:
            foe.hp = 1
        g.start_battle(foes)
        assert len(g.battle.enemies) == 3, "group was not kept"
        drive_battle(g, lambda g_, b: g_.handle_event(key(pygame.K_1)))
    assert g.state != "gameover", "the hero lost a fight against one-hit monsters"
    assert g.state != G.BATTLE, "group fight never resolved"
    assert all(not f.alive for f in foes), "a monster survived a victory"
    assert g.player.gold > before_gold, "no gold from a group"

    # and the generator hands back something playable
    group = make_encounter(5, "meadow", g.settings.mods, party=3)
    assert group and all(e.alive for e in group)


def targeting_a_group(g):
    """Aim must be movable, visible, and clickable."""
    foes = [make_enemy(1, "meadow", g.settings.mods) for _ in range(3)]
    for foe in foes:
        foe.hp = foe.max_hp() * 50      # nothing dies while we aim at it
    g.player.hp = g.player.max_hp() * 50
    g.start_battle(foes)
    # A three-monster enemy phase is three times as long as a duel's, so this
    # waits far longer than a single fight would need.
    for _ in range(1200):
        step(g)
        if g.battle and g.battle.phase == "menu":
            break
    assert g.battle and g.battle.phase == "menu", "group fight offered no menu"

    start = g.battle.target
    g.handle_event(key(pygame.K_RIGHT))
    assert g.battle.target != start, "Right did not move the aim"

    # every living monster registers a clickable spot, and clicking one aims
    # at it without swinging.
    spots = [(r, i) for r, k, i in g.ui.hot.items if k == "battle_target"]
    assert len(spots) == 3, "monsters are not clickable: %d" % len(spots)
    rect, index = spots[-1]
    phase_before = g.battle.phase
    g.mouse_click(rect.center, 1)
    assert g.battle.target == index, "clicking a monster did not aim at it"
    assert g.battle.phase == phase_before, "clicking a monster attacked it"

    g.battle = None
    g.state = G.OVERWORLD
    step(g, 2)


def controller_drives_the_menus(g):
    """A pad must reach the same code a keyboard reaches.

    Buttons are replayed as synthetic keypresses, so if this works the pad
    works on every screen -- including ones written before it existed.
    """
    from pixel_rpg import gamepad as gp

    pad = g.pad
    was_enabled, was_pads = pad.enabled, pad.pads
    g.go(G.SETTINGS_SCREEN)
    step(g, 2)
    start = g.ui.settings_index

    pad.enabled = True
    pad.pads = [FakePad(buttons={pygame.CONTROLLER_BUTTON_DPAD_DOWN: 1})]
    pad.update(1 / 60, g)
    step(g, 2)
    assert g.ui.settings_index != start, "d-pad did not move the settings cursor"

    # Holding is not a stream of presses: it waits before repeating.
    moved_to = g.ui.settings_index
    pad.update(0.01, g)
    assert g.ui.settings_index == moved_to, "d-pad repeated with no delay"

    # B backs out, exactly as Esc does.
    pad.pads = [FakePad()]
    pad.update(1 / 60, g)
    pad.handle_event(g, pygame.event.Event(
        pygame.CONTROLLERBUTTONDOWN, button=pygame.CONTROLLER_BUTTON_B))
    step(g, 2)
    assert g.state != G.SETTINGS_SCREEN, "B did not leave Settings"

    # In the world a direction is held, not pressed, and arrives as an arrow.
    g.go(G.OVERWORLD)
    pad.pads = [FakePad(axes={pygame.CONTROLLER_AXIS_LEFTX: -32767})]
    pad.update(1 / 60, g)
    assert pygame.K_LEFT in pad.held, "stick did not hold an arrow key"
    keys = pad.wrap_keys({pygame.K_LEFT: False})
    assert keys[pygame.K_LEFT], "held arrow did not reach movement"

    pad.enabled, pad.pads, pad.held = was_enabled, was_pads, set()
    g.go(G.OVERWORLD)
    step(g, 2)


def controller_never_costs_startup_time():
    """The reason the whole feature is opt-in.

    Enumerating pads measured 10.2s on the development machine. Building a
    Game must not touch that, or the window takes ten seconds again.
    """
    from pixel_rpg.gamepad import Gamepad

    fresh = Gamepad()
    assert not fresh.enabled, "a new Gamepad started enabled"
    assert not fresh.pads, "a new Gamepad opened devices"
    assert not pygame.joystick.get_init(), (
        "the joystick subsystem was initialised without anybody asking; "
        "that is the ten seconds init_pygame() exists to avoid")


def the_arena_you_pick_is_the_one_you_fight_in(g):
    """Item 0br, and the only end-to-end this screen has.

    The picker writes two fields on `Settings`; a wave asks `arena_for`
    what place it is in; `freeroamscreen.arena_of` asks the same thing
    for a real-time fight. **Three modules, and no test crossed all
    three** - a unit test can prove the picker sets a field and prove
    `arena_for` reads one, and both pass while the fight happens
    somewhere else entirely.
    """
    from pixel_rpg import freeroamscreen as frs, gamemodes, menus

    g.screen = pygame.display.set_mode((1280, 720))
    g.pending_mode = "waves"
    g.open_arena_select()
    assert g.state == G.ARENA_SELECT, "the picker did not open"

    # Walk to a named place with the keys, exactly as a player would.
    want = 6
    g.ui.arena_index = 0
    for _ in range(want):
        g.handle_event(key(pygame.K_RIGHT))
    assert g.ui.arena_index == want, g.ui.arena_index
    g.handle_event(key(pygame.K_RETURN))
    assert (g.settings.arena_mode, g.settings.arena_pick) == ("one", want), (
        "the picker did not write the choice: %r"
        % ((g.settings.arena_mode, g.settings.arena_pick),))

    chosen = gamemodes.ARENAS[want]
    for wave in (1, 2, 7, 41):
        here = gamemodes.arena_for(wave, g.settings.arena_mode,
                                   g.settings.arena_every,
                                   g.settings.arena_pick)
        assert here.key == chosen.key, (
            "wave %d is in %s and he picked %s"
            % (wave, here.key, chosen.key))

    # And the real-time screen agrees, which is the half that crosses a
    # module boundary - The Long Night draws its own arena.
    g.new_wave_run("Picker")
    assert frs.arena_of(g).key == chosen.key, (
        "the fight is in %s and he picked %s"
        % (frs.arena_of(g).key, chosen.key))

    # Every arena has a picture, and no two of them are the same picture.
    # A grid of eighteen identical rectangles is the screen he already
    # rejected twice, wearing a different shape.
    seen = set()
    for arena in gamemodes.ARENAS:
        shot = frs.arena_thumbnail(g.assets, (96, 60), arena)
        seen.add(pygame.image.tostring(shot, "RGB"))
    assert len(seen) == len(gamemodes.ARENAS), (
        "only %d of %d arenas draw a distinct picture"
        % (len(seen), len(gamemodes.ARENAS)))

    # **All of them are reachable.** The first cut of the screen stopped
    # drawing at the bottom edge and showed eight.
    shown = set()
    for index in range(len(gamemodes.ARENAS)):
        g.ui.arena_index = index
        _, cols, (start, fit, _) = menus.arena_grid(g, menus.frame_rect(g))
        shown |= {n for n in range(len(gamemodes.ARENAS))
                  if start <= n // cols < start + fit}
    assert len(shown) == len(gamemodes.ARENAS), (
        "%d of %d arenas can be seen" % (len(shown), len(gamemodes.ARENAS)))
    g.settings.arena_mode, g.settings.arena_pick = "every", 0


def a_wave_offers_a_choice_and_a_boss_improves_it(_g):
    """Items 0bs and 0bt, played rather than asserted.

    **Ten waves, every offer answered**, which is the only way to find
    out that a run accumulates what it takes: the unit tests prove one
    boon moves one stat, and this proves twenty waves of them do not end
    with a hero who is somehow weaker, a bag that silently stopped
    accepting loot, or an offer screen that stops appearing.
    """
    from pixel_rpg import spoils

    g = G.Game()
    g.screen = pygame.display.set_mode((1280, 720))
    g.new_wave_run("Spoiler")
    # Strong enough that eleven waves is a smoke check rather than an
    # afternoon. The offers do not care how hard the fight was.
    g.player.gain_xp(90_000)
    g.player.full_restore()
    seen = set()
    took = 0
    for _ in range(11):
        assert g.begin_wave(), "wave %d never opened" % g.waves.wave
        # **Fought, not shortcut.** Zeroing the monsters and calling the
        # payout by hand leaves `self.battle` standing, so the next
        # `begin_wave` refuses and the same wave is cleared eleven times -
        # a harness measuring a game nobody is playing (s23).
        drive_battle(g, lambda g_, b: g_.handle_event(key(pygame.K_1)))
        g.player.full_restore()
        assert g.state == G.SPOILS, (
            "wave %d cleared without an offer (state %r)"
            % (g.waves.cleared, g.state))
        guard = 0
        while g.state == G.SPOILS:
            offer = g.spoil_offer()
            seen.add(offer.kind)
            kind, card = g.spoil_card(0)
            assert card is not None, "an offer with no cards on it"
            # Every card has to say what it is and what it does - the
            # whole of his *"a small screen that says what each does"*.
            assert spoils.card_name(kind, card), "a card with no name"
            assert any(line.strip() for line
                       in spoils.card_lines(kind, card, g.player)), (
                "a %s card that says nothing" % kind)
            g.take_spoil(0)
            took += 1
            guard += 1
            assert guard < 8, "the offer screen would not close"
        assert g.state == G.WAVES, g.state

    assert seen == {"skill", "item", "buff"}, seen
    assert took >= 20, "only %d cards taken over ten waves" % took
    assert g.waves.bosses >= 1, "ten waves and no warden"
    assert g.player.buffs, "nothing held after ten waves of boons"
    assert g.player.granted, "no skill was ever kept"
    # **The drop rates went up** (item 0bt), which is one number read by
    # the rolling table rather than a second table.
    assert g.spoil_luck() > spoils.luck_for(0), (
        "a warden fell and the loot did not notice")

    # **Every mode hands out the same three** (item 0bz, 2026-09-08).
    # §86 gave The Long Night one mixed hand; he asked for the boons to
    # be their own four, so 0bu is open again and this is what it looks
    # like in the meantime.
    night = spoils.offers_for("zombies", 11, g.player, bosses=g.waves.bosses)
    assert [o.kind for o in night] == ["skill", "item", "buff"], (
        "a night should offer the same three, got %r"
        % [o.kind for o in night])
    for offer in night:
        assert len(offer.cards) == spoils.CARDS, (
            "%s offered %d cards" % (offer.kind, len(offer.cards)))


def run():
    print("Headless smoke test")
    g = G.Game()

    check("title renders", lambda: step(g, 3))
    check("title navigation", lambda: [g.handle_event(key(pygame.K_DOWN)),
                                       g.handle_event(key(pygame.K_UP)),
                                       step(g, 2)])
    check("new game flow", lambda: new_game(g))
    check("overworld movement", lambda: walk(g))
    check("bag", lambda: screen(g, G.INVENTORY, pygame.K_i))
    check("hero sheet", lambda: screen(g, G.CHARACTER, pygame.K_c))
    check("quest log", lambda: screen(g, G.QUESTS, pygame.K_j))
    check("shop buy and sell", lambda: shop(g))
    check("smith refines gear", lambda: smith(g))
    check("pause and settings", lambda: pause_settings(g))
    check("normal battle resolves", lambda: fight(g))
    check("skill and item submenus", lambda: skills_items(g))
    check("boss battle", lambda: boss(g))
    check("defeat and respawn", lambda: death(g))
    check("chest and shrine", lambda: chest_shrine(g))
    check("boss dialogue", lambda: dialogue(g))
    check("save and reload", lambda: save_load(g))
    check("codex screen", lambda: codex_screen(g))
    check("memory drop", lambda: memory_drop(g))
    check("class signatures", lambda: class_signatures(g))
    check("ending flow", lambda: ending_flow(g))
    check("new game plus", lambda: ngplus(g))
    check("mouse input", lambda: mouse_input(g))
    check("hover highlights, click selects",
          lambda: hover_highlights_without_selecting(g))
    check("the arena you pick is the one you fight in",
          lambda: the_arena_you_pick_is_the_one_you_fight_in(g))
    check("a wave offers a choice, and a boss improves it",
          lambda: a_wave_offers_a_choice_and_a_boss_improves_it(g))
    check("art is one grid", lambda: art_is_one_grid(g))
    check("shop needs confirmation",
          lambda: shop_needs_confirmation(g))
    check("creator never starts early",
          lambda: creator_does_not_start_early(g))
    check("rebinding controls", lambda: rebinding_controls(g))
    check("guide has no spoilers",
          lambda: guide_has_no_spoilers(g))
    check("village is a village",
          lambda: village_is_a_village(g))
    check("save folder setting",
          lambda: save_folder_setting(g))
    check("npc and door routing",
          lambda: npc_and_door_routing(g))
    check("one quest at a time", lambda: one_quest_at_a_time(g))
    check("overwriting a slot asks first",
          lambda: overwriting_a_slot_asks_first(g))
    check("a chest pays out once", lambda: a_chest_pays_out_once(g))
    check("a client never opens a chest itself",
          lambda: a_client_never_opens_a_chest_itself(g))
    check("a shared battle runs to a payout",
          lambda: a_shared_battle_runs_to_a_payout(g))
    check("a client can start a fight it does not roll",
          lambda: a_client_can_start_a_fight_it_does_not_roll(g))
    check("a client cannot move for somebody else",
          lambda: a_client_cannot_move_for_somebody_else(g))
    check("a dropped fighter does not hang the fight",
          lambda: a_dropped_fighter_does_not_hang_the_fight(g))
    check("a host whose own connection fails finishes its fight",
          lambda: a_host_whose_own_connection_fails_finishes_its_fight(g))
    check("one quest is shared by a room",
          lambda: one_quest_is_shared_by_a_room(g))
    check("a group fight resolves", lambda: a_group_fight_resolves(g))
    check("targeting a group", lambda: targeting_a_group(g))
    check("story choices are recorded",
          lambda: story_choices_are_recorded(g))
    check("achievements have two layers",
          lambda: achievements_have_two_layers(g))
    check("the debug menu is found, not given",
          lambda: the_debug_menu_is_found(g))
    check("the session is logged", lambda: the_session_is_logged(g))
    check("a crash leaves a report", lambda: a_crash_leaves_a_report(g))
    check("controller drives the menus",
          lambda: controller_drives_the_menus(g))
    check("controller never costs startup time",
          controller_never_costs_startup_time)
    check("touch walks but a tap never does",
          lambda: touch_walks_but_a_tap_never_does(g))
    check("a stick at the edge sprints", lambda: a_stick_at_the_edge_sprints(g))
    check("a resting thumb is not a direction",
          lambda: a_resting_thumb_is_not_a_direction(g))
    check("a touch button presses the bound key",
          lambda: a_touch_button_presses_the_bound_key(g))
    check("a tap on a menu is a click", lambda: a_tap_on_a_menu_is_a_click(g))
    check("touch controls stay out of the way of menus",
          lambda: the_controls_stay_out_of_the_way_of_menus(g))
    check("sdl touch echoes are swallowed once",
          lambda: sdl_touch_echoes_are_swallowed_once(g))
    check("both drivers run the same frame",
          lambda: both_drivers_run_the_same_frame(g))
    check("the async driver stops when the game does",
          lambda: the_async_driver_stops_when_the_game_does(g))
    check("a browser keeps its saves in the browser",
          lambda: a_browser_keeps_its_saves_in_the_browser(g))
    check("a run survives a save and load in browser storage",
          lambda: a_run_survives_a_save_and_load_in_browser_storage(g))
    check("a browser guest joins a real room over a real socket",
          a_browser_guest_joins_a_real_room)
    check("a guest is told whose valley it is",
          a_guest_is_told_whose_valley_it_is)
    check("a far away guest does not drag the host in",
          a_far_away_guest_does_not_drag_the_host_in)
    check("a declined fight is handed back",
          a_declined_fight_is_handed_back)
    check("two players share one front room",
          two_players_share_one_front_room)
    check("a last stand runs in a room", a_last_stand_runs_in_a_room)
    check("a hoard runs in a room", a_hoard_runs_in_a_room)
    check("a room works through a relay", a_room_works_through_a_relay)
    check("a hoard plays to a score", lambda: a_hoard_plays_to_a_score(g))
    check("every mode starts in itself",
          lambda: every_mode_starts_in_itself(g))
    check("a long night plays to a score", a_long_night_plays_to_a_score)
    check("a long night runs in a room", a_long_night_runs_in_a_room)
    check("a warden is fought by the whole room",
          a_warden_is_fought_by_the_whole_room)
    check("a host that leaves ends the room",
          a_host_that_leaves_ends_the_room)
    check("clean quit", g.quit_game)
    pygame.quit()

    print("-" * 60)
    if FAILURES:
        print("FAILED: %d of %d" % (len(FAILURES), TOTAL))
        return 1
    print("All %d smoke checks passed." % TOTAL)
    return 0


if __name__ == "__main__":
    sys.exit(run())
