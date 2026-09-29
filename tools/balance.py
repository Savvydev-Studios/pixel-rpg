"""Auto-play fights and report a win rate per class.

This exists because "balance feels off" is not a measurement. `battle.py`
imports no pygame (hard rule 3), so a fight can be played out thousands of
times in a couple of seconds with no window and no display driver, which is
how the difficulty table in HANDOFF.md was produced rather than guessed.

    python tools/balance.py                  # both preparation levels
    python tools/balance.py --trials 200     # tighter numbers, slower
    python tools/balance.py --difficulty hard
    python tools/balance.py --party          # parties, against pack sizes
    python tools/balance.py --waves          # how deep a Last Stand goes
    python tools/balance.py --bosses         # the six wardens, per difficulty
    python tools/balance.py --strikes        # the free swing, per class

**Read this before believing an outlier.** The probe plays every class with
one simple policy, and a policy that suits seven classes can be actively bad
for the eighth. The first run of this tool put Bloodhowl at 51% while every
other class sat above 80%. The class was fine; the AI was casting `bloodpact`
-- which costs 18% of maximum health -- over and over, on the one class whose
healing is halved. Teaching it not to pay health while low moved the same
class to 88% with no change to the game at all. A buff had already been
applied on the strength of the bad number and had to be taken back out.

So: an outlier here is a question, not an answer. Check what the probe
actually did on that class before you touch a stat.
"""
from __future__ import annotations

import argparse
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pixel_rpg import classes as classes_mod                      # noqa: E402
from pixel_rpg import gamemodes                                   # noqa: E402
from pixel_rpg.battle import Battle, MENU, SUBMENU                 # noqa: E402
from pixel_rpg.config import DIFFICULTY_MODS                       # noqa: E402
from pixel_rpg.entities import (ATTRIBUTE_CAP, ATTRIBUTES, BOSS_DEFS,  # noqa: E402
                               Player, make_boss_encounter, make_encounter,
                               make_enemy, max_enemies)
from pixel_rpg.items import CONSUMABLES, roll_item                 # noqa: E402
from pixel_rpg import relics as relics_mod                        # noqa: E402
from pixel_rpg.skills import HP_COST_SKILLS                        # noqa: E402
from pixel_rpg.util import rng                                     # noqa: E402

# (level, loot luck, label) -- roughly "just arrived" and "came prepared".
PREPARATIONS = ((8, 0.0, "level 8, plain gear"),
                (16, 0.5, "level 16, good gear"))
GUARD = 4000              # frames before a fight is called a stalemate
FRAME = 0.05
# The hardest fight a lone hero can be handed - `entities.max_enemies(1)`.
# `party_table` already treats this as the honest ceiling, "hard and
# winnable", and it is the only solo fight that tells two classes apart.
SOLO_CEILING = 3

# Who turns up, in order, as a party grows. Five starters rather than the
# whole roster: a party built out of unlockables is not a party anybody has
# on their first run, and this is meant to measure the fight most people
# will actually walk into.
PARTY_ROSTER = ("warden", "emberwright", "cutpurse", "longshot", "bloodhowl")
# The level the solo ceiling was measured at, kept so the two tables can be
# read against each other. See `entities.max_enemies`.
PARTY_LEVEL = 14


def power(player):
    """One number for how dangerous a hero is, for the spender below.

    Crude on purpose. It is not trying to be a damage model - it is
    trying to prefer attack on a class that hits and magic on a class
    that casts, so the probe does not pour points into a stat its class
    cannot use. The class multipliers do that work.
    """
    return (player.stat("atk") * getattr(player.pclass, "atk", 1.0)
            + player.stat("mag") * getattr(player.pclass, "mag", 1.0)
            + player.max_hp() / 6.0
            + player.stat("spd")
            + player.stat("defense")
            + player.stat("crit") * 0.5
            + player.stat("crit_dmg") * 0.3)


