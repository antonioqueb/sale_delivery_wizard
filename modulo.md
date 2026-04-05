## ./__init__.py
```py
from . import models
from . import wizard
```

## ./__manifest__.py
```py
{
    'name': 'Sale Delivery Wizard - SOM',
    'version': '19.0.1.1.0',
    'category': 'Sales/Delivery',
    'summary': 'Hub de entregas y devoluciones centralizado en la orden de venta',
    'description': """
        Módulo orquestador de entregas desde sale.order para Recubrimientos STO.
        - Wizard de entrega parcial con selección de lotes
        - Pick Ticket sin impacto de inventario
        - Remisión con impacto de inventario y secuencia propia
        - Swap de lotes previo a remisión
        - Devoluciones con motivo y resolución (Reagendar/Reponer/Finiquitar)
        - Fulfillment neto (entregado - devuelto)
        - Cockpit operativo en el formulario de venta
    """,
    'author': 'Alphaqueb Consulting SAS',
    'website': 'https://alphaqueb.com',
    'depends': [
        'sale_management',
        'stock',
        'sale_stock',
    ],
    'data': [
        'security/sale_delivery_groups.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'data/sale_return_reason_data.xml',
        'views/sale_order_views.xml',
        'views/sale_delivery_document_views.xml',
        'wizard/sale_delivery_wizard_views.xml',
        'wizard/sale_return_wizard_views.xml',
        'wizard/sale_swap_wizard_views.xml',
        'report/pick_ticket_report.xml',
        'report/remission_report.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'sale_delivery_wizard/static/src/scss/delivery_wizard.scss',
        ],
    },
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}```

## ./data/ir_sequence_data.xml
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <data noupdate="1">
        <record id="seq_pick_ticket" model="ir.sequence">
            <field name="name">Pick Ticket SOM</field>
            <field name="code">sale.delivery.pick.ticket</field>
            <field name="prefix">PT/%(year)s/</field>
            <field name="padding">5</field>
        </record>

        <record id="seq_remission" model="ir.sequence">
            <field name="name">Remisión SOM</field>
            <field name="code">sale.delivery.remission</field>
            <field name="prefix">REM/%(year)s/</field>
            <field name="padding">5</field>
        </record>

        <record id="seq_return" model="ir.sequence">
            <field name="name">Devolución SOM</field>
            <field name="code">sale.delivery.return</field>
            <field name="prefix">DEV/%(year)s/</field>
            <field name="padding">5</field>
        </record>

        <record id="seq_redelivery" model="ir.sequence">
            <field name="name">Reentrega SOM</field>
            <field name="code">sale.delivery.redelivery</field>
            <field name="prefix">REENT/%(year)s/</field>
            <field name="padding">5</field>
        </record>
    </data>
</odoo>```

## ./data/sale_return_reason_data.xml
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <data noupdate="1">
        <record id="return_reason_damaged" model="sale.return.reason">
            <field name="name">Material Dañado</field>
            <field name="code">DAMAGED</field>
            <field name="sequence">10</field>
        </record>
        <record id="return_reason_wrong_size" model="sale.return.reason">
            <field name="name">Medidas Incorrectas</field>
            <field name="code">WRONG_SIZE</field>
            <field name="sequence">20</field>
        </record>
        <record id="return_reason_no_unload" model="sale.return.reason">
            <field name="name">No se Pudo Descargar</field>
            <field name="code">NO_UNLOAD</field>
            <field name="sequence">30</field>
        </record>
        <record id="return_reason_not_requested" model="sale.return.reason">
            <field name="name">Material No Solicitado</field>
            <field name="code">NOT_REQUESTED</field>
            <field name="sequence">40</field>
        </record>
        <record id="return_reason_quality" model="sale.return.reason">
            <field name="name">Problema de Calidad</field>
            <field name="code">QUALITY</field>
            <field name="sequence">50</field>
        </record>
        <record id="return_reason_other" model="sale.return.reason">
            <field name="name">Otro</field>
            <field name="code">OTHER</field>
            <field name="sequence">99</field>
        </record>
    </data>
</odoo>
```

## ./models/__init__.py
```py
from . import sale_return_reason
from . import sale_delivery_document
from . import sale_delivery_document_line
from . import sale_order
from . import sale_order_line
```

## ./models/sale_delivery_document_line.py
```py
# Lines are defined in sale_delivery_document.py
```

## ./models/sale_delivery_document.py
```py
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
    is_replacement = fields.Boolean(default=False)```

## ./models/sale_order_line.py
```py
from odoo import api, fields, models


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    x_returned_qty = fields.Float(
        compute='_compute_return_qty',
        string='Cantidad Devuelta',
        store=True)
    x_delivered_net_qty = fields.Float(
        compute='_compute_delivery_net',
        string='Entregado Neto',
        store=True)
    x_pending_qty = fields.Float(
        compute='_compute_delivery_net',
        string='Pendiente')
    x_fulfillment_net_pct = fields.Float(
        compute='_compute_delivery_net',
        string='Fulfillment Neto %')
    x_delivery_status = fields.Selection([
        ('sin_asignar', 'Sin Asignar'),
        ('parcial_asignado', 'Parcial Asignado'),
        ('asignado', 'Asignado'),
        ('parcial_entregado', 'Parcial Entregado'),
        ('entregado', 'Entregado'),
        ('devuelto_parcial', 'Devuelto Parcial'),
        ('finiquitado', 'Finiquitado'),
    ], compute='_compute_delivery_status', string='Estado Entrega',
        store=True)

    @api.depends('move_ids.move_line_ids.quantity',
                 'move_ids.origin_returned_move_id')
    def _compute_return_qty(self):
        for line in self:
            returned = 0.0
            for move in line.move_ids:
                if (move.origin_returned_move_id
                        and move.state == 'done'
                        and move.location_dest_id.usage == 'internal'):
                    returned += move.product_uom_qty
            line.x_returned_qty = returned

    @api.depends('qty_delivered', 'x_returned_qty', 'product_uom_qty')
    def _compute_delivery_net(self):
        for line in self:
            net = line.qty_delivered - line.x_returned_qty
            line.x_delivered_net_qty = max(net, 0)
            line.x_pending_qty = max(
                line.product_uom_qty - line.x_delivered_net_qty, 0)
            line.x_fulfillment_net_pct = (
                (line.x_delivered_net_qty / line.product_uom_qty * 100)
                if line.product_uom_qty else 0.0)

    @api.depends('product_uom_qty', 'qty_delivered',
                 'x_returned_qty', 'x_delivered_net_qty')
    def _compute_delivery_status(self):
        for line in self:
            if line.product_id.type == 'service':
                line.x_delivery_status = 'entregado'
                continue
            demand = line.product_uom_qty
            delivered_net = line.x_delivered_net_qty
            returned = line.x_returned_qty

            if delivered_net <= 0 and demand > 0:
                line.x_delivery_status = 'sin_asignar'
            elif delivered_net >= demand:
                if returned > 0:
                    line.x_delivery_status = 'devuelto_parcial'
                else:
                    line.x_delivery_status = 'entregado'
            elif delivered_net > 0:
                line.x_delivery_status = 'parcial_entregado'
            else:
                line.x_delivery_status = 'sin_asignar'
```

