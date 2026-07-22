"""Cast Registry NER pre-pass (TASK-54): auto-draft characters from the
book's opening slice using local Gemma 3 4B via llama-cli.

Reads: manga -> original_text from bubbles_meta of the first N pages;
text book -> first chunk of merged markdown. Writes auto_drafted entries
into books/<slug>/edits/characters.json, never touching existing entries
(esp. verified ones). Gender guesses come from the TEXT only and always
require human verification before any rule is injected (QA-gate).

TASK-76: run_llm() streams llama-cli's stdout (instead of blocking on
subprocess.run) so a live progress %/log can be written to
ner_scan_progress.json for the dashboard to poll.
"""
import argparse
import glob
import json
import os
import re
import select
import subprocess
import sys
import time

from natsort import natsorted

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.cast_registry import load_characters, save_characters, VALID_GENDERS
from common.heartbeat import send_heartbeat

MODEL_DEFAULT = os.path.expanduser("~/models/gemma3-4b/gemma-3-4b-it-Q4_K_M.gguf")
LLAMA_CLI = os.path.expanduser("~/llama.cpp/build/bin/llama-cli")

PROMPT = """Extract the recurring CHARACTERS from this book excerpt.
Return ONLY a JSON array, no other text. For each character:
{"name_source": ["primary name", "variants seen in text"],
 "name_target": "", "gender": "feminine|masculine|neutral",
 "confidence": 0.0-1.0, "mentions": <count>}
Rules: only actual characters (not places/objects); gender only when the
text makes it clear (pronouns/titles), otherwise "neutral"; include
ALL-CAPS variants seen in the text as separate entries in name_source.

EXCERPT:
"""


