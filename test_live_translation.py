#!/usr/bin/env python3
import subprocess
import os
import sys

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_path = os.path.join(script_dir, "test_live_input.md")
    new_output_path = os.path.join(script_dir, "test_live_output_new.md")
    old_output_path = os.path.join(script_dir, "test_live_output_old.md")
    new_cache_path = os.path.join(script_dir, "test_live_cache_new.json")
    old_cache_path = os.path.join(script_dir, "test_live_cache_old.json")
    
    # 1. Define test paragraphs
    paragraphs = [
        # Para 1: Code and math formula block (from Task 1)
        "```\n* Основная часть программы\nLOADI BUFFER / Загрузка адреса BUFFER в регистр счетчика (пример псевдокоманды, фактическая команда зависит от ассемблера)\n* Резервирование пространства, заполнение нулями\nZBLOCK 16 / Выделение блока длиной 16 байт для инициализации нулями\n* Пример условной ассемблерной инструкции\nIFDEF BUFFER / Если BUFFER уже определен, то выполняется следующее\n TAD BUFFER\nENDIF\n* Конец ассемблера\n$\n```",
        # Para 2: Inline HTML tags
        "## <span id=\"page-9-0\"></span>**Предисловие от издательства**",
        # Para 3: Markdown link
        "Дизайн обложки разработан с использованием ресурса [magnific.com](http://magnific.com)"
    ]
    
    # Write input file
    with open(input_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(paragraphs))
        
    print(f"Created live translation input at {input_path}")
    
    # Clean old caches if any
    for path in [new_output_path, old_output_path, new_cache_path, old_cache_path]:
        if os.path.exists(path):
            os.remove(path)
            
    # 2. Run new translate_stage.py
    print("\n>>> Running refactored translate_stage.py...")
    cmd_new = [
        sys.executable,
        os.path.join(script_dir, "translate_stage.py"),
        "-i", input_path,
        "-o", new_output_path,
        "--cache", new_cache_path,
        "--api-url", "http://localhost:8081/v1/chat/completions"
    ]
    res_new = subprocess.run(cmd_new, capture_output=True, text=True)
    print("STDOUT:")
    print(res_new.stdout)
    print("STDERR:")
    print(res_new.stderr)
    
    if res_new.returncode != 0:
        print("Error: Refactored translate_stage.py failed!")
        sys.exit(1)
        
    # 3. Run old translate_stage.py
    print("\n>>> Running original translate_stage.py...")
    cmd_old = [
        sys.executable,
        "/data/data/com.termux/files/home/translate_stage.py",
        "-i", input_path,
        "-o", old_output_path,
        "--cache", old_cache_path,
        "--api-url", "http://localhost:8081/v1/chat/completions"
    ]
    res_old = subprocess.run(cmd_old, capture_output=True, text=True)
    print("STDOUT:")
    print(res_old.stdout)
    print("STDERR:")
    print(res_old.stderr)
    
    if res_old.returncode != 0:
        print("Error: Original translate_stage.py failed!")
        sys.exit(1)
        
    # Read outputs
    with open(new_output_path, "r", encoding="utf-8") as f:
        new_out = f.read()
    with open(old_output_path, "r", encoding="utf-8") as f:
        old_out = f.read()
        
    print("\n=============================================")
    print("ORIGINAL INPUT:")
    print("=============================================")
    print("\n\n".join(paragraphs))
    print("\n=============================================")
    print("REFACTORED translate_stage.py OUTPUT:")
    print("=============================================")
    print(new_out)
    print("\n=============================================")
    print("ORIGINAL translate_stage.py OUTPUT:")
    print("=============================================")
    print(old_out)
    print("=============================================")
    
    # Check if placeholders were restored in the outputs (they shouldn't contain any '__' placeholders)
    placeholders_in_new = [x for x in new_out.split() if "__" in x]
    placeholders_in_old = [x for x in old_out.split() if "__" in x]
    
    success = True
    if placeholders_in_new:
        print(f"FAIL: Refactored output contains unrestored placeholders: {placeholders_in_new}")
        success = False
    else:
        print("SUCCESS: Refactored output contains NO unrestored placeholders!")
        
    if placeholders_in_old:
        print(f"WARNING: Original output contains unrestored placeholders: {placeholders_in_old}")
    else:
        print("SUCCESS: Original output contains NO unrestored placeholders!")
        
    # Check if HTML tag was preserved
    if "<span id=\"page-9-0\"></span>" in new_out:
        print("SUCCESS: HTML tag structure is preserved in new output!")
    else:
        print("FAIL: HTML tag structure is NOT preserved in new output!")
        success = False
        
    # Check if headers are preserved
    if new_out.count("#") == old_out.count("#"):
        print("SUCCESS: Header counts match between new and old outputs!")
    else:
        print(f"WARNING: Header count mismatch: New={new_out.count('#')}, Old={old_out.count('#')}")
        
    if success:
        print("\nLIVE TRANSLATION TEST COMPLETED SUCCESSFULLY!")
        sys.exit(0)
    else:
        print("\nLIVE TRANSLATION TEST FAILED!")
        sys.exit(1)

if __name__ == "__main__":
    main()
