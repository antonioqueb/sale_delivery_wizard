from collections import OrderedDict
from odoo import api, fields, models, _


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    delivery_document_ids = fields.One2many(
        'sale.delivery.document', 'sale_order_id',
        string='Documentos de Entrega')

    x_total_assigned_qty = fields.Float(
        compute='_compute_delivery_summary', string='Total Asignado')
    x_total_delivered_gross_qty = fields.Float(
        compute='_compute_delivery_summary', string='Entregado Bruto')
    x_total_returned_qty = fields.Float(
        compute='_compute_delivery_summary', string='Devuelto')
    x_total_delivered_net_qty = fields.Float(
        compute='_compute_delivery_summary', string='Entregado Neto')
    x_total_pending_delivery_qty = fields.Float(
        compute='_compute_delivery_summary', string='Pendiente Entrega')
    x_total_demand_qty = fields.Float(
        compute='_compute_delivery_summary', string='Demanda Total')
    x_fulfillment_net_pct = fields.Float(
        compute='_compute_delivery_summary', string='Fulfillment Neto %')

    x_delivery_document_count = fields.Integer(
        compute='_compute_document_counts', string='Documentos')
    x_remission_count = fields.Integer(
        compute='_compute_document_counts', string='Remisiones')
    x_return_count = fields.Integer(
        compute='_compute_document_counts', string='Devoluciones')
    x_pick_ticket_count = fields.Integer(
        compute='_compute_document_counts', string='Pick Tickets')
    x_redelivery_count = fields.Integer(
        compute='_compute_document_counts', string='Reentregas')
    x_redelivery_pending_count = fields.Integer(
        compute='_compute_document_counts', string='Reentregas Pendientes')

    @api.depends(
        'order_line.product_uom_qty',
        'order_line.qty_delivered',
        'order_line.x_returned_qty',
    )
    def _compute_delivery_summary(self):
        for order in self:
            lines = order.order_line.filtered(
                lambda l: l.product_id.type != 'service')
            demand = sum(lines.mapped('product_uom_qty'))
            assigned = sum(lines.mapped('product_uom_qty'))
            delivered_net = sum(lines.mapped('qty_delivered'))
            returned = sum(lines.mapped('x_returned_qty'))
            pending = demand - delivered_net

            order.x_total_demand_qty = demand
            order.x_total_assigned_qty = assigned
            order.x_total_delivered_gross_qty = delivered_net + returned
            order.x_total_returned_qty = returned
            order.x_total_delivered_net_qty = max(delivered_net, 0)
            order.x_total_pending_delivery_qty = max(pending, 0)
            order.x_fulfillment_net_pct = (
                (delivered_net / demand * 100) if demand else 0.0)

    @api.depends(
        'delivery_document_ids',
        'delivery_document_ids.document_type',
        'delivery_document_ids.state'
    )
    def _compute_document_counts(self):
        for order in self:
            docs = order.delivery_document_ids
            order.x_delivery_document_count = len(docs)
            order.x_remission_count = len(
                docs.filtered(lambda d: d.document_type == 'remission'))
            order.x_return_count = len(
                docs.filtered(lambda d: d.document_type == 'return'))
            order.x_pick_ticket_count = len(
                docs.filtered(lambda d: d.document_type == 'pick_ticket'))
            order.x_redelivery_count = len(
                docs.filtered(lambda d: d.document_type == 'redelivery'))
            order.x_redelivery_pending_count = len(
                docs.filtered(
                    lambda d: d.document_type == 'redelivery'
                    and d.state in ('draft', 'prepared')
                )
            )

    def get_delivery_grouped_data(self, mode='delivery'):
        self.ensure_one()

        if mode == 'delivery':
            return self._build_delivery_groups()
        elif mode == 'return':
            return self._build_return_groups()
        elif mode == 'swap':
            return self._build_swap_groups()
        return []

    def _safe_quant_available(self, quant):
        if hasattr(quant, 'available_quantity'):
            return quant.available_quantity or 0.0
        return (quant.quantity or 0.0) - (quant.reserved_quantity or 0.0)

    def _build_delivery_groups(self):
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

                sale_line = move.sale_line_id
                lots_used = False

                if sale_line and hasattr(sale_line, 'lot_ids') and sale_line.lot_ids:
                    seen = set()

                    for lot in sale_line.lot_ids:
                        quants = Quant.search([
                            ('product_id', '=', pid),
                            ('lot_id', '=', lot.id),
                            ('location_id.usage', '=', 'internal'),
                            ('quantity', '>', 0),
                        ], order='location_id')

                        if not quants:
                            group['lines'].append({
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
                                'qtyAvailable': 0.0,
                                'qtyToDeliver': 0.0,
                                'sourceLocation': '',
                                'sourceLocationId': 0,
                            })
                            group['lineCount'] += 1
                            continue

                        for quant in quants:
                            key = (move.id, lot.id, quant.location_id.id)
                            if key in seen:
                                continue
                            seen.add(key)

                            qty_avail = self._safe_quant_available(quant)
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
                                'isSelected': qty_avail > 0,
                                'qtyAvailable': qty_avail,
                                'qtyToDeliver': qty_avail if qty_avail > 0 else 0.0,
                                'sourceLocation': quant.location_id.display_name or '',
                                'sourceLocationId': quant.location_id.id or 0,
                            }
                            group['lines'].append(ld)
                            group['lineCount'] += 1
                            group['totalQty'] += ld['qtyToDeliver']
                            if ld['isSelected']:
                                group['selectedCount'] += 1
                            lots_used = True

                if not lots_used:
                    for ml in move.move_line_ids:
                        lot_id = ml.lot_id.id if ml.lot_id else 0
                        qty = ml.quantity or getattr(ml, 'reserved_uom_qty', 0) or 0.0
                        if not lot_id and not qty:
                            continue

                        ld = {
                            'dbId': 0,
                            'lotId': lot_id,
                            'lotName': ml.lot_id.name if ml.lot_id else '',
                            'productId': pid,
                            'productName': pname,
                            'pickingId': picking.id,
                            'moveId': move.id,
                            'moveLineId': ml.id,
                            'saleLineId': sale_line.id if sale_line else 0,
                            'isSelected': qty > 0,
                            'qtyAvailable': qty,
                            'qtyToDeliver': qty if qty > 0 else 0.0,
                            'sourceLocation': ml.location_id.display_name if ml.location_id else '',
                            'sourceLocationId': ml.location_id.id if ml.location_id else 0,
                        }
                        group['lines'].append(ld)
                        group['lineCount'] += 1
                        group['totalQty'] += ld['qtyToDeliver']
                        if ld['isSelected']:
                            group['selectedCount'] += 1
                        lots_used = True

                if not lots_used:
                    qty = move.product_uom_qty or 0.0
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
                        'isSelected': qty > 0,
                        'qtyAvailable': qty,
                        'qtyToDeliver': qty,
                        'sourceLocation': move.location_id.display_name if move.location_id else '',
                        'sourceLocationId': move.location_id.id if move.location_id else 0,
                    }
                    group['lines'].append(ld)
                    group['lineCount'] += 1
                    group['totalQty'] += qty
                    if qty > 0:
                        group['selectedCount'] += 1

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