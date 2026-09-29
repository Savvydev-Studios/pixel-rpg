"""Touch input, for a phone.

**DORMANT since 2026-08-23.** Touch is parked (handoff section 37): mobile
was dropped and the browser build was pointed at PC and laptop instead,
so nothing turns these controls on any more. `wants_touch` always says no
and the Settings toggle is the only way in.

Kept rather than deleted because it works, it is tested, and this project
has no git history to recover it from. **Do not maintain it, and do not
delete it without asking.**

**A touch speaks mouse and keys**, which is the third time this codebase has
made that choice and for the third identical reason. Hard rule 9 says the
mouse moves the keyboard's cursor and replays an Enter press rather than
growing a parallel click handler; section 14 says a pad publishes synthetic
keypresses for the same reason. A finger does both: a tap becomes a click, so
every screen that was already clickable is already finished, and the walking
stick publishes held arrow keys, so `move_player` never learns it exists.

**Almost nothing here is about menus**, because the game was built to be
clicked and menus therefore already work. What a phone genuinely lacks is a
way to *walk* and a way to press the half-dozen world keys that have no
on-screen home. That is all this draws.

**Hard rule 6 still holds: no click-to-move.** A tap on the world does
nothing, exactly as a mouse click on the world does nothing. The stick moves
you, and it moves you by publishing arrow keys through the same lookup the
keyboard uses - which is the same allowance section 14 spells out for a pad,
for the same reason. A smoke check taps the world and asserts the player has
not budged.

**On SDL's own touch-to-mouse emulation.** SDL synthesises mouse events from
touches unless told not to, which would mean every tap arriving twice: once
as a finger and once as a mouse. pygame marks the synthetic ones with
`event.touch`, so they are swallowed while this layer is on and passed
through while it is off - a touchscreen laptop with touch support disabled
still behaves like a mouse, which is what somebody using it that way expects.
"""
from __future__ import annotations

import math

import pygame

from .ui import ACCENT, DIM, FAINT, INK, draw_text
from .util import KeyView, clamp

# Finger events. Listed so `Game.handle_event` can hand them over in one
# check, the way it does for the pad.
TOUCH_EVENTS = frozenset({
    pygame.FINGERDOWN, pygame.FINGERUP, pygame.FINGERMOTION,
})

WORLD_STATE = "overworld"
SPRINT_KEY = pygame.K_RSHIFT

# The stick. `DEADZONE` is a fraction of the radius: below it the finger is
# resting rather than steering, which matters far more on glass than on a
# pad because there is no spring to centre it.
STICK_RADIUS = 92
STICK_DEADZONE = 0.28
# Controls shrink on a small window rather than eating it. A phone in
# portrait is 390 points wide, and a full-size stick there covers a third of
# the screen and hangs off the left edge - which is how the first version
# drew it, and only the screenshot said so.
SCALE_AT = 620.0
MIN_SCALE = 0.62
# How far from the stick's home a finger may land and still grab it. Larger
# than the drawn circle on purpose: a thumb aims badly and the cost of
# missing is walking into a monster instead of away from one.
STICK_GRAB = 132

BUTTON_RADIUS = 40
BUTTON_GAP = 14


class _Button:
    __slots__ = ("action", "label", "cx", "cy", "r")

    def __init__(self, action, label):
        self.action = action
        self.label = label
        self.cx = 0
        self.cy = 0
        self.r = BUTTON_RADIUS

    def holds(self, x, y):
        return math.hypot(x - self.cx, y - self.cy) <= self.r + 8


# The four world keys with nowhere else to live. Deliberately the same four
# the pad puts on its face buttons (section 14), so the two devices do not
# teach different games.
BUTTONS = (
    _Button("interact", "Act"),
    _Button("bag", "Bag"),
    _Button("hero", "Hero"),
    _Button("quests", "Log"),
)


