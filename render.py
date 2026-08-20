#!/usr/bin/env python3
"""CutKit 渲染引擎 — 9:16 前后对比滑杆视频（复刻 7.8 golden hours / add flash 工作流）.

每个场景 = 一对 before/after 图：白色滑杆带圆形手柄在画面上左右扫动，
左边露出 Before、右边露出 After；场景之间用旋转模糊转场；顶部叠加字幕。
帧由 Pillow 逐帧生成，rawvideo 直灌 ffmpeg (imageio-ffmpeg 自带二进制) 编码 H.264。
"""
import math
import os
import subprocess
import sys
import threading

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        for p in (os.path.expanduser("~/.local/bin/ffmpeg"),
                  "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
            if os.path.exists(p):
                return p
        return "ffmpeg"


# ---------- 取消生成 ----------
# 所有渲染模块共用这一套：前端点「取消」后，帧循环在下一帧抛 Cancelled，
# 同时注册表里正在跑的 ffmpeg 会被直接杀掉（编码阶段不吃帧循环的检查）。
class Cancelled(Exception):
    """用户中途取消了生成。"""


_CANCEL = threading.Event()
_PROCS = set()
_PROCS_LOCK = threading.Lock()


def request_cancel():
    _CANCEL.set()
    with _PROCS_LOCK:
        procs = list(_PROCS)
    for pr in procs:
        try:
            pr.kill()
        except Exception:
            pass


def clear_cancel():
    _CANCEL.clear()


def is_cancelled():
    return _CANCEL.is_set()


def check_cancel():
    if _CANCEL.is_set():
        raise Cancelled()


class track_proc:
    """把 ffmpeg 子进程登记进注册表，取消时能被杀掉。"""

    def __init__(self, proc):
        self.proc = proc

    def __enter__(self):
        with _PROCS_LOCK:
            _PROCS.add(self.proc)
        return self.proc

    def __exit__(self, *a):
        with _PROCS_LOCK:
            _PROCS.discard(self.proc)
        return False


def abort_proc(proc, out_path=None):
    """取消时收尾：杀进程、删掉写了一半的文件。"""
    for fn in (lambda: proc.kill(),
               lambda: proc.stdin and proc.stdin.close(),
               lambda: proc.wait(timeout=5)):
        try:
            fn()
        except Exception:
            pass
    if out_path and os.path.exists(out_path):
        try:
            os.remove(out_path)
        except Exception:
            pass


def bail_if_cancelled(proc, out_path=None):
    """取消时我们直接杀 ffmpeg，管道会先炸出 BrokenPipe / 非零退出码。
    这里把那种「假失败」还原成 Cancelled，免得界面报成渲染出错。"""
    if is_cancelled():
        abort_proc(proc, out_path)
        raise Cancelled()


def run_tracked(cmd, out_path=None, **kw):
    """subprocess.run 的可取消版本：注册进程，取消时被杀掉后抛 Cancelled。
    传了 out_path 的话，取消时顺手删掉写了一半的输出文件。"""
    kw.pop("capture_output", None)          # Popen 不认这个参数，下面固定收管道
    kw.setdefault("stdout", subprocess.PIPE)
    kw.setdefault("stderr", subprocess.PIPE)
    proc = subprocess.Popen(cmd, **kw)
    with track_proc(proc):
        out, err = proc.communicate()
    if is_cancelled() and out_path and os.path.exists(out_path):
        try:
            os.remove(out_path)
        except Exception:
            pass
    check_cancel()
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


# ---------- fonts ----------
_FONT_FILES = (
    "/System/Library/Fonts/HelveticaNeue.ttc",   # macOS
    "C:/Windows/Fonts/arialbd.ttf",              # windows fallback
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
_CJK_FONT_FILES = (
    "/System/Library/Fonts/STHeiti Medium.ttc",   # macOS 中文（PIL 打不开 PingFang.ttc）
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
)


def _has_cjk(s):
    return any("⺀" <= ch <= "鿿" or "　" <= ch <= "〿"
               or "＀" <= ch <= "￯" for ch in s or "")


def _font(size, weight="medium", text=""):
    """weight: medium|bold|regular — HelveticaNeue.ttc indexes: 10=Medium, 1=Bold, 0=Regular.
    含中文的文本自动改用 PingFang（HelveticaNeue 没有中文字形会画成方框）."""
    if _has_cjk(text):
        for path in _CJK_FONT_FILES:
            if os.path.exists(path):
                try:
                    f = ImageFont.truetype(path, size, index=0)
                    if f.getmask("变").getbbox():  # 确认有中文字形
                        return f
                except Exception:
                    continue
    idx = {"medium": 10, "bold": 1, "regular": 0}.get(weight, 10)
    for path in _FONT_FILES:
        if os.path.exists(path):
            try:
                if path.endswith(".ttc"):
                    return ImageFont.truetype(path, size, index=idx)
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _text_shadow(draw, xy, s, font, anchor="la", fill=(255, 255, 255, 255)):
    x, y = xy
    draw.text((x + 2, y + 3), s, font=font, fill=(0, 0, 0, 110), anchor=anchor)
    draw.text((x, y), s, font=font, fill=fill, anchor=anchor)


def _smooth(a):
    return a * a * (3 - 2 * a)


# ---------- 揭示机制（对比展示方式） ----------
# sweep/once  滑杆（原有）
# reverse     反向污染：先给成图，绿色吞噬，再还原
# wipe        手指擦除：橡皮擦沿蛇形路径把滤镜抹掉
# flicker     硬切闪频：不做渐变，前后直接交替
# progress    进度条还原：顶部扫描线 + 处理进度条
# comment     评论区驱动：先出一条评论卡，再还原
# grid        九宫格多米诺：多对图同屏，逐格翻转
REVEAL_MODES = ("sweep", "once", "reverse", "wipe", "flicker",
                "progress", "comment", "grid")
_SLIDER_LIKE = ("sweep", "once", "comment")


def _blob_field(W, H, center=(0.5, 0.5), seed=7, wobble=0.55, cells=5):
    """归一化"污染扩散"场：0=起点，1=最远处；叠低频噪声让边缘不规则。"""
    rng = np.random.default_rng(seed)
    noise = np.asarray(
        Image.fromarray((rng.random((cells, cells)) * 255).astype(np.uint8))
        .resize((W, H), Image.BICUBIC), dtype=np.float32) / 255.0
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.hypot(xx - center[0] * W, yy - center[1] * H)
    d /= max(float(d.max()), 1e-6)
    f = d * (1.0 + wobble * (noise - 0.5) * 2.0)
    f -= float(f.min())
    return f / max(float(f.max()), 1e-6)


def _field_mask(field, t, feather=0.12):
    """field <= t 的区域为实心，边缘按 feather 羽化。"""
    a = np.clip((t * (1.0 + feather) - field) / feather, 0.0, 1.0)
    return Image.fromarray((a * 255.0).astype(np.uint8), "L")


_WIPE_ROWS = 5


def _wipe_head(u, rows=_WIPE_ROWS, ltr=False):
    """橡皮擦头部在归一化时刻 u 的位置（x, y 比例）。蛇形来回。
    ltr=True 从左边起手，否则从右边起手。"""
    u = max(0.0, min(1.0, u))
    row = min(int(u * rows), rows - 1)
    local = u * rows - row
    if row % 2:
        local = 1.0 - local
    x = -0.06 + 1.12 * local
    return (x if ltr else 1.0 - x), (row + 0.5) / rows


def _wipe_time_field(W, H, rows=_WIPE_ROWS, steps=160, ltr=False):
    """每个像素被擦掉的归一化时刻（0..1），没擦到的为 >1。四分之一分辨率算完再放大。"""
    sw, sh = max(W // 4, 8), max(H // 4, 8)
    yy, xx = np.mgrid[0:sh, 0:sw].astype(np.float32)
    best = np.full((sh, sw), 2.0, np.float32)
    r = 0.135 * sh
    for i in range(steps):
        u = i / (steps - 1)
        hx, hy = _wipe_head(u, rows, ltr)
        hit = np.hypot(xx - hx * sw, yy - hy * sh) <= r
        best = np.where(hit & (best > u), u, best)
    small = Image.fromarray((np.clip(best, 0.0, 2.0) * 127.5).astype(np.uint8), "L")
    return np.asarray(small.resize((W, H), Image.BILINEAR), np.float32) / 127.5


# 硬切闪频的翻转时刻（场景内比例）；最后一段定格 After
_FLICKER_KEYS = (0.0, 0.07, 0.13, 0.19, 0.25, 0.32, 0.41, 0.52, 0.64)


def _flicker_after(tf):
    if tf >= _FLICKER_KEYS[-1]:
        return True
    return sum(1 for k in _FLICKER_KEYS if tf >= k) % 2 == 0


def _wrap(draw, text, font, max_w):
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:3]


# 滑杆扫动关键帧: (场景内时间比例, 滑杆x比例)
# sweep = 来回扫几次（原工作流）; once = 只滑一次并滑到底（全 Before → 全 After）
_SLIDER_KEYS = {
    "sweep": [(0.0, 0.94), (0.30, 0.06), (0.58, 0.66), (0.92, 0.05), (1.0, 0.05)],
    "once": [(0.0, 1.0), (0.14, 1.0), (0.86, 0.0), (1.0, 0.0)],
}

# 每个场景滑杆手柄的默认纵向位置（循环使用）
_HANDLE_CYCLE = (0.45, 0.75, 0.62)


def _slider_x(tf, mode="sweep"):
    keys = _SLIDER_KEYS.get(mode) or _SLIDER_KEYS["sweep"]
    for (t0, x0), (t1, x1) in zip(keys, keys[1:]):
        if tf <= t1:
            a = 0 if t1 == t0 else (tf - t0) / (t1 - t0)
            return x0 + (x1 - x0) * _smooth(max(0.0, min(1.0, a)))
    return keys[-1][1]


class Renderer:
    """把若干 before/after 图片对渲染成一支 9:16 对比视频.

    pairs: [(before_path, after_path), ...]
    caption: 顶部字幕（可空）；画面为全屏对比
    """

    # 字幕默认放视频顶部、TikTok 安全区内（顶部 For You 标签栏以下，
    # 避开画面中部人脸）: 1080x1920 下顶部 ~220px 是 UI 遮挡区
    CAPTION_Y_TOP = 270

    def __init__(self, pairs, out_path,
                 caption="", caption_size=55, caption_y=CAPTION_Y_TOP,
                 label_before="Before", label_after="After", label_size=37,
                 scene_sec=3.6, fps=30, width=1080, height=1920,
                 transition="spin", slider_mode="sweep", audio_path=None,
                 handles=None, crf=18, preset="medium",
                 reveal=None, direction="rtl", comment_user="@user",
                 comment_text="can u remove the matcha filter from this",
                 progress_text="Removing filter",
                 align_mode="auto", align_fill=True, nudges=None,
                 progress=None, log=None):
        if not pairs:
            raise ValueError("没有图片对")
        self.align_mode = align_mode or "auto"
        self.align_fill = bool(align_fill)
        self.nudges = nudges or {}
        self.pairs = pairs
        self.out_path = out_path
        self.caption = caption or ""
        self.label_before = label_before
        self.label_after = label_after
        self.fps = int(fps)
        self.W = int(width)
        self.H = int(height)
        self.transition = transition
        # reveal 优先；未给则沿用旧的 slider_mode（向后兼容）
        mode = (reveal or slider_mode or "sweep")
        self.reveal = mode if mode in REVEAL_MODES else "sweep"
        # 滑杆本身的运动曲线：comment 模式复用 once
        self.slider_mode = ("once" if self.reveal in ("once", "comment")
                            else "sweep")
        # rtl = 滑杆从右往左（默认，前三条的行为）; ltr = 从左往右
        self.ltr = (direction == "ltr")
        self.comment_user = comment_user or "@user"
        self.comment_text = comment_text or ""
        self.progress_text = progress_text or "Removing filter"
        self.audio_path = audio_path or None
        self.crf = crf
        self.preset = preset
        self.progress = progress or (lambda done, total: None)
        self.log = log or (lambda s: None)

        # 对比卡全屏铺满
        self.CX0, self.CY0, self.CW, self.CH = 0, 0, self.W, self.H
        self.CRAD = 1
        self.label_pos = 100

        self.f_caption = _font(int(caption_size), "medium", caption)
        self.f_label = _font(int(label_size), "medium",
                             (label_before or "") + (label_after or ""))
        self.f_comment = _font(int(label_size * 1.22), "medium",
                               (comment_user or "") + (comment_text or ""))
        self.f_prog = _font(int(label_size * 1.15), "medium", progress_text or "")
        self.caption_y = int(caption_y)

        n = len(pairs)
        self.scene_frames = max(int(round(scene_sec * self.fps)), 20)
        if self.reveal == "grid":
            # 九宫格是"一屏演完"，时长按对数放大，最少 4 秒
            self.scene_frames = max(int(round(scene_sec * self.fps * 1.6)),
                                    int(round(4.0 * self.fps)))
            self.total = self.scene_frames
            self.bounds = []
        else:
            self.total = self.scene_frames * n
            self.bounds = [self.scene_frames * i for i in range(1, n)]
        self.TR = 8  # 转场帧数（跨切点居中）

        self.handles = list(handles) if handles else [
            _HANDLE_CYCLE[i % len(_HANDLE_CYCLE)] for i in range(n)]

        self.card_mask = Image.new("L", (self.CW, self.CH), 0)
        ImageDraw.Draw(self.card_mask).rounded_rectangle(
            (0, 0, self.CW - 1, self.CH - 1), self.CRAD, fill=255)

        self.grid = None
        self.scenes = []
        if self.reveal == "grid":
            self.grid = self._build_grid(pairs)
            self.log(f"九宫格: {self.grid[0]}×{self.grid[1]}，共 {len(self.grid[4])} 格")
        else:
            for i, (b, a) in enumerate(pairs):
                self.log(f"加载第 {i+1} 对: {os.path.basename(b)}  ↔  {os.path.basename(a)}")
                bs, as_ = ("right", "left") if self.ltr else ("left", "right")
                bc, ac, _info = self._load_pair(b, a, bs, as_, i)
                self.scenes.append(dict(before=bc, after=ac,
                                        handle=self.handles[i]))
        # 按需预计算（都是每片一次，不进逐帧循环）
        self.field = (_blob_field(self.CW, self.CH, (0.5, 0.42))
                      if self.reveal == "reverse" else None)
        self.wipe_t = (_wipe_time_field(self.CW, self.CH, ltr=self.ltr)
                       if self.reveal == "wipe" else None)
        self.UI = self._build_ui()

    # ---------- 素材与静态层 ----------
    def _label_card(self, img, label, side):
        d = ImageDraw.Draw(img, "RGBA")
        m = 48
        if label:
            if side == "left":
                _text_shadow(d, (m, self.label_pos), label, self.f_label, anchor="la")
            else:
                _text_shadow(d, (self.CW - m, self.label_pos), label, self.f_label, anchor="ra")
        return img

    def _load_card(self, path, label, side):
        img = ImageOps.exif_transpose(Image.open(path).convert("RGB"))
        img = ImageOps.fit(img, (self.CW, self.CH), method=Image.LANCZOS,
                           centering=(0.5, 0.5))
        return self._label_card(img, label, side)

    def _load_pair(self, bpath, apath, bside, aside, idx):
        """成对加载；开了自动对齐就先把 after 对到 before 再贴标签。"""
        bimg = Image.open(bpath)
        aimg = Image.open(apath)
        info = None
        if self.align_mode != "off":
            try:
                import align as _align
                nudge = (self.nudges or {}).get(idx)
                bc, ac, info = _align.align_pair(
                    bimg, aimg, (self.CW, self.CH),
                    fill=self.align_fill, nudge=nudge, log=self.log)
            except Exception as e:
                self.log(f"对齐失败({type(e).__name__})，按原样裁切: {e}")
                info = None
        if info is None:
            bc = ImageOps.fit(ImageOps.exif_transpose(bimg).convert("RGB"),
                              (self.CW, self.CH), method=Image.LANCZOS,
                              centering=(0.5, 0.5))
            ac = ImageOps.fit(ImageOps.exif_transpose(aimg).convert("RGB"),
                              (self.CW, self.CH), method=Image.LANCZOS,
                              centering=(0.5, 0.5))
        return (self._label_card(bc, self.label_before, bside),
                self._label_card(ac, self.label_after, aside), info)

    def _build_ui(self):
        return Image.new("RGB", (self.W, self.H), (0, 0, 0))

    # ---------- 帧合成 ----------
    # ---------- 各种揭示机制 ----------
    def _scene_frame(self, si, tf, progress):
        sc = self.scenes[si]
        m = self.reveal
        if m == "reverse":
            return self._comp_reverse(sc, tf)
        if m == "wipe":
            return self._comp_wipe(sc, tf)
        if m == "flicker":
            return self._comp_flicker(sc, tf)
        if m == "progress":
            return self._comp_progress(sc, tf)
        if m == "comment":
            # 前 35% 停在 Before 上出评论卡，之后再滑开
            t2 = max(0.0, (tf - 0.35)) / 0.65
            frame = self._comp_slider(sc, min(1.0, t2))
            self._draw_comment(frame, tf)
            return frame
        return self._comp_slider(sc, tf)

    def _comp_reverse(self, sc, tf):
        """反向污染：先给成图 → 绿色吞噬 → 扫描线还原。"""
        A, B = sc["after"], sc["before"]
        if tf < 0.20:
            return A.copy()
        if tf < 0.44:                                   # 污染扩散
            t = _smooth((tf - 0.20) / 0.24)
            comp = A.copy()
            comp.paste(B, (0, 0), _field_mask(self.field, t * 1.04))
            return comp
        if tf < 0.56:                                   # 定格在 Before
            return B.copy()
        if tf < 0.86:                                   # 扫描线由上往下还原
            t = _smooth((tf - 0.56) / 0.30)
            comp = B.copy()
            y = int(t * self.CH)
            if y > 0:
                comp.paste(A.crop((0, 0, self.CW, y)), (0, 0))
            if 0 < y < self.CH:
                ImageDraw.Draw(comp, "RGBA").rectangle(
                    (0, y - 5, self.CW, y + 5), fill=(255, 255, 255, 235))
            return comp
        return A.copy()

    def _comp_wipe(self, sc, tf):
        """手指擦除：橡皮擦沿蛇形路径把滤镜抹掉，擦过的不再回来。"""
        t = min(1.0, tf / 0.90)
        alpha = np.clip((t - self.wipe_t) * 12.0, 0.0, 1.0)
        comp = sc["before"].copy()
        comp.paste(sc["after"], (0, 0),
                   Image.fromarray((alpha * 255.0).astype(np.uint8), "L"))
        if tf < 0.93:
            hx, hy = _wipe_head(t, ltr=self.ltr)
            cx, cy = int(hx * self.CW), int(hy * self.CH)
            r = int(0.135 * self.CH)   # 与 _wipe_time_field 的擦头半径一致
            d = ImageDraw.Draw(comp, "RGBA")
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255, 26))
            d.ellipse((cx - r, cy - r, cx + r, cy + r),
                      outline=(255, 255, 255, 220), width=7)
            d.ellipse((cx - 13, cy - 13, cx + 13, cy + 13),
                      fill=(255, 255, 255, 235))
        return comp

    def _comp_flicker(self, sc, tf):
        """硬切闪频：不做任何渐变，前后直接交替，后段定格 After。"""
        return (sc["after"] if _flicker_after(tf) else sc["before"]).copy()

    def _comp_progress(self, sc, tf):
        """进度条还原：扫描线从上往下推，底部一条处理进度条。"""
        lead, tail = 0.10, 0.20
        t = _smooth(max(0.0, min(1.0, (tf - lead) / max(1e-6, 1 - lead - tail))))
        comp = sc["before"].copy()
        y = int(t * self.CH)
        if y > 0:
            comp.paste(sc["after"].crop((0, 0, self.CW, y)), (0, 0))
        d = ImageDraw.Draw(comp, "RGBA")
        if 0 < y < self.CH:
            d.rectangle((0, y - 4, self.CW, y + 4), fill=(255, 255, 255, 230))
        bw = int(self.CW * 0.70)
        bx, by, bh = (self.CW - bw) // 2, int(self.CH * 0.745), 20
        d.rounded_rectangle((bx, by, bx + bw, by + bh), bh // 2,
                            fill=(0, 0, 0, 120))
        fw = int(bw * t)
        if fw > bh:
            d.rounded_rectangle((bx, by, bx + fw, by + bh), bh // 2,
                                fill=(255, 255, 255, 245))
        _text_shadow(d, (self.CW // 2, by - 52),
                     f"{self.progress_text}  {int(round(t * 100))}%",
                     self.f_prog, anchor="mm")
        return comp

    def _draw_comment(self, frame, tf):
        """评论区驱动：开头浮一张 TikTok 风格评论卡，之后淡出。"""
        if tf > 0.44 or not self.comment_text:
            return
        a = 1.0 if tf < 0.36 else max(0.0, 1.0 - (tf - 0.36) / 0.08)
        pad, av = 30, 78
        cw = int(self.CW * 0.80)
        d0 = ImageDraw.Draw(frame)
        lines = _wrap(d0, self.comment_text, self.f_comment, cw - av - pad * 3)
        lh = int(self.f_comment.size * 1.32)
        ch = pad * 2 + max(av, int(self.f_comment.size * 0.9) + 12 + lh * len(lines))
        cx0, cy0 = (self.CW - cw) // 2, int(self.CH * 0.60)
        card = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        cd = ImageDraw.Draw(card)
        cd.rounded_rectangle((0, 0, cw - 1, ch - 1), 34, fill=(16, 16, 18, 226))
        cd.ellipse((pad, pad, pad + av, pad + av), fill=(72, 78, 88, 255))
        u = (self.comment_user or "@u").lstrip("@")[:1].upper()
        cd.text((pad + av // 2, pad + av // 2), u, font=self.f_comment,
                fill=(255, 255, 255, 255), anchor="mm")
        tx = pad * 2 + av
        cd.text((tx, pad + 2), self.comment_user, font=self.f_comment,
                fill=(150, 154, 162, 255), anchor="la")
        for i, ln in enumerate(lines):
            cd.text((tx, pad + int(self.f_comment.size * 0.9) + 12 + i * lh), ln,
                    font=self.f_comment, fill=(255, 255, 255, 255), anchor="la")
        if a < 1.0:
            card.putalpha(card.getchannel("A").point(lambda v: int(v * a)))
        frame.paste(card, (cx0, cy0), card)

    # ---------- 九宫格 ----------
    def _fit(self, path, w, h):
        img = ImageOps.exif_transpose(Image.open(path).convert("RGB"))
        return ImageOps.fit(img, (w, h), method=Image.LANCZOS,
                            centering=(0.5, 0.42))

    def _build_grid(self, pairs):
        n = len(pairs)
        cols, rows = ((3, 3) if n >= 9 else (3, 2) if n >= 6 else
                      (2, 2) if n >= 4 else (1, n))
        xs = [self.W * i // cols for i in range(cols + 1)]
        ys = [self.H * i // rows for i in range(rows + 1)]
        tiles = []
        for i in range(cols * rows):
            b, a = pairs[i % n]
            w, h = xs[i % cols + 1] - xs[i % cols], ys[i // cols + 1] - ys[i // cols]
            self.log(f"格 {i+1}: {os.path.basename(b)} → {os.path.basename(a)}")
            tiles.append((self._fit(b, w, h), self._fit(a, w, h)))
        return cols, rows, xs, ys, tiles

    def _grid_frame(self, tf):
        cols, rows, xs, ys, tiles = self.grid
        frame = Image.new("RGB", (self.W, self.H), (8, 8, 8))
        n = len(tiles)
        hold0, flip, hold1 = 0.10, 0.15, 0.22
        stag = max(1e-6, 1.0 - hold0 - hold1 - flip) / max(1, n - 1)
        for i, (B, A) in enumerate(tiles):
            x, y = xs[i % cols], ys[i // cols]
            tw, th = B.size
            p = (tf - (hold0 + i * stag)) / flip
            if p <= 0:
                frame.paste(B, (x, y))
            elif p >= 1:
                frame.paste(A, (x, y))
            else:
                cut = int(_smooth(p) * tw)
                cell = B.copy()
                if cut > 0:
                    if self.ltr:
                        cell.paste(A.crop((0, 0, cut, th)), (0, 0))
                    else:
                        cell.paste(A.crop((tw - cut, 0, tw, th)), (tw - cut, 0))
                if 0 < cut < tw:
                    ex = cut if self.ltr else tw - cut
                    ImageDraw.Draw(cell, "RGBA").rectangle(
                        (ex - 4, 0, ex + 4, th), fill=(255, 255, 255, 235))
                frame.paste(cell, (x, y))
        return frame

    def _comp_slider(self, sc, tf):
        x = _slider_x(tf, self.slider_mode)
        if self.ltr:
            x = 1.0 - x          # 镜像整条运动曲线
        px = int(round(x * self.CW))
        # once 模式允许滑到最边（滑杆滑出画面，露出完整的一侧）
        px = (max(0, min(self.CW, px)) if self.slider_mode == "once"
              else max(10, min(self.CW - 10, px)))
        # rtl: 左 Before / 右 After；ltr: 左 After / 右 Before
        base, top = ((sc["before"], sc["after"]) if self.ltr
                     else (sc["after"], sc["before"]))
        comp = base.copy()
        if px > 0:
            comp.paste(top.crop((0, 0, px, self.CH)), (0, 0))
        at_edge = self.slider_mode == "once" and (px <= 0 or px >= self.CW)
        if not at_edge:
            dd = ImageDraw.Draw(comp, "RGBA")
            dd.rectangle((px - 3, 0, px + 3, self.CH), fill=(255, 255, 255, 235))
        frame = self.UI.copy()
        frame.paste(comp, (self.CX0, self.CY0), self.card_mask)
        if not at_edge:
            d = ImageDraw.Draw(frame, "RGBA")
            hy = self.CY0 + int(sc["handle"] * self.CH)
            hx = self.CX0 + px
            d.ellipse((hx - 46, hy - 46, hx + 46, hy + 46), fill=(255, 255, 255, 240))
            for s in (-1, 1):
                ax = hx + s * 17
                d.line((ax, hy - 15, ax + s * 13, hy), fill=(0, 0, 0), width=7)
                d.line((ax + s * 13, hy, ax, hy + 15), fill=(0, 0, 0), width=7)
        return frame

    def _spin(self, img, ang, zoom, nsamp, dang, dzoom):
        """旋转运动模糊：多份旋转+缩放采样取平均."""
        W, H = self.W, self.H
        acc = None
        for i in range(nsamp):
            a = ang - dang * i / max(1, nsamp - 1)
            z = zoom - dzoom * i / max(1, nsamp - 1)
            zw, zh = int(W * z), int(H * z)
            s = img.resize((zw, zh), Image.BILINEAR)
            s = s.crop(((zw - W) // 2, (zh - H) // 2, (zw - W) // 2 + W, (zh - H) // 2 + H))
            s = s.rotate(a, resample=Image.BILINEAR, fillcolor=(0, 0, 0))
            acc = s if acc is None else Image.blend(acc, s, 1.0 / (i + 1))
        return acc

    def _draw_caption(self, frame):
        if self.caption:
            d = ImageDraw.Draw(frame, "RGBA")
            _text_shadow(d, (self.W // 2, self.caption_y), self.caption,
                         self.f_caption, anchor="mm")
        return frame

    def frame_at(self, n):
        progress = n / max(1, self.total - 1)
        if self.reveal == "grid":
            tf = n / max(1, self.total - 1)
            return self._draw_caption(self._grid_frame(tf))
        if self.transition == "spin":
            for bi, b in enumerate(self.bounds):
                if b - self.TR // 2 <= n < b + self.TR // 2:
                    p = (n - (b - self.TR // 2) + 1) / self.TR
                    step = 1.0 / self.TR
                    maxA, maxZ = 75.0, 0.42
                    if p <= 0.5:
                        base = self._scene_frame(bi, 1.0, progress)
                        e = _smooth(min(1, p * 2))
                        e0 = _smooth(max(0, (p - step) * 2))
                    else:
                        base = self._scene_frame(bi + 1, 0.0, progress)
                        e = _smooth(min(1, (1 - p) * 2))
                        e0 = _smooth(min(1, (1 - p + step) * 2))
                    sign = -1 if p <= 0.5 else 1
                    ang = sign * maxA * e
                    ang0 = sign * maxA * e0
                    z, z0 = 1 + maxZ * e, 1 + maxZ * e0
                    fr = self._spin(base, ang, z, 16, ang - ang0, z - z0)
                    return self._draw_caption(fr)
        si = min(n // self.scene_frames, len(self.scenes) - 1)
        a = si * self.scene_frames
        tf = (n - a) / max(1, self.scene_frames - 1)
        return self._draw_caption(self._scene_frame(si, tf, progress))

    # ---------- 编码 ----------
    def render(self):
        out_dir = os.path.dirname(self.out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        cmd = [ffmpeg_exe(), "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{self.W}x{self.H}", "-r", str(self.fps), "-i", "-"]
        if self.audio_path:
            cmd += ["-i", self.audio_path, "-map", "0:v", "-map", "1:a",
                    "-c:a", "aac", "-b:a", "192k", "-shortest"]
        cmd += ["-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf),
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", self.out_path]
        self.log(f"开始编码 → {self.out_path}")
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
            abort_proc(proc, self.out_path)
            raise
        try:
            proc.stdin.close()
        except Exception:
            broken = True
        err = proc.stderr.read()
        proc.wait()
        if proc.returncode != 0 or broken:
            bail_if_cancelled(proc, self.out_path)
            raise RuntimeError("ffmpeg 编码失败:\n" +
                               err.decode("utf-8", "ignore")[-2000:])
        self.log("编码完成 ✅")
        return self.out_path


# ---------- 自动配对 ----------
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".tif", ".tiff")


def list_images(folder):
    out = []
    for f in sorted(os.listdir(folder)):
        if f.startswith("."):
            continue
        if os.path.splitext(f)[1].lower() in IMG_EXTS:
            out.append(os.path.join(folder, f))
    return out


def _thumb_gray(path, size=16):
    im = Image.open(path).convert("L")
    im = ImageOps.exif_transpose(im)
    im = im.resize((size, size), Image.BILINEAR)
    return list(im.getdata())


def _dist(a, b):
    return sum((x - y) * (x - y) for x, y in zip(a, b))


def classify_role(path):
    """启发式（按本团队工作流）: AI 生成图(ChatGPT/generate 等命名)是拍摄底图
    ="变身前"; 从 App 导出的滤镜结果(IMG_ 相册编号等)通常是"变身后".
    返回 "before" | "after" | None(未知)."""
    n = os.path.basename(path).lower()
    if "before" in n or "原图" in n or "变身前" in n:
        return "before"
    if "after" in n or "成图" in n or "变身后" in n:
        return "after"
    for kw in ("chatgpt", "generate", "generated", "midjourney", "gemini",
               "即梦", "可灵"):
        if kw in n:
            return "before"
    return None


def auto_pair(paths):
    """按视觉相似度贪心配对（同一场景的前后图背景一致，灰度缩略图距离最小）.
    返回 [(before, after), ...]."""
    if len(paths) < 2:
        return []
    sigs = {}
    for p in paths:
        try:
            sigs[p] = _thumb_gray(p)
        except Exception:
            pass
    paths = [p for p in paths if p in sigs]
    befores = [p for p in paths if classify_role(p) == "before"]
    afters = [p for p in paths if classify_role(p) == "after"]
    unknown = [p for p in paths if classify_role(p) is None]

    pairs = []
    # 1) 每张已知 before 找最像的一张（已知 after 优先，否则 unknown 当 after）
    cand_a = afters + unknown
    for b in befores:
        if not cand_a:
            break
        best = min(cand_a, key=lambda a: _dist(sigs[b], sigs[a]))
        pairs.append((b, best))
        cand_a.remove(best)
        if best in unknown:
            unknown.remove(best)
    # 2) 剩余已知 after 找最像的 unknown 当 before
    for a in [x for x in cand_a if x in afters]:
        if not unknown:
            break
        best = min(unknown, key=lambda b: _dist(sigs[a], sigs[b]))
        pairs.append((best, a))
        unknown.remove(best)
    # 3) 剩余 unknown 两两配对（距离最近贪心），文件更旧的一张当 before
    while len(unknown) >= 2:
        p0 = unknown[0]
        best = min(unknown[1:], key=lambda q: _dist(sigs[p0], sigs[q]))
        a, b = sorted((p0, best), key=lambda x: os.path.getmtime(x))
        pairs.append((a, b))
        unknown.remove(p0)
        unknown.remove(best)
    return pairs


# ---------- CLI ----------
def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="CutKit — 前后对比视频生成")
    ap.add_argument("folder", help="含 before/after 图片的文件夹")
    ap.add_argument("-o", "--out", default=None, help="输出 mp4 路径")
    ap.add_argument("--caption", default="", help="顶部字幕")
    ap.add_argument("--label-before", default="Before")
    ap.add_argument("--label-after", default="After")
    ap.add_argument("--scene-sec", type=float, default=3.6)
    ap.add_argument("--transition", choices=("spin", "none"), default="spin")
    ap.add_argument("--slider", "--reveal", dest="slider",
                    choices=REVEAL_MODES, default="sweep",
                    help="对比展示方式: sweep=来回扫动(默认) once=只滑一次 "
                         "reverse=反向污染 wipe=手指擦除 flicker=硬切闪频 "
                         "progress=进度条 comment=评论区驱动 grid=九宫格")
    ap.add_argument("--direction", choices=("rtl", "ltr"), default="rtl",
                    help="横向揭示方向: rtl=从右往左(默认) ltr=从左往右。"
                         "对滑杆/擦除/九宫格生效")
    ap.add_argument("--comment-user", default="@user", help="comment 模式的用户名")
    ap.add_argument("--comment-text",
                    default="can u remove the matcha filter from this",
                    help="comment 模式的评论内容")
    ap.add_argument("--progress-text", default="Removing filter",
                    help="progress 模式进度条上方的文案")
    ap.add_argument("--audio", default=None, help="BGM 音频文件")
    ap.add_argument("--no-align", action="store_true",
                    help="关闭人物自动对齐（默认开启）")
    ap.add_argument("--align-keep-frame", action="store_true",
                    help="对齐时保留 before 的完整构图（默认裁到共同区域）")
    ap.add_argument("--plan-only", action="store_true", help="只打印配对结果不渲染")
    args = ap.parse_args(argv)

    paths = list_images(args.folder)
    pairs = auto_pair(paths)
    if not pairs:
        print("未找到可配对的图片")
        return 1
    for i, (b, a) in enumerate(pairs):
        print(f"pair {i+1}: {os.path.basename(b)}  ->  {os.path.basename(a)}")
    if args.plan_only:
        return 0
    out = args.out or os.path.join(args.folder, "paircut-beforeafter-9x16.mp4")
    r = Renderer(pairs, out, caption=args.caption,
                 label_before=args.label_before, label_after=args.label_after,
                 scene_sec=args.scene_sec, reveal=args.slider,
                 direction=args.direction,
                 comment_user=args.comment_user, comment_text=args.comment_text,
                 progress_text=args.progress_text,
                 align_mode="off" if args.no_align else "auto",
                 align_fill=not args.align_keep_frame,
                 transition=args.transition, audio_path=args.audio,
                 progress=lambda d, t: (d % 60 == 0) and print(f"{d}/{t}"),
                 log=print)
    r.render()
    print("OK", out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
