#!/usr/bin/env python3
import os
import sys

# CPU cap for OCR/detection, must be set before cv2/torch import. The
# earlier "Loading..." dashboard freeze was root-caused to an unrelated
# JS ReferenceError (fixed separately), not CPU contention - the phone
# now has a cooler and can sustain more cores. Cap at 6/8 to match
# mapaki's own established precedent, leaving 2 cores of headroom.
_detected_cpus = max(1, (os.cpu_count() or 8) - 2)
num_cpus_str = str(_detected_cpus)
os.environ.setdefault("OMP_NUM_THREADS", num_cpus_str)
os.environ.setdefault("OPENBLAS_NUM_THREADS", num_cpus_str)
os.environ.setdefault("MKL_NUM_THREADS", num_cpus_str)

import re
import argparse
import json
import shutil
import tempfile
import subprocess
import time
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import numpy as np
import requests
import cv2
cv2.setNumThreads(_detected_cpus)

repo_dir = os.path.dirname(os.path.abspath(__file__))
if repo_dir not in sys.path:
    sys.path.insert(0, repo_dir)
from kbg_web import edit_store
from common.book_paths import resolve_book_paths

# Import our TextDetector and natsort (installed in PRoot container)
try:
    import torch
    # TASK-64: on ARMv9 SoCs whose /proc/cpuinfo advertises SVE (e.g.
    # Snapdragon 8 Elite 2), PyTorch's oneDNN/ACL conv backend selects
    # SVE kernels that the Android kernel doesn't let userspace execute,
    # killing the process with SIGILL on the very first forward pass.
    # Bisected live: torch.load and model construction are fine, det.net(x)
    # crashes; ATEN_CPU_CAPABILITY=default alone does NOT help (the crash
    # is in mkldnn/ACL, not ATen dispatch) - disabling mkldnn is the one
    # confirmed fix. Only applied when torch detects SVE, so older
    # (ARMv8/NEON) devices keep the faster oneDNN path untouched.
    if torch.backends.cpu.get_cpu_capability().startswith("SVE"):
        torch.backends.mkldnn.enabled = False
        print("[translate_manga.py] SVE CPU detected: disabled mkldnn/oneDNN "
              "backend (Android kernels commonly reject userspace SVE - see TASK-64).")
    from comic_text_detector.inference import TextDetector
    from comic_text_detector.utils.textmask import REFINEMASK_INPAINT
    from natsort import natsorted
except ImportError as e:
    print(f"Error: Missing dependency in PRoot environment: {e}")
    print("This script must be run inside the PRoot Ubuntu container where packages are installed.")
    sys.exit(1)

def log(msg):
    print(f"[{Path(__file__).name}] {msg}")

def download_detector_model():
    model_dir = os.path.join(repo_dir, "models", "comic_text_detector")
    os.makedirs(model_dir, exist_ok=True)
    model_path = os.path.join(model_dir, "detector.pt")
    if not os.path.exists(model_path):
        log("Downloading comic text detector model (PyTorch)...")
        url = "https://github.com/zyddnys/manga-image-translator/releases/download/beta-0.3/comictextdetector.pt"
        import urllib.request
        urllib.request.urlretrieve(url, model_path)
        log("Model downloaded successfully!")
    return model_path

def extract_manga_pages(input_path, temp_dir):
    ext = os.path.splitext(input_path)[1].lower()
    if ext == '.pdf':
        log(f"Extracting PDF pages from {input_path}...")
        subprocess.run(['pdftoppm', '-png', '-r', '150', input_path, os.path.join(temp_dir, 'page')], check=True)
    elif ext in ['.zip', '.cbz', '.epub']:
        log(f"Extracting ZIP/CBZ/EPUB pages from {input_path}...")
        import zipfile
        with zipfile.ZipFile(input_path, 'r') as z:
            for file_info in z.infolist():
                if file_info.is_dir():
                    continue
                filename = file_info.filename.lower()
                if not (filename.endswith('.png') or filename.endswith('.jpg') or filename.endswith('.jpeg') or filename.endswith('.webp')):
                    continue
                basename = os.path.basename(file_info.filename)
                if not basename:
                    continue
                target_path = os.path.join(temp_dir, basename)
                with open(target_path, 'wb') as f_out:
                    f_out.write(z.read(file_info.filename))
    elif ext in ['.cbr', '.cb7']:
        log(f"Extracting RAR/7z pages from {input_path} using 7z...")
        subprocess.run(['7z', 'x', f'-o{temp_dir}', input_path], check=True)
        # Move nested images flat to temp_dir
        for root, dirs, files in os.walk(temp_dir):
            if root == temp_dir:
                continue
            for file in files:
                if file.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                    shutil.move(os.path.join(root, file), os.path.join(temp_dir, file))
    else:
        # If it's a folder, copy images
        if os.path.isdir(input_path):
            log(f"Copying images from directory {input_path}...")
            for file in os.listdir(input_path):
                if file.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                    shutil.copy(os.path.join(input_path, file), os.path.join(temp_dir, file))
        # If it's a single image file
        elif ext in ['.png', '.jpg', '.jpeg', '.webp']:
            log(f"Copying single image file {input_path}...")
            shutil.copy(input_path, os.path.join(temp_dir, os.path.basename(input_path)))
        else:
            raise ValueError(f"Unsupported input format: {ext}")

# TASK-28: single source of truth for the stroke outline width used both
# when actually drawing text (draw_text_centered) and when measuring it
# (wrap_text/_hard_wrap_word/fit_text/draw_text_centered's own centering
# math) - PIL's stroke_width widens the rendered glyph ink beyond its
# plain getbbox() extent on every side, so any getbbox() call missing this
# parameter underestimates the real rendered size and fit_text picks a
# font size that overflows once the stroke is actually drawn.
TEXT_STROKE_WIDTH = 2

# TASK-30: get_bubble_box() returns a rectangle approximating what's
# usually an oval speech bubble - a line wrapped/sized against the box's
# full width can visually press against the bubble's curved edge if it
# lands away from the box's vertical center, where the oval's real usable
# width is narrower than the rectangle. Used to shrink the width fit_text
# wraps/sizes against (never the box used for centering/rendering), as a
# uniform safety margin.
TEXT_WIDTH_SAFETY_MARGIN = 0.85


def _hard_wrap_word(word, font, max_width):
    """Character-level split for a single word wider than max_width on its
    own - wrap_text() below never breaks mid-word otherwise, so an overlong
    word (common in Ukrainian translations of short EN/JA source words)
    would blow past the box no matter how small the font gets. Every break
    except the final piece gets a trailing hyphen for readability (room for
    it is reserved conservatively during the fill, on all pieces, so a
    hyphenated break can never itself overflow)."""
    if not word:
        return [word]
    bbox = font.getbbox(word, stroke_width=TEXT_STROKE_WIDTH)
    if bbox[2] - bbox[0] <= max_width:
        return [word]
    pieces = []
    current = ""
    for ch in word:
        candidate = current + ch
        bbox = font.getbbox(candidate + "-", stroke_width=TEXT_STROKE_WIDTH)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = candidate
        else:
            pieces.append(current)
            current = ch
    if current:
        pieces.append(current)
    return [p + "-" if i < len(pieces) - 1 else p for i, p in enumerate(pieces)]

def wrap_text(text, font, max_width):
    words = text.split()
    lines = []
    current_line = []
    for word in words:
        test_line = " ".join(current_line + [word])
        bbox = font.getbbox(test_line, stroke_width=TEXT_STROKE_WIDTH)
        width = bbox[2] - bbox[0]
        if width <= max_width or not current_line:
            current_line.append(word)
        else:
            lines.append(" ".join(current_line))
            current_line = [word]
        # If the word we just placed alone still overflows (it was the
        # sole occupant of current_line and didn't fit), hard-wrap it in
        # place rather than silently exceeding max_width downstream.
        if len(current_line) == 1:
            solo_bbox = font.getbbox(current_line[0], stroke_width=TEXT_STROKE_WIDTH)
            if solo_bbox[2] - solo_bbox[0] > max_width:
                pieces = _hard_wrap_word(current_line[0], font, max_width)
                lines.extend(pieces[:-1])
                current_line = [pieces[-1]] if pieces else []
    if current_line:
        lines.append(" ".join(current_line))
    return lines

def fit_text(text, font_path, max_width, max_height, min_size=12, max_size_ratio=0.4):
    # Binary search for optimal font size, clamped to a readable floor and
    # a box-relative ceiling instead of the old fixed [8, 80] range.
    low = min_size
    high = max(min_size, min(80, int(max_height * max_size_ratio)))
    best_size = None
    best_lines = None

    def _measure(size):
        try:
            font = ImageFont.truetype(font_path, size)
        except Exception:
            font = ImageFont.load_default()
        lines = wrap_text(text, font, max_width)
        total_height = 0
        max_line_width = 0
        for line in lines:
            bbox = font.getbbox(line, stroke_width=TEXT_STROKE_WIDTH)
            total_height += (bbox[3] - bbox[1]) + 2
            max_line_width = max(max_line_width, bbox[2] - bbox[0])
        return lines, total_height, max_line_width

    while low <= high:
        mid = (low + high) // 2
        lines, total_height, max_line_width = _measure(mid)
        if total_height <= max_height and max_line_width <= max_width:
            best_size = mid
            best_lines = lines
            low = mid + 1
        else:
            high = mid - 1

    if best_size is not None:
        return best_size, best_lines

    # No size in [min_size, ceiling] satisfies both constraints - the old
    # code returned the ENTIRE text as one unwrapped line at size 8, which
    # is guaranteed to overflow far worse than a wrapped result at the
    # floor size. Return the floor-size wrapped lines instead: still
    # overflowing (flagged by post_render_check downstream), but readable
    # and box-relative rather than a single giant line.
    fallback_lines, _, _ = _measure(min_size)
    return min_size, fallback_lines

def draw_text_centered(draw, lines, font_path, size, box):
    x1, y1, x2, y2 = box
    box_w = x2 - x1
    box_h = y2 - y1
    
    try:
        font = ImageFont.truetype(font_path, size)
    except Exception:
        font = ImageFont.load_default()
        
    line_heights = []
    total_height = 0
    for line in lines:
        bbox = font.getbbox(line, stroke_width=TEXT_STROKE_WIDTH)
        h = bbox[3] - bbox[1]
        line_heights.append(h)
        total_height += h + 2

    y_offset = y1 + (box_h - total_height) // 2

    for line, h in zip(lines, line_heights):
        bbox = font.getbbox(line, stroke_width=TEXT_STROKE_WIDTH)
        w = bbox[2] - bbox[0]
        x_offset = x1 + (box_w - w) // 2
        # Render text with white border outline for visibility
        draw.text((x_offset, y_offset), line, font=font, fill="black", stroke_width=TEXT_STROKE_WIDTH, stroke_fill="white")
        y_offset += h + 2

_DICT_WORDS = None
_DICT_PATHS = ("/usr/share/dict/words", "/usr/share/dict/american-english", "/usr/share/dict/british-english")


def _load_english_dictionary():
    """TASK-33: cached lazy-load of a system word list, used as the
    fallback source_confidence signal for OCR engines (MangaOcr) that
    expose no confidence score of their own. Returns an empty set (not
    None) if no dictionary file is present on this system - callers
    must treat an empty set as 'signal unavailable', not 'zero real
    words found'."""
    global _DICT_WORDS
    if _DICT_WORDS is not None:
        return _DICT_WORDS
    _DICT_WORDS = set()
    for path in _DICT_PATHS:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    _DICT_WORDS = {w.strip().lower() for w in f if w.strip()}
                break
            except Exception:
                pass
    return _DICT_WORDS


def real_word_fraction(text):
    """TASK-33: fraction of this text's alphabetic tokens (len>=2, so
    single letters like the article "a" don't skew the score) that are
    found in the system English dictionary. Returns None - not 0.0 - if
    no dictionary is installed or there are no alphabetic tokens to
    check, so callers can tell 'no signal' apart from 'genuinely zero
    real words'. This is a complementary signal to raw OCR confidence,
    not a replacement: catches cases like frieren's real p020_b12
    ("...WHAT FUN ; DO YOU TOO... WANT SHALL TO WE DO? | DANCE? Fred
    eS") where individual characters/words are mostly legible (so
    per-character OCR confidence could look fine) but the word ORDER is
    scrambled nonsense with junk tokens mixed in."""
    words = _load_english_dictionary()
    if not words:
        return None
    tokens = [t.lower() for t in re.findall(r"[A-Za-z']+", text) if len(t) >= 2]
    if not tokens:
        return None
    real = sum(1 for t in tokens if t.strip("'") in words)
    return real / len(tokens)


