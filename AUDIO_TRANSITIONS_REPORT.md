# Audio Transition Analysis Report
**Target Book:** vibe-programming ("Вайб-програмування")
**Date:** 2026-07-14

---

## 1. Identified Files & Pipeline Code

### Audio Assets
* **Final Compiled Audiobook:** `/data/data/com.termux/files/home/kindle-butch-gen/books/vibe-programming/output/vibe-programming_translated_uk.mp3` (Size: 338 MB, Duration: 11h 11m).
* **Intermediate Chunks Directory:** `/data/data/com.termux/files/home/kindle-butch-gen/books/vibe-programming/audio/chunks_styletts2` (Contains 5167 files in WAV format, confirming the **StyleTTS2** engine was used).

### Source Code
* **Chunking Logic:** `audio_stage.py` -> `split_paragraph_to_chunks()` splits paragraphs by sentence boundary punctuation (`.`, `!`, `?`) up to a maximum character limit.
* **Trimming Logic:** `bin/tts_helper.py` -> `trim_silence()` cuts leading and trailing silences using a threshold of `100` absolute amplitude.
* **Concatenation Logic:** `audio_stage.py` -> runs `ffmpeg` with `-f concat` over a list of WAV chunks.

---

## 2. Silence & Pause Measurements

Silence intervals were extracted programmatically from the first several minutes of the final MP3 using `ffmpeg silencedetect` (threshold `-35dB`, minimum duration `50ms`):

| Silence Start (s) | Silence End (s) | Silence Duration (ms) | Context / Pause Type |
|-------------------|-----------------|-----------------------|----------------------|
| 2.057             | 2.293           | 235 ms                | Sentence boundary    |
| 3.868             | 3.933           | 65 ms                 | Word/syllable joint  |
| 3.986             | 4.201           | 214 ms                | Sentence boundary    |
| 5.508             | 5.976           | 468 ms                | Sentence boundary    |
| 7.715             | 7.779           | 63 ms                 | Word/syllable joint  |
| 7.809             | 7.989           | 180 ms                | Sentence boundary    |
| 9.332             | 9.758           | 425 ms                | Sentence boundary    |
| 12.684            | 12.755          | 70 ms                 | Word/syllable joint  |
| 12.757            | 12.952          | 195 ms                | Sentence boundary    |

### Key Findings
1. **Rushed Sentence Joint Pauses:** The average trailing silence across all tested chunks is **162.8 ms**. This is far lower than the intended 500 ms sentence pause, creating a rushed and unnatural reading pace.
2. **Missing Chapter Pauses:** Headings (e.g. `# **Вайб-програмування...**` or `## оди+н. Вступ`) have no trailing punctuation. Under the current logic, they default to `custom_pad_end = 100` ms. Thus, chapter headers immediately run into the following paragraphs with a tiny 100ms pause (a difference of zero/negative compared to sentence joints).

---

## 3. Word Cutoff & RMS Waveform Analysis

An audit of the final 200 ms of 500 random WAV chunks was performed:
* **Abruptly ending chunks (potential cutoffs):** 8 chunks (1.6% of the audited set).
* **Examples of Clipped Chunks:**

| Chunk Hash | Text Snippet | Last 20ms RMS | Last Sample Amp | Duration (ms) | Cutoff |
|------------|--------------|---------------|-----------------|---------------|--------|
| `c2d267e9...` | `## оди+н. Вступ` | 1369.1 | 1464.2 | 1020.0 ms | Yes |
| `6be441b2...` | `Рису+нок п'ять-три. Налаштува+ння глоба+льних...` | 1282.2 | 703.5 | 5120.1 ms | Yes |
| `4bd30e06...` | `|--|--|--|` | 1290.7 | 1247.7 | 800.0 ms | Yes |
| `46d94742...` | `\`\`\`мермаід` | 2461.8 | 2706.3 | 850.0 ms | Yes |

### Analysis of the Cutoff Cause
When a paragraph/chunk has no trailing punctuation (such as headings or list items), `tts_helper.py` applies a very short 100ms padding (`pad_end_ms=100`).
If the speaker's trailing phoneme or vocal tract decay takes longer than 100ms, the `trim_silence` slice cuts the audio file off while the amplitude is still high. This creates a hard cut (clipping/popping sound) and truncates the tail of the final word.

---

## 4. Current Concatenation Mechanics

1. **Raw Concat:** Chunks are merged raw using `-f concat` in `ffmpeg`. There is no crossfade, padding, or silence insertion during the merging step.
2. **Trimming Behavior:** `trim_silence()` slices the numpy audio sample array. However, **it does not pad or generate silence**. If the raw TTS model output only contains 50ms of silence at the end, then slicing with `pad_end_ms=500` will still yield only 50ms of silence.
3. **No Special Heading Markers:** Headings are stored as plain text inside `chunk_texts`. The pipeline does not differentiate a heading from regular text during synthesis or concatenation, explaining why there are no pauses before/after chapters.
4. **Markdown Garbage:** Non-spoken markdown elements (like code blocks `\`\`\`mermaid` or table grids `|--|--|--|`) are processed by the TTS engine, producing clicks, garbage noise, or sudden silent chunks.

---

## 5. Proposed Solutions

### Option A: Smart Concatenation & Post-processing (NO Re-synthesis Needed)
* **How it works:** 
  1. Modify `audio_stage.py` to generate short silent WAV files (e.g. `silence_300ms.wav`, `silence_600ms.wav`, `silence_1500ms.wav`).
  2. Modify the ffmpeg list builder to insert these silent chunks between text chunks during concatenation:
     - Between sentences inside a paragraph: 300 ms silence.
     - Between paragraphs: 600 ms silence.
     - After headings: 1500 ms silence.
  3. Apply a short linear fade-out (e.g., 15 ms) to the end of each chunk WAV file before concatenation to eliminate clicks/pops.
* **Re-synthesis cost:** **None**. Runs instantly in seconds.

### Option B: Padding Generation during Synthesis (Requires Full Re-synthesis)
* **How it works:**
  1. Modify `trim_silence` in `bin/tts_helper.py` so that if `last_idx + pad_end` exceeds the length of the generated sample array, it appends zeros (actual silence) to pad the chunk out to the desired length.
  2. Increase the default non-punctuated chunk padding from `100 ms` to `250 ms` to prevent word truncation.
* **Re-synthesis cost:** **High**. Requires clearing the cache and re-generating all 5167 audio chunks (takes several hours).

### Option C: Markdown Cleaning & Heading Marker Injection (Recommended Addition)
* **How it works:**
  1. Update `split_paragraph_to_chunks` in `audio_stage.py` to strip out markdown table separators (`|--|--|`), code block markers (`\`\`\``), and raw links.
  2. Identify heading lines (starting with `#`) and return them as a distinct chunk metadata type, allowing the concatenator (Option A) to inject longer pauses.
* **Re-synthesis cost:** **Low**. Only requires re-generating the modified/cleaned paragraphs.
