#!/usr/bin/env python3
"""Agentic vision editor (TASK-65; spec doc titled "TASK-53").

Extension of the Cast Registry premium line: Gemma 3 4B (vision, via
llama-mtmd-cli) LOOKS at already-flagged problem pages (box_overlap /
overflow from the algorithmic QA pass) and PROPOSES fixes using the exact
same edit API a human uses - PUT /api/edit/manga-bbox (TASK-36 geometry/
font-size) and PUT /api/edit/manga-text. Every proposal lands in
edit_store as status=pending with source="gemma_agent" and goes through
the same human Approve/Discard gate as any manual edit.

Hard rules (direct lessons from the removed TASK-17 editor_model):
- NOTHING is ever applied without human approval. agent_auto_approve is
  read but deliberately NOT honored in v1 - no exceptions.
- Only algorithmically flagged cases are examined (never the whole book).
- v2: geometry fixes are computed deterministically; the vision model
  only veto-checks them. Text proposals were dropped entirely (approved
  human text edits still feed the translation memory via the approve
  hook in app.py - this script never writes memory itself).
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kbg_web import edit_store
from common.heartbeat import send_heartbeat, clear_heartbeat

MODEL_DEFAULT = os.path.expanduser("~/models/gemma3-4b/gemma-3-4b-it-Q4_K_M.gguf")
MMPROJ_DEFAULT = os.path.expanduser("~/models/gemma3-4b/mmproj-model-f16.gguf")
MTMD_CLI = os.path.expanduser("~/llama.cpp/build/bin/llama-mtmd-cli")

FLAG_REASONS = ("box_overlap", "overflow", "text_overflow")

# v2 (Q's live feedback on v1): a 4B vision model CANNOT invent good
# coordinates - it played "least invasive" and produced +-10px tweaks and
# outright no-ops. Roles are now flipped: GEOMETRY computes the fix
# deterministically (resolve overlaps to zero intersection, widen
# distorting tall-narrow boxes), and vision only VETOES a computed
# candidate if the target area visibly contains artwork/other text.
VERIFY_PROMPT = """You are a manga typesetting reviewer. Look at the attached translated manga page.

A text box will be MOVED/RESIZED as follows (coordinates in a {width}x{height} pixel space):
{facts}

