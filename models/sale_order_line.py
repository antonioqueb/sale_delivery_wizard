from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    # ═══════════════════════════════════════════════════════════════════
    # PISO DE EDICIÓN: la asignación de un lote SIEMPRE es editable, pero
    # jamás por debajo de lo ya entregado de ese lote (regla de negocio).
    # ═══════════════════════════════════════════════════════════════════

    _SOM_FLOOR_SKIP_CONTEXTS = (
        'skip_stone_sync_picking',   # adopción/reparaciones internas
        'som_skip_cart_mirror',      # espejos carrito⇄selector
        'som_skip_breakdown_floor',  # escape explícito
    )

    def write(self, vals):
        if 'x_lot_breakdown_json' in vals and not any(
            self.env.context.get(k) for k in self._SOM_FLOOR_SKIP_CONTEXTS
        ):
            self._som_assert_breakdown_floor_vs_delivered(
                vals.get('x_lot_breakdown_json'))
        return super().write(vals)

    def _som_assert_breakdown_floor_vs_delivered(self, new_breakdown):
        """Valida que el desglose propuesto no asigne a ningún lote MENOS de
        lo que ya se le entregó (entregado neto por lote). El lote ausente
        del desglose nuevo cuenta como 0."""
        bd = new_breakdown or {}
        if not isinstance(bd, dict):
            return
        Lot = self.env['stock.lot']
        for line in self:
            if line.display_type or line.state not in ('sale', 'done'):
                continue
            order = line.order_id
            if not hasattr(order, '_som_lot_delivered_net_map'):
                continue
            delivered_map = order._som_lot_delivered_net_map()
            for (slid, lot_id), delivered in delivered_map.items():
                if slid != line.id or delivered <= 0.0001:
                    continue
                lot = Lot.browse(lot_id).exists()
                if not lot:
                    continue
                new_qty = None
                val = bd.get(str(lot_id))
                if val is not None:
                    new_qty = float(val or 0.0)
                elif 'x_selected_lots' in line._fields:
                    for quant in line.x_selected_lots:
                        if quant.lot_id.id == lot_id and str(quant.id) in bd:
                            new_qty = float(bd[str(quant.id)] or 0.0)
                            break
                if new_qty is None:
                    new_qty = 0.0
                if new_qty + 0.0001 < delivered:
                    raise UserError(_(
                        'No puedes asignar %(new).3f del lote %(lot)s en '
                        '"%(product)s": ya se entregaron %(delivered).3f de '
                        'ese lote. La asignación puede subir o quedarse, '
                        'pero nunca por debajo de lo entregado.'
                    ) % {
                        'new': new_qty,
                        'lot': lot.name,
                        'product': line.product_id.display_name,
                        'delivered': delivered,
                    })

    # ═══════════════════════════════════════════════════════════════════
    # Demanda origen operativa
    # ═══════════════════════════════════════════════════════════════════

    x_origin_demand_qty = fields.Float(
        string='Demanda Origen',
        copy=False,
        readonly=True,
        help=(
            'Cantidad original congelada antes del primer evento operativo '
            'de entrega. Sirve como base histórica aunque la demanda actual '
            'cambie después por swap, ajustes o diferencias de m².'
        ),
    )

    x_origin_demand_locked = fields.Boolean(
        string='Demanda Origen Congelada',
        copy=False,
        readonly=True,
    )

    x_origin_demand_locked_at = fields.Datetime(
        string='Fecha Congelación Demanda',
        copy=False,
        readonly=True,
    )

    x_origin_demand_source = fields.Selection(
        [
            ('delivery_button', 'Botón Entregar'),
            ('swap_button', 'Botón Swap'),
            ('manual', 'Manual'),
        ],
        string='Origen de Congelación',
        copy=False,
        readonly=True,
    )

    # ═══════════════════════════════════════════════════════════════════
    # Métricas existentes extendidas
    # ═══════════════════════════════════════════════════════════════════

    x_returned_qty = fields.Float(
        compute='_compute_return_qty',
        string='Cantidad Devuelta',
        store=True,
    )

    x_delivered_net_qty = fields.Float(
        compute='_compute_delivery_net',
        string='Entregado Neto',
        store=True,
    )

    x_pending_qty = fields.Float(
        compute='_compute_pending_fulfillment',
        string='Pendiente',
        store=True,
    )

    x_current_pending_qty = fields.Float(
        compute='_compute_pending_fulfillment',
        string='Pendiente Actual',
        store=True,
    )

    x_origin_pending_qty = fields.Float(
        compute='_compute_pending_fulfillment',
        string='Pendiente Origen',
        store=True,
    )

    x_overdelivered_origin_qty = fields.Float(
        compute='_compute_pending_fulfillment',
        string='Sobreentrega Origen',
        store=True,
    )

    x_fulfillment_net_pct = fields.Float(
        compute='_compute_pending_fulfillment',
        string='Fulfillment Neto %',
        store=True,
    )

    x_delivery_status = fields.Selection(
        [
            ('sin_asignar', 'Sin Asignar'),
            ('parcial_asignado', 'Parcial Asignado'),
            ('asignado', 'Asignado'),
            ('parcial_entregado', 'Parcial Entregado'),
            ('entregado', 'Entregado'),
            ('sobreentregado', 'Sobreentregado'),
            ('devuelto_parcial', 'Devuelto Parcial'),
            ('finiquitado', 'Finiquitado'),
        ],
        compute='_compute_delivery_status',
        string='Estado Entrega',
        store=True,
    )

    # ═══════════════════════════════════════════════════════════════════
    # Snapshot
    # ═══════════════════════════════════════════════════════════════════

    def _ensure_origin_demand_snapshot(self, source='manual'):
        """
        Congela la demanda origen una sola vez.

        Importante:
        - No congela 0.
        - No modifica líneas ya congeladas.
        - No aplica a servicios.
        """
        allowed_sources = {
            'delivery_button',
            'swap_button',
            'manual',
        }
        if source not in allowed_sources:
            source = 'manual'

        for line in self:
            if line.x_origin_demand_locked:
                continue

            if line.product_id and line.product_id.type == 'service':
                continue

            qty = line.product_uom_qty or 0.0
            if qty <= 0:
                continue

            line.write({
                'x_origin_demand_qty': qty,
                'x_origin_demand_locked': True,
                'x_origin_demand_locked_at': fields.Datetime.now(),
                'x_origin_demand_source': source,
            })

        return True

    def _get_delivery_base_demand_qty(self):
        self.ensure_one()
        if self.x_origin_demand_locked and self.x_origin_demand_qty > 0:
            return self.x_origin_demand_qty
        return self.product_uom_qty or 0.0

    # ═══════════════════════════════════════════════════════════════════
    # Helpers de cantidad
    # ═══════════════════════════════════════════════════════════════════

    def _som_move_done_qty(self, move):
        qty = 0.0

        for ml in move.move_line_ids:
            qty += (
                ml.quantity
                or getattr(ml, 'qty_done', 0.0)
                or 0.0
            )

        if qty:
            return qty

        return move.product_uom_qty or 0.0

    def _som_custom_delivery_gross_qty(self):
        self.ensure_one()

        docs = self.order_id.delivery_document_ids.filtered(
            lambda d: d.state == 'confirmed'
            and d.document_type in ('remission', 'redelivery')
        )

        doc_lines = docs.mapped('line_ids').filtered(
            lambda l: l.sale_line_id == self
            and l.product_id == self.product_id
        )

        return sum(
            l.qty_done or l.qty_selected or 0.0
            for l in doc_lines
        )

    def _som_custom_returned_qty(self):
        self.ensure_one()

        docs = self.order_id.delivery_document_ids.filtered(
            lambda d: d.state == 'confirmed'
            and d.document_type == 'return'
            and (
                not d.return_picking_id
                or d.return_picking_id.state == 'done'
            )
        )

        doc_lines = docs.mapped('line_ids').filtered(
            lambda l: l.sale_line_id == self
            and l.product_id == self.product_id
        )

        return sum(
            l.qty_returned or l.qty_done or l.qty_selected or 0.0
            for l in doc_lines
        )

    def _som_stock_returned_qty(self):
        self.ensure_one()

        if not self.product_id:
            return 0.0

        source_moves = self.move_ids.filtered(lambda m: m.state == 'done')

        return_moves = self.env['stock.move'].search([
            ('state', '=', 'done'),
            ('product_id', '=', self.product_id.id),
            ('location_id.usage', '=', 'customer'),
            ('location_dest_id.usage', '=', 'internal'),
            '|',
            ('origin_returned_move_id', 'in', source_moves.ids or [0]),
            ('sale_line_id', '=', self.id),
        ])

        return sum(self._som_move_done_qty(move) for move in return_moves)

    # ═══════════════════════════════════════════════════════════════════
    # Computes
    # ═══════════════════════════════════════════════════════════════════

    @api.depends(
        'move_ids.state',
        'move_ids.product_uom_qty',
        'move_ids.move_line_ids.quantity',
        'move_ids.returned_move_ids.state',
        'move_ids.returned_move_ids.product_uom_qty',
        'move_ids.returned_move_ids.location_id.usage',
        'move_ids.returned_move_ids.location_dest_id.usage',
        'move_ids.returned_move_ids.move_line_ids.quantity',
        'order_id.delivery_document_ids.state',
        'order_id.delivery_document_ids.document_type',
        'order_id.delivery_document_ids.return_picking_id.state',
        'order_id.delivery_document_ids.line_ids.sale_line_id',
        'order_id.delivery_document_ids.line_ids.product_id',
        'order_id.delivery_document_ids.line_ids.qty_selected',
        'order_id.delivery_document_ids.line_ids.qty_done',
        'order_id.delivery_document_ids.line_ids.qty_returned',
    )
    def _compute_return_qty(self):
        for line in self:
            stock_returned = line._som_stock_returned_qty()
            doc_returned = line._som_custom_returned_qty()

            # max evita doble conteo cuando el documento custom y el stock.move
            # representan la misma devolución.
            line.x_returned_qty = max(stock_returned, doc_returned)

    @api.depends(
        'qty_delivered',
        'x_returned_qty',
        'order_id.delivery_document_ids.state',
        'order_id.delivery_document_ids.document_type',
        'order_id.delivery_document_ids.line_ids.sale_line_id',
        'order_id.delivery_document_ids.line_ids.product_id',
        'order_id.delivery_document_ids.line_ids.qty_selected',
        'order_id.delivery_document_ids.line_ids.qty_done',
        'order_id.delivery_document_ids.line_ids.qty_returned',
    )
    def _compute_delivery_net(self):
        for line in self:
            gross_from_docs = line._som_custom_delivery_gross_qty()
            returned = line.x_returned_qty or 0.0

            if gross_from_docs > 0:
                line.x_delivered_net_qty = max(gross_from_docs - returned, 0.0)
            else:
                # Fallback nativo: si no hay documentos SOM, se respeta el cálculo
                # estándar de Odoo.
                line.x_delivered_net_qty = max(line.qty_delivered or 0.0, 0.0)

    @api.depends(
        'product_uom_qty',
        'qty_delivered',
        'x_returned_qty',
        'x_delivered_net_qty',
        'x_origin_demand_qty',
        'x_origin_demand_locked',
    )
    def _compute_pending_fulfillment(self):
        for line in self:
            current_demand = line.product_uom_qty or 0.0
            origin_demand = line._get_delivery_base_demand_qty()
            delivered_net = line.x_delivered_net_qty or 0.0

            origin_pending = max(origin_demand - delivered_net, 0.0)
            current_pending = max(current_demand - delivered_net, 0.0)
            overdelivered = max(delivered_net - origin_demand, 0.0)

            line.x_pending_qty = origin_pending
            line.x_origin_pending_qty = origin_pending
            line.x_current_pending_qty = current_pending
            line.x_overdelivered_origin_qty = overdelivered
            line.x_fulfillment_net_pct = (
                (delivered_net / origin_demand * 100.0)
                if origin_demand
                else 0.0
            )

    @api.depends(
        'product_id.type',
        'product_uom_qty',
        'qty_delivered',
        'x_returned_qty',
        'x_delivered_net_qty',
        'x_origin_demand_qty',
        'x_origin_demand_locked',
        'x_overdelivered_origin_qty',
    )
    def _compute_delivery_status(self):
        for line in self:
            if line.product_id.type == 'service':
                line.x_delivery_status = 'entregado'
                continue

            demand = line._get_delivery_base_demand_qty()
            delivered_net = line.x_delivered_net_qty or 0.0
            returned = line.x_returned_qty or 0.0
            overdelivered = line.x_overdelivered_origin_qty or 0.0

            if demand <= 0 and delivered_net <= 0:
                line.x_delivery_status = 'sin_asignar'
            elif overdelivered > 0:
                line.x_delivery_status = 'sobreentregado'
            elif returned > 0 and delivered_net < demand:
                line.x_delivery_status = 'devuelto_parcial'
            elif delivered_net <= 0 and demand > 0:
                line.x_delivery_status = 'sin_asignar'
            elif delivered_net >= demand:
                if returned > 0:
                    line.x_delivery_status = 'devuelto_parcial'
                else:
                    line.x_delivery_status = 'entregado'
            elif delivered_net > 0:
                line.x_delivery_status = 'parcial_entregado'
            else:
                line.x_delivery_status = 'sin_asignar'