"""Append an alpha ending over a frozen copy of the comparison's final frame."""
import math
import os
import re
import sys

from render import ffmpeg_exe, run_tracked


def bundled_ending():
    root = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "assets", "ad-ending.webm")


def media_info(path):
    if not os.path.isfile(path):
        raise ValueError("广告结尾素材不存在: " + path)
    result = run_tracked([ffmpeg_exe(), "-hide_banner", "-i", path])
    info = result.stderr.decode("utf-8", "replace")
    duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if not duration or "Video:" not in info:
        raise ValueError("无法读取广告结尾视频: " + path)
    h, m, s = map(float, duration.groups())
    return {"duration": h * 3600 + m * 60 + s,
            "audio": "Audio:" in info, "vp9": bool(re.search(r"Video:\s*vp9", info))}


def append_ending(body, ending, out_path, body_duration, width=1080, height=1920,
                  fps=30, log=None, info=None):
    """Keep the full body, then freeze its last decoded frame under the ending.

    Body audio stops at the cut; the ending's own audio starts at that exact cut.
    Missing audio on either side is padded with silence, never a shorter video.
    """
    log = log or (lambda *_: None)
    info = info or media_info(ending)
    frames = max(1, round(info["duration"] * fps))
    ending_duration = frames / fps
    if not math.isfinite(body_duration) or body_duration <= 0:
        raise ValueError("对比视频时长无效")
    body_info = media_info(body)
    total = body_duration + ending_duration
    log(f"广告结尾：最后一帧定格 {ending_duration:.2f}s，叠加透明动画及原声")
    cmd = [ffmpeg_exe(), "-y", "-v", "error", "-i", body]
    # FFmpeg's native VP9 decoder drops alpha; libvpx preserves it.
    if info["vp9"]:
        cmd += ["-c:v", "libvpx-vp9"]
    cmd += ["-i", ending]
    filters = [
        f"[0:v]setpts=PTS-STARTPTS,fps={fps},tpad=stop_mode=clone:stop_duration={ending_duration:.9f}[base]",
        f"[1:v]setpts=PTS-STARTPTS,scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"format=rgba,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black@0,"
        f"setsar=1,fps={fps},setpts=PTS+{body_duration:.9f}/TB[ending]",
        f"[base][ending]overlay=0:0:eof_action=pass:format=auto:"
        f"enable='gte(t,{body_duration:.9f})'[video]",
    ]
    for index, present, duration, label in (
            (0, body_info["audio"], body_duration, "body_audio"),
            (1, info["audio"], ending_duration, "ending_audio")):
        source = f"[{index}:a]" if present else "anullsrc=r=48000:cl=stereo,"
        filters.append(source + f"atrim=duration={duration:.9f},asetpts=PTS-STARTPTS,"
                       "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                       f"apad=whole_dur={duration:.9f},atrim=duration={duration:.9f}[{label}]")
    filters.append("[body_audio][ending_audio]concat=n=2:v=0:a=1[audio]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "[video]", "-map", "[audio]",
            "-t", f"{total:.9f}", "-r", str(fps), "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path]
    result = run_tracked(cmd, out_path=out_path)
    if result.returncode:
        if os.path.isfile(out_path):
            os.remove(out_path)
        raise RuntimeError("广告结尾合成失败:\n" + result.stderr.decode("utf-8", "replace")[-2000:])
    return out_path
