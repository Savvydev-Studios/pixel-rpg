"""The hidden debug menu.

Found rather than given: type the old up-up-down-down sequence on the title
screen. Ten specific keys in order cannot be hit by accident, which is the
only property the entrance actually needs.

**This is hidden, not secured, and the difference matters.** You cannot lock a
single-player game against the person whose machine it runs on - the save is
theirs, the memory is theirs, `SECURITY.md` already calls the checksums
tamper-evident rather than tamper-proof. A secret sequence stops somebody
wandering in; it stops nobody who wants in. So no effort is spent pretending
otherwise.

What *is* worth protecting is the profile that outlives the save. The codex,
the endings and the records persist across runs and gate two of the four
endings - that is the replay spine, and handing it out from a debug menu would
spoil the story permanently, for every future run, with no way back. So:

    using any power here marks the run, and a marked run can no longer
    write to the meta profile at all.

The mark lives on the player (`debug_used`), is saved with the run, and is
never cleared. `MetaProfile.tainted` enforces it in one place so no future
caller can forget. Break whatever you like - the toys are yours, the
progression stays honest.
"""
from __future__ import annotations

import pygame

from .ui import (ACCENT, BAD, DIM, FAINT, GOOD, INK, draw_text, panel,
                 row_height, row_text)

# The entrance. Long enough to be deliberate, famous enough to be findable.
SEQUENCE = (pygame.K_UP, pygame.K_UP, pygame.K_DOWN, pygame.K_DOWN,
            pygame.K_LEFT, pygame.K_RIGHT, pygame.K_LEFT, pygame.K_RIGHT,
            pygame.K_b, pygame.K_a)


class SequenceWatcher:
    """Tracks progress through the entrance sequence.

    A wrong key does not simply reset to zero: it re-checks whether that key
    opens the sequence, so a stray Up begins a fresh attempt rather than
    ending one. Without that, mashing the first key could never get you in.
    """

    def __init__(self, sequence=SEQUENCE):
        self.sequence = tuple(sequence)
        self.progress = 0

    def feed(self, key):
        """True when the final key of the sequence just landed."""
        if key == self.sequence[self.progress]:
            self.progress += 1
        else:
            self.progress = 1 if key == self.sequence[0] else 0
        if self.progress >= len(self.sequence):
            self.progress = 0
            return True
        return False


# -- the powers ---------------------------------------------------------------
#
# Each is (label, note, function, taints). **Every one taints since
# 2026-09-01**, on his instruction; the field is kept because the screen
# reads it and because a future safe action is a change of data rather
# than a change of code. The old comment said `taints` is False only for things
# that change nothing about the run's difficulty or progress; everything that
# hands out power marks the run.

# **One multiplier for every number on the screen** (2026-09-01), stepped
# with Left and Right. One control rather than a field per row: the rows
# already say what they give, so a x100 in the header answers *"how many
# times"* for all of them at once and needs no cursor of its own.
#
# **And since 2026-09-05 (0ah, s58) it is typed rather than cycled.** These
# are presets now, not the whole vocabulary. Savvydev:
#
# > "also allow the numbers in the debug menu to be changed, so you can
# > give yourself however much you want all at once without having to spam
# > the same button 100 times"
#
# The multiplier above was written for the *first* half of that complaint
# (s46) and left the second: six presets and a ceiling of x1000 is still a
# menu of amounts somebody else chose. Type a number instead.
SCALES = (1, 5, 10, 50, 100, 1000)

# The chips the mouse can reach, and what Left/Right step through. `1` is
# first so that stepping left from it wraps to the largest.
PRESETS = (1, 10, 100, 1000, 10000, 100000, 1000000)

# **A ceiling, because this writes into the run.** Gold, XP and materials
# are `safe_int`-clamped on save (s8), so an amount past what a save can
# hold is a number the next load silently disagrees with - the debug menu
# is allowed to be generous and not allowed to lie (`_points` already says
# so). Seven digits is past anything the game can spend.
MAX_AMOUNT = 9_999_999


def scale(game):
    """The multiplier every row reads. Never below 1."""
    return max(1, min(int(getattr(game.ui, "dev_amount", 1) or 1),
                      MAX_AMOUNT))


def set_amount(game, value):
    game.ui.dev_amount = max(1, min(int(value), MAX_AMOUNT))
    return scale(game)


def cycle_scale(game, step):
    """Left/Right: to the next preset above or below where we are now.

    Stepping by *preset* rather than by index is what lets typing and
    stepping share one field. The old version stored an index, so a typed
    number had nowhere to live.
    """
    current = scale(game)
    if step > 0:
        nxt = next((p for p in PRESETS if p > current), PRESETS[0])
    else:
        nxt = next((p for p in reversed(PRESETS) if p < current), PRESETS[-1])
    game.ui.dev_typing = False
    return set_amount(game, nxt)


