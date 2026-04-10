from odoo import api, fields, models, _
from odoo.exceptions import UserError
import json
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

    # ── JSON field written by JS widget ──
    widget_selections = fields.Text(
        string='Selecciones del Widget', default='[]')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so_id = res.get('sale_order_id') or self.env.context.get('active_id')
        if not so_id:
            return res
        order = self.env['sale.order'].browse(so_id)
        res['sale_order_id'] = order.id

        raw_lines = []
        for picking in order.picking_ids.filtered(
                lambda p: p.state == 'done'
                and p.picking_type_code == 'outgoing'):
            for move in picking.move_ids.filtered(
                    lambda m: m.state == 'done'):
                for ml in move.move_line_ids:
                    qty = ml.quantity or ml.qty_done or 0.0
                    if qty > 0:
                        raw_lines.append((0, 0, {
                            'move_id': move.id,
                            'move_line_id': ml.id,
                            'sale_line_id': move.sale_line_id.id,
                            'product_id': move.product_id.id,
                            'lot_id': ml.lot_id.id if ml.lot_id else False,
                            'qty_delivered': qty,
                            'qty_to_return': qty,
                            'is_selected': True,
                        }))
        res['line_ids'] = self._group_lines_by_product(raw_lines)
        return res

    def _group_lines_by_product(self, raw_lines):
        from collections import OrderedDict
        grouped = OrderedDict()
        for cmd in raw_lines:
            vals = cmd[2]
            pid = vals.get('product_id', 0)
            grouped.setdefault(pid, []).append(cmd)

        result = []
        Product = self.env['product.product']
        seq = 0
        for pid, lines in grouped.items():
            product = Product.browse(pid) if pid else None
            section_name = product.display_name if product else _('Sin Producto')
            result.append((0, 0, {
                'display_type': 'line_section',
                'section_name': section_name,
                'product_id': pid,
                'sequence': seq,
            }))
            seq += 1
            for line_cmd in lines:
                line_cmd[2]['sequence'] = seq
                result.append(line_cmd)
                seq += 1
        return result

    # ─── RPC for grouped list widget ─────────────────────────────────
    def get_grouped_lines_data(self):
        self.ensure_one()
        groups = []
        current_group = None

        for line in self.line_ids.sorted(lambda l: (l.sequence, l.id)):
            if line.display_type == 'line_section':
                if current_group and current_group['lines']:
                    groups.append(current_group)
                current_group = {
                    'productId': line.product_id.id or 0,
                    'productName': line.section_name or (
                        line.product_id.display_name if line.product_id else 'Sin Producto'),
                    'lines': [],
                    'totalQty': 0,
                    'selectedCount': 0,
                    'lineCount': 0,
                }
                continue

            if current_group is None:
                pname = line.product_id.display_name if line.product_id else 'Sin Producto'
                current_group = {
                    'productId': line.product_id.id or 0,
                    'productName': pname,
                    'lines': [],
                    'totalQty': 0,
                    'selectedCount': 0,
                    'lineCount': 0,
                }

            ld = {
                'dbId': line.id,
                'lotId': line.lot_id.id if line.lot_id else 0,
                'lotName': line.lot_id.name if line.lot_id else '',
                'productId': line.product_id.id if line.product_id else 0,
                'productName': line.product_id.display_name if line.product_id else '',
                'isSelected': line.is_selected,
                'qtyDelivered': line.qty_delivered,
                'qtyToReturn': line.qty_to_return,
                'moveId': line.move_id.id if line.move_id else 0,
                'moveLineId': line.move_line_id.id if line.move_line_id else 0,
                'saleLineId': line.sale_line_id.id if line.sale_line_id else 0,
                'pickingId': line.move_id.picking_id.id if line.move_id and line.move_id.picking_id else 0,
                'sourceLocationId': 0,
            }
            current_group['lines'].append(ld)
            current_group['lineCount'] += 1
            current_group['totalQty'] += line.qty_to_return or 0
            if line.is_selected:
                current_group['selectedCount'] += 1

        if current_group and current_group['lines']:
            groups.append(current_group)

        return groups

    # ─── Action ──────────────────────────────────────────────────────

    def action_confirm_return(self):
        self.ensure_one()

        # Try widget_selections first
        try:
            sels = json.loads(self.widget_selections or '[]')
        except (json.JSONDecodeError, TypeError):
            sels = []

        if sels:
            return self._confirm_return_from_selections(sels)
        else:
            return self._confirm_return_from_lines()

    def _confirm_return_from_selections(self, sels):
        """Process return using widget_selections JSON."""
        order = self.sale_order_id

        # Build a structure similar to what _confirm_return_from_lines expects
        # Group by original picking
        move_returns = {}
        for sel in sels:
            if sel.get('qty', 0) <= 0:
                continue
            move_id = sel.get('moveId', 0)
            if not move_id:
                continue
            move = self.env['stock.move'].browse(move_id)
            if not move.exists():
                continue
            picking = move.picking_id
            move_returns.setdefault(picking, []).append({
                'move': move,
                'product_id': sel.get('productId'),
                'lot_id': sel.get('lotId') or False,
                'qty': sel.get('qty', 0),
                'sale_line_id': sel.get('saleLineId') or False,
                'move_line_id': sel.get('moveLineId') or False,
            })

        if not move_returns:
            raise UserError(_(
                'Seleccione al menos una línea para devolver.'))

        return_pickings = self.env['stock.picking']
        for picking, sel_lines in move_returns.items():
            return_wiz = self.env['stock.return.picking'].with_context(
                active_id=picking.id,
                active_model='stock.picking',
            ).create({})
            return_wiz.product_return_moves.unlink()
            for sl in sel_lines:
                self.env['stock.return.picking.line'].create({
                    'wizard_id': return_wiz.id,
                    'product_id': sl['product_id'],
                    'quantity': sl['qty'],
                    'move_id': sl['move'].id,
                    'uom_id': sl['move'].product_uom.id,
                })
            result = return_wiz.action_create_returns()
            if result and result.get('res_id'):
                ret_picking = self.env['stock.picking'].browse(result['res_id'])
                return_pickings |= ret_picking

        docs = self.env['sale.delivery.document']
        for ret_picking in return_pickings:
            orig_picking = ret_picking.move_ids.mapped(
                'origin_returned_move_id.picking_id')
            picking_sels = []
            for picking, sl_list in move_returns.items():
                if picking in orig_picking:
                    picking_sels.extend(sl_list)
            if not picking_sels:
                # Fallback: use all selections
                for sl_list in move_returns.values():
                    picking_sels.extend(sl_list)

            doc = self.env['sale.delivery.document'].create({
                'document_type': 'return',
                'sale_order_id': order.id,
                'return_picking_id': ret_picking.id,
                'return_reason_id': self.return_reason_id.id,
                'return_action': self.return_action,
                'special_instructions': self.notes or '',
                'line_ids': [(0, 0, {
                    'product_id': sl['product_id'],
                    'lot_id': sl['lot_id'],
                    'qty_selected': sl['qty'],
                    'qty_done': sl['qty'],
                    'sale_line_id': sl['sale_line_id'],
                    'move_id': sl['move'].id,
                    'move_line_id': sl['move_line_id'],
                }) for sl in picking_sels],
            })

            self._validate_return_picking_from_sels(ret_picking, picking_sels)
            doc.state = 'confirmed'
            doc.delivery_date = fields.Datetime.now()
            docs |= doc

        if self.return_action == 'reagendar':
            self._action_reagendar_from_sels(order, sels)

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
                        'pendiente con el mismo material.'),
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

    def _validate_return_picking_from_sels(self, picking, sel_lines):
        """Validate return picking using selection dicts."""
        if picking.state in ('draft', 'confirmed', 'waiting'):
            picking.action_assign()

        lot_qty = {}
        product_qty = {}
        for sl in sel_lines:
            if sl.get('lot_id'):
                lot_qty[sl['lot_id']] = lot_qty.get(sl['lot_id'], 0) + sl['qty']
            else:
                pid = sl['product_id']
                product_qty[pid] = product_qty.get(pid, 0) + sl['qty']

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
                qty = sum(sl['qty'] for sl in sel_lines
                          if sl['product_id'] == move.product_id.id)
                if qty > 0:
                    lot_id = next(
                        (sl['lot_id'] for sl in sel_lines
                         if sl['product_id'] == move.product_id.id and sl.get('lot_id')),
                        False)
                    self.env['stock.move.line'].create({
                        'move_id': move.id,
                        'picking_id': picking.id,
                        'product_id': move.product_id.id,
                        'lot_id': lot_id,
                        'quantity': qty,
                        'location_id': move.location_id.id,
                        'location_dest_id': move.location_dest_id.id,
                    })

        result = picking.with_context(skip_backorder=False).button_validate()
        if isinstance(result, dict):
            if result.get('res_model') == 'stock.backorder.confirmation':
                backorder_wiz = self.env['stock.backorder.confirmation'].with_context(
                    button_validate_picking_ids=picking.ids,
                ).create({'pick_ids': [(4, picking.id)]})
                backorder_wiz.process()
            elif result.get('res_model') == 'stock.immediate.transfer':
                immediate_wiz = self.env['stock.immediate.transfer'].with_context(
                    button_validate_picking_ids=picking.ids,
                ).create({'pick_ids': [(4, picking.id)]})
                immediate_wiz.process()

    def _action_reagendar_from_sels(self, order, sels):
        """Create redelivery picking from selections."""
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

        grouped = {}
        for sel in sels:
            if sel.get('qty', 0) <= 0:
                continue
            move_id = sel.get('moveId', 0)
            move = self.env['stock.move'].browse(move_id) if move_id else False
            uom_id = move.product_uom.id if move else self.env['product.product'].browse(
                sel['productId']).uom_id.id
            key = (sel['productId'], sel.get('saleLineId', 0), uom_id)
            grouped.setdefault(key, []).append(sel)

        move_map = {}
        for (product_id, sale_line_id, uom_id), sel_group in grouped.items():
            total_qty = sum(s['qty'] for s in sel_group)
            move = self.env['stock.move'].create({
                'product_id': product_id,
                'product_uom_qty': total_qty,
                'product_uom': uom_id,
                'picking_id': new_picking.id,
                'location_id': new_picking.location_id.id,
                'location_dest_id': new_picking.location_dest_id.id,
                'sale_line_id': sale_line_id or False,
                'origin': order.name,
            })
            move_map[sale_line_id] = move
            for s in sel_group:
                if s.get('lotId'):
                    self.env['stock.move.line'].create({
                        'move_id': move.id,
                        'product_id': product_id,
                        'lot_id': s['lotId'],
                        'quantity': s['qty'],
                        'location_id': new_picking.location_id.id,
                        'location_dest_id': new_picking.location_dest_id.id,
                        'picking_id': new_picking.id,
                    })

        new_picking.action_confirm()
        new_picking.action_assign()

        doc_lines = []
        for sel in sels:
            if sel.get('qty', 0) <= 0:
                continue
            move = move_map.get(sel.get('saleLineId', 0))
            doc_lines.append((0, 0, {
                'product_id': sel['productId'],
                'lot_id': sel.get('lotId') or False,
                'qty_selected': sel['qty'],
                'sale_line_id': sel.get('saleLineId') or False,
                'move_id': move.id if move else False,
                'source_location_id': new_picking.location_id.id,
            }))

        doc = self.env['sale.delivery.document'].create({
            'document_type': 'redelivery',
            'sale_order_id': order.id,
            'picking_id': new_picking.id,
            'delivery_address': order.partner_shipping_id.contact_address or '',
            'special_instructions': _(
                'REENTREGA por devolución.'),
            'line_ids': doc_lines,
        })
        doc.action_prepare()
        return doc

    # ═══════════════════════════════════════════════════════════════════
    # FALLBACK: Original methods using line_ids (unchanged)
    # ═══════════════════════════════════════════════════════════════════

    def _confirm_return_from_lines(self):
        """Original return logic using line_ids."""
        selected = self.line_ids.filtered(
            lambda l: l.is_selected and l.display_type != 'line_section')
        if not selected:
            raise UserError(_(
                'Seleccione al menos una línea para devolver.'))

        order = self.sale_order_id
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
                ret_picking = self.env['stock.picking'].browse(result['res_id'])
                return_pickings |= ret_picking

        docs = self.env['sale.delivery.document']
        for ret_picking in return_pickings:
            orig_picking = ret_picking.move_ids.mapped(
                'origin_returned_move_id.picking_id')
            picking_lines = [
                l for l in selected if l.move_id.picking_id in orig_picking]
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

            self._validate_return_picking(ret_picking, picking_lines)
            doc.state = 'confirmed'
            doc.delivery_date = fields.Datetime.now()
            docs |= doc

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

    def _action_reagendar(self, order, wizard_lines):
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

        grouped = {}
        for wl in wizard_lines:
            key = (wl.product_id.id, wl.sale_line_id.id, wl.move_id.product_uom.id)
            grouped.setdefault(key, []).append(wl)

        move_map = {}
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

        new_picking.action_confirm()
        new_picking.action_assign()

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
                    ) for wl in wizard_lines)),
            'line_ids': doc_lines,
        })
        doc.action_prepare()
        return doc

    def _validate_return_picking(self, picking, wizard_lines):
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
                    product_qty.get(wl.product_id.id, 0.0) + wl.qty_to_return)

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

        result = picking.with_context(skip_backorder=False).button_validate()
        if isinstance(result, dict):
            if result.get('res_model') == 'stock.backorder.confirmation':
                backorder_wiz = self.env['stock.backorder.confirmation'].with_context(
                    button_validate_picking_ids=picking.ids,
                ).create({'pick_ids': [(4, picking.id)]})
                backorder_wiz.process()
            elif result.get('res_model') == 'stock.immediate.transfer':
                immediate_wiz = self.env['stock.immediate.transfer'].with_context(
                    button_validate_picking_ids=picking.ids,
                ).create({'pick_ids': [(4, picking.id)]})
                immediate_wiz.process()

        if picking.state != 'done':
            _logger.warning(
                'Return picking %s could not be validated (state: %s)',
                picking.name, picking.state)
        return picking.state == 'done'


