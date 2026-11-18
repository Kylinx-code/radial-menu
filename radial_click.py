# -*- coding: utf-8 -*-
"""
右键拖拽轮盘菜单  —— 复刻 Quicker "按住鼠标右键轻轻一拉" 的核心交互

交互逻辑：
  1. 右键按下：立刻吞掉（返回 1），系统收不到，绝不会弹原生右键菜单。
     记录起点。
  2. 右键按住并移动超过 trigger_distance：在【按下点】弹出 8 扇区轮盘，
     鼠标朝哪个方向，对应扇区高亮。
  3. 右键松开：
       - 拖拽激活过 -> 打开高亮扇区绑定的文件夹（右键仍被吞，不弹菜单）。
       - 纯单击（没拖拽） -> 重放一个合成的右键单击，让原生菜单照常弹出。
       - 拖拽中按 Esc -> 取消，不打开、也不弹菜单。

线程架构（关键，修复"系统鼠标卡死"）：
  - hook 线程：只安装 WH_MOUSE_LL 钩子 + PumpMessages。回调只做状态判断 +
    PostMessage / 合成事件，极速返回 CallNext。绝不在此线程做任何耗时操作
    （不开文件夹、不建窗、不渲染），否则会卡住整个系统的鼠标输入。
  - UI 线程：创建 host 窗口 + 轮盘窗口，处理显示/高亮/隐藏/打开文件夹。
    打开文件夹再单独起临时线程，避免 Shell 调用阻塞 UI 消息循环。

右键菜单原理：Explorer 的右键菜单由系统合成 WM_CONTEXTMENU 弹出，该消息不走
鼠标钩子，所以只在 UP 时吞消息没用。正确做法是 DOWN 时就吞掉，让系统不知道
有右键按下；单击时再手动重放一个完整右键事件。

运行：  python radial_click.py
自检：  python radial_click.py --selftest
预览：  python radial_click.py --preview     (生成 wheel_preview.png)
退出：  托盘图标右键 -> 退出，或 Ctrl+Alt+Shift+Q
"""

import ctypes
from ctypes import wintypes as w
import colorsys
import fnmatch
import json
import math
import os
import subprocess
import sys
import threading
import time
import logging
from collections import Counter
from logging.handlers import RotatingFileHandler

from PIL import Image, ImageDraw, ImageFont
import pythoncom
import win32api
import win32con
import win32gui

# ---------------------------------------------------------------- 路径 / 资源
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 打包后：配置放 exe 所在目录（可编辑、持久化）；源码运行：放脚本目录
if getattr(sys, "frozen", False):
    USER_DIR = os.path.dirname(sys.executable)
else:
    USER_DIR = BASE_DIR
CONFIG_PATH = os.path.join(USER_DIR, "folders.json")


def resource_path(rel):
    """返回资源绝对路径：兼容源码运行与 PyInstaller 打包（_MEIPASS）。"""
    try:
        base = sys._MEIPASS           # PyInstaller 单文件解压目录
    except AttributeError:
        base = BASE_DIR
    return os.path.join(base, rel)


def load_app_icon(size=32):
    """从 app.ico 加载图标句柄；失败返回 None（调用方回退系统图标）。"""
    import ctypes
    path = resource_path("app.ico")
    if not os.path.exists(path):
        return None
    try:
        # LR_LOADFROMFILE | LR_DEFAULTSIZE；icon 尺寸用 size
        h = ctypes.windll.user32.LoadImageW(
            0, path, win32con.IMAGE_ICON, size, size,
            win32con.LR_LOADFROMFILE)
        return h or None
    except Exception:
        return None

# 3 层玫瑰轮盘：3 个同心环 × 8 方向 = 24 格。
# 索引规则：idx = 层号 * 8 + 方向号
#   层 0 = 最内环，层 1 = 中环，层 2 = 最外环
#   方向 0..7 = 正上、右上、正右、右下、正下、左下、正左、左上（顺时针）
# 半径布局：中心死区到 inner_radius；三环平分 inner_radius~outer_radius。
DEFAULT_CONFIG = {
    "trigger_distance": 18,
    "inner_radius": 56,      # 中心死区半径（小于它不选中）——已放大
    "outer_radius": 216,     # 最外环外缘；3 环平分 56~216，每环约 53px——已放大
    "cancel_margin": 80,     # 越界取消余量：鼠标离轮盘圆心超过 outer_radius+本值 → 本次拖拽作废
    "opacity": 92,           # 轮盘整体透明度 0~100（100=不透明，越低越透）
    "render_scale": 3,       # 超采样倍率 1~4，越高越清晰（3 已足够锐利）
    # 排除程序：这些软件里的右键完全放行（例如思维导图右键是拖画布）。
    # 写进程名关键字即可，不区分大小写、支持 * 通配符，如 "xmind" / "*mind*.exe"。
    # 托盘菜单「识别并添加排除程序」可自动识别鼠标下的软件并写入，无需手查。
    "exclude_processes": [],
    # 可选：按窗口标题关键字排除（例如 "XMind" / "思维导图"）。
    "exclude_titles": [],
    "folders": [
        # ---- 内环（层 0，idx 0-7）----
        {"name": "桌面",   "path": "%USERPROFILE%\\Desktop"},
        {"name": "下载",   "path": "%USERPROFILE%\\Downloads"},
        {"name": "文档",   "path": "%USERPROFILE%\\Documents"},
        {"name": "图片",   "path": "%USERPROFILE%\\Pictures"},
        {"name": "工作区", "path": "%PUBLIC%\\Documents"},
        {"name": "临时",   "path": "%TEMP%"},
        {"name": "控制面板", "path": "control"},
        {"name": "回收站", "path": "shell:RecycleBinFolder"},
        # ---- 中环（层 1，idx 8-15）----
        {"name": "音乐",   "path": "%USERPROFILE%\\Music"},
        {"name": "视频",   "path": "%USERPROFILE%\\Videos"},
        {"name": "收藏夹", "path": "%USERPROFILE%\\Favorites"},
        {"name": "字体",   "path": "C:\\Windows\\Fonts"},
        {"name": "系统32", "path": "C:\\Windows\\System32"},
        {"name": "Windows", "path": "C:\\Windows"},
        {"name": "C盘",    "path": "C:\\"},
        {"name": "用户",   "path": "%USERPROFILE%"},
        # ---- 外环（层 2，idx 16-23）----
        {"name": "外·上",  "path": "%USERPROFILE%\\Desktop"},
        {"name": "外·右上", "path": "%USERPROFILE%\\Documents"},
        {"name": "外·右",  "path": "%USERPROFILE%\\Downloads"},
        {"name": "外·右下", "path": "%USERPROFILE%\\Pictures"},
        {"name": "外·下",  "path": "%TEMP%"},
        {"name": "外·左下", "path": "%PUBLIC%\\Desktop"},
        {"name": "外·左",  "path": "C:\\"},
        {"name": "外·左上", "path": "%USERPROFILE%"},
    ],
}
CONFIG = dict(DEFAULT_CONFIG)

# ---------------------------------------------------------------- 日志
# 所有运行事件写入 logs/radial.log（带时间戳、自动轮转）。
# 无控制台窗口的 pythonw 模式下 stdout 可能为 None，log() 内部做保护。
LOG_DIR = os.path.join(USER_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "radial.log")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except Exception:
    pass

_orig_print = print
_logger = logging.getLogger("radial_click")
_logger.setLevel(logging.INFO)
if not _logger.handlers:
    try:
        _fh = RotatingFileHandler(LOG_FILE, maxBytes=1024 * 1024, backupCount=3,
                                  encoding="utf-8")
        _fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        _logger.addHandler(_fh)
    except Exception:
        pass


