"""A tiny WebSocket relay so somebody behind CGNAT can still host.

Roadmap: *"a relay for CGNAT hosts"*, open since §32 and unblocked by §78.

**The problem.** A room code *is* an address (`net.encode_room`), which is
what lets co-op work with no server at all — and it stops working the
moment the host has no address anybody can dial. Carrier-grade NAT gives a
whole neighbourhood one public IP and no way to forward a port, and that
is what most phone tethering and a lot of home broadband now is.

**The shape of the fix, and why it is this shape.** Savvydev, 2026-09-06:

> i dont want ALL global servers running off of my pc, but you can still
> use FREE 3rd party tools for the global stuff

> if im hosting a party then it can run globally off of my pc, if that
> makes sense, or js use some free tools, up to you

So this is **a byte pipe and nothing else**. It knows nothing about the
game: not the protocol, not the players, not the world. The host is still
the authority on everything (§21), the guests still talk to the host, and
this only carries what they say. Run one anywhere with a public address —
any free tier that runs Python and terminates TLS — and point
`PIXEL_RPG_RELAY_URL` at it.

**Run it:**

    python tools/relay_server.py --port 8080

It has no dependencies beyond the standard library and `pixel_rpg.websock`
for framing. One process, one thread, `selectors` — a relay for four
players is not a system that needs a framework.

**What it can see.** Everything both ends send, because it is in the
middle of it. It is not encrypted end to end and this file will not
pretend otherwise: put it behind TLS (`wss://`) and treat whoever runs it
as a party to the conversation. Nothing secret crosses it — the game
sends positions, monsters and a serialised hero — but that is a fact
about the game, not a promise this makes.
"""
from __future__ import annotations

import argparse
import os
import selectors
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pixel_rpg import websock                            # noqa: E402

#: Room ids are the game's own spoken-aloud alphabet minus `#`, which is a
#: fragment delimiter in a URL and would be cut off before it arrived.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
ROOM_LEN = 6

#: Every one of these is a ceiling rather than a target. A relay with no
#: limits is a relay that is one bored person away from being a bill.
MAX_ROOMS = 200
MAX_PARKED = 4              # the host parks one per guest slot, plus one
MAX_PAYLOAD = 256 * 1024    # `net.MAX_FRAME`
MAX_QUEUE = 4 * 1024 * 1024
MAX_HANDSHAKE = 8 * 1024
PARK_TTL = 20 * 60.0        # a parked socket nobody ever joins
IDLE_TTL = 90.0             # a paired socket that has gone quiet
HANDSHAKE_TTL = 10.0


def valid_room(text):
    """Whether `text` is a room id and not something off the internet."""
    if not isinstance(text, str) or len(text) != ROOM_LEN:
        return False
    return all(ch in ALPHABET for ch in text)


def request_path(head):
    """The path out of an HTTP request head, or "" if there is not one."""
    try:
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
    except (UnicodeDecodeError, AttributeError, IndexError):
        return ""
    parts = first.split(" ")
    if len(parts) < 2 or parts[0].upper() != "GET":
        return ""
    return parts[1].split("?", 1)[0].split("#", 1)[0]


def read_route(path):
    """`(role, room)` for a relay path, or `(None, None)`.

    Two routes and no others: `/park/<ROOM>` is a host waiting for
    somebody, `/join/<ROOM>` is somebody arriving. **Anything else is not
    a route** — the rule §20 applies to a key off a socket, applied to a
    URL: it names one of two things or it names nothing.
    """
    parts = [p for p in str(path).split("/") if p]
    if len(parts) != 2:
        return None, None
    role, room = parts[0].lower(), parts[1].upper()
    if role not in ("park", "join") or not valid_room(room):
        return None, None
    return role, room


