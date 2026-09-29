"""Automated UI overlap and overflow checker.

Instruments every text, panel and sprite draw, renders each screen, and
reports:

  * text drawn outside the window
  * text colliding with other text in the same pass
  * text spilling out of the panel it was drawn into
  * text buried under a panel drawn later
  * **a sprite drawn off the window**
  * **two sprites stacked on each other in a laid-out screen**
  * **a sprite covering text that was drawn before it**

Eyeballing screenshots misses these; a rect intersection test does not.

**The sprite half was missing for a long time and it was expensive.** The
handoff names three bugs it would have caught: a four-hero party drawn
stacked on top of itself, an off-screen battle arrow nobody could see, and
a character-creator preview sitting on its own caption. Every one shipped
past a clean run of this file.

**It is narrower than the text half, on purpose.** In a *world*, things
overlap because that is what a world is - a hero stands in front of a tree
and neither is a bug. In a laid-out screen they do not. So the stacking
rule is off for the overworld and on everywhere else, and screens say which
they are (`world=True` in `check`). A checker that cried about the
overworld every run would be a checker nobody reads.
"""
import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["PIXEL_RPG_SAVE_DIR"] = tempfile.mkdtemp(prefix="rpglayout")
os.environ["PIXEL_RPG_SEED"] = "77"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame

from pixel_rpg import (battlescreen, devmenu, freeroamscreen, game as G,
                       gamepad, menus, touch, ui)
from pixel_rpg.entities import BOSS_DEFS, Boss, Player, make_enemy
from pixel_rpg.items import roll_item
from pixel_rpg.util import rng

TEXTS = []
PANELS = []
SPRITES = []
# **Selection highlights** (2026-09-05, s53). Savvydev, third school
# playtest: *"theres a LOT of places where the text like overlaps the
# selection highlight and stuff, and it doesnt look too good"*.
#
# Nothing here could see it. A highlight is a translucent fill, not a
# panel, so the panel rules never applied to it - a row whose box is
# tighter than its own label overflows nothing this file knew about, and
# "a LOT of places" is what a shared helper drifting looks like from the
# player's chair.
HIGHLIGHTS = []
RECORDING = False

# Surfaces that are scenery rather than a thing in the scene: gradients,
# vignettes, drop shadows. Held by `id`, which is safe because `Assets`
# caches them and a cached surface is never rebuilt at a new address.
DECOR = set()

_real_text = ui.draw_text
_real_panel = ui.panel
_real_highlight = ui.highlight


ORDER = [0]


# A source that spans the whole window in either direction is scenery: a
# backdrop, a gradient wash, a vignette, or one of the title screen's
# horizontal bands. Nothing the player thinks of as a sprite is wall to
# wall. Either direction rather than both, because the bands are 1280 wide
# and 90 tall and the first version of this let all of them through.
SCENERY_SPAN = 0.95

# Functions whose blits are somebody else's business. Text is drawn by
# blitting a rendered surface - twice, because of the shadow pass - and
# the text rules above already cover every one of those far better than a
# sprite rule could. Without this, every label on every screen arrives
# here as two sprites stacked 90% on each other.
#
# Panels are the same story from the other end: they are built on their own
# surface and blitted, they nest inside each other by design, and there are
# already three rules above about what a panel may sit on top of.
#
# A toast is an overlay by definition - it is *supposed* to land on top of
# whatever is underneath, which is the entire idea of a toast. It is in
# here after it flagged every battle screen on a phone, and that finding
# is written up in section 36 rather than thrown away: the checker was
# wrong to fail, and right about what it saw.
NOT_SPRITES = ("ui.draw_text", "ui.draw_text_shadow", "ui.text",
               "ui.panel", "ui.highlight")


class RecordingSurface(pygame.Surface):
    """The screen, but it remembers what was drawn onto it.

    A real `pygame.Surface` subclass rather than a proxy object, because
    `pygame.draw.rect` and friends take a surface and will not accept a
    stand-in. `Surface.blit` cannot be monkeypatched - the type is
    immutable - but it can be overridden in a subclass, which is the whole
    reason this class exists rather than a wrapper around `ui`.
    """

    def blit(self, source, dest, *args, **kwargs):
        rect = pygame.Surface.blit(self, source, dest, *args, **kwargs)
        if RECORDING:
            self._maybe_record(source, dest)
        return rect

    def _maybe_record(self, source, dest):
        if id(source) in DECOR:
            return
        try:
            sw, sh = source.get_size()
        except AttributeError:
            return
        if sw <= 0 or sh <= 0:
            return
        width, height = self.get_size()
        if sw >= width * SCENERY_SPAN or sh >= height * SCENERY_SPAN:
            return                       # scenery, not a thing
        who = _who()
        if who in NOT_SPRITES:
            return
        # The rect `blit` returns is already clipped to the window, so a
        # sprite drawn entirely off it comes back as (0, 0, 0, 0) and there
        # is nothing left to report. Where it was *asked* to go is the
        # thing worth knowing, so that is what gets recorded.
        try:
            x, y = dest[0], dest[1]
        except (TypeError, IndexError):
            return
        ORDER[0] += 1
        SPRITES.append((pygame.Rect(int(x), int(y), sw, sh), who, ORDER[0]))


def _who():
    """The game function that asked for this blit.

    Recorded rather than the line number: a name survives the file being
    edited, and the only thing it is used for is grouping sprites that were
    laid out together. Two heroes placed by the same function are peers in
    a row; a hero and a tree are not.
    """
    frame = sys._getframe(3)             # _who <- _maybe_record <- blit
    for _ in range(6):
        name = frame.f_globals.get("__name__", "")
        if name.startswith("pixel_rpg."):
            return "%s.%s" % (name.rsplit(".", 1)[-1], _qualify(frame))
        if frame.f_back is None:
            break
        frame = frame.f_back
    # Nothing from the game in the stack at all, which happens when the
    # self-test below draws directly. Name the immediate caller instead of
    # shrugging: an unnamed sprite is one nobody can group or find.
    direct = sys._getframe(3)
    return "%s.%s" % (direct.f_globals.get("__name__", "?").rsplit(".", 1)[-1],
                       direct.f_code.co_name)


def _qualify(frame):
    """`Toasts.draw` rather than `draw`.

    `ui.py` has four methods called `draw` and the first version of this
    reported all of them as `ui.draw`, which is the kind of label that
    costs somebody twenty minutes working out which one it meant.
    """
    owner = frame.f_locals.get("self")
    if owner is not None:
        return "%s.%s" % (type(owner).__name__, frame.f_code.co_name)
    return frame.f_code.co_name


def tag_decor(assets):
    """Register this Assets object's scenery so sprites can be told apart."""
    # `tile` is in here for the same reason as `gradient`: the ground is
    # scenery. Without it the overworld reports about twelve hundred
    # sprites a frame and every one of them is a patch of grass.
    for name in ("gradient", "vignette", "shadow", "tile"):
        original = getattr(assets, name, None)
        if original is None or getattr(original, "_tagged", False):
            continue

        def wrap(fn):
            def tagged(*args, **kwargs):
                out = fn(*args, **kwargs)
                DECOR.add(id(out))
                return out
            tagged._tagged = True
            return tagged

        setattr(assets, name, wrap(original))


def rec_text(surface, assets, text, pos, font="body", color=ui.INK,
             center=False, shadow=True, right=False):
    rect = _real_text(surface, assets, text, pos, font, color, center,
                      shadow, right)
    if RECORDING and str(text).strip():
        ORDER[0] += 1
        TEXTS.append((pygame.Rect(rect), str(text), font, ORDER[0]))
    return rect


