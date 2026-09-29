#!/usr/bin/env python3
"""Serve `build/web` with the headers a WASM build actually needs.

**This is a PC build.** Mobile was dropped on 2026-08-23 and the browser
build was kept and pointed at desktops instead - handoff section 37. It
is a live target: keyboard, mouse, and a window somebody drags about.

**Deliberately without `Cross-Origin-Embedder-Policy`.** pygbag's own test
server sends `require-corp`, this started as a copy of it, and it did not
work: the runtime lives on `pygame-web.github.io`, which does not send a
`Cross-Origin-Resource-Policy` header back, so COEP blocked every script the
page needed. The console said
`ERR_BLOCKED_BY_RESPONSE.NotSameOriginAfterDefaultedToSameOriginByCoep`
and the loading bar simply never moved.

Dropping COEP costs `SharedArrayBuffer`, which costs threads - and this
build has no threads to lose. That is the entire premise of `run_async`
(section 27): a browser gives the game one thread and it yields between
frames. So the header buys nothing here and breaks the load.

`access-control-allow-origin` and `cross-origin-resource-policy` stay,
because the apk is fetched by the runtime rather than the page.

**It is not a static file server, and that is not optional.** The page asks
its own origin for the interpreter's pieces, so anything missing is fetched
once from the CDN and written into `build/web`. Serve this folder with
`python -m http.server` before that has happened and you get a grey canvas
and a silent browser console.

    python tools/serve_web.py [port]

`tools/build_web.py --serve` runs this for you.
"""
import functools
import http.server
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "build" / "web"
DEFAULT_PORT = 8000

# Where the runtime's own pieces come from. **One fixed host, hardcoded.**
# This is a fetch-and-cache for a known set of files, not an open proxy,
# and it must never become one: the path is taken from the request, so
# anything that escaped `build/web` would be serving the whole disk.
CDN_HOST = "https://pygame-web.github.io"
# The build's own artefacts. Never go looking upstream for these - if one
# is missing the build is broken and a 404 is the honest answer.
OURS = (".apk", ".tar.gz")


class Handler(http.server.SimpleHTTPRequestHandler):
    """A static handler that also says "this page is cross-origin isolated"."""

    def end_headers(self):
        self.send_header("access-control-allow-origin", "*")
        self.send_header("cross-origin-resource-policy", "cross-origin")
        # Nothing here is worth a cache: this server exists to look at the
        # build that was just made, and a stale apk is an hour of confusion.
        self.send_header("cache-control", "no-store")
        super().end_headers()

    def do_GET(self):
        """Answer a trace ping, or serve a file."""
        if self.path.startswith("/__trace"):
            self._trace_ping()
            return
        http.server.SimpleHTTPRequestHandler.do_GET(self)

    def _trace_ping(self):
        """Write one line the game sent from somebody else's browser.

        The other half of `main.trace`. A phone with a grey screen cannot
        tell you anything; this is how it tells you something.
        """
        query = urllib.parse.urlsplit(self.path).query
        message = urllib.parse.parse_qs(query).get("m", [""])[0]
        # Straight off the network and into a log a human reads, so it is
        # capped and stripped of anything that could forge a log line.
        message = message.replace("\r", " ").replace("\n", " ")[:300]
        sys.stderr.write("  TRACE [%s] %s\n"
                         % (self.client_address[0], message))
        self.send_response(204)
        self.send_header("content-length", "0")
        self.end_headers()

    def send_head(self):
        """Serve it locally, or go and get it first."""
        if not os.path.isfile(self.translate_path(self.path)):
            self._fetch()
        return http.server.SimpleHTTPRequestHandler.send_head(self)

    def _fetch(self):
        """Pull one missing file from the CDN and keep it. Quiet on failure.

        The page asks its *own origin* for the interpreter's pieces -
        `/cdn/cp312/pygame_ce-...whl` and friends - because pygbag's own
        test server is a caching reverse proxy and the template is written
        for one. A plain static server answers 404, pygame never loads,
        and **the page shows a grey canvas with nothing in the browser
        console to explain it**. That cost an afternoon; the whole failure
        is one line in the *server* log.

        Saving into `build/web` rather than a cache directory is the point:
        after one successful load the folder is self-contained, and can be
        copied somewhere and served by anything.
        """
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/") or ".." in path:
            return
        if path.endswith(OURS):
            return
        target = Path(self.translate_path(self.path)).resolve()
        # `translate_path` already collapses `..`, so this cannot fire
        # today. It is here because the day it can, the failure is that
        # this writes anywhere on the disk a request names.
        if ROOT.resolve() not in target.parents:
            return
        url = CDN_HOST + path
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                body = response.read()
        except Exception as exc:                       # noqa: BLE001
            sys.stderr.write("  MISS %s (%s)\n" % (url, exc))
            return
        if not body:
            # An empty 200 is not a file. Writing it caches the emptiness
            # forever, and the browser's automatic `/favicon.ico` probe
            # returns exactly this.
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        sys.stderr.write("  PULLED %s (%d KB)\n" % (path, len(body) // 1024))

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))


def main(argv):
    port = int(argv[1]) if len(argv) > 1 else DEFAULT_PORT
    if not (ROOT / "index.html").is_file():
        print("no build at %s - run tools/build_web.py first" % ROOT)
        return 1
    handler = functools.partial(Handler, directory=str(ROOT))
    # **Threading, and not as a nicety.** A browser holds its connection
    # open, and a single-threaded server sits inside that one conversation
    # forever - so the *second* client to ask for anything simply hangs.
    # That is not a load problem to solve later: it is two tabs, or a phone
    # on the same wifi looking at the build somebody already has open.
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer(("0.0.0.0", port), handler) as httpd:
        print("serving %s at http://localhost:%d" % (ROOT, port))
        # Bound wide on purpose: checking a touch build means opening it on
        # a real phone, and a phone cannot reach `127.0.0.1` on a desktop.
        print("a phone on the same wifi wants http://<this pc>:%d" % port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
