"""Hosting from behind a NAT nobody can dial into.

Roadmap: *"a relay for CGNAT hosts"*, open since §32.

**A room code is an address** (`net.encode_room`), and that is the whole
reason co-op needs no server: the code *is* where to connect. It also
stops working completely the moment the host has no address anybody can
reach — carrier-grade NAT hands a whole neighbourhood one public IP with
no way to forward a port, which is what phone tethering and a lot of home
broadband now are.

**So the host dials out instead of listening.** It opens one connection
per guest slot to a relay and leaves them parked; a guest dials the same
relay with the same code; the relay ties one of each together and moves
bytes. Nothing else changes: the host is still the authority (§21), the
guests still talk to the host, and every frame is the one the direct path
would have carried.

**Off unless `PIXEL_RPG_RELAY_URL` is set** — the same shape as
`leaderboard.py`, and for the same two reasons. The relay has to *run*
somewhere with a public address, which is an account rather than code;
and nothing about the game should quietly start routing itself through a
third party because a file existed.

Savvydev, 2026-09-06, which is the rule this follows:

> i dont want ALL global servers running off of my pc, but you can still
> use FREE 3rd party tools for the global stuff

`tools/relay_server.py` is one he can run anywhere free that speaks
Python. **Whoever runs it can see everything both ends send** - it is a
pipe in the middle, not an encrypted tunnel - which is a fact worth
knowing rather than a reason not to: what crosses it is positions,
monsters and a serialised hero.
"""
from __future__ import annotations

import os
import socket
import ssl
import time

from . import websock
from .util import rng

#: Room ids use the game's spoken-aloud alphabet minus `#`, which is a
#: fragment delimiter in a URL and would never arrive.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
ROOM_LEN = 6

#: How long to wait for the relay to answer at all.
TIMEOUT = 6.0
#: How long a guest waits for the relay to hand it a host.
JOIN_WAIT = 8.0

_ENV = "PIXEL_RPG_RELAY_URL"


def url():
    """Where the relay is, or "" when there is not one.

    Read every time rather than cached at import: a setting nobody can
    change without restarting is a setting that gets reported as broken.
    """
    return str(os.environ.get(_ENV, "")).strip()


def enabled():
    """Whether hosting through a relay is available at all."""
    return bool(split(url())[0])


def split(text):
    """`(host, port, secure)` for a relay URL, or `("", 0, False)`.

    Deliberately small: `ws://host[:port]` and `wss://host[:port]`, and a
    bare `host[:port]` read as plain. **Anything with a path is refused**
    - the paths are this module's (`/park/<room>`, `/join/<room>`) and a
    URL that already carries one is a URL that means something else.
    """
    text = str(text or "").strip()
    if not text:
        return "", 0, False
    secure = False
    for prefix, is_tls in (("wss://", True), ("ws://", False),
                           ("https://", True), ("http://", False)):
        if text.lower().startswith(prefix):
            secure = is_tls
            text = text[len(prefix):]
            break
    if "/" in text:
        head, _, rest = text.partition("/")
        if rest.strip("/"):
            return "", 0, False
        text = head
    host, _, port_text = text.partition(":")
    if not host:
        return "", 0, False
    try:
        port = int(port_text) if port_text else (443 if secure else 80)
    except ValueError:
        return "", 0, False
    if not 1 <= port <= 65535:
        return "", 0, False
    return host, port, secure


def new_room():
    """A fresh room id. Six characters, said out loud once."""
    return "".join(rng.choice(ALPHABET) for _ in range(ROOM_LEN))


def valid_room(code):
    """Whether `code` is a relay room id, cleaned of spoken punctuation."""
    cleaned = clean(code)
    return len(cleaned) == ROOM_LEN and all(ch in ALPHABET for ch in cleaned)


def clean(code):
    """A code as somebody typed it, made into a code."""
    return "".join(ch for ch in str(code or "").upper()
                   if ch not in " -_\t").strip()


def connect(room, role, timeout=TIMEOUT):
    """One socket, upgraded, ready to carry frames. Raises `OSError`.

    `role` is "park" (a host waiting) or "join" (somebody arriving). The
    caller gets a plain socket and the key its handshake was made with;
    the framing is `net._Peer`'s job, exactly as it is for the direct
    path — this module opens doors and does not carry anything through
    them.
    """
    host, port, secure = split(url())
    if not host:
        raise OSError("no relay is configured")
    if role not in ("park", "join") or not valid_room(room):
        raise OSError("not a relay room")
    sock = socket.create_connection((host, port), timeout)
    if secure:
        context = ssl.create_default_context()
        sock = context.wrap_socket(sock, server_hostname=host)
    request, key = websock.client_handshake(
        host, port, "/%s/%s" % (role, clean(room)))
    sock.sendall(request)
    return sock, key


def wait_for_head(sock, deadline=TIMEOUT):
    """Read until the end of the relay's handshake reply.

    Returns `(head, rest)`. **The leftover matters**: the relay can pair a
    socket the instant it upgrades, so the first game frame can arrive in
    the same read as the 101 - and a version of this that threw the extra
    bytes away lost the first message of every session.
    """
    sock.settimeout(0.2)
    buffer = b""
    end = time.monotonic() + deadline
    while websock.HEADER_END not in buffer:
        if time.monotonic() > end:
            raise OSError("the relay never answered")
        try:
            chunk = sock.recv(4096)
        except (socket.timeout, ssl.SSLWantReadError):
            continue
        if not chunk:
            raise OSError("the relay hung up")
        buffer += chunk
        if len(buffer) > websock.MAX_HANDSHAKE:
            raise OSError("the relay said too much")
    head, _, rest = buffer.partition(websock.HEADER_END)
    return head, rest
