"""The tray icon: the Signet mark, drawn rather than shipped.

macOS renders the tray as a text badge — "⬡ Dispatch", "◌ Dispatch",
"⚠ Dispatch" — because a menu bar item can hold a title. The Windows
notification area cannot: it is a 16×16 bitmap and nothing else, so the state
that macOS says in words has to be said in pixels.

The mark is the product's own, ported from ``site/signet-mark-tile.svg``: a
night-coloured tile carrying the signed line — two nodes joined by one stroke,
which happens to be an S. It is transcribed here as geometry rather than
rasterised from the SVG at build time because the path is three cubics and two
circles, and carrying an SVG renderer (or three PNG assets, one per state and
per DPI) to reproduce that would cost more than the twenty lines below. If the
brand mark changes, the SVG is the source of truth and these numbers follow it.

**State.** The clean tile means connected — the ordinary case should look like
the logo and nothing else. Anything needing attention adds a badge dot in the
corner: amber while connecting or working, red on error. So the rule a user
learns is "a dot means look at me", and the icon is otherwise just the app.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

State = Literal["ok", "busy", "error"]

# Straight from site/signet-mark-tile.svg, in its 32-unit viewBox.
_GROUND = (15, 15, 13, 255)        # #0F0F0D — night
_MARK = (246, 245, 241, 255)       # #F6F5F1 — bone
_TILE_RADIUS = 8 / 32              # rx="8"
_INSET = 3 / 32                    # the inner <svg x="3" y="3" w="26" h="26">
_INNER = 26 / 32
_STROKE_W = 2.7
_NODE_R = 2.8
_START = (22.2, 9.0)
# Three cubic segments; each is (control1, control2, end).
_CURVES = (
    ((18.0, 5.2), (10.6, 6.4), (10.6, 11.6)),
    ((10.6, 16.8), (21.4, 15.4), (21.4, 20.6)),
    ((21.4, 25.6), (14.4, 26.8), (10.0, 22.9)),
)
_NODES = ((22.2, 9.0), (10.0, 22.9))

# Badge colours. Only shown when something is not simply fine.
_BADGE = {
    "busy": (232, 168, 60, 255),   # amber — connecting / working
    "error": (226, 84, 76, 255),   # red — needs attention
}


def _cubic(p0, p1, p2, p3, steps: int):
    """Points along one cubic Bézier, including both endpoints."""
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append((
            u * u * u * p0[0] + 3 * u * u * t * p1[0]
            + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1]
            + 3 * u * t * t * p2[1] + t * t * t * p3[1],
        ))
    return out


def render(state: State = "ok", size: int = 64):
    """A PIL image of the Signet tile in the given state.

    Drawn at 8× and downsampled: Pillow has no antialiased stroke, and at 16px
    an aliased curve reads as a grey smudge rather than as a letterform.
    """
    from PIL import Image, ImageDraw

    scale = 8
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle(
        [0, 0, big - 1, big - 1], radius=_TILE_RADIUS * big, fill=_GROUND
    )

    # Map the inner 32-unit viewBox onto the inset area of the tile.
    origin = _INSET * big
    unit = (_INNER * big) / 32.0

    def pt(p):
        return (origin + p[0] * unit, origin + p[1] * unit)

    points = [pt(_START)]
    cursor = _START
    for c1, c2, end in _CURVES:
        points.extend(pt(p) for p in _cubic(cursor, c1, c2, end, 48)[1:])
        cursor = end

    width = max(1, round(_STROKE_W * unit))
    draw.line(points, fill=_MARK, width=width, joint="curve")

    # Round caps: ImageDraw has no cap style, and a butt-ended stroke makes the
    # line look snapped off where the SVG has it meeting the nodes.
    radius = width / 2
    for x, y in (points[0], points[-1]):
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=_MARK)

    node_r = _NODE_R * unit
    for node in _NODES:
        x, y = pt(node)
        draw.ellipse([x - node_r, y - node_r, x + node_r, y + node_r], fill=_MARK)

    badge = _BADGE.get(state)
    if badge is not None:
        # Bottom-right, with a ground-coloured ring so it reads as a badge
        # sitting on the tile rather than as part of the mark.
        cx = cy = big * 0.80
        r = big * 0.148
        ring = r * 1.34
        draw.ellipse([cx - ring, cy - ring, cx + ring, cy + ring], fill=_GROUND)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=badge)

    return img.resize((size, size), Image.LANCZOS)


def write_ico(path: Path, state: State = "ok") -> Path:
    """Write a multi-resolution .ico.

    Windows picks a size per surface — 16px in the notification area, 32px in
    the Start menu, 48px in Alt-Tab — and scaling one bitmap to all of them is
    what makes an app icon look amateurish. An .ico holds them all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    base = render(state, 256)
    base.save(
        path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)],
    )
    return path
