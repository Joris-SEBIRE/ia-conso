"""Tous les chemins que l'app lit ou écrit, au même endroit.

Trois modules lisaient les mêmes fichiers locaux chacun de leur côté ; un chemin qui bouge ne
doit se corriger qu'une fois, et `make uninstall` doit pouvoir retrouver ici tout ce qui est écrit.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()

CLAUDE_JSON = HOME / ".claude.json"
CLAUDE_SESSIONS = HOME / ".claude" / "sessions"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CURSOR_STATE_DB = HOME / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"

CONFIG_PATH = Path(os.environ.get("IA_CONSO_CONFIG") or HOME / ".config" / "ia-conso" / "config.json")
STATE_PATH = Path(os.environ.get("IA_CONSO_STATE") or HOME / "Library" / "Application Support" / "IAConso" / "state.json")
STATE_DIR = STATE_PATH.parent
LOCK_PATH = STATE_DIR / "ia-conso.lock"
STATUS_PATH = STATE_DIR / "status.json"
# Seules les organisations Claude sont mémorisées : le token n'en voit qu'une à la fois.
ORGS_PATH = STATE_DIR / "orgs.json"
ERRORS_PATH = STATE_DIR / "errors.log"

CACHE_DIR = HOME / "Library" / "Caches" / "IAConso"
AVATARS_DIR = CACHE_DIR / "avatars"
