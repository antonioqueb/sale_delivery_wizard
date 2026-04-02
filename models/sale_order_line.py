from odoo import api, fields, models


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    x_returned_qty = fields.Float(
        compute='_compute_return_qty',
        string='Cantidad Devuelta',
        store=True)
    x_delivered_net_qty = fields.Float(
        compute='_compute_delivery_net',
        string='Entregado Neto',
        store=True)
    x_pending_qty = fields.Float(
        compute='_compute_delivery_net',
        string='Pendiente')
    x_fulfillment_net_pct = fields.Float(
        compute='_compute_delivery_net',
        string='Fulfillment Neto %')
    x_delivery_status = fields.Selection([
        ('sin_asignar', 'Sin Asignar'),
        ('parcial_asignado', 'Parcial Asignado'),
        ('asignado', 'Asignado'),
        ('parcial_entregado', 'Parcial Entregado'),
        ('entregado', 'Entregado'),
        ('devuelto_parcial', 'Devuelto Parcial'),
        ('finiquitado', 'Finiquitado'),
    ], compute='_compute_delivery_status', string='Estado Entrega',
        store=True)

    @api.depends('move_ids.move_line_ids.quantity',
                 'move_ids.origin_returned_move_id')
    def _compute_return_qty(self):
        for line in self:
            returned = 0.0
            for move in line.move_ids:
                if (move.origin_returned_move_id
                        and move.state == 'done'
                        and move.location_dest_id.usage == 'internal'):
                    returned += move.product_uom_qty
            line.x_returned_qty = returned

    @api.depends('qty_delivered', 'x_returned_qty', 'product_uom_qty')
    def _compute_delivery_net(self):
        for line in self:
            net = line.qty_delivered - line.x_returned_qty
            line.x_delivered_net_qty = max(net, 0)
            line.x_pending_qty = max(
                line.product_uom_qty - line.x_delivered_net_qty, 0)
            line.x_fulfillment_net_pct = (
                (line.x_delivered_net_qty / line.product_uom_qty * 100)
                if line.product_uom_qty else 0.0)

    @api.depends('product_uom_qty', 'qty_delivered',
                 'x_returned_qty', 'x_delivered_net_qty')
    def _compute_delivery_status(self):
        for line in self:
            if line.product_id.type == 'service':
                line.x_delivery_status = 'entregado'
                continue
            demand = line.product_uom_qty
            delivered_net = line.x_delivered_net_qty
            returned = line.x_returned_qty

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
