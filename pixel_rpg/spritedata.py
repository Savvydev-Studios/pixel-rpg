"""Hand-authored pixel art, stored as character grids.

Keeping the art as text means the whole game ships as source with zero binary
assets: nothing to lose, nothing to license, and every sprite is diffable.

A single shared palette keyed by character is used by every sprite, so the
whole cast stays colour-coordinated by construction.
"""
from __future__ import annotations

# "." and " " are transparent. Upper case is generally the lighter shade.
PALETTE = {
    "k": (16, 14, 22),        # outline / near-black
    "d": (38, 34, 50),        # deep shadow
    "w": (242, 242, 250),     # white
    "e": (250, 250, 255),     # eye white
    "a": (92, 96, 110),       # grey dark
    "A": (156, 162, 178),     # grey light
    "n": (86, 58, 36),        # brown dark
    "N": (140, 96, 58),       # brown light
    "s": (196, 140, 104),     # skin shadow
    "S": (238, 194, 152),     # skin light
    "r": (150, 38, 44),       # red dark
    "R": (226, 74, 66),       # red bright
    "o": (196, 96, 28),       # orange dark
    "O": (250, 158, 52),      # orange bright
    "y": (206, 168, 40),      # yellow dark
    "Y": (252, 226, 104),     # yellow bright
    "g": (44, 110, 58),       # green dark
    "G": (92, 190, 96),       # green bright
    "b": (38, 74, 148),       # blue dark
    "B": (86, 152, 236),      # blue bright
    "c": (44, 148, 158),      # cyan dark
    "C": (114, 224, 232),     # cyan bright
    "p": (86, 46, 130),       # purple dark
    "P": (162, 100, 226),     # purple bright
    "m": (214, 108, 168),     # pink
    "t": (58, 42, 30),        # bark
    "l": (60, 46, 84),        # cloth dark
    "L": (104, 84, 140),      # cloth light
    # Mid tones, so foliage and stone can be shaded rather than flat-filled.
    "v": (62, 140, 72),       # green mid
    "V": (132, 214, 130),     # green highlight
    "T": (104, 76, 48),       # bark light
    "q": (122, 128, 144),     # grey mid
}

# ------------------------------------------------------------------- hero ---
# --------------------------------------------------------------- monsters ---

# ------------------------------------------------------------- bosses 16x16 ---
SPROUT_KING = [
    "......GGGG......",
    ".....GGGGGG.....",
    "..G..gGGGGg..G..",
    ".GGG.gggggg.GGG.",
    "..G...gYYg...G..",
    "....ggGGGGgg....",
    "...gGGGGGGGGg...",
    "..gGGkGGGGkGGg..",
    "..gGGGGGGGGGGg..",
    "..gGGGGwwGGGGg..",
    "..gGGGGGGGGGGg..",
    "...gGGGGGGGGg...",
    "....gggGGggg....",
    "...nn..gg..nn...",
    "..nnn..nn..nnn..",
    "...n........n...",
]

STONE_GOLEM = [
    "....aaaaaaaa....",
    "...aAAAAAAAAa...",
    "..aAAAAAAAAAAa..",
    "..aAAkAAAAkAAa..",
    "..aAAAAAAAAAAa..",
    "..aAAAAOOAAAAa..",
    "..aAAAAAAAAAAa..",
    "aaaAAAAAAAAAAaaa",
    "aAaaAAAAAAAAaaAa",
    "aAaaAAAOOAAAaaAa",
    "aAa.aAAAAAAa.aAa",
    "aAa.aAAAAAAa.aAa",
    "aaa.aAAAAAAa.aaa",
    "....aaAAAAaa....",
    "...aaa....aaa...",
    "..aaaa....aaaa..",
]

NIGHT_WARDEN = [
    ".......dd.......",
    "......dddd......",
    ".....dllllld....",
    "....dlLLLLLld...",
    "...dlLLLLLLLld..",
    "...lLLdddddLLl..",
    "...lLdCCddCCdLl.",
    "...lLLdddddLLl..",
    "..dllLLLLLLLlld.",
    ".dllllLLLLllllld",
    "dlll.dLLLLd.llld",
    "ll....dLLd....ll",
    "l.....dLLd.....l",
    "......dllld.....",
    ".....dll.lld....",
    "....dll...lld...",
]

INFERNO_BEAST = [
    "..O..........O..",
    "..oO........Oo..",
    "..ooO..OO..Ooo..",
    "...oOOoooOOo....",
    "...ooOOOOOOoo...",
    "..oOOrrOOrrOOo..",
    "..oOrRkOOkRrOo..",
    "..oOOrrOOrrOOo..",
    ".ooOOOOwwOOOOoo.",
    ".oOOOOwwwwOOOOo.",
    "ooOOOOOOOOOOOOoo",
    "oOOrrOOOOOOrrOOo",
    "oOOOOOOOOOOOOOOo",
    ".oOO.OOOO.OOOo..",
    "..oo..oo..oo....",
    "..o....o....o...",
]

