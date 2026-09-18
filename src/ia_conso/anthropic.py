"""Lecture du token Claude Code et appel des endpoints de conso.

Le plus simple pour l'utilisateur : aucun token à coller. Claude Code (et Cursor, qui s'en
sert) range déjà un OAuth dans le trousseau, service `Claude Code-credentials`. On le relit
à chaque cycle, on n'en crée pas, on n'en rafraîchit pas : un refresh ferait tourner le
refresh token et casserait la session de Claude Code.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .models import Account, Extra, OrgView, Snapshot, Window, now, parse_ts
from .state import cached_org, remember_org

KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_JSON = Path.home() / ".claude.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
ACCOUNT_URL = "https://api.anthropic.com/api/oauth/account"
# Sans cet en-tête, l'endpoint range l'appel dans un seau 429 agressif. C'est le même que
# celui de Claude Code, qui est le client légitime de cet OAuth.
USER_AGENT = "claude-code/2.0.0"
BETA = "oauth-2025-04-20"
PLANS = {
    "claude_pro": "Claude Pro",
    "claude_max": "Claude Max",
    "claude_team": "Claude Team",
    "claude_enterprise": "Claude Enterprise",
    "pro": "Claude Pro",
    "max": "Claude Max",
    "team": "Claude Team",
    "enterprise": "Claude Enterprise",
}


class ClaudeError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _run(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def read_token() -> tuple[str, str]:
    raw = _run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"])
    if not raw:
        raise ClaudeError(
            "Aucun compte Claude Code dans le trousseau. Ouvre Cursor ou lance Claude Code une fois."
        )
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClaudeError("Le trousseau Claude Code ne contient pas de JSON lisible.") from exc
    oauth = blob.get("claudeAiOauth") if isinstance(blob, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise ClaudeError(
            "Claude Code est installé mais pas connecté à un abonnement. "
            "Ouvre Cursor ou Claude Code et connecte-toi avec le compte Claude, pas une clé API."
        )
    origin = f"trousseau macOS (service « {KEYCHAIN_SERVICE} »)"
    return str(oauth["accessToken"]), origin


def cached_account() -> Account | None:
    """Identité déjà connue de Claude Code, pour afficher un nom même si le profil API rate."""
    if not CLAUDE_JSON.exists():
        return None
    try:
        raw = json.loads(CLAUDE_JSON.read_text() or "{}")
    except (json.JSONDecodeError, OSError):
        return None
    info = raw.get("oauthAccount") if isinstance(raw, dict) else None
    if not isinstance(info, dict):
        return None
    email = str(info.get("emailAddress") or "")
    name = str(info.get("fullName") or info.get("displayName") or "")
    plan = PLANS.get(str(info.get("organizationType") or ""), "")
    org_id = str(info.get("organizationUuid") or "")
    org_name = str(info.get("organizationName") or "")
    if not email and not name:
        return None
    return Account(
        name=name or email,
        email=email,
        plan=plan or "Claude",
        org_id=org_id,
        org_name=org_name,
    )


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "anthropic-beta": BETA,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        retry = None
        if header := exc.headers.get("Retry-After"):
            try:
                retry = int(header.strip())
            except ValueError:
                retry = None
        detail = (exc.read() or b"").decode()[:200]
        if exc.code == 401:
            raise ClaudeError(
                "Session Claude expirée. Ouvre Cursor ou Claude Code une fois pour la renouveler.",
                status=401,
                retry_after=retry,
            ) from exc
        if exc.code == 429:
            raise ClaudeError(
                "Anthropic demande d'attendre avant de relire la conso.",
                status=429,
                retry_after=retry,
            ) from exc
        raise ClaudeError(f"Anthropic {exc.code} : {detail}", status=exc.code, retry_after=retry) from exc
    except urllib.error.URLError as exc:
        raise ClaudeError("Réseau injoignable.") from exc


def _window(percent, resets_at, label: str, is_session: bool = False) -> Window | None:
    if percent is None:
        return None
    try:
        value = float(percent)
    except (TypeError, ValueError):
        return None
    return Window(label=label, percent=value, resets_at=parse_ts(resets_at), is_session=is_session)


def _scope_label(scope: dict | None) -> str:
    blob = scope or {}
    model = blob.get("model") if isinstance(blob.get("model"), dict) else {}
    surface = blob.get("surface") if isinstance(blob.get("surface"), dict) else {}
    return str(model.get("display_name") or surface.get("display_name") or "modèle")


def _from_limits(data: dict) -> tuple[Window | None, Window | None, list[Window]]:
    session = weekly = None
    scoped: list[Window] = []
    for entry in data.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind == "session":
            session = _window(entry.get("percent"), entry.get("resets_at"), "session 5 h", True)
        elif kind == "weekly_all":
            weekly = _window(entry.get("percent"), entry.get("resets_at"), "semaine", False)
        elif kind == "weekly_scoped":
            label = _scope_label(entry.get("scope") if isinstance(entry.get("scope"), dict) else None)
            if window := _window(entry.get("percent"), entry.get("resets_at"), f"semaine {label}"):
                scoped.append(window)
    return session, weekly, scoped


def _from_legacy(data: dict) -> tuple[Window | None, Window | None, list[Window]]:
    five = data.get("five_hour") if isinstance(data.get("five_hour"), dict) else {}
    seven = data.get("seven_day") if isinstance(data.get("seven_day"), dict) else {}
    session = _window(five.get("utilization"), five.get("resets_at"), "session 5 h", True)
    weekly = _window(seven.get("utilization"), seven.get("resets_at"), "semaine")
    scoped: list[Window] = []
    for key, label in (("seven_day_sonnet", "semaine Sonnet"), ("seven_day_opus", "semaine Opus")):
        block = data.get(key) if isinstance(data.get(key), dict) else None
        if block and (window := _window(block.get("utilization"), block.get("resets_at"), label)):
            scoped.append(window)
    return session, weekly, scoped


def _money(block: dict | None) -> tuple[float | None, str]:
    if not isinstance(block, dict) or block.get("amount_minor") is None:
        return None, "EUR"
    exponent = int(block.get("exponent") or 2)
    return float(block["amount_minor"]) / (10**exponent), str(block.get("currency") or "EUR")


def next_month_start(at: datetime | None = None) -> datetime:
    """Les crédits extra se réarmant au mois civil, le prochain 1er à minuit UTC."""
    current = (at or now()).astimezone(timezone.utc)
    if current.month == 12:
        return datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)


def _extra(data: dict) -> Extra | None:
    resets = next_month_start()
    spend = data.get("spend") if isinstance(data.get("spend"), dict) else {}
    used, currency = _money(spend.get("used") if isinstance(spend.get("used"), dict) else None)
    cap, cap_currency = _money(spend.get("limit") if isinstance(spend.get("limit"), dict) else None)
    if used is not None and cap:
        percent = spend.get("percent")
        try:
            value = float(percent) if percent is not None else None
        except (TypeError, ValueError):
            value = None
        return Extra(
            used=used,
            cap=cap,
            currency=cap_currency or currency,
            percent=value,
            is_enabled=bool(spend.get("enabled", True)),
            resets_at=resets,
            disabled_reason=str(spend.get("disabled_reason") or ""),
        )
    extra = data.get("extra_usage") if isinstance(data.get("extra_usage"), dict) else {}
    if not extra.get("is_enabled") and extra.get("monthly_limit") is None:
        return None
    places = int(extra.get("decimal_places") or 2)
    cap_raw = extra.get("monthly_limit")
    if cap_raw is None:
        return None
    return Extra(
        used=float(extra.get("used_credits") or 0) / (10**places),
        cap=float(cap_raw) / (10**places),
        currency=str(extra.get("currency") or "EUR"),
        percent=float(extra["utilization"]) if extra.get("utilization") is not None else None,
        is_enabled=bool(extra.get("is_enabled", True)),
        resets_at=resets,
        disabled_reason=str(extra.get("disabled_reason") or ""),
    )


def _breakdown(data: dict) -> tuple[tuple[str, float], ...]:
    block = data.get("seven_day_breakdown") if isinstance(data.get("seven_day_breakdown"), dict) else {}
    names = {"Other": "Autres"}
    rows = []
    for row in block.get("rows") or []:
        if not isinstance(row, dict) or row.get("percent") is None:
            continue
        name = str(row.get("display_name") or row.get("key") or "")
        if not name:
            continue
        rows.append((names.get(name, name), float(row["percent"])))
    return tuple(rows)


def _account(profile: dict | None, cached: Account | None) -> Account | None:
    if not isinstance(profile, dict):
        return cached
    info = profile.get("account") if isinstance(profile.get("account"), dict) else {}
    org = profile.get("organization") if isinstance(profile.get("organization"), dict) else {}
    email = str(info.get("email") or (cached.email if cached else ""))
    name = str(info.get("full_name") or info.get("display_name") or (cached.name if cached else "") or email)
    org_id = str(org.get("uuid") or (cached.org_id if cached else ""))
    org_name = str(org.get("name") or (cached.org_name if cached else ""))
    plan = (
        PLANS.get(str(org.get("organization_type") or ""))
        or plan_from_org(org)
        or (cached.plan if cached else "")
        or "Claude"
    )
    if not email and not name:
        return cached
    return Account(name=name, email=email, plan=plan, org_id=org_id, org_name=org_name)


def plan_from_org(org: dict) -> str:
    caps = org.get("capabilities") or []
    if "claude_max" in caps:
        return "Claude Max"
    if "claude_pro" in caps:
        return "Claude Pro"
    if "claude_enterprise" in caps:
        return "Claude Enterprise"
    if "raven" in caps or org.get("raven_type") in ("team", "enterprise"):
        return "Claude Team"
    return ""


def is_chat_org(org: dict) -> bool:
    """Les orgs API-only n'ont pas de session 5 h / semaine d'abonnement."""
    caps = org.get("capabilities") or []
    return "chat" in caps


def org_label(name: str, plan: str) -> str:
    """Nom d'affichage : raccourcit l'org perso Anthropic trop verbeuse."""
    if "'s Organization" in name or "’s Organization" in name or "'s Individual Org" in name:
        return f"Perso · {plan}" if plan else "Perso"
    return name or plan or "Organisation"


def _memberships(account: dict | None, active_id: str, email: str, account_name: str) -> list[tuple[str, str, str]]:
    """(org_id, org_name, plan) pour chaque org chat, active en premier."""
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for entry in (account or {}).get("memberships") or []:
        if not isinstance(entry, dict):
            continue
        org = entry.get("organization") if isinstance(entry.get("organization"), dict) else {}
        if not is_chat_org(org):
            continue
        org_id = str(org.get("uuid") or "")
        if not org_id or org_id in seen:
            continue
        seen.add(org_id)
        name = str(org.get("name") or "")
        plan = plan_from_org(org) or "Claude"
        rows.append((org_id, name, plan))
    rows.sort(key=lambda row: (0 if row[0] == active_id else 1, row[1].lower()))
    if active_id and active_id not in seen:
        rows.insert(0, (active_id, "", ""))
    return rows


def _build_orgs(
    account_payload: dict | None,
    active: Account | None,
    session: Window | None,
    weekly: Window | None,
    scoped: tuple[Window, ...],
    extra: Extra | None,
    breakdown: tuple[tuple[str, float], ...],
    fetched_at,
) -> tuple[OrgView, ...]:
    email = active.email if active else ""
    account_name = active.name if active else ""
    active_id = active.org_id if active else ""
    views: list[OrgView] = []
    for org_id, org_name, plan in _memberships(account_payload, active_id, email, account_name):
        if org_id == active_id and active and fetched_at is not None:
            view = OrgView(
                org_id=org_id,
                org_name=active.org_name or org_name,
                plan=active.plan or plan,
                email=email,
                account_name=account_name,
                is_active=True,
                session=session,
                weekly=weekly,
                scoped=scoped,
                extra=extra,
                breakdown=breakdown,
                fetched_at=fetched_at,
            )
            remember_org(view)
            views.append(view)
        elif org_id == active_id and active:
            # Usage injoignable : on affiche le dernier cache, marqué actif.
            view = cached_org(org_id, active.org_name or org_name, active.plan or plan, email, account_name)
            views.append(
                OrgView(
                    org_id=view.org_id,
                    org_name=view.org_name,
                    plan=view.plan,
                    email=view.email,
                    account_name=view.account_name,
                    is_active=True,
                    session=view.session,
                    weekly=view.weekly,
                    scoped=view.scoped,
                    extra=view.extra,
                    breakdown=view.breakdown,
                    fetched_at=view.fetched_at,
                )
            )
        else:
            views.append(cached_org(org_id, org_name, plan, email, account_name))
    return tuple(views)


def fetch() -> Snapshot:
    origin = ""
    cached = cached_account()
    token = ""
    try:
        token, origin = read_token()
    except ClaudeError as exc:
        return Snapshot(account=cached, error=str(exc), token_origin=origin, retry_after=exc.retry_after)

    usage = None
    usage_error = ""
    retry_after = None
    try:
        usage = _get(USAGE_URL, token)
    except ClaudeError as exc:
        usage_error = str(exc)
        retry_after = exc.retry_after

    profile = None
    account_payload = None
    try:
        profile = _get(PROFILE_URL, token)
    except ClaudeError:
        profile = None
    try:
        account_payload = _get(ACCOUNT_URL, token)
    except ClaudeError:
        account_payload = None

    account = _account(profile, cached)
    if account and account_payload and isinstance(account_payload, dict):
        email = str(account_payload.get("email_address") or account.email)
        name = str(account_payload.get("full_name") or account_payload.get("display_name") or account.name)
        account = Account(
            name=name,
            email=email,
            plan=account.plan,
            org_id=account.org_id,
            org_name=account.org_name,
        )

    session = weekly = None
    scoped_tuple: tuple[Window, ...] = ()
    extra = None
    breakdown: tuple[tuple[str, float], ...] = ()
    fetched_at = None
    if usage is not None:
        session, weekly, scoped = _from_limits(usage)
        if session is None and weekly is None:
            session, weekly, scoped = _from_legacy(usage)
        scoped_tuple = tuple(scoped)
        extra = _extra(usage)
        breakdown = _breakdown(usage)
        fetched_at = now()

    orgs = _build_orgs(account_payload, account, session, weekly, scoped_tuple, extra, breakdown, fetched_at)
    # Si l'usage a raté mais qu'on a un cache pour l'org active, on remplit le snapshot dessus.
    if fetched_at is None:
        for org in orgs:
            if org.is_active and (org.session or org.weekly):
                session, weekly, scoped_tuple = org.session, org.weekly, org.scoped
                extra, breakdown, fetched_at = org.extra, org.breakdown, org.fetched_at
                break
    return Snapshot(
        account=account,
        session=session,
        weekly=weekly,
        scoped=scoped_tuple,
        extra=extra,
        breakdown=breakdown,
        orgs=orgs,
        fetched_at=fetched_at,
        error=usage_error,
        token_origin=origin,
        retry_after=retry_after,
    )
