import os
import sys
import re
import json
import subprocess
import shutil
import signal
import uuid
from datetime import datetime, timedelta
from flask import (Flask, jsonify, request, render_template_string, render_template,
                   send_file, session, redirect, url_for)

# Resolve repository root
repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if repo_dir not in sys.path:
    sys.path.insert(0, repo_dir)

from common.book_paths import resolve_book_paths
from common.edit_patch import patch_batch_translation
from common.file_lock import file_lock
from kbg_web.status_helper import calculate_progress, get_pdf_page_count
from kbg_web import edit_store

TTS_ENGINES = {
    "supertonic3": {
        "languages": ["ar", "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hi", "hu", "id", "it", "ja", "ko", "lv", "lt", "pl", "pt", "ro", "ru", "sk", "sl", "es", "sv", "tr", "uk", "vi", "na"],
        "label": "Supertonic 3 (Flow Matching, 31 мова)"
    },
    "styletts2": {
        "languages": ["uk"],
        "label": "StyleTTS2 (спеціалізована для української)"
    },
}

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024  # 1 GB - 200MB blocked real manga volumes (high-res scan CBZ routinely exceeds 200MB)

# TASK-62: persistent secret key (survives Flask restarts) - a key that
# changes every restart would silently log everyone out constantly,
# which is exactly the "keeps asking again" complaint this feature
# exists to fix. Generated once, reused forever after.
_secret_key_path = os.path.join(os.path.dirname(__file__), ".flask_secret_key")
try:
    with open(_secret_key_path, "r") as f:
        app.secret_key = f.read().strip()
    if not app.secret_key:
        raise ValueError("empty")
except (FileNotFoundError, ValueError):
    app.secret_key = os.urandom(32).hex()
    try:
        with open(_secret_key_path, "w") as f:
            f.write(app.secret_key)
    except OSError:
        pass  # session just won't survive a restart this one time
app.permanent_session_lifetime = timedelta(days=365)

# Translation server (llama-server on :8081) lifecycle tracking.
# PID-file based instead of pkill-by-pattern to avoid matching an unrelated
# process, and a start lock to prevent two concurrent /api/models/start
# calls from each spawning their own llama-server (see TASK-18).
LLAMA_PID_FILE = os.path.expanduser("~/llama-server-8081.pid")
LLAMA_START_LOCK_FILE = os.path.expanduser("~/llama-server-8081.lock")
LLAMA_START_LOCK_STALE_SECONDS = 15

@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    return response

from flask_httpauth import HTTPBasicAuth
from werkzeug.security import generate_password_hash, check_password_hash

auth = HTTPBasicAuth()

import secrets

credentials_file = os.path.join(repo_dir, "web_credentials.json")
users_data = {}

# KBG_WEB_USER lets an existing install keep a custom username (e.g. a
# pre-rebrand 'vokov') across a code update instead of silently flipping
# to the new default on next restart - deploy.sh writes 'admin' for
# fresh installs; an already-configured device is left alone unless the
# user explicitly changes it via /api/change-password.
env_username = (os.environ.get("KBG_WEB_USER") or "admin").strip() or "admin"
env_password = os.environ.get("KBG_WEB_PASSWORD")

if env_password:
    users_data = {env_username: generate_password_hash(env_password)}
elif os.path.exists(credentials_file):
    try:
        with open(credentials_file, "r") as f:
            users_data = json.load(f)
    except Exception:
        pass

if not users_data:
    generated_password = secrets.token_urlsafe(16)
    print(f"\n==================================================")
    print(f"WARNING: No credentials found and KBG_WEB_PASSWORD not set.")
    print(f"Generated temporary password for user '{env_username}':")
    print(f"Password: {generated_password}")
    print(f"==================================================\n")
    users_data = {env_username: generate_password_hash(generated_password)}
    try:
        with open(credentials_file, "w") as f:
            json.dump(users_data, f)
    except Exception as e:
        print(f"Failed to save generated credentials to {credentials_file}: {e}")


@auth.verify_password
def verify_password(username, password):
    if username in users_data:
        return check_password_hash(users_data.get(username), password)
    return False

