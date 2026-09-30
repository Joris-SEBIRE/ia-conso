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
from .paths import CLAUDE_SESSIONS, CONFIG_PATH, CURSOR_STATE_DB, ERRORS_PATH, STATE_DIR

DOC_WIDTH = 620.0
DOC_HEIGHT = 640.0

TEXT = """
# IA-Conso

Où en est la conso des abonnements IA, et quand elle se réarme. **Lecture seule** : l'app ne \
consomme aucun jeton, elle ne fait que lire des chiffres déjà calculés et des fichiers déjà \
posés sur le disque.

## Cette installation

- Compte Claude : {identity}
- Token fourni par : {token}
- Réglages : `{config}`
- État et journal : `{state}`
- Version {version}, code du {built}

## La barre

Le chiffre est le pourcentage de la **session 5 h** de Claude ; l'anneau autour est la **semaine**. \
Bleu jusqu'à 20 %, puis vert, jaune, orange, et rouge à partir de 80 %.

- Pastille en bas à gauche : les sessions qui **travaillent**, une par ligne du menu.
- Pastille rouge en haut à droite : les sessions qui **attendent une réponse** de ta part.

Les agents en vol appartiennent à une session : ils comptent sur sa ligne, pas dans la pastille, \
pour que le chiffre de la barre se retrouve toujours dans le détail. L'infobulle en donne le total.

## D'où viennent les chiffres

Aucun token à coller. Pour Claude, l'app relit celui que Claude Code (et Cursor) a déjà posé dans \
le trousseau macOS, service `{service}`, et interroge `api.anthropic.com/api/oauth/usage` — le même \
endpoint que la commande `/usage`. La réponse porte des **pourcentages**, pas un nombre de messages.

- **Session 5 h** : fenêtre glissante depuis le premier message. C'est le chiffre de la barre.
- **Semaine** : plafond glissant sur 7 jours, tous modèles confondus.
- **Semaine <modèle>** : plafond hebdomadaire d'un modèle, affiché seulement s'il est entamé.
- **En retrait** : tout ce qui n'est pas à ta disposition passe à mi-opacité — un chiffre pas \
lu à l'instant, ou une fonction que tu ne peux pas utiliser. Le texte gris dit laquelle. Une \
session ou une semaine pleine reste en rouge vif : c'est une alerte.
- **Extra** : crédits hors forfait. « Désactivé » y veut rarement dire éteint : l'app distingue \
le plafond de l'organisation atteint (jauge pleine, avec ta propre dépense), le solde prépayé \
épuisé (jauge du plafond mensuel, qui n'est pas ce qui bloque) et la vraie coupure par un admin. \
L'échéance est estimée au 1er du mois, comme Claude Code la calcule lui-même.

Pour Cursor, le token est lu dans sa base d'état (`{cursor}`) et la conso vient de \
`GetCurrentPeriodUsage`. Le pourcentage affiché est celui que Cursor calcule lui-même : la \
fraction « dépensé / forfait » reste collée à 100 % dès que le forfait est saturé et ne veut plus \
rien dire.

L'app **ne rafraîchit pas** le token Claude. Un rafraîchissement ferait tourner le jeton de Claude \
Code et casserait Cursor. Si la session a expiré, ouvre Cursor une fois : il la renouvelle, \
IA-Conso relit le trousseau au cycle suivant.

## Plusieurs organisations

Le token ne voit qu'**une** org à la fois (Pro perso, Team, etc.). L'app liste toutes tes orgs \
« chat », montre la conso en live pour l'active, et mémorise la dernière conso lue pour les autres. \
Ces chiffres mémorisés gardent leur jauge, en retrait : ils disent où tu en étais, pas où tu en es. Une fenêtre dont l'échéance est passée s'affiche vide, « réarmée » à sa \
date : elle est repartie de zéro, et ce qui a été consommé depuis est inconnu. Pour mettre à jour \
une org inactive : bascule dessus dans Cursor, attends un cycle.

## Ce qui travaille en ce moment

Sous chaque compte, l'app liste les sessions en cours, lues sur le disque, sans rien demander au \
réseau :

- Claude Code : `{sessions}` donne les sessions vivantes et leur état ; le transcript de chaque \
session donne son titre, son **modèle**, son **effort**, le **contexte** occupé et le nombre \
d'**agents** encore en vol.
- Chaque ligne se lit dans le même ordre : titre, modèle (une couleur par famille), effort sous le libellé \
même du sélecteur (`Extra high`, `Max`… ou `Ultracode` si le sixième cran est armé), puis en gris les agents, le contexte et la durée. En rouge, une session \
qui attend ta réponse.
- Cursor : la base d'état donne les agents en cours, leur modèle, leur contexte, et ceux qui \
attendent une approbation.

Une session en attente est comptée comme telle, pas comme active : elle ne consomme rien. Une session qui \
t'a rendu la main pendant qu'une commande de fond ou des agents travaillent reste affichée, \
avec le nombre de tâches de fond en cours.

## Réglages

Le cycle se règle dans `{config}`, clé `refresh_seconds` (60 s par défaut). L'app réécrit \
le fichier s'il manque une clé, pour qu'il reste exhaustif. Après un échec, elle attend au moins \
30 s — et le délai demandé par Anthropic en cas de 429 — avant de retenter.

## Lancer au démarrage

Le menu écrit `~/Library/LaunchAgents/fr.jsebire.ia-conso.plist`. Ce n'est pas une case \
Système : la décocher ici retire le fichier.

## Ce que l'app écrit

Rien hors de ta machine, à une exception près : les photos de profil viennent de Gravatar, qui \
reçoit donc une empreinte de l'adresse e-mail. Tout le reste est local : `{state}` (miroir de la \
barre, conso mémorisée par org, journal des pannes) et le cache d'avatars.
"""


def built_at() -> str:
    stamps = [source.stat().st_mtime for source in Path(__file__).parent.glob("*.py")]
    return datetime.fromtimestamp(max(stamps)).strftime("%d/%m/%Y à %H:%M") if stamps else "inconnue"


def document(context: dict) -> str:
    return TEXT.format(
        identity=context.get("identity") or "inconnu",
        token=context.get("token") or "aucun token trouvé",
        config=CONFIG_PATH,
        state=STATE_DIR,
        errors=ERRORS_PATH,
        sessions=CLAUDE_SESSIONS,
        cursor=CURSOR_STATE_DB,
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
