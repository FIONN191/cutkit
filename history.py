#!/usr/bin/env python3
"""CutKit 生成历史 — 记录每次产出的成片，带缩略图，可回看/定位/复制到剪映素材夹。

剪映的草稿素材表(draft_meta_info.json)是加密的，且 剪映专业版 没有声明任何
document type（无法 `open -a` 递文件），所以没法程序化写进它的素材库。这里的
做法是把成片同时放进一个固定的「剪映素材」文件夹 —— 在剪映里 导入→素材 定位
一次之后，以后每次打开文件框都停在这，等于一步可取。
"""
import json
import os
import subprocess
import time
import uuid

from render import ffmpeg_exe

MODE_LABEL = {
    "render": "前后对比", "screen": "录屏步骤", "demo": "拖照片演示",
    "ring": "圆环箭头", "": "其他",
}


def _root():
    import sys
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "CutKit")


HISTORY_PATH = os.path.join(_root(), "history.json")
THUMB_DIR = os.path.join(_root(), "thumbs")

# 默认的剪映素材落地文件夹（用户可改）
def default_jy_dir():
    return os.path.join(os.path.expanduser("~/Movies"), "CutKit 素材")


def jianying_installed():
    """剪映专业版是否装着（bundle id com.lemon.lvpro）。"""
    import plistlib
    for p in ("/Applications/VideoFusion-macOS.app",
              os.path.expanduser("~/Applications/VideoFusion-macOS.app")):
        f = os.path.join(p, "Contents", "Info.plist")
        try:
            if plistlib.load(open(f, "rb")).get("CFBundleIdentifier") == "com.lemon.lvpro":
                return p
        except Exception:
            continue
    return None


def load():
    try:
        with open(HISTORY_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save(items):
    os.makedirs(_root(), exist_ok=True)
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, HISTORY_PATH)


def probe(path):
    """返回 (时长秒, 宽, 高)，读不到就给 (0,0,0)。"""
    import re
    r = subprocess.run([ffmpeg_exe(), "-i", path], capture_output=True, text=True)
    s = r.stderr
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", s)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    w = h = 0
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", s)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    return dur, w, h


def make_thumb(path, rec_id, transparent=False):
    """抽一帧当封面。透明素材铺深色棋盘格，不然看着全黑。"""
    os.makedirs(THUMB_DIR, exist_ok=True)
    out = os.path.join(THUMB_DIR, f"{rec_id}.jpg")
    dur, _, _ = probe(path)
    ss = max(0.0, dur * 0.45)
    if transparent or path.lower().endswith(".mov"):
        vf = ("scale=240:-1,format=rgba,"
              "split[a][b];[a]drawbox=c=0x2b2b33:t=fill[bg];[bg][b]overlay")
    else:
        vf = "scale=240:-1"
    r = subprocess.run(
        [ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{ss:.2f}", "-i", path,
         "-frames:v", "1", "-vf", vf, "-q:v", "4", out],
        capture_output=True)
    if r.returncode != 0 or not os.path.exists(out):
        r = subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-i", path,
                            "-frames:v", "1", "-vf", "scale=240:-1", "-q:v", "4", out],
                           capture_output=True)
    return out if os.path.exists(out) else None


def add(path, mode="", note="", jy_dir=None):
    """登记一条历史；jy_dir 非空则同时把成片拷进剪映素材文件夹。"""
    if not path or not os.path.isfile(path):
        return None
    rec_id = uuid.uuid4().hex[:12]
    dur, w, h = probe(path)
    transparent = path.lower().endswith(".mov")
    thumb = make_thumb(path, rec_id, transparent)
    copied = None
    if jy_dir:
        try:
            copied = copy_to(path, jy_dir)
        except Exception:
            copied = None
    rec = {
        "id": rec_id, "path": path, "name": os.path.basename(path),
        "mode": mode, "note": note, "ts": time.time(),
        "size": os.path.getsize(path), "dur": round(dur, 2),
        "w": w, "h": h, "transparent": transparent,
        "thumb": thumb, "jy_path": copied,
    }
    items = load()
    items.insert(0, rec)
    _save(items[:300])
    return rec


def copy_to(path, folder):
    """拷到剪映素材文件夹，重名自动加序号。返回新路径。"""
    import shutil
    os.makedirs(folder, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(path))
    dst = os.path.join(folder, base + ext)
    i = 2
    while os.path.exists(dst):
        dst = os.path.join(folder, f"{base}-{i}{ext}")
        i += 1
    shutil.copy2(path, dst)
    return dst


def remove(rec_id, delete_file=False):
    items = load()
    keep, gone = [], None
    for r in items:
        if r.get("id") == rec_id:
            gone = r
        else:
            keep.append(r)
    if gone:
        _save(keep)
        for p in (gone.get("thumb"),):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        if delete_file:
            for p in (gone.get("path"), gone.get("jy_path")):
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
    return gone is not None


def prune():
    """清掉源文件已经不在的记录。"""
    items = load()
    keep = [r for r in items if r.get("path") and os.path.exists(r["path"])]
    if len(keep) != len(items):
        _save(keep)
    return len(items) - len(keep)
