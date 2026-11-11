from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools.odoo_json2 import OdooClientErreur

# Seul le code des assistants crée les copies des listes lues dans l'Odoo du client.
CONTEXTE_IMPORT = "hub_import_client"


class HubCopieClient(models.AbstractModel):
    """Copie temporaire d'un enregistrement de l'Odoo client : jamais créée par l'utilisateur."""

    _name = "hub.copie.client"
    _description = "Copie d'un enregistrement de l'Odoo client"

    client_id = fields.Integer("ID dans l'Odoo client", required=True)
    name = fields.Char(required=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get(CONTEXTE_IMPORT):
            raise UserError(_("Cette liste vient de l'Odoo du client : choisissez un élément "
                              "existant, la création n'est pas possible ici."))
        return super().create(vals_list)

    @api.model
    def _importer(self, vals_list):
        return self.with_context(**{CONTEXTE_IMPORT: True}).create(vals_list)


class HubDocumentsCopieDossier(models.TransientModel):
    _name = "hub.documents.copie.dossier"
    _inherit = ["hub.copie.client"]
    _description = "Dossier Documents de l'Odoo client (choix)"
    _order = "name"

    wizard_id = fields.Many2one("hub.documents.config.wizard", required=True, ondelete="cascade")
    alias_email = fields.Char("Alias")

    @api.depends("name", "alias_email")
    def _compute_display_name(self):
        for dossier in self:
            dossier.display_name = (f"{dossier.name} ({dossier.alias_email})"
                                    if dossier.alias_email else dossier.name)


class HubDocumentsCopieEtiquette(models.TransientModel):
    _name = "hub.documents.copie.etiquette"
    _inherit = ["hub.copie.client"]
    _description = "Étiquette Documents de l'Odoo client (choix)"
    _order = "name"

    wizard_id = fields.Many2one("hub.documents.config.wizard", required=True, ondelete="cascade")


class HubDocumentsConfigWizard(models.TransientModel):
    """Choix, dans des listes lues chez le client, du dossier Documents de chaque tâche et du
    marquage « traité ». Réservé aux gestionnaires."""

    _name = "hub.documents.config.wizard"
    _description = "Configuration Documents d'un dossier"

    dossier_id = fields.Many2one("hub.dossier", required=True, ondelete="cascade", readonly=True)
    societe_client_nom = fields.Char("Société du client", readonly=True)
    dossier_copie_ids = fields.One2many("hub.documents.copie.dossier", "wizard_id")
    etiquette_copie_ids = fields.One2many("hub.documents.copie.etiquette", "wizard_id")
    dossier_od_salaires_id = fields.Many2one(
        "hub.documents.copie.dossier", "Dossier des pièces de salaires",
        domain="[('wizard_id', '=', id)]",
    )
    traitement = fields.Selection(
        [("etiquette", "Ajouter une étiquette"), ("dossier", "Déplacer dans un dossier")],
        "Pièce traitée", default="etiquette", required=True,
    )
    etiquette_traite_id = fields.Many2one(
        "hub.documents.copie.etiquette", "Étiquette « traité »", domain="[('wizard_id', '=', id)]"
    )
    dossier_traite_id = fields.Many2one(
        "hub.documents.copie.dossier", "Dossier « traité »", domain="[('wizard_id', '=', id)]"
    )

    def _charger(self):
        """Lit dossiers et étiquettes chez le client et présélectionne la configuration actuelle."""
        self.ensure_one()
        dossier = self.dossier_id
        dossier._verifier_gestionnaire(_("Seul un gestionnaire du hub configure Documents."))
        try:
            client = dossier._client_odoo()
            dossiers = client.dossiers_documents()
            etiquettes = client.etiquettes_documents()
        except OdooClientErreur as erreur:
            raise UserError(_("Lecture de Documents impossible : %s", erreur)) from None
        chemins = chemins_dossiers(dossiers)
        copies_dossiers = self.env["hub.documents.copie.dossier"]._importer([{
            "wizard_id": self.id, "client_id": d["id"], "name": chemins[d["id"]],
            "alias_email": d.get("alias_email") or False,
        } for d in dossiers])
        copies_etiquettes = self.env["hub.documents.copie.etiquette"]._importer([{
            "wizard_id": self.id, "client_id": e["id"], "name": e["name"],
        } for e in etiquettes])
        par_id = {c.client_id: c for c in copies_dossiers}
        actuel = dossier.documents_dossier_ids.filtered(lambda l: l.tache == "od_salaires")
        self.write({
            "societe_client_nom": client.societe_nom or str(client.company_id),
            "dossier_od_salaires_id": par_id[actuel.dossier_client_id].id
            if actuel and actuel.dossier_client_id in par_id else False,
            "traitement": dossier.documents_traitement or "etiquette",
            "etiquette_traite_id": copies_etiquettes.filtered(
                lambda e: e.client_id == dossier.documents_etiquette_traite_id)[:1].id,
            "dossier_traite_id": par_id[dossier.documents_dossier_traite_id].id
            if dossier.documents_dossier_traite_id in par_id else False,
        })

    def action_valider(self):
        self.ensure_one()
        dossier = self.dossier_id
        dossier._verifier_gestionnaire(_("Seul un gestionnaire du hub configure Documents."))
        if not self.dossier_od_salaires_id:
            raise UserError(_("Choisissez le dossier des pièces de salaires."))
        if self.traitement == "etiquette" and not self.etiquette_traite_id:
            raise UserError(_("Choisissez l'étiquette « traité »."))
        if self.traitement == "dossier":
            if not self.dossier_traite_id:
                raise UserError(_("Choisissez le dossier « traité »."))
            if self.dossier_traite_id == self.dossier_od_salaires_id:
                raise UserError(_("Le dossier « traité » doit différer du dossier des pièces."))
        etiquette = self.etiquette_traite_id if self.traitement == "etiquette" else False
        rangement = self.dossier_traite_id if self.traitement == "dossier" else False
        source = self.dossier_od_salaires_id
        dossier.sudo().write({
            "documents_dossier_ids": [(5, 0, 0), (0, 0, {
                "tache": "od_salaires", "dossier_client_id": source.client_id,
                "dossier_chemin": source.name, "alias_email": source.alias_email or False,
            })],
            "documents_traitement": self.traitement,
            "documents_etiquette_traite_id": etiquette.client_id if etiquette else 0,
            "documents_etiquette_traite_nom": etiquette.name if etiquette else False,
            "documents_dossier_traite_id": rangement.client_id if rangement else 0,
            "documents_dossier_traite_nom": rangement.name if rangement else False,
        })
        marquage = (_("étiquette « %s »", etiquette.name) if etiquette
                    else _("dossier « %s »", rangement.name))
        dossier._tracer(Markup(
            "<p><b>Documents configuré</b></p><ul><li>OD de salaires : dossier « {} »</li>"
            "<li>Pièce traitée : {}</li></ul>"
        ).format(source.name, marquage))
        return {"type": "ir.actions.act_window_close"}


def chemins_dossiers(dossiers):
    """{id: « Parent / Enfant »} à partir de search_read (id, name, folder_id)."""
    par_id = {d["id"]: d for d in dossiers}
    chemins = {}

    def chemin(dossier_id, vus=()):
        if dossier_id in chemins:
            return chemins[dossier_id]
        dossier = par_id[dossier_id]
        parent = (dossier.get("folder_id") or [None])[0]
        if parent in par_id and parent not in vus:
            texte = f"{chemin(parent, vus + (dossier_id,))} / {dossier['name']}"
        else:
            texte = dossier["name"]
        chemins[dossier_id] = texte
        return texte

    for dossier_id in par_id:
        chemin(dossier_id)
    return chemins