def rec_panel(surface, rect, *args, **kwargs):
    out = _real_panel(surface, rect, *args, **kwargs)
    if RECORDING:
        ORDER[0] += 1
        PANELS.append((pygame.Rect(out), ORDER[0]))
    return out


def rec_highlight(surface, rect, *args, **kwargs):
    out = _real_highlight(surface, rect, *args, **kwargs)
    if RECORDING:
        ORDER[0] += 1
        HIGHLIGHTS.append((pygame.Rect(rect), ORDER[0]))
    return out


# Every module that draws must be listed here. One that is missing does
# not fail - it silently reports "0 texts, 0 panels", which reads as a
# pass. devmenu was added and did exactly that until it was listed.
for module in (menus, battlescreen, devmenu, freeroamscreen, G, touch,
               ui, gamepad):
    if hasattr(module, "draw_text"):
        module.draw_text = rec_text
    if hasattr(module, "panel"):
        module.panel = rec_panel
    if hasattr(module, "highlight"):
        module.highlight = rec_highlight


def start():
    global RECORDING
    TEXTS.clear()
    PANELS.clear()
    SPRITES.clear()
    HIGHLIGHTS.clear()
    ORDER[0] = 0
    RECORDING = True


def stop():
    global RECORDING
    RECORDING = False


def analyse(label, screen_size, allow_overlap=(), world=False):
    """Return a list of human-readable problems for the recorded pass."""
    problems = []
    width, height = screen_size
    bounds = pygame.Rect(0, 0, width, height)

    for rect, text, font, _seq in TEXTS:
        if not bounds.contains(rect):
            if rect.right > width or rect.bottom > height or rect.x < 0 \
                    or rect.y < 0:
                problems.append("OFFSCREEN  %-28r at %s (window %dx%d)"
                                % (text[:26], tuple(rect), width, height))

    # Text vs text collisions in the same pass.
    for i in range(len(TEXTS)):
        ra, ta, _fa, _sa = TEXTS[i]
        for j in range(i + 1, len(TEXTS)):
            rb, tb, _fb, _sb = TEXTS[j]
            if not ra.colliderect(rb):
                continue
            clip = ra.clip(rb)
            area = clip.w * clip.h
            smaller = min(ra.w * ra.h, rb.w * rb.h)
            if smaller <= 0 or area / smaller < 0.22:
                continue          # a couple of pixels of kerning is fine
            pair = tuple(sorted((ta[:20], tb[:20])))
            if pair in allow_overlap:
                continue
            problems.append("COLLIDE    %-26r  x  %-26r  (%d%% of the smaller)"
                            % (ta[:24], tb[:24], 100 * area / smaller))

    # A panel drawn after some text and covering it hides that text. The
    # first version compared text only with other text, so an item card laid
    # over the shop gold panel went unnoticed.
    for rect, text, font, seq in TEXTS:
        for panel_rect, panel_seq in PANELS:
            if panel_seq <= seq or not panel_rect.colliderect(rect):
                continue
            clip = panel_rect.clip(rect)
            area = clip.w * clip.h
            if area and area / max(1, rect.w * rect.h) > 0.35:
                problems.append("BURIED     %-26r under a later panel %s"
                                % (text[:24], tuple(panel_rect)))
                break

    # Text spilling out of its panel.
    for rect, text, font, _seq in TEXTS:
        holders = [p for p, _s in PANELS
                   if p.collidepoint(rect.centerx, rect.centery)]
        if not holders:
            continue
        holder = min(holders, key=lambda p: p.w * p.h)
        if rect.bottom > holder.bottom + 2 or rect.right > holder.right + 2 \
                or rect.x < holder.x - 2 or rect.y < holder.y - 2:
            problems.append("SPILLS     %-26r out of panel %s"
                            % (text[:24], tuple(holder)))

    problems += highlight_problems()
    problems += sprite_problems(bounds, world)
    return problems


# How far a label may poke out of the highlight behind it before it reads
# as sloppy rather than as a margin. Two pixels is the same tolerance the
# panel rule uses, and text carries a one-pixel drop shadow.
HIGHLIGHT_SLACK = 2


def highlight_problems():
    """Text that crosses the edge of the selection box behind it.

    **Savvydev, third school playtest** (s53): *"theres a LOT of places where
    the text like overlaps the selection highlight and stuff, and it
    doesnt look too good"*.

    A highlight is a translucent fill rather than a panel, so none of the
    rules above ever applied to one: a row whose box is tighter than its
    own label overflows nothing, collides with nothing, and is buried
    under nothing. It just looks wrong, on every screen with a list on it,
    which is exactly the "a LOT of places" he saw.

    The rule is deliberately narrow: a label either sits **inside** the box
    that is highlighting it or **clear of it**. Straddling the edge is the
    only case reported, so a highlight drawn over a whole region - a
    battle target marker, a hovered card - never fires, because the text
    it covers is wholly inside it.
    """
    problems = []
    for box, _seq in HIGHLIGHTS:
        for rect, text, _font, _tseq in TEXTS:
            if not box.colliderect(rect):
                continue
            clip = box.clip(rect)
            area = clip.w * clip.h
            if not area or area / max(1, rect.w * rect.h) > 0.98:
                continue                      # wholly inside: fine
            over = max(rect.right - box.right, box.x - rect.x,
                       rect.bottom - box.bottom, box.y - rect.y)
            if over <= HIGHLIGHT_SLACK:
                continue
            problems.append(
                "HALF-LIT   %-26r pokes %dpx out of the highlight %s"
                % (text[:24], over, tuple(box)))
    return problems


# How much of a sprite has to be off the window before it counts as lost.
# Not zero: plenty of things legitimately bleed a few pixels past an edge,
# and a rule that fires on those is a rule that gets switched off.
LOST_FRACTION = 0.5
# How much two sprites in the same row have to overlap before it is a
# stack rather than a deliberate huddle. The four-hero party bug was 100%%.
STACKED_FRACTION = 0.5
# How much of a text a later sprite has to cover before it is hidden.
# The same 0.35 the BURIED rule uses for a later *panel*, deliberately:
# it is the same question - something opaque landed on a label - and two
# different numbers for it would only ever be an accident. The self-test
# is what noticed they had drifted apart.
COVERED_FRACTION = 0.35


