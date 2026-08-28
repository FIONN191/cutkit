# -*- coding: utf-8 -*-
"""广告成片组装 —— 把 CutKit 已有的几段拼成日常投放的那种成片。

结构（照参考片 idol look2.mp4 逐段实测）：
    [前后对比 ×N]  →  [拖照片演示（加速）]  →  [结果定格]  →  [品牌结尾叠上来]
全程铺 BGM，**总长跟着 BGM 走**：先扣掉结尾和演示这两段固定长度，
剩下的摊给对比片段，再把每段时长吸附到 BGM 的节拍上，切点才不会踩空拍。

结尾素材自带 alpha 且开头是渐变擦入，所以它是「叠」在定格上的，不是接在后面。
"""
import math
import os
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageOps

import render
from render import ffmpeg_exe, check_cancel, run_tracked, Cancelled

W, H, FPS = 1080, 1920, 30


# ---------- 探测 ----------
def probe_duration(path):
    """秒。读文件头，GB 级也是毫秒级。"""
    r = subprocess.run([ffmpeg_exe(), "-i", path], capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "Duration:" in line:
            hms = line.split("Duration:")[1].split(",")[0].strip()
            try:
                h, m, s = hms.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
            except ValueError:
                return 0.0
    return 0.0


def decode_mono(path, sr=22050):
    """解成单声道浮点，供节拍分析。"""
    r = subprocess.run(
        [ffmpeg_exe(), "-v", "error", "-i", path, "-ac", "1", "-ar", str(sr),
         "-f", "f32le", "-"], capture_output=True)
    if not r.stdout:
        return np.zeros(0, np.float32), sr
    return np.frombuffer(r.stdout, np.float32), sr


def beat_period(path, log=None):
    """估 BGM 的每拍秒数。没有 librosa，这里用谱通量起音包络 + 自相关。

    返回 (每拍秒数, BPM)；估不出来就返回 (0, 0)，调用方退回按比例分。
    """
    log = log or (lambda *_: None)
    y, sr = decode_mono(path)
    if y.size < sr:                       # 不足 1 秒，没得分析
        return 0.0, 0.0
    hop, n_fft = 512, 1024
    win = np.hanning(n_fft).astype(np.float32)
    n = 1 + (len(y) - n_fft) // hop
    if n < 32:
        return 0.0, 0.0
    frames = np.lib.stride_tricks.as_strided(
        y, shape=(n, n_fft), strides=(y.strides[0] * hop, y.strides[0])) * win
    mag = np.abs(np.fft.rfft(frames, axis=1))
    flux = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)   # 只取变强的部分
    flux -= flux.mean()
    if flux.std() < 1e-6:
        return 0.0, 0.0
    flux /= flux.std()

    fps_env = sr / hop
    lo = int(fps_env * 60.0 / 200.0)      # 200 BPM
    hi = int(fps_env * 60.0 / 60.0)       # 60 BPM
    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    if hi >= len(ac):
        hi = len(ac) - 1
    if lo >= hi:
        return 0.0, 0.0
    lag = int(np.argmax(ac[lo:hi])) + lo
    if lag <= 0:
        return 0.0, 0.0
    per = lag / fps_env
    bpm = 60.0 / per
    log(f"BGM 节拍: {bpm:.1f} BPM（每拍 {per:.3f}s）")
    return per, bpm


def snap_to_beat(sec, per, lo, hi):
    """把一段时长吸附到最近的整数拍，并夹在 [lo, hi] 内。"""
    if per <= 0:
        return max(lo, min(hi, sec))
    k = max(1, round(sec / per))
    out = k * per
    while out > hi and k > 1:
        k -= 1
        out = k * per
    while out < lo:
        k += 1
        out = k * per
    return max(lo, min(hi, out))


# ---------- 各段 ----------
def build_hold(result_path, dur, out_path, label="After", label_size=37, log=None):
    """结果图铺满画布 + 右上角标，定格 dur 秒。结尾就叠在这一段上。"""
    log = log or (lambda *_: None)
    img = ImageOps.exif_transpose(Image.open(result_path).convert("RGB"))
    img = ImageOps.fit(img, (W, H), method=Image.LANCZOS, centering=(0.5, 0.5))
    if label:
        d = ImageDraw.Draw(img, "RGBA")
        render._text_shadow(d, (W - 48, 100), label,
                            render._font(int(label_size), "medium", label), anchor="ra")
    png = os.path.join(os.path.dirname(out_path), "_hold.png")
    img.save(png)
    log(f"结果定格 {dur:.2f}s")
    r = run_tracked([ffmpeg_exe(), "-y", "-v", "error", "-loop", "1", "-i", png,
                     "-t", f"{dur:.3f}", "-r", str(FPS),
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                     "-pix_fmt", "yuv420p", out_path], out_path=out_path)
    if r.returncode:
        raise RuntimeError("定格段失败:\n" + r.stderr.decode("utf-8", "ignore")[-800:])
    return out_path


