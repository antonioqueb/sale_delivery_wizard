# -*- coding: utf-8 -*-
"""Planificación de entregas — el VENDEDOR programa, LOGÍSTICA ejecuta.

Problema: la operación era reactiva (pasa algo → acción). Logística
terminaba buscando pagos, pidiendo autorizaciones y adivinando direcciones.

Diseño:
- `sale.delivery.schedule` ("Entrega programada"): la unidad de planeación.
  Nace desde la orden de venta con TODO lo necesario (contacto, teléfono,
  dirección, ubicación en el mapa, especificación y fecha). Sin eso no se
  puede programar: la constraint lo impide.
- Logística la ve en su Planificación (primera vista de Entregas): calendario
  semanal por día con arrastre para reprogramar. Cada movimiento queda en
  `sale.delivery.schedule.move` (de/a, quién, por qué, origen) y avisa al
  vendedor por el Centro de Actividades.
- El vendedor la ve en su propio menú raíz "Mis Entregas" (solo las suyas)
  con el historial de movimientos.
- Cron nocturno: lo programado para un día que ya pasó y no se entregó se
  recorre al día siguiente (origen 'rollover') y avisa a los usuarios de
  entregas con UNA actividad resumen.
- Liga con la operación: el pick ticket de la orden se engancha solo a la
  programación abierta más próxima (en proceso); la remisión hereda; la
  firma cierra la programación (entregada).

Rediseño (27 sep 2026) — dos dominios separados:
- SOLICITUD (ventas/operación): asistente guiado desde la orden
  (`sale_delivery_wizard.delivery_request`): qué materiales y cuánto, dónde,
  cuándo y especificaciones. Sin vehículo, chofer ni documentos.
  Una orden = N solicitudes; cada una guarda sus propias líneas
  (`sale.delivery.schedule.line`) y el saldo pendiente se respeta.
- LOGÍSTICA: bandeja «Solicitudes» (Entregas) — confirma, reprograma,
  asigna unidad y chofer (Programada) y ejecuta con el flujo de entrega que
  ya existe (asistente / pick ticket / remisión). La programación coordina;
  no sustituye ni duplica la lógica de entrega.
"""
import logging
from datetime import date as ddate, datetime, timedelta
from zoneinfo import ZoneInfo

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import html2plaintext as tools_html2text

_logger = logging.getLogger(__name__)

MONTERREY = ZoneInfo('America/Monterrey')
MESES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
DIAS = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']

TIME_WINDOWS = [
    ('any', 'Todo el día'),
    ('am', 'Mañana (9–13 h)'),
    ('pm', 'Tarde (13–18 h)'),
    ('exact', 'Hora exacta'),
]
# Estados con la realidad operativa (27 sep 2026). Las claves técnicas
# viejas se conservan (datos y liga con pick ticket/remisión); cambian las
# etiquetas y se agregan Programada (logística asignó unidad) y
# Reprogramada (cambió la fecha: logística debe volver a confirmar).
STATES = [
    ('scheduled', 'Solicitada'),
    ('confirmed', 'Confirmada por logística'),
    ('programmed', 'Programada'),
    ('rescheduled', 'Reprogramada'),
    ('in_progress', 'En proceso'),
    ('done', 'Entregada'),
    ('cancelled', 'Cancelada'),
]
OPEN_STATES = ('scheduled', 'confirmed', 'programmed', 'rescheduled', 'in_progress')
# Abiertas y todavía sin documento de entrega (pueden pasar a En proceso).
PRE_EXEC_STATES = ('scheduled', 'confirmed', 'programmed', 'rescheduled')
QTY_TOL = 0.0001


def _fmt_date(d):
    return '%d %s %d' % (d.day, MESES[d.month - 1], d.year) if d else ''


def _fmt_qty(q):
    return ('%.2f' % (q or 0.0)).rstrip('0').rstrip('.')


