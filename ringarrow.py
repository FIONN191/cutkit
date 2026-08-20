#!/usr/bin/env python3
"""CutKit 圆环箭头角标 — 任意比例的图 → 透明底 MOV，可直接压在原视频上.

复刻竞品「原图圆环 + 手绘箭头指向成片」的角标：照片圆形裁切 + 白色描边圆环 +
向右上弯出的白色渐粗箭头(实心三角头)，带轻微投影，整体在透明背景上。
几何按竞品逐帧实测(圆心 0.214W/0.78H、外半径 0.161W)，全部以半径 R 为基准，
换画布尺寸自动等比缩放。默认静止(与竞品一致)，可选 --pop 弹入动画。
输出 ProRes 4444(带 alpha)，同时另存一张 PNG 方便直接当贴纸用。
"""
import math
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from render import (ffmpeg_exe, _smooth,
                    Cancelled, check_cancel, track_proc, abort_proc,
                    bail_if_cancelled)

SS = 4                      # 超采样倍数（抗锯齿）

# ---------- 几何（以圆环外半径 R 为基准，实测自竞品） ----------
CENTER = (0.214, 0.780)     # 圆心占画布宽/高的比例
RADIUS = 0.161              # 圆环外半径占画布宽的比例
RING_W = 0.065              # 环线宽 = R 的倍数
# 以下全部实测自竞品：短直杆(约37°) + 一个偏长的手绘三角头
ARROW_TAIL = (0.00, -1.04)   # 起笔点（相对圆心，单位 R）
ARROW_CTRL = (0.10, -1.21)   # 杆的贝塞尔控制点（几乎直线，只带一点弧）
HEAD_BASE = (0.36, -1.38)    # 杆末 = 三角头底边中点
ARROW_TIP = (0.95, -1.67)    # 箭头尖
ARROW_W0, ARROW_W1 = 0.050, 0.080   # 杆的半宽：起笔 → 收笔（单位 R）
HEAD_HALF = 0.19                    # 三角头底边半宽（单位 R）
SHADOW_BLUR, SHADOW_OFF, SHADOW_A = 0.10, 0.035, 118   # 投影（单位 R）


def _bez(p0, p1, p2, t):
    u = 1 - t
    return (u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
            u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1])


def _draw_arrow(d, cx, cy, R, color=(255, 255, 255, 255)):
    """杆：沿贝塞尔逐点盖圆点（渐粗）；头：底边在杆末的实心三角。"""
    P = lambda p: (cx + p[0] * R, cy + p[1] * R)
    p0, p1, p2 = P(ARROW_TAIL), P(ARROW_CTRL), P(HEAD_BASE)
    tip = P(ARROW_TIP)
    N = 220
    for i in range(N + 1):
        t = i / N
        x, y = _bez(p0, p1, p2, t)
        w = (ARROW_W0 + (ARROW_W1 - ARROW_W0) * _smooth(t)) * R
        d.ellipse((x - w, y - w, x + w, y + w), fill=color)
    # 三角头：底边中点 = 杆末，朝向 = 杆末 → 尖
    ang = math.atan2(tip[1] - p2[1], tip[0] - p2[0])
    nx, ny = -math.sin(ang), math.cos(ang)
    HW = HEAD_HALF * R
    d.polygon([tip,
               (p2[0] + nx * HW, p2[1] + ny * HW),
               (p2[0] - nx * HW, p2[1] - ny * HW)], fill=color)


