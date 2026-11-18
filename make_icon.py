# -*- coding: utf-8 -*-
"""生成轮盘菜单的 ICO 图标（4x 超采样，多尺寸）。
运行： python make_icon.py
输出： app.ico  + app_icon_preview.png（仅预览用）
"""
import math
import os
from PIL import Image, ImageDraw, ImageFilter

BASE = os.path.dirname(os.path.abspath(__file__))
S = 4                      # 超采样倍数
N = 256 * S                # 1024 画布

# 调色板（现代深蓝 + 青蓝高亮）
C_DISC      = (27, 36, 58, 255)      # 底盘深蓝
C_DISC_EDGE = (58, 74, 112, 255)     # 底盘外圈描边
C_SECTOR    = (52, 66, 99, 255)      # 普通扇区
C_SECTOR2   = (44, 57, 88, 255)      # 交替扇区（微差，增加层次）
C_GAP       = (18, 25, 42, 255)      # 扇区间隙
C_HI        = (64, 156, 255, 255)    # 高亮扇区主色
C_HI2       = (56, 214, 255, 255)    # 高亮渐变端
C_HI_GLOW   = (72, 176, 255, 70)     # 高亮外发光
C_CENTER    = (240, 244, 252, 255)   # 中心圆（近白）
C_CENTER_ED = (200, 214, 238, 255)   # 中心圆描边
C_MOUSE     = (96, 112, 142, 255)    # 鼠标轮廓灰
C_MOUSE_BTN = (150, 166, 196, 255)   # 鼠标左键
C_MOUSE_R   = (40, 132, 230, 255)    # 鼠标右键高亮

cx = cy = N / 2
R_DISC   = int(0.455 * N)   # 底盘半径
R_OUT    = int(0.415 * N)   # 扇区外半径
R_IN     = int(0.185 * N)   # 扇区内半径（中心圆覆盖）
R_CENTER = int(0.172 * N)   # 中心圆半径

img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 1) 底盘圆 + 外圈描边
d.ellipse([cx - R_DISC, cy - R_DISC, cx + R_DISC, cy + R_DISC], fill=C_DISC)
d.ellipse([cx - R_DISC, cy - R_DISC, cx + R_DISC, cy + R_DISC],
          outline=C_DISC_EDGE, width=int(N * 0.008))

# 2) 高亮扇区的柔光（先画，铺在底盘上）
HI_IDX = 2                                   # 正右方向
mid_hi = -90 + HI_IDX * 45
d.pieslice([cx - R_OUT - int(N*0.02), cy - R_OUT - int(N*0.02),
            cx + R_OUT + int(N*0.02), cy + R_OUT + int(N*0.02)],
           start=mid_hi - 24, end=mid_hi + 24, fill=C_HI_GLOW)

# 3) 8 个扇区（环形 pie，中心随后被中心圆覆盖）
gap = 3.2
for i in range(8):
    mid = -90 + i * 45
    a0, a1 = mid - 22.5 + gap, mid + 22.5 - gap
    box = [cx - R_OUT, cy - R_OUT, cx + R_OUT, cy + R_OUT]
    if i == HI_IDX:
        # 高亮扇区用两层叠加模拟渐变：底色 + 靠内的亮色
        d.pieslice(box, start=a0, end=a1, fill=C_HI)
        inner_box = [cx - int(R_OUT*0.78), cy - int(R_OUT*0.78),
                     cx + int(R_OUT*0.78), cy + int(R_OUT*0.78)]
        d.pieslice(inner_box, start=a0, end=a1, fill=C_HI2)
    else:
        d.pieslice(box, start=a0, end=a1, fill=C_SECTOR if i % 2 == 0 else C_SECTOR2)

# 4) 扇区之间的分隔线（从内半径到外半径，沿间隙中线）
for i in range(8):
    sep_angle = -90 + i * 45 + 22.5
    rad = math.radians(sep_angle)
    x1 = cx + R_IN * math.cos(rad)
    y1 = cy + R_IN * math.sin(rad)
    x2 = cx + R_OUT * math.cos(rad)
    y2 = cy + R_OUT * math.sin(rad)
    d.line([x1, y1, x2, y2], fill=C_GAP, width=int(N * 0.010))

# 5) 中心圆（覆盖扇区内心）
d.ellipse([cx - R_CENTER, cy - R_CENTER, cx + R_CENTER, cy + R_CENTER],
          fill=C_CENTER)
d.ellipse([cx - R_CENTER, cy - R_CENTER, cx + R_CENTER, cy + R_CENTER],
          outline=C_CENTER_ED, width=int(N * 0.006))

# 6) 中心画鼠标符号（右键高亮），整体略缩小居中
mw = int(0.115 * N)     # 鼠标半宽
mh_top = cy - int(0.105 * N)
mh_bot = cy + int(0.115 * N)
# 鼠标外形：上窄下圆的竖直轮廓（圆角半径调小，避免看起来像脸）
d.rounded_rectangle([cx - mw, mh_top, cx + mw, mh_bot],
                    radius=int(mw * 0.5), fill=C_MOUSE)
# 左右键分界竖线
split_x = cx
# 左键 / 右键填充（顶部区域，加大高度、缩小圆角，强化"两个按键"感）
top_y = mh_top + int(0.004 * N)
btn_bottom = mh_top + int(0.055 * N)
d.rounded_rectangle([cx - mw + int(0.010*N), top_y,
                     split_x - int(0.004*N), btn_bottom],
                    radius=int(0.006 * N), fill=C_MOUSE_BTN)
d.rounded_rectangle([split_x + int(0.004*N), top_y,
                     cx + mw - int(0.010*N), btn_bottom],
                    radius=int(0.006 * N), fill=C_MOUSE_R)
# 滚轮：细长的深色竖条（明确是滚轮而非"嘴"）
wheel_y = mh_top + int(0.062 * N)
d.rounded_rectangle([cx - int(0.007*N), wheel_y, cx + int(0.007*N), wheel_y + int(0.026*N)],
                    radius=int(0.005*N), fill=C_DISC)

# ---- 缩放并保存 ----
small = img.resize((256, 256), Image.LANCZOS)
# 预览图（放大到透明棋盘底色上看）
preview = Image.new("RGBA", (256, 256), (245, 246, 250, 255))
preview.alpha_composite(small)
preview.convert("RGB").save(os.path.join(BASE, "app_icon_preview.png"))

# 多尺寸 ICO
sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
ico_path = os.path.join(BASE, "app.ico")
small.save(ico_path, format="ICO", sizes=sizes)
print("已生成:", ico_path)
print("预览:", os.path.join(BASE, "app_icon_preview.png"))
print("尺寸:", sizes)
