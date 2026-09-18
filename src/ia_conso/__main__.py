"""Point d'entrée : app de barre des menus, ou dump texte pour vérifier.

    python -m ia_conso            lance l'app
    python -m ia_conso --print    affiche ce que le menu contiendrait
"""

from __future__ import annotations

import sys

from .anthropic import fetch
from .config import CONFIG_PATH
from .formatting import dump as format_dump


def main() -> None:
    if "--print" in sys.argv:
        snapshot = fetch()
        print(format_dump(snapshot))
        print(f"config {CONFIG_PATH}")
        raise SystemExit(1 if snapshot.error and snapshot.session is None else 0)
    from .app import run

    run()


if __name__ == "__main__":
    main()
