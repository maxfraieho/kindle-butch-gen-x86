"""Geometric bubble-shape classification (TASK-67, free tier).

Rigaud et al. 2014: balloon outlines separate into smooth (speech),
spiky (shout/burst) and wavy (thought) by the variance of the contour's
deviation from its idealized smooth shape. This module implements that
sigma analysis over the BALLOON CONTOUR - which our pipeline does not
have directly (mask_refined is the text-glyph mask, see TASK-67 notes),
so the contour is recovered first: flood-fill the light balloon interior
from the bubble box center on the CLEANED page (post-inpaint - glyphs
gone, outline intact), then trace its outer contour.

Pure cv2/numpy, no model call - free for every user by design.
Thresholds are module constants; classify() always returns the raw
sigma/spike numbers so live runs can calibrate them.
"""
import cv2
import numpy as np

LIGHT_THRESHOLD = 200      # balloon interiors are near-white
SIGMA_SPEECH_MAX = 0.018   # sigma below this = smooth outline
SIGMA_SHOUT_MIN = 0.045    # above this + many spikes = burst
SPIKES_SHOUT_MIN = 8
SPIKES_THOUGHT_MIN = 4     # moderate sigma + rounded humps = thought
ASPECT_SFX_WIDE = 4.0      # extreme aspect ratios = SFX candidate
ASPECT_SFX_TALL = 0.25


def classify_bubble_shape(gray, box):
    """gray: full-page grayscale np array (cleaned page preferred).
    box: [x1, y1, x2, y2] in this image's pixel space.
    Returns dict: bubble_class (speech|shout|thought|sfx_candidate|
    uncertain), sigma, spikes, contour_closed, confidence."""
    H, W = gray.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in box]
    w, h = x2 - x1, y2 - y1
    if w <= 4 or h <= 4:
        return _res("uncertain", 0, 0, False, 0.0)
    aspect = w / h
    if aspect > ASPECT_SFX_WIDE or aspect < ASPECT_SFX_TALL:
        return _res("sfx_candidate", 0, 0, False, 0.7)

    # The balloon outline usually sits well OUTSIDE the text box (the
    # balloon is 1.5-2x the text extent) - a single tight window makes
    # the flood hit the window border before it ever meets the outline,
    # falsely classifying closed bubbles as open (first live calibration:
    # 75% false sfx_candidate). Escalate the window until the flooded
    # region closes, and only then call it genuinely open.
    region = None
    touches = True
    for margin_ratio in (0.4, 0.8, 1.4):
        margin = int(max(w, h) * margin_ratio)
        wx1, wy1 = max(0, x1 - margin), max(0, y1 - margin)
        wx2, wy2 = min(W, x2 + margin), min(H, y2 + margin)
        win = gray[wy1:wy2, wx1:wx2]
        light = (win > LIGHT_THRESHOLD).astype(np.uint8)
        # Morphological opening of the LIGHT mask = closing of the dark
        # outline: seals hairline gaps in the balloon border (tail
        # openings, scan artifacts) so the flood can't leak through a
        # 1-2px crack and falsely mark a closed bubble as open.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        light = cv2.morphologyEx(light, cv2.MORPH_OPEN, kernel)

        # Seed at the box center; if it lands on a leftover dark pixel,
        # probe a small neighborhood before giving up.
        sy, sx = (y1 + y2) // 2 - wy1, (x1 + x2) // 2 - wx1
        seed = None
        for dy, dx in [(0, 0), (-6, 0), (6, 0), (0, -6), (0, 6), (-12, -12), (12, 12)]:
            py, px = sy + dy, sx + dx
            if 0 <= py < light.shape[0] and 0 <= px < light.shape[1] and light[py, px]:
                seed = (py, px)
                break
        if seed is None:
            return _res("uncertain", 0, 0, False, 0.2)

        num, labels = cv2.connectedComponents(light, connectivity=4)
        region = (labels == labels[seed]).astype(np.uint8)
        touches = (region[0, :].any() or region[-1, :].any()
                   or region[:, 0].any() or region[:, -1].any())
        if not touches:
            break

    contours, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return _res("uncertain", 0, 0, False, 0.2)
    cnt = max(contours, key=cv2.contourArea)
    if cv2.contourArea(cnt) < 0.3 * w * h:
        # Interior didn't flood most of the box - probably no real
        # balloon around this text (caption on art, sign, SFX).
        return _res("sfx_candidate", 0, 0, not touches, 0.5)
    if touches:
        # Open region: distinguish rectangular CAPTION/narration boxes
        # (very common in seinen like frieren - panel-edge-attached
        # rectangles) from free-floating SFX. A caption's flooded
        # region fills its own bounding rect almost completely
        # (rectangularity ~1); irregular art background doesn't.
        rx, ry, rw, rh = cv2.boundingRect(cnt)
        rect_fill = cv2.contourArea(cnt) / max(1, rw * rh)
        # And it should not have flooded into half the page - a caption
        # region stays comparable to the text box size.
        area_ratio = cv2.contourArea(cnt) / max(1, w * h)
        if rect_fill > 0.85 and area_ratio < 8:
            return _res("caption", 0, 0, False, 0.7)
        return _res("sfx_candidate", 0, 0, False, 0.6)

    pts = cnt[:, 0, :].astype(np.float64)
    m = cv2.moments(cnt)
    if abs(m["m00"]) < 1e-6:
        return _res("uncertain", 0, 0, True, 0.2)
    cx, cy = m["m10"] / m["m00"], m["m01"] / m["m00"]
    r = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
    n = len(r)
    if n < 40:
        return _res("uncertain", 0, 0, True, 0.3)

    # Idealized smooth outline S: circular moving average of the radial
    # signal; C = O - S normalized by mean radius; sigma of C is the
    # classification signal (the paper's core idea).
    k = max(7, n // 18)
    padded = np.r_[r[-k:], r, r[:k]]
    S = np.convolve(padded, np.ones(k) / k, mode="same")[k:-k]
    C = (r - S) / (r.mean() + 1e-9)
    sigma = float(C.std())
    interior = C[1:-1]
    spikes = int(((interior > C[:-2]) & (interior > C[2:]) & (interior > 1.5 * sigma)
                  & (interior > 0.02)).sum())

    if sigma < SIGMA_SPEECH_MAX:
        return _res("speech", sigma, spikes, True, 0.9)
    if sigma > SIGMA_SHOUT_MIN and spikes >= SPIKES_SHOUT_MIN:
        return _res("shout", sigma, spikes, True, 0.8)
    if spikes >= SPIKES_THOUGHT_MIN:
        return _res("thought", sigma, spikes, True, 0.6)
    return _res("uncertain", sigma, spikes, True, 0.4)


def _res(cls, sigma, spikes, closed, conf):
    return {"bubble_class": cls, "sigma": round(float(sigma), 4),
            "spikes": int(spikes), "contour_closed": bool(closed),
            "confidence": conf}