def log(msg):
    """写日志（文件），并在控制台可用时同步打印（自检/预览时可见）。"""
    text = str(msg)
    try:
        _logger.info(text)
    except Exception:
        pass
    try:
        if sys.stdout is not None:
            _orig_print(text, flush=True)
    except Exception:
        pass


def log_error(msg):
    try:
        _logger.error(str(msg))
    except Exception:
        pass
    log(msg)


def load_config():
    global CONFIG
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            for k in ("trigger_distance", "inner_radius", "outer_radius",
                      "cancel_margin", "opacity", "render_scale"):
                if k in user:
                    try:
                        cfg[k] = int(user[k])
                    except (TypeError, ValueError):
                        pass
            # 数值范围钳制，防止配置写错导致渲染异常
            cfg["opacity"] = max(20, min(100, cfg["opacity"]))
            cfg["render_scale"] = max(1, min(4, cfg["render_scale"]))
            cfg["cancel_margin"] = max(0, min(400, cfg["cancel_margin"]))
            # 排除列表：只收非空字符串，忽略写错的类型
            for k in ("exclude_processes", "exclude_titles"):
                v = user.get(k)
                if isinstance(v, list):
                    cfg[k] = [str(s).strip() for s in v
                              if isinstance(s, str) and s.strip()]
            if isinstance(user.get("folders"), list) and user["folders"]:
                defaults = cfg["folders"]          # 24 项默认，用于补齐
                folders = []
                for i in range(24):                # 始终是 24 格
                    uf = user["folders"]
                    if i < len(uf) and isinstance(uf[i], dict) and uf[i].get("path"):
                        folders.append({
                            "name": str(uf[i].get("name") or "未命名"),
                            "path": str(uf[i]["path"]),
                        })
                    else:
                        folders.append(dict(defaults[i]))
                cfg["folders"] = folders
    except Exception as e:
        log(f"[警告] 读取配置失败：{e}，使用默认配置。")
    CONFIG.clear()
    CONFIG.update(cfg)
    # 配置变了（名称/大小/透明度/清晰度）必须重建帧缓存，否则显示旧图
    try:
        clear_frame_cache()
    except Exception:
        pass
    if not os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return cfg


def open_target(path):
    """打开文件夹 / 命令（在临时线程里调用，避免阻塞消息循环）。"""
    path = os.path.expandvars(os.path.expanduser(path))
    try:
        if path.lower().startswith("cmd:"):
            subprocess.Popen(path[4:].strip(), shell=True)
        else:
            os.startfile(path)
    except Exception as e:
        log_error(f"[错误] 打开失败 {path!r}: {e}")


# ---------------------------------------------------------------- 几何计算
SECTOR_COUNT = 8
LAYER_COUNT = 3
CELL_COUNT = SECTOR_COUNT * LAYER_COUNT   # 24
WHEEL_PAD = 34       # 轮盘画布边距；渲染与 DIB 缓冲区必须共用，否则内存越界
CANCEL_FRAME = -2    # 特殊高亮态：鼠标甩出有效范围，本轮作废（无高亮 + 中心提示）


def wheel_size(cfg=None):
    """轮盘图像/缓冲区的边长（渲染与 DIB 初始化都调用它，保证一致）。"""
    cfg = cfg or CONFIG
    return 2 * (cfg["outer_radius"] + WHEEL_PAD)


def wheel_origin(start_x, start_y, cfg=None):
    """轮盘窗口左上角（屏幕坐标），含屏幕边缘钳制。

    关键：轮盘绘制与命中判定必须共用本函数的结果。此前显示时被钳制、
    命中却用原始按下点当圆心，在屏幕边缘就会“鼠标指 A、高亮 B”。
    """
    cfg = cfg or CONFIG
    size = wheel_size(cfg)
    sw = win32api.GetSystemMetrics(0)
    sh = win32api.GetSystemMetrics(1)
    ox = int(start_x - size / 2)
    oy = int(start_y - size / 2)
    ox = max(0, min(ox, sw - size))
    oy = max(0, min(oy, sh - size))
    return ox, oy


def wheel_center(start_x, start_y, cfg=None):
    """轮盘的实际圆心（钳制后）。命中判定的唯一圆心，不可用原始按下点。"""
    cfg = cfg or CONFIG
    ox, oy = wheel_origin(start_x, start_y, cfg)
    half = wheel_size(cfg) / 2.0
    return ox + half, oy + half


def sector_from_angle(dx, dy):
    """方向 0..7：0=正上，顺时针每 45°。"""
    angle = math.degrees(math.atan2(dy, dx))            # -180~180, 0=右, -90=上
    return int(math.floor((angle + 90.0) / 45.0 + 0.5)) % SECTOR_COUNT


def sector_mid_angle(i, count=SECTOR_COUNT):
    return -90.0 + i * (360.0 / count)


def layer_from_dist(dist, inner_radius, outer_radius):
    """半径 -> 层 0..2（0=最内环）。小于内半径返回 -1（中心死区）。"""
    if dist < inner_radius:
        return -1
    band = (outer_radius - inner_radius) / LAYER_COUNT
    layer = int((dist - inner_radius) / band)
    return min(LAYER_COUNT - 1, max(0, layer))


def cell_from_point(cx, cy, x, y, inner_radius, outer_radius):
    """3 层轮盘格子索引 0..23 = 层*8 + 方向；中心死区返回 -1。"""
    dx, dy = x - cx, y - cy
    dist = math.hypot(dx, dy)
    layer = layer_from_dist(dist, inner_radius, outer_radius)
    if layer < 0:
        return -1
    return layer * SECTOR_COUNT + sector_from_angle(dx, dy)


def cancel_radius(cfg=None):
    """越界取消半径：鼠标离轮盘圆心超过此距离，本次拖拽即作废。"""
    cfg = cfg or CONFIG
    return cfg["outer_radius"] + cfg.get("cancel_margin", 80)


def is_outside(cx, cy, x, y, cfg=None):
    """鼠标是否已甩出有效范围（进入取消区）。"""
    return math.hypot(x - cx, y - cy) > cancel_radius(cfg)


# ---------------------------------------------------------------- 轮盘绘制（PIL）
FONT_CACHE = {}


def get_font(size, bold=False):
    key = (size, bold)
    if key in FONT_CACHE:
        return FONT_CACHE[key]
    path = r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc"
    try:
        f = ImageFont.truetype(path, size)
    except Exception:
        f = ImageFont.load_default()
    FONT_CACHE[key] = f
    return f


PALETTE = {
    "bg":        (26, 22, 34, 246),
    "bg_edge":   (120, 96, 132, 255),
    "hl_edge":   (255, 236, 168, 255),     # 高亮描金
    "text":      (255, 248, 244, 255),
    "text_dim":  (242, 224, 226, 255),
    "text_dark": (58, 26, 44, 255),
    "center":    (22, 18, 30, 252),
    "center_rg": (196, 150, 178, 255),
}

# 玫瑰渐变：三层色相从内到外 紫红 -> 玫红 -> 珊瑚粉；越外越亮，像绽放的花瓣
ROSE_HUES = (332.0, 348.0, 8.0)
ROSE_LIGHT = (0.40, 0.52, 0.64)


def rose_color(layer, sector, highlight=False):
    """返回一片花瓣的 RGB；相邻扇区明度交替，形成玫瑰层次。"""
    h = ((ROSE_HUES[layer] + sector * 1.5) % 360.0) / 360.0
    l = ROSE_LIGHT[layer] + (0.035 if sector % 2 else 0.0)
    s = 0.62
    if highlight:
        l = min(0.88, l + 0.24)
        s = 0.70
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return (int(r * 255), int(g * 255), int(b * 255))


