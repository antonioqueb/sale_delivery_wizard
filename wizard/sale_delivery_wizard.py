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
        ('select_pt', 'Seleccionar Pick Ticket'),
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

    # ── Modo edición ─────────────────────────────────────────────────
    editing_pick_ticket_id = fields.Many2one(
        'sale.delivery.document', string='Pick Ticket a Editar')
    is_editing = fields.Boolean(
        compute='_compute_is_editing', string='Modo Edición')

    # ── Selector integrado de PT ─────────────────────────────────────
    open_pt_ids = fields.Many2many(
        'sale.delivery.document',
        'sale_delivery_wizard_open_pt_rel',
        'wizard_id', 'pt_id',
        string='PTs Abiertos')
    pt_count = fields.Integer(
        compute='_compute_pt_count', string='PTs Abiertos')

    widget_selections = fields.Text(
        string='Selecciones del Widget', default='[]')

    @api.depends('editing_pick_ticket_id')
    def _compute_is_editing(self):
        for wiz in self:
            wiz.is_editing = bool(wiz.editing_pick_ticket_id)

    @api.depends('open_pt_ids')
    def _compute_pt_count(self):
        for wiz in self:
            wiz.pt_count = len(wiz.open_pt_ids)

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
        editing_pt_id = (
            res.get('editing_pick_ticket_id')
            or self.env.context.get('default_editing_pick_ticket_id')
        )

        # Si no viene editing_pt_id pero hay 2+ PTs abiertos → modo selector
        if not editing_pt_id:
            open_pts = order._get_open_pick_tickets()
            if len(open_pts) >= 2:
                res['sale_order_id'] = order.id
                res['wizard_state'] = 'select_pt'
                res['open_pt_ids'] = [(6, 0, open_pts.ids)]
                res['delivery_address'] = order.partner_shipping_id.contact_address or ''
                res['widget_selections'] = '[]'
                return res

        res.update(self._prepare_default_wizard_vals(order, editing_pt_id))
        # Mantener open_pt_ids cargados también en otros modos por si acaso
        open_pts = order._get_open_pick_tickets()
        res['open_pt_ids'] = [(6, 0, open_pts.ids)]
        return res

    def _prepare_default_wizard_vals(self, order, editing_pt_id=None):
        """
        - editing_pt_id presente → cargar líneas del PT (modo edición).
        - editing_pt_id vacío → wizard limpio para crear PT nuevo.

        Matching en 2 niveles (exacto + por lot_id) y grupo "huérfano"
        para lotes del PT que ya no aparecen en el grouped.
        """
        delivery_address = order.partner_shipping_id.contact_address or ''

        vals = {
            'sale_order_id': order.id,
            'delivery_address': delivery_address,
            'special_instructions': '',
            'widget_selections': '[]',
            'wizard_state': 'select',
        }

        grouped = order.get_delivery_grouped_data(
            mode='delivery', editing_pt_id=editing_pt_id) or []

        if not editing_pt_id:
            for group in grouped:
                for line in group.get('lines', []):
                    line['isSelected'] = False
                    line['qtyToDeliver'] = 0
            vals['line_ids'] = self._groups_to_line_commands(grouped)
            return vals

        pt = self.env['sale.delivery.document'].browse(editing_pt_id)
        if not pt.exists() or pt.state != 'prepared' or not pt.line_ids:
            _logger.warning(
                '[DELIVERY WIZARD EDIT] PT %s no existe / no es editable / vacío',
                editing_pt_id)
            for group in grouped:
                for line in group.get('lines', []):
                    line['isSelected'] = False
                    line['qtyToDeliver'] = 0
            vals['line_ids'] = self._groups_to_line_commands(grouped)
            return vals

        pt_line_map = {}
        pt_lot_map = {}
        pt_lots_unmatched = {}
        for pt_line in pt.line_ids:
            if pt_line.qty_selected <= 0:
                continue
            key_full = (
                pt_line.product_id.id,
                pt_line.lot_id.id if pt_line.lot_id else 0,
            )
            info = {
                'qty': pt_line.qty_selected,
                'productId': pt_line.product_id.id,
                'productName': pt_line.product_id.display_name,
                'lotId': pt_line.lot_id.id if pt_line.lot_id else 0,
                'lotName': pt_line.lot_id.name if pt_line.lot_id else '',
                'saleLineId': pt_line.sale_line_id.id if pt_line.sale_line_id else 0,
                'moveId': pt_line.move_id.id if pt_line.move_id else 0,
                'moveLineId': pt_line.move_line_id.id if pt_line.move_line_id else 0,
                'sourceLocationId': pt_line.source_location_id.id if pt_line.source_location_id else 0,
                'sourceLocation': pt_line.source_location_id.display_name if pt_line.source_location_id else '',
            }
            pt_line_map[key_full] = info
            if pt_line.lot_id:
                pt_lot_map[pt_line.lot_id.id] = info
            pt_lots_unmatched[key_full] = info

        widget_sels = []
        matched_count = 0
        for group in grouped:
            for line in group.get('lines', []):
                pid = line.get('productId', 0)
                lid = line.get('lotId', 0)
                key_full = (pid, lid)

                pt_info = pt_line_map.get(key_full)
                if not pt_info and lid:
                    pt_info = pt_lot_map.get(lid)

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
                    matched_key = (pt_info['productId'], pt_info['lotId'])
                    pt_lots_unmatched.pop(matched_key, None)
                    matched_count += 1
                else:
                    line['isSelected'] = False
                    line['qtyToDeliver'] = 0

        if pt_lots_unmatched:
            _logger.warning(
                '[DELIVERY WIZARD EDIT] PT %s: %d línea(s) huérfana(s)',
                pt.name, len(pt_lots_unmatched))

            orphan_groups = {}
            for key, info in pt_lots_unmatched.items():
                pid = info['productId']
                if pid not in orphan_groups:
                    orphan_groups[pid] = {
                        'productId': pid,
                        'productName': '⚠️ ' + info['productName'] + ' (lote desactualizado)',
                        'lines': [],
                        'totalQty': 0.0,
                        'selectedCount': 0,
                        'lineCount': 0,
                    }
                g = orphan_groups[pid]
                line_dict = {
                    'dbId': 0,
                    'lotId': info['lotId'],
                    'lotName': info['lotName'],
                    'productId': pid,
                    'productName': info['productName'],
                    'pickingId': 0,
                    'moveId': info['moveId'],
                    'moveLineId': info['moveLineId'],
                    'saleLineId': info['saleLineId'],
                    'isSelected': True,
                    'qtyAvailable': info['qty'],
                    'qtyToDeliver': info['qty'],
                    'sourceLocation': info['sourceLocation'],
                    'sourceLocationId': info['sourceLocationId'],
                }
                g['lines'].append(line_dict)
                g['lineCount'] += 1
                g['totalQty'] += info['qty']
                g['selectedCount'] += 1

                widget_sels.append({
                    'dbId': 0,
                    'lotId': info['lotId'],
                    'productId': pid,
                    'pickingId': 0,
                    'moveId': info['moveId'],
                    'moveLineId': info['moveLineId'],
                    'saleLineId': info['saleLineId'],
                    'sourceLocationId': info['sourceLocationId'],
                    'qty': info['qty'],
                    'qtyAvailable': info['qty'],
                })

            grouped.extend(orphan_groups.values())

        vals.update({
            'widget_selections': json.dumps(widget_sels),
            'wizard_state': 'pick_ticket',
            'editing_pick_ticket_id': pt.id,
            'pick_ticket_id': pt.id,
            'delivery_address': pt.delivery_address or delivery_address,
            'special_instructions': pt.special_instructions or '',
        })
        vals['line_ids'] = self._groups_to_line_commands(grouped)

        _logger.info(
            '[DELIVERY WIZARD EDIT] PT %s cargado — %d matcheadas, '
            '%d huérfanas, widget_sels=%d',
            pt.name, matched_count, len(pt_lots_unmatched), len(widget_sels))

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

        selected_by_lot = {}
        for s in selections or []:
            lid = int(s.get('lotId') or 0)
            if lid:
                selected_by_lot[lid] = {
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

            sel_info = selected_map.get(key)
            if not sel_info and line.lot_id:
                sel_info = selected_by_lot.get(line.lot_id.id)

            if sel_info:
                is_selected = sel_info['qty'] > 0
                qty_to_deliver = sel_info['qty']
                source_location_id = sel_info['sourceLocationId'] or (
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

    # ═══════════════════════════════════════════════════════════════════
    # SELECTOR INTEGRADO DE PT — cambia el mismo wizard de estado
    # ═══════════════════════════════════════════════════════════════════

    def action_load_pt_by_id(self, pt_id):
        """
        Carga un PT específico por ID dentro del mismo wizard.
        Llamado desde el componente OWL `pt_selector_cards` al hacer click
        en una tarjeta. NO abre un wizard nuevo — recarga el actual con las
        líneas del PT seleccionado y cambia a estado 'pick_ticket'.
        """
        self.ensure_one()
        if not pt_id:
            raise UserError(_('Pick Ticket no válido.'))

        pt = self.env['sale.delivery.document'].browse(pt_id)
        if not pt.exists() or pt.state != 'prepared':
            raise UserError(_(
                'El Pick Ticket ya no está disponible para edición.'))

        self.line_ids.unlink()
        vals = self._prepare_default_wizard_vals(
            self.sale_order_id, editing_pt_id=pt_id)
        line_cmds = vals.pop('line_ids', [])
        vals.pop('sale_order_id', None)
        self.write(vals)
        if line_cmds:
            self.write({'line_ids': line_cmds})
        return self._refresh()

    def action_start_new_pt(self):
        """Desde el selector, cambia el mismo wizard a modo 'nuevo PT'."""
        self.ensure_one()
        self.line_ids.unlink()
        vals = self._prepare_default_wizard_vals(
            self.sale_order_id, editing_pt_id=None)
        line_cmds = vals.pop('line_ids', [])
        vals.pop('sale_order_id', None)
        vals['wizard_state'] = 'select'
        vals['editing_pick_ticket_id'] = False
        vals['pick_ticket_id'] = False
        self.write(vals)
        if line_cmds:
            self.write({'line_ids': line_cmds})
        return self._refresh()

    def action_back_to_pt_selector(self):
        """Regresa al selector de PT (si hay 2+ PTs abiertos)."""
        self.ensure_one()
        open_pts = self.sale_order_id._get_open_pick_tickets()
        if len(open_pts) < 2:
            raise UserError(_(
                'Ya no hay múltiples Pick Tickets abiertos para seleccionar.'))
        self.line_ids.unlink()
        self.write({
            'wizard_state': 'select_pt',
            'open_pt_ids': [(6, 0, open_pts.ids)],
            'editing_pick_ticket_id': False,
            'pick_ticket_id': False,
            'widget_selections': '[]',
        })
        return self._refresh()

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
    # VALIDACIÓN DE COLISIÓN DE LOTES ENTRE PICK TICKETS
    # ═══════════════════════════════════════════════════════════════════

    def _validate_no_lot_collision(self, selections, exclude_pt_id=None):
        if not selections:
            return

        order = self.sale_order_id
        lot_to_pts = order._get_lot_to_pt_map(exclude_pt_id=exclude_pt_id)
        if not lot_to_pts:
            return

        collisions = {}
        for sel in selections:
            try:
                qty = float(sel.get('qty', 0) or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                continue
            lot_id = sel.get('lotId')
            if lot_id and lot_id in lot_to_pts:
                lot = self.env['stock.lot'].browse(lot_id)
                collisions[lot.name or str(lot_id)] = lot_to_pts[lot_id]

        if collisions:
            msg_lines = [
                '• %s → %s' % (lot_name, ', '.join(pt_names))
                for lot_name, pt_names in collisions.items()
            ]
            raise UserError(_(
                'Los siguientes lotes ya están incluidos en otros Pick Tickets '
                'abiertos y no se pueden duplicar:\n\n%s\n\n'
                'Edite o cancele el otro Pick Ticket antes de continuar.'
            ) % '\n'.join(msg_lines))

    # ═══════════════════════════════════════════════════════════════════
    # PICK TICKET — generación, edición y reimpresión
    # ═══════════════════════════════════════════════════════════════════

    def action_generate_pick_ticket(self):
        self.ensure_one()

        try:
            sels = json.loads(self.widget_selections or '[]')
        except (json.JSONDecodeError, TypeError):
            sels = []

        target_pt = self.editing_pick_ticket_id or (
            self.pick_ticket_id
            if self.pick_ticket_id and self.pick_ticket_id.state == 'prepared'
            else False
        )

        self._validate_no_lot_collision(
            sels, exclude_pt_id=target_pt.id if target_pt else None)

        if target_pt:
            return self._update_existing_pick_ticket(target_pt, sels)

        if sels:
            return self._generate_pick_ticket_from_selections(sels)
        return self._generate_pick_ticket_from_lines()

    def _update_existing_pick_ticket(self, pt, sels):
        if pt.state != 'prepared':
            raise UserError(_(
                'Solo se pueden editar Pick Tickets en estado Preparado '
                '(estado actual: %s).', pt.state))

        if not sels:
            selected = self._get_selected_lines()
            sels = [{
                'saleLineId': l.sale_line_id.id,
                'moveId': l.move_id.id,
                'moveLineId': l.move_line_id.id,
                'productId': l.product_id.id,
                'lotId': l.lot_id.id,
                'qty': l.qty_to_deliver,
                'sourceLocationId': l.source_location_id.id,
                'pickingId': l.picking_id.id,
            } for l in selected]

        new_lines = []
        for sel in sels:
            if float(sel.get('qty', 0) or 0) <= 0:
                continue
            new_lines.append((0, 0, {
                'sale_line_id': sel.get('saleLineId') or False,
                'move_id': sel.get('moveId') or False,
                'move_line_id': sel.get('moveLineId') or False,
                'product_id': sel.get('productId'),
                'lot_id': sel.get('lotId') or False,
                'qty_selected': sel.get('qty', 0),
                'source_location_id': sel.get('sourceLocationId') or False,
            }))

        if not new_lines:
            raise UserError(_('Seleccione al menos una línea.'))

        pt.line_ids.unlink()
        pt.write({
            'line_ids': new_lines,
            'delivery_address': self.delivery_address,
            'special_instructions': self.special_instructions,
        })
        pt.message_post(body=_(
            'Pick Ticket actualizado por %s — %d línea(s)',
            self.env.user.name, len(new_lines)))

        self.pick_ticket_id = pt.id
        self.editing_pick_ticket_id = pt.id
        self.wizard_state = 'pick_ticket'

        return self.env.ref(
            'sale_delivery_wizard.action_report_pick_ticket'
        ).report_action(pt)

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
        self.editing_pick_ticket_id = doc.id
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
        self.editing_pick_ticket_id = doc.id
        self.wizard_state = 'pick_ticket'
        return self.env.ref(
            'sale_delivery_wizard.action_report_pick_ticket'
        ).report_action(doc)

    def action_print_pick_ticket(self):
        self.ensure_one()
        pt = self.pick_ticket_id or self.editing_pick_ticket_id
        if not pt:
            raise UserError(_('No hay Pick Ticket para imprimir.'))
        return self.env.ref(
            'sale_delivery_wizard.action_report_pick_ticket'
        ).report_action(pt)

    # ═══════════════════════════════════════════════════════════════════
    # REMISIÓN
    # ═══════════════════════════════════════════════════════════════════

    def action_generate_remission(self):
        self.ensure_one()

        active_pt = self.editing_pick_ticket_id or self.pick_ticket_id

        if (active_pt
                and active_pt.state == 'prepared'
                and active_pt.line_ids):
            self.pick_ticket_id = active_pt.id
            return self._generate_remission_from_pick_ticket()

        try:
            sels = json.loads(self.widget_selections or '[]')
        except (json.JSONDecodeError, TypeError):
            sels = []

        if sels:
            self._validate_no_lot_collision(sels)
            return self._generate_remission_from_selections(sels)
        return self._generate_remission_from_lines()

    def _resolve_current_picking_for_lot(self, order, product_id, lot_id):
        active_pickings = order.picking_ids.filtered(
            lambda p: p.state in ('assigned', 'confirmed', 'waiting')
            and p.picking_type_code in ('internal', 'outgoing')
        )

        for picking in active_pickings:
            for move in picking.move_ids.filtered(
                lambda m: m.product_id.id == product_id
                and m.state not in ('done', 'cancel')
            ):
                if lot_id:
                    for ml in move.move_line_ids:
                        if ml.lot_id.id == lot_id:
                            return picking.id, move.id, ml.id
                else:
                    ml = move.move_line_ids[:1]
                    return picking.id, move.id, (ml.id if ml else 0)

        return 0, 0, 0

    def _generate_remission_from_pick_ticket(self):
        pt = self.pick_ticket_id
        order = self.sale_order_id

        if hasattr(order, 'delivery_auth_state') and order.delivery_auth_state == 'pending':
            if not self.env.user.has_group(
                    'sale_delivery_wizard.group_delivery_authorizer'):
                raise UserError(_(
                    'Entrega bloqueada: pedido sin autorización de pago.'))

        sels = []
        for pt_line in pt.line_ids:
            if pt_line.qty_selected <= 0:
                continue

            product_id = pt_line.product_id.id
            lot_id = pt_line.lot_id.id if pt_line.lot_id else 0

            picking_id, move_id, move_line_id = \
                self._resolve_current_picking_for_lot(order, product_id, lot_id)

            if not picking_id:
                _logger.warning(
                    '[REMISSION-PT] Could not resolve live picking for '
                    'product=%s lot=%s',
                    pt_line.product_id.display_name,
                    pt_line.lot_id.name if pt_line.lot_id else 'N/A')
                if pt_line.move_id and pt_line.move_id.picking_id:
                    picking_id = pt_line.move_id.picking_id.id
                elif pt_line.move_line_id and pt_line.move_line_id.picking_id:
                    picking_id = pt_line.move_line_id.picking_id.id
                move_id = pt_line.move_id.id if pt_line.move_id else 0
                move_line_id = pt_line.move_line_id.id if pt_line.move_line_id else 0

            sels.append({
                'saleLineId': pt_line.sale_line_id.id if pt_line.sale_line_id else False,
                'moveId': move_id or False,
                'moveLineId': move_line_id or False,
                'productId': product_id,
                'lotId': lot_id or False,
                'qty': pt_line.qty_selected,
                'sourceLocationId': pt_line.source_location_id.id if pt_line.source_location_id else False,
                'pickingId': picking_id,
            })

        if not sels:
            raise UserError(_('El Pick Ticket no tiene líneas válidas.'))

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

        active_pt = self.editing_pick_ticket_id or self.pick_ticket_id
        if active_pt and active_pt.state == 'prepared':
            active_pt.write({'state': 'confirmed'})

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