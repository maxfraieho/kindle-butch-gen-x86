#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import re
import json
import hashlib
import argparse
import requests
import zipfile
import shutil
import tempfile
import xml.etree.ElementTree as ET
from common.text_protect import PlaceholderManager
from common.epub_validate import sanitize_xhtml_for_xml_parser
from common.book_paths import resolve_book_paths
from common.utils import get_hash, to_xml_format, wait_for_server_ready, translate_segment_with_retry


def translate_epub_images(temp_dir, source_lang, repo_dir):
    # Walk temp_dir and find all images
    image_extensions = [".png", ".jpg", ".jpeg", ".webp"]
    images_to_translate = []
    
    for root, dirs, files in os.walk(temp_dir):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in image_extensions:
                # Skip cover images to avoid translating cover text
                if "cover" in file.lower():
                    continue
                images_to_translate.append(os.path.join(root, file))
                
    if not images_to_translate:
        log("No illustration images found to translate.")
        return
        
    log(f"Found {len(images_to_translate)} illustration image(s) to process via Manga Translator.")
    
    # Process each image using translate_manga.py inside PRoot container
    for img_idx, img_path in enumerate(images_to_translate):
        log(f"Processing image {img_idx+1}/{len(images_to_translate)}: {os.path.basename(img_path)}")
        
        # Create a temporary output dir for this image
        out_dir = os.path.join(os.path.dirname(img_path), "manga_out_temp")
        os.makedirs(out_dir, exist_ok=True)
        
        cmd = [
            "proot-distro", "login", "ubuntu", "--",
            "python3", os.path.join(repo_dir, "translate_manga.py"),
            "--input", img_path,
            "--output", out_dir,
            "--lang", source_lang
        ]
        
        try:
            import subprocess
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                basename = os.path.basename(img_path)
                processed_img = os.path.join(out_dir, basename)
                if os.path.exists(processed_img):
                    shutil.copy2(processed_img, img_path)
                    log(f"  Successfully replaced illustration: {basename}")
                else:
                    files_in_out = os.listdir(out_dir)
                    if files_in_out:
                        shutil.copy2(os.path.join(out_dir, files_in_out[0]), img_path)
                        log(f"  Replaced illustration with: {files_in_out[0]}")
            else:
                log(f"  Warning: Manga translator failed on image: {res.stderr}")
        except Exception as e:
            log(f"  Warning: Failed to run manga translator on image: {e}")
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


def log(message):
    print(f"[EPUB-Translate] {message}", flush=True)




def register_namespaces():
    # Register namespaces to prevent ElementTree from generating ns0: tags
    ET.register_namespace('', 'http://www.w3.org/1999/xhtml')
    ET.register_namespace('opf', 'http://www.idpf.org/2007/opf')
    ET.register_namespace('dc', 'http://purl.org/dc/elements/1.1/')
    ET.register_namespace('container', 'urn:oasis:names:tc:opendocument:xmlns:container')

def parse_args():
    parser = argparse.ArgumentParser(description="Direct EPUB Translation pipeline tool")
    parser.add_argument("--input", "-i", required=False, help="Path to input EPUB file")
    parser.add_argument("--output", "-o", required=False, help="Path to output translated EPUB file")
    parser.add_argument("--target-lang", "-t", default="uk", help="Target language (default: uk)")
    parser.add_argument("--api-url", default="http://localhost:8081/v1/chat/completions", help="Llama-server API URL")
    parser.add_argument("--cache", default=None, help="Path to JSON cache file")
    parser.add_argument("--book", "-b", help="Book slug")
    parser.add_argument("--config", "-c", help="Book configuration JSON path")
    return parser.parse_args()

