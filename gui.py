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
    "last_ping": time.time(),
}
LOCK = threading.Lock()
THUMB_CACHE = {}

SETTING_KEYS = ("jy_auto", "jy_dir",
                "caption", "caption_size", "label_before", "label_after",
                "scene_sec", "transition", "slider", "audio", "demo_caption",
                "comment_user", "comment_text", "progress_text", "direction")
DEFAULT_SETTINGS = {
    "jy_auto": "", "jy_dir": "",
    "caption": "", "caption_size": "55",
    "label_before": "Before", "label_after": "After",
    "scene_sec": "3.6", "transition": "spin", "slider": "sweep", "audio": "",
    "demo_caption": "Just upload one photo",
    "comment_user": "@user",
    "comment_text": "can u remove the matcha filter from this",
    "progress_text": "Removing filter",
    "direction": "rtl",
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
    except Exception:
        log("❌ 出错了:\n" + traceback.format_exc())
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
    except Exception:
        log("出错了:\n" + traceback.format_exc(limit=3))
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
    except Exception:
        log("出错了:\n" + traceback.format_exc(limit=3))
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
    except Exception:
        log("出错了:\n" + traceback.format_exc(limit=3))
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
                          progress=prog, log=log).render()
        with LOCK:
            STATE.update(busy=False, done=True, ok=True, out=out)
        _record(out, STATE.get("kind", ""))
    except Exception:
        log("出错了:\n" + traceback.format_exc(limit=3))
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
    except Exception:
        log("出错了:\n" + traceback.format_exc(limit=3))
        with LOCK:
            STATE.update(busy=False, done=True, ok=False)


PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>CutKit 视频运营工具箱</title>
<style>
:root{--bg:#101014;--card:#1a1a21;--line:#2a2a33;--txt:#ececf1;--dim:#9a9aa5;
--acc:#ff7a45;--acc2:#ffb347;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:15px/1.5 -apple-system,"PingFang SC",sans-serif;padding:20px 22px 40px}
h1{font-size:20px;display:flex;align-items:center;gap:10px;margin-bottom:16px}
h1 .logo{width:30px;height:30px;border-radius:8px;background:linear-gradient(105deg,#31313c 48%,#fff 48%,#fff 52%,var(--acc) 52%,var(--acc2));display:inline-block}
h1 small{color:var(--dim);font-weight:400;font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
.card h2{font-size:14px;color:var(--dim);margin-bottom:12px;font-weight:600}
button{background:#2c2c36;color:var(--txt);border:1px solid #3a3a46;border-radius:9px;
padding:8px 14px;font-size:14px;cursor:pointer}
button:hover{background:#34343f}
button.primary{background:linear-gradient(120deg,var(--acc),var(--acc2));border:none;color:#1b0d05;font-weight:700;padding:12px 22px;font-size:16px}
button.small{padding:3px 9px;font-size:12px;border-radius:7px}
button:disabled{opacity:.45;cursor:default}
input,select{background:#131318;color:var(--txt);border:1px solid #34343f;border-radius:8px;padding:7px 10px;font-size:14px;width:100%}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.row>button{white-space:nowrap;flex:0 0 auto}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px 14px}
.grid label{font-size:12px;color:var(--dim);display:block;margin-bottom:3px}
.folder{color:var(--dim);font-size:13px;word-break:break-all;flex:1}
#pairs{display:flex;flex-direction:column;gap:10px;margin-top:12px}
.pair{display:flex;align-items:center;gap:12px;background:#15151b;border:1px solid var(--line);border-radius:12px;padding:10px}
.pair img{width:84px;height:112px;object-fit:cover;border-radius:8px;background:#000}
.pair .arrow{color:var(--acc);font-size:20px}
.pair .name{font-size:11px;color:var(--dim);max-width:84px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:center;margin-top:3px}
.pair .ops{margin-left:auto;display:flex;flex-direction:column;gap:6px}
.tag{font-size:11px;color:var(--dim);text-align:center}
#picker{display:none;margin-top:12px;border-top:1px solid var(--line);padding-top:12px}
.uprow{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:4px}
.upcol>label{display:block;font-weight:700;font-size:13px;margin-bottom:8px}
.upcol>label span{font-weight:400;color:var(--dim)}
.drop{position:relative;overflow:hidden;border:2px dashed #3a4049;border-radius:14px;
  min-height:200px;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:10px;cursor:pointer;background:#15171b;transition:border-color .15s,background .15s}
.drop:hover{border-color:#5a626e;background:#191c21}
.drop.over{border-color:var(--acc);background:#241a14}
.drop .ico{font-size:34px;line-height:1}
.drop .txt{font-weight:700;font-size:14px;color:#c9ced6}
.drop img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000}
.drop .clr{position:absolute;top:8px;right:8px;z-index:2;background:#000c;border:0;color:#fff;
  border-radius:8px;padding:3px 9px;cursor:pointer;font-size:14px;line-height:1.5}
.upname{font-size:11px;color:var(--dim);margin-top:6px;min-height:15px;word-break:break-all}
.thumbs{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.thumbs figure{position:relative;cursor:pointer}
.thumbs img{width:74px;height:99px;object-fit:cover;border-radius:8px;background:#000;
border:2px solid transparent;display:block}
.thumbs figure.sel img{border-color:var(--acc);box-shadow:0 0 0 3px rgba(255,122,69,.25)}
.thumbs figure.used img{opacity:.3}
.thumbs figcaption{position:absolute;left:0;right:0;bottom:0;font-size:10px;text-align:center;
background:rgba(0,0,0,.62);color:#fff;border-radius:0 0 6px 6px;padding:1px 0}
.thumbs figure.sel figcaption{background:var(--acc);color:#1b0d05;font-weight:700}
#bar{height:10px;background:#26262f;border-radius:5px;overflow:hidden;margin:10px 0 6px;display:none}
#bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .2s}
#log{white-space:pre-wrap;font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--dim);max-height:150px;overflow-y:auto;margin-top:8px}
#doneRow{display:none;gap:10px;margin-top:10px}
.hint{color:var(--dim);font-size:12px;margin-top:8px}
#hitems{display:flex;flex-direction:column;gap:10px;margin-top:12px}
.hrec{display:flex;gap:12px;background:#15151b;border:1px solid var(--line);
border-radius:12px;padding:10px;align-items:center}
.hrec img{width:78px;height:104px;object-fit:cover;border-radius:8px;background:#000;flex:0 0 auto}
.hrec .meta{flex:1;min-width:0}
.hrec .nm{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hrec .sub{font-size:11px;color:var(--dim);margin-top:3px}
.hrec .ops{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;max-width:280px}
.badge{display:inline-block;font-size:10px;padding:1px 7px;border-radius:6px;
background:#2c2c36;color:var(--dim);margin-right:6px}
.badge.mv{background:#3a2a12;color:var(--acc2)}
.alignbox{margin-top:10px;padding:10px;background:#111117;border:1px solid var(--line);
border-radius:10px;display:none}
.alignbox.on{display:flex;gap:12px;align-items:flex-start}
.alignbox img{width:150px;border-radius:8px;background:#000}
.nudge{display:grid;grid-template-columns:repeat(3,34px);gap:4px}
.nudge button{padding:4px 0;font-size:12px;border-radius:6px}
.tabs{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.tabs button{border-radius:10px;padding:9px 18px;white-space:nowrap;flex:0 0 auto}
.tabs button.on{background:linear-gradient(120deg,var(--acc),var(--acc2));color:#1b0d05;border:none;font-weight:700}
.mode{display:none}.mode.on{display:block}
#planBox{background:#15151b;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:13px;color:var(--dim);margin-top:10px;display:none}
</style></head><body>
<h1><span class="logo"></span>CutKit <small>视频运营工具箱</small></h1>

<div class="tabs">
<button id="tabPairs" class="on" onclick="setMode('pairs')">前后对比</button>
<button id="tabScreen" onclick="setMode('screen')">录屏步骤</button>
<button id="tabDemo" onclick="setMode('demo')">拖照片演示</button>
<button id="tabRing" onclick="setMode('ring')">圆环箭头</button>
<button id="tabRosie" onclick="setMode('rosie')">录屏自动剪辑</button>
<button id="tabHist" onclick="setMode('hist')">历史记录</button>
</div>

<div id="modePairs" class="mode on">
<div class="card">
<h2>① 上传图片</h2>
<div class="uprow">
<div class="upcol"><label>Before Image <span>· 带抹茶滤镜的图</span></label>
<div class="drop" id="dropB" onclick="pickInto('B')"></div>
<div class="upname" id="nameB"></div></div>
<div class="upcol"><label>After Image <span>· 去掉滤镜后的图</span></label>
<div class="drop" id="dropA" onclick="pickInto('A')"></div>
<div class="upname" id="nameA"></div></div>
</div>
<div class="hint">点框选文件，或直接把图拖进来。两边都放好会自动加成一对并清空，接着放下一对。</div>
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
<div class="row">
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="alignOn" checked style="width:auto" onchange="alignChanged()"> 自动对齐前后图人物（比例不同/位移缩放都能对上）</label>
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="alignFill" checked style="width:auto" onchange="alignChanged()"> 裁到共同区域（推荐，无边缘拉丝）</label>
</div></div>
<div><label>对比展示方式</label><select id="slider" onchange="syncReveal()">
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
<div><label>BGM（可选）</label><div class="row">
<button class="small" onclick="pickAudio()">选择…</button>
<button class="small" onclick="clearAudio()">清除</button></div>
<div class="hint" id="audioName" style="margin-top:4px"></div></div>
</div>
</div>

<div class="card">
<h2>③ 生成</h2>
<div class="row">
<button class="primary" id="go" onclick="run()" disabled>生成视频</button>
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
<div class="row">
<button onclick="pickVideo()">选择录屏文件…</button>
<div class="folder" id="videoPath"></div>
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
<div style="grid-column:2/4"><label>BGM（可选，复用左侧已选）</label><div class="row">
<button class="small" onclick="pickAudio()">选择…</button>
<button class="small" onclick="clearAudio()">清除</button>
<span class="hint" id="audioName2" style="margin-top:0"></span></div></div>
<div><label>字幕 1 · 选照片</label><input id="cap0" value="Select Photo"></div>
<div><label>字幕 2 · 选滤镜</label><input id="cap1" value="Select the Effect" oninput="cap1Edited=true"></div>
<div><label>字幕 3 · 等待</label><input id="cap2" value="Wait...."></div>
<div><label>字幕 4 · 结果（自动带 ❤️）</label><input id="cap3" value="Results..."></div>
<div style="grid-column:1/4"><label>结尾效果</label>
<div class="row">
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="zoomEnd" checked style="width:auto"> 结尾放大最终结果图（原图放大铺满 + 定格 1.8s，需选原图）</label>
<button class="small" onclick="pickZoomPhoto()">选结果原图…</button>
<button class="small" onclick="clearZoomPhoto()">清除</button>
<span class="hint" id="zoomPhotoName" style="margin-top:0"></span>
</div></div>
</div>
</div>

<div class="card">
<h2>③ 生成</h2>
<div class="row">
<button class="primary" id="goScreen" onclick="runScreen()" disabled>剪辑生成</button>
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
<div class="row">
<button onclick="pickDemoPhoto()">选择要上传演示的照片…</button>
<div class="folder" id="demoPhoto"></div>
</div>
<div class="row" style="margin-top:10px">
<button onclick="pickDemoResult()">选 AI 结果图（可选）…</button>
<button class="small" onclick="clearDemoResult()">清除</button>
<div class="folder" id="demoResult"></div>
</div>
<div class="hint">复刻「上传一张照片」的教程演示：虚线上传框 → 照片飞入落框回弹 → Fotor 彩色转场（进度环 + AI Generated 徽章，自带压暗）。选了结果图的话，徽章出现时会淡入成结果。</div>
</div>

<div class="card">
<h2>② 参数</h2>
<div class="grid">
<div style="grid-column:1/3"><label>顶部标题（粉色药丸，留空则不加）</label>
<input id="demoCaption" value="Just upload one photo"></div>
<div><label>动画风格</label><select id="demoMotion" onchange="demoMotionChanged()">
<option value="slide">飞入落框（复刻参考片）</option>
<option value="drag">光标拖拽（任意比例自适应）</option></select></div>
<div style="grid-column:1/4"><label>输出与效果</label>
<div class="row">
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="demoTransparent" style="width:auto" onchange="demoTransChanged()"> 透明底 MOV（可直接盖在自己的视频上）</label>
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="demoTrans2" checked style="width:auto"> 叠 Fotor 转场</label>
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="demoCursor" style="width:auto"> 鼠标指针</label>
<label class="row" style="gap:6px;font-size:14px;color:var(--txt)">
<input type="checkbox" id="demoSound" style="width:auto"> 拖拽音效</label>
</div></div>
</div>
<div class="hint" id="demoHint2">透明 MOV 不含黑底和标题药丸，只有虚线框 + 照片卡片 + 光标，方便在剪映/CapCut 里叠到任意画面上。</div>
</div>

<div class="card">
<h2>③ 生成</h2>
<div class="row">
<button class="primary" id="goDemo" onclick="runDemo()" disabled>生成演示视频</button>
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
<div class="row">
<button onclick="pickRingPhoto()">选择原图…</button>
<div class="folder" id="ringPhoto"></div>
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
<div class="row">
<button onclick="pickRosieSrc()">选择文件…</button>
<div class="folder" id="rosieSrc"></div>
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
<div id="rosieAssets" style="margin-top:10px"></div>
<div class="grid" style="margin-top:10px">
<div><label>预合成动画宽度（% 画面宽）</label><input id="r_precomp_size" value="7.4"></div>
<div><label>水印宽度（% 画面宽）</label><input id="r_watermark_size" value="44"></div>
<div style="display:flex;align-items:flex-end"><button class="small" onclick="resetRosieSizes()">恢复默认</button></div>
</div>
</div>

<div class="card">
<h2>⑤ 生成</h2>
<div class="row">
<button class="primary" id="goRosie" onclick="runRosie()" disabled>开始剪辑</button>
</div>
<div class="hint" id="progRosie"></div>
<div id="doneRowRosie" class="row" style="display:none;gap:10px;margin-top:10px">
<button onclick="post('/reveal')">在 Finder 中显示</button>
</div>
<div id="logRosie" style="white-space:pre-wrap;font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--dim);max-height:220px;overflow-y:auto;margin-top:8px"></div>
</div>
</div><!-- /modeRosie -->

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
  $('go').disabled = PAIRS.length===0 || BUSY;
  $('pairHint').textContent = PAIRS.length ?
    `${PAIRS.length} 对素材 · 成片约 ${(PAIRS.length*parseFloat($('scene_sec').value||3.6)).toFixed(1)} 秒` :
    '还没有配对：把 Before / After 各放一张到上面的两个框里。';
  renderThumbs();
}
let NUDGE={};
function alignChanged(){
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

let SLOT={B:'',A:''};
function paintSlot(k){
  const el=$(k==='B'?'dropB':'dropA'), p=SLOT[k];
  el.innerHTML = p
    ? `<img src="${thumb(p)}"><button class="clr" title="移除" onclick="event.stopPropagation();clearSlot('${k}')">✕</button>`
    : `<div class="ico">🖼️</div><div class="txt">Upload photo or drag &amp; drop</div>`;
  $(k==='B'?'nameB':'nameA').textContent = p ? disp(p) : '';
}
function clearSlot(k){SLOT[k]='';paintSlot(k);}
function tryPair(){
  if(SLOT.B&&SLOT.A){
    PAIRS.push([SLOT.B,SLOT.A]); SLOT={B:'',A:''};
    paintSlot('B'); paintSlot('A'); renderPairs();
  }
}
async function pickInto(k){
  if(SLOT[k])return;
  const r=await post('/pick_image',{prompt:k==='B'?'选择 Before 图（带滤镜）':'选择 After 图（去掉滤镜）'});
  if(r.path){SLOT[k]=r.path;paintSlot(k);tryPair();}
}
async function uploadFile(k,file){
  if(!/^image\//.test(file.type||'')&&!/\.(jpe?g|png|webp|bmp|tiff?|heic)$/i.test(file.name||'')){
    $('prog').textContent='只能放图片文件';return;}
  const el=$(k==='B'?'dropB':'dropA');
  el.innerHTML='<div class="txt">读取中…</div>';
  const buf=await file.arrayBuffer(), b=new Uint8Array(buf);
  let bin='';
  for(let i=0;i<b.length;i+=0x8000)bin+=String.fromCharCode.apply(null,b.subarray(i,i+0x8000));
  const r=await post('/upload',{name:file.name,data:btoa(bin)});
  if(r.path){
    if(r.name)NAMES[r.path]=r.name;
    SLOT[k]=r.path; paintSlot(k);
    if(!FOLDER&&r.out)$('outHint').textContent='输出: '+r.out;
    tryPair();
  }else{ $('prog').textContent=r.error||'上传失败'; paintSlot(k); }
}
function wireDrop(k){
  const el=$(k==='B'?'dropB':'dropA');
  el.addEventListener('dragover',e=>{e.preventDefault();el.classList.add('over');});
  el.addEventListener('dragleave',()=>el.classList.remove('over'));
  el.addEventListener('drop',e=>{
    e.preventDefault(); el.classList.remove('over');
    const f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];
    if(f)uploadFile(k,f);
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
let RSRC='', RPRESETS={}, RASSETS={}, RDEFAULTS={};
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
  renderRosieAssets();
}
function renderRosieAssets(){
  const el=$('rosieAssets'); el.innerHTML='';
  Object.entries(RASSETS).forEach(([k,v])=>{
    const d=document.createElement('div'); d.className='row'; d.style.marginTop='6px';
    d.innerHTML=`<span style="font-size:13px;min-width:96px">${esc(v.label)}</span>
      <span class="hint" style="margin-top:0;flex:1">${v.path?esc(base(v.path)):'未找到，将跳过叠加'}</span>
      <button class="small" onclick="pickRosieAsset('${k}')">选择…</button>`;
    el.appendChild(d);
  });
}
async function pickRosieAsset(kind){
  const r=await post('/rosie_asset_pick',{kind:kind});
  RASSETS=r.assets||RASSETS; renderRosieAssets();
}
function resetRosieSizes(){
  $('r_precomp_size').value=RDEFAULTS.precomp; $('r_watermark_size').value=RDEFAULTS.watermark;
  post('/rosie_sizes',rSizes());
}
function applyRosiePreset(){
  const p=RPRESETS[$('rosiePreset').value]; if(!p)return;
  RKEYS.forEach(k=>{if(p[k]!==undefined)$('r_'+k).value=p[k];});
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
  if(r.path){RSRC=r.path;$('rosieSrc').textContent=RSRC;$('goRosie').disabled=false;}
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
  const r=await post('/rosie_run',{src:RSRC,params:rParams(),mode:$('rosieMode').value,
    overlay:$('rosieOverlay').checked,sizes:rSizes()});
  if(r.error){$('progRosie').textContent=r.error;return;}
  BUSY=true;$('goRosie').disabled=true;$('doneRowRosie').style.display='none';
  $('logRosie').textContent='';$('progRosie').textContent='处理中…（首次分析约 1-2 分钟）';
  POLL=setInterval(pollRosie,600);
}
async function pollRosie(){
  const s=await post('/status');
  $('logRosie').textContent=s.lines.join('\n');
  $('logRosie').scrollTop=$('logRosie').scrollHeight;
  if(s.done){clearInterval(POLL);BUSY=false;$('goRosie').disabled=false;
    if(s.ok){$('progRosie').textContent='完成 ✅  '+s.out;$('doneRowRosie').style.display='flex';}
    else{$('progRosie').textContent='失败 ❌（见下方日志）';}}
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
  $('histCount').textContent = HREC.length ? `共 ${HREC.length} 条` : '还没有生成记录';
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
}
const REVEAL_HINT={
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
  $('revealHint').textContent=REVEAL_HINT[v]||'';
  for(const c of ['rvcomment','rvprogress'])
    document.querySelectorAll('.'+c).forEach(e=>{
      e.style.display=(c==='rv'+v)?'':'none';});
  const hasDir=['sweep','once','comment','wipe','grid'].includes(v);
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
    else{$('prog4').textContent='失败 ❌（见下方日志）';}
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
  $('demoCaption').disabled=tp;
  $('demoHint2').style.color = tp ? 'var(--acc)' : 'var(--dim)';
}
async function pickDemoResult(){
  const r=await post('/pick_image',{prompt:'选择 AI 结果图'});
  if(r.path){DRESULT=r.path;$('demoResult').textContent=base(DRESULT);}
}
function clearDemoResult(){DRESULT='';$('demoResult').textContent='';}
async function runDemo(){
  if(!DPHOTO)return;
  const r=await post('/run_demo',{photo:DPHOTO,result:DRESULT,caption:$('demoCaption').value,
    motion:$('demoMotion').value, transparent:$('demoTransparent').checked,
    cursor:$('demoCursor').checked, sound:$('demoSound').checked,
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
    $('prog3').textContent=`渲染中 ${s.done_n}/${s.total} 帧`;}
  $('log3').textContent=s.lines.join('\n');
  $('log3').scrollTop=$('log3').scrollHeight;
  if(s.done){
    clearInterval(POLL);BUSY=false;$('goDemo').disabled=false;
    if(s.ok){$('prog3').textContent='完成 ✅  '+s.out;$('fill3').style.width='100%';
      $('doneRow3').style.display='flex';}
    else{$('prog3').textContent='失败 ❌（见下方日志）';}
  }
}
function filterChanged(){
  if(cap1Edited)return;
  const v=$('filterName').value.trim();
  $('cap1').value=v?('Select the \u201c'+v+' Effect\u201d'):'Select the Effect';
}
async function pickVideo(){
  const r=await post('/pick_video');
  if(r.error){$('prog2').textContent=r.error;return;}
  if(!r.path)return;
  VIDEO=r.path;PLAN=null;
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
    $('prog2').textContent=`分析中 ${s.done_n}/${s.total}`;
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
      else{$('prog2').textContent='失败 ❌（见下方日志）';}
    }
  }
}
let BUSY=false, POLL=null;
async function run(){
  if(!PAIRS.length)return;
  const d={pairs:PAIRS,folder:FOLDER,audio:AUDIO};
  for(const k of ['caption','caption_size','label_before','label_after','scene_sec','transition','slider','direction','comment_user','comment_text','progress_text'])d[k]=$(k).value;
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
    $('prog').textContent=`渲染中 ${s.done_n}/${s.total} 帧`;
  }
  $('log').textContent=s.lines.join('\n');
  $('log').scrollTop=$('log').scrollHeight;
  if(s.done){
    clearInterval(POLL);BUSY=false;$('go').disabled=false;
    if(s.ok){$('prog').textContent='完成 ✅  '+s.out;$('fill').style.width='100%';
      $('doneRow').style.display='flex';}
    else{$('prog').textContent='失败 ❌（见下方日志）';}
  }
}
(async()=>{
  const s=await post('/settings');
  for(const k of ['caption','caption_size','label_before','label_after','scene_sec','transition','slider','direction','comment_user','comment_text','progress_text'])
    if(s[k]!==undefined&&s[k]!=='')$(k).value=s[k];
  if(s.demo_caption)$('demoCaption').value=s.demo_caption;
  if(s.audio){AUDIO=s.audio;$('audioName').textContent=base(AUDIO);$('audioName2').textContent=base(AUDIO);}
  syncReveal();
  wireDrop('B'); wireDrop('A'); paintSlot('B'); paintSlot('A');
  ['dragover','drop'].forEach(ev=>document.addEventListener(ev,e=>e.preventDefault()));
  setInterval(()=>post('/ping'),5000);
})();
</script>
</body></html>"""


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
            body = PAGE.encode()
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
                STATE.update(busy=True, done=False, ok=False, out=None,
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
            self._json(load_settings())
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
            path = pick_video()
            if not path:
                self._json({"path": None})
                return
            with LOCK:
                if STATE["busy"]:
                    self._json({"error": "正在处理中"})
                    return
                STATE.update(busy=True, done=False, ok=False, kind="analyze",
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
                STATE.update(busy=True, done=False, ok=False, kind="screen",
                             out=None, prog_done=0, prog_total=0, lines=[])
            threading.Thread(target=worker_screen,
                             args=(video, plan_d, texts, out, audio),
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
                STATE.update(busy=True, done=False, ok=False, out=None,
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
                STATE.update(busy=True, done=False, ok=False, out=None,
                             kind="demo", prog_done=0, prog_total=0, lines=[])
            save_settings({"demo_caption": d.get("caption", "")})
            threading.Thread(target=worker_demo,
                             args=(photo, result, (d.get("caption") or "").strip(),
                                   out, dopts),
                             daemon=True).start()
            self._json({"ok": True, "out": out})
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
                STATE.update(busy=True, done=False, ok=False, out=None,
                             kind="render", prog_done=0, prog_total=0, lines=[])
            save_settings({k: str(d.get(k, "")) for k in SETTING_KEYS})
            threading.Thread(target=worker, args=(pairs, opts), daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/status":
            with LOCK:
                p = STATE["plan"]
                self._json({
                    "busy": STATE["busy"], "done": STATE["done"], "ok": STATE["ok"],
                    "out": STATE["out"], "done_n": STATE["prog_done"],
                    "total": STATE["prog_total"], "lines": STATE["lines"][-60:],
                    "kind": STATE["kind"],
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


def run_gui():
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
            webview.create_window("CutKit 前后对比视频", url,
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
