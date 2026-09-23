#!/usr/bin/env python3
"""CutKit — 前后对比视频自动生成（原生桌面窗口）.

选一个素材文件夹 → 自动按视觉相似度把 before/after 配对 → 一键渲染出
9:16 滑杆对比视频（字幕 + Before/After 角标 + 旋转转场，可选 BGM）。
界面在原生 macOS 窗口（WKWebView）里，内部本地 HTTP 服务承载页面。
命令行透传:  CutKit --cli <文件夹> -o out.mp4 --caption "..."
不用 Tkinter：本机系统 Tk 在部分系统版本上损坏。
"""
import base64
import json
import io
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import render
import screencut
import dragdemo
import history
import rosie
import rosiecut
import ringarrow
import adcut
import i18n
from app_version import APP_VERSION

STATE = {
    "lines": [],
    "busy": False,
    "done": False,
    "ok": False,
    "out": None,
    "kind": "",          # "render" | "analyze" | "screen"
    "plan": None,         # 录屏分析结果
    "video": None,        # 已分析的录屏路径
    "prog_done": 0,
    "prog_total": 0,
    "cancelled": False,
    "last_ping": time.time(),
}
LOCK = threading.Lock()
THUMB_CACHE = {}

SETTING_KEYS = ("jy_auto", "jy_dir",
                "caption", "caption_size", "label_before", "label_after",
                "scene_sec", "transition", "slider", "audio", "demo_caption",
                "comment_user", "comment_text", "progress_text", "direction",
                "pair_groups", "demo_caption_style", "demo_caption_y",
                "demo_font", "demo_duration", "demo_motion", "demo_caption_mode",
                "theme", "accent", "lang")
DEFAULT_SETTINGS = {
    "demo_caption_mode": "none",
    "jy_auto": "", "jy_dir": "",
    "caption": "", "caption_size": "55",
    "label_before": "Before", "label_after": "After",
    "scene_sec": "3.6", "transition": "spin", "slider": "linger", "audio": "",
    "demo_caption": "Upload Your Photo", "demo_caption_style": "pill_pink",
    "comment_user": "@user",
    "comment_text": "can u remove the matcha filter from this",
    "progress_text": "Removing filter",
    "direction": "rtl",
    "pair_groups": "2", "theme": "dark", "accent": "orange", "lang": "zh",
    "demo_caption_style": "plain", "demo_caption_y": "18.5",
    "demo_font": "system", "demo_duration": "1.9", "demo_motion": "drag",
}


def app_support_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "CutKit")


SETTINGS_PATH = os.path.join(app_support_dir(), "settings.json")
UPLOAD_DIR = os.path.join(app_support_dir(), "uploads")


def uploads_out_folder():
    """拖进来的图存在 App 支持目录里，成片不能也丢在那儿 —— 落到 ~/Movies/CutKit。"""
    d = os.path.expanduser("~/Movies/CutKit")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return os.path.expanduser("~")


VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".aiff", ".aif", ".flac", ".ogg")
KIND_EXTS = {"image": render.IMG_EXTS, "video": VIDEO_EXTS, "audio": AUDIO_EXTS}


def _upload_name(name, kind):
    """清洗出一个安全的落盘文件名，保留原扩展名（在该类型白名单内）。"""
    ext = os.path.splitext(name or "")[1].lower()
    allowed = KIND_EXTS.get(kind)
    if allowed and ext not in allowed:
        ext = allowed[0]
    elif not ext:
        ext = ".bin"
    stem = "".join(c for c in os.path.splitext(os.path.basename(name or "file"))[0]
                   if c.isalnum() or c in "-_ ")[:48] or "file"
    return f"{int(time.time()*1000)}-{stem}{ext}"


FRAME_CACHE = {}


def video_frame_png(path, keep_alpha=True, max_w=420):
    """抽一帧当预览图。透明 MOV 保留 alpha，这样叠在预览画面上是真实效果。"""
    key = (path, keep_alpha, max_w, os.path.getmtime(path) if os.path.exists(path) else 0)
    if key in FRAME_CACHE:
        return FRAME_CACHE[key]
    if not path or not os.path.exists(path):
        return None
    fmt = ["-pix_fmt", "rgba"] if keep_alpha else ["-pix_fmt", "rgb24"]

    def grab(t):
        cmd = [render.ffmpeg_exe(), "-v", "error", "-ss", str(t), "-i", path,
               "-frames:v", "1", "-vf", f"scale={max_w}:-1", *fmt,
               "-f", "image2pipe", "-vcodec", "png", "-"]
        try:
            return subprocess.run(cmd, capture_output=True, timeout=25).stdout
        except Exception:
            return b""

    if keep_alpha:
        # 素材常带淡入，头几帧可能几乎全透明。采几个点，挑画面内容最多的那帧，
        # 免得预览里显示一片空白让人以为素材坏了。
        best, best_cover = b"", -1.0
        for t in (0.2, 0.6, 1.2, 2.0):
            d = grab(t)
            if not d:
                continue
            try:
                import numpy as _np
                from PIL import Image as _Im
                a = _np.array(_Im.open(io.BytesIO(d)).convert("RGBA"))[..., 3]
                cover = float((a > 16).mean())
            except Exception:
                cover = 0.0
            if cover > best_cover:
                best, best_cover = d, cover
        data = best or grab(0)
    else:
        data = grab(0.6) or grab(0)
    if not data:
        return None
    FRAME_CACHE[key] = data
    return data


def _probe_ok(path, kind):
    """用 ffmpeg 读一下文件头，确认真有对应的音/视频流。"""
    try:
        r = subprocess.run([render.ffmpeg_exe(), "-v", "error", "-i", path,
                            "-t", "0", "-f", "null", "-"],
                           capture_output=True, timeout=20)
    except Exception:
        return True                    # 探测本身失败就别拦着用户
    if r.returncode != 0:
        return False
    try:
        info = subprocess.run([render.ffmpeg_exe(), "-i", path],
                              capture_output=True, timeout=20).stderr.decode("utf-8", "ignore")
    except Exception:
        return True
    want = "Video:" if kind == "video" else "Audio:"
    return want in info


