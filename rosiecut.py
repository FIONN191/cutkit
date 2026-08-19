#!/usr/bin/env python3
"""rosiecut - auto-edit Fotor magic-edit iPhone screen recordings into short demo clips.

Pipeline:
  1. analyze  - per-frame metrics (keyboard-zone brightness, frame diffs)  [cached .npz]
  2. classify - stable states: paint (editor), type (keyboard up), wait (generating dim)
                keyboard slide animations & unstable frames are dropped
  3. plan     - per-segment speed: painting shots squeezed to a fixed beat
                (--paint-sec), typing at a fixed speed-up (--type-speed), waits
                to a fixed beat; or fit a total duration when --target is given
  4. render   - ffmpeg: crop to 9:16 hiding all buttons, scale 1080x1920, 30fps, no audio

Usage:
  python3 rosiecut.py input.mp4 -o out.mp4 [--target 16] [--paint-sec 1.67]
                      [--type-speed 9] [--wait-sec 0.8] [--last-wait-sec 0.8]
                      [--crop W:H:X:Y] [--plan-only]
"""
import argparse
import json
import os
import re
import subprocess
import sys

import cv2
import numpy as np

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG = "ffmpeg"

# packaged as a windowed app: children must not flash a console (no-op elsewhere)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

KB_UP_THRESH = 150     # keyboard-zone mean brightness above this = keyboard up
DIM_THRESH = 45        # whole-frame mean below this = dimmed "generating" state
# NOTE: iPhone screen recordings are VFR (frames are dropped while the screen is
# static), so frame_index / nominal_fps is NOT real time. We index everything by
# decoder frame number (matches OpenCV sequential reads and ffmpeg select 'n') and
# carry a real per-frame timestamp for durations.
KB_SLIDE_DIFF = 15     # keyboard-zone diff above this while the keyboard shows =
                       # keyboard/input-box slide animation (keystrokes stay ~<10)
GREEN_PX = 20          # saturated-green pixels in the left-mid zone above this =
                       # the "Prompt History" header underline is on screen
COUNTER_P95 = 70       # p95 brightness of the char-counter corner ("N/3000 | x",
                       # bottom-right of the input box in its typing position):
                       # bright text while typing; flat panel when the prompt-history
                       # sheet is up (whatever its scroll state)
TYPE_HEAD_TRIM_SEC = 0.25  # shave the input-box settle animation off typing runs
MIN_STABLE_SEC = 0.35  # runs shorter than this are treated as transition -> dropped
PH_SLIDE_DIFF = 2.0    # photo-zone diff above this near a typing run = the whole
                       # layout sliding with the keyboard (photo shifts), not painting
PH_SETTLE = 0.4        # contiguous walk from a typing-run edge: still settling
                       # above this, calm below (the slide tail decays ~1.7->0.3)
TYPE_GUARD_SEC = 1.0   # window before/after typing runs scanned for slide motion
DIM_TRIM_FRAC = 0.85   # paint-run edge frames darker than this fraction of the run's
                       # median brightness are the dim ramp into/out of "generating"
DIM_TRIM_MAX_SEC = 0.6  # the ramp is short; never shave more than this per edge
OUT_W, OUT_H = 1080, 1920
CACHE_VERSION = 10

# ---- overlay assets for the natural-flow version -------------------------
# Both are authored square and are laid in scaled to the full output width, so
# the author's framing inside the square decides where the content lands:
#   precomp   - own loading animation (alpha), content centered -> frame centre
#   watermark - fotor logo on pure black, content at the square's bottom ->
#               square's bottom edge aligned to the frame bottom
PRECOMP_NAME = "预合成 1.mov"
WATERMARK_NAME = "FOTOR水印裁剪过.mov"
# How wide the artwork itself is drawn, in % of the output width (the asset's own
# transparent/black padding is measured away first, see content_bbox). Both land
# dead centre. The pre-comp default matches the hand-built reference edit (scale
# 0.2 on a 1170-wide canvas = 13.7% of the width for the asset frame, 7.4% for the
# artwork in it); the watermark default matches the reference frame it was sized
# against (the lockup spans ~44% of the frame width there).
PRECOMP_PCT = 7.4
WATERMARK_PCT = 44.0
# author's material folder: makes the assets resolve with no setup on that Mac
MATERIAL_DIR = ("/Volumes/SN580 1TB Media/运营内容/海外运营/rosiediary_ai/素材")