def build_badge(photo_path, W=1080, H=1920, center=CENTER, radius=RADIUS,
                crop_x=0.50, crop_y=0.40, log=None):
    """返回整幅画布大小的 RGBA 角标（透明底）。"""
    log = log or (lambda s: None)
    w, h = W * SS, H * SS
    cx, cy = center[0] * w, center[1] * h
    R = radius * w
    ring = RING_W * R

    # 1) 白色图形层（圆环 + 箭头），先单独画好用来生成投影
    shapes = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ds = ImageDraw.Draw(shapes)
    ds.ellipse((cx - R, cy - R, cx + R, cy + R), outline=(255, 255, 255, 255),
               width=int(round(ring)))
    _draw_arrow(ds, cx, cy, R)

    # 2) 投影：白色图形的 alpha 模糊后压暗，向下偏移
    alpha = shapes.split()[3]
    blur = alpha.filter(ImageFilter.GaussianBlur(SHADOW_BLUR * R))
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow.putalpha(blur.point(lambda v: int(v * SHADOW_A / 255)))

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.alpha_composite(shadow, (0, int(SHADOW_OFF * R)))

    # 3) 圆形裁切的照片（任意比例 → 居中方形裁切，纵向偏上取人脸）
    inner = int(R - ring / 2)
    img = ImageOps.exif_transpose(Image.open(photo_path).convert("RGB"))
    img = ImageOps.fit(img, (inner * 2, inner * 2), method=Image.LANCZOS,
                       centering=(crop_x, crop_y))
    mask = Image.new("L", (inner * 2, inner * 2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, inner * 2 - 1, inner * 2 - 1), fill=255)
    img.putalpha(mask)
    out.alpha_composite(img, (int(cx - inner), int(cy - inner)))

    # 4) 白色图形盖在最上（环压住照片边缘）
    out.alpha_composite(shapes)
    log(f"角标: 圆心({cx/SS:.0f},{cy/SS:.0f}) 外半径 {R/SS:.0f}px")
    return out.resize((W, H), Image.LANCZOS)


def render_mov(badge, out_path, dur=8.0, fps=30, pop=False, log=None):
    """把角标写成带 alpha 的 ProRes 4444 MOV（pop=True 时头 0.45s 弹入）。"""
    log = log or (lambda s: None)
    W, H = badge.size
    total = int(round(dur * fps))
    cmd = [ffmpeg_exe(), "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}",
           "-r", str(fps), "-i", "-",
           "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
           "-alpha_bits", "16", "-vendor", "ap4h", out_path]
    log(f"编码 {total} 帧 ProRes 4444 → {out_path}")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    blank = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    POP = 0.45
    broken = False
    try:
        with track_proc(proc):
            for n in range(total):
                check_cancel()
                t = n / fps
                if pop and t < POP:
                    e = _smooth(t / POP)
                    s = 0.35 + 0.65 * e          # 缩放弹入
                    fr = blank.copy()
                    sw, sh = max(2, int(W * s)), max(2, int(H * s))
                    sc = badge.resize((sw, sh), Image.LANCZOS)
                    # 以角标圆心为锚点缩放
                    ax, ay = CENTER[0] * W, CENTER[1] * H
                    fr.alpha_composite(sc, (int(ax - ax * s), int(ay - ay * s)))
                    a = fr.split()[3].point(lambda v: int(v * e))
                    fr.putalpha(a)
                else:
                    fr = badge
                proc.stdin.write(fr.tobytes())
    except BrokenPipeError:
        broken = True
    except Cancelled:
        abort_proc(proc, out_path)
        raise
    try:
        proc.stdin.close()
    except Exception:
        broken = True
    err = proc.stderr.read()
    proc.wait()
    if proc.returncode != 0 or broken:
        bail_if_cancelled(proc, out_path)
        raise RuntimeError("ffmpeg 编码失败:\n" + err.decode("utf-8", "ignore")[-2000:])
    log("编码完成 ✅")
    return out_path


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="CutKit — 圆环箭头角标（透明底 MOV）")
    ap.add_argument("photo", help="要放进圆环的原图（任意比例）")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--size", default="1080x1920", help="画布尺寸，默认 1080x1920")
    ap.add_argument("--dur", type=float, default=8.0, help="时长秒，默认 8")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--x", type=float, default=CENTER[0], help="圆心横向位置 0-1")
    ap.add_argument("--y", type=float, default=CENTER[1], help="圆心纵向位置 0-1")
    ap.add_argument("--radius", type=float, default=RADIUS, help="外半径占宽比例")
    ap.add_argument("--crop-x", type=float, default=0.50, help="方形裁切横向重心 0-1")
    ap.add_argument("--crop-y", type=float, default=0.40, help="方形裁切纵向重心 0-1")
    ap.add_argument("--pop", action="store_true", help="开头 0.45s 弹入动画")
    args = ap.parse_args(argv)

    W, H = (int(v) for v in args.size.lower().split("x"))
    out = args.out or os.path.splitext(args.photo)[0] + "-圆环箭头.mov"
    badge = build_badge(args.photo, W, H, center=(args.x, args.y),
                        radius=args.radius, crop_x=args.crop_x,
                        crop_y=args.crop_y, log=print)
    png = os.path.splitext(out)[0] + ".png"
    badge.save(png)
    print("PNG", png)
    render_mov(badge, out, dur=args.dur, fps=args.fps, pop=args.pop, log=print)
    print("OK", out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
