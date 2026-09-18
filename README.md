# IA-Conso

La consommation de l'abonnement Claude, dans la barre des menus macOS.

Dans la barre : le pourcentage de la **session 5 h**, dans un cercle qui se remplit d'autant. Un
clic ouvre le détail : le compte connecté, la session, la semaine, le temps avant chaque reset,
et les extra s'il y en a.

L'app ne fait que lire. Elle ne consomme aucun jeton.

```
  ( 55 )
  ┌─────────────────────────────────────────────┐
  │ ☺  Joris SEBIRE                             │
  │     joris.sebire@spacefill.fr · Claude Pro  │
  │ ◷  session 5 h    55 %                      │
  │     reset dans 4 h 31 min · 18:20           │
  │ ◔  semaine         7 %                      │
  │     reset dans 4 j 10 h · mar. 00:00        │
  │ 💳 Extra                                    │
  │     0,00 € / 20,00 €                        │
  │ ─────────────────────────────────────────── │
  │ ↻  Actualiser   prochaine dans 45 s         │
  │ ⚡  Lancer au démarrage                      │
  │ ?  Comment ça marche                        │
  └─────────────────────────────────────────────┘
```

## Prérequis

- macOS 13 ou plus récent.
- Les outils de développement en ligne de commande : `xcode-select --install`.
- Python 3.12 ou 3.13 installé par Homebrew (`brew install python@3.13`). PyObjC a besoin d'une
  installation *framework*.
- Un abonnement Claude (Pro, Max, Team…) déjà connecté dans Cursor ou Claude Code. L'app relit
  le token du trousseau, service `Claude Code-credentials`. Aucun token à coller.
- Un compte administrateur, parce que l'installation écrit dans `/Applications`.

Le `Makefile` cible `/opt/homebrew/bin/python3.13` par défaut. Sur une machine Intel, ou avec un
autre interpréteur : `make install PYTHON=/usr/local/bin/python3.13`.

## Installation

```bash
make install
```

La cible construit `build/IAConso.app`, le recopie dans `/Applications` et le lance. Le bundle
embarque ses dépendances Python. Comme GitTodo, l'exécutable principal **est** l'interpréteur, et
l'app démarre par `sitecustomize.py` : un `exec` ferait disparaître l'icône sur macOS 26.

Au premier lancement, macOS peut demander l'accès au trousseau pour lire `Claude Code-credentials`.
C'est le token déjà posé par Cursor : à autoriser.

Pour que l'app démarre avec la session : menu **Lancer au démarrage**.

Autres cibles :

```bash
make print      # ce que le menu afficherait, en texte
make run        # depuis les sources, sans installer
make restart    # relance /Applications/IAConso.app
make uninstall  # retire l'app, le LaunchAgent et l'état local
```

## D'où vient le chiffre

`GET https://api.anthropic.com/api/oauth/usage`, le même endpoint que `/usage` dans Claude Code.
Anthropic y répond en **pourcentages** (session 5 h, semaine, extra), pas en nombre de messages.

L'app ne rafraîchit pas le token. Un rafraîchissement ferait tourner le jeton de Claude Code et
casserait Cursor. Si la session a expiré, ouvrir Cursor une fois suffit : il la renouvelle, et
IA-Conso relit le trousseau au cycle suivant.

## Réglages

`~/.config/ia-conso/config.json`, clé `refresh_seconds` (60 s par défaut). L'app réécrit le
fichier s'il manque une clé.

## Couleurs de la barre

Bleu 0–20 %, vert 20–40 %, jaune 40–60 %, orange 60–80 %, rouge 80–100 %.
