"""Client de l'API JSON-2 d'un Odoo client (Odoo 19+), par requests.

POST /json/2/<modèle>/<méthode> ; corps = arguments nommés (+ ids, context) ;
en-têtes Authorization: bearer <clé> et X-Odoo-Database.
"""

import base64
import json

import requests

TIMEOUT = 30


class OdooClientErreur(Exception):
    """Erreur présentable à l'utilisateur ; ne contient jamais la clé."""


class OdooJson2:
    def __init__(self, url, base, cle):
        self.url = (url or "").rstrip("/")
        self.entetes = {
            "Authorization": f"bearer {cle}",
            "X-Odoo-Database": base,
            "Content-Type": "application/json; charset=utf-8",
        }
        self.company_id = None
        self.societe_nom = None
        self.contexte = {}

    def utiliser_societe(self, company_id, nom=None):
        """Toutes les lectures et écritures suivantes se font dans le contexte de cette société
        (account.account.code dépend de la société en Odoo 19+)."""
        self.company_id = company_id
        self.societe_nom = nom
        self.contexte = {"allowed_company_ids": [company_id]}

    def fixer_societe(self, company_id=None):
        """Société imposée (dossier) ou, à défaut, société de l'utilisateur API.

        Renvoie (id, nom). context_get renvoie l'uid de l'utilisateur de la clé API.
        """
        if company_id:
            info = self.appel("res.company", "read", ids=[company_id], fields=["name"])
            if not info:
                raise OdooClientErreur(f"Société {company_id} introuvable dans l'Odoo du dossier.")
            nom = info[0]["name"]
        else:
            uid = (self.appel("res.users", "context_get") or {}).get("uid")
            if not uid:
                raise OdooClientErreur("Odoo du dossier : utilisateur API introuvable (context_get).")
            utilisateur = self.appel("res.users", "read", ids=[uid], fields=["company_id"])
            if not utilisateur or not utilisateur[0].get("company_id"):
                raise OdooClientErreur("Odoo du dossier : société de l'utilisateur API introuvable.")
            company_id, nom = utilisateur[0]["company_id"][:2]
        self.utiliser_societe(company_id, nom)
        return company_id, nom

    def appel(self, modele, methode, **arguments):
        if self.contexte:
            arguments.setdefault("context", self.contexte)
        try:
            reponse = requests.post(
                f"{self.url}/json/2/{modele}/{methode}",
                headers=self.entetes, data=json.dumps(arguments), timeout=TIMEOUT,
            )
        except requests.exceptions.RequestException:
            raise OdooClientErreur("Odoo du dossier injoignable.") from None
        if reponse.status_code == 401:
            raise OdooClientErreur("Odoo du dossier : clé API refusée.")
        if reponse.status_code != 200:
            try:
                detail = reponse.json()
                message = f"{detail.get('name', '')} : {detail.get('message', '')}"
            except ValueError:
                message = reponse.text[:300]
            raise OdooClientErreur(
                f"Odoo du dossier, {modele}.{methode} : HTTP {reponse.status_code} — {message}"
            )
        return reponse.json()

    def _domaine_societe(self, champ):
        """Filtre sur la société retenue ; ``champ`` en *_ids (many2many) ou many2one."""
        if not self.company_id:
            return []
        if champ.endswith("_ids"):
            return [[champ, "in", [self.company_id]]]
        return [[champ, "=", self.company_id]]

    def comptes(self):
        return self.appel(
            "account.account", "search_read",
            domain=[["active", "=", True]] + self._domaine_societe("company_ids"),
            fields=["id", "code", "name", "account_type"], order="code",
        )

    def journaux_generaux(self):
        return self.appel(
            "account.journal", "search_read",
            domain=[["type", "=", "general"]] + self._domaine_societe("company_id"),
            fields=["id", "code", "name"], order="code",
        )

    def historique_salaires(self, limite=6):
        """Dernières OD de salaires comptabilisées : écritures des journaux généraux touchant
        un compte 62* ou dont la référence contient « salaire ». Renvoie (écritures, lignes)."""
        ecritures = self.appel(
            "account.move", "search_read",
            domain=[["move_type", "=", "entry"], ["state", "=", "posted"],
                    ["journal_id.type", "=", "general"]] + self._domaine_societe("company_id") + [
                    "|", ["line_ids.account_id.code", "=like", "62%"], ["ref", "ilike", "salaire"]],
            fields=["id", "name", "ref", "date", "journal_id"],
            order="date desc, id desc", limit=limite,
        )
        if not ecritures:
            return [], []
        lignes = self.appel(
            "account.move.line", "search_read",
            domain=[["move_id", "in", [e["id"] for e in ecritures]]],
            fields=["move_id", "account_id", "name", "debit", "credit"], order="move_id, id",
        )
        return ecritures, lignes

    def od_existante(self, reference):
        # Une OD annulée ne bloque pas ; brouillon ou comptabilisée bloque.
        return self.appel(
            "account.move", "search_count",
            domain=[["ref", "=", reference], ["move_type", "=", "entry"],
                    ["state", "in", ["draft", "posted"]]],
        )

    def creer_od_brouillon(self, valeurs):
        """Crée l'écriture ; aucun appel de comptabilisation (action_post) n'existe ici."""
        return self.appel("account.move", "create", vals_list=[valeurs])[0]

    def joindre_pdf(self, move_id, nom, pdf):
        return self.appel("ir.attachment", "create", vals_list=[{
            "name": nom,
            "datas": base64.b64encode(pdf).decode("ascii"),
            "res_model": "account.move",
            "res_id": move_id,
            "mimetype": "application/pdf",
        }])[0]

    def lien(self, modele, record_id):
        return f"{self.url}/odoo/{modele}/{record_id}"
