"""The Sixth Road - narrative content, codex and endings.

Design note
-----------
The plot is carried by the thing the player was already doing: killing six
wardens to get stronger. Each kill hands over a Memory, which is both a
mechanical upgrade and a page in that warden's own voice. The player becomes
powerful by hollowing out six people, and only learns who they were afterwards.

Codex entries persist in the meta profile across every save file, so knowledge
is the one thing a new run inherits - and two endings are gated on it.
"""
from __future__ import annotations

from dataclasses import dataclass

PROLOGUE = [
    "Six roads leave this valley. Five of them end at a warden.",
    "The sixth ends at the Door.",
    "",
    "Elder Maren says the Door must be sealed before the frost comes,",
    "and that the wardens will not let you pass.",
    "",
    "She has said this before.",
]

# What Maren tells you, act by act. She is not lying. She is choosing which
# true things to say, which is worse.
MAREN_BRIEFINGS = {
    0: ["The east road is open. Something squats at the end of it.",
        "Kill it and the next road unseals itself. That is how it works.",
        "Do not ask me why yet. You would not go."],
    1: ["Good. You are quicker than the last one.",
        "No. I misspoke. There was no last one."],
    2: ["You are carrying their Memories. I can see them on you.",
        "Do not read them. It does not help. I know."],
    3: ["Three left. You are further than anyone since Ilan.",
        "Bram will not speak to me. Ask him yourself, if you like."],
    4: ["Two. You could stop. People have stopped.",
        "They lived quite ordinary lives afterwards, down in the town.",
        "I remember all of their names."],
    5: ["One. Behind the last door is the thing that made all of this.",
        "When you reach her, it, you will want to listen.",
        "Do not."],
    6: ["So. The Door is open.",
        "Come back to me before you walk through it. Please.",
        "There is a thing I have not told you, and I would rather say it."],
}


@dataclass(frozen=True)
class WardenLore:
    boss_key: str
    true_name: str
    epitaph: str
    years: int
    memory_key: str
    intro: tuple
    death: tuple
    memory: tuple


WARDENS = [
    WardenLore(
        "sprout_king", "Callun Reed", "who was fourteen", 40, "memory_callun",
        ("You are not the frost. You are warm.",
         "Are you here to take my turn? Maren said someone would.",
         "I have been counting. I am up to four hundred thousand."),
        ("Oh. Oh, that is better.",
         "Tell my mother the field is finished. I finished it."),
        ("I was fourteen and the fastest runner in the valley, which is why",
         "they picked me. Maren said standing still was the same as running,",
         "only inward. She said it kindly. She says everything kindly.",
         "",
         "The first ten years I sang. The next ten I forgot the words but",
         "kept the tune. After that I counted, because counting has no end",
         "and neither did I.",
         "",
         "If you are reading this you have already killed me. That is all",
         "right. Someone had to be first through the door and it was never",
         "going to be her.")),
    WardenLore(
        "stone_golem", "Dorn Ashgrave", "who built his own cell", 80,
        "memory_dorn",
        ("Stop there. I have laid the stones out and you are standing on them.",
         "Eighty years to build a room with no door.",
         "You will not understand why that took skill."),
        ("Mind the lintel. It is not set.",
         "It was never going to be set."),
        ("A mason does poorly with nothing to build, so I built.",
         "I quarried the hill I stand on, dressed it, and raised a room",
         "around myself with no door and no window, one stone a year.",
         "",
         "People ask why a man would wall himself in. Wrong question.",
         "I was already walled in. The stones were the only part I chose.",
         "",
         "Maren visited in year sixty. She stood outside the wall and told",
         "me the valley was well, the harvest good, the children many.",
         "She did not come inside. There was no inside to come to.",
         "That was the point, and she knew it, and she came anyway.")),
    WardenLore(
        "night_warden", "Sister Vell", "who would not watch", 120,
        "memory_vell",
        ("Do not tell me what you look like.",
         "I have made a great effort not to know.",
         "If I never see the road I can pretend it goes nowhere."),
        ("Ah. There you are.",
         "I looked after all. It was worth it. You have a kind face."),
        ("I put out my own eyes in the ninth year, with my thumbs, on purpose,",
         "and I would do it again.",
         "",
         "The waypoint faces the town. You can see the smoke of it, and the",
         "lamps at evening, and in summer the children going down to the",
         "water. You can see all of it and you cannot go, not one step, and",
         "you have four hundred years.",
         "",
         "Blind, I only had to hear it. That is a considerable mercy and I",
         "arranged it myself. Nobody else was going to.")),
]

WARDENS += [
    WardenLore(
        "inferno_beast", "Kessa Vane", "the twice-burned", 200, "memory_kessa",
        ("Careful. I am still warm in places.",
         "I burned the road once. Ninety miles of it, up to the pass.",
         "It grew back. Roads do. I did not."),
        ("Put me out. Please put me out.",
         "...thank you."),
        ("Year one hundred and six I decided the anchor could not hold what",
         "did not exist, so I set fire to the road. All of it. I burned for",
         "eleven days and the road burned for nine.",
         "",
         "It grew back in a season. Grass does not care about arguments.",
         "",
         "What I learned is this: the seal does not need the road. It needs",
         "*me*. The road is scenery. Any of us could have been staked in a",
         "cellar and it would have worked exactly as well.",
         "",
         "So the roads, the waypoints, the long beautiful view of the town -",
         "those are not the price. Those are decoration on the price.",
         "Someone chose to decorate it. Ask yourself who, and why.")),
    WardenLore(
        "sky_tyrant", "Ilan Vance", "who kept writing", 340, "memory_ilan",
        ("You have my brother's shoulders. He stood like that.",
         "Bram. Is he - no. Do not answer. Three hundred years.",
         "Come on then. Quickly, before I ask again."),
        ("The letters are under the third stone. He never got them.",
         "Tell him I was not angry. Tell him I was never once angry."),
        ("I have written my brother four thousand and twelve letters.",
         "I cannot send them. There is nobody to carry them and I cannot",
         "leave the stone. So I write them, put them under the stone, and",
         "start the next one.",
         "",
         "Letter one was an apology for taking the road instead of him.",
         "Letter two thousand was about a bird.",
         "The last is only his name, over and over, because I had started to",
         "lose the sound of it and writing it down was the only way I had",
         "to keep it.",
         "",
         "Bram. Bram. Bram. Bram.",
         "There. It is still there.")),
    WardenLore(
        "void_sovereign", "Ysolde Ain", "the first", 600, "memory_ysolde",
        ("Stop. Look at me properly. I am not what she told you I am.",
         "Six hundred years I have held this shut with my hands.",
         "Every anchor she sends me fails. They were people. People fail.",
         "",
         "I am so tired. Do not seal it. End it, or take my place,",
         "but do not let her hand you the same rope she handed me."),
        ("Thank you. Thank you. Oh, thank you.",
         "Look - it is only a door. It was only ever a door."),
        ("I was the first and I did it gladly, which is the part nobody",
         "believes. The valley was drowning in cold, I was twenty-three, and",
         "I said yes before she finished the sentence.",
         "",
         "She was twenty-two. We were friends. Her name is Maren Ain,",
         "she is my sister, and she has sent five more people up this road",
         "to do a thing she has never once done herself.",
         "",
         "I do not hate her. I have had six hundred years to and I could not",
         "manage it. But I want you to know it, because everyone who came up",
         "this road died not knowing, and that is the actual crime.",
         "",
         "It is not the standing still. It is that she watched.")),
]

