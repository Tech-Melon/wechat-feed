"""生成技术瓜群流 PNG/ICO。SVG 以 web/favicon.svg 为准。

uv run --with pillow python scripts/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def render_png(size: int) -> Image.Image:
    master = 512
    scale = master / 128
    img = Image.new("RGBA", (master, master), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def box(x0: float, y0: float, x1: float, y1: float) -> list[float]:
        return [x0 * scale, y0 * scale, x1 * scale, y1 * scale]

    def point(x: float, y: float) -> tuple[float, float]:
        return (x * scale, y * scale)

    draw.rounded_rectangle([0, 0, master - 1, master - 1], radius=28 * scale, fill="#111827")
    draw.ellipse(box(24, 26, 104, 98), fill="#16a34a")
    draw.polygon(
        [point(26, 106), point(31, 88), point(50, 90), point(36, 100)],
        fill="#16a34a",
    )
    draw.ellipse(box(31, 31, 97, 89), fill="#fefce8")
    draw.ellipse(box(36, 36, 92, 84), fill="#e11d48")
    draw.ellipse(box(42, 42, 62, 54), fill="#f8718a")
    draw.ellipse(box(47, 55, 57, 65), fill="#f8fafc")
    draw.ellipse(box(59, 57, 69, 67), fill="#f8fafc")
    draw.ellipse(box(71, 55, 81, 65), fill="#f8fafc")
    leaf = Image.new("RGBA", (master, master), (0, 0, 0, 0))
    ImageDraw.Draw(leaf).ellipse(box(90, 10, 122, 34), fill="#86efac")
    img = Image.alpha_composite(
        img,
        leaf.rotate(32, resample=Image.Resampling.BICUBIC, center=(96 * scale, 24 * scale)),
    )
    draw = ImageDraw.Draw(img)
    draw.line([point(93, 22), point(110, 38)], fill="#166534", width=max(2, int(2.2 * scale)))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> int:
    svg = (ROOT / "web" / "favicon.svg").read_text(encoding="utf-8")
    (ROOT / "extension" / "icon.svg").write_text(svg, encoding="utf-8")
    png = render_png(128)
    png.save(ROOT / "web" / "favicon.png", "PNG")
    png.save(ROOT / "extension" / "icon.png", "PNG")
    render_png(256).save(
        ROOT / "extension" / "icon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (256, 256)],
    )
    print("已写入 web/favicon.svg|png 与 extension/icon.svg|png|ico")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