def sprite_problems(bounds, world):
    """The three things that have actually gone wrong with sprites here."""
    problems = []

    # 1. Drawn where nobody can see it.
    #
    #    **Not applied to the world at all**, and that is a decision rather
    #    than an oversight. The overworld deliberately draws a row beyond
    #    each edge so that things scroll in already complete instead of
    #    popping into existence, so a dozen fully-off-screen props every
    #    frame is the culling working. The bug this rule exists for - an
    #    arrow nobody could see - was on the battle screen, which is laid
    #    out rather than roamed.
    if not world:
        for rect, who, _seq in SPRITES:
            clip = rect.clip(bounds)
            visible = (clip.w * clip.h) / max(1, rect.w * rect.h)
            if visible < LOST_FRACTION:
                problems.append(
                    "SPRITE OFF %-26s at %s (%d%% visible)"
                    % (who, tuple(rect), 100 * visible))

    # 2. Two sprites the same function placed, on top of each other. Only
    #    peers: a hero and a tree are placed by the same loop and are meant
    #    to overlap, which is why this is off for the world entirely.
    if not world:
        for i in range(len(SPRITES)):
            ra, wa, _sa = SPRITES[i]
            for j in range(i + 1, len(SPRITES)):
                rb, wb, _sb = SPRITES[j]
                if wa != wb or not ra.colliderect(rb):
                    continue
                clip = ra.clip(rb)
                area = clip.w * clip.h
                smaller = min(ra.w * ra.h, rb.w * rb.h)
                if smaller <= 0 or area / smaller < STACKED_FRACTION:
                    continue
                problems.append(
                    "SPRITES ON %-26s %s on %s (%d%% of the smaller)"
                    % (wa, tuple(ra), tuple(rb), 100 * area / smaller))

    # 3. A sprite dropped on top of text that was already there. This is
    #    the character-creator preview sitting on its own caption, and it
    #    is the one rule that needs the draw order the recorder keeps.
    for rect, text, _font, seq in TEXTS:
        for sprite_rect, who, sprite_seq in SPRITES:
            if sprite_seq <= seq or not sprite_rect.colliderect(rect):
                continue
            clip = sprite_rect.clip(rect)
            area = clip.w * clip.h
            if area and area / max(1, rect.w * rect.h) > COVERED_FRACTION:
                problems.append(
                    "COVERED    %-26r under a later sprite from %s"
                    % (text[:24], who))
                break
    return problems


def build_game(size=(1280, 720)):
    os.environ["PIXEL_RPG_WIDTH"] = str(size[0])
    os.environ["PIXEL_RPG_HEIGHT"] = str(size[1])
    g = G.Game()
    pygame.display.set_mode(size)
    # The game draws onto a surface that reports every blit, rather than
    # onto the display. Nothing here is ever shown - the video driver is
    # `dummy` - so what the display surface holds does not matter, and this
    # is the only way to see a sprite go down.
    g.screen = RecordingSurface(size)
    tag_decor(g.assets)
    g.camera.resize(*size)
    return g


def populate(g):
    """A mid-run save with plenty of everything, to stress every list."""
    g.pending_class = "emberwright"
    g.new_run("Ysoldebane", 1)
    g.dialogue = None
    g.state = G.OVERWORLD
    g.player.gain_xp(30000)
    g.player.gold = 123456
    for _ in range(18):
        g.player.add_item(roll_item(g.player.level + 3))
    for key in ("potion", "hipotion", "ether", "elixir", "antidote",
                "phoenix", "bomb", "smoke", "whetstone", "wardstone"):
        g.player.add_consumable(key, 9)
    for relic in ("memory_callun", "memory_ilan", "roadstone", "echo"):
        g.player.add_relic(relic)
    g.ui.refresh_shop(g.player, g.world)
    for quest in ("cull", "scavenger", "warden1"):
        g.quests.accept(quest, g.player)
    g.player.stats_tracker.update(kills=143, bosses=3, chests=12, crits=88,
                                  steps=9021, deaths=2, damage=182344,
                                  gold_earned=48211)
    for entry in ("valley", "anchor", "bram", "memory_callun", "memory_dorn",
                  "memory_ilan", "maren", "letters"):
        g.meta.unlock_codex(entry)
    g.meta.record_ending("seventh")
    from pixel_rpg.meta import RunRecord
    for i in range(6):
        g.meta.finish_run(RunRecord("cutpurse", "seventh", 30 + i,
                                    12000 + i * 900, i, 3600.0,
                                    ("glass", "haunted"), "2026-08-1%d" % i))
    g.toast("A reasonably long toast message here", ui.ACCENT, 99)
    g.toast("Another one, stacked below it", ui.GOOD, 99)
    # A toast slides in from the right, so at age zero it is *deliberately*
    # off the window - that is the first frame of the animation, not a
    # layout. Measuring it there says nothing true about either the text or
    # the sprite rules, so age them past the slide and check them where a
    # player actually reads them. The sprite rule is what noticed; the text
    # rules had been measuring the wrong place quietly all along.
    for toast in g.toasts.items:
        toast.age = 0.5
    _fill_the_slots(g)
    return g


def _fill_the_slots(g):
    """Put real runs in the save slots, so the picker has rows to measure.

    **The slot picker had never been measured with anything in it.** This
    harness points at an empty temp directory and only ever builds a run in
    memory, so `draw_slots` was drawing six rows that said "empty" and
    reporting zero at every size - while the populated row, which carries a
    name, a level, a gold figure, a warden count, a playtime and a
    timestamp, went unchecked at any width. Found by screenshotting the
    veteran door (rule 31.4); the same row is what that door asks about.
    """
    # **This must not disturb a single thing `populate` set up**, and two
    # of them are easy to miss. `new_run` raises a toast of its own, which
    # lands at age zero mid-slide and reads to the sprite rule as a panel
    # hanging off the window. And it draws from the shared RNG, which moves
    # the stream - the particle positions three screens later came out on
    # top of each other, which is s21's lesson in a different costume.
    keep = (g.player, g.world, g.quests, g.slot, g.state,
            list(g.toasts.items))
    rng_state = rng._r.getstate()
    for slot, name, level, beaten in ((1, "Ysoldebane", 27, True),
                                      (2, "Bartholomew123456", 3, False)):
        g.pending_class = "emberwright"
        g.new_run(name, slot)
        g.dialogue = None
        g.player.gain_xp(30000 if level > 20 else 400)
        g.player.gold = 123456
        g.player.beaten = beaten
        g.playtime = 34567.0
        g.save_run(silent=True, force=True)
    (g.player, g.world, g.quests, g.slot, g.state,
     g.toasts.items) = keep
    rng._r.setstate(rng_state)


def check(label, size, draw_fn, allow=(), world=False):
    start()
    try:
        draw_fn()
    finally:
        stop()
    problems = analyse(label, size, allow, world)
    print("  %s %-26s %3d texts, %d panels, %d sprites"
          % ("ok  " if not problems else "FAIL", label, len(TEXTS),
             len(PANELS), len(SPRITES)))
    for line in problems[:5]:
        print("        %s" % line)
    if len(problems) > 5:
        print("        ... and %d more" % (len(problems) - 5))
    return problems


def _chapter_card(g):
    """The card mid-hold, over the valley it darkens (s19 item 10)."""
    from pixel_rpg import story
    g.draw_overworld()
    was_card, was_time = g.chapter_card, g.chapter_time
    g.chapter_card, g.chapter_time = story.chapter_for(2), 1.0
    try:
        menus.draw_chapter_card(g)
    finally:
        g.chapter_card, g.chapter_time = was_card, was_time


def _quest_tab(g, tab):
    was, g.ui.quest_tab = g.ui.quest_tab, tab
    try:
        menus.draw_quests(g)
    finally:
        g.ui.quest_tab = was


def _slots_mode(g, mode):
    """The picker answers three different questions and draws each of them
    differently (s19 5b, 5c). Checking one mode checked one of them."""
    was, g.ui.slot_mode = g.ui.slot_mode, mode
    try:
        menus.draw_slots(g)
    finally:
        g.ui.slot_mode = was


