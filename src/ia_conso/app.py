"""Élément de barre des menus : pourcentage de session, anneau semaine, menu détaillé."""

from __future__ import annotations

import sys
import threading
import time
import traceback
from dataclasses import replace

import objc
from Cocoa import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBaselineOffsetAttributeName,
    NSBezierPath,
    NSBundle,
    NSColor,
    NSCompositingOperationSourceOver,
    NSCursor,
    NSFont,
    NSFontAttributeName,
    NSFontWeightLight,
    NSFontWeightMedium,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageSymbolConfiguration,
    NSImageSymbolScaleSmall,
    NSMakePoint,
    NSMakeRect,
    NSMakeSize,
    NSMenu,
    NSMenuItem,
    NSMutableAttributedString,
    NSMutableParagraphStyle,
    NSObject,
    NSParagraphStyleAttributeName,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSStatusBar,
    NSTimer,
    NSVariableStatusItemLength,
    NSWorkspace,
    NSZeroRect,
)
from PyObjCTools import AppHelper

from . import IDENTITY_TINT, launchagent, shortcuts
from . import help as manual
from .activity import Activity, probe as probe_activity
from .anthropic import fetch as fetch_claude
from .avatars import Avatars
from .config import Config
from .cursor import fetch as fetch_cursor
from .formatting import (
    DISABLED_REASONS,
    account_title,
    activity_bits,
    age_seconds,
    amounts,
    countdown,
    extra_bits,
    freshness_label,
    freshness_tint,
    model_tint,
    percent,
    refill_line,
    reset_bits,
    tint_for,
)
from .models import CLAUDE, CURSOR, AccountView, Extra, Snapshot, Window, now
from .paths import CONFIG_PATH
from .state import acquire_single_instance, log_error, write_status

BUNDLE_ID = "fr.jsebire.ia-conso"
# Tick UI + sondage local. 0,5 s : on remarque la fin d'un run dès que Cursor écrit sa DB.
TICK_SECONDS = 0.5
ACTIVITY_PROBE_SECONDS = 1.0
# Au-dessus de la somme des délais réseau d'un cycle (trousseau 10 s + 5 appels à 8 s).
STUCK_AFTER = 90.0
FROZEN_AFTER = 180.0
# Après un échec, on attend au moins ça avant de retenter, même sans Retry-After.
RETRY_FLOOR = 30
RING_SIZE = 22.0
RING_WIDTH = 1.75
RING_TRACK_ALPHA = 0.28
# Pastilles de compte (même géométrie que GitTodo / LinearTodo).
COUNT_HEIGHT, COUNT_RADIUS, COUNT_PADDING = 11.0, 5.5, 4.0
COUNT_FONT = 9.0
COUNT_OVERLAP = 8.0
TITLE_FONT = 13.0
META_FONT = 11.0
HEADER_FONT = 10.0
HERO_FONT = 22.0
CHROME_GLYPH = 11.0
CHROME_LIFT = 1.75
LEFT_MARGIN = 6.0
AVATAR_SIZE = 22.0
SPINNER_FRAMES = ("◐", "◓", "◑", "◒")
SPINNER_INTERVAL = 0.13
BAR_WIDTH = 12
SHORTCUT_MIN = 84.0
# « 100 % » : largeur fixe pour aligner les jauges d'un compte à l'autre.
PERCENT_FIELD = 5
PERCENT_PAD = " "  # figure space = largeur d'un chiffre
BAR_GAP = "  "
BAR_BASELINE = (HERO_FONT - META_FONT) * 0.35
ACTIVITY_TITLE_MAX = 42
ACTIVITY_SEP = "  ·  "
# L'effort reprend la couleur du modèle, en retrait : il la précise, il ne la concurrence pas.
EFFORT_ALPHA = 0.75

_LABELS: dict[tuple, object] = {}
_CHROME: dict[tuple, object] = {}


def _colour(name: str):
    return getattr(NSColor, IDENTITY_TINT if name == "IDENTITY" else name)()


def _paragraph(before: float = 0.0, after: float = 1.0):
    style = NSMutableParagraphStyle.alloc().init()
    style.setParagraphSpacingBefore_(before)
    style.setParagraphSpacing_(after)
    style.setLineSpacing_(1.0)
    return style


def _run(text: str, size: float, color=None, weight=None, paragraph=None, mono: bool = False, baseline: float = 0.0):
    attributes = {
        NSFontAttributeName: (
            NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight or NSFontWeightSemibold)
            if mono
            else (NSFont.systemFontOfSize_(size) if weight is None else NSFont.systemFontOfSize_weight_(size, weight))
        )
    }
    if color is not None:
        attributes[NSForegroundColorAttributeName] = color
    if paragraph is not None:
        attributes[NSParagraphStyleAttributeName] = paragraph
    if baseline:
        attributes[NSBaselineOffsetAttributeName] = baseline
    return NSAttributedString.alloc().initWithString_attributes_(text, attributes)


def _bar(value: float, width: int = BAR_WIDTH) -> str:
    filled = max(0, min(width, int(round(value / 100.0 * width))))
    return "█" * filled + "░" * (width - filled)


def _percent_field(value: float | None) -> str:
    return percent(value).rjust(PERCENT_FIELD, PERCENT_PAD)


