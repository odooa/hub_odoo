# CLAUDE.md — hub_odoo (module Odoo du hub comptable Appalach)

Contexte complet du projet : voir le CLAUDE.md du repo `hub_compta` (moteur Python).
Ce repo contient uniquement le **module Odoo du hub**, source de vérité métier.

## Cible

- **Odoo Community 20.0** (base `hub` sur https://hub.appalach.be).
- Ce repo est cloné sur le VPS dans `~/infra/odoo20/addons` (monté en `/mnt/extra-addons`).
  Chaque module est donc un dossier à la racine du repo.
- Module principal : `appalach_hub` (dépendances : `base`, `mail` ; `portal` plus tard pour
  les pages de réponse à jeton).

## Source de référence

- **Source Odoo 20 locale : `~/odoo/odoo20`** (clone de la branche `20.0`). Toute convention
  se vérifie sur un module standard de cette source (ex. `mail`, `project`), pas sur la 19.
- Le Postgres local (14) est trop ancien pour Odoo 20 (≥ 16 requis) : tests sur le VPS ou
  dans un Postgres 16 jetable.

## Conventions Odoo 20 à respecter

- **Droits d'accès : `security/ir.access.csv`** (modèle `ir.access`, `ir.model.access` et
  `ir.rule` n'existent plus). Le nom du fichier détermine le modèle chargé. Colonnes :
  `id,name,model_id,group_id/id,operation,domain` ; `model_id` = nom technique du modèle
  (`hub.dossier`) ; `operation` = sous-ensemble de `crud` (ex. `ru`) ; `domain` vide = tous
  les enregistrements. Avec un groupe : **permission** ; sans groupe : **restriction**
  (remplace les règles d'enregistrement globales).
- Groupes : `res.groups` avec `privilege_id` → `res.groups.privilege` (→ `category_id`
  `ir.module.category`), `implied_ids`, `user_ids` (plus `users`). Utilisateurs :
  `group_ids` / `all_group_ids`.
- Contraintes SQL : attributs `models.Constraint(...)` (plus `_sql_constraints`).
- Vues liste : balise `<list>` (pas `<tree>`), `view_mode` = `list,form`.
- Pas d'attribut `attrs` ni `states` : utiliser `invisible=`, `readonly=`, `required=`
  avec des expressions Python.
- Chatter : `<chatter/>` dans les vues formulaire.
- Champs `tracking=True` sur ce qui doit être historisé.
- Vérifier toute API ou convention douteuse plutôt que de supposer ; signaler les doutes.

## Modèles visés (construits par étapes)

1. `hub.dossier` : un client. Nom, code, partenaire, statut (actif / suspendu / en_reprise),
   profil d'activité (services, commerce, construction…), URL Odoo, base, version, clé API
   **chiffrée** (voir Sécurité), chemin Nextcloud, comptables (utilisateurs),
   contacts client, tâches activées.
   Les tâches activées sont pour l'instant 5 booléens (`tache_*`) ; elles deviendront un
   modèle `hub.tache` (mode, fréquence, journal, version) à une étape ultérieure.
2. `hub.fait` : faits du dossier (clé, valeur, type, note). Ex. `achats_marchandises = false`.
3. `hub.regle` : règle déclarative condition → action. Portée globale / profil / dossier.
   Conditions en champs structurés (type de pièce, préfixes de comptes, fournisseurs, montant,
   fait requis). Actions : suspendre, question_client, question_comptable, proposer_correction,
   appliquer. Textes et options des questions. État brouillon / actif. Historique (chatter).
4. `hub.question` : question liée à un dossier et à une exécution ; pièce concernée (réf. +
   lien), destinataire, texte, options, réponse client, commentaire, décision comptable,
   état (`question_client_envoyee → reponse_client → decision_comptable → appliquee`),
   jeton d'accès.
5. `hub.run` (+ étapes, actions) : exécutions du moteur, alimentées par l'API.

## Sécurité

- Groupes : « Hub / Comptable » (voit et modifie les dossiers dont il est comptable,
  via un domaine dans `ir.access.csv`) et « Hub / Gestionnaire » (tout).
- Un utilisateur technique « moteur » accède par API (clé API), droits limités au nécessaire
  (groupe technique « Hub / Moteur »).
- Clé API de l'Odoo d'un dossier : **écriture seule**, jamais stockée ni relisible en clair.
  À la saisie (gestionnaires uniquement) : test de connexion JSON-2 avec la clé en mémoire,
  puis chiffrement RSA-OAEP (SHA-256, `cryptography`) avec la clé publique du moteur, dont
  le chemin PEM est donné par l'option `appalach_hub_moteur_public_key_file` de `odoo.conf`.
  Seul le chiffré est stocké, lisible uniquement par le groupe Moteur ; la clé privée reste
  côté moteur. Le chatter trace la date et l'auteur, jamais la valeur.
- Ne pas lancer le hub avec `--log-level=debug_rpc` (journalise les paramètres RPC).

## Façon de travailler

- Avancer **étape par étape** ; une étape = un commit lisible.
- Tests Odoo (`tests/`) pour les contraintes et règles d'accès.
- Pas de données client réelles dans le repo.
