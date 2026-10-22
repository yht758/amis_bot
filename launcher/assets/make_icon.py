"""生成管理台图标（PNG + ICO）。

用法：python launcher/assets/make_icon.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

SIZE = 512
OUT_DIR = Path(__file__).resolve().parent
CYAN = np.array([125, 211, 252], dtype=float)
VIOLET = np.array([167, 139, 250], dtype=float)
PINK = np.array([244, 114, 182], dtype=float)
DARK = (8, 13, 22, 255)


def gradient(size: int) -> Image.Image:
    yy, xx = np.mgrid[0:size, 0:size]
    t = (xx + yy) / (2.0 * (size - 1))
    first = CYAN + (VIOLET - CYAN) * np.clip(t / 0.5, 0, 1)[..., None]
    second = VIOLET + (PINK - VIOLET) * np.clip((t - 0.5) / 0.5, 0, 1)[..., None]
    rgb = np.where((t < 0.5)[..., None], first, second)
    return Image.fromarray(rgb.astype(np.uint8), "RGB")


def main() -> None:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    rounded = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(rounded).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=112, fill=255)
    background = gradient(SIZE).convert("RGBA")
    canvas.paste(background, (0, 0), rounded)

    # 深色聊天气泡
    bubble = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(bubble)
    draw.rounded_rectangle((104, 122, 408, 350), radius=64, fill=DARK)
    draw.polygon([(188, 340), (188, 412), (268, 346)], fill=DARK)

    # 气泡内三个渐变圆点（对话 + 脉冲）
    circle = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    circle_draw = ImageDraw.Draw(circle)
    dot_colors = [(125, 211, 252), (167, 139, 250), (244, 114, 182)]
    for index, color in enumerate(dot_colors):
        cx = 184 + index * 72
        cy = 236
        radius = 24 if index != 1 else 30
        circle_draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(*color, 255))
    bubble = Image.alpha_composite(bubble, circle)
    canvas = Image.alpha_composite(canvas, bubble)

    # 右上角小高光
    highlight = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(highlight).ellipse((330, 74, 430, 174), fill=(255, 255, 255, 36))
    canvas = Image.alpha_composite(canvas, highlight)

    png_path = OUT_DIR / "icon.png"
    canvas.save(png_path)
    canvas.resize((256, 256), Image.LANCZOS).save(
        OUT_DIR / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"saved: {png_path}")
    print(f"saved: {OUT_DIR / 'icon.ico'}")


if __name__ == "__main__":
    main()
