import base64
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged

CLE_EN_CLAIR = "cle-api-fictive-0123456789abcdef"
CHEMIN_POST = "odoo.addons.appalach_hub.tools.cle_api.requests.post"


def _reponse(status_code):
    reponse = MagicMock()
    reponse.status_code = status_code
    return reponse


@tagged("post_install", "-at_install")
class TestCleApi(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cle_privee = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        cls.pem_publique = cls.cle_privee.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        cls.comptable = new_test_user(
            cls.env, login="cle_comptable", groups="appalach_hub.group_hub_comptable"
        )
        cls.gestionnaire = new_test_user(
            cls.env, login="cle_gestionnaire", groups="appalach_hub.group_hub_gestionnaire"
        )
        cls.moteur = new_test_user(
            cls.env, login="cle_moteur", groups="appalach_hub.group_hub_moteur"
        )
        cls.dossier = cls.env["hub.dossier"].create({
            "name": "Dossier clé", "code": "CLE1",
            "odoo_url": "https://exemple.invalid", "odoo_base": "base_test",
        })
        cls.classe_dossier = type(cls.env["hub.dossier"])

    def _patch_cle_publique(self, pem=None):
        return patch.object(
            self.classe_dossier, "_lire_cle_publique_moteur",
            return_value=pem or self.pem_publique,
        )

    def _definir(self, user, status_code=200):
        with self._patch_cle_publique(), patch(CHEMIN_POST, return_value=_reponse(status_code)) as post:
            self.env["hub.dossier.cle.wizard"].with_user(user).create({
                "dossier_id": self.dossier.id, "cle_api": CLE_EN_CLAIR,
            })
        return post

    def _chiffre(self):
        return self.dossier.sudo().cle_api_chiffree

    def test_definition_chiffre_dechiffrable(self):
        post = self._definir(self.gestionnaire)
        appel = post.call_args
        self.assertTrue(appel.args[0].endswith("/json/2/res.users/context_get"))
        self.assertEqual(appel.kwargs["headers"]["Authorization"], f"bearer {CLE_EN_CLAIR}")
        self.assertEqual(appel.kwargs["headers"]["X-Odoo-Database"], "base_test")

        clair = self.cle_privee.decrypt(
            base64.b64decode(self._chiffre()),
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        ).decode()
        self.assertEqual(clair, CLE_EN_CLAIR)
        self.assertTrue(self.dossier.cle_api_definie)
        self.assertEqual(self.dossier.cle_api_user_id, self.gestionnaire)
        self.assertTrue(self.dossier.cle_api_date)
        self.assertEqual(len(self.dossier.sudo().cle_api_empreinte), 64)

    def test_cle_en_clair_absente_de_la_base(self):
        self._definir(self.gestionnaire)
        self._definir(self.gestionnaire)  # remplacement
        self.env.flush_all()
        motif = f"%{CLE_EN_CLAIR}%"
        for table in ("hub_dossier", "hub_dossier_cle_wizard", "mail_message", "mail_tracking_value"):
            self.env.cr.execute(
                f"SELECT count(*) FROM {table} t WHERE t::text LIKE %s", [motif]
            )
            self.assertEqual(self.env.cr.fetchone()[0], 0, f"clé en clair trouvée dans {table}")
        corps = self.dossier.message_ids.mapped("body")
        self.assertTrue(any("Clé API remplacée" in str(b) for b in corps))
        self.assertTrue(all(self._chiffre() not in str(b) for b in corps))

    def test_connexion_refusee(self):
        with self.assertRaisesRegex(UserError, "refusée"):
            self._definir(self.gestionnaire, status_code=401)
        self.assertFalse(self._chiffre())
        self.assertFalse(self.dossier.cle_api_definie)

    def test_cle_publique_absente(self):
        with patch(CHEMIN_POST, return_value=_reponse(200)) as post, \
                patch("odoo.addons.appalach_hub.models.hub_dossier.config.get", return_value=None), \
                self.assertRaisesRegex(UserError, "non configurée"):
            self.env["hub.dossier.cle.wizard"].with_user(self.gestionnaire).create({
                "dossier_id": self.dossier.id, "cle_api": CLE_EN_CLAIR,
            })
        post.assert_not_called()
        self.assertFalse(self._chiffre())

    def test_cle_publique_trop_courte(self):
        petite = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
        pem = petite.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        with self._patch_cle_publique(pem), patch(CHEMIN_POST, return_value=_reponse(200)), \
                self.assertRaisesRegex(UserError, "trop courte"):
            self.dossier.with_user(self.gestionnaire)._definir_cle_api(CLE_EN_CLAIR)

    def test_url_manquante(self):
        self.dossier.odoo_url = False
        with self.assertRaises(UserError):
            self._definir(self.gestionnaire)

    def test_comptable_ne_peut_pas_definir(self):
        with self.assertRaises(AccessError):
            self.dossier.with_user(self.comptable).action_ouvrir_cle_wizard()
        with self.assertRaises(AccessError):
            self._definir(self.comptable)
        with self.assertRaises(AccessError):
            self.dossier.with_user(self.comptable)._definir_cle_api(CLE_EN_CLAIR)

    def test_lecture_du_chiffre(self):
        self._definir(self.gestionnaire)
        for user in (self.comptable, self.gestionnaire):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.dossier.with_user(user).read(["cle_api_chiffree"])
        # L'affichage reste possible pour tous.
        self.assertTrue(self.dossier.with_user(self.comptable).cle_api_definie)
        lu = self.dossier.with_user(self.moteur).read(["cle_api_chiffree", "cle_api_empreinte"])
        self.assertEqual(lu[0]["cle_api_chiffree"], self._chiffre())
        with self.assertRaises(AccessError):
            self.dossier.with_user(self.moteur).write({"name": "Interdit"})