def annular_petal(d, cx, cy, r0, r1, a0, a1, fill, outline=None, width=1, steps=48):
    """画一片环形花瓣（扇环多边形）+ 可选描边。

    steps 默认从 12 提到 48：弧线更贴合真正圆弧，缩小后边缘不会有棱角/锯齿。
    """
    pts = []
    for i in range(steps + 1):
        a = math.radians(a0 + (a1 - a0) * i / steps)
        pts.append((cx + r1 * math.cos(a), cy + r1 * math.sin(a)))
    for i in range(steps, -1, -1):
        a = math.radians(a0 + (a1 - a0) * i / steps)
        pts.append((cx + r0 * math.cos(a), cy + r0 * math.sin(a)))
    d.polygon(pts, fill=fill)
    if outline:
        d.line(pts + [pts[0]], fill=outline, width=width, joint="curve")


_ROT_TEXT_CACHE = {}


def radial_text(img, text, font, fill, cx, cy, r, mid_deg):
    """沿切向（圆弧方向）绘制正立标签，贴合环形扇区的长边，带旋转缓存。

    font 必须已按超采样倍率放大；所有坐标是超采样画布坐标。
    旋转用 BICUBIC；整张超采样画布最后统一 LANCZOS 缩小，文字边缘因此锐利。
    """
    if not text:
        return
    # 文字长轴 = 切线方向 = mid+90；再归一化到 [-90,90] 保证字符不倒置。
    # 结果等价于 mid%180-90：正上/正下水平、正左/正右竖直，全部正立可读。
    theta = (mid_deg % 180.0) - 90.0
    # 临时画布按字体高度自适应，避免长名字被裁切；留足旋转余量。
    box = max(240, int(len(text) * font.size * 1.3) + 40)
    key = (text, font.size, tuple(fill), round(theta, 1), box)
    rot = _ROT_TEXT_CACHE.get(key)
    if rot is None:
        tmp = Image.new("RGBA", (box, font.size + 24), (0, 0, 0, 0))
        ImageDraw.Draw(tmp).text((box / 2, (font.size + 24) / 2), text,
                                 font=font, fill=fill, anchor="mm")
        rot = tmp.rotate(theta, resample=Image.BICUBIC, expand=True)
        _ROT_TEXT_CACHE[key] = rot
    rad = math.radians(mid_deg)
    px, py = cx + r * math.cos(rad), cy + r * math.sin(rad)
    img.alpha_composite(rot, (int(px - rot.width / 2), int(py - rot.height / 2)))


def render_wheel(selected=-1, cfg=None):
    """渲染 3 层玫瑰轮盘（24 格）返回 RGBA Image。selected: -1 无高亮。

    清晰度：在 ss 倍超采样画布上绘制（弧线更平滑、字号更大），
    最后用 LANCZOS 高质量缩小回目标尺寸。返回图尺寸恒为 wheel_size()，
    与 DIB 缓冲区严格一致，不会内存越界。
    """
    cfg = cfg or CONFIG
    size = wheel_size(cfg)        # 目标尺寸（与 DIB 共用）
    ss = max(1, min(4, int(cfg.get("render_scale", 3))))
    S = size * ss                 # 超采样画布边长
    R = cfg["outer_radius"] * ss
    ri = cfg["inner_radius"] * ss

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = S / 2
    folders = cfg.get("folders", [])

    bbox = [cx - R - 7 * ss, cy - R - 7 * ss, cx + R + 7 * ss, cy + R + 7 * ss]
    d.ellipse(bbox, fill=PALETTE["bg"], outline=PALETTE["bg_edge"], width=2 * ss)

    band = (R - ri) / LAYER_COUNT
    ang_half = 180.0 / SECTOR_COUNT - 1.5      # 每片花瓣半角，留 3° 缝隙
    gap_r = 3.0 * ss                           # 环间/内外缝隙
    # 环形扇区越靠内越窄，字体逐层缩小；名称过长则截断，避免溢出到相邻格
    base_sizes = (11, 12, 13)
    f_labels = [get_font(n * ss, bold=True) for n in base_sizes]
    f_small = get_font(11 * ss, bold=False)

    def short_name(s, layer):
        limit = (4, 5, 6)[layer]          # 内环最多 4 字，中环 5，外环 6
        return s if len(s) <= limit else s[:limit - 1] + "…"

    for layer in range(LAYER_COUNT):
        r0 = ri + layer * band + gap_r
        r1 = ri + (layer + 1) * band - gap_r
        for sec in range(SECTOR_COUNT):
            idx = layer * SECTOR_COUNT + sec
            mid = sector_mid_angle(sec)
            a0, a1 = mid - ang_half, mid + ang_half
            hi = (idx == selected)
            rgb = rose_color(layer, sec, highlight=hi)
            fill = rgb + (255,)
            if hi:
                annular_petal(d, cx, cy, r0, r1, a0, a1,
                              fill, PALETTE["hl_edge"], width=3 * ss)
            else:
                annular_petal(d, cx, cy, r0, r1, a0, a1,
                              fill, (18, 12, 24, 150), width=1 * ss)
            name = folders[idx].get("name", "") if idx < len(folders) else ""
            name = short_name(name, layer)
            lr = (r0 + r1) / 2.0
            txt = PALETTE["text_dark"] if hi else PALETTE["text"]
            radial_text(img, name, f_labels[layer], txt, cx, cy, lr, mid)

    rc = ri - 3 * ss
    d.ellipse([cx - rc, cy - rc, cx + rc, cy + rc],
              fill=PALETTE["center"], outline=PALETTE["center_rg"], width=2 * ss)
    if selected == CANCEL_FRAME:
        # 越界取消态：所有花瓣保持常态色（无描金高亮），中心明确提示已作废
        d.text((cx, cy - 9 * ss), "已取消", font=get_font(15 * ss, bold=True),
               fill=PALETTE["center_rg"], anchor="mm")
        d.text((cx, cy + 11 * ss), "松手不打开", font=f_small,
               fill=PALETTE["center_rg"], anchor="mm")
    elif 0 <= selected < CELL_COUNT:
        ctext = folders[selected].get("name", "") if selected < len(folders) else ""
        sub = f"第 {selected // SECTOR_COUNT + 1} 环"
        d.text((cx, cy - 9 * ss), ctext, font=get_font(15 * ss, bold=True),
               fill=PALETTE["text"], anchor="mm")
        d.text((cx, cy + 11 * ss), sub, font=f_small,
               fill=PALETTE["center_rg"], anchor="mm")
    else:
        d.text((cx, cy - 7 * ss), "松手打开", font=get_font(13 * ss, bold=True),
               fill=PALETTE["text"], anchor="mm")
        d.text((cx, cy + 11 * ss), "3环·24格", font=f_small,
               fill=PALETTE["center_rg"], anchor="mm")

    if ss > 1:
        img = img.resize((size, size), Image.LANCZOS)
    return img


# 帧缓存：超采样 LANCZOS 缩小每帧约 45ms，快速拖动会明显掉帧。
# 预先把 25 个状态（-1 无高亮 + 24 格）渲染好，切换时直接取图，零开销。
_FRAME_CACHE = {}


def get_wheel_frame(selected):
    """取指定高亮态的成品帧（带缓存）；配置变化时由 clear_frame_cache 清空。"""
    key = int(selected)
    frame = _FRAME_CACHE.get(key)
    if frame is None:
        frame = render_wheel(key)
        _FRAME_CACHE[key] = frame
    return frame


