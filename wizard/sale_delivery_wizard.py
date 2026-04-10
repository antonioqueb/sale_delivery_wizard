from collections import OrderedDict
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SaleDeliveryWizard(models.TransientModel):
    _name = 'sale.delivery.wizard'
    _description = 'Wizard de Entrega desde Orden de Venta'

    sale_order_id = fields.Many2one(
        'sale.order', string='Orden de Venta', required=True)
    partner_id = fields.Many2one(
        related='sale_order_id.partner_id', string='Cliente')
    delivery_address = fields.Text(string='Dirección de Entrega')
    special_instructions = fields.Text(string='Instrucciones Especiales')

    wizard_state = fields.Selection([
        ('select', 'Selección'),
        ('pick_ticket', 'Pick Ticket Generado'),
    ], default='select', string='Paso')

    line_ids = fields.One2many(
        'sale.delivery.wizard.line', 'wizard_id', string='Líneas')

    total_selected = fields.Float(
        compute='_compute_totals', string='Total Seleccionado')
    total_available = fields.Float(
        compute='_compute_totals', string='Total Disponible')

    pick_ticket_id = fields.Many2one(
        'sale.delivery.document', string='Pick Ticket')

    @api.depends('line_ids.qty_to_deliver', 'line_ids.is_selected',
                 'line_ids.display_type')
    def _compute_totals(self):
        for wiz in self:
            selected_lines = wiz.line_ids.filtered(
                lambda l: l.is_selected and l.display_type != 'line_section')
            wiz.total_selected = sum(selected_lines.mapped('qty_to_deliver'))

            data_lines = wiz.line_ids.filtered(
                lambda l: l.display_type != 'line_section')
            wiz.total_available = sum(data_lines.mapped('qty_available'))

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so_id = res.get('sale_order_id') or self.env.context.get('active_id')
        if not so_id:
            return res

        order = self.env['sale.order'].browse(so_id)
        res.update(self._prepare_default_wizard_vals(order))
        return res

    @api.model_create_multi
    def create(self, vals_list):
        """Blindaje para Odoo 19:
        si por alguna razón el wizard se crea sin line_ids persistidas,
        las reconstruimos en create para que el widget pueda leerlas por ORM.
        """
        records = super().create(vals_list)
        for rec in records:
            if rec.sale_order_id and not rec.line_ids:
                rec._rebuild_lines_from_source()
        return records

    def _get_pending_pick_ticket(self, order):
        return self.env['sale.delivery.document'].search([
            ('sale_order_id', '=', order.id),
            ('document_type', '=', 'pick_ticket'),
            ('state', '=', 'prepared'),
        ], order='create_date desc', limit=1)

    def _prepare_default_wizard_vals(self, order):
        delivery_address = order.partner_shipping_id.contact_address or ''
        pending_pt = self._get_pending_pick_ticket(order)

        vals = {
            'sale_order_id': order.id,
            'delivery_address': delivery_address,
        }

        if pending_pt:
            vals.update({
                'pick_ticket_id': pending_pt.id,
                'wizard_state': 'pick_ticket',
                'delivery_address': pending_pt.delivery_address or delivery_address,
                'special_instructions': pending_pt.special_instructions or '',
                'line_ids': self._build_lines_from_pick_ticket(order, pending_pt),
            })
        else:
            vals.update({
                'wizard_state': 'select',
                'line_ids': self._build_lines_from_pickings(order),
            })

        return vals

    def _rebuild_lines_from_source(self):
        self.ensure_one()
        vals = self._prepare_default_wizard_vals(self.sale_order_id)
        self.write({
            'delivery_address': vals.get('delivery_address', self.delivery_address),
            'special_instructions': vals.get('special_instructions', self.special_instructions),
            'wizard_state': vals.get('wizard_state', self.wizard_state),
            'pick_ticket_id': vals.get('pick_ticket_id', False),
            'line_ids': [(5, 0, 0)] + vals.get('line_ids', []),
        })

    def _group_lines_by_product(self, raw_lines):
        """Inserta headers tipo section agrupando por producto."""
        grouped = OrderedDict()
        for cmd in raw_lines:
            vals = cmd[2]
            pid = vals.get('product_id', 0)
            grouped.setdefault(pid, []).append(cmd)

        result = []
        Product = self.env['product.product']
        for pid, lines in grouped.items():
            product = Product.browse(pid) if pid else None
            section_name = product.display_name if product else _('Sin Producto')

            result.append((0, 0, {
                'display_type': 'line_section',
                'section_name': section_name,
                'product_id': pid,
                'sequence': len(result) * 10,
            }))

            for line_cmd in lines:
                line_cmd[2]['sequence'] = len(result) * 10
                result.append(line_cmd)

        return result

    def _build_lines_from_pickings(self, order):
        """Arma líneas desde pickings pendientes.
        Siempre fuerza datos sólidos para que el widget no reciba filas huecas.
        """
        raw_lines = []

        pickings = order.picking_ids.filtered(
            lambda p: p.state in ('assigned', 'confirmed')
        )

        for picking in pickings:
            moves = picking.move_ids.filtered(
                lambda m: m.state in ('assigned', 'confirmed')
            )

            for move in moves:
                if move.move_line_ids:
                    for ml in move.move_line_ids:
                        qty_avail = (
                            ml.quantity
                            or getattr(ml, 'product_uom_qty', 0.0)
                            or move.product_uom_qty
                            or 0.0
                        )

                        raw_lines.append((0, 0, {
                            'picking_id': picking.id,
                            'move_id': move.id,
                            'move_line_id': ml.id,
                            'sale_line_id': move.sale_line_id.id,
                            'product_id': move.product_id.id,
                            'lot_id': ml.lot_id.id if ml.lot_id else False,
                            'qty_available': qty_avail,
                            'qty_to_deliver': qty_avail,
                            'is_selected': qty_avail > 0,
                            'source_location_id': ml.location_id.id if ml.location_id else False,
                        }))
                else:
                    qty_avail = move.product_uom_qty or 0.0
                    raw_lines.append((0, 0, {
                        'picking_id': picking.id,
                        'move_id': move.id,
                        'sale_line_id': move.sale_line_id.id,
                        'product_id': move.product_id.id,
                        'qty_available': qty_avail,
                        'qty_to_deliver': qty_avail,
                        'is_selected': qty_avail > 0,
                    }))

        return self._group_lines_by_product(raw_lines)

    def _build_lines_from_pick_ticket(self, order, pt):
        """Arma líneas desde pickings, seleccionando sólo lo del pick ticket."""
        pt_lookup = {}
        for pt_line in pt.line_ids:
            key = (pt_line.move_line_id.id, pt_line.lot_id.id if pt_line.lot_id else False)
            pt_lookup[key] = pt_line.qty_selected

        raw_lines = []

        pickings = order.picking_ids.filtered(
            lambda p: p.state in ('assigned', 'confirmed')
        )

        for picking in pickings:
            moves = picking.move_ids.filtered(
                lambda m: m.state in ('assigned', 'confirmed')
            )

            for move in moves:
                if move.move_line_ids:
                    for ml in move.move_line_ids:
                        qty_avail = (
                            ml.quantity
                            or getattr(ml, 'product_uom_qty', 0.0)
                            or move.product_uom_qty
                            or 0.0
                        )

                        key = (ml.id, ml.lot_id.id if ml.lot_id else False)
                        pt_qty = pt_lookup.get(key, 0.0)
                        is_in_pt = pt_qty > 0

                        raw_lines.append((0, 0, {
                            'picking_id': picking.id,
                            'move_id': move.id,
                            'move_line_id': ml.id,
                            'sale_line_id': move.sale_line_id.id,
                            'product_id': move.product_id.id,
                            'lot_id': ml.lot_id.id if ml.lot_id else False,
                            'qty_available': qty_avail,
                            'qty_to_deliver': pt_qty if is_in_pt else 0.0,
                            'is_selected': is_in_pt,
                            'source_location_id': ml.location_id.id if ml.location_id else False,
                        }))
                else:
                    qty_avail = move.product_uom_qty or 0.0
                    raw_lines.append((0, 0, {
                        'picking_id': picking.id,
                        'move_id': move.id,
                        'sale_line_id': move.sale_line_id.id,
                        'product_id': move.product_id.id,
                        'qty_available': qty_avail,
                        'qty_to_deliver': 0.0,
                        'is_selected': False,
                    }))

        return self._group_lines_by_product(raw_lines)

    def _ensure_qty_on_selected(self):
        """Safety net extra para evitar qty en 0 al guardar."""
        for line in self.line_ids.filtered(
                lambda l: l.is_selected and l.display_type != 'line_section'):
            if line.qty_available <= 0 and line.move_line_id:
                line.qty_available = (
                    line.move_line_id.quantity
                    or getattr(line.move_line_id, 'product_uom_qty', 0.0)
                    or 0.0
                )
            if line.qty_available <= 0 and line.move_id:
                line.qty_available = line.move_id.product_uom_qty or 0.0
            if line.qty_to_deliver <= 0:
                line.qty_to_deliver = line.qty_available

    def _get_selected_lines(self):
        self._ensure_qty_on_selected()
        selected = self.line_ids.filtered(
            lambda l: l.is_selected and l.display_type != 'line_section')

        if not selected:
            raise UserError(_('Seleccione al menos una línea.'))

        for line in selected:
            if line.qty_to_deliver <= 0:
                raise UserError(_(
                    'La cantidad a entregar debe ser mayor a 0 para %s.',
                    line.product_id.display_name))

        return selected

    def action_select_all(self):
        for line in self.line_ids.filtered(
                lambda l: l.display_type != 'line_section'):
            line.is_selected = True
            if line.qty_available > 0 and line.qty_to_deliver <= 0:
                line.qty_to_deliver = line.qty_available
        return self._refresh()

    def action_deselect_all(self):
        for line in self.line_ids.filtered(
                lambda l: l.display_type != 'line_section'):
            line.is_selected = False
            line.qty_to_deliver = 0.0
        return self._refresh()

    def action_generate_pick_ticket(self):
        self.ensure_one()
        selected = self._get_selected_lines()

        doc = self.env['sale.delivery.document'].create({
            'document_type': 'pick_ticket',
            'sale_order_id': self.sale_order_id.id,
            'delivery_address': self.delivery_address,
            'special_instructions': self.special_instructions,
            'line_ids': [(0, 0, {
                'sale_line_id': line.sale_line_id.id,
                'move_id': line.move_id.id,
                'move_line_id': line.move_line_id.id,
                'product_id': line.product_id.id,
                'lot_id': line.lot_id.id,
                'qty_selected': line.qty_to_deliver,
                'source_location_id': line.source_location_id.id,
            }) for line in selected],
        })
        doc.action_prepare()

        return self.env.ref(
            'sale_delivery_wizard.action_report_pick_ticket'
        ).report_action(doc)

    def action_print_pick_ticket(self):
        self.ensure_one()
        if not self.pick_ticket_id:
            raise UserError(_('No hay Pick Ticket para imprimir.'))
        return self.env.ref(
            'sale_delivery_wizard.action_report_pick_ticket'
        ).report_action(self.pick_ticket_id)

    def action_generate_remission(self):
        self.ensure_one()
        selected = self._get_selected_lines()

        for line in selected:
            if line.qty_to_deliver > line.qty_available:
                raise UserError(_(
                    'No puede entregar más de lo disponible para %s. '
                    'Disponible: %s, Solicitado: %s',
                    line.product_id.display_name,
                    line.qty_available, line.qty_to_deliver))

        order = self.sale_order_id
        if hasattr(order, 'delivery_auth_state'):
            if order.delivery_auth_state == 'pending':
                if not self.env.user.has_group(
                        'sale_delivery_wizard.group_delivery_authorizer'):
                    raise UserError(_(
                        'Entrega bloqueada: pedido sin autorización de pago.'))

        picking_lines = {}
        for line in selected:
            picking_lines.setdefault(line.picking_id, []).append(line)

        docs = self.env['sale.delivery.document']
        for picking, lines in picking_lines.items():
            doc = self.env['sale.delivery.document'].create({
                'document_type': 'remission',
                'sale_order_id': order.id,
                'picking_id': picking.id,
                'delivery_address': self.delivery_address,
                'special_instructions': self.special_instructions,
                'line_ids': [(0, 0, {
                    'sale_line_id': line.sale_line_id.id,
                    'move_id': line.move_id.id,
                    'move_line_id': line.move_line_id.id,
                    'product_id': line.product_id.id,
                    'lot_id': line.lot_id.id,
                    'qty_selected': line.qty_to_deliver,
                    'qty_done': line.qty_to_deliver,
                    'source_location_id': line.source_location_id.id,
                }) for line in lines],
            })
            if self.pick_ticket_id:
                doc.message_post(body=_(
                    'Remisión generada desde Pick Ticket: %s',
                    self.pick_ticket_id.name))
                self.pick_ticket_id.state = 'confirmed'
            doc.action_confirm()
            docs |= doc

        if len(docs) == 1:
            return self.env.ref(
                'sale_delivery_wizard.action_report_remission'
            ).report_action(docs)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Remisiones Generadas'),
                'message': _('%d remisiones creadas exitosamente.') % len(docs),
                'type': 'success',
                'sticky': False,
            },
        }

    def _refresh(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }


class SaleDeliveryWizardLine(models.TransientModel):
    _name = 'sale.delivery.wizard.line'
    _description = 'Línea de Wizard de Entrega'
    _order = 'sequence, id'

    wizard_id = fields.Many2one(
        'sale.delivery.wizard', ondelete='cascade', required=True)
    sequence = fields.Integer(default=10)

    display_type = fields.Selection([
        ('line_section', 'Section'),
    ], string='Tipo de Fila')
    section_name = fields.Char(string='Nombre de Sección')

    is_selected = fields.Boolean(string='Sel.', default=False)

    picking_id = fields.Many2one('stock.picking', string='Picking')
    move_id = fields.Many2one('stock.move', string='Move')
    move_line_id = fields.Many2one('stock.move.line', string='Move Line')
    sale_line_id = fields.Many2one(
        'sale.order.line', string='Línea de Venta')
    product_id = fields.Many2one(
        'product.product', string='Producto')
    lot_id = fields.Many2one('stock.lot', string='Lote/Placa')
    source_location_id = fields.Many2one(
        'stock.location', string='Ubicación')

    qty_available = fields.Float(string='Disponible')
    qty_to_deliver = fields.Float(string='A Entregar')

    lot_name = fields.Char(related='lot_id.name', string='# Lote')
    product_name = fields.Char(
        related='product_id.display_name', string='Producto Desc.')

    @api.onchange('is_selected')
    def _onchange_is_selected(self):
        if self.display_type == 'line_section':
            return

        if self.is_selected and self.qty_to_deliver <= 0:
            self.qty_to_deliver = self.qty_available
        elif not self.is_selected:
            self.qty_to_deliver = 0.0

    @api.onchange('qty_to_deliver')
    def _onchange_qty_to_deliver(self):
        if self.display_type == 'line_section':
            return

        if self.qty_to_deliver > 0:
            self.is_selected = True

        if self.qty_to_deliver > self.qty_available:
            return {'warning': {
                'title': _('Cantidad excedida'),
                'message': _(
                    'La cantidad a entregar excede lo disponible (%s).',
                    self.qty_available),
            }}