def type_digit(game, digit):
    """Append a digit, the way a number field behaves.

    The first digit typed **replaces** rather than appends, so arriving at
    a x1000 preset and typing 25 gives 25 and not 100025. `dev_typing` is
    what remembers whether we are mid-number; it is cleared by stepping,
    by opening the menu, and by running an action.
    """
    if not game.ui.dev_typing:
        game.ui.dev_amount = 0
        game.ui.dev_typing = True
    grown = int(game.ui.dev_amount or 0) * 10 + int(digit)
    if grown > MAX_AMOUNT:
        return scale(game)
    game.ui.dev_amount = grown
    return max(1, grown)


def backspace(game):
    game.ui.dev_typing = True
    game.ui.dev_amount = int(game.ui.dev_amount or 0) // 10
    return max(1, game.ui.dev_amount)


def typed_text(game):
    """What the field shows. Mid-typing it may legitimately read 0."""
    if game.ui.dev_typing:
        return str(int(game.ui.dev_amount or 0))
    return str(scale(game))


def _heal(game):
    game.player.hp = game.player.max_hp()
    game.player.mp = game.player.max_mp()
    game.player.clear_statuses()
    return "Restored."


def _gold(game):
    amount = 1000 * scale(game)
    game.player.gold += amount
    return "+%d gold." % amount


def _xp(game):
    amount = 500 * scale(game)
    levels, _skills = game.player.gain_xp(amount)
    return "+%d XP%s." % (amount,
                          " (%d levels)" % levels if levels else "")


def _points(game):
    # Clamped by `spendable_points`, which is his 540 ceiling - the debug
    # menu is allowed to be generous and not allowed to lie.
    amount = 5 * scale(game)
    before = game.player.points
    game.player.points = min(before + amount,
                             game.player.spendable_points())
    return "+%d attribute points." % (game.player.points - before)


def _materials(game):
    from .items import SALVAGE_NAME
    amount = 50 * scale(game)
    game.player.materials += amount
    return "+%d %s." % (amount, SALVAGE_NAME)


def _item(game):
    from .items import roll_item
    item = roll_item(max(1, game.player.level + 4), luck=0.9)
    if game.player.add_item(item):
        return "Granted %s." % item.name
    return "Bag is full."


def _fight(game):
    """Spawn a fight of the size the amount says.

    **It used to count up on its own** - one more monster per press, so
    the only way to a pack of four was to fight your way through one, two
    and three first. Savvydev, 2026-09-01:

    > "the pick a fight in the debug menu doesnt really work, you click it
    > once and it puts u in battle with one enemy immediately, when u go
    > back and click it again it puts u in with 2 enemies and stuff"

    The multiplier is already on screen and already means "how much", so
    it means how many here. Five is the most a fight can hold (s4).
    """
    from .entities import make_enemy
    pack = max(1, min(5, scale(game)))
    game.ui.dev_pack = pack
    level = max(1, game.player.level)
    game.start_battle([make_enemy(level, "meadow", game.settings.mods)
                       for _ in range(pack)])
    return "Spawned %d." % pack


def _home(game):
    game.player.x = game.world.spawn[0] * game.tile + game.tile / 2
    game.player.y = game.world.spawn[1] * game.tile + game.tile / 2
    game.camera.follow(game.player.x, game.player.y, 1.0)
    return "Back to the village."


def _portals(game):
    opened = 0
    for portal in game.world.portals:
        if not portal.unlocked:
            portal.unlocked = True
            opened += 1
    return "Opened %d arena%s." % (opened, "" if opened == 1 else "s")


def _invuln(game):
    game.player.dev_invulnerable = not getattr(
        game.player, "dev_invulnerable", False)
    return "Invulnerable: %s." % ("on" if game.player.dev_invulnerable
                                  else "off")


def _time(game):
    from .game import DAY_LENGTH
    game.world_time = (game.world_time + DAY_LENGTH / 4) % DAY_LENGTH
    return "Time moved on."