def save_upload_stream(name, kind, src, length):
    """把拖进来的文件边读边落盘，不整个进内存（录屏可能上 GB）。"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, _upload_name(name, kind))
    left = length
    with open(path, "wb") as f:
        while left > 0:
            chunk = src.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    if left > 0:                      # 传输中断，别留半个文件
        try:
            os.remove(path)
        except Exception:
            pass
        raise IOError("文件没传完")
    return path


def save_upload(name, raw):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(name or "")[1].lower()
    if ext not in render.IMG_EXTS:
        ext = ".png"
    stem = "".join(c for c in os.path.splitext(os.path.basename(name or "img"))[0]
                   if c.isalnum() or c in "-_ ")[:48] or "img"
    path = os.path.join(UPLOAD_DIR, f"{int(time.time()*1000)}-{stem}{ext}")
    with open(path, "wb") as f:
        f.write(raw)
    return path


PAIR_PRESET_PATH = os.path.join(app_support_dir(), "pair_presets.json")

# 前后对比的参数预设。存的是 ② 参数卡片里的一整套值（含两个对齐开关）。
PAIR_PRESET_KEYS = ("caption", "caption_size", "label_before", "label_after",
                    "scene_sec", "transition", "slider", "direction",
                    "comment_user", "comment_text", "progress_text",
                    "align_on", "align_fill")


def pair_presets_load():
    try:
        with open(PAIR_PRESET_PATH) as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    presets = d.get("presets")
    if not isinstance(presets, dict):
        presets = {}
    last = d.get("last")
    return {"presets": presets, "last": last if last in presets else ""}


def _pair_presets_write(d):
    os.makedirs(app_support_dir(), exist_ok=True)
    with open(PAIR_PRESET_PATH, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def pair_preset_save(name, params):
    d = pair_presets_load()
    d["presets"][name] = {k: str(params.get(k, "")) for k in PAIR_PRESET_KEYS}
    d["last"] = name
    _pair_presets_write(d)
    return d


def pair_preset_delete(name):
    d = pair_presets_load()
    d["presets"].pop(name, None)
    if d["last"] == name:
        d["last"] = next(iter(d["presets"]), "")
    _pair_presets_write(d)
    return d


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            d = json.load(f)
        out = dict(DEFAULT_SETTINGS)
        out.update({k: str(d[k]) for k in SETTING_KEYS if k in d})
        return out
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(d):
    try:
        cur = load_settings()
        cur.update({k: str(d[k]) for k in SETTING_KEYS if k in d})
        os.makedirs(app_support_dir(), exist_ok=True)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def reveal(path):
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path])
    elif sys.platform == "win32":
        subprocess.run(f'explorer /select,"{os.path.normpath(path)}"')
    else:
        subprocess.run(["xdg-open", os.path.dirname(path)])


def osascript(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def ps_dialog(ps):
    """Windows: 用 PowerShell 弹原生文件对话框，返回 stdout."""
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def ps_open_file(filter_str, multi=False):
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "$f=New-Object System.Windows.Forms.OpenFileDialog;"
          f"$f.Filter='{filter_str}';"
          + ("$f.Multiselect=$true;" if multi else "") +
          "if($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK)"
          "{[Console]::Out.Write($f.FileNames -join \"`n\")}")
    return ps_dialog(ps)


def pick_folder():
    if sys.platform == "darwin":
        return osascript('POSIX path of (choose folder with prompt "选择前后对比素材文件夹")') or None
    if sys.platform == "win32":
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
              "if($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK)"
              "{[Console]::Out.Write($f.SelectedPath)}")
        return ps_dialog(ps) or None
    return None


def pick_audio():
    if sys.platform == "darwin":
        return osascript('POSIX path of (choose file with prompt "选择 BGM 音频/视频"'
                         ' of type {"public.audio", "public.movie"})') or None
    if sys.platform == "win32":
        return ps_open_file("Audio/Video|*.mp3;*.m4a;*.aac;*.wav;*.mp4;*.mov|All|*.*") or None
    return None


def pick_video():
    if sys.platform == "darwin":
        return osascript('POSIX path of (choose file with prompt "选择原始录屏"'
                         ' of type {"public.movie"})') or None
    if sys.platform == "win32":
        return ps_open_file("Videos|*.mp4;*.mov;*.m4v;*.MP4;*.MOV|All|*.*") or None
    return None


def pick_image_single(prompt="选择图片"):
    if sys.platform == "darwin":
        return osascript(f'POSIX path of (choose file with prompt "{prompt}"'
                         ' of type {"public.image"})') or None
    if sys.platform == "win32":
        return ps_open_file("Images|*.jpg;*.jpeg;*.png;*.webp;*.bmp|All|*.*") or None
    return None


def pick_images():
    if sys.platform == "win32":
        out = ps_open_file("Images|*.jpg;*.jpeg;*.png;*.webp;*.bmp|All|*.*", multi=True)
        return [l for l in out.splitlines() if l.strip()]
    if sys.platform != "darwin":
        return []
    script = ('set fl to choose file with prompt "选择图片（可多选）"'
              ' of type {"public.image"} with multiple selections allowed\n'
              'set out to ""\n'
              'repeat with f in fl\n'
              'set out to out & POSIX path of f & linefeed\n'
              'end repeat\n'
              'return out')
    out = osascript(script)
    return [l for l in out.splitlines() if l.strip()]


def default_out_path(folder):
    base = os.path.basename(os.path.normpath(folder))
    if base in ("前后对比", "素材", "图片", "images", "pairs") :
        parent = os.path.basename(os.path.dirname(os.path.normpath(folder)))
        if parent:
            base = parent
    base = base.replace(" ", "-") or "paircut"
    return os.path.join(folder, f"{base}-beforeafter-9x16.mp4")


def log(s):
    with LOCK:
        STATE["lines"].append(s)


def _record(out, mode):
    """渲染成功后登记历史；开了自动入库就同时拷进剪映素材文件夹。"""
    try:
        st = load_settings()
        jy = (st.get("jy_dir") or history.default_jy_dir()) if st.get("jy_auto") else None
        rec = history.add(out, mode=mode, jy_dir=jy)
        if rec and rec.get("jy_path"):
            log("已放入剪映素材文件夹: " + rec["jy_path"])
    except Exception:
        pass


def worker_rosie(src, params, mode, overlay, sizes):
    try:
        def f(k, dflt):
            v = str(params.get(k, "")).strip()
            try:
                return float(v)
            except ValueError:
                return dflt
        args = rosie.Args(
            f("target", None) if str(params.get("target", "")).strip() else None,
            f("paint_sec", 1.67), f("type_speed", 9.0),
            f("wait_sec", 0.8), f("last_wait", 0.8))
        out = rosie.run_job(src, args, mode=mode, overlay=overlay,
                            sizes=sizes, log=log)
        with LOCK:
            STATE.update(busy=False, done=True, ok=True, out=out)
        _record(out, "rosie")
    except render.Cancelled:
        log("已取消 ⏹")
        with LOCK:
            STATE.update(busy=False, done=True, ok=False, cancelled=True)
    except Exception:
        log("❌ 出错了:\n" + traceback.format_exc())
        with LOCK:
            STATE.update(busy=False, done=True, ok=False)


def worker_ad(opts):
    try:
        def prog(d, t):
            with LOCK:
                STATE["prog_done"], STATE["prog_total"] = d, t
        out = adcut.build(
            opts["pairs"], opts["photo"], opts["result"], opts["ending"],
            opts["bgm"], opts["out"], caption=opts["caption"],
            label_before=opts["label_before"], label_after=opts["label_after"],
            demo_speed=opts["speed"], slider=opts["slider"],
            transition=opts["transition"],
            seg_transition=opts["seg_transition"],
            seg_transition_sec=opts["seg_transition_sec"],
            log=log, progress=prog)
        with LOCK:
            STATE.update(busy=False, done=True, ok=True, out=out)
        _record(out, "ad")
    except render.Cancelled:
        log("已取消 ⏹")
        with LOCK:
            STATE.update(busy=False, done=True, ok=False, cancelled=True)
    except Exception:
        log("出错了:\n" + traceback.format_exc(limit=8))
        with LOCK:
            STATE.update(busy=False, done=True, ok=False)


def worker(pairs, opts):
    try:
        def prog(d, t):
            with LOCK:
                STATE["prog_done"], STATE["prog_total"] = d, t
        r = render.Renderer(
            pairs, opts["out"],
            caption=opts["caption"], caption_size=opts["caption_size"],
            label_before=opts["label_before"], label_after=opts["label_after"],
            scene_sec=opts["scene_sec"],
            align_mode=opts.get("align_mode", "auto"),
            align_fill=opts.get("align_fill", True),
            nudges=opts.get("nudges") or {}, reveal=opts["slider"],
            direction=opts["direction"],
            comment_user=opts["comment_user"], comment_text=opts["comment_text"],
            progress_text=opts["progress_text"],
            transition=opts["transition"], audio_path=opts["audio"] or None,
            progress=prog, log=log)
        out = r.render()
        with LOCK:
            STATE.update(busy=False, done=True, ok=True, out=out)
        _record(out, STATE.get("kind", ""))
    except render.Cancelled:
        log("已取消 ⏹")
        with LOCK:
            STATE.update(busy=False, done=True, ok=False, cancelled=True)
    except Exception:
        log("出错了:\n" + traceback.format_exc(limit=8))
        with LOCK:
            STATE.update(busy=False, done=True, ok=False)


def worker_analyze(video):
    try:
        def prog(d, t):
            with LOCK:
                STATE["prog_done"], STATE["prog_total"] = d, t
        diffs, afps, W, H, dur = screencut.analyze(video, log=log, progress=prog)
        p = screencut.plan(diffs, afps, dur)
        for (a, b), role in zip(p["segments"], p["roles"]):
            log(f"  {role:7s} {a:7.2f} – {b:7.2f}  ({b-a:.2f}s)")
        log(f"成片时长 ≈ {p['out_len']}s")
        with LOCK:
            STATE.update(busy=False, done=True, ok=True, plan=p, video=video)
    except render.Cancelled:
        log("已取消 ⏹")
        with LOCK:
            STATE.update(busy=False, done=True, ok=False, cancelled=True)
    except Exception:
        log("出错了:\n" + traceback.format_exc(limit=8))
        with LOCK:
            STATE.update(busy=False, done=True, ok=False)


def worker_screen(video, plan_d, texts, out, audio, zoom_end, zoom_photo):
    try:
        tmp = os.path.join(app_support_dir(), "captmp")
        screencut.render_screen(video, out, plan_d, texts, tmp,
                                audio_path=audio or None,
                                zoom_end=zoom_end, zoom_photo=zoom_photo or None,
                                log=log)
        with LOCK:
            STATE.update(busy=False, done=True, ok=True, out=out)
        _record(out, STATE.get("kind", ""))
    except render.Cancelled:
        log("已取消 ⏹")
        with LOCK:
            STATE.update(busy=False, done=True, ok=False, cancelled=True)
    except Exception:
        log("出错了:\n" + traceback.format_exc(limit=8))
        with LOCK:
            STATE.update(busy=False, done=True, ok=False)


def worker_demo(photo, result, caption, out, opts=None):
    try:
        def prog(d, t):
            with LOCK:
                STATE["prog_done"], STATE["prog_total"] = d, t
        o = opts or {}
        dragdemo.DragDemo(photo, out, caption=caption, result=result or None,
                          tmp_dir=os.path.join(app_support_dir(), "demotmp"),
                          motion=o.get("motion", "slide"),
                          transparent=o.get("transparent", False),
                          cursor=o.get("cursor"), sound=o.get("sound", False),
                          use_transition=o.get("use_transition", True),
                          caption_style=o.get("caption_style", "plain"),
                          caption_y=o.get("caption_y", dragdemo.CAPTION_Y),
                          caption_font_key=o.get("caption_font_key", "system"),
                          duration=o.get("duration"),
                          progress=prog, log=log).render()
        with LOCK:
            STATE.update(busy=False, done=True, ok=True, out=out)
        _record(out, STATE.get("kind", ""))
    except render.Cancelled:
        log("已取消 ⏹")
        with LOCK:
            STATE.update(busy=False, done=True, ok=False, cancelled=True)
    except Exception:
        log("出错了:\n" + traceback.format_exc(limit=8))
        with LOCK:
            STATE.update(busy=False, done=True, ok=False)


def worker_ring(opts):
    try:
        badge = ringarrow.build_badge(
            opts["photo"], opts["W"], opts["H"],
            center=(opts["cx"], opts["cy"]), radius=opts["radius"],
            crop_x=opts["crop_x"], crop_y=opts["crop_y"], log=log)
        badge.save(os.path.splitext(opts["out"])[0] + ".png")
        ringarrow.render_mov(badge, opts["out"], dur=opts["dur"],
                             pop=opts["pop"], log=log)
        with LOCK:
            STATE.update(busy=False, done=True, ok=True, out=opts["out"])
        _record(opts["out"], "ring")
    except render.Cancelled:
        log("已取消 ⏹")
        with LOCK:
            STATE.update(busy=False, done=True, ok=False, cancelled=True)
    except Exception:
        log("出错了:\n" + traceback.format_exc(limit=8))
        with LOCK:
            STATE.update(busy=False, done=True, ok=False)


PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>CutKit 视频运营工具箱</title>
<style>
/* ---- 主题 token ----
   深色是基准；[data-theme=light] 只覆盖表面/线/文字，[data-accent=*] 只覆盖强调色，
   两者正交，所以 2 种明暗 × 6 套配色 = 12 种组合，只维护两张小表。 */
:root{
  --bg:#101014; --card:#1a1a21; --sunken:#15151b; --sunken-hi:#191c21;
  --deep:#0b0b0f; --input:#131318; --btn:#2c2c36; --track:#26262f;
  --switch:#3d3d49; --knob:#ffffff; --media:#000000;
  --line:#2a2a33; --line2:#34343f; --btn-line:#3a3a46;
  --dash:#3a4049; --dash-hi:#5a626e;
  --txt:#ececf1; --txt2:#c9ced6; --dim:#9a9aa5; --faint:#6f7480;
  --shadow:rgba(0,0,0,.45); --shadow2:rgba(0,0,0,.5); --scrim:rgba(0,0,0,.62);
  --logo-dark:#2e2e33; --logo-edge:#ffd9c0;
}
:root{  /* 默认配色：橙（与 App 图标一致） */
  --acc:#ff7a45; --acc2:#ffb347; --acc-deep:#f2641a;
  --on-acc:#1b0d05; --acc-bg:#241a14; --acc-glow:rgba(255,122,69,.25);
  --badge-bg:#3a2a12;
  --danger-bg:#3a2320; --danger-hi:#4a2c27; --danger-line:#6b3a30; --danger-fg:#ffb9a6;
}
[data-accent="blue"]{--acc:#3d8bff; --acc2:#66c6ff; --acc-deep:#1f6ae0;
  --on-acc:#04101f; --acc-bg:#12203a; --acc-glow:rgba(61,139,255,.25); --badge-bg:#12263f;}
[data-accent="violet"]{--acc:#a367ff; --acc2:#d49bff; --acc-deep:#8341e6;
  --on-acc:#14041f; --acc-bg:#241436; --acc-glow:rgba(163,103,255,.25); --badge-bg:#26173a;}
[data-accent="green"]{--acc:#35c47a; --acc2:#8ee2a6; --acc-deep:#1ea15d;
  --on-acc:#04170d; --acc-bg:#12291d; --acc-glow:rgba(53,196,122,.25); --badge-bg:#143020;}
[data-accent="rose"]{--acc:#ff5c96; --acc2:#ff9dc0; --acc-deep:#e63c78;
  --on-acc:#1f0410; --acc-bg:#33141f; --acc-glow:rgba(255,92,150,.25); --badge-bg:#3a1624;}
[data-accent="mono"]{--acc:#c9ced6; --acc2:#8f959e; --acc-deep:#9aa0a8;
  --on-acc:#14141a; --acc-bg:#22242a; --acc-glow:rgba(201,206,214,.22); --badge-bg:#26282e;}

/* 浅色：只换表面/线/文字，强调色沿用上面选中的那套 */
[data-theme="light"]{
  --bg:#f4f5f7; --card:#ffffff; --sunken:#f0f1f4; --sunken-hi:#e8eaef;
  --deep:#e4e6ea; --input:#ffffff; --btn:#eceef2; --track:#dfe2e8;
  --switch:#c6cad3; --knob:#ffffff; --media:#e9ebef;
  --line:#dfe2e8; --line2:#cfd4dd; --btn-line:#d3d8e1;
  --dash:#c2c8d3; --dash-hi:#98a1b0;
  --txt:#1c1f24; --txt2:#3a3f47; --dim:#6b7280; --faint:#9aa1ac;
  --shadow:rgba(0,0,0,.14); --shadow2:rgba(0,0,0,.18); --scrim:rgba(0,0,0,.5);
  --logo-dark:#2e2e33; --logo-edge:#ffd9c0;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:15px/1.5 -apple-system,"PingFang SC",sans-serif;padding:20px 22px 40px}
h1{font-size:20px;display:flex;align-items:center;gap:10px;margin-bottom:16px}
h1 .logo{width:30px;height:30px;border-radius:9px;background:linear-gradient(144deg,var(--logo-dark) 61%,var(--logo-edge) 61%,var(--logo-edge) 63%,var(--acc) 63%,var(--acc-deep));display:inline-block}
h1 small{color:var(--dim);font-weight:400;font-size:13px}
h1 .app-version{flex-shrink:0;white-space:nowrap;font-size:11px;font-weight:500;line-height:1.5;color:var(--dim);background:var(--card);border:1px solid var(--line2);border-radius:6px;padding:2px 7px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
.card h2{font-size:14px;color:var(--dim);margin-bottom:12px;font-weight:600}
button{background:var(--btn);color:var(--txt);border:1px solid var(--btn-line);border-radius:9px;
padding:8px 14px;font-size:14px;cursor:pointer}
button:hover{background:var(--line2)}
button.primary{background:linear-gradient(120deg,var(--acc),var(--acc2));border:none;color:var(--on-acc);font-weight:700;padding:12px 22px;font-size:16px}
button.small{padding:3px 9px;font-size:12px;border-radius:7px}
button:disabled{opacity:.45;cursor:default}
input,select{background:var(--input);color:var(--txt);border:1px solid var(--line2);border-radius:8px;padding:7px 10px;font-size:14px;width:100%}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.row>button{white-space:nowrap;flex:0 0 auto}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px 14px}
.grid label{font-size:12px;color:var(--dim);display:block;margin-bottom:3px}
.folder{color:var(--dim);font-size:13px;word-break:break-all;flex:1}
#pairs{display:flex;flex-direction:column;gap:10px;margin-top:12px}
.pair{display:flex;align-items:center;gap:12px;background:var(--sunken);border:1px solid var(--line);border-radius:12px;padding:10px}
.pair img{width:84px;height:112px;object-fit:cover;border-radius:8px;background:var(--media)}
.pair .arrow{color:var(--acc);font-size:20px}
.pair .name{font-size:11px;color:var(--dim);max-width:84px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:center;margin-top:3px}
.pair .ops{margin-left:auto;display:flex;flex-direction:column;gap:6px}
.tag{font-size:11px;color:var(--dim);text-align:center}
#picker{display:none;margin-top:12px;border-top:1px solid var(--line);padding-top:12px}
.uprow{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:4px}
.upcol>label{display:block;font-weight:700;font-size:13px;margin-bottom:8px}
.upcol>label span{font-weight:400;color:var(--dim)}
.drop{position:relative;overflow:hidden;border:2px dashed var(--dash);border-radius:14px;
  min-height:200px;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:10px;cursor:pointer;background:var(--sunken);transition:border-color .15s,background .15s}
.drop:hover{border-color:var(--dash-hi);background:var(--sunken-hi)}
/* 组数多了就把框压矮，不然 3 组要滚半天 */
.compact .drop{min-height:118px;gap:5px}
.compact .drop .ico{font-size:24px}
.compact .drop .txt{font-size:12px}
.compact .upcol>label{margin-bottom:5px;font-size:12px}
.compact .uprow{gap:14px}
.drop.over{border-color:var(--acc);background:var(--acc-bg)}
button.cancel{background:var(--danger-bg);border:1px solid var(--danger-line);color:var(--danger-fg)}
button.cancel:hover{background:var(--danger-hi)}
.dz{border:1px dashed transparent;border-radius:10px;padding:6px;margin:-6px;transition:border-color .12s,background .12s}
.dz.over{border-color:var(--acc);background:var(--acc-bg)}
.dzhint{color:var(--faint);font-size:12px;margin-left:2px}
.topbar{margin-left:auto;display:flex;gap:8px;align-items:center}
.topbar .mini{width:auto;min-width:74px;padding:4px 8px;font-size:12px}
.topbar #themeBtn{padding:4px 10px;font-size:14px;line-height:1.2}
.shortcuts{margin:-6px 0 14px}
.capwrap{display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap}
.capctl{flex:1;min-width:340px}
.cappv{flex:0 0 auto}
.cappv img{width:150px;border-radius:10px;border:1px solid var(--line);background:var(--media);display:block}
.cappv figcaption{font-size:11px;color:var(--dim);text-align:center;margin-top:5px;line-height:1.4}
.numpair{display:flex;align-items:center;gap:9px}
.numpair input[type=range]{flex:1;min-width:64px}
.numpair .numbox{flex:0 0 auto;width:76px;text-align:center;padding:6px 4px}
/* 开关放在 .grid 里，得压过全局的 .grid label{display:block;font-size:12px} */
.swrow{display:flex;gap:34px;align-items:center;flex-wrap:wrap;margin-top:2px}
.swrow label.sw{display:inline-flex;align-items:center;gap:11px;margin:0;
  cursor:pointer;user-select:none;font-size:14px;color:var(--txt)}
.swrow label.sw>input{display:none}
.swrow label.sw .track{flex:0 0 auto;width:46px;height:26px;border-radius:13px;
  background:var(--switch);position:relative;transition:background .18s;
  box-shadow:inset 0 1px 2px var(--shadow)}
.swrow label.sw .track::after{content:"";position:absolute;top:3px;left:3px;
  width:20px;height:20px;border-radius:50%;background:var(--knob);transition:transform .18s;
  box-shadow:0 1px 3px var(--shadow2)}
.swrow label.sw>input:checked+.track{background:linear-gradient(120deg,var(--acc),var(--acc2))}
.swrow label.sw>input:checked+.track::after{transform:translateX(20px)}
.swrow label.sw .txt2{display:flex;flex-direction:column;gap:1px}
.swrow label.sw .lab{font-size:14px;color:var(--txt);font-weight:600;line-height:1.25}
.swrow label.sw .sub{font-size:12px;color:var(--dim);line-height:1.3}
.swrow label.sw.dim .lab{color:var(--dim);font-weight:400}
.swrow label.sw.dim{cursor:not-allowed;opacity:.45}
.ovwrap{display:flex;gap:20px;align-items:flex-start;margin-top:12px;flex-wrap:wrap}
.ovctl{flex:1;min-width:240px}
.ovctl label{font-size:12px;color:var(--dim);display:block;margin-bottom:3px}
.ovctl .num{display:flex;gap:8px;align-items:center;margin-bottom:12px}
.ovctl input[type=range]{flex:1;min-width:90px}
.ovctl input[type=text]{width:74px}
.ovprev{display:flex;gap:12px}
.ovprev figcaption{font-size:11px;color:var(--dim);text-align:center;margin-top:5px;line-height:1.4}
.ovframe{position:relative;width:124px;height:220px;border-radius:9px;overflow:hidden;
  background:var(--deep);border:1px solid var(--line)}
.ovframe .ovbg{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:.8}
.ovframe .ovart{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
  height:auto;display:block}
.ovframe .ovghost{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
  aspect-ratio:1;border:1px dashed var(--txt2);border-radius:4px;
  background:rgba(0,0,0,.55);box-shadow:0 0 0 1px var(--shadow);
  display:flex;align-items:center;justify-content:center;
  font-size:9px;color:var(--txt2);text-align:center;line-height:1.2}
.ovframe .ovempty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  font-size:11px;color:var(--faint);text-align:center;padding:0 10px}
.drop .ico{font-size:34px;line-height:1}
.drop .txt{font-weight:700;font-size:14px;color:var(--txt2)}
.drop img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:var(--media)}
.drop .clr{position:absolute;top:8px;right:8px;z-index:2;background:#000c;border:0;color:#fff;
  border-radius:8px;padding:3px 9px;cursor:pointer;font-size:14px;line-height:1.5}
.upname{font-size:11px;color:var(--dim);margin-top:6px;min-height:15px;word-break:break-all}
.thumbs{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.thumbs figure{position:relative;cursor:pointer}
.thumbs img{width:74px;height:99px;object-fit:cover;border-radius:8px;background:var(--media);
border:2px solid transparent;display:block}
.thumbs figure.sel img{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-glow)}
.thumbs figure.used img{opacity:.3}
.thumbs figcaption{position:absolute;left:0;right:0;bottom:0;font-size:10px;text-align:center;
background:var(--scrim);color:#fff;border-radius:0 0 6px 6px;padding:1px 0}
.thumbs figure.sel figcaption{background:var(--acc);color:var(--on-acc);font-weight:700}
#bar{height:10px;background:var(--track);border-radius:5px;overflow:hidden;margin:10px 0 6px;display:none}
#bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .2s}
#log{white-space:pre-wrap;font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--dim);max-height:150px;overflow-y:auto;margin-top:8px}
#doneRow{display:none;gap:10px;margin-top:10px}
.hint{color:var(--dim);font-size:12px;margin-top:8px}
#hitems{display:flex;flex-direction:column;gap:10px;margin-top:12px}
.hrec{display:flex;gap:12px;background:var(--sunken);border:1px solid var(--line);
border-radius:12px;padding:10px;align-items:center}
.hrec img{width:78px;height:104px;object-fit:cover;border-radius:8px;background:var(--media);flex:0 0 auto}
.hrec .meta{flex:1;min-width:0}
.hrec .nm{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hrec .sub{font-size:11px;color:var(--dim);margin-top:3px}
.hrec .ops{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;max-width:280px}
.badge{display:inline-block;font-size:10px;padding:1px 7px;border-radius:6px;
background:var(--btn);color:var(--dim);margin-right:6px}
.badge.mv{background:var(--badge-bg);color:var(--acc2)}
.alignbox{margin-top:10px;padding:10px;background:var(--deep);border:1px solid var(--line);
border-radius:10px;display:none}
.alignbox.on{display:flex;gap:12px;align-items:flex-start}
.alignbox img{width:150px;border-radius:8px;background:var(--media)}
.nudge{display:grid;grid-template-columns:repeat(3,34px);gap:4px}
.nudge button{padding:4px 0;font-size:12px;border-radius:6px}
.tabs{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.tabs button{border-radius:10px;padding:9px 18px;white-space:nowrap;flex:0 0 auto}
.tabs button.on{background:linear-gradient(120deg,var(--acc),var(--acc2));color:var(--on-acc);border:none;font-weight:700}
.mode{display:none}.mode.on{display:block}
#planBox{background:var(--sunken);border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:13px;color:var(--dim);margin-top:10px;display:none}
</style></head><body>
<h1><span class="logo"></span>CutKit <span class="app-version" id="appVersion">v<!--APP_VERSION--></span><small>视频运营工具箱</small>
<span class="topbar">
  <button class="small" id="themeBtn" onclick="toggleTheme()" title="日夜切换"></button>
  <select id="accentSel" class="mini" onchange="setAccent(this.value)">
    <option value="orange">橙</option><option value="blue">蓝</option>
    <option value="violet">紫</option><option value="green">绿</option>
    <option value="rose">玫红</option><option value="mono">灰</option></select>
  <select id="langSel" class="mini" onchange="setLang(this.value)"></select>
</span></h1>
<div class="row shortcuts"><span class="dzhint">快捷入口</span><!--SHORTCUTS--></div>

<div class="tabs">
<button id="tabPairs" class="on" onclick="setMode('pairs')">前后对比</button>
<button id="tabScreen" onclick="setMode('screen')">录屏步骤</button>
<button id="tabDemo" onclick="setMode('demo')">拖照片演示</button>
<button id="tabRing" onclick="setMode('ring')">圆环箭头</button>
<button id="tabRosie" onclick="setMode('rosie')">录屏自动剪辑</button>
<button id="tabAd" onclick="setMode('ad')">广告成片</button>
<button id="tabHist" onclick="setMode('hist')">历史记录</button>
</div>

<div id="modePairs" class="mode on">
<div class="card">
<h2>① 上传图片</h2>
<div class="row" style="margin:2px 0 10px">
<span style="font-size:12px;color:var(--dim)">同时准备</span>
<button class="small" onclick="setGroups(GROUPS-1)" id="grpMinus">−</button>
<span id="grpN" style="font-size:14px;font-weight:700;min-width:34px;text-align:center">2 组</span>
<button class="small" onclick="setGroups(GROUPS+1)" id="grpPlus">＋</button>
</div>
<div id="upGroups"></div>
<div class="hint">点框选文件，或直接把图拖进来。某一组两边都放好，就会自动加成一对进下面的列表并清空该组。</div>
<div class="row" style="margin-top:10px">
<button class="small" onclick="pickFolder()">或：一次导入整个文件夹…</button>
<button class="small" onclick="rePair()" id="repairBtn" style="display:none">重新自动配对</button>
<button class="small" onclick="clearPairs()" id="clearBtn" style="display:none">清空配对</button>
<div class="folder" id="folder"></div>
</div>
<div id="picker">
<div class="hint" id="pickHint" style="margin-top:0">手动配对：先点一张当 <b>Before</b>，再点另一张配成 <b>After</b>（再点一次取消选中；已配对的图会变暗）</div>
<div class="thumbs" id="thumbs"></div>
</div>
<div id="pairs"></div>
<div class="hint" id="pairHint">选择包含前后图片的文件夹，自动按画面相似度配对（同场景的前/后图会配到一起；文件名带 before/ChatGPT 等会自动识别方向）。</div>
</div>

<div class="card">
<h2>② 参数</h2>
<div class="row" style="margin-bottom:12px">
<select id="pairPreset" onchange="applyPairPreset()" style="max-width:220px">
<option value="">— 选择参数预设 —</option></select>
<input id="pairPresetName" placeholder="预设名" style="max-width:150px">
<button class="small" onclick="savePairPreset()">保存当前</button>
<button class="small" onclick="delPairPreset()">删除</button>
</div>
<div class="hint" id="pairPresetHint" style="margin:-6px 0 12px"></div>
<div class="grid">
<div style="grid-column:1/3"><label>顶部字幕（留空则不加）</label><input id="caption" placeholder="the viral game face filter"></div>
<div><label>字幕字号</label><input id="caption_size" type="number" value="55"></div>
<div><label>Before 标签</label><input id="label_before" value="Before"></div>
<div><label>After 标签</label><input id="label_after" value="After"></div>
<div><label>每段时长（秒）</label><input id="scene_sec" type="number" step="0.1" value="3.6"></div>
<div><label>转场</label><select id="transition">
<option value="spin">旋转模糊</option>
<option value="none">直切</option></select></div>
<div style="grid-column:1/4"><label>人物对齐</label>
<div class="swrow">
<label class="sw" id="swAlign">
  <input type="checkbox" id="alignOn" onchange="alignChanged()"><span class="track"></span>
  <span class="txt2"><span class="lab">自动对齐前后图人物</span>
        <span class="sub">比例不同 / 位移缩放都能对上</span></span></label>
<label class="sw" id="swFill">
  <input type="checkbox" id="alignFill" checked onchange="alignChanged()"><span class="track"></span>
  <span class="txt2"><span class="lab">裁到共同区域</span>
        <span class="sub">推荐，无边缘拉丝</span></span></label>
</div>
<div class="hint" id="alignHint" style="margin-top:8px"></div></div>
<div><label>对比展示方式</label><select id="slider" onchange="syncReveal()">
<option value="linger">滑杆·中段放慢</option>
<option value="sweep">滑杆·来回扫</option>
<option value="once">滑杆·滑到底</option>
<option value="reverse">反向污染</option>
<option value="wipe">手指擦除</option>
<option value="flicker">硬切闪频</option>
<option value="progress">进度条还原</option>
<option value="comment">评论区驱动</option>
<option value="grid">九宫格多米诺</option></select></div>
<div id="revealHint" class="hint" style="grid-column:1/-1;margin-top:-4px"></div>
<div class="rvdir" style="display:none"><label>滑动方向</label><select id="direction">
<option value="rtl">从右往左 ←</option>
<option value="ltr">从左往右 →</option></select></div>
<div class="rvcomment" style="display:none"><label>评论用户名</label>
<input id="comment_user" value="@user"></div>
<div class="rvcomment" style="display:none"><label>评论内容</label>
<input id="comment_text" value="can u remove the matcha filter from this"></div>
<div class="rvprogress" style="display:none"><label>进度条文案</label>
<input id="progress_text" value="Removing filter"></div>
<div><label>BGM（可选）</label><div class="dz" id="zBgm1"><div class="row">
<button class="small" onclick="pickAudio()">选择…</button>
<button class="small" onclick="clearAudio()">清除</button>
<span class="dzhint">或拖音频到这里</span></div></div>
<div class="hint" id="audioName" style="margin-top:4px"></div></div>
</div>
</div>

<div class="card">
<h2>③ 生成</h2>
<div class="row">
<button class="primary" id="go" onclick="run()" disabled>生成视频</button>
<button class="cancel" id="cancel_go" onclick="cancelRun()" style="display:none">取消生成</button>
<div class="hint" id="outHint"></div>
</div>
<div id="bar"><i id="fill"></i></div>
<div class="hint" id="prog"></div>
<div id="doneRow" class="row">
<button onclick="post('/reveal')">在 Finder 中显示</button>
</div>
<div id="log"></div>
</div>
</div><!-- /modePairs -->

<div id="modeScreen" class="mode">
<div class="card">
<h2>① 原始录屏</h2>
<div class="dz" id="zVideo">
<div class="row">
<button onclick="pickVideo()">选择录屏文件…</button>
<div class="folder" id="videoPath"></div>
</div>
<div class="dzhint">或把录屏文件直接拖到这里</div>
</div>
<div id="planBox"></div>
<div class="hint">选滤镜 App 的完整操作录屏，自动剪掉中间的等待时间（AI 生成排队等），压成 ~15 秒节奏。</div>
</div>

<div class="card">
<h2>② 步骤字幕</h2>
<div class="grid">
<div style="grid-column:1/4"><label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="addCaps" checked style="width:auto" onchange="capsToggled()"> 添加步骤字幕</label></div>
<div><label>滤镜名（自动填入第 2 条）</label><input id="filterName" placeholder="Game Face" oninput="filterChanged()"></div>
<div style="grid-column:2/4"><label>BGM（可选，复用左侧已选）</label><div class="dz" id="zBgm2"><div class="row">
<button class="small" onclick="pickAudio()">选择…</button>
<button class="small" onclick="clearAudio()">清除</button>
<span class="dzhint">或拖音频到这里</span>
<span class="hint" id="audioName2" style="margin-top:0"></span></div></div></div>
<div><label>字幕 1 · 选照片</label><input id="cap0" value="Select Photo"></div>
<div><label>字幕 2 · 选滤镜</label><input id="cap1" value="Select the Effect" oninput="cap1Edited=true"></div>
<div><label>字幕 3 · 等待</label><input id="cap2" value="Wait...."></div>
<div><label>字幕 4 · 结果（自动带 ❤️）</label><input id="cap3" value="Results..."></div>
<div style="grid-column:1/4"><label>结尾效果</label>
<div class="row">
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="zoomEnd" checked style="width:auto"> 结尾放大最终结果图（原图放大铺满 + 定格 1.8s，需选原图）</label>
<span class="dz" id="zZoom" style="display:inline-flex;align-items:center;gap:8px">
<button class="small" onclick="pickZoomPhoto()">选结果原图…</button>
<button class="small" onclick="clearZoomPhoto()">清除</button>
<span class="dzhint">或拖图片到这里</span></span>
<span class="hint" id="zoomPhotoName" style="margin-top:0"></span>
</div></div>
</div>
</div>

<div class="card">
<h2>③ 生成</h2>
<div class="row">
<button class="primary" id="goScreen" onclick="runScreen()" disabled>剪辑生成</button>
<button class="cancel" id="cancel_goScreen" onclick="cancelRun()" style="display:none">取消生成</button>
<div class="hint" id="outHint2"></div>
</div>
<div id="bar2" style="display:none;height:10px;background:#26262f;border-radius:5px;overflow:hidden;margin:10px 0 6px"><i id="fill2" style="display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .2s"></i></div>
<div class="hint" id="prog2"></div>
<div id="doneRow2" class="row" style="display:none;gap:10px;margin-top:10px">
<button onclick="post('/reveal')">在 Finder 中显示</button>
</div>
<div id="log2" style="white-space:pre-wrap;font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--dim);max-height:150px;overflow-y:auto;margin-top:8px"></div>
</div>
</div><!-- /modeScreen -->

<div id="modeDemo" class="mode">
<div class="card">
<h2>① 照片</h2>
<div class="dz" id="zDemoPhoto">
<div class="row">
<button onclick="pickDemoPhoto()">选择要上传演示的照片…</button>
<div class="folder" id="demoPhoto"></div>
</div>
<div class="dzhint">或把照片直接拖到这里</div>
</div>
<div class="dz" id="zDemoResult" style="margin-top:10px">
<div class="row">
<button onclick="pickDemoResult()">选 AI 结果图（可选）…</button>
<button class="small" onclick="clearDemoResult()">清除</button>
<div class="folder" id="demoResult"></div>
</div>
<div class="dzhint">或把结果图直接拖到这里</div>
</div>
<div class="hint">复刻「上传一张照片」的教程演示：虚线上传框 → 照片飞入落框回弹 → Fotor 彩色转场（进度环 + AI Generated 徽章，自带压暗）。选了结果图的话，徽章出现时会淡入成结果。</div>
</div>

<div class="card">
<h2>② 参数</h2>
<div class="capwrap">
<div class="capctl">
<div class="grid">
<div style="grid-column:1/3"><label>顶部标题</label>
<select id="demoCaptionMode" onchange="demoCaptionChanged()"><option value="none" selected>留空</option><option value="text">使用 text</option></select>
<input id="demoCaption" value="Upload Your Photo" oninput="capPreview()" disabled style="display:none"></div>
<div><label>标题样式</label>
<select id="demoCaptionStyle" onchange="capPreview()"><!--CAPTION_STYLE_OPTIONS--></select></div>
<div><label>字体</label><select id="demoFont" onchange="capPreview()"><!--FONT_OPTIONS--></select></div>
<div style="grid-column:2/4"><label>标题竖直位置 <span id="capYL">18.5</span>%</label>
<input type="range" id="demoCaptionY" min="4" max="80" step="0.5" value="18.5"
       oninput="capPreview()" style="width:100%"></div>
<div><label>动画风格</label><select id="demoMotion" onchange="demoMotionChanged()">
<option value="drag">光标拖拽（任意比例自适应）</option>
<option value="slide">飞入落框（复刻参考片）</option></select></div>
<div><label>速度预设</label><select id="demoSpeed" onchange="demoSpeedChanged()"><!--SPEED_OPTIONS--></select></div>
<div><label>成片时长（秒）</label><input id="demoDur" value="1.9"></div>
</div>
<div style="margin-top:10px"><label style="font-size:12px;color:var(--dim);display:block;margin-bottom:3px">输出与效果</label>
<div class="row">
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="demoTransparent" style="width:auto" onchange="demoTransChanged()"> 透明底 MOV（可直接盖在自己的视频上）</label>
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="demoTrans2" checked style="width:auto"> 叠 Fotor 转场</label>
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="demoCursor" checked style="width:auto"> 鼠标指针</label>
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="demoSound" checked style="width:auto"> 拖拽音效</label>
</div></div>
</div>
<figure class="cappv"><img id="capPv" alt="">
<figcaption>标题位置预览<br>（真实字体与样式）</figcaption></figure>
</div>
<div class="hint" id="demoHint2">透明 MOV 不含黑底和标题药丸，只有虚线框 + 照片卡片 + 光标，方便在剪映/CapCut 里叠到任意画面上。</div>
<div class="hint">Fotor 转场自带转场音效，随成片速度同步；「拖拽音效」单独控制拖动与落框声音。</div>
</div>

<div class="card">
<h2>③ 生成</h2>
<div class="row">
<button class="primary" id="goDemo" onclick="runDemo()" disabled>生成演示视频</button>
<button class="cancel" id="cancel_goDemo" onclick="cancelRun()" style="display:none">取消生成</button>
<div class="hint" id="outHint3"></div>
</div>
<div id="bar3" style="display:none;height:10px;background:#26262f;border-radius:5px;overflow:hidden;margin:10px 0 6px"><i id="fill3" style="display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .2s"></i></div>
<div class="hint" id="prog3"></div>
<div id="doneRow3" class="row" style="display:none;gap:10px;margin-top:10px">
<button onclick="post('/reveal')">在 Finder 中显示</button>
</div>
<div id="log3" style="white-space:pre-wrap;font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--dim);max-height:150px;overflow-y:auto;margin-top:8px"></div>
</div>
</div><!-- /modeDemo -->

<div id="modeRing" class="mode">
<div class="card">
<h2>① 原图 <span class="hint" style="margin:0">任意比例都行，自动圆形裁切</span></h2>
<div class="dz" id="zRing">
<div class="row">
<button onclick="pickRingPhoto()">选择原图…</button>
<div class="folder" id="ringPhoto"></div>
</div>
<div class="dzhint">或把图片直接拖到这里</div>
</div>
<div class="row" style="align-items:flex-start;margin-top:12px">
<img id="ringPv" style="width:180px;border-radius:10px;background:#111;display:none">
<div style="flex:1">
<div class="grid">
<div><label>画面裁切 · 横向 <span id="cxL">0.50</span></label>
<input type="range" id="crop_x" min="0" max="1" step="0.02" value="0.5" oninput="ringChanged()"></div>
<div><label>画面裁切 · 纵向 <span id="cyL">0.40</span></label>
<input type="range" id="crop_y" min="0" max="1" step="0.02" value="0.4" oninput="ringChanged()"></div>
<div><label>圆环大小 <span id="rL">0.161</span></label>
<input type="range" id="radius" min="0.08" max="0.30" step="0.005" value="0.161" oninput="ringChanged()"></div>
<div><label>位置 X <span id="pxL">0.214</span></label>
<input type="range" id="cx" min="0.05" max="0.95" step="0.01" value="0.214" oninput="ringChanged()"></div>
<div><label>位置 Y <span id="pyL">0.780</span></label>
<input type="range" id="cy" min="0.05" max="0.95" step="0.01" value="0.78" oninput="ringChanged()"></div>
<div><label>画布尺寸</label><div class="row" style="gap:6px">
<input id="ringW" type="number" value="1080" style="width:78px">
<input id="ringH" type="number" value="1920" style="width:78px"></div></div>
</div>
<div class="hint">默认位置/大小与竞品一致（左下角）。棋盘格代表透明区域。</div>
</div>
</div>
</div>

<div class="card">
<h2>② 输出</h2>
<div class="grid">
<div><label>时长（秒）</label><input id="ringDur" type="number" step="0.5" value="8"></div>
<div style="grid-column:2/4"><label>动画</label>
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="ringPop" style="width:auto"> 开头 0.45s 弹入（不勾选＝全程静止，与竞品一致）</label></div>
</div>
<div class="row" style="margin-top:14px">
<button class="primary" id="goRing" onclick="runRing()" disabled>生成透明底 MOV</button>
<button class="cancel" id="cancel_goRing" onclick="cancelRun()" style="display:none">取消生成</button>
<div class="hint" id="outHint4"></div>
</div>
<div class="hint" id="prog4"></div>
<div id="doneRow4" class="row" style="display:none;gap:10px;margin-top:10px">
<button onclick="post('/reveal')">在 Finder 中显示</button>
</div>
<div id="log4" style="white-space:pre-wrap;font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--dim);max-height:130px;overflow-y:auto;margin-top:8px"></div>
</div>
</div><!-- /modeRing -->

<div id="modeRosie" class="mode">
<div class="card">
<h2>① 录屏文件</h2>
<div class="dz" id="zRosie">
<div class="row">
<button onclick="pickRosieSrc()">选择文件…</button>
<div class="folder" id="rosieSrc"></div>
</div>
<div class="dzhint">或把录屏文件直接拖到这里</div>
</div>
<div class="hint">Fotor 录屏自动剪辑：识别「涂抹 / 打字 / 等待」三种状态，按节拍重排时长、自动裁剪，自然流版本还会把等待段换成无 logo 画面并叠加预合成动画与水印。</div>
</div>

<div class="card">
<h2>② 自然语言指令</h2>
<div class="row">
<input id="rosieNl" placeholder="例：目标 15 秒，打字快一点，等待再短些">
<button onclick="applyRosieNl()">应用</button>
</div>
<div class="hint" id="rosieNlMsg">离线解析；填了 Anthropic API key 会走 Claude 理解更灵活的说法。</div>
<div class="row" style="margin-top:8px">
<input id="rosieKey" type="password" placeholder="Anthropic API key（可选）">
</div>
</div>

<div class="card">
<h2>③ 参数</h2>
<div class="row" style="margin-bottom:10px">
<select id="rosiePreset" onchange="applyRosiePreset()" style="max-width:220px">
<option value="">— 选择预设 —</option></select>
<input id="rosiePresetName" placeholder="预设名" style="max-width:160px">
<button class="small" onclick="saveRosiePreset()">保存当前</button>
<button class="small" onclick="delRosiePreset()">删除</button>
</div>
<div class="grid">
<div><label>目标时长（秒，留空=自动节拍）</label><input id="r_target" placeholder="15.5"></div>
<div><label>涂抹段时长（秒）</label><input id="r_paint_sec" value="1.67"></div>
<div><label>打字加速倍数</label><input id="r_type_speed" value="9"></div>
<div><label>等待节拍（秒）</label><input id="r_wait_sec" value="0.8"></div>
<div><label>末尾等待节拍（秒）</label><input id="r_last_wait" value="0.8"></div>
<div><label>版本</label><select id="rosieMode" onchange="rosieModeChanged()">
<option value="natural">自然流（去 logo + 叠加素材）</option>
<option value="ad">广告版（保留 fotor logo）</option></select></div>
</div>
</div>

<div class="card" id="rosieOvCard">
<h2>④ 叠加素材<small style="color:var(--dim);font-weight:400"> — 仅自然流</small></h2>
<div class="row">
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="rosieOverlay" checked style="width:auto" onchange="rosieOvChanged()"> 叠加预合成动画 + fotor 水印</label>
</div>
<div class="hint">动画铺在除末拍外的等待节拍，末拍只放水印，都居中。</div>
<div class="row" style="margin-top:12px">
<select id="rosieOvPreset" onchange="applyRosieOvPreset()" style="max-width:220px">
<option value="">— 选择素材预设 —</option></select>
<input id="rosieOvName" placeholder="预设名" style="max-width:150px">
<button class="small" onclick="saveRosieOvPreset()">保存当前</button>
<button class="small" onclick="delRosieOvPreset()">删除</button>
</div>
<div class="hint" id="ovPresetHint" style="margin-top:6px"></div>

<div id="rosieAssets" style="margin-top:10px"></div>
<div class="hint">两个素材已随 CutKit 内置，换机器、换路径都不会丢；想用自己的那份就点「选择…」。</div>

<div class="ovwrap">
<div class="ovctl">
<label>预合成动画宽度（% 画面宽）</label>
<div class="num">
<input type="range" id="r_precomp_range" min="1" max="100" step="0.1" value="7.4" oninput="ovSlid('precomp')">
<input type="text" id="r_precomp_size" value="7.4" oninput="ovTyped()">
</div>
<label>水印宽度（% 画面宽）</label>
<div class="num">
<input type="range" id="r_watermark_range" min="1" max="100" step="0.1" value="44" oninput="ovSlid('watermark')">
<input type="text" id="r_watermark_size" value="44" oninput="ovTyped()">
</div>
<button class="small" onclick="resetRosieSizes()">恢复默认</button>
</div>
<div class="ovprev">
<figure><div class="ovframe" id="ovFrameP">
  <img class="ovbg" id="ovBgP" style="display:none">
  <img class="ovart" id="ovArtP" style="display:none">
  <div class="ovghost" id="ovGhostP" style="display:none">未选素材</div>
</div><figcaption>等待节拍<br>预合成动画</figcaption></figure>
<figure><div class="ovframe" id="ovFrameW">
  <img class="ovbg" id="ovBgW" style="display:none">
  <img class="ovart" id="ovArtW" style="display:none">
  <div class="ovghost" id="ovGhostW" style="display:none">未选素材</div>
</div><figcaption>末拍<br>fotor 水印</figcaption></figure>
</div>
</div>
<div class="hint" style="margin-top:8px">预览按 9:16 成片比例，素材大小即实际占比；底图取自当前录屏。</div>
</div>

<div class="card">
<h2>⑤ 生成</h2>
<div class="row">
<button class="primary" id="goRosie" onclick="runRosie()" disabled>开始剪辑</button>
<button class="cancel" id="cancel_goRosie" onclick="cancelRun()" style="display:none">取消生成</button>
</div>
<div class="hint" id="progRosie"></div>
<div id="doneRowRosie" class="row" style="display:none;gap:10px;margin-top:10px">
<button onclick="post('/reveal')">在 Finder 中显示</button>
</div>
<div id="logRosie" style="white-space:pre-wrap;font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--dim);max-height:220px;overflow-y:auto;margin-top:8px"></div>
</div>
</div><!-- /modeRosie -->

<div id="modeAd" class="mode">
<div class="card">
<h2>① 前后对比素材</h2>
<div class="row" style="margin:2px 0 10px">
<span style="font-size:12px;color:var(--dim)">同时准备</span>
<button class="small" onclick="setAdGroups(AD_GROUPS-1)" id="adGrpMinus">−</button>
<span id="adGrpN" style="font-size:14px;font-weight:700;min-width:34px;text-align:center">2 组</span>
<button class="small" onclick="setAdGroups(AD_GROUPS+1)" id="adGrpPlus">＋</button>
</div>
<div id="adGroups"></div>
<div class="hint">每组放一对前后图。组数越多，成片里对比段就越多、每段越短。</div>
</div>

<div class="card">
<h2>② 拖照片演示</h2>
<div class="dz" id="zAdPhoto">
<div class="row"><button onclick="pickAdPhoto()">选原图…</button>
<div class="folder" id="adPhoto"></div></div>
<div class="dzhint">或把原图直接拖到这里</div>
</div>
<div class="dz" id="zAdResult" style="margin-top:10px">
<div class="row"><button onclick="pickAdResult()">选 AI 结果图…</button>
<button class="small" onclick="clearAdResult()">清除</button>
<div class="folder" id="adResult"></div></div>
<div class="dzhint">或把结果图直接拖到这里 —— 它也是结尾定格用的那张</div>
</div>
</div>

<div class="card">
<h2>③ 文案与素材</h2>
<div class="grid">
<div style="grid-column:1/3"><label>顶部字幕（自动折行，支持 emoji）</label>
<input id="ad_caption" value="POV: one selfie later, you debuted as an idol 💀✨"></div>
<div><label>演示段加速</label><select id="ad_speed">
<option value="2">2×（推荐）</option><option value="1.5">1.5×</option>
<option value="1">原速</option><option value="3">3×</option></select></div>
<div><label>Before 标签</label><input id="ad_label_before" value="Before"></div>
<div><label>After 标签</label><input id="ad_label_after" value="After"></div>
<div><label>对比展示方式</label><select id="ad_slider">
<option value="linger">滑杆·中段放慢</option>
<option value="sweep">滑杆·来回扫</option><option value="once">滑杆·滑到底</option>
<option value="wipe">手指擦除</option></select></div>
<div><label>对比片段之间</label><select id="ad_transition">
<option value="spin">旋转模糊</option><option value="none">直切</option></select></div>
<div><label>段落之间的转场</label><select id="ad_seg_trans" onchange="adSegTransChanged()"><!--SEG_TRANSITION_OPTIONS--></select></div>
<div><label>转场时长 <span id="ad_seg_secL">0.50</span> 秒</label>
<input type="range" id="ad_seg_sec" min="0.1" max="1.5" step="0.05" value="0.5"
       oninput="adSegTransChanged()" style="width:100%"></div>
</div>
<div class="hint" id="adSegHint" style="margin-top:6px"></div>
<div class="dz" id="zAdEnding" style="margin-top:12px">
<div class="row"><button class="small" onclick="pickAdEnding()">选品牌结尾 MOV…</button>
<button class="small" onclick="clearAdEnding()">清除</button>
<div class="folder" id="adEnding"></div></div>
<div class="dzhint">带 alpha 的结尾素材，会叠在结果定格上擦入；留空则不加结尾</div>
</div>
<div class="dz" id="zAdBgm" style="margin-top:10px">
<div class="row"><button class="small" onclick="pickAdBgm()">选 BGM…</button>
<button class="small" onclick="clearAdBgm()">清除</button>
<div class="folder" id="adBgm"></div></div>
<div class="dzhint">必填 —— 成片总长跟着它走，切点会吸附到它的节拍</div>
</div>
</div>

<div class="card">
<h2>④ 生成</h2>
<div class="row">
<button class="primary" id="goAd" onclick="runAd()" disabled>生成广告成片</button>
<button class="cancel" id="cancel_goAd" onclick="cancelRun()" style="display:none">取消生成</button>
<div class="hint" id="outHintAd"></div>
</div>
<div id="barAd" style="display:none;height:10px;background:#26262f;border-radius:5px;overflow:hidden;margin:10px 0 6px"><i id="fillAd" style="display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .2s"></i></div>
<div class="hint" id="progAd"></div>
<div id="doneRowAd" class="row" style="display:none;gap:10px;margin-top:10px">
<button onclick="post('/reveal')">在 Finder 中显示</button>
</div>
<div id="logAd" style="white-space:pre-wrap;font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--dim);max-height:190px;overflow-y:auto;margin-top:8px"></div>
</div>
</div><!-- /modeAd -->

<div id="modeHist" class="mode">
<div class="card">
<h2>① 剪映素材夹</h2>
<div class="row">
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="jyAuto" style="width:auto" onchange="saveJy()"> 生成后自动放入剪映素材文件夹</label>
<button class="small" onclick="pickJyDir()">换文件夹…</button>
<button class="small" onclick="hAct(null,'open_jy_dir')">打开文件夹</button>
</div>
<div class="folder" id="jyDir" style="margin-top:8px"></div>
<div class="hint" id="jyHint"></div>
</div>

<div class="card">
<h2>② 生成历史</h2>
<div class="row">
<button onclick="loadHist()">刷新</button>
<div class="hint" id="histCount" style="margin-top:0"></div>
</div>
<div id="hitems"></div>
</div>
</div><!-- /modeHist -->

<script>
const $=id=>document.getElementById(id);
let FOLDER="", PAIRS=[], AUDIO="";
async function post(u,d){const r=await fetch(u,{method:'POST',body:JSON.stringify(d||{})});return r.json();}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;');}
function base(p){return p.split('/').pop();}
const NAMES={};                       // 上传件：真实路径 → 原始文件名
function disp(p){return NAMES[p]||base(p);}
function thumb(p){return '/thumb?p='+encodeURIComponent(p);}
function renderPairs(){
  const el=$('pairs'); el.innerHTML='';
  PAIRS.forEach((pr,i)=>{
    const d=document.createElement('div'); d.className='pair';
    d.innerHTML=`<div><img src="${thumb(pr[0])}"><div class="name" title="${esc(pr[0])}">${esc(disp(pr[0]))}</div><div class="tag">Before</div></div>
    <div class="arrow">→</div>
    <div><img src="${thumb(pr[1])}"><div class="name" title="${esc(pr[1])}">${esc(disp(pr[1]))}</div><div class="tag">After</div></div>
    <div class="ops">
      <button class="small" onclick="swapPair(${i})">⇄ 交换前后</button>
      <button class="small alignbtn" onclick="alignPanel(${i})">◎ 对齐</button>
      <div class="row" style="gap:6px">
        <button class="small" onclick="movePair(${i},-1)">↑</button>
        <button class="small" onclick="movePair(${i},1)">↓</button>
        <button class="small" onclick="delPair(${i})">✕</button>
      </div>
    </div>`;
    const ab=document.createElement('div'); ab.className='alignbox'; ab.id='ab'+i;
    d.appendChild(ab);
    el.appendChild(d);
  });
  const alignOn=$('alignOn')?$('alignOn').checked:true;
  document.querySelectorAll('.alignbtn').forEach(b=>{b.disabled=!alignOn;});
  $('go').disabled = PAIRS.length===0 || BUSY;
  $('pairHint').textContent = PAIRS.length ?
    `${PAIRS.length} 对素材 · 成片约 ${(PAIRS.length*parseFloat($('scene_sec').value||3.6)).toFixed(1)} 秒` :
    '还没有配对：把 Before / After 各放一张到上面的两个框里。';
  renderThumbs();
}
let NUDGE={};
function alignChanged(){
  const on=$('alignOn').checked;
  // 关掉对齐后，「裁到共同区域」和逐对微调都没有意义了，一并置灰
  $('alignFill').disabled=!on;
  $('swFill').classList.toggle('dim',!on);   // 只灰掉从属项；开关自己要始终可点回来
  document.querySelectorAll('.alignbtn').forEach(b=>{b.disabled=!on;});
  $('alignHint').textContent = tr(on
    ? '生成时会先把两张图的人物对到一起；逐对可点「◎ 对齐」查看并手动微调。'
    : '已关闭：两张图按原样直接用，不做任何位移缩放，逐对微调也不生效。');
  if(!on){document.querySelectorAll('.alignbox.on').forEach(b=>b.classList.remove('on'));return;}
  document.querySelectorAll('.alignbox.on').forEach(b=>{
    const i=parseInt(b.id.slice(2),10); loadAlign(i);});
}
function alignPanel(i){
  const b=$('ab'+i);
  if(b.classList.contains('on')){b.classList.remove('on');return;}
  b.classList.add('on'); loadAlign(i);
}
function nudgeOf(i){return NUDGE[i]||{scale:1,dx:0,dy:0};}
async function loadAlign(i){
  const b=$('ab'+i); if(!b)return;
  b.innerHTML='<div class="hint" style="margin-top:0">对齐分析中…</div>';
  const n=nudgeOf(i);
  const r=await post('/align_preview',{before:PAIRS[i][0],after:PAIRS[i][1],
    off:!$('alignOn').checked, fill:$('alignFill').checked,
    nudge:($('alignOn').checked?n:null)});
  if(r.error){b.innerHTML='<div class="hint" style="margin-top:0">'+esc(r.error)+'</div>';return;}
  const f=r.info, meth={features:'特征匹配','features+ecc':'特征+ECC',ecc:'ECC 梯度',phase:'相位相关',none:'无需调整',off:'已关闭'}[f.method]||f.method;
  b.innerHTML=`<img src="/thumb?p=${encodeURIComponent(r.img)}&v=${Date.now()}">
    <div style="flex:1">
      <div class="sub" style="font-size:12px;color:var(--dim)">
        梳齿预览：人物边缘接不上就是还没对齐<br>
        方式 <b>${esc(meth)}</b> · 缩放 ${(f.scale||1).toFixed(3)} ·
        位移 ${Math.round(f.dx||0)},${Math.round(f.dy||0)}px
        ${f.gain?`· 吻合度 +${f.gain.toFixed(3)}`:''}
      </div>
      <div class="row" style="margin-top:8px;align-items:flex-start;gap:14px">
        <div class="nudge">
          <span></span><button onclick="nud(${i},0,-8)">↑</button><span></span>
          <button onclick="nud(${i},-8,0)">←</button><button onclick="nud(${i},0,0,1)">⟲</button><button onclick="nud(${i},8,0)">→</button>
          <span></span><button onclick="nud(${i},0,8)">↓</button><span></span>
        </div>
        <div class="nudge" style="grid-template-columns:repeat(2,48px)">
          <button onclick="nud(${i},0,0,0,0.98)">缩小</button>
          <button onclick="nud(${i},0,0,0,1.02)">放大</button>
        </div>
      </div>
      <div class="hint" style="margin-top:6px">手动微调：${(nudgeOf(i).dx)||0},${(nudgeOf(i).dy)||0}px · ×${(nudgeOf(i).scale||1).toFixed(2)}</div>
    </div>`;
}
function nud(i,dx,dy,reset,ds){
  const n=nudgeOf(i);
  if(reset){NUDGE[i]={scale:1,dx:0,dy:0};}
  else NUDGE[i]={scale:(n.scale||1)*(ds||1),dx:(n.dx||0)+dx,dy:(n.dy||0)+dy};
  loadAlign(i);
}
function swapPair(i){PAIRS[i]=[PAIRS[i][1],PAIRS[i][0]];renderPairs();}
function delPair(i){PAIRS.splice(i,1);renderPairs();}
function movePair(i,d){const j=i+d;if(j<0||j>=PAIRS.length)return;
  [PAIRS[i],PAIRS[j]]=[PAIRS[j],PAIRS[i]];renderPairs();}
let IMAGES=[], SELIDX=-1;
function renderThumbs(){
  const el=$('thumbs'); if(!el)return;
  $('picker').style.display=IMAGES.length?'block':'none';
  const used=new Set(); PAIRS.forEach(p=>{used.add(p[0]);used.add(p[1]);});
  el.innerHTML='';
  IMAGES.forEach((p,i)=>{
    const fig=document.createElement('figure');
    fig.className = i===SELIDX ? 'sel' : (used.has(p)?'used':'');
    fig.title=base(p);
    fig.innerHTML=`<img src="${thumb(p)}">`+
      (i===SELIDX?'<figcaption>Before</figcaption>':'');
    fig.onclick=()=>clickThumb(i);
    el.appendChild(fig);
  });
}
function clickThumb(i){
  if(SELIDX===i){SELIDX=-1;renderThumbs();return;}
  if(SELIDX<0){SELIDX=i;renderThumbs();return;}
  PAIRS.push([IMAGES[SELIDX],IMAGES[i]]); SELIDX=-1; renderPairs();
}
function clearPairs(){PAIRS=[];SELIDX=-1;renderPairs();}

const MAX_GROUPS=6;
let GROUPS=2, SLOTS=[{B:'',A:''},{B:'',A:''}];
function slotId(g,k){return 'drop'+k+g;}
function renderGroups(){
  const el=$('upGroups'); el.innerHTML='';
  for(let g=0;g<GROUPS;g++){
    const tag=GROUPS>1?` <span>${tp('· 第 {} 组', g+1)}</span>`:'';
    const d=document.createElement('div');
    d.className='uprow'; if(g)d.style.marginTop='16px';
    d.innerHTML=`
      <div class="upcol"><label>Before Image${tag}</label>
        <div class="drop" id="dropB${g}" onclick="pickInto(${g},'B')"></div>
        <div class="upname" id="nameB${g}"></div></div>
      <div class="upcol"><label>After Image${tag}</label>
        <div class="drop" id="dropA${g}" onclick="pickInto(${g},'A')"></div>
        <div class="upname" id="nameA${g}"></div></div>`;
    el.appendChild(d);
  }
  el.classList.toggle('compact', GROUPS>=3);
  for(let g=0;g<GROUPS;g++){wireDrop(g,'B');wireDrop(g,'A');paintSlot(g,'B');paintSlot(g,'A');}
  $('grpN').textContent=GROUPS+' 组';
  $('grpMinus').disabled=GROUPS<=1;
  $('grpPlus').disabled=GROUPS>=MAX_GROUPS;
}
function setGroups(n){
  n=Math.max(1,Math.min(MAX_GROUPS,n|0));
  if(n===GROUPS)return;
  // 缩减时别把已经拖进去的图弄丢：成对的直接进列表，半边的挪去留下的空位
  const next=[];
  for(let g=0;g<n;g++)next.push(SLOTS[g]||{B:'',A:''});
  if(n<GROUPS){
    const orphans=[];
    for(let g=n;g<GROUPS;g++){
      const s=SLOTS[g]; if(!s)continue;
      if(s.B&&s.A)PAIRS.push([s.B,s.A]);
      else{ if(s.B)orphans.push(['B',s.B]); if(s.A)orphans.push(['A',s.A]); }
    }
    for(const [k,path] of orphans){
      const slot=next.find(x=>!x[k]);
      if(slot)slot[k]=path;              // 放不下就只能留在列表外，但至少不静默丢
    }
    renderPairs();
  }
  SLOTS=next; GROUPS=n;
  renderGroups();
  post('/save_settings',{pair_groups:String(GROUPS)});
}
function paintSlot(g,k){
  const el=$(slotId(g,k)); if(!el)return;
  const p=(SLOTS[g]||{})[k]||'';
  el.innerHTML = p
    ? `<img src="${thumb(p)}"><button class="clr" title="${tr('移除')}" onclick="event.stopPropagation();clearSlot(${g},'${k}')">✕</button>`
    : `<div class="ico">🖼️</div><div class="txt">Upload photo or drag &amp; drop</div>`;
  const nm=$('name'+k+g); if(nm)nm.textContent = p ? disp(p) : '';
}
function clearSlot(g,k){SLOTS[g][k]='';paintSlot(g,k);}
function tryPair(g){
  const s=SLOTS[g]; if(!s||!s.B||!s.A)return;
  PAIRS.push([s.B,s.A]); SLOTS[g]={B:'',A:''};
  paintSlot(g,'B'); paintSlot(g,'A'); renderPairs();
}
function firstEmptySlot(){        // 拖到面板空白处时，找第一个空位
  for(let g=0;g<GROUPS;g++){
    if(!SLOTS[g].B)return[g,'B'];
    if(!SLOTS[g].A)return[g,'A'];
  }
  return null;
}
async function pickInto(g,k){
  if(SLOTS[g][k])return;
  const r=await post('/pick_image',{prompt:k==='B'?'选择 Before 图（变身前）':'选择 After 图（变身后）'});
  if(r.path){SLOTS[g][k]=r.path;paintSlot(g,k);tryPair(g);}
}
async function uploadFile(g,k,file){
  if(!/^image\//.test(file.type||'')&&!/\.(jpe?g|png|webp|bmp|tiff?|heic)$/i.test(file.name||'')){
    $('prog').textContent='只能放图片文件';return;}
  const el=$(slotId(g,k));
  el.innerHTML='<div class="txt">读取中…</div>';
  const buf=await file.arrayBuffer(), b=new Uint8Array(buf);
  let bin='';
  for(let i=0;i<b.length;i+=0x8000)bin+=String.fromCharCode.apply(null,b.subarray(i,i+0x8000));
  const r=await post('/upload',{name:file.name,data:btoa(bin)});
  if(r.path){
    if(r.name)NAMES[r.path]=r.name;
    SLOTS[g][k]=r.path; paintSlot(g,k);
    if(!FOLDER&&r.out)$('outHint').textContent='输出: '+r.out;
    tryPair(g);
  }else{ $('prog').textContent=r.error||'上传失败'; paintSlot(g,k); }
}
// ---------- 通用拖拽上传 ----------
// 图片走内存即可，录屏可能上 GB，一律用 XHR 流式发给 /upload_stream。
const DZ_PAT={image:/\.(jpe?g|png|webp|bmp|tiff?|heic)$/i,
              video:/\.(mp4|mov|m4v|avi|mkv|webm)$/i,
              audio:/\.(mp3|m4a|aac|wav|aiff?|flac|ogg)$/i};
const DZ_LABEL={image:'图片',video:'视频',audio:'音频'};

function dzKindOk(file,kind){
  const t=file.type||'';
  if(kind==='image'&&/^image\//.test(t))return true;
  if(kind==='video'&&/^video\//.test(t))return true;
  if(kind==='audio'&&/^audio\//.test(t))return true;
  return DZ_PAT[kind].test(file.name||'');
}

function uploadStream(file,kind,onProg){
  return new Promise(resolve=>{
    const x=new XMLHttpRequest();
    x.open('POST','/upload_stream');
    x.setRequestHeader('X-Filename',encodeURIComponent(file.name||'file'));
    x.setRequestHeader('X-Kind',kind);
    x.upload.onprogress=e=>{if(e.lengthComputable&&onProg)onProg(e.loaded,e.total);};
    x.onload=()=>{try{resolve(JSON.parse(x.responseText));}catch(_){resolve({error:'上传失败'});}};
    x.onerror=()=>resolve({error:'上传失败'});
    x.send(file);
  });
}

// 每个投放点：接受的类型、落定后怎么用、状态提示往哪写
const DZ={
  zVideo:      {kind:'video', status:'prog2', apply:p=>setVideo(p)},
  zZoom:       {kind:'image', status:'prog2',
                apply:p=>{ZOOMPHOTO=p;$('zoomPhotoName').textContent=base(p);}},
  zDemoPhoto:  {kind:'image', status:'prog3',
                apply:p=>{DPHOTO=p;$('demoPhoto').textContent=base(p);$('goDemo').disabled=false;}},
  zDemoResult: {kind:'image', status:'prog3',
                apply:p=>{DRESULT=p;$('demoResult').textContent=base(p);}},
  zRing:       {kind:'image', status:'prog4',
                apply:p=>{RPHOTO=p;$('ringPhoto').textContent=base(p);
                          $('goRing').disabled=false;ringPreview();}},
  zRosie:      {kind:'video', status:'progRosie',
                apply:p=>{RSRC=p;$('rosieSrc').textContent=p;$('goRosie').disabled=false;ovArt();}},
  zBgm1:       {kind:'audio', status:'prog',
                apply:p=>{AUDIO=p;$('audioName').textContent=base(p);
                          $('audioName2').textContent=base(p);}},
  zAdPhoto:    {kind:'image', status:'progAd',
                apply:p=>{ADPHOTO=p;$('adPhoto').textContent=base(p);adSyncReady();}},
  zAdResult:   {kind:'image', status:'progAd',
                apply:p=>{ADRESULT=p;$('adResult').textContent=base(p);adSyncReady();}},
  zAdEnding:   {kind:'video', status:'progAd',
                apply:p=>{ADENDING=p;$('adEnding').textContent=base(p);}},
  zAdBgm:      {kind:'audio', status:'progAd',
                apply:p=>{ADBGM=p;$('adBgm').textContent=base(p);adSyncReady();}},
  zBgm2:       {kind:'audio', status:'prog2',
                apply:p=>{AUDIO=p;$('audioName').textContent=base(p);
                          $('audioName2').textContent=base(p);}},
};

function dzSay(id,msg){const el=$(id); if(el)el.textContent=msg;}
function mb(n){return n>=1073741824?(n/1073741824).toFixed(2)+' GB':(n/1048576).toFixed(1)+' MB';}

async function dzTake(zid,file){
  const spec=DZ[zid]; if(!spec||!file)return;
  if(!dzKindOk(file,spec.kind)){
    dzSay(spec.status,tp('这里只能放{}文件', tr(DZ_LABEL[spec.kind]))); return;
  }
  const big=file.size>8*1048576;
  dzSay(spec.status, big?tp('读取中… {}% ({})', 0, mb(file.size)):'读取中…');
  const r=await uploadStream(file,spec.kind,(a,b)=>{
    if(big)dzSay(spec.status,tp('读取中… {}% ({})', Math.round(100*a/b), mb(b)));
  });
  if(r.error){dzSay(spec.status,r.error);return;}
  if(r.name)NAMES[r.path]=r.name;
  dzSay(spec.status,'');
  spec.apply(r.path);
}

function wireZone(zid){
  const el=$(zid); if(!el)return;
  el.addEventListener('dragover',e=>{e.preventDefault();e.stopPropagation();el.classList.add('over');});
  el.addEventListener('dragleave',e=>{e.stopPropagation();el.classList.remove('over');});
  el.addEventListener('drop',e=>{
    e.preventDefault();e.stopPropagation();el.classList.remove('over');
    const f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];
    if(f)dzTake(zid,f);
  });
}

// 兜底：拖到该模式面板的任何空白处，也送到这个模式的主输入
const MODE_FALLBACK={screen:'zVideo',demo:'zDemoPhoto',ring:'zRing',rosie:'zRosie',ad:'zAdPhoto'};
function wireModeFallback(modeId,mode){
  const el=$(modeId); if(!el)return;
  el.addEventListener('dragover',e=>{e.preventDefault();});
  el.addEventListener('drop',e=>{
    e.preventDefault();
    const f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];
    if(!f)return;
    if(mode==='pairs'){                     // 前后对比：填第一个空位
      const slot=firstEmptySlot();
      if(slot)uploadFile(slot[0],slot[1],f);
      else $('prog').textContent='所有组都放满了，先加一组或等它自动配对';
      return;
    }
    const z=MODE_FALLBACK[mode]; if(z)dzTake(z,f);
  });
}

// ---------- 取消生成 ----------
async function cancelRun(){
  document.querySelectorAll('button.cancel').forEach(b=>{b.disabled=true;b.textContent=tr('取消中…');});
  const r=await post('/cancel');
  if(!r.ok){
    document.querySelectorAll('button.cancel').forEach(b=>{b.disabled=false;b.textContent=tr('取消生成');});
  }
}

function wireDrop(g,k){
  const el=$(slotId(g,k)); if(!el)return;
  el.addEventListener('dragover',e=>{e.preventDefault();e.stopPropagation();el.classList.add('over');});
  el.addEventListener('dragleave',e=>{e.stopPropagation();el.classList.remove('over');});
  el.addEventListener('drop',e=>{
    e.preventDefault(); e.stopPropagation(); el.classList.remove('over');
    const f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];
    if(f)uploadFile(g,k,f);
  });
}
async function pickFolder(){
  const r=await post('/pick_folder');
  if(!r.path)return;
  FOLDER=r.path; PAIRS=r.pairs; IMAGES=r.images||[]; SELIDX=-1;
  $('folder').textContent=FOLDER;
  $('repairBtn').style.display=$('clearBtn').style.display='';
  $('outHint').textContent='输出: '+r.out;
  renderPairs();
}
async function rePair(){
  if(!FOLDER)return;
  const r=await post('/repair',{folder:FOLDER});
  PAIRS=r.pairs; IMAGES=r.images||IMAGES; SELIDX=-1; renderPairs();
}
async function addPair(){
  const b=await post('/pick_image',{prompt:'选择 Before 图（变身前）'});
  if(!b.path)return;
  const a=await post('/pick_image',{prompt:'选择 After 图（变身后）'});
  if(!a.path)return;
  PAIRS.push([b.path,a.path]); renderPairs();
}
async function pickAudio(){
  const r=await post('/pick_audio');
  if(r.path){AUDIO=r.path;$('audioName').textContent=base(AUDIO);$('audioName2').textContent=base(AUDIO);}
}
function clearAudio(){AUDIO="";$('audioName').textContent="";$('audioName2').textContent="";}
let MODE='pairs', PLAN=null, VIDEO='', cap1Edited=false;
let RSRC='', RPRESETS={}, RASSETS={}, RDEFAULTS={}, RWAIT=null;
const RKEYS=['target','paint_sec','type_speed','wait_sec','last_wait'];
function rParams(){const o={};RKEYS.forEach(k=>o[k]=$('r_'+k).value);return o;}
function rSizes(){return {precomp:parseFloat($('r_precomp_size').value),
                          watermark:parseFloat($('r_watermark_size').value)};}
async function rosieInit(){
  const r=await post('/rosie_init');
  RPRESETS=r.presets||{}; RASSETS=r.assets||{}; RDEFAULTS=r.defaults||{};
  const sel=$('rosiePreset'); sel.innerHTML='<option value="">— 选择预设 —</option>';
  Object.keys(RPRESETS).forEach(n=>{const o=document.createElement('option');
    o.value=n;o.textContent=n;sel.appendChild(o);});
  if(r.sizes){$('r_precomp_size').value=r.sizes.precomp;$('r_watermark_size').value=r.sizes.watermark;}
  syncNumPairs();
  renderRosieAssets();
  ROV=r.ov||{presets:{},last:''};
  renderOvPresets();
  if(ROV.last&&ROV.presets[ROV.last])await applyRosieOvPreset();   // 默认套用最近保存的
  else ovArt();
}
function renderRosieAssets(){
  const el=$('rosieAssets'); el.innerHTML='';
  Object.entries(RASSETS).forEach(([k,v])=>{
    const d=document.createElement('div'); d.className='row'; d.style.marginTop='6px';
    // 内置那份永远在，标出来：看到「内置」就知道这台机器没有素材也照样能出片
    const where=v.path
      ? esc(base(v.path))+(v.builtin?' · '+tr('内置'):'')
      : tr('未找到，将跳过叠加');
    d.innerHTML=`<span style="font-size:13px;min-width:96px">${esc(v.label)}</span>
      <span class="hint" style="margin-top:0;flex:1">${where}</span>
      <button class="small" onclick="pickRosieAsset('${k}')">${tr('选择…')}</button>`
      + (v.builtin ? '' : `<button class="small" title="${tr('改回随 CutKit 内置的那份素材')}"`
                        + ` onclick="resetRosieAsset('${k}')">${tr('用内置')}</button>`);
    el.appendChild(d);
  });
}
async function pickRosieAsset(kind){
  const r=await post('/rosie_asset_pick',{kind:kind});
  RASSETS=r.assets||RASSETS; renderRosieAssets(); ovArt();
}
async function resetRosieAsset(kind){
  const r=await post('/rosie_asset_reset',{kind:kind});
  RASSETS=r.assets||RASSETS; renderRosieAssets(); ovArt();
}
function resetRosieSizes(){
  $('r_precomp_size').value=RDEFAULTS.precomp; $('r_watermark_size').value=RDEFAULTS.watermark;
  ovSync(); syncNumPairs(); post('/rosie_sizes',rSizes());
}

// ---------- 数值：滑杆与输入框并存 ----------
// 一个通用绑定：原来是输入框的补一根滑杆，原来是滑杆的补一个输入框，两边互相跟随。
// 拖滑杆时会在原元素上补发 input 事件，所以各模式原有的 oninput 逻辑照常触发。
const NUMPAIRS=[];
function numPair(id, min, max, step, valueSpan){
  const el=$(id); if(!el||el.dataset.paired)return; el.dataset.paired='1';
  const isRange = el.type==='range';
  const wrap=document.createElement('div'); wrap.className='numpair';
  el.parentNode.insertBefore(wrap, el);
  const mate=document.createElement('input');
  const clamp=v=>Math.max(min,Math.min(max,v));
  const fire=()=>el.dispatchEvent(new Event('input',{bubbles:true}));
  if(isRange){
    mate.type='text'; mate.className='numbox'; mate.value=el.value;
    wrap.appendChild(el); wrap.appendChild(mate);
    el.addEventListener('input',()=>{mate.value=el.value;});
    mate.addEventListener('input',()=>{
      const v=parseFloat(mate.value);
      if(isFinite(v)){el.value=clamp(v); fire();}
    });
    if(valueSpan&&$(valueSpan))$(valueSpan).style.display='none';   // 数字已在输入框里，别重复显示
  }else{
    mate.type='range'; mate.min=min; mate.max=max; mate.step=step;
    mate.value=isFinite(parseFloat(el.value))?clamp(parseFloat(el.value)):min;
    el.classList.add('numbox'); el.type='text';
    wrap.appendChild(mate); wrap.appendChild(el);
    el.addEventListener('input',()=>{
      const v=parseFloat(el.value); if(isFinite(v))mate.value=clamp(v);
    });
    mate.addEventListener('input',()=>{el.value=mate.value; fire();});
  }
  NUMPAIRS.push({el, mate, isRange, clamp});
}
// 预设套用等场景是直接改 .value，不会触发 input —— 事后调一次让滑杆跟上
function syncNumPairs(){
  NUMPAIRS.forEach(({el, mate, isRange, clamp})=>{
    if(isRange){ mate.value = el.value; }
    else{ const v=parseFloat(el.value); if(isFinite(v))mate.value=clamp(v); }
  });
}
function wireNumPairs(){
  // 前后对比
  numPair('caption_size', 20, 120, 1);
  numPair('scene_sec', 0.5, 15, 0.1);
  // 圆环箭头：本来只有滑杆，补上输入框（顺手把重复显示数值的小标签藏掉）
  numPair('crop_x', 0, 1, 0.02, 'cxL');
  numPair('crop_y', 0, 1, 0.02, 'cyL');
  numPair('radius', 0.08, 0.30, 0.005, 'rL');
  numPair('cx', 0.05, 0.95, 0.01, 'pxL');
  numPair('cy', 0.05, 0.95, 0.01, 'pyL');
  numPair('ringDur', 1, 30, 0.5);
  // 录屏自动剪辑
  numPair('r_paint_sec', 0.2, 6, 0.01);
  numPair('r_type_speed', 1, 20, 0.5);
  numPair('r_wait_sec', 0.1, 4, 0.05);
  numPair('r_last_wait', 0.1, 4, 0.05);
  // 广告成片
  numPair('ad_seg_sec', 0.1, 1.5, 0.05, 'ad_seg_secL');
}

// ---------- 拖照片演示：标题预览与速度 ----------
let capTimer=null;
function demoCaptionText(){
  return $('demoCaptionMode').value==='text' && !$('demoTransparent').checked ? $('demoCaption').value : '';
}
function demoCaptionChanged(){
  const text=$('demoCaptionMode').value==='text';
  const enabled=text && !$('demoTransparent').checked;
  $('demoCaption').style.display=text?'':'none';
  ['demoCaption','demoCaptionStyle','demoFont','demoCaptionY'].forEach(k=>$(k).disabled=!enabled);
  capPreview();
}
function capPreview(){
  const y=parseFloat($('demoCaptionY').value)/100;
  $('capYL').textContent=parseFloat($('demoCaptionY').value).toFixed(1);
  clearTimeout(capTimer);
  capTimer=setTimeout(()=>{                       // 拖滑杆时别每一帧都请求
    const q=new URLSearchParams({y:y, text:demoCaptionText(),
      style:$('demoCaptionStyle').value, font:$('demoFont').value, t:Date.now()});
    $('capPv').src='/caption_preview?'+q.toString();
  },120);
}
const SPEED_SEC={};
function demoSpeedChanged(){
  const v=SPEED_SEC[$('demoSpeed').value];
  if(v)$('demoDur').value=v;
}

// ---------- 主题与语言 ----------
let THEME='dark', ACCENT='orange', LANG='zh', I18N={}, LANGS=[['zh','中文']];
function applyTheme(){
  document.documentElement.setAttribute('data-theme', THEME);
  document.documentElement.setAttribute('data-accent', ACCENT);
  const b=$('themeBtn'); if(b)b.textContent = THEME==='dark' ? '🌙' : '☀️';
  const a=$('accentSel'); if(a)a.value=ACCENT;
}
function toggleTheme(){
  THEME = THEME==='dark' ? 'light' : 'dark';
  applyTheme(); post('/save_settings',{theme:THEME});
}
function setAccent(v){ ACCENT=v; applyTheme(); post('/save_settings',{accent:v}); }

// 词表以中文原文为键，所以页面不用打任何标记：遍历文本节点直接换。
// 换之前先把原文记在节点上，切回中文时能还原。
function walkText(node, fn){
  for(let n=node.firstChild; n; n=n.nextSibling){
    if(n.nodeType===3) fn(n);
    else if(n.nodeType===1 && n.tagName!=='SCRIPT' && n.tagName!=='STYLE') walkText(n, fn);
  }
}
function tr(zh){
  if(LANG==='zh')return zh;
  const d=I18N[LANG]||{}; return d[zh]!==undefined ? d[zh] : zh;
}
function applyLang(){
  walkText(document.body, n=>{
    if(n.__zh===undefined){
      if(!/[\u4e00-\u9fff]/.test(n.nodeValue))return;   // 只记有中文的节点
      n.__zh=n.nodeValue;
    }
    const raw=n.__zh, t=raw.trim();
    if(!t)return;
    n.nodeValue = raw.replace(t, tr(t));
  });
  document.querySelectorAll('[placeholder]').forEach(e=>{
    if(e.__zhph===undefined){
      if(!/[\u4e00-\u9fff]/.test(e.placeholder))return;
      e.__zhph=e.placeholder;
    }
    e.placeholder=tr(e.__zhph);
  });
  document.documentElement.lang = LANG;
  const sel=$('langSel'); if(sel)sel.value=LANG;
}
function tp(zh, ...args){          // 带 {} 占位符的模板，供 JS 拼出来的串用
  let i=0;
  return tr(zh).replace(/\{\}/g, ()=> args[i++]);
}
function setLang(v){ LANG=v; applyLang(); post('/save_settings',{lang:v}); }
async function openShortcut(i){ await post('/open_shortcut',{i:i}); }
async function renameShortcut(i){
  const btns=[...document.querySelectorAll('.shortcuts button')];
  const cur=btns[i]?btns[i].textContent.trim():'';
  const name=prompt(tr('给这个入口起个名字（留空恢复默认）'), cur);
  if(name===null)return;
  const r=await post('/rename_shortcut',{i:i,name:name});
  if(r.shortcuts)r.shortcuts.forEach(([lab],k)=>{ if(btns[k])btns[k].textContent=lab; });
}

// 配对列表、预设下拉这些是 JS 现生成的，靠观察器补翻。
// applyLang 只改 nodeValue（characterData），这里只观察 childList，不会自我触发。
let langPending=false;
function watchLang(){
  new MutationObserver(()=>{
    if(langPending)return;
    langPending=true;
    // 用 setTimeout 不用 rAF —— 窗口被切到后台时 rAF 不触发，
    // 那时正好是渲染完把结果写进列表的时刻，翻译就会漏掉。
    setTimeout(()=>{ langPending=false; applyLang(); }, 0);
  }).observe(document.body,{childList:true,subtree:true});
}

// ---------- 广告成片 ----------
let AD_GROUPS=2, AD_SLOTS=[{B:'',A:''},{B:'',A:''}];
let ADPHOTO='', ADRESULT='', ADENDING='', ADBGM='';
function adSlotId(g,k){return 'adDrop'+k+g;}
function renderAdGroups(){
  const el=$('adGroups'); if(!el)return;
  el.innerHTML='';
  for(let g=0;g<AD_GROUPS;g++){
    const d=document.createElement('div');
    d.className='uprow'; if(g)d.style.marginTop='14px';
    d.innerHTML=`
      <div class="upcol"><label>Before <span>${tp('· 第 {} 组', g+1)}</span></label>
        <div class="drop" id="adDropB${g}" onclick="pickAdInto(${g},'B')"></div>
        <div class="upname" id="adNameB${g}"></div></div>
      <div class="upcol"><label>After <span>${tp('· 第 {} 组', g+1)}</span></label>
        <div class="drop" id="adDropA${g}" onclick="pickAdInto(${g},'A')"></div>
        <div class="upname" id="adNameA${g}"></div></div>`;
    el.appendChild(d);
  }
  el.classList.add('compact');       // 这个模式里始终用紧凑框，卡片本来就长
  for(let g=0;g<AD_GROUPS;g++){wireAdDrop(g,'B');wireAdDrop(g,'A');paintAdSlot(g,'B');paintAdSlot(g,'A');}
  $('adGrpN').textContent=AD_GROUPS+' 组';
  $('adGrpMinus').disabled=AD_GROUPS<=1; $('adGrpPlus').disabled=AD_GROUPS>=6;
  adSyncReady();
}
function setAdGroups(n){
  n=Math.max(1,Math.min(6,n|0)); if(n===AD_GROUPS)return;
  const next=[];
  for(let g=0;g<n;g++)next.push(AD_SLOTS[g]||{B:'',A:''});
  AD_SLOTS=next; AD_GROUPS=n; renderAdGroups();
}
function paintAdSlot(g,k){
  const el=$(adSlotId(g,k)); if(!el)return;
  const p=(AD_SLOTS[g]||{})[k]||'';
  el.innerHTML = p
    ? `<img src="${thumb(p)}"><button class="clr" title="${tr('移除')}" onclick="event.stopPropagation();clearAdSlot(${g},'${k}')">✕</button>`
    : `<div class="ico">🖼️</div><div class="txt">Upload photo or drag &amp; drop</div>`;
  const nm=$('adName'+k+g); if(nm)nm.textContent = p ? disp(p) : '';
}
function clearAdSlot(g,k){AD_SLOTS[g][k]='';paintAdSlot(g,k);adSyncReady();}
async function pickAdInto(g,k){
  if(AD_SLOTS[g][k])return;
  const r=await post('/pick_image',{prompt:k==='B'?'选择 Before 图':'选择 After 图'});
  if(r.path){AD_SLOTS[g][k]=r.path;paintAdSlot(g,k);adSyncReady();}
}
async function adUpload(g,k,file){
  const el=$(adSlotId(g,k)); el.innerHTML='<div class="txt">读取中…</div>';
  const r=await uploadStream(file,'image');
  if(r.path){AD_SLOTS[g][k]=r.path;paintAdSlot(g,k);adSyncReady();}
  else{$('progAd').textContent=r.error||'上传失败';paintAdSlot(g,k);}
}
function wireAdDrop(g,k){
  const el=$(adSlotId(g,k)); if(!el)return;
  el.addEventListener('dragover',e=>{e.preventDefault();e.stopPropagation();el.classList.add('over');});
  el.addEventListener('dragleave',e=>{e.stopPropagation();el.classList.remove('over');});
  el.addEventListener('drop',e=>{e.preventDefault();e.stopPropagation();el.classList.remove('over');
    const f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];
    if(f)adUpload(g,k,f);});
}
function adSegTransChanged(){
  const on = $('ad_seg_trans').value !== 'none';
  $('ad_seg_sec').disabled = !on;
  $('ad_seg_secL').textContent = parseFloat($('ad_seg_sec').value).toFixed(2);
  $('adSegHint').textContent = tr(on
    ? '转场加在「对比段 → 演示」和「演示 → 定格」两处；成片总长会自动补偿，仍然贴合 BGM。'
    : '段落之间直接硬切。');
}
function adPairs(){return AD_SLOTS.filter(s=>s.B&&s.A).map(s=>[s.B,s.A]);}
function adSyncReady(){
  const ok = adPairs().length>0 && ADPHOTO && ADBGM;
  const b=$('goAd'); if(b)b.disabled=!ok;
  const h=$('progAd');
  if(h&&!BUSY){
    const miss=[];
    if(!adPairs().length)miss.push('至少一组完整的前后图');
    if(!ADPHOTO)miss.push('演示原图');
    if(!ADBGM)miss.push('BGM');
    h.textContent = miss.length ? tr('还缺：')+miss.map(tr).join(tr('、')) : '';
  }
}
async function pickAdPhoto(){const r=await post('/pick_image',{prompt:'选择演示原图'});
  if(r.path){ADPHOTO=r.path;$('adPhoto').textContent=base(r.path);adSyncReady();}}
async function pickAdResult(){const r=await post('/pick_image',{prompt:'选择 AI 结果图'});
  if(r.path){ADRESULT=r.path;$('adResult').textContent=base(r.path);adSyncReady();}}
function clearAdResult(){ADRESULT='';$('adResult').textContent='';adSyncReady();}
async function pickAdEnding(){const r=await post('/pick_video');
  if(r.path){ADENDING=r.path;$('adEnding').textContent=base(r.path);}}
function clearAdEnding(){ADENDING='';$('adEnding').textContent='';}
async function pickAdBgm(){const r=await post('/pick_audio');
  if(r.path){ADBGM=r.path;$('adBgm').textContent=base(r.path);adSyncReady();}}
function clearAdBgm(){ADBGM='';$('adBgm').textContent='';adSyncReady();}
async function runAd(){
  const pairs=adPairs(); if(!pairs.length||!ADPHOTO||!ADBGM)return;
  const r=await post('/run_ad',{pairs:pairs,photo:ADPHOTO,result:ADRESULT,
    ending:ADENDING,bgm:ADBGM,caption:$('ad_caption').value,
    speed:$('ad_speed').value,slider:$('ad_slider').value,
    transition:$('ad_transition').value,
    seg_transition:$('ad_seg_trans').value,
    seg_transition_sec:$('ad_seg_sec').value,
    label_before:$('ad_label_before').value,label_after:$('ad_label_after').value});
  if(r.error){$('progAd').textContent=r.error;return;}
  BUSY=true;$('goAd').disabled=true;$('doneRowAd').style.display='none';
  $('logAd').textContent='';$('progAd').textContent='组装中…';
  $('barAd').style.display='';$('fillAd').style.width='0%';
  $('outHintAd').textContent='输出: '+(r.out||'');
  POLL=setInterval(pollAd,500);
}
async function pollAd(){
  const s=await post('/status');
  if(s.total>0){$('fillAd').style.width=(100*s.done_n/s.total).toFixed(1)+'%';
    $('progAd').textContent=tp('渲染中 {}/{} 帧', s.done_n, s.total);}
  $('logAd').textContent=s.lines.join('\n');
  $('logAd').scrollTop=$('logAd').scrollHeight;
  if(s.done){
    clearInterval(POLL);BUSY=false;$('goAd').disabled=false;
    if(s.ok){$('progAd').textContent='完成 ✅  '+s.out;$('fillAd').style.width='100%';
      $('doneRowAd').style.display='flex';}
    else{$('progAd').textContent=s.cancelled?'已取消 ⏹':'失败 ❌（见下方日志）';}
  }
}

// ---------- 前后对比：参数预设 ----------
// 存的是 ② 参数卡片的一整套值，包含两个对齐开关。
const PP_TEXT=['caption','caption_size','label_before','label_after','scene_sec',
               'transition','slider','direction','comment_user','comment_text','progress_text'];
let PPRESETS={presets:{},last:''};
function ppData(){
  const o={}; PP_TEXT.forEach(k=>o[k]=$(k).value);
  o.align_on   = $('alignOn').checked   ? '1' : '';
  o.align_fill = $('alignFill').checked ? '1' : '';
  return o;
}
function renderPairPresets(){
  const sel=$('pairPreset'), names=Object.keys(PPRESETS.presets||{});
  const keep=sel.value;
  sel.innerHTML='<option value="">— 选择参数预设 —</option>'+
    names.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');
  if(names.includes(keep))sel.value=keep;
  $('pairPresetHint').textContent = names.length
    ? '' : tr('把常用的一套参数（含对齐开关）存下来，换片型时一键切回。');
}
function applyPairPreset(){
  const n=$('pairPreset').value; if(!n)return;
  const p=PPRESETS.presets[n]; if(!p)return;
  PP_TEXT.forEach(k=>{if(p[k]!==undefined)$(k).value=p[k];});
  if(p.align_on!==undefined)$('alignOn').checked=!!p.align_on;
  if(p.align_fill!==undefined)$('alignFill').checked=!!p.align_fill;
  alignChanged(); syncReveal(); syncNumPairs();
  $('pairPresetHint').textContent=tp('已套用：{}', n);
}
async function savePairPreset(){
  const n=$('pairPresetName').value.trim();
  if(!n){$('pairPresetHint').textContent='请先填预设名';return;}
  const r=await post('/pair_preset_save',{name:n,params:ppData()});
  if(r.error){$('pairPresetHint').textContent=r.error;return;}
  PPRESETS=r; $('pairPresetName').value='';
  renderPairPresets(); $('pairPreset').value=n;
  $('pairPresetHint').textContent=tp('已保存：{}', n);
}
async function delPairPreset(){
  const n=$('pairPreset').value; if(!n)return;
  PPRESETS=await post('/pair_preset_delete',{name:n});
  $('pairPreset').value=''; renderPairPresets();
  $('pairPresetHint').textContent=tp('已删除：{}', n);
}

// ---------- 叠加素材：尺寸可视化 ----------
// 光看百分比数字判断不了大小，这里按 9:16 成片比例画出素材的真实占比。
let ROV={presets:{},last:''};
function ovPct(id,dflt){
  const v=parseFloat($(id).value);
  return (isFinite(v)&&v>=1&&v<=100)?v:dflt;
}
function ovSync(){
  const pc=ovPct('r_precomp_size',RDEFAULTS.precomp||7.4);
  const wm=ovPct('r_watermark_size',RDEFAULTS.watermark||44);
  $('r_precomp_range').value=pc; $('r_watermark_range').value=wm;
  [['P',pc],['W',wm]].forEach(([sfx,pct])=>{
    const art=$('ovArt'+sfx), gh=$('ovGhost'+sfx);
    art.style.width=pct+'%';           // % 是相对预览框宽 —— 与「% 画面宽」同义
    gh.style.width=pct+'%';
  });
}
function ovSlid(kind){
  $(kind==='precomp'?'r_precomp_size':'r_watermark_size').value =
    $(kind==='precomp'?'r_precomp_range':'r_watermark_range').value;
  ovSync(); ovPersist();
}
function ovTyped(){ ovSync(); ovPersist(); }
let ovSaveTimer=null;
function ovPersist(){                  // 输入时别每敲一下就写盘
  clearTimeout(ovSaveTimer);
  ovSaveTimer=setTimeout(()=>post('/rosie_sizes',rSizes()),400);
}
function ovArt(){
  [['precomp','P'],['watermark','W']].forEach(([k,sfx])=>{
    const has=RASSETS[k]&&RASSETS[k].path;
    const art=$('ovArt'+sfx), gh=$('ovGhost'+sfx);
    if(has){ art.src='/asset_frame?kind='+k+'&t='+Date.now();
             art.style.display=''; gh.style.display='none'; }
    else   { art.removeAttribute('src'); art.style.display='none'; gh.style.display=''; }
  });
  ['P','W'].forEach(sfx=>{
    const bg=$('ovBg'+sfx);
    if(RSRC){ bg.src='/src_frame?p='+encodeURIComponent(RSRC)+'&t='+Date.now(); bg.style.display=''; }
    else    { bg.removeAttribute('src'); bg.style.display='none'; }
  });
  ovSync();
}

// ---------- 叠加素材预设 ----------
function ovData(){
  return {precomp:(RASSETS.precomp||{}).path||'',
          watermark:(RASSETS.watermark||{}).path||'',
          precomp_size:ovPct('r_precomp_size',RDEFAULTS.precomp||7.4),
          watermark_size:ovPct('r_watermark_size',RDEFAULTS.watermark||44)};
}
function renderOvPresets(){
  const sel=$('rosieOvPreset'), names=Object.keys(ROV.presets||{});
  sel.innerHTML='<option value="">— 选择素材预设 —</option>'+
    names.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');
  if(ROV.last&&ROV.presets[ROV.last])sel.value=ROV.last;
  $('ovPresetHint').textContent = names.length
    ? (ROV.last?`当前：${ROV.last}（最近保存的一份，已自动选中）`:'')
    : tr('把常用的素材 + 大小存成预设，下次打开自动选最近保存的那份。');
}
async function applyRosieOvPreset(){
  const n=$('rosieOvPreset').value; if(!n)return;
  const r=await post('/rosie_ov_apply',{name:n});
  if(r.error){$('progRosie').textContent=r.error;return;}
  RASSETS=r.assets||RASSETS;
  if(r.sizes){$('r_precomp_size').value=r.sizes.precomp;$('r_watermark_size').value=r.sizes.watermark;}
  if(r.ov)ROV=r.ov;
  syncNumPairs();
  renderRosieAssets(); ovArt();
  $('ovPresetHint').textContent=tp('已套用预设：{}', n);
}
async function saveRosieOvPreset(){
  const n=$('rosieOvName').value.trim();
  if(!n){$('ovPresetHint').textContent='请先填预设名';return;}
  const r=await post('/rosie_ov_save',{name:n,data:ovData()});
  if(r.error){$('ovPresetHint').textContent=r.error;return;}
  ROV=r.ov||ROV; RASSETS=r.assets||RASSETS;
  $('rosieOvName').value=''; renderOvPresets(); $('rosieOvPreset').value=n;
  $('ovPresetHint').textContent=tp('已保存预设：{}（下次打开默认选它）', n);
}
async function delRosieOvPreset(){
  const n=$('rosieOvPreset').value; if(!n)return;
  const r=await post('/rosie_ov_delete',{name:n});
  ROV=r.ov||ROV; renderOvPresets();
  $('ovPresetHint').textContent=tp('已删除预设：{}', n);
}
function applyRosiePreset(){
  const p=RPRESETS[$('rosiePreset').value]; if(!p)return;
  RKEYS.forEach(k=>{if(p[k]!==undefined)$('r_'+k).value=p[k];});
  syncNumPairs();
}
async function saveRosiePreset(){
  const n=$('rosiePresetName').value.trim(); if(!n){alert('请填预设名');return;}
  const r=await post('/rosie_preset_save',{name:n,params:rParams()});
  RPRESETS=r.presets||RPRESETS; await rosieInit(); $('rosiePreset').value=n;
}
async function delRosiePreset(){
  const n=$('rosiePreset').value; if(!n)return;
  const r=await post('/rosie_preset_delete',{name:n});
  RPRESETS=r.presets||RPRESETS; await rosieInit();
}
async function pickRosieSrc(){
  const r=await post('/pick_video');
  if(r.path){RSRC=r.path;$('rosieSrc').textContent=RSRC;$('goRosie').disabled=false;ovArt();}
}
function rosieModeChanged(){
  const nat=$('rosieMode').value==='natural';
  $('rosieOvCard').style.opacity=nat?'1':'.45';
  ['rosieOverlay','r_precomp_size','r_watermark_size'].forEach(k=>$(k).disabled=!nat);
}
function rosieOvChanged(){
  const on=$('rosieOverlay').checked && $('rosieMode').value==='natural';
  ['r_precomp_size','r_watermark_size'].forEach(k=>$(k).disabled=!on);
}
async function applyRosieNl(){
  const t=$('rosieNl').value.trim(); if(!t)return;
  const cur={}; RKEYS.forEach(k=>cur[k]=$('r_'+k).value);
  cur.last_wait_sec=cur.last_wait;
  const r=await post('/rosie_nl',{text:t,cur:cur,api_key:$('rosieKey').value.trim()});
  if(r.error){$('rosieNlMsg').textContent=r.error;return;}
  const p=r.params;
  $('r_target').value = (p.target===null||p.target===undefined)?'':p.target;
  $('r_paint_sec').value=p.paint_sec; $('r_type_speed').value=p.type_speed;
  $('r_wait_sec').value=p.wait_sec; $('r_last_wait').value=p.last_wait_sec;
  $('rosieNlMsg').textContent=`${r.summary}（${r.engine==='llm'?'Claude 解析':'离线解析'}）`;
}
async function runRosie(){
  if(!RSRC)return;
  if(RWAIT){clearInterval(RWAIT);RWAIT=null;}
  const r=await post('/rosie_run',{src:RSRC,params:rParams(),mode:$('rosieMode').value,
    overlay:$('rosieOverlay').checked,sizes:rSizes()});
  if(r.error){rosieBlocked(r.error);return;}
  BUSY=true;$('goRosie').disabled=true;$('doneRowRosie').style.display='none';
  $('logRosie').textContent='';$('progRosie').textContent='处理中…（首次分析约 1-2 分钟）';
  POLL=setInterval(pollRosie,600);
}
// 没能开工时别把一行字晾在那儿冒充进度：说清楚是没开始，
// 并盯着后台，等它空下来就把提示换成「可以再点一次」。
function rosieBlocked(msg){
  $('progRosie').textContent='⚠️ 没开始：'+msg;
  $('goRosie').disabled=false;
  if(RWAIT)clearInterval(RWAIT);
  RWAIT=setInterval(async()=>{
    const s=await post('/status');
    if(!s.busy){clearInterval(RWAIT);RWAIT=null;
      $('progRosie').textContent='后台任务已结束，可以再点一次「开始剪辑」';}
  },800);
}
async function pollRosie(){
  const s=await post('/status');
  $('logRosie').textContent=s.lines.join('\n');
  $('logRosie').scrollTop=$('logRosie').scrollHeight;
  if(s.done){clearInterval(POLL);BUSY=false;$('goRosie').disabled=false;
    if(s.ok){$('progRosie').textContent='完成 ✅  '+s.out;$('doneRowRosie').style.display='flex';}
    else{$('progRosie').textContent=s.cancelled?'已取消 ⏹':'失败 ❌（见下方日志）';}}
}
let HREC=[];
function fmtSize(b){return b>1048576?(b/1048576).toFixed(1)+' MB':(b/1024).toFixed(0)+' KB';}
function fmtTime(ts){const d=new Date(ts*1000);const p=n=>String(n).padStart(2,'0');
  return `${d.getMonth()+1}月${d.getDate()}日 ${p(d.getHours())}:${p(d.getMinutes())}`;}
async function loadHist(){
  const r=await post('/history');
  HREC=r.items||[];
  $('jyAuto').checked=!!r.jy_auto;
  $('jyDir').textContent=r.jy_dir;
  $('jyHint').innerHTML = r.jy_app
    ? '剪映的草稿素材表是加密的、且它没有文件打开接口，所以没法由程序直接写进素材库。这里改成把成片放进上面这个文件夹 —— 剪映里 <b>导入 → 素材</b> 定位一次之后，之后每次文件框都停在这，一步就能选。'
    : '没检测到剪映专业版；仍可把成片收进这个文件夹备用。';
  $('histCount').textContent = HREC.length ? tp('共 {} 条', HREC.length) : '还没有生成记录';
  const el=$('hitems'); el.innerHTML='';
  HREC.forEach(r=>{
    const d=document.createElement('div'); d.className='hrec';
    const lab=(r.labels||{})[r.mode]||({render:'前后对比',screen:'录屏步骤',demo:'拖照片演示',ring:'圆环箭头'})[r.mode]||'其他';
    d.innerHTML=`<img src="/thumb?p=${encodeURIComponent(r.thumb||'')}">
      <div class="meta">
        <div class="nm" title="${esc(r.path)}">${esc(r.name)}</div>
        <div class="sub"><span class="badge">${lab}</span>${r.transparent?'<span class="badge mv">透明 MOV</span>':''}
          ${r.dur}s · ${r.w}×${r.h} · ${fmtSize(r.size)} · ${fmtTime(r.ts)}</div>
        <div class="sub">${r.jy_path?'✅ 已在剪映素材夹':''}</div>
      </div>
      <div class="ops">
        <button class="small" onclick="hAct('${r.id}','play')">播放</button>
        <button class="small" onclick="hAct('${r.id}','reveal')">在 Finder 显示</button>
        <button class="small" onclick="hAct('${r.id}','to_jy')">放入剪映素材夹</button>
        <button class="small" onclick="hDel('${r.id}')">删除记录</button>
      </div>`;
    el.appendChild(d);
  });
}
async function hAct(id,act){
  const r=await post('/history_action',{id:id,act:act});
  if(r.error){alert(r.error);return;}
  if(act==='to_jy'||act==='delete')loadHist();
}
async function hDel(id){
  const withFile=confirm('同时删除磁盘上的文件？\n\n确定 = 连文件一起删\n取消 = 只删这条记录');
  await post('/history_action',{id:id,act:'delete',with_file:withFile});
  loadHist();
}
async function saveJy(){
  await post('/history_action',{act:'set_jy',auto:$('jyAuto').checked,dir:$('jyDir').textContent});
}
async function pickJyDir(){
  const r=await post('/history_action',{act:'pick_dir'});
  if(r.path){$('jyDir').textContent=r.path;saveJy();}
}
function setMode(m){
  MODE=m;
  if(m==='hist')loadHist();
  if(m==='rosie'&&!RDEFAULTS.precomp){rosieInit();rosieModeChanged();}
  for(const k of ['pairs','screen','demo','ring']){
    document.getElementById('mode'+k[0].toUpperCase()+k.slice(1)).classList.toggle('on',m===k);
    document.getElementById('tab'+k[0].toUpperCase()+k.slice(1)).classList.toggle('on',m===k);
  }
  document.getElementById('modeHist').classList.toggle('on',m==='hist');
  document.getElementById('tabHist').classList.toggle('on',m==='hist');
  document.getElementById('modeRosie').classList.toggle('on',m==='rosie');
  document.getElementById('tabRosie').classList.toggle('on',m==='rosie');
  document.getElementById('modeAd').classList.toggle('on',m==='ad');
  document.getElementById('tabAd').classList.toggle('on',m==='ad');
  if(m==='ad'){renderAdGroups();adSegTransChanged();}
}
const REVEAL_HINT={
  linger:'滑到底，但两头快、中间慢 —— 时间花在画面中段（人脸所在），边缘一带而过；前后各停一拍。',
  sweep:'滑杆左右来回扫动 —— 前三条用的就是这个。',
  once:'滑杆只滑一次并滑到底，从整张 Before 推到整张 After。',
  reverse:'先给完整成图（前 1 秒屏幕上是"奖励"不是绿色），绿色再从中心蔓延吞掉它，最后扫描线把画面还原。专治开头掉人。',
  wipe:'橡皮擦沿蛇形路径把滤镜抹掉，擦过的地方不再回来，画面里有"有人在操作"的实感。',
  flicker:'不做任何渐变，Before/After 直接硬切交替，前 1.7 秒闪 7 次后定格成图。视觉指纹和滑杆差最远。',
  progress:'扫描线自上而下推进 + 底部处理进度条 0→100%，"处理中"制造未完成感，冲完播。',
  comment:'开头浮一张评论卡（用你评论区里的真实提问），淡出后再还原 —— 明示"评论区发图我帮你修"，直接造评论。',
  grid:'多张图同屏，逐格从 Before 翻成 After。9 对=3×3，6 对=3×2，4 对=2×2，2/3 对=竖排。一次修一堆，冲收藏。'};
function syncReveal(){
  const v=$('slider').value;
  $('revealHint').textContent=tr(REVEAL_HINT[v]||'');
  for(const c of ['rvcomment','rvprogress'])
    document.querySelectorAll('.'+c).forEach(e=>{
      e.style.display=(c==='rv'+v)?'':'none';});
  const hasDir=['linger','sweep','once','comment','wipe','grid'].includes(v);
  document.querySelectorAll('.rvdir').forEach(e=>{e.style.display=hasDir?'':'none';});
}
let RPHOTO='', RTIMER=null;
function ringParams(){
  return {photo:RPHOTO,W:+$('ringW').value,H:+$('ringH').value,
    cx:+$('cx').value,cy:+$('cy').value,radius:+$('radius').value,
    crop_x:+$('crop_x').value,crop_y:+$('crop_y').value,
    dur:+$('ringDur').value,pop:$('ringPop').checked};
}
async function ringPreview(){
  if(!RPHOTO)return;
  const r=await post('/ring_preview',ringParams());
  if(r.png){$('ringPv').src=r.png;$('ringPv').style.display='';}
}
function ringChanged(){
  $('cxL').textContent=$('crop_x').value; $('cyL').textContent=$('crop_y').value;
  $('rL').textContent=$('radius').value; $('pxL').textContent=$('cx').value;
  $('pyL').textContent=$('cy').value;
  clearTimeout(RTIMER); RTIMER=setTimeout(ringPreview,180);
}
async function pickRingPhoto(){
  const r=await post('/pick_image',{prompt:'选择要放进圆环的原图'});
  if(!r.path)return;
  RPHOTO=r.path; $('ringPhoto').textContent=base(RPHOTO);
  $('goRing').disabled=false; ringPreview();
}
async function runRing(){
  if(!RPHOTO)return;
  const r=await post('/run_ring',ringParams());
  if(r.error){$('prog4').textContent=r.error;return;}
  BUSY=true;$('goRing').disabled=true;$('doneRow4').style.display='none';
  $('log4').textContent='';$('prog4').textContent='编码中…';
  $('outHint4').textContent='输出: '+(r.out||'');
  POLL=setInterval(pollRing,500);
}
async function pollRing(){
  const s=await post('/status');
  $('log4').textContent=s.lines.join('\n');
  $('log4').scrollTop=$('log4').scrollHeight;
  if(s.done){
    clearInterval(POLL);BUSY=false;$('goRing').disabled=false;
    if(s.ok){$('prog4').textContent='完成 ✅  '+s.out;$('doneRow4').style.display='flex';}
    else{$('prog4').textContent=s.cancelled?'已取消 ⏹':'失败 ❌（见下方日志）';}
  }
}
let DPHOTO='', DRESULT='';
async function pickDemoPhoto(){
  const r=await post('/pick_image',{prompt:'选择要上传演示的照片'});
  if(!r.path)return;
  DPHOTO=r.path; $('demoPhoto').textContent=base(DPHOTO); $('goDemo').disabled=false;
}
function demoMotionChanged(){
  // 拖拽风格默认带光标
  $('demoCursor').checked = $('demoMotion').value==='drag';
}
function demoTransChanged(){
  const tp=$('demoTransparent').checked;
  demoCaptionChanged();
  $('demoHint2').style.color = tp ? 'var(--acc)' : 'var(--dim)';
}
async function pickDemoResult(){
  const r=await post('/pick_image',{prompt:'选择 AI 结果图'});
  if(r.path){DRESULT=r.path;$('demoResult').textContent=base(DRESULT);}
}
function clearDemoResult(){DRESULT='';$('demoResult').textContent='';}
async function runDemo(){
  if(!DPHOTO)return;
  const r=await post('/run_demo',{photo:DPHOTO,result:DRESULT,caption:demoCaptionText(),caption_mode:$('demoCaptionMode').value,
    motion:$('demoMotion').value, transparent:$('demoTransparent').checked,
    cursor:$('demoCursor').checked, sound:$('demoSound').checked,
    caption_style:$('demoCaptionStyle').value,
    caption_y:$('demoCaptionY').value, caption_font:$('demoFont').value,
    duration:$('demoDur').value,
    use_transition:$('demoTrans2').checked});
  if(r.error){$('prog3').textContent=r.error;return;}
  BUSY=true;$('goDemo').disabled=true;
  $('bar3').style.display='';$('fill3').style.width='0%';
  $('doneRow3').style.display='none';$('log3').textContent='';
  $('outHint3').textContent='输出: '+(r.out||'');
  POLL=setInterval(pollDemo,400);
}
async function pollDemo(){
  const s=await post('/status');
  if(s.total>0){$('fill3').style.width=(100*s.done_n/s.total).toFixed(1)+'%';
    $('prog3').textContent=tp('渲染中 {}/{} 帧', s.done_n, s.total);}
  $('log3').textContent=s.lines.join('\n');
  $('log3').scrollTop=$('log3').scrollHeight;
  if(s.done){
    clearInterval(POLL);BUSY=false;$('goDemo').disabled=false;
    if(s.ok){$('prog3').textContent='完成 ✅  '+s.out;$('fill3').style.width='100%';
      $('doneRow3').style.display='flex';}
    else{$('prog3').textContent=s.cancelled?'已取消 ⏹':'失败 ❌（见下方日志）';}
  }
}
function filterChanged(){
  if(cap1Edited)return;
  const v=$('filterName').value.trim();
  $('cap1').value=v?('Select the \u201c'+v+' Effect\u201d'):'Select the Effect';
}
async function pickVideo(){
  const r=await post('/pick_video',{analyze:true});
  if(r.error){$('prog2').textContent=r.error;return;}
  if(!r.path)return;
  setVideo(r.path);
}
function setVideo(path){
  VIDEO=path;PLAN=null;
  $('videoPath').textContent=VIDEO;
  $('goScreen').disabled=true;
  $('planBox').style.display='';$('planBox').textContent='分析中…（检测操作与等待段）';
  $('doneRow2').style.display='none';$('log2').textContent='';
  $('bar2').style.display='';$('fill2').style.width='0%';
  BUSY=true;POLL=setInterval(pollScreen,500);
}
let ZOOMPHOTO='';
async function pickZoomPhoto(){
  const r=await post('/pick_zoom_photo');
  if(r.path){ZOOMPHOTO=r.path;$('zoomPhotoName').textContent=base(ZOOMPHOTO);}
}
function clearZoomPhoto(){ZOOMPHOTO='';$('zoomPhotoName').textContent='';}
function capsToggled(){
  const on=$('addCaps').checked;
  for(const k of ['filterName','cap0','cap1','cap2','cap3'])$(k).disabled=!on;
}
async function runScreen(){
  if(!PLAN)return;
  if($('zoomEnd').checked&&!ZOOMPHOTO){$('prog2').textContent='请先选择结果原图（结尾放大用）';return;}
  const texts=$('addCaps').checked?
    [$('cap0').value,$('cap1').value,$('cap2').value,$('cap3').value]:['','','',''];
  const r=await post('/run_screen',{texts:texts,audio:AUDIO,
    zoom_end:$('zoomEnd').checked,zoom_photo:ZOOMPHOTO});
  if(r.error){$('prog2').textContent=r.error;return;}
  BUSY=true;$('goScreen').disabled=true;
  $('bar2').style.display='';$('fill2').style.width='0%';
  $('doneRow2').style.display='none';$('log2').textContent='';
  $('prog2').textContent='剪辑编码中…（约 1-2 分钟）';
  POLL=setInterval(pollScreen,500);
}
async function pollScreen(){
  const s=await post('/status');
  if(s.kind==='analyze'&&s.total>0){
    $('fill2').style.width=(100*s.done_n/s.total).toFixed(1)+'%';
    $('prog2').textContent=tp('分析中 {}/{}', s.done_n, s.total);
  }
  if(s.kind==='screen'){$('fill2').style.width=s.done?'100%':'60%';}
  $('log2').textContent=s.lines.join('\n');
  $('log2').scrollTop=$('log2').scrollHeight;
  if(s.done){
    clearInterval(POLL);BUSY=false;
    if(s.kind==='analyze'){
      if(s.ok&&s.plan){PLAN=s.plan;
        $('planBox').textContent=`已分析：保留 ${s.plan.n} 段，成片约 ${s.plan.out_len}s（详见下方日志）`;
        $('goScreen').disabled=false;$('prog2').textContent='';
        $('fill2').style.width='100%';
      }else{$('planBox').textContent='分析失败（见日志）';}
    }else if(s.kind==='screen'){
      $('goScreen').disabled=false;
      if(s.ok){$('prog2').textContent='完成 ✅  '+s.out;$('fill2').style.width='100%';
        $('doneRow2').style.display='flex';}
      else{$('prog2').textContent=s.cancelled?'已取消 ⏹':'失败 ❌（见下方日志）';}
    }
  }
}
let BUSY=false, POLL=null;
async function run(){
  if(!PAIRS.length)return;
  const d={pairs:PAIRS,folder:FOLDER,audio:AUDIO};
  for(const k of ['caption','caption_size','label_before','label_after','scene_sec','transition','slider','direction','comment_user','comment_text','progress_text'])d[k]=$(k).value;
  // 对齐开关与手动微调必须一起送过去，否则后端只会走默认的「自动对齐」
  d.pair_groups = String(GROUPS);   // /run 会按 SETTING_KEYS 全量写盘，不带上就被抹掉
  d.align_off  = !$('alignOn').checked;
  d.align_fill = $('alignFill').checked;
  d.nudges     = d.align_off ? {} : NUDGE;
  const r=await post('/run',d);
  if(r.error){$('prog').textContent=r.error;return;}
  BUSY=true;$('go').disabled=true;$('bar').style.display='';$('doneRow').style.display='none';
  $('fill').style.width='0%';$('log').textContent='';
  POLL=setInterval(poll,400);
}
async function poll(){
  const s=await post('/status');
  if(s.total>0){
    $('fill').style.width=(100*s.done_n/s.total).toFixed(1)+'%';
    $('prog').textContent=tp('渲染中 {}/{} 帧', s.done_n, s.total);
  }
  $('log').textContent=s.lines.join('\n');
  $('log').scrollTop=$('log').scrollHeight;
  if(s.done){
    clearInterval(POLL);BUSY=false;$('go').disabled=false;
    if(s.ok){$('prog').textContent='完成 ✅  '+s.out;$('fill').style.width='100%';
      $('doneRow').style.display='flex';}
    else{$('prog').textContent=s.cancelled?'已取消 ⏹':'失败 ❌（见下方日志）';}
  }
}
(async()=>{
  const s=await post('/settings');
  for(const k of ['caption','caption_size','label_before','label_after','scene_sec','transition','slider','direction','comment_user','comment_text','progress_text'])
    if(s[k]!==undefined&&s[k]!=='')$(k).value=s[k];
  if(s.demo_caption)$('demoCaption').value=s.demo_caption;
  $('demoCaptionMode').value=s.demo_caption_mode==='text'?'text':'none';
  if(s.demo_caption_style)$('demoCaptionStyle').value=s.demo_caption_style;
  if(s.demo_caption_y)$('demoCaptionY').value=s.demo_caption_y;
  if(s.demo_font)$('demoFont').value=s.demo_font;
  if(s.demo_duration)$('demoDur').value=s.demo_duration;
  if(s.demo_motion)$('demoMotion').value=s.demo_motion;
  demoCaptionChanged();
  (s.speed_presets||[]).forEach(([k,,sec])=>{SPEED_SEC[k]=sec;});
  capPreview();
  if(s.audio){AUDIO=s.audio;$('audioName').textContent=base(AUDIO);$('audioName2').textContent=base(AUDIO);}
  // 上传框组数：记住上次用的，默认 2 组
  THEME=s.theme||'dark'; ACCENT=s.accent||'orange'; LANG=s.lang||'zh';
  I18N=s.i18n||{}; LANGS=s.langs||[['zh','中文']];
  $('langSel').innerHTML=LANGS.map(([c,n])=>`<option value="${c}">${n}</option>`).join('');
  applyTheme();
  const gn=parseInt(s.pair_groups,10);
  GROUPS=(gn>=1&&gn<=MAX_GROUPS)?gn:2;
  SLOTS=Array.from({length:GROUPS},()=>({B:'',A:''}));
  PPRESETS=s.pair_presets||{presets:{},last:''};
  renderPairPresets();
  wireNumPairs();
  syncReveal();
  renderGroups();
  alignChanged();
  Object.keys(DZ).forEach(wireZone);
  [['modePairs','pairs'],['modeScreen','screen'],['modeDemo','demo'],
   ['modeRing','ring'],['modeRosie','rosie'],['modeAd','ad']].forEach(([id,m])=>wireModeFallback(id,m));
  // 拖到窗口其它地方不要让 webview 直接打开文件
  ['dragover','drop'].forEach(ev=>document.addEventListener(ev,e=>e.preventDefault()));
  // 只在真的有任务在跑时露出取消按钮（非活动模式的按钮本来就不可见）
  setInterval(()=>{
    document.querySelectorAll('button.cancel').forEach(b=>{
      b.style.display=BUSY?'':'none';
      if(!BUSY){b.disabled=false;b.textContent=tr('取消生成');}
    });
  },200);
  applyLang(); watchLang();
  setInterval(()=>post('/ping'),5000);
})();
</script>
</body></html>"""


