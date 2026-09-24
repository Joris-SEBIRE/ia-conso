"""Formatage des libellés affichés dans le menu et dans `--print`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import AccountView, Extra, Snapshot, Window

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
DISABLED_REASONS = {
    "out_of_credits": "crédits épuisés",
    "user_disabled": "désactivé manuellement",
    "org_level_disabled_until": "coupé par l'organisation",
}


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
    return max(0, int((moment - (at or datetime.now(timezone.utc))).total_seconds()))


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
    return today + timedelta(days=delta or 7)


def _date(local: datetime, today) -> str:
    """Date complète : « lun. 21 sept. » (année si besoin)."""
    day = f"{WEEKDAYS[local.weekday()]} {local.day} {MONTHS[local.month - 1]}"
    return f"{day} {local.year}" if local.year != today.year else day


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
    hour = local.strftime("%H:%M")

    if 0 <= left < DAY:
        if local.date() == today:
            return hour
        if local.date() in (today + timedelta(days=1), _next_weekday(today, local.weekday())):
            return f"{WEEKDAYS[local.weekday()]} à {hour}"
        return f"{_date(local, today)} à {hour}"
    if local.date() == _next_weekday(today, local.weekday()):
        return f"{WEEKDAYS[local.weekday()]} à {hour}"
    return f"{_date(local, today)} à {hour}"


def reset_bits(resets_at: datetime | None, *, is_session: bool = False, at: datetime | None = None) -> tuple[str, str]:
    """(libellé gris, horodatage à mettre en évidence) pour une ligne de reset."""
    if resets_at is None:
        return ("pas encore commencée" if is_session else "reset inconnu", "")
    when = pinpoint(resets_at, at)
    if remaining(resets_at, at) <= 0:
        return ("reset imminent", when)
    return (f"reset {until(resets_at, at)}", when)


def reset_line(window: Window, at: datetime | None = None) -> str:
    text, when = reset_bits(window.resets_at, is_session=window.is_session, at=at)
    return f"{text} · {when}" if when else text


def extra_bits(extra: Extra, at: datetime | None = None) -> tuple[str, str]:
    """Aucune API ne date la remise à zéro des crédits : on ne promet qu'un rythme."""
    if extra.resets_at is None:
        return ("reset mensuel", "")
    return reset_bits(extra.resets_at, at=at)


def percent(value: float | None) -> str:
    return "—" if value is None else f"{int(round(value))} %"


def money(amount: float, currency: str) -> str:
    symbol = CURRENCY.get(currency, currency)
    text = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
    return f"{symbol}{text}" if currency == "USD" else f"{text} {symbol}"


def amounts(used: float | None, cap: float | None, currency: str) -> str:
    """« $25,08 / $472,50 », ou le seul montant connu."""
    if used is None:
        return ""
    if not cap:
        return money(used, currency)
    return f"{money(used, currency)} / {money(cap, currency)}"


def tokens(count: int) -> str:
    """Contexte occupé : « 253 k », « 1,2 M »."""
    if count <= 0:
        return ""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}".replace(".", ",") + " M"
    return f"{count // 1000} k"


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


def refill_line(refill, at: datetime | None = None) -> str:
    """« reset de session disponible · /limit-reset » ou « prochain reset · mar. 30 sept. »."""
    compte = ""
    if refill.left is not None:
        compte = f"{refill.left} sur {refill.total}" if refill.total else str(refill.left)
    if refill.is_available:
        bits = [f"{refill.label} disponible" if not compte else f"{refill.label} · {compte}"]
        if refill.needs_limit:
            bits.append("une fois la limite atteinte")
        bits.append("/limit-reset dans Claude Code")
    elif refill.available_at is not None:
        bits = [f"{refill.label} · de nouveau {until(refill.available_at, at)}", pinpoint(refill.available_at, at)]
    else:
        bits = [refill.label + (f" · {compte}" if compte else "")]
    if refill.ends_at is not None:
        bits.append(f"à utiliser avant {pinpoint(refill.ends_at, at)}")
    return "  ·  ".join(bit for bit in bits if bit)


def account_title(name: str, plan: str) -> str:
    """Nom d'affichage d'un compte : raccourcit l'org perso, et porte toujours le plan à côté."""
    if any(mark in name for mark in ("'s Organization", "’s Organization", "'s Individual Org")):
        short = "Perso"
    else:
        short = name or plan or "Compte"
    return f"{short} · {plan}" if plan and short != plan else short


