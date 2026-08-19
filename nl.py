#!/usr/bin/env python3
"""Natural-language → editing parameters for RosieCut.

Two backends, same output shape:
  parse(text, api_key=None) -> (overrides: dict, summary: str, engine: str)

overrides may contain any of: target (float|None), paint_sec, type_speed,
wait_sec, last_wait_sec. Only keys the instruction actually mentions are set;
the caller merges them onto the current values.

Offline keyword parser (Chinese + English) runs with no network. If an
Anthropic API key is supplied, an open-ended parse via the Messages API
(structured output through forced tool use) is tried first, falling back to
the offline parser on any error.
"""
import json
import re
import urllib.request

PARAM_KEYS = ("target", "paint_sec", "type_speed", "wait_sec", "last_wait_sec")

CN_DIGITS = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}


# ---------------------------------------------------------------- offline
def _num_after(text, keywords, unit=r"(?:秒|s|倍|x|X)?"):
    """Find a number that appears near any of `keywords` (keyword then number,
    within a short span). Returns float or None. Handles arabic + simple CN."""
    kw = "|".join(keywords)
    # arabic (with optional decimal), allowing a few chars between kw and number
    m = re.search(rf"(?:{kw})[^0-9\n]{{0,6}}?([0-9]+(?:\.[0-9]+)?)\s*{unit}", text)
    if m:
        return float(m.group(1))
    # number then keyword (e.g. "2秒的涂抹")
    m = re.search(rf"([0-9]+(?:\.[0-9]+)?)\s*{unit}[^0-9\n]{{0,4}}?(?:{kw})", text)
    if m:
        return float(m.group(1))
    # simple Chinese numerals: 两秒 / 三倍 / 十秒. Require an explicit unit so
    # "长一点" / "快一点" (一 = "a bit", not a quantity) don't false-match.
    unit_req = unit[:-1] if unit.endswith("?") else unit
    if unit_req:
        m = re.search(rf"(?:{kw})[^0-9\n]{{0,6}}?([零一两二三四五六七八九十]+)\s*{unit_req}", text)
        if m:
            return _cn_to_num(m.group(1))
    return None


def _cn_to_num(s):
    if s in CN_DIGITS:
        return float(CN_DIGITS[s])
    if "十" in s:  # 十, 十五, 二十, 二十五
        a, _, b = s.partition("十")
        tens = CN_DIGITS.get(a, 1) if a else 1
        ones = CN_DIGITS.get(b, 0) if b else 0
        return float(tens * 10 + ones)
    total = 0.0
    for ch in s:
        total += CN_DIGITS.get(ch, 0)
    return total or None


def parse_offline(text):
    """Rule-based parse. Returns (overrides, summary)."""
    t = text.strip()
    ov, notes = {}, []

    # total duration
    if re.search(r"不固定|别固定|自动|不限|随意|auto", t, re.I) and \
       re.search(r"总|时长|长度|target|duration", t, re.I):
        ov["target"] = None
        notes.append("总时长改为自动（按段节拍）")
    else:
        v = _num_after(t, [r"总时长", r"总长", r"目标时长", r"总共", r"整体",
                           r"target", r"duration", r"总"])
        if v is not None and re.search(r"总|时长|长度|target|duration", t, re.I):
            ov["target"] = v
            notes.append(f"总时长 {v:g}s")

    # painting shot length
    v = _num_after(t, [r"涂抹段", r"涂抹", r"片头", r"每段", r"painting", r"paint"])
    if v is not None:
        ov["paint_sec"] = v
        notes.append(f"每个涂抹段 {v:g}s")
    elif re.search(r"涂抹|片头|painting", t, re.I):
        if re.search(r"长一?点|久一?点|慢一?点|longer|slower", t, re.I):
            ov["paint_sec"] = ("+", 0.3)
            notes.append("涂抹段加长 0.3s")
        elif re.search(r"短一?点|快一?点|shorter|faster", t, re.I):
            ov["paint_sec"] = ("-", 0.3)
            notes.append("涂抹段缩短 0.3s")

    # typing speed
    v = _num_after(t, [r"打字", r"typing", r"type"], unit=r"(?:倍|x|X)")
    if v is not None:
        ov["type_speed"] = v
        notes.append(f"打字 {v:g}x")
    elif re.search(r"打字|typing", t, re.I):
        if re.search(r"快一?点|再快|更快|faster", t, re.I):
            ov["type_speed"] = ("*", 1.3)
            notes.append("打字更快 (×1.3)")
        elif re.search(r"慢一?点|再慢|更慢|slower", t, re.I):
            ov["type_speed"] = ("*", 1 / 1.3)
            notes.append("打字更慢 (÷1.3)")

    # wait beat
    v = _num_after(t, [r"等待", r"生成", r"过渡", r"节拍", r"wait"])
    if v is not None and not re.search(r"收尾|结尾|最后|ending|last", t, re.I):
        ov["wait_sec"] = v
        notes.append(f"等待节拍 {v:g}s")

    # ending beat
    v = _num_after(t, [r"收尾", r"结尾", r"最后", r"结束", r"ending", r"last"])
    if v is not None:
        ov["last_wait_sec"] = v
        notes.append(f"收尾 {v:g}s")

    summary = "，".join(notes) if notes else "没识别到可调整的指令"
    return ov, summary