def speed_up(src, factor, out_path, log=None):
    """加速片段（无音轨——BGM 统一在最后铺）。"""
    log = log or (lambda *_: None)
    log(f"演示段加速 {factor:g}×")
    r = run_tracked([ffmpeg_exe(), "-y", "-v", "error", "-i", src,
                     "-filter:v", f"setpts=PTS/{factor:g}", "-an",
                     "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
                     "-crf", "18", "-pix_fmt", "yuv420p", out_path],
                    out_path=out_path)
    if r.returncode:
        raise RuntimeError("加速失败:\n" + r.stderr.decode("utf-8", "ignore")[-800:])
    return out_path


def concat(parts, out_path, log=None):
    """顺序拼接（各段已统一到 1080x1920/30fps，可以走 concat 滤镜）。"""
    log = log or (lambda *_: None)
    cmd = [ffmpeg_exe(), "-y", "-v", "error"]
    for p in parts:
        cmd += ["-i", p]
    fc = "".join(f"[{i}:v]scale={W}:{H},setsar=1,fps={FPS}[v{i}];"
                 for i in range(len(parts)))
    fc += "".join(f"[v{i}]" for i in range(len(parts)))
    fc += f"concat=n={len(parts)}:v=1:a=0[out]"
    cmd += ["-filter_complex", fc, "-map", "[out]", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", out_path]
    r = run_tracked(cmd, out_path=out_path)
    if r.returncode:
        raise RuntimeError("拼接失败:\n" + r.stderr.decode("utf-8", "ignore")[-1200:])
    return out_path


def overlay_ending(body, ending, at, out_path, log=None):
    """把带 alpha 的品牌结尾叠到 at 秒处——它开头是渐变擦入，正好盖住定格。"""
    log = log or (lambda *_: None)
    log(f"结尾叠加于 {at:.2f}s")
    cmd = [ffmpeg_exe(), "-y", "-v", "error", "-i", body, "-i", ending,
           "-filter_complex",
           f"[1:v]scale={W}:{H},fps={FPS},setpts=PTS-STARTPTS+{at:.3f}/TB[ov];"
           f"[0:v][ov]overlay=0:0:eof_action=pass[out]",
           "-map", "[out]", "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", out_path]
    r = run_tracked(cmd, out_path=out_path)
    if r.returncode:
        raise RuntimeError("结尾叠加失败:\n" + r.stderr.decode("utf-8", "ignore")[-1200:])
    return out_path


def mux_bgm(video, bgm, out_path, fade=0.6, log=None):
    """铺 BGM，按成片长度裁掉多余并做尾部淡出。"""
    log = log or (lambda *_: None)
    dur = probe_duration(video)
    a = f"atrim=0:{dur:.3f},asetpts=PTS-STARTPTS"
    if fade > 0 and dur > fade:
        a += f",afade=t=out:st={dur - fade:.3f}:d={fade:.2f}"
    r = run_tracked([ffmpeg_exe(), "-y", "-v", "error", "-i", video, "-i", bgm,
                     "-filter_complex", f"[1:a]{a}[a]",
                     "-map", "0:v", "-map", "[a]",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                     "-shortest", out_path], out_path=out_path)
    if r.returncode:
        raise RuntimeError("配乐失败:\n" + r.stderr.decode("utf-8", "ignore")[-800:])
    return out_path


# ---------- 组装 ----------
def plan(total, n_pairs, demo_sec, ending_sec, per_beat,
         min_scene=1.2, max_scene=4.0, min_hold=1.0, max_hold=4.5):
    """把 BGM 长度摊给各段。

    结尾长度固定（素材本身多长就多长），演示段长度也基本固定，
    所以可伸缩的只有「对比片段」和「结果定格」两块。
    先给定格留够结尾擦入的时间，剩下的摊给对比段并吸附到整数拍。
    """
    need_hold = max(min_hold, ending_sec + 0.6)     # 结尾擦入前要能看到结果
    budget = total - demo_sec - need_hold
    if n_pairs <= 0 or budget <= 0:
        return None
    scene = snap_to_beat(budget / n_pairs, per_beat, min_scene, max_scene)
    pairs_sec = scene * n_pairs
    hold = total - pairs_sec - demo_sec
    if hold < min_hold:                              # 摊多了，退一拍
        if per_beat > 0 and scene - per_beat >= min_scene:
            scene -= per_beat
        else:
            scene = max(min_scene, (total - demo_sec - min_hold) / n_pairs)
        pairs_sec = scene * n_pairs
        hold = total - pairs_sec - demo_sec
    # 素材少而 BGM 长时，富余会全堆给定格 —— 与其让结果图干放十几秒，
    # 不如把成片收在自然长度、把 BGM 裁短。
    trimmed = False
    hold = max(min_hold, hold)
    if hold > max(max_hold, ending_sec + 0.6):
        hold = max(max_hold, ending_sec + 0.6)
        trimmed = True
    out_total = pairs_sec + demo_sec + hold
    return {"scene": scene, "pairs": pairs_sec, "demo": demo_sec,
            "hold": hold, "ending_at": max(0.0, out_total - ending_sec),
            "total": out_total, "trimmed": trimmed}


def build(pairs, demo_photo, demo_result, ending, bgm, out_path,
          caption="", label_before="Before", label_after="After",
          demo_speed=2.0, transition="spin", slider="sweep", direction="rtl",
          align_mode="off", align_fill=True,
          log=None, progress=None, tmp_dir=None):
    """产出一条成片。返回输出路径。"""
    log = log or (lambda *_: None)
    progress = progress or (lambda d, t: None)
    import dragdemo

    own_tmp = tmp_dir is None
    tmp = tmp_dir or tempfile.mkdtemp(prefix="adcut-")
    try:
        total = probe_duration(bgm)
        if total <= 0:
            raise RuntimeError("读不出 BGM 时长")
        ending_sec = probe_duration(ending) if ending else 0.0
        per_beat, bpm = beat_period(bgm, log=log)
        log(f"BGM {total:.2f}s，结尾 {ending_sec:.2f}s，{len(pairs)} 对素材")

        # 演示段：先按自然速度渲染，再加速——它的长度由素材决定，不受 BGM 摆布
        check_cancel()
        demo_raw = os.path.join(tmp, "demo_raw.mp4")
        dragdemo.DragDemo(demo_photo, demo_raw, caption="", result=demo_result,
                          motion="drag", cursor=True, sound=False,
                          transparent=False, tmp_dir=tmp,
                          log=log, progress=lambda d, t: progress(d, t * 4)).render()
        demo_fast = speed_up(demo_raw, demo_speed, os.path.join(tmp, "demo.mp4"), log=log)
        demo_sec = probe_duration(demo_fast)

        p = plan(total, len(pairs), demo_sec, ending_sec, per_beat)
        if p is None:
            raise RuntimeError("BGM 太短，装不下这些段落")
        log(f"分配：每对 {p['scene']:.2f}s ×{len(pairs)} = {p['pairs']:.2f}s"
            f" | 演示 {p['demo']:.2f}s | 定格 {p['hold']:.2f}s"
            + (f" | 每拍 {per_beat:.3f}s" if per_beat else " | 未测到节拍，按比例分"))
        if p.get("trimmed"):
            log(f"BGM 比素材能撑的长，成片收在 {p['total']:.2f}s，BGM 裁到同长"
                f"（想更长就多加几对素材）")

        # 前后对比段
        check_cancel()
        pairs_mp4 = os.path.join(tmp, "pairs.mp4")
        render.Renderer(pairs, pairs_mp4, caption=caption,
                        label_before=label_before, label_after=label_after,
                        scene_sec=p["scene"], align_mode=align_mode,
                        align_fill=align_fill, reveal=slider, direction=direction,
                        transition=transition, audio_path=None,
                        log=log, progress=lambda d, t: progress(d, t * 2)).render()

        hold_mp4 = build_hold(demo_result or demo_photo, p["hold"],
                              os.path.join(tmp, "hold.mp4"), label=label_after, log=log)

        check_cancel()
        body = concat([pairs_mp4, demo_fast, hold_mp4],
                      os.path.join(tmp, "body.mp4"), log=log)
        if ending:
            body = overlay_ending(body, ending, p["ending_at"],
                                  os.path.join(tmp, "withend.mp4"), log=log)
        mux_bgm(body, bgm, out_path, log=log)
        log(f"成片 {probe_duration(out_path):.2f}s ✅")
        return out_path
    finally:
        if own_tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ---------- CLI ----------
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="把前后对比 + 拖照片演示 + 品牌结尾组装成广告成片")
    ap.add_argument("--pair", action="append", nargs=2, metavar=("BEFORE", "AFTER"),
                    required=True, help="一对前后图，可重复")
    ap.add_argument("--photo", required=True, help="拖拽演示的原图")
    ap.add_argument("--result", help="AI 结果图（也用作结尾定格）")
    ap.add_argument("--ending", help="带 alpha 的品牌结尾 MOV")
    ap.add_argument("--bgm", required=True, help="BGM —— 成片总长跟着它走")
    ap.add_argument("--caption", default="")
    ap.add_argument("--speed", type=float, default=2.0)
    ap.add_argument("--slider", default="sweep")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args(argv)
    build([tuple(p) for p in a.pair], a.photo, a.result, a.ending, a.bgm, a.out,
          caption=a.caption, demo_speed=a.speed, slider=a.slider, log=print)
    print("OK", a.out)
    return 0
