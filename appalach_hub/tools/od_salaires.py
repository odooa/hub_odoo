"""Logique pure de l'OD de salaires (reprise du prototype hub_compta/prototypes/od_salaires.py).

Sans ORM ni réseau : schémas JSON envoyés à Claude, consignes, normalisation des libellés,
référence et date de l'OD, contrôles.
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

# Schémas identiques à ceux que le SDK anthropic génère à partir des modèles Pydantic
# du prototype (vérifié par capture de la requête).
SCHEMA_EXTRACTION = {
    "type": "object",
    "properties": {
        "periode": {"type": "string", "description": "Mois de paie au format AAAA-MM"},
        "secretariat": {"type": "string", "description": "Nom du secrétariat social"},
        "rubriques": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "libelle": {
                        "type": "string",
                        "description": "Libellé de la rubrique tel qu'imprimé sur le récapitulatif",
                    },
                    "montant": {
                        "type": "number",
                        "description": "Montant positif en euros, deux décimales",
                    },
                    "sens": {"enum": ["debit", "credit"]},
                    "categorie": {
                        "enum": ["brut", "onss", "precompte", "net", "autre"],
                        "description": (
                            "brut : rémunération brute ; onss : cotisations ONSS (travailleur ou "
                            "patronales) ; precompte : précompte professionnel ; net : net à "
                            "payer ; autre : le reste"
                        ),
                    },
                },
                "additionalProperties": False,
                "required": ["libelle", "montant", "sens", "categorie"],
            },
        },
        "totaux": {
            "type": "object",
            "description": "Totaux de contrôle imprimés sur le récapitulatif",
            "properties": {k: {"type": "number"} for k in CATEGORIES_TOTAUX},
            "additionalProperties": False,
            "required": list(CATEGORIES_TOTAUX),
        },
    },
    "additionalProperties": False,
    "required": ["periode", "secretariat", "rubriques", "totaux"],
}

SCHEMA_PROPOSITIONS = {
    "type": "object",
    "properties": {
        "propositions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "libelle": {"type": "string"},
                    "code_compte": {"type": "string"},
                    "ecriture_reference": {
                        "type": "string",
                        "description": "Nom exact de l'écriture d'historique citée, vide si aucune",
                    },
                    "justification": {"type": "string"},
                },
                "additionalProperties": False,
                "required": ["libelle", "code_compte", "ecriture_reference", "justification"],
            },
        },
    },
    "additionalProperties": False,
    "required": ["propositions"],
}

CONSIGNE_EXTRACTION = """\
Tu lis le récapitulatif comptable mensuel d'un secrétariat social belge.
Transcris fidèlement, sans rien calculer ni inventer :
- la période de paie (AAAA-MM) et le nom du secrétariat social ;
- chaque rubrique de l'imputation comptable proposée (libellé exact, montant positif,
  sens débit ou crédit, catégorie) ;
- les totaux de contrôle imprimés : brut, ONSS, précompte professionnel, net à payer.
Si un total n'est pas imprimé, mets 0. Si un montant est illisible, ne le devine pas :
mets 0 et signale-le dans le libellé entre crochets.
"""

MARQUEUR_SANS_CODE = "SANS-CODE-"

CONSIGNE_MAPPING = """\
Tu proposes l'imputation des rubriques d'une OD de salaires belge (PCMN) sur le plan
comptable fourni. Pour chaque rubrique, choisis exactement un code présent dans la liste,
avec une justification d'une phrase. N'invente aucun code. Laisse ecriture_reference vide.
"""

CONSIGNE_HISTORIQUE = """\
Tu proposes l'imputation des rubriques d'une OD de salaires belge sur le plan comptable du
client, en t'appuyant d'abord sur ses OD de salaires déjà comptabilisées (historique
ci-dessous).
Pour chaque rubrique :
- si une écriture de l'historique contient la rubrique équivalente, reprends le compte
  utilisé, mets dans ecriture_reference le nom exact de cette écriture et cite-la avec sa
  date dans la justification (ex. « comme OD/2026/0912 du 30/09/2026 ») ;
- si le compte de l'historique est noté SANS-CODE-<id>, renvoie exactement ce marqueur
  comme code_compte ;
