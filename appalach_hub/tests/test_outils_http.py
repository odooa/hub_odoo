"""Tests des outils sans ORM : Claude (HTTP), Nextcloud, JSON-2, chiffrement, contrôles."""

import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from odoo.tests import BaseCase, tagged

from ..tools import anthropic_http, cle_api, nextcloud, od_salaires as ods
from ..tools.odoo_json2 import OdooClientErreur, OdooJson2
from .common import generer_paire_pem, reponse_http

POST_CLAUDE = "odoo.addons.appalach_hub.tools.anthropic_http.requests.post"
GET_CLAUDE = "odoo.addons.appalach_hub.tools.anthropic_http.requests.get"


def message_claude(texte=None, stop_reason="end_turn", stop_details=None):
    return {
        "type": "message",
        "content": [{"type": "text", "text": texte}] if texte is not None else [],
        "stop_reason": stop_reason,
        "stop_details": stop_details,
    }


@tagged("post_install", "-at_install")
class TestClaudeHttp(BaseCase):
    def test_requete_et_reponse(self):
        donnees = {"periode": "2026-10"}
        with patch(POST_CLAUDE, return_value=reponse_http(200, message_claude(json.dumps(donnees)))) as post:
            resultat = anthropic_http.generer_json(
                "cle-fictive", "claude-sonnet-5-5", [{"type": "text", "text": "x"}],
                ods.SCHEMA_EXTRACTION, "extraction",
            )
        self.assertEqual(resultat, donnees)
        url = post.call_args.args[0]
        entetes = post.call_args.kwargs["headers"]
        corps = json.loads(post.call_args.kwargs["data"])
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(entetes["anthropic-version"], "2026-10-21")
        self.assertEqual(entetes["x-api-key"], "cle-fictive")
        self.assertEqual(corps["model"], "claude-sonnet-5-5")
        self.assertEqual(corps["output_config"]["format"]["type"], "json_schema")
        self.assertFalse(corps["output_config"]["format"]["schema"]["additionalProperties"])
        self.assertNotIn("fallbacks", corps)
        self.assertNotIn("temperature", corps)

    def test_refus_sans_repli(self):
        refus = message_claude(stop_reason="refusal",
                               stop_details={"category": "general_harms", "explanation": "Fictif."})
        with patch(POST_CLAUDE, return_value=reponse_http(200, refus)) as post, \
                self.assertRaisesRegex(anthropic_http.ClaudeErreur, "refusé.*general_harms.*Aucun repli"):
            anthropic_http.generer_json("k", "m", [], {}, "extraction")
        self.assertEqual(post.call_count, 1)

    def test_tronque_et_erreurs_http(self):
        cas = [
            (reponse_http(200, message_claude("{", stop_reason="max_tokens")), "tronquée"),
            (reponse_http(200, message_claude("pas du json")), "JSON invalide"),
            (reponse_http(401, {"error": {"message": "x"}}), "clé API refusée"),
            (reponse_http(404, {"error": {"message": "x"}}), "modèle inconnu"),
            (reponse_http(429, {"error": {"message": "x"}}), "limite de débit"),
            (reponse_http(529, {"error": {"message": "x"}}), "indisponible"),
        ]
        for reponse, motif in cas:
            with self.subTest(motif=motif), patch(POST_CLAUDE, return_value=reponse), \
                    self.assertRaisesRegex(anthropic_http.ClaudeErreur, motif):
                anthropic_http.generer_json("cle-secrete", "m", [], {}, "extraction")

    def test_tester_cle(self):
        with patch(GET_CLAUDE, return_value=reponse_http(200, {"data": []})) as get:
            anthropic_http.tester_cle("k")
        self.assertTrue(get.call_args.args[0].endswith("/v1/models"))
        with patch(GET_CLAUDE, return_value=reponse_http(401, {})), \
                self.assertRaisesRegex(anthropic_http.ClaudeErreur, "refusée"):
            anthropic_http.tester_cle("k")


