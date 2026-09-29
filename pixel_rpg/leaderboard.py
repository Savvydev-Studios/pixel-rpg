"""The global board (item 0az), and the seam it goes through.

**Roadmap item 0az**, his words (2026-09-06):

> "also there should be a global score leaderboard for each gamemode for
> each party size"

And the constraint, corrected by him the same day after this project had
spent a fortnight working from a bad summary of it:

> "well i said no hosting stuff off of my pc, like i dont want ALL global
> servers running off of my pc, but you can still use FREE 3rd party
> tools for the global stuff."

> "js use some free tools, up to you"

So: **no service of ours runs anywhere**, this file talks to somebody
else's free one, and which one is a setting rather than a decision baked
into the source.

## What this is and is not

It is a **client**, and a deliberately small one: it turns a `RunRecord`
into a row, posts it, and reads rows back. It is not a protocol, it does
not retry forever, and **it never blocks a frame** - every call is meant
to be made off the main path or not at all, and every failure is a
shrug.

**The local board is the real one until this is configured**
(`meta.MetaProfile.board`). That ordering matters: a leaderboard that
breaks the records screen when the wifi is off is worse than no
leaderboard, so the global rows are an *addition* to a screen that works
without them.

## Configuration, and why there is no key in this file

`PIXEL_RPG_BOARD_URL` and `PIXEL_RPG_BOARD_KEY`, both through
`config.get_secret` -> `os.environ` (hard rule 2.10, and rule 31.2). An
unset URL means the feature is simply off, which is the state every
build ships in: **nobody gets a global board by accident, and no key of
his is ever in a file that could be pushed.**

`.env.example` carries the placeholders and `.env` is git-ignored.

## What comes back is not trusted

Rows arrive over the network from strangers, which makes this the least
trustworthy input in the game - more so than a save file, which at least
started on the player's own disk. So every field is clamped or dropped
(`security.safe_*`), the response is size-capped before it is parsed,
and a name is sanitised the same way a player's own is. §8's posture, at
the one place it matters most.
"""
from __future__ import annotations

import json

from . import config, security as sec

#: Give up on a request after this long. **Short on purpose**: this is a
#: nicety on a records screen, and a player staring at a frozen menu
#: because somebody's free tier is asleep is a worse game than one with
#: no board at all.
TIMEOUT = 4.0

#: Refuse a response bigger than this before parsing it. A board is ten
#: short rows; anything megabyte-shaped is either a mistake or somebody
#: trying something.
MAX_BYTES = 64 * 1024

#: How many rows a board asks for and will accept.
TOP_N = 10

#: **Rate limit, his standing baseline** (rule 31.2): five attempts per
#: fifteen minutes on anything retryable. A submit is not login-shaped,
#: but a client that hammers a free tier gets the free tier taken away.
SUBMITS, SUBMIT_WINDOW = 5, 900.0


def _mode_keys():
    """Every gamemode key, for validating one off the network.

    Read from `gamemodes` rather than typed here - the typed version was
    already stale for The Hoard (§88).
    """
    from . import gamemodes
    return tuple(mode.key for mode in gamemodes.MODES)


def _clean_row(raw):
    """One row off the wire, or None if it is not one.

    Everything is clamped rather than validated-and-rejected where a
    sane value exists, because a board that shows nine rows instead of
    ten when one is malformed is better than one that shows an error.
    """
    raw = sec.safe_dict(raw)
    name = sec.sanitize_name(sec.safe_str(raw.get("name"), 18)) or "somebody"
    # **Every mode the game has** (§88). A hand-kept tuple here turned a
    # hoard score coming back off the board into a wave score - the same
    # clamp bug §87 found in `coop._fight_opened`, in the file next door.
    mode = sec.safe_choice(raw.get("mode"), _mode_keys(), "waves")
    return {
        "name": name,
        "mode": mode,
        "party": sec.safe_int(raw.get("party"), 1, 4, 1),
        "score": sec.safe_int(raw.get("score"), 0, 99_999_999, 0),
        "waves": sec.safe_int(raw.get("waves"), 0, 99_999, 0),
        "class_key": sec.safe_str(raw.get("class_key"), 24),
        "when": sec.safe_str(raw.get("when"), 24),
    }