LOGIN_PAGE = """<!DOCTYPE html><html lang="uk"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vydra 🦦 — Вхід</title><link rel="icon" type="image/png" href="/static/vydra-sm.png">
<style>
body{font-family:system-ui,sans-serif;background:#0d0d14;color:#e6e6f0;display:flex;
  min-height:100vh;align-items:center;justify-content:center;margin:0;padding:1rem;}
.box{background:#14141f;border:1px solid #2a2a3d;border-radius:14px;padding:2rem;
  width:100%;max-width:320px;text-align:center;}
img{width:64px;height:64px;border-radius:50%;border:2px solid rgba(79,209,197,.4);margin-bottom:.5rem;}
h1{font-size:1.4rem;margin:0 0 1.2rem;
  background:linear-gradient(135deg,#4fd1c5,#2b7a78 55%,#f0b429);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;}
input{width:100%;box-sizing:border-box;padding:.7rem;margin-bottom:.7rem;border-radius:8px;
  border:1px solid #33334a;background:#1a1a28;color:#eee;font-size:1rem;}
button{width:100%;padding:.7rem;border:none;border-radius:8px;background:#8b5cf6;
  color:#fff;font-weight:600;font-size:1rem;cursor:pointer;}
.err{color:#f56565;font-size:.88rem;margin-bottom:.7rem;min-height:1.1em;}
</style></head><body><div class="box">
<img src="/static/vydra-sm.png" alt="Vydra"><h1>Vydra</h1>
<form method="post"><div class="err">{{ error or '' }}</div>
<input name="username" placeholder="Логін" autocomplete="username" required autofocus>
<input name="password" type="password" placeholder="Пароль" autocomplete="current-password" required>
<button type="submit">Увійти</button></form></div></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if verify_password(username, password):
            session.clear()
            session["user"] = username
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        return render_template_string(LOGIN_PAGE, error="Невірний логін або пароль"), 401
    return render_template_string(LOGIN_PAGE, error=None)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# TASK-62: session-cookie auth replaces bare HTTP Basic Auth as the
# primary mechanism - mobile Chrome in particular does NOT reliably
# persist Basic Auth credentials across page reloads (confirmed live:
# "пароль не зберігається, доводиться постійно вводити"), while a
# signed session cookie with a 365-day lifetime behaves like every other
# website's "stay logged in". Basic Auth is kept as a fallback so curl/
# scripts (and this session's own SSH-based testing) keep working
# unchanged - a valid Basic Auth header also upgrades the browser to a
# persistent session automatically, so it only has to happen once.
@app.before_request
def require_login():
    if request.endpoint in ("login", "static"):
        return
    if session.get("user") in users_data:
        return
    auth_header = request.authorization
    if auth_header and verify_password(auth_header.username, auth_header.password):
        session["user"] = auth_header.username
        session.permanent = True
        return
    if request.path.startswith("/api/"):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    return redirect(url_for("login", next=request.path))

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"status": "error", "message": "File is too large (maximum allowed size is 1GB)"}), 413

# Registry of active background processes: {slug: subprocess.Popen}
active_processes = {}
completed_copied = set()

# Auto-resume-on-restart: persists the CURRENTLY running conversion's exact
# invocation to disk, so if Termux/Flask itself dies mid-run (not just this
# one process - the whole environment going down, which active_processes
# alone can't survive since it's purely in-memory), the autostart script
# can detect an interrupted run and re-launch the identical command on the
# next boot. The pipeline's own per-page skip-if-already-done logic (used
# throughout this project already) means simply re-running the same
# command resumes correctly with no extra state tracking needed here.
ACTIVE_CONVERSION_STATE_PATH = os.path.join(repo_dir, ".active_conversion.json")


def _write_active_conversion_state(slug, cmd, cwd, log_path):
    try:
        with open(ACTIVE_CONVERSION_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"slug": slug, "cmd": cmd, "cwd": cwd, "log_path": log_path}, f, ensure_ascii=False)
    except Exception as e:
        print(f"[AutoResume] Warning: failed to write active-conversion state: {e}")


def _clear_active_conversion_state(slug=None):
    # Only clear if the on-disk state still refers to THIS slug - avoids a
    # race where book A's completion handler clears book B's still-running
    # state if a later conversion started before A's completion was noticed
    # (completion detection is lazy/poll-based here, see handle_process_completion).
    try:
        if not os.path.exists(ACTIVE_CONVERSION_STATE_PATH):
            return
        if slug is not None:
            with open(ACTIVE_CONVERSION_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("slug") != slug:
                return
        os.remove(ACTIVE_CONVERSION_STATE_PATH)
    except Exception as e:
        print(f"[AutoResume] Warning: failed to clear active-conversion state: {e}")

_CONVERSION_SCRIPTS = ("translate_epub.py", "translate_manga.py", "run_conversion_batches.py")


def _find_book_process_pids(slug):
    """PIDs of running conversion processes for THIS book. Bare `slug in
    cmdline` (the substring check this replaced) false-matched any slug
    that happened to be a substring of an unrelated running book's own
    path/args - e.g. slug "home" or "manga" would match every conversion
    process, since those words appear in every cmdline via the fixed
    "/kindle-butch-gen/translate_manga.py" script path alone. The two
    callers of this (status display AND force-kill) both need the exact
    match - a false positive on the kill path could SIGKILL an unrelated
    book's live conversion.

    Every real invocation embeds the slug either as an exact `--book`
    argument (translate_epub.py, run_conversion_batches.py) or as a
    `/books/<slug>/` path segment / `<slug>_translated_...` filename
    (translate_manga.py, which has no --book flag). Deliberately anchored
    to "/books/<slug>" specifically, NOT a bare `/<slug>/` anywhere in
    cmdline - every real invocation's own fixed path
    (.../files/home/kindle-butch-gen/...) contains a genuine "/home/"
    segment, which would itself false-match any slug literally named
    "home" under a naive any-`/`-boundary rule (caught by a real test,
    not guessed)."""
    slug_re = re.escape(slug)
    pattern = re.compile(
        r'\x00--book\x00' + slug_re + r'(?:\x00|$)'
        r'|/books/' + slug_re + r'(?:/|\x00|_|\.|$)'
    )
    pids = []
    try:
        for pid_str in os.listdir("/proc"):
            if pid_str.isdigit():
                try:
                    with open(f"/proc/{pid_str}/cmdline", "r") as f:
                        cmdline = f.read()
                    if (any(s in cmdline for s in _CONVERSION_SCRIPTS)
                            and pattern.search(cmdline)):
                        pids.append(int(pid_str))
                except Exception:
                    pass
    except Exception:
        pass
    return pids


def is_book_process_running(slug):
    return bool(_find_book_process_pids(slug))

def handle_process_completion(slug, proc):
    # Flask observed this process end (success OR failure) while it was
    # still alive to see it - clear the auto-resume state regardless of
    # exit code, so a genuinely-failed run isn't retried forever on every
    # Termux restart. Only a crash that killed Flask/Termux itself BEFORE
    # this function ever got to run leaves the state file behind, which is
    # exactly the correct signal for the autostart script to resume it.
    _clear_active_conversion_state(slug)
    if proc.poll() == 0:
        try:
            settings = load_global_settings()
            out_root = settings.get("output_root")
            if out_root:
                os.makedirs(out_root, exist_ok=True)
                paths = resolve_book_paths(repo_dir, slug)
                local_out_dir = paths["output_dir"]
                if os.path.exists(local_out_dir):
                    import shutil
                    for filename in os.listdir(local_out_dir):
                        src_path = os.path.join(local_out_dir, filename)
                        if os.path.isfile(src_path):
                            dest_path = os.path.join(out_root, filename)
                            shutil.copy2(src_path, dest_path)
                            print(f"[CompletionHelper] Copied {filename} to {dest_path}")
        except Exception as e:
            print(f"[CompletionHelper] Error copying completion files: {e}")

def validate_slug(slug):
    return bool(re.match(r"^[a-z0-9_-]+$", slug))

def detect_epub_lang(epub_path):
    try:
        import zipfile
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(epub_path, 'r') as z:
            container_content = z.read("META-INF/container.xml")
            container_root = ET.fromstring(container_content)
            root_file_el = container_root.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
            if root_file_el is None:
                root_file_el = container_root.find(".//rootfile")
            opf_rel_path = root_file_el.attrib["full-path"]
            opf_content = z.read(opf_rel_path)
            opf_root = ET.fromstring(opf_content)
            lang_el = opf_root.find('.//{http://purl.org/dc/elements/1.1/}language')
            if lang_el is None:
                lang_el = opf_root.find('.//language')
            if lang_el is not None and lang_el.text:
                return lang_el.text.split('-')[0].lower()
    except Exception:
        pass
    return None

@app.route("/")
def dashboard():
    return render_template("dashboard.html")

@app.route("/api/books")
def list_books():
    books_dir = os.path.join(repo_dir, "books")
    if not os.path.exists(books_dir):
        return jsonify([])

    # TASK-82 review fix: is_entitled() does a real network round-trip to
    # Appwrite (common/support_profile.py:_fetch_entitlements_remote) -
    # the original patch called it once PER BOOK inside the loop below,
    # so a library of N books meant N sequential HTTPS calls before this
    # (frequently-polled, dashboard-critical) endpoint could even return.
    # Call it once, same result reused for every book - matches this
    # project's own standing architecture rule that core UI must never
    # block on cloud reachability.
    from common.support_profile import is_entitled
    try:
        entitled = is_entitled("cast_registry")
    except Exception:
        entitled = False

    books = []
    for entry in os.listdir(books_dir):
        entry_path = os.path.join(books_dir, entry)
        if os.path.isdir(entry_path) and validate_slug(entry):
            config_path = os.path.join(entry_path, "config.json")
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                except Exception:
                    cfg = {}
            else:
                cfg = {}
                
            title = cfg.get("title", entry)
            authors = cfg.get("authors", "Unknown")
            target_lang = cfg.get("target_lang", "uk")
            
            # Determine if running
            is_running = False
            if entry in active_processes:
                proc = active_processes[entry]
                if proc.poll() is None:
                    is_running = True
                elif entry not in completed_copied:
                    handle_process_completion(entry, proc)
                    completed_copied.add(entry)
            if not is_running:
                is_running = is_book_process_running(entry)
                    
            # Calculate progress
            prog = calculate_progress(entry)
            if "error" in prog:
                prog = {"marker_percent": 0.0, "translation_percent": 0.0, "stress_percent": 0.0, "tts_percent": 0.0, "overall_percent": 0.0}
                
            # Scan output files
            output_dir = os.path.join(entry_path, "output")
            output_files = []
            if os.path.exists(output_dir):
                for f in os.listdir(output_dir):
                    if f.endswith((".epub", ".azw3", ".mp3", ".md", ".cbz", ".cbr", ".cb7", ".zip")):
                        output_files.append(f)

            books.append({
                "slug": entry,
                "title": title,
                "authors": authors,
                "target_lang": target_lang,
                "is_running": is_running,
                "progress": prog,
                "output_files": sorted(output_files),
                "is_manga": cfg.get("is_manga", False),
                "tts_voice": cfg.get("tts_voice", "ukrainian_tts"),
                "tts_voice_quality": cfg.get("tts_voice_quality", "medium"),
                "tts_speaker_id": int(cfg.get("tts_speaker_id", 2)),
                "tts_speed": float(cfg.get("tts_speed", 1.0)),
                "tts_noise_scale": float(cfg.get("tts_noise_scale", 0.667)),
                "tts_noise_w": float(cfg.get("tts_noise_w", 0.8)),
                "tts_engine": cfg.get("tts_engine", "supertonic3"),
                "generate_audiobook": bool(cfg.get("generate_audiobook", True)),
                "enable_cast_registry": bool(cfg.get("enable_cast_registry", False)) and entitled,
                "enable_agent_editor": bool(cfg.get("enable_agent_editor", False)) and entitled,
                "enable_bubble_tone": bool(cfg.get("enable_bubble_tone", False))
            })
            
    return jsonify(books)

@app.route("/api/add", methods=["POST"])
def add_book_api():
    data = request.get_json() or {}
    slug = data.get("slug", "").strip()
    pdf_path = data.get("pdf_path", "").strip()
    title = data.get("title", "").strip()
    authors = data.get("authors", "").strip()
    lang = data.get("lang", "").strip()
    source_lang = data.get("source_lang", "").strip() or "ru"
    is_manga = bool(data.get("is_manga", False))
    
    if not slug or not pdf_path or not title or not authors or not lang:
        return jsonify({"status": "error", "message": "Усі поля є обов'язковими"}), 400
        
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Недійсний формат ідентифікатора (slug)"}), 400
        
    if source_lang == "auto":
        return jsonify({"status": "error", "message": "Автовизначення вихідної мови не підтримується для локальних PDF шляхів. Будь ласка, оберіть мову вручну."}), 400
        
    if not os.path.exists(pdf_path):
        return jsonify({"status": "error", "message": "Вихідний PDF-файл не знайдено"}), 400
        
    try:
        # Create folder structure
        paths = resolve_book_paths(repo_dir, slug)
        if os.path.exists(paths["config_path"]):
            return jsonify({"status": "error", "message": "Книга з таким ідентифікатором вже існує. Використовуйте інший slug або видаліть існуючу книгу"}), 409
        book_dir = paths["book_dir"]
        
        os.makedirs(book_dir, exist_ok=True)
        os.makedirs(paths["cache_dir"], exist_ok=True)
        os.makedirs(paths["batches_dir"], exist_ok=True)
        os.makedirs(paths["translated_dir"], exist_ok=True)
        os.makedirs(paths["output_dir"], exist_ok=True)
        os.makedirs(paths["audio_dir"], exist_ok=True)
        
        # Copy source file
        ext = ""
        if os.path.isdir(pdf_path):
            if not is_manga:
                return jsonify({"status": "error", "message": "Вихідний шлях є директорією. Джерело-директорія підтримується лише для манґи."}), 400
            dest_dir = os.path.join(book_dir, "source")
            shutil.copytree(pdf_path, dest_dir, dirs_exist_ok=True)
            pages = len([f for f in os.listdir(dest_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
            page_ranges = []
        else:
            ext = os.path.splitext(pdf_path)[1].lower()
            dest_file = os.path.join(book_dir, f"{slug}{ext}")
            shutil.copy2(pdf_path, dest_file)
            
            if ext == ".pdf":
                pages = get_pdf_page_count(dest_file)
                page_ranges = [[1, pages]]
            else:
                pages = 0
                page_ranges = []
        
        # Write config.json
        config_data = {
            "slug": slug,
            "title": title,
            "authors": authors,
            "source_lang": source_lang,
            "target_lang": lang,
            "pdf_path": f"books/{slug}/{slug}.pdf" if ext == ".pdf" else "",
            "is_manga": is_manga,
            "generate_audiobook": not is_manga,
            "tts_voice": "ukrainian_tts" if lang == "uk" else "irina",
            "tts_voice_quality": "medium",
            "tts_speaker_id": 2 if lang == "uk" else 0,
            "tts_speed": 1.0,
            "tts_noise_scale": 0.667,
            "tts_noise_w": 0.8,
            "page_ranges": page_ranges
        }
        
        with open(paths["config_path"], "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
            
        return jsonify({"status": "success", "message": f"Book '{slug}' added successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/parse-metadata", methods=["POST"])
def parse_metadata_api():
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400
    file = request.files["file"]
    filename = file.filename
    ext = os.path.splitext(filename)[1].lower()
    
    title = ""
    authors = ""
    slug = ""
    detected_lang = "auto"
    
    if ext == ".epub":
        try:
            import zipfile
            import xml.etree.ElementTree as ET
            import tempfile
            import shutil
            
            temp_dir = tempfile.mkdtemp()
            temp_path = os.path.join(temp_dir, "temp.epub")
            file.save(temp_path)
            
            with zipfile.ZipFile(temp_path, 'r') as z:
                container_content = z.read("META-INF/container.xml")
                container_root = ET.fromstring(container_content)
                root_file_el = container_root.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
                if root_file_el is None:
                    root_file_el = container_root.find(".//rootfile")
                opf_rel_path = root_file_el.attrib["full-path"]
                
                opf_content = z.read(opf_rel_path)
                opf_root = ET.fromstring(opf_content)
                
                title_el = opf_root.find('.//{http://purl.org/dc/elements/1.1/}title')
                if title_el is None:
                    title_el = opf_root.find('.//title')
                if title_el is not None:
                    title = title_el.text or ""
                    
                creator_el = opf_root.find('.//{http://purl.org/dc/elements/1.1/}creator')
                if creator_el is None:
                    creator_el = opf_root.find('.//creator')
                if creator_el is not None:
                    authors = creator_el.text or ""
                    
                lang_el = opf_root.find('.//{http://purl.org/dc/elements/1.1/}language')
                if lang_el is None:
                    lang_el = opf_root.find('.//language')
                if lang_el is not None and lang_el.text:
                    detected_lang = lang_el.text.split('-')[0].lower()
                    
            shutil.rmtree(temp_dir)
            title = title.strip()
            authors = authors.strip()
            
            if title:
                slug_base = title.lower()
            else:
                slug_base = os.path.splitext(filename)[0].lower()
            slug = re.sub(r'[^a-z0-9_-]', '-', slug_base)
            slug = re.sub(r'-+', '-', slug).strip('-')
            
            if not slug:
                slug_base = os.path.splitext(filename)[0].lower()
                slug = re.sub(r'[^a-z0-9_-]', '-', slug_base)
                slug = re.sub(r'-+', '-', slug).strip('-')
            if not slug:
                slug = "uploaded-book"
        except Exception:
            pass
            
    elif ext in [".pdf", ".txt", ".md"]:
        slug_base = os.path.splitext(filename)[0].lower()
        slug = re.sub(r'[^a-z0-9_-]', '-', slug_base)
        slug = re.sub(r'-+', '-', slug).strip('-')
        if not slug:
            slug = "uploaded-book"
        title = os.path.splitext(filename)[0]
        
    return jsonify({
        "status": "success",
        "detected_title": title,
        "detected_authors": authors,
        "detected_slug": slug,
        "detected_lang": detected_lang
    })

@app.route("/api/upload", methods=["POST"])
def upload_file_api():
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "Файл не завантажено"}), 400
        
    uploaded_file = request.files["file"]
    slug = request.form.get("slug", "").strip()
    title = request.form.get("title", "").strip()
    authors = request.form.get("authors", "").strip()
    lang = request.form.get("lang", "").strip()
    source_lang = request.form.get("source_lang", "").strip() or lang
    
    if not slug or not title or not authors or not lang:
        return jsonify({"status": "error", "message": "Усі поля (slug, назва, автори, мова) є обов'язковими"}), 400
        
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Недійсний формат ідентифікатора (slug)"}), 400
        
    paths = resolve_book_paths(repo_dir, slug)
    if os.path.exists(paths["config_path"]):
        return jsonify({"status": "error", "message": "Книга з таким ідентифікатором вже існує. Використовуйте інший slug або видаліть існуючу книгу"}), 409
        
    filename = uploaded_file.filename
    ext = os.path.splitext(filename)[1].lower()
    if ext != ".epub" and source_lang == "auto":
        return jsonify({"status": "error", "message": "Автовизначення вихідної мови підтримується лише для EPUB файлів. Будь ласка, оберіть мову вручну."}), 400
        
    manga_extensions = [".cbz", ".cbr", ".cb7", ".zip", ".rar"]
    if ext not in [".pdf", ".epub", ".txt", ".md"] + manga_extensions:
        return jsonify({"status": "error", "message": f"Непідтримуване розширення файлу '{ext}'"}), 400
        
    try:
        is_manga = ext in manga_extensions or request.form.get("is_manga", "false").lower() == "true"
        
        book_dir = paths["book_dir"]
        os.makedirs(book_dir, exist_ok=True)
        os.makedirs(paths["cache_dir"], exist_ok=True)
        os.makedirs(paths["batches_dir"], exist_ok=True)
        os.makedirs(paths["translated_dir"], exist_ok=True)
        os.makedirs(paths["output_dir"], exist_ok=True)
        os.makedirs(paths["audio_dir"], exist_ok=True)
        
        pdf_path = ""
        page_ranges = []
        
        if is_manga:
            dest_manga = os.path.join(book_dir, f"{slug}{ext}")
            uploaded_file.save(dest_manga)
            if ext == ".pdf":
                pdf_path = f"books/{slug}/{slug}.pdf"
                pages = get_pdf_page_count(dest_manga)
                page_ranges = [[1, pages]]
        else:
            if ext == ".pdf":
                pdf_path = f"books/{slug}/{slug}.pdf"
                dest_pdf = os.path.join(book_dir, f"{slug}.pdf")
                uploaded_file.save(dest_pdf)
                pages = get_pdf_page_count(dest_pdf)
                page_ranges = [[1, pages]]
                
            elif ext == ".epub":
                temp_epub_path = os.path.join(book_dir, f"uploaded_temp.epub")
                uploaded_file.save(temp_epub_path)
                
                if source_lang == "auto":
                    source_lang = detect_epub_lang(temp_epub_path) or lang
                    
                if source_lang == lang:
                    target_md_name = f"merged_translated_{lang}.md"
                else:
                    target_md_name = f"merged_source_{source_lang}.md"
                    
                merged_md_path = os.path.join(paths["translated_dir"], target_md_name)
                cmd = [
                    sys.executable,
                    os.path.join(repo_dir, "bin", "extract_epub_text.py"),
                    "-i", temp_epub_path,
                    "-o", merged_md_path
                ]
                res = subprocess.run(cmd, capture_output=True, text=True)
                if os.path.exists(temp_epub_path):
                    os.remove(temp_epub_path)
                    
                if res.returncode != 0:
                    raise Exception(f"Failed to extract text from EPUB: {res.stderr}")
                    
            elif ext in [".txt", ".md"]:
                if source_lang == "auto":
                    source_lang = lang
                    
                if source_lang == lang:
                    target_md_name = f"merged_translated_{lang}.md"
                else:
                    target_md_name = f"merged_source_{source_lang}.md"
                    
                merged_md_path = os.path.join(paths["translated_dir"], target_md_name)
                uploaded_file.save(merged_md_path)
            
        config_data = {
            "slug": slug,
            "title": title,
            "authors": authors,
            "source_lang": source_lang,
            "target_lang": lang,
            "pdf_path": pdf_path,
            "is_manga": is_manga,
            "generate_audiobook": not is_manga,
            "tts_voice": "ukrainian_tts" if lang == "uk" else "irina",
            "tts_voice_quality": "medium",
            "tts_speaker_id": 2 if lang == "uk" else 0,
            "tts_speed": 1.0,
            "tts_noise_scale": 0.667,
            "tts_noise_w": 0.8,
            "page_ranges": page_ranges
        }
        
        with open(paths["config_path"], "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
            
        return jsonify({"status": "success", "message": f"Book '{slug}' uploaded and initialized successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/run/<slug>", methods=["POST"])
def run_conversion_api(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Недійсний формат ідентифікатора (slug)"}), 400
        
    paths = resolve_book_paths(repo_dir, slug)
    if not os.path.exists(paths["book_dir"]):
        return jsonify({"status": "error", "message": "Директорію книги не знайдено"}), 404
        
    data = request.get_json() or {}
    force = data.get("force", False)

    # Check if already running
    is_running = False
    if slug in active_processes:
        proc = active_processes[slug]
        if proc.poll() is None:
            is_running = True
    if not is_running and is_book_process_running(slug):
        is_running = True

    if is_running:
        if not force:
            return jsonify({"status": "error", "message": "Конвертація вже виконується"}), 400
        # force=true: kill existing process and continue
        try:
            if slug in active_processes:
                active_processes[slug].kill()
                del active_processes[slug]
            # Boundary-checked match (_find_book_process_pids) - a bare
            # substring here would SIGKILL an unrelated book's live
            # conversion if its cmdline happened to contain this slug.
            for pid in _find_book_process_pids(slug):
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
        except Exception:
            pass
        import time as _time
        _time.sleep(1)
        
    heavy = _heavy_state()
    if heavy["agent"]:
        return _busy_409("🤖 Зараз працює ШІ-агент пошуку проблем. Зупиніть його у вкладці «Агент» (або зачекайте кілька хвилин) — і запускайте переклад.")
    if heavy["ner"]:
        return _busy_409("🔎 Йде сканування персонажів (NER). Це кілька хвилин — потім запускайте переклад.")

    # Clear stale progress files
    epub_prog_path = os.path.join(paths["cache_dir"], "epub_progress.json")
    if os.path.exists(epub_prog_path):
        try:
            os.remove(epub_prog_path)
        except Exception:
            pass
    manga_prog_path = os.path.join(paths["book_dir"], "manga_progress.json")
    if os.path.exists(manga_prog_path):
        try:
            os.remove(manga_prog_path)
        except Exception:
            pass
            
    # data already parsed above (force handling)
    
    config_path = paths["config_path"]
    is_manga = False
    cfg = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                is_manga = cfg.get("is_manga", False)
        except Exception:
            pass
            
    if is_manga:
        # Determine source file or directory
        manga_input = ""
        if os.path.isdir(os.path.join(paths["book_dir"], "source")):
            manga_input = os.path.join(paths["book_dir"], "source")
        else:
            source_ext = ""
            for possible_ext in [".cbz", ".cbr", ".cb7", ".zip", ".rar", ".pdf", ".epub"]:
                if os.path.exists(os.path.join(paths["book_dir"], f"{slug}{possible_ext}")):
                    source_ext = possible_ext
                    break
            if source_ext:
                manga_input = os.path.join(paths["book_dir"], f"{slug}{source_ext}")
                
        if not manga_input:
            return jsonify({"status": "error", "message": "Manga source file or directory not found"}), 400
        manga_output_dir = os.path.join(paths["book_dir"], "output")
        os.makedirs(manga_output_dir, exist_ok=True)
        manga_output = os.path.join(manga_output_dir, f"{slug}_translated_{cfg.get('target_lang', 'uk')}.cbz")
        
        # Reset progress file
        progress_file = os.path.join(paths["book_dir"], "manga_progress.json")
        try:
            with open(progress_file, "w", encoding="utf-8") as pf:
                json.dump({"current_page": 0, "total_pages": 1}, pf)
        except Exception:
            pass
            
        # Run translate_manga.py inside PRoot Ubuntu container
        cmd = [
            "proot-distro", "login", "ubuntu", "--", 
            "python3", "-u", os.path.join(repo_dir, "translate_manga.py"),
            "--input", manga_input,
            "--output", manga_output,
            "--lang", cfg.get("source_lang", "en"),
            "--progress-file", progress_file
        ]
        # Include glossary if it exists
        glossary_path = os.path.join(paths["book_dir"], "glossary.json")
        if os.path.exists(glossary_path):
            cmd.extend(["--glossary", glossary_path])
        if data.get("no_translate"):
            cmd.append("--no-translate")
        if data.get("no_ebook"):
            cmd.append("--no-ebook")
        # "Clean Pages" (clean=true) for manga means: redo every page from
        # scratch, ignoring the resume-skip of already-translated pages.
        # --clean-run-id identifies THIS deliberate sweep across possibly-
        # many Termux-restart/auto-resume cycles, so a crash-and-resume can
        # keep --force-retranslate active without either re-doing pages
        # already redone this sweep or (the real incident, 2026-07-19)
        # silently falling back to unrelated stale files once the flag
        # was stripped to stop endless full-restarts.
        if data.get("clean"):
            cmd.append("--force-retranslate")
            cmd.extend(["--clean-run-id", uuid.uuid4().hex])

        # TASK-56: the resolution dropdown moved off the card into the
        # per-book ⚙️ settings modal (persisted to config.json), so the
        # per-run request body won't normally send this anymore - fall
        # back to the book's saved default instead of a hardcoded literal.
        manga_resolution = data.get("manga_resolution", cfg.get("manga_resolution", "1280x1920"))
        max_width, max_height = 1280, 1920
        if manga_resolution == "original":
            max_width, max_height = 0, 0
        elif "x" in manga_resolution:
            try:
                w_str, h_str = manga_resolution.split("x")
                max_width, max_height = int(w_str), int(h_str)
            except ValueError:
                pass
        
        cmd.extend(["--max-width", str(max_width), "--max-height", str(max_height)])
    else:
        # Check if it is an EPUB book (so we use direct EPUB translation)
        epub_source_file = os.path.join(paths["book_dir"], f"{slug}.epub")
        if os.path.exists(epub_source_file):
            cmd = [
                sys.executable, "translate_epub.py",
                "--input", epub_source_file,
                "--output", os.path.join(paths["output_dir"], f"{slug}_translated_{cfg.get('target_lang', 'uk')}.epub"),
                "--target-lang", cfg.get("target_lang", "uk"),
                "--book", slug
            ]
        else:
            cmd = [sys.executable, "run_conversion_batches.py", "--book", slug]
            if data.get("clean"):
                cmd.append("--clean")
            if data.get("no_translate"):
                cmd.append("--no-translate")
            if data.get("no_ebook"):
                cmd.append("--no-ebook")
            if data.get("no_audio"):
                cmd.append("--no-audio")
        
    log_path = paths["log_path"]
    
    try:
        # Prepare progress log with execution header
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n--- Starting Conversion Pipeline via Web GUI at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            f.write(f"Command: {' '.join(cmd)}\n\n")
            
        log_file = open(log_path, "a", encoding="utf-8")
        
        # Start background subprocess
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=repo_dir,
            text=True
        )
        
        active_processes[slug] = proc
        # Real incident, confirmed live 2026-07-19: Termux got killed mid a
        # deliberate --force-retranslate run (three times in one session).
        # Auto-resume replays the saved cmd VERBATIM, so the flag survived
        # into every resume too. An earlier fix here stripped the flag
        # entirely from the saved resume state - which stopped the
        # wasteful full-restarts, but had a worse side effect discovered
        # later the SAME day: once the flag was gone, resume fell back to
        # "does a translated file already exist", which for a page this
        # sweep hadn't reached yet meant silently reusing a 3-day-old
        # stale translation instead of the fresh one the user explicitly
        # asked for (100+ of 187 pages in that run turned out to be
        # stale). The real fix is --clean-run-id (see the flag's own
        # argparse help in translate_manga.py): the flag now stays in the
        # saved cmd unmodified, and per-page "already done THIS sweep"
        # tracking (keyed by that id) is what makes a resumed clean run
        # both non-destructive to its own progress AND still guaranteed
        # to reach a genuinely fresh translation for every page.
        _write_active_conversion_state(slug, cmd, repo_dir, log_path)
        return jsonify({"status": "success", "message": "Pipeline started in background"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/stop/<slug>", methods=["POST"])
def stop_conversion_api(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Недійсний формат ідентифікатора (slug)"}), 400
        
    if slug not in active_processes:
        return jsonify({"status": "error", "message": "Немає активного процесу для цієї книги"}), 400
        
    proc = active_processes[slug]
    if proc.poll() is not None:
        return jsonify({"status": "error", "message": "Процес вже завершено"}), 400
        
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

        # Explicit user stop should not trigger auto-resume on next restart.
        _clear_active_conversion_state(slug)

        paths = resolve_book_paths(repo_dir, slug)
        with open(paths["log_path"], "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- Процес конвертації зупинено користувачем ---\n")

        return jsonify({"status": "success", "message": "Процес успішно зупинено"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/delete/<slug>", methods=["POST"])
def delete_book_api(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Недійсний формат ідентифікатора (slug)"}), 400

    paths = resolve_book_paths(repo_dir, slug)
    if not os.path.exists(paths["book_dir"]):
        return jsonify({"status": "error", "message": "Директорію книги не знайдено"}), 404

    is_running = False
    if slug in active_processes:
        proc = active_processes[slug]
        if proc.poll() is None:
            is_running = True
    if not is_running and is_book_process_running(slug):
        is_running = True

    if is_running:
        return jsonify({"status": "error", "message": "Зупиніть конвертацію перед видаленням цієї книги"}), 400

    try:
        shutil.rmtree(paths["book_dir"])
        active_processes.pop(slug, None)
        completed_copied.discard(slug)
        return jsonify({"status": "success", "message": f"Книгу '{slug}' видалено"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/status/<slug>")
def status_api(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Недійсний формат ідентифікатора (slug)"}), 400
        
    paths = resolve_book_paths(repo_dir, slug)
    if not os.path.exists(paths["book_dir"]):
        return jsonify({"status": "error", "message": "Директорію книги не знайдено"}), 404
        
    # Check running status
    is_running = False
    if slug in active_processes:
        proc = active_processes[slug]
        if proc.poll() is None:
            is_running = True
        elif slug not in completed_copied:
            handle_process_completion(slug, proc)
            completed_copied.add(slug)
    if not is_running:
        is_running = is_book_process_running(slug)
            
    # Calculate progress percentages
    prog = calculate_progress(slug)
    if "error" in prog:
        return jsonify({"status": "error", "message": prog["error"]}), 400
        
    # Read the last 30 lines of the progress log
    log_lines = []
    log_path = paths["log_path"]
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                log_lines = lines[-30:]
        except Exception as e:
            log_lines = [f"Error reading log file: {e}"]
            
    return jsonify({
        "slug": slug,
        "is_running": is_running,
        "marker_percent": prog["marker_percent"],
        "translation_percent": prog["translation_percent"],
        "stress_percent": prog["stress_percent"],
        "tts_percent": prog["tts_percent"],
        "overall_percent": prog.get("overall_percent", 0.0),
        "logs": log_lines
    })

@app.route("/api/download/<slug>/<filename>")
def download_output_file(slug, filename):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug format"}), 400
        
    filename = os.path.basename(filename)
    paths = resolve_book_paths(repo_dir, slug)
    output_dir = os.path.abspath(paths["output_dir"])
    file_path = os.path.abspath(os.path.join(output_dir, filename))
    
    # Path traversal validation: ensure resolved path is strictly inside books/<slug>/output/
    if not file_path.startswith(output_dir + os.sep):
        return jsonify({"status": "error", "message": "Access denied (path traversal detected)"}), 403
        
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return jsonify({"status": "error", "message": "File not found"}), 404

    return send_file(file_path, as_attachment=True)

@app.route("/api/delete-file/<slug>/<filename>", methods=["POST"])
def delete_output_file(slug, filename):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug format"}), 400

    filename = os.path.basename(filename)
    paths = resolve_book_paths(repo_dir, slug)
    output_dir = os.path.abspath(paths["output_dir"])
    file_path = os.path.abspath(os.path.join(output_dir, filename))

    # Path traversal validation: ensure resolved path is strictly inside books/<slug>/output/
    if not file_path.startswith(output_dir + os.sep):
        return jsonify({"status": "error", "message": "Access denied (path traversal detected)"}), 403

    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return jsonify({"status": "error", "message": "File not found"}), 404

    try:
        os.remove(file_path)
        return jsonify({"status": "success", "message": f"'{filename}' deleted"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/tts-settings/<slug>", methods=["POST"])
def update_tts_settings(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug format"}), 400
        
    paths = resolve_book_paths(repo_dir, slug)
    config_path = paths["config_path"]
    if not os.path.exists(config_path):
        return jsonify({"status": "error", "message": "Book configuration not found"}), 404
        
    data = request.get_json() or {}
    try:
        # Load existing config
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            
        # Parse inputs
        tts_engine = str(data.get("tts_engine", config.get("tts_engine", "supertonic3"))).strip()
        tts_voice = str(data.get("tts_voice", config.get("tts_voice", "supertonic3"))).strip()
        tts_voice_quality = str(data.get("tts_voice_quality", config.get("tts_voice_quality", "medium"))).strip()
        
        # Validations
        if tts_engine not in TTS_ENGINES:
            return jsonify({"status": "error", "message": "Invalid tts_engine"}), 400
            
        target_lang = config.get("target_lang", "uk")
        if target_lang not in TTS_ENGINES[tts_engine]["languages"]:
            return jsonify({"status": "error", "message": f"TTS engine '{tts_engine}' does not support book language '{target_lang}'"}), 400
            
        tts_speaker_id = int(data.get("tts_speaker_id", config.get("tts_speaker_id", 2)))
        tts_speed = float(data.get("tts_speed", config.get("tts_speed", 1.0)))
        tts_noise_scale = float(data.get("tts_noise_scale", config.get("tts_noise_scale", 0.667)))
        tts_noise_w = float(data.get("tts_noise_w", config.get("tts_noise_w", 0.8)))
        
        if tts_engine == "supertonic3":
            if not (0 <= tts_speaker_id <= 9):
                return jsonify({"status": "error", "message": "tts_speaker_id must be between 0 and 9 for Supertonic 3"}), 400
                
        if not (0.5 <= tts_speed <= 2.0):
            return jsonify({"status": "error", "message": "tts_speed must be between 0.5 and 2.0"}), 400
        if not (0.1 <= tts_noise_scale <= 1.5):
            return jsonify({"status": "error", "message": "tts_noise_scale must be between 0.1 and 1.5"}), 400
        if not (0.1 <= tts_noise_w <= 1.5):
            return jsonify({"status": "error", "message": "tts_noise_w must be between 0.1 and 1.5"}), 400
            
        # Update config
        config["tts_engine"] = tts_engine
        config["tts_voice"] = tts_voice
        config["tts_voice_quality"] = tts_voice_quality
        config["tts_speaker_id"] = tts_speaker_id
        config["tts_speed"] = tts_speed
        config["tts_noise_scale"] = tts_noise_scale
        config["tts_noise_w"] = tts_noise_w
        
        # Write back
        _atomic_write_json(config_path, config, ensure_ascii=False, indent=2)

        return jsonify({"status": "success", "message": "TTS settings saved successfully"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/tts-preview/<slug>", methods=["POST"])
def tts_preview(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug format"}), 400
        
    data = request.get_json() or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"status": "error", "message": "Text is required for preview"}), 400
        
    paths = resolve_book_paths(repo_dir, slug)
    config_path = paths["config_path"]
    if not os.path.exists(config_path):
        return jsonify({"status": "error", "message": "Book configuration not found"}), 404
        
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            
        target_lang = config.get("target_lang", "uk")
        tts_engine = str(data.get("tts_engine", config.get("tts_engine", "supertonic3"))).strip()
        
        if tts_engine not in TTS_ENGINES:
            return jsonify({"status": "error", "message": f"Unsupported tts_engine '{tts_engine}'"}), 400
        if target_lang not in TTS_ENGINES[tts_engine]["languages"]:
            return jsonify({"status": "error", "message": f"TTS engine '{tts_engine}' does not support language '{target_lang}'"}), 400
            
        # Read parameters from request JSON data or config
        speaker_id = int(data.get("tts_speaker_id", config.get("tts_speaker_id", 2) if target_lang == "uk" else 0))
        speed = float(data.get("tts_speed", config.get("tts_speed", 1.0)))
        noise_scale = float(data.get("tts_noise_scale", config.get("tts_noise_scale", 0.667)))
        noise_w = float(data.get("tts_noise_w", config.get("tts_noise_w", 0.8)))
        voice_quality = str(data.get("tts_voice_quality", config.get("tts_voice_quality", "x_low"))).strip()
        
        # Prepare target path
        preview_wav_path = os.path.join(paths["cache_dir"], "preview.wav")
        if os.path.exists(preview_wav_path):
            try:
                os.remove(preview_wav_path)
            except Exception:
                pass
        os.makedirs(os.path.dirname(preview_wav_path), exist_ok=True)
        if target_lang == "uk":
            try:
                cmd_stress = [
                    "proot-distro", "login", "ubuntu", "--",
                    "python3", os.path.join(repo_dir, "bin", "stressify_batch.py"),
                    "--inline", text
                ]
                res_stress = subprocess.run(cmd_stress, capture_output=True, text=True, timeout=15)
                if res_stress.returncode == 0:
                    stressed_text = res_stress.stdout.strip()
                    if stressed_text:
                        text = stressed_text
                else:
                    print(f"Warning: inline stressifier returned code {res_stress.returncode}, stderr: {res_stress.stderr}", file=sys.stderr)
            except Exception as e:
                print(f"Warning: inline stressifier failed: {e}", file=sys.stderr)

        if tts_engine in ["supertonic3", "styletts2"]:
            # Prepare payload for tts_helper.py
            payload = {
                "tts_engine": tts_engine,
                "output_dir": paths["cache_dir"],
                "chunks": [{"hash": "preview", "text": text}],
                "speaker_id": speaker_id,
                "speed": speed,
                "lang": target_lang
            }
            if tts_engine == "styletts2":
                payload["voice_quality"] = voice_quality
                payload["noise_scale"] = noise_scale
                payload["noise_w"] = noise_w
            helper_path = os.path.join(repo_dir, "bin", "tts_helper.py")
            cmd = [
                sys.executable, helper_path
            ]
            res = subprocess.run(cmd, input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True)
            if res.returncode != 0:
                return jsonify({"status": "error", "message": f"{tts_engine} preview failed: {res.stderr}"}), 500
        else:
            return jsonify({"status": "error", "message": f"TTS preview is not supported for engine: {tts_engine}"}), 400
                
        if not os.path.exists(preview_wav_path):
            return jsonify({"status": "error", "message": "Failed to generate preview WAV file"}), 500
            
        return send_file(preview_wav_path, mimetype="audio/wav")
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def load_global_settings():
    path = os.path.join(repo_dir, "global_settings.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"output_root": "/storage/emulated/0/Documents/kindle-butch-gen/library"}

def _atomic_write_json(path, data, **dump_kwargs):
    """write(tmp) + fsync + os.replace() - a plain open(path, "w") first
    truncates the file, so a Termux kill mid-write (common on Android,
    low memory) leaves it empty/corrupt rather than just stale. TASK-74
    (code review): this exact pattern already existed ad-hoc in
    common/device_identity.py and change_password_api - centralized here
    instead of copy-pasting a fourth time."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, **dump_kwargs)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def save_global_settings(settings):
    path = os.path.join(repo_dir, "global_settings.json")
    try:
        _atomic_write_json(path, settings, indent=2)
    except Exception as e:
        print(f"Failed to save global settings: {e}")