WARDEN_BY_BOSS = {w.boss_key: w for w in WARDENS}

# ------------------------------------------------------------------------
# **SIX IS A FIXED POINT. Do not add a seventh warden.**
#
# This is the load-bearing half of Savvydev's expandability constraint
# (2026-08-24): *"make it so that the story remains expandable without
# cutting like the meaning of an ending from the previous version"*.
#
# Adding a warden does not lengthen the story - it **falsifies finished
# runs**, because the count is not a statistic here, it is content:
#
#   * **`THE SEVENTH` is an ending named after a number.** Six anchors plus
#     you is the seventh. Seven anchors makes the player the eighth and the
#     title becomes a lie in every save that already reached it.
#   * `THE BREAKING` says *"All six"*, *"Six people stop standing still"*,
#     and names all of them. `THE LONG WALK` counts *"six waypoints"*.
#   * **Ysolde is "the first"** and the anchors run 40, 80, 120, 200, 340,
#     600 years - a sequence with a beginning, not a list with room in it.
#
# So the story gets longer **between** the wardens, never by adding one.
# That is not a restriction on the expansion; it is what makes the
# expansion safe, because everything added between fixed points leaves
# both points meaning exactly what they meant. Section 42 is the design
# built on this.
#
# `tests/test_all.py::TestTheAnchorCount` fails if this changes, and says
# why rather than just what.
ANCHOR_COUNT = 6


# ------------------------------------------------------------------ codex ---

# --------------------------------------------------------------- waystations ---
#
# **Roadmap item 12: where the two hours come from** (s42). A road used to
# be a walk to a portal. Each one now passes through a place where people
# live, and each place holds one fragment.
#
# **The fragment does not resolve until the Memory does**, and that rule is
# the whole design rather than a flourish. Explaining Callun before you
# kill Callun would delete the conceit s33 is built on - *you only find out
# who they were afterwards*. So the keeper is not withholding a secret; she
# genuinely does not know what she is part of. Then you read a page that
# says *"Tell my mother the field is finished"*, and the place you already
# walked through rearranges behind you.
#
# It also pays the replay system instead of spending it: on a second run
# every one of these reads differently on the way in.
#
# `before` is what the keeper says while the road is still standing.
# `after` is what they say once that warden has fallen - the same person,
# who has not learned anything, saying the thing you can now hear.
#
# **Drafted by an assistant** on Savvydev's instruction (s34, 2026-08-29):
# *"draft the story too, i dont really have a story for this one"*. The
# design underneath is his.
#
# ---------------------------------------------------------------------
# **The road back (2026-08-30).** Each stop now has *two* scenes, and the
# second one is the reason the first exists.
#
# `scene` is the fragment, asked on the way out, and it cannot resolve.
# `resolution` is the conversation on the way back, once the Memory has
# told you who was at the end of the road - so it is the only place in
# the game where **you** know more than the person you are talking to.
# That is the inversion the whole waystation design was built for: a
# warden scene asks who somebody *was*, of somebody who is dying; a
# resolution scene asks what you are going to do about knowing.
#
# **`after` is now the residue, not the payoff.** It used to carry the
# beat, because there was nothing else to carry it; the beat is in the
# resolution scene now and `after` is what the place is like afterwards,
# said every time you come back. Two rules kept it honest:
#
#   * **It must be true whatever the player said**, including nothing.
#     Every resolution scene offers a silence, and a residue that assumes
#     you spoke is a residue that lies to the quietest playthrough.
#   * **It still may not name the warden** - `TestWaystations` checks it,
#     and the reason is unchanged: the `after` lines are also what a
#     player reads if they somehow never opened the Memory.
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class Waystation:
    key: str
    name: str            # the place
    boss_key: str        # the road it sits on
    keeper: str          # who you meet there
    before: tuple        # said while that warden still stands
    after: tuple         # said forever after, once they have fallen
    scene: str           # the fragment, on the way out
    resolution: str      # the conversation on the way back


WAYSTATIONS = [
    Waystation(
        "hallow", "Hallow Field", "sprout_king", "Wenna",
        ("The field wants finishing. It has wanted finishing a long while.",
         "No. Thank you. No."),
        ("She is on the wall again. She is on the wall most days now.", "",
         "The strip is finished and nobody has started another, and the",
         "others have stopped walking past it quickly."),
        "way_hallow", "res_hallow"),
    Waystation(
        "cut", "Ashgrave Cut", "stone_golem", "Foreman Reyd",
        ("Best stone in the valley and none of us cut it. Older work.",
         "That block in the square? We keep it clean. We do not open it.",
         "There is nothing to open. That is the point of it."),
        ("Nobody has swept the block this week, and the town has not",
         "decided out loud why not.", "",
         "Reyd stands near it with his hands behind his back, the way a",
         "man stands near work he did not do and would like to have."),
        "way_cut", "res_cut"),
    Waystation(
        "watch", "Vell's Watch", "night_warden", "Lamplighter Hesk",
        ("Every lamp, every night. No, I do not know why. My father did it.",
         "The chapel is shut. It is shut from the inside, before you ask."),
        ("He does the round anyway. Every lamp, every night.", "",
         '"It was not for nothing," he says, before you have asked',
         'anything. "It was just not for what I thought it was for."'),
        "way_watch", "res_watch"),
    Waystation(
        "ninety", "Ninety Mile", "inferno_beast", "Dray",
        ("I take loads to the last marker. Not past it. Nobody goes past it.",
         "It is not fear. It is that the road stops meaning anything.",
         "The markers count down. Down to nothing. Somebody laid them wrong."),
        ("There is a new marker in the yard, painted by hand. It says",
         "NINETY, which is the wrong number for this end of the road.", "",
         "Dray has not explained it to the other drivers and does not",
         "intend to."),
        "way_ninety", "res_ninety"),
    Waystation(
        "relay", "The Relay", "sky_tyrant", "Postmaster Ivy",
        ("Sorted, held, and not collected. That is most of the job.",
         "One box has been full since before my grandmother.",
         "I keep filling it. It is a box. It is what it is for."),
        ("The brass frame at the front of the box is empty.", "",
         "The card lives in her apron pocket now. She checks it is still",
         "there the way people check for a key, about twice an hour,",
         "and the box behind it is as full as it ever was."),
        "way_relay", "res_relay"),
    Waystation(
        "ain", "Old Ain", "void_sovereign", "the sweeper",
        ("She does not answer, and she does not stop sweeping.",
         "The ruin is older than the village. Somebody keeps it tidy.",
         "By one doorway, two names are scratched. One is worn away."),
        ("The broom is still leaning where it was left, by the doorway",
         "with the two names on it.", "",
         "Nobody has picked it up. For the first time in six hundred",
         "years the floor is getting dusty, and it is only a floor."),
        "way_ain", "res_ain"),
]