def clear_frame_cache():
    _FRAME_CACHE.clear()
    # 透明度/尺寸也可能随配置变，字体旋转缓存含尺寸信息无需清；
    # 但 _ROT_TEXT_CACHE 会随字号变化自然增长，清空释放内存。
    _ROT_TEXT_CACHE.clear()


# ---------------------------------------------------------------- 分层窗口 + DIB
class BIH(ctypes.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
                ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG),
                ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]


class BI(ctypes.Structure):
    _fields_ = [("bmiHeader", BIH), ("bmiColors", w.DWORD * 3)]


class POINT(ctypes.Structure):
    _fields_ = [("x", w.LONG), ("y", w.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", w.LONG), ("cy", w.LONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)

# 给合成右键事件打标记：钩子读到该标记就无条件放行，避免合成事件被当成
# 新点击再次触发 replay（竞态递归 → 菜单刷屏 → 系统卡死）。比时序 flag 可靠。
SYNTH_MARK = 0x514B4D31   # "QKM1"

GDI = ctypes.windll.gdi32
USER32 = ctypes.windll.user32
GDI.CreateCompatibleDC.argtypes = [w.HDC]; GDI.CreateCompatibleDC.restype = w.HDC
GDI.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]; GDI.SelectObject.restype = w.HGDIOBJ
GDI.CreateDIBSection.argtypes = [w.HDC, ctypes.POINTER(BI), w.UINT,
                                 ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD]
GDI.CreateDIBSection.restype = w.HBITMAP
USER32.UpdateLayeredWindow.argtypes = [w.HWND, w.HDC, ctypes.POINTER(POINT),
                                       ctypes.POINTER(SIZE), w.HDC, ctypes.POINTER(POINT),
                                       w.DWORD, ctypes.POINTER(BLENDFUNCTION), w.DWORD]
USER32.UpdateLayeredWindow.restype = w.BOOL
USER32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, w.HINSTANCE, w.DWORD]
USER32.SetWindowsHookExW.restype = w.HHOOK
USER32.CallNextHookEx.argtypes = [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM]
USER32.CallNextHookEx.restype = ctypes.c_ssize_t
USER32.UnhookWindowsHookEx.argtypes = [w.HHOOK]
USER32.UnhookWindowsHookEx.restype = w.BOOL
USER32.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
USER32.PostThreadMessageW.restype = w.BOOL
# 钩子回调里必须用 GetAsyncKeyState（非阻塞）；GetKeyState 读队列快照，在
# 低级钩子回调中有阻塞/失真风险。
USER32.GetAsyncKeyState.argtypes = [ctypes.c_int]
USER32.GetAsyncKeyState.restype = ctypes.c_short

# ---- 排除程序识别所需的 Win32 API（都在钩子回调里同步调用，必须极轻量）----
KERNEL32 = ctypes.windll.kernel32
USER32.WindowFromPoint.argtypes = [POINT]
USER32.WindowFromPoint.restype = w.HWND
USER32.GetAncestor.argtypes = [w.HWND, w.UINT]
USER32.GetAncestor.restype = w.HWND
USER32.GetForegroundWindow.argtypes = []
USER32.GetForegroundWindow.restype = w.HWND
USER32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
USER32.GetWindowThreadProcessId.restype = w.DWORD
USER32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
USER32.GetWindowTextW.restype = ctypes.c_int
USER32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
USER32.GetCursorPos.restype = w.BOOL
USER32.MessageBoxW.argtypes = [w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT]
USER32.MessageBoxW.restype = ctypes.c_int
KERNEL32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
KERNEL32.OpenProcess.restype = w.HANDLE
KERNEL32.QueryFullProcessImageNameW.argtypes = [
    w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
KERNEL32.QueryFullProcessImageNameW.restype = w.BOOL
KERNEL32.CloseHandle.argtypes = [w.HANDLE]
KERNEL32.CloseHandle.restype = w.BOOL

MSG_SHOW = win32con.WM_USER + 10
MSG_SELECT = win32con.WM_USER + 11
MSG_HIDE = win32con.WM_USER + 12
MSG_OPEN = win32con.WM_USER + 13     # 拖拽松手：打开扇区对应文件夹
MSG_REPLAY = win32con.WM_USER + 14   # 纯单击：通知 UI 线程异步重放右键
MSG_CANCEL = win32con.WM_USER + 15   # 甩出有效范围：轮盘显示"已取消"，不打开
MSG_RELOAD = win32con.WM_USER + 16   # 排除列表写入配置后，回 UI 线程重载
# 防止自定义消息值碰撞：曾因 REPLAY 与 OPEN 同为 +13，导致拖拽松手被误判为
# 重放右键（弹原生菜单、不打开文件夹）。新增消息务必保持唯一。
assert len({MSG_SHOW, MSG_SELECT, MSG_HIDE, MSG_OPEN, MSG_REPLAY,
            MSG_CANCEL, MSG_RELOAD}) == 7, "自定义消息值冲突！请检查 WM_USER 偏移量"

HHOOK = None
HWND_HOST = None
HWND_WHEEL = None
TRAY_MSG = win32con.WM_USER + 100
HOTKEY_QUIT = 100
STATE = None
_HOOK_CALLBACK_REF = None

HOOK_THREAD_ID = None
UI_THREAD_ID = None
STOP_EVENT = threading.Event()
WINDOWS_READY = threading.Event()

_REPLAYING = False   # 正在重放合成右键事件，钩子回调需跳过

# 轮盘绘图资源（UI 线程持有）
_DIB_PTR = None
_DIB_PTR_C = None
_MEMDC = None
_BMP = None
_BMP_OLD = None
_DC = None
_IMG_W = _IMG_H = 0
_CX = _CY = 0
_ORIGIN_X = _ORIGIN_Y = 0


def _init_dib(size):
    global _DC, _MEMDC, _BMP, _BMP_OLD, _IMG_W, _IMG_H, _CX, _CY, _DIB_PTR, _DIB_PTR_C
    _IMG_W = _IMG_H = size
    _CX = _CY = size / 2
    _DC = win32gui.GetDC(0)
    _MEMDC = GDI.CreateCompatibleDC(_DC)
    bi = BI()
    bh = bi.bmiHeader
    bh.biSize = ctypes.sizeof(BIH)
    bh.biWidth = size
    bh.biHeight = -size
    bh.biPlanes = 1
    bh.biBitCount = 32
    bh.biCompression = 0
    pp = ctypes.c_void_p()
    _BMP = GDI.CreateDIBSection(_MEMDC, ctypes.byref(bi), 0, ctypes.byref(pp), None, 0)
    _DIB_PTR = pp.value
    _DIB_PTR_C = pp
    _BMP_OLD = GDI.SelectObject(_MEMDC, _BMP)


def _wheel_update(selected):
    global _ORIGIN_X, _ORIGIN_Y
    img = get_wheel_frame(selected)
    r, g, b, a = img.split()
    bgra = Image.merge("RGBA", (b, g, r, a)).tobytes()
    ctypes.memmove(_DIB_PTR, bgra, len(bgra))
    pt = POINT(_ORIGIN_X, _ORIGIN_Y)
    sz = SIZE(_IMG_W, _IMG_H)
    src = POINT(0, 0)
    # SourceConstantAlpha 控制整窗透明度：配置 0~100 映射到 0~255。
    # 用整窗 alpha 而非逐像素改 alpha 通道，文字/花瓣边缘混合更干净。
    opacity = max(20, min(100, int(CONFIG.get("opacity", 92))))
    bf = BLENDFUNCTION(0, 0, int(round(opacity * 255 / 100.0)), 1)
    USER32.UpdateLayeredWindow(HWND_WHEEL, None, ctypes.byref(pt), ctypes.byref(sz),
                               _MEMDC, ctypes.byref(src), 0, ctypes.byref(bf),
                               win32con.ULW_ALPHA)


def _quit_sequence():
    """退出：清理资源，通知两个线程的消息循环结束。"""
    try:
        if HHOOK:
            USER32.UnhookWindowsHookEx(HHOOK)
    except Exception:
        pass
    try:
        win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (HWND_HOST, 0))
    except Exception:
        pass
    try:
        win32gui.UnregisterHotKey(HWND_HOST, HOTKEY_QUIT)
    except Exception:
        pass
    STOP_EVENT.set()
    # 结束 hook 线程的消息循环
    if HOOK_THREAD_ID:
        try:
            USER32.PostThreadMessageW(HOOK_THREAD_ID, win32con.WM_QUIT, 0, 0)
        except Exception:
            pass
    # 结束 UI 线程的消息循环（若本函数运行在 UI 线程）
    try:
        win32gui.PostQuitMessage(0)
    except Exception:
        pass


