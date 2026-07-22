# [kindle-butch-gen] Generalization & Audiobook Extension Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refactor the `kindle-butch-gen` project from a single-book hardcoded codebase into a generalized multi-book translation and Kindle/audiobook publishing pipeline, complete with a bash CLI entry point (`kbg.sh`), an automated Piper TTS stage, and a Flask-based local web dashboard.

**Architecture:** 
1. **Directory Consolidation**: Move all scripts to `~/kindle-butch-gen/`, create `books/` subdirectory for multi-book configurations, and isolate helper logic under `common/`.
2. **Dynamic Configuration**: Adapt Python scripts to accept `--book <slug>` and read paths/metadata from `books/<slug>/config.json`.
3. **Piper TTS Pipeline**: Create `audio_stage.py` to synthesize speech using Piper, with paragraph chunking, SHA-256 caching, and M4B/MP3 output.
4. **Unified Controller (`kbg.sh`)**: Wrap all stages in a bash controller handling model downloads, llama-server lifecycle, and conversion flow.
5. **Flask Dashboard (`kbg_web/app.py`)**: A lightweight UI that interacts directly with `kbg.sh` via subprocess to display status, logs, and downloads.

**Tech Stack:** Python 3, Bash, Calibre (`ebook-convert`), Piper TTS (C++ standalone / ONNX), FFmpeg, Flask.

---

### Task 1: Reorganize Directory & Extract Helper Modules

**Files:**
- Create: `~/kindle-butch-gen/common/__init__.py`
- Create: `~/kindle-butch-gen/common/text_protect.py`
- Create: `~/kindle-butch-gen/common/epub_validate.py`
- Create: `~/kindle-butch-gen/docs/plans/2026-07-06-multibook-kindle-tts-web.md`

**Step 1: Create the directory structure**
Run:
```bash
mkdir -p ~/kindle-butch-gen/common
mkdir -p ~/kindle-butch-gen/books
mkdir -p ~/kindle-butch-gen/models/hy-mt2
mkdir -p ~/kindle-butch-gen/models/piper
mkdir -p ~/kindle-butch-gen/bin
mkdir -p ~/kindle-butch-gen/docs/plans
```

**Step 2: Create `common/text_protect.py` (Deduplicate `PlaceholderManager`)**
Write the following code to `~/kindle-butch-gen/common/text_protect.py` (combining the placeholder protection and the new robust normalization code):