def save_progress(book_dir, stage, percent, log_tail=None, error=None, done=False):
    path = os.path.join(book_dir, "edits", "ner_scan_progress.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    data = {
        "stage": stage,
        "percent": percent,
        "done": done
    }
    if log_tail is not None:
        data["log_tail"] = log_tail
    else:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                    if "log_tail" in old_data:
                        data["log_tail"] = old_data["log_tail"]
        except Exception:
            pass
        if "log_tail" not in data:
            data["log_tail"] = []

    if error is not None:
        data["error"] = error

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def collect_text(book_dir, pages, page_start=None, page_end=None):
    # natsorted, NOT plain sorted() - TASK-80's page_start/page_end (and
    # the viewer's "run on this page" button) assume this list's order
    # matches preview_manga()'s source_pages list (kbg_web/app.py), which
    # is natsorted. Filenames here happen to be zero-padded on every book
    # tested so far (both sorts agree), but a book with e.g. "page1.png"
    # .. "page10.png" would silently diverge under plain sorted() -
    # "page1" would target a different physical page than the viewer's
    # page 1. Found during TASK-80 review, not exercised by any real
    # book yet, but the whole point of a page-range feature is targeting
    # the RIGHT page.
    meta = natsorted(glob.glob(os.path.join(book_dir, "bubbles_meta", "*.json")))
    page_pairs = []
    if meta:
        chunks = []
        if page_start is not None or page_end is not None:
            start_idx = (page_start - 1) if page_start is not None else 0
            start_idx = max(0, start_idx)
            end_idx = page_end if page_end is not None else len(meta)
            target_slice = meta[start_idx:end_idx]
        else:
            target_slice = meta[:pages]
        for p in target_slice:
            page_stem = os.path.splitext(os.path.basename(p))[0]
            page_bubbles = []
            try:
                for b in json.load(open(p, encoding="utf-8")):
                    t = (b.get("original_text") or "").strip()
                    if t:
                        chunks.append(t)
                        page_bubbles.append(t)
            except Exception:
                continue
            if page_bubbles:
                page_pairs.append((page_stem, "\n".join(page_bubbles)))
        return "\n".join(chunks), page_pairs

    # 1. Try to load config.json to find source_lang
    source_lang = None
    config_path = os.path.join(book_dir, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                source_lang = cfg.get("source_lang")
        except Exception:
            pass

    # 2. Try the merged source file in translated/
    if source_lang:
        source_cand = os.path.join(book_dir, "translated", f"merged_source_{source_lang}.md")
        if os.path.exists(source_cand):
            try:
                return open(source_cand, encoding="utf-8").read()[:15000], []
            except Exception:
                pass

    # 3. Try to read from un-translated PDF batch files if they exist
    batches_pattern = os.path.join(book_dir, "batches", "batch_*", "*", "*.md")
    batch_files = glob.glob(batches_pattern)
    if batch_files:
        source_batch_files = []
        for bf in batch_files:
            basename = os.path.basename(bf)
            # Exclude any translated batch files
            if "_translated_" not in basename:
                source_batch_files.append(bf)
        
        if source_batch_files:
            def get_batch_start(path):
                match = re.search(r"batch_(\d+)_(\d+)", path)
                return int(match.group(1)) if match else 0
            def get_batch_end(path):
                match = re.search(r"batch_(\d+)_(\d+)", path)
                return int(match.group(2)) if match else 0
            
            source_batch_files.sort(key=get_batch_start)
            
            # Filter by page_start / page_end
            filtered_batches = []
            for bf in source_batch_files:
                b_start = get_batch_start(bf)
                b_end = get_batch_end(bf)
                if page_start is not None and b_end < page_start:
                    continue
                if page_end is not None and b_start > page_end:
                    continue
                filtered_batches.append(bf)
            
            chunks = []
            total_len = 0
            for bf in filtered_batches:
                try:
                    content = open(bf, encoding="utf-8").read()
                    chunks.append(content)
                    total_len += len(content)
                    if total_len >= 15000:
                        break
                except Exception:
                    continue
            if chunks:
                return "\n\n".join(chunks)[:15000], []

    # 4. Fallback to any merged_source_*.md in translated/
    source_cands = glob.glob(os.path.join(book_dir, "translated", "merged_source_*.md"))
    for cand in source_cands:
        try:
            return open(cand, encoding="utf-8").read()[:15000], []
        except Exception:
            continue

    # 5. Last resort fallback: existing translated merged or other files
    for cand in glob.glob(os.path.join(book_dir, "translated", "merged_translated_*.md")) + \
                glob.glob(os.path.join(book_dir, "translated", "merged*_*.md")) + \
                glob.glob(os.path.join(book_dir, "translated", "*.md")):
        if "merged_source_" in os.path.basename(cand):
            continue
        try:
            return open(cand, encoding="utf-8").read()[:15000], []
        except Exception:
            continue
    return "", []


def run_llm(text, model, book_dir=None):
    cmd = [LLAMA_CLI, "-m", model, "-c", "8192", "-n", "1024",
           "--temp", "1.0", "--top-k", "64", "--top-p", "0.95",
           "--min-p", "0.01", "--repeat-penalty", "1.0",
           "-no-cnv", "-p", PROMPT + text[:12000]]
    
    if not book_dir:
        # Fallback to blocking subprocess if book_dir is not provided
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        return res.stdout

    stderr_path = os.path.join(book_dir, "edits", "ner_scan_llama_stderr.log")
    os.makedirs(os.path.dirname(stderr_path), exist_ok=True)
    
    start_time = time.time()
    raw_output = []
    lines = []
    current_line = ""
    last_update_time = time.time()
    chars_so_far = 0

    with open(stderr_path, "w", encoding="utf-8") as stderr_file:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            bufsize=1
        )

        while True:
            # Bound each read with select() against the OVERALL deadline -
            # a bare blocking proc.stdout.read(64) would ignore the 1800s
            # timeout entirely if the model stalls mid-generation (no new
            # bytes = the check below never runs again). select() lets us
            # re-check the deadline even while waiting for output.
            remaining = 1800 - (time.time() - start_time)
            if remaining <= 0:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise subprocess.TimeoutExpired(cmd, 1800)

            ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 2.0))
            if not ready:
                continue  # nothing yet - loop back to re-check the deadline

            chunk = proc.stdout.read(64)
            if not chunk:
                break

            raw_output.append(chunk)
            chars_so_far += len(chunk)

            # Update log lines
            for char in chunk:
                if char == '\n':
                    lines.append(current_line)
                    current_line = ""
                else:
                    current_line += char

            # Keep only the last 40 lines
            if len(lines) > 40:
                lines = lines[-40:]

            # Periodically write progress every 2 seconds
            now = time.time()
            if now - last_update_time >= 2.0:
                last_update_time = now
                percent = int(min(90, 5 + 85 * chars_so_far / 4096))
                log_tail = lines + ([current_line] if current_line else [])
                save_progress(book_dir, "аналіз тексту", percent, log_tail=log_tail)

        # Wait for the process to fully exit
        proc.wait(timeout=30)

    raw = "".join(raw_output) + current_line
    return raw