SKY_TYRANT = [
    "...B..........B.",
    "..BB..........BB",
    ".BBBb........bBBB"[:16],
    "BBBBbb......bbBBB"[:16],
    "BBBBBb.CCCC.bBBBB"[:16],
    ".BBBb.CCCCCC.bBBB"[:16],
    "..Bb.CCkCCkCC.bB."[:16],
    ".....CCCCCCCC...",
    "....CCCCwwCCCC..",
    "....bCCCCCCCCb..",
    "...bbCCCCCCCCbb.",
    "..bBBBCCCCCCBBb.",
    "...BB.CCCCCC.BB.",
    "......yCCCCy....",
    ".....yy....yy...",
    "....yy......yy..",
]

VOID_SOVEREIGN = [
    "......pppp......",
    "....pppPPpppp...",
    "...ppPPPPPPpp...",
    "..ppPPPPPPPPpp..",
    "..pPPmPPPPmPPp..",
    "..pPPPPPPPPPPp..",
    ".ppPPPPwwPPPPpp.",
    ".pPPPPPPPPPPPPp.",
    "pPPPPmPPPPmPPPPp",
    "pPPPPPPPPPPPPPPp",
    "pPPPdPPPPPPdPPPp",
    ".pPPPddddddPPPp.",
    "..pPPPPPPPPPPp..",
    "...pppPPPPppp...",
    "....dd.pp.dd....",
    "...dd...d...dd..",
]

# ------------------------------------------------------------------ props ---
TREE = [
    "......vvvv......",
    "....vvVVVVvv....",
    "...vVVVVVVVVv...",
    "..vVVVGGGGVVVv..",
    "..vVVGGGGGGVVv..",
    ".vVVGGGGGGGGVVv.",
    ".vVGGGGGGGGGGVv.",
    ".vGGGGGGGGGGGgv.",
    ".vGGGGGGGGGgggv.",
    "..vGGGGGGGgggv..",
    "..vvGGGGGgggvv..",
    "...vvgggggggv...",
    "....vvgggggv....",
    "......TttT......",
    "......TttT......",
    ".....TTttTT.....",
]

ROCK = [
    "................",
    "................",
    "................",
    "................",
    "................",
    ".......AAAA.....",
    ".....AAAAAAA....",
    "....AAAAAAAAq...",
    "...AAAAAAAqqqa..",
    "..AAAAAAqqqqqa..",
    "..AAAAqqqqqqqa..",
    "..aqqqqqqqqqqa..",
    "..aaqqqqqqqqaa..",
    "...aaaaaaaaaa...",
    "....dddddddd....",
    "................",
]

FLOWER = [
    "................",
    "................",
    "................",
    "................",
    "......RR........",
    ".....RYYR....RR.",
    "....RRYYRR..RmmR",
    ".....RYYR...RmmR",
    "......vv.....RR.",
    ".....Gvv......v.",
    "......vv.....Gv.",
    ".....Gvvv.....v.",
    "......vv.....Gv.",
    "....GGvvGG....v.",
    "......vv......v.",
    "................",
]

BOSSES = {
    "sprout_king": SPROUT_KING, "stone_golem": STONE_GOLEM,
    "night_warden": NIGHT_WARDEN, "inferno_beast": INFERNO_BEAST,
    "sky_tyrant": SKY_TYRANT, "void_sovereign": VOID_SOVEREIGN,
}


# ===========================================================================
# 16x16 art
#
# Everything below supersedes the 12x12 grids above. The cast was split
# between 12x12 characters and 16x16 bosses and scenery, which read as two
# different games sitting next to each other. One grid size for everything.
# ===========================================================================

HERO16_FRONT = [
    "................",
    ".....nnnnnn.....",
    "....nnnnnnnn....",
    "...nnnnnnnnnn...",
    "...nnSSSSSSnn...",
    "...nSSSSSSSSn...",
    "...SSkSSSSkSS...",
    "...SSSSSSSSSS...",
    "....SSSrrSSS....",
    ".....SSSSSS.....",
    "...ABBBBBBBBA...",
    "..AABBBBBBBBAA..",
    "..AyBBBBBBBByA..",
    "...BBBBBBBBBB...",
    "....ll....ll....",
    "....tt....tt....",
]

HERO16_BACK = [
    "................",
    ".....nnnnnn.....",
    "....nnnnnnnn....",
    "...nnnnnnnnnn...",
    "...nnnnnnnnnn...",
    "...nnnnnnnnnn...",
    "...nnnnnnnnnn...",
    "...SnnnnnnnnS...",
    "....SnnnnnnS....",
    ".....SSSSSS.....",
    "...ABBBBBBBBA...",
    "..AABBBBBBBBAA..",
    "..AyBBBBBBBByA..",
    "...BBBBBBBBBB...",
    "....ll....ll....",
    "....tt....tt....",
]

