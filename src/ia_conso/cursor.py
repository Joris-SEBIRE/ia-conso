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
from pathlib import Path

from .models import CursorView, Window, now

STATE_DB = Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
PLAN_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetPlanInfo"
USER_AGENT = "IA-Conso"
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


def _auth() -> tuple[str, str, str]:
    """(token, email, membership) depuis la base d'état de Cursor."""
    if not STATE_DB.exists():
        raise RuntimeError("Cursor n'est pas installé (state.vscdb introuvable).")
    con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    try:
        rows = dict(
            con.execute(
                "SELECT key, value FROM ItemTable WHERE key IN (?, ?, ?)",
                ("cursorAuth/accessToken", "cursorAuth/cachedEmail", "cursorAuth/stripeMembershipType"),
            )
        )
    finally:
        con.close()
    token = str(rows.get("cursorAuth/accessToken") or "")
    if not token:
        raise RuntimeError("Cursor est installé mais pas connecté. Ouvre Cursor et connecte-toi.")
    email = str(rows.get("cursorAuth/cachedEmail") or "")
    membership = str(rows.get("cursorAuth/stripeMembershipType") or "")
    return token, email, membership


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
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise RuntimeError(f"Cursor API {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cursor injoignable : {exc.reason}") from exc


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


def _plan_label(membership: str, plan_name: str) -> str:
    if plan_name:
        name = plan_name if plan_name.lower().startswith("cursor") else f"Cursor {plan_name}"
        return name
    return PLANS.get(membership.lower(), f"Cursor {membership}" if membership else "Cursor")


def fetch_cursor() -> CursorView | None:
    """Lit la conso Cursor. Ne lève pas : un échec devient une vue avec `error`."""
    try:
        token, email, membership = _auth()
    except RuntimeError as exc:
        return CursorView(email="", plan="Cursor", error=str(exc))

    try:
        usage = _post(USAGE_URL, token)
    except RuntimeError as exc:
        return CursorView(email=email, plan=_plan_label(membership, ""), error=str(exc))

    plan_name = ""
    try:
        info = _post(PLAN_URL, token)
        block = info.get("planInfo") if isinstance(info.get("planInfo"), dict) else {}
        plan_name = str(block.get("planName") or "")
    except RuntimeError:
        plan_name = ""

    plan_usage = usage.get("planUsage") if isinstance(usage.get("planUsage"), dict) else {}
    used = _cents(plan_usage.get("includedSpend") if plan_usage.get("includedSpend") is not None else plan_usage.get("totalSpend"))
    cap = _cents(plan_usage.get("limit"))
    percent = None
    if used is not None and cap:
        percent = min(100.0, 100.0 * used / cap)
    elif plan_usage.get("totalPercentUsed") is not None:
        raw = float(plan_usage["totalPercentUsed"])
        # L'API a renvoyé 0.20 pour ~5 % : on préfère spend/limit quand c'est dispo.
        percent = raw if raw > 1.0 else raw * 100.0

    resets = _ms(usage.get("billingCycleEnd"))
    period = None
    if percent is not None:
        period = Window(label="période", percent=percent, resets_at=resets, is_session=False)

    return CursorView(
        email=email,
        plan=_plan_label(membership, plan_name),
        period=period,
        used=used or 0.0,
        cap=cap or 0.0,
        currency="USD",
        fetched_at=now(),
        error="",
    )
