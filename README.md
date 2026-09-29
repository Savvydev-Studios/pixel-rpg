# Pixel RPG

Six roads leave the valley. Five of them end at a warden. The sixth ends at
the Door.

A pygame action-RPG with eight classes, a story that turns on what you find out
too late, four endings, and a codex that carries knowledge between runs.
Everything you see and hear is generated from source at runtime — there is not
a single image or sound file in this repository.

---

## Playing it

**Double-click `run.bat`** — or `Pixel_RPG.exe` directly. Nothing to install.
The newest `Pixel_RPG.exe` is on the repository's **Releases** page.

If a `Pixel_RPG.new.exe` ever appears, it is a build made while the game was
open. Close the game and run `update.bat` to swap it in; saves are untouched.

The `.exe` is a single self-contained file with Python, pygame and SDL built
in. Copy the folder anywhere, put it on a USB stick, send it to someone
else's PC — `run.bat` resolves every path relative to itself, so it works
wherever it lands. If the exe is ever missing, `run.bat` falls back to running
from source.

### On a Chromebook (or any Linux)

**Nothing to install but the game.** Python, pygame and SDL are built into
it, the same as the Windows exe.

1. Turn on Linux: **Settings > About ChromeOS > Developers > Linux
   development environment**. School-managed Chromebooks usually have this
   switched off; if the button is missing or greyed out, see *In a
   browser* below instead.
2. From the repository's **Releases**, download the `.deb` for your
   Chromebook - `amd64` for Intel and AMD, `arm64` for ARM (MediaTek,
   Snapdragon).
3. Double-click it in the Files app and choose **Install**. Pixel RPG
   appears in the app launcher.

Saves live in `~/.local/share/Pixel_RPG`, so installing a newer `.deb`
keeps every save. Any other Linux: `sudo apt install ./pixel-rpg_*.deb`.

**From source instead** (for working on it): download the code, copy the
folder into **Linux files**, and in the **Terminal** run `bash run.sh` -
the first run installs pygame into `.venv`. Double-clicking `run.sh` in
the Files app only opens it in the browser as text.

### In a browser

`python tools/build_web.py` makes `build/web`, a page version of the game
that any browser can open once it is on a web host - which is how a
Chromebook without Linux plays it. Co-op does not work from a hosted page.
See HANDOFF section 39.

From source instead:

```bash
pip install -r requirements.txt
python main.py
```

### Controls

**Overworld** — `WASD`/arrows move, `Shift` sprints, `E` interacts,
`M` map, `I` bag, `C` hero sheet, `J` quests, `G` guide, `T` trades with
whoever you are standing next to, `F5` saves, `Esc` menu, `F11` fullscreen.

**Battle** — `1`-`5` pick an action directly, or arrows + `Enter`. The bar
across the top is the order the round goes in; your own chip is outlined,
and your options grey out while somebody else is deciding.

**Mouse** — click selects, wheel scrolls, and clicking something next to you
interacts with it. Hovering only lights a row; it never moves the selection,
so a stray nudge cannot retarget a keyboard confirm.

Every footer chip is a button: the `Esc`, `Enter`, `Tab` and `B` hints at the
bottom of a screen can be clicked instead of typed. Buying, equipping and
refining need a second, deliberate press of the **Confirm** button, so one
misclick never spends your gold.

Deliberately *not* click-to-move: the mouse never walks your character, so the
pointer and the keys can never fight over where you are going.

**Controller** — Xbox, PlayStation, Switch Pro and most third-party pads,
switched on in Settings → Controller. Stick and d-pad walk and navigate, `A`
interacts and confirms, `B` goes back, `X` and `Y` open the bag and hero sheet,
the shoulders change tabs, and the right trigger sprints.

It is off until you turn it on, and that is deliberate: asking the system for
the list of attached controllers takes about ten seconds on some Windows
machines, and that is the difference between a window that appears instantly
and one that seems broken. Turn it on once and the game remembers.

---

## The story