ACTIONS = [
    ("Restore health and MP", "Full HP, full MP, statuses cleared",
     _heal, True),
    ("Give 1000 gold", "Straight into your purse", _gold, True),
    ("Give 500 XP", "May level you up", _xp, True),
    ("Give 5 attribute points", "Spend them on the hero sheet",
     _points, True),
    ("Give 50 Ember Steel", "Refining material", _materials, True),
    ("Roll a good item", "Four levels above you, high rarity", _item, True),
    ("Pick a fight", "As many as the amount says, up to five",
     _fight, True),
    ("Teleport to the village", "Straight back to the spawn", _home, True),
    ("Open every arena", "Unlocks all six portals", _portals, True),
    ("Toggle invulnerability", "Nothing can damage you", _invuln, True),
    # **Nothing here is free any more** (2026-09-01). This was the one
    # exception, and he asked for it to go: *"everything inside should
    # mark the run"*. The run is marked by opening the menu at all now, so
    # the flag on each action only decides whether the *action* would mark
    # a run that somehow was not - which is every one of them.
    ("Shift the time of day", "Cosmetic, but nothing here is free",
     _time, True),
]


def mark(game):
    """Mark the run, and say so once.

    Setting `meta.tainted` here as well as at load keeps the guard live for
    the rest of the session without waiting for a reload.
    """
    if game.player is None or game.player.debug_used:
        return
    game.player.debug_used = True
    game.meta.tainted = True
    game.log.event("debug_used", level=game.player.level)
    # Short on purpose: a long toast runs off the right edge, and the menu
    # header already spells out what being marked costs you.
    game.toast("This run is now marked.", BAD, 6.0)


def run_action(game, index):
    if not (0 <= index < len(ACTIONS)):
        return
    label, _note, fn, taints = ACTIONS[index]
    if taints:
        mark(game)
    try:
        message = fn(game)
    except Exception as exc:                          # noqa: BLE001
        # A debug tool must never be the thing that kills the run.
        game.log.error("debug action %r failed: %r", label, exc)
        game.toast("That did not work: %s" % type(exc).__name__, BAD, 4.0)
        return
    # The number has been spent; the next digit starts a fresh one rather
    # than appending to what was just used.
    game.ui.dev_typing = False
    game.log.event("debug_action", action=label.replace(" ", "_"))
    if message:
        game.toast(message, ACCENT, 3.0)
    game.sfx("select")


# -- the screen ---------------------------------------------------------------

def state_typing(game):
    return bool(getattr(game.ui, "dev_typing", False))


