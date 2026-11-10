import base64
import logging
from collections import Counter

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.res_config_settings import MODELE_PAR_DEFAUT
from ..tools import od_salaires as ods
from ..tools.anthropic_http import ClaudeErreur, generer_json
from ..tools.nextcloud import Nextcloud, NextcloudErreur
from ..tools.odoo_json2 import OdooClientErreur

_logger = logging.getLogger(__name__)

SENS = [("debit", "Débit"), ("credit", "Crédit")]
CATEGORIES = [
    ("brut", "Brut"), ("onss", "ONSS"), ("precompte", "Précompte"),
    ("net", "Net"), ("autre", "Autre"),
]
ORIGINES = [
    ("mapping", "Mapping"), ("historique", "Historique"), ("pcmn", "PCMN"), ("manuel", "Manuel"),
]
ERREURS_SERVICES = (UserError, ClaudeErreur, NextcloudErreur, OdooClientErreur, ods.OdSalairesErreur)
# Seul le code de l'assistant crée les copies des comptes et journaux du client.
CONTEXTE_IMPORT = "hub_od_import"


class HubOdSalairesWizard(models.TransientModel):
    _name = "hub.od.salaires.wizard"
    _description = "Préparation d'une OD de salaires"

    dossier_id = fields.Many2one("hub.dossier", required=True, ondelete="cascade", readonly=True)
    etape = fields.Selection(
        [("source", "Source"), ("revue", "Revue"), ("termine", "Terminé")],
        default="source", required=True,
    )
    source = fields.Selection(
        [("nextcloud", "Dernier PDF sur Nextcloud"), ("fichier", "Fichier déposé")],
        default="nextcloud", required=True,
    )
    dossier_nextcloud = fields.Char("Dossier Nextcloud", compute="_compute_dossier_nextcloud")
    pdf = fields.Binary("PDF", attachment=False)
    pdf_nom = fields.Char("Nom du PDF")
    modele = fields.Char("Modèle Claude", readonly=True)

    societe_client_id = fields.Integer("ID de la société du client", readonly=True)
    societe_client_nom = fields.Char("Société du client", readonly=True)
    historique_nb = fields.Integer("OD d'historique lues", readonly=True)
    historique_periode = fields.Char("Historique", readonly=True)
    avertissements_html = fields.Html("Avertissements", readonly=True, sanitize=False)

    periode = fields.Char("Période", readonly=True)
    secretariat = fields.Char("Secrétariat social", readonly=True)
    reference = fields.Char("Référence", compute="_compute_reference")
    total_brut = fields.Float("Brut", digits=(16, 2), readonly=True)
    total_onss = fields.Float("ONSS", digits=(16, 2), readonly=True)
    total_precompte = fields.Float("Précompte", digits=(16, 2), readonly=True)
    total_net = fields.Float("Net", digits=(16, 2), readonly=True)

    ligne_ids = fields.One2many("hub.od.salaires.ligne", "wizard_id", "Lignes")
    compte_ids = fields.One2many("hub.od.salaires.compte", "wizard_id", "Plan comptable du client")
    journal_ids = fields.One2many("hub.od.salaires.journal", "wizard_id", "Journaux du client")
    journal_id = fields.Many2one(
        "hub.od.salaires.journal", "Journal", domain="[('wizard_id', '=', id)]"
    )
    reference_existante = fields.Boolean(readonly=True)

    controles_ok = fields.Boolean(compute="_compute_controles")
    controles_html = fields.Html("Contrôles", compute="_compute_controles", sanitize=False)

    move_id_client = fields.Integer(readonly=True)
    move_lien = fields.Char("OD créée", readonly=True)

    @api.depends("dossier_id.nextcloud_path")
    def _compute_dossier_nextcloud(self):
        for assistant in self:
            racine = (assistant.dossier_id.nextcloud_path or "").strip("/")
            assistant.dossier_nextcloud = f"{racine}/salaires" if racine else False

    @api.depends("periode")
    def _compute_reference(self):
        for assistant in self:
            try:
                assistant.reference = ods.reference_od(assistant.periode) if assistant.periode else False
            except ods.OdSalairesErreur:
                assistant.reference = False

    # ------------------------------------------------------------------
    # Contrôles
    # ------------------------------------------------------------------

    def _lignes_pour_controle(self):
        return [{
            "libelle": l.libelle,
            "montant": ods.dec(l.montant),
            "sens": l.sens,
            "categorie": l.categorie,
            "code": l.compte_id.code or None,
            "account_id": l.compte_id.client_id,
        } for l in self.ligne_ids]

    def _totaux(self):
        return {"brut": self.total_brut, "onss": self.total_onss,
                "precompte": self.total_precompte, "net": self.total_net}

    def _anomalies(self):
        anomalies = ods.controler(self._lignes_pour_controle(), self._totaux())
        sans_code = self.ligne_ids.filtered(lambda l: l.compte_sans_code and not l.compte_id)
        if sans_code:
            anomalies.append({
                "message": _("Rubriques dont le compte de référence n'a pas de code dans la "
                             "société du client (corrigez le plan du client ou choisissez un "
                             "autre compte) :"),
                "details": [f"{l.libelle} → {l.compte_sans_code}" for l in sans_code],
            })
        if self.reference_existante:
            anomalies.append({"message": _("Une OD « %s » existe déjà dans l'Odoo du client "
                                           "(brouillon ou comptabilisée).", self.reference),
                              "details": []})
        if not self.journal_id:
            anomalies.append({"message": _("Aucun journal choisi."), "details": []})
        return anomalies

    @api.depends(
        "ligne_ids.montant", "ligne_ids.sens", "ligne_ids.categorie", "ligne_ids.compte_id",
        "ligne_ids.compte_sans_code", "total_brut", "total_onss", "total_precompte", "total_net",
        "reference_existante", "journal_id",
    )
    def _compute_controles(self):
        for assistant in self:
            if assistant.etape == "source" or not assistant.ligne_ids:
                assistant.controles_ok = False
                assistant.controles_html = False
                continue
            anomalies = assistant._anomalies()
            assistant.controles_ok = not anomalies
            assistant.controles_html = _html_controles(anomalies)

    # ------------------------------------------------------------------
    # Exécution d'une étape (point de sauvegarde + trace)
    # ------------------------------------------------------------------

    def _executer(self, fonction, etape):
        """Exécute une étape dans un point de sauvegarde.

        En cas d'échec : retour au point de sauvegarde, trace de l'échec dans le chatter du
        dossier (dans la transaction normale, qui est validée puisqu'aucune exception ne
        remonte), puis notification de l'erreur et réouverture de l'assistant.
        """
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                fonction()
        except ERREURS_SERVICES as erreur:
            self.env.invalidate_all()
            message = erreur.args[0] if erreur.args else str(erreur)
            self.dossier_id._tracer(
                Markup("<p><b>OD de salaires — échec ({})</b></p><p>{}</p>").format(etape, message)
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("OD de salaires — échec (%s)", etape),
                    "message": message,
                    "type": "danger",
                    "sticky": True,
                    "next": self._rouvrir(),
                },
            }
        return self._rouvrir()

    # ------------------------------------------------------------------
    # Étape 1 : extraction et propositions
    # ------------------------------------------------------------------

    def action_extraire(self):
        self.ensure_one()
        return self._executer(self._extraire, _("extraction"))

    def _charger_pdf(self):
        if self.source == "fichier":
            if not self.pdf:
                raise UserError(_("Déposez un fichier PDF."))
            # Odoo 20 : un champ Binary se lit comme BinaryValue (octets bruts via .content).
            return self.pdf.content, self.pdf_nom or "salaires.pdf"
        if not self.dossier_nextcloud:
            raise UserError(_("Chemin Nextcloud du dossier non renseigné."))
        secrets = self.env["hub.secrets"]
        url, utilisateur = secrets._parametre("nextcloud_url"), secrets._parametre("nextcloud_user")
        if not url or not utilisateur:
            raise UserError(_("URL ou utilisateur Nextcloud absent des paramètres Hub compta."))
        nextcloud = Nextcloud(url, utilisateur, secrets._secret_global("nextcloud_password"))
        fichier = nextcloud.pdf_le_plus_recent(self.dossier_nextcloud)
        return nextcloud.telecharger(fichier), fichier.nom

    def _extraire(self):
        dossier = self.dossier_id
        secrets = self.env["hub.secrets"]
        modele = secrets._parametre("claude_modele") or MODELE_PAR_DEFAUT
        cle_claude = secrets._secret_global("anthropic_api_key")
        odoo = dossier._client_odoo()  # résout la société (dossier ou utilisateur API)

        contenu_pdf, nom_pdf = self._charger_pdf()
        pdf_b64 = base64.b64encode(contenu_pdf).decode("ascii")
        donnees = ods.verifier_extraction(generer_json(
            cle_claude, modele, ods.contenu_extraction(pdf_b64), ods.SCHEMA_EXTRACTION, "extraction"
        ))

        comptes = odoo.comptes()
        comptes_par_id = {c["id"]: c for c in comptes}
        comptes_codes = [c for c in comptes if c.get("code")]
        comptes_sans_code = [c for c in comptes if not c.get("code")]
        journaux = odoo.journaux_generaux()
        if not journaux:
            raise UserError(_("Aucun journal de type « général » dans l'Odoo du client."))
        ecritures, lignes_historique = odoo.historique_salaires()
        historique = ods.construire_historique(
            ecritures, lignes_historique, comptes_par_id, anonymiser=dossier.anonymiser_historique
        )
        reference = ods.reference_od(donnees["periode"])
        existante = bool(odoo.od_existante(reference))

        self.ligne_ids.unlink()
        self.compte_ids.unlink()
        self.journal_ids.unlink()
        Compte = self.env["hub.od.salaires.compte"].with_context(**{CONTEXTE_IMPORT: True})
        Journal = self.env["hub.od.salaires.journal"].with_context(**{CONTEXTE_IMPORT: True})
        comptes_hub = Compte.create([{
            "wizard_id": self.id, "client_id": c["id"], "code": c["code"],
            "name": c["name"], "account_type": c.get("account_type") or False,
        } for c in comptes_codes])
        journaux_hub = Journal.create([{
            "wizard_id": self.id, "client_id": j["id"], "code": j["code"], "name": j["name"],
        } for j in journaux])
        par_code = {c.code: c for c in comptes_hub}
        journal = (journaux_hub.filtered(lambda j: j.code == dossier.od_salaires_journal)[:1]
                   or (journaux_hub if len(journaux_hub) == 1 else Journal))

        lignes, a_proposer = self._proposer_depuis_mapping(donnees["rubriques"], par_code)
        if a_proposer:
            self._proposer_par_claude(
                cle_claude, modele, a_proposer, comptes_codes, historique, par_code, comptes_par_id
            )
        self.env["hub.od.salaires.ligne"].create(lignes)

        totaux = donnees["totaux"]
        dates = sorted(e["date"] for e in ecritures)
        self.write({
            "etape": "revue",
            "pdf": pdf_b64,
            "pdf_nom": nom_pdf,
            "modele": modele,
            "societe_client_id": odoo.company_id,
            "societe_client_nom": odoo.societe_nom,
            "historique_nb": len(ecritures),
            "historique_periode": f"{dates[0]} → {dates[-1]}" if dates else False,
            "avertissements_html": _html_comptes_sans_code(comptes_sans_code),
            "periode": donnees["periode"],
            "secretariat": donnees["secretariat"],
            "total_brut": totaux["brut"],
            "total_onss": totaux["onss"],
            "total_precompte": totaux["precompte"],
            "total_net": totaux["net"],
            "reference_existante": existante,
            "journal_id": journal.id,
        })
        origines = Counter(l.get("origine") or "aucune" for l in lignes)
        dossier._tracer(Markup(
            "<p><b>OD de salaires — extraction</b></p><ul>"
            "<li>PDF : {}</li><li>Modèle : {}</li><li>Société du client : {}</li>"
            "<li>Période : {} ({})</li><li>Historique : {} OD comptabilisée(s)</li>"
            "<li>Rubriques : {} — mapping {}, historique {}, PCMN {}, sans proposition {}</li>"
            "<li>Comptes sans code ignorés : {}</li><li>Contrôles : {}</li></ul>"
        ).format(nom_pdf, modele, odoo.societe_nom or odoo.company_id, donnees["periode"],
                 donnees["secretariat"], len(ecritures), len(lignes), origines["mapping"],
                 origines["historique"], origines["pcmn"], origines["aucune"],
                 len(comptes_sans_code),
                 _("OK") if self.controles_ok else _("anomalies à corriger")))

    def _proposer_depuis_mapping(self, rubriques, par_code):
        index = self.env["hub.mapping"]._index(self.dossier_id, ods.TACHE)
        lignes, a_proposer = [], []
        for sequence, rubrique in enumerate(rubriques, start=1):
            ligne = {
                "wizard_id": self.id, "sequence": sequence,
                "libelle": rubrique["libelle"], "montant": rubrique["montant"],
                "sens": rubrique["sens"], "categorie": rubrique["categorie"], "origine": False,
            }
            mapping = index.get(ods.normaliser(rubrique["libelle"]))
            compte = par_code.get(mapping.compte_code) if mapping else None
            if compte:
                ligne.update(compte_id=compte.id, origine="mapping",
                             justification=_("Mapping validé du dossier."))
            else:
                if mapping:
                    ligne["justification"] = _("Compte mémorisé %s absent du plan du client.",
                                               mapping.compte_code)
                a_proposer.append(ligne)
            lignes.append(ligne)
        return lignes, a_proposer

    def _proposer_par_claude(self, cle, modele, lignes, comptes, historique, par_code, comptes_par_id):
        """Un seul appel : historique s'il existe (écriture citée vérifiée), PCMN sinon."""
        resultat = generer_json(
            cle, modele, ods.contenu_mapping(lignes, comptes, historique), ods.SCHEMA_PROPOSITIONS,
            "proposition de mapping",
        )
        ecritures_connues = {e["name"] for e in historique}
        propositions = {ods.normaliser(p["libelle"]): p for p in resultat.get("propositions", [])}
        for ligne in lignes:
            proposition = propositions.get(ods.normaliser(ligne["libelle"]))
            if not proposition:
                ligne["justification"] = _("Aucune proposition : choisissez un compte.")
                continue
            code = proposition["code_compte"]
            cite = proposition.get("ecriture_reference") in ecritures_connues
            if code.startswith(ods.MARQUEUR_SANS_CODE):
                compte_client = comptes_par_id.get(_id_marqueur(code)) or {}
                ligne.update(
                    origine="historique" if cite else False,
                    compte_sans_code=compte_client.get("name") or code,
                    justification=proposition["justification"],
                )
                continue
            compte = par_code.get(code)
            if not compte:
                ligne["justification"] = _("Code proposé %s absent du plan : choisissez un compte.",
                                           code)
                continue
            ligne.update(
                compte_id=compte.id,
                origine="historique" if historique and cite else "pcmn",
                justification=proposition["justification"],
            )

    # ------------------------------------------------------------------
    # Étape 2 : création de l'OD en brouillon
    # ------------------------------------------------------------------

    def action_creer_od(self):
        self.ensure_one()
        return self._executer(self._creer_od, _("création de l'OD"))

    def _creer_od(self):
        if self.etape != "revue":
            raise UserError(_("Lancez d'abord l'extraction."))
        dossier = self.dossier_id
        odoo = dossier._client_odoo(company_id=self.societe_client_id or None)
        # Revérifier le doublon au dernier moment.
        self.reference_existante = bool(odoo.od_existante(self.reference))
        anomalies = self._anomalies()
        if anomalies:
            raise UserError(_("Contrôles en échec, OD non créée :\n%s", "\n".join(
                a["message"] for a in anomalies)))

        lignes = self._lignes_pour_controle()
        move_id = odoo.creer_od_brouillon(
            ods.valeurs_move(self.periode, self.journal_id.client_id, lignes)
        )
        odoo.joindre_pdf(move_id, self.pdf_nom or f"{self.reference}.pdf", self.pdf.content)
        lien = odoo.lien("account.move", move_id)

        # Le mapping est alimenté automatiquement par chaque OD créée.
        Mapping = self.env["hub.mapping"]
        for ligne in self.ligne_ids:
            Mapping._memoriser(dossier, ods.TACHE, ligne.libelle,
                               ligne.compte_id.code, ligne.compte_id.name)
        if dossier.od_salaires_journal != self.journal_id.code:
            dossier.od_salaires_journal = self.journal_id.code

        debit = sum((l["montant"] for l in lignes if l["sens"] == "debit"), ods.ZERO)
        self.write({"etape": "termine", "move_id_client": move_id, "move_lien": lien,
                    "pdf": False})
        dossier._tracer(Markup(
            "<p><b>OD de salaires — créée en brouillon</b></p><ul>"
            "<li>Référence : {} (id {} dans l'Odoo du client) — <a href=\"{}\" target=\"_blank\">"
            "ouvrir</a></li><li>Journal : {}</li><li>Total : {} €, {} lignes</li>"
            "<li>Mapping mémorisé : {} rubrique(s)</li></ul>"
            "<p>Non comptabilisée : à valider dans l'Odoo du client.</p>"
        ).format(self.reference, move_id, lien, self.journal_id.code, ods.euros(debit),
                 len(lignes), len(self.ligne_ids)))

    def _rouvrir(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "name": _("OD de salaires"),
        }


