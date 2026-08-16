# Paris sur la position — conception

État : **proposition, rien n'est codé.** À valider avant implémentation.

---

## 1. L'idée qui simplifie tout

Deux sliders — un minimum, un maximum — ne sont pas une option parmi d'autres. C'est **la
généralisation de toutes les autres**. Un pari s'écrit :

> *« ce pilote finira entre la place `min` et la place `max` »*

Et alors :

| Intervalle | Ce que ça donne | Correspond à |
|---|---|---|
| `[1, 1]` | il gagne | le pari actuel |
| `[2, 2]` | il finit exactement 2ᵉ | option 3 |
| `[1, 3]` | il monte sur le podium | option 4 |
| `[1, 5]` | il finit dans le top 5 | la « ligne » de l'option 2 |
| `[4, 8]` | il finit **hors** du top 3 | le pari inverse |

Donc : **une seule fonctionnalité à écrire, pas quatre.** C'était la bonne intuition.

L'ancien pari « X termine 1ᵉʳ » **disparaît en tant que type distinct** : il devient l'intervalle
`[1,1]`. Un seul mécanisme, une seule fenêtre, un seul calcul de cote.

## 2. Ce que ça ne remplace pas

Le duel, non. « X finit devant Y » est une question **relative**, l'intervalle est **absolu**, et les
deux peuvent être en désaccord : X peut finir devant Y alors que les deux sont hors du top 5.

Et sous la forme retenue — un **défi lancé entre deux pilotes**, pas un marché ouvert aux
spectateurs — c'est une fonctionnalité complètement séparée, décrite au §11.

## 3. À lire avant de coder : le piège de migration

Cette fonctionnalité a besoin d'une nouvelle table *et* de nouvelles colonnes sur `bet`. Livrer les
deux ensemble casse la production en silence — le mécanisme exact est décrit dans
[`../apps/betpluggin/migrations/README.md`](../apps/betpluggin/migrations/README.md).

**Deux déploiements, dans cet ordre :** d'abord la migration des colonnes, vérifiée avec
`DESCRIBE bet;` ; ensuite seulement le nouveau modèle. Vu que chaque déploiement chez ManiaServ =
un ticket support, c'est à prévoir dès maintenant dans le planning.

## 4. Données

### Nouvelles colonnes sur `Bet` (déploiement 1)

| Colonne | Type | Rôle |
|---|---|---|
| `bet_type` | `CharField`, défaut `'winner'` | `winner` / `range` / `duel` / `duel_side` |
| `pos_min`, `pos_max` | `IntegerField`, null | les bornes, pour `range` |
| `opponent_login` | `CharField`, null | l'adversaire, pour `duel` et `duel_side` |
| `payout_multiplier` | `FloatField`, null | **le prix verrouillé à la prise du pari** |

**Le duel n'a besoin d'aucune nouvelle table.** Les deux cagnottes du §11 se distinguent par
`bet_type` : les mises de X et Y sont des lignes `duel`, celles des spectateurs des lignes
`duel_side`, et `target_login` / `opponent_login` disent qui affronte qui. Le temps d'attente entre
« X déclare » et « Y accepte » ne touche pas la base — rien n'est prélevé à ce stade — donc il peut
rester en mémoire. Un redémarrage serveur pendant ce court instant annule le défi, ce qui est le bon
comportement.

Conséquence directe : **le duel tient en un seul déploiement**, puisqu'il n'ajoute pas de modèle et
ne déclenche donc pas le piège du §3. Les paris de position, eux, en demandent deux.

`payout_multiplier` est distinct du champ `odds` existant, et la distinction est importante : `odds`
est une *estimation indicative* en pari mutuel, qui bouge quand d'autres misent. `payout_multiplier`
est une **promesse ferme**. Une fois écrit, il ne bouge plus, quoi qu'il arrive au marché ensuite.

### Nouvelle table `RaceResult` (déploiement 2)

`map`, `player`, `position`, `points`, `time`, `created_at`.

**Bonne nouvelle : cette donnée circule déjà.** `on_scores` reçoit le classement complet au podium
(`players[]` avec `rank`, `map_points`, `player`) — voir `apps/betpluggin/__init__.py:664`. Le
plugin s'en sert uniquement pour identifier le vainqueur et jette le reste. Il suffit de l'écrire.

## 5. Calculer les probabilités