Question: would the NEW box position cover important artwork, a character's face,
or OTHER text that is not part of this box's own text? Answer ONLY JSON:
{{"approve": true|false, "reason": "one short sentence"}}"""


def _intersection(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 >= ix2 or iy1 >= iy2:
        return None
    return (ix1, iy1, ix2, iy2)


def _overlaps_any(box, partners):
    return any(_intersection(box, p) for p in partners)


def resolve_overlap(box, partners, W, H, min_gap=8,
                    prefer=None, forbid=None, max_shift=None):
    """Minimal-displacement shift (or edge cut as fallback) that leaves
    ZERO intersection with every partner box. Returns a new [x1,y1,x2,y2]
    or None if no acceptable geometry exists.

    prefer/forbid/max_shift come from Q-authored DRAKON rules (TASK-66):
    preferred directions are tried first (then the rest by distance),
    forbidden ones are never tried, and any candidate shift longer than
    max_shift px is rejected."""
    x1, y1, x2, y2 = box
    for _ in range(4):  # a shift can create a new collision; few passes
        hit = next((p for p in partners if _intersection((x1, y1, x2, y2), p)), None)
        if hit is None:
            break
        moves = {
            "left":  (hit[0] - x2 - min_gap, 0),
            "right": (hit[2] - x1 + min_gap, 0),
            "up":    (0, hit[1] - y2 - min_gap),
            "down":  (0, hit[3] - y1 + min_gap),
        }
        for d in (forbid or []):
            moves.pop(d, None)
        ordered = sorted(moves.items(),
                         key=lambda kv: (0 if kv[0] in (prefer or []) else 1,
                                         abs(kv[1][0]) + abs(kv[1][1])))
        candidates = [v for _, v in ordered
                      if max_shift is None or abs(v[0]) + abs(v[1]) <= max_shift]
        placed = False
        for dx, dy in candidates:
            nx1, ny1, nx2, ny2 = x1 + dx, y1 + dy, x2 + dx, y2 + dy
            if nx1 >= 0 and ny1 >= 0 and nx2 <= W and ny2 <= H \
                    and not _overlaps_any((nx1, ny1, nx2, ny2), partners):
                x1, y1, x2, y2 = nx1, ny1, nx2, ny2
                placed = True
                break
        if not placed:
            # No legal shift - cut the overlapping side instead.
            inter = _intersection((x1, y1, x2, y2), hit)
            if (inter[2] - inter[0]) <= (inter[3] - inter[1]):
                if hit[0] > x1:
                    x2 = hit[0] - min_gap
                else:
                    x1 = hit[2] + min_gap
            else:
                if hit[1] > y1:
                    y2 = hit[1] - min_gap
                else:
                    y1 = hit[3] + min_gap
            if (x2 - x1) < 40 or (y2 - y1) < 30:
                return None
    if _overlaps_any((x1, y1, x2, y2), partners):
        return None
    return [int(x1), int(y1), int(x2), int(y2)]


def load_rules(book_dir, repo):
    """v0 rules interpreter (TASK-66): reads agent_rules.yaml written by
    the studio's drakon2rules converter (strict subset - parsed with a
    tiny indent parser, no yaml dependency on the Termux host). Honored
    verbs: skip, require_note, advise lines (vision prompt), and the
    directional set - prefer_move / forbid_move / max_shift_px - which
    steer resolve_overlap's candidate ordering directly."""
    path = os.path.join(book_dir, "agent_rules.yaml")
    if not os.path.exists(path):
        path = os.path.join(repo, "agent_rules.yaml")
    if not os.path.exists(path):
        return [], []
    rules, advise, cur, section = [], [], None, None
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        s = line.strip()
        if s.startswith("#") or not s:
            continue
        if s == "rules:":
            section = "rules"
        elif s == "advise:":
            section = "advise"
        elif section == "advise" and s.startswith("- "):
            advise.append(s[2:].strip().strip('"'))
        elif section == "rules":
            if s.startswith("- id:"):
                cur = {"id": s.split(":", 1)[1].strip(), "when": [], "then": []}
                rules.append(cur)
            elif cur is not None and s.startswith("- ") and ":" not in s:
                cur["when"].append(s[2:].strip())
            elif cur is not None and s.startswith("- "):
                body = s[2:]
                if any(body.startswith(op) for op in ()) or " == " in body or " != " in body \
                        or " < " in body or " > " in body or " <= " in body or " >= " in body:
                    cur["when"].append(body.strip())
                else:
                    verb, _, arg = body.partition(":")
                    cur["then"].append((verb.strip(), arg.strip().strip('"')))
    return rules, advise


def _rule_facts(case, bubble, width, height):
    box = case.get("box") or bubble.get("bbox") or [0, 0, 1, 1]
    w, h = box[2] - box[0], box[3] - box[1]
    cy = (box[1] + box[3]) / 2
    pos = "top" if cy < height / 3 else ("bottom" if cy > 2 * height / 3 else "middle")
    text = bubble.get("translated_text") or ""
    return {"reason": case.get("reason"), "aspect": (w / h) if h else 1.0,
            "text_len": len(text), "text_looks_sfx": is_nonsense_text(text),
            "iou": case.get("iou") or 0.0, "page_position": pos,
            "box_w": w, "box_h": h}


def _eval_cond(cond, facts):
    m = re.match(r"^(\w+)\s*(==|!=|<=|>=|<|>)\s*(.+)$", cond)
    if not m or m.group(1) not in facts:
        return False
    left, op, raw = facts[m.group(1)], m.group(2), m.group(3).strip()
    if raw in ("true", "false"):
        right = raw == "true"
    else:
        try:
            right = float(raw)
        except ValueError:
            right = raw
    try:
        if isinstance(right, float):
            left = float(left)
        return {"==": left == right, "!=": left != right, "<": left < right,
                "<=": left <= right, ">": left > right, ">=": left >= right}[op]
    except (TypeError, ValueError):
        return False


