import os
import re
import json
import hashlib
import sys

# Resolve repo root directory
repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if repo_dir not in sys.path:
    sys.path.insert(0, repo_dir)

from common.book_paths import resolve_book_paths
from common.text_protect import PlaceholderManager

def get_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def split_into_segments(text, max_chars=1200):
    paragraphs = text.split("\n\n")
    segments = []
    current_segment = []
    current_length = 0
    for p in paragraphs:
        p_len = len(p)
        if p_len > max_chars:
            if current_segment:
                segments.append("\n\n".join(current_segment))
                current_segment = []
                current_length = 0
            sentences = re.split(r'(?<=[.!?])\s+', p)
            curr_sent_group = []
            curr_sent_len = 0
            for s in sentences:
                if curr_sent_len + len(s) > max_chars:
                    if curr_sent_group:
                        segments.append(" ".join(curr_sent_group))
                    curr_sent_group = [s]
                    curr_sent_len = len(s)
                else:
                    curr_sent_group.append(s)
                    curr_sent_len += len(s) + 1
            if curr_sent_group:
                segments.append(" ".join(curr_sent_group))
        else:
            if current_length + p_len > max_chars:
                segments.append("\n\n".join(current_segment))
                current_segment = [p]
                current_length = p_len
            else:
                current_segment.append(p)
                current_length += p_len + 2
    if current_segment:
        segments.append("\n\n".join(current_segment))
    return segments

def split_paragraph_to_chunks(text, max_chars=1000):
    text = re.sub(r"__[A-Z_]+_\d+__", "", text)
    clean_text = PlaceholderManager.strip_formatting(text).strip()
    if not clean_text:
        return []
    if len(clean_text) <= max_chars:
        return [clean_text]
    sentences = re.split(r'(?<=[.!?])\s+', clean_text)
    chunks = []
    curr_group = []
    curr_len = 0
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            if curr_group:
                chunks.append(" ".join(curr_group))
                curr_group = []
                curr_len = 0
            words = sentence.split(" ")
            word_group = []
            word_len = 0
            for w in words:
                if word_len + len(w) + 1 > max_chars:
                    if word_group:
                        chunks.append(" ".join(word_group))
                    word_group = [w]
                    word_len = len(w)
                else:
                    word_group.append(w)
                    word_len += len(w) + 1
            if word_group:
                chunks.append(" ".join(word_group))
        else:
            if curr_len + len(sentence) + (1 if curr_group else 0) > max_chars:
                if curr_group:
                    chunks.append(" ".join(curr_group))
                curr_group = [sentence]
                curr_len = len(sentence)
            else:
                curr_group.append(sentence)
                curr_len += len(sentence) + (1 if len(curr_group) > 1 else 0)
    if curr_group:
        chunks.append(" ".join(curr_group))
    return chunks

def get_pdf_page_count(pdf_path):
    try:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        return len(reader.pages)
    except ImportError:
        pass
    try:
        with open(pdf_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 102400))
            tail = f.read()
            matches = re.findall(rb"/Count\s+(\d+)", tail)
            if matches:
                return int(matches[-1])
            f.seek(0)
            content = f.read()
            matches = re.findall(rb"/Count\s+(\d+)", content)
            if matches:
                return int(matches[-1])
    except Exception:
        pass
    return 10  # Fallback