Elder Maren says the Door must be sealed before the frost comes, and that the
wardens will not let you pass. She has said this before.

Each warden you kill leaves a **Memory** — a permanent passive, and a page in
that person's own voice. You get powerful by hollowing out six people, and
you only learn who they were afterwards. The sixth one at the Door is not
what you were told it is.

Four endings. Two of them are locked behind knowing things you can only have
learned in an earlier run — the codex persists across every save file, so
knowledge is the one thing a new character inherits.

---

## Making a hero

Name, build, skin tone, hair colour, hair style and outfit, with a live
animated preview that rotates through all three facings. Build is Feminine,
Androgynous or Masculine and changes the silhouette; the rest is 3,200 colour
and style combinations. Every one of them is the same character grid with a
different palette and a few reshaped rows — so a custom look costs five
integers in the save file and nothing at runtime. "Surprise me" rolls one.

Arrows on each row are clickable, and only the Begin row starts the run.

Your look carries into New Game+.

---

## The classes

Each changes the stat curve, the skill list, and one rule of combat.

| Class | | Signature | What it does to a turn |
|---|---|---|---|
| **Warden** | Knight | Bulwark | Below 35% health, incoming damage is cut by 40% |
| **Emberwright** | Mage | Kindling | Each spell in a row adds 12% spell damage, up to 60%; a swing resets it |
| **Cutpurse** | Assassin | Opportunist | Your first strike every battle always crits, +15% gold |
| **Longshot** | Archer | Steady Aim | If nothing damaged you since your last turn, your next hit deals 30% more |
| **Bloodhowl** | Berserker | Bloodthirst | Every hit returns 15% of the damage as health, but potions and spells mend for half |
| **Hexbinder** | Warlock | Affliction | 35% more damage to anything already suffering a status effect |
| **Ironmonk** | Monk | Momentum | Above half health you take 18% less damage; below it, 20% more |
| **Apothecary** | Alchemist | Decoction | Potions and elixirs mend you for double, and you set out with far more |
| **Spellblade** | Battlemage | Runeblade | Your weapon attacks also carry magic damage worth 40% of your MAG |
| **Duelist** | Fencer | Single Combat | +35% damage against one enemy, -15% against a group |
| **Oathbound** | Paladin | Vow | Healing yourself deals 35% of it to the enemy as holy damage |
| **Revenant** | Dark Knight | Hollow | Cannot be healed past half, but damage rises up to 60% as health falls |
| **Wayfarer** | Generalist | Fortune's Road | Much better loot, +30% gold, an opening Memory, and one free stand a battle |

The second column is the ordinary RPG word for what each one is. The names are
the game's; the archetypes are there so you can tell at a glance which one is
the mage without having to read six paragraphs first.

**Ten are available from the start.** The last three unlock through endings and
codex progress — those are the reward for finishing, not a gate on choosing.

---

## Replaying it

- **Nine run modifiers**, stacked at run start, each a real trade with a score
  multiplier: *Glass Road* halves your health for +60% damage, *Ironheart*
  removes consumables entirely, *Blind Road* takes the map away.
- **New Game+** carries your class and modifiers into a fresh world with
  tougher enemies, and stacks.
- **A 16-page codex** across five chapters, filled by killing, reading,
  talking and finishing.
- **The story asks you things.** Every scene has answers, and no single
  one picks your ending — the ending is the shape of all of them.
- **Walk it again.** The codex keeps every scene you have reached, with
  the answers you gave in white and the ones you passed over greyed out.
  It fills in across playthroughs, so you can see what you have never
  tried.
- **Endings gated on knowledge** rather than stats. There are more of
  them than the game will admit to.
- **A new world every run** — 112x76 tiles of biome-partitioned terrain,
  generated from a seed.
- **Records** track every finished run, best score, and lifetime totals.
- **Monsters come in packs.** Most fights are still one-on-one, but a
  wandering group turns up often enough to keep you honest — pick who to
  hit with Left and Right, or click the one you want.
