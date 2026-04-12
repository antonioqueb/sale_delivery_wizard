from odoo import api, fields, models


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

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
    )
    x_fulfillment_net_pct = fields.Float(
        compute='_compute_pending_fulfillment',
        string='Fulfillment Neto %',
    )
    x_delivery_status = fields.Selection(
        [
            ('sin_asignar', 'Sin Asignar'),
            ('parcial_asignado', 'Parcial Asignado'),
            ('asignado', 'Asignado'),
            ('parcial_entregado', 'Parcial Entregado'),
            ('entregado', 'Entregado'),
            ('devuelto_parcial', 'Devuelto Parcial'),
            ('finiquitado', 'Finiquitado'),
        ],
        compute='_compute_delivery_status',
        string='Estado Entrega',
        store=True,
    )

    @api.depends(
        'move_ids.state',
        'move_ids.origin_returned_move_id',
        'move_ids.picking_id.picking_type_code',
        'move_ids.location_dest_id.usage',
        'move_ids.product_uom_qty',
    )
    def _compute_return_qty(self):
        for line in self:
            returned = 0.0
            for move in line.move_ids:
                if (
                    move.origin_returned_move_id
                    and move.state == 'done'
                    and move.location_dest_id.usage == 'internal'
                ):
                    returned += move.product_uom_qty or 0.0
            line.x_returned_qty = returned

    @api.depends('qty_delivered', 'product_uom_qty')
    def _compute_delivery_net(self):
        # En Odoo, qty_delivered ya es neto: salidas - entradas
        for line in self:
            line.x_delivered_net_qty = max(line.qty_delivered or 0.0, 0.0)

    @api.depends('x_delivered_net_qty', 'product_uom_qty')
    def _compute_pending_fulfillment(self):
        for line in self:
            demand = line.product_uom_qty or 0.0
            delivered_net = line.x_delivered_net_qty or 0.0
            line.x_pending_qty = max(demand - delivered_net, 0.0)
            line.x_fulfillment_net_pct = (
                (delivered_net / demand * 100.0) if demand else 0.0
            )

    @api.depends(
        'product_id.type',
        'product_uom_qty',
        'qty_delivered',
        'x_returned_qty',
        'x_delivered_net_qty',
    )
    def _compute_delivery_status(self):
        for line in self:
            if line.product_id.type == 'service':
                line.x_delivery_status = 'entregado'
                continue

            demand = line.product_uom_qty or 0.0
            delivered_net = line.x_delivered_net_qty or 0.0
            returned = line.x_returned_qty or 0.0

            if delivered_net <= 0 and demand > 0:
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