## ./models/sale_order.py
```py
from odoo import api, fields, models, _
import json


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # ── Delivery documents ──
    delivery_document_ids = fields.One2many(
        'sale.delivery.document', 'sale_order_id',
        string='Documentos de Entrega')

    # ── Computed summary ──
    x_total_assigned_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Total Asignado')
    x_total_delivered_gross_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Entregado Bruto')
    x_total_returned_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Devuelto')
    x_total_delivered_net_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Entregado Neto')
    x_total_pending_delivery_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Pendiente Entrega')
    x_total_demand_qty = fields.Float(
        compute='_compute_delivery_summary',
        string='Demanda Total')
    x_fulfillment_net_pct = fields.Float(
        compute='_compute_delivery_summary',
        string='Fulfillment Neto %')

    # ── Counts ──
    x_delivery_document_count = fields.Integer(
        compute='_compute_document_counts',
        string='Documentos')
    x_remission_count = fields.Integer(
        compute='_compute_document_counts',
        string='Remisiones')
    x_return_count = fields.Integer(
        compute='_compute_document_counts',
        string='Devoluciones')
    x_pick_ticket_count = fields.Integer(
        compute='_compute_document_counts',
        string='Pick Tickets')
    x_redelivery_count = fields.Integer(
        compute='_compute_document_counts',
        string='Reentregas')
    x_redelivery_pending_count = fields.Integer(
        compute='_compute_document_counts',
        string='Reentregas Pendientes')

    @api.depends(
        'order_line.product_uom_qty',
        'order_line.qty_delivered',
        'order_line.x_returned_qty',
    )
    def _compute_delivery_summary(self):
        for order in self:
            lines = order.order_line.filtered(
                lambda l: l.product_id.type != 'service')
            demand = sum(lines.mapped('product_uom_qty'))
            assigned = sum(lines.mapped('product_uom_qty'))
            delivered_gross = sum(lines.mapped('qty_delivered'))
            returned = sum(lines.mapped('x_returned_qty'))
            delivered_net = delivered_gross - returned
            pending = demand - delivered_net

            order.x_total_demand_qty = demand
            order.x_total_assigned_qty = assigned
            order.x_total_delivered_gross_qty = delivered_gross
            order.x_total_returned_qty = returned
            order.x_total_delivered_net_qty = max(delivered_net, 0)
            order.x_total_pending_delivery_qty = max(pending, 0)
            order.x_fulfillment_net_pct = (
                (delivered_net / demand * 100) if demand else 0.0)

    @api.depends('delivery_document_ids',
                 'delivery_document_ids.document_type',
                 'delivery_document_ids.state')
    def _compute_document_counts(self):
        for order in self:
            docs = order.delivery_document_ids
            order.x_delivery_document_count = len(docs)
            order.x_remission_count = len(
                docs.filtered(lambda d: d.document_type == 'remission'))
            order.x_return_count = len(
                docs.filtered(lambda d: d.document_type == 'return'))
            order.x_pick_ticket_count = len(
                docs.filtered(lambda d: d.document_type == 'pick_ticket'))
            order.x_redelivery_count = len(
                docs.filtered(lambda d: d.document_type == 'redelivery'))
            order.x_redelivery_pending_count = len(
                docs.filtered(
                    lambda d: d.document_type == 'redelivery'
                    and d.state in ('draft', 'prepared')))

    # ── Action buttons ──

    def action_open_delivery_wizard(self):
        """Open the delivery wizard from the sale order."""
        self.ensure_one()
        if self.state not in ('sale', 'done'):
            from odoo.exceptions import UserError
            raise UserError(_(
                'Solo puede entregar pedidos confirmados.'))
        if hasattr(self, 'delivery_auth_state'):
            if self.delivery_auth_state == 'pending':
                if not self.env.user.has_group(
                        'sale_delivery_wizard.group_delivery_authorizer'):
                    from odoo.exceptions import UserError
                    raise UserError(_(
                        'Este pedido no tiene autorización de entrega. '
                        'Contacte a un autorizador.'))
        return {
            'name': _('Entregar Material'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_sale_order_id': self.id,
                'active_id': self.id,
            },
        }

    def action_open_return_wizard(self):
        """Open the return wizard from the sale order."""
        self.ensure_one()
        return {
            'name': _('Devolución de Material'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.return.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_sale_order_id': self.id,
                'active_id': self.id,
            },
        }

    def action_open_swap_wizard(self):
        """Open swap wizard."""
        self.ensure_one()
        return {
            'name': _('Swap de Lotes'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.swap.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_sale_order_id': self.id,
            },
        }

    def action_view_delivery_documents(self):
        """View all delivery documents for this order."""
        self.ensure_one()
        return {
            'name': _('Documentos de Entrega'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id)],
            'context': {'default_sale_order_id': self.id},
        }

    def action_view_remissions(self):
        self.ensure_one()
        return {
            'name': _('Remisiones'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [
                ('sale_order_id', '=', self.id),
                ('document_type', '=', 'remission'),
            ],
        }

    def action_view_returns(self):
        self.ensure_one()
        return {
            'name': _('Devoluciones'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [
                ('sale_order_id', '=', self.id),
                ('document_type', '=', 'return'),
            ],
        }

    def action_view_pick_tickets(self):
        self.ensure_one()
        return {
            'name': _('Pick Tickets'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [
                ('sale_order_id', '=', self.id),
                ('document_type', '=', 'pick_ticket'),
            ],
        }

    def action_view_redeliveries(self):
        self.ensure_one()
        return {
            'name': _('Reentregas'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.delivery.document',
            'view_mode': 'list,form',
            'domain': [
                ('sale_order_id', '=', self.id),
                ('document_type', '=', 'redelivery'),
            ],
        }```

## ./models/sale_return_reason.py
```py
from odoo import fields, models


class SaleReturnReason(models.Model):
    _name = 'sale.return.reason'
    _description = 'Motivo de Devolución'
    _order = 'sequence, id'

    name = fields.Char(string='Motivo', required=True, translate=True)
    code = fields.Char(string='Código', required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    description = fields.Text(string='Descripción')
```

## ./report/pick_ticket_report.xml
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="action_report_pick_ticket" model="ir.actions.report">
        <field name="name">Pick Ticket</field>
        <field name="model">sale.delivery.document</field>
        <field name="report_type">qweb-pdf</field>
        <field name="report_name">sale_delivery_wizard.report_pick_ticket</field>
        <field name="report_file">sale_delivery_wizard.report_pick_ticket</field>
        <field name="binding_model_id" ref="model_sale_delivery_document"/>
        <field name="binding_type">report</field>
    </record>

    <template id="report_pick_ticket">
        <t t-call="web.html_container">
            <t t-foreach="docs" t-as="doc">
                <t t-call="web.external_layout">
                    <div class="page">
                        <h2 class="text-center mb-4">
                            PICK TICKET
                        </h2>

                        <div class="row mb-3">
                            <div class="col-6">
                                <table class="table table-sm table-borderless">
                                    <tr>
                                        <td class="fw-bold" style="width:40%;">No. Pick Ticket:</td>
                                        <td><t t-esc="doc.name"/></td>
                                    </tr>
                                    <tr>
                                        <td class="fw-bold">Pedido:</td>
                                        <td><t t-esc="doc.sale_order_id.name"/></td>
                                    </tr>
                                    <tr>
                                        <td class="fw-bold">Cliente:</td>
                                        <td><t t-esc="doc.partner_id.name"/></td>
                                    </tr>
                                </table>
                            </div>
                            <div class="col-6">
                                <table class="table table-sm table-borderless">
                                    <tr>
                                        <td class="fw-bold" style="width:40%;">Fecha:</td>
                                        <td><t t-esc="doc.create_date" t-options="{'widget': 'date'}"/></td>
                                    </tr>
                                    <tr>
                                        <td class="fw-bold">Dirección:</td>
                                        <td><t t-esc="doc.delivery_address or '-'"/></td>
                                    </tr>
                                </table>
                            </div>
                        </div>

                        <t t-if="doc.special_instructions">
                            <div class="alert alert-warning p-2 mb-3">
                                <strong>Instrucciones Especiales:</strong>
                                <t t-esc="doc.special_instructions"/>
                            </div>
                        </t>

                        <table class="table table-sm table-bordered">
                            <thead>
                                <tr class="bg-dark text-white">
                                    <th style="width:5%;">#</th>
                                    <th style="width:35%;">Producto</th>
                                    <th style="width:20%;">Lote / Placa</th>
                                    <th style="width:15%;">Ubicación</th>
                                    <th style="width:10%;" class="text-end">Cantidad</th>
                                    <th style="width:15%;">Recolectado</th>
                                </tr>
                            </thead>
                            <tbody>
                                <t t-set="idx" t-value="0"/>
                                <t t-foreach="doc.line_ids" t-as="line">
                                    <t t-set="idx" t-value="idx + 1"/>
                                    <tr>
                                        <td><t t-esc="idx"/></td>
                                        <td><t t-esc="line.product_id.display_name"/></td>
                                        <td><t t-esc="line.lot_id.name or '-'"/></td>
                                        <td><t t-esc="line.source_location_id.name or '-'"/></td>
                                        <td class="text-end">
                                            <t t-esc="'%.2f' % line.qty_selected"/>
                                        </td>
                                        <td class="text-center">
                                            <span style="font-size:18px;">☐</span>
                                        </td>
                                    </tr>
                                </t>
                            </tbody>
                            <tfoot>
                                <tr class="fw-bold">
                                    <td colspan="4" class="text-end">Total:</td>
                                    <td class="text-end">
                                        <t t-esc="'%.2f' % doc.total_qty"/>
                                    </td>
                                    <td/>
                                </tr>
                            </tfoot>
                        </table>

                        <div class="row mt-5">
                            <div class="col-6 text-center">
                                <div style="border-top: 1px solid #000; margin-top: 40px; padding-top: 5px;">
                                    Preparado por
                                </div>
                            </div>
                            <div class="col-6 text-center">
                                <div style="border-top: 1px solid #000; margin-top: 40px; padding-top: 5px;">
                                    Revisado por
                                </div>
                            </div>
                        </div>
                    </div>
                </t>
            </t>
        </t>
    </template>
