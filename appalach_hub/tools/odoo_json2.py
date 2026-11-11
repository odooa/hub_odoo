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

    def journaux(self):
        return self.appel(
            "account.journal", "search_read",
            domain=self._domaine_societe("company_id"),
            fields=["id", "code", "name", "type"], order="code",
        )

    def ecritures(self, domaine, limite, ordre="date desc, id desc"):
        """Écritures de la société retenue ; ``domaine`` est construit par le code du hub."""
        return self.appel(
            "account.move", "search_read",
            domain=list(domaine) + self._domaine_societe("company_id"),
            fields=["id", "name", "ref", "date", "journal_id", "state", "amount_total"],
            order=ordre, limit=limite,
        )

    def lignes_ecritures(self, move_ids, limite=500):
        return self.appel(
            "account.move.line", "search_read",
            domain=[["move_id", "in", list(move_ids)]],
            fields=["move_id", "account_id", "name", "debit", "credit", "partner_id"],
            order="move_id, id", limit=limite,
        )

    def partenaires(self, domaine, limite):
        return self.appel(
            "res.partner", "search_read",
            domain=list(domaine) + ["|", ["company_id", "=", False]]
                   + (self._domaine_societe("company_id") or [["company_id", "!=", False]]),
            fields=["id", "name", "vat", "ref"], order="name", limit=limite,
        )

    # ------------------------------------------------------------------
    # Documents (Enterprise 19, module documents) : documents.document sert à la fois de
    # dossier (type folder) et de fichier (type binary) ; raccourcis exclus.
    # ------------------------------------------------------------------

    CHAMPS_PIECE = ["id", "name", "folder_id", "mimetype", "file_size", "tag_ids", "res_model",
                    "res_id", "create_date", "access_url", "attachment_id"]

    def _domaine_societe_documents(self):
        if not self.company_id:
            return []
        return ["|", ["company_id", "=", False], ["company_id", "=", self.company_id]]

    def dossiers_documents(self):
        return self.appel(
            "documents.document", "search_read",
            domain=[["type", "=", "folder"], ["shortcut_document_id", "=", False]]
                   + self._domaine_societe_documents(),
            fields=["id", "name", "folder_id", "alias_email"], order="name", limit=2000,
        )

    def etiquettes_documents(self):
        return self.appel("documents.tag", "search_read", domain=[],
                          fields=["id", "name"], order="sequence, name", limit=2000)

    def pieces(self, domaine, limite):
        return self.appel(
            "documents.document", "search_read",
            domain=[["type", "=", "binary"], ["shortcut_document_id", "=", False]] + list(domaine)
                   + self._domaine_societe_documents(),
            fields=self.CHAMPS_PIECE, order="create_date desc, id desc", limit=limite,
        )

    def piece(self, document_id):
        trouvees = self.pieces([["id", "=", int(document_id)]], 1)
        return trouvees[0] if trouvees else None

    def contenu_piece(self, document_id):
        """Contenu du document en base64 (champ datas, lié à la pièce jointe)."""
        lu = self.appel("documents.document", "read", ids=[int(document_id)], fields=["datas"])
        if not lu or not lu[0].get("datas"):
            raise OdooClientErreur(f"Document {document_id} : contenu illisible.")
        return lu[0]["datas"]

    def lier_document(self, document_id, attachment_id, move_id):
        """Rattache la pièce existante à l'écriture, sans la dupliquer : l'inverse de
        res_model/res_id déplace la même pièce jointe sur l'écriture ; le document reste dans
        Documents."""
        self.appel("documents.document", "write", ids=[int(document_id)],
                   vals={"res_model": "account.move", "res_id": int(move_id)})
        if attachment_id:
            self.appel("account.move", "write", ids=[int(move_id)],
                       vals={"message_main_attachment_id": int(attachment_id)})

    def marquer_document(self, document_id, etiquette_id=None, dossier_id=None):
        vals = {}
        if etiquette_id:
            vals["tag_ids"] = [[4, int(etiquette_id)]]
        if dossier_id:
            vals["folder_id"] = int(dossier_id)
        if vals:
            self.appel("documents.document", "write", ids=[int(document_id)], vals=vals)

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
