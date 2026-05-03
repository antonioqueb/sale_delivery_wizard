from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class SaleDeliveryDocument(models.Model):
    _name = 'sale.delivery.document'
    _description = 'Documento de Entrega/Devolución'
    _order = 'create_date desc'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(
        string='Número', readonly=True, copy=False, default='/')
    document_type = fields.Selection([
        ('pick_ticket', 'Pick Ticket'),
        ('remission', 'Remisión'),
        ('return', 'Devolución'),
        ('redelivery', 'Reentrega'),
    ], string='Tipo', required=True, readonly=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('prepared', 'Preparado'),
        ('confirmed', 'Confirmado'),
        ('cancelled', 'Cancelado'),
    ], string='Estado', default='draft', required=True, tracking=True)

    sale_order_id = fields.Many2one(
        'sale.order', string='Orden de Venta', required=True,
        ondelete='cascade', index=True)
    partner_id = fields.Many2one(
        related='sale_order_id.partner_id', store=True, string='Cliente')
    picking_id = fields.Many2one(
        'stock.picking', string='Picking Asociado')
    out_picking_id = fields.Many2one(
        'stock.picking', string='Picking de Salida (OUT)')
    return_picking_id = fields.Many2one(
        'stock.picking', string='Picking de Devolución')

    remission_number = fields.Char(
        string='Número de Remisión', readonly=True, copy=False)
    delivery_address = fields.Text(string='Dirección de Entrega')
    special_instructions = fields.Text(string='Instrucciones Especiales')
    delivery_date = fields.Datetime(string='Fecha de Entrega')

    signed_by = fields.Char(string='Firmado por')
    signature_image = fields.Binary(string='Firma', attachment=True)

    return_reason_id = fields.Many2one(
        'sale.return.reason', string='Motivo de Devolución')
    return_action = fields.Selection([
        ('reagendar', 'Reagendar'),
        ('reponer', 'Reponer'),
        ('finiquitar', 'Finiquitar'),
    ], string='Acción de Devolución')

    attachment_ids = fields.Many2many(
        'ir.attachment', string='Evidencia Fotográfica')
    photo_count = fields.Integer(
        compute='_compute_photo_count', string='Fotos')

    line_ids = fields.One2many(
        'sale.delivery.document.line', 'document_id', string='Líneas')

    total_qty = fields.Float(
        compute='_compute_totals', string='Cantidad Total')

    @api.depends('attachment_ids')
    def _compute_photo_count(self):
        for rec in self:
            rec.photo_count = len(rec.attachment_ids)

    @api.depends('line_ids.qty_selected', 'line_ids.qty_done', 'line_ids.qty_returned')
    def _compute_totals(self):
        for rec in self:
            if rec.document_type == 'return':
                rec.total_qty = (
                    sum(rec.line_ids.mapped('qty_returned'))
                    or sum(rec.line_ids.mapped('qty_done'))
                    or sum(rec.line_ids.mapped('qty_selected'))
                )
            elif rec.document_type in ('remission', 'redelivery'):
                rec.total_qty = (
                    sum(rec.line_ids.mapped('qty_done'))
                    or sum(rec.line_ids.mapped('qty_selected'))
                )
            else:
                rec.total_qty = sum(rec.line_ids.mapped('qty_selected'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            doc_type = vals.get('document_type', '')
            if doc_type == 'pick_ticket' and vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'sale.delivery.pick.ticket') or '/'
            elif doc_type == 'remission' and vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code(
                    'sale.delivery.remission') or '/'
                vals['name'] = seq
                vals['remission_number'] = seq
            elif doc_type == 'return' and vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'sale.delivery.return') or '/'
            elif doc_type == 'redelivery' and vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'sale.delivery.redelivery') or '/'
        return super().create(vals_list)

    def action_prepare(self):
        self.filtered(lambda d: d.state == 'draft').write({'state': 'prepared'})
        return True

    def action_confirm(self):
        for doc in self.filtered(lambda d: d.state in ('draft', 'prepared')):
            if doc.document_type == 'remission':
                doc._action_confirm_remission()
            elif doc.document_type == 'return':
                doc._action_confirm_return()
            elif doc.document_type == 'redelivery':
                doc._action_confirm_redelivery()

            doc.write({
                'state': 'confirmed',
                'delivery_date': fields.Datetime.now(),
            })

            if doc.document_type == 'return':
                doc._som_finalize_return_document_quantities()

            if doc.document_type in ('remission', 'return', 'redelivery'):
                doc._som_force_sale_delivery_recompute()

        return True

    def action_cancel(self):
        self.filtered(
            lambda d: d.state != 'confirmed'
        ).write({'state': 'cancelled'})
        return True

    def action_edit_in_wizard(self):
        self.ensure_one()
        if self.document_type != 'pick_ticket':
            raise UserError(_('Solo se pueden editar Pick Tickets.'))
        if self.state != 'prepared':
            raise UserError(_(
                'Solo se pueden editar Pick Tickets en estado Preparado '
                '(estado actual: %s).'
            ) % self.state)
        return {
            'name': _('Editar Pick Ticket %s') % self.name,
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_sale_order_id': self.sale_order_id.id,
                'default_editing_pick_ticket_id': self.id,
                'active_id': self.sale_order_id.id,
            },
        }

    def action_cancel_pick_ticket(self):
        self.ensure_one()
        if self.document_type != 'pick_ticket':
            raise UserError(_('Esta acción solo aplica a Pick Tickets.'))
        if self.state == 'confirmed':
            raise UserError(_('No se puede cancelar un Pick Ticket confirmado.'))
        self.write({'state': 'cancelled'})
        self.message_post(body=_(
            'Pick Ticket cancelado por %s — lotes liberados.'
        ) % self.env.user.name)
        return True

    # ═══════════════════════════════════════════════════════════════════
    # Helpers de cantidades compatibles con Odoo 16/17/18/19
    # ═══════════════════════════════════════════════════════════════════

    def _som_set_move_line_done_qty(self, move_line, qty):
        vals = {}

        if 'quantity' in move_line._fields:
            vals['quantity'] = qty
        elif 'qty_done' in move_line._fields:
            vals['qty_done'] = qty

        if vals:
            move_line.write(vals)

    def _som_get_move_line_done_qty(self, move_line):
        if not move_line:
            return 0.0
        if 'quantity' in move_line._fields:
            return move_line.quantity or 0.0
        if 'qty_done' in move_line._fields:
            return move_line.qty_done or 0.0
        return 0.0

    def _som_get_move_line_pending_qty(self, move_line):
        if not move_line:
            return 0.0
        if 'quantity' in move_line._fields and move_line.quantity:
            return move_line.quantity or 0.0
        if 'reserved_uom_qty' in move_line._fields and move_line.reserved_uom_qty:
            return move_line.reserved_uom_qty or 0.0
        if 'qty_done' in move_line._fields and move_line.qty_done:
            return move_line.qty_done or 0.0
        return 0.0

    def _som_get_move_done_qty(self, move):
        qty = 0.0
        for ml in move.move_line_ids:
            qty += self._som_get_move_line_done_qty(ml)
        return qty or move.product_uom_qty or 0.0

    # ═══════════════════════════════════════════════════════════════════
    # Validación parcial de pickings
    # ═══════════════════════════════════════════════════════════════════

    def _som_process_validate_result(self, picking, result, label=None):
        label = label or _('transferencia')

        if isinstance(result, dict):
            res_model = result.get('res_model')

            if res_model == 'stock.backorder.confirmation':
                backorder_wiz = self.env['stock.backorder.confirmation'].with_context(
                    button_validate_picking_ids=picking.ids,
                ).create({
                    'pick_ids': [(4, picking.id)],
                })
                backorder_wiz.process()

            elif res_model == 'stock.immediate.transfer':
                immediate_wiz = self.env['stock.immediate.transfer'].with_context(
                    button_validate_picking_ids=picking.ids,
                ).create({
                    'pick_ids': [(4, picking.id)],
                })
                immediate_wiz.process()

        picking.invalidate_recordset()

        if picking.state != 'done':
            raise UserError(_(
                'La %s %s no quedó validada. Estado actual: %s. '
                'Revise cantidades, lote y disponibilidad antes de confirmar.'
            ) % (label, picking.name, picking.state))

        return True

    def _validate_picking_partial(self, picking, doc_ml_ids, doc_ml_qty):
        if picking.state == 'done':
            _logger.info('Picking %s already done, skipping.', picking.name)
            return True

        if picking.state in ('draft', 'confirmed', 'waiting'):
            if picking.state == 'draft':
                picking.action_confirm()
            picking.action_assign()

        if picking.state not in ('assigned', 'confirmed'):
            raise UserError(_(
                'El picking %s no está en estado válido para validar parcialmente (estado: %s).'
            ) % (picking.name, picking.state))

        has_positive_qty = any(
            (doc_ml_qty.get(ml_id, 0.0) or 0.0) > 0
            for ml_id in doc_ml_ids
        )
        if not has_positive_qty:
            raise UserError(_(
                'No puedes validar una transferencia con cantidad cero. '
                'Establece cantidades primero.'
            ))

        for move in picking.move_ids:
            for ml in move.move_line_ids:
                if ml.id in doc_ml_ids:
                    self._som_set_move_line_done_qty(ml, doc_ml_qty[ml.id])
                    _logger.info(
                        'Picking %s ML %s (lot %s): qty set to %s',
                        picking.name,
                        ml.id,
                        ml.lot_id.name if ml.lot_id else 'N/A',
                        doc_ml_qty[ml.id],
                    )
                else:
                    self._som_set_move_line_done_qty(ml, 0.0)

        result = picking.with_context(skip_backorder=False).button_validate()
        return self._som_process_validate_result(
            picking,
            result,
            label=_('transferencia'),
        )

    def _resolve_doc_move_lines_for_picking(self, picking):
        self.ensure_one()

        doc_ml_ids = set()
        doc_ml_qty = {}
        doc_lot_ids = set()
        doc_lot_qty = {}

        for doc_line in self.line_ids.filtered(lambda l: l.qty_selected > 0):
            requested_qty = doc_line.qty_selected or 0.0
            if requested_qty <= 0:
                continue

            if doc_line.lot_id:
                doc_lot_ids.add(doc_line.lot_id.id)
                doc_lot_qty[doc_line.lot_id.id] = (
                    doc_lot_qty.get(doc_line.lot_id.id, 0.0) + requested_qty
                )

            if doc_line.move_line_id and doc_line.move_line_id.picking_id == picking:
                ml = doc_line.move_line_id
                doc_ml_ids.add(ml.id)
                doc_ml_qty[ml.id] = doc_ml_qty.get(ml.id, 0.0) + requested_qty
                continue

            candidate_mls = picking.move_ids.move_line_ids.filtered(
                lambda ml: ml.product_id == doc_line.product_id
                and ml.lot_id == doc_line.lot_id
                and ml.move_id.state not in ('done', 'cancel')
            )

            if not candidate_mls and doc_line.move_id:
                candidate_mls = doc_line.move_id.move_line_ids.filtered(
                    lambda ml: ml.picking_id == picking
                    and ml.move_id.state not in ('done', 'cancel')
                )

            if not candidate_mls:
                _logger.warning(
                    '[REMISSION] No se encontró move line para doc_line=%s, product=%s, lot=%s, qty=%s en picking %s',
                    doc_line.id,
                    doc_line.product_id.display_name if doc_line.product_id else 'N/A',
                    doc_line.lot_id.name if doc_line.lot_id else 'N/A',
                    requested_qty,
                    picking.name,
                )
                continue

            remaining = requested_qty
            for ml in candidate_mls.sorted(lambda m: (m.id,)):
                if remaining <= 0:
                    break

                base_qty = (
                    self._som_get_move_line_pending_qty(ml)
                    or ml.move_id.product_uom_qty
                    or 0.0
                )

                assign_qty = min(remaining, base_qty) if base_qty > 0 else remaining
                if assign_qty <= 0:
                    continue

                doc_ml_ids.add(ml.id)
                doc_ml_qty[ml.id] = doc_ml_qty.get(ml.id, 0.0) + assign_qty
                remaining -= assign_qty

            if remaining > 0 and candidate_mls:
                first_ml = candidate_mls[0]
                doc_ml_ids.add(first_ml.id)
                doc_ml_qty[first_ml.id] = doc_ml_qty.get(first_ml.id, 0.0) + remaining
                _logger.info(
                    '[REMISSION] Remanente %s asignado al primer move line %s para lote %s',
                    remaining,
                    first_ml.id,
                    doc_line.lot_id.name if doc_line.lot_id else 'N/A',
                )

        return doc_ml_ids, doc_ml_qty, doc_lot_ids, doc_lot_qty

    # ═══════════════════════════════════════════════════════════════════
    # Remisión
    # ═══════════════════════════════════════════════════════════════════

    def _action_confirm_remission(self):
        self.ensure_one()
        if not self.picking_id:
            raise UserError(_('No hay picking asociado para confirmar la remisión.'))

        picking = self.picking_id

        if picking.state in ('draft', 'confirmed', 'waiting'):
            if picking.state == 'draft':
                picking.action_confirm()
            picking.action_assign()

        doc_ml_ids, doc_ml_qty, doc_lot_ids, doc_lot_qty = self._resolve_doc_move_lines_for_picking(picking)

        if not doc_ml_ids:
            raise UserError(_(
                'No se pudieron resolver las líneas de movimiento a partir de los lotes seleccionados. '
                'Verifica que los lotes estén realmente asignados en el picking antes de remisionar.'
            ))

        self._validate_picking_partial(picking, doc_ml_ids, doc_ml_qty)

        out_picking = self._find_out_picking_for_lots(doc_lot_ids)
        if out_picking:
            self.out_picking_id = out_picking.id

            if out_picking.state == 'done':
                _logger.info('OUT %s already done.', out_picking.name)
                return True

            if out_picking.state in ('draft', 'confirmed', 'waiting'):
                if out_picking.state == 'draft':
                    out_picking.action_confirm()
                out_picking.action_assign()

            if out_picking.state == 'assigned':
                out_doc_ml_ids = set()
                out_doc_ml_qty = {}

                for move in out_picking.move_ids:
                    for ml in move.move_line_ids:
                        lot_id = ml.lot_id.id if ml.lot_id else False
                        if lot_id and lot_id in doc_lot_ids:
                            out_doc_ml_ids.add(ml.id)
                            out_doc_ml_qty[ml.id] = doc_lot_qty.get(
                                lot_id,
                                self._som_get_move_line_pending_qty(ml),
                            )

                if out_doc_ml_ids:
                    self._validate_picking_partial(
                        out_picking,
                        out_doc_ml_ids,
                        out_doc_ml_qty,
                    )
            else:
                _logger.warning(
                    'OUT %s not assignable (state: %s).',
                    out_picking.name,
                    out_picking.state,
                )
        else:
            _logger.info(
                'No OUT picking found. Single-step or push rule not triggered.'
            )

        return True

    def _find_out_picking_for_lots(self, lot_ids):
        if not lot_ids:
            return False

        order = self.sale_order_id
        out_pickings = order.picking_ids.filtered(
            lambda p: p.picking_type_code == 'outgoing'
            and p.state not in ('done', 'cancel')
        )

        for out_pick in out_pickings:
            pick_lot_ids = set(out_pick.move_line_ids.mapped('lot_id').ids)
            if pick_lot_ids & lot_ids:
                return out_pick

        if self.picking_id:
            for move in self.picking_id.move_ids:
                for dest_move in move.move_dest_ids:
                    if (
                        dest_move.picking_id
                        and dest_move.picking_id.picking_type_code == 'outgoing'
                        and dest_move.picking_id.state not in ('done', 'cancel')
                    ):
                        return dest_move.picking_id

        return False

    # ═══════════════════════════════════════════════════════════════════
    # Devolución
    # ═══════════════════════════════════════════════════════════════════

    def _som_resolve_return_source_for_remission_line(self, remission, origin_line):
        StockMove = self.env['stock.move']
        StockMoveLine = self.env['stock.move.line']

        product = origin_line.product_id
        lot = origin_line.lot_id
        sale_line = origin_line.sale_line_id
        original_move = origin_line.move_id
        order = remission.sale_order_id or self.sale_order_id

        if not product:
            return {
                'move': StockMove.browse(),
                'move_line': StockMoveLine.browse(),
                'picking': self.env['stock.picking'].browse(),
            }

        candidates = StockMove.browse()

        if original_move and original_move.state == 'done':
            if (
                original_move.location_dest_id.usage == 'customer'
                or original_move.picking_id.picking_type_code == 'outgoing'
            ):
                candidates |= original_move

            candidates |= original_move.move_dest_ids.filtered(
                lambda m: m.state == 'done'
                and m.product_id == product
                and (
                    m.location_dest_id.usage == 'customer'
                    or m.picking_id.picking_type_code == 'outgoing'
                )
            )

        candidate_pickings = self.env['stock.picking']

        if remission.out_picking_id:
            candidate_pickings |= remission.out_picking_id

        if remission.picking_id and remission.picking_id.picking_type_code == 'outgoing':
            candidate_pickings |= remission.picking_id

        for picking in candidate_pickings.filtered(lambda p: p.state == 'done'):
            candidates |= picking.move_ids.filtered(
                lambda m: m.state == 'done'
                and m.product_id == product
                and (
                    m.location_dest_id.usage == 'customer'
                    or picking.picking_type_code == 'outgoing'
                )
            )

        if not candidates and order and sale_line:
            candidates |= StockMove.search([
                ('sale_line_id', '=', sale_line.id),
                ('product_id', '=', product.id),
                ('state', '=', 'done'),
                ('location_dest_id.usage', '=', 'customer'),
            ])

        candidates = candidates.filtered(lambda m: m.product_id == product)

        if sale_line:
            strict = candidates.filtered(lambda m: m.sale_line_id == sale_line)
            if strict:
                candidates = strict

        for move in candidates.sorted(lambda m: (m.picking_id.id or 0, m.id)):
            move_lines = move.move_line_ids.filtered(
                lambda ml: ml.product_id == product
                and self._som_get_move_line_done_qty(ml) > 0
                and (not lot or ml.lot_id == lot)
            )
            if move_lines:
                ml = move_lines.sorted(lambda l: l.id)[0]
                return {
                    'move': move,
                    'move_line': ml,
                    'picking': move.picking_id,
                }

        if candidates:
            move = candidates.sorted(lambda m: (m.picking_id.id or 0, m.id))[0]
            return {
                'move': move,
                'move_line': move.move_line_ids[:1],
                'picking': move.picking_id,
            }

        return {
            'move': StockMove.browse(),
            'move_line': StockMoveLine.browse(),
            'picking': self.env['stock.picking'].browse(),
        }

    def _som_create_or_update_return_move_line(self, move, lot, qty):
        StockMoveLine = self.env['stock.move.line']

        existing = move.move_line_ids.filtered(
            lambda ml: ml.product_id == move.product_id
            and (
                (lot and ml.lot_id == lot)
                or (not lot and not ml.lot_id)
                or not ml.lot_id
            )
        )[:1]

        vals = {
            'move_id': move.id,
            'picking_id': move.picking_id.id,
            'product_id': move.product_id.id,
            'location_id': move.location_id.id,
            'location_dest_id': move.location_dest_id.id,
        }

        if lot:
            vals['lot_id'] = lot.id

        if 'quantity' in StockMoveLine._fields:
            vals['quantity'] = qty
        elif 'qty_done' in StockMoveLine._fields:
            vals['qty_done'] = qty

        if existing:
            existing.write(vals)
            return existing

        return StockMoveLine.create(vals)

    def _som_get_return_source_move_for_doc_line(self, doc_line):
        source_move = doc_line.move_id

        if source_move and source_move.state == 'done':
            if (
                source_move.location_dest_id.usage == 'customer'
                or source_move.picking_id.picking_type_code == 'outgoing'
            ):
                return source_move

        origin_line = doc_line.origin_remission_line_id
        remission = doc_line.origin_remission_id or origin_line.origin_remission_id

        if origin_line and remission:
            if hasattr(self.sale_order_id, '_resolve_return_source_for_remission_line'):
                source = self.sale_order_id._resolve_return_source_for_remission_line(
                    remission,
                    origin_line,
                )
            else:
                source = self._som_resolve_return_source_for_remission_line(
                    remission,
                    origin_line,
                )

            resolved_move = source.get('move')
            resolved_ml = source.get('move_line')

            if resolved_move:
                vals = {'move_id': resolved_move.id}
                if resolved_ml:
                    vals['move_line_id'] = resolved_ml.id
                doc_line.write(vals)
                return resolved_move

        return source_move

    def _som_prepare_return_picking_quantities(self, picking):
        self.ensure_one()

        positive_lines = self.line_ids.filtered(
            lambda l: (l.qty_selected or 0.0) > 0
        )

        if not positive_lines:
            raise UserError(_(
                'No puedes confirmar una devolución sin cantidades positivas.'
            ))

        for ml in picking.move_line_ids:
            self._som_set_move_line_done_qty(ml, 0.0)

        aggregated = {}
        move_totals = {}

        for doc_line in positive_lines:
            qty = doc_line.qty_selected or 0.0
            if qty <= 0:
                continue

            source_move = self._som_get_return_source_move_for_doc_line(doc_line)

            if not source_move:
                raise UserError(_(
                    'No se encontró el movimiento original de salida para devolver %s.'
                ) % doc_line.product_id.display_name)

            target_moves = picking.move_ids.filtered(
                lambda m: m.origin_returned_move_id == source_move
                and m.product_id == doc_line.product_id
            )

            if not target_moves:
                target_moves = picking.move_ids.filtered(
                    lambda m: m.product_id == doc_line.product_id
                )

            if not target_moves:
                raise UserError(_(
                    'El picking de devolución %s no contiene movimiento de retorno para %s.'
                ) % (picking.name, doc_line.product_id.display_name))

            target_move = target_moves[0]

            if doc_line.sale_line_id and 'sale_line_id' in target_move._fields:
                target_move.sale_line_id = doc_line.sale_line_id.id

            if 'to_refund' in target_move._fields:
                target_move.to_refund = True

            lot = doc_line.lot_id

            key = (
                target_move.id,
                doc_line.product_id.id,
                lot.id if lot else 0,
                doc_line.sale_line_id.id if doc_line.sale_line_id else 0,
            )

            if key not in aggregated:
                aggregated[key] = {
                    'move': target_move,
                    'lot': lot,
                    'qty': 0.0,
                }

            aggregated[key]['qty'] += qty
            move_totals[target_move.id] = (
                move_totals.get(target_move.id, 0.0) + qty
            )

        for move in picking.move_ids:
            total = move_totals.get(move.id, 0.0)
            if total > 0:
                move.product_uom_qty = total

        for data in aggregated.values():
            self._som_create_or_update_return_move_line(
                data['move'],
                data['lot'],
                data['qty'],
            )

    def _som_finalize_return_document_quantities(self):
        self.ensure_one()

        for line in self.line_ids:
            qty = (
                line.qty_returned
                or line.qty_done
                or line.qty_selected
                or 0.0
            )

            if qty <= 0:
                continue

            line.write({
                'qty_done': qty,
                'qty_returned': qty,
            })

        origin_lines = self.line_ids.mapped('origin_remission_line_id')

        for origin_line in origin_lines:
            return_lines = self.env['sale.delivery.document.line'].search([
                ('origin_remission_line_id', '=', origin_line.id),
                ('document_id.document_type', '=', 'return'),
                ('document_id.state', '=', 'confirmed'),
            ]).filtered(
                lambda l: not l.document_id.return_picking_id
                or l.document_id.return_picking_id.state == 'done'
            )

            origin_line.qty_returned = sum(
                l.qty_returned or l.qty_done or l.qty_selected or 0.0
                for l in return_lines
            )

    def _som_force_sale_delivery_recompute(self):
        self.ensure_one()

        sale_lines = self.line_ids.mapped('sale_line_id')

        if not sale_lines:
            return

        for method_name in (
            '_compute_return_qty',
            '_compute_delivery_net',
            '_compute_pending_fulfillment',
            '_compute_delivery_status',
        ):
            if hasattr(sale_lines, method_name):
                getattr(sale_lines, method_name)()

        orders = sale_lines.mapped('order_id')
        if orders and hasattr(orders, '_compute_delivery_summary'):
            orders._compute_delivery_summary()

    def _action_confirm_return(self):
        self.ensure_one()

        if not self.return_picking_id:
            raise UserError(_('No hay picking de devolución asociado.'))

        picking = self.return_picking_id

        if picking.state == 'done':
            self._som_finalize_return_document_quantities()
            return True

        if picking.state in ('draft', 'confirmed', 'waiting'):
            if picking.state == 'draft':
                picking.action_confirm()
            picking.action_assign()

        self._som_prepare_return_picking_quantities(picking)

        result = picking.with_context(
            skip_backorder=True,
            cancel_backorder=True,
        ).button_validate()

        self._som_process_validate_result(
            picking,
            result,
            label=_('devolución'),
        )
        self._som_finalize_return_document_quantities()

        return True

    # ═══════════════════════════════════════════════════════════════════
    # Reentrega
    # ═══════════════════════════════════════════════════════════════════

    def _som_sync_redelivery_lines_from_picking(self):
        """
        Sincroniza una reentrega pendiente con su picking vivo.

        Caso corregido:
        - Reentrega pendiente con lote A.
        - Se hace swap A → B.
        - El picking ya tiene B, pero el documento SOM aún tenía A.
        - Al confirmar, parecía sumar A + B.

        Esta función reconstruye las líneas de la reentrega desde el picking
        antes de confirmar, por lo que queda solo el lote vigente.
        """
        for doc in self:
            if doc.document_type != 'redelivery':
                continue
            if doc.state == 'confirmed':
                continue
            if not doc.picking_id:
                continue

            picking = doc.picking_id
            existing_lines = doc.line_ids
            new_commands = []
            sequence = 10

            for move in picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
                move_lines = move.move_line_ids.filtered(
                    lambda ml: ml.product_id == move.product_id
                    and doc._som_get_move_line_pending_qty(ml) > 0
                )

                if not move_lines:
                    qty = move.product_uom_qty or 0.0
                    if qty <= 0:
                        continue

                    template_line = existing_lines.filtered(
                        lambda l: l.move_id == move
                        and l.product_id == move.product_id
                    )[:1]

                    new_commands.append((0, 0, {
                        'sequence': sequence,
                        'product_id': move.product_id.id,
                        'lot_id': False,
                        'qty_selected': qty,
                        'qty_done': 0.0,
                        'qty_returned': 0.0,
                        'sale_line_id': move.sale_line_id.id if move.sale_line_id else False,
                        'move_id': move.id,
                        'move_line_id': False,
                        'source_location_id': move.location_id.id if move.location_id else False,
                        'origin_remission_id': template_line.origin_remission_id.id if template_line and template_line.origin_remission_id else False,
                        'origin_remission_line_id': template_line.origin_remission_line_id.id if template_line and template_line.origin_remission_line_id else False,
                    }))
                    sequence += 10
                    continue

                for ml in move_lines:
                    qty = doc._som_get_move_line_pending_qty(ml)

                    if qty <= 0:
                        continue

                    template_line = existing_lines.filtered(
                        lambda l: (
                            (l.move_line_id and l.move_line_id == ml)
                            or (l.move_id == move and l.lot_id == ml.lot_id)
                            or (
                                l.product_id == ml.product_id
                                and l.sale_line_id == move.sale_line_id
                            )
                        )
                    )[:1]

                    new_commands.append((0, 0, {
                        'sequence': sequence,
                        'product_id': ml.product_id.id,
                        'lot_id': ml.lot_id.id if ml.lot_id else False,
                        'qty_selected': qty,
                        'qty_done': 0.0,
                        'qty_returned': 0.0,
                        'sale_line_id': move.sale_line_id.id if move.sale_line_id else False,
                        'move_id': move.id,
                        'move_line_id': ml.id,
                        'source_location_id': ml.location_id.id if ml.location_id else False,
                        'origin_remission_id': template_line.origin_remission_id.id if template_line and template_line.origin_remission_id else False,
                        'origin_remission_line_id': template_line.origin_remission_line_id.id if template_line and template_line.origin_remission_line_id else False,
                    }))
                    sequence += 10

            if new_commands:
                doc.line_ids.unlink()
                doc.write({'line_ids': new_commands})

    def _action_confirm_redelivery(self):
        self.ensure_one()
        if not self.picking_id:
            raise UserError(_('No hay picking asociado para confirmar la reentrega.'))

        picking = self.picking_id

        self._som_sync_redelivery_lines_from_picking()

        seq = self.env['ir.sequence'].next_by_code(
            'sale.delivery.remission') or '/'
        self.remission_number = seq

        if picking.state in ('draft', 'confirmed', 'waiting'):
            if picking.state == 'draft':
                picking.action_confirm()
            picking.action_assign()

        if picking.state == 'assigned':
            doc_ml_ids = set()
            doc_ml_qty = {}

            for doc_line in self.line_ids:
                if doc_line.qty_selected <= 0:
                    continue

                if doc_line.move_line_id and doc_line.move_line_id.picking_id == picking:
                    ml = doc_line.move_line_id
                    doc_ml_ids.add(ml.id)
                    doc_ml_qty[ml.id] = doc_ml_qty.get(ml.id, 0.0) + doc_line.qty_selected
                    continue

                for move in picking.move_ids:
                    for ml in move.move_line_ids:
                        if doc_line.lot_id and ml.lot_id == doc_line.lot_id:
                            doc_ml_ids.add(ml.id)
                            doc_ml_qty[ml.id] = doc_ml_qty.get(ml.id, 0.0) + doc_line.qty_selected
                        elif (
                            not doc_line.lot_id
                            and ml.product_id == doc_line.product_id
                        ):
                            doc_ml_ids.add(ml.id)
                            doc_ml_qty[ml.id] = doc_ml_qty.get(ml.id, 0.0) + doc_line.qty_selected

            if doc_ml_ids:
                self._validate_picking_partial(
                    picking,
                    doc_ml_ids,
                    doc_ml_qty,
                )
            else:
                all_ml_ids = set()
                all_ml_qty = {}

                for move in picking.move_ids:
                    for ml in move.move_line_ids:
                        qty = self._som_get_move_line_pending_qty(ml)
                        if qty <= 0:
                            continue
                        all_ml_ids.add(ml.id)
                        all_ml_qty[ml.id] = qty

                if all_ml_ids:
                    self._validate_picking_partial(
                        picking,
                        all_ml_ids,
                        all_ml_qty,
                    )
                else:
                    raise UserError(_(
                        'No se encontraron líneas de movimiento con cantidad para validar la reentrega.'
                    ))
        else:
            for move in picking.move_ids:
                qty = sum(
                    dl.qty_selected for dl in self.line_ids
                    if dl.product_id == move.product_id and dl.qty_selected > 0
                )
                if qty > 0:
                    move.product_uom_qty = qty

            result = picking.with_context(
                skip_backorder=False,
            ).button_validate()
            self._som_process_validate_result(
                picking,
                result,
                label=_('reentrega'),
            )

        picking.invalidate_recordset()
        if picking.state != 'done':
            raise UserError(_(
                'La reentrega %s no quedó validada. Estado actual: %s.'
            ) % (picking.name, picking.state))

        for doc_line in self.line_ids:
            doc_line.qty_done = doc_line.qty_selected

        _logger.info(
            'Redelivery %s confirmed. Picking %s state: %s',
            self.name,
            picking.name,
            picking.state,
        )

        return True