def wheel_wndproc(hwnd, msg, wparam, lparam):
    global _ORIGIN_X, _ORIGIN_Y
    if msg == MSG_SHOW:
        x, y = wparam & 0xFFFF, (wparam >> 16) & 0xFFFF
        # 与命中判定共用同一套钳制后的原点，保证“鼠标指哪、哪高亮”
        _ORIGIN_X, _ORIGIN_Y = wheel_origin(x, y)
        try:
            _wheel_update(-1)
        except Exception as e:
            log_error(f"[错误] 轮盘渲染异常：{e}")
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
        return 0
    if msg == MSG_SELECT:
        try:
            _wheel_update(wparam)
        except Exception as e:
            log_error(f"[错误] 轮盘渲染异常：{e}")
        return 0
    if msg == MSG_CANCEL:
        try:
            _wheel_update(CANCEL_FRAME)
        except Exception as e:
            log_error(f"[错误] 轮盘渲染异常：{e}")
        return 0
    if msg == MSG_HIDE:
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


def _open_text_file_async(path):
    """用记事本异步打开文本文件（不阻塞 UI 线程）。"""
    def worker():
        try:
            subprocess.Popen(["notepad.exe", path])
        except Exception as e:
            log_error(f"[错误] 打开文件失败 {path!r}: {e}")
    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------- 排除程序
# 某些软件（思维导图、绘图、CAD 等）的右键是"拖拽画布/平移视图"，绝不能吞掉。
# 判定只在 RBUTTONDOWN 做一次：命中就整条手势放行，DOWN/MOVE/UP 全不进状态机。
_PID_NAME_CACHE = {}       # pid -> 进程名；避免每次右键都开进程句柄
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MB_OK, MB_YESNO = 0x0, 0x4
MB_ICONQUESTION, MB_ICONINFORMATION, MB_TOPMOST = 0x20, 0x40, 0x40000
IDYES = 6


def _process_name_from_pid(pid):
    """取进程可执行文件名（如 XMind.exe）。失败返回空串，绝不影响主流程。"""
    if not pid:
        return ""
    h = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = w.DWORD(1024)
        if KERNEL32.QueryFullProcessImageNameW(
                h, 0, ctypes.cast(buf, w.LPWSTR), ctypes.byref(size)):
            return os.path.basename(buf.value)
    except Exception:
        pass
    finally:
        KERNEL32.CloseHandle(h)
    return ""


def window_info(hwnd):
    """返回 (进程名, 顶层窗口标题)。进程名按 pid 缓存，标题每次现取。"""
    if not hwnd:
        return ("", "")
    try:
        root = USER32.GetAncestor(hwnd, GA_ROOT) or hwnd
        pid = w.DWORD(0)
        USER32.GetWindowThreadProcessId(root, ctypes.byref(pid))
        proc = _PID_NAME_CACHE.get(pid.value)
        if proc is None:
            proc = _process_name_from_pid(pid.value)
            if len(_PID_NAME_CACHE) > 512:
                _PID_NAME_CACHE.clear()
            _PID_NAME_CACHE[pid.value] = proc
        tbuf = ctypes.create_unicode_buffer(256)
        USER32.GetWindowTextW(root, ctypes.cast(tbuf, w.LPWSTR), 256)
        return (proc or "", tbuf.value or "")
    except Exception:
        return ("", "")


def is_excluded(proc, title, cfg=None):
    """匹配规则：含 * / ? 走通配符；否则按"包含"匹配，不区分大小写。"""
    cfg = cfg or CONFIG
    p = (proc or "").lower()
    t = (title or "").lower()
    for pat in cfg.get("exclude_processes", []):
        pat = str(pat).strip().lower()
        if not pat:
            continue
        if ("*" in pat or "?" in pat):
            if fnmatch.fnmatchcase(p, pat):
                return True
        elif pat in p:
            return True
    for pat in cfg.get("exclude_titles", []):
        pat = str(pat).strip().lower()
        if not pat:
            continue
        if ("*" in pat or "?" in pat):
            if fnmatch.fnmatchcase(t, pat):
                return True
        elif pat in t:
            return True
    return False


def exclusion_hit(x, y, cfg=None):
    """鼠标位置的窗口（+兜底前台窗口）是否命中排除列表。命中返回 (进程名, 标题)。"""
    cfg = cfg or CONFIG
    pairs = []
    under = window_info(USER32.WindowFromPoint(POINT(int(x), int(y))))
    if under[0]:
        pairs.append(under)
    fg = window_info(USER32.GetForegroundWindow())
    if fg[0] and fg != under:
        pairs.append(fg)
    for proc, title in pairs:
        if is_excluded(proc, title, cfg):
            return (proc, title)
    return None


def add_exclude_process(name):
    """把进程名写回 folders.json（保留原有文件夹等配置），返回是否新增。"""
    try:
        data = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        lst = data.get("exclude_processes")
        if not isinstance(lst, list):
            lst = list(CONFIG.get("exclude_processes", []))
        if name.lower() in [str(v).lower() for v in lst]:
            return False
        lst.append(name)
        data["exclude_processes"] = lst
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log_error(f"[错误] 写入排除列表失败：{e}")
        return False


def show_excluded_list():
    procs = CONFIG.get("exclude_processes", [])
    titles = CONFIG.get("exclude_titles", [])
    text = ("当前排除的程序（右键完全放行）：\n" +
            ("\n".join("  · " + p for p in procs) if procs else "  （空）") +
            "\n\n窗口标题关键字：\n" +
            ("\n".join("  · " + p for p in titles) if titles else "  （空）") +
            "\n\n托盘菜单「识别并添加排除程序」可自动添加；\n也可直接编辑 folders.json。")
    USER32.MessageBoxW(0, text, "右键轮盘 · 排除列表",
                       MB_OK | MB_ICONINFORMATION | MB_TOPMOST)


