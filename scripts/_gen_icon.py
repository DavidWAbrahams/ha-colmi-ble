"""
Generate the brand icon/logo for colmi_ring (a simple gold ring).

Run from the repo root: ``python scripts/_gen_icon.py``

Writes custom_components/colmi_ring/brand/ (HA >= 2026.3 local brand
images, served by HA at /api/brands/integration/colmi_ring/...):
icon.png (256), icon@2x.png (512), logo.png (128), logo@2x.png (256).
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


def _render() -> Image.Image:
    ss = SCALE
    side = SIZE * ss
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx, cy = side // 2, side // 2
    _ring(draw, ss, cx, cy)

    return img.resize((SIZE, SIZE), Image.LANCZOS)


def _save(img: Image.Image, path: Path, size: int) -> None:
    img.resize((size, size), Image.LANCZOS).save(path)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    brand = root / "custom_components" / "colmi_ring" / "brand"
    brand.mkdir(parents=True, exist_ok=True)
    base = _render()  # 512x512 RGBA, transparent
    _save(base, brand / "icon.png", 256)
    _save(base, brand / "icon@2x.png", 512)
    _save(base, brand / "logo.png", 128)
    _save(base, brand / "logo@2x.png", 256)

    print("wrote custom_components/colmi_ring/brand/{icon,icon@2x,logo,logo@2x}.png")


if __name__ == "__main__":
    main()
