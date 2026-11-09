"""生成应用图标（Pillow 绘制，无外部素材、无网络）。

产物：
    packaging/assets/social_secretary.ico   # 多尺寸 Windows 图标（16~256）
    packaging/assets/social_secretary.png   # 256x256 预览（文档/截图用）

设计：圆角渐变色块（蓝→靛）+ 白色对话气泡 + 绿色勾（代表"已整理、不发送"）。
全部为几何绘制，不含任何会让人误以为可发送消息的箭头/纸飞机元素。

用法：
    .venv\\Scripts\\python.exe packaging/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent / "assets"
ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]
BASE = 1024
SS = 4  # 超采样倍数，保证小尺寸边缘干净


def lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def draw_icon(px: int) -> Image.Image:
    """在 px x px 画布上绘制图标（尺寸大时用超采样内部计算）。"""
    size = px * SS if px < 256 else px
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 1) 圆角渐变背景：左上 #3B82F6（蓝）→ 右下 #6366F1（靛）
    grad = Image.new("RGB", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(1, size - 1)
        gd.line([(0, y), (size, y)], fill=(lerp(59, 99, t), lerp(130, 102, t), lerp(246, 241, t)))
    radius = int(size * 0.22)
    img.paste(grad, (0, 0), rounded_mask(size, radius))

    # 2) 白色对话气泡（主体 + 左下小尾巴）
    bx0, by0 = int(size * 0.17), int(size * 0.21)
    bx1, by1 = int(size * 0.83), int(size * 0.70)
    brad = int(size * 0.15)
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=brad, fill=(255, 255, 255, 255))
    tail = [
        (int(size * 0.32), by1 - int(size * 0.02)),
        (int(size * 0.32), int(size * 0.84)),
        (int(size * 0.52), by1 - int(size * 0.02)),
    ]
    d.polygon(tail, fill=(255, 255, 255, 255))

    # 3) 气泡内的勾（绿色 #10B981），表示"整理完成/只读"
    cw = max(2, int(size * 0.055))
    points = [
        (int(size * 0.34), int(size * 0.475)),
        (int(size * 0.455), int(size * 0.60)),
        (int(size * 0.68), int(size * 0.345)),
    ]
    d.line(points, fill=(16, 185, 129, 255), width=cw, joint="curve")
    for p in (points[0], points[2]):
        r = cw // 2
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=(16, 185, 129, 255))

    if size != px:
        img = img.resize((px, px), Image.LANCZOS)
    return img


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    images = [draw_icon(px) for px in ICON_SIZES]
    ico = ASSETS / "social_secretary.ico"
    images[-1].save(
        ico,
        format="ICO",
        sizes=[(im.width, im.height) for im in images],
        append_images=images[:-1],
    )
    png = ASSETS / "social_secretary.png"
    images[-1].save(png, format="PNG")
    print(f"[icon] {ico} ({ico.stat().st_size} bytes)")
    print(f"[icon] {png} ({png.stat().st_size} bytes)")
    for im in images:
        print(f"   {im.width}x{im.height} RGBA")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