class SaleDeliverySchedule(models.Model):
    _name = 'sale.delivery.schedule'
    _description = 'Entrega programada'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date asc, time_window asc, time_exact asc, id asc'
    _rec_name = 'name'

    name = fields.Char('Folio', readonly=True, copy=False, default='Nuevo')
    sale_order_id = fields.Many2one(
        'sale.order', string='Orden de venta', required=True, index=True, ondelete='cascade',
        domain="[('state', 'in', ('sale', 'done'))]")
    partner_id = fields.Many2one(related='sale_order_id.partner_id', string='Cliente', store=True)
    company_id = fields.Many2one(related='sale_order_id.company_id', store=True)
    user_id = fields.Many2one(
        'res.users', string='Vendedor', index=True, tracking=True,
        default=lambda self: self.env.user)

    date = fields.Date('Fecha de entrega', required=True, index=True, tracking=True)
    original_date = fields.Date('Fecha original', readonly=True, copy=False)
    time_window = fields.Selection(TIME_WINDOWS, 'Horario', default='any', required=True, tracking=True)
    time_exact = fields.Float('Hora', help='Solo con horario "Hora exacta". 14.5 = 14:30.')

    contact_name = fields.Char('Contacto en sitio', required=True)
    contact_phone = fields.Char('Teléfono del contacto', required=True)
    delivery_address = fields.Text('Dirección de entrega', required=True)
    partner_shipping_id = fields.Many2one(
        'res.partner', string='Dirección del cliente',
        domain="['|', ('id', '=', partner_id), ('commercial_partner_id', '=', commercial_partner_id)]",
        help='Elige otro contacto o dirección del cliente: rellena contacto, teléfono, '
             'dirección y ubicación en el mapa. Después puedes ajustar el texto y el punto.')
    commercial_partner_id = fields.Many2one(
        related='partner_id.commercial_partner_id', string='Empresa del cliente')
    latitude = fields.Float('Latitud', digits=(10, 7))
    longitude = fields.Float('Longitud', digits=(10, 7))
    has_location = fields.Boolean('Con ubicación en mapa', compute='_compute_has_location', store=True)
    instructions = fields.Text(
        'Especificaciones de la entrega',
        help='Qué se entrega, cómo se recibe, accesos, horarios del sitio, quién recibe, '
             'maniobra, equipo necesario. Es lo que logística va a leer.')

    state = fields.Selection(STATES, 'Estado', default='scheduled', required=True, tracking=True, index=True)
    line_ids = fields.One2many('sale.delivery.schedule.line', 'schedule_id', 'Materiales a entregar', copy=True)
    material_summary = fields.Char('Materiales', compute='_compute_material_summary', store=True)
    logistics_user_id = fields.Many2one(
        'res.users', 'Responsable de logística', tracking=True, index=True,
        help='Quién de logística atiende esta solicitud. Se asigna solo al confirmar.')
    vehicle_id = fields.Many2one('fleet.vehicle', 'Vehículo', tracking=True)
    vehicle_driver_id = fields.Many2one('res.partner', 'Chofer', tracking=True)
    pick_ticket_id = fields.Many2one('sale.delivery.document', 'Pick ticket', readonly=True, copy=False)
    remission_id = fields.Many2one('sale.delivery.document', 'Remisión', readonly=True, copy=False)
    delivered_at = fields.Datetime('Entregada el', readonly=True, copy=False)
    cancel_reason = fields.Text('Motivo de cancelación', readonly=True, copy=False)

    move_ids = fields.One2many('sale.delivery.schedule.move', 'schedule_id', 'Movimientos', readonly=True)
    reschedule_count = fields.Integer('Reprogramaciones', compute='_compute_reschedule_count', store=True)

    qty_summary = fields.Char('Pendiente por entregar', compute='_compute_order_info')
    auth_ok = fields.Boolean('Entrega autorizada o pagada', compute='_compute_order_info')
    auth_label = fields.Char(compute='_compute_order_info')
    readiness = fields.Char('Pendientes para logística', compute='_compute_order_info')
    color = fields.Integer(compute='_compute_color')

    # ------------------------------------------------------------------
    # Cómputos
    # ------------------------------------------------------------------
    @api.depends('latitude', 'longitude')
    def _compute_has_location(self):
        for rec in self:
            rec.has_location = bool(rec.latitude and rec.longitude)

    @api.depends('line_ids.qty', 'line_ids.product_id', 'line_ids.uom_name')
    def _compute_material_summary(self):
        for rec in self:
            if not rec.line_ids:
                # Programaciones anteriores al rediseño: sin detalle por material.
                rec.material_summary = _('Toda la orden (sin detalle)')
                continue
            rec.material_summary = ' · '.join(
                '%s — %s %s' % (l.product_id.display_name or '', _fmt_qty(l.qty), l.uom_name or '')
                for l in rec.line_ids)

    @api.depends('move_ids')
    def _compute_reschedule_count(self):
        for rec in self:
            rec.reschedule_count = len(rec.move_ids.filtered(lambda m: m.kind == 'reschedule'))

    @api.depends('sale_order_id', 'sale_order_id.order_line.qty_delivered', 'state')
    def _compute_order_info(self):
        for rec in self:
            order = rec.sale_order_id
            by_uom = {}
            for line in order.order_line:
                if line.display_type or not line.product_id or line.product_id.type == 'service':
                    continue
                pending = (line.product_uom_qty or 0.0) - (line.qty_delivered or 0.0)
                if pending <= 0:
                    continue
                uom = line.product_uom_id.name if 'product_uom_id' in line._fields else line.product_uom.name
                by_uom[uom] = by_uom.get(uom, 0.0) + pending
            rec.qty_summary = ' · '.join('%g %s' % (round(q, 1), u) for u, q in by_uom.items()) or 'Nada pendiente'
            auth = getattr(order, 'delivery_auth_state', None)
            if auth is None:
                rec.auth_ok, rec.auth_label = True, ''
            else:
                rec.auth_ok = auth in ('authorized', 'paid')
                rec.auth_label = {
                    'paid': 'Pagada', 'authorized': 'Autorizada', 'requested': 'Autorización solicitada',
                }.get(auth, 'Sin autorización de entrega')
            issues = []
            if not rec.auth_ok:
                issues.append(rec.auth_label)
            if not rec.has_location:
                issues.append('Sin ubicación en mapa')
            if not rec.vehicle_id and rec.state == 'confirmed':
                issues.append('Sin camión')
            rec.readiness = ' · '.join(issues)

    @api.depends('state')
    def _compute_color(self):
        palette = {'scheduled': 4, 'confirmed': 10, 'programmed': 10, 'rescheduled': 3,
                   'in_progress': 2, 'done': 10, 'cancelled': 1}
        for rec in self:
            rec.color = palette.get(rec.state, 0)

    # ------------------------------------------------------------------
    # Dirección del cliente → contacto, teléfono, dirección y ubicación
    # ------------------------------------------------------------------
    @api.model
    def _som_vals_from_partner(self, partner):
        """Valores de la programación a partir de un contacto del cliente.

        Si el contacto no tiene calle/ciudad, la dirección (y sus
        coordenadas) se toman de la empresa. El teléfono solo se pisa si el
        contacto trae uno."""
        if not partner:
            return {}
        addr_partner = partner
        if not (partner.street or partner.street2 or partner.city or partner.zip):
            commercial = partner.commercial_partner_id
            if commercial and commercial != partner and (
                commercial.street or commercial.city or commercial.zip
            ):
                addr_partner = commercial
        lines = [
            ' '.join(x for x in [addr_partner.street or '', addr_partner.street2 or ''] if x),
            ', '.join(x for x in [
                addr_partner.city or '', addr_partner.state_id.name or '', addr_partner.zip or ''] if x),
            addr_partner.country_id.name or '',
        ]
        vals = {
            'contact_name': partner.name or partner.commercial_partner_id.name or '',
            'delivery_address': '\n'.join(l for l in lines if l.strip()),
            'latitude': getattr(addr_partner, 'partner_latitude', 0.0) or 0.0,
            'longitude': getattr(addr_partner, 'partner_longitude', 0.0) or 0.0,
        }
        phone = partner.phone or addr_partner.phone
        if phone:
            vals['contact_phone'] = phone
        return vals

    @api.onchange('partner_shipping_id')
    def _onchange_partner_shipping_id(self):
        if self.partner_shipping_id:
            self.update(self._som_vals_from_partner(self.partner_shipping_id))

    # ------------------------------------------------------------------
    # Reglas: sin información completa no hay programación
    # ------------------------------------------------------------------
    @api.constrains('contact_phone', 'delivery_address', 'latitude', 'longitude', 'state', 'date')
    def _check_complete(self):
        # Especificaciones: opcionales desde el rediseño (27 sep 2026, paso
        # "¿Existe alguna especificación adicional?").
        for rec in self.filtered(lambda r: r.state in PRE_EXEC_STATES):
            missing = []
            if not (rec.contact_phone or '').strip():
                missing.append('teléfono del contacto')
            if len((rec.delivery_address or '').strip()) < 10:
                missing.append('dirección de entrega completa')
            if not (rec.latitude and rec.longitude):
                missing.append('ubicación en el mapa')
            if missing:
                raise ValidationError(_(
                    'No se puede programar la entrega de %s sin: %s.\n'
                    'Logística solo ejecuta: la información completa la da el vendedor.'
                ) % (rec.sale_order_id.name, ', '.join(missing)))
            if rec.sale_order_id.state not in ('sale', 'done'):
                raise ValidationError(_('Solo se programan entregas de órdenes confirmadas.'))

    @api.constrains('time_window', 'time_exact')
    def _check_time(self):
        for rec in self:
            if rec.time_window == 'exact' and not (0 <= (rec.time_exact or 0) < 24):
                raise ValidationError(_('Captura una hora válida (0–23:59).'))

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'Nuevo') == 'Nuevo':
                vals['name'] = self.env['ir.sequence'].next_by_code('sale.delivery.schedule') or 'Nuevo'
            if vals.get('sale_order_id'):
                # El candado también aplica creando desde la lista o por RPC.
                gate_order = self.env['sale.order'].browse(vals['sale_order_id']).exists()
                reason = gate_order._som_schedule_block_reason() if gate_order else False
                if reason:
                    raise UserError(reason)
            if vals.get('sale_order_id') and not vals.get('user_id'):
                order = self.env['sale.order'].browse(vals['sale_order_id'])
                vals['user_id'] = order.user_id.id or self.env.uid
            if vals.get('date') and not vals.get('original_date'):
                vals['original_date'] = vals['date']
        records = super().create(vals_list)
        for rec in records:
            rec.sale_order_id.message_post(body=_(
                '📅 <b>Solicitud de entrega</b> %s para el <b>%s</b> (%s) por %s.%s'
            ) % (rec.name, _fmt_date(rec.date), dict(TIME_WINDOWS)[rec.time_window], self.env.user.name,
                 (' Materiales: %s.' % rec.material_summary) if rec.line_ids else ''),
                message_type='notification', subtype_xmlid='mail.mt_note')
        records._som_notify_logistics_new()
        return records

    def _som_logistics_notice_users(self):
        group = self.env.ref('sale_delivery_auth.group_delivery_logistics', raise_if_not_found=False)
        if not group:
            return self.env['res.users']
        # Odoo 19: user_ids trae solo miembros DIRECTOS; all_user_ids incluye
        # a quien recibe el grupo por implicación (se quedaban sin aviso).
        group = group.sudo()
        members = group.all_user_ids if 'all_user_ids' in group._fields else group.user_ids
        return members.filtered(lambda u: u.active and not u.share)

    def _som_notify_logistics_new(self):
        """Al guardar la programación arranca el proceso de logística: aviso
        (Centro de Actividades) a los usuarios del grupo «Logística — Avisos»
        para que confirmen con camión y chofer. Sin el grupo instalado o sin
        usuarios, no hace nada."""
        users = self._som_logistics_notice_users()
        for rec in self:
            for user in users:
                if user == self.env.user:
                    continue
                rec.activity_schedule(
                    'mail.mail_activity_data_todo', user_id=user.id,
                    summary=_('Confirmar entrega: %s · %s · %s') % (
                        rec.sale_order_id.name, rec.partner_id.name or '', _fmt_date(rec.date)),
                    note=_('<p>%s programó la entrega <b>%s</b> para el <b>%s</b> (%s).</p>'
                           '<p>Asigna camión y chofer y confirma.</p>') % (
                        self.env.user.name, rec.name, _fmt_date(rec.date),
                        dict(TIME_WINDOWS).get(rec.time_window, '')),
                    date_deadline=rec.date)

    def write(self, vals):
        if vals.get('state') in ('confirmed', 'programmed') and not self.env.su and not (
                self._is_delivery_staff()
                or self.env.user.has_group('base.group_system')):
            raise UserError(_('Solo logística (Usuario de Entregas) confirma entregas programadas.'))
        # La fecha solo cambia por action_reschedule (deja historial). Un
        # write directo con otra fecha se registra igual como reprogramación.
        if 'date' in vals and not self.env.context.get('som_schedule_move'):
            new_date = fields.Date.to_date(vals['date'])
            for rec in self:
                if rec.date != new_date:
                    rec._log_move('reschedule', rec.date, new_date, _('Cambio directo de fecha'), 'manual')
        return super().write(vals)

    def _log_move(self, kind, date_from, date_to, reason, source):
        self.ensure_one()
        return self.env['sale.delivery.schedule.move'].sudo().create({
            'schedule_id': self.id,
            'kind': kind,
            'date_from': date_from,
            'date_to': date_to,
            'reason': reason or '',
            'source': source,
            'user_id': self.env.uid,
        })

    def _is_delivery_staff(self):
        u = self.env.user
        return u.has_group('sale_delivery_wizard.group_delivery_user')

    def action_reschedule(self, new_date, reason='', source='logistics'):
        """Mueve la entrega a otra fecha dejando historial y avisando."""
        new_date = fields.Date.to_date(new_date)
        if not new_date:
            raise UserError(_('Indica la nueva fecha.'))
        for rec in self:
            if rec.state not in OPEN_STATES:
                raise UserError(_('%s ya está %s; no se reprograma.') % (rec.name, dict(STATES)[rec.state].lower()))
            if rec.date == new_date:
                continue
            old = rec.date
            rec._log_move('reschedule', old, new_date, reason, source)
            vals = {'date': new_date}
            # La movió alguien que NO es logística (el solicitante o un cambio
            # directo): logística la vuelve a confirmar y queda en su bandeja
            # como Reprogramada. Si la movió logística, la decisión ya es
            # suya y el estado se conserva (el historial la registra). Con
            # documento de entrega (En proceso) el estado nunca retrocede.
            if source != 'logistics' and rec.state in PRE_EXEC_STATES:
                vals['state'] = 'rescheduled'
            rec.with_context(som_schedule_move=True).write(vals)
            who = self.env.user.name
            body = _('📅 Entrega <b>reprogramada</b> del %s al <b>%s</b> por %s.%s') % (
                _fmt_date(old), _fmt_date(new_date), who,
                (' Motivo: %s' % reason) if reason else '')
            rec.message_post(body=body, message_type='notification', subtype_xmlid='mail.mt_note')
            rec.sale_order_id.message_post(body=body, message_type='notification', subtype_xmlid='mail.mt_note')
            # El solicitante movió la fecha: la solicitud vuelve a la bandeja
            # de logística como Reprogramada y se le avisa para confirmar.
            if source == 'seller':
                for user in rec._som_logistics_notice_users():
                    if user == self.env.user:
                        continue
                    rec.activity_schedule(
                        'mail.mail_activity_data_todo', user_id=user.id,
                        summary=_('Entrega reprogramada por el solicitante: %s · %s') % (
                            rec.sale_order_id.name, rec.partner_id.name or ''),
                        note=_('<p>%s movió la solicitud <b>%s</b> del %s al <b>%s</b>. Confírmala de nuevo.</p><p>%s</p>') % (
                            who, rec.name, _fmt_date(old), _fmt_date(new_date),
                            ('Motivo: %s' % reason) if reason else ''),
                        date_deadline=new_date)
            # Aviso al vendedor cuando NO fue él quien movió (Centro de Actividades).
            if rec.user_id and rec.user_id != self.env.user and source != 'seller':
                rec.activity_schedule(
                    'mail.mail_activity_data_todo', user_id=rec.user_id.id,
                    summary=_('Entrega reprogramada: %s · %s') % (rec.sale_order_id.name, rec.partner_id.name or ''),
                    note=_('<p>%s movió la entrega <b>%s</b> del %s al <b>%s</b>.</p><p>%s</p>') % (
                        who, rec.name, _fmt_date(old), _fmt_date(new_date),
                        ('Motivo: %s' % reason) if reason else 'Sin motivo capturado.'),
                    date_deadline=new_date)
        return True

    def _som_check_logistics(self):
        # El candado vive en el servidor, no solo en la vista (el vendedor
        # tiene escritura y confirmaba por RPC).
        if not (self.env.su or self._is_delivery_staff() or self.env.user.has_group('base.group_system')):
            raise UserError(_('Solo logística (Usuario de Entregas) confirma y programa entregas.'))

    def _som_close_request_activities(self, feedback):
        """Cierra los avisos «Confirmar entrega» de logística (antes quedaban
        colgados en el Centro después de confirmar o cancelar)."""
        for rec in self:
            acts = rec.sudo().activity_ids.filtered(
                lambda a: (a.summary or '').startswith(('Confirmar entrega', 'Entrega reprogramada por el solicitante')))
            if acts:
                acts.action_feedback(feedback=feedback)

    def action_confirm(self):
        self._som_check_logistics()
        for rec in self:
            if rec.state not in ('scheduled', 'rescheduled'):
                raise UserError(_('Solo se confirman solicitudes pendientes (Solicitada o Reprogramada).'))
            # Camión y chofer NO son obligatorios en la programación (22 sep
            # 2026): quien programa es el vendedor y no sabe qué unidad irá;
            # la unidad se define al generar la entrega (asistente). Si
            # logística ya puso camión, el chofer se toma de la unidad.
            if not rec.vehicle_driver_id and 'driver_id' in rec.vehicle_id._fields and rec.vehicle_id.driver_id:
                rec.vehicle_driver_id = rec.vehicle_id.driver_id
            vals = {'state': 'confirmed'}
            if not rec.logistics_user_id:
                vals['logistics_user_id'] = self.env.uid
            # Ya traía unidad (confirmada antes de reprogramarse): vuelve a
            # quedar Programada.
            if rec.vehicle_id:
                vals['state'] = 'programmed'
            rec.write(vals)
            rec._som_close_request_activities(_('Confirmada por %s') % self.env.user.name)
            rec.message_post(body=_('✅ Confirmada por logística (%s).') % self.env.user.name,
                             message_type='notification', subtype_xmlid='mail.mt_note')
        return True

    def action_program(self, vehicle_id=None, driver_id=None, logistics_user_id=None):
        """Logística asigna unidad (y chofer) → Programada. El chofer sale de
        la unidad si no se indica. Confirma de paso si venía pendiente."""
        self._som_check_logistics()
        for rec in self:
            if rec.state not in ('scheduled', 'rescheduled', 'confirmed', 'programmed'):
                raise UserError(_('%s ya está %s; no se programa.') % (rec.name, dict(STATES)[rec.state].lower()))
            vehicle = self.env['fleet.vehicle'].browse(vehicle_id) if vehicle_id else rec.vehicle_id
            if not vehicle:
                raise UserError(_('Asigna la unidad (vehículo) para programar la entrega.'))
            driver = self.env['res.partner'].browse(driver_id) if driver_id else (
                rec.vehicle_driver_id if rec.vehicle_id == vehicle and rec.vehicle_driver_id else
                (vehicle.driver_id if 'driver_id' in vehicle._fields else self.env['res.partner']))
            vals = {
                'state': 'programmed',
                'vehicle_id': vehicle.id,
                'vehicle_driver_id': driver.id if driver else False,
                'logistics_user_id': logistics_user_id or rec.logistics_user_id.id or self.env.uid,
            }
            rec.write(vals)
            rec._som_close_request_activities(_('Programada por %s') % self.env.user.name)
            rec.message_post(body=_('🚚 Programada por logística (%s): unidad <b>%s</b>%s.') % (
                self.env.user.name, vehicle.display_name,
                (', chofer %s' % driver.display_name) if driver else ''),
                message_type='notification', subtype_xmlid='mail.mt_note')
        return True

    def action_cancel(self, reason=None):
        reason = (reason or self.env.context.get('cancel_reason') or '').strip()
        for rec in self:
            if rec.state == 'done':
                raise UserError(_('Una entrega ya realizada no se cancela.'))
            rec._log_move('cancel', rec.date, False, reason, 'seller' if not rec._is_delivery_staff() else 'logistics')
            rec.write({'state': 'cancelled', 'cancel_reason': reason})
            rec._som_close_request_activities(_('Cancelada por %s') % self.env.user.name)
            body = _('⛔ Programación <b>cancelada</b> por %s.%s') % (
                self.env.user.name, (' Motivo: %s' % reason) if reason else '')
            rec.message_post(body=body, message_type='notification', subtype_xmlid='mail.mt_note')
            rec.sale_order_id.message_post(body=body, message_type='notification', subtype_xmlid='mail.mt_note')
        return True

    def action_reopen(self):
        for rec in self:
            if rec.state != 'cancelled':
                continue
            # Reabrir = volver a programar: mismo candado de pago/autorización.
            reason = rec.sale_order_id._som_schedule_block_reason()
            if reason:
                raise UserError(reason)
            rec.write({'state': 'scheduled', 'cancel_reason': False})
        return True

    def action_mark_done(self, delivered_at=None):
        for rec in self:
            if rec.state == 'done':
                continue
            rec.write({'state': 'done', 'delivered_at': delivered_at or fields.Datetime.now()})
            rec.message_post(body=_('📦 Entrega <b>realizada</b>.'), message_type='notification',
                             subtype_xmlid='mail.mt_note')
        return True

    def _som_pre_exec_state(self):
        """Estado al que vuelve si se cancela su documento de entrega."""
        self.ensure_one()
        if self.vehicle_id:
            return 'programmed'
        return 'confirmed' if self.logistics_user_id else 'scheduled'

    def action_open_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'res_model': 'sale.order', 'res_id': self.sale_order_id.id,
            'view_mode': 'form', 'target': 'current',
        }

    def _instructions_for_wizard(self):
        """Especificaciones + materiales solicitados, para que quien arma la
        entrega en el asistente (flujo existente) sepa qué pidió la solicitud."""
        self.ensure_one()
        parts = []
        if self.line_ids:
            parts.append(_('Materiales solicitados (%s): %s') % (self.name, self.material_summary))
        if self.instructions:
            parts.append(self.instructions)
        return '\n'.join(parts)

    def action_generate_delivery(self):
        """Logística ejecuta: abre el asistente de entrega de la orden con la
        dirección y el camión de la programación ya puestos."""
        self.ensure_one()
        if self.state not in OPEN_STATES:
            raise UserError(_('La programación %s ya está %s.') % (self.name, dict(STATES)[self.state].lower()))
        action = self.sale_order_id.with_context(
            default_delivery_address=self._address_for_wizard(),
            default_special_instructions=self._instructions_for_wizard(),
            default_vehicle_id=self.vehicle_id.id,
            default_vehicle_driver_id=self.vehicle_driver_id.id,
            som_schedule_id=self.id,
        ).action_open_delivery_wizard()
        if isinstance(action, dict):
            ctx = dict(action.get('context') or {})
            ctx.update({
                'default_delivery_address': self._address_for_wizard(),
                'default_special_instructions': self._instructions_for_wizard(),
                'som_schedule_id': self.id,
            })
            if self.vehicle_id:
                ctx['default_vehicle_id'] = self.vehicle_id.id
            if self.vehicle_driver_id:
                ctx['default_vehicle_driver_id'] = self.vehicle_driver_id.id
            action['context'] = ctx
        return action

    def _address_for_wizard(self):
        self.ensure_one()
        parts = [self.contact_name or '', self.delivery_address or '']
        if self.contact_phone:
            parts.append('Tel. %s' % self.contact_phone)
        if self.has_location:
            parts.append('Mapa: https://maps.google.com/?q=%s,%s' % (self.latitude, self.longitude))
        return '\n'.join(p for p in parts if p)

    def action_open_map(self):
        self.ensure_one()
        if not self.has_location:
            raise UserError(_('Esta programación no tiene ubicación en el mapa.'))
        return {'type': 'ir.actions.act_url', 'target': 'new',
                'url': 'https://maps.google.com/?q=%s,%s' % (self.latitude, self.longitude)}

    # ------------------------------------------------------------------
    # Solicitud guiada (client action sale_delivery_wizard.delivery_request)
    # ------------------------------------------------------------------
    @api.model
    def _som_request_order(self, order_id):
        order = self.env['sale.order'].browse(int(order_id or 0)).exists()
        if not order:
            raise UserError(_('La orden de venta ya no existe.'))
        order.check_access('read')
        return order

    @api.model
    def _som_order_material_lines(self, order):
        """Materiales de la orden con su saldo: pedido, entregado, ya
        programado (solicitudes abiertas), pendiente de programar y
        disponible para entregar (el MISMO cálculo que usa el asistente de
        entrega; solo se lee, no se duplica la lógica)."""
        Line = self.env['sale.delivery.schedule.line'].sudo()
        open_lines = Line.search([
            ('schedule_id.sale_order_id', '=', order.id),
            ('schedule_id.state', 'in', OPEN_STATES),
        ])
        scheduled = {}
        for l in open_lines:
            scheduled[l.sale_line_id.id] = scheduled.get(l.sale_line_id.id, 0.0) + l.qty
        available_by_line, available_by_product = {}, {}
        available_known = True
        try:
            for group in order.sudo().get_delivery_grouped_data(mode='delivery') or []:
                for ld in group.get('lines', []):
                    qty = float(ld.get('qtyAvailable') or 0.0)
                    if ld.get('saleLineId'):
                        available_by_line[ld['saleLineId']] = available_by_line.get(ld['saleLineId'], 0.0) + qty
                    else:
                        pid = ld.get('productId') or 0
                        available_by_product[pid] = available_by_product.get(pid, 0.0) + qty
        except Exception:  # noqa: BLE001 — el disponible es informativo; la solicitud no se bloquea por él
            _logger.exception('[SOLICITUD ENTREGA] no se pudo calcular el disponible de %s', order.name)
            available_known = False
        out = []
        for line in order.order_line:
            if line.display_type or not line.product_id or line.product_id.type == 'service':
                continue
            if hasattr(line, '_is_delivery') and line._is_delivery():
                continue
            ordered = line.product_uom_qty or 0.0
            delivered = line.qty_delivered or 0.0
            sched = scheduled.get(line.id, 0.0)
            pending = max(0.0, ordered - delivered - sched)
            if line.id in available_by_line:
                available = available_by_line[line.id]
            elif line.product_id.id in available_by_product:
                available = available_by_product.pop(line.product_id.id)
            else:
                available = 0.0 if available_known else None
            uom = line.product_uom_id if 'product_uom_id' in line._fields else line.product_uom
            out.append({
                'sale_line_id': line.id,
                'product': line.product_id.display_name,
                'description': (line.name or '').split('\n')[0][:120],
                'uom': uom.name if uom else '',
                'ordered': ordered,
                'delivered': delivered,
                'scheduled': sched,
                'pending': pending,
                'available': available,
            })
        return out

    @api.model
    def _som_address_payload(self, partner):
        vals = self._som_vals_from_partner(partner)
        state = partner.state_id
        return {
            'id': partner.id,
            'name': partner.name or partner.commercial_partner_id.name or '',
            'type': partner.type,
            'type_label': dict(partner._fields['type']._description_selection(self.env)).get(partner.type, ''),
            'is_company': bool(partner.is_company),
            'contact_name': vals.get('contact_name') or '',
            'contact_phone': vals.get('contact_phone') or partner.phone or '',
            'address': vals.get('delivery_address') or '',
            'street': partner.street or '',
            'street2': partner.street2 or '',
            'city': partner.city or '',
            'state': state.name if state else '',
            'zip': partner.zip or '',
            'references': (partner.comment and tools_html2text(partner.comment)) or '',
            'latitude': vals.get('latitude') or 0.0,
            'longitude': vals.get('longitude') or 0.0,
            'has_address': len((vals.get('delivery_address') or '').strip()) >= 10,
        }

    @api.model
    def _som_order_addresses(self, order):
        commercial = order.partner_id.commercial_partner_id
        candidates = (commercial | commercial.child_ids | order.partner_shipping_id | order.partner_id).filtered(
            lambda p: p.active and p.type in ('delivery', 'contact', 'other', 'invoice') or p == commercial)
        # Primero las direcciones de ENTREGA, luego la de la orden, luego el resto.
        def rank(p):
            return (0 if p.type == 'delivery' else 1 if p == order.partner_shipping_id else 2 if p == commercial else 3,
                    p.name or '')
        return [self._som_address_payload(p) for p in candidates.sorted(key=rank)]

    @api.model
    def request_prepare(self, order_id):
        """Todo lo que el asistente de solicitud necesita en una llamada."""
        order = self._som_request_order(order_id)
        block = order._som_schedule_block_reason()
        addresses = self._som_order_addresses(order)
        preferred = next((a for a in addresses if a['type'] == 'delivery' and a['has_address']), None) \
            or next((a for a in addresses if a['id'] == order.partner_shipping_id.id and a['has_address']), None) \
            or next((a for a in addresses if a['has_address']), None)
        mx = self.env.ref('base.mx', raise_if_not_found=False)
        states = self.env['res.country.state'].search([('country_id', '=', mx.id)], order='name') if mx else []
        return {
            'order': {
                'id': order.id,
                'name': order.name,
                'partner': order.partner_id.display_name,
                'seller': order.user_id.name or '',
            },
            'blocked': block or False,
            'lines': self._som_order_material_lines(order),
            'addresses': addresses,
            'default_address_id': preferred['id'] if preferred else False,
            'default_date': (datetime.now(MONTERREY).date() + timedelta(days=1)).isoformat(),
            'today': datetime.now(MONTERREY).date().isoformat(),
            'time_windows': [{'key': k, 'label': v} for k, v in TIME_WINDOWS],
            'states': [{'id': st.id, 'name': st.name} for st in states],
            'existing': [{
                'id': r.id, 'name': r.name, 'date': _fmt_date(r.date), 'state': dict(STATES)[r.state],
                'materials': r.material_summary or '',
            } for r in order.delivery_schedule_ids.filtered(lambda r: r.state in OPEN_STATES).sorted('date')],
        }

    @api.model
    def request_create_address(self, order_id, vals):
        """«+ Crear dirección de entrega» sin salir de la solicitud: nace
        como dirección de entrega del cliente y se devuelve lista para usarse."""
        order = self._som_request_order(order_id)
        vals = vals or {}
        errors = {}
        for key, label in (('name', 'Nombre o sitio'), ('phone', 'Teléfono'), ('street', 'Calle'),
                           ('number', 'Número'), ('street2', 'Colonia'), ('city', 'Ciudad'),
                           ('state_id', 'Estado'), ('zip', 'Código postal')):
            if not str(vals.get(key) or '').strip():
                errors[key] = _('Este campo es obligatorio.')
        if errors:
            return {'errors': errors}
        commercial = order.partner_id.commercial_partner_id
        mx = self.env.ref('base.mx', raise_if_not_found=False)
        street = ' '.join(x for x in [str(vals.get('street') or '').strip(), str(vals.get('number') or '').strip()] if x)
        partner = self.env['res.partner'].sudo().create({
            'type': 'delivery',
            'parent_id': commercial.id,
            'name': vals['name'].strip(),
            'phone': str(vals['phone']).strip(),
            'street': street,
            'street2': str(vals.get('street2') or '').strip(),
            'city': str(vals.get('city') or '').strip(),
            'state_id': int(vals['state_id']),
            'zip': str(vals.get('zip') or '').strip(),
            'country_id': mx.id if mx else False,
            'comment': str(vals.get('references') or '').strip() or False,
            'company_id': commercial.company_id.id or False,
        })
        order.message_post(body=_('📍 Nueva dirección de entrega creada desde la solicitud: <b>%s</b>.') % partner.display_name,
                           message_type='notification', subtype_xmlid='mail.mt_note')
        return {'address': self._som_address_payload(partner)}

    @api.model
    def request_submit(self, order_id, payload):
        """Valida y crea la solicitud. Errores por campo para que la pantalla
        marque cada uno en rojo junto a su dato: {'errors': {campo: msg}}."""
        order = self._som_request_order(order_id)
        block = order._som_schedule_block_reason()
        if block:
            return {'errors': {'general': block}}
        payload = payload or {}
        errors = {}
        # Paso 1 — materiales
        material_rows = {r['sale_line_id']: r for r in self._som_order_material_lines(order)}
        lines = []
        line_errors = {}
        for item in payload.get('lines') or []:
            slid = int(item.get('sale_line_id') or 0)
            qty = float(item.get('qty') or 0.0)
            if qty <= QTY_TOL:
                continue
            row = material_rows.get(slid)
            if not row:
                line_errors[slid] = _('Este material ya no está en la orden.')
                continue
            if qty > row['pending'] + QTY_TOL:
                line_errors[slid] = _('Máximo %s %s pendientes de programar.') % (_fmt_qty(row['pending']), row['uom'])
                continue
            lines.append((0, 0, {'sale_line_id': slid, 'qty': qty}))
        if line_errors:
            errors['lines'] = _('Revisa las cantidades marcadas.')
            errors['line_errors'] = {str(k): v for k, v in line_errors.items()}
        elif not lines:
            errors['lines'] = _('Selecciona al menos un material y la cantidad a entregar.')
        # Paso 2 — dirección y ubicación
        addr = payload.get('address') or {}
        if len((addr.get('delivery_address') or '').strip()) < 10:
            errors['delivery_address'] = _('Este campo es obligatorio: calle, número, colonia y ciudad.')
        if not (addr.get('contact_name') or '').strip():
            errors['contact_name'] = _('Este campo es obligatorio.')
        if not (addr.get('contact_phone') or '').strip():
            errors['contact_phone'] = _('Este campo es obligatorio.')
        lat, lng = float(addr.get('latitude') or 0.0), float(addr.get('longitude') or 0.0)
        if not (lat and lng):
            errors['location'] = _('Ubica la dirección en el mapa: búscala o haz clic en el punto de entrega.')
        # Paso 3 — fecha y condiciones
        today = datetime.now(MONTERREY).date()
        req_date = fields.Date.to_date(payload.get('date')) if payload.get('date') else None
        if not req_date:
            errors['date'] = _('Este campo es obligatorio.')
        elif req_date < today:
            errors['date'] = _('La fecha no puede ser anterior a hoy.')
        tw = payload.get('time_window') or 'any'
        if tw not in dict(TIME_WINDOWS):
            errors['time_window'] = _('Horario inválido.')
        time_exact = float(payload.get('time_exact') or 0.0)
        if tw == 'exact' and not (0 < time_exact < 24):
            errors['time_exact'] = _('Captura la hora exacta.')
        if errors:
            return {'errors': errors}
        partner_id = int(addr.get('partner_id') or 0)
        vals = {
            'sale_order_id': order.id,
            'user_id': order.user_id.id or self.env.uid,
            'date': req_date,
            'time_window': tw,
            'time_exact': time_exact if tw == 'exact' else 0.0,
            'partner_shipping_id': partner_id or False,
            'contact_name': addr['contact_name'].strip(),
            'contact_phone': addr['contact_phone'].strip(),
            'delivery_address': addr['delivery_address'].strip(),
            'latitude': lat,
            'longitude': lng,
            'instructions': (payload.get('instructions') or '').strip() or False,
            'line_ids': lines,
        }
        try:
            rec = self.create(vals)
        except (UserError, ValidationError) as exc:
            return {'errors': {'general': str(exc)}}
        return {'id': rec.id, 'name': rec.name}

    # ------------------------------------------------------------------
    # Liga con la operación (pick ticket / remisión / firma)
    # ------------------------------------------------------------------
    @api.model
    def _find_open_for_order(self, order):
        return self.search([
            ('sale_order_id', '=', order.id), ('state', 'in', OPEN_STATES),
        ], order='date asc, id asc', limit=1)

    def _som_propagate_contact_to_partner(self):
        """Si el contacto de entrega del cliente no tiene teléfono o dirección,
        se le copian los de la programación (una sola vez, solo lo que le
        falta). Si ya tiene datos, se respetan: para la entrega vale lo
        capturado en la programación."""
        for rec in self:
            order = rec.sale_order_id
            partner = (order.partner_shipping_id or order.partner_id).sudo()
            if not partner:
                continue
            vals = {}
            phone = (rec.contact_phone or '').strip()
            if phone and not (partner.phone or ('mobile' in partner._fields and partner.mobile)):
                vals['phone'] = phone
            address = (rec.delivery_address or '').strip()
            if address and not (partner.street or partner.street2 or partner.city or partner.zip):
                # Texto libre de la programación: va completo en calle (una
                # sola línea) para que el contacto deje de estar "sin dirección".
                vals['street'] = ' '.join(address.split())[:256]
            if vals:
                partner.write(vals)
                _logger.info('[SCHEDULE→PARTNER] %s: contacto %s completado con %s',
                             rec.name, partner.display_name, list(vals))

    def _link_pick_ticket(self, doc):
        for rec in self:
            vals = {'pick_ticket_id': doc.id}
            if rec.state in PRE_EXEC_STATES:
                vals['state'] = 'in_progress'
            if doc.vehicle_id and not rec.vehicle_id:
                vals['vehicle_id'] = doc.vehicle_id.id
            if doc.vehicle_driver_id and not rec.vehicle_driver_id:
                vals['vehicle_driver_id'] = doc.vehicle_driver_id.id
            rec.write(vals)

    def _link_remission(self, doc):
        for rec in self:
            vals = {'remission_id': doc.id}
            if rec.state in PRE_EXEC_STATES:
                vals['state'] = 'in_progress'
            if doc.vehicle_id:
                vals['vehicle_id'] = doc.vehicle_id.id
            if doc.vehicle_driver_id:
                vals['vehicle_driver_id'] = doc.vehicle_driver_id.id
            rec.write(vals)

    # ------------------------------------------------------------------
    # Cron: lo no entregado se recorre al día siguiente y se avisa
    # ------------------------------------------------------------------
    @api.model
    def _cron_rollover(self):
        today = datetime.now(MONTERREY).date()
        # Órdenes canceladas o regresadas a cotización no se recorren.
        stale = self.search([
            ('date', '<', today), ('state', 'in', OPEN_STATES),
            ('sale_order_id.state', 'in', ('sale', 'done')),
        ])
        if not stale:
            return 0
        moved = self.env['sale.delivery.schedule']
        for rec in stale:
            old = rec.date
            try:
                # Savepoint: un error SQL ya no aborta la transacción del
                # resto del barrido.
                with self.env.cr.savepoint():
                    rec.with_context(som_schedule_move=True)._rollover_one(old, today)
                moved |= rec
            except Exception:  # noqa: BLE001 — una programación rota no detiene el barrido
                _logger.exception('[PLANIFICACIÓN] no se pudo recorrer %s', rec.name)
        if moved:
            self._notify_rollover(moved, today)
        _logger.info('[PLANIFICACIÓN] rollover: %s entrega(s) recorrida(s) a %s', len(moved), today)
        return len(moved)

    def _rollover_one(self, old, today):
        self.ensure_one()
        self._log_move('reschedule', old, today, _('No se entregó el %s') % _fmt_date(old), 'rollover')
        vals = {'date': today}
        if self.state in PRE_EXEC_STATES:
            vals['state'] = 'rescheduled'
        self.write(vals)
        body = _('⏭️ Entrega <b>no realizada</b> el %s: se recorre automáticamente al <b>%s</b>.') % (
            _fmt_date(old), _fmt_date(today))
        self.message_post(body=body, message_type='notification', subtype_xmlid='mail.mt_note')
        self.sale_order_id.message_post(body=body, message_type='notification', subtype_xmlid='mail.mt_note')
        if self.user_id:
            self.activity_schedule(
                'mail.mail_activity_data_todo', user_id=self.user_id.id,
                summary=_('Entrega reprogramada: %s · %s') % (self.sale_order_id.name, self.partner_id.name or ''),
                note=_('<p>La entrega <b>%s</b> no se realizó el %s y se recorrió al <b>%s</b>. '
                       'Revisa con el cliente y ajusta la fecha si hace falta.</p>') % (
                    self.name, _fmt_date(old), _fmt_date(today)),
                date_deadline=today)

    @api.model
    def _delivery_users(self):
        group = self.env.ref('sale_delivery_wizard.group_delivery_user', raise_if_not_found=False)
        if not group:
            return self.env['res.users']
        users = self.env['res.users']
        for fname in ('all_user_ids', 'user_ids'):
            if fname in group._fields:
                users |= group[fname]
        return users.filtered(lambda u: u.active and not u.share)

    def _notify_rollover(self, moved, today):
        """UNA actividad resumen por usuario de entregas (no una por entrega)."""
        todo = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        lines = ''.join('<li><b>%s</b> · %s · %s (programada el %s)</li>' % (
            r.sale_order_id.name, r.partner_id.name or '', r.name,
            _fmt_date(r.move_ids.filtered(lambda m: m.source == 'rollover')[:1].date_from or r.original_date))
            for r in moved)
        note = _('<p>%d entrega(s) programadas no se realizaron y se recorrieron a hoy (%s):</p><ul>%s</ul>'
                 '<p>Revísalas en Entregas › Planificación.</p>') % (len(moved), _fmt_date(today), lines)
        summary = _('Entregas no realizadas recorridas a hoy: %d') % len(moved)
        Activity = self.env['mail.activity'].sudo()
        for user in self._delivery_users():
            Activity.with_context(mail_activity_quick_update=True).create({
                'activity_type_id': todo.id if todo else False,
                'user_id': user.id,
                'summary': summary,
                'note': note,
                'date_deadline': today,
            })

    # ------------------------------------------------------------------
    # Planificador (client action)
    # ------------------------------------------------------------------
    @api.model
    def _week_start(self, iso_date=None):
        base = fields.Date.to_date(iso_date) if iso_date else datetime.now(MONTERREY).date()
        return base - timedelta(days=base.weekday())

    @api.model
    def planner_data(self, week_start=None, only_mine=False):
        start = self._week_start(week_start)
        return self.planner_range(start.isoformat(), (start + timedelta(days=6)).isoformat(), only_mine)

    @api.model
    def planner_range(self, date_from, date_to, only_mine=False):
        """Días con sus entregas para cualquier rango (día, semana o mes):
        el cliente decide el rango; aquí solo se arma un día por fecha."""
        start = fields.Date.to_date(date_from)
        end = fields.Date.to_date(date_to)
        if not start or not end or end < start:
            raise UserError(_('Rango de fechas inválido.'))
        if (end - start).days > 62:
            raise UserError(_('El rango máximo es de dos meses.'))
        today = datetime.now(MONTERREY).date()
        domain = [('date', '>=', start), ('date', '<=', end)]
        if only_mine:
            domain.append(('user_id', '=', self.env.uid))
        records = self.search(domain)
        by_day = {start + timedelta(days=i): [] for i in range((end - start).days + 1)}
        for rec in records:
            by_day[rec.date].append(self._planner_card(rec))
        overdue = self.search_count([('date', '<', today), ('state', 'in', OPEN_STATES)] +
                                    ([('user_id', '=', self.env.uid)] if only_mine else []))
        days = []
        for d, cards in by_day.items():
            open_cards = [c for c in cards if c['state'] in OPEN_STATES]
            days.append({
                'iso': d.isoformat(),
                'label': '%s %d %s' % (DIAS[d.weekday()], d.day, MESES[d.month - 1]),
                'day': d.day,
                'month': d.month,
                'weekday': d.weekday(),
                'is_today': d == today,
                'is_past': d < today,
                'cards': cards,
                'open_count': len(open_cards),
                'done_count': len([c for c in cards if c['state'] == 'done']),
                'issues': len([c for c in open_cards if c['readiness']]),
            })
        return {
            'week_start': start.isoformat(),
            'date_from': start.isoformat(),
            'date_to': end.isoformat(),
            'week_label': '%d %s – %d %s %d' % (start.day, MESES[start.month - 1], end.day, MESES[end.month - 1], end.year),
            'today': today.isoformat(),
            'days': days,
            'overdue': overdue,
            'is_staff': self.env.user.has_group('sale_delivery_wizard.group_delivery_user'),
            'confirmable': ['scheduled', 'rescheduled'],
            'can_generate': self.env.user.has_group('sale_delivery_wizard.group_delivery_user'),
        }

    def _planner_card(self, rec):
        tw = dict(TIME_WINDOWS)[rec.time_window]
        if rec.time_window == 'exact':
            h = int(rec.time_exact or 0)
            m = int(round(((rec.time_exact or 0) - h) * 60))
            tw = '%02d:%02d h' % (h, m)
        last_move = rec.move_ids.filtered(lambda mv: mv.kind == 'reschedule')[:1]
        return {
            'id': rec.id,
            'name': rec.name,
            'order': rec.sale_order_id.name,
            'order_id': rec.sale_order_id.id,
            'partner': rec.partner_id.name or '',
            'seller': rec.user_id.name or '',
            'contact': rec.contact_name or '',
            'phone': rec.contact_phone or '',
            'address': (rec.delivery_address or '').replace('\n', ', ')[:120],
            'has_location': rec.has_location,
            'lat': rec.latitude,
            'lng': rec.longitude,
            'time_label': tw,
            'time_window': rec.time_window,
            'time_exact': rec.time_exact,
            'state': rec.state,
            'state_label': dict(STATES)[rec.state],
            'auth_ok': rec.auth_ok,
            'auth_label': rec.auth_label,
            'readiness': rec.readiness,
            'vehicle': rec.vehicle_id.display_name if rec.vehicle_id else '',
            'driver': rec.vehicle_driver_id.display_name if rec.vehicle_driver_id else '',
            'qty': rec.qty_summary,
            'materials': rec.material_summary or '',
            'logistics_user': rec.logistics_user_id.name or '',
            'instructions': (rec.instructions or '')[:220],
            'reschedules': rec.reschedule_count,
            'last_move': ('%s → %s (%s)' % (_fmt_date(last_move.date_from), _fmt_date(last_move.date_to),
                                             last_move.user_id.name or '')) if last_move else '',
            'pt': rec.pick_ticket_id.name or '',
            'rem': (rec.remission_id.remission_number or rec.remission_id.name) if rec.remission_id else '',
            'delivered_at': fields.Datetime.context_timestamp(self, rec.delivered_at).strftime('%H:%M') if rec.delivered_at else '',
        }

    @api.model
    def planner_move(self, schedule_id, new_date, reason=''):
        rec = self.browse(int(schedule_id)).exists()
        if not rec:
            return {'error': _('La programación ya no existe.')}
        source = 'logistics' if rec._is_delivery_staff() else 'seller'
        try:
            rec.action_reschedule(new_date, reason=reason, source=source)
        except (UserError, ValidationError) as exc:
            return {'error': str(exc)}
        return {'ok': True}

    @api.model
    def planner_confirm(self, schedule_id):
        rec = self.browse(int(schedule_id)).exists()
        if not rec:
            return {'error': _('La programación ya no existe.')}
        try:
            rec.action_confirm()
        except UserError as exc:
            return {'error': str(exc)}
        return {'ok': True}


