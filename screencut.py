#!/usr/bin/env python3
"""CutKit 录屏步骤剪辑 — 自动剪掉等待时间 + 步骤字幕（复刻 7.8 加字幕成片工作流）.

输入手机录屏（滤镜 App 操作全程），输出 ~15s 竖版成片:
  1. 运动检测: 低差分的长静止段 = 等待（AI 生成/加载），压缩成短节拍
  2. 最长的等待 = AI 处理: 之前是"选图/选滤镜"阶段、之后是"结果"阶段
  3. 四段步骤字幕（白字黑描边，随滤镜名变化）:
       Select Photo → Select the "X Effect" → Wait.... → Results... ❤️
  4. 裁 9:16（去状态栏）→ 1080x1920 30fps H.264
"""
import json
import os
import re
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from render import ffmpeg_exe, _font

W_OUT, H_OUT = 1080, 1920
ANA_FPS = 6          # 分析帧率
ANA_W = 160          # 分析缩放宽
ACT_TH = 2.2         # 帧差 > 此值 = 有操作（0-255 灰度均值差）
MIN_WAIT = 1.6       # 静止超过此秒数才算"等待"，否则原样保留
WAIT_KEEP = 0.8      # 每个等待压缩后保留秒数
PAD_PRE, PAD_POST = 0.30, 0.40   # 动作段前后留白
TAIL_MAX = 7.0       # 结果段（最后一个等待之后）最长保留
HEAD_SKIP = 0.8      # 开头丢弃（录屏启动 UI）

EMOJI_FONTS = ("/System/Library/Fonts/Apple Color Emoji.ttc",   # macOS
                "C:/Windows/Fonts/seguiemj.ttf")                  # Windows


# ---------- 探测与分析 ----------
def probe(path):
    """返回 (width, height, duration_sec)."""
    r = subprocess.run([ffmpeg_exe(), "-i", path], capture_output=True, text=True)
    info = r.stderr
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", info)
    d = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", info)
    if not (m and d):
        raise RuntimeError("无法读取视频信息: " + path)
    dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))
    return int(m.group(1)), int(m.group(2)), dur


