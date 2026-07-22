"""Durable per-device identity (TASK-72, multi-device support).

Software-defined UUIDv4, generated once and persisted locally. Android
gives no stable, permission-free hardware identifier reachable from
Termux without root: Wi-Fi/Bluetooth MACs are randomized since Android 8,
IMEI/Build.SERIAL need READ_PRIVILEGED_PHONE_STATE (unavailable to a
Termux app), and Settings.Secure.ANDROID_ID is signature-hashed per app
since Android 8 and unreliable to read via adb from inside Termux itself
(would also add an adb dependency deploy.sh's one-curl install can't
assume). uuid.getnode() is explicitly NOT used - it falls back to a
random 48-bit value whenever no network interface MAC is reachable
(common in Termux/proot), so it is not stable across restarts.

Stored at the Termux-home absolute path (bind-mounted into the proot
container too, per common/support_profile.py's own _TERMUX_HOME
precedent - the pipeline runs INSIDE proot but must resolve to the same
physical file as any Termux-level script). Excluded from git so a fresh
`git pull` never disturbs it - every phone/tablet clone generates its
own id on first heartbeat.
"""
import os
import subprocess
import uuid

_TERMUX_HOME = os.environ.get("TERMUX_HOME", "/data/data/com.termux/files/home")
_HOME = _TERMUX_HOME if os.path.isdir(_TERMUX_HOME) else os.path.expanduser("~")
DEVICE_ID_FILE = os.path.join(_HOME, ".vydra_device_id")

_alias_cache = None


def get_or_create_device_id():
    """Stable device UUID, generated once. Atomic write (tmp + rename)
    so a crash mid-write can never leave a half-written id file."""
    try:
        with open(DEVICE_ID_FILE, "r", encoding="utf-8") as f:
            device_id = f.read().strip()
            if device_id and len(device_id) > 10:
                return device_id
    except OSError:
        pass

    new_id = uuid.uuid4().hex
    tmp_path = f"{DEVICE_ID_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(new_id)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, DEVICE_ID_FILE)
    return new_id


def get_device_alias():
    """Best-effort human-readable device name for watchdog notifications
    (e.g. 'OnePlus 13') - purely cosmetic, never used as a lookup key.
    `getprop` needs no root/permissions on stock Android. Cached per
    process since it never changes during a run."""
    global _alias_cache
    if _alias_cache is not None:
        return _alias_cache
    try:
        out = subprocess.check_output(
            ["getprop", "ro.product.model"], timeout=3,
            stderr=subprocess.DEVNULL).decode("utf-8", "ignore").strip()
        if out:
            _alias_cache = out
            return _alias_cache
    except Exception:
        pass
    _alias_cache = "пристрій " + get_or_create_device_id()[:8]
    return _alias_cache