WAYSTATION_BY_BOSS = {w.boss_key: w for w in WAYSTATIONS}
WAYSTATION_BY_KEY = {w.key: w for w in WAYSTATIONS}


def waystation_lines(key, felled):
    """What the keeper says, given whether that road is done."""
    stop = WAYSTATION_BY_KEY.get(key)
    if stop is None:
        return ()
    return stop.after if felled else stop.before


# ------------------------------------------------------------------ favours ---
#
# **Small things people ask you for** (s42). Savvydev, 2026-08-29:
#
# > "add a bunch of npcs around the map and stuff that give you sidequests
# > or task you to do something super duper small like a puzzle task or
# > something like that which makes them grant you a reward"
#
# **Deliberately not quests.** `quests.py` has a one-at-a-time cap and
# that cap is Savvydev's own decision (s9, s13) - a dozen tiny errands
# competing for it would either break the rule or make the rule
# meaningless. A favour is settled inside the conversation that starts it:
# ask, answer, done, paid. Nothing goes in the log and nothing is carried.
#
# Three shapes, all resolvable on the spot:
#
#   * **riddle**  - a question with one right answer.
#   * **toll**    - they want something you are carrying.
#   * **notice**  - a question about the valley you can only answer if you
#                   have been looking at it.
#
# The wrong answer is never punished beyond losing the reward, because
# these are people asking a favour, not a trial. `needs` names a
# consumable a toll wants; everything else is answered from what the
# player knows.
#
# **A favour does not know who is asking it** (2026-08-31), the same rule
# `ERRAND_VOICE` has and for a sharper reason. These are handed to whoever
# the world had spare, and since everybody has a name now (s34, s44) the
# panel says **Orrin of Two Wells** over the top of the prose. Text that
# said *"she is counting fence posts"* was fine while the asker was
# anonymous and contradicts the header the moment they are not. So: the
# asker is *somebody* or *they*, throughout, and every option label says
# "them" rather than picking. A gendered pronoun in here now belongs to
# somebody the asker is talking *about* - their brother, the elder - never
# to the asker.


@dataclass(frozen=True)
class Favour:
    key: str
    prompt: tuple            # what they say
    options: tuple           # (label, reply, correct?)
    reward_gold: int
    reward_xp: int
    kind: str = "riddle"
    needs: str = ""          # consumable key, for a toll


FAVOURS = [
    Favour("counting",
           ("Somebody is counting fence posts and has lost the count",
            "twice.",
            "",
            '"Settle something for me," they say. "A thing that goes on',
            'and never arrives - what is it doing? Waiting, or walking?"'),
           (("Waiting", ('"That is what I said," they say, disappointed',
                         "in somebody who is not you."), False),
            ("Walking", ('"Walking," they agree. "It is walking. Thank',
                         'you. I can stop now."'), True),
            ("Neither. It has arrived.",
             ('"No," they say. "But that was a better answer than mine."',),
             False)),
           60, 40),
    Favour("thirsty",
           ("Somebody is sitting against a wall in the shade, and does",
            "not get up when you come over.",
            "",
            '"You would not have anything to drink," they say. It is not',
            "quite a question."),
           (("Hand over a potion",
             ("They drink half and hand the rest back, which is not what",
              "you expected and is somehow worse.",
              "",
              '"Right," they say, and get up.'), True),
            ("Say you have nothing",
             ('"Right," they say, and do not get up.',), False)),
           0, 60, kind="toll", needs="potion"),
    Favour("wardens",
           ("Somebody with a satchel and no obvious errand.",
            "",
            '"You have been out there," they say. "How many are down?"'),
           (("Tell them the number",
             ('It goes into a book. "Thank you. The elder likes to know."',
              "",
              "They do not say which elder, and you do not ask."), True),
            ("Say you have not counted",
             ('"Somebody should," they say, and move on.',), False)),
           45, 55, kind="notice"),
    Favour("stone",
           ("Somebody is looking at a standing stone with the expression",
            "of a person doing arithmetic.",
            "",
            '"Which is heavier," they say. "The stone, or the reason',
            'somebody dragged it up here?"'),
           (("The stone",
             ('"Sensible," they say, and sound let down.',), False),
            ("The reason",
             ('"Yes," they say. "Yes. Nobody ever says that first."',
              "",
              "Something comes out of a pocket and into your hand",
              "without either of you looking at it."), True),
            ("They weigh the same",
             ('"Now that," they say, "is a different argument," and',
              "start having it with themselves."), False)),
           80, 70),
    Favour("bread",
           ("Somebody is dividing a loaf into portions and there is one",
            "too few, which they have noticed and are pretending not to",
            "have.",
            "",
            '"You have not seen me do this," they say.'),
           (("Say nothing and walk on",
             ("You walk on. Behind you the arithmetic continues.",), False),
            ("Give them what you can spare",
             ("It is taken without a word and put with the others, and",
              "the count comes out, and you get looked at exactly once.",
              "",
              "It is a very long look for a small thing."), True)),
           0, 90, kind="toll", needs="potion"),
    Favour("road",
           ("Somebody with a walking stick, going the other way.",
            "",
            '"You have come from the village," they say. "Is the road',
            'still there? All of it?"'),
           (("Say yes",
             ('"Good," they say. "It moves, you know. Not much. Enough."',),
             True),
            ("Ask what that means",
             ('"You have not walked it enough yet," they say, kindly,',
              "and go on walking it."), False)),
           50, 45),

    # **The puzzle-shaped one Savvydev asked for by name** - *"a puzzle task
    # or something like that"* - and it is deliberately trivial. The
    # difficulty is not the point; wanting somebody else present while you
    # do a thing you could do alone is the point.
    Favour("catches",
           ("A box with three catches and no key, and whoever owned it is",
            "three hundred years dead.",
            "",
            '"Turning two of them opens it," they say. "Turning all three',
            'locks it for good. I have turned one. How many more?"'),
           (("One",
             ('"One," they agree. "I knew that. I wanted somebody to say',
              'it out loud while I did it."',
              "",
              "The second catch goes over. The lid stays shut while you",
              "are standing there."), True),
            ("Two",
             ('"That is all three," they say, and put their hands behind',
              'their back like somebody who has nearly touched a fire.'),
             False),
            ("None. Leave it shut.",
             ('"It has been shut three hundred years," they agree. "One',
              'more afternoon will not hurt it."',
              "",
              "They do not move, and they are still there when you look",
              "back from the road."), False)),
           70, 50),

    Favour("roads",
           ("Somebody is drawing in the dirt with a stick and has run out",
            "of room twice.",
            "",
            '"Settle something. How many roads leave this valley? My',
            'brother says five, and my brother is a fool."'),
           (("Five",
             ('"Five," they repeat, wounded. "Five. He is going to be',
              'unbearable about this."'), False),
            ("Six",
             ('"Six," they say. "Six. I have been right since Tuesday and',
              'nobody would say so."'), True),
            ("Seven",
             ('"Seven." They count them out on the ground, twice. "No.',
              'But I like that you tried to give me a spare one."'), False)),
           55, 45, kind="notice"),

    # A toll that wants something other than a potion, so the shape does
    # not read as "the healing item, always".
    Favour("edge",
           ("Somebody is sharpening a blade on a stone that stopped being",
            "a stone some years ago and is now a dish.",
            "",
            '"You would not have a whetstone," they say. "Mine has given',
            'up and I am not going to be the one who tells it."'),
           (("Hand over a whetstone",
             ("They try it on the blade twice and make a noise you have",
              "not previously heard a person make about a rock.",
              "",
              '"Right," they say. "Right. Now I have to do all of them."'),
             True),
            ("Say you have none",
             ('"No," they agree. "Nobody carries one until the day they',
              'need one. That is the entire trouble with them."'), False)),
           0, 70, kind="toll", needs="whetstone"),
]

