# Charger le clone d'un serveur de production dans la base dev

But : faire tourner `//raceimport` sur de vraies données, à une échelle que la base
dev ne peut pas produire. 71 lignes ne disent rien sur le comportement d'un import
qui doit avaler quinze ans de statistiques.

Ce document couvre la réception d'un dump. Il ne dit pas quoi demander — voir
`docs/demande-clone-stats.md`.

## Ce qu'on charge, et ce qu'on ne charge pas

| Table | Pourquoi |
|---|---|
| `stats_scores` | la matière première : chaque temps jamais réalisé, horodaté |
| `map` | pour que les `map_id` de `stats_scores` pointent sur quelque chose |
| `player` | idem pour les `player_id` |
| `localrecord` | meilleur temps par carte/joueur, utile à l'estimation de force (étape 3) |

Tout le reste reste chez son propriétaire. En particulier :

- **`wallet`** — les soldes de Planets de ses joueurs. Aucun usage ici, et de
  l'argent réel.
- **`setting`** — la configuration de tous ses plugins, secrets éventuels compris.
- **`bet`** — la nôtre. S'il y en a une chez lui, elle n'a rien à faire dans nos tests.

## Données personnelles

`player.last_ip` contient les adresses IP de ses joueurs. On n'en a aucun usage.
Le script les efface juste après le chargement, avant toute autre chose.

C'est une atténuation, pas une solution : les IP auront transité par le dump. Le
propriétaire du serveur doit le savoir avant d'envoyer quoi que ce soit, et c'est
sa décision, pas la nôtre. S'il préfère, il peut les vider lui-même avant le dump :

```sql
-- chez lui, sur une copie, jamais sur sa base de production
UPDATE player SET last_ip = NULL;
```

## Avant de charger

Le clone remplace la base dev. Elle ne contient rien qui ne se recrée pas, mais
vérifie quand même qu'aucun test en cours n'en dépend.

Regarde la taille du dump. Au-delà de quelques Go, l'import prend des heures et il
vaut mieux le lancer le soir.

## Charger

```bash
bash tools/load-stats-clone.sh /chemin/vers/dump.sql
```

Le script :

1. refuse de tourner si le fichier n'existe pas, ou si les conteneurs sont éteints ;
2. sauvegarde la base dev actuelle dans `tmp/` avant de toucher à quoi que ce soit ;
3. charge le dump ;
4. vide `player.last_ip` immédiatement ;
5. affiche les compteurs et vérifie que les clés étrangères tiennent — un
   `stats_scores` qui référence des cartes absentes du dump donnerait un import
   faux plutôt qu'une erreur, et c'est le genre de chose qui se voit tard ;
6. relance le conteneur PyPlanet pour qu'il recrée `raceresult` et ses propres tables.

## Après

`raceresult` est vide et n'a rien à voir avec le clone : c'est notre table, pas la
sienne. L'import se lance ensuite normalement, en preview d'abord :

```
//raceimport
```

Ce qu'on regarde à ce moment-là, dans l'ordre :

- **le temps que met le preview.** C'est un parcours complet des statistiques. S'il
  dépasse la minute, la commande devra être déportée hors du thread de jeu avant
  d'être proposée à qui que ce soit en production.
- **la taille des plateaux.** Des courses à 2-30 pilotes sont plausibles. Un paquet
  de courses juste sous `MAX_FIELD_SIZE` veut dire que le gap ne découpe rien et
  que des soirées entières fusionnent.
- **le nombre de courses par carte.** Une carte jouée quinze ans devrait en produire
  des centaines. Une seule, c'est un découpage qui n'a pas fonctionné.

Le gap se règle sans rien casser : `//raceimport confirm 8` reconstruit tout avec
huit minutes, `//raceimport clear` efface. Aucune ligne live n'est concernée.

## Rendre la base dev

```bash
bash tools/load-stats-clone.sh --restore
```

Recharge la sauvegarde prise à l'étape 2. À faire dès que les tests sont finis : ce
clone contient les données de joueurs réels et n'a pas vocation à rester sur une
machine de développement.