</odoo>
```

## ./report/remission_report.xml
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="action_report_remission" model="ir.actions.report">
        <field name="name">Remisión</field>
        <field name="model">sale.delivery.document</field>
        <field name="report_type">qweb-pdf</field>
        <field name="report_name">sale_delivery_wizard.report_remission</field>
        <field name="report_file">sale_delivery_wizard.report_remission</field>
        <field name="binding_model_id" ref="model_sale_delivery_document"/>
        <field name="binding_type">report</field>
    </record>

    <template id="report_remission">
        <t t-call="web.html_container">
            <t t-foreach="docs" t-as="doc">
                <t t-call="web.external_layout">
                    <div class="page">
                        <h2 class="text-center mb-4">
                            REMISIÓN
                        </h2>

                        <div class="row mb-3">
                            <div class="col-6">
                                <table class="table table-sm table-borderless">
                                    <tr>
                                        <td class="fw-bold" style="width:45%;">No. Remisión:</td>
                                        <td><t t-esc="doc.remission_number or doc.name"/></td>
                                    </tr>
                                    <tr>
                                        <td class="fw-bold">Pedido:</td>
                                        <td><t t-esc="doc.sale_order_id.name"/></td>
                                    </tr>
                                    <tr>
                                        <td class="fw-bold">Cliente:</td>
                                        <td><t t-esc="doc.partner_id.name"/></td>
                                    </tr>
                                </table>
                            </div>
                            <div class="col-6">
                                <table class="table table-sm table-borderless">
                                    <tr>
                                        <td class="fw-bold" style="width:45%;">Fecha Salida:</td>
                                        <td><t t-esc="doc.delivery_date or doc.create_date"
                                               t-options="{'widget': 'datetime'}"/></td>
                                    </tr>
                                    <tr>
                                        <td class="fw-bold">Dirección Entrega:</td>
                                        <td><t t-esc="doc.delivery_address or '-'"/></td>
                                    </tr>
                                </table>
                            </div>
                        </div>

                        <t t-if="doc.special_instructions">
                            <div class="alert alert-info p-2 mb-3">
                                <strong>Instrucciones:</strong>
                                <t t-esc="doc.special_instructions"/>
                            </div>
                        </t>

                        <!-- NO PRECIOS - Packing list -->
                        <table class="table table-sm table-bordered">
                            <thead>
                                <tr class="bg-dark text-white">
                                    <th style="width:5%;">#</th>
                                    <th style="width:40%;">Producto / Material</th>
                                    <th style="width:25%;">Lote / Placa</th>
                                    <th style="width:15%;" class="text-end">Cantidad</th>
                                    <th style="width:15%;">UdM</th>
                                </tr>
                            </thead>
                            <tbody>
                                <t t-set="idx" t-value="0"/>
                                <t t-foreach="doc.line_ids" t-as="line">
                                    <t t-set="idx" t-value="idx + 1"/>
                                    <tr>
                                        <td><t t-esc="idx"/></td>
                                        <td><t t-esc="line.product_id.display_name"/></td>
                                        <td><t t-esc="line.lot_id.name or '-'"/></td>
                                        <td class="text-end">
                                            <t t-esc="'%.2f' % line.qty_done"/>
                                        </td>
                                        <td>
                                            <t t-esc="line.product_id.uom_id.name or ''"/>
                                        </td>
                                    </tr>
                                </t>
                            </tbody>
                            <tfoot>
                                <tr class="fw-bold">
                                    <td colspan="3" class="text-end">Total:</td>
                                    <td class="text-end">
                                        <t t-esc="'%.2f' % sum(doc.line_ids.mapped('qty_done'))"/>
                                    </td>
                                    <td/>
                                </tr>
                            </tfoot>
                        </table>

                        <!-- Firma -->
                        <div class="row mt-5">
                            <div class="col-4 text-center">
                                <div style="border-top: 1px solid #000; margin-top: 50px; padding-top: 5px;">
                                    Entregó
                                </div>
                            </div>
                            <div class="col-4 text-center">
                                <div style="border-top: 1px solid #000; margin-top: 50px; padding-top: 5px;">
                                    Transportó
                                </div>
                            </div>
                            <div class="col-4 text-center">
                                <t t-if="doc.signature_image">
                                    <img t-att-src="image_data_uri(doc.signature_image)"
                                         style="max-height: 50px;"/>
                                    <br/>
                                </t>
                                <div style="border-top: 1px solid #000; margin-top: 50px; padding-top: 5px;">
                                    Recibió: <t t-esc="doc.signed_by or '________________'"/>
                                </div>
                            </div>
                        </div>

                        <div class="text-center mt-3" style="font-size: 10px; color: #999;">
                            Este documento NO incluye precios. Para información fiscal consulte su factura.
                        </div>
                    </div>
                </t>
            </t>
        </t>
    </template>
</odoo>
```

## ./security/sale_delivery_groups.xml
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="group_delivery_user" model="res.groups">
        <field name="name">Entregas SOM / Usuario de Entregas</field>
    </record>

    <record id="group_delivery_authorizer" model="res.groups">
        <field name="name">Entregas SOM / Autorizador de Entregas</field>
        <field name="implied_ids" eval="[(4, ref('group_delivery_user'))]"/>
    </record>

    <record id="group_delivery_manager" model="res.groups">
        <field name="name">Entregas SOM / Gerente de Entregas</field>
        <field name="implied_ids" eval="[(4, ref('group_delivery_authorizer'))]"/>
    </record>

    <record id="group_delivery_driver" model="res.groups">
        <field name="name">Entregas SOM / Chofer (Solo Lectura)</field>
    </record>
</odoo>```

## ./static/src/scss/delivery_wizard.scss
```scss
// ============================================================
// Sale Delivery Wizard - Full-screen dialogs
// All wizards from this module should occupy 95% of viewport
// ============================================================

.o_dialog {
    &:has(.sale_delivery_wizard_form),
    &:has(.sale_return_wizard_form),
    &:has(.sale_swap_wizard_form) {
        .modal-dialog {
            max-width: 95vw !important;
            width: 95vw !important;
            margin: 1.5vh auto !important;
        }

        .modal-content {
            max-height: 95vh !important;
            height: auto;
        }

        .modal-body {
            max-height: calc(95vh - 120px) !important;
            overflow-y: auto;
        }

        .o_list_renderer {
            max-height: 60vh;
            overflow-y: auto;
        }
    }
}