FAVOUR_BY_KEY = {f.key: f for f in FAVOURS}

# ------------------------------------------------------------- waysides ---
#
# **The last third of a road** (item 12). The one thing left of the two
# hours was that *nothing happens between a waystation and the warden at
# the end of it except walking* - the waystation sits at two thirds, and
# after it the road is a corridor with a boss on the end of it.
#
# A wayside is **a place, not a person**, which is what separates it from
# a favour (§42) - and it has **a decision in it**, which is what
# separates it from a marker (§55). Somebody stopped here before you, and
# what they left is worth something and costs something.
#
# Three rules that are the whole design:
#
# 1. **Every option is a real option.** No wayside has a right answer;
#    walking on is always one of the choices and it is never the wrong
#    one. A favour is a riddle you can fail. This is a decision you make.
# 2. **It costs what it gives.** Gold, a potion, health, or a fight you
#    chose - the road stops being free, which is the whole complaint.
# 3. **It is not story.** No ending leans on one, nothing reaches the
#    meta profile, and `TestNoWaysideTouchesTheStory` enforces it - the
#    same rule §55 gave the readable markers and for the same reason: the
#    road can grow without changing what an earlier ending meant (§42).


@dataclass(frozen=True)
class Wayside:
    """Somewhere on a road worth stopping at.

    `options` are `(label, reply, effect)`, and `effect` is a small dict
    the game reads - never a function, because a `Wayside` is data that
    has to survive being looked at by a test, a probe and a save.
    """

    key: str
    title: str               # what the panel calls the place
    prompt: tuple            # what you find
    options: tuple


WAYSIDES = [
    Wayside(
        "fire", "A fire, banked",
        ("Somebody banked this fire before they left, which is a thing",
         "you do for whoever comes next rather than for yourself.",
         "",
         "There is enough in it for one more sit-down."),
        (("Sit a while",
          ("You sit. The road is still there when you get up, and so",
           "are you, which is more than you could say an hour ago."),
          {"rest": 1.0}),
         ("Bank it again and walk on",
          ("You put the turf back over it. Whoever is behind you gets",
           "the same hour you did not take."),
          {}))),
    Wayside(
        "toll", "A toll-stone",
        ("A stone with a slot cut in the top of it, and a hand painted",
         "on the side pointing at the slot. No house, no keeper, no",
         "gate - just the stone, and the road going past it.",
         "",
         "Somebody has been feeding it. You can hear the coins."),
        (("Pay it (60 g)",
          ("The coin goes in. Nothing happens, and then something",
           "under the stone shifts, and there is a gap at the base",
           "that was not there before."),
          {"gold": -60, "item": 1}),
         ("Walk past",
          ("You walk past. The hand keeps pointing.",), {}))),
    Wayside(
        "cache", "A cairn with a gap in it",
        ("Stones stacked to about knee height, and one of them out of",
         "place - deliberately, the way you leave a thing you mean to",
         "come back for.",
         "",
         "Something is wedged in under the bottom course. Something",
         "else is asleep against the far side of it."),
        (("Take it",
          ("You get it out. The far side of the cairn stops being",
           "asleep."),
          {"item": 1, "fight": 1}),
         ("Restack the stone and go",
          ("You put the stone back the way it was. It is not yours",
           "and neither is what is under it."), {}))),
    Wayside(
        "spring", "A spring, and a cup on a string",
        ("Water coming out of the rock, a cup on a string, and the",
         "string worn through in two places and knotted in three.",
         "",
         "Everybody who comes this way drinks here. You can tell."),
        (("Drink",
          ("It is very cold and tastes of the rock. You feel better",
           "than the walk deserves."),
          {"rest": 0.5}),
         ("Fill a flask for later (a potion)",
          ("You fill what you are carrying. It will not keep long and",
           "it will keep long enough."),
          {"consumable": "potion"}),
         ("Leave it for the next one",
          ("You knot the string again while you are there.",), {}))),
    Wayside(
        "wreck", "A cart, off the road",
        ("A cart on its side with one wheel gone and the other still",
         "turning slightly, which means not long ago.",
         "",
         "There is no driver, no horse, and no blood. The load is",
         "still roped on."),
        (("Cut the ropes and take a share",
          ("You take what one person can carry and leave the rest",
           "roped, which is either decency or superstition."),
          {"item": 1}),
         # **Short enough for a 390px window**, and it is a label rather
         # than a sentence for that reason. `layout.py --phone` caught
         # the long version; ellipsizing it would have cut "and leave
         # everything on it", which is the entire decision (§50).
         ("Right it, and take nothing",
          ("It takes both shoulders and most of your wind. Whoever",
           "comes back for it will not know you were here."),
          {"gold": 40, "xp": 60}),
         ("Leave it alone",
          ("You go around it. The wheel is still turning when you",
           "look back, which it should not be."), {}))),
    Wayside(
        "shrine", "A boundary shrine, half fallen",
        ("A shrine the size of a doorway with nothing in it, at the",
         "point where the road stops belonging to the village and",
         "starts belonging to whatever is at the end of it.",
         "",
         "There are coins in the bowl. Not many, and not recent."),
        (("Leave a coin (30 g)",
          ("You put one in. It sounds exactly like the others, which",
           "is somehow the point."),
          {"gold": -30, "xp": 120}),
         ("Take the coins",
          ("You take them. The bowl is a bowl and the shrine is",
           "stones, and you are still walking a bit faster."),
          {"gold": 90}),
         ("Put your hand on it and go",
          ("The stone is the temperature of the day. Nothing else",
           "happens, which you did not really expect."), {}))),
]

WAYSIDE_BY_KEY = {w.key: w for w in WAYSIDES}


def wayside_options(key):
    """The labelled choices at one wayside. Never raises."""
    stop = WAYSIDE_BY_KEY.get(str(key))
    return stop.options if stop else ()



# ------------------------------------------------------------------ errands ---
#
# **What an errand actually sounds like** (2026-08-30, roadmap item 12).
#
# `quests.ERRAND_DEFS` is the mechanism - a counter, a target and a purse -
# and until now it was also the whole of the writing: the screen said
# *"Defeat 6 monsters out in the wild."* and *"It is done, and she says so
# in about four words."* That is a job board, not a person, and s42's
# density argument only works if the people out there are worth stopping
# for.
#
# So each errand gets three lines of somebody's own voice:
#
#   * `ask`   - why they want it. **The detail line still appears under
#               it**, because a player has to know what the counter is.
#   * `chase` - what they say while it is unfinished. One or two lines: it
#               is the one you will hear most and the one that gets old.
#   * `done`  - what they say when you come back.
#
# **The hard constraint, and it shapes every line: an errand does not know
# who is saying it.** The world hands errands to whoever is spare
# (`World._place_favours`) - a villager, a runner, somebody outside a
# house in a hamlet - so the speaker may be a man, a woman, or "Somebody
# of Two Wells". These are written in the **first person** throughout,
# which sidesteps it entirely and reads better than the alternative. The
# old payout line said *"out of her bag"* and was wrong about half the
# people who said it.
#
# The other rule is s33's: **none of this knows about the wardens.** A
# person on the road has ewes, knees, a daughter and a hill they walk
# round. That is the whole of what the valley knows.


