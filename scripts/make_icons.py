#!/usr/bin/env python3
"""Dibuja los iconos de la app (PNG) sin librerías externas.

Un cuadrado azul con el símbolo del euro en blanco. Se ejecuta a mano:
    python3 scripts/make_icons.py
Los PNG resultantes se guardan en app/static/icons/ y van dentro de la imagen.
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ICONS = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
BLUE = (42, 120, 214)          # --accent del CSS
WHITE = (255, 255, 255)
SS = 3                         # muestras por lado y píxel (suavizado)


def write_png(path: Path, pixels: list[list[tuple[int, int, int, int]]]) -> None:
    height, width = len(pixels), len(pixels[0])
    raw = bytearray()
    for row in pixels:
        raw.append(0)                                   # filtro "None"
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def rounded_square(x: float, y: float, size: float, radius: float) -> bool:
    """¿Está el punto dentro de un cuadrado de esquinas redondeadas?"""
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def euro(x: float, y: float, cx: float, cy: float, r: float) -> bool:
    """Símbolo del euro: un arco abierto por la derecha y dos barras."""
    thickness = r * 0.30
    dx, dy = x - cx, y - cy
    dist = math.hypot(dx, dy)
    if abs(dist - r * 0.92) <= thickness / 2:
        angle = math.degrees(math.atan2(dy, dx))        # 0° = derecha
        if abs(angle) > 38:
            return True
    for offset in (-0.30, 0.16):
        if (abs(dy - r * offset) <= thickness * 0.42
                and -r * 1.32 <= dx <= r * 0.46):
            return True
    return False


def draw(size: int, maskable: bool = False, rounded: bool = True) -> list[list[tuple]]:
    pad = size * 0.22 if maskable else size * 0.17     # zona segura de los iconos recortables
    radius = size * 0.22
    cx = cy = size / 2
    r = (size - 2 * pad) / 2
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            bg_hits = glyph_hits = 0
            for sy in range(SS):
                for sx in range(SS):
                    x = px + (sx + 0.5) / SS
                    y = py + (sy + 0.5) / SS
                    if not rounded or rounded_square(x, y, size, radius):
                        bg_hits += 1
                        if euro(x, y, cx, cy, r):
                            glyph_hits += 1
            total = SS * SS
            if not bg_hits:
                row.append((0, 0, 0, 0))
                continue
            glyph = glyph_hits / total
            alpha = round(255 * bg_hits / total)
            color = tuple(round(b + (w - b) * glyph) for b, w in zip(BLUE, WHITE))
            row.append((*color, alpha))
        rows.append(row)
    return rows


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("icon-192.png", 192, False, True),
        ("icon-512.png", 512, False, True),
        ("icon-maskable-512.png", 512, True, False),
        ("apple-touch-icon.png", 180, False, False),    # iOS ya le pone él las esquinas
    ]
    for name, size, maskable, rounded in jobs:
        write_png(ICONS / name, draw(size, maskable, rounded))
        print("escrito", name, size)


if __name__ == "__main__":
    main()