class Leaderboard:
    """Post a score, read the top of a board. Never raises at a caller.

    `transport` is injected so the tests can drive this without a
    network - which is the only way it *can* be tested, and §7's rule
    about a fallback nothing exercises applies with force here: the
    disabled path is the one every player is on.
    """

    def __init__(self, transport=None, clock=None):
        self.url = (config.get_secret("PIXEL_RPG_BOARD_URL") or "").strip()
        self.key = (config.get_secret("PIXEL_RPG_BOARD_KEY") or "").strip()
        self._transport = transport
        self._clock = clock
        self._sent = []
        self.last_error = ""

    # -- state -------------------------------------------------------------

    @property
    def enabled(self):
        """Whether there is anywhere to talk to.

        **Off is the shipped state.** A build with no URL set has no
        global board and says so quietly; it does not fail, warn, or ask
        anybody to configure anything.
        """
        return bool(self.url)

    def _now(self):
        if self._clock is not None:
            return float(self._clock())
        import time
        return time.time()

    def may_submit(self):
        """Whether the rate limit allows another post."""
        now = self._now()
        self._sent = [t for t in self._sent if now - t < SUBMIT_WINDOW]
        return len(self._sent) < SUBMITS

    # -- the wire ----------------------------------------------------------

    def _send(self, method, path, payload=None):
        """One request. Returns parsed JSON, or None for any failure.

        **Every exception ends here**, which is the whole contract: a
        caller asks for a board and gets rows or nothing, and a records
        screen never learns that sockets exist.
        """
        if not self.enabled:
            return None
        try:
            if self._transport is not None:
                body = self._transport(method, self.url + path, payload,
                                       self.key)
            else:
                body = self._http(method, self.url + path, payload)
            if body is None:
                return None
            if len(body) > MAX_BYTES:
                self.last_error = "response too large"
                return None
            return json.loads(body)
        except Exception as exc:                    # noqa: BLE001
            # Deliberately everything: a DNS failure, a 500, a captive
            # portal returning HTML, a free tier that has gone away. None
            # of them is the game's problem and all of them look the same
            # from here.
            self.last_error = str(exc)[:120]
            return None

    def _http(self, method, url, payload):
        """The real transport. stdlib only - no new dependency (rule 2.2)."""
        import urllib.request
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.key:
            # A header, never a query string: a key in a URL ends up in
            # somebody's access log (§8, and the standing rule about not
            # putting anything sensitive in a query).
            headers["Authorization"] = "Bearer %s" % self.key
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            return answer.read(MAX_BYTES + 1).decode("utf-8", "replace")

    # -- what a caller wants -----------------------------------------------

    def submit(self, record, name):
        """Post one finished run. True if it went.

        **A marked run never gets here** - `MetaProfile.finish_run`
        refuses one (§16) and this is called with what that produced, so
        the debug menu cannot post a score by a different door.
        """
        if not self.enabled or not self.may_submit():
            return False
        row = {"name": sec.sanitize_name(str(name or ""))[:18],
               "mode": str(getattr(record, "mode", "story")),
               "party": int(getattr(record, "party", 1)),
               "score": int(getattr(record, "score", 0)),
               "waves": int(getattr(record, "waves", 0)),
               "class_key": str(getattr(record, "class_key", "")),
               "when": str(getattr(record, "when", ""))}
        self._sent.append(self._now())
        return self._send("POST", "/scores", row) is not None

    def top(self, mode="waves", party=1, limit=TOP_N):
        """The best rows for one board, cleaned. `[]` for any failure."""
        limit = max(1, min(int(limit), TOP_N))
        answer = self._send(
            "GET", "/scores?mode=%s&party=%d&limit=%d"
            % (sec.safe_choice(mode, _mode_keys(), "waves"),
               sec.safe_int(party, 1, 4, 1), limit))
        rows = answer.get("scores") if isinstance(answer, dict) else answer
        if not isinstance(rows, list):
            return []
        out = []
        for raw in rows[:limit]:
            row = _clean_row(raw)
            if row:
                out.append(row)
        out.sort(key=lambda r: (-r["score"], -r["waves"]))
        return out
