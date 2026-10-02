# IA-Conso

La consommation des abonnements IA, dans la barre des menus macOS. Claude et Cursor, plus ce qui
travaille en ce moment sur la machine.

Dans la barre : le pourcentage de la **session 5 h** de Claude, dans un anneau qui se remplit de la
**semaine**. Deux pastilles comptent les sessions — celles qui travaillent à gauche, celles qui
attendent une réponse à droite, une par ligne du menu. Les agents en vol s'affichent sur la ligne
de leur session, et le total est dans l'infobulle. Un clic ouvre le détail.

L'app ne fait que lire. Elle ne consomme aucun jeton.

```
  ( 57 )²
  ┌──────────────────────────────────────────────────────────────────┐
  │ ☺  Spacefill · Claude Team        ·  actif à l'instant            │
  │     joris.sebire@spacefill.fr                                    │
  │     → revue ia-conso · Opus 5 1M · Extra high · Ultracode        │
  │       · Thinking · 3 agents · contexte 325 k · depuis 17 min     │
  │     ↻ SPA-8285 · session 5 h pleine · il y a 12 min · reset 14:10│
  │     ✓ revue PR · Sonnet 5 · Medium · terminée il y a 24 min      │
  │ ◷  SESSION 5 H  ·  reset dans 3 h 34 min · 19:30                 │
  │     57 %  ███████░░░░░                                           │
  │ ◔  SEMAINE  ·  reset dans 3 j 12 h · lun. à 04:00                │
  │     64 %  ████████░░░░                                           │
  │ 💳 EXTRA  ·  réarmement estimé · jeu. à 00:00                     │
  │     100 %  ████████████  ·  plafond de l'organisation atteint     │
  │            · 0,00 € dépensés par toi                             │
  │ ───────────────────────────────────────────────────────────────  │
  │ ☺  Cursor Pro  ·  actif à l'instant                              │
  │ ◷  PÉRIODE  ·  reset dans 3 sem 3 j · dim. 18 oct. à 16:51       │
  │      5 %  █░░░░░░░░░░░  ·  $25,08 / $472,50                      │
  │ ───────────────────────────────────────────────────────────────  │
  │ ↻  Actualiser   prochaine dans 45 s                              │
  │ ⚡  Lancer au démarrage                                           │
  │ 🔔  Sons                                                          │
  │ ?  Comment ça marche                                             │
  └──────────────────────────────────────────────────────────────────┘
```

## Prérequis

- macOS 13 ou plus récent.
- Les outils de développement en ligne de commande : `xcode-select --install`.
- Python 3.12 ou 3.13 installé par Homebrew (`brew install python@3.13`). PyObjC a besoin d'une
  installation *framework*.
- Un abonnement Claude (Pro, Max, Team…) déjà connecté dans Cursor ou Claude Code. L'app relit
  le token du trousseau, service `Claude Code-credentials`. Aucun token à coller.
- Pour le bloc Cursor : Cursor installé et connecté. Son token est lu dans sa base d'état locale.
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
make print      # ce que le menu afficherait, en texte : comptes, conso, sessions
make run        # depuis les sources, sans installer
make restart    # relance /Applications/IAConso.app
make uninstall  # retire l'app, le LaunchAgent et l'état local
```

## D'où viennent les chiffres

**Claude** : `GET https://api.anthropic.com/api/oauth/usage`, le même endpoint que `/usage` dans
Claude Code. Anthropic y répond en **pourcentages** (session 5 h, semaine, plafonds par modèle),
pas en nombre de messages. Pour les crédits extra, l'app distingue un plafond atteint (celui de
l'organisation ou le tien), un solde prépayé épuisé et une vraie coupure par un admin — trois cas
que l'API regroupe sous « désactivé ». L'échéance est estimée au 1er du mois, comme Claude Code la
calcule lui-même.

L'app ne rafraîchit pas le token. Un rafraîchissement ferait tourner le jeton de Claude Code et
casserait Cursor. Si la session a expiré, ouvrir Cursor une fois suffit : il la renouvelle, et
IA-Conso relit le trousseau au cycle suivant.

**Cursor** : `GetCurrentPeriodUsage` du DashboardService, avec le token de sa base d'état. Le
pourcentage affiché est celui que Cursor calcule lui-même ; la fraction « dépensé / forfait » reste
collée à 100 % dès que le forfait est saturé et ne dit plus rien de la conso réelle.

