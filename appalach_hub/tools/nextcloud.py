"""Accès WebDAV à Nextcloud par requests (repris du prototype hub_compta)."""

import xml.etree.ElementTree as ET
from collections import namedtuple
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlsplit

import requests

TIMEOUT = 30

FichierDistant = namedtuple("FichierDistant", "nom chemin modifie_le")


class NextcloudErreur(Exception):
    """Erreur présentable à l'utilisateur ; ne contient jamais le mot de passe."""


class Nextcloud:
    def __init__(self, url, utilisateur, mot_de_passe):
        self.url = (url or "").rstrip("/")
        self.utilisateur = utilisateur
        self.auth = (utilisateur, mot_de_passe)

    def _url_dossier(self, dossier):
        chemin = quote((dossier or "").strip("/"))
        base = f"{self.url}/remote.php/dav/files/{quote(self.utilisateur)}"
        return f"{base}/{chemin}/" if chemin else f"{base}/"

    def _propfind(self, url, profondeur):
        corps = (
            '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
            "<d:getlastmodified/><d:resourcetype/></d:prop></d:propfind>"
        )
        try:
            reponse = requests.request(
                "PROPFIND", url, data=corps, auth=self.auth, timeout=TIMEOUT,
                headers={"Depth": str(profondeur), "Content-Type": "application/xml"},
            )
        except requests.exceptions.RequestException:
            raise NextcloudErreur("Nextcloud injoignable.") from None
        if reponse.status_code == 401:
            raise NextcloudErreur("Nextcloud : identifiants refusés.")
        if reponse.status_code == 404:
            raise NextcloudErreur("Nextcloud : dossier introuvable.")
        if reponse.status_code != 207:
            raise NextcloudErreur(f"Nextcloud : réponse inattendue (HTTP {reponse.status_code}).")
        return reponse.content

    def tester(self):
        self._propfind(self._url_dossier(""), 0)

    def pdf_le_plus_recent(self, dossier):
        fichiers = lire_propfind(self._propfind(self._url_dossier(dossier), 1))
        pdfs = [f for f in fichiers if f.nom.lower().endswith(".pdf")]
        if not pdfs:
            raise NextcloudErreur(f"Aucun PDF dans {dossier} sur Nextcloud.")
        return max(pdfs, key=lambda f: f.modifie_le)

    def telecharger(self, fichier):
        try:
            reponse = requests.get(f"{self.url}{fichier.chemin}", auth=self.auth, timeout=TIMEOUT)
        except requests.exceptions.RequestException:
            raise NextcloudErreur("Téléchargement Nextcloud impossible.") from None
        if reponse.status_code != 200:
            raise NextcloudErreur(f"Téléchargement Nextcloud : HTTP {reponse.status_code}.")
        return reponse.content


def lire_propfind(contenu):
    ns = {"d": "DAV:"}
    fichiers = []
    try:
        racine = ET.fromstring(contenu)
    except ET.ParseError:
        raise NextcloudErreur("Nextcloud : réponse WebDAV illisible.") from None
    for element in racine.findall("d:response", ns):
        href = element.findtext("d:href", default="", namespaces=ns)
        prop = element.find("d:propstat/d:prop", ns)
        if prop is None or prop.find("d:resourcetype/d:collection", ns) is not None:
            continue
        modifie = prop.findtext("d:getlastmodified", default="", namespaces=ns)
        if not modifie:
            continue
        chemin = urlsplit(href).path
        fichiers.append(FichierDistant(
            nom=unquote(chemin.rstrip("/").rsplit("/", 1)[-1]),
            chemin=chemin,
            modifie_le=parsedate_to_datetime(modifie),
        ))
    return fichiers
