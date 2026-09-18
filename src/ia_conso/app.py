"""Élément de barre des menus : pourcentage de session, anneau, menu détaillé."""

from __future__ import annotations

import sys
import threading
import traceback

import objc
from Cocoa import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBezierPath,
    NSBundle,
    NSColor,
    NSCompositingOperationSourceOver,
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

from . import IDENTITY_TINT, launchagent
from . import help as manual
from .anthropic import fetch
from .config import CONFIG_PATH, Config
from .formatting import ago, countdown, money, percent, reset_line, tint_for
from .models import Snapshot, Window, now
from .state import acquire_single_instance, log_error, write_status

BUNDLE_ID = "fr.jsebire.ia-conso"
TICK_SECONDS = 1.0
STUCK_AFTER = 30.0
FROZEN_AFTER = 180.0
RING_SIZE = 22.0
RING_WIDTH = 1.75
RING_TRACK_ALPHA = 0.28
TITLE_FONT = 13.0
META_FONT = 11.0
HEADER_FONT = 10.0
HERO_FONT = 22.0
CHROME_GLYPH = 11.0
CHROME_LIFT = 1.75
LEFT_MARGIN = 6.0
SPINNER_FRAMES = ("◐", "◓", "◑", "◒")
SPINNER_INTERVAL = 0.13
BAR_WIDTH = 12


def _colour(name: str):
    if name == "IDENTITY":
        name = IDENTITY_TINT
    return getattr(NSColor, name)()


def _paragraph(before: float = 0.0, after: float = 1.0):
    style = NSMutableParagraphStyle.alloc().init()
    style.setParagraphSpacingBefore_(before)
    style.setParagraphSpacing_(after)
    style.setLineSpacing_(1.0)
    return style


def _run(text: str, size: float, color=None, weight=None, paragraph=None):
    attributes = {
        NSFontAttributeName: (
            NSFont.systemFontOfSize_(size) if weight is None else NSFont.systemFontOfSize_weight_(size, weight)
        )
    }
    if color is not None:
        attributes[NSForegroundColorAttributeName] = color
    if paragraph is not None:
        attributes[NSParagraphStyleAttributeName] = paragraph
    return NSAttributedString.alloc().initWithString_attributes_(text, attributes)


def _bar(value: float, width: int = BAR_WIDTH) -> str:
    """Barre textuelle remplie au pourcentage, pour lire la conso d'un coup d'œil."""
    filled = max(0, min(width, int(round(value / 100.0 * width))))
    return "█" * filled + "░" * (width - filled)


def _window_title(window: Window):
    """Bloc d'une fenêtre de quota : libellé, pourcentage coloré, barre, reset."""
    tint = _colour(tint_for(window.percent))
    grey = NSColor.secondaryLabelColor()
    head = _paragraph()
    body = _paragraph(before=2.0)
    foot = _paragraph(before=1.0)
    text = NSMutableAttributedString.alloc().init()
    text.appendAttributedString_(
        _run(window.label.upper(), HEADER_FONT, color=tint, weight=NSFontWeightSemibold, paragraph=head)
    )
    text.appendAttributedString_(
        _run(
            f"\n{percent(window.percent)}",
            HERO_FONT,
            color=tint,
            weight=NSFontWeightSemibold,
            paragraph=body,
        )
    )
    text.appendAttributedString_(
        _run(f"\n{_bar(window.percent)}", META_FONT, color=tint.colorWithAlphaComponent_(0.85), paragraph=foot)
    )
    text.appendAttributedString_(
        _run(f"\n{reset_line(window)}", META_FONT, color=grey, weight=NSFontWeightMedium, paragraph=foot)
    )
    return text


def _account_title(name: str, meta: str):
    text = NSMutableAttributedString.alloc().init()
    text.appendAttributedString_(_run(name, TITLE_FONT, weight=NSFontWeightSemibold, paragraph=_paragraph()))
    if meta:
        text.appendAttributedString_(
            _run(f"\n{meta}", META_FONT, color=NSColor.secondaryLabelColor(), paragraph=_paragraph(before=1.0))
        )
    return text