# ---------------------------------------------------------------- Claude API
_TOOL = {
    "name": "set_edit_params",
    "description": "Set the video-editing parameters the user asked to change. "
                   "Only include a field if the user's instruction changes it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "target": {"type": ["number", "null"],
                       "description": "total output duration in seconds; null = "
                                      "auto (each painting shot uses paint_sec)"},
            "paint_sec": {"type": "number",
                          "description": "output seconds per painting/smear shot"},
            "type_speed": {"type": "number",
                           "description": "playback speed multiplier for typing shots"},
            "wait_sec": {"type": "number",
                         "description": "output seconds per generating-wait beat"},
            "last_wait_sec": {"type": "number",
                              "description": "output seconds for the final wait beat"},
        },
        "additionalProperties": False,
    },
}

_SYS = ("你把中文/英文的视频剪辑口语指令解析成参数，只调用 set_edit_params 工具。"
        "只填用户明确要改的字段，没提到的不要填。单位：秒。"
        "打字倍速是播放加速倍数（如 9 表示 9 倍速）。"
        "涂抹段=展示涂抹换装的每个镜头时长。等待节拍=生成加载过渡的时长。"
        "若用户说总时长不固定/自动，target 传 null。")


def parse_llm(text, api_key):
    body = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 512,
        "system": _SYS,
        "tools": [_TOOL],
        "tool_choice": {"type": "tool", "name": "set_edit_params"},
        "messages": [{"role": "user", "content": text}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json",
                 "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    for block in data.get("content", []):
        if block.get("type") == "tool_use":
            ov = {k: v for k, v in block["input"].items() if k in PARAM_KEYS}
            notes = []
            for k, v in ov.items():
                notes.append(f"{k}={'自动' if v is None else v}")
            return ov, "，".join(notes) if notes else "没识别到可调整的指令"
    raise ValueError("no tool_use in response")


# ---------------------------------------------------------------- entry
def parse(text, api_key=None):
    if api_key:
        try:
            ov, summary = parse_llm(text, api_key.strip())
            return ov, summary, "claude"
        except Exception as e:  # network / auth / parse -> fall back offline
            ov, summary = parse_offline(text)
            return ov, summary + f"（AI 解析失败，用离线：{type(e).__name__}）", "offline"
    ov, summary = parse_offline(text)
    return ov, summary, "offline"


def apply_overrides(args, overrides):
    """Merge overrides (possibly relative tuples) onto an _Args-like object."""
    for k, v in overrides.items():
        if isinstance(v, tuple):  # relative op
            op, amt = v
            cur = getattr(args, k, 0) or 0
            if op == "+":
                nv = cur + amt
            elif op == "-":
                nv = max(cur - amt, 0.1)
            elif op == "*":
                nv = cur * amt
            else:
                nv = cur
            setattr(args, k, round(nv, 2))
        else:
            setattr(args, k, v)
    return args