# Les crans du sélecteur d'effort de Claude Code, écrits comme lui les écrit : c'est ce que
# l'utilisateur lit dans son interface, et le seul repère qui lui permette de recouper.
# « Ultracode » en est le sixième : il pose l'effort à xhigh et arme les workflows.
EFFORT_LABELS = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high", "max": "Max"}
ULTRACODE_LABEL = "Ultracode"
# Une couleur par famille de modèle : reconnaître Opus d'un coup d'œil vaut mieux qu'un mot de plus.
MODEL_TINTS = {
    "opus": "systemPurpleColor",
    "sonnet": "systemTealColor",
    "haiku": "systemGreenColor",
    "fable": "systemIndigoColor",
}


def effort_label(effort: str) -> str:
    """« xhigh » → « Extra high » ; un niveau inconnu est rendu tel quel."""
    text = str(effort or "").strip().lower()
    return EFFORT_LABELS.get(text, text)


def model_tint(model: str) -> str:
    """La couleur d'une famille de modèle, ou l'identité de l'app pour tout le reste."""
    first = str(model or "").split()[0].lower() if model else ""
    return MODEL_TINTS.get(first, "IDENTITY")


def activity_bits(item, now_ms: int) -> list[tuple[str, str]]:
    """Les morceaux d'une ligne d'activité, chacun avec son rôle d'affichage.

    L'ordre place en tête ce qui coûte — le modèle, puis l'effort — et laisse en gris ce qui
    décrit l'avancement. L'état « en cours » n'est pas écrit : il est porté par la couleur.
    """
    bits: list[tuple[str, str]] = [("title", item.title.strip() or "session")]
    if item.is_waiting:
        bits.append(("alert", "en attente de réponse"))
    if item.model:
        bits.append(("model", item.model))
    if getattr(item, "is_ultra", False):
        bits.append(("effort", ULTRACODE_LABEL))
    elif label := effort_label(item.effort):
        bits.append(("effort", label))
    if item.agents:
        bits.append(("muted", f"{item.agents} agent" + ("s" if item.agents > 1 else "")))
    if item.context_percent is not None:
        bits.append(("muted", f"contexte {percent(item.context_percent)}"))
    elif item.context_tokens:
        bits.append(("muted", f"contexte {tokens(item.context_tokens)}"))
    if item.since_ms and (elapsed := spell(max(0, (now_ms - int(item.since_ms)) // 1000))):
        bits.append(("muted", f"depuis {elapsed}"))
    return bits


def activity_detail(item, now_ms: int) -> str:
    """La même ligne, à plat, pour `--print`."""
    return "  ·  ".join(text for _, text in activity_bits(item, now_ms))


def _view_lines(view: AccountView) -> list[str]:
    lines = [f"{account_title(view.name, view.plan)}  {freshness_label(view.is_active, view.fetched_at)}"]
    if view.error:
        lines.append(f"  {view.error}")
    for window in (view.session, view.weekly, *view.scoped):
        if window is None:
            continue
        detail = amounts(window.used, window.cap, window.currency)
        lines.append(
            f"  {window.label:<16} {percent(window.percent):>5}  {reset_line(window)}"
            + (f"  ·  {detail}" if detail else "")
        )
    if view.extra:
        extra = view.extra
        state = "" if extra.is_enabled else f"  {DISABLED_REASONS.get(extra.disabled_reason, 'désactivé')}"
        lines.append(
            f"  {'extra':<16} {percent(extra.percent):>5}  {' · '.join(p for p in extra_bits(extra) if p)}"
            + (f"  ·  {amounts(extra.used, extra.cap, extra.currency)}" if extra.cap else "")
            + state
        )
    for refill in view.refills:
        lines.append(f"  ↺ {refill_line(refill)}")
    for name, share in view.breakdown:
        if share > 0:
            lines.append(f"    {name:<14} {percent(share)} de la conso")
    if not view.has_figures and not view.error:
        lines.append("  conso inconnue : passe sur ce compte une fois")
    return lines


def dump(snapshot: Snapshot, activity=None) -> str:
    """Ce que le menu contiendrait, en texte : comptes, conso, et sessions en cours."""
    lines: list[str] = []
    if snapshot.account:
        who = snapshot.account
        lines.append(f"{who.name} · {who.email} · {who.plan}".strip(" ·"))
    if snapshot.token_origin:
        lines.append(f"token {snapshot.token_origin}")
    if snapshot.error:
        lines.append(f"erreur : {snapshot.error}")
    for view in snapshot.views:
        lines.append("")
        lines.extend(_view_lines(view))
    if activity is not None:
        lines.append("")
        lines.append(f"activité : {activity.running_count} en cours · {activity.waiting_count} en attente")
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        for item in activity.items:
            lines.append(f"  {activity_detail(item, now_ms)}")
    return "\n".join(lines).strip() or "aucune donnée"
