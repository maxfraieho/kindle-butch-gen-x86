import os
import zipfile
import re
import xml.etree.ElementTree as ET
from html.entities import name2codepoint

def sanitize_xhtml_for_xml_parser(xml_content_bytes):
    content_str = xml_content_bytes.decode('utf-8', errors='ignore')
    def replace_entity(match):
        entity = match.group(1)
        if entity in ['amp', 'lt', 'gt', 'quot', 'apos']:
            return match.group(0)
        if entity.startswith('#'):
            return match.group(0)
        if entity in name2codepoint:
            return f"&#{name2codepoint[entity]};"
        return match.group(0)
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