// Fallback for older Odoo dialog structure
.modal {
    &:has(.sale_delivery_wizard_form),
    &:has(.sale_return_wizard_form),
    &:has(.sale_swap_wizard_form) {
        .modal-dialog {
            max-width: 95vw !important;
            width: 95vw !important;
            margin: 1.5vh auto !important;
        }

        .modal-content {
            max-height: 95vh !important;
        }

        .modal-body {
            max-height: calc(95vh - 120px) !important;
            overflow-y: auto;
        }
    }
}```

## ./views/sale_delivery_document_views.xml
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- Delivery Document Form -->
    <record id="sale_delivery_document_form" model="ir.ui.view">
        <field name="name">sale.delivery.document.form</field>
        <field name="model">sale.delivery.document</field>
        <field name="arch" type="xml">
            <form>
                <header>
                    <button name="action_prepare" string="Preparar"
                            type="object" class="btn-primary"
                            invisible="state != 'draft'"/>
                    <button name="action_confirm" string="Confirmar Entrega"
                            type="object" class="btn-primary"
                            invisible="state not in ('draft', 'prepared') or document_type != 'redelivery'"
                            confirm="¿Confirmar la reentrega? Se validará el picking y se descontará inventario."/>
                    <button name="action_confirm" string="Confirmar"
                            type="object" class="btn-primary"
                            invisible="state not in ('draft', 'prepared') or document_type == 'redelivery'"/>
                    <button name="action_cancel" string="Cancelar"
                            type="object"
                            invisible="state in ('confirmed', 'cancelled')"/>
                    <field name="state" widget="statusbar"
                           statusbar_visible="draft,prepared,confirmed"/>
                </header>
                <sheet>
                    <!-- Redelivery banner -->
                    <div class="alert alert-warning text-center p-3"
                         invisible="document_type != 'redelivery' or state != 'prepared'">
                        <strong>⏳ Reentrega Pendiente</strong> —
                        Este material fue devuelto y reagendado.
                        Presione <em>"Confirmar Entrega"</em> cuando el material sea entregado al cliente.
                    </div>

                    <div class="oe_title">
                        <h1><field name="name"/></h1>
                    </div>
                    <group>
                        <group>
                            <field name="document_type"/>
                            <field name="sale_order_id"/>
                            <field name="partner_id"/>
                            <field name="remission_number"
                                   invisible="document_type not in ('remission', 'redelivery')"/>
                            <field name="picking_id"
                                   invisible="document_type == 'return'"/>
                            <field name="return_picking_id"
                                   invisible="document_type != 'return'"/>
                        </group>
                        <group>
                            <field name="delivery_date"/>
                            <field name="delivery_address"/>
                            <field name="special_instructions"/>
                            <field name="return_reason_id"
                                   invisible="document_type != 'return'"/>
                            <field name="return_action"
                                   invisible="document_type != 'return'"/>
                        </group>
                    </group>
                    <notebook>
                        <page string="Líneas">
                            <field name="line_ids">
                                <list editable="bottom">
                                    <field name="product_id"/>
                                    <field name="lot_id"/>
                                    <field name="qty_selected"/>
                                    <field name="qty_done"/>
                                    <field name="qty_returned"
                                           column_invisible="parent.document_type != 'return'"/>
                                    <field name="source_location_id"/>
                                    <field name="is_swap_origin" column_invisible="1"/>
                                    <field name="is_swap_target" column_invisible="1"/>
                                    <field name="sale_line_id" optional="hide"/>
                                    <field name="move_id" optional="hide"/>
                                </list>
                            </field>
                            <group>
                                <field name="total_qty"/>
                            </group>
                        </page>
                        <page string="Firma y Evidencia">
                            <group>
                                <field name="signed_by"/>
                                <field name="signature_image" widget="image"/>
                            </group>
                            <field name="attachment_ids" widget="many2many_binary"/>
                            <field name="photo_count"/>
                        </page>
                    </notebook>
                </sheet>
                <chatter/>
            </form>
        </field>
    </record>

    <!-- Delivery Document List -->
    <record id="sale_delivery_document_list" model="ir.ui.view">
        <field name="name">sale.delivery.document.list</field>
        <field name="model">sale.delivery.document</field>
        <field name="arch" type="xml">
            <list>
                <field name="name"/>
                <field name="document_type"/>
                <field name="state" widget="badge"
                       decoration-success="state == 'confirmed'"
                       decoration-warning="state == 'prepared'"
                       decoration-danger="state == 'cancelled'"/>
                <field name="sale_order_id"/>
                <field name="partner_id"/>
                <field name="remission_number"/>
                <field name="total_qty"/>
                <field name="delivery_date"/>
            </list>
        </field>
    </record>

    <!-- Menu & Actions -->
    <record id="action_sale_delivery_documents" model="ir.actions.act_window">
        <field name="name">Documentos de Entrega</field>
        <field name="res_model">sale.delivery.document</field>
        <field name="view_mode">list,form</field>
    </record>

    <record id="action_sale_return_reasons" model="ir.actions.act_window">
        <field name="name">Motivos de Devolución</field>
        <field name="res_model">sale.return.reason</field>
        <field name="view_mode">list,form</field>
    </record>

    <menuitem id="menu_delivery_root"
              name="Entregas SOM"
              parent="sale.sale_menu_root"
              sequence="15"/>
    <menuitem id="menu_delivery_documents"
              name="Documentos de Entrega"
              parent="menu_delivery_root"
              action="action_sale_delivery_documents"
              sequence="10"/>
    <menuitem id="menu_return_reasons"
              name="Motivos de Devolución"
              parent="menu_delivery_root"
              action="action_sale_return_reasons"
              sequence="20"
              groups="sale_delivery_wizard.group_delivery_manager"/>
</odoo>```

## ./views/sale_order_views.xml
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- Sale Order Form: Delivery Cockpit -->
    <record id="sale_order_form_delivery_cockpit" model="ir.ui.view">
        <field name="name">sale.order.form.delivery.cockpit</field>
        <field name="model">sale.order</field>
        <field name="inherit_id" ref="sale.view_order_form"/>
        <field name="priority">99</field>
        <field name="arch" type="xml">
            <!-- Buttons in header -->
            <xpath expr="//header" position="inside">
                <button name="action_open_delivery_wizard"
                        string="Entregar"
                        type="object"
                        class="btn-primary"
                        invisible="state not in ('sale', 'done')"
                        groups="sale_delivery_wizard.group_delivery_user"/>
                <button name="action_open_return_wizard"
                        string="Devolución"
                        type="object"
                        class="btn-warning"
                        invisible="state not in ('sale', 'done')"
                        groups="sale_delivery_wizard.group_delivery_user"/>
                <button name="action_open_swap_wizard"
                        string="Swap"
                        type="object"
                        invisible="state not in ('sale', 'done')"
                        groups="sale_delivery_wizard.group_delivery_user"/>
            </xpath>

            <!-- Stat buttons -->
            <xpath expr="//div[@name='button_box']" position="inside">
                <button name="action_view_pick_tickets"
                        type="object"
                        class="oe_stat_button"
                        icon="fa-clipboard"
                        invisible="x_pick_ticket_count == 0">
                    <field name="x_pick_ticket_count"
                           widget="statinfo"
                           string="Pick Tickets"/>
                </button>
                <button name="action_view_remissions"
                        type="object"
                        class="oe_stat_button"
                        icon="fa-truck"
                        invisible="x_remission_count == 0">
                    <field name="x_remission_count"
                           widget="statinfo"
                           string="Remisiones"/>
                </button>
                <button name="action_view_returns"
                        type="object"
                        class="oe_stat_button"
                        icon="fa-undo"
                        invisible="x_return_count == 0">
                    <field name="x_return_count"
                           widget="statinfo"
                           string="Devoluciones"/>
                </button>
                <button name="action_view_redeliveries"
                        type="object"
                        class="oe_stat_button"
                        icon="fa-refresh"
                        invisible="x_redelivery_count == 0">
                    <div class="o_field_widget o_stat_info">
                        <span class="o_stat_value">
                            <field name="x_redelivery_count" raw-attrs="{}"/>
                            <span invisible="x_redelivery_pending_count == 0"
                                  class="badge bg-warning ms-1"
                                  title="Pendientes">
                                <field name="x_redelivery_pending_count" raw-attrs="{}"/>
                            </span>
                        </span>
                        <span class="o_stat_text">Reentregas</span>
                    </div>
                </button>
            </xpath>

            <!-- Delivery summary cards before notebook -->
            <xpath expr="//notebook" position="before">
                <div class="row mt-2 mb-3" invisible="state not in ('sale', 'done')">
                    <div class="col-lg-2 col-md-4 col-sm-6 mb-2">
                        <div class="card bg-light text-center p-2">
                            <small class="text-muted">Demanda</small>
                            <h5><field name="x_total_demand_qty" class="mb-0"/></h5>
                        </div>
                    </div>
                    <div class="col-lg-2 col-md-4 col-sm-6 mb-2">
                        <div class="card bg-info text-white text-center p-2">
                            <small>Asignado</small>
                            <h5><field name="x_total_assigned_qty" class="mb-0"/></h5>
                        </div>
                    </div>
                    <div class="col-lg-2 col-md-4 col-sm-6 mb-2">
                        <div class="card bg-success text-white text-center p-2">
                            <small>Entregado Neto</small>
                            <h5><field name="x_total_delivered_net_qty" class="mb-0"/></h5>
                        </div>
                    </div>
                    <div class="col-lg-2 col-md-4 col-sm-6 mb-2">
                        <div class="card bg-warning text-center p-2">
                            <small>Devuelto</small>
                            <h5><field name="x_total_returned_qty" class="mb-0"/></h5>
                        </div>
                    </div>
                    <div class="col-lg-2 col-md-4 col-sm-6 mb-2">
                        <div class="card bg-secondary text-white text-center p-2">
                            <small>Pendiente</small>
                            <h5><field name="x_total_pending_delivery_qty" class="mb-0"/></h5>
                        </div>
                    </div>
                    <div class="col-lg-2 col-md-4 col-sm-6 mb-2">
                        <div class="card bg-primary text-white text-center p-2">
                            <small>Fulfillment</small>
                            <h5><field name="x_fulfillment_net_pct" widget="float" digits="[3,1]" class="mb-0"/>%</h5>
                        </div>
                    </div>
                </div>
            </xpath>

            <!-- Delivery history page in notebook -->
            <xpath expr="//notebook" position="inside">
                <page string="Historial de Entregas" name="delivery_history"
                      invisible="x_delivery_document_count == 0">
                    <field name="delivery_document_ids" readonly="1">
                        <list>
                            <field name="name"/>
                            <field name="document_type"/>
                            <field name="state"
                                   decoration-success="state == 'confirmed'"
                                   decoration-warning="state == 'prepared'"
                                   decoration-danger="state == 'cancelled'"
                                   widget="badge"/>
                            <field name="remission_number"/>
                            <field name="total_qty"/>
                            <field name="return_reason_id"/>
                            <field name="return_action"/>
                            <field name="delivery_date"/>
                            <field name="create_date"/>
                        </list>
                    </field>
                </page>
            </xpath>

            <!-- Add delivery status to order lines -->
            <xpath expr="//field[@name='order_line']/list/field[@name='product_uom_qty']" position="after">
                <field name="x_delivered_net_qty" string="Entregado Neto" optional="show"/>
                <field name="x_returned_qty" string="Devuelto" optional="hide"/>
                <field name="x_pending_qty" string="Pendiente" optional="show"/>
                <field name="x_delivery_status" string="Estado Entrega"
                       widget="badge"
                       decoration-success="x_delivery_status == 'entregado'"
                       decoration-warning="x_delivery_status in ('parcial_entregado', 'parcial_asignado')"
                       decoration-danger="x_delivery_status == 'sin_asignar'"
                       decoration-info="x_delivery_status == 'asignado'"
                       optional="show"/>
            </xpath>
        </field>
    </record>

    <!-- Hidden fields for counts -->
    <record id="sale_order_form_delivery_fields" model="ir.ui.view">
        <field name="name">sale.order.form.delivery.fields</field>
        <field name="model">sale.order</field>
        <field name="inherit_id" ref="sale.view_order_form"/>
        <field name="priority">100</field>
        <field name="arch" type="xml">
            <xpath expr="//field[@name='partner_id']" position="after">
                <field name="x_delivery_document_count" invisible="1"/>
                <field name="x_total_delivered_gross_qty" invisible="1"/>
                <field name="x_redelivery_pending_count" invisible="1"/>
            </xpath>
        </field>
    </record>