- **Logs.** The game keeps a session log, and writes a separate crash
  report if it ever falls over, in a `logs` folder beside your saves.
  Send the crash file and there is something to actually go on.

---

## What changed from the original

The starting point was a single 1120-line file: one screen of tiles, five
bosses, four battle commands and a `save.json` next to the script.

| | Original | Now |
|---|---|---|
| Code | 1 file, 1120 lines | 35 modules, ~16,300 lines |
| World | 40x22 tiles, one screen | 112x76 tiles, 6 biomes, scrolling camera |
| Story | none | six wardens, a codex, four endings |
| Classes | none | 8, each with a signature passive |
| Progression | gear only | 60 levels, XP, 4 attributes, 28 skills |
| Character | one fixed sprite | build, skin, hair, style, outfit |
| Combat | attack / defend / potion / run | initiative, MP, 13 statuses, 6 elements, crits, lifesteal, boss phases |
| Items | 12 fixed pieces | 29 bases x 6 rarities x 25 affixes, 14 relics, 10 consumables |
| Art | coloured rectangles | 25 sprites on one 16x16 grid, walk cycles, idle animation, 16 materials x 4 variants, ambient occlusion, day/night |
| Audio | none | 26 synthesised effects |
| Saves | one file, overwritten | 3 slots, versioned, atomic, checksummed, validated |
| Input | keyboard | keyboard + mouse + controller, clickable hints, confirm buttons |
| Shipping | run it in an IDE | self-contained .exe with its own icon |
| Tests | none | 219 unit, 44 integration, a fuzzer, a layout checker, an art checker, a security audit |

---

## How it fits together

```
main.py                  entry point
run.bat                  launcher, works from any folder
run.sh                   the same for Linux and a Chromebook's Linux
Pixel_RPG.exe            self-contained build

pixel_rpg/
  game.py                window, main loop, overworld, HUD, mouse
  menus.py               every non-battle screen
  battle.py              combat rules: initiative, damage, statuses  (no pygame)
  battlescreen.py        how a fight looks and how keys map onto it
  story.py               the wardens, the codex, the endings
  classes.py             the eight classes and their signatures
  appearance.py          skin, hair, style and outfit
  relics.py              Memories and ordinary relics
  modifiers.py           run modifiers and their trades
  meta.py                the profile that survives across saves
  world.py               generation, biomes, props, camera
  entities.py            player progression, monsters, bosses
  items.py               bases, rarities, affixes, loot rolls
  skills.py              28 abilities and 13 status effects
  gamepad.py             controller support, off until you ask for it
  logbook.py             session and crash logs
  devmenu.py             a hidden debug menu, and the rule that keeps it safe
  scenes.py              story moments, the answers, and the ledger
  net.py                 co-op transport: framed JSON over TCP
  coop.py                co-op sessions: handshake, roster, shared fights
  trade.py               offers, locks, and the transfer between two bags
  portmap.py             STUN + UPnP, so a room code works anywhere
  quests.py              quests and achievements
  save.py                slots: versioned, atomic, validated
  security.py            validation, sanitization, rate limiting
  config.py              settings and environment loading
  audit.py               security self-audit CLI
  assets.py              sprite, tile and font construction
  spritedata.py          the pixel art, as character grids
  textures.py            procedural ground materials
  audio.py               procedural sound synthesis
  ui.py                  panels, bars, toasts, particles, hotspots

tools/                   icon builder, exe builder, verifiers
tests/                   unit, integration, fuzz, layout
```

Four splits are worth knowing about:

- **`battle.py` never imports pygame.** The combat maths is testable without
  a display, which is how the balance table below was measured rather than
  guessed.
- **The world is regenerated from its seed on load, never stored.** Saves stay
  around 10 KB and cannot inject arbitrary map data. The seed *is* the map.
- **Mouse and keyboard share one selection model.** Screens register clickable
  rects while drawing; a click moves the same cursor a key would, then replays
  the same confirm. There is no second copy of the menu logic to drift.
