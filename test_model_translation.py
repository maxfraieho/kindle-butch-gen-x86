#!/usr/bin/env python3
"""Quick model evaluation for EN→UK translation quality.
Usage: python3 test_model_translation.py [--port 8081] [--model-name 'Model Name']
"""
import requests
import time
import sys
import json
import argparse

TEST_SENTENCES = [
    # 1. Simple dialogue
    '"Basically, yes."',
    # 2. Literary prose
    'On the third day, she told him the truth about what had happened that summer.',
    # 3. With HTML placeholders
    'On the __HTML_TAG_1__third__HTML_TAG_2__ day, she told him the truth.',
    # 4. Complex literary with metaphor
    'The sky was painted in shades of amber and violet, as though the universe itself was mourning the passing of another day.',
    # 5. Dialogue with emotion
    '"I don\'t want to hear it," she whispered, her voice trembling like autumn leaves in the wind. "Not now. Not ever."',
]

def test_translation(api_url, model_name="Unknown"):
    print(f"\n{'='*60}")
    print(f"Testing model: {model_name}")
    print(f"   API: {api_url}")
    print(f"{'='*60}\n")
    
    results = []
    total_time = 0
    
    for i, sentence in enumerate(TEST_SENTENCES):
        prompt = (
            "Виконай роль професійного перекладача на Ukrainian мову. Переклади наданий текст.\n"
            "Суворі правила обробки:\n"
            "1. Перекладений текст має повністю відповідати вхідному за структурою та змістом.\n"
            "2. Категорично заборонено перекладати, змінювати, видаляти або переміщувати будь-які службові мітки з подвійними підкресленнями.\n"
            "3. Поверни виключно перекладений текст без будь-яких додаткових пояснень.\n\n"
            f"Вхідний текст для перекладу:\n{sentence}"
        )
        
        data = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "top_p": 1.0,
            "max_tokens": 512,
            "stop": ["<|im_end|>", "<|im_start|>", "<|endoftext|>"]
        }
        
        start = time.time()
        try:
            resp = requests.post(api_url, json=data, headers={"Content-Type": "application/json"}, timeout=120)
            elapsed = time.time() - start
            total_time += elapsed
            
            if resp.status_code != 200:
                print(f"  X Test {i+1}: HTTP {resp.status_code}")
                results.append({"ok": False, "error": f"HTTP {resp.status_code}"})
                continue
            
            result = resp.json()
            translated = result["choices"][0]["message"]["content"].strip()
            
            placeholders_ok = True
            if "__HTML_TAG_" in sentence:
                for tag in ["__HTML_TAG_1__", "__HTML_TAG_2__"]:
                    if tag in sentence and tag not in translated:
                        placeholders_ok = False
            
            prompt_leak = any(x in translated.lower() for x in ["виконай роль", "суворі правила", "переклади наданий"])
            
            usage = result.get("usage", {})
            tokens = usage.get("completion_tokens", 0)
            t_per_s = tokens / elapsed if elapsed > 0 else 0
            
            status = "OK" if not prompt_leak and placeholders_ok else "WARN"
            if prompt_leak:
                status = "FAIL: PROMPT LEAK"
            
            print(f"  [{status}] Test {i+1} ({elapsed:.1f}s, {t_per_s:.1f} t/s):")
            print(f"     EN: {sentence[:80]}")
            print(f"     UK: {translated[:80]}")
            if not placeholders_ok:
                print(f"     WARNING: Placeholders LOST!")
            print()
            
            results.append({
                "ok": not prompt_leak and placeholders_ok,
                "time": elapsed,
                "tokens_per_sec": t_per_s,
                "translated": translated,
                "prompt_leak": prompt_leak
            })
            
        except Exception as e:
            elapsed = time.time() - start
            print(f"  X Test {i+1}: {e}")
            results.append({"ok": False, "error": str(e)})
    
    passed = sum(1 for r in results if r.get("ok"))
    avg_tps = sum(r.get("tokens_per_sec", 0) for r in results if "tokens_per_sec" in r)
    count_tps = sum(1 for r in results if "tokens_per_sec" in r)
    
    print(f"\n{'='*60}")
    print(f"Summary: {model_name}")
    print(f"   Passed: {passed}/{len(TEST_SENTENCES)}")
    print(f"   Avg speed: {avg_tps/count_tps:.1f} t/s" if count_tps > 0 else "   Speed: N/A")
    print(f"   Total time: {total_time:.1f}s")
    print(f"{'='*60}")
    
    return results

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--model-name", default="Unknown Model")
    args = parser.parse_args()
    
    api_url = f"http://127.0.0.1:{args.port}/v1/chat/completions"
    
    try:
        health_url = f"http://127.0.0.1:{args.port}"
        requests.get(health_url, timeout=3)
    except:
        print(f"Server not reachable at port {args.port}")
        sys.exit(1)
    
    test_translation(api_url, args.model_name)