class Side:
    """One socket. Before it is paired it is a mouth; after, a pipe."""

    def __init__(self, sock, address):
        self.sock = sock
        self.address = address
        self.head = b""          # handshake bytes, before the upgrade
        self.buffer = b""        # frame bytes, after it
        self.out = b""
        self.role = None
        self.room = None
        self.partner = None
        self.upgraded = False
        self.opened = time.monotonic()
        self.touched = time.monotonic()
        self.closing = False

    def fileno(self):
        return self.sock.fileno()

    def push(self, data):
        if len(self.out) + len(data) > MAX_QUEUE:
            # A peer that will not read is a peer that is gone. Dropping is
            # the only honest answer: buffering for it is how one stuck
            # socket becomes the relay's memory.
            self.closing = True
            return
        self.out += data

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Relay:
    """Rooms, sockets, and the loop that moves bytes between them."""

    def __init__(self, port=8080, bind="0.0.0.0", quiet=False):
        self.selector = selectors.DefaultSelector()
        self.rooms = {}          # room -> [parked Side, ...]
        self.sides = {}          # fileno -> Side
        self.quiet = quiet
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((bind, int(port)))
        self.listener.listen(64)
        self.listener.setblocking(False)
        self.port = self.listener.getsockname()[1]
        self.selector.register(self.listener, selectors.EVENT_READ, None)

    def say(self, message):
        if not self.quiet:
            print(message, flush=True)

    # -- the loop ------------------------------------------------------
    def serve_forever(self):
        self.say("relay listening on %d" % self.port)
        while True:
            self.tick()

    def tick(self, timeout=0.5):
        for key, events in self.selector.select(timeout):
            if key.data is None:
                self.accept()
                continue
            side = key.data
            if events & selectors.EVENT_READ:
                self.readable(side)
            if events & selectors.EVENT_WRITE:
                self.writable(side)
        self.sweep()

    def accept(self):
        while True:
            try:
                sock, address = self.listener.accept()
            except (BlockingIOError, OSError):
                return
            sock.setblocking(False)
            side = Side(sock, address)
            self.sides[sock.fileno()] = side
            self.selector.register(sock, selectors.EVENT_READ, side)

    def readable(self, side):
        try:
            chunk = side.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            chunk = b""
        if not chunk:
            self.drop(side)
            return
        side.touched = time.monotonic()
        if not side.upgraded:
            side.head += chunk
            if len(side.head) > MAX_HANDSHAKE:
                self.drop(side)
                return
            if websock.HEADER_END not in side.head:
                return
            head, _, rest = side.head.partition(websock.HEADER_END)
            side.head = b""
            side.buffer = rest
            if not self.upgrade(side, head):
                return
        else:
            # **The `else` is the whole thing working.** Without it the
            # handshake's leftover is the only payload this socket ever
            # has: every byte that arrives after the upgrade is read off
            # the socket and dropped on the floor, and the relay pairs
            # two sockets that then sit in silence.
            side.buffer += chunk
        self.forward(side)

    def upgrade(self, side, head):
        """Answer the handshake and file the socket, or turn it away."""
        role, room = read_route(request_path(head))
        response = websock.handshake_response(websock.parse_request(head))
        if role is None or response is None:
            side.push(websock.reject_response("Not a relay route"))
            side.closing = True
            self.want_write(side)
            return False
        side.push(response)
        side.upgraded = True
        side.role, side.room = role, room
        self.want_write(side)
        if role == "park":
            waiting = self.rooms.setdefault(room, [])
            if len(self.rooms) > MAX_ROOMS or len(waiting) >= MAX_PARKED:
                # **Refused, not queued.** A relay that grows a room for
                # everybody who asks is a relay somebody fills up.
                side.push(websock.encode(b"", websock.OP_CLOSE))
                side.closing = True
                return True
            waiting.append(side)
            self.say("park %s (%d waiting)" % (room, len(waiting)))
            return True
        waiting = self.rooms.get(room) or []
        if not waiting:
            # Nobody is hosting that code. Say so by closing rather than by
            # hanging: a guest waiting on a room that does not exist is the
            # worst version of this.
            side.push(websock.encode(b"", websock.OP_CLOSE))
            side.closing = True
            self.say("join %s refused: nobody there" % room)
            return True
        host_side = waiting.pop(0)
        if not waiting:
            self.rooms.pop(room, None)
        side.partner, host_side.partner = host_side, side
        self.say("pair %s" % room)
        return True

    def forward(self, side):
        """Move whole messages across, and nothing else.

        **Message payloads, not bytes.** Both ends are WebSocket *clients*,
        so both mask what they send and neither will accept a masked frame
        back - the frames have to be taken apart and rebuilt rather than
        spliced. Doing it this way also means a peer cannot smuggle a
        half-frame through: what arrives here is a whole message or it is
        nothing yet.
        """
        while True:
            try:
                fin, opcode, payload, rest = websock.take_frame(
                    side.buffer, MAX_PAYLOAD, expect_mask=True)
            except websock.ProtocolError:
                self.drop(side)
                return
            if fin is None:
                return
            side.buffer = rest
            if opcode == websock.OP_CLOSE:
                self.drop(side)
                return
            if opcode == websock.OP_PING:
                side.push(websock.encode(payload, websock.OP_PONG))
                self.want_write(side)
                continue
            if opcode == websock.OP_PONG:
                continue
            partner = side.partner
            if partner is None:
                # A parked host that says something before anybody joins
                # has nowhere to say it to. Dropped rather than held.
                continue
            partner.push(websock.encode(payload, opcode))
            self.want_write(partner)

    def writable(self, side):
        if side.out:
            try:
                sent = side.sock.send(side.out)
                side.out = side.out[sent:]
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self.drop(side)
                return
        if not side.out:
            if side.closing:
                self.drop(side)
                return
            self.want_write(side, False)

    def want_write(self, side, on=True):
        events = selectors.EVENT_READ | (selectors.EVENT_WRITE if on else 0)
        try:
            self.selector.modify(side.sock, events, side)
        except (KeyError, ValueError, OSError):
            pass

    def drop(self, side):
        """Close one side, and the other with it.

        **A pipe with one end is not a pipe.** Leaving the partner open
        would leave a guest talking to a host that is not there, which
        looks exactly like a hung game.
        """
        for room, waiting in list(self.rooms.items()):
            if side in waiting:
                waiting.remove(side)
                if not waiting:
                    self.rooms.pop(room, None)
        try:
            self.selector.unregister(side.sock)
        except (KeyError, ValueError, OSError):
            pass
        self.sides.pop(side.sock.fileno(), None)
        side.close()
        partner, side.partner = side.partner, None
        if partner is not None and partner.partner is side:
            partner.partner = None
            self.drop(partner)

    def sweep(self):
        """Time out anything that is not going to finish.

        Three clocks, because there are three ways to be stuck: a socket
        that never finishes its handshake, a room nobody ever joins, and a
        pair that has gone silent. Every queue here has one - the lesson
        `Host._show_the_door` already wrote down.
        """
        now = time.monotonic()
        for side in list(self.sides.values()):
            if not side.upgraded and now - side.opened > HANDSHAKE_TTL:
                self.drop(side)
            elif (side.partner is None and side.role == "park"
                  and now - side.opened > PARK_TTL):
                self.drop(side)
            elif side.partner is not None and now - side.touched > IDLE_TTL:
                self.drop(side)

    def close(self):
        for side in list(self.sides.values()):
            self.drop(side)
        try:
            self.selector.unregister(self.listener)
        except (KeyError, ValueError, OSError):
            pass
        self.listener.close()
        self.selector.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8080")),
                        help="port to listen on (or $PORT)")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    relay = Relay(args.port, args.bind, args.quiet)
    try:
        relay.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        relay.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
