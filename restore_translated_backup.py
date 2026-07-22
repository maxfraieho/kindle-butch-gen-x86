import os
import re
import zipfile
import shutil

def natural_keys(text):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]

book_dir = "/data/data/com.termux/files/home/kindle-butch-gen/books/frieren"
source_dir = os.path.join(book_dir, "source")
translated_dir = os.path.join(book_dir, "translated")
epub_path = "/tmp/frieren_extracted.epub"
temp_extract_dir = "/tmp/frieren_epub_unpacked"

# 1. Unpack epub
if os.path.exists(temp_extract_dir):
    shutil.rmtree(temp_extract_dir)
os.makedirs(temp_extract_dir, exist_ok=True)

print(f"Extracting EPUB from {epub_path}...")
with zipfile.ZipFile(epub_path, "r") as z:
    z.extractall(temp_extract_dir)

# 2. Get list of images in unpacked epub
images_dir = os.path.join(temp_extract_dir, "images")
epub_images = sorted([f for f in os.listdir(images_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))], key=natural_keys)

# 3. Get list of original files inside source folder
cbz_files = sorted([f for f in os.listdir(source_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))], key=natural_keys)

print(f"EPUB images count: {len(epub_images)}")
print(f"CBZ files count: {len(cbz_files)}")

if len(epub_images) != len(cbz_files):
    print("Warning: counts do not match exactly! Proceeding anyway...")

# Clean current translated folder so we don't have mixed English/Ukrainian pages
if os.path.exists(translated_dir):
    shutil.rmtree(translated_dir)
os.makedirs(translated_dir, exist_ok=True)

# 4. Map and copy
for i in range(min(len(epub_images), len(cbz_files))):
    epub_img_name = epub_images[i]
    cbz_img_name = os.path.basename(cbz_files[i])
    
    src_path = os.path.join(images_dir, epub_img_name)
    dest_path = os.path.join(translated_dir, cbz_img_name)
    shutil.copy2(src_path, dest_path)

print(f"Successfully restored {min(len(epub_images), len(cbz_files))} translated pages back to {translated_dir}!")
