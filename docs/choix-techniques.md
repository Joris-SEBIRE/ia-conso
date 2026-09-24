# Choix techniques

Les décisions qui ne se devinent pas à la lecture du code, et ce qui les impose.

## Le pourcentage Cursor ne se calcule pas sur le forfait

`GetCurrentPeriodUsage` renvoie `planUsage` avec `includedSpend`, `bonusSpend`, `totalSpend`,
`limit`, et trois taux déjà calculés. La fraction intuitive `includedSpend / limit` est un piège :
dès que le forfait est saturé, les deux valent la même chose et le rapport reste collé à 100 %,
quelle que soit la conso. Cursor lui-même a cessé de l'afficher — il renvoie `displayThreshold: 200`
sur un rapport borné à 100, donc la barre correspondante est inatteignable.

Le bon chiffre est `totalPercentUsed`, avec `autoPercentUsed` en repli. La dépense est `totalSpend`,
et l'enveloppe s'en déduit : sur un relevé réel, `2508 / 5,5733 %` donne exactement 45 000 centimes
et `2508 / 5,3079 %` exactement 47 250 — deux valeurs rondes, ce qui confirme que `totalSpend` est
bien une dépense et non une allocation. C'est pour ça que `cap` est déduit plutôt que lu : par
construction, `used / cap` redonne le taux affiché, donc les deux ne peuvent jamais se contredire.

## Les offres de remise à zéro ne sont servies que si on les demande

`/api/oauth/usage` ne renvoie aucun bloc d'offre tel quel. Claude Code appelle
`/api/oauth/usage?at_wall=1` quand il veut savoir ce qui est proposé, et c'est cette réponse-là qui
porte `cedar_ember` (recharges nominatives) et `juniper_tide` (reset de session). Sans le paramètre,
les clés sont absentes et rien ne permet de deviner qu'une offre existe.

Le bloc dit aussi pourquoi il ne s'applique pas : `ineligible_reason` vaut par exemple `surface`
quand l'offre est réservée à claude.ai et ne concerne pas le client en ligne de commande. Un compte
inéligible n'affiche donc rien, ce qui est le cas courant.

L'app ne fait que lire. Consommer une remise à zéro est un `POST` sur
`/api/organizations/<uuid>/reset_rate_limits` : c'est irréversible et décompté, donc cela reste la
décision de l'utilisateur, dans Claude Code ou sur claude.ai.

## Aucune date de reset pour les crédits extra

Ni `/usage`, ni `/profile`, ni `/account` ne datent la remise à zéro des crédits hors forfait :
`spend` n'a que des montants, `extra_usage.daily` et `.weekly` sont nuls, et l'ancre de facturation
de l'organisation n'est pas le 1er du mois. L'app affiche donc « reset mensuel » et laisse
`resets_at` vide. Déduire une date au jour près donnerait une échéance fausse présentée comme sûre.

## La cadence se compte sur la tentative, pas sur la donnée

`Snapshot` porte deux horodatages : `fetched_at` (dernière lecture réussie, pour la fraîcheur
affichée) et `attempted_at` (dernière tentative, réussie ou non). Le compte à rebours se base sur le
second. Sur le premier, un échec durable laisse le compteur à zéro en permanence et l'app relance un
cycle complet à chaque tick de 0,5 s — sur le token OAuth partagé avec Claude Code, c'est un 429
auto-entretenu.

Corollaire : `STUCK_AFTER` doit rester au-dessus de la somme des délais réseau d'un cycle (trousseau
10 s + cinq appels à 8 s), sinon le garde-fou déclare perdu un cycle qui allait aboutir, en lance un
second, et le premier continue de tourner.

## Ce qui sort du cache se voit

Le token ne voit qu'une organisation à la fois, d'où le cache par organisation. Ses chiffres
gardent leur jauge — une jauge se lit mieux qu'une phrase — mais passent en retrait : ils disent où
on en était, pas où on en est. Le critère n'est pas « compte inactif » mais
« pas lu à ce cycle » (`AccountView.is_live`) : l'organisation active reprise après une panne de
jeton est tout aussi ancienne, et porte la même marque.

Une session 5 h mémorisée hier s'est réarmée depuis : ressortir son pourcentage serait afficher un
chiffre faux, teinté comme une alerte. Elle ressort donc à zéro, datée de son réarmement, seule
valeur encore certaine, avec « conso inconnue depuis ». La barre des menus, elle, n'a pas la place
de dater un zéro : elle montre « — » plutôt qu'un zéro qui passerait pour une lecture.

## L'activité se lit par la queue des fichiers