class SaleReturnWizardLine(models.TransientModel):
    _name = 'sale.return.wizard.line'
    _description = 'Línea de Wizard de Devolución'
    _order = 'sequence, id'

    wizard_id = fields.Many2one(
        'sale.return.wizard', ondelete='cascade', required=True)
    sequence = fields.Integer(default=10)
    display_type = fields.Selection([
        ('line_section', 'Section'),
    ], string='Tipo de Fila')
    section_name = fields.Char(string='Nombre de Sección')

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
        for vals in vals_list:
            if vals.get('display_type') == 'line_section':
                continue
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

    @api.onchange('is_selected')
    def _onchange_is_selected(self):
        if self.display_type == 'line_section':
            return
        if self.is_selected:
            if self.qty_delivered <= 0 and self.move_line_id:
                self.qty_delivered = (
                    self.move_line_id.quantity or self.move_line_id.qty_done or 0.0)
            if self.qty_to_return <= 0:
                self.qty_to_return = self.qty_delivered
        else:
            self.qty_to_return = 0.0

    @api.onchange('qty_to_return')
    def _onchange_qty_to_return(self):
        if self.display_type == 'line_section':
            return
        if self.qty_to_return > 0:
            self.is_selected = True
        if self.qty_delivered <= 0 and self.move_line_id:
            self.qty_delivered = (
                self.move_line_id.quantity or self.move_line_id.qty_done or 0.0)
        if self.qty_to_return > self.qty_delivered and self.qty_delivered > 0:
            return {'warning': {
                'title': _('Cantidad excedida'),
                'message': _(
                    'La cantidad a devolver excede lo entregado (%s).',
                    self.qty_delivered),
            }}