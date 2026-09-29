"""Small, dependency-free helpers used across the whole game.

Nothing in here touches the filesystem, the network, or the environment, which
keeps it trivially unit-testable and safe to import from anywhere.
"""
from __future__ import annotations

import math
import contextlib
import random
import zlib
from dataclasses import dataclass, field

# ---------------------------------------------------------------- numbers ---

# **The level ceiling lives here, not in `entities`.** `items.py` needs it
# to clamp an item level and cannot import `entities` - `entities` imports
# `items`. A constant two modules share belongs under both of them.
MAX_LEVEL = 100


def stable_seed(value, modulo=9973):
    """A number made from `value` that is the same everywhere, every time.

    **Not `hash()`** (§95). Python salts the hash of a string differently
    in every process, so `abs(hash(arena.key)) % 9973` put an arena's
    cover somewhere new on every launch - and, in a room, somewhere
    different on the host and the guest: the host's heroes walked round
    rocks the guest could not see, and stopped at ones the guest could.
    CRC32 of the value's `repr` is plain arithmetic on its bytes.
    """
    return zlib.crc32(repr(value).encode("utf-8")) % modulo


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def lerp(a, b, t):
    return a + (b - a) * t


def inv_lerp(a, b, v):
    return 0.0 if b == a else clamp((v - a) / (b - a), 0.0, 1.0)


def approach(cur, target, delta):
    """Move `cur` toward `target` by at most `delta`."""
    if cur < target:
        return min(cur + delta, target)
    return max(cur - delta, target)


def ease_out_cubic(t):
    t = clamp(t, 0.0, 1.0)
    return 1.0 - (1.0 - t) ** 3


def ease_in_cubic(t):
    t = clamp(t, 0.0, 1.0)
    return t * t * t


def ease_out_back(t):
    t = clamp(t, 0.0, 1.0)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def ease_in_out_sine(t):
    return -(math.cos(math.pi * clamp(t, 0.0, 1.0)) - 1) / 2


def pulse(time_s, speed=1.0):
    """0..1 oscillation, handy for glows and selection highlights."""
    return (math.sin(time_s * speed * math.tau) + 1.0) * 0.5


# ----------------------------------------------------------------- colors ---

def lerp_color(c1, c2, t):
    t = clamp(t, 0.0, 1.0)
    return (
        int(lerp(c1[0], c2[0], t)),
        int(lerp(c1[1], c2[1], t)),
        int(lerp(c1[2], c2[2], t)),
    )


def shade(color, factor):
    """factor < 1 darkens, > 1 brightens. Always returns a valid RGB tuple."""
    return tuple(int(clamp(c * factor, 0, 255)) for c in color[:3])


def with_alpha(color, a):
    return (color[0], color[1], color[2], int(clamp(a, 0, 255)))


def hsv_to_rgb(h, s, v):
    i = int(h * 6.0)
    f = h * 6.0 - i
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    i %= 6
    r, g, b = [(v, t, p), (q, v, p), (p, v, t),
               (p, q, v), (t, p, v), (v, p, q)][i]
    return (int(r * 255), int(g * 255), int(b * 255))


# -------------------------------------------------------------------- rng ---

class KeyView:
    """`pygame.key.get_pressed()` with some held keys folded in.

    `move_player` only ever indexes its `keys` argument, so presenting one of
    these keeps a controller's - or a thumbstick's - movement inside the
    existing movement code instead of growing a second copy of it.

    Lives here rather than in either device because both need it and neither
    owns it. They also stack: a `KeyView` over a `KeyView` is fine, which is
    what happens when a pad and a touchscreen are both switched on.
    """

    __slots__ = ("_real", "_virtual")

    def __init__(self, real, virtual):
        self._real = real
        self._virtual = virtual

    def __getitem__(self, code):
        return bool(self._real[code]) or code in self._virtual

    def __len__(self):
        return len(self._real)


