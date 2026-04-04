from odoo import api, fields, models, _
import json


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # ── Delivery documents ──
    delivery_document_ids = fields.One2many(
        'sale.delivery.document', 'sale_order_id',
        string='Documentos de Entrega')

    # ── Computed summary ──
    x_total_assigned_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Total Asignado')
    x_total_delivered_gross_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Entregado Bruto')
    x_total_returned_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Devuelto')
    x_total_delivered_net_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Entregado Neto')
    x_total_pending_delivery_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Pendiente Entrega')
    x_total_demand_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Demanda Total')
    x_fulfillment_net_pct = fields.Float(
        compute='_compute_delivery_summary',
        string='Fulfillment Neto %')

    # ── Counts ──
    x_delivery_document_count = fields.Integer(
        compute='_compute_document_counts',
        string='Documentos')
    x_remission_count = fields.Integer(
        compute='_compute_document_counts',
        string='Remisiones')
    x_return_count = fields.Integer(
        compute='_compute_document_counts',
        string='Devoluciones')
    x_pick_ticket_count = fields.Integer(
        compute='_compute_document_counts',
        string='Pick Tickets')
    x_redelivery_count = fields.Integer(
        compute='_compute_document_counts',
        string='Reentregas')
    x_redelivery_pending_count = fields.Integer(
        compute='_compute_document_counts',
        string='Reentregas Pendientes')

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
            delivered_gross = sum(lines.mapped('qty_delivered'))
            returned = sum(lines.mapped('x_returned_qty'))
            delivered_net = delivered_gross - returned
            pending = demand - delivered_net

            order.x_total_demand_qty = demand
            order.x_total_assigned_qty = assigned
            order.x_total_delivered_gross_qty = delivered_gross
            order.x_total_returned_qty = returned
            order.x_total_delivered_net_qty = max(delivered_net, 0)
            order.x_total_pending_delivery_qty = max(pending, 0)
            order.x_fulfillment_net_pct = (
                (delivered_net / demand * 100) if demand else 0.0)

    @api.depends('delivery_document_ids',
                 'delivery_document_ids.document_type',
                 'delivery_document_ids.state')
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
                    and d.state in ('draft', 'prepared')))

    # ── Action buttons ──

    def action_open_delivery_wizard(self):
        """Open the delivery wizard from the sale order."""
        self.ensure_one()
        if self.state not in ('sale', 'done'):
            from odoo.exceptions import UserError
            raise UserError(_(
                'Solo puede entregar pedidos confirmados.'))
        if hasattr(self, 'delivery_auth_state'):
            if self.delivery_auth_state == 'pending':
                if not self.env.user.has_group(
                        'sale_delivery_wizard.group_delivery_authorizer'):
                    from odoo.exceptions import UserError
                    raise UserError(_(
                        'Este pedido no tiene autorización de entrega. '
                        'Contacte a un autorizador.'))
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
        """Open the return wizard from the sale order."""
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
        """Open swap wizard."""
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
        """View all delivery documents for this order."""
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
            'domain': [
                ('sale_order_id', '=', self.id),
                ('document_type', '=', 'remission'),
            ],
        }

    def action_view_returns(self):
        self.ensure_one()
        return {
            'name': _('Devoluciones'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [
                ('sale_order_id', '=', self.id),
                ('document_type', '=', 'return'),
            ],
        }

    def action_view_pick_tickets(self):
        self.ensure_one()
        return {
            'name': _('Pick Tickets'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [
                ('sale_order_id', '=', self.id),
                ('document_type', '=', 'pick_ticket'),
            ],
        }

    def action_view_redeliveries(self):
        self.ensure_one()
        return {
            'name': _('Reentregas'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [
                ('sale_order_id', '=', self.id),
                ('document_type', '=', 'redelivery'),
            ],
        }