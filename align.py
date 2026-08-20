#!/usr/bin/env python3
"""CutKit 前后图对齐 — 把 After 图对齐到 Before 图，让人物 1:1 重合.

两张图常常比例不同、人物被 AI 重绘后有位移/缩放，直接各自居中裁切会错位。
这里估计一个相似变换（缩放 + 平移 + 小角度旋转），把 After 摆到与 Before 同位。

三种策略按顺序尝试，各自打分后取最优（都失败就退回不变换）：
  1. 特征匹配 SIFT + RANSAC —— 人物像素基本保留时（换背景类滤镜）最准
  2. ECC 梯度对齐 —— 人物被重绘、只有轮廓相近时仍能收敛
  3. 相位相关 —— 只解平移，前两者都崩时的保底
打分用边缘图的归一化互相关（NCC），对颜色/风格变化不敏感。
"""
import math

import cv2
import numpy as np
from PIL import Image, ImageOps

WORK_W = 480          # 估计时的工作宽度（卡片空间）
MAX_SCALE = 2.5       # 允许的缩放范围
MIN_SCALE = 0.4
MAX_ROT = 12.0        # 允许的旋转角度（度）


def _to_gray(pil_img, size):
    a = np.asarray(pil_img.convert("L").resize(size, Image.LANCZOS))
    return a.astype(np.float32)


def _edges(g):
    """梯度幅值图 —— 抹掉颜色/风格差异，只留结构。"""
    g = cv2.GaussianBlur(g, (0, 0), 1.6)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    m = cv2.magnitude(gx, gy)
    m = cv2.normalize(m, None, 0, 1, cv2.NORM_MINMAX)
    return m


def _ncc(a, b, mask=None):
    """归一化互相关，[-1,1]，越大越像。"""
    if mask is not None:
        a, b = a[mask], b[mask]
    else:
        a, b = a.ravel(), b.ravel()
    if a.size < 64:
        return -1.0
    a = a - a.mean()
    b = b - b.mean()
    d = (np.sqrt((a * a).sum()) * np.sqrt((b * b).sum()))
    return float((a * b).sum() / d) if d > 1e-9 else -1.0


def _score(eb, ea, M):
    """把 ea 按 M 变换后与 eb 比对；只在有效区域内算分。"""
    h, w = eb.shape
    warped = cv2.warpAffine(ea, M, (w, h), flags=cv2.INTER_LINEAR,
                            borderValue=0)
    valid = cv2.warpAffine(np.ones_like(ea), M, (w, h),
                           flags=cv2.INTER_NEAREST, borderValue=0) > 0.5
    # 边界一圈不算分，避免变换后空白区拉高相关
    pad = max(4, int(0.04 * w))
    inner = np.zeros_like(valid)
    inner[pad:h - pad, pad:w - pad] = True
    m = valid & inner
    if m.sum() < 0.25 * h * w:
        return -1.0
    return _ncc(eb, warped, m)


def _sane(M):
    """变换是否在合理范围内（防止匹配到离谱的解）。"""
    if M is None:
        return False
    a, b = M[0, 0], M[0, 1]
    s = math.hypot(a, b)
    if not (MIN_SCALE <= s <= MAX_SCALE):
        return False
    rot = abs(math.degrees(math.atan2(b, a)))
    return rot <= MAX_ROT


