import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import config

from ..tools import cle_api

_logger = logging.getLogger(__name__)

OPTION_CLE_PUBLIQUE = "appalach_hub_moteur_public_key_file"
OPTION_CLE_PRIVEE = "appalach_hub_moteur_private_key_file"
PREFIXE = "appalach_hub."

# Secrets globaux (paramètres « Hub compta »), stockés chiffrés dans ir.config_parameter.
SECRETS_GLOBAUX = {
    "anthropic_api_key": "Clé API Anthropic",
    "nextcloud_password": "Mot de passe d'application Nextcloud",
}


class HubSecrets(models.AbstractModel):
    """Chiffrement / déchiffrement des secrets du hub (RSA-OAEP).

    POC : la clé privée est sur le serveur du hub (option odoo.conf), lue à chaque
    déchiffrement. Les secrets en clair ne sont jamais stockés, journalisés ni affichés.
    """

    _name = "hub.secrets"
    _description = "Secrets du hub"

    @api.model
    def _lire_pem(self, option):
        chemin = config.get(option)
        if not chemin:
            raise UserError(_("Option %s absente de odoo.conf.", option))
        try:
            with open(chemin, "rb") as fichier:
                return fichier.read()
        except OSError:
            raise UserError(_("Fichier de clé illisible (option %s).", option)) from None

    @api.model
    def _chiffrer(self, clair):
        """Renvoie (chiffré base64, empreinte de la clé publique)."""
        try:
            cle_publique = cle_api.charger_cle_publique(self._lire_pem(OPTION_CLE_PUBLIQUE))
            return (
                cle_api.chiffrer_rsa_oaep(cle_publique, clair),
                cle_api.empreinte_cle_publique(cle_publique),
            )
        except cle_api.CleApiErreur as erreur:
            raise UserError(str(erreur)) from None

    @api.model
    def _dechiffrer(self, chiffre, empreinte):
        if not chiffre:
            raise UserError(_("Secret non défini."))
        try:
            cle_privee = cle_api.charger_cle_privee(self._lire_pem(OPTION_CLE_PRIVEE))
            if empreinte and cle_api.empreinte_cle_publique(cle_privee.public_key()) != empreinte:
                raise UserError(_(
                    "La clé privée configurée ne correspond pas à la clé publique utilisée "
                    "pour chiffrer ce secret : redéfinissez-le ou corrigez odoo.conf."
                ))
            return cle_api.dechiffrer_rsa_oaep(cle_privee, chiffre)
        except cle_api.CleApiErreur as erreur:
            raise UserError(str(erreur)) from None

    # ------------------------------------------------------------------
    # Secrets globaux
    # ------------------------------------------------------------------

    @api.model
    def _verifier_admin(self):
        if not self.env.su and not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Seul un administrateur peut définir les secrets du hub."))

    @api.model
    def _definir_secret_global(self, nom, clair):
        self._verifier_admin()
        if nom not in SECRETS_GLOBAUX:
            raise UserError(_("Secret inconnu : %s", nom))
        chiffre, empreinte = self._chiffrer(clair)
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_str(f"{PREFIXE}{nom}_chiffre", chiffre)
        icp.set_str(f"{PREFIXE}{nom}_empreinte", empreinte)
        icp.set_str(f"{PREFIXE}{nom}_date", fields.Datetime.to_string(fields.Datetime.now()))
        icp.set_str(f"{PREFIXE}{nom}_user_id", str(self.env.user.id))
        _logger.info("Secret global %s défini par l'utilisateur %s", nom, self.env.user.id)

    @api.model
    def _secret_global(self, nom):
        icp = self.env["ir.config_parameter"].sudo()
        chiffre = icp.get_str(f"{PREFIXE}{nom}_chiffre")
        if not chiffre:
            raise UserError(_("%s non définie dans les paramètres Hub compta.", SECRETS_GLOBAUX[nom]))
        return self._dechiffrer(chiffre, icp.get_str(f"{PREFIXE}{nom}_empreinte"))

    @api.model
    def _info_secret_global(self, nom):
        """Renvoie (date, utilisateur) ou (False, False) ; jamais la valeur."""
        icp = self.env["ir.config_parameter"].sudo()
        if not icp.get_str(f"{PREFIXE}{nom}_chiffre"):
            return False, False
        date = fields.Datetime.to_datetime(icp.get_str(f"{PREFIXE}{nom}_date") or False)
        user_id = icp.get_str(f"{PREFIXE}{nom}_user_id")
        user = self.env["res.users"].sudo().browse(int(user_id)).exists() if user_id else False
        return date, user

    @api.model
    def _parametre(self, nom, defaut=False):
        # Odoo 20 : accesseurs typés (get_str / set_str) à la place de get_param / set_param.
        return self.env["ir.config_parameter"].sudo().get_str(f"{PREFIXE}{nom}", defaut)
