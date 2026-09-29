"""Rebindable controls.

Only the actions people actually want to move are bindable: walking,
interacting and the screen shortcuts. Menu navigation (arrows, Enter, Esc,
Tab) stays fixed, because rebinding the keys you need in order to reach the
rebinding screen is a good way to lock yourself out of your own game.

Bindings live in the meta profile, so they are shared by every save.
"""
from __future__ import annotations

import pygame

from . import security as sec

# action -> (label, default key)
ACTIONS = [
    ("up", "Walk up", pygame.K_w),
    ("down", "Walk down", pygame.K_s),
    ("left", "Walk left", pygame.K_a),
    ("right", "Walk right", pygame.K_d),
    ("sprint", "Sprint", pygame.K_LSHIFT),
    ("interact", "Interact", pygame.K_e),
    ("bag", "Open bag", pygame.K_i),
    ("hero", "Hero sheet", pygame.K_c),
    ("quests", "Quest notes", pygame.K_j),
    ("guide", "Guide", pygame.K_g),
    # **The map was never a bindable action** (2026-08-31), which is why it
    # was missing from the footer: that strip is built from `binds`, so a
    # key the binds do not know about cannot be advertised and cannot be
    # rebound. `event_overworld` had `pygame.K_m` written into it directly.
    ("map", "Map", pygame.K_m),
    # **Trading needed a key of its own** (2026-09-03, s50). Interact was
    # the obvious home and it is already spoken for: standing next to a
    # friend at a chest, `E` has to mean the chest.
    ("trade", "Trade", pygame.K_t),
    ("save", "Save", pygame.K_F5),
    # Only offered once the debug menu has been found. Listing it before then
    # would give the secret away from the Controls screen, which is the one
    # place somebody is guaranteed to look.
    ("debug", "Debug menu", pygame.K_F10),
]

# Actions that stay out of the rebinding screen until they have been earned.
SECRET_ACTIONS = {"debug"}


def visible_actions(found_devmenu=False):
    """The rows the Controls screen should show."""
    return [entry for entry in ACTIONS
            if entry[0] not in SECRET_ACTIONS or found_devmenu]

ACTION_KEYS = [a for a, _label, _key in ACTIONS]
LABELS = {a: label for a, label, _key in ACTIONS}
DEFAULTS = {a: key for a, _label, key in ACTIONS}

# Keys the interface needs no matter what, so they cannot be claimed.
RESERVED = {pygame.K_RETURN, pygame.K_ESCAPE, pygame.K_TAB, pygame.K_UP,
            pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT, pygame.K_SPACE,
            pygame.K_BACKSPACE, pygame.K_F11}

# Arrows always walk as well, whatever else is bound.
FALLBACK_MOVEMENT = {
    pygame.K_UP: "up", pygame.K_DOWN: "down",
    pygame.K_LEFT: "left", pygame.K_RIGHT: "right",
}


def key_name(code):
    try:
        name = pygame.key.name(int(code))
    except (TypeError, ValueError, pygame.error):
        return "?"
    special = {"left shift": "Shift", "right shift": "Shift",
               "left ctrl": "Ctrl", "escape": "Esc", "return": "Enter"}
    name = special.get(name, name)
    return name.upper() if len(name) == 1 else name.title()


class Keybinds:
    def __init__(self, mapping=None):
        self.map = dict(DEFAULTS)
        if mapping:
            self.apply_dict(mapping)

    def key_for(self, action):
        return self.map.get(action, DEFAULTS.get(action))

    def label_for(self, action):
        return key_name(self.key_for(action))

    def action_for(self, code):
        """Which action a pressed key triggers, if any."""
        for action, key in self.map.items():
            if key == code:
                return action
        return None

    def conflict(self, action, code):
        """The action already using this key, if it is not this one."""
        for other, key in self.map.items():
            if key == code and other != action:
                return other
        return None

    def rebind(self, action, code):
        """Returns (ok, message). Swaps on conflict rather than duplicating."""
        if action not in DEFAULTS:
            return False, "unknown action"
        if code in RESERVED:
            return False, "%s is needed by the menus" % key_name(code)
        clash = self.conflict(action, code)
        if clash:
            self.map[clash] = self.map.get(action, DEFAULTS[action])
            self.map[action] = code
            return True, "swapped with %s" % LABELS[clash]
        self.map[action] = code
        return True, "bound to %s" % key_name(code)

    def reset(self):
        self.map = dict(DEFAULTS)

    def to_dict(self):
        return {a: int(k) for a, k in self.map.items()}

    def apply_dict(self, data):
        raw = sec.safe_dict(data)
        self.map = dict(DEFAULTS)
        for action in ACTION_KEYS:
            code = sec.safe_int(raw.get(action), 0, 2 ** 31, 0)
            if code and code not in RESERVED:
                self.map[action] = code
        # A duplicate binding would make one action unreachable.
        seen = {}
        for action in ACTION_KEYS:
            code = self.map[action]
            if code in seen:
                self.map[action] = DEFAULTS[action]
            else:
                seen[code] = action
        return self
