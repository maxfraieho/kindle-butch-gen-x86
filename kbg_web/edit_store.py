import os
import sys
import json
import uuid
from datetime import datetime, timezone

repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if repo_dir not in sys.path:
    sys.path.insert(0, repo_dir)

from common.book_paths import resolve_book_paths


def _edits_path(slug):
    paths = resolve_book_paths(repo_dir, slug)
    return os.path.join(paths["book_dir"], "edits", "edits.json")


def _load_edits(slug):
    path = _edits_path(slug)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_edits(slug, edits):
    # Atomic write (tmp+os.replace) - same pattern already correct in
    # common/cast_registry.py's save_characters(). Without it, a crash or
    # concurrent write mid-json.dump (this file is read-mutate-write with
    # no locking) could leave edits.json truncated/corrupt, losing every
    # pending/approved edit for the book, not just the one being added.
    path = _edits_path(slug)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(edits, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def add_edit(slug, mode, target_id, field, original_value, edited_value,
             source="human", note=None):
    """Record a new non-destructive edit overlay. Does not touch any
    generated artifact (cache/markdown/audio) — that only happens on
    approve_edit().

    source: "human" (default) or "gemma_agent" (TASK-65 agent editor) —
    a mandatory audit trail so any future bug triage can always tell who
    authored a given edit. Agent edits go through the exact same
    pending→approve gate as human ones."""
    edits = _load_edits(slug)
    edit = {
        "id": f"e_{uuid.uuid4().hex[:12]}",
        "mode": mode,
        "target_id": target_id,
        "field": field,
        "original_value": original_value,
        "edited_value": edited_value,
        "status": "pending",
        "source": source,
        "note": note,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "applied_at": None,
    }
    edits.append(edit)
    _save_edits(slug, edits)
    return edit


def list_edits(slug, mode=None, status=None):
    edits = _load_edits(slug)
    if mode:
        edits = [e for e in edits if e["mode"] == mode]
    if status:
        edits = [e for e in edits if e["status"] == status]
    return edits


def get_edit(slug, edit_id):
    for e in _load_edits(slug):
        if e["id"] == edit_id:
            return e
    return None


def mark_status(slug, edit_id, status, applied_at=None):
    edits = _load_edits(slug)
    updated = None
    for e in edits:
        if e["id"] == edit_id:
            e["status"] = status
            if applied_at:
                e["applied_at"] = applied_at
            updated = e
            break
    if updated:
        _save_edits(slug, edits)
    return updated
