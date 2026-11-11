#!/usr/bin/env bash
# Déploie le module appalach_hub sur le VPS du hub (base « hub »), depuis le poste local.
#
#   scripts/deploy_hub.sh
#
# Étapes : git pull → arrêt d'odoo20 → mise à jour du module (-u) → redémarrage → contrôle
# des logs (aucune erreur, threads cron « Alive »).
#
# Règles : seul le container odoo20 est arrêté / démarré (db20 tourne déjà ; odoo19,
# nextcloud, caddy ne sont pas concernés) ; aucun fichier de configuration modifié ; rien
# n'est supprimé (hormis le journal temporaire de la mise à jour, créé par ce script).
#
# Notes sur le VPS :
# - Le repo ~/infra/odoo20/addons/hub_odoo appartient à root : le pull passe par
#   « sudo -n git » (root, propriétaire ; pas d'exception safe.directory à ajouter).
# - « docker compose run … odoo … » passe par /entrypoint.sh de l'image odoo:20.0, qui
#   ajoute db_host / db_user / mot de passe (secret pg_pass) comme pour le service.
# - Mode threadé (workers = 0) : les threads cron écrivent « ThreadedServer.cronN: Alive ».
# - La base hub_test n'est pas mise à jour par ce script.
# - Avertissements attendus au démarrage (sans gravité) : « invalid addons directory
#   '/mnt/extra-addons' » et « unknown option 'appalach_hub_moteur_…_key_file' ».

set -euo pipefail

HOTE="vps-hub"
PILE="~/infra/odoo20"
REPO="$PILE/addons/hub_odoo"
BASE="hub"
MODULE="appalach_hub"
ATTENTE_DEMARRAGE=20
MOTIF_ERREURS="ERROR|CRITICAL|Traceback"

distant() { ssh -o BatchMode=yes "$HOTE" "$@"; }
titre() { printf '\n=== %s\n' "$*"; }

titre "0. État avant déploiement"
distant "cd $PILE && docker compose ps"
if [ -n "$(distant "sudo -n git -C $REPO status --porcelain")" ]; then
    echo "Le repo du VPS a des modifications locales : déploiement annulé." >&2
    exit 1
fi
distant "sudo -n git -C $REPO log --oneline -1"

titre "1. git pull (--ff-only)"
distant "cd $REPO && sudo -n git pull --ff-only && sudo -n git log --oneline -1"

titre "2. Arrêt d'odoo20"
distant "cd $PILE && docker compose stop odoo20"

titre "3. Mise à jour de $MODULE sur la base $BASE"
# Le journal complet reste sur le VPS le temps du filtrage, puis est supprimé.
set +e
distant "cd $PILE && journal=\$(mktemp) && docker compose run --rm odoo20 odoo -d $BASE -u $MODULE --stop-after-init > \"\$journal\" 2>&1; rc=\$?; grep -E 'WARNING|$MOTIF_ERREURS|Modules loaded|Registry loaded' \"\$journal\" | cut -c1-300; rm -f \"\$journal\"; exit \$rc"
rc_maj=$?
set -e
echo "Code retour de la mise à jour : $rc_maj"

titre "4. Redémarrage d'odoo20 et contrôle des logs"
# Toujours redémarré, même si la mise à jour a échoué (la base n'est alors pas modifiée).
debut=$(distant "date -u +%Y-%m-%dT%H:%M:%SZ")
distant "cd $PILE && docker compose start odoo20"
sleep "$ATTENTE_DEMARRAGE"
distant "cd $PILE && docker compose ps odoo20"
logs=$(distant "cd $PILE && docker compose logs --no-color --since $debut odoo20")
printf '%s\n' "$logs" | grep -E "WARNING|$MOTIF_ERREURS|Alive|HTTP service" | cut -c1-300 || true
nb_erreurs=$(printf '%s\n' "$logs" | grep -cE "$MOTIF_ERREURS" || true)
nb_cron=$(printf '%s\n' "$logs" | grep -cE "cron[0-9]+: Alive" || true)

titre "Version installée"
distant "cd $PILE && docker compose exec -T db20 psql -U odoo -d $BASE -Atc \"select name, latest_version, state from ir_module_module where name = '$MODULE'\""

titre "Résumé"
echo "Mise à jour : code $rc_maj ; erreurs dans les logs : $nb_erreurs ; threads cron Alive : $nb_cron"
if [ "$rc_maj" -ne 0 ] || [ "$nb_erreurs" -ne 0 ] || [ "$nb_cron" -eq 0 ]; then
    echo "DÉPLOIEMENT À VÉRIFIER." >&2
    exit 1
fi
echo "Déploiement OK."