def spend_points(player):
    """Spend every attribute point, greedily, one at a time.

    **This did not exist until 2026-08-24, and its absence was a hole in
    the instrument.** `build` levelled a hero and geared them and left
    every attribute point unspent, so every number this tool has ever
    printed - including the difficulty table in handoff section 6 - was
    measured on a character who had never opened their own hero sheet.
    The game was always easier than the table said.

    Greedy rather than optimal, and deterministic: for each point, take
    whichever attribute raises `power` most. That is roughly what a
    reasonable player does and nothing like what a min-maxer does, so
    read it as a middle case rather than a ceiling.

    Cost is nine `power` evaluations per point against thousands of
    simulated fights, which is not where the time goes.
    """
    while player.points > 0:
        best, best_score = None, None
        for key in ATTRIBUTES:
            # **Skip what is already full.** Every attribute caps at 60
            # (`ATTRIBUTE_CAP`), and a greedy spender pours everything
            # into one row - so it reaches that cap and then `spend_point`
            # refuses. The first version *broke* out of the loop there,
            # which left **15 points unspent at level 16 and 85 at level
            # 30** - more than half of them.
            #
            # That is §38's bug a second time, in the fix for §38's bug:
            # the harness spent points, and then quietly stopped spending
            # them, and every table in §6 was measured on the result.
            if player.attributes.get(key, 0) >= ATTRIBUTE_CAP:
                continue
            player.attributes[key] += 1
            score = power(player)
            player.attributes[key] -= 1
            if best_score is None or score > best_score:
                best, best_score = key, score
        if best is None or not player.spend_point(best):
            break
    return player


def build(class_key, level, luck):
    """A plausible character: levelled up, geared, and spent.

    **It gives a class everything the game gives it** (2026-09-04). It did
    not, and the Wayfarer paid for it: Fortune's Road is better loot, more
    gold, *and* an opening relic - and the relic is handed out by
    `Game.new_run`, which a probe that builds a bare `Player` never calls.
    So the tool measured a Generalist with no signature at all and read it
    as the weakest class in the game. Its own `loot_luck()` was missing
    for the same reason: `luck` here is the *preparation* level, and the
    class adds to it.

    Third time this docstring's warning has been the answer in one
    session - see `take_turn` on drinking, and on not drinking as a
    Bloodhowl. **Check what the probe did.**
    """
    player = Player("Probe", class_key)
    for _ in range(level - 1):
        player.gain_xp(player.xp_to_next())
    luck += player.loot_luck()
    # **The six, and then a weapon asked for by slot** (s71). The six
    # were rolled unfiltered and the probe simply hoped, so a stream where
    # none of them came out a weapon measured a hero fighting **bare
    # handed** - and it never said so, because `equip_best` on nothing is
    # not an error. `TestTheProbesSpendWhatTheyAreGiven` only caught it
    # when an unrelated change moved the shared stream far enough to roll
    # six pieces of armour. A probe measuring combat with nothing in its
    # hand is this file's own docstring warning, one more time.
    #
    # **Two things about the seventh roll, and both are the point.**
    #
    # It is a *seventh*, not one of the six: spending one of the six on it
    # makes every probe's weapon the only weapon it was offered, which is
    # a real difficulty change smuggled in as a fix.
    #
    # And it is drawn **last**. Drawn first it pushes all six behind it
    # down the shared stream, and the level-16 ceiling reads 75-98% where
    # it read 88-100% - thirteen points of apparent difficulty out of a
    # change that altered no rule in the game. That is s21's wasted
    # `rng.choice` exactly, and it is why rule 31.5 says to diff this
    # table rather than to trust a number off it.
    for _ in range(6):
        player.add_item(roll_item(level, luck=luck))
    player.add_item(roll_item(level, slot="weapon", luck=luck))
    player.equip_best()
    # What `Game.new_run` hands this class before its first step.
    if player.signature() == "fortune":
        pool = [r.key for r in relics_mod.COMMON_RELICS]
        for key in rng.sample(pool, min(1, len(pool))):
            player.add_relic(key)
    spend_points(player)
    player.hp = player.max_hp()
    player.mp = player.max_mp()
    return player