def _section_title(label: str, detail: str, tint: str = IDENTITY_TINT):
    text = NSMutableAttributedString.alloc().init()
    text.appendAttributedString_(
        _run(label.upper(), HEADER_FONT, color=_colour(tint), weight=NSFontWeightSemibold, paragraph=_paragraph())
    )
    if detail:
        text.appendAttributedString_(
            _run(f"\n{detail}", TITLE_FONT, weight=NSFontWeightMedium, paragraph=_paragraph(before=2.0))
        )
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


_CHROME: dict = {}


def _fit_label(label: str, tint, room: float):
    """Police la plus grande dont le chiffre tient dans le diamètre intérieur du cercle."""
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


def _badge(value: float | None) -> object:
    """Pourcentage de la session 5 h, dans un cercle qui se remplit d'autant."""
    size = RING_SIZE
    canvas = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    tint_name = tint_for(value)
    label = "—" if value is None else str(int(round(max(0.0, min(100.0, value)))))
    fraction = 0.0 if value is None else max(0.0, min(1.0, value / 100.0))
    # Diamètre utile : intérieur du trait, moins 3 pt de marge pour ne pas frôler l'anneau.
    room = size - 2 * (RING_WIDTH + 3.0)

    def paint():
        canvas.lockFocus()
        tint = _colour(tint_name)
        track = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(RING_WIDTH / 2, RING_WIDTH / 2, size - RING_WIDTH, size - RING_WIDTH)
        )
        track.setLineWidth_(RING_WIDTH)
        tint.colorWithAlphaComponent_(RING_TRACK_ALPHA).setStroke()
        track.stroke()
        if fraction > 0:
            middle = size / 2
            arc = NSBezierPath.bezierPath()
            arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                (middle, middle), (size - RING_WIDTH) / 2, 90.0, 90.0 - 360.0 * fraction, True
            )
            arc.setLineWidth_(RING_WIDTH)
            tint.setStroke()
            arc.stroke()
        drawn, box = _fit_label(label, tint, room)
        drawn.drawAtPoint_(NSMakePoint((size - box.width) / 2, (size - box.height) / 2))
        canvas.unlockFocus()

    NSApplication.sharedApplication().effectiveAppearance().performAsCurrentDrawingAppearance_(paint)
    canvas.setTemplate_(False)
    return canvas


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
        self.fetching = False
        self.fetch_started = None
        self.fetch_epoch = 0
        self.fetch_local = threading.local()
        self.menu_open = False
        self.shown = None
        self.footer_item = None
        self.updated_item = None
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
        self.start_fetch()

    def tick_(self, timer):
        if self.fetching and self.fetch_started and (now() - self.fetch_started).total_seconds() > STUCK_AFTER:
            log_error("cycle bloqué au-delà du délai : drapeau relâché de force")
            self.fetch_epoch += 1
            self.fetching = False
        if self.menu_open:
            if self.contents() != self.shown:
                self.build_menu(self.menu)
            else:
                self.update_footer()
                self.refresh_live()
        if self.countdown() <= 0:
            self.start_fetch()

    def menuWillOpen_(self, menu):
        self.menu_open = True
        self.build_menu(menu)

    def menuDidClose_(self, menu):
        self.menu_open = False

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
        if self.snapshot.retry_after:
            return max(self.cfg.refresh_seconds, self.snapshot.retry_after)
        return max(15, self.cfg.refresh_seconds)

    @objc.python_method
    def countdown(self) -> int:
        if self.snapshot.fetched_at is None:
            return 0 if not self.fetching else self.interval()
        elapsed = (now() - self.snapshot.fetched_at).total_seconds()
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
        self.start_spinner()
        threading.Thread(target=self._fetch_worker, args=(self.fetch_epoch,), daemon=True).start()

    @objc.python_method
    def _fetch_worker(self, epoch: int) -> None:
        self.fetch_local.epoch = epoch
        try:
            snapshot = fetch()
            self.land(self.apply_snapshot, snapshot)
        except Exception as exc:
            log_error(f"cycle interrompu : {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            self.land(self.apply_snapshot, Snapshot(error=f"{type(exc).__name__}: {exc}"))
        finally:
            self.land(self.release_fetch)

    @objc.python_method
    def land(self, apply, *args) -> None:
        epoch = getattr(self.fetch_local, "epoch", 0)

        def deliver() -> None:
            if epoch == self.fetch_epoch:
                apply(*args)

        self.performSelectorOnMainThread_withObject_waitUntilDone_("deliver:", (deliver,), False)

    def deliver_(self, payload):
        payload[0]()

    @objc.python_method
    def apply_snapshot(self, snapshot: Snapshot) -> None:
        # Un cycle en erreur ne doit pas effacer le dernier pourcentage encore lisible.
        if snapshot.error and self.snapshot.session is not None and snapshot.session is None:
            snapshot = Snapshot(
                account=snapshot.account or self.snapshot.account,
                session=self.snapshot.session,
                weekly=self.snapshot.weekly,
                scoped=self.snapshot.scoped,
                extra=self.snapshot.extra,
                breakdown=self.snapshot.breakdown,
                fetched_at=self.snapshot.fetched_at,
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
        session = self.snapshot.session
        value = session.percent if session else None
        button = self.status_item.button()
        button.setImage_(_badge(value))
        button.setTitle_("")
        if session:
            tip = f"IA-Conso — session 5 h {percent(session.percent)}"
            if session.resets_at:
                tip += f", {reset_line(session)}"
        elif self.snapshot.error:
            tip = f"IA-Conso — {self.snapshot.error}"
        else:
            tip = "IA-Conso"
        button.setToolTip_(tip)
        write_status(
            {
                "session": session.percent if session else None,
                "weekly": self.snapshot.weekly.percent if self.snapshot.weekly else None,
                "account": self.snapshot.account.email if self.snapshot.account else "",
                "error": self.snapshot.error,
                "maj": self.snapshot.fetched_at.isoformat() if self.snapshot.fetched_at else None,
            }
        )

    @objc.python_method
    def contents(self) -> tuple:
        snap = self.snapshot
        return (
            snap.account,
            snap.session,
            snap.weekly,
            snap.scoped,
            snap.extra,
            snap.breakdown,
            snap.error,
            launchagent.is_enabled(),
        )

    @objc.python_method
    def build_menu(self, menu) -> None:
        menu.removeAllItems()
        snap = self.snapshot
        self.shown = self.contents()
        self.updated_item = None

        if snap.account:
            who = snap.account
            meta = " · ".join(p for p in (who.email, who.plan) if p)
            self.add_rich(menu, _account_title(who.name or who.email, meta), "person.crop.circle", IDENTITY_TINT)
        elif snap.error:
            self.add_title(menu, "Compte introuvable", "person.crop.circle.badge.exclamationmark", "systemYellowColor")

        if snap.error:
            self.add_info(menu, snap.error, "exclamationmark.triangle")

        if snap.session or snap.weekly or snap.scoped:
            menu.addItem_(NSMenuItem.separatorItem())

        if snap.session:
            self.add_window(menu, snap.session, "timer")
        if snap.weekly:
            if snap.session:
                menu.addItem_(NSMenuItem.separatorItem())
            self.add_window(menu, snap.weekly, "calendar")
        for window in snap.scoped:
            menu.addItem_(NSMenuItem.separatorItem())
            self.add_window(menu, window, "square.stack")

        if snap.extra and snap.extra.is_enabled:
            menu.addItem_(NSMenuItem.separatorItem())
            extra = snap.extra
            used = f"{money(extra.used, extra.currency)} / {money(extra.cap, extra.currency)}"
            if extra.percent:
                used += f"  ·  {percent(extra.percent)}"
            tint = tint_for(extra.percent) if extra.percent else IDENTITY_TINT
            self.add_rich(menu, _section_title("Extra", used, tint), "creditcard", tint)
            self.add_info(menu, "crédits une fois le plan saturé")

        if snap.breakdown:
            rows = [(name, share) for name, share in snap.breakdown if share > 0]
            if rows:
                menu.addItem_(NSMenuItem.separatorItem())
                self.add_title(menu, "Répartition de la semaine", "chart.bar", IDENTITY_TINT)
                for name, share in rows:
                    self.add_info(menu, f"{name}  ·  {percent(share)} de la conso")

        menu.addItem_(NSMenuItem.separatorItem())
        self.add_footer(menu)

    @objc.python_method
    def add_window(self, menu, window, symbol: str) -> None:
        tint = tint_for(window.percent)
        row = NSMenuItem.alloc().init()
        row.setAttributedTitle_(_window_title(window))
        row.setImage_(_chrome_symbol(symbol, tint))
        row.setEnabled_(False)
        row.setRepresentedObject_(window)
        menu.addItem_(row)

    @objc.python_method
    def add_rich(self, menu, title, symbol: str = "", tint: str = "") -> NSMenuItem:
        row = NSMenuItem.alloc().init()
        row.setAttributedTitle_(title)
        if symbol:
            row.setImage_(_chrome_symbol(symbol, tint))
        row.setEnabled_(False)
        menu.addItem_(row)
        return row

    @objc.python_method
    def add_title(self, menu, text: str, symbol: str, tint: str) -> None:
        label = "RÉPARTITION DE LA SEMAINE" if text.startswith("Répartition") else text
        size = HEADER_FONT if text.startswith("Répartition") else TITLE_FONT
        color = _colour(tint) if text.startswith("Répartition") else None
        row = NSMenuItem.alloc().init()
        row.setAttributedTitle_(_run(label, size, color=color, weight=NSFontWeightSemibold))
        row.setImage_(_chrome_symbol(symbol, tint))
        row.setEnabled_(False)
        menu.addItem_(row)

    @objc.python_method
    def add_info(self, menu, text: str, symbol: str = "") -> None:
        row = NSMenuItem.alloc().init()
        row.setAttributedTitle_(_run(text, META_FONT, color=NSColor.secondaryLabelColor()))
        if symbol:
            row.setImage_(_chrome_symbol(symbol))
        row.setEnabled_(False)
        menu.addItem_(row)

    @objc.python_method
    def refresh_live(self) -> None:
        """Le décompte du reset et l'âge de la lecture avancent menu ouvert."""
        for item in self.menu.itemArray():
            window = item.representedObject()
            if window is None:
                continue
            item.setAttributedTitle_(_window_title(window))
        if self.updated_item is not None:
            self.updated_item.setAttributedTitle_(self.updated_title())

    @objc.python_method
    def updated_line(self) -> str:
        if self.fetching:
            return "mise à jour…"
        if self.snapshot.fetched_at is None:
            return "pas encore lu"
        when = self.snapshot.fetched_at.astimezone().strftime("%H:%M:%S")
        return f"lu {ago(self.snapshot.fetched_at)} · {when}"

    @objc.python_method
    def updated_title(self):
        return _run(self.updated_line(), META_FONT, color=NSColor.secondaryLabelColor())

    @objc.python_method
    def footer_text(self) -> str:
        if self.fetching:
            state = "actualisation…"
        elif self.snapshot.fetched_at is None:
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
        self.updated_item = NSMenuItem.alloc().init()
        self.updated_item.setAttributedTitle_(self.updated_title())
        self.updated_item.setImage_(_chrome_symbol("clock"))
        self.updated_item.setEnabled_(False)
        menu.addItem_(self.updated_item)

        self.footer_item = self.add_action(menu, "", "refresh:", "r", "arrow.clockwise")
        self.footer_item.setAttributedTitle_(self.refresh_title())
        self.footer_item.setToolTip_("Actualiser maintenant (⌘R)")
        menu.addItem_(NSMenuItem.separatorItem())
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
        self.add_action(menu, "Comment ça marche", "openHelp:", ",", "questionmark.circle")
        row = self.add_action(menu, "Quitter IA-Conso", "quitApp:", "q", "xmark.circle")
        row.setToolTip_("Quitter IA-Conso (⌘Q)")

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
        bundle = NSBundle.mainBundle()
        path = bundle.bundlePath()
        if path.endswith(".app"):
            return path
        return ""

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
        identity = ""
        if account:
            identity = " · ".join(p for p in (account.name, account.email, account.plan) if p)
        self.help_window = manual.panel({"identity": identity, "token": self.snapshot.token_origin})
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.help_window.window.makeKeyAndOrderFront_(None)

    def quitApp_(self, sender):
        NSApplication.sharedApplication().terminate_(self)


def run() -> None:
    if not acquire_single_instance():
        print("IA-Conso tourne déjà (une seule icône à la fois).", file=sys.stderr)
        return
    application = NSApplication.sharedApplication()
    delegate = IAConsoApp.alloc().init()
    application.setDelegate_(delegate)
    AppHelper.runEventLoop(installInterrupt=True)
