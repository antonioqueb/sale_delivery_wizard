from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaleDeliveryPtSelector(models.TransientModel):
    _name = 'sale.delivery.pt.selector'
    _description = 'Selector de Pick Ticket Abierto'

    sale_order_id = fields.Many2one(
        'sale.order', string='Orden de Venta', required=True, readonly=True)
    partner_id = fields.Many2one(
        related='sale_order_id.partner_id', string='Cliente', readonly=True)
    line_ids = fields.One2many(
        'sale.delivery.pt.selector.line', 'selector_id',
        string='Pick Tickets Abiertos')
    pt_count = fields.Integer(
        compute='_compute_pt_count', string='Total PTs')

    @api.depends('line_ids')
    def _compute_pt_count(self):
        for rec in self:
            rec.pt_count = len(rec.line_ids)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so_id = res.get('sale_order_id') or self.env.context.get('active_id')
        if not so_id:
            return res

        order = self.env['sale.order'].browse(so_id)
        pts = order._get_open_pick_tickets()

        res['sale_order_id'] = order.id
        res['line_ids'] = [(0, 0, {
            'pick_ticket_id': pt.id,
        }) for pt in pts]
        return res

    def action_create_new_pt(self):
        """Abre un wizard limpio para crear un nuevo Pick Ticket."""
        self.ensure_one()
        return self.sale_order_id._open_delivery_wizard_new()


class SaleDeliveryPtSelectorLine(models.TransientModel):
    _name = 'sale.delivery.pt.selector.line'
    _description = 'Línea de Selector de PT'
    _order = 'pt_create_date desc'

    selector_id = fields.Many2one(
        'sale.delivery.pt.selector', required=True, ondelete='cascade')
    pick_ticket_id = fields.Many2one(
        'sale.delivery.document', string='Pick Ticket',
        required=True, ondelete='cascade', readonly=True)
    pt_name = fields.Char(
        related='pick_ticket_id.name', string='Número', readonly=True)
    pt_create_date = fields.Datetime(
        related='pick_ticket_id.create_date', string='Creado', readonly=True)
    pt_created_by = fields.Many2one(
        related='pick_ticket_id.create_uid', string='Por', readonly=True)
    pt_total_qty = fields.Float(
        related='pick_ticket_id.total_qty', string='Total m²', readonly=True)
    pt_line_count = fields.Integer(
        compute='_compute_pt_line_count', string='Lotes')
    pt_special_instructions = fields.Text(
        related='pick_ticket_id.special_instructions',
        string='Instrucciones', readonly=True)

    @api.depends('pick_ticket_id.line_ids')
    def _compute_pt_line_count(self):
        for rec in self:
            rec.pt_line_count = len(rec.pick_ticket_id.line_ids)

    def action_edit_pt(self):
        """Abre el wizard de entrega en modo edición para este PT."""
        self.ensure_one()
        order = self.selector_id.sale_order_id
        return order._open_delivery_wizard_editing(self.pick_ticket_id.id)

    def action_print_pt(self):
        """Imprime el Pick Ticket."""
        self.ensure_one()
        return self.env.ref(
            'sale_delivery_wizard.action_report_pick_ticket'
        ).report_action(self.pick_ticket_id)

    def action_cancel_pt(self):
        """Cancela el Pick Ticket liberando sus lotes."""
        self.ensure_one()
        self.pick_ticket_id.action_cancel_pick_ticket()
        # refrescar el selector removiendo esta línea
        self.unlink()
        # reabrir el selector (o cerrarlo si ya no hay PTs)
        order = self.selector_id.sale_order_id
        remaining = order._get_open_pick_tickets()
        if not remaining:
            return {'type': 'ir.actions.act_window_close'}
        return order.action_open_delivery_wizard()