def take_turn(battle, player):
    """One reasonable decision.

    Deliberately simple, but not stupid: it will not pay health for a spell
    while it is losing, which is the difference between measuring a class and
    measuring this function.

    **It drinks, as of 2026-09-04.** It never had, and that was not a
    simplification - it was the docstring's own warning happening again.
    Every hero starts a run holding potions and no real player facing three
    monsters leaves them in the bag, but the probe fought to the death with
    a full pack every time. It cost most on the classes that learn `mend`
    late or never: Cutpurse has no heal at all before level 15 and read
    25% at the ceiling, which measured *this function's* refusal to drink,
    not the class.

    Order is: heal when low (a skill first, because it costs MP rather than
    an item), then hit with the best thing that is affordable.
    """
    healthy = player.hp > player.max_hp() * 0.6
    low = player.hp < player.max_hp() * 0.35
    usable = list(player.skills())
    heals = [s for s in usable if s.key == "mend" and battle.can_pay(s)]
    attacks = [s for s in usable
               if s.kind == "attack" and battle.can_pay(s)
               and (healthy or s.key not in HP_COST_SKILLS)]
    # Healing items only. A Fire Bomb is a damage decision and a Smoke Bomb
    # ends the fight in a way this probe counts as a loss, so leaving both
    # out keeps the policy honest about what it is doing.
    #
    # **Except on a class that heals by hitting.** Bloodhowl's signature
    # mends it for half from a potion and returns 15% of every blow as
    # health, so a turn spent drinking is worth less than the turn it
    # replaced - and teaching the probe to drink dropped it from 72% to
    # 42% at the ceiling while lifting everybody else. That is the
    # docstring's warning for the *second* time on the *same class*, which
    # is why the rule is written against the signature rather than the
    # name: any future class with `bloodthirst` gets it too.
    potions = []
    if getattr(player.pclass, "signature", "") != "bloodthirst":
        potions = [k for k in battle.usable_items()
                   if CONSUMABLES[k].effect in ("heal", "full")]

    if low and heals:
        battle.phase = SUBMENU        # where the keyboard would have put us
        battle.use_skill(heals[0])
    elif low and potions:
        # The weakest one that is still worth the turn, so a hero does not
        # burn an Elixir on a scratch and then have nothing at the boss.
        battle.phase = SUBMENU
        battle.use_item(min(potions, key=lambda k: CONSUMABLES[k].price))
    elif attacks:
        battle.phase = SUBMENU
        battle.use_skill(max(attacks, key=lambda s: s.power))
    else:
        battle.player_attack()


def play(party, enemy, mods):
    """Play one fight out. `party` may be a single player or a list.

    `battle.actor` rather than a captured player, because with more than one
    hero the menu belongs to whoever is up. Solo the two are the same object,
    which is why the per-class table below is unchanged by this.
    """
    battle = Battle(party, enemy, mods=mods)
    for _ in range(GUARD):
        if battle.finished:
            break
        if battle.phase == MENU:
            take_turn(battle, battle.actor)
        battle.update(FRAME)
    return battle.result == "victory"


def measure(level, luck, mods, trials, count=1):
    """Win rate per class against `count` monsters of their own level.

    **`count` exists because the one-monster table was blind** (2026-09-04).
    Every class sat at 95-100% in it, which handoff §19 recorded as "the
    instrument is saturated" - and it was not saturation, it was the wrong
    fight. One hero against one same-level monster is the *easiest* thing
    this game generates; naturally everybody wins it, and a table that
    always reads 100% is a check that checks nothing.

    Asked at the solo ceiling instead - three monsters, which `party_table`
    already calls the honest hard-but-winnable fight - the same eight
    classes spread from 25% to 100%. **Nothing about the game changed
    between those two tables.** The classes had never been within five
    points of each other; the probe simply could not see it.
    """
    rows = []
    for cls in classes_mod.CLASSES:
        wins = 0
        for trial in range(trials):
            # Same seed per trial across classes, so every class faces the
            # same monsters with the same drops. Without this the comparison
            # is noise.
            rng.reseed(1000 + trial)
            player = build(cls.key, level, luck)
            if count == 1:
                foes = make_enemy(level, "meadow", mods)
            else:
                # Through `make_encounter`, so the pack carries the same
                # per-head softening the real game applies. Built by hand it
                # would measure a fight the game never generates.
                foes = make_encounter(level, "meadow", mods,
                                      party=1, count=count)
            if play(player, foes, mods):
                wins += 1
        rows.append((cls.archetype, cls.name, round(100 * wins / trials)))
    return rows


def measure_party(size, monsters, mods, trials, level=PARTY_LEVEL,
                  foe_level=None):
    """Win rate for `size` heroes against exactly `monsters` monsters.

    The pack comes from `make_encounter` with a forced count, so it carries
    the same per-head softening the real game applies. Building the monsters
    by hand here would measure a fight the game never generates.

    `foe_level` is separate from `level` on purpose. Raising both together
    measures nothing - a level-20 party against level-20 monsters is *easier*
    than a level-14 one against level-14 monsters, because hero growth
    outruns monster growth. Asking "would tougher monsters help" means
    holding the heroes still.
    """
    wins = 0
    for trial in range(trials):
        rng.reseed(4000 + trial)
        party = [build(PARTY_ROSTER[i % len(PARTY_ROSTER)], level, 0.15)
                 for i in range(size)]
        foes = make_encounter(level if foe_level is None else foe_level,
                              "meadow", mods, party=size, count=monsters)
        if play(party, foes, mods):
            wins += 1
    return round(100 * wins / trials)