- sinon, propose un compte selon le PCMN belge parmi le plan fourni et laisse
  ecriture_reference vide.
N'invente aucun code ni aucun nom d'écriture.
"""


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


def contenu_extraction(pdf_b64):
    return [
        {"type": "document",
         "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
        {"type": "text", "text": CONSIGNE_EXTRACTION},
    ]


def construire_historique(ecritures, lignes, comptes_par_id, anonymiser=False):
    """Assemble l'historique lu par JSON-2.

    ecritures : dicts (id, name, ref, date) ; lignes : dicts (move_id [id, nom], account_id
    [id, nom], name, debit, credit) ; comptes_par_id : {id: dict compte (code éventuellement
    vide)}. anonymiser : remplace le libellé de ligne par le libellé du compte.
    """
    par_ecriture = {e["id"]: {"name": e["name"], "date": e["date"], "ref": e.get("ref") or "",
                              "lignes": []} for e in ecritures}
    for ligne in lignes:
        ecriture = par_ecriture.get(ligne["move_id"][0] if ligne.get("move_id") else None)
        if ecriture is None or not ligne.get("account_id"):
            continue
        compte_id = ligne["account_id"][0]
        compte = comptes_par_id.get(compte_id) or {}
        nom_compte = compte.get("name") or ligne["account_id"][1]
        ecriture["lignes"].append({
            "code": compte.get("code") or f"{MARQUEUR_SANS_CODE}{compte_id}",
            "compte": nom_compte,
            "libelle": nom_compte if anonymiser else (ligne.get("name") or nom_compte),
            "debit": dec(ligne.get("debit")),
            "credit": dec(ligne.get("credit")),
        })
    return [par_ecriture[e["id"]] for e in ecritures]


def texte_historique(historique):
    blocs = []
    for ecriture in historique:
        lignes = "\n".join(
            f"  {l['code']} | {l['compte']} | {l['libelle']} | {euros(l['debit'])} | {euros(l['credit'])}"
            for l in ecriture["lignes"]
        )
        blocs.append(f"Écriture {ecriture['name']} du {ecriture['date']} (réf. {ecriture['ref']}) :\n"
                     f"  code | compte | libellé | débit | crédit\n{lignes}")
    return "\n\n".join(blocs)


def contenu_mapping(rubriques, comptes, historique=None):
    """rubriques : dicts (libelle, sens, categorie) ; comptes : dicts (code, name, account_type) ;
    historique : sortie de construire_historique (vide : proposition PCMN générique)."""
    plan = "\n".join(f"{c['code']}\t{c['name']}\t{c.get('account_type') or ''}" for c in comptes)
    liste = "\n".join(f"- {r['libelle']} ({r['sens']}, {r['categorie']})" for r in rubriques)
    if historique:
        texte = (f"{CONSIGNE_HISTORIQUE}\nRubriques :\n{liste}\n\n"
                 f"Historique des OD de salaires :\n{texte_historique(historique)}\n\n"
                 f"Plan comptable (code, nom, type) :\n{plan}")
    else:
        texte = f"{CONSIGNE_MAPPING}\nRubriques :\n{liste}\n\nPlan comptable (code, nom, type) :\n{plan}"
    return [{"type": "text", "text": texte}]


def verifier_extraction(donnees):
    """Contrôle minimal de la sortie de Claude (le schéma est déjà imposé côté API)."""
    verifier_periode(donnees.get("periode"))
    if not donnees.get("rubriques"):
        raise OdSalairesErreur("Claude n'a extrait aucune rubrique du PDF.")
    return donnees


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


def valeurs_move(periode, journal_id, lignes):
    """lignes : dicts {account_id, libelle, montant, sens}."""
    return {
        "move_type": "entry",
        "journal_id": journal_id,
        "date": date_fin_periode(periode).isoformat(),
        "ref": reference_od(periode),
        "line_ids": [
            [0, 0, {
                "account_id": l["account_id"],
                "name": l["libelle"],
                "debit": float(l["montant"]) if l["sens"] == "debit" else 0.0,
                "credit": float(l["montant"]) if l["sens"] == "credit" else 0.0,
            }]
            for l in lignes
        ],
    }
