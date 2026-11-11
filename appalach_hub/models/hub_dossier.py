from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..tools import cle_api
from ..tools.odoo_json2 import OdooJson2

GROUPE_GESTIONNAIRE = "appalach_hub.group_hub_gestionnaire"
GROUPE_MOTEUR = "appalach_hub.group_hub_moteur"


def _domaine_comptables(self):
    groupe = self.env.ref("appalach_hub.group_hub_comptable", raise_if_not_found=False)
    return [("all_group_ids", "in", groupe.ids if groupe else [])]


class HubDossier(models.Model):
    _name = "hub.dossier"
    _description = "Dossier client du hub"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "code"

    name = fields.Char("Nom", required=True, tracking=True)
    code = fields.Char(
        "Code", required=True, tracking=True, help="Identifiant court et stable, ex. APPALACH."
    )
    active = fields.Boolean(default=True, tracking=True)
    partner_id = fields.Many2one("res.partner", "Société cliente", tracking=True)
    statut = fields.Selection(
        [("actif", "Actif"), ("suspendu", "Suspendu"), ("en_reprise", "En reprise")],
        default="actif",
        required=True,
        tracking=True,
    )
    profil_activite = fields.Selection(
        [
            ("services", "Services"),
            ("commerce", "Commerce"),
            ("construction", "Construction"),
            ("autre", "Autre"),
        ],
        "Profil d'activité",
        tracking=True,
    )

    # Odoo du client
    odoo_url = fields.Char("URL Odoo", tracking=True, help="Ex. https://client.odoo.com")
    odoo_base = fields.Char("Base", tracking=True)
    odoo_version = fields.Char("Version", tracking=True, help="Ex. 19.0")
    odoo_company_id = fields.Integer(
        "ID de la société", tracking=True,
        help="Facultatif : société à utiliser si la base du client en contient plusieurs.",
    )

    # Clé API : écriture seule, voir _definir_cle_api. Le chiffré n'est lisible que
    # par le groupe Moteur ; la clé en clair n'est jamais stockée.
    cle_api_chiffree = fields.Text(
        "Clé API chiffrée", groups=GROUPE_MOTEUR, copy=False, readonly=True
    )
    cle_api_empreinte = fields.Char(
        "Empreinte de la clé publique", groups=GROUPE_MOTEUR, copy=False, readonly=True
    )
    cle_api_definie = fields.Boolean("Clé API définie", compute="_compute_cle_api_definie")
    cle_api_date = fields.Datetime("Clé définie le", readonly=True, copy=False, tracking=True)
    cle_api_user_id = fields.Many2one(
        "res.users", "Clé définie par", readonly=True, copy=False, tracking=True
    )

    nextcloud_path = fields.Char(
        "Chemin Nextcloud", tracking=True, help="Ex. /clients/appalach"
    )
    source_pieces = fields.Selection(
        [("nextcloud", "Nextcloud"), ("documents", "Documents Odoo")], "Source des pièces",
        default="nextcloud", required=True, tracking=True,
        help="Nextcloud : clients hors écosystème Odoo. Documents Odoo : module Documents "
             "(Enterprise) de l'Odoo du client, lu et mis à jour par l'API JSON-2.",
    )
    # Configuration Documents : choisie dans des listes lues chez le client (assistant
    # « Configurer Documents »), jamais saisie à la main.
    documents_dossier_ids = fields.One2many(
        "hub.dossier.documents", "dossier_id", "Dossiers Documents par tâche", readonly=True
    )
    documents_traitement = fields.Selection(
        [("etiquette", "Ajouter une étiquette"), ("dossier", "Déplacer dans un dossier")],
        "Pièce traitée", readonly=True, tracking=True,
    )
    documents_etiquette_traite_id = fields.Integer("ID de l'étiquette « traité »", readonly=True)
    documents_etiquette_traite_nom = fields.Char("Étiquette « traité »", readonly=True, tracking=True)
    documents_dossier_traite_id = fields.Integer("ID du dossier « traité »", readonly=True)
    documents_dossier_traite_nom = fields.Char("Dossier « traité »", readonly=True, tracking=True)
    comptable_ids = fields.Many2many(
        "res.users",
        "hub_dossier_comptable_rel",
        "dossier_id",
        "user_id",
        "Comptables",
        domain=_domaine_comptables,
        tracking=True,
    )
    contact_ids = fields.Many2many(
        "res.partner",
        "hub_dossier_contact_rel",
        "dossier_id",
        "partner_id",
        "Contacts client",
        help="Destinataires des questions au client.",
    )

    # Tâches activées (deviendront un modèle hub.tache)
    tache_reprise = fields.Boolean("Reprise", tracking=True)
    tache_tva = fields.Boolean("Déclaration TVA", tracking=True)
    tache_controle_factures = fields.Boolean("Contrôle des factures", tracking=True)
    tache_lettrage = fields.Boolean("Lettrage bancaire", tracking=True)
    tache_od_salaires = fields.Boolean("OD de salaires", tracking=True)

    od_salaires_journal = fields.Char(
        "Journal des OD de salaires", tracking=True,
        help="Code du journal général mémorisé lors de la dernière OD de salaires.",
    )
    anonymiser_historique = fields.Boolean(
        "Anonymiser les libellés de l'historique", default=False, tracking=True,
        help="OD de salaires : n'envoyer à Claude que le libellé du compte des lignes "
             "d'historique, pas le libellé de ligne (qui peut contenir des noms de travailleurs).",
    )
    mapping_ids = fields.One2many("hub.mapping", "dossier_id", "Mappings")
    run_ids = fields.One2many("hub.run", "dossier_id", "Exécutions")
    run_count = fields.Integer("Nombre d'exécutions", compute="_compute_run_count")
    fait_ids = fields.One2many("hub.fait", "dossier_id", "Faits")
    fait_count = fields.Integer("Nombre de faits", compute="_compute_fait_count")
    note = fields.Html("Notes")

    _code_uniq = models.Constraint("UNIQUE (code)", "Le code du dossier doit être unique.")

    _CHAMPS_CLE_API = {"cle_api_chiffree", "cle_api_empreinte", "cle_api_date", "cle_api_user_id"}
    _CHAMPS_DOCUMENTS = {
        "documents_dossier_ids", "documents_traitement", "documents_etiquette_traite_id",
        "documents_etiquette_traite_nom", "documents_dossier_traite_id",
        "documents_dossier_traite_nom",
    }

    def write(self, vals):
        # Les champs de la clé ne se modifient que par _definir_cle_api (en sudo).
        if not self.env.su and self._CHAMPS_CLE_API & set(vals):
            raise AccessError(_("La clé API se modifie uniquement par le bouton dédié."))
        if not self.env.su and self._CHAMPS_DOCUMENTS & set(vals):
            raise AccessError(_("La configuration Documents se modifie uniquement par "
                                "l'assistant « Configurer Documents »."))
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(self._CHAMPS_CLE_API & set(vals) for vals in vals_list):
            raise AccessError(_("La clé API se définit uniquement par le bouton dédié."))
        if not self.env.su and any(self._CHAMPS_DOCUMENTS & set(vals) for vals in vals_list):
            raise AccessError(_("La configuration Documents se définit uniquement par "
                                "l'assistant « Configurer Documents »."))
        return super().create(vals_list)

    @api.depends("cle_api_chiffree")
    def _compute_cle_api_definie(self):
        for dossier, dossier_su in zip(self, self.sudo()):
            dossier.cle_api_definie = bool(dossier_su.cle_api_chiffree)

    @api.depends("fait_ids")
    def _compute_fait_count(self):
        donnees = self.env["hub.fait"]._read_group(
            [("dossier_id", "in", self.ids)], ["dossier_id"], ["__count"]
        )
        comptes = {dossier.id: nombre for dossier, nombre in donnees}
        for dossier in self:
            dossier.fait_count = comptes.get(dossier.id, 0)

    @api.depends("run_ids")
    def _compute_run_count(self):
        donnees = self.env["hub.run"]._read_group(
            [("dossier_id", "in", self.ids)], ["dossier_id"], ["__count"]
        )
        comptes = {dossier.id: nombre for dossier, nombre in donnees}
        for dossier in self:
            dossier.run_count = comptes.get(dossier.id, 0)

    def action_voir_runs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Exécutions"),
            "res_model": "hub.run",
            "view_mode": "list,form",
            "domain": [("dossier_id", "=", self.id)],
        }

    def action_voir_faits(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Faits"),
            "res_model": "hub.fait",
            "view_mode": "list,form",
            "domain": [("dossier_id", "=", self.id)],
            "context": {"default_dossier_id": self.id},
        }

    def action_ouvrir_cle_wizard(self):
        self.ensure_one()
        self._verifier_gestionnaire()
        return {
            "type": "ir.actions.act_window",
            "name": _("Clé API du dossier"),
            "res_model": "hub.dossier.cle.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_dossier_id": self.id},
        }

    # ------------------------------------------------------------------
    # Clé API
    # ------------------------------------------------------------------

    def _verifier_gestionnaire(self, message=None):
        if not self.env.su and not self.env.user.has_group(GROUPE_GESTIONNAIRE):
            raise AccessError(message or _("Seul un gestionnaire du hub peut définir la clé API."))

    def _definir_cle_api(self, cle):
        """Chiffre la clé, la teste sur l'Odoo du dossier et ne stocke que le chiffré.

        La clé en clair ne quitte pas cette méthode : ni stockage, ni journal, ni message.
        """
        self.ensure_one()
        self._verifier_gestionnaire()
        cle = (cle or "").strip()
        if not cle:
            raise UserError(_("Saisissez la clé API."))
        if not self.odoo_url or not self.odoo_base:
            raise UserError(
                _("Renseignez l'URL et la base de l'Odoo du dossier avant de définir la clé.")
            )
        remplacement = self.cle_api_definie
        try:
            # Chiffrer d'abord : une clé publique absente bloque avant tout appel réseau.
            chiffre, empreinte = self.env["hub.secrets"]._chiffrer(cle)
            cle_api.tester_connexion_json2(self.odoo_url, self.odoo_base, cle)
        except cle_api.CleApiErreur as erreur:
            raise UserError(str(erreur)) from None
        finally:
            del cle
        self.sudo().write(
            {
                "cle_api_chiffree": chiffre,
                "cle_api_empreinte": empreinte,
                "cle_api_date": fields.Datetime.now(),
                "cle_api_user_id": self.env.user.id,
            }
        )
        if remplacement:
            corps = _("Clé API remplacée — connexion testée sur la base %s.", self.odoo_base)
        else:
            corps = _("Clé API définie — connexion testée sur la base %s.", self.odoo_base)
        self.message_post(body=corps)

    def _cle_api_en_clair(self):
        """Clé API déchiffrée, pour un appel immédiat. Ne jamais la stocker ni l'afficher."""
        self.ensure_one()
        dossier = self.sudo()
        if not dossier.cle_api_chiffree:
            raise UserError(_("Aucune clé API définie pour le dossier %s.", self.code))
        return self.env["hub.secrets"]._dechiffrer(
            dossier.cle_api_chiffree, dossier.cle_api_empreinte
        )

    def _client_odoo(self, company_id=None):
        """Client JSON-2 de l'Odoo du dossier, dans le contexte d'une société.

        company_id : société déjà résolue (pas d'appel) ; sinon odoo_company_id du dossier,
        sinon société de l'utilisateur API (lue dans l'Odoo du client).
        """
        self.ensure_one()
        if not self.odoo_url or not self.odoo_base:
            raise UserError(_("URL ou base de l'Odoo du dossier non renseignée."))
        client = OdooJson2(self.odoo_url, self.odoo_base, self._cle_api_en_clair())
        if company_id:
            client.utiliser_societe(company_id)
        else:
            client.fixer_societe(self.odoo_company_id or None)
        return client

    # ------------------------------------------------------------------
    # Documents (Odoo du client)
    # ------------------------------------------------------------------

    def action_configurer_documents(self):
        self.ensure_one()
        self._verifier_gestionnaire(_("Seul un gestionnaire du hub configure Documents."))
        assistant = self.env["hub.documents.config.wizard"].create({"dossier_id": self.id})
        assistant._charger()
        return {
            "type": "ir.actions.act_window",
            "name": _("Configurer Documents"),
            "res_model": "hub.documents.config.wizard",
            "res_id": assistant.id,
            "view_mode": "form",
            "target": "new",
        }

    def _config_documents(self, tache):
        """Configuration Documents de la tâche, pour les outils de l'agent."""
        self.ensure_one()
        ligne = self.documents_dossier_ids.filtered(lambda l: l.tache == tache)[:1]
        if not ligne:
            raise UserError(_("Dossier %(dossier)s : aucun dossier Documents configuré pour la "
                              "tâche (bouton « Configurer Documents »).", dossier=self.code))
        return {
            "dossier_id": ligne.dossier_client_id,
            "dossier_chemin": ligne.dossier_chemin,
            "etiquette_traite_id": self.documents_etiquette_traite_id
            if self.documents_traitement == "etiquette" else None,
            "dossier_traite_id": self.documents_dossier_traite_id
            if self.documents_traitement == "dossier" else None,
        }

    # ------------------------------------------------------------------
    # Traces et OD de salaires
    # ------------------------------------------------------------------

    def _tracer(self, corps):
        """Message dans le chatter du dossier (jamais de secret ni de détail de paie)."""
        self.ensure_one()
        self.message_post(body=corps)

    def action_preparer_od_salaires(self):
        self.ensure_one()
        assistant = self.env["hub.od.salaires.wizard"].create({
            "dossier_id": self.id,
            "source": "documents" if self.source_pieces == "documents" else "nextcloud",
        })
        assistant._charger_pieces()
        return {
            "type": "ir.actions.act_window",
            "name": _("OD de salaires"),
            "res_model": "hub.od.salaires.wizard",
            "res_id": assistant.id,
            "view_mode": "form",
            "target": "new",
        }


class HubDossierDocuments(models.Model):
    """Dossier Documents de l'Odoo du client utilisé par une tâche (écrit par l'assistant)."""

    _name = "hub.dossier.documents"
    _description = "Dossier Documents par tâche"
    _order = "dossier_id, tache"

    dossier_id = fields.Many2one("hub.dossier", required=True, ondelete="cascade", index=True)
    tache = fields.Selection([("od_salaires", "OD de salaires")], "Tâche", required=True)
    dossier_client_id = fields.Integer("ID du dossier chez le client", required=True)
    dossier_chemin = fields.Char("Dossier Documents", required=True)
    alias_email = fields.Char("Alias")

    _dossier_tache_uniq = models.Constraint(
        "UNIQUE (dossier_id, tache)", "Un seul dossier Documents par tâche."
    )
