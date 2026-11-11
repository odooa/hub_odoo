import base64
import json
import logging
from collections import Counter

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..tools import od_salaires as ods
from ..tools.agent import Agent, AgentErreur, etat_initial
from ..tools.agent_outils import SOURCE_DOCUMENTS, SOURCE_PDF, OutilsOdSalaires
from ..tools.anthropic_http import ClaudeErreur, envoyer_message
from ..tools.nextcloud import NextcloudErreur
from ..tools.odoo_json2 import OdooClientErreur
from .hub_mapping import TACHES
from .res_config_settings import BUDGET_PAR_DEFAUT, ETAPES_PAR_DEFAUT, MODELE_PAR_DEFAUT

_logger = logging.getLogger(__name__)

ERREURS_SERVICES = (UserError, AgentErreur, ClaudeErreur, NextcloudErreur, OdooClientErreur,
                    ods.OdSalairesErreur)
CONTEXTE_CRON = "hub_run_cron"
GROUPE_SYSTEME = "base.group_system"
ETATS = [
    ("en_file", "En file"), ("en_cours", "En cours"), ("en_attente", "Question"),
    ("proposition", "Proposition"), ("od_creee", "OD créée"), ("rejete", "Rejeté"),
    ("echec", "Échec"),
]
ETATS_FINAUX = ("od_creee", "rejete", "echec")
SOURCES = [("nextcloud", "Nextcloud"), ("documents", "Documents Odoo"), ("fichier", "Fichier déposé")]
STATUTS_PIECE = [
    ("jointe", "PDF joint à l'OD"), ("liee", "Liée à l'OD"),
    ("liee_traitee", "Liée à l'OD et marquée traitée"), ("avertissement", "À vérifier"),
]
ORIGINES = [
    ("mapping", "Mapping"), ("historique", "Historique"), ("pcmn", "PCMN"), ("manuel", "Manuel"),
]
CATEGORIES = [
    ("brut", "Brut"), ("onss", "ONSS"), ("precompte", "Précompte"), ("net", "Net"), ("autre", "Autre"),
]


