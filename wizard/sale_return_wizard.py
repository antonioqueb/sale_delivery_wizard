from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaleReturnWizard(models.TransientModel):
    _name = 'sale.return.wizard'
    _description = 'Wizard de Devolución desde Orden de Venta'

    sale_order_id = fields.Many2one(
        'sale.order', string='Orden de Venta', required=True)
    return_reason_id = fields.Many2one(
        'sale.return.reason', string='Motivo', required=True)
    return_action = fields.Selection([
        ('reagendar', 'Reagendar - Reentrega del mismo material'),
        ('reponer', 'Reponer - Liberar y asignar nuevo material'),
        ('finiquitar', 'Finiquitar - Cerrar línea y nota de crédito'),
    ], string='Acción', required=True)
    notes = fields.Text(string='Notas')
    line_ids = fields.One2many(
        'sale.return.wizard.line', 'wizard_id', string='Líneas')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so_id = res.get('sale_order_id') or self.env.context.get('active_id')
        if not so_id:
            return res
        order = self.env['sale.order'].browse(so_id)
        res['sale_order_id'] = order.id

        lines = []
        # Only show done outgoing pickings (delivered material)
        for picking in order.picking_ids.filtered(
                lambda p: p.state == 'done'
                and p.picking_type_code == 'outgoing'):
            for move in picking.move_ids.filtered(
                    lambda m: m.state == 'done'):
                for ml in move.move_line_ids:
                    qty = ml.quantity or ml.qty_done or 0.0
                    if qty > 0:
                        lines.append((0, 0, {
                            'move_id': move.id,
                            'move_line_id': ml.id,
                            'sale_line_id': move.sale_line_id.id,
                            'product_id': move.product_id.id,
                            'lot_id': ml.lot_id.id if ml.lot_id else False,
                            'qty_delivered': qty,
                            'qty_to_return': 0.0,
                            'is_selected': False,
                        }))
        res['line_ids'] = lines
        return res

    def action_confirm_return(self):
        """Process the return."""
        self.ensure_one()
        selected = self.line_ids.filtered('is_selected')
        if not selected:
            raise UserError(_(
                'Seleccione al menos una línea para devolver.'))

        order = self.sale_order_id

        # Create return picking via stock.return.picking wizard
        # Group by original picking
        move_returns = {}
        for line in selected:
            if line.qty_to_return <= 0:
                raise UserError(_(
                    'La cantidad a devolver debe ser mayor a 0 para %s.',
                    line.product_id.display_name))
            picking = line.move_id.picking_id
            move_returns.setdefault(picking, []).append(line)

        return_pickings = self.env['stock.picking']
        for picking, lines in move_returns.items():
            # Use standard return wizard
            return_wiz = self.env['stock.return.picking'].with_context(
                active_id=picking.id,
                active_model='stock.picking',
            ).create({})
            # Customize return lines
            return_wiz.product_return_moves.unlink()
            for line in lines:
                self.env['stock.return.picking.line'].create({
                    'wizard_id': return_wiz.id,
                    'product_id': line.product_id.id,
                    'quantity': line.qty_to_return,
                    'move_id': line.move_id.id,
                    'uom_id': line.move_id.product_uom.id,
                })
            result = return_wiz.action_create_returns()
            if result and result.get('res_id'):
                ret_picking = self.env['stock.picking'].browse(
                    result['res_id'])
                return_pickings |= ret_picking

        # Create delivery document for each return picking
        docs = self.env['sale.delivery.document']
        for ret_picking in return_pickings:
            doc = self.env['sale.delivery.document'].create({
                'document_type': 'return',
                'sale_order_id': order.id,
                'return_picking_id': ret_picking.id,
                'return_reason_id': self.return_reason_id.id,
                'return_action': self.return_action,
                'line_ids': [(0, 0, {
                    'product_id': line.product_id.id,
                    'lot_id': line.lot_id.id,
                    'qty_selected': line.qty_to_return,
                    'sale_line_id': line.sale_line_id.id,
                    'move_id': line.move_id.id,
                }) for line in selected],
            })
            docs |= doc

        # Process based on return action
        if self.return_action == 'reagendar':
            # Material stays linked to the order for re-delivery
            pass
        elif self.return_action == 'reponer':
            # Material freed to general inventory
            # Line stays pending for new assignment
            pass
        elif self.return_action == 'finiquitar':
            # TODO: Trigger credit note creation
            pass

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Devolución Registrada'),
                'message': _(
                    'Se creó devolución con acción: %s. '
                    'Procese el picking de devolución para completar.',
                    dict(self._fields['return_action'].selection).get(
                        self.return_action)),
                'type': 'warning',
                'sticky': True,
            },
        }


class SaleReturnWizardLine(models.TransientModel):
    _name = 'sale.return.wizard.line'
    _description = 'Línea de Wizard de Devolución'

    wizard_id = fields.Many2one(
        'sale.return.wizard', ondelete='cascade', required=True)
    is_selected = fields.Boolean(string='Sel.', default=False)
    move_id = fields.Many2one('stock.move', string='Move')
    move_line_id = fields.Many2one('stock.move.line', string='Move Line')
    sale_line_id = fields.Many2one(
        'sale.order.line', string='Línea de Venta')
    product_id = fields.Many2one(
        'product.product', string='Producto', required=True)
    lot_id = fields.Many2one('stock.lot', string='Lote/Placa')
    qty_delivered = fields.Float(string='Entregado')
    qty_to_return = fields.Float(string='A Devolver')

    def _refresh_qty_delivered(self):
        """Re-read qty_delivered from move_line if lost by transient save."""
        if self.qty_delivered <= 0 and self.move_line_id:
            self.qty_delivered = (
                self.move_line_id.quantity
                or self.move_line_id.qty_done
                or 0.0)

    @api.onchange('is_selected')
    def _onchange_is_selected(self):
        if self.is_selected:
            self._refresh_qty_delivered()
            if self.qty_to_return <= 0:
                self.qty_to_return = self.qty_delivered
        else:
            self.qty_to_return = 0.0

    @api.onchange('qty_to_return')
    def _onchange_qty_to_return(self):
        if self.qty_to_return > 0:
            self.is_selected = True
        self._refresh_qty_delivered()
        if self.qty_to_return > self.qty_delivered and self.qty_delivered > 0:
            return {'warning': {
                'title': _('Cantidad excedida'),
                'message': _(
                    'La cantidad a devolver excede lo entregado (%s).',
                    self.qty_delivered),
            }}