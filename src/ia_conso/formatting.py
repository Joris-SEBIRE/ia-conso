"""Formatage des libellés affichés dans le menu et dans `--print`."""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Snapshot, Window

UNITS: tuple[tuple[str, int], ...] = (
    ("j", 86400),
    ("h", 3600),
    ("min", 60),
    ("s", 1),
)
WEEKDAYS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
CURRENCY = {"EUR": "€", "USD": "$", "GBP": "£"}


def _plural(label: str, count: int) -> str:
    return f"{count} {label}"


def spell(seconds: int) -> str:
    """Durée dans les deux plus grandes unités qui se suivent : « 3 h 25 min », « 4 j 10 h »."""
    seconds = max(0, int(seconds))
    for index, (label, size) in enumerate(UNITS):
        if count := seconds // size:
            parts = [_plural(label, count)]
            if index + 1 < len(UNITS):
                below, unit = UNITS[index + 1]
                if extra := (seconds - count * size) // unit:
                    parts.append(_plural(below, extra))
            return " ".join(parts)
    return "maintenant"


def countdown(seconds: int) -> str:
    return spell(seconds) if seconds > 0 else "maintenant"


def remaining(moment: datetime | None, at: datetime | None = None) -> int:
    if moment is None:
        return 0
    current = at or datetime.now(timezone.utc)
    return max(0, int((moment - current).total_seconds()))


JUST_NOW = 10


def ago(moment: datetime | None, at: datetime | None = None) -> str:
    if moment is None:
        return ""
    seconds = max(0, int(((at or datetime.now(timezone.utc)) - moment).total_seconds()))
    return "à l'instant" if seconds < JUST_NOW else f"il y a {spell(seconds)}"


def clock(moment: datetime | None) -> str:
    """Heure locale du reset, avec le jour si ce n'est pas aujourd'hui."""
    if moment is None:
        return ""
    local = moment.astimezone()
    today = datetime.now().astimezone().date()
    hour = local.strftime("%H:%M")
    if local.date() == today:
        return hour
    return f"{WEEKDAYS[local.weekday()]} {hour}"


def reset_line(window: Window, at: datetime | None = None) -> str:
    if window.resets_at is None:
        return "pas encore commencée" if window.is_session else "reset inconnu"
    left = remaining(window.resets_at, at)
    when = clock(window.resets_at)
    wait = countdown(left)
    if left <= 0:
        return "reset imminent"
    return f"reset dans {wait}" + (f" · {when}" if when else "")


def extra_reset_line(extra, at: datetime | None = None) -> str:
    if extra.resets_at is None:
        return "reset mensuel"
    left = remaining(extra.resets_at, at)
    when = clock(extra.resets_at)
    wait = countdown(left)
    if left <= 0:
        return "reset imminent"
    return f"reset dans {wait}" + (f" · {when}" if when else "")


def percent(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{int(round(value))} %"


def money(amount: float, currency: str) -> str:
    symbol = CURRENCY.get(currency, currency)
    text = f"{amount:,.2f}".replace(",", "\u00a0").replace(".", ",")
    if currency == "USD":
        return f"{symbol}{text}"
    return f"{text} {symbol}"


def tint_for(value: float | None) -> str:
    """Teinte du pourcentage : bleu → vert → jaune → orange → rouge, par tranches de 20 %."""
    if value is None:
        return "systemBlueColor"
    if value >= 80:
        return "systemRedColor"
    if value >= 60:
        return "systemOrangeColor"
    if value >= 40:
        return "systemYellowColor"
    if value >= 20:
        return "systemGreenColor"
    return "systemBlueColor"


def dump(snapshot: Snapshot) -> str:
    lines: list[str] = []
    if snapshot.account:
        who = snapshot.account
        lines.append(f"{who.name} · {who.email} · {who.plan}".strip(" ·"))
    if snapshot.token_origin:
        lines.append(f"token {snapshot.token_origin}")
    if snapshot.error:
        lines.append(f"erreur : {snapshot.error}")
    if snapshot.session:
        lines.append(f"session 5 h  {percent(snapshot.session.percent)}  {reset_line(snapshot.session)}")
    if snapshot.weekly:
        lines.append(f"semaine      {percent(snapshot.weekly.percent)}  {reset_line(snapshot.weekly)}")
    for window in snapshot.scoped:
        lines.append(f"{window.label:<12} {percent(window.percent)}  {reset_line(window)}")
    if snapshot.extra and snapshot.extra.cap:
        extra = snapshot.extra
        used = f"{money(extra.used, extra.currency)} / {money(extra.cap, extra.currency)}"
        state = "" if extra.is_enabled else "  off"
        lines.append(
            f"extra        {used}"
            + (f"  {percent(extra.percent)}" if extra.percent is not None else "")
            + state
            + f"  {extra_reset_line(extra)}"
        )
    if snapshot.breakdown:
        lines.append("répartition de la semaine")
        for name, share in snapshot.breakdown:
            lines.append(f"  {name:<16} {percent(share)} de la conso")
    others = [org for org in snapshot.orgs if not org.is_active]
    if others:
        lines.append("autres comptes")
        for org in others:
            title = org.org_name or org.plan or org.org_id
            if org.session or org.weekly:
                bits = []
                if org.session:
                    bits.append(f"session {percent(org.session.percent)}")
                if org.weekly:
                    bits.append(f"semaine {percent(org.weekly.percent)}")
                age = f" · lu {ago(org.fetched_at)}" if org.fetched_at else ""
                lines.append(f"  {title}: {' · '.join(bits)}{age}")
            else:
                lines.append(f"  {title}: pas encore capturé")
    return "\n".join(lines) or "aucune donnée"