def probe_excluded_app():
    """托盘入口：后台采样 5 秒，识别鼠标停留的软件并询问是否加入排除列表。"""
    def worker():
        samples = []
        end = time.time() + 5.0
        while time.time() < end and not STOP_EVENT.is_set():
            pt = POINT()
            USER32.GetCursorPos(ctypes.byref(pt))
            info = window_info(USER32.WindowFromPoint(POINT(pt.x, pt.y)))
            if info[0]:
                samples.append((time.time(), info[0], info[1]))
            time.sleep(0.12)
        # 只取最后 2.5 秒：给用户时间把鼠标从托盘移到目标软件窗口
        recent = [s for s in samples if s[0] >= end - 2.5] or samples
        if not recent:
            USER32.MessageBoxW(0, "没能识别到窗口，请重试并把鼠标停在目标软件上。",
                               "添加排除程序", MB_OK | MB_TOPMOST)
            return
        (proc, title), _ = Counter((p, t) for _, p, t in recent).most_common(1)[0]
        warn = ""
        if proc.lower() == "explorer.exe" and "program manager" in title.lower():
            warn = "\n⚠ 这是 Windows 桌面，加入后桌面右键也不再触发轮盘。\n"
        text = (f"识别到的程序：{proc}\n窗口标题：{title[:60] or '（无标题）'}\n"
                f"{warn}\n是否加入排除列表？\n"
                "加入后该程序内右键完全放行（可正常拖画布），不再弹出轮盘。")
        ans = USER32.MessageBoxW(0, text, "添加排除程序",
                                 MB_YESNO | MB_ICONQUESTION | MB_TOPMOST)
        if ans == IDYES:
            added = add_exclude_process(proc)
            if HWND_HOST:
                win32gui.PostMessage(HWND_HOST, MSG_RELOAD, 0, 0)
            tip = (f"已加入排除列表：{proc}\n立即生效。" if added
                   else f"{proc} 已在排除列表中，无需重复添加。")
            USER32.MessageBoxW(0, tip, "添加排除程序",
                               MB_OK | MB_ICONINFORMATION | MB_TOPMOST)
    threading.Thread(target=worker, daemon=True).start()


def host_wndproc(hwnd, msg, wparam, lparam):
    if msg == TRAY_MSG and lparam == win32con.WM_RBUTTONUP:
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, 1, "设置 · 打开文件夹配置")
        win32gui.AppendMenu(menu, win32con.MF_STRING, 2, "重新加载配置")
        win32gui.AppendMenu(menu, win32con.MF_STRING, 3, "查看运行日志")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, 5, "识别并添加排除程序（5秒）")
        win32gui.AppendMenu(menu, win32con.MF_STRING, 6, "查看排除列表")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, 4, "退出")
        x, y = win32gui.GetCursorPos()
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        cmd = win32gui.TrackPopupMenu(menu, win32con.TPM_RETURNCMD, x, y, 0, hwnd, None)
        win32gui.DestroyMenu(menu)
        if cmd == 1:
            _open_text_file_async(CONFIG_PATH)
        elif cmd == 2:
            load_config()
            log("[信息] 配置已重新加载。")
        elif cmd == 3:
            _open_text_file_async(LOG_FILE)
        elif cmd == 5:
            probe_excluded_app()
        elif cmd == 6:
            threading.Thread(target=show_excluded_list, daemon=True).start()
        elif cmd == 4:
            _quit_sequence()
        return 0
    if msg == win32con.WM_HOTKEY and wparam == HOTKEY_QUIT:
        _quit_sequence()
        return 0
    if msg == MSG_RELOAD:
        load_config()
        log(f"[信息] 配置已重新加载；排除程序 {len(CONFIG.get('exclude_processes', []))} 项。")
        return 0
    if msg == MSG_REPLAY:
        # 纯单击重放：独立线程执行，绝不占住钩子回调或 UI 消息循环
        threading.Thread(target=_replay_right_click_worker, daemon=True).start()
        return 0
    if msg == MSG_OPEN:
        folders = CONFIG.get("folders", [])
        if 0 <= wparam < len(folders):
            name = folders[wparam].get("name", "")
            log(f"[动作] 打开扇区 {wparam} · {name}")
            # 异步打开，避免 Shell 调用阻塞 UI 消息循环
            threading.Thread(target=open_target, args=(folders[wparam]["path"],),
                             daemon=True).start()
        return 0
    if msg == win32con.WM_DESTROY:
        _quit_sequence()
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


class InputState:
    def __init__(self):
        self.pressed = False
        self.start_x = 0
        self.start_y = 0
        self.active = False
        self.cancelled = False
        self.outside = False   # 鼠标已甩出有效范围 → 本轮作废（取消手势）
        self.cur_idx = -1
        self.settled = False   # 本轮是否已结算，防止重复打开


def _hook_next(nCode, wParam, lParam):
    return USER32.CallNextHookEx(HHOOK, nCode, wParam, lParam)


def _replay_right_click_worker():
    """在独立工作线程里重放右键单击，让系统弹出原生右键菜单。
    合成事件经过本钩子时由 _REPLAYING 标志跳过处理，直接放行。
    关键：绝不能在钩子回调线程里同步执行——mouse_event 会触发事件重入，
    把钩子回调堵住，导致整个系统鼠标输入卡死。"""
    global _REPLAYING
    _REPLAYING = True
    try:
        # 第 5 个参数 dwExtraInfo=SYNTH_MARK，供钩子识别合成事件
        win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, SYNTH_MARK)
        win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, SYNTH_MARK)
    except Exception as e:
        log_error(f"[错误] 重放右键失败：{e}")
    finally:
        _REPLAYING = False


def _replay_right_click():
    """异步请求重放右键：投递到 UI 线程消息循环，不在钩子回调里做事。"""
    try:
        win32gui.PostMessage(HWND_HOST, MSG_REPLAY, 0, 0)
    except Exception as e:
        log_error(f"[错误] 投递重放请求失败：{e}")


def _settle_drag(cancelled):
    """结算一次拖拽：隐藏轮盘；仅当未取消且有效选中时才打开文件夹。

    cancelled=True 表示用户把鼠标甩出了有效范围（或按了 Esc）——这是"取消
    手势"，本轮作废：只隐藏轮盘，不打开任何文件夹，也不弹原生菜单。
    """
    if STATE.settled:
        return
    was_active, idx = STATE.active, STATE.cur_idx
    if not was_active:
        return
    STATE.settled = True
    win32gui.PostMessage(HWND_WHEEL, MSG_HIDE, 0, 0)
    folders = CONFIG.get("folders", [])
    if cancelled:
        log("[取消] 鼠标甩出有效范围或按 Esc，本轮作废，不打开任何文件夹")
    elif 0 <= idx < len(folders):
        log(f"[结算] 拖拽松手 -> 扇区 {idx} · {folders[idx].get('name', '')}")
        win32gui.PostMessage(HWND_HOST, MSG_OPEN, idx, 0)


def _reset_input_state():
    """一次右键交互彻底结束后复位状态机。"""
    STATE.pressed = False
    STATE.active = False
    STATE.cancelled = False
    STATE.outside = False
    STATE.cur_idx = -1
    STATE.settled = False


