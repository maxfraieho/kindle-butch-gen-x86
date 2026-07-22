import os
import glob

from common.file_lock import file_lock, LockTimeoutError


def patch_batch_translation(batches_dir, suffix, old_text, new_text):
    """Find the per-batch translated markdown file containing old_text and
    replace it with new_text (first occurrence). Locked against a
    concurrent write from translate_stage.py (TASK-23: a live edit can
    land while the main pipeline is still translating other batches).

    Returns True if a batch file was patched, False if old_text wasn't
    found in any batch file.
    """
    if not os.path.exists(batches_dir):
        return False

    for batch_md in glob.glob(os.path.join(batches_dir, "batch_*", "*", f"*{suffix}.md")):
        try:
            with file_lock(batch_md, timeout=2.0):
                with open(batch_md, "r", encoding="utf-8") as f:
                    batch_content = f.read()
                if old_text not in batch_content:
                    continue
                batch_content = batch_content.replace(old_text, new_text, 1)
                with open(batch_md, "w", encoding="utf-8") as f:
                    f.write(batch_content)
                return True
        except LockTimeoutError:
            continue
        except Exception:
            continue

    return False