SHORTCUTS = [
    ("AI 视频 · 模板", "https://www.fotor.com/apps/ai-video-generator/#from-template"),
    ("AI 视频 · 新建", "https://www.fotor.com/apps/ai-video-generator/#from-create"),
    ("AI 视频 · Magic Sync", "https://www.fotor.com/apps/ai-video-generator/#from-magic-sync"),
    ("AI 图片创作", "https://www.fotor.com/images/create/"),
    ("Pinterest", "https://www.pinterest.com/"),
    ("钉钉文档", "https://alidocs.dingtalk.com/i/nodes/ZX6GRezwJl5bRPBDfgX6KqdP8dqbropQ"
                 "?cid=76657130221&utm_source=im&utm_scene=person_space"
                 "&iframeQuery=utm_medium%253Dim_card%2526utm_source%253Dim"
                 "&utm_medium=im_card&corpId=dingcfe491e24cf0192a35c2f4657eb6378f"),
    ("钉钉表格", "https://alidocs.dingtalk.com/spreadsheetv2/meeagJ10uzYWEwQ5/edit"
                 "?cid=76657130221&type=s&docKey=oJGq75k2y9LdBlAK"
                 "&dentryKey=meeagJ10uzYWEwQ5&utm_source=im&utm_medium=im_card"
                 "&dontjump=true&chInfo=im"),
]


