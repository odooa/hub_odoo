from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install")
class TestDossierFait(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.comptable = new_test_user(
            cls.env, login="hub_comptable", groups="appalach_hub.group_hub_comptable"
        )
        cls.gestionnaire = new_test_user(
            cls.env, login="hub_gestionnaire", groups="appalach_hub.group_hub_gestionnaire"
        )
        cls.interne = new_test_user(cls.env, login="hub_interne", groups="base.group_user")
        cls.dossier = cls.env["hub.dossier"].create({"name": "Dossier test", "code": "TEST1"})

    def test_code_unique(self):
        with mute_logger("odoo.sql_db"), self.assertRaises(Exception):
            with self.cr.savepoint():
                self.env["hub.dossier"].create({"name": "Doublon", "code": "TEST1"})

    def test_cle_unique_par_dossier(self):
        autre = self.env["hub.dossier"].create({"name": "Autre", "code": "TEST2"})
        fait = self.env["hub.fait"]
        fait.create({"dossier_id": self.dossier.id, "cle": "achats_marchandises"})
        fait.create({"dossier_id": autre.id, "cle": "achats_marchandises"})
        with mute_logger("odoo.sql_db"), self.assertRaises(Exception):
            with self.cr.savepoint():
                fait.create({"dossier_id": self.dossier.id, "cle": "achats_marchandises"})
        self.assertEqual(self.dossier.fait_count, 1)

    def test_cle_snake_case(self):
        for cle in ("AchatsMarchandises", "1er_fait", "avec espace", "accentué"):
            with self.subTest(cle=cle), self.assertRaises(ValidationError):
                self.env["hub.fait"].create({"dossier_id": self.dossier.id, "cle": cle})

    def test_valeur_selon_type(self):
        valides = [
            ("booleen", "true"), ("booleen", "false"), ("entier", "-12"),
            ("decimal", "1000.50"), ("decimal", "3"), ("date", "2026-11-10"),
            ("texte", "n'importe quoi"),
        ]
        invalides = [
            ("booleen", "oui"), ("booleen", "True"), ("entier", "1.5"),
            ("decimal", "1000,50"), ("date", "10/11/2026"), ("date", "2026-13-01"),
        ]
        for i, (type_valeur, valeur) in enumerate(valides):
            self.env["hub.fait"].create({
                "dossier_id": self.dossier.id, "cle": f"ok_{i}",
                "type_valeur": type_valeur, "valeur": valeur,
            })
        for i, (type_valeur, valeur) in enumerate(invalides):
            with self.subTest(type=type_valeur, valeur=valeur), self.assertRaises(ValidationError):
                self.env["hub.fait"].create({
                    "dossier_id": self.dossier.id, "cle": f"ko_{i}",
                    "type_valeur": type_valeur, "valeur": valeur,
                })

    def test_droits_comptable(self):
        dossier = self.dossier.with_user(self.comptable)
        dossier.read(["name"])
        dossier.write({"profil_activite": "services"})
        with self.assertRaises(AccessError):
            self.env["hub.dossier"].with_user(self.comptable).create(
                {"name": "Interdit", "code": "TEST3"}
            )
        with self.assertRaises(AccessError):
            dossier.unlink()
        fait = self.env["hub.fait"].with_user(self.comptable).create(
            {"dossier_id": self.dossier.id, "cle": "seuil_immobilisation",
             "type_valeur": "decimal", "valeur": "1000"}
        )
        fait.unlink()

    def test_droits_gestionnaire(self):
        dossiers = self.env["hub.dossier"].with_user(self.gestionnaire)
        dossier = dossiers.create({"name": "Nouveau", "code": "TEST4"})
        dossier.write({"statut": "suspendu"})
        dossier.unlink()

    def test_interne_sans_groupe(self):
        with self.assertRaises(AccessError):
            self.dossier.with_user(self.interne).read(["name"])
        with self.assertRaises(AccessError):
            self.env["hub.fait"].with_user(self.interne).search([])

    def test_champs_cle_non_modifiables(self):
        with self.assertRaises(AccessError):
            self.dossier.with_user(self.gestionnaire).write(
                {"cle_api_user_id": self.gestionnaire.id}
            )
