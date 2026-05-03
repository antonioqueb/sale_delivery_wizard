from collections import OrderedDict
import json
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SaleSwapWizard(models.TransientModel):
    _name = 'sale.swap.wizard'
    _description = 'Wizard de Swap de Lotes'

    sale_order_id = fields.Many2one(
        'sale.order',
        string='Orden de Venta',
        required=True,
    )
    line_ids = fields.One2many(
        'sale.swap.wizard.line',
        'wizard_id',
        string='Lotes Asignados',
    )

    widget_selections = fields.Text(
        string='Selecciones del Widget',
        default='[]',
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so_id = res.get('sale_order_id') or self.env.context.get('active_id')

        if not so_id:
            return res

        order = self.env['sale.order'].browse(so_id)
        if not order.exists():
            return res

        res['sale_order_id'] = order.id
        res['widget_selections'] = '[]'

        raw_lines = []

        for picking in order.picking_ids.filtered(
            lambda p: p.state in ('assigned', 'confirmed')
            and p.picking_type_code in ('outgoing', 'internal')
        ):
            for move in picking.move_ids.filtered(
                lambda m: m.state in ('assigned', 'confirmed')
            ):
                for ml in move.move_line_ids:
                    if not ml.lot_id:
                        continue

                    lot = ml.lot_id

                    raw_lines.append((0, 0, {
                        'product_id': move.product_id.id,
                        'origin_lot_id': lot.id,
                        'move_line_id': ml.id,
                        'picking_id': picking.id,
                        'sale_line_id': move.sale_line_id.id if move.sale_line_id else False,
                        'qty': (
                            ml.quantity
                            or getattr(ml, 'reserved_uom_qty', 0.0)
                            or getattr(ml, 'qty_done', 0.0)
                            or move.product_uom_qty
                            or 0.0
                        ),
                        'origin_bloque': lot.x_bloque or '' if hasattr(lot, 'x_bloque') else '',
                        'origin_atado': lot.x_atado or '' if hasattr(lot, 'x_atado') else '',
                        'origin_alto': str(lot.x_alto) if hasattr(lot, 'x_alto') and lot.x_alto else '',
                        'origin_ancho': str(lot.x_ancho) if hasattr(lot, 'x_ancho') and lot.x_ancho else '',
                        'origin_grosor': str(lot.x_grosor) if hasattr(lot, 'x_grosor') and lot.x_grosor else '',
                    }))

        res['line_ids'] = self._group_lines_by_product(raw_lines)
        return res

    def _group_lines_by_product(self, raw_lines):
        grouped = OrderedDict()

        for cmd in raw_lines:
            vals = cmd[2]
            pid = vals.get('product_id') or 0
            grouped.setdefault(pid, []).append(cmd)

        result = []
        Product = self.env['product.product']
        seq = 0

        for pid, lines in grouped.items():
            product = Product.browse(pid) if pid else Product
            section_name = (
                product.display_name
                if product and product.exists()
                else _('Sin Producto')
            )

            result.append((0, 0, {
                'display_type': 'line_section',
                'section_name': section_name,
                'product_id': pid or False,
                'sequence': seq,
            }))
            seq += 1

            for line_cmd in lines:
                line_cmd[2]['sequence'] = seq
                result.append(line_cmd)
                seq += 1

        return result

    def get_grouped_lines_data(self):
        self.ensure_one()

        groups = []
        current_group = None

        for line in self.line_ids.sorted(lambda l: (l.sequence, l.id)):
            if line.display_type == 'line_section':
                if current_group and current_group['lines']:
                    groups.append(current_group)

                current_group = {
                    'productId': line.product_id.id if line.product_id else 0,
                    'productName': line.section_name or (
                        line.product_id.display_name if line.product_id else _('Sin Producto')
                    ),
                    'lines': [],
                    'totalQty': 0.0,
                    'selectedCount': 0,
                    'lineCount': 0,
                }
                continue

            if current_group is None:
                pname = line.product_id.display_name if line.product_id else _('Sin Producto')
                current_group = {
                    'productId': line.product_id.id if line.product_id else 0,
                    'productName': pname,
                    'lines': [],
                    'totalQty': 0.0,
                    'selectedCount': 0,
                    'lineCount': 0,
                }

            has_target = bool(line.target_lot_id)

            ld = {
                'dbId': line.id,
                'productId': line.product_id.id if line.product_id else 0,
                'productName': line.product_id.display_name if line.product_id else '',
                'originLotId': line.origin_lot_id.id if line.origin_lot_id else 0,
                'originLotName': line.origin_lot_id.name if line.origin_lot_id else '',
                'originBloque': line.origin_bloque or '',
                'originAlto': line.origin_alto or '',
                'originAncho': line.origin_ancho or '',
                'qty': line.qty or 0.0,
                'targetLotId': line.target_lot_id.id if line.target_lot_id else 0,
                'targetLotName': line.target_lot_id.name if line.target_lot_id else '',
                'targetBloque': line.target_bloque or '',
                'targetQty': line.target_qty or 0.0,
                'pickingId': line.picking_id.id if line.picking_id else 0,
                'moveLineId': line.move_line_id.id if line.move_line_id else 0,
                'saleLineId': line.sale_line_id.id if line.sale_line_id else 0,
            }

            current_group['lines'].append(ld)
            current_group['lineCount'] += 1
            current_group['totalQty'] += line.qty or 0.0

            if has_target:
                current_group['selectedCount'] += 1

        if current_group and current_group['lines']:
            groups.append(current_group)

        return groups

    def _safe_quant_available_qty(self, quant):
        if not quant:
            return 0.0

        if 'available_quantity' in quant._fields:
            return quant.available_quantity or 0.0

        return (quant.quantity or 0.0) - (quant.reserved_quantity or 0.0)

    def _find_available_target_quant(self, target_lot, product):
        Quant = self.env['stock.quant']

        quants = Quant.search([
            ('lot_id', '=', target_lot.id),
            ('product_id', '=', product.id),
            ('location_id.usage', '=', 'internal'),
            ('quantity', '>', 0),
        ], order='quantity desc, id asc')

        for quant in quants:
            if self._safe_quant_available_qty(quant) > 0:
                return quant

        return Quant.browse()

    def _get_move_line_qty(self, move_line):
        return (
            move_line.quantity
            or getattr(move_line, 'reserved_uom_qty', 0.0)
            or getattr(move_line, 'qty_done', 0.0)
            or 0.0
        )

    def _write_move_line_swap_values(self, move_line, target_lot, target_location, qty):
        vals = {
            'lot_id': target_lot.id,
            'location_id': target_location.id,
        }

        if 'quantity' in move_line._fields:
            vals['quantity'] = qty
        elif 'qty_done' in move_line._fields:
            vals['qty_done'] = qty

        move_line.write(vals)

    def _get_swap_lines_from_widget_selections(self):
        self.ensure_one()

        try:
            payload = json.loads(self.widget_selections or '[]')
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = []

        if not isinstance(payload, list):
            return []

        result = []

        for item in payload:
            if not isinstance(item, dict):
                continue

            target_lot_id = int(item.get('targetLotId') or 0)
            move_line_id = int(item.get('moveLineId') or 0)

            if not target_lot_id or not move_line_id:
                continue

            move_line = self.env['stock.move.line'].browse(move_line_id)
            if not move_line.exists():
                continue

            target_lot = self.env['stock.lot'].browse(target_lot_id)
            if not target_lot.exists():
                continue

            origin_lot_id = int(item.get('originLotId') or 0)
            origin_lot = (
                self.env['stock.lot'].browse(origin_lot_id)
                if origin_lot_id
                else move_line.lot_id
            )

            product_id = int(item.get('productId') or 0)
            product = (
                self.env['product.product'].browse(product_id)
                if product_id
                else move_line.product_id
            )

            sale_line_id = int(item.get('saleLineId') or 0)
            sale_line = (
                self.env['sale.order.line'].browse(sale_line_id)
                if sale_line_id
                else move_line.move_id.sale_line_id
            )

            qty = float(
                item.get('qty')
                or self._get_move_line_qty(move_line)
                or move_line.move_id.product_uom_qty
                or 0.0
            )

            target_qty = float(item.get('targetQty') or 0.0)

            result.append({
                'move_line': move_line,
                'origin_lot': origin_lot,
                'target_lot': target_lot,
                'product': product,
                'sale_line': sale_line,
                'qty': qty,
                'target_qty': target_qty,
            })

        return result

    def _get_swap_lines_from_db_lines(self):
        self.ensure_one()

        result = []

        lines = self.line_ids.filtered(
            lambda l: l.display_type != 'line_section' and l.target_lot_id
        )

        for line in lines:
            move_line = line.move_line_id

            if not move_line:
                continue

            result.append({
                'move_line': move_line,
                'origin_lot': line.origin_lot_id or move_line.lot_id,
                'target_lot': line.target_lot_id,
                'product': line.product_id or move_line.product_id,
                'sale_line': line.sale_line_id or move_line.move_id.sale_line_id,
                'qty': line.qty or self._get_move_line_qty(move_line) or move_line.move_id.product_uom_qty or 0.0,
                'target_qty': line.target_qty or 0.0,
            })

        return result

    def _get_pending_redelivery_docs_for_picking(self, picking):
        self.ensure_one()

        if not picking:
            return self.env['sale.delivery.document']

        return self.env['sale.delivery.document'].search([
            ('sale_order_id', '=', self.sale_order_id.id),
            ('document_type', '=', 'redelivery'),
            ('state', 'in', ('draft', 'prepared')),
            ('picking_id', '=', picking.id),
        ])

    def _find_redelivery_doc_line_candidates(
        self,
        doc,
        move_line,
        origin_lot,
        target_lot,
        product,
        sale_line,
    ):
        candidates = doc.line_ids.filtered(
            lambda l: l.product_id == product
            and (
                (l.move_line_id and l.move_line_id == move_line)
                or (
                    l.move_id
                    and l.move_id == move_line.move_id
                    and l.lot_id == origin_lot
                )
                or (
                    not l.move_line_id
                    and l.lot_id == origin_lot
                    and (not sale_line or l.sale_line_id == sale_line)
                )
                or (
                    not l.move_line_id
                    and l.lot_id == target_lot
                    and (not sale_line or l.sale_line_id == sale_line)
                )
            )
        )

        if candidates:
            return candidates.sorted(lambda l: l.id)

        return self.env['sale.delivery.document.line']

    def _sync_pending_redelivery_doc_after_swap(
        self,
        move_line,
        origin_lot,
        target_lot,
        target_quant,
        replacement_qty,
        product,
        sale_line,
    ):
        """
        Sincroniza el documento SOM de reentrega pendiente con el picking.

        Esta es la corrección crítica:
        - No se agrega una línea nueva junto a la anterior.
        - Se reemplaza la línea vieja por el lote nuevo.
        - La cantidad queda como la del lote nuevo, no como suma.
        """
        docs = self._get_pending_redelivery_docs_for_picking(move_line.picking_id)

        if not docs:
            return self.env['sale.delivery.document']

        touched_docs = self.env['sale.delivery.document']

        for doc in docs:
            candidates = self._find_redelivery_doc_line_candidates(
                doc,
                move_line,
                origin_lot,
                target_lot,
                product,
                sale_line,
            )

            vals = {
                'product_id': product.id,
                'lot_id': target_lot.id,
                'qty_selected': replacement_qty,
                'qty_done': 0.0,
                'qty_returned': 0.0,
                'sale_line_id': sale_line.id if sale_line else False,
                'move_id': move_line.move_id.id if move_line.move_id else False,
                'move_line_id': move_line.id,
                'source_location_id': target_quant.location_id.id if target_quant else False,
            }

            if candidates:
                keep = candidates[0]
                old_name = keep.lot_id.name if keep.lot_id else ''

                keep.write(vals)

                duplicates = candidates - keep
                if duplicates:
                    duplicates.unlink()

                doc.message_post(body=_(
                    'Swap aplicado en reentrega pendiente: %s → %s. '
                    'La línea fue reemplazada, no sumada. Cantidad nueva: %.2f.'
                ) % (
                    old_name or (origin_lot.name if origin_lot else 'S/L'),
                    target_lot.name,
                    replacement_qty,
                ))
            else:
                self.env['sale.delivery.document.line'].create(dict(
                    vals,
                    document_id=doc.id,
                ))

                doc.message_post(body=_(
                    'Swap aplicado en reentrega pendiente: %s → %s. '
                    'Se creó una línea sincronizada porque no existía una línea SOM equivalente. '
                    'Cantidad nueva: %.2f.'
                ) % (
                    origin_lot.name if origin_lot else 'S/L',
                    target_lot.name,
                    replacement_qty,
                ))

            touched_docs |= doc

        return touched_docs

    def _sync_move_after_swap(self, move):
        if not move:
            return

        total_qty = 0.0

        for ml in move.move_line_ids:
            total_qty += self._get_move_line_qty(ml)

        if total_qty > 0:
            move.product_uom_qty = total_qty

    def _update_sale_line_lots_after_swap(self, sale_line, origin_lot, target_lot):
        if (
            not sale_line
            or not sale_line.exists()
            or not hasattr(sale_line, 'lot_ids')
        ):
            return

        commands = []

        if origin_lot and origin_lot in sale_line.lot_ids:
            commands.append((3, origin_lot.id))

        if target_lot and target_lot not in sale_line.lot_ids:
            commands.append((4, target_lot.id))

        if commands:
            sale_line.write({'lot_ids': commands})

    def action_confirm_swap(self):
        """
        Ejecuta el swap de lotes sobre pickings pendientes.

        Corrección principal:
        - Reemplaza el lote en stock.move.line.
        - Sincroniza la reentrega pendiente relacionada.
        - No crea documento artificial pick_ticket con origen + destino.
        - No suma lote anterior + lote nuevo.
        """
        self.ensure_one()

        lines_with_target = self._get_swap_lines_from_widget_selections()

        if not lines_with_target:
            lines_with_target = self._get_swap_lines_from_db_lines()

        if not lines_with_target:
            raise UserError(_(
                'Seleccione al menos un lote destino para ejecutar el swap.'
            ))

        processed = 0
        touched_redeliveries = self.env['sale.delivery.document']

        for data in lines_with_target:
            move_line = data.get('move_line')
            origin_lot = data.get('origin_lot')
            target_lot = data.get('target_lot')
            product = data.get('product')
            sale_line = data.get('sale_line')
            original_qty = data.get('qty') or 0.0

            if not move_line or not move_line.exists():
                raise UserError(_(
                    'No se encontró la línea de movimiento pendiente para ejecutar el swap.'
                ))

            move = move_line.move_id
            move_state = move.state if move else False

            if move_state not in ('assigned', 'confirmed'):
                raise UserError(_(
                    'No se puede hacer swap sobre el lote %s porque el movimiento ya no está pendiente. Estado actual: %s.'
                ) % (
                    origin_lot.name if origin_lot else 'S/L',
                    move_state or 'N/A',
                ))

            if (
                move_line.picking_id
                and move_line.picking_id.state not in ('assigned', 'confirmed', 'waiting')
            ):
                raise UserError(_(
                    'No se puede hacer swap sobre el picking %s porque ya no está pendiente. Estado actual: %s.'
                ) % (
                    move_line.picking_id.name,
                    move_line.picking_id.state,
                ))

            if not origin_lot or not origin_lot.exists():
                raise UserError(_(
                    'La línea seleccionada no tiene lote origen. No se puede ejecutar el swap.'
                ))

            if not target_lot or not target_lot.exists():
                raise UserError(_(
                    'La línea seleccionada no tiene lote destino. No se puede ejecutar el swap.'
                ))

            if not product or not product.exists():
                raise UserError(_(
                    'La línea seleccionada no tiene producto válido. No se puede ejecutar el swap.'
                ))

            if origin_lot.id == target_lot.id:
                raise UserError(_(
                    'El lote origen y destino no pueden ser el mismo (%s).'
                ) % origin_lot.name)

            if target_lot.product_id and target_lot.product_id != product:
                raise UserError(_(
                    'El lote destino %s pertenece al producto %s, pero se esperaba %s.'
                ) % (
                    target_lot.name,
                    target_lot.product_id.display_name,
                    product.display_name,
                ))

            target_quant = self._find_available_target_quant(target_lot, product)

            if not target_quant:
                raise UserError(_(
                    'El lote destino %s no tiene stock interno disponible.'
                ) % target_lot.name)

            available_qty = self._safe_quant_available_qty(target_quant)

            if available_qty <= 0:
                raise UserError(_(
                    'El lote destino %s existe, pero no tiene cantidad disponible. Cantidad: %.2f, Reservado: %.2f.'
                ) % (
                    target_lot.name,
                    target_quant.quantity or 0.0,
                    target_quant.reserved_quantity or 0.0,
                ))

            if hasattr(target_lot, 'hold_order_ids'):
                active_holds = target_lot.hold_order_ids.filtered(
                    lambda h: h.state == 'active'
                    and h.sale_order_id != self.sale_order_id
                )
                if active_holds:
                    raise UserError(_(
                        'El lote %s está apartado en otra orden (%s).'
                    ) % (
                        target_lot.name,
                        active_holds[0].sale_order_id.name,
                    ))

            replacement_qty = available_qty or data.get('target_qty') or original_qty

            if replacement_qty <= 0:
                raise UserError(_(
                    'No se pudo determinar la cantidad del lote destino %s.'
                ) % target_lot.name)

            old_lot_name = origin_lot.name

            self._write_move_line_swap_values(
                move_line=move_line,
                target_lot=target_lot,
                target_location=target_quant.location_id,
                qty=replacement_qty,
            )

            self._sync_move_after_swap(move)

            touched_redeliveries |= self._sync_pending_redelivery_doc_after_swap(
                move_line=move_line,
                origin_lot=origin_lot,
                target_lot=target_lot,
                target_quant=target_quant,
                replacement_qty=replacement_qty,
                product=product,
                sale_line=sale_line,
            )

            self._update_sale_line_lots_after_swap(
                sale_line=sale_line,
                origin_lot=origin_lot,
                target_lot=target_lot,
            )

            self.sale_order_id.message_post(body=_(
                'Swap ejecutado: %s → %s en picking %s. '
                'Cantidad original: %.2f. Cantidad reemplazo: %.2f. '
                'El lote fue reemplazado, no sumado.'
            ) % (
                old_lot_name,
                target_lot.name,
                move_line.picking_id.name if move_line.picking_id else 'N/A',
                original_qty,
                replacement_qty,
            ))

            processed += 1

            _logger.info(
                '[SWAP] Ejecutado correctamente: %s → %s en picking %s. qty %.2f → %.2f',
                old_lot_name,
                target_lot.name,
                move_line.picking_id.name if move_line.picking_id else 'N/A',
                original_qty,
                replacement_qty,
            )

        self.write({'widget_selections': '[]'})

        if touched_redeliveries:
            message = _(
                '%d swap(s) realizados exitosamente. '
                'Se actualizaron %d reentrega(s) pendiente(s) reemplazando lote/cantidad.'
            ) % (processed, len(touched_redeliveries))
        else:
            message = _('%d swap(s) realizados exitosamente.') % processed

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Swap Completado'),
                'message': message,
                'type': 'success',
                'sticky': False,
            },
        }


