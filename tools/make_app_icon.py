"""Render the HealthSync app icon (1024x1024 PNG).

    uv run --no-project --with pillow python tools/make_app_icon.py
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024
OUT = Path(__file__).resolve().parents[1] / "ios/HealthSync/Resources/Assets.xcassets/AppIcon.appiconset/AppIcon.png"


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def main() -> None:
    top, bottom = (0x1B, 0x64, 0xBE), (0x00, 0xA8, 0x88)
    img = Image.new("RGB", (S, S))
    px = img.load()
    for y in range(S):
        for x in range(S):
            px[x, y] = lerp(top, bottom, (0.65 * y + 0.35 * x) / S)

    # Heart from the classic parametric curve.
    pts = []
    for i in range(720):
        t = 2 * math.pi * i / 720
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((S / 2 + x * 25, S / 2 - 20 - y * 25))
    shadow = Image.new("L", (S, S), 0)
    ImageDraw.Draw(shadow).polygon([(x, y + 18) for x, y in pts], fill=90)
    img.paste((0, 40, 80), mask=shadow.filter(ImageFilter.GaussianBlur(24)))
    d = ImageDraw.Draw(img)
    d.polygon(pts, fill=(255, 255, 255))

    # ECG line across the heart.
    c = S / 2
    line = [(150, c), (360, c), (410, c - 70), (470, c + 150), (540, c - 230), (600, c + 60), (640, c), (874, c)]
    d.line(line, fill=(0x1B, 0x64, 0xBE), width=44, joint="curve")
    for p in (line[0], line[-1]):
        d.ellipse((p[0] - 22, p[1] - 22, p[0] + 22, p[1] + 22), fill=(0x1B, 0x64, 0xBE))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
