from odoo import _, fields, models
from odoo.exceptions import UserError

from ..models.hub_secrets import SECRETS_GLOBAUX
from ..tools import anthropic_http
from ..tools.nextcloud import Nextcloud, NextcloudErreur


class HubParametreSecretWizard(models.TransientModel):
    """Saisie d'un secret global (clé Anthropic, mot de passe Nextcloud).

    Même principe que la clé du dossier : ``valeur`` n'est pas stockée ; l'inverse teste
    la valeur, la chiffre et n'enregistre que le chiffré (paramètres système).
    """

    _name = "hub.parametre.secret.wizard"
    _description = "Définition d'un secret du hub"

    nom = fields.Selection(list(SECRETS_GLOBAUX.items()), required=True)
    valeur = fields.Char(
        "Valeur", compute="_compute_valeur", inverse="_inverse_valeur", store=False,
        help="Testée, chiffrée, puis oubliée : elle ne pourra plus être relue.",
    )

    def _compute_valeur(self):
        for assistant in self:
            assistant.valeur = False

    def _inverse_valeur(self):
        secrets = self.env["hub.secrets"]
        for assistant in self:
            if not assistant.valeur:
                continue
            secrets._verifier_admin()
            valeur = assistant.valeur.strip()
            # Chiffrer d'abord : une clé publique absente bloque avant tout appel réseau.
            secrets._chiffrer(valeur)
            assistant._tester(valeur)
            secrets._definir_secret_global(assistant.nom, valeur)
            del valeur
        self.invalidate_recordset(["valeur"])

    def _tester(self, valeur):
        secrets = self.env["hub.secrets"]
        if self.nom == "anthropic_api_key":
            try:
                anthropic_http.tester_cle(valeur)
            except anthropic_http.ClaudeErreur as erreur:
                raise UserError(str(erreur)) from None
        elif self.nom == "nextcloud_password":
            url = secrets._parametre("nextcloud_url")
            utilisateur = secrets._parametre("nextcloud_user")
            if not url or not utilisateur:
                raise UserError(_(
                    "Enregistrez d'abord l'URL et l'utilisateur Nextcloud dans les paramètres."
                ))
            try:
                Nextcloud(url, utilisateur, valeur).tester()
            except NextcloudErreur as erreur:
                raise UserError(str(erreur)) from None

    def action_valider(self):
        return {"type": "ir.actions.client", "tag": "reload"}
