"""Boucle de l'agent (tool use de l'API Messages, par requests), sans ORM.

L'état de la conversation (``etat``) est un dict sérialisable en JSON, conservé par le hub
entre deux reprises (question au comptable). Le PDF n'y figure pas : il est remplacé par un
marqueur, substitué à l'envoi. Les blocs de réflexion sont renvoyés tels quels.
"""

import copy
import time

from .agent_outils import CONSIGNE_SYSTEME, DEFINITIONS, MARQUEUR_PDF, RELANCE

MAX_TOKENS_APPEL = 16000
CACHE = {"type": "ephemeral"}


class AgentErreur(Exception):
    """Arrêt du run, présentable à l'utilisateur."""


def tokens_utilises(usage):
    """(entrée, sortie) ; l'entrée compte aussi les lectures et écritures du cache."""
    usage = usage or {}
    entree = sum(usage.get(k) or 0 for k in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return entree, usage.get("output_tokens") or 0


def raisonnement(contenu):
    """Réflexion résumée et texte de l'assistant, pour affichage dans le hub."""
    morceaux = []
    for bloc in contenu:
        if bloc.get("type") == "thinking" and bloc.get("thinking"):
            morceaux.append(bloc["thinking"].strip())
        elif bloc.get("type") == "text" and bloc.get("text"):
            morceaux.append(bloc["text"].strip())
    return "\n\n".join(morceaux)


def etat_initial(mission):
    return {"messages": [{"role": "user", "content": [{"type": "text", "text": mission}]}],
            "nb_etapes": 0, "tokens_entree": 0, "tokens_sortie": 0,
            "relance_faite": False, "en_attente": None, "outils": {}}


class Agent:
    """
    appeler(corps, remplacements) -> message : appel HTTP de l'API Messages ;
    outils : OutilsOdSalaires ; noter(**etape) : enregistre une étape dans le hub.
    """

    def __init__(self, appeler, outils, modele, max_etapes, budget_tokens, pdf_b64, noter):
        self.appeler = appeler
        self.outils = outils
        self.modele = modele
        self.max_etapes = max_etapes
        self.budget_tokens = budget_tokens
        self.pdf_b64 = pdf_b64
        self.noter = noter

    def _corps(self, messages):
        # Point de cache sur le dernier bloc envoyé (copie : la conversation stockée n'en a pas).
        envoyes = list(messages)
        dernier = dict(envoyes[-1])
        contenu = [dict(b) for b in dernier["content"]]
        contenu[-1]["cache_control"] = CACHE
        dernier["content"] = contenu
        envoyes[-1] = dernier
        return {
            "model": self.modele,
            "max_tokens": MAX_TOKENS_APPEL,
            "system": [{"type": "text", "text": CONSIGNE_SYSTEME, "cache_control": CACHE}],
            "tools": DEFINITIONS,
            "tool_choice": {"type": "auto"},
            "thinking": {"type": "adaptive", "display": "summarized"},
            "messages": envoyes,
        }

    def reprendre(self, etat, reponse):
        """Ajoute la réponse du comptable à la question en attente, puis continue."""
        attente = etat.get("en_attente")
        if not attente:
            raise AgentErreur("Aucune question en attente.")
        # Réponse humaine en bloc texte APRÈS les tool_result (et non dans le tool_result).
        contenu = list(attente["resultats"]) + [
            {"type": "tool_result", "tool_use_id": attente["tool_use_id"],
             "content": "Réponse du comptable dans le message qui suit."},
            {"type": "text", "text": f"Réponse du comptable : {reponse}"},
        ]
        etat["messages"].append({"role": "user", "content": contenu})
        etat["en_attente"] = None
        return self.executer(etat)

    def executer(self, etat):
        """Boucle jusqu'à proposer_od (valide) ou poser_question.

        Renvoie {"type": "question", ...} ou {"type": "proposition", "proposition": ...} ;
        lève AgentErreur (limite, budget, doublon, fin sans outil final) ou ClaudeErreur.
        """
        self.outils.etat = etat.setdefault("outils", {})
        self.outils.etat.setdefault("ecritures_lues", [])
        self.outils.etat.setdefault("pdf_fourni", False)
        while True:
            if etat["nb_etapes"] >= self.max_etapes:
                raise AgentErreur(f"Limite de {self.max_etapes} étapes atteinte sans proposition.")
            consommes = etat["tokens_entree"] + etat["tokens_sortie"]
            if consommes >= self.budget_tokens:
                raise AgentErreur(f"Budget de {self.budget_tokens} tokens épuisé "
                                  f"({consommes} consommés) sans proposition.")
            debut = time.monotonic()
            message = self.appeler(self._corps(etat["messages"]), {MARQUEUR_PDF: self.pdf_b64})
            duree = time.monotonic() - debut
            entree, sortie = tokens_utilises(message.get("usage"))
            etat["nb_etapes"] += 1
            etat["tokens_entree"] += entree
            etat["tokens_sortie"] += sortie
            contenu = message.get("content") or []
            demandes = [b for b in contenu if b.get("type") == "tool_use"]
            self.noter(
                type="modele",
                resume=(f"{message.get('stop_reason')} — outils demandés : "
                        + (", ".join(b["name"] for b in demandes) or "aucun")),
                raisonnement=raisonnement(contenu),
                tokens_entree=entree, tokens_sortie=sortie, duree=duree,
            )
            etat["messages"].append({"role": "assistant", "content": contenu})

            if not demandes:
                if etat["relance_faite"]:
                    raise AgentErreur("L'agent s'est arrêté sans proposer d'OD ni poser de question.")
                etat["relance_faite"] = True
                etat["messages"].append({"role": "user", "content": [{"type": "text", "text": RELANCE}]})
                continue

            resultats, final = [], None
            for bloc in demandes:
                nom, entree_outil = bloc["name"], bloc.get("input") or {}
                if nom in ("poser_question", "proposer_od") and final is not None:
                    resultats.append(_resultat(bloc, "Un seul outil final par tour : ignoré.", True))
                    continue
                if nom == "poser_question":
                    self.noter(type="outil", outil=nom, parametres=entree_outil,
                               resume=f"Question : {entree_outil.get('question', '')}")
                    final = {"type": "question", "bloc": bloc}
                    continue
                if nom == "proposer_od":
                    anomalies, proposition, doublon = self.outils.valider_proposition(entree_outil)
                    resume = (doublon or ("Anomalies : " + " | ".join(anomalies) if anomalies
                                          else "Proposition valide, soumise au comptable."))
                    self.noter(type="controle", outil=nom, parametres=entree_outil, resume=resume,
                               erreur=bool(doublon or anomalies))
                    if doublon:
                        raise AgentErreur(doublon)
                    if anomalies:
                        texte = "Proposition refusée par les contrôles, corrige :\n- " + "\n- ".join(anomalies)
                        resultats.append(_resultat(bloc, texte, True))
                        continue
                    final = {"type": "proposition", "proposition": proposition}
                    continue
                debut = time.monotonic()
                contenu_resultat, resume, erreur = self.outils.executer(nom, entree_outil)
                self.noter(type="outil", outil=nom, parametres=entree_outil, resume=resume,
                           erreur=erreur, duree=time.monotonic() - debut)
                resultats.append(_resultat(bloc, contenu_resultat, erreur))

            if final and final["type"] == "question":
                question = final["bloc"].get("input") or {}
                etat["en_attente"] = {"tool_use_id": final["bloc"]["id"], "resultats": resultats}
                return {"type": "question", "question": question.get("question") or "",
                        "options": question.get("options") or [],
                        "contexte": question.get("contexte") or ""}
            if final:
                return final
            etat["messages"].append({"role": "user", "content": resultats})


def _resultat(bloc, contenu, erreur=False):
    resultat = {"type": "tool_result", "tool_use_id": bloc["id"], "content": copy.deepcopy(contenu)}
    if erreur:
        resultat["is_error"] = True
    return resultat