def apply_rules(rules, facts):
    """Returns (skip_reason|None, extra_notes, matched_ids, constraints).
    First-in-file precedence; skip is strongest and stops evaluation.
    constraints: prefer/forbid move directions, max_shift_px - consumed
    by resolve_overlap so Q's diagrams literally steer the geometry."""
    notes, matched = [], []
    cons = {"prefer": [], "forbid": [], "max_shift": None}
    for r in rules:
        if not all(_eval_cond(c, facts) for c in r["when"]):
            continue
        matched.append(r["id"])
        for verb, arg in r["then"]:
            if verb == "skip":
                return arg or r["id"], notes, matched, cons
            if verb == "require_note":
                notes.append(arg)
            elif verb == "veto_note":
                pass  # advise lines cover the vision prompt globally
            elif verb == "prefer_move" and arg in ("left", "right", "up", "down"):
                if arg not in cons["prefer"]:
                    cons["prefer"].append(arg)
            elif verb == "forbid_move" and arg in ("left", "right", "up", "down"):
                if arg not in cons["forbid"]:
                    cons["forbid"].append(arg)
            elif verb == "max_shift_px":
                try:
                    v = int(arg)
                    # first-in-file wins: keep the earliest (strictest-by-order)
                    if cons["max_shift"] is None:
                        cons["max_shift"] = v
                except ValueError:
                    log(f"[rules] {r['id']}: bad max_shift_px value {arg!r}")
            else:
                log(f"[rules] {r['id']}: verb {verb!r} not honored (font verbs apply once font proposals exist)")
    return None, notes, matched, cons


def is_nonsense_text(text):
    """OCR-garbage detector (Q's feedback on the real p173 case: a page-
    tall column reading 'му ікі ?а ащ и 2 от @ << пат) ее іб 2 ей').
    Counts tokens that look like real Ukrainian/Latin words (3+ letters,
    vowel present) vs noise tokens (symbols, digits, 1-2 char shards,
    vowelless clusters). Deterministic - no model call needed."""
    if not text:
        return False
    tokens = re.findall(r"\S+", text)
    if len(tokens) < 3:
        return False
    wordlike = 0
    for t in tokens:
        letters = re.sub(r"[^а-щьюяіїєґa-z]", "", t.lower())
        if len(letters) >= 3 and re.search(r"[аеиіоуюяєїa-z]", letters) \
                and re.search(r"[аеиіоуюяєї]|[aeiouy]", letters):
            wordlike += 1
    return wordlike / len(tokens) < 0.4


def reformat_horizontal(box, partners, W, H, min_gap=8):
    """Convert a page-tall vertical column into a horizontal box a
    left-to-right reader can actually read (target-language ergonomics:
    Ukrainian/European readers need horizontal lines; vertical stacking
    is a Japanese-typography artifact). Keeps the top edge, shrinks
    height hard, widens into whatever horizontal span is free. Returns
    a new box (guaranteed non-overlapping) or None."""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    if w >= 0.6 * h:
        return None  # already horizontal-ish
    new_h = max(90, int(h * 0.22))
    row_partners = [p for p in partners if p[3] > y1 and p[1] < y1 + new_h]
    left_limit = max([0] + [p[2] + min_gap for p in row_partners if p[2] <= x1])
    right_limit = min([W] + [p[0] - min_gap for p in row_partners if p[0] >= x2])
    target_w = min(right_limit - left_limit, max(int(2.2 * new_h), int(w * 3)))
    if target_w < int(w * 1.5):
        return None
    cx = (x1 + x2) // 2
    nx1 = max(left_limit, min(cx - target_w // 2, right_limit - target_w))
    nx2 = nx1 + target_w
    cand = [int(nx1), int(y1), int(nx2), int(y1 + new_h)]
    if _overlaps_any(cand, partners):
        cand = resolve_overlap(cand, partners, W, H, min_gap)
    return cand


def widen_if_distorting(box, text, partners, W, H, min_gap=8):
    """A box much taller than wide squeezes text into a distorted
    one-character-per-line column. Widen it horizontally into free space
    (never into a partner box or off-page). Returns new box or None."""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    if w >= 0.5 * h or len(text or "") < 8:
        return None
    target_w = min(int(1.3 * h), int(2.5 * w) + 60)
    grow = (target_w - w) / 2
    left_limit = max([0] + [p[2] + min_gap for p in partners if p[3] > y1 and p[1] < y2 and p[2] <= x1])
    right_limit = min([W] + [p[0] - min_gap for p in partners if p[3] > y1 and p[1] < y2 and p[0] >= x2])
    nx1 = max(left_limit, int(x1 - grow))
    nx2 = min(right_limit, int(x2 + grow))
    if (nx2 - nx1) < w * 1.3:
        return None  # not enough free space to make a real difference
    return [nx1, y1, nx2, y2]


def log(msg):
    print(f"[agent_editor] {msg}", flush=True)


def api_call(api_base, auth, method, path, payload):
    import requests
    url = api_base.rstrip("/") + path
    r = requests.request(method, url, json=payload, auth=auth, timeout=30)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})


