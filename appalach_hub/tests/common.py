"""Outils communs aux tests : paire RSA de test, faux serveurs HTTP. Données fictives."""

import json
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from odoo.tests import TransactionCase, new_test_user

from ..models.hub_secrets import OPTION_CLE_PRIVEE, OPTION_CLE_PUBLIQUE


def generer_paire_pem(taille=3072):
    privee = rsa.generate_private_key(public_exponent=65537, key_size=taille)
    pem_privee = privee.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pem_publique = privee.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return privee, pem_publique, pem_privee


def reponse_http(status_code=200, donnees=None, contenu=b""):
    reponse = MagicMock()
    reponse.status_code = status_code
    reponse.json.return_value = donnees
    reponse.content = contenu
    reponse.text = json.dumps(donnees) if donnees is not None else ""
    return reponse


class FauxOdooClient:
    """Simule l'API JSON-2 d'un Odoo client et enregistre chaque appel."""

    def __init__(self):
        self.appels = []
        self.nb_existantes = 0
        self.comptes = [
            {"id": 101, "code": "620200", "name": "Rémunérations employés", "account_type": "expense"},
            {"id": 102, "code": "621000", "name": "Cotisations patronales", "account_type": "expense"},
            {"id": 103, "code": "453000", "name": "Précomptes retenus", "account_type": "liability_current"},
            {"id": 104, "code": "454000", "name": "ONSS", "account_type": "liability_current"},
            {"id": 105, "code": "455000", "name": "Rémunérations à payer", "account_type": "liability_current"},
        ]
        self.journaux = [{"id": 9, "code": "OD", "name": "Opérations diverses"}]

    def __call__(self, url, headers=None, data=None, timeout=None):
        _, _, _json2, modele, methode = urlsplit(url).path.split("/")
        corps = json.loads(data)
        self.appels.append((modele, methode, corps, headers))
        resultats = {
            ("account.account", "search_read"): self.comptes,
            ("account.journal", "search_read"): self.journaux,
            ("account.move", "search_count"): self.nb_existantes,
            ("account.move", "create"): [42],
            ("ir.attachment", "create"): [7],
        }
        if (modele, methode) not in resultats:
            return reponse_http(404, {"name": "NotFound", "message": methode})
        return reponse_http(200, resultats[(modele, methode)])

    def methodes(self):
        return [(m, meth) for m, meth, _c, _h in self.appels]


class HubCase(TransactionCase):
    """Base : utilisateurs, paire RSA de test branchée sur hub.secrets._lire_pem."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cle_privee, cls.pem_publique, cls.pem_privee = generer_paire_pem()
        pems = {OPTION_CLE_PUBLIQUE: cls.pem_publique, OPTION_CLE_PRIVEE: cls.pem_privee}
        cls.startClassPatcher(patch.object(
            type(cls.env["hub.secrets"]), "_lire_pem", side_effect=lambda option: pems[option]
        ))
        cls.comptable = new_test_user(
            cls.env, login="t_comptable", groups="appalach_hub.group_hub_comptable"
        )
        cls.gestionnaire = new_test_user(
            cls.env, login="t_gestionnaire", groups="appalach_hub.group_hub_gestionnaire"
        )
        cls.admin = new_test_user(
            cls.env, login="t_admin", groups="base.group_system,appalach_hub.group_hub_gestionnaire"
        )
        cls.moteur = new_test_user(
            cls.env, login="t_moteur", groups="appalach_hub.group_hub_moteur"
        )
