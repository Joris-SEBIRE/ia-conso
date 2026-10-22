"""Fabrique les deux sons d'ia-conso, dans src/ia_conso/ :

- marimba.wav, deux notes de marimba : une session a fini son tour ;
- gouttes.wav, deux gouttes d'eau : une session attend ton intervention.

Chaque son est une suite de coups mixés, passés dans un peu d'espace (trois échos amortis), puis
normalisés. Rejouer ce script après avoir touché à une note ou à un timbre.
"""

import math
import struct
import wave
from pathlib import Path

RATE = 44100
# Partiels du marimba : rapport à la fondamentale, amplitude, constante d'extinction (s).
MARIMBA = ((1.0, 1.0, 0.32), (3.93, 0.3, 0.07), (9.2, 0.07, 0.025))
SOL5, DO6 = 440 * 2 ** (10 / 12), 440 * 2 ** (15 / 12)
PEAK = 0.42  # crête des fichiers : « léger », bien en dessous du plein volume


def put(buf, i, value):
    if i >= len(buf):
        buf.extend([0.0] * (i - len(buf) + RATE // 4))
    buf[i] += value


def strike(buf, start, freq, gain):
    """Un coup de marimba : chaque partiel s'éteint à son rythme, après 2 ms d'attaque."""
    for k in range(int(5 * 0.32 * RATE)):
        t = k / RATE
        value = sum(a * math.exp(-t / tau) * math.sin(2 * math.pi * freq * r * t) for r, a, tau in MARIMBA)
        put(buf, start + k, gain * min(1.0, t / 0.002) * value)


def drop(buf, start, f0, f1, gain):
    """Une goutte : un sinus qui glisse vers le grave en s'éteignant."""
    phase = 0.0
    for k in range(int(5 * 0.09 * RATE)):
        t = k / RATE
        phase += 2 * math.pi * (f1 + (f0 - f1) * math.exp(-t / 0.02)) / RATE
        put(buf, start + k, gain * min(1.0, t / 0.001) * math.exp(-t / 0.09) * math.sin(phase))


def write(name, buf, space):
    buf = buf + [0.0] * int(0.4 * RATE)
    for delay, fade in ((0.031, 0.5), (0.047, 0.4), (0.071, 0.3)):
        d = int(delay * RATE)
        for i in range(d, len(buf)):
            buf[i] += space * fade * buf[i - d]
    top = max(abs(s) for s in buf)
    while abs(buf[-1]) < 2e-3 * top:
        buf.pop()
    fade = int(0.02 * RATE)
    for k in range(fade):
        buf[-1 - k] *= k / fade
    with wave.open(str(Path(__file__).parent.parent / "src/ia_conso" / name), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(b"".join(struct.pack("<h", int(32767 * PEAK * s / top)) for s in buf))


marimba: list[float] = []
strike(marimba, 0, SOL5, 1.0)
strike(marimba, int(0.13 * RATE), DO6, 0.9)
write("marimba.wav", marimba, 0.12)

gouttes: list[float] = []
drop(gouttes, 0, 1300, 700, 1.0)
drop(gouttes, int(0.15 * RATE), 1700, 950, 0.8)
write("gouttes.wav", gouttes, 0.2)
