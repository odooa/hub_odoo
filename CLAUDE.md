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
  dans un Postgres 16 jetable (`docker run --rm -p 127.0.0.1:55432:5432 postgres:16`).

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
- Paramètres système : `ir.config_parameter.get_str/set_str` (et `get_bool/int/float`) ;
  `get_param` / `set_param` n'existent plus.
- Champs `Binary` : lecture = `BinaryValue` (octets via `.content`) ; écriture = base64 en
  `str` (des `bytes` lèvent une erreur).
- `registry.cursor()` ouvre toujours une vraie connexion, même pendant les tests : ne pas
  s'en servir pour « survivre » à une erreur ; utiliser un point de sauvegarde
  (`cr.savepoint()`) et ne pas relancer l'exception.
- Actions de fenêtre : pas de `target="inline"` (valeurs : current, new, fullscreen, main).
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
6. `hub.mapping` : libellé de rubrique → code et libellé de compte de l'Odoo client, par
   dossier et par tâche (`od_salaires` pour l'instant). Unicité sur le libellé normalisé.

## OD de salaires dans le hub (POC)

- Pour le POC, l'OD de salaires tourne **dans le module**, sans moteur externe : assistant
  « Préparer l'OD de salaires » sur le dossier (PDF Nextcloud ou déposé → extraction par
  Claude → comptes proposés : mapping d'abord, Claude ensuite → contrôles → OD en
  **brouillon** avec PDF joint). Jamais de comptabilisation ; refus si la réf existe déjà.
- Appels HTTP par `requests` uniquement (aucun SDK dans le conteneur) : API Claude
  (`anthropic-version` = valeur du SDK Python officiel, vérifiée dans sa source), Nextcloud
  WebDAV, JSON-2 de l'Odoo client. Pas de repli automatique si Claude refuse.
- Chaque exécution (extraction, création, échec) est tracée dans le chatter du dossier, sans
  secret ni détail de paie ; le PDF n'est pas conservé dans le hub.
- Logique reprise de `hub_compta/prototypes/od_salaires.py`, conservé comme référence.

## Sécurité

- Groupes : « Hub / Comptable » (voit et modifie les dossiers dont il est comptable,
  via un domaine dans `ir.access.csv`) et « Hub / Gestionnaire » (tout).
- Un utilisateur technique « moteur » accède par API (clé API), droits limités au nécessaire
  (groupe technique « Hub / Moteur »).
- Secrets **en écriture seule**, jamais stockés ni relisibles en clair : clé API de l'Odoo
  d'un dossier (gestionnaires), clé API Anthropic et mot de passe d'application Nextcloud
  (paramètres « Hub compta », administrateurs). À la saisie : test avec la valeur en
  mémoire, puis chiffrement RSA-OAEP (SHA-256, `cryptography`) avec la clé publique dont le
  chemin PEM est donné par `appalach_hub_moteur_public_key_file` dans `odoo.conf`. Seul le
  chiffré est stocké (champ du dossier lisible par le groupe Moteur ; paramètres système pour
  les secrets globaux). Date et auteur tracés, jamais la valeur.
- **POC : la clé privée est sur le serveur du hub** (`appalach_hub_moteur_private_key_file`
  dans `odoo.conf`), lue à chaque déchiffrement par le code des services (`hub.secrets`).
  Décision assumée : le chiffrement protège contre une fuite de la base seule (dump,
  sauvegarde), **pas** contre un accès au serveur, qui détient base et clé privée. À revoir
  quand le moteur externe reprendra les traitements (clé privée hors du hub).
- Ne pas lancer le hub avec `--log-level=debug_rpc` (journalise les paramètres RPC).

## Façon de travailler

- Avancer **étape par étape** ; une étape = un commit lisible.
- Tests Odoo (`tests/`) pour les contraintes et règles d'accès.
- Pas de données client réelles dans le repo.
