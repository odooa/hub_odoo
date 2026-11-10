"""Parcours de l'assistant OD de salaires, services externes simulés. Données fictives."""

import base64
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from ..tools import od_salaires as ods
from ..tools.anthropic_http import ClaudeErreur
from .common import FauxOdooClient, HubCase

GENERER_JSON = "odoo.addons.appalach_hub.wizard.hub_od_salaires_wizard.generer_json"
POST_ODOO = "odoo.addons.appalach_hub.tools.odoo_json2.requests.post"
CLE_CLAUDE = "cle-anthropic-fictive-123"
CLE_DOSSIER = "cle-odoo-client-fictive-456"
PDF = b"%PDF-1.4 recapitulatif fictif"

EXTRACTION = {
    "periode": "2026-10",
    "secretariat": "Secrétariat fictif",
    "rubriques": [
        {"libelle": "Rémunérations brutes", "montant": 5000.00, "sens": "debit", "categorie": "brut"},
        {"libelle": "Cotisations patronales", "montant": 1353.25, "sens": "debit", "categorie": "autre"},
        {"libelle": "ONSS travailleur", "montant": 653.50, "sens": "credit", "categorie": "onss"},
        {"libelle": "ONSS patronal", "montant": 1353.25, "sens": "credit", "categorie": "onss"},
        {"libelle": "Précompte professionnel", "montant": 1000.00, "sens": "credit", "categorie": "precompte"},
        {"libelle": "Net à payer", "montant": 3346.50, "sens": "credit", "categorie": "net"},
    ],
    "totaux": {"brut": 5000.00, "onss": 2006.75, "precompte": 1000.00, "net": 3346.50},
}

def proposition(libelle, code, justification="PCMN.", ecriture=""):
    return {"libelle": libelle, "code_compte": code, "ecriture_reference": ecriture,
            "justification": justification}


PROPOSITIONS_CLAUDE = {"propositions": [
    proposition("Cotisations patronales", "621000"),
    proposition("ONSS travailleur", "454000"),
    proposition("ONSS patronal", "454000"),
    proposition("Précompte professionnel", "453000"),
    proposition("Net à payer", "455000"),
]}