@dataclass(frozen=True)
class ErrandVoice:
    key: str
    ask: tuple
    chase: tuple
    done: tuple
    # **Some of these need somebody who stays put.** A voice that talks
    # about its own ewes, its own knees, or its daughter standing right
    # behind you cannot be handed to one of Maren's couriers, who walks
    # the valley for a living - a runner whose knees have "developed
    # opinions about distance" is exactly the kind of thing Savvydev notices
    # and calls weird, and he would be right.
    # `World._place_favours` deals these to villagers and hamlet folk
    # first. **A preference, not a rule**: a run with nobody settled left
    # over would rather hand a runner an awkward line than drop the
    # errand entirely.
    settled: bool = False


ERRAND_VOICE = [
    ErrandVoice(
        "errand_wolves",
        ("Something has been at the ewes. Not a wolf - a wolf takes one",
         "and goes, and you find the rest where you left them.",
         "",
         "This takes one and leaves the others standing in the field,",
         "and they have not walked back into that field since."),
        ("Still out there. I can tell from the ewes.",),
        ("They went back into the field this morning without being",
         "driven. That is how I know. Nobody came and told me."),
        settled=True),
    ErrandVoice(
        "errand_chests",
        ("There are boxes out on the roads. Under a stone, in a hollow,",
         "always shut, never mine.",
         "",
         "Open two of them and come back. I am not after what is inside.",
         "I want to know somebody has been out that far this year."),
        ("Boxes are patient. That is most of what they are for.",),
        ("Two. Good. Do not tell me what was in them - I asked for the",
         "looking, and the looking is what you did.")),
    ErrandVoice(
        "errand_walk",
        ("My knees have developed opinions about distance.",
         "",
         "Walk it for me. Out past the marker, as far as you feel like",
         "going, and then come back and tell me it is all still in the",
         "order I left it in."),
        ("Not far enough yet. I would be able to tell.",),
        ("Still in that order, then. I did not really doubt it. I wanted",
         "somebody to have checked it this year."),
        settled=True),
    ErrandVoice(
        "errand_crits",
        ("I taught this for a while. Not well, but for a while.",
         "",
         "Eight clean ones. Not eight fights - eight strikes that went",
         "exactly where you meant them. You will know which ones count",
         "and you will not need to ask me."),
        ("You would not be asking me if you had them yet.",),
        ("Eight. And you knew the number without counting on the way in,",
         "which is the part I was actually asking about.")),
    ErrandVoice(
        "errand_gold",
        ("Nobody out here knows what anything is worth any more. We",
         "trade in guesses and we all pretend not to.",
         "",
         "Go and earn six hundred on the road, and come back and tell me",
         "what it took. I will pay you in something that is not coin."),
        ("Not yet. Coin is slow when you are the one watching it.",),
        ("Six hundred, and it took you about as long as I thought it",
         "would. Here. This is worth more than the coin and weighs less.")),
    ErrandVoice(
        "errand_hunt",
        ("There is a hill north of here that people go around instead of",
         "over, and nobody has ever decided to say why.",
         "",
         "Thin out what is between here and the foot of it. Fifteen.",
         "I am not asking you to go up it."),
        ("Fifteen. I did say fifteen.",),
        ("That will do for a season. It is never done - it is only ever",
         "done for a season, and this is one.")),
    ErrandVoice(
        "errand_deep",
        ("There is near work and there is far work, and the far work is",
         "the same work with a much longer walk in front of it.",
         "",
         "Thirty. Nobody out here is going to thank you for it, and I",
         "would rather say that now than afterwards."),
        ("Thirty is meant to take a while. That is the whole shape of",
         "it."),
        ("Nobody is going to thank you. I did say. I am not nobody,",
         "so: thank you.")),
    ErrandVoice(
        "errand_purse",
        ("My daughter wants to leave. Not run off - leave, properly,",
         "with money of her own that nobody handed her.",
         "",
         "I cannot give her that; giving it is the one thing that would",
         "ruin it. But if somebody earns two thousand out there and says",
         "so in front of her, she will believe it can be done."),
        ("Not yet. She has not asked me about it again either, which I",
         "have noticed."),
        ("Say it to her, not to me.",
         "",
         "She is behind you. She has been behind you since you came in."),
        settled=True),
]

ERRAND_VOICE_BY_KEY = {v.key: v for v in ERRAND_VOICE}


def errand_voice(key):
    """What the person handing out this errand says, or None."""
    return ERRAND_VOICE_BY_KEY.get(key)


# ------------------------------------------------------------------ hamlets ---
#
# **The outposts said nothing** (2026-08-30, roadmap item 12). A hamlet is
# two or three roofed houses with somebody standing outside, and that
# somebody was built with an empty `lines` tuple - so they fell through to
# `VILLAGER_LINES`' default and every one of the six said *"Quiet year, so
# far."* Six places with names as good as **The Drowned Rows** and
# **What Is Left of Hale**, and the people in them had one shared line.
#
# One thing each, about the place they live, keyed by its name. The rules:
#
# * **First person plural, no pronoun for the speaker** - the same
#   constraint the errands have and for the same reason (see above).
# * **Nothing about the wardens.** These are people four miles from a
#   thing they have arranged their whole lives around not thinking about,
#   which is `villager_herder`'s joke and is the town's whole character.
# * **The name is the story.** Every one of these explains its own place
#   name, and every explanation is a small disappointment - the hill is a
#   field, the mere is a field, the crossing has one road. That is the
#   valley: things were bigger here once and nobody is upset about it.
#
# **Ruins get nobody, on purpose.** `_build_outpost` only puts a person
# outside a roofed house, and the empty ones are doing the work s42 gives
# them - *"a valley with abandoned places in it has had a past"* - which
# an inhabitant would undo.

# **They have names.** Savvydev, 2026-08-31:
#
# > "bro give the npcs actual names, instead of "A woman off Cobb's Rest"
# > kinda weird"
#
# He is right, and the anonymous version was never a decision - it was
# `_build_outpost` picking from ("A woman", "A man", "Somebody") because
# nobody had written anybody. Everybody else out here is named: the
# keepers are Wenna, Reyd, Hesk, Dray, Ivy; the runners are Pell, Odarry,
# Wick, Sannet, Corvi. A person outside their own house was the only one
# in the valley introduced as a category.
#
# **One name per place, not a name per run.** The hamlet at Two Wells is
# Orrin's every time. A person whose name changes with the seed is not a
# person, and this way a later pass can write *for* Orrin - which is what
# happened to the waystation keepers and is why they are worth talking to.

