"""Agent OD de salaires (hub.run) : Claude, Nextcloud et Odoo client simulés. Données fictives."""

import base64
import copy
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from ..tools.agent_outils import MARQUEUR_PDF, RELANCE
from ..tools.anthropic_http import ClaudeErreur
from .common import FauxOdooClient, HubCase

ENVOYER = "odoo.addons.appalach_hub.models.hub_run.envoyer_message"
POST_ODOO = "odoo.addons.appalach_hub.tools.odoo_json2.requests.post"
CLE_CLAUDE = "cle-anthropic-fictive-123"
CLE_DOSSIER = "cle-odoo-client-fictive-456"
PDF = b"%PDF-1.4 recapitulatif fictif"
PDF_B64 = base64.b64encode(PDF).decode()


def outil(ident, nom, entree=None):
    return {"type": "tool_use", "id": ident, "name": nom, "input": entree or {}}


def pensee(texte):
    return {"type": "thinking", "thinking": texte, "signature": "signature-fictive"}


def reponse(*blocs, stop="tool_use", entree=1000, sortie=100):
    return {"type": "message", "role": "assistant", "content": list(blocs), "stop_reason": stop,
            "usage": {"input_tokens": entree, "output_tokens": sortie}}


def ligne(libelle, code, debit, credit, categorie, ecriture="OD/2026/0912", partenaire=0):
    return {"libelle": libelle, "code_compte": code, "partenaire_id": partenaire, "debit": debit,
            "credit": credit, "categorie": categorie, "ecriture_reference": ecriture,
            "justification": f"comme {ecriture} du 30/09/2026" if ecriture else "PCMN."}


def proposition(**changements):
    valeurs = {
        "periode": "2026-10", "journal_code": "OD", "date": "2026-10-31",
        "reference": "Salaires 2026-10",
        "totaux_controle": {"brut": 5000.00, "onss": 2006.75, "precompte": 1000.00, "net": 3346.50},
        "lignes": [
            ligne("Rémunérations brutes", "620200", 5000.00, 0, "brut"),
            ligne("Cotisations patronales", "621000", 1353.25, 0, "autre"),
            ligne("ONSS travailleur", "454000", 0, 653.50, "onss", partenaire=7),
            ligne("ONSS patronal", "454000", 0, 1353.25, "onss", partenaire=7),
            ligne("Précompte professionnel", "453000", 0, 1000.00, "precompte",
                  ecriture="OD/2026/0999"),
            ligne("Net à payer", "455000", 0, 3346.50, "net"),
        ],
        "remarques": "",
    }
    valeurs.update(changements)
    return valeurs


PARCOURS = [
    reponse(pensee("Je lis d'abord le PDF et les mappings."),
            outil("t1", "lire_pdf"), outil("t2", "mappings_valides")),
    reponse(outil("t3", "chercher_od_historique", {
        "date_debut": "", "date_fin": "", "journal_code": "", "prefixe_compte": "62",
        "reference_contient": "", "libelle_contient": "", "partenaire_id": 0,
        "inclure_brouillons": False, "limite": 6})),
    reponse(outil("t4", "lire_lignes_ecriture", {"ecriture": "OD/2026/0912"})),
    reponse(pensee("Je reprends les comptes de septembre."), outil("t5", "proposer_od", proposition())),
]