_OCR_EDGE_NOISE_CHARS = set("|\\/{}@:")


def clean_ocr_edge_noise(text, page_name=None, bubble_ref=None):
    """TASK-33: strips ONLY noise-character runs anchored at the very
    start/end of raw OCR'd text - pipe, backslash, forward-slash, curly
    braces, at-sign, colon (the exact set the task specified, nothing
    added on top). Deliberately does NOT touch the same characters when
    they appear mid-sentence (e.g. "OUR \\ FUTURE." keeps its stray
    backslash) - garbage embedded inside otherwise-legible text is a
    genuinely different, harder problem (would need real content
    understanding to safely remove) than a junk prefix/suffix, and the
    task's own instruction was explicit about not touching legitimate
    mid-sentence punctuation. Applied BEFORE translation (garbage-in-
    garbage-out: if the LLM never sees the junk, it can't leak into the
    translation either). Every actual removal is logged for audit -
    never a silent edit. Real examples this resolves (frieren):
    "VIALA- THOR//" -> "VIALA- THOR" (trailing double-slash gone, the
    hyphen is untouched since it's not in the noise set); "DID I REALLY
    GO BACK IN |@ TIME? | \\ \\ \\ |" -> "DID I REALLY GO BACK IN |@
    TIME?" (trailing pipe/backslash junk gone, the embedded |@ stays -
    not anchored)."""
    start = 0
    while start < len(text) and (text[start].isspace() or text[start] in _OCR_EDGE_NOISE_CHARS):
        start += 1
    end = len(text)
    while end > start and (text[end - 1].isspace() or text[end - 1] in _OCR_EDGE_NOISE_CHARS):
        end -= 1
    cleaned = text[start:end]
    if not cleaned:
        # The whole string was noise/whitespace - nothing legible left to
        # translate anyway, but don't return an empty string silently;
        # keep the original so it's still visible (and flaggable via
        # source_confidence) rather than vanishing entirely.
        return text
    if cleaned != text:
        ref = f"{page_name}/{bubble_ref}" if page_name else (bubble_ref or "?")
        log(f"[OCR-noise] {ref}: {text!r} -> {cleaned!r}")
    return cleaned


def _iou(box_a, box_b):
    """Intersection-over-union of two [x1,y1,x2,y2] boxes. 0.0 if they
    don't intersect at all (also handles degenerate zero-area boxes)."""
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0

class _MergedBlock:
    """Minimal duck-typed stand-in for comic_text_detector's TextBlock,
    carrying only the union bbox after Stage A dedup - the rest of the
    pipeline (OCR crop, typeset box) only ever reads .xyxy off a block."""
    def __init__(self, xyxy):
        self.xyxy = xyxy

def _containment_ratio(box_a, box_b):
    """intersection / area of the SMALLER of the two boxes - unlike IoU,
    this stays high (up to 1.0) when one box is fully or near-fully
    nested inside a much bigger one, even though their AREAS differ a
    lot (which is exactly what tanks plain IoU in that situation)."""
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    smaller = min(area_a, area_b)
    return inter / smaller if smaller > 0 else 0.0