def chip_label(value):
    """x1000 is wider than the chip; x1k is not."""
    if value >= 1000000:
        return "x%dM" % (value // 1000000)
    if value >= 1000:
        return "x%dk" % (value // 1000)
    return "x%d" % value


def amount_chips(box):
    """The clickable presets, left to right, as (value, rect).

    **Rule 8b**: typing is a keyboard affordance the way the hero's name
    is, so the mouse gets the presets as real buttons rather than being
    told to use the keyboard. Every amount the six-preset version could
    reach is still one click away, and three more besides.
    """
    chips = []
    gap = 4
    width = min(56, (box.w - 52 - gap * (len(PRESETS) - 1)) // len(PRESETS))
    total = width * len(PRESETS) + gap * (len(PRESETS) - 1)
    x = box.centerx - total // 2
    for value in PRESETS:
        chips.append((value, pygame.Rect(x, box.y + 136, width, 22)))
        x += width + gap
    return chips


def draw_amount_chips(game, box):
    from .menus import row_highlight
    surface, assets = game.screen, game.assets
    current = scale(game)
    for value, rect in amount_chips(box):
        row_highlight(surface, rect, value == current, game=game,
                      kind="dev_amount", index=value)
        # Centred rather than `row_text`'s left pad: a chip is 56px wide
        # and a left-aligned label in it reads as a misplaced list row.
        draw_text(surface, assets, chip_label(value), rect.center, "tiny",
                  INK if value == current else DIM, center=True)


def draw_devmenu(game):
    from .menus import overlay, row_highlight
    overlay(game, 224)
    surface, assets = game.screen, game.assets
    width, height = surface.get_size()

    rows_h = len(ACTIONS) * 34
    box_h = min(height - 40, 196 + rows_h + 74)
    box_w = min(500, width - 24)
    box = pygame.Rect((width - box_w) // 2, (height - box_h) // 2, box_w,
                      box_h)
    panel(surface, box, edge=BAD)

    draw_text(surface, assets, "Debug", (box.centerx, box.y + 30), "big",
              BAD, center=True)
    # Opening the menu marks the run (2026-09-01), so by the time anybody
    # reads this line it is almost always already true. The `else` is the
    # one frame before `open_devmenu` gets to it, and a run that somehow
    # reached this screen without a player.
    marked = bool(game.player and game.player.debug_used)
    draw_text(surface, assets,
              "This run is marked. Opening this did it." if marked
              else "Opening this marks the run",
              (box.centerx, box.y + 60), "tiny",
              BAD if marked else ACCENT, center=True)
    draw_text(surface, assets,
              "No codex, endings or records from a marked run",
              (box.centerx, box.y + 78), "tiny", FAINT, center=True)
    # **The amount, as a field you can type into** (0ah, s58).
    draw_text(surface, assets,
              "Amount  x%s%s" % (typed_text(game),
                                 "_" if state_typing(game) else ""),
              (box.centerx, box.y + 102), "small", ACCENT, center=True)
    draw_text(surface, assets,
              # Short enough for a 390px window (s29): the long version
              # spilled its panel and ran off the edge there, and the
              # chips below already say what Left and Right do.
              "Type a number. Backspace clears.",
              (box.centerx, box.y + 122), "tiny", FAINT, center=True)
    draw_amount_chips(game, box)

    state = game.ui
    # Eleven rows at 34 is more than a landscape phone has after the header,
    # so they squeeze and then scroll - the same treatment settings needed.
    from .ui import list_view, scroll_note
    dev_top = box.y + 168
    start, visible, pitch = list_view(dev_top, box.bottom - 40, len(ACTIONS),
                                      state.dev_index, natural=34,
                                      minimum=22)
    scroll_note(surface, assets,
                pygame.Rect(box.x + 26, dev_top, box.w - 52, visible * pitch),
                start, visible, len(ACTIONS))
    for offset in range(visible):
        i = start + offset
        label, _note, _fn, taints = ACTIONS[i]
        row = pygame.Rect(box.x + 26, dev_top + offset * pitch, box.w - 52,
                          row_height(assets, "small", pitch, pad=4))
        row_highlight(surface, row, i == state.dev_index,
                      game=game, kind="dev", index=i)
        # The label carries the number, so it has to carry the multiplier
        # too - a row that says 1000 while handing over 100000 is worse
        # than no number at all.
        step = scale(game)
        shown = label
        if step > 1:
            for base in ("1000", "500", "50", "5"):
                if base in label:
                    shown = label.replace(base, str(int(base) * step), 1)
                    break
        row_text(surface, assets, shown, row, "small",
                 INK if i == state.dev_index else DIM, pad=14)
        if not taints:
            # Nothing carries this today (2026-09-01): every action marks
            # the run, and so does opening the menu. Kept so that adding a
            # genuinely cosmetic tool later is a line of data.
            row_text(surface, assets, "safe", row, "tiny", GOOD,
                     pad=14, right=True)

    # The note belongs to whichever row is highlighted, on its own line. Put
    # it beside the label instead and a long note walks straight over it -
    # the layout checker tolerates that much overlap as kerning, so nothing
    # catches it but your eyes.
    index = min(state.dev_index, len(ACTIONS) - 1)
    draw_text(surface, assets, ACTIONS[index][1],
              (box.centerx, box.bottom - 48), "tiny", ACCENT, center=True)
    draw_text(surface, assets, "Esc to close",
              (box.centerx, box.bottom - 26), "tiny", FAINT, center=True)


# Digits, both rows, so a numpad works (0ah). `K_KP0`..`K_KP9` are a
# separate block of key codes and a debug menu that ignored them would be
# the kind of half-done that rule 8b is about, one input device over.
DIGIT_KEYS = {}
for _d in range(10):
    DIGIT_KEYS[getattr(pygame, "K_%d" % _d)] = _d
    DIGIT_KEYS[getattr(pygame, "K_KP%d" % _d)] = _d


def handle_devmenu_event(game, event):
    state = game.ui
    # **Typing beats every other binding here** (0ah, s58), which is why it
    # is first: `a` and `d` are also the amount presets below, and a digit
    # must never be read as a cursor move.
    if event.key in DIGIT_KEYS:
        type_digit(game, DIGIT_KEYS[event.key])
        game.sfx("move")
        return
    if event.key == pygame.K_BACKSPACE:
        backspace(game)
        game.sfx("move")
        return
    if event.key in (pygame.K_a, pygame.K_LEFT, pygame.K_d, pygame.K_RIGHT):
        step = -1 if event.key in (pygame.K_a, pygame.K_LEFT) else 1
        cycle_scale(game, step)
        game.sfx("move")
        return
    if event.key in (pygame.K_w, pygame.K_UP):
        state.dev_index = (state.dev_index - 1) % len(ACTIONS)
        game.sfx("move")
    elif event.key in (pygame.K_s, pygame.K_DOWN):
        state.dev_index = (state.dev_index + 1) % len(ACTIONS)
        game.sfx("move")
    elif event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE):
        run_action(game, state.dev_index)
    elif event.key == pygame.K_ESCAPE:
        game.close_devmenu()
        game.sfx("back")
