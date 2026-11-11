from datetime import timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.res_config_settings import MODELE_PAR_DEFAUT, MODELES
from ..tools import od_salaires as ods
from ..tools.agent_outils import TYPES_PIECES
from ..tools.nextcloud import Nextcloud, NextcloudErreur
from ..tools.odoo_json2 import OdooClientErreur

LIMITE_PIECES = 50


class HubDocumentsCopiePiece(models.TransientModel):
    _name = "hub.documents.copie.piece"
    _inherit = ["hub.copie.client"]
    _description = "Pièce Documents de l'Odoo client (choix)"
    _order = "date desc, id desc"

    wizard_id = fields.Many2one("hub.od.salaires.wizard", required=True, ondelete="cascade")
    date = fields.Datetime()

    @api.depends("name", "date")
    def _compute_display_name(self):
        for piece in self:
            date = fields.Date.to_string(piece.date) if piece.date else ""
            piece.display_name = f"{piece.name} ({date})" if date else piece.name


class HubOdSalairesWizard(models.TransientModel):
    """Lancement de l'agent « OD de salaires » : source de la pièce et modèle.

    Le travail se fait en arrière-plan (cron) dans une exécution hub.run, où la proposition
    est ensuite validée par un comptable.
    """

    _name = "hub.od.salaires.wizard"
    _description = "Lancement de l'agent OD de salaires"

    dossier_id = fields.Many2one("hub.dossier", required=True, ondelete="cascade", readonly=True)
    source_dossier = fields.Selection(related="dossier_id.source_pieces")
    source = fields.Selection(
        [("nextcloud", "Dernier PDF sur Nextcloud"), ("documents", "Pièces Documents de l'Odoo client"),
         ("fichier", "Fichier déposé")],
        default="nextcloud", required=True,
    )
    dossier_nextcloud = fields.Char("Dossier Nextcloud", compute="_compute_dossier_nextcloud")
    dossier_documents = fields.Char("Dossier Documents", compute="_compute_dossier_documents")
    pdf = fields.Binary("PDF", attachment=False)
    pdf_nom = fields.Char("Nom du PDF")
    piece_copie_ids = fields.One2many("hub.documents.copie.piece", "wizard_id")
    piece_imposee_id = fields.Many2one(
        "hub.documents.copie.piece", "Pièce imposée (facultatif)",
        domain="[('wizard_id', '=', id)]",
        help="Vide : l'agent choisit la pièce de la période dans le dossier Documents.",
    )
    avertissement = fields.Char(readonly=True)
    periode = fields.Char("Période (facultatif)", help="AAAA-MM ; sinon celle de la pièce.")
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

    @api.depends("dossier_id.documents_dossier_ids")
    def _compute_dossier_documents(self):
        for assistant in self:
            ligne = assistant.dossier_id.documents_dossier_ids.filtered(
                lambda l: l.tache == ods.TACHE)[:1]
            assistant.dossier_documents = ligne.dossier_chemin or False

    def _charger_pieces(self):
        """Pièces non traitées du dossier Documents, pour le choix facultatif d'une pièce
        imposée. Une erreur de lecture n'empêche pas d'ouvrir l'assistant."""
        self.ensure_one()
        if self.dossier_id.source_pieces != "documents":
            return
        try:
            config = self.dossier_id._config_documents(ods.TACHE)
            domaine = [["folder_id", "=", config["dossier_id"]],
                       ["mimetype", "in", list(TYPES_PIECES)]]
            if config["etiquette_traite_id"]:
                domaine.append(["tag_ids", "not in", [config["etiquette_traite_id"]]])
            pieces = self.dossier_id._client_odoo().pieces(domaine, LIMITE_PIECES)
        except (UserError, OdooClientErreur) as erreur:
            self.avertissement = _("Liste des pièces indisponible : %s", erreur.args[0])
            return
        self.env["hub.documents.copie.piece"]._importer([{
            "wizard_id": self.id, "client_id": p["id"], "name": p["name"],
            "date": p.get("create_date") or False,
        } for p in pieces if p.get("res_model") != "account.move"])

    def _charger_pdf(self):
        """Renvoie (contenu, nom, date, lien) pour les sources Nextcloud et fichier."""
        if self.source == "fichier":
            if not self.pdf:
                raise UserError(_("Déposez un fichier PDF."))
            # Odoo 20 : un champ Binary se lit comme BinaryValue (octets bruts via .content).
            return self.pdf.content, self.pdf_nom or "salaires.pdf", fields.Datetime.now(), False
        if not self.dossier_nextcloud:
            raise UserError(_("Chemin Nextcloud du dossier non renseigné."))
        secrets = self.env["hub.secrets"]
        url, utilisateur = secrets._parametre("nextcloud_url"), secrets._parametre("nextcloud_user")
        if not url or not utilisateur:
            raise UserError(_("URL ou utilisateur Nextcloud absent des paramètres Hub compta."))
        nextcloud = Nextcloud(url, utilisateur, secrets._secret_global("nextcloud_password"))
        try:
            fichier = nextcloud.pdf_le_plus_recent(self.dossier_nextcloud)
            contenu = nextcloud.telecharger(fichier)
        except NextcloudErreur as erreur:
            raise UserError(str(erreur)) from None
        date = fichier.modifie_le
        if date.tzinfo:  # en-tête HTTP en GMT → datetime naïf UTC (convention Odoo)
            date = date.astimezone(timezone.utc).replace(tzinfo=None)
        return contenu, fichier.nom, date, nextcloud.lien(fichier, self.dossier_nextcloud)

    def action_lancer(self):
        self.ensure_one()
        if self.periode:
            try:
                ods.verifier_periode(self.periode)
            except ods.OdSalairesErreur as erreur:
                raise UserError(str(erreur)) from None
        if self.source not in (self.dossier_id.source_pieces, "fichier"):
            raise UserError(_("Source non prévue pour ce dossier (voir « Source des pièces »)."))
        Run = self.env["hub.run"]
        if self.source == "documents":
            self.dossier_id._config_documents(ods.TACHE)  # configuration présente
            run = Run._lancer(self.dossier_id, "documents", modele=self.modele,
                              periode=self.periode,
                              piece_imposee_id=self.piece_imposee_id.client_id or 0)
        else:
            contenu, nom, date, lien = self._charger_pdf()
            run = Run._lancer(self.dossier_id, self.source, pdf=contenu, pdf_nom=nom,
                              modele=self.modele, periode=self.periode, piece_date=date,
                              piece_lien=lien)
        return {
            "type": "ir.actions.act_window",
            "res_model": "hub.run",
            "res_id": run.id,
            "view_mode": "form",
            "target": "current",
            "name": _("OD de salaires"),
        }
