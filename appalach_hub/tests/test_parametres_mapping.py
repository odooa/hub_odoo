"""Secrets globaux (paramètres Hub compta) et modèle hub.mapping. Données fictives."""

from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from ..models.hub_secrets import OPTION_CLE_PRIVEE, OPTION_CLE_PUBLIQUE
from .common import HubCase, generer_paire_pem, reponse_http

GET_CLAUDE = "odoo.addons.appalach_hub.tools.anthropic_http.requests.get"
REQUETE_NC = "odoo.addons.appalach_hub.tools.nextcloud.requests.request"
SECRET = "sk-ant-fictive-0000"


@tagged("post_install", "-at_install")
class TestParametres(HubCase):
    def _definir(self, user, nom="anthropic_api_key", valeur=SECRET):
        return self.env["hub.parametre.secret.wizard"].with_user(user).create(
            {"nom": nom, "valeur": valeur}
        )

    def test_cle_anthropic_ecriture_seule(self):
        with patch(GET_CLAUDE, return_value=reponse_http(200, {"data": []})):
            self._definir(self.admin)
        icp = self.env["ir.config_parameter"].sudo()
        chiffre = icp.get_str("appalach_hub.anthropic_api_key_chiffre")
        self.assertTrue(chiffre)
        self.assertNotEqual(chiffre, SECRET)
        self.assertEqual(self.env["hub.secrets"]._secret_global("anthropic_api_key"), SECRET)
        date, user = self.env["hub.secrets"]._info_secret_global("anthropic_api_key")
        self.assertTrue(date)
        self.assertEqual(user, self.admin)
        self.env.flush_all()
        for table in ("ir_config_parameter", "hub_parametre_secret_wizard"):
            self.env.cr.execute(f"SELECT count(*) FROM {table} t WHERE t::text LIKE %s", [f"%{SECRET}%"])
            self.assertEqual(self.env.cr.fetchone()[0], 0, table)
        settings = self.env["res.config.settings"].with_user(self.admin).create({})
        self.assertTrue(settings.hub_anthropic_definie)
        self.assertIn(self.admin.name, settings.hub_anthropic_info)

    def test_cle_refusee_rien_stocke(self):
        with patch(GET_CLAUDE, return_value=reponse_http(401, {})), \
                self.assertRaisesRegex(UserError, "refusée"):
            self._definir(self.admin)
        self.assertFalse(self.env["ir.config_parameter"].sudo().get_str(
            "appalach_hub.anthropic_api_key_chiffre"))

    def test_reserve_aux_administrateurs(self):
        with self.assertRaises(AccessError):
            self._definir(self.gestionnaire)
        with self.assertRaises(AccessError):
            self.env["hub.secrets"].with_user(self.gestionnaire)._definir_secret_global(
                "anthropic_api_key", SECRET)

    def test_mot_de_passe_nextcloud(self):
        icp = self.env["ir.config_parameter"].sudo()
        with self.assertRaisesRegex(UserError, "URL et l'utilisateur"):
            self._definir(self.admin, "nextcloud_password", "mdp")
        icp.set_str("appalach_hub.nextcloud_url", "https://nc.invalid")
        icp.set_str("appalach_hub.nextcloud_user", "agent")
        with patch(REQUETE_NC, return_value=reponse_http(207, contenu=b"<d:multistatus xmlns:d='DAV:'/>")) as requete:
            self._definir(self.admin, "nextcloud_password", "mdp-fictif")
        self.assertEqual(requete.call_args.kwargs["auth"], ("agent", "mdp-fictif"))
        self.assertEqual(self.env["hub.secrets"]._secret_global("nextcloud_password"), "mdp-fictif")

    def test_mauvaise_cle_privee(self):
        self.env["hub.secrets"]._definir_secret_global("anthropic_api_key", SECRET)
        _autre, _pub, pem_priv_autre = generer_paire_pem()
        pems = {OPTION_CLE_PUBLIQUE: self.pem_publique, OPTION_CLE_PRIVEE: pem_priv_autre}
        with patch.object(type(self.env["hub.secrets"]), "_lire_pem", side_effect=lambda o: pems[o]), \
                self.assertRaisesRegex(UserError, "ne correspond pas"):
            self.env["hub.secrets"]._secret_global("anthropic_api_key")

    def test_secret_absent(self):
        with self.assertRaisesRegex(UserError, "non définie"):
            self.env["hub.secrets"]._secret_global("nextcloud_password")


@tagged("post_install", "-at_install")
class TestMapping(HubCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dossier = cls.env["hub.dossier"].create({"name": "Dossier M", "code": "MAP1"})

    def test_unicite_normalisee(self):
        Mapping = self.env["hub.mapping"]
        Mapping.create({"dossier_id": self.dossier.id, "libelle": "Net à payer", "compte_code": "455000"})
        with mute_logger("odoo.sql_db"), self.assertRaises(Exception):
            with self.cr.savepoint():
                Mapping.create({"dossier_id": self.dossier.id, "libelle": "  NET à   payer ",
                                "compte_code": "455100"})

    def test_memoriser(self):
        Mapping = self.env["hub.mapping"]
        premier = Mapping._memoriser(self.dossier, "od_salaires", "Net à payer", "455000", "Rém. à payer")
        premier.active = False
        second = Mapping._memoriser(self.dossier, "od_salaires", "net à payer", "455100", "Autre")
        self.assertEqual(premier, second)
        self.assertTrue(second.active)
        self.assertEqual(second.compte_code, "455100")

    def test_droits(self):
        mapping = self.env["hub.mapping"].with_user(self.comptable).create({
            "dossier_id": self.dossier.id, "libelle": "Brut", "compte_code": "620200"})
        mapping.with_user(self.moteur).read(["compte_code"])
        with self.assertRaises(AccessError):
            mapping.with_user(self.moteur).write({"compte_code": "1"})
        mapping.unlink()
