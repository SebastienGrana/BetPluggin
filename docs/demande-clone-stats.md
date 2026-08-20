# Demander un clone des statistiques à un serveur de production

On a besoin de vraies données, à une échelle que la base dev ne produira jamais.
Ce document contient la demande à envoyer, en anglais, et ce qu'il faut en attendre.

Pour charger le dump une fois reçu : `docs/import-clone-stats.md`.

## Principe

On demande **des compteurs d'abord, pas un dump**. La requête coûte dix secondes à
celui qui la lance et décide de tout le reste : selon qu'il annonce deux millions ou
quarante millions de lignes, on demande la base entière ou un échantillon.

Demander un dump directement, c'est risquer qu'il lance un export de plusieurs Go
sur son serveur de production sans que personne ait su à l'avance ce que ça
représentait.

## Étape 1 — les compteurs

> Hey Santa,
>
> I'm building a betting plugin for PyPlanet. One part of it estimates how strong
> each driver is, and it does that by reading past races. On a fresh server that
> means waiting weeks before the numbers mean anything — but PyPlanet has been
> recording every finish on your server for years, in `stats_scores`, and I can
> reconstruct those races from it.
>
> Before asking you for anything heavy: could you run this? It's a read-only count,
> it takes a couple of seconds, and it tells me whether what I want is reasonable or
> completely out of proportion.
>
> ```sql
> SELECT
>   (SELECT COUNT(*) FROM stats_scores) AS scores,
>   (SELECT COUNT(*) FROM localrecord)  AS records,
>   (SELECT COUNT(*) FROM map)          AS maps,
>   (SELECT COUNT(*) FROM player)       AS players,
>   (SELECT MIN(created_at) FROM stats_scores) AS oldest;
> ```
>
> Depending on the numbers I'll either ask for a dump of four tables, or just a
> sample of the most-played maps. I'll tell you which, and I'll never ask for
> `wallet` or `setting` — your players' Planets balances and your plugin config are
> none of my business.
>
> Thanks!

## Étape 2 — le dump, si les compteurs le permettent

Quatre tables seulement : `stats_scores`, `map`, `player`, `localrecord`.

> Thanks — that's workable. Could you dump these four tables?
>
> ```bash
> mysqldump -u USER -p --single-transaction \
>   YOUR_DB stats_scores map player localrecord > pyplanet-stats.sql
> gzip pyplanet-stats.sql
> ```
>
> `--single-transaction` matters: it means the dump doesn't lock your tables, so the
> server keeps running normally while it writes.
>
> One thing you should know before you send it. The `player` table has a `last_ip`
> column — your players' IP addresses. I have no use for it and my import script
> wipes it the moment the dump lands, but it would still have travelled. If you'd
> rather it never left, blank it on a copy first and dump that instead:
>
> ```sql
> UPDATE player SET last_ip = NULL;
> ```
>
> Your server, your players, your call. Tell me either way.
>
> I'll delete the whole thing once I'm done testing.

## Étape 2 bis — l'échantillon, si la base est énorme

Au-delà de ~20 millions de lignes dans `stats_scores`, le dump complet devient
pénible pour tout le monde. Un échantillon des cartes les plus jouées suffit : on
veut mesurer la charge et la qualité du découpage, pas posséder son historique.

> That's bigger than I expected — let's not move all of it. Just the 200 most-played
> maps would tell me everything I need:
>
> ```sql
> CREATE TEMPORARY TABLE top_maps AS
>   SELECT map_id FROM stats_scores GROUP BY map_id ORDER BY COUNT(*) DESC LIMIT 200;
> ```
>
> ```bash
> mysqldump -u USER -p --single-transaction YOUR_DB map player > part1.sql
> mysqldump -u USER -p --single-transaction YOUR_DB stats_scores localrecord \
>   --where="map_id IN (SELECT map_id FROM (SELECT map_id FROM stats_scores GROUP BY map_id ORDER BY COUNT(*) DESC LIMIT 200) t)" > part2.sql
> ```
>
> Same note about `last_ip` as before.

Attention : `map` et `player` sont dumpés **en entier** dans cette variante. C'est
volontaire — ce sont les petites tables, et les tronquer casserait les clés
étrangères de `stats_scores`, ce que le script de chargement détecterait mais qu'on
préfère éviter.

## Ce qu'on regarde une fois chargé

Voir `docs/import-clone-stats.md`, section « Après ». En résumé : le temps du
preview, la taille des plateaux, le nombre de courses par carte.

## Après les tests

Le clone contient les données de joueurs réels. Il repart dès que les tests sont
finis :

```bash
bash tools/load-stats-clone.sh --restore
```

Et on le lui dit.
