"""Character appearance: skin, hair colour, hair style, outfit.

The hero is already drawn from a character grid through a palette lookup, so
customisation is a palette override plus a few row substitutions for the hair
silhouette. No new sprite sheets, and the whole appearance persists as four
small integers.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import security as sec

# (label, shadow, light) -> palette chars "s" and "S"
SKIN_TONES = [
    ("Porcelain", (222, 176, 148), (252, 224, 202)),
    ("Fair", (196, 140, 104), (238, 194, 152)),
    ("Warm", (176, 122, 84), (226, 176, 132)),
    ("Olive", (150, 108, 72), (204, 160, 114)),
    ("Bronze", (126, 84, 52), (182, 130, 88)),
    ("Umber", (96, 62, 40), (146, 100, 66)),
    ("Deep", (66, 42, 30), (110, 74, 50)),
    ("Ash", (128, 122, 132), (182, 176, 190)),
]

# (label, dark, light) -> palette chars "n" and "N"
HAIR_COLORS = [
    ("Black", (34, 30, 38), (66, 60, 74)),
    ("Chestnut", (86, 58, 36), (140, 96, 58)),
    ("Auburn", (112, 52, 34), (176, 88, 54)),
    ("Ginger", (150, 74, 30), (222, 122, 52)),
    ("Wheat", (166, 132, 62), (232, 196, 110)),
    ("Platinum", (176, 176, 186), (238, 238, 246)),
    ("Ivy", (40, 90, 58), (78, 148, 92)),
    ("Cobalt", (44, 66, 132), (86, 122, 206)),
    ("Violet", (86, 46, 130), (150, 96, 208)),
    ("Rose", (146, 62, 104), (216, 110, 164)),
]

# (label, dark, light) -> palette chars "b" and "B"
OUTFIT_COLORS = [
    ("Wayfarer Blue", (38, 74, 148), (86, 152, 236)),
    ("Forest", (34, 92, 56), (72, 152, 90)),
    ("Crimson", (128, 34, 44), (206, 66, 72)),
    ("Amber", (158, 106, 28), (238, 176, 62)),
    ("Plum", (78, 42, 112), (146, 92, 196)),
    ("Slate", (66, 70, 84), (124, 130, 148)),
    ("Bone", (166, 158, 146), (232, 226, 214)),
    ("Teal", (30, 104, 110), (72, 172, 178)),
]

HAIR_STYLES = ["Cropped", "Swept", "Long", "Tousled", "Shaved"]

# How the body is shaped. Named plainly so the choice is obvious.
BUILDS = ["Feminine", "Androgynous", "Masculine"]
BUILD_KEYS = ["feminine", "androgynous", "masculine"]

# Row substitutions on the 16x16 hero grids. Row 0 is empty, 1-3 are the
# crown, 4-9 the face, 10-13 the torso, 14-15 the legs.
_STYLE_ROWS = {
    0: {   # Cropped
        "front": {1: "......nnnn......", 2: ".....nnnnnn.....",
                  3: "....nnnnnnnn...."},
        "back": {1: "......nnnn......", 2: ".....nnnnnn.....",
                 3: "....nnnnnnnn...."},
        "side": {1: ".....nnnn.......", 2: "....nnnnnn......",
                 3: "...nnnnnnnn....."},
    },
    1: {},     # Swept - the grids as drawn
    2: {       # Long - falls past the jaw
        "front": {6: "..nSSkSSSSkSSn..", 7: "..nSSSSSSSSSSn..",
                  8: "..nnSSSrrSSSnn..", 9: "...n.SSSSSS.n..."},
        "back": {7: "..nSnnnnnnnnSn..", 8: "..nnSnnnnnnSnn..",
                 9: "...n.SSSSSS.n..."},
        "side": {6: ".nSSkSSSSSS.....", 7: ".nSSSSSSSSS.....",
                 8: ".nnSSSSrrS......", 9: "..n.SSSSSS......"},
    },
    3: {       # Tousled - a ragged crown
        "front": {1: "....n.nnnn.n....", 2: "...nnnnnnnnnn...",
                  3: "..nnnnnnnnnnnn.."},
        "back": {1: "....n.nnnn.n....", 2: "...nnnnnnnnnn...",
                 3: "..nnnnnnnnnnnn.."},
        "side": {1: "...n.nnnn.n.....", 2: "..nnnnnnnnnn....",
                 3: ".nnnnnnnnnnnn..."},
    },
    4: {       # Shaved - scalp, no crown
        "front": {1: "................", 2: ".....SSSSSS.....",
                  3: "....SSSSSSSS....", 4: "...SSSSSSSSSS...",
                  5: "...SSSSSSSSSS..."},
        "back": {1: "................", 2: ".....SSSSSS.....",
                 3: "....SSSSSSSS....", 4: "...SSSSSSSSSS...",
                 5: "...SSSSSSSSSS...", 6: "...SSSSSSSSSS..."},
        "side": {1: "................", 2: "....SSSSSS......",
                 3: "...SSSSSSSS.....", 4: "..SSSSSSSSS.....",
                 5: "..SSSSSSSSS....."},
    },
}


def _apply(rows, table):
    rows = list(rows)
    for index, text in table.items():
        if 0 <= index < len(rows):
            rows[index] = text
    return rows


def _rows_for(rows, style, build, facing, walk_frame=0):
    """Reshape a hero grid for build, hair style and walk phase."""
    from . import spritedata as sd
    rows = _apply(rows, sd.BUILD_ROWS.get(BUILD_KEYS[build], {}).get(facing, {}))
    rows = _apply(rows, _STYLE_ROWS.get(style, {}).get(facing, {}))
    frames = sd.WALK_LEGS.get(facing)
    if frames and walk_frame:
        legs, boots = frames[walk_frame % len(frames)]
        rows = _apply(rows, {len(rows) - 2: legs, len(rows) - 1: boots})
    return rows


@dataclass
class Appearance:
    """Four indices. Everything else is derived."""
    skin: int = 1
    hair: int = 1
    style: int = 1
    outfit: int = 0
    build: int = 1

    FIELDS = ("build", "skin", "hair", "style", "outfit")
    TABLES = {"skin": SKIN_TONES, "hair": HAIR_COLORS,
              "style": HAIR_STYLES, "outfit": OUTFIT_COLORS,
              "build": BUILDS}

    def count(self, field):
        return len(self.TABLES[field])

    def label(self, field):
        table = self.TABLES[field]
        value = table[getattr(self, field) % len(table)]
        return value if field in ("style", "build") else value[0]

    def color(self, field):
        """Representative swatch colour for a field, for the UI."""
        if field in ("style", "build"):
            return HAIR_COLORS[self.hair % len(HAIR_COLORS)][2]
        return self.TABLES[field][getattr(self, field) % self.count(field)][2]

    def cycle(self, field, step=1):
        if field in self.FIELDS:
            setattr(self, field,
                    (getattr(self, field) + step) % self.count(field))
        return self

    def randomize(self, rng):
        for field in self.FIELDS:
            setattr(self, field, rng.rint(0, self.count(field) - 1))
        return self

    def signature(self):
        """Hashable identity, used as part of the sprite cache key."""
        return (self.skin, self.hair, self.style, self.outfit, self.build)

    def palette(self, base):
        """Palette override applied when the hero grid is rasterised."""
        skin = SKIN_TONES[self.skin % len(SKIN_TONES)]
        hair = HAIR_COLORS[self.hair % len(HAIR_COLORS)]
        outfit = OUTFIT_COLORS[self.outfit % len(OUTFIT_COLORS)]
        merged = dict(base)
        merged["s"], merged["S"] = skin[1], skin[2]
        merged["n"], merged["N"] = hair[1], hair[2]
        merged["b"], merged["B"] = outfit[1], outfit[2]
        return merged

    def rows(self, base_rows, facing, walk_frame=0):
        return _rows_for(base_rows, self.style % len(HAIR_STYLES),
                         self.build % len(BUILDS), facing, walk_frame)

    # -- persistence -------------------------------------------------------

    def to_dict(self):
        return {field: int(getattr(self, field)) for field in self.FIELDS}

    @classmethod
    def from_dict(cls, data):
        raw = sec.pick_known_keys(data, set(cls.FIELDS))
        out = cls()
        for field in cls.FIELDS:
            out_max = len(cls.TABLES[field]) - 1
            setattr(out, field,
                    sec.safe_int(raw.get(field), 0, out_max,
                                 getattr(out, field)))
        return out