**Ce qui travaille** : uniquement des fichiers locaux, aucune requête. `~/.claude/sessions/` donne
les sessions Claude Code vivantes ; leur transcript donne le titre, le modèle, l'effort, le
contexte occupé et les agents encore en vol ; la base d'état de Cursor donne ses agents, leur
modèle et ceux qui attendent une approbation.

Chaque ligne se lit dans cet ordre : le titre, puis le **modèle** — couleur par famille, violet
pour Opus, turquoise pour Sonnet, vert pour Haiku, indigo pour Fable, suivi de `1M` pour le contexte
d'un million de tokens — puis l'**effort**, écrit comme Claude Code l'écrit — `Low`, `Medium`,
`High`, `Extra high`, `Max` — puis les **options** actives, plus pâles : `Ultracode`, `Thinking`,
`Fast`, `Advisor` suivi de son modèle quand il en consulte un autre que celui de la session, `Plan`.
Enfin, en gris, ce qui décrit l'avancement : agents en vol, contexte occupé, durée. Une session qui
attend une réponse passe en rouge.

Sous les sessions en cours, en retrait, celles qui ne travaillent plus, une ligne par session et
jamais une session en cours : **↻** pour une session coupée par une limite ou une erreur d'API —
la limite en rouge, puis l'heure de son reset, ou « à relancer » une fois le reset passé — et
**✓** pour une session terminée dans les cinq dernières heures, avec depuis quand — « interrompue »
si c'est toi qui l'as coupée. Chacune garde le modèle, l'effort et les options de son dernier tour,
lus dans son propre transcript, en retrait. Une session coupée reste listée jusqu'à sa relance, et
au plus un jour après le reset. Aucune n'entre dans les pastilles.

**Sons** : une clochette (`Glass`) quand une session finit son tour — sa réponse est prête — et un
autre son (`Submarine`) quand une session attend ton intervention (permission, question, plan à
valider) ou qu'une limite vient de la couper. Une session qui a lancé des agents, ou une commande
de fond pour ce tour, ne sonne qu'une fois, à la fin ; un tour que tu as coupé ne sonne pas.
L'entrée **Sons** du menu les coupe.

## Remises à zéro

Claude propose parfois de remettre une limite à zéro avant l'heure : un reset de la session 5 h,
décompté du quota hebdomadaire, ou des recharges offertes pour un temps. L'app affiche l'offre sous
les jauges du compte qui y a droit — en couleur quand elle est utilisable tout de suite, en gris
avec la date quand elle ne l'est pas encore. Le déclenchement se fait dans Claude Code, par
`/limit-reset` : IA-Conso ne fait que lire.

Ces offres ne sont servies que si on les demande (`?at_wall=1`) et circulent sous des clés à noms
de code qui changent d'une expérience à l'autre. Un compte qui n'y a pas droit n'affiche rien —
c'est le cas courant, l'offre étant parfois réservée à claude.ai.

## Plusieurs organisations

Le token ne voit qu'une organisation à la fois. L'app liste toutes les organisations « chat »,
montre la conso en direct pour l'active et mémorise la dernière lue pour les autres. Une fenêtre
mémorisée dont l'échéance est passée n'est plus affichée : elle s'est réarmée depuis, sa valeur
n'a plus cours.

## Réglages

`~/.config/ia-conso/config.json` :

- `refresh_seconds` : le cycle de lecture de la conso (60 s par défaut) ;
- `sounds` : les sons, activés par défaut — c'est aussi ce que bascule l'entrée **Sons** du menu ;
- `sound_finished` et `sound_attention` : le nom d'un son système (`/System/Library/Sounds`), un
  nom vide coupant ce son-là.

L'app réécrit le fichier s'il manque une clé. Après un échec, elle attend au moins 30 s — et le délai demandé par
Anthropic en cas de 429 — avant de retenter.

## Ce que l'app écrit

Tout est local : `~/Library/Application Support/IAConso` (miroir de la barre, conso mémorisée par
organisation, journal des pannes) et `~/Library/Caches/IAConso` (photos de profil). Une seule
requête sort vers un tiers : Gravatar, qui reçoit une empreinte de l'adresse e-mail pour rendre la
photo de profil.

## Couleurs de la barre

Bleu 0–20 %, vert 20–40 %, jaune 40–60 %, orange 60–80 %, rouge 80–100 %.