Chaque pilote reçoit une **force**, estimée depuis `RaceResult`.

Pour chaque course passée, son score normalisé vaut `1 − (position − 1) / (nb_pilotes − 1)` — soit 1
pour le vainqueur, 0 pour le dernier. On fait la moyenne sur les 40 dernières courses, puis :

```
score_ajusté = 0.5 + (moyenne − 0.5) × n / (n + 10)
force        = exp(4 × score_ajusté)
```

Le terme `n / (n + 10)` est la partie qui compte : un pilote avec 2 courses au compteur est tiré
vers la moyenne, un pilote avec 60 courses garde son vrai niveau. **Sans ça, quelqu'un qui gagne ses
deux premières courses est évalué comme imbattable, et le serveur lui vend des cotes ruineuses.**

### De la force aux probabilités

Simulation, 10 000 courses fictives. Chaque tirage : on choisit le vainqueur au hasard
proportionnellement aux forces, on le retire, on recommence pour la 2ᵉ place, etc.

On compte, et on obtient une **matrice pilote × position** :

|  | 1ᵉʳ | 2ᵉ | 3ᵉ | 4ᵉ | … |
|---|---|---|---|---|---|
| Alice | 30 % | 22 % | 16 % | 11 % | … |
| Bob | 8 % | 11 % | 13 % | 14 % | … |

N'importe quel intervalle est alors une **somme de cases**. `Alice [1,3]` = 30 + 22 + 16 = **68 %**.
On simule **une fois** à l'ouverture du marché, on garde la matrice, et chaque cote demandée ensuite
est une addition. C'est immédiat.

## 6. La marge, et pourquoi le serveur gagne sur la durée

Si le serveur payait la cote juste (`1 / p`), il ne gagnerait ni ne perdrait rien en moyenne — et
comme il joue un nombre fini de paris, il finirait par se faire trouer par la variance.

Il paie donc légèrement moins que le juste prix :

```
multiplicateur = (1 − marge) / p        marge = 5 % (décidé)
```

Concrètement, sur `Alice [1,3]` à 68 % : prix juste × 1.47, prix affiché **× 1.40**. Sur 100 planets
misés, le joueur en récupère 140 au lieu de 147.

C'est ça qui transforme « le serveur peut perdre » en « le serveur perd parfois, mais gagne en
moyenne ».

### Ce que coûte une marge basse

Tu as demandé quelques pour cent plutôt que 10 %, et c'est cohérent avec l'esprit du plugin. Mais il
faut savoir ce que ça implique, parce que ce n'est pas proportionnel :

> **Le nombre de paris nécessaires avant que la marge se voie varie comme 1/marge². Diviser la marge
> par deux quadruple ce nombre.**

En ordre de grandeur, pour que le gain attendu dépasse nettement les fluctuations : ~850 paris à
10 %, ~3 400 à 5 %, ~9 500 à 3 %. Sur un serveur de cette taille, 5 % veut dire **des mois** avant
que la marge se distingue du hasard.

Concrètement : à 5 %, le serveur passera des soirées, voire des semaines, dans le rouge, et ce sera
parfaitement normal — pas un bug, pas un modèle cassé. C'est la raison pour laquelle les garde-fous
du §7 comptent **plus** à marge basse qu'à marge haute : le coussin est plus mince.

Vu que la caisse peut être réapprovisionnée, c'est un choix tenable. Il fallait juste qu'il soit fait
en connaissance de cause.

## 7. Garde-fous

Tu as dit que le risque était acceptable — la caisse peut être réapprovisionnée et les planets
s'accumulent sans emploi. D'accord. Ces protections coûtent quelques lignes, alors autant les avoir,
parce qu'elles ne protègent pas seulement du risque financier :

- **Plancher de probabilité, `p ≥ 2 %`.** En dessous, le multiplicateur dépasse ×45 et surtout le
  modèle n'a aucune fiabilité dans les cas extrêmes. On refuse le pari plutôt que de vendre un prix
  qu'on ne sait pas calculer.
- **Plafond de gain par pari.**
- **Exposition maximale par map** : la somme des gains potentiels ouverts reste sous un pourcentage
  de la réserve.
- **Fermeture automatique** des paris à cote si la réserve passe sous un seuil. Le pari mutuel, lui,
  continue de fonctionner — il ne risque rien.
