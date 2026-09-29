"""Procedurally synthesized sound effects.

No .wav files and no numpy: every sound is a few hundred milliseconds of
16-bit mono PCM built with the standard library `array` module and handed
straight to pygame.mixer.

If there is no audio device - CI, a headless box, a machine with sound
disabled - every call degrades to a no-op instead of raising.
"""
from __future__ import annotations

import math
import random
from array import array

import pygame

SAMPLE_RATE = 22050
AMPLITUDE = 12000          # headroom below the 32767 ceiling for mixing


def _wave_sample(shape, phase, rng):
    """One sample of a normalized (-1..1) waveform at the given phase."""
    if shape == "sine":
        return math.sin(phase)
    if shape == "square":
        return 1.0 if math.sin(phase) >= 0 else -1.0
    if shape == "saw":
        return 2.0 * ((phase / math.tau) % 1.0) - 1.0
    if shape == "triangle":
        frac = (phase / math.tau) % 1.0
        return 4.0 * abs(frac - 0.5) - 1.0
    if shape == "noise":
        return rng.uniform(-1.0, 1.0)
    return math.sin(phase)


def synth(freq=440.0, duration=0.18, shape="square", volume=1.0,
          freq_end=None, attack=0.01, release=0.6, vibrato=0.0, seed=1):
    """Build one tone as an `array('h')` of signed 16-bit mono samples.

    freq_end enables a linear pitch sweep, which is what makes a coin pickup
    sound like a coin pickup rather than a beep.
    """
    rng = random.Random(seed)
    count = max(1, int(SAMPLE_RATE * max(0.01, duration)))
    samples = array("h", bytes(count * 2))
    freq_end = freq if freq_end is None else freq_end
    attack_n = max(1, int(count * attack))
    release_n = max(1, int(count * release))
    phase = 0.0

    for i in range(count):
        t = i / count
        current = freq + (freq_end - freq) * t
        if vibrato:
            current *= 1.0 + vibrato * math.sin(t * math.tau * 14.0)
        phase += math.tau * current / SAMPLE_RATE

        # Envelope: linear attack, exponential-ish release.
        if i < attack_n:
            env = i / attack_n
        elif i > count - release_n:
            env = max(0.0, (count - i) / release_n) ** 1.6
        else:
            env = 1.0

        value = _wave_sample(shape, phase, rng) * env * volume
        samples[i] = int(max(-32767, min(32767, value * AMPLITUDE)))
    return samples


def mix(*layers):
    """Sum several tone arrays, longest wins, with clipping protection."""
    length = max((len(layer) for layer in layers), default=0)
    out = array("h", bytes(length * 2))
    for layer in layers:
        for i, value in enumerate(layer):
            total = out[i] + value
            out[i] = 32767 if total > 32767 else -32767 if total < -32767 else total
    return out


def chord(freqs, duration, shape="square", volume=0.5, **kwargs):
    return mix(*[synth(f, duration, shape, volume, **kwargs) for f in freqs])


def arpeggio(freqs, note=0.07, shape="square", volume=0.7):
    """Sequential notes, concatenated - used for level-up and victory."""
    out = array("h")
    for i, freq in enumerate(freqs):
        out.extend(synth(freq, note, shape, volume, attack=0.02,
                         release=0.5, seed=i + 1))
    return out


