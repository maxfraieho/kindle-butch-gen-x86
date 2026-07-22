#!/usr/bin/env python3
"""TASK-67 calibration runner: classify every bubble of a book's already-
processed pages and report the distribution WITHOUT touching the
pipeline. Runs inside the proot container (needs cv2). Additive only:
with --write, stores bubble_class/confidence back into bubbles_meta.

Usage (container):
    python3 bin/classify_bubbles.py --book frieren [--limit 30] [--write]
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from common.bubble_shape import classify_bubble_shape


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--limit", type=int, default=30, help="max pages")
    ap.add_argument("--write", action="store_true",
                    help="store results into bubbles_meta (additive fields)")
    args = ap.parse_args()

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    book_dir = os.path.join(repo, "books", args.book)
    cleaned_dir = os.path.join(book_dir, "cleaned")
    meta_files = sorted(glob.glob(os.path.join(book_dir, "bubbles_meta", "*.json")))
    if not meta_files:
        print("no bubbles_meta - book not processed yet")
        return 1

    dist = Counter()
    sigmas = []
    shown = 0
    for mf in meta_files[: args.limit]:
        stem = os.path.splitext(os.path.basename(mf))[0]
        page_img = None
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            cand = os.path.join(cleaned_dir, stem + ext)
            if os.path.exists(cand):
                page_img = cand
                break
        if page_img is None:
            continue
        gray = cv2.imread(page_img, cv2.IMREAD_GRAYSCALE)
        if gray is None:
            continue
        bubbles = json.load(open(mf, encoding="utf-8"))
        changed = False
        for b in bubbles:
            box = b.get("bbox")
            ref = b.get("bbox_ref_size") or [gray.shape[1], gray.shape[0]]
            if not box:
                continue
            sx = gray.shape[1] / ref[0]
            sy = gray.shape[0] / ref[1]
            scaled = [box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy]
            res = classify_bubble_shape(gray, scaled)
            dist[res["bubble_class"]] += 1
            if res["contour_closed"]:
                sigmas.append(res["sigma"])
            if shown < 25:
                text = (b.get("translated_text") or "")[:38]
                print(f"{stem[-12:]}/{b['id'][-4:]}: {res['bubble_class']:<13} "
                      f"σ={res['sigma']:.4f} spikes={res['spikes']} "
                      f"conf={res['confidence']} closed={res['contour_closed']} | {text!r}")
                shown += 1
            if args.write:
                b["bubble_class"] = res["bubble_class"]
                b["bubble_class_confidence"] = res["confidence"]
                b["bubble_class_sigma"] = res["sigma"]
                changed = True
        if args.write and changed:
            json.dump(bubbles, open(mf, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)

    total = sum(dist.values())
    print("\n=== distribution ===")
    for cls, n in dist.most_common():
        print(f"  {cls:<14} {n:>4}  ({n / total * 100:.0f}%)")
    uncertain = dist["uncertain"] + dist["sfx_candidate"]
    print(f"\nwould need vision-fallback/human: {uncertain}/{total} "
          f"({uncertain / total * 100:.0f}%) [research benchmark ~15%]")
    if sigmas:
        import statistics
        print(f"sigma over closed contours: median={statistics.median(sigmas):.4f} "
              f"min={min(sigmas):.4f} max={max(sigmas):.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
