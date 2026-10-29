"""Generate the HACS brand icon/logo for colmi_ring (a simple gold ring).

Run from the repo root: ``python scripts/_gen_icon.py``
Writes repository/icons/colmi_ring.png and repository/logos/colmi_ring.png
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
SCALE = 4  # draw supersampled, then downscale for smooth edges

GOLD = (212, 175, 55, 255)
GOLD_HI = (255, 236, 150, 255)
GOLD_MID = (230, 196, 84, 255)
GOLD_LO = (140, 104, 18, 255)
GOLD_EDGE = (104, 76, 12, 255)


def _ring(draw: ImageDraw.ImageDraw, ss: int, cx: int, cy: int) -> None:
    r_out = int(180 * ss)
    r_in = int(124 * ss)

    # Band: outer disc, then punch the hole.
    draw.ellipse(
        [cx - r_out, cy - r_out, cx + r_out, cy + r_out],
        fill=GOLD,
        outline=GOLD_EDGE,
        width=int(3 * ss),
    )
    draw.ellipse([cx - r_in, cy - r_in, cx + r_in, cy + r_in], fill=(0, 0, 0, 0))

    # Metal shading: bright arc top-left, dark arc bottom-right.
    r_mid = (r_out + r_in) // 2
    thick = int((r_out - r_in) * 0.30)
    box = [cx - r_mid, cy - r_mid, cx + r_mid, cy + r_mid]
    draw.arc(box, start=195, end=300, fill=GOLD_HI, width=thick)
    draw.arc(box, start=20, end=155, fill=GOLD_LO, width=thick)
    # Thin inner-rim shadow so the hole reads as a tube.
    r_rim = r_in + thick // 2
    draw.arc(
        [cx - r_rim, cy - r_rim, cx + r_rim, cy + r_rim],
        start=110,
        end=250,
        fill=GOLD_MID,
        width=int(2 * ss),
    )


def _gem(draw: ImageDraw.ImageDraw, ss: int, cx: int, cy: int) -> None:
    """Small brilliant-cut stone set at the top of the band."""
    gy = cy - int(176 * ss)
    r = int(40 * ss)
    top = (cx, gy - r)
    right = (cx + r, gy)
    bottom = (cx, gy + r)
    left = (cx - r, gy)

    draw.polygon([top, right, bottom, left], fill=GOLD_MID, outline=GOLD_EDGE)
    # Two facets: lit upper-right, shaded lower-left.
    draw.polygon([top, right, (cx, gy)], fill=GOLD_HI)
    draw.polygon([(cx, gy), bottom, left], fill=GOLD_LO)
    draw.line([top, left], fill=GOLD_EDGE, width=max(1, int(1.5 * ss)))
    draw.line([right, bottom], fill=GOLD_EDGE, width=max(1, int(1.5 * ss)))


def _render(with_gem: bool) -> Image.Image:
    ss = SCALE
    side = SIZE * ss
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx, cy = side // 2, side // 2 + int(22 * ss)
    _ring(draw, ss, cx, cy)
    if with_gem:
        _gem(draw, ss, cx, cy)

    return img.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out = root / "repository"
    (out / "icons").mkdir(parents=True, exist_ok=True)
    (out / "logos").mkdir(parents=True, exist_ok=True)

    _render(with_gem=True).save(out / "icons" / "colmi_ring.png")
    _render(with_gem=True).save(out / "logos" / "colmi_ring.png")
    print("wrote repository/icons/colmi_ring.png and repository/logos/colmi_ring.png")


if __name__ == "__main__":
    main()