```python
import re

class PlaceholderManager:
    def __init__(self):
        self.placeholders = {}
        self.counter = 0

    def add(self, text, prefix):
        key = f"__{prefix}_{self.counter}__"
        self.placeholders[key] = text
        self.counter += 1
        return key

    def protect(self, text):
        # 1. Code blocks
        def cb_repl(match):
            return self.add(match.group(0), "CODE_BLOCK")
        text = re.sub(r"```[\s\S]*?```", cb_repl, text)

        # 2. LaTeX blocks
        def math_block_repl(match):
            return self.add(match.group(0), "MATH_BLOCK")
        text = re.sub(r"\$\$[\s\S]*?\$\$", math_block_repl, text)

        # 3. LaTeX inline
        def math_inline_repl(match):
            return self.add(match.group(0), "MATH_INLINE")
        text = re.sub(r"\$[^\$\n]+?\$", math_inline_repl, text)

        # 4. Inline code
        def inline_code_repl(match):
            return self.add(match.group(0), "INLINE_CODE")
        text = re.sub(r"`[^`\n]+?`", inline_code_repl, text)

        # 5. Markdown link/image URLs or HTML link targets
        def link_url_repl(match):
            prefix = match.group(1)
            url = match.group(2)
            suffix = match.group(3)
            placeholder = self.add(url, "LINK_URL")
            return f"{prefix}{placeholder}{suffix}"
        text = re.sub(r"(\]\s*\()([^)]+)(\))", link_url_repl, text)

        # 6. Raw URLs
        def raw_url_repl(match):
            return self.add(match.group(0), "RAW_URL")
        text = re.sub(r"https?://[a-zA-Z0-9\-._~:/?#\[\]@!$&'*+,;=%]+", raw_url_repl, text)

        # 7. HTML/XML tags
        def html_tag_repl(match):
            return self.add(match.group(0), "HTML_TAG")
        text = re.sub(r"<[a-zA-Z/!][^>]*?>", html_tag_repl, text)

        return text

    def normalize_placeholders(self, text):
        if not text:
            return text
        prefixes = set()
        for key in self.placeholders.keys():
            match = re.match(r"^__([A-Z_]+?)_[0-9]+__$", key)
            if match:
                prefixes.add(match.group(1))
        
        for prefix in prefixes:
            flat_prefix = prefix.replace("_", "")
            pattern = re.compile(
                r'__(?:' + '|'.join([prefix, flat_prefix]) + r')[-_\s]*(\d+)\s*__',
                re.IGNORECASE
            )
            text = pattern.sub(rf'__{prefix}_\1__', text)
        return text

    def restore(self, text):
        text = self.normalize_placeholders(text)
        keys = list(self.placeholders.keys())
        keys.reverse()
        for key in keys:
            text = text.replace(key, self.placeholders[key])
        return text

    def strip_formatting(self, text):
        """Used for TTS: strips all Markdown and HTML formatting tags."""
        text = re.sub(r"```[\s\S]*?```", "", text) # Remove code blocks
        text = re.sub(r"`[^`\n]+?`", "", text)     # Remove inline code
        text = re.sub(r"<[^>]+>", "", text)         # Remove HTML tags
        text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text) # Keep link text, strip URLs
        text = re.sub(r"\$\$[\s\S]*?\$\$", "", text) # Remove LaTeX math blocks
        text = re.sub(r"\$[^\$\n]+?\$", "", text)     # Remove inline LaTeX
        return text
```

**Step 3: Create `common/epub_validate.py` (Deduplicate `validate_epub`)**
Write the following code to `~/kindle-butch-gen/common/epub_validate.py`:

```python
import os
import zipfile
import re
import xml.etree.ElementTree as ET

def sanitize_xhtml_for_xml_parser(xml_content_bytes):
    content_str = xml_content_bytes.decode('utf-8', errors='ignore')
    def replace_entity(match):
        entity = match.group(1)
        if entity in ['amp', 'lt', 'gt', 'quot', 'apos']:
            return match.group(0)
        replacements = {
            'nbsp': '&#160;',
            'mdash': '&#8212;',
            'ndash': '&#8211;',
            'copy': '&#169;',
            'hellip': '&#8230;',
            'ldquo': '&#8220;',
            'rdquo': '&#8221;',
            'lsquo': '&#8216;',
            'rsquo': '&#8217;',
            'middot': '&#183;',
            'sect': '&#167;',
            'bull': '&#8226;',
            'prime': '&#8242;',
            'Prime': '&#8243;'
        }
        if entity in replacements:
            return replacements[entity]
        if entity.startswith('#'):
            return match.group(0)
        return ' '
    sanitized = re.sub(r'&([a-zA-Z0-9#]+);', replace_entity, content_str)
    return sanitized

