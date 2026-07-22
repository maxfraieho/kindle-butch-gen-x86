#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import json
import re
import hashlib
import argparse
import glob

from common.text_protect import PlaceholderManager
from common.book_paths import resolve_book_paths
from common.utils import get_hash, split_into_segments


def main():
    parser = argparse.ArgumentParser(description="Clean failed entries from translation cache.")
    parser.add_argument("--book", type=str, help="Book slug (e.g. vibe-programming)")
    parser.add_argument("--config", type=str, help="Path to config.json (optional)")
    args = parser.parse_args()

    if not args.book and not args.config:
        parser.error("At least one of --book or --config must be specified.")

    repo_dir = os.path.dirname(os.path.abspath(__file__))
    slug = args.book
    if not slug and args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                slug = cfg.get("slug")
        except Exception:
            pass
    if not slug:
        print("Could not determine book slug.")
        return

    paths = resolve_book_paths(repo_dir, slug, config_path=args.config)

    cache_path = paths["translate_cache"]
    if not os.path.exists(cache_path):
        print(f"Cache file not found at {cache_path}")
        return

    with open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)

    print(f"Loaded cache with {len(cache)} entries.")

    batches_dir = paths["batches_dir"]
    if not os.path.exists(batches_dir):
        print(f"Batches directory not found at {batches_dir}")
        return

    # Dynamically search for .md source files in books/<slug>/batches/batch_*/<title>/<title>.md
    search_pattern = os.path.join(batches_dir, "batch_*", "*", "*.md")
    md_files = []
    for filepath in glob.glob(search_pattern):
        # Check if the filename (without extension) matches the folder name
        parent_dir = os.path.basename(os.path.dirname(filepath))
        filename = os.path.splitext(os.path.basename(filepath))[0]
        if parent_dir == filename:
            md_files.append(filepath)

    if not md_files:
        print("No matching batch markdown files found.")
        return

    removed_count = 0

    for md_file in md_files:
        print(f"Processing batch file: {md_file}")
        with open(md_file, "r", encoding="utf-8") as f:
            source_text = f.read()

        pm = PlaceholderManager()
        protected_text = pm.protect(source_text)
        segments = split_into_segments(protected_text)

        for seg in segments:
            seg_hash = get_hash(seg)
            if seg_hash in cache:
                cached_val = cache[seg_hash]
                # If cached value is identical to the original segment (meaning translation failed)
                if cached_val == seg:
                    print(f"Found failed translation in cache for segment hash: {seg_hash}")
                    print(f"Original: {seg[:100]}...")
                    del cache[seg_hash]
                    removed_count += 1

    if removed_count > 0:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print(f"Cleaned cache successfully. Removed {removed_count} failed entries. New size: {len(cache)}")
    else:
        print("No failed entries found in cache.")

if __name__ == "__main__":
    main()