def parse_characters(raw, page_pairs=None):
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for i, ch in enumerate(data if isinstance(data, list) else []):
        names = [n for n in (ch.get("name_source") or []) if isinstance(n, str) and n.strip()]
        if not names:
            continue
        gender = ch.get("gender") if ch.get("gender") in VALID_GENDERS else "neutral"

        # Find first sample_page if page_pairs is provided
        sample_page = None
        if page_pairs:
            for name in names:
                for stem, text in page_pairs:
                    # Cyrillic boundary check using Python's standard word boundary re
                    if re.search(r"[a-zA-Zа-яА-ЯёЁіІїЇєЄґҐ]", name):
                        pattern = r'(?i)\b' + re.escape(name) + r'\b'
                        if re.search(pattern, text):
                            sample_page = stem
                            break
                    else:
                        if name.lower() in text.lower():
                            sample_page = stem
                            break
                if sample_page:
                    break

        char_entry = {
            "id": f"char_auto_{i:02d}_{re.sub(r'[^a-z0-9]', '', names[0].lower())[:12]}",
            "name_source": names,
            "name_target": ch.get("name_target") or "",
            "gender": gender,
            "grammar_rules": "",
            "speech_style": "",
            "is_pov_narrator": False,
            "status": "auto_drafted",
            "ner_confidence": ch.get("confidence"),
            "ner_mentions": ch.get("mentions"),
        }
        if sample_page:
            char_entry["sample_page"] = sample_page

        out.append(char_entry)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-dir", required=True)
    ap.add_argument("--pages", type=int, default=50)
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--page-start", type=int, default=None)
    ap.add_argument("--page-end", type=int, default=None)
    args = ap.parse_args()

    book_dir = args.book_dir
    try:
        save_progress(book_dir, "читання тексту", 5, log_tail=[])

        if not os.path.exists(args.model):
            err_msg = f"NER model missing: {args.model}"
            print(err_msg, file=sys.stderr)
            save_progress(book_dir, "помилка", 5, error=err_msg, done=True)
            sys.exit(2)

        text, page_pairs = collect_text(book_dir, args.pages, page_start=args.page_start, page_end=args.page_end)
        if not text.strip():
            err_msg = "No source text found to scan."
            print(err_msg, file=sys.stderr)
            save_progress(book_dir, "помилка", 5, error=err_msg, done=True)
            sys.exit(1)

        slug = os.path.basename(book_dir.rstrip("/"))
        send_heartbeat(slug, "аналізує текст", stage="сканування персонажів")
        raw_llm_out = run_llm(text, args.model, book_dir=book_dir)

        save_progress(book_dir, "обробка результатів", 92)
        drafted = parse_characters(raw_llm_out, page_pairs=page_pairs)

        if page_pairs:
            save_progress(book_dir, "пошук зображень", 96)

        existing = load_characters(book_dir)
        known = {n for ch in existing for n in (ch.get("name_source") or [])}
        added = [ch for ch in drafted
                 if not any(n in known for n in ch["name_source"])]
        save_characters(book_dir, existing + added)

        # Clear auto-resume state on normal completion
        try:
            repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            state_path = os.path.join(repo_dir, ".active_conversion.json")
            if os.path.exists(state_path):
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                if state.get("slug") == slug:
                    os.remove(state_path)
        except Exception:
            pass

        save_progress(book_dir, "завершено", 100, done=True)
        print(f"NER pre-pass: {len(drafted)} candidates, {len(added)} new "
              f"drafted (existing entries untouched).")

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"NER pre-pass failed: {e}\n{tb}", file=sys.stderr)
        save_progress(book_dir, "помилка", 99, error=str(e), done=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
