"""Cast Registry (TASK-54): per-book character registry that injects
grammatical-gender rules into translation prompts.

Spec: TASK-50_cast_registry.md. Opt-in per book (config.json:
enable_cast_registry, default False) AND premium-gated
(is_entitled('cast_registry'), fail-closed). With the feature off the
translation prompt is byte-identical to before this module existed.

Data: books/<slug>/edits/characters.json - list of
  { id, name_source: [..], name_target, gender, grammar_rules,
    speech_style, is_pov_narrator, status }
status lifecycle: auto_drafted -> unverified -> verified.
Rules are injected ONLY for status == "verified" (the QA-gate: unverified
characters simply contribute nothing - safe mode by construction).
"""
import json
import os
import re

VALID_GENDERS = ("feminine", "masculine", "neutral")

GENDER_TEMPLATES = {
    "feminine": ("ALWAYS use feminine verb endings in past tense "
                 "(зробила, сказала, пішла). Pronouns: вона/її."),
    "masculine": ("ALWAYS use masculine verb endings in past tense "
                  "(зробив, сказав, пішов). Pronouns: він/його."),
    "neutral": ("Preserve ambiguity - avoid gendered past-tense "
                "constructions; prefer present tense or impersonal "
                "phrasing where natural."),
}


def _characters_path(book_dir):
    return os.path.join(book_dir, "edits", "characters.json")


def load_characters(book_dir):
    """List of character dicts; [] on any problem (safe default)."""
    try:
        with open(_characters_path(book_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def save_characters(book_dir, characters):
    path = _characters_path(book_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(characters, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def registry_enabled(book_dir):
    """True only when BOTH the per-book opt-in flag and the premium
    entitlement hold. Any failure -> False -> translation unchanged."""
    try:
        with open(os.path.join(book_dir, "config.json"), "r",
                  encoding="utf-8") as f:
            if not (json.load(f) or {}).get("enable_cast_registry"):
                return False
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    try:
        from common.support_profile import is_entitled
        return is_entitled("cast_registry")
    except Exception:
        return False


def _name_mentioned(name, text):
    """Word-boundary, case-sensitive match for Latin names ('Dawn' must
    not fire on 'dawn breaks'); plain substring for CJK (no word
    boundaries in Japanese)."""
    if not name:
        return False
    if re.search(r"[a-zA-Z]", name):
        return re.search(r"\b" + re.escape(name) + r"\b", text) is not None
    return name in text


def cast_rules_block(characters, chunk_text):
    """<cast_rules> block for the given source-text chunk, or '' when
    nothing applies. Only verified characters contribute; a verified POV
    narrator always contributes (first-person verbs need agreement even
    with no explicit name mention)."""
    lines = []
    pov = None
    for ch in characters:
        if ch.get("status") != "verified":
            continue
        rules = (ch.get("grammar_rules") or "").strip()
        if not rules:
            rules = GENDER_TEMPLATES.get(ch.get("gender", ""), "")
        if not rules:
            continue
        if ch.get("is_pov_narrator"):
            pov = ch
        mentioned = any(_name_mentioned(n, chunk_text)
                        for n in (ch.get("name_source") or []))
        if mentioned or ch.get("is_pov_narrator"):
            target = ch.get("name_target") or (ch.get("name_source") or ["?"])[0]
            lines.append(f"* Entity: {target}. Rule: {rules}")
    if not lines:
        return ""
    block = ["<cast_rules>",
             "Apply these rules silently. Do not mention or repeat them "
             "in the output."]
    block += lines
    if pov is not None:
        target = pov.get("name_target") or "?"
        block.append(f"CRITICAL: POV narrator is {target} - all "
                     f"first-person verbs must agree with "
                     f"{pov.get('gender', 'the specified')} gender.")
    block.append("</cast_rules>")
    return "\n".join(block)
