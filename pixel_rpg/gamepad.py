"""Game controller support.

A pad speaks keys. Every button press is turned into a synthetic KEYDOWN and
handed to `Game.handle_event`, which is exactly what the mouse layer already
does, and for the same reason: there is then only one implementation of what
any control does, and a screen nobody remembered to update still works.
Directions are held rather than pressed, so they are published as virtual
arrow keys that `move_player` reads through the same lookup as the keyboard.

**Enumeration is opt-in and never happens at startup.** `pygame.joystick`
and the SDL controller subsystem share one enumeration, which measured 10.2s
on the development machine. Pushed onto a worker thread it took 20s *and*
dragged the main loop down to 17fps, so there is no free way to hide it. That
is the same ten seconds `assets.init_pygame()` exists to avoid, so the pad is
only looked for when somebody asks for it, and the game says what it is doing
while it waits.

SDL's own controller database supplies the mapping, so Xbox, DualShock,
DualSense, Switch Pro and the common third-party pads all arrive as the same
A/B/X/Y + d-pad + sticks layout. Devices SDL does not recognise as game
controllers are ignored, which is how a keyboard's wireless dongle stops
registering as a pad.
"""
from __future__ import annotations

import pygame

from .ui import ACCENT, FAINT, INK, draw_text, panel
from .util import KeyView

# How far a stick must leave centre before it counts as a direction. High
# enough that a worn stick resting off-centre does not walk you into a fight.
DEADZONE = 0.45
# A trigger is analog; past this it counts as held.
TRIGGER_POINT = 0.6
# Menu repeat: a pause before the first repeat, then a steady rate.
REPEAT_FIRST = 0.42
REPEAT_NEXT = 0.14
AXIS_RANGE = 32767.0

_B = pygame  # shorthand: the CONTROLLER_BUTTON_* constants live on pygame

# Event types that belong to a pad. Axis motion is listed so it is swallowed
# rather than falling through: sticks are polled in update(), because a held
# direction is a state, not a stream of events. The raw JOY* types arrive
# alongside the CONTROLLER* ones for the same device and are ignored.
PAD_EVENTS = frozenset({
    pygame.CONTROLLERBUTTONDOWN, pygame.CONTROLLERBUTTONUP,
    pygame.CONTROLLERAXISMOTION,
    pygame.CONTROLLERDEVICEADDED, pygame.CONTROLLERDEVICEREMOVED,
    pygame.JOYBUTTONDOWN, pygame.JOYBUTTONUP, pygame.JOYAXISMOTION,
    pygame.JOYHATMOTION, pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED,
})

# Buttons that mean the same thing on every screen.
COMMON_BUTTONS = {
    _B.CONTROLLER_BUTTON_B: pygame.K_ESCAPE,
    _B.CONTROLLER_BUTTON_START: pygame.K_ESCAPE,
}

# On menus a pad is just a keyboard with fewer keys.
MENU_BUTTONS = {
    _B.CONTROLLER_BUTTON_A: pygame.K_RETURN,
    _B.CONTROLLER_BUTTON_LEFTSHOULDER: pygame.K_TAB,
    _B.CONTROLLER_BUTTON_RIGHTSHOULDER: pygame.K_TAB,
}

# In the overworld the face buttons open things, so they resolve through the
# player's own bindings rather than hard-coded keys. Rebinding "bag" moves the
# pad button with it.
OVERWORLD_BUTTONS = {
    _B.CONTROLLER_BUTTON_A: "interact",
    _B.CONTROLLER_BUTTON_X: "bag",
    _B.CONTROLLER_BUTTON_Y: "hero",
    _B.CONTROLLER_BUTTON_LEFTSHOULDER: "quests",
    _B.CONTROLLER_BUTTON_RIGHTSHOULDER: "guide",
    _B.CONTROLLER_BUTTON_BACK: "save",
}

# Directions are published as arrow keys and RSHIFT because those always work:
# arrows walk whatever else is bound (keybinds.FALLBACK_MOVEMENT) and RSHIFT is
# always a sprint key. Going through the bindings instead would let somebody
# rebind their way out of a working pad.
DPAD_KEYS = {
    _B.CONTROLLER_BUTTON_DPAD_UP: pygame.K_UP,
    _B.CONTROLLER_BUTTON_DPAD_DOWN: pygame.K_DOWN,
    _B.CONTROLLER_BUTTON_DPAD_LEFT: pygame.K_LEFT,
    _B.CONTROLLER_BUTTON_DPAD_RIGHT: pygame.K_RIGHT,
}
SPRINT_KEY = pygame.K_RSHIFT