Les transcripts montent à la centaine de mégaoctets et la base d'état de Cursor à plusieurs
centaines. Deux règles tiennent le coût du sondage — qui tourne à la seconde — sous la milliseconde :

- on lit les derniers octets d'un fichier par `seek`, jamais le fichier entier, et on ne relit que
  si le `mtime` a changé ;
- on interroge SQLite par bornes (`key >= préfixe AND key < préfixe + '￿'`) et jamais par
  `LIKE` : l'index unique sur `key` n'est pas utilisé pour un `LIKE` tant que `case_sensitive_like`
  est off, et la requête balaie alors toute la table.

Les sessions sont identifiées par leur PID et les agents Cursor par leur `composerId`, jamais par
leur titre : deux sessions homonymes sont deux sessions.

## Reconnaître le cran « ultracode »

Le sélecteur d'effort de Claude Code a six crans pour cinq niveaux : le sixième, « Ultracode »,
pose l'effort à `xhigh` — le quatrième — et arme en plus l'orchestration de workflows. Le transcript
n'écrit donc que `xhigh`, alors que le curseur de l'utilisateur est au bout : sans ce marqueur, son
sélecteur et le menu se contrediraient. Les crans sont d'ailleurs affichés sous le libellé même de
Claude Code, pour qu'il n'y ait rien à convertir de tête.

L'état se lit ailleurs, et de façon structurée : Claude Code inscrit dans son propre transcript un
attachment `ultra_effort_enter` à l'armement et `ultra_effort_exit` à la retombée, et relit le
dernier pour retrouver son état. On applique la même règle. Ce marqueur n'est réécrit que tous les
dix prompts et se retrouve souvent à plusieurs mégaoctets de la fin du fichier : on remonte donc une
fois depuis la fin, borné à 8 Mo, puis on ne relit que ce qui s'ajoute.

Rien de tout cela n'est persisté ailleurs — c'est délibéré, le schéma de réglages de Claude Code
dit d'ultracode : « Session-scoped […] interactive toggles never persist it ».

## Lire du JSONL : `split("\n")`, jamais `splitlines()`

`str.splitlines()` coupe aussi sur U+2028, U+2029, \x0b et \x0c, que le texte rendu par un agent
peut très bien contenir. Une ligne JSON se retrouve alors coupée en deux, les deux moitiés sont
illisibles, et l'événement disparaît. Mesuré sur un journal d'agents : un seul U+2028 suffisait à
faire croire qu'un agent terminé tournait encore, pour toujours.

Même raison pour les requêtes sur la base de Cursor : les bubbles se trient sur `rowid`, jamais sur
`key`, dont le suffixe est un UUID aléatoire — trier dessus rendrait 25 lignes au hasard plutôt que
les 25 dernières écrites, et la détection des approbations en attente ne verrait plus rien.

## Ce qui compte comme « en cours »

Une session en attente d'une réponse ne consomme rien : elle est comptée dans la pastille rouge et
retirée de celle des actifs. Ses agents de fond, en revanche,
continuent de tourner et restent comptés.

Les agents en vol se lisent de deux façons, parce qu'ils ne laissent pas la même trace. Ceux d'un
workflow ont un journal qui les ouvre et les ferme (`started` sans `result` ni `failed`), à
condition de n'ouvrir que les journaux encore chauds : un workflow interrompu laisse ses agents
« démarrés » pour toujours. Ceux lancés hors workflow, eux, ne sont fermés par rien — l'outil rend
son résultat dès le lancement, longtemps avant la fin du travail — et se comptent donc par
l'activité de leur propre transcript.

## Le bundle et la barre des menus

L'exécutable principal du bundle **est** l'interpréteur, et l'app démarre par `sitecustomize.py` :
sur macOS 26, un exécutable lancé par Launch Services qui `exec` un autre binaire perd la place de
son élément de barre.

Pour la même raison, le LaunchAgent doit pointer sur `Contents/MacOS/IAConso` et non sur le dossier
`.app` — `launchd` ne sait pas exécuter un dossier, et l'entrée « Lancer au démarrage » resterait
cochée sans rien lancer. `launchagent.enable()` refuse d'écrire un plist qui ne pointe pas sur un
fichier exécutable.

## Le squelette partagé avec les apps sœurs

GitTodo, LinearTodo, SpacefillLocalhost et IA-Conso partagent un squelette : `Makefile`,
`build_app.sh`, `sitecustomize.py`, `shortcuts.py`, `launchagent.py`, les pastilles de comptage, les
glyphes, l'échelle de durées. Ce sont quatre dépôts et quatre apps autonomes : la duplication est
assumée, elle leur permet d'évoluer sans se casser mutuellement. La contrepartie est une règle :
**un correctif de squelette se réapplique aux quatre**.