SHORTCUT_NAMES_PATH = os.path.join(app_support_dir(), "shortcut_names.json")


def shortcuts_load():
    """返回 [[显示名, 网址], ...]。改过名的用自定义名，其余用内置默认名。"""
    try:
        with open(SHORTCUT_NAMES_PATH) as f:
            names = json.load(f)
    except Exception:
        names = {}
    out = []
    for i, (label, url) in enumerate(SHORTCUTS):
        custom = names.get(str(i)) if isinstance(names, dict) else None
        out.append([custom or label, url])
    return out


def shortcut_rename(idx, name):
    try:
        with open(SHORTCUT_NAMES_PATH) as f:
            names = json.load(f)
        if not isinstance(names, dict):
            names = {}
    except Exception:
        names = {}
    name = (name or "").strip()[:24]
    if name:
        names[str(idx)] = name
    else:
        names.pop(str(idx), None)      # 清空 = 恢复默认名
    os.makedirs(app_support_dir(), exist_ok=True)
    with open(SHORTCUT_NAMES_PATH, "w") as f:
        json.dump(names, f, ensure_ascii=False, indent=1)
    return shortcuts_load()


def _clamp_float(v, dflt, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return dflt
    return max(lo, min(hi, f))


def _options(items, selected=None):
    """从源表生成 <option>，别再手抄一份到 HTML 里 —— 抄了就会和源表走散。"""
    out = []
    for key, label in items:
        sel = " selected" if key == selected else ""
        out.append(f'<option value="{key}"{sel}>{label}</option>')
    return "".join(out)


def render_page():
    """把页面里的下拉占位符按当前源表填上。"""
    shortcuts = "".join(
        f'<button class="small" onclick="openShortcut({i})" '
        f'oncontextmenu="renameShortcut({i});return false" '
        f'title="右键改名">{label}</button>'
        for i, (label, _) in enumerate(shortcuts_load()))
    return (PAGE
            .replace("<!--APP_VERSION-->", APP_VERSION)
            .replace("<!--SHORTCUTS-->", shortcuts)
            .replace("<!--CAPTION_STYLE_OPTIONS-->",
                     _options([(k, lab) for k, lab, _ in dragdemo.CAPTION_STYLES],
                              selected="plain"))
            .replace("<!--FONT_OPTIONS-->",
                     _options([(k, lab) for k, lab, _ in dragdemo.FONTS]))
            .replace("<!--SPEED_OPTIONS-->",
                     _options([(k, lab) for k, lab, _ in dragdemo.SPEED_PRESETS]))
            .replace("<!--SEG_TRANSITION_OPTIONS-->",
                     _options(adcut.SEG_TRANSITIONS, selected="fadeblack")))


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body = render_page().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/thumb?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            p = (q.get("p") or [""])[0]
            data = THUMB_CACHE.get(p)
            if data is None:
                try:
                    from PIL import Image, ImageOps
                    im = Image.open(p).convert("RGB")
                    im = ImageOps.exif_transpose(im)
                    im.thumbnail((240, 320))
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=80)
                    data = buf.getvalue()
                    THUMB_CACHE[p] = data
                except Exception:
                    self.send_response(404)
                    self.end_headers()
                    return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/caption_preview?"):
            # 直接用真正的渲染函数出图，所见即所得（字体/样式/位置都是成片里的那套）
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, keep_blank_values=True)
            g = lambda k, d="": (q.get(k) or [d])[0]
            try:
                y = float(g("y", str(dragdemo.CAPTION_Y)))
            except ValueError:
                y = dragdemo.CAPTION_Y
            try:
                from PIL import Image as _Im, ImageDraw as _D
                base = _Im.new("RGB", (dragdemo.W, dragdemo.H), (0, 0, 0))
                d = _D.Draw(base, "RGBA")
                dragdemo._dashed_round_rect(d, dragdemo.ZONE, dragdemo.ZONE_R,
                                            dragdemo.DASH_ON, dragdemo.DASH_OFF,
                                            dragdemo.DASH_W, dragdemo.DASH_COLOR)
                pill = dragdemo.build_pill(g("text", ""),
                                           g("style", "plain"), y, g("font", "system"))
                base.paste(pill, (0, 0), pill)
                base.thumbnail((216, 384), _Im.LANCZOS)
                buf = io.BytesIO()
                base.save(buf, "PNG")
                data = buf.getvalue()
            except Exception:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/asset_frame?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            kind = (q.get("kind") or [""])[0]
            assets = rosie.resolve_assets()
            path = (assets.get(kind) or {}).get("path")
            data = video_frame_png(path, keep_alpha=True) if path else None
            if not data:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/src_frame?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            path = (q.get("p") or [""])[0]
            data = video_frame_png(path, keep_alpha=False) if path else None
            if not data:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/ping":
            with LOCK:
                STATE["last_ping"] = time.time()
            self._json({"ok": True})
        elif self.path == "/align_preview":
            d = self._read()
            b, a = d.get("before"), d.get("after")
            if not (b and a and os.path.isfile(b) and os.path.isfile(a)):
                self._json({"error": "图片不存在"})
                return
            try:
                import align as _align
                from PIL import Image as _Im
                nudge = d.get("nudge") or None
                B, A = _Im.open(b), _Im.open(a)
                card = (540, 960)
                if d.get("off"):
                    bc = _align.fit_card(B, card)
                    ac = _align.fit_card(A, card)
                    info = {"method": "off", "score": 0, "gain": 0,
                            "scale": 1, "dx": 0, "dy": 0}
                else:
                    bc, ac, info = _align.align_pair(
                        B, A, card, fill=bool(d.get("fill", True)),
                        nudge=nudge)
                    info = {k: info[k] for k in
                            ("method", "score", "gain", "scale", "dx", "dy")}
                # 梳齿交错预览：错位时人物边缘会呈锯齿
                out = bc.copy()
                n, step = 10, card[1] // 10
                for i in range(n):
                    if i % 2:
                        y0 = i * step
                        out.paste(ac.crop((0, y0, card[0],
                                           min(card[1], y0 + step))), (0, y0))
                tmp = os.path.join(app_support_dir(), "align_preview")
                os.makedirs(tmp, exist_ok=True)
                p = os.path.join(tmp, f"p{abs(hash((b, a, str(nudge), d.get('off'), d.get('fill'))))%99999}.jpg")
                out.save(p, "JPEG", quality=86)
                self._json({"img": p, "info": info})
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"})
        elif self.path == "/rosie_init":
            self._json({"presets": rosie.load_presets(),
                        "assets": rosie.resolve_assets(),
                        "sizes": rosie.load_sizes(),
                        "ov": rosie.ov_load(),
                        "defaults": {"precomp": rosiecut.PRECOMP_PCT,
                                     "watermark": rosiecut.WATERMARK_PCT}})
        elif self.path == "/rosie_preset_save":
            d = self._read()
            name = (d.get("name") or "").strip()
            if not name:
                self._json({"error": "请填预设名"})
                return
            self._json({"presets": rosie.preset_save(name, d.get("params") or {})})
        elif self.path == "/rosie_preset_delete":
            self._json({"presets": rosie.preset_delete(
                (self._read().get("name") or "").strip())})
        elif self.path == "/rosie_asset_pick":
            d = self._read()
            kind = d.get("kind")
            if kind not in rosie.ASSET_KINDS:
                self._json({"error": "未知素材类型"})
                return
            p = osascript('POSIX path of (choose file with prompt "选择叠加素材"'
                          ' of type {"public.movie"})')
            if p:
                rosie.save_assets_file(**{kind: p})
            self._json({"assets": rosie.resolve_assets(), "sizes": rosie.load_sizes()})
        elif self.path == "/rosie_asset_reset":
            kind = self._read().get("kind")
            if kind not in rosie.ASSET_KINDS:
                self._json({"error": "未知素材类型"})
                return
            self._json(rosie.clear_asset(kind))
        elif self.path == "/rosie_ov_save":
            d = self._read()
            name = (d.get("name") or "").strip()
            if not name:
                self._json({"error": "请填预设名"})
                return
            ov = rosie.ov_save(name, d.get("data") or {})
            self._json({"ov": ov, "assets": rosie.resolve_assets(),
                        "sizes": rosie.load_sizes()})
        elif self.path == "/rosie_ov_delete":
            self._json({"ov": rosie.ov_delete((self._read().get("name") or "").strip())})
        elif self.path == "/rosie_ov_apply":
            d = self._read()
            ov = rosie.ov_load()
            entry = ov["presets"].get((d.get("name") or "").strip())
            if entry is None:
                self._json({"error": "预设不存在"})
                return
            r = rosie.ov_apply(entry)
            r["ov"] = ov
            self._json(r)
        elif self.path == "/rosie_sizes":
            d = self._read()
            sz = {}
            for k in ("precomp", "watermark"):
                try:
                    v = float(d.get(k))
                    if 1 <= v <= 100:
                        sz[k] = v
                except (TypeError, ValueError):
                    pass
            if sz:
                rosie.save_assets_file(sizes=sz)
            self._json({"sizes": rosie.load_sizes()})
        elif self.path == "/rosie_nl":
            d = self._read()
            text = (d.get("text") or "").strip()
            if not text:
                self._json({"error": "请输入指令"})
                return
            try:
                params, summary, engine = rosie.parse_nl(
                    text, d.get("cur") or {}, d.get("api_key"))
            except Exception as e:
                self._json({"error": f"解析失败: {e}"})
                return
            self._json({"params": params, "summary": summary, "engine": engine})
        elif self.path == "/rosie_run":
            d = self._read()
            src = (d.get("src") or "").strip()
            if not os.path.isfile(src):
                self._json({"error": f"文件不存在: {src}"})
                return
            params = d.get("params") or {}
            for k in ("paint_sec", "type_speed", "wait_sec", "last_wait"):
                try:
                    float(str(params.get(k, "")).strip())
                except ValueError:
                    self._json({"error": "参数必须是数字"})
                    return
            if str(params.get("target", "")).strip():
                try:
                    float(params["target"])
                except ValueError:
                    self._json({"error": "目标时长必须是数字或留空"})
                    return
            sizes = {}
            for k in ("precomp", "watermark"):
                try:
                    v = float((d.get("sizes") or {}).get(k))
                    if 1 <= v <= 100:
                        sizes[k] = v
                except (TypeError, ValueError):
                    pass
            if sizes:
                rosie.save_assets_file(sizes=sizes)
            with LOCK:
                if STATE["busy"]:
                    self._json({"error": "正在处理中"})
                    return
                render.clear_cancel()
                STATE.update(busy=True, done=False, ok=False, cancelled=False, out=None,
                             kind="rosie", prog_done=0, prog_total=0, lines=[])
            threading.Thread(target=worker_rosie,
                             args=(src, params,
                                   "ad" if d.get("mode") == "ad" else "natural",
                                   bool(d.get("overlay", True)), sizes),
                             daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/history":
            history.prune()
            st = load_settings()
            self._json({
                "items": history.load()[:120],
                "labels": history.MODE_LABEL,
                "jy_auto": bool(st.get("jy_auto")),
                "jy_dir": st.get("jy_dir") or history.default_jy_dir(),
                "jy_app": bool(history.jianying_installed()),
            })
        elif self.path == "/history_action":
            d = self._read()
            act = d.get("act")
            rid = d.get("id")
            rec = next((r for r in history.load() if r.get("id") == rid), None)
            if act == "set_jy":
                save_settings({"jy_auto": "1" if d.get("auto") else "",
                               "jy_dir": (d.get("dir") or "").strip()})
                self._json({"ok": True})
                return
            if act == "pick_dir":
                p = osascript('POSIX path of (choose folder with prompt "选择剪映素材文件夹")')
                self._json({"path": p or None})
                return
            if not rec:
                self._json({"error": "记录不存在"})
                return
            if act == "reveal":
                reveal(rec["path"])
            elif act == "play":
                subprocess.run(["open", rec["path"]] if sys.platform == "darwin"
                               else ["xdg-open", rec["path"]])
            elif act == "to_jy":
                st = load_settings()
                folder = st.get("jy_dir") or history.default_jy_dir()
                try:
                    p = history.copy_to(rec["path"], folder)
                except Exception as e:
                    self._json({"error": f"复制失败: {e}"})
                    return
                reveal(p)
                self._json({"ok": True, "path": p})
                return
            elif act == "open_jy_dir":
                st = load_settings()
                folder = st.get("jy_dir") or history.default_jy_dir()
                os.makedirs(folder, exist_ok=True)
                subprocess.run(["open", folder])
            elif act == "delete":
                history.remove(rid, delete_file=bool(d.get("with_file")))
            self._json({"ok": True})
        elif self.path == "/settings":
            r = load_settings()
            r["pair_presets"] = pair_presets_load()
            r["langs"] = i18n.LANGS
            r["speed_presets"] = dragdemo.SPEED_PRESETS
            r["shortcuts"] = shortcuts_load()
            r["i18n"] = {code: i18n.table(code) for code, _ in i18n.LANGS}
            self._json(r)
        elif self.path == "/rename_shortcut":
            d = self._read()
            try:
                idx = int(d.get("i"))
            except (TypeError, ValueError):
                idx = -1
            if not (0 <= idx < len(SHORTCUTS)):
                self._json({"error": "未知入口"})
                return
            self._json({"shortcuts": shortcut_rename(idx, d.get("name"))})
        elif self.path == "/open_shortcut":
            try:
                idx = int(self._read().get("i"))
            except (TypeError, ValueError):
                idx = -1
            if not (0 <= idx < len(SHORTCUTS)):
                self._json({"error": "未知入口"})
                return
            import webbrowser
            webbrowser.open(SHORTCUTS[idx][1])          # 用系统浏览器开，不占应用窗口
            self._json({"ok": True})
        elif self.path == "/save_settings":
            save_settings(self._read())
            self._json({"ok": True})
        elif self.path == "/pair_preset_save":
            d = self._read()
            name = (d.get("name") or "").strip()
            if not name:
                self._json({"error": "请填预设名"})
                return
            self._json(pair_preset_save(name, d.get("params") or {}))
        elif self.path == "/pair_preset_delete":
            self._json(pair_preset_delete((self._read().get("name") or "").strip()))
        elif self.path == "/pick_folder":
            path = pick_folder()
            if not path:
                self._json({"path": None})
                return
            imgs = render.list_images(path)
            self._json({"path": path,
                        "pairs": render.auto_pair(imgs),
                        "images": imgs,
                        "out": default_out_path(path)})
        elif self.path == "/repair":
            folder = self._read().get("folder") or ""
            imgs = render.list_images(folder) if os.path.isdir(folder) else []
            self._json({"pairs": render.auto_pair(imgs), "images": imgs})
        elif self.path == "/pick_video":
            # 只有「录屏步骤」那页需要顺带分析；自然流自动剪辑和广告结尾片段
            # 只是挑个文件，别替它们占住 busy —— 否则它们下一步会被自己
            # 悄悄触发的分析挡在门外，而且那页根本没有轮询来提示这件事。
            analyze = bool(self._read().get("analyze"))
            path = pick_video()
            if not path:
                self._json({"path": None})
                return
            if not analyze:
                self._json({"path": path})
                return
            with LOCK:
                if STATE["busy"]:
                    self._json({"error": "正在处理中"})
                    return
                render.clear_cancel()
                STATE.update(busy=True, done=False, ok=False, cancelled=False, kind="analyze",
                             plan=None, video=None, out=None,
                             prog_done=0, prog_total=0, lines=[])
            threading.Thread(target=worker_analyze, args=(path,),
                             daemon=True).start()
            self._json({"path": path})
        elif self.path == "/run_screen":
            d = self._read()
            with LOCK:
                video, plan_d = STATE["video"], STATE["plan"]
            if not (video and plan_d):
                self._json({"error": "请先选择并分析录屏"})
                return
            texts = d.get("texts") or []
            if len(texts) != 4:
                self._json({"error": "字幕参数不完整"})
                return
            audio = (d.get("audio") or "").strip()
            if audio and not os.path.isfile(audio):
                self._json({"error": f"BGM 文件不存在: {audio}"})
                return
            zoom_end = bool(d.get("zoom_end"))
            zoom_photo = (d.get("zoom_photo") or "").strip()
            if zoom_end and not zoom_photo:
                self._json({"error": "勾选了结尾放大，请先选择结果原图"})
                return
            if zoom_photo and not os.path.isfile(zoom_photo):
                self._json({"error": f"结果图不存在: {zoom_photo}"})
                return
            out = os.path.splitext(video)[0] + "_加字幕.mp4"
            base, ext = os.path.splitext(out)
            i = 2
            while os.path.exists(out):
                out = f"{base}-{i}{ext}"
                i += 1
            with LOCK:
                if STATE["busy"]:
                    self._json({"error": "正在渲染中"})
                    return
                render.clear_cancel()
                STATE.update(busy=True, done=False, ok=False, cancelled=False, kind="screen",
                             out=None, prog_done=0, prog_total=0, lines=[])
            threading.Thread(target=worker_screen,
                             args=(video, plan_d, texts, out, audio,
                                   zoom_end, zoom_photo),
                             daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/ring_preview":
            d = self._read()
            try:
                badge = ringarrow.build_badge(
                    d.get("photo") or "", int(d.get("W") or 1080),
                    int(d.get("H") or 1920),
                    center=(float(d.get("cx") or .214), float(d.get("cy") or .78)),
                    radius=float(d.get("radius") or .161),
                    crop_x=float(d.get("crop_x") or .5),
                    crop_y=float(d.get("crop_y") or .4))
            except Exception as e:
                self._json({"error": str(e)})
                return
            # 棋盘格底衬，方便看透明区域
            from PIL import Image as _I, ImageDraw as _D
            pv = badge.copy()
            pv.thumbnail((360, 640))
            bg = _I.new("RGB", pv.size, (60, 60, 66))
            dd = _D.Draw(bg)
            for yy in range(0, pv.size[1], 16):
                for xx in range(0, pv.size[0], 16):
                    if (xx // 16 + yy // 16) % 2:
                        dd.rectangle((xx, yy, xx + 15, yy + 15), fill=(78, 78, 86))
            bg.paste(pv, (0, 0), pv)
            buf = io.BytesIO(); bg.save(buf, "PNG")
            self._json({"png": "data:image/png;base64," +
                        base64.b64encode(buf.getvalue()).decode()})
        elif self.path == "/run_ring":
            d = self._read()
            photo = (d.get("photo") or "").strip()
            if not os.path.isfile(photo):
                self._json({"error": "请先选择原图"})
                return
            out = os.path.splitext(photo)[0] + "-圆环箭头.mov"
            base, ext = os.path.splitext(out)
            i = 2
            while os.path.exists(out):
                out = f"{base}-{i}{ext}"; i += 1
            try:
                opts = dict(photo=photo, out=out,
                            W=int(d.get("W") or 1080), H=int(d.get("H") or 1920),
                            cx=float(d.get("cx") or .214), cy=float(d.get("cy") or .78),
                            radius=float(d.get("radius") or .161),
                            crop_x=float(d.get("crop_x") or .5),
                            crop_y=float(d.get("crop_y") or .4),
                            dur=float(d.get("dur") or 8), pop=bool(d.get("pop")))
            except ValueError:
                self._json({"error": "参数必须是数字"}); return
            with LOCK:
                if STATE["busy"]:
                    self._json({"error": "正在渲染中"}); return
                render.clear_cancel()
                STATE.update(busy=True, done=False, ok=False, cancelled=False, out=None,
                             kind="ring", prog_done=0, prog_total=0, lines=[])
            threading.Thread(target=worker_ring, args=(opts,), daemon=True).start()
            self._json({"ok": True, "out": out})
        elif self.path == "/run_demo":
            d = self._read()
            photo = (d.get("photo") or "").strip()
            if not os.path.isfile(photo):
                self._json({"error": "请先选择要演示上传的照片"})
                return
            result = (d.get("result") or "").strip()
            if result and not os.path.isfile(result):
                self._json({"error": f"结果图不存在: {result}"})
                return
            dopts = {
                "motion": "drag" if d.get("motion") == "drag" else "slide",
                "transparent": bool(d.get("transparent")),
                "cursor": None if d.get("cursor") is None else bool(d.get("cursor")),
                "sound": bool(d.get("sound")),
                "use_transition": bool(d.get("use_transition", True)),
                "caption_style": (d.get("caption_style")
                                  if d.get("caption_style") in dragdemo.CAPTION_STYLE_KEYS
                                  else "plain"),
                "caption_font_key": (d.get("caption_font")
                                     if d.get("caption_font") in dragdemo.FONT_KEYS
                                     else "system"),
                "caption_y": _clamp_float(d.get("caption_y"), 18.5, 4, 80) / 100.0,
                "duration": _clamp_float(d.get("duration"), 1.9, 0.6, 15.0),
            }
            out = os.path.splitext(photo)[0] + "-拖照片演示" + \
                (".mov" if dopts["transparent"] else ".mp4")
            base, ext = os.path.splitext(out)
            i = 2
            while os.path.exists(out):
                out = f"{base}-{i}{ext}"
                i += 1
            with LOCK:
                if STATE["busy"]:
                    self._json({"error": "正在渲染中"})
                    return
                render.clear_cancel()
                STATE.update(busy=True, done=False, ok=False, cancelled=False, out=None,
                             kind="demo", prog_done=0, prog_total=0, lines=[])
            caption_mode = "text" if d.get("caption_mode") == "text" else "none"
            caption = (d.get("caption") or "").strip() if caption_mode == "text" else ""
            save_settings({"demo_caption": caption,
                           "demo_caption_mode": caption_mode,
                           "demo_caption_style": dopts["caption_style"],
                           "demo_caption_y": str(d.get("caption_y", "18.5")),
                           "demo_font": dopts["caption_font_key"],
                           "demo_duration": str(d.get("duration", "1.9")),
                           "demo_motion": dopts["motion"]})
            threading.Thread(target=worker_demo,
                             args=(photo, result, caption,
                                   out, dopts),
                             daemon=True).start()
            self._json({"ok": True, "out": out})
        elif self.path == "/upload_stream":
            # 拖拽上传：任意类型、任意大小，边收边写盘
            name = urllib.parse.unquote(self.headers.get("X-Filename") or "file")
            kind = (self.headers.get("X-Kind") or "image").strip()
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._json({"error": "空文件"})
                return
            try:
                path = save_upload_stream(name, kind, self.rfile, length)
            except Exception:
                self._json({"error": "文件写入失败"})
                return
            if kind == "image":
                try:
                    from PIL import Image as _Im
                    _Im.open(path).verify()
                except Exception:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    self._json({"error": "这不是一张能识别的图片"})
                    return
            elif kind in ("video", "audio"):
                # 扩展名对不上、或者 ffmpeg 根本读不出这条流，就当场退回，
                # 免得拖到渲染那一步才炸出一大段 ffmpeg 报错。
                bad = os.path.splitext(path)[1].lower() not in KIND_EXTS[kind]
                if not bad:
                    bad = not _probe_ok(path, kind)
                if bad:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    self._json({"error": "这个文件打不开（格式不对或已损坏）"})
                    return
            self._json({"path": path,
                        "name": os.path.basename(name),
                        "out": default_out_path(uploads_out_folder())})
        elif self.path == "/cancel":
            with LOCK:
                busy = STATE.get("busy")
            if not busy:
                self._json({"ok": False, "error": "当前没有正在生成的任务"})
                return
            log("正在取消 …")
            render.request_cancel()
            self._json({"ok": True})
        elif self.path == "/upload":
            d = self._read()
            try:
                raw = base64.b64decode(d.get("data") or "")
            except Exception:
                raw = b""
            if not raw:
                self._json({"error": "文件读取失败"})
                return
            try:
                from PIL import Image as _Im
                path = save_upload(d.get("name") or "img.png", raw)
                _Im.open(path).verify()
            except Exception:
                self._json({"error": "这不是一张能识别的图片"})
                return
            self._json({"path": path,
                        "name": os.path.basename(d.get("name") or ""),
                        "out": default_out_path(uploads_out_folder())})
        elif self.path == "/pick_image":
            prompt = (self._read().get("prompt") or "选择图片").strip()
            self._json({"path": pick_image_single(prompt)})
        elif self.path == "/pick_zoom_photo":
            self._json({"path": pick_image_single("选择最终结果原图")})
        elif self.path == "/pick_images":
            self._json({"paths": pick_images()})
        elif self.path == "/pick_audio":
            self._json({"path": pick_audio()})
        elif self.path == "/run":
            d = self._read()
            pairs = [tuple(p) for p in d.get("pairs") or []
                     if isinstance(p, (list, tuple)) and len(p) == 2
                     and all(isinstance(x, str) and os.path.isfile(x) for x in p)]
            if not pairs:
                self._json({"error": "没有有效的图片对"})
                return
            folder = (d.get("folder") or "").strip()
            if not folder:
                first_dir = os.path.dirname(pairs[0][0])
                folder = (uploads_out_folder()
                          if os.path.normpath(first_dir).startswith(
                              os.path.normpath(UPLOAD_DIR))
                          else first_dir)
            audio = (d.get("audio") or "").strip()
            if audio and not os.path.isfile(audio):
                self._json({"error": f"BGM 文件不存在: {audio}"})
                return
            try:
                opts = dict(
                    out=default_out_path(folder),
                    caption=(d.get("caption") or "").strip(),
                    caption_size=int(float(d.get("caption_size") or 55)),
                    label_before=(d.get("label_before") or "").strip(),
                    label_after=(d.get("label_after") or "").strip(),
                    scene_sec=float(d.get("scene_sec") or 3.6),
                    align_mode="off" if d.get("align_off") else "auto",
                    align_fill=bool(d.get("align_fill", True)),
                    nudges={int(k): v for k, v in
                            (d.get("nudges") or {}).items()},
                    transition=d.get("transition") or "spin",
                    slider=d.get("slider") or "sweep",
                    direction=d.get("direction") or "rtl",
                    comment_user=(d.get("comment_user") or "@user").strip(),
                    comment_text=(d.get("comment_text") or "").strip(),
                    progress_text=(d.get("progress_text") or "Removing filter").strip(),
                    audio=audio,
                )
            except ValueError:
                self._json({"error": "参数必须是数字"})
                return
            # 不覆盖旧成片：自动加 -2、-3 后缀
            base, ext = os.path.splitext(opts["out"])
            i = 2
            while os.path.exists(opts["out"]):
                opts["out"] = f"{base}-{i}{ext}"
                i += 1
            with LOCK:
                if STATE["busy"]:
                    self._json({"error": "正在渲染中"})
                    return
                render.clear_cancel()
                STATE.update(busy=True, done=False, ok=False, cancelled=False, out=None,
                             kind="render", prog_done=0, prog_total=0, lines=[])
            save_settings({k: str(d.get(k, "")) for k in SETTING_KEYS})
            threading.Thread(target=worker, args=(pairs, opts), daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/run_ad":
            d = self._read()
            pairs = [tuple(p) for p in d.get("pairs") or []
                     if isinstance(p, (list, tuple)) and len(p) == 2
                     and all(isinstance(x, str) and os.path.isfile(x) for x in p)]
            photo = (d.get("photo") or "").strip()
            bgm = (d.get("bgm") or "").strip()
            if not pairs:
                self._json({"error": "至少要有一组完整的前后图"})
                return
            for label, path in (("演示原图", photo), ("BGM", bgm)):
                if not path or not os.path.isfile(path):
                    self._json({"error": f"{label}不存在"})
                    return
            ending = (d.get("ending") or "").strip()
            if ending and not os.path.isfile(ending):
                self._json({"error": "结尾素材不存在"})
                return
            result = (d.get("result") or "").strip()
            try:
                speed = float(d.get("speed") or 2)
            except ValueError:
                speed = 2.0
            folder = os.path.dirname(photo)
            if os.path.normpath(folder).startswith(os.path.normpath(UPLOAD_DIR)):
                folder = uploads_out_folder()
            out = os.path.join(folder, os.path.splitext(os.path.basename(photo))[0]
                               + "-广告成片.mp4")
            seg = d.get("seg_transition") or "fadeblack"
            if seg not in adcut.SEG_TRANSITION_KEYS:
                seg = "fadeblack"
            try:
                seg_sec = float(d.get("seg_transition_sec") or 0.5)
            except ValueError:
                seg_sec = 0.5
            opts = dict(pairs=pairs, photo=photo, result=result or None,
                        ending=ending or None, bgm=bgm, out=out,
                        transition=d.get("transition") or "spin",
                        seg_transition=seg,
                        seg_transition_sec=max(0.1, min(1.5, seg_sec)),
                        caption=(d.get("caption") or "").strip(),
                        label_before=(d.get("label_before") or "Before").strip(),
                        label_after=(d.get("label_after") or "After").strip(),
                        speed=max(1.0, min(4.0, speed)),
                        slider=d.get("slider") or "sweep")
            with LOCK:
                if STATE["busy"]:
                    self._json({"error": "正在渲染中"})
                    return
                render.clear_cancel()
                STATE.update(busy=True, done=False, ok=False, cancelled=False,
                             out=None, kind="ad", prog_done=0, prog_total=0, lines=[])
            threading.Thread(target=worker_ad, args=(opts,), daemon=True).start()
            self._json({"ok": True, "out": out})
        elif self.path == "/status":
            with LOCK:
                p = STATE["plan"]
                self._json({
                    "busy": STATE["busy"], "done": STATE["done"], "ok": STATE["ok"],
                    "out": STATE["out"], "done_n": STATE["prog_done"],
                    "total": STATE["prog_total"], "lines": STATE["lines"][-60:],
                    "kind": STATE["kind"], "cancelled": STATE.get("cancelled", False),
                    "plan": (dict(n=len(p["segments"]), out_len=p["out_len"])
                             if p else None),
                })
        elif self.path == "/reveal":
            with LOCK:
                out = STATE["out"]
            if out:
                reveal(out)
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


def watchdog():
    while True:
        time.sleep(5)
        with LOCK:
            idle = time.time() - STATE["last_ping"]
            busy = STATE["busy"]
        if idle > 90 and not busy:
            os._exit(0)


def warm_imports():
    """启动时（主线程）把渲染链路会用到的模块全部导入一遍。

    冻结包里模块是压缩存放的，PyInstaller 的导入器解压时共用一份 zlib 状态；
    两个工作线程同时首次导入同一个模块会把它撞坏，报
    "zlib.error: Error -3 while decompressing data: incorrect header check"。
    提前在单线程里导完，工作线程就再也不需要现场解压。
    """
    import importlib
    for name in ("wave", "math", "re", "shutil", "argparse", "urllib.request",
                 "numpy", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFilter",
                 "PIL.ImageFont", "PIL.ImageOps", "imageio_ffmpeg",
                 "align", "adcut", "dragdemo", "ringarrow", "screencut",
                 "rosie", "rosiecut", "history", "analyze", "nl"):
        try:
            importlib.import_module(name)
        except Exception:
            pass


def run_gui():
    warm_imports()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    import urllib.request
    for _ in range(50):
        try:
            urllib.request.urlopen(url, timeout=0.2).read()
            break
        except Exception:
            time.sleep(0.1)
    print(f"CutKit running at {url}", flush=True)

    # mac 用 WKWebView、Windows 用 WebView2(EdgeChromium) 的原生窗口；
    # 只有原生窗口起不来时才退回浏览器模式
    if sys.platform in ("darwin", "win32"):
        try:
            import webview
            webview.create_window("CutKit", url,
                                  width=880, height=1000, min_size=(680, 720))
            webview.start()
            os._exit(0)
        except Exception:
            traceback.print_exc()

    import webbrowser
    threading.Thread(target=watchdog, daemon=True).start()
    webbrowser.open(url)
    srv.serve_forever()


def main():
    if "--cli-rosie" in sys.argv:
        argv = [a for a in sys.argv[1:] if a != "--cli-rosie"]
        sys.argv = [sys.argv[0]] + argv
        rosiecut.main()
        return
    if "--cli-ad" in sys.argv:
        argv = [a for a in sys.argv[1:] if a != "--cli-ad"]
        sys.exit(adcut.main(argv))
    if "--cli-ring" in sys.argv:
        argv = [a for a in sys.argv[1:] if a != "--cli-ring"]
        sys.exit(ringarrow.main(argv))
    elif "--cli-demo" in sys.argv:
        argv = [a for a in sys.argv[1:] if a != "--cli-demo"]
        sys.exit(dragdemo.main(argv))
    elif "--cli-screen" in sys.argv:
        argv = [a for a in sys.argv[1:] if a != "--cli-screen"]
        sys.exit(screencut.main(argv))
    elif "--cli" in sys.argv:
        argv = [a for a in sys.argv[1:] if a != "--cli"]
        sys.exit(render.main(argv))
    else:
        run_gui()


if __name__ == "__main__":
    main()