HERO16_SIDE = [
    "................",
    "....nnnnnn......",
    "...nnnnnnnn.....",
    "..nnnnnnnnnn....",
    "..nnSSSSSSS.....",
    "..nSSSSSSSS.....",
    "..SSkSSSSSS.....",
    "..SSSSSSSSS.....",
    "...SSSSrrS......",
    "....SSSSSS......",
    "..ABBBBBBBA.....",
    ".AABBBBBBBAA....",
    ".AyBBBBBBBy.....",
    "..BBBBBBBBB.....",
    "...ll..ll.......",
    "...tt..tt.......",
]

# Walk cycle. Only the leg and arm rows change, so a frame is two row swaps
# rather than a whole redrawn sprite.
WALK_LEGS = {
    "front": [
        ("....ll....ll....", "....tt....tt...."),   # stand
        ("....lll..ll.....", "....ttt...tt...."),   # left forward
        ("....ll....ll....", "....tt....tt...."),   # pass
        (".....ll..lll....", "....tt...ttt...."),   # right forward
    ],
    "back": [
        ("....ll....ll....", "....tt....tt...."),
        ("....lll..ll.....", "....ttt...tt...."),
        ("....ll....ll....", "....tt....tt...."),
        (".....ll..lll....", "....tt...ttt...."),
    ],
    "side": [
        ("...ll..ll.......", "...tt..tt......."),
        ("..lll...ll......", "..ttt...tt......"),
        ("...ll..ll.......", "...tt..tt......."),
        ("...ll...lll.....", "...tt...ttt....."),
    ],
}

# Body silhouette. Shoulders and waist rows differ; everything else is shared.
BUILD_ROWS = {
    "feminine": {
        "front": {10: "....BBBBBBBB....", 11: "...ABBBBBBBBA...",
                  12: "...AyBBBBBByA...", 13: "...BBBBBBBBBB..."},
        "back": {10: "....BBBBBBBB....", 11: "...ABBBBBBBBA...",
                 12: "...AyBBBBBByA...", 13: "...BBBBBBBBBB..."},
        "side": {10: "...BBBBBBBB.....", 11: "..ABBBBBBBA.....",
                 12: "..AyBBBBBy......", 13: "..BBBBBBBBB....."},
    },
    "androgynous": {},          # the base grids as drawn
    "masculine": {
        "front": {10: "..ABBBBBBBBBBA..", 11: ".AABBBBBBBBBBAA.",
                  12: ".AyBBBBBBBBBByA.", 13: "..BBBBBBBBBBBB.."},
        "back": {10: "..ABBBBBBBBBBA..", 11: ".AABBBBBBBBBBAA.",
                 12: ".AyBBBBBBBBBByA.", 13: "..BBBBBBBBBBBB.."},
        "side": {10: ".ABBBBBBBBA.....", 11: "AABBBBBBBBAA....",
                 12: "AyBBBBBBBBy.....", 13: ".BBBBBBBBBB....."},
    },
}


SLIME16 = [
    "................",
    "................",
    "......GGGG......",
    "....GGGGGGGG....",
    "...GGGGGGGGGG...",
    "..GGGGGGGGGGGG..",
    "..GGkGGGGGGkGG..",
    ".GGGGGGGGGGGGGG.",
    ".GGGGGGGGGGGGGG.",
    ".GGGGGGwwGGGGGG.",
    ".GGGGGGGGGGGGGG.",
    ".gGGGGGGGGGGGGg.",
    ".ggGGGGGGGGGGgg.",
    "..gggggggggggg..",
    "...gggggggggg...",
    "................",
]

BAT16 = [
    "................",
    "..p..........p..",
    ".pp....pp....pp.",
    "ppp...pPPp...ppp",
    "pppp.pPPPPp.pppp",
    "ppppppPPPPpppppp",
    ".ppppPPPPPPpppp.",
    "..pppPkPPkPppp..",
    "...ppPPPPPPpp...",
    "....pPPwwPPp....",
    "....pPPPPPPp....",
    ".....pPPPPp.....",
    "......pPPp......",
    ".......pp.......",
    "................",
    "................",
]

RAT16 = [
    "................",
    "................",
    "...AA...........",
    "..AAAA.......a..",
    "..AAAAAA....aa..",
    ".AAkAAAAA..aa...",
    ".AAAAAAAAAaa....",
    "mAAAAAAAAAA.....",
    ".AAAAAAAAAAA....",
    ".AAAAAAAAAAAA...",
    ".AAAAAAAAAAAA...",
    "..AA.AA.AA.AA...",
    "..A...A..A..A...",
    "................",
    "................",
    "................",
]

IMP16 = [
    "................",
    "....r......r....",
    "....rr....rr....",
    ".....rrrrrr.....",
    "....rRRRRRRr....",
    "...rRRRRRRRRr...",
    "...RRkRRRRkRR...",
    "...RRRRRRRRRR...",
    "....RRRwwRRR....",
    "..ooRRRRRRRRoo..",
    ".oooRRRRRRRRooo.",
    "..ooRRRRRRRRoo..",
    "....RRRRRRRR....",
    "....rr....rr....",
    "....r......r....",
    "................",
]