- **Art is data.** Sprites are character grids in `spritedata.py`; ground is
  built by `textures.py`; sound is built by `audio.py`. Nothing loads.

### Art

```python
SLIME16 = [
    "......GGGG......",
    "....GGGGGGGG....",
    "..GGkGGGGGGkGG..",
    ".GGGGGGwwGGGGGG.",
]
```

**Every sprite is 16x16** — hero, monsters, bosses, scenery, all of it. An
earlier pass left characters at 12x12 next to 16x16 bosses and trees, which
read as two different games side by side; `tools/check_art.py` now fails the
build if a grid drifts off the shared size.

Animation comes from the same grids: the hero has a four-phase walk cycle
built from leg-row swaps, creatures with a hand-drawn second pose alternate
between them while the rest breathe procedurally, and scenery sways on a
per-tile phase offset.

Ground materials are generated rather than noised: grass is blades that lean
and catch light at the tip, stone is lit facets with wandering cracks, water
is layered sine bands with caustic highlights. Each material has four variants
picked by a hash of the tile coordinate, so a field stops reading as graph
paper, and walkable tiles get an inner shadow where they meet a solid one.

### Audio

Each effect is a few hundred milliseconds of 16-bit PCM built with the
standard library `array` module — waveform, envelope, optional pitch sweep and
vibrato — handed to `pygame.mixer`. No numpy, no `.wav` files. With no audio
device every call becomes a no-op and the game runs in silence.

---

## Configuration

Copy `.env.example` to `.env` and edit. Every value is validated and clamped,
so a typo degrades the experience instead of crashing the game.

```
PIXEL_RPG_DIFFICULTY=hard     # story | normal | hard | nightmare
PIXEL_RPG_SEED=1234           # fixed seed for a reproducible world
PIXEL_RPG_VOLUME=0.55
```

Saves go to your OS application-data directory
(`%APPDATA%/Pixel_RPG/saves` on Windows), deliberately outside the folder so
they survive a reinstall and are never committed. Override with
`PIXEL_RPG_SAVE_DIR`.

**No credential is hardcoded anywhere, and the game needs none.** See
[SECURITY.md](SECURITY.md).

### Playing together from behind CGNAT

A room code is normally an address, which is what lets co-op work with no
server at all. That stops working if your connection has no address of its
own — most phone tethering, and a lot of home broadband. If that is you,
run a relay somewhere with a public address and point both machines at it:

```bash
python tools/relay_server.py --port 8080     # on the machine with an address
```

```
PIXEL_RPG_RELAY_URL=wss://your-relay.example
```

The host then dials out instead of listening, and room codes become six
characters instead of ten. Both ends need the same URL — the code says
which *room*, not which relay.

**Whoever runs the relay can see everything the room sends.** It is a pipe
in the middle, not an encrypted tunnel. Nothing secret crosses it — the
game sends positions, monsters and a serialised hero — but run your own
rather than somebody else's. With no `PIXEL_RPG_RELAY_URL` set, nothing
about this exists: rooms are direct, exactly as before.

---

## Building and testing

```bash
python tools/build_exe.py          # icon + one-file exe
python tools/verify_exe.py         # boot it headlessly, confirm it runs
python -m pixel_rpg.audit          # security self-audit and secret scan
python tests/test_all.py           # 219 unit tests
python tests/smoke.py              # 35 end-to-end checks, headless
python tests/layout.py             # UI overlap checker, 3 resolutions
python tests/fuzz.py 5000          # mutate saves, assert the loader survives
python tools/check_art.py          # every sprite square, uniform, in palette
python tools/balance.py            # auto-played win rate per class
```

`smoke.py` drives a real `Game` through character creation, every menu, four
battles, a death, an ending and a New Game+ against a dummy display.
`layout.py` instruments every text and panel draw, renders ~70 screens at
three window sizes, and fails on anything that collides, overflows its panel,
falls off the window, or is **buried under a panel drawn later** — which is
how the dialogue boxes ended up sizing themselves to their text, and how the
shop's gold panel was caught hiding behind an item card.

