"""Barre d'accès rapide en tête de menu : les actions du pied, en une ligne d'icônes.

Une vue à zones plutôt que des boutons AppKit : dans un menu déroulé, les zones de suivi ne
délivrent `mouseEntered:` qu'une fois sur deux — c'est mesuré dans SpacefillLocalhost, qui pose
le même genre de ligne. Le survol se lit donc par sondage de la position de la souris, et la
vue dessine elle-même sa pastille de survol, son éclair de clic et ses libellés.
"""

from __future__ import annotations

import objc
from Cocoa import (
    NSAppearanceNameDarkAqua,
    NSColorSpace,
    NSFontWeightLight,
    NSImage,
    NSImageSymbolConfiguration,
    NSImageSymbolScaleSmall,
    NSAppearanceNameVibrantDark,
    NSAttributedString,
    NSBezierPath,
    NSColor,
    NSCompositingOperationSourceOver,
    NSFont,
    NSFontAttributeName,
    NSFontWeightMedium,
    NSForegroundColorAttributeName,
    NSLineBreakByTruncatingTail,
    NSMakeRect,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSPointInRect,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSTimer,
    NSView,
    NSZeroRect,
)

ROW_HEIGHT = 30.0
# Le glyphe reprend les cotes de ceux du menu : onze points, trait fin, petite échelle. Le
# dessiner nous-mêmes plutôt que réutiliser l'image de la ligne évite d'hériter de sa marge de
# gauche et de sa remontée, qui n'ont de sens qu'à l'intérieur d'une ligne de menu.
GLYPH_SIZE = 11.0
INSET = 6.0
GLYPH = 15.0
# Écart entre l'icône et son libellé, et respiration à droite avant la zone suivante. Serrés :
# la ligne se partage en parts égales, et chaque point gagné est un caractère de plus.
GAP = 5.0
TAIL = 6.0
LABEL_SIZE = 11.0
# Fond de la zone survolée, puis l'éclair du clic : les mêmes valeurs que dans la ligne à zones
# de SpacefillLocalhost, où elles ont été réglées à l'œil sur les deux thèmes.
HOVER_DARK = 0.14
HOVER_LIGHT = 0.07
FLASH_ALPHA = 0.42
FLASH_SECONDS = 0.18
# Cadence du sondage de la souris, tant que le menu est ouvert.
POLL_SECONDS = 0.05


_GLYPHS: dict = {}


def _glyph(name: str, tint):
    """Le symbole du menu, peint dans une couleur : un gabarit resterait noir en thème sombre.

    La couleur résolue entre dans la clé du cache : sans elle, un passage clair/sombre garderait
    l'ancienne teinte jusqu'au redémarrage.
    """
    resolved = tint.colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
    # Une couleur de catalogue non résolue n'a pas de composantes : les lui demander lèverait
    # ici, dans la boucle du menu, et gèlerait l'app sans un mot. Son nom fait alors la clé.
    key = (
        (name, str(tint))
        if resolved is None
        else (
            name,
            round(resolved.redComponent(), 3),
            round(resolved.greenComponent(), 3),
            round(resolved.blueComponent(), 3),
            round(resolved.alphaComponent(), 3),
        )
    )
    if key not in _GLYPHS:
        symbol = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if symbol is None:
            return None
        thin = symbol.imageWithSymbolConfiguration_(
            NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
                GLYPH_SIZE, NSFontWeightLight, NSImageSymbolScaleSmall
            )
        )
        _GLYPHS[key] = thin.imageWithSymbolConfiguration_(
            NSImageSymbolConfiguration.configurationWithHierarchicalColor_(tint)
        )
    return _GLYPHS[key]


def _style():
    style = NSMutableParagraphStyle.alloc().init()
    style.setLineBreakMode_(NSLineBreakByTruncatingTail)
    return style