WOLF16 = [
    "................",
    "..n..........n..",
    "..nn........nn..",
    "..nNn......nNn..",
    "..nNNNNNNNNNNn..",
    "..NNkNNNNNNkNN..",
    ".NNNNNNNNNNNNNN.",
    "NNwwNNNNNNNNNNNn",
    ".NNNNNNNNNNNNNN.",
    ".NNNNNNNNNNNNNN.",
    "..NNNNNNNNNNNN..",
    "..NN.NN..NN.NN..",
    "..n...n..n...n..",
    "................",
    "................",
    "................",
]

WISP16 = [
    "................",
    "......CC........",
    ".....CCCC....c..",
    "....CCCCCC...c..",
    "...CCCCCCCC.....",
    "...CCkCCCCC.....",
    "..CCCCCCCCCC..c.",
    "..CCCCCCCCCC..c.",
    "..CCCCCCCCCC....",
    "...CCCCCCCC..c..",
    "....CCCCCC...c..",
    ".....CCCC.......",
    "......CC........",
    "................",
    "................",
    "................",
]

SPIDER16 = [
    "................",
    "d..d........d..d",
    ".d.d........d.d.",
    "..ddd......ddd..",
    "...dddd..dddd...",
    "....dpppppppd...",
    "...dpPPPPPPPpd..",
    "..dpPPkPPkPPPpd.",
    "..dpPPPPPPPPPpd.",
    "...dpPPPwwPPpd..",
    "....dpPPPPPpd...",
    "...dddpppppddd..",
    "..ddd......ddd..",
    ".d.d........d.d.",
    "d..d........d..d",
    "................",
]


CHEST16 = [
    "................",
    "................",
    "................",
    "......nnnn......",
    "....nnNNNNnn....",
    "...nNNNNNNNNn...",
    "..nNNNNNNNNNNn..",
    "..naaNNNNNNaan..",
    "..nNNNNyyNNNNn..",
    "..nNNNNyYyNNNn..",
    "..naaNNykyNNaan.",
    "..nNNNNNNNNNNn..",
    "..nNNNNNNNNNNn..",
    "..nnnnnnnnnnnn..",
    "...dddddddddd...",
    "................",
]

CHEST_OPEN16 = [
    "................",
    "......nnnn......",
    "....nnNNNNnn....",
    "...nNNNNNNNNn...",
    "..nnnnnnnnnnnn..",
    "................",
    ".....YYYYYY.....",
    "....YYyYYyYY....",
    "..nYYYYYYYYYYn..",
    "..naaNNNNNNaan..",
    "..nNNNNyyNNNNn..",
    "..nNNNNNNNNNNn..",
    "..nNNNNNNNNNNn..",
    "..nnnnnnnnnnnn..",
    "...dddddddddd...",
    "................",
]

ELDER16 = [
    "................",
    ".....wwwwww.....",
    "....wwwwwwww....",
    "...wwSSSSSSww...",
    "...wSSSSSSSSw...",
    "...SSkSSSSkSS...",
    "...SSSSSSSSSS...",
    "....SSwwwwSS....",
    "....wwwwwwww....",
    "...LLLLLLLLLL...",
    "..LLLLLLLLLLLL..",
    "..LLLLLLLLLLLL..",
    "...LLLLLLLLLL...",
    "....LLLLLLLL....",
    "....ll....ll....",
    "....tt....tt....",
]

SMITH16 = [
    "................",
    "................",
    "....aaaaaaaa....",
    "...aaSSSSSSaa...",
    "...aSSSSSSSSa...",
    "...SSkSSSSkSS...",
    "...SSSSSSSSSS...",
    "....SSnnnnSS....",
    ".....SSSSSS.....",
    "..rrrrrrrrrrrr..",
    ".rrrrrrrrrrrrrrA",
    "..rrrrrrrrrrrrAA",
    "..rrrrrrrrrrrr.A",
    "...rrrrrrrrrr...",
    "....tt....tt....",
    "....tt....tt....",
]

SHRINE16 = [
    "................",
    "................",
    "......CCCC......",
    ".....CCCCCC.....",
    "....CCCCCCCC....",
    ".....CCCCCC.....",
    "......CCCC......",
    ".......CC.......",
    "....AAAAAAAA....",
    "...AAAAAAAAAA...",
    "..AAAAAAAAAAAA..",
    "..AAaaaaaaaaAA..",
    "..AAaaaaaaaaAA..",
    "..AAAAAAAAAAAA..",
    ".AAAAAAAAAAAAAA.",
    "..dddddddddddd..",
]

