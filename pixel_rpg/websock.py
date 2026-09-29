"""RFC 6455 WebSocket, as pure functions over bytes.

**Why this exists.** Section 18 of the handoff establishes two facts that
between them decide the shape of PC-to-mobile crossplay: a browser cannot
listen for incoming connections, so a phone can never host; and a browser
cannot speak raw TCP either, so a phone could not otherwise even *join*. A
browser build is therefore a **guest, permanently**, and this is the language
it speaks.

**Both halves live here, and they are not symmetric.** A server never masks
and must reject an unmasked client; a client always masks and must reject a
masked server. That asymmetry is the protocol, not an accident, so the two
directions are separate functions rather than one with a flag.

Nothing here touches a socket, for the same reason `battle.py` touches no
pygame: a frame is a sequence of bytes and should be testable without a
network. `net._Peer` owns the socket and calls in here to decide what the
bytes mean.

**The rules from `net.py` still apply, and two of them bite harder here:**

* *Bounded before trusted.* A WebSocket length header can claim 2^64 bytes in
  eight bytes of wire. It is checked against the caller's ceiling before a
  single byte of payload is buffered, exactly as the 4-byte TCP prefix is.
* *Client frames must be masked.* RFC 6455 section 5.1 requires it, and the
  requirement is not decoration - it exists so that a hostile page cannot
  make a browser emit bytes that a confused proxy would read as its own
  protocol. An unmasked client frame is refused rather than tolerated.
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct

# RFC 6455 section 1.3. Fixed by the standard; not a secret and not a salt.
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Opcodes worth naming.
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

# A handshake is a few hundred bytes. Anything much larger is somebody
# feeding us headers to see how much we will hold, so the ceiling is checked
# while reading rather than after.
MAX_HANDSHAKE = 8 * 1024
HEADER_END = b"\r\n\r\n"


class ProtocolError(Exception):
    """The peer is not speaking WebSocket, or is speaking it wrongly."""


# -- the handshake ------------------------------------------------------------

def looks_like_http(buffer):
    """Whether these opening bytes are an HTTP request rather than a frame.

    This is how one port serves both transports: the raw protocol's first
    four bytes are a big-endian length, and a length that spells `GET ` would
    be 1,195,725,856 bytes - about forty times over any sane ceiling and
    rejected on sight. So the two can never be confused, and a host does not
    need a second port or a flag to know which one it is talking to.
    """
    return buffer[:4] == b"GET "


def accept_key(key):
    """The `Sec-WebSocket-Accept` value for a given `Sec-WebSocket-Key`.

    Not authentication and not integrity - it proves only that the far end
    read the request and knows the protocol, which is precisely what it is
    for: it stops a cached HTTP response from being mistaken for a live
    upgrade.
    """
    if isinstance(key, str):
        key = key.encode("ascii", "ignore")
    digest = hashlib.sha1(key.strip() + GUID).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_request(head):
    """Headers from a request head, lowercased. None if it is not one.

    Only the request *head* - everything before the blank line. A WebSocket
    upgrade carries no body, so anything after it is either pipelining or
    mischief and is not this function's problem.
    """
    try:
        text = head.decode("latin-1")
    except (UnicodeDecodeError, AttributeError):
        return None
    lines = text.split("\r\n")
    if not lines or not lines[0].upper().startswith("GET "):
        return None
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        headers[name.strip().lower()] = value.strip()
    return headers


def handshake_response(headers):
    """The 101 to send back, or None if this is not a WebSocket upgrade.

    Deliberately minimal. No subprotocol negotiation, no extensions, no
    compression: every one of those is a feature the game does not use and a
    parser somebody would have to get right.
    """
    if not headers:
        return None
    if "websocket" not in headers.get("upgrade", "").lower():
        return None
    if "upgrade" not in headers.get("connection", "").lower():
        return None
    key = headers.get("sec-websocket-key")
    if not key:
        return None
    if headers.get("sec-websocket-version", "13").strip() != "13":
        return None
    return ("HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n"
            "\r\n" % accept_key(key)).encode("ascii")


def client_handshake(host, port, path="/"):
    """The opening request a guest sends, and the key to check the reply with.

    Returns `(request_bytes, key)`. The key is 16 random bytes in base64, as
    RFC 6455 section 4.1 requires - random rather than fixed because it is
    what makes a *cached* HTTP response impossible to mistake for a live
    upgrade. It is not a secret and proves nothing about who is at either end.
    """
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = ("GET %s HTTP/1.1\r\n"
               "Host: %s:%d\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n"
               "\r\n" % (path, host, int(port), key))
    return request.encode("ascii"), key


def check_handshake(head, key):
    """Whether a response head is the 101 we asked for.

    Both halves are checked. A 101 with the wrong accept value is a server
    that did not read our request - most likely a cache or a proxy answering
    on its behalf - and continuing would mean framing bytes at something that
    is not listening for them.
    """
    try:
        text = head.decode("latin-1")
    except (UnicodeDecodeError, AttributeError):
        return False
    lines = text.split("\r\n")
    if not lines or " 101" not in lines[0]:
        return False
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "sec-websocket-accept":
            return value.strip() == accept_key(key)
    return False


def reject_response(reason="Bad Request"):
    """A plain HTTP refusal, so a browser gets a status rather than silence."""
    body = reason.encode("ascii", "ignore")[:200]
    return (b"HTTP/1.1 400 Bad Request\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + body)


# -- frames -------------------------------------------------------------------

def encode(payload, opcode=OP_TEXT):
    """One unfragmented server frame. Never masked - RFC 6455 section 5.1."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    header = bytes((0x80 | opcode,))
    size = len(payload)
    if size < 126:
        header += bytes((size,))
    elif size < 65536:
        header += bytes((126,)) + struct.pack("!H", size)
    else:
        header += bytes((127,)) + struct.pack("!Q", size)
    return header + payload