# Each entry is a zero-argument builder, evaluated once on first play.
RECIPES = {
    "move":      lambda: synth(520, 0.05, "square", 0.30, 620, release=0.7),
    "select":    lambda: arpeggio([660, 880], 0.05, "square", 0.45),
    "back":      lambda: synth(420, 0.09, "square", 0.35, 260),
    "error":     lambda: synth(180, 0.16, "saw", 0.40, 120),
    "step":      lambda: synth(90, 0.04, "noise", 0.16, release=0.9),
    "swing":     lambda: synth(700, 0.10, "noise", 0.30, 260, release=0.8),
    "hit":       lambda: mix(synth(190, 0.13, "square", 0.45, 90),
                             synth(300, 0.09, "noise", 0.28)),
    "crit":      lambda: mix(synth(260, 0.20, "saw", 0.50, 110),
                             synth(880, 0.12, "square", 0.30, 440)),
    "block":     lambda: mix(synth(320, 0.10, "square", 0.30, 300),
                             synth(140, 0.12, "noise", 0.22)),
    "hurt":      lambda: synth(240, 0.18, "saw", 0.42, 110, vibrato=0.05),
    "heal":      lambda: arpeggio([523, 659, 784, 1047], 0.06, "sine", 0.55),
    "coin":      lambda: arpeggio([988, 1319], 0.05, "square", 0.40),
    "item":      lambda: arpeggio([659, 880, 1109], 0.05, "triangle", 0.45),
    "chest":     lambda: mix(synth(300, 0.22, "noise", 0.22),
                             arpeggio([784, 988, 1319], 0.06, "square", 0.45)),
    "levelup":   lambda: arpeggio([523, 659, 784, 1047, 1319], 0.08, "square", 0.55),
    "victory":   lambda: arpeggio([784, 784, 784, 1047, 988, 1047], 0.10,
                                  "square", 0.50),
    "defeat":    lambda: arpeggio([392, 349, 311, 262], 0.16, "saw", 0.45),
    "boss":      lambda: mix(synth(70, 0.75, "saw", 0.55, 45, vibrato=0.06),
                             synth(140, 0.60, "square", 0.28, 90)),
    "fire":      lambda: mix(synth(420, 0.30, "noise", 0.34, 120),
                             synth(180, 0.26, "saw", 0.26, 90)),
    "ice":       lambda: mix(synth(1200, 0.24, "sine", 0.30, 1800),
                             synth(900, 0.18, "triangle", 0.22, 1400)),
    "thunder":   lambda: mix(synth(1600, 0.16, "square", 0.34, 200),
                             synth(120, 0.28, "noise", 0.34)),
    "shadow":    lambda: synth(160, 0.34, "saw", 0.34, 60, vibrato=0.09),
    "holy":      lambda: chord([784, 988, 1175], 0.34, "sine", 0.26),
    "portal":    lambda: synth(200, 0.45, "sine", 0.36, 900, vibrato=0.05),
    "quest":     lambda: arpeggio([659, 784, 1047], 0.07, "triangle", 0.5),
    "unlock":    lambda: arpeggio([440, 554, 659, 880], 0.07, "square", 0.5),
}


class Audio:
    """Sound bank with a master volume and graceful degradation."""

    def __init__(self, enabled=True, volume=0.55):
        self.enabled = bool(enabled)
        self.volume = float(volume)
        self.available = False
        self._cache = {}
        self._last_played = {}
        if self.enabled:
            self._init_mixer()

    def _init_mixer(self):
        try:
            pygame.mixer.pre_init(SAMPLE_RATE, -16, 1, 512)
            pygame.mixer.init(SAMPLE_RATE, -16, 1, 512)
            pygame.mixer.set_num_channels(16)
            self.available = True
        except pygame.error:
            # No audio device. The game is fully playable in silence.
            self.available = False

    def _build(self, name):
        recipe = RECIPES.get(name)
        if recipe is None:
            return None
        try:
            return pygame.mixer.Sound(buffer=recipe().tobytes())
        except (pygame.error, ValueError):
            return None

    def play(self, name, volume=1.0, throttle=0.0):
        """Play a named effect.

        `throttle` suppresses repeats of the same sound inside a time window,
        which keeps footsteps and rapid hits from turning into a buzzsaw.
        """
        if not (self.enabled and self.available):
            return
        if throttle:
            now = pygame.time.get_ticks() / 1000.0
            if now - self._last_played.get(name, -99) < throttle:
                return
            self._last_played[name] = now
        sound = self._cache.get(name)
        if sound is None:
            sound = self._build(name)
            if sound is None:
                return
            self._cache[name] = sound
        sound.set_volume(max(0.0, min(1.0, self.volume * volume)))
        try:
            sound.play()
        except pygame.error:
            pass

    def set_volume(self, value):
        self.volume = max(0.0, min(1.0, float(value)))

    def toggle(self):
        self.enabled = not self.enabled
        if self.enabled and not self.available:
            self._init_mixer()
        elif not self.enabled:
            try:
                pygame.mixer.stop()
            except pygame.error:
                pass
        return self.enabled

    def prewarm(self, names=("move", "select", "hit", "swing", "hurt", "coin")):
        """Build the hottest sounds up front so the first hit is not late."""
        for name in names:
            if self.available and name not in self._cache:
                built = self._build(name)
                if built:
                    self._cache[name] = built
