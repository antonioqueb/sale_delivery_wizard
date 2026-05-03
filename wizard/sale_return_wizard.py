from collections import OrderedDict
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

    widget_selections = fields.Text(
        string='Selecciones del Widget', default='[]')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so_id = res.get('sale_order_id') or self.env.context.get('active_id')
        if not so_id:
            return res

        order = self.env['sale.order'].browse(so_id)
        if not order.exists():
            return res

        res['sale_order_id'] = order.id
        res['widget_selections'] = '[]'

        raw_lines = self._prepare_return_lines_from_remissions(order)

        if not raw_lines:
            raw_lines = self._prepare_return_lines_fallback_from_pickings(order)

        res['line_ids'] = self._group_lines_by_remission_product(raw_lines)
        return res

    def _get_returned_qty_by_origin_remission_line(self, order):
        return order._get_returned_qty_by_origin_remission_line()

    def _get_returned_qty_by_source_move_lot(self, order):
        if hasattr(order, '_get_returned_qty_by_source_move_lot'):
            return order._get_returned_qty_by_source_move_lot()
        return {}

    def _prepare_return_lines_from_remissions(self, order):
        raw_lines = []
        returned_by_line = self._get_returned_qty_by_origin_remission_line(order)

        remissions = order.delivery_document_ids.filtered(
            lambda d: d.document_type == 'remission'
            and d.state == 'confirmed'
        ).sorted(lambda d: d.id)

        for remission in remissions:
            remission_name = remission.remission_number or remission.name or _('Sin Remisión')

            for doc_line in remission.line_ids.sorted(lambda l: (l.sequence, l.id)):
                qty_delivered = doc_line.qty_done or doc_line.qty_selected or 0.0
                qty_returned = returned_by_line.get(doc_line.id, 0.0)
                qty_available = max(qty_delivered - qty_returned, 0.0)

                if qty_available <= 0:
                    continue

                if not doc_line.product_id:
                    continue

                source = order._resolve_return_source_for_remission_line(
                    remission,
                    doc_line,
                )

                move = source.get('move')
                ml = source.get('move_line')

                if not move:
                    _logger.warning(
                        '[RETURN WIZARD] No se encontró movimiento OUT para devolver '
                        'remisión=%s línea=%s producto=%s lote=%s',
                        remission.name,
                        doc_line.id,
                        doc_line.product_id.display_name,
                        doc_line.lot_id.name if doc_line.lot_id else 'S/L',
                    )
                    continue

                raw_lines.append((0, 0, {
                    'move_id': move.id,
                    'move_line_id': ml.id if ml else False,
                    'sale_line_id': doc_line.sale_line_id.id if doc_line.sale_line_id else False,
                    'product_id': doc_line.product_id.id,
                    'lot_id': doc_line.lot_id.id if doc_line.lot_id else False,
                    'qty_delivered': qty_available,
                    'qty_to_return': qty_available,
                    'is_selected': True,
                    'origin_remission_id': remission.id,
                    'origin_remission_line_id': doc_line.id,
                    'origin_remission_number': remission_name,
                }))

        return raw_lines

    def _prepare_return_lines_fallback_from_pickings(self, order):
        raw_lines = []
        returned_by_source = self._get_returned_qty_by_source_move_lot(order)

        for picking in order.picking_ids.filtered(
                lambda p: p.state == 'done'
                and p.picking_type_code == 'outgoing'):
            fallback_ref = picking.name or _('Sin Remisión')

            for move in picking.move_ids.filtered(
                    lambda m: m.state == 'done'):
                for ml in move.move_line_ids:
                    qty = ml.quantity or getattr(ml, 'qty_done', 0.0) or 0.0
                    if qty <= 0:
                        continue

                    returned_qty = returned_by_source.get(
                        (move.id, ml.lot_id.id if ml.lot_id else 0),
                        0.0,
                    )
                    qty_available = max(qty - returned_qty, 0.0)

                    if qty_available <= 0:
                        continue

                    raw_lines.append((0, 0, {
                        'move_id': move.id,
                        'move_line_id': ml.id,
                        'sale_line_id': move.sale_line_id.id if move.sale_line_id else False,
                        'product_id': move.product_id.id,
                        'lot_id': ml.lot_id.id if ml.lot_id else False,
                        'qty_delivered': qty_available,
                        'qty_to_return': qty_available,
                        'is_selected': True,
                        'origin_remission_id': False,
                        'origin_remission_line_id': False,
                        'origin_remission_number': fallback_ref,
                    }))

        return raw_lines

    def _group_lines_by_remission_product(self, raw_lines):
        grouped = OrderedDict()

        for cmd in raw_lines:
            vals = cmd[2]
            remission_id = vals.get('origin_remission_id') or 0
            remission_number = vals.get('origin_remission_number') or _('Sin Remisión')
            pid = vals.get('product_id', 0)

            key = (remission_id, remission_number, pid)
            grouped.setdefault(key, []).append(cmd)

        result = []
        Product = self.env['product.product']
        seq = 0

        for (remission_id, remission_number, pid), lines in grouped.items():
            product = Product.browse(pid) if pid else Product
            product_name = product.display_name if product and product.exists() else _('Sin Producto')
            section_name = '%s · %s' % (remission_number, product_name)

            result.append((0, 0, {
                'display_type': 'line_section',
                'section_name': section_name,
                'product_id': pid or False,
                'origin_remission_id': remission_id or False,
                'origin_remission_number': remission_number,
                'sequence': seq,
            }))
            seq += 1

            for line_cmd in lines:
                line_cmd[2]['sequence'] = seq
                result.append(line_cmd)
                seq += 1

        return result

    def get_grouped_lines_data(self):
        self.ensure_one()
        groups = []
        current_group = None

        for line in self.line_ids.sorted(lambda l: (l.sequence, l.id)):
            if line.display_type == 'line_section':
                if current_group and current_group['lines']:
                    groups.append(current_group)

                remission_name = line.origin_remission_number or _('Sin Remisión')
                product_name = (
                    line.product_id.display_name
                    if line.product_id
                    else _('Sin Producto')
                )
                group_key = 'remission-%s-product-%s-section-%s' % (
                    line.origin_remission_id.id if line.origin_remission_id else 0,
                    line.product_id.id if line.product_id else 0,
                    line.id or line.sequence,
                )

                current_group = {
                    'groupKey': group_key,
                    'productId': line.product_id.id or 0,
                    'productName': line.section_name or '%s · %s' % (
                        remission_name, product_name),
                    'originRemissionId': line.origin_remission_id.id if line.origin_remission_id else 0,
                    'originRemissionName': remission_name,
                    'lines': [],
                    'totalQty': 0,
                    'selectedCount': 0,
                    'lineCount': 0,
                }
                continue

            if current_group is None:
                remission_name = line.origin_remission_number or _('Sin Remisión')
                pname = line.product_id.display_name if line.product_id else _('Sin Producto')
                group_key = 'remission-%s-product-%s-line-%s' % (
                    line.origin_remission_id.id if line.origin_remission_id else 0,
                    line.product_id.id if line.product_id else 0,
                    line.id or line.sequence,
                )
                current_group = {
                    'groupKey': group_key,
                    'productId': line.product_id.id if line.product_id else 0,
                    'productName': '%s · %s' % (remission_name, pname),
                    'originRemissionId': line.origin_remission_id.id if line.origin_remission_id else 0,
                    'originRemissionName': remission_name,
                    'lines': [],
                    'totalQty': 0,
                    'selectedCount': 0,
                    'lineCount': 0,
                }

            remission_name = line.origin_remission_number or (
                current_group.get('originRemissionName') or _('Sin Remisión')
            )

            ld = {
                'groupKey': current_group.get('groupKey'),
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
                'originRemissionId': line.origin_remission_id.id if line.origin_remission_id else 0,
                'originRemissionName': remission_name,
                'originRemissionLineId': line.origin_remission_line_id.id if line.origin_remission_line_id else 0,
            }

            current_group['lines'].append(ld)
            current_group['lineCount'] += 1
            current_group['totalQty'] += line.qty_to_return or 0
            if line.is_selected:
                current_group['selectedCount'] += 1

        if current_group and current_group['lines']:
            groups.append(current_group)

        return groups

    def action_confirm_return(self):
        self.ensure_one()

        try:
            sels = json.loads(self.widget_selections or '[]')
        except (json.JSONDecodeError, TypeError, ValueError):
            sels = []

        if sels:
            return self._confirm_return_from_selections(sels)

        return self._confirm_return_from_lines()

    def _build_return_payloads_from_selections(self, order, sels):
        payloads = []

        for sel in sels:
            qty = float(sel.get('qty', 0) or 0.0)

            if qty <= 0:
                continue

            qty_available = float(
                sel.get('qtyAvailable')
                or sel.get('qtyDelivered')
                or 0.0
            )
            if qty_available > 0 and qty > qty_available:
                raise UserError(_(
                    'La cantidad a devolver %.2f excede la cantidad disponible %.2f.'
                ) % (qty, qty_available))

            move_id = sel.get('moveId', 0)
            move = self.env['stock.move'].browse(move_id) if move_id else self.env['stock.move']

            origin_line_id = sel.get('originRemissionLineId') or False
            origin_line = (
                self.env['sale.delivery.document.line'].browse(origin_line_id)
                if origin_line_id
                else self.env['sale.delivery.document.line']
            )

            if (
                not move
                or not move.exists()
                or move.state != 'done'
                or (
                    move.location_dest_id.usage != 'customer'
                    and move.picking_id.picking_type_code != 'outgoing'
                )
            ):
                if origin_line and origin_line.exists():
                    remission = origin_line.origin_remission_id
                    source = order._resolve_return_source_for_remission_line(
                        remission,
                        origin_line,
                    )
                    move = source.get('move')

            if not move or not move.exists():
                raise UserError(_(
                    'No se encontró el movimiento OUT original para devolver el producto ID %s.'
                ) % (sel.get('productId') or 'N/A'))

            if move.state != 'done':
                raise UserError(_(
                    'No se puede devolver %s porque el movimiento original %s no está validado.'
                ) % (move.product_id.display_name, move.display_name))

            if (
                move.location_dest_id.usage != 'customer'
                and move.picking_id.picking_type_code != 'outgoing'
            ):
                raise UserError(_(
                    'No se puede devolver %s porque el movimiento original no corresponde '
                    'a una salida hacia cliente. Movimiento: %s.'
                ) % (move.product_id.display_name, move.display_name))

            payloads.append({
                'move': move,
                'product_id': sel.get('productId') or move.product_id.id,
                'lot_id': sel.get('lotId') or False,
                'qty': qty,
                'sale_line_id': sel.get('saleLineId') or (
                    move.sale_line_id.id if move.sale_line_id else False
                ),
                'move_line_id': sel.get('moveLineId') or False,
                'origin_remission_id': sel.get('originRemissionId') or False,
                'origin_remission_line_id': origin_line_id or False,
                'origin_remission_number': sel.get('originRemissionName') or '',
            })

        return payloads

    def _create_native_return_picking(self, source_picking, payloads):
        if not source_picking or source_picking.state != 'done':
            raise UserError(_(
                'El picking origen %s no está validado; no se puede devolver.'
            ) % (source_picking.name if source_picking else 'N/A'))

        ReturnLine = self.env['stock.return.picking.line']

        return_wiz = self.env['stock.return.picking'].with_context(
            active_id=source_picking.id,
            active_ids=source_picking.ids,
            active_model='stock.picking',
        ).create({})

        return_wiz.product_return_moves.unlink()

        for payload in payloads:
            move = payload['move']

            vals = {
                'wizard_id': return_wiz.id,
                'product_id': payload['product_id'],
                'quantity': payload['qty'],
                'move_id': move.id,
                'uom_id': move.product_uom.id,
            }

            if 'to_refund' in ReturnLine._fields:
                vals['to_refund'] = True

            ReturnLine.create(vals)

        result = return_wiz.action_create_returns()

        ret_picking_id = result.get('res_id') if isinstance(result, dict) else False

        if ret_picking_id:
            return self.env['stock.picking'].browse(ret_picking_id)

        ret_picking = self.env['stock.picking'].search([
            ('return_id', '=', source_picking.id),
            ('state', '!=', 'cancel'),
        ], order='id desc', limit=1)

        if not ret_picking:
            raise UserError(_(
                'Odoo creó la devolución, pero no se pudo identificar el picking de retorno.'
            ))

        return ret_picking

    def _create_return_document_from_payloads(self, ret_picking, payloads):
        doc = self.env['sale.delivery.document'].create({
            'document_type': 'return',
            'sale_order_id': self.sale_order_id.id,
            'return_picking_id': ret_picking.id,
            'return_reason_id': self.return_reason_id.id,
            'return_action': self.return_action,
            'special_instructions': self.notes or '',
            'line_ids': [(0, 0, {
                'product_id': p['product_id'],
                'lot_id': p['lot_id'],
                'qty_selected': p['qty'],
                'qty_done': p['qty'],
                'qty_returned': p['qty'],
                'sale_line_id': p['sale_line_id'],
                'move_id': p['move'].id,
                'move_line_id': p['move_line_id'],
                'origin_remission_id': p.get('origin_remission_id') or False,
                'origin_remission_line_id': p.get('origin_remission_line_id') or False,
            }) for p in payloads],
        })

        return doc

    def _confirm_return_from_selections(self, sels):
        order = self.sale_order_id

        payloads = self._build_return_payloads_from_selections(order, sels)

        if not payloads:
            raise UserError(_('Seleccione al menos una línea para devolver.'))

        payloads_by_picking = {}
        for payload in payloads:
            picking = payload['move'].picking_id
            bucket = payloads_by_picking.setdefault(
                picking.id,
                {
                    'picking': picking,
                    'payloads': [],
                }
            )
            bucket['payloads'].append(payload)

        docs = self.env['sale.delivery.document']

        for bucket in payloads_by_picking.values():
            source_picking = bucket['picking']
            picking_payloads = bucket['payloads']

            ret_picking = self._create_native_return_picking(
                source_picking,
                picking_payloads,
            )

            doc = self._create_return_document_from_payloads(
                ret_picking,
                picking_payloads,
            )

            doc.action_confirm()
            docs |= doc

        if self.return_action == 'reagendar':
            self._action_reagendar_from_payloads(order, payloads)

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
                        'Se validó la devolución física y se creó una reentrega pendiente.'
                    ),
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
                    '%d devolución(es) validada(s) con acción: %s.'
                ) % (len(docs), action_label),
                'type': 'success',
                'sticky': False,
            },
        }

    def _confirm_return_from_lines(self):
        selected = self.line_ids.filtered(
            lambda l: l.is_selected and l.display_type != 'line_section'
        )

        if not selected:
            raise UserError(_('Seleccione al menos una línea para devolver.'))

        sels = []

        for line in selected:
            if line.qty_to_return <= 0:
                raise UserError(_(
                    'La cantidad a devolver debe ser mayor a 0 para %s.'
                ) % line.product_id.display_name)

            sels.append({
                'moveId': line.move_id.id if line.move_id else 0,
                'moveLineId': line.move_line_id.id if line.move_line_id else 0,
                'saleLineId': line.sale_line_id.id if line.sale_line_id else 0,
                'productId': line.product_id.id if line.product_id else 0,
                'lotId': line.lot_id.id if line.lot_id else 0,
                'qty': line.qty_to_return,
                'qtyDelivered': line.qty_delivered,
                'originRemissionId': line.origin_remission_id.id if line.origin_remission_id else 0,
                'originRemissionLineId': line.origin_remission_line_id.id if line.origin_remission_line_id else 0,
                'originRemissionName': line.origin_remission_number or '',
            })

        return self._confirm_return_from_selections(sels)

    def _resolve_source_location(self, lot_id, product_id, parent_location_id):
        Quant = self.env['stock.quant']
        quant = Quant.search([
            ('lot_id', '=', lot_id),
            ('product_id', '=', product_id),
            ('location_id', 'child_of', parent_location_id),
            ('quantity', '>', 0),
        ], limit=1)
        if not quant:
            quant = Quant.search([
                ('lot_id', '=', lot_id),
                ('product_id', '=', product_id),
                ('location_id.usage', '=', 'internal'),
                ('quantity', '>', 0),
            ], limit=1)
        return quant.location_id.id if quant else parent_location_id

    def _action_reagendar_from_payloads(self, order, payloads):
        sels = []

        for payload in payloads:
            sels.append({
                'productId': payload['product_id'],
                'lotId': payload['lot_id'],
                'qty': payload['qty'],
                'saleLineId': payload['sale_line_id'],
                'moveId': payload['move'].id,
                'originRemissionId': payload.get('origin_remission_id') or False,
                'originRemissionLineId': payload.get('origin_remission_line_id') or False,
            })

        return self._action_reagendar_from_sels(order, sels)

    def _action_reagendar_from_sels(self, order, sels):
        warehouse = order.warehouse_id
        pick_type = warehouse.out_type_id
        if not pick_type:
            pick_type = self.env['stock.picking.type'].search([
                ('code', '=', 'outgoing'),
                ('warehouse_id', '=', warehouse.id),
            ], limit=1)
        if not pick_type:
            raise UserError(_(
                'No se encontró tipo de picking de salida para el almacén %s.'
            ) % warehouse.name)

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
            product = self.env['product.product'].browse(sel['productId'])
            uom_id = move.product_uom.id if move else product.uom_id.id
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
                    source_loc_id = self._resolve_source_location(
                        s['lotId'], product_id, new_picking.location_id.id
                    )
                    self.env['stock.move.line'].create({
                        'move_id': move.id,
                        'product_id': product_id,
                        'lot_id': s['lotId'],
                        'quantity': s['qty'],
                        'location_id': source_loc_id,
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
                'origin_remission_id': sel.get('originRemissionId') or False,
                'origin_remission_line_id': sel.get('originRemissionLineId') or False,
            }))

        doc = self.env['sale.delivery.document'].create({
            'document_type': 'redelivery',
            'sale_order_id': order.id,
            'picking_id': new_picking.id,
            'delivery_address': order.partner_shipping_id.contact_address or '',
            'special_instructions': _('REENTREGA por devolución.'),
            'line_ids': doc_lines,
        })
        doc.action_prepare()
        return doc


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

    origin_remission_id = fields.Many2one(
        'sale.delivery.document',
        string='Remisión Origen',
        readonly=True,
        domain=[('document_type', '=', 'remission')],
    )
    origin_remission_line_id = fields.Many2one(
        'sale.delivery.document.line',
        string='Línea Remisión Origen',
        readonly=True,
    )
    origin_remission_number = fields.Char(
        string='Folio Remisión Origen',
        readonly=True,
    )

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
                vals['qty_delivered'] = ml.quantity or getattr(ml, 'qty_done', 0.0) or 0.0
        return super().create(vals_list)

    @api.onchange('is_selected')
    def _onchange_is_selected(self):
        if self.display_type == 'line_section':
            return
        if self.is_selected:
            if self.qty_delivered <= 0 and self.move_line_id:
                self.qty_delivered = (
                    self.move_line_id.quantity or getattr(self.move_line_id, 'qty_done', 0.0) or 0.0)
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
                self.move_line_id.quantity or getattr(self.move_line_id, 'qty_done', 0.0) or 0.0)
        if self.qty_to_return > self.qty_delivered and self.qty_delivered > 0:
            return {'warning': {
                'title': _('Cantidad excedida'),
                'message': _(
                    'La cantidad a devolver excede lo entregado (%s).'
                ) % self.qty_delivered,
            }}