def mouse_hook_proc(nCode, wParam, lParam):
    try:
        if nCode >= 0:
            try:
                ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                x, y = ms.pt.x, ms.pt.y
                extra = ms.dwExtraInfo
            except Exception:
                x = y = extra = 0
            # 自己合成的右键事件（带标记）：无条件放行，不进状态机。
            # 这是防止"合成事件递归触发 replay"的关键，绝不能依赖时序标志。
            if extra == SYNTH_MARK or _REPLAYING:
                return _hook_next(nCode, wParam, lParam)

            if wParam == win32con.WM_RBUTTONDOWN:
                # 排除程序：整条手势放行。DOWN 不吞、状态机不介入，
                # 思维导图/绘图软件自己的"右键拖画布"就能原样工作。
                # 只在 DOWN 判一次（每次右键一次窗口查询，带 pid 缓存），
                # 后续 MOVE/UP 因 STATE.pressed=False 自动全部放行。
                hit = None
                try:
                    hit = exclusion_hit(x, y)
                except Exception as e:
                    log_error(f"[警告] 排除程序检测失败，按正常流程处理：{e}")
                if hit:
                    _reset_input_state()
                    log(f"[排除] 放行右键 · 程序={hit[0]} · 窗口={hit[1][:60]}")
                    return _hook_next(nCode, wParam, lParam)
                if not STATE.pressed:
                    STATE.pressed = True
                    STATE.active = False
                    STATE.cancelled = False
                    STATE.outside = False
                    STATE.cur_idx = -1
                    STATE.settled = False
                    STATE.start_x, STATE.start_y = x, y
                # 吞掉 DOWN：系统不知有右键，从源头阻止 WM_CONTEXTMENU
                return 1

            if wParam == win32con.WM_MOUSEMOVE and STATE.pressed:
                try:
                    if USER32.GetAsyncKeyState(win32con.VK_ESCAPE) & 0x8000:
                        STATE.cancelled = True
                        if STATE.active:
                            win32gui.PostMessage(HWND_WHEEL, MSG_HIDE, 0, 0)
                except Exception:
                    pass

                # ⚠️ 绝不能用 GetAsyncKeyState(VK_RBUTTON) 判断右键是否松开！
                # 因为 RBUTTONDOWN 被吞掉（return 1，从源头阻止原生菜单），系统
                # 从未把右键记为"按下"，异步按键状态读到的永远是"未按下"。曾据此
                # 做"物理状态兜底"，导致轮盘刚显示就被误判松手而"一闪而过"。
                # 松手只认 WM_RBUTTONUP 事件，不要再把物理状态检测加回来。
                if STATE.pressed and not STATE.settled:
                    if not STATE.active and not STATE.cancelled:
                        if math.hypot(x - STATE.start_x, y - STATE.start_y) >= CONFIG["trigger_distance"]:
                            STATE.active = True
                            packed = (int(STATE.start_x) & 0xFFFF) | ((int(STATE.start_y) & 0xFFFF) << 16)
                            win32gui.PostMessage(HWND_WHEEL, MSG_SHOW, packed, 0)
                    if STATE.active and not STATE.cancelled:
                        # 用钳制后的轮盘真实圆心判定，绝不用原始按下点：
                        # 屏幕边缘处轮盘会被推入屏内，用按下点会整体错位一格。
                        wcx, wcy = wheel_center(STATE.start_x, STATE.start_y)
                        if not STATE.outside and is_outside(wcx, wcy, x, y, CONFIG):
                            # 甩出有效范围 = 取消手势：本轮作废，清掉高亮并给提示。
                            # 一旦进入取消区，本轮不再恢复（移回范围内也无效），
                            # 避免"甩出去又甩回来"意外打开文件夹。
                            STATE.outside = True
                            STATE.cur_idx = -1
                            win32gui.PostMessage(HWND_WHEEL, MSG_CANCEL, 0, 0)
                        elif not STATE.outside:
                            idx = cell_from_point(wcx, wcy, x, y,
                                                  CONFIG["inner_radius"], CONFIG["outer_radius"])
                            # 粘滞高亮：中心死区保留最后一次有效选中
                            if idx >= 0 and idx != STATE.cur_idx:
                                STATE.cur_idx = idx
                                win32gui.PostMessage(HWND_WHEEL, MSG_SELECT, idx, 0)
                # MOUSEMOVE 放行，保证光标正常移动
                return _hook_next(nCode, wParam, lParam)

            if wParam == win32con.WM_RBUTTONUP and STATE.pressed:
                was_active = STATE.active
                moved = (math.hypot(x - STATE.start_x, y - STATE.start_y)
                         >= CONFIG["trigger_distance"])
                # 松手坐标也要判越界：快速甩出时中途 MOUSEMOVE 可能被系统合并/丢弃，
                # UP 是唯一一定能拿到的位置，不能只靠移动过程里的判定。
                wcx, wcy = wheel_center(STATE.start_x, STATE.start_y)
                if not STATE.outside and (was_active or moved) \
                        and is_outside(wcx, wcy, x, y, CONFIG):
                    STATE.outside = True
                    STATE.cur_idx = -1
                cancelled = STATE.cancelled or STATE.outside
                # 拖拽激活兜底：中途 MOVE 全丢时，只要位移够就认作一次拖拽手势。
                # 注意越界(moved+cancelled)也必须置 active，否则会掉进下面的
                # "纯单击 replay" 分支而弹出原生菜单，且本轮作废没被记录。
                # 越界时不再计算命中格；未越界才用 UP 坐标补算。
                if not was_active and moved:
                    if not cancelled:
                        idx = cell_from_point(wcx, wcy, x, y,
                                              CONFIG["inner_radius"], CONFIG["outer_radius"])
                        if idx >= 0:
                            STATE.cur_idx = idx
                    was_active = True
                    STATE.active = True
                if was_active and not STATE.settled:
                    # 拖拽：结算。cancelled=True 时只隐藏轮盘、不打开任何文件夹，
                    # 并且吞掉 UP 不弹原生菜单（这是"取消手势"而不是单击）。
                    _settle_drag(cancelled)
                elif not was_active and not STATE.settled:
                    # 纯单击：投递异步重放（绝不在钩子回调里同步发合成事件）
                    _replay_right_click()
                _reset_input_state()
                return 1   # 吞掉原始 UP，不弹菜单
    except Exception as e:
        log_error(f"[错误] 鼠标钩子异常：{e}")
    return _hook_next(nCode, wParam, lParam)


# ---------------------------------------------------------------- 线程
def hook_thread_main():
    """钩子线程：只安装钩子 + 泵消息。绝不在此做耗时操作。"""
    global HHOOK, STATE, _HOOK_CALLBACK_REF, HOOK_THREAD_ID
    HOOK_THREAD_ID = threading.get_ident()
    STATE = InputState()
    pythoncom.CoInitialize()
    hinst = win32api.GetModuleHandle(None)
    _HOOK_CALLBACK_REF = HOOKPROC(mouse_hook_proc)
    HHOOK = USER32.SetWindowsHookExW(win32con.WH_MOUSE_LL, _HOOK_CALLBACK_REF, hinst, 0)
    if not HHOOK:
        log_error("[错误] 安装鼠标钩子失败！")
        STOP_EVENT.set()
        return
    log("[就绪] 后台运行中：右键拖动打开轮盘；托盘右键或 Ctrl+Alt+Shift+Q 退出。")
    pythoncom.PumpMessages()


