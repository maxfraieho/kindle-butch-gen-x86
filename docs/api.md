# REST API Reference — Vydra (`kbg_web/app.py`)

The Vydra web backend provides a full HTTP REST API on port `5000` (default) for managing books, triggering translation/synthesis pipelines, inspecting system status, and configuring per-book AI features.

---

## 🔐 Authentication

- **HTTP Basic Authentication**: Supplied via standard `Authorization: Basic <base64>` header (default user: `vokov`).
- **Session Cookie Authentication**: Form login at `/login` sets a secure `session` cookie.

---

## 📚 Book Management Endpoints

### 1. `GET /api/books`
Returns the list of all books in the library along with their metadata and generation progress.

- **Response `200 OK`**:
```json
[
  {
    "slug": "vibe-programming",
    "title": "Vibe Programming Guide",
    "author": "Author Name",
    "source_lang": "en",
    "target_lang": "uk",
    "status": "ready",
    "progress": 100,
    "has_audio": true,
    "has_epub": true,
    "has_azw3": true
  }
]
```

---

### 2. `GET /api/book-settings/<slug>`
Retrieves granular settings for a specific book, including UI 2.0 premium capabilities.

- **Response `200 OK`**:
```json
{
  "slug": "vibe-programming",
  "enable_asr_verify": true,
  "enable_mqm_review": false,
  "enable_agent_editor": true,
  "tts_voice": "supertonic3",
  "tts_speaker_id": 2,
  "tts_speed": 1.0
}
```

---

### 3. `POST /api/book-settings/<slug>`
Updates per-book configuration flags.

- **Request Body**:
```json
{
  "enable_asr_verify": true,
  "enable_mqm_review": true,
  "enable_agent_editor": false,
  "tts_speaker_id": 3
}
```
- **Response `200 OK`**:
```json
{
  "status": "ok",
  "message": "Settings updated for vibe-programming"
}
```

---

## 👑 Premium & Model Management Endpoints

### 1. `GET /api/premium/models-status`
Inspects local availability of premium AI models required for ASR, MQM, and Gemma 3 Agent-Editor.

- **Response `200 OK`**:
```json
{
  "asr_whisper": {
    "ready": true,
    "encoder_path": "~/models/sherpa-onnx-whisper-small-int8/small-encoder.int8.onnx",
    "decoder_path": "~/models/sherpa-onnx-whisper-small-int8/small-decoder.int8.onnx"
  },
  "gemma": {
    "ready": true,
    "path": "~/models/gemma3-4b/gemma-3-4b-it-Q4_K_M.gguf"
  },
  "mmproj": {
    "ready": true,
    "path": "~/models/gemma3-4b/mmproj-model-f16.gguf"
  }
}
```

---

### 2. `POST /api/premium/download-models`
Triggers background downloading of missing AI models.

- **Request Body**:
```json
{
  "target": "asr"  // "all" | "gemma" | "asr"
}
```
- **Response `202 Accepted`**:
```json
{
  "status": "downloading",
  "target": "asr",
  "message": "Background model download initiated"
}
```

---

## 🩺 System & Support Endpoints

### 1. `GET /api/status`
Returns general pipeline execution status and active background jobs.

### 2. `GET /api/system-health`
Returns hardware metrics (RAM usage, storage space, CPU load, and thermal status).

### 3. `GET /api/support-profile`
Returns Telegram entitlement status, multi-device registration info, and support banner flags.
