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
        # Re-read from DB to get persisted target_lot_id values
        self.line_ids.invalidate_recordset()
        lines_to_swap = self.line_ids.filtered(lambda l: l.target_lot_id)

        _logger.info(
            'Swap confirm: %d total lines, %d with target_lot_id',
            len(self.line_ids), len(lines_to_swap))
        for line in self.line_ids:
            _logger.info(
                '  Line %s: origin=%s target=%s',
                line.id, line.origin_lot_id.name,
                line.target_lot_id.name if line.target_lot_id else 'NONE')

        if not lines_to_swap:
            raise UserError(_(
                'Seleccione al menos un lote destino para ejecutar el swap.'))

        for line in lines_to_swap:
            if line.origin_lot_id == line.target_lot_id:
                raise UserError(_(
                    'El lote origen y destino no pueden ser el mismo (%s).',
                    line.origin_lot_id.name))

            move_line = line.move_line_id
            if not move_line or move_line.state not in ('assigned', 'confirmed'):
                raise UserError(_(
                    'No se encontró movimiento pendiente para el lote %s.',
                    line.origin_lot_id.name))

            target_quant = self.env['stock.quant'].search([
                ('lot_id', '=', line.target_lot_id.id),
                ('location_id.usage', '=', 'internal'),
                ('quantity', '>', 0),
            ], limit=1)
            if not target_quant:
                raise UserError(_(
                    'El lote destino %s no tiene stock disponible.',
                    line.target_lot_id.name))

            if hasattr(line.target_lot_id, 'hold_order_ids'):
                active_holds = line.target_lot_id.hold_order_ids.filtered(
                    lambda h: h.state == 'active'
                    and h.sale_order_id != self.sale_order_id)
                if active_holds:
                    raise UserError(_(
                        'El lote %s está apartado en otra orden (%s).',
                        line.target_lot_id.name,
                        active_holds[0].sale_order_id.name))

            target_qty = target_quant.quantity
            old_lot_name = line.origin_lot_id.name
            new_lot = line.target_lot_id

            move_line.lot_id = new_lot.id
            move_line.quantity = target_qty

            if move_line.move_id:
                total_ml_qty = sum(
                    move_line.move_id.move_line_ids.mapped('quantity'))
                if total_ml_qty != move_line.move_id.product_uom_qty:
                    move_line.move_id.product_uom_qty = total_ml_qty

            self.env['sale.delivery.document'].create({
                'document_type': 'pick_ticket',
                'state': 'confirmed',
                'sale_order_id': self.sale_order_id.id,
                'special_instructions': _(
                    'SWAP: %s (Bloque: %s, %.2f m²) → %s (Bloque: %s, %.2f m²)',
                    old_lot_name,
                    line.origin_bloque or 'S/B',
                    line.qty,
                    new_lot.name,
                    new_lot.x_bloque or 'S/B' if hasattr(new_lot, 'x_bloque') else 'S/B',
                    target_qty),
                'line_ids': [
                    (0, 0, {
                        'product_id': line.product_id.id,
                        'lot_id': line.origin_lot_id.id,
                        'qty_selected': line.qty,
                        'is_swap_origin': True,
                    }),
                    (0, 0, {
                        'product_id': line.product_id.id,
                        'lot_id': new_lot.id,
                        'qty_selected': target_qty,
                        'is_swap_target': True,
                    }),
                ],
            })

            if (line.sale_line_id and hasattr(line.sale_line_id, 'lot_ids')
                    and line.origin_lot_id in line.sale_line_id.lot_ids):
                line.sale_line_id.lot_ids = [
                    (3, line.origin_lot_id.id),
                    (4, new_lot.id),
                ]

            _logger.info(
                'Swap executed: %s → %s on picking %s',
                old_lot_name, new_lot.name,
                line.picking_id.name)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Swap Completado'),
                'message': _(
                    '%d swap(s) realizados exitosamente.') % len(lines_to_swap),
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

    origin_bloque = fields.Char(string='Bloque', readonly=True)
    origin_atado = fields.Char(string='Atado', readonly=True)
    origin_alto = fields.Char(string='Alto', readonly=True)
    origin_ancho = fields.Char(string='Ancho', readonly=True)
    origin_grosor = fields.Char(string='Grosor', readonly=True)

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

    def action_open_lot_picker(self):
        """Return a client action that the JS will handle.
        The wizard MUST be saved before this is called so lines have DB IDs.
        Buttons type=object in Odoo 19 trigger web_save first, ensuring this.
        """
        self.ensure_one()
        _logger.info(
            'action_open_lot_picker: line_id=%s product=%s origin_lot=%s',
            self.id, self.product_id.id, self.origin_lot_id.name)
        return {
            'type': 'ir.actions.client',
            'tag': 'swap_open_lot_picker',
            'params': {
                'wizard_id': self.wizard_id.id,
                'line_id': self.id,
                'product_id': self.product_id.id,
                'product_name': self.product_id.display_name,
                'origin_lot_id': self.origin_lot_id.id,
                'origin_lot_name': self.origin_lot_id.name or '',
                'current_target_lot_id': self.target_lot_id.id if self.target_lot_id else False,
                'current_target_lot_name': self.target_lot_id.name if self.target_lot_id else '',
                'exclude_lot_ids': self.wizard_id.line_ids.mapped('origin_lot_id').ids,
            },
        }

    def action_write_target_lot(self, lot_id):
        """Write target lot directly via ORM. Called from JS."""
        self.ensure_one()
        self.write({'target_lot_id': lot_id or False})
        _logger.info(
            'action_write_target_lot: line=%s lot_id=%s', self.id, lot_id)
        return True