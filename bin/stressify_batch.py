#!/usr/bin/env python3
import sys
import os
import json
import unicodedata

import re
import unicodedata

class NeuralStressifier:
    def __init__(self, stress_symbol="+"):
        from stress_uk import Stressifier
        self.stressifier = Stressifier()
        self.stress_symbol = stress_symbol

    def remove_monosyllabic_stresses(self, text):
        tokens = re.split(r'([a-zA-Zа-яіїєґА-ЯІЇЄҐ\u0301]+)', text)
        result = []
        for token in tokens:
            if not token:
                continue
            # Check if this token is a word (contains letters)
            if not re.match(r'[a-zA-Zа-яіїєґА-ЯІЇЄҐ]', token):
                result.append(token)
                continue
            
            clean_token = token.replace("\u0301", "")
            vowels = re.findall(r'[аеєиіїоуюяАЕЄИІЇОУЮЯ]', clean_token)
            if len(vowels) <= 1:
                result.append(clean_token)
            else:
                result.append(token)
        return "".join(result)

    def __call__(self, text):
        stressed = self.stressifier.stressify_text(text)
        stressed = self.remove_monosyllabic_stresses(stressed)
        return stressed.replace("\u0301", self.stress_symbol)

def normalize_accents(text):
    return text.replace("\u00b4", "\u0301")

def replace_numbers_with_words(text, lang="uk"):
    import re
    try:
        import num2words
    except ImportError:
        return text

    pattern = r'\d+([.,]\d+)?'
    def repl(match):
        val_str = match.group(0)
        val_str_normalized = val_str.replace(",", ".")
        try:
            if "." in val_str_normalized:
                val = float(val_str_normalized)
            else:
                val = int(val_str_normalized)
            return num2words.num2words(val, lang=lang)
        except Exception:
            return val_str
    return re.sub(pattern, repl, text)

def main():
    if "--inline" in sys.argv:
        try:
            idx = sys.argv.index("--inline")
            text = sys.argv[idx + 1]
        except Exception:
            print("Error: --inline requires a text argument", file=sys.stderr)
            sys.exit(1)
        
        # Load stressifier
        stressifier = None
        try:
            stressifier = NeuralStressifier(stress_symbol="+")
        except Exception as e:
            print(f"[StressifyBatch] Warning: Failed to load NeuralStressifier: {e}. Stress will not be added.", file=sys.stderr)
        
        # Process inline text
        text = replace_numbers_with_words(text, lang="uk")
        if stressifier is not None:
            try:
                text = stressifier(text)
            except Exception:
                pass
        text = normalize_accents(text)
        text = unicodedata.normalize("NFD", text)
        
        print(text)
        sys.exit(0)

    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    input_path = os.path.join(repo_dir, "books", "temp_unstressed.json")
    output_path = os.path.join(repo_dir, "books", "temp_stressed.json")

    if not os.path.exists(input_path):
        print(f"[StressifyBatch] Error: Input file {input_path} not found.", file=sys.stderr)
        sys.exit(1)

    try:
        with open(input_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[StressifyBatch] Error: Failed to parse input JSON: {e}", file=sys.stderr)
        sys.exit(1)

    chunks = data.get("chunks", [])
    lang = data.get("lang", "uk")

    stressifier = None
    if lang == "uk":
        try:
            stressifier = NeuralStressifier(stress_symbol="+")
            print("[StressifyBatch] Initialized NeuralStressifier (stress-uk) successfully.", flush=True)
        except Exception as e:
            print(f"[StressifyBatch] Warning: Failed to load NeuralStressifier: {e}. Stress will not be added.", file=sys.stderr)

    total = len(chunks)
    print(f"[StressifyBatch] Stressifying and normalizing {total} chunks...", flush=True)

    stressed_chunks = []
    for i, chunk in enumerate(chunks):
        h = chunk.get("hash")
        text = chunk.get("text", "")

        # 1. Expand numbers to words for Ukrainian
        if lang == "uk":
            text = replace_numbers_with_words(text, lang="uk")

        # 2. Add word stress if Ukrainian
        if lang == "uk" and stressifier is not None:
            try:
                stressed_text = stressifier(text)
            except Exception as e:
                stressed_text = text
        else:
            stressed_text = text

        # 2. Normalize accent characters
        stressed_text = normalize_accents(stressed_text)

        # 3. Apply NFD Normalization
        nfd_text = unicodedata.normalize("NFD", stressed_text)

        stressed_chunks.append({
            "hash": h,
            "text": nfd_text
        })

    # Save output
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump({"chunks": stressed_chunks}, f, ensure_ascii=False, indent=2)
        print("[StressifyBatch] Batch completed successfully.", flush=True)
    except Exception as e:
        print(f"[StressifyBatch] Error: Failed to save output JSON: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