HAMLET_FOLK = {
    "Two Wells": ("Orrin",
                  ("One well is sweet and one is not, and we have never",
                   "agreed which is which. Both of them get used.")),
    "Underhill": ("Tace",
                  ("We are not under a hill. There was a hill. It is a",
                   "field now, and the name stayed on, the way they do.")),
    "Cobb's Rest": ("Nell",
                    ("Cobb stopped here. That is the whole of it. He",
                     "stopped, and then everybody else stopped, and now",
                     "it is a place with a name.")),
    "Fen End": ("Aldis",
                ("The fen ends here. It has been trying to end somewhere",
                 "further in for about ten years and we keep moving the",
                 "fence to agree with it.")),
    "Stonecross": ("Merrit",
                   ("Two roads crossed here once. One of them is gone.",
                    "We still call it the crossing and still give",
                    "directions off it.")),
    "Mareswood": ("Talla",
                  ("Named for horses or named for a person, depending",
                   "who you ask, and the two families who argue about it",
                   "are related.")),
    "Little Ain": ("Ruan",
                   ("Big Ain is that ruin up the last road. This is the",
                    "little one. Nobody knows which came first and both",
                    "of them are older than the village.")),
    "Byre": ("Ottil",
             ("It is a cattle shed with people living in it. We know.",
              "You get used to saying it.")),
    "Thorncot": ("Corryn",
                 ("Everything here has thorns in it. The hedge, the",
                  "roses, and the family on the end.")),
    "Rushmere": ("Odell",
                 ("There is no mere. There were rushes and there was a",
                  "mere, and now there is neither and a very good field.")),
}

HAMLET_LINES = {place: lines for place, (_name, lines)
                in HAMLET_FOLK.items()}

# **The ruins, and what is left to read at one** (2026-09-05, s55).
# Roadmap item 12b. s44 closed the item's first half - the hamlets speak -
# and left this written down: *"the ruins say nothing on purpose and could
# say something anyway - not a person, but the place itself. `What Is Left
# of Hale` is a name with a story in it and no way to read it."*
#
# **Not a person.** A ruin is empty and stays empty; putting somebody in
# one would make it a hamlet with worse walls. What a ruin has is a marker
# - a doorstep, a boundary stone, a lintel somebody stood back up - and
# the reading of it is the whole interaction.
#
# **Written to the same register as the hamlets**: dry, short, about the
# *name*, and nobody explains anything. The difference is tense. A hamlet
# tells you why it is called that; a ruin tells you what it was called and
# leaves you to notice that the telling is in the past.
#
# **None of them touches the story.** Every one is a village that failed
# for a village-shaped reason - water, fire, soil, debt - because s42's
# rule for anything added between fixed points is that it must not change
# what an earlier ending meant. A ruin that hinted at the Door would be
# the codex leaking into scenery that nobody has earned (s17).
RUIN_MARKERS = {
    "Ashcot": ("A boundary stone, chiselled and then re-chiselled.",
               ("ASHCOT. The second cut is deeper and says the same word,",
                "so somebody came back at least once to insist on it.",
                "",
                "The ash is not from a fire. There is a grey seam under",
                "the whole field and it comes up through everything you",
                "plant in it.")),
    "Old Byre": ("A doorstep, worn into a dip in the middle.",
                 ("Nothing is carved here. The dip is the writing: it",
                  "takes a long time and a great many people to wear a",
                  "stone away like that, and they all went in and out of",
                  "a shed.")),
    "The Drowned Rows": ("A post with notches up it, most under the mud.",
                         ("Somebody marked the water each spring. The",
                          "notches climb, and then stop climbing, which is",
                          "worse - it means the last one was cut by",
                          "somebody who did not come back to cut another.",
                          "",
                          "The rows are still down there in straight lines.",
                          "You can see them when the light is low.")),
    "Nettlefold": ("A gatepost, with a hinge still on it and no gate.",
                   ("The nettles are chest high and they are the only",
                    "thing here that is thriving.",
                    "",
                    "Somebody scratched a tally into the post. Forty-one",
                    "marks, then a gap, then four more in a different",
                    "hand.")),
    # **The one the handoff named.** It gets the longest reading, because
    # the name is the only one on the list that is a sentence, and a name
    # that is a sentence is somebody having already tried to explain.
    "What Is Left of Hale": (
        "A signboard, painted over twice and legible anyway.",
        ("Under the top coat it said HALE. Under that it said HALE",
         "as well, in a steadier hand.",
         "",
         "The name it carries now was not chosen by anybody who lived",
         "here. It is what the next village along started calling it,",
         "and it stuck, the way they do.",
         "",
         "Nobody has painted over it a third time.")),
    "Cinderhome": ("A hearthstone, still in the middle of a floor.",
                   ("The house is gone from around it. The hearth is",
                    "unburnt - whatever took this place took it from",
                    "outside, and the fire in here had been out for a",
                    "while first.")),
    "Grey Acre": ("A marker stone, flat, with a list on it.",
                  ("Eleven names, and then AND THE REST OF US, which is",
                   "either somebody running out of stone or somebody",
                   "running out of time.")),
    "The Empty Fold": ("A drystone wall, still standing, going nowhere.",
                       ("The wall is in excellent repair. It encloses",
                        "four acres of nothing at all and it has outlasted",
                        "everyone who ever leaned on it.")),
}

# **The standing stones, which are older than all of it.** Five circles,
# no names in the world data, so they are told apart by *what is written at
# them* rather than by a place name - and what is written is not writing.
# Nobody knows who raised them, the game never says, and that is the point:
# they are the one thing on the island older than the village, the wardens
# and the Door, and they answer no questions about any of them.
STONE_READINGS = (
    ("Five stones, and a sixth socket with nothing in it.",
     ("The empty socket has been empty long enough to grow moss in.",
      "",
      "Somebody took a stone away, or somebody never finished, and",
      "there is no way at all to tell which from here.")),
    ("The stones are pocked all over with small round holes.",
     ("Cup marks. Every circle on the island has them and no two",
      "circles have them in the same pattern.",
      "",
      "The village will tell you they are for water. The village is",
      "guessing.")),
    ("One stone has fallen and lies where it fell.",
     ("The turf has grown over the top of it, so it fell a long time",
      "ago and nobody living has ever seen this circle whole.",
      "",
      "Nobody has moved it either. That is not laziness - it is four",
      "tons of rock and a long walk from anywhere.")),
    ("The inner faces are smoother than the outer ones.",
     ("Rain does not do that. Hands do that, over a very long time,",
      "on the side you stand when you are facing in.")),
    ("A ring of stones, and the grass inside is a different green.",
     ("Nothing has been planted here and nothing has been ploughed.",
      "The grass simply knows where the edge is.")),
)


def ruin_marker(name):
    """(what stands there, what reading it says) for a ruin, or None."""
    return RUIN_MARKERS.get(name)


def stone_reading(index):
    """One of the five circle readings, chosen by placement order.

    By index rather than at random, so the same circle says the same thing
    every time you walk back to it - the hamlets keep their person across a
    whole run (s44) and a place that changes its mind is not a place.
    """
    return STONE_READINGS[int(index) % len(STONE_READINGS)]


def hamlet_lines(name):
    """What the person who lives in `name` will say."""
    return HAMLET_LINES.get(name, ())


