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

## « Désactivé » ne veut pas dire éteint

Pour l'extra, `is_enabled` à faux veut dire « ne peut pas couvrir les envois en ce moment » — c'est
la définition même de Claude Code. La cause est dans `disabled_reason`, et Claude Code en range
trois parmi les états actifs mais bloqués : `org_level_disabled_until` (plafond mensuel de
l'organisation atteint), `org_spend_cap_reached` (plafond individuel atteint) et `out_of_credits`
(solde prépayé épuisé). Les autres raisons sont de vraies coupures, que seul un admin peut lever.

`org_level_disabled_until` n'est un plafond atteint que si `extra_usage.spend_limit_reached` le
confirme ; sans lui, Claude Code le traite comme une coupure. Et un membre d'équipe ne reçoit ni le
plafond ni la dépense de son organisation : quand ce plafond est atteint, la jauge de l'organisation
est pleine par définition, et la seule somme connue est la sienne.

Le solde prépayé et le plafond mensuel sont deux compteurs : on peut être à sec avec un plafond à
peine entamé. La jauge montre le plafond, la raison dit que c'est le solde qui bloque.

L'API de conso ne date pas la remise à zéro. Claude Code la calcule localement au 1er du mois
suivant, et c'est aussi ce que fixe son simulateur interne : l'app fait de même, en l'affichant
« estimé ». Aucune date de bascule — « activé depuis », « coupé depuis » — n'est exposée.

## La cadence se compte sur la tentative, pas sur la donnée

`Snapshot` porte deux horodatages : `fetched_at` (dernière lecture réussie, pour la fraîcheur
affichée) et `attempted_at` (dernière tentative, réussie ou non). Le compte à rebours se base sur le
second. Sur le premier, un échec durable laisse le compteur à zéro en permanence et l'app relance un
cycle complet à chaque tick de 0,5 s — sur le token OAuth partagé avec Claude Code, c'est un 429
auto-entretenu.

Corollaire : `STUCK_AFTER` doit rester au-dessus de la somme des délais réseau d'un cycle (trousseau
10 s + cinq appels à 8 s), sinon le garde-fou déclare perdu un cycle qui allait aboutir, en lance un
second, et le premier continue de tourner.

## En retrait : ce qui n'est pas à ta disposition

Une seule convention visuelle dit « pas à ta disposition tel quel » : le bloc passe à mi-opacité —
libellé, chiffre, horodatage et barre ensemble. Elle couvre deux situations :

- **un chiffre qui n'a pas été lu à ce cycle** (`AccountView.is_live` à faux) : un compte inactif,
  ou l'organisation active reprise après une panne de jeton. Il dit où on en était, pas où on en est ;
- **une fonction qu'on ne peut pas utiliser** : un extra qui ne couvre pas les envois, quelle
  qu'en soit la raison — plafond d'équipe, solde à sec, coupure par un admin.

Les deux partagent le signal, et le détail gris dit toujours laquelle des deux s'applique. Une
jauge garde sa barre même vide : un trait à la place casserait l'alignement du menu.

Un quota plein n'en fait pas partie. Une session ou une semaine à 100 % est la même situation — on
ne peut plus rien faire — mais c'est l'alerte la plus importante du menu, et le retrait l'éteindrait.
Elle reste en rouge vif.

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

Les sessions sont identifiées par leur `sessionId` et les agents Cursor par leur `composerId`,
jamais par leur titre : deux sessions homonymes sont deux sessions. Le `sessionId` est aussi ce qui
relie une session vivante à son transcript, donc à la liste des sessions terminées, et deux process
qui reprennent la même session n'en font qu'une.

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

« idle » veut dire « attend l'utilisateur », pas « ne fait rien ». Une session qui lance une commande
en arrière-plan — une recette de plusieurs minutes, par exemple — rend la main aussitôt et passe
« idle » jusqu'à la notification qui la réveille. Sans autre signal, elle paraît éteinte pendant
tout ce temps. Le transcript borne ces commandes : un résultat d'outil qui commence par l'accusé de
lancement (lancée d'emblée en arrière-plan, ou basculée d'office après un délai), puis une
notification en tête de message. Une commande lancée mais jamais notifiée depuis deux heures ne
compte plus : la session a pu être tuée entre-temps. Une session « idle » reste donc affichée tant
qu'elle a des agents ou des commandes de fond en vol.