def _meter_title(label: str, value: float | None, detail: str, reset: str = "", when: str = ""):
    """Bloc commun : libellé + reset, puis « N %  ████ » teintés comme l'icône."""
    tint = _colour(tint_for(value))
    grey = NSColor.secondaryLabelColor()
    text = NSMutableAttributedString.alloc().init()
    text.appendAttributedString_(
        _run(label.upper(), HEADER_FONT, color=tint, weight=NSFontWeightSemibold, paragraph=_paragraph())
    )
    if reset:
        text.appendAttributedString_(_run(f"  ·  {reset}", META_FONT, color=grey, weight=NSFontWeightMedium))
    if when:
        text.appendAttributedString_(
            _run(f" · {when}", META_FONT, color=_colour(IDENTITY_TINT), weight=NSFontWeightSemibold)
        )
    text.appendAttributedString_(
        _run(
            f"\n{_percent_field(value)}",
            HERO_FONT,
            color=tint,
            weight=NSFontWeightSemibold,
            paragraph=_paragraph(before=2.0),
            mono=True,
        )
    )
    if value is not None:
        text.appendAttributedString_(
            _run(
                f"{BAR_GAP}{_bar(value)}",
                META_FONT,
                color=tint.colorWithAlphaComponent_(0.85),
                weight=NSFontWeightMedium,
                mono=True,
                baseline=BAR_BASELINE,
            )
        )
    if detail:
        text.appendAttributedString_(
            _run(f"  ·  {detail}", META_FONT, color=grey, weight=NSFontWeightMedium, baseline=BAR_BASELINE)
        )
    return text


def _window_title(window: Window):
    reset, when = reset_bits(window.resets_at, is_session=window.is_session)
    return _meter_title(window.label, window.percent, amounts(window.used, window.cap, window.currency), reset, when)


def _extra_title(extra: Extra):
    detail = amounts(extra.used, extra.cap, extra.currency) if extra.cap else ""
    if not extra.is_enabled:
        why = DISABLED_REASONS.get(extra.disabled_reason, "désactivé")
        detail = f"{detail} · {why}" if detail else why
    value = extra.percent
    if value is None:
        value = (100.0 * extra.used / extra.cap) if extra.cap else None
    reset, when = extra_bits(extra)
    return _meter_title("extra", value, detail, reset, when)


def _refill_title(refill):
    """Une remise à zéro proposée par Claude Code, mise en avant seulement si elle est utilisable."""
    tint = _colour(IDENTITY_TINT) if refill.is_available else NSColor.secondaryLabelColor()
    weight = NSFontWeightSemibold if refill.is_available else NSFontWeightMedium
    text = NSMutableAttributedString.alloc().init()
    text.appendAttributedString_(_run("↺  ", META_FONT, color=tint, weight=weight))
    text.appendAttributedString_(_run(refill_line(refill), META_FONT, color=tint, weight=weight))
    return text


def _crop(title: str) -> str:
    text = (title or "").strip() or "session"
    return text if len(text) <= ACTIVITY_TITLE_MAX else text[: ACTIVITY_TITLE_MAX - 1].rstrip() + "…"


def _activity_style(role: str, item) -> tuple:
    """(couleur, graisse) d'un morceau de ligne d'activité.

    Le modèle porte la couleur de sa famille et l'effort la reprend, en retrait : les deux se
    lisent ensemble. Tout ce qui décrit l'avancement passe en gris, pour ne pas concurrencer la
    seule information qu'on cherche de loin — sur quoi tourne cette session.
    """
    if role == "alert":
        return _colour("systemRedColor"), NSFontWeightSemibold
    if role == "title":
        return _colour("systemRedColor" if item.is_waiting else IDENTITY_TINT), NSFontWeightSemibold
    if role == "model":
        return _colour(model_tint(item.model)), NSFontWeightSemibold
    if role == "effort":
        return _colour(model_tint(item.model)).colorWithAlphaComponent_(EFFORT_ALPHA), NSFontWeightMedium
    return NSColor.secondaryLabelColor(), NSFontWeightMedium


def _append_activity(text, items) -> None:
    """Une ligne par session : son titre, le modèle et l'effort, puis son avancement en gris."""
    if not items:
        return
    now_ms = int(time.time() * 1000)
    for index, item in enumerate(items):
        arrow = _colour("systemRedColor" if item.is_waiting else IDENTITY_TINT)
        text.appendAttributedString_(
            _run("\n", META_FONT, paragraph=_paragraph(before=2.0 if index == 0 else 1.0))
        )
        text.appendAttributedString_(_run("→ ", META_FONT, color=arrow, weight=NSFontWeightSemibold))
        for position, (role, raw) in enumerate(activity_bits(item, now_ms)):
            if position:
                text.appendAttributedString_(
                    _run(ACTIVITY_SEP, META_FONT, color=NSColor.tertiaryLabelColor(), weight=NSFontWeightMedium)
                )
            colour, weight = _activity_style(role, item)
            text.appendAttributedString_(
                _run(_crop(raw) if role == "title" else raw, META_FONT, color=colour, weight=weight)
            )


def _count_pill(count: str, tint: str):
    """Pastille de comptage : même primitive / géométrie que GitTodo et LinearTodo."""
    skin = str(NSApplication.sharedApplication().effectiveAppearance().name())
    key = (count, tint, skin)
    if key not in _LABELS:
        glyph = _run(count, COUNT_FONT, color=NSColor.whiteColor(), weight=NSFontWeightSemibold)
        measured = glyph.size()
        width = max(COUNT_HEIGHT, measured.width + COUNT_PADDING)
        canvas = NSImage.alloc().initWithSize_(NSMakeSize(width, COUNT_HEIGHT))

        def paint() -> None:
            canvas.lockFocus()
            _colour(tint).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(0, 0, width, COUNT_HEIGHT), COUNT_RADIUS, COUNT_RADIUS
            ).fill()
            glyph.drawAtPoint_(((width - measured.width) / 2, (COUNT_HEIGHT - measured.height) / 2))
            canvas.unlockFocus()

        NSApplication.sharedApplication().effectiveAppearance().performAsCurrentDrawingAppearance_(paint)
        _LABELS[key] = canvas
    return _LABELS[key]


