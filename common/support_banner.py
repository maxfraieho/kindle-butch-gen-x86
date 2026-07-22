"""Support-banner insertion (Track A/B) - Phase 1 of the support system.

Spec: docs/plans/support-system-plan.md. Two STRICTLY separate channels:
Track A = verified fund's official general donate page (developer never
touches that money), Track B = supporting the tool's developer, labeled
as exactly that. Inserted at natural pauses (chapter boundaries) every
random 50-70 pages, visually distinct from the book's own content, never
right after an emotionally heavy scene, and fully disabled by the user's
`no_support_banner` opt-out flag.

Safe defaults everywhere: missing config file, `enabled: false`, or the
opt-out flag all mean "insert nothing" - the pipeline must behave exactly
as before this feature existed.
"""
import json
import os
import random

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(_REPO_ROOT, "support_config.json")

# Conservative tone heuristic: if the text just before the insertion point
# contains any of these, we simply skip this insertion opportunity (the
# counter keeps running; the next chapter boundary gets the banner
# instead). A false positive only delays a banner - deliberately cheap.
_HEAVY_SCENE_MARKERS = [
    # uk
    "смерт", "загину", "загибл", "помер", "похорон", "могил", "кров",
    "плаче", "плач", "сльоз", "прощанн", "траур", "вбит", "убит",
    # en (source text may be untranslated at some call sites)
    "death", "died", "dies", "funeral", "grave", "blood", "crying",
    "tears", "farewell", "mourning", "killed",
]


def load_support_config():
    """Return the support config dict, or None when insertion must not
    happen at all (no file / disabled / malformed - all safe no-ops)."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    if not cfg.get("track_a_url"):
        return None  # Track A is the point; never show a developer-only banner
    return cfg


def user_opted_out():
    """The opt-out is real and one-step: a single flag wins over everything.
    Two sources, either wins: the local global_settings.json flag, and the
    remote Appwrite profile flag set by the Telegram bot's
    /no_support_banner (TASK-49). The remote check has a hard timeout and
    fails toward 'not opted out' - the banner counts as enabled and the
    build is never blocked by the external service being down."""
    try:
        with open(os.path.join(_REPO_ROOT, "global_settings.json"),
                  "r", encoding="utf-8") as f:
            settings = json.load(f)
        if isinstance(settings, dict) and settings.get("no_support_banner"):
            return True
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    try:
        from common.support_profile import remote_banner_disabled
        return remote_banner_disabled()
    except Exception:
        return False


def is_heavy_scene(text_tail):
    """True if the ~last stretch of text before the insertion point looks
    emotionally heavy. Checks only the tail (the scene the reader just
    left), lowercased substring match - deliberately simple."""
    if not text_tail:
        return False
    tail = text_tail[-1500:].lower()
    return any(m in tail for m in _HEAVY_SCENE_MARKERS)


class SupportInserter:
    """Tracks 'pages since last insertion' and decides when to insert.

    Seeded by the book slug so a re-run of the same book places banners
    at the same spots (reproducible builds; also keeps the TASK-23/45
    resume logic honest - a resumed run makes identical decisions).
    """

    def __init__(self, slug, cfg):
        self.cfg = cfg
        self._rng = random.Random(f"support:{slug}")
        lo = int(cfg.get("interval_min_pages", 50))
        hi = int(cfg.get("interval_max_pages", 70))
        self._lo, self._hi = min(lo, hi), max(lo, hi)
        self._threshold = self._rng.randint(self._lo, self._hi)
        self._since_last = 0
        self.inserted_count = 0

    def advance(self, pages):
        self._since_last += int(pages)

    def due(self):
        return self._since_last >= self._threshold

    def mark_inserted(self):
        self.inserted_count += 1
        self._since_last = 0
        self._threshold = self._rng.randint(self._lo, self._hi)


def render_md_block(cfg):
    """Markdown/HTML block for the EPUB path. Raw HTML with inline styles
    survives Calibre's md->EPUB2 conversion and is unambiguously not the
    author's prose (bordered, own background, explicit service note)."""
    track_a = cfg["track_a_url"]
    track_a_name = cfg.get("track_a_name", "Повернись живим")
    track_b = cfg.get("track_b_url", "")
    lines = [
        '',
        '<div style="border:2px solid #888; border-radius:8px; padding:1em; '
        'margin:1.5em 0; background:#f4f4f4; font-family:sans-serif; font-size:0.9em;">',
        '<p><em>Коротка примітка від сервісу перекладу — не від автора книги.</em></p>',
        '<p>Поки в сюжеті пауза: цю книгу переклав безкоштовний інструмент, '
        'створений в Україні під час війни.</p>',
        f'<p>🇺🇦 Якщо хочете допомогти — <a href="{track_a}">{track_a_name}</a> '
        '(офіційний фонд, публічна звітність).</p>',
    ]
    if track_b:
        lines.append(
            f'<p>☕ Окремо — можна <a href="{track_b}">підтримати розробника '
            'цього інструменту</a> (це не воєнний збір).</p>')
    lines += [
        '<p style="font-size:0.8em; color:#666;">Вимкнути ці примітки: '
        'налаштування акаунта → «no support banner».</p>',
        '</div>',
        '',
    ]
    return "\n".join(lines)


def render_interstitial_png(cfg, out_path, width=1280, height=1920):
    """Interstitial manga page: own visual template, clearly not artwork.
    Uses PIL only (already a pipeline dependency)."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (width, height), "#1a1a2e")
    draw = ImageDraw.Draw(img)

    def _font(size):
        # The production container's Pillow is built WITHOUT FreeType
        # (importing PIL._imagingft fails), so truetype() can raise even
        # when the font file exists - verified live. Fall back to the
        # bitmap default font (sized when Pillow >= 10.1 supports it);
        # an ugly banner beats a crashed build every time.
        for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans.ttf"):
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    break
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    big, med, small = _font(56), _font(40), _font(30)
    track_a_name = cfg.get("track_a_name", "Повернись живим")

    y = height // 5
    for text, font, gap in [
        ("— пауза між розділами —", small, 90),
        ("Примітка від сервісу,", med, 60),
        ("не від автора манґи.", med, 120),
        ("Цю манґу переклав безкоштовний", med, 60),
        ("інструмент, створений в Україні.", med, 140),
        ("🇺🇦 Підтримати захисників:", big, 80),
        (track_a_name, big, 60),
        (cfg["track_a_url"], small, 70),
        ("(офіційний фонд, публічна звітність)", small, 160),
    ]:
        try:
            w = draw.textlength(text, font=font)
        except Exception:
            w = len(text) * 20
        draw.text(((width - w) / 2, y), text, fill="#eaeaea", font=font)
        y += gap

    if cfg.get("track_b_url"):
        for text, font, gap in [
            ("Окремо: підтримати розробника інструменту", small, 50),
            (cfg["track_b_url"], small, 60),
        ]:
            try:
                w = draw.textlength(text, font=font)
            except Exception:
                w = len(text) * 14
            draw.text(((width - w) / 2, y), text, fill="#9a9ab0", font=font)
            y += gap

    draw.rectangle([40, 40, width - 40, height - 40], outline="#4a4a6a", width=4)
    img.save(out_path, "PNG")
    return out_path