def party_table(mods, trials):
    """Every party size against every pack size the game can hand it.

    Cells above `max_enemies(party)` are left blank rather than measured:
    the question is whether the caps are honest, and a number for a fight
    that cannot happen would invite somebody to tune against it.
    """
    widest = max(max_enemies(p) for p in range(1, 5))
    print("Party fights: %d per cell, level %d, plain-ish gear" %
          (trials, PARTY_LEVEL))
    print()
    header = "".join("%7s" % ("%d foe%s" % (n, "" if n == 1 else "s"))
                     for n in range(1, widest + 1))
    print("  %-11s%s" % ("", header))
    verdicts = []
    for size in range(1, 5):
        cap = max_enemies(size)
        cells = []
        for count in range(1, widest + 1):
            if count > cap:
                cells.append("%7s" % "-")
                continue
            pct = measure_party(size, count, mods, trials)
            cells.append("%6d%%" % pct)
            if count == cap:
                verdicts.append((size, cap, pct))
        label = "%d hero%s" % (size, "" if size == 1 else "es")
        print("  %-11s%s" % (label, "".join(cells)))
    print()
    print("  At each party's own ceiling:")
    for size, cap, pct in verdicts:
        note = "wall" if pct < 40 else ("soft" if pct > 92 else "ok")
        print("    %d vs %d   %3d%%   %s" % (size, cap, pct, note))
    print()
    print("  A ceiling is honest when it is hard and winnable. Solo's was")
    print("  set at 3 monsters and 90%; a cell far outside that band means")
    print("  the cap is wrong, not that the party is.")


# -- the free swing -----------------------------------------------------------
#
# **The class table cannot see this.** `take_turn` presses the basic attack
# only when nothing else is affordable, so per-class strikes (item 0ai)
# barely move a table built out of skill casts - the diff either side of
# adding them was two points on two classes, which is not evidence of
# anything. A clean diff from an instrument that does not exercise the
# change is the §60 lesson wearing a different hat.
#
# So this measures the swing on its own: one hero, one pack, one attack,
# repeated. It answers "what is a class's free attack worth", which is the
# question the strikes changed and no other table asks.


def measure_strike(class_key, level, mods, trials, foes=3):
    """Average damage dealt by one basic attack, across the whole pack."""
    total = 0
    for trial in range(trials):
        rng.reseed(8000 + trial)
        hero = build(class_key, level, 0.15)
        pack = make_encounter(level, "meadow", mods, party=1, count=foes)
        before = [foe.hp for foe in pack]
        battle = Battle(hero, pack, mods=mods)
        for _ in range(GUARD):
            if battle.finished or battle.phase == MENU:
                break
            battle.update(FRAME)
        if battle.finished:
            continue
        battle.player_attack()
        for _ in range(120):
            if battle.phase == MENU or battle.finished:
                break
            battle.update(FRAME)
        total += sum(was - foe.hp for was, foe in zip(before, battle.enemies))
    return total / max(1, trials)


def strike_table(mods, trials, level):
    from pixel_rpg import classes as classes_mod2
    print("The free swing: %d attacks per class at level %d, three foes"
          % (trials, level))
    print()
    rows = []
    for cls in classes_mod2.CLASSES:
        strike = classes_mod2.strike_for(cls.key)
        rows.append((cls.archetype, cls.name, strike.name,
                     measure_strike(cls.key, level, mods, trials)))
    best = max(row[3] for row in rows) or 1.0
    for archetype, name, strike, damage in rows:
        print("  %-12s %-13s %-18s %6.1f  %s"
              % (archetype, name, strike, damage,
                 "#" * int(28 * damage / best)))
    low = min(row[3] for row in rows)
    high = max(row[3] for row in rows)
    print()
    print("  %-46s spread %.1f - %.1f  (x%.2f)"
          % ("", low, high, high / max(0.1, low)))
    return rows


# -- the wardens --------------------------------------------------------------
#
# **Item 16.** §6 carried a boss table that was marked STALE and kept only
# as a record: every figure in it was taken before this harness spent
# attribute points (§38), so it described a hero who had never opened
# their own sheet. `build` has spent them since 2026-08-24; nothing
# re-measured the table.
#
# The two preparations are §6's own words - "under-levelled, uncommon
# gear" and "prepared, rare gear" - pinned to numbers here so the next
# person re-running this measures the same thing.

