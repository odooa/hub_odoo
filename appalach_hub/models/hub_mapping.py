from odoo import api, fields, models

from ..tools.od_salaires import normaliser

TACHES = [("od_salaires", "OD de salaires")]


class HubMapping(models.Model):
    """Correspondance validée : libellé de rubrique d'une pièce → compte de l'Odoo client."""

    _name = "hub.mapping"
    _description = "Mapping rubrique → compte"
    _inherit = ["mail.thread"]
    _order = "dossier_id, tache, libelle"
    _rec_name = "libelle"

    dossier_id = fields.Many2one(
        "hub.dossier", "Dossier", required=True, ondelete="cascade", index=True, tracking=True
    )
    tache = fields.Selection(TACHES, "Tâche", required=True, default="od_salaires", tracking=True)
    libelle = fields.Char("Libellé de la rubrique", required=True, tracking=True)
    libelle_normalise = fields.Char(compute="_compute_libelle_normalise", store=True, index=True)
    compte_code = fields.Char("Code du compte", required=True, tracking=True)
    compte_libelle = fields.Char("Libellé du compte", tracking=True)
    active = fields.Boolean(default=True, tracking=True)

    _dossier_tache_libelle_uniq = models.Constraint(
        "UNIQUE (dossier_id, tache, libelle_normalise)",
        "Ce libellé de rubrique a déjà un mapping pour ce dossier et cette tâche.",
    )

    @api.depends("libelle")
    def _compute_libelle_normalise(self):
        for mapping in self:
            mapping.libelle_normalise = normaliser(mapping.libelle)

    @api.model
    def _index(self, dossier, tache):
        """{libellé normalisé: mapping} des mappings actifs du dossier pour la tâche."""
        mappings = self.search([("dossier_id", "=", dossier.id), ("tache", "=", tache)])
        return {m.libelle_normalise: m for m in mappings}

    @api.model
    def _memoriser(self, dossier, tache, libelle, code, nom_compte):
        """Crée ou met à jour le mapping (un mapping archivé est réactivé)."""
        cle = normaliser(libelle)
        existant = self.with_context(active_test=False).search([
            ("dossier_id", "=", dossier.id), ("tache", "=", tache), ("libelle_normalise", "=", cle),
        ], limit=1)
        valeurs = {"compte_code": code, "compte_libelle": nom_compte, "active": True}
        if existant:
            changements = {k: v for k, v in valeurs.items() if existant[k] != v}
            if changements:
                existant.write(changements)
            return existant
        return self.create({"dossier_id": dossier.id, "tache": tache, "libelle": libelle, **valeurs})
