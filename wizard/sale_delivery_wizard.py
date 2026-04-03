from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class SaleDeliveryWizard(models.TransientModel):
    _name = 'sale.delivery.wizard'
    _description = 'Wizard de Entrega desde Orden de Venta'

    sale_order_id = fields.Many2one(
        'sale.order', string='Orden de Venta', required=True)
    partner_id = fields.Many2one(
        related='sale_order_id.partner_id', string='Cliente')
    delivery_address = fields.Text(string='Dirección de Entrega')
    special_instructions = fields.Text(string='Instrucciones Especiales')

    line_ids = fields.One2many(
        'sale.delivery.wizard.line', 'wizard_id', string='Líneas')

    total_selected = fields.Float(
        compute='_compute_totals', string='Total Seleccionado')
    total_available = fields.Float(
        compute='_compute_totals', string='Total Disponible')

    # Reference to pick ticket if one was generated in this session
    pick_ticket_id = fields.Many2one(
        'sale.delivery.document', string='Pick Ticket Generado')

    @api.depends('line_ids.qty_to_deliver', 'line_ids.is_selected')
    def _compute_totals(self):
        for wiz in self:
            selected_lines = wiz.line_ids.filtered('is_selected')
            wiz.total_selected = sum(selected_lines.mapped('qty_to_deliver'))
            wiz.total_available = sum(wiz.line_ids.mapped('qty_available'))

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so_id = res.get('sale_order_id') or self.env.context.get('active_id')
        if not so_id:
            return res
        order = self.env['sale.order'].browse(so_id)
        res['sale_order_id'] = order.id
        res['delivery_address'] = (
            order.partner_shipping_id.contact_address or '')

        lines = []
        for picking in order.picking_ids.filtered(
                lambda p: p.state in ('assigned', 'confirmed')):
            for move in picking.move_ids.filtered(
                    lambda m: m.state in ('assigned', 'confirmed')):
                if move.move_line_ids:
                    for ml in move.move_line_ids:
                        qty_avail = ml.quantity
                        if qty_avail <= 0:
                            qty_avail = move.product_uom_qty
                        lines.append((0, 0, {
                            'picking_id': picking.id,
                            'move_id': move.id,
                            'move_line_id': ml.id,
                            'sale_line_id': move.sale_line_id.id,
                            'product_id': move.product_id.id,
                            'lot_id': ml.lot_id.id if ml.lot_id else False,
                            'qty_available': qty_avail,
                            'qty_to_deliver': 0.0,
                            'is_selected': False,
                            'source_location_id': ml.location_id.id,
                        }))
                else:
                    lines.append((0, 0, {
                        'picking_id': picking.id,
                        'move_id': move.id,
                        'sale_line_id': move.sale_line_id.id,
                        'product_id': move.product_id.id,
                        'qty_available': move.product_uom_qty,
                        'qty_to_deliver': 0.0,
                        'is_selected': False,
                    }))
        res['line_ids'] = lines
        return res

    def _ensure_qty_on_selected(self):
        """For selected lines where qty_to_deliver is 0, auto-fill from
        qty_available. Also refresh qty_available from the move line if
        it was lost during save."""
        for line in self.line_ids.filtered('is_selected'):
            # Refresh qty_available from source if it's 0
            if line.qty_available <= 0 and line.move_line_id:
                line.qty_available = line.move_line_id.quantity or 0.0
            if line.qty_available <= 0 and line.move_id:
                line.qty_available = line.move_id.product_uom_qty
            # Auto-fill qty_to_deliver if still 0
            if line.qty_to_deliver <= 0:
                line.qty_to_deliver = line.qty_available

    def action_select_all(self):
        for line in self.line_ids:
            line.is_selected = True
            line.qty_to_deliver = line.qty_available
        return self._reopen()

    def action_deselect_all(self):
        for line in self.line_ids:
            line.is_selected = False
            line.qty_to_deliver = 0.0
        return self._reopen()

    def action_generate_pick_ticket(self):
        self.ensure_one()
        selected = self.line_ids.filtered('is_selected')
        if not selected:
            raise UserError(_('Seleccione al menos una línea para el pick ticket.'))

        # Auto-fill qty if onchange didn't persist
        self._ensure_qty_on_selected()
        selected = self.line_ids.filtered('is_selected')

        # Validate
        for line in selected:
            if line.qty_to_deliver <= 0:
                raise UserError(_(
                    'La cantidad a entregar debe ser mayor a 0 para %s.',
                    line.product_id.display_name))

        doc = self.env['sale.delivery.document'].create({
            'document_type': 'pick_ticket',
            'sale_order_id': self.sale_order_id.id,
            'delivery_address': self.delivery_address,
            'special_instructions': self.special_instructions,
            'line_ids': [(0, 0, {
                'sale_line_id': line.sale_line_id.id,
                'move_id': line.move_id.id,
                'move_line_id': line.move_line_id.id,
                'product_id': line.product_id.id,
                'lot_id': line.lot_id.id,
                'qty_selected': line.qty_to_deliver,
                'source_location_id': line.source_location_id.id,
            }) for line in selected],
        })
        doc.action_prepare()
        self.pick_ticket_id = doc.id

        return self.env.ref(
            'sale_delivery_wizard.action_report_pick_ticket'
        ).report_action(doc)

    def action_generate_remission(self):
        self.ensure_one()

        selected = self.line_ids.filtered('is_selected')

        # If nothing selected, try loading from pick ticket
        if not selected and self.pick_ticket_id:
            self._load_from_pick_ticket()
            selected = self.line_ids.filtered('is_selected')

        if not selected:
            latest_pt = self.env['sale.delivery.document'].search([
                ('sale_order_id', '=', self.sale_order_id.id),
                ('document_type', '=', 'pick_ticket'),
                ('state', '=', 'prepared'),
            ], order='create_date desc', limit=1)
            if latest_pt:
                self.pick_ticket_id = latest_pt.id
                self._load_from_pick_ticket()
                selected = self.line_ids.filtered('is_selected')

        if not selected:
            raise UserError(_(
                'Seleccione al menos una línea para la remisión, '
                'o genere primero un Pick Ticket.'))

        # Auto-fill qty if onchange didn't persist
        self._ensure_qty_on_selected()
        selected = self.line_ids.filtered('is_selected')

        # Check delivery auth
        order = self.sale_order_id
        if hasattr(order, 'delivery_auth_state'):
            if order.delivery_auth_state == 'pending':
                if not self.env.user.has_group(
                        'sale_delivery_wizard.group_delivery_authorizer'):
                    raise UserError(_(
                        'Entrega bloqueada: pedido sin autorización de pago.'))

        # Validate
        for line in selected:
            if line.qty_to_deliver <= 0:
                raise UserError(_(
                    'La cantidad a entregar debe ser mayor a 0 para %s.',
                    line.product_id.display_name))
            if line.qty_to_deliver > line.qty_available:
                raise UserError(_(
                    'No puede entregar más de lo disponible para %s. '
                    'Disponible: %s, Solicitado: %s',
                    line.product_id.display_name,
                    line.qty_available, line.qty_to_deliver))

        # Group by picking
        picking_lines = {}
        for line in selected:
            picking_lines.setdefault(line.picking_id, []).append(line)

        docs = self.env['sale.delivery.document']
        for picking, lines in picking_lines.items():
            doc = self.env['sale.delivery.document'].create({
                'document_type': 'remission',
                'sale_order_id': order.id,
                'picking_id': picking.id,
                'delivery_address': self.delivery_address,
                'special_instructions': self.special_instructions,
                'line_ids': [(0, 0, {
                    'sale_line_id': line.sale_line_id.id,
                    'move_id': line.move_id.id,
                    'move_line_id': line.move_line_id.id,
                    'product_id': line.product_id.id,
                    'lot_id': line.lot_id.id,
                    'qty_selected': line.qty_to_deliver,
                    'qty_done': line.qty_to_deliver,
                    'source_location_id': line.source_location_id.id,
                }) for line in lines],
            })
            if self.pick_ticket_id:
                doc.message_post(body=_(
                    'Remisión generada desde Pick Ticket: %s',
                    self.pick_ticket_id.name))
            doc.action_confirm()
            docs |= doc

        if len(docs) == 1:
            return self.env.ref(
                'sale_delivery_wizard.action_report_remission'
            ).report_action(docs)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Remisiones Generadas'),
                'message': _('%d remisiones creadas exitosamente.') % len(docs),
                'type': 'success',
                'sticky': False,
            },
        }

    def _load_from_pick_ticket(self):
        if not self.pick_ticket_id:
            return
        pt = self.pick_ticket_id
        for pt_line in pt.line_ids:
            wiz_line = False
            if pt_line.move_line_id:
                wiz_line = self.line_ids.filtered(
                    lambda l: l.move_line_id.id == pt_line.move_line_id.id)
            if not wiz_line and pt_line.move_id and pt_line.lot_id:
                wiz_line = self.line_ids.filtered(
                    lambda l: l.move_id.id == pt_line.move_id.id
                    and l.lot_id.id == pt_line.lot_id.id)
            if not wiz_line:
                wiz_line = self.line_ids.filtered(
                    lambda l: l.product_id.id == pt_line.product_id.id
                    and l.lot_id.id == pt_line.lot_id.id)
            if wiz_line:
                wiz_line = wiz_line[0]
                wiz_line.is_selected = True
                wiz_line.qty_to_deliver = pt_line.qty_selected

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }


class SaleDeliveryWizardLine(models.TransientModel):
    _name = 'sale.delivery.wizard.line'
    _description = 'Línea de Wizard de Entrega'

    wizard_id = fields.Many2one(
        'sale.delivery.wizard', ondelete='cascade', required=True)
    is_selected = fields.Boolean(string='Sel.', default=False)

    picking_id = fields.Many2one('stock.picking', string='Picking')
    move_id = fields.Many2one('stock.move', string='Move')
    move_line_id = fields.Many2one('stock.move.line', string='Move Line')
    sale_line_id = fields.Many2one(
        'sale.order.line', string='Línea de Venta')
    product_id = fields.Many2one(
        'product.product', string='Producto', required=True)
    lot_id = fields.Many2one('stock.lot', string='Lote/Placa')
    source_location_id = fields.Many2one(
        'stock.location', string='Ubicación')

    qty_available = fields.Float(string='Disponible')
    qty_to_deliver = fields.Float(string='A Entregar')

    # Display helpers
    lot_name = fields.Char(related='lot_id.name', string='# Lote')
    product_name = fields.Char(
        related='product_id.display_name', string='Producto Desc.')

    @api.onchange('is_selected')
    def _onchange_is_selected(self):
        if self.is_selected and self.qty_to_deliver <= 0:
            self.qty_to_deliver = self.qty_available
        elif not self.is_selected:
            self.qty_to_deliver = 0.0

    @api.onchange('qty_to_deliver')
    def _onchange_qty_to_deliver(self):
        if self.qty_to_deliver > 0:
            self.is_selected = True
        if self.qty_to_deliver > self.qty_available:
            return {'warning': {
                'title': _('Cantidad excedida'),
                'message': _(
                    'La cantidad a entregar excede lo disponible (%s).',
                    self.qty_available),
            }}