- **Interdiction de parier sur soi-même en cote fixe.** C'est le vrai trou : un très bon pilote qui
  arrive sans historique est évalué comme moyen (§5), il le sait, et il mise gros sur lui-même. En
  pari mutuel ce n'était pas un problème — il jouait contre les autres joueurs, pas contre la caisse.

## 8. Interface

**Vérifié :** le slider est faisable, mais pas n'importe où. Les fenêtres de type liste embarquent
`list.Script.Txt`, qui contient déjà une boucle `while(True) { … yield; }` — tout ManiaScript ajouté
après ne s'exécuterait jamais. En revanche, une vue `TemplateView` que l'on écrit entièrement
(comme `alert.xml` / `prompt.xml`) **n'embarque aucun script**. L'emplacement est libre.

Donc : le marché reste une liste, et cliquer sur un pilote ouvre **une fenêtre dédiée** — celle qui
porte les deux sliders, avec un ManiaScript qui nous appartient.

Ce que la fenêtre affiche, en direct pendant qu'on bouge les sliders : le pilote, l'intervalle
choisi, la probabilité, le multiplicateur, et le gain pour la mise en cours.

Le calcul du multiplicateur peut se faire **côté client** : la matrice du §5 est petite (une ligne
par pilote), on l'envoie dans le manialink et le ManiaScript fait la somme. Les cotes réagissent
instantanément au slider, sans aucun aller-retour serveur. Le serveur recalcule et **revérifie** au
moment de valider — le client ne fait qu'afficher, il ne décide jamais du prix.

## 9. Démarrage à froid et phasage

Le point qui commande le calendrier : **le modèle a besoin d'historique, et cet historique n'existe
pas encore.** Rien n'enregistre les classements aujourd'hui. Avant une trentaine de courses, tous
les pilotes se ressemblent et les cotes ne veulent pas dire grand-chose.

D'où l'ordre :

| Étape | Contenu | Déploiement | Besoin de données ? |
|---|---|---|---|
| **1** | Colonnes sur `bet` + **le duel complet** (§11) | 1 seul, sans nouveau modèle | **non** — jouable tout de suite |
| **2** | Table `RaceResult` + enregistrement des classements | 1, après vérification du `DESCRIBE` | non (elle les produit) |
| **3** | Sliders + cotes, une fois ~30 courses accumulées | inclus dans le 2 ou suivant | oui |

L'étape 1 rend le plugin plus intéressant **dès le prochain déploiement** : elle ne dépend d'aucun
historique, n'ajoute aucune table, et ne fait courir aucun risque à la caisse. Pendant ce temps
l'étape 2 accumule tranquillement de quoi alimenter l'étape 3.

C'est aussi l'ordre qui respecte la séparation imposée par le §3 sans effort : l'étape 1 ne contient
que des colonnes, l'étape 2 n'ajoute son modèle qu'une fois ces colonnes vérifiées en production.

## 10. Décisions prises

### Pilote qui ne finit pas → remboursement

S'il n'apparaît pas dans le classement final (déconnexion, abandon), les paris qui le visent sont
remboursés. Le mécanisme existe déjà : c'est celui utilisé quand le classement n'arrive jamais.

### Plateau qui rétrécit → annulation de la période, annoncée d'avance

Il faut voir que le rétrécissement **ne favorise pas tout le monde dans le même sens**. Quand des
pilotes abandonnent, les restants remontent : les paris `[1, x]` (« dans les premiers ») deviennent
plus faciles, les paris `[x, N]` (« hors du top 3 ») deviennent plus durs.

C'est exactement pour ça qu'on ne peut pas rembourser seulement ceux qui y perdent — ce serait
rembourser les perdants et garder les gagnants, et personne ne trouverait ça juste. **La règle doit
donc être tout-ou-rien, et connue avant de miser.**

- Le **plateau de référence** est figé à l'ouverture du marché et annoncé dans le chat.
- La borne `max` d'un pari ne peut pas dépasser ce plateau.
- Si le nombre d'arrivants s'en écarte de plus de `max(1, 25 %)`, **tous** les paris de position de
  la période sont remboursés. À 8 pilotes, tolérance 2 → annulation en dessous de 6 arrivants ; à
  4 pilotes, tolérance 1 → annulation en dessous de 3.

