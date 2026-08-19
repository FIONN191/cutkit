#!/usr/bin/env python3
"""Pass 1: scan a Fotor magic-edit screen recording and dump per-frame metrics.

Metrics (per frame, computed on a 1/4-scale grayscale image):
  kb_mean   - mean brightness of the keyboard zone (bottom band)
  kb_diff   - mean abs diff vs previous frame, keyboard zone (slide animation -> spike)
  ph_diff   - mean abs diff vs previous frame, photo zone (painting activity)
  all_mean  - mean brightness of whole frame (page-change detection)
  all_diff  - mean abs diff vs previous frame, whole frame
Saved to <out>.npz together with fps / frame size.
"""
import sys
import cv2
import numpy as np

def main(path, out):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        sys.exit(f"cannot open {path}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    sw, sh = W // 4, H // 4
    # zones in downscaled coords (tuned for 1170x2532 iPhone recording)
    kb = (slice(int(sh * 0.66), int(sh * 0.92)), slice(int(sw * 0.05), int(sw * 0.95)))
    ph = (slice(int(sh * 0.12), int(sh * 0.55)), slice(int(sw * 0.15), int(sw * 0.85)))
    rows = []
    prev = None
    n = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        g = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (sw, sh),
                       interpolation=cv2.INTER_AREA).astype(np.int16)
        kb_mean = float(g[kb].mean())
        all_mean = float(g.mean())
        if prev is None:
            kb_diff = ph_diff = all_diff = 0.0
        else:
            d = np.abs(g - prev)
            kb_diff = float(d[kb].mean())
            ph_diff = float(d[ph].mean())
            all_diff = float(d.mean())
        rows.append((kb_mean, kb_diff, ph_diff, all_mean, all_diff))
        prev = g
        n += 1
        if n % 2000 == 0:
            print(f"  {n} frames...", flush=True)
    cap.release()
    a = np.array(rows, dtype=np.float32)
    np.savez(out, metrics=a, fps=fps, width=W, height=H)
    print(f"done: {n} frames @ {fps:.2f}fps  {W}x{H} -> {out}.npz")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