def validate_epub(epub_path, log_func=print):
    log_func(f"Validating EPUB at {epub_path}...")
    if not os.path.exists(epub_path):
        log_func(f"Validation error: File {epub_path} does not exist.")
        return False
        
    try:
        with zipfile.ZipFile(epub_path, 'r') as z:
            infolist = z.infolist()
            if not infolist:
                log_func("Validation error: EPUB zip is empty.")
                return False
                
            mimetype_info = infolist[0]
            if mimetype_info.filename != "mimetype":
                log_func(f"Validation error: First file must be 'mimetype', found '{mimetype_info.filename}'")
                return False
            if mimetype_info.compress_type != zipfile.ZIP_STORED:
                log_func("Validation error: 'mimetype' must be ZIP_STORED")
                return False
                
            mimetype_content = z.read("mimetype").decode('utf-8').strip()
            if mimetype_content != "application/epub+zip":
                log_func(f"Validation error: 'mimetype' content mismatch: '{mimetype_content}'")
                return False
                
            opf_path, ncx_path = None, None
            html_paths = []
            
            for info in infolist:
                filename = info.filename
                if filename.endswith(".opf"):
                    opf_path = filename
                elif filename.endswith(".ncx"):
                    ncx_path = filename
                elif filename.endswith(".html") or filename.endswith(".xhtml"):
                    html_paths.append(filename)
                    
            if not opf_path:
                log_func("Validation error: Missing .opf file in EPUB.")
                return False
                
            # Parse content.opf
            opf_content = z.read(opf_path)
            try:
                root = ET.fromstring(opf_content)
                lang_el = root.find('.//{http://purl.org/dc/elements/1.1/}language')
                if lang_el is None:
                    lang_el = root.find('.//language')
                if lang_el is None:
                    log_func("Validation error: <dc:language> element not found.")
                    return False
                lang = lang_el.text
                if not lang or lang.strip().lower() in ["c", "", "posix"]:
                    log_func(f"Validation error: Invalid language code '{lang}' in OPF.")
                    return False
            except Exception as e:
                log_func(f"Validation error: Failed to parse content.opf: {e}")
                return False
                
            # Parse HTML/XHTML files
            for html_path in html_paths:
                html_content = z.read(html_path)
                sanitized_html = sanitize_xhtml_for_xml_parser(html_content)
                try:
                    ET.fromstring(sanitized_html.encode('utf-8'))
                except Exception as e:
                    log_func(f"Validation error: HTML file '{html_path}' is not valid XML: {e}")
                    return False
                    
        log_func("EPUB validation completed successfully!")
        return True
    except Exception as e:
        log_func(f"Validation error: Failed to validate EPUB file: {e}")
        return False
