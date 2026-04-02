from odoo import fields, models


class SaleReturnReason(models.Model):
    _name = 'sale.return.reason'
    _description = 'Motivo de Devolución'
    _order = 'sequence, id'

    name = fields.Char(string='Motivo', required=True, translate=True)
    code = fields.Char(string='Código', required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    description = fields.Text(string='Descripción')