class SaleSwapWizardLine(models.TransientModel):
    _name = 'sale.swap.wizard.line'
    _description = 'Línea de Swap'
    _order = 'sequence, id'

    wizard_id = fields.Many2one(
        'sale.swap.wizard',
        ondelete='cascade',
        required=True,
    )
    sequence = fields.Integer(default=10)

    display_type = fields.Selection([
        ('line_section', 'Section'),
    ], string='Tipo de Fila')

    section_name = fields.Char(string='Nombre de Sección')

    product_id = fields.Many2one(
        'product.product',
        string='Producto',
        readonly=True,
    )
    origin_lot_id = fields.Many2one(
        'stock.lot',
        string='Lote Actual',
        readonly=True,
    )
    target_lot_id = fields.Many2one(
        'stock.lot',
        string='Lote Nuevo',
        domain="[('product_id', '=', product_id), ('id', '!=', origin_lot_id)]",
    )

    qty = fields.Float(string='m² Actual', readonly=True)

    move_line_id = fields.Many2one(
        'stock.move.line',
        string='Move Line',
    )
    picking_id = fields.Many2one(
        'stock.picking',
        string='Picking',
    )
    sale_line_id = fields.Many2one(
        'sale.order.line',
        string='Línea de Venta',
    )

    origin_bloque = fields.Char(string='Bloque', readonly=True)
    origin_atado = fields.Char(string='Atado', readonly=True)
    origin_alto = fields.Char(string='Alto', readonly=True)
    origin_ancho = fields.Char(string='Ancho', readonly=True)
    origin_grosor = fields.Char(string='Grosor', readonly=True)

    target_bloque = fields.Char(
        string='Bloque Nuevo',
        compute='_compute_target_info',
        readonly=True,
    )
    target_atado = fields.Char(
        string='Atado Nuevo',
        compute='_compute_target_info',
        readonly=True,
    )
    target_alto = fields.Char(
        string='Alto Nuevo',
        compute='_compute_target_info',
        readonly=True,
    )
    target_ancho = fields.Char(
        string='Ancho Nuevo',
        compute='_compute_target_info',
        readonly=True,
    )
    target_grosor = fields.Char(
        string='Grosor Nuevo',
        compute='_compute_target_info',
        readonly=True,
    )
    target_qty = fields.Float(
        string='m² Nuevo',
        compute='_compute_target_info',
        readonly=True,
    )

    @api.depends('target_lot_id')
    def _compute_target_info(self):
        Quant = self.env['stock.quant']

        for line in self:
            line.target_bloque = ''
            line.target_atado = ''
            line.target_alto = ''
            line.target_ancho = ''
            line.target_grosor = ''
            line.target_qty = 0.0

            if line.display_type == 'line_section':
                continue

            lot = line.target_lot_id

            if not lot:
                continue

            line.target_bloque = lot.x_bloque if hasattr(lot, 'x_bloque') else ''
            line.target_atado = lot.x_atado if hasattr(lot, 'x_atado') else ''
            line.target_alto = str(lot.x_alto) if hasattr(lot, 'x_alto') and lot.x_alto else ''
            line.target_ancho = str(lot.x_ancho) if hasattr(lot, 'x_ancho') and lot.x_ancho else ''
            line.target_grosor = str(lot.x_grosor) if hasattr(lot, 'x_grosor') and lot.x_grosor else ''

            quant = Quant.search([
                ('lot_id', '=', lot.id),
                ('product_id', '=', line.product_id.id),
                ('location_id.usage', '=', 'internal'),
                ('quantity', '>', 0),
            ], order='quantity desc, id asc', limit=1)

            if quant:
                if 'available_quantity' in quant._fields:
                    line.target_qty = quant.available_quantity or quant.quantity or 0.0
                else:
                    line.target_qty = (
                        (quant.quantity or 0.0)
                        - (quant.reserved_quantity or 0.0)
                    )