def ui_thread_main():
    """UI 线程：创建窗口 + 泵消息，处理显示/高亮/打开。"""
    global HWND_HOST, HWND_WHEEL, UI_THREAD_ID
    UI_THREAD_ID = threading.get_ident()
    pythoncom.CoInitialize()
    hinst = win32api.GetModuleHandle(None)

    wch = win32gui.WNDCLASS()
    wch.hInstance = hinst
    wch.lpszClassName = "QuickerRadialHost"
    wch.lpfnWndProc = host_wndproc
    try:
        win32gui.RegisterClass(wch)
    except win32gui.error:
        pass
    HWND_HOST = win32gui.CreateWindow(wch.lpszClassName, "RadialHost", 0,
                                      0, 0, 0, 0, 0, 0, hinst, None)

    size = wheel_size(CONFIG)
    _init_dib(size)
    # 预热 25 帧（约 1.4 秒，仅启动一次）：之后拖动切换高亮零渲染开销，
    # 避免首次弹出或快速拖动时因 LANCZOS 缩小造成卡顿。
    try:
        for s in [CANCEL_FRAME] + list(range(-1, CELL_COUNT)):
            get_wheel_frame(s)
        log("[信息] 轮盘帧缓存预热完成（26 帧，含越界取消态）。")
    except Exception as e:
        log(f"[警告] 帧缓存预热失败（首次显示会略慢）：{e}")
    wcw = win32gui.WNDCLASS()
    wcw.hInstance = hinst
    wcw.lpszClassName = "QuickerRadialWheel"
    wcw.lpfnWndProc = wheel_wndproc
    try:
        win32gui.RegisterClass(wcw)
    except win32gui.error:
        pass
    HWND_WHEEL = win32gui.CreateWindowEx(
        win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT |
        win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW,
        wcw.lpszClassName, "", win32con.WS_POPUP,
        0, 0, size, size, 0, 0, hinst, None)
    win32gui.ShowWindow(HWND_WHEEL, win32con.SW_HIDE)

    # 托盘（用自定义图标）
    try:
        hicon = load_app_icon(32) or win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        # 给 host 窗口也设置图标（Alt+Tab / 任务管理器等处显示）
        try:
            win32gui.SendMessage(HWND_HOST, win32con.WM_SETICON, 1, hicon)  # ICON_BIG
            win32gui.SendMessage(HWND_HOST, win32con.WM_SETICON, 0, hicon)  # ICON_SMALL
        except Exception:
            pass
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (
            HWND_HOST, 0,
            win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
            TRAY_MSG, hicon, "右键轮盘菜单 · 右键托盘：设置 / 退出"
        ))
        log("[信息] 自定义托盘图标已加载。")
    except Exception as e:
        log(f"[警告] 托盘图标创建失败：{e}")

    try:
        win32gui.RegisterHotKey(HWND_HOST, HOTKEY_QUIT,
                                win32con.MOD_CONTROL | win32con.MOD_ALT | win32con.MOD_SHIFT,
                                ord("Q"))
    except Exception as e:
        log(f"[警告] 注册退出热键失败（可用托盘退出）：{e}")

    WINDOWS_READY.set()
    pythoncom.PumpMessages()


# ---------------------------------------------------------------- 自检 / 预览
def run_selftest():
    ok = True
    def check(desc, got, expect):
        nonlocal ok
        good = got == expect
        ok = ok and good
        print(f"  {'PASS' if good else 'FAIL'}  {desc}: got={got}, expect={expect}")
    print("== 3 层 24 格自检（中心 0,0，死区 48，三环 48~168）==")
    # 每层取一个半径中点：内环 68、中环 108、外环 148
    # 对角点必须用 cos/sin 保持半径 r（直接用 (r,r) 会变成 r√2 落到外环）
    for layer, r in enumerate((68, 108, 148)):
        d = r * math.sqrt(0.5)
        base = layer * 8
        cases = [
            ("正上", 0, -r, 0), ("右上", d, -d, 1), ("正右", r, 0, 2),
            ("右下", d, d, 3), ("正下", 0, r, 4), ("左下", -d, d, 5),
            ("正左", -r, 0, 6), ("左上", -d, -d, 7),
        ]
        for name, x, y, sec in cases:
            check(f"{['内','中','外'][layer]}环·{name}",
                  cell_from_point(0, 0, x, y, 48, 168), base + sec)
    check("中心死区", cell_from_point(0, 0, 10, 10, 48, 168), -1)
    check("超出外环归外环", cell_from_point(0, 0, 300, 0, 48, 168), 2 * 8 + 2)
    # 回归用例：屏幕边缘按下时轮盘被钳制，命中圆心必须跟着钳制后的原点走
    ox, oy = wheel_origin(4, 4)
    wcx, wcy = wheel_center(4, 4)
    half = wheel_size() / 2.0
    check("钳制圆心=原点+半宽", (round(wcx), round(wcy)),
          (round(ox + half), round(oy + half)))
    check("边缘钳制命中正右外环",
          cell_from_point(wcx, wcy, wcx + 148, wcy, 48, 168), 2 * 8 + 2)
    check("边缘钳制命中正上内环",
          cell_from_point(wcx, wcy, wcx, wcy - 68, 48, 168), 0 * 8 + 0)
    # 越界取消区：cancel_radius = outer_radius + cancel_margin
    _t = {"outer_radius": 200, "cancel_margin": 80}
    check("取消半径=外缘+余量", cancel_radius(_t), 280)
    check("范围内不算越界", is_outside(0, 0, 279, 0, _t), False)
    check("边界上不算越界", is_outside(0, 0, 280, 0, _t), False)
    check("超出边界算越界", is_outside(0, 0, 281, 0, _t), True)
    check("斜向远点算越界", is_outside(0, 0, 300, 300, _t), True)
    check("中心死区不算越界", is_outside(0, 0, 10, 10, _t), False)
    # 排除程序匹配规则
    _e = {"exclude_processes": ["xmind", "*mind*.exe", "notepad.exe"],
          "exclude_titles": ["思维导图"]}
    check("排除·进程名关键字包含匹配", is_excluded("XMind.exe", "", _e), True)
    check("排除·大小写不敏感", is_excluded("XMIND.EXE", "", _e), True)
    check("排除·通配符匹配", is_excluded("FreeMind.exe", "", _e), True)
    check("排除·精确进程名", is_excluded("notepad.exe", "", _e), True)
    check("排除·窗口标题关键字",
          is_excluded("app.exe", "某某思维导图 - 文档", _e), True)
    check("排除·未命中不排除",
          is_excluded("explorer.exe", "Program Manager", _e), False)
    check("排除·空列表不排除",
          is_excluded("xmind.exe", "XMind",
                      {"exclude_processes": [], "exclude_titles": []}), False)
    # 真实窗口识别：空句柄必须安全返回；再打印一次真实前台窗口供人工核对
    check("空窗口句柄安全返回", window_info(0), ("", ""))
    _wi = window_info(USER32.GetForegroundWindow())
    print(f"  当前前台窗口: 进程={_wi[0] or '(未知)'} 标题={_wi[1][:40] or '(无)'}")
    load_config()
    print(f"  文件夹数: {len(CONFIG['folders'])}")
    print(f"  排除程序: {CONFIG.get('exclude_processes', [])}")
    print(f"  取消半径: {cancel_radius()} px (外缘 {CONFIG['outer_radius']} + 余量 {CONFIG['cancel_margin']})")
    print("自检结果:", "全部通过 OK" if ok else "存在失败 FAIL")
    return 0 if ok else 1


def run_preview():
    load_config()
    out = os.path.join(BASE_DIR, "wheel_preview.png")
    # 展示：无高亮、内环、中环、外环各一格高亮
    imgs = [render_wheel(s) for s in (-1, 2, 10, 19)]
    w, h = imgs[0].size
    sheet = Image.new("RGBA", (w * 4, h), (245, 246, 250, 255))
    for i, im in enumerate(imgs):
        sheet.alpha_composite(im, (i * w, 0))
    sheet.convert("RGB").save(out)
    print(f"[预览] 已生成：{out}")
    return 0


def set_dpi_aware():
    """声明 Per-Monitor DPI Aware V2：任何屏幕缩放下分层窗口都按物理像素
    1:1 显示，不会被系统位图拉伸（拉伸是"模糊"的常见元凶）。
    失败时逐级回退到 System Aware / 基础 DPI Aware。"""
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # system
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    log("=" * 46)
    log("[启动] 右键轮盘菜单程序启动")
    set_dpi_aware()
    load_config()
    ui_t = threading.Thread(target=ui_thread_main, daemon=True)
    ui_t.start()
    WINDOWS_READY.wait(10)     # 等窗口和 DIB 就绪，钩子才能安全 PostMessage
    hook_t = threading.Thread(target=hook_thread_main, daemon=True)
    hook_t.start()
    STOP_EVENT.wait()          # 主线程阻塞等待退出信号
    log("[退出] 程序已结束。")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    if "--preview" in sys.argv:
        sys.exit(run_preview())
    main()