#: (levels above the warden, loot luck, label).
BOSS_PREPARATIONS = ((-1, 0.0, "under-levelled, plain gear"),
                     (4, 0.6, "prepared, good gear"))


def measure_boss(definition, class_key, level, luck, mods, trials):
    """Win rate for one class against one warden."""
    wins = 0
    for trial in range(trials):
        rng.reseed(6000 + trial)
        hero = build(class_key, level, luck)
        foes = make_boss_encounter(definition, mods, party=1)
        if play(hero, foes, mods):
            wins += 1
    return round(100 * wins / trials)


def boss_table(mods, trials, difficulty):
    """Every warden, at both preparations, averaged over the classes.

    Averaged rather than per-class on purpose: §6's table is a statement
    about **the difficulty setting**, not about the roster, and a table
    with thirteen columns answers a question nobody asked there.
    """
    print("Wardens: %d fights per class per warden, %s"
          % (trials, difficulty))
    print()
    rows = []
    for definition in BOSS_DEFS:
        cells = []
        for offset, luck, _label in BOSS_PREPARATIONS:
            level = max(1, definition.level + offset)
            per_class = [measure_boss(definition, cls.key, level, luck,
                                      mods, trials)
                         for cls in classes_mod.CLASSES]
            cells.append(round(sum(per_class) / len(per_class)))
        rows.append((definition.name, cells))
    width = max(len(name) for name, _cells in rows)
    header = "".join("%26s" % label for _o, _l, label in BOSS_PREPARATIONS)
    print("  %-*s%s" % (width, "", header))
    for name, cells in rows:
        print("  %-*s%s" % (width, name,
                            "".join("%25d%%" % c for c in cells)))
    means = [round(sum(cells[i] for _n, cells in rows) / len(rows))
             for i in range(len(BOSS_PREPARATIONS))]
    print("  %-*s%s" % (width, "average",
                        "".join("%25d%%" % m for m in means)))
    return means


# -- wave mode ----------------------------------------------------------------
#
# **The ramp shipped unmeasured** (s59) and said so. This is the probe that
# was missing: a real Last Stand, played out wave by wave with the hero it
# actually gives you.
#
# Two things make this different from every other measurement here, and both
# are properties of the mode rather than of the harness:
#
# * **The hero carries between waves.** Health, MP, level, gear and the
#   rewards all persist, so a wave is not a fresh fight - it is a fight
#   against whatever the last one left of you. That is the whole question.
# * **The payout is the game's own.** `gamemodes.grant_wave_rewards` is
#   what `Game.wave_cleared` calls, so this measures the wave the game
#   pays rather than a second copy of it (s23's warning, applied early).

#: Give up on a run this deep. Nothing should reach it; if a class does,
#: that is the finding.
WAVE_GUARD = 120


def play_wave_run(class_key, mods, seed, party_size=1):
    """Play one Last Stand to its end. Returns the wave it died on.

    The hero is the one the mode actually hands you: level one and the
    class's starting kit. Points are spent and the best gear is worn after
    every wave, because a player does both and s38 records what measuring
    a hero who does neither is worth.
    """
    rng.reseed(seed)
    party = [build(class_key, 1, 0.0) for _ in range(party_size)]
    for hero in party:
        hero.full_restore()
    run = gamemodes.WaveRun()
    for _ in range(WAVE_GUARD):
        wave = run.wave
        level = gamemodes.wave_level(wave)
        count = gamemodes.wave_pack(wave, party_size)
        foes = make_encounter(level, "meadow", mods,
                              party=party_size, count=count)
        if not play(party if party_size > 1 else party[0], foes, mods):
            return run.cleared
        reward = run.clear_wave(kills=len(foes))
        for hero in party:
            gamemodes.grant_wave_rewards(
                hero, reward,
                roll_item=lambda lv: roll_item(lv, luck=0.45),
                roll_consumable=lambda: "potion")
            # **Spend the points and wear the gear.** The first version of
            # this did neither, and measured a hero who levelled fifteen
            # times without opening their own sheet - which is s38's hole
            # in the instrument, reopened in a new probe. It read as the
            # ramp being brutal; it was the harness.
            spend_points(hero)
            hero.equip_best()
    return run.cleared