PROPFIND = b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response><d:href>/remote.php/dav/files/agent/clients/test/salaires/</d:href>
    <d:propstat><d:prop><d:getlastmodified>Mon, 09 Nov 2026 10:00:00 GMT</d:getlastmodified>
    <d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
  <d:response><d:href>/remote.php/dav/files/agent/clients/test/salaires/r%C3%A9cap%202026-09.pdf</d:href>
    <d:propstat><d:prop><d:getlastmodified>Mon, 05 Oct 2026 08:00:00 GMT</d:getlastmodified>
    <d:resourcetype/></d:prop></d:propstat></d:response>
  <d:response><d:href>/remote.php/dav/files/agent/clients/test/salaires/recap-2026-10.pdf</d:href>
    <d:propstat><d:prop><d:getlastmodified>Wed, 04 Nov 2026 08:00:00 GMT</d:getlastmodified>
    <d:resourcetype/></d:prop></d:propstat></d:response>
  <d:response><d:href>/remote.php/dav/files/agent/clients/test/salaires/notes.txt</d:href>
    <d:propstat><d:prop><d:getlastmodified>Thu, 05 Nov 2026 08:00:00 GMT</d:getlastmodified>
    <d:resourcetype/></d:prop></d:propstat></d:response>
</d:multistatus>"""


@tagged("post_install", "-at_install")
class TestNextcloud(BaseCase):
    def test_pdf_le_plus_recent(self):
        nc = nextcloud.Nextcloud("https://nc.invalid/", "agent", "mdp")
        with patch("odoo.addons.appalach_hub.tools.nextcloud.requests.request",
                   return_value=reponse_http(207, contenu=PROPFIND)) as requete:
            fichier = nc.pdf_le_plus_recent("/clients/test/salaires")
        self.assertEqual(fichier.nom, "recap-2026-10.pdf")
        self.assertEqual(requete.call_args.args[:2], (
            "PROPFIND", "https://nc.invalid/remote.php/dav/files/agent/clients/test/salaires/"))
        self.assertEqual(requete.call_args.kwargs["headers"]["Depth"], "1")

    def test_erreurs(self):
        nc = nextcloud.Nextcloud("https://nc.invalid", "agent", "mdp-secret")
        for statut, motif in ((401, "identifiants"), (404, "introuvable"), (500, "inattendue")):
            with self.subTest(statut=statut), \
                    patch("odoo.addons.appalach_hub.tools.nextcloud.requests.request",
                          return_value=reponse_http(statut)), \
                    self.assertRaisesRegex(nextcloud.NextcloudErreur, motif):
                nc.pdf_le_plus_recent("x")


@tagged("post_install", "-at_install")
class TestOdooJson2(BaseCase):
    def test_appel_et_erreur(self):
        client = OdooJson2("https://client.invalid/", "base", "cle-secrete")
        client.utiliser_societe(3)
        with patch("odoo.addons.appalach_hub.tools.odoo_json2.requests.post",
                   return_value=reponse_http(200, 2)) as post:
            self.assertEqual(client.od_existante("Salaires 2026-10"), 2)
        self.assertEqual(post.call_args.args[0], "https://client.invalid/json/2/account.move/search_count")
        corps = json.loads(post.call_args.kwargs["data"])
        self.assertIn(["state", "in", ["draft", "posted"]], corps["domain"])
        self.assertEqual(corps["context"], {"allowed_company_ids": [3]})
        self.assertEqual(post.call_args.kwargs["headers"]["X-Odoo-Database"], "base")

        erreur = {"name": "odoo.exceptions.AccessError", "message": "Accès refusé"}
        with patch("odoo.addons.appalach_hub.tools.odoo_json2.requests.post",
                   return_value=reponse_http(403, erreur)), \
                self.assertRaisesRegex(OdooClientErreur, "HTTP 403.*Accès refusé") as contexte:
            client.comptes()
        self.assertNotIn("cle-secrete", str(contexte.exception))


@tagged("post_install", "-at_install")
class TestChiffrement(BaseCase):
    def test_aller_retour_et_mauvaise_cle(self):
        _privee, pem_pub, pem_priv = generer_paire_pem()
        _autre, _pub2, pem_priv2 = generer_paire_pem()
        publique = cle_api.charger_cle_publique(pem_pub)
        chiffre = cle_api.chiffrer_rsa_oaep(publique, "secret-fictif")
        self.assertEqual(
            cle_api.dechiffrer_rsa_oaep(cle_api.charger_cle_privee(pem_priv), chiffre), "secret-fictif"
        )
        with self.assertRaisesRegex(cle_api.CleApiErreur, "ne correspond pas"):
            cle_api.dechiffrer_rsa_oaep(cle_api.charger_cle_privee(pem_priv2), chiffre)
        with self.assertRaises(cle_api.CleApiErreur):
            cle_api.charger_cle_privee(b"pas une cle")


def lignes_fictives():
    def ligne(libelle, montant, sens, categorie, code="X"):
        return {"libelle": libelle, "montant": Decimal(montant), "sens": sens,
                "categorie": categorie, "code": code}
    return [
        ligne("Rémunérations brutes", "5000.00", "debit", "brut"),
        ligne("Cotisations patronales", "1353.25", "debit", "autre"),
        ligne("ONSS travailleur", "653.50", "credit", "onss"),
        ligne("ONSS patronal", "1353.25", "credit", "onss"),
        ligne("Précompte professionnel", "1000.00", "credit", "precompte"),
        ligne("Net à payer", "3346.50", "credit", "net"),
    ]


TOTAUX = {"brut": 5000.00, "onss": 2006.75, "precompte": 1000.00, "net": 3346.50}


@tagged("post_install", "-at_install")
class TestControles(BaseCase):
    def test_ok(self):
        self.assertEqual(ods.controler(lignes_fictives(), TOTAUX), [])

    def test_ecart_detaille(self):
        anomalies = ods.controler(lignes_fictives(), TOTAUX | {"onss": 2000.00})
        self.assertEqual(len(anomalies), 1)
        self.assertIn("attendu 2 000,00, obtenu 2 006,75 (écart 6,75)", anomalies[0]["message"])
        self.assertEqual(anomalies[0]["details"], [
            "ONSS travailleur : 653,50 (credit)", "ONSS patronal : 1 353,25 (credit)"])

    def test_desequilibre_sans_compte_tolerance(self):
        lignes = lignes_fictives()
        lignes[0]["montant"] = Decimal("5000.10")
        lignes[1]["code"] = None
        messages = [a["message"] for a in ods.controler(lignes, TOTAUX | {"brut": 5000.10})]
        self.assertTrue(any("déséquilibrée" in m and "écart 0,10" in m for m in messages))
        self.assertTrue(any("sans compte" in m for m in messages))
        self.assertEqual(ods.controler(lignes_fictives(), TOTAUX | {"net": 3346.51}), [])

    def test_reference_date_move(self):
        self.assertEqual(ods.reference_od("2026-10"), "Salaires 2026-10")
        self.assertEqual(ods.date_fin_periode("2028-02"), date(2028, 2, 29))
        with self.assertRaises(ods.OdSalairesErreur):
            ods.reference_od("2026-13")
        valeurs = ods.valeurs_move("2026-10", 9, [
            {"account_id": 101, "libelle": "Brut", "montant": Decimal("10.00"), "sens": "debit"}])
        self.assertEqual(valeurs["move_type"], "entry")
        self.assertEqual(valeurs["date"], "2026-10-31")
        self.assertNotIn("state", valeurs)
        self.assertEqual(valeurs["line_ids"], [[0, 0, {"account_id": 101, "name": "Brut",
                                                       "debit": 10.0, "credit": 0.0}]])