# ---------- 策略 1: 特征匹配 ----------
def _by_features(gb, ga):
    try:
        sift = cv2.SIFT_create(nfeatures=1500)
    except Exception:
        return None
    b8 = cv2.normalize(gb, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    a8 = cv2.normalize(ga, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    kb, db = sift.detectAndCompute(b8, None)
    ka, da = sift.detectAndCompute(a8, None)
    if db is None or da is None or len(kb) < 8 or len(ka) < 8:
        return None
    matcher = cv2.BFMatcher()
    pairs = matcher.knnMatch(da, db, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2)
            if m.distance < 0.75 * n.distance]
    if len(good) < 8:
        return None
    src = np.float32([ka[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kb[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    M, inl = cv2.estimateAffinePartial2D(
        src, dst, method=cv2.RANSAC, ransacReprojThreshold=4.0,
        maxIters=4000, confidence=0.995)
    if M is None or inl is None or int(inl.sum()) < 8:
        return None
    return M.astype(np.float32)


# ---------- 策略 2: ECC 梯度对齐 ----------
def _by_ecc(eb, ea, init=None):
    M = np.eye(2, 3, dtype=np.float32) if init is None else init.copy()
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6)
    try:
        cv2.findTransformECC(eb, ea, M, cv2.MOTION_EUCLIDEAN, crit, None, 5)
    except cv2.error:
        return None
    return M


# ---------- 策略 3: 相位相关（只解平移）----------
def _by_phase(gb, ga):
    try:
        win = cv2.createHanningWindow((gb.shape[1], gb.shape[0]), cv2.CV_32F)
        (dx, dy), resp = cv2.phaseCorrelate(np.ascontiguousarray(ga),
                                            np.ascontiguousarray(gb), win)
    except cv2.error:
        return None
    if resp < 0.02:
        return None
    return np.array([[1, 0, dx], [0, 1, dy]], dtype=np.float32)


def fit_card(img, card):
    """与渲染端一致的 cover 裁切（ImageOps.fit 居中）。"""
    return ImageOps.fit(ImageOps.exif_transpose(img).convert("RGB"), card,
                        method=Image.LANCZOS, centering=(0.5, 0.5))


def _fit_matrix(w, h, card):
    """cover 裁切的仿射矩阵：源像素 → 卡片像素。"""
    CW, CH = card
    k = max(CW / w, CH / h)
    return np.array([[k, 0, -(k * w - CW) / 2],
                     [0, k, -(k * h - CH) / 2]], dtype=np.float32)


def _compose(A, B):
    """A ∘ B（都是 2x3 仿射）。"""
    A3 = np.vstack([A, [0, 0, 1]])
    B3 = np.vstack([B, [0, 0, 1]])
    return (A3 @ B3)[:2].astype(np.float32)


def estimate(before_pil, after_pil, card=(1080, 1920), log=None):
    """在卡片空间估计把 after 对齐到 before 的相似变换。

    两张图先按渲染端同样的方式 cover 裁切进卡片，再估计变换 —— 这样
    「比例不同导致的裁切差异」也被一并解掉。
    返回 dict(scale, dx, dy, rot, method, score, gain, M)；
    M 是卡片空间的 2x3 仿射（after 卡片 → before 卡片）。
    """
    log = log or (lambda s: None)
    CW, CH = card
    sw = WORK_W
    sh = max(2, int(round(WORK_W * CH / CW)))
    bc = fit_card(before_pil, card).resize((sw, sh), Image.LANCZOS)
    ac = fit_card(after_pil, card).resize((sw, sh), Image.LANCZOS)
    gb = np.asarray(bc.convert("L")).astype(np.float32)
    ga = np.asarray(ac.convert("L")).astype(np.float32)
    eb, ea = _edges(gb), _edges(ga)

    ident = np.eye(2, 3, dtype=np.float32)
    base = _score(eb, ea, ident)
    cands = []

    Mf = _by_features(gb, ga)
    if _sane(Mf):
        cands.append(("features", Mf, _score(eb, ea, Mf)))
        Me = _by_ecc(eb, ea, Mf)
        if _sane(Me):
            cands.append(("features+ecc", Me, _score(eb, ea, Me)))
    Me0 = _by_ecc(eb, ea, None)
    if _sane(Me0):
        cands.append(("ecc", Me0, _score(eb, ea, Me0)))
    Mp = _by_phase(gb, ga)
    if _sane(Mp):
        cands.append(("phase", Mp, _score(eb, ea, Mp)))

    none = dict(scale=1.0, dx=0.0, dy=0.0, rot=0.0, method="none",
                score=base, gain=0.0, M=ident)
    if not cands:
        log("对齐：没找到可靠的变换，保持原样")
        return none
    method, M, score = max(cands, key=lambda c: c[2])
    if score <= base + 0.005:
        log(f"对齐：两张图已经基本对齐（NCC {base:.3f}），不动")
        return none

    # 工作分辨率 → 卡片分辨率
    r = CW / sw
    Mc = M.copy()
    Mc[0, 2] *= r
    Mc[1, 2] *= r
    a, b = float(M[0, 0]), float(M[0, 1])
    sc = math.hypot(a, b)
    rot = math.degrees(math.atan2(b, a))
    log(f"对齐：{method}  缩放 {sc:.3f}  位移 ({Mc[0,2]:+.0f}px, {Mc[1,2]:+.0f}px)"
        f"  旋转 {rot:+.2f}°  NCC {base:.3f} → {score:.3f}")
    return dict(scale=sc, dx=float(Mc[0, 2]), dy=float(Mc[1, 2]), rot=rot,
                method=method, score=score, gain=score - base, M=Mc)


def render_aligned(after_pil, card, info, nudge=None):
    """按估计结果（可叠加手动微调）把 after 原图直接采样进卡片。

    直接从原图做一次仿射，避免「先裁切再变换」的二次插值损失；
    超出源图的部分用边缘延展填充。nudge = dict(scale, dx, dy)，dx/dy 单位是卡片像素。
    """
    img = ImageOps.exif_transpose(after_pil).convert("RGB")
    CW, CH = card
    M = _compose(info.get("M", np.eye(2, 3, dtype=np.float32)),
                 _fit_matrix(img.width, img.height, card))
    if nudge:
        ns = float(nudge.get("scale", 1.0) or 1.0)
        ndx = float(nudge.get("dx", 0.0) or 0.0)
        ndy = float(nudge.get("dy", 0.0) or 0.0)
        # 以卡片中心为锚点缩放，再平移
        cx, cy = CW / 2.0, CH / 2.0
        N = np.array([[ns, 0, cx - ns * cx + ndx],
                      [0, ns, cy - ns * cy + ndy]], dtype=np.float32)
        M = _compose(N, M)
    out = cv2.warpAffine(np.asarray(img), M, (CW, CH),
                         flags=cv2.INTER_LANCZOS4,
                         borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(out)


def _covered_rect(M, card):
    """after 经 M 变换后在卡片内实际覆盖到的内接矩形。"""
    CW, CH = card
    pts = np.array([[0, 0], [CW, 0], [CW, CH], [0, CH]], dtype=np.float32)
    tp = (np.hstack([pts, np.ones((4, 1), np.float32)]) @ M.T)
    x0 = max(0.0, float(max(tp[0, 0], tp[3, 0])))
    x1 = min(float(CW), float(min(tp[1, 0], tp[2, 0])))
    y0 = max(0.0, float(max(tp[0, 1], tp[1, 1])))
    y1 = min(float(CH), float(min(tp[2, 1], tp[3, 1])))
    if x1 - x0 < CW * 0.35 or y1 - y0 < CH * 0.35:
        return (0.0, 0.0, float(CW), float(CH))
    return (x0, y0, x1, y1)


def align_pair(before_pil, after_pil, card=(1080, 1920), info=None,
               fill=True, nudge=None, log=None):
    """把一对前后图对齐后各自渲染成卡片，返回 (before_card, after_card, info).

    fill=True（默认）会把两张图一起裁到「共同覆盖区域」再铺满卡片 —— 对齐
    的同时不留边缘填充痕迹，代价是构图整体略微放大。
    fill=False 则保留 before 的完整构图，after 不足处用边缘延展补。
    """
    if info is None:
        info = estimate(before_pil, after_pil, card, log=log)
    CW, CH = card
    M = info.get("M", np.eye(2, 3, dtype=np.float32)).astype(np.float32)

    if nudge:
        ns = float(nudge.get("scale", 1.0) or 1.0)
        cx, cy = CW / 2.0, CH / 2.0
        N = np.array([[ns, 0, cx - ns * cx + float(nudge.get("dx", 0) or 0)],
                      [0, ns, cy - ns * cy + float(nudge.get("dy", 0) or 0)]],
                     dtype=np.float32)
        M = _compose(N, M)

    C = np.eye(2, 3, dtype=np.float32)
    if fill and info.get("method") != "none":
        x0, y0, x1, y1 = _covered_rect(M, card)
        k = min(CW / (x1 - x0), CH / (y1 - y0))
        C = np.array([[k, 0, -k * x0 + (CW - k * (x1 - x0)) / 2],
                      [0, k, -k * y0 + (CH - k * (y1 - y0)) / 2]],
                     dtype=np.float32)

    b_src = ImageOps.exif_transpose(before_pil).convert("RGB")
    a_src = ImageOps.exif_transpose(after_pil).convert("RGB")
    Mb = _compose(C, _fit_matrix(b_src.width, b_src.height, card))
    Ma = _compose(C, _compose(M, _fit_matrix(a_src.width, a_src.height, card)))
    kw = dict(flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
    bc = Image.fromarray(cv2.warpAffine(np.asarray(b_src), Mb, (CW, CH), **kw))
    ac = Image.fromarray(cv2.warpAffine(np.asarray(a_src), Ma, (CW, CH), **kw))
    return bc, ac, info
