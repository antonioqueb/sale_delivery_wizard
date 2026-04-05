from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


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
                            'qty_to_return': qty,
                            'is_selected': True,
                        }))
        res['line_ids'] = lines
        return res

    def action_confirm_return(self):
        """Process the return: create return picking, validate it, and create document."""
        self.ensure_one()
        selected = self.line_ids.filtered('is_selected')
        if not selected:
            raise UserError(_(
                'Seleccione al menos una línea para devolver.'))

        order = self.sale_order_id

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
            return_wiz = self.env['stock.return.picking'].with_context(
                active_id=picking.id,
                active_model='stock.picking',
            ).create({})
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

        # Create delivery document and validate each return picking
        docs = self.env['sale.delivery.document']
        for ret_picking in return_pickings:
            orig_picking = ret_picking.move_ids.mapped(
                'origin_returned_move_id.picking_id')
            picking_lines = [
                l for l in selected
                if l.move_id.picking_id in orig_picking
            ]
            if not picking_lines:
                picking_lines = selected

            doc = self.env['sale.delivery.document'].create({
                'document_type': 'return',
                'sale_order_id': order.id,
                'return_picking_id': ret_picking.id,
                'return_reason_id': self.return_reason_id.id,
                'return_action': self.return_action,
                'special_instructions': self.notes or '',
                'line_ids': [(0, 0, {
                    'product_id': line.product_id.id,
                    'lot_id': line.lot_id.id,
                    'qty_selected': line.qty_to_return,
                    'qty_done': line.qty_to_return,
                    'sale_line_id': line.sale_line_id.id,
                    'move_id': line.move_id.id,
                    'move_line_id': line.move_line_id.id,
                }) for line in picking_lines],
            })

            # Validate the return picking (receive material back)
            self._validate_return_picking(ret_picking, picking_lines)

            doc.state = 'confirmed'
            doc.delivery_date = fields.Datetime.now()
            docs |= doc

        # ── Post-return action ──
        if self.return_action == 'reagendar':
            self._action_reagendar(order, selected)

        action_label = dict(
            self._fields['return_action'].selection
        ).get(self.return_action)

        if self.return_action == 'reagendar':
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Devolución Procesada — Reentrega Creada'),
                    'message': _(
                        'Se recibió la devolución y se creó una reentrega '
                        'pendiente con el mismo material. '
                        'Confirme la reentrega cuando el material sea entregado.'),
                    'type': 'success',
                    'sticky': True,
                },
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Devolución Procesada'),
                'message': _(
                    '%d devolución(es) procesada(s) con acción: %s.',
                    len(docs), action_label),
                'type': 'success',
                'sticky': False,
            },
        }

    # ──────────────────────────────────────────────
    #  REAGENDAR: create new outgoing picking + redelivery doc
    # ──────────────────────────────────────────────

    def _action_reagendar(self, order, wizard_lines):
        """Create a new outgoing picking with the same lots/qty so the material
        stays reserved for this SO. Groups by product (one move per product,
        multiple move lines for lots).
        """
        warehouse = order.warehouse_id
        pick_type = warehouse.out_type_id
        if not pick_type:
            pick_type = self.env['stock.picking.type'].search([
                ('code', '=', 'outgoing'),
                ('warehouse_id', '=', warehouse.id),
            ], limit=1)
        if not pick_type:
            raise UserError(_(
                'No se encontró tipo de picking de salida para el almacén %s.',
                warehouse.name))

        new_picking = self.env['stock.picking'].create({
            'picking_type_id': pick_type.id,
            'partner_id': order.partner_shipping_id.id or order.partner_id.id,
            'origin': order.name,
            'location_id': pick_type.default_location_src_id.id,
            'location_dest_id': pick_type.default_location_dest_id.id
                or self.env.ref('stock.stock_location_customers').id,
            'sale_id': order.id,
        })

        # ── Group wizard lines by (product_id, sale_line_id, uom) ──
        grouped = {}
        for wl in wizard_lines:
            key = (wl.product_id.id, wl.sale_line_id.id, wl.move_id.product_uom.id)
            grouped.setdefault(key, []).append(wl)

        move_map = {}  # sale_line_id -> move, for doc lines later
        for (product_id, sale_line_id, uom_id), wls in grouped.items():
            total_qty = sum(wl.qty_to_return for wl in wls)

            move = self.env['stock.move'].create({
                'product_id': product_id,
                'product_uom_qty': total_qty,
                'product_uom': uom_id,
                'picking_id': new_picking.id,
                'location_id': new_picking.location_id.id,
                'location_dest_id': new_picking.location_dest_id.id,
                'sale_line_id': sale_line_id,
                'origin': order.name,
            })
            move_map[sale_line_id] = move

            # Create one move line per lot
            for wl in wls:
                if wl.lot_id:
                    self.env['stock.move.line'].create({
                        'move_id': move.id,
                        'product_id': product_id,
                        'lot_id': wl.lot_id.id,
                        'quantity': wl.qty_to_return,
                        'location_id': new_picking.location_id.id,
                        'location_dest_id': new_picking.location_dest_id.id,
                        'picking_id': new_picking.id,
                    })

        # Confirm and assign
        new_picking.action_confirm()
        new_picking.action_assign()

        _logger.info(
            'Redelivery picking %s created for SO %s (state: %s)',
            new_picking.name, order.name, new_picking.state)

        # Create redelivery document (prepared = pending confirmation)
        doc_lines = []
        for wl in wizard_lines:
            move = move_map.get(wl.sale_line_id.id)
            doc_lines.append((0, 0, {
                'product_id': wl.product_id.id,
                'lot_id': wl.lot_id.id,
                'qty_selected': wl.qty_to_return,
                'sale_line_id': wl.sale_line_id.id,
                'move_id': move.id if move else False,
                'source_location_id': new_picking.location_id.id,
            }))

        doc = self.env['sale.delivery.document'].create({
            'document_type': 'redelivery',
            'sale_order_id': order.id,
            'picking_id': new_picking.id,
            'delivery_address': order.partner_shipping_id.contact_address or '',
            'special_instructions': _(
                'REENTREGA por devolución. Material: %s',
                ', '.join(
                    '%s [%s] x%s' % (
                        wl.product_id.display_name,
                        wl.lot_id.name or 'S/L',
                        wl.qty_to_return,
                    ) for wl in wizard_lines
                )),
            'line_ids': doc_lines,
        })
        doc.action_prepare()

        _logger.info(
            'Redelivery document %s created for SO %s',
            doc.name, order.name)

        return doc

    # ──────────────────────────────────────────────
    #  Validate return picking
    # ──────────────────────────────────────────────

    def _validate_return_picking(self, picking, wizard_lines):
        """Assign and validate the return picking with the correct quantities."""
        if picking.state in ('draft', 'confirmed', 'waiting'):
            picking.action_assign()

        lot_qty = {}
        product_qty = {}
        for wl in wizard_lines:
            if wl.lot_id:
                lot_qty[wl.lot_id.id] = (
                    lot_qty.get(wl.lot_id.id, 0.0) + wl.qty_to_return)
            else:
                product_qty[wl.product_id.id] = (
                    product_qty.get(wl.product_id.id, 0.0)
                    + wl.qty_to_return)

        for move in picking.move_ids:
            if move.move_line_ids:
                for ml in move.move_line_ids:
                    lot_id = ml.lot_id.id if ml.lot_id else False
                    if lot_id and lot_id in lot_qty:
                        ml.quantity = lot_qty[lot_id]
                    elif ml.product_id.id in product_qty:
                        ml.quantity = product_qty[ml.product_id.id]
                    else:
                        ml.quantity = 0
            else:
                qty = 0.0
                lot_id = False
                for wl in wizard_lines:
                    if wl.product_id.id == move.product_id.id:
                        qty += wl.qty_to_return
                        if wl.lot_id:
                            lot_id = wl.lot_id.id

                if qty > 0:
                    self.env['stock.move.line'].create({
                        'move_id': move.id,
                        'picking_id': picking.id,
                        'product_id': move.product_id.id,
                        'lot_id': lot_id,
                        'quantity': qty,
                        'location_id': move.location_id.id,
                        'location_dest_id': move.location_dest_id.id,
                    })

        result = picking.with_context(
            skip_backorder=False,
        ).button_validate()

        if isinstance(result, dict):
            if result.get('res_model') == 'stock.backorder.confirmation':
                backorder_wiz = self.env['stock.backorder.confirmation'].with_context(
                    button_validate_picking_ids=picking.ids,
                ).create({
                    'pick_ids': [(4, picking.id)],
                })
                backorder_wiz.process()
            elif result.get('res_model') == 'stock.immediate.transfer':
                immediate_wiz = self.env['stock.immediate.transfer'].with_context(
                    button_validate_picking_ids=picking.ids,
                ).create({
                    'pick_ids': [(4, picking.id)],
                })
                immediate_wiz.process()

        if picking.state != 'done':
            _logger.warning(
                'Return picking %s could not be validated (state: %s)',
                picking.name, picking.state)

        return picking.state == 'done'


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
        'product.product', string='Producto')
    lot_id = fields.Many2one('stock.lot', string='Lote/Placa')
    qty_delivered = fields.Float(string='Entregado')
    qty_to_return = fields.Float(string='A Devolver')

    @api.model_create_multi
    def create(self, vals_list):
        """Ensure product_id and lot_id are filled from move/move_line."""
        for vals in vals_list:
            if not vals.get('product_id') and vals.get('move_id'):
                move = self.env['stock.move'].browse(vals['move_id'])
                vals['product_id'] = move.product_id.id
            if not vals.get('lot_id') and vals.get('move_line_id'):
                ml = self.env['stock.move.line'].browse(vals['move_line_id'])
                vals['lot_id'] = ml.lot_id.id if ml.lot_id else False
            if not vals.get('qty_delivered') and vals.get('move_line_id'):
                ml = self.env['stock.move.line'].browse(vals['move_line_id'])
                vals['qty_delivered'] = ml.quantity or ml.qty_done or 0.0
        return super().create(vals_list)

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