PORTAL16 = [
    "................",
    "......pppp......",
    "....ppPPPPpp....",
    "...pPPddddPPp...",
    "..pPPddCCddPPp..",
    "..pPdCCCCCCdPp..",
    ".pPdCCCCCCCCdPp.",
    ".pPdCCCwwCCCdPp.",
    ".pPdCCCwwCCCdPp.",
    ".pPdCCCCCCCCdPp.",
    "..pPdCCCCCCdPp..",
    "..pPPddCCddPPp..",
    "...pPPddddPPp...",
    "....ppPPPPpp....",
    "......pppp......",
    "................",
]


# Second idle frame for the creatures whose motion is a pose change rather
# than a wobble. The rest are animated procedurally in `assets`.
SLIME16_ALT = [
    "................",
    "................",
    "................",
    "................",
    "......GGGG......",
    "....GGGGGGGG....",
    "..GGGGGGGGGGGG..",
    "..GGkGGGGGGkGG..",
    ".GGGGGGGGGGGGGG.",
    ".GGGGGGwwGGGGGG.",
    ".GGGGGGGGGGGGGG.",
    "gGGGGGGGGGGGGGGg",
    "ggGGGGGGGGGGGGgg",
    "gggggggggggggggg",
    "..gggggggggggg..",
    "................",
]

BAT16_ALT = [
    "................",
    "................",
    "................",
    ".......pp.......",
    "ppp...pPPp...ppp",
    "pppp.pPPPPp.pppp",
    ".ppppPPPPPPpppp.",
    "..pppPkPPkPppp..",
    "..pppPPPPPPppp..",
    ".pppPPPwwPPPppp.",
    "ppp.pPPPPPPp.ppp",
    "pp...pPPPPp...pp",
    "p.....pPPp.....p",
    ".......pp.......",
    "................",
    "................",
]

WISP16_ALT = [
    "................",
    "..c...CC........",
    "..c..CCCC.......",
    "....CCCCCC...c..",
    "...CCCCCCCC..c..",
    "...CCkCCCCC.....",
    "..CCCCCCCCCC....",
    "..CCCCCCCCCC....",
    "..CCCCCCCCCC..c.",
    "...CCCCCCCC...c.",
    "....CCCCCC......",
    ".c...CCCC.......",
    ".c....CC........",
    "................",
    "................",
    "................",
]

SPIDER16_ALT = [
    "................",
    "................",
    "d..d........d..d",
    ".d.dd......dd.d.",
    "..dddd....dddd..",
    "....dpppppppd...",
    "...dpPPPPPPPpd..",
    "..dpPPkPPkPPPpd.",
    "..dpPPPPPPPPPpd.",
    "...dpPPPwwPPpd..",
    "....dpPPPPPpd...",
    "..dddpppppdddd..",
    ".d.dd......dd.d.",
    "d..d........d..d",
    "................",
    "................",
]


# --------------------------------------------------------------- rebinding ---
# The 16x16 cast replaces the 12x12 one. Bosses, trees, rocks and flowers were
# already drawn at this size and carry over unchanged.

# --------------------------------------------------------------- the night ---
#
# **The Long Night wanted zombies and had slimes in it** (item 0bx).
# Savvydev, 2026-09-08: *"the zombie gamemode should use actual zombies"* -
# and he is right that a mode named for them should have them.
#
# Three, because one is a texture and three is a crowd you can read: the
# shambler is the wall, the crawler is the one that is faster than it
# looks, and the risen is what a warden's soldier turned into. All of
# them are the green of `g`/`G` gone wrong - the palette has no rot in
# it, so the rot is grey-green skin over dark cloth.

# The ordinary one. Arms out, head tipped, a jaw that does not close.
SHAMBLER16 = [
    "................",
    ".....vvvvvv.....",
    "....vggggggv....",
    "....gGGGGGGg....",
    "...gGkGGGGkGg...",
    "...gGGGGGGGGg...",
    "...gGGkkkkGGg...",
    "....gGGGGGGg....",
    "..lllgggggglll..",
    ".llLLllllllLLll.",
    ".lLLLllllllLLLl.",
    "..ll..llll..ll..",
    "......llll......",
    ".....ll..ll.....",
    "....lll..lll....",
    "................",
]

# Low, wide, and quick. What is left of somebody who fell face down.
CRAWLER16 = [
    "................",
    "................",
    "................",
    "................",
    ".....vvvvv......",
    "....vgggggv.....",
    "...gGkGGGkGg....",
    "...gGGGGGGGg....",
    "..llgGkkkkGgll..",
    ".lLLllllllllLLl.",
    "lLLlllllllllllLl",
    ".ll..ll..ll..ll.",
    "......ll..ll....",
    "................",
    "................",
    "................",
]

# Taller, still wearing what it died in. The one that used to be somebody.
RISEN16 = [
    "................",
    "......vvvv......",
    ".....vggggv.....",
    ".....gGGGGg.....",
    "....gGkGGkGg....",
    "....gGGGGGGg....",
    "....gGGkkGGg....",
    ".....ggggg......",
    "...aallllllaa...",
    "..aaLLllllLLaa..",
    "..aaLLllllLLaa..",
    "...aallllllaa...",
    "......llll......",
    ".....ll..ll.....",
    ".....ll..ll.....",
    "....lll..lll....",
]