### Balance

Because combat is display-free, the difficulty curve is measured rather than
guessed. Ten auto-played fights per boss per preparation level:

| Difficulty | Under-levelled, uncommon gear | Prepared, rare gear |
|---|---|---|
| normal | 22% wins | 97% |
| hard | 13% | 92% |
| nightmare | 15% | 67% |

---

## Quality of life

- **Equip best** (`B` in the bag) wears the strongest item you own in every
  slot, ranked by the same power score the shop sorts by. It keeps what you
  are already wearing on a tie, so pressing it twice changes nothing.
- The shop and smith show **gold, bag space and potions** in a panel while you
  are spending, and the Confirm button names the exact trade.
- Item rows fit as many stats as the width allows and say how many were left
  out, rather than slicing the text mid-word.
- **Quests only count what you do after accepting them.** Taking on "defeat
  10 monsters" with 400 kills behind you starts at zero. Work is taken from
  Elder Maren in the village or from her runners out on the roads; `J` shows
  your own notes anywhere.
- **Refining** costs gold *and* Ember Steel, and Ember Steel comes from
  salvaging gear you are not using — so ordinary drops feed the piece you
  actually care about.
- A **guide book** on the title screen and pause menu explains every system
  without spoiling any of the story.
- **Controls are rebindable** in Settings → Controls, and a **controller**
  can be switched on in Settings → Controller.
- **One quest at a time**, taken from Maren or a runner, shown in the header.
- Starting a new game on an occupied slot **asks before erasing it**.
- **Play together.** Host a room, read out a ten-character code, and a
  friend joins from anywhere — no server, no account, nothing to pay for.
  You share one world **and the fights in it**: up to four heroes against
  a pack, with XP and gold split evenly among everyone who fought. The
  wardens you face alone.
- **Trading.** `T` next to a friend puts gold, gear and potions on a
  table — anything at all, including what you are wearing. Nothing moves
  until you have both agreed, and changing your side un-agrees both of
  you, so the offer you said yes to is always the offer you get.
- **Six save slots**, scrollable with the wheel.
- **Achievements work twice.** Each character earns the whole set from
  scratch — so a 100% run is something you can chase — while the
  Achievements screen on the title keeps the record of everything you
  have ever earned, across every save.
- **Saves can live beside the game** instead of in your system folder —
  Settings → Save folder — which is what you want on a USB stick.

### Taking it to another PC

**Send the `.exe` on its own.** It is one self-contained file — Python,
pygame and SDL are inside it — so there is nothing to install at the
other end. Windows only, and unsigned, so SmartScreen warns once: **More
info → Run anyway**.

**Your saves do not travel with it.** They live per-user at
`%APPDATA%\Pixel_RPG\saves`, so a new PC starts empty. That folder also
holds `profile.json`, which is the codex, the endings you have reached
and the names you have heard — the things that persist across every
character. Losing it costs the story knowledge, not just the gold.

Two ways to bring it:

- **Copy the folder** to the same path on the new PC, or
- **Switch to portable** (Settings → Save folder) so saves sit in a
  `saves` folder beside the exe and travel with it.

> Switching to portable does **not** move the saves you already have.
> Copy them into the new `saves` folder yourself or the slots will look
> empty — the old ones are still in `%APPDATA%`, just not being read.

---

## Known limitations

- Attribute points are not refundable. There is no respec.
- The `.exe` is Windows-only. On Linux - a Chromebook's included - use
  `bash run.sh`; on macOS, run from source.
- Windows may warn about an unsigned executable the first time; the build is
  unsigned because code-signing certificates cost money.

---

## License

**All rights reserved.** This repository is public so it can be seen, not
so it can be reused: nothing here may be copied, modified or redistributed
without permission from Savvydev-Studios. See [`LICENSE`](LICENSE).
