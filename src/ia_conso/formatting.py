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
# Les raisons que Claude Code connaît pour un extra qui ne couvre pas les envois, et leur nature.
# Trois sont des blocages — l'extra est actif mais à sec, jusqu'à la fin de la période ou jusqu'au
# prochain achat — les autres sont de vraies coupures, que seul un admin peut lever.
BLOCKED_BY_ORG_CAP, BLOCKED_BY_OWN_CAP, BLOCKED_BY_BALANCE, TURNED_OFF = "org_cap", "own_cap", "balance", "off"
EXTRA_REASONS = {
    "org_level_disabled_until": ("plafond d'équipe atteint", BLOCKED_BY_ORG_CAP),
    "org_spend_cap_reached": ("plafond individuel atteint", BLOCKED_BY_OWN_CAP),
    "out_of_credits": ("solde prépayé épuisé", BLOCKED_BY_BALANCE),
    "org_level_disabled": ("coupé par l'organisation", TURNED_OFF),
    "org_service_level_disabled": ("service coupé pour l'organisation", TURNED_OFF),
    "member_level_disabled": ("coupé par l'admin", TURNED_OFF),
    "member_zero_credit_limit": ("plafond fixé à zéro par l'admin", TURNED_OFF),
    "seat_tier_level_disabled": ("non inclus dans ce type de siège", TURNED_OFF),
    "seat_tier_zero_credit_limit": ("non inclus dans ce type de siège", TURNED_OFF),
    "group_zero_credit_limit": ("plafond du groupe fixé à zéro", TURNED_OFF),
    "overage_not_provisioned": ("jamais activé", TURNED_OFF),
    "no_limits_configured": ("aucun plafond configuré", TURNED_OFF),
    "user_disabled": ("désactivé manuellement", TURNED_OFF),
}


def extra_reason(extra: Extra) -> tuple[str, str]:
    """(libellé, nature) de l'état d'un extra qui ne couvre pas les envois.

    `org_level_disabled_until` n'est un plafond atteint que si le serveur le confirme ; sinon,
    Claude Code le traite comme une coupure ordinaire.
    """
    label, nature = EXTRA_REASONS.get(extra.disabled_reason, ("indisponible", TURNED_OFF))
    if nature == BLOCKED_BY_ORG_CAP and not extra.limit_reached:
        return ("coupé par l'organisation pour la période", TURNED_OFF)
    return label, nature


def extra_gauge(extra: Extra) -> tuple[float, str]:
    """(remplissage de la jauge, détail) : ce qu'on peut montrer honnêtement de cet extra.

    La jauge est toujours celle de l'utilisateur : sa dépense sur son propre plafond. Un plafond
    d'équipe atteint n'est qu'une contrainte, dite dans le détail — l'utilisateur ne reçoit ni ce
    plafond ni ce que l'équipe a dépensé. Sans plafond individuel, la jauge reste vide plutôt que de
    céder la place à un trait : le détail dit la somme réellement dépensée.
    """
    if extra.rearmed_at is not None:
        # Reparti de zéro à cette date ; l'en-tête le dit, ce qui a suivi est inconnu.
        return 0.0, ""
    spent = amounts(extra.used, extra.cap, extra.currency) if extra.cap else ""
    ratio = extra.percent
    if ratio is None and extra.cap:
        ratio = 100.0 * extra.used / extra.cap
    if extra.is_enabled:
        return ratio or 0.0, spent
    label, nature = extra_reason(extra)
    if nature == BLOCKED_BY_ORG_CAP:
        if extra.cap:
            return ratio or 0.0, f"{spent} · {label}"
        return 0.0, f"toi : {money(extra.used, extra.currency)}, sans plafond perso · {label}"
    if nature == BLOCKED_BY_OWN_CAP:
        return (ratio if ratio is not None else 100.0), f"{spent} · {label}" if spent else label
    return ratio or 0.0, f"{spent} · {label}" if spent else label


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
    """Ce qui débloquera cet extra, et quand.

    L'API de conso ne date pas la fin de la période : elle est posée à la lecture, au 1er du mois
    suivant, comme Claude Code la calcule lui-même, et affichée « estimée ». Un solde à sec ne se
    recharge pas avec le mois, et une coupure par un admin n'a pas d'échéance.
    """
    if extra.rearmed_at is not None:
        return (f"réarmé {pinpoint(extra.rearmed_at, at)} · conso inconnue depuis", "")
    if extra.resets_at is not None:
        return (f"réarmement estimé {until(extra.resets_at, at)}", pinpoint(extra.resets_at, at))
    if not extra.is_enabled and extra_reason(extra)[1] == BLOCKED_BY_BALANCE:
        return ("débloqué au prochain achat de crédits", "")
    return ("", "")


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


def window_reset_bits(window: Window, at: datetime | None = None) -> tuple[str, str]:
    """La ligne d'échéance, ou, pour une fenêtre réarmée, ce qu'on en sait vraiment."""
    if window.rearmed_at is not None:
        return (f"réarmée {pinpoint(window.rearmed_at, at)} · conso inconnue depuis", "")
    return reset_bits(window.resets_at, is_session=window.is_session, at=at)


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
    if getattr(item, "background", 0):
        count = item.background
        bits.append(("muted", f"{count} tâche{'s' if count > 1 else ''} de fond"))
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
        text, when = window_reset_bits(window)
        lines.append(
            f"  {window.label:<16} {percent(window.percent):>5}  {' · '.join(p for p in (text, when) if p)}"
            + (f"  ·  {detail}" if detail else "")
        )
    if view.extra:
        value, detail = extra_gauge(view.extra)
        tail = "  ·  ".join(p for p in (" · ".join(p for p in extra_bits(view.extra) if p), detail) if p)
        lines.append(f"  {'extra':<16} {percent(value):>5}  {tail}".rstrip())
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