class SaleDeliveryScheduleLine(models.Model):
    """Material y cantidad de UNA solicitud de entrega. Solo coordina: la
    entrega física sigue usando el asistente/pick ticket/remisión."""
    _name = 'sale.delivery.schedule.line'
    _description = 'Material de la entrega programada'
    _order = 'schedule_id, sequence, id'

    schedule_id = fields.Many2one('sale.delivery.schedule', required=True, index=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    sale_line_id = fields.Many2one(
        'sale.order.line', 'Línea de venta', required=True, index=True, ondelete='cascade')
    product_id = fields.Many2one(related='sale_line_id.product_id', string='Producto', store=True)
    uom_name = fields.Char('Unidad', compute='_compute_uom_name', store=True)
    qty = fields.Float('Cantidad a entregar', required=True, digits='Product Unit')
    company_id = fields.Many2one(related='schedule_id.company_id', store=True)
    state = fields.Selection(related='schedule_id.state', string='Estado de la entrega')

    @api.depends('sale_line_id')
    def _compute_uom_name(self):
        for line in self:
            sl = line.sale_line_id
            uom = sl.product_uom_id if 'product_uom_id' in sl._fields else getattr(sl, 'product_uom', False)
            line.uom_name = uom.name if uom else ''

    @api.constrains('qty', 'sale_line_id', 'schedule_id')
    def _check_qty(self):
        for line in self:
            if line.qty <= QTY_TOL:
                raise ValidationError(_('La cantidad a entregar de %s debe ser mayor a cero.') % (
                    line.product_id.display_name or ''))
            if line.sale_line_id.order_id != line.schedule_id.sale_order_id:
                raise ValidationError(_('El material %s no pertenece a la orden de la solicitud.') % (
                    line.product_id.display_name or ''))


class SaleDeliveryScheduleMove(models.Model):
    _name = 'sale.delivery.schedule.move'
    _description = 'Movimiento de entrega programada'
    _order = 'create_date desc, id desc'

    schedule_id = fields.Many2one('sale.delivery.schedule', required=True, index=True, ondelete='cascade')
    kind = fields.Selection([('reschedule', 'Reprogramación'), ('cancel', 'Cancelación')], required=True, default='reschedule')
    date_from = fields.Date('De')
    date_to = fields.Date('A')
    reason = fields.Text('Motivo')
    source = fields.Selection([
        ('seller', 'Vendedor'), ('logistics', 'Logística'), ('rollover', 'Automático (no entregada)'), ('manual', 'Cambio directo'),
    ], required=True, default='manual')
    user_id = fields.Many2one('res.users', 'Quién', default=lambda self: self.env.user, readonly=True)
    company_id = fields.Many2one(related='schedule_id.company_id', store=True)


class SaleDeliveryScheduleMoveWizard(models.TransientModel):
    _name = 'sale.delivery.schedule.move.wizard'
    _description = 'Reprogramar entrega'

    schedule_id = fields.Many2one('sale.delivery.schedule', required=True, readonly=True)
    current_date = fields.Date(related='schedule_id.date')
    new_date = fields.Date('Nueva fecha', required=True)
    reason = fields.Text('Motivo', required=True)

    def action_confirm(self):
        self.ensure_one()
        source = 'logistics' if self.schedule_id._is_delivery_staff() else 'seller'
        self.schedule_id.action_reschedule(self.new_date, reason=self.reason, source=source)
        return {'type': 'ir.actions.act_window_close'}


class SaleDeliverySchedulePlanWizard(models.TransientModel):
    _name = 'sale.delivery.schedule.plan.wizard'
    _description = 'Programar entrega: asignar unidad'

    schedule_id = fields.Many2one('sale.delivery.schedule', required=True, readonly=True)
    vehicle_id = fields.Many2one('fleet.vehicle', 'Vehículo', required=True)
    vehicle_driver_id = fields.Many2one('res.partner', 'Chofer',
                                        help='Si lo dejas vacío se toma el chofer de la unidad.')
    logistics_user_id = fields.Many2one('res.users', 'Responsable de logística',
                                        default=lambda self: self.env.user)

    @api.onchange('vehicle_id')
    def _onchange_vehicle_id(self):
        if self.vehicle_id and 'driver_id' in self.vehicle_id._fields and self.vehicle_id.driver_id:
            self.vehicle_driver_id = self.vehicle_id.driver_id

    def action_confirm(self):
        self.ensure_one()
        self.schedule_id.action_program(
            vehicle_id=self.vehicle_id.id,
            driver_id=self.vehicle_driver_id.id or None,
            logistics_user_id=self.logistics_user_id.id or None)
        return {'type': 'ir.actions.act_window_close'}


class SaleDeliveryScheduleCancelWizard(models.TransientModel):
    _name = 'sale.delivery.schedule.cancel.wizard'
    _description = 'Cancelar entrega programada'

    schedule_id = fields.Many2one('sale.delivery.schedule', required=True, readonly=True)
    reason = fields.Text('Motivo', required=True)

    def action_confirm(self):
        self.ensure_one()
        self.schedule_id.action_cancel(reason=self.reason)
        return {'type': 'ir.actions.act_window_close'}


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    delivery_schedule_ids = fields.One2many('sale.delivery.schedule', 'sale_order_id', 'Entregas programadas')
    # CANDADO (21 sep 2026): sin pago registrado ni autorización de entrega
    # sin pago no se programa. Misma regla que el pick ticket.
    x_can_schedule_delivery = fields.Boolean(
        string='Puede programar entrega', compute='_compute_x_can_schedule_delivery',
        help='Verdadero cuando la orden tiene al menos un pago registrado o autorización de entrega sin pago.')

    def _action_cancel(self):
        """Cancelar la orden cierra su logística viva: programaciones
        abiertas y pick tickets preparados. Antes quedaban abiertos para
        siempre (el cron las recorría a diario con actividades y los PT
        seguían reteniendo lotes)."""
        res = super()._action_cancel()
        for order in self:
            open_sched = order.sudo().delivery_schedule_ids.filtered(
                lambda s: s.state in OPEN_STATES)
            if open_sched:
                open_sched.action_cancel(reason=_('Orden %s cancelada') % order.name)
            pts = order.sudo().delivery_document_ids.filtered(
                lambda d: d.document_type == 'pick_ticket'
                and d.state in ('draft', 'prepared'))
            if pts:
                pts.action_cancel()
        return res

    @api.depends('state', 'amount_total')
    def _compute_x_can_schedule_delivery(self):
        for order in self:
            order.x_can_schedule_delivery = not order._som_schedule_block_reason()

    def _som_schedule_block_reason(self):
        """Motivo por el que NO se puede programar la entrega, o False.
        Regla del negocio (21 sep 2026): el vendedor solo programa cuando la
        orden ya es dinero (al menos un pago registrado) o tiene autorización
        de entrega sin pago. Reusa el gate del pick ticket para que las dos
        puertas se abran y cierren juntas."""
        self.ensure_one()
        # Una cotización no se programa (antes devolvía False = "sí se
        # puede" y por RPC se programaban cotizaciones). Sin banderas de
        # contexto para saltar el candado: el contexto lo manda el cliente.
        if self.state not in ('sale', 'done'):
            return _('Solo se programan entregas de órdenes confirmadas (%s).') % self.name
        if not self._som_pick_ticket_block_reason():
            return False
        requested = any(
            r.state in ('draft', 'requested')
            for r in getattr(self, 'delivery_auth_request_ids', []))
        return _(
            'No se puede programar la entrega de %(name)s: la orden no tiene '
            'ningún pago registrado ni autorización de entrega sin pago.\n\n'
            'Registra el anticipo del cliente o solicita "Entregar sin pago" '
            'y, cuando esté aprobada, programa la entrega.%(req)s'
        ) % {
            'name': self.name,
            'req': _('\n\nYa hay una solicitud de autorización pendiente: '
                     'espera a que se apruebe.') if requested else '',
        }
    delivery_schedule_count = fields.Integer(compute='_compute_delivery_schedule_count')
    next_delivery_date = fields.Date('Próxima entrega', compute='_compute_delivery_schedule_count')

    @api.depends('delivery_schedule_ids.state', 'delivery_schedule_ids.date')
    def _compute_delivery_schedule_count(self):
        for order in self:
            open_ones = order.delivery_schedule_ids.filtered(lambda s: s.state in OPEN_STATES).sorted('date')
            order.delivery_schedule_count = len(order.delivery_schedule_ids)
            order.next_delivery_date = open_ones[:1].date if open_ones else False

    def _som_schedule_for_delivery_info(self):
        """Programación cuya información de entrega manda: la del contexto
        (som_schedule_id, desde «Generar entrega») o la abierta más próxima
        de la orden, siempre que tenga teléfono y dirección capturados."""
        self.ensure_one()
        Schedule = self.env['sale.delivery.schedule'].sudo()
        schedule = Schedule.browse(self.env.context.get('som_schedule_id')).exists() \
            if self.env.context.get('som_schedule_id') else Schedule
        if not schedule or schedule.sale_order_id != self:
            schedule = Schedule._find_open_for_order(self)
        if schedule and (schedule.contact_phone or '').strip() and len((schedule.delivery_address or '').strip()) >= 10:
            return schedule
        return Schedule

    def _som_schedule_defaults(self):
        self.ensure_one()
        partner = self.partner_shipping_id or self.partner_id
        phone = partner.phone or self.partner_id.phone or ''
        try:
            address = self._som_get_delivery_address_text()
        except Exception:  # noqa: BLE001 — RedirectWarning por contacto incompleto: se captura a mano
            address = partner.contact_address or ''
        return {
            'default_sale_order_id': self.id,
            'default_partner_shipping_id': partner.id,
            'default_user_id': self.user_id.id or self.env.uid,
            'default_contact_name': partner.name or self.partner_id.name,
            'default_contact_phone': phone,
            'default_delivery_address': address,
            'default_latitude': getattr(partner, 'partner_latitude', 0.0) or 0.0,
            'default_longitude': getattr(partner, 'partner_longitude', 0.0) or 0.0,
            'default_date': (datetime.now(MONTERREY).date() + timedelta(days=1)).isoformat(),
        }

    def action_schedule_delivery(self):
        """Botón «Programar entrega» del vendedor: abre la programación con
        todo lo que ya se sabe de la orden para completar lo que falta."""
        self.ensure_one()
        if self.state not in ('sale', 'done'):
            raise UserError(_('Solo se programan entregas de órdenes confirmadas.'))
        reason = self._som_schedule_block_reason()
        if reason:
            raise UserError(reason)
        # VARIAS ENTREGAS POR ORDEN: cada «Programar entrega» es una
        # solicitud NUEVA. Desde el rediseño (27 sep 2026) se captura en el
        # asistente guiado (materiales → dirección → fecha → especificaciones
        # → resumen), sin datos de logística.
        return {
            'type': 'ir.actions.client', 'name': _('Solicitar entrega'),
            'tag': 'sale_delivery_wizard.delivery_request', 'target': 'current',
            'params': {'order_id': self.id},
            'context': {'som_request_order_id': self.id},
        }

    def action_view_delivery_schedules(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Entregas programadas'),
            'res_model': 'sale.delivery.schedule', 'view_mode': 'list,calendar,form',
            'domain': [('sale_order_id', '=', self.id)],
            'context': {'default_sale_order_id': self.id},
        }


class SaleDeliveryDocument(models.Model):
    _inherit = 'sale.delivery.document'

    schedule_id = fields.Many2one('sale.delivery.schedule', 'Entrega programada', index=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        docs = super().create(vals_list)
        Schedule = self.env['sale.delivery.schedule'].sudo()
        for doc in docs:
            try:
                # Savepoint: un error SQL en la liga (informativa) no debe
                # abortar la transacción de la remisión.
                with self.env.cr.savepoint():
                    doc._som_link_schedule(Schedule)
            except Exception:  # noqa: BLE001 — la liga es informativa; jamás bloquea la operación
                _logger.exception('[PLANIFICACIÓN] no se pudo ligar %s a su programación', doc.name)
        return docs

    def _som_link_schedule(self, Schedule):
        self.ensure_one()
        doc = self
        ctx_sched = Schedule.browse(self.env.context.get('som_schedule_id') or []).exists()
        if ctx_sched and ctx_sched.sale_order_id != doc.sale_order_id:
            ctx_sched = Schedule
        if doc.document_type == 'pick_ticket':
            sched = ctx_sched or Schedule._find_open_for_order(doc.sale_order_id)
            if sched:
                doc.schedule_id = sched.id
                sched._link_pick_ticket(doc)
        elif doc.document_type == 'remission':
            # La del contexto o la de SU pick ticket; la más antigua abierta
            # solo como último recurso (con varias programaciones por orden
            # se cerraba la equivocada).
            sched = ctx_sched or doc.pick_ticket_id.schedule_id \
                or Schedule._find_open_for_order(doc.sale_order_id)
            if sched:
                doc.schedule_id = sched.id
                sched._link_remission(doc)

    def write(self, vals):
        res = super().write(vals)
        if vals.get('signed_at'):
            for doc in self.filtered(lambda d: d.document_type == 'remission' and d.schedule_id):
                try:
                    doc.schedule_id.sudo().action_mark_done(delivered_at=doc.signed_at)
                except Exception:  # noqa: BLE001
                    _logger.exception('[PLANIFICACIÓN] no se pudo cerrar la programación de %s', doc.name)
        if vals.get('state') == 'cancelled':
            for doc in self.filtered(lambda d: d.schedule_id and d.schedule_id.state == 'in_progress'):
                sched = doc.schedule_id.sudo()
                back = sched._som_pre_exec_state()
                if doc.document_type == 'pick_ticket' and sched.pick_ticket_id == doc:
                    sched.write({'pick_ticket_id': False, 'state': back})
                elif doc.document_type == 'remission' and sched.remission_id == doc:
                    sched.write({'remission_id': False, 'state': back if not sched.pick_ticket_id else 'in_progress'})
        return res