Les agents en vol se lisent de deux façons, parce qu'ils ne laissent pas la même trace. Ceux d'un
workflow ont un journal qui les ouvre et les ferme (`started` sans `result` ni `failed`), à
condition de n'ouvrir que les journaux encore chauds : un workflow interrompu laisse ses agents
« démarrés » pour toujours. Ceux lancés hors workflow, eux, ne sont fermés par rien — l'outil rend
son résultat dès le lancement, longtemps avant la fin du travail — et se comptent donc par
l'activité de leur propre transcript.

## Ce qui compte comme « terminée » ou « coupée »

Le fichier de `~/.claude/sessions/` disparaît à la fermeture de Claude Code : seul le transcript
reste, c'est donc lui qui liste les sessions finies. Sa date ne date rien — Claude Code y réécrit
titre et dernier prompt à chaque reprise, même sans rien demander. La fin d'une session est celle
de son dernier tour : le dernier message de l'assistant.

Une erreur d'API s'inscrit elle aussi comme un message de l'assistant, au modèle `<synthetic>`,
marqué `isApiErrorMessage`, avec le code de l'erreur dans `error`. Pour une limite (`rate_limit`),
le bloc `quotaLimits` dit laquelle (`rateLimitType` : `five_hour`, `seven_day`…) et quand elle se
réarme (`resetsAt`). Une session dont le dernier tour est une telle erreur a été coupée ; dès
qu'elle est relancée, un vrai message suit et elle redevient une session comme les autres. Ce
message synthétique n'a ni modèle ni usage : il date la fin du tour, sans effacer le modèle et le
contexte du tour précédent.

Les messages marqués `isSidechain` sont ceux d'un agent : ni leur modèle, ni leur fin, ni leur
coupure ne sont ceux de la session.

Côté Cursor, un composer qui n'a pas bougé depuis la fenêtre de fraîcheur ne peut plus être en
cours : il est terminé à sa dernière mise à jour. Un brouillon, ou un composer sans nom, n'a jamais
rien fait et n'est pas listé.

## Quand sonner

Un son se décide sur une transition entre deux sondages, jamais sur un état : une session qui
attend depuis dix minutes ne resonne pas à chaque seconde, et le premier sondage après le
lancement ne sonne jamais.

**Fin de tour.** Une session sonne quand on l'a vue travailler (`busy`) puis qu'on la lit revenue
à `idle`. C'est le statut de Claude Code qui fait foi : il garde la session `busy` tant que ses
agents ou son workflow tournent, et son `idle` est la vraie fin du tour — une session qui a lancé
des agents sonne donc une fois, à la fin. Le comptage d'agents de l'app, qui les retient encore un
moment après leur dernière écriture, sert à l'affichage, pas au son. Une commande de fond ne
retient pas le son non plus : un serveur lancé en arrière-plan ne s'arrête jamais, et la réponse
est déjà là. En terminal, Claude Code écrit `shell` pour cet état-là : c'est un `idle`.

La session doit être lue revenue au calme, pas seulement absente : un fichier de session lu au
moment où Claude Code le réécrit disparaît le temps d'un sondage, et n'a rien fini pour autant.
Une absence plus longue est une session fermée en plein tour : elle est oubliée, pour ne pas
sonner le jour où elle est rouverte.

**Intervention.** Une session qui passe à `waiting` — permission, question, plan à valider — alors
qu'on la lisait juste avant sans qu'elle attende. En terminal, un dialogue que l'utilisateur ouvre
lui-même (`/model`, `/config`) s'écrit aussi `waiting`, avec `waitingFor: "dialog open"` : ce n'est
pas la session qui demande, et il compte comme un `idle`.

Une session coupée par une erreur d'API sonne ce son-là plutôt que celui de la fin : elle est à
relancer. Un tour coupé en moins d'une seconde peut tomber entre deux sondages sans qu'on ait vu
la session travailler ; c'est donc l'apparition d'une coupure dans la liste des terminées qui
sonne, chacune une seule fois, reconnue par sa session et sa date.

**Silence.** Un tour coupé par l'utilisateur ne sonne pas : Claude Code inscrit alors un message
`[Request interrupted by user]` (ou `… for tool use]` pour un outil refusé), que le transcript
garde jusqu'au tour suivant. Coupé avant toute réponse, le tour n'a que ce marqueur pour le dater :
c'est lui qui fixe sa fin. Le message `<synthetic>` « No response requested. » qui suit ne dit rien
du modèle ni du contexte.

Deux sons dans le même sondage n'en font qu'un, l'intervention d'abord — sauf si son nom est vide
dans les réglages, auquel cas la fin de tour sonne quand même.

Côté Cursor, un composer bloqué sur une approbation peut rester sans mise à jour bien au-delà de
la fenêtre de fraîcheur : il n'a rien fini, il n'est ni listé parmi les terminés ni sonné.

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
