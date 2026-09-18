"""Traces locales : verrou d'instance, miroir de la barre, journal de pannes."""

from __future__ import annotations

import fcntl
import json
import time

from .config import STATE_PATH

LOCK_PATH = STATE_PATH.parent / "ia-conso.lock"
STATUS_PATH = STATE_PATH.parent / "status.json"
ERRORS_PATH = STATE_PATH.parent / "errors.log"
ERRORS_MAX_BYTES = 1_000_000
_lock_handle = None


def log_error(message: str) -> None:
    try:
        ERRORS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if ERRORS_PATH.exists() and ERRORS_PATH.stat().st_size > ERRORS_MAX_BYTES:
            ERRORS_PATH.replace(ERRORS_PATH.with_suffix(".log.1"))
        with ERRORS_PATH.open("a") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def write_status(status: dict) -> None:
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps(status, indent=1, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def acquire_single_instance(attempts: int = 4, delay: float = 0.5) -> bool:
    """Empêche deux barres de menus concurrentes (le verrou tombe à la mort du process)."""
    global _lock_handle
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    for remaining in range(attempts, 0, -1):
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if remaining > 1:
                time.sleep(delay)
            continue
        _lock_handle = handle
        return True
    handle.close()
    return False
