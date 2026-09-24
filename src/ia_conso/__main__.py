"""Point d'entrée : app de barre des menus, ou dump texte pour vérifier.

    python -m ia_conso            lance l'app
    python -m ia_conso --print    affiche ce que le menu contiendrait
"""

from __future__ import annotations

import sys

from .activity import probe
from .anthropic import fetch as fetch_claude
from .cursor import fetch as fetch_cursor
from .formatting import dump
from .models import Snapshot
from .paths import CONFIG_PATH, ERRORS_PATH, STATE_DIR


def snapshot() -> Snapshot:
    """Le même assemblage que l'app : Claude d'abord, Cursor ensuite."""
    claude = fetch_claude()
    return Snapshot(
        account=claude.account,
        views=claude.views + (fetch_cursor(),),
        fetched_at=claude.fetched_at,
        attempted_at=claude.attempted_at,
        error=claude.error,
        token_origin=claude.token_origin,
        retry_after=claude.retry_after,
    )


def main() -> None:
    if "--print" in sys.argv:
        snap = snapshot()
        print(dump(snap, probe()))
        print()
        print(f"config  {CONFIG_PATH}")
        print(f"état    {STATE_DIR}")
        print(f"journal {ERRORS_PATH}")
        raise SystemExit(1 if snap.error and snap.active is None else 0)
    from .app import run

    run()


if __name__ == "__main__":
    main()
