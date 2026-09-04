#!/usr/bin/env python3
"""CutKit 拖照片演示 — 复刻"上传一张照片"的教程演示片.

流程: 粉色标题药丸 + 虚线上传框(带 +) → 照片从左下飞入、落框时放大回弹 →
定格 → 叠加 Fotor 彩色转场(进度环 → AI Generated 徽章, 自带压暗蒙版) →
可选把结果图交叉淡入。输出 1080x1920 / 30fps。

几何与时间轴按参考片逐帧实测得到；Fotor 转场是随包的 VP9-alpha webm
(assets/fotor-loading.webm，由原始 113MB ProRes/qtrle 无损压到 ~180KB)。
"""
import math
import os
import re
import subprocess
import sys
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from render import (ffmpeg_exe, _font, _smooth, _has_cjk,
                    Cancelled, check_cancel, track_proc, abort_proc,
                    bail_if_cancelled)

W, H = 1080, 1920
FPS = 30

# ---------- 几何（1080x1920 下实测） ----------
# 标题竖直位置：以「中心 y 占画面高的比例」表示，方便可视化调。
# 0.185 是照参考片实测的（1920 画布上约 356px）；原来的 0.109 明显偏高。
CAPTION_Y = 0.185
PILL_H = 141                       # 药丸高度，位置由 CAPTION_Y 推出来
PILL_R = 24
PILL_FILL = (210, 30, 247)

# 可选字体。空路径 = 交给 render._font 按文字内容自动挑（中文走黑体，西文走 HelveticaNeue）。
FONTS = [
    ("system",   "系统", ""),
    ("helvetica", "Helvetica Neue", "/System/Library/Fonts/HelveticaNeue.ttc"),
    ("avenir",   "Avenir Next", "/System/Library/Fonts/Avenir Next.ttc"),
    ("futura",   "Futura", "/System/Library/Fonts/Supplemental/Futura.ttc"),
    ("impact",   "Impact", "/System/Library/Fonts/Supplemental/Impact.ttf"),
    ("arialblack", "Arial Black", "/System/Library/Fonts/Supplemental/Arial Black.ttf"),
    ("georgia",  "Georgia", "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"),
    ("times",    "Times", "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf"),
    ("chalk",    "Chalkboard", "/System/Library/Fonts/Supplemental/Chalkboard.ttc"),
    ("heiti",    "黑体", "/System/Library/Fonts/STHeiti Medium.ttc"),
]
FONT_KEYS = {k for k, _, _ in FONTS}
_FONT_PATHS = {k: p for k, _, p in FONTS}

# 速度预设。数值是成片总时长（秒）——比"倍数"直观，
# 1.9 是参考片 进度条-默认速度.mp4 的实测长度。
SPEED_PRESETS = [
    ("default", "默认速度（1.9 秒）", 1.9),
    ("fast",    "更快（1.4 秒）", 1.4),
    ("normal",  "原速（5.0 秒）", 5.0),
    ("slow",    "放慢（2.6 秒）", 2.6),
]


def caption_font(size, text, key="system"):
    """按选择拿字体；选了具体字体但文本含中文而该字体没有中文字形时，退回自动选择。"""
    path = _FONT_PATHS.get(key or "system", "")
    if path and os.path.exists(path):
        try:
            f = ImageFont.truetype(path, size)
            if not _has_cjk(text) or f.getmask("变").getbbox():
                return f
        except Exception:
            pass
    return _font(size, "bold", text)
PILL_PAD = 58                      # 文字左右内边距
ZONE = (139, 402, 936, 1272)       # 虚线上传框
ZONE_R, DASH_ON, DASH_OFF, DASH_W = 64, 24, 14, 7
DASH_COLOR = (205, 205, 205)
PLUS_SIZE, PLUS_W, PLUS_COLOR = 186, 16, (112, 112, 112)
PHOTO_BOX = (133, 389, 943, 1275)  # 照片落定位置
PHOTO_R = 64

