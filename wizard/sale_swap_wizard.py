from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class SaleSwapWizard(models.TransientModel):
    _name = 'sale.swap.wizard'
    _description = 'Wizard de Swap de Lotes'

    sale_order_id = fields.Many2one(
        'sale.order', string='Orden de Venta', required=True)
    line_ids = fields.One2many(
        'sale.swap.wizard.line', 'wizard_id', string='Lotes Asignados')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so_id = res.get('sale_order_id') or self.env.context.get('active_id')
        if not so_id:
            return res
        res['sale_order_id'] = so_id
        order = self.env['sale.order'].browse(so_id)

        lines = []
        for picking in order.picking_ids.filtered(
                lambda p: p.state in ('assigned', 'confirmed')
                and p.picking_type_code in ('outgoing', 'internal')):
            for move in picking.move_ids.filtered(
                    lambda m: m.state in ('assigned', 'confirmed')):
                for ml in move.move_line_ids:
                    if ml.lot_id:
                        lot = ml.lot_id
                        lines.append((0, 0, {
                            'product_id': move.product_id.id,
                            'origin_lot_id': lot.id,
                            'move_line_id': ml.id,
                            'picking_id': picking.id,
                            'sale_line_id': move.sale_line_id.id,
                            'qty': ml.quantity or move.product_uom_qty,
                            'origin_bloque': lot.x_bloque or '' if hasattr(lot, 'x_bloque') else '',
                            'origin_atado': lot.x_atado or '' if hasattr(lot, 'x_atado') else '',
                            'origin_alto': str(lot.x_alto) if hasattr(lot, 'x_alto') and lot.x_alto else '',
                            'origin_ancho': str(lot.x_ancho) if hasattr(lot, 'x_ancho') and lot.x_ancho else '',
                            'origin_grosor': str(lot.x_grosor) if hasattr(lot, 'x_grosor') and lot.x_grosor else '',
                        }))
        res['line_ids'] = lines
        return res

    def action_confirm_swap(self):
        """Execute lot swaps on pending pickings."""
        self.ensure_one()

        # Read target_lot_id directly from DB to avoid web_save overwrites.
        # The OWL widget persists via orm.write, but web_save may reset
        # the ORM cache — search_read bypasses that.
        line_data = self.env['sale.swap.wizard.line'].search_read(
            [('wizard_id', '=', self.id)],
            ['id', 'target_lot_id', 'origin_lot_id', 'product_id',
             'move_line_id', 'picking_id', 'sale_line_id', 'qty',
             'origin_bloque'],
        )

        lines_with_target = [
            d for d in line_data if d.get('target_lot_id')
        ]
        if not lines_with_target:
            raise UserError(_(
                'Seleccione al menos un lote destino para ejecutar el swap.'))

        for data in lines_with_target:
            origin_lot_id = data['origin_lot_id'][0]
            target_lot_id = data['target_lot_id'][0]
            target_lot_name = data['target_lot_id'][1]
            origin_lot_name = data['origin_lot_id'][1]

            if origin_lot_id == target_lot_id:
                raise UserError(_(
                    'El lote origen y destino no pueden ser el mismo (%s).',
                    origin_lot_name))

            move_line = self.env['stock.move.line'].browse(
                data['move_line_id'][0]) if data.get('move_line_id') else False
            if not move_line or move_line.state not in ('assigned', 'confirmed'):
                raise UserError(_(
                    'No se encontró movimiento pendiente para el lote %s.',
                    origin_lot_name))

            target_lot = self.env['stock.lot'].browse(target_lot_id)
            origin_lot = self.env['stock.lot'].browse(origin_lot_id)

            # Check target lot availability
            target_quant = self.env['stock.quant'].search([
                ('lot_id', '=', target_lot_id),
                ('location_id.usage', '=', 'internal'),
                ('quantity', '>', 0),
            ], limit=1)
            if not target_quant:
                raise UserError(_(
                    'El lote destino %s no tiene stock disponible.',
                    target_lot_name))

            # Check hold status if stock_lot_dimensions is installed
            if hasattr(target_lot, 'hold_order_ids'):
                active_holds = target_lot.hold_order_ids.filtered(
                    lambda h: h.state == 'active'
                    and h.sale_order_id != self.sale_order_id)
                if active_holds:
                    raise UserError(_(
                        'El lote %s está apartado en otra orden (%s).',
                        target_lot_name,
                        active_holds[0].sale_order_id.name))

            target_qty = target_quant.quantity
            old_lot_name = origin_lot_name

            # Execute swap on the move line
            move_line.lot_id = target_lot_id
            move_line.quantity = target_qty

            # Update the move demand if qty changed
            if move_line.move_id:
                total_ml_qty = sum(
                    move_line.move_id.move_line_ids.mapped('quantity'))
                if total_ml_qty != move_line.move_id.product_uom_qty:
                    move_line.move_id.product_uom_qty = total_ml_qty

            # Create swap record in delivery document
            picking_id = data['picking_id'][0] if data.get('picking_id') else False
            sale_line_id = data['sale_line_id'][0] if data.get('sale_line_id') else False
            product_id = data['product_id'][0]
            qty = data.get('qty', 0.0)
            origin_bloque = data.get('origin_bloque', '')

            self.env['sale.delivery.document'].create({
                'document_type': 'pick_ticket',
                'state': 'confirmed',
                'sale_order_id': self.sale_order_id.id,
                'special_instructions': _(
                    'SWAP: %s (Bloque: %s, %.2f m²) → %s (Bloque: %s, %.2f m²)',
                    old_lot_name,
                    origin_bloque or 'S/B',
                    qty,
                    target_lot_name,
                    target_lot.x_bloque or 'S/B' if hasattr(target_lot, 'x_bloque') else 'S/B',
                    target_qty),
                'line_ids': [
                    (0, 0, {
                        'product_id': product_id,
                        'lot_id': origin_lot_id,
                        'qty_selected': qty,
                        'is_swap_origin': True,
                    }),
                    (0, 0, {
                        'product_id': product_id,
                        'lot_id': target_lot_id,
                        'qty_selected': target_qty,
                        'is_swap_target': True,
                    }),
                ],
            })

            # Update SO line lot_ids if sale_stone_selection is installed
            sale_line = self.env['sale.order.line'].browse(
                sale_line_id) if sale_line_id else False
            if (sale_line and hasattr(sale_line, 'lot_ids')
                    and origin_lot in sale_line.lot_ids):
                sale_line.lot_ids = [
                    (3, origin_lot_id),
                    (4, target_lot_id),
                ]

            _logger.info(
                'Swap executed: %s → %s on picking %s',
                old_lot_name, target_lot_name,
                data['picking_id'][1] if data.get('picking_id') else 'N/A')

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Swap Completado'),
                'message': _(
                    '%d swap(s) realizados exitosamente.') % len(lines_with_target),
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
        'product.product', string='Producto', readonly=True)
    origin_lot_id = fields.Many2one(
        'stock.lot', string='Lote Actual', readonly=True)
    target_lot_id = fields.Many2one(
        'stock.lot', string='Lote Nuevo',
        domain="[('product_id', '=', product_id), ('id', '!=', origin_lot_id)]")
    qty = fields.Float(string='m² Actual', readonly=True)
    move_line_id = fields.Many2one('stock.move.line', string='Move Line')
    picking_id = fields.Many2one('stock.picking', string='Picking')
    sale_line_id = fields.Many2one('sale.order.line', string='Línea de Venta')

    # Origin lot info
    origin_bloque = fields.Char(string='Bloque', readonly=True)
    origin_atado = fields.Char(string='Atado', readonly=True)
    origin_alto = fields.Char(string='Alto', readonly=True)
    origin_ancho = fields.Char(string='Ancho', readonly=True)
    origin_grosor = fields.Char(string='Grosor', readonly=True)

    # Target lot info (computed on selection)
    target_bloque = fields.Char(
        string='Bloque Nuevo', compute='_compute_target_info', readonly=True)
    target_atado = fields.Char(
        string='Atado Nuevo', compute='_compute_target_info', readonly=True)
    target_alto = fields.Char(
        string='Alto Nuevo', compute='_compute_target_info', readonly=True)
    target_ancho = fields.Char(
        string='Ancho Nuevo', compute='_compute_target_info', readonly=True)
    target_grosor = fields.Char(
        string='Grosor Nuevo', compute='_compute_target_info', readonly=True)
    target_qty = fields.Float(
        string='m² Nuevo', compute='_compute_target_info', readonly=True)

    @api.depends('target_lot_id')
    def _compute_target_info(self):
        for line in self:
            lot = line.target_lot_id
            if lot:
                line.target_bloque = lot.x_bloque if hasattr(lot, 'x_bloque') else ''
                line.target_atado = lot.x_atado if hasattr(lot, 'x_atado') else ''
                line.target_alto = str(lot.x_alto) if hasattr(lot, 'x_alto') and lot.x_alto else ''
                line.target_ancho = str(lot.x_ancho) if hasattr(lot, 'x_ancho') and lot.x_ancho else ''
                line.target_grosor = str(lot.x_grosor) if hasattr(lot, 'x_grosor') and lot.x_grosor else ''
                quant = self.env['stock.quant'].search([
                    ('lot_id', '=', lot.id),
                    ('location_id.usage', '=', 'internal'),
                    ('quantity', '>', 0),
                ], limit=1)
                line.target_qty = quant.quantity if quant else 0.0
            else:
                line.target_bloque = ''
                line.target_atado = ''
                line.target_alto = ''
                line.target_ancho = ''
                line.target_grosor = ''
                line.target_qty = 0.0