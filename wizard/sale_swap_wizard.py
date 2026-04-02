from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaleSwapWizard(models.TransientModel):
    _name = 'sale.swap.wizard'
    _description = 'Wizard de Swap de Lotes'

    sale_order_id = fields.Many2one(
        'sale.order', string='Orden de Venta', required=True)
    line_ids = fields.One2many(
        'sale.swap.wizard.line', 'wizard_id', string='Swaps')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so_id = res.get('sale_order_id') or self.env.context.get('active_id')
        if not so_id:
            return res
        res['sale_order_id'] = so_id
        return res

    def action_confirm_swap(self):
        """Execute lot swaps on pending pickings."""
        self.ensure_one()
        for line in self.line_ids:
            if not line.origin_lot_id or not line.target_lot_id:
                raise UserError(_(
                    'Debe seleccionar lote origen y destino para cada swap.'))
            if line.origin_lot_id == line.target_lot_id:
                raise UserError(_(
                    'El lote origen y destino no pueden ser el mismo.'))

            # Find the move line with the origin lot
            move_line = self.env['stock.move.line'].search([
                ('picking_id.sale_id', '=', self.sale_order_id.id),
                ('lot_id', '=', line.origin_lot_id.id),
                ('state', 'in', ('assigned', 'confirmed')),
            ], limit=1)
            if not move_line:
                raise UserError(_(
                    'No se encontró movimiento pendiente para el lote %s.',
                    line.origin_lot_id.name))

            # Check target lot availability
            target_quant = self.env['stock.quant'].search([
                ('lot_id', '=', line.target_lot_id.id),
                ('location_id.usage', '=', 'internal'),
                ('quantity', '>', 0),
            ], limit=1)
            if not target_quant:
                raise UserError(_(
                    'El lote destino %s no tiene stock disponible.',
                    line.target_lot_id.name))

            # Check hold status if stock_lot_dimensions is installed
            if hasattr(line.target_lot_id, 'hold_order_ids'):
                active_holds = line.target_lot_id.hold_order_ids.filtered(
                    lambda h: h.state == 'active'
                    and h.sale_order_id != self.sale_order_id)
                if active_holds:
                    raise UserError(_(
                        'El lote %s está apartado en otra orden.',
                        line.target_lot_id.name))

            # Execute swap
            move_line.lot_id = line.target_lot_id.id

            # Create swap record in delivery document
            self.env['sale.delivery.document'].create({
                'document_type': 'pick_ticket',
                'state': 'confirmed',
                'sale_order_id': self.sale_order_id.id,
                'special_instructions': _(
                    'SWAP: %s → %s',
                    line.origin_lot_id.name,
                    line.target_lot_id.name),
                'line_ids': [
                    (0, 0, {
                        'product_id': line.product_id.id,
                        'lot_id': line.origin_lot_id.id,
                        'qty_selected': line.qty,
                        'is_swap_origin': True,
                    }),
                    (0, 0, {
                        'product_id': line.product_id.id,
                        'lot_id': line.target_lot_id.id,
                        'qty_selected': line.qty,
                        'is_swap_target': True,
                    }),
                ],
            })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Swap Completado'),
                'message': _(
                    '%d swap(s) realizados exitosamente.') % len(self.line_ids),
                'type': 'success',
                'sticky': False,
            },
        }


class SaleSwapWizardLine(models.TransientModel):
    _name = 'sale.swap.wizard.line'
    _description = 'Línea de Swap'

    wizard_id = fields.Many2one(
        'sale.swap.wizard', ondelete='cascade', required=True)
    product_id = fields.Many2one(
        'product.product', string='Producto', required=True)
    origin_lot_id = fields.Many2one(
        'stock.lot', string='Lote Origen', required=True)
    target_lot_id = fields.Many2one(
        'stock.lot', string='Lote Destino', required=True)
    qty = fields.Float(string='Cantidad', default=1.0)