def calculate_progress(slug):
    paths = resolve_book_paths(repo_dir, slug)
    book_dir = paths["book_dir"]
    if not os.path.exists(book_dir):
        return {
            "marker_percent": 0.0,
            "translation_percent": 0.0,
            "tts_percent": 0.0,
            "error": "Book directory does not exist"
        }
        
    # Check if direct EPUB progress is available
    epub_prog_path = os.path.join(paths["cache_dir"], "epub_progress.json")
    if os.path.exists(epub_prog_path):
        try:
            with open(epub_prog_path, "r", encoding="utf-8") as f:
                ep = json.load(f)
                curr = ep.get("current_file", 0)
                tot = ep.get("total_files", 0)
                pct = ep.get("percent", 0.0)
            
            is_manga = False
            generate_audiobook = True
            config_path = paths["config_path"]
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as cf:
                        cfg_json = json.load(cf)
                        is_manga = cfg_json.get("is_manga", False)
                        generate_audiobook = cfg_json.get("generate_audiobook", True)
                except Exception:
                    pass
                    
            if is_manga:
                overall_percent = pct
            else:
                if generate_audiobook:
                    overall_percent = (100.0 + pct + 0.0 + 0.0) / 4
                else:
                    overall_percent = (100.0 + pct) / 2

            return {
                "is_manga": is_manga,
                "manga_percent": pct,
                "manga_pages_completed": curr,
                "manga_total_pages": tot,
                "marker_percent": 100.0,
                "translation_percent": pct,
                "stress_percent": 0.0,
                "tts_percent": 0.0,
                "overall_percent": round(overall_percent, 1)
            }
        except Exception:
            pass

    # Check if manga
    config_path = paths["config_path"]
    is_manga = False
    generate_audiobook = True
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                is_manga = cfg.get("is_manga", False)
                generate_audiobook = cfg.get("generate_audiobook", True)
        except Exception:
            pass
            
    if is_manga:
        manga_progress_path = os.path.join(book_dir, "manga_progress.json")
        manga_percent = 0.0
        curr = 0
        tot = 0
        if os.path.exists(manga_progress_path):
            try:
                with open(manga_progress_path, "r", encoding="utf-8") as f:
                    mp = json.load(f)
                    curr = mp.get("current_page", 0)
                    tot = mp.get("total_pages", 0)
                    if tot > 0:
                        manga_percent = round((curr / tot) * 100.0, 1)
            except Exception:
                pass
        return {
            "is_manga": True,
            "manga_percent": manga_percent,
            "manga_pages_completed": curr,
            "manga_total_pages": tot,
            "marker_percent": manga_percent,
            "translation_percent": manga_percent,
            "stress_percent": manga_percent,
            "tts_percent": manga_percent,
            "overall_percent": manga_percent
        }
    
    pdf_path = paths.get("pdf_path")
    has_pdf = pdf_path and os.path.exists(pdf_path)
    page_ranges = paths.get("page_ranges")
    
    # 1. Marker Progress
    if not has_pdf or not page_ranges:
        marker_percent = 100.0
    else:
        total_pages = sum(end - start + 1 for start, end in page_ranges)
        completed_marker_pages = 0
        pdf_basename = os.path.splitext(os.path.basename(pdf_path))[0]
        
        for start, end in page_ranges:
            batch_out_dir = os.path.join(paths["batches_dir"], f"batch_{start}_{end}")
            marker_out_subdir = os.path.join(batch_out_dir, pdf_basename)
            marker_md_file = os.path.join(marker_out_subdir, f"{pdf_basename}.md")
            if os.path.exists(marker_md_file) and os.path.getsize(marker_md_file) > 0:
                completed_marker_pages += (end - start + 1)
        marker_percent = (completed_marker_pages / total_pages * 100) if total_pages > 0 else 0.0
    
    # 2. Translation Progress
    should_translate = paths["target_lang"] != paths["source_lang"]
    merged_translated = os.path.join(book_dir, "translated", f"merged_translated_{paths['target_lang']}.md")
    if not should_translate or (os.path.exists(merged_translated) and os.path.getsize(merged_translated) > 0):
        translation_percent = 100.0
    elif not has_pdf or not page_ranges:
        translation_percent = 0.0
    else:
        translate_cache = {}
        if os.path.exists(paths["translate_cache"]):
            try:
                with open(paths["translate_cache"], "r", encoding="utf-8") as f:
                    translate_cache = json.load(f)
            except Exception:
                pass
                
        completed_trans_pages = 0.0
        pdf_basename = os.path.splitext(os.path.basename(pdf_path))[0]
        total_pages = sum(end - start + 1 for start, end in page_ranges)
        
        for start, end in page_ranges:
            batch_out_dir = os.path.join(paths["batches_dir"], f"batch_{start}_{end}")
            marker_out_subdir = os.path.join(batch_out_dir, pdf_basename)
            marker_md_file = os.path.join(marker_out_subdir, f"{pdf_basename}.md")
            
            if os.path.exists(marker_md_file) and os.path.getsize(marker_md_file) > 0:
                try:
                    pm = PlaceholderManager()
                    with open(marker_md_file, "r", encoding="utf-8") as f:
                        text = f.read()
                    protected_text = pm.protect(text)
                    segments = split_into_segments(protected_text)
                    if segments:
                        completed_segs = sum(1 for seg in segments if get_hash(seg) in translate_cache)
                        fraction = completed_segs / len(segments)
                    else:
                        fraction = 1.0
                except Exception:
                    fraction = 0.0
                completed_trans_pages += (end - start + 1) * fraction
                
        translation_percent = (completed_trans_pages / total_pages * 100) if total_pages > 0 else 0.0

    # 3. TTS Progress
    tts_engine = paths.get("tts_engine", "supertonic3")
    if tts_engine == "styletts2":
        voice_slug = "styletts2"
    else:
        voice_slug = "supertonic-3-tts-int8"
    
    tts_cache_path = os.path.join(paths["cache_dir"], f"tts_cache_{voice_slug}.json")
    tts_cache = {}
    if os.path.exists(tts_cache_path):
        try:
            with open(tts_cache_path, "r", encoding="utf-8") as f:
                tts_cache = json.load(f)
        except Exception:
            pass
            
    chunks_dir = os.path.join(paths["audio_dir"], f"chunks_{voice_slug}")
    
    # 4. Stressifier Progress
    stress_cache_path = os.path.join(paths["book_dir"], "translated", f"stress_cache_{paths['target_lang']}.json")
    stress_cache = {}
    if os.path.exists(stress_cache_path):
        try:
            with open(stress_cache_path, "r", encoding="utf-8") as f:
                stress_cache = json.load(f)
        except Exception:
            pass

    suffix = f"_translated_{paths['target_lang']}" if (paths["target_lang"] != paths["source_lang"]) else ""
    if suffix:
        target_md_file = os.path.join(paths["translated_dir"], f"merged_translated_{paths['target_lang']}.md")
    else:
        target_md_file = os.path.join(paths["translated_dir"], f"merged_source_{paths['source_lang']}.md")

    if os.path.exists(target_md_file) and os.path.getsize(target_md_file) > 0:
        try:
            with open(target_md_file, "r", encoding="utf-8") as f:
                content = f.read()
            paragraphs = re.split(r'\n\s*\n', content)
            chunk_texts = []
            max_chunk_chars = 150 if tts_engine == "styletts2" else 1000
            for p in paragraphs:
                chunks = split_paragraph_to_chunks(p, max_chars=max_chunk_chars)
                for chunk in chunks:
                    chunk = chunk.strip()
                    if chunk:
                        chunk_texts.append(chunk)
            
            if chunk_texts:
                completed_chunks = 0
                completed_stress = 0
                for text in chunk_texts:
                    h = get_hash(text)
                    wav_file = os.path.join(chunks_dir, f"{h}.wav")
                    if h in tts_cache and os.path.exists(wav_file):
                        completed_chunks += 1
                    if h in stress_cache:
                        completed_stress += 1
                tts_percent = (completed_chunks / len(chunk_texts) * 100)
                stress_percent = (completed_stress / len(chunk_texts) * 100)
            else:
                tts_percent = 100.0
                stress_percent = 100.0
        except Exception:
            tts_percent = 0.0
            stress_percent = 0.0
    else:
        tts_percent = 0.0
        stress_percent = 0.0
    
    # Calculate overall percent
    if generate_audiobook:
        overall_percent = (marker_percent + translation_percent + stress_percent + tts_percent) / 4
    else:
        overall_percent = (marker_percent + translation_percent) / 2

    return {
        "is_manga": False,
        "marker_percent": round(marker_percent, 1),
        "translation_percent": round(translation_percent, 1),
        "stress_percent": round(stress_percent, 1),
        "tts_percent": round(tts_percent, 1),
        "overall_percent": round(overall_percent, 1)
    }

