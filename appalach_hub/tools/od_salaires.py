"""Logique pure de l'OD de salaires (reprise du prototype hub_compta/prototypes/od_salaires.py).

Sans ORM ni réseau : normalisation des libellés, période, contrôles, valeurs de l'écriture.
"""

import calendar
import re
import unicodedata
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

TACHE = "od_salaires"
CENTIME = Decimal("0.01")
TOLERANCE = Decimal("0.01")
ZERO = Decimal("0.00")
CATEGORIES_TOTAUX = ("brut", "onss", "precompte", "net")

MARQUEUR_SANS_CODE = "SANS-CODE-"


class OdSalairesErreur(Exception):
    """Erreur présentable à l'utilisateur."""


def dec(valeur):
    return Decimal(str(valeur or 0)).quantize(CENTIME, rounding=ROUND_HALF_UP)


def euros(valeur):
    return f"{valeur:,.2f}".replace(",", " ").replace(".", ",")


def normaliser(libelle):
    texte = unicodedata.normalize("NFKC", libelle or "").casefold()
    return re.sub(r"\s+", " ", texte).strip()


def verifier_periode(periode):
    correspondance = re.fullmatch(r"(\d{4})-(\d{2})", periode or "")
    if not correspondance or not 1 <= int(correspondance.group(2)) <= 12:
        raise OdSalairesErreur(f"Période invalide : « {periode} » (AAAA-MM attendu).")
    return int(correspondance.group(1)), int(correspondance.group(2))


def reference_od(periode):
    verifier_periode(periode)
    return f"Salaires {periode}"


def date_fin_periode(periode):
    annee, mois = verifier_periode(periode)
    return date(annee, mois, calendar.monthrange(annee, mois)[1])


def controler(lignes, totaux, tolerance=TOLERANCE):
    """Contrôles bloquants de l'OD.

    lignes : dicts {libelle, montant (Decimal), sens, categorie, code (ou None)} ;
    totaux : dict {brut, onss, precompte, net} (Decimal).
    Renvoie une liste d'anomalies {"message": str, "details": [str]} (vide si tout est correct).
    """
    anomalies = []
    sans_compte = [l["libelle"] for l in lignes if not l.get("code")]
    if sans_compte:
        anomalies.append({"message": "Rubriques sans compte :", "details": sans_compte})

    debit = sum((l["montant"] for l in lignes if l["sens"] == "debit"), ZERO)
    credit = sum((l["montant"] for l in lignes if l["sens"] == "credit"), ZERO)
    if abs(debit - credit) > tolerance:
        anomalies.append({
            "message": f"OD déséquilibrée : débit {euros(debit)} ≠ crédit {euros(credit)} "
                       f"(écart {euros(debit - credit)}).",
            "details": [],
        })

    for categorie in CATEGORIES_TOTAUX:
        attendu = dec(totaux.get(categorie))
        concernees = [l for l in lignes if l["categorie"] == categorie]
        obtenu = sum((l["montant"] for l in concernees), ZERO)
        if abs(attendu - obtenu) > tolerance:
            anomalies.append({
                "message": f"Total {categorie} incohérent avec le récapitulatif : attendu "
                           f"{euros(attendu)}, obtenu {euros(obtenu)} "
                           f"(écart {euros(obtenu - attendu)}).",
                "details": [f"{l['libelle']} : {euros(l['montant'])} ({l['sens']})"
                            for l in concernees] or ["aucune rubrique de cette catégorie"],
            })
    return anomalies


def valeurs_move(journal_id, date_od, reference, lignes):
    """lignes : dicts {account_id, libelle, debit, credit, partner_id (0 ou False : aucun)}."""
    return {
        "move_type": "entry",
        "journal_id": journal_id,
        "date": date_od,
        "ref": reference,
        "line_ids": [
            [0, 0, {
                "account_id": l["account_id"],
                "name": l["libelle"],
                "debit": float(l["debit"]),
                "credit": float(l["credit"]),
                **({"partner_id": l["partner_id"]} if l.get("partner_id") else {}),
            }]
            for l in lignes
        ],
    }