def _with_counts(face, waiting: str, running: str):
    """Compte rouge en haut à droite, compte identité en bas à gauche — format GitTodo."""
    if face is None:
        return None
    top_right = _count_pill(waiting, "systemRedColor") if waiting else None
    bottom_left = _count_pill(running, IDENTITY_TINT) if running else None
    if top_right is None and bottom_left is None:
        return face
    size = face.size().height
    inner = face.size().width
    right = top_right.size().width - COUNT_OVERLAP if top_right else 0.0
    left = bottom_left.size().width - COUNT_OVERLAP if bottom_left else 0.0
    width = left + inner + right
    canvas = NSImage.alloc().initWithSize_(NSMakeSize(width, size))
    canvas.lockFocus()
    face.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(left, 0, inner, size), NSZeroRect, NSCompositingOperationSourceOver, 1.0
    )
    for mark, corner in ((top_right, "haut-droite"), (bottom_left, "bas-gauche")):
        if mark is None:
            continue
        span = mark.size()
        x = width - span.width if corner == "haut-droite" else 0.0
        y = 0.0 if corner == "bas-gauche" else size - span.height
        mark.drawInRect_fromRect_operation_fraction_(
            NSMakeRect(x, y, span.width, span.height), NSZeroRect, NSCompositingOperationSourceOver, 1.0
        )
    canvas.unlockFocus()
    return canvas


def _account_header(view: AccountView, items=()):
    """En-tête d'un compte, Claude ou Cursor : titre, fraîcheur, e-mail, puis son activité."""
    grey = NSColor.secondaryLabelColor()
    tint = _colour(IDENTITY_TINT) if view.is_active else grey
    age_tint = _colour(freshness_tint(age_seconds(view.fetched_at))) if view.is_active else grey
    text = NSMutableAttributedString.alloc().init()
    text.appendAttributedString_(
        _run(
            account_title(view.name, view.plan),
            TITLE_FONT,
            color=tint,
            weight=NSFontWeightSemibold,
            paragraph=_paragraph(),
        )
    )
    text.appendAttributedString_(_run("  ·  ", TITLE_FONT, color=grey, weight=NSFontWeightSemibold))
    text.appendAttributedString_(
        _run(freshness_label(view.is_active, view.fetched_at), TITLE_FONT, color=age_tint, weight=NSFontWeightMedium)
    )
    if view.email:
        text.appendAttributedString_(_run(f"\n{view.email}", META_FONT, color=grey, paragraph=_paragraph(before=1.0)))
    _append_activity(text, items)
    return text


def _account_summary(view: AccountView):
    """Compte replié : une ligne de titre, une ligne de chiffres."""
    grey = NSColor.secondaryLabelColor()
    text = NSMutableAttributedString.alloc().init()
    text.appendAttributedString_(
        _run(
            account_title(view.name, view.plan),
            TITLE_FONT,
            color=None if view.is_active else grey,
            weight=NSFontWeightSemibold,
            paragraph=_paragraph(),
        )
    )
    text.appendAttributedString_(_run("  ·  ", TITLE_FONT, color=grey, weight=NSFontWeightSemibold))
    text.appendAttributedString_(
        _run(
            freshness_label(view.is_active, view.fetched_at),
            TITLE_FONT,
            color=_colour(freshness_tint(age_seconds(view.fetched_at))) if view.is_active else grey,
            weight=NSFontWeightMedium,
        )
    )
    bits = []
    if view.session:
        bits.append((f"{view.session.label} {percent(view.session.percent)}", tint_for(view.session.percent)))
    if view.weekly:
        bits.append((f"semaine {percent(view.weekly.percent)}", tint_for(view.weekly.percent)))
    if view.extra and view.extra.cap:
        bits.append((f"extra {percent(view.extra.percent)}", tint_for(view.extra.percent)))
    if bits:
        text.appendAttributedString_(_run("\n", TITLE_FONT, paragraph=_paragraph(before=2.0)))
        for index, (label, bit_tint) in enumerate(bits):
            if index:
                text.appendAttributedString_(_run("  ·  ", TITLE_FONT, color=grey, weight=NSFontWeightMedium))
            text.appendAttributedString_(_run(label, TITLE_FONT, color=_colour(bit_tint), weight=NSFontWeightMedium))
    return text


def _chrome_symbol(name: str, tint: str = ""):
    skin = str(NSApplication.sharedApplication().effectiveAppearance().name()) if tint else ""
    key = (name, tint, skin)
    cached = _CHROME.get(key)
    if cached is not None:
        return cached
    symbol = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if symbol is None:
        return None
    canvas = None

    def paint() -> None:
        nonlocal canvas
        thin = symbol.imageWithSymbolConfiguration_(
            NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
                CHROME_GLYPH, NSFontWeightLight, NSImageSymbolScaleSmall
            )
        )
        if tint:
            thin = thin.imageWithSymbolConfiguration_(
                NSImageSymbolConfiguration.configurationWithPaletteColors_([_colour(tint)])
            )
        span = thin.size()
        canvas = NSImage.alloc().initWithSize_(NSMakeSize(span.width + LEFT_MARGIN, span.height + 2 * CHROME_LIFT))
        canvas.lockFocus()
        thin.drawInRect_fromRect_operation_fraction_(
            NSMakeRect(LEFT_MARGIN, 2 * CHROME_LIFT, span.width, span.height),
            NSZeroRect,
            NSCompositingOperationSourceOver,
            1.0,
        )
        canvas.unlockFocus()

    NSApplication.sharedApplication().effectiveAppearance().performAsCurrentDrawingAppearance_(paint)
    if not tint:
        canvas.setTemplate_(True)
    _CHROME[key] = canvas
    return canvas