@tagged("post_install", "-at_install")
class TestAgentOdSalaires(HubCase):
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
        cls.env["hub.mapping"].create({
            "dossier_id": cls.dossier.id, "libelle": "rémunérations  BRUTES",
            "compte_code": "620200", "compte_libelle": "Rémunérations employés",
        })
        cls.autre_comptable = new_test_user(
            cls.env, login="t_autre_comptable", groups="appalach_hub.group_hub_comptable"
        )

    def setUp(self):
        super().setUp()
        self.odoo = FauxOdooClient()
        self.odoo.ajouter_historique("OD/2026/0912", "2026-09-30", [
            (101, "Rémunérations brutes Jean Fictif", 4900.00, 0),
            (104, "ONSS", 0, 1990.00, 7),
            (105, "Net à payer", 0, 2910.00),
        ])
        self.startPatcher(patch(POST_ODOO, side_effect=self.odoo))
        self.requetes = []

    # ------------------------------------------------------------------

    def _claude(self, reponses=None, erreur=None):
        reponses = list(copy.deepcopy(reponses or PARCOURS))

        def envoyer(cle, corps, etape, remplacements=None):
            self.assertEqual(cle, CLE_CLAUDE)
            self.requetes.append((copy.deepcopy(corps), remplacements))
            if erreur:
                raise erreur
            return reponses.pop(0)
        return patch(ENVOYER, side_effect=envoyer)

    def _lancer(self, user=None):
        assistant = self.env["hub.od.salaires.wizard"].with_user(user or self.comptable).create({
            "dossier_id": self.dossier.id, "source": "fichier", "pdf": PDF_B64,
            "pdf_nom": "recap-2026-10.pdf",
        })
        action = assistant.action_lancer()
        return self.env["hub.run"].browse(action["res_id"])

    def _executer(self, run, reponses=None, erreur=None):
        with self._claude(reponses, erreur):
            run._executer()
        return run

    def _messages(self):
        return " ".join(self.dossier.message_ids.mapped(lambda m: str(m.body)))

    def _resultats(self, numero):
        """tool_result envoyés dans la requête ``numero`` (dernier message utilisateur)."""
        return self.requetes[numero][0]["messages"][-1]["content"]

    # ------------------------------------------------------------------

    def test_parcours_complet(self):
        run = self._lancer()
        self.assertEqual(run.etat, "en_file")
        self.assertEqual(run.modele, "claude-sonnet-5-5")
        self._executer(run)
        self.assertEqual(run.etat, "proposition", run.motif)

        # Requête : outils stricts, appel non forcé, réflexion adaptative résumée, cache.
        corps, remplacements = self.requetes[0]
        self.assertEqual(corps["model"], "claude-sonnet-5-5")
        self.assertEqual(corps["tool_choice"], {"type": "auto"})
        self.assertEqual(corps["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertEqual({o["name"] for o in corps["tools"]}, {
            "lire_pdf", "chercher_od_historique", "lire_lignes_ecriture", "plan_comptable",
            "journaux", "partenaires", "mappings_valides", "poser_question", "proposer_od"})
        self.assertTrue(all(o["strict"] for o in corps["tools"]))
        self.assertIn("en suivant la façon dont ce dossier encode", corps["messages"][0]["content"][0]["text"])
        self.assertEqual(remplacements, {MARQUEUR_PDF: PDF_B64})
        for corps_n, _r in self.requetes:
            caches = [b for m in corps_n["messages"] for b in m["content"] if "cache_control" in b]
            self.assertEqual(len(caches), 1)

        # Le PDF part en document dans le tool_result (marqueur substitué à l'envoi).
        pdf, mappings = self._resultats(1)
        self.assertEqual(pdf["content"][0]["source"]["data"], MARQUEUR_PDF)
        self.assertIn("620200", mappings["content"])
        # Les blocs de réflexion sont renvoyés tels quels.
        self.assertEqual(self.requetes[1][0]["messages"][1]["content"][0]["signature"], "signature-fictive")
        # Recherche : domaine construit par le hub, société appliquée.
        recherche = self.odoo.corps("account.move", "search_read")[0]
        self.assertIn(["line_ids.account_id.code", "=like", "62%"], recherche["domain"])
        self.assertIn(["state", "in", ["posted"]], recherche["domain"])
        self.assertIn(["company_id", "=", 1], recherche["domain"])
        self.assertEqual(recherche["context"], {"allowed_company_ids": [1]})
        lignes_lues = json.loads(self._resultats(3)[0]["content"])
        self.assertEqual(lignes_lues["lignes"][0]["libelle"], "Rémunérations brutes Jean Fictif")

        # Rien n'est écrit chez le client avant la validation.
        self.assertNotIn(("account.move", "create"), self.odoo.methodes())
        origines = dict(zip(run.ligne_ids.mapped("libelle"), run.ligne_ids.mapped("origine")))
        self.assertEqual(origines["Rémunérations brutes"], "mapping")
        self.assertEqual(origines["Net à payer"], "historique")
        self.assertEqual(origines["Précompte professionnel"], "pcmn")  # écriture citée non lue
        precompte = run.ligne_ids.filtered(lambda l: l.libelle == "Précompte professionnel")
        self.assertFalse(precompte.ecriture_reference)
        self.assertIn("non lue", precompte.justification)
        self.assertEqual(run.ligne_ids.filtered(lambda l: l.libelle == "ONSS patronal").partenaire_nom, "ONSS")
        self.assertTrue(run.controles_ok, run.controles_html)
        self.assertEqual((run.nb_etapes, run.tokens_entree, run.tokens_sortie), (4, 4000, 400))
        self.assertFalse(run.sudo().conversation)

        # Étapes visibles : appels du modèle, outils, contrôle ; raisonnement conservé.
        types = run.etape_ids.mapped("type")
        self.assertEqual(types.count("modele"), 4)
        self.assertEqual(run.etape_ids.filtered(lambda e: e.type == "controle").outil, "proposer_od")
        self.assertIn("Je lis d'abord le PDF", run.etape_ids[0].raisonnement)
        self.assertIn("OD/2026/0912", run.etape_ids.filtered(
            lambda e: e.outil == "lire_lignes_ecriture").parametres)

        # Validation humaine : création en brouillon, PDF joint, mappings alimentés.
        run.with_user(self.comptable).action_creer_od()
        self.assertEqual(run.etat, "od_creee")
        move = self.odoo.corps("account.move", "create")[0]["vals_list"][0]
        self.assertNotIn("state", move)
        self.assertEqual((move["journal_id"], move["date"], move["ref"]), (9, "2026-10-31", "Salaires 2026-10"))
        self.assertEqual(move["line_ids"][2][2], {"account_id": 104, "name": "ONSS travailleur",
                                                  "debit": 0.0, "credit": 653.5, "partner_id": 7})
        self.assertNotIn("partner_id", move["line_ids"][0][2])
        self.assertNotIn("action_post", " ".join(m for _m, m in self.odoo.methodes()))
        piece = self.odoo.corps("ir.attachment", "create")[0]["vals_list"][0]
        self.assertEqual(base64.b64decode(piece["datas"]), PDF)
        mappings = self.env["hub.mapping"]._index(self.dossier, "od_salaires")
        self.assertEqual(len(mappings), 6)
        self.assertEqual(mappings["net à payer"].compte_code, "455000")
        self.assertEqual(self.dossier.od_salaires_journal, "OD")
        self.assertFalse(run.sudo().pdf)
        messages = self._messages()
        self.assertIn("créée en brouillon", messages)
        self.assertNotIn(CLE_CLAUDE, messages)
        self.assertNotIn(CLE_DOSSIER, messages)

    def test_question_puis_reprise(self):
        run = self._lancer()
        self._executer(run, [reponse(
            outil("t1", "lire_pdf"),
            outil("t2", "poser_question", {"question": "Quel compte pour la prime fictive ?",
                                           "options": ["620200", "623000"], "contexte": "Absente de l'historique."}),
        )])
        self.assertEqual(run.etat, "en_attente")
        self.assertEqual(run.question, "Quel compte pour la prime fictive ?")
        self.assertEqual(run.question_options, "620200\n623000")
        self.assertNotIn(PDF_B64, run.sudo().conversation)
        self.assertTrue(run.sudo().pdf)

        with self.assertRaises(UserError):
            run.with_user(self.comptable).action_repondre()  # réponse vide
        run.with_user(self.comptable).write({"reponse": "620200"})
        run.with_user(self.comptable).action_repondre()
        self.assertEqual(run.etat, "en_file")
        self._executer(run, [reponse(outil("t3", "proposer_od", proposition()))])
        self.assertEqual(run.etat, "proposition", run.motif)
        contenu = self._resultats(1)
        self.assertEqual([b["type"] for b in contenu], ["tool_result", "tool_result", "text"])
        self.assertEqual(contenu[1]["tool_use_id"], "t2")
        self.assertEqual(contenu[2]["text"], "Réponse du comptable : 620200")
        # Le PDF déjà fourni n'est pas renvoyé ; l'historique de la conversation est conservé.
        self.assertEqual(len(self.requetes[1][0]["messages"]), 3)

    def test_anomalies_renvoyees_puis_corrigees(self):
        fausse = proposition()
        fausse["lignes"][5] = ligne("Net à payer", "999999", 0, 3346.40, "net")
        run = self._lancer()
        self._executer(run, [
            reponse(outil("t1", "proposer_od", fausse)),
            reponse(outil("t2", "proposer_od", proposition())),
        ])
        self.assertEqual(run.etat, "proposition", run.motif)
        erreur = self._resultats(1)[0]
        self.assertTrue(erreur["is_error"])
        self.assertIn("999999", erreur["content"])
        self.assertIn("déséquilibrée", erreur["content"])
        self.assertTrue(run.etape_ids.filtered(lambda e: e.type == "controle" and e.est_erreur))

    def test_doublon_arrete(self):
        self.odoo.nb_existantes = 1
        run = self._executer(self._lancer(), [reponse(outil("t1", "proposer_od", proposition()))])
        self.assertEqual(run.etat, "echec")
        self.assertIn("existe déjà", run.motif)
        self.assertFalse(run.sudo().pdf)
        self.assertIn("échec de l'agent", self._messages())

    def test_limite_etapes(self):
        self.env["ir.config_parameter"].sudo().set_int("appalach_hub.agent_max_etapes", 2)
        run = self._lancer()
        self._executer(run, [reponse(outil(f"t{i}", "journaux")) for i in range(3)])
        self.assertEqual((run.etat, run.nb_etapes), ("echec", 2))
        self.assertIn("Limite de 2 étapes", run.motif)
        self.assertEqual(len(self.requetes), 2)

    def test_budget_tokens(self):
        self.env["ir.config_parameter"].sudo().set_int("appalach_hub.agent_budget_tokens", 1500)
        run = self._lancer()
        self._executer(run, [reponse(outil(f"t{i}", "journaux")) for i in range(3)])
        self.assertEqual(run.etat, "echec")
        self.assertIn("Budget de 1500 tokens", run.motif)
        self.assertEqual(len(self.requetes), 2)

    def test_refus_sans_repli(self):
        run = self._executer(self._lancer(), erreur=ClaudeErreur("Claude a refusé (étape 1). Aucun repli."))
        self.assertEqual(run.etat, "echec")
        self.assertIn("refusé", run.motif)
        self.assertEqual(len(self.requetes), 1)
        self.assertEqual(run.etape_ids.type, "erreur")

    def test_fin_sans_outil_final(self):
        texte = {"type": "text", "text": "Voici mon analyse."}
        run = self._executer(self._lancer(), [reponse(texte, stop="end_turn"),
                                              reponse(texte, stop="end_turn")])
        self.assertEqual(run.etat, "echec")
        self.assertIn("sans proposer", run.motif)
        self.assertEqual(self._resultats(1)[0]["text"], RELANCE)

    def test_anonymisation(self):
        self.dossier.anonymiser_historique = True
        run = self._lancer()
        self._executer(run, PARCOURS)
        lignes = json.loads(self._resultats(3)[0]["content"])["lignes"]
        self.assertEqual(lignes[0]["libelle"], "Rémunérations employés")
        self.assertNotIn("Jean Fictif", json.dumps(self.requetes[-1][0]))

    def test_compte_sans_code_refuse(self):
        fausse = proposition()
        fausse["lignes"][1]["code_compte"] = "SANS-CODE-199"
        run = self._executer(self._lancer(), [reponse(outil("t1", "proposer_od", fausse)),
                                              reponse(outil("t2", "proposer_od", proposition()))])
        self.assertIn("SANS-CODE-199", self._resultats(1)[0]["content"])
        self.assertEqual(run.etat, "proposition")

    def test_modification_manuelle_et_creation_refusee(self):
        run = self._executer(self._lancer())
        net = run.ligne_ids.filtered(lambda l: l.libelle == "Net à payer")
        net.with_user(self.comptable).write({"compte_code": "455100"})
        self.assertEqual((net.origine, net.compte_nom), ("manuel", False))
        with self.assertRaises(AccessError):
            net.with_user(self.comptable).write({"debit": 1.0})
        action = run.with_user(self.comptable).action_creer_od()
        self.assertEqual(action["params"]["type"], "danger")
        self.assertIn("455100", action["params"]["message"])
        self.assertEqual(run.etat, "proposition")
        self.assertNotIn(("account.move", "create"), self.odoo.methodes())

    def test_rejet(self):
        run = self._executer(self._lancer())
        run.with_user(self.comptable).action_rejeter()
        self.assertEqual(run.etat, "rejete")
        self.assertFalse(run.sudo().pdf)
        with self.assertRaises(UserError):
            run.with_user(self.comptable).action_creer_od()

    def test_droits(self):
        run = self._lancer()
        with self.assertRaises(AccessError):
            run.with_user(self.autre_comptable).read(["etat"])
        with self.assertRaises(AccessError):
            run.with_user(self.comptable).write({"etat": "proposition"})
        with self.assertRaises(AccessError):
            self.env["hub.run"].with_user(self.comptable).create({"dossier_id": self.dossier.id})
        with self.assertRaises(AccessError):
            run.with_user(self.comptable).read(["conversation"])
        self.assertEqual(run.with_user(self.gestionnaire).etat, "en_file")
        self._executer(run)
        with self.assertRaises(AccessError):
            run.with_user(self.autre_comptable).action_creer_od()
        with self.assertRaises(AccessError):
            run.etape_ids[0].with_user(self.comptable).write({"resultat_resume": "x"})

    def test_cron(self):
        run = self._lancer()
        commit = MagicMock()
        with self._claude(), patch.object(type(self.env["ir.cron"]), "_commit_progress", commit):
            self.env["hub.run"]._cron_traiter()
        self.assertEqual(run.etat, "proposition", run.motif)
        self.assertTrue(commit.called)
