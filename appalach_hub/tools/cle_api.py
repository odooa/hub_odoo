"""Outils de la clé API d'un dossier : test de connexion JSON-2 et chiffrement.

Fonctions pures, sans accès à l'ORM, testables sans réseau.
Règle absolue : la clé en clair n'apparaît jamais dans un message, une exception
ou un journal.
"""

import base64
import hashlib

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

TIMEOUT_SECONDES = 10
TAILLE_MIN_CLE_RSA = 3072


class CleApiErreur(Exception):
    """Erreur présentable à l'utilisateur ; ne contient jamais la clé."""


def tester_connexion_json2(url, base, cle):
    """Vérifie que la clé ouvre une session sur l'Odoo du dossier (API JSON-2, Odoo 19+).

    Appelle ``res.users/context_get`` (lecture seule, sans effet de bord).
    """
    point = url.rstrip("/") + "/json/2/res.users/context_get"
    try:
        reponse = requests.post(
            point,
            json={},
            headers={
                "Authorization": f"bearer {cle}",
                "X-Odoo-Database": base,
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=TIMEOUT_SECONDES,
        )
    except requests.exceptions.Timeout:
        raise CleApiErreur("Délai dépassé en joignant l'Odoo du dossier.") from None
    except requests.exceptions.SSLError:
        raise CleApiErreur("Certificat HTTPS de l'Odoo du dossier invalide.") from None
    except requests.exceptions.RequestException:
        # Ne pas chaîner l'exception d'origine : elle peut contenir la requête.
        raise CleApiErreur("Impossible de joindre l'Odoo du dossier.") from None

    if reponse.status_code in (401, 403):
        raise CleApiErreur("Clé API refusée par l'Odoo du dossier.")
    if reponse.status_code == 404:
        raise CleApiErreur(
            "Point d'accès JSON-2 introuvable (Odoo 19 ou plus requis, ou base incorrecte)."
        )
    if reponse.status_code != 200:
        raise CleApiErreur(
            f"Réponse inattendue de l'Odoo du dossier (HTTP {reponse.status_code})."
        )


def charger_cle_publique(pem):
    """Charge une clé publique RSA PEM (bytes) et vérifie sa taille minimale."""
    try:
        cle_publique = serialization.load_pem_public_key(pem)
    except (ValueError, TypeError):
        raise CleApiErreur("Clé publique du moteur illisible (PEM attendu).") from None
    if not isinstance(cle_publique, rsa.RSAPublicKey):
        raise CleApiErreur("La clé publique du moteur doit être une clé RSA.")
    if cle_publique.key_size < TAILLE_MIN_CLE_RSA:
        raise CleApiErreur(
            f"Clé publique du moteur trop courte ({cle_publique.key_size} bits, "
            f"minimum {TAILLE_MIN_CLE_RSA})."
        )
    return cle_publique


def chiffrer_rsa_oaep(cle_publique, texte):
    """Chiffre ``texte`` (str) en RSA-OAEP / MGF1-SHA256 / SHA-256 ; renvoie du base64 (str)."""
    chiffre = cle_publique.encrypt(
        texte.encode("utf-8"),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(chiffre).decode("ascii")


def empreinte_cle_publique(cle_publique):
    """Empreinte SHA-256 (hex) de la clé publique, format DER SubjectPublicKeyInfo."""
    der = cle_publique.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()
