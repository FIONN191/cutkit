#!/usr/bin/env python3
"""CutKit 录屏自动剪辑（原 RosieCut）的 GUI 侧逻辑.

把 RosieCut 的预设、叠加素材、自然语言指令和渲染流程整理成模块，
供合并后的单一界面调用；渲染内核仍是原来的 rosiecut.py，行为不变。
"""
import json
import os
import subprocess
import sys
import time
import traceback

import rosiecut
import nl

PRESET_KEYS = ("target", "paint_sec", "type_speed", "wait_sec", "last_wait")
DEFAULT_PRESETS = {
    "标准 15 秒": {"target": "15.5", "paint_sec": "1.67", "type_speed": "9",
                  "wait_sec": "0.8", "last_wait": "0.8"},
    "自动节拍": {"target": "", "paint_sec": "1.67", "type_speed": "9",
                "wait_sec": "0.8", "last_wait": "0.8"},
    "快节奏": {"target": "", "paint_sec": "1.2", "type_speed": "12",
              "wait_sec": "0.6", "last_wait": "0.6"},
}
ASSET_KINDS = {"precomp": ("预合成动画", rosiecut.PRECOMP_NAME),
               "watermark": ("fotor 水印", rosiecut.WATERMARK_NAME)}


def _dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "CutKit")


PRESET_PATH = os.path.join(_dir(), "rosie_presets.json")
ASSETS_PATH = os.path.join(_dir(), "rosie_assets.json")
OVERLAY_PRESET_PATH = os.path.join(_dir(), "rosie_overlay_presets.json")

# 叠加素材预设：两个素材路径 + 两个宽度百分比
OVERLAY_KEYS = ("precomp", "watermark", "precomp_size", "watermark_size")