</odoo>```

## ./wizard/__init__.py
```py
from . import sale_delivery_wizard
from . import sale_return_wizard
from . import sale_swap_wizard
```

## ./wizard/sale_delivery_wizard_views.xml
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="sale_delivery_wizard_form" model="ir.ui.view">
        <field name="name">sale.delivery.wizard.form</field>
        <field name="model">sale.delivery.wizard</field>
        <field name="arch" type="xml">
            <form string="Entregar Material" class="sale_delivery_wizard_form">
                <!-- Status banner -->
                <div class="alert alert-info p-2 mb-2 text-center"
                     invisible="wizard_state != 'pick_ticket'">
                    <strong>Pick Ticket generado:</strong>
                    <field name="pick_ticket_id" readonly="1" class="d-inline"/>
                    — Puede hacer swaps, ajustar selección, y luego generar la remisión.
                </div>

                <field name="wizard_state" invisible="1"/>

                <group>
                    <group>
                        <field name="sale_order_id" readonly="1"/>
                        <field name="partner_id" readonly="1"/>
                    </group>
                    <group>
                        <field name="delivery_address"/>
                        <field name="special_instructions"
                               placeholder="Ej: Entrada por puerta trasera, puerta roja"/>
                    </group>
                </group>

                <group>
                    <div class="d-flex gap-2 mb-2">
                        <button name="action_select_all" string="Seleccionar Todo"
                                type="object" class="btn-secondary btn-sm"/>
                        <button name="action_deselect_all" string="Deseleccionar Todo"
                                type="object" class="btn-secondary btn-sm"/>
                    </div>
                </group>

                <field name="line_ids">
                    <list editable="bottom">
                        <field name="is_selected" widget="boolean_toggle"/>
                        <field name="product_name" string="Producto"/>
                        <field name="lot_name" string="Lote"/>
                        <field name="source_location_id" string="Ubicación" optional="hide"/>
                        <field name="qty_available" string="Disponible" readonly="1"/>
                        <field name="qty_to_deliver" string="A Entregar"/>
                        <field name="product_id" column_invisible="1"/>
                        <field name="lot_id" column_invisible="1"/>
                        <field name="picking_id" column_invisible="1"/>
                        <field name="move_id" column_invisible="1"/>
                        <field name="move_line_id" column_invisible="1"/>
                        <field name="sale_line_id" column_invisible="1"/>
                    </list>
                </field>

                <group>
                    <group>
                        <field name="total_selected" string="Total a Entregar"/>
                        <field name="total_available" string="Total Disponible"/>
                    </group>
                </group>

                <footer>
                    <!-- Step 1: Generate Pick Ticket (only when no PT yet) -->
                    <button name="action_generate_pick_ticket"
                            string="Generar Pick Ticket"
                            type="object" class="btn-secondary"
                            invisible="wizard_state != 'select'"
                            help="Genera documento de preparación. No descuenta inventario."/>

                    <!-- Step 2: After PT generated - can print, swap, or go to remission -->
                    <button name="action_print_pick_ticket"
                            string="Imprimir Pick Ticket"
                            type="object" class="btn-secondary"
                            invisible="wizard_state == 'select'"
                            help="Imprime el pick ticket generado."/>

                    <!-- Remission available at any step -->
                    <button name="action_generate_remission"
                            string="Generar Remisión"
                            type="object" class="btn-primary"
                            help="Genera remisión y descuenta inventario. Usa la selección del Pick Ticket."/>

                    <button string="Cancelar" class="btn-secondary" special="cancel"/>
                </footer>
            </form>
        </field>
    </record>
</odoo>```

