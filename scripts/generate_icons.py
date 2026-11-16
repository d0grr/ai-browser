"""Reproducible antialiased PNG logo and toolbar states; Python standard library only."""
from pathlib import Path
import math
import struct
import zlib

ROOT = Path(__file__).resolve().parents[1] / 'extension' / 'icons'
ROOT.mkdir(parents=True, exist_ok=True)
COLORS = dict(idle=(139, 160, 190), ready=(73, 227, 174), error=(248, 98, 121), paused=(246, 194, 94))

def rounded(x, y, left, top, right, bottom, radius):
    cx = max(left + radius, min(x, right - radius))
    cy = max(top + radius, min(y, bottom - radius))
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2

def pixel(x, y, state):
    color = (0, 0, 0, 0)
    if rounded(x, y, 3, 3, 125, 125, 29): color = (18, 29, 49, 255)
    # Stacked browser windows with a bright foreground tab and cursor.
    if rounded(x, y, 25, 24, 101, 84, 10): color = (56, 99, 112, 255)
    if rounded(x, y, 18, 34, 94, 94, 10): color = (108, 236, 201, 255)
    if rounded(x, y, 23, 49, 89, 89, 5): color = (24, 51, 64, 255)
    if 27 < x < 32 and 39 < y < 44: color = (18, 51, 57, 255)
    if 36 < x < 41 and 39 < y < 44: color = (18, 51, 57, 255)
    # White pointer arrow.
    if 51 <= y <= 103 and 54 <= x <= 86 and x - 54 <= (y - 51) * .62 and (y <= 82 or x >= 65):
        color = (239, 247, 255, 255)
    if state != 'logo':
        if (x - 103) ** 2 + (y - 102) ** 2 < 22 ** 2: color = (18, 29, 49, 255)
        if state.startswith('spin'):
            dist = math.hypot(x - 103, y - 102)
            angle = (math.atan2(y - 102, x - 103) - int(state[-1]) * math.pi / 2) % (2 * math.pi)
            if 11 < dist < 17: color = (108, 236, 201, 255) if angle < 4.7 else (39, 62, 82, 255)
        elif (x - 103) ** 2 + (y - 102) ** 2 < 16 ** 2:
            color = (*COLORS[state], 255)
    return color

def png(size, state):
    scale = 3
    data = bytearray()
    for row in range(size):
        data.append(0)
        for col in range(size):
            samples = [pixel((col + (sx + .5) / scale) * 128 / size,
                             (row + (sy + .5) / scale) * 128 / size, state)
                       for sy in range(scale) for sx in range(scale)]
            # Average premultiplied samples to avoid dark fringes on transparency.
            alpha = sum(p[3] for p in samples)
            for channel in range(3): data.append(round(sum(p[channel] * p[3] for p in samples) / alpha) if alpha else 0)
            data.append(round(alpha / len(samples)))
    def chunk(kind, value):
        return struct.pack('!I', len(value)) + kind + value + struct.pack('!I', zlib.crc32(kind + value))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', size, size, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(data, 9)) + chunk(b'IEND', b'')

for state in ['logo', 'idle', 'ready', 'error', 'paused', 'spin0', 'spin1', 'spin2', 'spin3']:
    for size in ([16, 32, 48, 128] if state == 'logo' else [16, 32]):
        (ROOT / f'{state}-{size}.png').write_bytes(png(size, state))
