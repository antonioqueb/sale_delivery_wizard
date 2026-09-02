# -*- coding: utf-8 -*-
"""CANDADO DE DEVOLUCIONES (regla del cliente, 2 sep 2026): jamás se puede
devolver más de lo que se entregó.

Vive en stock.move._action_done, el embudo por el que pasa TODA devolución
(asistente SOM, botón "Devolver" nativo de Odoo, transferencias directas):
un movimiento que regresa material del cliente al almacén ligado a una
línea de venta se valida contra lo entregado neto de ESA línea:

    devuelto (ya validado) + lo que se está devolviendo ≤ entregado

y, por lote, contra lo entregado de ese lote en esa línea (no se acepta un
lote que nunca salió en la venta ni más metros de los que salieron).
"""
from collections import defaultdict

from odoo import models, _
from odoo.exceptions import UserError

TOL = 0.0001


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _som_is_customer_return(self):
        self.ensure_one()
        return bool(
            self.sale_line_id
            and self.location_id.usage == 'customer'
            and self.location_dest_id.usage in ('internal', 'transit'))

    def _som_check_returns_vs_delivered(self):
        returns = self.filtered(lambda m: m.state not in ('done', 'cancel') and m._som_is_customer_return())
        if not returns or self.env.context.get('som_skip_return_guard'):
            return
        MoveLine = self.env['stock.move.line'].sudo()
        by_line = defaultdict(lambda: self.env['stock.move'])
        for move in returns:
            by_line[move.sale_line_id] |= move

        problems = []
        for line, moves in by_line.items():
            done = MoveLine.search([
                ('move_id.sale_line_id', '=', line.id),
                ('state', '=', 'done'),
            ])
            delivered = sum((ml.quantity or 0.0) for ml in done if ml.location_dest_id.usage == 'customer')
            returned = sum((ml.quantity or 0.0) for ml in done if ml.location_id.usage == 'customer')
            incoming = 0.0
            incoming_by_lot = defaultdict(float)
            for move in moves:
                mls = move.move_line_ids
                if mls:
                    for ml in mls:
                        qty = ml.quantity or 0.0
                        incoming += qty
                        if ml.lot_id:
                            incoming_by_lot[ml.lot_id] += qty
                else:
                    incoming += move.product_uom_qty or 0.0
            product = line.product_id.display_name
            order = line.order_id.name
            if returned + incoming > delivered + TOL:
                problems.append(_(
                    '%(order)s · %(product)s: entregado %(delivered).2f, ya devuelto %(returned).2f, '
                    'se intenta devolver %(incoming).2f (máximo permitido %(allowed).2f).'
                ) % {'order': order, 'product': product, 'delivered': delivered, 'returned': returned,
                     'incoming': incoming, 'allowed': max(delivered - returned, 0.0)})
                continue
            # Por lote: solo si la entrega de esta línea se hizo con lotes.
            delivered_by_lot = defaultdict(float)
            returned_by_lot = defaultdict(float)
            for ml in done:
                if not ml.lot_id:
                    continue
                if ml.location_dest_id.usage == 'customer':
                    delivered_by_lot[ml.lot_id] += ml.quantity or 0.0
                elif ml.location_id.usage == 'customer':
                    returned_by_lot[ml.lot_id] += ml.quantity or 0.0
            if not delivered_by_lot:
                continue
            for lot, qty in incoming_by_lot.items():
                allowed = delivered_by_lot.get(lot, 0.0) - returned_by_lot.get(lot, 0.0)
                if lot not in delivered_by_lot:
                    problems.append(_(
                        '%(order)s · %(product)s: el lote %(lot)s no se entregó en esta venta.'
                    ) % {'order': order, 'product': product, 'lot': lot.name})
                elif qty > allowed + TOL:
                    problems.append(_(
                        '%(order)s · %(product)s · lote %(lot)s: entregado %(d).2f, ya devuelto %(r).2f, '
                        'se intenta devolver %(q).2f.'
                    ) % {'order': order, 'product': product, 'lot': lot.name,
                         'd': delivered_by_lot.get(lot, 0.0), 'r': returned_by_lot.get(lot, 0.0), 'q': qty})
        if problems:
            raise UserError(_(
                'No se puede devolver más de lo entregado.\n\n%s\n\n'
                'Corrige las cantidades de la devolución.'
            ) % '\n'.join(problems))

    def _action_done(self, cancel_backorder=False):
        self._som_check_returns_vs_delivered()
        return super()._action_done(cancel_backorder=cancel_backorder)
