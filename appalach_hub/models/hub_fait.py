import re
from datetime import date

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

MOTIF_CLE = re.compile(r"^[a-z][a-z0-9_]*$")
MOTIF_ENTIER = re.compile(r"^-?\d+$")
MOTIF_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")


class HubFait(models.Model):
    _name = "hub.fait"
    _description = "Fait d'un dossier"
    _inherit = ["mail.thread"]
    _order = "dossier_id, cle"
    _rec_name = "cle"

    dossier_id = fields.Many2one(
        "hub.dossier", "Dossier", required=True, ondelete="cascade", index=True, tracking=True
    )
    cle = fields.Char(
        "Clé", required=True, tracking=True, help="snake_case, ex. achats_marchandises"
    )
    type_valeur = fields.Selection(
        [
            ("booleen", "Booléen"),
            ("entier", "Entier"),
            ("decimal", "Décimal"),
            ("texte", "Texte"),
            ("date", "Date"),
        ],
        "Type",
        default="texte",
        required=True,
        tracking=True,
    )
    valeur = fields.Char(
        tracking=True,
        help="Booléen : true / false ; décimal avec un point ; date au format AAAA-MM-JJ.",
    )
    note = fields.Text(help="Justification ou source du fait.")
    active = fields.Boolean(default=True, tracking=True)

    _dossier_cle_uniq = models.Constraint(
        "UNIQUE (dossier_id, cle)", "Cette clé existe déjà pour ce dossier."
    )

    @api.constrains("cle")
    def _check_cle(self):
        for fait in self:
            if not MOTIF_CLE.match(fait.cle or ""):
                raise ValidationError(
                    _(
                        "Clé « %s » invalide : minuscules, chiffres et _ uniquement, "
                        "en commençant par une lettre.",
                        fait.cle,
                    )
                )

    @api.constrains("valeur", "type_valeur")
    def _check_valeur(self):
        for fait in self:
            if not fait.valeur or fait.type_valeur == "texte":
                continue
            valeur = fait.valeur
            if fait.type_valeur == "booleen":
                valide = valeur in ("true", "false")
            elif fait.type_valeur == "entier":
                valide = bool(MOTIF_ENTIER.match(valeur))
            elif fait.type_valeur == "decimal":
                valide = bool(MOTIF_DECIMAL.match(valeur))
            else:  # date
                try:
                    date.fromisoformat(valeur)
                    valide = len(valeur) == 10
                except ValueError:
                    valide = False
            if not valide:
                raise ValidationError(
                    _(
                        "Valeur « %(valeur)s » incompatible avec le type %(type)s du fait %(cle)s.",
                        valeur=valeur,
                        type=dict(self._fields["type_valeur"].selection)[fait.type_valeur],
                        cle=fait.cle,
                    )
                )
