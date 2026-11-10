from odoo import fields, models


class HubDossierCleWizard(models.TransientModel):
    """Saisie de la clé API d'un dossier.

    Le champ ``cle_api`` n'est pas stocké : sa valeur est traitée dans l'inverse
    (test de connexion puis chiffrement, voir hub.dossier._definir_cle_api) au moment
    de l'enregistrement, et n'atteint jamais la table de l'assistant.
    """

    _name = "hub.dossier.cle.wizard"
    _description = "Définition de la clé API d'un dossier"

    dossier_id = fields.Many2one("hub.dossier", required=True, ondelete="cascade")
    cle_api = fields.Char(
        "Clé API",
        compute="_compute_cle_api",
        inverse="_inverse_cle_api",
        store=False,
        help="Clé API d'un utilisateur dédié de l'Odoo du dossier. "
        "Elle est testée, chiffrée, puis oubliée.",
    )

    def _compute_cle_api(self):
        for assistant in self:
            assistant.cle_api = False

    def _inverse_cle_api(self):
        for assistant in self:
            if assistant.cle_api:
                assistant.dossier_id._definir_cle_api(assistant.cle_api)
        # Retirer la valeur du cache de la requête.
        self.invalidate_recordset(["cle_api"])

    def action_valider(self):
        return {"type": "ir.actions.act_window_close"}