## ./wizard/sale_delivery_wizard.py
```py
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging

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

    # ── Wizard state ──
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

    @api.depends('line_ids.qty_to_deliver', 'line_ids.is_selected')
    def _compute_totals(self):
        for wiz in self:
            selected_lines = wiz.line_ids.filtered('is_selected')
            wiz.total_selected = sum(selected_lines.mapped('qty_to_deliver'))
            wiz.total_available = sum(wiz.line_ids.mapped('qty_available'))

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so_id = res.get('sale_order_id') or self.env.context.get('active_id')
        if not so_id:
            return res
        order = self.env['sale.order'].browse(so_id)
        res['sale_order_id'] = order.id
        res['delivery_address'] = (
            order.partner_shipping_id.contact_address or '')

        # ── Check if there's a pending pick ticket for this order ──
        pending_pt = self.env['sale.delivery.document'].search([
            ('sale_order_id', '=', order.id),
            ('document_type', '=', 'pick_ticket'),
            ('state', '=', 'prepared'),
        ], order='create_date desc', limit=1)

        if pending_pt:
            # Load from pick ticket — only its lines, pre-selected
            res['pick_ticket_id'] = pending_pt.id
            res['wizard_state'] = 'pick_ticket'
            res['delivery_address'] = pending_pt.delivery_address or res.get('delivery_address', '')
            res['special_instructions'] = pending_pt.special_instructions or ''
            res['line_ids'] = self._build_lines_from_pick_ticket(order, pending_pt)
        else:
            # Fresh wizard — all lines, all selected
            res['wizard_state'] = 'select'
            res['line_ids'] = self._build_lines_from_pickings(order)

        return res

    def _build_lines_from_pickings(self, order):
        """Build wizard lines from all pending pickings. All pre-selected."""
        lines = []
        for picking in order.picking_ids.filtered(
                lambda p: p.state in ('assigned', 'confirmed')):
            for move in picking.move_ids.filtered(
                    lambda m: m.state in ('assigned', 'confirmed')):
                if move.move_line_ids:
                    for ml in move.move_line_ids:
                        qty_avail = ml.quantity
                        if qty_avail <= 0:
                            qty_avail = move.product_uom_qty
                        lines.append((0, 0, {
                            'picking_id': picking.id,
                            'move_id': move.id,
                            'move_line_id': ml.id,
                            'sale_line_id': move.sale_line_id.id,
                            'product_id': move.product_id.id,
                            'lot_id': ml.lot_id.id if ml.lot_id else False,
                            'qty_available': qty_avail,
                            'qty_to_deliver': qty_avail,
                            'is_selected': True,
                            'source_location_id': ml.location_id.id,
                        }))
                else:
                    lines.append((0, 0, {
                        'picking_id': picking.id,
                        'move_id': move.id,
                        'sale_line_id': move.sale_line_id.id,
                        'product_id': move.product_id.id,
                        'qty_available': move.product_uom_qty,
                        'qty_to_deliver': move.product_uom_qty,
                        'is_selected': True,
                    }))
        return lines

    def _build_lines_from_pick_ticket(self, order, pt):
        """Build wizard lines from pickings, but only select those in the PT."""
        # Build a lookup of PT lines: (move_line_id, lot_id) -> qty
        pt_lookup = {}
        for pt_line in pt.line_ids:
            key = (pt_line.move_line_id.id, pt_line.lot_id.id)
            pt_lookup[key] = pt_line.qty_selected

        lines = []
        for picking in order.picking_ids.filtered(
                lambda p: p.state in ('assigned', 'confirmed')):
            for move in picking.move_ids.filtered(
                    lambda m: m.state in ('assigned', 'confirmed')):
                if move.move_line_ids:
                    for ml in move.move_line_ids:
                        qty_avail = ml.quantity
                        if qty_avail <= 0:
                            qty_avail = move.product_uom_qty

                        # Check if this line is in the pick ticket
                        key = (ml.id, ml.lot_id.id if ml.lot_id else False)
                        pt_qty = pt_lookup.get(key, 0.0)
                        is_in_pt = pt_qty > 0

                        lines.append((0, 0, {
                            'picking_id': picking.id,
                            'move_id': move.id,
                            'move_line_id': ml.id,
                            'sale_line_id': move.sale_line_id.id,
                            'product_id': move.product_id.id,
                            'lot_id': ml.lot_id.id if ml.lot_id else False,
                            'qty_available': qty_avail,
                            'qty_to_deliver': pt_qty if is_in_pt else 0.0,
                            'is_selected': is_in_pt,
                            'source_location_id': ml.location_id.id,
                        }))
                else:
                    lines.append((0, 0, {
                        'picking_id': picking.id,
                        'move_id': move.id,
                        'sale_line_id': move.sale_line_id.id,
                        'product_id': move.product_id.id,
                        'qty_available': move.product_uom_qty,
                        'qty_to_deliver': 0.0,
                        'is_selected': False,
                    }))
        return lines

    def _ensure_qty_on_selected(self):
        """Safety net: refresh qty from source if lost during save."""
        for line in self.line_ids.filtered('is_selected'):
            if line.qty_available <= 0 and line.move_line_id:
                line.qty_available = line.move_line_id.quantity or 0.0
            if line.qty_available <= 0 and line.move_id:
                line.qty_available = line.move_id.product_uom_qty
            if line.qty_to_deliver <= 0:
                line.qty_to_deliver = line.qty_available

    def _get_selected_lines(self):
        """Get selected lines, ensuring qty is filled."""
        self._ensure_qty_on_selected()
        selected = self.line_ids.filtered('is_selected')
        if not selected:
            raise UserError(_('Seleccione al menos una línea.'))
        for line in selected:
            if line.qty_to_deliver <= 0:
                raise UserError(_(
                    'La cantidad a entregar debe ser mayor a 0 para %s.',
                    line.product_id.display_name))
        return selected

    # ── Button actions ──

    def action_select_all(self):
        for line in self.line_ids:
            line.is_selected = True
            if line.qty_available > 0 and line.qty_to_deliver <= 0:
                line.qty_to_deliver = line.qty_available
        return self._refresh()

    def action_deselect_all(self):
        for line in self.line_ids:
            line.is_selected = False
            line.qty_to_deliver = 0.0
        return self._refresh()

    def action_generate_pick_ticket(self):
        """Generate pick ticket. Closes wizard.
        Next time user clicks 'Entregar', wizard will detect the PT
        and pre-load its selection automatically.
        """
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

        # Print pick ticket — this closes the wizard
        return self.env.ref(
            'sale_delivery_wizard.action_report_pick_ticket'
        ).report_action(doc)

    def action_print_pick_ticket(self):
        """Re-print the pick ticket."""
        self.ensure_one()
        if not self.pick_ticket_id:
            raise UserError(_('No hay Pick Ticket para imprimir.'))
        return self.env.ref(
            'sale_delivery_wizard.action_report_pick_ticket'
        ).report_action(self.pick_ticket_id)

    def action_generate_remission(self):
        """Generate remission ONLY for selected lines."""
        self.ensure_one()
        selected = self._get_selected_lines()

        # Validate no over-delivery
        for line in selected:
            if line.qty_to_deliver > line.qty_available:
                raise UserError(_(
                    'No puede entregar más de lo disponible para %s. '
                    'Disponible: %s, Solicitado: %s',
                    line.product_id.display_name,
                    line.qty_available, line.qty_to_deliver))

        # Check delivery auth
        order = self.sale_order_id
        if hasattr(order, 'delivery_auth_state'):
            if order.delivery_auth_state == 'pending':
                if not self.env.user.has_group(
                        'sale_delivery_wizard.group_delivery_authorizer'):
                    raise UserError(_(
                        'Entrega bloqueada: pedido sin autorización de pago.'))

        # Group by picking
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
                # Mark PT as confirmed (consumed)
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
        """Return action to refresh current wizard form without re-running default_get."""
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

    wizard_id = fields.Many2one(
        'sale.delivery.wizard', ondelete='cascade', required=True)
    is_selected = fields.Boolean(string='Sel.', default=False)

    picking_id = fields.Many2one('stock.picking', string='Picking')
    move_id = fields.Many2one('stock.move', string='Move')
    move_line_id = fields.Many2one('stock.move.line', string='Move Line')
    sale_line_id = fields.Many2one(
        'sale.order.line', string='Línea de Venta')
    product_id = fields.Many2one(
        'product.product', string='Producto', required=True)
    lot_id = fields.Many2one('stock.lot', string='Lote/Placa')
    source_location_id = fields.Many2one(
        'stock.location', string='Ubicación')

    qty_available = fields.Float(string='Disponible')
    qty_to_deliver = fields.Float(string='A Entregar')

    # Display helpers
    lot_name = fields.Char(related='lot_id.name', string='# Lote')
    product_name = fields.Char(
        related='product_id.display_name', string='Producto Desc.')

    @api.onchange('is_selected')
    def _onchange_is_selected(self):
        if self.is_selected and self.qty_to_deliver <= 0:
            self.qty_to_deliver = self.qty_available
        elif not self.is_selected:
            self.qty_to_deliver = 0.0

    @api.onchange('qty_to_deliver')
    def _onchange_qty_to_deliver(self):
        if self.qty_to_deliver > 0:
            self.is_selected = True
        if self.qty_to_deliver > self.qty_available:
            return {'warning': {
                'title': _('Cantidad excedida'),
                'message': _(
                    'La cantidad a entregar excede lo disponible (%s).',
                    self.qty_available),
            }}```

## ./wizard/sale_return_wizard_views.xml
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="sale_return_wizard_form" model="ir.ui.view">
        <field name="name">sale.return.wizard.form</field>
        <field name="model">sale.return.wizard</field>
        <field name="arch" type="xml">
            <form string="Devolución de Material" class="sale_return_wizard_form">
                <group>
                    <group>
                        <field name="sale_order_id" readonly="1"/>
                        <field name="return_reason_id"
                               options="{'no_create': True}"/>
                    </group>
                    <group>
                        <field name="return_action" widget="radio"/>
                        <field name="notes" placeholder="Notas adicionales..."/>
                    </group>
                </group>
                <separator string="Material Entregado"/>
                <field name="line_ids">
                    <list editable="bottom">
                        <field name="is_selected" widget="boolean_toggle"/>
                        <field name="product_id" readonly="1"/>
                        <field name="lot_id" readonly="1"/>
                        <field name="qty_delivered" string="Entregado" readonly="1"/>
                        <field name="qty_to_return" string="A Devolver"/>
                        <field name="sale_line_id" column_invisible="1"/>
                        <field name="move_id" column_invisible="1"/>
                        <field name="move_line_id" column_invisible="1"/>
                    </list>
                </field>
                <div class="alert alert-info mt-2" role="alert">
                    <strong>Reagendar:</strong> El material regresa al pedido para reentrega.<br/>
                    <strong>Reponer:</strong> El material se libera a inventario. La línea queda pendiente de nueva asignación.<br/>
                    <strong>Finiquitar:</strong> Se libera material, se genera nota de crédito y se cierra la línea.
                </div>
                <footer>
                    <button name="action_confirm_return"
                            string="Confirmar Devolución"
                            type="object" class="btn-primary"/>
                    <button string="Cancelar" class="btn-secondary" special="cancel"/>
                </footer>
            </form>
        </field>
    </record>
