"""The tray icon, drawn rather than shipped.

macOS renders the tray as a text badge — "⬡ Dispatch", "◌ Dispatch",
"⚠ Dispatch" — because a menu bar item can hold a title. The Windows
notification area cannot: it is a 16×16 bitmap and nothing else, so the state
that macOS says in words has to be said in pixels.

Generating the images at runtime with Pillow (already required by pystray)
keeps three binary assets out of the repo and lets the icon render at whatever
size the caller asks for, which matters on a 200% display where a 16px icon is
resampled to mush.

The mark is the same hexagon the macOS badge uses, so the two platforms look
like one product.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Literal

State = Literal["ok", "busy", "error"]

# Tray icons sit on a taskbar that is dark by default but can be light, so the
# stroke carries the meaning and stays legible either way. These are chosen for
# contrast at 16px, where hue is nearly all you can perceive.
_STROKE: dict[str, tuple[int, int, int, int]] = {
    "ok": (61, 194, 122, 255),      # green — connected
    "busy": (232, 168, 60, 255),    # amber — connecting / working
    "error": (226, 84, 76, 255),    # red — needs attention
}
_FILL: dict[str, tuple[int, int, int, int]] = {
    "ok": (61, 194, 122, 70),
    "busy": (232, 168, 60, 55),
    "error": (226, 84, 76, 80),
}


def _hexagon(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    """A flat-topped hexagon, matching the ⬡ glyph's orientation."""
    return [
        (cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
        for a in range(30, 390, 60)
    ]


def render(state: State = "ok", size: int = 64):
    """A PIL image of the tray mark in the given state.

    Drawn at 8× and downsampled: Pillow has no antialiased polygon stroke, and
    at 16px an aliased hexagon reads as a grey blob.
    """
    from PIL import Image, ImageDraw

    scale = 8
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Leave a margin so the stroke is not clipped by the icon bounds; Windows
    # crops nothing, but a mark flush to the edge looks broken next to the
    # system icons, which all sit inset.
    r = big * 0.40
    points = _hexagon(big / 2, big / 2, r)
    draw.polygon(points, fill=_FILL[state])
    draw.line(points + [points[0]], fill=_STROKE[state], width=int(big * 0.075), joint="curve")

    if state == "error":
        # A centre bar reads as "!" at 16px, where a real glyph would not.
        w = big * 0.055
        draw.rounded_rectangle(
            [big / 2 - w, big * 0.34, big / 2 + w, big * 0.60],
            radius=w, fill=_STROKE["error"],
        )
        draw.ellipse(
            [big / 2 - w, big * 0.65, big / 2 + w, big * 0.65 + 2 * w],
            fill=_STROKE["error"],
        )

    return img.resize((size, size), Image.LANCZOS)


def write_ico(path: Path, state: State = "ok") -> Path:
    """Write a multi-resolution .ico.

    Windows picks a size per surface — 16px in the notification area, 32px in
    the Start menu, 48px in Alt-Tab — and scaling one bitmap to all of them is
    what makes a tray icon look amateurish. An .ico holds them all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    base = render(state, 256)
    base.save(path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    return path
