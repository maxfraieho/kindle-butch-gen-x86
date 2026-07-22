#!/usr/bin/env python3
import sys
import os
import shutil
import subprocess
import argparse
import json
from datetime import datetime

# Add the repo root to sys.path so we can import from common
repo_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, repo_dir)

from common.book_paths import resolve_book_paths
from common.epub_validate import validate_epub
from common.edit_patch import patch_batch_translation
from common.heartbeat import send_heartbeat
from kbg_web import edit_store

def log(message, log_path):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = f"[{timestamp}] {message}\n"
    print(msg, end="", flush=True)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(msg)
    except Exception as e:
        print(f"Failed to write to log path {log_path}: {e}")

def run_command_streaming(cmd, log_path, prefix="", env=None):
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env
        )
        for line in process.stdout:
            log(f"{prefix}{line.strip()}", log_path)
        process.wait()
        return process.returncode == 0
    except Exception as e:
        log(f"Command failed with exception: {e}", log_path)
        return False

def run_marker_batch(start, end, pdf_path, batches_dir, log_path):
    log(f"Starting marker batch {start}-{end}...", log_path)
    batch_out_dir = os.path.join(batches_dir, f"batch_{start}_{end}")
    os.makedirs(batch_out_dir, exist_ok=True)
    
    cmd = [
        "proot-distro", "login", "ubuntu", "--",
        "env",
        "OMP_NUM_THREADS=4",
        "MKL_NUM_THREADS=4",
        "OPENBLAS_NUM_THREADS=4",
        "TORCH_NUM_THREADS=4",
        "marker_single",
        pdf_path,
        "--disable_ocr",
        "--disable_multiprocessing",
        "--page_range", f"{start}-{end}",
        "--output_dir", batch_out_dir
    ]
    
    marker_log_path = os.path.join(batch_out_dir, "marker_run.log")
    returncode = -1
    try:
        with open(marker_log_path, "w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            for line in process.stdout:
                log_file.write(line)
                log_file.flush()
                line_lower = line.lower()
                if any(x in line_lower for x in ["saving", "processing", "rendering", "percent", "page", "success", "error", "warning"]):
                    log(f"[Marker {start}-{end}] {line.strip()}", log_path)
            process.wait()
            returncode = process.returncode
    except Exception as e:
        log(f"Error executing marker batch: {e}", log_path)
        return False
        
    if returncode != 0:
        log(f"Error: Batch {start}-{end} failed with return code {returncode}!", log_path)
        return False
    
    log(f"Batch {start}-{end} completed successfully.", log_path)
    return True

def apply_pending_text_edits(slug, batches_dir, suffix, log_path):
    """TASK-23: called between batches while this book is still
    status=running. Applies any live text edits that target text already
    present in an already-completed batch file, so an edit made mid-run
    doesn't sit stale until the whole book finishes."""
    pending = edit_store.list_edits(slug, mode="text", status="pending")
    if not pending:
        return
    for edit in pending:
        old_text = edit.get("original_value")
        new_text = edit.get("edited_value")
        if not old_text or new_text is None:
            continue
        if patch_batch_translation(batches_dir, suffix, old_text, new_text):
            edit_store.mark_status(slug, edit["id"], "regenerated", applied_at=datetime.now().isoformat())
            log(f"[LiveEdit] Applied pending text edit {edit['id']} to a completed batch file.", log_path)

def main():
    parser = argparse.ArgumentParser(description="Batch conversion, translation, and ebook generation pipeline")
    parser.add_argument("--book", "-b", required=True, help="Book slug")
    parser.add_argument("--config", "-c", help="Optional path to config.json")
    parser.add_argument("--clean", action="store_true", help="Clean batches directory before running")
    parser.add_argument("--no-translate", action="store_true", help="Disable translation stage")
    parser.add_argument("--no-ebook", action="store_true", help="Disable Calibre EPUB/AZW3 conversion")
    parser.add_argument("--no-audio", action="store_true", help="Disable audiobook synthesis stage")
    args = parser.parse_args()

    slug = args.book
    
    # Load paths using resolve_book_paths
    paths = resolve_book_paths(repo_dir, slug, config_path=args.config)
    log_path = paths["log_path"]
    
    # If custom config is provided, overwrite key fields in paths
    if args.config and os.path.exists(args.config):
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                custom_cfg = json.load(f)
            log(f"Overriding book paths with custom config from {args.config}", log_path)
            for k, v in custom_cfg.items():
                if k in ["title", "authors", "target_lang", "source_lang", "page_ranges", "generate_audiobook", "enable_asr_verify", "enable_mqm_review", "enable_agent_editor", "tts_voice", "tts_voice_quality"]:
                    paths[k] = v
                elif k == "pdf_path":
                    paths["pdf_path"] = os.path.abspath(v)
                elif k == "cover":
                    cover_path = v
                    if not os.path.isabs(cover_path):
                        cover_path = os.path.join(paths["book_dir"], cover_path)
                    paths["cover_path"] = os.path.abspath(cover_path)
        except Exception as e:
            log(f"Warning: Failed to parse custom config: {e}", log_path)
            
    log(f"=== Starting pipeline for book: {slug} ===", log_path)
    
    pdf_path = paths.get("pdf_path")
    has_pdf = pdf_path and os.path.exists(pdf_path)
    
    os.makedirs(paths["batches_dir"], exist_ok=True)
    os.makedirs(paths["translated_dir"], exist_ok=True)
    os.makedirs(paths["output_dir"], exist_ok=True)
    
    suffix = f"_translated_{paths['target_lang']}" if (paths["target_lang"] != paths["source_lang"]) else ""
    if suffix:
        merged_md_name = f"merged_translated_{paths['target_lang']}.md"
    else:
        merged_md_name = f"merged_source_{paths['source_lang']}.md"
    final_merged_md_path = os.path.join(paths["translated_dir"], merged_md_name)

    if not has_pdf:
        log("No source PDF found or configured. Checking for existing merged files...", log_path)
        source_md_name = f"merged_source_{paths['source_lang']}.md"
        source_md_path = os.path.join(paths["translated_dir"], source_md_name)
        
        should_translate = (paths["target_lang"] != paths["source_lang"]) and not args.no_translate
        
        if should_translate:
            if not os.path.exists(final_merged_md_path):
                if os.path.exists(source_md_path):
                    log(f"Merged source Markdown exists at '{source_md_path}'. Translating to '{final_merged_md_path}'...", log_path)
                    cmd_translate = [
                        "python3", os.path.join(repo_dir, "translate_stage.py"),
                        "--input", source_md_path,
                        "--output", final_merged_md_path,
                        "--cache", paths["translate_cache"],
                        "--target-lang", paths["target_lang"],
                        "--book", slug
                    ]
                    if args.config:
                        cmd_translate.extend(["--config", args.config])
                    log(f"Running translation command: {' '.join(cmd_translate)}", log_path)
                    success_trans = run_command_streaming(cmd_translate, log_path, prefix="[Translate] ")
                    if not success_trans:
                        log("Error: Translation of merged source markdown failed!", log_path)
                        sys.exit(1)
                else:
                    log(f"Error: Target translated markdown '{final_merged_md_path}' does not exist, and no source markdown '{source_md_path}' found.", log_path)
                    sys.exit(1)
        else:
            # No translation needed, make sure the file is in final_merged_md_path
            if not os.path.exists(final_merged_md_path):
                if os.path.exists(source_md_path):
                    shutil.copy2(source_md_path, final_merged_md_path)
                else:
                    log(f"Error: Neither target markdown '{final_merged_md_path}' nor source markdown '{source_md_path}' exists.", log_path)
                    sys.exit(1)
        log("Merged Markdown is ready. Skipping extraction stage.", log_path)
    else:
        log(f"PDF Path: {pdf_path}", log_path)
        log(f"Output Directory: {paths['output_dir']}", log_path)
        
        # Clean batches if requested
        if args.clean and os.path.exists(paths["batches_dir"]):
            log(f"Cleaning batches directory: {paths['batches_dir']}", log_path)
            shutil.rmtree(paths["batches_dir"])
            os.makedirs(paths["batches_dir"], exist_ok=True)
            
        pdf_basename = os.path.splitext(os.path.basename(pdf_path))[0]
        
        page_ranges = paths.get("page_ranges")
        if not page_ranges:
            log("Warning: No page_ranges defined in config.json. Pipeline cannot run batches.", log_path)
            sys.exit(1)
            
        success = True
        
        # 1. Run marker single-page extraction for each range
        for batch_idx, (start, end) in enumerate(page_ranges):
            send_heartbeat(slug, f"блок {batch_idx + 1}/{len(page_ranges)} (стор. {start}-{end})",
                           stage="переклад книги")
            batch_out_dir = os.path.join(paths["batches_dir"], f"batch_{start}_{end}")
            marker_out_subdir = os.path.join(batch_out_dir, pdf_basename)
            marker_md_file = os.path.join(marker_out_subdir, f"{pdf_basename}.md")
            
            if os.path.exists(marker_md_file) and os.path.getsize(marker_md_file) > 0:
                log(f"Marker batch {start}-{end} already completed. Skipping extraction.", log_path)
            else:
                if not run_marker_batch(start, end, pdf_path, paths["batches_dir"], log_path):
                    success = False
                    break
                    
            # 2. Run translation stage if target_lang != source_lang and not disabled
            should_translate = (paths["target_lang"] != paths["source_lang"]) and not args.no_translate
            if should_translate:
                translated_batch_md = os.path.join(marker_out_subdir, f"{pdf_basename}_translated_{paths['target_lang']}.md")
                if os.path.exists(translated_batch_md) and os.path.getsize(translated_batch_md) > 0:
                    log(f"Translated batch {start}-{end} already exists. Skipping translation.", log_path)
                else:
                    log(f"Translating batch {start}-{end} to {paths['target_lang']}...", log_path)
                    cmd_translate = [
                        "python3", os.path.join(repo_dir, "translate_stage.py"),
                        "--input", marker_md_file,
                        "--output", translated_batch_md,
                        "--cache", paths["translate_cache"],
                        "--target-lang", paths["target_lang"],
                        "--book", slug
                    ]
                    if args.config:
                        cmd_translate.extend(["--config", args.config])
                    log(f"Running translation command: {' '.join(cmd_translate)}", log_path)
                    success_trans = run_command_streaming(cmd_translate, log_path, prefix=f"[Translate {start}-{end}] ")
                    if not success_trans:
                        log(f"Error: Translation of batch {start}-{end} failed!", log_path)
                        success = False
                        break

            # TASK-23: batch boundary — the natural point to check for live
            # edits against already-completed batches before moving on.
            apply_pending_text_edits(slug, paths["batches_dir"], suffix, log_path)

        if not success:
            log("Pipeline aborted due to batch extraction/translation failures.", log_path)
            sys.exit(1)
            
        # 3. Merge batch markdown files and copy images
        log("Merging batch results...", log_path)
        merged_md_content = []

        # Support banners (docs/plans/support-system-plan.md, Phase 1):
        # inserted BETWEEN batches - batch boundaries are page-range
        # boundaries, the closest natural pause available at merge time.
        # Every guard is a safe no-op: no config / disabled / opt-out /
        # heavy-scene tail all mean the merged file is byte-identical to
        # the pre-feature behavior.
        from common.support_banner import (
            load_support_config, user_opted_out, is_heavy_scene,
            SupportInserter, render_md_block,
        )
        support_cfg = load_support_config()
        if support_cfg and user_opted_out():
            support_cfg = None
        support_ins = SupportInserter(slug, support_cfg) if support_cfg else None

        for start, end in page_ranges:
            batch_out_dir = os.path.join(paths["batches_dir"], f"batch_{start}_{end}")
            marker_out_subdir = os.path.join(batch_out_dir, pdf_basename)
            md_file = os.path.join(marker_out_subdir, f"{pdf_basename}{suffix}.md")
            
            if not os.path.exists(md_file):
                log(f"Warning: Expected batch markdown file '{md_file}' not found. Skipping in merge.", log_path)
                continue
                
            # Copy images
            if os.path.exists(marker_out_subdir):
                for item in os.listdir(marker_out_subdir):
                    if item.lower().endswith((".jpeg", ".jpg", ".png", ".gif")):
                        src_img = os.path.join(marker_out_subdir, item)
                        dst_img = os.path.join(paths["translated_dir"], item)
                        shutil.copy2(src_img, dst_img)
                        
            # Read content
            with open(md_file, "r", encoding="utf-8") as f:
                batch_text = f.read()

            if support_ins:
                # Counter first, decision after: the banner lands at the
                # boundary AFTER ~50-70 accumulated pages, right before
                # this batch's text - i.e. at the previous batch's end.
                if support_ins.due() and merged_md_content \
                        and not is_heavy_scene(merged_md_content[-1]):
                    merged_md_content.append(render_md_block(support_cfg))
                    support_ins.mark_inserted()
                    log(f"Support banner inserted before pages {start}-{end} "
                        f"(#{support_ins.inserted_count}).", log_path)
                support_ins.advance(end - start + 1)

            merged_md_content.append(batch_text)

        if support_ins:
            log(f"Support banners: {support_ins.inserted_count} inserted.", log_path)

        with open(final_merged_md_path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(merged_md_content))
            
        log(f"Merged markdown saved to: {final_merged_md_path}", log_path)
    
    # 4. Convert merged markdown to EPUB and AZW3 using Calibre's ebook-convert
    if not args.no_ebook:
        lang_code = paths["target_lang"] if suffix else paths["source_lang"]
        lang_locale = "ru_RU.UTF-8" if lang_code == "ru" else f"{lang_code}_{lang_code.upper()}.UTF-8"
        
        epub_out_path = os.path.join(paths["output_dir"], f"{slug}_translated_{lang_code}.epub" if suffix else f"{slug}_source_{lang_code}.epub")
        azw3_out_path = os.path.join(paths["output_dir"], f"{slug}_translated_{lang_code}.azw3" if suffix else f"{slug}_source_{lang_code}.azw3")
        
        # EPUB conversion
        log(f"Converting merged markdown to EPUB: {epub_out_path}...", log_path)
        cmd_epub = [
            "proot-distro", "login", "ubuntu", "--",
            "env",
            f"LANG={lang_locale}",
            f"LC_ALL={lang_locale}",
            "ebook-convert",
            final_merged_md_path,
            epub_out_path,
            "--epub-version=2",
            f"--language={lang_code}",
            f"--title={paths['title']}",
            f"--authors={paths['authors']}"
        ]
        if os.path.exists(paths["cover_path"]):
            cmd_epub.append(f"--cover={paths['cover_path']}")
            
        env = os.environ.copy()
        env["LANG"] = lang_locale
        env["LC_ALL"] = lang_locale
        
        success_epub = run_command_streaming(cmd_epub, log_path, prefix="[Calibre EPUB] ", env=env)
        if not success_epub or not os.path.exists(epub_out_path):
            log("Error: Calibre EPUB generation failed!", log_path)
            sys.exit(1)
            
        # EPUB Validation
        def epub_log(msg):
            log(msg, log_path)
            
        if not validate_epub(epub_out_path, epub_log):
            log("Error: Generated EPUB failed post-generation validation!", log_path)
            sys.exit(1)
            
        log("EPUB generated and validated successfully.", log_path)
        
        # AZW3 conversion
        log(f"Converting merged markdown to AZW3: {azw3_out_path}...", log_path)
        cmd_azw = [
            "proot-distro", "login", "ubuntu", "--",
            "env",
            f"LANG={lang_locale}",
            f"LC_ALL={lang_locale}",
            "ebook-convert",
            final_merged_md_path,
            azw3_out_path,
            f"--language={lang_code}",
            f"--title={paths['title']}",
            f"--authors={paths['authors']}"
        ]
        if os.path.exists(paths["cover_path"]):
            cmd_azw.append(f"--cover={paths['cover_path']}")
            
        success_azw = run_command_streaming(cmd_azw, log_path, prefix="[Calibre AZW3] ", env=env)
        if not success_azw or not os.path.exists(azw3_out_path):
            log("Error: Calibre AZW3 generation failed!", log_path)
            sys.exit(1)
            
        log("AZW3 generated successfully.", log_path)
        
        # Copy to configured library folder
        book_output_dir = paths["book_output_dir"]
        os.makedirs(book_output_dir, exist_ok=True)
        
        title_safe = "".join(c for c in paths["title"] if c.isalnum() or c in (' ', '-', '_')).strip()
        lang_code = paths["target_lang"] if suffix else paths["source_lang"]
        
        try:
            epub_dest = os.path.join(book_output_dir, f"{title_safe}_{lang_code}.epub")
            azw3_dest = os.path.join(book_output_dir, f"{title_safe}_{lang_code}.azw3")
            shutil.copy2(epub_out_path, epub_dest)
            shutil.copy2(azw3_out_path, azw3_dest)
            log(f"Copied EPUB/AZW3 files to {book_output_dir}.", log_path)
        except Exception as e:
            log(f"Warning: Failed to copy outputs to {book_output_dir}: {e}", log_path)
                
    # 5. Audiobook generation
    if paths["generate_audiobook"] and not args.no_audio:
        log("Triggering audio stage synthesis...", log_path)
        send_heartbeat(slug, "старт", stage="озвучення")
        cmd_audio = [
            "python3", os.path.join(repo_dir, "audio_stage.py"),
            "--book", slug
        ]
        if args.config:
            cmd_audio.extend(["--config", args.config])
        success_audio = run_command_streaming(cmd_audio, log_path, prefix="[Audio] ")
        if not success_audio:
            log("Error: Audiobook generation stage failed!", log_path)
            sys.exit(1)
            
        # Copy MP3 to configured library audio folder
        lang_code = paths["target_lang"] if suffix else paths["source_lang"]
        mp3_filename = f"{slug}_translated_{lang_code}.mp3"
        mp3_out_path = os.path.join(paths["output_dir"], mp3_filename)
        
        book_output_dir = paths["book_output_dir"]
        audio_dest_dir = os.path.join(book_output_dir, "audio")
        os.makedirs(audio_dest_dir, exist_ok=True)
        
        if os.path.exists(mp3_out_path):
            try:
                title_safe = "".join(c for c in paths["title"] if c.isalnum() or c in (' ', '-', '_')).strip()
                mp3_dest = os.path.join(audio_dest_dir, f"{title_safe}_{lang_code}.mp3")
                shutil.copy2(mp3_out_path, mp3_dest)
                log(f"Copied MP3 audiobook file to {audio_dest_dir}.", log_path)
            except Exception as e:
                log(f"Warning: Failed to copy audiobook to {audio_dest_dir}: {e}", log_path)

    # 6. Agent Editor stage (if enabled for this book)
    if paths.get("enable_agent_editor"):
        log("Triggering Agent Editor vision scan...", log_path)
        send_heartbeat(slug, "старт", stage="агент-редактор")
        agent_script = os.path.join(repo_dir, "bin", "agent_editor.py")
        if os.path.exists(agent_script):
            cmd_agent = ["python3", agent_script, "--book", slug]
            success_agent = run_command_streaming(cmd_agent, log_path, prefix="[AgentEditor] ")
            if not success_agent:
                log("Warning: Agent Editor scan skipped or encountered non-fatal issues.", log_path)
        else:
            log("Warning: Agent Editor script bin/agent_editor.py not found.", log_path)
                
    log("=== Pipeline run fully completed successfully! ===", log_path)

if __name__ == "__main__":
    main()
