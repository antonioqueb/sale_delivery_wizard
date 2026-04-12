from collections import OrderedDict
from odoo import api, fields, models, _


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
    x_redelivery_count = fields.Integer(
        compute='_compute_document_counts',
        string='Reentregas',
    )
    x_redelivery_pending_count = fields.Integer(
        compute='_compute_document_counts',
        string='Reentregas Pendientes',
    )

    @api.depends(
        'order_line.product_uom_qty',
        'order_line.qty_delivered',
        'order_line.x_returned_qty',
    )
    def _compute_delivery_summary(self):
        for order in self:
            lines = order.order_line.filtered(
                lambda l: l.product_id.type != 'service'
            )
            demand = sum(lines.mapped('product_uom_qty'))
            assigned = sum(lines.mapped('product_uom_qty'))
            delivered_net = sum(lines.mapped('qty_delivered'))
            returned = sum(lines.mapped('x_returned_qty'))
            pending = demand - delivered_net

            order.x_total_demand_qty = demand
            order.x_total_assigned_qty = assigned
            order.x_total_delivered_gross_qty = delivered_net + returned
            order.x_total_returned_qty = returned
            order.x_total_delivered_net_qty = max(delivered_net, 0.0)
            order.x_total_pending_delivery_qty = max(pending, 0.0)
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
            order.x_redelivery_count = len(
                docs.filtered(lambda d: d.document_type == 'redelivery')
            )
            order.x_redelivery_pending_count = len(
                docs.filtered(
                    lambda d: d.document_type == 'redelivery'
                    and d.state in ('draft', 'prepared')
                )
            )

    def get_delivery_grouped_data(self, mode='delivery'):
        self.ensure_one()

        if mode == 'delivery':
            groups = self._build_delivery_groups()
            return self._apply_pick_ticket_selection(groups)
        if mode == 'return':
            return self._build_return_groups()
        if mode == 'swap':
            return self._build_swap_groups()
        return []

    # ═══════════════════════════════════════════════════════════════════
    # Pick Ticket selection overlay
    # ═══════════════════════════════════════════════════════════════════

    def _apply_pick_ticket_selection(self, groups):
        """If a prepared Pick Ticket exists, override isSelected/qtyToDeliver
        so only the PT lines appear selected."""
        self.ensure_one()
        pending_pt = self.env['sale.delivery.document'].search([
            ('sale_order_id', '=', self.id),
            ('document_type', '=', 'pick_ticket'),
            ('state', '=', 'prepared'),
        ], order='create_date desc', limit=1)

        if not pending_pt or not pending_pt.line_ids:
            return groups

        # Build lookup (product_id, lot_id) → qty from PT
        pt_keys = {}
        for pt_line in pending_pt.line_ids:
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

    # ═══════════════════════════════════════════════════════════════════

    def _safe_quant_available(self, quant):
        if hasattr(quant, 'available_quantity'):
            return quant.available_quantity or 0.0
        return (quant.quantity or 0.0) - (quant.reserved_quantity or 0.0)

    def _ml_pending_qty(self, ml):
        """
        Cantidad pendiente utilizable para el wizard.
        Prioriza reservado/cantidad actual de la move line.
        """
        return (
            ml.quantity
            or getattr(ml, 'reserved_uom_qty', 0.0)
            or 0.0
        )

    def _move_pending_qty(self, move):
        """
        Cantidad pendiente del move a partir de lo ya entregado.
        """
        demanded = move.product_uom_qty or 0.0
        done = 0.0
        for ml in move.move_line_ids:
            if ml.move_id.state == 'done' or ml.picking_id.state == 'done':
                done += (ml.quantity or getattr(ml, 'qty_done', 0.0) or 0.0)
        pending = demanded - done
        return max(pending, 0.0)

    def _append_group_line(self, groups_map, pid, pname, line_dict):
        if pid not in groups_map:
            groups_map[pid] = {
                'productId': pid,
                'productName': pname,
                'lines': [],
                'totalQty': 0.0,
                'selectedCount': 0,
                'lineCount': 0,
            }

        group = groups_map[pid]
        group['lines'].append(line_dict)
        group['lineCount'] += 1
        group['totalQty'] += line_dict.get('qtyToDeliver', 0.0) or 0.0
        if line_dict.get('isSelected'):
            group['selectedCount'] += 1

    def _build_delivery_groups(self):
        """
        Build delivery groups ONLY from pending/open pickings.

        Regla clave:
        - No volver a usar toda la selección histórica de sale_line.lot_ids
          si el picking ya avanzó.
        - Mostrar solo lo pendiente de entregar.
        """
        self.ensure_one()
        groups_map = OrderedDict()
        Quant = self.env['stock.quant']

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

                # 1) PRIMERO: usar move_line_ids reales del picking abierto
                pending_mls = move.move_line_ids.filtered(
                    lambda ml: (
                        (ml.lot_id or ml.product_id)
                        and ml.picking_id == picking
                        and ml.move_id.state not in ('done', 'cancel')
                        and self._ml_pending_qty(ml) > 0
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
                            'isSelected': qty_pending > 0,
                            'qtyAvailable': qty_pending,
                            'qtyToDeliver': qty_pending,
                            'sourceLocation': ml.location_id.display_name if ml.location_id else '',
                            'sourceLocationId': ml.location_id.id if ml.location_id else 0,
                        }
                        self._append_group_line(groups_map, pid, pname, ld)
                    continue

                # 2) FALLBACK: si todavía no hay move lines, usar sale_line.lot_ids
                # pero SOLO si el move sigue teniendo pendiente
                move_pending = self._move_pending_qty(move)
                if move_pending <= 0:
                    continue

                if sale_line and hasattr(sale_line, 'lot_ids') and sale_line.lot_ids:
                    seen = set()
                    remaining_to_allocate = move_pending

                    for lot in sale_line.lot_ids:
                        if remaining_to_allocate <= 0:
                            break

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
                                'isSelected': qty_pending > 0,
                                'qtyAvailable': qty_pending,
                                'qtyToDeliver': qty_pending,
                                'sourceLocation': quant.location_id.display_name or '',
                                'sourceLocationId': quant.location_id.id or 0,
                            }
                            self._append_group_line(groups_map, pid, pname, ld)
                            remaining_to_allocate -= qty_pending

                    continue

                # 3) Último fallback: sin lotes, solo producto pendiente
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
                        'isSelected': True,
                        'qtyAvailable': move_pending,
                        'qtyToDeliver': move_pending,
                        'sourceLocation': move.location_id.display_name if move.location_id else '',
                        'sourceLocationId': move.location_id.id if move.location_id else 0,
                    }
                    self._append_group_line(groups_map, pid, pname, ld)

        # Solo grupos con líneas efectivamente pendientes
        return [g for g in groups_map.values() if g['lineCount'] > 0]

    def _build_return_groups(self):
        groups_map = OrderedDict()

        for picking in self.picking_ids.filtered(
            lambda p: p.state == 'done' and p.picking_type_code == 'outgoing'
        ):
            for move in picking.move_ids.filtered(lambda m: m.state == 'done'):
                pid = move.product_id.id
                pname = move.product_id.display_name

                if pid not in groups_map:
                    groups_map[pid] = {
                        'productId': pid,
                        'productName': pname,
                        'lines': [],
                        'totalQty': 0.0,
                        'selectedCount': 0,
                        'lineCount': 0,
                    }
                group = groups_map[pid]

                for ml in move.move_line_ids:
                    qty = ml.quantity or ml.qty_done or 0.0
                    if qty <= 0:
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
                        'isSelected': True,
                        'qtyDelivered': qty,
                        'qtyToReturn': qty,
                    }
                    group['lines'].append(ld)
                    group['lineCount'] += 1
                    group['totalQty'] += qty
                    group['selectedCount'] += 1

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
                            'productId': pid,
                            'productName': pname,
                            'lines': [],
                            'totalQty': 0.0,
                            'selectedCount': 0,
                            'lineCount': 0,
                        }
                    group = groups_map[pid]

                    ld = {
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

    def action_open_delivery_wizard(self):
        self.ensure_one()
        if self.state not in ('sale', 'done'):
            from odoo.exceptions import UserError
            raise UserError(_('Solo puede entregar pedidos confirmados.'))
        if hasattr(self, 'delivery_auth_state'):
            if self.delivery_auth_state == 'pending':
                if not self.env.user.has_group(
                    'sale_delivery_wizard.group_delivery_authorizer'
                ):
                    from odoo.exceptions import UserError
                    raise UserError(
                        _('Este pedido no tiene autorización de entrega. Contacte a un autorizador.')
                    )
        return {
            'name': _('Entregar Material'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_sale_order_id': self.id,
                'active_id': self.id,
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