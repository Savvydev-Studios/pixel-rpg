"""Story scenes with choices, and the ledger they feed.

A scene is a moment the story stops and asks you something. What you answer
does three things:

1. It is **said back to you** - the reply is the point, not the button.
2. It **leans the ending.** Nothing here picks an ending outright. Each choice
   adds weight to one of them, and the ending you get is the shape of every
   answer you gave, not the last one. That is why there is no final menu.
3. It is **remembered by the profile**, so the codex can replay the scene
   later showing what you picked and what you passed over.

Two kinds of hidden thing live here:

* A choice with `needs` is not offered until that flag has been granted by
  some earlier choice. Nothing hints that it exists.
* `grants` is how a flag is earned. The six warden scenes each grant a shard
  of `heard_them`; collect all six and the Door offers something that is
  otherwise not in the game at all.

**Nothing in this module is recorded by a marked run.** The guard is in
`MetaProfile`, not here - see `devmenu.py` for why.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# The endings a choice can lean toward. "names" is the hidden one and is
# never listed anywhere the player can see until it has happened.
LEANINGS = ("seventh", "breaking", "debt", "longwalk", "names")


@dataclass(frozen=True)
class Choice:
    key: str                 # unique within its scene
    label: str               # what the button says
    reply: tuple             # what the game says back
    lean: str = ""           # which ending this nudges toward
    weight: int = 1
    needs: str = ""          # flag required before this is even offered
    grants: str = ""         # flag this choice hands out

    @property
    def secret(self):
        return bool(self.needs)


@dataclass(frozen=True)
class Scene:
    key: str
    title: str
    chapter: str
    body: tuple
    choices: tuple = field(default_factory=tuple)

    def offered(self, secrets):
        """The choices visible to somebody holding `secrets`."""
        held = set(secrets or ())
        return [c for c in self.choices if not c.needs or c.needs in held]

    def choice(self, key):
        for option in self.choices:
            if option.key == key:
                return option
        return None


# Six shards, one per warden. Collected only by listening to them die.
HEARD = tuple("heard_%s" % name for name in
              ("callun", "dorn", "vell", "kessa", "ilan", "ysolde"))
ALL_HEARD = "heard_them_all"

# A second, shorter thread, and the one that shows what "a secret choice
# unlocks a secret choice" is for. The weaver mentions black cloth; asking who
# it is for lets you ask Maren the same question; her answer lets you say
# something to Ysolde that is otherwise unsayable. Three scenes, one line of
# curiosity, and none of it is signposted.
MOURNING = "knows_mourning"
KNOWS_YSOLDE = "knows_ysolde"


def _warden_scene(key, title, body, third, lean_third, heard_flag):
    """Every warden death asks the same three questions in its own words.

    The shape is deliberately identical - say nothing, ask, or the one thing
    only this person invites - so that by the third warden the player knows
    the middle option is the one that costs them time and gives them nothing.
    That is the whole trick: it has to look useless.
    """
    return Scene(key, title, "The Wardens", body, (
        Choice("silent", "Say nothing",
               ("You do not answer. There is nothing owed and nothing said.",
                "The quiet afterwards is longer than the fight was."),
               lean="seventh"),
        Choice("listen", "Ask who they were",
               third[1],
               lean="longwalk", grants=heard_flag),
        third[0],
    ))


# ---------------------------------------------------------------------------
# The waystations (s42, roadmap item 12).
#
# **Every one follows the warden-scene shape and inverts its timing.** A
# warden scene asks who somebody *was*, after you have killed them. These
# ask what a place is *doing*, before you know it is connected to anything
# - and the curious option is again the one that costs a moment and appears
# to give nothing.
#
# The rule these are written to, from s33: **a curious option has to be a
# question the player can parse before they know the answer.** "Why won't
# she let anyone help?" parses on the way in. It just does not resolve
# until a Memory answers it, which is the entire point.
#
# None of them lean toward `seventh`. Taking the anchor is what you do when
# you have not looked at anybody, and every one of these is looking.
# ---------------------------------------------------------------------------

WAYSTATION_SCENES = [
    Scene("way_hallow", "Hallow Field", "The Valley",
          ("Old women, a good field, and nobody under fifty.",
           "",
           "One of them is working a strip alone, badly, at an hour when",
           "nobody sensible is out. She has been at it long enough that",
           "the others have stopped offering.",
           "",
           '"The field wants finishing," she says, when you get close',
           "enough to be a problem."),
          (Choice("leave", "Leave her to it",
                  ("You go. Behind you the sound of it continues,",
                   "unhurried, wrong, and not yours."),
                  lean="breaking"),
           Choice("why", "Ask why she will not be helped",
                  ('"Because it is not mine to hand on," she says.',
                   "",
                   "She works another few feet. Then, without turning:",
                   '"It was left. There is a difference between left and',
                   'given, and I am not the one who gets to decide it."'),
                  lean="longwalk"),
           Choice("work", "Work an hour of it with her",
                  ("She does not thank you and she does not stop you.",
                   "",
                   "At the end of the hour she looks at the strip you did,",
                   "and at the strip she did, and something in her face",
                   "gives up a little of whatever it was holding."),
                  lean="debt"))),

    Scene("way_cut", "Ashgrave Cut", "The Valley",
          ("A quarry town, and better stonework than a quarry town has any",
           "business having. Lintels squared to nothing. Corners you could",
           "cut yourself on.",
           "",
           "In the square there is a block of dressed stone the size of a",
           "small room, with no door and no seam, and it is swept."),
          (Choice("shrug", "Leave the block alone",
                  ("You leave it. Somebody comes out later and sweeps",
                   "the step you were standing on."),
                  lean="breaking"),
           Choice("who", "Ask who cut it",
                  ("Nobody knows. That is said quickly, by three people,",
                   "in the same words.",
                   "",
                   "The foreman is slower about it. \'Older work,\' he says.",
                   '"You can tell. He was very good and he was in a hurry',
                   'at the end."'),
                  lean="longwalk"),
           Choice("measure", "Walk the block and measure it",
                  ("Nine feet by nine by nine. Squared to nothing.",
                   "",
                   "A room, then. Somebody built a room and did not put a",
                   "door in it, and did all the work from the inside."),
                  lean="names"))),

    Scene("way_watch", "Vell's Watch", "The Valley",
          ("Every lamp in the town is lit, and it is not dark yet.",
           "",
           "The lamplighter is already on his round. He has the unbothered",
           "speed of a man doing a thing his father did and his",
           "grandmother did.",
           "",
           "The chapel at the end of the row is shuttered."),
          (Choice("pass", "Walk through",
                  ("You pass under a hundred lamps that nobody needs.",
                   "The town behaves as if this is weather."),
                  lean="breaking"),
           Choice("why", "Ask why the lamps",
                  ('"So it is never dark here," he says, pleased to be asked,',
                   "and then his face does something complicated.",
                   "",
                   '"That is the reason I was given. I notice it is not a',
                   'reason. I have been carrying it about forty years."'),
                  lean="longwalk"),
           Choice("chapel", "Try the chapel door",
                  ("Shuttered from the inside. Barred, in fact, and the bar",
                   "was set a very long time ago by somebody who then had",
                   "no way out of the room they were in.",
                   "",
                   "There is no way out of that room. You check twice."),
                  lean="debt"))),

    Scene("way_ninety", "Ninety Mile", "The Valley",
          ("A caravan post at the top of a burnt road. The burning is old -",
           "old enough to be soil - and nothing has grown back.",
           "",
           "The mile-markers along it count down, not up. The nearest says",
           "eleven. You cannot see the far end from here."),
          (Choice("turn", "Turn back to the road you came for",
                  ("You go. The drivers watch you not go down it,",
                   "and approve, and are a little ashamed of approving."),
                  lean="breaking"),
           Choice("markers", "Ask about the markers",
                  ('"Laid wrong," says Dray, too fast.',
                   "",
                   "Then, because you have not said anything:",
                   '"They count down to nothing. To a spot. There is nothing',
                   'at the spot. I have been. There is nothing at the spot."'),
                  lean="longwalk"),
           Choice("walk", "Walk to the last marker",
                  ("It takes most of an afternoon and it says ONE.",
                   "",
                   "Past it the burning stops in a clean line, the way a",
                   "thing stops when whoever was doing it stopped."),
                  lean="names"))),

    Scene("way_relay", "The Relay", "The Valley",
          ("A post house on a road that no longer carries much post.",
           "",
           "The postmaster sorts anyway: held, held, uncollected, held.",
           "One pigeonhole is not a pigeonhole any more. It is a wall of",
           "paper with a name card in a brass frame at the front of it."),
          (Choice("nothing", "Say nothing about the box",
                  ("She sorts. You wait. Neither of you mentions it,",
                   "which takes some doing, given the size of it."),
                  lean="breaking"),
           Choice("box", "Ask about the full box",
                  ('"Held," she says. "For collection."',
                   "",
                   "The card is old enough that the ink has gone brown.",
                   '"He has not come in. I am not going to be the one who',
                   'decides he is not coming in."'),
                  lean="longwalk"),
           Choice("count", "Ask how many",
                  ("She does not have to look it up.",
                   "",
                   '"Four thousand and twelve," she says. "I have not read',
                   'them. Somebody counted them and it was not me, and I',
                   'would like that on the record."'),
                  lean="names"))),

    Scene("way_ain", "Old Ain", "The Valley",
          ("A ruin the valley does not talk about, older than the village",
           "and tidier than it has any right to be.",
           "",
           "Somebody is sweeping a floor that has been open to the sky for",
           "six hundred years. She does not look up.",
           "",
           "By one doorway, two names are scratched at a child's height.",
           "One of them has been worn away by a thumb."),
          (Choice("respect", "Leave her to her sweeping",
                  ("You go quietly, which she notices, and does not",
                   "acknowledge, and is glad of."),
                  lean="breaking"),
           Choice("names", "Ask about the two names",
                  ("The broom stops.",
                   "",
                   '"Two girls lived here," she says. "One of them left."',
                   "",
                   "She starts again. You are not going to get the other",
                   "half and you understand, standing there, that she does",
                   "not have it either."),
                  lean="longwalk"),
           Choice("worn", "Ask which name was worn away",
                  ("She looks at the doorway for a while.",
                   "",
                   '"The one that got touched," she says. "Every day, by',
                   "somebody going past. That is what wears a thing out.",
                   'Not weather. Being loved at.\''),
                  lean="names"))),
]


# ---------------------------------------------------------------------------
# The road back (2026-08-30, roadmap item 12).
#
# **The same six places, after the Memory.** A waystation scene asks a
# question that cannot resolve; this is where it resolves, and the shape is
# deliberately the inverse of everything else in the game:
#
# * A warden scene is somebody dying telling you who they were.
# * A Maren scene is somebody who knows everything choosing what to say.
# * **A resolution scene is the only one where the player knows more than
#   the person they are talking to**, and every choice is about what to do
#   with that. Telling is not automatically the kind option, and the scenes
#   are written so that it costs something either way.
#
# Three rules they are written to:
#
# 1. **Silence is a real answer**, offered first, and it leans `breaking` -
#    the ending for letting a thing be what it is. `after` in `story.py` is
#    written to be true whichever answer was given, because a residue that
#    assumes you spoke lies to the quietest playthrough.
# 2. **The fourth choice needs the shard.** It is offered only to a player
#    who stopped and asked a dying warden who they were - the `listen`
#    option s33 says has to look useless. This is where it stops looking
#    useless, and it is the only reward it has ever had outside the Door.
#    It leans `names` for the same reason the Door does.
# 3. **They may name their warden and they may not name an ending.** The
#    naming is the entire point - the spoiler rule guards the way *out*,
#    not the way back - and `TestResolutionScenes` checks both halves.
#
# **Drafted by an assistant** (s34, 2026-08-29: *"draft the story too, i
# dont really have a story for this one"*). The design underneath is
# Savvydev's and unchanged.
# ---------------------------------------------------------------------------

RESOLUTION_SCENES = [
    Scene("res_hallow", "Hallow Field", "The Valley",
          ("She is out on the strip in the last of the light, doing it",
           "badly, the way she has done it for forty years.",
           "",
           "You have a page in your pack in a boy's handwriting. It is not",
           "addressed to anyone. It asks that his mother be told the field",
           "is finished, and that he finished it.",
           "",
           "She sees you standing at the wall and does not stop."),
          (Choice("go", "Say nothing and go",
                  ("You put the page away. She works until it is too dark",
                   "to work, and then for a while after that.",
                   "",
                   "You are at the gate before you understand that you",
                   "decided something back there rather than put it off."),
                  lean="breaking"),
           Choice("tell", "Tell her the field is finished",
                  ('"By whom," she says. It is not a question.',
                   "",
                   "So you say the rest of it: that he finished it, and",
                   "that he wanted her told. She stands in the row with",
                   "the hoe still in her hands for a long moment.",
                   "",
                   '"Well," she says. "Then I can stop."'),
                  lean="debt"),
           Choice("name", "Say his name",
                  ("Callun. You say it the way it was written, which is",
                   "the way he said it to you, and it is the first time in",
                   "forty years that anybody has said it to her out loud.",
                   "",
                   "She says it back.",
                   "",
                   '"You will have to say the rest now," she says. "You',
                   'cannot start with that and stop."'),
                  lean="longwalk"),
           Choice("counted", "Tell her how he spent it",
                  ("You tell her he sang for ten years, then forgot the",
                   "words and kept the tune for ten more, and then counted,",
                   "because counting has no end. Four hundred thousand.",
                   "",
                   "She does the arithmetic. She is faster at it than you",
                   "were.",
                   "",
                   '"That is about right," she says. "He was a quick boy.',
                   'He was the fastest in this valley and they had him',
                   'stand still."'),
                  lean="names", weight=2, needs=HEARD[0]))),

    Scene("res_cut", "Ashgrave Cut", "The Valley",
          ("The block sits in the square, nine feet by nine by nine, swept",
           "this morning the way it is swept every morning.",
           "",
           "You know what is inside it now. You know the name of the man",
           "who dressed the stone and raised it around himself one course",
           "a year, and you have read him explaining, patiently, why that",
           "took skill.",
           "",
           "Foreman Reyd is doing his round of the yards with a rule in",
           "his hand."),
          (Choice("keep", "Keep it to yourself",
                  ("You say nothing. The town goes on cleaning a thing it",
                   "does not discuss, which it has managed for three",
                   "hundred years and will presumably manage for three",
                   "hundred more.",
                   "",
                   "It is not your square."),
                  lean="breaking"),
           Choice("name", "Tell Reyd who cut it",
                  ('"Dorn," you say. "Dorn Ashgrave."',
                   "",
                   "Reyd looks at you. Then at the block. Then, slowly, up",
                   "at the gate of his own town, which has had that name",
                   "painted across it since before anybody now alive.",
                   "",
                   '"Ah," he says, and sits down on the step, which is',
                   "very good stonework."),
                  lean="longwalk"),
           Choice("inside", "Say who is inside it",
                  ("You tell him it was built from the inside, one stone a",
                   "year, by a man with nothing else to build and nowhere",
                   "he was permitted to want to go.",
                   "",
                   '"We knew that," Reyd says. "We have always known that.',
                   'What we did was agree it was a story."',
                   "",
                   "He puts the rule away without measuring anything."),
                  lean="debt"),
           Choice("chose", "Say which part he chose",
                  ("He was already walled in, you say. The stones were the",
                   "only part of it he picked.",
                   "",
                   "Reyd takes that away across the square with him and",
                   "you can watch him carrying it.",
                   "",
                   'At the far side he stops and says, to nobody: "Then he',
                   'was working. All that time. He was working."'),
                  lean="names", weight=2, needs=HEARD[1]))),

    Scene("res_watch", "Vell's Watch", "The Valley",
          ("The lamps are going on and it is not dark yet. It has not been",
           "dark in this town for a hundred and twenty years.",
           "",
           "From the top of the street you can see the waypoint on the",
           "ridge. It faces the town. Whoever stood up there stood looking",
           "down at every window in it.",
           "",
           "You have read what she did about that, in the ninth year, with",
           "her own hands, on purpose, and that she would do it again."),
          (Choice("walk", "Walk on",
                  ("You let him finish his round: a hundred lamps nobody",
                   "needs, lit briskly, by a man who is good at it.",
                   "",
                   "It costs the town four gallons of oil a night and they",
                   "have never once discussed stopping."),
                  lean="breaking"),
           Choice("for", "Say what the lamps were for",
                  ('"So she could see us," Hesk says - and then hears it,',
                   "and stops with the taper still burning.",
                   "",
                   '"My father lit these. His mother lit these. Forty',
                   'years I have carried a reason that was not a reason,',
                   'and it was that. It was only ever that."'),
                  lean="longwalk"),
           Choice("blind", "Say that she could not see",
                  ('"Sister Vell," you say - a name he has said every day',
                   "of his life without once meaning a person by it.",
                   "",
                   "Then the rest of it: the ninth year, her own thumbs, on",
                   "purpose, so that she would not have to watch the town",
                   "go on without her.",
                   "",
                   "Hesk lights the next lamp, and the next, and gets to",
                   "the end of the row before he answers.",
                   "",
                   '"Then she heard us," he says. "A man with a ladder,',
                   'every night, for a hundred and eleven years. She will',
                   'have heard that."'),
                  lean="debt"),
           Choice("asked", "Say what she asked about",
                  ("She never asked what the town looked like, you say. In",
                   "a hundred and twenty years she asked one question about",
                   "it, once, and the question was whether it was still",
                   "there.",
                   "",
                   "Hesk looks down his own street: the lamps, the",
                   "shutters, the chapel still barred from the inside by",
                   "somebody nothing you have read explains.",
                   "",
                   '"It is still here," he says, to nobody. "It is still',
                   'here."'),
                  lean="names", weight=2, needs=HEARD[2]))),

    Scene("res_ninety", "Ninety Mile", "The Valley",
          ("The burnt ground starts a hundred yards past the yard and does",
           "not stop. Two hundred years, and nothing has come back.",
           "",
           "You know who burned it now. Eleven days for her, nine for the",
           "road, and the road grew back in a season, because grass does",
           "not care about arguments.",
           "",
           "Dray is roping a load he will take as far as the last marker."),
          (Choice("load", "Help with the load",
                  ("You rope the far side. He is glad of the help and does",
                   "not ask what you were about to say.",
                   "",
                   "At the last marker he turns the cart, the way he has",
                   "always turned the cart."),
                  lean="breaking"),
           Choice("laid", "Say who laid the markers",
                  ('"Kessa Vane," you say. "She laid them going out,',
                   'counting down. She was not measuring the road. She was',
                   'measuring what was left of it."',
                   "",
                   "Dray has never heard the name before in his life. You",
                   "can watch him decide to keep it.",
                   "",
                   '"Then the last one is not a place," he says. "It is a',
                   'number somebody got to." He does not sound comforted.',
                   "He sounds relieved, which is a different thing."),
                  lean="longwalk"),
           Choice("spot", "Say what is at the spot",
                  ("Nothing, you say. The road was never what held her.",
                   "She went ninety miles to be told the walk was",
                   "decoration.",
                   "",
                   '"So it would not have mattered," says Dray. "Any of',
                   'it."',
                   "",
                   "He stands at the yard gate looking down the burnt line",
                   "for a while, then comes back and finishes the load."),
                  lean="debt"),
           Choice("back", "Say what she never did",
                  ("In two hundred years she never once tried to go back",
                   "the way she came. Out, or nothing. Never back.",
                   "",
                   "Dray does not answer that at all.",
                   "",
                   "In the morning there is a marker in the yard, painted",
                   "by hand, that says NINETY - the wrong number for this",
                   "end of a road that counts down - and he will not",
                   "explain it to the other drivers."),
                  lean="names", weight=2, needs=HEARD[3]))),

    Scene("res_relay", "The Relay", "The Valley",
          ("Held, held, uncollected, held. The wall of paper at the back",
           "of the pigeonhole has not moved since before her grandmother.",
           "",
           "You have been up this road. There were letters under the third",
           "stone at the top of it - four thousand and twelve of them,",
           "written to a man in the village by a brother who could not",
           "send them.",
           "",
           "The card in the brass frame says Ilan Vance."),
          (Choice("quiet", "Say nothing about the box",
                  ("She sorts. You watch her file two more items into a",
                   "box nobody has opened in three hundred years, without",
                   "hesitating, because that is where they go.",
                   "",
                   "You leave with your half of it still in your pack."),
                  lean="breaking"),
           Choice("count", "Tell her your number",
                  ('"Four thousand and twelve," you say.',
                   "",
                   "Ivy stops with a bundle half-sorted.",
                   "",
                   '"That is my number," she says. "That is the number in',
                   'the book, for that box." She looks at the wall of',
                   "paper, and then at your pack, and does not ask."),
                  lean="longwalk"),
           Choice("coming", "Say he is not coming in",
                  ("You tell her. She has waited a long time for somebody",
                   "else to be the one who decided it, and now that it is",
                   "decided she does not look grateful.",
                   "",
                   "She lifts the card out of the brass frame, copies the",
                   "name onto clean paper, folds it twice, and puts it in",
                   "her apron pocket.",
                   "",
                   '"There is a man in the village posts to this box," she',
                   'says. "Three or four a year. Same name on him."'),
                  lean="debt"),
           Choice("writing", "Say he was writing back",
                  ("Three hundred and forty years of them, you say. He",
                   "knew there was nobody at the other end - you can tell",
                   "from the middle ones that he knew - and he kept",
                   "writing, because stopping would have made it true.",
                   "",
                   "Ivy sits down on the sorting stool.",
                   "",
                   '"Both ends," she says. "Both ends of the same road,',
                   'for three hundred years, and the post house in the',
                   'middle of it doing its job perfectly."'),
                  lean="names", weight=2, needs=HEARD[4]))),

    Scene("res_ain", "Old Ain", "The Valley",
          ("The ruin is empty. The broom is leaning by the doorway with",
           "the two names on it and there is nobody here at all.",
           "",
           "You know both names now. You know which of them was worn away",
           "by a thumb, and that it was not weather, and whose hand has",
           "been going past it for six hundred years.",
           "",
           "There is an afternoon's dust on the floor, which is the most",
           "it has had in a very long time."),
          (Choice("leave", "Leave it as you found it",
                  ("You go without touching anything, which is what she",
                   "would have wanted and is not why you do it.",
                   "",
                   "The broom is still there a week later."),
                  lean="breaking"),
           Choice("sweep", "Take up the broom",
                  ("It is a good broom, rehandled many times by somebody",
                   "who did not want a new one. You do the near half of",
                   "the floor.",
                   "",
                   "It does not take long and it does not accomplish",
                   "anything, and you are aware of both of those for the",
                   "entire time you are doing it."),
                  lean="debt"),
           Choice("names", "Say both names aloud",
                  ("Ysolde Ain. Maren Ain.",
                   "",
                   "Two girls lived here. One of them left, and the other",
                   "has been coming back to this doorway ever since to put",
                   "her thumb on her sister's name until there was nothing",
                   "left of it to touch.",
                   "",
                   "Saying it out loud does not do anything. You do it",
                   "twice."),
                  lean="longwalk"),
           Choice("properly", "Say what she asked you to say",
                  ('"She did it properly." You say it to an empty room,',
                   "because that was the whole of what she wanted said",
                   "after six hundred years of holding a door shut with",
                   "her hands.",
                   "",
                   "There is one clean patch in the dust by the doorway.",
                   "Thumb-sized, at the height a child scratches a name.",
                   "",
                   "It is fresh. You did not pass anybody on the road."),
                  lean="names", weight=2, needs=HEARD[5]))),
]


SCENES = [
    Scene("prologue", "The Sixth Road", "The Valley",
          ("Elder Maren finds you before you have put your pack down.",
           "",
           '"The Door must be sealed before the frost comes. Six wardens',
           'stand between you and it, and they will not let you pass."',
           "",
           "She has said this before. You can hear it in the shape of it."),
          (Choice("accept", "Say you will go",
                  ('"Good," she says, and does not look relieved.',
                   "She has been ready for a yes for a long time."),
                  lean="seventh"),
           Choice("who", "Ask who the wardens are",
                  ('"People who stopped," she says.',
                   "",
                   "She does not offer more, and you notice that she did not",
                   "say the word heroes, which is what the songs say."),
                  lean="longwalk"),
           Choice("cost", "Ask what it costs",
                  ('"Less than the frost costs," she says.',
                   "",
                   "It is a real answer to a different question, and she",
                   "knows it, and she lets you see that she knows it."),
                  lean="debt"))),

    _warden_scene(
        "warden_callun", "Callun Reed",
        ("The boy stops counting.",
         "",
         '"Oh. Oh, that is better."',
         '"Tell my mother the field is finished. I finished it."'),
        (Choice("field", "Promise to tell her",
                ("You say you will. You do not know her name, or the field,",
                 "or whether either is still there after forty years.",
                 "",
                 "He believes you immediately. That is the worst of it."),
                lean="breaking"),
         ("He was fourteen. He was the fastest runner in the valley, and",
          "they chose him for it, and then they asked him to stand still.",
          "",
          "He counted to four hundred thousand. You do the arithmetic",
          "later and wish you had not.")),
        "breaking", HEARD[0]),

    _warden_scene(
        "warden_dorn", "Dorn Ashgrave",
        ("The mason goes down slowly, the way a wall does.",
         "",
         '"Mind the lintel. It is not set."',
         '"It was never going to be set."'),
        (Choice("stones", "Set the lintel",
                ("You lift it. It is heavier than a stone that size has any",
                 "right to be, and it settles with a sound like a held",
                 "breath let go.",
                 "",
                 "He watches you do it. Then he stops watching."),
                lean="breaking"),
         ("Eighty years, and he built one room, and he built it with no",
          "door, because a mason does poorly with nothing to build and",
          "because a room with a door is a room you can leave.",
          "",
          "He was proud of the work. He wanted you to see the work.")),
        "breaking", HEARD[1]),

    _warden_scene(
        "warden_vell", "Sister Vell",
        ("She does not look at you, and it takes you a moment to understand",
         "that this is not a choice she is making now.",
         "",
         '"Is it still there? The town. Do not describe it."'),
        (Choice("still", "Tell her it is still there",
                ('"Good," she says. "Do not tell me more than that."',
                 "",
                 "You do not. It is the only kindness on offer and she has",
                 "already told you exactly how to give it."),
                lean="longwalk"),
         ("She blinded herself in the ninth year, deliberately, with her",
          "own hands, so that she would not have to watch the town go on",
          "without her.",
          "",
          "A hundred and twenty years. She never asked what it looked",
          "like. She asked, once, whether it was still there.")),
        "longwalk", HEARD[2]),

    _warden_scene(
        "warden_kessa", "Kessa Vane",
        ("The heat goes out of her all at once.",
         "",
         '"I got ninety miles. Did you know that? Ninety."',
         '"It was not the running that failed. It was the road."'),
        (Choice("far", "Tell her ninety miles is far",
                ('"It is not," she says. "But thank you."',
                 "",
                 "She says thank you like somebody using a word they have",
                 "not needed in two hundred years."),
                lean="debt"),
         ("She burned ninety miles of road trying to leave, and the road",
          "kept putting itself back, and she kept burning it.",
          "",
          "Two hundred years. She never once tried to go back the way",
          "she came, which you think about for a long time afterwards.")),
        "debt", HEARD[3]),

    _warden_scene(
        "warden_ilan", "Ilan Vance",
        ("The letters go everywhere when he falls. There are a great many",
         "of them.",
         "",
         '"Bram. Tell Bram I. No. He will be dead. Of course he is dead."'),
        (Choice("letters", "Gather the letters",
                ("You pick them up. It takes a while. Four thousand and",
                 "twelve, though you do not count them; the number is",
                 "written on the last one, in a hand that had stopped",
                 "expecting anybody.",
                 "",
                 "You carry them out. You are not sure why."),
                lean="breaking"),
         ("Three hundred and forty years of letters to a brother he was",
          "sure had died in the first thirty of them.",
          "",
          "He was sure. You can tell from the middle ones. He kept",
          "writing because stopping would have made it true.")),
        "breaking", HEARD[4]),

    _warden_scene(
        "warden_ysolde", "Ysolde Ain",
        ("The first of them. She goes down like someone setting a weight",
         "they have carried a long way carefully on the ground.",
         "",
         '"Six hundred years. Tell my sister I did it properly."'),
        (Choice("sister", "Ask about her sister",
                ('"Maren," she says. "She will still be there. She is very',
                 'good at still being there."',
                 "",
                 "And then, after a while, in a different voice:",
                 '"Do not let her talk you into it. She is very good at',
                 'that too."'),
                lean="debt"),
         ("Ysolde Ain went first, because somebody had to, and because",
          "the one who asked her was her sister.",
          "",
          "Six hundred years. She held longest and she was asked first",
          "and nobody has said her name aloud in four hundred of them.")),
        "debt", HEARD[5]),

    Scene("door", "The Door", "The Door",
          ("The Door is not a door. You had been told it was a door.",
           "",
           "There is a place for hands where hands have been before, worn",
           "smooth and slightly too small, and the frost is two days out,",
           "and Maren is behind you saying your name."),
          (Choice("take", "Put your hands where hers were",
                  ("It fits. Of course it fits. It was always going to fit.",),
                  lean="seventh", weight=2),
           Choice("break", "Break it open",
                  ("You start with the anchors. They do not want to come",
                   "loose. Some of them have been loose for years."),
                  lean="breaking", weight=2),
           Choice("her", "Turn around and look at Maren",
                  ("She stops saying your name.",
                   "",
                   "She has been standing at the bottom of this road for",
                   "six hundred years, sending people up it, and she has",
                   "never once climbed it herself."),
                  lean="debt", weight=2),
           # Only offered to somebody who stopped and listened to all six.
           Choice("names", "Say their names",
                  ("Callun Reed. Dorn Ashgrave. Sister Vell.",
                   "Kessa Vane. Ilan Vance. Ysolde Ain.",
                   "",
                   "You say them slowly, because you are the first person",
                   "in four hundred years who can.",
                   "",
                   "Behind you, Maren has stopped saying yours."),
                  lean="names", weight=6, needs=ALL_HEARD))),
]


# -- the road between wardens ------------------------------------------------
#
# Maren says something different each time you come back, and each time gives
# you a chance to ask the thing she is plainly not saying. The town, by
# contrast, does not know anything is happening at all - which is most of the
# reason for putting the two next to each other.

SCENES += [
    Scene("maren_0", "Elder Maren", "The Valley",
          ('"The east road is open. Something squats at the end of it.',
           'Kill it and the next road unseals itself. That is how it works.',
           'Do not ask me why yet. You would not go."'),
          (Choice("go", "Go without asking",
                  ('"Good," she says.',
                   "It is not clear whether she means it."),
                  lean="seventh"),
           Choice("why", "Ask why anyway",
                  ('"Because I asked you to," she says, "and because you',
                   'said yes, and because the frost does not care which of',
                   'us was honest."'),
                  lean="debt"),
           # The label asks something the player can actually parse.
           # It used to read "Ask what happened to the last one", and
           # nothing Maren has said mentions a person - so "the last
           # one" had no referent and read as a writing slip.
           #
           # Asking who she *sent* fixes that and makes the scene
           # better: the phrase "last one" is now hers, produced
           # unprompted while denying there was one, and `maren_1`
           # echoes her own word back at her rather than the
           # player's. Savvydev flagged it on 2026-08-24.
           Choice("last", "Ask who she sent before you",
                  ("She looks at you for slightly too long.",
                   "",
                   '"There was no last one."'),
                  lean="longwalk"))),

    Scene("maren_1", "Elder Maren", "The Valley",
          ('"Good. You are quicker than the last one."',
           "",
           "She stops. You watch her decide whether to correct it.",
           "",
           '"No. I misspoke. There was no last one."'),
          (Choice("let", "Let it go",
                  ("You let it go. She is grateful and does not say so.",),
                  lean="seventh"),
           Choice("press", "Say that she has said it twice now",
                  ('"I am six hundred years old," she says. "I have said a',
                   'great many things twice."',
                   "",
                   "It is the first true thing she has told you."),
                  lean="debt"),
           Choice("count", "Ask how many there were",
                  ("She does not answer. She counts, though - you can see",
                   "her doing it, and it does not take her long, which is",
                   "somehow worse than if it had."),
                  lean="longwalk"))),

    Scene("maren_2", "Elder Maren", "The Valley",
          ('"You are carrying their Memories. I can see them on you."',
           '"Do not read them. It does not help. I know."'),
          (Choice("obey", "Say you will not read them",
                  ("You lie. She knows you are lying. She lets you.",),
                  lean="seventh"),
           Choice("read", "Say you have already read them",
                  ('"Then you know they were people," she says.',
                   '"That is the part that does not help."'),
                  lean="breaking"),
           Choice("cloth", "Ask who the black cloth is for",
                  ("Everything goes out of her face at once.",
                   "",
                   '"Ysolde," she says. "Ysolde Ain. She was my sister, and',
                   'I have not said her name aloud in four hundred years,',
                   'and I would be grateful if you did not either."',
                   "",
                   'Then, quieter: "Thank you for asking."'),
                  lean="debt", weight=2,
                  needs=MOURNING, grants=KNOWS_YSOLDE))),

    Scene("maren_3", "Elder Maren", "The Valley",
          ('"Three left. You are further than anyone since Ilan."',
           '"Bram will not speak to me. Ask him yourself, if you like."'),
          (Choice("nod", "Nod and go",
                  ("She has already turned away.",), lean="seventh"),
           Choice("bram", "Ask what she did to Bram",
                  ('"I asked his brother to stand in a doorway for three',
                   'hundred and forty years," she says, "and his brother',
                   'said yes, and I let him."'),
                  lean="debt"),
           Choice("ilan", "Ask about Ilan",
                  ('"He wrote letters," she says. "I carried the first few',
                   'up the road myself. Then I stopped, because there was',
                   'nobody left at the other end, and I did not tell him',
                   'that."'),
                  lean="breaking"))),

    Scene("maren_4", "Elder Maren", "The Valley",
          ('"Two. You could stop. People have stopped."',
           '"They lived quite ordinary lives afterwards, down in the town."',
           '"I remember all of their names."'),
          (Choice("on", "Say you are not stopping",
                  ('"No," she agrees. "You are not."',), lean="seventh"),
           Choice("names", "Ask her to say one of them",
                  ("She says a name. You have never heard it. She says it",
                   "the way you would say a word in a language you learned",
                   "as a child and have not used since."),
                  lean="longwalk"),
           Choice("offer", "Ask whether she has ever stopped",
                  ("She has been standing at the bottom of this road for",
                   "six hundred years, sending people up it.",
                   "",
                   '"No," she says. "I have not."'),
                  lean="debt"))),

    Scene("maren_5", "Elder Maren", "The Valley",
          ('"One. Behind the last door is the thing that made all of this."',
           '"When you reach her, it, you will want to listen."',
           '"Do not."'),
          (Choice("agree", "Say you will not listen",
                  ('"Good," she says, and does not believe you.',),
                  lean="seventh"),
           Choice("her", "Ask why she said her",
                  ("She does not answer that either. But she does not",
                   "correct it this time, which she would have, once."),
                  lean="debt"),
           Choice("promise", "Promise to listen",
                  ("She closes her eyes.",
                   "",
                   '"Yes," she says. "I thought you might."'),
                  lean="breaking"))),

    Scene("maren_6", "Elder Maren", "The Valley",
          ('"So. The Door is open."',
           '"Come back to me before you walk through it. Please."',
           '"There is a thing I have not told you, and I would rather say',
           'it than have you find it."'),
          (Choice("listen", "Let her say it",
                  ('"The first one was my sister," she says. "I asked her.',
                   'She said yes because I asked her. Everything after that',
                   'was me trying to make that worth something."'),
                  lean="debt", weight=2),
           Choice("known", "Tell her you already know",
                  ("She takes it better than you expected. She has had a",
                   "long time to prepare for somebody knowing.",
                   "",
                   '"Then you know what to do about it," she says.'),
                  lean="breaking", weight=2),
           Choice("walk", "Walk past her",
                  ("You go up the road. She does not follow. She has never",
                   "once climbed it and she does not start now."),
                  lean="seventh", weight=2))),

    Scene("villager_weaver", "Sena the Weaver", "The Town",
          ("The loom does not stop while she talks to you.",
           "",
           '"Maren buys black cloth every few years."',
           '"I have never asked who for."'),
          (Choice("shrug", "Say it is not your business",
                  ('"No," she agrees, pleasantly. "Nor mine."',)),
           Choice("ask", "Ask who it is for",
                  ('"I said I have never asked." The loom keeps going.',
                   '"You could, though. She is right there."',
                   "",
                   "It had not occurred to you that you could."),
                  grants=MOURNING),
           Choice("often", "Ask how often",
                  ('"Every forty years or so," she says. "Give or take."',
                   "",
                   "She says it like weather. You do the arithmetic on the",
                   "way out and stop halfway."),
                  lean="longwalk"))),

    Scene("villager_child", "Pip", "The Town",
          ('"Are you going up the road?"',
           '"Everyone who goes up the road is very brave."'),
          (Choice("brave", "Say you are very brave",
                  ("She is delighted. She tells three other children. You",
                   "hear about it for the rest of the afternoon.",)),
           Choice("scared", "Say you are frightened",
                  ("She thinks about this seriously.",
                   "",
                   '"That is all right," she decides. "You can be both."'),
                  lean="longwalk"),
           Choice("back", "Ask whether anyone comes back",
                  ("She has to think about it. Then she brightens.",
                   "",
                   '"Elder Maren stayed! She never went at all!"'),
                  lean="debt"))),

    Scene("villager_herder", "Grell the Herder", "The Town",
          ('"Lost two ewes to something up the east road."',
           '"I did not go looking."'),
          (Choice("sensible", "Say that was sensible",
                  ('"It was two ewes," he says. "I have forty."',)),
           Choice("offer", "Offer to go and look",
                  ('"No," he says, fast. Then, embarrassed by how fast:',
                   '"They are only ewes."'),
                  lean="longwalk"),
           Choice("what", "Ask what took them",
                  ("He does not know and does not want to. He has lived",
                   "four miles from it his whole life and has arranged that",
                   "whole life around not knowing."),
                  lean="seventh"))),

    Scene("villager_plain", "Tobin", "The Town",
          ('"Quiet year, so far."',
           '"The wardens keep to their roads. Long may it last."'),
          (Choice("agree", "Agree that it is quiet",
                  ("It is. That is the strange part.",)),
           Choice("roads", "Ask about the roads",
                  ('"They stay on them," he says. "We stay off them. It has',
                   'worked for a very long time."'),
                  lean="seventh"),
           Choice("wardens", "Ask whether he knows what they are",
                  ('"Monsters," he says, comfortably.',
                   "",
                   "You have their Memories in your pack and you do not",
                   "correct him."),
                  lean="breaking"))),
]

# **Keyed by the person's name**, which is a name and not a role since
# 2026-08-31 (Savvydev: *"real names for everyone bro"*). The scene *keys*
# below are unchanged on purpose - they are written into every profile
# that has ever played, and `meta.scene_seen` would forget the lot.
VILLAGER_SCENES = {
    "Sena the Weaver": "villager_weaver",
    "Pip": "villager_child",
    "Grell the Herder": "villager_herder",
    "Tobin": "villager_plain",
}

MAREN_SCENES = {n: "maren_%d" % n for n in range(7)}


# -- orientation -------------------------------------------------------------
#
# The prologue sets a tone. This one answers "what am I supposed to do",
# because atmosphere is not a substitute for a player knowing where to walk.
# It is deliberately blunt and deliberately short, and Maren finds it as
# tiresome to deliver as you would expect.

SCENES += [
    Scene("orientation", "Before you go", "The Valley",
          ("She sees that you have no idea what she means, and sighs.",
           "",
           '"Plainly, then. Six roads lead out of this valley. At the end',
           'of each one is a warden. Kill one and the next road opens.',
           'Kill all six and the Door opens."',
           "",
           '"Between here and there: fight what you meet, and you will get',
           'stronger for it. The smith will improve what you carry. The',
           'trader sells what he has. I have work, if you want paying."',
           "",
           '"The shrine mends you for a coin. Chests are worth opening.',
           'Come back to me when a warden falls."'),
          (Choice("clear", "Say that is clear enough",
                  ('"It is not," she says. "But it will do."',),
                  lean="seventh"),
           Choice("map", "Ask which road first",
                  ('"East," she says. "The others are sealed. They open in',
                   'their own order and not in yours."',
                   "",
                   "She points. You can see the road from here. It does not",
                   "look like anything."),
                  lean="seventh"),
           Choice("refuse", "Ask what happens if you simply do not",
                  ("She considers it properly, which you were not expecting.",
                   "",
                   '"Then the frost comes, and the valley empties, and in',
                   'forty years somebody else stands where you are standing',
                   'and I say all of this again."',
                   "",
                   '"You would be surprised how often I have to think',
                   'about that answer."'),
                  lean="longwalk"))),
]

# The waystations join the register rather than living in a list of their
# own - `play_scene` looks scenes up by key and should not have to know
# which kind it is holding. The same goes for the resolutions, and it is
# what puts both of them in the codex playback list for free (s19 item
# 11): that screen is every scene in `SCENES` the profile has ever seen.
SCENES = SCENES + WAYSTATION_SCENES + RESOLUTION_SCENES

SCENES_BY_KEY = {s.key: s for s in SCENES}
WARDEN_SCENE_BY_BOSS = {
    "sprout_king": "warden_callun",
    "stone_golem": "warden_dorn",
    "night_warden": "warden_vell",
    "inferno_beast": "warden_kessa",
    "sky_tyrant": "warden_ilan",
    "void_sovereign": "warden_ysolde",
}


def grant_for(secrets, granted):
    """Add a flag, and the six-shard flag once every shard is held.

    Returns the flags that are newly held, so the caller can say so.
    """
    held = set(secrets or ())
    fresh = []
    if granted and granted not in held:
        held.add(granted)
        fresh.append(granted)
    if all(shard in held for shard in HEARD) and ALL_HEARD not in held:
        held.add(ALL_HEARD)
        fresh.append(ALL_HEARD)
    return sorted(held), fresh


def leaning_totals(ledger):
    """The ledger as a sorted list of (ending, weight), heaviest first."""
    return sorted(((k, v) for k, v in (ledger or {}).items() if v > 0),
                  key=lambda pair: (-pair[1], pair[0]))


def leading_ending(ledger):
    """Which ending the answers so far are pointing at, or "" for none."""
    totals = leaning_totals(ledger)
    return totals[0][0] if totals else ""