</odoo>```

## ./wizard/sale_return_wizard.py
```py
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class SaleReturnWizard(models.TransientModel):
    _name = 'sale.return.wizard'
    _description = 'Wizard de Devolución desde Orden de Venta'

    sale_order_id = fields.Many2one(
        'sale.order', string='Orden de Venta', required=True)
    return_reason_id = fields.Many2one(
        'sale.return.reason', string='Motivo', required=True)
    return_action = fields.Selection([
        ('reagendar', 'Reagendar - Reentrega del mismo material'),
        ('reponer', 'Reponer - Liberar y asignar nuevo material'),
        ('finiquitar', 'Finiquitar - Cerrar línea y nota de crédito'),
    ], string='Acción', required=True)
    notes = fields.Text(string='Notas')
    line_ids = fields.One2many(
        'sale.return.wizard.line', 'wizard_id', string='Líneas')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so_id = res.get('sale_order_id') or self.env.context.get('active_id')
        if not so_id:
            return res
        order = self.env['sale.order'].browse(so_id)
        res['sale_order_id'] = order.id

        lines = []
        for picking in order.picking_ids.filtered(
                lambda p: p.state == 'done'
                and p.picking_type_code == 'outgoing'):
            for move in picking.move_ids.filtered(
                    lambda m: m.state == 'done'):
                for ml in move.move_line_ids:
                    qty = ml.quantity or ml.qty_done or 0.0
                    if qty > 0:
                        lines.append((0, 0, {
                            'move_id': move.id,
                            'move_line_id': ml.id,
                            'sale_line_id': move.sale_line_id.id,
                            'product_id': move.product_id.id,
                            'lot_id': ml.lot_id.id if ml.lot_id else False,
                            'qty_delivered': qty,
                            'qty_to_return': qty,
                            'is_selected': True,
                        }))
        res['line_ids'] = lines
        return res

    def action_confirm_return(self):
        """Process the return: create return picking, validate it, and create document."""
        self.ensure_one()
        selected = self.line_ids.filtered('is_selected')
        if not selected:
            raise UserError(_(
                'Seleccione al menos una línea para devolver.'))

        order = self.sale_order_id

        # Group by original picking
        move_returns = {}
        for line in selected:
            if line.qty_to_return <= 0:
                raise UserError(_(
                    'La cantidad a devolver debe ser mayor a 0 para %s.',
                    line.product_id.display_name))
            picking = line.move_id.picking_id
            move_returns.setdefault(picking, []).append(line)

        return_pickings = self.env['stock.picking']
        for picking, lines in move_returns.items():
            return_wiz = self.env['stock.return.picking'].with_context(
                active_id=picking.id,
                active_model='stock.picking',
            ).create({})
            return_wiz.product_return_moves.unlink()
            for line in lines:
                self.env['stock.return.picking.line'].create({
                    'wizard_id': return_wiz.id,
                    'product_id': line.product_id.id,
                    'quantity': line.qty_to_return,
                    'move_id': line.move_id.id,
                    'uom_id': line.move_id.product_uom.id,
                })
            result = return_wiz.action_create_returns()
            if result and result.get('res_id'):
                ret_picking = self.env['stock.picking'].browse(
                    result['res_id'])
                return_pickings |= ret_picking

        # Create delivery document and validate each return picking
        docs = self.env['sale.delivery.document']
        for ret_picking in return_pickings:
            orig_picking = ret_picking.move_ids.mapped(
                'origin_returned_move_id.picking_id')
            picking_lines = [
                l for l in selected
                if l.move_id.picking_id in orig_picking
            ]
            if not picking_lines:
                picking_lines = selected

            doc = self.env['sale.delivery.document'].create({
                'document_type': 'return',
                'sale_order_id': order.id,
                'return_picking_id': ret_picking.id,
                'return_reason_id': self.return_reason_id.id,
                'return_action': self.return_action,
                'special_instructions': self.notes or '',
                'line_ids': [(0, 0, {
                    'product_id': line.product_id.id,
                    'lot_id': line.lot_id.id,
                    'qty_selected': line.qty_to_return,
                    'qty_done': line.qty_to_return,
                    'sale_line_id': line.sale_line_id.id,
                    'move_id': line.move_id.id,
                    'move_line_id': line.move_line_id.id,
                }) for line in picking_lines],
            })

            # Validate the return picking (receive material back)
            self._validate_return_picking(ret_picking, picking_lines)

            doc.state = 'confirmed'
            doc.delivery_date = fields.Datetime.now()
            docs |= doc

        # ── Post-return action ──
        if self.return_action == 'reagendar':
            self._action_reagendar(order, selected)

        action_label = dict(
            self._fields['return_action'].selection
        ).get(self.return_action)

        if self.return_action == 'reagendar':
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Devolución Procesada — Reentrega Creada'),
                    'message': _(
                        'Se recibió la devolución y se creó una reentrega '
                        'pendiente con el mismo material. '
                        'Confirme la reentrega cuando el material sea entregado.'),
                    'type': 'success',
                    'sticky': True,
                },
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Devolución Procesada'),
                'message': _(
                    '%d devolución(es) procesada(s) con acción: %s.',
                    len(docs), action_label),
                'type': 'success',
                'sticky': False,
            },
        }

    # ──────────────────────────────────────────────
    #  REAGENDAR: create new outgoing picking + redelivery doc
    # ──────────────────────────────────────────────

    def _action_reagendar(self, order, wizard_lines):
        """Create a new outgoing picking with the same lots/qty so the material
        stays reserved for this SO. Groups by product (one move per product,
        multiple move lines for lots).
        """
        warehouse = order.warehouse_id
        pick_type = warehouse.out_type_id
        if not pick_type:
            pick_type = self.env['stock.picking.type'].search([
                ('code', '=', 'outgoing'),
                ('warehouse_id', '=', warehouse.id),
            ], limit=1)
        if not pick_type:
            raise UserError(_(
                'No se encontró tipo de picking de salida para el almacén %s.',
                warehouse.name))

        new_picking = self.env['stock.picking'].create({
            'picking_type_id': pick_type.id,
            'partner_id': order.partner_shipping_id.id or order.partner_id.id,
            'origin': order.name,
            'location_id': pick_type.default_location_src_id.id,
            'location_dest_id': pick_type.default_location_dest_id.id
                or self.env.ref('stock.stock_location_customers').id,
            'sale_id': order.id,
        })

        # ── Group wizard lines by (product_id, sale_line_id, uom) ──
        grouped = {}
        for wl in wizard_lines:
            key = (wl.product_id.id, wl.sale_line_id.id, wl.move_id.product_uom.id)
            grouped.setdefault(key, []).append(wl)

        move_map = {}  # sale_line_id -> move, for doc lines later
        for (product_id, sale_line_id, uom_id), wls in grouped.items():
            total_qty = sum(wl.qty_to_return for wl in wls)

            move = self.env['stock.move'].create({
                'product_id': product_id,
                'product_uom_qty': total_qty,
                'product_uom': uom_id,
                'picking_id': new_picking.id,
                'location_id': new_picking.location_id.id,
                'location_dest_id': new_picking.location_dest_id.id,
                'sale_line_id': sale_line_id,
                'origin': order.name,
            })
            move_map[sale_line_id] = move

            # Create one move line per lot
            for wl in wls:
                if wl.lot_id:
                    self.env['stock.move.line'].create({
                        'move_id': move.id,
                        'product_id': product_id,
                        'lot_id': wl.lot_id.id,
                        'quantity': wl.qty_to_return,
                        'location_id': new_picking.location_id.id,
                        'location_dest_id': new_picking.location_dest_id.id,
                        'picking_id': new_picking.id,
                    })

        # Confirm and assign
        new_picking.action_confirm()
        new_picking.action_assign()

        _logger.info(
            'Redelivery picking %s created for SO %s (state: %s)',
            new_picking.name, order.name, new_picking.state)

        # Create redelivery document (prepared = pending confirmation)
        doc_lines = []
        for wl in wizard_lines:
            move = move_map.get(wl.sale_line_id.id)
            doc_lines.append((0, 0, {
                'product_id': wl.product_id.id,
                'lot_id': wl.lot_id.id,
                'qty_selected': wl.qty_to_return,
                'sale_line_id': wl.sale_line_id.id,
                'move_id': move.id if move else False,
                'source_location_id': new_picking.location_id.id,
            }))

        doc = self.env['sale.delivery.document'].create({
            'document_type': 'redelivery',
            'sale_order_id': order.id,
            'picking_id': new_picking.id,
            'delivery_address': order.partner_shipping_id.contact_address or '',
            'special_instructions': _(
                'REENTREGA por devolución. Material: %s',
                ', '.join(
                    '%s [%s] x%s' % (
                        wl.product_id.display_name,
                        wl.lot_id.name or 'S/L',
                        wl.qty_to_return,
                    ) for wl in wizard_lines
                )),
            'line_ids': doc_lines,
        })
        doc.action_prepare()

        _logger.info(
            'Redelivery document %s created for SO %s',
            doc.name, order.name)

        return doc

    # ──────────────────────────────────────────────
    #  Validate return picking
    # ──────────────────────────────────────────────

    def _validate_return_picking(self, picking, wizard_lines):
        """Assign and validate the return picking with the correct quantities."""
        if picking.state in ('draft', 'confirmed', 'waiting'):
            picking.action_assign()

        lot_qty = {}
        product_qty = {}
        for wl in wizard_lines:
            if wl.lot_id:
                lot_qty[wl.lot_id.id] = (
                    lot_qty.get(wl.lot_id.id, 0.0) + wl.qty_to_return)
            else:
                product_qty[wl.product_id.id] = (
                    product_qty.get(wl.product_id.id, 0.0)
                    + wl.qty_to_return)

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
                qty = 0.0
                lot_id = False
                for wl in wizard_lines:
                    if wl.product_id.id == move.product_id.id:
                        qty += wl.qty_to_return
                        if wl.lot_id:
                            lot_id = wl.lot_id.id

                if qty > 0:
                    self.env['stock.move.line'].create({
                        'move_id': move.id,
                        'picking_id': picking.id,
                        'product_id': move.product_id.id,
                        'lot_id': lot_id,
                        'quantity': qty,
                        'location_id': move.location_id.id,
                        'location_dest_id': move.location_dest_id.id,
                    })

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
            elif result.get('res_model') == 'stock.immediate.transfer':
                immediate_wiz = self.env['stock.immediate.transfer'].with_context(
                    button_validate_picking_ids=picking.ids,
                ).create({
                    'pick_ids': [(4, picking.id)],
                })
                immediate_wiz.process()

        if picking.state != 'done':
            _logger.warning(
                'Return picking %s could not be validated (state: %s)',
                picking.name, picking.state)

        return picking.state == 'done'