def _id_marqueur(code):
    try:
        return int(code[len(ods.MARQUEUR_SANS_CODE):])
    except ValueError:
        return None


def _html_controles(anomalies):
    if not anomalies:
        return Markup('<p class="text-success mb-0">✔ Contrôles OK : OD équilibrée, totaux '
                      "conformes au récapitulatif, toutes les rubriques ont un compte, "
                      "pas de doublon.</p>")
    elements = Markup()
    for anomalie in anomalies:
        details = Markup("").join(Markup("<li>{}</li>").format(d) for d in anomalie["details"])
        elements += Markup("<li>{}{}</li>").format(
            anomalie["message"], Markup("<ul>{}</ul>").format(details) if details else ""
        )
    return Markup('<div class="text-danger"><p class="mb-1">✘ Contrôles en échec :</p>'
                  "<ul>{}</ul></div>").format(elements)


def _html_comptes_sans_code(comptes):
    if not comptes:
        return False
    elements = Markup("").join(
        Markup("<li>{} (id {})</li>").format(c.get("name") or "?", c["id"]) for c in comptes
    )
    return Markup(
        '<div class="text-warning"><p class="mb-1">⚠ {} compte(s) sans code dans la société du '
        "client, ignorés (non bloquant sauf si une rubrique en a besoin) :</p><ul>{}</ul></div>"
    ).format(len(comptes), elements)