def wave_table(mods, trials, party_size=1):
    """How deep each class gets, and how deep a party gets."""
    print("Last Stand: %d run%s per class, party of %d"
          % (trials, "" if trials == 1 else "s", party_size))
    print()
    rows = []
    for cls in classes_mod.CLASSES:
        depths = [play_wave_run(cls.key, mods, 7000 + trial, party_size)
                  for trial in range(trials)]
        depths.sort()
        median = depths[len(depths) // 2]
        rows.append((cls.archetype, cls.name, median,
                     min(depths), max(depths)))
    for archetype, name, median, low, high in rows:
        print("  %-12s %-13s %3d waves   (%d-%d)  %s"
              % (archetype, name, median, low, high,
                 "#" * min(40, median)))
    medians = [median for _a, _n, median, _l, _h in rows]
    print()
    print("  %-26s median %d-%d waves across the classes"
          % ("", min(medians), max(medians)))
    return rows


def wave_milestones(mods, trials):
    """What share of runs reach each rest wave.

    The rest waves are the mode's own milestones (every tenth), so they are
    the honest places to ask "how far does this actually go".
    """
    depths = []
    for cls in classes_mod.CLASSES:
        for trial in range(trials):
            depths.append(play_wave_run(cls.key, mods, 9000 + trial))
    depths.sort()
    print()
    print("Across every class, %d runs:" % len(depths))
    for milestone in (5, 10, 20, 30, 40, 50):
        reached = sum(1 for d in depths if d >= milestone)
        print("  reached wave %-3d %3d%%  %s"
              % (milestone, round(100 * reached / len(depths)),
                 "#" * (40 * reached // len(depths))))
    print("  deepest run     %3d waves" % depths[-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=60,
                        help="fights per class per preparation (default 60)")
    parser.add_argument("--difficulty", default="normal",
                        choices=sorted(DIFFICULTY_MODS),
                        help="which difficulty to measure (default normal)")
    parser.add_argument("--party", action="store_true",
                        help="measure parties against pack sizes instead")
    parser.add_argument("--strikes", action="store_true",
                        help="measure each class's free attack (item 0ai)")
    parser.add_argument("--bosses", action="store_true",
                        help="re-measure the section 6 warden table (item 16)")
    parser.add_argument("--waves", action="store_true",
                        help="measure how deep a Last Stand run goes (s60)")
    parser.add_argument("--wave-party", type=int, default=1,
                        help="party size for --waves (default 1)")
    args = parser.parse_args()
    mods = DIFFICULTY_MODS[args.difficulty]

    if args.strikes:
        for level in (8, 16):
            strike_table(mods, max(20, args.trials), level)
            print()
        print("A basic attack is the floor everything else is measured")
        print("against, so read the spread rather than the numbers.")
        return 0

    if args.bosses:
        means = {}
        for name in ("normal", "hard", "nightmare"):
            means[name] = boss_table(DIFFICULTY_MODS[name], args.trials // 6
                                     or 1, name)
            print()
        print("Section 6's table, re-measured:")
        print()
        print("| Difficulty | %s | %s |"
              % (BOSS_PREPARATIONS[0][2], BOSS_PREPARATIONS[1][2]))
        print("|---|---|---|")
        for name in ("normal", "hard", "nightmare"):
            print("| %s | %d%% wins | %d%% |"
                  % (name, means[name][0], means[name][1]))
        return 0

    if args.waves:
        wave_table(mods, max(1, args.trials // 6), args.wave_party)
        if args.wave_party == 1:
            wave_milestones(mods, max(1, args.trials // 12))
        print()
        print("A wave run carries its hero between fights, so this is the")
        print("one table here where the *previous* fight is half the")
        print("question. An outlier is still a question, not an answer.")
        return 0

    if args.party:
        party_table(mods, args.trials)
        print("An outlier is a question, not an answer -- read the docstring.")
        return 0

    print("Auto-played fights: %d per class, %s difficulty"
          % (args.trials, args.difficulty))
    print()
    for level, luck, label in PREPARATIONS:
        for count in (1, SOLO_CEILING):
            print("%s, %s" % (label, "one monster" if count == 1
                              else "%d monsters (the solo ceiling)" % count))
            rows = measure(level, luck, mods, args.trials, count=count)
            for archetype, name, pct in rows:
                print("  %-12s %-13s %3d%%  %s"
                      % (archetype, name, pct, "#" * (pct // 5)))
            spread = [pct for _a, _n, pct in rows]
            print("  %-26s spread %d-%d%%" % ("", min(spread), max(spread)))
            print()
    print("**The ceiling table is the one that says anything.** The")
    print("one-monster table reads ~100% for every class at every level and")
    print("always has; it is the easiest fight the game makes. Read the")
    print("second table of each pair.")
    print("An outlier is a question, not an answer -- read the docstring.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