# ---------- 时间轴（秒） ----------
T_IN = 0.22          # 照片开始飞入
T_LAND = 0.72        # 落到框里（此刻最大）
T_SETTLE = 1.30      # 回弹结束
T_TRANS = 1.45       # Fotor 转场开始
TAIL = 0.35          # 转场结束后留白
PEAK = 1.21          # 落地瞬间放大倍数
START_SCALE = 0.55
START_CENTER = (-190, 1090)
REVEAL_AT = 0.62     # 结果图在转场进度的百分之多少处淡入


def asset_path(name):
    """打包后资源在 sys._MEIPASS 下，开发时在源码目录旁。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, "assets", name)
    if os.path.exists(p):
        return p
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name)


def probe_duration(path):
    r = subprocess.run([ffmpeg_exe(), "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr)
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


# ---------- 静态层 ----------
def _rounded(img, radius):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.size[0] - 1, img.size[1] - 1),
                                           radius, fill=255)
    out = img.copy()
    out.putalpha(mask)
    return out


def _dashed_round_rect(d, box, radius, on, off, width, color):
    """沿圆角矩形周长画虚线（直边打点 + 四角圆弧分段）。"""
    x0, y0, x1, y1 = box
    r = radius
    # 四条直边
    segs = [((x0 + r, y0), (x1 - r, y0), "h"), ((x0 + r, y1), (x1 - r, y1), "h"),
            ((x0, y0 + r), (x0, y1 - r), "v"), ((x1, y0 + r), (x1, y1 - r), "v")]
    for (ax, ay), (bx, by), kind in segs:
        length = (bx - ax) if kind == "h" else (by - ay)
        pos = 0
        while pos < length:
            e = min(pos + on, length)
            if kind == "h":
                d.line((ax + pos, ay, ax + e, ay), fill=color, width=width)
            else:
                d.line((ax, ay + pos, ax, ay + e), fill=color, width=width)
            pos = e + off
    # 四角圆弧
    step = (on + off) / max(r, 1) * 57.2958 / 2   # 角度步长
    for cx, cy, a0 in ((x0 + r, y0 + r, 180), (x1 - r, y0 + r, 270),
                       (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90)):
        a = a0
        while a < a0 + 90:
            d.arc((cx - r, cy - r, cx + r, cy + r), a,
                  min(a + step * on / (on + off), a0 + 90), fill=color, width=width)
            a += step


# 标题样式。box: pill=胶囊 / rect=方角块 / None=只有字。
# stroke 是描边宽度，shadow 是投影偏移。
# 演示片的底是纯黑，所以黑底色块 / 黑描边 / 投影在这里都看不出差别 ——
# 只保留在黑底上真正有区分度的几种。
CAPTION_STYLES = [
    ("pill_pink",    "粉色药丸",   dict(box="pill", bg=(210, 30, 247), fg=(255, 255, 255))),
    ("pill_white",   "白色药丸",   dict(box="pill", bg=(255, 255, 255), fg=(20, 20, 22))),
    ("pill_yellow",  "黄色药丸",   dict(box="pill", bg=(255, 214, 10),  fg=(20, 20, 22))),
    ("pill_cyan",    "青色药丸",   dict(box="pill", bg=(46, 214, 214),  fg=(12, 32, 34))),
    ("rect_white",   "白色方块",   dict(box="rect", bg=(255, 255, 255), fg=(20, 20, 22))),
    ("plain",        "纯白大字",   dict(box=None, fg=(255, 255, 255))),
    ("outline_pink", "粉字白描边", dict(box=None, fg=(255, 92, 214), stroke=(255, 255, 255), sw=9)),
    ("outline_wp",   "白字粉描边", dict(box=None, fg=(255, 255, 255), stroke=(226, 42, 200), sw=9)),
]
CAPTION_STYLE_KEYS = {k for k, _, _ in CAPTION_STYLES}
_CAPTION_STYLE_MAP = {k: v for k, _, v in CAPTION_STYLES}


def build_pill(caption, style="pill_pink", y=CAPTION_Y, font_key="system"):
    """标题单独出一张透明图 —— 它要盖在转场蒙版之上，保持不被压暗。

    y 是标题中心占画面高的比例，界面上用滑杆调、旁边有预览。
    """
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if not caption:
        return im
    st = _CAPTION_STYLE_MAP.get(style) or _CAPTION_STYLE_MAP["pill_pink"]
    d = ImageDraw.Draw(im)
    f = caption_font(56, caption, font_key)
    tw = d.textlength(caption, font=f)
    cy = max(PILL_H / 2 + 8, min(H - PILL_H / 2 - 8, float(y) * H))
    PILL_Y0, PILL_Y1 = cy - PILL_H / 2, cy + PILL_H / 2

    box = st.get("box")
    if box:
        half = min(tw / 2 + PILL_PAD, W / 2 - 40)
        xy = (W / 2 - half, PILL_Y0, W / 2 + half, PILL_Y1)
        r = PILL_R if box == "pill" else 8
        d.rounded_rectangle(xy, r, fill=tuple(st["bg"]) + (255,))

    sh = st.get("shadow")
    if sh:
        d.text((W / 2 + sh[0], cy + sh[1]), caption, font=f,
               fill=(0, 0, 0, 150), anchor="mm")
    kw = {}
    if st.get("stroke"):
        kw = dict(stroke_width=st.get("sw", 8), stroke_fill=tuple(st["stroke"]) + (255,))
    d.text((W / 2, cy), caption, font=f,
           fill=tuple(st["fg"]) + (255,), anchor="mm", **kw)
    return im


def build_static():
    """黑底 + 虚线上传框 + 加号（标题药丸走单独一层）。"""
    im = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(im, "RGBA")
    _dashed_round_rect(d, ZONE, ZONE_R, DASH_ON, DASH_OFF, DASH_W, DASH_COLOR)
    cx, cy = (ZONE[0] + ZONE[2]) / 2, (ZONE[1] + ZONE[3]) / 2
    h = PLUS_SIZE / 2
    d.line((cx - h, cy, cx + h, cy), fill=PLUS_COLOR, width=PLUS_W)
    d.line((cx, cy - h, cx, cy + h), fill=PLUS_COLOR, width=PLUS_W)
    return im


def load_photo(path, box=PHOTO_BOX, scale=2):
    """按落定框的比例裁切照片，存 2 倍分辨率供逐帧缩放。"""
    bw, bh = box[2] - box[0], box[3] - box[1]
    img = ImageOps.exif_transpose(Image.open(path).convert("RGB"))
    img = ImageOps.fit(img, (bw * scale, bh * scale), method=Image.LANCZOS)
    return _rounded(img, PHOTO_R * scale)


# ---------- 缓动 ----------
def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _lerp(a, b, v):
    return a + (b - a) * v


def _ease_in_out_cubic(v):
    v = _clamp(v)
    return 4 * v ** 3 if v < 0.5 else 1 - ((-2 * v + 2) ** 3) / 2


def _ease_out_back(v, amount=1.28):
    v = _clamp(v)
    c3 = amount + 1
    return 1 + c3 * ((v - 1) ** 3) + amount * ((v - 1) ** 2)


# ---------- 自适应画框（任意比例的图都能放进来） ----------
FIT_MAX_W, FIT_MAX_H, FIT_CY = 860, 1040, 830


def target_box_for_aspect(aspect):
    aspect = max(0.35, min(2.4, aspect))
    if aspect >= FIT_MAX_W / FIT_MAX_H:
        tw = FIT_MAX_W
        th = round(tw / aspect)
    else:
        th = FIT_MAX_H
        tw = round(th * aspect)
    cx = W // 2
    return (round(cx - tw / 2), round(FIT_CY - th / 2),
            round(cx + tw / 2), round(FIT_CY + th / 2))


# ---------- 卡片 / 光标 ----------
def paste_card(base, source, x, y, w, aspect, angle, shadow_alpha=145,
               border=True):
    """把照片当成一张卡片贴上去：圆角 + 白边 + 投影 + 旋转。"""
    iw = max(2, round(w))
    ih = max(2, round(iw / max(0.35, min(2.4, aspect))))
    radius = max(12, round(iw * 0.035))
    card = ImageOps.fit(source, (iw, ih), method=Image.LANCZOS)
    card = _rounded(card.convert("RGB"), radius)

    if border:
        layer = Image.new("RGBA", (iw + 12, ih + 12), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            (2, 2, iw + 9, ih + 9), radius=radius + 4,
            outline=(255, 255, 255, 235), width=5)
        layer.alpha_composite(card, (6, 6))
    else:
        layer = card

    if shadow_alpha > 0:
        shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (5, 5, layer.width - 4, layer.height - 4), radius=radius + 4,
            fill=(0, 0, 0, shadow_alpha))
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(12, round(iw * 0.035))))
    else:
        shadow = None

    if abs(angle) > 0.01:
        layer = layer.rotate(angle, resample=Image.BICUBIC, expand=True)
        if shadow is not None:
            shadow = shadow.rotate(angle, resample=Image.BICUBIC, expand=True)

    px = round(x - (layer.width - (iw + (12 if border else 0))) / 2)
    py = round(y - (layer.height - (ih + (12 if border else 0))) / 2)
    if shadow is not None:
        base.alpha_composite(shadow, (px + 10, py + 22))
    base.alpha_composite(layer, (px, py))
    return iw, ih


def draw_cursor(base, x, y, pressed=0.0):
    """鼠标指针（按下时带一圈扩散环）。"""
    if pressed > 0:
        ring = Image.new("RGBA", base.size, (0, 0, 0, 0))
        r = round(42 + 18 * pressed)
        ImageDraw.Draw(ring).ellipse((x - r, y - r, x + r, y + r),
                                     outline=(255, 255, 255, round(125 * (1 - pressed))),
                                     width=5)
        base.alpha_composite(ring)
    s = 1.38
    pts = [(0, 0), (0, 45), (12, 34), (24, 61), (37, 55), (25, 30), (46, 29)]
    pts = [(round(x + px * s), round(y + py * s)) for px, py in pts]
    d = ImageDraw.Draw(base)
    sh = [(px + 6, py + 8) for px, py in pts]
    d.polygon(sh, fill=(0, 0, 0, 150))
    d.line(sh + [sh[0]], fill=(0, 0, 0, 150), width=8, joint="curve")
    d.polygon(pts, fill=(255, 255, 255, 255))
    d.line(pts + [pts[0]], fill=(24, 28, 38, 255), width=5, joint="curve")


# ---------- 运动 ----------
# drag 风格时间轴（秒）
D_APPEAR, D_DRAG0, D_SNAP, D_SETTLE = 0.42, 0.80, 2.15, 2.57
D_CURSOR_OFF = 2.48


def drag_motion(t, aspect, target):
    """光标从下方拖起卡片、走弧线拖进框、吸附回弹。返回 (x, y, 宽, 角度)。"""
    tw = max(260.0, (target[2] - target[0]) - 24.0)
    sw = min(560.0, max(300.0, tw * 0.66))
    sx = W / 2 - sw / 2 - 40
    sy = min(1450.0, target[3] + 260.0)
    tx, ty = target[0] + 12.0, target[1] + 12.0

    appear = _ease_out_back((t - D_APPEAR) / 0.38)
    drag = _ease_in_out_cubic((t - D_DRAG0) / (D_SNAP - D_DRAG0))
    settle = _ease_out_back((t - D_SNAP) / 0.42)

    if t < D_DRAG0:
        return sx, _lerp(1505, sy, appear), sw * _clamp(appear), _lerp(7.0, -4.0, _clamp(appear))
    if t < D_SNAP:
        curve = math.sin(math.pi * drag)
        return (_lerp(sx, tx, drag) - 72 * curve,
                _lerp(sy, ty, drag) - 35 * curve,
                _lerp(sw, tw, drag),
                _lerp(-4.0, 1.4, drag) - 1.6 * curve)
    return tx, ty, tw * _lerp(1.035, 1.0, _clamp(settle)), _lerp(1.1, 0.0, _clamp(settle))


def photo_state(t):
    """slide 风格：返回 (中心x, 中心y, 缩放) —— 未出现时返回 None。"""
    bx0, by0, bx1, by1 = PHOTO_BOX
    end_c = ((bx0 + bx1) / 2, (by0 + by1) / 2)
    if t < T_IN:
        return None
    if t < T_LAND:
        e = _smooth((t - T_IN) / (T_LAND - T_IN))
        return (START_CENTER[0] + (end_c[0] - START_CENTER[0]) * e,
                START_CENTER[1] + (end_c[1] - START_CENTER[1]) * e,
                START_SCALE + (PEAK - START_SCALE) * e)
    if t < T_SETTLE:
        e = _smooth((t - T_LAND) / (T_SETTLE - T_LAND))
        return (end_c[0], end_c[1], PEAK + (1.0 - PEAK) * e)
    return (end_c[0], end_c[1], 1.0)


# ---------- 拖拽音效（合成，无需素材） ----------
def create_audio(path, dur, drag0=D_DRAG0, snap=D_SNAP):
    sr = 48000
    n = max(1, round(dur * sr))
    t = np.arange(n, dtype=np.float64) / sr
    a = np.zeros(n)
    rng = np.random.default_rng(7)
    m = (t >= drag0) & (t <= snap)                 # 拖动的风声
    if m.any():
        p = (t[m] - drag0) / max(1e-6, snap - drag0)
        env = np.sin(np.pi * p) ** 1.4
        sm = np.convolve(rng.normal(0, 1, int(m.sum())), np.ones(55) / 55, mode="same")
        a[m] += 0.20 * env * sm + 0.018 * env * np.sin(2 * np.pi * (165 + 260 * p) * t[m])
    c = (t >= snap) & (t <= snap + 0.32)           # 吸附的咔哒
    if c.any():
        ct = t[c] - snap
        a[c] += 0.19 * np.exp(-24 * ct) * np.sin(2 * np.pi * (720 + 420 * ct) * ct)
        a[c] += 0.10 * np.exp(-12 * ct) * np.sin(2 * np.pi * 115 * ct)
    a = np.clip(a / max(1e-8, np.abs(a).max()) * 0.48, -1, 1)
    with wave.open(str(path), "wb") as wv:
        wv.setnchannels(1); wv.setsampwidth(2); wv.setframerate(sr)
        wv.writeframes((a * 32767).astype("<i2").tobytes())
    return path


# ---------- 转场帧流（顺序读取，恒定内存） ----------
class TransitionStream:
    """把带 alpha 的转场素材按需一帧帧读出来（RGBA），供 Pillow 合成。"""

    def __init__(self, path, w=W, h=H, fps=FPS, speed=1.0):
        self.w, self.h, self.size = w, h, w * h * 4
        self.eof = False
        # 整条时间轴压缩时，转场素材也得同比放快，否则只会播到一半就被截断
        vf = f"scale={w}:{h}"
        if abs(speed - 1.0) > 1e-6:
            vf += f",setpts=PTS/{speed:g}"
        vf += f",fps={fps}"
        self.proc = subprocess.Popen(
            [ffmpeg_exe(), "-v", "error", "-c:v", "libvpx-vp9", "-i", path,
             "-vf", vf, "-f", "rawvideo",
             "-pix_fmt", "rgba", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def next(self):
        if self.eof:
            return None
        buf = self.proc.stdout.read(self.size)
        if buf is None or len(buf) < self.size:
            self.eof = True
            self.close()
            return None
        return Image.frombytes("RGBA", (self.w, self.h), buf)

    def close(self):
        try:
            self.proc.stdout.close()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class DragDemo:
    """把一张照片做成"拖进上传框"的演示片。

    motion: "slide" 复刻参考片（左下飞入 + 落框回弹）; "drag" 光标拖拽（任意比例自适应）
    transparent: True 输出带 alpha 的 MOV，可直接盖在自己的视频上（剪映/CapCut）
    """

    def __init__(self, photo, out_path, caption="Upload Your Photo",
                 result=None, transition=None, tail=TAIL, tmp_dir=None,
                 motion="drag", transparent=False, aspect_fit=None,
                 cursor=None, sound=True, glow=True, use_transition=True,
                 caption_style="plain", caption_y=CAPTION_Y,
                 caption_font_key="system", duration=None,
                 progress=None, log=None):
        self.tmp_dir = tmp_dir or os.path.join(
            os.path.expanduser("~/Library/Caches"), "CutKit")
        self.photo_path = photo
        self.out_path = out_path
        self.caption = caption or ""
        self.caption_style = (caption_style if caption_style in CAPTION_STYLE_KEYS
                              else "plain")
        self.caption_y = float(caption_y)
        self.caption_font_key = (caption_font_key if caption_font_key in FONT_KEYS
                                 else "system")
        self.result_path = result or None
        self.motion = motion if motion in ("slide", "drag") else "slide"
        self.transparent = bool(transparent)
        self.aspect_fit = (self.motion == "drag") if aspect_fit is None else bool(aspect_fit)
        self.cursor = (self.motion == "drag") if cursor is None else bool(cursor)
        self.sound = bool(sound)
        self.glow = bool(glow)
        self.tail = tail
        self.progress = progress or (lambda d, t: None)
        self.log = log or (lambda s: None)

        self.transition = None
        self.trans_dur = 0.0
        if use_transition:
            self.transition = transition or asset_path("fotor-loading.webm")
            if not os.path.exists(self.transition):
                raise RuntimeError("找不到 Fotor 转场素材: " + self.transition)
            self.trans_dur = probe_duration(self.transition) or 1.97

        self.t_trans = T_TRANS if self.motion == "slide" else D_SETTLE + 0.15
        end = self.t_trans + self.trans_dur if use_transition else \
            (T_SETTLE if self.motion == "slide" else D_SETTLE) + 0.8
        # 自然时长（所有关键点都按这个时间轴定义），再按目标时长整体缩放。
        # 缩放放在「帧 → 时间」这一步，所有动作关键点就自动跟着走，不用逐个改。
        natural = end + self.tail
        try:
            want = float(duration) if duration else 0.0
        except (TypeError, ValueError):
            want = 0.0
        self.speed = (natural / want) if want > 0.05 else 1.0
        self.total = int(round(natural / self.speed * FPS))

    # ---------- 每帧 ----------
    def _base_layer(self):
        if self.transparent:
            return Image.new("RGBA", (W, H), (0, 0, 0, 0))
        return self.static.copy().convert("RGBA")

    def _frame_slide(self, t):
        frame = self._base_layer()
        st = photo_state(t)
        if st:
            cx, cy, sc = st
            bw = (PHOTO_BOX[2] - PHOTO_BOX[0]) * sc
            bh = (PHOTO_BOX[3] - PHOTO_BOX[1]) * sc
            ph = self._photo_now(t).resize((max(2, int(bw)), max(2, int(bh))),
                                           Image.LANCZOS)
            x, y = int(cx - bw / 2), int(cy - bh / 2)
            if not self.transparent:
                g = ph.convert("RGB").resize((max(1, ph.width // 8), max(1, ph.height // 8)))
                g = g.resize(ph.size).filter(ImageFilter.GaussianBlur(38)).convert("RGBA")
                g.putalpha(ph.split()[3].point(lambda v: v // 3))
                frame.alpha_composite(g, (x, y + 46))
            frame.alpha_composite(ph, (x, y))
            if self.cursor and t < self.t_trans:
                draw_cursor(frame, x + bw * 0.74, y + bh * 0.72)
        return frame

    def _frame_drag(self, t):
        frame = self._base_layer()
        target = self.target
        if not self.transparent:
            frame.alpha_composite(self.vignette)
            panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(panel).rounded_rectangle(
                (target[0] - 20, target[1] - 20, target[2] + 20, target[3] + 20),
                radius=42, fill=(4, 8, 16, 92))
            frame.alpha_composite(panel.filter(ImageFilter.GaussianBlur(3)))

        # 吸附瞬间的白色描边脉冲
        snap = _clamp((t - D_SNAP) / 0.34)
        if self.glow and 0 < snap < 1:
            ga = round(170 * math.sin(math.pi * snap))
            if ga:
                gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                ImageDraw.Draw(gl).rounded_rectangle(
                    (target[0] - 9, target[1] - 9, target[2] + 9, target[3] + 9),
                    radius=39, outline=(255, 255, 255, ga), width=18)
                frame.alpha_composite(gl.filter(ImageFilter.GaussianBlur(18)))

        # 虚线框（相位随时间走，形成流动感）+ 加号
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        _dashed_round_rect(d, target, 36, 25, 17, 5,
                           (255, 255, 255, 175 if t < D_SNAP else 240))
        if t < D_DRAG0 + 0.62:
            cx, cy = (target[0] + target[2]) // 2, (target[1] + target[3]) // 2
            d.line((cx - 42, cy, cx + 42, cy), fill=(255, 255, 255, 185), width=8)
            d.line((cx, cy - 42, cx, cy + 42), fill=(255, 255, 255, 185), width=8)
        frame.alpha_composite(layer)

        if t >= D_APPEAR:
            x, y, w, ang = drag_motion(t, self.aspect, target)
            iw, ih = paste_card(frame, self._photo_now(t), x, y, w, self.aspect, ang,
                                shadow_alpha=0 if self.transparent else 145)
            if self.cursor and t < D_CURSOR_OFF:
                press = _clamp((t - 0.68) / 0.18) * (1 - _clamp((t - 2.14) / 0.22))
                rel = _clamp((t - 2.13) / 0.34)
                draw_cursor(frame, x + iw * 0.74, y + ih * 0.72,
                            rel if rel > 0 else press * 0.12)
        return frame

    def _photo_now(self, t):
        """转场跑过 REVEAL_AT 后把结果图淡进来。"""
        if self.result is None or not self.trans_dur:
            return self.photo
        r0 = self.t_trans + self.trans_dur * REVEAL_AT
        if t < r0:
            return self.photo
        return Image.blend(self.photo, self.result,
                           _smooth(min(1.0, (t - r0) / 0.32)))

    def frame_at(self, n):
        t = n / FPS * self.speed
        frame = self._frame_drag(t) if self.motion == "drag" else self._frame_slide(t)
        if self.stream is not None and t >= self.t_trans:
            ov = self.stream.next()
            if ov is not None:
                frame.alpha_composite(ov)
        if self.pill is not None:
            frame.alpha_composite(self.pill)
        return frame if self.transparent else frame.convert("RGB")

    # ---------- 渲染 ----------
    def render(self):
        os.makedirs(self.tmp_dir, exist_ok=True)
        out_dir = os.path.dirname(self.out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        src = ImageOps.exif_transpose(Image.open(self.photo_path).convert("RGB"))
        self.aspect = src.width / src.height
        if self.motion == "drag":
            self.target = target_box_for_aspect(self.aspect) if self.aspect_fit \
                else (PHOTO_BOX[0], PHOTO_BOX[1], PHOTO_BOX[2], PHOTO_BOX[3])
            self.photo = src
            self.result = ImageOps.exif_transpose(
                Image.open(self.result_path).convert("RGB")) if self.result_path else None
            if self.result is not None:
                self.result = ImageOps.fit(self.result, self.photo.size, method=Image.LANCZOS)
            # 拖拽风格自己逐帧画虚线框和加号，底层只要一张纯黑；
            # 以前这里设成 None，非透明底时 _base_layer 的 .copy() 直接崩。
            self.static = Image.new("RGB", (W, H), (0, 0, 0))
            v = Image.new("L", (W, H), 0)
            ImageDraw.Draw(v).ellipse((-260, 180, W + 260, H + 310), fill=170)
            v = v.filter(ImageFilter.GaussianBlur(210))
            dim = Image.new("RGBA", (W, H), (4, 8, 18, 175))
            dim.putalpha(Image.eval(v, lambda p: 195 - p // 2))
            self.vignette = dim
        else:
            box = target_box_for_aspect(self.aspect) if self.aspect_fit else PHOTO_BOX
            self.photo = load_photo(self.photo_path, box)
            self.result = load_photo(self.result_path, box) if self.result_path else None
            self.static = build_static()

        self.pill = (build_pill(self.caption, self.caption_style,
                                self.caption_y, self.caption_font_key)
                     if (self.caption and not self.transparent) else None)
        self.stream = (TransitionStream(self.transition, speed=self.speed)
                       if self.transition else None)

        wav = None
        if self.sound:
            wav = os.path.join(self.tmp_dir, "dragsfx.wav")
            # 音效的时间点要换算到输出时间轴上，否则变速后咔哒声会对不上吸附
            create_audio(wav, self.total / FPS,
                         drag0=(D_DRAG0 if self.motion == "drag" else T_IN) / self.speed,
                         snap=(D_SNAP if self.motion == "drag" else T_LAND) / self.speed)

        pix = "rgba" if self.transparent else "rgb24"
        cmd = [ffmpeg_exe(), "-y", "-v", "error",
               "-f", "rawvideo", "-pix_fmt", pix, "-s", f"{W}x{H}",
               "-r", str(FPS), "-i", "-"]
        if wav:
            cmd += ["-i", wav]
        else:
            cmd += ["-f", "lavfi", "-t", f"{self.total / FPS:.2f}",
                    "-i", "anullsrc=r=44100:cl=stereo"]
        if self.transparent:
            cmd += ["-c:v", "qtrle", "-pix_fmt", "argb"]
        else:
            cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest",
                "-map", "0:v", "-map", "1:a", self.out_path]

        self.log(f"渲染 {self.total} 帧 ({self.total / FPS:.2f}s, "
                 f"{'透明 MOV' if self.transparent else 'MP4'}) → {self.out_path}")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        broken = False
        try:
            with track_proc(proc):
                for n in range(self.total):
                    check_cancel()
                    proc.stdin.write(self.frame_at(n).tobytes())
                    self.progress(n + 1, self.total)
        except BrokenPipeError:
            broken = True
        except Cancelled:
            if self.stream is not None:
                self.stream.close()
            abort_proc(proc, self.out_path)
            raise
        finally:
            if self.stream is not None:
                self.stream.close()
        try:
            proc.stdin.close()
        except Exception:
            broken = True
        err = proc.stderr.read()
        proc.wait()
        if proc.returncode != 0 or broken:
            bail_if_cancelled(proc, self.out_path)
            raise RuntimeError("ffmpeg 编码失败:\n" + err.decode("utf-8", "ignore")[-2000:])
        self.log("编码完成 ✅")
        return self.out_path


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="CutKit — 拖照片效果演示生成")
    ap.add_argument("photo", help="要上传演示的照片（任意比例）")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--caption", default="Upload Your Photo")
    ap.add_argument("--caption-style", default="plain",
                    choices=[k for k, _, _ in CAPTION_STYLES])
    ap.add_argument("--caption-y", type=float, default=CAPTION_Y,
                    help="标题中心占画面高的比例，0~1")
    ap.add_argument("--font", dest="font_key", default="system",
                    choices=[k for k, _, _ in FONTS])
    ap.add_argument("--duration", type=float, default=SPEED_PRESETS[0][2],
                    help=f"成片总时长（秒），默认 {SPEED_PRESETS[0][2]}；传 0 = 按自然时长")
    ap.add_argument("--result", default=None, help="AI 结果图（转场后淡入）")
    ap.add_argument("--motion", choices=("slide", "drag"), default="slide",
                    help="slide=复刻参考片飞入; drag=光标拖拽（任意比例自适应）")
    ap.add_argument("--transparent", action="store_true",
                    help="输出带 alpha 的 MOV，可直接盖在自己的视频上")
    ap.add_argument("--aspect-fit", dest="aspect_fit", action="store_true", default=None,
                    help="画框按原图比例自适应")
    ap.add_argument("--no-aspect-fit", dest="aspect_fit", action="store_false")
    ap.add_argument("--cursor", dest="cursor", action="store_true", default=None,
                    help="画鼠标指针")
    ap.add_argument("--no-cursor", dest="cursor", action="store_false")
    ap.add_argument("--sound", action="store_true", help="加合成的拖拽音效")
    ap.add_argument("--no-transition", dest="use_transition", action="store_false",
                    default=True, help="不叠 Fotor 转场")
    ap.add_argument("--transition", default=None, help="替换用的转场素材(带 alpha)")
    ap.add_argument("--tail", type=float, default=TAIL)
    args = ap.parse_args(argv)
    ext = ".mov" if args.transparent else ".mp4"
    out = args.out or os.path.splitext(args.photo)[0] + "-拖照片演示" + ext
    DragDemo(args.photo, out, caption=args.caption, result=args.result,
             transition=args.transition, tail=args.tail, motion=args.motion,
             transparent=args.transparent, aspect_fit=args.aspect_fit,
             cursor=args.cursor, sound=args.sound,
             use_transition=args.use_transition,
             caption_style=args.caption_style,
             caption_y=args.caption_y, caption_font_key=args.font_key,
             duration=args.duration,
             progress=lambda d, t: (d % 30 == 0) and print(f"{d}/{t}"),
             log=print).render()
    print("OK", out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
