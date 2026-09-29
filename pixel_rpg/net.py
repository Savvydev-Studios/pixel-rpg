"""Co-op transport: length-framed JSON over TCP, pumped from the game loop.

This module is the first thing in the project that reads bytes written by
another machine. `SECURITY.md` opens by saying this is an offline game with no
endpoints; that stops being true here, so the rules are written down before
the first line of protocol:

1. **Never block the loop.** Sockets are non-blocking and drained by
   `pump()` once a frame. A network layer that can stall the game at 60fps is
   a network layer that will, on somebody's worse connection.
2. **The host is the authority.** A client sends *intent* - "I pressed left",
   "I chose Attack" - and never state. It cannot tell the host its own health,
   its gold, or that it killed something. Anything else is a client that can
   edit the shared run.
3. **Every frame is bounded before it is trusted.** The length prefix is
   checked against `MAX_FRAME` before a single byte is buffered, so a peer
   cannot make us allocate a gigabyte by claiming to.
4. **JSON only, never pickle.** Pickle over a socket is remote code
   execution wearing a hat.
5. **Every field is validated.** Payloads go through `security.py` exactly
   like save files do, because they are exactly as untrusted.

**Two transports, one port.** A peer that opens with `GET ` is speaking HTTP
and gets a WebSocket handshake (`websock.py`); anything else is the original
length-prefixed framing. They cannot be confused: the raw protocol's first
four bytes are a big-endian length, and a length spelling `GET ` would be
1,195,725,856 bytes - forty times over `MAX_FRAME` and refused on sight.
Which one a peer chose lives entirely inside `_Peer`, so `Host`, `coop.py`
and the game never learn there is more than one.

That exists because a browser cannot speak raw TCP, so a phone could not
otherwise even join. See section 18 of the handoff for why a phone can never
*host*, whatever we do here.

Nothing here knows what a Player is. It moves dictionaries; `coop.py` decides
what they mean.
"""
from __future__ import annotations

import errno
import json
import socket
import struct
import time

from . import security as sec
from . import relay
from . import websock

# **Not the one the handshake uses.** `coop.PROTOCOL` is the session
# version peers actually compare (3, at the time of writing); this is the
# transport's own and nothing reads it. Two constants of the same name in
# adjacent modules with different values, and sending the wrong one gets
# you "different version of the game", which reads exactly like a real
# mismatch. It cost a debugging cycle writing the browser-guest check.
PROTOCOL = 1
DEFAULT_PORT = 47216
# A frame big enough for a world handshake and nothing like big enough to
# hurt. Checked before any buffering.
MAX_FRAME = 256 * 1024
MAX_CLIENTS = 3               # four players including the host
HEADER = struct.Struct("!I")
# Roughly one full second of a 60fps client shouting every frame, doubled.
MAX_MESSAGES_PER_SECOND = 120
# How long a peer being turned away is given to finish its handshake and
# hear why. Long enough for a WebSocket upgrade, short enough that a silent
# socket cannot camp.
REJECT_GRACE = 2.0

# Room codes avoid characters people mistype reading them aloud: no O/0, no
# I/1/L. What is left is 32 symbols, which is exactly one base32 alphabet.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ#"
_DECODE = {ch: i for i, ch in enumerate(ALPHABET)}


class NetError(Exception):
    """Anything the caller should show the player rather than crash on."""


# -- room codes ---------------------------------------------------------------

