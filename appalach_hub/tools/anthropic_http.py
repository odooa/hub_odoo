"""Appels à l'API Claude (Messages) par requests, sans SDK.

Version d'API : valeur exacte envoyée par le SDK Python `anthropic` 2.7.0
(`anthropic/_constants.py`, ANTHROPIC_API_VERSION). Sortie structurée par
`output_config.format` (json_schema). Aucun repli automatique en cas de refus.
La clé n'apparaît jamais dans un message d'erreur.
"""

import json

import requests

URL_API = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2026-10-21"
TIMEOUT_MESSAGES = 180
TIMEOUT_COURT = 15


class ClaudeErreur(Exception):
    """Erreur présentable à l'utilisateur."""


def _entetes(cle):
    return {
        "x-api-key": cle,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def _message_erreur(reponse):
    try:
        return reponse.json().get("error", {}).get("message", "")
    except ValueError:
        return ""


def _verifier_statut(reponse, modele=None):
    statut = reponse.status_code
    if statut == 200:
        return
    if statut == 401:
        raise ClaudeErreur("Claude : clé API refusée.")
    if statut == 403:
        raise ClaudeErreur("Claude : accès refusé pour cette clé API.")
    if statut == 404 and modele:
        raise ClaudeErreur(f"Claude : modèle inconnu « {modele} ».")
    if statut == 429:
        raise ClaudeErreur("Claude : limite de débit atteinte, réessayez plus tard.")
    if statut == 400:
        raise ClaudeErreur(f"Claude : requête refusée ({_message_erreur(reponse)}).")
    if statut == 529 or statut >= 500:
        raise ClaudeErreur(f"Claude : service indisponible (HTTP {statut}), réessayez plus tard.")
    raise ClaudeErreur(f"Claude : erreur HTTP {statut}.")


def tester_cle(cle):
    """Vérifie la clé sans consommer de jetons (liste des modèles)."""
    try:
        reponse = requests.get(f"{URL_API}/v1/models", headers=_entetes(cle), timeout=TIMEOUT_COURT)
    except requests.exceptions.RequestException:
        raise ClaudeErreur("Claude : API injoignable.") from None
    _verifier_statut(reponse)


def generer_json(cle, modele, contenu, schema, etape, max_tokens=16000):
    """Envoie un message utilisateur et renvoie l'objet JSON conforme à ``schema``.

    ``contenu`` : liste de blocs (document, text). ``etape`` : libellé pour les messages.
    """
    corps = {
        "model": modele,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": contenu}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    try:
        reponse = requests.post(
            f"{URL_API}/v1/messages",
            headers=_entetes(cle),
            data=json.dumps(corps),
            timeout=TIMEOUT_MESSAGES,
        )
    except requests.exceptions.Timeout:
        raise ClaudeErreur(f"Claude : délai dépassé pendant l'{etape}.") from None
    except requests.exceptions.RequestException:
        raise ClaudeErreur("Claude : API injoignable.") from None
    _verifier_statut(reponse, modele)

    message = reponse.json()
    raison = message.get("stop_reason")
    if raison == "refusal":
        details = message.get("stop_details") or {}
        categorie = details.get("category") or "non précisée"
        explication = details.get("explanation") or ""
        raise ClaudeErreur(
            f"Claude a refusé l'{etape} (catégorie : {categorie}). {explication} "
            "Aucun repli automatique : traitement arrêté."
        )
    if raison == "max_tokens":
        raise ClaudeErreur(f"Claude : réponse tronquée pendant l'{etape} (max_tokens atteint).")
    texte = next(
        (bloc.get("text") for bloc in message.get("content", []) if bloc.get("type") == "text"),
        None,
    )
    if not texte:
        raise ClaudeErreur(f"Claude : réponse sans contenu pendant l'{etape}.")
    try:
        return json.loads(texte)
    except ValueError:
        raise ClaudeErreur(f"Claude : JSON invalide pendant l'{etape}.") from None
