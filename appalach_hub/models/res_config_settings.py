from odoo import _, api, fields, models

from .hub_secrets import SECRETS_GLOBAUX

MODELE_PAR_DEFAUT = "claude-sonnet-5-5"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    hub_claude_modele = fields.Char(
        "Modèle Claude", config_parameter="appalach_hub.claude_modele", default=MODELE_PAR_DEFAUT,
    )
    hub_nextcloud_url = fields.Char("URL Nextcloud", config_parameter="appalach_hub.nextcloud_url")
    hub_nextcloud_user = fields.Char(
        "Utilisateur Nextcloud", config_parameter="appalach_hub.nextcloud_user"
    )
    hub_anthropic_info = fields.Char("Clé API Anthropic", compute="_compute_hub_secrets_info")
    hub_anthropic_definie = fields.Boolean(compute="_compute_hub_secrets_info")
    hub_nextcloud_info = fields.Char("Mot de passe Nextcloud", compute="_compute_hub_secrets_info")
    hub_nextcloud_definie = fields.Boolean(compute="_compute_hub_secrets_info")

    @api.depends("company_id")
    def _compute_hub_secrets_info(self):
        secrets = self.env["hub.secrets"]
        infos = {}
        for nom, champ in (("anthropic_api_key", "anthropic"), ("nextcloud_password", "nextcloud")):
            date, user = secrets._info_secret_global(nom)
            if date:
                texte = _("Définie le %(date)s par %(user)s",
                          date=fields.Datetime.to_string(fields.Datetime.context_timestamp(self, date)),
                          user=user.name if user else "?")
            else:
                texte = _("Aucune valeur définie")
            infos[champ] = (texte, bool(date))
        for settings in self:
            settings.hub_anthropic_info, settings.hub_anthropic_definie = infos["anthropic"]
            settings.hub_nextcloud_info, settings.hub_nextcloud_definie = infos["nextcloud"]

    def action_hub_definir_secret(self):
        nom = self.env.context.get("hub_secret")
        if nom not in SECRETS_GLOBAUX:
            return False
        self.env["hub.secrets"]._verifier_admin()
        return {
            "type": "ir.actions.act_window",
            "name": SECRETS_GLOBAUX[nom],
            "res_model": "hub.parametre.secret.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_nom": nom},
        }