class SaleReturnWizardLine(models.TransientModel):
    _name = 'sale.return.wizard.line'
    _description = 'Línea de Wizard de Devolución'

    wizard_id = fields.Many2one(
        'sale.return.wizard', ondelete='cascade', required=True)
    is_selected = fields.Boolean(string='Sel.', default=False)
    move_id = fields.Many2one('stock.move', string='Move')
    move_line_id = fields.Many2one('stock.move.line', string='Move Line')
    sale_line_id = fields.Many2one(
        'sale.order.line', string='Línea de Venta')
    product_id = fields.Many2one(
        'product.product', string='Producto')
    lot_id = fields.Many2one('stock.lot', string='Lote/Placa')
    qty_delivered = fields.Float(string='Entregado')
    qty_to_return = fields.Float(string='A Devolver')

    @api.model_create_multi
    def create(self, vals_list):
        """Ensure product_id and lot_id are filled from move/move_line."""
        for vals in vals_list:
            if not vals.get('product_id') and vals.get('move_id'):
                move = self.env['stock.move'].browse(vals['move_id'])
                vals['product_id'] = move.product_id.id
            if not vals.get('lot_id') and vals.get('move_line_id'):
                ml = self.env['stock.move.line'].browse(vals['move_line_id'])
                vals['lot_id'] = ml.lot_id.id if ml.lot_id else False
            if not vals.get('qty_delivered') and vals.get('move_line_id'):
                ml = self.env['stock.move.line'].browse(vals['move_line_id'])
                vals['qty_delivered'] = ml.quantity or ml.qty_done or 0.0
        return super().create(vals_list)

    def _refresh_qty_delivered(self):
        """Re-read qty_delivered from move_line if lost by transient save."""
        if self.qty_delivered <= 0 and self.move_line_id:
            self.qty_delivered = (
                self.move_line_id.quantity
                or self.move_line_id.qty_done
                or 0.0)

    @api.onchange('is_selected')
    def _onchange_is_selected(self):
        if self.is_selected:
            self._refresh_qty_delivered()
            if self.qty_to_return <= 0:
                self.qty_to_return = self.qty_delivered
        else:
            self.qty_to_return = 0.0

    @api.onchange('qty_to_return')
    def _onchange_qty_to_return(self):
        if self.qty_to_return > 0:
            self.is_selected = True
        self._refresh_qty_delivered()
        if self.qty_to_return > self.qty_delivered and self.qty_delivered > 0:
            return {'warning': {
                'title': _('Cantidad excedida'),
                'message': _(
                    'La cantidad a devolver excede lo entregado (%s).',
                    self.qty_delivered),
            }}```

## ./wizard/sale_swap_wizard_views.xml
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="sale_swap_wizard_form" model="ir.ui.view">
        <field name="name">sale.swap.wizard.form</field>
        <field name="model">sale.swap.wizard</field>
        <field name="arch" type="xml">
            <form string="Swap de Lotes" class="sale_swap_wizard_form">
                <group>
                    <field name="sale_order_id" readonly="1"/>
                </group>
                <separator string="Lotes a Intercambiar"/>
                <field name="line_ids">
                    <list editable="bottom">
                        <field name="product_id"/>
                        <field name="origin_lot_id"
                               string="Lote Actual"
                               domain="[('product_id', '=', product_id)]"/>
                        <field name="target_lot_id"
                               string="Lote Nuevo"
                               domain="[('product_id', '=', product_id)]"/>
                        <field name="qty"/>
                    </list>
                </field>
                <div class="alert alert-warning mt-2" role="alert">
                    El swap reemplaza el lote asignado en el picking pendiente.
                    Verifique disponibilidad y que el lote destino no esté apartado en otra orden.
                </div>
                <footer>
                    <button name="action_confirm_swap"
                            string="Ejecutar Swap"
                            type="object" class="btn-primary"/>
                    <button string="Cancelar" class="btn-secondary" special="cancel"/>
                </footer>
            </form>
        </field>
    </record>
</odoo>```

## ./wizard/sale_swap_wizard.py
```py
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaleSwapWizard(models.TransientModel):
    _name = 'sale.swap.wizard'
    _description = 'Wizard de Swap de Lotes'

    sale_order_id = fields.Many2one(
        'sale.order', string='Orden de Venta', required=True)
    line_ids = fields.One2many(
        'sale.swap.wizard.line', 'wizard_id', string='Swaps')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so_id = res.get('sale_order_id') or self.env.context.get('active_id')
        if not so_id:
            return res
        res['sale_order_id'] = so_id
        return res

    def action_confirm_swap(self):
        """Execute lot swaps on pending pickings."""
        self.ensure_one()
        for line in self.line_ids:
            if not line.origin_lot_id or not line.target_lot_id:
                raise UserError(_(
                    'Debe seleccionar lote origen y destino para cada swap.'))
            if line.origin_lot_id == line.target_lot_id:
                raise UserError(_(
                    'El lote origen y destino no pueden ser el mismo.'))

            # Find the move line with the origin lot
            move_line = self.env['stock.move.line'].search([
                ('picking_id.sale_id', '=', self.sale_order_id.id),
                ('lot_id', '=', line.origin_lot_id.id),
                ('state', 'in', ('assigned', 'confirmed')),
            ], limit=1)
            if not move_line:
                raise UserError(_(
                    'No se encontró movimiento pendiente para el lote %s.',
                    line.origin_lot_id.name))

            # Check target lot availability
            target_quant = self.env['stock.quant'].search([
                ('lot_id', '=', line.target_lot_id.id),
                ('location_id.usage', '=', 'internal'),
                ('quantity', '>', 0),
            ], limit=1)
            if not target_quant:
                raise UserError(_(
                    'El lote destino %s no tiene stock disponible.',
                    line.target_lot_id.name))

            # Check hold status if stock_lot_dimensions is installed
            if hasattr(line.target_lot_id, 'hold_order_ids'):
                active_holds = line.target_lot_id.hold_order_ids.filtered(
                    lambda h: h.state == 'active'
                    and h.sale_order_id != self.sale_order_id)
                if active_holds:
                    raise UserError(_(
                        'El lote %s está apartado en otra orden.',
                        line.target_lot_id.name))

            # Execute swap
            move_line.lot_id = line.target_lot_id.id

            # Create swap record in delivery document
            self.env['sale.delivery.document'].create({
                'document_type': 'pick_ticket',
                'state': 'confirmed',
                'sale_order_id': self.sale_order_id.id,
                'special_instructions': _(
                    'SWAP: %s → %s',
                    line.origin_lot_id.name,
                    line.target_lot_id.name),
                'line_ids': [
                    (0, 0, {
                        'product_id': line.product_id.id,
                        'lot_id': line.origin_lot_id.id,
                        'qty_selected': line.qty,
                        'is_swap_origin': True,
                    }),
                    (0, 0, {
                        'product_id': line.product_id.id,
                        'lot_id': line.target_lot_id.id,
                        'qty_selected': line.qty,
                        'is_swap_target': True,
                    }),
                ],
            })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Swap Completado'),
                'message': _(
                    '%d swap(s) realizados exitosamente.') % len(self.line_ids),
                'type': 'success',
                'sticky': False,
            },
        }


class SaleSwapWizardLine(models.TransientModel):
    _name = 'sale.swap.wizard.line'
    _description = 'Línea de Swap'

    wizard_id = fields.Many2one(
        'sale.swap.wizard', ondelete='cascade', required=True)
    product_id = fields.Many2one(
        'product.product', string='Producto', required=True)
    origin_lot_id = fields.Many2one(
        'stock.lot', string='Lote Origen', required=True)
    target_lot_id = fields.Many2one(
        'stock.lot', string='Lote Destino', required=True)
    qty = fields.Float(string='Cantidad', default=1.0)
```