class HubOdSalairesLigne(models.TransientModel):
    _name = "hub.od.salaires.ligne"
    _description = "Ligne d'OD de salaires (préparation)"
    _order = "sequence, id"

    wizard_id = fields.Many2one("hub.od.salaires.wizard", required=True, ondelete="cascade")
    sequence = fields.Integer(default=10)
    libelle = fields.Char("Rubrique", required=True)
    montant = fields.Float(digits=(16, 2), required=True)
    sens = fields.Selection(SENS, required=True)
    categorie = fields.Selection(CATEGORIES, "Catégorie", required=True, default="autre")
    compte_id = fields.Many2one("hub.od.salaires.compte", "Compte")
    compte_sans_code = fields.Char("Compte de référence sans code", readonly=True)
    origine = fields.Selection(ORIGINES)
    justification = fields.Char()

    @api.onchange("compte_id")
    def _onchange_compte_id(self):
        for ligne in self:
            ligne.origine = "manuel"


class _CopieClient(models.AbstractModel):
    """Copie temporaire d'un enregistrement de l'Odoo client : jamais créée par l'utilisateur."""

    _name = "hub.od.salaires.copie.client"
    _description = "Copie d'un enregistrement de l'Odoo client"

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get(CONTEXTE_IMPORT):
            raise UserError(_("Cette liste vient de l'Odoo du client : choisissez un élément "
                              "existant, la création n'est pas possible ici."))
        return super().create(vals_list)


