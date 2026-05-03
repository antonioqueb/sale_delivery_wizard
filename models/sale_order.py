from collections import OrderedDict
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    delivery_document_ids = fields.One2many(
        'sale.delivery.document',
        'sale_order_id',
        string='Documentos de Entrega',
    )

    x_total_assigned_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Total Asignado',
    )
    x_total_delivered_gross_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Entregado Bruto',
    )
    x_total_returned_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Devuelto',
    )
    x_total_delivered_net_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Entregado Neto',
    )
    x_total_pending_delivery_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Pendiente Entrega',
    )
    x_total_demand_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Demanda Total',
    )
    x_total_current_demand_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Demanda Actual',
    )
    x_total_overdelivered_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Sobreentregado',
    )
    x_fulfillment_net_pct = fields.Float(
        compute='_compute_delivery_summary',
        string='Fulfillment Neto %',
    )

    x_delivery_document_count = fields.Integer(
        compute='_compute_document_counts',
        string='Documentos',
    )
    x_remission_count = fields.Integer(
        compute='_compute_document_counts',
        string='Remisiones',
    )
    x_return_count = fields.Integer(
        compute='_compute_document_counts',
        string='Devoluciones',
    )
    x_pick_ticket_count = fields.Integer(
        compute='_compute_document_counts',
        string='Pick Tickets',
    )
    x_pick_ticket_open_count = fields.Integer(
        compute='_compute_document_counts',
        string='Pick Tickets Abiertos',
    )
    x_redelivery_count = fields.Integer(
        compute='_compute_document_counts',
        string='Reentregas',
    )
    x_redelivery_pending_count = fields.Integer(
        compute='_compute_document_counts',
        string='Reentregas Pendientes',
    )

    def _ensure_origin_demand_snapshot(self, source='manual'):
        for order in self:
            lines = order.order_line.filtered(
                lambda l: l.product_id and l.product_id.type != 'service'
            )
            lines._ensure_origin_demand_snapshot(source=source)
        return True

    @api.depends(
        'order_line.product_uom_qty',
        'order_line.qty_delivered',
        'order_line.x_returned_qty',
        'order_line.x_delivered_net_qty',
        'order_line.x_origin_demand_qty',
        'order_line.x_origin_demand_locked',
        'order_line.x_overdelivered_origin_qty',
        'delivery_document_ids.state',
        'delivery_document_ids.document_type',
        'delivery_document_ids.return_picking_id.state',
        'delivery_document_ids.line_ids.qty_selected',
        'delivery_document_ids.line_ids.qty_done',
        'delivery_document_ids.line_ids.qty_returned',
    )
    def _compute_delivery_summary(self):
        for order in self:
            lines = order.order_line.filtered(
                lambda l: l.product_id and l.product_id.type != 'service'
            )

            demand = sum(
                line.x_origin_demand_qty
                if line.x_origin_demand_locked and line.x_origin_demand_qty > 0
                else line.product_uom_qty
                for line in lines
            )

            current_demand = sum(lines.mapped('product_uom_qty'))
            assigned = current_demand
            delivered_net = sum(lines.mapped('x_delivered_net_qty'))
            returned = sum(lines.mapped('x_returned_qty'))
            pending = demand - delivered_net
            overdelivered = delivered_net - demand

            order.x_total_demand_qty = demand
            order.x_total_current_demand_qty = current_demand
            order.x_total_assigned_qty = assigned
            order.x_total_delivered_gross_qty = delivered_net + returned
            order.x_total_returned_qty = returned
            order.x_total_delivered_net_qty = max(delivered_net, 0.0)
            order.x_total_pending_delivery_qty = max(pending, 0.0)
            order.x_total_overdelivered_qty = max(overdelivered, 0.0)
            order.x_fulfillment_net_pct = (
                (delivered_net / demand * 100.0) if demand else 0.0
            )

    @api.depends(
        'delivery_document_ids',
        'delivery_document_ids.document_type',
        'delivery_document_ids.state',
    )
    def _compute_document_counts(self):
        for order in self:
            docs = order.delivery_document_ids
            order.x_delivery_document_count = len(docs)
            order.x_remission_count = len(
                docs.filtered(lambda d: d.document_type == 'remission')
            )
            order.x_return_count = len(
                docs.filtered(lambda d: d.document_type == 'return')
            )
            order.x_pick_ticket_count = len(
                docs.filtered(lambda d: d.document_type == 'pick_ticket')
            )
            order.x_pick_ticket_open_count = len(
                docs.filtered(
                    lambda d: d.document_type == 'pick_ticket'
                    and d.state == 'prepared'
                )
            )
            order.x_redelivery_count = len(
                docs.filtered(lambda d: d.document_type == 'redelivery')
            )
            order.x_redelivery_pending_count = len(
                docs.filtered(
                    lambda d: d.document_type == 'redelivery'
                    and d.state in ('draft', 'prepared')
                )
            )

    def _ml_done_qty(self, ml):
        return (
            ml.quantity
            or getattr(ml, 'qty_done', 0.0)
            or 0.0
        )

    def _resolve_return_source_for_remission_line(self, remission, doc_line):
        """
        Resuelve el movimiento REAL que debe devolverse.

        Para que Odoo reduzca el entregado/fulfillment, la devolución debe
        hacerse contra el stock.move DONE que salió hacia cliente, no contra
        un movimiento interno previo.
        """
        self.ensure_one()

        StockMove = self.env['stock.move']
        StockMoveLine = self.env['stock.move.line']

        product = doc_line.product_id
        lot = doc_line.lot_id
        sale_line = doc_line.sale_line_id
        original_move = doc_line.move_id

        if not product:
            return {
                'move': StockMove.browse(),
                'move_line': StockMoveLine.browse(),
                'picking': self.env['stock.picking'].browse(),
            }

        candidates = StockMove.browse()

        if original_move and original_move.state == 'done':
            if (
                original_move.location_dest_id.usage == 'customer'
                or original_move.picking_id.picking_type_code == 'outgoing'
            ):
                candidates |= original_move

            candidates |= original_move.move_dest_ids.filtered(
                lambda m: m.state == 'done'
                and m.product_id == product
                and (
                    m.location_dest_id.usage == 'customer'
                    or m.picking_id.picking_type_code == 'outgoing'
                )
            )

        candidate_pickings = self.env['stock.picking']

        if remission.out_picking_id:
            candidate_pickings |= remission.out_picking_id

        if remission.picking_id and remission.picking_id.picking_type_code == 'outgoing':
            candidate_pickings |= remission.picking_id

        for picking in candidate_pickings.filtered(lambda p: p.state == 'done'):
            candidates |= picking.move_ids.filtered(
                lambda m: m.state == 'done'
                and m.product_id == product
                and (
                    m.location_dest_id.usage == 'customer'
                    or picking.picking_type_code == 'outgoing'
                )
            )

        if not candidates and sale_line:
            candidates |= StockMove.search([
                ('sale_line_id', '=', sale_line.id),
                ('product_id', '=', product.id),
                ('state', '=', 'done'),
                ('location_dest_id.usage', '=', 'customer'),
            ])

        candidates = candidates.filtered(lambda m: m.product_id == product)

        if sale_line:
            strict = candidates.filtered(lambda m: m.sale_line_id == sale_line)
            if strict:
                candidates = strict

        for move in candidates.sorted(lambda m: (m.picking_id.id or 0, m.id)):
            move_lines = move.move_line_ids.filtered(
                lambda ml: ml.product_id == product
                and self._ml_done_qty(ml) > 0
                and (not lot or ml.lot_id == lot)
            )
            if move_lines:
                ml = move_lines.sorted(lambda l: l.id)[0]
                return {
                    'move': move,
                    'move_line': ml,
                    'picking': move.picking_id,
                }

        if candidates:
            move = candidates.sorted(lambda m: (m.picking_id.id or 0, m.id))[0]
            return {
                'move': move,
                'move_line': move.move_line_ids[:1],
                'picking': move.picking_id,
            }

        return {
            'move': StockMove.browse(),
            'move_line': StockMoveLine.browse(),
            'picking': self.env['stock.picking'].browse(),
        }

    def _get_locked_lot_ids(self, exclude_pt_id=None):
        self.ensure_one()
        domain = [
            ('sale_order_id', '=', self.id),
            ('document_type', '=', 'pick_ticket'),
            ('state', '=', 'prepared'),
        ]
        if exclude_pt_id:
            domain.append(('id', '!=', exclude_pt_id))
        pts = self.env['sale.delivery.document'].search(domain)
        return set(pts.mapped('line_ids.lot_id').ids)

    def _get_lot_to_pt_map(self, exclude_pt_id=None):
        self.ensure_one()
        domain = [
            ('sale_order_id', '=', self.id),
            ('document_type', '=', 'pick_ticket'),
            ('state', '=', 'prepared'),
        ]
        if exclude_pt_id:
            domain.append(('id', '!=', exclude_pt_id))
        pts = self.env['sale.delivery.document'].search(domain)
        result = {}
        for pt in pts:
            for pl in pt.line_ids:
                if pl.lot_id:
                    result.setdefault(pl.lot_id.id, []).append(pt.name)
        return result

    def get_delivery_grouped_data(self, mode='delivery', editing_pt_id=None):
        self.ensure_one()

        if mode == 'delivery':
            groups = self._build_delivery_groups(editing_pt_id=editing_pt_id)
            return self._apply_pick_ticket_selection(
                groups, editing_pt_id=editing_pt_id)
        if mode == 'return':
            return self._build_return_groups()
        if mode == 'swap':
            return self._build_swap_groups()
        return []

    def _apply_pick_ticket_selection(self, groups, editing_pt_id=None):
        self.ensure_one()
        if not editing_pt_id:
            return groups

        pt = self.env['sale.delivery.document'].browse(editing_pt_id)
        if not pt.exists() or not pt.line_ids:
            return groups

        pt_keys = {}
        for pt_line in pt.line_ids:
            key = (
                pt_line.product_id.id,
                pt_line.lot_id.id if pt_line.lot_id else 0,
            )
            pt_keys[key] = pt_keys.get(key, 0.0) + pt_line.qty_selected

        for group in groups:
            for line in group.get('lines', []):
                key = (
                    line.get('productId', 0),
                    line.get('lotId', 0),
                )
                if key in pt_keys:
                    line['isSelected'] = True
                    line['qtyToDeliver'] = pt_keys[key]
                else:
                    line['isSelected'] = False
                    line['qtyToDeliver'] = 0

        return groups

    def _safe_quant_available(self, quant):
        if hasattr(quant, 'available_quantity'):
            return quant.available_quantity or 0.0
        return (quant.quantity or 0.0) - (quant.reserved_quantity or 0.0)

    def _ml_pending_qty(self, ml):
        return (
            ml.quantity
            or getattr(ml, 'reserved_uom_qty', 0.0)
            or 0.0
        )

    def _move_pending_qty(self, move):
        demanded = move.product_uom_qty or 0.0
        done = 0.0
        for ml in move.move_line_ids:
            if ml.move_id.state == 'done' or ml.picking_id.state == 'done':
                done += self._ml_done_qty(ml)
        pending = demanded - done
        return max(pending, 0.0)

    def _append_group_line(self, groups_map, pid, pname, line_dict):
        if pid not in groups_map:
            groups_map[pid] = {
                'groupKey': 'product-%s' % pid,
                'productId': pid,
                'productName': pname,
                'lines': [],
                'totalQty': 0.0,
                'selectedCount': 0,
                'lineCount': 0,
            }

        group = groups_map[pid]
        line_dict.setdefault('groupKey', group['groupKey'])
        group['lines'].append(line_dict)
        group['lineCount'] += 1
        group['totalQty'] += line_dict.get('qtyToDeliver', 0.0) or 0.0
        if line_dict.get('isSelected'):
            group['selectedCount'] += 1

    def _build_delivery_groups(self, editing_pt_id=None):
        self.ensure_one()
        groups_map = OrderedDict()
        Quant = self.env['stock.quant']

        locked_lot_ids = self._get_locked_lot_ids(exclude_pt_id=editing_pt_id)

        pickings = self.picking_ids.filtered(
            lambda p: p.state in ('assigned', 'confirmed', 'waiting')
            and p.picking_type_code in ('internal', 'outgoing')
        )

        for picking in pickings:
            for move in picking.move_ids.filtered(
                lambda m: m.state not in ('done', 'cancel')
            ):
                pid = move.product_id.id
                pname = move.product_id.display_name
                sale_line = move.sale_line_id

                pending_mls = move.move_line_ids.filtered(
                    lambda ml: (
                        (ml.lot_id or ml.product_id)
                        and ml.picking_id == picking
                        and ml.move_id.state not in ('done', 'cancel')
                        and self._ml_pending_qty(ml) > 0
                        and (not ml.lot_id or ml.lot_id.id not in locked_lot_ids)
                    )
                )

                if pending_mls:
                    seen = set()
                    for ml in pending_mls:
                        key = (
                            picking.id,
                            move.id,
                            ml.id,
                            ml.lot_id.id if ml.lot_id else 0,
                        )
                        if key in seen:
                            continue
                        seen.add(key)

                        qty_pending = self._ml_pending_qty(ml)
                        if qty_pending <= 0:
                            continue

                        ld = {
                            'dbId': 0,
                            'lotId': ml.lot_id.id if ml.lot_id else 0,
                            'lotName': ml.lot_id.name if ml.lot_id else '',
                            'productId': pid,
                            'productName': pname,
                            'pickingId': picking.id,
                            'moveId': move.id,
                            'moveLineId': ml.id,
                            'saleLineId': sale_line.id if sale_line else 0,
                            'isSelected': False,
                            'qtyAvailable': qty_pending,
                            'qtyToDeliver': 0,
                            'sourceLocation': ml.location_id.display_name if ml.location_id else '',
                            'sourceLocationId': ml.location_id.id if ml.location_id else 0,
                        }
                        self._append_group_line(groups_map, pid, pname, ld)
                    continue

                move_pending = self._move_pending_qty(move)
                if move_pending <= 0:
                    continue

                if sale_line and hasattr(sale_line, 'lot_ids') and sale_line.lot_ids:
                    seen = set()
                    remaining_to_allocate = move_pending

                    for lot in sale_line.lot_ids:
                        if remaining_to_allocate <= 0:
                            break
                        if lot.id in locked_lot_ids:
                            continue

                        quants = Quant.search(
                            [
                                ('product_id', '=', pid),
                                ('lot_id', '=', lot.id),
                                ('location_id.usage', '=', 'internal'),
                                ('quantity', '>', 0),
                            ],
                            order='location_id',
                        )

                        for quant in quants:
                            key = (move.id, lot.id, quant.location_id.id)
                            if key in seen:
                                continue
                            seen.add(key)

                            qty_avail = self._safe_quant_available(quant)
                            if qty_avail <= 0:
                                continue

                            qty_pending = min(qty_avail, remaining_to_allocate)
                            if qty_pending <= 0:
                                continue

                            ld = {
                                'dbId': 0,
                                'lotId': lot.id,
                                'lotName': lot.name or '',
                                'productId': pid,
                                'productName': pname,
                                'pickingId': picking.id,
                                'moveId': move.id,
                                'moveLineId': 0,
                                'saleLineId': sale_line.id if sale_line else 0,
                                'isSelected': False,
                                'qtyAvailable': qty_pending,
                                'qtyToDeliver': 0,
                                'sourceLocation': quant.location_id.display_name or '',
                                'sourceLocationId': quant.location_id.id or 0,
                            }
                            self._append_group_line(groups_map, pid, pname, ld)
                            remaining_to_allocate -= qty_pending

                    continue

                if move_pending > 0:
                    ld = {
                        'dbId': 0,
                        'lotId': 0,
                        'lotName': '',
                        'productId': pid,
                        'productName': pname,
                        'pickingId': picking.id,
                        'moveId': move.id,
                        'moveLineId': 0,
                        'saleLineId': sale_line.id if sale_line else 0,
                        'isSelected': False,
                        'qtyAvailable': move_pending,
                        'qtyToDeliver': 0,
                        'sourceLocation': move.location_id.display_name if move.location_id else '',
                        'sourceLocationId': move.location_id.id if move.location_id else 0,
                    }
                    self._append_group_line(groups_map, pid, pname, ld)

        return [g for g in groups_map.values() if g['lineCount'] > 0]

    def _get_returned_qty_by_origin_remission_line(self):
        self.ensure_one()
        returned_by_line = {}

        return_docs = self.delivery_document_ids.filtered(
            lambda d: d.document_type == 'return'
            and d.state == 'confirmed'
            and (
                not d.return_picking_id
                or d.return_picking_id.state == 'done'
            )
        )

        for return_doc in return_docs:
            for line in return_doc.line_ids:
                if not line.origin_remission_line_id:
                    continue

                qty = (
                    line.qty_returned
                    or line.qty_done
                    or line.qty_selected
                    or 0.0
                )

                if qty <= 0:
                    continue

                origin_line_id = line.origin_remission_line_id.id
                returned_by_line[origin_line_id] = (
                    returned_by_line.get(origin_line_id, 0.0) + qty
                )

        return returned_by_line

    def _get_returned_qty_by_source_move_lot(self):
        self.ensure_one()
        returned_by_key = {}

        return_docs = self.delivery_document_ids.filtered(
            lambda d: d.document_type == 'return'
            and d.state == 'confirmed'
            and (
                not d.return_picking_id
                or d.return_picking_id.state == 'done'
            )
        )

        for return_doc in return_docs:
            for line in return_doc.line_ids:
                qty = (
                    line.qty_returned
                    or line.qty_done
                    or line.qty_selected
                    or 0.0
                )
                if qty <= 0 or not line.move_id:
                    continue

                key = (
                    line.move_id.id,
                    line.lot_id.id if line.lot_id else 0,
                )
                returned_by_key[key] = returned_by_key.get(key, 0.0) + qty

        return returned_by_key

    def _append_return_group_line(self, groups_map, group_key, group_name, line_dict):
        if group_key not in groups_map:
            groups_map[group_key] = {
                'groupKey': group_key,
                'productId': line_dict.get('productId') or 0,
                'productName': group_name,
                'originRemissionId': line_dict.get('originRemissionId') or 0,
                'originRemissionName': line_dict.get('originRemissionName') or '',
                'lines': [],
                'totalQty': 0.0,
                'selectedCount': 0,
                'lineCount': 0,
            }

        group = groups_map[group_key]
        line_dict['groupKey'] = group_key
        group['lines'].append(line_dict)
        group['lineCount'] += 1
        group['totalQty'] += line_dict.get('qtyDelivered', 0.0) or 0.0
        if line_dict.get('isSelected'):
            group['selectedCount'] += 1

    def _build_return_groups(self):
        self.ensure_one()
        groups_map = OrderedDict()
        returned_by_line = self._get_returned_qty_by_origin_remission_line()

        remissions = self.delivery_document_ids.filtered(
            lambda d: d.document_type == 'remission'
            and d.state == 'confirmed'
        ).sorted(lambda d: d.id)

        for remission in remissions:
            remission_name = remission.remission_number or remission.name or _('Sin Remisión')

            for doc_line in remission.line_ids.sorted(lambda l: (l.sequence, l.id)):
                delivered_qty = doc_line.qty_done or doc_line.qty_selected or 0.0
                returned_qty = returned_by_line.get(doc_line.id, 0.0)
                qty_available = max(delivered_qty - returned_qty, 0.0)

                if qty_available <= 0:
                    continue

                product = doc_line.product_id
                if not product:
                    continue

                source = self._resolve_return_source_for_remission_line(
                    remission,
                    doc_line,
                )
                move = source.get('move')
                ml = source.get('move_line')
                source_picking = source.get('picking')

                if not move:
                    continue

                pid = product.id
                pname = product.display_name
                group_key = 'remission-%s-product-%s' % (remission.id, pid)
                group_name = '%s · %s' % (remission_name, pname)

                ld = {
                    'dbId': 0,
                    'lotId': doc_line.lot_id.id if doc_line.lot_id else 0,
                    'lotName': doc_line.lot_id.name if doc_line.lot_id else '',
                    'productId': pid,
                    'productName': pname,
                    'pickingId': source_picking.id if source_picking else 0,
                    'moveId': move.id if move else 0,
                    'moveLineId': ml.id if ml else 0,
                    'saleLineId': doc_line.sale_line_id.id if doc_line.sale_line_id else 0,
                    'sourceLocationId': ml.location_dest_id.id if ml and ml.location_dest_id else 0,
                    'isSelected': False,
                    'qtyDelivered': qty_available,
                    'qtyToReturn': 0,
                    'originRemissionId': remission.id,
                    'originRemissionName': remission_name,
                    'originRemissionLineId': doc_line.id,
                }

                self._append_return_group_line(
                    groups_map, group_key, group_name, ld
                )

        if groups_map:
            return [g for g in groups_map.values() if g['lineCount'] > 0]

        returned_by_source = self._get_returned_qty_by_source_move_lot()

        for picking in self.picking_ids.filtered(
            lambda p: p.state == 'done' and p.picking_type_code == 'outgoing'
        ):
            fallback_remission_name = picking.name or _('Sin Remisión')

            for move in picking.move_ids.filtered(lambda m: m.state == 'done'):
                pid = move.product_id.id
                pname = move.product_id.display_name
                group_key = 'picking-%s-product-%s' % (picking.id, pid)
                group_name = '%s · %s' % (fallback_remission_name, pname)

                for ml in move.move_line_ids:
                    qty = self._ml_done_qty(ml)
                    if qty <= 0:
                        continue

                    returned_qty = returned_by_source.get(
                        (move.id, ml.lot_id.id if ml.lot_id else 0),
                        0.0,
                    )
                    qty_available = max(qty - returned_qty, 0.0)

                    if qty_available <= 0:
                        continue

                    ld = {
                        'dbId': 0,
                        'lotId': ml.lot_id.id if ml.lot_id else 0,
                        'lotName': ml.lot_id.name if ml.lot_id else '',
                        'productId': pid,
                        'productName': pname,
                        'pickingId': picking.id,
                        'moveId': move.id,
                        'moveLineId': ml.id,
                        'saleLineId': move.sale_line_id.id if move.sale_line_id else 0,
                        'sourceLocationId': ml.location_dest_id.id if ml.location_dest_id else 0,
                        'isSelected': False,
                        'qtyDelivered': qty_available,
                        'qtyToReturn': 0,
                        'originRemissionId': 0,
                        'originRemissionName': fallback_remission_name,
                        'originRemissionLineId': 0,
                    }

                    self._append_return_group_line(
                        groups_map, group_key, group_name, ld
                    )

        return [g for g in groups_map.values() if g['lineCount'] > 0]

    def _build_swap_groups(self):
        groups_map = OrderedDict()

        for picking in self.picking_ids.filtered(
            lambda p: p.state in ('assigned', 'confirmed')
            and p.picking_type_code in ('outgoing', 'internal')
        ):
            for move in picking.move_ids.filtered(
                lambda m: m.state in ('assigned', 'confirmed')
            ):
                for ml in move.move_line_ids:
                    if not ml.lot_id:
                        continue

                    pid = move.product_id.id
                    pname = move.product_id.display_name
                    lot = ml.lot_id

                    if pid not in groups_map:
                        groups_map[pid] = {
                            'groupKey': 'product-%s' % pid,
                            'productId': pid,
                            'productName': pname,
                            'lines': [],
                            'totalQty': 0.0,
                            'selectedCount': 0,
                            'lineCount': 0,
                        }
                    group = groups_map[pid]

                    ld = {
                        'groupKey': group['groupKey'],
                        'dbId': 0,
                        'productId': pid,
                        'productName': pname,
                        'pickingId': picking.id,
                        'moveId': move.id,
                        'moveLineId': ml.id,
                        'saleLineId': move.sale_line_id.id if move.sale_line_id else 0,
                        'originLotId': lot.id,
                        'originLotName': lot.name or '',
                        'originBloque': lot.x_bloque or '' if hasattr(lot, 'x_bloque') else '',
                        'originAlto': str(lot.x_alto) if hasattr(lot, 'x_alto') and lot.x_alto else '',
                        'originAncho': str(lot.x_ancho) if hasattr(lot, 'x_ancho') and lot.x_ancho else '',
                        'qty': ml.quantity or move.product_uom_qty or 0.0,
                        'targetLotId': 0,
                        'targetLotName': '',
                        'targetBloque': '',
                        'targetQty': 0.0,
                    }
                    group['lines'].append(ld)
                    group['lineCount'] += 1
                    group['totalQty'] += ld['qty']

        return [g for g in groups_map.values() if g['lineCount'] > 0]

    def _get_open_pick_tickets(self):
        self.ensure_one()
        return self.env['sale.delivery.document'].search([
            ('sale_order_id', '=', self.id),
            ('document_type', '=', 'pick_ticket'),
            ('state', '=', 'prepared'),
        ], order='create_date desc')

    def _check_delivery_authorization(self):
        self.ensure_one()
        if self.state not in ('sale', 'done'):
            raise UserError(_('Solo puede entregar pedidos confirmados.'))
        if hasattr(self, 'delivery_auth_state'):
            if self.delivery_auth_state == 'pending':
                if not self.env.user.has_group(
                    'sale_delivery_wizard.group_delivery_authorizer'
                ):
                    raise UserError(_(
                        'Este pedido no tiene autorización de entrega. '
                        'Contacte a un autorizador.'))

    def action_open_delivery_wizard(self):
        self.ensure_one()
        self._check_delivery_authorization()

        self._ensure_origin_demand_snapshot(source='delivery_button')

        return {
            'name': _('Entregar Material'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_sale_order_id': self.id,
                'active_id': self.id,
                'active_model': 'sale.order',
            },
        }

    def action_open_return_wizard(self):
        self.ensure_one()
        return {
            'name': _('Devolución de Material'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.return.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_sale_order_id': self.id,
                'active_id': self.id,
            },
        }

    def action_open_swap_wizard(self):
        self.ensure_one()

        self._ensure_origin_demand_snapshot(source='swap_button')

        return {
            'name': _('Swap de Lotes'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.swap.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_sale_order_id': self.id,
            },
        }

    def action_view_delivery_documents(self):
        self.ensure_one()
        return {
            'name': _('Documentos de Entrega'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id)],
            'context': {'default_sale_order_id': self.id},
        }

    def action_view_remissions(self):
        self.ensure_one()
        return {
            'name': _('Remisiones'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id), ('document_type', '=', 'remission')],
        }

    def action_view_returns(self):
        self.ensure_one()
        return {
            'name': _('Devoluciones'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id), ('document_type', '=', 'return')],
        }

    def action_view_pick_tickets(self):
        self.ensure_one()
        return {
            'name': _('Pick Tickets'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id), ('document_type', '=', 'pick_ticket')],
        }

    def action_view_redeliveries(self):
        self.ensure_one()
        return {
            'name': _('Reentregas'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id), ('document_type', '=', 'redelivery')],
        }