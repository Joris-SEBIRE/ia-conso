"""Traces locales : verrou d'instance, miroir de la barre, journal de pannes, cache multi-org.

Le token OAuth ne voit qu'une organisation à la fois. On mémorise donc la dernière conso lue
pour chaque org : quand tu repasses dessus, le cache se met à jour ; sinon le menu montre
encore le dernier chiffre connu.
"""

from __future__ import annotations

import fcntl
import json
import time
from datetime import datetime, timezone

from .config import STATE_PATH
from .models import Extra, OrgView, Window, now, parse_ts

LOCK_PATH = STATE_PATH.parent / "ia-conso.lock"
STATUS_PATH = STATE_PATH.parent / "status.json"
ORGS_PATH = STATE_PATH.parent / "orgs.json"
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


def _window_blob(window: Window | None) -> dict | None:
    if window is None:
        return None
    return {
        "label": window.label,
        "percent": window.percent,
        "resets_at": window.resets_at.isoformat() if window.resets_at else None,
        "is_session": window.is_session,
    }


def _window_from(blob) -> Window | None:
    if not isinstance(blob, dict) or blob.get("percent") is None:
        return None
    return Window(
        label=str(blob.get("label") or ""),
        percent=float(blob["percent"]),
        resets_at=parse_ts(blob.get("resets_at")),
        is_session=bool(blob.get("is_session")),
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
    resets = parse_ts(blob.get("resets_at"))
    if resets is None:
        # Anciens caches sans date : même règle que l'API (1er du mois UTC).
        current = now().astimezone(timezone.utc)
        if current.month == 12:
            resets = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            resets = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)
    return Extra(
        used=float(blob.get("used") or 0),
        cap=float(blob["cap"]),
        currency=str(blob.get("currency") or "EUR"),
        percent=float(blob["percent"]) if blob.get("percent") is not None else None,
        is_enabled=bool(blob.get("is_enabled", True)),
        resets_at=resets,
        disabled_reason=str(blob.get("disabled_reason") or ""),
    )


def load_org_cache() -> dict[str, dict]:
    if not ORGS_PATH.exists():
        return {}
    try:
        raw = json.loads(ORGS_PATH.read_text() or "{}")
    except (json.JSONDecodeError, OSError):
        return {}
    orgs = raw.get("orgs") if isinstance(raw, dict) else None
    return dict(orgs) if isinstance(orgs, dict) else {}


def save_org_cache(orgs: dict[str, dict]) -> None:
    try:
        ORGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ORGS_PATH.write_text(json.dumps({"orgs": orgs}, indent=1, ensure_ascii=False) + "\n")
    except OSError:
        pass


def remember_org(view: OrgView) -> None:
    """Écrit la conso de l'org active : c'est la seule qu'on peut lire en live."""
    if not view.org_id or not view.is_active:
        return
    store = load_org_cache()
    store[view.org_id] = {
        "org_name": view.org_name,
        "plan": view.plan,
        "email": view.email,
        "account_name": view.account_name,
        "session": _window_blob(view.session),
        "weekly": _window_blob(view.weekly),
        "scoped": [_window_blob(w) for w in view.scoped],
        "extra": _extra_blob(view.extra),
        "breakdown": [list(row) for row in view.breakdown],
        "fetched_at": view.fetched_at.isoformat() if view.fetched_at else None,
    }
    save_org_cache(store)


def cached_org(org_id: str, org_name: str, plan: str, email: str, account_name: str) -> OrgView:
    store = load_org_cache().get(org_id) or {}
    fetched = parse_ts(store.get("fetched_at")) if store else None
    scoped = tuple(w for blob in (store.get("scoped") or []) if (w := _window_from(blob)))
    breakdown = tuple(
        (str(name), float(share))
        for name, share in (store.get("breakdown") or [])
        if name is not None and share is not None
    )
    return OrgView(
        org_id=org_id,
        org_name=str(store.get("org_name") or org_name),
        plan=str(store.get("plan") or plan),
        email=str(store.get("email") or email),
        account_name=str(store.get("account_name") or account_name),
        is_active=False,
        session=_window_from(store.get("session")),
        weekly=_window_from(store.get("weekly")),
        scoped=scoped,
        extra=_extra_from(store.get("extra")),
        breakdown=breakdown,
        fetched_at=fetched,
    )


def stamp(moment: datetime | None) -> str:
    return moment.isoformat() if isinstance(moment, datetime) else ""