# ---------- 预设 ----------
def save_presets(d):
    os.makedirs(_dir(), exist_ok=True)
    with open(PRESET_PATH, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def load_presets():
    try:
        with open(PRESET_PATH) as f:
            d = json.load(f)
            if isinstance(d, dict) and d:
                return d
    except Exception:
        pass
    save_presets(DEFAULT_PRESETS)
    return dict(DEFAULT_PRESETS)


def preset_save(name, params):
    p = load_presets()
    p[name] = {k: str(params.get(k, "")) for k in PRESET_KEYS}
    save_presets(p)
    return p


def preset_delete(name):
    p = load_presets()
    p.pop(name, None)
    save_presets(p)
    return p


# ---------- 叠加素材与尺寸 ----------
def _read_assets():
    try:
        with open(ASSETS_PATH) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_assets_file(**kw):
    d = _read_assets()
    for k, v in kw.items():
        if k == "sizes":
            d.setdefault("sizes", {}).update(v)
        else:
            d[k] = v
    os.makedirs(_dir(), exist_ok=True)
    with open(ASSETS_PATH, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def load_asset_overrides():
    d = _read_assets()
    return {k: v for k, v in d.items() if k in ASSET_KINDS and v}


def load_sizes():
    """叠加素材宽度（占画面宽的百分比），记忆上次设置。"""
    sz = {"precomp": rosiecut.PRECOMP_PCT, "watermark": rosiecut.WATERMARK_PCT}
    d = (_read_assets().get("sizes") or {})
    for k in sz:
        try:
            v = float(d.get(k, sz[k]))
            if 1 <= v <= 100:
                sz[k] = v
        except (TypeError, ValueError):
            pass
    return sz


# ---------- 叠加素材预设 ----------
# 与节拍预设分开存：那边是剪辑节奏，这边是素材本身和它在画面里的大小。
def ov_load():
    """返回 {"presets": {名字: {...}}, "last": 最近一次保存的名字}。"""
    try:
        with open(OVERLAY_PRESET_PATH) as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    presets = d.get("presets")
    if not isinstance(presets, dict):
        presets = {}
    last = d.get("last")
    if last not in presets:
        last = ""
    return {"presets": presets, "last": last}


def _ov_write(d):
    os.makedirs(_dir(), exist_ok=True)
    with open(OVERLAY_PRESET_PATH, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def ov_save(name, data):
    """存一份预设，并把它记为「最近一次」——下次打开默认选它。"""
    d = ov_load()
    entry = {}
    for k in OVERLAY_KEYS:
        v = data.get(k)
        if k.endswith("_size"):
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = None
            entry[k] = v if (v is not None and 1 <= v <= 100) else None
        else:
            entry[k] = v if isinstance(v, str) and v else None
    d["presets"][name] = entry
    d["last"] = name
    _ov_write(d)
    return d


def ov_delete(name):
    d = ov_load()
    d["presets"].pop(name, None)
    if d["last"] == name:
        d["last"] = next(iter(d["presets"]), "")
    _ov_write(d)
    return d


def ov_apply(entry):
    """把一份预设落到当前生效的素材与尺寸上。"""
    entry = entry or {}
    kw = {}
    for k in ("precomp", "watermark"):
        if entry.get(k):
            kw[k] = entry[k]
    sizes = {}
    for k, dst in (("precomp_size", "precomp"), ("watermark_size", "watermark")):
        try:
            v = float(entry.get(k))
        except (TypeError, ValueError):
            continue
        if 1 <= v <= 100:
            sizes[dst] = v
    if sizes:
        kw["sizes"] = sizes
    if kw:
        save_assets_file(**kw)
    return {"assets": resolve_assets(), "sizes": load_sizes()}


def resolve_assets():
    over = load_asset_overrides()
    return {k: {"label": label, "path": rosiecut.find_asset(name, over.get(k))}
            for k, (label, name) in ASSET_KINDS.items()}


class Args:
    def __init__(self, target, paint_sec, type_speed, wait_sec, last_wait_sec):
        self.target = target
        self.paint_sec = paint_sec
        self.type_speed = type_speed
        self.wait_sec = wait_sec
        self.last_wait_sec = last_wait_sec


def parse_nl(text, cur, api_key=None):
    """自然语言指令 → 参数覆盖。返回 (params, summary, engine)。"""
    def f(key, default):
        v = str((cur or {}).get(key, "")).strip()
        try:
            return float(v)
        except ValueError:
            return default
    args = Args(f("target", None) if str((cur or {}).get("target", "")).strip() else None,
                f("paint_sec", 1.67), f("type_speed", 9.0),
                f("wait_sec", 0.8), f("last_wait_sec", 0.8))
    overrides, summary, engine = nl.parse(text, api_key or None)
    nl.apply_overrides(args, overrides)
    return ({"target": args.target, "paint_sec": args.paint_sec,
             "type_speed": args.type_speed, "wait_sec": args.wait_sec,
             "last_wait_sec": args.last_wait_sec}, summary, engine)


# ---------- 渲染 ----------
def run_job(src, args, mode="natural", overlay=True, sizes=None, log=None):
    """跑完整的录屏自动剪辑，返回成片路径（行为与 RosieCut 一致）。"""
    log = log or (lambda s: None)
    sz = load_sizes()
    sz.update({k: v for k, v in (sizes or {}).items() if v})

    base = os.path.splitext(os.path.basename(src))[0]
    out_dir = os.path.dirname(src)
    suffix = "_part1.mp4" if mode == "natural" else "_part1_广告.mp4"
    out = rosiecut.unique_path(os.path.join(out_dir, f"{base}{suffix}"))
    cache = os.path.join(out_dir, f"{base}_metrics.npz")

    log(f"分析录屏（首次约 1-2 分钟）: {os.path.basename(src)}")
    metrics, fps, W, H, times = rosiecut.analyze(src, cache, log=log)
    log(f"源分辨率: {W}×{H}")
    runs = rosiecut.classify(metrics, fps)
    segs = rosiecut.plan(runs, fps, args, times)
    total = sum(s["out"] for s in segs)
    log(f"分段计划: {len(segs)} 段, 输出约 {total:.1f}s")
    for s in segs:
        log(f"  {s['kind']:5s} {s['start']:7.2f}-{s['end']:7.2f}s"
            f"  x{s['speed']:<6.2f} -> {s['out']:.2f}s")
    crop = rosiecut.detect_crop(src, runs, fps, W, H)
    log(f"裁剪: {crop[0]}x{crop[1]}+{crop[2]}+{crop[3]}")
    warn = rosiecut.upscale_warning(crop)
    if warn:
        log(warn)

    dims, precomp, watermark = {}, None, None
    if mode == "natural":
        log("自然流版本：测量等待段压暗系数，替换为无 logo 画面…")
        dims = rosiecut.measure_dim(src, segs, metrics)
        log(f"  已替换 {len(dims)} 个等待段（fotor logo/进度条/文字已去除）")
        if overlay:
            a = resolve_assets()
            precomp, watermark = a["precomp"]["path"], a["watermark"]["path"]
            for kind in ("precomp", "watermark"):
                p = a[kind]["path"]
                log(f"  {a[kind]['label']}: "
                    + (os.path.basename(p) if p else "未找到，跳过叠加"))
            if precomp:
                log(f"  预合成动画铺在除末拍外的等待节拍、居中（时长与节拍一致，"
                    f"宽度 {sz['precomp']:.0f}% 画面宽）")
            if watermark:
                log(f"  最后一个等待节拍只放 fotor 水印、居中（宽度 "
                    f"{sz['watermark']:.0f}% 画面宽）")
    else:
        log("广告版本：保留 fotor logo 等待画面")

    log("渲染中…")
    rosiecut.render(src, segs, crop, out, dims=dims, precomp=precomp,
                    watermark=watermark, precomp_pct=sz["precomp"],
                    watermark_pct=sz["watermark"])
    log(f"✅ 完成: {out}")
    return out
