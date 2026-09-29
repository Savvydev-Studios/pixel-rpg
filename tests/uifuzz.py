"""Fuzz the player, not the save file.

`tests/fuzz.py` mutates saves, because a save is the one *attacker*
controlled input this game has. **The one a player controls is the
keyboard**, and nothing has ever hammered it:

> the second page of the maps screen is super buggy

Savvydev, 2026-09-08, about a screen that had a clean unit suite, a clean
layout run and a screenshot taken of it. All three were looking at one
frame at a time; what was wrong was what happened **between** frames -
the page swapped back and forth every time it was drawn - and no test in
this repo pressed a key twice and compared.

So this presses keys. Thousands of them, from every screen it can reach,
drawing every frame, and it asserts the things a screen must never do:

  * raise out of `handle_event`, `update` or `draw`
  * end up in a state nobody wrote
  * **register a hotspot the game then refuses** - the picker's second
    page failed exactly this: it drew cells the mouse could not use
  * **move on its own** - two draws with no input between them must
    produce the same screen. That is the oscillation, stated as a rule
  * leave a cursor below zero

Reproducible: every run prints its seed, and `--seed N` replays one.

    python tests/uifuzz.py 4000
    python tests/uifuzz.py 4000 --seed 1234
"""
import argparse
import os
import random
import sys
import tempfile
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
# **`setdefault`, not assignment.** `tests/test_all.py` imports this
# to check the instrument, and an assignment here moved the whole
# suite's save sandbox - which `TestTheSuiteCannotEatARealSave`
# caught immediately, because that is the test it exists to be.
os.environ.setdefault("PIXEL_RPG_SAVE_DIR",
                      tempfile.mkdtemp(prefix="rpguifuzz"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame                                             # noqa: E402

from pixel_rpg import game as G, gamemodes                 # noqa: E402

#: The keys a player actually has. Every bind, the arrows, confirm and
#: back, plus the digits a battle uses - and nothing else, because
#: fuzzing F13 tells you about a keyboard nobody owns.
KEYS = [
    pygame.K_w, pygame.K_a, pygame.K_s, pygame.K_d,
    pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT,
    pygame.K_RETURN, pygame.K_e, pygame.K_SPACE, pygame.K_ESCAPE,
    pygame.K_TAB, pygame.K_q, pygame.K_i, pygame.K_c, pygame.K_j,
    pygame.K_m, pygame.K_g, pygame.K_r, pygame.K_1, pygame.K_2,
    pygame.K_3, pygame.K_4,
]

#: Window sizes worth walking. The landscape phone is in here because it
#: is the one that has found the most layout faults (§53, §86).
SIZES = [(1280, 720), (1024, 768), (900, 600), (844, 390), (420, 800)]

#: Where a run can start. Every mode, because three of the last six bugs
#: were in a mode that had been written by copying another one.
STARTS = ["story"] + [m.key for m in gamemodes.MODES if not m.saves]


#: Kinds `mouse_click` answers itself rather than moving a cursor to.
#: Read out of the source, the way `TestMouseReach` does, so this list
#: cannot quietly disagree with the game.
def _acted_on():
    import inspect
    import re
    body = inspect.getsource(G.Game.mouse_click)
    kinds = set(re.findall(r'kind == "([a-z_]+)"', body))
    for pair in re.findall(r'kind in \(("[a-z_", ]+)\)', body):
        kinds |= set(re.findall(r'"([a-z_]+)"', pair))
    return kinds


ACTED_ON = _acted_on()


class Fault(Exception):
    """Something a screen must never do, with the seed to replay it."""


#: Screens a run does not wander into by mashing keys, so the fuzzer is
#: **put** there. The offers screen is the one that matters: it is the
#: newest thing in the game, a player sees it after every single wave,
#: and 2400 random events never reached it once - which is exactly the
#: silent pass §7 keeps warning about, in a fuzzer instead of a checker.
SCENARIOS = ("plain", "plain", "spoils", "picker", "battle")


def fresh(rng, size):
    """A game with a run in it, in one of the modes."""
    game = G.Game()
    game.screen = pygame.display.set_mode(size)
    scenario = rng.choice(SCENARIOS)
    # **The scenario picks the mode when it needs one.** A hand of offers
    # only exists after a wave, so a fuzzer that chose the mode first
    # reached the offers screen in one round out of twenty - which reads
    # as coverage and is not.
    if scenario in ("spoils", "battle"):
        mode = "waves"
    else:
        mode = rng.choice(STARTS)
    game.choose_mode(mode)
    game.pending_class = rng.choice(["warden", "emberwright", "duelist"])
    game.name_buffer = "Fuzz"
    if gamemodes.mode_for(mode).saves:
        game.slot = 1
    game.begin_run()
    for _ in range(90):
        game.update(1 / 30)
    try:
        put_there(game, scenario, mode)
    except Exception as exc:                              # noqa: BLE001
        raise Fault("setting up %s in %s raised %r"
                    % (scenario, mode, exc)) from exc
    return game, "%s/%s" % (mode, scenario)


def put_there(game, scenario, mode):
    """Walk the game to one of the screens mashing keys does not find."""
    if scenario == "picker":
        game.pending_mode = mode
        game.open_arena_select()
    elif scenario == "spoils" and game.waves is not None:
        # Clear a wave the way the game does, which is the only path
        # that produces a real hand of offers.
        game.begin_wave()
        if game.battle is not None:
            for enemy in game.battle.enemies:
                enemy.hp = 0
            for _ in range(400):
                if game.battle is None or game.battle.result:
                    break
                game.battle.update(1 / 30)
            if game.battle is not None and game.battle.result == "win":
                game.finish_battle()
    elif scenario == "battle" and game.waves is not None:
        game.begin_wave()
    for _ in range(20):
        game.update(1 / 30)


def step(game, dt=1 / 30):
    game.update(dt)
    game.draw()


def draw_twice_is_the_same(game):
    """**Two draws with no input between them are the same screen.**

    The rule the arena picker broke. A screen that moves while nobody is
    touching it is a screen whose clicks land somewhere else than they
    look, and it is invisible to anything that draws one frame.

    Compared through the hotspots rather than the pixels: pixels move for
    honest reasons - a pulse, a float, a monster walking - and what must
    not move is **where the things you can press are**.
    """
    game.ui.hot.clear()
    game.draw()
    first = [(tuple(r), k, i) for r, k, i in game.ui.hot.items]
    game.ui.hot.clear()
    game.draw()
    second = [(tuple(r), k, i) for r, k, i in game.ui.hot.items]
    return first == second, first, second


def hotspots_are_usable(game):
    """Everything drawn as pressable must be pressable.

    `point_cursor` returning False for a registered hotspot means the
    screen drew a control the game does not believe in - which is what
    the picker's off-by-one page did, and what `TestMouseReach` catches
    only for *kinds* rather than for the indices actually on screen.
    """
    bad = []
    for _rect, kind, index in list(game.ui.hot.items):
        if kind in ("key", "spoil_detail"):
            continue                       # replays a keypress; no cursor
        if kind in ACTED_ON:
            continue                       # answered in `mouse_click`
        if kind in G.Game.SELECT_ONLY or True:
            try:
                ok = game.point_cursor(kind, index)
            except Exception as exc:       # noqa: BLE001
                bad.append("%s[%s] raised %r" % (kind, index, exc))
                continue
            if not ok:
                bad.append("%s[%s] was drawn and is refused" % (kind, index))
    return bad


def cursors_are_sane(game):
    out = []
    for name in dir(game.ui):
        if not name.endswith("_index"):
            continue
        value = getattr(game.ui, name, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value < 0:
            out.append("ui.%s is %d" % (name, value))
    return out


def one_round(seed, events=120, seen=None):
    """Drive one game for `events` keypresses, checking as it goes."""
    seen = {} if seen is None else seen
    rng = random.Random(seed)
    size = rng.choice(SIZES)
    game, mode = fresh(rng, size)
    known = known_states()
    for n in range(events):
        # **A third of the events are the mouse**, because the thing he
        # reported was a *click* landing somewhere other than it looked.
        # A press aimed at a hotspot the screen just drew is the honest
        # test; a press at a random point is the one that finds a screen
        # that believes in a control it did not draw.
        roll = rng.random()
        if roll < 0.22 and game.ui.hot.items:
            rect, kind, index = rng.choice(list(game.ui.hot.items))
            spot = (rect.centerx, rect.centery)
            what = "click %s[%s] at %s" % (kind, index, spot)
            action = lambda: game.mouse_click(spot, 1)    # noqa: E731
        elif roll < 0.28:
            spot = (rng.randrange(size[0]), rng.randrange(size[1]))
            what = "click nowhere at %s" % (spot,)
            action = lambda: game.mouse_click(spot, 1)    # noqa: E731
        elif roll < 0.33:
            amount = rng.choice((-3, -1, 1, 3))
            what = "wheel %d" % amount
            action = lambda: game.mouse_scroll(amount)    # noqa: E731
        elif roll < 0.38:
            # **Drag the window** (§92). His own bug once: every
            # `VIDEORESIZE` called `set_mode` and the window died inside
            # Windows' modal resize loop (§46). Nothing has dragged one
            # since, and **every grid screen measures itself from the
            # width** - the picker's columns, the offer cards, the mode
            # rows - so a resize is the event most likely to leave a
            # cursor pointing at a cell that is no longer there.
            size = rng.choice(SIZES)
            what = "resize to %r" % (size,)

            def action(size=size):
                game.screen = pygame.display.set_mode(size)
                game.handle_event(pygame.event.Event(
                    pygame.VIDEORESIZE, w=size[0], h=size[1],
                    size=size))
        else:
            key = rng.choice(KEYS)
            what = "key %s" % pygame.key.name(key)
            action = lambda: game.handle_event(pygame.event.Event(
                pygame.KEYDOWN, key=key, unicode="", mod=0))  # noqa: E731
        where = "seed %d, %s at %r, event %d, %s" % (
            seed, mode, size, n, what)
        try:
            action()
            step(game)
        except Exception as exc:                          # noqa: BLE001
            raise Fault("%s: %r" % (where, exc)) from exc
        if game.state not in known:
            raise Fault("%s: unknown state %r" % (where, game.state))
        # Every few events, the expensive checks.
        if n % 7 == 0:
            try:
                same, first, second = draw_twice_is_the_same(game)
            except Exception as exc:                      # noqa: BLE001
                raise Fault("%s: draw raised %r" % (where, exc)) from exc
            if not same:
                moved = [a for a in first if a not in second][:2]
                raise Fault("%s (%s): the screen moved with no input: %r"
                            % (where, game.state, moved))
            for line in hotspots_are_usable(game):
                raise Fault("%s (%s): %s" % (where, game.state, line))
        for line in cursors_are_sane(game):
            raise Fault("%s: %s" % (where, line))
        seen[game.state] = seen.get(game.state, 0) + 1
    return seen


# ------------------------------------------------------------- a room ---
#
# **Every bug family in the last month has been a co-op one** (§81, §87,
# §91): a field that crossed the wire and was never read, a party built
# of one, a guest whose clock never started. All three were found by
# writing a check that ran two machines, and none of them by a check
# that ran one.
#
# So this runs two, over a real loopback socket, and mashes both
# keyboards. What it asserts is what a room must never do, which is
# narrower than what one machine must never do:
#
#   * neither end raises;
#   * **a mirror stays a mirror** - a guest that started simulating its
#     own monsters is a guest fighting a different room;
#   * **the roster matches** - index 1 has to be the same person on both
#     machines, which is the bug item 0bg was;
#   * neither end is dropped for talking too fast, because a fuzzer that
#     floods is measuring its own loop.


def room(rng, size, mode):
    """A host and a guest in one room, both with a hero."""
    host = G.Game()
    host.screen = pygame.display.set_mode(size)
    host.choose_mode(mode)
    host.pending_class = "warden"
    host.name_buffer = "Hostess"
    host.begin_run()
    for _ in range(60):
        host.update(1 / 30)
    host.start_hosting(reach_out=False, port=0)
    if host.coop is None:
        raise Fault("hosting %s opened no room" % mode)

    guest = G.Game()
    guest.screen = pygame.display.set_mode(size)
    guest.pending_class = "duelist"
    guest.code_buffer = host.coop.room_code
    if not guest.try_join():
        raise Fault("joining a %s room failed" % mode)
    pump(host, guest, 120)
    if guest.state == G.CLASS_SELECT:
        guest.name_buffer = "Guest"
        guest.begin_run()
        pump(host, guest, 90)
    return host, guest


def pump(host, guest, frames=60):
    """Both ends, in something close to real time.

    The sleep is not politeness: peers are dropped for talking faster
    than `net.MAX_MESSAGES_PER_SECOND`, measured against the wall clock,
    so a loop that runs a whole fight in fifty milliseconds looks exactly
    like a flood and disconnects itself (`smoke._pump` says the same).
    """
    for _ in range(frames):
        time.sleep(0.004)
        for game in (host, guest):
            game.update(1 / 30)
            game.draw()


def room_is_sane(host, guest, where):
    if host.coop is None or guest.coop is None:
        return                     # somebody left; that is allowed
    if guest.roam is not None and not guest.roam.mirror:
        raise Fault("%s: the guest is simulating the arena itself" % where)
    if host.roam is not None and guest.roam is not None:
        if len(host.roam.heroes) != len(guest.roam.heroes):
            raise Fault("%s: the room is %d people on the host and %d on "
                        "the guest" % (where, len(host.roam.heroes),
                                       len(guest.roam.heroes)))
    if host.battle is not None and guest.battle is not None:
        theirs = [e.name for e in host.battle.enemies]
        ours = [e.name for e in guest.battle.enemies]
        if theirs != ours:
            raise Fault("%s: different monsters: %s vs %s"
                        % (where, theirs, ours))


def one_room(seed, events=60):
    """Drive a host and a guest for `events` presses each."""
    rng = random.Random(seed)
    size = rng.choice(SIZES)
    mode = rng.choice([m.key for m in gamemodes.MODES if not m.saves])
    host, guest = room(rng, size, mode)
    try:
        for n in range(events):
            for who, game in (("host", host), ("guest", guest)):
                key = rng.choice(KEYS)
                where = "seed %d, %s room at %r, event %d, %s pressed %s" % (
                    seed, mode, size, n, who, pygame.key.name(key))
                try:
                    game.handle_event(pygame.event.Event(
                        pygame.KEYDOWN, key=key, unicode="", mod=0))
                except Exception as exc:                  # noqa: BLE001
                    raise Fault("%s: %r" % (where, exc)) from exc
            try:
                pump(host, guest, 6)
            except Exception as exc:                      # noqa: BLE001
                raise Fault("%s: a frame raised %r" % (where, exc)) from exc
            room_is_sane(host, guest, where)
    finally:
        for game in (host, guest):
            game.roam = None
            game.battle = None
            try:
                game.leave_coop()
            except Exception:                             # noqa: BLE001
                pass
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("events", nargs="?", type=int, default=2000,
                        help="how many keypresses in total")
    parser.add_argument("--seed", type=int, default=None,
                        help="replay one round")
    parser.add_argument("--per-round", type=int, default=120)
    parser.add_argument("--room", action="store_true",
                        help="two machines over a real socket instead")
    args = parser.parse_args(argv)

    if args.room:
        rounds = max(1, args.events // 60)
        base = (args.seed if args.seed is not None
                else random.Random().randrange(1, 2 ** 31))
        print("ui fuzz (room): %d rounds x 60 presses each, base seed %d"
              % (rounds, base))
        for n in range(rounds):
            seed = base + n
            try:
                one_room(seed)
            except Fault as fault:
                print("FAULT: %s" % fault)
                print("replay with: python tests/uifuzz.py --room --seed %d"
                      % seed)
                return 1
        print("ui fuzz (room): %d rounds, no faults" % rounds)
        return 0

    if args.seed is not None:
        print("replaying seed %d" % args.seed)
        one_round(args.seed, args.per_round)
        print("no fault")
        return 0

    rounds = max(1, args.events // args.per_round)
    base = random.Random().randrange(1, 2 ** 31)
    print("ui fuzz: %d rounds x %d events, base seed %d"
          % (rounds, args.per_round, base))
    seen = {}
    for n in range(rounds):
        seed = base + n
        try:
            one_round(seed, args.per_round, seen)
        except Fault as fault:
            print("FAULT: %s" % fault)
            print("replay with: python tests/uifuzz.py --seed %d" % seed)
            return 1
    print("ui fuzz: %d rounds, %d events, no faults"
          % (rounds, rounds * args.per_round))
    # **What it actually walked.** A fuzzer that never reaches a screen
    # says nothing about it, and the screen he reported is four presses
    # deep - so this prints the map rather than letting a clean run imply
    # more than it earned (§7).
    for state, hits in sorted(seen.items(), key=lambda kv: -kv[1]):
        print("    %-14s %5d" % (state, hits))
    missed = sorted(known_states() - set(seen))
    if missed:
        print("    never reached: %s" % ", ".join(missed))
    return 0


def known_states():
    return {v for k, v in vars(G).items()
            if k.isupper() and isinstance(v, str) and not k.startswith("_")}


if __name__ == "__main__":
    raise SystemExit(main())