@tagged("post_install", "-at_install")
class TestOdSalaires(HubCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        secrets = cls.env["hub.secrets"]
        secrets._definir_secret_global("anthropic_api_key", CLE_CLAUDE)
        chiffre, empreinte = secrets._chiffrer(CLE_DOSSIER)
        cls.dossier = cls.env["hub.dossier"].create({
            "name": "Dossier OD", "code": "OD1", "tache_od_salaires": True,
            "odoo_url": "https://client.invalid", "odoo_base": "client",
            "comptable_ids": [(4, cls.comptable.id)],
        })
        cls.dossier.sudo().write({"cle_api_chiffree": chiffre, "cle_api_empreinte": empreinte})
        # Mapping existant pour une seule rubrique (variation de casse et d'espaces).
        cls.env["hub.mapping"].create({
            "dossier_id": cls.dossier.id, "libelle": "rémunérations  BRUTES",
            "compte_code": "620200", "compte_libelle": "Rémunérations employés",
        })

    def setUp(self):
        super().setUp()
        self.odoo = FauxOdooClient()
        self.startPatcher(patch(POST_ODOO, side_effect=self.odoo))
        self.appels_claude = []

    def _claude(self, extraction=EXTRACTION, propositions=PROPOSITIONS_CLAUDE, erreur=None):
        def generer(cle, modele, contenu, schema, etape, max_tokens=16000):
            self.appels_claude.append((cle, modele, contenu, etape))
            if erreur:
                raise erreur
            return extraction if schema is ods.SCHEMA_EXTRACTION else propositions
        return patch(GENERER_JSON, side_effect=generer)

    def _assistant(self, user=None):
        return self.env["hub.od.salaires.wizard"].with_user(user or self.comptable).create({
            "dossier_id": self.dossier.id, "source": "fichier",
            "pdf": base64.b64encode(PDF).decode(), "pdf_nom": "recap-2026-10.pdf",
        })

    def _messages(self):
        return " ".join(self.dossier.message_ids.mapped(lambda m: str(m.body)))

    def test_parcours_complet(self):
        assistant = self._assistant()
        with self._claude():
            assistant.action_extraire()

        # Extraction : modèle par défaut, clé déchiffrée, PDF envoyé en document.
        cle, modele, contenu, etape = self.appels_claude[0]
        self.assertEqual((cle, modele, etape), (CLE_CLAUDE, "claude-sonnet-5-5", "extraction"))
        self.assertEqual(contenu[0]["source"]["data"], base64.b64encode(PDF).decode())
        # Clé de l'Odoo client déchiffrée et utilisée en bearer.
        self.assertEqual(self.odoo.appels[0][3]["Authorization"], f"bearer {CLE_DOSSIER}")

        # Mapping existant d'abord ; Claude seulement pour les 5 rubriques inconnues.
        self.assertEqual(self.appels_claude[1][3], "proposition de mapping")
        texte_mapping = self.appels_claude[1][2][0]["text"]
        self.assertNotIn("- Rémunérations brutes", texte_mapping)
        self.assertIn("- Net à payer", texte_mapping)
        origines = dict(zip(assistant.ligne_ids.mapped("libelle"), assistant.ligne_ids.mapped("origine")))
        self.assertEqual(origines["Rémunérations brutes"], "mapping")
        self.assertEqual(origines["Net à payer"], "pcmn")  # aucun historique
        self.assertNotIn("Historique des OD", texte_mapping)
        # Société de l'utilisateur API (pas d'odoo_company_id) appliquée à toutes les lectures.
        self.assertEqual(self.odoo.methodes()[:2], [("res.users", "context_get"), ("res.users", "read")])
        self.assertEqual(self.odoo.corps("account.account", "search_read")[0]["context"],
                         {"allowed_company_ids": [1]})
        self.assertIn(["company_ids", "in", [1]], self.odoo.corps("account.account", "search_read")[0]["domain"])
        self.assertEqual(assistant.societe_client_nom, "Société fictive")
        # Compte sans code : ignoré et signalé, non bloquant.
        self.assertNotIn("Compte sans code", assistant.compte_ids.mapped("name"))
        self.assertIn("Compte sans code", str(assistant.avertissements_html))

        self.assertEqual(assistant.etape, "revue")
        self.assertEqual(assistant.journal_id.code, "OD")
        self.assertTrue(assistant.controles_ok, assistant.controles_html)

        assistant.action_creer_od()
        self.assertEqual(assistant.etape, "termine")
        self.assertEqual(assistant.move_id_client, 42)
        creation = next(c for m, meth, c, _h in self.odoo.appels if (m, meth) == ("account.move", "create"))
        valeurs = creation["vals_list"][0]
        self.assertEqual(valeurs["move_type"], "entry")
        self.assertEqual(valeurs["ref"], "Salaires 2026-10")
        self.assertEqual(valeurs["date"], "2026-10-31")
        self.assertEqual(valeurs["journal_id"], 9)
        self.assertNotIn("state", valeurs)
        self.assertEqual(len(valeurs["line_ids"]), 6)
        piece = next(c for m, meth, c, _h in self.odoo.appels if (m, meth) == ("ir.attachment", "create"))
        self.assertEqual(piece["vals_list"][0]["res_id"], 42)
        self.assertEqual(base64.b64decode(piece["vals_list"][0]["datas"]), PDF)
        # Jamais de comptabilisation.
        self.assertFalse([meth for _m, meth in self.odoo.methodes() if "post" in meth])

        # Mapping mémorisé, journal mémorisé, traces sans secret.
        mappings = self.env["hub.mapping"].search([("dossier_id", "=", self.dossier.id)])
        self.assertEqual(len(mappings), 6)
        self.assertEqual(self.dossier.od_salaires_journal, "OD")
        messages = self._messages()
        self.assertIn("extraction", messages)
        self.assertIn("créée en brouillon", messages)
        self.assertNotIn(CLE_CLAUDE, messages)
        self.assertNotIn(CLE_DOSSIER, messages)

    def test_ecart_de_total_affiche_et_bloque(self):
        extraction = EXTRACTION | {"totaux": EXTRACTION["totaux"] | {"onss": 2000.00}}
        assistant = self._assistant()
        with self._claude(extraction=extraction):
            assistant.action_extraire()
        self.assertFalse(assistant.controles_ok)
        html = str(assistant.controles_html)
        self.assertIn("Total onss", html)
        self.assertIn("attendu 2 000,00, obtenu 2 006,75", html)
        self.assertIn("ONSS patronal : 1 353,25", html)
        action = assistant.action_creer_od()
        self.assertEqual(action["tag"], "display_notification")
        self.assertIn("Contrôles en échec", action["params"]["message"])
        self.assertEqual(action["params"]["next"]["res_id"], assistant.id)
        self.assertNotIn(("account.move", "create"), self.odoo.methodes())
        self.assertIn("échec", self._messages())

    def test_doublon_refuse(self):
        assistant = self._assistant()
        with self._claude():
            assistant.action_extraire()
        self.assertTrue(assistant.controles_ok)
        self.odoo.nb_existantes = 1  # OD créée entre-temps dans l'Odoo client
        action = assistant.action_creer_od()
        self.assertIn("existe déjà", action["params"]["message"])
        self.assertEqual(assistant.etape, "revue")
        self.assertNotIn(("account.move", "create"), self.odoo.methodes())
        self.assertIn("échec", self._messages())

    def test_refus_claude_trace(self):
        assistant = self._assistant()
        with self._claude(erreur=ClaudeErreur("Claude a refusé l'extraction (catégorie : x).")):
            action = assistant.action_extraire()
        self.assertEqual(action["params"]["type"], "danger")
        self.assertIn("refusé", action["params"]["message"])
        self.assertFalse(assistant.ligne_ids)
        self.assertEqual(len(self.appels_claude), 1)
        self.assertIn("refusé", self._messages())
        self.assertEqual(assistant.etape, "source")

    def test_compte_memorise_disparu(self):
        self.odoo.comptes = [c for c in self.odoo.comptes if c["code"] != "620200"]
        self.odoo.comptes.append({"id": 106, "code": "620300", "name": "Rémunérations", "account_type": "expense"})
        propositions = {"propositions": PROPOSITIONS_CLAUDE["propositions"] + [
            proposition("Rémunérations brutes", "620300", "Brut.")]}
        assistant = self._assistant()
        with self._claude(propositions=propositions):
            assistant.action_extraire()
        ligne = assistant.ligne_ids.filtered(lambda l: l.libelle == "Rémunérations brutes")
        self.assertEqual((ligne.compte_id.code, ligne.origine), ("620300", "pcmn"))
        self.assertIn("- Rémunérations brutes", self.appels_claude[1][2][0]["text"])

    def test_historique_puis_pcmn(self):
        self.odoo.ajouter_historique("OD/2026/0912", "2026-09-30", [
            (101, "Rémunérations brutes", 4900.0, 0.0),
            (102, "Cotisations patronales ONSS", 1320.0, 0.0),
            (104, "ONSS à payer", 0.0, 1960.0),
            (105, "Rémunérations nettes Dupont", 0.0, 3280.0),
        ])
        self.odoo.ajouter_historique("OD/2026/0811", "2026-08-31", [(101, "Brut", 4800.0, 0.0)])
        propositions = {"propositions": [
            proposition("Cotisations patronales", "621000", "Comme OD/2026/0912 du 30/09/2026.", "OD/2026/0912"),
            proposition("ONSS travailleur", "454000", "Comme OD/2026/0912.", "OD/2026/0912"),
            proposition("ONSS patronal", "454000", "Écriture inventée.", "OD/9999/0001"),
            proposition("Précompte professionnel", "453000", "PCMN : 453."),
            proposition("Net à payer", "455000", "Comme OD/2026/0912.", "OD/2026/0912"),
        ]}
        assistant = self._assistant()
        with self._claude(propositions=propositions):
            assistant.action_extraire()
        texte = self.appels_claude[1][2][0]["text"]
        self.assertIn("Historique des OD de salaires", texte)
        self.assertIn("Écriture OD/2026/0912 du 2026-09-30", texte)
        self.assertIn("Rémunérations nettes Dupont", texte)  # libellés tels quels par défaut
        self.assertEqual(assistant.historique_nb, 2)
        self.assertEqual(assistant.historique_periode, "2026-08-31 → 2026-09-30")
        origines = {l.libelle: (l.origine, l.justification) for l in assistant.ligne_ids}
        self.assertEqual(origines["Rémunérations brutes"][0], "mapping")
        self.assertEqual(origines["Cotisations patronales"], ("historique", "Comme OD/2026/0912 du 30/09/2026."))
        self.assertEqual(origines["ONSS patronal"][0], "pcmn")  # écriture citée inconnue
        self.assertEqual(origines["Précompte professionnel"][0], "pcmn")
        historique = self.odoo.corps("account.move", "search_read")[0]
        self.assertEqual(historique["limit"], 6)
        self.assertIn(["state", "=", "posted"], historique["domain"])
        self.assertIn(["line_ids.account_id.code", "=like", "62%"], historique["domain"])
        self.assertIn(["ref", "ilike", "salaire"], historique["domain"])

    def test_anonymisation_historique(self):
        self.dossier.anonymiser_historique = True
        self.odoo.ajouter_historique("OD/2026/0912", "2026-09-30", [(105, "Net Dupont Jean", 0.0, 3280.0)])
        assistant = self._assistant()
        with self._claude():
            assistant.action_extraire()
        texte = self.appels_claude[1][2][0]["text"]
        self.assertNotIn("Dupont", texte)
        self.assertIn("455000 | Rémunérations à payer | Rémunérations à payer", texte)

    def test_compte_historique_sans_code_bloque(self):
        self.odoo.ajouter_historique("OD/2026/0912", "2026-09-30", [(199, "Chèques-repas", 100.0, 0.0)])
        propositions = {"propositions": PROPOSITIONS_CLAUDE["propositions"][:-1] + [
            proposition("Net à payer", "SANS-CODE-199", "Comme OD/2026/0912.", "OD/2026/0912")]}
        assistant = self._assistant()
        with self._claude(propositions=propositions):
            assistant.action_extraire()
        self.assertIn("SANS-CODE-199", self.appels_claude[1][2][0]["text"])
        ligne = assistant.ligne_ids.filtered(lambda l: l.libelle == "Net à payer")
        self.assertFalse(ligne.compte_id)
        self.assertEqual(ligne.compte_sans_code, "Compte sans code")
        self.assertFalse(assistant.controles_ok)
        self.assertIn("Net à payer → Compte sans code", str(assistant.controles_html))

    def test_societe_du_dossier(self):
        self.dossier.odoo_company_id = 3
        assistant = self._assistant()
        with self._claude():
            assistant.action_extraire()
        self.assertNotIn(("res.users", "context_get"), self.odoo.methodes())
        self.assertEqual(assistant.societe_client_nom, "Autre société fictive")
        self.assertEqual(self.odoo.corps("account.journal", "search_read")[0]["context"],
                         {"allowed_company_ids": [3]})

    def test_aucune_creation_de_compte(self):
        assistant = self._assistant()
        with self._claude():
            assistant.action_extraire()
        Compte = self.env["hub.od.salaires.compte"].with_user(self.comptable)
        with self.assertRaisesRegex(UserError, "création n'est pas possible"):
            Compte.create({"wizard_id": assistant.id, "client_id": 1, "code": "1", "name": "x"})
        with self.assertRaisesRegex(UserError, "création n'est pas possible"):
            Compte.name_create("Nouveau compte")
        with self.assertRaisesRegex(UserError, "création n'est pas possible"):
            self.env["hub.od.salaires.journal"].with_user(self.comptable).name_create("JX")

    def test_assistant_reserve_a_son_auteur(self):
        assistant = self._assistant(self.comptable)
        autre = self.env["hub.od.salaires.wizard"].with_user(self.gestionnaire)
        self.assertFalse(autre.search([("id", "=", assistant.id)]))