# game.OVERWORLD, repeated rather than imported because game imports this
# module. A unit test asserts the two still agree.
WORLD_STATE = "overworld"

# Buttons the pad must not send while a screen is waiting for a key to bind,
# or you would bind a keyboard key by pressing a pad button.
BUTTON_LABELS = {
    _B.CONTROLLER_BUTTON_A: "A",
    _B.CONTROLLER_BUTTON_B: "B",
    _B.CONTROLLER_BUTTON_X: "X",
    _B.CONTROLLER_BUTTON_Y: "Y",
    _B.CONTROLLER_BUTTON_LEFTSHOULDER: "LB",
    _B.CONTROLLER_BUTTON_RIGHTSHOULDER: "RB",
    _B.CONTROLLER_BUTTON_START: "Start",
    _B.CONTROLLER_BUTTON_BACK: "Select",
}


class Gamepad:
    """Controller state. Inert, and free, until somebody enables it."""

    def __init__(self):
        self.enabled = False
        self.pending = False       # enable once the first frame is on screen
        self.pads = []
        self.held = set()          # virtual key codes currently down
        self.status = ""           # what to show in Settings
        self._repeat = {}          # key code -> seconds until it repeats
        self._started = False      # the expensive enumeration has been paid

    # -- lifecycle ---------------------------------------------------------

    def names(self):
        out = []
        for pad in self.pads:
            try:
                out.append(pad.name)
            except (AttributeError, pygame.error):
                continue
        return out

    def enable(self):
        """Pay for enumeration and open every pad SDL recognises.

        Blocking, and slow enough to be worth a notice on screen. Safe to call
        again; the subsystem is only started once.
        """
        try:
            from pygame._sdl2 import controller
        except ImportError:
            self.status = "not available in this build"
            self.enabled = False
            return False
        try:
            if not self._started:
                controller.init()
                self._started = True
            self._open_all(controller)
        except (pygame.error, OSError) as exc:
            self.status = "could not start: %s" % exc
            self.enabled = False
            return False
        self.enabled = True
        found = self.names()
        self.status = ", ".join(found) if found else "none connected"
        return True

    def _open_all(self, controller):
        self.pads = []
        for index in range(pygame.joystick.get_count()):
            try:
                if not controller.is_controller(index):
                    continue          # a joystick SDL has no mapping for
                pad = controller.Controller(index)
            except (pygame.error, ValueError):
                continue
            self.pads.append(pad)

    def disable(self):
        """Stop listening. The subsystem stays up: shutting it down would only
        mean paying the ten seconds again if they change their mind."""
        self.enabled = False
        self.pads = []
        self.held.clear()
        self._repeat.clear()
        self.status = "off"

    def device_changed(self, event):
        """Hot-plug. Only meaningful once enumeration has been paid for."""
        if not self.enabled or not self._started:
            return
        try:
            from pygame._sdl2 import controller
            self._open_all(controller)
        except (ImportError, pygame.error):
            return
        found = self.names()
        self.status = ", ".join(found) if found else "none connected"

    # -- input -------------------------------------------------------------

    def handle_event(self, game, event):
        """Translate a pad button into a keypress. True if it was consumed."""
        if event.type in (pygame.CONTROLLERDEVICEADDED,
                          pygame.CONTROLLERDEVICEREMOVED):
            self.device_changed(event)
            return True
        if not self.enabled or event.type != pygame.CONTROLLERBUTTONDOWN:
            return False
        if event.button in DPAD_KEYS:
            return True           # directions are polled, not pressed
        code = self._key_for(game, event.button)
        if code is None:
            return True
        self._press(game, code)
        return True

    def _key_for(self, game, button):
        if button in COMMON_BUTTONS:
            return COMMON_BUTTONS[button]
        if self._in_world(game):
            action = OVERWORLD_BUTTONS.get(button)
            if action is None:
                return None
            return game.binds.key_for(action)
        return MENU_BUTTONS.get(button)

    @staticmethod
    def _in_world(game):
        """Walking around, as opposed to reading a screen."""
        return game.state == WORLD_STATE and not game.dialogue

    def _press(self, game, code):
        # `synthetic` marks this as a pad speaking keys rather than a
        # finger on a keyboard. Only the settings screen looks at it, and
        # only because it went deaf to the arrows for the konami sequence
        # - a d-pad publishes those arrows and would have been deafened
        # with them, leaving a controller unable to use settings at all.
        game.handle_event(pygame.event.Event(
            pygame.KEYDOWN, key=code, unicode="", mod=0,
            synthetic=True))

    def update(self, dt, game):
        """Poll the sticks and d-pad.

        In the world a direction is *held*, so it becomes a virtual arrow key.
        On a menu a direction is *pressed*, and repeats while it is held, so it
        becomes a stream of arrow keypresses. Both go through the keyboard.
        """
        if not self.enabled or not self.pads:
            return
        directions = self._directions()
        if self._in_world(game):
            self.held = directions
            self._repeat.clear()
            return
        self.held = set()
        self._repeat_keys(dt, game, directions)

    def _directions(self):
        """Which arrow keys the sticks and d-pad are currently asking for."""
        held = set()
        for pad in self.pads:
            try:
                x = pad.get_axis(pygame.CONTROLLER_AXIS_LEFTX) / AXIS_RANGE
                y = pad.get_axis(pygame.CONTROLLER_AXIS_LEFTY) / AXIS_RANGE
                trigger = (pad.get_axis(pygame.CONTROLLER_AXIS_TRIGGERRIGHT)
                           / AXIS_RANGE)
                buttons = {b: pad.get_button(b) for b in DPAD_KEYS}
            except (pygame.error, AttributeError):
                continue
            if x <= -DEADZONE:
                held.add(pygame.K_LEFT)
            elif x >= DEADZONE:
                held.add(pygame.K_RIGHT)
            if y <= -DEADZONE:
                held.add(pygame.K_UP)
            elif y >= DEADZONE:
                held.add(pygame.K_DOWN)
            for button, code in DPAD_KEYS.items():
                if buttons.get(button):
                    held.add(code)
            if trigger >= TRIGGER_POINT:
                held.add(SPRINT_KEY)
        return held

    def _repeat_keys(self, dt, game, directions):
        """Turn a held direction into a repeating keypress on menus."""
        for code in list(self._repeat):
            if code not in directions:
                del self._repeat[code]
        for code in directions:
            if code == SPRINT_KEY:
                continue
            remaining = self._repeat.get(code)
            if remaining is None:
                self._repeat[code] = REPEAT_FIRST
                self._press(game, code)
                continue
            remaining -= dt
            if remaining <= 0:
                self._repeat[code] = REPEAT_NEXT
                self._press(game, code)
            else:
                self._repeat[code] = remaining

    def wrap_keys(self, real):
        """The keyboard state the overworld should read."""
        if not self.enabled or not self.held:
            return real
        return KeyView(real, self.held)


def draw_searching(game):
    """Say why the game has stopped, before it stops.

    Enumeration blocks for about ten seconds and there is no way to make it
    not; the least it can do is admit what it is waiting for rather than
    looking hung.
    """
    surface, assets = game.screen, game.assets
    width, height = surface.get_size()
    # Fits the window rather than assuming 420 will. At 390 wide the
    # fixed box hung 15px off each edge and its border was clipped away,
    # so it read as a full-width band instead of a panel. Nothing caught
    # it because this module was missing from `layout.py`'s
    # instrumentation list - the silent pass section 6 warns about.
    box_w = min(420, width - 32)
    box = pygame.Rect((width - box_w) // 2, height // 2 - 52, box_w, 104)
    panel(surface, box)
    draw_text(surface, assets, "Looking for controllers",
              (box.centerx, box.y + 30), "small", INK, center=True)
    draw_text(surface, assets, "This takes a few seconds.",
              (box.centerx, box.y + 58), "tiny", ACCENT, center=True)
    draw_text(surface, assets, "It only happens while the setting is on.",
              (box.centerx, box.y + 76), "tiny", FAINT, center=True)
    pygame.display.flip()