def dedupe_blocks(blk_list, iou_threshold=0.3, containment_threshold=0.85):
    """Stage A: merge overlapping/duplicate detector blocks - a known
    imperfect-grouping failure mode for large/oval bubbles even after the
    detector's own internal NMS - before OCR, so one physical bubble never
    gets OCR'd/translated/typeset twice (the "double text" defect).
    Union-find so transitively-overlapping chains merge into one group,
    not just directly-overlapping pairs.

    TASK-33: plain IoU alone misses a real, confirmed failure pattern -
    a small block ALMOST ENTIRELY CONTAINED inside a much larger one
    (e.g. the detector finding one whole speech bubble's full dialogue,
    and separately finding just its opening clause as its own block)
    produces a low IoU purely because the two areas differ so much, even
    though the smaller block is ~100% redundant with part of the larger
    one. Confirmed on real data (frieren c118 p022, _b07 100% contained
    in _b06, IoU~0.14 - well under the 0.3 threshold, yet clearly
    duplicated text). containment_threshold catches this case
    specifically without lowering iou_threshold (which would risk
    merging genuinely separate, merely-adjacent bubbles instead)."""
    n = len(blk_list)
    if n <= 1:
        return list(blk_list)
    parent = list(range(n))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj
    for i in range(n):
        for j in range(i + 1, n):
            box_i, box_j = blk_list[i].xyxy, blk_list[j].xyxy
            if _iou(box_i, box_j) > iou_threshold or _containment_ratio(box_i, box_j) > containment_threshold:
                union(i, j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    merged = []
    for indices in groups.values():
        if len(indices) == 1:
            merged.append(blk_list[indices[0]])
        else:
            xs1 = [blk_list[i].xyxy[0] for i in indices]
            ys1 = [blk_list[i].xyxy[1] for i in indices]
            xs2 = [blk_list[i].xyxy[2] for i in indices]
            ys2 = [blk_list[i].xyxy[3] for i in indices]
            merged.append(_MergedBlock([min(xs1), min(ys1), max(xs2), max(ys2)]))
    return merged

def robust_inpaint(img, mask_refined, blk_list, radius=9):
    """Stage B: TELEA inpaint at a larger radius (was 3px - too thin for
    bold/large source fonts, leaving a visible "ghost" of the original
    text under the new one) plus a per-block solid-fill pass on top,
    where the surrounding background is flat enough to estimate safely
    (typical of manga's solid-color bubble fills)."""
    inpainted = cv2.inpaint(img, mask_refined, radius, cv2.INPAINT_TELEA)
    h_img, w_img = img.shape[:2]
    ring = 6

    for blk in blk_list:
        x1, y1, x2, y2 = blk.xyxy
        bx1, by1 = max(0, x1 - ring), max(0, y1 - ring)
        bx2, by2 = min(w_img, x2 + ring), min(h_img, y2 + ring)
        if bx2 <= bx1 or by2 <= by1:
            continue

        region_mask = mask_refined[by1:by2, bx1:bx2]
        if region_mask.size == 0 or not region_mask.any():
            continue

        # Sample the border ring OUTSIDE the mask for a background color
        # estimate - the mask itself covers where the old text glyphs
        # were, so pixels just outside it are the bubble's fill color.
        border = np.zeros_like(region_mask, dtype=bool)
        border[:ring, :] = True
        border[-ring:, :] = True
        border[:, :ring] = True
        border[:, -ring:] = True
        border &= (region_mask == 0)
        sample_pixels = inpainted[by1:by2, bx1:bx2][border]
        if sample_pixels.shape[0] < 10:
            continue  # not enough clean background sampled - trust TELEA alone

        std = float(np.std(sample_pixels, axis=0).mean())
        if std > 18:
            continue  # background too noisy/textured to safely flat-fill

        bg_color = np.median(sample_pixels, axis=0)
        fill_mask = region_mask.astype(bool)
        inpainted[by1:by2, bx1:bx2][fill_mask] = bg_color

    return inpainted

def _get_bubble_core_box(blk, mask_refined, img_shape):
    """Stage C (core): the UNPADDED box from the connected mask region(s)
    the detector/inpaint actually covered for this block, instead of the
    OCR-tight blk.xyxy - the tight bbox is why typeset text overflows
    (translated text needs the bubble's real interior, not just the
    original glyphs' bounding box) or looks disproportionate.

    Multi-line/multi-word text is usually NOT one connected mask blob -
    gaps between lines and letters break 8-connectivity, so a single block
    can correspond to several disconnected components. Picking "the
    component under the block's geometric center" (an earlier version of
    this function) is fragile: the center often lands in a gap between
    lines, landing on background or an unrelated nearby component instead.
    Union the bounding rects of every component that actually overlaps
    the original OCR bbox instead - correctly reconstructs the full
    multi-line extent.

    No padding is applied here (see _apply_padding_neighbor_safe) - kept
    separate so a page's full set of core boxes can be computed before
    any bubble's padding decision, which is what makes neighbor-aware
    padding (TASK-36) possible without an ordering dependency."""
    h_img, w_img = img_shape[:2]
    x1, y1, x2, y2 = blk.xyxy

    # Small search margin - just enough to recover component edges a
    # too-tight OCR bbox might have clipped, not a scan for "anything
    # nearby" (that's what caused the center-pixel approach to grab
    # unrelated content).
    search_pad = int(max(x2 - x1, y2 - y1) * 0.15) + 2
    sx1, sy1 = max(0, x1 - search_pad), max(0, y1 - search_pad)
    sx2, sy2 = min(w_img, x2 + search_pad), min(h_img, y2 + search_pad)
    region = mask_refined[sy1:sy2, sx1:sx2]
    if region.size == 0 or not region.any():
        return (int(x1), int(y1), int(x2), int(y2))

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats((region > 0).astype(np.uint8), connectivity=8)
    if num_labels <= 1:
        return (int(x1), int(y1), int(x2), int(y2))

    orig_x1, orig_y1 = x1 - sx1, y1 - sy1
    orig_x2, orig_y2 = x2 - sx1, y2 - sy1
    union = None
    for label in range(1, num_labels):
        lx = stats[label, cv2.CC_STAT_LEFT]
        ly = stats[label, cv2.CC_STAT_TOP]
        lw = stats[label, cv2.CC_STAT_WIDTH]
        lh = stats[label, cv2.CC_STAT_HEIGHT]
        # Overlap test against the ORIGINAL detection bbox (in this
        # region's local coordinates) - only union components genuinely
        # tied to this block's text, not just anything in the margin.
        ox1, oy1 = max(lx, orig_x1), max(ly, orig_y1)
        ox2, oy2 = min(lx + lw, orig_x2), min(ly + lh, orig_y2)
        if ox2 <= ox1 or oy2 <= oy1:
            continue
        comp = (lx, ly, lx + lw, ly + lh)
        union = comp if union is None else (
            min(union[0], comp[0]), min(union[1], comp[1]),
            max(union[2], comp[2]), max(union[3], comp[3])
        )

    if union is None:
        return (int(x1), int(y1), int(x2), int(y2))

    # union[...] can be numpy.int32 (from cv2.connectedComponentsWithStats's
    # `stats` array) rather than plain Python int - cast explicitly so
    # this box is safe to json.dump() (bubbles_meta.json needs it to be),
    # not just safe to pass to cv2/PIL calls (which don't care).
    bx1, by1, bx2, by2 = sx1 + union[0], sy1 + union[1], sx1 + union[2], sy1 + union[3]
    return (int(bx1), int(by1), int(bx2), int(by2))


def _apply_padding_neighbor_safe(core_box, img_shape, padding_ratio=0.15, other_core_boxes=None, min_gap=3):
    """Stage C (padding): pads core_box by padding_ratio in each direction
    (same formula the original single-function get_bubble_box always
    used), clamped to image bounds and, if other_core_boxes is given,
    ALSO clamped so the padded edge never crosses closer than min_gap px
    to another bubble's own core (unpadded) box on the same page.

    TASK-36: get_bubble_box previously padded every bubble independently
    with zero awareness of neighbors, which is why closely-spaced bubbles'
    padded boxes routinely overlapped - confirmed via a book-wide IoU scan
    of frieren before this fix: 105 overlapping pairs across 187 pages
    (161 distinct bubbles affected), not just the 2 cases spotted by eye.
    Clamping against neighbors' CORE boxes (not their own already-padded
    boxes) avoids a chicken-and-egg ordering dependency - every bubble's
    core box on a page is already known before any padding decision, so
    no bubble's padding needs another bubble's padding to be computed
    first."""
    h_img, w_img = img_shape[:2]
    x1, y1, x2, y2 = core_box
    pad_x, pad_y = int((x2 - x1) * padding_ratio), int((y2 - y1) * padding_ratio)
    px1, py1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
    px2, py2 = min(w_img, x2 + pad_x), min(h_img, y2 + pad_y)

    if other_core_boxes:
        for (ox1, oy1, ox2, oy2) in other_core_boxes:
            # Clamp to the MIDPOINT between the two cores (minus half the
            # safety gap), not to "min_gap from the neighbor's raw edge" -
            # the latter lets each box independently claim up to min_gap
            # of the SAME shared space from its own side, which can still
            # overlap when the two cores are close together (caught by a
            # unit test: two 10px-apart cores each padding independently
            # toward "min_gap from the other's edge" left a 4px overlap
            # in the middle, since both sides used nearly the whole gap).
            # Splitting at the midpoint guarantees each side stays
            # strictly on its own half, no coordination between the two
            # independent calls required.
            if oy2 > y1 and oy1 < y2:  # vertical ranges overlap -> a left/right neighbor
                if ox2 <= x1:  # neighbor is to the left
                    px1 = max(px1, int((x1 + ox2) / 2 + min_gap / 2))
                if ox1 >= x2:  # neighbor is to the right
                    px2 = min(px2, int((x2 + ox1) / 2 - min_gap / 2))
            if ox2 > x1 and ox1 < x2:  # horizontal ranges overlap -> a top/bottom neighbor
                if oy2 <= y1:  # neighbor is above
                    py1 = max(py1, int((y1 + oy2) / 2 + min_gap / 2))
                if oy1 >= y2:  # neighbor is below
                    py2 = min(py2, int((y2 + oy1) / 2 - min_gap / 2))

    # Never let clamping invert the box (px1 >= px2) - if neighbors are
    # too close for any padding at all, fall back to the unpadded core
    # box on that axis rather than a degenerate/negative-size box.
    if px1 >= px2:
        px1, px2 = x1, x2
    if py1 >= py2:
        py1, py2 = y1, y2

    return (int(px1), int(py1), int(px2), int(py2))


def get_bubble_box(blk, mask_refined, img_shape, padding_ratio=0.15, other_core_boxes=None):
    """Stage C: derive the typeset box from the connected mask region(s)
    the detector/inpaint actually covered for this block (see
    _get_bubble_core_box), padded outward by padding_ratio.

    TASK-36: now neighbor-aware when other_core_boxes is supplied (the
    core, unpadded boxes of every other bubble already located on this
    page) - padding no longer overlaps a neighbor's territory. Backward
    compatible: omitting other_core_boxes reproduces the original
    independent-padding behavior exactly (e.g. any caller that hasn't
    been updated to pass neighbor context is unaffected)."""
    core_box = _get_bubble_core_box(blk, mask_refined, img_shape)
    return _apply_padding_neighbor_safe(core_box, img_shape, padding_ratio, other_core_boxes)

def post_render_check(flags, page_name, block_index, chosen_size, wrapped_lines, box, font_path, min_size):
    """Stage E: after typeset, flag anything that still looks wrong so it
    surfaces for review instead of shipping silently. Appends to `flags`
    (a list the caller accumulates per-book and writes to
    quality_flags.json at the end) - does not raise or block the pipeline."""
    x1, y1, x2, y2 = box
    box_w = max(1, x2 - x1)
    try:
        font = ImageFont.truetype(font_path, chosen_size)
    except Exception:
        font = ImageFont.load_default()
    max_line_width = 0
    for line in wrapped_lines:
        # TASK-28: same stroke_width omission as fit_text/wrap_text - without
        # it, overflow_ratio itself was underestimated, so some genuinely
        # overflowing bubbles could score just under the 1.05 flag threshold
        # and never get flagged for review.
        bbox = font.getbbox(line, stroke_width=TEXT_STROKE_WIDTH)
        max_line_width = max(max_line_width, bbox[2] - bbox[0])
    overflow_ratio = max_line_width / box_w if box_w else 0

    hit_floor = chosen_size <= min_size
    if overflow_ratio > 1.05 or hit_floor:
        flags.append({
            "page": page_name,
            "block_index": block_index,
            "box": [x1, y1, x2, y2],
            "chosen_size": chosen_size,
            "overflow_ratio": round(overflow_ratio, 3),
            "hit_min_size": hit_floor,
            "reason": "overflow" if overflow_ratio > 1.05 else "min_size_floor"
        })

BOX_OVERLAP_IOU_THRESHOLD = 0.02  # TASK-36: below this is boundary-rounding noise, not a real visible overlap


def compute_box_overlap_flags(bubbles, page_name, iou_threshold=BOX_OVERLAP_IOU_THRESHOLD):
    """TASK-36: pairwise IoU across every bubble on one page - flags any
    pair whose padded typeset boxes genuinely overlap (get_bubble_box's
    padding is neighbor-aware now, see _apply_padding_neighbor_safe, but
    this still catches any residual multi-way conflict the pairwise
    clamp doesn't fully resolve, and is also the only way to surface
    overlaps in pages processed before this fix existed - see the
    --backfill-box-overlap-flags CLI mode).

    Returns a dict {bubble_id: {"box_overlap": True, "overlapping_with":
    [other_id, ...], "iou": max_iou_with_any_neighbor}} for every
    affected bubble - a bubble not in the returned dict has no overlap.
    Confirmed via a full book-wide scan of frieren before this fix
    existed: 105 overlapping pairs across 187 pages, 161 distinct
    bubbles affected - not a rare edge case, a systemic padding bug."""
    result = {}
    n = len(bubbles)
    for i in range(n):
        a = bubbles[i]
        if "bbox" not in a or "id" not in a:
            continue
        for j in range(i + 1, n):
            b = bubbles[j]
            if "bbox" not in b or "id" not in b:
                continue
            score = _iou(a["bbox"], b["bbox"])
            if score <= iou_threshold:
                continue
            for this_b, other_b, this_id, other_id in ((a, b, a["id"], b["id"]), (b, a, b["id"], a["id"])):
                entry = result.setdefault(this_id, {"box_overlap": True, "overlapping_with": [], "iou": 0.0})
                if other_id not in entry["overlapping_with"]:
                    entry["overlapping_with"].append(other_id)
                entry["iou"] = round(max(entry["iou"], score), 4)
    return result


def assign_bubble_ids(page_stem, blocks):
    """TASK-21: stable bubble IDs for manual editing. Ranks blocks by
    (y1, x1) reading order for the ID *value* (so IDs are consistent
    across runs with the same detections, regardless of blk_list's
    internal order), but returns the ID list in the SAME order as the
    input `blocks` so callers can zip it against their own per-block loop."""
    indexed = list(enumerate(blocks))
    ranked = sorted(indexed, key=lambda pair: (pair[1].xyxy[1], pair[1].xyxy[0]))
    id_for_original_index = {}
    for rank, (orig_idx, blk) in enumerate(ranked):
        id_for_original_index[orig_idx] = f"{page_stem}_b{rank:02d}"
    return [id_for_original_index[i] for i in range(len(blocks))]

def write_bubbles_meta(book_dir, page_stem, entries):
    """TASK-21: writes book_dir/bubbles_meta/<page_stem>.json - one file
    per page, the source of truth the manual-edit overlay UI draws bubble
    bounding boxes and current text from."""
    meta_dir = os.path.join(book_dir, "bubbles_meta")
    os.makedirs(meta_dir, exist_ok=True)
    path = os.path.join(meta_dir, f"{page_stem}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    return path

def match_bubbles_iou(old_entries, new_entries, iou_threshold=0.5):
    """TASK-21: reconciles bubble IDs across a page regeneration.
    dedupe_blocks (Stage A) can legitimately produce a different block
    count/order between runs, so a plain index-based ID would silently
    disconnect a pending edit from its bubble. Greedy best-IoU-first
    matching (each old bubble matched to at most one new bubble, and vice
    versa). Returns {old_id: new_id_or_None} - None means no confident
    match was found in the new set, so the caller should mark any edit
    referencing that old bubble as "orphaned" rather than silently
    dropping it."""
    candidates = []
    for old in old_entries:
        best_iou = 0.0
        best_new_id = None
        for new in new_entries:
            iou = _iou(old["bbox"], new["bbox"])
            if iou > best_iou:
                best_iou = iou
                best_new_id = new["id"]
        candidates.append((best_iou, old["id"], best_new_id))
    # Strongest matches claim their target first, so a weak/ambiguous
    # match never steals a new bubble a stronger match also wanted.
    candidates.sort(key=lambda c: c[0], reverse=True)
    result = {}
    used_new = set()
    for iou, old_id, new_id in candidates:
        if new_id is not None and new_id not in used_new and iou > iou_threshold:
            result[old_id] = new_id
            used_new.add(new_id)
        else:
            result[old_id] = None
    return result

# TASK-29: source text is OCR'd from ALL-CAPS comic lettering, and the
# translation LLM only inconsistently follows a prompt instruction asking
# for normal sentence case (empirically confirmed - some lines came back
# fully uppercase, others with just one stray lowercase word). Post-
# process instead of relying on prompt compliance alone.
_UPPER_RATIO_THRESHOLD = 0.7


def _normalize_sentence_case(text, glossary):
    """If most of the string's letters are uppercase, treat the whole
    thing as mis-cased and rewrite it: lowercase everything, then
    capitalize the first letter of the string and of each sentence
    (after . ! ?). Glossary terms (character names etc.) are the only
    proper nouns we can reliably identify after the fact, so re-
    capitalize any occurrence of one to its exact glossary casing.
    Strings that are already mostly normal case are left untouched."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return text
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    if upper_ratio < _UPPER_RATIO_THRESHOLD:
        return text

    lowered = text.lower()
    result = []
    capitalize_next = True
    for ch in lowered:
        if capitalize_next and ch.isalpha():
            result.append(ch.upper())
            capitalize_next = False
        else:
            result.append(ch)
        if ch in ".!?":
            capitalize_next = True
    normalized = "".join(result)

    for term in (glossary or {}).values():
        if not term:
            continue
        normalized = re.sub(re.escape(term), term, normalized, flags=re.IGNORECASE)

    return normalized


# TASK-54 Cast Registry: loaded once per run (config + entitlement check
# hit the network at most once, never per page). None = feature inactive.
_CAST_CACHE = {"initialized": False, "chars": None}

# TASK-67: bubble-tone prompt injection gate. Classification itself
# always runs (data lands in bubbles_meta); only the PROMPT effect is
# flag-gated until a live retranslate A/B confirms no regressions.
_TONE_CFG = {"enabled": False}

# TASK-56: keep_honorifics prompt-injection gate, same pattern as _TONE_CFG.
_HONORIFICS_CFG = {"enabled": False}


# Tracks which pages have already been force-retranslated under a given
# --clean-run-id, so a crash-and-resume of the SAME deliberate clean sweep
# can keep --force-retranslate active (correctly redoing pages it hasn't
# reached yet) without re-doing pages it already redid THIS sweep, and
# without falling back to unrelated stale files from a previous run - see
# the --clean-run-id argparse help above for the full incident writeup.
def _force_retranslate_progress_path(book_dir):
    return os.path.join(book_dir, ".force_retranslate_progress.json")


def load_clean_run_progress(book_dir, run_id):
    """Returns the set of page basenames already completed under run_id.
    A missing file, a different stored run_id, or any read error all mean
    'nothing done yet for this run' - the safe default is to redo more,
    never to skip a page that wasn't actually verified done this sweep."""
    if not run_id:
        return set()
    try:
        with open(_force_retranslate_progress_path(book_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("run_id") == run_id:
            return set(data.get("completed_pages") or [])
    except Exception:
        pass
    return set()


def record_clean_run_page(book_dir, run_id, basename):
    if not run_id:
        return
    path = _force_retranslate_progress_path(book_dir)
    try:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("run_id") != run_id:
                data = {"run_id": run_id, "completed_pages": []}
        except Exception:
            data = {"run_id": run_id, "completed_pages": []}
        pages = set(data.get("completed_pages") or [])
        pages.add(basename)
        data["completed_pages"] = sorted(pages)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log(f"Warning: failed to record clean-run progress for {basename}: {e}")


def init_bubble_tone(book_dir):
    try:
        cfg = json.load(open(os.path.join(book_dir, "config.json"), encoding="utf-8"))
        _TONE_CFG["enabled"] = bool(cfg.get("enable_bubble_tone"))
        if _TONE_CFG["enabled"]:
            log("Bubble-tone prompt injection: ENABLED for this book")
    except Exception:
        _TONE_CFG["enabled"] = False


def init_honorifics(book_dir):
    try:
        cfg = json.load(open(os.path.join(book_dir, "config.json"), encoding="utf-8"))
        _HONORIFICS_CFG["enabled"] = bool(cfg.get("keep_honorifics"))
        if _HONORIFICS_CFG["enabled"]:
            log("Honorific-suffix preservation: ENABLED for this book")
    except Exception:
        _HONORIFICS_CFG["enabled"] = False


def init_cast_registry(book_dir):
    if _CAST_CACHE["initialized"]:
        return
    _CAST_CACHE["initialized"] = True
    try:
        from common.cast_registry import registry_enabled, load_characters
        if registry_enabled(book_dir):
            chars = [c for c in load_characters(book_dir)
                     if c.get("status") == "verified"]
            if chars:
                _CAST_CACHE["chars"] = chars
                log(f"Cast Registry active: {len(chars)} verified character(s).")
            else:
                log("Cast Registry enabled but no verified characters yet - safe mode (no injection).")
    except Exception as ex:
        log(f"Cast Registry init skipped: {ex}")


def translate_batch_llm(texts, source_lang, glossary, api_url, overrides=None,
                        cast_characters=None, tones=None):
    if not texts:
        return {}

    # TASK-21: texts with a human-edited override skip the LLM entirely -
    # both to avoid wasting a translation call and, more importantly, to
    # guarantee the human edit survives verbatim rather than risking the
    # LLM producing something different on a re-run.
    overrides = overrides or {}
    result = {txt: overrides[txt] for txt in texts if txt in overrides}
    remaining = [txt for txt in texts if txt not in overrides]
    if not remaining:
        return result

    # Construct terminology instructions from glossary
    glossary_rules = ""
    if glossary:
        glossary_rules = "Follow this terminology exactly:\n"
        for src_word, tgt_word in glossary.items():
            glossary_rules += f"- {src_word} -> {tgt_word}\n"

    # TASK-54 Cast Registry: verified characters' grammar rules for THIS
    # batch's source text. Empty string when the feature is off/ungated or
    # nothing matches - the prompt is then byte-identical to pre-feature.
    cast_rules = ""
    if cast_characters:
        try:
            from common.cast_registry import cast_rules_block
            cast_rules = cast_rules_block(cast_characters, "\n".join(remaining))
            if cast_rules:
                cast_rules += "\n"
        except Exception:
            cast_rules = ""

    # TASK-67: per-bubble tone tags from geometric bubble classification
    # (shout/thought/caption). tones maps source text -> class. When the
    # feature is off or every bubble is plain speech, the prompt stays
    # byte-identical to before (same guarantee as the Cast Registry hook).
    tone_tag = {"shout": "[КРИК]", "thought": "[ДУМКА]", "caption": "[НАРАЦІЯ]"}
    tones = tones or {}
    def _line(i, txt):
        tag = tone_tag.get(tones.get(txt, ""), "")
        return f"{i+1}. {tag} {txt}" if tag else f"{i+1}. {txt}"
    prompt_list = "\n".join([_line(i, txt) for i, txt in enumerate(remaining)])
    tone_rules = ""
    if any(tones.get(t) in tone_tag for t in remaining):
        tone_rules = ("Some lines carry a tone tag describing the bubble type - adapt the translation register accordingly and DO NOT copy the tag into the output:\n"
                      "[КРИК] - a shout/burst bubble: expressive, punchy phrasing.\n"
                      "[ДУМКА] - an inner-thought cloud: reflective, softer inner voice.\n"
                      "[НАРАЦІЯ] - a narration caption: literary register, third person.\n")

    # TASK-56: opt-in preservation of Japanese/Korean honorific suffixes
    # (-san/-chan/-kun/-sama/-nim etc.) that the source OCR may already
    # carry romanized (scanlations commonly keep them). Off by default -
    # empty string means byte-identical prompt to pre-feature, same
    # guarantee as every other optional rule block here.
    honorific_rule = ""
    if _HONORIFICS_CFG["enabled"]:
        honorific_rule = ("Preserve Japanese/Korean honorific suffixes exactly as they appear in the "
                          "source (e.g. -san, -chan, -kun, -sama, -senpai, -nim) - do NOT translate, "
                          "drop, or localize them into a Ukrainian equivalent.\n")

    system_prompt = f"""You are a professional manga translator. Translate the following numbered list of texts from {source_lang.upper()} to Ukrainian.
Preserve context, sound effects (if present), informal spoken registers, character personalities, and sentence fragments.
Do NOT translate characters names if they are part of the glossary.
The source text is OCR'd from ALL-CAPS comic lettering - ignore that formatting entirely. Output your translation in normal Ukrainian sentence case: capitalize only the first letter of each sentence and proper nouns, everything else lowercase. Never output an entire line in capital letters unless it is a genuine sound effect (onomatopoeia) or the character is explicitly shouting/emphasizing that specific word.
Maintain the exact same line-by-line numbering format. Output ONLY the translated list. No intro, no chat.
{glossary_rules}{cast_rules}{tone_rules}{honorific_rule}"""

    # Retry with backoff on transient failures - notably 503 "Loading
    # model", which fires reliably when translate_manga.py starts (e.g.
    # via the auto-resume-on-restart path) before llama-server has
    # finished loading the GGUF into memory. Without this, the first few
    # pages of every auto-resumed run silently kept their original
    # English text forever with zero indication anything went wrong.
    # Total budget matches the established convention elsewhere in this
    # codebase (common/utils.py's wait_for_server_ready: max_wait=300) -
    # found via GitNexus impact analysis on this function, which flagged
    # this file's original 6-attempt/~60s cap as undershooting the
    # precedent other pipelines already rely on for the same 503 case.
    max_attempts = 20
    backoff = [2, 4, 8, 15] + [15] * 15
    for attempt in range(max_attempts):
        try:
            response = requests.post(
                api_url,
                json={
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt_list}
                    ],
                    "temperature": 0.2
                },
                timeout=300
            )
            if response.status_code == 200:
                res_json = response.json()
                content = res_json["choices"][0]["message"]["content"].strip()
                lines = content.split("\n")

                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    if "." in line:
                        parts = line.split(".", 1)
                        try:
                            idx = int(parts[0].strip()) - 1
                            val = _normalize_sentence_case(parts[1].strip(), glossary)
                            if 0 <= idx < len(remaining):
                                result[remaining[idx]] = val
                        except ValueError:
                            continue

                # Fill missing translations with original text
                for txt in remaining:
                    if txt not in result:
                        result[txt] = txt

                return result
            elif response.status_code == 503 and attempt < max_attempts - 1:
                log(f"API 503 (model still loading), retrying in {backoff[attempt]}s "
                    f"(attempt {attempt+1}/{max_attempts})...")
                time.sleep(backoff[attempt])
                continue
            else:
                log(f"Error: API returned status code {response.status_code}: {response.text}")
                break
        except Exception as e:
            if attempt < max_attempts - 1:
                log(f"Translation API request failed: {e}; retrying in {backoff[attempt]}s "
                    f"(attempt {attempt+1}/{max_attempts})...")
                time.sleep(backoff[attempt])
                continue
            log(f"Translation API request failed: {e}")
            break

    # Fallback to returning original text if translation fails
    for txt in remaining:
        result.setdefault(txt, txt)
    return result

def process_page(img, page_basename, glossary, api_url, lang, detector, mocr, font_path, overrides=None, bbox_overrides=None, font_size_overrides=None):
    """TASK-21: runs the Stage A-E pipeline (detect -> dedupe -> OCR ->
    translate -> inpaint -> typeset -> post-check) on a single
    already-loaded page image. Pure image processing, no file I/O - both
    the full-book loop in main() and the single-page --regenerate-page
    path share this, so a manual edit's regen goes through exactly the
    same pipeline a fresh page does (fixes TASK-19_manga_typeset_autofix's
    "regen must go through the full A-E pipeline, not just typeset, or
    the ghost-text problem returns" requirement).

    overrides: optional {original_ocr_text: edited_translation} dict, see
    translate_batch_llm - lets a human edit survive a page regeneration
    verbatim instead of being re-translated by the LLM.

    bbox_overrides: optional {original_ocr_text: [x1, y1, x2, y2]} dict
    (TASK-36) - keyed the same way as `overrides` (by the block's OCR'd
    source text, not a positional bubble_id, since block order/ids are
    not guaranteed stable across a full re-detection) - for a matching
    block, this box is used VERBATIM instead of get_bubble_box(), and
    still participates as a neighbor for every other block's own
    padding clamp, so a manually-placed box is respected by its
    neighbors too, not just applied and then immediately encroached on.

    font_size_overrides: optional {original_ocr_text: int} dict (TASK-36)
    - same original_ocr_text keying as overrides/bbox_overrides. For a
      matching block, this size is used verbatim instead of fit_text()'s
      own binary search - lets a human force a smaller size when the
      auto-picked size still overflows the (possibly also manually
      resized) box, or a larger one when auto-fit chose smaller than
      necessary. Independent of bbox_overrides - either can be set
      without the other, since a human might only want to fix one.
      post_render_check still runs against a manual size exactly as it
      does for an auto-fit one, so a manual size that's still too big
      is flagged, not silently hidden.

    Returns (final_img_bgr, cleaned_img_bgr, page_quality_flags,
    page_bubbles_meta). If no text bubbles are found/recognized,
    final_img_bgr and cleaned_img_bgr are both `img` untouched, and both
    lists are empty."""
    page_quality_flags = []
    page_bubbles_meta = []

    mask, mask_refined, blk_list = detector(img, refine_mode=REFINEMASK_INPAINT, keep_undetected_mask=True)
    blk_list = dedupe_blocks(blk_list)

    if not blk_list:
        return img, img, page_quality_flags, page_bubbles_meta

    page_ocr_texts = []
    block_crops = []
    # TASK-33: source_confidence is a SEPARATE quality axis from render
    # geometry (overflow_ratio/font_size/box_overlap) - keyed by the same
    # stable original_ocr_text convention overrides/bbox_overrides
    # already use, since positional block order isn't guaranteed stable.
    source_confidence_by_text = {}
    h_img, w_img = img.shape[:2]

    if lang != 'ja':
        import pytesseract

    for ocr_index, blk in enumerate(blk_list):
        x1, y1, x2, y2 = blk.xyxy
        # Add 5px padding for safer boundary OCR
        x1_pad = max(0, x1 - 5)
        y1_pad = max(0, y1 - 5)
        x2_pad = min(w_img, x2 + 5)
        y2_pad = min(h_img, y2 + 5)

        crop = img[y1_pad:y2_pad, x1_pad:x2_pad]
        if crop.size == 0:
            continue

        pil_crop = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))

        if lang == 'ja':
            # TASK-33: MangaOcr's high-level API exposes no per-call
            # confidence score, unlike Tesseract - honestly leave
            # tess_conf unset rather than fabricate one. The dictionary
            # fallback below is also skipped for Japanese source text
            # (an English word list is meaningless against Japanese OCR
            # output), so source_confidence stays unavailable for this
            # language path - disclosed, not silently guessed at.
            txt = mocr(pil_crop).strip()
            tess_conf = None
        else:
            # TASK-33: image_to_data (not image_to_string) is the only
            # pytesseract call that exposes per-word confidence.
            data = pytesseract.image_to_data(pil_crop, lang='eng', config='--psm 6', output_type=pytesseract.Output.DICT)
            words, confs = [], []
            for w_idx, word in enumerate(data.get("text", [])):
                word = word.strip()
                if not word:
                    continue
                words.append(word)
                try:
                    c = float(data.get("conf", [])[w_idx])
                except (IndexError, TypeError, ValueError):
                    c = -1
                if c >= 0:
                    confs.append(c)
            txt = " ".join(words)
            tess_conf = (sum(confs) / len(confs)) if confs else None

        txt = txt.replace("\n", " ").replace("\r", " ").strip()
        txt = " ".join(txt.split())
        # TASK-33: strip anchored edge noise BEFORE translation - garbage
        # the LLM never sees can't leak into the translated text either.
        txt = clean_ocr_edge_noise(txt, page_name=page_basename, bubble_ref=f"block{ocr_index}")

        if txt:
            page_ocr_texts.append(txt)
            block_crops.append((blk, txt))
            dict_frac = real_word_fraction(txt) if lang != 'ja' else None
            # "low" if EITHER available signal says so - a false positive
            # here just means a human glances at a translation that's
            # actually fine; a false negative means real garbage ships
            # unflagged. Erring toward surfacing more, not less.
            # Threshold 68 (not the original 60), set from real comparative
            # data across 11 real frieren bubbles during TASK-33 testing:
            # legitimately good bubbles scored 71.0-95.4, one confirmed-
            # garbage bubble scored 24.0, and the reported-scrambled p020
            # _b12 (individually-real words in jumbled order - the
            # dictionary check alone can't catch that, since it only
            # checks vocabulary, not order) scored 61.5 - between the two
            # clusters. 68 sits with margin on both sides of that gap.
            is_low = (tess_conf is not None and tess_conf < 68) or (dict_frac is not None and dict_frac < 0.5)
            if tess_conf is not None or dict_frac is not None:
                source_confidence_by_text[txt] = {
                    "tesseract_confidence": round(tess_conf, 1) if tess_conf is not None else None,
                    "dictionary_real_word_fraction": round(dict_frac, 3) if dict_frac is not None else None,
                    "low": is_low,
                }

    if not page_ocr_texts:
        return img, img, page_quality_flags, page_bubbles_meta

    # Stage B: larger radius + per-block flat-fill fallback for cases the
    # wider TELEA radius alone still leaves a "ghost" of the original
    # text under (bold/large source fonts).
    inpainted = robust_inpaint(img, mask_refined, blk_list)

    # TASK-67: geometric bubble classification on the INPAINTED page
    # (glyphs gone, balloon outline intact - the flood-fill needs a clear
    # interior). Always computed and stored in bubbles_meta; feeds the
    # translation prompt only when enable_bubble_tone is on.
    bubble_class_by_text = {}
    bubble_class_full = {}
    try:
        from common.bubble_shape import classify_bubble_shape
        gray_clean = cv2.cvtColor(inpainted, cv2.COLOR_BGR2GRAY)
        for blk, orig_txt in block_crops:
            bx1, by1, bx2, by2 = blk.xyxy
            cls = classify_bubble_shape(gray_clean, [bx1, by1, bx2, by2])
            bubble_class_full[orig_txt] = cls
            if cls["confidence"] >= 0.6:
                bubble_class_by_text[orig_txt] = cls["bubble_class"]
    except Exception as e:
        log(f"bubble classification skipped: {e}")

    translations = translate_batch_llm(page_ocr_texts, lang, glossary, api_url, overrides=overrides,
                                       cast_characters=_CAST_CACHE["chars"],
                                       tones=bubble_class_by_text if _TONE_CFG["enabled"] else None)

    pil_img = Image.fromarray(cv2.cvtColor(inpainted, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil_img)

    page_stem = os.path.splitext(page_basename)[0]
    bubble_ids = assign_bubble_ids(page_stem, [blk for blk, _ in block_crops])
    bbox_overrides = bbox_overrides or {}
    font_size_overrides = font_size_overrides or {}

    # TASK-36: compute every block's CORE (unpadded) box up front, in its
    # own pass, before any block's padding is decided - this is what lets
    # padding be neighbor-aware without an ordering dependency (bubble A's
    # padding needs to know about bubble B's core box regardless of which
    # one is processed first). A manually-overridden bubble contributes
    # its override box here too, so its neighbors' padding respects it.
    core_boxes = []
    for blk, orig_txt in block_crops:
        if orig_txt in bbox_overrides:
            ox1, oy1, ox2, oy2 = bbox_overrides[orig_txt]
            core_boxes.append((int(ox1), int(oy1), int(ox2), int(oy2)))
        else:
            core_boxes.append(_get_bubble_core_box(blk, mask_refined, img.shape))

    for block_index, (blk, orig_txt) in enumerate(block_crops):
        translated_txt = translations.get(orig_txt, orig_txt)
        if not translated_txt:
            continue

        # Stage C: use the actual cleaned/masked region for this block
        # (padded) as the typeset box, instead of the OCR-tight blk.xyxy -
        # unless a human manually placed this exact box (TASK-36), in
        # which case it's used verbatim, no padding applied on top of it.
        if orig_txt in bbox_overrides:
            x1, y1, x2, y2 = core_boxes[block_index]
        else:
            other_core_boxes = core_boxes[:block_index] + core_boxes[block_index + 1:]
            x1, y1, x2, y2 = _apply_padding_neighbor_safe(core_boxes[block_index], img.shape, other_core_boxes=other_core_boxes)
        w_box = x2 - x1
        h_box = y2 - y1

        # Stage D: floor/ceiling-clamped font size, hyphenated hard-wrap.
        # TASK-30: get_bubble_box's box is a RECTANGLE approximating what's
        # usually an oval speech bubble - the true usable width near the
        # box's top/bottom is narrower than the rectangle's full width, so
        # a line landing there can visually press against the bubble's
        # curved edge even though it satisfies the (looser) rectangular
        # width check post_render_check/fit_text use. Fit/wrap against a
        # narrowed width as a uniform safety margin (still centered and
        # rendered against the FULL box, so there's breathing room on both
        # sides everywhere, not just at the true vertical center).
        fit_w_box = max(1, int(w_box * TEXT_WIDTH_SAFETY_MARGIN))
        if orig_txt in font_size_overrides:
            best_size = int(font_size_overrides[orig_txt])
            try:
                _font = ImageFont.truetype(font_path, best_size)
            except Exception:
                _font = ImageFont.load_default()
            wrapped_lines = wrap_text(translated_txt, _font, fit_w_box)
        else:
            best_size, wrapped_lines = fit_text(translated_txt, font_path, fit_w_box, h_box)
        draw_text_centered(draw, wrapped_lines, font_path, best_size, (x1, y1, x2, y2))

        # Stage E: flag anything that still looks wrong for later review.
        block_flags = []
        post_render_check(block_flags, page_basename, block_index, best_size, wrapped_lines,
                           (x1, y1, x2, y2), font_path, min_size=12)
        page_quality_flags.extend(block_flags)

        bubble_quality_flags = dict(block_flags[0]) if block_flags else {}
        # TASK-33: source_confidence is BEFORE-translation OCR quality -
        # a separate axis from render geometry (overflow_ratio/font_size
        # above), never merged into the same sub-dict/reason. A bubble
        # can be flagged for both at once (bad OCR AND bad render fit).
        src_conf = source_confidence_by_text.get(orig_txt)
        if src_conf:
            bubble_quality_flags["source_confidence"] = src_conf
            if src_conf["low"]:
                page_quality_flags.append({
                    "page": page_basename,
                    "bubble_id": bubble_ids[block_index],
                    "reason": "low_source_confidence",
                    "original_text": orig_txt,
                    **src_conf,
                })

        page_bubbles_meta.append({
            "id": bubble_ids[block_index],
            "bbox": [x1, y1, x2, y2],
            # TASK-26: bbox is computed in THIS image's pixel space (the
            # pre-downscale page passed into process_page), but cleaned/
            # translated files get downscaled to max-width/height in a
            # later pass. Without recording that reference size, any UI
            # code overlaying/cropping bbox against the (smaller) final
            # file silently drifts - the further from (0,0), the worse.
            "bbox_ref_size": [w_img, h_img],
            "original_text": orig_txt,
            "translated_text": translated_txt,
            "bubble_class": (bubble_class_full.get(orig_txt) or {}).get("bubble_class"),
            "bubble_class_confidence": (bubble_class_full.get(orig_txt) or {}).get("confidence"),
            "quality_flags": bubble_quality_flags
        })

    # Stage E (continued, TASK-36): overlap detection needs every bubble's
    # FINAL box already known, so it runs once after the loop rather than
    # per-block inside it. Merges into each bubble's own quality_flags
    # (what the manual-edit side panel reads) as well as the book-wide
    # page_quality_flags list (what the Auto-flagged list/quality_flags.json
    # reads) - a bubble can carry both a render-geometry flag (overflow/
    # min_size) and a box_overlap flag at the same time, they're additive.
    overlap_by_id = compute_box_overlap_flags(page_bubbles_meta, page_basename)
    for bubble in page_bubbles_meta:
        overlap = overlap_by_id.get(bubble["id"])
        if overlap:
            bubble["quality_flags"] = {**bubble["quality_flags"], **overlap}
            page_quality_flags.append({
                "page": page_basename,
                "bubble_id": bubble["id"],
                "reason": "box_overlap",
                "box": bubble["bbox"],
                "overlapping_with": overlap["overlapping_with"],
                "iou": overlap["iou"],
            })

    final_img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    return final_img, inpainted, page_quality_flags, page_bubbles_meta

def _persist_regenerated_page(book_dir, page_filename, final_img, cleaned_img, page_flags, new_bubbles):
    """Shared by regenerate_single_page (on-demand CLI mode, TASK-21) and
    apply_pending_manga_edits (in-loop live regen, TASK-23): writes the
    regenerated page's images, bubbles_meta.json, and merges its quality
    flags into the book-wide quality_flags.json. Returns the IoU
    old-id -> new-id-or-None reconciliation mapping."""
    page_stem = os.path.splitext(page_filename)[0]
    cleaned_dir = os.path.join(book_dir, "cleaned")
    translated_dir = os.path.join(book_dir, "translated")
    os.makedirs(cleaned_dir, exist_ok=True)
    os.makedirs(translated_dir, exist_ok=True)
    cv2.imwrite(os.path.join(cleaned_dir, page_filename), cleaned_img)
    cv2.imwrite(os.path.join(translated_dir, page_filename), final_img)

    meta_path = os.path.join(book_dir, "bubbles_meta", f"{page_stem}.json")
    old_bubbles = []
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                old_bubbles = json.load(f)
        except Exception:
            old_bubbles = []

    write_bubbles_meta(book_dir, page_stem, new_bubbles)
    id_mapping = match_bubbles_iou(old_bubbles, new_bubbles)

    quality_flags_path = os.path.join(book_dir, "quality_flags.json")
    all_flags = []
    if os.path.exists(quality_flags_path):
        try:
            with open(quality_flags_path, "r", encoding="utf-8") as f:
                all_flags = json.load(f)
        except Exception:
            all_flags = []
    all_flags = [f for f in all_flags if f.get("page") != page_filename]
    all_flags.extend(page_flags)
    with open(quality_flags_path, "w", encoding="utf-8") as f:
        json.dump(all_flags, f, ensure_ascii=False, indent=2)

    return id_mapping, old_bubbles


def apply_pending_manga_edits(book_dir, temp_in, glossary, api_url, lang, detector, mocr, font_path):
    """TASK-23: called between pages in main()'s loop while this book is
    still status=running. Applies any live manga bubble edits that target
    a page already processed earlier in this run (or a prior run, on
    resume), regenerating just that page in-process — reuses the already-
    extracted temp_in source dir instead of regenerate_single_page's
    on-demand full re-extraction, since we're already inside main()'s
    loop with it available."""
    slug = os.path.basename(book_dir)
    all_manga_edits = edit_store.list_edits(slug, mode="manga")
    pending = [e for e in all_manga_edits if e.get("status") == "pending"]
    if not pending:
        return

    pages_needing_regen = set()
    for edit in pending:
        target_id = edit.get("target_id", "")
        if "#" in target_id:
            pages_needing_regen.add(target_id.split("#", 1)[0])

    # Bug found live during TASK-36 testing: process_page() re-runs the
    # WHOLE page from scratch, so an edit already "regenerated" from a
    # PAST run is invisible to a LATER regen triggered by a different
    # bubble's new pending edit on the same page, unless it's included
    # again here too - otherwise it silently reverts to a fresh (possibly
    # different) auto-translation. pages_needing_regen above still gates
    # WHETHER a page regenerates at all (only pages with a genuinely new
    # pending edit qualify); once a page qualifies, every "regenerated"
    # edit for it rides along too, not just the new one that triggered it.
    edits_by_page = {}
    for edit in all_manga_edits:
        if edit.get("status") not in ("pending", "regenerated"):
            continue
        target_id = edit.get("target_id", "")
        if "#" not in target_id:
            continue
        page_filename = target_id.split("#", 1)[0]
        if page_filename in pages_needing_regen:
            edits_by_page.setdefault(page_filename, []).append(edit)

    for page_filename, edits_for_page in edits_by_page.items():
        meta_path = os.path.join(book_dir, "bubbles_meta", f"{os.path.splitext(page_filename)[0]}.json")
        if not os.path.exists(meta_path):
            continue  # page not processed yet this run - nothing to regenerate against
        page_path = os.path.join(temp_in, page_filename)
        if not os.path.exists(page_path):
            continue

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                old_bubbles = json.load(f)
        except Exception:
            old_bubbles = []

        overrides = {}
        raw_bbox_overrides = {}
        for edit in edits_for_page:
            _, bubble_id = edit["target_id"].split("#", 1)
            bubble = next((b for b in old_bubbles if b.get("id") == bubble_id), None)
            if not bubble:
                continue
            orig_text = bubble["original_text"]
            if edit.get("field") == "translated_text":
                overrides[orig_text] = edit["edited_value"]
            elif edit.get("field") == "manual_bbox_override":
                # TASK-36: same reference-scaling deferral as
                # regenerate_single_page - kept unscaled here, scaled
                # below once img.shape is known.
                ev = edit.get("edited_value") or {}
                entry = raw_bbox_overrides.setdefault(orig_text, {})
                entry.update(ev)
        if not overrides and not raw_bbox_overrides:
            continue

        img = cv2.imread(page_path)
        if img is None:
            continue

        bbox_overrides, font_size_overrides = _scale_bbox_overrides(raw_bbox_overrides, img.shape)

        final_img, cleaned_img, page_flags, new_bubbles = process_page(
            img, page_filename, glossary, api_url, lang, detector, mocr, font_path,
            overrides=overrides, bbox_overrides=bbox_overrides, font_size_overrides=font_size_overrides
        )
        id_mapping, _ = _persist_regenerated_page(book_dir, page_filename, final_img, cleaned_img, page_flags, new_bubbles)

        for edit in edits_for_page:
            _, bubble_id = edit["target_id"].split("#", 1)
            new_id = id_mapping.get(bubble_id)
            status = "regenerated" if new_id else "orphaned"
            edit_store.mark_status(slug, edit["id"], status, applied_at=datetime.now().isoformat())
        log(f"[LiveEdit] Applied {len(overrides)} text edit(s) and {len(raw_bbox_overrides)} geometry/font edit(s) to already-completed page '{page_filename}'.")


def _scale_bbox_overrides(raw_bbox_overrides, actual_img_shape):
    """TASK-36: raw_bbox_overrides is {original_text: {"bbox": [...],
    "ref_size": [w,h], "font_size": int}} (any subset of fields present)
    as captured by the client against whatever image dimensions were on
    screen at edit time. Scales bbox/font_size to actual_img_shape (the
    real dimensions of the image this regen is about to process) - same
    reference-scaling approach TASK-27 established for the read-only
    crop preview, so a box/size picked against a downscaled preview
    still lands correctly on the actual (possibly larger) working image.
    Returns (bbox_overrides, font_size_overrides), each keyed exactly as
    process_page() expects (by original_ocr_text)."""
    h_actual, w_actual = actual_img_shape[:2]
    bbox_overrides = {}
    font_size_overrides = {}
    for orig_text, entry in (raw_bbox_overrides or {}).items():
        ref_size = entry.get("ref_size")
        rw, rh = (ref_size if ref_size else (w_actual, h_actual))
        sx = w_actual / rw if rw else 1.0
        sy = h_actual / rh if rh else 1.0
        if "bbox" in entry:
            x1, y1, x2, y2 = entry["bbox"]
            bbox_overrides[orig_text] = [
                int(max(0, min(w_actual, x1 * sx))),
                int(max(0, min(h_actual, y1 * sy))),
                int(max(0, min(w_actual, x2 * sx))),
                int(max(0, min(h_actual, y2 * sy))),
            ]
        if "font_size" in entry:
            # Font size scales with the same horizontal ratio as bbox -
            # a size picked while looking at a downscaled preview should
            # come out proportionally the same in the actual image.
            font_size_overrides[orig_text] = max(8, int(entry["font_size"] * sx))
    return bbox_overrides, font_size_overrides


def regenerate_single_page(args, glossary, detector, mocr):
    """TASK-21: re-runs the FULL A-E pipeline (process_page) on just one
    page, for a manual bubble-edit regen. Deliberately does NOT skip
    straight to typeset on the existing cleaned image - re-running
    detection/dedup/inpaint too means the TASK-20 fixes (ghost-text
    removal, deduped blocks) still apply to a regenerated page, not just
    a fresh one. Re-extracts the target page from the ORIGINAL source
    (same extract_manga_pages() path/PDF/CBZ/directory every normal run
    uses) rather than reading a persisted "source" copy, since archive
    sources are only ever extracted to a temp dir today - this keeps
    directory/CBZ/PDF sources all working uniformly with no special-casing.

    Prints a single JSON line to stdout on success:
    {"status": "success", "bubble_id_mapping": {old_id: new_id_or_null},
    "bubbles": [...new bubbles_meta entries...]} - kbg_web/app.py's
    regenerate-manga-page route parses this to reconcile pending edits
    (mark matched ones "regenerated", unmatched ones "orphaned")."""
    init_cast_registry(os.path.abspath(os.path.join(os.path.dirname(args.output), "..")))
    init_bubble_tone(os.path.abspath(os.path.join(os.path.dirname(args.output), "..")))
    init_honorifics(os.path.abspath(os.path.join(os.path.dirname(args.output), "..")))
    page_filename = args.regenerate_page

    overrides = {}
    if args.overrides_json and os.path.exists(args.overrides_json):
        try:
            with open(args.overrides_json, "r", encoding="utf-8") as f:
                overrides = json.load(f)
        except Exception as e:
            log(f"Warning: Failed to load overrides JSON: {e}")

    raw_bbox_overrides = {}
    if getattr(args, "bbox_overrides_json", None) and os.path.exists(args.bbox_overrides_json):
        try:
            with open(args.bbox_overrides_json, "r", encoding="utf-8") as f:
                raw_bbox_overrides = json.load(f)
        except Exception as e:
            log(f"Warning: Failed to load bbox-overrides JSON: {e}")

    regen_temp_in = tempfile.mkdtemp()
    try:
        extract_manga_pages(args.input, regen_temp_in)
        page_path = os.path.join(regen_temp_in, page_filename)
        if not os.path.exists(page_path):
            log(f"Error: page '{page_filename}' not found after re-extracting source '{args.input}'.")
            print(json.dumps({"status": "error", "message": f"page '{page_filename}' not found in source"}))
            sys.exit(1)

        img = cv2.imread(page_path)
        if img is None:
            log(f"Error: failed to read page '{page_path}'.")
            print(json.dumps({"status": "error", "message": "failed to read page image"}))
            sys.exit(1)

        # TASK-36: scale manual bbox/font-size overrides against THIS
        # image's actual dimensions - only knowable now, after loading it.
        bbox_overrides, font_size_overrides = _scale_bbox_overrides(raw_bbox_overrides, img.shape)

        font_path = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
        if not os.path.exists(font_path):
            font_path = None

        final_img, cleaned_img, page_flags, new_bubbles = process_page(
            img, page_filename, glossary, args.api_url, args.lang, detector, mocr, font_path,
            overrides=overrides, bbox_overrides=bbox_overrides, font_size_overrides=font_size_overrides
        )

        book_dir = os.path.abspath(os.path.join(os.path.dirname(args.output), ".."))
        id_mapping, _ = _persist_regenerated_page(book_dir, page_filename, final_img, cleaned_img, page_flags, new_bubbles)

        log(f"Regenerated page '{page_filename}': {len(new_bubbles)} bubble(s), {len(page_flags)} quality flag(s).")
        print(json.dumps({"status": "success", "bubble_id_mapping": id_mapping, "bubbles": new_bubbles}, ensure_ascii=False))
    finally:
        shutil.rmtree(regen_temp_in, ignore_errors=True)

# TASK-25: maps a book's target_lang to the Tesseract language-data code
# needed to OCR its *translated* text (distinct from the source-language
# OCR pytesseract/mocr calls elsewhere in this file already do).
_TESS_LANG_MAP = {"uk": "ukr", "ru": "rus", "en": "eng"}


def _resolve_manga_source(book_dir, slug):
    """Same source resolution kbg_web/app.py already duplicates in two
    routes (edit_regenerate_manga_page, run_conversion_api): a `source/`
    directory takes priority, else a `<slug>.<ext>` archive in book_dir."""
    source_dir = os.path.join(book_dir, "source")
    if os.path.isdir(source_dir):
        return source_dir
    for ext in [".cbz", ".cbr", ".cb7", ".zip", ".rar", ".pdf", ".epub"]:
        candidate = os.path.join(book_dir, f"{slug}{ext}")
        if os.path.exists(candidate):
            return candidate
    return ""


def backfill_box_overlap_flags(slug):
    """TASK-36: retroactively computes box_overlap flags for a book's
    ALREADY-EXISTING bubbles_meta/*.json (no re-detection, no OCR, no
    model loading needed - the boxes are already on disk). Merges
    box_overlap/overlapping_with/iou into each affected bubble's own
    quality_flags dict (idempotent - also CLEARS a stale box_overlap key
    from a bubble that no longer overlaps anything, e.g. after a manual
    fix), and replaces only the box_overlap-reason entries in
    quality_flags.json, preserving any other reason (overflow/min_size)
    already recorded there for the same page."""
    repo_dir_local = os.path.abspath(os.path.join(os.path.dirname(__file__), "."))
    paths = resolve_book_paths(repo_dir_local, slug)
    book_dir = paths["book_dir"]
    meta_dir = os.path.join(book_dir, "bubbles_meta")
    if not os.path.isdir(meta_dir):
        log(f"Error: no bubbles_meta/ directory for '{slug}' - nothing to scan.")
        sys.exit(1)

    quality_flags_path = os.path.join(book_dir, "quality_flags.json")
    all_flags = []
    if os.path.exists(quality_flags_path):
        try:
            with open(quality_flags_path, "r", encoding="utf-8") as f:
                all_flags = json.load(f)
        except Exception:
            all_flags = []
    all_flags = [f for f in all_flags if f.get("reason") != "box_overlap"]

    pages_scanned = 0
    pages_with_overlap = 0
    bubbles_affected = 0
    pairs_found = 0

    for fname in sorted(os.listdir(meta_dir)):
        if not fname.endswith(".json"):
            continue
        meta_path = os.path.join(meta_dir, fname)
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                bubbles = json.load(f)
        except Exception as e:
            log(f"Warning: failed to read '{fname}': {e}")
            continue
        pages_scanned += 1
        page_name = os.path.splitext(fname)[0]
        overlap_by_id = compute_box_overlap_flags(bubbles, page_name)

        changed = False
        for bubble in bubbles:
            qf = bubble.get("quality_flags") or {}
            had_overlap = qf.get("box_overlap", False)
            overlap = overlap_by_id.get(bubble.get("id"))
            if overlap:
                new_qf = {k: v for k, v in qf.items() if k not in ("box_overlap", "overlapping_with", "iou")}
                new_qf.update(overlap)
                if new_qf != qf:
                    bubble["quality_flags"] = new_qf
                    changed = True
            elif had_overlap:
                # No longer overlapping (e.g. fixed manually since the
                # last scan) - clear the stale flag rather than leaving
                # it lying, so a re-run of this backfill is idempotent.
                bubble["quality_flags"] = {k: v for k, v in qf.items() if k not in ("box_overlap", "overlapping_with", "iou")}
                changed = True

        if changed:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(bubbles, f, ensure_ascii=False, indent=2)

        if overlap_by_id:
            pages_with_overlap += 1
            bubbles_affected += len(overlap_by_id)
            pairs_found += sum(len(v["overlapping_with"]) for v in overlap_by_id.values()) // 2
            for bubble in bubbles:
                overlap = overlap_by_id.get(bubble.get("id"))
                if overlap:
                    all_flags.append({
                        "page": page_name,
                        "bubble_id": bubble["id"],
                        "reason": "box_overlap",
                        "box": bubble.get("bbox"),
                        "overlapping_with": overlap["overlapping_with"],
                        "iou": overlap["iou"],
                    })

    with open(quality_flags_path, "w", encoding="utf-8") as f:
        json.dump(all_flags, f, ensure_ascii=False, indent=2)

    log(f"box_overlap backfill for '{slug}': {pages_scanned} pages scanned, "
        f"{pages_with_overlap} pages with overlap, {pairs_found} overlapping pairs, "
        f"{bubbles_affected} bubbles flagged.")


def backfill_bubbles_meta(slug, lang, detector, mocr):
    """TASK-25: for manga translated before TASK-20/21 existed, there is no
    bubbles_meta/ directory, so the manual-edit overlay UI has nothing to
    draw. STRICTLY read-only w.r.t. cleaned/translated PNGs - never
    rewrites them, never re-translates via LLM. Only ADDS
    bubbles_meta/<page_stem>.json per already-translated page.

    bbox geometry comes from re-running detect->dedupe->get_bubble_box on
    the untouched SOURCE image (the same Stage A/C calls process_page()
    makes). original_text is OCR of the source crop (same as
    process_page's own source-OCR box/padding). translated_text is OCR of
    the EXISTING translated PNG at the same typeset box - never a fresh
    LLM translate call. quality_flags is deliberately null (TASK-20's
    post_render_check never ran against these pages' actual historical
    render, so approximating it would be a lie); every entry is tagged
    backfilled=true so the UI can render it distinctly instead of
    implying "checked and clean"."""
    paths = resolve_book_paths(repo_dir, slug)
    book_dir = paths["book_dir"]
    translated_dir = os.path.join(book_dir, "translated")
    bubbles_meta_dir = os.path.join(book_dir, "bubbles_meta")

    manga_input = _resolve_manga_source(book_dir, slug)
    if not manga_input:
        log(f"Error: no manga source (source/ dir or {slug}.<ext> archive) found in {book_dir}.")
        return
    if not os.path.isdir(translated_dir):
        log(f"Error: no translated/ directory found for '{slug}' - nothing to backfill against.")
        return

    target_lang = paths.get("target_lang", "uk")
    tess_target_lang = _TESS_LANG_MAP.get(target_lang, "eng")

    temp_in = tempfile.mkdtemp()
    try:
        extract_manga_pages(manga_input, temp_in)
        source_pages = {
            os.path.basename(p): p
            for p in [os.path.join(temp_in, f) for f in os.listdir(temp_in)]
            if p.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))
        }

        os.makedirs(bubbles_meta_dir, exist_ok=True)
        translated_files = natsorted([
            f for f in os.listdir(translated_dir)
            if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))
        ])

        # Always needed for translated_text OCR below - target-language
        # text is never Japanese/mocr, regardless of the manga's source lang.
        import pytesseract

        processed = 0
        skipped_existing = 0
        skipped_no_source = 0
        skipped_unreadable = 0

        for basename in translated_files:
            page_stem = os.path.splitext(basename)[0]
            meta_path = os.path.join(bubbles_meta_dir, f"{page_stem}.json")
            if os.path.exists(meta_path):
                skipped_existing += 1
                continue

            source_path = source_pages.get(basename)
            if not source_path:
                log(f"Warning: no matching source page for translated '{basename}' - skipping backfill for this page.")
                skipped_no_source += 1
                continue

            translated_path = os.path.join(translated_dir, basename)
            src_img = cv2.imread(source_path)
            trans_img = cv2.imread(translated_path)
            if src_img is None or trans_img is None:
                log(f"Warning: failed to read image(s) for '{basename}' - skipping.")
                skipped_unreadable += 1
                continue

            mask, mask_refined, blk_list = detector(src_img, refine_mode=REFINEMASK_INPAINT, keep_undetected_mask=True)
            blk_list = dedupe_blocks(blk_list)

            if not blk_list:
                write_bubbles_meta(book_dir, page_stem, [])
                processed += 1
                continue

            # Per the task spec: run robust_inpaint to mirror the same
            # code path a real regen would use. Its output is discarded -
            # get_bubble_box only reads mask_refined (unaffected by this
            # call, robust_inpaint doesn't mutate it), so this has no
            # bearing on the computed bboxes; included for parity anyway,
            # never written to cleaned/.
            _ = robust_inpaint(src_img, mask_refined, blk_list)

            h_img, w_img = src_img.shape[:2]
            th_img, tw_img = trans_img.shape[:2]
            bubble_ids = assign_bubble_ids(page_stem, blk_list)
            entries = []

            # TASK-36: same neighbor-aware padding as process_page() - see
            # that function's comment for why core boxes are computed in
            # their own pass before any padding decision.
            core_boxes = [_get_bubble_core_box(blk, mask_refined, src_img.shape) for blk in blk_list]

            for idx, blk in enumerate(blk_list):
                other_core_boxes = core_boxes[:idx] + core_boxes[idx + 1:]
                bx1, by1, bx2, by2 = _apply_padding_neighbor_safe(core_boxes[idx], src_img.shape, other_core_boxes=other_core_boxes)

                # original_text: OCR the SOURCE crop at the OCR-tight
                # padded box - identical to process_page's own source-OCR step.
                sx1, sy1, sx2, sy2 = blk.xyxy
                sx1p, sy1p = max(0, sx1 - 5), max(0, sy1 - 5)
                sx2p, sy2p = min(w_img, sx2 + 5), min(h_img, sy2 + 5)
                src_crop = src_img[sy1p:sy2p, sx1p:sx2p]
                original_text = ""
                if src_crop.size > 0:
                    pil_src_crop = Image.fromarray(cv2.cvtColor(src_crop, cv2.COLOR_BGR2RGB))
                    if lang == 'ja':
                        original_text = mocr(pil_src_crop).strip()
                    else:
                        original_text = pytesseract.image_to_string(pil_src_crop, lang='eng', config='--psm 6').strip()
                    original_text = " ".join(original_text.replace("\n", " ").replace("\r", " ").split())

                # translated_text: OCR the EXISTING translated PNG at the
                # get_bubble_box-derived typeset box - never re-translated.
                # TASK-26: bx1..by2 are in SOURCE pixel space, but
                # trans_img is frequently downscaled relative to source
                # (confirmed 156/193 frieren pages) - scale before cropping,
                # or this reads the wrong region entirely (the original bug:
                # "поле Переклад містить обрізані фрагменти правильного тексту").
                scale_x = tw_img / w_img if w_img else 1.0
                scale_y = th_img / h_img if h_img else 1.0
                tx1, ty1 = max(0, int(bx1 * scale_x)), max(0, int(by1 * scale_y))
                tx2, ty2 = min(tw_img, int(bx2 * scale_x)), min(th_img, int(by2 * scale_y))
                trans_crop = trans_img[ty1:ty2, tx1:tx2]
                translated_text = ""
                if trans_crop.size > 0:
                    pil_trans_crop = Image.fromarray(cv2.cvtColor(trans_crop, cv2.COLOR_BGR2RGB))
                    try:
                        translated_text = pytesseract.image_to_string(pil_trans_crop, lang=tess_target_lang, config='--psm 6').strip()
                    except Exception as e:
                        log(f"Warning: OCR of translated crop failed for {page_stem} bubble {idx} (tesseract lang '{tess_target_lang}'): {e}")
                    translated_text = " ".join(translated_text.replace("\n", " ").replace("\r", " ").split())

                entries.append({
                    "id": bubble_ids[idx],
                    "bbox": [bx1, by1, bx2, by2],
                    # TASK-26: bbox is in the SOURCE image's pixel space
                    # (w_img/h_img below), but translated/cleaned PNGs from
                    # the pre-TASK-20/21 pipeline were frequently downscaled
                    # relative to source (confirmed: 156/193 frieren pages
                    # differ) - record it so the UI can scale correctly
                    # instead of assuming 1:1 with whatever's on screen.
                    "bbox_ref_size": [w_img, h_img],
                    "original_text": original_text,
                    "translated_text": translated_text,
                    "quality_flags": None,
                    "backfilled": True
                })

            write_bubbles_meta(book_dir, page_stem, entries)
            processed += 1
            log(f"Backfilled bubbles_meta for '{basename}': {len(entries)} bubble(s).")

        log(f"Backfill complete for '{slug}': {processed} page(s) processed, "
            f"{skipped_existing} already had bubbles_meta, "
            f"{skipped_no_source} had no matching source page, "
            f"{skipped_unreadable} had unreadable image(s).")
    finally:
        shutil.rmtree(temp_in, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Manga translation pipeline (Segmentation -> OCR -> LLM translation -> Inpainting -> Typesetting)")
    parser.add_argument("--input", help="Path to input manga (PDF, CBZ, CBR, CB7, EPUB, or image folder) - required unless --backfill-bubbles-meta")
    parser.add_argument("--output", help="Path to output CBZ archive or directory - required unless --backfill-bubbles-meta")
    parser.add_argument("--lang", default="en", choices=["en", "ja"], help="Manga source language (en=English, ja=Japanese)")
    parser.add_argument("--glossary", help="Path to glossary.json file")
    parser.add_argument("--api-url", default="http://127.0.0.1:8081/v1/chat/completions", help="llama-server API Endpoint")
    parser.add_argument("--progress-file", help="Path to write progress JSON")
    parser.add_argument("--left-to-right", action="store_true", help="Set reading direction to LTR")
    parser.add_argument("--no-translate", action="store_true", help="Skip translation (copy original images)")
    parser.add_argument("--no-ebook", action="store_true", help="Skip AZW3 generation via Mapaki")
    parser.add_argument("--max-width", type=int, default=1280, help="Maximum width of pages (0 to disable)")
    parser.add_argument("--max-height", type=int, default=1920, help="Maximum height of pages (0 to disable)")
    parser.add_argument("--regenerate-page", help="TASK-21: re-run the full A-E pipeline on just this one page filename (manual bubble edit regen), instead of the whole book")
    parser.add_argument("--overrides-json", help="TASK-21: path to a JSON {original_ocr_text: edited_translation} map, used only with --regenerate-page")
    parser.add_argument("--bbox-overrides-json", help="TASK-36: path to a JSON {original_ocr_text: {bbox, ref_size, font_size}} map (any subset of fields), used only with --regenerate-page")
    parser.add_argument("--backfill-bubbles-meta", action="store_true", help="TASK-25: read-only backfill of bubbles_meta/ for a book translated before TASK-20/21 existed - never touches cleaned/translated PNGs or re-translates")
    parser.add_argument("--force-retranslate", action="store_true", help="Ignore resume: re-translate every page from scratch even if a translated page already exists (the 'Clean Pages' option).")
    parser.add_argument("--clean-run-id", default=None,
                        help="Identifies ONE deliberate --force-retranslate sweep across possibly-many "
                             "Termux-restart/auto-resume cycles. Without this, a resumed force-retranslate "
                             "run has no way to tell 'a page I already redid THIS sweep' apart from 'a page "
                             "that merely has some old file on disk from a previous, unrelated run' - real "
                             "incident 2026-07-19: an interrupted clean run's auto-resume state was patched "
                             "to drop --force-retranslate entirely (to stop endless full-restarts), which "
                             "then silently let 100+ pages fall back to 3-day-old stale translations instead "
                             "of the intended fresh ones. Pages already completed under a given run-id are "
                             "tracked in <book_dir>/.force_retranslate_progress.json and skipped on resume "
                             "WITHOUT needing to drop the flag; a different/new run-id starts that tracking "
                             "over from empty.")
    parser.add_argument("--backfill-box-overlap-flags", action="store_true", help="TASK-36: retroactively compute box_overlap quality flags for a book's existing bubbles_meta/ - no model loading, no re-detection, just reads/writes JSON")
    parser.add_argument("--slug", help="Book slug - required with --backfill-bubbles-meta / --backfill-box-overlap-flags")
    args = parser.parse_args()

    if args.backfill_box_overlap_flags:
        if not args.slug:
            log("Error: --backfill-box-overlap-flags requires --slug.")
            sys.exit(1)
        backfill_box_overlap_flags(args.slug)
        return

    if args.backfill_bubbles_meta:
        if not args.slug:
            log("Error: --backfill-bubbles-meta requires --slug.")
            sys.exit(1)
    elif not args.input or not args.output:
        log("Error: --input and --output are required unless --backfill-bubbles-meta is set.")
        sys.exit(1)

    # Load glossary if provided
    glossary = {}
    if args.glossary and os.path.exists(args.glossary):
        try:
            with open(args.glossary, "r", encoding="utf-8") as f:
                glossary = json.load(f)
            log(f"Loaded glossary with {len(glossary)} entries.")
        except Exception as e:
            log(f"Warning: Failed to load glossary: {e}")

    # Set up models
    detector_model_path = download_detector_model()
    log("Initializing comic text detector...")
    detector = TextDetector(model_path=detector_model_path, device='cpu')
    
    mocr = None
    if args.lang == 'ja':
        log("Initializing Japanese Manga OCR...")
        from manga_ocr import MangaOcr
        mocr = MangaOcr()
    else:
        log("Using Tesseract OCR for English...")
        import pytesseract

    if args.backfill_bubbles_meta:
        backfill_bubbles_meta(args.slug, args.lang, detector, mocr)
        return

    if args.regenerate_page:
        regenerate_single_page(args, glossary, detector, mocr)
        return

    # Create temporary directories for processing
    temp_in = tempfile.mkdtemp()
    temp_out = tempfile.mkdtemp()

    try:
        # Extract source manga to temporary folder
        extract_manga_pages(args.input, temp_in)
        pages = natsorted([os.path.join(temp_in, f) for f in os.listdir(temp_in) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
        
        if not pages:
            log("No pages/images found in input.")
            return

        log(f"Processing {len(pages)} pages...")
        
        font_path = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
        if not os.path.exists(font_path):
            font_path = None # Pillow will fall back to default font if not found

        pages_to_process = [] if args.no_translate else pages
        quality_flags = []  # Stage E: accumulated across all pages, written to quality_flags.json at the end

        if args.no_translate:
            log("No-translate mode: copying already translated images to output...")
            translated_dir = None
            if args.output.lower().endswith('.cbz'):
                translated_dir = os.path.abspath(os.path.join(os.path.dirname(args.output), "..", "translated"))
            for page_path in pages:
                basename = os.path.basename(page_path)
                copied = False
                if translated_dir:
                    possible_translated = os.path.join(translated_dir, basename)
                    if os.path.exists(possible_translated):
                        shutil.copy2(possible_translated, os.path.join(temp_out, basename))
                        copied = True
                if not copied:
                    # Fallback to original image if not translated yet
                    shutil.copy2(page_path, os.path.join(temp_out, basename))

        book_dir = os.path.abspath(os.path.join(os.path.dirname(args.output), ".."))
        init_cast_registry(book_dir)
        init_bubble_tone(book_dir)
        init_honorifics(book_dir)
        clean_run_done = load_clean_run_progress(book_dir, args.clean_run_id)
        if args.force_retranslate and args.clean_run_id:
            log(f"--clean-run-id {args.clean_run_id}: {len(clean_run_done)} page(s) already "
                f"completed in this sweep (from a prior interrupted attempt) will be skipped; "
                f"everything else is force-retranslated regardless of any existing file.")

        for idx, page_path in enumerate(pages_to_process):
            log(f"Page {idx+1}/{len(pages_to_process)}: {os.path.basename(page_path)}")
            
            basename = os.path.basename(page_path)
            # Resume logic: check if already translated
            translated_path = None
            if args.output.lower().endswith('.cbz'):
                translated_dir = os.path.abspath(os.path.join(os.path.dirname(args.output), "..", "translated"))
                cleaned_dir = os.path.abspath(os.path.join(os.path.dirname(args.output), "..", "cleaned"))
                possible_translated = os.path.join(translated_dir, basename)
                if os.path.exists(possible_translated):
                    translated_path = possible_translated

            if translated_path and args.force_retranslate:
                if args.clean_run_id and basename in clean_run_done:
                    log(f"Page {idx+1}: already force-retranslated in this --clean-run-id sweep. Skipping.")
                else:
                    log(f"Page {idx+1}: --force-retranslate — ігнорую наявний переклад, роблю з нуля.")
                    translated_path = None

            if translated_path:
                log(f"Page {idx+1} already translated. Skipping.")
                # Copy to temp_out
                shutil.copy2(translated_path, os.path.join(temp_out, basename))
                # Make sure it's in cleaned_dir
                possible_cleaned = os.path.join(cleaned_dir, basename)
                if not os.path.exists(possible_cleaned):
                    shutil.copy2(translated_path, possible_cleaned)
                continue

            if args.progress_file:
                try:
                    with open(args.progress_file, "w", encoding="utf-8") as pf:
                        json.dump({"current_page": idx + 1, "total_pages": len(pages)}, pf)
                except Exception:
                    pass
            from common.heartbeat import send_heartbeat
            send_heartbeat(args.slug or os.path.basename(book_dir), f"{idx + 1}/{len(pages)}")
            img = cv2.imread(page_path)
            if img is None:
                log(f"Warning: Failed to read page {page_path}")
                continue
                
            final_img, cleaned_img, page_flags, page_bubbles = process_page(
                img, basename, glossary, args.api_url, args.lang, detector, mocr, font_path
            )
            quality_flags.extend(page_flags)

            if not page_bubbles:
                log("No text bubbles found/recognized. Copying original page.")

            cv2.imwrite(os.path.join(temp_out, basename), final_img)

            if args.force_retranslate and args.clean_run_id:
                record_clean_run_page(book_dir, args.clean_run_id, basename)

            if args.output.lower().endswith('.cbz'):
                cleaned_dir = os.path.abspath(os.path.join(os.path.dirname(args.output), "..", "cleaned"))
                os.makedirs(cleaned_dir, exist_ok=True)
                cv2.imwrite(os.path.join(cleaned_dir, basename), cleaned_img)

                if page_bubbles:
                    translated_dir = os.path.abspath(os.path.join(os.path.dirname(args.output), "..", "translated"))
                    os.makedirs(translated_dir, exist_ok=True)
                    cv2.imwrite(os.path.join(translated_dir, basename), final_img)

                    # TASK-21: per-page bubble metadata for the manual-edit
                    # overlay UI (bbox + current text + quality flags).
                    book_dir = os.path.abspath(os.path.join(os.path.dirname(args.output), ".."))
                    page_stem = os.path.splitext(basename)[0]
                    write_bubbles_meta(book_dir, page_stem, page_bubbles)

                # TASK-23: page boundary — the natural point to check for
                # live edits against already-completed pages before moving on.
                apply_pending_manga_edits(book_dir, temp_in, glossary, args.api_url, args.lang, detector, mocr, font_path)

        # Stage E: write accumulated quality flags for this book, a future
        # input for a manual-review queue (not built in this pass).
        if quality_flags:
            book_dir = os.path.abspath(os.path.join(os.path.dirname(args.output), ".."))
            quality_flags_path = os.path.join(book_dir, "quality_flags.json")
            try:
                with open(quality_flags_path, "w", encoding="utf-8") as f:
                    json.dump(quality_flags, f, ensure_ascii=False, indent=2)
                log(f"Wrote {len(quality_flags)} quality flag(s) to {quality_flags_path}")
            except Exception as e:
                log(f"Warning: Failed to write quality_flags.json: {e}")

        # Downscale images if they exceed maximum dimensions to prevent blank page bugs
        if args.max_height > 0 or args.max_width > 0:
            log(f"Preprocessing translated images (fitting into max dimensions {args.max_width}x{args.max_height})...")
            for f in os.listdir(temp_out):
                fpath = os.path.join(temp_out, f)
                if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')) and os.path.isfile(fpath):
                    try:
                        with Image.open(fpath) as img_pil:
                            width, height = img_pil.size
                            need_resize = False
                            new_width = width
                            new_height = height
                            
                            # Check height boundary
                            if args.max_height > 0 and height > args.max_height:
                                need_resize = True
                                ratio = args.max_height / height
                                new_height = args.max_height
                                new_width = int(width * ratio)
                            
                            # Check width boundary on scaled dimensions
                            if args.max_width > 0 and new_width > args.max_width:
                                need_resize = True
                                ratio = args.max_width / new_width
                                new_width = args.max_width
                                new_height = int(new_height * ratio)
                                
                            if need_resize:
                                log(f"Downscaling {f} from {width}x{height} to {new_width}x{new_height}")
                                img_pil = img_pil.resize((new_width, new_height), Image.Resampling.LANCZOS)
                                img_pil.save(fpath)
                    except Exception as ex:
                        log(f"Warning: Failed to downscale image {f}: {ex}")

        # Support interstitial pages (docs/plans/support-system-plan.md,
        # Phase 1): dropped into temp_out with names that sort right after
        # the last page of a closing chapter ('<page>.png' < '<page>_zz_...'
        # because '.' < '_'), so CBZ/AZW3 readers show them at the chapter
        # boundary. Chapter = the 'c<NNN>' marker in scanlation filenames;
        # books without that marker simply never hit a boundary and get no
        # insertions (safe default). All the usual guards apply (config,
        # opt-out); tone heuristic uses the closing page's bubbles_meta
        # text when available, best-effort.
        if args.output.lower().endswith('.cbz'):
            try:
                from common.support_banner import (
                    load_support_config, user_opted_out, is_heavy_scene,
                    SupportInserter, render_interstitial_png,
                )
                support_cfg = load_support_config()
                if support_cfg and not user_opted_out():
                    slug = os.path.basename(book_dir)
                    ins = SupportInserter(slug, support_cfg)
                    meta_dir = os.path.join(book_dir, "bubbles_meta")
                    chapter_re = re.compile(r"\bc(\d+)\b")

                    def _page_text(fname):
                        meta = os.path.join(
                            meta_dir, os.path.splitext(fname)[0] + ".json")
                        try:
                            with open(meta, "r", encoding="utf-8") as mf:
                                blocks = json.load(mf)
                            return " ".join(b.get("translation", b.get("text", ""))
                                            for b in blocks if isinstance(b, dict))
                        except Exception:
                            return ""

                    files = sorted(f for f in os.listdir(temp_out)
                                   if f.lower().endswith((".png", ".jpg", ".jpeg")))
                    prev_ch, prev_file = None, None
                    for fname in files:
                        m = chapter_re.search(fname)
                        ch = m.group(1) if m else None
                        if (prev_ch is not None and ch is not None
                                and ch != prev_ch and ins.due()
                                and not is_heavy_scene(_page_text(prev_file))):
                            out_name = os.path.splitext(prev_file)[0] + "_zz_support.png"
                            render_interstitial_png(
                                support_cfg, os.path.join(temp_out, out_name))
                            ins.mark_inserted()
                            log(f"Support interstitial inserted after chapter "
                                f"c{prev_ch} ({out_name}).")
                        ins.advance(1)
                        prev_ch, prev_file = ch, fname
                    log(f"Support interstitials: {ins.inserted_count} inserted.")
            except Exception as ex:
                # The banner must never be able to break a finished book.
                log(f"Warning: support interstitial step skipped: {ex}")

        # Packaging processed pages
        if args.output.lower().endswith('.cbz'):
            log(f"Packaging pages into CBZ archive: {args.output}")
            shutil.make_archive(args.output[:-4], 'zip', temp_out)
            shutil.move(args.output[:-4] + '.zip', args.output)

            # Generate AZW3 using Mapaki
            mapaki_bin = shutil.which("mapaki")
            if not mapaki_bin:
                for possible_path in ["/root/go/bin/mapaki", os.path.expanduser("~/go/bin/mapaki"), "/usr/local/bin/mapaki"]:
                    if os.path.exists(possible_path):
                        mapaki_bin = possible_path
                        break

            if mapaki_bin and not args.no_ebook:
                azw3_output = args.output[:-4] + ".azw3"
                log(f"Generating AZW3 using Mapaki: {azw3_output}")

                title = os.path.splitext(os.path.basename(args.output))[0]
                clean_title = title.replace('_', ' ').replace('-', ' ').title()

                cmd = [
                    mapaki_bin,
                    "-i", temp_out,
                    "-o", azw3_output,
                    "--title", clean_title
                ]
                if args.left_to_right:
                    cmd.append("--left-to-right")

                try:
                    log(f"Running Mapaki: {' '.join(cmd)}")
                    subprocess.run(cmd, check=True)
                    log(f"AZW3 manga generated successfully at: {azw3_output}")
                except Exception as e:
                    log(f"Error running Mapaki: {e}")
            else:
                log("Mapaki executable not found. Skipping AZW3 generation.")
        else:
            log(f"Saving pages to directory: {args.output}")
            os.makedirs(args.output, exist_ok=True)
            for f in os.listdir(temp_out):
                shutil.copy(os.path.join(temp_out, f), os.path.join(args.output, f))
                
        if args.progress_file:
            try:
                with open(args.progress_file, "w", encoding="utf-8") as pf:
                    json.dump({"current_page": len(pages), "total_pages": len(pages)}, pf)
            except Exception:
                pass
        log("Manga translation completed successfully!")
        
        # Agent Editor stage for manga (if enabled)
        cfg_manga = {}
        try:
            cfg_manga = json.load(open(os.path.join(book_dir, "config.json"), encoding="utf-8"))
        except Exception:
            pass
        if cfg_manga.get("enable_agent_editor"):
            log("Triggering Agent Editor vision scan for manga...")
            send_heartbeat(slug, "старт", stage="агент-редактор")
            agent_script = os.path.join(repo_dir, "bin", "agent_editor.py")
            if os.path.exists(agent_script):
                try:
                    subprocess.run(["python3", agent_script, "--book", slug], check=False)
                except Exception as e:
                    log(f"Warning: Agent Editor scan encountered issues: {e}")

        # TASK-57: tell Appwrite this book's heartbeat tracking is over -
        # runs even when the loop above made zero send_heartbeat() calls
        # (a resumed run where every page was already done), which is the
        # exact scenario that used to leave heartbeat-watchdog alerting on
        # a 100%-finished book.
        from common.heartbeat import clear_heartbeat
        clear_heartbeat()

    finally:
        # Clean temporary directories
        shutil.rmtree(temp_in, ignore_errors=True)
        shutil.rmtree(temp_out, ignore_errors=True)

if __name__ == "__main__":
    # TASK-28 follow-up: full-book runs (and 194-page backfills) can run
    # long enough that Android kills this backgrounded process outright
    # when the screen locks without a wake-lock held - audio_stage.py
    # already does this for its long TTS runs, translate_manga.py never
    # did. Confirmed termux-wake-lock is reachable from inside this PRoot
    # Ubuntu container (proot bind-mounts expose it). Wrapping the whole
    # entrypoint rather than just the per-book loop, since --backfill-
    # bubbles-meta and --regenerate-page can also run long enough to matter.
    try:
        subprocess.run(["termux-wake-lock"], check=False)
    except Exception as e:
        log(f"Warning: termux-wake-lock failed: {e}")
    try:
        main()
    finally:
        try:
            subprocess.run(["termux-wake-unlock"], check=False)
        except Exception as e:
            log(f"Warning: termux-wake-unlock failed: {e}")