MONSTERS = {
    "slime": SLIME16, "bat": BAT16, "rat": RAT16, "imp": IMP16,
    "wolf": WOLF16, "wisp": WISP16, "spider": SPIDER16,
    # The night (item 0bx).
    "shambler": SHAMBLER16, "crawler": CRAWLER16, "risen": RISEN16,
}

MONSTER_ALT = {
    "slime": SLIME16_ALT, "bat": BAT16_ALT,
    "wisp": WISP16_ALT, "spider": SPIDER16_ALT,
}

PROPS = {
    "chest": CHEST16, "chest_open": CHEST_OPEN16, "elder": ELDER16,
    "smith": SMITH16, "shrine": SHRINE16, "portal": PORTAL16,
    "tree": TREE, "rock": ROCK, "flower": FLOWER,
}

HERO = {"front": HERO16_FRONT, "back": HERO16_BACK, "side": HERO16_SIDE}

GRID = 16          # every sprite in the game is this size


# One of Maren's couriers. She has been organising this for six hundred years
# and does not walk the roads herself any more.
RUNNER16 = [
    "................",
    ".....oooooo.....",
    "....oooooooo....",
    "...ooSSSSSSoo...",
    "...oSSSSSSSSo...",
    "...SSkSSSSkSS...",
    "...SSSSSSSSSS...",
    "....SSSrrSSS....",
    ".....SSSSSS.....",
    "...gGGGGGGGGn...",
    "..gGGGGGGGGGGn..",
    "..gGGGGGGGGGGn..",
    "...GGGGGGGGnn...",
    "....GGGGGGGG....",
    "....ll....ll....",
    "....tt....tt....",
]

PROPS["runner"] = RUNNER16


# A field boss's ground (s42). Deliberately unlike the portal: a portal is
# a road the story opens for you, and this is a thing somebody left.
LAIR16 = [
    "................",
    "....wwwwwwww....",
    "...wwwwwwwwww...",
    "..wwwwwwwwwwww..",
    "..wwkkwwwwkkww..",
    "..wwkkwwwwkkww..",
    "..wwwwwkkwwwww..",
    "..wwwwwkkwwwww..",
    "...wwwwwwwwww...",
    "....wwwwwwww....",
    "....wkwkwkww....",
    "................",
    "..aaaaaaaaaaaa..",
    ".aAAAAAAAAAAAAa.",
    ".aaaaaaaaaaaaaa.",
    "..aaaaaaaaaaaa..",
]

PROPS["lair"] = LAIR16


# A survey tower (s42). Tall, narrow and lit at the top, so it reads as a
# landmark from across a valley rather than as another rock.
TOWER16 = [
    "................",
    "......CCCC......",
    ".....CCCCCC.....",
    "......CCCC......",
    ".....aAAAAa.....",
    ".....aAaaAa.....",
    "......aAAa......",
    "......aAAa......",
    "......aAAa......",
    ".....aAAAAa.....",
    ".....aAaaAa.....",
    "....aAAAAAAa....",
    "....aAaaaaAa....",
    "...aAAAAAAAAa...",
    "...aaaaaaaaaa...",
    "................",
]

PROPS["tower"] = TOWER16


# A stepstone (s42): a standing arch you have to earn the use of. Read as a
# doorway rather than a monument, because that is what it turns into.
STEPSTONE16 = [
    "................",
    "....aaaaaaaa....",
    "...aAAAAAAAAa...",
    "...aAaaaaaaAa...",
    "...aAaCCCCaAa...",
    "...aAaCppCaAa...",
    "...aAaCppCaAa...",
    "...aAaCppCaAa...",
    "...aAaCppCaAa...",
    "...aAaCppCaAa...",
    "...aAaCppCaAa...",
    "...aAaCppCaAa...",
    "...aAaCppCaAa...",
    "...aAaaaaaaAa...",
    "...aaaaaaaaaa...",
    "................",
]

PROPS["stepstone"] = STEPSTONE16


# --------------------------------------------------------------- village ---

WELL16 = [
    "................",
    "....dddddddd....",
    "...dTTTTTTTTd...",
    "...dTTTTTTTTd...",
    "....t......t....",
    "....t......t....",
    "..aaaaaaaaaaaa..",
    "..aAAAAAAAAAAa..",
    "..aAddddddddAa..",
    "..aAdbbbbbbdAa..",
    "..aAdbBBBBbdAa..",
    "..aAdbbbbbbdAa..",
    "..aAAAAAAAAAAa..",
    "..aaaaaaaaaaaa..",
    "...dddddddddd...",
    "................",
]

