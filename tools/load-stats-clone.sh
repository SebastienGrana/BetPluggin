#!/usr/bin/env bash
#
# Load another server's PyPlanet statistics into the dev database, so //raceimport can be tried at a
# scale the dev server will never reach on its own. See docs/import-clone-stats.md.
#
# Refuses to touch anything it has not backed up first, and blanks the players' IP addresses the
# moment the load finishes -- we have no use for them and no business keeping them.
#
#   bash tools/load-stats-clone.sh /path/to/dump.sql
#   bash tools/load-stats-clone.sh --restore
#
set -euo pipefail

DB_CONTAINER=betpluggin-db-1
APP_CONTAINER=betpluggin-betpluggin-1
DB_NAME=pyplanet
DB_USER=pyplanet
DB_PASS=pyplanet

cd "$(dirname "$0")/.."
BACKUP=tmp/dev-db-before-clone.sql

# Git Bash on Windows rewrites anything that looks like a unix path in a docker argument.
export MSYS_NO_PATHCONV=1

die() { echo "ERREUR: $*" >&2; exit 1; }

mysql_in() { docker exec -i "$DB_CONTAINER" mysql -u"$DB_USER" -p"$DB_PASS" "$DB_NAME" "$@" 2>&1 | grep -v 'Using a password' || true; }

require_containers() {
	docker ps --format '{{.Names}}' | grep -qx "$DB_CONTAINER" || die "le conteneur $DB_CONTAINER ne tourne pas (docker compose up -d)"
}

restore() {
	[ -f "$BACKUP" ] || die "aucune sauvegarde a restaurer ($BACKUP)"
	echo "Restauration de la base dev depuis $BACKUP..."
	mysql_in -e "DROP DATABASE $DB_NAME; CREATE DATABASE $DB_NAME CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" || true
	docker exec -i "$DB_CONTAINER" mysql -u"$DB_USER" -p"$DB_PASS" "$DB_NAME" < "$BACKUP" 2>&1 | grep -v 'Using a password' || true
	docker restart "$APP_CONTAINER" >/dev/null
	echo "Base dev restauree. Le clone n'est plus sur cette machine."
}

require_containers

if [ "${1:-}" = "--restore" ]; then
	restore
	exit 0
fi

DUMP="${1:-}"
[ -n "$DUMP" ] || die "usage: bash tools/load-stats-clone.sh /chemin/vers/dump.sql  (ou --restore)"
[ -f "$DUMP" ] || die "fichier introuvable: $DUMP"

echo "Dump: $DUMP ($(du -h "$DUMP" | cut -f1))"

# 1. Sauvegarder avant de toucher a quoi que ce soit. La base dev se recree, mais pas en une commande.
mkdir -p tmp
echo "Sauvegarde de la base dev vers $BACKUP..."
docker exec "$DB_CONTAINER" mysqldump -u"$DB_USER" -p"$DB_PASS" "$DB_NAME" 2>/dev/null > "$BACKUP"
echo "  $(du -h "$BACKUP" | cut -f1)"

# 2. Charger. Le conteneur PyPlanet est arrete pendant ce temps: il ecrit dans cette base, et une
#    ecriture au milieu d'un chargement laisse un etat que personne ne saura expliquer ensuite.
echo "Arret de PyPlanet pendant le chargement..."
docker stop "$APP_CONTAINER" >/dev/null 2>&1 || true

echo "Chargement du clone (ca peut etre long)..."
docker exec -i "$DB_CONTAINER" mysql -u"$DB_USER" -p"$DB_PASS" "$DB_NAME" < "$DUMP" 2>&1 | grep -v 'Using a password' || true

# 3. Les IP des joueurs, tout de suite. Avant les compteurs, avant les verifications, avant tout ce
#    qui pourrait echouer et les laisser en place.
echo "Effacement de player.last_ip..."
mysql_in -e "UPDATE player SET last_ip = NULL WHERE last_ip IS NOT NULL;"

# 4. Ce qui est arrive.
echo
echo "--- Contenu ---"
mysql_in -e "
SELECT
  (SELECT COUNT(*) FROM stats_scores) AS scores,
  (SELECT COUNT(*) FROM localrecord) AS records,
  (SELECT COUNT(*) FROM map)         AS cartes,
  (SELECT COUNT(*) FROM player)      AS joueurs;
SELECT MIN(created_at) AS depuis, MAX(created_at) AS jusqua FROM stats_scores;
SELECT COUNT(*) AS ip_restantes FROM player WHERE last_ip IS NOT NULL;
"

# 5. Les cles etrangeres. Un stats_scores qui pointe sur des cartes absentes du dump ne provoque
#    aucune erreur -- il produit juste un import faux, et ca se voit trois jours plus tard.
echo "--- Integrite ---"
mysql_in -e "
SELECT COUNT(*) AS scores_sans_carte  FROM stats_scores s LEFT JOIN map    m ON m.id = s.map_id    WHERE m.id IS NULL;
SELECT COUNT(*) AS scores_sans_joueur FROM stats_scores s LEFT JOIN player p ON p.id = s.player_id WHERE p.id IS NULL;
"
echo "Les deux doivent etre a 0. Sinon le dump est incomplet: il manque map ou player."

# 6. PyPlanet recree ses propres tables, dont raceresult, qui n'a rien a voir avec le clone.
echo
echo "Redemarrage de PyPlanet..."
docker start "$APP_CONTAINER" >/dev/null
sleep 25
docker logs --tail 200 "$APP_CONTAINER" 2>&1 | grep -iE 'traceback|error|App is ready: betpluggin|successfully started' | tail -5

echo
echo "Pret. Lance '//raceimport' en preview d'abord, et chronometre-le."
echo "Pour rendre la base dev: bash tools/load-stats-clone.sh --restore"