def app_support_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "RosieCut")


def find_asset(name, override=None):
    """Locate an overlay asset: explicit path, then the usual drop folders."""
    if override:
        return override if os.path.isfile(override) else None
    exe_dir = os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, "frozen", False) else __file__))
    for d in (os.path.join(app_support_dir(), "assets"),
              os.path.join(exe_dir, "assets"),
              os.path.join(getattr(sys, "_MEIPASS", exe_dir), "assets"),
              MATERIAL_DIR):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def probe_range(path):
    """'pc' (full) or 'tv' (limited) colour range of a file, None if unknown.

    Overlay assets are usually tv-range; mixing them in makes ffmpeg convert the
    BASE to tv, which shifts every level in the picture. Knowing the source's
    range lets us convert the overlays instead and pin the output.
    """
    r = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True,
                       creationflags=NO_WINDOW)
    m = re.search(r"Video:.*?,\s(yuvj?[0-9a-z]+)\(([a-z0-9]+)", r.stderr)
    if not m:
        return None
    if m.group(2) in ("pc", "tv"):
        return m.group(2)
    return "pc" if m.group(1).startswith("yuvj") else None


def unique_path(path):
    """Never clobber an earlier cut: foo.mp4 -> foo_2.mp4 -> foo_3.mp4 ..."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem}_{n}{ext}"):
        n += 1
    return f"{stem}_{n}{ext}"


def content_bbox(path, use_alpha):
    """Bounding box of the asset's visible content, as fractions of its frame.

    Scanned over every frame on a downscaled copy — the transparent (or black)
    padding around the artwork must not count towards the on-screen size, so
    sizing can be expressed as "how wide the artwork itself is".
    """
    vf = "alphaextract," if use_alpha else ""
    r = subprocess.run([FFMPEG, "-loglevel", "error", "-i", path,
                        "-vf", f"{vf}scale=160:160,format=gray",
                        "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True, creationflags=NO_WINDOW)
    a = np.frombuffer(r.stdout, np.uint8)
    if a.size < 160 * 160:
        return 0.0, 0.0, 1.0, 1.0
    a = a[: a.size - a.size % (160 * 160)].reshape(-1, 160, 160).max(axis=0)
    ys, xs = np.where(a > 8)
    if not len(xs):
        return 0.0, 0.0, 1.0, 1.0
    return (xs.min() / 160, ys.min() / 160,
            (xs.max() + 1) / 160, (ys.max() + 1) / 160)


ALPHA_FMTS = ("yuva", "argb", "rgba", "abgr", "bgra", "gbrap", "ya8", "ya16")


def has_alpha(path):
    """Does the asset carry a real alpha channel (vs. artwork on black)?"""
    r = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True,
                       creationflags=NO_WINDOW)
    m = re.search(r"Video: .*?, (\w+)", r.stderr)
    return bool(m) and m.group(1).startswith(ALPHA_FMTS)


def asset_duration(path):
    """Seconds, read off the ffmpeg header (works for ProRes 4444 too)."""
    r = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True,
                       creationflags=NO_WINDOW)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr)
    if not m:
        raise RuntimeError(f"读不出素材时长: {path}")
    h, mm, s = m.groups()
    return int(h) * 3600 + int(mm) * 60 + float(s)


# ---------------------------------------------------------------- analysis
def analyze(path, cache, log=print):
    if cache and os.path.exists(cache):
        z = np.load(cache)
        if "version" in z.files and int(z["version"]) == CACHE_VERSION:
            return (z["metrics"], float(z["fps"]), int(z["width"]),
                    int(z["height"]), z["times"])
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError(f"cannot open {path}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    sw, sh = W // 4, H // 4
    kb = (slice(int(sh * 0.66), int(sh * 0.92)), slice(int(sw * 0.05), int(sw * 0.95)))
    # left-mid zone: wherever the layout puts the prompt-history panel, its header
    # underline (saturated brand green, left-aligned) lands inside this window;
    # photo content there is dimmed while typing, so it can't fake that green
    gz = (slice(int(sh * 0.25), int(sh * 0.65)), slice(int(sw * 0.02), int(sw * 0.25)))
    # char-counter corner of the input box at its typing position ("N/3000 | x")
    ct = (slice(int(sh * 0.535), int(sh * 0.57)), slice(int(sw * 0.70), int(sw * 0.92)))
    # photo zone: catches the whole layout sliding up/down with the keyboard
    ph = (slice(int(sh * 0.08), int(sh * 0.60)), slice(int(sw * 0.05), int(sw * 0.95)))
    rows, times, prev, n = [], [], None, 0
    while True:
        t_ms = cap.get(cv2.CAP_PROP_POS_MSEC)  # real timestamp, before read advances it
        ok, frame = cap.read()
        if not ok:
            break
        times.append(t_ms / 1000.0)
        small = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_AREA)
        g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.int16)
        kb_diff = 0.0 if prev is None else float(np.abs(g - prev)[kb].mean())
        ph_diff = 0.0 if prev is None else float(np.abs(g - prev)[ph].mean())
        z = small[gz].astype(np.int16)
        green = float(((z[..., 1] - np.maximum(z[..., 0], z[..., 2]) > 40)
                       & (z[..., 1] > 90)).sum())
        counter = float(np.percentile(g[ct], 95))
        rows.append((float(g[kb].mean()), kb_diff, green, float(g.mean()), counter,
                     ph_diff))
        prev = g
        n += 1
        if n % 2000 == 0:
            log(f"  analyzing... {n} frames")
    cap.release()
    m = np.array(rows, dtype=np.float32)
    t = np.array(times, dtype=np.float64)
    # POS_MSEC can be flaky/zero on some files; fall back to nominal fps if so
    if len(t) < 2 or not np.all(np.diff(t) >= 0) or t[-1] <= 0:
        t = np.arange(len(m), dtype=np.float64) / (fps or 30.0)
    if cache:
        np.savez(cache, metrics=m, fps=fps, width=W, height=H,
                 version=CACHE_VERSION, times=t)
    return m, fps, W, H, t


# ---------------------------------------------------------------- classify
def classify(metrics, fps):
    kb_mean, kb_diff, green, all_mean, counter, ph_diff = metrics.T
    n = len(kb_mean)
    states = np.full(n, "trans", dtype=object)
    typing = kb_mean > KB_UP_THRESH
    states[typing] = "type"
    # keyboard-up frames where the keyboard zone itself is in fast motion are the
    # rise/dismiss slide animation, not typing
    states[typing & (kb_diff > KB_SLIDE_DIFF)] = "trans"
    # typing view requires the input box at its middle position: its char counter
    # is rendered there. The prompt-history sheet (any scroll state) hides it.
    states[typing & (counter < COUNTER_P95)] = "trans"
    # belt and braces: the history sheet's green header underline
    states[typing & (green > GREEN_PX)] = "trans"
    dark = kb_mean <= KB_UP_THRESH
    states[dark & (all_mean < DIM_THRESH)] = "wait"
    states[dark & (all_mean >= DIM_THRESH) & (kb_mean < 60)] = "paint"

    # keyboard rise/dismiss around typing: the whole layout slides with the
    # keyboard, so near a typing stretch any photo- or keyboard-zone motion is
    # the slide animation, not painting — the user isn't painting there
    guard = int(TYPE_GUARD_SEC * fps)
    moving = (kb_diff > KB_SLIDE_DIFF) | (ph_diff > PH_SLIDE_DIFF)
    i = 0
    while i < n:
        if states[i] == "type":
            j = i
            while j < n and states[j] == "type":
                j += 1
            for k in range(max(i - guard, 0), i):
                if moving[k] and states[k] != "type":
                    states[k] = "trans"
            for k in range(j, min(j + guard, n)):
                if moving[k] and states[k] != "type":
                    states[k] = "trans"
            # the slide's decaying tail sits below PH_SLIDE_DIFF but still reads
            # as a wobble at the cut: walk outward contiguously until truly calm
            k = j
            while (k < min(j + guard, n) and states[k] != "type"
                   and (ph_diff[k] > PH_SETTLE or kb_diff[k] > KB_SLIDE_DIFF)):
                states[k] = "trans"
                k += 1
            k = i - 1
            while (k >= max(i - guard, 0) and states[k] != "type"
                   and (ph_diff[k] > PH_SETTLE or kb_diff[k] > KB_SLIDE_DIFF)):
                states[k] = "trans"
                k -= 1
            i = j
        else:
            i += 1

    # shave the input-box settle animation off the head of every typing stretch
    head = int(TYPE_HEAD_TRIM_SEC * fps)
    i = 0
    while i < n:
        if states[i] == "type" and (i == 0 or states[i - 1] != "type"):
            j = i
            while j < n and states[j] == "type":
                j += 1
            states[i: min(i + head, j)] = "trans"
            i = j
        else:
            i += 1

    runs = []
    s0, i0 = states[0], 0
    for i in range(1, n):
        if states[i] != s0:
            runs.append([s0, i0, i])
            s0, i0 = states[i], i
    runs.append([s0, i0, n])
    # paint-run edges darker than the run's median are the dim ramp into/out of
    # the generating overlay -> shaved (they read as a brightness flicker at
    # speed). Only next to a nearby wait run, and never deeper than the ramp
    # could be — so legitimately dark content is left alone.
    min_frames = int(MIN_STABLE_SEC * fps)
    near = int(1.0 * fps)
    limit = int(DIM_TRIM_MAX_SEC * fps)
    cand = [[s, a, b] for s, a, b in runs if s != "trans"]
    for i, r in enumerate(cand):
        s, a, b = r
        if s != "paint":
            continue
        med = float(np.median(all_mean[a:b]))
        if i + 1 < len(cand) and cand[i + 1][0] == "wait" and cand[i + 1][1] - b < near:
            stop = max(b - limit, a + 1)
            while b > stop and all_mean[b - 1] < med * DIM_TRIM_FRAC:
                b -= 1
        if i and cand[i - 1][0] == "wait" and a - cand[i - 1][2] < near:
            stop = min(a + limit, b - 1)
            while a < stop and all_mean[a] < med * DIM_TRIM_FRAC:
                a += 1
        r[1], r[2] = a, b
    # short runs are slide animations / flicker -> dropped
    return [(s, a, b) for s, a, b in cand if (b - a) >= min_frames]


# ---------------------------------------------------------------- plan
def plan(runs, fps, args, times):
    n = len(times)

    def real(a, b):  # real-time duration of frame span [a, b)
        return float(times[min(b, n - 1)] - times[a])

    # drop everything after the last wait (post-generate result screens)
    last_wait = max((i for i, r in enumerate(runs) if r[0] == "wait"), default=None)
    if last_wait is not None:
        runs = runs[: last_wait + 1]

    waits = [i for i, r in enumerate(runs) if r[0] == "wait"]
    target = getattr(args, "target", None)
    if target:
        # fixed total: waits & typing keep their fixed pacing, painting fills the rest
        t_paint = sum(real(a, b) for s, a, b in runs if s == "paint")
        t_type = sum(real(a, b) for s, a, b in runs if s == "type")
        wait_out = (max(len(waits) - 1, 0) * args.wait_sec
                    + (args.last_wait_sec if waits else 0))
        budget = target - wait_out - t_type / args.type_speed
        if budget <= 0:
            raise ValueError("目标时长太短，等待节拍+打字就超了；调大目标时长或留空")
        k = max(t_paint / budget, 1.0)  # common paint speed

    segs = []
    for i, (s, a, b) in enumerate(runs):
        dur = real(a, b)
        if s == "wait":
            out = args.last_wait_sec if i == waits[-1] else args.wait_sec
        elif s == "type":
            out = dur / args.type_speed
        elif target:
            out = dur / k
        else:
            out = min(args.paint_sec, dur)  # fixed beat, never slower than real time
        out = max(out, 1.0 / 30)  # at least one output frame
        segs.append({"kind": s, "a": int(a), "b": int(b),
                     "start": float(times[a]), "end": float(times[min(b, n - 1)]),
                     "speed": round(dur / out, 2), "out": round(out, 3)})
    return segs


# ---------------------------------------------------------------- crop
# The crop is derived from two fixed UI landmarks on keyboard-down editor frames
# (photo size varies per video, the app chrome doesn't):
#   top    = photo top edge, minus a small black margin
#   bottom = top edge of the bright green Generate button, minus a small gap
TOP_MARGIN = 40
BOTTOM_GAP = 12


def _photo_top(gray, W):
    rows = (gray > 60)[:, int(W * 0.25): int(W * 0.75)].mean(axis=1)
    ys = np.where(rows > 0.3)[0]
    return int(ys[0]) if len(ys) else None


def _generate_top(gray, W, H):
    """Topmost row of the Generate button: the last solid bright band of the frame."""
    frac = (gray > 100)[:, int(W * 0.2): int(W * 0.8)].mean(axis=1)
    y = H - 1
    while y > int(H * 0.8) and frac[y] < 0.5:
        y -= 1
    if y <= int(H * 0.8):
        return None
    while y > int(H * 0.8) and frac[y] > 0.35:  # >0.35 rides over the label text dip
        y -= 1
    return y + 1


def detect_crop(path, runs, fps, W, H):
    fallback = (W, W * 16 // 9, 0, max((H - W * 16 // 9) // 2, 0))
    paint = next((r for r in runs if r[0] == "paint"), None)
    if paint is None:
        return fallback
    # median over several frames: drawing overlays (zoom loupe etc.) skew single
    # frames. Read sequentially (VFR-safe) and sample within the paint run, since
    # OpenCV's frame seeking is unreliable on variable-frame-rate recordings.
    wanted = set(np.linspace(paint[1] + 5, paint[2] - 5, 7).astype(int).tolist())
    hi = max(wanted)
    tops, gens = [], []
    cap = cv2.VideoCapture(path)
    idx = 0
    while idx <= hi:
        ok, frame = cap.read()
        if not ok:
            break
        if idx in wanted:
            g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            t = _photo_top(g, W)
            if t is not None:
                tops.append(t)
            b = _generate_top(g, W, H)
            if b is not None:
                gens.append(b)
        idx += 1
    cap.release()
    if not tops:
        return fallback
    top = max(int(np.median(tops)) - TOP_MARGIN, 0)
    bottom = int(np.median(gens)) - BOTTOM_GAP if gens else int(H * 0.9)
    ch = bottom - top
    cw = int(ch * 9 / 16)
    if cw > W:  # landmarks unusually far apart: fall back to full width, keep the top
        cw, ch = W, W * 16 // 9
    cw -= cw % 2
    ch = cw * 16 // 9
    x0 = (W - cw) // 2
    y0 = max(min(top, H - ch), 0)
    return cw, ch, x0, y0


# ------------------------------------------------- natural-mode wait cleanup
# The generating screen = the editor frame the user just left, dimmed, plus the
# fotor logo / progress bar / "Uploading..." overlay. The natural-flow version
# must lose the overlay without touching brightness, geometry or the painted
# smear — so each wait segment is replaced by the last stable pre-wait frame,
# dimmed by the ratio measured from the real wait frames. Geometry is continuous
# by construction (it IS the neighbouring frame), and the dim level matches
# because it is measured, not assumed.

def measure_dim(path, segs, metrics=None, log=print):
    """For each wait segment: (pre_frame_index, measured dim ratio).

    pre = the last bright, keyboard-down editor frame before the wait (found on
    the metrics, so it works even when that moment was too short to survive as
    its own segment). Ratio = median(wait_pixel / pre_pixel) over the top/bottom
    bands of the frame, away from the centered logo/progress/text overlay.
    """
    wants = {}
    for i, s in enumerate(segs):
        if s["kind"] != "wait":
            continue
        pre = segs[i - 1]["b"] - 1 if i else max(s["a"] - 15, 0)
        if metrics is not None:
            kb_mean, all_mean = metrics[:, 0], metrics[:, 3]
            prev_paint = next((p for p in reversed(segs[:i]) if p["kind"] == "paint"),
                              None)
            bright = (np.median(all_mean[prev_paint["a"]:prev_paint["b"]])
                      if prev_paint else np.median(all_mean[all_mean > DIM_THRESH * 2])
                      if (all_mean > DIM_THRESH * 2).any() else None)
            if bright:
                for k in range(s["a"] - 1, max(s["a"] - 200, 0), -1):
                    if all_mean[k] >= bright * 0.9 and kb_mean[k] < 60:
                        pre = k
                        break
        mid = (s["a"] + s["b"]) // 2
        wants[i] = (pre, mid)
    if not wants:
        return {}
    need = sorted({f for pm in wants.values() for f in pm})
    frames, idx = {}, 0
    cap = cv2.VideoCapture(path)
    while idx <= need[-1]:
        ok, fr = cap.read()
        if not ok:
            break
        if idx in need:
            frames[idx] = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        idx += 1
    cap.release()
    dims = {}
    for i, (pre, mid) in wants.items():
        if pre not in frames or mid not in frames:
            continue
        H = frames[pre].shape[0]
        # top band starts below the iOS status bar (it is NOT dimmed by the app)
        band = np.r_[int(H * 0.08):int(H * 0.25), int(H * 0.82):H]
        p, w = frames[pre][band], frames[mid][band]
        mask = p > 30
        if mask.sum() < 100:
            continue
        ratio = float(np.median(w[mask] / p[mask]))
        dims[i] = (pre, min(max(ratio, 0.05), 1.0))
    return dims


def upscale_warning(crop):
    """Human-readable warning when the crop must be upscaled a lot (soft output).

    The usual culprit is a screen recording that was recompressed in transit
    (WeChat/QQ "send video" shrinks it hard); the pipeline then upscales the
    crop to 1080 wide and the softness becomes obvious.
    """
    factor = OUT_W / crop[0]
    if factor <= 1.15:
        return None
    return (f"⚠️ 裁剪宽度只有 {crop[0]}px，需要放大 {factor:.1f} 倍到 {OUT_W}px，成片会发糊。\n"
            "   多半是录屏在传到这台电脑时被压缩了（微信/QQ 直接「发视频」会大幅压画质）。\n"
            "   请把原录屏用「文件」方式发送（微信里选发送文件而不是视频），或用数据线/网盘/AirDrop 传。")


# ---------------------------------------------------------------- render
def render(path, segs, crop, out_path, out_fps=30, dims=None,
           precomp=None, watermark=None, precomp_pct=PRECOMP_PCT,
           watermark_pct=WATERMARK_PCT):
    # Select frames by decoder number (VFR-safe: matches the sequential indices used
    # in analysis) and give each segment its own constant rate so it lasts exactly
    # 'out' seconds, independent of the source's variable timestamps.
    # dims (natural mode): {seg_index: (pre_frame, ratio)} — those wait segments
    # become the pre-wait frame dimmed in RGB (the round trip through gbrp keeps
    # the stream's own colour matrix, so hue/saturation stay put) held for the
    # segment's duration.
    dims = dims or {}
    cw, ch, x0, y0 = crop
    n = len(segs)
    # Overlay layers ride along on the wait segments themselves (before concat),
    # placed in SOURCE pixels so the later crop+scale maps them exactly to the
    # intended output spot — no cumulative-timing drift, no separate pass. The
    # crop is 9:16 like the output, so a square asset stays square.
    # The ending beat carries the fotor lockup alone; every earlier beat gets the
    # pre-comp loading animation (that is how the hand-built edit reads).
    ov_idx = sorted(dims)
    p_idx = ov_idx[:-1]
    w_idx = ov_idx[-1] if ov_idx else None
    lay = {}                             # ffmpeg input index per asset
    inputs = [path]
    if precomp and p_idx:
        lay["p"] = len(inputs)
        inputs.append(precomp)
    if watermark and w_idx is not None:
        lay["w"] = len(inputs)
        inputs.append(watermark)
    # keep the base untouched: bend the overlays to ITS colour range, not vice versa
    rng = probe_range(path) if lay else None
    to_base = f",scale=out_range={rng}" if rng else ""
    k = cw / OUT_W                       # output pixels -> source pixels

    def place(asset, pct, use_alpha):
        """(scaled width, x, y) in SOURCE pixels to draw the artwork centred.

        Sizing and centring are done on the artwork, not on the asset frame (the
        padding around it differs per asset), then mapped into the source so the
        later crop+scale lands it exactly.
        """
        bx0, by0, bx1, by1 = content_bbox(asset, use_alpha)
        s = (pct / 100 * OUT_W) / max(bx1 - bx0, 1e-6)   # asset width in output px
        ox = OUT_W / 2 - (bx0 + bx1) / 2 * s
        oy = OUT_H / 2 - (by0 + by1) / 2 * s
        w = max(int(round(s * k)) // 2 * 2, 2)
        return w, x0 + int(round(ox * k)), y0 + int(round(oy * k))
    def keyed(alpha):
        """Filter bit that leaves a usable alpha channel on an overlay stream.

        Assets that carry real alpha are used as authored; artwork sitting on
        pure black gets its alpha from the brightest channel, so only the artwork
        is drawn and the picture underneath stays untouched.
        """
        return "" if alpha else ("format=rgba,geq=r='r(X,Y)':g='g(X,Y)':"
                                 "b='b(X,Y)':a='max(max(r(X,Y),g(X,Y)),b(X,Y))',")
    parts = [f"[0:v]split={n}" + "".join(f"[s{i}]" for i in range(n)) + ";"]
    if "p" in lay:
        parts.append(f"[{lay['p']}:v]split={len(p_idx)}"
                     + "".join(f"[p{i}]" for i in p_idx) + ";")
        p_dur = asset_duration(precomp)
        p_alpha = has_alpha(precomp)
        p_w, p_x, p_y = place(precomp, precomp_pct, p_alpha)
    if "w" in lay:
        w_dur = asset_duration(watermark)
        w_alpha = has_alpha(watermark)
        w_w, w_x, w_y = place(watermark, watermark_pct, w_alpha)
    for i, s in enumerate(segs):
        if i in dims:
            pre, r = dims[i]
            F = max(int(round(s["out"] * out_fps)), 1)
            parts.append(
                f"[s{i}]select='eq(n\\,{pre})',format=gbrp,"
                f"lutrgb=r=val*{r:.4f}:g=val*{r:.4f}:b=val*{r:.4f},"
                f"format=yuv420p,loop=loop={F - 1}:size=1:start=0,"
                f"setpts=N/{out_fps}/TB[b{i}];")
            cur = f"b{i}"
            if "p" in lay and i in p_idx:
                # the asset plays its whole animation across exactly this beat
                f = (F / out_fps) / p_dur
                parts.append(
                    f"[p{i}]scale={p_w}:-1,{keyed(p_alpha)}"
                    f"setpts=PTS*{f:.6f},fps={out_fps},"
                    f"tpad=stop_mode=clone:stop_duration=0.2,"
                    f"format=yuva420p{to_base}[po{i}];")
                parts.append(
                    f"[{cur}][po{i}]overlay={p_x}:{p_y}:"
                    f"shortest=0:repeatlast=0:eof_action=pass[bp{i}];")
                cur = f"bp{i}"
            if "w" in lay and i == w_idx:
                f = (F / out_fps) / w_dur
                parts.append(
                    f"[{lay['w']}:v]scale={w_w}:-1,{keyed(w_alpha)}"
                    f"setpts=PTS*{f:.6f},fps={out_fps},"
                    f"tpad=stop_mode=clone:stop_duration=0.2,"
                    f"format=yuva420p{to_base}[wo];")
                parts.append(
                    f"[{cur}][wo]overlay={w_x}:{w_y}:"
                    f"shortest=0:repeatlast=0:eof_action=pass[bw{i}];")
                cur = f"bw{i}"
            parts.append(f"[{cur}]null[t{i}];")
            continue
        f = max(s["b"] - s["a"], 1)          # source frames in this segment
        rate = f / s["out"]                  # play them across 'out' seconds
        parts.append(
            f"[s{i}]select='between(n\\,{s['a']}\\,{s['b'] - 1})',"
            f"setpts=N/{rate:.6f}/TB,fps={out_fps},format=yuv420p[t{i}];")
    parts.append("".join(f"[t{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[cat];")
    parts.append(f"[cat]crop={cw}:{ch}:{x0}:{y0},scale={OUT_W}:{OUT_H}:"
                 f"flags=lanczos,fps={out_fps}[out]")
    cmd = [FFMPEG, "-y", "-loglevel", "error"]
    for src in inputs:
        cmd += ["-i", src]
    cmd += ["-filter_complex", "".join(parts), "-map", "[out]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-an"]
    if rng:
        cmd += ["-color_range", rng]
    cmd += [out_path]
    p = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
    if p.returncode:
        raise RuntimeError(f"ffmpeg 失败 ({p.returncode}):\n{p.stderr.strip()[-2000:]}")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Auto-edit Fotor magic-edit screen recordings")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--target", type=float, default=None,
                    help="optional total duration (s); when omitted each painting "
                         "shot gets a fixed --paint-sec beat instead")
    ap.add_argument("--paint-sec", type=float, default=1.67,
                    help="output seconds per painting shot when no --target "
                         "(1.67s = 00:00:01:20 at 30fps)")
    ap.add_argument("--type-speed", type=float, default=9.0,
                    help="fixed speed-up for typing shots")
    ap.add_argument("--wait-sec", type=float, default=0.8,
                    help="output seconds per intermediate generating-wait "
                         "(0.8s = 24 frames, matches the preset overlay length)")
    ap.add_argument("--last-wait-sec", type=float, default=0.8,
                    help="output seconds for the final generating-wait (the ending beat)")
    ap.add_argument("--crop", default=None,
                    help="override crop window as W:H:X:Y (source pixels)")
    ap.add_argument("--mode", choices=("natural", "ad"), default="natural",
                    help="natural: replace generating-wait frames so the fotor "
                         "logo/progress overlay disappears; ad: keep them as-is")
    ap.add_argument("--no-overlay", action="store_true",
                    help="natural mode: don't lay the pre-comp loading animation "
                         "over the wait beats / the fotor watermark over the last one")
    ap.add_argument("--precomp", default=None, help="override pre-comp asset path")
    ap.add_argument("--watermark", default=None, help="override watermark asset path")
    ap.add_argument("--precomp-size", type=float, default=PRECOMP_PCT,
                    help="pre-comp artwork width, %% of the output width")
    ap.add_argument("--watermark-size", type=float, default=WATERMARK_PCT,
                    help="watermark artwork width, %% of the output width")
    ap.add_argument("--plan-only", action="store_true", help="print the plan, don't render")
    args = ap.parse_args()

    src_dir = os.path.dirname(os.path.abspath(args.input))
    base = os.path.splitext(os.path.basename(args.input))[0]
    suffix = "_part1.mp4" if args.mode == "natural" else "_part1_广告.mp4"
    # an explicit -o is used as given; the default name never clobbers an earlier cut
    out = args.output or unique_path(os.path.join(src_dir, f"{base}{suffix}"))
    cache = os.path.join(src_dir, f"{base}_metrics.npz")

    metrics, fps, W, H, times = analyze(args.input, cache)
    runs = classify(metrics, fps)
    segs = plan(runs, fps, args, times)

    total = sum(s["out"] for s in segs)
    print(f"\nplan ({len(segs)} segments, output ~{total:.1f}s):")
    for s in segs:
        print(f"  {s['kind']:5s} {s['start']:7.2f}-{s['end']:7.2f}s  "
              f"x{s['speed']:<6.2f} -> {s['out']:.2f}s")
    with open(os.path.join(src_dir, f"{base}_plan.json"), "w") as f:
        json.dump(segs, f, indent=1)

    if args.plan_only:
        return
    if args.crop is not None:
        crop = tuple(int(v) for v in args.crop.split(":"))
    else:
        crop = detect_crop(args.input, runs, fps, W, H)
    cw, ch, x0, y0 = crop
    print(f"\ncrop: {cw}x{ch}+{x0}+{y0}  ->  {OUT_W}x{OUT_H}@30fps")
    warn = upscale_warning(crop)
    if warn:
        print(warn)
    dims, precomp, watermark = {}, None, None
    if args.mode == "natural":
        dims = measure_dim(args.input, segs, metrics)
        print(f"natural mode: {len(dims)} wait segment(s) replaced "
              f"(ratios {', '.join(f'{r:.2f}' for _, r in dims.values()) or '-'})")
        if not args.no_overlay:
            precomp = find_asset(PRECOMP_NAME, args.precomp)
            watermark = find_asset(WATERMARK_NAME, args.watermark)
            print(f"overlay: precomp {precomp or '未找到（跳过）'}")
            print(f"         watermark {watermark or '未找到（跳过）'}")
    render(args.input, segs, crop, out, dims=dims, precomp=precomp,
           watermark=watermark, precomp_pct=args.precomp_size,
           watermark_pct=args.watermark_size)
    print(f"done: {out}")


if __name__ == "__main__":
    main()