L'annulation est annoncée avec sa raison, pour que ça ne passe jamais pour un bug.

### Marge → 5 %

Voir §6, y compris ce que ça coûte en durée avant que la marge se voie.

### Valeurs réglables à chaud

Marge, plafond de gain, exposition maximale, seuil de réserve, tolérance de plateau : **tous en
réglages PyPlanet, pas en constantes dans le code.** Chaque déploiement chez ManiaServ coûte un
ticket support — il serait absurde d'en dépenser un pour passer la marge de 5 % à 4 %.

## 11. Le duel

Un **défi lancé entre deux pilotes**, annoncé dans le chat, sur lequel les autres peuvent ensuite
parier. Le serveur ne fixe aucun prix : il ne risque rien, et **l'exploit du §7 (parier sur soi-même
sans historique) disparaît de lui-même** — il n'y a pas de cote à tromper, juste deux joueurs qui se
mettent d'accord.

### Déroulé

1. X lance le défi : adversaire Y, mise Z, portée = la map.
2. Y reçoit une fenêtre **Oui / Non**. S'il refuse, ou ne répond pas avant expiration, **tout
   s'annule** — et rien n'a encore été prélevé (voir plus bas).
3. S'il accepte, Y choisit sa mise : **moins, autant ou plus** que X.
4. Le défi est annoncé dans le chat. Les spectateurs peuvent parier sur X ou sur Y **pendant toute la
   map**. X et Y, eux, ne peuvent plus rien changer.
5. À l'arrivée, celui des deux qui finit devant l'emporte.

### Deux cagnottes, pas une

C'est le point important, et il vient d'un défaut trouvé en chiffrant la version à cagnotte unique.

X mise 500, Y accepte pour 200. **Sans spectateurs**, en cagnotte unique : le pot fait 700, X gagne
et récupère `500 × 700/500` = 700, soit **+200 net** — exactement la mise de Y. Le handicap
fonctionne tout seul, sans code dédié. Jusque-là, parfait.

**Avec 2000 planets de spectateurs sur X** : le pot sur X passe à 2500, le total à 2700, et X
récupère `500 × 2700/2500` = 540. **+40 au lieu de +200.**

X et Y s'étaient mis d'accord sur « mes 500 contre tes 200 ». La foule vient de réécrire leur accord
sans les prévenir — et plus le challenger est populaire, moins son défi lui rapporte. C'est le
contraire de l'effet recherché.

Donc :

- **Cagnotte du duel** — X et Y seulement. Le gagnant prend la mise du perdant, exactement comme
  convenu. Rien ni personne ne peut la modifier après l'accord.
- **Cagnotte des spectateurs** — pari mutuel classique sur la même question, totalement séparée.
  Les spectateurs jouent entre eux ; ils n'influencent ni ne subissent le duel.

Si tous les spectateurs ont misé du côté du perdant, personne n'a gagné cette cagnotte-là et elle est
remboursée — le mécanisme existe déjà. Le duel, lui, se règle quand même.

### Pourquoi laisser les paris ouverts toute la map ne pose pas de problème

En cours de map, l'issue devient de plus en plus lisible. On pourrait craindre que parier tard soit
de l'argent gratuit. Le pari mutuel s'en occupe seul : quand tout le monde se range du côté qui mène,
la cote de ce côté tombe vers ×1, et le pari tardif ne rapporte quasiment rien. Miser tard est donc
sans risque **et sans intérêt**. Rien à corriger.

### Ordre des prélèvements

Les mises passent par SendBill, qui demande une confirmation dans le client — donc deux
confirmations, et chacune peut échouer. L'ordre compte :

```
X déclare  →  Y accepte (fenêtre oui/non)  →  SendBill à X et à Y
           →  les deux confirment  →  duel actif, annoncé dans le chat
```

**Rien n'est prélevé avant que Y ait accepté**, sinon un refus laisserait X à découvert le temps d'un
remboursement. Et si l'un des deux confirme mais pas l'autre, le premier est remboursé et le défi
tombe. Le plugin sait déjà faire ça pour un joueur ; il faut l'étendre à une paire.

### Cohabitation avec les paris de position

**Un réglage serveur choisit le mode actif** — duel ou position, jamais les deux à la fois. Deux
systèmes de paris ouverts en même temps sur la même map noieraient les joueurs, et rendraient
l'affichage du widget illisible.
