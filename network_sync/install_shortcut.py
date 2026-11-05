"""Build the app icon and add a "4th Year Sync" shortcut to the Start menu.

    python -m network_sync.install_shortcut

Then pin it: Start -> type "4th Year Sync" -> right-click -> Pin to taskbar.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

APP_NAME = "4th Year Sync"
HERE = Path(__file__).resolve().parent
ICON = HERE / "app.ico"
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# The logo on a 64-unit grid (same as the SVG favicon in static/index.html).
TILE, ARROW, PAGE = "#C2410C", "#FFF7ED", "#FED7AA"
STROKE = 5


def draw_logo(size: int) -> Image.Image:
    big = size * 8  # draw large, then shrink, for smooth edges
    s = big / 64
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def pt(x: float, y: float) -> tuple[float, float]:
        return x * s, y * s

    def dot(x: float, y: float) -> None:  # round cap / join
        r = STROKE / 2 * s
        d.ellipse([x * s - r, y * s - r, x * s + r, y * s + r], fill=ARROW)

    def arc(cx: float, cy: float, start: float, end: float) -> None:
        r = 17 + STROKE / 2
        d.arc([*pt(cx - r, cy - r), *pt(cx + r, cy + r)], start, end, fill=ARROW, width=round(STROKE * s))

    def elbow(points: list[tuple[float, float]]) -> None:
        d.line([pt(*p) for p in points], fill=ARROW, width=round(STROKE * s))
        for p in points:
            dot(*p)

    d.rounded_rectangle([*pt(2, 2), *pt(62, 62)], radius=15 * s, fill=TILE)
    arc(32, 30, 183.4, 317.4)
    dot(15, 29)
    elbow([(46, 10.5), (46, 19.5), (37, 19.5)])
    arc(32, 34, 3.4, 137.4)
    dot(49, 35)
    elbow([(18, 53.5), (18, 44.5), (27, 44.5)])
    d.rounded_rectangle([*pt(26.5, 24.5), *pt(37.5, 39.5)], radius=2 * s, fill=PAGE)

    return img.resize((size, size), Image.LANCZOS)


def build_icon() -> None:
    images = [draw_logo(size) for size in ICON_SIZES]
    images[-1].save(ICON, sizes=[(n, n) for n in ICON_SIZES], append_images=images[:-1])


def create_shortcut() -> Path:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    start_menu = Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs"
    link = start_menu / f"{APP_NAME}.lnk"
    script = f"""
$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{link}')
$s.TargetPath = '{pythonw}'
$s.Arguments = '-m network_sync.ui'
$s.WorkingDirectory = '{HERE.parent}'
$s.IconLocation = '{ICON},0'
$s.Description = 'Open the 4th year sync dashboard'
$s.Save()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
    return link


if __name__ == "__main__":
    build_icon()
    print(f"Icon:     {ICON}")
    print(f"Shortcut: {create_shortcut()}")
    print(f'Pin it: Start -> type "{APP_NAME}" -> right-click -> Pin to taskbar')