FENCE16 = [
    "................",
    "................",
    "................",
    "................",
    "..T..........T..",
    "..T..........T..",
    "TTTTTTTTTTTTTTTT",
    "..T..........T..",
    "..T..........T..",
    "TTTTTTTTTTTTTTTT",
    "..T..........T..",
    "..T..........T..",
    "..t..........t..",
    "..t..........t..",
    "................",
    "................",
]

LANTERN16 = [
    "................",
    "......aaaa......",
    ".....aAAAAa.....",
    "....aAYYYYAa....",
    "....aYYwwYYa....",
    "....aYYwwYYa....",
    "....aAYYYYAa....",
    ".....aAAAAa.....",
    "......a..a......",
    "......t..t......",
    "......t..t......",
    "......t..t......",
    "......t..t......",
    ".....tttttt.....",
    "....tttttttt....",
    "................",
]

VILLAGER16 = [
    "................",
    ".....NNNNNN.....",
    "....NNNNNNNN....",
    "...NNSSSSSSNN...",
    "...NSSSSSSSSN...",
    "...SSkSSSSkSS...",
    "...SSSSSSSSSS...",
    "....SSSrrSSS....",
    ".....SSSSSS.....",
    "...ooooooooo....",
    "..oooooooooooo..",
    "..oooooooooooo..",
    "...oooooooooo...",
    "....oooooooo....",
    "....ll....ll....",
    "....tt....tt....",
]

PROPS["well"] = WELL16
PROPS["fence"] = FENCE16
PROPS["lantern"] = LANTERN16
PROPS["villager"] = VILLAGER16
# A waystation keeper wears what a villager wears. **Reused rather than
# drawn** (s42): they are the people who already live on that road, not a
# new kind of person, and inventing a sprite to say otherwise would be
# adding art to make a distinction the story does not make.
PROPS["keeper"] = PROPS["villager"]


WINDOW16 = [
    "................",
    "................",
    "...tttttttttt...",
    "...tTTTTTTTTt...",
    "...tTYYYYYYTt...",
    "...tTYYwwYYTt...",
    "...tTYYwwYYTt...",
    "...tTTTTTTTTt...",
    "...tTYYwwYYTt...",
    "...tTYYwwYYTt...",
    "...tTYYYYYYTt...",
    "...tTTTTTTTTt...",
    "...tttttttttt...",
    "....TTTTTTTT....",
    "................",
    "................",
]

SIGN16 = [
    "................",
    "...nnnnnnnnnn...",
    "..nNNNNNNNNNNn..",
    "..nNYYYYYYYYNn..",
    "..nNYnnnnnnYNn..",
    "..nNYnYYYYnYNn..",
    "..nNYnYnnYnYNn..",
    "..nNYnYYYYnYNn..",
    "..nNYnnnnnnYNn..",
    "..nNYYYYYYYYNn..",
    "..nNNNNNNNNNNn..",
    "...nnnnnnnnnn...",
    "......tttt......",
    "......tttt......",
    ".....tttttt.....",
    "................",
]

# **A marker stone** (2026-09-05, s55, roadmap item 12b). What stands at a
# ruin or a stone circle and can be read. Deliberately *not* the shop's
# `SIGN16`: a painted board is somebody's business advertising itself, and
# these are older than whoever put them up. Grey and uneven, leaning very
# slightly, on a patch of turf.
MARKER16 = [
    "................",
    "......kkkk......",
    ".....kqqAqk.....",
    ".....kqAAqk.....",
    "....kqqAAqqk....",
    "....kqAAqAqk....",
    "....kqqAqqqk....",
    "....kqAAqAqk....",
    "....kqqqAqqk....",
    "....kqAqqAqk....",
    "....kqqAqqqk....",
    ".....kqqqqk.....",
    ".....kqqqqk.....",
    "....gkkqqkkg....",
    "...gvgkkkkgvg...",
    "................",
]

PROPS["window"] = WINDOW16
CAMP16 = [
    "................",
    "................",
    "................",
    "......k...k.....",
    ".....kOk.kok....",
    "....kOYOkOOk....",
    "....kOYYOYOk....",
    "...kOYYwYYOk....",
    "...kOYwwwYOk....",
    "...koOYwYOok....",
    "..kqqkoOOokqqk..",
    "..kqAqqkkqqAqk..",
    "...kqAqqqqAqk...",
    "....kqqqqqqk....",
    ".....kkkkkk.....",
    "................",
]

# **A fire somebody else built, and left.** The one new prop this needed
# and it is one grid, hand-typed like every other (hard rule 1). A cairn
# was tried first and reads as `MARKER16` at a glance - which is exactly
# wrong, because a marker is a thing to *read* and this is a thing to
# *stop at*. Flame says stop.
PROPS["camp"] = CAMP16
PROPS["sign"] = SIGN16
PROPS["marker"] = MARKER16