def analyze(path, log=None, progress=None):
    """低帧率灰度解码，算每帧与上一帧的平均差. 返回 (diffs, ana_fps, W, H, dur)."""
    log = log or (lambda s: None)
    W, H, dur = probe(path)
    ah = max(2, int(round(ANA_W * H / W / 2)) * 2)
    log(f"分析录屏 {W}x{H} {dur:.1f}s ...")
    cmd = [ffmpeg_exe(), "-v", "error", "-i", path,
           "-vf", f"fps={ANA_FPS},scale={ANA_W}:{ah}",
           "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    frame_bytes = ANA_W * ah
    diffs = []
    prev = None
    n_expect = max(1, int(dur * ANA_FPS))
    while True:
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        g = np.frombuffer(buf, dtype=np.uint8).astype(np.float32)
        diffs.append(0.0 if prev is None else float(np.abs(g - prev).mean()))
        prev = g
        if progress and len(diffs) % 30 == 0:
            progress(min(len(diffs), n_expect), n_expect)
    proc.wait()
    return np.array(diffs), ANA_FPS, W, H, dur


# ---------- 剪辑计划 ----------
def plan(diffs, ana_fps, dur,
         act_th=ACT_TH, min_wait=MIN_WAIT, wait_keep=WAIT_KEEP,
         tail_max=TAIL_MAX, head_skip=HEAD_SKIP):
    """返回 dict(segments=[(src_a, src_b), ...], roles=[...], big_wait_idx, out_len).

    roles: 每段一个 'pre'(选图等操作) | 'effect'(选滤镜, 大等待前最后一段)
           | 'wait'(压缩的等待节拍) | 'result'(大等待之后)
    """
    t = np.arange(len(diffs)) / ana_fps
    active = diffs > act_th
    active[t < head_skip] = False

    # 动作运行区间（源时间）
    runs = []
    i = 0
    n = len(diffs)
    while i < n:
        if active[i]:
            j = i
            while j < n and (active[j] or (j + 1 < n and active[j + 1])):
                j += 1
            runs.append([i / ana_fps, j / ana_fps])
            i = j
        i += 1
    if not runs:
        raise RuntimeError("没检测到任何操作（画面几乎全程静止）")

    # 加前后留白并合并（间隔 < min_wait 的静止直接保留原样，并入同段）
    merged = []
    for a, b in runs:
        a, b = max(0.0, a - PAD_PRE), min(dur, b + PAD_POST)
        if merged and a - merged[-1][1] < min_wait:
            merged[-1][1] = b
        else:
            merged.append([a, b])

    # 段间等待: 找最长的一个 = AI 处理
    gaps = [(merged[k + 1][0] - merged[k][1], k) for k in range(len(merged) - 1)]
    big_gap_k = max(gaps)[1] if gaps else None

    # 组装输出段: 每个等待压缩成 wait_keep 秒的节拍（取等待开头）
    segments, roles = [], []
    for k, (a, b) in enumerate(merged):
        is_last_pre = big_gap_k is not None and k == big_gap_k
        segments.append((a, b))
        if big_gap_k is None:
            roles.append("pre" if k < len(merged) - 1 else "result")
        elif k <= big_gap_k:
            roles.append("effect" if is_last_pre else "pre")
        else:
            roles.append("result")
        if k < len(merged) - 1:
            gap_len = merged[k + 1][0] - b
            if gap_len >= min_wait and wait_keep > 0:
                segments.append((b, min(b + wait_keep, merged[k + 1][0])))
                roles.append("wait")

    # 结果段限长：把 'result' 段的总长裁到 tail_max（保留前面部分和最后1s定格）
    res_idx = [i for i, r in enumerate(roles) if r == "result"]
    res_len = sum(segments[i][1] - segments[i][0] for i in res_idx)
    if res_len > tail_max:
        over = res_len - tail_max
        for i in reversed(res_idx):
            a, b = segments[i]
            cut = min(over, (b - a) - 0.5)
            if cut > 0:
                segments[i] = (a, b - cut)
                over -= cut
            if over <= 0.01:
                break

    out_len = sum(b - a for a, b in segments)
    return dict(segments=[(round(a, 2), round(b, 2)) for a, b in segments],
                roles=roles, out_len=round(out_len, 2))


def caption_texts(filter_name):
    fn = (filter_name or "").strip()
    eff = f'Select the “{fn} Effect”' if fn else "Select the Effect"
    return ["Select Photo", eff, "Wait....", "Results..."]


def caption_windows(p):
    """按段角色算 4 条字幕的输出时间窗 [(t0,t1) or None]×4.

    大等待前最后一个动作段是"点生成"，从它开始显示 Wait....（与上次成片一致）；
    它之前的部分：若有多个 pre 段，最后一个 pre 段配 Select Effect 字幕，
    只有一个 pre 段就按 60/40 拆给 Select Photo / Select Effect。
    """
    t = 0.0
    starts = []
    for (a, b) in p["segments"]:
        starts.append(t)
        t += b - a
    total = t
    w = [None, None, None, None]

    tap_start = None       # 大等待前最后一个动作段（点生成）的输出时间
    first_result = None
    pre_starts = []
    for i, role in enumerate(p["roles"]):
        if role == "pre":
            pre_starts.append(starts[i])
        elif role == "effect":
            tap_start = starts[i]
        elif role == "result" and first_result is None:
            first_result = starts[i]

    end_sel = tap_start if tap_start is not None else \
        (first_result if first_result is not None else total)
    if end_sel > 0:
        if len(pre_starts) >= 2:
            split = pre_starts[-1]          # 最后一个 pre 段 = 选滤镜
        else:
            split = round(end_sel * 0.6, 2)  # 单段按 60/40 拆
        w[0] = (0, split)
        w[1] = (split, end_sel)
    if tap_start is not None:
        w[2] = (tap_start, first_result if first_result is not None else total)
    if first_result is not None:
        w[3] = (first_result, total + 1)
    return w, total


# ---------- 字幕 PNG ----------
def _make_caption(text, size, stroke, y_center, heart=False):
    img = Image.new("RGBA", (W_OUT, H_OUT), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = _font(size, "bold", text)
    bbox = d.textbbox((0, 0), text, font=f)
    tw = bbox[2] - bbox[0]
    heart_img, gap, hw = None, 9, 0
    emoji_font = next((p for p in EMOJI_FONTS if os.path.exists(p)), None)
    if heart and emoji_font:
        try:
            ef = ImageFont.truetype(emoji_font, 160)
            eimg = Image.new("RGBA", (220, 220), (0, 0, 0, 0))
            ImageDraw.Draw(eimg).text((10, 10), "❤️", font=ef,
                                      embedded_color=True)
            eb = eimg.getbbox()
            if eb:
                heart_img = eimg.crop(eb)
                hh = 66
                hw = round(heart_img.width * hh / heart_img.height)
                heart_img = heart_img.resize((hw, hh), Image.LANCZOS)
        except Exception:
            heart_img = None
    total = tw + ((gap + hw) if heart_img else 0)
    x0 = (W_OUT - total) / 2 - bbox[0]
    y0 = y_center - (bbox[1] + bbox[3]) / 2
    d.text((x0, y0), text, font=f, fill=(255, 255, 255, 255),
           stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    if heart_img:
        img.paste(heart_img,
                  (round((W_OUT - total) / 2 + tw + gap),
                   round(y_center - heart_img.height / 2)), heart_img)
    return img


# 字幕样式: (字号, 描边, y中心) — 与上次成片一致
CAP_STYLE = [(86, 6, 1024), (63, 5, 369), (86, 6, 367), (73, 5, 377)]

ZOOM_IN_FRAMES = 12    # 放大动画帧数（30fps ≈ 0.4s）
ZOOM_HOLD_SEC = 1.8    # 放大后定格秒数


# ---------- 结尾放大 ----------
def detect_result_rect(img):
    """在成片最后一帧(1080x1920)里找结果照片显示区（有纹理的内容区 vs 扁平UI）.
    照片内容行/列的局部标准差高，纯色 UI 底接近 0. 返回 (x, y, w, h) 或 None."""
    g = np.asarray(img.convert("L"), dtype=np.float32)
    H, W = g.shape
    mid = g[:, W // 5:W * 4 // 5]
    rich = (mid.std(axis=1) > 14) | (mid.mean(axis=1) > 60)
    rich[:70] = rich[-250:] = False   # 排除顶部导航与底部样式条
    best, cur = None, None
    for y in range(len(rich)):
        # 容忍 ≤12 行的间断（照片里的纯色横条，如记分牌边缘）
        ok = rich[y] or (cur is not None and rich[y:y + 12].any())
        if ok and cur is None:
            cur = y
        elif not ok and cur is not None:
            if best is None or y - cur > best[1] - best[0]:
                best = (cur, y)
            cur = None
    if cur is not None and (best is None or len(rich) - cur > best[1] - best[0]):
        best = (cur, len(rich))
    if not best or best[1] - best[0] < H * 0.3:
        return None
    y0, y1 = best
    band = g[y0:y1, :]
    cok = (band.std(axis=0) > 14) | (band.mean(axis=0) > 60)
    xs = np.where(cok)[0]
    if len(xs) < W * 0.4:
        return None
    x0, x1 = int(xs[0]), int(xs[-1]) + 1
    if (x1 - x0) < W * 0.5:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def build_zoom_tail(base_img, tmp_dir, photo_path, log=None):
    """把结果原图从 App 照片框位置放大铺满全屏（12帧 smoothstep + 定格）.

    起点 = 最后一帧里检测到的照片显示区（按原图长宽比 contain 修正），
    终点 = 原图 cover 铺满 1080x1920。中间帧把原图缩放后贴在最后一帧上。
    """
    log = log or (lambda s: None)
    if not (photo_path and os.path.isfile(photo_path)):
        raise ValueError("结尾放大需要选择结果原图")
    from PIL import ImageOps as _IO
    photo = Image.open(photo_path).convert("RGB")
    photo = _IO.exif_transpose(photo)

    rect = detect_result_rect(base_img)
    if rect is None:
        rect = (75, 180, W_OUT - 150, int((W_OUT - 150) * photo.height / photo.width))
        log("未检测到照片框，使用默认位置")
    else:
        log(f"检测到照片显示区 x={rect[0]} y={rect[1]} w={rect[2]} h={rect[3]}")
    # 原图与录屏显示方向可能不一致（AI 出图存在镜像变体）——
    # 与最后一帧照片区比对，镜像更接近就自动翻转对齐，保证放大动画连贯
    rx, ry, rw, rh = rect
    try:
        region = base_img.crop((rx, ry, rx + rw, ry + rh))
        sig = lambda im: np.asarray(im.convert("L").resize((32, 32)),
                                    dtype=np.float32)
        reg_s = sig(region)
        d_norm = np.abs(reg_s - sig(photo)).mean()
        mirrored = photo.transpose(Image.FLIP_LEFT_RIGHT)
        d_mirr = np.abs(reg_s - sig(mirrored)).mean()
        if d_mirr + 3 < d_norm:
            photo = mirrored
            log("原图与录屏方向相反，已自动镜像对齐")
    except Exception:
        pass
    s = min(rw / photo.width, rh / photo.height)
    ws, hs = photo.width * s, photo.height * s
    x0s, y0s = rx + (rw - ws) / 2, ry + (rh - hs) / 2

    # 终点: cover 铺满画布
    s = max(W_OUT / photo.width, H_OUT / photo.height)
    we, he = photo.width * s, photo.height * s
    x0e, y0e = (W_OUT - we) / 2, (H_OUT - he) / 2

    zoom_dir = os.path.join(tmp_dir, "zoom")
    os.makedirs(zoom_dir, exist_ok=True)
    N = ZOOM_IN_FRAMES
    for i in range(N + 1):
        t = i / N
        e = t * t * (3 - 2 * t)
        x = x0s + (x0e - x0s) * e
        y = y0s + (y0e - y0s) * e
        w = ws + (we - ws) * e
        h = hs + (he - hs) * e
        fr = base_img.copy()
        fr.paste(photo.resize((round(w), round(h)), Image.LANCZOS),
                 (round(x), round(y)))
        fr.save(os.path.join(zoom_dir, f"z_{i:02d}.jpg"), quality=95)

    lst = os.path.join(tmp_dir, "zoomlist.txt")
    with open(lst, "w") as f:
        for i in range(N):
            f.write(f"file 'zoom/z_{i:02d}.jpg'\nduration {1/30:.6f}\n")
        f.write(f"file 'zoom/z_{N:02d}.jpg'\nduration {ZOOM_HOLD_SEC}\n")
        f.write(f"file 'zoom/z_{N:02d}.jpg'\n")
    tail = os.path.join(tmp_dir, "tail.mp4")
    r = subprocess.run([ffmpeg_exe(), "-y", "-v", "error",
                        "-f", "concat", "-safe", "0", "-i", lst,
                        "-vf", f"fps=30,scale={W_OUT}:{H_OUT}",
                        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                        "-pix_fmt", "yuv420p", tail], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("结尾放大编码失败:\n" +
                           r.stderr.decode("utf-8", "ignore")[-1000:])
    return tail


# ---------- 渲染 ----------
def render_screen(src, out_path, plan_d, texts, tmp_dir,
                  audio_path=None, zoom_end=False, zoom_photo=None,
                  log=None, progress=None):
    log = log or (lambda s: None)
    if zoom_end and not (zoom_photo and os.path.isfile(zoom_photo)):
        raise ValueError("勾选了结尾放大，但没有选择结果原图")
    W, H, dur = probe(src)
    os.makedirs(tmp_dir, exist_ok=True)
    main_out = os.path.join(tmp_dir, "main.mp4") if zoom_end else out_path

    # 9:16 裁剪: 宽度全保留，去状态栏（约高度的 3.9%），下方裁掉多余
    ch = int(W * 16 / 9 / 2) * 2
    cy = min(int(0.039 * H), max(0, H - ch))
    if ch > H:
        # 源已比 9:16 更窄长 — 不裁高度，改为裁宽
        ch = H
        cw = int(H * 9 / 16 / 2) * 2
        cx = (W - cw) // 2
        crop = f"crop={cw}:{ch}:{cx}:0"
    else:
        crop = f"crop={W}:{ch}:0:{cy}"

    wins, total = caption_windows(plan_d)
    cap_files = []
    for i, (text, (size, stroke, ycc)) in enumerate(zip(texts, CAP_STYLE)):
        if wins[i] is None or not (text or "").strip():
            cap_files.append(None)
            continue
        fp = os.path.join(tmp_dir, f"cap{i+1}.png")
        _make_caption(text, size, stroke, ycc, heart=(i == 3)).save(fp)
        cap_files.append(fp)

    segs = plan_d["segments"]
    fc = []
    for i, (a, b) in enumerate(segs):
        fc.append(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[s{i}]")
    fc.append("".join(f"[s{i}]" for i in range(len(segs))) +
              f"concat=n={len(segs)}:v=1[cat]")
    fc.append(f"[cat]{crop},scale={W_OUT}:{H_OUT},fps=30[base]")

    cmd = [ffmpeg_exe(), "-y", "-i", src]
    lbl = "base"
    n_in = 1
    for i, fp in enumerate(cap_files):
        if fp is None:
            continue
        cmd += ["-i", fp]
        t0, t1 = wins[i]
        nxt = f"o{i}"
        fc.append(f"[{lbl}][{n_in}:v]overlay=0:0:"
                  f"enable='between(t,{t0:.2f},{t1:.2f})'[{nxt}]")
        lbl = nxt
        n_in += 1
    full_len = total + (ZOOM_IN_FRAMES / 30 + ZOOM_HOLD_SEC if zoom_end else 0)
    if zoom_end:
        # 主片先出无声视频，结尾放大 tail 拼接时再统一配音轨
        cmd += ["-filter_complex", ";".join(fc),
                "-map", f"[{lbl}]", "-an"]
    else:
        if audio_path:
            cmd += ["-i", audio_path]
        else:
            cmd += ["-f", "lavfi", "-t", f"{total:.2f}",
                    "-i", "anullsrc=r=44100:cl=stereo"]
        cmd += ["-filter_complex", ";".join(fc),
                "-map", f"[{lbl}]", "-map", f"{n_in}:a",
                "-c:a", "aac", "-b:a", "128k", "-shortest"]
    cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", main_out]
    log(f"剪辑 {len(segs)} 段 → {full_len:.1f}s，开始编码 ...")
    if progress:
        progress(0, 0)   # 编码阶段进度未知，前端转为跑马
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 编码失败:\n" +
                           r.stderr.decode("utf-8", "ignore")[-2000:])

    if zoom_end:
        log("生成结尾放大动画 ...")
        # 主片最后一帧（带字幕）作为放大底图
        r = subprocess.run([ffmpeg_exe(), "-y", "-v", "error",
                            "-sseof", "-0.1", "-i", main_out,
                            "-frames:v", "1", "-update", "1",
                            os.path.join(tmp_dir, "last.png")],
                           capture_output=True)
        if r.returncode != 0:
            raise RuntimeError("提取最后一帧失败:\n" +
                               r.stderr.decode("utf-8", "ignore")[-800:])
        base = Image.open(os.path.join(tmp_dir, "last.png")).convert("RGB")
        tail = build_zoom_tail(base, tmp_dir, photo_path=zoom_photo, log=log)
        cmd = [ffmpeg_exe(), "-y", "-v", "error", "-i", main_out, "-i", tail]
        if audio_path:
            cmd += ["-i", audio_path]
        else:
            cmd += ["-f", "lavfi", "-t", f"{full_len:.2f}",
                    "-i", "anullsrc=r=44100:cl=stereo"]
        cmd += ["-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
                "-map", "[v]", "-map", "2:a",
                "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-shortest", "-movflags", "+faststart", out_path]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError("结尾拼接失败:\n" +
                               r.stderr.decode("utf-8", "ignore")[-2000:])
    log("编码完成 ✅")
    return out_path


# ---------- CLI ----------
def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="CutKit — 录屏步骤剪辑（剪掉等待+步骤字幕）")
    ap.add_argument("video", help="原始录屏文件")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--filter", dest="filter_name", default="",
                    help='滤镜名，用于第二条字幕 Select the "X Effect"')
    ap.add_argument("--wait-keep", type=float, default=WAIT_KEEP)
    ap.add_argument("--tail-max", type=float, default=TAIL_MAX)
    ap.add_argument("--audio", default=None)
    ap.add_argument("--no-captions", action="store_true",
                    help="不加步骤字幕，只剪辑")
    ap.add_argument("--zoom-end", action="store_true",
                    help="结尾把最终结果图放大铺满并定格")
    ap.add_argument("--zoom-photo", default=None,
                    help="结果原图路径（可选，放大更清晰）")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args(argv)

    diffs, afps, W, H, dur = analyze(args.video, log=print)
    p = plan(diffs, afps, dur, wait_keep=args.wait_keep, tail_max=args.tail_max)
    for (a, b), role in zip(p["segments"], p["roles"]):
        print(f"  {role:7s} {a:7.2f} – {b:7.2f}  ({b-a:.2f}s)")
    print(f"成片时长 ≈ {p['out_len']}s")
    if args.plan_only:
        return 0
    if args.zoom_end and not args.zoom_photo:
        ap.error("--zoom-end 需要 --zoom-photo <结果原图>")
    out = args.out or os.path.splitext(args.video)[0] + "_加字幕.mp4"
    tmp = os.path.join(os.path.expanduser("~/Library/Caches"), "CutKit")
    texts = ["", "", "", ""] if args.no_captions else caption_texts(args.filter_name)
    render_screen(args.video, out, p, texts, tmp,
                  audio_path=args.audio, zoom_end=args.zoom_end,
                  zoom_photo=args.zoom_photo, log=print)
    print("OK", out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