def hamlet_person(place):
    """Who lives at this hamlet: `"Orrin of Two Wells"`, or "".

    **The place stays in the name** because it is the only thing telling
    you where you are - the overworld has no place labels for an outpost -
    and because "Orrin" alone is a stranger while "Orrin of Two Wells" is
    somebody from somewhere.
    """
    found = HAMLET_FOLK.get(place)
    return "%s of %s" % (found[0], place) if found else ''


# ---------------------------------------------------------------- chapters ---
#
# **Roadmap item 10, and the reason it exists.** Savvydev's friend finished
# the whole game and *"had no clue what was really going on"*. Section 33
# explains how: the story is delivered almost entirely through *optional*
# curiosity, so a player who never asks is playing correctly and learns
# nothing. There is no floor under how much story a player receives.
#
# A chapter is the floor. The game already **has** this structure - six
# roads, six wardens, unlocked strictly in order, with `MAREN_BRIEFINGS`
# already written per warden count. What it never did was *say* it. So
# nothing below invents progression; it names the progression already
# there, which is why the item was called mostly presentation.
#
# **The words are Savvydev's now.** They were written by an assistant to get
# the mechanism standing and put to him as a draft; he approved them
# unchanged on 2026-08-29 - *"the titles and goal lines are good"*. So they
# are settled story content and rule 31.7 applies to them like any other:
# changing one is his call, not a tidy-up. Nothing in the code depends on
# their wording - the arc is keyed by warden count - which is what makes
# them safe to change if he ever wants to.
#
# **Two spoiler rules from section 33 apply**, and they shape the data:
#
#   1. **Only the chapter you are in is named.** The ones ahead are not
#      listed anywhere, the way a locked codex row says nothing. A title
#      like "The Door" read three chapters early is a different game.
#   2. **The count is safe, and only because it is already public.** Six
#      wardens is on the save slot rows ("%d/6 wardens") and in Maren's own
#      opening. A total is a spoiler when it announces something unseen
#      (§33); this one announces nothing the player has not been told.


@dataclass(frozen=True)
class Chapter:
    number: int
    title: str
    # One line, in the imperative, saying what to do next. It is what the
    # player reads when they come back after a week away, which is the case
    # that matters - the story is optional, the goal must not be.
    goal: str


CHAPTER_ARC = (
    Chapter(1, "The First Road",
            "Speak to Elder Maren, then take the east road."),
    Chapter(2, "What It Leaves Behind",
            "A road unsealed itself when the first fell. Find it."),
    Chapter(3, "The Weight You Carry",
            "Two Memories are yours. Take the next road."),
    Chapter(4, "Further Than Anyone",
            "Three roads are behind you. Three are not."),
    Chapter(5, "You Could Stop",
            "Two wardens remain. Nobody is making you."),
    Chapter(6, "The Last Door",
            "One warden stands between you and the Door."),
    Chapter(7, "The Sixth Road",
            "The Door is open. Maren asked you to see her first."),
)


def chapter_for(wardens_defeated):
    """Which chapter a run is in, from the one number that decides it.

    Clamped rather than validated: this is called with a count off a world
    that may have come from a save or a socket, and a run that somehow
    reports eight dead wardens is still in the last chapter rather than in
    no chapter at all. A crash here would take the HUD down.
    """
    index = int(max(0, min(len(CHAPTER_ARC) - 1, wardens_defeated)))
    return CHAPTER_ARC[index]


def chapter_total():
    """How many there are. Public - see the note above."""
    return len(CHAPTER_ARC)



@dataclass(frozen=True)
class CodexEntry:
    key: str
    title: str
    chapter: str
    body: tuple
    hint: str = ""     # shown while the entry is still locked


def _warden_entries():
    out = []
    for i, warden in enumerate(WARDENS):
        out.append(CodexEntry(
            warden.memory_key,
            "%s, %s" % (warden.true_name, warden.epitaph),
            "The Wardens",
            warden.memory,
            "Defeat the warden of road %d." % (i + 1)))
    return out


CODEX = _warden_entries() + [
    CodexEntry("valley", "The Reach", "The Valley",
               ("A bowl of good land with one way out and six roads that",
                "pretend to be ways out. Four thousand people, a river, and",
                "a frost that comes every hundred years and would kill every",
                "one of them if the Door were ever to open.",
                "",
                "Everyone here knows about the wardens. Nobody here knows a",
                "warden's name. That is not an accident. It is a policy."),
               "Enter the valley."),
    CodexEntry("anchor", "On Anchors", "The Valley",
               ("A seal is a shape held in place. Something must hold it.",
                "Stone will not; stone forgets. Iron will not; iron rusts.",
                "",
                "A person will, because a person can be told why, and can",
                "keep being told why for as long as somebody keeps walking",
                "up the road to say it.",
                "",
                "That is the whole theory. It is one paragraph long and it",
                "has cost six lives."),
               "Claim two Memories."),
    CodexEntry("bram", "Bram Vance", "The Town",
               ("The smith. Broad, quiet, sixty-one years old, and the only",
                "person in the valley who will say the word warden out loud",
                "in Maren's hearing.",
                "",
                "His brother Ilan took the road three hundred and forty",
                "years ago. Bram is sixty-one. Both of those are true and he",
                "has given up trying to explain it to people.",
                "",
                "The frost does odd things to time near the waypoints. It",
                "does odder things to the ones who wait at the near end."),
               "Speak with the smith."),
    CodexEntry("letters", "Four Thousand Letters", "The Town",
               ("Bram keeps a box. It is empty.",
                "",
                "He has kept it empty a very long time, on the understanding",
                "that one day somebody will come back down the road with",
                "something to put in it.",
                "",
                "If you carry Ilan's Memory, give it to him. He will not",
                "read it in front of you. He will thank you, and go inside,",
                "and shut the door, and you should let him."),
               "Claim Ilan Vance's Memory, then speak to Bram."),
    CodexEntry("maren", "Maren Ain", "The Town",
               ("Twenty-two years old, six hundred years ago. Still",
                "twenty-two in the parts that matter, which she will tell",
                "you is a side effect and not a benefit.",
                "",
                "She has stood at the near end of the road and waved off six",
                "people. She learns all their names first. She keeps them",
                "afterwards. She believes this is decency.",
                "",
                "Ysolde Ain is her sister. Maren has never once said so."),
               "Reach the Door."),
    CodexEntry("cycle", "The Cycle", "The Door",
               ("Anchors fail. Not quickly - forty years, eighty, six",
                "hundred - but they fail, because what is asked of them is",
                "not survivable, only slow.",
                "",
                "When one fails the Door strains, the frost creeps in, and",
                "somebody must go and replace it. Somebody young, and fast,",
                "and willing, and told only the encouraging half.",
                "",
                "You are the seventh. There was never an eighth planned,",
                "because there has never needed to be a plan. There has only",
                "ever needed to be a road, and a woman at the bottom of it."),
               "Learn what the Door is."),
]

CODEX_BY_KEY = {c.key: c for c in CODEX}
CHAPTERS = ("The Wardens", "The Valley", "The Town", "The Door", "Endings")


# ---------------------------------------------------------------- endings ---

