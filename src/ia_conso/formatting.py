"""Formatage des libellés affichés dans le menu et dans `--print`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Snapshot, Window

# Même échelle que GitTodo / LinearTodo : deux plus grandes unités qui se suivent.
UNITS: tuple[tuple[str, int], ...] = (
    ("an", 365 * 86400),
    ("mois", 30 * 86400),
    ("sem", 7 * 86400),
    ("j", 86400),
    ("h", 3600),
    ("min", 60),
    ("s", 1),
)
JUST_NOW = 10
DAY = 86400
FRESH_OK = 5 * 60
FRESH_STALE = 3600
WEEKDAYS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
MONTHS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")
CURRENCY = {"EUR": "€", "USD": "$", "GBP": "£"}


def _plural(label: str, count: int) -> str:
    return f"{count} {label}s" if label == "an" and count > 1 else f"{count} {label}"


def spell(seconds: int) -> str:
    """Durée dans les deux plus grandes unités, à condition qu'elles se suivent.

    « 3 h 25 min », « 2 mois 1 sem », « 1 an 2 mois ». Si l'unité juste en dessous de la
    plus grande est nulle, elle n'est pas affichée : 2 ans et 3 jours donne « 2 ans ».
    """
    seconds = max(0, int(seconds))
    for index, (label, size) in enumerate(UNITS):
        if count := seconds // size:
            parts = [_plural(label, count)]
            if index + 1 < len(UNITS):
                below, unit = UNITS[index + 1]
                if extra := (seconds - count * size) // unit:
                    parts.append(_plural(below, extra))
            return " ".join(parts)
    return ""


def countdown(seconds: int) -> str:
    return spell(seconds) if seconds > 0 else "maintenant"


def remaining(moment: datetime | None, at: datetime | None = None) -> int:
    if moment is None:
        return 0
    current = at or datetime.now(timezone.utc)
    return max(0, int((moment - current).total_seconds()))


def until(moment: datetime, now: datetime | None = None) -> str:
    """Délai restant avant une échéance (LinearTodo) : « dans 3 h 25 min »."""
    seconds = int((moment - (now or datetime.now(timezone.utc))).total_seconds())
    return f"dans {spell(seconds)}" if seconds > 0 else "maintenant"


def ago(moment: datetime | None, at: datetime | None = None) -> str:
    if moment is None:
        return ""
    seconds = max(0, int(((at or datetime.now(timezone.utc)) - moment).total_seconds()))
    return "à l'instant" if seconds < JUST_NOW else f"il y a {spell(seconds)}"


def age_seconds(moment: datetime | None, at: datetime | None = None) -> int | None:
    if moment is None:
        return None
    return max(0, int(((at or datetime.now(timezone.utc)) - moment).total_seconds()))


def freshness_tint(seconds: int | None) -> str:
    """Vert < 5 min, jaune jusqu'à 1 h, rouge au-delà (ou jamais lu)."""
    if seconds is None:
        return "systemRedColor"
    if seconds < FRESH_OK:
        return "systemGreenColor"
    if seconds < FRESH_STALE:
        return "systemYellowColor"
    return "systemRedColor"


def since_read(moment: datetime | None, at: datetime | None = None) -> str:
    """Âge de la dernière lecture : « depuis 3 min », « à l'instant », « pas encore lu »."""
    seconds = age_seconds(moment, at)
    if seconds is None:
        return "pas encore lu"
    if seconds < JUST_NOW:
        return "à l'instant"
    return f"depuis {spell(seconds)}"


def freshness_label(is_active: bool, moment: datetime | None, at: datetime | None = None) -> str:
    """« actif depuis 3 min » / « inactif · pas encore lu »."""
    status = "actif" if is_active else "inactif"
    age = since_read(moment, at)
    if age.startswith("depuis "):
        return f"{status} {age}"
    if age == "à l'instant":
        return f"{status} à l'instant"
    return f"{status} · {age}"


def _next_weekday(today, weekday: int):
    """Prochaine occurrence du jour (aujourd'hui exclu : un mercredi « prochain » est à +7 j)."""
    delta = (weekday - today.weekday()) % 7
    if delta == 0:
        delta = 7
    return today + timedelta(days=delta)


def _date(local: datetime, today) -> str:
    """Date complète : « lun. 21 sept. » (année si besoin)."""
    month = MONTHS[local.month - 1]
    day = f"{WEEKDAYS[local.weekday()]} {local.day} {month}"
    if local.year != today.year:
        return f"{day} {local.year}"
    return day


def pinpoint(moment: datetime | None, at: datetime | None = None) -> str:
    """Horodatage absolu du reset, calibré sur la distance.

    - moins de 24 h, aujourd'hui : « 18:20 »
    - moins de 24 h, demain : « sam. à 02:00 »
    - prochain jour de la semaine : « mer. à 04:00 »
    - plus loin : « lun. 21 sept. à 04:00 » (année si besoin)
    """
    if moment is None:
        return ""
    current = (at or datetime.now(timezone.utc)).astimezone()
    local = moment.astimezone()
    left = (local - current).total_seconds()
    today = current.date()
    tomorrow = today + timedelta(days=1)
    hour = local.strftime("%H:%M")

    def with_day(prefix: str) -> str:
        return f"{prefix} à {hour}"

    if 0 <= left < DAY:
        if local.date() == today:
            return hour
        if local.date() == tomorrow:
            return with_day(WEEKDAYS[local.weekday()])
        if local.date() == _next_weekday(today, local.weekday()):
            return with_day(WEEKDAYS[local.weekday()])
        return with_day(_date(local, today))

    if local.date() == _next_weekday(today, local.weekday()):
        return with_day(WEEKDAYS[local.weekday()])
    return with_day(_date(local, today))


def reset_bits(resets_at: datetime | None, *, is_session: bool = False, at: datetime | None = None) -> tuple[str, str]:
    """(libellé gris, horodatage à mettre en évidence) pour une ligne de reset."""
    if resets_at is None:
        return ("pas encore commencée" if is_session else "reset inconnu", "")
    left = remaining(resets_at, at)
    when = pinpoint(resets_at, at)
    if left <= 0:
        return ("reset imminent", when)
    return (f"reset {until(resets_at, at)}", when)


def reset_line(window: Window, at: datetime | None = None) -> str:
    text, when = reset_bits(window.resets_at, is_session=window.is_session, at=at)
    return f"{text} · {when}" if when else text


def extra_reset_line(extra, at: datetime | None = None) -> str:
    if extra.resets_at is None:
        return "reset mensuel"
    text, when = reset_bits(extra.resets_at, at=at)
    return f"{text} · {when}" if when else text


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
