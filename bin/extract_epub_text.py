#!/usr/bin/env python3
import os
import sys
import zipfile
import re
import xml.etree.ElementTree as ET
import argparse

# Add parent dir to path so we can import from common
repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo_dir)

from common.epub_validate import sanitize_xhtml_for_xml_parser

def extract_text_from_node(node, zip_file=None, opf_dir=None, output_dir=None):
    """Recursively extract text from an XML/HTML node, embedding image markdown tags."""
    tag_name = node.tag.split('}')[-1]
    
    if tag_name == 'img':
        src = node.attrib.get('src')
        if src:
            alt = node.attrib.get('alt', '').strip()
            src_basename = os.path.basename(src)
            # Extract image file from zip
            if zip_file and opf_dir is not None and output_dir:
                try:
                    img_rel_path = os.path.normpath(os.path.join(opf_dir, src))
                    img_data = zip_file.read(img_rel_path)
                    dest_img_path = os.path.join(output_dir, src_basename)
                    with open(dest_img_path, "wb") as img_f:
                        img_f.write(img_data)
                except Exception as e:
                    print(f"Warning: Failed to extract image {src}: {e}")
            return f"\n\n![{alt}]({src_basename})\n\n"
        return ""

    if tag_name == 'image':
        href = node.attrib.get('{http://www.w3.org/1999/xlink}href') or node.attrib.get('href')
        if href:
            href_basename = os.path.basename(href)
            # Extract image file from zip
            if zip_file and opf_dir is not None and output_dir:
                try:
                    img_rel_path = os.path.normpath(os.path.join(opf_dir, href))
                    img_data = zip_file.read(img_rel_path)
                    dest_img_path = os.path.join(output_dir, href_basename)
                    with open(dest_img_path, "wb") as img_f:
                        img_f.write(img_data)
                except Exception as e:
                    print(f"Warning: Failed to extract image {href}: {e}")
            return f"\n\n![]({href_basename})\n\n"
        return ""

    texts = []
    if node.text:
        texts.append(node.text)
    for child in node:
        texts.append(extract_text_from_node(child, zip_file, opf_dir, output_dir))
        if child.tail:
            texts.append(child.tail)
    return "".join(texts)

def walk_and_extract(node, blocks, zip_file=None, opf_dir=None, output_dir=None):
    tag_name = node.tag.split('}')[-1]
    
    if tag_name == 'img':
        img_text = extract_text_from_node(node, zip_file, opf_dir, output_dir).strip()
        if img_text:
            blocks.append(img_text)
        return

    if tag_name == 'image':
        img_text = extract_text_from_node(node, zip_file, opf_dir, output_dir).strip()
        if img_text:
            blocks.append(img_text)
        return

    is_block = tag_name in ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'pre', 'blockquote']
    
    # Check if a div has text and no nested blocks
    if tag_name == 'div':
        has_block_children = any(
            c.tag.split('}')[-1] in ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'pre', 'blockquote', 'div']
            for c in node
        )
        if not has_block_children and node.text and node.text.strip():
            is_block = True

    if is_block:
        text = extract_text_from_node(node, zip_file, opf_dir, output_dir).strip()
        if text:
            if tag_name.startswith('h'):
                try:
                    level = int(tag_name[1])
                except ValueError:
                    level = 3
                blocks.append(f"{'#' * level} {text}")
            elif tag_name == 'li':
                blocks.append(f"- {text}")
            elif tag_name == 'pre':
                blocks.append(f"```\n{text}\n```")
            else:
                blocks.append(text)
        return

    for child in node:
        walk_and_extract(child, blocks, zip_file, opf_dir, output_dir)

def extract_epub_to_markdown(epub_path, output_md_path):
    print(f"Extracting text from EPUB: {epub_path}...")
    if not os.path.exists(epub_path):
        print(f"Error: EPUB file {epub_path} does not exist.")
        sys.exit(1)

    with zipfile.ZipFile(epub_path, 'r') as z:
        # Locate OPF path
        try:
            container_content = z.read("META-INF/container.xml")
            container_root = ET.fromstring(container_content)
            root_file_el = container_root.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
            if root_file_el is None:
                root_file_el = container_root.find(".//rootfile")
            opf_rel_path = root_file_el.attrib["full-path"]
        except Exception as e:
            print(f"Error parsing container.xml: {e}")
            sys.exit(1)

        opf_dir = os.path.dirname(opf_rel_path)

        # Parse OPF
        try:
            opf_content = z.read(opf_rel_path)
            opf_root = ET.fromstring(opf_content)
        except Exception as e:
            print(f"Error parsing OPF file: {e}")
            sys.exit(1)

        # Manifest mapping
        manifest_el = opf_root.find(".//{http://www.idpf.org/2007/opf}manifest")
        if manifest_el is None:
            manifest_el = opf_root.find(".//manifest")

        manifest = {}
        for item in manifest_el.findall(".//{http://www.idpf.org/2007/opf}item"):
            item_id = item.attrib.get("id")
            href = item.attrib.get("href")
            manifest[item_id] = href
        if not manifest:
            for item in manifest_el.findall(".//item"):
                item_id = item.attrib.get("id")
                href = item.attrib.get("href")
                manifest[item_id] = href

        # Spine items
        spine_el = opf_root.find(".//{http://www.idpf.org/2007/opf}spine")
        if spine_el is None:
            spine_el = opf_root.find(".//spine")

        item_refs = []
        for itemref in spine_el.findall(".//{http://www.idpf.org/2007/opf}itemref"):
            idref = itemref.attrib.get("idref")
            item_refs.append(idref)
        if not item_refs:
            for itemref in spine_el.findall(".//itemref"):
                idref = itemref.attrib.get("idref")
                item_refs.append(idref)

        output_dir = os.path.dirname(os.path.abspath(output_md_path))
        os.makedirs(output_dir, exist_ok=True)

        markdown_blocks = []
        for idref in item_refs:
            href = manifest.get(idref)
            if not href:
                continue

            html_rel_path = os.path.normpath(os.path.join(opf_dir, href))
            try:
                html_bytes = z.read(html_rel_path)
            except KeyError:
                print(f"Warning: Spine file {html_rel_path} not found in zip.")
                continue

            sanitized_html = sanitize_xhtml_for_xml_parser(html_bytes)
            try:
                html_root = ET.fromstring(sanitized_html.encode('utf-8'))
            except Exception as e:
                print(f"Warning: Failed to parse XML for {html_rel_path}: {e}")
                continue

            body_el = html_root.find(".//{http://www.w3.org/1999/xhtml}body")
            if body_el is None:
                body_el = html_root.find(".//body")
            if body_el is None:
                body_el = html_root

            walk_and_extract(body_el, markdown_blocks, zip_file=z, opf_dir=opf_dir, output_dir=output_dir)

        output_content = "\n\n".join(markdown_blocks)
        with open(output_md_path, "w", encoding="utf-8") as f:
            f.write(output_content)
        print(f"Successfully extracted markdown to: {output_md_path}")

def main():
    parser = argparse.ArgumentParser(description="Extract EPUB text content to Markdown file")
    parser.add_argument("--input", "-i", required=True, help="Path to input EPUB file")
    parser.add_argument("--output", "-o", required=True, help="Path to output Markdown file")
    args = parser.parse_args()
    extract_epub_to_markdown(args.input, args.output)

if __name__ == "__main__":
    main()