def _face(avatars: Avatars, email: str, fallback: str = "person.crop.circle", tint: str = IDENTITY_TINT):
    photo = avatars.image(email, AVATAR_SIZE)
    if photo is None:
        return _chrome_symbol(fallback, tint)
    # Même marge de gauche que les glyphes chrome, pour aligner les textes.
    canvas = NSImage.alloc().initWithSize_(NSMakeSize(AVATAR_SIZE + LEFT_MARGIN, AVATAR_SIZE))
    canvas.lockFocus()
    photo.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(LEFT_MARGIN, 0, AVATAR_SIZE, AVATAR_SIZE), NSZeroRect, NSCompositingOperationSourceOver, 1.0
    )
    canvas.unlockFocus()
    return canvas


def _fit_label(label: str, tint, room: float):
    size_pt = room
    while size_pt >= 5.0:
        drawn = NSAttributedString.alloc().initWithString_attributes_(
            label,
            {
                NSFontAttributeName: NSFont.monospacedDigitSystemFontOfSize_weight_(size_pt, NSFontWeightSemibold),
                NSForegroundColorAttributeName: tint,
            },
        )
        box = drawn.size()
        if box.width <= room and box.height <= room:
            return drawn, box
        size_pt -= 0.25
    return drawn, box


def _badge(session_percent: float | None, weekly_percent: float | None, waiting: int = 0, running: int = 0):
    """Chiffre = session 5 h ; anneau = semaine ; pastilles = attente (rouge) / en cours (identité)."""
    size = RING_SIZE
    canvas = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    ring_tint = tint_for(weekly_percent)
    digit_tint = tint_for(session_percent)
    label = "—" if session_percent is None else str(int(round(max(0.0, min(100.0, session_percent)))))
    fraction = 0.0 if weekly_percent is None else max(0.0, min(1.0, weekly_percent / 100.0))
    room = size - 2 * (RING_WIDTH + 3.0)

    def paint():
        canvas.lockFocus()
        ring = _colour(ring_tint)
        track = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(RING_WIDTH / 2, RING_WIDTH / 2, size - RING_WIDTH, size - RING_WIDTH)
        )
        track.setLineWidth_(RING_WIDTH)
        ring.colorWithAlphaComponent_(RING_TRACK_ALPHA).setStroke()
        track.stroke()
        if fraction > 0:
            middle = size / 2
            arc = NSBezierPath.bezierPath()
            arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                (middle, middle), (size - RING_WIDTH) / 2, 90.0, 90.0 - 360.0 * fraction, True
            )
            arc.setLineWidth_(RING_WIDTH)
            ring.setStroke()
            arc.stroke()
        drawn, box = _fit_label(label, _colour(digit_tint), room)
        drawn.drawAtPoint_(NSMakePoint((size - box.width) / 2, (size - box.height) / 2))
        canvas.unlockFocus()

    NSApplication.sharedApplication().effectiveAppearance().performAsCurrentDrawingAppearance_(paint)
    canvas.setTemplate_(False)
    return _with_counts(canvas, str(waiting) if waiting else "", str(running) if running else "")


def _spinner_image(frame: str):
    glyph = NSAttributedString.alloc().initWithString_attributes_(
        frame,
        {
            NSFontAttributeName: NSFont.monospacedDigitSystemFontOfSize_weight_(13.0, NSFontWeightMedium),
            NSForegroundColorAttributeName: _colour("IDENTITY"),
        },
    )
    canvas = NSImage.alloc().initWithSize_(NSMakeSize(RING_SIZE, RING_SIZE))

    def paint():
        canvas.lockFocus()
        box = glyph.size()
        glyph.drawAtPoint_(NSMakePoint((RING_SIZE - box.width) / 2, (RING_SIZE - box.height) / 2))
        canvas.unlockFocus()

    NSApplication.sharedApplication().effectiveAppearance().performAsCurrentDrawingAppearance_(paint)
    canvas.setTemplate_(False)
    return canvas


