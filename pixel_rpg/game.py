"""The Game object: window, main loop, shared state and the overworld."""
from __future__ import annotations

import math
import sys
import time

import pygame

from . import classes as classes_mod
from .appearance import Appearance
from . import meta as meta_mod
from . import menus, modifiers as modifiers_mod
from . import relics as relics_mod, security as sec, story
from .assets import Assets, init_pygame
from .audio import Audio
from .battle import Battle, MENU as BATTLE_MENU, SUBMENU as BATTLE_SUBMENU
from . import anim
from .battlescreen import (BATTLE_STACK_WIDTH, combatant_anchor,
                           draw_battle, handle_battle_event, shaped)
from . import freeroam, freeroamscreen
from . import config as config_mod
from .config import Settings, load_environment
from .entities import (BOSS_BY_KEY, BOSS_DEFS, MAX_LEVEL, Boss, Enemy, Player,
                       make_boss_encounter, make_encounter, make_enemy,
                       make_field_boss)
from . import gamepad as gamepad_mod
from . import touch as touch_mod
from . import devmenu as devmenu_mod
from . import gamemodes
from . import spoils as spoils_mod
from . import coop as coop_mod
from . import interiors
from . import trade as trade_mod
from . import scenes as scenes_mod
from .logbook import Logbook
from .items import (BASES_BY_NAME, CONSUMABLE_ORDER, CONSUMABLES, Item,
                    roll_item)
from .keybinds import FALLBACK_MOVEMENT, Keybinds
from .modifiers import RunModifiers
from .quests import ACHIEVEMENTS as _ACHIEVEMENTS, QuestLog


def quest_achievements():
    return _ACHIEVEMENTS
from .save import (SLOT_COUNT, GuestVault, SaveManager, build_payload,
                   deserialize_player, format_playtime, serialize_player)
from .ui import (ACCENT, BAD, DIM, GOOD, INK, Fade, FloatingTextLayer,
                ShotLayer,
                 ParticleSystem, ToastQueue, Tween, bar, draw_text,
                 ellipsize, hp_color, key_hint, panel, status_chips,
                 tint_screen)
from .util import clamp, commafy, pulse, rng, stable_seed
from .world import (BIOMES, TILES, Camera, World, WORLD_H, WORLD_W,
                    enemy_kin, siblings_of)

# Scene identifiers
SPLASH = "splash"
TITLE = "title"
SLOTS = "slots"
NAME_ENTRY = "name"
CLASS_SELECT = "classes"
MODIFIER_SELECT = "modifiers"
# Wave mode (0ag, s59). `MODE_SELECT` is the picker, `WAVES` is the
# intermission between fights, and `WAVE_OVER` is the score.
MODE_SELECT = "modeselect"
WAVES = "waves"
WAVE_OVER = "waveover"
#: The Long Night (items 0ak and 0ay). One state, because a free-roam
#: fight has no intermission to be in - the next night opens where the
#: last one ended.
ROAM = "roam"
#: The arena picker (item 0br): a screen of places rather than a row
#: with a name on it.
ARENA_SELECT = "arenaselect"
#: What a cleared wave offers (items 0bs/0bt). One screen per offer -
#: a skill, an item, a boon - and in a night, one offer of four mixed
#: cards, because a night is the mode about not stopping.
SPOILS = "spoils"
CODEX = "codex"
GUIDE = "guide"
KEYBINDS = "keybinds"
RECORDS = "records"
ACHIEVEMENTS_SCREEN = "achievements"
ENDING_CHOICE = "endingchoice"
ENDING_TEXT = "endingtext"
OVERWORLD = "overworld"
BATTLE = "battle"
INVENTORY = "inventory"
SHOP = "shop"
SMITH = "smith"
CHARACTER = "character"
QUESTS = "quests"
PAUSE = "pause"
SETTINGS_SCREEN = "settings"
DIALOGUE = "dialogue"
GAMEOVER = "gameover"
DEVMENU = "devmenu"
FULLBAG = "fullbag"
TRADE = "trade"
NEWSKILL = "newskill"
SCENE = "scene"
LOBBY = "lobby"
JOIN_CODE = "joincode"
STORY = "story"
CHAPTER_CARD = "chaptercard"
WORLDMAP = "worldmap"

#: The keys `repeat_held_arrows` repeats while held on a menu.
HELD_ARROWS = (pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT)

# **The smallest the desktop window may get.** Asked for by Savvydev on
# 2026-08-29 - *"why dont you limit how small the window can be?"* - and
# the honest answer was that it was already half-done: the resize handler
# clamped the surface and nothing told the window itself, so dragging
# smaller snapped back with a jolt instead of simply refusing.
#
# **The browser is deliberately exempt.** A page does not own its window,
# so there is nothing there to refuse - clamping the canvas would crop the
# game rather than shrink it, and content the player cannot reach is worse
# than a narrow layout. That is why `layout.py --phone` stays a gate (s37):
# it is checking sizes only the browser can produce.
MIN_WINDOW_W = 800
MIN_WINDOW_H = 600

PLAYER_BOX = 0.62          # hitbox as a fraction of a tile
DAY_LENGTH = 240.0         # seconds for a full day/night cycle


class Game:
    def __init__(self):
        load_environment()
        self.settings = Settings.from_env()
        if self.settings.seed is not None:
            rng.reseed(self.settings.seed)

        # Cached: `tick` asks every frame and `is_browser` is a function
        # call that can never change its answer mid-run.
        self.in_browser = config_mod.is_browser()
        self.frame_count = 0
        _trace("pygame init")
        init_pygame()
        _trace("display mode next")
        # Open at the size of the browser window rather than the built
        # framebuffer. See `follow_browser_window`.
        opening = config_mod.browser_viewport()
        if opening:
            self.settings.width, self.settings.height = opening
        flags = pygame.FULLSCREEN if self.settings.fullscreen else pygame.RESIZABLE
        self.screen = pygame.display.set_mode(
            (self.settings.width, self.settings.height), flags)
        pygame.display.set_caption("Pixel RPG")
        self._set_window_icon()
        self.clock = pygame.time.Clock()

        self.assets = Assets(self.settings.tile)
        _trace("display up; synthesising audio")
        self.audio = Audio(self.settings.sfx_enabled, self.settings.master_volume)
        self.audio.prewarm()
        _trace("audio done; opening the save store")
        store = config_mod.open_save_store(self.settings.save_dir)
        self.saves = SaveManager(store)
        # Heroes that belong to somebody else's world (s19 item 5c). Same
        # store, different shelf - a guest hero is not one of the save slots.
        self.guests = GuestVault(store)
        self.meta = meta_mod.load_profile(store)
        self.binds = Keybinds(self.meta.keybinds)
        # Inert until enabled. If it was left on, enabling waits until the
        # first frame is up, so enumeration never delays the window.
        self.pad = gamepad_mod.Gamepad()
        self.pad.pending = bool(self.meta.gamepad)
        # Costs nothing to build and nothing to leave off, so unlike the pad
        # it can simply decide for itself: a browser is a phone, and a real
        # touch device is a real touch device.
        self.touch = touch_mod.Touch()
        self.touch.enabled = bool(self.meta.touch
                                  or touch_mod.wants_touch(self))
        # Free to build; the file appears on the first line written.
        self.log = Logbook()
        self.log.info("session started")
        self.konami = devmenu_mod.SequenceWatcher()
        #: Arrow keys held on a menu, and how long until each repeats -
        #: see `repeat_held_arrows`. Only ever the state they were
        #: pressed in; a change of screen empties it.
        self.key_repeat = {}
        self.key_repeat_state = None

        # The opening card. Skippable with any key or click - an opening
        # you cannot get past is one people learn to resent by the third
        # launch, and this one runs every single time the game starts.
        self.state = SPLASH
        self.splash_time = 0.0
        self.previous_state = TITLE
        self.running = True
        self.playtime = 0.0
        self.slot = 1
        self.world_time = 60.0

        self.player = None
        self.world = None
        self.quests = QuestLog()
        self.battle = None
        self.camera = Camera(self.settings.width, self.settings.height,
                             self.settings.tile)

        self.particles = ParticleSystem()
        self.floats = FloatingTextLayer()
        #: Things crossing the arena: arrows, bolts, thrown flasks (0al).
        self.shots = ShotLayer()
        self.toasts = ToastQueue()
        self.fade = Fade()
        self.hp_tween = Tween(1.0)
        self.mp_tween = Tween(1.0)
        self.xp_tween = Tween(0.0)
        self.enemy_hp_tween = Tween(1.0)

        # Per-screen cursor state, owned here so screens stay stateless.
        self.ui = menus.MenuState()
        self.dialogue = None
        self.scene = None
        self.coop = None            # a co-op Session, or None when solo
        # Set when Host a room sent the player through the save picker
        # first, and consumed the moment a run becomes live. A one-shot
        # intent rather than a mode, so abandoning the picker with Esc
        # cannot leave the game quietly wanting to host later.
        self.host_when_ready = False
        # **Which valley the hero on screen belongs to**, or None when it is
        # your own run. Set while a guest, and it is what decides where a
        # save goes - a guest hero must never be written into one of your
        # save slots (s19 item 5c).
        self.guest_world = None
        # One-shot, like `host_when_ready`: the seed we are currently making
        # a hero *for*, live only between "this valley is new to you" and
        # the moment character creation finishes.
        self.guest_pending = None
        # The slot that was loaded when you joined, so leaving a room puts
        # you back in your own run rather than on the title screen.
        self.pre_coop_slot = None
        # A window dragged below the minimum, waiting to be snapped back on
        # the next frame rather than mid-drag. See `window_resized`.
        self.pending_clamp = None
        # An invitation to somebody else's fight, on a client. See
        # `offered_a_fight`.
        self.fight_offer = None
        # The island and where you were standing on it, while you are
        # inside a house (s56). None whenever you are outdoors, which is
        # what everything else tests.
        self.house = None
        # The trade this player is in, as `trade.TradeSession`, or None.
        # It is a screen *and* a question - an invitation is drawn as a
        # dialogue rather than as the screen - so it lives here rather than
        # on `self.ui` with the other cursors.
        self.trade = None
        # **What a cleared wave is offering** (items 0bs/0bt, s83): a
        # list of `spoils.Offer`, walked one at a time. `spoils_then`
        # says what happens when the last one is answered, because the
        # two modes reach this screen from different places - a Last
        # Stand goes back to its intermission, a night opens.
        self.spoils = []
        self.spoil_at = 0
        self.spoils_then = ""
        #: The hoard run this game is in, or None (item 0bv, §86).
        self.hoard = None
        self._battle_shape = None   # last shared-battle frame actually sent
        self.code_buffer = ""
        self.autosave_cooldown = sec.Cooldown(45.0)
        self._field_cooldown = sec.Cooldown(self.FIELD_BOSS_COOLDOWN)
        # The lair whose occupant is currently being fought, so a win can
        # put that one - and only that one - to rest.
        self.pending_lair = None
        # The stepstone whose guardian is currently being fought.
        self.pending_gate = None
        self.walk_anim = 0.0
        #: How much of the walk is currently showing, 0 to 1 (item 0al(b),
        #: s71). `walk_anim` is the clock; this is the blend, and without
        #: it letting go of a key cut the bob to nothing mid-stride.
        self.walk_ease = 0.0
        # Fractions of a step not yet counted - see `move_player`.
        self.step_carry = 0.0
        #: How many fights have started this session. Only ever read
        #: modulo the variant count (item 0bd); nothing depends on the
        #: absolute number, and it is deliberately not saved.
        self.fights_seen = 0
        #: Which place a `random` Last Stand rolled for itself (0au).
        self.wave_arena_roll = 0
        #: The free-roam fight in progress, if any (item 0ak).
        self.roam = None
        #: The Long Night run this fight belongs to (item 0ay).
        self.zombies = None
        #: How far the last swing reached, so the arc drawn is the arc
        #: the simulation used.
        self.roam_reach = 0.0
        #: And how wide it swept (item 0by). The telegraph is aiming
        #: feedback, so it has to be drawn at the size the simulation
        #: used - both halves of it, not just the length.
        self.roam_arc = 0.0
        #: A guest's hero order, shared with the host so a frame's
        #: indices mean the same person on both machines (item 0bg).
        self.roam_order = []
        #: A skill a guest pressed, waiting for the next input message.
        self.roam_pending_skill = ""
        self.last_tile = None
        self.last_score = 0
        self.toast_anchor = 210
        self.hover = (None, None)     # last hotspot the pointer was over
        self.title_index = 0
        self.name_buffer = "Wanderer"
        self.name_touched = False
        self.message = ""
        # Run setup carried between the class, modifier and name screens.
        self.pending_class = classes_mod.DEFAULT_CLASS
        # Which mode the run being set up will be, and which mode the run
        # actually in progress is (0ag, s59).
        self.pending_mode = gamemodes.DEFAULT_MODE
        self.mode = gamemodes.DEFAULT_MODE
        self.waves = None
        #: Visible monsters walking the overworld (item 0aj, s68). On the
        #: `Game` rather than the `World` on purpose: they are spawned
        #: around the player as they walk rather than generated, so they
        #: cannot move a chest (s63) and cost the save format nothing.
        self.wanderers = []
        #: Set while a guest is making a hero for a wave room.
        self.wave_guest = False
        self.pending_modifiers = RunModifiers()
        self.pending_ngplus = 0
        self.pending_appearance = Appearance()
        self.ending = None
        self.reading = None          # codex entry being read full-screen
        self.pending_epitaph = None  # warden last words, shown after victory
        self.pending_ending = False
        self.ending_options = []
        # The chapter card: what it says, and how long it has been up.
        self.chapter_card = None
        self.chapter_time = 0.0
        #: What runs when the chapter card closes - the warden's last words,
        #: queued behind the card that announces their death (s96).
        self.after_card = None

    def _set_window_icon(self):
        """Draw the taskbar icon from the same sprite the .ico is built from.

        Cosmetic only: if anything about it fails the game still runs, it just
        wears the default pygame icon.
        """
        try:
            from .assets import build_surface
            from . import spritedata as sd
            hero = build_surface(sd.HERO["front"], scale=1, outline=True)
            icon = pygame.Surface((32, 32), pygame.SRCALPHA)
            pygame.draw.rect(icon, (34, 28, 56, 255), icon.get_rect(),
                             border_radius=6)
            pygame.draw.rect(icon, (250, 200, 96, 235), icon.get_rect(), 1,
                             border_radius=6)
            hero = pygame.transform.scale(hero, (28, 28))
            icon.blit(hero, (2, 2))
            pygame.display.set_icon(icon)
        except pygame.error:
            pass

    # ------------------------------------------------------------ helpers ---

    @property
    def tile(self):
        return self.settings.tile

    def toast(self, text, color=INK, life=3.0):
        self.toasts.push(text, color, life)

    def sfx(self, name, **kw):
        self.audio.play(name, **kw)

    def player_rect(self):
        size = int(self.tile * PLAYER_BOX)
        return pygame.Rect(int(self.player.x - size / 2),
                           int(self.player.y - size / 2), size, size)

    def player_tile(self):
        return int(self.player.x // self.tile), int(self.player.y // self.tile)

    def daylight(self):
        """0 = deep night, 1 = full noon.

        **Long Nightfall pins it at zero** (s54). Its `always_night` tag
        was declared in `modifiers.py` and read by nothing, so half of that
        modifier - the half it is named after - did nothing at all, and the
        sun came up as usual on a run that had paid for permanent night.
        Third dead tag found by the same test, after Brittle Steel and
        Blind Road before it.

        One place, because everything else derives from this: the tint,
        the clock in the corner and whatever else asks the time all follow
        without knowing the modifier exists.
        """
        if self.player is not None                 and self.player.modifiers.has("always_night"):
            return 0.0
        phase = (self.world_time % DAY_LENGTH) / DAY_LENGTH
        return clamp(0.5 - 0.5 * math.cos(phase * math.tau), 0.0, 1.0)

    def night_tint(self):
        light = self.daylight()
        if light > 0.72:
            return None
        strength = int((0.72 - light) / 0.72 * 118)
        hue = (18, 22, 62) if light < 0.4 else (40, 34, 62)
        return (*hue, strength)

    # ------------------------------------------------------- run lifecycle ---

    def build_hero(self, name, cls):
        """The hero every mode starts with (0ag, s59).

        Lifted out of `new_run` when wave mode needed the same hero
        without the same world. **The order of the rolls is unchanged**,
        which is the only thing about this that could have gone wrong:
        s21 records a "pure refactor" that moved four classes in the
        balance table because one draw moved in the shared stream.
        """
        self.player = Player(sec.sanitize_name(name), cls.key)
        self.player.appearance = self.pending_appearance
        self.player.modifiers = self.pending_modifiers
        self.player.ngplus = self.pending_ngplus

        for base_name in (cls.start_weapon, cls.start_armor):
            base = BASES_BY_NAME.get(base_name)
            item = roll_item(1, rarity="common", base=base)
            item.name = base_name
            self.player.add_item(item)
            self.player.equip(item)
        self.player.consumables = {}
        for key, count in cls.start_consumables:
            self.player.add_consumable(key, count)

        # Fortune's Road hands out an opening relic. **Brittle Steel used
        # to hand out two** and does not any more (s45): a modifier is a
        # cost now, and `extra_relics` is not a tag anybody carries.
        bonus = 1 if cls.signature == "fortune" else 0
        pool = [r.key for r in relics_mod.COMMON_RELICS]
        for key in rng.sample(pool, min(bonus, len(pool))):
            self.player.add_relic(key)

    # ---------------------------------------------------------- wave mode ---

    def new_wave_run(self, name="Wanderer"):
        """Start a Last Stand run (0ag, s59).

        > "that gamemode wouldnt have save slots or anything, if u die you
        > get a score and thats it"

        So: no slot, no `save_run`, no prologue, no chapter. The hero, the
        difficulty and the modifiers are the ordinary ones - his *"u can
        add modifiers and set the difficulty for that one too"* - and the
        battle engine is the ordinary one too.

        **It still builds a world, and never shows it.** A world is one
        integer (hard rule 4) and half the game asks for one: `save_run`,
        `overworld()`, and above all `coop._island`, which reads a seed out
        of it to hand a joiner. Making the world optional would put a
        `None` check into every one of those; building one nobody walks in
        costs a generation nobody notices and keeps every existing
        invariant true.
        """
        cls = classes_mod.get(self.pending_class)
        self.house = None
        self.mode = "waves"
        self.roll_arena()
        # **Not `story`, whatever the last run was set to** (item 0bb).
        # The difficulty is one setting shared with the story mode, so a
        # player who finished an easy run and then opened a Last Stand
        # would carry it in without ever seeing the picker refuse it.
        if self.settings.difficulty not in config_mod.difficulties_for(
                "waves"):
            self.settings.cycle_difficulty(
                0, config_mod.difficulties_for("waves"))
        self.meta.tainted = False
        self.log.event("run_start", slot=0, cls=cls.key, mode="waves",
                       difficulty=self.settings.difficulty,
                       modifiers=",".join(
                           sorted(self.pending_modifiers.keys)) or "none")
        self.build_hero(name, cls)
        seed = self.settings.seed
        if seed is None:
            seed = rng.rint(1, 2 ** 31 - 2)
        self.world = World(self.tile, seed)
        self.player.x = self.world.spawn[0] * self.tile + self.tile / 2
        self.player.y = self.world.spawn[1] * self.tile + self.tile / 2
        self.player.full_restore()
        self.quests = QuestLog()
        self.playtime = 0.0
        self.world_time = 60.0
        self.waves = gamemodes.WaveRun(
            difficulty=self.settings.difficulty,
            modifier_score=self.pending_modifiers.score)
        # **No slot, and it is set rather than left alone.** `self.slot`
        # carries over from the previous run, and a stale one here is a
        # save waiting to be overwritten - see the gate in `save_run`.
        self.slot = 0
        self.ui.reset()
        self.state = WAVES
        # A room made *for* waves opens as soon as the run exists, exactly
        # as a story run's does.
        self._host_if_asked()

    def in_waves(self):
        """Whether a wave run is in progress. Cheap enough to ask often."""
        return self.mode == "waves" and self.waves is not None

    def wave_party_size(self):
        return self.coop.player_count if self.coop is not None else 1

    def roll_arena(self):
        """Take the "random each run" roll, once, at the start of a run.

        **Rolled here rather than per wave** (items 0au and 0aq): a game
        that changes its scenery behind the player is the thing he asked
        us not to do, and a roll at the start is the player asking for a
        surprise and getting one.

        **Split out because two modes forgot it.** It lived inline in
        `new_wave_run`, and The Long Night - written by copying most of
        that function - never took one. So with *Random each run* set, a
        night was fought in whatever the last Last Stand had rolled, or
        in arena zero on a fresh launch. The Hoard would have inherited
        the same hole.
        """
        self.wave_arena_roll = rng.rint(0, len(gamemodes.ARENAS) - 1)
        return self.wave_arena_roll

    def new_night_run(self, name="Wanderer"):
        """Start a Long Night (items 0ay and 0ak).

        **Everything but the fighting is Last Stand's** - no slot, no
        prologue, no chapter, a world it never walks, a score at the end.
        The mode picker, the modifiers and the difficulty are the
        ordinary ones. Only `begin_night` differs, and what it opens is a
        `FreeRoam` rather than a `Battle`.
        """
        cls = classes_mod.get(self.pending_class)
        self.house = None
        self.mode = "zombies"
        # **Not `story`** (item 0bb): his rule covers this mode by name.
        if self.settings.difficulty not in config_mod.difficulties_for(
                "zombies"):
            self.settings.cycle_difficulty(
                0, config_mod.difficulties_for("zombies"))
        self.meta.tainted = False
        # **Through the same door a wave run uses.** `build_hero` is
        # where a class's opening relic, its appearance and its modifiers
        # are handed out (§52's Wayfarer lesson, in the probe's costume),
        # and a mode that built its own `Player` would quietly be playing
        # a different game.
        self.build_hero(name, cls)
        seed = self.settings.seed
        if seed is None:
            seed = rng.rint(1, 2 ** 31 - 2)
        # A world it never walks, for the reason `new_wave_run` gives at
        # length: half the game asks for one, and making it optional
        # would put a `None` check into every one of those.
        self.world = World(self.tile, seed)
        self.player.x = self.world.spawn[0] * self.tile + self.tile / 2
        self.player.y = self.world.spawn[1] * self.tile + self.tile / 2
        self.player.full_restore()
        self.quests = QuestLog()
        self.world_time = 60.0
        self.zombies = gamemodes.NightRun(
            difficulty=self.settings.difficulty,
            modifier_score=self.pending_modifiers.score)
        self.roll_arena()
        self.playtime = 0.0
        # **No slot, and it is set rather than left alone** - the same
        # guard `new_wave_run` carries and for the same reason: `slot`
        # survives the previous run, and a stale one here is a save
        # waiting to be overwritten by `save_run`'s gate. A test caught
        # this one leaving it at 1.
        self.slot = 0
        self.ui.reset()
        self.log.event("run_start", slot=0, cls=cls.key, mode="zombies",
                       difficulty=self.settings.difficulty)
        self.begin_night()
        # **A room made *for* this mode opens as soon as the run exists**
        # (item 0cb). Every other starter has done this since §46; the
        # two written later did not, so hosting a night or a hoard opened
        # no room at all and there was no code to read out. His: *"when
        # playing one of the new gamemodes, co-op specifically doesnt
        # present you with the room code option"*.
        self._host_if_asked()
        return True

    def new_hoard_run(self, name="Wanderer"):
        """Start a hoard (item 0bv, §86).

        **The same opening as a night**, which is the point of §59's
        seam: no slot, no prologue, a world it never walks, a score at
        the end. What differs is `begin_hoard`, and what that opens is a
        `FreeRoam` with money on the floor.
        """
        cls = classes_mod.get(self.pending_class)
        self.house = None
        self.mode = "hoard"
        if self.settings.difficulty not in config_mod.difficulties_for(
                "hoard"):
            self.settings.cycle_difficulty(
                0, config_mod.difficulties_for("hoard"))
        self.meta.tainted = False
        self.build_hero(name, cls)
        seed = self.settings.seed
        if seed is None:
            seed = rng.rint(1, 2 ** 31 - 2)
        self.world = World(self.tile, seed)
        self.player.x = self.world.spawn[0] * self.tile + self.tile / 2
        self.player.y = self.world.spawn[1] * self.tile + self.tile / 2
        self.player.full_restore()
        self.quests = QuestLog()
        self.world_time = 60.0
        self.hoard = gamemodes.HoardRun(
            difficulty=self.settings.difficulty,
            modifier_score=self.pending_modifiers.score)
        self.roll_arena()
        self.zombies = None
        self.playtime = 0.0
        self.slot = 0
        self.ui.reset()
        self.log.event("run_start", slot=0, cls=cls.key, mode="hoard",
                       difficulty=self.settings.difficulty)
        self.begin_hoard()
        self._host_if_asked()          # item 0cb, see `new_night_run`
        return True

    def begin_hoard(self):
        """Open the arena, with money already on the floor.

        **Coins first, monsters later.** The first thing a hoard run
        should see is what it is here for; §68's *asked, not sprung*
        applies to the opening seconds of this as much as to a night.
        """
        run = self.hoard
        if run is None or self.player is None:
            return False
        party, order, viewer = self.roam_party()
        self.roam = freeroam.FreeRoam(
            self.player, [], on_event=self.on_roam_event,
            mods=self.settings.mods, party=party, viewer=viewer, hoard=True)
        self.roam_order = order
        self.dress_roam_arena()
        # **After the arena is dressed**, because that is what puts the
        # cover down - and the camp has to be a place you can run into.
        self.roam.clear_camp()
        self.roam.scatter(gamemodes.HOARD_START)
        if self.coop is not None and self.coop.role == coop_mod.HOST:
            self.coop.open_fight({"mode": "hoard", "night": 1,
                                  "party": order, "seed": 0, "level": 1,
                                  "biome": "meadow", "boss": ""})
        self.state = ROAM
        self.toast("Fill your arms and get back to the fire.", ACCENT, 4.0)
        self.sfx("select")
        return True

    def update_hoard(self, dt):
        """The clock, the coins and the crowd. The fight itself is
        `update_roam`'s, unchanged."""
        run, fight = self.hoard, self.roam
        if run is None or fight is None:
            return
        if fight.mirror:
            # **The clock is the one thing a mirror does run**, because a
            # HUD counting down is the mode's whole pressure and one that
            # reads 3:00 all game is worse than one that drifts a second.
            #
            # **And it ends its own run.** The host stops pushing frames
            # when its clock runs out, so a guest that waited to be told
            # would stand in a still arena for ever. What it banked
            # arrived in the frames, already valued by the host, so both
            # machines score the same trips.
            over = run.tick(dt)
            run.banked = int(fight.banked.get(fight.viewer, 0))
            if over:
                self.finish_hoard()
            return
        if run.tick(dt):
            self.finish_hoard()
            return
        # **One number, read everywhere.** The HUD, the score and the
        # record all ask `run.banked`; letting it lag behind the arena
        # until the whistle would mean a player watching a counter that
        # is not the one they are being scored on.
        count = run.due_coins(len(fight.loot))
        if count:
            fight.scatter(count)
        if run.due_monster(len(fight.living_foes)):
            # A hoard keeps the island's bestiary: it is a robbery, not a
            # haunting, and the things in the way are ordinary.
            fight.spawn(make_enemy(gamemodes.hoard_level(run.elapsed),
                                   "meadow", self.settings.mods))

    def finish_hoard(self):
        """The clock ran out. **Nobody died their way out of this one.**"""
        run = self.hoard
        if run is None or run.over and self.state == WAVE_OVER:
            return
        fight = self.roam
        if fight is not None:
            # What is still in your arms when the whistle goes is not
            # yours - the trip has to finish.
            for body in fight.heroes:
                body.carrying = 0
            if fight.mirror:
                # A guest's total is whatever the host's frames said,
                # and its own trips were valued there.
                run.banked = int(fight.banked.get(fight.viewer, 0))
        score = run.end()
        self.last_score = score
        record = meta_mod.RunRecord(
            class_key=self.player.class_key,
            level=self.player.level, score=score,
            playtime=run.playtime,
            modifiers=tuple(sorted(self.player.modifiers.keys)),
            when=time.strftime("%Y-%m-%d %H:%M"),
            mode="hoard", party=max(1, len(fight.heroes) if fight else 1),
            waves=run.banked)
        self.meta.finish_run(record)
        self.meta.add_totals(self.player.stats_tracker)
        self.meta.save()
        self.state = WAVE_OVER
        self.sfx("levelup" if run.banked else "defeat")

    def begin_night(self):
        """Open a night: an empty arena that starts filling up.

        **Nothing is waiting when it opens.** A crowd already on top of
        you is not a fight you can read, and §68's *asked, not sprung*
        applies to the first second of a night as much as to a monster on
        the island.
        """
        run = self.zombies
        if run is None or self.player is None:
            return False
        party, order, viewer = self.roam_party()
        self.roam = freeroam.FreeRoam(
            self.player, [], on_event=self.on_roam_event,
            mods=self.settings.mods, party=party, viewer=viewer)
        self.roam_order = order
        self.dress_roam_arena()
        if self.coop is not None and self.coop.role == coop_mod.HOST:
            # **The room is told a night has opened**, through the same
            # `fight` announcement a turn-based one uses - so a guest's
            # `coop.fight` is set and its frames have somewhere to land.
            self.coop.open_fight({"mode": "zombies", "night": int(run.wave),
                                  "party": order, "seed": 0, "level": 1,
                                  "biome": "meadow", "boss": ""})
        self.state = ROAM
        self.toast("Night %d." % run.wave, ACCENT, 2.4)
        self.sfx("select")
        return True

    def roam_party(self):
        """Everybody in the arena, in one order both machines agree on.

        **Sorted by player id, not by who joined when** (item 0bg). A
        frame is a list and every index in it has to mean the same
        person on both sides; joining order is a thing only the host
        knows, and a room where the guest is index 1 on one machine and
        index 0 on the other is a room where you heal the wrong person.
        """
        # **`self.mode`, not `coop.mode`.** A room's mode is set from the
        # *welcome*, and a host never receives one - so `coop.mode` on the
        # machine running the room is permanently "story" by
        # construction. Asking it here made the host build a party of one
        # and announce an empty roster, which the guests then correctly
        # ignored as somebody else's fight.
        # **Every real-time mode, not one of them by name** (§87). This
        # read `!= "zombies"`, so a hoard in a room built a party of one
        # and announced a roster nobody was in - the exact symptom the
        # comment above describes, arriving a second time by a different
        # door.
        if self.coop is None or not gamemodes.is_roam(self.mode):
            return [self.player], [], 0
        bodies = self.coop.bodies()
        order = sorted(set(list(bodies) + [self.coop.player_id]))
        party = []
        for player_id in order:
            if player_id == self.coop.player_id:
                party.append(self.player)
                continue
            body = bodies.get(player_id)
            party.append(deserialize_player(body) if body
                         else Player("Wanderer", "warden"))
        return party, order, order.index(self.coop.player_id)

    def begin_guest_hoard(self, info):
        """Client side: the host opened a hoard (item 0bv, §86).

        **A mirror, exactly like a night's.** The coins, the crowd and
        the clock are all the host's; this side draws what arrives and
        drives one body. The only thing it keeps of its own is the run
        object, so the HUD has a clock to show between frames.
        """
        if self.player is None or self.coop is None:
            return False
        order = [pid for pid in (info.get("party") or [])]
        if self.coop.player_id not in order:
            order = sorted(set(order + [self.coop.player_id]))
        bodies = self.coop.bodies()
        party = []
        for player_id in order:
            if player_id == self.coop.player_id:
                party.append(self.player)
                continue
            body = bodies.get(player_id)
            party.append(deserialize_player(body) if body
                         else Player("Wanderer", "warden"))
        viewer = order.index(self.coop.player_id)
        self.hoard = self.hoard or gamemodes.HoardRun(
            difficulty=self.settings.difficulty)
        self.zombies = None
        self.roam = freeroam.FreeRoam(
            self.player, [], on_event=self.on_roam_event,
            mods=self.settings.mods, party=party, viewer=viewer,
            mirror=True, hoard=True)
        self.roam_order = order
        self.mode = "hoard"
        self.dress_roam_arena()
        self.roam.clear_camp()
        self.state = ROAM
        self.toast("Fill your arms and get back to the fire.", ACCENT, 3.4)
        return True

    def begin_guest_night(self, info):
        """Client side: the host has opened a night (item 0bg).

        A **mirror**: it draws what arrives and simulates nothing but its
        own hero's walk. §21's rule for a shared battle, unchanged by the
        fight not taking turns.
        """
        if self.player is None or self.coop is None:
            return False
        # **The host's order, not one worked out here** (item 0bg). A
        # frame is a list and index 1 has to be the same person on both
        # machines; the guest recomputing it from its own view of the
        # room gave a party of one against the host's two, because a
        # roster arrives when it arrives. The announcement is the
        # authority, exactly as it is for a shared battle (§21).
        order = [pid for pid in (info.get("party") or [])]
        if self.coop.player_id not in order:
            order = sorted(set(order + [self.coop.player_id]))
        bodies = self.coop.bodies()
        party = []
        for player_id in order:
            if player_id == self.coop.player_id:
                party.append(self.player)
                continue
            body = bodies.get(player_id)
            party.append(deserialize_player(body) if body
                         else Player("Wanderer", "warden"))
        viewer = order.index(self.coop.player_id)
        self.zombies = self.zombies or gamemodes.NightRun(
            difficulty=self.settings.difficulty)
        # **A guest is paid for the night it just survived** (s83). The
        # announcement is the only sign a guest gets that a night ended -
        # `finish_night` runs on the host alone - so before this, a
        # guest's hero came out of an hour of nights at level one.
        announced = max(1, int(info.get("night") or 1))
        owed = announced > self.zombies.wave and self.zombies.wave >= 1
        if owed:
            self.pay_for_night(self.zombies)
        self.zombies.wave = announced
        self.roam = freeroam.FreeRoam(
            self.player, [], on_event=self.on_roam_event,
            mods=self.settings.mods, party=party, viewer=viewer,
            mirror=True)
        self.roam_order = order
        self.mode = "zombies"
        self.dress_roam_arena()
        self.state = ROAM
        self.toast("Night %d." % self.zombies.wave, ACCENT, 2.4)
        if owed:
            # **The same card the host is taking** (item 0bs). A night
            # opens empty and nothing arrives for a few seconds (§68), so
            # this is read in the quiet the mode already leaves.
            self.offer_spoils(then="guest_night")
        return True

    def roam_remote_input(self, player_id, dx, dy, swing, dodge, skill):
        """Host side: one guest pushed a key (item 0bg).

        **Which hero it drives comes from who sent it**, never from the
        message - the one thing a peer cannot lie about, and the reason
        there is no index in the payload at all (§20).
        """
        fight = self.roam
        if fight is None or self.coop is None:
            return
        order = getattr(self, "roam_order", []) or []
        if player_id not in order:
            return
        index = order.index(player_id)
        chosen = None
        if skill:
            body = fight.hero(index)
            if body is not None:
                chosen = next((s for s in body.who.skills()
                               if s.key == skill), None)
        fight.input(index, dx=dx, dy=dy, swing=bool(swing),
                    dodge=bool(dodge), skill=chosen, dt=1 / 60.0)

    def roam_frame(self, frame):
        """Client side: where the host says everything is."""
        if self.roam is not None and self.roam.mirror:
            self.roam.apply_snapshot(frame)
            if self.roam.finished and self.zombies is not None \
                    and not self.zombies.over:
                self.finish_night_run()

    def on_roam_event(self, kind, data):
        """Presentation for a free-roam fight.

        The same shape `on_battle_event` has, and deliberately thinner:
        this fight has no log, no turn bar and no menu to grey out.
        """
        if kind == "hit":
            weight = min(1.0, float(data.get("share", 0.0)) * 2.6)
            self.sfx("crit" if data.get("crit") else "hit", throttle=0.04)
            self.camera.add_shake(int(3 + 10 * weight))
            spot = self.roam_anchor(data.get("who"), data.get("index"))
            if spot:
                self.floats.add(spot[0], spot[1], str(data.get("amount", 0)),
                                (252, 208, 96) if data.get("crit")
                                else (250, 240, 210),
                                pop=bool(data.get("crit")))
                from .items import ELEMENT_COLOR
                self.particles.burst(
                    spot[0], spot[1], int(8 + 18 * weight),
                    color=ELEMENT_COLOR.get(data.get("element"),
                                            (250, 200, 96)))
        elif kind == "felled":
            self.sfx("hit")
            if self.zombies is not None:
                self.zombies.kills += 1
            if self.hoard is not None:
                self.hoard.kills += 1
        elif kind == "took":
            # **The pickup has to be heard as well as seen** (§86). It is
            # the only feedback that says *that one is yours now*, and
            # the arms-full slowdown is felt long before it is read.
            self.sfx("coin", throttle=0.04, volume=0.55)
        elif kind == "banked":
            # **The trip is what is scored, not the coin** (§87), so the
            # run is told about the trip rather than reading a total off
            # the arena - a big one pays double and a total cannot know
            # how it got there.
            if self.hoard is not None:
                self.hoard.bank(int(data.get("worth") or 0),
                                int(data.get("count") or 0))
            self.sfx("levelup", volume=0.5)
            spot = self.roam_anchor("player", data.get("index"))
            if spot:
                self.floats.add(spot[0], spot[1],
                                "+%d" % int(data.get("count") or 0),
                                (252, 214, 110), pop=True)
        elif kind == "spill":
            self.sfx("error", volume=0.6)
            if self.hoard is not None:
                self.hoard.falls += 1
                self.hoard.lost += int(data.get("count") or 0)
        elif kind == "stood":
            self.sfx("heal", volume=0.6)
        elif kind == "swing":
            self.roam_reach = float(data.get("reach") or 0.0)
            self.roam_arc = float(data.get("arc") or 0.0)
            self.sfx("swing", throttle=0.05)
        elif kind == "dodge":
            self.sfx("step", volume=0.5)
        elif kind == "charged":
            self.sfx("select", volume=0.6)
        elif kind == "refused":
            self.sfx("error", throttle=0.3)
        elif kind == "fell":
            self.sfx("hurt")
        elif kind == "over":
            if self.mode == "hoard":
                # **A hoard cannot end this way** and `check_over` says
                # so; if one ever does, the clock is what answers.
                self.finish_hoard()
            elif self.zombies is not None:
                self.finish_night_run()
            else:
                # An island fight in real time (item 0bi): won or lost,
                # it goes back through the ordinary payout.
                self.finish_open_fight()

    def ask_leave_roam(self):
        """Esc in a real-time mode: **ask, do not end** (§90).

        It used to end the run on the spot - mid-fight, no question, with
        a score written to the records. Every other screen in the game
        treats Esc as *back*, and the two modes where it was instead
        *give up for ever* are the two you are most likely to press it in
        by reflex: there is no pause menu out here, so a player reaching
        for one loses the run.

        The same shape gold already has (rule 2.9): the irreversible half
        takes a second, deliberate press.
        """
        if self.roam is None or self.player is None:
            return False
        run = self.current_run()
        # What giving up actually costs, in the mode's own words - a
        # hoard keeps what is banked and a night keeps its depth.
        if self.mode == "hoard":
            kept = "You keep the %d you have banked." % int(
                getattr(run, "banked", 0))
            title = "Leave with what you have?"
        else:
            nights = int(getattr(run, "cleared", 0))
            kept = ("You stood %d night%s." % (nights,
                                               "" if nights == 1 else "s")
                    if nights else "You have not survived one yet.")
            title = "Give up the night?"
        self.dialogue = {
            "title": title,
            "body": [kept, "", "This ends the run and writes the score."],
            "confirm": "End the run",
            "on_confirm": (self.finish_hoard if self.mode == "hoard"
                           else self.finish_night_run),
            "on_cancel": self.back_to_roam,
        }
        self.go(DIALOGUE)
        self.sfx("back")
        return True

    def back_to_roam(self):
        """Cancelling the question above: back into the arena."""
        self.dialogue = None
        self.state = ROAM if self.roam is not None else TITLE

    def roam_anchor(self, who, index):
        """Where a body is, in window pixels, for a float or a burst."""
        fight = self.roam
        if fight is None:
            return None
        bodies = fight.heroes if who == "player" else fight.foes
        if not 0 <= int(index or 0) < len(bodies):
            return None
        body = bodies[int(index or 0)]
        return freeroamscreen.to_screen(self.screen, body.x, body.y)

    def update_roam(self, dt):
        """One frame of a Long Night: input, arrivals, the simulation."""
        fight, run = self.roam, self.zombies
        if fight is None or fight.finished:
            return
        if run is not None:
            run.playtime += dt
            run.since += dt
        self.playtime += dt
        keys = pygame.key.get_pressed()
        dx = (keys[pygame.K_d] or keys[pygame.K_RIGHT]) - \
             (keys[pygame.K_a] or keys[pygame.K_LEFT])
        dy = (keys[pygame.K_s] or keys[pygame.K_DOWN]) - \
             (keys[pygame.K_w] or keys[pygame.K_UP])
        fight.input(fight.viewer, dx=dx, dy=dy,
                    swing=bool(keys[pygame.K_SPACE]),
                    dodge=bool(keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]),
                    dt=dt)
        if self.mode == "hoard":
            # **Before the mirror's early return**, not after it. A
            # guest's clock is the mode's whole pressure, and the first
            # cut put this below the `return` - so a guest's HUD read
            # 3:00 for the entire run and the check that found it was
            # the one over a real socket (§87).
            self.update_hoard(dt)
        if fight.mirror:
            # **A guest simulates nothing but its own walk.** The keys
            # above already moved this machine's hero for the feel of it
            # (`apply_snapshot` corrects it when the two disagree); the
            # host is told what was pressed and everything else waits for
            # a frame.
            self.coop.send_roam_input(dx, dy,
                                      swing=bool(keys[pygame.K_SPACE]),
                                      dodge=bool(keys[pygame.K_LSHIFT]
                                                 or keys[pygame.K_RSHIFT]),
                                      skill=self.roam_pending_skill)
            self.roam_pending_skill = ""
            fight.update(dt)
            return
        if run is not None and run.ready(len(fight.living_foes)):
            # **The night's own bestiary** (item 0bx). It spawned
            # `"meadow"` - slimes and rats - in a mode he asked for as
            # *"a zombies gamemode"*.
            fight.spawn(make_enemy(gamemodes.night_level(run.wave),
                                   gamemodes.NIGHT_BIOME,
                                   self.settings.mods))
            run.arrived()
        fight.update(dt)
        if self.coop is not None and self.coop.role == coop_mod.HOST:
            self.coop.push_roam(fight.snapshot())
        if run is not None and not run.due and not fight.living_foes                 and not fight.finished:
            self.finish_night()

    def finish_night(self):
        """A night survived: pay for it and open the next one."""
        run = self.zombies
        if run is None:
            return
        self.pay_for_night(run)
        # **The night's one card** (items 0bs/0bu). A Last Stand stops
        # and hands you three; a night hands you one of four mixed and
        # opens. Taking it is what starts the next night, so the arena
        # is not standing empty while somebody reads.
        if not self.offer_spoils(then="night"):
            self.begin_night()

    def pay_for_night(self, run):
        """What surviving a night is worth. **Host and guest alike.**

        Split out because `finish_night` only ever runs on the machine
        that is simulating the fight - so before this existed, **a guest
        who survived a night was paid nothing at all**: no gold, no XP,
        no heal. It looked correct because the host was always the one
        being watched.
        """
        reward = run.clear_night()
        levels, _skills = self.player.gain_xp(int(reward.get("xp", 0)))
        self.player.gold += int(reward.get("gold", 0))
        healed = self.player.heal(int(self.player.max_hp() * 0.35))
        self.player.restore_mp(int(self.player.max_mp() * 0.5))
        spoils_mod.expire(self.player, run.cleared)
        self.toast("Night %d survived. +%d hp" % (run.cleared, healed),
                   GOOD, 3.5)
        if levels:
            self.toast("Level %d!" % self.player.level, GOOD, 4.0)
        self.sfx("levelup")
        return reward

    def finish_night_run(self):
        """The player fell. One score, and out - his rule for these modes."""
        run = self.zombies
        if run is None or run.over:
            return
        # **The run scores itself** (§89), like the other two modes -
        # this was a line of arithmetic here, and the difficulty
        # multiplier every other mode applies was simply missing from it.
        score = run.end()
        self.last_score = score
        record = meta_mod.RunRecord(
            class_key=self.player.class_key,
            level=self.player.level, score=score,
            playtime=run.playtime,
            modifiers=tuple(sorted(self.player.modifiers.keys)),
            when=time.strftime("%Y-%m-%d %H:%M"),
            mode="zombies", party=1, waves=run.cleared)
        self.meta.finish_run(record)
        self.meta.add_totals(self.player.stats_tracker)
        self.meta.save()
        self.state = WAVE_OVER
        self.sfx("defeat")

    def begin_wave(self):
        """Send in the next wave.

        Solo and hosting take different doors for the same reason chests
        do (s20): in a room the host is the authority on what the monsters
        are, and everybody has to be fighting the same ones.
        """
        if not self.in_waves() or self.battle is not None:
            return False
        if self.coop is not None and self.coop.role == coop_mod.CLIENT:
            # A guest never opens a wave. It waits for the host's `open`,
            # which arrives through the ordinary shared-battle path.
            self.toast("Waiting for the host.", DIM, 2.0)
            return False
        wave = self.waves.wave
        level = gamemodes.wave_level(wave)
        party = self.wave_party_size()
        count = gamemodes.wave_pack(wave, party)
        if self.coop is not None and self.coop.role == coop_mod.HOST:
            return self.host_open_wave(wave, level, count)
        # **Where this wave is fought** (item 0am) - and *only* where.
        # His correction, 2026-09-06 (item 0ao): *"the arenas are supposed
        # to be purely visual."* The first version let the arena pick the
        # biome and therefore the bestiary, which is a difficulty lever
        # wearing a scenery costume. The pack is rolled exactly as it was
        # before any of this existed.
        arena = self.arena_now(wave)
        enemies = self.wave_enemies(wave, level, party, count)
        self.announce_arena(wave, arena)
        self.start_battle(enemies)
        self.dress_arena(arena)
        return True

    def dress_roam_arena(self):
        """Put this arena's cover into the fight (item 0bp).

        **In `Game`, not in `freeroam`**, for the same reason the tint
        is: the simulation is told what is in its arena and never asks a
        gamemode table. It is a list of circles either way.
        """
        from . import freeroamscreen as frs
        fight = self.roam
        if fight is None:
            return
        arena = frs.arena_of(self)
        fight.blockers = freeroam.plan_blockers(
            getattr(arena, "plan", "open"),
            stable_seed(arena.key))

    def dress_arena(self, arena):
        """Hand the fight the colour of the place it is happening in.

        **After `start_battle`, not inside it.** Every other caller of
        that method is a fight that happens *somewhere on the island*,
        where the existing rule - the colour comes off what you are
        fighting - is the right one. Only the wave mode has a place
        without a map tile under it.
        """
        if self.battle is None or arena is None:
            return
        # **Its own colour, and there is nowhere else to get one.** An
        # arena named a biome once and fell back to that biome's ambient
        # wash; both halves of that are gone - the biome because an arena
        # is purely visual (0ao), and the wash because it sits near white
        # and flattened eighteen places into four (§72).
        self.battle.arena_tint = tuple(arena.tint or (200, 200, 210))
        self.battle.arena_scene = str(getattr(arena, "scene", "") or "hills")

    def arena_now(self, wave):
        """Where this wave is fought, **as the player asked for it**.

        Item 0au. The three shapes are his: *"they can pick an arena to
        use for the whole run or pick a random one, or it changes every
        10 or 5 or whatever waves. its up to them."* The setting lives in
        `Settings` because it is a preference; the roll for `random`
        lives on the run because it must not change halfway through one.
        """
        s = self.settings
        pick = int(s.arena_pick)
        if s.arena_mode == "random":
            pick = int(self.wave_arena_roll)
        if self.mode == "hoard" and s.arena_mode == "every":
            # **A hoard has no waves to change on.** *"Changes every 10
            # waves"* has nothing to step through in a mode that is one
            # three-minute run, so it reads as the nearest thing that
            # means something: one place, chosen fresh each run.
            return gamemodes.arena_for(1, "random", None,
                                       int(self.wave_arena_roll))
        return gamemodes.arena_for(wave, s.arena_mode, s.arena_every, pick)

    def arena_changes_now(self, wave):
        return gamemodes.arena_changes_at(
            wave, self.settings.arena_mode, self.settings.arena_every)

    def wave_enemies(self, wave, level, party, count):
        """What walks in on this wave. **One door, so a room agrees.**

        Item 0as, his words: *"you can end up fighting wardens as
        bosses"*. Every tenth wave is a warden - and a warden **here** is
        a statblock and nothing else: no arena briefing, no taunt scene,
        no Memory, no chapter, and nothing written to the profile. Wave
        mode has no story (§59, and he said so again), so there is no
        path for one of these to reach it.
        """
        if gamemodes.boss_wave(wave):
            key = gamemodes.boss_for_wave(
                wave, [b.key for b in BOSS_DEFS])
            definition = BOSS_BY_KEY.get(key)
            if definition is not None:
                # The run's own difficulty, then the depth on top of it -
                # a warden has one printed level and a Last Stand runs
                # straight past it (`boss_scale` says why it is not a
                # level change).
                hp, atk, xp, gold, flee = self.settings.mods
                lift = gamemodes.boss_scale(wave)
                return make_boss_encounter(
                    definition, (hp * lift, atk * lift, xp, gold, flee),
                    party=party)
        return make_encounter(level, "meadow", self.settings.mods,
                              party=party, count=count)

    def announce_arena(self, wave, arena):
        """Say where you are, and only when it changes.

        A wave number every wave is a counter; **a place name when the
        ground changes is a journey.** Both are shown, because the score
        is the wave and the point is the place.
        """
        self.toast("Wave %d." % wave, ACCENT, 2.0)
        if gamemodes.boss_wave(wave) and self.battle is not None:
            # **Named, not briefed.** On the island a warden arrives with
            # a scene and a taunt; here it is a thing that turns up, which
            # is the whole of what "no connection to the story" means.
            self.toast(self.battle.enemy.name, BAD, 3.6)
        if not self.arena_changes_now(wave):
            return
        lap = gamemodes.arena_lap(wave, self.settings.arena_every)
        name = arena.name if not lap else "%s, again" % arena.name
        self.toast(name, GOOD, 3.4)
        self.toast(arena.line, DIM, 4.2)

    def host_open_wave(self, wave, level, count):
        """Open one wave as a shared fight, for everybody in the room.

        **Nobody is asked, and that is the difference from `host_open_fight`.**
        s45's rule is that you are never dragged into a fight you did not
        agree to - and in wave mode the agreement happened at the door:
        joining a Last Stand room *is* saying yes to every wave in it.
        Offering each one would be a prompt every ninety seconds with only
        one sensible answer.
        """
        bodies = self.coop.bodies()
        host_id = self.coop.player_id
        members = [(host_id, self.player)]
        for player_id, body in bodies.items():
            if player_id == host_id or body is None:
                continue
            members.append((player_id, deserialize_player(body)))
        members.sort(key=lambda pair: -pair[1].stat("spd"))
        order = [player_id for player_id, _ in members]

        # **The arena crosses in the payload**, not recomputed on each
        # machine. It is derived from the wave and both sides know the
        # wave - but "both sides can work it out" is how two builds that
        # disagree end up in different fields, and the biome already
        # rides in this dict for exactly that reason.
        arena = self.arena_now(wave)
        seed = rng.rint(1, 2 ** 31 - 2)
        with rng.forked(seed):
            enemies = self.wave_enemies(wave, level, len(members), count)
        self.coop.open_fight({"seed": seed, "level": int(level),
                              "biome": "meadow", "boss": "",
                              "arena": arena.key,
                              "party": order, "wave": int(wave),
                              "count": int(count)})
        self.announce_arena(wave, arena)
        self.start_battle(enemies, party=[body for _, body in members],
                          viewer=order.index(host_id), sort_party=False)
        self.dress_arena(arena)
        return True

    def wave_cleared(self, enemies):
        """A wave was won: pay it, and stand everybody up if it is a tenth.

        The rewards are `gamemodes.wave_rewards`, which is data rather than
        an act - so the intermission can show what the *next* wave pays
        before it is fought, and so a co-op wave pays every player the same
        thing without a second implementation.
        """
        reward = self.waves.clear_wave(kills=len(enemies))
        player = self.player
        player.stats_tracker["kills"] += len(enemies)
        # **The payout itself lives in `gamemodes`** (s60), so that
        # `tools/balance.py` plays the same wave the game does rather than
        # a reimplementation of it. What stays here is presentation: the
        # toasts, the sound, and the co-op publish.
        levels, new_skills, _kept = gamemodes.grant_wave_rewards(
            player, reward,
            roll_item=lambda level: self.roll_loot(level, luck=0.45),
            roll_consumable=lambda: rng.choice(
                ["potion", "hipotion", "ether", "antidote"]))
        if levels:
            self.sfx("levelup")
            for skill in new_skills:
                self.learned_skill(skill)
        if reward["heal"]:
            # *"every 10 waves or something heals everyone"* - everyone,
            # so in a room this is announced rather than only felt. Each
            # machine heals its own hero off the same wave number, which
            # is the same trick as the encounter seed: agreement without a
            # message.
            self.toast("Wave %d: everybody back on their feet."
                       % reward["wave"], GOOD, 5.0)
            self.sfx("heal")
        self.toast("Wave %d cleared. +%d gold, +%d XP"
                   % (reward["wave"], reward["gold"], reward["xp"]),
                   ACCENT, 4.0)
        self.publish_body()
        # **A wave buff dies at the end of the wave it was taken for**
        # (s83). Dropped here rather than when the next one starts, so
        # the intermission shows what you are actually carrying.
        spoils_mod.expire(self.player, self.waves.cleared)
        self.state = WAVES
        self.ui.reset()
        # **And then the choice** (item 0bs). After the payout, because
        # a skill offered before the level-up would be offered against
        # the wrong list of what you already know.
        self.offer_spoils()

    def wave_lost(self):
        """*"if u die u get a score and thats it"*.

        No shrine, no gold lost, no waking up - those are the story mode's
        answer to dying and they exist because that run continues. This one
        does not.
        """
        score = self.waves.end()
        record = meta_mod.RunRecord(
            class_key=self.player.class_key,
            level=self.player.level,
            score=score,
            playtime=self.playtime,
            modifiers=tuple(sorted(self.player.modifiers.keys)),
            when=time.strftime("%Y-%m-%d %H:%M"),
            mode="waves",
            # **How many were in it** (item 0az): a solo Last Stand and a
            # four-player one are different boards, so the record has to
            # carry the number rather than the board guessing it later.
            party=self.wave_party_size(),
            waves=self.waves.cleared)
        # `finish_run` refuses a marked run (s16), which is the answer to
        # "can a debug run post a score": no, the same as every other
        # thing the profile keeps.
        self.meta.finish_run(record)
        self.meta.add_totals(self.player.stats_tracker)
        self.meta.save()
        self.log.event("wave_run_end", waves=self.waves.cleared, score=score)
        self.state = WAVE_OVER
        self.ui.reset()

    def finish_wave_battle(self, battle, enemies):
        """What a won or lost wave means. Solo and host take this together.

        **Fleeing is losing here.** There is nowhere to flee to - the run
        is the arena - so a fight that ends any way but a win ends the run.
        Refusing Flee outright would be the other answer and it is worse:
        s45's rule is that a control on screen works, and the flee that
        saves you in a corridor is the flee that ends a Last Stand.
        """
        if battle.result == "victory":
            self.wave_cleared(enemies)
            return
        self.wave_lost()

    def leave_waves(self):
        """Back to the title, scoring what was reached."""
        if self.in_waves() and not self.waves.over:
            self.wave_lost()
            return
        self.clear_run()
        self.go(TITLE)

    def new_run(self, name="Wanderer", slot=1):
        self.house = None          # a new run starts outdoors (s56)
        # Visible monsters, the waves, a night: none of it is this run's.
        self.forget_the_mode()
        self.slot = slot
        cls = classes_mod.get(self.pending_class)
        # A fresh run starts clean, whatever the last one did.
        self.meta.tainted = False
        self.log.event("run_start", slot=slot, cls=cls.key,
                       difficulty=self.settings.difficulty,
                       ngplus=self.pending_ngplus,
                       modifiers=",".join(
                           sorted(self.pending_modifiers.keys)) or "none")
        self.build_hero(name, cls)

        seed = self.settings.seed
        if seed is None:
            seed = rng.rint(1, 2 ** 31 - 2)
        self.world = World(self.tile, seed)
        self.player.x = self.world.spawn[0] * self.tile + self.tile / 2
        self.player.y = self.world.spawn[1] * self.tile + self.tile / 2
        self.player.full_restore()
        self.quests = QuestLog()
        self.playtime = 0.0
        self.world_time = 60.0
        self.unlock_codex("valley")
        self.enter_overworld(snap=True)
        # **Silently.** The prologue runs next and says all of this at
        # more length, and the toast under it already points at Maren - a
        # card in front of both would be the third screen before the
        # player has moved. Chapter one is recorded as seen instead, so
        # the first card a run shows is the first chapter it *turns*.
        self.refresh_chapter(announce=False)
        # A run made *for* a room opens the room as soon as it exists.
        self._host_if_asked()
        # **The prologue plays for every new hero** (2026-09-01, s48).
        # It was gated on `meta.seen_prologue`, which is a **profile**
        # flag, so the second character anybody ever made started with a
        # toast instead of a story:
        #
        # > "also where did the prologue for the game go, i dont get one
        # > anymore"
        #
        # A new character is a new story, and the story is what this whole
        # project is for. The flag is not wrong, it was just on the wrong
        # scene - see `show_prologue`.
        first_ever = not self.meta.seen_prologue
        if first_ever:
            self.meta.seen_prologue = True
            self.meta.save()
        self.show_prologue(orientation=first_ever)

    def show_prologue(self, orientation=True):
        """Maren's opening, and - once ever - the blunt version after it.

        **Two scenes with two different jobs**, which is why only one of
        them repeats. `prologue` is the story: six roads, a Door, and a
        woman who has said this before. `orientation` is the rules, and it
        opens with *"She sees that you have no idea what she means, and
        sighs"* - a line that is true the first time somebody plays this
        game and faintly insulting the fifth.
        """
        self.play_scene(
            "prologue",
            after=(lambda: self.play_scene("orientation"))
            if orientation else None)

    def unlock_codex(self, key):
        """Record a codex page and tell the player about it, once."""
        entry = self.meta.unlock_codex(key)
        if entry is None:
            return None
        self.meta.save()
        self.toast("Codex: %s" % entry.title, (192, 118, 250), 5.0)
        self.sfx("quest")
        return entry

    def start_ngplus(self):
        """Carry the character forward into a harder, freshly made world."""
        self.pending_ngplus = (self.player.ngplus + 1) if self.player else 1
        self.pending_class = (self.player.class_key if self.player
                              else classes_mod.DEFAULT_CLASS)
        if self.player:
            self.pending_appearance = self.player.appearance
        keys = self.player.modifiers.to_list() if self.player else []
        self.pending_modifiers = RunModifiers(keys)
        name = self.player.name if self.player else "Wanderer"
        self.fade.out(lambda: self.new_run(name, self.slot))

    def current_run(self):
        """The run object this mode scores in, or None (§87).

        Three places worked this out with `self.zombies if self.mode ==
        "zombies" else self.waves`, which answers *the wave run* for
        every mode that is not a night - including a hoard, and
        including a story run. It was harmless by luck rather than by
        design, which is the state the other three mode bugs were in.
        """
        return {"waves": self.waves, "zombies": self.zombies,
                "hoard": self.hoard}.get(self.mode)

    def spoil_luck(self):
        """The `luck` this run's loot is rolled with (item 0bt)."""
        return spoils_mod.luck_for(self.run_bosses())

    def run_bosses(self):
        """How many wardens this run has put down. 0 outside a wave run."""
        run = self.current_run()
        return int(getattr(run, "bosses", 0) or 0)

    def offer_spoils(self, then=""):
        """Put a cleared wave's offers on the screen (item 0bs).

        > at the end of each wave in the last stand gamemode, there
        > should be an option to pick a random skill out of four and then
        > another option to pick a random item

        **Nothing is granted here.** The offers are data until one is
        taken, which is what lets the same call serve a Last Stand
        pausing between waves and a night that is about to open.
        """
        if self.player is None:
            self.spoils, self.spoil_at = [], 0
            return False
        run = self.current_run()
        wave = int(getattr(run, "cleared", 0) or getattr(run, "wave", 1))
        self.spoils = spoils_mod.offers_for(
            self.mode, wave, self.player, bosses=self.run_bosses(),
            roll=lambda level, slot: self.roll_loot(
                level, slot=slot, luck=self.spoil_luck()),
            level=gamemodes.wave_level(wave) + 2,
            # **Was this a warden's wave** (item 0ca). The cards that put
            # you back to full are only in the hand when it was.
            boss=gamemodes.boss_wave(wave))
        self.spoil_at = 0
        self.spoils_then = str(then)
        self.ui.spoil_index = 0
        self.ui.spoil_detail = None
        if not self.spoils:
            self.finish_spoils()
            return False
        self.state = SPOILS
        # **The room is told somebody is reading** (s84). Everyone gets
        # their own offers, so the cards need no arbitration - but *when
        # the next wave starts* does, and starting it while three people
        # are mid-decision is the same failure decision 4 fixed for
        # wardens: a shared moment that does not wait for the room.
        if self.coop is not None and self.coop.role == coop_mod.CLIENT:
            self.coop.say_choosing(True)
        self.sfx("select")
        return True

    def spoil_offer(self):
        """The offer being answered, or None."""
        if 0 <= self.spoil_at < len(self.spoils):
            return self.spoils[self.spoil_at]
        return None

    def spoil_card(self, index):
        """(kind, card) for one card of the current offer, or (None, None).

        **A night's hand carries its own kind per card**, because it is
        drawn across all three; every other offer is one kind for all
        four. Both are unpacked here so no screen has to know which.
        """
        offer = self.spoil_offer()
        if offer is None or not 0 <= int(index) < len(offer.cards):
            return None, None
        card = offer.cards[int(index)]
        if offer.kind == "mixed":
            return card[0], card[1]
        return offer.kind, card

    def take_spoil(self, index):
        """Take the card the cursor is on and move to the next offer."""
        kind, card = self.spoil_card(index)
        if card is None:
            return False
        run = self.current_run()
        wave = int(getattr(run, "cleared", 0) or 0)
        line = spoils_mod.take(self.player, kind, card, wave)
        self.toast("%s. %s" % (spoils_mod.card_name(kind, card), line),
                   GOOD, 4.0)
        self.sfx("levelup" if kind == "skill" else "select")
        self.publish_body()
        self.next_spoil()
        return True

    def leave_spoils(self):
        """Take nothing from this offer.

        **A door that always opens** (rule from §45). A full bag makes an
        item card impossible to accept, and a screen that refuses to let
        you past is worse than one that lets you walk away from a gift.
        """
        self.sfx("back")
        self.next_spoil()

    def next_spoil(self):
        self.spoil_at += 1
        self.ui.spoil_index = 0
        self.ui.spoil_detail = None
        if self.spoil_at >= len(self.spoils):
            self.finish_spoils()

    def finish_spoils(self):
        """Nothing left to offer: go wherever this wave was going."""
        then, self.spoils_then = self.spoils_then, ""
        self.spoils, self.spoil_at = [], 0
        self.ui.spoil_detail = None
        if self.coop is not None and self.coop.role == coop_mod.CLIENT:
            self.coop.say_choosing(False)
        if then == "night":
            self.begin_night()
            return
        if then == "guest_night":
            self.state = ROAM
            return
        self.state = WAVES
        self.ui.reset()

    def event_spoils(self, event):
        """The offer screen's keys.

        **Right-click is the reader** - his *"each when right clicked
        open a small screen that says what each does"* - and the keyboard
        needs the same door, so `Q` opens and closes the same panel. Rule
        8b in the direction it is usually needed in reverse: the mouse
        asked for it, and the keyboard cannot be the one thing that
        cannot.
        """
        offer = self.spoil_offer()
        if offer is None:
            self.finish_spoils()
            return
        count = len(offer.cards)
        index = int(self.ui.spoil_index)
        if event.key in (pygame.K_ESCAPE,):
            self.leave_spoils()
            return
        if event.key in (pygame.K_q, pygame.K_SLASH):
            self.ui.spoil_detail = None if self.ui.spoil_detail == index \
                else index
            self.sfx("select")
            return
        if event.key in (pygame.K_a, pygame.K_LEFT, pygame.K_w, pygame.K_UP):
            index -= 1
        elif event.key in (pygame.K_d, pygame.K_RIGHT, pygame.K_s,
                           pygame.K_DOWN):
            index += 1
        elif event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE):
            self.take_spoil(index)
            return
        else:
            return
        self.ui.spoil_index = int(clamp(index, 0, max(0, count - 1)))
        self.ui.spoil_detail = None
        self.sfx("move")

    def roll_loot(self, *args, **kwargs):
        """Roll an item for this run, with the run's modifiers applied.

        **Every loot roll goes through here** so that Brittle Steel
        cannot be forgotten at one call site - which is how it came to
        do nothing at all for weeks (s54). The starter kit does not:
        it forces `rarity="common"`, which is already the floor.
        """
        kwargs.setdefault("downgrade", self.loot_downgrade())
        return roll_item(*args, **kwargs)

    def loot_downgrade(self):
        if self.player is None:
            return 0
        return 1 if self.player.modifiers.has("downgrade") else 0

    def load_run(self, slot):
        # **A fallen run cannot be continued** (s54, Mortal). Refused here
        # rather than on the row, because every way into a save comes
        # through this one function - the picker, "Back to title" resuming
        # your own run, and the co-op path that puts you back where you
        # were. The picker also draws the row as fallen, so nobody has to
        # find this out by pressing Enter.
        info = self.saves.describe(slot)
        if info and info.get("fallen"):
            self.toast("%s fell. Mortal keeps no second chances."
                       % info.get("name", "That hero"), BAD, 5.0)
            self.sfx("error")
            return False
        result = self.saves.load_into(slot, lambda seed: World(self.tile, seed),
                                      self.settings)
        if result is None:
            self.log.error("load failed slot=%s: %s", slot,
                           self.saves.last_error or "unknown")
            self.toast(self.saves.last_error or "Could not load slot.", BAD, 4.0)
            self.sfx("error")
            return False
        self.player, self.world, self.quests, meta = result
        # **A loaded run is a story run** (s96): whatever mode was on
        # screen before - a Last Stand you were a guest in - is not.
        self.forget_the_mode()
        # **A loaded run is never indoors** (s56). Saving inside writes
        # you standing on the doorstep, so loading has to put the house
        # down as well - otherwise `overworld()` would hand back the
        # island from the *previous* run and the exit door would take
        # you to it.
        self.house = None
        self.slot = slot
        self.playtime = meta["playtime"]
        self.audio.set_volume(self.settings.master_volume)
        self.audio.enabled = self.settings.sfx_enabled
        if meta.get("warning"):
            self.toast(meta["warning"][:70], (250, 200, 96), 6.0)
        self.enter_overworld(snap=True)
        self.meta.tainted = bool(self.player.debug_used)
        self.log.event("load", slot=slot, level=self.player.level,
                       cls=self.player.class_key,
                       warning=meta.get("warning") or "none")
        self.toast("Welcome back, %s." % self.player.name, GOOD, 3.5)
        # **Coming back after a week is the case this exists for.** A save
        # from before chapters has `chapter_seen` at zero, so it gets one
        # card saying where it is and none after.
        self.refresh_chapter()
        self._host_if_asked()
        self.sfx("select")
        return True

    def save_run(self, silent=False, force=False):
        """Write the hero on screen back to wherever it lives.

        **A guest hero goes to the vault, not to a slot** (s19 item 5c). It
        has to be decided here rather than at the call sites: autosave, the
        pause menu, an ending and a fight all save, and any one of them
        forgetting would write somebody else's valley over one of your own
        six runs.
        """
        if not self.player or not self.world:
            return False
        # **One Sitting never writes** (s54). This is the single gate,
        # because every save in the game comes through here - autosave, the
        # pause menu, respawning, an ending and the quit path - and the
        # modifier is only safe if none of them can slip past it.
        #
        # Savvydev asked for "a modifier that deletes the save if you leave";
        # this is that experience built inside out, so there is no code
        # path anywhere whose job is erasing somebody's run. See s54.
        if self.player.modifiers.never_saves():
            if not silent:
                self.toast("One Sitting. This run is not written down.",
                           (240, 96, 140), 3.0)
            return False
        # **A wave run is never written down either** (0ag, s59): *"that
        # gamemode wouldnt have save slots or anything"*. It is gated here
        # for the reason the docstring gives - every save comes through
        # this one function - and it is not merely tidiness: `self.slot`
        # survives from whatever run came before, so without this an
        # autosave in a Last Stand would write the wave hero straight over
        # somebody's story save. That is s7's lost-save bug with a new
        # cause, and it must not be reachable.
        # **Asked of the mode, not of a list of modes** (item 0bk). His
        # words, 2026-09-07: *"the long night gets no save either btw"* -
        # and it did not have this gate, because the gate named wave mode
        # by hand. `Mode.saves` is the fact; a second mode with no slot
        # was always going to be added, and the version that lists them
        # is the version that forgets one.
        #
        # It is not tidiness: `self.slot` survives from whatever run came
        # before, so without this an autosave in a Long Night writes the
        # night's hero straight over somebody's story save. That is §7's
        # lost-save bug with a new cause.
        if not gamemodes.mode_for(self.mode).saves:
            return False
        if self.guest_world is not None:
            return self.save_guest_hero(silent=silent)
        # **Saving indoors saves you outdoors** (s56). A payload built
        # from inside a house would carry a *room's* seed, and loading it
        # would rebuild a kitchen with no island around it. Putting the
        # player back on the doorstep for the length of the write is also
        # the honest answer to closing the game in somebody's front room:
        # you were in the village, and that is where you wake up.
        world, at = self.overworld(), None
        if self.house is not None:
            at = (self.player.x, self.player.y)
            self.player.x, self.player.y = self.house["x"], self.house["y"]
        try:
            payload = build_payload(self.player, world, self.quests,
                                    self.settings, self.playtime)
        finally:
            if at is not None:
                self.player.x, self.player.y = at
        if self.saves.write(self.slot, payload, force=force):
            if not silent:
                self.toast("Saved to slot %d." % self.slot, GOOD, 2.0)
                self.sfx("select")
            return True
        self.log.error("save failed slot=%s: %s", self.slot,
                       self.saves.last_error or "unknown")
        if not silent:
            self.toast(self.saves.last_error or "Save failed.", BAD, 4.0)
            self.sfx("error")
        return False

    def save_guest_hero(self, silent=False):
        """Leave this hero in the valley it belongs to."""
        if self.guest_world is None or not self.player or not self.world:
            return False
        # **A Last Stand or Long Night guest is not a valley's hero** (s96).
        # Their runs keep `guest_world` only because a world has to exist
        # (`new_wave_guest_run`), and leaving the room came straight here -
        # so every one of them was written into the vault, which keeps
        # twelve heroes and evicts the least recently played. A few rooms
        # of Last Stand could push out the hero you have in a friend's
        # valley. `save_run`'s gate, asked here too.
        if not gamemodes.mode_for(self.mode).saves:
            return False
        # **The island, with the hero on the step** - `save_run`'s rule
        # for a slot (s56), which this path never had. From inside a house
        # it wrote the *room* as the valley: the wardens you had felled
        # standing again, every flag gone, and you in a kitchen's
        # coordinates on the island.
        world, at = self.overworld(), None
        if self.house is not None:
            at = (self.player.x, self.player.y)
            self.player.x, self.player.y = self.house["x"], self.house["y"]
        try:
            kept = self.guests.remember(self.guest_world, self.player, world,
                                        self.quests)
        finally:
            if at is not None:
                self.player.x, self.player.y = at
        if kept:
            if not silent:
                self.toast("Hero kept for this valley.", GOOD, 2.0)
                self.sfx("select")
            return True
        self.log.error("guest save failed seed=%s: %s", self.guest_world,
                       self.guests.last_error or "unknown")
        if not silent:
            self.toast(self.guests.last_error or "Could not keep that hero.",
                       BAD, 4.0)
            self.sfx("error")
        return False

    def veteran_saves(self):
        """Your finished runs, newest slot first. Empty is the usual case."""
        return [row for row in self.saves.describe_all()
                if not row.get("empty") and not row.get("corrupt")
                and row.get("beaten")]

    def veteran_import_offered(self, seed):
        """Whether the door has a second option on it.

        **Both sides must have finished** (s19 item 5c): *"they can bring
        that hero from that save into the host\'s game, only if the host had
        beaten the game in the save they\'re hosting too"*. It is a New
        Game+ handshake - one side alone cannot import power into a fresh
        run - and it is why this asks the session, not the profile.
        """
        if self.guests.has(seed):
            return False        # you already have a hero here; nothing to ask
        if not self.coop or not getattr(self.coop, "host_beaten", False):
            return False
        return bool(self.veteran_saves())

    def offer_veteran_import(self, seed):
        """Ask at the door: bring a finished hero, or start fresh here.

        **Offered, never automatic** - Savvydev, 2026-08-29. *"can bring"*
        reads like a choice, and taking a level thirty hero into a friend\'s
        world is not something to have happen to you.

        It reuses the slot picker rather than growing a screen of its own,
        which is the same call 5b made - one picker, several questions, and
        the header says which one it is asking.
        """
        self.guest_pending = int(seed)
        self.ui.slot_mode = "veteran"
        self.ui.slot.index = self.ui.slot.offset = 0
        self.go(SLOTS)

    def import_veteran(self, slot, seed):
        """Carry a finished hero into somebody else\'s valley.

        **A copy, and the slot is untouched.** From here the two diverge:
        what happens to this hero in their world is nothing to do with the
        run it came from. Anything else would mean a friend\'s valley could
        cost you the save you finished.
        """
        result = self.saves.load_into(slot, lambda n: World(self.tile, n),
                                      self.settings)
        if result is None:
            self.toast(self.saves.last_error or "Could not read that run.",
                       BAD, 4.0)
            self.sfx("error")
            return False
        player, _their_world, quests, _meta = result
        if not player.beaten:
            # The picker already filters, so this is the belt to that
            # braces: the only way here is a save that changed underneath.
            self.toast("That run has not been finished.", BAD, 3.5)
            self.sfx("error")
            return False
        self.guest_pending = None
        self.guest_world = int(seed)
        self.forget_the_mode()              # a valley is a story run (s96)
        self.house = None                   # and is entered outdoors (s56)
        self.player = player
        self.quests = quests
        self.world = World(self.tile, int(seed))
        self.world.generate()
        self.player.x = self.world.spawn[0] * self.tile + self.tile / 2
        self.player.y = self.world.spawn[1] * self.tile + self.tile / 2
        self.player.full_restore()
        self.playtime = 0.0
        self.meta.tainted = bool(self.player.debug_used)
        self.enter_overworld(snap=True)
        self.apply_pending_chests()
        self.save_guest_hero(silent=True)
        self.publish_body()
        self.log.event("veteran_import", seed=int(seed), slot=slot,
                       level=self.player.level)
        host = getattr(self.coop, "host_name", "") if self.coop else ""
        self.toast("%s walks into %s valley."
                   % (self.player.name, ("%s's" % host) if host else "their"),
                   GOOD, 5.0)
        self.sfx("select")
        return True

    def begin_guest_creation(self, seed):
        """A valley you have never been in: make a hero for it.

        **Full character creation**, settled by Savvydev on 2026-08-29 with a
        quicker class-only pick on the table and turned down. A hero you may
        play for hours is worth the minute, and *"level one, no existing
        hero or anything"* means what it says.

        It reuses the ordinary creation chain rather than growing a second
        one - class, modifiers, name - and `guest_pending` is what makes the
        last step build a guest hero instead of writing a slot.
        """
        self.guest_pending = int(seed)
        self.pending_class = classes_mod.DEFAULT_CLASS
        self.pending_modifiers = RunModifiers()
        self.pending_ngplus = 0
        self.pending_appearance = Appearance()
        self.ui.class_index = 0
        self.go(CLASS_SELECT)
        host = getattr(self.coop, "host_name", "") if self.coop else ""
        self.toast("New valley%s - make a hero for it."
                   % ((" of %s's" % host) if host else ""), ACCENT, 5.0)

    def begin_wave_guest(self, seed):
        """Join a Last Stand room: make a hero for it and wait for wave 1.

        The same creation chain as any other guest - class, name, no
        modifiers (they are the host's, s46) - with `wave_guest` deciding
        what the last step builds.
        """
        self.guest_pending = int(seed)
        self.wave_guest = True
        self.pending_class = classes_mod.DEFAULT_CLASS
        self.pending_modifiers = RunModifiers()
        self.pending_ngplus = 0
        self.pending_appearance = Appearance()
        self.ui.class_index = 0
        self.go(CLASS_SELECT)

    def new_wave_guest_run(self, name, seed):
        """A hero for somebody else's Last Stand.

        No slot, no vault, no island of our own - the host's seed is kept
        only because `coop._island` and `save_run` both expect a world to
        exist, and a guest's world has to be the host's (hard rule 4).
        """
        self.guest_pending = None
        self.wave_guest = False
        self.guest_world = int(seed)
        self.house = None
        self.mode = "waves"
        cls = classes_mod.get(self.pending_class)
        self.meta.tainted = False
        self.log.event("wave_guest_start", seed=int(seed), cls=cls.key)
        self.build_hero(name, cls)
        self.world = World(self.tile, int(seed))
        self.player.x = self.world.spawn[0] * self.tile + self.tile / 2
        self.player.y = self.world.spawn[1] * self.tile + self.tile / 2
        self.player.full_restore()
        self.quests = QuestLog()
        self.playtime = 0.0
        self.slot = 0
        self.waves = gamemodes.WaveRun(
            # **The host's difficulty, not ours.** The monsters were rolled
            # on their machine, so scoring against a local setting would be
            # scoring a game nobody played (s59).
            difficulty=getattr(self.coop, "difficulty",
                               self.settings.difficulty),
            modifier_score=self.pending_modifiers.score)
        self.publish_body()
        self.ui.reset()
        host = getattr(self.coop, "host_name", "") if self.coop else ""
        if getattr(self.coop, "mode", "waves") == "hoard":
            # Same shape as the night below: wait on the intermission
            # until the host opens the arena (§86).
            self.mode = "hoard"
            self.hoard = gamemodes.HoardRun(
                difficulty=getattr(self.coop, "difficulty",
                                   self.settings.difficulty),
                modifier_score=self.pending_modifiers.score)
            self.waves = self.zombies = None
            self.state = WAVES
            self.toast("%s's hoard. Wait for the fire to be lit."
                       % (host or "Their"), ACCENT, 6.0)
            return
        if getattr(self.coop, "mode", "waves") == "zombies":
            # **Waiting, not fighting.** The night opens when the host
            # opens it; until then this is the same intermission a Last
            # Stand guest sits in, which is a screen that already exists.
            self.mode = "zombies"
            self.zombies = gamemodes.NightRun(
                difficulty=getattr(self.coop, "difficulty",
                                   self.settings.difficulty),
                modifier_score=self.pending_modifiers.score)
            self.waves = None
            self.state = WAVES
            self.toast("%s's long night. Wait for dark."
                       % (host or "Their"), ACCENT, 6.0)
            return
        self.state = WAVES
        self.toast("%s's last stand. Wait for wave one."
                   % (host or "Their"), ACCENT, 6.0)

    def new_guest_run(self, name, seed):
        """A level-one hero, made for somebody else's valley.

        The same build as `new_run` minus everything that belongs to a slot:
        no `self.slot`, no prologue, no autosave into your own save set. The
        world is the host's, so it is not rolled here.
        """
        self.guest_pending = None
        self.guest_world = int(seed)
        self.forget_the_mode()              # a valley is a story run (s96)
        self.house = None                   # and is entered outdoors (s56)
        cls = classes_mod.get(self.pending_class)
        self.meta.tainted = False
        self.log.event("guest_run_start", seed=int(seed), cls=cls.key)
        self.player = Player(sec.sanitize_name(name), cls.key)
        self.player.appearance = self.pending_appearance
        self.player.modifiers = self.pending_modifiers
        self.player.ngplus = 0

        for base_name in (cls.start_weapon, cls.start_armor):
            base = BASES_BY_NAME.get(base_name)
            item = roll_item(1, rarity="common", base=base)
            item.name = base_name
            self.player.add_item(item)
            self.player.equip(item)
        self.player.consumables = {}
        for key, count in cls.start_consumables:
            self.player.add_consumable(key, count)
        bonus = 1 if cls.signature == "fortune" else 0
        pool = [r.key for r in relics_mod.COMMON_RELICS]
        for key in rng.sample(pool, min(bonus, len(pool))):
            self.player.add_relic(key)

        self.world = World(self.tile, int(seed))
        self.player.x = self.world.spawn[0] * self.tile + self.tile / 2
        self.player.y = self.world.spawn[1] * self.tile + self.tile / 2
        self.player.full_restore()
        self.quests = QuestLog()
        self.playtime = 0.0
        self.world_time = 60.0
        self.unlock_codex("valley")
        self.enter_overworld(snap=True)
        self.refresh_chapter(announce=False)
        # The valley is only now built, so this is the first moment the
        # host's already-opened chests have anything to land on.
        self.apply_pending_chests()
        self.save_guest_hero(silent=True)
        self.publish_body()
        host = getattr(self.coop, "host_name", "") if self.coop else ""
        self.toast("Welcome to %s valley." % (("%s's" % host) if host
                                              else "their"), GOOD, 5.0)

    # How long a chapter card holds before it lifts on its own, in
    # seconds. Long enough to read twice; any key or click skips it, the
    # way the opening card does.
    CHAPTER_HOLD = 4.2
    CHAPTER_FADE = 0.6

    def chapter(self):
        """The chapter this run is in, or None outside a run."""
        if not self.world:
            return None
        return story.chapter_for(self.world.wardens_defeated())

    def refresh_chapter(self, announce=True):
        """Notice that the story has moved, and say so once.

        **The card is shown once per run per chapter**, which is what
        `Player.chapter_seen` is for. Re-entering a save, reloading, or
        walking back past the same portal must not replay it - a moment
        that repeats stops being a moment.
        """
        chapter = self.chapter()
        if chapter is None or not self.player:
            return
        if chapter.number <= self.player.chapter_seen:
            return
        self.player.chapter_seen = chapter.number
        if not announce:
            return
        self.chapter_card = chapter
        self.chapter_time = 0.0
        self.go(CHAPTER_CARD)
        self.sfx("unlock")
        self.log.event("chapter", number=chapter.number, title=chapter.title)

    def update_chapter_card(self, dt):
        self.chapter_time += dt
        if self.chapter_time >= self.CHAPTER_HOLD + self.CHAPTER_FADE:
            self.close_chapter_card()

    def close_chapter_card(self):
        self.chapter_card = None
        then, self.after_card = self.after_card, None
        if self.player and self.world:
            self.enter_overworld()
        else:
            self.go(TITLE)
            return
        if then is not None:
            then()

    def skip_chapter_card(self):
        """Any key, any click - the same contract as the opening card."""
        if self.state == CHAPTER_CARD:
            self.close_chapter_card()
            return True
        return False

    def enter_overworld(self, snap=False):
        # **Every path into the world comes through here** - new run, load,
        # a guest hero, adopting a host's valley - which makes it the one
        # place a modifier can take something out of the map and have it
        # stay out across a reload (hard rule 4 rebuilds the terrain).
        self.strip_world_for_modifiers()
        self.state = OVERWORLD
        self.camera.resize(*self.screen.get_size())
        self.camera.follow(*self.camera_target(), 0.0, snap=snap)
        self.hp_tween.set(self.player.hp / max(1, self.player.max_hp()), snap)
        self.mp_tween.set(self.player.mp / max(1, self.player.max_mp()), snap)
        self.last_tile = self.player_tile()

    def go(self, state):
        self.previous_state = self.state
        self.state = state
        self.ui.reset()

    # ------------------------------------------------------------ overworld ---

    def move_player(self, dt, keys):
        speed = self.tile * 4.6
        dx = dy = 0.0

        def held(action):
            """Bound key, plus the arrow keys, which always walk."""
            if keys[self.binds.key_for(action)]:
                return True
            return any(keys[code] for code, name in FALLBACK_MOVEMENT.items()
                       if name == action)

        if held("left"):
            dx -= 1
        if held("right"):
            dx += 1
        if held("up"):
            dy -= 1
        if held("down"):
            dy += 1
        if keys[self.binds.key_for("sprint")] or keys[pygame.K_RSHIFT]:
            speed *= 1.55

        if dx or dy:
            if dx and dy:
                dx *= 0.7071
                dy *= 0.7071
            self.player.facing = ("left" if dx < 0 else "right") if abs(dx) > abs(dy) \
                else ("back" if dy < 0 else "front")
            self.walk_anim += dt * (8.0 if speed > self.tile * 5 else 6.0)
            self._try_move(dx * speed * dt, dy * speed * dt)
            # **A step is a sixtieth of a second of walking, not a frame**
            # (§95). Counted per frame, "Walk 900 steps" took fifteen
            # seconds at 60fps, twenty-two at 40 and six at 144 - an
            # errand that ran at the speed of the computer. At the default
            # 60 this counts exactly what it always did.
            self.step_carry += dt * 60.0
            whole = int(self.step_carry)
            self.step_carry -= whole
            self.player.stats_tracker["steps"] += whole
            self.sfx("step", volume=0.35, throttle=0.28)
        # **The clock keeps running while the blend falls away.** Zeroing
        # `walk_anim` here is what made stopping a cut rather than a
        # settle; the pose asks `walk_ease` how much of it to show, so the
        # stride can finish its arc on the way out.
        self.walk_ease = anim.walk_ease(self.walk_ease, dt, bool(dx or dy))
        if not self.walk_ease:
            self.walk_anim = 0.0
        self.walk_running = speed > self.tile * 5

    def _try_move(self, dx, dy):
        """Axis-separated movement so sliding along a wall feels right.

        **The move that is tested is the move that is made** (§95). This
        probed the box shifted by the *rounded* step - two pixels - and
        then moved by the real one, two and a half at 60fps; the extra
        half could carry the box a pixel into a wall. From there its
        corner was in the wall's row, the sideways probe hit the wall too,
        and walking diagonally along a house stopped dead about one
        approach in thirty. A blocked step now closes the gap a pixel at a
        time instead, so a wall is touched, never entered and never
        stopped short of.
        """
        # Somebody already inside a wall - an old save, a spawn that went
        # wrong - is let out rather than frozen there: every step would
        # test as blocked, and a player who cannot move cannot fix it.
        trapped = self._collides(self.player_rect())
        for axis, delta in (("x", dx), ("y", dy)):
            if not delta:
                continue
            start = getattr(self.player, axis)
            setattr(self.player, axis, start + delta)
            if trapped or not self._collides(self.player_rect()):
                continue
            setattr(self.player, axis, start)
            step = 1.0 if delta > 0 else -1.0
            moved = 0.0
            while abs(moved + step) <= abs(delta):
                setattr(self.player, axis, start + moved + step)
                if self._collides(self.player_rect()):
                    setattr(self.player, axis, start + moved)
                    break
                moved += step

    def _collides(self, rect):
        for px, py in ((rect.left, rect.top), (rect.right - 1, rect.top),
                       (rect.left, rect.bottom - 1),
                       (rect.right - 1, rect.bottom - 1)):
            if self.world.blocked(px // self.tile, py // self.tile):
                return True
        return False

    # **A field boss is a rare wandering encounter as well as a place you
    # can walk to** (s42). Savvydev, 2026-08-29:
    #
    # > "field bosses arent just a one-time encounter, you can encounter
    # > them multiple times with js like a low encounter rate and a cool
    # > down so u dont get "lucky" and encounter them like 5 times in a
    # > row"
    #
    # Both halves are needed and they do different jobs. **The rate** makes
    # it rare enough to be an event. **The cooldown** is what stops rarity
    # from being unreliable - a low chance rolled often enough will
    # eventually come up three times in a minute, and the player will not
    # experience that as luck, they will experience it as the game being
    # broken.
    FIELD_BOSS_CHANCE = 0.012          # per encounter that was going to happen
    FIELD_BOSS_COOLDOWN = 240.0        # seconds before another may be rolled

    def field_boss_ready(self):
        """Whether a wandering field boss may happen at all right now."""
        return self._field_cooldown.ready()

    def roll_field_boss(self, biome):
        """A field boss for this biome, or None. Consumes the cooldown."""
        from .entities import FIELD_BOSSES, field_variant
        if not self.field_boss_ready() or not rng.chance(
                self.FIELD_BOSS_CHANCE):
            return None
        here = siblings_of(biome)
        pool = [b for b in FIELD_BOSSES if b.biome in here]
        if not pool:
            return None
        family = rng.choice(pool)
        boss = make_field_boss(family.key, field_variant(),
                               player_level=self.player.level)
        if boss is None:
            return None
        self._field_cooldown.trigger()
        # `name` is `event()`'s own first parameter - a field called
        # `name` collides with it. Same trap for any structured log line.
        self.log.event("field_boss", key=family.key, who=boss.name,
                       biome=biome, how="wandering")
        return boss

    # How many monsters are on screen at once under Plain Sight, how far
    # out they may spawn, and how close is close enough to be caught.
    #
    # **Six is the pack cap, not a coincidence.** More than that walking
    # about reads as an infestation rather than a valley, and it is the
    # same number `entities.MAX_PACK` uses for the fight they turn into.
    WANDER_MAX = 6
    WANDER_NEAR = 9        # tiles: no closer than this when it appears
    WANDER_FAR = 22        # tiles: gone once it is this far behind you
    WANDER_SPEED = 26.0    # pixels a second - slower than you, on purpose
    WANDER_CATCH = 0.55    # tiles: how close it has to get
    #: How far a field boss strays from its own lair, in tiles. **It
    #: patrols rather than hunts**: something four times your level
    #: following you across the valley is a different game, and §42's
    #: whole point about a lair is that it is a place you decide to go.
    BOSS_LEASH = 5
    BOSS_SPEED = 14.0

    def wanderers_on(self):
        """Whether monsters are walked into rather than stumbled upon."""
        # **"Is there an island to walk on"**, which is what `island`
        # means. Written as `!= "waves"` it was already wrong for the two
        # arena modes that came later - they have a world they never
        # walk, and wanderers were being moved around it (§88).
        return bool(self.player
                    and self.player.modifiers.has("seen")
                    and self.house is None
                    and gamemodes.mode_for(self.mode).island)

    def update_wanderers(self, dt):
        """Move what is on the map, retire what is behind you, spawn more.

        **One sprite is one encounter, not one monster** - his words:
        *"in the overworld, only one enemy is shown, but when battle
        begins there can be multiple"*. So a wanderer carries the level
        and the biome and nothing else; `make_encounter` still rolls the
        pack when the fight opens, exactly as a random encounter would.
        """
        if not self.wanderers_on():
            if self.wanderers:
                self.wanderers = []
            return
        tile = self.tile
        px, py = self.player.x, self.player.y
        kept = []
        for one in self.wanderers:
            gap = math.hypot(one["x"] - px, one["y"] - py)
            if gap > self.WANDER_FAR * tile:
                continue                      # wandered off; forget it
            # **They drift toward you, slowly.** Standing still has to be
            # a decision rather than a hiding place, and something that
            # ran at you would make walking anywhere a chase.
            if gap > 1.0:
                step = self.WANDER_SPEED * dt
                self.step_wanderer(one, (px - one["x"]) / gap * step,
                                   (py - one["y"]) / gap * step)
            one["bob"] = one.get("bob", 0.0) + dt
            kept.append(one)
        self.wanderers = kept
        self.update_boss_wanderers(dt)
        if self.battle is None and self.state == OVERWORLD:
            self.catch_wanderer()
        while len(self.wanderers) < self.WANDER_MAX:
            if not self.spawn_wanderer():
                break

    def walking_lairs(self):
        """Which lairs currently have their tenant out walking.

        Read from `self.wanderers` rather than stored, so there is one
        answer and it cannot go stale - a boss that has been fought, or
        that fell off the back of the map, is simply not in the list.
        """
        return {one["lair"] for one in self.wanderers if one.get("lair")}

    def update_boss_wanderers(self, dt):
        """Field bosses patrol their own lair (his, 2026-09-06).

        > "the mini bosses should walk around"

        **They patrol, and they still ask.** §42 settled that a field boss
        is *"asked, not sprung"* - walking into something four times your
        level should be your decision - and he asked for them to move, not
        for that to stop. So one walks up to you and the same dialogue
        opens: fight it, or leave it standing.
        """
        world, tile = self.world, self.tile
        if world is None:
            return
        px, py = self.player.x, self.player.y
        here = self.walking_lairs()
        for one in self.wanderers:
            if not one.get("lair"):
                continue
            # **A leashed wander, not a circuit.** Steering at a fixed
            # point on a circle around the den presses the boss into
            # whatever wall lies between it and that point. A random
            # heading, re-rolled when it expires or when the way is
            # blocked, goes round obstacles for free and reads as an
            # animal rather than as a turntable.
            home_x = one["lair"][0] * tile + tile / 2
            home_y = one["lair"][1] * tile + tile / 2
            one["turn"] = one.get("turn", 0.0) - dt
            from_den = math.hypot(one["x"] - home_x, one["y"] - home_y)
            if one["turn"] <= 0.0:
                one["turn"] = rng.rfloat(0.9, 2.2)
                if from_den > self.BOSS_LEASH * tile:
                    # On the leash: head back, roughly.
                    one["head"] = math.atan2(home_y - one["y"],
                                             home_x - one["x"])                         + rng.rfloat(-0.7, 0.7)
                else:
                    one["head"] = rng.rfloat(0.0, 6.283)
            step = self.BOSS_SPEED * dt
            if not self.step_wanderer(one,
                                      math.cos(one["head"]) * step,
                                      math.sin(one["head"]) * step,
                                      hunting=False):
                one["turn"] = 0.0        # blocked: try somewhere else now
        # Wake the ones near enough to be seen.
        for spot, entry in world.lairs.items():
            if spot in here:
                continue
            if math.hypot(spot[0] * tile + tile / 2 - px,
                          spot[1] * tile + tile / 2 - py) > self.WANDER_FAR * tile:
                continue
            found = world.lair_at(spot[0], spot[1], self.playtime)
            if not found:
                continue
            boss, resting = found
            if resting:
                continue          # felled recently; the den is empty (s42)
            shown = make_field_boss(boss.key, world.lair_variant(spot),
                                    player_level=self.player.level) or boss
            self.wanderers.append({
                "x": spot[0] * tile + tile / 2,
                "y": spot[1] * tile + tile / 2,
                "biome": world.biome_map[spot[1]][spot[0]],
                "level": int(shown.level),
                "sprite": shown.sprite,
                "bob": rng.rfloat(0.0, 3.0),
                "turn": 0.0,
                "head": rng.rfloat(0.0, 6.283),
                "lair": spot,
            })

    def wander_ground(self, gx, gy, hunting=True):
        """Whether a monster may stand on this tile.

        **The same question the spawn asks**, so a monster cannot walk
        somewhere it could not have appeared. Two rules ride on that:
        §42's *"a road never spawns an encounter"* stays true when the
        encounter has legs, and a wanderer cannot drift through a rock or
        out onto the water - which it happily did until a test asked where
        they had all ended up.
        """
        world = self.world
        if world is None or not world.in_bounds(gx, gy):
            return False
        if not world.on_land(gx, gy) or world.blocked(gx, gy):
            return False
        if world.town_rect.collidepoint(gx, gy):
            return False
        # **`hunting` is the road rule, and only ambushes are bound by
        # it.** §42 says a road never *spawns* an encounter; a field boss
        # is asked rather than sprung (§42 again), so one crossing a path
        # in front of you ambushes nobody - you can see it, and it still
        # asks before it fights.
        #
        # It also gives a boss room to patrol: **a lair's own tile has an
        # encounter chance of zero**, and so do a good many of the tiles
        # around one, so holding a boss to the ambush rule would pen it
        # into whatever fragment of its den's surroundings happened to
        # qualify.
        if not hunting:
            return True
        return world.encounter_chance(gx, gy) > 0

    def step_wanderer(self, one, dx, dy, hunting=True):
        """Move one, per axis, refusing ground it may not stand on.

        Per axis rather than per step so a monster slides along a road or
        a cliff instead of stopping dead against it - stopping dead is how
        you get a queue of them piled against a hedge.
        """
        tile = self.tile
        # **A body already standing somewhere it may not be can always
        # leave.** A lair's own tile has an encounter chance of zero, so a
        # field boss spawned on its den failed this check on every axis of
        # every frame and stood there for ever - which looked exactly like
        # the patrol not being wired up.
        stuck = not self.wander_ground(int(one["x"] // tile),
                                       int(one["y"] // tile), hunting)
        moved = False
        for axis, delta in (("x", dx), ("y", dy)):
            was = one[axis]
            one[axis] = was + delta
            gx, gy = int(one["x"] // tile), int(one["y"] // tile)
            if not stuck and not self.wander_ground(gx, gy, hunting):
                one[axis] = was
            elif delta:
                moved = True
        return moved

    def spawn_wanderer(self):
        """Put one monster on a walkable tile out of sight of the player."""
        world, tile = self.world, self.tile
        if world is None or self.player is None:
            return False
        gx, gy = self.player_tile()
        for _ in range(40):
            angle = rng.rfloat(0.0, 6.283)
            away = rng.rint(self.WANDER_NEAR, self.WANDER_FAR - 2)
            tx = int(gx + math.cos(angle) * away)
            ty = int(gy + math.sin(angle) * away)
            if not world.in_bounds(tx, ty) or not world.on_land(tx, ty):
                continue
            if (tx, ty) in world.props:
                continue
            if not self.wander_ground(tx, ty):
                continue              # roads stay safe (s42), and so does town
            biome = world.biome_map[ty][tx]
            level = clamp(world.suggested_level(tx, ty, self.player.level)
                          + rng.rint(-1, 1), 1, MAX_LEVEL)
            self.wanderers.append({
                "x": tx * tile + tile / 2, "y": ty * tile + tile / 2,
                "biome": biome, "level": int(level),
                "sprite": make_enemy(level, biome,
                                     self.settings.mods).sprite,
                "bob": rng.rfloat(0.0, 3.0),
            })
            return True
        return False

    def catch_wanderer(self):
        """Walk into one and the fight starts."""
        tile = self.tile
        for one in list(self.wanderers):
            if math.hypot(one["x"] - self.player.x,
                          one["y"] - self.player.y) > self.WANDER_CATCH * tile:
                continue
            if one.get("lair"):
                # **A field boss is asked, not sprung** (§42). It walked up
                # to you; the decision is still yours. Left in the list so
                # declining does not delete it from the map - it wanders
                # off and can be met again.
                if self.dialogue is None and self.state == OVERWORLD:
                    self.approach_lair(one["lair"], from_wanderer=True)
                return True
            self.wanderers.remove(one)
            # **Through the same door a random encounter uses**, so co-op
            # arbitration, difficulty and the party scaling all behave
            # exactly as they already do (s21).
            if self.coop is not None and self.coop.ready:
                if self.coop.role == coop_mod.HOST:
                    gx, gy = self.player_tile()
                    self.host_open_fight(one["level"], one["biome"], gx, gy)
                else:
                    self.coop.request_fight(one["level"], one["biome"])
                return True
            self.start_battle(make_encounter(
                one["level"], one["biome"], self.settings.mods,
                party=self.party_size()))
            return True
        return False

    def check_encounter(self):
        # **Nothing hunts you indoors** (s56). A room is a `World` and its
        # floor is stone, which has an encounter rate out on the island -
        # so without this a wolf could find you in a kitchen.
        if self.house is not None:
            return
        # **Plain Sight replaces the ambush, it does not add to it**
        # (item 0aj, s68). Monsters you can see *and* monsters you cannot
        # would be twice the fights, which is a difficulty change nobody
        # asked for - his ask was *"instead of"*.
        if self.wanderers_on():
            return
        gx, gy = self.player_tile()
        if (gx, gy) == self.last_tile:
            return
        self.last_tile = (gx, gy)
        chance = self.world.encounter_chance(gx, gy)
        if chance <= 0 or not rng.chance(chance):
            return
        biome = self.world.biome_map[gy][gx]
        level = clamp(
            self.world.suggested_level(gx, gy, self.player.level)
            + rng.rint(-1, 1), 1, MAX_LEVEL)
        # **Solo only, deliberately.** A shared fight is arbitrated by the
        # host (s21) and a wandering boss rolled on a guest's machine would
        # be a fight the host never agreed to. Lairs work in a room; this
        # does not, and saying so is better than a rare desync.
        if self.coop is None:
            boss = self.roll_field_boss(biome)
            if boss is not None:
                self.toast("%s has found you." % boss.name, BAD, 5.0)
                self.start_battle(make_boss_encounter(
                    boss, self.settings.mods, party=1))
                return
        if self.coop is not None and self.coop.ready:
            # In a room the host rolls every encounter, including the ones
            # somebody else walked into. Warden arenas are the exception and
            # stay private - each player earns their own Memory.
            if self.coop.role == coop_mod.HOST:
                self.host_open_fight(level, biome, gx, gy)
            else:
                self.coop.request_fight(level, biome)
            return
        self.start_battle(make_encounter(level, enemy_kin(biome),
                                         self.settings.mods,
                                         party=self.party_size()))

    # -------------------------------------------------------------- battle ---

    def ask_open_devmenu(self):
        """Confirm before opening. Ten keys is deliberate, but not a decision.

        Entering costs nothing by itself - the run is only marked when a power
        is actually used - and the prompt says so rather than overstating it,
        because a warning that turns out to be untrue is one people learn to
        ignore.
        """
        if not self.player:
            return
        if self.player.debug_used:
            # Already marked: there is nothing left to warn about, and a
            # warning people cannot act on is just a keypress in the way.
            self.open_devmenu()
            return
        self.dialogue = {
            "title": "A door that was not meant for you",
            "body": ["Beyond this is a debug menu: gold, levels, items,",
                     "invulnerability, whatever you like.",
                     "",
                     "The moment you use any of it, this run is marked.",
                     "A marked run can never again add to your codex,",
                     "your endings, or your records - and that mark is",
                     "saved with it. There is no taking it back.",
                     "",
                     "Opening the door costs nothing. Using it does."],
            "confirm": "Open it",
            "on_confirm": self.open_devmenu,
            "on_cancel": lambda: setattr(self, "state", OVERWORLD),
        }
        self.go(DIALOGUE)
        self.sfx("error")

    def take_item(self, item, source=""):
        """Put an item in the bag, or ask what to do about the bag.

        **The game used to decide for you** (2026-09-01, s48). A full bag
        meant the thing you had just earned was gone, with a toast to say
        so - the same shape as the Flee row that refuses and the shrine
        that will not heal: *the game making a choice you would have made
        differently.*

        Returns True when the item is now yours, False when the player has
        been asked instead. Nothing is thrown away without somebody
        choosing it.
        """
        if item is None or self.player is None:
            return False
        if self.player.add_item(item):
            self.toast("Found %s" % item.name, item.color, 4.5)
            self.sfx("item")
            return True
        self.ui.fullbag = {"item": item, "source": source}
        self.ui.fullbag_index = 0
        self.go(FULLBAG)
        self.sfx("error")
        return False

    def droppable(self):
        """What the full-bag question may offer to throw away.

        **Not what you are wearing.** Dropping equipped gear to make room
        has a second half - now you are wearing nothing - and that belongs
        on the bag screen where you can see what replaces it.
        """
        if self.player is None:
            return []
        return [i for i in self.player.inventory
                if self.player.equipment.get(i.slot) is not i]

    def resolve_full_bag(self, keep):
        """Answer it: `keep` is which item to throw out, or None to leave
        the new one behind."""
        state = self.ui.fullbag or {}
        item = state.get("item")
        self.ui.fullbag = None
        self.go(BATTLE if self.battle is not None else OVERWORLD)
        if item is None:
            return
        if keep is None:
            self.toast("%s left behind." % item.name, DIM, 3.0)
            self.sfx("back")
            return
        pool = self.droppable()
        if not (0 <= keep < len(pool)):
            return
        dropped = pool[keep]
        self.player.remove_item(dropped)
        self.player.add_item(item)
        self.toast("Dropped %s for %s." % (dropped.name, item.name),
                   ACCENT, 4.0)
        self.sfx("item")

    def event_fullbag(self, event):
        pool = self.droppable()
        index = int(getattr(self.ui, "fullbag_index", 0))
        if event.key in (pygame.K_w, pygame.K_UP):
            self.ui.fullbag_index = (index - 1) % max(1, len(pool))
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            self.ui.fullbag_index = (index + 1) % max(1, len(pool))
            self.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e):
            self.resolve_full_bag(index if pool else None)
        elif event.key in (pygame.K_ESCAPE, pygame.K_x):
            self.resolve_full_bag(None)

    # -------------------------------------------------------- a new skill ---
    #
    # **Roadmap item 0ac** (2026-09-05, s53). Savvydev, third school playtest:
    #
    # > "maybe add a pop up window asking you if you want to replace a skill
    # > with the one you just learned and what that skill does"
    #
    # This is s48's full-bag question in a second place. A loadout of four
    # that is already full, a fifth thing arriving, and the game quietly
    # deciding - and the rule there was that **the game never throws away
    # something you would have kept**.
    #
    # It also answers 0ab, which he withdrew himself: *"use a pop-up
    # notification when you learn a new skill... (nevermind later found out
    # there already is one, i just wasnt paying proper attention)"*. There
    # was one, and it was a toast among the four other toasts a won fight
    # raises. A screen is not a toast.

    def learned_skill(self, skill):
        """A new ability. Carry it, or ask which one it replaces."""
        from .entities import LOADOUT_SIZE
        player = self.player
        if player is None:
            return
        carried = [s.key for s in player.skills()]
        if len(carried) < LOADOUT_SIZE:
            # Nothing to decide: there is a free hand.
            player.equip_skill(skill.key)
            self.toast("Learned %s - equipped." % skill.name,
                       (192, 118, 250), 5.0)
            return
        self.ui.newskill = {"skill": skill, "carried": carried}
        self.ui.newskill_index = 0
        self.go(NEWSKILL)
        self.sfx("levelup")

    def resolve_new_skill(self, replace):
        """`replace` is which carried skill to put down, or None to keep
        the four you have."""
        state = self.ui.newskill or {}
        skill = state.get("skill")
        carried = list(state.get("carried", ()))
        self.ui.newskill = None
        self.go(BATTLE if self.battle is not None else OVERWORLD)
        if skill is None:
            return
        if replace is None or not (0 <= replace < len(carried)):
            self.toast("%s learned. Not carried." % skill.name, DIM, 4.0)
            self.sfx("back")
            return
        from .skills import SKILLS_BY_KEY
        old = SKILLS_BY_KEY.get(carried[replace])
        self.player.equip_skill(carried[replace])      # put it down
        self.player.equip_skill(skill.key)             # take the new one
        self.toast("%s replaces %s."
                   % (skill.name, old.name if old else "it"),
                   (192, 118, 250), 5.0)
        self.sfx("select")

    def event_newskill(self, event):
        state = self.ui.newskill or {}
        carried = list(state.get("carried", ()))
        index = int(getattr(self.ui, "newskill_index", 0))
        if event.key in (pygame.K_w, pygame.K_UP):
            self.ui.newskill_index = (index - 1) % max(1, len(carried))
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            self.ui.newskill_index = (index + 1) % max(1, len(carried))
            self.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e):
            self.resolve_new_skill(index if carried else None)
        elif event.key in (pygame.K_ESCAPE, pygame.K_x):
            self.resolve_new_skill(None)

    # ------------------------------------------------------------- trading ---
    #
    # **Roadmap item 0x.** Decided on 2026-08-24 - *"no limits. Any gold,
    # any gear"* - and asked about again on 2026-09-01, because it had
    # never been built. `trade.py` holds the offer and the transfer;
    # `coop.py` moves the messages; this is the half a player touches.

    def trade_partner(self):
        """Whoever is close enough to trade with, or None.

        Nearest wins when two people are standing together, because the
        alternative is a prompt that names one of them arbitrarily and
        trades with the other.
        """
        if self.coop is None or not self.coop.ready or self.player is None:
            return None
        tile = max(1, int(self.tile))
        best, best_gap = None, coop_mod.TRADE_REACH
        for remote in self.coop.here():
            gap = math.hypot(remote.x / tile - self.player.x / tile,
                             remote.y / tile - self.player.y / tile)
            if gap <= best_gap:
                best, best_gap = remote, gap
        return best

    def start_trade(self):
        """Ask the person you are standing next to."""
        if self.state != OVERWORLD or self.battle is not None:
            return False
        if self.trade is not None:
            self.toast("You are already trading.", DIM, 2.0)
            return False
        partner = self.trade_partner()
        if partner is None:
            self.toast("Nobody close enough to trade with.", DIM, 2.0)
            self.sfx("error")
            return False
        # **Published before the question, not after.** The host checks an
        # offer against the last body its owner sent (`trade.covers`), so a
        # stale one turns an honest trade into a refusal.
        self.publish_body()
        self.trade = trade_mod.TradeSession(partner.player_id, partner.name,
                                            trade_mod.INVITING)
        if not self.coop.invite_trade(self, partner.player_id):
            self.trade = None
            self.toast("Could not reach %s." % partner.name, BAD, 2.5)
            return False
        self.toast("Asked %s to trade." % partner.name, ACCENT, 3.0)
        self.sfx("select")
        return True

    # `publish_body` is further down, where the shared-battle code that has
    # always used it lives. A second copy was written here during the trade
    # pass and Python kept the *later* definition, so it did nothing at all
    # - the silent no-op rule 31.6 is about, in a new shape.

    # -- what the room says to us ------------------------------------------

    def trade_asked(self, player_id, name):
        """Somebody wants to trade. Ask, or say no for us.

        **Declined without a word when we are anywhere but the overworld.**
        A dialogue thrown over a battle or a shop is the co-op menu bug
        from s45 in a new costume: a screen the player did not open,
        landing on top of one they did.
        """
        if self.coop is None:
            return
        if self.trade is not None or self.state != OVERWORLD \
                or self.battle is not None or self.player is None:
            self.coop.answer_trade(self, False)
            return
        self.trade = trade_mod.TradeSession(player_id, name,
                                            trade_mod.INVITED)
        self.publish_body()
        self.dialogue = {
            "title": "%s wants to trade" % self.trade.partner_name,
            "body": ["Gold, gear and potions - both sides agree before",
                     "anything changes hands, and either of you can",
                     "walk away up to the moment it settles."],
            "confirm": "Trade",
            "on_confirm": lambda: self.answer_trade(True),
            "on_cancel": lambda: self.answer_trade(False),
        }
        self.go(DIALOGUE)
        self.sfx("select")

    def answer_trade(self, accept):
        if self.trade is None or self.coop is None:
            self.go(OVERWORLD)
            return
        if not accept:
            self.trade = None
        self.coop.answer_trade(self, accept)
        if not accept:
            self.go(OVERWORLD)

    def trade_opened(self, player_id, name):
        """Both sides are in. This is the screen."""
        if self.coop is None or self.player is None:
            return
        self.trade = trade_mod.TradeSession(player_id, name, trade_mod.OPEN)
        self.publish_body()
        self.go(TRADE)
        self.sfx("select")

    def trade_partner_offer(self, wire):
        if self.trade is None:
            return
        self.trade.set_theirs(trade_mod.Offer.from_wire(wire))
        self.sfx("move")

    def trade_partner_lock(self, locked):
        if self.trade is None:
            return
        self.trade.theirs_locked = bool(locked)
        if locked:
            self.sfx("select")

    def trade_settled(self):
        """Both agreed. Move it, and say what moved."""
        session, self.trade = self.trade, None
        if session is None or self.player is None:
            return
        spare = trade_mod.apply_to(self.player, session.mine, session.theirs)
        self.go(OVERWORLD)
        got = session.theirs.lines()
        self.toast("Traded with %s." % session.partner_name, GOOD, 3.5)
        for label, colour in got[:4]:
            self.toast("Received %s" % label, colour, 4.0)
        self.publish_body()
        self.sfx("item")
        # Should be unreachable - `trade.room_for` refuses the lock that
        # would overfill a bag - so this is the honest fallback rather than
        # the plan: ask, never discard (s48).
        for item in spare:
            self.take_item(item, "trade")

    def trade_ended(self, why=""):
        session, self.trade = self.trade, None
        if session is None:
            return
        if self.state in (TRADE, DIALOGUE):
            # The invitation may still be the dialogue on screen, and `go`
            # does not clear one - it only stops drawing it. A dict left
            # behind here comes back the next time anything opens a
            # dialogue without setting its own.
            self.dialogue = None
            self.go(OVERWORLD)
        self.toast("Trade called off%s."
                   % (" - %s" % why if why else ""), DIM, 3.5)
        self.sfx("back")

    # -- the screen --------------------------------------------------------

    # Rows the trade screen offers, in order: your gold, your gear, your
    # potions, then the two buttons. One flat list because the mouse model
    # is one flat list (hard rule 2.9) - a second cursor would be a second
    # click handler.
    def trade_rows(self):
        session, player = self.trade, self.player
        if session is None or player is None:
            return []
        rows = [{"kind": "gold", "have": player.gold,
                 "offered": session.mine.gold}]
        offered = list(session.mine.items)
        for item in self.tradeable_items():
            rows.append({"kind": "item", "item": item,
                         "worn": player.equipment.get(item.slot) is item,
                         "offered": any(o is item for o in offered)})
        for key in CONSUMABLE_ORDER:
            have = player.consumable_count(key)
            if have:
                rows.append({"kind": "cons", "key": key, "have": have,
                             "offered": session.mine.cons.get(key, 0)})
        rows.append({"kind": "lock"})
        rows.append({"kind": "cancel"})
        return rows

    def tradeable_items(self):
        """Gear, worn first. Decision 10: *any* gear, not just spares."""
        player = self.player
        worn = [i for i in player.equipment.values() if i is not None]
        return worn + [i for i in player.inventory if i not in worn]

    def trade_gold_step(self):
        """How much one press of Left or Right is worth.

        Twenty presses to hand over everything, whatever "everything" is.
        Shift makes it one, because the difference between 400 and 397
        occasionally matters and a text box for it does not.
        """
        if self.player is None:
            return 1
        if pygame.key.get_mods() & pygame.KMOD_SHIFT:
            return 1
        return max(1, int(self.player.gold) // 20)

    def trade_offer_now(self):
        """Rebuild our offer from the rows and push it to the room."""
        session = self.trade
        if session is None or self.coop is None:
            return
        self.coop.send_trade_offer(self, session.mine.to_wire())

    def trade_change(self, mutate):
        """Apply a change to our side, then tell the room.

        Every edit funnels through here because every edit has the same two
        consequences - both locks drop, and the other end has to be told -
        and a path that forgets either is a trade somebody agreed to and did
        not get.
        """
        session = self.trade
        if session is None:
            return
        offer = trade_mod.Offer(session.mine.gold, list(session.mine.items),
                                dict(session.mine.cons))
        mutate(offer)
        if session.set_mine(offer):
            self.trade_offer_now()

    def trade_toggle_lock(self):
        session = self.trade
        if session is None or self.coop is None:
            return
        if session.mine_locked:
            session.mine_locked = False
            self.coop.send_trade_lock(self, False)
            self.sfx("back")
            return
        ok, why = trade_mod.room_for(self.player, session.mine, session.theirs)
        if not ok:
            session.note = why
            self.toast(why.capitalize() + ".", BAD, 3.0)
            self.sfx("error")
            return
        session.note = ""
        session.mine_locked = True
        self.coop.send_trade_lock(self, True)
        self.sfx("select")

    def close_trade(self, reason=""):
        session, self.trade = self.trade, None
        if session is None:
            # **A trade screen with no trade behind it is not a screen you
            # can be left on** (s96). Every key on it calls this, and this
            # returned before leaving - so if the session ever went first,
            # nothing would take you back. No known path does that today;
            # a dead end is not worth trusting that to.
            if self.state == TRADE:
                self.go(OVERWORLD)
            return
        if self.coop is not None:
            self.coop.end_trade(self, reason)
        if self.state in (TRADE, DIALOGUE):
            self.go(OVERWORLD)
        self.sfx("back")

    def event_trade(self, event):
        session = self.trade
        rows = self.trade_rows()
        if session is None or not rows:
            self.close_trade()
            return
        index = max(0, min(int(session.index), len(rows) - 1))
        session.index = index
        row = rows[index]
        if event.key in (pygame.K_w, pygame.K_UP):
            session.index = (index - 1) % len(rows)
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            session.index = (index + 1) % len(rows)
            self.sfx("move")
        elif event.key in (pygame.K_a, pygame.K_LEFT, pygame.K_d,
                           pygame.K_RIGHT):
            up = event.key in (pygame.K_d, pygame.K_RIGHT)
            self.trade_nudge(row, up)
        elif event.key in (pygame.K_RETURN, pygame.K_e):
            self.trade_press(row)
        elif event.key in (pygame.K_ESCAPE, pygame.K_x):
            self.close_trade()

    def trade_nudge(self, row, up):
        """Left and Right move a quantity. Everything else ignores them."""
        if row["kind"] == "gold":
            step = self.trade_gold_step() * (1 if up else -1)
            top = int(self.player.gold)

            def move(offer):
                offer.gold = max(0, min(top, offer.gold + step))
            self.trade_change(move)
            self.sfx("move")
        elif row["kind"] == "cons":
            key, top = row["key"], int(row["have"])
            step = 1 if up else -1

            def move(offer):
                count = max(0, min(top, offer.cons.get(key, 0) + step))
                if count:
                    offer.cons[key] = count
                else:
                    offer.cons.pop(key, None)
            self.trade_change(move)
            self.sfx("move")

    def trade_press(self, row):
        kind = row["kind"]
        if kind == "lock":
            self.trade_toggle_lock()
        elif kind == "cancel":
            self.close_trade()
        elif kind == "gold":
            top = int(self.player.gold)

            def flip(offer):
                offer.gold = 0 if offer.gold else top
            self.trade_change(flip)
            self.sfx("select")
        elif kind == "item":
            item = row["item"]

            def flip(offer):
                for existing in list(offer.items):
                    if existing is item:
                        offer.items.remove(existing)
                        return
                offer.items.append(item)
            self.trade_change(flip)
            self.sfx("select")
        elif kind == "cons":
            key, top = row["key"], int(row["have"])

            def flip(offer):
                if offer.cons.get(key):
                    offer.cons.pop(key, None)
                else:
                    offer.cons[key] = top
            self.trade_change(flip)
            self.sfx("select")

    def open_devmenu(self):
        """Reached from the confirmation, or from the bound key once found."""
        if not self.player:
            self.toast("Nothing to tinker with yet.", DIM, 2.5)
            return
        first_time = not self.meta.found_devmenu
        self.meta.found_devmenu = True
        if first_time:
            self.meta.save()
            self.player.stats_tracker["secrets"] = 1
            self.after_battle_checks()
            self.toast("You found something that was not meant for you.",
                       ACCENT, 6.0)
        # **Opening it is using it** (2026-09-01, his call). The feat for
        # finding the menu is granted immediately above, *before* this
        # line, which is the whole trick: a marked run cannot write to the
        # profile, so the order is what makes "opening marks the run" and
        # "finding it is an achievement" both true. s19 decision 14 was
        # answered the other way in 2026-08-24 because that ordering had
        # not been noticed.
        devmenu_mod.mark(self)
        self.log.event("devmenu_opened", first_time=first_time)
        self.ui.dev_index = 0
        # Opening it starts a fresh number rather than resuming whatever
        # was half-typed last time (0ah, s58).
        self.ui.dev_typing = False
        self.sfx("select")
        self.go(DEVMENU)

    def close_devmenu(self):
        self.state = (self.previous_state if self.previous_state != DEVMENU
                      else OVERWORLD)

    # --------------------------------------------------------------- coop ---

    def open_lobby(self):
        self.ui.lobby_index = 0
        self.ui.lobby_mode = ""
        self.ui.lobby_notice = ""
        self.go(LOBBY)

    def choose_save_then_host(self):
        """Host a room, picking which run to host first.

        Savvydev, 2026-08-24: *"when you click play together and create a lobby,
        before the lobby is created give the option to select an existing save
        to play on or create a new save."*

        **A room is a world, so hosting one is a decision about which world.**
        It used to be whatever happened to be loaded, which is how the
        playtest ended up unsure which save was in play (§40) - and if
        nothing was loaded it simply refused.
        """
        # **It always asks now** (2026-08-31, s45). There used to be a
        # shortcut here - *"already in a run: that is the world they
        # mean"* - and it was wrong for a reason that only shows up from
        # the outside: **the lobby is reachable only from the title**
        # (§40), so a run still being in memory when you get here does not
        # mean you are playing it. It means you left it and the object
        # stayed behind. Savvydev walked into exactly that:
        #
        # > "when going back to title from a save and then going to play
        # > together, you are not prompted with what save you want to
        # > load"
        #
        # Asking costs one keypress on a screen that already lists the
        # save you just left, at the top, with its playtime next to it.
        self.host_when_ready = True
        # **Which game, then which world** (0ag, s59). His: *"when ur
        # starting co-op u get to pick whether to do a regular run or do a
        # different gamemode"*. A wave room has no world to pick, so the
        # picker is what decides whether the slot list is shown at all.
        self.open_mode_select()

    def run_screen(self):
        """Which screen *is* this run, when a menu closes over it.

        **`OVERWORLD` was hard-wired here**, so a Last Stand host leaving
        the lobby was put on the island their run never walks - with the
        HUD, the minimap and a hero standing in a field they cannot
        fight in (item 0cb, found beside it). A night or a hoard would
        have been worse: the arena is still running underneath.
        """
        if gamemodes.is_roam(self.mode) and self.roam is not None:
            return ROAM
        if self.in_waves():
            return WAVES
        return OVERWORLD

    def _host_if_asked(self):
        """Consume the intent, once a run is actually live.

        **And go to the lobby, which is the only place the room code is
        written down.** Without this the room opened correctly and the
        player was dropped straight into the overworld with no way to read
        the code they had just created:

        > "when quitting the game entirely and loading back in you are
        > presented with the option to pick which save but you are not
        > presented with the room code"

        Enter leaves the lobby for the valley whenever they are ready, and
        Play together shows it again.
        """
        if not self.host_when_ready:
            return
        self.host_when_ready = False
        if self.player and self.world:
            self.start_hosting()
            if self.coop is not None:
                self.go(LOBBY)

    def start_hosting(self, reach_out=True, port=None):
        """Open a room. Needs a run, because a room is a world.

        `reach_out=False` keeps the room strictly on the LAN. Tests pass it,
        because no test may knock on somebody's router.
        """
        if not self.player or not self.world:
            self.toast("Start or load a game first, then host it.", DIM, 4.0)
            return
        try:
            self.coop = coop_mod.Session.host(
                self.player.name, seed=self.world.seed,
                reach_out=reach_out,
                **({} if port is None else {"port": port}))
        except Exception as exc:                          # noqa: BLE001
            self.toast("Could not open a room: %s" % exc, BAD, 5.0)
            self.log.error("host failed: %r", exc)
            return
        self.coop._on_fighter_lost = self.fighter_lost
        self.publish_body()
        self.ui.lobby_mode = "host"
        self.log.event("coop_host", code=self.coop.room_code)
        self.sfx("portal")

    def start_joining(self):
        self.code_buffer = ""
        self.ui.lobby_notice = ""
        self.go(JOIN_CODE)

    def try_join(self):
        """Join a room. **A run of your own is no longer required.**

        This reverses the refusal added earlier the same day, and the
        reversal is Savvydev's design rather than a second thought about the
        fix: *"If you're joining a save for the first time (not as a host),
        you start at level one, no existing hero or anything."* A hero
        belongs to a world (s19 item 5c), so there is nothing to bring and
        nothing to refuse for.

        **What that fix was actually for is still fixed.** Joining with no
        run used to reach `adopt_world` with `self.player` as None and die
        on `'NoneType' object has no attribute 'x'`; the refusal was one way
        to stop that and the level-one path is the other. Nothing here may
        assume a player exists.
        """
        # A name is needed for the handshake before a hero exists, and
        # the hero you make in there may well be called something else.
        # This is only what the roster shows until you have made one.
        name = self.player.name if self.player else "Newcomer"
        try:
            self.coop = coop_mod.Session.join(self.code_buffer, name)
        except Exception as exc:                          # noqa: BLE001
            self.ui.lobby_notice = str(exc)[:60]
            self.sfx("error")
            return False
        self.ui.lobby_mode = "join"
        self.log.event("coop_join")
        self.go(LOBBY)
        self.sfx("select")
        return True

    def leave_coop(self, resume_own_run=False):
        """Close the room, and put the hero back where it belongs.

        **A guest hero is kept, not carried out.** It belongs to that valley
        (s19 item 5c), so leaving writes it to the vault and takes it off
        the screen - carrying it into your own save set is exactly what the
        save slots are protected from.

        `resume_own_run` is for the callers that stay in the world - Leave
        room, and the room dying under you. "Back to title" is going
        somewhere itself and passes nothing.
        """
        was_guest = self.guest_world is not None
        # **Out of the house before anything is saved or swapped** (s56,
        # s96). This dropped the house and kept the room: a host was left
        # in a kitchen with no way out (`leave_house` needs the house), and
        # the next save wrote the room's seed over their slot - a
        # different island on reload, every warden standing. The step
        # outside is where a save made indoors puts you anyway.
        self.step_outdoors()
        if was_guest:
            self.save_guest_hero(silent=True)
        # **A guest's copy of a room's fight ends with the room** (s96).
        # The host ran it and nobody else can: left behind, a mirror
        # battle stopped wanderers finding you and sent a full bag's
        # answer back into a fight with nobody behind it, and a Long
        # Night's mirror asked the room that was gone for its next frame
        # and crashed. So did an invitation, frozen on the HUD.
        if self.battle is not None and self.battle.mirror:
            self.battle = None
        if self.roam is not None and self.roam.mirror:
            self.roam = None
        self.fight_offer = None
        # **A trade dies with the room, and moves nothing.** The screen is
        # modal, so without this the player is left on a table whose other
        # half no longer exists, holding an agreement nobody can settle.
        if self.trade is not None:
            self.trade = None
            if self.state == TRADE:
                self.state = OVERWORLD
        if self.coop:
            self.log.event("coop_end", role=self.coop.role)
            self.coop.close()
        self.coop = None
        self.ui.lobby_mode = ""
        if not was_guest:
            # You were the host, or never left the title: the run on screen
            # is your own and nothing needs restoring.
            self.pre_coop_slot = None
            return
        self.guest_world = None
        # **Nothing the valley had open comes home with you** (s96). A
        # dialogue left behind stopped field bosses asking and made a held
        # arrow repeat while you walked; a chapter card's `after_card`
        # would have played *their* warden's last words in your run.
        self.dialogue = None
        self.scene = None
        self.chapter_card = None
        self.after_card = None
        self.pending_epitaph = None
        self.pending_ending = False
        slot, self.pre_coop_slot = self.pre_coop_slot, None
        if resume_own_run and slot is not None and self.load_run(slot):
            return
        # Either nothing to come back to - you joined straight off the title
        # - or the slot would not load. **The guest hero does not stay on
        # screen either way**: it is not a run you own, and leaving it there
        # would let the next Host a room open a room onto somebody else's
        # valley. And **the title means no run** (§45), mode and all -
        # a Long Night's state left here went into the next slot you
        # loaded (s96).
        self.clear_run()
        self.go(TITLE)

    def adopt_world(self, seed):
        """Step into somebody else's valley, as whoever you are *there*.

        Joining a room means playing *their* run, so the world is rebuilt
        from their seed - one integer, because terrain regenerates rather
        than being stored (hard rule 4).

        **Your hero no longer comes with you** (s19 item 5c). A hero belongs
        to a valley: the one you left in this one is waiting, and if you
        have never been here there is nobody to wait, so you make one. That
        supersedes s18's *"your level, gear and bag come with you"* for
        guests, and it is why this returns without a world on the new-valley
        path - creation builds it.
        """
        seed = int(seed)
        # **A Long Night guest takes the same door as a Last Stand
        # one** (item 0bg). Both are modes with no slot and no valley, so
        # the reasoning below is identical - and routing them together
        # is what stops `GuestVault` keeping a hero for a mode that must
        # not have one.
        # **"Has no save slot", asked rather than listed** (§88).
        if not gamemodes.mode_for(getattr(self.coop, "mode", "story")).saves:
            # **A wave guest makes a hero for the run, never for the
            # valley** (0ag, s59). `GuestVault` keeps a hero *per world*
            # so it is waiting for you next time - which is exactly what a
            # mode with no save must not do. It would also let a hero
            # levelled in somebody's story room walk into a Last Stand.
            if self.player and self.world and self.guest_world is None:
                self.pre_coop_slot = self.slot
                self.save_run(silent=True, force=True)
            self.begin_wave_guest(seed)
            return False
        # **Write your own run back before the hero on screen is replaced.**
        # You can join from inside a run, and the swap is silent - without
        # this, whatever you had done since the last autosave is gone.
        if self.player and self.world and self.guest_world is None:
            self.pre_coop_slot = self.slot
            self.save_run(silent=True, force=True)
        self.log.event("coop_world", seed=seed)
        found = self.guests.recall(seed, lambda n: World(self.tile, n),
                                   self.tile)
        if found is None:
            if self.guests.last_error:
                self.toast(self.guests.last_error[:70], BAD, 5.0)
            self.player = self.world = None
            self.guest_world = None
            if self.veteran_import_offered(seed):
                self.offer_veteran_import(seed)
            else:
                self.begin_guest_creation(seed)
            return False
        self.player, self.world, self.quests = found
        self.guest_world = seed
        self.forget_the_mode()              # a valley is a story run (s96)
        self.house = None                   # and is entered outdoors (s56)
        # Not stored with a guest hero: playtime is a property of a run in a
        # slot, and there is no honest number to show for a valley you drop
        # in and out of.
        self.playtime = 0.0
        self.meta.tainted = bool(self.player.debug_used)
        self.enter_overworld(snap=True)
        if self.guests.last_warning:
            self.toast(self.guests.last_warning[:70], (250, 200, 96), 6.0)
        # **Say whose valley this is.** The map changing is correct and was
        # completely silent, which is why the playtest read it as a save
        # being replaced (s40).
        host = getattr(self.coop, "host_name", "") if self.coop else ""
        self.toast("Welcome back to %s valley."
                   % (("%s's" % host) if host else "their"), ACCENT, 6.0)
        return True

    def apply_pending_chests(self):
        """Catch the valley up on chests opened before we arrived.

        Chests are shared and the host arbitrates them (s20), so these are
        facts about the world rather than about any hero - they land the
        same way whether the hero was waiting here or was made a moment ago.
        """
        if not self.coop or not self.world:
            return
        for gx, gy in self.coop.pending_chests:
            self.mark_chest_opened(gx, gy)
        self.coop.pending_chests = []

    # How the opening card is timed, in seconds. The black holds, then
    # lifts to show the title already sitting underneath it, and the words
    # go a moment later - so the last thing to leave is the name.
    SPLASH_HOLD = 1.0
    SPLASH_LIFT = 0.6
    # **Measured from when the black *starts* fading.** Settled by
    # Savvydev on 2026-08-24 after a round of it being read both ways:
    # "the name starts fading .5 seconds after the black starts fading".
    #
    # So the two deliberately overlap for the back half of the black's
    # fade. It is a cross-fade, not a sequence: black 1.0-1.6, name
    # 1.5-2.1, and the name is still the last thing on screen.
    SPLASH_GAP = 0.5           # after the black starts lifting
    SPLASH_TOTAL = 2.3

    def update_splash(self, dt):
        self.splash_time += dt
        if self.splash_time >= self.SPLASH_TOTAL:
            self.go(TITLE)

    def skip_splash(self):
        """Any key, any click. Nobody should have to sit through it twice."""
        if self.state == SPLASH:
            self.go(TITLE)
            return True
        return False

    def splash_alphas(self):
        """(black, words) as 0-255, for the draw pass."""
        t = self.splash_time
        black = 255
        if t > self.SPLASH_HOLD:
            black = int(255 * clamp(
                1.0 - (t - self.SPLASH_HOLD) / self.SPLASH_LIFT, 0, 1))
        words = 255
        start = self.SPLASH_HOLD + self.SPLASH_GAP
        if t > start:
            words = int(255 * clamp(
                1.0 - (t - start) / self.SPLASH_LIFT, 0, 1))
        return black, words

    def update_coop(self, dt):
        """One frame of co-op, if there is one. Never blocks."""
        if self.coop is None:
            return
        alive = self.coop.update(self, dt)
        # An invitation counting down on a client, and the offer the host
        # is holding open. Both are short-lived and both must tick even
        # while a menu is up, which is why they live here rather than in
        # the overworld's own update.
        self.update_fight_offer(dt)
        self.update_pending_fight(dt)
        if self.coop is not None and self.coop.role == coop_mod.HOST:
            self.push_battle_state()
        if self.coop is not None and self.coop.adopt_seed:
            self.coop.adopt_seed = False
            # False means there is no world yet: character creation is on
            # screen and `new_guest_run` finishes the job, chests included.
            if self.adopt_world(self.coop.seed):
                # Only now do the chests exist to be marked. The seed
                # rebuilds the terrain; this is what has since happened.
                self.apply_pending_chests()
                # Say what we are made of straight away, so that walking
                # into a monster later costs no round trip.
                self.publish_body()
                self.toast("You step into their valley.", ACCENT, 4.0)
        if self.coop.status and self.state == OVERWORLD:
            note, self.coop.status = self.coop.status, ""
            self.toast(note, ACCENT, 3.0)
        if not alive:
            message = self.coop.error or "The room closed."
            if self.at_the_door():
                # **A room that closes does not cut an ending short**
                # (s96). The host reaching the title first would
                # otherwise swap a guest's world out from under the
                # ending they were reading. Only the socket goes; the
                # hero stays a guest (`guest_world`), so the ending's own
                # save still goes to the vault, and closing the records
                # finishes leaving the way it always does.
                self.log.event("coop_end", role=self.coop.role)
                self.coop.close()
                self.coop = None
                self.ui.lobby_mode = ""
            else:
                self.leave_coop(resume_own_run=True)
            self.toast(message, BAD, 5.0)

    def event_lobby(self, event):
        if self.coop is not None:
            if event.key == pygame.K_ESCAPE:
                self.leave_coop()
                self.sfx("back")
            elif event.key in (pygame.K_RETURN, pygame.K_e) \
                    and self.coop.role == coop_mod.HOST:
                self.go(self.run_screen())
                self.sfx("select")
            return
        rows = menus.LOBBY_ROWS
        if event.key in (pygame.K_w, pygame.K_UP):
            self.ui.lobby_index = (self.ui.lobby_index - 1) % len(rows)
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            self.ui.lobby_index = (self.ui.lobby_index + 1) % len(rows)
            self.sfx("move")
        elif event.key == pygame.K_ESCAPE:
            self.go(PAUSE if self.player else TITLE)
            self.sfx("back")
        elif event.key in (pygame.K_RETURN, pygame.K_e):
            choice = rows[self.ui.lobby_index]
            if choice == "Host a room":
                self.choose_save_then_host()
            elif choice == "Join with a code":
                self.start_joining()
            else:
                self.go(PAUSE if self.player else TITLE)
                self.sfx("back")

    def event_join_code(self, event):
        if event.key == pygame.K_ESCAPE:
            self.go(LOBBY)
            self.sfx("back")
            return
        if event.key == pygame.K_BACKSPACE:
            self.code_buffer = self.code_buffer[:-1]
            self.ui.lobby_notice = ""
            return
        if event.key in (pygame.K_RETURN, pygame.K_e) \
                and len(self.code_buffer) == 10:
            self.try_join()
            return
        char = (event.unicode or "").upper()
        if char and char in net_alphabet() and len(self.code_buffer) < 10:
            self.code_buffer += char
            self.ui.lobby_notice = ""

    # ------------------------------------------------------------- scenes ---

    def play_scene(self, key, after=None):
        """Put a story moment on screen and wait for an answer.

        `after` runs once the reply has been read, so callers can chain a
        scene into whatever came next - a battle, an ending, the overworld.
        """
        scene = scenes_mod.SCENES_BY_KEY.get(key)
        options = scene.offered(self.meta.secrets) if scene else []
        if not options:
            if after:
                after()
            return False
        self.scene = {"scene": scene, "options": options, "index": 0,
                      "phase": "choosing", "reply": (), "after": after,
                      "scroll": 0}
        self.log.event("scene", key=scene.key, offered=len(options))
        self.go(SCENE)
        return True

    # What a replayed scene says when you pick an answer you never gave.
    # **It does not show the reply.** The reply is the reward for having
    # said the thing (s33), and handing it over here would turn the codex
    # into a way to read the whole story without playing it - which is
    # exactly the replay spine this screen exists to serve.
    UNSAID = ("You did not say this.",
              "",
              "Whatever it would have got back is still out there,",
              "in a run you have not had yet.")

    def replay_scene(self, key):
        """Watch a scene again, exactly as it played (s19 item 12).

        Savvydev, on the codex: *"it like allows you to replay/watch the
        entire story at any time yk?"* The story screen was already a
        readable record of it; this is the record **played back**, on the
        same screen the scene itself uses.

        **One presentation, not two** - hard rule 9's principle. The only
        differences are that nothing is written down and nothing is
        weighed, which is what `replay` means everywhere it is checked.
        """
        scene = scenes_mod.SCENES_BY_KEY.get(key)
        if scene is None:
            return False
        seen = set(self.meta.scene_seen.get(key, ()))
        # Only what you have actually been offered. An answer that has
        # never come up does not exist yet, and listing it here would leak
        # the shape of a run you have not had.
        options = [c for c in scene.choices if c.key in seen]
        if not options:
            return False
        self.scene = {
            "scene": scene, "options": options, "index": 0,
            "phase": "choosing", "reply": (), "replay": True, "scroll": 0,
            "taken": set(self.meta.scene_taken.get(key, ())),
            "after": lambda: self.go(STORY),
        }
        self.go(SCENE)
        self.sfx("select")
        return True

    def answer_scene(self, index):
        """Take an answer: say it back, weigh it, and write it down."""
        state = self.scene
        if not state or state["phase"] != "choosing":
            return
        options = state["options"]
        option = options[min(index, len(options) - 1)]
        scene = state["scene"]
        # Every one of the three paths below switches to the reply, and a
        # reply that opened halfway down because the question was long is
        # a reply with its first line missing.
        state["scroll"] = 0

        if state.get("favour"):
            self.settle_favour(index)
            state["phase"] = "reply"
            state["reply"] = option.reply
            self.sfx("select")
            return

        if state.get("wayside"):
            self.settle_wayside(index)
            state["phase"] = "reply"
            state["reply"] = option.reply
            self.sfx("select")
            return

        if state.get("replay"):
            # **A memory, not a choice.** No lean, no record, no secret
            # learned, no save - replaying a scene must not be a second way
            # to earn what playing it earns.
            state["phase"] = "reply"
            state["reply"] = (option.reply if option.key in state["taken"]
                              else self.UNSAID)
            self.sfx("select")
            return

        if option.lean and self.player:
            self.player.leanings[option.lean] = (
                self.player.leanings.get(option.lean, 0) + option.weight)
        self.meta.record_scene(scene.key, [o.key for o in options],
                               option.key)
        for flag in self.meta.learn_secret(option.grants):
            if flag == scenes_mod.ALL_HEARD:
                # Deliberately vague. Naming what it opened would undo the
                # point of having hidden it.
                self.toast("You have listened to all six of them.",
                           ACCENT, 6.0)
        self.meta.save()
        self.log.event("scene_answer", key=scene.key, took=option.key)

        if scene.key == "door":
            self.ui.door_choice = option.key
        state["phase"] = "reply"
        state["reply"] = option.reply
        self.sfx("select")

    def finish_scene(self):
        state, self.scene = self.scene, None
        # A replay can be watched from the title, where there is no world to
        # go back to. Its `after` sends it to the story record; setting the
        # overworld first would draw one frame of nothing on the way.
        if not (state and state.get("replay")):
            self.state = OVERWORLD
        if state and state.get("after"):
            state["after"]()

    def scroll_scene(self, step):
        """Read on, or back, when the prose is taller than the window.

        **Silently returns False when everything fits**, so the caller can
        fall through to whatever that key means otherwise. On any ordinary
        window that is every press: the whole feature is invisible until a
        short one makes it necessary.
        """
        state = self.scene
        if not state:
            return False
        limit = menus.scene_scroll_max(self)
        if limit <= 0:
            return False
        before = int(state.get("scroll", 0))
        state["scroll"] = clamp(before + step, 0, limit)
        if state["scroll"] != before:
            self.sfx("move")
        return True

    def event_scene(self, event):
        state = self.scene
        if not state:
            self.state = OVERWORLD
            return
        if event.key in (pygame.K_LEFT, pygame.K_a) and self.scroll_scene(-1):
            return
        if event.key in (pygame.K_RIGHT, pygame.K_d) and self.scroll_scene(1):
            return
        if state["phase"] == "reply":
            if event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE,
                             pygame.K_ESCAPE):
                if state.get("replay"):
                    # Back to the answers, so a scene can be read through
                    # rather than restarted for every line of it.
                    state["phase"] = "choosing"
                    state["reply"] = ()
                    state["scroll"] = 0
                    return
                self.finish_scene()
            return
        if event.key == pygame.K_ESCAPE and state.get("replay"):
            self.finish_scene()
            return
        count = len(state["options"])
        if event.key in (pygame.K_w, pygame.K_UP):
            state["index"] = (state["index"] - 1) % count
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            state["index"] = (state["index"] + 1) % count
            self.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE):
            self.answer_scene(state["index"])
        # Esc is deliberately not an exit: the scene is asking you something.

    def open_story_record(self):
        """The codex's replay of the road, if any of it has happened yet."""
        if not self.meta.scenes_discovered():
            self.toast("There is nothing to look back on yet.", DIM, 3.0)
            return
        self.ui.story.index = self.ui.story.offset = 0
        self.go(STORY)
        self.sfx("select")

    def event_story(self, event):
        known = [s for s in scenes_mod.SCENES if self.meta.saw_scene(s.key)]
        state = self.ui.story
        if event.key in (pygame.K_w, pygame.K_UP):
            state.move(-1, len(known))
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            state.move(1, len(known))
            self.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e):
            # **Enter used to mean "close"**, because a read-only list had
            # nothing to confirm (s35). It has something now.
            if known and not self.replay_scene(known[state.index].key):
                self.toast("Nothing of that one is written down yet.",
                           DIM, 3.0)
        elif event.key == pygame.K_ESCAPE:
            self.go(CODEX)
            self.sfx("back")

    def party_size(self):
        """How many heroes this fight is being sized for.

        Once battles became shared (section 21) this stopped being a guess
        about the room and became a count of who is actually in the fight -
        the host works that out from who was standing nearby, and passes it
        to `make_encounter` directly. What is left here is the solo path and
        the fights that stay private, both of which are one.
        """
        return 1

    def start_battle(self, enemy, party=None, viewer=0, mirror=False,
                     sort_party=True):
        """Put a fight on screen.

        `party` is the whole side, in the order the fight will run in; the
        default of None means this player alone, which is every solo caller
        and is why none of them had to change.
        """
        self.particles.clear()
        self.floats.items.clear()
        if self.open_combat() and not mirror:
            # **Item 0bi.** His words: *"free-roam is available in all
            # modes, and free-roam combat as well"* - so the real-time
            # engine is a modifier rather than a mode, and every fight in
            # a run that took it happens in it. `start_battle` is the one
            # door every fight in the game comes through, which is why
            # the branch is here and not in each caller.
            return self.start_open_fight(enemy, party=party, viewer=viewer,
                                         sort_party=sort_party)
        self.battle = Battle(party if party is not None else self.player,
                             enemy, self.settings.mods,
                             on_event=self.on_battle_event, viewer=viewer,
                             mirror=mirror, sort_party=sort_party)
        self.enemy_hp_tween.set(1.0, snap=True)
        # **Which of this place's four skylines this fight gets** (item
        # 0bd). A counter rather than a roll: it must not touch the
        # shared RNG stream (§21), it has to be the same for the whole
        # fight, and it has to differ between fights - which is the case
        # he actually described, *"if ur in one area for a long time"*.
        self.fights_seen += 1
        self.battle.arena_variant = self.fights_seen
        self.state = BATTLE
        self.ui.reset()
        group = enemy if isinstance(enemy, (list, tuple)) else [enemy]
        self.log.event("battle_start", count=len(group),
                       level=max(e.level for e in group),
                       boss=any(isinstance(e, Boss) for e in group),
                       hp="%d/%d" % (self.player.hp, self.player.max_hp()))
        self.sfx("boss" if any(isinstance(e, Boss) for e in group)
                 else "select")

    def open_combat(self):
        """Whether this run fights in real time (item 0bi).

        **The Long Night always does**, whatever modifiers were taken -
        the mode *is* free-roam combat. Everywhere else it is the
        `openfight` modifier, which cannot be taken without Plain Sight
        (`modifiers.REQUIRES`): a real-time fight you are dropped into by
        an invisible random encounter is the one arrangement that makes
        no sense, because there is nothing to have walked into.
        """
        if gamemodes.is_roam(self.mode):
            return True
        player = self.player
        return bool(player is not None
                    and player.modifiers.has("free_combat"))

    def start_open_fight(self, enemy, party=None, viewer=0, sort_party=True):
        """An island fight, in real time (item 0bi).

        Everything around it is unchanged: the pack was rolled the
        ordinary way, the payout is `finish_battle`'s, and walking back
        out lands on the overworld. **Only the middle is different.**
        """
        group = list(enemy) if isinstance(enemy, (list, tuple)) else [enemy]
        side = party if party is not None else [self.player]
        self.roam = freeroam.FreeRoam(
            self.player, group, on_event=self.on_roam_event,
            mods=self.settings.mods, party=side, viewer=viewer,
            until_clear=True)
        self.roam_order = []
        self.dress_roam_arena()
        self.battle = None
        self.state = ROAM
        self.log.event("battle_start", count=len(group),
                       level=max(e.level for e in group),
                       boss=any(isinstance(e, Boss) for e in group),
                       hp="%d/%d" % (self.player.hp, self.player.max_hp()))
        self.sfx("boss" if any(isinstance(e, Boss) for e in group)
                 else "select")
        return True

    def finish_open_fight(self):
        """One outcome, through the payout every fight already uses.

        **Not a second payout path** (§54): `finish_battle` does the
        levelling, the loot, the quests and the achievements, and none of
        that is different because the fight was real time. `FreeRoam`
        presents `result`, `enemies`, `players` and `reward_for` for
        exactly this reason.
        """
        fight, self.roam = self.roam, None
        if fight is None:
            return
        self.battle = fight
        self.finish_battle()
        self.battle = None

    def on_battle_event(self, kind, data):
        """Bridge from battle logic to presentation: sound, shake, text, dust."""
        # The host is the only machine where battle logic runs, so it is the
        # only one that can produce these. Relaying them is what lets a
        # mirror show a crit rather than just a smaller number.
        if self.coop is not None and self.coop.role == coop_mod.HOST \
                and self.coop.fight is not None:
            self.coop.push_fx(kind, data)
        self.replay_battle_event(kind, data)

    def replay_battle_event(self, kind, data):
        """Turn one battle event into sound, shake, text and dust."""
        if kind == "sfx":
            self.sfx(data["name"], throttle=data.get("throttle", 0.0))
        elif kind == "shake":
            if self.settings.screen_shake:
                self.camera.add_shake(data.get("amount", 5))
        elif kind == "float":
            if not self.settings.damage_numbers:
                return
            x, y = self.battle_anchor(data, lift=0.04)
            self.floats.add(x, y, data["text"], data["color"],
                            pop=data.get("pop", False))
        elif kind == "motion":
            # Only a mirror needs telling: the host already started the move
            # when it played it, and re-playing would restart the clock.
            if self.battle is not None and self.battle.mirror:
                self.battle.set_motion(data.get("who"),
                                       int(data.get("index") or 0),
                                       str(data.get("move") or ""))
        elif kind == "shot":
            # **Item 0al.** `battle.py` said what was fired and at whom;
            # this is the half that knows where those two things are on
            # screen. Both ends are asked for at the moment of firing, so
            # a body that moves mid-flight does not drag its own arrow.
            from .items import ELEMENT_COLOR
            start = self.battle_anchor({"who": data.get("who"),
                                        "index": data.get("index")},
                                       lift=0.0)
            end = self.battle_anchor({"who": data.get("at"),
                                      "index": data.get("at_index")},
                                     lift=0.0)
            colour = ELEMENT_COLOR.get(data.get("element"), (236, 226, 200))
            what = str(data.get("what") or "bolt")
            # **A beam does not arc and a thrown flask does** (0ba).
            # The arc is a property of the shape rather than of the
            # word "bolt", which is what it was keyed on when `bolt` was
            # the only spell shape there was.
            arc = {"thrown": 26.0, "arrow": 26.0, "orb": 16.0,
                   "shard": 8.0, "mote": 20.0, "wave": 6.0,
                   "beam": 0.0, "spark": 0.0}.get(what, 10.0)
            self.shots.add(start, end, colour, look=what,
                           life=float(data.get("flight") or 0.24),
                           arc=arc, scale=float(data.get("scale") or 1.0))
        elif kind == "particles":
            from .items import ELEMENT_COLOR
            x, y = self.battle_anchor(data, lift=0.06)
            self.particles.burst(x, y, data.get("count", 10),
                                 ELEMENT_COLOR.get(data.get("element"),
                                                   (250, 220, 180)),
                                 speed=210, life=0.5)

    def battle_anchor(self, data, lift=0.04):
        """Where a battle event should appear on screen.

        Events name a side and, when it matters, which member of it. A number
        with no index is a number over the wrong body as soon as either side
        has more than one, which is why `combatant_anchor` exists rather than
        two constants.

        A number sits below a lone hero, which is where it was pinned before
        there was more than one - but under a *party* is where the strip and
        the plate live, so a crowd wears its numbers above its head the way
        the monsters always have.
        """
        w, h = self.screen.get_size()
        if self.battle is None:
            return w * 0.5, h * 0.5
        who = data.get("who")
        x, y = combatant_anchor(self.battle, who, data.get("index"), (w, h))
        if who == "player" and len(self.battle.players) == 1:
            return x, y + h * lift
        return x, y - h * (lift + 0.02)

    # ------------------------------------------------------- shared battles ---

    def in_shared_battle(self):
        return self.coop is not None and self.coop.fight is not None \
            and self.battle is not None

    def battle_slot(self, player_id):
        """Where a given player sits in the running fight, or -1."""
        party = (self.coop.fight or {}).get("party", []) if self.coop else []
        return party.index(player_id) if player_id in party else -1

    def publish_body(self):
        """Tell the room what this character is made of, if there is a room."""
        if self.coop is not None and self.player is not None:
            self.coop.publish_body(serialize_player(self.player))

    def start_lone_encounter(self, level, biome):
        """A fight the host declined to share, because nobody was near.

        Resolved locally, exactly as it would be outside a room. There is no
        authority to defer to for a fight with one participant in it, and
        pretending otherwise would be ceremony.
        """
        if self.battle is not None or self.player is None:
            return
        self.start_battle(make_encounter(level, biome, self.settings.mods,
                                         party=1))

    def host_open_fight(self, level, biome, x, y, trigger=None):
        """Host side: ask who wants in, then roll it and announce it.

        Everything a client could have lied about is decided here - the
        seed, the monsters, and who was near enough to be asked.

        The encounter is generated from a seed rather than sent, which is
        the same trick that makes a world one integer (hard rule 4). It is
        rolled through `rng.forked` so that agreeing with another machine
        on a monster does not leave the rest of the session running off a
        number that came over a socket.

        **Nobody is put in a fight they did not agree to** (2026-09-01,
        s45). Savvydev, twice: *"i keep getting dragged into battle with my
        friend without even being near him"*, and then *"dragging in
        shouldnt happen anyway though"*. Standing close now earns you the
        **question**; `E` is the answer, and ignoring it is also an answer.

        Two rules make the rest of it fall out:

        * **The trigger is always in.** They walked into the monster. They
          are not asked and cannot decline.
        * **Everybody else near enough is asked, the host included.** The
          host is a player standing in the world like any other - and it
          being the machine that holds the offer is an implementation
          detail, not a reason to conscript it. That is the same asymmetry
          §40 found from the other side.

        Called twice per fight: once to make the offer (returns True with
        no battle), and again from `update_pending_fight` when the window
        closes, which is the call that opens it.
        """
        if self.coop is None or self.coop.role != coop_mod.HOST \
                or self.coop.fight is not None or self.battle is not None:
            return False

        bodies = self.coop.bodies()
        host_id = self.coop.player_id
        pending = self.coop.pending

        if pending is None:
            gx, gy = self.player_tile()
            host_near = (abs(gx - x) <= coop_mod.FIGHT_REACH
                         and abs(gy - y) <= coop_mod.FIGHT_REACH)
            invited = [pid for pid in self.coop.near(x, y, self.tile)
                       if pid != trigger and bodies.get(pid) is not None]
            if host_near and trigger not in (None, host_id):
                invited.append(host_id)
            if invited:
                info = {"level": int(level), "biome": str(biome),
                        "by": self.name_of(trigger), "trigger": trigger,
                        "x": float(x), "y": float(y)}
                self.coop.offer_fight(
                    info, [p for p in invited if p != host_id])
                if host_id in invited:
                    # The host has no socket to itself, so its own
                    # invitation is delivered by hand.
                    self.offered_a_fight(info["by"], int(level),
                                         self.coop.JOIN_WINDOW)
                    self.coop.pending["invited"].append(host_id)
                return True
            # Nobody to ask. A fight with one participant is a solo fight,
            # so if the host is not that participant it declines and
            # `_asked_for_a_fight` hands it back to the guest.
            if trigger not in (None, host_id) and not host_near:
                return False
            pending = {"accepted": [], "info": {"trigger": trigger}}

        coming = list(pending.get("accepted", ()))
        trigger = pending.get("info", {}).get("trigger", trigger)
        self.coop.pending = None
        if trigger is None:
            trigger = host_id
        order_ids = [trigger] + [p for p in coming if p != trigger]

        members = []
        for player_id in order_ids:
            if player_id == host_id:
                members.append((host_id, self.player))
                continue
            body = bodies.get(player_id)
            if body is None:
                # Somebody who has never said what they are made of cannot
                # be put in a fight. They keep walking; nothing breaks.
                continue
            members.append((player_id, deserialize_player(body)))
        if not members:
            return False
        if host_id not in [pid for pid, _ in members]:
            # The host is not in this one. There is no shared fight to
            # have - the guest fights it alone, on their own machine.
            return False

        # Fastest first, settled here once so that every mirror is handed
        # the order rather than deriving it.
        members.sort(key=lambda pair: -pair[1].stat("spd"))
        order = [player_id for player_id, _ in members]

        seed = rng.rint(1, 2 ** 31 - 2)
        with rng.forked(seed):
            enemies = make_encounter(level, biome, self.settings.mods,
                                     party=len(members))
        self.coop.open_fight({"seed": seed, "level": int(level),
                              "biome": str(biome), "boss": "",
                              "party": order})
        self.start_battle(enemies, party=[body for _, body in members],
                          viewer=order.index(host_id),
                          sort_party=False)
        return True

    def name_of(self, player_id):
        """Whoever that id belongs to, for a line of UI."""
        if self.coop is None or player_id in (None, self.coop.player_id):
            return self.player.name if self.player else "Someone"
        remote = self.coop.remotes.get(player_id)
        return getattr(remote, "name", "Someone") or "Someone"

    def begin_shared_battle(self, info, bodies):
        """Client side: build a mirror of the fight the host just started.

        The monsters come from the seed, the party comes from the order the
        host settled, and this player's own body is the real one - so a win
        lands on the character being played rather than on a copy of it.
        """
        if self.player is None or self.coop is None:
            return
        if str(info.get("mode") or "") == "hoard":
            # A hoard arrives through the same door a night does, and
            # for the same reason: `coop.fight` has to be set before any
            # frame can land (§86).
            self.begin_guest_hoard(info)
            return
        if str(info.get("mode") or "") == "zombies":
            # **A night, not a battle** (item 0bg). It arrives through
            # the same announcement so that `coop.fight` is set and the
            # frames have somewhere to land, and then it goes to the
            # other engine.
            self.begin_guest_night(info)
            return
        party = []
        for player_id in info["party"]:
            if player_id == self.coop.player_id:
                party.append(self.player)
                continue
            body = bodies.get(player_id)
            party.append(deserialize_player(body) if body
                         else Player("Wanderer", "warden"))
        boss = BOSS_BY_KEY.get(str(info.get("boss") or ""))
        with rng.forked(info["seed"]):
            if boss is not None:
                # **A warden the whole room is fighting** (decision 4,
                # s81). `boss` has ridden in this payload since §21 and
                # nothing read it, because a warden was solo - so a
                # mirror rolled an ordinary pack and the two machines
                # fought different things. It is the same rebuild-from-
                # the-seed the ordinary path does; only the roller
                # differs.
                enemies = make_boss_encounter(boss, self.settings.mods,
                                              party=len(party))
            else:
                # `count` is only ever set by a wave (0ag, s59); every
                # other fight leaves it at 0 and rolls its pack the
                # ordinary way.
                enemies = make_encounter(info["level"], info["biome"],
                                         self.settings.mods,
                                         party=len(party),
                                         count=info.get("count") or None)
        self.start_battle(enemies, party=party,
                          viewer=self.battle_slot(self.coop.player_id),
                          mirror=True, sort_party=False)
        # **Off the payload, not recomputed.** A guest that worked the
        # arena out from the wave number would sit in a different field
        # to the host the moment the two builds disagreed about the
        # table - and an unknown key is simply no wash, which is the
        # ordinary fight colour rather than an error (§20).
        self.dress_arena(gamemodes.ARENA_BY_KEY.get(
            str(info.get("arena") or "")))

    def shared_battle_frame(self, frame):
        """Client side: take the host's word for the state of the fight."""
        if self.battle is not None and self.battle.mirror:
            self.battle.apply_frame(frame)

    def shared_battle_fx(self, kind, data):
        """Client side: replay one of the host's presentation events."""
        if self.battle is None or not self.battle.mirror:
            return
        colour = data.get("color")
        if isinstance(colour, list) and len(colour) == 3:
            data = dict(data, color=tuple(int(clamp(c, 0, 255))
                                          for c in colour))
        elif "color" in data:
            data = dict(data, color=(250, 240, 210))
        self.replay_battle_event(kind, data)

    def host_apply_act(self, player_id, kind, key, target):
        """Host side: a client pressed something. Does it count?

        Two checks, and they are the whole of the authority: it has to be
        that player's turn, and the fight has to be waiting on a decision.
        Anything else is a client trying to move on somebody else's behalf.
        """
        battle = self.battle
        if battle is None or battle.mirror:
            return
        slot = self.battle_slot(player_id)
        if slot < 0 or slot != battle.acting_player \
                or battle.turn != "player":
            return
        if battle.phase not in (BATTLE_MENU, BATTLE_SUBMENU):
            return
        battle.targets[slot] = min(max(0, target), len(battle.enemies) - 1)
        battle._retarget()
        if kind == "attack":
            battle.player_attack()
        elif kind == "defend":
            battle.player_defend()
        elif kind == "flee":
            battle.player_flee()
        elif kind == "skill":
            for skill in battle.actor.skills():
                if skill.key == key:
                    battle.phase = BATTLE_SUBMENU
                    battle.use_skill(skill)
                    break
        elif kind == "item":
            if key in battle.usable_items():
                battle.use_item(key)

    def accepted_fighters(self):
        """Who is coming, out of the last offer. Empty when nobody was asked.

        Consumed here rather than in `coop`, because opening the fight is
        the game's job and holding the offer is the session's.
        """
        pending = getattr(self.coop, "pending", None) if self.coop else None
        if not pending:
            return []
        accepted = list(pending.get("accepted", ()))
        self.coop.pending = None
        return accepted

    def pending_fight_note(self):
        """Tell the trigger that the game is waiting on somebody."""
        pending = self.coop.pending if self.coop else None
        if not pending:
            return
        self.toast("Waiting for %d nearby..." % len(pending["invited"]),
                   DIM, 2.0)

    def update_pending_fight(self, dt):
        """Host side: run the invitation down, then start the fight.

        **It starts either way.** The window is a courtesy to the people
        who were asked, not a hold on the person who walked into the
        monster - so when it runs out the fight opens with whoever said
        yes, which is often nobody, which is a solo fight.
        """
        if self.coop is None or self.coop.role != coop_mod.HOST:
            return
        pending = self.coop.pending
        if not pending or self.battle is not None:
            return
        pending["left"] -= dt
        everyone_answered = (len(pending["accepted"])
                             >= len(pending["invited"]))
        if pending["left"] > 0 and not everyone_answered:
            return
        info = pending["info"]
        self.host_open_fight(info["level"], info["biome"], info["x"],
                             info["y"], trigger=info.get("trigger"))

    def offered_a_fight(self, host_name, level, seconds):
        """Client side: somebody near you started a fight. Coming?

        **A prompt, not a summons.** It expires on its own, and ignoring
        it is a complete answer.
        """
        self.fight_offer = {"by": host_name or "Someone", "level": level,
                            "left": float(seconds)}
        self.sfx("select")

    def accept_fight_offer(self):
        """Say yes to the invitation on screen, if there is one.

        **The host answers its own offer in memory**, because it has no
        socket to itself - the same hand-delivery `host_open_fight` uses to
        put the invitation there in the first place.
        """
        if not self.fight_offer or self.coop is None:
            return False
        if self.coop.role == coop_mod.HOST:
            pending = self.coop.pending
            if not pending:
                return False
            if self.coop.player_id not in pending["accepted"]:
                pending["accepted"].append(self.coop.player_id)
        elif not self.coop.accept_offer():
            return False
        self.fight_offer = None
        self.toast("On your way.", ACCENT, 1.6)
        return True

    def update_fight_offer(self, dt):
        if not self.fight_offer:
            return
        self.fight_offer["left"] -= dt
        if self.fight_offer["left"] <= 0:
            self.fight_offer = None

    def fighter_lost(self, player_id):
        """Host side: somebody in the fight dropped off the network.

        Treated as fleeing, because a turn-based fight that waits on a player
        who is not coming back is a fight that never ends.
        """
        battle = self.battle
        slot = self.battle_slot(player_id)
        if battle is None or battle.mirror or slot < 0:
            return
        battle.fled.add(slot)
        battle.say("%s is gone." % battle.players[slot].name)
        if not battle.standing():
            battle.flee_success()
        elif battle.turn == "player" and battle.acting_player == slot:
            battle.end_turn()

    def push_battle_state(self, force=False):
        """Host side: the authoritative frame.

        Sent **the moment the fight actually moves** - a new phase, a new
        turn, a result - and otherwise at a slow keep-alive rate. Waiting up
        to a twelfth of a second to be told it is your turn is a twelfth of a
        second of a menu that does not answer, and rolling frames out at a
        fixed rate regardless of whether anything happened is traffic spent
        on saying nothing.
        """
        if self.coop is None or self.coop.role != coop_mod.HOST:
            return
        if self.coop.fight is None or self.battle is None:
            return
        battle = self.battle
        shape = (battle.phase, battle.turn, battle.acting_player,
                 battle.acting, battle.round, battle.result,
                 len(battle.log), tuple(sorted(battle.fled)))
        if shape != self._battle_shape:
            self._battle_shape = shape
            force = True
        self.coop.push_frame(battle.snapshot(), force=force)

    def close_shared_battle(self, battle):
        """Host side: pay everybody out and tell them so.

        Loot is rolled here, once, and dealt to one participant per drop.
        Rolling it on each machine would mean four people each finding the
        same sword, which is the chest bug wearing different clothes.
        """
        if self.coop is None or self.coop.role != coop_mod.HOST:
            return None
        party = (self.coop.fight or {}).get("party", [])
        shares, drops = {}, {}
        earners = [i for i in range(len(battle.players)) if i not in battle.fled]
        if battle.result == "victory":
            for slot, player_id in enumerate(party):
                cut = battle.reward_for(battle.players[slot])
                shares[str(player_id)] = {"xp": cut["xp"], "gold": cut["gold"]}
            for enemy in battle.enemies:
                if not earners or not rng.chance(0.30):
                    continue
                item = self.roll_loot(max(1, enemy.level))
                winner = party[rng.choice(earners)]
                drops.setdefault(str(winner), []).append(item.to_dict())
        bodies = {}
        for slot, player_id in enumerate(party):
            member = battle.players[slot]
            bodies[str(player_id)] = {"hp": int(member.hp),
                                      "mp": int(member.mp)}
        payload = {"rs": battle.result or "fled", "sh": shares,
                   "lt": drops, "bd": bodies}
        self.coop.close_fight(payload)
        return payload

    def finish_shared_battle(self, payload, player_id):
        """Client side: the host says it is over. Apply our own outcome.

        Only this player's row is read. The others are in the payload so the
        host can send one message rather than four, not so that anybody can
        act on somebody else's numbers.
        """
        battle, self.battle = self.battle, None
        key = str(player_id)
        result = sec.safe_choice(payload.get("rs"),
                                 ("victory", "defeat", "fled"), "fled")
        body = sec.safe_dict(payload.get("bd")).get(key)
        if isinstance(body, dict):
            self.player.hp = sec.safe_int(body.get("hp"), 0,
                                          self.player.max_hp(),
                                          self.player.hp)
            self.player.mp = sec.safe_int(body.get("mp"), 0,
                                          self.player.max_mp(),
                                          self.player.mp)
        self.player.clear_statuses()

        # A guest in a wave room runs the same loop off the same wave
        # number (0ag, s59). The host decided the monsters and the split;
        # what a wave *pays* is `gamemodes.wave_rewards`, which both ends
        # compute rather than send - the same agreement-without-a-message
        # the encounter seed uses.
        if self.in_waves():
            if result == "victory":
                self.wave_cleared(list(battle.enemies) if battle else [])
            else:
                self.wave_lost()
            self.publish_body()
            return

        if result == "defeat" and self.player.hp <= 0:
            self.player.stats_tracker["deaths"] += 1
            # **Mortal: there is no waking up** (s54). The death is still
            # counted - the Get Up feat is for falling (s49) and a run that
            # ended is still a run that happened - and the gold penalty is
            # skipped, because taking 15% off a hero who will never spend
            # it again is a rule with nothing left to do.
            if self.player.modifiers.has("mortal"):
                self.player.fallen = True
                self.message = "Mortal. This run is over."
                self.state = GAMEOVER
                self.publish_body()
                return
            lost = min(self.player.gold, max(20, int(self.player.gold * 0.15)))
            self.player.gold -= lost
            self.player.hp = max(1, self.player.max_hp() // 2)
            self.player.mp = self.player.max_mp() // 2
            self.message = "You wake at the shrine, %d gold lighter." % lost
            self.state = GAMEOVER
            self.publish_body()
            return

        if result == "victory":
            cut = sec.safe_dict(sec.safe_dict(payload.get("sh")).get(key))
            xp = sec.safe_int(cut.get("xp"), 0, 10 ** 7, 0)
            gold = sec.safe_int(cut.get("gold"), 0, 10 ** 7, 0)
            enemies = list(battle.enemies) if battle else []
            for enemy in enemies:
                self.player.stats_tracker[
                    "bosses" if isinstance(enemy, Boss) else "kills"] += 1
            self.player.gold += gold
            self.player.stats_tracker["gold_earned"] += gold
            levels, new_skills = self.player.gain_xp(xp)
            self.toast("+%d XP   +%d gold" % (xp, gold), ACCENT, 3.0)
            if levels:
                self.sfx("levelup")
                self.toast("Level %d - %d points to spend (%s)."
                           % (self.player.level, self.player.points,
                              self.binds.label_for("hero")), GOOD, 5.0)
                for skill in new_skills:
                    # **The same question the solo path asks** (s53 item
                    # 0ac). This site was missed when that landed, so
                    # levelling in a shared fight still raised the toast
                    # nobody sees. Two payout paths, one behaviour.
                    self.learned_skill(skill)
            for entry in sec.safe_list(
                    sec.safe_dict(payload.get("lt")).get(key), 8):
                item = Item.from_dict(entry)
                if item is None:
                    continue
                self.take_item(item)
            # **A warden a room fells is felled for each of them** (s96).
            # This path never called it, so after an assembled fight
            # (§81) nobody's portal was marked, nobody got the Memory and
            # the next road stayed sealed - and in a room a warden cannot
            # be fought alone, so co-op never got past the first one.
            # Each machine records its own kill, as §20 says a warden
            # kill is recorded: per player, never shared.
            for enemy in enemies:
                if isinstance(enemy, Boss):
                    self.on_boss_defeated(enemy)
            self.after_battle_checks()
            self.after_the_fight()
            self.publish_body()
            return
        self.enter_overworld()
        self.publish_body()

    def finish_battle(self):
        if self.in_shared_battle() and self.coop.role == coop_mod.HOST:
            # The host takes exactly the path everybody else takes. Paying
            # itself locally and everybody else through a payload would be
            # two reward implementations, and one of them would drift.
            battle = self.battle
            self.log.event("battle_end", result=battle.result,
                           rounds=battle.round, count=len(battle.enemies),
                           shared=len(battle.players))
            payload = self.close_shared_battle(battle)
            self.finish_shared_battle(payload, self.coop.player_id)
            return
        battle, self.battle = self.battle, None
        enemies = list(battle.enemies)
        player = self.player
        self.log.event("battle_end", result=battle.result,
                       rounds=battle.round, count=len(enemies),
                       hp="%d/%d" % (player.hp, player.max_hp()))

        # **Wave mode ends a fight differently** (0ag, s59), and it is the
        # whole of what the mode changes about combat: the engine, the
        # screen and the rewards are the ordinary ones; only what happens
        # *after* the last monster falls is the mode's business.
        if self.in_waves():
            self.finish_wave_battle(battle, enemies)
            return

        if battle.result == "defeat":
            player.stats_tracker["deaths"] += 1
            # **Mortal, and this is the path that matters** (s54). There
            # are two defeat handlers - this one and the shared-battle one
            # in `finish_shared_battle` - and the first version of this
            # only patched the co-op half, which is the *rarer* of the two.
            # Both now, and a test drives this one.
            if player.modifiers.has("mortal"):
                player.fallen = True
                player.clear_statuses()
                self.message = "Mortal. This run is over."
                self.state = GAMEOVER
                return
            lost = min(player.gold, max(20, int(player.gold * 0.15)))
            player.gold -= lost
            player.hp = max(1, player.max_hp() // 2)
            player.mp = player.max_mp() // 2
            player.clear_statuses()
            self.message = "You wake at the shrine, %d gold lighter." % lost
            self.state = GAMEOVER
            return

        if battle.result == "victory":
            # The local player's cut, not the whole pot. Solo those are the
            # same number, because a party of one splits everything with
            # itself.
            self.grant_victory_rewards(enemies, battle.reward_for(player))
        self.after_the_fight()

    def after_the_fight(self):
        """What a won fight leaves on screen, in order: **the chapter card
        the kill turned, then the warden's last words, then the Door.**

        **Both payout paths end here** (s96), and neither did this right.
        The solo one called `enter_overworld` straight after
        `on_boss_defeated` had put the chapter card up, so the state was
        overwritten before the card was ever drawn - and `chapter_seen`
        had already moved, so it never came back. **No chapter after the
        first has ever shown its card after a warden.** The shared one
        never called `on_boss_defeated` at all (see
        `finish_shared_battle`), so it had nothing to show.

        **The finale is left exactly as it was**: when the kill opens the
        Door (`pending_ending`), chapter seven's card - *"Maren asked you
        to see her first"* - would say one thing and the Door do another.
        Which of those is right is 0ck, his.
        """
        self.enter_overworld()
        epitaph, self.pending_epitaph = self.pending_epitaph, None
        follow = self.open_ending_choice if self.pending_ending else None

        def last_words():
            if epitaph:
                self.play_scene(epitaph, after=follow)
            elif follow is not None:
                follow()

        if self.chapter_card is not None and follow is None:
            self.after_card = last_words
            self.chapter_time = 0.0
            self.go(CHAPTER_CARD)
            return
        self.chapter_card = None
        last_words()

    def grant_victory_rewards(self, enemies, rewards=None):
        """Pay this player their share of a won fight.

        XP and gold are announced once, however many fell, because five toasts
        stacked up after a five-monster fight buries everything else. Loot
        still rolls per enemy: killing more should mean finding more.

        `rewards` is **one player's cut**, already split by
        `Battle.split_rewards`. It defaults to the whole pot so that callers
        outside a battle - the debug menu, tests - keep working.
        """
        if isinstance(enemies, (Boss, Enemy)):
            enemies = [enemies]          # callers that pass a single monster
        player = self.player
        gold = (rewards or {}).get(
            "gold", sum(e.gold_reward for e in enemies))
        xp = (rewards or {}).get("xp", sum(e.xp_reward for e in enemies))

        player.gold += gold
        player.stats_tracker["gold_earned"] += gold
        for enemy in enemies:
            player.stats_tracker[
                "bosses" if isinstance(enemy, Boss) else "kills"] += 1
        levels, new_skills = player.gain_xp(xp)
        self.toast("+%d XP   +%d gold" % (xp, gold), ACCENT, 3.0)
        self.publish_body()

        if levels:
            self.sfx("levelup")
            self.toast("Level %d - %d points to spend (%s)."
                       % (player.level, player.points,
                          self.binds.label_for("hero")), GOOD, 5.0)
            for skill in new_skills:
                self.learned_skill(skill)

        found = 0
        for enemy in enemies:
            is_boss = isinstance(enemy, Boss)
            if rng.chance(0.85 if is_boss else 0.30):
                item = self.roll_loot(
                    max(1, enemy.level + (3 if is_boss else 0)),
                    luck=0.5 if is_boss else 0.0)
                if player.add_item(item):
                    found += 1
                    if found <= 2:
                        self.toast("Found %s" % item.name, item.color, 4.5)
                    self.sfx("item")
                else:
                    # **Asked, not discarded** (s48). The prompt opens over
                    # the victory screen and the rest of the payout has
                    # already been handed out, so answering it is the last
                    # thing that happens rather than an interruption.
                    self.take_item(item, source="the fight")
            if rng.chance(0.35 if is_boss else 0.12):
                player.add_consumable("potion")
                self.toast("Found a Potion", (226, 96, 106), 3.0)
        if found > 2:
            self.toast("...and %d more" % (found - 2), ACCENT, 3.5)

        for enemy in enemies:
            if isinstance(enemy, Boss):
                self.on_boss_defeated(enemy)
        self.after_battle_checks()

    def on_field_boss_defeated(self, boss):
        """A field boss falls: no Memory, no codex page, no portal.

        **That absence is the design** (s42). Six anchors is a fixed point
        and `THE SEVENTH` counts them, so a mini boss that handed over a
        Memory would quietly become a seventh warden. What it gives is what
        the fight already gave - gold, experience and whatever it dropped.
        """
        spot, self.pending_lair = self.pending_lair, None
        if spot is not None:
            self.world.fell_lair(spot[0], spot[1], self.playtime)
        self.log.event("field_boss_down", who=boss.definition.name)
        self.toast("%s falls." % boss.definition.name, GOOD, 5.0)
        self.sfx("unlock")

    def on_boss_defeated(self, boss):
        key = boss.definition.key
        # **A warden in a Last Stand is a statblock** (item 0as). His
        # words: *"the wave gamemode has no connection to the story or
        # anything but yoou can end up fighting wardens as bosses"* - and
        # everything below this line is story. A wave run builds a world
        # it never walks (`new_wave_run` says why), so without this the
        # wave-10 Sprout King would mark that throwaway island's portal
        # defeated, toast *"Stone Golem stirs in the distance"*, hand
        # over a Memory, unlock two codex pages and queue an epitaph
        # scene - in a mode that has no story to put any of it in.
        if self.in_waves():
            self.sfx("unlock")
            return
        # A field boss is not a warden and must not walk the warden path -
        # no portal to open, no Memory to hand over, no chapter to turn.
        from .entities import FIELD_BOSS_BY_KEY, GUARDIAN_BY_KEY
        if key in GUARDIAN_BY_KEY:
            spot, self.pending_gate = self.pending_gate, None
            if spot is not None:
                self.open_stepstone(spot)
            self.sfx("unlock")
            return
        if key in FIELD_BOSS_BY_KEY:
            self.on_field_boss_defeated(boss)
            return
        for i, portal in enumerate(self.world.portals):
            if portal.boss_key != key:
                continue
            portal.defeated = True
            self.world.props.pop((portal.gx, portal.gy), None)
            if i + 1 < len(self.world.portals):
                nxt = self.world.portals[i + 1]
                nxt.unlocked = True
                self.toast("%s stirs in the distance."
                           % BOSS_BY_KEY[nxt.boss_key].name, ACCENT, 6.0)
            break
        self.sfx("unlock")
        # **After the portal is marked, before the Memory is handed over.**
        # The count this reads is the one that just changed, and the card
        # wants to be the first thing that acknowledges it.
        self.refresh_chapter()

        lore = story.WARDEN_BY_BOSS.get(key)
        if lore:
            relic = self.player.add_relic(lore.memory_key)
            if relic:
                self.toast("Memory taken: %s" % relic.name, relic.color, 6.0)
            self.unlock_codex(lore.memory_key)
            if len(relics_mod.memories_held(self.player.relic_objects())) >= 2:
                self.unlock_codex("anchor")
            # Their last words land after the victory banner, not over it.
            # The scene asks something; the epitaph is only the opening of it.
            self.pending_epitaph = scenes_mod.WARDEN_SCENE_BY_BOSS.get(
                boss.definition.key)
        if self.world.wardens_defeated() >= len(story.WARDENS):
            self.unlock_codex("maren")
            self.unlock_codex("cycle")
            self.pending_ending = True
        self.save_run(silent=True, force=True)

    def after_battle_checks(self):
        fresh = self.quests.check_achievements(self.player)
        for ach in fresh:
            # Earned in this run either way; whether it also reaches the
            # global cabinet depends on the run being unmarked.
            first_ever = self.meta.unlock_achievement(ach.key)
            self.toast("Achievement: %s%s" % (ach.title,
                                              "  (new!)" if first_ever else ""),
                       (250, 200, 96), 5.0)
            self.sfx("quest")
        if fresh:
            self.meta.save()
        for quest in self.quests.ready_to_claim(self.player):
            self.toast("Quest ready: %s" % quest.title, GOOD, 5.0)

    # -------------------------------------------------------- interactions ---

    def interact(self):
        # **An invitation is what E means while one is on screen** (s45):
        # *"using e to join my friend's battle doesnt work"*. It is on top
        # because it expires - a chest will still be there in four
        # seconds, and the fight will not.
        if self.fight_offer and self.accept_fight_offer():
            return
        gx, gy = self.player_tile()
        kind, target = self.world.interactable_near(gx, gy)
        if kind == "chest":
            self.open_chest(target)
        elif kind == "portal":
            self.enter_portal(target)
        elif kind == "door":
            self.use_door(target)
        elif kind == "shop":
            self.go(SHOP)
            self.ui.refresh_shop(self.player, self.world)
            self.sfx("select")
        elif kind == "npc":
            if target.kind == "smith":
                self.talk_to_smith()
            elif target.kind == "runner":
                self.talk_to_runner(target)
            elif target.kind == "keeper":
                self.talk_to_keeper(target)
            elif target.kind == "elder":
                self.talk_to_elder()
            else:
                self.talk_to_villager(target)
            self.sfx("select")
        elif kind == "lair":
            self.approach_lair(target)
        elif kind == "tower":
            self.climb_tower(target)
        elif kind == "stepstone":
            self.approach_stepstone(target)
        elif kind == "shrine":
            self.use_shrine()
        elif kind == "marker":
            self.read_marker(target)
        elif kind == "camp":
            self.stop_at_wayside(target)
        elif kind == "hoard":
            self.take_hoard(target)
        else:
            self.toast("Nothing to interact with here.", DIM, 1.2)

    def approach_stepstone(self, spot):
        """A stepstone: fight its guardian once, then travel to it forever.

        Savvydev, 2026-08-29: *"to be able to teleport to the shrine, you
        have to beat the challenge inside"*. So the network is not a
        reward for walking - it is a reward for **winning** somewhere, and
        an unopened gate is a fight you have not taken yet.
        """
        from .entities import guardian_for
        spot = tuple(spot)
        found = self.world.stepstone_at(*spot)
        if not found:
            return
        key, attuned = found
        stop = story.WAYSTATION_BY_KEY.get(key)
        name = stop.name if stop else "the stepstone"
        if attuned:
            self.toast("The way to %s is already open." % name, DIM, 3.0)
            return
        guardian = guardian_for(key)
        if guardian is None:
            # No guardian written for this one: opening it is the honest
            # fallback, not a locked gate nobody can ever use.
            self.open_stepstone(spot, quiet=False)
            return
        self.dialogue = {
            "title": guardian.name,
            "body": [guardian.title,
                     "",
                     guardian.taunt,
                     "",
                     "Beat it and the way to %s stays open." % name,
                     "Walk away and the gate stays shut. It will wait."],
            "confirm": "Take the trial",
            "on_confirm": lambda: self.fight_guardian(spot, guardian),
        }
        self.go(DIALOGUE)

    def fight_guardian(self, spot, guardian):
        self.pending_gate = spot
        self.log.event("guardian", key=guardian.key)
        self.start_battle(make_boss_encounter(guardian, self.settings.mods,
                                              party=1))

    def open_stepstone(self, spot, quiet=True):
        if not self.world.attune_stepstone(*spot):
            return
        key = self.world.stepstones[spot][0]
        stop = story.WAYSTATION_BY_KEY.get(key)
        open_count = len(self.world.open_stepstones())
        self.log.event("stepstone", key=key, open=open_count)
        self.toast("The way to %s is open.  %d gates"
                   % (stop.name if stop else "the waystation", open_count),
                   GOOD, 5.0)
        if not quiet:
            self.sfx("portal")

    def travel_to(self, spot):
        """Step through to an open stepstone.

        **Refused in a room.** The host arbitrates position and fights
        (s21); a guest teleporting across the map is a desync waiting to
        be found, and telling somebody why beats a rare one.
        """
        if self.coop is not None:
            self.toast("Not while you are in somebody's room.", BAD, 3.5)
            self.sfx("error")
            return False
        if self.world.stepstone_at(*spot) is None:
            return False
        if not self.world.stepstones[spot][1]:
            self.toast("That gate is still shut.", BAD, 3.0)
            self.sfx("error")
            return False
        self.player.x = spot[0] * self.tile + self.tile / 2
        self.player.y = spot[1] * self.tile + self.tile / 2
        self.enter_overworld(snap=True)
        self.last_tile = self.player_tile()
        key = self.world.stepstones[spot][0]
        stop = story.WAYSTATION_BY_KEY.get(key)
        self.toast("You step out at %s."
                   % (stop.name if stop else "the stepstone"), ACCENT, 4.0)
        self.sfx("portal")
        self.log.event("travel", key=key)
        return True

    def climb_tower(self, spot):
        """Climb a survey tower and light up that part of the map (s42).

        **It gates nothing.** You could already walk here and you can walk
        anywhere else; what changes is that the map remembers this country
        instead of showing you a hole. Savvydev: *"you can travel anywhere in
        the map without unlocking those big towers, but to actually see how
        the whole map looks you have to unlock all the towers"*.
        """
        spot = tuple(spot)
        if spot not in self.world.towers:
            return
        if not self.world.climb_tower(*spot):
            self.toast("You have already been up this one.", DIM, 2.5)
            return
        climbed = self.world.towers_climbed()
        total = len(self.world.towers)
        # Counted for the feat (s49). **From the world, not a running
        # tally**: the world already knows how many you have climbed, and
        # a second counter would be a second truth that can disagree.
        self.player.stats_tracker["towers"] = climbed
        self.log.event("tower", gx=spot[0], gy=spot[1], climbed=climbed)
        self._minimap_cache = None          # the map has changed shape
        self.toast("The land opens out below you.  %d of %d towers"
                   % (climbed, total), GOOD, 5.0)
        self.sfx("unlock")

    def approach_lair(self, target, from_wanderer=False):
        """A field boss you went looking for (s42).

        **It is asked, not sprung.** Walking into a monster is the game\'s
        business; walking into something four times your level should be
        yours, and the lair is a landmark you can see and decide about.

        **It comes back.** Felling one puts it on a timer rather than
        deleting it - Savvydev: *"field bosses arent just a one-time
        encounter"* - so the landmark stays a landmark and the map does not
        quietly empty as you clear it.
        """
        spot = tuple(target)
        found = self.world.lair_at(spot[0], spot[1], self.playtime)
        if not found:
            return
        boss, resting = found
        if resting:
            self.toast("Whatever lives here is not here just now.", DIM, 3.0)
            return
        # **Under Plain Sight the den is a den, not a doorbell.** Its
        # tenant is out walking somewhere you can see, so fighting it from
        # the empty lair would be two ways to start the same fight and one
        # of them would make the walking pointless.
        if (self.wanderers_on() and not from_wanderer
                and spot in self.walking_lairs()):
            self.toast("Out walking. Find it.", DIM, 3.0)
            return
        variant = self.world.lair_variant(spot)
        shown = make_field_boss(boss.key, variant,
                                player_level=self.player.level) or boss
        self.dialogue = {
            "title": shown.name,
            "body": [shown.title,
                     "",
                     shown.taunt,
                     "",
                     "Nothing about this is required of you.",
                     "It will still be here later if you would rather not."],
            "confirm": "Fight it",
            "on_confirm": lambda: self.fight_field_boss(spot, shown),
        }
        self.go(DIALOGUE)

    def fight_field_boss(self, spot, boss):
        self.pending_lair = spot
        self.log.event("field_boss", key=boss.key, who=boss.name,
                       how="lair")
        self.start_battle(make_boss_encounter(boss, self.settings.mods,
                                              party=1))

    def open_chest(self, chest):
        """Open a chest, or - in a room - ask the host whether we may.

        Solo and hosting are the same case: this machine is the authority,
        so it decides and rolls on the spot. A client asks instead, because
        a chest is a one-shot and two people looting the same one was the
        most obvious broken thing about co-op.
        """
        if chest is None or chest.opened:
            self.toast("Already looted.", DIM, 1.2)
            return
        # Role, not readiness: a client is never the authority on a chest,
        # and a condition that can fall through to the host path when a
        # session is half-open is one more state to get wrong later.
        if self.coop is not None and self.coop.role == coop_mod.CLIENT:
            self.coop.claim_chest(chest.gx, chest.gy, self.player.level)
            return
        self.take_chest_loot(chest.gx, chest.gy,
                             self.chest_loot(chest.tier, self.player.level))
        if self.coop is not None:
            self.coop.announce_chest(chest.gx, chest.gy)

    def chest_loot(self, tier, level):
        """Roll what a chest holds, as plain JSON-safe values.

        The host rolls for everyone, so this returns data rather than
        touching a player: a client that could name its own loot would be
        editing its save with extra steps.
        """
        item = self.roll_loot(max(1, int(level) + tier * 2 - 2),
                              luck=0.2 * tier)
        return {
            "gold": rng.rint(40, 90) * tier,
            "item": item.to_dict(),
            "cons": rng.choice(["potion", "ether", "antidote"])
                    if rng.chance(0.5) else "",
            "relic": rng.choice(relics_mod.COMMON_RELICS).key
                     if rng.chance(0.22) else "",
        }

    def mark_chest_opened(self, gx, gy):
        """This chest is empty now, whoever emptied it.

        State only - no sound and no sparks, because those belong to the
        person who actually reached in. The Hollow Key check lives here so
        that a room where your friend opened the last chest still hands
        everybody the key.
        """
        chest = self.world.chest_at(gx, gy)
        if chest is None or chest.opened:
            return False
        chest.opened = True
        self.world.props[(chest.gx, chest.gy)] = "chest_open"
        if self.world.all_chests_opened() and not self.world.hollow_key:
            self.world.hollow_key = True
            self.toast("The last chest held a key with no lock.",
                       (168, 108, 226), 8.0)
            self.sfx("portal")
        return True

    def take_hoard(self, spot):
        """Empty the stacked room (0af, s58).

        Savvydev, 2026-09-05: *"add a super rare house interior that is
        absolutely STACKED bro"*.

        **This is not a chest and must never become one.** The chest count
        is what `THE LONG WALK` costs and what the Hollow Key counts
        (s42), so a hoard that registered as a chest would either reprice
        an ending or be one the key could never find. Nothing here touches
        `world.chests`, `mark_chest_opened` or `all_chests_opened`.

        **And it is per player, not arbitrated by the host.** s20 makes
        the same call for warden kills and story flags: being handed
        somebody else's discovery removes the discovery. So there is no
        claim, no round trip and no race - two people in the same room
        (s57) each find it, and each empties their own.

        It is recorded against the *door*, on the save, which is the first
        thing about an interior that does not regenerate. Without that,
        stepping outside and back in is a farm.
        """
        if self.player is None or self.house is None:
            return
        door = self.house["door"]
        if tuple(door) in self.player.looted_hoards:
            self.toast("You have already emptied this one.", DIM, 2.0)
            return
        self.player.looted_hoards.add(tuple(door))
        self.world.props.pop(tuple(spot), None)

        # **Generous, and bounded by the same rolls everything else uses.**
        # A hoard is worth roughly a tier-three chest several times over,
        # which is what "stacked" has to mean to be worth the 1-in-18; it
        # is not a level-up in a box, because the level curve is his
        # (rule 31.7) and this must not quietly become the way to play.
        level = max(1, self.player.level)
        gold = rng.rint(320, 640) + level * 40
        materials = rng.rint(30, 70)
        self.player.gold += gold
        self.player.materials += materials
        kept, missed = [], 0
        for _ in range(rng.rint(3, 4)):
            item = self.roll_loot(level + rng.rint(2, 5), luck=0.75)
            if self.player.add_item(item):
                kept.append(item.name)
            else:
                missed += 1
        for _ in range(rng.rint(2, 3)):
            self.player.add_consumable(
                rng.choice(["potion", "hipotion", "ether", "antidote"]), 1)

        from .items import SALVAGE_NAME
        # **Two short notices rather than one long one** (s7). A toast is
        # sized to its own text and `ellipsize` trims it to the window, so
        # a single line naming gold, steel and a count is the line that
        # loses its ending on a narrow window - and this is the one moment
        # in the game where what you got is the whole point.
        self.toast("A hoard! %d gold, %d item%s."
                   % (gold, len(kept), "" if len(kept) == 1 else "s"),
                   (252, 226, 104), 7.0)
        self.toast("+%d %s" % (materials, SALVAGE_NAME), (252, 226, 104), 6.0)
        if missed:
            # The bag being full must not silently eat the rare thing.
            self.toast("%d more would not fit in your bag." % missed,
                       BAD, 5.0)
        self.log.event("hoard_taken", door="%d_%d" % (door[0], door[1]),
                       gold=gold, items=len(kept))
        self.sfx("chest")

    def chest_lost(self, gx, gy):
        """We asked the host for a chest and were told it was already empty.

        Solo, the same press says this immediately. Saying nothing here
        would make a room feel like a stuck key rather than a lost race.
        """
        self.toast("Already looted.", DIM, 1.2)

    def take_chest_loot(self, gx, gy, loot):
        """Apply a rolled chest payload. Same path host-side and client-side.

        Everything in `loot` may have come off a socket, so it is read the
        way save data is read: coerced, range-checked and allow-listed, with
        a bad field costing that one reward rather than the run.
        """
        chest = self.world.chest_at(gx, gy)
        if chest is None:
            return
        first = self.mark_chest_opened(gx, gy)
        if not first:
            self.toast("Already looted.", DIM, 1.2)
            return
        self.player.stats_tracker["chests"] += 1
        self.sfx("chest")
        self.particles.burst(gx * self.tile + self.tile / 2,
                             gy * self.tile + self.tile / 2,
                             26, (250, 220, 120), speed=190, life=0.8)

        loot = sec.safe_dict(loot)
        gold = sec.safe_int(loot.get("gold"), 0, 100000, 0)
        if gold:
            self.player.gold += gold
            self.player.stats_tracker["gold_earned"] += gold
            self.toast("+%d gold" % gold, ACCENT, 3.0)

        item = Item.from_dict(loot.get("item")) if loot.get("item") else None
        if item is not None:
            self.take_item(item, source="the chest")

        key = sec.safe_choice(loot.get("cons"), tuple(CONSUMABLES), "")
        if key:
            self.player.add_consumable(key)
            self.toast("Found %s" % CONSUMABLES[key].name, DIM, 2.5)

        relic_key = sec.safe_choice(
            loot.get("relic"),
            tuple(r.key for r in relics_mod.COMMON_RELICS), "")
        if relic_key:
            relic = relics_mod.RELICS_BY_KEY[relic_key]
            if self.player.add_relic(relic.key):
                self.toast("Relic: %s" % relic.name, relic.color, 5.0)
                self.sfx("unlock")
        self.after_battle_checks()

    def enter_portal(self, portal):
        if portal is None:
            return
        definition = BOSS_BY_KEY.get(portal.boss_key)
        last = portal.boss_key == story.WARDENS[-1].boss_key

        if (last and self.world.hollow_key and not portal.defeated
                and self.world.wardens_defeated() == 0):
            self.dialogue = {
                "title": "The Door",
                "body": ["The key has no lock and the Door has no keyhole,",
                         "and yet.",
                         "",
                         "Six wardens are still standing at their waypoints.",
                         "Not one of them has had to explain itself to you."],
                "confirm": "Turn the key",
                "on_confirm": self.open_ending_choice}
            self.go(DIALOGUE)
            return
        if portal.defeated and last:
            self.open_ending_choice()
            return
        if portal.defeated:
            self.toast("This warden has already fallen.", DIM, 2.0)
            return
        if not portal.unlocked:
            self.toast("Sealed. Fell the previous warden first.", BAD, 3.0)
            self.sfx("error")
            return
        lore = story.WARDEN_BY_BOSS.get(portal.boss_key)
        known = self.meta.knows(lore.memory_key) if lore else False
        body = list(lore.intro) if lore else ['"%s"' % definition.taunt]
        body += ["",
                 "Recommended level %d.  You are level %d."
                 % (definition.level, self.player.level)]
        if known and lore:
            # Second time around you know exactly who you are about to kill.
            body += ["", "You know this one. %s, %s."
                     % (lore.true_name, lore.epitaph)]
        else:
            body += ["", "There is no fleeing a warden."]
        # **Assembled** (§19 decision 4, 2026-08-24): *"party-wide, but
        # everyone must be present before it starts"*. Until now a warden
        # was fought alone even in a room, and §31.9 has carried that as
        # a thing that looks like a bug and is not - because his answer
        # was recorded and never built. This is it.
        missing = self.wardens_missing()
        if missing:
            body += ["", "Not everybody is here. Waiting on %s."
                     % ", ".join(missing[:3])]
        self.dialogue = {
            "title": lore.true_name if known and lore else definition.name,
            "body": body,
            # **A control that will refuse you is not drawn** - his rule,
            # 2026-08-31 (§45). If the room is not assembled the button
            # says so rather than being a door that does not open.
            "confirm": ("Wait for them" if missing else "Draw your weapon"),
            "on_confirm": (None if missing
                           else lambda: self.open_warden(definition)),
        }
        self.go(DIALOGUE)

    #: How close a friend has to be standing to count as here, in tiles.
    #: **The same reach a shared encounter uses** (`coop.FIGHT_REACH`),
    #: because it is the same question - somebody across the island is
    #: not in this fight - and two numbers for one idea drift.
    WARDEN_REACH = coop_mod.FIGHT_REACH

    def wardens_missing(self):
        """Who is in the room but not at the door (decision 4).

        Empty solo, and empty in a room where everybody is standing with
        you. **Names**, because "not everybody is here" without saying
        who is a message you cannot act on.
        """
        if self.coop is None or not self.coop.ready or self.player is None:
            return []
        away = []
        for remote in self.coop.here():
            gap = math.hypot(remote.x - self.player.x,
                             remote.y - self.player.y) / max(1, self.tile)
            if gap > self.WARDEN_REACH:
                away.append(remote.name or "somebody")
        return away

    def warden_party_size(self):
        """How many are about to fight it.

        **Everyone in the room who is present**, which is what makes the
        fight party-wide - and it feeds `make_boss_encounter`, so a
        warden met by three people is sized for three.
        """
        if self.coop is None or not self.coop.ready:
            return 1
        return 1 + len(self.coop.here()) - len(self.wardens_missing())

    def open_warden(self, definition):
        """Start a warden fight, alone or with the room.

        Solo takes the path it always took. In a room the host opens it
        as a shared fight through the ordinary door (§21), so the
        arbitration, the payout and the mirroring are the ones every
        other shared fight already uses.
        """
        party = max(1, self.warden_party_size())
        if self.coop is not None and self.coop.ready and party > 1:
            if self.coop.role == coop_mod.HOST:
                self.host_open_boss(definition, party)
            else:
                # A guest asks; the host is the authority on every shared
                # fight and a warden is not the exception any more.
                self.coop.request_boss(definition.key)
            return
        self.start_battle(make_boss_encounter(definition, self.settings.mods,
                                              party=party))

    def host_open_boss(self, definition, party):
        """Host side: announce a warden the whole room is fighting."""
        bodies = self.coop.bodies()
        host_id = self.coop.player_id
        members = [(host_id, self.player)]
        for player_id, body in bodies.items():
            if player_id == host_id or body is None:
                continue
            members.append((player_id, deserialize_player(body)))
        members.sort(key=lambda pair: -pair[1].stat("spd"))
        order = [player_id for player_id, _ in members]
        seed = rng.rint(1, 2 ** 31 - 2)
        with rng.forked(seed):
            enemies = make_boss_encounter(definition, self.settings.mods,
                                          party=len(members))
        self.coop.open_fight({"seed": seed, "level": int(definition.level),
                              "biome": "ruins", "boss": definition.key,
                              "party": order, "wave": 0, "count": 0})
        self.start_battle(enemies, party=[body for _, body in members],
                          viewer=order.index(host_id), sort_party=False)

    def talk_to_elder(self):
        """Maren says the encouraging half. Which half changes as you go."""
        self.ui.quest_tab = 1          # Offered: you came here for work
        self.ui.quest.index = self.ui.quest.offset = 0
        if self.player.modifiers.has("no_shops"):
            pass
        beaten = self.world.wardens_defeated()
        beaten = min(beaten, 6)
        self.ui.quest_readonly = False
        scene_key = scenes_mod.MAREN_SCENES.get(beaten)
        # Once each, then she goes straight to the board - she is not going
        # to have the same conversation every time you walk past her.
        if scene_key and self.world.flag("maren_%d" % beaten):
            self.play_scene(scene_key, after=lambda: self.go(QUESTS))
            return
        self.go(QUESTS)

    # Keyed by name since 2026-08-31, when the village four stopped being
    # called after their jobs (Savvydev: *"real names for everyone bro"*).
    VILLAGER_LINES = {
        "Tobin": ("Quiet year, so far.",
                  "The wardens keep to their roads. Long may it last."),
        "Grell the Herder": ("Lost two ewes to something up the east road.",
                             "I did not go looking."),
        "Sena the Weaver": ("Maren buys black cloth every few years.",
                            "I have never asked who for."),
        "Pip": ("Are you going up the road?",
                "Everyone who goes up the road is very brave."),
    }

    HOME_LINES = (
        ("The door is shut and the shutters are drawn.",
         "Whoever lives here is not taking visitors."),
        ("Warm inside, by the smell of it. Bread, and woodsmoke.",
         "You have no business knocking."),
        ("A child laughs somewhere behind the door, then goes quiet.",
         "You step back off the step."),
        ("Bolted. Sensibly.",
         "There is a warden four miles up the road."),
    )

    def read_marker(self, spot):
        """Read what is left standing at a ruin or a circle (s55, item 12b).

        **Not a person, and deliberately not a scene.** A scene asks a
        question and leans an ending (s33); this asks nothing and records
        nothing, because a ruin is not a decision. It is the place saying
        what it was, the way a hamlet's one line is the person saying what
        theirs is - and s44 left this written down as the half of item 12b
        that was still missing.
        """
        reading = self.world.marker_at(*spot) if self.world else None
        if not reading:
            return
        title, lines = reading
        self.dialogue = {"title": title, "body": list(lines),
                         "confirm": "Leave it standing", "on_confirm": None}
        self.go(DIALOGUE)
        self.sfx("select")

    def stop_at_wayside(self, spot):
        """Somewhere on the last third of a road worth stopping at (item 12).

        **A favour's machinery, a marker's manners.** It runs on the
        scene screen the way `play_favour` does - because it is a choice
        and a choice needs options - and it records nothing anywhere the
        way `read_marker` does, because a wayside is not story (§33) and
        no ending may ever lean on one.

        The difference from a favour is the only interesting part:
        **there is no right answer.** A favour is a riddle you can get
        wrong and go unpaid for; every option here is a real option and
        walking on is always one of them. That is what makes the road a
        sequence of decisions rather than a sequence of rewards.
        """
        found = self.world.wayside_at(*spot) if self.world else None
        if not found:
            return
        stop, spent = found
        if spent:
            self.dialogue = {"title": stop.title,
                             "body": ["Ashes, and your own boot prints.",
                                      "",
                                      "Whatever was here, you took it or",
                                      "you left it. Either way it is done."],
                             "confirm": "Walk on", "on_confirm": None}
            self.go(DIALOGUE)
            self.sfx("select")
            return
        options = [one for one in stop.options
                   if self.can_afford_wayside(one[2])]
        if not options:
            # **Never offer what will be refused** - his rule, 2026-08-31
            # (§45). A stop whose every option costs more than you have
            # is a stop you have not reached yet, not a conversation.
            self.toast("Nothing here you can use yet.", DIM, 1.6)
            return
        self.scene = {
            "scene": scenes_mod.Scene(
                "wayside_%s" % stop.key, stop.title, "The Road",
                tuple(stop.prompt), ()),
            "options": [scenes_mod.Choice(str(i), label, tuple(reply))
                        for i, (label, reply, _e) in enumerate(options)],
            "index": 0, "phase": "choosing", "reply": (),
            "wayside": {"key": stop.key, "spot": tuple(spot),
                        "effects": [effect for _l, _r, effect in options]},
            "after": None,
        }
        self.go(SCENE)
        return True

    def can_afford_wayside(self, effect):
        """Whether this option is one the player could actually take."""
        cost = int(effect.get("gold", 0))
        if cost < 0 and self.player.gold < -cost:
            return False
        need = effect.get("consumable")
        if need and self.player.bag_full():
            return False
        return True

    def settle_wayside(self, index):
        """Pay for, and be paid by, whichever way you went.

        **Spent whichever option was taken, including walking on.** A
        wayside you can come back to and take the other branch of is a
        menu, not a decision - the same call `settle_favour` makes about
        one attempt at a riddle.
        """
        state = self.scene
        info = state.get("wayside") or {}
        stop = story.WAYSIDE_BY_KEY.get(info.get("key", ""))
        spot = tuple(info.get("spot") or ())
        effects = info.get("effects") or []
        if stop is None or len(spot) != 2:
            return
        self.world.settle_wayside(*spot)
        if not 0 <= index < len(effects):
            return
        effect = effects[index]
        self.apply_wayside(stop, effect)

    def apply_wayside(self, stop, effect):
        """What one wayside option actually does.

        Every arm is something the game already had - gold, a rolled
        item, a potion, health, XP, a fight. **Nothing here is a new
        system**, which is what keeps twelve stops per island from being
        twelve new things that can break.
        """
        told = []
        gold = int(effect.get("gold", 0))
        if gold:
            self.player.gold = max(0, self.player.gold + gold)
            if gold > 0:
                self.player.stats_tracker["gold_earned"] += gold
            told.append("%+d g" % gold)
        rest = float(effect.get("rest", 0.0))
        if rest:
            back = self.player.heal(int(self.player.max_hp() * rest))
            back_mp = self.player.restore_mp(
                int(self.player.max_mp() * rest))
            if back or back_mp:
                told.append("+%d hp, +%d mp" % (back, back_mp))
        need = effect.get("consumable")
        if need:
            self.player.add_consumable(need, 1)
            told.append("+1 %s" % need)
        if effect.get("item"):
            # Through `roll_loot`, so Brittle Steel still takes its step
            # off it - the funnel §54 built exists so no call site can
            # forget the modifier.
            item = self.roll_loot(max(1, self.player.level))
            if item is not None:
                self.player.add_item(item)
                told.append(item.name)
        xp = int(effect.get("xp", 0))
        levels = []
        if xp:
            levels, _skills = self.player.gain_xp(xp)
            told.append("%d xp" % xp)
        if told:
            self.toast(", ".join(told), ACCENT, 3.5)
            self.sfx("quest")
        if levels:
            self.toast("Level %d!" % self.player.level, GOOD, 4.0)
        if effect.get("fight"):
            # **Asked, not sprung** (§68). A road tile never spawns an
            # encounter and this does not change that: the fight is the
            # one you chose by reaching under the cairn, and it starts
            # after the reply so the answer is read before the arena.
            # The scene's own `after` hook, rather than a flag somebody
            # has to remember to clear: the fight starts when the reply
            # has been read, which is the one ordering that reads right.
            if self.scene is not None:
                self.scene["after"] = self.start_wayside_fight

    def start_wayside_fight(self):
        """The thing that was asleep against the far side of the cairn.

        **Not a road encounter.** `TILES[T_PATH][2]` is 0.00 and stays
        0.00 - roads are safe corridors by design (§31.9) and this does
        not touch that rule. It is the fight you asked for by reaching
        under the stones, which is §68's *asked, not sprung* from the
        other direction: there, a monster walks up to you; here, you
        reach into one.
        """
        if self.player is None or not self.player.alive:
            return
        biome = self.world.biome_at(*self.player_tile())             if self.world else "meadow"
        level = max(1, self.player.level)
        self.start_battle(make_encounter(level, enemy_kin(biome),
                                         self.settings.mods,
                                         party=self.party_size()))

    def use_door(self, target):
        """Doors open onto a trade, a forge, or somebody's private evening.

        There are no interiors to walk into - the village is a place to do
        business and go, not a set of rooms.
        """
        (gx, gy), kind = target
        if kind == "out":
            self.leave_house()
            return
        if kind == "shop":
            self.go(SHOP)
            self.ui.refresh_shop(self.player, self.world)
            self.sfx("select")
            return
        if kind == "forge":
            self.talk_to_smith()
            self.sfx("select")
            return
        self.enter_house((gx, gy))

    def enter_house(self, door):
        """Go inside (s56, roadmap item 18).

        Savvydev, 2026-08-24: *"yes house interior will be good."* §13 had
        recorded the absence as scope - *"the village is a place to do
        business and go, not a set of rooms"* - and §11 named this as the
        feature that would change that.

        **`self.world` is swapped, and that is the whole trick.** A room is
        a `World` (see `interiors.py`), so movement, collision, drawing and
        interaction all carry on against it without knowing anything has
        happened. What is *not* allowed to follow is anything that treats
        the world as the run: `save_run` and the co-op handshake both ask
        `overworld()` instead, so a house cannot be saved into a slot or
        broadcast to somebody joining.
        """
        if self.world is None or self.player is None:
            return
        if self.house is not None:
            return
        room, spawn, way_out = interiors.build(self.world.seed, door,
                                               self.tile)
        self.house = {"world": self.world, "door": door, "out": way_out,
                      "x": self.player.x, "y": self.player.y}
        self.world = room
        self.player.x = spawn[0] * self.tile + self.tile / 2
        self.player.y = spawn[1] * self.tile + self.tile / 2
        self.player.facing = "back"
        self.enter_overworld(snap=True)
        self.toast(interiors.room_note(door), DIM, 4.0)
        self.sfx("select")

    def camera_target(self):
        """Where the camera looks: the player - or, indoors, the room.

        **A room that fits the window is framed, not followed** (§95).
        You come in at the door in the south wall, and a camera centred on
        you there hangs the whole room above your head with half the
        window blank wall beneath it. Each axis on its own: a room too
        wide for a narrow window still follows you across, and one too
        tall still follows you up.
        """
        x, y = self.player.x, self.player.y
        bounds = (getattr(self.world, "room_bounds", None)
                  if self.house is not None else None)
        if bounds:
            rx, ry, rw, rh = bounds
            if rw * self.tile <= self.camera.view_w:
                x = (rx + rw / 2) * self.tile
            if rh * self.tile <= self.camera.view_h:
                y = (ry + rh / 2) * self.tile
        return x, y

    def step_outdoors(self):
        """Put the house down without walking out of it: the island back,
        the hero on the step, the screen as it was (s96).

        For leaving a room, which has to take the house with it and is
        not a door - `leave_house` is the door, and says so.
        """
        state, self.house = self.house, None
        if state is None:
            return
        self.world = state["world"]
        if self.player is not None:
            self.player.x, self.player.y = state["x"], state["y"]
            self.player.facing = "front"
            self.camera.follow(*self.camera_target(), 0.0, snap=True)
            self.last_tile = self.player_tile()

    def leave_house(self):
        """Back out onto the step you came in from."""
        if self.house is None:
            return
        state, self.house = self.house, None
        self.world = state["world"]
        self.player.x, self.player.y = state["x"], state["y"]
        self.player.facing = "front"
        self.enter_overworld(snap=True)
        self.sfx("back")

    def overworld(self):
        """The island, even while the player is standing in a kitchen.

        **Everything that treats the world as *the run* asks this**, not
        `self.world` - saving, and the co-op handshake that sends a joiner
        a seed. Getting a house's seed into either of those would be a save
        of a room, or a guest rebuilding their island out of somebody's
        front parlour.
        """
        if self.house is not None:
            return self.house["world"]
        return self.world

    def talk_to_villager(self, npc):
        """The town, which does not know what is happening up the road.

        The first conversation with each of them is a scene with answers;
        after that they go back to a line of flavour, because a passer-by
        who interrogates you every single time is not a village, it is a
        quiz.
        """
        scene_key = scenes_mod.VILLAGER_SCENES.get(npc.name)
        if scene_key and self.world.flag("villager_%s" % npc.name):
            self.play_scene(scene_key)
            return
        # **A hamlet person's own line is their scene** (2026-08-30). They
        # have no written conversation - what they have is one thing to
        # say about the place they live - so it goes in the same slot, on
        # the same flag, for the reason the note below gives: a favour
        # offered before it jumps the queue in front of the only thing
        # they were given to say.
        if npc.lines and self.world.flag("villager_%s" % npc.name):
            self.dialogue = {"title": npc.name, "body": list(npc.lines),
                             "confirm": "Nod", "on_confirm": None}
            self.state = DIALOGUE
            return
        # **After their own scene, not before it.** A favour offered on the
        # first meeting jumps the queue in front of the villager's one
        # written conversation, which is the wrong way round - the story
        # they have is theirs, and the errand is what they get to once you
        # have met.
        if self.play_favour(npc):
            return
        if self.offer_errand(npc):
            return
        # **What they were built with wins** (2026-08-30). Hamlet folk
        # carry the lines for the place they live (`story.HAMLET_LINES`),
        # and the four village names are in `VILLAGER_LINES` below. Only
        # somebody with neither gets the default, and nobody in the game
        # currently has neither.
        lines = npc.lines or self.VILLAGER_LINES.get(
            npc.name, ("Quiet year, so far.",))
        self.dialogue = {"title": npc.name, "body": list(lines),
                         "confirm": "Nod", "on_confirm": None}
        self.state = DIALOGUE

    def offer_errand(self, npc):
        """Somebody asks you to go and do something (s42).

        **A sidequest, not a favour and not one of Maren's jobs.** It runs
        on its own track, so taking one never costs you the quest you are
        carrying - and you may hold several, which Savvydev confirmed on
        2026-08-29.
        """
        errand = self.world.errand_at(npc.gx, npc.gy)
        if errand is None:
            return False
        log = self.quests
        if errand.key in log.completed:
            return False
        voice = story.errand_voice(errand.key)
        if errand.key in log.errands:
            done, target = log.progress(self.player, errand.key)
            if done < target:
                body = list(voice.chase) + [""] if voice else []
                self.dialogue = {
                    "title": npc.name,
                    "body": body + [errand.detail, "",
                                    "%d of %d so far." % (done, target)],
                    "confirm": "Keep at it", "on_confirm": None}
                self.go(DIALOGUE)
                return True
            reward = log.claim_errand(self.player, errand.key)
            if reward:
                self.pay_errand(npc, errand, reward)
            return True
        if log.errands_full():
            self.dialogue = {
                "title": npc.name,
                "body": ["They look at how much you are already carrying.",
                         "", '"Come back when you have done some of it."'],
                "confirm": "Fair", "on_confirm": None}
            self.go(DIALOGUE)
            return True
        # **Their reason first, then the counter.** The detail line stays -
        # a player has to know what the target actually is - but it is no
        # longer the whole of what an errand says (s44).
        body = list(voice.ask) + [""] if voice else []
        self.dialogue = {
            "title": npc.name,
            "body": body + [errand.detail, "",
                            "%d gold and %d experience, when it is done."
                            % (errand.gold, errand.xp)],
            "confirm": "Take it on",
            "on_confirm": lambda: self.take_errand(errand),
        }
        self.go(DIALOGUE)
        return True

    def take_errand(self, errand):
        if self.quests.accept_errand(errand.key, self.player):
            self.log.event("errand_taken", key=errand.key)
            self.toast("Errand: %s" % errand.title, ACCENT, 3.5)
            self.sfx("quest")

    def pay_errand(self, npc, errand, reward):
        """Pay an errand out, in the words of whoever asked for it.

        **The old version was gendered and the speaker is not.** It said
        *"she says so in about four words"* and *"out of her bag"*, and an
        errand is handed to whoever the world had spare - a runner, a
        villager, or "A man of Two Wells". `story.ERRAND_VOICE` is written
        in the first person for exactly this reason (s44).
        """
        gold, xp, ilvl = reward
        self.player.gold += gold
        self.player.stats_tracker["gold_earned"] += gold
        levels, _skills = self.player.gain_xp(xp)
        voice = story.errand_voice(errand.key)
        body = list(voice.done) + [""] if voice else ["It is done.", ""]
        body.append("%d gold, %d experience." % (gold, xp))
        if ilvl and not self.player.bag_full():
            item = self.roll_loot(ilvl, luck=0.3)
            self.player.add_item(item)
            body.append("And something out of a pocket: %s." % item.name)
        self.player.stats_tracker["errands"] = (
            self.player.stats_tracker.get("errands", 0) + 1)
        self.log.event("errand_done", key=errand.key)
        self.dialogue = {"title": npc.name, "body": body,
                         "confirm": "Nod", "on_confirm": None}
        self.go(DIALOGUE)
        if levels:
            self.toast("Level %d!" % self.player.level, GOOD, 4.0)
        self.sfx("quest")

    def play_favour(self, npc):
        """Somebody asks you for something small (s42).

        **On the scene screen and recorded nowhere.** A favour is not a
        scene - it leans no ending, unlocks no secret and never reaches
        the meta profile - and it is not a quest either, because
        `quests.py` caps you at one at a time and that cap is a design
        decision (s9), not a limitation to route around. It is settled
        inside the conversation that starts it.
        """
        found = self.world.favour_at(npc.gx, npc.gy)
        if not found:
            return False
        favour, done = found
        if done:
            return False
        options = []
        for label, reply, correct in favour.options:
            if favour.kind == "toll" and correct and favour.needs:
                # An option you cannot take is not offered. Asking for a
                # potion you do not have and then refusing the answer is
                # a worse conversation than not asking.
                if not self.player.consumables.get(favour.needs):
                    continue
            options.append((label, reply, correct))
        if not any(correct for _l, _r, correct in options):
            return False
        self.scene = {
            "scene": scenes_mod.Scene(
                "favour_%s" % favour.key, npc.name, "The Valley",
                tuple(favour.prompt), ()),
            "options": [scenes_mod.Choice(str(i), label, tuple(reply))
                        for i, (label, reply, _c) in enumerate(options)],
            "index": 0, "phase": "choosing", "reply": (),
            "favour": {"key": favour.key, "spot": (npc.gx, npc.gy),
                       "right": [i for i, (_l, _r, c)
                                 in enumerate(options) if c]},
            "after": None,
        }
        self.go(SCENE)
        return True

    def settle_favour(self, index):
        """Pay for a favour, if that was the answer they wanted."""
        state = self.scene
        favour = story.FAVOUR_BY_KEY.get(state["favour"]["key"])
        spot = state["favour"]["spot"]
        if favour is None:
            return
        # **One attempt, whichever way it goes.** The first version left a
        # wrong answer askable, on the grounds that these are favours
        # rather than trials - which sounded kind and made every riddle
        # meaningless, because you could simply keep guessing. The answer
        # you give is your answer. There is no penalty beyond not being
        # paid.
        self.world.settle_favour(*spot)
        if index not in state["favour"]["right"]:
            return
        if favour.kind == "toll" and favour.needs:
            self.player.use_consumable(favour.needs)
        if favour.reward_gold:
            self.player.gold += favour.reward_gold
            self.player.stats_tracker["gold_earned"] += favour.reward_gold
        levels = []
        if favour.reward_xp:
            levels, _skills = self.player.gain_xp(favour.reward_xp)
        self.player.stats_tracker["favours"] = (
            self.player.stats_tracker.get("favours", 0) + 1)
        self.log.event("favour", key=favour.key)
        self.toast("%d g, %d xp" % (favour.reward_gold, favour.reward_xp),
                   ACCENT, 3.5)
        if levels:
            self.toast("Level %d!" % self.player.level, GOOD, 4.0)
        self.sfx("quest")

    def talk_to_keeper(self, npc):
        """Somebody who lives on the road, and what they will say (s42).

        **Two scenes and then a standing line.** On the way out the keeper
        hands you a fragment that cannot resolve. On the way back - once
        the thing at the end of the road has fallen and its Memory is in
        your pack - the same person gets the conversation the fragment was
        for, and this time *you* are the one who knows something. After
        that they are somebody you can say hello to.

        The change is the point of the whole design: the keeper has learned
        nothing between the two, and neither has the valley. What changed
        is that you read a page.

        **Each scene fires once and only once**, on a world flag, because
        s33 says a moment that replays stops being a moment - and the
        codex is where you go to see one again.

        **The Memory is part of the gate, not just the kill.** The
        resolution scene says out loud that you are carrying a page, so it
        waits until that is true rather than assuming the two always arrive
        together. Nothing is lost by waiting: the flag is not spent until
        the scene actually plays.
        """
        stop_key = self.world.waystations.get((npc.gx, npc.gy))
        stop = story.WAYSTATION_BY_KEY.get(stop_key) if stop_key else None
        if stop is None:
            self.talk_to_villager(npc)
            return
        portal = next((p for p in self.world.portals
                       if p.boss_key == stop.boss_key), None)
        felled = bool(portal and portal.defeated)
        lore = story.WARDEN_BY_BOSS.get(stop.boss_key)
        read_it = bool(lore and self.player
                       and lore.memory_key in self.player.relics)
        if not felled:
            if self.world.flag("way_%s" % stop.key) and self.play_scene(
                    stop.scene):
                return
        elif read_it and self.world.flag("res_%s" % stop.key):
            # **It plays whether or not you stopped here on the way out.**
            # A player who walked straight past the first time still killed
            # the thing and still read the page, and locking the payoff
            # behind a conversation they did not know was available would
            # delete it for exactly the players it is written for. The
            # scenes lean on what the Memory says rather than on what the
            # keeper said, so they read cold.
            if self.play_scene(stop.resolution):
                return
        self.dialogue = {
            "title": "%s - %s" % (stop.name, npc.name),
            "body": list(story.waystation_lines(stop.key, felled)),
            "confirm": "Nod", "on_confirm": None}
        self.state = DIALOGUE

    def talk_to_runner(self, npc):
        """A courier carries Maren's work out to the roads."""
        self.ui.quest_readonly = False
        self.ui.quest_tab = 1          # Offered, for the same reason
        self.ui.quest.index = self.ui.quest.offset = 0
        if self.world.flag("met_runner"):
            self.dialogue = {
                "title": npc.name,
                "body": ["She sends word out. I carry it back.",
                         "",
                         "Anything she is offering, I can write you down for,",
                         "and I can pay you out when it is done.",
                         "You do not have to walk all the way in."],
                "confirm": "See the work",
                "on_confirm": lambda: self.go(QUESTS)}
            self.state = DIALOGUE
            return
        self.go(QUESTS)

    def talk_to_smith(self):
        """Bram. Sixty-one years old, brother of a man three centuries gone."""
        if self.player.modifiers.has("no_shops"):
            self.toast("Bram will not open the forge for you.", BAD, 2.5)
            self.sfx("error")
            return
        if self.world.flag("met_bram"):
            self.unlock_codex("bram")
        holds_letter = "memory_ilan" in self.player.relics
        if holds_letter and self.world.flag("gave_letter"):
            self.unlock_codex("letters")
            self.dialogue = {
                "title": "Bram Vance",
                "body": ["You have it. You have his hand on it.",
                         "",
                         "No. Do not read it to me. Give it here.",
                         "",
                         "He was not angry. You said that already.",
                         "Say it once more and then go, please."],
                "confirm": "Leave him to it",
                "on_confirm": lambda: self.go(SMITH)}
            self.state = DIALOGUE
            return
        self.go(SMITH)

    def strip_world_for_modifiers(self):
        """Take out of the world what this run's modifiers forbid.

        **Runs after every world build, because the world is a seed.**
        Terrain regenerates on load (hard rule 4), so a prop removed at
        creation comes back unless this is re-applied - which is why it is
        one method called from both paths rather than a line in `new_run`.

        Only Hollowed needs it today: *"and remove the shrine entirely
        too"* (s45).
        """
        if not (self.world and self.player):
            return
        if self.player.modifiers.has("no_shrine"):
            self.world.remove_shrines()

    def shrine_cost(self):
        """Mending a bigger body costs more, so it stays a real decision."""
        player = self.player
        if player.hp >= player.heal_cap() and player.mp >= player.max_mp():
            return 0
        return 12 + player.max_hp() // 6

    def use_shrine(self):
        if self.player.modifiers.has("no_shrine"):
            self.toast("The shrine is cold to you. Hollowed.", BAD, 2.5)
            self.sfx("error")
            return
        cost = self.shrine_cost()
        if cost and self.player.gold < cost:
            self.toast("The shrine asks %d gold. You have %d."
                       % (cost, self.player.gold), BAD, 3.0)
            self.sfx("error")
            return
        if not cost:
            self.toast("You are already whole.", DIM, 2.0)
            return
        self.player.gold -= cost
        self.player.full_restore()
        self.sfx("holy")
        self.particles.burst(self.player.x, self.player.y, 30,
                             (252, 240, 180), speed=150, life=0.9, gravity=-40)
        # **The price it charged, not a number from before it had one**
        # (s96). This said -25 whatever the prompt a step earlier had
        # asked - 23g at level 1 - since the cost began to grow.
        self.toast("Fully restored. (-%d gold)" % cost, GOOD, 2.5)
        self.save_run(silent=True, force=True)

    # ---------------------------------------------------------- rendering ---

    def draw_overworld(self):
        surface = self.screen
        tile = self.tile
        ox, oy = self.camera.offset
        xs, ys = self.camera.visible_tiles()
        frame = int(self.world_time * 3) % 4

        for gy in ys:
            for gx in xs:
                texture = self.world.texture_at(gx, gy)
                # A cheap hash picks one of four variants, so a field of grass
                # stops reading as graph paper.
                variant = (gx * 7 + gy * 13 + (gx ^ gy)) & 3
                surface.blit(self.assets.tile(texture, frame, variant),
                             (gx * tile - ox, gy * tile - oy))

        # Props and the player share a depth sort so trees overlap correctly.
        drawables = []
        for gy in ys:
            for gx in xs:
                prop = self.world.props.get((gx, gy))
                if not prop:
                    continue
                sprite = self.assets.sprite(prop, scale=self.prop_scale(prop))
                if prop == "portal":
                    portal = self.world.portal_at(gx, gy)
                    if portal and not portal.unlocked:
                        sprite = sprite.copy()
                        sprite.set_alpha(120)
                px = gx * tile + (tile - sprite.get_width()) // 2 - ox
                py = gy * tile + (tile - sprite.get_height()) - oy + 2
                # A slow, per-tile-offset sway keeps the world from looking
                # like a photograph.
                phase = self.world_time * 1.6 + (gx * 0.7 + gy * 1.3)
                if prop in ("elder", "smith"):
                    py += int(math.sin(phase) * 1.5)
                elif prop == "tree":
                    px += int(math.sin(phase * 0.45) * 1.2)
                elif prop in ("portal", "shrine"):
                    py += int(math.sin(phase * 0.8) * 2.0)
                drawables.append((gy * tile + tile, sprite, (px, py), prop))

        # Four-phase walk: contact, pass, contact, pass - and since s71
        # a plant on each contact and a breath when standing still, which
        # is the same `Pose` the battle screen has always drawn bodies
        # with. The overworld was the last place in the game where a body
        # was a rectangle that moved.
        walk = anim.walk_pose(self.walk_anim, self.walk_ease,
                              self.world_time,
                              running=getattr(self, "walk_running", False))
        step = int(self.walk_anim * 1.7) % 4 if self.walk_ease else 0
        hero = self.assets.hero(self.player.facing, step,
                                appearance=self.player.appearance)
        hero = shaped(hero, walk)
        hx = int(self.player.x - hero.get_width() / 2 - ox)
        hy = int(self.player.y - hero.get_height() + tile * 0.42 - oy
                 + walk.dy)
        drawables.append((self.player.y + tile * 0.4, hero, (hx, hy), "hero"))

        # **Monsters you can see** (item 0aj, s68), in the same depth list
        # as everything else so one behind a tree is behind the tree.
        for one in self.wanderers:
            sprite = self.assets.sprite(one["sprite"])
            wob = math.sin(one["bob"] * 3.0) * 1.6
            wx = int(one["x"] - sprite.get_width() / 2 - ox)
            wy = int(one["y"] - sprite.get_height() + tile * 0.42 - oy + wob)
            drawables.append((one["y"] + tile * 0.4, sprite, (wx, wy),
                              "hero"))

        # Everybody else in the room, sorted into the same depth list so they
        # pass behind a tree exactly as you do.
        nametags = []
        if self.coop is not None:
            # **`here()`, not `remotes`** (s57). Somebody indoors is in a
            # different world at coordinates that look like the middle of
            # this one, so drawing everybody would stand them in a field.
            for remote in self.coop.here():
                other = self.assets.hero(
                    remote.facing, step if remote.walking else 0)
                rx = int(remote.x - other.get_width() / 2 - ox)
                ry = int(remote.y - other.get_height() + tile * 0.42 - oy)
                drawables.append((remote.y + tile * 0.4, other, (rx, ry),
                                  "hero"))
                # Name tags are drawn straight after the depth pass, not as
                # floating text: a float drifts and fades, and a label over
                # somebody's head has to just sit there.
                nametags.append((remote.name,
                                 int(remote.x - ox),
                                 int(remote.y - oy - tile * 1.5)))

        drawables.sort(key=lambda d: d[0])
        # A small ellipse under each object reads as it standing on the
        # ground. The previous approach shaded the four tiles around every
        # solid thing, which drew a hard box behind rocks and along water.
        grounded = {"hero", "elder", "smith", "runner", "keeper",
                    "tree", "rock",
                    "chest", "chest_open", "shrine", "portal", "flower"}
        for depth, sprite, pos, kind in drawables:
            if kind in grounded:
                width = int(sprite.get_width() * 0.62)
                shadow = self.assets.shadow(width, max(3, width // 4))
                surface.blit(shadow,
                             (pos[0] + (sprite.get_width() - width) // 2,
                              pos[1] + sprite.get_height() - 7))
            if kind == "portal":
                glow = pulse(self.world_time, 0.5)
                halo = pygame.Surface((tile * 2, tile * 2), pygame.SRCALPHA)
                pygame.draw.circle(halo, (150, 90, 230, int(40 + glow * 50)),
                                   (tile, tile), int(tile * (0.7 + glow * 0.2)))
                surface.blit(halo, (pos[0] - tile // 2, pos[1] - tile // 2))
            surface.blit(sprite, pos)

        for name, nx, ny in nametags:
            draw_text(surface, self.assets, name, (nx, ny), "tiny",
                      (170, 200, 250), center=True)

        self.particles.draw(surface, (ox, oy))
        self.shots.draw(surface, self.assets, (ox, oy))
        self.floats.draw(surface, self.assets, (ox, oy))

        tint = self.night_tint()
        if tint:
            tint_screen(surface, tint[:3], tint[3])
        surface.blit(self.assets.vignette(surface.get_size()), (0, 0))

    def objective(self):
        """Where the chapter is pointing: `(x, y, label)` or None.

        **Read off the world, never stored.** The next road is whichever
        portal is unlocked and still standing, which is a fact the world
        already knows - keeping a copy of it somewhere would be a second
        source of truth that can disagree with the first.

        Roadmap item 11, and the complaint behind it (s40): his friend
        *"had no clue what was really going on"*. A player who does not
        know where to go next cannot receive a story at all.
        """
        if not self.world or not self.player:
            return None
        for portal in self.world.portals:
            if portal.unlocked and not portal.defeated:
                return (portal.gx * self.tile + self.tile / 2,
                        portal.gy * self.tile + self.tile / 2,
                        "The road")
        # Every warden is down: the Door is open and Maren asked you to see
        # her first (chapter seven, and MAREN_BRIEFINGS[6] before it).
        for npc in getattr(self.world, "npcs", ()):
            if npc.kind == "elder":
                return (npc.gx * self.tile + self.tile / 2,
                        npc.gy * self.tile + self.tile / 2,
                        "Elder Maren")
        return None

    def draw_objective_marker(self, surface):
        """An arrow at the screen edge for where the chapter says to go.

        Same shape as the fighting-friend arrow below, deliberately: a
        player has already learned to read one edge arrow, and a second
        idiom would be a second thing to learn. It is a different colour
        and it never shows a distance in metres, because "how far" is not
        the question it answers.

        **Nothing when the place is on screen.** An arrow pointing at
        something you can see is clutter, and this one is always on - it
        has to earn that by staying quiet.
        """
        if self.state != OVERWORLD or not self.player:
            return
        target = self.objective()
        if target is None:
            return
        tx, ty, label = target
        width, height = surface.get_size()
        cx, cy = width / 2.0, height / 2.0
        margin = 46
        dx, dy = tx - self.player.x, ty - self.player.y
        if math.hypot(dx, dy) < 1.0:
            return
        if abs(dx) < cx - margin and abs(dy) < cy - margin:
            return
        angle = math.atan2(dy, dx)
        half_w, half_h = cx - margin, cy - margin
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        scale = min(half_w / abs(cos_a) if abs(cos_a) > 1e-6 else 1e9,
                    half_h / abs(sin_a) if abs(sin_a) > 1e-6 else 1e9)
        ax, ay = cx + cos_a * scale, cy + sin_a * scale

        # Slower and dimmer than the co-op arrow. That one is somebody in
        # trouble and should catch the eye; this one is a signpost and
        # should not compete with it when both are up.
        beat = 0.55 + 0.25 * pulse(self.world_time, 2.6)
        colour = (int(150 * beat), int(196 * beat), int(250 * beat))
        tip = (ax + cos_a * 11, ay + sin_a * 11)
        left = (ax + math.cos(angle + 2.4) * 10,
                ay + math.sin(angle + 2.4) * 10)
        right = (ax + math.cos(angle - 2.4) * 10,
                 ay + math.sin(angle - 2.4) * 10)
        pygame.draw.polygon(surface, colour, [tip, left, right])
        pygame.draw.polygon(surface, (24, 20, 28), [tip, left, right], 1)

        font = self.assets.font("tiny")
        size = font.size(label)
        plate = pygame.Rect(0, 0, size[0] + 12, size[1] + 6)
        plate.center = (int(ax - cos_a * 24), int(ay - sin_a * 24))
        plate.clamp_ip(surface.get_rect())
        backing = pygame.Surface(plate.size, pygame.SRCALPHA)
        backing.fill((14, 12, 18, 200))
        surface.blit(backing, plate.topleft)
        pygame.draw.rect(surface, colour, plate, 1)
        draw_text(surface, self.assets, label, plate.center, "tiny",
                  colour, center=True)

    def draw_battle_markers(self, surface):
        """An arrow at the screen edge for each friend who is fighting.

        Walking into a monster is a private event: nobody else is pulled in,
        and XP goes only to whoever actually swung. The cost of that is not
        knowing your friend is in trouble, so the arrow rides the edge of the
        screen pointing at them with the distance on it. On screen already,
        it draws nothing - you can see them.
        """
        if self.coop is None:
            return
        fighting = self.coop.fighting()
        if not fighting:
            return
        width, height = surface.get_size()
        cx, cy = width / 2.0, height / 2.0
        margin = 46
        for remote in fighting:
            dx = remote.x - self.player.x
            dy = remote.y - self.player.y
            distance = math.hypot(dx, dy)
            if distance < 1.0:
                continue
            # Already visible? Then there is nothing to point at.
            if abs(dx) < cx - margin and abs(dy) < cy - margin:
                continue
            angle = math.atan2(dy, dx)
            # Push out to whichever edge the ray leaves through.
            half_w, half_h = cx - margin, cy - margin
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            scale = min(half_w / abs(cos_a) if abs(cos_a) > 1e-6 else 1e9,
                        half_h / abs(sin_a) if abs(sin_a) > 1e-6 else 1e9)
            ax, ay = cx + cos_a * scale, cy + sin_a * scale

            beat = 0.6 + 0.4 * pulse(self.world_time, 1.6)
            colour = (250, int(120 + 60 * beat), 110)
            tip = (ax + cos_a * 13, ay + sin_a * 13)
            left = (ax + math.cos(angle + 2.4) * 12,
                    ay + math.sin(angle + 2.4) * 12)
            right = (ax + math.cos(angle - 2.4) * 12,
                     ay + math.sin(angle - 2.4) * 12)
            pygame.draw.polygon(surface, colour, [tip, left, right])
            pygame.draw.polygon(surface, (24, 20, 28), [tip, left, right], 1)

            # The corners already belong to the minimap and the HUD panel,
            # and an arrow has to be legible on top of both, so the label
            # gets its own dark backing rather than fighting for contrast.
            label = "%s  %dm" % (remote.name, int(distance / self.tile))
            font = self.assets.font("tiny")
            size = font.size(label)
            lx = int(ax - cos_a * 26)
            ly = int(ay - sin_a * 26)
            plate = pygame.Rect(0, 0, size[0] + 12, size[1] + 6)
            plate.center = (lx, ly)
            plate.clamp_ip(surface.get_rect())
            backing = pygame.Surface(plate.size, pygame.SRCALPHA)
            backing.fill((14, 12, 18, 208))
            surface.blit(backing, plate.topleft)
            pygame.draw.rect(surface, colour, plate, 1)
            draw_text(surface, self.assets, label, plate.center, "tiny",
                      colour, center=True)

    def prop_scale(self, prop):
        """Every sprite shares one grid now; trees just stand taller."""
        return self.assets.sprite_scale() + (1 if prop == "tree" else 0)

    # The drifting motes, per biome. Siblings are tinted near their kin
    # but not identically - standing in the pines should not look exactly
    # like standing in the wood.
    AMBIENT_TINTS = {
        "meadow": (250, 240, 160), "heath": (238, 232, 150),
        "forest": (150, 230, 170), "marsh": (130, 210, 160),
        "cave": (150, 170, 220), "mire": (130, 150, 170),
        "tundra": (220, 240, 255), "clay": (240, 180, 140),
        "pine": (170, 220, 205),
        "volcano": (250, 150, 70), "obsidian": (200, 150, 240),
        "wastes": (230, 210, 170), "salt": (245, 245, 235),
    }

    def spawn_ambient(self, dt):
        """A few drifting motes per second, coloured by the local biome."""
        if len(self.particles.items) > 90 or not rng.chance(dt * 7):
            return
        gx, gy = self.player_tile()
        biome = self.world.biome_map[clamp(gy, 0, WORLD_H - 1)][
            clamp(gx, 0, WORLD_W - 1)]
        color = self.AMBIENT_TINTS.get(biome, (220, 220, 220))
        if biome in ("meadow", "forest", "heath", "marsh") \
                and self.daylight() > 0.6:
            return          # fireflies only come out near dusk
        self.particles.ambient(
            self.player.x + rng.rfloat(-self.camera.view_w / 2,
                                       self.camera.view_w / 2),
            self.player.y + rng.rfloat(-self.camera.view_h / 2,
                                       self.camera.view_h / 2), color)

    # How wide the minimap should come out, in pixels. The world size is
    # free to change; the corner it sits in is not.
    MINIMAP_PIXELS = 224

    def world_map(self, scale=3):
        """The whole island, fogged where nothing has surveyed it (s42).

        **Fog is about knowing, not about going.** Every tile under it is
        walkable right now; what the fog says is that nobody has looked at
        this from anywhere high up. Climbing a tower does not unlock
        country, it remembers it.
        """
        cache = getattr(self, "_worldmap_cache", None)
        key = (self.world, self.world.towers_climbed(), scale)
        if cache is None or cache[0] != key:
            from .textures import MATERIALS
            surf = pygame.Surface((WORLD_W * scale, WORLD_H * scale))
            surf.fill((10, 9, 14))
            for gy in range(WORLD_H):
                for gx in range(WORLD_W):
                    if not self.world.revealed(gx, gy):
                        # Not black: unsurveyed country still shows as
                        # *something*, the way an old chart shows a coast
                        # nobody has walked. A hole reads as broken.
                        surf.fill((24, 22, 30),
                                  (gx * scale, gy * scale, scale, scale))
                        continue
                    colour = MATERIALS[self.world.texture_at(gx, gy)][0]
                    surf.fill(colour, (gx * scale, gy * scale, scale, scale))
            self._worldmap_cache = (key, surf)
            cache = self._worldmap_cache
        return cache[1], scale

    def map_markers(self):
        """What to light up on the big map: `(gx, gy, colour, label)`.

        **Only in surveyed country.** A quest marker glowing inside the
        fog would tell you what is there, which is the one thing the fog
        is for.
        """
        out = []
        target = self.objective()
        if target:
            tx, ty, label = target
            gx, gy = int(tx // self.tile), int(ty // self.tile)
            if self.world.revealed(gx, gy):
                out.append((gx, gy, (250, 220, 120), label))
        for (gx, gy), key in self.world.waystations.items():
            if self.world.revealed(gx, gy):
                stop = story.WAYSTATION_BY_KEY.get(key)
                out.append((gx, gy, (150, 210, 250),
                            stop.name if stop else "Waystation"))
        for (gx, gy), climbed in self.world.towers.items():
            if climbed or self.world.revealed(gx, gy):
                out.append((gx, gy, (120, 230, 200),
                            "Tower" if climbed else "Tower (unclimbed)"))
        for (gx, gy), entry in self.world.stepstones.items():
            if entry[1]:
                out.append((gx, gy, (198, 150, 250), "Stepstone"))
        return out

    def travel_targets(self):
        """Open stepstones, for the map's travel list."""
        out = []
        for spot, key in self.world.open_stepstones():
            stop = story.WAYSTATION_BY_KEY.get(key)
            out.append((spot, stop.name if stop else "Stepstone"))
        return sorted(out, key=lambda row: row[1])

    def event_world_map(self, event):
        targets = self.travel_targets()
        if event.key in (pygame.K_w, pygame.K_UP) and targets:
            self.ui.travel_index = (self.ui.travel_index - 1) % len(targets)
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN) and targets:
            self.ui.travel_index = (self.ui.travel_index + 1) % len(targets)
            self.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e) and targets:
            spot, _name = targets[min(self.ui.travel_index,
                                      len(targets) - 1)]
            if self.travel_to(spot):
                self.enter_overworld(snap=True)
        elif event.key in (pygame.K_m, pygame.K_ESCAPE):
            self.enter_overworld()
            self.sfx("back")

    def open_world_map(self):
        if not self.world or not self.player:
            return
        if self.player.modifiers.has("no_map"):
            # The corner map is gone under Blind Road, so the full one
            # cannot be a way around it.
            self.toast("You are walking blind. That was the deal.",
                       DIM, 2.5)
            self.sfx("error")
            return
        self.go(WORLDMAP)
        self.sfx("select")

    # The window the corner map shows, in tiles. Savvydev, 2026-08-29:
    # *"the minimap on the top right should show a bit of the surrounding
    # area around you"*.
    #
    # **It stopped being the whole world and became a neighbourhood**, and
    # on an island this size that is the only honest thing it can be: 336
    # tiles squeezed into 224 pixels is a smear, and the thing a player
    # wants in the corner is "what is just over there", not "what is the
    # shape of the country".
    MINIMAP_TILES_W = 56
    MINIMAP_TILES_H = 38

    def minimap(self):
        """The country immediately around the player.

        Returns `(surface, scale, origin)` - the origin being the top-left
        tile, because everything drawn on top has to be placed relative to
        the window rather than to the world.

        **No fog here.** You are standing in it; a tower tells you what a
        place looks like from a distance, not what is under your feet.
        """
        from .textures import MATERIALS
        px, py = self.player_tile()
        ox = int(clamp(px - self.MINIMAP_TILES_W // 2,
                       0, max(0, WORLD_W - self.MINIMAP_TILES_W)))
        oy = int(clamp(py - self.MINIMAP_TILES_H // 2,
                       0, max(0, WORLD_H - self.MINIMAP_TILES_H)))
        scale = max(1, round(self.MINIMAP_PIXELS / self.MINIMAP_TILES_W))
        cache = getattr(self, "_minimap_cache", None)
        if cache is not None and cache[0] == (self.world, ox, oy, scale):
            return cache[1], scale, (ox, oy)
        surf = pygame.Surface((self.MINIMAP_TILES_W * scale,
                               self.MINIMAP_TILES_H * scale))
        surf.fill((10, 9, 14))
        for row in range(self.MINIMAP_TILES_H):
            for col in range(self.MINIMAP_TILES_W):
                gx, gy = ox + col, oy + row
                if not self.world.in_bounds(gx, gy):
                    continue
                colour = MATERIALS[self.world.texture_at(gx, gy)][0]
                surf.fill(colour, (col * scale, row * scale, scale, scale))
        self._minimap_cache = ((self.world, ox, oy, scale), surf)
        return surf, scale, (ox, oy)

    def draw_hud(self):
        surface = self.screen
        player = self.player
        width, height = surface.get_size()

        panel(surface, (12, 12, 328, 112), alpha=214)
        named = draw_text(surface, self.assets, player.name, (28, 22), "body",
                          INK)
        # On the name's centre line: `right=True` anchors on the centre
        # (§7), and `24` was a top (§94).
        draw_text(surface, self.assets, "Lv %d" % player.level,
                  (326, named.centery), "small", ACCENT, right=True)

        ratio = player.hp / max(1, player.max_hp())
        bar(surface, (28, 46, 296, 15), self.hp_tween.value, hp_color(ratio),
            label="%d / %d" % (player.hp, player.max_hp()),
            assets=self.assets)
        bar(surface, (28, 66, 296, 11), self.mp_tween.value, (86, 132, 226),
            label="MP %d / %d" % (player.mp, player.max_mp()),
            assets=self.assets)

        need = player.xp_to_next()
        bar(surface, (28, 82, 296, 9), self.xp_tween.value, (192, 118, 250))
        draw_text(surface, self.assets,
                  "XP %s / %s" % (commafy(player.xp), commafy(need)),
                  (176, 87), "tiny", (250, 250, 255), center=True)
        purse = draw_text(surface, self.assets, "%s g" % commafy(player.gold),
                          (28, 98), "small", ACCENT)
        if player.points:
            # **Where to spend them, and a click that goes there** (s96).
            # It said "4 points!" and nothing else: a hero who never
            # opens the sheet fights with none of them - seven rounds a
            # fight at level 5 instead of one. The key is the binding's.
            label = "%d points (%s)" % (player.points,
                                        self.binds.label_for("hero"))
            if (self.assets.font("small").size(label)[0]
                    > 326 - purse.right - 12):
                label = "%d points!" % player.points   # a long key name
            spot = draw_text(surface, self.assets, label, (326, 104),
                             "small", GOOD, right=True)
            if self.state == OVERWORLD and not self.dialogue:
                self.ui.hot.add(spot.inflate(8, 6), "key",
                                self.binds.key_for("hero"))

        if player.statuses:
            status_chips(surface, self.assets, player.statuses, (28, 130))

        # **Blind Road, which for its whole existence did nothing**
        # (2026-08-31, s45). Savvydev: *"the minimap remover modifier doesnt
        # work"*. Its `no_map` tag appeared in `gold_mult` and `xp_mult`
        # and nowhere else, so the modifier that says *"no minimap and no
        # interaction prompts"* was a pure reward.
        #
        # **What it takes is the map and the prompts. Not the controls.**
        # The first version of this returned out of the whole method, which
        # skipped the footer along the bottom as well - and that footer had
        # been given the map key and made clickable in the same session:
        #
        # > "all the tooltips at the bottom left of my screen are gone,
        # > what happened??"
        #
        # A modifier may take away what the game tells you about *the
        # world*. The list of which key does what is not that; it is the
        # controls, and taking it away is not difficulty.
        blind = player.modifiers.has("no_map")
        self.toast_anchor = 120
        if not blind:
            self.draw_minimap_panel(surface, width)
        self.draw_interaction_prompt(surface, width, height, quiet=blind)
        self.draw_overworld_footer(surface, width, height)
        if self.settings.show_fps:
            draw_text(surface, self.assets, "%d fps" % self.clock.get_fps(),
                      (width - 24, height - 32), "tiny", DIM, right=True)

    def draw_minimap_panel(self, surface, width):
        """The corner map, the markers on it, and the biome under it."""
        player = self.player
        # Minimap
        mini, scale, (ox, oy) = self.minimap()
        mw, mh = mini.get_size()
        frame = pygame.Rect(width - mw - 24, 16, mw + 8, mh + 8)
        # On a narrow window the minimap lands on top of the stats panel.
        # The map is the one that goes: it is an orientation aid, and the
        # numbers underneath it are what the fight is decided on.
        if frame.left < 350:
            return
        panel(surface, frame, alpha=200)
        surface.blit(mini, (frame.x + 4, frame.y + 4))
        def spot(gx, gy, size):
            """A world tile as a rect inside the window, or None."""
            col, row = gx - ox, gy - oy
            if not (0 <= col < self.MINIMAP_TILES_W
                    and 0 <= row < self.MINIMAP_TILES_H):
                return None
            return (frame.x + 4 + col * scale - 1,
                    frame.y + 4 + row * scale - 1, size, size)

        for portal in self.world.portals:
            color = (90, 90, 100) if not portal.unlocked else \
                (100, 220, 120) if portal.defeated else (250, 90, 90)
            box = spot(portal.gx, portal.gy, 4)
            if box:
                pygame.draw.rect(surface, color, box)
        for (tx, ty), climbed in self.world.towers.items():
            box = spot(tx, ty, 4)
            if box:
                pygame.draw.rect(surface,
                                 (120, 230, 200) if climbed
                                 else (110, 130, 130), box)
        # **Field bosses are marked too** (s42). A landmark you can only
        # find by walking into it is not a landmark, and in a top-down game
        # the minimap is what "you can see it from over there" means.
        # Dimmer than a portal: a warden is the road, this is an option.
        for (lx, ly), entry in self.world.lairs.items():
            resting = self.playtime < float(entry[2])
            colour = (96, 92, 104) if resting else (226, 214, 160)
            box = spot(lx, ly, 3)
            if box:
                pygame.draw.rect(surface, colour, box)
        gx, gy = self.player_tile()
        box = spot(gx, gy, 4)
        if box:
            pygame.draw.rect(surface, (255, 255, 255), box)

        # **Indoors is not a biome** (s56). A room is a `World` and its
        # biome map says meadow, so the corner read "Verdant Meadow" from
        # inside somebody's kitchen.
        clock_label = "Day" if self.daylight() > 0.5 else "Night"
        where = ("Indoors" if self.house is not None
                 else self.world.biome_at(gx, gy).label)
        draw_text(surface, self.assets,
                  "%s  -  %s" % (where, clock_label),
                  (width - 24, frame.bottom + 8), "small", DIM, right=True)
        # Notices hang under whichever of the two is on screen.
        self.toast_anchor = frame.bottom + 34

    def draw_interaction_prompt(self, surface, width, height, quiet=False):
        """The strip above the footer: an invitation, or what E would do.

        `quiet` is Blind Road, which takes away *"interaction prompts"* -
        but never the invitation, which is a person asking you a question
        with a timer on it rather than the world labelling itself.
        """
        gx, gy = self.player_tile()
        # **The invitation, and it outranks the prompt underneath it.**
        # A fight offer lasts four seconds (s45); whatever you are standing
        # next to will still be there afterwards.
        kind, target = self.world.interactable_near(gx, gy)
        if quiet:
            kind = None
        # **Where the strip sits.** 118 off the bottom normally - above the
        # footer, which is where it has always been - but the touch
        # controls live in that same band and the footer is not drawn when
        # they are on, so with a pad of thumb buttons up it lifts to clear
        # them (s61).
        strip_y = height - 118
        if self.touch.active(self):
            strip_y = min(strip_y,
                          self.touch.ceiling((width, height)) - 46)
        if self.fight_offer:
            # It replaces the ordinary prompt rather than stacking with
            # it - they occupy the same strip, and E means this while it
            # is up (see `interact`). Red, because it expires.
            left = max(0.0, self.fight_offer["left"])
            label = ("%s  %s is fighting - join?   (%ds)"
                     % (self.binds.label_for("interact"),
                        self.fight_offer["by"], max(1, math.ceil(left))))
            hint_w = self.assets.font("body").size(label)[0] + 70
            box = pygame.Rect(width // 2 - hint_w // 2, strip_y,
                              min(hint_w, width - 40), 34)
            panel(surface, box, edge=(226, 96, 96), alpha=235)
            draw_text(surface, self.assets, label, box.center, "body",
                      (250, 200, 200), center=True)
            self.ui.hot.add(box, "key", self.binds.key_for("interact"))
        elif kind:
            label = {"chest": "Open chest", "portal": "Enter arena",
                     "shop": "Enter shop", "npc": "Talk",
                     "door": "Knock", "shrine": "Rest",
                     "marker": "Read",
                     "camp": "Stop here"}.get(kind, "Interact")
            if kind == "door" and target:
                label = {"shop": "Enter the trader",
                         "forge": "Enter the forge",
                         "out": "Step outside"}.get(target[1], "Knock")
            if kind == "chest" and target and target.opened:
                label = "Empty chest"
            elif kind == "shrine":
                # Say plainly what it does and what it takes.
                cost = self.shrine_cost()
                label = ("Rest - restore all HP and MP  (%d g)" % cost
                         if cost else "Rest - you are already whole")
            elif kind == "npc" and target:
                label = "Talk to %s" % target.name.split(",")[0]
            hint_w = self.assets.font("body").size(label)[0] + 70
            box = pygame.Rect(width // 2 - hint_w // 2, strip_y,
                              min(hint_w, width - 40), 34)
            panel(surface, box, edge=ACCENT, alpha=225)
            draw_text(surface, self.assets,
                      "%s   %s" % (self.binds.label_for("interact"), label),
                      box.center, "body", ACCENT, center=True)
            # **Clickable, like the invitation above it.** It said "E" no
            # matter what the key was bound to and it was the one prompt
            # on this strip a mouse could not press - s45's complaint
            # about the footer, in the strip directly above the footer.
            self.ui.hot.add(box, "key", self.binds.key_for("interact"))
        else:
            partner = self.trade_partner()
            if partner is None or quiet:
                return
            label = "%s   Trade with %s" % (self.binds.label_for("trade"),
                                            partner.name)
            hint_w = self.assets.font("body").size(label)[0] + 40
            box = pygame.Rect(width // 2 - hint_w // 2, strip_y,
                              min(hint_w, width - 40), 34)
            panel(surface, box, edge=GOOD, alpha=225)
            draw_text(surface, self.assets,
                      ellipsize(self.assets.font("body"), label, box.w - 24),
                      box.center, "body", GOOD, center=True)
            self.ui.hot.add(box, "key", self.binds.key_for("trade"))

    def draw_overworld_footer(self, surface, width, height):
        """The controls along the bottom. Never taken away by a modifier."""
        # Not on a phone. There is no E to press, the four buttons in the
        # corner say the same thing, and the strip sits exactly where a
        # thumb does.
        if not self.touch.active(self):
            # **Clickable, and it lists the map** (2026-08-31, s45). `hot`
            # was False, so these were decoration; and `M` was missing
            # because the map was not a bindable action at all, so there
            # was no `label_for` to ask.
            #
            # Order is load-bearing: `key_hint` drops whole chips off the
            # end when the window is too narrow (s29), so the most useful
            # one goes first and Menu - which Esc does anyway - goes last.
            # **The trade chip is only there when there is somebody to
            # trade with.** Every other chip is always true; this one
            # would be a dead key most of the time, and a footer that
            # advertises a key that does nothing is what s45 complained
            # about from the other direction.
            trade_chip = ([(self.binds.label_for("trade"), "Trade")]
                          if self.trade_partner() is not None else [])
            key_hint(surface, self.assets,
                     [(self.binds.label_for("interact"), "Interact")]
                     + trade_chip +
                     [(self.binds.label_for("map"), "Map"),
                      (self.binds.label_for("bag"), "Bag"),
                      (self.binds.label_for("hero"), "Hero"),
                      (self.binds.label_for("quests"), "Quests"),
                      (self.binds.label_for("guide"), "Guide"),
                      ("Esc", "Menu")],
                     (20, height - 32), game=self)

    # ------------------------------------------------------------- update ---

    def notices_would_intrude(self):
        """Whether a notice has nowhere to sit in the current fight.

        Two cases, and they are the same case: the space under the enemy
        plate is already spoken for.

        **Stacked** (a narrow window) - every panel runs down the screen
        and there is no free column at all.

        **A submenu is open** - skills and items bottom-anchor upward and
        reach under the plate on a short window, which is where a notice
        goes. A notice over the MP cost of the skill you are choosing is
        worse than a notice a few seconds late.
        """
        if self.screen.get_width() < BATTLE_STACK_WIDTH:
            return True
        return self.battle is not None and self.battle.phase == BATTLE_SUBMENU

    def update(self, dt):
        # Before anything else, and outside the event handler: a window
        # dragged below the minimum is snapped back here rather than
        # mid-drag, where `set_mode` is what closes the game.
        if self.pending_clamp:
            self.apply_pending_clamp()
        if self.state == SPLASH:
            self.update_splash(dt)
            return
        if self.state == CHAPTER_CARD:
            # The card holds over a still valley: co-op keeps running
            # underneath so a room does not time out behind it, but the
            # world does not.
            self.update_coop(dt)
            self.update_chapter_card(dt)
            return
        self.fade.update(dt)
        if self.state == ROAM:
            # **Its own branch, and it returns.** A free-roam fight is
            # not the overworld and not a battle - nothing below this
            # applies to it, and letting the world tick underneath would
            # walk the hero around an island nobody is looking at.
            #
            # **The room still has to be pumped, though** (item 0bg). The
            # first version of this branch returned without it, so a host
            # in a night stopped answering the socket entirely - the
            # guest never even finished the handshake. The one thing an
            # early return must never skip is the network.
            self.update_coop(dt)
            self.update_roam(dt)
            self.floats.update(dt)
            self.particles.update(dt)
            self.toasts.update(dt)
            # **`follow`, not an `update`** - the camera has no idea what
            # a free-roam arena is and does not need one. This is only
            # here to let the screen shake decay, which is what `follow`
            # does on its way past.
            self.camera.follow(self.camera.x, self.camera.y, dt)
            return
        # **Notices wait out a fight on a narrow window.** Stacked, the
        # battle screen is full - log, enemy plate, party strip, player
        # plate, menu - and there is nowhere to float a notice that is
        # not on top of something you need. Moving it under the enemy
        # plate only landed it on the skill costs instead.
        #
        # So they are held rather than hidden: nothing ages, nothing is
        # lost, and a level-up you earned mid-fight is still there when
        # the fight ends. Wide screens are untouched.
        if not (self.state == BATTLE and self.notices_would_intrude()):
            self.toasts.update(dt)
        self.floats.update(dt)
        self.shots.update(dt)
        self.particles.update(dt)

        if self.player:
            maxhp = max(1, self.player.max_hp())
            maxmp = max(1, self.player.max_mp())
            self.hp_tween.set(self.player.hp / maxhp)
            self.mp_tween.set(self.player.mp / maxmp)
            self.xp_tween.set(self.player.xp / max(1, self.player.xp_to_next()))
        self.hp_tween.update(dt)
        self.mp_tween.update(dt)
        self.xp_tween.update(dt)
        self.enemy_hp_tween.update(dt)
        self.pad.update(dt, self)
        self.repeat_held_arrows(dt)
        self.touch.update(dt, self)
        self.update_coop(dt)

        if self.state == OVERWORLD:
            self.playtime += dt
            self.world_time += dt
            self.player.stats_tracker["playtime"] = self.playtime
            # Stacked, so a pad and a thumbstick can both be held at
            # once without either noticing the other.
            keys = self.touch.wrap_keys(
                self.pad.wrap_keys(pygame.key.get_pressed()))
            if not self.fade.busy:
                self.move_player(dt, keys)
                self.check_encounter()
                # **After the step, not before.** A monster you have just
                # walked into should catch you on the frame you touched
                # it, not on the next one (item 0aj).
                self.update_wanderers(dt)
            self.camera.follow(*self.camera_target(), dt)
            self.spawn_ambient(dt)
            if self.settings.autosave and self.autosave_cooldown.trigger():
                self.save_run(silent=True, force=True)

        elif self.state == BATTLE and self.battle:
            self.playtime += dt
            self.battle.update(dt)
            self.enemy_hp_tween.set(
                self.battle.enemy.hp / max(1, self.battle.enemy.max_hp()))
            # **Twice the island's settle** (s96): what a battle shake
            # died at when `draw_battle` followed too, once more a frame
            # at a fixed 1/60 - the same at 60fps, and now at any rate.
            self.camera.follow(*self.camera_target(), dt, settle=84.0)
            # A mirror never ends its own fight. The host says when it is
            # over and what it paid; deciding that locally would be this
            # machine writing its own rewards.
            if (self.battle.finished and self.battle.banner_timer <= 0
                    and not self.battle.mirror):
                self.finish_battle()

    def repeat_held_arrows(self, dt):
        """A held arrow on a menu keeps moving the cursor (s96).

        It moved one row per press, so a forty-row list was forty presses
        from a keyboard while a pad, holding the same direction, ran down
        it (section 14). Same timing as the pad: a pause, then a stream.

        **Arrows only, never Enter.** An operating-system repeat on every
        key would let a held Enter buy a shop out. And only where an
        arrow is a *press*:

        - walking polls the arrows as held keys already, and a repeat
          there would be a second, stuttering walk on top of the first;
        - settings is deaf to real arrows so the konami code can be typed
          on it (`menus.KONAMI_KEYS`);
        - a key being rebound takes the next key it hears as the binding,
          and a repeat is not a key anybody chose.

        A repeat carries `repeat=True`, which the konami watcher skips: a
        held Up is one Up, the way the sequence means it.
        """
        if not self.key_repeat:
            return
        if (self.state != self.key_repeat_state
                or not self.arrows_repeat_here()):
            self.key_repeat.clear()
            return
        held = pygame.key.get_pressed()
        for code in list(self.key_repeat):
            if not held[code]:
                del self.key_repeat[code]
                continue
            remaining = self.key_repeat[code] - dt
            if remaining > 0:
                self.key_repeat[code] = remaining
                continue
            self.key_repeat[code] = gamepad_mod.REPEAT_NEXT
            self.handle_event(pygame.event.Event(
                pygame.KEYDOWN, key=code, unicode="", mod=0, repeat=True))
            if self.state != self.key_repeat_state:
                self.key_repeat.clear()
                return

    def arrows_repeat_here(self):
        """Whether a held arrow is a stream of presses on this screen."""
        if self.state in (SPLASH, CHAPTER_CARD, ROAM, SETTINGS_SCREEN):
            return False
        if self.state == OVERWORLD and not self.dialogue:
            return False
        return not (self.state == KEYBINDS and self.ui.rebinding is not None)

    # -------------------------------------------------------------- events ---

    def handle_event(self, event):
        if event.type == pygame.QUIT:
            self.quit_game()
            return
        if event.type == pygame.VIDEORESIZE and not self.settings.fullscreen:
            self.window_resized(event.w, event.h)
            return
        if event.type == pygame.MOUSEMOTION:
            self.mouse_move(event.pos)
            return
        if event.type == pygame.MOUSEBUTTONDOWN:
            # **Buttons 4 and 5 are the wheel, and they are ignored here.**
            # pygame's own docs: "The mouse wheel will generate
            # pygame.MOUSEBUTTONDOWN and pygame.MOUSEBUTTONUP events when
            # rolled" - button 4 up, button 5 down - and pygame 2 *also*
            # sends MOUSEWHEEL for the same roll. Handling both scrolled
            # every list two rows per notch, which is what a save slot
            # picker jumping in twos looks like.
            #
            # MOUSEWHEEL is the one that survives, because it carries how
            # far rather than just which way.
            if event.button not in (4, 5):
                self.mouse_click(event.pos, event.button)
            return
        if event.type == pygame.MOUSEWHEEL:
            self.mouse_scroll(event.y)
            return
        if event.type in touch_mod.TOUCH_EVENTS:
            self.touch.handle_event(self, event)
            return
        if self.touch.swallow_mouse(event):
            # SDL's own echo of a touch we have already dealt with. Letting
            # it through would make every tap happen twice.
            return
        if event.type in gamepad_mod.PAD_EVENTS:
            self.pad.handle_event(self, event)
            return
        if event.type != pygame.KEYDOWN:
            return
        if (event.key in HELD_ARROWS and not getattr(event, "synthetic", False)
                and not getattr(event, "repeat", False)):
            self.key_repeat[event.key] = gamepad_mod.REPEAT_FIRST
            self.key_repeat_state = self.state
        if self.skip_splash() or self.skip_chapter_card():
            return
        if event.key == pygame.K_F11:
            self.toggle_fullscreen()
            return
        # Only inside a run. There is nothing for it to act on at the title,
        # and an entrance that opens onto an empty game is just a dead end.
        # **Settings is where the sequence lives now** (§19 decision 6).
        # It is the one screen deaf to those keys, which is what makes
        # entering the code there cost nothing - everywhere else, typing it
        # would walk the cursor about on the way in.
        #
        # Overworld and pause are kept because that is where it has always
        # worked and somebody may know it; they still act on the way in.
        if (self.player and self.state in (OVERWORLD, PAUSE, SETTINGS_SCREEN)
                and not getattr(event, "repeat", False)
                and self.konami.feed(event.key)):
            self.ask_open_devmenu()
            return

        handler = {
            TITLE: self.event_title,
            SLOTS: self.event_slots,
            WORLDMAP: self.event_world_map,
            NAME_ENTRY: self.event_name,
            OVERWORLD: self.event_overworld,
            BATTLE: lambda e: handle_battle_event(self, e),
            CLASS_SELECT: self.event_class_select,
            MODIFIER_SELECT: self.event_modifier_select,
            MODE_SELECT: self.event_mode_select,
            WAVES: self.event_waves,
            ROAM: self.event_roam,
            ARENA_SELECT: self.event_arena_select,
            SPOILS: self.event_spoils,
            WAVE_OVER: self.event_wave_over,
            CODEX: self.event_codex,
            GUIDE: self.event_guide,
            KEYBINDS: self.event_keybinds,
            RECORDS: self.event_records,
            ACHIEVEMENTS_SCREEN: self.event_achievements,
            ENDING_CHOICE: self.event_ending_choice,
            ENDING_TEXT: self.event_ending_text,
            GAMEOVER: self.event_gameover,
            DEVMENU: lambda e: devmenu_mod.handle_devmenu_event(self, e),
            SCENE: self.event_scene,
            FULLBAG: self.event_fullbag,
            TRADE: self.event_trade,
            NEWSKILL: self.event_newskill,
            LOBBY: self.event_lobby,
            JOIN_CODE: self.event_join_code,
            STORY: self.event_story,
            DIALOGUE: self.event_dialogue,
        }.get(self.state, lambda e: menus.handle_event(self, e))
        handler(event)

    def event_title(self, event):
        options = self.title_options()
        if event.key in (pygame.K_w, pygame.K_UP):
            self.title_index = (self.title_index - 1) % len(options)
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            self.title_index = (self.title_index + 1) % len(options)
            self.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE):
            self.sfx("select")
            options[self.title_index][1]()
        elif event.key == pygame.K_ESCAPE:
            self.quit_game()

    def title_options(self):
        done, total = self.meta.codex_progress()
        return [
            # **The picker, not the slot list** (0ag, s59). Every new
            # game answers "which game" first now, because a mode
            # reachable only from the co-op flow is one most people
            # would never find.
            ("New Game", self.open_mode_select),
            ("Continue", lambda: self.go_slots("load")),
            ("Guide", lambda: self.go(GUIDE)),
            ("Codex  %d/%d" % (done, total), lambda: self.go(CODEX)),
            ("Achievements  %d/%d" % (len(self.meta.achievements),
                                      len(quest_achievements())),
             lambda: self.go(ACHIEVEMENTS_SCREEN)),
            ("Play together", self.open_lobby),
            ("Records", lambda: self.go(RECORDS)),
            ("Settings", lambda: self.go(SETTINGS_SCREEN)),
            ("Quit", self.quit_game),
        ]

    def clear_run(self):
        """Put the game back to having no run in progress.

        **The title screen means no run** (2026-08-31). "Back to title"
        saved, disconnected and switched screens, but left the hero and
        the world in memory - and everything that asks *"is a run live?"*
        then got the wrong answer. Hosting was the visible one (s45); the
        pause header naming a slot you were no longer in was another.

        Nothing is lost: the caller saves first, and the title's only way
        back into a run is the slot picker, which reads from disk.
        """
        self.player = None
        self.world = None
        self.slot = None
        # A wave run is a run: leaving one leaves no wave state behind
        # for the next mode to trip over (0ag, s59).
        self.forget_the_mode()
        self.pending_mode = gamemodes.DEFAULT_MODE
        self.guest_world = None
        self.guest_pending = None
        self.pre_coop_slot = None
        self.host_when_ready = False

    def forget_the_mode(self):
        """Put down every mode's state: a story run is about to be on
        screen, or none is.

        **One place, because each caller forgot a different part** (s96).
        `new_run` reset the mode and the waves but not a night, a hoard or
        a free-roam fight; `clear_run` the same; and `load_run` - every
        way back into a saved run - and the guest doors reset none of it.
        So a guest who left a Last Stand or a Long Night room went home
        to their own story run *still in that mode*, where `save_run`
        refuses (correctly - §88's gate), and **nothing they did after
        that was ever written down**: no autosave, no Save game, no save
        on quit. In a Last Stand's mode a warden's kill did not count
        either (`on_boss_defeated`).

        Every mode sets its own state after this, so it is safe on any
        way in; nothing here is a story run's own state.
        """
        self.mode = gamemodes.DEFAULT_MODE
        self.waves = None
        self.zombies = None
        self.hoard = None
        self.roam = None
        self.roam_order = []
        self.roam_pending_skill = ""
        self.spoils = []
        self.spoil_at = 0
        self.spoils_then = ""
        self.wave_guest = False
        self.battle = None
        self.wanderers = []
        self.pending_lair = None
        self.pending_gate = None

    def go_slots(self, mode):
        self.ui.slot_mode = mode
        self.ui.slot.index = self.ui.slot.offset = 0
        self.go(SLOTS)

    def event_slots(self, event):
        summaries = self.saves.describe_all()
        state = self.ui.slot
        if event.key in (pygame.K_w, pygame.K_UP):
            state.move(-1, len(summaries))
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            state.move(1, len(summaries))
            self.sfx("move")
        elif event.key == pygame.K_DELETE:
            slot = state.index + 1
            if self.saves.delete(slot):
                self.toast("Slot %d erased." % slot, BAD, 2.5)
                self.sfx("back")
        elif event.key in (pygame.K_RETURN, pygame.K_e):
            slot = state.index + 1
            summary = summaries[state.index]
            if self.ui.slot_mode == "veteran":
                # Only a finished run may cross, so an unfinished row is a
                # wrong answer rather than a second path - say so and stay.
                if not summary.get("beaten"):
                    self.toast("Only a finished run can cross over.",
                               BAD, 3.0)
                    self.sfx("error")
                    return
                self.import_veteran(slot, self.guest_pending)
                return
            if self.ui.slot_mode == "coop":
                # One picker, either answer. Hosting asks *which world*,
                # and an empty row is a perfectly good answer to that -
                # it just means the world does not exist yet.
                if summary.get("empty") or summary.get("corrupt"):
                    self.begin_new_run_setup(slot)
                else:
                    self.load_run(slot)
                return
            if self.ui.slot_mode == "new":
                if not summary.get("empty") and not summary.get("corrupt"):
                    # Starting here would destroy a run in progress.
                    self.confirm_overwrite(slot, summary)
                    return
                self.begin_new_run_setup(slot)
            elif summary.get("empty") or summary.get("corrupt"):
                self.toast("Nothing to load in slot %d." % slot, BAD, 2.5)
                self.sfx("error")
            else:
                self.load_run(slot)
        elif event.key == pygame.K_ESCAPE:
            if self.ui.slot_mode == "veteran":
                # **Backing out is an answer here**, not a cancellation:
                # you are already in somebody\'s room and you still need a
                # hero. Declining the import is choosing to start fresh.
                self.sfx("back")
                self.begin_guest_creation(self.guest_pending)
                return
            # Backing out of the picker cancels hosting too - an intent
            # that outlives the screen that set it is a trap.
            self.host_when_ready = False
            self.go(TITLE)
            self.sfx("back")

    def begin_new_run_setup(self, slot):
        self.slot = slot
        self.pending_class = classes_mod.DEFAULT_CLASS
        self.pending_modifiers = RunModifiers()
        self.pending_ngplus = 0
        self.pending_appearance = Appearance()
        self.ui.class_index = 0
        self.go(CLASS_SELECT)

    def confirm_overwrite(self, slot, summary):
        """Never quietly write over somebody's run."""
        self.dialogue = {
            "title": "Overwrite slot %d?" % slot,
            "body": ["This slot already holds a run:",
                     "",
                     "  %s, level %d - %d of six wardens felled"
                     % (summary.get("name", "?"), summary.get("level", 1),
                        summary.get("bosses", 0)),
                     "  %s played, last saved %s"
                     % (format_playtime(summary.get("playtime", 0.0)),
                        summary.get("updated", "?")),
                     "",
                     "Starting a new game here erases it for good."],
            "confirm": "Erase it and start fresh",
            "on_confirm": lambda: self.begin_new_run_setup(slot),
            "on_cancel": lambda: self.go(SLOTS),
        }
        self.go(DIALOGUE)
        self.sfx("error")

    def event_name(self, event):
        """Character creation. One screen: name, look, and Begin."""
        rows = menus.CREATOR_ROWS
        index = self.ui.creator_index
        label = rows[index]
        field = menus.CREATOR_FIELDS.get(label)

        if event.key in (pygame.K_UP, pygame.K_DOWN):
            step = -1 if event.key == pygame.K_UP else 1
            self.ui.creator_index = (index + step) % len(rows)
            self.sfx("move")
            return
        if event.key in (pygame.K_LEFT, pygame.K_RIGHT) and field:
            self.pending_appearance.cycle(
                field, -1 if event.key == pygame.K_LEFT else 1)
            self.sfx("move")
            return
        if event.key == pygame.K_ESCAPE:
            # Back the way you came, which for a guest is the class list -
            # they were never shown the modifiers (see
            # `after_class_select`), and dropping them there on the way
            # back would be a screen that only exists when you retreat.
            self.go(CLASS_SELECT if self.guest_pending is not None
                    else MODIFIER_SELECT)
            self.sfx("back")
            return
        if event.key == pygame.K_BACKSPACE:
            self.name_buffer = self.name_buffer[:-1]
            self.name_touched = True
            return
        if event.key == pygame.K_RETURN:
            # Only the Begin row starts the run. Confirming a look row cycles
            # it, which is what both a keyboard press and a mouse click on
            # that row should obviously do.
            if label == "Surprise me":
                self.pending_appearance.randomize(rng)
                self.sfx("item")
                return
            if field:
                self.pending_appearance.cycle(field, 1)
                self.sfx("move")
                return
            if label == "Name":
                self.ui.creator_index = (index + 1) % len(rows)
                self.sfx("move")
                return
            if label == "Begin":
                self.begin_run()
            return
        # Any other printable key types into the name, wherever the cursor is.
        if event.unicode and len(self.name_buffer) < sec.MAX_NAME_LEN:
            base = "" if not self.name_touched else self.name_buffer
            candidate = sec.sanitize_name(base + event.unicode, "")
            if candidate:
                self.name_buffer = candidate
                self.name_touched = True
                self.ui.creator_index = 0

    # ------------------------------------------------- run setup screens ---

    def event_class_select(self, event):
        options = classes_mod.CLASSES
        if event.key in (pygame.K_w, pygame.K_UP, pygame.K_a, pygame.K_LEFT):
            self.ui.class_index = (self.ui.class_index - 1) % len(options)
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN, pygame.K_d,
                           pygame.K_RIGHT):
            self.ui.class_index = (self.ui.class_index + 1) % len(options)
            self.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e):
            chosen = options[self.ui.class_index]
            if not classes_mod.is_unlocked(chosen.key, self.meta):
                self.toast(chosen.unlock_hint, BAD, 3.5)
                self.sfx("error")
                return
            self.pending_class = chosen.key
            self.ui.modifier_index = 0
            self.go(self.after_class_select())
            self.sfx("select")
        elif event.key == pygame.K_ESCAPE:
            self.go(SLOTS)
            self.sfx("back")

    def after_class_select(self):
        """What comes after picking a class - and it is not always the same.

        **A guest skips the modifiers** (2026-08-31, s45). Savvydev, playtest
        2: *"when joining someone, you are prompted with the character
        creation menu but you're also prompted with the modifier menu,
        which shouldn't happen"*.

        He is right and it is not a taste question. **A modifier is a
        property of the run, and a guest is not in their own run** - they
        are standing in the host's world, which was rolled with the host's
        modifiers. Offering the list implies a choice that cannot be
        honoured, and half of them (`no_shops`, `always_night`,
        `more_encounters`) describe a world the guest does not own.

        `begin_guest_creation` already sets `pending_modifiers` to an
        empty set; this stops the screen that would fill it in.
        """
        return NAME_ENTRY if self.guest_pending is not None \
            else MODIFIER_SELECT

    def event_mode_select(self, event):
        """Pick a game (0ag, s59)."""
        count = len(gamemodes.MODES)
        if event.key in (pygame.K_w, pygame.K_UP):
            self.ui.mode_index = (self.ui.mode_index - 1) % count
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            self.ui.mode_index = (self.ui.mode_index + 1) % count
            self.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE):
            self.choose_mode(gamemodes.MODES[self.ui.mode_index].key)
        elif event.key == pygame.K_ESCAPE:
            self.host_when_ready = False
            self.go(TITLE)
            self.sfx("back")

    def waves_waiting_for(self):
        """Who is still reading their cards (item 0bs, s84).

        **Names**, for the same reason `wardens_missing` names people:
        *"not everybody is ready"* without saying who is a message you
        cannot act on. Empty solo, empty for a guest - a guest's button
        already says it is waiting for the host - and empty once the
        host's patience runs out, because a wait that cannot expire is a
        room somebody has to restart.
        """
        if self.coop is None or self.coop.role != coop_mod.HOST:
            return []
        return self.coop.still_choosing()

    def event_waves(self, event):
        if event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE):
            if self.waves_waiting_for():
                # The button says so rather than being one that does
                # nothing (§45): pressing it is not an error, it is just
                # not the moment.
                self.sfx("back")
                return
            self.begin_wave()
        elif event.key == pygame.K_ESCAPE:
            self.leave_waves()
            self.sfx("back")

    def event_roam(self, event):
        """Keys that are *pressed* rather than held (item 0ak).

        Walking, swinging and dodging are read every frame from the
        keyboard's state in `update_roam` - a fight you have to tap your
        way across is not free-roam. What is left for a keypress is the
        things that happen **once**: choosing a skill to hold, and
        leaving.
        """
        if event.key == pygame.K_ESCAPE:
            self.ask_leave_roam()
            return
        if self.roam is None or self.player is None:
            return
        numbers = (pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4)
        if event.key in numbers:
            self.hold_roam_skill(numbers.index(event.key))

    def hold_roam_skill(self, index):
        """Hold skill `index` for the next swing.

        **One door, because the chips are clickable** (§92). The keys
        1-4 had this to themselves and `freeroamscreen` drew a
        `"roamskill"` hotspot for every chip - so the chips lit under the
        mouse and did nothing when pressed, in the two modes that have no
        other menu. Rule 8b, in the modes written last.
        """
        if self.roam is None or self.player is None:
            return False
        skills = self.player.skills()
        index = int(index)
        if not 0 <= index < len(skills):
            return False
        if self.roam.mirror:
            # **A guest asks; it does not decide.** The host spends the
            # MP and starts the cooldown, so a client that charged
            # locally would show a skill it had not paid for.
            self.roam_pending_skill = skills[index].key
            self.sfx("select", volume=0.6)
            return True
        self.roam.charge(self.roam.viewer, skills[index])
        return True

    def event_wave_over(self, event):
        if event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE,
                         pygame.K_ESCAPE):
            self.clear_run()
            self.go(TITLE)
            self.sfx("back")

    def open_mode_select(self):
        self.ui.mode_index = 0
        self.go(MODE_SELECT)

    def choose_mode(self, key):
        """Answer the picker, and go wherever that mode starts.

        **The two modes diverge here and nowhere else.** A story run needs
        a slot, so it goes to the slot picker; a wave run has none, so it
        goes straight to the class list. Everything after that - class,
        modifiers, difficulty, name - is one path for both.
        """
        self.pending_mode = key
        self.sfx("select")
        if gamemodes.mode_for(key).saves:
            self.go_slots("coop" if self.host_when_ready else "new")
            return
        # No slot: nothing to pick, nothing to overwrite.
        self.slot = 0
        self.pending_class = classes_mod.DEFAULT_CLASS
        self.pending_modifiers = RunModifiers()
        self.pending_ngplus = 0
        self.pending_appearance = Appearance()
        self.ui.class_index = 0
        self.go(CLASS_SELECT)

    def arena_row_shown(self):
        """Whether this run's mode has an arena to choose (item 0bq).

        **Every mode that is not on the island is fought in one**, which
        is what `Mode.island` already says - so this asks rather than
        keeping a third list of the same modes (§88).
        """
        return not gamemodes.mode_for(self.pending_mode).island

    def open_arena_select(self):
        """Open the picker (item 0br), with the cursor on what is set."""
        self.ui.arena_index = (int(self.settings.arena_pick)
                               if self.settings.arena_mode == "one"
                               else len(gamemodes.ARENAS)
                               + (0 if self.settings.arena_mode == "random"
                                  else 1))
        self.go(ARENA_SELECT)
        self.sfx("select")

    def choose_arena(self, index):
        """Take whatever the cursor is on.

        **One setting, three ways in** - this screen, the Settings rows
        and the run-setup row all write the same `Settings` fields, the
        call `cycle_difficulty` made and the reason there is nothing to
        drift.
        """
        total = len(gamemodes.ARENAS)
        if 0 <= index < total:
            self.settings.arena_mode = "one"
            self.settings.arena_pick = index
        elif index == total:
            self.settings.arena_mode = "random"
        else:
            if self.settings.arena_mode == "every":
                cadences = list(gamemodes.ARENA_CADENCES)
                here = (cadences.index(self.settings.arena_every)
                        if self.settings.arena_every in cadences else 0)
                self.settings.arena_every = cadences[
                    (here + 1) % len(cadences)]
            else:
                self.settings.arena_mode = "every"
        self.sfx("select")

    def event_arena_select(self, event):
        """The picker's keys. A grid, so Left and Right are not the same
        as Up and Down - which is the one thing a list screen's handler
        cannot be reused for."""
        total = len(gamemodes.ARENAS)
        count = total + 2
        # **The same grid the screen draws** (item 0bw). This measured
        # the columns from the *window* and `draw_arena_select` measures
        # them from the framed panel inside it - 120 pixels narrower - so
        # at 1280 the keyboard moved five at a time across a grid four
        # wide. Down from the top left landed two cells along on the next
        # row, and on a paged grid the page jumped with it.
        #
        # His: *"the second page of the maps screen is super buggy"*. One
        # measurement, asked for in one place.
        _cells, cols, _page = menus.arena_grid(self, menus.frame_rect(self))
        index = self.ui.arena_index
        if event.key == pygame.K_ESCAPE:
            self.go(MODIFIER_SELECT)
            self.sfx("back")
            return
        if event.key in (pygame.K_a, pygame.K_LEFT):
            index -= 1
        elif event.key in (pygame.K_d, pygame.K_RIGHT):
            index += 1
        elif event.key in (pygame.K_w, pygame.K_UP):
            index = index - cols if index >= cols else index
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            index = index + cols if index + cols < count else index
        elif event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE):
            self.choose_arena(index)
            return
        else:
            return
        self.ui.arena_index = int(clamp(index, 0, count - 1))
        self.sfx("move")

    def cycle_arena_choice(self, step):
        """Walk the arena setting from the run-setup screen.

        **One implementation, two screens**, the call `cycle_difficulty`
        already made: this is the same `Settings` fields the options
        screen edits, so a choice made in either place is the same
        choice and there is no second copy to drift.
        """
        s = self.settings
        modes = gamemodes.ARENA_MODES
        if s.arena_mode == "one" and step:
            nxt = s.arena_pick + step
            if 0 <= nxt < len(gamemodes.ARENAS):
                s.arena_pick = nxt
                self.sfx("move")
                return
        if s.arena_mode == "every" and step:
            # Inside "changes", the arrows walk the cadence before they
            # leave the mode - so 2, 5 and 10 are reachable without
            # anybody finding a second row for them.
            cadences = list(gamemodes.ARENA_CADENCES)
            here = (cadences.index(s.arena_every)
                    if s.arena_every in cadences else 0)
            nxt = here + step
            if 0 <= nxt < len(cadences):
                s.arena_every = cadences[nxt]
                self.sfx("move")
                return
        idx = modes.index(s.arena_mode) if s.arena_mode in modes else 0
        s.arena_mode = modes[(idx + (step or 1)) % len(modes)]
        self.sfx("move")

    def event_modifier_select(self, event):
        # Row 0 is the difficulty, then - in a wave mode - the arena,
        # then the modifiers, then Begin.
        extra = 1 if self.arena_row_shown() else 0
        count = len(modifiers_mod.MODIFIERS) + 2 + extra
        if event.key in (pygame.K_w, pygame.K_UP):
            self.ui.modifier_index = (self.ui.modifier_index - 1) % count
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            self.ui.modifier_index = (self.ui.modifier_index + 1) % count
            self.sfx("move")
        elif event.key in (pygame.K_a, pygame.K_LEFT,
                           pygame.K_d, pygame.K_RIGHT) \
                and self.ui.modifier_index in (0, 1 if extra else -1):
            step = -1 if event.key in (pygame.K_a, pygame.K_LEFT) else 1
            if self.ui.modifier_index == 0:
                self.cycle_difficulty(step)
            else:
                self.cycle_arena_choice(step)
        elif event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE):
            if self.ui.modifier_index == 0:
                self.cycle_difficulty(1)
            elif extra and self.ui.modifier_index == 1:
                # **Enter opens the picker; the arrows still cycle.**
                # A row that only cycles is the version he asked to be
                # replaced (0br), and a row that only opens loses the
                # quick change - so it does both.
                self.open_arena_select()
            elif self.ui.modifier_index >= (len(modifiers_mod.MODIFIERS)
                                            + 1 + extra):
                self.name_buffer = "Wanderer"
                self.name_touched = False
                self.go(NAME_ENTRY)
                self.sfx("select")
            else:
                key = modifiers_mod.MODIFIERS[
                    self.ui.modifier_index - 1 - extra].key
                self.pending_modifiers.toggle(key)
                self.sfx("move")
        elif event.key == pygame.K_ESCAPE:
            self.go(CLASS_SELECT)
            self.sfx("back")

    def cycle_difficulty(self, step):
        """Move the run's difficulty one notch, at creation time.

        The same setting the Settings screen owns - there is one
        difficulty, not a per-screen copy of it - and it stays changeable
        afterwards. What changed on 2026-09-01 is that a new run now
        *says* what it is starting on rather than inheriting the last
        answer silently.
        """
        # `Settings.cycle_difficulty` is the one implementation, the same
        # one the Settings screen calls - there is a single difficulty, not
        # a copy of it per screen.
        #
        # **What it may land on depends on the mode being started** (item
        # 0bb): a Last Stand does not offer `story`.
        self.settings.cycle_difficulty(
            step, config_mod.difficulties_for(self.pending_mode))
        self.sfx("move")

    #: What "start this run" means, per mode. Every mode that does not
    #: live in a save slot needs an entry here; one that does not have
    #: one silently becomes a story run (§86).
    RUN_STARTERS = {
        "waves": "new_wave_run",
        "zombies": "new_night_run",
        "hoard": "new_hoard_run",
    }

    def begin_run(self):
        name = sec.sanitize_name(self.name_buffer)
        self.sfx("select")
        # The one-shot decides which kind of hero this is. Consumed inside
        # `new_guest_run`, so backing out of the chain cannot leave the game
        # quietly making guest heroes later.
        if self.guest_pending is not None:
            seed = self.guest_pending
            if getattr(self, "wave_guest", False):
                self.fade.out(lambda: self.new_wave_guest_run(name, seed))
                return
            self.fade.out(lambda: self.new_guest_run(name, seed))
            return
        # **A table, not a branch** (§86). This read `== "waves"` and
        # every other mode fell through to `new_run` - so choosing **The
        # Long Night** in the picker started a *story* run, with the
        # prologue, for as long as that mode has existed. Every test
        # called `new_night_run` directly, so nothing ever walked the
        # route a player walks: the mode was built, tested and shipped
        # with no way into it.
        starter = self.RUN_STARTERS.get(getattr(self, "pending_mode",
                                                "story"))
        if starter is not None:
            self.fade.out(lambda: getattr(self, starter)(name))
            return
        self.fade.out(lambda: self.new_run(name, self.slot))

    def creator_cycle(self, row_index, step):
        """Nudge one look field, used by the on-screen arrows."""
        label = menus.CREATOR_ROWS[row_index]
        field = menus.CREATOR_FIELDS.get(label)
        if not field:
            return False
        self.ui.creator_index = row_index
        self.pending_appearance.cycle(field, step)
        self.sfx("move")
        return True

    # ----------------------------------------------------- codex, records ---

    def event_codex(self, event):
        if self.reading is not None:
            if event.key in (pygame.K_ESCAPE, pygame.K_RETURN, pygame.K_e):
                self.reading = None
                self.sfx("back")
            return
        entries = menus.codex_rows(self)
        if event.key == pygame.K_r:
            self.open_story_record()
            return
        if event.key in (pygame.K_w, pygame.K_UP):
            self.ui.codex.move(-1, len(entries))
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            self.ui.codex.move(1, len(entries))
            self.sfx("move")
        elif event.key == pygame.K_TAB:
            step = -1 if event.mod & pygame.KMOD_SHIFT else 1
            self.ui.codex_tab = (self.ui.codex_tab + step) % len(
                story.CHAPTERS)
            self.ui.codex.index = self.ui.codex.offset = 0
            self.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e) and entries:
            entry, known = entries[self.ui.codex.index]
            if known:
                self.reading = entry
                self.sfx("select")
            else:
                self.sfx("error")
        elif event.key == pygame.K_ESCAPE:
            self.go(PAUSE if self.player else TITLE)
            self.sfx("back")

    def event_guide(self, event):
        """Left and Right turn the page; Up and Down read on.

        **Up and Down used to turn the page too**, which was fine while a
        page was whatever fitted and is wrong now that one can be twice
        the window (`menus.guide_measure`). They fall through to turning
        when there is nothing to scroll, so a page that fits behaves
        exactly as it always did - the same contract `scroll_scene` has.
        """
        from . import guide
        count = len(guide.PAGES)
        if event.key in (pygame.K_UP, pygame.K_w, pygame.K_DOWN, pygame.K_s):
            step = -1 if event.key in (pygame.K_UP, pygame.K_w) else 1
            if self.scroll_guide(step):
                return
            self.ui.guide_index = (self.ui.guide_index + step) % count
            self.ui.guide_scroll = 0
            self.sfx("move")
        elif event.key in (pygame.K_LEFT, pygame.K_a):
            self.ui.guide_index = (self.ui.guide_index - 1) % count
            self.ui.guide_scroll = 0
            self.sfx("move")
        elif event.key in (pygame.K_RIGHT, pygame.K_d):
            self.ui.guide_index = (self.ui.guide_index + 1) % count
            self.ui.guide_scroll = 0
            self.sfx("move")
        elif event.key == pygame.K_ESCAPE:
            self.go(PAUSE if self.player else TITLE)
            self.sfx("back")

    def scroll_guide(self, step):
        """Read on, or back. False when the whole page already fits."""
        limit = menus.guide_scroll_max(self)
        if limit <= 0:
            return False
        before = int(getattr(self.ui, "guide_scroll", 0))
        self.ui.guide_scroll = int(clamp(before + step, 0, limit))
        if self.ui.guide_scroll != before:
            self.sfx("move")
        return True

    def event_keybinds(self, event):
        from . import keybinds as kb
        state = self.ui
        if state.rebinding is not None:
            # Every key press is a candidate binding, so Esc has to mean
            # "cancel this rebind" rather than "leave the screen".
            action = state.rebinding
            state.rebinding = None
            if event.key == pygame.K_ESCAPE:
                state.keybind_notice = "Cancelled."
                self.sfx("back")
                return
            ok, message = self.binds.rebind(action, event.key)
            state.keybind_notice = message
            self.sfx("select" if ok else "error")
            if ok:
                self.meta.keybinds = self.binds.to_dict()
                self.meta.save()
            return

        rows = kb.visible_actions(self.meta.found_devmenu)
        count = len(rows)
        if event.key in (pygame.K_UP, pygame.K_w):
            state.keybind_index = (state.keybind_index - 1) % count
            self.sfx("move")
        elif event.key in (pygame.K_DOWN, pygame.K_s):
            state.keybind_index = (state.keybind_index + 1) % count
            self.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e):
            state.rebinding = rows[state.keybind_index][0]
            state.keybind_notice = ""
            self.sfx("select")
        elif event.key == pygame.K_r:
            self.binds.reset()
            self.meta.keybinds = self.binds.to_dict()
            self.meta.save()
            state.keybind_notice = "Controls reset to defaults."
            self.sfx("back")
        elif event.key == pygame.K_ESCAPE:
            self.go(SETTINGS_SCREEN)
            self.sfx("back")

    def event_achievements(self, event):
        from .quests import ACHIEVEMENTS
        state = self.ui.feats
        if event.key in (pygame.K_w, pygame.K_UP):
            state.move(-1, len(ACHIEVEMENTS))
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            state.move(1, len(ACHIEVEMENTS))
            self.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_ESCAPE):
            self.go(TITLE)
            self.sfx("back")

    def event_records(self, event):
        if event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_ESCAPE):
            if self.ending:
                # **An ending leaves the room and the run, the way "Back
                # to title" does** (§40, §45; s96). This cleared the hero
                # and the world and nothing else, so an ending reached in
                # a room came back to a title with the socket still open
                # - going back in from there is §40's bug, somebody
                # else's valley. `finish_ending` has already saved; the
                # slot is kept, not spent (2026-08-24).
                self.ending = None
                self.leave_coop()
                self.clear_run()
            self.go(TITLE)
            self.sfx("back")

    # -------------------------------------------------------- the endings ---

    def event_ending_choice(self, event):
        options = self.ending_options
        if not options:
            return
        if event.key in (pygame.K_w, pygame.K_UP):
            self.ui.ending_index = (self.ui.ending_index - 1) % len(options)
            self.sfx("move")
        elif event.key in (pygame.K_s, pygame.K_DOWN):
            self.ui.ending_index = (self.ui.ending_index + 1) % len(options)
            self.sfx("move")
        elif event.key in (pygame.K_RETURN, pygame.K_e):
            self.choose_ending(options[self.ui.ending_index])

    def event_ending_text(self, event):
        if event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_SPACE):
            self.finish_ending()
            self.sfx("select")

    def event_overworld(self, event):
        action = self.binds.action_for(event.key)
        if action == "map" or (event.key == pygame.K_m and action is None):
            # The bind is the source of truth; the bare `K_m` stays as a
            # fallback for a save whose binds predate the action existing.
            self.open_world_map()
            return
        if action == "interact":
            self.interact()
        elif action == "bag":
            self.go(INVENTORY)
            self.sfx("select")
        elif action == "hero":
            self.go(CHARACTER)
            self.sfx("select")
        elif action == "quests":
            # Read-only: you can check progress anywhere, but taking on or
            # handing in work means finding Maren or one of her runners.
            self.ui.quest_readonly = True
            self.ui.quest_tab = menus.STORY_TAB
            self.ui.quest.index = self.ui.quest.offset = 0
            self.go(QUESTS)
            self.sfx("select")
        elif action == "guide":
            self.go(GUIDE)
            self.sfx("select")
        elif action == "trade":
            self.start_trade()
        elif action == "save":
            self.save_run()
        elif action == "debug" and self.meta.found_devmenu:
            self.ask_open_devmenu()
        elif event.key == pygame.K_ESCAPE:
            self.go(PAUSE)
            self.sfx("back")

    def scroll_dialogue(self, step):
        """Read on, or back, when a dialogue is taller than the window.

        False when all of it fits, so Left and Right keep meaning whatever
        else they mean - the same contract `scroll_scene` has (s43).
        """
        if not self.dialogue:
            return False
        limit = menus.dialogue_scroll_max(self)
        if limit <= 0:
            return False
        before = int(self.dialogue.get("scroll", 0))
        self.dialogue["scroll"] = clamp(before + step, 0, limit)
        if self.dialogue["scroll"] != before:
            self.sfx("move")
        return True

    def event_dialogue(self, event):
        if event.key in (pygame.K_LEFT, pygame.K_a) \
                and self.scroll_dialogue(-1):
            return
        if event.key in (pygame.K_RIGHT, pygame.K_d) \
                and self.scroll_dialogue(1):
            return
        if event.key in (pygame.K_RETURN, pygame.K_e):
            action = (self.dialogue or {}).get("on_confirm")
            self.dialogue = None
            self.state = OVERWORLD
            self.sfx("select")
            if action:
                action()
        elif event.key == pygame.K_ESCAPE:
            cancel = (self.dialogue or {}).get("on_cancel")
            self.dialogue = None
            self.sfx("back")
            if cancel:
                cancel()
            else:
                self.state = OVERWORLD

    def event_gameover(self, event):
        if event.key in (pygame.K_RETURN, pygame.K_e, pygame.K_ESCAPE):
            self.sfx("select")
            self.fade.out(self.respawn)

    def respawn(self):
        # **A fallen hero does not get up** (s54, Mortal). The slot is kept
        # and written one last time carrying `fallen`, so the picker can
        # say what happened rather than the run quietly vanishing - and
        # under One Sitting that write is refused too, which is the right
        # answer twice over.
        if self.player is not None and getattr(self.player, "fallen", False):
            self.save_run(silent=True, force=True)
            self.end_fallen_run()
            return
        self.player.x = self.world.spawn[0] * self.tile + self.tile / 2
        self.player.y = self.world.spawn[1] * self.tile + self.tile / 2
        self.enter_overworld(snap=True)
        self.save_run(silent=True, force=True)

    def end_fallen_run(self):
        """Mortal, and the hero fell. Record it and go back to the title.

        The profile keeps what a run earns - the codex, the feats, the
        totals - so a Mortal run that ends badly still leaves everything
        s33 says survives a run. What it does not leave is a hero to carry
        on with.
        """
        self.meta.add_totals(self.player.stats_tracker)
        self.meta.save()
        name = self.player.name
        level = self.player.level
        self.player = None
        self.world = None
        self.go(TITLE)
        self.toast("%s fell at level %d. Mortal keeps no second chances."
                   % (name, level), (196, 60, 60), 7.0)

    # -------------------------------------------------------------- endings ---

    def open_ending_choice(self):
        """The Door. One scene, then whatever that answer means.

        The scene is where the hidden ending lives: `door.names` is only on
        the list for somebody who stopped and listened to all six, and
        taking it goes straight to that ending rather than through the
        ordinary menu, which does not know it exists.
        """
        self.pending_ending = False
        if self.scene is None and not self.ui.door_answered:
            self.ui.door_answered = True
            self.play_scene("door", after=self.resolve_door)
            return
        self.resolve_door()

    def at_the_door(self):
        """Is this run ending - from the last warden's fall to the records?

        The one stretch a closing room must not interrupt (s96): the
        last words, the Door, the choice, the ending and its records.
        A replayed Door from the codex is not an ending.
        """
        if self.pending_ending or self.ending is not None:
            return True
        if self.state in (ENDING_CHOICE, ENDING_TEXT):
            return True
        scene = self.scene
        return (self.state == SCENE and scene is not None
                and not scene.get("replay")
                and scene["scene"].key == "door")

    def resolve_door(self):
        """Act on what was said at the Door."""
        taken = self.ui.door_choice
        self.ui.door_choice = ""
        if taken == "names":
            secret = story.ENDINGS_BY_KEY.get("names")
            if secret is not None:
                self.choose_ending(secret)
                return
        self.offer_ending_menu()

    def offer_ending_menu(self):
        self.ending_options = story.offered_endings(
            set(self.meta.codex), self.world.wardens_defeated(),
            self.world.hollow_key)
        # The answers given along the road decide which one is highlighted
        # first, so the ending you have been leaning toward is the one your
        # hand is already on.
        #
        # **The default goes first** (§94). It was written *after* the loop,
        # so the cursor was put on the leaning ending and then straight back
        # on the first one - and mechanism 1 of §33, *"the Door simply
        # starts your cursor on whichever is heaviest"*, has never happened.
        self.ui.ending_index = 0
        leading = scenes_mod.leading_ending(self.player.leanings)
        for i, option in enumerate(self.ending_options):
            if option.key == leading:
                self.ui.ending_index = i
                break
        self.state = ENDING_CHOICE
        self.sfx("portal")

    def choose_ending(self, ending):
        self.ending = ending
        fresh = self.meta.record_ending(ending.key)
        score = self.meta.score_run(
            self.player, self.world.wardens_defeated(),
            self.player.modifiers.score, self.player.ngplus, ending.key)
        record = meta_mod.RunRecord(
            class_key=self.player.class_key, ending=ending.key,
            level=self.player.level, score=score,
            ngplus=self.player.ngplus, playtime=self.playtime,
            modifiers=tuple(self.player.modifiers.to_list()),
            party=self.party_size(),
            when=time.strftime("%Y-%m-%d"))
        self.meta.finish_run(record)
        self.meta.add_totals(self.player.stats_tracker)
        self.meta.save()
        self.last_score = score

        if fresh and ending.unlocks:
            cls = classes_mod.CLASSES_BY_KEY.get(ending.unlocks)
            if cls and classes_mod.is_unlocked(cls.key, self.meta):
                self.toast("New class unlocked: %s" % cls.name,
                           cls.color, 8.0)
        self.state = ENDING_TEXT
        self.ui.ending_scroll = 0
        self.sfx("victory")

    def finish_ending(self):
        """Mark the run finished and keep it.

        **The run is no longer spent.** This used to call
        `saves.delete(self.slot)`: section 33's design was that
        knowledge survives a run and the run does not. Savvydev reversed
        it on 2026-08-24 after the playtest, where finishing the game
        and finding the slot gone read as data loss rather than as the
        point.

        Re-finishing costs nothing and awards nothing: `record_ending`
        only counts an ending the profile has not seen, so walking back
        to the Door cannot farm it.
        """
        if self.player is not None:
            self.player.beaten = True
            self.save_run(silent=True, force=True)
        self.state = RECORDS
        self.ui.records_scroll = 0

    # --------------------------------------------------------------- mouse ---

    # Which cursor field each registered hotspot kind drives. Kinds ending in
    # "_tab" only move the cursor; the rest also confirm, by replaying the
    # same Enter keypress the keyboard would have sent.
    CURSOR_FIELDS = {
        "title": ("title_index", None),
        "class": (None, "class_index"),
        "modifier": (None, "modifier_index"),
        "pause": (None, "pause_index"),
        "setting": (None, "settings_index"),
        "attr": (None, "char_index"),
        "ending": (None, "ending_index"),
        "creator": (None, "creator_index"),
        # `guide` is answered above this table, because a page turn
        # also has to put the scroll back to the top.
        "keybind": (None, "keybind_index"),
        "inv_tab": (None, "inv_tab"),
        "quest_tab": (None, "quest_tab"),
        "codex_tab": (None, "codex_tab"),
        # Registered by their screens but missing from here until
        # 2026-08-23, which meant `point_cursor` returned False and
        # `mouse_click` gave up with a click sound. Play Together looked
        # completely dead to a mouse: hover lit a row, clicking did
        # nothing, and only Esc got you out.
        "lobby": (None, "lobby_index"),
        # The gamemode picker (0ag, s59). `wave` and `waveover` are
        # single buttons and are answered directly in `mouse_click`.
        "mode": (None, "mode_index"),
        "dev": (None, "dev_index"),
        # The full-bag question (s48). Its cursor lives here rather than
        # inside the question itself for exactly the reason this table
        # exists: `TestMouseReach` walks every registered kind, and a
        # cursor kept somewhere private is a row the mouse cannot move to.
        "fullbag": (None, "fullbag_index"),
        # The new-skill question (s53). Same shape as the full-bag one.
        "newskill": (None, "newskill_index"),
        # A cleared wave's offers (items 0bs/0bt).
        "spoil": (None, "spoil_index"),
        # The arena picker (item 0br). **`TestMouseReach` caught this**:
        # the screen registered a hotspot for every thumbnail and the
        # mouse could not move to one, because a kind that is drawn and
        # not in this table is a row that lights under the cursor and
        # does nothing when you click it - which is exactly what Play
        # Together did before 2026-08-23.
        "arena": (None, "arena_index"),
    }
    # A tab and the list it fills. Changing the tab puts that list's
    # cursor back to the top, whichever way it was changed.
    TAB_LISTS = {"inv_tab": "inv", "quest_tab": "quest", "codex_tab": "codex"}
    LIST_FIELDS = {"inv": "inv", "shop": "shop", "smith": "smith",
                   "quest": "quest", "codex": "codex", "slot": "slot",
                   "story": "story", "feat": "feats"}

    # Clicking one of these picks a row and stops there. Spending gold or
    # changing your loadout takes a second, deliberate press of Confirm.
    # Only rows that spend gold, change your gear, or commit to a class.
    # Toggling a modifier or cycling a look is harmless and acts on click.
    #
    # `scene` and `dev` joined on 2026-08-23, when the mouse first reached
    # them at all. Neither spends gold and both are worse than gold: a
    # scene answer leans an ending and cannot be taken back (section 33),
    # and a debug action marks the run permanently (section 16). Rule 2.8
    # is about stray clicks costing something, and those cost more than
    # a potion. **This is a feel decision - section 31 rule 7 - so it is
    # Savvydev's to overrule.**
    # `feat` and `story` are **read-only lists whose Enter means "close"**.
    # They joined on 2026-08-24 after the mouse first reached them at all:
    # a click moved the cursor and replayed Enter, and Enter on those two
    # screens leaves them - so clicking any achievement threw you back to
    # the title. A row you cannot confirm must not be confirmable.
    #
    # `scene` was in here for a day and is **deliberately out again.**
    # Rule 2.8 is about a stray click costing something, and a scene
    # answer leans an ending permanently - but Savvydev playtested it and
    # reported that you simply "cannot choose story options with your
    # mouse", which is the worse failure. A scene screen holds two to
    # four large rows and nothing else clickable, so the stray click that
    # rule 2.8 guards against barely exists here.
    # `story` came off this list when Enter there stopped meaning "close"
    # and started meaning "play it back". A click that confirms is only a
    # trap when confirming costs something (s2.8); watching a scene again
    # costs nothing and is the only thing a mouse can do on that screen.
    #
    # **`dev` came off this list on 2026-09-01.** It was here so that a
    # stray click could not mark a run (s19 decision 14) - and opening the
    # menu marks the run now, on his instruction, so by the time a row can
    # be clicked there is nothing left to protect. What was left was a
    # screen with rows you could select and never press:
    #
    # > "you cant use the menu with your mouse, and like left click and
    # > stuff"
    # **`trade` is here for the same reason `shop` is** (2026-09-03, s50).
    # Nothing moves until both sides lock, but putting a legendary on the
    # table is one press and the row that settles it sits in the same list
    # - so the screen takes the ordinary shape: a click picks a row, the
    # Confirm button commits it. Rule 2.8 exactly, on the one screen in the
    # game where the thing being spent is somebody else's as well as yours.
    SELECT_ONLY = {"inv", "shop", "smith", "class", "battle_target",
                   "feat", "travel", "trade",
                   # **Taking a card spends the other three** (item 0bs).
                   # Nothing is bought, but the choice is as final as a
                   # purchase - so a stray click picks the card up and
                   # Confirm is what takes it, the same shape as gold.
                   "spoil"}

    def point_cursor(self, kind, index):
        """Move the selection to whatever the mouse is over. Never confirms."""
        if kind is None:
            return False
        if kind == "smith_tab":
            self.ui.smith_mode = "refine" if index == 0 else "salvage"
            self.ui.smith.index = self.ui.smith.offset = 0
            return True
        if kind == "shop_tab":
            mode = "buy" if index == 0 else "sell"
            if self.ui.shop_mode != mode:
                self.ui.shop_mode = mode
                self.ui.shop.index = self.ui.shop.offset = 0
            return True
        if kind in self.TAB_LISTS:
            # **A clicked tab starts its list at the top, as Tab does**
            # (s96). It kept the old row: row six of one chapter, clicked
            # over to a chapter of five, was a cursor past the end until
            # the next draw - and an Enter before that draw raised.
            cursor = getattr(self.ui, self.TAB_LISTS[kind])
            if getattr(self.ui, kind) != index:
                setattr(self.ui, kind, index)
                cursor.index = cursor.offset = 0
            return True
        if kind in self.CURSOR_FIELDS:
            game_attr, ui_attr = self.CURSOR_FIELDS[kind]
            if game_attr:
                setattr(self, game_attr, index)
            else:
                setattr(self.ui, ui_attr, index)
            return True
        if kind in self.LIST_FIELDS:
            getattr(self.ui, self.LIST_FIELDS[kind]).index = index
            return True
        if kind == "travel":
            # Select-only: a click picks the destination and Enter goes.
            # Travel is not a spend, but it does pick you up and put you
            # down somewhere else, and a stray click should not.
            self.ui.travel_index = index
            return True
        if kind == "loadout":
            # **A click reaches the pane Tab used to be the only way into**
            # (s53). Savvydev: *"you should be able to use mouse on selecting
            # your skills without having to click tab"*. Moving the cursor
            # here moves the *pane* as well, because a cursor in a pane the
            # keyboard is not pointed at is the invisible selection he was
            # complaining about in the same sentence.
            known = self.player.known_skills() if self.player else []
            if 0 <= index < len(known):
                self.ui.char_pane = "skills"
                self.ui.skill_index = index
                return True
            return False
        if kind == "guide":
            # Clicking a chapter in the rail is a page turn, so it starts
            # at the top of that page like every other page turn does.
            self.ui.guide_index = index
            self.ui.guide_scroll = 0
            return True
        if kind == "trade" and self.trade is not None:
            # The second cursor in the game that lives outside `self.ui`,
            # and for the same reason the scene's does: it belongs to a
            # thing that comes and goes, so closing the trade takes the
            # cursor with it rather than leaving a stale index behind.
            rows = self.trade_rows()
            if 0 <= index < len(rows):
                self.trade.index = index
                return True
            return False
        if kind == "scene" and self.scene:
            # The only cursor in the game that lives in a plain dict.
            # Answering leans an ending permanently (section 33), so it
            # is also in SELECT_ONLY: a click picks the row and Confirm
            # commits it, the same shape as spending gold.
            if 0 <= index < len(self.scene.get("options", ())):
                self.scene["index"] = index
                return True
            return False
        if kind == "battle_menu" and self.battle:
            self.battle.choose_menu(index)
            return True
        if kind == "battle_sub" and self.battle:
            self.battle.sub_index = index
            return True
        if kind == "battle_target" and self.battle:
            if (0 <= index < len(self.battle.enemies)
                    and self.battle.enemies[index].alive):
                self.battle.target = index
                return True
            return False
        return False

    def mouse_move(self, pos):
        """Track what is under the pointer. Hovering never selects anything.

        Moving the cursor on hover meant an accidental mouse nudge could
        silently retarget a keyboard confirm. Hover only lights the row; the
        left button is what commits.
        """
        kind, index = self.ui.hot.at(pos)
        current = (kind, index)
        if current != self.hover:
            self.hover = current
            if kind is not None and kind != "key":
                self.sfx("move", volume=0.25, throttle=0.06)

    def mouse_click(self, pos, button=1):
        if button == 3:
            # **Right-click asks what a thing is.** Today that is skills on
            # the hero sheet (2026-08-31); it is deliberately a separate
            # verb from left-click, which selects, so nothing that spends
            # or equips can happen by accident (hard rule 2.8).
            self.right_click(pos)
            return
        if button != 1:
            return
        self.ui.skill_detail = None
        self.ui.gear_detail = None
        if self.skip_splash() or self.skip_chapter_card():
            return
        kind, index = self.ui.hot.at(pos)
        if self.state == OVERWORLD and kind is None:
            # **The world only gets the click nothing else wanted**
            # (2026-08-31). This used to hand every overworld click
            # straight to the world, so the footer along the bottom and
            # the co-op chips down the side were drawn, registered, and
            # unclickable - *"you also aren't able to click the tooltips
            # with your mouse to open whatever they're meant for"*.
            #
            # Hard rule 2.6 is untouched: a chip replays its key, and a
            # click on the world still only interacts when you are next
            # to the thing. Neither one walks you anywhere.
            self.click_world(pos)
            return
        if kind is None:
            return
        if kind == "command":
            # An action command (s67/s68). Answered here rather than
            # through `point_cursor` for the same reason the wave buttons
            # are: there is no cursor, there is one target, and the answer
            # is *when* you pressed rather than *what* you picked.
            if self.battle is not None:
                self.battle.time_command()
            return
        if kind in ("wave", "waveover"):
            # One button apiece, so there is no cursor to move.
            if kind == "wave":
                self.begin_wave()
            else:
                self.clear_run()
                self.go(TITLE)
            return
        if kind == "roamskill":
            # The chips at the bottom of a free-roam fight (§92).
            self.hold_roam_skill(index)
            return
        if kind == "spoil_detail":
            # **Clicking the reader closes it**, which is what a panel
            # sitting over the thing you were looking at should do - and
            # it is the left-click half of the right-click that opened
            # it (item 0bs).
            self.ui.spoil_detail = None
            self.sfx("back")
            return
        if kind == "dev_amount":
            # A preset chip in the debug menu (0ah, s58). The index *is*
            # the amount - there is no cursor to move, so this is answered
            # here rather than through `point_cursor`.
            devmenu_mod.set_amount(self, index)
            self.ui.dev_typing = False
            self.sfx("move")
            return
        if kind in ("creator_prev", "creator_next"):
            self.creator_cycle(index, -1 if kind == "creator_prev" else 1)
            return
        if kind == "gear":
            # **Left-click opens the card too** (s53, rule 8b). Inspecting
            # worn gear was right-click only, so on the one screen the
            # loadout bug was found on there was a *second* row nothing
            # happened when you clicked. Nobody reported this one; the
            # audit did. It is the same toggle either button, because the
            # card is all there is to do here and a second right-click
            # already closed it.
            self.right_click(pos)
            return
        if kind == "key":
            # A footer chip: do exactly what pressing that key does.
            self.handle_event(pygame.event.Event(
                pygame.KEYDOWN, key=index, unicode="", mod=0))
            return
        if kind in ("dialogue_ok", "dialogue_back"):
            code = (pygame.K_RETURN if kind == "dialogue_ok"
                    else pygame.K_ESCAPE)
            self.handle_event(pygame.event.Event(
                pygame.KEYDOWN, key=code, unicode="", mod=0))
            return
        if kind == "confirm":
            self.handle_event(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_RETURN, unicode=chr(13), mod=0))
            return
        moved = self.point_cursor(kind, index)
        if not moved or kind.endswith("_tab") or kind in self.SELECT_ONLY:
            self.sfx("move")
            return
        # Confirm through the ordinary keyboard path, so there is exactly one
        # implementation of what every row does.
        self.handle_event(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_RETURN, unicode=chr(13), mod=0))

    #: Screens that own a scrolling list, and the window object that
    #: knows where it is scrolled to. Everything **not** in here still
    #: answers the wheel - see `mouse_scroll`.
    def scroll_windows(self):
        return {INVENTORY: self.ui.inv, SHOP: self.ui.shop,
                SMITH: self.ui.smith, QUESTS: self.ui.quest,
                CODEX: self.ui.codex, SLOTS: self.ui.slot}

    def scroll_length(self):
        """How many rows the list under the wheel has - each screen's
        own handler counts them the same way. None when there is nothing
        to count them from."""
        state, ui = self.state, self.ui
        if state == SLOTS:
            return SLOT_COUNT
        if state == CODEX:
            return len(menus.codex_rows(self))
        if self.player is None:
            return None
        if state == INVENTORY:
            if ui.inv_tab == 4:
                return len(menus.consumable_rows(self.player))
            return len(menus.filtered_inventory(self.player, ui.inv_tab))
        rows = {SHOP: menus.shop_rows, SMITH: menus.smith_rows,
                QUESTS: menus.quest_rows}
        return len(rows[state](self))

    def mouse_scroll(self, amount):
        """The wheel moves whatever the arrow keys move.

        **Item 0bc, and it is rule 8b's fourth instance.** Savvydev:
        *"you cant use the scroll wheel in the modifier menu, but you
        should be able to, again its one of those industry
        standard/common sense things"*.

        The old version was a **six-entry table**, so the wheel worked on
        the six screens somebody had thought about and did nothing on the
        other twenty - the modifier picker, class select, the mode
        picker, the guide, the hero sheet, the pause menu, every one of
        them. Adding a seventh entry is how this gets reported a fifth
        time.

        **So the fallback is the rule instead:** a notch is Up or Down,
        replayed through the same handler a keypress goes through. That
        is hard rule 2.9 - one selection model - which is already why a
        *click* replays Enter rather than having a second code path.
        Anything that answers the arrow keys now answers the wheel, and
        anything that does not is unchanged.
        """
        if not amount:
            return
        target = self.scroll_windows().get(self.state)
        if target is not None:
            target.offset = max(0, target.offset - amount)
            target.index = int(clamp(target.index - amount, 0, 10 ** 6))
            # **Inside the list, now, not at the next draw** (s96). The
            # draw clamps the cursor and every handler trusts it had: a
            # notch past the end and an Enter in the same frame - one
            # flick of a trackpad - read the codex one row past its last
            # and raised. The UI fuzzer found it at wheel, wheel, Enter.
            count = self.scroll_length()
            if count is not None:
                target.clamp_to(count)
            self.sfx("move", volume=0.4, throttle=0.03)
            return
        # **One notch is one row, however far the wheel was rolled.** A
        # trackpad sends large `y` values and a list that jumps eleven
        # rows on one flick is worse than one that does not move.
        code = pygame.K_UP if amount > 0 else pygame.K_DOWN
        for _ in range(min(3, abs(int(amount)) or 1)):
            self.handle_event(pygame.event.Event(
                pygame.KEYDOWN, key=code, unicode="", mod=0))

    def right_click(self, pos):
        """Ask what something is, without selecting it."""
        kind, index = self.ui.hot.at(pos)
        if kind in ("spoil", "spoil_detail"):
            # **His own words for this screen** (item 0bs): *"each when
            # right clicked open a small screen that says what each
            # does"*. The same toggle gear and skills already use, so a
            # second right-click closes it.
            want = None if kind == "spoil_detail" else int(index)
            self.ui.spoil_detail = (None if self.ui.spoil_detail == want
                                    else want)
            self.sfx("move")
            return
        if kind == "gear":
            # Toggling means the same click closes it, which is what a
            # second right-click on an open card should obviously do.
            self.ui.gear_detail = (None if self.ui.gear_detail == index
                                   else index)
            self.ui.skill_detail = None
            self.sfx("move")
            return
        if kind == "loadout" and self.state == CHARACTER:
            known = self.player.known_skills() if self.player else []
            if 0 <= index < len(known):
                self.ui.skill_detail = (None if self.ui.skill_detail == index
                                        else index)
                self.sfx("move")
                return
        self.ui.skill_detail = None

    def click_world(self, pos):
        """Clicking the world interacts. Walking stays on the keyboard.

        Deliberately no click-to-move: mouse-driven pathing fights the
        keyboard for control of the same character and neither ends up
        feeling precise.
        """
        ox, oy = self.camera.offset
        gx = int((pos[0] + ox) // self.tile)
        gy = int((pos[1] + oy) // self.tile)
        px, py = self.player_tile()
        if max(abs(gx - px), abs(gy - py)) <= 1:
            self.interact()

    def rebind_save_dir(self):
        """Point the save manager and profile at the configured folder."""
        store = config_mod.open_save_store(self.settings.save_dir)
        self.saves = SaveManager(store)
        self.guests = GuestVault(store)
        self.meta = meta_mod.load_profile(store)
        self.binds = Keybinds(self.meta.keybinds)

    def toggle_skill(self, key):
        """Carry an ability, or put it down (item 13)."""
        from .entities import LOADOUT_SIZE
        from .skills import SKILLS_BY_KEY
        carried = key in {s.key for s in self.player.skills()}
        if self.player.equip_skill(key):
            self.toast("%s %s." % (SKILLS_BY_KEY[key].name,
                                   "put down" if carried else "equipped"),
                       ACCENT, 2.0)
            self.sfx("select")
        else:
            self.toast("You can carry %d. Put one down first."
                       % LOADOUT_SIZE, BAD, 2.5)
            self.sfx("error")

    def ask_erase_everything(self):
        """Offer to wipe the lot, and say exactly what "the lot" is.

        Savvydev, 2026-09-01: *"add a setting where you reset ALL of your
        data, with a confirmation button when you click it and
        everything"*.

        **The confirmation says what will be gone, counted.** "Are you
        sure?" is not a confirmation, it is a speed bump; a player who is
        about to lose six runs and a codex should be told it is six runs
        and a codex.
        """
        slots = [s for s in self.saves.describe_all() if not s.get("empty")]
        found = len(self.meta.codex)
        body = ["This erases everything this game has ever saved:", ""]
        body.append("  %d save%s" % (len(slots), "" if len(slots) == 1
                                     else "s"))
        body.append("  %d codex page%s, %d achievement%s"
                    % (found, "" if found == 1 else "s",
                       len(self.meta.achievements),
                       "" if len(self.meta.achievements) == 1 else "s"))
        body.append("  every ending, record and heroes left in other")
        body.append("  people's valleys")
        body += ["", "There is no undo. Settings are kept."]
        self.dialogue = {
            "title": "Erase everything",
            "body": body,
            "confirm": "Erase it all",
            "on_confirm": self.erase_everything,
            "on_cancel": lambda: self.go(SETTINGS_SCREEN),
        }
        self.go(DIALOGUE)

    def erase_everything(self):
        """Delete every save, the profile, and every guest hero.

        **Through the `Store`, not the filesystem.** A save is a key in a
        store and may not be a file at all (s26) - doing this with `os`
        would work on a desktop and quietly do nothing in a browser.
        """
        self.leave_coop()
        self.clear_run()
        removed = 0
        for store in (self.saves.store, self.guests.store):
            for key in list(store.keys()):
                try:
                    store.delete(key)
                    removed += 1
                except Exception:                         # noqa: BLE001
                    pass
        # The profile in memory has to go too, or the next save writes it
        # all back out again.
        self.meta = meta_mod.MetaProfile(self.meta.store)
        self.meta.save()
        self.quests = QuestLog()
        self.log.event("erased_everything", keys=removed)
        self.toast("Everything is gone. %d file%s removed."
                   % (removed, "" if removed == 1 else "s"), BAD, 6.0)
        self.go(TITLE)

    def ask_respec(self):
        """Offer to take every attribute point back, for gold.

        Savvydev asked for this on 2026-08-24: "allow it, all points respec at
        once, at a cost". All at once rather than one at a time, because a
        point you can quietly un-spend was never a decision.

        Behind a confirmation because it spends gold - hard rule 2.8 - and
        because it throws a build away. That rule exists for both reasons
        and this trips both at the same time.
        """
        player = self.player
        if player is None:
            return
        cost = player.respec_cost()
        if cost <= 0:
            self.toast("Nothing is spent yet.", DIM)
            self.sfx("error")
            return
        if player.gold < cost:
            self.toast("You need %d gold to start over." % cost, BAD)
            self.sfx("error")
            return
        self.dialogue = {
            "title": "Start over",
            "body": ["Every attribute point comes back at once, and you",
                     "spend them again from nothing.",
                     "",
                     "%d points spent." % player.spent_points(),
                     "%d gold, and you have %d." % (cost, player.gold)],
            "confirm": "Start over",
            "on_confirm": self.do_respec,
            "on_cancel": lambda: self.go(CHARACTER),
        }
        self.go(DIALOGUE)

    def do_respec(self):
        """The confirmed half: charge, then refund."""
        player = self.player
        if player is None:
            return
        cost = player.respec_cost()
        if cost <= 0 or player.gold < cost:
            return                    # changed underneath the prompt
        player.gold -= cost
        moved = player.refund_points()
        self.ui.char_index = 0
        self.toast("%d points are yours again." % moved, GOOD)
        self.sfx("select")
        self.log.event("respec", points=moved, cost=cost)
        self.go(CHARACTER)

    def toggle_fullscreen(self):
        self.settings.fullscreen = not self.settings.fullscreen
        flags = pygame.FULLSCREEN if self.settings.fullscreen else pygame.RESIZABLE
        self.screen = pygame.display.set_mode(
            (self.settings.width, self.settings.height), flags)
        self.camera.resize(*self.screen.get_size())

    def quit_game(self):
        if self.player and self.world:
            self.save_run(silent=True, force=True)
        self.log.event("session_end", playtime=int(self.playtime))
        self.log.close()
        self.running = False

    # --------------------------------------------------------------- draw ---

    def draw(self):
        surface = self.screen
        self.ui.hot.clear()
        if self.state == SPLASH:
            menus.draw_splash(self)
            self.fade.draw(surface)
            pygame.display.flip()
            return
        standalone = (TITLE, SLOTS, NAME_ENTRY, CLASS_SELECT,
                      MODIFIER_SELECT, MODE_SELECT, ARENA_SELECT,
                      RECORDS, ACHIEVEMENTS_SCREEN,
                      STORY, LOBBY, JOIN_CODE, ENDING_TEXT)
        if self.state in standalone or (self.state in (CODEX, GUIDE, KEYBINDS)
                                        and not self.player):
            menus.draw_title_backdrop(self)
            {
                TITLE: menus.draw_title,
                SLOTS: menus.draw_slots,
                NAME_ENTRY: menus.draw_name_entry,
                CLASS_SELECT: menus.draw_class_select,
                MODIFIER_SELECT: menus.draw_modifier_select,
                ARENA_SELECT: menus.draw_arena_select,
                MODE_SELECT: menus.draw_mode_select,
                RECORDS: menus.draw_records,
                ACHIEVEMENTS_SCREEN: menus.draw_achievements,
                STORY: menus.draw_story,
                LOBBY: menus.draw_lobby,
                JOIN_CODE: menus.draw_join_code,
                ENDING_TEXT: menus.draw_ending_text,
                CODEX: menus.draw_codex,
                GUIDE: menus.draw_guide,
                KEYBINDS: menus.draw_keybinds,
            }[self.state](self)
        elif self.state == BATTLE and self.battle:
            draw_battle(self)
        elif self.state == ROAM and self.roam:
            freeroamscreen.draw_free_roam(self)
            self.floats.draw(self.screen, self.assets)
            self.particles.draw(self.screen)
        elif self.state == SPOILS:
            # **Over whatever the mode was already showing.** A night's
            # offer is read standing in the arena and a Last Stand's over
            # the title backdrop, because that is where each mode is.
            if self.roam is not None and gamemodes.is_roam(self.mode):
                freeroamscreen.draw_free_roam(self)
            else:
                menus.draw_title_backdrop(self)
            menus.draw_spoils(self)
        elif self.state in (WAVES, WAVE_OVER):
            # **Over the title backdrop, not the island.** A wave run
            # never shows the world it quietly owns (0ag, s59), and
            # painting one behind the intermission would advertise a
            # place the player cannot go.
            menus.draw_title_backdrop(self)
            (menus.draw_waves if self.state == WAVES
             else menus.draw_wave_over)(self)
        else:
            surface.fill((14, 13, 20))
            if (self.state == DIALOGUE and self.roam is not None
                    and gamemodes.is_roam(self.mode)):
                # **The arena behind the question, not the island** - a
                # scored run never shows the world it quietly owns (§59),
                # and the thing you are deciding about is on screen
                # behind it.
                freeroamscreen.draw_free_roam(self)
            elif self.world and self.player:
                self.draw_overworld()
                self.draw_hud()
                # After the HUD on purpose: drawn before it, the minimap
                # covers the whole top-right corner an arrow points into.
                self.draw_objective_marker(surface)
                self.draw_battle_markers(surface)
            if self.state == INVENTORY:
                menus.draw_inventory(self)
            elif self.state == SHOP:
                menus.draw_shop(self)
            elif self.state == SMITH:
                menus.draw_smith(self)
            elif self.state == CHARACTER:
                menus.draw_character(self)
            elif self.state == QUESTS:
                menus.draw_quests(self)
            elif self.state == PAUSE:
                menus.draw_pause(self)
            elif self.state == SETTINGS_SCREEN:
                menus.draw_settings(self)
            elif self.state == DIALOGUE:
                menus.draw_dialogue(self)
            elif self.state == CODEX:
                menus.draw_codex(self)
            elif self.state == GUIDE:
                menus.draw_guide(self)
            elif self.state == KEYBINDS:
                menus.draw_keybinds(self)
            elif self.state == ENDING_CHOICE:
                menus.draw_ending_choice(self)
            elif self.state == GAMEOVER:
                menus.draw_gameover(self)
            elif self.state == WORLDMAP:
                menus.draw_world_map(self)
            elif self.state == CHAPTER_CARD:
                menus.draw_chapter_card(self)
            elif self.state == DEVMENU:
                devmenu_mod.draw_devmenu(self)
            elif self.state == SCENE:
                menus.draw_scene(self)
            elif self.state == FULLBAG:
                menus.draw_full_bag(self)
            elif self.state == TRADE:
                menus.draw_trade(self)
            elif self.state == NEWSKILL:
                menus.draw_new_skill(self)
        # Toasts last of all, so a message never ends up behind the menu that
        # produced it - "Not finished yet" used to appear under the quest log.
        if self.player and self.state != BATTLE:
            self.toasts.draw(surface, self.assets, surface.get_width() - 24,
                             getattr(self, "toast_anchor", 210))
        # Over everything the game drew, under the fade: the controls are a
        # part of the device rather than a part of the world, but a fade to
        # black should still take them with it.
        self.touch.draw(self)
        self.fade.draw(surface)
        pygame.display.flip()

    # --------------------------------------------------------------- loop ---

    def window_resized(self, width, height):
        """The window was dragged. **Do not call `set_mode` for this.**

        Savvydev, 2026-08-31: *"when i readjust the window size the game just
        closes out bro"*.

        The handler used to call `resize_window` - and therefore
        `pygame.display.set_mode` - on **every** `VIDEORESIZE`. A drag
        emits a stream of them, so a two-second drag asked SDL to rebuild
        the window some hundreds of times, each one inside Windows' modal
        move/size loop. That is a known way to lose an SDL window on
        Windows, and losing the window is the game vanishing with no
        traceback, no crash report and no `session_end` line - which is
        the shape of it: **the log simply stops.**

        A `RESIZABLE` pygame 2 window has *already* resized its own display
        surface by the time this event arrives. There is nothing to set. So
        this adopts the surface, moves the camera, and returns.

        **The minimum size is enforced on the next frame, not here.**
        Fighting the drag while the drag is still going is the same
        `set_mode`-inside-the-modal-loop call by another name;
        `pending_clamp` carries it out to `update`, by which point the user
        has usually let go.
        """
        surface = pygame.display.get_surface()
        if surface is None:
            self.resize_window(max(MIN_WINDOW_W, width),
                               max(MIN_WINDOW_H, height))
            return
        self.screen = surface
        asked_w, asked_h = width, height
        width, height = surface.get_size()
        self.settings.width = width
        self.settings.height = height
        self.camera.resize(width, height)
        # **Either measure being under the minimum earns a clamp.** The
        # surface is the truth on a real window, and the event is the
        # truth on a driver that does not resize surfaces by itself - a
        # dummy one, and a browser canvas. Taking the smaller of the two
        # means neither case is missed.
        if min(asked_w, width) < MIN_WINDOW_W                 or min(asked_h, height) < MIN_WINDOW_H:
            self.pending_clamp = (max(MIN_WINDOW_W, min(asked_w, width)),
                                  max(MIN_WINDOW_H, min(asked_h, height)))

    def apply_pending_clamp(self):
        """Snap a too-small window back, once, between frames."""
        target, self.pending_clamp = self.pending_clamp, None
        if target and not self.settings.fullscreen:
            surface = pygame.display.get_surface()
            if surface is None or surface.get_size() != target:
                self.resize_window(*target)

    def resize_window(self, width, height):
        """Move to a new window size. The only place that does.

        Two callers with two different reasons - somebody dragging the
        desktop window, and a browser window changing under a build that
        has no say in its own canvas size - and hard rule 9's principle
        applies to more than clicking: one implementation, or they drift.
        """
        self.settings.width = width
        self.settings.height = height
        # Keep whichever mode the window is already in. Hardcoding
        # RESIZABLE here would quietly drop a fullscreen player out of
        # fullscreen the first time anything resized.
        flags = (pygame.FULLSCREEN if self.settings.fullscreen
                 else pygame.RESIZABLE)
        self.screen = pygame.display.set_mode((width, height), flags)
        self._enforce_minimum_size()
        self.camera.resize(width, height)

    def _enforce_minimum_size(self):
        """Ask SDL to refuse a window smaller than the game needs.

        The clamp in the resize handler already stops the *surface* going
        below the minimum, but it acts after the fact: the window shrinks,
        the game snaps it back, and the player feels it fight them. SDL can
        simply not allow the drag, which is what a minimum should feel
        like. The clamp stays as the thing that works when this does not.

        **Never fatal, and never in a browser.** `pygame._sdl2` does not
        exist under pygame-web, and a window size is not worth an
        exception. Note this is a lazy import into `pygame._sdl2` - §7's
        trap - so `build/Pixel_RPG.spec` names `pygame._sdl2.video` in
        `hiddenimports` and `--selftest` imports it, or a packaged build
        would silently lose it the way it once lost controllers.
        """
        if self.in_browser or self.settings.fullscreen:
            return
        try:
            from pygame._sdl2 import video
            video.Window.from_display_module().minimum_size = (
                MIN_WINDOW_W, MIN_WINDOW_H)
        except Exception:                                 # noqa: BLE001
            pass          # a window that can be dragged too small is a
                          # cosmetic problem; the clamp still catches it

    # Twice a second. Each check is two calls across the JS bridge, which
    # is not free, and a window that changed size a moment ago is not
    # something anybody notices.
    BROWSER_RESIZE_EVERY = 30

    def follow_browser_window(self):
        """Keep the canvas the size of the browser window.

        pygbag builds a fixed framebuffer and its page template never
        touches the canvas afterwards, so a web build otherwise sits at
        1280x720 in the corner of whatever window it was opened in. On a
        phone that hardly mattered. **The web build is a PC build now**
        (section 37), and a PC window gets dragged about.
        """
        if self.settings.fullscreen:
            # The desktop resize path has always skipped fullscreen.
            # This is the same rule, and without it the poll would
            # fight the player's own choice twice a second.
            return
        size = config_mod.browser_viewport()
        if size and size != (self.settings.width, self.settings.height):
            self.resize_window(*size)

    def tick(self):
        """One frame, and the only thing the two drivers share.

        Pulled out of `run` so that a browser build can drive the same frame
        without a second copy of it. Everything that decides what a frame
        *is* lives here; the drivers below decide only what happens between
        frames.
        """
        dt = min(self.clock.tick(self.settings.fps) / 1000.0, 0.05)
        if self.in_browser:
            self.frame_count += 1
            if self.frame_count % self.BROWSER_RESIZE_EVERY == 0:
                self.follow_browser_window()
        for event in pygame.event.get():
            self.handle_event(event)
        self.update(dt)
        self.draw()
        if self.pad.pending:
            # Deferred to here so the window is already visible: SDL's
            # enumeration blocks for about ten seconds.
            self.pad.pending = False
            gamepad_mod.draw_searching(self)
            self.pad.enable()
            self.clock.tick()
        return self.running

    def run(self):
        """The desktop driver: a plain loop, deliberately not an async one.

        One driver would normally beat two - it is the rule this codebase
        keeps everywhere else. It is not the right call here. Making the
        desktop async would put an event loop and a scheduler under every
        frame for the sole benefit of a platform that does not exist yet,
        and it would change how `KeyboardInterrupt` reaches `main`, which is
        the path that writes a crash log and saves the run.

        The thing that would actually drift between two drivers is the frame
        itself, and the frame is `tick`, and both call it.
        """
        while self.running:
            self.tick()
        pygame.quit()

    async def run_async(self):
        """The browser driver: identical frames, with a yield between them.

        A page that never yields never repaints and never answers input -
        the browser has one thread and this loop would own it. `sleep(0)` is
        the whole difference: it hands control back long enough for the frame
        that `tick` just drew to actually be presented.
        """
        import asyncio
        frames = 0
        while self.running:
            self.tick()
            frames += 1
            if frames in (1, 60):
                # The first frame proves the loop turns at all, and the
                # sixtieth proves it keeps turning. Between them is every
                # way a browser can accept a frame and never show it.
                _trace("frame %d drawn, state=%s" % (frames, self.state))
            await asyncio.sleep(0)
        pygame.quit()


def _trace(message):
    """Startup progress, in a browser only.

    A desktop failure leaves a crash log and a window that did not open.
    A browser failure leaves a grey canvas and nothing else - the WASM
    interpreter is fine, the page is fine, and the only thing wrong is on
    the Python side where no browser tool can see it.

    Silent on the desktop, because the desktop has better tools and this
    would just be noise on a path that runs once.
    """
    if config_mod.is_browser():
        print("[pixel_rpg] %s" % message)


def net_alphabet():
    from .net import ALPHABET
    return ALPHABET


def _after_crash(game, exc):
    """Write the crash down and try to save the run, then let it through.

    Shared by both entry points rather than copied into each, because the
    one thing that must not differ between platforms is what happens when
    something has already gone wrong.
    """
    # The exe is windowed: without this file, the only evidence a crash
    # happened is that the window vanished.
    try:
        where = game.log.crash(
            exc, context="state=%s playtime=%d"
            % (game.state, int(game.playtime)))
        if where:
            print("crash log written to %s" % where)
    except Exception:                                 # noqa: BLE001
        pass
    if game.player and game.world:
        try:
            game.save_run(silent=True, force=True)
        except Exception:                             # noqa: BLE001
            pass
    pygame.quit()


def main():
    """Desktop entry point. Any unexpected crash still tries to save the run."""
    game = Game()
    try:
        game.run()
    except KeyboardInterrupt:
        game.quit_game()
    except Exception as exc:                          # noqa: BLE001
        _after_crash(game, exc)
        raise
    return 0


async def main_async():
    """Browser entry point. The same game, yielding once a frame.

    Not called on the desktop - see `Game.run` for why that is deliberate.
    """
    _trace("constructing Game")
    game = Game()
    _trace("Game built at %dx%d; entering the loop"
           % (game.settings.width, game.settings.height))
    try:
        await game.run_async()
    except Exception as exc:                          # noqa: BLE001
        _after_crash(game, exc)
        raise
    return 0