def main():
    args = parse_args()
    register_namespaces()
    
    if not args.input and not args.book and not args.config:
        print("Error: At least one of --input, --book, or --config must be specified.")
        sys.exit(1)
        
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    slug = args.book
    if not slug and args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                slug = cfg.get("slug")
        except Exception:
            pass
    if not slug:
        slug = "default-book"
        
    paths = resolve_book_paths(repo_dir, slug, config_path=args.config)
    
    input_path = args.input
    if not input_path:
        input_path = os.path.join(paths["book_dir"], "input", "input.epub")
        
    output_path = args.output
    if not output_path:
        output_path = os.path.join(paths["output_dir"], "output.epub")
        
    cache_path = args.cache
    if not cache_path:
        if args.book or args.config:
            cache_path = paths["translate_cache"]
        else:
            cache_path = "progress_translate_epub_uk.json"
            
    target_lang = args.target_lang
    if paths.get("target_lang"):
        target_lang = paths["target_lang"]
        
    # Assign back to args namespace
    args.input = input_path
    args.output = output_path
    args.cache = cache_path
    args.target_lang = target_lang
    
    # Ensure directories exist
    os.makedirs(os.path.dirname(os.path.abspath(args.input)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.cache)), exist_ok=True)
    
    if not os.path.exists(args.input):
        log(f"Error: Input file '{args.input}' does not exist.")
        sys.exit(1)
        
    # Check server availability and wait for model loading
    if not wait_for_server_ready(args.api_url):
        log("Error: Translation server did not become ready in time.")
        sys.exit(1)
        
    # Load cache
    cache = {}
    if os.path.exists(args.cache):
        try:
            with open(args.cache, "r", encoding="utf-8") as cf:
                cache = json.load(cf)
            log(f"Loaded cache from {args.cache} with {len(cache)} entries.")
        except Exception as e:
            log(f"Warning: Failed to load cache: {e}. Starting fresh.")
            
    # Unpack EPUB to temp folder inside book directory (accessible to PRoot container)
    temp_dir = os.path.join(paths["book_dir"], "temp_epub_trans")
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir, exist_ok=True)
    log(f"Extracting EPUB to temporary directory: {temp_dir}")
    try:
        with zipfile.ZipFile(args.input, "r") as z:
            z.extractall(temp_dir)
    except Exception as e:
        log(f"Error: Failed to extract EPUB file: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        sys.exit(1)
        
    # Locate OPF file via container.xml
    container_path = os.path.join(temp_dir, "META-INF", "container.xml")
    if not os.path.exists(container_path):
        log("Error: META-INF/container.xml not found!")
        shutil.rmtree(temp_dir)
        sys.exit(1)
        
    try:
        tree = ET.parse(container_path)
        root = tree.getroot()
        root_file_el = root.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
        if root_file_el is None:
            # try finding ignoring namespace
            root_file_el = root.find(".//rootfile")
        if root_file_el is None:
            raise ValueError("No rootfile element found in container.xml")
        opf_rel_path = root_file_el.attrib["full-path"]
        opf_path = os.path.join(temp_dir, opf_rel_path)
        log(f"OPF file located: {opf_path}")
    except Exception as e:
        log(f"Error: Failed to parse container.xml: {e}")
        shutil.rmtree(temp_dir)
        sys.exit(1)
        
    # Parse OPF to list HTML/XHTML files
    opf_dir = os.path.dirname(opf_path)
    try:
        opf_tree = ET.parse(opf_path)
        opf_root = opf_tree.getroot()
        
        # Find manifest items
        manifest_el = opf_root.find(".//{http://www.idpf.org/2007/opf}manifest")
        if manifest_el is None:
            manifest_el = opf_root.find(".//manifest")
            
        if manifest_el is None:
            raise ValueError("Manifest element not found in OPF")
            
        xhtml_items = []
        for item in manifest_el.findall(".//{http://www.idpf.org/2007/opf}item"):
            href = item.attrib.get("href")
            media_type = item.attrib.get("media-type", "")
            if href and media_type in ["application/xhtml+xml", "text/html"]:
                xhtml_items.append(href)
                
        log(f"Found {len(xhtml_items)} HTML/XHTML file(s) in EPUB manifest.")
    except Exception as e:
        log(f"Error: Failed to parse OPF file: {e}")
        shutil.rmtree(temp_dir)
        sys.exit(1)
        
    # Translate XHTML files
    block_tags = [
        "{http://www.w3.org/1999/xhtml}p", "{http://www.w3.org/1999/xhtml}li",
        "{http://www.w3.org/1999/xhtml}h1", "{http://www.w3.org/1999/xhtml}h2",
        "{http://www.w3.org/1999/xhtml}h3", "{http://www.w3.org/1999/xhtml}h4",
        "{http://www.w3.org/1999/xhtml}h5", "{http://www.w3.org/1999/xhtml}h6",
        "{http://www.w3.org/1999/xhtml}blockquote", "{http://www.w3.org/1999/xhtml}td",
        "{http://www.w3.org/1999/xhtml}th",
        # Fallbacks for namespace-less tags if parsing doesn't match namespace
        "p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "td", "th"
    ]
    
    total_translated_blocks = 0
    total_cached_blocks = 0
    
    # Count total block elements to translate
    total_blocks = 0
    for item_href in xhtml_items:
        f_path = os.path.join(opf_dir, item_href)
        if os.path.exists(f_path):
            try:
                with open(f_path, "rb") as f:
                    r_bytes = f.read()
                sanitized_xml = sanitize_xhtml_for_xml_parser(r_bytes)
                h_root = ET.fromstring(sanitized_xml.encode('utf-8'))
                for el in h_root.iter():
                    if el.tag in block_tags:
                        text = el.text or ''
                        children_str = ''.join(ET.tostring(child, encoding='utf-8').decode('utf-8') for child in el)
                        inner_xml = text + children_str
                        if inner_xml.strip():
                            total_blocks += 1
            except Exception:
                pass
    log(f"Total block elements to translate across all files: {total_blocks}")
    
    completed_blocks_count = 0
    
    for item_idx, item_href in enumerate(xhtml_items):
        file_path = os.path.join(opf_dir, item_href)
        log(f"Translating file {item_idx+1}/{len(xhtml_items)}: {item_href}")
        
        if not os.path.exists(file_path):
            log(f"Warning: File {file_path} not found! Skipping.")
            continue
            
        try:
            with open(file_path, "rb") as f:
                raw_bytes = f.read()
                
            sanitized = sanitize_xhtml_for_xml_parser(raw_bytes)
            html_root = ET.fromstring(sanitized.encode('utf-8'))
            
            # Find and translate block elements
            modified = False
            
            # Walk all elements in the tree
            for el in html_root.iter():
                if el.tag in block_tags:
                    # Get inner XML content
                    text = el.text or ''
                    children_str = ''.join(ET.tostring(child, encoding='utf-8').decode('utf-8') for child in el)
                    inner_xml = text + children_str
                    
                    if not inner_xml.strip():
                        continue
                        
                    completed_blocks_count += 1
                    try:
                        prog_path = os.path.join(paths["cache_dir"], "epub_progress.json")
                        with open(prog_path, "w", encoding="utf-8") as pf:
                            json.dump({
                                "current_file": item_idx + 1,
                                "total_files": len(xhtml_items),
                                "percent": round((completed_blocks_count / total_blocks) * 100.0, 1) if total_blocks > 0 else 0.0,
                                "completed_blocks": completed_blocks_count,
                                "total_blocks": total_blocks
                            }, pf)
                    except Exception:
                        pass
                        
                    # Calculate hash
                    h = get_hash(inner_xml)
                    
                    if h in cache:
                        translated_inner_xml = cache[h]
                        total_cached_blocks += 1
                    else:
                        log(f"  Translating block ({len(inner_xml)} chars)...")
                        # Protect tags
                        pm = PlaceholderManager()
                        protected = pm.protect(inner_xml)
                        
                        # Translate
                        translated_protected = translate_segment_with_retry(protected, pm, args.api_url, target_lang=args.target_lang, source_lang=paths["source_lang"], book_dir=paths["book_dir"])
                        
                        if not translated_protected:
                            raise ValueError("Critical: Failed to translate block after all attempts.")
                        else:
                            translated_inner_xml = pm.restore(translated_protected)
                            
                        # Store in cache
                        cache[h] = translated_inner_xml
                        total_translated_blocks += 1
                        
                        # Save cache periodically
                        if total_translated_blocks % 5 == 0:
                            try:
                                with open(args.cache, "w", encoding="utf-8") as cf:
                                    json.dump(cache, cf, ensure_ascii=False, indent=2)
                            except Exception as e:
                                log(f"Warning: Failed to save cache: {e}")
                                
                    # Reconstruct element content from translated inner XML
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
                        modified = True
                    except Exception as e:
                        log(f"  Warning: Failed to parse translated XML for block: {e}. Falling back to plain text.")
                        plain_text = re.sub(r'<[^>]+>', '', translated_inner_xml)
                        el.text = None
                        el.tail = None
                        for child in list(el):
                            el.remove(child)
                        el.text = plain_text
                        modified = True
                        
            if modified:
                # Write XHTML file back
                xhtml_tree = ET.ElementTree(html_root)
                # XHTML files need a doc-type sometimes, but writing valid XML is enough.
                # Writing with utf-8 encoding and xml_declaration
                xhtml_tree.write(file_path, encoding="utf-8", xml_declaration=True)
                
        except Exception as e:
            log(f"Error translating file {item_href}: {e}")
            
    # Save final cache
    try:
        with open(args.cache, "w", encoding="utf-8") as cf:
            json.dump(cache, cf, ensure_ascii=False, indent=2)
        log(f"Saved final cache to {args.cache}. Total blocks translated: {total_translated_blocks}, Cached: {total_cached_blocks}")
    except Exception as e:
        log(f"Warning: Failed to save final cache: {e}")
        
    # Update language metadata in OPF file
    try:
        language_el = opf_root.find(".//{http://purl.org/dc/elements/1.1/}language")
        if language_el is None:
            language_el = opf_root.find(".//language")
        if language_el is not None:
            language_el.text = args.target_lang
            log(f"OPF language code updated to '{args.target_lang}'")
            # Write OPF back
            opf_tree.write(opf_path, encoding="utf-8", xml_declaration=True)
    except Exception as e:
        log(f"Warning: Failed to update language metadata in OPF file: {e}")
        
    # Translate illustrations/images using Manga Translator
    translate_epub_images(temp_dir, paths.get("source_lang", "en"), repo_dir)
        
    # Re-pack EPUB to final output path (Kindle-compatible ZIP layout)
    log(f"Re-packaging translated EPUB to: {args.output}")
    if os.path.exists(args.output):
        os.remove(args.output)
        
    try:
        with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as z:
            # Rule 1: mimetype must be the first file and ZIP_STORED (uncompressed)
            mimetype_src = os.path.join(temp_dir, "mimetype")
            if os.path.exists(mimetype_src):
                z.write(mimetype_src, "mimetype", compress_type=zipfile.ZIP_STORED)
            else:
                z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
                
            # Write all other files
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, temp_dir)
                    if rel_path != "mimetype":
                        z.write(full_path, rel_path)
                        
        log("EPUB re-packaged successfully!")
    except Exception as e:
        log(f"Error: Failed to re-package EPUB: {e}")
    finally:
        shutil.rmtree(temp_dir)
        
    try:
        prog_path = os.path.join(paths["cache_dir"], "epub_progress.json")
        with open(prog_path, "w", encoding="utf-8") as pf:
            json.dump({
                "current_file": len(xhtml_items),
                "total_files": len(xhtml_items),
                "percent": 100.0
            }, pf)
    except Exception:
        pass
        
    log("=== EPUB TRANSLATION PIPELINE FULLY COMPLETED ===")

if __name__ == "__main__":
    main()