class HubRun(models.Model):
    """Exécution de l'agent sur un dossier (ici : préparation d'une OD de salaires)."""

    _name = "hub.run"
    _description = "Exécution de l'agent"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char("Exécution", compute="_compute_name", store=True)
    dossier_id = fields.Many2one("hub.dossier", "Dossier", required=True, ondelete="cascade",
                                 index=True, readonly=True)
    tache = fields.Selection(TACHES, "Tâche", required=True, default=ods.TACHE, readonly=True)
    etat = fields.Selection(ETATS, "État", default="en_file", required=True, readonly=True,
                            tracking=True)
    user_id = fields.Many2one("res.users", "Lancé par", readonly=True,
                              default=lambda self: self.env.user)
    modele = fields.Char("Modèle Claude", readonly=True)
    max_etapes = fields.Integer("Étapes max.", readonly=True)
    budget_tokens = fields.Integer("Budget (tokens)", readonly=True)
    nb_etapes = fields.Integer("Appels au modèle", readonly=True)
    tokens_entree = fields.Integer("Tokens en entrée", readonly=True)
    tokens_sortie = fields.Integer("Tokens en sortie", readonly=True)
    source = fields.Selection(SOURCES, "Source de la pièce", default="fichier", required=True,
                              readonly=True)
    pdf_nom = fields.Char("PDF", readonly=True)
    # Traçabilité de la pièce (Documents : connue quand l'agent la lit / la propose).
    piece_nom = fields.Char("Pièce", readonly=True)
    piece_date = fields.Datetime("Date de la pièce", readonly=True)
    piece_lien = fields.Char("Lien vers la pièce", readonly=True)
    piece_document_id = fields.Integer("ID du document (Documents)", readonly=True)
    piece_imposee_id = fields.Integer("Pièce imposée (ID Documents)", readonly=True)
    piece_statut = fields.Selection(STATUTS_PIECE, "Pièce après création", readonly=True)
    piece_avertissement = fields.Text("Avertissement sur la pièce", readonly=True)
    periode_demandee = fields.Char("Période demandée", readonly=True)
    societe_client_id = fields.Integer("ID de la société du client", readonly=True)
    societe_client_nom = fields.Char("Société du client", readonly=True)
    motif = fields.Text("Motif de l'arrêt", readonly=True)

    # Données techniques : jamais affichées, purgées en fin de run.
    pdf = fields.Binary("PDF (temporaire)", attachment=True, groups=GROUPE_SYSTEME, copy=False)
    conversation = fields.Text("Conversation (technique)", groups=GROUPE_SYSTEME, copy=False)

    # Question au comptable
    question = fields.Text(readonly=True)
    question_options = fields.Text("Options proposées", readonly=True)
    question_contexte = fields.Text("Contexte", readonly=True)
    reponse = fields.Text("Réponse")

    # Proposition
    periode = fields.Char("Période", readonly=True)
    journal_code = fields.Char("Journal", readonly=True)
    journal_client_id = fields.Integer(readonly=True)
    date_od = fields.Date("Date de l'OD", readonly=True)
    reference = fields.Char("Référence", readonly=True)
    total_brut = fields.Float("Brut", digits=(16, 2), readonly=True)
    total_onss = fields.Float("ONSS", digits=(16, 2), readonly=True)
    total_precompte = fields.Float("Précompte", digits=(16, 2), readonly=True)
    total_net = fields.Float("Net", digits=(16, 2), readonly=True)
    remarques = fields.Text("Remarques de l'agent", readonly=True)
    ligne_ids = fields.One2many("hub.run.ligne", "run_id", "Lignes proposées")
    controles_ok = fields.Boolean(compute="_compute_controles")
    controles_html = fields.Html("Contrôles", compute="_compute_controles", sanitize=False)
    move_id_client = fields.Integer("ID de l'OD chez le client", readonly=True)
    move_lien = fields.Char("OD créée", readonly=True)

    etape_ids = fields.One2many("hub.run.etape", "run_id", "Étapes")

    _CHAMPS_UTILISATEUR = {"reponse", "ligne_ids"}

    @api.depends("dossier_id.code")
    def _compute_name(self):
        for run in self:
            run.name = f"{run.dossier_id.code or '?'}/{run.id or 'nouveau'}"

    def write(self, vals):
        # Dans l'interface, seuls la réponse à une question et les comptes / partenaires
        # des lignes proposées se modifient ; le reste est écrit par le code (sudo).
        if not self.env.su and set(vals) - self._CHAMPS_UTILISATEUR:
            raise AccessError(_("Cette exécution ne se modifie que par ses boutons."))
        return super().write(vals)

    # ------------------------------------------------------------------
    # Contrôles affichés (recalculés localement ; revérifiés chez le client à la création)
    # ------------------------------------------------------------------

    def _anomalies_locales(self):
        lignes = [{
            "libelle": l.libelle, "montant": ods.dec(l.debit or l.credit),
            "sens": "debit" if l.debit else "credit", "categorie": l.categorie,
            "code": l.compte_code or None,
        } for l in self.ligne_ids]
        totaux = {"brut": self.total_brut, "onss": self.total_onss,
                  "precompte": self.total_precompte, "net": self.total_net}
        return ods.controler(lignes, totaux)

    @api.depends("etat", "ligne_ids.compte_code", "ligne_ids.debit", "ligne_ids.credit",
                 "ligne_ids.categorie")
    def _compute_controles(self):
        for run in self:
            if run.etat not in ("proposition", "od_creee"):
                run.controles_ok, run.controles_html = False, False
                continue
            anomalies = run._anomalies_locales()
            run.controles_ok = not anomalies
            run.controles_html = _html_controles(anomalies)

    # ------------------------------------------------------------------
    # Lancement et exécution (cron)
    # ------------------------------------------------------------------

    @api.model
    def _lancer(self, dossier, source, pdf=None, pdf_nom=None, modele=None, periode=None,
                piece_date=None, piece_lien=None, piece_imposee_id=0):
        """Crée le run (en sudo, après vérification de l'accès au dossier) et réveille le cron.

        source : nextcloud / fichier (``pdf`` fourni, conservé le temps du run) ou documents
        (l'agent lit la pièce chez le client ; ``piece_imposee_id`` facultatif).
        """
        dossier.check_access("read")
        secrets = self.env["hub.secrets"]
        icp = self.env["ir.config_parameter"].sudo()
        run = self.sudo().create({
            "dossier_id": dossier.id,
            "user_id": self.env.user.id,
            "modele": modele or secrets._parametre("claude_modele") or MODELE_PAR_DEFAUT,
            "max_etapes": icp.get_int("appalach_hub.agent_max_etapes") or ETAPES_PAR_DEFAUT,
            "budget_tokens": icp.get_int("appalach_hub.agent_budget_tokens") or BUDGET_PAR_DEFAUT,
            "source": source,
            "pdf": base64.b64encode(pdf).decode("ascii") if pdf else False,
            "pdf_nom": pdf_nom or False,
            "piece_nom": pdf_nom or False,
            "piece_date": piece_date or False,
            "piece_lien": piece_lien or False,
            "piece_imposee_id": piece_imposee_id or 0,
            "periode_demandee": periode or False,
        })
        piece = pdf_nom or (_("pièce imposée %s", piece_imposee_id) if piece_imposee_id
                            else _("choisie par l'agent dans Documents"))
        dossier._tracer(Markup("<p><b>OD de salaires — agent lancé</b> ({})</p><p>Source : {}, "
                               "pièce : {}, modèle : {}</p>").format(
            run.name, dict(SOURCES)[source], piece, run.modele))
        self._reveiller_cron()
        return run.sudo(False)

    @api.model
    def _reveiller_cron(self):
        cron = self.env.ref("appalach_hub.ir_cron_hub_run", raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    @api.model
    def _cron_traiter(self, limite=10):
        for _i in range(limite):
            run = self.sudo().search([("etat", "=", "en_file")], order="id", limit=1)
            if not run:
                return
            run.with_context(**{CONTEXTE_CRON: True})._executer()

    def _valider(self):
        """Valide la transaction en cours d'exécution par le cron (étapes visibles au fur et à
        mesure) ; ne fait rien hors cron (tests, actions de l'interface)."""
        if self.env.context.get(CONTEXTE_CRON):
            self.env["ir.cron"]._commit_progress()

    def _executer(self):
        """Exécute ou reprend l'agent. Toute erreur termine le run en échec, tracé."""
        self.ensure_one()
        run = self.sudo()
        run.etat = "en_cours"
        run._valider()
        try:
            run._executer_agent()
        except ERREURS_SERVICES as erreur:
            run._echouer(erreur.args[0] if erreur.args else str(erreur))
        except Exception as erreur:  # noqa: BLE001 — le cron ne doit pas laisser le run « en cours »
            if not self.env.context.get(CONTEXTE_CRON):
                raise
            _logger.exception("Run %s : erreur inattendue", run.id)
            self.env.cr.rollback()
            self.env.invalidate_all()
            run._echouer(_("Erreur inattendue (%s), voir le journal du serveur.",
                           type(erreur).__name__))
        run._valider()

    def _client(self):
        return self.dossier_id._client_odoo(company_id=self.societe_client_id or None)

    def _outils(self, client, etat_outils=None, avec_pdf=False):
        mappings = [{"libelle": m.libelle, "compte_code": m.compte_code,
                     "compte_libelle": m.compte_libelle or ""}
                    for m in self.env["hub.mapping"].search([
                        ("dossier_id", "=", self.dossier_id.id), ("tache", "=", self.tache)])]
        options = {"source": SOURCE_PDF}
        if self.source == "documents":
            documents = self.dossier_id._config_documents(self.tache)
            documents["piece_imposee_id"] = self.piece_imposee_id or None
            options = {"source": SOURCE_DOCUMENTS, "documents": documents}
        elif avec_pdf:
            if not self.pdf:
                raise UserError(_("PDF de l'exécution introuvable."))
            options["pdf_b64"] = base64.b64encode(self.pdf.content).decode("ascii")
        return OutilsOdSalaires(client, mappings, anonymiser=self.dossier_id.anonymiser_historique,
                                etat=etat_outils, **options)

    def _mission(self):
        dossier = self.dossier_id
        periode = (_("Période demandée : %s.", self.periode_demandee) if self.periode_demandee
                   else _("Période : celle de la pièce."))
        if self.source == "documents":
            config = dossier._config_documents(self.tache)
            piece = _("Les pièces sont dans le dossier Documents « %s » de l'Odoo du client "
                      "(lister_pieces, lire_piece).", config["dossier_chemin"])
            if self.piece_imposee_id:
                piece += " " + _("Le comptable impose la pièce %s.", self.piece_imposee_id)
        else:
            piece = _("La pièce est fournie par lire_pdf.")
        return _(
            "Prépare l'OD de salaires de la période à partir de cette pièce, en suivant la façon "
            "dont ce dossier encode ses salaires.\n%(piece)s\nDossier : %(nom)s (%(code)s). "
            "Société du client : %(societe)s. %(periode)s\nJournal utilisé la dernière fois : "
            "%(journal)s.",
            piece=piece, nom=dossier.name, code=dossier.code,
            societe=self.societe_client_nom or "?", periode=periode,
            journal=dossier.od_salaires_journal or _("inconnu"),
        )

    def _executer_agent(self):
        secrets = self.env["hub.secrets"]
        cle_claude = secrets._secret_global("anthropic_api_key")
        client = self._client()
        if not self.societe_client_id:
            self.write({"societe_client_id": client.company_id,
                        "societe_client_nom": client.societe_nom})
        reprise = bool(self.conversation)
        etat = json.loads(self.conversation) if reprise else etat_initial(self._mission())

        def appeler(corps, remplacements):
            return envoyer_message(cle_claude, corps, f"étape {etat['nb_etapes'] + 1}",
                                   remplacements)

        agent = Agent(appeler, self._outils(client, etat.get("outils"), avec_pdf=True),
                      self.modele, self.max_etapes, self.budget_tokens, self._noter_etape(etat))
        if reprise and etat.get("en_attente"):
            resultat = agent.reprendre(etat, self.reponse or "")
        else:
            resultat = agent.executer(etat)
        self._enregistrer_etat(etat)

        if resultat["type"] == "question":
            self.write({
                "etat": "en_attente",
                "question": resultat["question"],
                "question_options": "\n".join(resultat["options"]) or False,
                "question_contexte": resultat["contexte"] or False,
                "reponse": False,
            })
            self.dossier_id._tracer(Markup(
                "<p><b>OD de salaires — question de l'agent</b> ({})</p><p>{}</p>"
            ).format(self.name, resultat["question"]))
            return
        self._enregistrer_proposition(resultat["proposition"])

    def _noter_etape(self, etat):
        Etape = self.env["hub.run.etape"].sudo()

        def noter(type, outil=False, parametres=None, resume="", raisonnement="",
                  tokens_entree=0, tokens_sortie=0, duree=0.0, erreur=False):
            Etape.create({
                "run_id": self.id, "sequence": self._prochaine_sequence(), "type": type,
                "outil": outil or False,
                "parametres": json.dumps(parametres, ensure_ascii=False, indent=1)
                if parametres is not None else False,
                "resultat_resume": (resume or "")[:2000], "raisonnement": raisonnement or False,
                "tokens_entree": tokens_entree, "tokens_sortie": tokens_sortie,
                "duree": duree, "est_erreur": erreur,
            })
            self.write({"nb_etapes": etat["nb_etapes"], "tokens_entree": etat["tokens_entree"],
                        "tokens_sortie": etat["tokens_sortie"]})
            self._valider()
        return noter

    def _prochaine_sequence(self):
        return self.env["hub.run.etape"].sudo().search_count([("run_id", "=", self.id)]) + 1

    def _enregistrer_etat(self, etat):
        self.conversation = json.dumps(etat, ensure_ascii=False)

    def _enregistrer_proposition(self, proposition):
        totaux = proposition["totaux"]
        self.ligne_ids.unlink()
        self.write({
            "etat": "proposition",
            "periode": proposition["periode"],
            "journal_code": proposition["journal"]["code"],
            "journal_client_id": proposition["journal"]["id"],
            "date_od": proposition["date"],
            "reference": proposition["reference"],
            "total_brut": float(totaux["brut"]), "total_onss": float(totaux["onss"]),
            "total_precompte": float(totaux["precompte"]), "total_net": float(totaux["net"]),
            "remarques": proposition["remarques"] or False,
            **self._valeurs_piece(proposition.get("document")),
            "ligne_ids": [(0, 0, {
                "sequence": numero, "libelle": l["libelle"], "compte_code": l["compte_code"],
                "compte_nom": l["compte_nom"], "partenaire_id": l["partenaire_id"],
                "partenaire_nom": l["partenaire_nom"] or False,
                "debit": float(l["debit"]), "credit": float(l["credit"]),
                "categorie": l["categorie"], "ecriture_reference": l["ecriture_reference"] or False,
                "justification": l["justification"], "origine": l["origine"],
            }) for numero, l in enumerate(proposition["lignes"], start=1)],
            "conversation": False,
        })
        origines = Counter(l["origine"] for l in proposition["lignes"])
        self.dossier_id._tracer(Markup(
            "<p><b>OD de salaires — proposition de l'agent</b> ({})</p><ul>"
            "<li>Période : {}, référence : {}, journal : {}</li><li>Pièce : {}</li>"
            "<li>{} ligne(s) — mapping {}, historique {}, PCMN {}</li>"
            "<li>{} appel(s) au modèle, {} tokens</li></ul><p>À valider dans l'exécution.</p>"
        ).format(self.name, self.periode, self.reference, self.journal_code, self.piece_nom or "?",
                 len(proposition["lignes"]), origines["mapping"], origines["historique"],
                 origines["pcmn"], self.nb_etapes, self.tokens_entree + self.tokens_sortie))

    @staticmethod
    def _valeurs_piece(document):
        if not document:
            return {}
        return {"piece_document_id": document["id"], "piece_nom": document["nom"],
                "piece_date": document.get("date") or False,
                "piece_lien": document.get("lien") or False}

    def _echouer(self, message):
        self.env["hub.run.etape"].sudo().create({
            "run_id": self.id, "sequence": self._prochaine_sequence(), "type": "erreur",
            "resultat_resume": message, "est_erreur": True,
        })
        self.write({"etat": "echec", "motif": message})
        self._purger()
        self.dossier_id._tracer(Markup(
            "<p><b>OD de salaires — échec de l'agent</b> ({})</p><p>{}</p>"
        ).format(self.name, message))

    def _purger(self):
        """Supprime le PDF et la conversation (fin de run)."""
        self.sudo().write({"pdf": False, "conversation": False})

    # ------------------------------------------------------------------
    # Actions de l'interface
    # ------------------------------------------------------------------

    def _verifier_acteur(self, etats):
        self.ensure_one()
        self.check_access("write")
        if self.etat not in etats:
            raise UserError(_("Action impossible dans l'état « %s ».",
                              dict(ETATS).get(self.etat, self.etat)))

    def action_repondre(self):
        self._verifier_acteur(("en_attente",))
        if not (self.reponse or "").strip():
            raise UserError(_("Saisissez une réponse."))
        self.sudo().write({"etat": "en_file"})
        self.dossier_id._tracer(Markup(
            "<p><b>OD de salaires — réponse du comptable</b> ({}) : reprise de l'agent.</p>"
        ).format(self.name))
        self._reveiller_cron()
        return True

    def action_rejeter(self):
        self._verifier_acteur(("en_attente", "proposition"))
        self.sudo().write({"etat": "rejete", "motif": _("Rejeté par %s.", self.env.user.name)})
        self._purger()
        self.dossier_id._tracer(Markup("<p><b>OD de salaires — proposition rejetée</b> ({})</p>")
                                .format(self.name))
        return True

    def action_creer_od(self):
        self._verifier_acteur(("proposition",))
        try:
            with self.env.cr.savepoint():
                self.sudo()._creer_od()
        except ERREURS_SERVICES as erreur:
            self.env.invalidate_all()
            message = erreur.args[0] if erreur.args else str(erreur)
            self.dossier_id._tracer(Markup(
                "<p><b>OD de salaires — création refusée</b> ({})</p><p>{}</p>"
            ).format(self.name, message))
            return {"type": "ir.actions.client", "tag": "display_notification", "params": {
                "title": _("OD non créée"), "message": message, "type": "danger", "sticky": True,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            }}
        return True

    def _creer_od(self):
        """Revérifie tout chez le client, puis crée l'OD en brouillon (jamais comptabilisée)."""
        client = self._client()
        outils = self._outils(client)
        anomalies, proposition, doublon = outils.valider_proposition({
            "periode": self.periode, "journal_code": self.journal_code,
            "date": fields.Date.to_string(self.date_od), "reference": self.reference,
            "totaux_controle": {"brut": self.total_brut, "onss": self.total_onss,
                                "precompte": self.total_precompte, "net": self.total_net},
            "lignes": [{
                "libelle": l.libelle, "code_compte": l.compte_code, "partenaire_id": l.partenaire_id,
                "debit": l.debit, "credit": l.credit, "categorie": l.categorie,
                "origine": l.origine,
            } for l in self.ligne_ids],
            "document_id": self.piece_document_id,
        }, origines=False)
        if doublon:
            raise UserError(doublon)
        if anomalies:
            raise UserError(_("Contrôles en échec, OD non créée :\n%s", "\n".join(anomalies)))

        lignes = proposition["lignes"]
        move_id = client.creer_od_brouillon(ods.valeurs_move(
            proposition["journal"]["id"], proposition["date"], proposition["reference"],
            [{"account_id": l["compte_client_id"], "libelle": l["libelle"], "debit": l["debit"],
              "credit": l["credit"], "partner_id": l["partenaire_id"]} for l in lignes],
        ))
        lien = client.lien("account.move", move_id)
        statut, avertissement = self._rattacher_piece(client, move_id, proposition.get("document"))

        # Mappings validés alimentés par l'OD créée.
        Mapping = self.env["hub.mapping"]
        for ligne, controlee in zip(self.ligne_ids, lignes):
            ligne.write({"compte_nom": controlee["compte_nom"],
                         "partenaire_nom": controlee["partenaire_nom"] or False})
            Mapping._memoriser(self.dossier_id, self.tache, ligne.libelle,
                               controlee["compte_code"], controlee["compte_nom"])
        if self.dossier_id.od_salaires_journal != self.journal_code:
            self.dossier_id.od_salaires_journal = self.journal_code

        total = sum((l["debit"] for l in lignes), ods.ZERO)
        self.write({"etat": "od_creee", "move_id_client": move_id, "move_lien": lien,
                    "piece_statut": statut, "piece_avertissement": avertissement or False})
        self._purger()
        self.dossier_id._tracer(Markup(
            "<p><b>OD de salaires — créée en brouillon</b> ({})</p><ul>"
            "<li>Référence : {} (id {} dans l'Odoo du client) — <a href=\"{}\" target=\"_blank\">"
            "ouvrir</a></li><li>Journal : {}</li><li>Total : {} €, {} lignes</li>"
            "<li>Pièce : {} — {}</li><li>Validée par : {}</li></ul>"
            "<p>Non comptabilisée : à valider dans l'Odoo du client.</p>{}"
        ).format(self.name, self.reference, move_id, lien, self.journal_code, ods.euros(total),
                 len(lignes), self.piece_nom or "?", dict(STATUTS_PIECE)[statut],
                 self.env.user.name,
                 Markup("<p><b>⚠ {}</b></p>").format(avertissement) if avertissement else ""))

    def _rattacher_piece(self, client, move_id, document):
        """Après création de l'OD : PDF joint (Nextcloud, fichier) ou document existant lié
        puis marqué traité (Documents). L'OD existe déjà chez le client : un échec ici ne
        l'annule pas, il est signalé. Renvoie (statut, avertissement)."""
        if self.source != "documents":
            if not self.pdf:
                return "avertissement", _("PDF de l'exécution introuvable : non joint à l'OD.")
            try:
                client.joindre_pdf(move_id, self.pdf_nom or f"{self.reference}.pdf",
                                   self.pdf.content)
            except OdooClientErreur as erreur:
                return "avertissement", _("PDF non joint à l'OD : %s", erreur)
            return "jointe", None
        try:
            client.lier_document(document["id"], document.get("attachment_id"), move_id)
        except OdooClientErreur as erreur:
            return "avertissement", _("Pièce non liée à l'OD : %s", erreur)
        config = self.dossier_id._config_documents(self.tache)
        try:
            client.marquer_document(document["id"], etiquette_id=config["etiquette_traite_id"],
                                    dossier_id=config["dossier_traite_id"])
        except OdooClientErreur as erreur:
            return "avertissement", _("Pièce liée mais non marquée traitée : %s", erreur)
        return "liee_traitee", None


class HubRunLigne(models.Model):
    _name = "hub.run.ligne"
    _description = "Ligne d'OD proposée par l'agent"
    _order = "run_id, sequence, id"

    run_id = fields.Many2one("hub.run", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    libelle = fields.Char("Libellé", required=True, readonly=True)
    compte_code = fields.Char("Compte", required=True)
    compte_nom = fields.Char("Nom du compte", readonly=True)
    partenaire_id = fields.Integer("ID partenaire (client)")
    partenaire_nom = fields.Char("Partenaire", readonly=True)
    debit = fields.Float(digits=(16, 2), readonly=True)
    credit = fields.Float(digits=(16, 2), readonly=True)
    categorie = fields.Selection(CATEGORIES, "Catégorie", readonly=True)
    ecriture_reference = fields.Char("Écriture de référence", readonly=True)
    justification = fields.Text(readonly=True)
    origine = fields.Selection(ORIGINES, readonly=True)

    _CHAMPS_MODIFIABLES = {"compte_code", "partenaire_id"}

    def write(self, vals):
        if not self.env.su:
            if set(vals) - self._CHAMPS_MODIFIABLES:
                raise AccessError(_("Seuls le compte et le partenaire d'une ligne se modifient."))
            if any(ligne.run_id.etat != "proposition" for ligne in self):
                raise UserError(_("La proposition n'est plus modifiable."))
            # Compte ou partenaire changé à la main : nom revérifié à la création de l'OD.
            vals = dict(vals, origine="manuel")
            if "compte_code" in vals:
                vals["compte_nom"] = False
            if "partenaire_id" in vals:
                vals["partenaire_nom"] = False
            return super(HubRunLigne, self.sudo()).write(vals)
        return super().write(vals)


class HubRunEtape(models.Model):
    _name = "hub.run.etape"
    _description = "Étape d'une exécution de l'agent"
    _order = "run_id, sequence, id"

    run_id = fields.Many2one("hub.run", required=True, ondelete="cascade", index=True)
    dossier_id = fields.Many2one(related="run_id.dossier_id", store=True)
    sequence = fields.Integer()
    type = fields.Selection([("modele", "Appel du modèle"), ("outil", "Outil"),
                             ("controle", "Contrôle"), ("erreur", "Erreur")], required=True)
    outil = fields.Char()
    parametres = fields.Text("Paramètres")
    resultat_resume = fields.Text("Résultat")
    raisonnement = fields.Text()
    tokens_entree = fields.Integer("Tokens entrée")
    tokens_sortie = fields.Integer("Tokens sortie")
    duree = fields.Float("Durée (s)", digits=(16, 1))
    est_erreur = fields.Boolean("Erreur")


def _html_controles(anomalies):
    if not anomalies:
        return Markup('<p class="text-success mb-0">✔ Contrôles locaux OK : OD équilibrée, totaux '
                      "conformes au récapitulatif, toutes les lignes ont un compte. Comptes, "
                      "journal, partenaires et doublon sont revérifiés chez le client à la "
                      "création.</p>")
    elements = Markup()
    for anomalie in anomalies:
        details = Markup("").join(Markup("<li>{}</li>").format(d) for d in anomalie["details"])
        elements += Markup("<li>{}{}</li>").format(
            anomalie["message"], Markup("<ul>{}</ul>").format(details) if details else ""
        )
    return Markup('<div class="text-danger"><p class="mb-1">✘ Contrôles en échec :</p>'
                  "<ul>{}</ul></div>").format(elements)
