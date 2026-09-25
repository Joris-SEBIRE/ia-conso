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

from dataclasses import replace

from .models import Account, AccountView, Extra, Refill, Snapshot, Window, next_month_start, now, parse_ts
from .paths import CLAUDE_JSON
from .state import cached_account_view, remember_account_view

KEYCHAIN_SERVICE = "Claude Code-credentials"
# `at_wall=1` est ce que Claude Code demande quand il veut connaître les remises à zéro
# proposées : sans ce paramètre, le serveur ne renvoie tout simplement pas les blocs d'offre.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage?at_wall=1"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
ACCOUNT_URL = "https://api.anthropic.com/api/oauth/account"
# Sans cet en-tête, l'endpoint range l'appel dans un seau 429 agressif. C'est le même que
# celui de Claude Code, qui est le client légitime de cet OAuth.
USER_AGENT = "claude-code/2.0.0"
BETA = "oauth-2025-04-20"
# Le garde-fou de cycle bloqué doit rester au-dessus de la somme des délais : on les tient courts.
HTTP_TIMEOUT = 8
KEYCHAIN_TIMEOUT = 10
# Claude Code propose parfois de remettre une limite à zéro. L'offre voyage sous des clés à nom
# de code, qui changent d'une expérience à l'autre : on lit celles qu'on connaît, et l'absence
# de la clé est le cas normal, pas une anomalie.
SESSION_RESET_KEY = "juniper_tide"
GRANTS_KEY = "cedar_ember"
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
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def read_token() -> tuple[str, str]:
    raw = _run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"])
    if not raw:
        raise ClaudeError("Aucun compte Claude Code dans le trousseau. Ouvre Cursor ou lance Claude Code une fois.")
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
    except (json.JSONDecodeError, OSError, UnicodeError):
        return None
    info = raw.get("oauthAccount") if isinstance(raw, dict) else None
    if not isinstance(info, dict):
        return None
    email = str(info.get("emailAddress") or "")
    name = str(info.get("fullName") or info.get("displayName") or "")
    if not email and not name:
        return None
    return Account(
        name=name or email,
        email=email,
        plan=PLANS.get(str(info.get("organizationType") or ""), "") or "Claude",
        org_id=str(info.get("organizationUuid") or ""),
        org_name=str(info.get("organizationName") or ""),
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
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = resp.read() or b"{}"
    except urllib.error.HTTPError as exc:
        retry = None
        if header := exc.headers.get("Retry-After"):
            try:
                retry = int(header.strip())
            except ValueError:
                retry = None
        detail = (exc.read() or b"").decode(errors="replace")[:200]
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
    except OSError as exc:
        # Timeout de lecture, coupure de socket : ne passe pas par URLError.
        raise ClaudeError(f"Réseau interrompu : {exc}") from exc
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ClaudeError("Anthropic a répondu autre chose que du JSON (portail captif ?).") from exc
    return data if isinstance(data, dict) else {}


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
            # Un plafond par modèle jamais entamé n'apprend rien, et porte parfois un nom de code.
            if not entry.get("is_active") and not entry.get("percent"):
                continue
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


def _extra(data: dict) -> Extra | None:
    """Crédits hors forfait, avec l'échéance de leur période quand elle s'applique."""
    extra = _read_extra(data)
    # Datée à la lecture : relue plus tard depuis le cache, elle ne doit pas glisser d'un mois.
    return replace(extra, resets_at=next_month_start()) if extra and extra.resets_with_period else extra


def _read_extra(data: dict) -> Extra | None:
    """Crédits hors forfait.

    `is_enabled` à faux ne veut pas dire « éteint » : Claude Code le définit comme « ne peut pas
    couvrir les envois en ce moment ». Seul `disabled_reason` dit pourquoi, et `spend_limit_reached`
    — porté par `extra_usage` seulement — dit si c'est un plafond atteint.
    """
    spend = data.get("spend") if isinstance(data.get("spend"), dict) else {}
    extra = data.get("extra_usage") if isinstance(data.get("extra_usage"), dict) else {}
    reached = bool(extra.get("spend_limit_reached"))
    if spend:
        used, currency = _money(spend.get("used") if isinstance(spend.get("used"), dict) else None)
        cap, cap_currency = _money(spend.get("limit") if isinstance(spend.get("limit"), dict) else None)
        reason = str(spend.get("disabled_reason") or "")
        enabled = bool(spend.get("enabled", True))
        # Sans plafond, il n'y a de ligne à montrer que pour dire pourquoi c'est coupé.
        if used is not None and (cap or (not enabled and reason)):
            percent = spend.get("percent")
            try:
                value = float(percent) if percent is not None else None
            except (TypeError, ValueError):
                value = None
            return Extra(
                used=used,
                cap=cap or 0.0,
                currency=cap_currency or currency,
                percent=value,
                is_enabled=enabled,
                disabled_reason=reason,
                limit_reached=reached,
            )
    if not extra:
        return None
    places = int(extra.get("decimal_places") or 2)
    cap_raw = extra.get("monthly_limit")
    reason = str(extra.get("disabled_reason") or "")
    enabled = bool(extra.get("is_enabled", True))
    if cap_raw is None and (enabled or not reason):
        return None
    return Extra(
        used=float(extra.get("used_credits") or 0) / (10**places),
        cap=float(cap_raw or 0) / (10**places),
        currency=str(extra.get("currency") or "EUR"),
        percent=float(extra["utilization"]) if extra.get("utilization") is not None else None,
        is_enabled=enabled,
        disabled_reason=reason,
        limit_reached=reached,
    )


def _whole(value, fallback: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _refills(data: dict) -> tuple[Refill, ...]:
    """Les remises à zéro proposées sur ce compte, ou rien du tout — le cas le plus courant.

    Ces blocs viennent d'expériences en cours : un champ peut manquer ou changer de type d'une
    semaine à l'autre. Comme Claude Code, on ignore ce qui ne se lit pas plutôt que d'échouer.
    Un compte déclaré inéligible n'affiche rien : l'offre ne le concerne pas.
    """
    found: list[Refill] = []
    session = data.get(SESSION_RESET_KEY) if isinstance(data.get(SESSION_RESET_KEY), dict) else {}
    if session.get("eligible"):
        found.append(
            Refill(
                label="reset de session",
                is_available=bool(session.get("available")),
                total=_whole(session.get("resets_per_week"), 1),
                available_at=parse_ts(session.get("next_available_at")),
            )
        )
    grants = data.get(GRANTS_KEY) if isinstance(data.get(GRANTS_KEY), dict) else {}
    if not grants.get("eligible"):
        return tuple(found)
    # Une temporisation vaut pour tout le bloc : aucune recharge n'est utilisable avant.
    cooldown = parse_ts(grants.get("cooldown_until"))
    for grant in grants.get("grants") or []:
        if not isinstance(grant, dict) or grant.get("paused"):
            continue
        left = _whole(grant.get("resets_left"))
        if not left or left <= 0:
            continue
        starts_at = parse_ts(grant.get("starts_at"))
        waiting = cooldown or (starts_at if starts_at and starts_at > now() else None)
        found.append(
            Refill(
                label=str(grant.get("label") or "recharge"),
                is_available=bool(grant.get("usable_now")) and waiting is None,
                left=left,
                total=_whole(grant.get("resets_total")) or None,
                available_at=waiting,
                ends_at=parse_ts(grant.get("ends_at")),
                needs_limit=bool(grant.get("use_requires_limit", True)),
            )
        )
    return tuple(found)


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
    return "chat" in (org.get("capabilities") or [])


def _memberships(account: dict | None, active_id: str) -> list[tuple[str, str, str]]:
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
        rows.append((org_id, str(org.get("name") or ""), plan_from_org(org) or "Claude"))
    rows.sort(key=lambda row: (0 if row[0] == active_id else 1, row[1].lower()))
    if active_id and active_id not in seen:
        rows.insert(0, (active_id, "", ""))
    return rows


def _views(account_payload: dict | None, active: Account | None, live: AccountView | None) -> tuple[AccountView, ...]:
    """Une vue par organisation : celle du token en direct, les autres depuis le cache."""
    active_id = active.org_id if active else ""
    email = active.email if active else ""
    memberships = _memberships(account_payload, active_id)
    if not memberships and (active_id or live is not None):
        # Le profil peut rater alors que l'usage a répondu : on montre quand même les chiffres.
        memberships = [(active_id, active.org_name if active else "", active.plan if active else "")]
    views: list[AccountView] = []
    for org_id, org_name, plan in memberships:
        if org_id == active_id and live is not None:
            view = AccountView(
                key=org_id,
                name=(active.org_name if active else "") or org_name,
                plan=(active.plan if active else "") or plan,
                email=email,
                is_active=True,
                session=live.session,
                weekly=live.weekly,
                scoped=live.scoped,
                extra=live.extra,
                refills=live.refills,
                breakdown=live.breakdown,
                fetched_at=live.fetched_at,
                is_live=True,
            )
            remember_account_view(view)
        else:
            view = cached_account_view(org_id, org_name, plan, email, is_active=org_id == active_id)
        views.append(view)
    return tuple(views)


def fetch() -> Snapshot:
    """Une lecture complète du compte Claude. `attempted_at` est posé même quand tout rate."""
    cached = cached_account()
    try:
        token, origin = read_token()
    except ClaudeError as exc:
        return Snapshot(account=cached, error=str(exc), attempted_at=now(), retry_after=exc.retry_after)

    usage = None
    usage_error = ""
    retry_after = None
    try:
        usage = _get(USAGE_URL, token)
    except ClaudeError as exc:
        usage_error = str(exc)
        retry_after = exc.retry_after

    try:
        profile = _get(PROFILE_URL, token)
    except ClaudeError:
        profile = None
    try:
        account_payload = _get(ACCOUNT_URL, token)
    except ClaudeError:
        account_payload = None

    account = _account(profile, cached)
    if account and isinstance(account_payload, dict):
        account = Account(
            name=str(account_payload.get("full_name") or account_payload.get("display_name") or account.name),
            email=str(account_payload.get("email_address") or account.email),
            plan=account.plan,
            org_id=account.org_id,
            org_name=account.org_name,
        )

    live = None
    if usage is not None:
        session, weekly, scoped = _from_limits(usage)
        if session is None and weekly is None:
            session, weekly, legacy_scoped = _from_legacy(usage)
            scoped = scoped or legacy_scoped
        live = AccountView(
            key=account.org_id if account else "",
            session=session,
            weekly=weekly,
            scoped=tuple(scoped),
            extra=_extra(usage),
            refills=_refills(usage),
            breakdown=_breakdown(usage),
            fetched_at=now(),
        )

    views = _views(account_payload, account, live)
    active = next((view for view in views if view.is_active), None)
    return Snapshot(
        account=account,
        views=views,
        fetched_at=active.fetched_at if active else None,
        attempted_at=now(),
        error=usage_error,
        token_origin=origin,
        retry_after=retry_after,
    )