```

**Step 4: Copy the plan to the plan location**
Write this plan markdown file to `~/kindle-butch-gen/docs/plans/2026-07-06-multibook-kindle-tts-web.md`.

**Step 5: Verify**
Run a test python script to check imports:
`python3 -c "from common.text_protect import PlaceholderManager; from common.epub_validate import validate_epub; print('Imports OK!')"`
Expected: `Imports OK!`

---

### Task 2: Refactor `translate_stage.py` & `translate_epub.py` to use `common` and config-driven parameters

**Files:**
- Modify: `~/kindle-butch-gen/translate_stage.py`
- Modify: `~/kindle-butch-gen/translate_epub.py`

**Step 1: Replace local PlaceholderManager with import**
Remove `PlaceholderManager` class definition from both scripts and add:
```python
from common.text_protect import PlaceholderManager
```

**Step 2: Modify `translate_epub.py` to accept config options**
Make it accept `--book <slug>` and `--config <path>` to resolve book-specific directories.
Update the `main` or arguments parsing:
```python
# In translate_epub.py / translate_stage.py:
# Accept --book arg, read config.json from books/<book_slug>/config.json
```

**Step 3: Verify**
Compile the scripts:
`python3 -m py_compile ~/kindle-butch-gen/translate_epub.py ~/kindle-butch-gen/translate_stage.py`
Expected: Return 0 (no syntax errors).

---

### Task 3: Implement `audio_stage.py` (Piper TTS Pipeline)

**Files:**
- Create: `~/kindle-butch-gen/audio_stage.py`

**Step 1: Write `audio_stage.py`**
Implement the TTS conversion logic:
1. Parse `--book <slug>` and load `books/<slug>/config.json`.
2. Locate the merged translated markdown file `books/<slug>/translated/merged_translated_<target_lang>.md` (or read from chapters).
3. Read config `tts_voice` (default `uk_UA-lada-medium.onnx`) and `generate_audiobook` settings.
4. Clean text using `PlaceholderManager.strip_formatting(text)`.
5. Preprocess text for stresses (inject `+` before vowels using custom dictionary or simple phonetic rules if library not installed).
6. Split text into paragraph chunks (up to 1000 characters).
7. Synthesize chunks into `books/<slug>/audio/chunks/<chunk_hash>.wav` using Piper binary:
   `~/kindle-butch-gen/bin/piper -m ~/kindle-butch-gen/models/piper/uk_UA-lada-medium.onnx -f books/<slug>/audio/chunks/<chunk_hash>.wav`
   - Cache results in `books/<slug>/cache/tts_cache.json`.
   - Wrap chunk generation in `termux-wake-lock` / `try-finally` `termux-wake-unlock`.
8. Concat chapter chunks into `.mp3` or `.m4b` using `ffmpeg` and output to `books/<slug>/output/`.

**Step 2: Verification**
Compile:
`python3 -m py_compile ~/kindle-butch-gen/audio_stage.py`

---

### Task 4: Refactor `run_conversion_batches.py` (Batch Orchestrator)

**Files:**
- Modify: `~/kindle-butch-gen/run_conversion_batches.py`

**Step 1: Re-write argument parser & config integration**
Make the orchestrator read configuration from `books/<slug>/config.json` instead of constants.
Support `--book <slug>` argument.

**Step 2: Integrate `audio_stage.py`**
Trigger `audio_stage.py` after the translation and compilation phase completes.

**Step 3: Verification**
Compile:
`python3 -m py_compile ~/kindle-butch-gen/run_conversion_batches.py`

---

### Task 5: Create Unified Bash Controller (`kbg.sh`)

**Files:**
- Create: `~/kindle-butch-gen/kbg.sh`

**Step 1: Write `kbg.sh`**
Implement actions:
- `add <slug> --pdf <path> --title <title> --authors <authors> --lang <lang>`: Create folder structure, copy PDF, write `config.json`.
- `run <slug> [--translate <lang>] [--audiobook] [--ebook]`:
  - Run `start-translation-server.sh` if llama-server port 8081 is down.
  - Run `run_conversion_batches.py` with options.
- `status <slug>`: Report progress (translated pages, TTS chunks finished, final files availability).
- `serve`: Run web dashboard on port 8090.

**Step 2: Make executable**
`chmod +x ~/kindle-butch-gen/kbg.sh`

---

### Task 6: Implement Flask Web Interface (`kbg_web/app.py`)

**Files:**
- Create: `~/kindle-butch-gen/kbg_web/app.py`
- Create: `~/kindle-butch-gen/kbg_web/templates/index.html`

**Step 1: Write `kbg_web/app.py`**
Implement Flask/FastAPI server:
- Route `/`: Dashboard listing books and their status.
- Route `/book/<slug>`: Book details page, logs, download buttons, action triggers.
- Route `/api/run/<slug>`: Run command inside subprocess.
- Route `/api/logs/<slug>`: Return last 200 lines of `logs/<slug>.log`.
- Route `/downloads/<slug>/<file>`: Serve output files (`.epub`, `.azw3`, `.m4b`).

**Step 2: Write `kbg_web/templates/index.html`**
Create a premium, clean responsive web interface (curated colors, sleek dark mode, simple dashboard grids, auto-refresh logs).

---

### Task 7: Set Up Models, Download Piper Binary and Test

**Step 1: Download Piper Binary & Voice**
Implement automatic download scripts inside `kbg.sh` or run curl to download Piper C++ arm64 and Lada medium voice files.

**Step 2: End-to-end Test**
Run a complete conversion test command on a mini-PDF or existing book batch.
Verify epub, azw3, and audio chunks generation.

---
