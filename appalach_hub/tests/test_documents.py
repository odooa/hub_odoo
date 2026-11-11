"""Source « Documents Odoo » (module Documents de l'Odoo client, Enterprise 19), par JSON-2.
Claude et l'Odoo client simulés. Données fictives."""

import base64
import copy
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from ..tools.agent_outils import MARQUEUR_PIECE
from .common import FauxOdooClient, HubCase
from .test_od_salaires import CLE_CLAUDE, CLE_DOSSIER, ENVOYER, POST_ODOO, outil, proposition, reponse

PDF = b"%PDF-1.4 recapitulatif Documents fictif"
PDF_B64 = base64.b64encode(PDF).decode()


def piece(ident, nom, dossier=50, mimetype="application/pdf", tags=(), lie=False):
    return {"id": ident, "name": nom, "folder_id": [dossier, "Salaires"], "mimetype": mimetype,
            "file_size": 1000, "tag_ids": list(tags), "res_model": "account.move" if lie else False,
            "res_id": 77 if lie else 0, "create_date": "2026-11-05 08:00:00",
            "access_url": f"https://client.invalid/odoo/documents/jeton{ident}",
            "attachment_id": [900 + ident % 100, nom]}


@tagged("post_install", "-at_install")
class TestDocuments(HubCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        secrets = cls.env["hub.secrets"]
        secrets._definir_secret_global("anthropic_api_key", CLE_CLAUDE)
        chiffre, empreinte = secrets._chiffrer(CLE_DOSSIER)
        cls.dossier = cls.env["hub.dossier"].create({
            "name": "Dossier Documents", "code": "DOC1", "tache_od_salaires": True,
            "source_pieces": "documents", "odoo_url": "https://client.invalid",
            "odoo_base": "client", "comptable_ids": [(4, cls.comptable.id)],
        })
        cls.dossier.sudo().write({"cle_api_chiffree": chiffre, "cle_api_empreinte": empreinte})

    def setUp(self):
        super().setUp()
        self.odoo = FauxOdooClient()
        self.odoo.dossiers_documents = [
            {"id": 40, "name": "Paie", "folder_id": False, "alias_email": False},
            {"id": 50, "name": "Salaires", "folder_id": [40, "Paie"],
             "alias_email": "salaires@client.invalid"},
            {"id": 60, "name": "Traité", "folder_id": [40, "Paie"], "alias_email": False},
        ]
        self.odoo.etiquettes = [{"id": 5, "name": "Traité"}, {"id": 6, "name": "Urgent"}]
        self.odoo.pieces = [
            piece(300, "recap-2026-10.pdf"),
            piece(301, "recap-2026-09.pdf", tags=[5]),
            piece(302, "photo.jpg", mimetype="image/jpeg"),
            piece(303, "ailleurs.pdf", dossier=99),
            piece(304, "deja-liee.pdf", lie=True),
        ]
        self.odoo.contenus = {300: PDF_B64, 301: PDF_B64}
        self.startPatcher(patch(POST_ODOO, side_effect=self.odoo))
        self.requetes = []

    # ------------------------------------------------------------------

    def _configurer(self, traitement="etiquette"):
        assistant = self.dossier.with_user(self.gestionnaire).action_configurer_documents()
        wizard = self.env["hub.documents.config.wizard"].with_user(self.gestionnaire).browse(
            assistant["res_id"])
        copies = {c.client_id: c for c in wizard.dossier_copie_ids}
        wizard.write({
            "dossier_od_salaires_id": copies[50].id, "traitement": traitement,
            "etiquette_traite_id": wizard.etiquette_copie_ids.filtered(lambda e: e.client_id == 5).id,
            "dossier_traite_id": copies[60].id,
        })
        wizard.action_valider()
        return wizard

    def _claude(self, reponses):
        reponses = list(copy.deepcopy(reponses))

        def envoyer(cle, corps, etape, remplacements=None):
            self.requetes.append((copy.deepcopy(corps), dict(remplacements or {})))
            return reponses.pop(0)
        return patch(ENVOYER, side_effect=envoyer)

    def _lancer(self, imposee=None):
        action = self.dossier.with_user(self.comptable).action_preparer_od_salaires()
        assistant = self.env["hub.od.salaires.wizard"].with_user(self.comptable).browse(action["res_id"])
        if imposee:
            assistant.piece_imposee_id = assistant.piece_copie_ids.filtered(
                lambda p: p.client_id == imposee)
        run_action = assistant.action_lancer()
        return assistant, self.env["hub.run"].browse(run_action["res_id"])

    def _executer(self, run, reponses):
        with self._claude(reponses):
            run._executer()
        return run

    def _lister(self, ident="t1"):
        return outil(ident, "lister_pieces", {"recherche": "", "inclure_traitees": False, "limite": 10})

    def _resultats(self, numero):
        return self.requetes[numero][0]["messages"][-1]["content"]

    # ------------------------------------------------------------------

    def test_configuration_par_listes(self):
        self._configurer()
        ligne = self.dossier.documents_dossier_ids
        self.assertEqual((ligne.tache, ligne.dossier_client_id, ligne.dossier_chemin, ligne.alias_email),
                         ("od_salaires", 50, "Paie / Salaires", "salaires@client.invalid"))
        self.assertEqual((self.dossier.documents_traitement, self.dossier.documents_etiquette_traite_id,
                          self.dossier.documents_etiquette_traite_nom), ("etiquette", 5, "Traité"))
        self.assertIn("Documents configuré", " ".join(self.dossier.message_ids.mapped(lambda m: str(m.body))))
        # Lecture dans le contexte de la société, raccourcis exclus.
        lecture = self.odoo.corps("documents.document", "search_read")[0]
        self.assertIn(["shortcut_document_id", "=", False], lecture["domain"])
        self.assertEqual(lecture["context"], {"allowed_company_ids": [1]})

    def test_configuration_sans_saisie_libre(self):
        with self.assertRaises(AccessError):
            self.dossier.with_user(self.comptable).action_configurer_documents()
        wizard = self._configurer()
        with self.assertRaises(UserError):
            self.env["hub.documents.copie.dossier"].with_user(self.gestionnaire).create({
                "wizard_id": wizard.id, "client_id": 999, "name": "Inventé"})
        with self.assertRaises(AccessError):
            self.dossier.with_user(self.gestionnaire).write({"documents_dossier_traite_id": 999})
        with self.assertRaises(AccessError):
            self.env["hub.dossier.documents"].with_user(self.gestionnaire).create({
                "dossier_id": self.dossier.id, "tache": "od_salaires", "dossier_client_id": 999,
                "dossier_chemin": "Inventé"})

    def test_parcours_documents(self):
        self._configurer()
        assistant, run = self._lancer()
        # Choix facultatif : pièces PDF non traitées et non liées du dossier configuré.
        self.assertEqual(assistant.piece_copie_ids.mapped("client_id"), [300])
        self.assertEqual((run.source, run.etat), ("documents", "en_file"))
        self.assertFalse(run.sudo().pdf)

        self._executer(run, [
            reponse(self._lister()),
            reponse(outil("t2", "lire_piece", {"document_id": 300})),
            reponse(outil("t3", "proposer_od", proposition(document_id=300))),
        ])
        self.assertEqual(run.etat, "proposition", run.motif)
        noms = {o["name"] for o in self.requetes[0][0]["tools"]}
        self.assertTrue({"lister_pieces", "lire_piece"} <= noms)
        self.assertNotIn("lire_pdf", noms)
        self.assertIn("Paie / Salaires", self.requetes[0][0]["messages"][0]["content"][0]["text"])
        liste = self._resultats(1)[0]["content"]
        self.assertIn("recap-2026-10.pdf", liste)
        self.assertNotIn("recap-2026-09.pdf", liste)  # marquée traitée
        self.assertNotIn("ailleurs.pdf", liste)       # autre dossier
        bloc = self._resultats(2)[0]["content"][0]
        self.assertEqual(bloc["source"]["data"], MARQUEUR_PIECE.format(300))
        self.assertEqual(self.requetes[2][1][MARQUEUR_PIECE.format(300)], PDF_B64)
        # Traçabilité : nom, date, lien direct du document.
        self.assertEqual((run.piece_document_id, run.piece_nom), (300, "recap-2026-10.pdf"))
        self.assertEqual(run.piece_lien, "https://client.invalid/odoo/documents/jeton300")
        self.assertEqual(str(run.piece_date), "2026-11-05 08:00:00")

        run.with_user(self.comptable).action_creer_od()
        self.assertEqual((run.etat, run.piece_statut), ("od_creee", "liee_traitee"))
        self.assertNotIn(("ir.attachment", "create"), self.odoo.methodes())  # pas de doublon
        ecritures = self.odoo.corps("documents.document", "write")
        self.assertEqual(ecritures[0]["ids"], [300])
        self.assertEqual(ecritures[0]["vals"], {"res_model": "account.move", "res_id": 42})
        self.assertEqual(ecritures[1]["vals"], {"tag_ids": [[4, 5]]})
        self.assertEqual(self.odoo.corps("account.move", "write")[0]["vals"],
                         {"message_main_attachment_id": 900})
        self.assertIn("recap-2026-10.pdf", " ".join(self.dossier.message_ids.mapped(lambda m: str(m.body))))

    def test_marquage_par_dossier(self):
        self._configurer(traitement="dossier")
        _assistant, run = self._lancer()
        self._executer(run, [reponse(outil("t1", "lire_piece", {"document_id": 300})),
                             reponse(outil("t2", "proposer_od", proposition(document_id=300)))])
        run.with_user(self.comptable).action_creer_od()
        self.assertEqual(self.odoo.corps("documents.document", "write")[1]["vals"], {"folder_id": 60})

    def test_echec_du_marquage_signale(self):
        self._configurer()
        self.odoo.echec_marquage = True
        _assistant, run = self._lancer()
        self._executer(run, [reponse(outil("t1", "lire_piece", {"document_id": 300})),
                             reponse(outil("t2", "proposer_od", proposition(document_id=300)))])
        run.with_user(self.comptable).action_creer_od()
        self.assertEqual((run.etat, run.piece_statut), ("od_creee", "avertissement"))
        self.assertIn("non marquée traitée", run.piece_avertissement)
        self.assertEqual(run.move_id_client, 42)

    def test_pieces_refusees(self):
        self._configurer()
        _assistant, run = self._lancer()
        self._executer(run, [
            reponse(outil("t1", "lire_piece", {"document_id": 302}),
                    outil("t2", "lire_piece", {"document_id": 303}),
                    outil("t3", "lire_piece", {"document_id": 304}),
                    outil("t4", "lire_pdf")),
            reponse(outil("t5", "proposer_od", proposition(document_id=301))),
            reponse(outil("t6", "lire_piece", {"document_id": 300})),
            reponse(outil("t7", "proposer_od", proposition(document_id=300))),
        ])
        type_, ailleurs, liee, pdf = self._resultats(1)
        self.assertTrue(all(r.get("is_error") for r in (type_, ailleurs, liee, pdf)))
        self.assertIn("non pris en charge", type_["content"])
        self.assertIn("pas dans le dossier", ailleurs["content"])
        self.assertIn("déjà liée", liee["content"])
        self.assertIn("Outil inconnu", pdf["content"])
        self.assertIn("lue avec lire_piece", self._resultats(2)[0]["content"])
        self.assertEqual(run.etat, "proposition", run.motif)

    def test_piece_imposee(self):
        self._configurer()
        _assistant, run = self._lancer(imposee=300)
        self.assertEqual(run.piece_imposee_id, 300)
        self._executer(run, [
            reponse(self._lister(), outil("t2", "lire_piece", {"document_id": 301})),
            reponse(outil("t3", "lire_piece", {"document_id": 300})),
            reponse(outil("t4", "proposer_od", proposition(document_id=300))),
        ])
        liste, refus = self._resultats(1)
        self.assertIn("recap-2026-10.pdf", liste["content"])
        self.assertIn("Seule la pièce imposée (300)", refus["content"])
        self.assertIn("impose la pièce 300", self.requetes[0][0]["messages"][0]["content"][0]["text"])
        self.assertEqual(run.etat, "proposition", run.motif)

    def test_reprise_relit_la_piece(self):
        self._configurer()
        _assistant, run = self._lancer()
        self._executer(run, [reponse(
            outil("t1", "lire_piece", {"document_id": 300}),
            outil("t2", "poser_question", {"question": "Prime ?", "options": [], "contexte": ""}))])
        self.assertEqual(run.etat, "en_attente")
        self.assertNotIn(PDF_B64, run.sudo().conversation)
        lectures = len(self.odoo.corps("documents.document", "read"))
        run.with_user(self.comptable).write({"reponse": "Pas de prime."})
        run.with_user(self.comptable).action_repondre()
        self._executer(run, [reponse(outil("t3", "proposer_od", proposition(document_id=300)))])
        self.assertEqual(run.etat, "proposition", run.motif)
        self.assertEqual(len(self.odoo.corps("documents.document", "read")), lectures + 1)
        self.assertEqual(self.requetes[-1][1][MARQUEUR_PIECE.format(300)], PDF_B64)

    def test_source_documents_non_configuree(self):
        action = self.dossier.with_user(self.comptable).action_preparer_od_salaires()
        assistant = self.env["hub.od.salaires.wizard"].with_user(self.comptable).browse(action["res_id"])
        self.assertIn("aucun dossier Documents", assistant.avertissement)
        with self.assertRaises(UserError):
            assistant.action_lancer()
