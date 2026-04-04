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
            elif doc_type == 'redelivery' and vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'sale.delivery.redelivery') or '/'
        return super().create(vals_list)

    def action_prepare(self):
        self.filtered(lambda d: d.state == 'draft').write({'state': 'prepared'})

    def action_confirm(self):
        for doc in self.filtered(lambda d: d.state in ('draft', 'prepared')):
            if doc.document_type == 'remission':
                doc._action_confirm_remission()
            elif doc.document_type == 'return':
                doc._action_confirm_return()
            elif doc.document_type == 'redelivery':
                doc._action_confirm_redelivery()
            doc.state = 'confirmed'
            doc.delivery_date = fields.Datetime.now()

    def action_cancel(self):
        self.filtered(
            lambda d: d.state != 'confirmed'
        ).write({'state': 'cancelled'})

    def _validate_picking_partial(self, picking, doc_ml_ids, doc_ml_qty):
        """Validate a picking partially by setting qty only for selected move lines."""
        if picking.state == 'done':
            _logger.info('Picking %s already done, skipping.', picking.name)
            return True
        if picking.state not in ('assigned', 'confirmed'):
            raise UserError(_(
                'El picking %s no está en estado válido (estado: %s).',
                picking.name, picking.state))

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

        result = picking.with_context(
            skip_backorder=False,
        ).button_validate()

        if isinstance(result, dict):
            if result.get('res_model') == 'stock.backorder.confirmation':
                backorder_wiz = self.env['stock.backorder.confirmation'].with_context(
                    button_validate_picking_ids=picking.ids,
                ).create({
                    'pick_ids': [(4, picking.id)],
                })
                backorder_wiz.process()
                _logger.info('Picking %s validated with backorder.', picking.name)
            elif result.get('res_model') == 'stock.immediate.transfer':
                immediate_wiz = self.env['stock.immediate.transfer'].with_context(
                    button_validate_picking_ids=picking.ids,
                ).create({
                    'pick_ids': [(4, picking.id)],
                })
                immediate_wiz.process()
        elif picking.state == 'done':
            _logger.info('Picking %s validated successfully.', picking.name)
        else:
            _logger.warning(
                'Picking %s validation unexpected state: %s',
                picking.name, picking.state)

        return picking.state == 'done'

    def _action_confirm_remission(self):
        """Validate ONLY the selected lots/qty in the picking chain."""
        self.ensure_one()
        if not self.picking_id:
            raise UserError(_(
                'No hay picking asociado para confirmar la remisión.'))

        picking = self.picking_id

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

        self._validate_picking_partial(picking, doc_ml_ids, doc_ml_qty)

        out_picking = self._find_out_picking_for_lots(doc_lot_ids)
        if out_picking:
            self.out_picking_id = out_picking.id
            if out_picking.state == 'done':
                _logger.info('OUT %s already done.', out_picking.name)
                return

            if out_picking.state in ('confirmed', 'waiting'):
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
                                lot_id, ml.quantity)

                self._validate_picking_partial(
                    out_picking, out_doc_ml_ids, out_doc_ml_qty)
            else:
                _logger.warning(
                    'OUT %s not assignable (state: %s).',
                    out_picking.name, out_picking.state)
        else:
            _logger.info(
                'No OUT picking found. Single-step or push rule not triggered.')

    def _find_out_picking_for_lots(self, lot_ids):
        """Find an outgoing picking for this SO that contains any of the given lots."""
        if not lot_ids:
            return False

        order = self.sale_order_id
        out_pickings = order.picking_ids.filtered(
            lambda p: p.picking_type_code == 'outgoing'
            and p.state not in ('done', 'cancel'))

        for out_pick in out_pickings:
            pick_lot_ids = set(
                out_pick.move_line_ids.mapped('lot_id').ids)
            if pick_lot_ids & lot_ids:
                return out_pick

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

        if picking.state in ('draft', 'confirmed', 'waiting'):
            picking.action_assign()

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

        for move in picking.move_ids:
            if move.move_line_ids:
                for ml in move.move_line_ids:
                    lot_id = ml.lot_id.id if ml.lot_id else False
                    if lot_id and lot_id in lot_qty:
                        ml.quantity = lot_qty[lot_id]
                    elif ml.product_id.id in product_qty:
                        ml.quantity = product_qty[ml.product_id.id]
                    else:
                        ml.quantity = 0
            else:
                total_qty = sum(
                    doc_line.qty_selected
                    for doc_line in self.line_ids
                    if doc_line.qty_selected > 0
                    and doc_line.product_id.id == move.product_id.id)
                if total_qty > 0:
                    move.quantity = total_qty

        result = picking.button_validate()
        if isinstance(result, dict):
            if result.get('res_model') == 'stock.backorder.confirmation':
                backorder_wiz = self.env['stock.backorder.confirmation'].with_context(
                    button_validate_picking_ids=picking.ids,
                ).create({
                    'pick_ids': [(4, picking.id)],
                })
                backorder_wiz.process()
            elif result.get('res_model') == 'stock.immediate.transfer':
                immediate_wiz = self.env['stock.immediate.transfer'].with_context(
                    button_validate_picking_ids=picking.ids,
                ).create({
                    'pick_ids': [(4, picking.id)],
                })
                immediate_wiz.process()

    def _action_confirm_redelivery(self):
        """Confirm the redelivery: validate the associated outgoing picking
        to actually deliver the material that was returned and re-scheduled.
        This generates a remission number for the redelivery.
        """
        self.ensure_one()
        if not self.picking_id:
            raise UserError(_(
                'No hay picking asociado para confirmar la reentrega.'))

        picking = self.picking_id

        # Generate remission number for the redelivery
        seq = self.env['ir.sequence'].next_by_code(
            'sale.delivery.remission') or '/'
        self.remission_number = seq

        # Assign if needed
        if picking.state in ('confirmed', 'waiting'):
            picking.action_assign()

        if picking.state == 'assigned':
            # Build ml lookup from document lines
            doc_ml_ids = set()
            doc_ml_qty = {}
            for doc_line in self.line_ids:
                if doc_line.qty_selected > 0:
                    # Find matching move line by lot
                    for move in picking.move_ids:
                        for ml in move.move_line_ids:
                            if doc_line.lot_id and ml.lot_id == doc_line.lot_id:
                                doc_ml_ids.add(ml.id)
                                doc_ml_qty[ml.id] = doc_line.qty_selected
                            elif (not doc_line.lot_id
                                    and ml.product_id == doc_line.product_id):
                                doc_ml_ids.add(ml.id)
                                doc_ml_qty[ml.id] = doc_line.qty_selected

            if doc_ml_ids:
                self._validate_picking_partial(
                    picking, doc_ml_ids, doc_ml_qty)
            else:
                # Fallback: validate all move lines
                all_ml_ids = set()
                all_ml_qty = {}
                for move in picking.move_ids:
                    for ml in move.move_line_ids:
                        all_ml_ids.add(ml.id)
                        all_ml_qty[ml.id] = ml.quantity
                self._validate_picking_partial(
                    picking, all_ml_ids, all_ml_qty)
        else:
            # Force quantities and validate
            for move in picking.move_ids:
                qty = sum(
                    dl.qty_selected for dl in self.line_ids
                    if dl.product_id == move.product_id and dl.qty_selected > 0
                )
                if qty > 0:
                    move.quantity = qty

            result = picking.with_context(
                skip_backorder=False).button_validate()
            if isinstance(result, dict):
                if result.get('res_model') == 'stock.backorder.confirmation':
                    backorder_wiz = self.env[
                        'stock.backorder.confirmation'
                    ].with_context(
                        button_validate_picking_ids=picking.ids,
                    ).create({'pick_ids': [(4, picking.id)]})
                    backorder_wiz.process()
                elif result.get('res_model') == 'stock.immediate.transfer':
                    immediate_wiz = self.env[
                        'stock.immediate.transfer'
                    ].with_context(
                        button_validate_picking_ids=picking.ids,
                    ).create({'pick_ids': [(4, picking.id)]})
                    immediate_wiz.process()

        # Update document lines with qty_done
        for doc_line in self.line_ids:
            doc_line.qty_done = doc_line.qty_selected

        _logger.info(
            'Redelivery %s confirmed. Picking %s state: %s',
            self.name, picking.name, picking.state)


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