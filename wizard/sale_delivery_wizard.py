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

    # JSON que escribe el widget OWL
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

            # Fallback para compatibilidad
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
        vals['line_ids'] = self._groups_to_line_commands(grouped)

        if pending_pt:
            vals['wizard_state'] = 'pick_ticket'
            vals['pick_ticket_id'] = pending_pt.id

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

        # Si hay JSON del widget, usar el estado actual persistido
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

    def _get_selection_payload(self):
        self.ensure_one()
        try:
            selections = json.loads(self.widget_selections or '[]')
            if isinstance(selections, list):
                return selections
        except (json.JSONDecodeError, TypeError):
            pass

        payload = []
        for line in self.line_ids.filtered(
            lambda l: l.display_type != 'line_section' and l.is_selected and l.qty_to_deliver > 0
        ):
            payload.append({
                'dbId': line.id,
                'lotId': line.lot_id.id if line.lot_id else 0,
                'productId': line.product_id.id if line.product_id else 0,
                'pickingId': line.picking_id.id if line.picking_id else 0,
                'moveId': line.move_id.id if line.move_id else 0,
                'moveLineId': line.move_line_id.id if line.move_line_id else 0,
                'saleLineId': line.sale_line_id.id if line.sale_line_id else 0,
                'sourceLocationId': line.source_location_id.id if line.source_location_id else 0,
                'qty': line.qty_to_deliver or 0.0,
                'qtyAvailable': line.qty_available or 0.0,
            })
        return payload

    def _validate_selection_payload(self, payload):
        if not payload:
            raise UserError(_('Debes seleccionar al menos un lote para continuar.'))

        invalid = [p for p in payload if float(p.get('qty', 0) or 0) <= 0]
        if invalid:
            raise UserError(_('Todas las líneas seleccionadas deben tener cantidad mayor a cero.'))

    def _build_document_line_commands_from_payload(self, payload):
        self.ensure_one()
        commands = []
        seq = 10

        move_line_ids = [int(p.get('moveLineId') or 0) for p in payload if p.get('moveLineId')]
        lot_ids = [int(p.get('lotId') or 0) for p in payload if p.get('lotId')]
        move_ids = [int(p.get('moveId') or 0) for p in payload if p.get('moveId')]
        sale_line_ids = [int(p.get('saleLineId') or 0) for p in payload if p.get('saleLineId')]
        location_ids = [int(p.get('sourceLocationId') or 0) for p in payload if p.get('sourceLocationId')]

        move_line_map = {ml.id: ml for ml in self.env['stock.move.line'].browse(move_line_ids)}
        lot_map = {lot.id: lot for lot in self.env['stock.lot'].browse(lot_ids)}
        move_map = {mv.id: mv for mv in self.env['stock.move'].browse(move_ids)}
        sale_line_map = {sl.id: sl for sl in self.env['sale.order.line'].browse(sale_line_ids)}
        location_map = {loc.id: loc for loc in self.env['stock.location'].browse(location_ids)}

        for item in payload:
            move_line = move_line_map.get(int(item.get('moveLineId') or 0))
            lot = lot_map.get(int(item.get('lotId') or 0))
            move = move_map.get(int(item.get('moveId') or 0))
            sale_line = sale_line_map.get(int(item.get('saleLineId') or 0))
            source_location = location_map.get(int(item.get('sourceLocationId') or 0))

            product = False
            picking = False

            if move_line:
                product = move_line.product_id
                picking = move_line.picking_id
                if not source_location:
                    source_location = move_line.location_id
                if not move:
                    move = move_line.move_id
            elif move:
                product = move.product_id
                picking = move.picking_id
                if not source_location:
                    source_location = move.location_id
            elif sale_line:
                product = sale_line.product_id

            if not product:
                continue

            commands.append((0, 0, {
                'sequence': seq,
                'sale_line_id': sale_line.id if sale_line else False,
                'move_id': move.id if move else False,
                'move_line_id': move_line.id if move_line else False,
                'product_id': product.id,
                'lot_id': lot.id if lot else False,
                'qty_selected': float(item.get('qty', 0) or 0.0),
                'source_location_id': source_location.id if source_location else False,
            }))
            seq += 10

        return commands

    def _get_primary_picking_from_payload(self, payload):
        self.ensure_one()

        picking_ids = [int(p.get('pickingId') or 0) for p in payload if p.get('pickingId')]
        if picking_ids:
            return self.env['stock.picking'].browse(picking_ids[0])

        move_line_ids = [int(p.get('moveLineId') or 0) for p in payload if p.get('moveLineId')]
        if move_line_ids:
            ml = self.env['stock.move.line'].browse(move_line_ids[0])
            if ml and ml.picking_id:
                return ml.picking_id

        move_ids = [int(p.get('moveId') or 0) for p in payload if p.get('moveId')]
        if move_ids:
            mv = self.env['stock.move'].browse(move_ids[0])
            if mv and mv.picking_id:
                return mv.picking_id

        return self.sale_order_id.picking_ids.filtered(
            lambda p: p.state in ('assigned', 'confirmed', 'waiting')
        )[:1]

    def action_generate_pick_ticket(self):
        self.ensure_one()

        payload = self._get_selection_payload()
        self._validate_selection_payload(payload)

        line_commands = self._build_document_line_commands_from_payload(payload)
        if not line_commands:
            raise UserError(_('No fue posible construir líneas válidas para el pick ticket.'))

        picking = self._get_primary_picking_from_payload(payload)

        doc = self.env['sale.delivery.document'].create({
            'document_type': 'pick_ticket',
            'state': 'prepared',
            'sale_order_id': self.sale_order_id.id,
            'picking_id': picking.id if picking else False,
            'delivery_address': self.delivery_address or '',
            'special_instructions': self.special_instructions or '',
            'line_ids': line_commands,
        })

        self.pick_ticket_id = doc.id
        self.wizard_state = 'pick_ticket'

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_confirm_remission(self):
        self.ensure_one()

        payload = self._get_selection_payload()
        self._validate_selection_payload(payload)

        line_commands = self._build_document_line_commands_from_payload(payload)
        if not line_commands:
            raise UserError(_('No fue posible construir líneas válidas para la remisión.'))

        picking = self._get_primary_picking_from_payload(payload)

        doc = self.env['sale.delivery.document'].create({
            'document_type': 'remission',
            'state': 'draft',
            'sale_order_id': self.sale_order_id.id,
            'picking_id': picking.id if picking else False,
            'delivery_address': self.delivery_address or '',
            'special_instructions': self.special_instructions or '',
            'line_ids': line_commands,
        })

        doc.action_confirm()

        return {
            'type': 'ir.actions.act_window',
            'name': _('Remisión'),
            'res_model': 'sale.delivery.document',
            'res_id': doc.id,
            'view_mode': 'form',
            'target': 'current',
        }


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