class Touch:
    """Finger state. Inert, and free, until somebody enables it.

    Unlike the pad there is nothing to enumerate and nothing to wait for, so
    this costs no startup time at all - the ten-second problem section 14 is
    about does not exist here.
    """

    def __init__(self):
        self.enabled = False
        self.held = set()          # arrow keys the stick is asking for
        self.stick_finger = None   # which finger owns the stick, if any
        self.stick_home = (0, 0)   # where it was first put down
        self.stick_at = (0, 0)     # where it is now
        self.button_finger = {}    # finger id -> button being held
        self.stick_r = STICK_RADIUS
        self.grab_r = STICK_GRAB
        self._size = (0, 0)

    # -- placement -----------------------------------------------------------

    def layout(self, size):
        """Put the controls where this window can fit them.

        Recomputed per frame rather than cached, because a browser window
        resizes whenever the phone is turned and a stale control is one you
        cannot press.
        """
        width, height = size
        self._size = (width, height)
        scale = clamp(min(width, height) / SCALE_AT, MIN_SCALE, 1.0)
        self.stick_r = int(STICK_RADIUS * scale)
        self.grab_r = int(STICK_GRAB * scale)
        radius = int(BUTTON_RADIUS * scale)
        gap = int(BUTTON_GAP * scale)
        margin = int(30 * scale)

        # Far enough in that the whole circle is on the screen. A stick with
        # its left half off the edge cannot be pushed left.
        home_x = max(self.stick_r + margin, int(width * 0.16))
        self.stick_home = (home_x,
                           int(height - self.stick_r - margin - 14))
        right = width - radius - margin
        bottom = height - radius - margin
        step = radius * 2 + gap
        # A diamond, thumb-shaped: the one you press most sits lowest.
        places = ((right, bottom),
                  (right - step, bottom),
                  (right, bottom - step),
                  (right - step, bottom - step))
        for button, (cx, cy) in zip(BUTTONS, places):
            button.cx, button.cy, button.r = cx, cy, radius

    def ceiling(self, size):
        """The highest pixel the controls occupy, for anything drawn above.

        The prompt strip sits at a fixed height off the bottom and the
        buttons are a diamond in the same corner, so on a narrow window
        the two land on each other - which is what `layout.py --phone`
        found the moment a villager stood near enough to the spawn to
        raise a prompt at all (s61).
        """
        self.layout(size)
        return min(button.cy - button.r for button in BUTTONS)

    def active(self, game):
        """Whether the controls are on screen and listening.

        Only in the world. Everywhere else the game is already a set of
        things you can tap, and a stick over a menu is a stick in the way.
        """
        return (self.enabled and game.state == WORLD_STATE
                and not game.dialogue)

    # -- events --------------------------------------------------------------

    def handle_event(self, game, event):
        """One finger event. True if this layer consumed it."""
        if not self.enabled:
            return False
        width, height = game.screen.get_size()
        self.layout((width, height))
        x, y = int(event.x * width), int(event.y * height)
        finger = getattr(event, "finger_id", 0)

        if event.type == pygame.FINGERDOWN:
            return self._down(game, finger, x, y)
        if event.type == pygame.FINGERMOTION:
            return self._motion(game, finger, x, y)
        if event.type == pygame.FINGERUP:
            return self._up(game, finger, x, y)
        return False

    def _down(self, game, finger, x, y):
        if self.active(game):
            if math.hypot(x - self.stick_home[0],
                          y - self.stick_home[1]) <= self.grab_r:
                self.stick_finger = finger
                self.stick_at = (x, y)
                self._recompute()
                return True
            for button in BUTTONS:
                if button.holds(x, y):
                    self.button_finger[finger] = button
                    self._press_action(game, button.action)
                    return True
        # Anything else is a tap, and a tap is a click. Every screen already
        # knows what to do with one of those.
        self._click(game, x, y)
        return True

    def _motion(self, game, finger, x, y):
        if finger == self.stick_finger:
            self.stick_at = (x, y)
            self._recompute()
            return True
        if finger in self.button_finger:
            return True          # a thumb rolling on a button is still on it
        # A dragging finger moves the pointer, so hover works the way it does
        # for a mouse. It does not drag anything: nothing in this game drags.
        game.handle_event(pygame.event.Event(pygame.MOUSEMOTION, pos=(x, y),
                                             rel=(0, 0), buttons=(0, 0, 0),
                                             touch=False))
        return True

    def _up(self, game, finger, _x, _y):
        if finger == self.stick_finger:
            self.stick_finger = None
            self.held = set()
            return True
        if self.button_finger.pop(finger, None) is not None:
            return True
        return True

    # -- turning fingers into what the game already understands ---------------

    def _click(self, game, x, y):
        """A tap, as the mouse layer would have delivered it.

        Motion first, because hover is what tells a screen which row the
        pointer is over, and a click with no preceding motion would land on
        whatever was hovered last.
        """
        for event in (
            pygame.event.Event(pygame.MOUSEMOTION, pos=(x, y), rel=(0, 0),
                               buttons=(0, 0, 0), touch=False),
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=(x, y), button=1,
                               touch=False),
            pygame.event.Event(pygame.MOUSEBUTTONUP, pos=(x, y), button=1,
                               touch=False),
        ):
            game.handle_event(event)

    def _press_action(self, game, action):
        code = game.binds.key_for(action)
        if code is None:
            return
        # See `gamepad._press`: a thumbstick publishes arrow keys too,
        # and the settings screen is deaf to real ones.
        game.handle_event(pygame.event.Event(pygame.KEYDOWN, key=code,
                                             unicode="", mod=0,
                                             synthetic=True))

    def _recompute(self):
        """Which arrow keys the stick is currently asking for."""
        dx = self.stick_at[0] - self.stick_home[0]
        dy = self.stick_at[1] - self.stick_home[1]
        distance = math.hypot(dx, dy)
        dead = self.stick_r * STICK_DEADZONE
        if distance < dead:
            self.held = set()
            return
        held = set()
        # Both axes, so diagonals work: a thumb is never on an axis and a
        # stick that snapped to four directions would fight it constantly.
        if abs(dx) > dead:
            held.add(pygame.K_RIGHT if dx > 0 else pygame.K_LEFT)
        if abs(dy) > dead:
            held.add(pygame.K_DOWN if dy > 0 else pygame.K_UP)
        # Pushed to the edge means running. There is no trigger to hold.
        if distance > self.stick_r * 0.92:
            held.add(SPRINT_KEY)
        self.held = held

    def update(self, _dt, game):
        """Drop a held direction if the world went away under it."""
        if not self.active(game) and self.held:
            self.held = set()
            self.stick_finger = None

    def wrap_keys(self, real):
        """The keyboard state the overworld should read."""
        if not self.enabled or not self.held:
            return real
        return KeyView(real, self.held)

    def swallow_mouse(self, event):
        """Whether a mouse event is SDL's echo of a touch we already handled.

        Passed through while this layer is off, so a touchscreen laptop still
        behaves like a mouse for somebody using it that way.
        """
        return self.enabled and bool(getattr(event, "touch", False))

    # -- drawing --------------------------------------------------------------

    def draw(self, game):
        """The stick and the four buttons, over the world.

        Drawn last and drawn faint. These sit on top of the game and are worth
        the pixels only while a thumb is near them, so the stick brightens
        under a finger and the buttons are outlines rather than slabs.
        """
        if not self.active(game):
            return
        surface = game.screen
        self.layout(surface.get_size())
        layer = pygame.Surface(surface.get_size(), pygame.SRCALPHA)

        home = self.stick_home
        holding = self.stick_finger is not None
        knob_r = max(18, int(self.stick_r * 0.37))
        pygame.draw.circle(layer, (230, 236, 255, 34 if holding else 22),
                           home, self.stick_r)
        pygame.draw.circle(layer, (230, 236, 255, 92 if holding else 54),
                           home, self.stick_r, 3)
        knob = home
        if holding:
            dx = self.stick_at[0] - home[0]
            dy = self.stick_at[1] - home[1]
            distance = math.hypot(dx, dy) or 1.0
            reach = min(distance, self.stick_r)
            knob = (int(home[0] + dx / distance * reach),
                    int(home[1] + dy / distance * reach))
        pygame.draw.circle(layer, (250, 224, 150, 150 if holding else 96),
                           knob, knob_r)
        pygame.draw.circle(layer, (250, 224, 150, 200), knob, knob_r, 2)

        for button in BUTTONS:
            pressed = button in self.button_finger.values()
            pygame.draw.circle(layer, (240, 236, 255, 46 if pressed else 26),
                               (button.cx, button.cy), button.r)
            pygame.draw.circle(layer, (250, 224, 150, 190) if pressed
                               else (230, 236, 255, 108),
                               (button.cx, button.cy), button.r, 3)
        surface.blit(layer, (0, 0))

        for button in BUTTONS:
            draw_text(surface, game.assets, button.label,
                      (button.cx, button.cy - 7), "small",
                      ACCENT if button in self.button_finger.values() else INK,
                      center=True)


def wants_touch(game):
    """Whether this machine should have the controls on without being asked.

    **Always no, since 2026-08-23.** Touch is parked - handoff section 37.
    Savvydev dropped mobile, kept the browser build, and pointed it at PC and
    laptop instead.

    Both halves of the old answer were about phones, and the browser half
    had *inverted*. It read: a browser is assumed to be a phone, because
    nobody reaches the browser build from a laptop by accident. Reaching it
    from a laptop is now the entire point of it, so that assumption would
    now put a thumbstick over a keyboard game on a desktop.

    The controls themselves still work and Settings still switches them on.
    Nothing switches them on for you. The check that is gone, for whoever
    unparks this:

        config_mod.is_browser() or pygame.get_num_touch_devices() > 0
    """
    return False
