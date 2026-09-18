"""Panneau « Comment ça marche ? » : le mode d'emploi, dans l'app."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import objc
from Cocoa import (
    NSAttributedString,
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSMakeRect,
    NSMakeSize,
    NSMutableAttributedString,
    NSMutableParagraphStyle,
    NSObject,
    NSParagraphStyleAttributeName,
    NSScrollView,
    NSTextView,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)

from . import IDENTITY_TINT, VERSION
from .anthropic import KEYCHAIN_SERVICE
from .config import CONFIG_PATH, STATE_PATH

DOC_WIDTH = 620.0
DOC_HEIGHT = 640.0

TEXT = """
# IA-Conso

Où en est l'abonnement Claude, et quand il se réarme. **Lecture seule** : l'app ne consomme \
aucun jeton, elle ne fait que demander le pourcentage déjà calculé par Anthropic.

## Cette installation

- Compte : {identity}
- Token fourni par : {token}
- Réglages : `{config}`
- Journal : `{errors}`
- Version {version}, code du {built}

## D'où vient le chiffre

Aucun token à coller. L'app relit celui que Claude Code (et Cursor) a déjà posé dans le \
trousseau macOS, service `{service}`. C'est le même compte que tes sessions Cursor.

Elle interroge ensuite `api.anthropic.com/api/oauth/usage`, le même endpoint que la commande \
`/usage` de Claude Code. La réponse porte des **pourcentages**, pas un nombre de messages : \
Anthropic ne publie pas le quota absolu.

- **Session 5 h** : fenêtre glissante depuis le premier message. C'est le chiffre de la barre.
- **Semaine** : plafond glissant sur 7 jours, tous modèles confondus.
- **Sonnet / Opus** : plafonds hebdomadaires d'un modèle, seulement s'ils existent sur le plan.
- **Extra** : crédits payants une fois le plan saturé, s'ils sont activés.
- **Cette semaine** : répartition de la conso hebdomadaire (Claude Code, chats, Cowork…).

L'app **ne rafraîchit pas** le token. Un rafraîchissement ferait tourner le jeton de Claude \
Code et casserait Cursor. Si la session a expiré, ouvre Cursor une fois : il la renouvelle, \
IA-Conso relit le trousseau au cycle suivant.

## Plusieurs organisations

Le token ne voit qu'**une** org à la fois (Pro perso, Team Spacefill, etc.). L'app liste \
toutes tes orgs « chat », montre la conso en live pour l'active, et mémorise la dernière \
conso lue pour les autres. Pour mettre à jour une org inactive : bascule dessus dans Cursor, \
attends un cycle (ou Actualiser).

## La barre

Un pourcentage, celui de la session 5 h, dans un cercle qui se remplit d'autant. Bleu jusqu'à 20 %, puis vert, jaune, orange, et rouge à partir de 80 %. Un clic ouvre le \
détail : compte, \
session, semaine, extra, et le temps restant avant chaque reset.

## Réglages

Le cycle se règle dans `{config}`, clé `refresh_seconds` (60 s par défaut). L'app réécrit \
le fichier s'il manque une clé, pour qu'il reste exhaustif.

## Lancer au démarrage

