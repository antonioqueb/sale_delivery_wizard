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

    # ── JSON field written by JS widget with selections ──
    widget_selections = fields.Text(
        string='Selecciones del Widget', default='[]')

    @api.depends('line_ids.qty_to_deliver', 'line_ids.is_selected',
                 'line_ids.display_type')
    def _compute_totals(self):
        for wiz in self:
            # Try to compute from widget_selections first (more reliable)
            try:
                sels = json.loads(wiz.widget_selections or '[]')
                if sels:
                    wiz.total_selected = sum(s.get('qty', 0) for s in sels)
                    wiz.total_available = sum(
                        l.qty_available for l in wiz.line_ids
                        if l.display_type != 'line_section')
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
            # Fallback to line_ids
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

    # ─── RPC for grouped list widget ─────────────────────────────────
    def get_grouped_lines_data(self):
        """Return line data grouped by product for the JS widget."""
        self.ensure_one()
        groups = []
        current_group = None

        for line in self.line_ids.sorted(lambda l: (l.sequence, l.id)):
            if line.display_type == 'line_section':
                if current_group and current_group['lines']:
                    groups.append(current_group)
                current_group = {
                    'productId': line.product_id.id or 0,
                    'productName': line.section_name or (
                        line.product_id.display_name if line.product_id else 'Sin Producto'),
                    'lines': [],
                    'totalQty': 0,
                    'selectedCount': 0,
                    'lineCount': 0,
                }
                continue

            if current_group is None:
                pname = line.product_id.display_name if line.product_id else 'Sin Producto'
                current_group = {
                    'productId': line.product_id.id or 0,
                    'productName': pname,
                    'lines': [],
                    'totalQty': 0,
                    'selectedCount': 0,
                    'lineCount': 0,
                }

            ld = {
                'dbId': line.id,
                'lotId': line.lot_id.id if line.lot_id else 0,
                'lotName': line.lot_id.name if line.lot_id else '',
                'productId': line.product_id.id if line.product_id else 0,
                'productName': line.product_id.display_name if line.product_id else '',
                'isSelected': line.is_selected,
                'qtyAvailable': line.qty_available,
                'qtyToDeliver': line.qty_to_deliver,
                'sourceLocation': (
                    line.source_location_id.display_name
                    if line.source_location_id else ''),
                'sourceLocationId': line.source_location_id.id if line.source_location_id else 0,
                'pickingId': line.picking_id.id if line.picking_id else 0,
                'moveId': line.move_id.id if line.move_id else 0,
                'moveLineId': line.move_line_id.id if line.move_line_id else 0,
                'saleLineId': line.sale_line_id.id if line.sale_line_id else 0,
            }
            current_group['lines'].append(ld)
            current_group['lineCount'] += 1
            current_group['totalQty'] += line.qty_to_deliver or 0
            if line.is_selected:
                current_group['selectedCount'] += 1

        if current_group and current_group['lines']:
            groups.append(current_group)

        return groups

    # ─── Line building ───────────────────────────────────────────────

    def _group_lines_by_product(self, raw_lines):
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

    def _safe_quant_available(self, quant):
        if hasattr(quant, 'available_quantity'):
            return quant.available_quantity or 0.0
        return (quant.quantity or 0.0) - (quant.reserved_quantity or 0.0)

    def _build_lines_from_sale_line_lots(self, move):
        raw_lines = []
        sale_line = move.sale_line_id
        if not sale_line or not hasattr(sale_line, 'lot_ids') or not sale_line.lot_ids:
            return raw_lines
        Quant = self.env['stock.quant']
        seen = set()
        for lot in sale_line.lot_ids:
            quants = Quant.search([
                ('product_id', '=', move.product_id.id),
                ('lot_id', '=', lot.id),
                ('location_id.usage', '=', 'internal'),
                ('quantity', '>', 0),
            ], order='location_id')
            if not quants:
                raw_lines.append((0, 0, {
                    'picking_id': move.picking_id.id,
                    'move_id': move.id,
                    'sale_line_id': sale_line.id,
                    'product_id': move.product_id.id,
                    'lot_id': lot.id,
                    'qty_available': 0.0,
                    'qty_to_deliver': 0.0,
                    'is_selected': False,
                }))
                continue
            for quant in quants:
                qty_avail = self._safe_quant_available(quant)
                key = (move.id, lot.id, quant.location_id.id)
                if key in seen:
                    continue
                seen.add(key)
                raw_lines.append((0, 0, {
                    'picking_id': move.picking_id.id,
                    'move_id': move.id,
                    'sale_line_id': sale_line.id,
                    'product_id': move.product_id.id,
                    'lot_id': lot.id,
                    'quant_id': quant.id,
                    'source_location_id': quant.location_id.id,
                    'qty_available': qty_avail,
                    'qty_to_deliver': qty_avail if qty_avail > 0 else 0.0,
                    'is_selected': qty_avail > 0,
                }))
        return raw_lines

    def _build_lines_from_move_lines(self, move):
        raw_lines = []
        seen = set()
        for ml in move.move_line_ids:
            lot_id = ml.lot_id.id if ml.lot_id else False
            location_id = ml.location_id.id if ml.location_id else False
            qty_avail = (
                ml.quantity
                or getattr(ml, 'reserved_uom_qty', 0.0)
                or getattr(ml, 'product_uom_qty', 0.0)
                or 0.0)
            if not lot_id and not location_id and not qty_avail:
                continue
            key = (move.id, ml.id, lot_id, location_id)
            if key in seen:
                continue
            seen.add(key)
            raw_lines.append((0, 0, {
                'picking_id': move.picking_id.id,
                'move_id': move.id,
                'move_line_id': ml.id,
                'sale_line_id': move.sale_line_id.id,
                'product_id': move.product_id.id,
                'lot_id': lot_id,
                'source_location_id': location_id,
                'qty_available': qty_avail,
                'qty_to_deliver': qty_avail if qty_avail > 0 else 0.0,
                'is_selected': qty_avail > 0,
            }))
        return raw_lines

    def _build_lines_from_pickings(self, order):
        raw_lines = []
        pickings = order.picking_ids.filtered(
            lambda p: p.state in ('assigned', 'confirmed', 'waiting')
            and p.picking_type_code in ('internal', 'outgoing'))
        for picking in pickings:
            moves = picking.move_ids.filtered(
                lambda m: m.state not in ('done', 'cancel'))
            for move in moves:
                move_lines = self._build_lines_from_sale_line_lots(move)
                if not move_lines:
                    move_lines = self._build_lines_from_move_lines(move)
                if not move_lines:
                    qty_fallback = move.product_uom_qty or 0.0
                    move_lines = [(0, 0, {
                        'picking_id': picking.id,
                        'move_id': move.id,
                        'sale_line_id': move.sale_line_id.id,
                        'product_id': move.product_id.id,
                        'qty_available': qty_fallback,
                        'qty_to_deliver': qty_fallback if qty_fallback > 0 else 0.0,
                        'is_selected': qty_fallback > 0,
                    })]
                raw_lines.extend(move_lines)
        return self._group_lines_by_product(raw_lines)

    def _build_lines_from_pick_ticket(self, order, pt):
        pt_lookup = {}
        for pt_line in pt.line_ids:
            key = (
                pt_line.move_id.id if pt_line.move_id else False,
                pt_line.lot_id.id if pt_line.lot_id else False,
                pt_line.source_location_id.id if pt_line.source_location_id else False,
            )
            pt_lookup[key] = pt_line.qty_selected
        base_lines = self._build_lines_from_pickings(order)
        adjusted = []
        for cmd in base_lines:
            vals = dict(cmd[2])
            if vals.get('display_type') == 'line_section':
                adjusted.append((0, 0, vals))
                continue
            key = (
                vals.get('move_id'),
                vals.get('lot_id'),
                vals.get('source_location_id'),
            )
            pt_qty = pt_lookup.get(key, 0.0)
            vals['qty_to_deliver'] = pt_qty
            vals['is_selected'] = pt_qty > 0
            adjusted.append((0, 0, vals))
        return adjusted

    # ─── Get selected from widget_selections JSON ────────────────────

    def _get_selected_from_widget(self):
        """Parse widget_selections JSON and return list of selection dicts.
        Each dict has: lotId, productId, pickingId, moveId, moveLineId,
        saleLineId, sourceLocationId, qty, qtyAvailable
        """
        self.ensure_one()
        try:
            sels = json.loads(self.widget_selections or '[]')
        except (json.JSONDecodeError, TypeError):
            sels = []

        if not sels:
            raise UserError(_('Seleccione al menos una línea.'))

        for sel in sels:
            if sel.get('qty', 0) <= 0:
                product = self.env['product.product'].browse(
                    sel.get('productId', 0))
                raise UserError(_(
                    'La cantidad a entregar debe ser mayor a 0 para %s.',
                    product.display_name if product else 'desconocido'
                ))

        return sels

    # ─── Actions ─────────────────────────────────────────────────────

    def _get_selected_lines(self):
        """Fallback: get selected from line_ids (used when widget_selections
        is empty, e.g. when using native list view)."""
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
        self.ensure_one()
        for line in self.line_ids.filtered(
                lambda l: l.display_type != 'line_section'):
            line.is_selected = True
            if line.qty_available > 0 and line.qty_to_deliver <= 0:
                line.qty_to_deliver = line.qty_available
        return self._refresh()

    def action_deselect_all(self):
        self.ensure_one()
        for line in self.line_ids.filtered(
                lambda l: l.display_type != 'line_section'):
            line.is_selected = False
            line.qty_to_deliver = 0.0
        return self._refresh()

    def action_generate_pick_ticket(self):
        self.ensure_one()
        # Try widget_selections first, fallback to line_ids
        try:
            sels = json.loads(self.widget_selections or '[]')
        except (json.JSONDecodeError, TypeError):
            sels = []

        if sels:
            return self._generate_pick_ticket_from_selections(sels)
        else:
            return self._generate_pick_ticket_from_lines()

    def _generate_pick_ticket_from_selections(self, sels):
        """Generate pick ticket from widget_selections JSON."""
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
        return self.env.ref(
            'sale_delivery_wizard.action_report_pick_ticket'
        ).report_action(doc)

    def _generate_pick_ticket_from_lines(self):
        """Fallback: generate from line_ids."""
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
        try:
            sels = json.loads(self.widget_selections or '[]')
        except (json.JSONDecodeError, TypeError):
            sels = []

        if sels:
            return self._generate_remission_from_selections(sels)
        else:
            return self._generate_remission_from_lines()

    def _generate_remission_from_selections(self, sels):
        """Generate remission from widget_selections JSON."""
        order = self.sale_order_id

        if hasattr(order, 'delivery_auth_state') and order.delivery_auth_state == 'pending':
            if not self.env.user.has_group(
                    'sale_delivery_wizard.group_delivery_authorizer'):
                raise UserError(_(
                    'Entrega bloqueada: pedido sin autorización de pago.'))

        # Group by picking
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
                }) for sel in sel_lines],
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

    def _generate_remission_from_lines(self):
        """Fallback: generate from line_ids."""
        selected = self._get_selected_lines()
        for line in selected:
            if line.qty_to_deliver > line.qty_available:
                raise UserError(_(
                    'No puede entregar más de lo disponible para %s. '
                    'Disponible: %s, Solicitado: %s',
                    line.product_id.display_name,
                    line.qty_available, line.qty_to_deliver))

        order = self.sale_order_id
        if hasattr(order, 'delivery_auth_state') and order.delivery_auth_state == 'pending':
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
    quant_id = fields.Many2one('stock.quant', string='Quant')

    qty_available = fields.Float(string='Disponible')
    qty_to_deliver = fields.Float(string='A Entregar')

    lot_name = fields.Char(related='lot_id.name', string='# Lote')
    product_name = fields.Char(
        related='product_id.display_name', string='Producto Desc.')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('display_type') == 'line_section':
                continue
            if not vals.get('product_id') and vals.get('move_id'):
                move = self.env['stock.move'].browse(vals['move_id'])
                vals['product_id'] = move.product_id.id
            if not vals.get('sale_line_id') and vals.get('move_id'):
                move = self.env['stock.move'].browse(vals['move_id'])
                vals['sale_line_id'] = move.sale_line_id.id
            if not vals.get('lot_id') and vals.get('move_line_id'):
                ml = self.env['stock.move.line'].browse(vals['move_line_id'])
                vals['lot_id'] = ml.lot_id.id if ml.lot_id else False
            if not vals.get('source_location_id'):
                if vals.get('quant_id'):
                    quant = self.env['stock.quant'].browse(vals['quant_id'])
                    vals['source_location_id'] = quant.location_id.id
                elif vals.get('move_line_id'):
                    ml = self.env['stock.move.line'].browse(vals['move_line_id'])
                    vals['source_location_id'] = ml.location_id.id if ml.location_id else False
            if not vals.get('qty_available'):
                if vals.get('quant_id'):
                    quant = self.env['stock.quant'].browse(vals['quant_id'])
                    if hasattr(quant, 'available_quantity'):
                        vals['qty_available'] = quant.available_quantity or 0.0
                    else:
                        vals['qty_available'] = (
                            (quant.quantity or 0.0) - (quant.reserved_quantity or 0.0))
                elif vals.get('move_line_id'):
                    ml = self.env['stock.move.line'].browse(vals['move_line_id'])
                    vals['qty_available'] = (
                        ml.quantity
                        or getattr(ml, 'reserved_uom_qty', 0.0)
                        or getattr(ml, 'product_uom_qty', 0.0)
                        or 0.0)
                elif vals.get('move_id'):
                    move = self.env['stock.move'].browse(vals['move_id'])
                    vals['qty_available'] = move.product_uom_qty or 0.0
            if vals.get('is_selected') and not vals.get('qty_to_deliver'):
                vals['qty_to_deliver'] = vals.get('qty_available', 0.0)
        return super().create(vals_list)

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