class SaleDeliveryDocumentLine(models.Model):
    _name = 'sale.delivery.document.line'
    _description = 'Línea de Documento de Entrega'
    _order = 'sequence, id'

    document_id = fields.Many2one(
        'sale.delivery.document', string='Documento',
        required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)

    sale_line_id = fields.Many2one('sale.order.line', string='Línea de Venta')
    move_id = fields.Many2one('stock.move', string='Movimiento')
    move_line_id = fields.Many2one('stock.move.line', string='Línea de Movimiento')
    product_id = fields.Many2one('product.product', string='Producto', required=True)
    lot_id = fields.Many2one('stock.lot', string='Lote/Placa')
    quant_id = fields.Many2one('stock.quant', string='Quant')

    qty_selected = fields.Float(string='Cantidad Seleccionada')
    qty_done = fields.Float(string='Cantidad Realizada')
    qty_returned = fields.Float(string='Cantidad Devuelta')

    source_location_id = fields.Many2one('stock.location', string='Ubicación Origen')

    origin_remission_id = fields.Many2one(
        'sale.delivery.document',
        string='Remisión Origen',
        index=True,
        ondelete='set null',
        domain=[('document_type', '=', 'remission')],
        help='Remisión original desde la que se originó esta devolución.'
    )
    origin_remission_line_id = fields.Many2one(
        'sale.delivery.document.line',
        string='Línea Remisión Origen',
        index=True,
        ondelete='set null',
        help='Línea exacta de la remisión original que se está devolviendo.'
    )
    origin_remission_number = fields.Char(
        string='Folio Remisión Origen',
        compute='_compute_origin_remission_number',
        store=True,
        readonly=True,
    )

    is_swap_origin = fields.Boolean(default=False)
    is_swap_target = fields.Boolean(default=False)
    is_replacement = fields.Boolean(default=False)

    @api.depends('origin_remission_id.name', 'origin_remission_id.remission_number')
    def _compute_origin_remission_number(self):
        for line in self:
            remission = line.origin_remission_id
            line.origin_remission_number = (
                remission.remission_number
                or remission.name
                or ''
            ) if remission else ''