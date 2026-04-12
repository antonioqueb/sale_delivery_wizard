from collections import OrderedDict
import json
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

    widget_selections = fields.Text(
        string='Selecciones del Widget', default='[]')

    @api.depends(
        'widget_selections',
        'line_ids.qty_to_deliver',
        'line_ids.qty_available',
        'line_ids.is_selected',
        'line_ids.display_type',
    )
    def _compute_totals(self):
        for wiz in self:
            total_selected = 0.0
            data_lines = wiz.line_ids.filtered(
                lambda l: l.display_type != 'line_section'
            )
            total_available = sum(data_lines.mapped('qty_available'))

            try:
                sels = json.loads(wiz.widget_selections or '[]')
                if isinstance(sels, list):
                    total_selected = sum(
                        float(s.get('qty', 0) or 0.0) for s in sels
                    )
            except (json.JSONDecodeError, TypeError, ValueError):
                sels = []

            if not total_selected and data_lines:
                selected_lines = data_lines.filtered(lambda l: l.is_selected)
                total_selected = sum(selected_lines.mapped('qty_to_deliver'))

            wiz.total_selected = total_selected
            wiz.total_available = total_available

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so_id = res.get('sale_order_id') or self.env.context.get('active_id')
        if not so_id:
            return res

        order = self.env['sale.order'].browse(so_id)
        res.update(self._prepare_default_wizard_vals(order))
        return res

    def _prepare_default_wizard_vals(self, order):
        delivery_address = order.partner_shipping_id.contact_address or ''
        pending_pt = self.env['sale.delivery.document'].search([
            ('sale_order_id', '=', order.id),
            ('document_type', '=', 'pick_ticket'),
            ('state', '=', 'prepared'),
        ], order='create_date desc', limit=1)

        vals = {
            'sale_order_id': order.id,
            'delivery_address': delivery_address,
            'special_instructions': '',
            'widget_selections': '[]',
        }

        grouped = order.get_delivery_grouped_data(mode='delivery') or []

        if pending_pt and pending_pt.line_ids:
            # ── Restore selection from existing Pick Ticket ──
            # Build lookup: (product_id, lot_id) → qty from PT
            pt_line_map = {}
            for pt_line in pending_pt.line_ids:
                key = (
                    pt_line.product_id.id,
                    pt_line.lot_id.id if pt_line.lot_id else 0,
                )
                pt_line_map[key] = {
                    'qty': pt_line.qty_selected,
                    'saleLineId': pt_line.sale_line_id.id if pt_line.sale_line_id else 0,
                    'moveId': pt_line.move_id.id if pt_line.move_id else 0,
                    'moveLineId': pt_line.move_line_id.id if pt_line.move_line_id else 0,
                    'sourceLocationId': pt_line.source_location_id.id if pt_line.source_location_id else 0,
                }

            # Apply PT selection to grouped lines
            widget_sels = []
            for group in grouped:
                for line in group.get('lines', []):
                    key = (
                        line.get('productId', 0),
                        line.get('lotId', 0),
                    )
                    pt_info = pt_line_map.get(key)
                    if pt_info:
                        line['isSelected'] = True
                        line['qtyToDeliver'] = pt_info['qty']
                        widget_sels.append({
                            'dbId': 0,
                            'lotId': line.get('lotId', 0),
                            'productId': line.get('productId', 0),
                            'pickingId': line.get('pickingId', 0),
                            'moveId': line.get('moveId', 0) or pt_info.get('moveId', 0),
                            'moveLineId': line.get('moveLineId', 0) or pt_info.get('moveLineId', 0),
                            'saleLineId': line.get('saleLineId', 0) or pt_info.get('saleLineId', 0),
                            'sourceLocationId': line.get('sourceLocationId', 0) or pt_info.get('sourceLocationId', 0),
                            'qty': pt_info['qty'],
                            'qtyAvailable': line.get('qtyAvailable', 0),
                        })
                    else:
                        line['isSelected'] = False
                        line['qtyToDeliver'] = 0

            vals['widget_selections'] = json.dumps(widget_sels)
            vals['wizard_state'] = 'pick_ticket'
            vals['pick_ticket_id'] = pending_pt.id

        vals['line_ids'] = self._groups_to_line_commands(grouped)
        return vals

    def _groups_to_line_commands(self, groups):
        commands = []
        sequence = 10

        for group in groups:
            product_name = group.get('productName') or 'Producto'
            commands.append((0, 0, {
                'sequence': sequence,
                'display_type': 'line_section',
                'name': product_name,
            }))
            sequence += 1

            for line in group.get('lines', []):
                commands.append((0, 0, {
                    'sequence': sequence,
                    'name': line.get('lotName') or product_name,
                    'product_id': line.get('productId') or False,
                    'lot_id': line.get('lotId') or False,
                    'picking_id': line.get('pickingId') or False,
                    'move_id': line.get('moveId') or False,
                    'move_line_id': line.get('moveLineId') or False,
                    'sale_line_id': line.get('saleLineId') or False,
                    'source_location_id': line.get('sourceLocationId') or False,
                    'qty_available': line.get('qtyAvailable') or 0.0,
                    'qty_to_deliver': line.get('qtyToDeliver') or 0.0,
                    'is_selected': bool(line.get('isSelected')),
                }))
                sequence += 1

        return commands

    def get_grouped_lines_data(self):
        self.ensure_one()

        try:
            selections = json.loads(self.widget_selections or '[]')
        except (json.JSONDecodeError, TypeError):
            selections = []

        selected_map = {}
        for s in selections or []:
            key = (
                int(s.get('moveLineId') or 0),
                int(s.get('lotId') or 0),
                int(s.get('moveId') or 0),
                int(s.get('pickingId') or 0),
            )
            selected_map[key] = {
                'qty': float(s.get('qty', 0) or 0.0),
                'sourceLocationId': int(s.get('sourceLocationId') or 0),
            }

        groups_map = OrderedDict()

        for line in self.line_ids.sorted(key=lambda l: (l.sequence, l.id)):
            if line.display_type == 'line_section':
                continue
            if not line.product_id:
                continue

            pid = line.product_id.id
            pname = line.product_id.display_name

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

            key = (
                line.move_line_id.id if line.move_line_id else 0,
                line.lot_id.id if line.lot_id else 0,
                line.move_id.id if line.move_id else 0,
                line.picking_id.id if line.picking_id else 0,
            )

            if key in selected_map:
                is_selected = selected_map[key]['qty'] > 0
                qty_to_deliver = selected_map[key]['qty']
                source_location_id = selected_map[key]['sourceLocationId'] or (
                    line.source_location_id.id if line.source_location_id else 0
                )
            else:
                is_selected = bool(line.is_selected)
                qty_to_deliver = line.qty_to_deliver or 0.0
                source_location_id = line.source_location_id.id if line.source_location_id else 0

            data = {
                'dbId': line.id,
                'lotId': line.lot_id.id if line.lot_id else 0,
                'lotName': line.lot_id.name if line.lot_id else (line.name or ''),
                'productId': pid,
                'productName': pname,
                'pickingId': line.picking_id.id if line.picking_id else 0,
                'moveId': line.move_id.id if line.move_id else 0,
                'moveLineId': line.move_line_id.id if line.move_line_id else 0,
                'saleLineId': line.sale_line_id.id if line.sale_line_id else 0,
                'sourceLocationId': source_location_id,
                'sourceLocation': line.source_location_id.display_name if line.source_location_id else '',
                'isSelected': is_selected,
                'qtyAvailable': line.qty_available or 0.0,
                'qtyToDeliver': qty_to_deliver,
            }

            group['lines'].append(data)
            group['lineCount'] += 1
            group['totalQty'] += qty_to_deliver
            if is_selected:
                group['selectedCount'] += 1

        return [g for g in groups_map.values() if g['lineCount'] > 0]

    def _refresh(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _get_selected_lines(self):
        self.ensure_one()
        selected = self.line_ids.filtered(
            lambda l: l.display_type != 'line_section'
            and l.is_selected
            and l.qty_to_deliver > 0
        )
        if not selected:
            raise UserError(_('Seleccione al menos una línea.'))
        return selected

    def action_select_all(self):
        self.ensure_one()
        for line in self.line_ids.filtered(
                lambda l: l.display_type != 'line_section'):
            line.is_selected = True
            line.qty_to_deliver = line.qty_available or 0.0
        return self._refresh()

    def action_deselect_all(self):
        self.ensure_one()
        for line in self.line_ids.filtered(
                lambda l: l.display_type != 'line_section'):
            line.is_selected = False
            line.qty_to_deliver = 0.0
        return self._refresh()

    # ═══════════════════════════════════════════════════════════════════
    # PICK TICKET
    # ═══════════════════════════════════════════════════════════════════

    def _cancel_previous_pick_tickets(self):
        """Cancel ALL previous prepared Pick Tickets for this order."""
        old_pts = self.env['sale.delivery.document'].search([
            ('sale_order_id', '=', self.sale_order_id.id),
            ('document_type', '=', 'pick_ticket'),
            ('state', '=', 'prepared'),
        ])
        if old_pts:
            old_pts.write({'state': 'cancelled'})
            _logger.info(
                'Cancelled %d old pick ticket(s) for SO %s: %s',
                len(old_pts), self.sale_order_id.name,
                ', '.join(old_pts.mapped('name')))

    def action_generate_pick_ticket(self):
        self.ensure_one()

        # Always cancel previous prepared PTs
        self._cancel_previous_pick_tickets()

        try:
            sels = json.loads(self.widget_selections or '[]')
        except (json.JSONDecodeError, TypeError):
            sels = []

        if sels:
            return self._generate_pick_ticket_from_selections(sels)
        return self._generate_pick_ticket_from_lines()

    def _generate_pick_ticket_from_selections(self, sels):
        doc_lines = []
        for sel in sels:
            if sel.get('qty', 0) <= 0:
                continue
            doc_lines.append((0, 0, {
                'sale_line_id': sel.get('saleLineId') or False,
                'move_id': sel.get('moveId') or False,
                'move_line_id': sel.get('moveLineId') or False,
                'product_id': sel.get('productId'),
                'lot_id': sel.get('lotId') or False,
                'qty_selected': sel.get('qty', 0),
                'source_location_id': sel.get('sourceLocationId') or False,
            }))

        if not doc_lines:
            raise UserError(_('Seleccione al menos una línea.'))

        doc = self.env['sale.delivery.document'].create({
            'document_type': 'pick_ticket',
            'sale_order_id': self.sale_order_id.id,
            'delivery_address': self.delivery_address,
            'special_instructions': self.special_instructions,
            'line_ids': doc_lines,
        })
        doc.action_prepare()
        self.pick_ticket_id = doc.id
        self.wizard_state = 'pick_ticket'
        return self.env.ref(
            'sale_delivery_wizard.action_report_pick_ticket'
        ).report_action(doc)

    def _generate_pick_ticket_from_lines(self):
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
        self.pick_ticket_id = doc.id
        self.wizard_state = 'pick_ticket'
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

    # ═══════════════════════════════════════════════════════════════════
    # REMISIÓN
    # ═══════════════════════════════════════════════════════════════════

    def action_generate_remission(self):
        self.ensure_one()

        # Si hay Pick Ticket preparado, usar sus líneas (última verdad)
        if (self.pick_ticket_id
                and self.pick_ticket_id.state == 'prepared'
                and self.pick_ticket_id.line_ids):
            return self._generate_remission_from_pick_ticket()

        # Fallback: widget_selections o líneas del wizard
        try:
            sels = json.loads(self.widget_selections or '[]')
        except (json.JSONDecodeError, TypeError):
            sels = []

        if sels:
            return self._generate_remission_from_selections(sels)
        return self._generate_remission_from_lines()

    def _generate_remission_from_pick_ticket(self):
        """Generate remission based on Pick Ticket lines (source of truth)."""
        pt = self.pick_ticket_id
        order = self.sale_order_id

        if hasattr(order, 'delivery_auth_state') and order.delivery_auth_state == 'pending':
            if not self.env.user.has_group(
                    'sale_delivery_wizard.group_delivery_authorizer'):
                raise UserError(_(
                    'Entrega bloqueada: pedido sin autorización de pago.'))

        # Build selections from PT lines
        sels = []
        for pt_line in pt.line_ids:
            if pt_line.qty_selected <= 0:
                continue

            # Resolve picking from move
            picking_id = 0
            if pt_line.move_id and pt_line.move_id.picking_id:
                picking_id = pt_line.move_id.picking_id.id
            elif pt_line.move_line_id and pt_line.move_line_id.picking_id:
                picking_id = pt_line.move_line_id.picking_id.id

            sels.append({
                'saleLineId': pt_line.sale_line_id.id if pt_line.sale_line_id else False,
                'moveId': pt_line.move_id.id if pt_line.move_id else False,
                'moveLineId': pt_line.move_line_id.id if pt_line.move_line_id else False,
                'productId': pt_line.product_id.id,
                'lotId': pt_line.lot_id.id if pt_line.lot_id else False,
                'qty': pt_line.qty_selected,
                'sourceLocationId': pt_line.source_location_id.id if pt_line.source_location_id else False,
                'pickingId': picking_id,
            })

        if not sels:
            raise UserError(_('El Pick Ticket no tiene líneas válidas.'))

        _logger.info(
            'Generating remission from Pick Ticket %s with %d lines',
            pt.name, len(sels))

        return self._generate_remission_from_selections(sels)

    def _generate_remission_from_selections(self, sels):
        order = self.sale_order_id

        if hasattr(order, 'delivery_auth_state') and order.delivery_auth_state == 'pending':
            if not self.env.user.has_group(
                    'sale_delivery_wizard.group_delivery_authorizer'):
                raise UserError(_(
                    'Entrega bloqueada: pedido sin autorización de pago.'))

        picking_sels = {}
        for sel in sels:
            if sel.get('qty', 0) <= 0:
                continue
            picking_id = sel.get('pickingId', 0)
            picking_sels.setdefault(picking_id, []).append(sel)

        if not picking_sels:
            raise UserError(_('Seleccione al menos una línea.'))

        docs = self.env['sale.delivery.document']
        for picking_id, sel_lines in picking_sels.items():
            picking = self.env['stock.picking'].browse(picking_id) if picking_id else False
            doc = self.env['sale.delivery.document'].create({
                'document_type': 'remission',
                'sale_order_id': order.id,
                'picking_id': picking.id if picking else False,
                'delivery_address': self.delivery_address,
                'special_instructions': self.special_instructions,
                'line_ids': [(0, 0, {
                    'sale_line_id': sel.get('saleLineId') or False,
                    'move_id': sel.get('moveId') or False,
                    'move_line_id': sel.get('moveLineId') or False,
                    'product_id': sel.get('productId'),
                    'lot_id': sel.get('lotId') or False,
                    'qty_selected': sel.get('qty', 0),
                    'qty_done': sel.get('qty', 0),
                    'source_location_id': sel.get('sourceLocationId') or False,
                }) for sel in sel_lines if sel.get('qty', 0) > 0],
            })
            doc.action_confirm()
            docs |= doc

        # Mark PT as confirmed if remission was generated from it
        if self.pick_ticket_id and self.pick_ticket_id.state == 'prepared':
            self.pick_ticket_id.write({'state': 'confirmed'})

        action = {
            'type': 'ir.actions.act_window',
            'name': _('Remisiones'),
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [('id', 'in', docs.ids)],
            'target': 'current',
        }
        if len(docs) == 1:
            action.update({
                'view_mode': 'form',
                'res_id': docs.id,
            })
        return action

    def _generate_remission_from_lines(self):
        order = self.sale_order_id

        if hasattr(order, 'delivery_auth_state') and order.delivery_auth_state == 'pending':
            if not self.env.user.has_group(
                    'sale_delivery_wizard.group_delivery_authorizer'):
                raise UserError(_(
                    'Entrega bloqueada: pedido sin autorización de pago.'))

        selected = self._get_selected_lines()

        picking_lines = {}
        for line in selected:
            picking_id = line.picking_id.id if line.picking_id else 0
            picking_lines.setdefault(picking_id, self.env['sale.delivery.wizard.line'])
            picking_lines[picking_id] |= line

        docs = self.env['sale.delivery.document']
        for picking_id, lines in picking_lines.items():
            picking = self.env['stock.picking'].browse(picking_id) if picking_id else False
            doc = self.env['sale.delivery.document'].create({
                'document_type': 'remission',
                'sale_order_id': order.id,
                'picking_id': picking.id if picking else False,
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
            doc.action_confirm()
            docs |= doc

        action = {
            'type': 'ir.actions.act_window',
            'name': _('Remisiones'),
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [('id', 'in', docs.ids)],
            'target': 'current',
        }
        if len(docs) == 1:
            action.update({
                'view_mode': 'form',
                'res_id': docs.id,
            })
        return action


class SaleDeliveryWizardLine(models.TransientModel):
    _name = 'sale.delivery.wizard.line'
    _description = 'Línea del Wizard de Entrega'
    _order = 'sequence, id'

    wizard_id = fields.Many2one(
        'sale.delivery.wizard', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)

    display_type = fields.Selection([
        ('line_section', 'Sección'),
    ], default=False)

    name = fields.Char(string='Descripción')

    sale_line_id = fields.Many2one('sale.order.line', string='Línea de Venta')
    picking_id = fields.Many2one('stock.picking', string='Picking')
    move_id = fields.Many2one('stock.move', string='Movimiento')
    move_line_id = fields.Many2one('stock.move.line', string='Línea Movimiento')
    product_id = fields.Many2one('product.product', string='Producto')
    lot_id = fields.Many2one('stock.lot', string='Lote')
    source_location_id = fields.Many2one('stock.location', string='Ubicación Origen')

    qty_available = fields.Float(string='Cantidad Disponible')
    qty_to_deliver = fields.Float(string='Cantidad a Entregar')
    is_selected = fields.Boolean(string='Seleccionado')