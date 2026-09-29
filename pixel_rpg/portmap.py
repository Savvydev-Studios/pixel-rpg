"""Making a room reachable from outside the house, with nobody's server.

Two free-standing tricks, both pure stdlib, neither costing a penny:

* **STUN** asks a public server "what does my address look like from out
  there?" and gets back the public IP the world sees. Google run several for
  free and every WebRTC call on earth uses them.
* **UPnP/IGD** asks the *router* to forward a port back to this machine. Most
  home routers say yes; some have it switched off.

Do both and the room code can carry a public address, which is the entire
difference between "co-op with someone in your house" and "co-op". There is
no relay and no rendezvous - the code still *is* the address.

**This runs on a worker thread**, because it talks to a router and a server
on the internet and either can be slow or absent. `net.py` rule 1 says the
loop never blocks, and that rule does not get an exception for the one
operation with a stranger at the far end. The lobby polls `Portal.done`.

**The honest limit:** a host behind CGNAT (mobile data, some fibre, student
halls) has no public address to map. STUN will return the carrier's address
and forwarding it is not possible, so the check below compares what STUN saw
against what UPnP mapped and reports plainly rather than handing out a code
that cannot work.
"""
from __future__ import annotations

import re
import socket
import struct
import threading
import urllib.request
import xml.etree.ElementTree as ET

# Public STUN servers, tried in order. All free, all run by people who expect
# this traffic; none of them learn anything about the game.
STUN_SERVERS = (("stun.l.google.com", 19302),
                ("stun1.l.google.com", 19302),
                ("stun.cloudflare.com", 3478))
MAGIC_COOKIE = 0x2112A442
SSDP_ADDR = ("239.255.255.250", 1900)
IGD_TYPES = ("urn:schemas-upnp-org:device:InternetGatewayDevice:1",
             "urn:schemas-upnp-org:service:WANIPConnection:1",
             "urn:schemas-upnp-org:service:WANPPPConnection:1")
WAN_SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:1",
                "urn:schemas-upnp-org:service:WANPPPConnection:1")
LEASE_SECONDS = 3600 * 6


def _private(address):
    """True for anything that only means something inside one network."""
    try:
        parts = [int(p) for p in address.split(".")]
    except (ValueError, AttributeError):
        return True
    if len(parts) != 4:
        return True
    a, b = parts[0], parts[1]
    return (a == 10 or a == 127 or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b == 168) or (a == 169 and b == 254)
            # 100.64/10 is the carrier-grade NAT range. Seeing our own
            # address in here is the clearest possible sign of CGNAT.
            or (a == 100 and 64 <= b <= 127))


def is_cgnat(address):
    try:
        parts = [int(p) for p in str(address).split(".")]
    except ValueError:
        return False
    return len(parts) == 4 and parts[0] == 100 and 64 <= parts[1] <= 127


# -- STUN ---------------------------------------------------------------------

def public_address(timeout=1.5):
    """The address the internet sees, or "" if nothing answers."""
    for host, port in STUN_SERVERS:
        found = _stun_once(host, port, timeout)
        if found:
            return found
    return ""


def _stun_once(host, port, timeout):
    # RFC 5389 wants exactly 12 bytes of transaction id after the
    # 4-byte cookie. One byte short and the request is malformed, and a
    # STUN server answers a malformed request with silence.
    transaction = struct.pack("!I", MAGIC_COOKIE) + b"pixelrpg0001"
    request = struct.pack("!HH", 0x0001, 0) + transaction
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(request, (host, port))
        data, _addr = sock.recvfrom(1024)
    except (OSError, socket.timeout):
        return ""
    finally:
        sock.close()

    if len(data) < 20 or data[4:20] != transaction:
        return ""
    length = struct.unpack("!H", data[2:4])[0]
    body, offset = data[20:20 + length], 0
    while offset + 4 <= len(body):
        attr, size = struct.unpack("!HH", body[offset:offset + 4])
        value = body[offset + 4:offset + 4 + size]
        # 0x0020 XOR-MAPPED-ADDRESS, 0x0001 MAPPED-ADDRESS (older servers).
        if attr in (0x0020, 0x0001) and size >= 8 and value[1] == 0x01:
            raw_port = struct.unpack("!H", value[2:4])[0]
            raw_ip = value[4:8]
            if attr == 0x0020:
                raw_port ^= MAGIC_COOKIE >> 16
                raw_ip = bytes(b ^ c for b, c in
                               zip(raw_ip, struct.pack("!I", MAGIC_COOKIE)))
            return socket.inet_ntoa(raw_ip)
        offset += 4 + size + ((4 - size % 4) % 4)
    return ""


# -- UPnP ---------------------------------------------------------------------