def run_vision(model, mmproj, image_path, prompt, timeout=1200):
    # -t 4: deliberately NOT all cores. Full-load llama-mtmd-cli got the
    # entire Termux session killed by Android's background process killer
    # on the OnePlus 13 (known device behavior, independent of thermals -
    # happened WITH active cooling and 8GB free RAM). Slower per page but
    # survives.
    cmd = [MTMD_CLI, "-m", model, "--mmproj", mmproj, "--image", image_path,
           "-c", "8192", "-n", "512", "--temp", "0.2", "-t", "4", "-p", prompt]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return res.stdout


def parse_proposal(raw):
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--mmproj", default=MMPROJ_DEFAULT)
    ap.add_argument("--api", default="http://127.0.0.1:5000")
    ap.add_argument("--page-start", type=int, default=None)
    ap.add_argument("--page-end", type=int, default=None)
    args = ap.parse_args()

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    book_dir = os.path.join(repo, "books", args.book)

    cfg = {}
    try:
        cfg = json.load(open(os.path.join(book_dir, "config.json"), encoding="utf-8"))
    except Exception:
        pass
    if not cfg.get("enable_agent_editor"):
        log("enable_agent_editor is not true for this book - refusing to run (opt-in).")
        return 1
    if cfg.get("agent_auto_approve"):
        log("agent_auto_approve=true is IGNORED in v1 - every proposal still requires human approval.")

    for p in (args.model, args.mmproj, MTMD_CLI):
        if not os.path.exists(p):
            log(f"missing required file: {p}")
            return 1

    # RAM guard: the first live run OOM-killed the ENTIRE Termux session
    # (Android low-memory killer) because Gemma vision (~3.3GB) was loaded
    # on top of the resident llama-server translation model (~4.4GB).
    # Refuse to start rather than take the whole phone environment down -
    # and never silently kill the user's llama-server ourselves (it may be
    # mid-translation on another book).
    try:
        meminfo = {l.split(":")[0]: int(l.split()[1])
                   for l in open("/proc/meminfo") if ":" in l}
        avail_gb = meminfo.get("MemAvailable", 0) / 1024 / 1024
    except Exception:
        avail_gb = None
    if avail_gb is not None and avail_gb < 5.0:
        llama_up = subprocess.run(["pgrep", "-f", "llama-server"],
                                  capture_output=True).returncode == 0
        log(f"ABORT: only {avail_gb:.1f}GB RAM available; the vision model needs ~5GB headroom "
            f"or Android will OOM-kill all of Termux (happened on the first live run).")
        if llama_up:
            log("llama-server is holding the translation model - stop it first "
                "(Models Manager -> Stop Server), then re-run the agent scan.")
        return 1

    user = os.environ.get("KBG_WEB_USER", "admin")
    password = os.environ.get("KBG_WEB_PASSWORD", "")
    auth = (user, password)

    rules, advise = load_rules(book_dir, repo)
    if rules or advise:
        log(f"[rules] loaded {len(rules)} rule(s), {len(advise)} advise line(s) from agent_rules.yaml")

    flags_path = os.path.join(book_dir, "quality_flags.json")
    try:
        flags = json.load(open(flags_path, encoding="utf-8"))
    except Exception as e:
        log(f"cannot read quality_flags.json: {e}")
        return 1
    cases = [f for f in flags if f.get("reason") in FLAG_REASONS]
    
    if args.page_start is not None or args.page_end is not None:
        import glob
        from natsort import natsorted
        # natsorted, not plain sorted() - must match preview_manga()'s
        # source_pages ordering (kbg_web/app.py) and cast_ner_prepass.py's
        # own page_start/page_end indexing, or the viewer's "run on this
        # page" button could silently target the wrong physical page on
        # any book with non-zero-padded page filenames.
        meta = natsorted(glob.glob(os.path.join(book_dir, "bubbles_meta", "*.json")))
        page_stems = [os.path.splitext(os.path.basename(p))[0] for p in meta]
        
        def get_page_number(page_name, page_stems):
            stem = os.path.splitext(page_name)[0]
            if stem in page_stems:
                return page_stems.index(stem) + 1
            m = re.search(r'\d+', stem)
            if m:
                return int(m.group(0))
            return 1
            
        filtered = []
        for c in cases:
            pg = c.get("page", "")
            pg_num = get_page_number(pg, page_stems)
            if args.page_start is not None and pg_num < args.page_start:
                continue
            if args.page_end is not None and pg_num > args.page_end:
                continue
            filtered.append(c)
        cases = filtered

    log(f"{len(cases)} flagged case(s); limit {args.limit}")

    existing = {e["target_id"] for e in edit_store.list_edits(args.book)
                if e["status"] in ("pending", "approved")}

    proposed = skipped = 0
    for case_idx, case in enumerate(cases):
        if proposed >= args.limit:
            break
        send_heartbeat(args.book, f"{case_idx + 1}/{len(cases)}", stage="агент-редактор")
        page = case.get("page", "")
        bubble_id = case.get("bubble_id", "")
        target_id = f"{page}#{bubble_id}"
        if target_id in existing:
            log(f"skip {bubble_id}: edit already pending/approved")
            skipped += 1
            continue

        image_path = os.path.join(book_dir, "preview_cache", "translated", page)
        if not os.path.exists(image_path):
            log(f"skip {bubble_id}: rendered page not found ({page})")
            skipped += 1
            continue

        stem = os.path.splitext(page)[0]
        try:
            bubbles = json.load(open(os.path.join(book_dir, "bubbles_meta", f"{stem}.json"), encoding="utf-8"))
        except Exception:
            log(f"skip {bubble_id}: no bubbles_meta")
            skipped += 1
            continue
        bubble = next((b for b in bubbles if b["id"] == bubble_id), None)
        if not bubble:
            log(f"skip {bubble_id}: bubble not in meta")
            skipped += 1
            continue

        # Canonical bubble coordinate space (no PIL on the Termux host -
        # TASK-57 lesson; bubbles_meta already carries the reference size,
        # and using it keeps every number in the prompt consistent with
        # the bubble bboxes, which are stored in that same space).
        ref = bubble.get("bbox_ref_size")
        if not (isinstance(ref, list) and len(ref) == 2):
            log(f"skip {bubble_id}: no bbox_ref_size in meta")
            skipped += 1
            continue
        width, height = int(ref[0]), int(ref[1])
        cur_box = case.get("box") or bubble.get("bbox")
        partner_ids = case.get("overlapping_with") or []
        partners = [b["bbox"] for b in bubbles
                    if b["id"] in partner_ids and b.get("bbox")]

        # 0. Q-authored rules first (TASK-66 v0 interpreter): the
        # knowledge base can skip a case outright or attach notes.
        facts = _rule_facts(case, bubble, width, height)
        skip_reason, rule_notes, matched_ids, rule_cons = apply_rules(rules, facts)
        if matched_ids:
            log(f"[rules] matched for {bubble_id}: {', '.join(matched_ids)}")
        if skip_reason:
            log(f"skip {bubble_id}: rule says skip - {skip_reason}")
            skipped += 1
            continue

        # 1. GEOMETRY computes the actual fix (deterministic - the thing
        # a human would do: розвести рамки, розширити спотворену).
        other_boxes = [b["bbox"] for b in bubbles
                       if b["id"] != bubble_id and b.get("bbox")]
        new_box = None
        fix_kind = None
        note = None

        # Nonsense-text branch first (Q's feedback): a page-tall column
        # of OCR garbage isn't a "shift it sideways" problem - the human
        # needs to know the TEXT itself is meaningless (likely decorative
        # kanji / chapter title), and the box should become horizontal
        # for a left-to-right reader.
        if is_nonsense_text(bubble.get("translated_text")):
            note = ("⚠️ Текст виглядає як OCR-шум без змісту (ймовірно "
                    "декоративний напис/назва розділу в оригіналі). Рамку "
                    "запропоновано переформатувати горизонтально - впишіть "
                    "правильний текст вручну, звірившись з оригіналом.")
            horiz = reformat_horizontal(cur_box, other_boxes, width, height)
            if horiz:
                new_box, fix_kind = horiz, "horizontal-reformat"
            else:
                # No room to reformat - still surface the finding: the
                # note IS the value; box stays as-is so approve is a
                # no-op geometrically but the flag reaches the human.
                new_box, fix_kind = [int(v) for v in cur_box], "nonsense-flag-only"
        else:
            widened = widen_if_distorting(cur_box, bubble.get("translated_text"),
                                          other_boxes, width, height)
            if widened and not _overlaps_any(widened, partners):
                new_box, fix_kind = widened, "widen-distorted-box"
            elif partners:
                resolved = resolve_overlap(cur_box, partners, width, height,
                                           prefer=rule_cons["prefer"],
                                           forbid=rule_cons["forbid"],
                                           max_shift=rule_cons["max_shift"])
                if resolved and resolved != [int(v) for v in cur_box]:
                    new_box, fix_kind = resolved, "resolve-overlap"

        # Hard honesty gate: no computable real improvement -> NO proposal.
        # (v1 shipped +-10px tweaks and no-ops; better silence than noise.)
        # A nonsense-flag proposal is exempt: its value is the note itself.
        if new_box is None:
            log(f"skip {bubble_id}: no real geometric improvement computable "
                f"(box={cur_box}, partners={len(partners)})")
            skipped += 1
            continue
        if partners and _overlaps_any(new_box, partners) and fix_kind != "nonsense-flag-only":
            log(f"skip {bubble_id}: computed box still overlaps - refusing to propose")
            skipped += 1
            continue

        # 2. VISION only veto-checks the computed candidate (binary
        # judgment - within a 4B model's actual competence, unlike
        # coordinate generation).
        vfacts = {
            "fix_kind": fix_kind,
            "bubble_own_text": bubble.get("translated_text"),
            "current_box": [int(v) for v in cur_box],
            "proposed_new_box": new_box,
        }
        prompt = VERIFY_PROMPT.format(
            facts=json.dumps(vfacts, ensure_ascii=False, indent=1),
            width=width, height=height)
        if advise:
            prompt += "\nAdditional owner rules:\n" + "\n".join(f"- {a}" for a in advise)
        log(f"geometry fix ready for {bubble_id} ({fix_kind}: {cur_box} -> {new_box}); vision veto-check...")
        vetoed = False
        veto_reason = ""
        try:
            raw = run_vision(args.model, args.mmproj, image_path, prompt)
            verdict = parse_proposal(raw)
            if isinstance(verdict, dict) and verdict.get("approve") is False:
                vetoed = True
                veto_reason = str(verdict.get("reason", ""))
                log(f"vision VETO for {bubble_id}: {veto_reason!r}")
        except subprocess.TimeoutExpired:
            log(f"{bubble_id}: vision check timed out - submitting geometry fix anyway "
                f"(human gate is the final QA)")
        if vetoed:
            if note:
                # Nonsense case: the FINDING must reach the human even
                # when no safe geometry exists (first live run: both
                # p173 reformats vetoed for covering the character's
                # face - correct veto, but the flag itself vanished).
                new_box = [int(v) for v in cur_box]
                fix_kind = "nonsense-flag-only"
                note += (" Горизонтальне переформатування відхилено vision-перевіркою"
                         + (f" ({veto_reason})" if veto_reason else "")
                         + " - розташуйте рамку вручну.")
                log(f"{bubble_id}: falling back to flag-only proposal with the note")
            else:
                log(f"skip {bubble_id}: vetoed, no note to surface")
                skipped += 1
                continue

        if rule_notes:
            note = ((note + " ") if note else "") + " ".join(rule_notes)
        if matched_ids:
            note = ((note + " ") if note else "") + f"[правила: {', '.join(matched_ids)}]"
        body = {"bubble_id": bubble_id, "source": "gemma_agent",
                "bbox": new_box, "ref_size": [width, height]}
        if note:
            body["note"] = note
        endpoint = f"/api/edit/manga-bbox/{args.book}/{page}"
        status, resp = api_call(args.api, auth, "PUT", endpoint, body)
        if status == 200:
            proposed += 1
            existing.add(target_id)
            log(f"PROPOSED {fix_kind} for {bubble_id}: {cur_box} -> {new_box}")
        else:
            skipped += 1
            log(f"skip {bubble_id}: API {status}: {resp.get('message')}")

    log(f"done: {proposed} proposal(s) submitted as pending (source=gemma_agent), {skipped} skipped.")
    log("Nothing was applied - review in Pending Edits (Approve/Discard).")

    # TASK-57: same false-stall bug as translate_manga.py - a resumed scan
    # where every case was already pending/approved skips the loop above
    # entirely (zero send_heartbeat() calls), so nothing else would ever
    # tell Appwrite this run is over.
    clear_heartbeat()

    # Clear auto-resume state on normal completion - mirrors kbg_web/
    # app.py's handle_process_completion for the main pipeline. Only
    # clear if it still refers to THIS run (a newer conversion may have
    # started and overwritten it while this one was finishing).
    try:
        state_path = os.path.join(repo, ".active_conversion.json")
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("slug") == args.book:
                os.remove(state_path)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