@dataclass(frozen=True)
class Ending:
    key: str
    title: str
    subtitle: str
    body: tuple
    requires: tuple = ()       # codex keys that must already be unlocked
    hint: str = ""
    unlocks: str = ""          # class key unlocked by reaching it
    # A hidden ending is not in the list, not in the count, and not hinted
    # at. "3 of 5" would give it away as surely as naming it, so anything
    # that totals endings must use `visible_endings()`.
    hidden: bool = False


ENDINGS = [
    Ending("seventh", "THE SEVENTH", "You took the rope.",
           ("You put your hands where Ysolde's had been. They fit.",
            "",
            "Maren does not thank you, which you appreciate. She writes your",
            "name in a book with six other names, closes it, and walks back",
            "down the road, and does not look up, and does not stop.",
            "",
            "The frost turns back at the pass. Four thousand people wake up",
            "warm and will never know why.",
            "",
            "You have some time now. Four hundred years, at the outside.",
            "Callun sang for the first ten. You might try that.",
            "",
            "Somebody will come up this road eventually. Be kind to them.",
            "They will not know either."),
           hint="Reach the Door and accept the anchor.",
           unlocks="revenant"),
    Ending("breaking", "THE BREAKING", "You let it open.",
           ("You break the anchors. All six. It takes longer than you",
            "expect, because they do not want to come loose, because some",
            "part of each of them is still trying to be useful.",
            "",
            "The Door opens onto exactly nothing. There is no horror behind",
            "it. There was never anything behind it. There was only ever the",
            "cold, and the cold is patient, and now it comes in.",
            "",
            "The valley empties over two winters. Most people make it to the",
            "coast. Bram does not go; he stays in the forge with a box that",
            "is finally not empty.",
            "",
            "Callun. Dorn. Vell. Kessa. Ilan. Ysolde.",
            "Six people stop standing still.",
            "",
            "You are not sure it was worth it. Neither are they.",
            "All six of them say thank you anyway."),
           # Derived from the wardens rather than typed out. The list
           # was identical to this and would have gone quietly stale the
           # moment anybody touched WARDENS - the ending would still be
           # offered while missing a Memory it is *about*.
           requires=tuple(_w.memory_key for _w in WARDENS),
           hint="Read all six Memories, then refuse the anchor at the Door.",
           unlocks="wayfarer"),
    Ending("debt", "THE DEBT", "You sent her instead.",
           ("You have Ysolde's Memory, and Ilan's letters, and the name in",
            "the book, and Maren has run out of encouraging halves.",
            "",
            "She does not argue. That is the worst part. She listens to the",
            "whole of it with her hands folded, and when you finish she says",
            "only: yes. All right. Yes.",
            "",
            "She is the best anchor the Door has ever had. Six hundred years",
            "of practice at not leaving. It holds without straining.",
            "",
            "Ysolde sits down on the step, in the sun, for the first time",
            "since she was twenty-three, and asks you what the town looks",
            "like now, and you tell her, and it takes most of the afternoon.",
            "",
            "Nobody else will ever have to walk up this road.",
            "That is the whole of what you accomplished. It is enough."),
           requires=("memory_ysolde", "maren", "letters"),
           hint="Learn who Maren is, what she did, and who she did it to.",
           unlocks="oathbound"),
    Ending("longwalk", "THE LONG WALK", "You never drew the sword.",
           ("The Hollow Key turns. Six wardens are still standing at their",
            "waypoints and not one of them has been made to explain itself",
            "to you at swordpoint.",
            "",
            "Ysolde looks at your hands, and at the key, and understands",
            "immediately, because she was clever before she was tired.",
            "",
            "She lets you through.",
            "",
            "There is a pass beyond the Door and a road beyond the pass and",
            "the frost has not come yet and will not for eighty years.",
            "You walk out of the valley. Nobody is saved. Nobody is bound.",
            "Nothing is fixed and nothing is broken.",
            "",
            "Behind you, at six waypoints, six people go on standing.",
            "One of them is counting. He is up to four hundred thousand."),
           hint="Open every chest in the valley. Reach the Door with all six "
                "wardens still alive.",
           unlocks="cutpurse"),
]

ENDINGS.append(
    Ending("names", "THE NAMES", "You said them out loud.",
           ("Callun Reed, who counted to four hundred thousand.",
            "Dorn Ashgrave, who built a room with no door and was proud of",
            "the work. Sister Vell, who asked only whether it was still",
            "there. Kessa Vane, who got ninety miles. Ilan Vance, who wrote",
            "four thousand and twelve letters to a dead man because",
            "stopping would have made it true. Ysolde Ain, who went first,",
            "because her sister asked her to.",
            "",
            "You say them slowly. Nobody has said all six aloud since",
            "before Kessa was born.",
            "",
            "The Door does not open so much as admit that it was never",
            "shut. It was held by six people nobody could name, and a",
            "name is the one thing an anchor cannot hold against.",
            "",
            "They go. Not upward, not anywhere - they simply stop being",
            "held, which is all any of them ever asked for.",
            "",
            "Maren is still standing at the bottom of the road. She looks",
            "up as you come down it. You tell her the sixth name last and",
            "watch six hundred years go out of her face.",
            "",
            '"Oh," she says. "Was that all it was."'),
           hint="Listen to all six, then speak at the Door.",
           unlocks="", hidden=True))

ENDINGS_BY_KEY = {e.key: e for e in ENDINGS}


def visible_endings(reached=()):
    """Endings the player is allowed to know exist.

    A hidden one appears only once it has been reached. Everything that
    counts or lists endings must go through here, including totals: the
    number itself is a spoiler.
    """
    got = set(reached or ())
    return [e for e in ENDINGS if not e.hidden or e.key in got]


def ending_available(key, codex_unlocked, wardens_defeated, has_hollow_key):
    """Is this ending offerable right now?"""
    ending = ENDINGS_BY_KEY.get(key)
    if ending is None:
        return False
    if ending is not None and ending.hidden:
        return False          # reached by saying it, never by qualifying
    if key == "longwalk":
        return has_hollow_key and wardens_defeated == 0
    if wardens_defeated < len(WARDENS):
        return False
    return all(req in codex_unlocked for req in ending.requires)


def offered_endings(codex_unlocked, wardens_defeated, has_hollow_key):
    """Endings the player may choose at the Door, always at least one."""
    out = [e for e in ENDINGS
           if ending_available(e.key, codex_unlocked, wardens_defeated,
                               has_hollow_key)]
    return out or [ENDINGS_BY_KEY["seventh"]]


# Codex entries awarded for reaching an ending.
for _ending in ENDINGS:
    CODEX.append(CodexEntry("ending_%s" % _ending.key, _ending.title,
                            "Endings", _ending.body, _ending.hint))
CODEX_BY_KEY = {c.key: c for c in CODEX}

# Codex pages that only exist once their hidden ending has happened. Counting
# them before then shows "0 of 17" against a visible maximum of 16, which both
# leaks the secret and makes the codex look impossible to finish.
HIDDEN_CODEX = {"ending_%s" % e.key for e in ENDINGS if e.hidden}


def visible_codex(known=()):
    got = set(known or ())
    return [c for c in CODEX if c.key not in HIDDEN_CODEX or c.key in got]