class RNG:
    """Seedable RNG wrapper.

    Using an explicit instance rather than the `random` module globals means a
    seeded run is genuinely reproducible even if some other library reseeds the
    global generator behind our back.
    """

    def __init__(self, seed=None):
        self.seed_value = seed
        self._r = random.Random(seed)

    def reseed(self, seed=None):
        self.seed_value = seed
        self._r.seed(seed)

    @contextlib.contextmanager
    def forked(self, seed):
        """Roll from a named seed, then put the stream back where it was.

        Two machines have to agree on an encounter, so it is built from a
        seed both of them were told. That must not cost the rest of the game
        its unpredictability - a plain `reseed` here would leave every roll
        for the rest of the session running off a number that travelled over
        a socket.
        """
        state = self._r.getstate()
        self._r.seed(seed)
        try:
            yield self
        finally:
            self._r.setstate(state)

    def chance(self, p):
        return self._r.random() < p

    def rint(self, a, b):
        if a > b:
            a, b = b, a
        return self._r.randint(a, b)

    def rfloat(self, a=0.0, b=1.0):
        return self._r.uniform(a, b)

    def shuffle(self, seq):
        """In place, from the seeded stream like everything else.

        Returns the list as well, so it can be used inline.

        **There were two of these** until 2026-09-03, forty lines apart,
        and Python had been keeping the second one for as long as they had
        both existed. They drew from the stream identically - the only
        difference was the return value - so nothing was ever wrong and the
        balance table never moved, which is exactly why nobody found it.
        `TestNothingIsDefinedTwice` did.
        """
        self._r.shuffle(seq)
        return seq

    def choice(self, seq):
        return self._r.choice(seq)

    def choices(self, seq, weights=None, k=1):
        return self._r.choices(seq, weights=weights, k=k)

    def sample(self, seq, k):
        k = min(k, len(seq))
        return self._r.sample(list(seq), k)

    def gauss(self, mu, sigma):
        return self._r.gauss(mu, sigma)


# Shared instance. `Game` reseeds this at startup from settings.
rng = RNG()

#: **How things look, never what happens** (s96): a camera shake, a
#: flicker. The camera rolled its shake on `rng` and `draw_battle` moved
#: the camera, so every frame drawn during a shake spent two numbers of
#: the stream the next damage roll comes from - a fight's outcome
#: depended on how many frames were drawn. Presentation rolls here.
fx_rng = RNG()


# ------------------------------------------------------------------ timer ---

@dataclass
class Timer:
    """Counts down in seconds; `done` latches once it hits zero."""
    duration: float = 0.0
    remaining: float = 0.0

    def start(self, duration):
        self.duration = max(0.0, float(duration))
        self.remaining = self.duration

    def tick(self, dt):
        if self.remaining > 0.0:
            self.remaining = max(0.0, self.remaining - dt)
        return self.remaining <= 0.0

    @property
    def done(self):
        return self.remaining <= 0.0

    @property
    def progress(self):
        if self.duration <= 0.0:
            return 1.0
        return clamp(1.0 - self.remaining / self.duration, 0.0, 1.0)


# ------------------------------------------------------------------- text ---

def wrap_text(text, font, max_width):
    """Greedy word wrap. Falls back to hard character splits for long words."""
    if not text:
        return [""]
    lines, line = [], ""
    for word in str(text).split(" "):
        probe = word if not line else line + " " + word
        if font.size(probe)[0] <= max_width:
            line = probe
            continue
        if line:
            lines.append(line)
            line = ""
        # Word alone is wider than the box: split it hard.
        if font.size(word)[0] > max_width:
            chunk = ""
            for ch in word:
                if font.size(chunk + ch)[0] > max_width and chunk:
                    lines.append(chunk)
                    chunk = ch
                else:
                    chunk += ch
            line = chunk
        else:
            line = word
    if line:
        lines.append(line)
    return lines or [""]


def roman(n):
    """Small roman numerals for skill ranks and item tiers."""
    n = int(clamp(n, 1, 39))
    table = [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for value, sym in table:
        while n >= value:
            out += sym
            n -= value
    return out


def article(word):
    """"a" or "an" for a following word. Vowel rule, which is enough here."""
    return "an" if word[:1].lower() in "aeiou" else "a"


def commafy(n):
    return f"{int(n):,}"


# ---------------------------------------------------------------- geometry ---

def dist2(ax, ay, bx, by):
    dx, dy = ax - bx, ay - by
    return dx * dx + dy * dy


def normalize(dx, dy):
    mag = math.hypot(dx, dy)
    if mag == 0:
        return 0.0, 0.0
    return dx / mag, dy / mag