def print_status(slug):
    res = calculate_progress(slug)
    if "error" in res:
        print(f"Error: {res['error']}")
        sys.exit(1)
    print(f"Marker: {res['marker_percent']}%")
    print(f"Translation: {res['translation_percent']}%")
    print(f"TTS: {res['tts_percent']}%")

def add_book(slug, pdf_path, title, authors, lang, source_lang="ru", is_manga=False):
    import shutil
    if not re.match(r"^[a-z0-9_-]+$", slug):
        raise ValueError("Invalid slug")
    
    paths = resolve_book_paths(repo_dir, slug)
    
    os.makedirs(paths["book_dir"], exist_ok=True)
    os.makedirs(paths["cache_dir"], exist_ok=True)
    os.makedirs(paths["batches_dir"], exist_ok=True)
    os.makedirs(paths["translated_dir"], exist_ok=True)
    os.makedirs(paths["output_dir"], exist_ok=True)
    os.makedirs(paths["audio_dir"], exist_ok=True)
    
    ext = os.path.splitext(pdf_path)[1].lower()
    dest_file = os.path.join(paths["book_dir"], f"{slug}{ext}")
    shutil.copy2(pdf_path, dest_file)
    
    if ext == ".pdf":
        pages = get_pdf_page_count(dest_file)
        page_ranges = [[1, pages]]
    else:
        pages = 0
        page_ranges = []
        
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
        
    print(f"Book '{slug}' added successfully.")