def _discover(timeout=1.5):
    """Find a router that admits to being an internet gateway."""
    message = ("M-SEARCH * HTTP/1.1\r\n"
               "HOST: %s:%d\r\n"
               'MAN: "ssdp:discover"\r\n'
               "MX: 1\r\n"
               "ST: %s\r\n\r\n")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    try:
        for service in IGD_TYPES:
            try:
                sock.sendto((message % (SSDP_ADDR[0], SSDP_ADDR[1], service))
                            .encode("ascii"), SSDP_ADDR)
            except OSError:
                continue
        deadline_reads = 6
        while deadline_reads > 0:
            deadline_reads -= 1
            try:
                data, _addr = sock.recvfrom(2048)
            except (OSError, socket.timeout):
                break
            match = re.search(rb"(?i)^location:\s*(\S+)", data, re.M)
            if match:
                return match.group(1).decode("ascii", "replace")
    finally:
        sock.close()
    return ""


def _control_url(description_url, timeout=2.0):
    """Read the router's description and find the port-mapping endpoint."""
    try:
        with urllib.request.urlopen(description_url, timeout=timeout) as res:
            xml = res.read(256 * 1024)
    except Exception:                                     # noqa: BLE001
        return "", ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return "", ""

    for service in root.iter():
        if not service.tag.endswith("service"):
            continue
        kind = control = ""
        for child in service:
            if child.tag.endswith("serviceType"):
                kind = (child.text or "").strip()
            elif child.tag.endswith("controlURL"):
                control = (child.text or "").strip()
        if kind in WAN_SERVICES and control:
            base = re.match(r"(https?://[^/]+)", description_url)
            if not base:
                return "", ""
            if not control.startswith("/"):
                control = "/" + control
            return base.group(1) + control, kind
    return "", ""


def _soap(control, service, action, body, timeout=3.0):
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body><u:%s xmlns:u=\"%s\">%s</u:%s></s:Body></s:Envelope>"
        % (action, service, body, action)).encode("utf-8")
    request = urllib.request.Request(
        control, data=envelope,
        headers={"Content-Type": 'text/xml; charset="utf-8"',
                 "SOAPAction": '"%s#%s"' % (service, action)})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as res:
            return res.read(64 * 1024)
    except Exception:                                     # noqa: BLE001
        return b""


def map_port(port, local_ip, timeout=2.0):
    """Ask the router to forward `port` here. True if it agreed."""
    description = _discover(timeout)
    if not description:
        return False
    control, service = _control_url(description, timeout)
    if not control:
        return False
    body = ("<NewRemoteHost></NewRemoteHost>"
            "<NewExternalPort>%d</NewExternalPort>"
            "<NewProtocol>TCP</NewProtocol>"
            "<NewInternalPort>%d</NewInternalPort>"
            "<NewInternalClient>%s</NewInternalClient>"
            "<NewEnabled>1</NewEnabled>"
            "<NewPortMappingDescription>Pixel RPG co-op"
            "</NewPortMappingDescription>"
            "<NewLeaseDuration>%d</NewLeaseDuration>"
            % (int(port), int(port), local_ip, LEASE_SECONDS))
    return bool(_soap(control, service, "AddPortMapping", body))


def unmap_port(port, timeout=2.0):
    """Tidy up. Best effort; the lease expires on its own regardless."""
    description = _discover(timeout)
    if not description:
        return False
    control, service = _control_url(description, timeout)
    if not control:
        return False
    body = ("<NewRemoteHost></NewRemoteHost>"
            "<NewExternalPort>%d</NewExternalPort>"
            "<NewProtocol>TCP</NewProtocol>" % int(port))
    return bool(_soap(control, service, "DeletePortMapping", body))


# -- the thing the lobby actually uses -----------------------------------------

class Portal:
    """Tries to make one port reachable, on a thread, without ever blocking.

    Poll `done`. When it is set, `address` is the best address to advertise
    and `note` says in one line what happened, in words a player can act on.
    """

    def __init__(self, port, local_ip, enabled=True):
        self.port = int(port)
        self.local_ip = local_ip
        self.address = local_ip
        self.mapped = False
        self.public = ""
        self.note = "Local network only"
        self.done = not enabled
        self._thread = None
        if enabled:
            self._thread = threading.Thread(target=self._work, daemon=True)
            self._thread.start()

    @property
    def global_ready(self):
        return self.mapped and bool(self.public)

    def _work(self):
        try:
            self.public = public_address()
            if self.public:
                self.mapped = map_port(self.port, self.local_ip)
            self._describe()
        except Exception:                                 # noqa: BLE001
            # A failure here costs global play, never the game.
            self.note = "Local network only"
        finally:
            self.done = True

    def _describe(self):
        if not self.public:
            self.note = "No answer from the internet - local network only"
            return
        if is_cgnat(self.public):
            # Worth naming precisely: this one is not fixable by the player.
            self.note = ("Your provider shares this connection (CGNAT), "
                         "so it cannot be reached from outside")
            return
        if self.mapped:
            self.address = self.public
            self.note = "Anyone can join with this code"
            return
        self.note = ("Router would not open a port - local network only, "
                     "or forward port %d by hand" % self.port)

    def release(self):
        if self.mapped:
            try:
                unmap_port(self.port)
            except Exception:                             # noqa: BLE001
                pass
            self.mapped = False