@app.route("/api/browse-fs")
def browse_fs():
    """TASK-52 hardening: the 'Failed to load directories' report could not
    be reproduced (both roots list fine when storage permission is granted
    and Flask is up), so instead of a guess-fix this endpoint now (a) can
    never return a non-JSON 500 - every OSError becomes a JSON error with
    the REAL message, (b) falls back from the sdcard root to the Termux
    home when Android storage isn't accessible (e.g. Flask autostarted
    via Termux:Boot before storage mounted, or termux-setup-storage never
    run), with a hint the UI shows verbatim."""
    termux_home = os.environ.get("TERMUX_HOME", os.path.expanduser("~"))
    ALLOWED_ROOTS = ["/storage/emulated/0", termux_home]
    requested = request.args.get("path")
    path = os.path.abspath(requested or "/storage/emulated/0")
    if not any(path.startswith(root) for root in ALLOWED_ROOTS):
        return jsonify({"error": "Доступ за межами дозволених каталогів заборонено"}), 403

    hint = None
    if requested is None and not os.path.isdir(path):
        # Default root unavailable - fall back instead of failing.
        path = termux_home
        hint = ("Сховище Android недоступне (виконайте termux-setup-storage "
                "або перевідкрийте Termux) — показано домашню теку Termux.")
    if not os.path.isdir(path):
        return jsonify({"error": f"Каталог не існує: {path}"}), 400
    try:
        entries = []
        for item in sorted(os.listdir(path)):
            full = os.path.join(path, item)
            if os.path.isdir(full) and not item.startswith('.'):
                entries.append({"name": item, "path": full})
        parent = os.path.dirname(path) if path not in ALLOWED_ROOTS else None
        return jsonify({"current": path, "parent": parent, "dirs": entries,
                        "hint": hint})
    except OSError as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 403

@app.route("/api/settings/output-root", methods=["POST"])
def set_output_root():
    data = request.get_json() or {}
    new_root = data.get("path", "").strip()
    if not new_root or not os.path.isabs(new_root):
        return jsonify({"status": "error", "message": "Invalid path"}), 400
    try:
        os.makedirs(new_root, exist_ok=True)  # verification that we can write
    except Exception as e:
        return jsonify({"status": "error", "message": f"Cannot write to directory: {e}"}), 403
    settings = load_global_settings()
    settings["output_root"] = new_root
    save_global_settings(settings)
    return jsonify({"status": "success", "output_root": new_root})

@app.route("/api/settings")
def get_settings():
    return jsonify(load_global_settings())

@app.route("/api/characters/<slug>", methods=["GET"])
def get_characters_api(slug):
    """Cast Registry (TASK-54): character list + feature state."""
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    book_dir = os.path.join(repo_dir, "books", slug)
    from common.cast_registry import load_characters, GENDER_TEMPLATES
    try:
        with open(os.path.join(book_dir, "config.json"), "r", encoding="utf-8") as f:
            enabled_flag = bool((json.load(f) or {}).get("enable_cast_registry"))
    except Exception:
        enabled_flag = False
    try:
        from common.support_profile import is_entitled
        entitled = is_entitled("cast_registry")
    except Exception:
        entitled = False
    return jsonify({
        "characters": load_characters(book_dir),
        "enabled": enabled_flag,
        "entitled": entitled,
        "gender_templates": GENDER_TEMPLATES,
    })

@app.route("/api/characters/<slug>", methods=["PUT"])
def put_characters_api(slug):
    """Save the character list (verification, gender edits, POV flags).
    Editing requires the premium entitlement - the registry is a paid
    feature; without it the endpoint refuses rather than silently
    accepting rules that the pipeline would then ignore."""
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    try:
        from common.support_profile import is_entitled
        if not is_entitled("cast_registry"):
            return jsonify({"status": "error",
                            "message": "Cast Registry — розширена можливість. Активуйте через @GetVydraBot (/premium)."}), 403
    except Exception:
        return jsonify({"status": "error", "message": "Entitlement check unavailable"}), 403
    data = request.get_json() or {}
    chars = data.get("characters")
    if not isinstance(chars, list):
        return jsonify({"status": "error", "message": "characters must be a list"}), 400
    from common.cast_registry import save_characters, VALID_GENDERS
    for ch in chars:
        if ch.get("gender") and ch["gender"] not in VALID_GENDERS:
            return jsonify({"status": "error",
                            "message": f"invalid gender: {ch['gender']}"}), 400
    book_dir = os.path.join(repo_dir, "books", slug)
    save_characters(book_dir, chars)
    return jsonify({"status": "success", "count": len(chars)})