def encode_masked(payload, opcode=OP_TEXT):
    """One client frame. Always masked - RFC 6455 section 5.1.

    The mask is four random bytes per frame and is *not* encryption: the key
    travels in front of the payload. It exists so that a hostile page cannot
    choose the exact bytes a browser puts on the wire, which is what stopped
    an old class of proxy-poisoning attack. Random per frame is the part that
    matters; reusing one would hand that choice straight back.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    size = len(payload)
    header = bytes((0x80 | opcode,))
    if size < 126:
        header += bytes((0x80 | size,))
    elif size < 65536:
        header += bytes((0x80 | 126,)) + struct.pack("!H", size)
    else:
        header += bytes((0x80 | 127,)) + struct.pack("!Q", size)
    key = os.urandom(4)
    body = bytearray(payload)
    for i in range(size):
        body[i] ^= key[i & 3]
    return header + key + bytes(body)


def close_frame(code=1000):
    return encode(struct.pack("!H", int(code)), OP_CLOSE)


def masked_close_frame(code=1000):
    return encode_masked(struct.pack("!H", int(code)), OP_CLOSE)


def take_frame(buffer, max_payload, expect_mask=True):
    """Pull one frame off the front of `buffer`.

    Returns `(fin, opcode, payload, rest)`, or `(None, None, None, buffer)`
    when the frame has not all arrived yet. Raises `ProtocolError` for
    anything that is not worth continuing over.

    The size is checked against `max_payload` **from the length header**,
    before the payload is waited for - so a peer claiming four gigabytes is
    refused for free rather than after we have held four gigabytes.

    `expect_mask` says which direction this is. A server reading a client
    requires masking; a client reading a server requires its *absence*. The
    rule is not symmetric and neither is the check - a masked frame arriving
    at a client means something in the middle is rewriting the stream.
    """
    if len(buffer) < 2:
        return None, None, None, buffer
    first, second = buffer[0], buffer[1]
    fin = bool(first & 0x80)
    if first & 0x70:
        # RSV bits are only meaningful with a negotiated extension, and none
        # is ever negotiated here, so a set bit means we disagree about what
        # the connection is.
        raise ProtocolError("reserved bits set")
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    size = second & 0x7F
    offset = 2

    if size == 126:
        if len(buffer) < offset + 2:
            return None, None, None, buffer
        (size,) = struct.unpack("!H", buffer[offset:offset + 2])
        offset += 2
    elif size == 127:
        if len(buffer) < offset + 8:
            return None, None, None, buffer
        (size,) = struct.unpack("!Q", buffer[offset:offset + 8])
        offset += 8

    if size > max_payload:
        raise ProtocolError("frame of %d bytes is over the ceiling" % size)
    if expect_mask and not masked:
        # Not pedantry: masking is what stops a hostile page persuading a
        # browser to emit bytes a confused proxy would read as its own
        # protocol. A client that skips it is not one we want.
        raise ProtocolError("client frame was not masked")
    if masked and not expect_mask:
        raise ProtocolError("server frame was masked")

    if masked:
        if len(buffer) < offset + 4 + size:
            return None, None, None, buffer
        mask = buffer[offset:offset + 4]
        offset += 4
        body = bytearray(buffer[offset:offset + size])
        for i in range(size):
            body[i] ^= mask[i & 3]
        return fin, opcode, bytes(body), buffer[offset + size:]

    if len(buffer) < offset + size:
        return None, None, None, buffer
    return fin, opcode, bytes(buffer[offset:offset + size]), \
        buffer[offset + size:]
