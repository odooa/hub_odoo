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
    def __init__(self, url, base, cle, company_id=None):
        self.url = (url or "").rstrip("/")
        self.entetes = {
            "Authorization": f"bearer {cle}",
            "X-Odoo-Database": base,
            "Content-Type": "application/json; charset=utf-8",
        }
        self.contexte = {"allowed_company_ids": [company_id]} if company_id else {}

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

    def comptes(self):
        return self.appel(
            "account.account", "search_read",
            domain=[["active", "=", True]], fields=["id", "code", "name", "account_type"],
            order="code",
        )

    def journaux_generaux(self):
        return self.appel(
            "account.journal", "search_read",
            domain=[["type", "=", "general"]], fields=["id", "code", "name"], order="code",
        )

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