class HubOdSalairesCompte(models.TransientModel):
    """Copie temporaire d'un compte du plan comptable de l'Odoo client."""

    _name = "hub.od.salaires.compte"
    _inherit = ["hub.od.salaires.copie.client"]
    _description = "Compte de l'Odoo client (préparation)"
    _order = "code"

    wizard_id = fields.Many2one("hub.od.salaires.wizard", required=True, ondelete="cascade")
    client_id = fields.Integer("ID dans l'Odoo client", required=True)
    code = fields.Char(required=True)
    name = fields.Char(required=True)
    account_type = fields.Char()

    @api.depends("code", "name")
    def _compute_display_name(self):
        for compte in self:
            compte.display_name = f"{compte.code} {compte.name}"

    @api.model
    def _search_display_name(self, operator, value):
        if operator in ("ilike", "=ilike", "like", "=like") and value:
            return ["|", ("code", operator, value), ("name", operator, value)]
        return super()._search_display_name(operator, value)


class HubOdSalairesJournal(models.TransientModel):
    """Copie temporaire d'un journal général de l'Odoo client."""

    _name = "hub.od.salaires.journal"
    _inherit = ["hub.od.salaires.copie.client"]
    _description = "Journal de l'Odoo client (préparation)"
    _order = "code"

    wizard_id = fields.Many2one("hub.od.salaires.wizard", required=True, ondelete="cascade")
    client_id = fields.Integer("ID dans l'Odoo client", required=True)
    code = fields.Char(required=True)
    name = fields.Char(required=True)

    @api.depends("code", "name")
    def _compute_display_name(self):
        for journal in self:
            journal.display_name = f"{journal.code} — {journal.name}"