class IAConsoApp(NSObject):
    def init(self):
        self = objc.super(IAConsoApp, self).init()
        if self is None:
            return None
        self.cfg = Config.load()
        self.cfg_mtime = self.config_mtime()
        self.snapshot = Snapshot()
        self.activity = Activity()
        self.activity_rows = []
        self.activity_stop = threading.Event()
        self.avatars = Avatars()
        self.fetching = False
        self.fetch_started = None
        self.fetch_epoch = 0
        self.menu_open = False
        self.shown = None
        self.status_written = None
        self.footer_item = None
        self.footer_rows = []
        self.footer_active = []
        self.shortcut_row = None
        self.timer = None
        self.hover_timer = None
        self.help_window = None
        self.show_spinner = False
        self.spinner_frame = 0
        self.spinner_timer = None
        self.status_item = None
        self.menu = None
        return self

    def applicationDidFinishLaunching_(self, notification):
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.menu = NSMenu.alloc().init()
        self.menu.setDelegate_(self)
        self.status_item.setMenu_(self.menu)
        self.render()
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            TICK_SECONDS, self, "tick:", None, True
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
        NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
            self, "wake:", "NSWorkspaceDidWakeNotification", None
        )
        threading.Thread(target=self._activity_loop, name="ia-conso-activity", daemon=True).start()
        self.start_fetch(force=True)

    @objc.python_method
    def _activity_loop(self) -> None:
        """Sonde Claude/Cursor hors du thread UI (sqlite peut coûter des centaines de ms)."""
        while not self.activity_stop.is_set():
            try:
                activity = probe_activity()
            except Exception as exc:
                log_error(f"activité : {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
                activity = None
            if activity is not None:
                self.performSelectorOnMainThread_withObject_waitUntilDone_("applyActivity:", activity, False)
            self.activity_stop.wait(ACTIVITY_PROBE_SECONDS)

    def applyActivity_(self, activity) -> None:
        changed = activity != self.activity
        self.activity = activity
        if changed and not self.show_spinner:
            self.render()
            if self.menu_open:
                self.build_menu(self.menu)

    def tick_(self, timer):
        if self.fetching and self.fetch_started and (now() - self.fetch_started).total_seconds() > STUCK_AFTER:
            log_error("cycle bloqué au-delà du délai : drapeau relâché de force")
            self.fetch_epoch += 1
            self.fetching = False
            # Sans redater la tentative, le compte à rebours reste à zéro et un cycle repart aussitôt.
            self.snapshot = replace(self.snapshot, attempted_at=now(), error="cycle interrompu")
            self.stop_spinner()
        if self.menu_open:
            if self.contents() != self.shown:
                self.build_menu(self.menu)
            else:
                self.update_footer()
                self.refresh_live()
        if self.countdown() <= 0:
            self.start_fetch()

    def menuNeedsUpdate_(self, menu):
        try:
            self.build_menu(menu)
        except Exception as exc:
            log_error(f"menu : {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            menu.removeAllItems()
            self.add_info(menu, f"menu cassé : {type(exc).__name__}: {exc}")

    def menuWillOpen_(self, menu):
        self.menu_open = True
        if self.hover_timer is None:
            self.hover_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                shortcuts.POLL_SECONDS, self, "hover:", None, True
            )
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.hover_timer, NSRunLoopCommonModes)

    def hover_(self, timer):
        row = self.shortcut_row
        if row is None:
            return
        zone = row.hovered()
        row.set_hover(zone)
        if zone is None:
            return
        row.setToolTip_(row.tip_of(zone))
        (NSCursor.pointingHandCursor() if row.enabled_at(zone) else NSCursor.arrowCursor()).set()

    def menuDidClose_(self, menu):
        self.menu_open = False
        if self.hover_timer is not None:
            self.hover_timer.invalidate()
            self.hover_timer = None
        if self.shortcut_row is not None:
            self.shortcut_row.set_hover(None)
        NSCursor.arrowCursor().set()

    def wake_(self, notification):
        self.start_fetch(force=True)

    @objc.python_method
    def config_mtime(self) -> float:
        try:
            return CONFIG_PATH.stat().st_mtime
        except OSError:
            return 0.0

    @objc.python_method
    def reload_config(self) -> None:
        mtime = self.config_mtime()
        if mtime and mtime != self.cfg_mtime:
            self.cfg = Config.load()
            self.cfg_mtime = mtime

    @objc.python_method
    def interval(self) -> int:
        """Rythme du prochain cycle : le sien, ou celui qu'Anthropic impose après un 429."""
        base = max(15, self.cfg.refresh_seconds)
        if self.snapshot.retry_after:
            return max(base, self.snapshot.retry_after)
        if self.snapshot.error:
            # Un échec ne doit pas relancer un cycle à chaque tick : c'est ce qui entretient un 429.
            return max(base, RETRY_FLOOR)
        return base

    @objc.python_method
    def countdown(self) -> int:
        """Temps avant la prochaine tentative, comptée depuis la dernière — réussie ou non."""
        if self.snapshot.attempted_at is None:
            return 0
        elapsed = (now() - self.snapshot.attempted_at).total_seconds()
        return max(0, int(self.interval() - elapsed))

    @objc.python_method
    def is_frozen(self) -> bool:
        if self.snapshot.fetched_at is None:
            return False
        return (now() - self.snapshot.fetched_at).total_seconds() > FROZEN_AFTER

    @objc.python_method
    def start_fetch(self, force: bool = False) -> None:
        self.reload_config()
        if self.fetching:
            return
        if not force and self.countdown() > 0:
            return
        self.fetching = True
        self.fetch_started = now()
        self.fetch_epoch += 1
        # Le spinner remplace le badge : il ne le fait que pour une attente demandée, ou la première.
        if force or self.snapshot.fetched_at is None:
            self.start_spinner()
        threading.Thread(target=self._fetch_worker, args=(self.fetch_epoch,), daemon=True).start()

    @objc.python_method
    def _fetch_worker(self, epoch: int) -> None:
        try:
            snapshot = fetch_claude()
            if epoch != self.fetch_epoch:
                # Un cycle plus récent a pris la main : inutile d'aller plus loin.
                return
            cursor = fetch_cursor()
            snapshot = Snapshot(
                account=snapshot.account,
                views=snapshot.views + (cursor,),
                fetched_at=snapshot.fetched_at,
                attempted_at=snapshot.attempted_at,
                error=snapshot.error,
                token_origin=snapshot.token_origin,
                retry_after=snapshot.retry_after,
            )
            self.land(epoch, self.apply_snapshot, snapshot)
            emails = {view.email for view in snapshot.views if view.email}
            if snapshot.account and snapshot.account.email:
                emails.add(snapshot.account.email)
            try:
                self.avatars.prefetch(emails)
            except Exception as exc:
                # Les chiffres sont déjà affichés : une panne de photo ne doit pas coûter le cycle.
                log_error(f"photos : {type(exc).__name__}: {exc}")
            else:
                # Une photo arrivée après coup ne se voit qu'au rendu suivant : on le redemande.
                self.land(epoch, self.apply_snapshot, snapshot)
        except Exception as exc:
            log_error(f"cycle interrompu : {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            self.land(epoch, self.apply_snapshot, Snapshot(error=f"{type(exc).__name__}: {exc}", attempted_at=now()))
        finally:
            self.land(epoch, self.release_fetch)

    @objc.python_method
    def land(self, epoch: int, apply, *args) -> None:
        """Repasse sur le thread UI, sauf si un cycle plus récent a déjà pris la main."""

        def deliver() -> None:
            if epoch == self.fetch_epoch:
                apply(*args)

        self.performSelectorOnMainThread_withObject_waitUntilDone_("deliver:", (deliver,), False)

    def deliver_(self, payload):
        payload[0]()

    @objc.python_method
    def apply_snapshot(self, snapshot: Snapshot) -> None:
        if snapshot.error and not snapshot.of(CLAUDE) and self.snapshot.of(CLAUDE):
            # Claude n'a rien rendu : on garde ses chiffres précédents et la vue Cursor du cycle,
            # et on date la tentative pour que le prochain cycle respecte son palier.
            snapshot = Snapshot(
                account=snapshot.account or self.snapshot.account,
                views=self.snapshot.of(CLAUDE) + snapshot.of(CURSOR),
                fetched_at=self.snapshot.fetched_at,
                attempted_at=snapshot.attempted_at or now(),
                error=snapshot.error,
                token_origin=snapshot.token_origin or self.snapshot.token_origin,
                retry_after=snapshot.retry_after,
            )
        self.snapshot = snapshot
        self.render()
        if self.menu_open:
            self.build_menu(self.menu)

    @objc.python_method
    def release_fetch(self) -> None:
        self.fetching = False
        self.fetch_started = None
        self.stop_spinner()

    @objc.python_method
    def start_spinner(self) -> None:
        if self.show_spinner:
            return
        self.show_spinner = True
        self.spinner_frame = 0
        self.draw_spinner()
        self.spinner_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            SPINNER_INTERVAL, self, "spin:", None, True
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.spinner_timer, NSRunLoopCommonModes)

    @objc.python_method
    def stop_spinner(self) -> None:
        self.show_spinner = False
        if self.spinner_timer is not None:
            self.spinner_timer.invalidate()
            self.spinner_timer = None
        self.render()

    def spin_(self, timer):
        if not self.show_spinner:
            self.stop_spinner()
            return
        self.spinner_frame = (self.spinner_frame + 1) % len(SPINNER_FRAMES)
        self.draw_spinner()

    @objc.python_method
    def draw_spinner(self) -> None:
        button = self.status_item.button()
        button.setImage_(_spinner_image(SPINNER_FRAMES[self.spinner_frame]))
        button.setTitle_("")
        button.setToolTip_("IA-Conso — chargement…")

    @objc.python_method
    def render(self) -> None:
        if self.status_item is None or self.show_spinner:
            return
        active = self.snapshot.active
        session = active.session if active else None
        weekly = active.weekly if active else None
        running, waiting = self.activity.running_count, self.activity.waiting_count
        button = self.status_item.button()
        button.setImage_(
            _badge(session.percent if session else None, weekly.percent if weekly else None, waiting, running)
        )
        button.setTitle_("")
        button.setToolTip_(self.tooltip(session, weekly, running, waiting))
        self.mirror_status()

    @objc.python_method
    def tooltip(self, session, weekly, running: int, waiting: int) -> str:
        tips = []
        if session:
            tips.append(f"session {percent(session.percent)}")
        if weekly:
            tips.append(f"semaine {percent(weekly.percent)}")
        if waiting:
            tips.append(f"{waiting} en attente")
        if running:
            tips.append(f"{running} en cours")
        if agents := self.activity.agent_count:
            tips.append(f"{agents} agent" + ("s" if agents > 1 else ""))
        if self.snapshot.error and not tips:
            return f"IA-Conso — {self.snapshot.error}"
        return "IA-Conso — " + " · ".join(tips) if tips else "IA-Conso"

    @objc.python_method
    def mirror_status(self) -> None:
        """Miroir sur disque, réécrit seulement quand il change (le rendu, lui, passe 2 fois/s)."""
        status = {
            "comptes": [
                {
                    "cle": view.key,
                    "fournisseur": view.provider,
                    "nom": account_title(view.name, view.plan),
                    "actif": view.is_active,
                    "session": view.session.percent if view.session else None,
                    "semaine": view.weekly.percent if view.weekly else None,
                    "maj": view.fetched_at.isoformat() if view.fetched_at else None,
                    "erreur": view.error,
                }
                for view in self.snapshot.views
            ],
            "activite": {
                "en_cours": self.activity.running_count,
                "en_attente": self.activity.waiting_count,
                "sessions": [
                    {
                        "titre": item.title,
                        "modele": item.model,
                        "effort": item.effort,
                        "agents": item.agents,
                        "en_attente": item.is_waiting,
                    }
                    for item in self.activity.items
                ],
            },
            "erreur": self.snapshot.error,
            "maj": self.snapshot.fetched_at.isoformat() if self.snapshot.fetched_at else None,
        }
        if status != self.status_written:
            self.status_written = status
            write_status(status)

    @objc.python_method
    def contents(self) -> tuple:
        return (self.snapshot.views, self.snapshot.error, self.activity)

    @objc.python_method
    def build_menu(self, menu) -> None:
        menu.removeAllItems()
        menu.setShowsStateColumn_(False)
        snap = self.snapshot
        self.shown = self.contents()
        self.footer_rows, self.footer_active = [], []
        self.activity_rows = []
        self.shortcut_row = None

        if snap.error and not snap.of(CLAUDE):
            self.add_info(menu, snap.error)
        elif not snap.views:
            self.add_info(menu, "première lecture en cours…")

        claude = snap.of(CLAUDE)
        active = next((view for view in claude if view.is_active), None)
        others = [view for view in claude if not view.is_active]
        if active:
            self.add_account(menu, active, self.activity.claude, live=True)
        for view in snap.of(CURSOR):
            self.add_break(menu)
            self.add_account(menu, view, self.activity.cursor, live=True)
        # Un compte inactif en détail, le reste replié : le menu reste lisible à 5 comptes.
        if others:
            self.add_break(menu)
            self.add_account(menu, others[0])
        if others[1:]:
            self.add_break(menu)
            for view in others[1:]:
                self.add_rich(menu, _account_summary(view), image=_face(self.avatars, view.email))

        self.add_break(menu)
        self.add_footer(menu)
        self.add_shortcuts(menu)

    @objc.python_method
    def add_break(self, menu) -> None:
        """Un séparateur ne sépare que s'il y a quelque chose au-dessus."""
        last = menu.itemAtIndex_(menu.numberOfItems() - 1) if menu.numberOfItems() else None
        if last is not None and not last.isSeparatorItem():
            menu.addItem_(NSMenuItem.separatorItem())

    @objc.python_method
    def add_account(self, menu, view: AccountView, items=(), live: bool = False) -> None:
        """Un compte : son en-tête, ses jauges, ses extra.

        `live` dit que ce compte-là porte l'activité en cours : sans lui, l'en-tête se rafraîchit
        quand même (la fraîcheur vieillit à chaque tick) mais sans se voir greffer les sessions
        d'un autre compte.
        """
        row = self.add_rich(menu, _account_header(view, items), image=_face(self.avatars, view.email))
        self.activity_rows.append((row, view, live))
        if view.error:
            self.add_info(menu, view.error)
        for window in (view.session, view.weekly, *view.scoped):
            if window is not None:
                self.add_meter(menu, "window", window, _window_title(window))
        if view.extra:
            self.add_meter(menu, "extra", view.extra, _extra_title(view.extra))
        for refill in view.refills:
            self.add_meter(menu, "refill", refill, _refill_title(refill))
        for name, share in view.breakdown:
            if share > 0:
                self.add_info(menu, f"{name}  ·  {percent(share)} de la conso")
        if not view.has_figures and not view.error:
            self.add_info(
                menu,
                "conso réarmée depuis la dernière lecture : passe sur ce compte une fois"
                if view.fetched_at
                else "passe sur ce compte une fois pour capturer la conso",
            )

    @objc.python_method
    def add_meter(self, menu, kind: str, payload, title) -> None:
        row = NSMenuItem.alloc().init()
        row.setAttributedTitle_(title)
        row.setEnabled_(False)
        row.setRepresentedObject_((kind, payload))
        menu.addItem_(row)

    @objc.python_method
    def add_rich(self, menu, title, image=None) -> NSMenuItem:
        row = NSMenuItem.alloc().init()
        row.setAttributedTitle_(title)
        if image is not None:
            row.setImage_(image)
        row.setEnabled_(False)
        menu.addItem_(row)
        return row

    @objc.python_method
    def add_info(self, menu, text: str) -> None:
        row = NSMenuItem.alloc().init()
        row.setAttributedTitle_(_run(text, META_FONT, color=NSColor.secondaryLabelColor()))
        row.setEnabled_(False)
        menu.addItem_(row)

    @objc.python_method
    def refresh_live(self) -> None:
        # « depuis … » recalculé chaque tick sans reconstruire le menu.
        for item, view, live in self.activity_rows:
            items = ()
            if live:
                items = self.activity.cursor if view.provider == CURSOR else self.activity.claude
            item.setAttributedTitle_(_account_header(view, items))
        for item in self.menu.itemArray():
            payload = item.representedObject()
            if not isinstance(payload, tuple) or len(payload) != 2:
                continue
            kind, value = payload
            if kind == "window":
                item.setAttributedTitle_(_window_title(value))
            elif kind == "extra":
                item.setAttributedTitle_(_extra_title(value))
            elif kind == "refill":
                item.setAttributedTitle_(_refill_title(value))

    @objc.python_method
    def footer_text(self) -> str:
        if self.fetching:
            state = "actualisation…"
        elif self.snapshot.attempted_at is None:
            state = "en attente"
        else:
            left = self.countdown()
            state = f"prochaine dans {countdown(left)}" if left > 0 else "actualisation imminente"
        frozen = "⚠︎ données figées" if self.is_frozen() else ""
        return " · ".join(p for p in (state, frozen) if p)

    @objc.python_method
    def refresh_title(self):
        title = NSMutableAttributedString.alloc().init()
        title.appendAttributedString_(_run("Actualiser", TITLE_FONT))
        title.appendAttributedString_(_run("   " + self.footer_text(), META_FONT, color=NSColor.secondaryLabelColor()))
        return title

    @objc.python_method
    def update_footer(self) -> None:
        if self.footer_item is not None:
            self.footer_item.setAttributedTitle_(self.refresh_title())

    @objc.python_method
    def add_footer(self, menu) -> None:
        self.footer_item = self.add_action(menu, "", "refresh:", "r", "arrow.clockwise")
        self.footer_item.setAttributedTitle_(self.refresh_title())
        self.footer_item.setToolTip_("Actualiser maintenant (⌘R)")
        self.footer_rows.append((self.footer_item, "arrow.clockwise"))

        if self.bundle_program():
            started = launchagent.is_enabled()
            row = self.add_action(
                menu, "Lancer au démarrage" + ("  ✓" if started else ""), "toggleLogin:", symbol="power"
            )
            row.setToolTip_(
                "Lancer au démarrage : activé, cliquer pour désactiver"
                if started
                else "Lancer au démarrage : désactivé, cliquer pour activer"
            )
            self.footer_rows.append((row, "power"))
            if started:
                self.footer_active.append(row)

        row = self.add_action(menu, "Comment ça marche", "openHelp:", ",", "questionmark.circle")
        row.setToolTip_("Comment ça marche (⌘,)")
        self.footer_rows.append((row, "questionmark.circle"))

        row = self.add_action(menu, "Quitter IA-Conso", "quitApp:", "q", "xmark.circle")
        row.setToolTip_("Quitter IA-Conso (⌘Q)")
        self.footer_rows.append((row, "xmark.circle"))

    @objc.python_method
    def add_shortcuts(self, menu) -> None:
        entries = []
        for item, symbol in self.footer_rows:
            action = item.action()
            label = str(item.attributedTitle().string()) if item.attributedTitle() else str(item.title())
            # Sur la barre rapide, on garde le premier mot : « Actualiser », « Lancer », etc.
            short = label.split("   ")[0].split("  ✓")[0]
            if short.startswith("Lancer"):
                short = "Démarrage"
            elif short.startswith("Comment"):
                short = "Aide"
            elif short.startswith("Quitter"):
                short = "Quitter"
            entries.append(
                (
                    symbol,
                    short,
                    str(action) if action and item.isEnabled() else None,
                    getattr(NSColor, IDENTITY_TINT)() if item in self.footer_active else None,
                    item in self.footer_active,
                    str(item.toolTip() or "") or short,
                )
            )
        if not entries:
            return
        width = max(menu.size().width, SHORTCUT_MIN * len(entries))
        row = shortcuts.Shortcuts.alloc().initWithFrame_(NSMakeRect(0, 0, width, shortcuts.ROW_HEIGHT))
        row.load(entries)
        row.on_pick = self.run_shortcut
        holder = NSMenuItem.alloc().init()
        holder.setView_(row)
        menu.insertItem_atIndex_(holder, 0)
        menu.insertItem_atIndex_(NSMenuItem.separatorItem(), 1)
        self.shortcut_row = row

    @objc.python_method
    def run_shortcut(self, action: str) -> None:
        menu = self.status_item.menu() if self.status_item is not None else None
        if menu is not None:
            menu.cancelTracking()
        self.performSelector_withObject_afterDelay_("runDeferred:", action, 0.0)

    def runDeferred_(self, action):
        handler = getattr(self, str(action).replace(":", "_"), None)
        if handler is not None:
            handler(self)

    @objc.python_method
    def add_action(self, menu, label: str, selector: str, key: str = "", symbol: str = ""):
        entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, selector, key)
        entry.setTarget_(self)
        if symbol:
            entry.setImage_(_chrome_symbol(symbol))
        menu.addItem_(entry)
        return entry

    @objc.python_method
    def bundle_program(self) -> str:
        """L'exécutable du bundle, pas son dossier : c'est ce que launchd sait lancer."""
        bundle = NSBundle.mainBundle()
        if bundle.bundleIdentifier() != BUNDLE_ID:
            return ""
        return str(bundle.executablePath() or "")

    def refresh_(self, sender):
        self.start_fetch(force=True)

    def toggleLogin_(self, sender):
        program = self.bundle_program()
        if not program:
            return
        if launchagent.is_enabled():
            launchagent.disable()
        else:
            launchagent.enable(program)
        if self.menu_open:
            self.build_menu(self.menu)

    def openHelp_(self, sender):
        account = self.snapshot.account
        identity = " · ".join(p for p in (account.name, account.email, account.plan) if p) if account else ""
        self.help_window = manual.panel({"identity": identity, "token": self.snapshot.token_origin})
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.help_window.window.makeKeyAndOrderFront_(None)

    def quitApp_(self, sender):
        self.activity_stop.set()
        NSWorkspace.sharedWorkspace().notificationCenter().removeObserver_(self)
        for timer in ("timer", "hover_timer", "spinner_timer"):
            handle = getattr(self, timer, None)
            if handle is not None:
                handle.invalidate()
                setattr(self, timer, None)
        NSApplication.sharedApplication().terminate_(self)


def run() -> None:
    if not acquire_single_instance():
        print("IA-Conso tourne déjà (une seule icône à la fois).", file=sys.stderr)
        return
    application = NSApplication.sharedApplication()
    delegate = IAConsoApp.alloc().init()
    application.setDelegate_(delegate)
    AppHelper.runEventLoop(installInterrupt=True)
