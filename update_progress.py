#!/usr/bin/env python3
import os
import json
import time
import urllib.request
from datetime import datetime

# Configuration
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
CHUNKS_DIR = os.path.join(REPO_DIR, "books/vibe-programming/audio/chunks_styletts2")
STATE_FILE = os.path.join(REPO_DIR, "books/vibe-programming/audiobook_progress_state.json")
MCP_URL = "http://192.168.3.184:49374/mcp"
TOTAL_CHUNKS = 5219

def count_chunks():
    log_path = os.path.join(REPO_DIR, "books/vibe-programming/conversion_progress.log")
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                log_content = f.read()
            import re
            proc_matches = list(re.finditer(r'Processing (\d+) chunks', log_content))
            if proc_matches:
                total_missing = int(proc_matches[-1].group(1))
                start_pos = proc_matches[-1].start()
                progress_matches = re.findall(r'\[(\d+)/' + str(total_missing) + r'\]', log_content[start_pos:])
                if progress_matches:
                    current_missing_progress = int(progress_matches[-1])
                    already_cached = TOTAL_CHUNKS - total_missing
                    return already_cached + current_missing_progress
        except Exception as e:
            print(f"[ProgressTracker] Warning: failed to parse log progress: {e}")
            
    if not os.path.exists(CHUNKS_DIR):
        return 0
    return len([f for f in os.listdir(CHUNKS_DIR) if f.endswith(".wav")])

def update_progress_in_memory(body_text):
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "memory_write_page",
            "arguments": {
                "path": "notes/audiobook_vibe_programming_progress.md",
                "body": body_text,
                "title": "Audiobook Generation Progress: vibe-programming",
                "tier": "working"
            }
        },
        "id": 1
    }
    
    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        MCP_URL,
        data=req_data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        },
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res = json.loads(response.read().decode("utf-8"))
            if res.get("result", {}).get("isError"):
                print(f"[ProgressTracker] MCP Error: {res}")
            else:
                print(f"[ProgressTracker] Successfully updated progress in ai-memory. Page: {res['result']['content'][0]['text']}")
    except Exception as e:
        print(f"[ProgressTracker] Failed to update ai-memory: {e}")

def main():
    now = datetime.now()
    current_count = count_chunks()
    
    # Load or initialize state
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
        except Exception:
            pass
            
    if not state:
        state = {
            "start_time": now.isoformat(),
            "start_count": current_count,
            "last_check_time": now.isoformat(),
            "last_check_count": current_count,
            "history": []
        }
    
    start_time = datetime.fromisoformat(state["start_time"])
    last_check_time = datetime.fromisoformat(state["last_check_time"])
    start_count = state["start_count"]
    last_check_count = state["last_check_count"]
    
    # Calculate stats
    total_elapsed = (now - start_time).total_seconds() / 60.0  # in minutes
    interval_elapsed = (now - last_check_time).total_seconds() / 60.0  # in minutes
    
    total_chunks_done = current_count - start_count
    interval_chunks_done = current_count - last_check_count
    
    overall_speed = total_chunks_done / total_elapsed if total_elapsed > 0 else 0
    interval_speed = interval_chunks_done / interval_elapsed if interval_elapsed > 0 else 0
    
    # Select best speed for ETA (prefer interval speed if we have elapsed time, fallback to overall)
    effective_speed = interval_speed if interval_speed > 0 else overall_speed
    
    remaining_chunks = TOTAL_CHUNKS - current_count
    if effective_speed > 0:
        eta_mins = remaining_chunks / effective_speed
        eta_hours = eta_mins / 60.0
        eta_str = f"{eta_hours:.1f} hours ({eta_mins:.1f} mins)"
    else:
        eta_str = "Unknown (Not running or zero speed)"
        
    percentage = (current_count / TOTAL_CHUNKS) * 100
    
    # Thresholds / Milestones
    milestones = [
        {"pct": 20, "label": "Milestone 1 (20%)", "chunks": int(TOTAL_CHUNKS * 0.20)},
        {"pct": 30, "label": "Milestone 2 (30%)", "chunks": int(TOTAL_CHUNKS * 0.30)},
        {"pct": 40, "label": "Milestone 3 (40%)", "chunks": int(TOTAL_CHUNKS * 0.40)},
        {"pct": 50, "label": "Milestone 4 (50%)", "chunks": int(TOTAL_CHUNKS * 0.50)},
        {"pct": 60, "label": "Milestone 5 (60%)", "chunks": int(TOTAL_CHUNKS * 0.60)},
        {"pct": 70, "label": "Milestone 6 (70%)", "chunks": int(TOTAL_CHUNKS * 0.70)},
        {"pct": 80, "label": "Milestone 7 (80%)", "chunks": int(TOTAL_CHUNKS * 0.80)},
        {"pct": 90, "label": "Milestone 8 (90%)", "chunks": int(TOTAL_CHUNKS * 0.90)},
        {"pct": 100, "label": "Final Milestone (100%)", "chunks": TOTAL_CHUNKS}
    ]
    
    # Determine reached milestones
    milestones_status = []
    for m in milestones:
        status = "🟢 Reached" if current_count >= m["chunks"] else "⏳ Pending"
        milestones_status.append(f"- **{m['label']}**: {status} ({m['chunks']} chunks)")
    
    # Log progress entry to history
    state["history"].append({
        "time": now.isoformat(),
        "count": current_count,
        "speed": interval_speed
    })
    
    # Keep only last 20 history entries
    if len(state["history"]) > 20:
        state["history"] = state["history"][-20:]
        
    # Update state for next check
    state["last_check_time"] = now.isoformat()
    state["last_check_count"] = current_count
    
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        
    # Build markdown report
    report = f"""# Audiobook Generation Progress: vibe-programming

**Last Updated:** `{now.strftime("%Y-%m-%d %H:%M:%S")}`
**Status:** {"🟢 Active (Generating Chunks)" if interval_chunks_done > 0 or now.isoformat() == state["start_time"] else "🟠 Inactive / Paused"}

## Progress Metrics
- **Current Count:** `{current_count}` / `{TOTAL_CHUNKS}` chunks
- **Percentage Completed:** `{percentage:.2f}%`
- **Remaining Chunks:** `{remaining_chunks}` chunks
- **Interval Speed (Last 15m):** `{interval_speed:.2f}` chunks/min
- **Overall Speed (Session):** `{overall_speed:.2f}` chunks/min
- **Estimated Time to Completion (ETA):** **`{eta_str}`**

## Thresholds & Milestones
{chr(10).join(milestones_status)}

## Session Logs (Last 5 Checks)
"""
    # Format last 5 history items
    for item in reversed(state["history"][-5:]):
        item_time = datetime.fromisoformat(item["time"]).strftime("%H:%M:%S")
        report += f"- `{item_time}`: **{item['count']}** chunks ({item['count']/TOTAL_CHUNKS*100:.1f}%) | Speed: `{item['speed']:.2f}`/min\n"
        
    # Send to AI Memory
    update_progress_in_memory(report)

if __name__ == "__main__":
    main()
