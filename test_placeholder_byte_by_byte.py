#!/usr/bin/env python3
import os
import sys
import importlib.util

# Add the kindle-butch-gen directory to sys.path
script_dir = os.path.dirname(os.path.abspath(__file__))
kb_gen_dir = script_dir
if kb_gen_dir not in sys.path:
    sys.path.insert(0, kb_gen_dir)

# 1. Load the original PlaceholderManager dynamically from translate_epub.py
translate_epub_path = os.path.abspath(os.path.join(script_dir, "..", "translate_epub.py"))
print(f"Loading original PlaceholderManager from {translate_epub_path}...")
spec = importlib.util.spec_from_file_location("translate_epub", translate_epub_path)
translate_epub = importlib.util.module_from_spec(spec)
sys.modules["translate_epub"] = translate_epub
spec.loader.exec_module(translate_epub)
OldPlaceholderManager = translate_epub.PlaceholderManager

# 2. Import the new PlaceholderManager
print("Importing new PlaceholderManager from common.text_protect...")
from common.text_protect import PlaceholderManager as NewPlaceholderManager

# 3. Find paragraphs in the markdown file
md_path = os.path.abspath(os.path.join(
    script_dir,
    "..",
    "vibe_markdown_batches",
    "batch_0_49",
    "Ван Вэньцзе - Вайб-программирование - 2026",
    "Ван Вэньцзе - Вайб-программирование - 2026.md"
))
test_paragraphs = []

if os.path.exists(md_path):
    print(f"Reading markdown file: {md_path}")
    try:
        with open(md_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # Split by paragraphs (double newlines or Windows style)
        paragraphs = [p.strip() for p in content.split('\n\n') if p.strip()]
        
        # Look for paragraphs containing code (`...`) and math ($...$)
        for p in paragraphs:
            has_code = '`' in p
            has_math = '$' in p
            if has_code and has_math:
                test_paragraphs.append(p)
        print(f"Found {len(test_paragraphs)} paragraphs with both code and math.")
    except Exception as e:
        print(f"Error reading markdown file: {e}")
else:
    print(f"Markdown file not found at {md_path}")

# Always add fallback and specific test cases to verify the expanded requirements
fallback_p = "Here is inline code `x = 42` and a math formula $$y = f(x)$$ inside a <span class=\"math\">formula</span>."
print("Adding fallback paragraph to test cases.")
test_paragraphs.append(fallback_p)

# Case 1: Inline HTML tags
print("Adding HTML tag test case.")
html_tag_p = "We can use inline HTML elements like <span class=\"highlight\" id=\"tag-1\">this text</span> or a link with an anchor <a href=\"#section-2\">jump to section 2</a>."
test_paragraphs.append(html_tag_p)

# Case 2: Standard markdown links and nested parentheses in URLs
print("Adding nested URL parentheses test case.")
nested_url_p = "Read about it on [Wikipedia Llama Page](https://en.wikipedia.org/wiki/Llama_(milking)) or check this [link with nested parentheses](https://example.com/some(nested)url) for details."
test_paragraphs.append(nested_url_p)

# Case 3: Code blocks and inline code
print("Adding code blocks and inline code test case.")
code_blocks_p = "Consider the following python script:\n```python\ndef test():\n    return \"Success\"\n```\nIt is simple to run it inline like `test()` to check output."
test_paragraphs.append(code_blocks_p)

# Case 4: Real paragraph from the batch markdown file with HTML tags
print("Adding real paragraph with HTML tags.")
real_html_p = "## <span id=\"page-9-0\"></span>**Предисловие от издательства**"
test_paragraphs.append(real_html_p)

# Case 5: Real paragraph from the batch markdown file with a link
print("Adding real paragraph with link.")
real_link_p = "Дизайн обложки разработан с использованием ресурса [magnific.com](http://magnific.com)"
test_paragraphs.append(real_link_p)

# Limit to all added test cases plus a few dynamically found ones to keep output clean but thorough
test_cases = test_paragraphs

print(f"Running byte-by-byte verification on {len(test_cases)} test case(s)...")

success = True
for i, orig_text in enumerate(test_cases):
    print(f"\n--- Test Case {i+1} ---")
    print(f"Original Text (first 100 chars): {orig_text[:100]}...")
    
    # Initialize both managers
    old_pm = OldPlaceholderManager()
    new_pm = NewPlaceholderManager()
    
    # Protect text
    old_prot = old_pm.protect(orig_text)
    new_prot = new_pm.protect(orig_text)
    
    # Restore text
    old_res = old_pm.restore(old_prot)
    new_res = new_pm.restore(new_prot)
    
    # Compare
    print(f"Protected (Old) (first 100 chars): {old_prot[:100]}...")
    print(f"Protected (New) (first 100 chars): {new_prot[:100]}...")
    
    # Verify byte-by-byte match between old and new restored text
    if old_res != new_res:
        print("FAIL: Restored strings do not match byte-by-byte between Old and New managers!")
        success = False
        # Find first difference
        min_len = min(len(old_res), len(new_res))
        for idx in range(min_len):
            if old_res[idx] != new_res[idx]:
                print(f"Difference at char index {idx}:")
                print(f"Old: ...{old_res[max(0, idx-20):idx+20]}...")
                print(f"New: ...{new_res[max(0, idx-20):idx+20]}...")
                break
        else:
            print(f"Length mismatch: Old={len(old_res)}, New={len(new_res)}")
    else:
        print("SUCCESS: Restored strings match byte-by-byte!")
        
    # Verify that they actually restored back to the original text
    if old_res != orig_text:
        print("WARNING: Restored string is not equal to original text!")
        success = False
    else:
        print("SUCCESS: Restored string matches the original text perfectly!")

if success:
    print("\n=============================================")
    print("ALL TESTS PASSED: BYTE-BY-BYTE VERIFICATION SUCCESSFUL!")
    print("=============================================")
    sys.exit(0)
else:
    print("\n=============================================")
    print("TESTS FAILED! Please inspect the logs above.")
    print("=============================================")
    sys.exit(1)