@app.route("/api/characters/<slug>/settings", methods=["POST"])
def characters_settings_api(slug):
    """Per-book enable_cast_registry toggle (premium-gated on enable)."""
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    data = request.get_json() or {}
    enable = bool(data.get("enable_cast_registry"))
    if enable:
        try:
            from common.support_profile import is_entitled
            if not is_entitled("cast_registry"):
                return jsonify({"status": "error",
                                "message": "Розширена можливість: активуйте через @GetVydraBot (/premium)."}), 403
        except Exception:
            return jsonify({"status": "error", "message": "Entitlement check unavailable"}), 403
    cfg_path = os.path.join(repo_dir, "books", slug, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:
        return jsonify({"status": "error", "message": "Book config not found"}), 404
    cfg["enable_cast_registry"] = enable
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return jsonify({"status": "success", "enable_cast_registry": enable})

@app.route("/api/manga/<slug>/bubble-tone", methods=["GET"])
def get_bubble_tone_api(slug):
    """TASK-67: per-book enable_bubble_tone read. NOT premium-gated - the
    geometric bubble classification itself always runs for every user
    (free or paid) and lands in bubbles_meta; this flag only decides
    whether those [КРИК]/[ДУМКА]/[НАРАЦІЯ] tags feed the translation
    prompt, so it belongs in both tiers, not behind an entitlement."""
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    cfg_path = os.path.join(repo_dir, "books", slug, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:
        return jsonify({"status": "error", "message": "Book config not found"}), 404
    return jsonify({"enable_bubble_tone": bool(cfg.get("enable_bubble_tone"))})

@app.route("/api/manga/<slug>/bubble-tone", methods=["POST"])
def set_bubble_tone_api(slug):
    """TASK-67: per-book enable_bubble_tone toggle - see GET above for why
    this is deliberately not entitlement-gated."""
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    data = request.get_json() or {}
    enable = bool(data.get("enable_bubble_tone"))
    cfg_path = os.path.join(repo_dir, "books", slug, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:
        return jsonify({"status": "error", "message": "Book config not found"}), 404
    cfg["enable_bubble_tone"] = enable
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return jsonify({"status": "success", "enable_bubble_tone": enable})

@app.route("/api/book-settings/<slug>", methods=["GET"])
def get_book_settings_api(slug):
    """TASK-56: consolidated per-book settings for the new ⚙️ modal -
    reads straight from config.json, no separate storage. Returns the
    live entitlement state too so the frontend can show a locked/
    unlocked indicator without a second round-trip."""
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    cfg_path = os.path.join(repo_dir, "books", slug, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:
        return jsonify({"status": "error", "message": "Book config not found"}), 404
    try:
        from common.support_profile import is_entitled
        entitled = is_entitled("cast_registry")
    except Exception:
        entitled = False
    return jsonify({
        "is_manga": bool(cfg.get("is_manga")),
        "enable_agent_editor": bool(cfg.get("enable_agent_editor")),
        "generate_audiobook": bool(cfg.get("generate_audiobook")),
        "enable_asr_verify": bool(cfg.get("enable_asr_verify")),
        "keep_honorifics": bool(cfg.get("keep_honorifics")),
        "manga_resolution": cfg.get("manga_resolution", "1280x1920"),
        "enable_mqm_review": bool(cfg.get("enable_mqm_review")),
        "entitled": entitled,
    })

@app.route("/api/book-settings/<slug>", methods=["POST"])
def set_book_settings_api(slug):
    """TASK-56: write-back for the per-book settings modal. Only
    enable_agent_editor is entitlement-gated (same "cast_registry"
    entitlement the scan endpoint already checks - see TASK-56's
    reconciliation note removing the unused "vision_qa" duplicate name);
    the rest are free for every user, matching enable_bubble_tone's own
    precedent above."""
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    data = request.get_json() or {}
    cfg_path = os.path.join(repo_dir, "books", slug, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:
        return jsonify({"status": "error", "message": "Book config not found"}), 404

    if "enable_agent_editor" in data:
        enable_agent = bool(data["enable_agent_editor"])
        if enable_agent:
            try:
                from common.support_profile import is_entitled
                if not is_entitled("cast_registry"):
                    return jsonify({"status": "error",
                                    "message": "Розширена можливість: активуйте через @GetVydraBot (/premium)."}), 403
            except Exception:
                return jsonify({"status": "error", "message": "Entitlement check unavailable"}), 403
        cfg["enable_agent_editor"] = enable_agent
    if "generate_audiobook" in data:
        cfg["generate_audiobook"] = bool(data["generate_audiobook"])
    if "keep_honorifics" in data:
        cfg["keep_honorifics"] = bool(data["keep_honorifics"])
    if "manga_resolution" in data:
        cfg["manga_resolution"] = str(data["manga_resolution"])
    if "enable_mqm_review" in data:
        # Free (like keep_honorifics/generate_audiobook) - not entitlement-
        # gated per TASK-88's own Tier classification. Opt-in because it
        # roughly doubles per-segment LLM time (an extra reviewer call for
        # every translated paragraph) - must never silently turn on.
        cfg["enable_mqm_review"] = bool(data["enable_mqm_review"])
    if "enable_asr_verify" in data:
        cfg["enable_asr_verify"] = bool(data["enable_asr_verify"])

    _atomic_write_json(cfg_path, cfg, ensure_ascii=False, indent=2)
    return jsonify({"status": "success"})

@app.route("/api/characters/<slug>/scan", methods=["POST"])
def characters_scan_api(slug):
    """Launch the NER auto-draft pre-pass (TASK-54) detached; drafts land
    in characters.json as auto_drafted. Entitlement-gated like the rest."""
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    try:
        from common.support_profile import is_entitled
        if not is_entitled("cast_registry"):
            return jsonify({"status": "error",
                            "message": "Розширена можливість: /premium у @GetVydraBot"}), 403
    except Exception:
        return jsonify({"status": "error", "message": "Entitlement check unavailable"}), 403
    heavy = _heavy_state()
    if heavy["agent"]:
        return _busy_409("🤖 ШІ-агент зараз працює — сканування персонажів використовує ту саму модель. Зачекайте або зупиніть агента.")
    if heavy["conversion"]:
        return _busy_409("📚 Йде переклад книги — сканування персонажів конкурувало б за ресурси. Дочекайтесь завершення перекладу.")
    if heavy["llama_server"]:
        return _busy_409("Модель перекладу все ще завантажена в пам'яті (лишається так для швидкості, навіть коли нічого не перекладає). Зупиніть її (кнопка «Моделі» на головній → Stop) і повторіть — після сканування увімкніть назад.")
    book_dir = os.path.join(repo_dir, "books", slug)
    model = os.path.expanduser("~/models/gemma3-4b/gemma-3-4b-it-Q4_K_M.gguf")
    if not os.path.exists(model):
        return jsonify({"status": "error", "model_missing": True,
                        "message": "Модель аналізу (~3.3GB) ще не завантажена. "
                                   "Завантаження стартує з деплой-флоу розширених можливостей."}), 409
    data = request.get_json(silent=True) or {}
    page_start = data.get("page_start")
    page_end = data.get("page_end")
    log_path = os.path.join(book_dir, "edits", "ner_scan.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    cmd = ["python3", os.path.join(repo_dir, "bin", "cast_ner_prepass.py"),
           "--book-dir", book_dir]
    try:
        if page_start is not None:
            cmd.extend(["--page-start", str(int(page_start))])
        if page_end is not None:
            cmd.extend(["--page-end", str(int(page_end))])
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Неправильний формат сторінок"}), 400
    with open(log_path, "w") as lf:
        subprocess.Popen(
            cmd, stdout=lf, stderr=subprocess.STDOUT,
            cwd=repo_dir, start_new_session=True)
    # Auto-resume-on-restart, same mechanism/reasoning as agent_editor.py's
    # registration above - a Termux kill mid-scan previously left NER
    # scanning silently un-resumed. cast_ner_prepass.py's own de-dup
    # against existing name_source entries (see that file) makes a
    # re-launch of the identical command safe to simply redo, not just
    # resumable in principle. Clears its own state on completion.
    _write_active_conversion_state(slug, cmd, repo_dir, log_path)
    return jsonify({"status": "started",
                    "message": "Сканування персонажів запущено (кілька хвилин); "
                               "оновіть список згодом."})

@app.route("/api/characters/<slug>/scan-progress", methods=["GET"])
def characters_scan_progress_api(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    try:
        from common.support_profile import is_entitled
        if not is_entitled("cast_registry"):
            return jsonify({"status": "error",
                            "message": "Розширена можливість: /premium у @GetVydraBot"}), 403
    except Exception:
        return jsonify({"status": "error", "message": "Entitlement check unavailable"}), 403

    book_dir = os.path.join(repo_dir, "books", slug)
    progress_path = os.path.join(book_dir, "edits", "ner_scan_progress.json")
    if os.path.exists(progress_path):
        try:
            with open(progress_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return jsonify(data)
        except Exception:
            pass
    return jsonify({"stage": None, "percent": 0})

@app.route("/api/characters/<slug>/scan/stop", methods=["POST"])
def characters_scan_stop_api(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    try:
        from common.support_profile import is_entitled
        if not is_entitled("cast_registry"):
            return jsonify({"status": "error",
                            "message": "Розширена можливість: /premium у @GetVydraBot"}), 403
    except Exception:
        return jsonify({"status": "error", "message": "Entitlement check unavailable"}), 403

    subprocess.run(["pkill", "-f", "cast_ner_prepass.py"], capture_output=True)
    subprocess.run(["pkill", "-f", "llama-cli"], capture_output=True)
    _clear_active_conversion_state(slug)
    
    # Write a stopped progress file
    try:
        book_dir = os.path.join(repo_dir, "books", slug)
        progress_path = os.path.join(book_dir, "edits", "ner_scan_progress.json")
        data = {"stage": "зупинено", "percent": 0, "done": True, "error": "Сканування зупинено користувачем."}
        tmp = progress_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, progress_path)
    except Exception:
        pass

    return jsonify({"status": "success", "message": "Сканування зупинено."})

def _resolve_manga_input(book_dir, slug):
    source_dir = os.path.join(book_dir, "source")
    if os.path.isdir(source_dir):
        return source_dir
    for possible_ext in [".cbz", ".cbr", ".cb7", ".zip", ".rar", ".pdf", ".epub"]:
        input_path = os.path.join(book_dir, f"{slug}{possible_ext}")
        if os.path.exists(input_path):
            return input_path
    return None

def get_manga_page_image(book_dir, slug, page_stem):
    import zipfile
    import shutil
    import tempfile
    from PIL import Image

    # 1. Search actual source dir
    actual_source_dir = os.path.join(book_dir, "source")
    if os.path.isdir(actual_source_dir):
        for f in os.listdir(actual_source_dir):
            if os.path.splitext(f)[0] == page_stem and f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                return Image.open(os.path.join(actual_source_dir, f))

    # 2. Search preview cache source dir
    preview_source_dir = os.path.join(book_dir, "preview_cache", "source")
    if os.path.isdir(preview_source_dir):
        for f in os.listdir(preview_source_dir):
            if os.path.splitext(f)[0] == page_stem and f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                return Image.open(os.path.join(preview_source_dir, f))

    # 3. If archive exists, extract on the fly
    archive_path = _resolve_manga_input(book_dir, slug)
    if archive_path:
        source_ext = os.path.splitext(archive_path)[1].lower()
        if source_ext in [".zip", ".cbz", ".epub"]:
            with zipfile.ZipFile(archive_path, 'r') as z:
                for file_info in z.infolist():
                    if file_info.is_dir():
                        continue
                    filename = file_info.filename
                    basename = os.path.basename(filename)
                    if os.path.splitext(basename)[0] == page_stem and basename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                        from io import BytesIO
                        return Image.open(BytesIO(z.read(filename)))
        elif source_ext in [".rar", ".cbr", ".cb7"]:
            temp_d = tempfile.mkdtemp()
            try:
                subprocess.run(["7z", "x", f"-o{temp_d}", archive_path, f"*{page_stem}*"], capture_output=True)
                for root, _, files in os.walk(temp_d):
                    for f in files:
                        if os.path.splitext(f)[0] == page_stem and f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                            img = Image.open(os.path.join(root, f))
                            img.load()
                            return img
            except Exception:
                pass
            finally:
                shutil.rmtree(temp_d, ignore_errors=True)
        elif source_ext == ".pdf":
            m = re.search(r'\d+', page_stem)
            if m:
                page_num = int(m.group(0))
                temp_d = tempfile.mkdtemp()
                try:
                    subprocess.run(["pdftoppm", "-png", "-f", str(page_num), "-l", str(page_num), "-r", "150", archive_path, os.path.join(temp_d, "page")], capture_output=True)
                    for f in os.listdir(temp_d):
                        if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                            img = Image.open(os.path.join(temp_d, f))
                            img.load()
                            return img
                except Exception:
                    pass
                finally:
                    shutil.rmtree(temp_d, ignore_errors=True)
                    
    return None

@app.route("/api/characters/<slug>/thumbnail/<char_id>")
def character_thumbnail_api(slug, char_id):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    # char_id lands directly in a filesystem path below (thumbnails/<char_id>.jpg) -
    # same defensive-validation convention as every other <slug>-shaped path
    # param in this file, even though Flask's default converter already
    # excludes "/" (so this is defense-in-depth, not a proven traversal today).
    if not re.match(r"^[a-zA-Z0-9_.-]+$", char_id):
        return jsonify({"status": "error", "message": "Invalid character id"}), 400
    try:
        from common.support_profile import is_entitled
        if not is_entitled("cast_registry"):
            return jsonify({"status": "error", "message": "Access denied"}), 403
    except Exception:
        return jsonify({"status": "error", "message": "Entitlement check failed"}), 403

    from common.cast_registry import load_characters
    from PIL import Image
    
    book_dir = os.path.join(repo_dir, "books", slug)
    thumbnail_path = os.path.join(book_dir, "edits", "thumbnails", f"{char_id}.jpg")
    
    if os.path.exists(thumbnail_path):
        return send_file(thumbnail_path)

    # Find sample_page from characters.json
    characters = load_characters(book_dir)
    sample_page = None
    for char in characters:
        if char.get("id") == char_id:
            sample_page = char.get("sample_page")
            break

    if not sample_page:
        return "Character sample page not found", 404

    try:
        img = get_manga_page_image(book_dir, slug, sample_page)
        if not img:
            return "Manga page image not found", 404

        # Generate thumbnail (PIL modifies image in-place)
        img.thumbnail((480, 999999))
        
        # Save as JPEG
        os.makedirs(os.path.dirname(thumbnail_path), exist_ok=True)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        tmp_path = thumbnail_path + ".tmp"
        img.save(tmp_path, "JPEG", quality=85)
        os.replace(tmp_path, thumbnail_path)
        
        return send_file(thumbnail_path)
    except Exception as e:
        app.logger.error(f"Failed to generate thumbnail for {char_id}: {e}")
        return f"Error generating thumbnail: {e}", 500

@app.route("/api/characters/<slug>/thumbnail/<char_id>", methods=["POST"])
def character_thumbnail_upload_api(slug, char_id):
    """TASK-78: lets a manually-added character (no sample_page - it was
    never seen by the NER scan) get a picture too, and doubles as an
    override for an auto-drafted one the user wants to correct. Writes to
    the SAME cache path the GET handler above checks first, so this is
    the only code path that needs to know about "manual vs auto" - GET
    just serves whatever's newest, no separate flag to keep in sync."""
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    if not re.match(r"^[a-zA-Z0-9_.-]+$", char_id):
        return jsonify({"status": "error", "message": "Invalid character id"}), 400
    try:
        from common.support_profile import is_entitled
        if not is_entitled("cast_registry"):
            return jsonify({"status": "error", "message": "Access denied"}), 403
    except Exception:
        return jsonify({"status": "error", "message": "Entitlement check failed"}), 403

    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400

    # Cheap size guard BEFORE touching PIL - this is a thumbnail source,
    # not a document upload (that's parse-metadata's much larger limit).
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > 15 * 1024 * 1024:
        return jsonify({"status": "error", "message": "Файл завеликий (макс. 15MB)"}), 400

    from PIL import Image
    try:
        img = Image.open(file.stream)
        img.load()  # force-decode now, inside our try/except - a truncated
                    # or non-image file otherwise fails later, off-path
    except Exception:
        return jsonify({"status": "error", "message": "Не вдалося розпізнати зображення"}), 400

    book_dir = os.path.join(repo_dir, "books", slug)
    thumbnail_path = os.path.join(book_dir, "edits", "thumbnails", f"{char_id}.jpg")
    os.makedirs(os.path.dirname(thumbnail_path), exist_ok=True)
    try:
        img.thumbnail((480, 999999))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        tmp_path = thumbnail_path + ".tmp"
        img.save(tmp_path, "JPEG", quality=85)
        os.replace(tmp_path, thumbnail_path)
    except Exception as e:
        app.logger.error(f"Failed to save uploaded thumbnail for {char_id}: {e}")
        return jsonify({"status": "error", "message": f"Помилка збереження: {e}"}), 500

    return jsonify({"status": "success", "message": "Зображення збережено."})

@app.route("/api/characters/<slug>/thumbnail/<char_id>", methods=["DELETE"])
def character_thumbnail_delete_api(slug, char_id):
    """Revert to the auto-detected sample_page thumbnail (or to no image,
    if the character has none) by clearing the cache the GET handler
    checks first - does NOT touch characters.json/sample_page at all."""
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    if not re.match(r"^[a-zA-Z0-9_.-]+$", char_id):
        return jsonify({"status": "error", "message": "Invalid character id"}), 400
    try:
        from common.support_profile import is_entitled
        if not is_entitled("cast_registry"):
            return jsonify({"status": "error", "message": "Access denied"}), 403
    except Exception:
        return jsonify({"status": "error", "message": "Entitlement check failed"}), 403

    book_dir = os.path.join(repo_dir, "books", slug)
    thumbnail_path = os.path.join(book_dir, "edits", "thumbnails", f"{char_id}.jpg")
    try:
        if os.path.exists(thumbnail_path):
            os.remove(thumbnail_path)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    return jsonify({"status": "success", "message": "Зображення видалено."})

@app.route("/api/premium/model-status")
@app.route("/api/premium/models-status")
def premium_models_status_api():
    """TASK-65 onboarding & ASR/MQM model check: return each premium model's
    download state (present / partial with byte progress / absent)."""
    gemma_dir = os.path.expanduser("~/models/gemma3-4b")
    whisper_dir = os.path.expanduser("~/models/sherpa-onnx-whisper-small-int8")
    
    def _f(model_dir, name):
        done = os.path.join(model_dir, name)
        part = done + ".part"
        if os.path.exists(done):
            return {"ready": True, "bytes": os.path.getsize(done)}
        if os.path.exists(part):
            return {"ready": False, "bytes": os.path.getsize(part)}
        return {"ready": False, "bytes": 0}

    gemma_status = _f(gemma_dir, "gemma-3-4b-it-Q4_K_M.gguf")
    mmproj_status = _f(gemma_dir, "mmproj-model-f16.gguf")

    w_enc = _f(whisper_dir, "small-encoder.int8.onnx")
    w_dec = _f(whisper_dir, "small-decoder.int8.onnx")
    w_tok = _f(whisper_dir, "small-tokens.txt")
    asr_ready = w_enc["ready"] and w_dec["ready"] and w_tok["ready"]
    asr_bytes = w_enc["bytes"] + w_dec["bytes"] + w_tok["bytes"]

    downloading = subprocess.run(["pgrep", "-f", "download_premium_models"],
                                 capture_output=True).returncode == 0
    return jsonify({
        "gemma": gemma_status,
        "mmproj": mmproj_status,
        "asr_whisper": {
            "ready": asr_ready,
            "bytes": asr_bytes,
            "expected_bytes": 245000000,
            "encoder": w_enc,
            "decoder": w_dec,
            "tokens": w_tok
        },
        "downloading": downloading,
        "total_expected_bytes": 3745000000,
    })

@app.route("/api/premium/download-models", methods=["POST"])
def premium_download_models_api():
    """Kick off the detached, resumable premium model download (Gemma / ASR Whisper).
    Entitlement-gated like every premium action."""
    try:
        from common.support_profile import is_entitled
        if not is_entitled("cast_registry"):
            return jsonify({"status": "error",
                            "message": "Розширена можливість: /premium у @GetVydraBot"}), 403
    except Exception:
        return jsonify({"status": "error", "message": "Entitlement check unavailable"}), 403
    
    data = request.get_json(silent=True) or {}
    consent_given = data.get("consent_accepted") or data.get("gemma_terms_accepted")
    if not consent_given:
        return jsonify({"status": "error", "message":
                        "Потрібно надати згоду перед завантаженням моделей."}), 400

    if subprocess.run(["pgrep", "-f", "download_premium_models"],
                      capture_output=True).returncode == 0:
        return jsonify({"status": "already_running",
                        "message": "Завантаження моделей вже триває у фоні."})

    target = data.get("target", "all")
    if target not in ["all", "gemma", "asr", "whisper"]:
        target = "all"

    log_path = os.path.expanduser("~/premium-model-download.log")
    env = dict(os.environ, CONSENT_ACCEPTED="1", GEMMA_TERMS_ACCEPTED="1")
    with open(log_path, "w") as lf:
        subprocess.Popen(
            ["bash", os.path.join(repo_dir, "bin", "download_premium_models.sh"), f"--{target}"],
            stdout=lf, stderr=subprocess.STDOUT, start_new_session=True, env=env)
    return jsonify({"status": "started",
                    "message": f"Завантаження моделей ({target}) стартувало у фоні."})

def _heavy_state():
    """One source of truth for 'which heavy model is busy right now'.
    Every launcher of a resource-hungry process consults this and refuses
    with a clear Ukrainian message instead of letting two multi-GB models
    fight over RAM/CPU (the exact failure mode that OOM-killed the whole
    Termux session during TASK-65 testing)."""
    def up(pattern):
        return subprocess.run(["pgrep", "-f", pattern],
                              capture_output=True).returncode == 0
    return {
        "llama_server": up("llama-server"),
        "agent": up("agent_editor.py") or up("llama-mtmd-cli"),
        "ner": up("cast_ner_prepass.py"),
        "conversion": any(p.poll() is None for p in active_processes.values())
                      or up("translate_manga.py") or up("run_conversion_batches.py")
                      or up("translate_epub.py"),
    }


def _busy_409(message):
    return jsonify({"status": "busy", "message": message}), 409


@app.route("/api/agent-editor/status/<slug>")
def agent_editor_status_api(slug):
    """Live state for the Agent tab: is a scan running, log tail, how
    many flagged cases exist, how many agent proposals are pending. The
    agent NEVER starts on its own - only the explicit start button (or
    API call) launches it; this endpoint is read-only."""
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    book_dir = os.path.join(repo_dir, "books", slug)
    running = subprocess.run(["pgrep", "-f", "agent_editor.py"],
                             capture_output=True).returncode == 0
    log_lines = []
    case_total = None
    case_done = 0
    log_path = os.path.join(book_dir, "edits", "agent_editor.log")
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                full_log = f.read()
            log_lines = full_log.splitlines()[-30:]
            # Live progress bar for the Agent tab (previously: only a raw
            # scrolling log, no visible "how far along is this" signal -
            # real complaint, 2026-07-19: "процес йде, але не видно
            # прогресу"). agent_editor.py logs "N flagged case(s); limit
            # M" once at start, then one "PROPOSED ..." or "skip ..." line
            # per case processed - counted against the FULL log, not the
            # 30-line display tail, so progress stays accurate on a long
            # run even once the tail has scrolled past earlier cases.
            m = re.search(r"(\d+) flagged case\(s\); limit (\d+)", full_log)
            if m:
                case_total = min(int(m.group(1)), int(m.group(2)))
            # Every real log() line is prefixed "[agent_editor] " (see
            # bin/agent_editor.py's log()) - the anchor must account for
            # that or it never matches anything, which is exactly what
            # shipped first: progress stuck at 0/N forever regardless of
            # real completed cases, confirmed live by Q immediately.
            case_done = len(re.findall(r"^\[agent_editor\] (?:PROPOSED|skip) ", full_log, re.MULTILINE))
        except OSError:
            pass
    flagged = 0
    qf_path = os.path.join(book_dir, "quality_flags.json")
    if os.path.exists(qf_path):
        try:
            with open(qf_path, "r", encoding="utf-8") as f:
                flagged = sum(1 for x in json.load(f)
                              if x.get("reason") in ("box_overlap", "overflow", "text_overflow"))
        except Exception:
            pass
    agent_pending = sum(1 for e in edit_store.list_edits(slug, mode="manga", status="pending")
                        if e.get("source") == "gemma_agent")
    llama_running = subprocess.run(["pgrep", "-f", "llama-server"],
                                   capture_output=True).returncode == 0
    ner_running = subprocess.run(["pgrep", "-f", "cast_ner_prepass.py"],
                                 capture_output=True).returncode == 0
    return jsonify({"running": running, "log": log_lines, "flagged": flagged,
                    "agent_pending": agent_pending, "llama_running": llama_running,
                    "ner_running": ner_running, "case_total": case_total, "case_done": case_done})

@app.route("/api/agent-editor/stop/<slug>", methods=["POST"])
def agent_editor_stop_api(slug):
    """Owner's hard stop: kills the agent scan and its vision inference
    immediately. Deliberately narrow patterns - never touches
    llama-server or a running translation."""
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    subprocess.run(["pkill", "-f", "agent_editor.py"], capture_output=True)
    subprocess.run(["pkill", "-f", "llama-mtmd-cli"], capture_output=True)
    # Explicit user stop should not trigger auto-resume on next restart -
    # same reasoning as the main conversion's stop handler.
    _clear_active_conversion_state(slug)
    return jsonify({"status": "success", "message": "Агента зупинено."})

@app.route("/api/agent-editor/scan/<slug>", methods=["POST"])
def agent_editor_scan_api(slug):
    """TASK-65 (spec "TASK-53"): launch the Gemma vision edit-agent
    detached over the book's ALREADY-FLAGGED QA cases only. Proposals
    land in edit_store as pending/source=gemma_agent - the human
    Approve/Discard gate is never bypassed. Opt-in per book
    (enable_agent_editor in config.json) + premium-gated like the rest
    of the Cast Registry feature line."""
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    try:
        from common.support_profile import is_entitled
        if not is_entitled("cast_registry"):
            return jsonify({"status": "error",
                            "message": "Розширена можливість: /premium у @GetVydraBot"}), 403
    except Exception:
        return jsonify({"status": "error", "message": "Entitlement check unavailable"}), 403
    book_dir = os.path.join(repo_dir, "books", slug)
    cfg = {}
    cfg_path = os.path.join(book_dir, "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
    if not cfg.get("enable_agent_editor"):
        return jsonify({"status": "error",
                        "message": "Агентний редактор вимкнено для цієї книги "
                                   "(enable_agent_editor у config.json)."}), 400
    heavy = _heavy_state()
    if heavy["conversion"]:
        return _busy_409("📚 Йде переклад книги. ШІ-агента можна запускати тільки коли переклад завершено або зупинено — обидва процеси надто важкі для одночасної роботи.")
    if heavy["ner"]:
        return _busy_409("🔎 Йде сканування персонажів. Зачекайте кілька хвилин — потім запускайте агента.")
    model = os.path.expanduser("~/models/gemma3-4b/gemma-3-4b-it-Q4_K_M.gguf")
    mmproj = os.path.expanduser("~/models/gemma3-4b/mmproj-model-f16.gguf")
    if not (os.path.exists(model) and os.path.exists(mmproj)):
        return jsonify({"status": "error", "model_missing": True,
                        "message": "Vision-модель ще не завантажена."}), 409
    data = request.get_json() or {}
    limit = min(int(data.get("limit", 5) or 5), 20)
    page_start = data.get("page_start")
    page_end = data.get("page_end")
    log_path = os.path.join(book_dir, "edits", "agent_editor.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    env = dict(os.environ)
    cmd = ["python3", os.path.join(repo_dir, "bin", "agent_editor.py"),
           "--book", slug, "--limit", str(limit)]
    try:
        if page_start is not None:
            cmd.extend(["--page-start", str(int(page_start))])
        if page_end is not None:
            cmd.extend(["--page-end", str(int(page_end))])
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Неправильний формат сторінок"}), 400
    with open(log_path, "w") as lf:
        subprocess.Popen(
            cmd, stdout=lf, stderr=subprocess.STDOUT,
            cwd=repo_dir, env=env, start_new_session=True)
    # Register for auto-resume-on-restart (same mechanism as the main
    # conversion pipeline, see ACTIVE_CONVERSION_STATE_PATH above) - a
    # Termux kill mid-scan used to leave the agent silently not-resumed,
    # unlike translate_manga.py/translate_epub.py. agent_editor.py clears
    # its own entry on normal completion (see its main()); its per-case
    # skip-if-already-pending-or-approved check means a re-launch of the
    # identical command correctly resumes from the cases not yet
    # processed, not from scratch.
    _write_active_conversion_state(slug, cmd, repo_dir, log_path)
    return jsonify({"status": "started",
                    "message": "Агент аналізує позначені сторінки (vision, кілька хвилин "
                               "на випадок); пропозиції з'являться в Pending Edits."})

@app.route("/api/change-password", methods=["POST"])
def change_password_api():
    """In-UI password change (TASK-61) - the only way to change it before
    this was hand-editing ~/.bashrc, exactly the friction a non-specialist
    user can't clear alone (real incident: Q forgot the printed password
    within the same session). Updates the running process's auth
    immediately (no restart needed) AND persists TWO ways for future
    restarts: KBG_WEB_PASSWORD in ~/.bashrc (deploy.sh's own mechanism -
    kept as-is since "reopen the Termux app" sources it fine), AND
    web_credentials.json directly (TASK-74: found by code review that
    ~/.termux/boot/start-services.sh - the REAL device-reboot autostart
    path, distinct from "user reopens Termux" - execs bash directly with
    its own shebang and never sources ~/.bashrc, so KBG_WEB_PASSWORD is
    never set on a genuine reboot; startup then silently fell back to
    whatever was in web_credentials.json, which this endpoint never
    updated - a real, previously-undetected lockout-after-reboot bug).
    Writing both paths makes password persistence not depend on which
    autostart trigger actually fires."""
    data = request.get_json() or {}
    new_password = (data.get("new_password") or "").strip()
    if len(new_password) < 6:
        return jsonify({"status": "error", "message": "Пароль має бути щонайменше 6 символів"}), 400
    if "'" in new_password or "\n" in new_password:
        return jsonify({"status": "error", "message": "Пароль не може містити апостроф чи перенос рядка"}), 400

    username = next(iter(users_data), env_username)
    users_data[username] = generate_password_hash(new_password)

    # web_credentials.json is the fallback startup source when
    # KBG_WEB_PASSWORD isn't set (see the reboot-path bug explained
    # above) - atomic write so a Termux kill mid-write can't corrupt it
    # into a second way to get locked out.
    try:
        _atomic_write_json(credentials_file, users_data)
    except OSError as e:
        return jsonify({
            "status": "partial",
            "message": f"Пароль змінено для поточного сеансу, але не вдалося зберегти "
                       f"назавжди ({e}) - після перезапуску діятиме старий.",
        }), 200

    bashrc_path = os.path.expanduser("~/.bashrc")
    new_line = f"export KBG_WEB_PASSWORD='{new_password}'\n"
    try:
        try:
            with open(bashrc_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []
        replaced = False
        for i, line in enumerate(lines):
            if line.strip().startswith("export KBG_WEB_PASSWORD="):
                lines[i] = new_line
                replaced = True
                break
        if not replaced:
            # TASK-63: must land BEFORE the autostart trigger line (which
            # runs "bash start-all-services.sh", spawning Flask as a child
            # that only inherits env exports already in effect at that
            # line) - appending to the end is invisible to Flask on every
            # future autostart. Insert right before that line if present,
            # otherwise at the very top.
            insert_at = next(
                (i for i, line in enumerate(lines) if "start-all-services.sh" in line),
                0,
            )
            lines.insert(insert_at, new_line)
        with open(bashrc_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError as e:
        # web_credentials.json already has the new password at this point
        # (see above) - a genuine reboot will still pick it up correctly.
        # Only "reopen Termux app" (which relies on .bashrc) is at risk.
        return jsonify({
            "status": "partial",
            "message": f"Пароль змінено та збережено на випадок перезавантаження, "
                       f"але не вдалося оновити ~/.bashrc ({e}) - при простому "
                       f"повторному відкритті Termux (без перезавантаження) може "
                       f"знадобитись новий пароль ще раз.",
        }), 200

    return jsonify({"status": "success",
                    "message": f"Пароль змінено. Логін лишається «{username}»."})

@app.route("/manual")
def user_manual():
    """Ukrainian user manual with live-UI screenshots (TASK-56)."""
    return render_template("manual.html")

@app.route("/api/support/profile")
def support_profile():
    """Support-banner status for the dashboard card (TASK-51).

    Combines the LOCAL opt-out flag (global_settings.json) with the
    REMOTE Appwrite profile flag set via @GetVydraBot. The remote read
    inherits support_profile.fetch_profile()'s hard timeout and
    fail-toward-enabled semantics - this endpoint never hangs the UI.
    """
    settings = load_global_settings()
    local_disabled = bool(settings.get("no_support_banner"))
    try:
        from common.support_profile import fetch_profile
        remote = fetch_profile()
    except Exception:
        remote = {"banner_disabled": False, "priority_tier": 0}
    try:
        from common.support_banner import load_support_config
        cfg_enabled = load_support_config() is not None
    except Exception:
        cfg_enabled = False
    try:
        from common.support_profile import get_entitlements
        entitlements = get_entitlements()
    except Exception:
        entitlements = []
    # TASK-71: every fresh deploy.sh clone ships support_config.json with
    # an empty appwrite.telegram_id (safe-by-default - see that file's own
    # comment) - the dashboard needs to know whether THIS installation has
    # been linked yet, to show the one-time linking prompt.
    try:
        from common.support_banner import CONFIG_PATH
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw_cfg = json.load(f)
        linked_telegram_id = str((raw_cfg.get("appwrite") or {}).get("telegram_id") or "").strip()
    except Exception:
        linked_telegram_id = ""
    # TASK-73: free tier caps devices per account at MAX_FREE_DEVICES -
    # this is display-only (enforcement is server-side, see tg-support-bot's
    # _heartbeat()), but the dashboard is where a family user actually
    # looks, not a Telegram push, so surface it here.
    try:
        from common.support_profile import get_device_status
        device_status = get_device_status()
    except Exception:
        device_status = {"count": 0, "limit": 3, "over_limit": False}
    return jsonify({
        "config_enabled": cfg_enabled,
        "entitlements": entitlements,
        "local_disabled": local_disabled,
        "remote_disabled": bool(remote.get("banner_disabled")),
        "priority_tier": int(remote.get("priority_tier") or 0),
        "effective_disabled": local_disabled or bool(remote.get("banner_disabled")),
        "bot_link": "https://t.me/GetVydraBot",
        "telegram_id": linked_telegram_id,
        "device_count": device_status["count"],
        "device_limit": device_status["limit"],
        "device_over_limit": device_status["over_limit"],
    })

@app.route("/api/support/local-optout", methods=["POST"])
def support_local_optout():
    """One-step LOCAL banner toggle - honors the plan's promise that the
    opt-out is never hidden behind extra steps. The Telegram-bot route is
    the primary funnel (donation options live there), this is the direct
    switch."""
    data = request.get_json() or {}
    settings = load_global_settings()
    settings["no_support_banner"] = bool(data.get("disabled"))
    save_global_settings(settings)
    return jsonify({"status": "success",
                    "local_disabled": settings["no_support_banner"]})

@app.route("/api/support/link-telegram", methods=["POST"])
def support_link_telegram():
    """Bind this installation to its owner's real Telegram account
    (TASK-71). Every fresh deploy.sh clone inherits support_config.json
    with an intentionally EMPTY appwrite.telegram_id (real incident,
    2026-07-19: it used to ship with the original developer's own ID
    baked in, so every new install's support/entitlement checks silently
    queried - and could overwrite the heartbeat state of - the wrong
    Appwrite user). Real fix is one-time, local, explicit: the user pastes
    the numeric ID the bot shows them after /start."""
    data = request.get_json() or {}
    tg_id = str(data.get("telegram_id", "")).strip()
    if not tg_id.isdigit():
        return jsonify({"status": "error",
                        "message": "Telegram ID має складатись лише з цифр - скопіюйте його з повідомлення бота після /start."}), 400
    from common.support_banner import CONFIG_PATH
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}
    cfg.setdefault("appwrite", {})["telegram_id"] = tg_id
    _atomic_write_json(CONFIG_PATH, cfg, ensure_ascii=False, indent=2)
    return jsonify({"status": "success", "telegram_id": tg_id})

@app.route("/api/update", methods=["POST"])
def self_update():
    """One-button service update (TASK-46).

    Checks the public GitHub remote for new commits; if there are any,
    spawns bin/self-update.sh DETACHED (it has to kill and restart this
    very Flask process, so it can't be our child in the normal sense)
    and tells the client the service is about to restart. Refuses to
    update while a conversion is running - killing Flask mid-run would
    orphan the conversion from the dashboard and the state-file
    lifecycle (TASK-41/45).
    """
    if any(p.poll() is None for p in active_processes.values()):
        return jsonify({
            "status": "busy",
            "message": "A conversion is currently running. Wait for it to finish, then update."
        }), 409

    try:
        # Check SSH remote 'server-projects' first to avoid git-remote-https signal 11 in Termux
        remote_used = "server-projects"
        fetch_res = subprocess.run(
            ["git", "fetch", "server-projects"],
            cwd=repo_dir, capture_output=True, text=True, timeout=30
        )
        if fetch_res.returncode != 0:
            remote_used = "origin"
            fetch_res2 = subprocess.run(
                ["git", "fetch", "origin"],
                cwd=repo_dir, capture_output=True, text=True, timeout=60
            )
            if fetch_res2.returncode != 0:
                err = (fetch_res2.stderr or fetch_res2.stdout or fetch_res.stderr or "").strip()
                if "signal 11" in err or "SEGV" in err:
                    err = "Git HTTPS transport crash in Termux. Please update via SSH or local repository."
                return jsonify({"status": "error", "message": f"git fetch failed: {err}"}), 500

        behind = int(subprocess.run(
            ["git", "rev-list", "--count", f"HEAD..{remote_used}/master"],
            cwd=repo_dir, capture_output=True, text=True, timeout=15, check=True
        ).stdout.strip())
        current = subprocess.run(
            ["git", "log", "-1", "--format=%h %s"],
            cwd=repo_dir, capture_output=True, text=True, timeout=15, check=True
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        return jsonify({"status": "error",
                        "message": f"git failed: {(e.stderr or e.stdout or '').strip()}"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error",
                        "message": "git fetch timed out - check network connectivity."}), 504

    if behind == 0:
        return jsonify({"status": "up_to_date", "version": current})

    env_vars = os.environ.copy()
    env_vars["UPDATE_REMOTE"] = remote_used

    subprocess.Popen(
        ["bash", os.path.join(repo_dir, "bin", "self-update.sh")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=repo_dir, start_new_session=True, env=env_vars
    )
    return jsonify({"status": "updating", "behind": behind, "version": current})

@app.route("/api/models")
def get_models_info():
    import socket
    import glob
    default_model = os.path.join(os.path.expanduser("~"), "models", "hy-mt2", "Hy-MT2-7B-Q4_K_M.gguf")
    translation_model = settings.get("translation_model", default_model)

    models_dir = os.path.expanduser("~/models")
    available = []
    if os.path.exists(models_dir):
        for path in glob.glob(os.path.join(models_dir, "**/*.gguf"), recursive=True):
            available.append(path)
            
    is_open = False
    try:
        with socket.create_connection(("127.0.0.1", 8081), timeout=0.5) as s:
            is_open = True
    except Exception:
        pass
        
    loaded_model = None
    if is_open:
        try:
            import requests
            resp = requests.get("http://127.0.0.1:8081/props", timeout=0.5)
            if resp.status_code == 200:
                data = resp.json()
                loaded_model = data.get("model_alias", "") or data.get("model", "")
        except Exception:
            pass
            
    return jsonify({
        "translation_model": translation_model,
        "available_models": available,
        "server_status": {
            "running": is_open,
            "loaded_model": loaded_model
        }
    })

@app.route("/api/models/configure", methods=["POST"])
def configure_models():
    data = request.get_json() or {}
    translation_model = data.get("translation_model")

    settings = load_global_settings()
    if translation_model:
        settings["translation_model"] = translation_model

    save_global_settings(settings)
    return jsonify({"status": "success"})

def _read_llama_pid():
    """Return the tracked llama-server PID if the PID file exists and that
    process is still alive, else None (also true for a stale/dead PID)."""
    if not os.path.exists(LLAMA_PID_FILE):
        return None
    try:
        with open(LLAMA_PID_FILE, "r") as f:
            pid = int(f.read().strip())
    except Exception:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid

def _stop_llama_server():
    """Stop the tracked llama-server (if any) and always clear the PID
    file afterward, so a stale/dead entry never lingers and blocks a
    future start."""
    import signal
    import time
    pid = _read_llama_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.3)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        except OSError:
            pass
    if os.path.exists(LLAMA_PID_FILE):
        try:
            os.remove(LLAMA_PID_FILE)
        except Exception:
            pass

@app.route("/api/models/start", methods=["POST"])
def start_translation_server_api():
    heavy = _heavy_state()
    if heavy["agent"]:
        return _busy_409("🤖 ШІ-агент зараз використовує пам'ять для vision-аналізу. Сервер перекладу увімкнеться автоматично, щойно агент завершить (або зупиніть агента у вкладці «Агент»).")
    if heavy["ner"]:
        return _busy_409("🔎 Йде сканування персонажів — воно займає пам'ять тієї ж моделі. Зачекайте кілька хвилин і повторіть.")
    import time

    # Clear a stale lock (e.g. left behind by a crashed request) before
    # trying to acquire — a lock older than this is never legitimate,
    # since the critical section below only takes ~1s.
    if os.path.exists(LLAMA_START_LOCK_FILE):
        try:
            age = time.time() - os.path.getmtime(LLAMA_START_LOCK_FILE)
        except OSError:
            age = LLAMA_START_LOCK_STALE_SECONDS + 1
        if age > LLAMA_START_LOCK_STALE_SECONDS:
            try:
                os.remove(LLAMA_START_LOCK_FILE)
            except Exception:
                pass

    try:
        lock_fd = os.open(LLAMA_START_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return jsonify({"status": "error", "message": "A start is already in progress"}), 409

    try:
        os.write(lock_fd, str(os.getpid()).encode())
        os.close(lock_fd)

        # Single point of truth for stopping the old instance: the API
        # layer, via the PID file. start-translation-server.sh no longer
        # does its own pkill.
        _stop_llama_server()

        sh_script = os.path.expanduser("~/start-translation-server.sh")
        if not os.path.exists(sh_script):
            return jsonify({"status": "error", "message": "Translation server script not found"}), 404

        subprocess.Popen(["bash", sh_script, LLAMA_PID_FILE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        return jsonify({"status": "success", "message": "Translation server start triggered"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        try:
            os.remove(LLAMA_START_LOCK_FILE)
        except Exception:
            pass

@app.route("/api/models/stop", methods=["POST"])
def stop_translation_server_api():
    _stop_llama_server()
    return jsonify({"status": "success", "message": "Translation server stopped"})

# -------------------------------------------------------------
# VISUAL STAGE VIEWER / QUALITY ASSURANCE ROUTES
# -------------------------------------------------------------

@app.route("/view/<slug>")
def view_book_stages(slug):
    if not validate_slug(slug):
        return "Invalid slug format", 400
    # Serve visualizer page
    return render_template("stages.html", slug=slug)

@app.route("/api/preview/audio/<slug>/<chunk_hash>")
def preview_audio(slug, chunk_hash):
    if not validate_slug(slug) or not re.match(r"^[a-f0-9]{64}$", chunk_hash):
        return "Invalid parameters", 400
    paths = resolve_book_paths(repo_dir, slug)
    for voice_slug in ["styletts2", "supertonic-3-tts-int8"]:
        wav_path = os.path.join(paths["audio_dir"], f"chunks_{voice_slug}", f"{chunk_hash}.wav")
        if os.path.exists(wav_path):
            return send_file(wav_path, mimetype="audio/wav")
    return "Audio not found", 404

@app.route("/api/preview/manga/<slug>")
def preview_manga(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
        
    paths = resolve_book_paths(repo_dir, slug)
    config_path = paths["config_path"]
    if not os.path.exists(config_path):
        return jsonify({"status": "error", "message": "Book not found"}), 404
        
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
        
    if not cfg.get("is_manga", False):
        return jsonify({"status": "error", "message": "Not a manga"}), 400
        
    source_ext = ""
    is_dir_source = os.path.isdir(os.path.join(paths["book_dir"], "source"))
    if not is_dir_source:
        for possible_ext in [".cbz", ".cbr", ".cb7", ".zip", ".rar", ".pdf"]:
            if os.path.exists(os.path.join(paths["book_dir"], f"{slug}{possible_ext}")):
                source_ext = possible_ext
                break
        if not source_ext:
            return jsonify({"status": "error", "message": "Manga source file or directory not found"}), 400
            
    translated_file = os.path.join(paths["book_dir"], "output", f"{slug}_translated_{cfg.get('target_lang', 'uk')}.cbz")
    
    preview_cache = os.path.join(paths["book_dir"], "preview_cache")
    os.makedirs(preview_cache, exist_ok=True)
    
    # 1. Source pages extraction
    src_preview_dir = os.path.join(preview_cache, "source")
    os.makedirs(src_preview_dir, exist_ok=True)
    if not os.listdir(src_preview_dir):
        try:
            if is_dir_source:
                pass  # Read directly from actual source directory
            elif source_ext in [".zip", ".cbz"]:
                source_file = os.path.join(paths["book_dir"], f"{slug}{source_ext}")
                subprocess.run(["unzip", "-j", source_file, "*.png", "*.jpg", "*.jpeg", "-d", src_preview_dir], capture_output=True)
            elif source_ext in [".rar", ".cbr"]:
                source_file = os.path.join(paths["book_dir"], f"{slug}{source_ext}")
                subprocess.run(["unrar", "e", source_file, "-d", src_preview_dir], capture_output=True)
            elif source_ext == ".pdf":
                source_file = os.path.join(paths["book_dir"], f"{slug}{source_ext}")
                subprocess.run(["pdftoppm", "-png", "-f", "1", "-l", "5", "-r", "100", source_file, os.path.join(src_preview_dir, "page")], capture_output=True)
        except Exception:
            pass
            
    # 2. Cleaned pages extraction (not needed anymore, we serve directly from the actual cleaned folder)
    cleaned_preview_dir = os.path.join(preview_cache, "cleaned")
    os.makedirs(cleaned_preview_dir, exist_ok=True)
            
    # 3. Translated pages extraction (only extract if archive exists and target_preview is empty)
    tgt_preview_dir = os.path.join(preview_cache, "translated")
    os.makedirs(tgt_preview_dir, exist_ok=True)
    if os.path.exists(translated_file) and not os.listdir(tgt_preview_dir):
        try:
            subprocess.run(["unzip", "-j", translated_file, "*.png", "*.jpg", "*.jpeg", "-d", tgt_preview_dir], capture_output=True)
        except Exception:
            pass
            
    from natsort import natsorted
    actual_source_dir = os.path.join(paths["book_dir"], "source")
    actual_cleaned_dir = os.path.join(paths["book_dir"], "cleaned")
    actual_translated_dir = os.path.join(paths["book_dir"], "translated")

    if is_dir_source and os.path.exists(actual_source_dir):
        src_files = natsorted([f for f in os.listdir(actual_source_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
    else:
        src_files = natsorted([f for f in os.listdir(src_preview_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])

    if os.path.exists(actual_cleaned_dir):
        clean_files = natsorted([f for f in os.listdir(actual_cleaned_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
    else:
        clean_files = natsorted([f for f in os.listdir(cleaned_preview_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])

    if os.path.exists(actual_translated_dir):
        tgt_files = natsorted([f for f in os.listdir(actual_translated_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
    else:
        tgt_files = natsorted([f for f in os.listdir(tgt_preview_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
    
    return jsonify({
        "status": "success",
        "source_pages": src_files,
        "cleaned_pages": clean_files,
        "translated_pages": tgt_files
    })

@app.route("/api/preview/manga-file/<slug>/<folder>/<filename>")
def serve_manga_preview_file(slug, folder, filename):
    if not validate_slug(slug) or folder not in ["source", "translated", "cleaned"]:
        return "Invalid parameters", 400
    paths = resolve_book_paths(repo_dir, slug)
    
    # Try actual directory first
    actual_path = os.path.join(paths["book_dir"], folder, filename)
    if os.path.exists(actual_path):
        return send_file(actual_path)
        
    # Fallback to preview_cache
    file_path = os.path.join(paths["book_dir"], "preview_cache", folder, filename)
    if os.path.exists(file_path):
        return send_file(file_path)
    return "Not found", 404

@app.route("/api/preview/manga-bubbles/<slug>/<page_filename>")
def preview_manga_bubbles(slug, page_filename):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    paths = resolve_book_paths(repo_dir, slug)
    page_filename = os.path.basename(page_filename)
    page_stem = os.path.splitext(page_filename)[0]
    meta_path = os.path.join(paths["book_dir"], "bubbles_meta", f"{page_stem}.json")
    if not os.path.exists(meta_path):
        return jsonify({"status": "error", "message": "Page not processed yet - no bubble metadata found"}), 404
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            bubbles = json.load(f)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to read bubble metadata: {e}"}), 500
    return jsonify({"status": "success", "page": page_filename, "bubbles": bubbles})

@app.route("/api/preview/manga-quality-flags/<slug>")
def preview_manga_quality_flags(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    paths = resolve_book_paths(repo_dir, slug)
    quality_flags_path = os.path.join(paths["book_dir"], "quality_flags.json")
    if not os.path.exists(quality_flags_path):
        return jsonify({"status": "success", "flags": []})
    try:
        with open(quality_flags_path, "r", encoding="utf-8") as f:
            flags = json.load(f)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to read quality flags: {e}"}), 500
    return jsonify({"status": "success", "flags": flags})

@app.route("/api/preview/asr-quality-flags/<slug>")
def preview_asr_quality_flags(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
    paths = resolve_book_paths(repo_dir, slug)
    asr_queue_path = os.path.join(paths["book_dir"], "asr_stress_queue.json")
    if not os.path.exists(asr_queue_path):
        return jsonify({"status": "success", "flags": []})
    try:
        with open(asr_queue_path, "r", encoding="utf-8") as f:
            flags = json.load(f)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to read ASR quality flags: {e}"}), 500
    return jsonify({"status": "success", "flags": flags})

@app.route("/api/preview/book/<slug>")
def preview_book_stages(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
        
    paths = resolve_book_paths(repo_dir, slug)
    config_path = paths["config_path"]
    if not os.path.exists(config_path):
        return jsonify({"status": "error", "message": "Book not found"}), 404
        
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
        
    target_lang = cfg.get("target_lang", "uk")
    source_lang = cfg.get("source_lang", "ru")
    
    trans_cache = {}
    if os.path.exists(paths["translate_cache"]):
        try:
            with open(paths["translate_cache"], "r", encoding="utf-8") as f:
                trans_cache = json.load(f)
        except Exception:
            pass
            
    stress_cache = {}
    stress_cache_path = os.path.join(paths["book_dir"], "translated", f"stress_cache_{target_lang}.json")
    if os.path.exists(stress_cache_path):
        try:
            with open(stress_cache_path, "r", encoding="utf-8") as f:
                stress_cache = json.load(f)
        except Exception:
            pass
            
    import hashlib
    def get_hash(text):
        return hashlib.sha256(text.encode('utf-8')).hexdigest()
        
    from kbg_web.status_helper import split_paragraph_to_chunks
    
    tts_engine = cfg.get("tts_engine", "supertonic3")
    voice_slug = "styletts2" if tts_engine == "styletts2" else "supertonic-3-tts-int8"
    chunks_dir = os.path.join(paths["audio_dir"], f"chunks_{voice_slug}")
    
    suffix = f"_translated_{target_lang}" if (target_lang != source_lang) else ""
    if suffix:
        target_md_file = os.path.join(paths["translated_dir"], f"merged_translated_{target_lang}.md")
    else:
        target_md_file = os.path.join(paths["translated_dir"], f"merged_source_{source_lang}.md")
        
    from flask import request
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 30, type=int)
    
    raw_chunks = []
    if os.path.exists(target_md_file):
        try:
            with open(target_md_file, "r", encoding="utf-8") as f:
                content = f.read()
            raw_paragraphs = re.split(r'\n\s*\n', content)
            
            max_chunk_chars = 150 if tts_engine == "styletts2" else 1000
            
            for p in raw_paragraphs:
                p = p.strip()
                if not p or p.startswith("#"):
                    continue
                chunks = split_paragraph_to_chunks(p, max_chars=max_chunk_chars)
                for chunk in chunks:
                    chunk = chunk.strip()
                    if chunk:
                        raw_chunks.append(chunk)
        except Exception as e:
            return jsonify({"status": "error", "message": f"Error parsing book: {e}"}), 500
            
    total_chunks = len(raw_chunks)
    total_pages = (total_chunks + limit - 1) // limit if total_chunks > 0 else 1
    
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    sliced_chunks = raw_chunks[start_idx:end_idx]
    
    paragraphs = []
    if sliced_chunks:
        try:
            # Speed up the translation original text lookup using a reverse index dict
            reverse_trans_cache = {v.strip(): k for k, v in trans_cache.items()}
            for chunk in sliced_chunks:
                h = get_hash(chunk)
                original = reverse_trans_cache.get(chunk, chunk)
                stressed = stress_cache.get(h, chunk)
                has_audio = os.path.exists(os.path.join(chunks_dir, f"{h}.wav"))
                
                paragraphs.append({
                    "hash": h,
                    "original": original,
                    "translated": chunk,
                    "stressed": stressed,
                    "has_audio": has_audio
                })
        except Exception as e:
            return jsonify({"status": "error", "message": f"Error resolving chunks: {e}"}), 500
            
    # Detect EPUB availability and cache stats
    epub_path = find_book_epub(paths["book_dir"], slug)
    epub_available = epub_path is not None and os.path.exists(epub_path)

    # Count translated blocks from cache
    cache_size = len(trans_cache)
    is_epub_book = cfg.get("pdf_path", "") == "" and epub_available

    # Check epub_progress for live stats
    epub_progress = {}
    epub_progress_path = os.path.join(paths["cache_dir"], "epub_progress.json")
    if os.path.exists(epub_progress_path):
        try:
            with open(epub_progress_path, "r", encoding="utf-8") as f:
                epub_progress = json.load(f)
        except Exception:
            pass

    return jsonify({
        "status": "success",
        "tts_engine": tts_engine,
        "paragraphs": paragraphs,
        "total_chunks": total_chunks,
        "total_pages": total_pages,
        "page": page,
        "limit": limit,
        "epub_available": epub_available,
        "is_epub_book": is_epub_book,
        "cache_stats": {
            "translated_blocks": cache_size,
            "current_file": epub_progress.get("current_file", 0),
            "total_files": epub_progress.get("total_files", 0),
            "percent": epub_progress.get("percent", 0),
            "completed_blocks": epub_progress.get("completed_blocks", 0),
            "total_blocks": epub_progress.get("total_blocks", 0)
        }
    })

def _tts_voice_slug_and_model(paths, repo_dir):
    tts_engine = paths.get("tts_engine", "supertonic3")
    if tts_engine == "styletts2":
        return "styletts2", os.path.join(repo_dir, "models", "styletts2", "model.onnx")
    return "supertonic-3-tts-int8", ""

# -------------------------------------------------------------
# POINT-EDIT ROUTES (text/audio) — see EDIT_METHODOLOGY_kindle-butch-gen.md
# Non-destructive overlay via edit_store.py. Approve is the only step that
# touches generated artifacts (translate_cache/merged markdown/batch files).
# No automatic full-book re-pass — see TASK-17 for why that was removed.
# -------------------------------------------------------------

@app.route("/api/edit/text/<slug>/<chunk_hash>", methods=["PUT"])
def edit_text(slug, chunk_hash):
    if not validate_slug(slug) or not re.match(r"^[a-f0-9]{64}$", chunk_hash):
        return jsonify({"status": "error", "message": "Invalid parameters"}), 400

    data = request.get_json() or {}
    original_text = data.get("original_text", "")
    new_text = data.get("new_text", "").strip()
    if not original_text or not new_text:
        return jsonify({"status": "error", "message": "original_text and new_text are required"}), 400

    import hashlib
    if hashlib.sha256(original_text.encode("utf-8")).hexdigest() != chunk_hash:
        return jsonify({"status": "error", "message": "original_text does not match chunk_hash — data is stale, refresh and retry"}), 409

    edit = edit_store.add_edit(slug, mode="text", target_id=chunk_hash, field="translated_text",
                                original_value=original_text, edited_value=new_text)
    return jsonify({"status": "success", "edit": edit})

@app.route("/api/edit/stress/<slug>/<chunk_hash>", methods=["PUT"])
def edit_stress(slug, chunk_hash):
    if not validate_slug(slug) or not re.match(r"^[a-f0-9]{64}$", chunk_hash):
        return jsonify({"status": "error", "message": "Invalid parameters"}), 400

    data = request.get_json() or {}
    original_stress = data.get("original_stress", "")
    new_stress = data.get("new_stress", "").strip()
    if not new_stress:
        return jsonify({"status": "error", "message": "new_stress is required"}), 400

    edit = edit_store.add_edit(slug, mode="stress", target_id=chunk_hash, field="stress",
                                original_value=original_stress, edited_value=new_stress)

    # Auto-resolve ASR quality flag if it exists for this chunk
    paths = resolve_book_paths(repo_dir, slug)
    asr_queue_path = os.path.join(paths["book_dir"], "asr_stress_queue.json")
    if os.path.exists(asr_queue_path):
        try:
            with open(asr_queue_path, "r", encoding="utf-8") as f:
                flags = json.load(f)
            new_flags = [flag for flag in flags if flag.get("chunk_id") != chunk_hash]
            tmp_path = asr_queue_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(new_flags, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, asr_queue_path)
        except Exception:
            pass

    return jsonify({"status": "success", "edit": edit})

@app.route("/api/edit/queue/<slug>")
def edit_queue(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug format"}), 400
    mode = request.args.get("mode")
    status = request.args.get("status")
    return jsonify(edit_store.list_edits(slug, mode=mode, status=status))

@app.route("/api/edit/regenerate-audio/<slug>/<chunk_hash>", methods=["POST"])
def edit_regenerate_audio(slug, chunk_hash):
    if not validate_slug(slug) or not re.match(r"^[a-f0-9]{64}$", chunk_hash):
        return jsonify({"status": "error", "message": "Invalid parameters"}), 400

    paths = resolve_book_paths(repo_dir, slug)
    if not os.path.exists(paths["config_path"]):
        return jsonify({"status": "error", "message": "Book not found"}), 404

    pending = edit_store.list_edits(slug, status="pending")
    stress_edit = next((e for e in pending if e["target_id"] == chunk_hash and e["mode"] == "stress"), None)
    text_edit = next((e for e in pending if e["target_id"] == chunk_hash and e["mode"] == "text"), None)

    if stress_edit:
        # Already manually stress-marked by the user — synthesize as-is.
        tts_text = stress_edit["edited_value"]
        source_edit = stress_edit
    elif text_edit:
        tts_text = text_edit["edited_value"]
        source_edit = text_edit
        target_lang = paths.get("target_lang", "uk")
        if target_lang == "uk":
            try:
                cmd_stress = [
                    "proot-distro", "login", "ubuntu", "--",
                    "python3", os.path.join(repo_dir, "bin", "stressify_batch.py"),
                    "--inline", tts_text
                ]
                res_stress = subprocess.run(cmd_stress, capture_output=True, text=True, timeout=15)
                if res_stress.returncode == 0 and res_stress.stdout.strip():
                    tts_text = res_stress.stdout.strip()
            except Exception as e:
                print(f"Warning: inline stressifier failed during regenerate-audio: {e}", file=sys.stderr)
    else:
        return jsonify({"status": "error", "message": "Не знайдено редагування для цього фрагмента — спочатку збережіть редагування"}), 400

    import hashlib
    new_hash = hashlib.sha256(tts_text.encode("utf-8")).hexdigest()

    voice_slug, model_path = _tts_voice_slug_and_model(paths, repo_dir)
    chunks_dir = os.path.join(paths["audio_dir"], f"chunks_{voice_slug}")
    cache_path = os.path.join(paths["cache_dir"], f"tts_cache_{voice_slug}.json")
    os.makedirs(chunks_dir, exist_ok=True)
    os.makedirs(paths["cache_dir"], exist_ok=True)

    # TASK-23: don't fire a second tts_helper.py (a second loaded TTS model)
    # if audio_stage.py is already running for this book — its own
    # tts_helper.py loop checks this file between chunks and picks new
    # entries up using the already-loaded model instead.
    if is_book_process_running(slug):
        audio_priority_path = os.path.join(paths["audio_dir"], f"audio_priority_{voice_slug}.json")
        os.makedirs(paths["audio_dir"], exist_ok=True)
        with file_lock(audio_priority_path, timeout=2.0):
            queued = []
            if os.path.exists(audio_priority_path):
                try:
                    with open(audio_priority_path, "r", encoding="utf-8") as f:
                        queued = json.load(f)
                except Exception:
                    queued = []
            if not any(q.get("hash") == new_hash for q in queued):
                queued.append({"hash": new_hash, "text": tts_text, "edit_id": source_edit["id"]})
            with open(audio_priority_path, "w", encoding="utf-8") as f:
                json.dump(queued, f, ensure_ascii=False, indent=2)
        return jsonify({
            "status": "queued",
            "message": "Конвертація для цієї книги зараз триває — ваші зміни збережені та будуть синтезовані автоматично."
        })

    tts_engine = paths.get("tts_engine", "supertonic3")
    payload = {
        "tts_engine": tts_engine,
        "model_path": model_path,
        "output_dir": os.path.abspath(chunks_dir),
        "cache_path": os.path.abspath(cache_path),
        "chunks": [{"hash": new_hash, "text": tts_text}],
        "speaker_id": paths.get("tts_speaker_id", 2),
        "speed": paths.get("tts_speed", 1.0),
        "lang": paths.get("target_lang", "uk")
    }
    if tts_engine == "styletts2":
        payload["voice_quality"] = paths.get("tts_voice_quality", "x_low")
        payload["noise_scale"] = paths.get("tts_noise_scale", 0.667)
        payload["noise_w"] = paths.get("tts_noise_w", 0.8)
    helper_path = os.path.join(repo_dir, "bin", "tts_helper.py")
    res = subprocess.run([sys.executable, helper_path], input=json.dumps(payload, ensure_ascii=False),
                          capture_output=True, text=True)
    if res.returncode != 0:
        return jsonify({"status": "error", "message": f"Synthesis failed: {res.stderr}"}), 500

    new_wav_path = os.path.join(chunks_dir, f"{new_hash}.wav")
    if not os.path.exists(new_wav_path):
        return jsonify({"status": "error", "message": "Synthesis reported success but no wav file was produced"}), 500

    edit_store.mark_status(slug, source_edit["id"], "regenerated")
    return jsonify({"status": "success", "new_hash": new_hash})

@app.route("/api/edit/approve/<slug>/<edit_id>", methods=["POST"])
def edit_approve(slug, edit_id):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug format"}), 400

    paths = resolve_book_paths(repo_dir, slug)
    edit = edit_store.get_edit(slug, edit_id)
    if not edit:
        return jsonify({"status": "error", "message": "Edit not found"}), 404
    if edit["status"] == "approved":
        return jsonify({"status": "error", "message": "Edit already approved"}), 400

    if edit["mode"] == "stress":
        target_lang = paths.get("target_lang", "uk")
        stress_cache_path = os.path.join(paths["book_dir"], "translated", f"stress_cache_{target_lang}.json")
        stress_cache = {}
        if os.path.exists(stress_cache_path):
            try:
                with open(stress_cache_path, "r", encoding="utf-8") as f:
                    stress_cache = json.load(f)
            except Exception:
                pass
        stress_cache[edit["target_id"]] = edit["edited_value"]
        os.makedirs(os.path.dirname(stress_cache_path), exist_ok=True)
        with open(stress_cache_path, "w", encoding="utf-8") as f:
            json.dump(stress_cache, f, ensure_ascii=False, indent=2)

    elif edit["mode"] == "text":
        old_text = edit["original_value"]
        new_text = edit["edited_value"]

        # 1. translate_cache.json — keyed by hash of the ORIGINAL SOURCE
        # (RU/EN) segment, not the translated chunk, so we can't look it up
        # by re-hashing old_text. Segments (translate_stage.py, ~1200 chars)
        # and TTS chunks (~150-1000 chars) are also different granularities,
        # so a chunk isn't guaranteed to equal a full cached segment value.
        # Best-effort: patch by exact value match if found; if not, skip —
        # this only affects a full re-translate (rare, opt-in), not what's
        # actually served (steps 2/3 below cover that).
        if os.path.exists(paths["translate_cache"]):
            try:
                with open(paths["translate_cache"], "r", encoding="utf-8") as f:
                    trans_cache = json.load(f)
            except Exception:
                trans_cache = {}
        else:
            trans_cache = {}
        cache_patched = False
        for seg_hash, seg_translation in trans_cache.items():
            if seg_translation == old_text:
                trans_cache[seg_hash] = new_text
                cache_patched = True
                break
        if cache_patched:
            os.makedirs(paths["cache_dir"], exist_ok=True)
            with open(paths["translate_cache"], "w", encoding="utf-8") as f:
                json.dump(trans_cache, f, ensure_ascii=False, indent=2)

        # 2. merged_translated_<lang>.md — the canonical file everything
        # (preview, audio_stage.py, ebook-convert on the no-PDF resume path)
        # reads from. Reconstruct via the exact same chunking the preview
        # endpoint uses rather than a blind substring replace on raw markdown.
        target_lang = paths.get("target_lang", "uk")
        source_lang = paths.get("source_lang", "ru")
        suffix = f"_translated_{target_lang}" if (target_lang != source_lang) else ""
        if suffix:
            target_md_file = os.path.join(paths["translated_dir"], f"merged_translated_{target_lang}.md")
        else:
            target_md_file = os.path.join(paths["translated_dir"], f"merged_source_{source_lang}.md")
        merged_patched = False
        if os.path.exists(target_md_file):
            with open(target_md_file, "r", encoding="utf-8") as f:
                content = f.read()
            if old_text in content:
                content = content.replace(old_text, new_text, 1)
                with open(target_md_file, "w", encoding="utf-8") as f:
                    f.write(content)
                merged_patched = True

        # 3. Per-batch translated markdown files — books that still have
        # their source PDF get merged_translated_<lang>.md unconditionally
        # rebuilt from these on the next conversion run, which would
        # silently discard step 2's patch otherwise. Locked (TASK-23):
        # the main pipeline may still be writing other batch files
        # concurrently if this book is still status=running.
        batch_patched = patch_batch_translation(paths["batches_dir"], suffix, old_text, new_text)

        if not merged_patched and not batch_patched:
            return jsonify({"status": "error", "message": "Could not locate the original text in merged markdown or any batch file — approve aborted, nothing was changed"}), 500

    # mode == "manga" intentionally falls through with no file mutation
    # here: regenerate-manga-page already bakes the edit into the actual
    # page image (a manga edit only affects pixels, unlike text/stress
    # which patch a shared cache/markdown file), so approving one is just
    # a reviewer confirming the regenerated result, not a data write.

    edit_store.mark_status(slug, edit_id, "approved", applied_at=datetime.now().isoformat())

    # TASK-65: translation-memory record for APPROVED text edits only.
    # Agent proposals must never contaminate the memory before a human
    # confirms them - so this hook fires exclusively on approve, never on
    # add. Local JSONL always; MemPalace POST is best-effort and only
    # when KBG_MEMPALACE_URL is configured (the phone may have no route
    # to it - must never block or fail the approve itself).
    if edit.get("field") == "translated_text":
        tm_record = {
            "slug": slug, "target_id": edit["target_id"],
            "original": edit["original_value"], "approved": edit["edited_value"],
            "source": edit.get("source", "human"),
            "approved_at": datetime.now().isoformat(),
        }
        try:
            tm_path = os.path.join(paths["book_dir"], "edits", "approved_tm.jsonl")
            os.makedirs(os.path.dirname(tm_path), exist_ok=True)
            with open(tm_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(tm_record, ensure_ascii=False) + "\n")
        except OSError:
            pass
        mp_url = os.environ.get("KBG_MEMPALACE_URL")
        if mp_url:
            try:
                import requests
                requests.post(mp_url.rstrip("/") + "/api/tm", json=tm_record, timeout=5)
            except Exception:
                pass

    return jsonify({"status": "success"})

@app.route("/api/edit/discard/<slug>/<edit_id>", methods=["POST"])
def edit_discard(slug, edit_id):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug format"}), 400
    edit = edit_store.get_edit(slug, edit_id)
    if not edit:
        return jsonify({"status": "error", "message": "Edit not found"}), 404
    edit_store.mark_status(slug, edit_id, "discarded")
    return jsonify({"status": "success"})

@app.route("/api/edit/stress/discard/<slug>/<chunk_hash>", methods=["POST"])
def edit_stress_discard(slug, chunk_hash):
    if not validate_slug(slug) or not re.match(r"^[a-f0-9]{64}$", chunk_hash):
        return jsonify({"status": "error", "message": "Invalid parameters"}), 400
    
    paths = resolve_book_paths(repo_dir, slug)
    asr_queue_path = os.path.join(paths["book_dir"], "asr_stress_queue.json")
    if os.path.exists(asr_queue_path):
        try:
            with open(asr_queue_path, "r", encoding="utf-8") as f:
                flags = json.load(f)
            new_flags = [flag for flag in flags if flag.get("chunk_id") != chunk_hash]
            tmp_path = asr_queue_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(new_flags, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, asr_queue_path)
        except Exception as e:
            return jsonify({"status": "error", "message": f"Failed to modify ASR queue: {e}"}), 500
            
    return jsonify({"status": "success"})

@app.route("/api/edit/manga-text/<slug>/<page_filename>", methods=["PUT"])
def edit_manga_text(slug, page_filename):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug format"}), 400

    data = request.get_json() or {}
    bubble_id = data.get("bubble_id", "").strip()
    new_text = data.get("translated_text", "").strip()
    if not bubble_id or not new_text:
        return jsonify({"status": "error", "message": "bubble_id and translated_text are required"}), 400

    page_filename = os.path.basename(page_filename)
    page_stem = os.path.splitext(page_filename)[0]
    paths = resolve_book_paths(repo_dir, slug)
    meta_path = os.path.join(paths["book_dir"], "bubbles_meta", f"{page_stem}.json")
    if not os.path.exists(meta_path):
        return jsonify({"status": "error", "message": "Page not processed yet - no bubble metadata found"}), 404

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            bubbles = json.load(f)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to read bubble metadata: {e}"}), 500

    bubble = next((b for b in bubbles if b["id"] == bubble_id), None)
    if not bubble:
        return jsonify({"status": "error", "message": f"Bubble '{bubble_id}' not found on this page"}), 404

    source = data.get("source", "human")
    if source not in ("human", "gemma_agent"):
        return jsonify({"status": "error", "message": "source must be 'human' or 'gemma_agent'"}), 400
    note = (data.get("note") or "")[:500] or None
    edit = edit_store.add_edit(slug, mode="manga", target_id=f"{page_filename}#{bubble_id}",
                                field="translated_text", original_value=bubble["translated_text"],
                                edited_value=new_text, source=source, note=note)
    return jsonify({"status": "success", "edit": edit})

@app.route("/api/edit/manga-bbox/<slug>/<page_filename>", methods=["PUT"])
def edit_manga_bbox(slug, page_filename):
    # TASK-36: manual geometry/font-size override, independent of and
    # separate-Save-button from edit_manga_text above - a human can fix
    # only the box, only the font size, or both, without touching the
    # translated text itself.
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug format"}), 400

    data = request.get_json() or {}
    bubble_id = data.get("bubble_id", "").strip()
    bbox = data.get("bbox")
    ref_size = data.get("ref_size")
    font_size = data.get("font_size")

    if not bubble_id:
        return jsonify({"status": "error", "message": "bubble_id is required"}), 400
    if bbox is None and font_size is None:
        return jsonify({"status": "error", "message": "at least one of bbox or font_size is required"}), 400

    if bbox is not None:
        if not (isinstance(bbox, list) and len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox)):
            return jsonify({"status": "error", "message": "bbox must be [x1, y1, x2, y2]"}), 400
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return jsonify({"status": "error", "message": "bbox must have positive width and height"}), 400
        if not (isinstance(ref_size, list) and len(ref_size) == 2 and all(isinstance(v, (int, float)) for v in ref_size)):
            return jsonify({"status": "error", "message": "ref_size ([w, h]) is required when bbox is set"}), 400

    if font_size is not None:
        try:
            font_size = int(font_size)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "font_size must be an integer"}), 400
        if font_size < 8 or font_size > 200:
            return jsonify({"status": "error", "message": "font_size out of range (8-200)"}), 400

    page_filename = os.path.basename(page_filename)
    page_stem = os.path.splitext(page_filename)[0]
    paths = resolve_book_paths(repo_dir, slug)
    meta_path = os.path.join(paths["book_dir"], "bubbles_meta", f"{page_stem}.json")
    if not os.path.exists(meta_path):
        return jsonify({"status": "error", "message": "Page not processed yet - no bubble metadata found"}), 404

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            bubbles = json.load(f)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to read bubble metadata: {e}"}), 500

    bubble = next((b for b in bubbles if b["id"] == bubble_id), None)
    if not bubble:
        return jsonify({"status": "error", "message": f"Bubble '{bubble_id}' not found on this page"}), 404

    original_value = {"bbox": bubble.get("bbox"), "ref_size": bubble.get("bbox_ref_size")}
    edited_value = {}
    if bbox is not None:
        edited_value["bbox"] = [int(v) for v in bbox]
        edited_value["ref_size"] = [int(v) for v in ref_size]
    if font_size is not None:
        edited_value["font_size"] = font_size

    source = data.get("source", "human")
    if source not in ("human", "gemma_agent"):
        return jsonify({"status": "error", "message": "source must be 'human' or 'gemma_agent'"}), 400
    note = (data.get("note") or "")[:500] or None
    edit = edit_store.add_edit(slug, mode="manga", target_id=f"{page_filename}#{bubble_id}",
                                field="manual_bbox_override", original_value=original_value,
                                edited_value=edited_value, source=source, note=note)
    return jsonify({"status": "success", "edit": edit})

@app.route("/api/edit/regenerate-manga-page/<slug>/<page_filename>", methods=["POST"])
def edit_regenerate_manga_page(slug, page_filename):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug format"}), 400

    page_filename = os.path.basename(page_filename)
    paths = resolve_book_paths(repo_dir, slug)
    config_path = paths["config_path"]
    if not os.path.exists(config_path):
        return jsonify({"status": "error", "message": "Book not found"}), 404
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("is_manga", False):
        return jsonify({"status": "error", "message": "Not a manga"}), 400

    _h = _heavy_state()
    if _h["agent"]:
        return _busy_409("🤖 ШІ-агент зараз аналізує сторінки. Зупиніть його у вкладці «Агент» або зачекайте — тоді регенеруйте сторінку.")
    if _h["ner"]:
        return _busy_409("🔎 Йде сканування персонажів. Зачекайте кілька хвилин і повторіть.")

    # TASK-23: don't fire a second translate_manga.py process (same GPU/OCR/
    # LLM resources) if the main pipeline is still running for this book -
    # main()'s own per-page loop now checks pending edits at each page
    # boundary and will pick this one up automatically.
    if is_book_process_running(slug):
        return jsonify({
            "status": "queued",
            "message": "Generation is currently in progress for this book - your edit is saved and will be applied automatically as the pipeline reaches this page."
        })

    # Same source resolution run_conversion_api already uses for the full
    # manga run - --regenerate-page re-extracts from this same source, so
    # directory/CBZ/PDF sources all work uniformly with no special-casing.
    manga_input = ""
    if os.path.isdir(os.path.join(paths["book_dir"], "source")):
        manga_input = os.path.join(paths["book_dir"], "source")
    else:
        for possible_ext in [".cbz", ".cbr", ".cb7", ".zip", ".rar", ".pdf", ".epub"]:
            if os.path.exists(os.path.join(paths["book_dir"], f"{slug}{possible_ext}")):
                manga_input = os.path.join(paths["book_dir"], f"{slug}{possible_ext}")
                break
    if not manga_input:
        return jsonify({"status": "error", "message": "Manga source file or directory not found"}), 400

    target_lang = cfg.get("target_lang", "uk")
    manga_output = os.path.join(paths["book_dir"], "output", f"{slug}_translated_{target_lang}.cbz")

    prefix = f"{page_filename}#"
    page_edits = [e for e in edit_store.list_edits(slug, mode="manga") if e["target_id"].startswith(prefix)]
    # TASK-65 UX fix (found via Q approving before regenerating): edits
    # approved while still status=pending never got baked - to_apply
    # below only carried pending+regenerated, and this gate refused to
    # run at all once everything on the page was approved. Approved
    # edits are confirmed human intent - they must both allow a regen
    # and ride along in it.
    actionable = [e for e in page_edits if e.get("status") in ("pending", "approved")]
    if not actionable:
        return jsonify({"status": "error", "message": "No pending or approved edits for this page - nothing to regenerate"}), 400

    # Bug found live during TASK-36 testing: process_page() re-runs the
    # WHOLE page from scratch on every regen (fresh OCR + fresh LLM
    # translation), so an edit that's already "regenerated" from a PAST
    # run is invisible to a LATER regen triggered for an unrelated reason
    # (e.g. a geometry-only fix on a different bubble) unless it's
    # included again here too - otherwise it silently reverts to a fresh
    # (and possibly different) auto-translation. "regenerated" edits are
    # therefore included in the override set on every future regen of
    # this page, not just the run they were first created in - the
    # pending-only check above still gates WHETHER a regen even happens
    # (so clicking Regenerate on a page with zero new edits does nothing),
    # but once triggered, every previously-confirmed fix for this page
    # rides along. "orphaned"/"discarded" edits are excluded - they no
    # longer correspond to anything meaningful to reapply.
    to_apply = [e for e in page_edits if e.get("status") in ("pending", "regenerated", "approved")]

    page_stem = os.path.splitext(page_filename)[0]
    meta_path = os.path.join(paths["book_dir"], "bubbles_meta", f"{page_stem}.json")
    bubbles_by_id = {}
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                bubbles_by_id = {b["id"]: b for b in json.load(f)}
        except Exception:
            bubbles_by_id = {}

    overrides = {}
    bbox_overrides = {}
    for e in to_apply:
        bubble_id = e["target_id"].split("#", 1)[1]
        bubble = bubbles_by_id.get(bubble_id)
        if not bubble:
            continue
        orig_text = bubble["original_text"]
        if e.get("field") == "translated_text":
            overrides[orig_text] = e["edited_value"]
        elif e.get("field") == "manual_bbox_override":
            # TASK-36: bbox/font_size overrides are captured by the client
            # against whatever image dimensions were on screen at edit
            # time (ref_size) - kept UNSCALED here and scaled later inside
            # translate_manga.py once the actual regen's working image
            # dimensions are known (same reference-scaling approach TASK-27
            # established), not here where that size isn't available yet.
            ev = e.get("edited_value") or {}
            entry = bbox_overrides.setdefault(orig_text, {})
            if "bbox" in ev and "ref_size" in ev:
                entry["bbox"] = ev["bbox"]
                entry["ref_size"] = ev["ref_size"]
            if "font_size" in ev:
                entry["font_size"] = ev["font_size"]
                entry.setdefault("ref_size", ev.get("ref_size"))

    overrides_path = os.path.join(paths["cache_dir"], f"manga_regen_overrides_{page_stem}.json")
    bbox_overrides_path = os.path.join(paths["cache_dir"], f"manga_regen_bbox_overrides_{page_stem}.json")
    os.makedirs(paths["cache_dir"], exist_ok=True)
    with open(overrides_path, "w", encoding="utf-8") as f:
        json.dump(overrides, f, ensure_ascii=False)
    with open(bbox_overrides_path, "w", encoding="utf-8") as f:
        json.dump(bbox_overrides, f, ensure_ascii=False)

    cmd = [
        "proot-distro", "login", "ubuntu", "--",
        "python3", "-u", os.path.join(repo_dir, "translate_manga.py"),
        "--input", manga_input,
        "--output", manga_output,
        "--lang", cfg.get("source_lang", "en"),
        "--regenerate-page", page_filename,
        "--overrides-json", overrides_path,
        "--bbox-overrides-json", bbox_overrides_path
    ]
    glossary_path = os.path.join(paths["book_dir"], "glossary.json")
    if os.path.exists(glossary_path):
        cmd.extend(["--glossary", glossary_path])

    try:
        # Single-page regen (detector init + a few OCR/LLM calls) is fast
        # enough to run synchronously - the UI shows a spinner rather than
        # needing a background-job+poll flow, per the doc's own framing.
        #
        # start_new_session=True (setsid) puts the whole proot -> bash ->
        # python3 chain in its own process group, so a timeout can kill the
        # ENTIRE tree via os.killpg. Plain subprocess.run(..., timeout=...)'s
        # default TimeoutExpired handling only kills the direct child (the
        # proot wrapper itself) - confirmed live during TASK-36 testing that
        # this left the python3 translate_manga.py process it exec'd running
        # ORPHANED inside proot-distro for several more minutes after Flask
        # (and the client) had already given up, wasting phone battery/CPU
        # with nothing watching it. It happened to complete correctly on its
        # own both times this was observed, but that was luck, not a
        # guarantee - a truly hung regen would run forever unmonitored.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.communicate()  # reap the now-killed process, avoid a zombie
            raise
        res = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "Regeneration timed out after 180s"}), 500
    finally:
        try:
            os.remove(overrides_path)
        except Exception:
            pass
        try:
            os.remove(bbox_overrides_path)
        except Exception:
            pass

    if res.returncode != 0:
        return jsonify({"status": "error", "message": f"Regeneration failed: {res.stderr[-2000:]}"}), 500

    # translate_manga.py prints exactly one JSON line to stdout on success.
    result_line = None
    for line in res.stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("{"):
            result_line = line
    if not result_line:
        return jsonify({"status": "error", "message": f"Regeneration completed but produced no result JSON: {res.stdout[-1000:]}"}), 500

    try:
        result = json.loads(result_line)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to parse regeneration result: {e}"}), 500

    if result.get("status") != "success":
        return jsonify({"status": "error", "message": result.get("message", "Regeneration failed")}), 500

    id_mapping = result.get("bubble_id_mapping", {})
    now = datetime.now().isoformat()
    for e in to_apply:
        old_bubble_id = e["target_id"].split("#", 1)[1]
        new_bubble_id = id_mapping.get(old_bubble_id)
        if new_bubble_id is not None:
            # Re-marking an already-"regenerated" edit here too (fresh
            # applied_at) - it just rode along in this regen, still
            # correctly applied, timestamp reflects the latest confirmation.
            edit_store.mark_status(slug, e["id"], "regenerated", applied_at=now)
        else:
            # The bubble this edit targeted has no confident IoU match in
            # the fresh detection - don't silently drop the edit, surface
            # it for a human to resolve.
            edit_store.mark_status(slug, e["id"], "orphaned")

    return jsonify({"status": "success", "bubbles": result.get("bubbles", []), "bubble_id_mapping": id_mapping})

def find_book_epub(book_dir, slug):
    import os
    import glob
    candidate = os.path.join(book_dir, f"{slug}.epub")
    if os.path.exists(candidate):
        return candidate
    epubs = glob.glob(os.path.join(book_dir, "*.epub"))
    if epubs:
        return epubs[0]
    epubs_input = glob.glob(os.path.join(book_dir, "input", "*.epub"))
    if epubs_input:
        return epubs_input[0]
    epubs_output = glob.glob(os.path.join(book_dir, "output", "*.epub"))
    if epubs_output:
        return epubs_output[0]
    return None

@app.route("/api/preview/book-chapters/<slug>")
def preview_book_chapters(slug):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
        
    import zipfile
    import xml.etree.ElementTree as ET
    
    paths = resolve_book_paths(repo_dir, slug)
    epub_path = find_book_epub(paths["book_dir"], slug)
    if not epub_path or not os.path.exists(epub_path):
        return jsonify({"status": "error", "message": f"EPUB file not found for book '{slug}'"}), 404
        
    try:
        with zipfile.ZipFile(epub_path, 'r') as z:
            container_data = z.read("META-INF/container.xml")
            root = ET.fromstring(container_data)
            root_file_el = root.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
            if root_file_el is None:
                root_file_el = root.find(".//rootfile")
            if root_file_el is None:
                return jsonify({"status": "error", "message": "rootfile not found in container.xml"}), 400
            opf_rel_path = root_file_el.attrib["full-path"]
            opf_dir = os.path.dirname(opf_rel_path)
            
            opf_data = z.read(opf_rel_path)
            opf_root = ET.fromstring(opf_data)
            
            manifest_el = opf_root.find(".//{http://www.idpf.org/2007/opf}manifest")
            if manifest_el is None:
                manifest_el = opf_root.find(".//manifest")
            if manifest_el is None:
                return jsonify({"status": "error", "message": "manifest not found in OPF"}), 400
                
            chapters = []
            for item in manifest_el.findall(".//{http://www.idpf.org/2007/opf}item"):
                href = item.attrib.get("href")
                media_type = item.attrib.get("media-type", "")
                if href and media_type in ["application/xhtml+xml", "text/html"]:
                    chapters.append({
                        "href": href,
                        "id": item.attrib.get("id", "")
                    })
            return jsonify({
                "status": "success",
                "chapters": chapters,
                "opf_dir": opf_dir
            })
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to read EPUB chapters: {e}"}), 500

@app.route("/api/preview/book-page/<slug>/<path:href>")
def preview_book_page(slug, href):
    if not validate_slug(slug):
        return jsonify({"status": "error", "message": "Invalid slug"}), 400
        
    import os
    import json
    import zipfile
    import hashlib
    import re
    import xml.etree.ElementTree as ET
    from common.epub_validate import sanitize_xhtml_for_xml_parser
    
    paths = resolve_book_paths(repo_dir, slug)
    epub_path = find_book_epub(paths["book_dir"], slug)
    if not epub_path or not os.path.exists(epub_path):
        return jsonify({"status": "error", "message": "EPUB file not found"}), 404
        
    # Serve binary assets (images, css) directly from EPUB
    lower_href = href.lower()
    if lower_href.endswith(('.jpg', '.jpeg', '.png', '.gif', '.svg', '.css')):
        try:
            with zipfile.ZipFile(epub_path, 'r') as z:
                container_data = z.read("META-INF/container.xml")
                c_root = ET.fromstring(container_data)
                rf_el = c_root.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
                if rf_el is None:
                    rf_el = c_root.find(".//rootfile")
                opf_rel_path = rf_el.attrib["full-path"]
                opf_dir = os.path.dirname(opf_rel_path)
                
                full_rel_path = os.path.join(opf_dir, href) if opf_dir else href
                full_rel_path = full_rel_path.replace("\\", "/")
                
                raw_bytes = z.read(full_rel_path)
                
                content_type = "application/octet-stream"
                if lower_href.endswith(('.jpg', '.jpeg')):
                    content_type = "image/jpeg"
                elif lower_href.endswith('.png'):
                    content_type = "image/png"
                elif lower_href.endswith('.gif'):
                    content_type = "image/gif"
                elif lower_href.endswith('.svg'):
                    content_type = "image/svg+xml"
                elif lower_href.endswith('.css'):
                    content_type = "text/css"
                
                from flask import Response
                return Response(raw_bytes, mimetype=content_type)
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 404

    opf_dir = ""
    try:
        with zipfile.ZipFile(epub_path, 'r') as z:
            container_data = z.read("META-INF/container.xml")
            c_root = ET.fromstring(container_data)
            rf_el = c_root.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
            if rf_el is None:
                rf_el = c_root.find(".//rootfile")
            opf_rel_path = rf_el.attrib["full-path"]
            opf_dir = os.path.dirname(opf_rel_path)
            
            full_rel_path = os.path.join(opf_dir, href) if opf_dir else href
            full_rel_path = full_rel_path.replace("\\", "/")
            
            try:
                raw_bytes = z.read(full_rel_path)
            except KeyError:
                return jsonify({"status": "error", "message": f"File {full_rel_path} not found in EPUB"}), 404
                
        sanitized = sanitize_xhtml_for_xml_parser(raw_bytes)
        ET.register_namespace('', 'http://www.w3.org/1999/xhtml')
        
        cache = {}
        if os.path.exists(paths["translate_cache"]):
            try:
                with open(paths["translate_cache"], "r", encoding="utf-8") as cf:
                    cache = json.load(cf)
            except Exception:
                pass
                
        orig_root = ET.fromstring(sanitized.encode('utf-8'))
        trans_root = ET.fromstring(sanitized.encode('utf-8'))
        
        block_tags = [
            "{http://www.w3.org/1999/xhtml}p", "{http://www.w3.org/1999/xhtml}li",
            "{http://www.w3.org/1999/xhtml}h1", "{http://www.w3.org/1999/xhtml}h2",
            "{http://www.w3.org/1999/xhtml}h3", "{http://www.w3.org/1999/xhtml}h4",
            "{http://www.w3.org/1999/xhtml}h5", "{http://www.w3.org/1999/xhtml}h6",
            "{http://www.w3.org/1999/xhtml}blockquote", "{http://www.w3.org/1999/xhtml}td",
            "{http://www.w3.org/1999/xhtml}th",
            "p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "td", "th"
        ]
        
        for el in trans_root.iter():
            if el.tag in block_tags:
                text = el.text or ''
                children_str = ''.join(ET.tostring(child, encoding='utf-8').decode('utf-8') for child in el)
                inner_xml = text + children_str
                
                if not inner_xml.strip():
                    continue
                    
                h = hashlib.sha256(inner_xml.encode('utf-8')).hexdigest()
                if h in cache:
                    translated_inner_xml = cache[h]
                    dummy_xml = f'<div xmlns="http://www.w3.org/1999/xhtml">{translated_inner_xml}</div>'
                    try:
                        sanitized_dummy = sanitize_xhtml_for_xml_parser(dummy_xml.encode('utf-8'))
                        dummy_root = ET.fromstring(sanitized_dummy.encode('utf-8'))
                        el.text = None
                        el.tail = None
                        for child in list(el):
                            el.remove(child)
                        el.text = dummy_root.text
                        for child in dummy_root:
                            el.append(child)
                    except Exception:
                        plain_text = re.sub(r'<[^>]+>', '', translated_inner_xml)
                        el.text = None
                        el.tail = None
                        for child in list(el):
                            el.remove(child)
                        el.text = plain_text
                else:
                    existing_class = el.attrib.get("class", "")
                    el.attrib["class"] = (existing_class + " untranslated-block").strip()
                    
        orig_html = ET.tostring(orig_root, encoding='utf-8').decode('utf-8')
        trans_html = ET.tostring(trans_root, encoding='utf-8').decode('utf-8')
        
        style_inject = """
        <style>
            body {
                background-color: #09090b !important;
                color: #f4f4f5 !important;
                font-family: 'Outfit', system-ui, -apple-system, sans-serif !important;
                line-height: 1.65 !important;
                padding: 1.5rem !important;
                margin: 0 !important;
                font-size: 1.05rem !important;
            }
            p, li {
                margin-bottom: 1.2rem !important;
            }
            h1, h2, h3, h4, h5, h6 {
                color: #a78bfa !important;
                margin-top: 1.6rem !important;
                margin-bottom: 0.8rem !important;
                font-weight: 600 !important;
            }
            .untranslated-block {
                color: #71717a !important;
                border-left: 2px solid #d97706 !important;
                padding-left: 10px !important;
                background-color: rgba(217, 119, 6, 0.04) !important;
                border-radius: 0 4px 4px 0 !important;
            }
        </style>
        """
        
        base_tag = f'<base href="/api/preview/book-page/{slug}/{opf_dir}/" />' if opf_dir else f'<base href="/api/preview/book-page/{slug}/" />'
        style_inject_with_base = f"\n        {base_tag}\n" + style_inject
        
        def inject_style(html_str):
            if "</head>" in html_str:
                return html_str.replace("</head>", f"{style_inject_with_base}</head>")
            elif "<body>" in html_str:
                return html_str.replace("<body>", f"<body>{style_inject_with_base}")
            else:
                return style_inject_with_base + html_str
                
        orig_html = inject_style(orig_html)
        trans_html = inject_style(trans_html)
        
        def clean_prefixes(html_str):
            import re
            # Replace tags like <ns1:svg -> <svg, </ns1:svg -> </svg
            html_str = re.sub(r'</?ns\d+:', lambda m: '</' if m.group().startswith('</') else '<', html_str)
            # Replace attributes like ns2:href -> xlink:href or href
            html_str = re.sub(r'\bns\d+:href\b', 'xlink:href', html_str)
            # Remove namespace declarations for ns1, ns2
            html_str = re.sub(r'\s*xmlns:ns\d+=\"[^\"]*\"', '', html_str)
            return html_str
            
        orig_html = clean_prefixes(orig_html)
        trans_html = clean_prefixes(trans_html)
        
        return jsonify({
            "status": "success",
            "original_html": orig_html,
            "translated_html": trans_html
        })
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error loading book page: {e}"}), 500

@app.route("/downloads")
def downloads_page():
    return render_template("downloads.html")

@app.route("/api/downloads")
def api_all_downloads():
    import os
    import json
    all_files = []
    books_dir = os.path.join(repo_dir, "books")
    if not os.path.exists(books_dir):
        return jsonify([])
        
    for entry in os.listdir(books_dir):
        entry_path = os.path.join(books_dir, entry)
        if not os.path.isdir(entry_path) or entry.startswith('.'):
            continue
            
        config_path = os.path.join(entry_path, "config.json")
        title = entry
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    title = cfg.get("title", entry)
            except Exception:
                pass
                
        output_dir = os.path.join(entry_path, "output")
        if os.path.exists(output_dir):
            for f in os.listdir(output_dir):
                if f.endswith((".epub", ".azw3", ".mp3", ".md", ".cbz", ".cbr", ".cb7", ".zip")):
                    fpath = os.path.join(output_dir, f)
                    if os.path.isfile(fpath):
                        size_bytes = os.path.getsize(fpath)
                        if size_bytes >= 1024*1024:
                            size_str = f"{size_bytes / (1024*1024):.1f} MB"
                        else:
                            size_str = f"{size_bytes / 1024:.1f} KB"
                            
                        desc = "Скомпільований файл проекту."
                        target = "Будь-який пристрій"
                        fname_lower = f.lower()
                        
                        if fname_lower.endswith(".azw3"):
                            target = "Amazon Kindle"
                            if "translated" in fname_lower:
                                desc = "Перекладена книга/манга у форматі AZW3. Оптимізовано для рідерів Amazon Kindle (включаючи Paperwhite, Oasis, Scribe та Basic)."
                            else:
                                desc = "Книга/манга у форматі AZW3. Готова до завантаження на рідер Kindle."
                        elif fname_lower.endswith(".cbz"):
                            target = "Комікс-рідери / Планшети"
                            desc = "Перекладений комікс-архів (CBZ) з оригінальним роздільним дозволом. Підходить для перегляду на комп'ютерах, планшетах чи сторонніх читалках."
                        elif fname_lower.endswith(".epub"):
                            target = "Kobo, PocketBook, Apple Books, Android"
                            desc = "Перекладена електронна книга у стандартному форматі EPUB. Підходить для будь-яких пристроїв читання (окрім старих Kindle)."
                        elif fname_lower.endswith(".mp3"):
                            target = "Будь-який аудіоплеєр / Смартфон"
                            desc = "Синтезована аудіокнига у форматі MP3. Високоякісне озвучування розділів."
                        elif fname_lower.endswith(".md"):
                            target = "Текстовий редактор / Obsidian"
                            desc = "Текстовий файл у форматі Markdown. Містить чистий перекладений текст або розділи книги."
                            
                        all_files.append({
                            "slug": entry,
                            "book_title": title,
                            "filename": f,
                            "size": size_str,
                            "target": target,
                            "description": desc,
                            "download_url": f"/api/download/{entry}/{f}"
                        })
                        
    return jsonify(all_files)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="KBG Web Service Dashboard")
    parser.add_argument("--port", type=int, default=5000, help="Port to run the dashboard on (default: 5000)")
    parser.add_argument("--debug", action="store_true", help="Run in Flask debug mode")
    args = parser.parse_args()
    
    app.run(host="0.0.0.0", port=args.port, debug=args.debug, threaded=True)

