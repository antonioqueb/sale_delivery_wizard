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
        """Mark document as prepared (pick ticket ready)."""
        self.filtered(lambda d: d.state == 'draft').write({'state': 'prepared'})

    def action_confirm(self):
        """Confirm document - for remissions this triggers inventory impact."""
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

    def _action_confirm_remission(self):
        """Validate the picking chain to trigger inventory moves.
        For 2-step delivery (pick_ship):
          1. Validate the Pick (internal: Existencias -> Salida)
          2. Find and validate the chained OUT (outgoing: Salida -> Customers)
        """
        self.ensure_one()
        if not self.picking_id:
            raise UserError(_(
                'No hay picking asociado para confirmar la remisión.'))

        picking = self.picking_id
        if picking.state == 'done':
            _logger.info('Pick %s already done, skipping validation.', picking.name)
        elif picking.state in ('assigned', 'confirmed'):
            # Set quantities done on move lines based on document lines
            for doc_line in self.line_ids:
                if doc_line.move_line_id and doc_line.qty_selected > 0:
                    doc_line.move_line_id.quantity = doc_line.qty_selected
            # Validate the Pick
            picking.with_context(
                skip_backorder=False,
                picking_ids_not_to_backorder=[],
            ).button_validate()
            _logger.info('Pick %s validated successfully.', picking.name)
        else:
            raise UserError(_(
                'El picking %s no está en estado válido para validar (estado: %s).',
                picking.name, picking.state))

        # === Step 2: Find and validate the chained OUT picking ===
        out_picking = self._find_chained_out_picking(picking)
        if out_picking:
            self.out_picking_id = out_picking.id
            if out_picking.state == 'done':
                _logger.info('OUT %s already done.', out_picking.name)
            elif out_picking.state in ('assigned', 'confirmed', 'waiting'):
                # Wait for assignment if needed
                if out_picking.state in ('confirmed', 'waiting'):
                    out_picking.action_assign()

                if out_picking.state == 'assigned':
                    # Set quantities done on OUT move lines
                    for out_move in out_picking.move_ids:
                        for out_ml in out_move.move_line_ids:
                            if out_ml.quantity > 0:
                                # quantity is already the reserved amount
                                pass  # quantity field serves as both reserved and done in Odoo 19
                    # Validate the OUT picking
                    out_picking.with_context(
                        skip_backorder=False,
                        picking_ids_not_to_backorder=[],
                    ).button_validate()
                    _logger.info('OUT %s validated successfully.', out_picking.name)
                else:
                    _logger.warning(
                        'OUT %s could not be assigned (state: %s). '
                        'Manual validation required.',
                        out_picking.name, out_picking.state)
            else:
                _logger.warning(
                    'OUT %s in unexpected state: %s',
                    out_picking.name, out_picking.state)
        else:
            # Single-step delivery or no chained picking found
            _logger.info('No chained OUT picking found for %s. '
                         'Assuming single-step delivery.', picking.name)

    def _find_chained_out_picking(self, pick_picking):
        """Find the outgoing (OUT) picking chained to the given internal pick.
        Follows move_dest_ids from the pick's moves to find the OUT picking.
        """
        out_pickings = self.env['stock.picking']
        for move in pick_picking.move_ids:
            for dest_move in move.move_dest_ids:
                if (dest_move.picking_id
                        and dest_move.picking_id != pick_picking
                        and dest_move.picking_id.picking_type_code == 'outgoing'):
                    out_pickings |= dest_move.picking_id
        if len(out_pickings) == 1:
            return out_pickings
        elif len(out_pickings) > 1:
            # Multiple OUT pickings — return the one that's not done yet,
            # or the first one
            pending = out_pickings.filtered(lambda p: p.state != 'done')
            return pending[0] if pending else out_pickings[0]
        return False

    def _action_confirm_return(self):
        """Process return picking."""
        self.ensure_one()
        if not self.return_picking_id:
            raise UserError(_(
                'No hay picking de devolución asociado.'))
        picking = self.return_picking_id
        for doc_line in self.line_ids:
            if doc_line.move_line_id and doc_line.qty_selected > 0:
                doc_line.move_line_id.quantity = doc_line.qty_selected
        picking.with_context(
            skip_backorder=False,
        ).button_validate()


class SaleDeliveryDocumentLine(models.Model):
    _name = 'sale.delivery.document.line'
    _description = 'Línea de Documento de Entrega'
    _order = 'sequence, id'

    document_id = fields.Many2one(
        'sale.delivery.document', string='Documento',
        required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)

    sale_line_id = fields.Many2one(
        'sale.order.line', string='Línea de Venta')
    move_id = fields.Many2one('stock.move', string='Movimiento')
    move_line_id = fields.Many2one(
        'stock.move.line', string='Línea de Movimiento')
    product_id = fields.Many2one(
        'product.product', string='Producto', required=True)
    lot_id = fields.Many2one('stock.lot', string='Lote/Placa')
    quant_id = fields.Many2one('stock.quant', string='Quant')

    qty_selected = fields.Float(string='Cantidad Seleccionada')
    qty_done = fields.Float(string='Cantidad Realizada')
    qty_returned = fields.Float(string='Cantidad Devuelta')

    source_location_id = fields.Many2one(
        'stock.location', string='Ubicación Origen')

    is_swap_origin = fields.Boolean(default=False)
    is_swap_target = fields.Boolean(default=False)
    is_replacement = fields.Boolean(default=False)