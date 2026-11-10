from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import config

from ..tools import cle_api

OPTION_CLE_PUBLIQUE = "appalach_hub_moteur_public_key_file"
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

    fait_ids = fields.One2many("hub.fait", "dossier_id", "Faits")
    fait_count = fields.Integer("Nombre de faits", compute="_compute_fait_count")
    note = fields.Html("Notes")

    _code_uniq = models.Constraint("UNIQUE (code)", "Le code du dossier doit être unique.")

    _CHAMPS_CLE_API = {"cle_api_chiffree", "cle_api_empreinte", "cle_api_date", "cle_api_user_id"}

    def write(self, vals):
        # Les champs de la clé ne se modifient que par _definir_cle_api (en sudo).
        if not self.env.su and self._CHAMPS_CLE_API & set(vals):
            raise AccessError(_("La clé API se modifie uniquement par le bouton dédié."))
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(self._CHAMPS_CLE_API & set(vals) for vals in vals_list):
            raise AccessError(_("La clé API se définit uniquement par le bouton dédié."))
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

    def _verifier_gestionnaire(self):
        if not self.env.su and not self.env.user.has_group(GROUPE_GESTIONNAIRE):
            raise AccessError(_("Seul un gestionnaire du hub peut définir la clé API."))

    @api.model
    def _lire_cle_publique_moteur(self):
        """Renvoie la clé publique du moteur (PEM, bytes) déclarée dans odoo.conf."""
        chemin = config.get(OPTION_CLE_PUBLIQUE)
        if not chemin:
            raise UserError(
                _(
                    "Clé publique du moteur non configurée : ajoutez l'option %s "
                    "dans odoo.conf.",
                    OPTION_CLE_PUBLIQUE,
                )
            )
        try:
            with open(chemin, "rb") as fichier:
                return fichier.read()
        except OSError:
            raise UserError(
                _("Fichier de clé publique du moteur illisible : %s", chemin)
            ) from None

    def _definir_cle_api(self, cle):
        """Teste la clé sur l'Odoo du dossier, la chiffre et ne stocke que le chiffré.

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
            cle_publique = cle_api.charger_cle_publique(self._lire_cle_publique_moteur())
            cle_api.tester_connexion_json2(self.odoo_url, self.odoo_base, cle)
            chiffre = cle_api.chiffrer_rsa_oaep(cle_publique, cle)
        except cle_api.CleApiErreur as erreur:
            raise UserError(str(erreur)) from None
        finally:
            del cle
        self.sudo().write(
            {
                "cle_api_chiffree": chiffre,
                "cle_api_empreinte": cle_api.empreinte_cle_publique(cle_publique),
                "cle_api_date": fields.Datetime.now(),
                "cle_api_user_id": self.env.user.id,
            }
        )
        if remplacement:
            corps = _("Clé API remplacée — connexion testée sur la base %s.", self.odoo_base)
        else:
            corps = _("Clé API définie — connexion testée sur la base %s.", self.odoo_base)
        self.message_post(body=corps)
