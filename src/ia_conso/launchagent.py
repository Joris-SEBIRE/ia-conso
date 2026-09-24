"""Lancement au démarrage via un LaunchAgent utilisateur."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

LABEL = "fr.jsebire.ia-conso"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>{program}</string></array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
"""


def _program() -> str:
    """Le programme inscrit dans le plist installé, s'il y en a un."""
    try:
        blob = plistlib.loads(PLIST.read_bytes())
    except (OSError, ValueError):
        return ""
    arguments = blob.get("ProgramArguments") if isinstance(blob, dict) else None
    return str(arguments[0]) if isinstance(arguments, list) and arguments else ""


def _runnable(program: str) -> bool:
    return bool(program) and Path(program).is_file() and os.access(program, os.X_OK)


def is_enabled() -> bool:
    """Vrai seulement si l'agent installé peut encore lancer quelque chose.

    Un exécutable renommé laisse derrière lui un plist qui ne lance plus rien, et une case qui
    prétend le contraire. On lit donc le programme inscrit plutôt que la seule présence du fichier.
    """
    return _runnable(_program())


def enable(program: str) -> None:
    """`program` est l'exécutable du bundle : launchd ne sait pas lancer un dossier `.app`."""
    if not _runnable(program):
        return
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    # Un agent déjà chargé sur un ancien chemin doit sortir avant qu'on réécrive le plist.
    subprocess.run(["/bin/launchctl", "unload", str(PLIST)], capture_output=True)
    PLIST.write_text(TEMPLATE.format(label=LABEL, program=program))
    subprocess.run(["/bin/launchctl", "load", "-w", str(PLIST)], capture_output=True)


def disable() -> None:
    subprocess.run(["/bin/launchctl", "unload", "-w", str(PLIST)], capture_output=True)
    PLIST.unlink(missing_ok=True)
