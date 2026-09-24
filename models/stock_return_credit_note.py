# -*- coding: utf-8 -*-
"""NOTA DE CRÉDITO EN BORRADOR AL DEVOLVER (24 sep 2026).

Antes una devolución de cliente bajaba lo entregado pero no movía nada en
contabilidad: el saldo a favor de la orden solo aparecía cuando facturación
capturaba a mano la nota de crédito. Ahora, al validar una devolución de
cliente (asistente SOM o botón "Devolver" nativo: movimientos cliente →
almacén ligados a un renglón de venta) cuyos renglones ya estaban
FACTURADOS, se crea una nota de crédito en BORRADOR por la cantidad devuelta
al precio facturado, ligada a la factura original y a los renglones de venta.

Jamás se publica ni se timbra: facturación la revisa, la publica y la
concilia. Una sola vez por devolución (x_return_credit_note_ids). No se
genera cuando la devolución es para Reagendar o Reponer (el cliente vuelve a
recibir material) ni por lo que ya esté acreditado (publicado o borrador).
"""
import logging
from collections import defaultdict

from markupsafe import Markup, escape

from odoo import fields, models, _

_logger = logging.getLogger(__name__)

TOL = 0.0001


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    x_return_credit_note_ids = fields.Many2many(
        'account.move',
        'som_return_picking_credit_note_rel',
        'picking_id', 'move_id',
        string='Notas de crédito por devolución',
        copy=False,
        readonly=True,
    )

    def _action_done(self):
        res = super()._action_done()
        for picking in self:
            if picking.state != 'done' or picking.x_return_credit_note_ids:
                continue
            try:
                with self.env.cr.savepoint():
                    picking._som_return_create_draft_credit_notes()
            except Exception:
                # La devolución física manda: si la nota falla, la devolución
                # queda validada y se avisa en la orden.
                _logger.exception(
                    '[RETURN CN] No se pudo crear la nota de crédito de %s',
                    picking.name)
                order = picking._som_return_order()
                if order:
                    order.sudo().message_post(
                        body=Markup(_(
                            '<p>⚠️ No se pudo generar la nota de crédito en '
                            'borrador de la devolución <b>%s</b>. Facturación '
                            'debe capturarla a mano.</p>'
                        )) % picking.name,
                        subtype_xmlid='mail.mt_note',
                    )
        return res

    def _som_return_order(self):
        """Orden de la devolución: sale_id o, si viene vacío (devoluciones
        armadas a mano), la del renglón de venta de sus movimientos."""
        self.ensure_one()
        return self.sale_id or self.move_ids.sale_line_id.order_id[:1]

    def _som_return_credit_action(self):
        """Acción del documento de devolución SOM ligado (o False)."""
        self.ensure_one()
        Doc = self.env.get('sale.delivery.document')
        if Doc is None:
            return False
        doc = Doc.sudo().search([
            ('document_type', '=', 'return'),
            ('return_picking_id', '=', self.id),
        ], order='id desc', limit=1)
        if doc and doc.return_action:
            return doc.return_action
        # Devolución ligada a una orden de reposición (stonia_replacements):
        # el cliente recibe material de vuelta, igual que Reponer.
        Replacement = self.env.get('sale.replacement.order')
        if Replacement is not None and Replacement.sudo().search_count([
            '|', ('return_picking_id', '=', self.id),
            ('return_picking_ids', 'in', self.ids),
        ], limit=1):
            return 'reponer'
        return False

    def _som_return_create_draft_credit_notes(self):
        self.ensure_one()
        moves = self.move_ids.filtered(
            lambda m: m.state == 'done'
            and m._som_is_customer_return()
        )
        if not moves:
            return self.env['account.move']

        action = self._som_return_credit_action()
        if action in ('reagendar', 'reponer'):
            return self.env['account.move']

        # Cantidad devuelta por renglón, en la unidad del renglón.
        returned = defaultdict(float)
        for move in moves:
            line = move.sale_line_id
            qty = sum(ml.quantity or 0.0 for ml in move.move_line_ids) \
                or move.quantity or 0.0
            if move.product_uom and line.product_uom_id \
                    and move.product_uom != line.product_uom_id:
                qty = move.product_uom._compute_quantity(qty, line.product_uom_id)
            returned[line] += qty

        # Reparto contra las facturas publicadas del renglón (la más reciente
        # primero), descontando lo ya acreditado contra cada factura.
        by_invoice = defaultdict(list)
        for line, qty in returned.items():
            aml = line.sudo().invoice_lines.filtered(
                lambda l: l.move_id.state != 'cancel'
                and l.move_id.move_type in ('out_invoice', 'out_refund')
            )
            refunded_by_inv = defaultdict(float)
            for rl in aml.filtered(lambda l: l.move_id.move_type == 'out_refund'):
                refunded_by_inv[rl.move_id.reversed_entry_id.id or 0] += rl.quantity
            loose_refund = refunded_by_inv.pop(0, 0.0)
            inv_lines = aml.filtered(
                lambda l: l.move_id.move_type == 'out_invoice'
                and l.move_id.state == 'posted'
            ).sorted(lambda l: (l.move_id.invoice_date or fields.Date.today(), l.id),
                     reverse=True)
            remaining = qty
            for il in inv_lines:
                if remaining <= TOL:
                    break
                avail = il.quantity - refunded_by_inv.get(il.move_id.id, 0.0)
                # Notas sin factura de origen: consumen lo acreditable en orden.
                if loose_refund > TOL and avail > TOL:
                    use = min(loose_refund, avail)
                    avail -= use
                    loose_refund -= use
                if avail <= TOL:
                    continue
                take = min(remaining, avail)
                by_invoice[il.move_id].append((line, il, take))
                refunded_by_inv[il.move_id.id] += take
                remaining -= take

        if not by_invoice:
            return self.env['account.move']

        credit_notes = self.env['account.move']
        for invoice, items in by_invoice.items():
            Move = self.env['account.move'].sudo().with_company(invoice.company_id)
            line_vals = []
            for line, il, qty in items:
                vals = {
                    'product_id': il.product_id.id,
                    'name': il.name or line.name,
                    'quantity': qty,
                    'product_uom_id': il.product_uom_id.id,
                    'price_unit': il.price_unit,
                    'discount': il.discount,
                    'tax_ids': [(6, 0, il.tax_ids.ids)],
                    'sale_line_ids': [(6, 0, [line.id])],
                }
                if 'analytic_distribution' in il._fields and il.analytic_distribution:
                    vals['analytic_distribution'] = il.analytic_distribution
                line_vals.append((0, 0, vals))
            credit_notes |= Move.create({
                'move_type': 'out_refund',
                'partner_id': invoice.partner_id.id,
                'currency_id': invoice.currency_id.id,
                'journal_id': invoice.journal_id.id,
                'company_id': invoice.company_id.id,
                'reversed_entry_id': invoice.id,
                'invoice_origin': invoice.invoice_origin or self._som_return_order().name,
                'ref': _('Devolución %(pick)s de %(inv)s') % {
                    'pick': self.name, 'inv': invoice.name},
                'invoice_line_ids': line_vals,
            })

        self.sudo().x_return_credit_note_ids = [(6, 0, credit_notes.ids)]
        self._som_return_notify_credit_notes(credit_notes, action)
        return credit_notes

    def _som_return_notify_credit_notes(self, credit_notes, action):
        self.ensure_one()
        order = self._som_return_order().sudo()
        if not order:
            return
        links = Markup(', ').join(
            Markup('<a href="#" data-oe-model="account.move" data-oe-id="%d">%s</a>')
            % (cn.id, cn.display_name)
            for cn in credit_notes
        )
        total = sum(credit_notes.mapped('amount_total'))
        currency = credit_notes[:1].currency_id
        order.message_post(
            body=Markup(_(
                '<p>🧾 Devolución <b>%(pick)s</b>: se generó la nota de crédito '
                'en <b>borrador</b> %(links)s por %(total)s. Facturación debe '
                'revisarla, publicarla y conciliarla; hasta entonces no cuenta '
                'como saldo a favor.</p>'
            )) % {
                'pick': escape(self.name),
                'links': links,
                'total': escape('%s %s' % (currency.symbol or '', '{:,.2f}'.format(total))),
            },
            subtype_xmlid='mail.mt_note',
        )
        # El finiquito ya avisa a facturación (action_tc_close_allocation_short
        # con nota de crédito): no se duplica la actividad.
        if action == 'finiquitar':
            return
        if hasattr(order, '_credit_note_request_notify'):
            order._credit_note_request_notify(
                reason=_('Devolución %(pick)s — nota de crédito en borrador %(cn)s '
                         'lista para revisar y publicar.') % {
                    'pick': self.name,
                    'cn': ', '.join(credit_notes.mapped('display_name')),
                })
