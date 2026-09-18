"""Lancement au démarrage via un LaunchAgent utilisateur."""

from __future__ import annotations

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


def is_enabled() -> bool:
    return PLIST.exists()


def enable(program: str) -> None:
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(TEMPLATE.format(label=LABEL, program=program))
    subprocess.run(["/bin/launchctl", "load", "-w", str(PLIST)], capture_output=True)


def disable() -> None:
    subprocess.run(["/bin/launchctl", "unload", "-w", str(PLIST)], capture_output=True)
    PLIST.unlink(missing_ok=True)
