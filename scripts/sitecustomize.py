"""Amorce du bundle : ce fichier démarre l'app sans jamais remplacer le processus.

L'exécutable principal du bundle est l'interpréteur lui-même. Sur macOS 26, un exécutable lancé
par Launch Services qui `exec` un autre binaire perd la place de son élément de barre des menus :
mesuré, l'élément se retrouve hors écran, à (0, 0) et de hauteur nulle, alors qu'un élément bien
placé fait 33 points de haut. Le défaut ne vient ni du script shell ni de notre code — un lanceur
compilé qui `execv` échoue pareil, et le même code lancé sans `exec` se place correctement.

Python importe `sitecustomize` au démarrage, ce qui laisse démarrer l'app dans le processus même
que Launch Services a lancé. Sans argument, on démarre ; avec des arguments, on ne fait rien, pour
que l'interpréteur du bundle reste utilisable tel quel pour déboguer.
"""

import os
import runpy
import sys

sys.dont_write_bytecode = True

# Ce fichier vit dans Contents/lib/pythonX.Y/site-packages/, d'où quatre niveaux à remonter.
CONTENTS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
RESOURCES = os.path.join(CONTENTS, "Resources")


def _package() -> str:
    """Le paquet à lancer, reconnu à son `__main__.py` : les apps sœurs partagent ce fichier."""
    if not os.path.isdir(RESOURCES):
        return ""
    for name in sorted(os.listdir(RESOURCES)):
        if os.path.exists(os.path.join(RESOURCES, name, "__main__.py")):
            return name
    return ""


if len(sys.argv) <= 1 and (package := _package()):
    sys.path.insert(0, RESOURCES)
    runpy.run_module(package, run_name="__main__", alter_sys=True)
    raise SystemExit(0)
