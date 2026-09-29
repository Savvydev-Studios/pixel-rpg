"""All non-battle screens: title, slots, bag, shop, smith, hero, quests, pause.

Each screen is a `draw_*` function plus a branch in `handle_event`, with the
per-screen cursor living on the shared `MenuState`. Keeping the screens as
functions over game state avoids a class hierarchy that would earn nothing.
"""
from __future__ import annotations

import math

import pygame

from . import security as sec
from . import story
from .config import DIFFICULTIES
from .entities import ATTRIBUTE_HELP, ATTRIBUTE_LABEL, ATTRIBUTES
from . import __version__
from .items import (CONSUMABLES, RARITY_ORDER, SALVAGE_NAME, SHORT,
                    apply_upgrade, roll_item, salvage_yield, sell_price,
                    shop_stock, upgrade_cost, upgrade_materials)
from .quests import (ACHIEVEMENTS, ACHIEVEMENTS_BY_KEY, QUESTS_BY_KEY)
from .save import format_playtime
from .ui import (ACCENT, BAD, DIM, FAINT, GOOD, INK, PANEL_EDGE, Hotspots,
                 skill_card,
                 ScrollList, bar, draw_text, fit_stats, highlight, hp_color,
                 ellipsize, is_narrow, item_card, key_hint, list_view,
                 panel, row_height, row_text, scroll_note, scrollbar,
                 window_start,
                 status_chips)
from .util import article, clamp, commafy, pulse, rng, wrap_text

TABS = ("All", "Weapon", "Armor", "Accessory", "Items")

# Screens where more than one row can be switched on at once. On these the
# filled box means "this is on", not "the cursor is here".
# Screens where several rows can be switched on at once, so "chosen" and
# "where the cursor is" have to look different. The trade table is the
# second one: an item on the table stays lit while you move past it.
MULTI_SELECT_KINDS = {"modifier", "trade", "loadout"}


class MenuState:
    """Cursor and mode state for every menu screen."""

    def __init__(self):
        self.inv = ScrollList(11)
        self.inv_tab = 0
        self.shop = ScrollList(9)
        self.shop_mode = "buy"
        self.shop_items = []
        self.shop_keys = []
        self.smith = ScrollList(6)
        self.char_index = 0
        self.quest = ScrollList(9)
        self.quest_tab = 0
        self.pause_index = 0
        self.settings_index = 0
        self.slot = ScrollList(4)   # visible count is set by the screen
        self.feats = ScrollList(8)  # the global achievement cabinet
        self.story = ScrollList(9)  # the codex's story record
        self.lobby_index = 0        # co-op lobby cursor
        self.mode_index = 0         # which gamemode (0ag, s59)
        self.lobby_mode = ""        # "host" or "join"
        self.lobby_notice = ""      # why a code was refused
        self.door_answered = False  # the Door asks once per run
        self.door_choice = ""       # what was said there
        self.dev_index = 0          # hidden debug menu cursor
        self.dev_pack = 0           # last pack size it spawned
        # **The amount, typed** (0ah, s58). `dev_scale` was an index into
        # six presets; this is the number itself, so any amount can be
        # asked for rather than only the six that were anticipated.
        self.dev_amount = 1
        self.dev_typing = False     # digits have been typed since it opened
        self.slot_mode = "new"
        # Added with classes, the codex and the endings.
        self.class_index = 0
        self.modifier_index = 0
        #: Where the arena picker's cursor is (item 0br). The two
        #: buttons live past the last arena, so it counts one
        #: sequence rather than a grid plus a footer.
        self.arena_index = 0
        # A cleared wave's offer (items 0bs/0bt): which card the cursor
        # is on, and which one has been asked about.
        self.spoil_index = 0
        self.spoil_detail = None
        #: Which row of the arena picker is at the top of the page.
        #: `None` until it is first drawn - `window_start` reads that
        #: as *nothing remembered* and places the page once, which is
        #: the difference between a list that settles and one that
        #: slides on every keypress (item 0bn).
        self.arena_row = None
        self.codex = ScrollList(10)
        self.codex_tab = 0
        self.ending_index = 0
        self.ending_scroll = 0
        # Which skill row has its card open on the hero sheet, or None.
        self.skill_detail = None
        # Which equipped slot is being inspected, on either screen.
        self.gear_detail = None
        # The "your bag is full" question, while one is being asked, and
        # its cursor - which lives here rather than inside the question so
        # that `point_cursor` can move it like every other list (s48).
        self.fullbag = None
        self.fullbag_index = 0
        # The new-skill question: which of the four it replaces (s53).
        self.newskill = None
        self.newskill_index = 0
        self.records_scroll = 0
        self.creator_index = 0
        self.quest_readonly = False
        self.travel_index = 0
        # Which half of the hero sheet the keys drive, and where the
        # cursor is in the ability list.
        self.char_pane = "stats"
        self.skill_index = 0
        self.quest_notice = None
        # Set by `frame` each time it draws; read by `tab_row`.
        self.header_bottom = 0
        self.guide_index = 0
        # How far down the current guide page you have read. Reset on every
        # page turn, because arriving at page five halfway down is not
        # reading on, it is landing in the middle of a sentence.
        self.guide_scroll = 0
        self.smith_mode = "refine"
        self.keybind_index = 0
        self.rebinding = None       # action awaiting a keypress
        self.keybind_notice = ""
        self.hot = Hotspots()

    def reset(self):
        for lst in (self.inv, self.shop, self.smith, self.quest):
            lst.index = lst.offset = 0
        self.quest_notice = None
        self.char_index = 0
        self.pause_index = 0

    def refresh_shop(self, player, world):
        beaten = sum(1 for p in world.portals if p.defeated)
        # Brittle Steel is "all gear", and the trader's shelves are gear.
        self.shop_keys, self.shop_items = shop_stock(
            beaten, max(1, player.level),
            downgrade=1 if player.modifiers.has("downgrade") else 0)
        self.shop.index = self.shop.offset = 0
        self.shop_mode = "buy"


# --------------------------------------------------------------- helpers ---

def overlay(game, alpha=176):
    layer = pygame.Surface(game.screen.get_size(), pygame.SRCALPHA)
    layer.fill((6, 5, 10, alpha))
    game.screen.blit(layer, (0, 0))


