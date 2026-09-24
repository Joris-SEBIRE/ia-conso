"""Traces locales : verrou d'instance, miroir de la barre, journal de pannes, cache multi-org.

Le token OAuth ne voit qu'une organisation à la fois. On mémorise donc la dernière conso lue
pour chaque org : quand tu repasses dessus, le cache se met à jour ; sinon le menu montre
encore le dernier chiffre connu. Une fenêtre dont l'échéance est passée s'est réarmée depuis :
elle ressort à zéro, datée de son réarmement, seule valeur encore certaine.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from datetime import datetime

from .models import CLAUDE, AccountView, Extra, Refill, Window, now, parse_ts
from .paths import ERRORS_PATH, LOCK_PATH, ORGS_PATH, STATE_DIR, STATUS_PATH

ERRORS_MAX_BYTES = 1_000_000
_lock_handle = None
# Le cache est relu-modifié-réécrit depuis le thread de fetch : deux cycles qui se chevauchent
# effaceraient les comptes l'un de l'autre.
_cache_lock = threading.Lock()


def log_error(message: str) -> None:
    try:
        ERRORS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if ERRORS_PATH.exists() and ERRORS_PATH.stat().st_size > ERRORS_MAX_BYTES:
            ERRORS_PATH.replace(ERRORS_PATH.with_suffix(".log.1"))
        with ERRORS_PATH.open("a") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _write_atomic(path, payload: str) -> None:
    """Écrit par un temporaire puis un renommage : jamais de fichier à moitié écrit."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_text(payload)
        os.replace(temporary, path)
    except OSError:
        pass


def write_status(status: dict) -> None:
    _write_atomic(STATUS_PATH, json.dumps(status, indent=1, ensure_ascii=False, default=str) + "\n")


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


def _window_blob(window: Window | None) -> dict | None:
    if window is None:
        return None
    return {
        "label": window.label,
        "percent": window.percent,
        "resets_at": window.resets_at.isoformat() if window.resets_at else None,
        "is_session": window.is_session,
        "used": window.used,
        "cap": window.cap,
        "currency": window.currency,
    }


def _window_from(blob, at: datetime | None = None) -> Window | None:
    """Une fenêtre mémorisée dont l'échéance est passée s'est réarmée : sa valeur est périmée."""
    if not isinstance(blob, dict) or blob.get("percent") is None:
        return None
    resets_at = parse_ts(blob.get("resets_at"))
    if resets_at is not None and resets_at <= (at or now()):
        # Le pourcentage lu avant le réarmement est faux ; zéro, à cette date, est certain.
        return Window(
            label=str(blob.get("label") or ""),
            percent=0.0,
            resets_at=None,
            is_session=bool(blob.get("is_session")),
            currency=str(blob.get("currency") or ""),
            rearmed_at=resets_at,
        )
    return Window(
        label=str(blob.get("label") or ""),
        percent=float(blob["percent"]),
        resets_at=resets_at,
        is_session=bool(blob.get("is_session")),
        used=blob.get("used"),
        cap=blob.get("cap"),
        currency=str(blob.get("currency") or ""),
    )


def _extra_blob(extra: Extra | None) -> dict | None:
    if extra is None:
        return None
    return {
        "used": extra.used,
        "cap": extra.cap,
        "currency": extra.currency,
        "percent": extra.percent,
        "is_enabled": extra.is_enabled,
        "resets_at": extra.resets_at.isoformat() if extra.resets_at else None,
        "disabled_reason": extra.disabled_reason,
    }


def _extra_from(blob) -> Extra | None:
    if not isinstance(blob, dict) or blob.get("cap") is None:
        return None
    return Extra(
        used=float(blob.get("used") or 0),
        cap=float(blob["cap"]),
        currency=str(blob.get("currency") or "EUR"),
        percent=float(blob["percent"]) if blob.get("percent") is not None else None,
        is_enabled=bool(blob.get("is_enabled", True)),
        # Les caches d'avant portent une échéance qui avait été déduite, pas lue : on l'ignore.
        disabled_reason=str(blob.get("disabled_reason") or ""),
    )


def _refill_blob(refill: Refill) -> dict:
    return {
        "label": refill.label,
        "is_available": refill.is_available,
        "left": refill.left,
        "total": refill.total,
        "available_at": refill.available_at.isoformat() if refill.available_at else None,
        "ends_at": refill.ends_at.isoformat() if refill.ends_at else None,
        "needs_limit": refill.needs_limit,
    }


def _refill_from(blob) -> Refill | None:
    """Une offre mémorisée dont la date de fin est passée n'a plus cours."""
    if not isinstance(blob, dict) or not blob.get("label"):
        return None
    ends_at = parse_ts(blob.get("ends_at"))
    if ends_at is not None and ends_at <= now():
        return None
    return Refill(
        label=str(blob["label"]),
        is_available=bool(blob.get("is_available")),
        left=blob.get("left"),
        total=blob.get("total"),
        available_at=parse_ts(blob.get("available_at")),
        ends_at=ends_at,
        needs_limit=bool(blob.get("needs_limit")),
    )


def load_org_cache() -> dict[str, dict]:
    if not ORGS_PATH.exists():
        return {}
    try:
        raw = json.loads(ORGS_PATH.read_text() or "{}")
    except (json.JSONDecodeError, OSError, UnicodeError):
        return {}
    orgs = raw.get("orgs") if isinstance(raw, dict) else None
    return dict(orgs) if isinstance(orgs, dict) else {}


def remember_account_view(view: AccountView) -> None:
    """Écrit la conso de l'org active : c'est la seule qu'on peut lire en live."""
    if not view.key or not view.is_active or not view.has_figures:
        return
    with _cache_lock:
        store = load_org_cache()
        store[view.key] = {
            "name": view.name,
            "plan": view.plan,
            "email": view.email,
            "session": _window_blob(view.session),
            "weekly": _window_blob(view.weekly),
            "scoped": [_window_blob(window) for window in view.scoped],
            "extra": _extra_blob(view.extra),
            "refills": [_refill_blob(refill) for refill in view.refills],
            "breakdown": [list(row) for row in view.breakdown],
            "fetched_at": view.fetched_at.isoformat() if view.fetched_at else None,
        }
        _write_atomic(ORGS_PATH, json.dumps({"orgs": store}, indent=1, ensure_ascii=False) + "\n")


def cached_account_view(key: str, name: str, plan: str, email: str, is_active: bool = False) -> AccountView:
    store = load_org_cache().get(key) or {}
    scoped = tuple(window for blob in (store.get("scoped") or []) if (window := _window_from(blob)))
    breakdown = tuple(
        (str(label), float(share))
        for label, share in (store.get("breakdown") or [])
        if label is not None and share is not None
    )
    return AccountView(
        key=key,
        provider=CLAUDE,
        # « org_name » est le nom d'avant la refonte du cache : les fichiers existants le portent.
        name=str(store.get("name") or store.get("org_name") or name),
        plan=str(store.get("plan") or plan),
        email=str(store.get("email") or email),
        is_active=is_active,
        session=_window_from(store.get("session")),
        weekly=_window_from(store.get("weekly")),
        scoped=scoped,
        extra=_extra_from(store.get("extra")),
        refills=tuple(r for blob in (store.get("refills") or []) if (r := _refill_from(blob))),
        breakdown=breakdown,
        fetched_at=parse_ts(store.get("fetched_at")),
    )


def state_paths() -> tuple:
    """Tout ce que l'app écrit, pour l'aide et la désinstallation."""
    return (STATE_DIR, STATUS_PATH, ORGS_PATH, ERRORS_PATH, LOCK_PATH)
