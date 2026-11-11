from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.res_config_settings import MODELE_PAR_DEFAUT, MODELES
from ..tools import od_salaires as ods
from ..tools.nextcloud import Nextcloud, NextcloudErreur


class HubOdSalairesWizard(models.TransientModel):
    """Lancement de l'agent « OD de salaires » : choix du PDF et du modèle.

    Le travail se fait en arrière-plan (cron) dans une exécution hub.run, où la proposition
    est ensuite validée par un comptable.
    """

    _name = "hub.od.salaires.wizard"
    _description = "Lancement de l'agent OD de salaires"

    dossier_id = fields.Many2one("hub.dossier", required=True, ondelete="cascade", readonly=True)
    source = fields.Selection(
        [("nextcloud", "Dernier PDF sur Nextcloud"), ("fichier", "Fichier déposé")],
        default="nextcloud", required=True,
    )
    dossier_nextcloud = fields.Char("Dossier Nextcloud", compute="_compute_dossier_nextcloud")
    pdf = fields.Binary("PDF", attachment=False)
    pdf_nom = fields.Char("Nom du PDF")
    periode = fields.Char("Période (facultatif)", help="AAAA-MM ; sinon celle du PDF.")
    modele = fields.Selection("_selection_modeles", "Modèle Claude", required=True,
                              default=lambda self: self._modele_par_defaut())

    @api.model
    def _modele_par_defaut(self):
        return self.env["hub.secrets"]._parametre("claude_modele") or MODELE_PAR_DEFAUT

    @api.model
    def _selection_modeles(self):
        modeles = list(MODELES)
        parametre = self._modele_par_defaut()
        if parametre not in dict(modeles):
            modeles.insert(0, (parametre, parametre))
        return modeles

    @api.depends("dossier_id.nextcloud_path")
    def _compute_dossier_nextcloud(self):
        for assistant in self:
            racine = (assistant.dossier_id.nextcloud_path or "").strip("/")
            assistant.dossier_nextcloud = f"{racine}/salaires" if racine else False

    def _charger_pdf(self):
        if self.source == "fichier":
            if not self.pdf:
                raise UserError(_("Déposez un fichier PDF."))
            # Odoo 20 : un champ Binary se lit comme BinaryValue (octets bruts via .content).
            return self.pdf.content, self.pdf_nom or "salaires.pdf"
        if not self.dossier_nextcloud:
            raise UserError(_("Chemin Nextcloud du dossier non renseigné."))
        secrets = self.env["hub.secrets"]
        url, utilisateur = secrets._parametre("nextcloud_url"), secrets._parametre("nextcloud_user")
        if not url or not utilisateur:
            raise UserError(_("URL ou utilisateur Nextcloud absent des paramètres Hub compta."))
        nextcloud = Nextcloud(url, utilisateur, secrets._secret_global("nextcloud_password"))
        try:
            fichier = nextcloud.pdf_le_plus_recent(self.dossier_nextcloud)
            return nextcloud.telecharger(fichier), fichier.nom
        except NextcloudErreur as erreur:
            raise UserError(str(erreur)) from None

    def action_lancer(self):
        self.ensure_one()
        if self.periode:
            try:
                ods.verifier_periode(self.periode)
            except ods.OdSalairesErreur as erreur:
                raise UserError(str(erreur)) from None
        contenu, nom = self._charger_pdf()
        run = self.env["hub.run"]._lancer(self.dossier_id, contenu, nom, self.modele, self.periode)
        return {
            "type": "ir.actions.act_window",
            "res_model": "hub.run",
            "res_id": run.id,
            "view_mode": "form",
            "target": "current",
            "name": _("OD de salaires"),
        }