# ------------------------------------------------------------ interiors ---
# **Roadmap item 18** (2026-09-05, s56). Savvydev wanted house interiors on
# 2026-08-24 - *"yes house interior will be good."* - and §13 had recorded
# their absence as a scope decision. Three pieces of furniture is enough to
# make a room read as somebody's: something to sleep on, something to eat
# at, and a fire.
BED16 = [
    "................",
    "..nnnnnnnnnnnn..",
    "..nwwwwNNNNNNn..",
    "..nwwwwNNNNNNn..",
    "..nwwwwNNNNNNn..",
    "..nAAAANNNNNNn..",
    "..nAAAArrrrrrn..",
    "..nAAAArRRRRrn..",
    "..nAAAArRRRRrn..",
    "..nAAAArRRRRrn..",
    "..nAAAArRRRRrn..",
    "..nAAAArrrrrrn..",
    "..nnnnnnnnnnnn..",
    "...n..........n.",
    "...n..........n.",
    "................",
]

TABLE16 = [
    "................",
    "................",
    "..NNNNNNNNNNNN..",
    "..NTTTTTTTTTTN..",
    "..NTTTwwwTTTTN..",
    "..NTTTwwwTTTTN..",
    "..NTTTTTTTTTTN..",
    "..NNNNNNNNNNNN..",
    "..nn........nn..",
    "..nn........nn..",
    "..nn........nn..",
    "..nn........nn..",
    "..nn........nn..",
    "................",
    "................",
    "................",
]

HEARTH16 = [
    "................",
    "..aaaaaaaaaaaa..",
    "..aqqqqqqqqqqa..",
    "..aqAAAAAAAAqa..",
    "..aqA......Aqa..",
    "..aqA..oo..Aqa..",
    "..aqA.oOOo.Aqa..",
    "..aqA.OOYO.Aqa..",
    "..aqA.oOYOoAqa..",
    "..aqA.tOOOtAqa..",
    "..aqAttttttAqa..",
    "..aqqqqqqqqqqa..",
    "..aaaaaaaaaaaa..",
    "...aaaaaaaaaa...",
    "................",
    "................",
]

# **Four more, so no two rooms furnish the same** (0af, s58). A room used
# to draw from three pieces plus flowers and lanterns, which is why every
# house read as the same house with the ornaments moved.
SHELF16 = [
    "................",
    "..NNNNNNNNNNNN..",
    "..NrRbBgGyYPpN..",
    "..NnnnnnnnnnnN..",
    "..NNNNNNNNNNNN..",
    "..NbBrRyYgGcCN..",
    "..NnnnnnnnnnnN..",
    "..NNNNNNNNNNNN..",
    "..NPpcCrRbBgGN..",
    "..NnnnnnnnnnnN..",
    "..NNNNNNNNNNNN..",
    "..nn........nn..",
    "..nn........nn..",
    "................",
    "................",
    "................",
]

BARREL16 = [
    "................",
    "................",
    "....NNNNNNNN....",
    "...NnnnnnnnnN...",
    "...NaaaaaaaaN...",
    "...NnnnnnnnnN...",
    "...NnnnnnnnnN...",
    "...NaaaaaaaaN...",
    "...NnnnnnnnnN...",
    "...NnnnnnnnnN...",
    "...NaaaaaaaaN...",
    "...NnnnnnnnnN...",
    "....NNNNNNNN....",
    "................",
    "................",
    "................",
]

# **Flat on purpose.** A rug is the one furnishing you walk over, so it is
# drawn dark and low-contrast - the §7 texture-density trap says a bright
# thing underfoot on every floor tile reads as noise.
RUG16 = [
    "................",
    "................",
    "..llllllllllll..",
    "..lLLLLLLLLLLl..",
    "..lLnnnnnnnnLl..",
    "..lLnNNNNNNnLl..",
    "..lLnNLLLLNnLl..",
    "..lLnNLLLLNnLl..",
    "..lLnNNNNNNnLl..",
    "..lLnnnnnnnnLl..",
    "..lLLLLLLLLLLl..",
    "..llllllllllll..",
    "................",
    "................",
    "................",
    "................",
]

# The stacked house's pile (0af). Loud, because finding one is the event.
HOARD16 = [
    "................",
    ".......YY.......",
    "......YyyY......",
    ".....YyYYyY.....",
    "....PpYYYYpP....",
    "...CcYyYYyYcC...",
    "..YyYYyYYyYYyY..",
    "..yYYyYYyYYyYy..",
    ".YyYYyYPpYYyYYy.",
    ".yYYyYYyYYyYYyY.",
    "YyYYyYYyYYyYYyYy",
    "yYYyYcCyYYyYPpYY",
    "YyYYyYYyYYyYYyYy",
    ".yYYyYYyYYyYYyY.",
    "..yYYyYYyYYyYy..",
    "...yyyyyyyyyy...",
]

PROPS["bed"] = BED16
PROPS["table"] = TABLE16
PROPS["hearth"] = HEARTH16
PROPS["shelf"] = SHELF16
PROPS["barrel"] = BARREL16
PROPS["rug"] = RUG16
PROPS["hoard"] = HOARD16