Le menu écrit `~/Library/LaunchAgents/fr.jsebire.ia-conso.plist`. Ce n'est pas une case \
Système : la décocher ici retire le fichier.
"""


def built_at() -> str:
    stamps = [source.stat().st_mtime for source in Path(__file__).parent.glob("*.py")]
    return datetime.fromtimestamp(max(stamps)).strftime("%d/%m/%Y à %H:%M") if stamps else "inconnue"


def document(context: dict) -> str:
    return TEXT.format(
        identity=context.get("identity") or "inconnu",
        token=context.get("token") or "aucun token trouvé",
        config=CONFIG_PATH,
        errors=STATE_PATH.with_name("errors.log"),
        service=KEYCHAIN_SERVICE,
        version=VERSION,
        built=built_at(),
    )


BULLET = "•   "
_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")


def _style(before: float = 0.0, indent: float = 0.0) -> NSMutableParagraphStyle:
    style = NSMutableParagraphStyle.alloc().init()
    style.setParagraphSpacingBefore_(before)
    style.setParagraphSpacing_(2.0)
    style.setLineSpacing_(2.0)
    if indent:
        style.setHeadIndent_(indent)
    return style


def _identity():
    return getattr(NSColor, IDENTITY_TINT)()


def _inline(line: str, size: float, weight: float, colour, style) -> NSMutableAttributedString:
    out = NSMutableAttributedString.alloc().init()
    for piece in _INLINE.split(line):
        if not piece:
            continue
        font = NSFont.systemFontOfSize_weight_(size, weight)
        shade = colour
        if piece.startswith("**") and piece.endswith("**"):
            piece, font = piece[2:-2], NSFont.systemFontOfSize_weight_(size, NSFontWeightSemibold)
        elif piece.startswith("`") and piece.endswith("`"):
            piece = piece[1:-1]
            font = NSFont.monospacedSystemFontOfSize_weight_(size - 1.0, NSFontWeightRegular)
            shade = NSColor.secondaryLabelColor()
        out.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(
                piece,
                {
                    NSFontAttributeName: font,
                    NSForegroundColorAttributeName: shade,
                    NSParagraphStyleAttributeName: style,
                },
            )
        )
    return out


def render(document_text: str) -> NSMutableAttributedString:
    body = NSMutableAttributedString.alloc().init()
    for raw in document_text.strip("\n").split("\n"):
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("# "):
            piece = _inline(line[2:], 22.0, NSFontWeightSemibold, _identity(), _style(2.0))
        elif line.startswith("## "):
            piece = _inline(line[3:], 15.0, NSFontWeightSemibold, _identity(), _style(22.0))
        elif line.startswith("- "):
            piece = _inline(BULLET + line[2:], 12.0, NSFontWeightRegular, NSColor.labelColor(), _style(3.0, 20.0))
        else:
            piece = _inline(line, 12.0, NSFontWeightRegular, NSColor.labelColor(), _style(8.0))
        body.appendAttributedString_(piece)
        body.appendAttributedString_(NSAttributedString.alloc().initWithString_("\n"))
    return body


class Panel(NSObject):
    def initWithContext_(self, context):
        self = objc.super(Panel, self).init()
        if self is None:
            return None
        self.window = self._window(context)
        return self

    @objc.python_method
    def refresh(self, context: dict) -> None:
        self.doc.textStorage().setAttributedString_(render(document(context)))

    @objc.python_method
    def _window(self, context: dict):
        frame = NSMakeRect(0, 0, DOC_WIDTH, DOC_HEIGHT)
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered,
            False,
        )
        window.setTitle_("IA-Conso, mode d'emploi")
        window.setReleasedWhenClosed_(False)
        window.setMinSize_(NSMakeSize(420, 320))
        window.setDelegate_(self)

        doc = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, DOC_WIDTH, DOC_HEIGHT))
        doc.setEditable_(False)
        doc.setSelectable_(True)
        doc.setDrawsBackground_(False)
        doc.setTextContainerInset_(NSMakeSize(22.0, 22.0))
        doc.setHorizontallyResizable_(False)
        doc.setAutoresizingMask_(NSViewWidthSizable)
        doc.textContainer().setWidthTracksTextView_(True)
        doc.textStorage().setAttributedString_(render(document(context)))
        self.doc = doc
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, DOC_WIDTH, DOC_HEIGHT))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        scroll.setDocumentView_(doc)
        window.setContentView_(scroll)
        window.center()
        return window

    def windowWillClose_(self, notification):
        _ALIVE.pop("panel", None)


_ALIVE: dict = {}


def panel(context: dict):
    live = _ALIVE.get("panel")
    if live is None:
        live = Panel.alloc().initWithContext_(context)
        _ALIVE["panel"] = live
    else:
        live.refresh(context)
    return live
