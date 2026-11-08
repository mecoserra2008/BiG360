"""Logo-BIG.png -> packaging/assets/big360.ico (tray, launcher, installer, shortcuts) + big360-256.png.

The logo (orange 'BiG' on white) is turned into the app-tile style of the web favicon: white letters on an orange
rounded square, which stays legible at 16 px in the notification area.

    .venv\\Scripts\\python.exe packaging\\make_icon.py [--logo Logo-BIG.png] [--out packaging\\assets]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
ORANGE = (255, 98, 0)
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]


def glyph_mask(logo: Image.Image) -> Image.Image:
    """Alpha mask of the orange letters (anti-aliased), cropped to their bounding box."""
    rgb = logo.convert("RGB")
    r, g, b = rgb.split()
    # orange = strong red, low blue; white background has high blue
    m = ImageChops.subtract(r, b)                       # ~255 on orange, ~0 on white / grey
    m = m.point(lambda v: 0 if v < 40 else min(255, int((v - 40) * 255 / 160)))
    box = m.getbbox()
    return m.crop(box) if box else m


def tile(mask: Image.Image, size: int = 1024, radius: float = 0.22, fill: float = 0.80) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg = Image.new("L", (size, size), 0)
    ImageDraw.Draw(bg).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * radius), fill=255)
    img.paste(Image.new("RGBA", (size, size), (*ORANGE, 255)), (0, 0), bg)
    w = int(size * fill)
    h = int(mask.height * w / mask.width)
    glyph = mask.resize((w, h), Image.LANCZOS)
    x, y = (size - w) // 2, (size - h) // 2
    img.paste(Image.new("RGBA", (w, h), (255, 255, 255, 255)), (x, y), glyph)
    return img


def build(logo_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    big = tile(glyph_mask(Image.open(logo_path)))
    frames = []
    for s in SIZES:
        f = big.resize((s, s), Image.LANCZOS)
        if s <= 32:  # a touch of sharpening keeps the letters crisp in the tray
            f = f.filter(ImageFilter.UnsharpMask(radius=0.6, percent=60, threshold=0))
        frames.append(f)
    ico = out_dir / "big360.ico"
    frames[-1].save(ico, format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    big.resize((256, 256), Image.LANCZOS).save(out_dir / "big360-256.png")
    return ico


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logo", default=str(ROOT / "Logo-BIG.png"))
    ap.add_argument("--out", default=str(ROOT / "packaging" / "assets"))
    a = ap.parse_args()
    p = build(Path(a.logo), Path(a.out))
    print(f"wrote {p} ({p.stat().st_size} bytes, sizes {SIZES})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