def _spoils(g, kind, detail=False):
    """The offer screen, with one offer of a known kind on it (item 0bs).

    Built here rather than by playing a wave, because a wave's offers are
    random and a checker that measures a different screen every run is a
    checker whose green means nothing.
    """
    from pixel_rpg import spoils as spoils_mod
    from pixel_rpg.items import roll_item
    from pixel_rpg.skills import ALL_SKILLS
    was = rng._r.getstate()
    if kind == "buff":
        cards = spoils_mod.BUFFS[:4]
        offer = spoils_mod.Offer("buff", list(cards), "Choose a boon",
                                 "Some last the run.")
    elif kind == "skill":
        offer = spoils_mod.Offer("skill", list(ALL_SKILLS[:4]), "Learn one",
                                 "Whatever you take, you keep.")
    elif kind == "mixed":
        offer = spoils_mod.Offer(
            "mixed", [("buff", spoils_mod.BUFFS[0]),
                      ("item", roll_item(9, slot="weapon")),
                      ("skill", ALL_SKILLS[0]),
                      ("buff", spoils_mod.BUFFS[5])],
            "Take one", "The night does not wait.")
    else:
        offer = spoils_mod.Offer(
            "item", [roll_item(9, slot=slot) for slot in
                     ("weapon", "armor", "accessory", "weapon")],
            "Take one", "The rest are left where they fell.")
    g.spoils, g.spoil_at = [offer], 0
    g.ui.spoil_index = 1
    g.ui.spoil_detail = 1 if detail else None
    try:
        menus.draw_spoils(g)
    finally:
        # **Put the game back and put the dice back.** Rolling four items
        # to build this screen re-phases the shared stream, and every
        # screen measured after it then measures a different game - which
        # is how five new entries here turned four *battle* screens red.
        g.spoils, g.spoil_at = [], 0
        g.ui.spoil_index, g.ui.spoil_detail = 0, None
        rng._r.setstate(was)


def _full_boards(g):
    """The records screen with every gamemode scored at every size."""
    import time
    from pixel_rpg import gamemodes, meta as meta_mod
    was = list(g.meta.runs)
    try:
        for mode in gamemodes.MODES:
            if not mode.scores or mode.key == "story":
                continue
            for party in (1, 2, 3, 4):
                g.meta.runs.append(meta_mod.RunRecord(
                    class_key="warden", level=40, score=1_234_567,
                    playtime=3600.0, modifiers=(),
                    when=time.strftime("%Y-%m-%d %H:%M"),
                    mode=mode.key, party=party, waves=999))
        menus.draw_records(g)
    finally:
        g.meta.runs = was


