"""生成桌面端与 exe 用的图标 ``src/picsaver/desktop/icon.ico``（用 Pillow 画，无需设计资源）。

与 Web 界面配色一致：深色底 + 琥珀色强调，图案是"相机"。
用法::

    python scripts\\make_icon.py            # 已存在则跳过
    python scripts\\make_icon.py --force    # 强制重新生成
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ICON_PATH = ROOT / "src" / "picsaver" / "desktop" / "icon.ico"

BG = (20, 22, 26, 255)
ACCENT = (224, 164, 74, 255)
ACCENT_DARK = (150, 106, 40, 255)
SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw_icon(size: int = 256) -> Image.Image:
    """画一枚"深色底 + 琥珀相机"的图标。"""
    scale = size / 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def box(coords: tuple[float, float, float, float], **kwargs) -> None:
        draw.rounded_rectangle([value * scale for value in coords], **kwargs)

    # 圆角底板 + 琥珀描边
    box((6, 6, 250, 250), radius=48 * scale, fill=BG, outline=ACCENT, width=max(1, int(6 * scale)))
    # 顶部取景器凸起
    box((96, 58, 160, 88), radius=10 * scale, fill=ACCENT)
    # 机身上下
    box((46, 82, 210, 200), radius=22 * scale, fill=ACCENT)
    box((56, 92, 200, 190), radius=16 * scale, fill=ACCENT_DARK)
    # 镜头
    draw.ellipse([(94 * scale, 112 * scale), (162 * scale, 180 * scale)], fill=BG)
    draw.ellipse([(110 * scale, 128 * scale), (146 * scale, 164 * scale)], fill=ACCENT)
    draw.ellipse([(120 * scale, 138 * scale), (136 * scale, 154 * scale)], fill=BG)
    # 闪光灯小点
    draw.ellipse([(176 * scale, 100 * scale), (192 * scale, 116 * scale)], fill=BG)
    return image


def main() -> int:
    parser = argparse.ArgumentParser(description="生成桌面端图标 icon.ico")
    parser.add_argument("--force", action="store_true", help="已存在时也重新生成")
    args = parser.parse_args()

    if ICON_PATH.exists() and not args.force:
        print(f"已存在，跳过：{ICON_PATH}")
        return 0

    ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
    icon = draw_icon(256)
    icon.save(ICON_PATH, format="ICO", sizes=[(size, size) for size in SIZES])
    print(f"已生成：{ICON_PATH}（{ICON_PATH.stat().st_size} 字节，含 {len(SIZES)} 种尺寸）")

    # 顺手导出 256px PNG，便于在文档里预览
    preview = ROOT / "docs" / "screenshots" / "desktop-icon.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    icon.save(preview, format="PNG")
    print(f"预览图：{preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