def encode_room(host, port=DEFAULT_PORT):
    """Pack an IPv4 address and port into a short spoken-aloud code.

    Six bytes becomes ten characters. This is what lets a room code exist
    with no server behind it: the code *is* the address.
    """
    try:
        packed = socket.inet_aton(host)
    except OSError as exc:
        raise NetError("not an IPv4 address: %s" % host) from exc
    value = int.from_bytes(packed + struct.pack("!H", int(port)), "big")
    out = []
    for _ in range(10):
        out.append(ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def decode_room(code):
    """Unpack a room code. Returns (host, port)."""
    cleaned = "".join(ch for ch in str(code).upper()
                      if ch not in " -_\t").strip()
    if len(cleaned) != 10:
        raise NetError("a room code is ten characters")
    value = 0
    for ch in cleaned:
        if ch not in _DECODE:
            raise NetError("%r is not part of a room code" % ch)
        value = (value << 5) | _DECODE[ch]
    raw = value.to_bytes(6, "big")
    return socket.inet_ntoa(raw[:4]), struct.unpack("!H", raw[4:])[0]


def local_address():
    """This machine's address on the LAN, or loopback if it has none.

    Uses a UDP socket that never sends anything: connecting a datagram socket
    only picks a route, which is all we want to know.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))       # TEST-NET-1, reserved, unroutable
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


# -- framing ------------------------------------------------------------------

def encode_body(kind, payload=None):
    """One message as JSON bytes, before any transport wraps it.

    Split out from `encode_frame` when a second transport arrived: the
    message is the same either way, and only the wrapper differs. `Host`
    builds the body once per broadcast and lets each peer dress it, which is
    also why a room can hold a desktop client and a browser at once.
    """
    body = json.dumps({"t": str(kind), "d": payload if payload is not None
                       else {}}, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise NetError("outgoing frame too large")
    return body


def encode_frame(kind, payload=None):
    """A message wrapped for the original length-prefixed transport."""
    body = encode_body(kind, payload)
    return HEADER.pack(len(body)) + body


# How a peer is framing its messages.
MODE_UNKNOWN = "?"       # nothing has arrived yet; we do not know
MODE_TCP = "tcp"         # 4-byte length prefix, the original
MODE_WS = "ws"           # RFC 6455, serving a browser
# The guest end of the same protocol. Separate from MODE_WS because the two
# directions are not symmetric: a server never masks and refuses an unmasked
# client, a client always masks and refuses a masked server.
MODE_WS_CLIENT = "wsc"


class _RateWindow:
    """One second of message counting, and the only copy of the limit.

    Lifted out of `_Peer` when `BrowserClient` arrived. A guest that owns
    no socket still has to refuse a host that floods it, and a second copy
    of a limit is how one of them quietly stops matching the other.
    """

    def __init__(self):
        self._window_start = time.monotonic()
        self._window_count = 0

    def over_rate(self):
        """True when this peer is talking faster than any client should."""
        now = time.monotonic()
        if now - self._window_start >= 1.0:
            self._window_start = now
            self._window_count = 0
        self._window_count += 1
        return self._window_count > MAX_MESSAGES_PER_SECOND


class _Peer(_RateWindow):
    """One socket, the half-read bytes in front of it, and how it speaks.

    The transport is decided from the first bytes to arrive and then never
    reconsidered. Everything above this class - `Host`, `coop.py`, the game -
    deals in messages and never learns which one a peer chose.
    """

    def __init__(self, sock, address):
        self.sock = sock
        self.address = address
        self.buffer = b""
        self.outbox = b""
        self.alive = True
        self.name = ""
        self.player_id = 0
        self.mode = MODE_UNKNOWN
        # Messages queued before the transport was known. A refusal is the
        # common case: the room is full and we want to say so, but we cannot
        # dress the words until we know which language they are in. On the
        # guest side it is instead everything said before the upgrade lands.
        self.pending = []
        self._ws_upgraded = False   # guest side: has the 101 come back yet
        self._ws_partial = b""      # a fragmented message being reassembled
        self._ws_opcode = None
        self.ws_key = ""            # guest side: what the 101 must answer
        _RateWindow.__init__(self)
        sock.setblocking(False)

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, data):
        """Raw bytes, already wrapped. Used by callers that built a frame."""
        if not self.alive:
            return
        self.outbox += data
        self.flush()

    def send_message(self, body):
        """One message body, wrapped for whichever transport this peer uses.

        Held rather than guessed while the transport is unknown: a browser
        handed a length-prefixed frame sees garbage and reports a broken
        host, which is a worse answer than a moment's wait.
        """
        if not self.alive:
            return
        if self.mode == MODE_UNKNOWN:
            self.pending.append(body)
            return
        if self.mode == MODE_WS:
            self.send(websock.encode(body, websock.OP_TEXT))
        elif self.mode == MODE_WS_CLIENT:
            if not self._ws_upgraded:
                # The upgrade is still in flight. Framing now would put
                # WebSocket bytes in front of an HTTP response.
                self.pending.append(body)
                return
            self.send(websock.encode_masked(body, websock.OP_TEXT))
        else:
            self.send(HEADER.pack(len(body)) + body)

    def _settle(self, mode):
        """Fix the transport and let anything held go out."""
        self.mode = mode
        held, self.pending = self.pending, []
        for body in held:
            self.send_message(body)

    def flush(self):
        while self.outbox and self.alive:
            try:
                sent = self.sock.send(self.outbox)
            except (BlockingIOError, InterruptedError):
                return                       # the kernel is full; try later
            except OSError:
                self.close()
                return
            if sent <= 0:
                self.close()
                return
            self.outbox = self.outbox[sent:]

    def read(self):
        """Drain the socket and yield whole messages. Never blocks."""
        try:
            chunk = self.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            chunk = b""
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                self.close()
            chunk = b""
        else:
            if not chunk:
                self.close()
        if chunk:
            self.buffer += chunk

        if self.mode == MODE_UNKNOWN and not self._sniff():
            return
        if self.mode == MODE_WS_CLIENT and not self._await_upgrade():
            return
        if self.mode in (MODE_WS, MODE_WS_CLIENT):
            for message in self._read_ws():
                yield message
            return

        while len(self.buffer) >= HEADER.size:
            (length,) = HEADER.unpack(self.buffer[:HEADER.size])
            # Checked *before* waiting for the body, so a peer claiming a
            # gigabyte costs us nothing at all.
            if length > MAX_FRAME:
                self.close()
                return
            if len(self.buffer) < HEADER.size + length:
                return
            body = self.buffer[HEADER.size:HEADER.size + length]
            self.buffer = self.buffer[HEADER.size + length:]
            if self.over_rate():
                self.close()
                return
            message = _decode_body(body)
            if message is not None:
                yield message

    def _sniff(self):
        """Decide which transport this peer is using. True once settled.

        Called until it succeeds, which for a WebSocket means until the whole
        request head has arrived. The two protocols cannot be mistaken for
        each other - see the note at the top of this module.
        """
        if len(self.buffer) < 4:
            return False                    # not enough yet to tell
        if not websock.looks_like_http(self.buffer[:4]):
            self._settle(MODE_TCP)
            return True

        end = self.buffer.find(websock.HEADER_END)
        if end < 0:
            # A handshake is a few hundred bytes. Anything much larger is
            # somebody seeing how much we will hold, and the check happens
            # while reading rather than after.
            if len(self.buffer) > websock.MAX_HANDSHAKE:
                self.close()
            return False
        head, self.buffer = self.buffer[:end], self.buffer[end + 4:]
        response = websock.handshake_response(websock.parse_request(head))
        if response is None:
            # A browser deserves a status line rather than a dropped socket.
            try:
                self.sock.send(websock.reject_response())
            except OSError:
                pass
            self.close()
            return False
        self.send(response)
        self._settle(MODE_WS)
        return True

    def _dress(self, payload, opcode):
        """Wrap a control frame for whichever end of the protocol we are."""
        if self.mode == MODE_WS_CLIENT:
            return websock.encode_masked(payload, opcode)
        return websock.encode(payload, opcode)

    def _dress_close(self):
        if self.mode == MODE_WS_CLIENT:
            return websock.masked_close_frame()
        return websock.close_frame()

    def _await_upgrade(self):
        """Guest side: wait for the 101, then let anything held go out.

        A server that answers 101 with the wrong accept value is one that did
        not read our request - a cache or a proxy replying on its behalf - and
        framing WebSocket at it would be shouting into a pipe nobody is
        holding. Refused rather than continued.
        """
        if self._ws_upgraded:
            return True
        end = self.buffer.find(websock.HEADER_END)
        if end < 0:
            if len(self.buffer) > websock.MAX_HANDSHAKE:
                self.close()
            return False
        head, self.buffer = self.buffer[:end], self.buffer[end + 4:]
        if not websock.check_handshake(head, self.ws_key):
            self.close()
            return False
        self._ws_upgraded = True
        held, self.pending = self.pending, []
        for body in held:
            self.send_message(body)
        return True

    def _read_ws(self):
        """Yield whole messages from the WebSocket buffer.

        Fragmentation is handled rather than refused: a browser is entitled
        to split a message and some proxies do it for fun. The reassembled
        size is bounded exactly like a single frame, so a peer cannot get
        past the ceiling by arriving in pieces.
        """
        while self.alive:
            try:
                fin, opcode, payload, rest = websock.take_frame(
                    self.buffer, MAX_FRAME,
                    expect_mask=self.mode == MODE_WS)
            except websock.ProtocolError:
                self.close()
                return
            if fin is None:
                return                       # not all here yet
            self.buffer = rest

            if opcode == websock.OP_CLOSE:
                try:
                    self.send(self._dress_close())
                except OSError:
                    pass
                self.close()
                return
            if opcode == websock.OP_PING:
                self.send(self._dress(payload, websock.OP_PONG))
                continue
            if opcode == websock.OP_PONG:
                continue

            if opcode == websock.OP_CONT:
                if self._ws_opcode is None:
                    self.close()             # a continuation of nothing
                    return
                self._ws_partial += payload
            else:
                self._ws_opcode = opcode
                self._ws_partial = payload
            if len(self._ws_partial) > MAX_FRAME:
                self.close()
                return
            if not fin:
                continue

            body, self._ws_partial = self._ws_partial, b""
            self._ws_opcode = None
            if self.over_rate():
                self.close()
                return
            message = _decode_body(body)
            if message is not None:
                yield message


def _decode_body(body):
    """Turn bytes off the wire into a (kind, dict). None if it is nonsense."""
    try:
        raw = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    kind = sec.safe_str(raw.get("t"), 32)
    if not kind:
        return None
    data = raw.get("d")
    if not isinstance(data, dict):
        data = {}
    try:
        sec.check_structure(data)
    except sec.ValidationError:
        return None
    return kind, data


# -- host ---------------------------------------------------------------------

class Host:
    """Listens for players. Owns nothing about the game itself."""

    def __init__(self, port=DEFAULT_PORT, bind="0.0.0.0"):
        self.port = int(port)
        self.peers = []
        self.inbox = []            # [(peer, kind, data)]
        self.events = []           # ("join"|"leave", peer)
        # Peers being turned away. They are held only long enough to learn
        # their transport, hear "full" in it, and go.
        self._rejecting = []
        self._next_id = 2          # the host is player 1
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((bind, self.port))
            self.sock.listen(MAX_CLIENTS + 2)
            self.sock.setblocking(False)
            # Port 0 asks the OS to choose one, so read back what it chose:
            # otherwise the room code would advertise port zero.
            self.port = self.sock.getsockname()[1]
        except OSError as exc:
            raise NetError("could not open port %d: %s" % (self.port, exc))

    @property
    def room_code(self):
        return encode_room(local_address(), self.port)

    def pump(self):
        """One frame's worth of network. Returns the messages that arrived."""
        self._accept()
        self._show_the_door()
        self.inbox = []
        for peer in list(self.peers):
            peer.flush()
            for kind, data in peer.read():
                self.inbox.append((peer, kind, data))
            if not peer.alive:
                self.peers.remove(peer)
                self.events.append(("leave", peer))
        return self.inbox

    def _show_the_door(self):
        """Let a refused peer finish its handshake, hear "full", and leave.

        Bounded by a deadline rather than by politeness: a socket that opens
        and then says nothing must not be able to sit in this list for ever,
        which is the shape every "just until it finishes" queue takes when
        nobody gives it a clock.
        """
        now = time.monotonic()
        for entry in list(self._rejecting):
            peer, since = entry
            for _ in peer.read():
                pass                          # drives the handshake; discard
            peer.flush()
            done = not peer.alive or (peer.mode != MODE_UNKNOWN
                                      and not peer.outbox)
            if done or now - since > REJECT_GRACE:
                peer.close()
                self._rejecting.remove(entry)

    def _accept(self):
        while True:
            try:
                sock, address = self.sock.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            peer = _Peer(sock, address)
            if len(self.peers) >= MAX_CLIENTS:
                # Turned away, but not before we know how they speak: a
                # browser handed a length-prefixed refusal sees garbage and
                # reports a broken host rather than a full room. The message
                # is queued and goes out the moment the transport settles.
                peer.send_message(encode_body("full", {}))
                self._rejecting.append((peer, time.monotonic()))
                continue
            peer.player_id = self._next_id
            self._next_id += 1
            self.peers.append(peer)
            self.events.append(("join", peer))

    def send(self, peer, kind, payload=None):
        peer.send_message(encode_body(kind, payload))

    def broadcast(self, kind, payload=None, skip=None):
        """One body, wrapped once per peer.

        Built here rather than in the loop because the *message* is the same
        for everybody; only the wrapper differs. That is also what lets a
        room hold a desktop client and a browser at the same time without
        anything above this line knowing.
        """
        body = encode_body(kind, payload)
        for peer in self.peers:
            if peer is not skip:
                peer.send_message(body)

    def drop(self, peer, reason=""):
        if reason:
            try:
                peer.send_message(encode_body("bye", {"reason": reason[:80]}))
                peer.flush()
            except (OSError, NetError):
                pass
        peer.close()

    def close(self):
        for peer in list(self.peers):
            peer.close()
        for peer, _since in list(self._rejecting):
            peer.close()
        self._rejecting = []
        self.peers = []
        try:
            self.sock.close()
        except OSError:
            pass


class RelayHost:
    """A host that **dials out** instead of listening (`relay.py`).

    Same surface as `Host` - `peers`, `inbox`, `events`, `room_code`,
    `pump`, `send`, `broadcast`, `drop`, `close` - so `coop.py` picks one
    at the door and never thinks about it again, exactly as it already
    does for the two guest transports.

    **How it works.** There is no `accept` here because there is nothing
    to accept: the host opens one connection per free slot to the relay
    and leaves them **parked**. A parked socket is silent until the relay
    ties a guest to it, so *the first message to arrive on one is a guest
    arriving* - and that is the whole of the join detection.

    The alternative was multiplexing every guest down one socket, which
    would mean a second framing layer, a stream id on every message, and
    a new way for two players to be confused for each other. Parking
    costs three idle sockets and changes nothing above this line.
    """

    def __init__(self, room=None, slots=MAX_CLIENTS):
        self.room = relay.clean(room) if room else relay.new_room()
        self.slots = int(slots)
        self.peers = []
        self.inbox = []
        self.events = []
        self._parked = []
        self._rejecting = []
        self._next_id = 2
        self._next_try = 0.0
        if not relay.enabled():
            raise NetError("no relay is configured")
        self._park()
        if not self._parked:
            raise NetError("could not reach the relay")

    @property
    def port(self):
        """There is no port. Kept so callers that log one do not care."""
        return 0

    @property
    def room_code(self):
        return self.room

    def _park(self):
        """Keep one spare connection open per free slot.

        **Blocking, and deliberately rare.** Opening a socket takes as
        long as it takes; doing it every frame would hitch the game every
        frame, so this only runs when a slot has actually opened up and
        no more than once every `PARK_COOLDOWN` seconds. A relay that is
        down costs one stall and then leaves the room as it is.
        """
        now = time.monotonic()
        if now < self._next_try:
            return
        want = max(0, self.slots - len(self.peers))
        while len(self._parked) < want:
            try:
                sock, key = relay.connect(self.room, "park")
            except OSError:
                self._next_try = now + self.PARK_COOLDOWN
                return
            peer = _Peer(sock, (self.room, 0))
            peer.mode = MODE_WS_CLIENT
            peer.ws_key = key
            self._parked.append(peer)

    #: How long to wait before trying the relay again after it refuses.
    PARK_COOLDOWN = 5.0

    def pump(self):
        """One frame's worth. A parked socket that speaks is a guest."""
        self._park()
        self._show_the_door()
        self.inbox = []
        for peer in list(self._parked):
            peer.flush()
            messages = list(peer.read())
            if not peer.alive:
                self._parked.remove(peer)
                continue
            if not messages:
                continue
            self._parked.remove(peer)
            if len(self.peers) >= self.slots:
                peer.send_message(encode_body("full", {}))
                self._rejecting.append((peer, time.monotonic()))
                continue
            peer.player_id = self._next_id
            self._next_id += 1
            self.peers.append(peer)
            self.events.append(("join", peer))
            for kind, data in messages:
                self.inbox.append((peer, kind, data))
        for peer in list(self.peers):
            peer.flush()
            for kind, data in peer.read():
                self.inbox.append((peer, kind, data))
            if not peer.alive:
                self.peers.remove(peer)
                self.events.append(("leave", peer))
        return self.inbox

    def _show_the_door(self):
        """`Host`'s rule, and its clock: a refusal is not a room."""
        now = time.monotonic()
        for entry in list(self._rejecting):
            peer, since = entry
            for _ in peer.read():
                pass
            peer.flush()
            if not peer.alive or not peer.outbox or now - since > REJECT_GRACE:
                peer.close()
                self._rejecting.remove(entry)

    def send(self, peer, kind, payload=None):
        peer.send_message(encode_body(kind, payload))

    def broadcast(self, kind, payload=None, skip=None):
        body = encode_body(kind, payload)
        for peer in self.peers:
            if peer is not skip:
                peer.send_message(body)

    def drop(self, peer, reason=""):
        if reason:
            try:
                peer.send_message(encode_body("bye", {"reason": reason[:80]}))
                peer.flush()
            except (OSError, NetError):
                pass
        peer.close()

    def close(self):
        for peer in list(self.peers) + list(self._parked):
            peer.close()
        for peer, _since in list(self._rejecting):
            peer.close()
        self.peers, self._parked, self._rejecting = [], [], []


# -- client -------------------------------------------------------------------

class Client:
    """Connects to a host. Sends intent, receives truth."""

    def __init__(self, host, port=DEFAULT_PORT, timeout=6.0):
        try:
            sock = socket.create_connection((host, int(port)), timeout)
        except OSError as exc:
            raise NetError("could not reach the host: %s" % exc)
        self.peer = _Peer(sock, (host, port))
        self.inbox = []
        self.player_id = 0

    @classmethod
    def from_code(cls, code, timeout=6.0):
        host, port = decode_room(code)
        return cls(host, port, timeout)

    @property
    def connected(self):
        return self.peer.alive

    def pump(self):
        self.peer.flush()
        self.inbox = [m for m in self.peer.read()]
        return self.inbox

    def send(self, kind, payload=None):
        self.peer.send(encode_frame(kind, payload))

    def close(self):
        self.peer.close()


class WebClient:
    """A guest that joins over WebSocket instead of raw TCP.

    Same four things as `Client` - `connected`, `pump`, `send`, `close` - so
    `coop.py` picks one at the door and never thinks about it again.

    **Why this exists on the desktop at all**, when the browser build will
    ultimately hand its framing to the browser's own `WebSocket` object:
    because it is the only way to exercise the crossplay path without a
    browser. Every byte a phone would put on the wire, this puts on the wire,
    against the real `Host` over a real socket. The alternative is shipping a
    transport whose only test is a mock of the thing being tested.

    It is also a real fallback: a desktop player behind something that eats
    unrecognised TCP but passes HTTP upgrades can join with it.
    """

    def __init__(self, host, port=DEFAULT_PORT, timeout=6.0, path="/"):
        try:
            sock = socket.create_connection((host, int(port)), timeout)
        except OSError as exc:
            raise NetError("could not reach the host: %s" % exc)
        self.peer = _Peer(sock, (host, port))
        self.peer.mode = MODE_WS_CLIENT
        self.inbox = []
        self.player_id = 0
        request, key = websock.client_handshake(host, port, path)
        self.peer.ws_key = key
        self.peer.send(request)

    @classmethod
    def from_code(cls, code, timeout=6.0):
        host, port = decode_room(code)
        return cls(host, port, timeout)

    @property
    def connected(self):
        return self.peer.alive

    @property
    def ready(self):
        """Whether the upgrade has completed. Sends are held until it has."""
        return self.peer._ws_upgraded

    def pump(self):
        self.peer.flush()
        self.inbox = list(self.peer.read())
        return self.inbox

    def send(self, kind, payload=None):
        self.peer.send_message(encode_body(kind, payload))

    def close(self):
        if self.peer._ws_upgraded and self.peer.alive:
            try:
                self.peer.send(websock.masked_close_frame())
                self.peer.flush()
            except OSError:
                pass
        self.peer.close()


class RelayClient(WebClient):
    """A guest that reaches the host through the relay.

    **Only the door is different.** Everything after it - `connected`,
    `ready`, `pump`, `send`, `close` - is `WebClient`'s, because past the
    handshake this *is* a WebSocket guest; the socket simply lands on a
    relay that is holding one of the host's parked connections rather
    than on the host itself.
    """

    def __init__(self, room, timeout=relay.TIMEOUT):
        sock, key = relay.connect(room, "join", timeout)
        self.peer = _Peer(sock, (str(room), 0))
        self.peer.mode = MODE_WS_CLIENT
        self.peer.ws_key = key
        self.inbox = []
        self.player_id = 0

    @classmethod
    def from_code(cls, code, timeout=relay.TIMEOUT):
        if not relay.enabled():
            raise NetError("that looks like a relay code and there is no "
                           "relay set up")
        if not relay.valid_room(code):
            raise NetError("that is not a room code")
        return cls(relay.clean(code), timeout)


class BrowserClient(_RateWindow):
    """The guest a browser can actually be: no socket, and no framing of ours.

    **Still live.** Mobile was dropped on 2026-08-23 but the browser build
    was kept for PC (section 37), and a browser can still only ever be a
    guest - so this is how a web player joins somebody's room. That path
    has never been run end to end against a real host.

    `Client` and `WebClient` both open a socket, and a page cannot. Under
    emscripten `socket` exists, but it is backed by a WebSocket the browser
    opens asynchronously - `connect` returns `BlockingIOError` immediately and
    settles only once control goes back to the JS event loop, so a blocking
    wait on the one thread a page has is a deadlock rather than a slow path.
    `WebClient` would also frame the connection twice, because the browser's
    own `WebSocket` has already done the RFC 6455 handshake and is already
    wrapping everything we hand it.

    So this owns a JS `WebSocket` and speaks only message *bodies*.
    `websock.py` is not involved on this side at all.

    Same four things as the other two - `connected`, `pump`, `send`, `close` -
    plus `ready`, which `WebClient` has for the same reason: the connection is
    not open the instant the object exists, and anything said before it opens
    is held rather than lost.

    **The host needs no changes.** A browser `WebSocket` sends a real HTTP
    upgrade, `Host._sniff` reads `GET ` in the first four bytes and settles on
    `MODE_WS` (section 24), and from there the two ends already agree.

    **What is different from every other transport here, and matters:**
    constructing one cannot fail. A socket refuses immediately and raises
    `NetError`; a `WebSocket` reports failure later, through `onerror`, on
    the event loop. So a room code that goes nowhere shows up as `connected`
    turning False a moment after joining, not as an exception at the door.

    `window` is a parameter because nothing on a desktop can supply the real
    one. The interface it has to offer is small and exact, and `FakeWindow` in
    the tests implements precisely that - written out rather than mocked for
    the same reason `FakeLocalStorage` is: the interesting behaviour is what
    happens when the answer is no.
    """

    def __init__(self, host, port=DEFAULT_PORT, timeout=6.0, path="/",
                 window=None):
        _RateWindow.__init__(self)
        self.address = (host, int(port))
        self.inbox = []
        self.player_id = 0
        self.alive = True
        self._open = False
        self._ever_open = False      # see `reached`
        self._pending = []           # bodies said before the socket opened
        self._incoming = []          # raw strings, drained by `pump`
        window = _browser_window() if window is None else window
        url = "ws://%s:%d%s" % (host, int(port), path)
        try:
            self.socket = window.WebSocket.new(url)
        except Exception as exc:                       # noqa: BLE001
            # A page with no WebSocket is not a page this game runs on, but
            # it must still be a message rather than a traceback.
            raise NetError("this browser would not open a connection: %s"
                           % exc)
        self.socket.onopen = _bind(self._on_open)
        self.socket.onmessage = _bind(self._on_message)
        self.socket.onclose = _bind(self._on_close)
        self.socket.onerror = _bind(self._on_close)

    @classmethod
    def from_code(cls, code, timeout=6.0, window=None):
        host, port = decode_room(code)
        return cls(host, port, timeout, window=window)

    # -- what the browser calls back into ----------------------------------
    #
    # All four are deliberately tiny and never parse anything. A callback
    # runs on the JS event loop, in the middle of whatever frame the game was
    # drawing; doing work here is how a rendering bug becomes a networking
    # bug. They record, `pump` decides.

    def _on_open(self, _event=None):
        self._open = True
        self._ever_open = True
        held, self._pending = self._pending, []
        for body in held:
            self._transmit(body)

    def _on_message(self, event):
        data = getattr(event, "data", None)
        if data is None:
            return
        self._incoming.append(data)

    def _on_close(self, _event=None):
        self._open = False
        self.alive = False

    # -- the interface `coop.py` uses --------------------------------------

    @property
    def connected(self):
        return self.alive

    @property
    def ready(self):
        """Whether the socket is open. Sends are held until it is."""
        return self.alive and self._open

    @property
    def reached(self):
        """Whether the host was ever actually there.

        A latch, not a state, and that is the whole point of it. The two
        socket transports connect inside their own constructor, so for
        them the answer is yes by the time the object exists - they do not
        define this and `getattr(..., True)` is right for them. Here it is
        a real question, and the caller cannot answer it by watching
        `ready`: a socket that opens and dies between two frames is never
        seen open at all, which is exactly the case a test caught.
        """
        return self._ever_open

    def pump(self):
        """Drain what the browser has handed us. Never blocks; nothing to flush.

        The other two transports flush an outbox here because a kernel buffer
        can fill. A `WebSocket` owns its own queue, so there is nothing on
        this side to push.
        """
        raw, self._incoming = self._incoming, []
        self.inbox = []
        for item in raw:
            body = self._as_bytes(item)
            if body is None:
                continue
            # Checked in the same order as `_Peer.read`: size first, so a
            # host claiming a gigabyte costs nothing, then rate, then sense.
            if len(body) > MAX_FRAME:
                self.close()
                break
            if self.over_rate():
                self.close()
                break
            message = _decode_body(body)
            if message is not None:
                self.inbox.append(message)
        return self.inbox

    def send(self, kind, payload=None):
        body = encode_body(kind, payload)
        if not self.alive:
            return
        if not self._open:
            self._pending.append(body)
            return
        self._transmit(body)

    def close(self):
        self._open = False
        self.alive = False
        try:
            self.socket.close()
        except Exception:                              # noqa: BLE001
            pass

    # -- plumbing ----------------------------------------------------------

    def _transmit(self, body):
        try:
            self.socket.send(body.decode("utf-8"))
        except Exception:                              # noqa: BLE001
            # A send that throws means the socket is gone and the close event
            # is either in flight or never coming. Either way it is over.
            self.alive = False
            self._open = False

    @staticmethod
    def _as_bytes(item):
        """A WebSocket message as bytes, or None if it is not one.

        A text frame arrives as a string, and that is all this protocol ever
        sends. Anything else is either a binary frame nobody wrote or a JS
        object the bridge did not convert, and both are nonsense to drop
        rather than guess at.
        """
        if isinstance(item, bytes):
            return item
        if isinstance(item, str):
            try:
                return item.encode("utf-8")
            except UnicodeEncodeError:
                return None
        return None


def _browser_window():
    """The page's JS globals, or a `NetError` explaining there are none.

    Imported here rather than at the top of the module because `platform` is
    something pygbag injects, and on a desktop importing it gets you the
    stdlib module of the same name - which has no `window` and would make
    this fail with an `AttributeError` instead of a sentence.
    """
    try:
        import platform as browser_platform
        return browser_platform.window
    except (ImportError, AttributeError):
        raise NetError("no browser here: this transport only runs in a page")


def _bind(handler):
    """Hand a Python function to JS in whatever way this bridge wants.

    pygbag's FFI usually accepts a plain callable. Pyodide-derived bridges
    want it wrapped in `create_proxy` first, or it is garbage-collected while
    JS still holds the reference and the callback silently stops arriving.
    Trying the wrapper first costs nothing and covers both.

    This is the one line in this class that cannot be checked from a desktop,
    which is exactly why it is one line and lives on its own.
    """
    try:
        import platform as browser_platform
        return browser_platform.ffi.create_proxy(handler)
    except (ImportError, AttributeError):
        return handler


def guest_transport(websocket=False):
    """Which guest class this platform can be. The only place that chooses.

    Three transports now, and the same reasoning as `config.open_save_store`:
    one decision point, made once, so nothing above it grows a second opinion.
    A browser gets `BrowserClient` no matter what `websocket` says, because
    the other two cannot run there at all.
    """
    from . import config as config_mod
    if config_mod.is_browser():
        return BrowserClient
    return WebClient if websocket else Client
