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

    # Delivery info
    remission_number = fields.Char(
        string='Número de Remisión', readonly=True, copy=False)
    delivery_address = fields.Text(string='Dirección de Entrega')
    special_instructions = fields.Text(string='Instrucciones Especiales')
    delivery_date = fields.Datetime(string='Fecha de Entrega')

    # Signature
    signed_by = fields.Char(string='Firmado por')
    signature_image = fields.Binary(string='Firma', attachment=True)

    # Return specific
    return_reason_id = fields.Many2one(
        'sale.return.reason', string='Motivo de Devolución')
    return_action = fields.Selection([
        ('reagendar', 'Reagendar'),
        ('reponer', 'Reponer'),
        ('finiquitar', 'Finiquitar'),
    ], string='Acción de Devolución')

    # Photos
    attachment_ids = fields.Many2many(
        'ir.attachment', string='Evidencia Fotográfica')
    photo_count = fields.Integer(
        compute='_compute_photo_count', string='Fotos')

    # Lines
    line_ids = fields.One2many(
        'sale.delivery.document.line', 'document_id', string='Líneas')

    # Computed
    total_qty = fields.Float(
        compute='_compute_totals', string='Cantidad Total')

    @api.depends('attachment_ids')
    def _compute_photo_count(self):
        for rec in self:
            rec.photo_count = len(rec.attachment_ids)

    @api.depends('line_ids.qty_selected')
    def _compute_totals(self):
        for rec in self:
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
        return super().create(vals_list)

    def action_prepare(self):
        self.filtered(lambda d: d.state == 'draft').write({'state': 'prepared'})

    def action_confirm(self):
        for doc in self.filtered(lambda d: d.state in ('draft', 'prepared')):
            if doc.document_type == 'remission':
                doc._action_confirm_remission()
            elif doc.document_type == 'return':
                doc._action_confirm_return()
            doc.state = 'confirmed'
            doc.delivery_date = fields.Datetime.now()

    def action_cancel(self):
        self.filtered(
            lambda d: d.state != 'confirmed'
        ).write({'state': 'cancelled'})

    def _validate_picking_partial(self, picking, doc_ml_ids, doc_ml_qty):
        """Validate a picking partially by setting qty only for selected move lines.
        Handles the backorder confirmation wizard programmatically.
        """
        if picking.state == 'done':
            _logger.info('Picking %s already done, skipping.', picking.name)
            return True
        if picking.state not in ('assigned', 'confirmed'):
            raise UserError(_(
                'El picking %s no está en estado válido (estado: %s).',
                picking.name, picking.state))

        # Set quantities: selected lines get their qty, others get 0
        for move in picking.move_ids:
            for ml in move.move_line_ids:
                if ml.id in doc_ml_ids:
                    ml.quantity = doc_ml_qty[ml.id]
                    _logger.info(
                        'Picking %s ML %s (lot %s): qty set to %s',
                        picking.name, ml.id,
                        ml.lot_id.name if ml.lot_id else 'N/A',
                        doc_ml_qty[ml.id])
                else:
                    ml.quantity = 0
                    _logger.info(
                        'Picking %s ML %s (lot %s): qty zeroed',
                        picking.name, ml.id,
                        ml.lot_id.name if ml.lot_id else 'N/A')

        # Try to validate — button_validate may return a wizard action
        # for backorder confirmation
        result = picking.with_context(
            skip_backorder=False,
        ).button_validate()

        # If result is a dict (wizard action), handle backorder confirmation
        if isinstance(result, dict) and result.get('res_model') == 'stock.backorder.confirmation':
            # Create and process the backorder confirmation wizard
            backorder_wiz = self.env['stock.backorder.confirmation'].with_context(
                button_validate_picking_ids=picking.ids,
            ).create({
                'pick_ids': [(4, picking.id)],
            })
            backorder_wiz.process()
            _logger.info('Picking %s validated with backorder created.', picking.name)
        elif picking.state == 'done':
            _logger.info('Picking %s validated successfully.', picking.name)
        else:
            _logger.warning(
                'Picking %s validation returned unexpected result. State: %s',
                picking.name, picking.state)

        return picking.state == 'done'

    def _action_confirm_remission(self):
        """Validate ONLY the selected lots/qty in the picking chain.
        For 2-step delivery (pick_ship):
          1. Validate Pick partially (creates backorder for remaining)
          2. Push rule auto-creates OUT picking → find and validate it
        """
        self.ensure_one()
        if not self.picking_id:
            raise UserError(_(
                'No hay picking asociado para confirmar la remisión.'))

        picking = self.picking_id

        # Collect the move_line IDs and lot IDs from this document
        doc_ml_ids = set()
        doc_ml_qty = {}
        doc_lot_ids = set()
        doc_lot_qty = {}
        for doc_line in self.line_ids:
            if doc_line.move_line_id and doc_line.qty_selected > 0:
                doc_ml_ids.add(doc_line.move_line_id.id)
                doc_ml_qty[doc_line.move_line_id.id] = doc_line.qty_selected
            if doc_line.lot_id and doc_line.qty_selected > 0:
                doc_lot_ids.add(doc_line.lot_id.id)
                doc_lot_qty[doc_line.lot_id.id] = doc_line.qty_selected

        # ── Step 1: Validate the Pick (partial) ──
        self._validate_picking_partial(picking, doc_ml_ids, doc_ml_qty)

        # ── Step 2: Find the OUT picking (created by push rule) ──
        # After validating the Pick, the push rule "Salida → Customers"
        # should have auto-created an OUT picking.
        # We look for it in the SO's pickings.
        out_picking = self._find_out_picking_for_lots(doc_lot_ids)
        if out_picking:
            self.out_picking_id = out_picking.id
            if out_picking.state == 'done':
                _logger.info('OUT %s already done.', out_picking.name)
                return

            # Assign if needed
            if out_picking.state in ('confirmed', 'waiting'):
                out_picking.action_assign()

            if out_picking.state == 'assigned':
                # Build ml lookup for OUT picking by lot
                out_doc_ml_ids = set()
                out_doc_ml_qty = {}
                for move in out_picking.move_ids:
                    for ml in move.move_line_ids:
                        lot_id = ml.lot_id.id if ml.lot_id else False
                        if lot_id and lot_id in doc_lot_ids:
                            out_doc_ml_ids.add(ml.id)
                            out_doc_ml_qty[ml.id] = doc_lot_qty.get(lot_id, ml.quantity)
                        else:
                            # Not in our selection — will be zeroed
                            pass

                self._validate_picking_partial(
                    out_picking, out_doc_ml_ids, out_doc_ml_qty)
            else:
                _logger.warning(
                    'OUT %s not assignable (state: %s). Manual validation needed.',
                    out_picking.name, out_picking.state)
        else:
            _logger.info(
                'No OUT picking found for delivered lots. '
                'May be single-step or push rule not triggered.')

    def _find_out_picking_for_lots(self, lot_ids):
        """Find an outgoing picking for this SO that contains any of the given lots.
        This handles the case where pickings are created via push rules
        (no move_dest_ids chain).
        """
        if not lot_ids:
            return False

        order = self.sale_order_id
        # Look for outgoing pickings that are not done and have our lots
        out_pickings = order.picking_ids.filtered(
            lambda p: p.picking_type_code == 'outgoing'
            and p.state not in ('done', 'cancel'))

        for out_pick in out_pickings:
            pick_lot_ids = set(
                out_pick.move_line_ids.mapped('lot_id').ids)
            if pick_lot_ids & lot_ids:
                return out_pick

        # Also check via move_dest_ids from the pick
        if self.picking_id:
            for move in self.picking_id.move_ids:
                for dest_move in move.move_dest_ids:
                    if (dest_move.picking_id
                            and dest_move.picking_id.picking_type_code == 'outgoing'
                            and dest_move.picking_id.state not in ('done', 'cancel')):
                        return dest_move.picking_id

        return False

    def _action_confirm_return(self):
        """Validate the return picking with quantities from the document lines."""
        self.ensure_one()
        if not self.return_picking_id:
            raise UserError(_('No hay picking de devolución asociado.'))
        picking = self.return_picking_id

        # Assign the picking first to generate move lines
        if picking.state in ('draft', 'confirmed', 'waiting'):
            picking.action_assign()

        # Build lot->qty map from document lines
        lot_qty = {}
        product_qty = {}
        for doc_line in self.line_ids:
            if doc_line.qty_selected > 0:
                if doc_line.lot_id:
                    lot_qty[doc_line.lot_id.id] = doc_line.qty_selected
                elif doc_line.product_id:
                    product_qty[doc_line.product_id.id] = (
                        product_qty.get(doc_line.product_id.id, 0.0)
                        + doc_line.qty_selected)

        # Set quantities on move lines
        for move in picking.move_ids:
            if move.move_line_ids:
                for ml in move.move_line_ids:
                    lot_id = ml.lot_id.id if ml.lot_id else False
                    if lot_id and lot_id in lot_qty:
                        ml.quantity = lot_qty[lot_id]
                        _logger.info(
                            'Return %s ML %s (lot %s): qty set to %s',
                            picking.name, ml.id,
                            ml.lot_id.name, lot_qty[lot_id])
                    elif ml.product_id.id in product_qty:
                        ml.quantity = product_qty[ml.product_id.id]
                    else:
                        ml.quantity = 0
            else:
                # No move lines — set qty_done directly on the move
                total_qty = sum(
                    doc_line.qty_selected
                    for doc_line in self.line_ids
                    if doc_line.qty_selected > 0
                    and doc_line.product_id.id == move.product_id.id)
                if total_qty > 0:
                    move.quantity = total_qty
                    _logger.info(
                        'Return %s Move %s: qty set to %s',
                        picking.name, move.id, total_qty)

        # Validate the picking
        result = picking.button_validate()
        # Handle backorder confirmation if needed
        if isinstance(result, dict) and result.get('res_model') == 'stock.backorder.confirmation':
            backorder_wiz = self.env['stock.backorder.confirmation'].with_context(
                button_validate_picking_ids=picking.ids,
            ).create({
                'pick_ids': [(4, picking.id)],
            })
            backorder_wiz.process()
            _logger.info('Return %s validated with backorder.', picking.name)
        elif picking.state == 'done':
            _logger.info('Return %s validated successfully.', picking.name)
        else:
            _logger.warning(
                'Return %s validation unexpected state: %s',
                picking.name, picking.state)


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

    is_swap_origin = fields.Boolean(default=False)
    is_swap_target = fields.Boolean(default=False)
    is_replacement = fields.Boolean(default=False)