class Shortcuts(NSView):
    """Une ligne de raccourcis : icône, libellé coupé, une zone cliquable par action."""

    def initWithFrame_(self, frame):
        self = objc.super(Shortcuts, self).initWithFrame_(frame)
        if self is None:
            return None
        self.entries = []
        self.zone = None
        self.flashing = None
        self.on_pick = None
        return self

    @objc.python_method
    def load(self, entries) -> None:
        """Les actions à montrer : (glyphe, libellé, action, teinte, active, infobulle)."""
        self.entries = list(entries)
        self.setNeedsDisplay_(True)

    @objc.python_method
    def slot(self, index: int):
        """Rectangle d'une zone : la ligne se partage en parts égales, bords compris."""
        span = (self.bounds().size.width - 2 * INSET) / max(1, len(self.entries))
        return NSMakeRect(INSET + index * span, 0.0, span, self.bounds().size.height)

    @objc.python_method
    def hovered(self):
        """La zone sous la souris, lue par sondage. `None` si la souris est ailleurs."""
        window = self.window()
        if window is None:
            return None
        point = self.convertPoint_fromView_(window.mouseLocationOutsideOfEventStream(), None)
        if not NSPointInRect(point, self.bounds()):
            return None
        for index in range(len(self.entries)):
            if NSPointInRect(point, self.slot(index)):
                return index
        return None

    @objc.python_method
    def set_hover(self, zone) -> None:
        if zone == self.zone:
            return
        self.zone = zone
        self.setNeedsDisplay_(True)

    @objc.python_method
    def tip_of(self, zone) -> str:
        if zone is None or not (0 <= zone < len(self.entries)):
            return ""
        return self.entries[zone][5]

    @objc.python_method
    def enabled_at(self, zone) -> bool:
        return bool(0 <= zone < len(self.entries)) and self.entries[zone][2] is not None

    def mouseUp_(self, event):
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        for index in range(len(self.entries)):
            if NSPointInRect(point, self.slot(index)) and self.enabled_at(index):
                self.flash(index)
                if self.on_pick is not None:
                    self.on_pick(self.entries[index][2])
                return

    @objc.python_method
    def flash(self, index: int) -> None:
        """Éclaire la zone cliquée le temps d'un battement : sans quoi le clic ne se voit pas."""
        self.flashing = index
        self.setNeedsDisplay_(True)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            FLASH_SECONDS, self, "unflash:", None, False
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)

    def unflash_(self, timer):
        self.flashing = None
        self.setNeedsDisplay_(True)

    @objc.python_method
    def _dark(self) -> bool:
        name = self.effectiveAppearance().bestMatchFromAppearancesWithNames_(
            ["NSAppearanceNameAqua", NSAppearanceNameDarkAqua, NSAppearanceNameVibrantDark]
        )
        return str(name) in (str(NSAppearanceNameDarkAqua), str(NSAppearanceNameVibrantDark))

    def drawRect_(self, rect):
        pale = 1.0 if self._dark() else 0.0
        for index, (glyph, label, action, tint, active, _) in enumerate(self.entries):
            box = self.slot(index)
            if index in (self.zone, self.flashing) and action is not None:
                alpha = FLASH_ALPHA if index == self.flashing else (HOVER_DARK if self._dark() else HOVER_LIGHT)
                cushion = NSMakeRect(box.origin.x + 1.0, box.origin.y + 3.0, box.size.width - 2.0,
                                     box.size.height - 6.0)
                NSColor.colorWithWhite_alpha_(pale, alpha).setFill()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(cushion, 7.0, 7.0).fill()
            colour = (
                (tint or NSColor.labelColor())
                if active
                else (NSColor.labelColor() if action is not None else NSColor.tertiaryLabelColor())
            )
            picture = _glyph(glyph, colour) if glyph else None
            if picture is not None:
                span = picture.size()
                # Centré sur la hauteur de la part, et sur la même ligne que le texte : l'image
                # du menu, elle, est volontairement remontée, ce qui se voyait ici.
                picture.drawInRect_fromRect_operation_fraction_(
                    NSMakeRect(
                        box.origin.x + GAP + (GLYPH - span.width) / 2,
                        box.origin.y + (box.size.height - span.height) / 2,
                        span.width,
                        span.height,
                    ),
                    NSZeroRect,
                    NSCompositingOperationSourceOver,
                    1.0,
                )
            font = NSFont.systemFontOfSize_weight_(LABEL_SIZE, NSFontWeightMedium)
            text = NSAttributedString.alloc().initWithString_attributes_(
                label,
                {
                    NSFontAttributeName: font,
                    NSForegroundColorAttributeName: colour,
                    NSParagraphStyleAttributeName: _style(),
                },
            )
            left = box.origin.x + GAP + GLYPH + GAP
            # Le texte se centre sur la hauteur de ses capitales, pas sur celle de sa ligne :
            # sinon il tombe un point plus bas que le glyphe, et le décalage se voit.
            cap = font.capHeight()
            height = font.ascender() - font.descender()
            top = box.origin.y + (box.size.height - cap) / 2 - (font.ascender() - cap)
            text.drawInRect_(
                NSMakeRect(left, top, max(0.0, box.origin.x + box.size.width - TAIL - left), height + 1.0)
            )
