#!/usr/bin/env python3
"""Report (and, with --delete, remove) orphaned TTS chunk .wav files
(Grok-review W6, 2026-07-19).

Why these accumulate: kbg_web/app.py's edit_regenerate_audio() synthesizes
a NEW <new_hash>.wav whenever a chunk's text/stress is edited, but never
removes the OLD <chunk_hash>.wav - by design, since the edit is
non-destructive until approved (see edit_store.py's docstring: nothing
touches a generated artifact until approve_edit()). Over many edits this
leaves stale chunk audio on disk indefinitely.

Deliberately conservative: audio_stage.py's paragraph-chunking logic
(the only fully authoritative source of "which hashes are current") is
inline inside its main(), not a reusable function - safely extracting it
without risking a subtle segmentation-drift bug (which could misclassify
a STILL-NEEDED chunk as orphaned) was judged out of scope for a quick
fix. Instead this uses tts_cache_<voice>.json as a heuristic: any .wav
whose hash is NOT a key in that book's cache file is very likely
genuinely orphaned (a live book's cache is written every time
audio_stage.py runs and always contains every hash it just needed), plus
an age floor to avoid racing a synthesis that's mid-flight right now.

Default: dry-run, reports candidates only. Pass --delete to actually
remove them - review the report first.
"""
import argparse
import json
import os
import time


def find_orphans(book_dir, min_age_hours=24):
    audio_dir = os.path.join(book_dir, "audio")
    if not os.path.isdir(audio_dir):
        return []

    orphans = []
    now = time.time()
    min_age_s = min_age_hours * 3600

    for entry in os.listdir(audio_dir):
        if not entry.startswith("chunks_"):
            continue
        voice_slug = entry[len("chunks_"):]
        chunks_dir = os.path.join(audio_dir, entry)
        cache_path = os.path.join(book_dir, "cache", f"tts_cache_{voice_slug}.json")
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache_hashes = set(json.load(f).keys())
        except Exception:
            # No cache to compare against - too risky to guess, skip this voice entirely.
            continue

        for fname in os.listdir(chunks_dir):
            if not fname.endswith(".wav"):
                continue
            h = fname[:-4]
            if h in cache_hashes:
                continue
            fpath = os.path.join(chunks_dir, fname)
            try:
                age = now - os.path.getmtime(fpath)
            except OSError:
                continue
            if age < min_age_s:
                continue
            orphans.append({"path": fpath, "voice": voice_slug, "age_hours": round(age / 3600, 1)})
    return orphans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, help="Book slug")
    ap.add_argument("--min-age-hours", type=float, default=24,
                    help="Only consider .wav files at least this old (default 24h, avoids racing an in-flight synth)")
    ap.add_argument("--delete", action="store_true",
                    help="Actually delete the candidates (default: report only)")
    args = ap.parse_args()

    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    book_dir = os.path.join(repo_dir, "books", args.book)
    if not os.path.isdir(book_dir):
        print(f"Error: no such book directory: {book_dir}")
        return 1

    orphans = find_orphans(book_dir, args.min_age_hours)
    if not orphans:
        print(f"No orphan candidates found for '{args.book}'.")
        return 0

    total_bytes = 0
    for o in orphans:
        size = os.path.getsize(o["path"])
        total_bytes += size
        action = "DELETING" if args.delete else "candidate"
        print(f"[{action}] {o['path']}  ({o['age_hours']}h old, {size/1024:.1f} KB, voice={o['voice']})")
        if args.delete:
            try:
                os.remove(o["path"])
            except OSError as e:
                print(f"  failed to delete: {e}")

    print(f"\n{len(orphans)} candidate(s), {total_bytes/1024/1024:.1f} MB total.")
    if not args.delete:
        print("Dry-run only - re-run with --delete to actually remove these.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
