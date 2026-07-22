import os
import time
import contextlib


class LockTimeoutError(Exception):
    pass


# Generic default, deliberately conservative: every current caller's own
# critical section is short (seconds), so 5 minutes is comfortably above
# any legitimate hold - but still self-heals a lock orphaned by a killed
# process (the exact failure mode hit repeatedly this session: Termux
# dying mid-critical-section) instead of deadlocking that resource
# forever, which is what happened before this fix (kbg_web/app.py's
# LLAMA_START_LOCK_FILE already had this exact pattern at a tighter
# 15s threshold tuned for its own ~1s section; this generic helper had
# no equivalent and could wedge shut permanently).
DEFAULT_STALE_SECONDS = 300


def acquire_lock(target_path, timeout=1.0, poll_interval=0.2, stale_seconds=DEFAULT_STALE_SECONDS):
    """Atomic O_CREAT|O_EXCL sentinel lock for target_path (creates
    '<target_path>.lock'). Retries on contention; raises LockTimeoutError
    if the lock isn't free within timeout. A lock file older than
    stale_seconds is treated as abandoned (owning process died without
    releasing it) and removed before the next acquire attempt."""
    lock_path = target_path + ".lock"
    deadline = time.time() + timeout
    while True:
        if stale_seconds is not None and os.path.exists(lock_path):
            try:
                age = time.time() - os.path.getmtime(lock_path)
            except OSError:
                age = stale_seconds + 1
            if age > stale_seconds:
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return lock_path
        except FileExistsError:
            if time.time() >= deadline:
                raise LockTimeoutError(f"Could not acquire lock for {target_path} within {timeout}s")
            time.sleep(poll_interval)


def release_lock(lock_path):
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


@contextlib.contextmanager
def file_lock(target_path, timeout=1.0, poll_interval=0.2, stale_seconds=DEFAULT_STALE_SECONDS):
    lock_path = acquire_lock(target_path, timeout=timeout, poll_interval=poll_interval,
                             stale_seconds=stale_seconds)
    try:
        yield
    finally:
        release_lock(lock_path)
