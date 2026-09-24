"""Conso du compte Cursor : token local + DashboardService.

Pas de token à coller. Cursor range un accessToken dans
`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`.
On le lit en lecture seule et on interroge `GetCurrentPeriodUsage`.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .models import CURSOR, AccountView, Extra, Window, now
from .paths import CURSOR_STATE_DB as STATE_DB

USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
PLAN_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetPlanInfo"
USER_AGENT = "IA-Conso"
HTTP_TIMEOUT = 8
KEY = "cursor"
PLANS = {
    "free": "Cursor Free",
    "pro": "Cursor Pro",
    "pro_plus": "Cursor Pro+",
    "pro+": "Cursor Pro+",
    "business": "Cursor Business",
    "team": "Cursor Business",
    "ultra": "Cursor Ultra",
    "enterprise": "Cursor Enterprise",
}
# En dessous, le pourcentage est trop imprécis pour en déduire la taille de l'enveloppe.
ENVELOPE_FLOOR = 0.1


def _auth() -> tuple[str, str, str]:
    """(token, email, membership) depuis la base d'état de Cursor."""
    if not STATE_DB.exists():
        raise RuntimeError("Cursor n'est pas installé (state.vscdb introuvable).")
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=0.25)
        try:
            rows = dict(
                con.execute(
                    "SELECT key, value FROM ItemTable WHERE key IN (?, ?, ?)",
                    ("cursorAuth/accessToken", "cursorAuth/cachedEmail", "cursorAuth/stripeMembershipType"),
                )
            )
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise RuntimeError("La base d'état de Cursor est illisible (Cursor est peut-être en train d'écrire).") from exc
    token = str(rows.get("cursorAuth/accessToken") or "")
    if not token:
        raise RuntimeError("Cursor est installé mais pas connecté. Ouvre Cursor et connecte-toi.")
    return token, str(rows.get("cursorAuth/cachedEmail") or ""), str(rows.get("cursorAuth/stripeMembershipType") or "")


def _post(url: str, token: str) -> dict:
    request = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise RuntimeError(f"Cursor API {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cursor injoignable : {exc.reason}") from exc
    except OSError as exc:
        # Un timeout de lecture ou une coupure de socket ne passe pas par URLError.
        raise RuntimeError(f"Cursor injoignable : {exc}") from exc
    try:
        data = json.loads(payload or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Portail captif : HTTP 200, mais du HTML dans le corps.
        raise RuntimeError("Cursor a répondu autre chose que du JSON (portail captif ?).") from exc
    return data if isinstance(data, dict) else {}


def _ms(value) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _cents(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        return None


def _rate(value) -> float | None:
    """Un pourcentage de l'API, déjà en base 100, borné."""
    if value is None:
        return None
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _plan_label(membership: str, plan_name: str) -> str:
    if plan_name:
        return plan_name if plan_name.lower().startswith("cursor") else f"Cursor {plan_name}"
    return PLANS.get(membership.lower(), f"Cursor {membership}" if membership else "Cursor")


def _period(plan_usage: dict, resets_at: datetime | None) -> Window | None:
    """Fenêtre de facturation : le pourcentage vient du serveur, jamais de spend / limit.

    `includedSpend` et `limit` valent tous les deux l'enveloppe du forfait dès qu'elle est
    saturée : leur rapport reste collé à 100 %. Cursor lui-même a cessé de l'afficher
    (`displayThreshold` à 200 rend la barre inatteignable) et publie à la place des taux déjà
    calculés. L'enveloppe en euros se déduit alors de la dépense et du taux.
    """
    percent = _rate(plan_usage.get("totalPercentUsed"))
    if percent is None:
        percent = _rate(plan_usage.get("autoPercentUsed"))
    used = _cents(plan_usage.get("totalSpend"))
    if percent is None:
        # Aucun taux : on retombe sur la fraction brute, faute de mieux.
        spent, cap = _cents(plan_usage.get("includedSpend")), _cents(plan_usage.get("limit"))
        if spent is None or not cap:
            return None
        percent, used = min(100.0, 100.0 * spent / cap), spent
    cap = round(used / (percent / 100.0), 2) if used is not None and percent >= ENVELOPE_FLOOR else None
    return Window(
        label="période",
        percent=percent,
        resets_at=resets_at,
        used=used,
        cap=cap,
        currency="USD",
    )


def _split(plan_usage: dict, resets_at: datetime | None) -> tuple[Window, ...]:
    """Modèles maison contre modèles facturés à l'API : seulement si les deux tirent."""
    auto = _rate(plan_usage.get("autoPercentUsed"))
    api = _rate(plan_usage.get("apiPercentUsed"))
    if not auto or not api:
        return ()
    return (
        Window(label="modèles inclus", percent=auto, resets_at=resets_at, used=_cents(plan_usage.get("autoSpend")), currency="USD"),
        Window(label="modèles API", percent=api, resets_at=resets_at, used=_cents(plan_usage.get("apiSpend")), currency="USD"),
    )


def _overage(usage: dict, resets_at: datetime | None) -> Extra | None:
    """Dépassement facturé au-delà du forfait, quand un plafond de dépense est posé."""
    block = usage.get("spendLimitUsage") if isinstance(usage.get("spendLimitUsage"), dict) else {}
    used = _cents(block.get("individualUsed"))
    cap = _cents(block.get("individualLimit"))
    if used is None or not cap:
        return None
    return Extra(
        used=used,
        cap=cap,
        currency="USD",
        percent=min(100.0, 100.0 * used / cap),
        is_enabled=bool(usage.get("enabled", True)),
        resets_at=resets_at,
    )


def fetch() -> AccountView:
    """Lit la conso Cursor. Ne lève jamais : un échec devient une vue portant son `error`."""
    try:
        token, email, membership = _auth()
    except Exception as exc:
        return AccountView(key=KEY, provider=CURSOR, plan="Cursor", error=str(exc))

    try:
        usage = _post(USAGE_URL, token)
    except Exception as exc:
        return AccountView(key=KEY, provider=CURSOR, email=email, plan=_plan_label(membership, ""), error=str(exc))

    plan_name = ""
    try:
        info = _post(PLAN_URL, token)
        block = info.get("planInfo") if isinstance(info.get("planInfo"), dict) else {}
        plan_name = str(block.get("planName") or "")
    except Exception:
        plan_name = ""

    plan_usage = usage.get("planUsage") if isinstance(usage.get("planUsage"), dict) else {}
    resets_at = _ms(usage.get("billingCycleEnd"))
    return AccountView(
        key=KEY,
        provider=CURSOR,
        plan=_plan_label(membership, plan_name),
        email=email,
        is_active=True,
        session=_period(plan_usage, resets_at),
        scoped=_split(plan_usage, resets_at),
        extra=_overage(usage, resets_at),
        fetched_at=now(),
    )
