# -*- coding: utf-8 -*-
"""CANDADO DE ENTREGAS (regla del cliente, 29 sep 2026): jamás se puede
entregar más de lo solicitado.

Espejo de stock_return_guard. Vive en stock.move._action_done, el embudo por
el que pasa TODA salida al cliente (remisión SOM, botón "Validar" nativo, app
del chofer, carrito, reentregas): los movimientos al cliente ligados a una
línea de venta se validan contra el Solicitado de ESA línea:

    entregado (ya validado) − devuelto + lo que sale ahora ≤ solicitado

El candado de la remisión (_som_assert_remission_within_demand) revisa lo
que el USUARIO pidió; este revisa lo que el MOVIMIENTO se lleva. V/150 pasó
el primero y rompió el segundo: la remisión pidió 13.46 / 6.73 / 6.73, pero
las 4 placas se cargaron al movimiento del primer renglón y Odoo lo cerró en
26.92 de 13.46 (200 %) con los hermanos en 0.

El ratchet (Solicitado ≥ Asignado) corre al ASIGNAR placas, no al validar;
una asignación fresca legítima ya subió el Solicitado cuando llega aquí.
"""
from collections import defaultdict

from odoo import models, _
from odoo.exceptions import UserError
from odoo.tools import float_compare


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _som_is_customer_delivery(self):
        self.ensure_one()
        return bool(
            self.sale_line_id
            and self.location_dest_id.usage == 'customer'
            and self.location_id.usage != 'customer'
            # Componentes de kit: su cantidad no se compara con la del renglón.
            and self.product_id == self.sale_line_id.product_id)

    def _som_ml_qty_in_line_uom(self, ml, line_uom):
        qty = ml.quantity or 0.0
        if line_uom and ml.product_uom_id and ml.product_uom_id != line_uom:
            qty = ml.product_uom_id._compute_quantity(qty, line_uom, rounding_method='HALF-UP')
        return qty

    def _som_outgoing_qty_now(self, line_uom):
        """Lo que ESTE movimiento se llevará al cerrarse: solo lo marcado como
        recogido (Odoo 17+ manda a backorder lo no recogido)."""
        self.ensure_one()
        has_picked = 'picked' in self._fields
        if has_picked and not self.picked:
            return 0.0
        mls = self.move_line_ids
        if has_picked:
            # Odoo borra al cerrar las líneas no recogidas de un move recogido.
            mls = mls.filtered('picked')
        if not self.move_line_ids:
            qty = self.quantity or 0.0
            if line_uom and self.product_uom and self.product_uom != line_uom:
                qty = self.product_uom._compute_quantity(qty, line_uom, rounding_method='HALF-UP')
            return qty
        return sum(self._som_ml_qty_in_line_uom(ml, line_uom) for ml in mls)

    def _som_check_delivery_vs_demand(self):
        if self.env.context.get('som_skip_delivery_guard'):
            return
        outgoing = self.filtered(
            lambda m: m.state not in ('done', 'cancel') and m._som_is_customer_delivery())
        if not outgoing:
            return
        MoveLine = self.env['stock.move.line'].sudo()
        by_line = defaultdict(lambda: self.env['stock.move'])
        for move in outgoing:
            by_line[move.sale_line_id] |= move

        problems = []
        for line, moves in by_line.items():
            line_uom = (
                line.product_uom_id if 'product_uom_id' in line._fields
                else line.product_uom)
            done = MoveLine.search([
                ('move_id.sale_line_id', '=', line.id),
                ('move_id.product_id', '=', line.product_id.id),
                ('state', '=', 'done'),
            ])
            delivered = sum(
                self._som_ml_qty_in_line_uom(ml, line_uom)
                for ml in done if ml.location_dest_id.usage == 'customer')
            returned = sum(
                self._som_ml_qty_in_line_uom(ml, line_uom)
                for ml in done if ml.location_id.usage == 'customer')
            now = sum(m._som_outgoing_qty_now(line_uom) for m in moves)
            if now <= 0:
                continue
            demand = line.product_uom_qty or 0.0
            total = delivered - returned + now
            over = (line_uom.compare(total, demand) if line_uom
                    else float_compare(total, demand, precision_digits=4)) > 0
            if over:
                problems.append(_(
                    '%(order)s · %(product)s: solicitado %(demand).2f, ya entregado (neto) '
                    '%(net).2f, esta salida lleva %(now).2f (máximo permitido %(allowed).2f).'
                ) % {
                    'order': line.order_id.name,
                    'product': line.product_id.display_name,
                    'demand': demand,
                    'net': delivered - returned,
                    'now': now,
                    'allowed': max(demand - (delivered - returned), 0.0),
                })
        if problems:
            raise UserError(_(
                'No se puede entregar más de lo solicitado.\n\n%s\n\n'
                'Revisa las cantidades y los lotes de la salida: cada placa debe ir en el '
                'renglón de venta al que pertenece. Si el cliente realmente se lleva más, '
                'primero se ajusta el Solicitado en la orden.'
            ) % '\n'.join(problems))

    def _action_done(self, cancel_backorder=False):
        self._som_check_delivery_vs_demand()
        return super()._action_done(cancel_backorder=cancel_backorder)
