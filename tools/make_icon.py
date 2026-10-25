"""產生程式圖示 assets/app.ico（需要 Pillow；只有修改圖示時才需要執行）。"""
from pathlib import Path

from PIL import Image, ImageDraw


def draw(size: int) -> Image.Image:
    s = 4  # 先畫大圖再縮小，邊緣較平滑
    n = size * s
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 藍色圓角底
    d.rounded_rectangle((0, 0, n - 1, n - 1), radius=n // 5, fill=(28, 110, 220, 255))
    # 雲
    white = (255, 255, 255, 255)
    d.ellipse((n * 0.16, n * 0.34, n * 0.48, n * 0.62), fill=white)
    d.ellipse((n * 0.32, n * 0.20, n * 0.70, n * 0.58), fill=white)
    d.ellipse((n * 0.54, n * 0.34, n * 0.86, n * 0.62), fill=white)
    d.rectangle((n * 0.30, n * 0.46, n * 0.72, n * 0.62), fill=white)
    # 雙向箭頭 ⇄
    blue = (28, 110, 220, 255)
    w = max(2, int(n * 0.045))
    y1, y2 = n * 0.74, n * 0.86
    d.line((n * 0.24, y1, n * 0.76, y1), fill=white, width=w)
    d.polygon([(n * 0.80, y1), (n * 0.68, y1 - n * 0.06), (n * 0.68, y1 + n * 0.06)], fill=white)
    d.line((n * 0.24, y2, n * 0.76, y2), fill=white, width=w)
    d.polygon([(n * 0.20, y2), (n * 0.32, y2 - n * 0.06), (n * 0.32, y2 + n * 0.06)], fill=white)
    # 雲上的「115」
    if size >= 32:
        d.text((n * 0.5, n * 0.45), "115", fill=blue, anchor="mm", font_size=int(n * 0.16))
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "assets" / "app.ico"
    sizes = [16, 24, 32, 48, 64, 128, 256]
    big = draw(256)
    big.save(out, format="ICO", sizes=[(x, x) for x in sizes], append_images=[draw(x) for x in sizes[:-1]])
    big.save(out.with_suffix(".png"))
    print(out)
