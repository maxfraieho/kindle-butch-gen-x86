# CLI Command Reference — Vydra (`kindle-butch-gen`)

This reference details command-line scripts available for managing book pipelines, downloading models, diagnosing system health, and fixing Android background limits.

---

## 🛠️ Main CLI Driver: `./kbg.sh`

The `./kbg.sh` script is the primary entry point for managing books and pipeline tasks.

### 1. Adding a Book (`add`)
Registers a new EPUB or PDF book into the local library workspace (`~/kindle-butch-gen/books/<slug>/`).

```bash
./kbg.sh add --slug <unique_slug> \
             --pdf </path/to/book.pdf> \
             --title "<Book Title>" \
             --authors "<Author Name>" \
             --lang <en|uk|ru>
```

---

### 2. Running a Pipeline (`run`)
Executes the full translation, stress tagging, TTS synthesis, ASR verification, and packaging pipeline.

```bash
./kbg.sh run <slug>
```

- **Options**:
  - `--force`: Re-run all completed steps from scratch.
  - `--audio-only`: Skip text translation and execute only audiobook synthesis on existing text.

---

### 3. Checking Progress (`status`)
Displays the status of each pipeline stage for a specific book.

```bash
./kbg.sh status <slug>
```

---

### 4. Serving the Web Dashboard (`serve`)
Launches the Flask web dashboard and REST API.

```bash
./kbg.sh serve --port 5000
```

---

## 📦 Model Downloader: `bin/download_premium_models.sh`

Downloads neural network models required for TTS, ASR, and Agent-Editor. Features size verification and resumable HTTP transfers.

```bash
# Download ASR Whisper Small INT8 models (~245 MB)
./bin/download_premium_models.sh --asr

# Download Gemma 3 4B Vision models (~3.3 GB)
./bin/download_premium_models.sh --gemma

# Download all premium models
./bin/download_premium_models.sh --target all
```

---

## 👻 Android Phantom Process Killer Fix: `bin/fix_phantom_process_killer.sh`

Resolves background process termination issues on Android 12+ devices where long-running translations disappear after 20-40 minutes despite Battery Optimization being disabled.

```bash
bash bin/fix_phantom_process_killer.sh
```

- Guides the user through enabling Wireless Debugging in Android Developer Options.
- Adjusts `max_phantom_processes` system limits safely via ADB pairing.