def frame_rect(game, inset=64):
    """The inner rect a framed screen draws into, without drawing it.

    **Split out of `frame` rather than copied** (item 0br): the arena
    picker's grid, its hotspots and the tests that measure them all need
    the same rectangle, and a second copy of this arithmetic is a second
    copy that drifts - which is how a test can pass against a five-column
    grid the screen is drawing in four.
    """
    width, height = game.screen.get_size()
    inset = int(clamp(min(width, height) // 12, 12, inset))
    return pygame.Rect(inset, inset, width - inset * 2, height - inset * 2)


def frame(game, title, subtitle="", inset=64):
    """Standard full-screen menu chrome. Returns the usable inner rect.

    The inset shrinks on a small window - sixty-four pixels a side is nine
    per cent of a desktop and a third of a phone in portrait - and the
    subtitle wraps rather than running out through the edge of its own
    panel. Both were fixed points, and on a narrow window the subtitle was
    the single most common layout failure in the game: it appears on every
    framed screen, so one line of prose broke twenty of them at once.
    """
    rect = frame_rect(game, inset)
    panel(game.screen, rect, alpha=244)
    # **The title was the half of this that never got the treatment.** The
    # subtitle was taught to wrap and became the fix that stopped twenty
    # screens failing at once; the title sat at "big" with no guard, and
    # simply had no string long enough to expose it until "Bring a finished
    # hero?" arrived. A title reads badly ellipsized, so it steps down a
    # size first and is only cut if even that will not fit.
    title_font = "big"
    if game.assets.font(title_font).size(str(title))[0] > rect.w - 36:
        title_font = "mid"
    draw_text(game.screen, game.assets,
              ellipsize(game.assets.font(title_font), title, rect.w - 36),
              (rect.centerx, rect.y + 30), title_font, INK, center=True)
    # **Where the header actually ends, for whatever sits under it.** The
    # subtitle wraps to a second line on a narrow window and the tab row
    # below it is placed from a constant that assumed one - so on a phone
    # the subtitle ran straight through the tabs on the quest log and all
    # five codex pages (s53). `tab_row` reads this rather than guessing.
    lines = []
    if subtitle:
        lines = wrap_text(subtitle, game.assets.font("small"),
                          rect.w - 36)[:2]
        for i, line in enumerate(lines):
            draw_text(game.screen, game.assets, line,
                      (rect.centerx, rect.y + 62 + i * 17), "small", DIM,
                      center=True)
    game.ui.header_bottom = rect.y + (54 + 17 * len(lines) + 8 if lines
                                      else 48)
    return rect


def tab_row(surface, assets, game, rect, labels, active, kind, top=84):
    """A row of filter tabs across the top of a framed screen.

    Four screens draw one of these and every one of them had the same two
    bugs: the gaps between the tabs were not counted against the width, so
    the last tab hung off the frame; and the label was not cut to the tab,
    so on a narrow window every one of them overlapped its neighbour.

    Fixed here once. `active` is an index or a predicate.
    """
    count = max(1, len(labels))
    gaps = 8 * (count - 1)
    width = int(clamp((rect.w - 60 - gaps) // count, 40, 100))
    font = assets.font("small")
    # Under the header wherever the header ended, never under a constant.
    top = max(top, getattr(game.ui, "header_bottom", 0) - rect.y + 6)
    for i, label in enumerate(labels):
        tab = pygame.Rect(rect.x + 30 + i * (width + 8), rect.y + top,
                          width, 28)
        chosen = active(i, label) if callable(active) else i == active
        row_highlight(surface, tab, chosen, PANEL_EDGE, game=game,
                      kind=kind, index=i)
        draw_text(surface, assets, ellipsize(font, label, tab.w - 10),
                  tab.center, "small", INK if chosen else FAINT, center=True)


def filtered_inventory(player, tab):
    if tab == 0:
        return list(player.inventory)
    slot = ("weapon", "armor", "accessory")[tab - 1] if tab <= 3 else None
    if slot:
        return [i for i in player.inventory if i.slot == slot]
    return []


def consumable_rows(player):
    return [(key, CONSUMABLES[key], player.consumable_count(key))
            for key in CONSUMABLES if player.consumable_count(key) > 0]


def row_highlight(surface, rect, selected, color=ACCENT, game=None,
                  kind=None, index=0, chosen=False):
    """Draw a row's state and, when told what it is, make it clickable.

    Three distinct states, because conflating them is what made menus read as
    "stuck on whatever I clicked last":

      chosen   a filled box - this option is switched ON. Only meaningful on
               screens where several things can be on at once.
      cursor   a bright outline - where Enter would act.
      hover    a dim outline - merely where the pointer is.

    On single-choice screens nothing is ever `chosen`, so the cursor is the
    filled box and there is no ambiguity.
    """
    multi = chosen is not None and kind in MULTI_SELECT_KINDS
    if chosen:
        highlight(surface, rect, color, fill_alpha=58, border=2)
    elif selected and not multi:
        highlight(surface, rect, color)
    elif selected:
        # Thicker than the hover outline, so the keyboard cursor and the
        # pointer are still tellable apart on a multi-select screen.
        highlight(surface, rect, color, fill_alpha=0, border=3)

    if game is not None and kind is not None:
        game.ui.hot.add(rect, kind, index)
        if game.hover == (kind, index) and not chosen:
            faint = (color[0] * 3 // 4, color[1] * 3 // 4, color[2] * 3 // 4)
            highlight(surface, rect, faint if selected else color,
                      fill_alpha=0, border=2)
    return rect


# ----------------------------------------------------------------- title ---

def draw_splash(game):
    """The opening card: a name on black, which lifts to reveal the title.

    Drawn over the real title backdrop rather than over nothing, so when the
    black lifts the title is already there behind it and the words are the
    last thing to leave. Savvydev asked for it in that order on 2026-08-24.
    """
    surface, assets = game.screen, game.assets
    draw_title_backdrop(game)

    black, words = game.splash_alphas()
    width, height = surface.get_size()
    if black > 0:
        cover = pygame.Surface((width, height))
        cover.fill((0, 0, 0))
        cover.set_alpha(black)
        surface.blit(cover, (0, 0))
    if words > 0:
        # Built on its own layer so the whole line fades together. Drawing
        # it with a faded colour instead would fade the glyphs against the
        # black and leave the shadow behind.
        line = assets.text("made by saveer", "big", INK)
        layer = pygame.Surface(line.get_size(), pygame.SRCALPHA)
        layer.blit(line, (0, 0))
        layer.set_alpha(words)
        surface.blit(layer, (width // 2 - line.get_width() // 2,
                             height // 2 - line.get_height() // 2))


def draw_title_backdrop(game):
    """Animated parallax bands so the menu is not a flat colour."""
    surface = game.screen
    width, height = surface.get_size()
    surface.blit(game.assets.gradient((width, height), (26, 22, 44),
                                      (10, 10, 18)), (0, 0))
    t = pygame.time.get_ticks() / 1000.0
    period = 300                       # the peaks repeat every 300 pixels
    for i in range(4):
        depth = 1 + i
        band = pygame.Surface((width, 90), pygame.SRCALPHA)
        color = (30 + i * 12, 26 + i * 10, 50 + i * 12, 120)
        # Wrapping on the pattern period, not the screen width, is what makes
        # this loop without a visible jump back to the start.
        offset = (t * 12 * depth) % period
        for k in range(-1, width // period + 3):
            x = offset + k * period
            pygame.draw.polygon(band, color,
                                [(x - period // 2, 90), (x, 10),
                                 (x + period // 2, 90)])
        surface.blit(band, (0, height - 210 + i * 42))
    surface.blit(game.assets.vignette((width, height)), (0, 0))


def draw_title(game):
    surface = game.screen
    width, height = surface.get_size()
    glow = pulse(pygame.time.get_ticks() / 1000.0, 0.25)
    # A quarter of the screen is a generous place for a title on a desktop
    # and most of the room on a landscape phone, so it is capped.
    title_y = min(height // 4, 96)
    draw_text(surface, game.assets, "PIXEL RPG",
              (width // 2, title_y), "huge", (240, 226, 255), center=True)
    draw_text(surface, game.assets, "six wardens  -  one road",
              (width // 2, title_y + 46), "small",
              (150 + int(glow * 60), 150, 190), center=True)

    options = game.title_options()
    # Fit the list between the title block and the footer rather than
    # assuming a fixed row height, or adding an option pushes Quit into the
    # save-path line underneath it.
    # The footer needs about seventy pixels and the title block a quarter
    # of the screen - on a 390-tall window those two claims overlap, so the
    # title block gives way first.
    first = min(height // 4, 96) + 84
    last = height - 74
    # Seven options do not fit a landscape phone even squeezed, so the
    # title menu scrolls like every other list. Centred while there is room.
    start, visible, spacing = list_view(first, last, len(options),
                                        game.title_index, natural=46,
                                        minimum=24)
    top = first
    if visible >= len(options):
        top = max(first, min((height + first - spacing * len(options)) // 2,
                             last - spacing * len(options)))
    for offset in range(visible):
        i = start + offset
        label, _ = options[i]
        selected = i == game.title_index
        rect = pygame.Rect(width // 2 - 150, top + offset * spacing, 300,
                           row_height(game.assets, "mid", spacing))
        row_highlight(surface, rect, selected, game=game, kind="title",
                      index=i)
        draw_text(surface, game.assets, label, rect.center, "mid",
                  ACCENT if selected else DIM, center=True)
    scroll_note(surface, game.assets,
                pygame.Rect(width // 2 - 150, top, 300, visible * spacing),
                start, visible, len(options))
    # **Read, not typed** (2026-09-05, rule 31.1e). This was the string
    # "v3.0.0" and `pixel_rpg.__version__` was another one, so the game
    # had two versions and could disagree with itself.
    draw_text(surface, game.assets, "v%s" % __version__,
              (width // 2, height - 46),
              "tiny", FAINT, center=True)
    where = str(game.settings.save_dir)
    font = game.assets.font("tiny")
    # Trimmed from the front: the tail of a save path is the part that
    # tells you which folder it is.
    while where and font.size("saves: ..." + where)[0] > width - 32:
        where = where[1:]
    if where != str(game.settings.save_dir):
        where = "..." + where
    draw_text(surface, game.assets, "saves: %s" % where,
              (width // 2, height - 28), "tiny", FAINT, center=True)


def scroll_hint(surface, assets, rect, state, count):
    """Say how much of a list you are looking at.

    Only drawn when some of it is off screen. Without this a short window
    silently hides slots, and "I only have four saves" is a reasonable thing
    to conclude from a screen that shows four.
    """
    if count <= state.visible:
        return
    # **The bar too** (item 0bn). This one is inset rather than hung off
    # the edge, because the screens that use it draw their rows inside a
    # frame with its own border a few pixels further out.
    scrollbar(surface, pygame.Rect(rect.x, rect.y + 70, rect.w - 34,
                                   max(24, rect.h - 130)),
              state.offset, state.visible, count)
    first = state.offset + 1
    last = min(state.offset + state.visible, count)
    draw_text(surface, assets, "%d-%d of %d   scroll for more"
              % (first, last, count),
              (rect.centerx, rect.bottom - 38), "tiny", FAINT, center=True)


def draw_slots(game):
    surface = game.screen
    # The header names the *question*, because the same picker now
    # answers three of them and the playtest could not tell which one
    # it was looking at (s40).
    titles = {"new": "Choose a slot", "load": "Choose a slot",
              "coop": "Which world will you host?",
              "veteran": "Bring a finished hero?"}
    hints = {"new": "start here", "load": "load",
             "coop": "host this one"}
    mode = game.ui.slot_mode
    if mode == "veteran":
        # **Esc is an answer here, not a way out** - you are already in
        # somebody's room and you still need a hero, so declining is
        # choosing to start fresh. Delete is left off the line for the same
        # reason: erasing one of your own runs is nothing to do with this
        # question, and the row under the cursor is a run you finished.
        rect = frame(game, titles[mode],
                     "Enter to bring them   Esc to start fresh here")
    else:
        rect = frame(game, titles.get(mode, "Choose a slot"),
                     "Enter to %s   Delete to erase   Esc to go back"
                     % hints.get(mode, "load"))
    slots = game.saves.describe_all()
    # Rows keep their readable height; the list scrolls instead of shrinking,
    # so adding slots never squeezes the text into itself.
    top = rect.y + 96
    state = game.ui.slot
    state.visible = max(2, ((rect.bottom - 56) - top) // 82)
    state.clamp_to(len(slots))
    for i, info in state.window(slots):
        row = pygame.Rect(rect.x + 40, top + (i - state.offset) * 82,
                          rect.w - 80, 70)
        row_highlight(surface, row, i == state.index,
                      game=game, kind="slot", index=i)
        label = "Slot %d" % info["slot"]
        draw_text(surface, game.assets, label, (row.x + 20, row.y + 12),
                  "body", INK)
        # **A marked run says so before you load it** (2026-09-01):
        # *"add a distinguishable way to see what runs are flagged when
        # choosing which run to load up, something like a red flag to the
        # right of "slot 1" for example."*
        #
        # Beside the number rather than on the second line, because the
        # second line is already six facts wide and this one is not about
        # the hero - it is about what the run can never do again.
        # **FALLEN outranks FLAGGED** (s54). Both sit beside the slot
        # number and both are about what the run can never do again, but a
        # fallen run cannot be loaded at all - so if a hero managed to be
        # both, the one that stops you pressing Enter is the one to show.
        stamp_text = ("FALLEN" if info.get("fallen")
                      else "FLAGGED" if info.get("marked") else "")
        if stamp_text:
            flag_x = row.x + 26 + game.assets.font("body").size(label)[0]
            draw_text(surface, game.assets, stamp_text, (flag_x, row.y + 16),
                      "tiny", BAD)
        if info.get("corrupt"):
            draw_text(surface, game.assets,
                      "unreadable: %s" % info.get("error", ""),
                      (row.x + 20, row.y + 38), "small", BAD)
        elif info.get("empty"):
            draw_text(surface, game.assets, "empty",
                      (row.x + 20, row.y + 38), "small", FAINT)
        else:
            # **A row has two columns and a phone has room for one** (s29).
            # Everything below is measured against what is actually left
            # after the other column, because a slot row carries six
            # variable-length facts - a name typed by the player, a gold
            # figure with no ceiling worth trusting, and a timestamp - and
            # any of them can be the one that overflows.
            narrow = is_narrow(rect)
            stamp = "%s   %s" % (format_playtime(info["playtime"]),
                                 info["updated"])
            stamp_w = 0 if narrow else (
                game.assets.font("tiny").size(stamp)[0] + 24)
            if mode == "veteran":
                # The one fact the question turns on, said on the row that
                # answers it rather than left for the player to remember.
                #
                # **On the first line, not a third one.** A row is 70 tall
                # and the second line already sits at 38; a third at 58 is
                # inside the border, which the layout checker forgives as
                # kerning and a screenshot does not.
                done = info.get("beaten")
                draw_text(surface, game.assets,
                          "finished" if done else "not finished yet",
                          (row.right - 20, row.y + 22), "tiny",
                          GOOD if done else FAINT, right=True)
            draw_text(surface, game.assets,
                      ellipsize(game.assets.font("small"),
                                "%s   Lv %d   %d gold   %d/6 wardens"
                                % (info["name"], info["level"], info["gold"],
                                   info["bosses"]),
                                row.w - 40 - stamp_w),
                      (row.x + 20, row.y + 38), "small", DIM)
            # The timestamp is the column that goes. It is the least of the
            # six facts and the only one the player can get elsewhere.
            if not narrow:
                draw_text(surface, game.assets, stamp,
                          (row.right - 20, row.y + 38), "tiny", FAINT,
                          right=True)
    scroll_hint(surface, game.assets, rect, state, len(slots))


def draw_world_map(game):
    """The whole island, at whatever size the window can give it (s42).

    Three things on it: the country you have surveyed, the fog over the
    country you have not, and a glowing dot on anywhere worth going that
    is not under the fog.
    """
    overlay(game, 236)
    surface, assets = game.screen, game.assets
    width, height = surface.get_size()
    rect = frame(game, "The Island",
                 "Enter to travel   M or Esc to close   "
                 "Towers light the map, they do not open it")

    from .world import WORLD_W, WORLD_H
    # Fit the island to whatever room is left, whole pixels only - a map
    # drawn at 2.4 pixels per tile shimmers.
    room_w = rect.w - 48
    room_h = rect.h - 132
    scale = max(1, min(room_w // WORLD_W, room_h // WORLD_H))
    board, scale = game.world_map(scale)
    bx = rect.centerx - board.get_width() // 2
    by = rect.y + 96
    surface.blit(board, (bx, by))
    pygame.draw.rect(surface, PANEL_EDGE,
                     (bx - 2, by - 2, board.get_width() + 4,
                      board.get_height() + 4), 1)

    beat = 0.55 + 0.45 * pulse(game.world_time, 1.4)
    for gx, gy, colour, _label in game.map_markers():
        cx, cy = bx + gx * scale, by + gy * scale
        glow = tuple(int(c * beat) for c in colour)
        pygame.draw.circle(surface, glow, (cx, cy), max(3, scale + 2))
        pygame.draw.circle(surface, (16, 14, 20), (cx, cy),
                           max(3, scale + 2), 1)

    px, py = game.player_tile()
    pygame.draw.circle(surface, (255, 255, 255),
                       (bx + px * scale, by + py * scale), max(3, scale + 1))
    pygame.draw.circle(surface, (16, 14, 20),
                       (bx + px * scale, by + py * scale),
                       max(3, scale + 1), 1)

    # The travel list, down the left. **Only open gates**: a shut one is
    # a fight you have not taken, and listing it as a destination would
    # say otherwise (s42).
    targets = game.travel_targets()
    if targets:
        game.ui.travel_index = min(game.ui.travel_index, len(targets) - 1)
        draw_text(surface, assets, "Travel to", (rect.x + 30, rect.y + 96),
                  "small", ACCENT)
        for i, (spot, name) in enumerate(targets[:8]):
            row = pygame.Rect(rect.x + 26, rect.y + 120 + i * 26,
                              190, 22)
            row_highlight(surface, row, i == game.ui.travel_index,
                          game=game, kind="travel", index=i)
            draw_text(surface, assets,
                      ellipsize(assets.font("tiny"), name, row.w - 20),
                      (row.x + 10, row.y + 5), "tiny",
                      INK if i == game.ui.travel_index else DIM)
    else:
        draw_text(surface, assets, "No stepstone is open yet.",
                  (rect.x + 30, rect.y + 96), "tiny", FAINT)

    climbed = game.world.towers_climbed()
    total = len(game.world.towers)
    draw_text(surface, assets,
              "%d of %d towers climbed" % (climbed, total),
              (rect.centerx, rect.bottom - 34), "small",
              GOOD if climbed == total else DIM, center=True)


def draw_chapter_card(game):
    """The chapter turning, over a dimmed valley (s19 item 10).

    Deliberately not a panel like everything else. A panel is a menu and
    this is a beat - the world stays visible underneath, darkened, so the
    card reads as something happening in the story rather than something
    the player opened.

    Only the chapter you are in is named. The ones ahead are not listed
    anywhere, the same rule locked codex rows follow (s33).
    """
    chapter = game.chapter_card
    if chapter is None:
        return
    surface, assets = game.screen, game.assets
    width, height = surface.get_size()

    # Fade the card out at the end rather than cutting it, and fade the
    # dimming with it - a bright valley snapping back is a flash.
    fade = clamp((game.CHAPTER_HOLD + game.CHAPTER_FADE - game.chapter_time)
                 / game.CHAPTER_FADE, 0, 1)
    veil = pygame.Surface((width, height), pygame.SRCALPHA)
    veil.fill((10, 9, 16, int(196 * fade)))
    surface.blit(veil, (0, 0))

    middle = height // 2
    # "Chapter 3 of 7" is safe to say and load-bearing: the count is the
    # part that answers "how far in am I", which is the whole complaint
    # this feature exists for. Six wardens is already public (the save
    # rows say so), so the total announces nothing unseen.
    number = "Chapter %d of %d" % (chapter.number, story.chapter_total())
    draw_text(surface, assets, number, (width // 2, middle - 52), "small",
              _faded(ACCENT, fade), center=True)

    # The title steps down a size on a narrow window rather than running
    # off it - the same rule `frame` follows for a screen heading.
    title_font = "big"
    if assets.font(title_font).size(chapter.title)[0] > width - 64:
        title_font = "mid"
    draw_text(surface, assets,
              ellipsize(assets.font(title_font), chapter.title, width - 64),
              (width // 2, middle - 14), title_font, _faded(INK, fade),
              center=True)

    for i, line in enumerate(wrap_text(chapter.goal, assets.font("small"),
                                       min(width - 80, 520))):
        draw_text(surface, assets, line, (width // 2, middle + 30 + i * 20),
                  "small", _faded(DIM, fade), center=True)

    draw_text(surface, assets, "any key to continue",
              (width // 2, height - 46), "tiny", _faded(FAINT, fade),
              center=True)


def _faded(color, fade):
    """A colour dimmed toward the background, since `draw_text` has no
    alpha of its own and the card has to be able to leave."""
    return tuple(int(c * fade + 12 * (1 - fade)) for c in color[:3])


CREATOR_ROWS = ("Name", "Build", "Skin", "Hair", "Style", "Outfit",
                "Surprise me", "Begin")
CREATOR_FIELDS = {"Build": "build", "Skin": "skin", "Hair": "hair",
                  "Style": "style", "Outfit": "outfit"}


def draw_name_entry(game):
    """Character creation: name, look, and a live preview of the result."""
    surface, assets = game.screen, game.assets
    look = game.pending_appearance
    rect = frame(game, "Make your hero",
                 "Up/Down to pick a line   Left/Right to change   "
                 "Enter to begin")

    left = rect.x + 46
    top = rect.y + 104
    # Eight rows at 46 is 368 pixels, which is more than a landscape phone
    # has in total. The pitch follows the room instead; all eight still fit
    # because they are allowed to get closer together rather than scroll -
    # a creator you have to scroll to find "Begin" in is a worse creator.
    _start, _visible, row_h = list_view(top, rect.bottom - 24,
                                        len(CREATOR_ROWS), 0, natural=46,
                                        minimum=26)
    # Beside the rows on a wide frame, underneath them on a narrow one.
    # Squeezed between the two it came out eighty pixels across, which is
    # narrower than the class name it has to caption.
    stacked = is_narrow(rect)
    row_w = (rect.w - 92) if stacked else min(430, max(180, rect.w - 380))

    for i, label in enumerate(CREATOR_ROWS):
        row = pygame.Rect(left, top + i * row_h, row_w,
                          row_height(assets, "body", row_h))
        selected = i == game.ui.creator_index
        accent = ACCENT
        value = ""
        if label == "Name":
            caret = "_" if int(pygame.time.get_ticks() / 380) % 2 else " "
            value = game.name_buffer + (caret if selected else "")
        elif label in CREATOR_FIELDS:
            field = CREATOR_FIELDS[label]
            value = look.label(field)
            accent = look.color(field)
        row_highlight(surface, row, selected, accent, game=game,
                      kind="creator", index=i)
        # One pass over the row, left to right: label, then the arrow pair
        # pushed clear of it, then the value in whatever is left. Placing
        # the three from three independent constants is what put the arrows
        # on the label at one width and on the value at another.
        font = assets.font("body")
        text_y = row.y + max(1, (row.h - font.get_height()) // 2)
        draw_text(surface, assets, label, (row.x + 16, text_y), "body",
                  INK if selected else DIM)
        arrowed = label in CREATOR_FIELDS
        prev_x = max(row.x + 96, row.x + 16 + font.size(label)[0] + 14)
        next_x = prev_x + 72
        value_right = row.right - 16
        value_room = (value_right - (next_x + 30 + 12) if arrowed
                      else row.w - 44 - font.size(label)[0])
        if value:
            # **`right=True` anchors on the vertical centre** - §7 has that
            # trap written down - and `text_y` is a *top*. The value was
            # drawn half a line higher than its own label and hung out of
            # the top of the row's highlight on every creator row (s53).
            row_text(surface, assets,
                     ellipsize(font, value, max(0, value_room)),
                     row, "body", accent, pad=16, right=True)
        if label in CREATOR_FIELDS:
            # Arrows are always drawn and always clickable, so the mouse has
            # an obvious target rather than relying on click-to-cycle alone.
            inset = min(4, max(1, row.h // 8))
            arrow_h = max(12, row.h - inset * 2)
            prev_box = pygame.Rect(prev_x, row.y + inset, 30, arrow_h)
            next_box = pygame.Rect(next_x, row.y + inset, 30, arrow_h)
            game.ui.hot.add(prev_box, "creator_prev", i)
            game.ui.hot.add(next_box, "creator_next", i)
            for box, hkind in ((prev_box, "creator_prev"),
                               (next_box, "creator_next")):
                if game.hover == (hkind, i):
                    highlight(surface, box, ACCENT, fill_alpha=44, border=2)
            arrow = ACCENT if selected else FAINT
            draw_text(surface, assets, "<", prev_box.center, "body", arrow,
                      center=True)
            draw_text(surface, assets, ">", next_box.center, "body", arrow,
                      center=True)

    # Live preview, drawn from the very same code the game will use.
    if stacked:
        stage_top = top + row_h * len(CREATOR_ROWS) + 12
        stage = pygame.Rect(left, stage_top, row_w,
                            max(80, rect.bottom - 30 - stage_top))
    else:
        stage_x = left + row_w + 40
        stage = pygame.Rect(stage_x, top, max(80, rect.right - 46 - stage_x),
                            max(80, row_h * len(CREATOR_ROWS) - 8))
    panel(surface, stage, edge=look.color("outfit"))
    bob = math.sin(pygame.time.get_ticks() / 420.0) * 3
    facing = ("front", "side", "back")[int(pygame.time.get_ticks() / 1600) % 3]
    walk = int(pygame.time.get_ticks() / 190) % 4
    # The outlined hero grid is 18x18. Size it to the stage with the caption
    # area reserved, so the sprite never sits on top of its own name.
    caption_h = 74
    scale = max(3, min((stage.w - 56) // 18, (stage.h - caption_h - 36) // 18))
    hero = assets.hero(facing, walk, scale=scale, appearance=look)
    art_center_y = stage.y + (stage.h - caption_h) // 2
    surface.blit(hero, (stage.centerx - hero.get_width() // 2,
                        int(art_center_y - hero.get_height() // 2 + bob)))
    draw_text(surface, assets, sec.sanitize_name(game.name_buffer),
              (stage.centerx, stage.bottom - 56), "body", INK, center=True)
    from . import classes as classes_mod
    chosen = classes_mod.get(game.pending_class)
    draw_text(surface, assets, chosen.name, (stage.centerx, stage.bottom - 30),
              "small", chosen.color, center=True)

    # Only if the rows left room for it. On a short window the pitch has
    # already squeezed to its floor, and a hint printed anyway lands on top
    # of the Begin button - which is the one row that must stay readable.
    rows_end = top + row_h * len(CREATOR_ROWS)
    if rect.bottom - 30 > rows_end + 6:
        draw_text(surface, assets,
                  "letters, digits, spaces, - and _ only (max %d)"
                  % sec.MAX_NAME_LEN,
                  (rect.centerx, rect.bottom - 30), "tiny", FAINT,
                  center=True)


# ------------------------------------------------------------- inventory ---

def draw_inventory(game):
    overlay(game)
    surface, assets, player = game.screen, game.assets, game.player
    rect = frame(game, "Bag", "%d / %d slots used   %s gold"
                 % (len(player.inventory), sec.MAX_INVENTORY,
                    commafy(player.gold)))
    state = game.ui

    # Tabs share the width rather than each taking a fixed hundred pixels,
    # which walked five of them straight off a narrow frame.
    tab_row(surface, assets, game, rect, TABS, state.inv_tab, "inv_tab")

    # One column when there is no room for the detail card beside the list.
    # The row already carries the name and as many stats as fit, and the
    # action bar at the bottom names what is selected, so dropping the card
    # costs the comparison arrows and nothing else.
    # The card needs room in both directions: 350 across and about 200
    # down. A short frame has the width and not the height, and the card
    # landed on the equipped panel.
    narrow = is_narrow(rect) or rect.h < 430
    # A short frame cannot spare 132 pixels for the equipped panel and still
    # show a list worth the name. It is dropped there rather than squeezed:
    # every row already says "equipped" where it applies, so what is lost is
    # a convenience and what is gained is the list being usable at all.
    short = rect.h < 430
    eq_h = 0 if short else 96

    list_x = rect.x + 30
    list_y = rect.y + 128
    row_h = 34
    card_w = 0 if narrow else 350
    list_w = max(140, rect.w - 60 - card_w)
    # Size the visible window to the space above the equipped panel, so a
    # full bag can never draw rows on top of it. The old floor of four rows
    # did exactly that on a short window: it kept drawing four whatever the
    # room, and the last two landed on the panel.
    # The Confirm button sits bottom-right and the list stops above it,
    # rather than the list running full width straight underneath it.
    button = pygame.Rect(rect.right - 350, rect.bottom - 190, 320, 56)
    if short:
        button = pygame.Rect(rect.right - 250, rect.bottom - 96, 230, 48)
    list_bottom = min(rect.bottom - eq_h - 54,
                      button.top - 8 if narrow else rect.bottom)
    state.inv.visible = max(1, (list_bottom - list_y) // row_h)

    if state.inv_tab == 4:
        rows = consumable_rows(player)
        state.inv.clamp_to(len(rows))
        if not rows:
            draw_text(surface, assets, "No consumables.",
                      (rect.centerx, rect.centery), "mid", FAINT, center=True)
        for i, (key, item, count) in state.inv.window(rows):
            row = pygame.Rect(list_x, list_y + (i - state.inv.offset) * row_h,
                              list_w, row_h - 4)
            row_highlight(surface, row, i == state.inv.index,
                          game=game, kind="inv", index=i)
            head = "%-16s x%d" % (item.name, count)
            draw_text(surface, assets, head, (row.x + 12, row.y + 6), "small",
                      item.color)
            # The description only if it fits beside the name. Same rule as
            # the stats on an item row, and the same reason.
            room = row.w - assets.font("small").size(head)[0] - 40
            if assets.font("tiny").size(item.description)[0] <= room:
                draw_text(surface, assets, item.description,
                          (row.right - 12, row.y + 8), "tiny", DIM,
                          right=True)
        if rows and not narrow:
            key, item, count = rows[state.inv.index]
            card = pygame.Rect(rect.right - 350, list_y, 320, 130)
            panel(surface, card, edge=item.color)
            draw_text(surface, assets, item.name, (card.x + 14, card.y + 12),
                      "body", item.color)
            for j, line in enumerate(wrap_text(item.description,
                                               assets.font("small"), 290)):
                draw_text(surface, assets, line,
                          (card.x + 14, card.y + 42 + j * 20), "small", INK)
            draw_text(surface, assets,
                      "Battle only" if item.battle_only else "Usable anywhere",
                      (card.x + 14, card.bottom - 26), "tiny", FAINT)
    else:
        items = filtered_inventory(player, state.inv_tab)
        state.inv.clamp_to(len(items))
        if not items:
            draw_text(surface, assets, "Nothing here yet.",
                      (rect.centerx, rect.centery), "mid", FAINT, center=True)
        for i, item in state.inv.window(items):
            row = pygame.Rect(list_x, list_y + (i - state.inv.offset) * row_h,
                              list_w, row_h - 4)
            row_highlight(surface, row, i == state.inv.index, item.color,
                          game=game, kind="inv", index=i)
            equipped = player.equipment.get(item.slot) is item
            # Reserve room for the name, then fit as many stats as remain.
            # No floor: a row with no room left after the name shows no
            # stats rather than printing them over it. `max(90, ...)` was
            # doing the latter on every narrow row.
            name_w = assets.font("small").size(item.name[:34])[0]
            stats_w = row.w - name_w - 40
            tail = ("equipped" if equipped
                    else fit_stats(item, assets.font("tiny"), stats_w)
                    if stats_w >= 40 else "")
            tail_w = assets.font("tiny").size(tail)[0] if tail else 0
            draw_text(surface, assets,
                      ellipsize(assets.font("small"), item.name[:34],
                                row.w - 28 - tail_w),
                      (row.x + 12, row.y + 6), "small", item.color)
            if tail:
                draw_text(surface, assets, tail,
                          (row.right - 12, row.y + 8), "tiny",
                          GOOD if equipped else DIM, right=True)
        if items and not narrow:
            chosen = items[state.inv.index]
            item_card(surface, assets, chosen, (rect.right - 350, list_y),
                      equipped=player.equipment.get(chosen.slot),
                      price_value=sell_price(chosen), price_label="Sells for")

    # Equipped panel, when the frame can hold it.
    if eq_h:
        eq_y = rect.bottom - eq_h - 36
        panel(surface, (rect.x + 30, eq_y, rect.w - 60, eq_h), alpha=210)
        column = max(70, (rect.w - 96) // 3)
        font = assets.font("small")
        for i, slot in enumerate(("weapon", "armor", "accessory")):
            item = player.equipment.get(slot)
            x = rect.x + 48 + i * column
            game.ui.hot.add(pygame.Rect(x - 8, eq_y + 8, column - 8,
                                        eq_h - 16), "gear", i)
            name = item.name if item else "- empty -"
            draw_text(surface, assets, slot.title(), (x, eq_y + 12), "tiny",
                      FAINT)
            draw_text(surface, assets,
                      ellipsize(font, name, column - 16), (x, eq_y + 32),
                      "small", item.color if item else FAINT)
            if item:
                draw_text(surface, assets,
                          ellipsize(assets.font("tiny"), item.stat_line(),
                                    column - 16),
                          (x, eq_y + 54), "tiny", DIM)
    # The same card the hero sheet shows, on the same verb (2026-09-01).
    worn = getattr(state, "gear_detail", None)
    if eq_h and worn is not None:
        piece = player.equipment.get(("weapon", "armor",
                                      "accessory")[worn % 3])
        if piece is not None:
            item_card(surface, assets, piece,
                      (rect.x + 40 + (worn % 3) * max(70, (rect.w - 96) // 3),
                       max(60, rect.bottom - eq_h - 260)),
                      width=min(320, rect.w // 2), player=player)

    label, detail, enabled = "Equip  (Enter)", "", False
    if state.inv_tab == 4:
        rows = consumable_rows(player)
        if rows:
            key, item, count = rows[min(state.inv.index, len(rows) - 1)]
            label, detail, enabled = "Use  (Enter)", item.name, True
            if item.battle_only:
                detail, enabled = "%s - battle only" % item.name, False
    else:
        pool = filtered_inventory(player, state.inv_tab)
        if pool:
            chosen = pool[min(state.inv.index, len(pool) - 1)]
            worn = player.equipment.get(chosen.slot) is chosen
            label = "Equipped" if worn else "Equip  (Enter)"
            detail = chosen.name[:26]
            enabled = not worn
    confirm_button(surface, assets, game, button, label, detail,
                   enabled=enabled)

    key_hint(surface, assets,
             [("Tab", "Filter"), ("Enter", "Equip / Use"), ("B", "Equip best"),
              ("U", "Unequip"), ("X", "Drop"), ("Esc", "Close")],
             (rect.x + 30, rect.bottom - 28), game=game)


def inventory_event(game, event):
    state, player = game.ui, game.player
    if event.key == pygame.K_TAB:
        step = -1 if event.mod & pygame.KMOD_SHIFT else 1
        state.inv_tab = (state.inv_tab + step) % len(TABS)
        state.inv.index = state.inv.offset = 0
        game.sfx("move")
        return
    if state.inv_tab == 4:
        rows = consumable_rows(player)
        if event.key in (pygame.K_w, pygame.K_UP):
            state.inv.move(-1, len(rows))
            game.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            state.inv.move(1, len(rows))
            game.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e) and rows:
            key, item, _count = rows[state.inv.index]
            use_field_item(game, key, item)
        return

    items = filtered_inventory(player, state.inv_tab)
    if event.key in (pygame.K_w, pygame.K_UP):
        state.inv.move(-1, len(items))
        game.sfx("move")
    elif event.key in (pygame.K_s, pygame.K_DOWN):
        state.inv.move(1, len(items))
        game.sfx("move")
    elif event.key in (pygame.K_RETURN, pygame.K_e) and items:
        item = items[state.inv.index]
        player.equip(item)
        game.toast("Equipped %s" % item.name, item.color, 2.2)
        game.sfx("item")
        state.inv.clamp_to(len(filtered_inventory(player, state.inv_tab)))
    elif event.key == pygame.K_u:
        # Unequip the slot the cursor points at, falling back to the tab.
        if items:
            slot = items[state.inv.index].slot
        elif state.inv_tab in (1, 2, 3):
            slot = ("weapon", "armor", "accessory")[state.inv_tab - 1]
        else:
            slot = "weapon"
        removed = player.unequip(slot)
        if removed:
            game.toast("Unequipped %s" % removed.name, DIM, 2.0)
            game.sfx("back")
        else:
            game.sfx("error")
    elif event.key == pygame.K_b:
        changed = player.equip_best()
        if changed:
            game.toast("Equipped best: %s"
                       % ", ".join(i.name for i in changed)[:60], GOOD, 3.5)
            game.sfx("item")
        else:
            game.toast("Already wearing your best.", DIM, 2.0)
            game.sfx("error")
        state.inv.clamp_to(len(filtered_inventory(player, state.inv_tab)))
    elif event.key == pygame.K_x and items:
        item = items[state.inv.index]
        if player.equipment.get(item.slot) is item:
            game.sfx("error")
            return
        player.remove_item(item)
        game.toast("Dropped %s" % item.name, BAD, 2.0)
        game.sfx("back")


def use_field_item(game, key, item):
    """Consumables usable outside battle."""
    player = game.player
    if item.battle_only:
        game.toast("%s only works in battle." % item.name, BAD, 2.0)
        game.sfx("error")
        return
    if item.effect == "heal" and player.hp >= player.max_hp():
        game.toast("Already at full health.", DIM, 1.8)
        game.sfx("error")
        return
    if item.effect == "mana" and player.mp >= player.max_mp():
        game.toast("Already at full MP.", DIM, 1.8)
        game.sfx("error")
        return
    player.use_consumable(key)
    if item.effect == "heal":
        game.toast("Restored %d HP." % player.heal(item.power), GOOD, 2.0)
    elif item.effect == "mana":
        game.toast("Restored %d MP." % player.restore_mp(item.power), GOOD, 2.0)
    elif item.effect == "full":
        player.full_restore()
        game.toast("Fully restored.", GOOD, 2.0)
    elif item.effect == "cure":
        player.clear_statuses(only_bad=True)
        game.toast("Ailments cleared.", GOOD, 2.0)
    elif item.effect == "revive":
        player.revive_charge = True
        game.toast("Phoenix Down readied.", ACCENT, 2.0)
    game.sfx("heal")


# ------------------------------------------------------------------ shop ---

def confirm_button(surface, assets, game, rect, label, detail="",
                   enabled=True, color=GOOD):
    """A real Confirm button for actions that spend or change something.

    Buying and equipping used to happen on the same click that selected the
    row, so one stray click could spend your gold. Selecting and committing
    are now two separate acts.
    """
    panel(surface, rect, alpha=232, edge=color if enabled else (90, 90, 104))
    hovered = enabled and game is not None and game.hover == ("confirm", 0)
    if hovered:
        highlight(surface, rect, color, fill_alpha=40, border=2)
    draw_text(surface, assets, label,
              (rect.centerx, rect.y + (10 if detail else rect.h // 2 - 10)),
              "body", color if enabled else FAINT, center=True)
    if detail:
        draw_text(surface, assets, detail, (rect.centerx, rect.y + 34),
                  "tiny", DIM if enabled else FAINT, center=True)
    if enabled and game is not None:
        game.ui.hot.add(rect, "confirm", 0)
    return rect


def purse(surface, assets, player, rect, price=None):
    """Gold, bag space and potions, shown large while you are spending.

    Buried in a subtitle it was easy to miss; a shop should tell you what you
    can afford without you having to leave and check.
    """
    # Beside the Buy/Sell tabs on a wide frame, under them on a narrow one.
    # 258 from the right plus 30 + 216 of tabs on the left needs about 510
    # to keep apart, and below that they were drawn on top of each other.
    if rect.w >= 520:
        box = pygame.Rect(rect.right - 258, rect.y + 74, 228, 74)
    else:
        box = pygame.Rect(rect.x + 30, rect.y + 118, rect.w - 60, 42)
    panel(surface, box, alpha=225, edge=ACCENT)

    affordable = price is None or player.gold >= price
    used, cap = len(player.inventory), sec.MAX_INVENTORY
    # Right-aligned text anchors on its vertical centre, so these y values are
    # midpoints, not tops.
    if box.h < 60:
        # The wide, short variant: one row across instead of two stacked,
        # because two rows of labels in forty pixels is one row of labels
        # written over another.
        draw_text(surface, assets, "GOLD", (box.x + 14, box.y + 14), "tiny",
                  DIM)
        draw_text(surface, assets, commafy(player.gold),
                  (box.x + 60, box.y + 21), "small",
                  ACCENT if affordable else BAD)
        draw_text(surface, assets, "BAG", (box.centerx - 20, box.y + 14),
                  "tiny", DIM)
        draw_text(surface, assets, "%d / %d" % (used, cap),
                  (box.centerx + 22, box.y + 12), "small",
                  BAD if used >= cap else INK)
        if box.w >= 330:
            # Dropped on the narrowest frames. Steel is the smith's currency
            # and the trader does not take it, so it is the one of the three
            # that can go without anybody looking for it.
            draw_text(surface, assets, "STEEL", (box.right - 96, box.y + 14),
                      "tiny", DIM)
            draw_text(surface, assets, str(player.materials),
                      (box.right - 14, box.y + 21), "small", INK, right=True)
        return box

    draw_text(surface, assets, "GOLD", (box.x + 14, box.y + 14), "tiny", DIM)
    draw_text(surface, assets, commafy(player.gold),
              (box.right - 14, box.y + 22), "mid",
              ACCENT if affordable else BAD, right=True)

    draw_text(surface, assets, "BAG", (box.x + 14, box.y + 46), "tiny", DIM)
    draw_text(surface, assets, "%d / %d" % (used, cap),
              (box.x + 58, box.y + 44), "small",
              BAD if used >= cap else INK)

    draw_text(surface, assets, "STEEL", (box.x + 126, box.y + 46), "tiny",
              DIM)
    draw_text(surface, assets, str(player.materials),
              (box.right - 14, box.y + 52), "small", INK, right=True)
    return box


def shop_rows(game):
    """Unified row list so buy and sell share one cursor and one renderer."""
    state, player = game.ui, game.player
    if state.shop_mode == "buy":
        rows = [("consumable", CONSUMABLES[k], CONSUMABLES[k].price)
                for k in state.shop_keys]
        rows += [("item", item, item.price) for item in state.shop_items]
        # **The bag is the first thing he sells** (2026-09-01, s48), and it
        # is a row rather than a screen: the trader is already where gold
        # turns into carrying capacity.
        cost = player.bag_upgrade_cost()
        if cost:
            rows.insert(0, ("bag", None, cost))
        return rows
    return [("item", item, sell_price(item)) for item in player.inventory
            if player.equipment.get(item.slot) is not item]


def draw_shop(game):
    overlay(game)
    surface, assets, player, state = game.screen, game.assets, game.player, game.ui
    rect = frame(game, "Trader", "Tab switches Buy / Sell")

    tab_row(surface, assets, game, rect, ("Buy", "Sell"),
            lambda i, label: state.shop_mode == label.lower(), "shop_tab")

    list_x, list_y, row_h = rect.x + 30, rect.y + 128, 34
    # One column when the card cannot sit beside the list, and no forced
    # floor of four rows on a frame that has room for one.
    narrow = is_narrow(rect) or rect.h < 430
    list_w = max(140, rect.w - 60 - (0 if narrow else 350))
    # A full-width list has to start under the purse, not behind it. The
    # purse only stays out of the way by itself while the list is stopping
    # short of the card column, which is exactly what `narrow` turns off.
    if narrow:
        list_y = max(list_y, rect.y + (172 if rect.w < 520 else 164))
    # Stop above the Confirm button; a full-width list ran straight under it.
    list_bottom = min(rect.bottom - 60,
                      rect.bottom - 104 if narrow else rect.bottom)
    state.shop.visible = max(1, (list_bottom - list_y) // row_h)
    rows = shop_rows(game)
    state.shop.clamp_to(len(rows))
    selected_price = (rows[state.shop.index][2]
                      if rows and state.shop_mode == "buy" else None)
    purse(surface, assets, player, rect, selected_price)

    if not rows:
        message = "Nothing to sell." if state.shop_mode == "sell" else "Sold out."
        draw_text(surface, assets, message, (rect.centerx, rect.centery),
                  "mid", FAINT, center=True)

    for i, entry in state.shop.window(rows):
        kind, payload, price = entry
        # Stop short of the card and purse column on the right.
        row = pygame.Rect(list_x, list_y + (i - state.shop.offset) * row_h,
                          list_w, row_h - 4)
        row_highlight(surface, row, i == state.shop.index,
                      GOOD if kind == "bag" else payload.color,
                      game=game, kind="shop", index=i)
        # The bag upgrade is a row with nothing behind it - there is no
        # item to buy, only a number to move (s48).
        name = ("A bigger bag  (+%d slots)" % player.BAG_STEP
                if kind == "bag" else payload.name[:34])
        font = assets.font("small")
        tag = "%d g" % price
        while name and font.size(name)[0] + font.size(tag)[0] > row.w - 40:
            name = name[:-1]
        draw_text(surface, assets, name, (row.x + 12, row.y + 6), "small",
                  GOOD if kind == "bag" else payload.color)
        affordable = state.shop_mode == "sell" or player.gold >= price
        draw_text(surface, assets, tag, (row.right - 12, row.y + 6),
                  "small", ACCENT if affordable else BAD, right=True)

    if rows and not narrow:
        kind, payload, price = rows[state.shop.index]
        card_y = rect.y + 166          # clears the purse panel above it
        if kind == "bag":
            card = pygame.Rect(rect.right - 350, card_y, 320, 108)
            panel(surface, card, edge=GOOD)
            draw_text(surface, assets, "A bigger bag",
                      (card.x + 14, card.y + 12), "body", GOOD)
            draw_text(surface, assets,
                      "Carry %d instead of %d."
                      % (min(player.bag_size() + player.BAG_STEP,
                             sec.MAX_INVENTORY), player.bag_size()),
                      (card.x + 14, card.y + 44), "small", INK)
            draw_text(surface, assets,
                      "Each one costs more than the last.",
                      (card.x + 14, card.y + 70), "tiny", DIM)
        elif kind == "item":
            item_card(surface, assets, payload, (rect.right - 350, card_y),
                      equipped=player.equipment.get(payload.slot),
                      price_value=price,
                      price_label="Sells for" if state.shop_mode == "sell"
                      else "Costs")
        else:
            card = pygame.Rect(rect.right - 350, card_y, 320, 122)
            panel(surface, card, edge=payload.color)
            draw_text(surface, assets, payload.name, (card.x + 14, card.y + 12),
                      "body", payload.color)
            for j, line in enumerate(wrap_text(payload.description,
                                               assets.font("small"), 288)):
                draw_text(surface, assets, line,
                          (card.x + 14, card.y + 44 + j * 20), "small", INK)
            draw_text(surface, assets,
                      "You hold %d" % player.consumable_count(payload.key),
                      (card.x + 14, card.bottom - 26), "tiny", DIM)

    if rows:
        kind, payload, price = rows[state.shop.index]
        buying = state.shop_mode == "buy"
        can = (player.gold >= price) if buying else True
        if buying and kind == "item" and player.bag_full():
            can = False
        verb = "Buy" if buying else "Sell"
        # The bag upgrade is a row with no item behind it (s48), so every
        # line that reaches through `payload` needs the name from
        # somewhere else.
        row_name = ("A bigger bag" if kind == "bag" else payload.name[:22])
        detail = ("%s  -  %s g" % (row_name, commafy(price)))
        if buying and player.gold < price:
            detail = "not enough gold"
        elif buying and kind == "item" and player.bag_full():
            detail = "bag is full"
        confirm_button(surface, assets, game,
                       pygame.Rect(rect.right - 258, rect.bottom - 96,
                                   228, 56),
                       "%s  (Enter)" % verb, detail, enabled=can,
                       color=GOOD if buying else ACCENT)

    key_hint(surface, assets,
             [("Tab", "Buy/Sell"), ("Enter", "Confirm"), ("Esc", "Leave")],
             (rect.x + 30, rect.bottom - 28), game=game)


def shop_event(game, event):
    state, player = game.ui, game.player
    rows = shop_rows(game)
    if event.key == pygame.K_TAB:
        state.shop_mode = "sell" if state.shop_mode == "buy" else "buy"
        state.shop.index = state.shop.offset = 0
        game.sfx("move")
        return
    if event.key in (pygame.K_w, pygame.K_UP):
        state.shop.move(-1, len(rows))
        game.sfx("move")
    elif event.key in (pygame.K_s, pygame.K_DOWN):
        state.shop.move(1, len(rows))
        game.sfx("move")
    elif event.key in (pygame.K_RETURN, pygame.K_e) and rows:
        kind, payload, price = rows[state.shop.index]
        if state.shop_mode == "buy":
            if player.gold < price:
                game.toast("Not enough gold.", BAD, 2.0)
                game.sfx("error")
                return
            if kind == "bag":
                size = player.buy_bag_upgrade()
                if size:
                    game.toast("Your bag holds %d now." % size, GOOD, 3.0)
            elif kind == "consumable":
                player.gold -= price
                player.add_consumable(payload.key)
                game.toast("Bought %s." % payload.name, GOOD, 2.0)
            else:
                if player.bag_full():
                    game.toast("Bag is full.", BAD, 2.0)
                    game.sfx("error")
                    return
                player.gold -= price
                player.add_item(payload)
                state.shop_items.remove(payload)
                game.toast("Bought %s." % payload.name, payload.color, 2.5)
            game.sfx("coin")
        else:
            player.gold += price
            player.stats_tracker["gold_earned"] += price
            player.remove_item(payload)
            game.toast("Sold %s for %d g." % (payload.name, price), ACCENT, 2.2)
            game.sfx("coin")
        state.shop.clamp_to(len(shop_rows(game)))


# ----------------------------------------------------------------- smith ---

def equipped_slots(player):
    return [(slot, player.equipment.get(slot))
            for slot in ("weapon", "armor", "accessory")]


def smith_rows(game):
    """Refine lists the three worn slots; salvage lists spare gear."""
    player = game.player
    if game.ui.smith_mode == "refine":
        return [("slot", slot, player.equipment.get(slot))
                for slot in ("weapon", "armor", "accessory")]
    return [("item", item.slot, item) for item in player.inventory
            if player.equipment.get(item.slot) is not item]


def draw_smith(game):
    """Refine what you wear, fuelled by breaking down what you do not.

    Refinement used to be pure gold, which felt like re-buying the same
    weapon. It now costs Ember Steel, and Ember Steel only comes from
    salvaging the loot you were going to sell for coppers anyway.
    """
    overlay(game)
    surface, assets, player, state = game.screen, game.assets, game.player, game.ui
    rect = frame(game, "Bram the Smith", "Tab switches Refine / Salvage")
    coins = purse(surface, assets, player, rect)

    tab_row(surface, assets, game, rect, ("Refine", "Salvage"),
            lambda i, label: state.smith_mode == label.lower(), "smith_tab")

    # One column when the card cannot sit beside the list, and the list
    # stops above the Confirm button instead of running under it.
    narrow = is_narrow(rect) or rect.h < 430
    list_w = max(140, rect.w - 60 - (0 if narrow else 350))
    list_top = rect.y + (172 if rect.w < 520 else 128)
    # **The two helpers disagreed about which layout this was** (s53).
    # `purse` picks its corner box on width alone; the list goes to one
    # full-width column on width *or* height. On a short wide window - a
    # phone in landscape - the rows ran under the purse and the first one
    # had BAG and STEEL sitting inside its highlight. `purse` hands back
    # the box it used, so the list can simply start below it.
    if narrow:
        list_top = max(list_top, coins.bottom + 10)
    rows = smith_rows(game)
    state.smith.visible = max(1, ((rect.bottom - (130 if not narrow else 116))
                                  - list_top) // 48)
    state.smith.clamp_to(len(rows))
    if not rows:
        draw_text(surface, assets, "Nothing to break down.",
                  (rect.centerx, rect.centery), "mid", FAINT, center=True)

    for i, (kind, slot, item) in state.smith.window(rows):
        row = pygame.Rect(rect.x + 30,
                          list_top + (i - state.smith.offset) * 48,
                          list_w, 42)
        row_highlight(surface, row, i == state.smith.index,
                      item.color if item else PANEL_EDGE, game=game,
                      kind="smith", index=i)
        draw_text(surface, assets, slot.title() if kind == "slot" else "",
                  (row.x + 14, row.y + 4), "tiny", FAINT)
        # The price is worked out first so the name knows how much room it
        # has left; a right-aligned price on a narrow row was landing at a
        # negative x, which is off the window entirely.
        tag, tag_colour = "", DIM
        if item and kind == "slot":
            level = item.name.count("+")
            tag = "%s g + %d steel" % (commafy(upgrade_cost(item, level)),
                                       upgrade_materials(item, level))
        elif item:
            tag, tag_colour = "+%d steel" % salvage_yield(item), ACCENT
        tag_w = assets.font("tiny").size(tag)[0] if tag else 0
        draw_text(surface, assets,
                  ellipsize(assets.font("small"),
                            item.name if item else "- nothing -",
                            row.w - 34 - tag_w),
                  (row.x + 14, row.y + 20), "small",
                  item.color if item else FAINT)
        if tag:
            draw_text(surface, assets, tag, (row.right - 14, row.y + 20),
                      "tiny", tag_colour, right=True)

    chosen = rows[state.smith.index][2] if rows else None
    if chosen and not narrow:
        item_card(surface, assets, chosen, (rect.right - 350, rect.y + 166))

    button = pygame.Rect(rect.right - 258, rect.bottom - 96, 228, 56)
    if state.smith_mode == "refine":
        if chosen:
            level = chosen.name.count("+")
            price = upgrade_cost(chosen, level)
            steel = upgrade_materials(chosen, level)
            can = player.gold >= price and player.materials >= steel
            if player.gold < price:
                detail = "not enough gold"
            elif player.materials < steel:
                detail = "need %d steel, you have %d" % (steel,
                                                         player.materials)
            else:
                detail = "%s g and %d steel" % (commafy(price), steel)
            confirm_button(surface, assets, game, button, "Refine  (Enter)",
                           detail, enabled=can, color=ACCENT)
        note = "Refining raises every positive stat by 12% and adds 1."
    else:
        if chosen:
            confirm_button(surface, assets, game, button, "Salvage  (Enter)",
                           "%s  ->  +%d steel" % (chosen.name[:16],
                                                  salvage_yield(chosen)),
                           enabled=True, color=GOOD)
        note = "Breaking gear down is the only source of Ember Steel."
    # Only where it will not run under the Confirm button or off the frame.
    note_room = (button.x if narrow else rect.right) - rect.x - 60
    draw_text(surface, assets,
              ellipsize(assets.font("small"), note, note_room),
              (rect.x + 30, rect.bottom - 74), "small", DIM)

    key_hint(surface, assets,
             [("Tab", "Refine/Salvage"), ("Enter", "Confirm"),
              ("Esc", "Leave")],
             (rect.x + 30, rect.bottom - 28), game=game)


def smith_event(game, event):
    state, player = game.ui, game.player
    rows = smith_rows(game)
    if event.key == pygame.K_TAB:
        state.smith_mode = ("salvage" if state.smith_mode == "refine"
                            else "refine")
        state.smith.index = state.smith.offset = 0
        game.sfx("move")
        return
    if event.key in (pygame.K_w, pygame.K_UP):
        state.smith.move(-1, len(rows))
        game.sfx("move")
        return
    if event.key in (pygame.K_s, pygame.K_DOWN):
        state.smith.move(1, len(rows))
        game.sfx("move")
        return
    if event.key not in (pygame.K_RETURN, pygame.K_e) or not rows:
        return

    kind, slot, item = rows[state.smith.index]
    if item is None:
        game.toast("Nothing equipped in that slot.", BAD, 2.0)
        game.sfx("error")
        return

    if state.smith_mode == "salvage":
        gained = salvage_yield(item)
        player.remove_item(item)
        player.materials += gained
        game.toast("Broke down %s for %d Ember Steel."
                   % (item.name, gained), ACCENT, 3.0)
        game.sfx("hit")
        state.smith.clamp_to(len(smith_rows(game)))
        return

    level = item.name.count("+")
    price = upgrade_cost(item, level)
    steel = upgrade_materials(item, level)
    if player.gold < price:
        game.toast("Bram wants %s gold." % commafy(price), BAD, 2.5)
        game.sfx("error")
        return
    if player.materials < steel:
        game.toast("Bram needs %d Ember Steel. Salvage something spare."
                   % steel, BAD, 3.5)
        game.sfx("error")
        return
    player.gold -= price
    player.materials -= steel
    apply_upgrade(item)
    player.hp = min(player.hp, player.max_hp())
    game.toast("Refined into %s" % item.name, item.color, 3.0)
    game.sfx("unlock")


# ------------------------------------------------------------- character ---

def draw_character(game):
    overlay(game)
    surface, assets, player, state = game.screen, game.assets, game.player, game.ui
    rect = frame(game, player.name,
                 "%s   Level %d   %d unspent points   "
                 "Tab: %s   R: respec"
                 % (player.pclass.name, player.level, player.points,
                    "attributes" if state.char_pane == "skills"
                    else "skills"))

    left = rect.x + 40
    top = rect.y + 100
    # Four even columns computed from the frame, so a narrow window cannot
    # push the skill list into the lifetime totals.
    # Four columns need about 160 each to hold a label and a right-aligned
    # value without them meeting in the middle. Below that the sheet folds
    # into two columns and two bands: stats and attributes on the first,
    # skills and this-run on the second.
    columns = 4 if rect.w >= 640 else 2
    col_w = (rect.w - 80) // columns
    band = 0 if columns == 4 else 26 * 11 + 40
    bar(surface, (left, top, min(320, col_w * 2), 12),
        player.xp / max(1, player.xp_to_next()), (192, 118, 250))

    draw_text(surface, assets, "XP %d / %d" % (player.xp, player.xp_to_next()),
              (left, top + 18), "tiny", DIM)

    draw_text(surface, assets,
              ellipsize(assets.font("tiny"), player.pclass.signature_name,
                        col_w // 2 - 20),
              (left, top + 40), "tiny", player.pclass.color)
    # **What your free swing is** (item 0ai, s65). It is the move a player
    # presses most and the only part of a class that was never written
    # down anywhere they could read it.
    from .classes import strike_for
    strike = strike_for(player.class_key)
    # **On the signature's own line, right-aligned.** A line of its own
    # underneath had nowhere to go: the XP bar is above it and the first
    # stat's right-aligned *value* is 12 pixels below, which `layout.py`
    # caught twice - first as a 56% collision across columns, then as a
    # 31% one down the same column. Half the column each, so a long
    # signature and a long swing cannot meet in the middle.
    draw_text(surface, assets,
              ellipsize(assets.font("tiny"), strike.name, col_w // 2 - 20),
              (left + col_w - 24, top + 40), "tiny", DIM, right=True)
    stats = [("Max HP", player.max_hp()), ("Max MP", player.max_mp()),
             ("Attack", player.stat("atk")), ("Defense", player.stat("defense")),
             ("Magic", player.stat("mag")), ("Resist", player.stat("res")),
             ("Speed", player.stat("spd")),
             ("Crit", "%d%%" % player.stat("crit")),
             ("Crit dmg", "+%d%%" % player.stat("crit_dmg")),
             ("Lifesteal", "%d%%" % player.stat("lifesteal"))]
    font = assets.font("small")
    for i, (label, value) in enumerate(stats):
        y = top + 66 + i * 24
        if y > rect.bottom - 30:
            break
        draw_text(surface, assets, label, (left, y), "small", DIM)
        # Right-aligned, so the edge has to clear the label *plus* the value
        # itself. Forgetting the second half left a 23% overlap that read as
        # fixed and was not.
        edge = max(left + col_w - 24,
                   left + font.size(label)[0] + 16 + font.size(str(value))[0])
        draw_text(surface, assets, str(value), (edge, y), "small", INK,
                  right=True)

    # **What you are wearing, directly under what it adds up to** (s45).
    # He asked for this twice - *"should be a way to check the stats of
    # your own weapons"* in the first school playtest and *"there's still
    # no way to easily check the stats of the items you have equipped"* on
    # 2026-08-31 - and the bag's version was never the answer: it is a
    # strip along the bottom that only appears *"when the frame can hold
    # it"*, on a screen you open to change things rather than to read them.
    #
    # The hero sheet is where you go to look at yourself, and this column
    # already holds the totals these three items produce. Adjacency is the
    # whole feature: Attack 34 above, and the +6 that is doing it below.
    skills_top_guess = top + (0 if columns == 4 else band)
    gear_top = top + 66 + len(stats) * 24 + 10
    gear_floor = (skills_top_guess + 30 if columns == 2
                  else rect.bottom - 34)
    slots = ("weapon", "armor", "accessory")
    # The name starts clear of the widest label rather than at a guessed
    # offset: "Accessory" is half again as wide as "Armor" and ran
    # straight into its own value.
    name_x = left + max(assets.font("tiny").size(s.title())[0]
                        for s in slots) + 12
    room = max(0, gear_floor - (gear_top + 24))
    pitch = min(40, room // max(1, len(slots)))
    # **The header is drawn only if a row can follow it.** On a short
    # window the ten stat rows above already reach the bottom of the
    # frame, and a heading with nothing under it was landing off the
    # window entirely - which `layout.py` caught at 844x390.
    if pitch >= 18:
        draw_text(surface, assets, "EQUIPPED", (left, gear_top), "small",
                  ACCENT)
    # Same rule the attribute help follows: the second line is the first
    # thing to go, and the name is the half you steer by.
    show_stats = pitch >= 34
    for i, slot in enumerate(slots):
        y = gear_top + 24 + i * pitch
        if pitch < 18 or y > gear_floor - 12:
            break
        item = player.equipment.get(slot)
        # Registered so the mouse can reach them: right-click inspects,
        # which is the same verb the skills list uses (2026-09-01).
        game.ui.hot.add(pygame.Rect(left - 6, y - 4, col_w - 10,
                                    max(16, pitch - 4)), "gear", i)
        draw_text(surface, assets, slot.title(), (left, y), "tiny", FAINT)
        name = item.name if item else "- empty -"
        draw_text(surface, assets,
                  ellipsize(font, name, left + col_w - name_x - 12),
                  (name_x, y), "small", item.color if item else FAINT)
        if item and show_stats:
            draw_text(surface, assets,
                      ellipsize(assets.font("tiny"), item.stat_line(),
                                left + col_w - name_x - 12),
                      (name_x, y + 17), "tiny", DIM)

    mid = rect.x + 40 + col_w
    # Needed by the attribute pitch below, so it is computed here rather
    # than beside the block that draws with it.
    far = rect.x + 40 + col_w * (3 if columns == 4 else 1)
    far_top = top + (0 if columns == 4 else band)
    draw_text(surface, assets, "ATTRIBUTES", (mid, top + 46), "small", ACCENT)
    tiny = assets.font("tiny")
    # **The pitch is derived, not chosen.** There were four attributes at a
    # fixed 42px until 2026-08-24; there are nine now, and on a narrow
    # window ATTRIBUTES and THIS RUN share one column separated only by a
    # vertical offset - so nine fixed rows walked straight into the totals.
    #
    # The budget is whatever sits below: the next block on a folded sheet,
    # the bottom of the frame on a wide one. Same idea as `ellipsize`
    # (section 29) one axis over - fit to the space, never to a count.
    attr_top = top + 76
    floor = (far_top + 40) if columns == 2 else (rect.bottom - 40)
    pitch = min(42, max(18, (floor - attr_top) // len(ATTRIBUTES)))
    # The help line is the first thing to go. A row that cannot hold both
    # keeps the name and the number, which is the half you steer by.
    show_help = pitch >= 34
    # **The label steps down a size before the row does** (s53). Nine rows
    # in a folded column can push the pitch below the height of `small`,
    # and a box that is then forced to fit the text overlaps the row under
    # it - trading one bad look for a worse one. `frame` already solves
    # this the right way round for its title: shrink the text, keep the
    # box honest.
    attr_font = "small" if pitch >= assets.font("small").get_height() + 6         else "tiny"
    line_h = assets.font(attr_font).get_height()
    for i, key in enumerate(ATTRIBUTES):
        y = attr_top + i * pitch
        if y > floor - 12:
            break
        # The box is built around what is drawn in it, and never taller
        # than its own slot.
        # With the help line the row covers two lines and the label sits
        # at the top of it; without one there is a single line to centre.
        row = pygame.Rect(mid - 8, y - 6 if show_help else y - 3,
                          max(40, col_w - 10),
                          min(38, pitch - 4) if show_help else line_h + 6)
        row_highlight(surface, row, i == state.char_index, game=game,
                      kind="attr", index=i)
        label = "%-9s %d" % (ATTRIBUTE_LABEL[key], player.attributes[key])
        if show_help:
            draw_text(surface, assets, label, (mid, y), attr_font, INK)
        else:
            row_text(surface, assets, label, row, attr_font, INK, pad=8)
        if not show_help:
            continue
        help_text = ATTRIBUTE_HELP[key]
        while help_text and tiny.size(help_text)[0] > col_w - 16:
            help_text = help_text[:-1]
        draw_text(surface, assets, help_text, (mid, y + 17), "tiny", FAINT)

    right = rect.x + 40 + col_w * (2 if columns == 4 else 0)
    skills_top = top + (0 if columns == 4 else band)
    # **The list is everything you know; the mark is what you carry.**
    # Item 13 is a loadout, and a loadout you cannot see the alternatives
    # to is just a shorter list.
    from .entities import LOADOUT_SIZE
    carried = {s.key for s in player.skills()}
    draw_text(surface, assets, "SKILLS  %d/%d"
              % (len(carried), LOADOUT_SIZE),
              (right, skills_top + 46), "small", ACCENT)
    for i, skill in enumerate(player.known_skills()[:11]):
        y = skills_top + 76 + i * 26
        if y > rect.bottom - 30:
            break
        affordable = player.mp >= skill.mp
        equipped = skill.key in carried
        row = pygame.Rect(right - 6, y - 4, col_w - 24, 24)
        # **Three states, through the one helper that draws them** (s53).
        # Savvydev, third school playtest: *"no clue what skill you have
        # selected"*. Two separate reasons, and both were invisible:
        #
        #  * the *cursor* was never drawn here at all. `state.skill_index`
        #    has existed since item 13 and only `character_event` ever read
        #    it, so Tab moved a selection nothing showed you.
        #  * the *equipped* mark was `highlight(surface, row, colour, 2)` -
        #    and the third positional argument of `highlight` is
        #    `fill_alpha`, not `border`. It was painting the carried skills
        #    at an alpha of 2 out of 255.
        #
        # `row_highlight` is the helper that already knows chosen / cursor
        # / hover apart, and routing through it also registers the hotspot,
        # which is what makes the pane clickable (rule 8b).
        row_highlight(surface, row,
                      state.char_pane == "skills" and i == state.skill_index,
                      (150, 210, 250), game=game, kind="loadout", index=i,
                      chosen=equipped)
        cost = "%d MP" % skill.mp
        cost_w = assets.font("tiny").size(cost)[0]
        # The column is the budget. Widening past it to fit a long name put
        # the cost on top of the next column; the name gives way instead.
        name = skill.name[:13]
        while name and font.size(name)[0] + 14 + cost_w > col_w - 24:
            name = name[:-1]
        draw_text(surface, assets, name, (right, y), "small",
                  INK if (affordable and equipped)
                  else DIM if equipped else FAINT)
        # **Inside the box, not past its edge** (s53). The cost was drawn
        # at `right + col_w - 24` and the row ends at `right + col_w - 30`,
        # so every MP figure hung six pixels out of the highlight behind
        # it - which is the thing he could see and the checker could not.
        draw_text(surface, assets, cost, (row.right - 8, y), "tiny",
                  DIM if affordable else FAINT, right=True)

    tracker = player.stats_tracker
    lines = ["Kills %d" % tracker["kills"], "Wardens %d" % tracker["bosses"],
             "Chests %d" % tracker["chests"], "Crits %d" % tracker["crits"],
             "Steps %d" % tracker["steps"], "Deaths %d" % tracker["deaths"],
             "Damage %d" % tracker["damage"],
             "Played %s" % format_playtime(game.playtime)]
    draw_text(surface, assets, "THIS RUN", (far, far_top + 46), "small",
              ACCENT)
    for i, line in enumerate(lines):
        if far_top + 76 + i * 22 > rect.bottom - 40:
            break
        draw_text(surface, assets, line, (far, far_top + 76 + i * 22), "tiny",
                  DIM)

    # **The gear card, if an equipped row was right-clicked** (2026-09-01):
    # *"this is kinda useless if everything just gets cut off"*. The row
    # has one line and a name to fit in a quarter of the frame; the card
    # has the whole item, and `item_card` is the one that already knows how
    # to draw that.
    worn = getattr(state, "gear_detail", None)
    if worn is not None and 0 <= worn < len(slots):
        piece = player.equipment.get(slots[worn])
        if piece is not None:
            item_card(surface, assets, piece,
                      (left + col_w - 20, gear_top + 20),
                      width=min(320, max(220, rect.w // 3)), player=player)

    # **The card, if a skill was right-clicked** (2026-08-31). Drawn last
    # so it sits over the columns, and anchored to the row it came from.
    detail = getattr(state, "skill_detail", None)
    known = player.known_skills()
    if detail is not None and 0 <= detail < len(known):
        anchor_y = skills_top + 76 + detail * 26
        skill_card(surface, assets, known[detail], player,
                   (right + col_w - 40, anchor_y - 8),
                   width=min(300, max(200, rect.w // 3)))

    hints = [("Up/Down", "Pick attribute"), ("Enter", "Spend point"),
             ("Shift", "Spend five"), ("Right-click", "Inspect")]
    # Offered only when there is something to take back, with the price on
    # the chip: a respec you have to open a menu to price is a respec
    # nobody uses.
    respec = player.respec_cost()
    if respec:
        hints.append(("R", "Start over  %d g" % respec))
    hints.append(("Esc", "Close"))
    key_hint(surface, assets, hints, (rect.x + 30, rect.bottom - 28),
             game=game)


def character_event(game, event):
    state, player = game.ui, game.player
    known = player.known_skills()
    if state.char_pane == "skills" and known:
        if event.key in (pygame.K_w, pygame.K_UP):
            state.skill_index = (state.skill_index - 1) % len(known)
            game.sfx("move")
            return
        if event.key in (pygame.K_s, pygame.K_DOWN):
            state.skill_index = (state.skill_index + 1) % len(known)
            game.sfx("move")
            return
        if event.key in (pygame.K_RETURN, pygame.K_e):
            game.toggle_skill(known[min(state.skill_index,
                                        len(known) - 1)].key)
            return
    if event.key in (pygame.K_w, pygame.K_UP):
        state.char_index = (state.char_index - 1) % len(ATTRIBUTES)
        game.sfx("move")
    elif event.key in (pygame.K_s, pygame.K_DOWN):
        state.char_index = (state.char_index + 1) % len(ATTRIBUTES)
        game.sfx("move")
    elif event.key == pygame.K_r:
        game.ask_respec()
    elif event.key == pygame.K_TAB:
        # **A second cursor, not a second screen.** The loadout lives on
        # the sheet because it is the same question the attributes are -
        # what kind of hero is this - and a screen you have to go and find
        # is a screen most players will not.
        state.char_pane = "skills" if state.char_pane != "skills" else "stats"
        game.sfx("move")
    elif event.key in (pygame.K_RETURN, pygame.K_e):
        key = ATTRIBUTES[state.char_index]
        # **Hold Shift to spend five** (2026-09-01): *"add a way to put in
        # multiple skill points into an attribute at once"*. Five rather
        # than all of them, because "all" is a different decision and one
        # you cannot walk back without paying for a respec.
        wanted = 5 if event.mod & pygame.KMOD_SHIFT else 1
        spent = 0
        for _ in range(wanted):
            if not player.spend_point(key):
                break
            spent += 1
        if spent:
            game.toast("%s raised to %d.%s"
                       % (ATTRIBUTE_LABEL[key], player.attributes[key],
                          "  (+%d)" % spent if spent > 1 else ""),
                       GOOD, 2.0)
            game.sfx("levelup")
        else:
            game.toast("No points to spend.", BAD, 1.6)
            game.sfx("error")


# ---------------------------------------------------------------- quests ---

# **Story is first**, and that is the whole point of the item. Opening
# the log to a list of jobs answers "what am I carrying"; his friend's
# problem was "what am I doing" (s40), and the first thing a screen shows
# is the thing it is for.
QUEST_TABS = ("Story", "Offered", "Active", "Done", "Feats")
STORY_TAB = 0


def quest_rows(game):
    log, player = game.quests, game.player
    tab = game.ui.quest_tab
    if tab == STORY_TAB:
        return []               # a page, not a list - see `draw_story_tab`
    if tab == 1:
        return [("offer", q) for q in log.available(player)]
    if tab == 2:
        from .quests import ERRANDS_BY_KEY
        rows = [("active", QUESTS_BY_KEY[k]) for k in log.active]
        rows += [("errand", ERRANDS_BY_KEY[k]) for k in log.errands
                 if k in ERRANDS_BY_KEY]
        return rows
    if tab == 3:
        return [("done", QUESTS_BY_KEY[k]) for k in log.completed]
    return [("feat", a) for a in ACHIEVEMENTS]


def draw_story_tab(game, rect):
    """Where you are, what to do, and what Maren last said (item 11).

    Three things, in that order, because that is the order a player who
    has been away for a week wants them. It is a page rather than a list:
    everything here is one fact with no rows to scroll.

    **It says nothing the player has not already been told.** The wardens
    behind you are counted, never named - a warden\'s true name is learned
    from their Memory, after the kill (s33) - and the chapters ahead are
    not listed at all. What is *new* here is only the arrangement.
    """
    surface, assets = game.screen, game.assets
    chapter = game.chapter()
    if chapter is None:
        return
    top = rect.y + 132
    draw_text(surface, assets,
              "Chapter %d of %d" % (chapter.number, story.chapter_total()),
              (rect.centerx, top), "small", ACCENT, center=True)
    title_font = "big" if not is_narrow(rect) else "mid"
    draw_text(surface, assets,
              ellipsize(assets.font(title_font), chapter.title, rect.w - 60),
              (rect.centerx, top + 30), title_font, INK, center=True)

    y = top + 78
    for line in wrap_text(chapter.goal, assets.font("small"),
                          min(rect.w - 80, 560)):
        draw_text(surface, assets, line, (rect.centerx, y), "small", DIM,
                  center=True)
        y += 22

    felled = game.world.wardens_defeated() if game.world else 0
    remaining = len(game.world.portals) - felled if game.world else 0
    draw_text(surface, assets,
              "%d of %d wardens have fallen.  %s"
              % (felled, len(game.world.portals) if game.world else 6,
                 "The Door is open." if not remaining
                 else "%d %s left." % (remaining,
                                       "road" if remaining == 1 else "roads")),
              (rect.centerx, y + 10), "tiny", FAINT, center=True)
    y += 42

    # **Maren\'s own words for this chapter, already written and almost
    # never read twice.** `MAREN_BRIEFINGS` is keyed by the same number the
    # chapter is, so this costs nothing and is the closest thing the game
    # has to a narrator saying where the story is.
    briefing = story.MAREN_BRIEFINGS.get(felled)
    if briefing and y < rect.bottom - 90:
        draw_text(surface, assets, "Elder Maren said:",
                  (rect.centerx, y), "tiny", FAINT, center=True)
        y += 24
        for said in briefing:
            for line in wrap_text(said, assets.font("small"),
                                  min(rect.w - 100, 520)):
                if y > rect.bottom - 52:
                    return
                draw_text(surface, assets, line, (rect.centerx, y), "small",
                          _MAREN, center=True)
                y += 21
            y += 4


# Maren is quoted rather than narrated, so her lines get a colour of their
# own - the same warm one the scene screens use for speech.
_MAREN = (214, 198, 168)


def draw_quests(game):
    overlay(game)
    surface, assets, player, state = game.screen, game.assets, game.player, game.ui
    from .quests import MAX_ACTIVE
    readonly = state.quest_readonly
    carrying = len(game.quests.active)
    from .quests import MAX_ERRANDS
    errands = len(game.quests.errands)
    subtitle = ("Your own notes - find Maren or a runner to take work on"
                if readonly else "Work, and the roads open.")
    # **Two counts, because there are two tracks.** One job from Maren, and
    # several errands from whoever asked - saying "1 of 1" while you carry
    # four errands would be a lie about what you are holding.
    rect = frame(game, "Quest log" if readonly else "Elder Maren",
                 "%s     Job %d of %d   Errands %d of %d"
                 % (subtitle, carrying, MAX_ACTIVE, errands, MAX_ERRANDS))

    tab_row(surface, assets, game, rect, QUEST_TABS, state.quest_tab,
            "quest_tab")

    if state.quest_tab == STORY_TAB:
        draw_story_tab(game, rect)
        key_hint(surface, assets, [("Tab", "Switch"), ("Esc", "Close")],
                 (rect.x + 30, rect.bottom - 28), game=game)
        return

    rows = quest_rows(game)
    state.quest.visible = max(3, ((rect.bottom - 60) - (rect.y + 128)) // 52)
    state.quest.clamp_to(len(rows))
    if not rows:
        draw_text(surface, assets, "Nothing here.",
                  (rect.centerx, rect.centery), "mid", FAINT, center=True)

    for i, (kind, entry) in state.quest.window(rows):
        row = pygame.Rect(rect.x + 30, rect.y + 128 + (i - state.quest.offset) * 52,
                          rect.w - 60, 46)
        row_highlight(surface, row, i == state.quest.index, game=game,
                      kind="quest", index=i)
        if kind == "feat":
            # This run's progress. The global cabinet is on the title screen;
            # here you are looking at what *this* character has done.
            done = entry.key in game.quests.unlocked
            draw_text(surface, assets, entry.shown_title(done),
                      (row.x + 14, row.y + 6),
                      "small", ACCENT if done else FAINT)
            draw_text(surface, assets, entry.shown_detail(done),
                      (row.x + 14, row.y + 26),
                      "tiny", DIM if done else FAINT)
            draw_text(surface, assets, "unlocked" if done else "locked",
                      (row.right - 14, row.y + 16), "tiny",
                      GOOD if done else FAINT, right=True)
            continue

        draw_text(surface, assets, entry.title, (row.x + 14, row.y + 6),
                  "small", INK)
        if kind == "errand":
            draw_text(surface, assets, "errand",
                      (row.right - 14, row.y + 6), "tiny", FAINT, right=True)
        draw_text(surface, assets, entry.detail, (row.x + 14, row.y + 26),
                  "tiny", DIM)
        if kind in ("active", "errand"):
            done, target = game.quests.progress(player, entry.key)
            ready = done >= target
            bar(surface, (row.right - 190, row.y + 12, 130, 10),
                done / max(1, target), GOOD if ready else (108, 164, 252))
            draw_text(surface, assets,
                      "READY" if ready else "%d / %d" % (done, target),
                      (row.right - 14, row.y + 10), "tiny",
                      GOOD if ready else DIM, right=True)
        elif kind == "offer":
            blocked = game.quests.full()
            draw_text(surface, assets,
                      "finish your current job first" if blocked
                      else "%d g  %d xp" % (entry.gold, entry.xp),
                      (row.right - 14, row.y + 16), "tiny",
                      FAINT if blocked else ACCENT, right=True)
        else:
            draw_text(surface, assets, "complete", (row.right - 14, row.y + 16),
                      "tiny", GOOD, right=True)

    if readonly:
        hint = "Reading only"
    else:
        hint = {1: "Enter to accept", 2: "Enter to claim when ready",
                3: "Finished work", 4: "Lifetime feats"}[state.quest_tab]
    notice = getattr(state, "quest_notice", None)
    if notice:
        text, tone = notice
        strip = pygame.Rect(rect.x + 30, rect.bottom - 84, rect.w - 60, 34)
        panel(surface, strip, edge=tone, alpha=238)
        draw_text(surface, assets,
                  ellipsize(assets.font("small"), text, strip.w - 20),
                  strip.center, "small", tone, center=True)

    key_hint(surface, assets, [("Tab", "Switch"), ("Enter", hint),
                               ("Esc", "Close")],
             (rect.x + 30, rect.bottom - 28), game=game)


def quests_event(game, event):
    state, player = game.ui, game.player
    if state.quest_readonly and event.key in (pygame.K_RETURN, pygame.K_e):
        state.quest_notice = (
            "Find Elder Maren or one of her runners to take this on.", BAD)
        game.sfx("error")
        return
    if event.key == pygame.K_TAB:
        step = -1 if event.mod & pygame.KMOD_SHIFT else 1
        state.quest_tab = (state.quest_tab + step) % len(QUEST_TABS)
        state.quest.index = state.quest.offset = 0
        game.sfx("move")
        return
    rows = quest_rows(game)
    if event.key in (pygame.K_w, pygame.K_UP):
        state.quest.move(-1, len(rows))
        game.sfx("move")
    elif event.key in (pygame.K_s, pygame.K_DOWN):
        state.quest.move(1, len(rows))
        game.sfx("move")
    elif event.key in (pygame.K_RETURN, pygame.K_e) and rows:
        kind, entry = rows[state.quest.index]
        if kind == "offer":
            if not game.quests.accept(entry.key, player):
                state.quest_notice = (
                    "You are already carrying a job. Finish it first.", BAD)
                game.sfx("error")
                return
            state.quest_notice = ("Accepted: %s" % entry.title, GOOD)
            game.toast("Accepted: %s" % entry.title, GOOD, 3.0)
            game.sfx("quest")
        elif kind == "active":
            reward = game.quests.claim(player, entry.key)
            if not reward:
                done, target = game.quests.progress(player, entry.key)
                state.quest_notice = ("%s - %d of %d done"
                                      % (entry.title, done, target), BAD)
                game.sfx("error")
                return
            gold, xp, ilvl = reward
            player.gold += gold
            player.stats_tracker["gold_earned"] += gold
            levels, new_skills = player.gain_xp(xp)
            game.toast("Reward: %d g, %d xp" % (gold, xp), ACCENT, 3.5)
            if ilvl and not player.bag_full():
                item = roll_item(ilvl, luck=0.35)
                player.add_item(item)
                game.toast("Reward item: %s" % item.name, item.color, 4.0)
            if levels:
                game.toast("Level %d!" % player.level, GOOD, 4.0)
            game.sfx("levelup")
        state.quest.clamp_to(len(quest_rows(game)))


# ------------------------------------------------------ pause / settings ---

PAUSE_ITEMS = ("Resume", "Save game", "Guide", "Codex", "Settings",
               "Back to title", "Quit")


def pause_items(game):
    """The pause menu, with a way out of a room when you are in one.

    **There was no way to leave a room from inside a run.** The lobby is
    only reachable from the title, and "Back to title" did not disconnect -
    so a player who wanted out had to quit the whole game, and anything
    short of that put them straight back in the shared world. Reported in
    the school playtest (§40) as *"unless my friend exited out of the game
    completely, he would join back into the server"*.

    Built per frame rather than kept as a second constant: the row exists
    exactly when `game.coop` does, and two lists would drift.
    """
    if getattr(game, "coop", None) is None:
        return PAUSE_ITEMS
    rows = list(PAUSE_ITEMS)
    rows.insert(rows.index("Back to title"), "Leave room")
    return tuple(rows)


def draw_pause(game):
    overlay(game, 200)
    surface, assets = game.screen, game.assets
    width, height = surface.get_size()
    box_w = min(380, width - 32)
    items = pause_items(game)
    # The list can shrink under the cursor - leaving a room removes a row.
    game.ui.pause_index = min(game.ui.pause_index, len(items) - 1)
    box_h = min(height - 32, 146 + len(items) * 44)
    box = pygame.Rect((width - box_w) // 2, (height - box_h) // 2,
                      box_w, box_h)
    panel(surface, box)
    draw_text(surface, assets, "Paused", (box.centerx, box.y + 34), "big",
              INK, center=True)
    # **Where you are in the story, on the screen you already open.** The
    # chapter card is a moment and moments are missed; this is the line a
    # player can go and look at (s19 item 10). Only the chapter you are in
    # is named - the ones ahead are nowhere (s33).
    chapter = game.chapter()
    if chapter:
        draw_text(surface, assets,
                  ellipsize(assets.font("small"),
                            "Chapter %d of %d - %s"
                            % (chapter.number, story.chapter_total(),
                               chapter.title),
                            box.w - 40),
                  (box.centerx, box.y + 68), "small", ACCENT, center=True)
    # **A guest hero has no slot and no playtime**, and saying "Slot 3" at
    # one would be a straight lie - it is whichever slot they had open last
    # (s19 item 5c). Say whose valley it is instead, which is the true
    # answer to the same question.
    if game.guest_world is not None:
        host = getattr(game.coop, "host_name", "") if game.coop else ""
        where = ("%s's valley" % host) if host else "a friend's valley"
    else:
        where = "Slot %d   %s" % (game.slot, format_playtime(game.playtime))
    draw_text(surface, assets,
              ellipsize(assets.font("tiny"), where, box.w - 40),
              (box.centerx, box.y + 90), "tiny", FAINT, center=True)
    p_top = box.y + 118
    p_start, p_visible, p_pitch = list_view(p_top, box.bottom - 20,
                                            len(items),
                                            game.ui.pause_index, natural=44,
                                            minimum=26)
    for offset in range(p_visible):
        i = p_start + offset
        label = items[i]
        row = pygame.Rect(box.x + 40, p_top + offset * p_pitch, box.w - 80,
                          max(18, p_pitch - 8))
        row_highlight(surface, row, i == game.ui.pause_index, game=game,
                      kind="pause", index=i)
        draw_text(surface, assets, label, row.center, "body",
                  ACCENT if i == game.ui.pause_index else DIM, center=True)
    scroll_note(surface, assets,
                pygame.Rect(box.x + 40, p_top, box.w - 80,
                            p_visible * p_pitch),
                p_start, p_visible, len(items))


def pause_event(game, event):
    state = game.ui
    # Same list the screen drew, so the cursor and the labels cannot
    # disagree about whether there is a Leave room row.
    items = pause_items(game)
    state.pause_index = min(state.pause_index, len(items) - 1)
    if event.key in (pygame.K_w, pygame.K_UP):
        state.pause_index = (state.pause_index - 1) % len(items)
        game.sfx("move")
    elif event.key in (pygame.K_s, pygame.K_DOWN):
        state.pause_index = (state.pause_index + 1) % len(items)
        game.sfx("move")
    elif event.key in (pygame.K_RETURN, pygame.K_e):
        choice = items[state.pause_index]
        game.sfx("select")
        if choice == "Resume":
            game.state = "overworld"
        elif choice == "Save game":
            game.save_run(force=True)
        elif choice == "Guide":
            game.go("guide")
        elif choice == "Codex":
            game.go("codex")
        elif choice == "Settings":
            game.go("settings")
        elif choice == "Leave room":
            # **Leaving may take the world with it.** A guest hero belongs
            # to the host's valley (s19 item 5c) and is left there, so what
            # comes back is your own run - or the title screen, if you
            # joined without one. Either way `leave_coop` has already
            # decided where you are; forcing the overworld here would put a
            # camera on a world that is gone.
            game.leave_coop(resume_own_run=True)
            game.toast("You left the room.", DIM, 3.0)
            if game.player and game.world:
                game.state = "overworld"
            game.sfx("back")
        elif choice == "Back to title":
            game.save_run(silent=True, force=True)
            # **Leaving the run leaves the room.** Without this the socket
            # stayed open and the session stayed live, so going back in put
            # the player straight back into somebody else's world (§40).
            game.leave_coop()
            # **And leaves the run.** The hero and the world used to stay
            # in memory, so the title screen was lying about whether one
            # was in progress - which is what made Host a room skip the
            # save picker (§45).
            game.clear_run()
            game.state = "title"
        elif choice == "Quit":
            game.quit_game()
    elif event.key == pygame.K_ESCAPE:
        game.state = "overworld"
        game.sfx("back")


SETTING_ROWS = ("Master volume", "Sound effects", "Difficulty",
                # **The arena preference** (item 0au). Above the visual
                # toggles because it is a choice about a run rather than
                # about the window.
                "Wave arena", "Arena changes", "Screen shake",
                "Damage numbers", "Autosave", "Show FPS", "Fullscreen",
                "Controls", "Controller", "Touch controls", "Save folder",
                # **Last, above Back, and it asks first** (2026-09-01).
                # Nothing else on this screen destroys anything, so it
                # sits at the bottom where a mis-keyed Enter is least
                # likely to land on it.
                "Erase all data",
                "Back")


def _gamepad_label(game):
    """What the Controller row says on the right.

    Naming the pad matters: the honest failure here is "on, none connected",
    and a row that just said "on" would leave somebody hunting for a fault in
    the game when the pad is asleep.
    """
    pad = getattr(game, "pad", None)
    if pad is None or not pad.enabled:
        return "off"
    return pad.status or "on"


def _save_location_label():
    from . import config as cfg
    if cfg.save_dir_overridden():
        return "set by PIXEL_RPG_SAVE_DIR"
    return cfg.SAVE_LOCATION_LABEL.get(cfg.read_save_location(), "System")


def _arena_note(s):
    """The line under the Arena row: what the setting means, in words."""
    if s.arena_mode == "one":
        return "Every wave in the same place."
    if s.arena_mode == "random":
        return "One place, rolled when the run starts."
    return "The ground changes every %d waves." % s.arena_every


def _arena_label(s):
    """What the Wave arena row says on the right."""
    from .gamemodes import ARENAS
    if s.arena_mode == "one":
        return ARENAS[int(s.arena_pick) % len(ARENAS)].name
    return {"every": "changes", "random": "random each run"}.get(
        s.arena_mode, "changes")


def setting_value(game, label):
    s = game.settings
    return {
        "Master volume": "%d%%" % int(s.master_volume * 100),
        "Sound effects": "on" if s.sfx_enabled else "off",
        "Difficulty": s.difficulty.title(),
        "Wave arena": _arena_label(s),
        "Arena changes": ("every %d waves" % s.arena_every
                          if s.arena_mode == "every" else "-"),
        "Screen shake": "on" if s.screen_shake else "off",
        "Damage numbers": "on" if s.damage_numbers else "off",
        "Autosave": "on" if s.autosave else "off",
        "Show FPS": "on" if s.show_fps else "off",
        "Fullscreen": "on" if s.fullscreen else "off",
        "Controls": "rebind",
        "Controller": _gamepad_label(game),
        "Touch controls": "on" if getattr(game, "touch", None)
                          and game.touch.enabled else "off",
        "Save folder": _save_location_label(),
        "Back": "",
    }.get(label, "")


def _toggle_gamepad(game):
    """Turn controller support on or off, and say what happened.

    Turning it on blocks for about ten seconds while SDL enumerates devices,
    which is why the caller draws a notice first rather than appearing hung.
    """
    from . import gamepad as gamepad_mod
    if game.pad.enabled:
        game.pad.disable()
        game.meta.gamepad = False
        game.meta.save()
        game.toast("Controller support off.", DIM, 3.0)
        game.sfx("back")
        return
    gamepad_mod.draw_searching(game)
    ok = game.pad.enable()
    game.meta.gamepad = bool(ok and game.pad.enabled)
    game.meta.save()
    if not ok:
        game.toast("Controller support could not start: %s" % game.pad.status,
                   BAD, 5.0)
        game.sfx("error")
        return
    found = game.pad.names()
    if found:
        game.toast("Controller ready: %s." % ", ".join(found), ACCENT, 4.0)
        game.sfx("select")
    else:
        game.toast("Controller support on, but no pad is connected. Plug one "
                   "in and it will be picked up.", DIM, 5.0)
        game.sfx("select")


#: How many arenas a row of the picker holds, by how wide the window is.
#: **Thumbnails do not squeeze**: a picture small enough to fit six of on
#: a phone is a picture you cannot tell from the next one, which is the
#: whole point of the screen (item 0br).
def arena_columns(width):
    if width >= 1180:
        return 5
    if width >= 900:
        return 4
    if width >= 640:
        return 3
    return 2


#: The gap between thumbnails, and the room the two buttons take at the
#: bottom of the picker. Named because the drawing, the paging and the
#: scrollbar all have to agree about them.
ARENA_GAP = 12
ARENA_FOOTER = 96
#: The narrowest a thumbnail may get before it stops being a picture of
#: somewhere and becomes a coloured rectangle.
ARENA_MIN_W = 120


def arena_grid(game, rect):
    """Where every thumbnail goes, how big, and which rows this page
    holds. One place, so the drawing, the clicking and the paging cannot
    disagree.

    **Eighteen places do not fit on one screen**, and the first cut of
    this stopped drawing at the bottom edge - so ten arenas were simply
    not there, on the screen whose whole job is showing you *all* of
    them. A picker that hides half its choices is worse than the row of
    text it replaced, because the row at least admitted there were
    eighteen.

    Returns the cells for every arena - off-page ones included, at
    off-page coordinates - so the caller skips them by row rather than
    by testing pixels against an edge, which is the test that failed.
    """
    from .gamemodes import ARENAS
    gap = ARENA_GAP
    # **Under the header wherever the header ended**, never under a
    # constant - `tab_row`'s rule, and for the same reason (s53). On a
    # phone the subtitle wraps to a second line, and 74 put the first row
    # of thumbnails straight through it.
    top = max(rect.y + 74, int(getattr(game.ui, "header_bottom", 0)) + 6)
    room = rect.bottom - ARENA_FOOTER - top + gap

    def measure(columns):
        cell_w = (rect.w - 40 - gap * (columns - 1)) // columns
        cell_h = int(cell_w * 0.62) + 18
        return cell_w, cell_h, max(1, room // (cell_h + gap))

    cols = arena_columns(rect.w)
    cell_w, cell_h, fit = measure(cols)
    # **A grid one row tall is not a grid.** A short window - 900x600, a
    # laptop with the taskbar showing - has width for three columns and
    # height for one row of them, and a picker that shows three of
    # eighteen is the failure this screen was built to fix, arriving
    # again through the other axis. So it takes a narrower column
    # instead, down to the width where a picture stops being one.
    while fit < 2 and cols < 6:
        wider = measure(cols + 1)
        if wider[0] < ARENA_MIN_W:
            break
        cols, (cell_w, cell_h, fit) = cols + 1, wider
    rows = (len(ARENAS) + cols - 1) // cols
    index = int(game.ui.arena_index)
    if index < len(ARENAS):
        here = index // cols
        start = window_start(here, fit, rows, game.ui.arena_row)
        # **`window_start`'s margin is bigger than the page can be.** It
        # keeps a row of context above and below the cursor, which on a
        # two-row page pushes the cursor itself off - so the cursor's own
        # row wins over the margin. A picker whose highlight is not on
        # screen is a picker with no cursor at all.
        start = int(clamp(start, max(0, here - fit + 1), here))
    else:
        # The cursor is on Random or Changes-every, which are below the
        # grid and not in it. **The page does not move** - jumping to the
        # last row because the cursor left the grid is exactly the slide
        # item 0bn was about.
        was = 0 if game.ui.arena_row is None else int(game.ui.arena_row)
        start = int(clamp(was, 0, max(0, rows - fit)))
    game.ui.arena_row = start
    cells = []
    for index in range(len(ARENAS)):
        row, col = divmod(index, cols)
        cells.append(pygame.Rect(rect.x + 20 + col * (cell_w + gap),
                                 top + (row - start) * (cell_h + gap),
                                 cell_w, cell_h))
    return cells, cols, (start, fit, rows)


def draw_arena_select(game):
    """A screen of places, not a row with a name on it (item 0br).

    His words: *"a whole new screen that has pictures of what all the
    arenas look like and a random button at the bottom and the wave
    settings next to it"*.

    **The third position this setting has been in**, and each move was
    him saying the last was wrong: Settings (§74), a run-setup row (§80),
    and now this. The reason is in his first noun - the thing being
    chosen is a *place*, and a name in a list does not show you one.
    """
    from . import freeroamscreen as frs
    from .gamemodes import ARENAS, ARENA_CADENCES
    surface, assets = game.screen, game.assets
    settings = game.settings
    rect = frame(game, "Where you fight",
                 "Enter picks   Esc back" if is_narrow(game.screen.get_rect())
                 else ("Enter picks   Left / Right / Up / Down to look"
                       "   Esc back"))
    cells, cols, (start, fit, rows) = arena_grid(game, rect)
    index = game.ui.arena_index

    for n, (arena, cell) in enumerate(zip(ARENAS, cells)):
        if not start <= n // cols < start + fit:
            # Off this page. **Skipped by row, not by pixel** - the cell
            # still has a place, it is simply not on screen, and no
            # hotspot is registered for something nobody can see.
            continue
        chosen = settings.arena_mode == "one" and settings.arena_pick == n
        shot = frs.arena_thumbnail(assets, (cell.w, cell.h - 18), arena)
        surface.blit(shot, (cell.x, cell.y))
        draw_text(surface, assets,
                  ellipsize(assets.font("tiny"), arena.name, cell.w - 8),
                  (cell.centerx, cell.bottom - 9), "tiny",
                  ACCENT if chosen else DIM, center=True)
        if n == index:
            pygame.draw.rect(surface, ACCENT, cell.inflate(6, 6), 2)
        elif chosen:
            pygame.draw.rect(surface, GOOD, cell.inflate(4, 4), 2)
        game.ui.hot.add(cell, "arena", n)

    # **The two buttons, on the same screen and side by side** - his
    # *"a random button at the bottom and the wave settings next to
    # it"*. They are choices about the same thing, so they live where
    # the thing is rather than one screen away.
    if cells:
        scrollbar(surface, pygame.Rect(rect.x + 20, rect.y + 74, rect.w - 46,
                                       fit * (cells[0].h + ARENA_GAP)
                                       - ARENA_GAP),
                  start, fit, rows)
    total = len(ARENAS)
    wide = min(260, (rect.w - 60) // 2)
    row_y = rect.bottom - 62
    random_rect = pygame.Rect(rect.x + 20, row_y, wide, 34)
    every_rect = pygame.Rect(random_rect.right + 16, row_y, wide, 34)
    for slot, (box, label, on) in enumerate((
            (random_rect, "Random each run", settings.arena_mode == "random"),
            (every_rect, "Changes every %d waves" % settings.arena_every,
             settings.arena_mode == "every"))):
        row_highlight(surface, box, index == total + slot, ACCENT,
                      game=game, kind="arena", index=total + slot,
                      chosen=on)
        draw_text(surface, assets,
                  ellipsize(assets.font("small"),
                            ("* " if on else "") + label, box.w - 20),
                  (box.centerx, box.centery), "small",
                  ACCENT if on else INK, center=True)
    note = "Pick a place and every wave is fought there."
    if rows > fit:
        # **Where you are, not how many fit.** "8 of 18 shown" answers a
        # question nobody asked; the range answers "is there more below".
        first = start * cols + 1
        note = "%d-%d of %d   %s" % (
            first, min(len(ARENAS), (start + fit) * cols), len(ARENAS), note)
    draw_text(surface, assets,
              ellipsize(assets.font("tiny"), note, rect.w - 40),
              (rect.centerx, rect.bottom - 22), "tiny", FAINT, center=True)
    assert ARENA_CADENCES


def draw_settings(game):
    overlay(game, 210)
    surface, assets = game.screen, game.assets
    width, height = surface.get_size()
    # Height follows the row count, with room reserved for the footer, so
    # adding a setting can never push the last row onto the explainer. On a
    # short window the panel shrinks and `list_view` scrolls the rows inside
    # it - the rows used to march out through the bottom of the panel that
    # had already shrunk to fit, which is section 28's whole story.
    rows_h = len(SETTING_ROWS) * 40
    box_h = min(height - 40, 96 + rows_h + 74)
    box = pygame.Rect(max(12, width // 2 - 240), (height - box_h) // 2,
                      min(480, width - 24), box_h)
    panel(surface, box)
    draw_text(surface, assets, "Settings", (box.centerx, box.y + 34), "big",
              INK, center=True)
    draw_text(surface, assets, "Left / Right to change",
              (box.centerx, box.y + 68), "tiny", FAINT, center=True)

    top = box.y + 96
    footer = 74 if box_h >= 96 + rows_h + 74 else 40
    start, visible, pitch = list_view(top, box.bottom - footer,
                                      len(SETTING_ROWS),
                                      game.ui.settings_index)
    last = top
    for offset in range(visible):
        i = start + offset
        label = SETTING_ROWS[i]
        row = pygame.Rect(box.x + 30, top + offset * pitch, box.w - 60,
                          max(18, pitch - 6))
        last = row.bottom
        row_highlight(surface, row, i == game.ui.settings_index,
                      game=game, kind="setting", index=i)
        # Left-aligned text anchors on its top, right-aligned on its middle
        # (section 7). So the label is nudged and the value is not.
        draw_text(surface, assets, label,
                  (row.x + 18, row.y + max(1, (row.h - 16) // 2)), "small",
                  INK if i == game.ui.settings_index else DIM)
        value = setting_value(game, label)
        if value:
            # The label is drawn first and the value is right-aligned, so on
            # a narrow row they walk into each other rather than clipping.
            # The layout checker forgives overlaps under 22% as kerning and
            # let this through; the screenshot did not (section 16).
            font = assets.font("small")
            room = row.w - 66 - font.size(label)[0]
            while value and font.size(value)[0] > room:
                value = value[:-1]
            if value != setting_value(game, label):
                value = (value[:-1] + "…") if len(value) > 1 else ""
            if value:
                draw_text(surface, assets, value,
                          (row.right - 24, row.y + row.h // 2), "small",
                          ACCENT, right=True)
    scroll_note(surface, assets,
                pygame.Rect(box.x + 30, top, box.w - 60, last - top),
                start, visible, len(SETTING_ROWS))

    if footer < 74:
        return                      # no room for the explainer; drop it
    from . import config as cfg
    where = str(cfg.default_save_dir())
    font = assets.font("tiny")
    while where and font.size(where)[0] > box.w - 32:
        where = where[1:]
    if where != str(cfg.default_save_dir()):
        where = "..." + where[3:]
    draw_text(surface, assets, where, (box.centerx, box.bottom - 62), "tiny",
              FAINT, center=True)
    for i, line in enumerate(wrap_text(
            "Difficulty changes enemy health, damage and rewards.",
            assets.font("tiny"), box.w - 40)):
        draw_text(surface, assets, line,
                  (box.centerx, box.bottom - 42 + i * 15), "tiny",
                  FAINT, center=True)


# The keys this screen refuses to hear, so the konami sequence has somewhere
# to live where it cannot change anything on the way in (§19 decision 6).
# Savvydev: "A B and arrow keys should not be able to interact with the
# settings menu (this ONLY applies to the settings menu) and only interact
# with Konami code."
#
# **`A` was "decrease value" here**, which is the contradiction inside that
# instruction: WASD cannot drive this screen while `A` is reserved. Of the
# thirteen rows only Master volume genuinely decrements - the rest are
# toggles, short cycles or actions - so `D`/Enter now advance every row and
# the volume wraps at the top. One row got slightly worse and the sequence
# got a home.
KONAMI_KEYS = (pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT,
               pygame.K_a, pygame.K_b)


# A pad and a thumbstick both publish arrow keys (§14, §28), and this screen
# is deaf to arrows. Deafening them too would leave a controller unable to
# use the settings menu at all - which is exactly what happened, and what the
# smoke check caught. They are translated to the WASD this screen speaks
# instead. Left has no equivalent: decrement is gone with `A`.
SYNTHETIC_ARROWS = {pygame.K_UP: pygame.K_w, pygame.K_DOWN: pygame.K_s,
                    pygame.K_RIGHT: pygame.K_d}


def settings_event(game, event):
    state, s = game.ui, game.settings
    if getattr(event, "synthetic", False):
        key = SYNTHETIC_ARROWS.get(event.key, event.key)
    elif event.key in KONAMI_KEYS:
        return                      # deaf on purpose; see KONAMI_KEYS
    else:
        key = event.key

    if key == pygame.K_w:
        state.settings_index = (state.settings_index - 1) % len(SETTING_ROWS)
        game.sfx("move")
        return
    if key == pygame.K_s:
        state.settings_index = (state.settings_index + 1) % len(SETTING_ROWS)
        game.sfx("move")
        return

    if key == pygame.K_ESCAPE:
        game.state = "pause" if game.player else "title"
        game.sfx("back")
        return

    label = SETTING_ROWS[state.settings_index]
    # Forward only. `A` and the arrows are gone (see KONAMI_KEYS), so there
    # is no decrement key left - every row advances and wraps instead.
    step = 1 if key == pygame.K_d else 0
    confirm = key in (pygame.K_RETURN, pygame.K_e)

    if label == "Controls" and (confirm or step):
        game.ui.keybind_index = 0
        game.ui.rebinding = None
        game.ui.keybind_notice = ""
        game.go("keybinds")
        game.sfx("select")
        return
    if label == "Controller" and (confirm or step):
        _toggle_gamepad(game)
        return
    if label == "Touch controls" and (confirm or step):
        # Nothing to enumerate and nothing to wait for, so unlike the pad
        # this is just a flag. It turns itself on in a browser; the row is
        # here so a desktop can try it and a phone can turn it off.
        game.touch.enabled = not game.touch.enabled
        game.touch.held = set()
        game.touch.stick_finger = None
        game.meta.touch = game.touch.enabled
        game.meta.save()
        game.toast("Touch controls %s."
                   % ("on" if game.touch.enabled else "off"),
                   ACCENT if game.touch.enabled else DIM, 3.0)
        game.sfx("select" if game.touch.enabled else "back")
        return
    if label == "Save folder" and (confirm or step):
        from . import config as cfg
        if cfg.save_dir_overridden():
            game.toast("PIXEL_RPG_SAVE_DIR is set, so it decides where saves "
                       "go. Clear it to use this setting.", BAD, 6.0)
            game.sfx("error")
            return
        current = cfg.read_save_location()
        index = (cfg.SAVE_LOCATIONS.index(current)
                 + (step or 1)) % len(cfg.SAVE_LOCATIONS)
        choice = cfg.SAVE_LOCATIONS[index]
        if cfg.write_save_location(choice):
            game.settings.save_dir = cfg.default_save_dir()
            game.rebind_save_dir()
            game.toast("Saves now go to the %s. Existing saves stay where "
                       "they are." % cfg.SAVE_LOCATION_LABEL[choice].lower(),
                       ACCENT, 6.0)
            game.sfx("select")
        else:
            game.toast("Could not write the save-location file.", BAD, 4.0)
            game.sfx("error")
        return
    if label == "Back" and confirm:
        game.state = "pause" if game.player else "title"
        game.sfx("back")
        return
    if label == "Master volume" and step:
        # **Wraps rather than clamping.** With `A` and the arrows given
        # to the konami sequence there is no decrement key left, so a
        # volume that stopped at 1.0 could never come back down. Round
        # before comparing: 0.05 steps do not land exactly on 1.0.
        nxt = round(s.master_volume + step * 0.05, 2)
        s.master_volume = 0.0 if nxt > 1.0 else nxt
        game.audio.set_volume(s.master_volume)
        game.sfx("move")
    elif label == "Sound effects" and (step or confirm):
        s.sfx_enabled = game.audio.toggle()
        game.sfx("select")
    elif label == "Difficulty" and (step or confirm):
        s.cycle_difficulty(step or 1)
        game.sfx("move")
    elif label == "Wave arena" and (step or confirm):
        from .gamemodes import ARENA_MODES, ARENAS
        if s.arena_mode == "one" and step:
            # Inside "one place", the arrows walk the eighteen places -
            # which is what *"they can pick an arena"* means. It leaves
            # the mode when it runs off either end, so the whole setting
            # is still one row and one pair of keys.
            nxt = s.arena_pick + step
            if 0 <= nxt < len(ARENAS):
                s.arena_pick = nxt
                game.sfx("move")
                return
        idx = ARENA_MODES.index(s.arena_mode) if s.arena_mode in ARENA_MODES \
            else 0
        s.arena_mode = ARENA_MODES[(idx + (step or 1)) % len(ARENA_MODES)]
        game.sfx("move")
    elif label == "Arena changes" and (step or confirm):
        from .gamemodes import ARENA_CADENCES
        if s.arena_mode != "every":
            game.sfx("error")
            return
        idx = (ARENA_CADENCES.index(s.arena_every)
               if s.arena_every in ARENA_CADENCES else 0)
        s.arena_every = ARENA_CADENCES[(idx + (step or 1))
                                       % len(ARENA_CADENCES)]
        game.sfx("move")
    elif label == "Screen shake" and (step or confirm):
        s.screen_shake = not s.screen_shake
        game.sfx("move")
    elif label == "Damage numbers" and (step or confirm):
        s.damage_numbers = not s.damage_numbers
        game.sfx("move")
    elif label == "Autosave" and (step or confirm):
        s.autosave = not s.autosave
        game.sfx("move")
    elif label == "Erase all data" and confirm:
        # Only on a confirm - Left and Right must not start this.
        game.ask_erase_everything()
    elif label == "Show FPS" and (step or confirm):
        s.show_fps = not s.show_fps
        game.sfx("move")
    elif label == "Fullscreen" and (step or confirm):
        game.toggle_fullscreen()
        game.sfx("move")


# ----------------------------------------------------- dialogue / defeat ---

def dialogue_measure(game):
    """The dialogue box, its wrapped body, and how many lines fit.

    **Split out for the same reason `scene_measure` was** (2026-08-30, and
    this half on the 31st): the draw did `wrapped = wrapped[:max_lines]`,
    which throws prose away without overflowing anything, so
    `tests/layout.py` cannot see it. At 390 high it kept **three lines** -
    and the docstring below it says wardens say long, uneven things, which
    is exactly what was being cut.

    `event_dialogue` needs the same numbers to clamp scrolling.

    Returns `(box, wrapped, capacity, line_h, head_h)`.
    """
    surface, assets = game.screen, game.assets
    width, height = surface.get_size()
    data = game.dialogue or {}

    box_w = min(720, width - 120)
    text_w = box_w - 72
    font = assets.font("small")

    wrapped = []
    for line in data.get("body", []):
        if not line:
            wrapped.append("")
        else:
            wrapped.extend(wrap_text(line, font, text_w))

    head_h, foot_h, line_h = 88, 74, 25
    capacity = max(1, (height - 140 - head_h - foot_h) // line_h)
    shown = min(len(wrapped), capacity)
    box_h = head_h + shown * line_h + foot_h
    box = pygame.Rect(width // 2 - box_w // 2, (height - box_h) // 2,
                      box_w, box_h)
    return box, wrapped, capacity, line_h, head_h


def dialogue_scroll_max(game):
    """How far the dialogue body can be pushed down. Zero when it fits."""
    _box, wrapped, capacity, _line_h, _head = dialogue_measure(game)
    return max(0, len(wrapped) - capacity)


def draw_dialogue(game):
    """Story box. Sizes itself to the text so nothing ever overlaps.

    Wardens say long, uneven things, so the body is wrapped to the box width
    and the panel height is computed from the wrapped result rather than
    guessed at.

    **And it scrolls when the window is too short for all of it**, on the
    same keys and with the same hint as the scene screen (s43) - one idiom,
    not two. It used to keep the first few lines and drop the rest in
    silence.
    """
    overlay(game, 190)
    surface, assets = game.screen, game.assets
    data = game.dialogue or {}

    box, wrapped, capacity, line_h, head_h = dialogue_measure(game)
    top = clamp(int(data.get("scroll", 0)), 0, max(0, len(wrapped) - capacity))
    data["scroll"] = top
    panel(surface, box, edge=(226, 96, 96))

    # **A dialogue title is whoever is speaking, and the world makes some
    # long ones** - "A woman of Cobb's Rest", "Pell, Maren's runner". It
    # was drawn at full length in the big font with nothing bounding it,
    # so on a 390-wide window it left the panel and then the screen. The
    # confirm button below has given way to its own label since s29; the
    # title does the same now.
    draw_text(surface, assets,
              ellipsize(assets.font("big"), data.get("title", ""),
                        box.w - 48),
              (box.centerx, box.y + 30), "big", (250, 200, 200), center=True)
    if len(wrapped) > capacity:
        draw_text(surface, assets,
                  "%d-%d of %d   Left and Right to read on"
                  % (top + 1, min(top + capacity, len(wrapped)),
                     len(wrapped)),
                  (box.centerx, box.y + 60), "tiny", FAINT, center=True)
    y = box.y + head_h
    for i, line in enumerate(wrapped[top:top + capacity]):
        if line:
            draw_text(surface, assets, line, (box.centerx, y), "small",
                      INK if (i or top) else DIM, center=True)
        y += line_h
    # Both choices are buttons, so the mouse can answer a dialogue.
    confirm_label = data.get("confirm", "Continue")
    # Two buttons side by side need the box's width between them. A long
    # confirm label - "Draw your weapon" - pushed its own button off the
    # left-hand edge, so the label gives way rather than the button.
    # The two are split *at* the centre, not centred as a pair, so each one
    # gets half the box and not the whole of it.
    back_w = min(132, max(40, box.right - box.centerx - 24))
    confirm_w = min(assets.font("small").size(confirm_label)[0] + 74,
                    max(40, box.centerx - box.x - 24))
    confirm_label = ellipsize(assets.font("small"), confirm_label,
                              confirm_w - 74)
    confirm_rect = pygame.Rect(box.centerx - confirm_w - 8, box.bottom - 50,
                               max(40, confirm_w), 34)
    back_rect = pygame.Rect(box.centerx + 8, box.bottom - 50, back_w, 34)
    for rect, text, kind, tone in (
            (confirm_rect, "Enter  %s" % confirm_label, "dialogue_ok", ACCENT),
            (back_rect, "Esc  Back", "dialogue_back", DIM)):
        game.ui.hot.add(rect, kind, 0)
        if game.hover == (kind, 0):
            highlight(surface, rect, tone, fill_alpha=40, border=2)
        else:
            highlight(surface, rect, tone, fill_alpha=0, border=1)
        draw_text(surface, assets, text, rect.center, "small", tone,
                  center=True)


def _skill_cost(player, skill):
    """What an ability costs, outside a battle.

    `Battle.skill_cost_text` needs an actor and a fight; a skill can be
    learned from a favour or an errand with neither, so the arithmetic is
    repeated here against the player rather than reached for through a
    battle that may not exist.
    """
    from .skills import HP_COST_SKILLS
    fraction = HP_COST_SKILLS.get(skill.key)
    if fraction and player is not None:
        return "%d HP" % int(player.max_hp() * fraction)
    return "%d MP" % skill.mp


def draw_new_skill(game):
    """"You learned something. It does not fit. Which one goes?"

    **Roadmap item 0ac** (s53). Savvydev, third school playtest: *"maybe add
    a pop up window asking you if you want to replace a skill with the one
    you just learned and what that skill does"*.

    Two asks, and the second is the one that makes the screen worth having
    over a toast: **it says what the new skill does, beside what it would
    replace.** A prompt that only names them is asking you to remember a
    description you have never read.
    """
    from .skills import SKILLS_BY_KEY
    overlay(game, 228)
    surface, assets = game.screen, game.assets
    state = game.ui.newskill or {}
    skill = state.get("skill")
    if skill is None:
        return
    rect = frame(game, "You learned %s" % skill.name,
                 "Your hands are full. Choose what it replaces, "
                 "or leave it behind.")

    carried = [SKILLS_BY_KEY[k] for k in state.get("carried", ())
               if k in SKILLS_BY_KEY]
    picked = int(clamp(getattr(game.ui, "newskill_index", 0), 0,
                       max(0, len(carried) - 1)))

    roomy = rect.w >= 760
    left = rect.x + 34
    top = rect.y + 96
    list_w = min(430, (rect.w - 100) // 2) if roomy else rect.w - 68

    # What you just learned, first and in its own colour: it is the thing
    # the screen is about, and on a narrow window it is all that fits.
    card = pygame.Rect(left, top, list_w, 74)
    panel(surface, card, alpha=232, edge=(192, 118, 250))
    row_text(surface, assets,
             ellipsize(assets.font("small"), skill.name, card.w - 90),
             pygame.Rect(card.x, card.y, card.w, 26), "small", (192, 118, 250))
    row_text(surface, assets, _skill_cost(game.player, skill),
             pygame.Rect(card.x, card.y, card.w, 26), "tiny", DIM, right=True)
    for i, line in enumerate(wrap_text(skill.description,
                                       assets.font("tiny"), card.w - 24)[:2]):
        draw_text(surface, assets, line, (card.x + 12, card.y + 30 + i * 15),
                  "tiny", INK)

    # Clear of the card's bottom edge, not on it: the card is 74 tall from
    # `top`, and a heading at 86 - 18 lands on its border.
    list_top = top + 104
    draw_text(surface, assets, "PUT DOWN", (left, list_top - 20), "tiny",
              FAINT)
    start, visible, row_h = list_view(list_top, rect.bottom - 78,
                                      len(carried), picked, natural=30,
                                      minimum=22)
    for offset in range(visible):
        i = start + offset
        if i >= len(carried):
            break
        old = carried[i]
        row = pygame.Rect(left, list_top + offset * row_h, list_w,
                          row_height(assets, "small", row_h, pad=4))
        row_highlight(surface, row, i == picked, ACCENT,
                      game=game, kind="newskill", index=i)
        row_text(surface, assets,
                 ellipsize(assets.font("small"), old.name, list_w - 90),
                 row, "small", INK if i == picked else DIM)
        row_text(surface, assets, _skill_cost(game.player, old), row, "tiny",
                 FAINT, right=True)
    scroll_note(surface, assets,
                pygame.Rect(left, list_top, list_w, visible * row_h),
                start, min(visible, len(carried)), len(carried))

    # Beside the list when there is room: what the highlighted one does, so
    # the trade is legible without remembering either description.
    if roomy and carried:
        side = pygame.Rect(left + list_w + 26, top,
                           rect.right - 34 - (left + list_w + 26), 160)
        panel(surface, side, alpha=228)
        row_text(surface, assets, carried[picked].name,
                 pygame.Rect(side.x, side.y, side.w, 28), "small", ACCENT)
        for i, line in enumerate(wrap_text(carried[picked].description,
                                           assets.font("tiny"),
                                           side.w - 24)[:5]):
            draw_text(surface, assets, line,
                      (side.x + 12, side.y + 34 + i * 15), "tiny", DIM)

    key_hint(surface, assets,
             [("Enter", "Swap it in"), ("Esc", "Leave it")],
             (left, rect.bottom - 28), game=game)


def draw_full_bag(game):
    """"Your bag is full" as a question rather than an announcement.

    Savvydev, 2026-09-01: *"you should be granted with a screen asking you
    if you want to keep the item you just got and toss out another one or
    toss out the item you just got instead of immediately discarding it
    for you"*.

    **Equipped gear is not in the list.** Dropping what you are wearing to
    make room is a decision with a second half - now you are wearing
    nothing - and the bag screen is where that belongs.
    """
    overlay(game, 226)
    surface, assets, player = game.screen, game.assets, game.player
    state = game.ui.fullbag or {}
    item = state.get("item")
    if item is None:
        return
    width, height = surface.get_size()
    # **The sentence gives way before the numbers do.** On a phone the
    # long version wraps to two lines and lands on the stat line below it;
    # the item is named in the card (or in that stat line) either way.
    rect = frame(game, "Your bag is full",
                 ("%s does not fit. Something has to give." % item.name)
                 if surface.get_width() >= 760 else "Something has to give.")

    pool = [i for i in player.inventory
            if player.equipment.get(i.slot) is not i]
    left = rect.x + 34
    top = rect.y + 96
    # **One column when two will not fit** (s29's rule, s48's screen). A
    # card squeezed into 119 pixels does not shrink, it overflows - which
    # is what `layout.py` caught at 390 wide. Below the threshold the list
    # takes the whole frame and the header carries the comparison.
    roomy = rect.w >= 760
    list_w = min(430, (rect.w - 100) // 2) if roomy else rect.w - 68
    picked = int(getattr(game.ui, "fullbag_index", 0))
    if pool:
        picked = max(0, min(picked, len(pool) - 1))
    start, visible, row_h = list_view(top, rect.bottom - 78, len(pool),
                                      picked, natural=30, minimum=22)
    for offset in range(visible):
        i = start + offset
        if i >= len(pool):
            break
        worn = pool[i]
        row = pygame.Rect(left, top + offset * row_h, list_w,
                          max(16, row_h - 4))
        row_highlight(surface, row, i == picked, worn.color,
                      game=game, kind="fullbag", index=i)
        row_text(surface, assets,
                 ellipsize(assets.font("small"), worn.name, list_w - 90),
                 row, "small", INK if i == picked else DIM, pad=12)
        row_text(surface, assets, "%d" % int(worn.power()), row, "tiny",
                 FAINT, pad=12, right=True)
    scroll_note(surface, assets,
                pygame.Rect(left, top, list_w, visible * row_h),
                start, min(visible, len(pool)), len(pool))

    # The thing you just found, against the thing you would drop for it.
    chosen = pool[picked] if pool else None
    if roomy:
        item_card(surface, assets, item, (left + list_w + 26, top),
                  equipped=chosen, width=min(330, rect.right - 34
                                             - (left + list_w + 26)),
                  player=player)
    else:
        # No room for the card: the one line that matters instead, under
        # the title where the item is already named.
        # Under the subtitle, not on it: the frame's own second line wraps
        # to two on a narrow window, and this landed on top of it.
        draw_text(surface, assets,
                  ellipsize(assets.font("tiny"), item.stat_line(),
                            rect.w - 68),
                  (left, top - 18), "tiny", item.color)

    key_hint(surface, assets,
             [("Enter", "Drop that one, keep this"),
              ("Esc", "Leave it behind")],
             (left, rect.bottom - 28), game=game)


def _offer_panel(surface, assets, rect, title, offer, locked, tone):
    """One side's half of the table, drawn as a list of what is on it.

    Both halves use this, which is the point: the offer you are making and
    the offer you are being made have to be equally easy to read, or the
    screen is only honest in one direction.
    """
    panel(surface, rect, alpha=232,
          edge=tone if locked else (90, 90, 104))
    status = "AGREED" if locked else "not agreed"
    # **Beside the name, or under it - never merged into it.** Side by side
    # these panels are 129 pixels wide on a phone in portrait, which is not
    # enough for a name and a verdict on one line; `layout.py` caught them
    # overlapping by a third. The first version of the fix ran them
    # together and ellipsized the result, which cut the *verdict* - so a
    # rule about fitting quietly deleted the thing the panel is for.
    draw_text(surface, assets,
              ellipsize(assets.font("small"), title, rect.w - 20),
              (rect.x + 10, rect.y + 8), "small", tone)
    if (assets.font("small").size(title)[0]
            + assets.font("tiny").size(status)[0] + 28) <= rect.w:
        draw_text(surface, assets, status, (rect.right - 10, rect.y + 10),
                  "tiny", GOOD if locked else FAINT, right=True)
        first = rect.y + 32
    else:
        draw_text(surface, assets, status, (rect.right - 10, rect.y + 32),
                  "tiny", GOOD if locked else FAINT, right=True)
        first = rect.y + 48
    lines = offer.lines()
    room = max(0, (rect.bottom - 12 - first) // 17)
    if not lines:
        draw_text(surface, assets, "nothing yet", (rect.x + 10, first),
                  "tiny", FAINT)
    for i, (label, colour) in enumerate(lines[:room]):
        draw_text(surface, assets,
                  ellipsize(assets.font("tiny"), label, rect.w - 20),
                  (rect.x + 10, first + i * 17), "tiny", colour)
    if len(lines) > room:
        draw_text(surface, assets, "+%d more" % (len(lines) - room),
                  (rect.right - 10, rect.bottom - 18), "tiny", FAINT,
                  right=True)


def draw_trade(game):
    """Two people, two offers, and nothing moves until both say yes.

    **Roadmap item 0x** - decided 2026-08-24 (*"no limits. Any gold, any
    gear"*) and asked about again on 2026-09-01 because it had never been
    built. `trade.py` holds the rules; this is the table.

    The left column is everything you could put on the table and the right
    is the table itself, both halves of it. **Changing anything un-agrees
    both sides**, which is why the two AGREED marks are drawn where a
    change can be seen to clear them.
    """
    overlay(game, 226)
    surface, assets, player = game.screen, game.assets, game.player
    session = game.trade
    if session is None or player is None:
        return
    rect = frame(game, "Trading with %s" % session.partner_name,
                 session.note or
                 "Nothing changes hands until you both agree.")
    rows = game.trade_rows()
    index = max(0, min(int(session.index), max(0, len(rows) - 1)))

    # **One column when two will not fit** (s29's rule, s48's screen). The
    # offers are what you cannot do without, so below the threshold they
    # become a band across the top and the list takes the rest.
    roomy = rect.w >= 760
    left = rect.x + 28
    top = rect.y + 96
    if roomy:
        list_w = min(400, (rect.w - 84) * 3 // 5)
        col_x = left + list_w + 24
        col_w = rect.right - 28 - col_x
        half = max(74, (rect.bottom - 96 - top) // 2 - 8)
        _offer_panel(surface, assets,
                     pygame.Rect(col_x, top, col_w, half),
                     "You give", session.mine, session.mine_locked, ACCENT)
        _offer_panel(surface, assets,
                     pygame.Rect(col_x, top + half + 12, col_w, half),
                     "%s gives" % session.partner_name, session.theirs,
                     session.theirs_locked, GOOD)
        list_top = top
    else:
        list_w = rect.w - 56
        # **The band takes what the list does not need.** A fixed 76 showed
        # one line of each offer and "+3 more" on a phone in portrait,
        # where there were four hundred spare pixels underneath - and what
        # the other person is putting up is the whole reason to look at
        # this screen. Eight rows is as much list as anyone needs at once.
        spare = (rect.bottom - 92) - top
        need = min(len(rows), 8) * 24 + 8
        band = int(clamp((spare - need) // 2 - 6, 60, 210))
        gap = (list_w - 12) // 2
        _offer_panel(surface, assets, pygame.Rect(left, top, gap, band),
                     "You give", session.mine, session.mine_locked, ACCENT)
        _offer_panel(surface, assets,
                     pygame.Rect(left + gap + 12, top, gap, band),
                     "They give", session.theirs, session.theirs_locked,
                     GOOD)
        list_top = top + band + 12

    list_bottom = rect.bottom - 92
    start, visible, row_h = list_view(list_top, list_bottom, len(rows),
                                      index, natural=28, minimum=20)
    for offset in range(visible):
        i = start + offset
        if i >= len(rows):
            break
        row = rows[i]
        box = pygame.Rect(left, list_top + offset * row_h, list_w,
                          max(15, row_h - 4))
        label, right_label, colour, chosen = _trade_row_text(game, row)
        row_highlight(surface, box, i == index, colour, game=game,
                      kind="trade", index=i, chosen=chosen)
        row_text(surface, assets,
                 ellipsize(assets.font("small"), label, list_w - 96),
                 box, "small", INK if i == index else DIM)
        if right_label:
            row_text(surface, assets, right_label, box, "tiny",
                     colour if chosen else FAINT, right=True)
    scroll_note(surface, assets,
                pygame.Rect(left, list_top, list_w, visible * row_h),
                start, min(visible, len(rows)), len(rows))

    # The Confirm button commits the highlighted row, exactly as it does in
    # the shop - `trade` is in `SELECT_ONLY` for the reason recorded there.
    button = pygame.Rect(left, rect.bottom - 78, min(260, list_w), 34)
    row = rows[index] if rows else {"kind": "cancel"}
    confirm_button(surface, assets, game, button,
                   _trade_confirm_label(game, row))
    key_hint(surface, assets,
             [("Left/Right", "Amount"), ("Shift", "One at a time"),
              ("Esc", "Call it off")],
             (left, rect.bottom - 30), game=game)


def _trade_row_text(game, row):
    """(label, right-hand label, colour, is it on the table)."""
    kind = row["kind"]
    if kind == "gold":
        return ("Gold", "%d / %d" % (row["offered"], row["have"]),
                (250, 216, 128), bool(row["offered"]))
    if kind == "item":
        # **"worn" goes on the right, not on the end of the name.** As a
        # suffix it was the first thing the ellipsis ate, so the longest
        # names - which is to say the best gear - were the ones that never
        # said they were the sword you are holding.
        item = row["item"]
        return (item.name,
                ("worn  %d" if row["worn"] else "%d") % int(item.power()),
                item.color, bool(row["offered"]))
    if kind == "cons":
        spec = CONSUMABLES.get(row["key"])
        return (spec.name if spec else row["key"],
                "%d / %d" % (row["offered"], row["have"]),
                spec.color if spec else DIM, bool(row["offered"]))
    session = game.trade
    if kind == "lock":
        if session is not None and session.mine_locked:
            # The row says the state; the button below says what pressing
            # it would do. A row that tried to say both was the first thing
            # the ellipsis reached on a narrow window.
            return ("You have agreed", "", GOOD, True)
        return ("Agree to this trade", "", ACCENT, False)
    return ("Call the trade off", "", BAD, False)


def _trade_confirm_label(game, row):
    kind = row["kind"]
    session = game.trade
    if kind == "lock":
        return ("Take back your agreement"
                if session is not None and session.mine_locked else "Agree")
    if kind == "cancel":
        return "Call it off"
    if kind == "item":
        return "Take it off the table" if row["offered"] else "Put it up"
    return "Take it off the table" if row["offered"] else "Offer all"


def draw_gameover(game):
    overlay(game, 220)
    surface, assets = game.screen, game.assets
    width, height = surface.get_size()
    draw_text(surface, assets, "YOU FELL", (width // 2, height // 2 - 60),
              "huge", (236, 92, 96), center=True)
    draw_text(surface, assets, game.message, (width // 2, height // 2 + 10),
              "body", DIM, center=True)
    draw_text(surface, assets, "Press Enter, or click, to wake at the shrine",
              (width // 2, height // 2 + 60), "small", ACCENT, center=True)
    # **The whole screen is the button** (2026-08-31). There is one thing
    # to do here and no cursor to move, so anywhere is the right place to
    # click; it replays Enter rather than calling `respawn` itself.
    game.ui.hot.add(surface.get_rect(), "key", pygame.K_RETURN)


# ------------------------------------------------------------- dispatcher ---

def handle_event(game, event):
    """Route a keydown to the active screen. Esc always steps back."""
    handlers = {
        "inventory": inventory_event,
        "shop": shop_event,
        "smith": smith_event,
        "character": character_event,
        "quests": quests_event,
        "pause": pause_event,
        "settings": settings_event,
    }
    handler = handlers.get(game.state)
    if handler is None:
        return
    if event.key == pygame.K_ESCAPE and game.state not in ("pause", "settings"):
        game.state = "overworld"
        game.sfx("back")
        return
    handler(game, event)


# ----------------------------------------------------------- class select ---

def draw_class_select(game):
    surface, assets = game.screen, game.assets
    rect = frame(game, "Choose your road",
                 "Up and Down to browse   Enter to take it   Esc to go back")
    from . import classes as classes_mod

    # The list keeps a third of the frame at most, so the card beside it
    # always has somewhere to be. `panel_w` below used to be worked out by
    # subtraction and went negative on a narrow window, which is the crash
    # section 28 found.
    # One column when there is no room for a list beside a card. The card
    # is the content here - it is what you are choosing between - so the
    # list becomes a name and a counter, exactly as the story record does.
    narrow = is_narrow(rect)
    list_w = 0 if narrow else int(clamp(rect.w // 3, 150, 250))
    list_x = rect.x + 34
    top = rect.y + (128 if narrow else 96)

    start, visible, row_h = list_view(top, rect.bottom - 40,
                                      len(classes_mod.CLASSES),
                                      game.ui.class_index, natural=46,
                                      minimum=28)
    two_lines = row_h >= 40
    if narrow:
        chosen = classes_mod.CLASSES[game.ui.class_index]
        head = pygame.Rect(rect.x + 34, rect.y + 92, rect.w - 68, 30)
        game.ui.hot.add(head, "class", game.ui.class_index)
        draw_text(surface, assets, chosen.name, (head.x + 4, head.y + 6),
                  "small", chosen.color)
        draw_text(surface, assets,
                  "%d of %d" % (game.ui.class_index + 1,
                                len(classes_mod.CLASSES)),
                  (head.right - 4, head.y + 15), "tiny", DIM, right=True)
        visible = 0
    for offset in range(visible):
        i = start + offset
        cls = classes_mod.CLASSES[i]
        unlocked = classes_mod.is_unlocked(cls.key, game.meta)
        row = pygame.Rect(list_x, top + offset * row_h, list_w,
                          max(18, row_h - 6))
        row_highlight(surface, row, i == game.ui.class_index, cls.color,
                      game=game, kind="class", index=i)
        draw_text(surface, assets, cls.name if unlocked else "? ? ?",
                  (row.x + 14, row.y + (6 if two_lines
                                        else max(1, (row.h - 19) // 2))),
                  "body", cls.color if unlocked else FAINT)
        if unlocked and cls.archetype:
            # "Emberwright" does not tell you it is the mage. This does.
            draw_text(surface, assets, cls.archetype,
                      (row.right - 14, row.y + row.h // 2), "tiny", DIM,
                      right=True)
        if two_lines:
            draw_text(surface, assets, cls.title if unlocked else "locked",
                      (row.x + 14, row.y + 25), "tiny",
                      DIM if unlocked else FAINT)
    if not narrow:
        scroll_note(surface, assets,
                    pygame.Rect(list_x, top, list_w, visible * row_h),
                    start, visible, len(classes_mod.CLASSES))

    cls = classes_mod.CLASSES[game.ui.class_index]
    unlocked = classes_mod.is_unlocked(cls.key, game.meta)
    panel_x = list_x if narrow else list_x + list_w + 26
    # Floored, not trusted: this used to be a bare subtraction, and at a
    # phone width it came out negative and `pygame.Surface` refused it -
    # a hard crash rather than an ugly screen. See HANDOFF section 28.
    panel_w = max(120, rect.right - 34 - panel_x)
    card = pygame.Rect(panel_x, top, panel_w,
                       max(80, rect.bottom - 60 - top))
    panel(surface, card, edge=cls.color)

    x = card.x + 20
    y = card.y + 16
    floor = card.bottom - 18
    # **The name gives way to the archetype, not the other way round.**
    # On a phone the card is about 250 wide and "Hexbinder" in `big` plus
    # "Warlock" in `small` is more than that, so the two sat on each other.
    # The archetype is the shorter and the more useful of the pair to a new
    # player - it is the whole reason it is there (see the module
    # docstring) - so the name is the one that gets cut.
    archetype = cls.archetype if unlocked else ""
    room = card.w - 40
    if archetype:
        room -= assets.font("small").size(archetype)[0] + 14
    draw_text(surface, assets,
              ellipsize(assets.font("big"), cls.name, max(40, room)),
              (x, y), "big", cls.color)
    if archetype:
        draw_text(surface, assets, archetype, (card.right - 20, y + 14),
                  "small", DIM, right=True)
    y += 40
    draw_text(surface, assets, cls.title, (x, y), "small", DIM)
    y += 30

    if not unlocked:
        for line in wrap_text(cls.unlock_hint, assets.font("body"),
                              card.w - 40):
            if y > floor:
                break
            draw_text(surface, assets, line, (x, y), "body", BAD)
            y += 24
        return

    for line in cls.blurb:
        for wrapped in wrap_text(line, assets.font("small"), card.w - 40):
            if y > floor:
                break
            draw_text(surface, assets, wrapped, (x, y), "small", INK)
            y += 21
    # **What it trained with** (2026-09-01, s47). On the card you read
    # before choosing, because a class that hits harder with a bow is a
    # reason to pick it, and finding that out afterwards is finding it out
    # too late.
    if cls.favours and y <= floor:
        from .items import FAMILY_LABEL
        # Ellipsized to the card, like every other line on it: two family
        # names plus the label is wider than a phone's column.
        draw_text(surface, assets,
                  ellipsize(assets.font("small"),
                            "Trained with: %s"
                            % ", ".join(FAMILY_LABEL.get(f, f.title())
                                        for f in cls.favours),
                            card.w - 40),
                  (x, y + 6), "small", GOOD)
        y += 27
    y += 10

    bars = (("Health", cls.hp), ("Magic", cls.mag), ("Attack", cls.atk),
            ("Defense", cls.defense), ("Speed", cls.spd), ("Crit", cls.crit))
    col_w = (card.w - 40) // 2
    for i, (label, value) in enumerate(bars):
        bx = x + (i % 2) * col_w
        by = y + (i // 2) * 26
        if by > floor:
            break
        draw_text(surface, assets, label, (bx, by), "tiny", DIM)
        bar(surface, (bx + 66, by + 3, max(10, col_w - 86), 9),
            clamp((value - 0.6) / 1.1, 0.05, 1.0), cls.color)
    y += 3 * 26 + 14

    if y > floor:
        return
    draw_text(surface, assets, cls.signature_name, (x, y), "body", ACCENT)
    y += 24
    for line in wrap_text(cls.signature_text, assets.font("small"),
                          card.w - 40):
        if y > floor:
            break
        draw_text(surface, assets, line, (x, y), "small", INK)
        y += 21
    y += 8
    # **Wide enough for its own label** (2026-09-04). 230 was hardcoded and
    # "Take this road  (Enter)" needs 238 in the shipped font, so the T and
    # the closing bracket sat on the border - visible in a screenshot and
    # invisible to `layout.py`, which measures a text against the *panel*
    # it is in and this one is inside the card either way.
    # The longest phrasing that fits. On a phone even "Take this road" is
    # too wide for the card, and a label the button cuts in half is worse
    # than a short one.
    room = card.w - 40
    label = "Locked"
    if unlocked:
        for candidate in ("Take this road  (Enter)", "Take this road",
                          "Take it", "Enter"):
            label = candidate
            if assets.font("body").size(candidate)[0] + 28 <= room:
                break
    button_w = min(room, max(230, assets.font("body").size(label)[0] + 28))
    confirm_button(surface, assets, game,
                   pygame.Rect(card.right - 20 - button_w, card.bottom - 74,
                               button_w, 56),
                   label,
                   cls.name if unlocked else cls.unlock_hint[:34],
                   enabled=unlocked, color=cls.color)

    # The two footnotes go under the Confirm button, so they need room for
    # both lines or neither - half of a starting-kit sentence is worse than
    # none of it.
    if y + 18 > floor:
        return
    first = cls.skills[0][0] if cls.skills else ""
    from .skills import SKILLS_BY_KEY
    tiny = assets.font("tiny")
    if first in SKILLS_BY_KEY:
        draw_text(surface, assets,
                  ellipsize(tiny, "Opens with %s." % SKILLS_BY_KEY[first].name,
                            card.w - 40),
                  (x, y), "tiny", DIM)
    draw_text(surface, assets,
              ellipsize(tiny, "Starts with %s %s and %s %s."
                        % (article(cls.start_weapon), cls.start_weapon,
                           article(cls.start_armor), cls.start_armor),
                        card.w - 40),
              (x, y + 18), "tiny", DIM)


# -------------------------------------------------------- modifier select ---

def draw_modifier_select(game):
    from . import modifiers as modifiers_mod
    from .modifiers import MODIFIERS, MODIFIERS_BY_KEY
    surface, assets = game.screen, game.assets
    active = game.pending_modifiers
    rect = frame(game, "Set your terms",
                 "Enter toggles   every one is a cost   Esc to go back")

    # Shares a line with the frame's subtitle on a wide window and drops
    # below it on a narrow one, where the two used to sit on top of each
    # other. `top` follows, so the list starts under whichever it is.
    roomy_header = rect.w >= 900
    # The frame's subtitle wraps to two lines on a narrow window, so the
    # score has to clear whichever it turned out to be rather than a
    # position that was right for one line.
    subtitle_lines = len(wrap_text(
        "Enter toggles   every one is a cost   Esc to go back",
        assets.font("small"), rect.w - 36)[:2])
    below = rect.y + 62 + subtitle_lines * 19
    draw_text(surface, assets, "Score multiplier  x%.2f" % active.score,
              (rect.right - 34, rect.y + 62) if roomy_header
              else (rect.centerx, below), "body", ACCENT,
              right=roomy_header, center=not roomy_header)

    top = rect.y + 96 if roomy_header else below + 26
    # Fit every modifier plus the Begin row inside the frame, whatever the
    # window size and however many modifiers exist. The old floor of 36
    # was still taller than a landscape phone could hold ten of, so this
    # squeezes further and then scrolls - with the Begin row counted as one
    # of the entries, because it is one of the things you can land on.
    list_w = rect.w - 68
    # **Difficulty is the first row** (2026-09-01). It was only ever in
    # Settings, so a run began on whatever the last run used and never
    # said so: *"when creating a new save you're never granted with the
    # difficulty option itself"*. It belongs here because this is the
    # screen that asks what kind of run this is going to be, and because
    # every other answer on it is on the same axis.
    #
    # It stays changeable in Settings afterwards - that is old behaviour
    # and this does not touch it.
    # **The arena choice, where a run is chosen** (item 0bq). It has
    # been two rows in Settings since §74 and he could not find it -
    # which is §46's difficulty complaint word for word: *"when creating
    # a new save you're never granted with the difficulty option
    # itself"*. Same screen, same reason, and it only appears for the
    # modes it means anything to.
    # **Asked, not repeated** (§88). `Game.arena_row_shown` is the same
    # predicate and it already knew about a third mode; this copy did
    # not, so a hoard was set up on a screen with no arena row while the
    # arena it would be fought in was chosen somewhere else entirely.
    arena_row = game.arena_row_shown()
    extra = 1 if arena_row else 0
    total_rows = len(MODIFIERS) + 2 + extra
    # **Scroll rather than squeeze past the cost line** (s54). `minimum`
    # was 28, so nine modifiers fitted at 52 apiece and fifteen dropped to
    # 34 - under the 40 that a second line needs, which silently hid *what
    # every modifier takes* on the one screen whose entire job is saying
    # so. Adding six rows should cost a scrollbar, not the text.
    #
    # 40 rather than 52, because on a short window the choice is between a
    # squeezed list with costs and a roomy one without, and the cost is
    # the half nobody can look up elsewhere.
    start, visible, row_h = list_view(top, rect.bottom - 30, total_rows,
                                      game.ui.modifier_index, natural=52,
                                      minimum=44)
    # Two lines of text per row need the height; below that the second line
    # is dropped rather than drawn over the first.
    roomy = row_h >= 40
    if arena_row and start <= 1 and visible > 1:
        # Drawn before the difficulty row's `if`, because it shares the
        # same offset arithmetic and is easier to follow next to it.
        slot = 1 - start
        arena_rect = pygame.Rect(rect.x + 34, top + slot * row_h, list_w,
                                 max(18, row_h - 6))
        row_highlight(surface, arena_rect, game.ui.modifier_index == 1,
                      ACCENT, game=game, kind="modifier", index=1)
        draw_text(surface, assets, "Arena",
                  (arena_rect.x + 14,
                   arena_rect.y + (4 if roomy else
                                   max(1, (arena_rect.h - 19) // 2))),
                  "body", INK)
        draw_text(surface, assets, _arena_label(game.settings),
                  (arena_rect.right - 18, arena_rect.y + arena_rect.h // 2),
                  "small", ACCENT, right=True)
        if roomy:
            draw_text(surface, assets,
                      ellipsize(assets.font("tiny"),
                                _arena_note(game.settings),
                                arena_rect.w - 190),
                      (arena_rect.x + 14,
                       arena_rect.bottom
                       - assets.font("tiny").get_height() - 3),
                      "tiny", DIM)
    if start == 0 and visible:
        diff = pygame.Rect(rect.x + 34, top, list_w, max(18, row_h - 6))
        row_highlight(surface, diff, game.ui.modifier_index == 0, ACCENT,
                      game=game, kind="modifier", index=0)
        draw_text(surface, assets, "Difficulty",
                  (diff.x + 14, diff.y + (4 if roomy else
                                          max(1, (diff.h - 19) // 2))),
                  "body", INK)
        value = game.settings.difficulty.title()
        if roomy:
            # **Ellipsized against the value, not the row.** The help line
            # and the difficulty share a line, so the space it may use is
            # what is left after "Nightmare" - which on a 390-wide window
            # is not much, and it ran into it and then off the panel.
            room = (diff.right - 18
                    - assets.font("small").size(value)[0] - 24) - (diff.x + 14)
            draw_text(surface, assets,
                      ellipsize(assets.font("tiny"),
                                "Enter changes it. Settings can too.", room),
                      (diff.x + 14,
                       diff.bottom - assets.font("tiny").get_height() - 3),
                      "tiny", DIM)
        draw_text(surface, assets, value,
                  (diff.right - 18, diff.y + diff.h // 2), "small", ACCENT,
                  right=True)
    # **Begin gets a slot of its own** (s54). When the list scrolls past
    # the bottom, Begin is pinned to the last visible slot - and with
    # nine modifiers the list never scrolled, so nobody saw it land on
    # top of the final row. The list gives up that slot instead.
    pinned = (len(MODIFIERS) + 1 + extra - start) >= visible
    for offset in range(visible - 1 if pinned else visible):
        i = start + offset
        if i == 0:
            continue                       # the difficulty row, drawn above
        if arena_row and i == 1:
            continue                       # the arena row, drawn above
        if i - 1 - extra >= len(MODIFIERS):
            break
        mod = MODIFIERS[i - 1 - extra]
        row = pygame.Rect(rect.x + 34, top + offset * row_h, list_w,
                          max(18, row_h - 6))
        picked = mod.key in active.keys
        # **Something else is holding this one on** (item 0bi). His rule:
        # *"for free-roam combat to exist free-roam MUST also exist"* - so
        # the row is on and cannot be turned off, and it has to *say* so.
        # A control that will refuse you is his oldest interface
        # complaint (§45); one that silently ignores you is the same
        # thing wearing a tick.
        held = active.locked(mod.key)
        row_highlight(surface, row, i == game.ui.modifier_index, mod.color,
                      game=game, kind="modifier", index=i, chosen=picked)
        mark = "[-]" if held else ("[x]" if picked else "[ ]")
        draw_text(surface, assets, "%s %s" % (mark, mod.name),
                  (row.x + 14, row.y + (4 if roomy else
                                        max(1, (row.h - 19) // 2))), "body",
                  mod.color if picked else INK)
        if roomy:
            # **Anchored to the bottom of the row, not to a constant**
            # (s54). At 26 it sat below a 34-pixel box once fifteen
            # modifiers squeezed the pitch, so the cost line hung out of
            # the highlight of every row you selected.
            note = "%s   %s" % (mod.cost, mod.boon)
            if held:
                owners = [MODIFIERS_BY_KEY[k].name
                          for k in modifiers_mod.requires_which(mod.key)
                          if k in active.keys]
                if owners:
                    note = "Required by %s" % owners[0]
            draw_text(surface, assets,
                      ellipsize(assets.font("tiny"), note, row.w - 70),
                      (row.x + 14,
                       row.bottom - assets.font("tiny").get_height() - 3),
                      "tiny", DIM)
        draw_text(surface, assets, "x%.2f" % mod.score,
                  (row.right - 18, row.y + row.h // 2), "small",
                  ACCENT if picked else FAINT, right=True)
    scroll_note(surface, assets,
                pygame.Rect(rect.x + 34, top, list_w, visible * row_h),
                start, min(visible, total_rows), total_rows)

    begin_offset = len(MODIFIERS) + 1 + extra - start
    if begin_offset >= visible:
        # Scrolled past the bottom of the window: pin Begin to the last slot
        # so it is always reachable and never drawn off the frame.
        begin_offset = visible - 1
    begin = pygame.Rect(rect.x + 34, top + max(0, begin_offset) * row_h,
                        list_w, max(18, row_h - 6))
    selected = game.ui.modifier_index >= len(MODIFIERS) + 1 + extra
    row_highlight(surface, begin, selected, GOOD, game=game,
                  kind="modifier", index=len(MODIFIERS) + 1 + extra)
    label = "Begin the run" if not active.keys else (
        "Begin  -  %s" % active.summary())
    draw_text(surface, assets, label[:70], begin.center, "body",
              GOOD if selected else DIM, center=True)


# ----------------------------------------------------------------- codex ---

def codex_rows(game):
    """[(entry, known)] for the selected chapter.

    Endings are omitted entirely until reached: listing them, even as sealed
    rows, would give away how many there are and that a choice is coming.
    """
    chapter = story.CHAPTERS[game.ui.codex_tab]
    rows = []
    for entry in story.CODEX:
        if entry.chapter != chapter:
            continue
        known = game.meta.knows(entry.key)
        if chapter == "Endings" and not known:
            continue
        rows.append((entry, known))
    return rows


def draw_codex(game):
    if game.player:
        overlay(game)
    surface, assets = game.screen, game.assets
    done, total = game.meta.codex_progress()
    found = len(game.meta.endings)
    subtitle = "%d of %d pages   Tab changes chapter   Enter to read" % (
        done, total)
    if game.meta.scenes_discovered():
        subtitle = "%s   R to walk it again" % subtitle
    if found:
        subtitle = "%s   -   %d ending%s found" % (
            subtitle, found, "" if found == 1 else "s")
    rect = frame(game, "Codex", subtitle)

    # The gaps come out of the width too, and the label is cut to the tab.
    # Same pair of mistakes the bag's tabs had: a row of tabs that fits by
    # arithmetic can still have every label hanging over its neighbour.
    gaps = 6 * (len(story.CHAPTERS) - 1)
    tab_w = min(178, (rect.w - 68 - gaps) // len(story.CHAPTERS))
    for i, chapter in enumerate(story.CHAPTERS):
        # Under the header wherever it ended - the subtitle wraps to a
        # second line on a phone and 86 assumed one (s53). The codex
        # rolls its own tabs rather than using `tab_row`, so it needs
        # the same line; that duplication is why it was missed.
        tab_top = max(86, game.ui.header_bottom - rect.y + 6)
        tab = pygame.Rect(rect.x + 34 + i * (tab_w + 6), rect.y + tab_top,
                          tab_w, 28)
        row_highlight(surface, tab, i == game.ui.codex_tab, PANEL_EDGE,
                      game=game, kind="codex_tab", index=i)
        draw_text(surface, assets,
                  ellipsize(assets.font("tiny"), chapter, tab.w - 8),
                  tab.center, "tiny",
                  INK if i == game.ui.codex_tab else FAINT, center=True)

    rows = codex_rows(game)
    top = rect.y + 128
    row_h = 44
    game.ui.codex.visible = max(3, (rect.bottom - 60 - top) // row_h)
    game.ui.codex.clamp_to(len(rows))

    if not rows:
        draw_text(surface, assets, "Nothing recorded here yet.",
                  (rect.centerx, rect.centery), "mid", FAINT, center=True)
        return

    for i, (entry, known) in game.ui.codex.window(rows):
        row = pygame.Rect(rect.x + 34, top + (i - game.ui.codex.offset) * row_h,
                          rect.w - 68, row_h - 6)
        row_highlight(surface, row, i == game.ui.codex.index,
                      ACCENT if known else PANEL_EDGE,
                      game=game, kind="codex", index=i)
        # Both lines cut to the row, not to a character count: the preview
        # was `[:64]`, which is a different width in every font and on a
        # narrow row is most of the way across the next screen.
        draw_text(surface, assets,
                  ellipsize(assets.font("body"),
                            entry.title if known else "- sealed page -",
                            row.w - 28),
                  (row.x + 14, row.y + 4), "body", INK if known else FAINT)
        # A locked row says nothing about its contents. The old hint text
        # named characters and events that had not happened yet.
        draw_text(surface, assets,
                  ellipsize(assets.font("tiny"),
                            entry.body[0] if known else "not yet discovered",
                            row.w - 28),
                  (row.x + 14, row.y + 24), "tiny", DIM if known else FAINT)

    if game.reading is not None:
        draw_codex_page(game, game.reading)


def draw_codex_page(game, entry):
    """Full-screen reader for one unlocked page."""
    overlay(game, 226)
    surface, assets = game.screen, game.assets
    width, height = surface.get_size()
    # 760 wide is wider than a phone, so the box - and everything measured
    # from it - was hanging off both edges at once.
    box_w = min(760, width - 32)
    box = pygame.Rect((width - box_w) // 2, 60, box_w, max(120, height - 120))
    panel(surface, box, edge=ACCENT)

    draw_text(surface, assets, entry.title, (box.centerx, box.y + 26),
              "mid", ACCENT, center=True)
    draw_text(surface, assets, entry.chapter, (box.centerx, box.y + 56),
              "tiny", FAINT, center=True)

    y = box.y + 90
    limit = box.bottom - 54
    for line in entry.body:
        if not line:
            y += 12
            continue
        for wrapped in wrap_text(line, assets.font("small"), box.w - 72):
            if y > limit:
                break
            draw_text(surface, assets, wrapped, (box.x + 36, y), "small", INK)
            y += 22
    draw_text(surface, assets, "Esc to close", (box.centerx, box.bottom - 28),
              "tiny", DIM, center=True)


# --------------------------------------------------------------- endings ---

def draw_ending_choice(game):
    overlay(game, 216)
    surface, assets = game.screen, game.assets
    width, height = surface.get_size()
    options = game.ending_options

    draw_text(surface, assets, "THE DOOR", (width // 2, 70), "big",
              (226, 214, 250), center=True)
    for i, line in enumerate(wrap_text(
            "It is only a door. Everything else was people.",
            assets.font("small"), width - 48)):
        draw_text(surface, assets, line, (width // 2, 108 + i * 18), "small",
                  DIM, center=True)

    top = 156
    row_w = min(660, width - 48)
    e_start, e_visible, row_h = list_view(top, height - 60, len(options),
                                          game.ui.ending_index, natural=92,
                                          minimum=44)
    for offset in range(e_visible):
        i = e_start + offset
        ending = options[i]
        row = pygame.Rect((width - row_w) // 2, top + offset * row_h, row_w,
                          max(28, row_h - 12))
        row_highlight(surface, row, i == game.ui.ending_index, game=game,
                      kind="ending", index=i)
        draw_text(surface, assets, ending.title, (row.x + 22, row.y + 12),
                  "mid", ACCENT if i == game.ui.ending_index else INK)
        if row.h >= 60:
            draw_text(surface, assets,
                      ellipsize(assets.font("small"), ending.subtitle,
                                row.w - 44),
                      (row.x + 22, row.y + 44), "small", DIM)
    scroll_note(surface, assets,
                pygame.Rect((width - row_w) // 2, top, row_w,
                            e_visible * row_h),
                e_start, e_visible, len(options))

    # Deliberately no list of the endings you cannot reach. Knowing they
    # exist, and what unlocks them, is the thing the codex is supposed to
    # earn you across runs.
    y = top + len(options) * row_h + 10
    if y < height - 50:
        draw_text(surface, assets,
                  "Whatever you choose, this run ends here.",
                  (width // 2, y), "tiny", FAINT, center=True)


def draw_ending_text(game):
    surface, assets = game.screen, game.assets
    width, height = surface.get_size()
    ending = game.ending
    if ending is None:
        return

    draw_text(surface, assets, ending.title, (width // 2, 56), "big",
              ACCENT, center=True)
    draw_text(surface, assets, ending.subtitle, (width // 2, 96), "small",
              DIM, center=True)

    y = 140
    limit = height - 96
    for line in ending.body:
        if not line:
            y += 12
            continue
        # Wrapped to the window, not to 720: an ending is centred prose and
        # a fixed measure wider than the screen pushes half of every line
        # off both edges at once.
        for wrapped in wrap_text(line, assets.font("small"),
                                 min(720, width - 64)):
            if y > limit:
                break
            draw_text(surface, assets, wrapped, (width // 2, y), "small",
                      INK, center=True)
            y += 22

    draw_text(surface, assets, "Score  %s" % commafy(game.last_score),
              (width // 2, height - 66), "body", ACCENT, center=True)
    draw_text(surface, assets, "Enter to close the book",
              (width // 2, height - 38), "tiny", DIM, center=True)


def scene_measure(game):
    """The scene box, its prose, and how much of that prose actually fits.

    **Split out of `draw_scene` because the draw was silently eating
    story** (2026-08-30). The loop here used to `break` when it ran out of
    panel, which overflows nothing and is therefore invisible to
    `tests/layout.py`: the checker looks for text outside its box, and text
    that was never drawn is not outside anything. At 844x390 that quietly
    cut the prologue, the Door, the orientation and four of the six
    waystations, and a resolution scene - four rows of answers rather than
    three - would have shown four lines out of thirteen.

    `event_scene` needs the same numbers to clamp scrolling, so they are
    measured once here rather than twice with two chances to disagree.

    Returns `(box, wrapped, capacity, rows_h)`.
    """
    surface, state = game.screen, game.scene
    width, height = surface.get_size()
    scene = state["scene"]
    lines = state["reply"] if state["phase"] == "reply" else scene.body
    options = state["options"] if state["phase"] == "choosing" else []

    # Wrapped to the box before the box is measured, because the lines are
    # written as prose and fit a narrow panel only by luck. On a phone they
    # were being drawn at their full length from a negative x.
    #
    # **660, not 600** (2026-08-30). Every scene in the game is written as
    # hand-broken lines of up to 65 characters, and a 600-wide panel gives
    # 60 - so forty of them re-wrapped, each one dropping its last word or
    # two onto a line of its own. Nothing overlapped and nothing overflowed,
    # so `tests/layout.py` was right to pass it; it just looked like a
    # mistake. The panel now fits the measure the prose was written to, and
    # `TestSceneProseIsAllReadable` holds new writing to it.
    box_w = min(660, width - 32)
    wrapped = []
    for line in lines:
        wrapped.extend(wrap_text(line, game.assets.font("small"), box_w - 60)
                       if line else [""])
    rows_h = len(options) * 36
    box_h = min(height - 40, 96 + len(wrapped) * 22 + rows_h + 60)
    box = pygame.Rect((width - box_w) // 2, (height - box_h) // 2, box_w,
                      box_h)
    # The first line sits at box.y + 72 and the last may start exactly on
    # the floor, so a box with room for one line has a capacity of one.
    capacity = max(1, ((box.bottom - 60 - rows_h) - (box.y + 72)) // 22 + 1)
    return box, wrapped, capacity, rows_h


def scene_scroll_max(game):
    """How far the prose can be pushed down. Zero when all of it fits."""
    _box, wrapped, capacity, _rows = scene_measure(game)
    return max(0, len(wrapped) - capacity)


def draw_scene(game):
    """A story moment, and the answers on offer.

    Two phases in one screen: the question, then what the game says back.
    The reply is the reason the choice exists, so it gets the same room the
    question did rather than being squeezed into a toast.

    **Prose scrolls on a short window rather than being cut.** Left and
    Right read on, because Up and Down already belong to the answers and
    Enter already means "that one" - and the hint appears only when there
    is something below the fold, so a window that fits is untouched.
    """
    from . import scenes as scenes_mod
    overlay(game, 232)
    surface, assets, state = game.screen, game.assets, game.scene
    scene = state["scene"]

    box, wrapped, capacity, rows_h = scene_measure(game)
    options = state["options"] if state["phase"] == "choosing" else []
    top = clamp(int(state.get("scroll", 0)), 0, max(0, len(wrapped) - capacity))
    state["scroll"] = top
    panel(surface, box, edge=ACCENT)

    # A favour is titled with the NPC's name (`Game.play_favour`), so this
    # screen inherits the same problem the dialogue box had.
    draw_text(surface, assets,
              ellipsize(assets.font("big"), scene.title, box.w - 48),
              (box.centerx, box.y + 30), "big", ACCENT, center=True)

    y = box.y + 72
    for line in wrapped[top:top + capacity]:
        draw_text(surface, assets, line, (box.x + 30, y), "small", INK)
        y += 22
    if len(wrapped) > capacity:
        # Under the title rather than beside the prose: the box is at its
        # narrowest exactly when this hint exists, and it says which key
        # to press rather than only that there is more, because an arrow
        # on its own is the kind of hint nobody reads.
        draw_text(surface, assets,
                  "%d-%d of %d   Left and Right to read on"
                  % (top + 1, min(top + capacity, len(wrapped)), len(wrapped)),
                  (box.centerx, box.y + 56), "tiny", FAINT, center=True)

    y += 12
    for i, option in enumerate(options):
        row = pygame.Rect(box.x + 26, y + i * 36, box.w - 52, 32)
        chosen_before = option.key in game.meta.scene_taken.get(scene.key, [])
        row_highlight(surface, row, i == state["index"], game=game,
                      kind="scene", index=i)
        # A choice taken in some earlier run is marked, not hidden: seeing
        # what you did last time is the point of keeping the record.
        draw_text(surface, assets, option.label, (row.x + 14, row.y + 8),
                  "small", INK if i == state["index"] else DIM)
        if option.secret:
            draw_text(surface, assets, "?", (row.right - 16, row.y + 16),
                      "small", ACCENT, right=True)
        elif chosen_before:
            # In a replay this is the whole point of the row, so it says so
            # plainly rather than in the shorthand a live scene uses.
            draw_text(surface, assets,
                      "you said this" if state.get("replay") else "before",
                      (row.right - 16, row.y + 16), "tiny",
                      ACCENT if state.get("replay") else FAINT, right=True)

    if state["phase"] == "reply":
        # **A reply is one keypress with no cursor**, so the panel is the
        # button (2026-08-31): *"when talking to someone it js says 'Enter
        # to go on' you should be able to left-click to go on as well"*.
        # Registered after the prose and before the footer, and only in
        # this phase - the choosing phase has rows of its own and they are
        # added later, so they sit on top of it either way.
        game.ui.hot.add(box, "key", pygame.K_RETURN)

    if state.get("replay"):
        footer = ("Enter or click to go back to the answers   Esc to close"
                  if state["phase"] == "reply"
                  else "Up and Down to choose   Enter to hear it   "
                       "Esc to close")
    else:
        footer = ("Enter or click to go on" if state["phase"] == "reply"
                  else "Up and Down to choose   Enter to answer")
    draw_text(surface, assets, footer, (box.centerx, box.bottom - 26),
              "tiny", FAINT, center=True)


def draw_story(game):
    """The codex's story record: every scene you have reached, and the
    answers you gave and did not give.

    Greyed rows are roads you were offered and passed over. That is the
    whole feature - the page fills in across playthroughs, and you can see
    the shape of what you have never tried.
    """
    from . import scenes as scenes_mod
    surface, assets, meta = game.screen, game.assets, game.meta
    rect = frame(game, "The Sixth Road",
                 "Enter to watch it again   Esc to go back")

    known = [s for s in scenes_mod.SCENES if meta.saw_scene(s.key)]
    if not known:
        draw_text(surface, assets,
                  "Nothing yet. Walk the road and it will fill in.",
                  (rect.centerx, rect.centery), "mid", FAINT, center=True)
        return

    state = game.ui.story
    state.clamp_to(len(known))
    index = min(state.index, len(known) - 1)

    # One column when there is not room for two. The list becomes a title
    # and a counter, and the card gets the whole frame - which is a real
    # layout, where a list crushed against a sixty-pixel card is not.
    narrow = is_narrow(rect)
    list_w = 0 if narrow else 200
    if narrow:
        head = pygame.Rect(rect.x + 34, rect.y + 92, rect.w - 68, 30)
        game.ui.hot.add(head, "story", index)
        draw_text(surface, assets, known[index].title,
                  (head.x + 4, head.y + 6), "small", ACCENT)
        draw_text(surface, assets, "%d of %d" % (index + 1, len(known)),
                  (head.right - 4, head.y + 15), "tiny", DIM, right=True)
    else:
        for i, scene in state.window(known):
            row = pygame.Rect(rect.x + 34,
                              rect.y + 96 + (i - state.offset) * 40,
                              list_w, 34)
            row_highlight(surface, row, i == index, game=game,
                          kind="story", index=i)
            draw_text(surface, assets, scene.title, (row.x + 12, row.y + 8),
                      "small", ACCENT if i == index else DIM)

    scene = known[index]
    seen = meta.scene_seen.get(scene.key, [])
    taken = meta.scene_taken.get(scene.key, [])
    card_x = rect.x + 34 if narrow else rect.x + 34 + list_w + 22
    card_top = rect.y + (128 if narrow else 96)
    card = pygame.Rect(card_x, card_top, max(120, rect.right - 34 - card_x),
                       max(80, rect.bottom - 60 - card_top))
    panel(surface, card, edge=PANEL_EDGE)

    x, y = card.x + 18, card.y + 16
    floor = card.bottom - 16
    for line in scene.body:
        for wrapped in wrap_text(line, assets.font("tiny"), card.w - 36):
            if y > floor:
                break
            draw_text(surface, assets, wrapped, (x, y), "tiny", DIM)
            y += 15
    y += 18

    for option in scene.choices:
        if option.key not in seen or y > floor:
            continue                  # never offered; it does not exist yet
        picked = option.key in taken
        draw_text(surface, assets,
                  ("> " if picked else "  ") + option.label, (x, y), "small",
                  INK if picked else FAINT)
        y += 20
        if picked:
            for line in option.reply:
                for wrapped in wrap_text(line, assets.font("tiny"),
                                         card.w - 52):
                    draw_text(surface, assets, wrapped, (x + 16, y), "tiny",
                              DIM)
                    y += 15
            y += 8

    hidden = [c for c in scene.choices if c.key not in seen]
    if hidden:
        draw_text(surface, assets,
                  "%d answer%s you have never been offered"
                  % (len(hidden), "" if len(hidden) == 1 else "s"),
                  (card.centerx, card.bottom - 22), "tiny", FAINT,
                  center=True)


LOBBY_ROWS = ("Host a room", "Join with a code", "Back")


def draw_mode_select(game):
    """Which game are we playing (0ag, s59).

    > "when ur starting co-op u get to pick whether to do a regular run or
    > do a different gamemode"

    Shown for a new solo run as well as a room, because a mode that only
    exists in co-op is a mode most people never see.
    """
    from . import gamemodes
    surface, assets, state = game.screen, game.assets, game.ui
    rect = frame(game, "Choose a game",
                 "Up and Down to choose   Enter to pick   Esc to go back")
    # **The rows share what there is**, rather than each taking 74 pixels
    # and the last one running off the bottom. Adding a fourth mode did
    # exactly that on a landscape phone, which is `layout.py --phone`
    # catching the thing it exists for (§86) - and a fifth would have
    # done it again.
    top = max(rect.y + 110, int(getattr(game.ui, "header_bottom", 0)) + 18)
    count = max(1, len(gamemodes.MODES))
    span = int(clamp((rect.bottom - 58 - top) // count, 40, 74))
    height = max(34, span - 12)
    # **Nothing, rather than half of it.** On a short landscape window
    # the rows squeeze to 34 pixels and a blurb at +30 hangs out of its
    # own highlight - which is §53's complaint in his own words: *"the
    # text like overlaps the selection highlight"*. The name is enough;
    # the note at the bottom says the rest.
    lines = 2 if height >= 56 else (1 if height >= 50 else 0)
    for i, mode in enumerate(gamemodes.MODES):
        row = pygame.Rect(rect.x + 40, top + i * span, rect.w - 80, height)
        row_highlight(surface, row, i == state.mode_index, game=game,
                      kind="mode", index=i)
        draw_text(surface, assets, mode.name, (row.x + 18, row.y + 8),
                  "small", INK if i == state.mode_index else DIM)
        for j, line in enumerate(
                wrap_text(mode.blurb, assets.font("tiny"),
                          row.w - 40)[:lines]):
            draw_text(surface, assets, line, (row.x + 18, row.y + 30 + j * 15),
                      "tiny", FAINT)

    note_top = top + count * span + 10
    note = ("They all use the same classes, difficulty and modifiers. "
            "Only the long road keeps a save; the rest are scored.")
    room = max(0, (rect.bottom - 26 - note_top) // 16)
    for i, line in enumerate(
            wrap_text(note, assets.font("tiny"), rect.w - 120)[:room]):
        draw_text(surface, assets, line, (rect.centerx, note_top + i * 16),
                  "tiny", FAINT, center=True)


#: What a card of each kind is called on its own badge. **A night's hand
#: is mixed**, so a card has to say what it is before it says what it
#: does - in a Last Stand the whole offer is one kind and the badge is
#: only a reminder.
SPOIL_BADGE = {"skill": "Skill", "item": "Loot", "buff": "Boon"}
#: The colour a boon's length is written in. Permanent is the loud one,
#: because *"+7% attack (perm buff)"* and *"heal 20% right now(temp
#: buff)"* are his own two categories and the difference is the decision.
SPOIL_LASTS_TONE = {"run": GOOD, "wave": ACCENT, "now": INK}


def spoil_cards(game, rect):
    """Where the cards go. One place, so drawing and clicking agree.

    **A row on a window, two rows on a phone.** Four cards side by side
    on a 420-pixel screen is four columns of ellipsis, and a card you
    cannot read is the thing this screen exists to prevent.
    """
    offer = game.spoil_offer()
    count = len(offer.cards) if offer else 0
    if not count:
        return [], 1
    cols = count if rect.w >= 720 else 2
    cols = max(1, min(cols, count))
    rows = (count + cols - 1) // cols
    gap = 10
    card_w = (rect.w - 40 - gap * (cols - 1)) // cols
    top = max(rect.y + 96, int(getattr(game.ui, "header_bottom", 0)) + 12)
    # **As tall as a card needs and no taller.** The first cut gave every
    # card the height that was left over, which on a desktop is a row of
    # four mostly-empty boxes with a line of text at the top of each -
    # and the empty half is where the reader panel now goes.
    card_h = int(clamp((rect.bottom - 74 - top - gap * (rows - 1)) // rows,
                       92, 138))
    cells = []
    for index in range(count):
        row, col = divmod(index, cols)
        cells.append(pygame.Rect(rect.x + 20 + col * (card_w + gap),
                                 top + row * (card_h + gap),
                                 card_w, max(70, card_h)))
    return cells, cols


def draw_spoils(game):
    """What a cleared wave offers (items 0bs/0bt). Savvydev, 2026-09-07:

    > at the end of each wave in the last stand gamemode, there should be
    > an option to pick a random skill out of four and then another
    > option to pick a random item (armor, weapon or accessories) each
    > when right clicked open a small screen that says what each does

    **The reading is the feature.** Four names is not a choice; four
    names you can find out about is. So every card carries one line of
    what it does on its face, and right-click - the verb this game
    already uses for *ask what this is* - opens the rest.
    """
    from . import spoils as spoils_mod
    surface, assets = game.screen, game.assets
    offer = game.spoil_offer()
    if offer is None:
        return
    total = len(game.spoils)
    step = ("" if total <= 1
            else "   %d of %d" % (game.spoil_at + 1, total))
    rect = frame(game, offer.title or "Take one",
                 (offer.line or "") + step)
    cells, cols = spoil_cards(game, rect)
    index = int(getattr(game.ui, "spoil_index", 0))
    detail = getattr(game.ui, "spoil_detail", None)
    if detail is not None and not 0 <= int(detail) < len(cells):
        detail = None
    # **On a narrow window the reader replaces the cards** rather than
    # covering them. Two rows of cards leave nothing underneath, and a
    # panel laid over the three things you are choosing between is the
    # comparison made harder - which the layout checker says out loud:
    # *"BURIED 'Enter to take' under a later panel"*.
    below = (rect.bottom - 46) - (max((c.bottom for c in cells),
                                      default=rect.y) + 12)
    alone = detail is not None and below < 150

    for n, cell in enumerate([] if alone else cells):
        kind, card = game.spoil_card(n)
        if card is None:
            continue
        chosen = n == index
        panel(surface, cell, alpha=228,
              edge=ACCENT if chosen else PANEL_EDGE)
        tone = INK
        if kind == "item":
            # `Item.color` is the rarity colour every other
            # screen uses; there is no second table here.
            tone = tuple(getattr(card, "color", INK))
        draw_text(surface, assets, SPOIL_BADGE.get(kind, "Card"),
                  (cell.x + 10, cell.y + 8), "tiny", FAINT)
        draw_text(surface, assets,
                  ellipsize(assets.font("small"),
                            spoils_mod.card_name(kind, card), cell.w - 20),
                  (cell.centerx, cell.y + 34), "small", tone, center=True)
        # **Wrapped, not cut.** "+7% resistance for the rest of the ..."
        # is a card that has told you nothing, and the one thing this
        # screen cannot do is hide what a card does.
        body, tones = [], []
        for i, line in enumerate(spoils_mod.card_lines(kind, card,
                                                       game.player)):
            tone = (SPOIL_LASTS_TONE.get(getattr(card, "lasts", ""), DIM)
                    if kind == "buff" and i == 1 else DIM)
            for part in wrap_text(line, assets.font("tiny"), cell.w - 18):
                body.append(part)
                tones.append(tone)
        for i, (line, tone) in enumerate(zip(body[:4], tones[:4])):
            draw_text(surface, assets, line,
                      (cell.centerx, cell.y + 58 + i * 15), "tiny", tone,
                      center=True)
        if chosen:
            draw_text(surface, assets, "Enter to take",
                      (cell.centerx, cell.bottom - 14), "tiny", ACCENT,
                      center=True)
        game.ui.hot.add(cell, "spoil", n)

    # **The small screen a right-click opens** - his words, and the
    # existing verb: `Game.right_click` already means *ask what this is*
    # on gear and on skills, so this is the third place it means it.
    if detail is not None:
        kind, card = game.spoil_card(int(detail))
        if card is not None:
            # Under the row where there is room for it, and in place of
            # the row where there is not.
            _spoil_detail(game, rect, kind, card,
                          top=None if alone
                          else max(c.bottom for c in cells) + 12)

    hint = "Enter takes it   Right-click or Q to read   Esc leaves them"
    if alone:
        hint = "Q or right-click goes back   Enter takes it"
    elif is_narrow(surface.get_rect()):
        hint = "Enter takes   Q reads   Esc leaves"
    draw_text(surface, assets,
              ellipsize(assets.font("tiny"), hint, rect.w - 40),
              (rect.centerx, rect.bottom - 26), "tiny", FAINT, center=True)


def _spoil_detail(game, rect, kind, card, top=None):
    """The small screen a right-click opens.

    **Two of the three already had a reader** and neither is rewritten
    here: `item_card` diffs an item against what you are wearing, which
    is the honest answer to *what does this do*, and `skill_card` says
    what a skill costs and roughly what it hits for. A third copy of
    either would be a third thing to keep true.
    """
    from . import spoils as spoils_mod
    surface, assets = game.screen, game.assets
    # **It has to end above the footer.** Placing the reader under the
    # cards is right on a window and impossible on a phone, where two
    # rows of cards leave no room below - so when it will not fit it
    # rides up over them instead of writing through the hint line.
    floor = rect.bottom - 46

    def settle(top, tall):
        top = int(top if top is not None else rect.centery - tall // 2)
        return int(clamp(min(top, floor - tall), rect.y + 58, floor - 40))

    if kind == "item":
        width = min(340, max(240, rect.w - 60))
        spot = (rect.centerx - width // 2, settle(top, 230))
        box = item_card(surface, assets, card, spot,
                        equipped=game.player.equipment.get(card.slot)
                        if game.player else None,
                        width=width, player=game.player)
        game.ui.hot.add(box, "spoil_detail", 0)
        return
    if kind == "skill":
        width = min(320, max(240, rect.w - 60))
        spot = (rect.centerx - width // 2, settle(top, 230))
        box = skill_card(surface, assets, card, game.player, spot,
                         width=width)
        game.ui.hot.add(box, "spoil_detail", 0)
        return
    lines = spoils_mod.card_lines(kind, card, game.player)
    wrapped = []
    for line in lines:
        wrapped.extend(wrap_text(line, assets.font("tiny"),
                                 min(420, rect.w - 90)))
    box_w = min(460, rect.w - 60)
    box_h = min(rect.h - 60, 78 + 16 * len(wrapped[:8]))
    box = pygame.Rect(rect.centerx - box_w // 2, settle(top, box_h),
                      box_w, box_h)
    panel(surface, box, alpha=246, edge=ACCENT)
    draw_text(surface, assets,
              ellipsize(assets.font("small"),
                        spoils_mod.card_name(kind, card), box.w - 24),
              (box.centerx, box.y + 22), "small", ACCENT, center=True)
    for i, line in enumerate(wrapped[:8]):
        draw_text(surface, assets, line, (box.centerx, box.y + 48 + i * 16),
                  "tiny", INK, center=True)
    draw_text(surface, assets, "Right-click or Q to close",
              (box.centerx, box.bottom - 16), "tiny", FAINT, center=True)
    game.ui.hot.add(box, "spoil_detail", 0)


def draw_waiting_room(game):
    """What a guest sees before the host opens the arena.

    Small on purpose. There is nothing to decide here and nothing to
    read; the only question it answers is *is this working*, and a name
    and a line of text answer it.
    """
    from . import coop as coop_mod, gamemodes
    surface, assets = game.screen, game.assets
    mode = gamemodes.mode_for(getattr(game, "mode", ""))
    host = ""
    if game.coop is not None:
        host = getattr(game.coop, "host_name", "") or ""
    overlay(game, 232)
    width, height = surface.get_size()
    box = pygame.Rect(0, 0, min(460, width - 24), 150)
    box.center = (width // 2, height // 2)
    panel(surface, box, edge=ACCENT)
    draw_text(surface, assets, mode.name, (box.centerx, box.y + 32),
              "big", ACCENT, center=True)
    draw_text(surface, assets,
              ellipsize(assets.font("small"),
                        "%s is getting it ready." % (host or "The host"),
                        box.w - 40),
              (box.centerx, box.y + 72), "small", INK, center=True)
    waiting = (game.coop is not None
               and game.coop.role == coop_mod.CLIENT)
    draw_text(surface, assets,
              "It starts when they start it." if waiting
              else "Waiting.",
              (box.centerx, box.y + 100), "tiny", DIM, center=True)
    draw_text(surface, assets, "Esc to leave",
              (box.centerx, box.bottom - 22), "tiny", FAINT, center=True)


def draw_waves(game):
    """Between waves: what you just cleared, and what is coming.

    **The next wave's rewards are shown before it is fought**, because
    `gamemodes.wave_rewards` is data rather than an act - which is also
    what lets both machines in a room agree what a wave pays without
    sending it.
    """
    from . import gamemodes
    surface, assets = game.screen, game.assets
    run = game.waves
    if run is None:
        # **A guest waiting on a mode that has no waves** - a night or a
        # hoard. It sat on this screen with `game.waves` as None and this
        # function returned immediately, so what the guest actually got
        # was **a blank window** until the host opened the arena. Nothing
        # caught it because nothing draws a screen for a state that is
        # only ever reached over a socket (§86).
        draw_waiting_room(game)
        return
    overlay(game, 232)
    width, height = surface.get_size()
    box_w = min(560, width - 24)
    # Tall enough for the deepest reward list and no taller - the first
    # version left a third of the panel empty under the button.
    box_h = min(height - 40, 352)
    box = pygame.Rect((width - box_w) // 2, (height - box_h) // 2,
                      box_w, box_h)
    panel(surface, box, edge=ACCENT)

    draw_text(surface, assets, "Last Stand", (box.centerx, box.y + 30),
              "big", ACCENT, center=True)
    draw_text(surface, assets,
              "Cleared %d" % run.cleared if run.cleared else "Nothing yet",
              (box.centerx, box.y + 62), "small", INK, center=True)

    nxt = gamemodes.wave_rewards(run.wave)
    draw_text(surface, assets, "Wave %d" % run.wave,
              (box.centerx, box.y + 96), "big",
              GOOD if nxt["heal"] else INK, center=True)
    party = game.wave_party_size()
    draw_text(surface, assets,
              "%d monster%s around level %d"
              % (gamemodes.wave_pack(run.wave, party),
                 "" if gamemodes.wave_pack(run.wave, party) == 1 else "s",
                 nxt["level"]),
              (box.centerx, box.y + 126), "tiny", DIM, center=True)

    lines = ["Clearing it pays %d gold and %d XP" % (nxt["gold"], nxt["xp"]),
             "%d item%s" % (nxt["items"], "" if nxt["items"] == 1 else "s")]
    if nxt["consumables"]:
        lines.append("%d potions and such" % nxt["consumables"])
    if nxt["points"]:
        lines.append("%d attribute points" % nxt["points"])
    if nxt["heal"]:
        lines.append("Everybody back on their feet")
    for i, line in enumerate(lines[:5]):
        draw_text(surface, assets, line, (box.centerx, box.y + 156 + i * 18),
                  "tiny", GOOD if i >= 2 else DIM, center=True)

    from . import coop as coop_mod
    waiting = (game.coop is not None
               and game.coop.role == coop_mod.CLIENT)
    # **And the host waits for the room** (s84). Everyone answers their
    # own offers after a wave; sending the next one in while somebody is
    # still deciding what they are carrying into it is the same failure
    # decision 4 fixed for wardens.
    reading = [] if waiting else game.waves_waiting_for()
    label = ("Waiting for the host" if waiting
             else "Waiting for %s" % ", ".join(reading[:2]) if reading
             else "Send in wave %d" % run.wave)
    row = pygame.Rect(box.x + 60, box.bottom - 92, box.w - 120, 34)
    row_highlight(surface, row, not waiting and not reading,
                  game=game, kind="wave", index=0)
    draw_text(surface, assets,
              ellipsize(assets.font("small"), label, row.w - 16),
              row.center, "small", DIM if reading else INK, center=True)
    # **What you are carrying, on the screen you sit on between waves**
    # (item 0bs). A boon taken ten waves ago is invisible otherwise, and
    # a wave-long one has to be visible or nobody can plan around it.
    from . import spoils as spoils_mod
    held = spoils_mod.held_buffs(game.player) if game.player else []
    if held:
        # **Above the button, not through it.** The layout checker caught
        # this at every size: `bottom - 66` is inside a row that starts
        # at `bottom - 92` and is 34 tall.
        # **Says it rather than marking it.** An asterisk needs a legend
        # and there is no room for one; "(1 wave)" needs nothing.
        names = ", ".join(("%s (1 wave)" % buff.name) if until is not None
                          else buff.name for buff, until in held)
        draw_text(surface, assets,
                  ellipsize(assets.font("tiny"), "Carrying  " + names,
                            box.w - 40),
                  (box.centerx, box.bottom - 106), "tiny", GOOD, center=True)
    line = "Score so far  %d" % run.score()
    if getattr(run, "bosses", 0):
        # Item 0bt made this number worth showing: it is why the loot is
        # getting better, and a curve nobody can see is a curve nobody
        # believes in.
        line += "    Wardens down  %d" % run.bosses
    draw_text(surface, assets,
              ellipsize(assets.font("tiny"), line, box.w - 40),
              (box.centerx, box.bottom - 48), "tiny", ACCENT, center=True)
    draw_text(surface, assets, "Esc to give up", (box.centerx, box.bottom - 26),
              "tiny", FAINT, center=True)


def draw_wave_over(game):
    """*"if u die u get a score and thats it"* - so this is the score."""
    surface, assets = game.screen, game.assets
    run = game.waves
    overlay(game, 238)
    width, height = surface.get_size()
    box_w = min(520, width - 24)
    box_h = min(height - 40, 320)
    box = pygame.Rect((width - box_w) // 2, (height - box_h) // 2,
                      box_w, box_h)
    panel(surface, box, edge=BAD)

    draw_text(surface, assets, "The stand ends", (box.centerx, box.y + 34),
              "big", BAD, center=True)
    cleared = run.cleared if run else 0
    draw_text(surface, assets, "%d wave%s survived"
              % (cleared, "" if cleared == 1 else "s"),
              (box.centerx, box.y + 78), "small", INK, center=True)
    draw_text(surface, assets, "%d" % (run.score() if run else 0),
              (box.centerx, box.y + 122), "big", ACCENT, center=True)
    best = getattr(game.meta, "best_waves", 0)
    draw_text(surface, assets, "Best so far  %d wave%s"
              % (best, "" if best == 1 else "s"),
              (box.centerx, box.y + 158), "tiny", DIM, center=True)
    if game.player is not None and game.player.debug_used:
        draw_text(surface, assets, "A marked run keeps no record",
                  (box.centerx, box.y + 180), "tiny", BAD, center=True)

    row = pygame.Rect(box.x + 60, box.bottom - 76, box.w - 120, 34)
    row_highlight(surface, row, True, game=game, kind="waveover", index=0)
    draw_text(surface, assets, "Back to the title", row.center, "small",
              INK, center=True)


def draw_lobby(game):
    """Host or join. Deliberately says what it can and cannot reach.

    A player whose friend is not on the same network needs to find that out
    here, in one line, rather than after two minutes of a code that will
    never connect.
    """
    surface, assets, state = game.screen, game.assets, game.ui
    rect = frame(game, "Play together",
                 "Up and Down to choose   Enter to pick   Esc to go back")
    session = game.coop

    if session is None:
        for i, label in enumerate(LOBBY_ROWS):
            row = pygame.Rect(rect.x + 40, rect.y + 110 + i * 44,
                              rect.w - 80, 38)
            row_highlight(surface, row, i == state.lobby_index, game=game,
                          kind="lobby", index=i)
            draw_text(surface, assets, label, (row.x + 18, row.y + 9),
                      "small", INK if i == state.lobby_index else DIM)
        # **What co-op is, before how it connects.** The playtest found
        # both players expecting a room to make a fresh world; it does
        # not, and nothing here said so (s40).
        note = ("You all play in the host's world. The host plays their "
            "own save; everyone else gets a hero that belongs to that "
            "world, starting at level one the first time and waiting for "
            "you every time after. Nobody's save is overwritten.  "
            "Hosting asks your router to open a port so friends can "
            "reach you from anywhere. If it refuses, the room still works "
            "on your own network.")
        # Grows down from under the rows and stops when it runs out of
        # panel, rather than from a fixed offset above the bottom - four
        # lines fitted, six did not, and the extra ones fell off a phone.
        # The sentence that matters is first, so it is the one that
        # survives a short window.
        note_top = rect.y + 110 + len(LOBBY_ROWS) * 44 + 14
        room = max(0, (rect.bottom - 26 - note_top) // 16)
        lines = wrap_text(note, assets.font("tiny"), rect.w - 120)
        for i, line in enumerate(lines[:room]):
            draw_text(surface, assets, line,
                      (rect.centerx, note_top + i * 16), "tiny",
                      FAINT, center=True)
        return

    if state.lobby_mode == "join" and not session.ready:
        draw_text(surface, assets, session.status or "Connecting",
                  (rect.centerx, rect.centery), "mid", ACCENT, center=True)
        return

    # Hosting, or joined: show the room.
    if session.role == "host":
        settled, reach = session.reach
        draw_text(surface, assets, "Room code",
                  (rect.centerx, rect.y + 104), "small", DIM, center=True)
        code = session.room_code
        draw_text(surface, assets, code[:5] + "  " + code[5:],
                  (rect.centerx, rect.y + 140), "big",
                  ACCENT if settled else FAINT, center=True)
        # Say how far the code carries. A player whose router refused needs
        # to know now, not after a friend fails to connect.
        reachable = settled and session.portal is not None \
            and session.portal.global_ready
        for i, line in enumerate(wrap_text(reach, assets.font("tiny"),
                                           rect.w - 140)):
            note_y = rect.y + 176 + i * 15
            if note_y + 14 > min(rect.y + 222, rect.bottom - 140):
                break               # the roster starts here
            draw_text(surface, assets, line, (rect.centerx, note_y), "tiny",
                      GOOD if reachable else FAINT, center=True)

    # The roster starts where there is room for it and stops above the
    # status line, rather than at a fixed offset that assumed a tall frame.
    y = min(rect.y + 222, rect.bottom - 140)
    roster_floor = rect.bottom - 96
    draw_text(surface, assets, "In the room", (rect.centerx, y), "small",
              DIM, center=True)
    y += 30
    for entry in ([{"id": session.player_id, "name": session.name}]
                  + [{"id": r.player_id, "name": r.name}
                     for r in session.remotes.values()]):
        if y > roster_floor:
            break
        mine = entry["id"] == session.player_id
        draw_text(surface, assets,
                  "%s%s" % (entry["name"], "  (you)" if mine else ""),
                  (rect.centerx, y), "small", ACCENT if mine else INK,
                  center=True)
        y += 24

    if session.status:
        draw_text(surface, assets, session.status,
                  (rect.centerx, rect.bottom - 80), "tiny", FAINT,
                  center=True)
    footer = ("Enter to begin   Esc to close the room"
              if session.role == "host" else "Waiting for the host   Esc to leave")
    draw_text(surface, assets, footer, (rect.centerx, rect.bottom - 52),
              "tiny", DIM, center=True)


def draw_join_code(game):
    """Type a room code."""
    surface, assets = game.screen, game.assets
    rect = frame(game, "Join a room", "Enter to connect   Esc to go back")
    typed = game.code_buffer
    shown = (typed + "_" * (10 - len(typed)))[:10]
    draw_text(surface, assets, shown[:5] + "  " + shown[5:],
              (rect.centerx, rect.centery - 20), "big", ACCENT, center=True)
    message = game.ui.lobby_notice or "Ten characters, as your friend read them out."
    draw_text(surface, assets, message, (rect.centerx, rect.centery + 30),
              "tiny", BAD if game.ui.lobby_notice else FAINT, center=True)


def draw_achievements(game):
    """The global cabinet: every feat ever earned, across all saves.

    Distinct from the quest log's list, which is *this run's* progress. Both
    exist on purpose - the run list is the 100% chase, this is the record
    that a chase happened at all.
    """
    surface, assets = game.screen, game.assets
    earned = set(game.meta.achievements)
    rect = frame(game, "Achievements",
                 "Earned across every save   Esc to go back")

    total = len(ACHIEVEMENTS)
    draw_text(surface, assets, "%d of %d" % (len(earned), total),
              (rect.centerx, rect.y + 96), "body",
              GOOD if len(earned) == total else ACCENT, center=True)

    state = game.ui.feats
    top = rect.y + 128
    state.visible = max(3, ((rect.bottom - 60) - top) // 44)
    state.clamp_to(total)
    for i, ach in state.window(ACHIEVEMENTS):
        done = ach.key in earned
        row = pygame.Rect(rect.x + 40, top + (i - state.offset) * 44,
                          rect.w - 80, 38)
        row_highlight(surface, row, i == state.index, game=game,
                      kind="feat", index=i)
        tag = "earned" if done else "locked"
        tail = assets.font("tiny").size(tag)[0] + 24
        draw_text(surface, assets,
                  ellipsize(assets.font("small"), ach.shown_title(done),
                            row.w - 32 - tail),
                  (row.x + 16, row.y + 4), "small", ACCENT if done else FAINT)
        draw_text(surface, assets,
                  ellipsize(assets.font("tiny"), ach.shown_detail(done),
                            row.w - 32 - tail),
                  (row.x + 16, row.y + 21), "tiny", DIM if done else FAINT)
        draw_text(surface, assets, tag, (row.right - 16, row.y + 18), "tiny",
                  GOOD if done else FAINT, right=True)
    scroll_hint(surface, assets, rect, state, total)


def board_lines(meta):
    """A line per gamemode per party size, for every mode that has runs.

    **His shape, and it was built for one mode** (item 0az): *"a global
    score leaderboard for each gamemode for each party size"*. The
    records screen listed Last Stand and nothing else, so a player could
    survive twenty nights and never see the number anywhere - and The
    Hoard would have arrived into the same silence (§88).

    Derived from `gamemodes.MODES`, so a fifth mode appears here the day
    it scores its first run rather than the day somebody remembers.
    """
    from . import gamemodes
    lines = []
    for mode in gamemodes.MODES:
        if not mode.scores or mode.key == "story":
            continue
        sizes = [size for size in meta.board_sizes(mode.key)
                 if meta.board(mode.key, size)]
        for size in sizes:
            best = meta.board(mode.key, size)[0]
            lines.append(("%s, %s" % (mode.name,
                                      "solo" if size == 1
                                      else "%d players" % size),
                          commafy(best.score)))
    return lines


def draw_records(game):
    from . import classes as classes_mod
    surface, assets = game.screen, game.assets
    meta = game.meta
    rect = frame(game, "Records", "Esc to go back")

    done, total = meta.codex_progress()
    left = rect.x + 40
    y = rect.y + 96
    stats = [("Runs finished", meta.totals.get("runs", 0)),
             # Counted against what they are allowed to know exists; a
             # bare total would announce the hidden one.
             ("Endings reached",
              "%d of %d" % (len(meta.endings),
                            len(story.visible_endings(meta.endings)))),
             ("Codex pages", "%d of %d" % (done, total)),
             ("Highest New Game+", meta.ngplus),
             ("Best score", commafy(meta.best_score)),
             # **Its own line, not folded into "Best score"** (0ag, s59).
             # A wave score and a story score measure two different games,
             # and one number for both would make the smaller meaningless.
             ("Last Stand best", "%d wave%s"
              % (getattr(meta, "best_waves", 0),
                 "" if getattr(meta, "best_waves", 0) == 1 else "s")),
             # **A line per party size** (item 0az). His shape: *"a
             # global score leaderboard for each gamemode for each party
             # size"*. The board itself is local until there is
             # somewhere to sync one to (*"no hosted server, ever"*),
             # and this is the half of it that does not need a server -
             # the same list, with fewer people in it.
             ] + board_lines(meta) + [
             ("Monsters felled", commafy(meta.totals.get("kills", 0))),
             ("Wardens felled", meta.totals.get("bosses", 0)),
             ("Gold earned", commafy(meta.totals.get("gold", 0))),
             ("Time on the road",
              format_playtime(meta.totals.get("playtime", 0.0)))]
    # Both columns stop at the bottom of the frame rather than running out
    # through it. The right-hand one has always done this; the left-hand one
    # did not, and on a short window it wrote nine stats and eight classes
    # straight off the screen.
    floor = rect.bottom - 34
    for label, value in stats:
        if y > floor:
            break
        draw_text(surface, assets, label, (left, y), "small", DIM)
        draw_text(surface, assets, str(value), (left + 280, y), "small", INK,
                  right=True)
        y += 26

    if y + 24 <= floor:
        y += 10
        draw_text(surface, assets, "CLASSES", (left, y), "small", ACCENT)
        y += 24
        for cls in classes_mod.CLASSES:
            if y > floor:
                break
            unlocked = classes_mod.is_unlocked(cls.key, meta)
            tiny = assets.font("tiny")
            draw_text(surface, assets,
                      ellipsize(tiny, cls.name if unlocked else "? ? ?", 120),
                      (left, y), "tiny", cls.color if unlocked else FAINT)
            draw_text(surface, assets,
                      ellipsize(tiny,
                                "unlocked" if unlocked else cls.unlock_hint,
                                rect.right - 40 - (left + 130)),
                      (left + 130, y), "tiny", DIM if unlocked else FAINT)
            y += 20

    # Beside the stats on a wide frame, under them on a narrow one. At 390
    # the "right" column worked out to the *left* of the left one, which is
    # what a subtraction does when it runs out of room.
    stacked = is_narrow(rect)
    right = rect.x + 40 if stacked else min(rect.x + 470, rect.right - 300)
    ry = (y + 24) if stacked else (rect.y + 96)
    draw_text(surface, assets, "RECENT ROADS", (right, ry), "small", ACCENT)
    ry += 26
    if not meta.runs:
        draw_text(surface, assets, "No completed runs yet.", (right, ry),
                  "tiny", FAINT)
    for record in list(reversed(meta.runs))[:12]:
        if ry > floor:
            break
        cls = classes_mod.get(record.class_key)
        ending = story.ENDINGS_BY_KEY.get(record.ending)
        # Three columns in a row that may be 260 pixels wide: the score is
        # measured first and the ending title takes what is left.
        tiny = assets.font("tiny")
        score = commafy(record.score)
        # **A wave run is not "unfinished"** (0ag, s59) - it ended the only
        # way it can. It says how far it got instead, which is the whole of
        # what that mode records.
        if ending:
            title = ending.title
        else:
            # **Each mode's own word** (§88): waves, nights, coins. This
            # said *"unfinished"* for everything that was not a Last
            # Stand, so a night that stood twelve and a hoard that
            # carried two hundred home both read as failures.
            from . import gamemodes as _modes
            title = _modes.depth_label(record.mode,
                                       record.waves) or "unfinished"
        mid_x = right + min(150, max(70, (rect.right - 40 - right) // 2))
        draw_text(surface, assets,
                  ellipsize(tiny, "%-12s L%-2d" % (cls.name, record.level),
                            mid_x - right - 8),
                  (right, ry), "tiny", cls.color)
        draw_text(surface, assets,
                  ellipsize(tiny, title,
                            rect.right - 48 - tiny.size(score)[0] - mid_x),
                  (mid_x, ry), "tiny",
                  ACCENT if (ending or record.mode == "waves") else FAINT)
        draw_text(surface, assets, score, (rect.right - 40, ry), "tiny", DIM,
                  right=True)
        ry += 20


# ------------------------------------------------------------- guide book ---

def guide_body_rect(game, rect, narrow, rail_w):
    """Where a guide page's prose goes. One place, two callers."""
    body_x = rect.x + 30 if narrow else rect.x + 30 + rail_w + 24
    return pygame.Rect(body_x, rect.y + 90,
                       max(140, rect.right - 30 - body_x),
                       max(80, rect.bottom - 150 - rect.y + 60))


def guide_measure(game, index=None):
    """A guide page's lines, and how many of them fit. `(wrapped, room)`.

    **Split out because the draw was silently eating the guide**
    (2026-09-03) - the same fault, in the same shape, that s43 found in
    `draw_scene`: the loop stopped when it ran out of panel, and text that
    was never drawn overflows nothing, so `tests/layout.py` reported zero
    for every page at every size for as long as the screen has existed.

    It was not a near miss. On a phone in landscape the book drew **seven
    lines of a forty-two line page**; at 1024x768 the Fighting page drew
    21 of 36. The guide was rebuilt on 2026-08-31 *because Savvydev asked
    for it* (rule 1d), and most of what went in has never been readable
    anywhere but a wide desktop window.

    `event_guide` needs the same numbers to clamp scrolling, so they are
    measured here once rather than twice with two chances to disagree.
    """
    from . import guide
    if index is None:
        index = game.ui.guide_index
    page = guide.PAGES[index % len(guide.PAGES)]
    width, height = game.screen.get_size()
    inset = int(clamp(min(width, height) // 12, 12, 64))
    rect = pygame.Rect(inset, inset, width - inset * 2, height - inset * 2)
    narrow = is_narrow(rect)
    rail_w = 0 if narrow else min(208, max(120, rect.w // 3))
    body = guide_body_rect(game, rect, narrow, rail_w)

    # **Re-flowed as paragraphs, not wrapped line by line.** The pages are
    # written as hand-broken lines of about seventy characters; wrapping
    # each one on its own leaves the tail of every line as a widow, so on a
    # narrow panel the book read as alternating long and three-word rows.
    # Same finding as s43's 600 -> 660, met from the other side: there the
    # panel grew to fit the lines, and here the lines have to stop being
    # lines.
    #
    # **A line that starts with a space is kept as it was.** Three pages
    # hold columns - the nine attributes, the rarity ladder, the errand and
    # favour note - and joining those into prose would turn a table into a
    # sentence.
    paragraphs, buffer = [], []

    def flush():
        if buffer:
            paragraphs.append(" ".join(buffer))
            buffer.clear()

    for line in page.lines:
        if not line:
            flush()
            paragraphs.append("")
        elif line[0] == " ":
            flush()
            paragraphs.append(line)
        else:
            buffer.append(line.strip())
    flush()

    wrapped = []
    for para in paragraphs:
        if not para:
            wrapped.append("")
            continue
        wrapped.extend(wrap_text(para, game.assets.font("small"),
                                 body.w - 44))
    room = max(1, (body.bottom - 26 - (body.y + 54)) // 21)
    if len(wrapped) > room:
        # **The bottom row belongs to "N below" once there is an N.** The
        # note sits under the list the way it does everywhere else, and
        # here the list already reaches the bottom of the panel - so it
        # landed on the last line on three pages, and beside the title on
        # five when it was moved up there instead. Reserving the row is
        # the only placement that is right at every width.
        room = max(1, room - 1)
    return wrapped, room


def guide_scroll_max(game, index=None):
    wrapped, room = guide_measure(game, index)
    return max(0, len(wrapped) - room)


def draw_guide(game):
    """How to play. Contains nothing the story has not already told you.

    **It scrolls** (2026-09-03). See `guide_measure` for what it was doing
    before that and why nothing caught it.
    """
    from . import guide
    if game.player:
        overlay(game)
    surface, assets = game.screen, game.assets
    index = game.ui.guide_index % len(guide.PAGES)
    page = guide.PAGES[index]
    limit = guide_scroll_max(game, index)
    rect = frame(game, "Guide",
                 "Page %d of %d   Left / Right to turn%s"
                 % (index + 1, len(guide.PAGES),
                    "   Up / Down to read on" if limit else ""))

    # Chapter rail down the left, so the whole book is one click away -
    # unless there is no room for it beside the page, in which case the
    # footer's "Left / Right to turn" is the whole navigation and the page
    # gets the frame to itself.
    narrow = is_narrow(rect)
    rail_w = 0 if narrow else min(208, max(120, rect.w // 3))
    rail_top = rect.y + 96
    start, visible, pitch = list_view(rail_top, rect.bottom - 40,
                                      len(guide.PAGES), index)
    for offset in range(0 if narrow else visible):
        i = start + offset
        entry = guide.PAGES[i]
        row = pygame.Rect(rect.x + 30, rail_top + offset * pitch, rail_w,
                          max(18, pitch - 6))
        row_highlight(surface, row, i == index, ACCENT, game=game,
                      kind="guide", index=i)
        draw_text(surface, assets, entry.title,
                  (row.x + 14, row.y + max(1, (row.h - 16) // 2)), "small",
                  INK if i == index else DIM)
    if not narrow:
        scroll_note(surface, assets,
                    pygame.Rect(rect.x + 30, rail_top, rail_w,
                                visible * pitch),
                    start, visible, len(guide.PAGES))

    body = guide_body_rect(game, rect, narrow, rail_w)
    panel(surface, body, alpha=228)
    draw_text(surface, assets, page.title, (body.x + 22, body.y + 16),
              "mid", ACCENT)

    wrapped, room = guide_measure(game, index)
    start = int(clamp(getattr(game.ui, "guide_scroll", 0), 0, limit))
    y = body.y + 54
    for line in wrapped[start:start + room]:
        if line:
            draw_text(surface, assets, line, (body.x + 22, y), "small", INK)
        y += 21
    # A blank line costs a full row here rather than the eleven pixels it
    # used to, because the scroll window counts rows and a half-height one
    # would put the arithmetic and the drawing at different answers - the
    # exact way `scene_measure` and `draw_scene` were able to disagree.
    if limit:
        # In the row `guide_measure` held back for it.
        scroll_note(surface, assets,
                    pygame.Rect(body.x, body.y + 54, body.w - 22,
                                (room - 1) * 21 + 26),
                    start, min(room, len(wrapped) - start), len(wrapped))

    key_hint(surface, assets,
             ([("Up/Down", "Read on")] if limit else [])
             + [("Left/Right", "Turn page"), ("Esc", "Close")],
             (rect.x + 30, rect.bottom - 28), game=game)


# --------------------------------------------------------------- keybinds ---

def draw_keybinds(game):
    """Rebinding screen. Menu keys stay fixed and are shown as such."""
    from . import keybinds as kb
    if game.player:
        overlay(game, 210)
    else:
        overlay(game, 120)
    surface, assets, state = game.screen, game.assets, game.ui
    waiting = state.rebinding is not None
    rect = frame(game, "Controls",
                 "Press the new key..." if waiting
                 else "Enter to rebind   R to reset   Esc to close")

    rows = kb.visible_actions(game.meta.found_devmenu)
    top = rect.y + 96
    # Twelve actions at forty pixels does not fit a landscape phone, so the
    # rows squeeze and then scroll. The cursor stays inside the window
    # because `list_view` centres on it.
    start, visible, row_h = list_view(top, rect.bottom - 74, len(rows),
                                      state.keybind_index)
    for offset in range(visible):
        i = start + offset
        action, label, _default = rows[i]
        row = pygame.Rect(rect.x + 40, top + offset * row_h, rect.w - 80,
                          max(18, row_h - 6))
        active = i == state.keybind_index
        text_y = row.y + max(1, (row.h - 16) // 2)
        row_highlight(surface, row, active, ACCENT, game=game,
                      kind="keybind", index=i)
        draw_text(surface, assets, label, (row.x + 16, text_y), "small",
                  INK if active else DIM)
        if waiting and active:
            draw_text(surface, assets, "press a key...",
                      (row.right - 16, row.y + row.h // 2), "small", GOOD,
                      right=True)
        else:
            draw_text(surface, assets, game.binds.label_for(action),
                      (row.right - 16, row.y + row.h // 2), "small",
                      ACCENT if active else INK, right=True)
    scroll_note(surface, assets,
                pygame.Rect(rect.x + 40, top, rect.w - 80,
                            visible * row_h), start, visible, len(rows))

    note = state.keybind_notice or (
        "Arrow keys always walk. Enter, Esc and Tab always work the menus.")
    for i, line in enumerate(wrap_text(note, assets.font("small"),
                                       rect.w - 80)[:2]):
        draw_text(surface, assets, line,
                  (rect.centerx, rect.bottom - 60 + i * 18), "small",
                  DIM if not state.keybind_notice else GOOD, center=True)
    key_hint(surface, assets,
             [("Enter", "Rebind"), ("R", "Reset all"), ("Esc", "Close")],
             (rect.x + 30, rect.bottom - 28), game=game, hot=False)