def screen_suite(g, size):
    from pixel_rpg import story
    total = 0

    basic = [
        ("title", lambda: (menus.draw_title_backdrop(g), menus.draw_title(g))),
        ("slots", lambda: _slots_mode(g, "load")),
        ("slots veteran", lambda: _slots_mode(g, "veteran")),
        ("slots host", lambda: _slots_mode(g, "coop")),
        # s7: a screen missing from this list is a silent pass. The
        # chapter card draws over the world and has never been measured.
        ("chapter card", lambda: _chapter_card(g)),
        # s7 again: a screen missing from this list is a silent pass.
        ("world map", lambda: menus.draw_world_map(g)),
        ("class select", lambda: menus.draw_class_select(g)),
        ("modifier select", lambda: menus.draw_modifier_select(g)),
        # s7: a screen missing from this list is a silent pass. Every
        # kind of offer, and the reader panel open - which is the state
        # that draws two things on top of each other on purpose and so
        # is the one worth measuring (item 0bs).
        ("spoils skill", lambda: _spoils(g, "skill")),
        ("spoils item", lambda: _spoils(g, "item")),
        ("spoils buff", lambda: _spoils(g, "buff")),
        ("spoils night", lambda: _spoils(g, "mixed")),
        ("spoils reading a boon", lambda: _spoils(g, "buff", True)),
        # s7: a screen missing from this list is a silent pass. The
        # arena picker (item 0br) is the only screen here that is a
        # *grid*, so it is the only one whose contents can run off
        # the bottom as well as off the side - which is exactly what
        # its first cut did.
        ("arena select", lambda: menus.draw_arena_select(g)),
        ("character creator", lambda: menus.draw_name_entry(g)),
        ("hud", lambda: (g.draw_overworld(), g.draw_hud())),
        ("bag", lambda: menus.draw_inventory(g)),
        ("shop buy", lambda: menus.draw_shop(g)),
        ("smith", lambda: menus.draw_smith(g)),
        ("hero sheet", lambda: menus.draw_character(g)),
        ("quests", lambda: _quest_tab(g, 1)),
        # s7: a tab the checker never selects is a tab it never looked at.
        ("quests story", lambda: _quest_tab(g, menus.STORY_TAB)),
        ("quests feats", lambda: _quest_tab(g, 4)),
        ("pause", lambda: menus.draw_pause(g)),
        ("settings", lambda: menus.draw_settings(g)),
        ("records", lambda: menus.draw_records(g)),
        # **And with every board filled** (§88). The screen lists a line
        # per gamemode per party size, so four modes at four sizes is
        # sixteen rows it never had before - and the classes list under
        # them is the thing that gets pushed off.
        ("records (every board)", lambda: _full_boards(g)),
    ]
    # The overworld is a world and not a layout (see `sprite_problems`);
    # the chapter card draws over it, so it inherits the exemption (s36).
    worlds = ("hud", "chapter card")
    for label, fn in basic:
        total += len(check(label, size, fn, world=label in worlds))

    # Every creator row selected, and a few different looks.
    from pixel_rpg.appearance import Appearance
    for index in range(len(menus.CREATOR_ROWS)):
        g.ui.creator_index = index
        g.pending_appearance = Appearance(index % 8, index % 10, index % 5,
                                          index % 8)
        total += len(check("creator row %d" % index, size,
                           lambda: menus.draw_name_entry(g)))
    g.ui.creator_index = 0

    # **The picker with the cursor on each kind of thing** (item 0br). Its
    # two buttons sit below the grid and are not in it, so a cursor there
    # is a different layout from a cursor on a thumbnail - and the page a
    # cursor at the end scrolls to is a third.
    from pixel_rpg import gamemodes as gm
    for at, label in ((0, "first"), (len(gm.ARENAS) - 1, "last"),
                      (len(gm.ARENAS), "random"),
                      (len(gm.ARENAS) + 1, "changes")):
        g.ui.arena_index = at
        total += len(check("arena select %s" % label, size,
                           lambda: menus.draw_arena_select(g)))
    g.ui.arena_index = 0
    g.ui.arena_row = None

    # The guide, and the smith in both modes.
    from pixel_rpg import guide as guide_mod
    for i in range(len(guide_mod.PAGES)):
        g.ui.guide_index = i
        total += len(check("guide %d" % i, size, lambda: menus.draw_guide(g)))
    g.ui.guide_index = 0

    g.player.materials = 40
    for mode in ("refine", "salvage"):
        g.ui.smith_mode = mode
        total += len(check("smith %s" % mode, size,
                           lambda: menus.draw_smith(g)))
    g.ui.smith_mode = "refine"

    for waiting in (None, "bag"):
        g.ui.rebinding = waiting
        g.ui.keybind_notice = "swapped with Hero" if waiting else ""
        total += len(check("keybinds %s" % ("waiting" if waiting else "idle"),
                           size, lambda: menus.draw_keybinds(g)))
    g.ui.rebinding = None
    g.ui.keybind_notice = ""

    g.ui.quest_readonly = True
    g.ui.quest_notice = ("Find Elder Maren or one of her runners.", ui.BAD)
    total += len(check("quests read-only", size, lambda: menus.draw_quests(g)))
    g.ui.quest_readonly = False
    g.ui.quest_notice = None

    g.ui.shop_mode = "sell"
    total += len(check("shop sell", size, lambda: menus.draw_shop(g)))
    g.ui.shop_mode = "buy"
    g.ui.inv_tab = 4
    total += len(check("bag consumables", size,
                       lambda: menus.draw_inventory(g)))
    g.ui.inv_tab = 0

    for i, chapter in enumerate(story.CHAPTERS):
        g.ui.codex_tab = i
        total += len(check("codex %s" % chapter[:14], size,
                           lambda: menus.draw_codex(g)))
    g.ui.codex_tab = 0
    for entry in story.CODEX:
        g.meta.unlock_codex(entry.key)
    longest = max(story.CODEX, key=lambda c: sum(len(l) for l in c.body))
    total += len(check("codex page", size,
                       lambda: menus.draw_codex_page(g, longest)))

    dialogues = [("prologue", {"title": "The Sixth Road",
                               "body": story.PROLOGUE, "confirm": "Begin"})]
    for warden in story.WARDENS:
        short = warden.true_name.split()[0]
        dialogues.append(("warden %s" % short,
                          {"title": warden.true_name,
                           "body": list(warden.intro)
                                   + ["", "Recommended level 30.  You are 28.",
                                      "There is no fleeing a warden."],
                           "confirm": "Draw your weapon"}))
        dialogues.append(("death %s" % short,
                          {"title": warden.true_name,
                           "body": list(warden.death), "confirm": "Stand up"}))
    for beaten, lines in story.MAREN_BRIEFINGS.items():
        dialogues.append(("maren %d" % beaten,
                          {"title": "Elder Maren", "body": list(lines),
                           "confirm": "Say nothing"}))
    dialogues.append(("bram letter",
                      {"title": "Bram Vance",
                       "body": ["You have it. You have his hand on it.", "",
                                "No. Do not read it to me. Give it here.", "",
                                "He was not angry. You said that already.",
                                "Say it once more and then go, please."],
                       "confirm": "Leave him to it"}))
    # **Every errand ask, in the longest shape it is ever shown in** (s44):
    # somebody's reason, the counter line and the purse line, under a name
    # as long as the world produces. These are the tallest dialogues in the
    # game and the ones that made `draw_dialogue` scroll.
    for voice in story.ERRAND_VOICE:
        dialogues.append(("errand %s" % voice.key,
                          {"title": "Somebody of The Drowned Rows",
                           "body": list(voice.ask)
                                   + ["", "Earn 2000 gold on the road.", "",
                                      "340 gold and 360 experience, when it "
                                      "is done."],
                           "confirm": "Take it on"}))
    for name, lines in list(story.HAMLET_LINES.items())[:3]:
        dialogues.append(("hamlet %s" % name,
                          {"title": "A woman of %s" % name,
                           "body": list(lines), "confirm": "Nod"}))
    # **The ruin and circle markers** (s55, item 12b). Their titles are a
    # whole sentence rather than a name, which makes them the longest
    # titles any dialogue in the game carries - and s44 records the
    # dialogue title being the thing that walked off a narrow window.
    # `What Is Left of Hale` is the longest body as well, so it is in
    # here by name rather than by slicing the first few.
    # Built the way `World._place_marker` builds them: the place name is
    # the title and the stone is the first body line. `What Is Left of
    # Hale` is the longest title and the longest body in the set.
    for name in ("What Is Left of Hale", "The Drowned Rows"):
        what, lines = story.RUIN_MARKERS[name]
        dialogues.append(("marker %s" % name,
                          {"title": name,
                           "body": [what, ""] + list(lines),
                           "confirm": "Leave it standing"}))
    what, lines = story.STONE_READINGS[0]
    dialogues.append(("marker stones",
                      {"title": "Standing stones",
                       "body": [what, ""] + list(lines),
                       "confirm": "Leave it standing"}))
    for label, data in dialogues:
        g.dialogue = data
        total += len(check("dlg %s" % label, size,
                           lambda: menus.draw_dialogue(g)))
    # The scroll hint only exists on a window too short for the body, and
    # it is drawn between the title and the first line - which is the one
    # place on this screen where two pieces of text can collide.
    g.dialogue = dict(dialogues[-4][1])
    g.dialogue["scroll"] = 99
    total += len(check("dlg scrolled", size,
                       lambda: menus.draw_dialogue(g)))
    g.dialogue = None

    g.world.hollow_key = True
    for portal in g.world.portals:
        portal.defeated = True
    g.open_ending_choice()
    total += len(check("ending choice", size,
                       lambda: menus.draw_ending_choice(g)))
    for ending in story.ENDINGS:
        g.ending = ending
        g.last_score = 128400
        total += len(check("end %s" % ending.key, size,
                           lambda: menus.draw_ending_text(g)))
    g.ending = None
    g.state = G.OVERWORLD

    g.player.full_restore()
    g.start_battle(make_enemy(20, "forest", g.settings.mods))
    for _ in range(160):
        g.battle.update(1 / 60)
        if g.battle.phase == "menu":
            break
    total += len(check("battle menu", size,
                       lambda: battlescreen.draw_battle(g)))
    g.battle.phase = "submenu"
    g.battle.sub_kind = "skill"
    total += len(check("battle skills", size,
                       lambda: battlescreen.draw_battle(g)))
    g.battle.sub_kind = "item"
    total += len(check("battle items", size,
                       lambda: battlescreen.draw_battle(g)))
    g.battle = None

    g.player.full_restore()
    g.start_battle(Boss(BOSS_DEFS[5], g.settings.mods))
    for _ in range(220):
        g.battle.update(1 / 60)
        if g.battle.phase == "menu":
            break
    total += len(check("battle boss", size,
                       lambda: battlescreen.draw_battle(g)))
    g.battle = None

    # The crowded case: five monsters plus a target marker is where the enemy
    # plate and the log are most likely to be walked over.
    from pixel_rpg import scenes as scenes_mod
    # A scene mid-question, and the same scene mid-reply.
    g.play_scene("door")
    if g.scene:
        total += len(check("scene question", size,
                           lambda: menus.draw_scene(g)))
        g.scene["phase"] = "reply"
        g.scene["reply"] = g.scene["options"][0].reply
        total += len(check("scene reply", size,
                           lambda: menus.draw_scene(g)))
    g.scene = None

    # **The widest and tallest scene in the game, and its scroll hint**
    # (s43). The Door above is four rows and short prose; a resolution
    # scene is four rows and thirteen wrapped lines, which is where the
    # scroll indicator and the answers are closest together. The scrolled
    # frame is here because the hint only exists in it.
    g.meta.secrets = sorted(set(scenes_mod.HEARD) | {scenes_mod.ALL_HEARD})
    g.play_scene("res_cut")
    if g.scene:
        total += len(check("resolution scene", size,
                           lambda: menus.draw_scene(g)))
        g.scene["scroll"] = 99      # the draw clamps it to the last page
        total += len(check("resolution scene scrolled", size,
                           lambda: menus.draw_scene(g)))
    g.scene = None

    # **A favour, which is a scene built at the point of asking** (s44) -
    # so it never appeared in any list the checker walks. The title is an
    # NPC's name here, which is the longest one the world produces and the
    # thing that used to run off the panel.
    for favour in (story.FAVOUR_BY_KEY["bread"],
                   story.FAVOUR_BY_KEY["catches"]):
        g.scene = {
            "scene": scenes_mod.Scene(
                "favour_%s" % favour.key, "Somebody of The Drowned Rows",
                "The Valley", tuple(favour.prompt), ()),
            "options": [scenes_mod.Choice(str(i), label, tuple(reply))
                        for i, (label, reply, _c) in enumerate(
                            favour.options)],
            "index": 0, "phase": "choosing", "reply": (), "scroll": 0,
            "favour": {"key": favour.key, "spot": (0, 0), "right": [0]},
            "after": None,
        }
        total += len(check("favour %s" % favour.key, size,
                           lambda: menus.draw_scene(g)))
    g.scene = None

    # **A wayside, which is a scene built at the point of stopping**
    # (item 12, s72) - the same hole the favours were in, and the same
    # fix. Its prose is longer than a favour's and it carries up to three
    # options, so it is the tallest of the built-at-runtime scenes.
    # **All of them, not one**: the checker walking a single example is
    # what let the guide ship seven readable lines of a forty-two line
    # page (s51).
    for stop in story.WAYSIDES:
        g.scene = {
            "scene": scenes_mod.Scene(
                "wayside_%s" % stop.key, stop.title, "The Road",
                tuple(stop.prompt), ()),
            "options": [scenes_mod.Choice(str(i), label, tuple(reply))
                        for i, (label, reply, _e) in enumerate(stop.options)],
            "index": 0, "phase": "choosing", "reply": (), "scroll": 0,
            "wayside": {"key": stop.key, "spot": (0, 0), "effects": []},
            "after": None,
        }
        total += len(check("wayside %s" % stop.key, size,
                           lambda: menus.draw_scene(g)))
        g.scene["phase"] = "reply"
        g.scene["reply"] = g.scene["options"][0].reply
        total += len(check("wayside %s reply" % stop.key, size,
                           lambda: menus.draw_scene(g)))
    g.scene = None
    g.state = G.OVERWORLD

    # The story record, with one road taken and others passed over.
    g.meta.scene_seen = {"prologue": ["accept", "who", "cost"],
                         "warden_callun": ["silent", "listen", "field"]}
    g.meta.scene_taken = {"prologue": ["who"], "warden_callun": ["listen"]}
    total += len(check("story record", size, lambda: menus.draw_story(g)))

    # **The full-bag question** (s48). A screen missing from this list is
    # a silent pass (s7), and this one is a list beside a card - the two
    # things this checker is best at catching overlapping.
    from pixel_rpg.items import roll_item as _roll
    g.ui.fullbag = {"item": _roll(24, rarity="legendary"),
                    "source": "the chest"}
    g.ui.fullbag_index = 2
    total += len(check("full bag", size, lambda: menus.draw_full_bag(g)))
    g.ui.fullbag = None

    # **The new-skill question** (s53, item 0ac). A card, a list and a
    # description panel beside it - and the heading between the first two
    # landed on the card's border until a screenshot said so.
    from pixel_rpg.skills import SKILLS_BY_KEY as _SKILLS
    g.ui.newskill = {"skill": _SKILLS["judgment"],
                     "carried": [s.key for s in g.player.skills()]}
    g.ui.newskill_index = 1
    total += len(check("new skill", size, lambda: menus.draw_new_skill(g)))
    g.ui.newskill = None

    # **The trade table** (s50). Two offer panels beside a list of
    # everything a hero owns, with a name that arrives off a socket in the
    # title - the three things this checker exists to catch, on one screen.
    # Loaded up on purpose: a long name, worn gear, a full offer both ways.
    from pixel_rpg import trade as _trade
    g.trade = _trade.TradeSession(2, "Wanderingsmith")
    g.player.gold = 4820
    g.player.consumables = {"potion": 9, "ether": 4, "elixir": 2}
    for _ in range(6):
        g.player.inventory.append(_roll(30, rarity="epic"))
    g.trade.mine = _trade.Offer(2400, g.player.inventory[:3],
                                {"potion": 9, "ether": 4})
    g.trade.theirs = _trade.Offer(999999, [_roll(40, rarity="mythic")
                                           for _ in range(4)],
                                  {"elixir": 2, "phoenix": 1})
    g.trade.theirs_locked = True
    g.trade.index = 4
    total += len(check("trade", size, lambda: menus.draw_trade(g)))
    g.trade.note = "your bag has no room for that"
    g.trade.mine_locked = True
    g.trade.index = len(g.trade_rows()) - 2
    total += len(check("trade agreed", size, lambda: menus.draw_trade(g)))
    g.trade = None

    # **Inside a house** (s56, item 18). A whole world the checker has
    # never drawn - and s7's rule is that a screen missing from this list
    # is a silent pass.
    _island, _at = g.world, (g.player.x, g.player.y)
    _door = next((k for k, v in g.world.doors.items() if v == "home"), None)
    if _door is not None:
        # **Put back the toast queue** - s7's rule for anything added to
        # this harness, and it caught this one immediately: `enter_house`
        # raises the room's note, which lands at age zero mid-slide and
        # reads to the sprite rule as a panel hanging off the window. It
        # failed four *battle* screens two hundred lines later.
        _toasts = list(g.toasts.items)
        # **Every plan, not one room** (0af, s58). Rooms are rolled now -
        # five shapes across a range of sizes - so drawing the one door
        # this island happens to have first would leave four of the five
        # untested. A door is picked per shape out of the whole coordinate
        # space, which the builder accepts whether or not the island has a
        # house there: `interiors.build` only ever reads the seed and the
        # pair of numbers.
        from pixel_rpg import interiors as _interiors
        _wanted = {}
        for _gx in range(0, 220, 3):
            for _gy in range(0, 220, 7):
                _plan = _interiors.plan_for(g.overworld().seed, (_gx, _gy))
                _key = _plan["shape"]
                if _plan["stacked"]:
                    _key = "stacked"
                _wanted.setdefault(_key, (_gx, _gy))
        for _name, _spot in sorted(_wanted.items()):
            g.enter_house(_spot)
            # **A fresh copy, not the captured list.** Assigning `_toasts`
            # itself makes the queue *be* that list, so the next
            # `enter_house` appends its room note straight into the thing
            # holding the restore point - six rooms later five stale
            # notices rode into the battle screens and failed them at
            # 1256px, which is s56's trap word for word, one loop up.
            g.toasts.items = list(_toasts)
            total += len(check("house interior (%s)" % _name, size,
                               g.draw_overworld, world=True))
            g.leave_house()
            g.toasts.items = list(_toasts)
            g.world, (g.player.x, g.player.y) = _island, _at

    g.meta.achievements = ["first_blood", "curious"]
    total += len(check("achievements", size,
                       lambda: menus.draw_achievements(g)))

    # The lobby in each of its three states.
    g.coop = None
    g.ui.lobby_index = 1
    total += len(check("lobby", size, lambda: menus.draw_lobby(g)))
    g.code_buffer = "82Q244"
    total += len(check("join code", size, lambda: menus.draw_join_code(g)))

    from pixel_rpg import coop as coop_mod
    hosting = coop_mod.Session.host("Host", port=0, seed=1,
                                    reach_out=False)
    hosting.remotes[2] = coop_mod.Remote(2, "Friend")
    g.coop = hosting
    g.ui.lobby_mode = "host"
    total += len(check("lobby hosting", size, lambda: menus.draw_lobby(g)))
    hosting.close()
    g.coop = None
    g.ui.lobby_mode = ""

    # **Wave mode** (0ag, s59): the picker, the intermission at its
    # shallowest and its deepest, and the score. The tenth wave is drawn
    # because it is the only one that adds two more lines to the reward
    # list - the panel has to fit its worst case, not its first.
    from pixel_rpg import gamemodes
    g.ui.mode_index = 1
    total += len(check("gamemode picker", size,
                       lambda: menus.draw_mode_select(g)))
    _waves, g.waves = g.waves, gamemodes.WaveRun("nightmare", 2.4)
    _mode, g.mode = g.mode, "waves"
    total += len(check("wave intermission", size,
                       lambda: menus.draw_waves(g)))
    for _ in range(9):
        g.waves.clear_wave(kills=4)
    total += len(check("wave intermission (rest wave)", size,
                       lambda: menus.draw_waves(g)))
    # **The host waiting for the room to finish choosing** (s84). It is
    # a different label on the same button and it is the longest one,
    # which is the only reason a layout checker cares.
    _waiting, g.waves_waiting_for = (g.waves_waiting_for,
                                     lambda: ["Somebody", "Another One"])
    total += len(check("wave intermission (waiting)", size,
                       lambda: menus.draw_waves(g)))
    g.waves_waiting_for = _waiting
    # And with a hand full of boons, which is the line under it.
    # **Not `_spoils`** - that name is already the helper that draws the
    # offer screen, and shadowing it inside this function made every
    # size crash before a single check ran.
    from pixel_rpg import spoils as spoils_mod
    for key in ("sharp", "steady", "rally", "bulwark"):
        spoils_mod.give(g.player, key, g.waves.cleared)
    total += len(check("wave intermission (carrying)", size,
                       lambda: menus.draw_waves(g)))
    g.player.buffs = []
    g.waves.end()
    total += len(check("wave over", size, lambda: menus.draw_wave_over(g)))
    g.waves, g.mode = _waves, _mode

    g.ui.dev_index = 3
    total += len(check("debug menu", size,
                       lambda: devmenu.draw_devmenu(g)))

    # Never checked until 2026-08-24 because `gamepad` was missing from
    # the instrumentation list above - the exact silent pass section 6
    # warns about. It draws a fixed-width box.
    total += len(check("controller search", size,
                       lambda: gamepad.draw_searching(g)))

    g.player.full_restore()
    pack = [make_enemy(12, "forest", g.settings.mods) for _ in range(5)]
    for foe in pack:
        foe.hp = foe.max_hp() * 50
    g.player.hp = g.player.max_hp() * 50
    g.start_battle(pack)
    for _ in range(900):
        g.battle.update(1 / 60)
        if g.battle.phase == "menu":
            break
    g.battle.target = 2
    total += len(check("battle group of five", size,
                       lambda: battlescreen.draw_battle(g)))

    # **The action-command window** (item 0ai, s67). It draws in the phase
    # the menu is *gone* for, so nothing above would ever have put it on
    # screen - s7's rule that a screen missing from this list is a silent
    # pass. Both states: waiting for the press, and after a good one.
    # **Something crossing the arena** (item 0al, s69). Shots draw in the
    # acting phase like the command window, so nothing above would ever
    # have put one on screen - s7's silent pass again.
    g.shots.clear()
    # **Placed against this window, not a 1280 one.** Hard coordinates
    # put all three off the side of a 390px phone, which the sprite rule
    # duly reported as 0% visible - the harness was wrong, not the layer.
    _w, _h = size
    for _i, _look in enumerate(("arrow", "bolt", "thrown")):
        g.shots.add((_w * 0.25, _h * 0.45 + _i * 14),
                    (_w * 0.72, _h * 0.40 + _i * 14),
                    (250, 158, 52), look=_look)
    for _one in g.shots.items:
        _one.age = _one.life * 0.5
    total += len(check("battle projectiles", size,
                       lambda: battlescreen.draw_battle(g)))
    g.shots.clear()

    _phase, g.battle.phase = g.battle.phase, "acting"
    g.battle.command = {"kind": "attack", "best": g.battle.elapsed + 0.15,
                        "opens": g.battle.elapsed - 0.15,
                        "closes": g.battle.elapsed + 0.30,
                        "taken": False, "quality": 0.0}
    total += len(check("battle action command", size,
                       lambda: battlescreen.draw_battle(g)))
    g.battle.command = dict(g.battle.command, kind="block", taken=True,
                            quality=0.9)
    total += len(check("battle action command taken", size,
                       lambda: battlescreen.draw_battle(g)))
    g.battle.command = None
    g.battle.phase = _phase
    g.battle = None

    # **The free-roam screen** (items 0ak and 0ay, §76). A drawing module
    # missing from the list above is a silent pass - §7 has warned about
    # that since `devmenu` did it and again when `gamepad` did - so this
    # is here in the same commit as the screen.
    #
    # Two states worth checking: an empty arena, and a crowded one with
    # a skill held and a swing live, which is every element it can draw
    # at once.
    from pixel_rpg import freeroam as _fr
    _mode, _zombies, _roam = g.mode, g.zombies, g.roam
    g.mode = "zombies"
    g.zombies = gamemodes.NightRun()
    g.roam = _fr.FreeRoam(g.player, [])
    # **`world=True`, for §36's reason exactly.** A crowd of monsters
    # standing on each other is what this mode *is* - they converge on
    # the player and push apart, and two overlapping by half is a fight
    # rather than a layout fault. The text rules still apply, which is
    # what caught the skill chips running off a phone.
    total += len(check("free roam empty", size,
                       lambda: freeroamscreen.draw_free_roam(g), world=True))
    g.zombies.wave, g.zombies.kills = 6, 21
    for _ in range(8):
        g.roam.spawn(make_enemy(6, "meadow", g.settings.mods))
    for _ in range(90):
        g.roam.update(1 / 60)
    if g.player.skills():
        g.roam.charge(0, g.player.skills()[0])
    g.roam.swing(0)
    total += len(check("free roam crowd", size,
                       lambda: freeroamscreen.draw_free_roam(g), world=True))
    g.roam, g.zombies, g.mode = _roam, _zombies, _mode

    # **The hoard** (item 0bv, §86): the same arena with money on the
    # floor, a camp ring and a different right-hand plate. Its worst case
    # is arms full and the clock nearly out, which is when every number
    # on it is at its longest.
    _hoard = getattr(g, "hoard", None)
    g.mode = "hoard"
    g.hoard = gamemodes.HoardRun()
    g.roam = _fr.FreeRoam(g.player, [], hoard=True)
    g.roam.scatter(24)
    for _ in range(4):
        g.roam.spawn(make_enemy(6, "meadow", g.settings.mods))
    g.roam.heroes[0].carrying = 18
    g.hoard.left, g.hoard.banked = 9.0, 137
    g.roam.banked[0] = 137
    total += len(check("hoard arena", size,
                       lambda: freeroamscreen.draw_free_roam(g), world=True))
    g.roam.heroes[0].carrying = 0
    g.hoard.left = 174.0
    total += len(check("hoard arena (arms empty)", size,
                       lambda: freeroamscreen.draw_free_roam(g), world=True))
    # **The give-up question, over the arena** (§90). Esc used to end the
    # run on the spot; it asks now, and the question is drawn over the
    # thing it is asking about.
    g.hoard.banked = 137
    g.ask_leave_roam()
    total += len(check("hoard give up", size,
                       lambda: menus.draw_dialogue(g), world=True))
    g.dialogue = None
    # And what a guest looks at while the host gets it ready - a screen
    # that drew *nothing at all* until §86.
    _waves = g.waves
    g.waves, g.roam = None, None
    total += len(check("waiting room", size, lambda: menus.draw_waves(g)))
    g.waves, g.roam = _waves, _roam
    g.hoard, g.mode = _hoard, _mode

    # A full room against a full pack: four name tags, a party strip and
    # five monsters, which is the busiest this screen can ever get.
    from pixel_rpg.entities import Player as _Player
    party = [g.player]
    for name in ("Kessa", "Dorn", "Vell"):
        friend = _Player(name, "emberwright")
        for _ in range(12):
            friend.gain_xp(friend.xp_to_next())
        friend.hp = max(1, friend.max_hp() // 3)
        party.append(friend)
    pack = [make_enemy(12, "forest", g.settings.mods) for _ in range(5)]
    for foe in pack:
        foe.hp = foe.max_hp() * 50
    g.player.hp = g.player.max_hp() * 50
    g.start_battle(pack, party=party, viewer=0, sort_party=False)
    for _ in range(900):
        g.battle.update(1 / 60)
        if g.battle.phase == "menu":
            break
    g.battle.acting_player = 2
    total += len(check("battle four against five", size,
                       lambda: battlescreen.draw_battle(g)))

    # **Somebody else's turn** (s51, item 0h). A distinct state and a
    # distinct set of things to collide: the turn bar carries nine chips
    # and a name, and the menu is drawn greyed rather than not drawn.
    g.battle.acting_player = 2
    total += len(check("battle someone else's turn", size,
                       lambda: battlescreen.draw_battle(g)))
    # And the round the monsters open, which reverses the bar.
    g.battle.players_first = False
    g.battle.turn = "enemy"
    g.battle.acting = 1
    total += len(check("battle monsters first", size,
                       lambda: battlescreen.draw_battle(g)))
    g.battle = None

    # A phone: the world with a thumbstick and four buttons over it. The
    # controls sit on top of the HUD, so this is the one screen where a
    # collision would be with something the player still needs to read.
    g.go(G.OVERWORLD)
    g.touch.enabled = True
    g.touch.stick_finger = 1
    g.touch.layout(size)
    g.touch.stick_at = (g.touch.stick_home[0] + 60,
                        g.touch.stick_home[1] - 40)
    total += len(check("overworld with touch controls", size, g.draw,
                       world=True))
    g.touch.enabled = False
    g.touch.stick_finger = None
    return total


DESKTOP_SIZES = ((1280, 720), (1024, 768), (1600, 900))
# A phone, both ways up. **This is a gate now.** It began as a measurement
# because the answer was 1240 problems and a crash (HANDOFF s28); it reached
# zero in s29 and there is no reason to let it drift back. Run it with
# `--phone`; it fails the build like the desktop sizes do.
PHONE_SIZES = ((844, 390), (390, 844))


def run(sizes=DESKTOP_SIZES, tolerate=False):
    """Draw every screen at every size and report what does not fit."""
    total = 0
    for size in sizes:
        print("\n=== %dx%d ===" % size)
        try:
            g = populate(build_game(size))
            total += screen_suite(g, size)
        except Exception as exc:                      # noqa: BLE001
            # A screen that cannot be drawn at all is the loudest
            # possible result, and must not stop the other sizes being
            # measured.
            print("  CRASH: %s: %s" % (type(exc).__name__, exc))
            total += 1
        pygame.display.quit()
    print("\n" + "-" * 62)
    # Which font these numbers were measured with. Every check in here
    # is `font.size()` underneath, so a different font is a different
    # result - and a report that does not say which one is a report
    # somebody will disagree with for no discoverable reason.
    try:
        from pixel_rpg.assets import Assets
        print("measured with: %s" % Assets().font_name)
    except Exception:                                 # noqa: BLE001
        pass
    print("TOTAL layout problems: %d" % total)
    if total and tolerate:
        print("(tolerated: this size is not a gate)")
        return 0
    return 1 if total else 0


def selftest():
    """Prove the sprite rules can fail, because a rule that never has is
    a rule nobody has tested.

    This file spent a long time reporting a clean run on screens that had
    visible bugs in them - not because it was broken, but because it was
    not looking at sprites at all. The failure mode of a checker is
    silence, and section 6 of the handoff already records the same shape
    of mistake once: a module left out of the instrumentation list
    reported "0 texts, 0 panels", which reads exactly like a pass.

    So each rule gets a deliberately broken screen and has to catch it,
    and a deliberately fine one it has to stay quiet about.
    """
    size = (640, 480)
    screen = RecordingSurface(size)
    from pixel_rpg.assets import Assets
    assets = Assets(48)
    blob = pygame.Surface((64, 64))
    # Wide enough to bury a whole label rather than clip its first word.
    # The first version of this test used `blob` and covered 36% of the
    # text, which is a marginal case to hang a self-test on either way.
    slab = pygame.Surface((260, 64))
    failures = []

    def expect(label, marker, draw_fn, wanted=True):
        start()
        try:
            draw_fn()
        finally:
            stop()
        problems = analyse(label, size)
        hit = any(line.startswith(marker) for line in problems)
        ok = hit == wanted
        print("  %s %-34s %s"
              % ("ok  " if ok else "FAIL", label,
                 "caught" if hit else "nothing"))
        if not ok:
            failures.append(label)
            for line in problems[:3]:
                print("        %s" % line)

    def stacked():
        screen.blit(blob, (100, 100))
        screen.blit(blob, (104, 104))

    def apart():
        screen.blit(blob, (100, 100))
        screen.blit(blob, (400, 300))

    def off_window():
        screen.blit(blob, (620, 200))

    def inside():
        screen.blit(blob, (200, 200))

    def over_text():
        ui.draw_text(screen, assets, "buried under a sprite", (120, 200))
        screen.blit(slab, (110, 190))

    def under_text():
        screen.blit(blob, (120, 190))
        ui.draw_text(screen, assets, "drawn on top, so fine", (300, 400))

    # **The highlight rule gets the same treatment** (s53). It is the
    # newest rule here and the one with the most call sites depending on
    # it, so it has to be able to fail on demand like the rest.
    def label_out_of_its_box():
        ui.highlight(screen, pygame.Rect(80, 80, 200, 16))
        ui.draw_text(screen, assets, "hanging out", (84, 88), "small")

    def label_inside_its_box():
        ui.highlight(screen, pygame.Rect(80, 200, 200, 30))
        ui.row_text(screen, assets, "sitting neatly",
                    pygame.Rect(80, 200, 200, 30), "small")

    print("\n=== self-test: can these rules fail? ===")
    expect("two sprites stacked", "SPRITES ON", stacked)
    expect("two sprites apart", "SPRITES ON", apart, wanted=False)
    expect("a sprite off the window", "SPRITE OFF", off_window)
    expect("a sprite on the window", "SPRITE OFF", inside, wanted=False)
    expect("a sprite over earlier text", "COVERED", over_text)
    expect("text over an earlier sprite", "COVERED", under_text,
           wanted=False)
    expect("a label out of its highlight", "HALF-LIT", label_out_of_its_box)
    expect("a label inside its highlight", "HALF-LIT",
           label_inside_its_box, wanted=False)

    print("\n" + "-" * 62)
    if failures:
        print("SELF-TEST FAILED: %s" % ", ".join(failures))
        return 1
    print("the rules catch what they are for, and only that.")
    return 0


if __name__ == "__main__":
    __import__("pixel_rpg.assets", fromlist=["a"]).init_pygame()
    if "--selftest" in sys.argv:
        code = selftest()
    else:
        phone = "--phone" in sys.argv
        code = run(PHONE_SIZES if phone else DESKTOP_SIZES)
    pygame.quit()
    sys.exit(code)
